"""The Stats page's cost summary and the assistant's tile (frontend/app.js's renderStats).

app.js is a browser script with no exports, so, like test_frontend_nav.py, this runs the real file
under Node against a minimal stand-in DOM and a fake GET /stats, then reads back the tiles it drew.
The fake response is built by the API's own code (common/stats.py, common/stats_tracking.py), so
the page and the API are tested against each other, not against a hand-written copy of the shape.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from common import stats_tracking as st
from common.costing import USD_TO_AUD_RATE
from common.stats import build_stats

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "frontend" / "app.js"
APP_TEXT = APP_JS.read_text(encoding="utf-8")
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="needs Node.js")

_HARNESS = r"""
const fs = require("fs");
const stats = JSON.parse(fs.readFileSync(0, "utf8"));
process.on("unhandledRejection", () => {});
function makeEl(tag) {
  return {
    tagName: tag, children: [], attributes: {}, textContent: "", className: "", style: {}, hidden: false,
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    get firstChild() { return this.children[0] || null; },
    appendChild(child) { this.children.push(child); return child; },
    removeChild(child) { this.children.splice(this.children.indexOf(child), 1); return child; },
    setAttribute(k, v) { this.attributes[k] = String(v); },
    getAttribute(k) { return this.attributes[k] ?? null; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {}, focus() {}, scrollIntoView() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    getBoundingClientRect() { return { left: 0, top: 0, width: 0, height: 0 }; },
  };
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
  location: { hash: "#/stats" },
  localStorage: { getItem() { return null; }, setItem() {} },
  addEventListener(name, fn) { listeners[name] = fn; },
  scrollTo() {},
};
global.fetch = (url) => Promise.resolve({
  ok: true,
  json: () => Promise.resolve(String(url).endsWith("/stats") ? stats : { topics: [] }),
});
eval(fs.readFileSync(process.argv[1], "utf8"));
try { listeners.DOMContentLoaded(); } catch (e) { /* only the Stats view matters here */ }
setTimeout(() => {
  const content = byId["content"];
  const grids = [];
  const notes = [];
  const headings = [];
  (function walk(node) {
    if (node.className === "stats-tiles") {
      grids.push(node.children.map((tile) => ({
        className: tile.className,
        parts: tile.children.map((part) => [part.tagName, part.className, part.textContent]),
      })));
    }
    if (node.className === "stats-note") notes.push(node.textContent);
    if (/^h[1-3]$/.test(node.tagName || "")) headings.push(node.textContent);
    (node.children || []).forEach(walk);
  })(content);
  process.stdout.write(JSON.stringify({ grids, notes, headings }));
}, 50);
"""

BILL = {
    "Amazon Bedrock": Decimal("2.00"),
    "Claude Haiku 4.5 (Amazon Bedrock Edition)": Decimal("1.00"),
    "AWS WAF": Decimal("10.00"),
    "AWS Lambda": Decimal("4.00"),
}
TOTALS_ROW = {
    "week_start": "all-time",
    "assistant_calls": 10,
    "assistant_input_tokens": 9000,
    "assistant_output_tokens": 1000,
    "assistant_cost_aud": Decimal("0.30"),
    st.AWS_BILL_TOTAL_USD: BILL,
    st.AWS_BILL_TOTAL_SINCE: "2026-08-31",
    st.AWS_BILL_TOTAL_WEEKS: 5,
}
CURRENT_ROW = {
    "week_start": "2026-10-05",
    "assistant_calls": 2,
    "assistant_input_tokens": 1500,
    "assistant_output_tokens": 500,
    "assistant_cost_aud": Decimal("0.06"),
}


def _response(totals_row: dict, current_row: dict, *, overall: bool = True) -> dict:
    """What GET /stats answers (public_api_handler.py's _stats), built by the same functions."""
    stats = build_stats([], [], [])
    stats["weekly"] = st.public_view(current_row)
    stats["historic"] = {**st.public_view(totals_row), "note": st.HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE}
    if overall:
        stats["overall"] = st.overall_view(totals_row, current_row)
    return stats


def _render(stats: dict) -> dict:
    result = subprocess.run(
        [NODE, "-e", _HARNESS, str(APP_JS)],
        input=json.dumps(stats),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    page = json.loads(result.stdout)
    assert len(page["grids"]) == 3, "Total Stats, then the all-time section, then Weekly Stats"
    # Every tile is the existing markup: a .stat-tile of label, value and (optional) small print.
    for grid in page["grids"]:
        for tile in grid:
            assert tile["className"] == "stat-tile"
            assert [(tag, cls) for tag, cls, _ in tile["parts"]] == [
                ("span", "stat-label"),
                ("span", "stat-value"),
                ("span", "stat-sub"),
            ][: len(tile["parts"])]
    page["grids"] = [[[text for _, _, text in tile["parts"]] for tile in grid] for grid in page["grids"]]
    return page


def _aud(usd: str) -> str:
    return f"${float(Decimal(usd) * Decimal(str(USD_TO_AUD_RATE))):.2f}"


# --- Total Stats ----------------------------------------------------------------------------------


@needs_node
def test_total_stats_ends_with_the_assistant_the_bill_and_the_overall_total_last():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    total_stats = page["grids"][0]
    assert [tile[0] for tile in total_stats] == [
        "Estimated AI spend",
        "Articles drafted",
        "Average cost per article",
        "Tokens",
        "Research spend",
        "Operator assistant spend",
        "AI charges on the AWS bill",
        "Total infrastructure cost",
        "Total overall cost",
    ]
    assistant, ai, infrastructure, overall = total_stats[5:]
    # To date: every rolled-over week plus this one.
    assert assistant[1:] == ["$0.360", "AUD, all time, est., 12 model calls"]
    assert ai[1] == _aud("3.00") and infrastructure[1] == _aud("14.00") and overall[1] == _aud("17.00")


@needs_node
def test_every_bill_tile_says_its_currency_its_source_and_its_real_period():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    for label, _, sub in page["grids"][0][6:]:
        assert sub.startswith("AUD, "), label
        assert "AWS bill" in label + sub, label
        assert sub.endswith("complete weeks since 2026-08-31"), label
        assert "all time" not in sub, "the bill is not all time: it starts at its first complete week"


@needs_node
def test_the_note_under_the_tiles_is_the_apis_own_wording_of_the_formula():
    stats = _response(TOTALS_ROW, CURRENT_ROW)

    page = _render(stats)

    assert stats["overall"]["note"] in page["notes"]
    # ...and it comes before the per-day chart's heading, i.e. it sits under the Total Stats tiles.
    assert page["notes"].index(stats["overall"]["note"]) == 1  # after the cost-basis note


@needs_node
def test_with_no_bill_yet_the_page_says_no_data_and_never_offers_the_ai_estimate_as_the_total():
    totals_row = {key: value for key, value in TOTALS_ROW.items() if not key.startswith("aws_bill")}

    page = _render(_response(totals_row, CURRENT_ROW))

    tiles = {tile[0]: tile[1:] for tile in page["grids"][0]}
    assert tiles["Total infrastructure cost"][0] == "No data"
    assert tiles["Total overall cost"][0] == "No data"
    assert "AI charges on the AWS bill" not in tiles
    assert tiles["Operator assistant spend"][0] == "$0.360"


@needs_node
def test_an_assistant_that_never_ran_is_a_real_zero_not_unpriced():
    page = _render(_response({"week_start": "all-time"}, {}))

    for grid in page["grids"]:
        tile = next(tile for tile in grid if tile[0] == "Operator assistant spend")
        assert tile[1] == "$0" and tile[2].endswith("0 model calls")


@needs_node
def test_assistant_calls_with_no_price_are_shown_as_unpriced_never_as_free():
    current_row = {"week_start": "2026-10-05", "assistant_calls": 1, "assistant_unpriced_calls": 1}

    page = _render(_response({"week_start": "all-time"}, current_row))

    assert page["grids"][0][5][1:] == ["unpriced", "AUD, all time, est., 1 model call"]


@needs_node
def test_the_page_still_works_against_a_response_cached_from_before_overall_existed():
    """CloudFront and the browser keep GET /stats for five minutes (max-age=300), so a new page
    can meet an old answer: it must draw what it always drew, and nothing half-made."""
    page = _render(_response(TOTALS_ROW, CURRENT_ROW, overall=False))

    assert [tile[0] for tile in page["grids"][0]] == [
        "Estimated AI spend",
        "Articles drafted",
        "Average cost per article",
        "Tokens",
        "Research spend",
    ]
    assert page["headings"][:2] == ["Stats", "Total Stats"] and "Weekly Stats" in page["headings"]


# --- "Other AI spend and activity" and Weekly Stats ------------------------------------------------


@needs_node
def test_the_assistants_cost_is_the_last_tile_of_the_all_time_and_the_weekly_sections():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    _, all_time, weekly = page["grids"]
    # Each section's own period: all time without this week, and this week alone.
    assert all_time[-1] == ["Operator assistant spend", "$0.300", "est., 10 model calls"]
    assert weekly[-1] == ["Operator assistant spend", "$0.060", "est., 2 model calls"]


@needs_node
def test_a_response_with_no_assistant_category_has_no_assistant_tile():
    stats = _response(TOTALS_ROW, CURRENT_ROW)
    for section in ("historic", "weekly"):
        kept = [c for c in stats[section]["categories"] if c["category"] != "assistant"]
        stats[section]["categories"] = kept

    page = _render(stats)

    assert all(tile[0] != "Operator assistant spend" for grid in page["grids"][1:] for tile in grid)


# --- the code itself --------------------------------------------------------------------------------


def _new_code() -> str:
    """The three functions, without their comments (whole-line and trailing)."""
    start = APP_TEXT.index("function assistantTile(assistant, period)")
    code = APP_TEXT[start : APP_TEXT.index("function renderObservabilitySection(", start)]
    lines = [line for line in code.splitlines() if not line.lstrip().startswith("//")]
    return "\n".join(line.split(" // ")[0] for line in lines)


def test_the_new_tiles_are_plain_old_javascript_and_text_only():
    code = _new_code()

    assert "function appendAssistantTile(tiles, categories)" in code
    assert "function appendOverallTiles(tiles, overall)" in code
    assert not re.search(r"=>|\bconst\b|\blet\b|`", code)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|\.style\b", code)


def test_the_page_never_adds_money_up_itself():
    """The overall total is the API's sum (Decimal, one place, tested there). If the page added
    tiles together it could add a token estimate to the bill that already contains it."""
    code = _new_code()

    assert "formatAud(bill.total_aud)" in code
    assert not re.search(r"_aud\s*\+|\+\s*[\w.]+_aud\b", code)


def test_nothing_but_money_and_counts_is_read_from_the_summary():
    fields = set(re.findall(r"\b(?:overall|bill|assistant)\.(\w+)", _new_code()))

    assert fields <= {
        "assistant", "aws_bill", "note",
        "cost_aud", "calls", "unpriced",
        "ai_aud", "infrastructure_aud", "total_aud", "since",
    }  # fmt: skip
