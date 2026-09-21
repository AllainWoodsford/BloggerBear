"""Tests for frontend/markdown.js, the article-body parser the SPA renders from.

The parser is pure (text in, a tree of plain objects out), so these run it under Node and
inspect the tree. Skipped when Node isn't installed -- the parser is browser code and Node
is not otherwise a dependency of this repo.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MARKDOWN_JS = Path(__file__).resolve().parents[2] / "frontend" / "markdown.js"

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_RUNNER = """
const md = require(process.argv[1]);
const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
process.stdout.write(JSON.stringify(md.parse(input.text, input.options)));
"""


def parse(text: str, **options) -> list[dict]:
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(MARKDOWN_JS)],
        input=json.dumps({"text": text, "options": options}),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)


def plain(nodes: list[dict]) -> str:
    """The visible text of a run of inline nodes."""
    out = []
    for node in nodes:
        if "text" in node:
            out.append(node["text"])
        else:
            out.append(plain(node.get("children", [])))
    return "".join(out)


def test_headings_are_shifted_below_the_page_h1():
    blocks = parse("# One\n\n## Two\n\n### Three", headingOffset=1)
    assert [(b["type"], b["level"], plain(b["children"])) for b in blocks] == [
        ("heading", 2, "One"),
        ("heading", 3, "Two"),
        ("heading", 4, "Three"),
    ]


def test_headings_default_to_their_own_level_and_cap_at_six():
    assert parse("## Two")[0]["level"] == 2
    assert parse("###### Six", headingOffset=2)[0]["level"] == 6


def test_the_screenshot_article_shape_becomes_real_headings():
    text = (
        "# How Cloudflare's Skill Is Capitalizing on the Wave\n\n"
        "## The Rise of Agentic Security\n\n"
        "GitHub Trending is shifting, and **security-audit-skill** is riding it."
    )
    blocks = parse(text, headingOffset=1)
    assert [b["type"] for b in blocks] == ["heading", "heading", "paragraph"]
    assert [b.get("level") for b in blocks[:2]] == [2, 3]
    paragraph = blocks[2]["children"]
    assert [n["type"] for n in paragraph] == ["text", "strong", "text"]
    assert plain(paragraph[1]["children"]) == "security-audit-skill"


def test_a_hash_without_a_space_is_not_a_heading():
    blocks = parse("#hashtag is just text")
    assert blocks[0]["type"] == "paragraph"


def test_paragraphs_split_on_blank_lines_and_join_soft_breaks():
    blocks = parse("first line\nsecond line\n\nnext paragraph")
    assert [plain(b["children"]) for b in blocks] == ["first line second line", "next paragraph"]


def test_emphasis_and_code():
    (block,) = parse("a *soft* and **hard** and `x < y` word")
    assert [n["type"] for n in block["children"]] == ["text", "em", "text", "strong", "text", "code", "text"]
    assert block["children"][5]["text"] == "x < y"


def test_snake_case_and_stray_stars_are_left_alone():
    (block,) = parse("the file_name_here costs 5 * 3 and 2*4")
    assert block["children"] == [{"type": "text", "text": "the file_name_here costs 5 * 3 and 2*4"}]


def test_escapes_are_honoured():
    (block,) = parse(r"not \*bold\* here")
    assert plain(block["children"]) == "not *bold* here"
    assert all(n["type"] == "text" for n in block["children"])


def test_links_keep_only_http_and_https():
    (block,) = parse("[good](https://example.com/a?b=1) and [bad](javascript:alert(1))")
    link = block["children"][0]
    assert link == {
        "type": "link",
        "href": "https://example.com/a?b=1",
        "children": [{"type": "text", "text": "good"}],
    }
    assert all(n["type"] != "link" for n in block["children"][1:])
    assert "bad" in plain(block["children"])
    assert "javascript" not in plain(block["children"])


@pytest.mark.parametrize(
    "target",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,x",
        "//evil.example",
        "/relative",
        "ftp://x.y",
    ],
)
def test_unsafe_or_relative_link_targets_never_become_links(target):
    (block,) = parse(f"[click]({target})")
    assert [n["type"] for n in block["children"]] == ["text"]
    assert plain(block["children"]) == "click"


def test_raw_html_is_just_text():
    (block,) = parse('<script>alert(1)</script> and <img src=x onerror=alert(1)>')
    assert all(n["type"] == "text" for n in block["children"])
    assert "<script>" in plain(block["children"])


def test_bullet_and_numbered_lists():
    bullets, numbers = parse("- one\n- two\n\n3. three\n4. four")
    assert bullets["type"] == "list" and not bullets["ordered"]
    assert [plain(i["inline"]) for i in bullets["items"]] == ["one", "two"]
    assert numbers["ordered"] and numbers["start"] == 3
    assert [plain(i["inline"]) for i in numbers["items"]] == ["three", "four"]


def test_nested_list():
    (block,) = parse("- parent\n  - child a\n  - child b\n- sibling")
    assert [plain(i["inline"]) for i in block["items"]] == ["parent", "sibling"]
    (nested,) = block["items"][0]["blocks"]
    assert nested["type"] == "list"
    assert [plain(i["inline"]) for i in nested["items"]] == ["child a", "child b"]


def test_list_item_continuation_line_joins_the_item():
    (block,) = parse("- first part\n  second part\n- other")
    assert plain(block["items"][0]["inline"]) == "first part second part"


def test_a_list_ends_at_a_paragraph_after_a_blank_line():
    blocks = parse("- item\n\nA paragraph.")
    assert [b["type"] for b in blocks] == ["list", "paragraph"]


def test_blockquote_hr_and_fenced_code():
    blocks = parse("> quoted **text**\n\n---\n\n```\n<b>not bold</b>\n# not a heading\n```")
    assert [b["type"] for b in blocks] == ["blockquote", "hr", "code"]
    assert blocks[0]["children"][0]["type"] == "paragraph"
    assert blocks[2]["text"] == "<b>not bold</b>\n# not a heading"


def test_table_with_alignment_and_ragged_rows():
    text = "| Coin | Price | Change |\n| :-- | --: | :-: |\n| BTC | 100 | 1% |\n| ETH | 5 |"
    (table,) = parse(text)
    assert table["type"] == "table"
    assert [plain(cell) for cell in table["head"]] == ["Coin", "Price", "Change"]
    assert table["aligns"] == ["left", "right", "center"]
    assert [[plain(c) for c in row] for row in table["rows"]] == [
        ["BTC", "100", "1%"],
        ["ETH", "5", ""],
    ]


def test_a_pipe_in_prose_is_not_a_table():
    (block,) = parse("this | that\nand more")
    assert block["type"] == "paragraph"


def test_paragraph_stops_at_a_following_heading_or_list():
    blocks = parse("some text\n## Heading\nmore text\n- item")
    assert [b["type"] for b in blocks] == ["paragraph", "heading", "paragraph", "list"]


def test_empty_and_odd_input_never_raises():
    assert parse("") == []
    assert parse("\n\n   \n") == []
    for text in ["**", "[", "[a](", "`", "|", "- ", "> ", "```", "* * *", "\\", "[a]()"]:
        parse(text)  # must not throw or hang


def test_windows_line_endings():
    blocks = parse("# T\r\n\r\nBody line\r\nsecond\r\n\r\n- a\r\n- b")
    assert [b["type"] for b in blocks] == ["heading", "paragraph", "list"]
    assert plain(blocks[1]["children"]) == "Body line second"


def test_a_long_pathological_input_finishes_quickly():
    parse("*" * 5000 + "a" * 5000 + "[" * 2000 + "_" * 3000)
