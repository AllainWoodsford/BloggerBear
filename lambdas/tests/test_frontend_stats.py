"""The Stats page: three sections, each from one source (frontend/app.js's renderStats).

The owner's rule, after production showed "Total overall cost $18.45" above "Firewall spend
$20.41" (one was complete weeks, the other a rolling 30 days):

    Total Stats   only the history table's all-time row
    Weekly Stats  only the current table's row
    Articles      counted live from the articles, and said to be

app.js is a browser script with no exports, so, like test_frontend_nav.py, this runs the real file
under Node against a minimal stand-in DOM and a fake GET /stats, then reads back what it drew.
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
  // The page in reading order: every heading, tile grid, table and note, as one flat list.
  const flow = [];
  (function walk(node) {
    if (/^h[1-3]$/.test(node.tagName || "")) {
      flow.push({ kind: node.tagName, text: node.textContent });
    } else if (node.className === "stats-tiles") {
      flow.push({ kind: "tiles", tiles: node.children.map((tile) => ({
        className: tile.className,
        parts: tile.children.map((part) => [part.tagName, part.className, part.textContent]),
      })) });
      return;
    } else if (node.tagName === "table") {
      const rows = [];
      (function cells(inner) {
        if (inner.tagName === "tr") rows.push(inner.children.map((cell) => cell.textContent));
        (inner.children || []).forEach(cells);
      })(node);
      flow.push({ kind: "table", rows });
      return;
    } else if (node.className === "stats-note") {
      flow.push({ kind: "note", text: node.textContent });
    }
    (node.children || []).forEach(walk);
  })(content);
  process.stdout.write(JSON.stringify(flow));
}, 50);
"""

BILL = {
    "Amazon Bedrock": Decimal("2.00"),
    "Claude Haiku 4.5 (Amazon Bedrock Edition)": Decimal("1.00"),
    "AWS WAF": Decimal("10.00"),
    "AWS Lambda": Decimal("4.00"),
}
WEEK_BILL = {"Amazon Bedrock": Decimal("0.50"), "AWS WAF": Decimal("2.00"), "AWS Lambda": Decimal("1.00")}
TOTALS_ROW = {
    "week_start": "all-time",
    "assistant_calls": 10,
    "assistant_input_tokens": 9000,
    "assistant_output_tokens": 1000,
    "assistant_cost_aud": Decimal("0.30"),
    "articles_calls": 40,
    "articles_input_tokens": 80000,
    "articles_output_tokens": 20000,
    "articles_cost_aud": Decimal("2.00"),
    "feedback_given": 7,
    "loot_drops": 2,
    st.AWS_BILL_TOTAL_USD: BILL,
    st.AWS_BILL_TOTAL_SINCE: "2026-08-31",
    st.AWS_BILL_TOTAL_WEEKS: 5,
    # What a rollover carries onto the all-time row: the last week's rolling and calendar
    # readings. They are not all-time figures, and the page must show none of them.
    st.WAF_COST_AUD_30D: Decimal("20.41"),
    st.WAF_COST_AUD_WEEK_TO_DATE: Decimal("7.92"),
    st.WAF_COST_MONTH: "2026-10",
    st.WAF_COST_AUD_MONTH_TO_DATE: Decimal("3.93"),
    st.WAF_COST_PREVIOUS_MONTH: "2026-09",
    st.WAF_COST_AUD_PREVIOUS_MONTH: Decimal("16.49"),
    st.API_GATEWAY_COST_USD_30D: Decimal("0.01"),
    st.API_GATEWAY_COST_AUD_30D: Decimal("0.016"),
    st.AGENTCORE_COST_AUD_30D: Decimal("1.39"),
}
CURRENT_ROW = {
    "week_start": "2026-10-05",
    "assistant_calls": 2,
    "assistant_input_tokens": 1500,
    "assistant_output_tokens": 500,
    "assistant_cost_aud": Decimal("0.06"),
    "feedback_given": 1,
    st.AWS_BILL_WEEK_USD: WEEK_BILL,
    st.WAF_COST_AUD_30D: Decimal("26.08"),
    st.WAF_COST_AUD_WEEK_TO_DATE: Decimal("3.62"),
    st.WAF_COST_MONTH: "2026-10",
    st.WAF_COST_AUD_MONTH_TO_DATE: Decimal("9.59"),
    st.WAF_COST_PREVIOUS_MONTH: "2026-09",
    st.WAF_COST_AUD_PREVIOUS_MONTH: Decimal("16.49"),
    st.API_GATEWAY_COST_AUD_30D: Decimal("0.024"),
    st.AGENTCORE_COST_AUD_30D: Decimal("2.18"),
}
SECTIONS = ("Total Stats", "Weekly Stats", "Articles")


def _response(totals_row: dict, current_row: dict) -> dict:
    """What GET /stats answers (public_api_handler.py's _stats), built by the same functions."""
    stats = build_stats([], [], [])
    stats["weekly"] = st.public_view(current_row)
    stats["historic"] = {**st.public_view(totals_row), "note": st.HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE}
    return stats


def _render(stats: dict) -> dict:
    """The page as {section heading: [items in reading order]}, plus "flow" for the whole of it.
    A tile is [label, value, small print]; a table is its rows of cell text."""
    result = subprocess.run(
        [NODE, "-e", _HARNESS, str(APP_JS)],
        input=json.dumps(stats),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    flow = json.loads(result.stdout)
    for item in flow:
        if item["kind"] != "tiles":
            continue
        # Every tile is the existing markup: a .stat-tile of label, value and (optional) small print.
        for tile in item["tiles"]:
            assert tile["className"] == "stat-tile"
            assert [(tag, cls) for tag, cls, _ in tile["parts"]] == [
                ("span", "stat-label"),
                ("span", "stat-value"),
                ("span", "stat-sub"),
            ][: len(tile["parts"])]
        item["tiles"] = [[text for _, _, text in tile["parts"]] for tile in item["tiles"]]
    page: dict = {"flow": flow}
    section = None
    for item in flow:
        if item["kind"] == "h2":
            section = item["text"]
            page[section] = []
        elif section is not None:
            page[section].append(item)
    return page


def _tiles(items: list[dict], under: str) -> list[list[str]]:
    """The tile grid that follows the h3 `under` in a section."""
    at = next(i for i, item in enumerate(items) if item["kind"] == "h3" and item["text"] == under)
    return next(item["tiles"] for item in items[at + 1 :] if item["kind"] == "tiles")


def _table(items: list[dict], under: str) -> list[list[str]]:
    at = next(i for i, item in enumerate(items) if item["kind"] == "h3" and item["text"] == under)
    return next(item["rows"] for item in items[at + 1 :] if item["kind"] == "table")


def _all_text(items: list[dict]) -> str:
    return json.dumps(items)


def _aud(usd: str) -> str:
    return f"${float(Decimal(usd) * Decimal(str(USD_TO_AUD_RATE))):.2f}"


# --- the shape of the page ------------------------------------------------------------------------


@needs_node
def test_the_page_is_three_sections_then_gear():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    headings = [item["text"] for item in page["flow"] if item["kind"] == "h2"]
    assert headings[:3] == list(SECTIONS)
    # Total Stats and Weekly Stats are the same three parts, in the same order: money first.
    for section in ("Total Stats", "Weekly Stats"):
        parts = [item["text"] for item in page[section] if item["kind"] == "h3"]
        assert parts == ["AWS bill", "AI spend by token estimate", "Activity"], section
    # The per-article detail is the Articles section's, not Total Stats'.
    parts = [item["text"] for item in page["Articles"] if item["kind"] == "h3"]
    assert parts[0].startswith("Estimated spend per day") and parts[1:3] == ["By model", "By topic"]


@needs_node
def test_every_section_says_where_its_numbers_come_from():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    total, weekly, articles = (page[section][0]["text"] for section in SECTIONS)
    assert total.startswith("Every completed week, added up.")
    assert st.HISTORIC_EXCLUDES_CURRENT_WEEK_NOTE in total
    assert weekly.startswith("The week of Monday 5 October 2026, so far.")
    assert articles.startswith("Counted from the articles themselves")
    assert "will not match either exactly" in articles


# --- one period per section -----------------------------------------------------------------------


@needs_node
def test_the_bill_is_four_tiles_that_add_up_all_for_the_same_period():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    total, ai, firewall, hosting = _tiles(page["Total Stats"], "AWS bill")
    assert [tile[0] for tile in (total, ai, firewall, hosting)] == [
        "Total cost",
        "AI (Bedrock)",
        "Firewall (WAF)",
        "Hosting, data and monitoring",
    ]
    assert (ai[1], firewall[1], hosting[1]) == (_aud("3.00"), _aud("10.00"), _aud("4.00"))
    assert total[1] == _aud("17.00")
    period = "5 complete weeks since 31 August 2026"
    assert total[2] == f"AUD, whole AWS bill, {period}"
    for part in (ai, firewall, hosting):
        assert part[2] == f"AUD, part of the total, {period}"

    week = _tiles(page["Weekly Stats"], "AWS bill")
    # Under a dollar the page shows three decimals, as it does everywhere.
    assert [tile[1] for tile in week] == [_aud("3.50"), "$0.750", _aud("2.00"), _aud("1.00")]
    assert all(tile[2].endswith("this week so far") for tile in week)


@needs_node
def test_the_firewall_has_one_figure_per_section_and_no_rolling_or_monthly_ones():
    """Production showed the firewall four ways in Weekly Stats and three in Total Stats: a
    rolling 30 days, this week, this month and last month. The rows still hold them all."""
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    for section in ("Total Stats", "Weekly Stats"):
        text = _all_text(page[section])
        labels = [tile[0] for item in page[section] if item["kind"] == "tiles" for tile in item["tiles"]]
        assert [label for label in labels if "irewall" in label] == ["Firewall (WAF)"], section
        for gone in ("last 30 days", "30 days", "September", "whole month", "API Gateway spend", "(actual)"):
            assert gone not in text, (section, gone)
        # None of the stored rolling or monthly amounts reaches the page.
        for amount in ("$20.41", "$26.08", "$16.49", "$3.93", "$9.59", "$7.92", "$1.39", "$2.18"):
            assert amount not in text, (section, amount)


@needs_node
def test_nothing_in_total_stats_includes_the_current_week_and_the_reverse():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    total = dict((row[0], row) for row in _table(page["Total Stats"], "AI spend by token estimate"))
    weekly = dict((row[0], row) for row in _table(page["Weekly Stats"], "AI spend by token estimate"))
    # The assistant: ten calls in the weeks that have ended, two this week. Never twelve.
    assert total["Operator assistant"][1:] == ["10", "9,000", "1,000", "$0.300"]
    assert weekly["Operator assistant"][1:] == ["2", "1,500", "500", "$0.060"]
    assert "12" not in (total["Operator assistant"][1], weekly["Operator assistant"][1])
    activity = {tile[0]: tile[1] for tile in _tiles(page["Total Stats"], "Activity")}
    assert activity["Feedback given"] == "7"
    activity = {tile[0]: tile[1] for tile in _tiles(page["Weekly Stats"], "Activity")}
    assert activity["Feedback given"] == "1"


@needs_node
def test_the_operator_assistant_is_one_row_per_section_and_never_a_tile():
    """It used to be a tile in Total Stats, a tile in each section and a row in each table."""
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    tiles = [tile[0] for item in page["flow"] if item["kind"] == "tiles" for tile in item["tiles"]]
    assert not [label for label in tiles if "ssistant" in label]
    for section in ("Total Stats", "Weekly Stats"):
        rows = [row[0] for row in _table(page[section], "AI spend by token estimate")]
        assert rows.count("Operator assistant") == 1


# --- the token estimates ----------------------------------------------------------------------------


@needs_node
def test_the_estimate_table_ends_with_the_apis_total():
    stats = _response(TOTALS_ROW, CURRENT_ROW)

    page = _render(stats)

    rows = _table(page["Total Stats"], "AI spend by token estimate")
    assert rows[0] == ["Category", "Calls", "Tokens in", "Tokens out", "Est. cost (AUD)"]
    assert rows[-1] == ["Total", "50", "89,000", "21,000", "$2.30"]
    assert stats["historic"]["ai_estimate"]["cost_aud"] == 2.30
    # ...and the note under it says why it is not added to the bill above.
    notes = [item["text"] for item in page["Total Stats"] if item["kind"] == "note"]
    assert any("already inside the bill's AI figure above" in note for note in notes)


@needs_node
def test_an_assistant_that_never_ran_is_a_real_zero_and_unpriced_calls_are_never_free():
    never = _render(_response({"week_start": "all-time"}, {}))
    rows = dict((row[0], row) for row in _table(never["Weekly Stats"], "AI spend by token estimate"))
    assert rows["Total"][1:] == ["0", "0", "0", "$0"]
    # A category with no calls is a $0 too: it used to read "unpriced", as if a price were missing.
    assert all(row[4] == "$0" for name, row in rows.items() if name != "Category")

    current_row = {"week_start": "2026-10-05", "assistant_calls": 1, "assistant_unpriced_calls": 1}
    unpriced = _render(_response({"week_start": "all-time"}, current_row))
    rows = dict((row[0], row) for row in _table(unpriced["Weekly Stats"], "AI spend by token estimate"))
    assert rows["Operator assistant"][4] == "unpriced" and rows["Total"][4] == "unpriced"


# --- missing data, and an older answer -----------------------------------------------------------


@needs_node
def test_with_no_bill_yet_each_section_says_no_data_and_offers_no_estimate_in_its_place():
    totals_row = {key: value for key, value in TOTALS_ROW.items() if not key.startswith("aws_bill")}
    current_row = {key: value for key, value in CURRENT_ROW.items() if not key.startswith("aws_bill")}

    page = _render(_response(totals_row, current_row))

    assert _tiles(page["Total Stats"], "AWS bill") == [
        ["Total cost", "No data", "no complete week of the AWS bill yet"]
    ]
    assert _tiles(page["Weekly Stats"], "AWS bill") == [
        ["Total cost", "No data", "the bill has not been read yet this week"]
    ]


@needs_node
def test_the_page_still_works_against_an_answer_cached_from_before_the_new_fields():
    """CloudFront and the browser keep GET /stats for five minutes (max-age=300), so a new page
    can meet an old answer: no ai_estimate, no week_start, no weeks on the bill, and an `overall`
    block it no longer reads. It draws what it can and makes nothing up."""
    stats = _response(TOTALS_ROW, CURRENT_ROW)
    for section in ("historic", "weekly"):
        del stats[section]["ai_estimate"], stats[section]["week_start"]
        del stats[section]["aws_bill"]["weeks"], stats[section]["aws_bill"]["scope"]
    # As the API used to send them (it sends none of these now):
    stats["overall"] = {
        "assistant": {"calls": 12, "cost_aud": 0.36, "unpriced": 0},
        "aws_bill": {"ai_aud": 4.5, "infrastructure_aud": 94.5, "total_aud": 99.0, "since": "2026-08-31"},
        "note": "Total overall cost is the whole AWS bill: ...",
    }
    stats["historic"]["waf"] = {"cost_aud_30d": 20.41, "month": "2026-10", "cost_aud_month_to_date": 3.93}
    stats["historic"]["api_gateway_cost_aud_30d"] = 0.016
    stats["weekly"]["web_search"]["agentcore_actual_cost_aud_30d"] = 2.18

    page = _render(stats)

    rows = [row[0] for row in _table(page["Total Stats"], "AI spend by token estimate")]
    assert "Total" not in rows and "Operator assistant" in rows
    assert _tiles(page["Total Stats"], "AWS bill")[0][2] == (
        "AUD, whole AWS bill, complete weeks since 31 August 2026"
    )
    assert page["Weekly Stats"][0]["text"].startswith("This week so far.")
    text = _all_text(page["flow"])
    for gone in ("Total overall cost", "$20.41", "$3.93", "$0.016", "$2.18", "$99.00", "$94.50", "$0.360"):
        assert gone not in text, gone


@needs_node
def test_the_articles_section_keeps_the_per_article_tiles_and_says_they_are_estimates():
    page = _render(_response(TOTALS_ROW, CURRENT_ROW))

    tiles = next(item["tiles"] for item in page["Articles"] if item["kind"] == "tiles")
    assert [tile[0] for tile in tiles] == [
        "AI spend on articles",
        "Articles drafted",
        "Average cost per article",
        "Tokens",
        "Research spend",
    ]
    assert tiles[0][2].startswith("AUD, est., every article so far")
    assert "all time" not in _all_text(page["Articles"])


# --- the code itself --------------------------------------------------------------------------------


def _section_code() -> str:
    """The functions that draw a section, without their comments (whole-line and trailing)."""
    start = APP_TEXT.index("var OBSERVABILITY_CATEGORY_LABELS = {")
    code = APP_TEXT[start : APP_TEXT.index("// Quick Links: jumps to", start)]
    lines = [line for line in code.splitlines() if not line.lstrip().startswith("//")]
    return "\n".join(line.split(" // ")[0] for line in lines)


def test_the_section_is_plain_old_javascript_and_text_only():
    code = _section_code()

    assert "function renderStatsSection(data, isCurrentWeek)" in code
    assert not re.search(r"=>|\bconst\b|\blet\b|`", code)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|\.style\b", code)


def test_the_page_never_adds_money_up_itself():
    """Each total is the API's sum (Decimal, one place, tested there). If the page added figures
    together it could add a token estimate to the bill that already contains it."""
    code = _section_code()

    assert "formatAud(bill.total_aud)" in code and "estimateCost(total)" in code
    assert not re.search(r"_aud\s*\+|\+\s*[\w.]+_aud\b|\.reduce\(", code)


def test_a_section_reads_one_row_and_none_of_the_rolling_or_monthly_readings():
    code = _section_code()
    render = APP_TEXT[APP_TEXT.index("function renderStats(stats)") : APP_TEXT.index("function loadStats()")]

    # Each section is handed its own row and nothing else.
    assert "renderStatsSection(historic, false)" in render and "renderStatsSection(weekly, true)" in render
    assert "stats.overall" not in APP_TEXT
    for gone in ("cost_aud_30d", "month_to_date", "previous_month", "week_to_date", "data.waf"):
        assert gone not in code, gone
