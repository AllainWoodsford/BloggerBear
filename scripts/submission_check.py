#!/usr/bin/env python3
"""The submission's pre-submit check: what must be true before the OpenCV AI Competition 2026's
Devpost form is submitted (docs/hackathon/devpost-checklist.md). Offline: it reads the checkout and
asks local git, and fetches nothing.

    python scripts/submission_check.py --video-seconds 287          # at the tag
    python scripts/submission_check.py --ref HEAD --video-seconds 287   # before the tag exists
    python scripts/submission_check.py --accept S5 --video-seconds 287  # a missing row accepted, and listed

Five checks, each reported with every failure it found:

- **placeholders**: no placeholder left in the documents a judge reads (PACKAGE_DOCS). A placeholder
  is bracketed text that is not a link: the package marks each number or detail still to come as
  `[R2: what goes here, and its source]`, and anything else in brackets (`[n]`, `[0.xx]`) counts too.
  Code blocks and inline code are skipped (Mermaid's `node["label"]` is not a placeholder), and so
  are links, reference definitions, footnotes and task-list boxes (`[ ]`, `[x]`).
- **links**: every repository-relative link in those documents resolves to a file inside the
  checkout. External links are listed for the owner to open by hand.
- **tracker**: docs/hackathon/rubric-tracker.md has no `missing` row except those that come after
  this check by design (ACCEPTED_MISSING: the form itself, the optional production release, keeping
  the site up through judging) and those passed with `--accept`; every accepted row is listed.
- **runs**: every run file under docs/hackathon/evidence/runs/ was measured on code equal to the
  checked commit's in the vision paths (VISION_PATHS), by `git diff --quiet <its git_sha> <ref> --
  <paths>`, or docs/hackathon/evidence/RESULTS.md lists it as "measured on older code" with its reason
  (the evaluation's index writes that file).
- **video**: the video's duration, entered by hand with `--video-seconds`, is at most 300 seconds.

Exit 0 when every check passes, 1 when any fails, 2 on a usage error. Runs on Windows as well as
Linux and macOS: it needs only the standard library and `git` on the PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HACKATHON = Path("docs") / "hackathon"
# The documents a judge reads, or the owner copies from: every placeholder in them must be gone.
PACKAGE_DOCS = (
    HACKATHON / "technical-report.md",
    HACKATHON / "REPRODUCE.md",
    HACKATHON / "dependencies.md",
    HACKATHON / "video-script.md",
    HACKATHON / "opencv-2026-submission.md",
    HACKATHON / "devpost-checklist.md",
)
TRACKER = HACKATHON / "rubric-tracker.md"
RUNS = HACKATHON / "evidence" / "runs"
RESULTS = HACKATHON / "evidence" / "RESULTS.md"
TAG = "opencv-2026-submission"
# The code a run file's numbers depend on (the worker, the vision core, the shared vision modules
# and the rail adapter): a run measured on a commit whose files here differ from the submission's
# is cited only when RESULTS.md says why the difference does not matter.
VISION_PATHS = (
    "lambdas/vision",
    "lambdas/vision_worker_handler.py",
    "lambdas/common/vision_*.py",
    "lambdas/common/adapters/rail_access.py",
)
OLDER_CODE = "measured on older code"
# Rows that are still `missing` when this check runs, by design: O7 is the form this check comes
# before; O10 is the optional production release (recommended: skip); O11 starts after the tag.
ACCEPTED_MISSING = ("O7", "O10", "O11")
VIDEO_MAX_SECONDS = 300
GIT_TIMEOUT_SECONDS = 30

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
# The innermost bracketed span: no bracket inside it, over line breaks (a placeholder wraps).
_BRACKETS = re.compile(r"\[([^\[\]]*)\]")
_DEFINITION = re.compile(r"^[ \t]*$")
_CHECKBOX = frozenset({" ", "x", "X"})
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
_LINK_DEFINITION = re.compile(r"^[ \t]*\[(?!\^)[^\]]+\]:[ \t]*(\S+)", re.M)
_SEPARATOR_CELL = re.compile(r"^:?-+:?$")


@dataclass
class Result:
    name: str
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


# --- placeholders --------------------------------------------------------------------------------


def prose(text: str) -> str:
    """The text with fenced code blocks and inline code spans blanked (each character but a line
    break turned into a space), so positions and line numbers are kept and nothing in code is read
    as Markdown."""
    lines, inside = [], False
    for line in text.split("\n"):
        if _FENCE.match(line):
            inside = not inside
            lines.append(" " * len(line))
        else:
            lines.append(" " * len(line) if inside else line)
    blanked = "\n".join(lines)
    return _INLINE_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), blanked)


def _line_of(text: str, position: int) -> int:
    return text.count("\n", 0, position) + 1


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + re.sub(r"[^\n]", " ", text[start:end]) + text[end:]


def placeholders(text: str) -> list[tuple[int, str]]:
    """(line, the placeholder on one line) for every bracketed span of `text` that is not a link,
    a reference definition, a footnote or a task-list box, and every `[` or `]` left unmatched.
    Innermost spans first: a link inside a placeholder is removed, then the placeholder is found.
    The span is quoted from `text` itself, inline code included (prose keeps every position)."""
    original, text = text, prose(text)
    found = []
    while match := _BRACKETS.search(text):
        inner, start, end = match.group(1), match.start(), match.end()
        after = text[end : end + 1]
        stop = end
        if after in ("(", "["):  # an inline link's text, or a reference link's: blank its target too
            close = text.find(")" if after == "(" else "]", end)
            stop = close + 1 if close != -1 else end
        else:
            line_start = text.rfind("\n", 0, start) + 1
            is_definition = after == ":" and _DEFINITION.match(text[line_start:start])
            if not (is_definition or inner.startswith("^") or inner in _CHECKBOX):
                found.append((_line_of(text, start), " ".join(original[start:end].split())))
        text = _blank(text, start, stop)
    for position, char in enumerate(text):
        if char in "[]":
            found.append((_line_of(text, position), f"an unmatched {char!r}"))
    return sorted(found)


def check_placeholders(root: Path, docs=PACKAGE_DOCS) -> Result:
    result = Result("placeholders")
    for doc in docs:
        path = root / doc
        if not path.exists():
            result.failures.append(f"{doc.as_posix()}: missing")
            continue
        for line, span in placeholders(path.read_text(encoding="utf-8")):
            result.failures.append(f"{doc.as_posix()}:{line}: {span}")
    return result


# --- links ---------------------------------------------------------------------------------------


def link_targets(text: str) -> list[str]:
    """Every Markdown link's target outside code: inline links, titled or not, and reference
    definitions."""
    readable = prose(text)
    return _LINK.findall(readable) + _LINK_DEFINITION.findall(readable)


def resolves(root: Path, page: Path, target: str) -> bool:
    """Whether a repository-relative link from `page` lands on a file or directory inside `root`.
    A root-relative target (`/docs/x.md`, as GitHub resolves it) is read against `root`."""
    path = target.split("#", 1)[0]
    if not path:
        return True  # an anchor on the same page
    resolved = root / path.lstrip("/") if path.startswith("/") else page.parent / path
    return resolved.exists() and resolved.resolve().is_relative_to(root.resolve())


def check_links(root: Path, docs=PACKAGE_DOCS) -> Result:
    result = Result("links")
    external = set()
    for doc in docs:
        path = root / doc
        if not path.exists():
            continue  # said by the placeholder check
        for target in link_targets(path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://")):
                external.add(target)
            elif target.startswith("mailto:"):
                result.failures.append(f"{doc.as_posix()}: a mail link: {target}")
            elif not resolves(root, path, target):
                result.failures.append(f"{doc.as_posix()}: {target} does not resolve")
    result.notes += [f"to open by hand: {url}" for url in sorted(external)]
    return result


# --- the tracker ---------------------------------------------------------------------------------


def tracker_rows(text: str) -> list[dict[str, str]]:
    """The tracker's table as rows keyed by its header (scripts/tests/test_hackathon_docs.py holds
    its format)."""
    table = [line.strip() for line in text.splitlines() if line.lstrip().startswith("|")]
    if len(table) < 2:
        return []
    split = [[cell.strip() for cell in line.strip("|").split("|")] for line in table]
    header, rows = split[0], split[1:]
    return [
        dict(zip(header, row, strict=False)) for row in rows if not all(_SEPARATOR_CELL.match(c) for c in row)
    ]


def check_tracker(root: Path, accepted=()) -> Result:
    result = Result("tracker")
    path = root / TRACKER
    if not path.exists():
        result.failures.append(f"{TRACKER.as_posix()}: missing")
        return result
    rows = tracker_rows(path.read_text(encoding="utf-8"))
    if not rows:
        result.failures.append(f"{TRACKER.as_posix()}: no table")
        return result
    allowed = set(ACCEPTED_MISSING) | set(accepted)
    for row in rows:
        if row.get("Status") != "missing":
            continue
        if row.get("ID") in allowed:
            result.notes.append(f"accepted as missing: {row.get('ID')} ({row.get('Say')})")
        else:
            result.failures.append(f"{row.get('ID')} is missing: {row.get('Line')}")
    return result


# --- the run files -------------------------------------------------------------------------------


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS
    )


def older_code_runs(root: Path) -> set[str]:
    """The run ids RESULTS.md lists as measured on older code: a line naming the run and the
    phrase (its reason is the line's)."""
    path = root / RESULTS
    if not path.exists():
        return set()
    listed = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if OLDER_CODE in line.lower():
            listed.update(re.findall(r"\d{4}-\d{2}-\d{2}-[A-Za-z0-9._-]+", line))
    return {run.removesuffix(".json") for run in listed}


def check_runs(root: Path, ref: str = TAG) -> Result:
    result = Result("runs")
    runs = sorted((root / RUNS).glob("*.json"))
    if not runs:
        result.notes.append(f"no run files under {RUNS.as_posix()}")
        return result
    target = git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if target.returncode != 0:
        result.failures.append(f"no commit {ref!r} in this repository (tag it first, or pass --ref HEAD)")
        return result
    listed = older_code_runs(root)
    if not (root / RESULTS).exists():
        result.notes.append(f"{RESULTS.as_posix()} does not exist: no run is listed as {OLDER_CODE}")
    for path in runs:
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
            run_id = run.get("run_id") or path.stem
            sha = (run.get("code") or {}).get("git_sha")
        except (ValueError, AttributeError):
            result.failures.append(f"{path.name}: not a run file")
            continue
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{7,40}", sha):
            result.failures.append(f"{run_id}: names no commit (code.git_sha)")
            continue
        diff = git(root, "diff", "--quiet", sha, ref, "--", *VISION_PATHS)
        if diff.returncode == 0:
            continue
        if diff.returncode != 1:
            result.failures.append(f"{run_id}: its commit {sha[:12]} is not in this repository's history")
        elif run_id in listed:
            result.notes.append(f"{run_id}: measured on older code, listed in {RESULTS.as_posix()}")
        else:
            result.failures.append(
                f"{run_id}: measured on {sha[:12]}, whose vision code differs from {ref}'s, and "
                f"{RESULTS.as_posix()} does not list it as {OLDER_CODE!r}"
            )
    return result


# --- the video -----------------------------------------------------------------------------------


def check_video(seconds: float | None) -> Result:
    result = Result("video")
    if seconds is None:
        result.failures.append("enter the video's length with --video-seconds")
    elif not 0 < seconds <= VIDEO_MAX_SECONDS:
        result.failures.append(f"the video is {seconds:g} s; at most {VIDEO_MAX_SECONDS} s is allowed")
    return result


# --- main ----------------------------------------------------------------------------------------


def run_checks(root: Path, *, ref: str, accepted, video_seconds) -> list[Result]:
    return [
        check_placeholders(root),
        check_links(root),
        check_tracker(root, accepted),
        check_runs(root, ref),
        check_video(video_seconds),
    ]


def report(results: list[Result]) -> str:
    lines = []
    for result in results:
        state = "ok" if result.ok else f"FAIL ({len(result.failures)})"
        lines.append(f"{result.name}: {state}")
        lines += [f"  - {failure}" for failure in result.failures]
        lines += [f"  . {note}" for note in result.notes]
    failed = [r.name for r in results if not r.ok]
    lines.append(f"failed: {', '.join(failed)}" if failed else "every check passed")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--ref", default=TAG, help=f"the commit the run files are checked against ({TAG})")
    parser.add_argument(
        "--accept", action="append", default=[], metavar="ID",
        help="a tracker row accepted as missing (repeatable); listed in the report",
    )  # fmt: skip
    parser.add_argument("--video-seconds", type=float, help="the uploaded video's length, entered by hand")
    args = parser.parse_args(argv)
    results = run_checks(root, ref=args.ref, accepted=args.accept, video_seconds=args.video_seconds)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")  # a Windows console's code page cannot print every name
    print(report(results))
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    sys.exit(main())
