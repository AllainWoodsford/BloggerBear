#!/usr/bin/env python3
"""Build step: mirror frontend/ into frontend-dist/ with .js/.css minified.

frontend/ stays exactly as it is -- plain, comment-rich, no build step, readable straight from a
browser's dev tools -- that is deliberate (see docs/project-plan.md and this project's frontend
README note on it) and this script does not change it. It only produces a second, disposable
directory Terraform deploys from instead (infra/environments/*/main.tf's `local.frontend_dir`),
the same "generated, never committed" relationship lambda-build/ already has with lambdas/ (see
.gitignore's comment on that one).

Deliberately scoped to .js/.css only. Every other file (the .html shells, .svg/.png/.webp/.ico
images, robots.txt) is copied byte-for-byte, unminified -- HTML minification is whitespace- and
context-sensitive in ways a mechanical pass can get subtly wrong (this project doesn't need that
risk for the ~6-8KB each of index.html/about.html/error.html actually weigh, next to app.js's own
82KB unminified), and the images/text files here have nothing a minifier would do anyway. JS and
CSS are the two asset types actually worth it: comment/whitespace stripping only, never renaming
(no identifier mangling) -- what ships is a smaller-but-still-directly-readable version of the
same source, not a rewritten one, which matters if anyone ever has to debug it from what a browser
serves.

Run with: python scripts/minify_frontend.py [--source frontend] [--dest frontend-dist]

Requires rjsmin/rcssmin (scripts/requirements.txt) -- both pure-Python, dependency-free, and doing
exactly one mechanical thing (strip comments/whitespace) rather than a general-purpose bundler,
which is why they were chosen over a Node/esbuild toolchain this project has no other use for.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import rcssmin
import rjsmin

_MINIFIERS = {
    ".js": rjsmin.jsmin,
    ".css": rcssmin.cssmin,
}


def minify_frontend(source_dir: Path, dest_dir: Path) -> list[tuple[str, int, int]]:
    """Mirror `source_dir` into `dest_dir`, minifying .js/.css and copying everything else as-is.

    `dest_dir` is removed and rebuilt from scratch each run, so a file deleted from `source_dir`
    since the last run never lingers in `dest_dir` as a stale leftover. Returns one
    (relative_path, bytes_before, bytes_after) tuple per file, in the order processed, for the
    caller to report a savings summary from -- bytes_before == bytes_after for a copied
    (non-minified) file.
    """
    if not source_dir.is_dir():
        raise SystemExit(f"source directory {source_dir} does not exist")

    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True)

    results = []
    for source_path in sorted(source_dir.rglob("*")):
        if source_path.is_dir():
            continue
        relative_path = source_path.relative_to(source_dir)
        # .as_posix() rather than str(): a Windows checkout would otherwise report
        # "bears\logo.svg" (backslashes) in the summary below, inconsistent with every path this
        # tool otherwise deals in (Terraform's frontend_files map, the CI runner it actually
        # deploys from) -- cosmetic today, but no reason to leave it host-dependent.
        relative_name = relative_path.as_posix()
        dest_path = dest_dir / relative_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        minifier = _MINIFIERS.get(source_path.suffix.lower())
        original = source_path.read_bytes()
        if minifier is None:
            dest_path.write_bytes(original)
            results.append((relative_name, len(original), len(original)))
            continue

        minified = minifier(original.decode("utf-8")).encode("utf-8")
        # A minifier that somehow produced nothing from real content is a bug worth stopping on,
        # not a 0-byte file silently deployed in place of app.js -- never happened in practice
        # (both libraries are long-established), but the failure mode (a blank page in production)
        # is bad enough to guard against outright rather than trust that.
        if original.strip() and not minified.strip():
            raise SystemExit(f"{relative_name}: minifying produced empty output from real content")
        dest_path.write_bytes(minified)
        results.append((relative_name, len(original), len(minified)))

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("frontend"))
    parser.add_argument("--dest", type=Path, default=Path("frontend-dist"))
    args = parser.parse_args()

    results = minify_frontend(args.source, args.dest)

    total_before = sum(before for _, before, _ in results)
    total_after = sum(after for _, _, after in results)
    for name, before, after in results:
        if before != after:
            saved_pct = 100 * (1 - after / before) if before else 0
            print(f"  {name}: {before:,} -> {after:,} bytes (-{saved_pct:.0f}%)")
    saved_pct = 100 * (1 - total_after / total_before) if total_before else 0
    print(
        f"{len(results)} files -> {args.dest}/ "
        f"({total_before:,} -> {total_after:,} bytes, -{saved_pct:.0f}%)"
    )


if __name__ == "__main__":
    sys.exit(main())
