"""The pre-submit check (scripts/submission_check.py): each of its five checks on a small checkout
built in a temporary directory, a real git repository for the run files' code check, and its exit
codes. Nothing here touches the network."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import submission_check as sc

ROOT = Path(__file__).resolve().parents[2]
# Joined, so the file holds no address the secret scanners would read as one.
TEST_EMAIL = "@".join(("submission-check", "example.invalid"))

TRACKER = """# Tracker

| ID | Line | Status | Needs | Evidence | Gap | Closed by | Say | Checked |
|---|---|---|---|---|---|---|---|---|
| S1 | The report | partly | docs | x | y | R | the technical report | 2026-10-16 |
| S5 | The endpoint | missing | owner | x | y | O4 | the live article | 2026-10-16 |
| O7 | The form | missing | owner | x | y | O7 | the Devpost form | 2026-10-16 |
| O10 | Production | missing | owner | x | y | O10 | the production release | 2026-10-16 |
"""


# --- placeholders --------------------------------------------------------------------------------


def test_a_marked_placeholder_is_found_with_its_line():
    text = "# Report\n\nThe team. [R2: the team, from the owner]\n"
    assert sc.placeholders(text) == [(3, "[R2: the team, from the owner]")]


def test_a_placeholder_that_wraps_is_one_finding_on_its_first_line():
    text = "Intro.\n\nE3: [R2: the false-alarm rate\nwith its Wilson interval] and more.\n"
    assert sc.placeholders(text) == [(3, "[R2: the false-alarm rate with its Wilson interval]")]


def test_an_old_style_bracketed_value_is_a_placeholder_too():
    assert sc.placeholders("| Coverage | [0.xx] | [s] |\n") == [(1, "[0.xx]"), (1, "[s]")]


@pytest.mark.parametrize(
    "text",
    [
        "See [the report](technical-report.md) and [the runsheet](../runsheet.md#a-heading).",
        'A [titled link](x.md "Title") and an image ![alt](x.png).',
        "A [reference link][ref].\n\n[ref]: x.md\n",
        "A link whose text holds code: [`2026-10-16-run.json`](runs/2026-10-16-run.json).",
        "- [ ] a task\n- [x] a done task\n- [X] another\n",
        "| [ ] Title | its source |\n",
        "A footnote[^1].\n\n[^1]: The note.\n",
        "Inline code: `node[\"label\"]` and `[timeout:25]`.",
        "```mermaid\nflowchart LR\n  a[\"A\"] --> b[\"B\"]\n```\n",
        "    ```bash\n    echo [not a placeholder]\n    ```\n",
    ],
)
def test_links_code_task_boxes_and_footnotes_are_not_placeholders(text):
    assert sc.placeholders(text) == []


def test_a_link_inside_a_placeholder_leaves_the_placeholder():
    text = "[R2: from [the export](evidence/README.md), the counts]\n"
    assert sc.placeholders(text) == [(1, "[R2: from [the export](evidence/README.md), the counts]")]


def test_the_placeholder_is_quoted_with_its_inline_code():
    assert sc.placeholders("[R2: the tag `opencv-2026-submission`]") == [
        (1, "[R2: the tag `opencv-2026-submission`]")
    ]


def test_an_unmatched_bracket_is_reported():
    assert sc.placeholders("A closing ] first.\n\nThen a stray [ with nothing after.\n") == [
        (1, "an unmatched ']'"),
        (3, "an unmatched '['"),
    ]


def test_brackets_far_apart_still_count_as_one_placeholder():
    # A placeholder may wrap over lines, so a stray pair across paragraphs is reported once, whole.
    assert sc.placeholders("A stray [ here.\n\nAnd ] there.\n") == [(1, "[ here. And ]")]


def test_prose_keeps_every_position_and_line():
    text = "a `b[c]` d\n```\n[x]\n```\ne"
    blanked = sc.prose(text)
    assert len(blanked) == len(text) and blanked.count("\n") == text.count("\n")
    assert "[" not in blanked


def test_check_placeholders_names_the_file_and_line_and_a_missing_doc(tmp_path):
    docs = (Path("a.md"), Path("b.md"), Path("gone.md"))
    (tmp_path / "a.md").write_text("Fine.\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("\n[R2: a number]\n", encoding="utf-8")
    result = sc.check_placeholders(tmp_path, docs)
    assert result.failures == ["b.md:2: [R2: a number]", "gone.md: missing"]


# --- links ---------------------------------------------------------------------------------------


def test_links_resolve_relative_root_relative_and_anchor_only_and_list_external_ones(tmp_path):
    (tmp_path / "docs" / "hackathon").mkdir(parents=True)
    (tmp_path / "docs" / "runsheet.md").write_text("# R\n", encoding="utf-8")
    page = tmp_path / "docs" / "hackathon" / "page.md"
    page.write_text(
        "[a](../runsheet.md#x), [b](/docs/runsheet.md), [c](#here), [d](https://opencv.org/) and "
        "[e](missing.md), [f](/../../etc/passwd), [g](mailto:someone), `[h](in-code.md)`.\n"
        "\n[ref]: gone.md\n",
        encoding="utf-8",
    )
    result = sc.check_links(tmp_path, (Path("docs/hackathon/page.md"),))
    assert result.failures == [
        "docs/hackathon/page.md: missing.md does not resolve",
        "docs/hackathon/page.md: /../../etc/passwd does not resolve",
        "docs/hackathon/page.md: a mail link: mailto:someone",
        "docs/hackathon/page.md: gone.md does not resolve",
    ]
    assert result.notes == ["to open by hand: https://opencv.org/"]


# --- the tracker ---------------------------------------------------------------------------------


def _tracker(root: Path, text: str = TRACKER) -> None:
    path = root / sc.TRACKER
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_missing_row_fails_unless_it_is_accepted_and_every_accepted_one_is_listed(tmp_path):
    _tracker(tmp_path)
    result = sc.check_tracker(tmp_path)
    assert result.failures == ["S5 is missing: The endpoint"]
    assert result.notes == [
        "accepted as missing: O7 (the Devpost form)",
        "accepted as missing: O10 (the production release)",
    ]
    accepted = sc.check_tracker(tmp_path, accepted=["S5"])
    assert accepted.ok and "accepted as missing: S5 (the live article)" in accepted.notes


def test_a_tracker_with_no_table_or_no_file_fails(tmp_path):
    assert sc.check_tracker(tmp_path).failures == ["docs/hackathon/rubric-tracker.md: missing"]
    _tracker(tmp_path, "# Nothing here\n")
    assert sc.check_tracker(tmp_path).failures == ["docs/hackathon/rubric-tracker.md: no table"]


def test_the_real_tracker_parses_into_its_25_rows():
    rows = sc.tracker_rows((ROOT / sc.TRACKER).read_text(encoding="utf-8"))
    assert len(rows) == 25 and all(row["Status"] in ("met", "partly", "missing") for row in rows)


# --- the run files -------------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=submission-check", "-c", f"user.email={TEST_EMAIL}",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout.strip()


def _commit(root: Path, files: dict[str, str], message: str) -> str:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _run_file(root: Path, run_id: str, sha) -> None:
    path = root / sc.RUNS / f"{run_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    run = {"run_id": run_id, "code": {"git_sha": sha, "dirty": False}}
    path.write_text(json.dumps(run), encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    measured = _commit(
        tmp_path,
        {
            "lambdas/vision/core.py": "A = 1\n",
            "lambdas/common/vision_sites.py": "B = 1\n",
            "README.md": "x\n",
        },
        "the measured code",
    )
    return tmp_path, measured


def test_a_run_on_the_same_vision_code_passes_even_when_other_files_changed(repo):
    root, measured = repo
    _run_file(root, "2026-10-16-eval-coverage-x-none", measured)
    _commit(root, {"README.md": "y\n", "docs/hackathon/x.md": "z\n"}, "docs only")
    result = sc.check_runs(root, "HEAD")
    assert result.ok, result.failures


@pytest.mark.parametrize("changed", ["lambdas/vision/core.py", "lambdas/common/vision_sites.py"])
def test_a_run_on_older_vision_code_fails_unless_results_lists_it(repo, changed):
    root, measured = repo
    _run_file(root, "2026-10-16-eval-coverage-x-none", measured)
    _commit(root, {changed: "CHANGED = 2\n"}, "a vision change")
    result = sc.check_runs(root, "HEAD")
    assert len(result.failures) == 1 and "measured on older code" in result.failures[0]
    assert "2026-10-16-eval-coverage-x-none" in result.failures[0]
    results = root / sc.RESULTS
    results.write_text(
        "| run | note |\n|---|---|\n| 2026-10-16-eval-coverage-x-none | Measured on older code: coverage "
        "reads only the COG reader, which did not change |\n",
        encoding="utf-8",
    )
    listed = sc.check_runs(root, "HEAD")
    assert listed.ok and any("listed in" in note for note in listed.notes)


def test_a_run_that_names_no_commit_or_an_unknown_one_or_is_not_json_fails(repo):
    root, _measured = repo
    _run_file(root, "2026-10-16-a", None)
    _run_file(root, "2026-10-16-b", "0" * 40)
    (root / sc.RUNS / "2026-10-16-c.json").write_text("{not json", encoding="utf-8")
    failures = sc.check_runs(root, "HEAD").failures
    assert failures == [
        "2026-10-16-a: names no commit (code.git_sha)",
        "2026-10-16-b: its commit 000000000000 is not in this repository's history",
        "2026-10-16-c.json: not a run file",
    ]


def test_the_runs_check_says_when_the_tag_does_not_exist_yet(repo):
    root, measured = repo
    _run_file(root, "2026-10-16-a", measured)
    result = sc.check_runs(root)  # the submission tag, not made in this repository
    assert result.failures == [f"no commit {sc.TAG!r} in this repository (tag it first, or pass --ref HEAD)"]
    _git(root, "tag", sc.TAG)
    assert sc.check_runs(root).ok


def test_no_run_files_is_a_note_not_a_failure(tmp_path):
    result = sc.check_runs(tmp_path, "HEAD")
    assert result.ok and result.notes == ["no run files under docs/hackathon/evidence/runs"]


def test_older_code_runs_reads_the_run_ids_on_lines_that_say_so(tmp_path):
    (tmp_path / sc.RESULTS).parent.mkdir(parents=True)
    (tmp_path / sc.RESULTS).write_text(
        "- [2026-10-16-eval-coverage-x-none.json](runs/2026-10-16-eval-coverage-x-none.json): measured on "
        "older code (the change since touches only the adapter)\n"
        "- 2026-10-19-bench-scenes-y-cool-1t: current\n",
        encoding="utf-8",
    )
    assert sc.older_code_runs(tmp_path) == {"2026-10-16-eval-coverage-x-none"}


# --- the video -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "ok"), [(None, False), (0, False), (-5, False), (299.5, True), (300, True), (301, False)]
)
def test_the_video_is_at_most_300_seconds_and_must_be_entered(seconds, ok):
    assert sc.check_video(seconds).ok is ok


# --- main ----------------------------------------------------------------------------------------


def _passing_checkout(root: Path, measured: str) -> None:
    for doc in sc.PACKAGE_DOCS:
        path = root / doc
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# A page\n\nSee [the checklist](devpost-checklist.md).\n", encoding="utf-8")
    _tracker(root)
    _run_file(root, "2026-10-16-a", measured)


def test_main_passes_on_a_ready_checkout_and_fails_with_each_failure_named(repo, capsys):
    root, measured = repo
    _passing_checkout(root, measured)
    assert sc.main(["--ref", "HEAD", "--video-seconds", "280", "--accept", "S5"], root=root) == 0
    assert capsys.readouterr().out.strip().endswith("every check passed")
    assert sc.main(["--ref", "HEAD"], root=root) == 1
    out = capsys.readouterr().out
    assert "tracker: FAIL (1)" in out and "video: FAIL (1)" in out
    assert out.strip().endswith("failed: tracker, video")


def test_the_real_package_has_its_placeholders_marked_and_its_links_whole():
    """At R1 the package holds R2's placeholders by design; whatever is left must be in R2's form,
    and every link must resolve (the docs test checks the same from its side)."""
    placeholders = sc.check_placeholders(ROOT)
    assert all(": [R2: " in failure for failure in placeholders.failures), placeholders.failures
    assert sc.check_links(ROOT).ok, sc.check_links(ROOT).failures
