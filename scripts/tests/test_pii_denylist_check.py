"""Tests for scripts/pii_denylist_check.py: the exact-string personal-data check.

The pure parts (denylist parsing, diff parsing, matching) are tested directly; the end-to-end
runs use a throwaway git repository in tmp_path, never this one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import pii_denylist_check as pdc

# A made-up "personal" string: never anything real in a test.
SECRET_NAME = "Quentin Placeholder"


# --- denylist parsing ------------------------------------------------------------------------------


def test_entries_skip_blanks_comments_and_duplicates_and_ignore_case():
    text = "# my list\n\nQuentin Placeholder\n  quentin placeholder  \n203.0.113.7\n"
    assert pdc.parse_entries(text) == ["quentin placeholder", "203.0.113.7"]


def test_the_environment_wins_over_the_local_file(tmp_path):
    (tmp_path / pdc.DENYLIST_FILE).write_text("from-the-file\n", encoding="utf-8")
    assert pdc.load_entries({pdc.DENYLIST_ENV: "from-the-env"}, tmp_path) == ["from-the-env"]
    assert pdc.load_entries({pdc.DENYLIST_ENV: "  "}, tmp_path) == ["from-the-file"]
    assert pdc.load_entries({}, tmp_path / "nowhere") == []


# --- diff parsing ----------------------------------------------------------------------------------

DIFF = """\
diff --git a/docs/a.md b/docs/a.md
--- a/docs/a.md
+++ b/docs/a.md
@@ -3,0 +4,2 @@ heading
+first added line
+second added line
@@ -10 +12 @@
-an old line with Quentin Placeholder
+a replacement
diff --git a/gone.txt b/gone.txt
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-Quentin Placeholder was here
"""


def test_only_added_lines_are_read_with_their_new_line_numbers():
    assert pdc.added_lines(DIFF) == [
        ("docs/a.md", 4, "first added line"),
        ("docs/a.md", 5, "second added line"),
        ("docs/a.md", 12, "a replacement"),
    ]


def test_removing_a_denylisted_string_is_never_a_finding():
    assert pdc.find_hits(pdc.added_lines(DIFF), ["quentin placeholder"]) == []


def test_a_hit_names_the_entry_by_position_only():
    lines = [("notes.md", 7, "Written by QUENTIN placeholder, 203.0.113.7")]
    hits = pdc.find_hits(lines, ["quentin placeholder", "203.0.113.7"])
    assert hits == [pdc.Hit("notes.md", 7, 1), pdc.Hit("notes.md", 7, 2)]


# --- end to end, in a throwaway repository ---------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "README.md").write_text(f"Old text mentioning {SECRET_NAME}.\n", encoding="utf-8")
    _git(tmp_path, "add", "README.md")
    _git(tmp_path, "commit", "-q", "-m", "base")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(pdc.DENYLIST_ENV, raising=False)
    return tmp_path


def _stage(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", name)


def test_staged_personal_data_fails_without_printing_it(repo, monkeypatch, capsys):
    monkeypatch.setenv(pdc.DENYLIST_ENV, SECRET_NAME)
    _stage(repo, "notes.md", f"line one\nwritten by {SECRET_NAME}\n")

    assert pdc.main(["--staged"]) == 1
    out = capsys.readouterr()
    assert "notes.md:2 adds denylist entry #1" in out.err
    assert SECRET_NAME.lower() not in (out.out + out.err).lower()


def test_text_already_in_the_history_does_not_fail_an_unrelated_change(repo, monkeypatch):
    monkeypatch.setenv(pdc.DENYLIST_ENV, SECRET_NAME)
    _stage(repo, "other.md", "nothing personal\n")
    assert pdc.main(["--staged"]) == 0


def test_a_pull_request_range_is_checked_the_same_way(repo, monkeypatch):
    monkeypatch.setenv(pdc.DENYLIST_ENV, SECRET_NAME)
    base = _git(repo, "rev-parse", "HEAD").strip()
    _stage(repo, "notes.md", f"by {SECRET_NAME}\n")
    _git(repo, "commit", "-q", "-m", "adds it")
    head = _git(repo, "rev-parse", "HEAD").strip()

    assert pdc.main(["--range", base, head]) == 1
    assert pdc.main(["--range", base, base]) == 0


def test_the_local_denylist_file_is_used_and_can_never_be_committed(repo, capsys):
    (repo / pdc.DENYLIST_FILE).write_text(f"{SECRET_NAME}\n", encoding="utf-8")
    _stage(repo, "notes.md", f"by {SECRET_NAME}\n")
    assert pdc.main(["--staged"]) == 1  # read from the file

    _git(repo, "add", pdc.DENYLIST_FILE)
    assert pdc.main(["--staged"]) == 1
    assert "must stay local" in capsys.readouterr().err


def test_no_denylist_passes_with_a_notice(repo, capsys):
    _stage(repo, "notes.md", f"by {SECRET_NAME}\n")
    assert pdc.main(["--staged"]) == 0
    assert "nothing to check" in capsys.readouterr().out
