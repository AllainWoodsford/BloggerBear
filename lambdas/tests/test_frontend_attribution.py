"""The source credit in the single-page frontend (frontend/app.js's sourceAttribution): one
italic line under an open article's title block and under a topic's title, built from the API's
`attribution` (lambdas/common/attribution.py).

app.js is a browser script with no exports, so this runs the real file under Node against a
minimal stand-in DOM and a fake public API, opens a topic page or an article, and reads back what
it put in #content. The stand-in DOM has no HTML parser at all: a value can only ever arrive as a
text node or an attribute, which is the point -- a credit that tried to be markup shows up here as
text.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
APP_JS = FRONTEND / "app.js"
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="needs Node.js")

COINGECKO = {
    "text": "Powered by CoinGecko API",
    "label": "CoinGecko API",
    "url": "https://www.coingecko.com/en/api",
}
GDELT = {
    "text": "News search by the GDELT Project",
    "label": "GDELT Project",
    "url": "https://www.gdeltproject.org/",
}
GITHUB = {
    "text": "Data sourced from GitHub Trending",
    "label": "GitHub Trending",
    "url": "https://github.com/trending",
}

# Just enough DOM for app.js to route to a page and render it. Elements are plain objects; setting
# innerHTML throws, so the test fails loudly if the page ever builds a credit from markup.
_HARNESS = r"""
const fs = require("fs");
const api = JSON.parse(process.argv[2]);
function makeEl(tag) {
  const node = {
    tagName: tag, children: [], attributes: {}, textContent: "", className: "", style: {}, dataset: {},
    classList: {
      set: new Set(),
      add(c) { this.set.add(c); }, remove(c) { this.set.delete(c); },
      toggle(c, on) {
        if (on === undefined ? !this.set.has(c) : on) this.set.add(c); else this.set.delete(c);
      },
      contains(c) { return this.set.has(c); },
    },
    get firstChild() { return this.children[0] || null; },
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; },
    insertBefore(child) { this.children.unshift(child); return child; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] ?? null; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {}, focus() {}, querySelector() { return null; }, querySelectorAll() { return []; },
    set innerHTML(v) { throw new Error("innerHTML was set"); },
  };
  return node;
}
const byId = {};
const listeners = {};
global.document = {
  getElementById(id) { return (byId[id] = byId[id] || makeEl("div")); },
  createElement: makeEl,
  createElementNS(ns, tag) { return makeEl(tag); },
  createTextNode(text) { return { text: String(text), children: [] }; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, title: "", body: makeEl("body"),
};
global.window = {
  PUBLIC_API_URL: "https://api.example",
  location: { hash: api.hash },
  localStorage: { getItem() { return null; }, setItem() {} },
  addEventListener(name, fn) { listeners[name] = fn; },
  scrollTo() {},
  BloggerMarkdown: { parse() { return []; } },
};
global.fetch = (url) => {
  const path = String(url).replace("https://api.example", "");
  const found = Object.prototype.hasOwnProperty.call(api.responses, path);
  return Promise.resolve({ ok: found, status: found ? 200 : 404,
                           json: () => Promise.resolve(api.responses[path]) });
};
eval(fs.readFileSync(process.argv[1], "utf8"));
try { listeners.DOMContentLoaded(); } catch (e) { /* only #content matters here */ }
function dump(node) {
  if (node.text !== undefined) return { text: node.text };
  return { tag: node.tagName, className: node.className, attributes: node.attributes,
           textContent: node.textContent, children: node.children.map(dump) };
}
setTimeout(() => {
  process.stdout.write(JSON.stringify(byId["content"].children.map(dump)));
}, 80);
"""


def _content(hash_: str, responses: dict) -> list[dict]:
    """#content's children after app.js has routed to `hash_` against a fake API."""
    result = subprocess.run(
        [NODE, "-e", _HARNESS, str(APP_JS), json.dumps({"hash": hash_, "responses": responses})],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _topic_page(attribution) -> list[dict]:
    payload = {"topic_id": "crypto", "articles": []}
    if attribution is not None:
        payload["attribution"] = attribution
    return _content(
        "#/topic/crypto",
        {"/topics": {"topics": []}, "/articles?topic_id=crypto": payload, "/topics/crypto/activity": {}},
    )


def _article_page(attribution) -> list[dict]:
    article = {
        "article_id": "a1",
        "title": "A Title",
        "body": "Body.",
        "published_at": "2026-09-20T00:00:00+00:00",
        "source_refs": [],
        "view_count": 1,
        "lineage": None,
        "published_by": None,
        "fact_check": None,
        "equipment_used": [],
        "attribution": attribution,
    }
    return _content("#/article/a1", {"/topics": {"topics": []}, "/articles/a1": article})


def _credit_line(children: list[dict]) -> dict | None:
    lines = [child for child in children if child.get("className") == "source-attribution"]
    assert len(lines) <= 1
    return lines[0] if lines else None


def _flat(node: dict) -> str:
    """A node's text as a reader sees it: its text nodes and its elements' textContent, in order."""
    if "text" in node:
        return node["text"]
    return node["textContent"] + "".join(_flat(child) for child in node["children"])


def _links(node: dict) -> list[dict]:
    found = [node] if node.get("tag") == "a" else []
    for child in node.get("children", []):
        found.extend(_links(child))
    return found


@needs_node
def test_a_topic_page_shows_its_credit_in_italics_right_under_the_title():
    children = _topic_page([COINGECKO, GDELT])

    assert children[0]["tag"] == "h1"
    line = children[1]
    assert line["tag"] == "p" and line["className"] == "source-attribution"
    assert _flat(line) == "Powered by CoinGecko API · News search by the GDELT Project"
    # Each credit is its own <em>, so it is italic with or without the stylesheet.
    assert [child["tag"] for child in line["children"] if "tag" in child] == ["em", "em"]

    links = _links(line)
    assert [(link["textContent"], link["attributes"]["href"]) for link in links] == [
        ("CoinGecko API", "https://www.coingecko.com/en/api"),
        ("GDELT Project", "https://www.gdeltproject.org/"),
    ]
    for link in links:
        assert link["attributes"]["rel"] == "noopener noreferrer"
        assert link["attributes"]["target"] == "_blank"


@needs_node
def test_an_open_article_shows_its_credit_under_the_title_block_and_above_the_body():
    children = _article_page([GITHUB])

    classes = [child.get("className") or child.get("tag") for child in children]
    at = classes.index("source-attribution")
    assert classes[:at] == ["h1", "article-meta", "lineage-summary"]
    assert classes[at + 1] == "article-body"

    line = children[at]
    assert _flat(line) == "Data sourced from GitHub Trending"
    (link,) = _links(line)
    assert link["textContent"] == "GitHub Trending"
    assert link["attributes"]["href"] == "https://github.com/trending"
    assert link["attributes"]["rel"] == "noopener noreferrer"


@needs_node
@pytest.mark.parametrize("attribution", [None, []])
def test_nothing_to_credit_means_no_line_at_all(attribution):
    assert _credit_line(_topic_page(attribution)) is None
    assert _credit_line(_article_page(attribution)) is None


@needs_node
def test_markup_in_a_credit_is_shown_as_text_never_parsed():
    hostile = {
        "text": 'Data by <img src=x onerror=alert(1)> "Evil" & Co',
        "label": '<img src=x onerror=alert(1)>',
        "url": "https://evil.example/?a=1&b=\"><script>alert(2)</script>",
    }
    line = _credit_line(_topic_page([hostile]))

    # The whole sentence comes back as text, character for character, and the only elements are
    # the ones the page itself creates.
    assert _flat(line) == hostile["text"]
    (credit,) = line["children"]
    assert credit["tag"] == "em"
    assert [child.get("tag") for child in credit["children"]] == [None, "a", None]
    (link,) = _links(line)
    assert link["textContent"] == hostile["label"]
    assert link["attributes"]["href"] == hostile["url"]  # an attribute value, set with setAttribute


@needs_node
@pytest.mark.parametrize(
    "source",
    [
        {"text": "Click here", "label": "here", "url": "javascript:alert(1)"},
        {"text": "Plain http", "label": "http", "url": "http://x.example/"},
        {"text": "Data URL", "label": "Data", "url": "data:text/html,<script>alert(1)</script>"},
        {"text": "Label is not in the text", "label": "elsewhere", "url": "https://x.example/"},
        {"text": "Empty label", "label": "", "url": "https://x.example/"},
        {"text": "No url", "label": "No"},
        {"text": 5, "label": "5", "url": "https://x.example/"},
        "not an object",
        None,
    ],
)
def test_a_credit_that_is_not_a_plain_https_link_is_left_out(source):
    assert _credit_line(_topic_page([source])) is None
    # ...and it does not take a good one down with it.
    line = _credit_line(_topic_page([source, GITHUB]))
    assert _flat(line) == "Data sourced from GitHub Trending"


# --- the source itself ----------------------------------------------------------------------------


def _section() -> str:
    """The "Source attribution" part of app.js: its helper, up to the next function."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("// --- Source attribution")
    return source[start : source.index("function renderArticleList", start)]


def test_the_credit_is_built_from_nodes_and_text_never_from_markup():
    section = _section()
    assert "function sourceAttribution(" in section
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", section)
    assert "createTextNode" in section
    assert 'rel: "noopener noreferrer"' in section
    assert 'indexOf("https://") !== 0' in section


def test_both_pages_use_the_one_helper_with_the_apis_attribution():
    source = APP_JS.read_text(encoding="utf-8")
    assert "sourceAttribution(article.attribution)" in source
    assert "sourceAttribution(attribution)" in source
    assert source.count("data.attribution") == 2  # with and without the activity call succeeding


def test_the_credit_line_is_italic_and_no_smaller_than_coingeckos_minimum():
    """CoinGecko's API terms: "in a legible font ... no smaller than font size 10". The rule's
    size is in rem, so at the browser default (16px = 12pt per rem) it must come to 10pt or more."""
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    rule = re.search(r"^\.source-attribution \{(.*?)\}", css, re.S | re.M).group(1)

    assert "font-style: italic" in rule
    size = re.search(r"font-size:\s*([\d.]+)rem", rule)
    assert size, "the credit line's size must be set in rem so this check can read it"
    assert float(size.group(1)) * 12 >= 10
    # Nothing later in the stylesheet shrinks it again.
    assert len(re.findall(r"\.source-attribution[^{]*\{[^}]*font-size", css)) == 1
