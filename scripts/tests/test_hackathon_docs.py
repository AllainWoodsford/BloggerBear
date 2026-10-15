"""The submission round's rubric tracker (docs/hackathon/rubric-tracker.md) keeps the format its
readers rely on: one Markdown table with nine named columns; 25 rows, one per rubric line of the
OpenCV AI Competition 2026, one for the owner's own ask and one per owner step; a verdict word and
needs from closed lists; evidence that cites repository paths in backticks and links only to the
repository or the competition's pages; a "Say" phrase from the closed list below; an ISO date; and
no cell long enough, or worded so, that it could steer the assistant that may one day read it.

Why a test: every submission-round PR updates the rows it closes, the owner updates the owner rows
by hand, and the ops assistant's `submission_status` (if it is built) ships a copy of this table
inside the MCP Lambda, where `redact.looks_like_instructions` is the rule for text a model reads.
A malformed row fails CI here, not a judge's or the assistant's reading of it.

The parser is deliberately plain: a cell holds no `|`, so a row is split on it. The "Say" list is
`SAY_PHRASES` until `scripts/submission_index.py` exists; the test then imports it from there.
Nothing here touches the network.
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import pytest

# ops_mcp imports common.*: scripts/pyproject.toml puts only scripts/ on the path when a scripts
# test runs on its own, so the test adds lambdas/ the way scripts/vision_benchmark.py does.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas"))

from ops_mcp import redact, suggestions

ROOT = Path(__file__).resolve().parents[2]
HACKATHON = ROOT / "docs" / "hackathon"
TRACKER = HACKATHON / "rubric-tracker.md"

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
# Where a cell may link: the repository's owner on GitHub, and the competition's pages.
GITHUB_PREFIX = "https://github.com/AllainWoodsford/"
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
# A repo-relative path in backticks: path characters only, with a directory or an extension, so a
# backticked flag or variable name (`VISION_ENABLED`, `--pipeline daily_cycle`) is not one.
_PATH_LIKE = re.compile(r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?$")
_URL = re.compile(r"https?://[^\s<>()`\"']+")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")
_MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _cells(line: str) -> list[str]:
    stripped = line.strip()
    assert stripped.startswith("|") and stripped.endswith("|"), f"not a table row: {line!r}"
    return [cell.strip() for cell in stripped[1:-1].split("|")]


def parse_table(text: str) -> list[dict[str, str]]:
    """The tracker's one table as a list of rows keyed by column. Asserts the shape of 0.4: one
    contiguous table, the nine columns in order, a separator row, and nine cells in every row."""
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
    """The ids on the header's `Demo topics:` line, as the readers that report them apart parse it."""
    lines = [line for line in text.splitlines() if line.startswith(DEMO_TOPICS_PREFIX)]
    assert len(lines) == 1, f"expected one line starting {DEMO_TOPICS_PREFIX!r}, found {len(lines)}"
    return _BACKTICKED.findall(lines[0])


def backticked_paths(cell: str) -> list[str]:
    tokens = _BACKTICKED.findall(cell)
    return [token for token in tokens if _PATH_LIKE.match(token) and ("/" in token or "." in token)]


def _allowed_url(url: str) -> bool:
    if url.startswith(GITHUB_PREFIX):
        return True
    parts = urlsplit(url)
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in COMPETITION_HOSTS)


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
        if not (ROOT / path).exists()
    ]
    assert not missing, f"a met row cites a path that is not in the repository: {missing}"


def test_urls_point_only_at_the_repository_or_the_competition(rows):
    bad = [
        (row_id, column, url)
        for row_id, column, cell in _each_cell(rows)
        for url in _URL.findall(cell)
        if not _allowed_url(url.rstrip(".,;:"))
    ]
    assert not bad, f"a cell links outside {GITHUB_PREFIX} and {COMPETITION_HOSTS}: {bad}"


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
    assert ids, f"the {DEMO_TOPICS_PREFIX!r} line names no topic in backticks"
    bad = [i for i in ids if not suggestions.ID_PATTERN.match(i)]
    assert not bad, f"demo topic ids that are not topic ids: {bad}"


def test_no_cell_reads_like_instructions_to_the_assistant(rows):
    flagged = [
        (row_id, column) for row_id, column, cell in _each_cell(rows) if redact.looks_like_instructions(cell)
    ]
    assert not flagged, f"cells the assistant's redaction would withhold: {flagged}"


def test_every_relative_link_under_docs_hackathon_resolves():
    """The tracker names a planned document as a backticked path, not a link, until it exists: a
    link to a missing file is dead on the page, and fails here."""
    dead = []
    for page in sorted(HACKATHON.rglob("*.md")):
        for target in _MARKDOWN_LINK.findall(page.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            path = target.split("#", 1)[0]
            if path and not (page.parent / path).exists():
                dead.append((str(page.relative_to(ROOT)), target))
    assert not dead, f"links to files that do not exist: {dead}"
