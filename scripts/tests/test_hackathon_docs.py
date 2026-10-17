"""The submission round's rubric tracker (docs/hackathon/rubric-tracker.md) keeps the format its
readers rely on: one Markdown table with nine named columns; 25 rows, one per rubric line of the
OpenCV AI Competition 2026, one for the owner's own ask and one per owner step; a verdict word and
needs from closed lists; evidence that cites repository paths in backticks and links only to the
repository or the competition's pages; a "Say" phrase from the closed list below; an ISO date; and
no cell long enough, or worded so, that it could steer the assistant that may one day read it.

Why a test: every submission-round PR updates the rows it closes, the owner updates the owner rows
by hand, and the ops assistant's `submission_status` (if it is built) ships a copy of this table
inside the MCP Lambda, where `redact.looks_like_instructions` is the rule for text a model reads.
A malformed row fails here, not in a judge's or the assistant's reading of it. The Python CI runs
this file on every pull request that touches `docs/hackathon/`, `lambdas/` or `scripts/`.

The rest of the submission package is held here too: the technical report keeps the rubric's eight
sections in order; each diagram's Mermaid source has its SVG, appears verbatim in every page that
embeds it, and names what the rubric asks for; README.md sends judges to the package first; every
placeholder is marked for R2 in one form; and REPRODUCE.md's tier 1a is CI's own commands with only
the judges' constraints file added, parsed from `python-ci.yml`, so it cannot drift unseen. The
review fixes are held too: each SVG carries every word of its source whole, the workflow's captions
match the diagram and the code, no page names a CI run on the tag as the freeze's source, and a
number taken from a design doc is marked indicative.

The parser is deliberately plain: a cell holds no `|`, so a row is split on it. The "Say" list is
`SAY_PHRASES` until `scripts/submission_index.py` exists; the test then imports it from there.
Nothing here touches the network.
"""

from __future__ import annotations

import html
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml

# ops_mcp imports common.*: scripts/pyproject.toml puts only scripts/ on the path when a scripts
# test runs on its own, so the test adds lambdas/ the way scripts/vision_benchmark.py does.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas"))

from common import vision_triage
from ops_mcp import redact, suggestions

import submission_check

ROOT = Path(__file__).resolve().parents[2]
HACKATHON = ROOT / "docs" / "hackathon"
TRACKER = HACKATHON / "rubric-tracker.md"
PYTHON_CI = ROOT / ".github" / "workflows" / "python-ci.yml"
# The path filter that would run this file on a pull request that changes only the tracker.
TRACKER_PATH_FILTER = "docs/hackathon/**"

COLUMNS = ("ID", "Line", "Status", "Needs", "Evidence", "Gap", "Closed by", "Say", "Checked")
ID_PATTERN = re.compile(r"^(S[1-7]|C[1-3]|A[1-3]|T1|O([1-9]|1[01]))$")
# Every id the pattern allows: the table has exactly one row for each of them.
ALL_IDS = frozenset(
    [f"S{n}" for n in range(1, 8)]
    + [f"C{n}" for n in range(1, 4)]
    + [f"A{n}" for n in range(1, 4)]
    + ["T1"]
    + [f"O{n}" for n in range(1, 12)]
)
STATUSES = ("met", "partly", "missing")
NEEDS = ("code", "docs", "deploy", "run", "owner")
NOTHING_NEEDED = "-"
CELL_MAX_CHARS = 300
# Where a cell may link: the repository's owner on GitHub (the first path segment, read as a
# browser reads it), and the competition's pages.
GITHUB_HOST = "github.com"
GITHUB_OWNER = "AllainWoodsford"
COMPETITION_HOSTS = ("opencv.org", "devpost.com")
DEMO_TOPICS_PREFIX = "Demo topics:"
# What the assistant may say for a row: a closed list, so no table cell ever becomes speech.
SAY_PHRASES = (
    "the technical report",
    "repository access",
    "the reproduction guide",
    "the architecture diagram",
    "the live article",
    "the video",
    "the evaluation",
    "the COOL configuration",
    "the COOL benchmark",
    "the COOL proof",
    "the agent diagram",
    "the agent trace",
    "the agent evaluation",
    "asking the assistant",
    "the deploy",
    "the city topics",
    "the demo topic",
    "approving an article",
    "the Graviton session",
    "the Devpost form",
    "the submission tag",
    "the evidence export",
    "the production release",
    "keeping the site up",
)

_BACKTICKED = re.compile(r"`([^`]+)`")
# A repo-relative path in backticks, with the `:line` or `:from-to` anchor the specifications cite
# allowed after it: path characters only, with a directory or an extension of letters, so a
# backticked flag, variable or version (`VISION_ENABLED`, `--pipeline daily_cycle`, `3.11`) is not
# one. A `.` or `..` segment matches, so the existence test can refuse it rather than skip it.
_PATH_LIKE = re.compile(r"^(?P<path>[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?)(?::\d+(?:-\d+)?)?$")
_EXTENSION = re.compile(r"\.[A-Za-z]+$")
_DOT_SEGMENTS = (".", "..")
_URL = re.compile(r"https?://[^\s<>()`\"']+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
# A link's target: inline, with or without a quoted title, or a reference definition on a line of
# its own (`[ref]: target`; a footnote, `[^1]: text`, is not one).
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[(?!\^)[^\]]+\]:[ \t]*(\S+)", re.M)
# python-ci.yml's pull_request path filters, read from the text as test_terraform_wiring.py reads
# the workflows (PyYAML is not a dependency); a comment line between two entries is part of the list.
_PULL_REQUEST_PATHS = re.compile(
    r"^on:\n  pull_request:\n    paths:\n((?:      (?:- '[^']+'|#[^\n]*)\n)+)", re.M
)
_PATH_ENTRY = re.compile(r"^      - '([^']+)'$", re.M)


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    assert stripped.startswith("|") and stripped.endswith("|"), f"not a table row: {line!r}"
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_table(text: str) -> list[dict[str, str]]:
    """The tracker's one table as a list of rows keyed by column. Asserts the shape every reader
    relies on: one contiguous table, the nine columns in order, a separator row, and nine cells in
    every row."""
    numbered = [(n, line) for n, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("|")]
    assert len(numbered) >= 3, "the tracker has no table (a header, a separator and at least one row)"
    first, last = numbered[0][0], numbered[-1][0]
    assert last - first + 1 == len(numbered), "the tracker holds more than one table, or a broken one"
    header, separator, *rows = (line for _, line in numbered)
    assert tuple(_cells(header)) == COLUMNS, f"the columns are {_cells(header)}, not {list(COLUMNS)}"
    separator_cells = _cells(separator)
    assert len(separator_cells) == len(COLUMNS) and all(_SEPARATOR_CELL.match(c) for c in separator_cells), (
        f"the separator row is malformed: {separator!r}"
    )
    parsed = []
    for offset, line in enumerate(rows):
        cells = _cells(line)
        assert len(cells) == len(COLUMNS), (
            f"row {first + 2 + offset} has {len(cells)} cells, not {len(COLUMNS)} (a `|` inside a cell?)"
        )
        parsed.append(dict(zip(COLUMNS, cells, strict=True)))
    return parsed


def demo_topics(text: str) -> list[str]:
    """The ids in the header's `Demo topics:` paragraph: the line that starts with the prefix and
    every line after it up to a blank one, since the paragraph wraps. The ids are the backticked
    tokens of the whole paragraph, and the readers that report these topics apart (P2a, P2b, Q3)
    parse it the same way."""
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.startswith(DEMO_TOPICS_PREFIX)]
    assert len(starts) == 1, f"expected one paragraph starting {DEMO_TOPICS_PREFIX!r}, found {len(starts)}"
    paragraph = []
    for line in lines[starts[0]:]:
        if not line.strip():
            break
        paragraph.append(line)
    return _BACKTICKED.findall(" ".join(paragraph))


def backticked_paths(cell: str) -> list[str]:
    """The backticked tokens of a cell that name a repo-relative path, their line anchors removed."""
    paths = []
    for token in _BACKTICKED.findall(cell):
        match = _PATH_LIKE.match(token)
        if match and ("/" in match["path"] or _EXTENSION.search(match["path"])):
            paths.append(match["path"])
    return paths


def _in_repository(path: str) -> bool:
    """Whether a cited path exists under the checkout, by name: a `.` or `..` segment is refused,
    since it can resolve to something outside it (the raw evidence directory O2 keeps beside the
    checkout, say) on one machine and not another."""
    segments = path.rstrip("/").split("/")
    return not any(segment in _DOT_SEGMENTS for segment in segments) and (ROOT / path).exists()


def _allowed_url(url: str) -> bool:
    """https with no userinfo, and either `github.com` under the repository's owner or a
    competition host. The owner is the first path segment, with no `.` or `..` segment after it
    (a browser folds `/AllainWoodsford/../evil` into another owner before the request), `\\` read
    as `/` and `%2e` as `.`, as a browser's URL parser reads them."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.hostname or ""
    if parts.scheme != "https" or parts.username is not None or not host:
        return False
    if host == GITHUB_HOST:
        segments = [s.lower().replace("%2e", ".") for s in parts.path.replace("\\", "/").split("/")[1:]]
        return (
            bool(segments)
            and segments[0] == GITHUB_OWNER.lower()
            and not any(segment in _DOT_SEGMENTS for segment in segments)
        )
    return any(host == h or host.endswith("." + h) for h in COMPETITION_HOSTS)


def dead_links(root: Path, pages: list[Path]) -> list[tuple[str, str]]:
    """The (page, target) pairs of the links whose target file is not in the repository: a
    relative target is resolved against the page's directory, a root-relative one (`/docs/x.md`,
    which GitHub resolves against the repository) against `root`, and either must land inside
    `root`. External and mailto links are not followed."""
    dead = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        for target in _MARKDOWN_LINK.findall(text) + _LINK_DEFINITION.findall(text):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if not path:
                continue
            resolved = root / path.lstrip("/") if path.startswith("/") else page.parent / path
            if not (resolved.exists() and resolved.resolve().is_relative_to(root.resolve())):
                dead.append((page.relative_to(root).as_posix(), target))
    return dead


def python_ci_pull_request_paths() -> list[str]:
    match = _PULL_REQUEST_PATHS.search(PYTHON_CI.read_text(encoding="utf-8"))
    assert match, "python-ci.yml's pull_request trigger has no paths list where this test reads it"
    return _PATH_ENTRY.findall(match[1])


@pytest.fixture
def tracker_text() -> str:
    return TRACKER.read_text(encoding="utf-8")


@pytest.fixture
def rows(tracker_text) -> list[dict[str, str]]:
    return parse_table(tracker_text)


def _each_cell(rows):
    for row in rows:
        for column in COLUMNS:
            yield row["ID"], column, row[column]


def test_the_table_parses_with_the_nine_columns_in_order(tracker_text):
    rows = parse_table(tracker_text)
    assert rows, "the table has no rows"
    assert all(tuple(row) == COLUMNS for row in rows)


def test_the_ids_match_the_pattern_and_cover_all_25_rows(rows):
    ids = [row["ID"] for row in rows]
    bad = [i for i in ids if not ID_PATTERN.match(i)]
    assert not bad, f"ids outside the rubric's pattern: {bad}"
    assert len(ids) == len(set(ids)), f"duplicate ids: {sorted({i for i in ids if ids.count(i) > 1})}"
    assert set(ids) == ALL_IDS, (
        f"missing {sorted(ALL_IDS - set(ids))}, unexpected {sorted(set(ids) - ALL_IDS)}"
    )
    assert len(rows) == 25


def test_status_is_a_verdict_word(rows):
    bad = [(row["ID"], row["Status"]) for row in rows if row["Status"] not in STATUSES]
    assert not bad, f"Status must be one of {STATUSES}: {bad}"


def test_needs_is_from_the_closed_list_or_a_dash_once_met(rows):
    for row in rows:
        needs = row["Needs"]
        if row["Status"] == "met":
            assert needs == NOTHING_NEEDED, (
                f"{row['ID']} is met, so Needs must be {NOTHING_NEEDED!r}, not {needs!r}"
            )
            continue
        values = [value.strip() for value in needs.split(",")]
        assert values and all(value in NEEDS for value in values), (
            f"{row['ID']}: Needs must be comma-separated values from {NEEDS}, not {needs!r}"
        )
        assert len(values) == len(set(values)), f"{row['ID']}: Needs repeats a value: {needs!r}"


def test_every_backticked_path_of_a_met_row_exists(rows):
    missing = [
        (row["ID"], path)
        for row in rows
        if row["Status"] == "met"
        for path in backticked_paths(row["Evidence"])
        if not _in_repository(path)
    ]
    assert not missing, (
        f"a met row cites a path that is not in the repository (missing, or with a `.` or `..` segment): "
        f"{missing}"
    )


def test_backticked_paths_are_files_and_directories_without_their_anchors_not_versions():
    cell = (
        "`README.md`, `lambdas/common/vision_triage.py:391-393`, `research_schedule.py:23`, "
        "`docs/hackathon/`, `.gitignore` and `../bloggerbear-evidence-raw/x.json`; not `3.11`, "
        "`1.16.2`, `VISION_ENABLED`, `--pipeline daily_cycle`, `rail-sydney-demo` or `0ef08003c311`"
    )
    assert backticked_paths(cell) == [
        "README.md",
        "lambdas/common/vision_triage.py",
        "research_schedule.py",
        "docs/hackathon/",
        ".gitignore",
        "../bloggerbear-evidence-raw/x.json",
    ]


def test_a_path_with_a_dot_segment_is_not_in_the_repository_even_when_it_exists():
    assert _in_repository("README.md")
    assert _in_repository("docs/hackathon/")
    assert not _in_repository("docs/hackathon/not-written-yet.md")
    assert not _in_repository("docs/../README.md")
    assert (ROOT / ".." / ROOT.name / "README.md").exists()
    assert not _in_repository(f"../{ROOT.name}/README.md")


def test_urls_point_only_at_the_repository_or_the_competition(rows):
    bad = [
        (row_id, column, url)
        for row_id, column, cell in _each_cell(rows)
        for url in _URL.findall(cell)
        if not _allowed_url(url.rstrip(".,;:"))
    ]
    assert not bad, (
        f"a cell links outside https://{GITHUB_HOST}/{GITHUB_OWNER}/ and {COMPETITION_HOSTS}: {bad}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/AllainWoodsford",
        "https://github.com/AllainWoodsford/BloggerBear",
        "https://github.com/allainwoodsford/BloggerBear/blob/dev/README.md",
        "https://opencv.org/opencv-ai-competition-2026/",
        "https://opencv-ai-competition-2026.devpost.com/",
    ],
)
def test_the_url_rule_allows_the_owner_and_the_competition(url):
    assert _allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/AllainWoodsford/BloggerBear",
        "https://github.com/AllainWoodsford/../evil/repo",
        "https://github.com/AllainWoodsford/%2E%2E/evil/repo",
        "https://github.com/AllainWoodsford\\..\\evil/repo",
        "https://github.com/AllainWoodsford/./BloggerBear",
        "https://github.com/AllainWoodsfordX/repo",
        "https://github.com/",
        "https://github.com",
        # Joined here, since written out these would read as addresses to the gitleaks email rule.
        "@".join(("https://evil.example.com", "opencv.org/x")),
        "@".join(("https://github.com", "evil.example.com/AllainWoodsford/x")),
        "https://opencv.org.evil.com/x",
        "https://example.com/AllainWoodsford/BloggerBear",
        "https://[bad",
    ],
)
def test_the_url_rule_refuses_another_owner_host_scheme_or_userinfo(url):
    assert not _allowed_url(url)


def test_say_is_from_the_closed_list(rows):
    bad = [(row["ID"], row["Say"]) for row in rows if row["Say"] not in SAY_PHRASES]
    assert not bad, f"Say must be one of SAY_PHRASES: {bad}"


def test_checked_is_an_iso_date(rows):
    for row in rows:
        checked = row["Checked"]
        assert _ISO_DATE.match(checked), f"{row['ID']}: Checked must be YYYY-MM-DD, not {checked!r}"
        try:
            date.fromisoformat(checked)
        except ValueError:
            pytest.fail(f"{row['ID']}: Checked is not a calendar date: {checked!r}")


def test_every_cell_is_filled_and_at_most_300_characters(rows):
    for row_id, column, cell in _each_cell(rows):
        assert cell, f"{row_id}: {column} is empty"
        assert len(cell) <= CELL_MAX_CHARS, (
            f"{row_id}: {column} is {len(cell)} characters, over {CELL_MAX_CHARS}"
        )


def test_the_demo_topics_line_parses_and_each_id_is_a_topic_id(tracker_text):
    ids = demo_topics(tracker_text)
    assert ids, f"the {DEMO_TOPICS_PREFIX!r} paragraph names no topic in backticks"
    bad = [i for i in ids if not suggestions.ID_PATTERN.match(i)]
    assert not bad, f"demo topic ids that are not topic ids: {bad}"


def test_demo_topics_reads_the_whole_paragraph_not_only_its_first_line():
    text = (
        "Demo topics: `rail-sydney-demo` (thresholds of 0, disclosed in the report; every count reports it\n"
        "apart from the cities) and `rail-melbourne-demo`.\n"
        "\n"
        "Planned documents, named here until they exist: `docs/hackathon/technical-report.md`.\n"
    )
    assert demo_topics(text) == ["rail-sydney-demo", "rail-melbourne-demo"]


def test_no_cell_reads_like_instructions_to_the_assistant(rows):
    flagged = [
        (row_id, column) for row_id, column, cell in _each_cell(rows) if redact.looks_like_instructions(cell)
    ]
    assert not flagged, f"cells the assistant's redaction would withhold: {flagged}"


def test_every_link_under_docs_hackathon_resolves_inside_the_repository():
    """The tracker names a planned document as a backticked path, not a link, until it exists: a
    link to a missing file is dead on the page, and fails here."""
    dead = dead_links(ROOT, sorted(HACKATHON.rglob("*.md")))
    assert not dead, f"links to files that do not exist: {dead}"


def test_dead_links_reads_root_relative_titled_and_reference_links(tmp_path):
    (tmp_path / "docs" / "hackathon").mkdir(parents=True)
    (tmp_path / "docs" / "runsheet.md").write_text("# The runsheet\n", encoding="utf-8")
    page = tmp_path / "docs" / "hackathon" / "page.md"
    page.write_text(
        "[relative](../runsheet.md) and [root-relative](/docs/runsheet.md#a-heading), "
        '[titled](../runsheet.md "The runsheet"), [by reference][ref] and [external](https://opencv.org/).\n'
        "[missing](/docs/missing.md), [outside](/etc/passwd) and "
        "[climbing](../../../../../../../../etc/passwd).\n"
        "\n"
        '[ref]: ../runsheet.md "The runsheet"\n'
        "[gone]: ../gone.md\n"
        "[^1]: A footnote, not a link.\n",
        encoding="utf-8",
    )
    assert dead_links(tmp_path, [page]) == [
        ("docs/hackathon/page.md", "/docs/missing.md"),
        ("docs/hackathon/page.md", "/etc/passwd"),
        ("docs/hackathon/page.md", "../../../../../../../../etc/passwd"),
        ("docs/hackathon/page.md", "../gone.md"),
    ]


def test_a_change_to_this_file_runs_it_in_the_python_ci():
    assert "scripts/**" in python_ci_pull_request_paths()


def test_a_change_to_the_tracker_alone_runs_this_file_in_the_python_ci():
    assert TRACKER_PATH_FILTER in python_ci_pull_request_paths()


def test_the_path_filter_is_read_past_the_comments_between_its_entries():
    paths = python_ci_pull_request_paths()
    assert ".claude/skills/**" in paths and TRACKER_PATH_FILTER in paths
    assert not any(path.startswith("#") for path in paths)


# --- the submission package (R1) -----------------------------------------------------------------

REPORT = HACKATHON / "technical-report.md"
REPRODUCE = HACKATHON / "REPRODUCE.md"
DIAGRAMS = HACKATHON / "diagrams"
README = ROOT / "README.md"
BLOGGER_VISION = ROOT / "docs" / "architecture" / "blogger-vision.md"
# The rubric's eight sections, in its order (decision 15: the rubric's headings, not Devpost's).
REPORT_SECTIONS = (
    "1. The problem",
    "2. Users",
    "3. Architecture",
    "4. The OpenCV 5 implementation",
    "5. AWS deployment",
    "6. Evaluation",
    "7. Limitations",
    "8. Responsible use",
)
REPORT_APPENDICES = ("Appendix A. Best use of COOL", "Appendix B. Agentic Vision", "Appendix C. Reproduce")
# Each diagram, the pages that embed its source verbatim, and the words the rubric's lines ask it to
# show (S4: the OpenCV 5 and AWS components, COOL, the agent's news search and its fallback, the
# assistant, the rotation; A1: the loop's tools, the fail-closed edges and the person).
DIAGRAM_PAGES = {
    "architecture": (REPORT, BLOGGER_VISION),
    "agent-workflow": (REPORT, BLOGGER_VISION),
}
DIAGRAM_WORDS = {
    "architecture": (
        "vision worker", "us-west-2", "sentinel-cogs", "OpenCV 5", "COOL", "Bedrock", "Overpass",
        "Earth Search", "GDELT", "AgentCore", "MCP", "Alexa+", "CloudFront", "rota",
    ),
    "agent-workflow": (
        "look_again", "previous_scene", "site_history", "web_context", "deadline", "no model", "artefact",
        "No Finding", "a person", "rota",
    ),
}
# The one form every placeholder in the package takes, so R2 (and submission_check.py) find them all:
# in brackets in prose, in angle brackets inside code (where brackets are code).
PLACEHOLDER_PREFIXES = ("[R2: ", "<R2: ")
CONSTRAINTS_FLAG = ("-c", "constraints-py311.txt")


def _level_two_headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _mermaid_blocks(text: str) -> list[str]:
    return re.findall(r"^```mermaid\n(.*?)^```$", text, re.M | re.S)


def _fenced_after(text: str, heading: str, language: str) -> str:
    """The first ```<language> block after the line `heading`."""
    start = text.index(heading)
    match = re.compile(rf"^```{language}\n(.*?)^```$", re.M | re.S).search(text, start)
    assert match, f"no ```{language} block after {heading!r}"
    return match.group(1)


def _commands(block: str) -> list[list[str]]:
    """Each line of a shell block as its tokens, `&&` and `;` splitting a line into commands."""
    commands = []
    for line in block.splitlines():
        for part in re.split(r"\s*(?:&&|;)\s*", line.strip()):
            if part:
                commands.append(part.split())
    return commands


def _without_constraints(tokens: list[str]) -> list[str]:
    out, skip = [], False
    for n, token in enumerate(tokens):
        if skip:
            skip = False
            continue
        if tuple(tokens[n : n + 2]) == CONSTRAINTS_FLAG:
            skip = True
            continue
        out.append(token)
    return out


def ci_commands() -> dict[str, list]:
    """What python-ci.yml runs, read from its text: every pip install of the install step, in order,
    the ruff check (not its --fix retry) and the pytest line."""
    text = PYTHON_CI.read_text(encoding="utf-8")
    start = text.index("- name: Install dependencies")
    end = text.index("- name: Print the installed packages")
    pips = [
        line.strip().split()
        for line in text[start:end].splitlines()
        if re.match(r"^\s*(python -m )?pip install ", line)
    ]
    ruff = [m.group(1).split() for m in re.finditer(r"\b(ruff check [^;\n]+?)\s*(?:;|$)", text, re.M)]
    pytest_lines = [line.strip().split() for line in text.splitlines() if re.match(r"^\s*pytest ", line)]
    return {"pip": pips, "ruff": [r for r in ruff if "--fix" not in r], "pytest": pytest_lines}


def test_the_report_has_the_rubrics_eight_sections_in_order_then_the_appendices():
    headings = _level_two_headings(REPORT.read_text(encoding="utf-8"))
    assert headings == list(REPORT_SECTIONS) + list(REPORT_APPENDICES), headings


def test_every_diagram_has_its_svg_and_is_embedded_verbatim():
    sources = sorted(DIAGRAMS.glob("*.mmd"))
    assert {path.stem for path in sources} == set(DIAGRAM_PAGES)
    for source in sources:
        svg = source.with_suffix(".svg")
        assert svg.exists(), f"{source.name} has no SVG export (docs/hackathon/diagrams/README.md says how)"
        assert svg.read_text(encoding="utf-8").lstrip().startswith("<svg"), f"{svg.name} is not an SVG"
        text = source.read_text(encoding="utf-8")
        for page in DIAGRAM_PAGES[source.stem]:
            assert text in _mermaid_blocks(page.read_text(encoding="utf-8")), (
                f"{page.relative_to(ROOT)} does not embed {source.name} verbatim"
            )


@pytest.mark.parametrize("name", sorted(DIAGRAM_WORDS))
def test_each_diagram_names_what_the_rubric_asks_for(name):
    text = (DIAGRAMS / f"{name}.mmd").read_text(encoding="utf-8")
    missing = [word for word in DIAGRAM_WORDS[name] if word not in text]
    assert not missing, f"{name}.mmd does not show {missing}"


def test_the_readme_sends_judges_to_the_package_first_and_draws_the_vision_worker():
    text = README.read_text(encoding="utf-8")
    top = "\n".join(text.splitlines()[:20])
    assert "Judges" in top and "start here" in top
    for target in ("docs/hackathon/REPRODUCE.md", "docs/hackathon/technical-report.md"):
        assert f"]({target})" in top, f"README's judges' line does not link {target}"
    (diagram,) = _mermaid_blocks(text)
    assert "vision worker" in diagram and "sentinel-cogs" in diagram


def test_the_judges_pages_resolve_their_links():
    dead = dead_links(ROOT, [README, BLOGGER_VISION])
    assert not dead, f"links to files that do not exist: {dead}"


def test_every_placeholder_in_the_package_is_marked_for_r2():
    unmarked = [
        (doc.as_posix(), line, span)
        for doc in submission_check.PACKAGE_DOCS
        for line, span in submission_check.placeholders((ROOT / doc).read_text(encoding="utf-8"))
        if not span.startswith(PLACEHOLDER_PREFIXES)
    ]
    assert not unmarked, f"a placeholder not in the form [R2: ...] or <R2: ...>: {unmarked}"


def _is_pip_install(command: list[str]) -> bool:
    return command[:2] == ["pip", "install"] or command[:4] == ["python", "-m", "pip", "install"]


def test_reproduce_tier_1a_is_cis_commands_with_only_the_constraints_file_added():
    tier = _fenced_after(REPRODUCE.read_text(encoding="utf-8"), "## Tier 1a", "bash")
    venv, activate, *rest = _commands(tier)
    assert venv == ["python3.11", "-m", "venv", ".venv"] and activate == [".", ".venv/bin/activate"]
    pips = [command for command in rest if _is_pip_install(command)]
    checks = [command for command in rest if not _is_pip_install(command)]
    ci = ci_commands()
    assert all(" ".join(CONSTRAINTS_FLAG) in " ".join(c) for c in pips), "a pip install without -c"
    assert [_without_constraints(c) for c in pips] == ci["pip"], (pips, ci["pip"])
    assert checks == ci["ruff"] + ci["pytest"], (checks, ci["ruff"], ci["pytest"])


def test_reproduce_tier_1a_in_powershell_is_the_same_commands():
    text = REPRODUCE.read_text(encoding="utf-8")
    bash = _commands(_fenced_after(text, "## Tier 1a", "bash"))[2:]
    powershell = _commands(_fenced_after(text, "## Tier 1a", "powershell"))
    assert powershell[:2] == [["py", "-3.11", "-m", "venv", ".venv"], [".\\.venv\\Scripts\\Activate.ps1"]]
    assert [[token.replace("\\", "/") for token in c] for c in powershell[2:]] == bash


def test_the_reports_appendix_c_carries_tier_1a_exactly():
    reproduce = _fenced_after(REPRODUCE.read_text(encoding="utf-8"), "## Tier 1a", "bash")
    report = _fenced_after(REPORT.read_text(encoding="utf-8"), "## Appendix C. Reproduce", "bash")
    assert report == reproduce


def test_ci_prints_its_environment_after_installing_it():
    text = PYTHON_CI.read_text(encoding="utf-8")
    assert text.index("- name: Install dependencies") < text.index("- name: Print the installed packages")
    step = text[text.index("- name: Print the installed packages") : text.index("- name: Ruff lint")]
    assert "run: pip freeze --all" in step


def test_tier_1a_parsing_reads_cis_three_installs_and_its_two_checks():
    ci = ci_commands()
    assert ci["pip"] == [
        ["python", "-m", "pip", "install", "--upgrade", "pip"],
        ["pip", "install", "-r", "lambdas/requirements.txt"],
        ["pip", "install", "-r", "lambdas/requirements-dev.txt"],
        ["pip", "install", "-r", "scripts/requirements.txt"],
    ]
    assert ci["ruff"] == [["ruff", "check", "lambdas/", "scripts/"]]
    assert ci["pytest"] == [["pytest", "lambdas/", "scripts/"]]


# --- the package's review fixes ------------------------------------------------------------------


def _label_words(mmd: str) -> set[str]:
    """Every word of a Mermaid source's quoted labels, node and edge alike."""
    words = set()
    for label in re.findall(r'"([^"]*)"', mmd):
        words.update(re.sub(r"<br\s*/?>", " ", label).split())
    return words


def _svg_words(svg: str) -> set[str]:
    """Every word of an SVG's text elements, its wrapped rows joined as one line each."""
    words = set()
    for text in re.findall(r"<text\b[^>]*>(.*?)</text>", svg, re.S):
        rows = re.split(r'<tspan class="text-outer-tspan row"[^>]*>', text)
        words.update(html.unescape(" ".join(re.sub(r"<[^>]*>", "", row) for row in rows)).split())
    return words


@pytest.mark.parametrize("name", sorted(DIAGRAM_PAGES))
def test_every_word_of_a_diagram_is_whole_in_its_svg(name):
    """The SVG is the export of the source as it is now, and Mermaid wrapped no word mid-way
    ("(force_manual_revi" / "ew)"), which a zoom in the video would show."""
    source = (DIAGRAMS / f"{name}.mmd").read_text(encoding="utf-8")
    svg = (DIAGRAMS / f"{name}.svg").read_text(encoding="utf-8")
    missing = sorted(_label_words(source) - _svg_words(svg))
    assert not missing, f"{name}.svg lacks these words whole; export it again (diagrams/README.md): {missing}"


def test_the_svg_word_check_sees_a_word_split_across_rows():
    svg = (
        '<svg><text><tspan class="text-outer-tspan row" x="0">(force_manual_revi</tspan>'
        '<tspan class="text-outer-tspan row" x="0">ew)</tspan></text></svg>'
    )
    assert _label_words('DC["(force_manual_review)"]') - _svg_words(svg) == {"(force_manual_review)"}


def test_the_diagrams_look_again_cut_off_is_the_codes():
    """look_again is refused when its worker wait, the time left less the answer turn and the
    connect allowance, rounded down to a step, is under the rail minimum: that many seconds before
    the deadline, not the minimum itself."""
    rail = vision_triage.RAIL_MIN_LOOK_AGAIN_SECONDS
    cut_off = next(
        left for left in range(0, 120)
        if vision_triage.look_again_timeout(None, float(left), lambda: 0.0) >= rail
    )  # fmt: skip
    assert cut_off == 45
    label = f"(refused with under {cut_off} s to the deadline)"
    for page in (DIAGRAMS / "agent-workflow.mmd", REPORT, BLOGGER_VISION):
        assert label in page.read_text(encoding="utf-8"), page.name


def test_the_captions_say_where_each_failure_edge_ends():
    """The worker's or a source's failure ends at "Not measured", which keeps no decision; only the
    gates' and the agent's failures end at "No Finding"."""
    workflow = (DIAGRAMS / "agent-workflow.mmd").read_text(encoding="utf-8")
    assert re.search(r'P -- "the worker or a source failed" --> E\b', workflow)
    assert 'E["Not measured' in workflow
    for page in (REPORT, BLOGGER_VISION):
        text = " ".join(page.read_text(encoding="utf-8").split())
        assert "Every failure edge" not in text and "every failure edge" not in text, page.name
        assert '"Not measured"' in text, page.name


def _workflow_triggers(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` key as True
    if isinstance(on, str):
        return {on: None}
    if isinstance(on, list):
        return dict.fromkeys(on)
    return on or {}


def _runs_on_a_tag_push(triggers: dict) -> bool:
    if "create" in triggers:
        return True
    if "push" not in triggers:
        return False
    push = triggers["push"] or {}
    # A push filtered by branches alone never runs for a tag; one with no filter, or a tags filter, does.
    return "tags" in push or "tags-ignore" in push or not ({"branches", "branches-ignore"} & set(push))


# Where R2 is told the constraints file and the test counts come from.
FREEZE_SOURCES = (
    ROOT / "constraints-py311.txt",
    PYTHON_CI,
    HACKATHON / "dependencies.md",
    REPRODUCE,
    TRACKER,
)


def test_no_workflow_runs_on_a_tag_so_the_freeze_comes_from_r2s_pull_request():
    """No CI runs on the submission tag, and a commit cannot hold its own run's output, so the
    freeze and the test counts come from python-ci's run on R2's pull request, which runs it
    because it touches docs/hackathon/."""
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflows
    on_tag = [path.name for path in workflows if _runs_on_a_tag_push(_workflow_triggers(path))]
    assert not on_tag, f"a workflow runs on a tag push; the freeze's source may be the tag's run: {on_tag}"
    assert TRACKER_PATH_FILTER in python_ci_pull_request_paths()
    for path in FREEZE_SOURCES:
        text = " ".join(path.read_text(encoding="utf-8").replace("#", " ").split())
        assert not re.search(r"tag's CI|tagged commit's run|CI's run on the tag|CI freeze", text), path.name
        assert re.search(r"R2's (own )?pull request", text), path.name


@pytest.mark.parametrize(
    ("triggers", "on_tag"),
    [
        ({"push": {"branches": ["dev"], "paths": ["x/**"]}}, False),
        ({"push": {"tags": ["v*"]}}, True),
        ({"push": None}, True),
        ({"create": None}, True),
        ({"pull_request": None, "workflow_call": None}, False),
    ],
)
def test_the_tag_rule_reads_branch_and_tag_filters(triggers, on_tag):
    assert _runs_on_a_tag_push(triggers) is on_tag


def test_the_text_diagram_keeps_each_network_in_its_own_object():
    """blogger-vision.md's text diagram says what the Mermaid one does: the network is its own S3
    object, never the running state, and the day's city is chosen by the rotation."""
    text = BLOGGER_VISION.read_text(encoding="utf-8")
    start = text.index("**The same, in text:**")
    block = " ".join(_fenced_after(text[start:], "**The same, in text:**", "").split())
    assert "vision/<topic>/<site>/network.json" in block
    assert "cached in the running state" not in block and "sent inline" not in block
    assert "rotation" in block and "day's subject" in block


def test_the_reports_window_budget_is_the_codes():
    """The report's worst case at the rail cap says what the code's comment says: the read scales
    with the window, and a full read on top would not fit."""
    handler = (ROOT / "lambdas" / "vision_worker_handler.py").read_text(encoding="utf-8")
    assert "41-56 s in all" in handler and "would not fit" in handler
    report = " ".join(REPORT.read_text(encoding="utf-8").split())
    assert "41 to 56 s with the read" in report
    assert "a full 30 s read on top of the worst case would not fit" in report


def _passages(text: str) -> list[str]:
    """A page's paragraphs, each table row on its own."""
    passages = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip()]
        passages += lines if lines and all(line.startswith("|") for line in lines) else ["\n".join(lines)]
    return passages


def test_numbers_from_the_design_docs_are_marked_indicative():
    """The front page's rule: a number cites the evidence or a code anchor, or is marked indicative
    and cites the design doc that recorded it (vision.md §7, friction.md)."""
    report = REPORT.read_text(encoding="utf-8")
    design = re.compile(r"\]\((?:\.\./)*(?:friction\.md|enhancements/vision\.md#7-measured)")
    unmarked = [p[:80] for p in _passages(report) if design.search(p) and "indicative" not in p]
    assert not unmarked, f"cites a design doc for a number without saying indicative: {unmarked}"
    front = report[: report.index("## 1. The problem")]
    assert "is marked indicative and cites the design doc that recorded it" in front
