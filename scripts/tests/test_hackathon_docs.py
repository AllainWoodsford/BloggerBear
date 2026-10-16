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
this file on a pull request that touches `lambdas/` or `scripts/`; on a docs-only one the owner
runs it by hand, until `python-ci.yml` lists `docs/hackathon/**` (the last two tests).

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
# the workflows (PyYAML is not a dependency).
_PULL_REQUEST_PATHS = re.compile(r"^on:\n  pull_request:\n    paths:\n((?:      - '[^']+'\n)+)", re.M)


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
    return re.findall(r"'([^']+)'", match[1])


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


@pytest.mark.xfail(
    strict=True,
    reason="python-ci.yml does not list docs/hackathon/**, so a pull request that changes only the tracker "
    "runs no pytest and the owner runs this file by hand. terraform.yml applies dev on a push that touches "
    "python-ci.yml, so the path goes in with the next PR that deploys; when it lands, drop this marker "
    "and the 'by hand' wording in the tracker's header and docs/PROGRESS.md.",
)
def test_a_change_to_the_tracker_alone_runs_this_file_in_the_python_ci():
    assert TRACKER_PATH_FILTER in python_ci_pull_request_paths()
