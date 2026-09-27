"""The header's topic nav (frontend/app.js's renderNav): at most NAV_TOPIC_LIMIT (3) topics, plus a
"More..." link to the home page -- whose Topics list is the full one -- once there are more than that.

app.js is a browser script with no exports, so this runs the real file under Node against a minimal
stand-in DOM and a fake GET /topics, then reads back what it put in #nav.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "frontend" / "app.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="needs Node.js")

# Just enough DOM for app.js's startup path (nav + home route). Elements are plain objects.
_HARNESS = r"""
const fs = require("fs");
const topics = JSON.parse(process.argv[2]);
function makeEl(tag) {
  const node = {
    tagName: tag, children: [], attributes: {}, textContent: "", className: "", style: {},
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
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] ?? null; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {}, focus() {}, querySelector() { return null; }, querySelectorAll() { return []; },
  };
  return node;
}
const byId = {};
const listeners = {};
global.document = {
  getElementById(id) { return (byId[id] = byId[id] || makeEl("div")); },
  createElement: makeEl,
  createElementNS(ns, tag) { return makeEl(tag); },
  createTextNode(text) { return { textContent: text, children: [] }; },
  querySelector() { return null; }, querySelectorAll() { return []; },
  addEventListener() {}, title: "",
};
global.window = {
  PUBLIC_API_URL: "https://api.example",
  location: { hash: "#/stats-not-routed" },
  localStorage: { getItem() { return null; }, setItem() {} },
  addEventListener(name, fn) { listeners[name] = fn; },
  scrollTo() {},
};
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ topics }) });
eval(fs.readFileSync(process.argv[1], "utf8"));
try { listeners.DOMContentLoaded(); } catch (e) { /* only the nav matters here */ }
setTimeout(() => {
  const nav = byId["nav"];
  process.stdout.write(JSON.stringify({
    links: nav.children.map(a => ({ text: a.textContent, href: a.attributes.href, className: a.className,
                                   label: a.attributes["aria-label"] || null })),
    visible: byId["nav-topics"].classList.contains("is-visible"),
  }));
}, 50);
"""


def _nav(topics: list[dict]) -> dict:
    result = subprocess.run(
        [NODE, "-e", _HARNESS, str(APP_JS), json.dumps(topics)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _topic(n: int, articles: int = 1) -> dict:
    return {
        "topic_id": f"t{n}",
        "name": f"Topic {n}",
        "article_count": articles,
        "researching": False,
        "latest_published_at": f"2026-09-{10 + n:02d}T00:00:00+00:00",
    }


def test_three_topics_fill_the_nav_with_no_more_link():
    nav = _nav([_topic(n) for n in range(3)])

    assert [link["href"] for link in nav["links"]] == ["#/topic/t2", "#/topic/t1", "#/topic/t0"]
    assert nav["visible"] is True


def test_a_fourth_topic_adds_a_more_link_to_the_home_page():
    nav = _nav([_topic(n) for n in range(4)])

    *topics, more = nav["links"]
    assert len(topics) == 3  # the nav itself stays capped
    assert more["text"] == "More…" and more["href"] == "#/"
    assert more["className"] == "nav-more"
    assert more["label"].startswith("More")  # the accessible name starts with the visible text


def test_topics_hidden_from_the_nav_still_count_towards_more():
    # Two topics with nothing published or being researched never show in the nav, but they are
    # still topics the home page lists, so the link appears.
    topics = [_topic(0), _topic(1), _topic(2, articles=0), _topic(3, articles=0)]

    nav = _nav(topics)

    assert [link["text"] for link in nav["links"]] == ["Topic 1", "Topic 0", "More…"]


def test_no_topics_means_no_nav_and_no_more_link():
    nav = _nav([])

    assert nav["links"] == [] and nav["visible"] is False
