"""Fail when a change adds one of the operator's own personal strings.

Gitleaks (.gitleaks.toml) catches *kinds* of personal data: email addresses, AWS account IDs. This
catches *exact* strings that no pattern describes -- a name, a home IP, a personal address -- without
writing them anywhere public. The list comes from:

- the PII_DENYLIST environment variable (CI: the repository secret of the same name), else
- a local `.pii-denylist` file at the repo root (gitignored; the pre-commit hook uses it).

One entry per line; blank lines and lines starting with "#" are ignored; matching is
case-insensitive. Only *added* lines are checked, so text already in the history never fails a
change that doesn't touch it.

Findings name the file, the line and the entry's position in the list -- never the entry or the
line itself, because CI logs are public on a public repo.

    python scripts/pii_denylist_check.py --staged           # pre-commit: what's about to be committed
    python scripts/pii_denylist_check.py --range BASE HEAD  # CI: what a pull request adds
    python scripts/pii_denylist_check.py --all              # on-demand: every tracked file as it is now
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DENYLIST_ENV = "PII_DENYLIST"
DENYLIST_FILE = ".pii-denylist"

_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    entry: int  # 1-based position in the denylist


def parse_entries(text: str) -> list[str]:
    """Denylist text -> entries, lower-cased, without blanks, comments or duplicates."""
    entries: list[str] = []
    for raw in text.splitlines():
        entry = raw.strip()
        if entry and not entry.startswith("#") and entry.lower() not in entries:
            entries.append(entry.lower())
    return entries


def load_entries(environ: dict[str, str], root: Path) -> list[str]:
    """The environment variable wins over the local file, so CI never reads a stray file."""
    if environ.get(DENYLIST_ENV, "").strip():
        return parse_entries(environ[DENYLIST_ENV])
    path = root / DENYLIST_FILE
    if path.is_file():
        return parse_entries(path.read_text(encoding="utf-8"))
    return []


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(path, new line number, text) for every line a unified diff adds. Deleted files are skipped."""
    out: list[tuple[str, int, str]] = []
    path: str | None = None
    line = 0
    for row in diff.splitlines():
        if row.startswith("+++ "):
            target = row[4:]
            path = None if target == "/dev/null" else target.removeprefix("b/")
        elif row.startswith("@@"):
            match = _HUNK.match(row)
            line = int(match.group(1)) if match else 0
        elif path is not None and row.startswith("+"):
            out.append((path, line, row[1:]))
            line += 1
        elif path is not None and not row.startswith("-") and not row.startswith("\\"):
            line += 1  # a context line (none with -U0, but harmless)
    return out


def find_hits(lines: list[tuple[str, int, str]], entries: list[str]) -> list[Hit]:
    hits = []
    for path, number, text in lines:
        lowered = text.lower()
        for index, entry in enumerate(entries, start=1):
            if entry in lowered:
                hits.append(Hit(path, number, index))
    return hits


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8", check=True)
    return result.stdout


def changed_diff(staged: bool, base: str | None, head: str | None) -> str:
    """-U0: no context lines, so only what the change adds is ever read."""
    if staged:
        return _git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff")
    return _git("diff", "-U0", "--no-color", "--no-ext-diff", f"{base}...{head}")


def staged_paths() -> list[str]:
    return _git("diff", "--cached", "--name-only").splitlines()


def tracked_lines(root: Path) -> list[tuple[str, int, str]]:
    """(path, line number, text) for every line of every tracked text file. Binary files (a NUL in
    the first 8 KB) are skipped."""
    out: list[tuple[str, int, str]] = []
    for path in filter(None, _git("ls-files", "-z").split("\0")):
        try:
            data = (root / path).read_bytes()
        except OSError:  # listed but missing from the working tree (deleted, not yet committed)
            continue
        if b"\0" in data[:8192]:
            continue
        for number, text in enumerate(data.decode("utf-8", errors="replace").splitlines(), start=1):
            out.append((path, number, text))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="check the staged changes (pre-commit)")
    group.add_argument("--range", nargs=2, metavar=("BASE", "HEAD"), help="check BASE...HEAD (CI)")
    group.add_argument("--all", action="store_true", help="check every tracked file (on-demand scan)")
    args = parser.parse_args(argv)

    root = Path(_git("rev-parse", "--show-toplevel").strip())

    # The local list must never be committed: it would publish everything it protects.
    if args.staged and DENYLIST_FILE in staged_paths():
        print(f"pii-denylist: {DENYLIST_FILE} is staged. Unstage it; it must stay local.", file=sys.stderr)
        return 1

    entries = load_entries(dict(os.environ), root)
    if not entries:
        print(f"pii-denylist: no denylist ({DENYLIST_ENV} unset, no {DENYLIST_FILE}); nothing to check.")
        return 0

    if args.all:
        lines = tracked_lines(root)
    else:
        base, head = args.range if args.range else (None, None)
        lines = added_lines(changed_diff(args.staged, base, head))
    hits = find_hits(lines, entries)
    if not hits:
        print(f"pii-denylist: checked against {len(entries)} entries; nothing found.")
        return 0

    verb = "contains" if args.all else "adds"
    for hit in hits:
        print(f"pii-denylist: {hit.path}:{hit.line} {verb} denylist entry #{hit.entry}", file=sys.stderr)
    print(f"pii-denylist: {len(hits)} finding(s). Remove the personal data, then commit.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
