#!/usr/bin/env python3
"""Check the prompts tree (prompts/) before it is published: every rule the publisher relies on.

docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.1 and 4.2. The tree holds
`prompts/<lambda_id>/<prompt_name>/latest/vN.txt`, what is served (one file: the prompt; two or
more: an A/B split), and `.../archive/vN.txt`, retired files that are never published.
prompts/README.md states the rules for the people who edit it; this holds the tree to them:

- every folder is a prompt id (`prompts.is_prompt_id`) the registry has
  (`common.prompts_registry.all_specs`), as an asset: a code-only spec (`asset=False`) has no files;
- every file is a plain file (not a symlink) named for its version (`vN.txt`), and is UTF-8 with
  LF line endings only;
- every file in latest/ passes `prompts.validate` against its spec, the check the Lambdas run on
  each asset they read: the placeholders, the locked blocks at the tail, and the size;
- no file in latest/ holds a phrase that contradicts a locked block (GUARDRAIL_PHRASES);
- no file in latest/ has the same bytes as any other file of the tree (a hash row is keyed by the
  bytes' sha256 and names one prompt id and one version, so a text keeps the version it first had:
  restore that version from archive/ rather than copying it under a new name);
- a prompt uses each version once, across latest/ and archive/; and against an earlier tree
  (`--base-ref`, and the release below) a version keeps its bytes and a new one is numbered above
  every version that tree had, so a version names one text for ever, in lineage and the evals.

**The second pass**, against the latest release (`--release-ref`; a run that names none takes the
newest `v*` tag, the shape a release's tag has, but the workflow always names the release itself):
production runs the release's code, so the release's registry decides what production can read.
The release is checked out in a `git worktree` (its `lambdas/` only), where a subprocess imports
the release's own registry, runs the release's own `prompts.validate` on each file in latest/ and
prints the result as JSON. A prompt that registry does not have, has as code-only, or whose files its rules
refuse, is flagged `skip-at-production`: the production publish leaves that prompt as it is until
a release knows it (scripts/publish_prompts.py). A flag is not an error: dev publishes it.

Exit status: 0 when the tree can be published, 1 on any error, 2 on a usage error. Nothing it
prints holds a prompt's text: a finding names the file, the line and the rule, never the words.

    python scripts/prompt_lint.py                          # this checkout; the newest v* tag is the release
    python scripts/prompt_lint.py --release-ref v0.4.0     # name the release
    python scripts/prompt_lint.py --base-ref origin/dev    # also hold versions to what dev has now
    python scripts/prompt_lint.py --no-release             # skip the second pass
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

from common import prompts  # noqa: E402 - after the path above
from common.prompts import PromptSpec, sha256_text  # noqa: E402

TREE = "prompts"
# The only files at the top of the tree that are not prompts.
TOP_LEVEL_FILES = frozenset({"README.md", ".gitkeep"})
LATEST, ARCHIVE = "latest", "archive"
# A file's name is its version: v1.txt, v2.txt, ... (the S3 `version` tag carries the `vN` part).
VERSION_FILE = re.compile(r"^(v([1-9][0-9]*))\.txt$")
REGISTRY_FILE = "lambdas/common/prompts_registry.py"
UTF8_BOM = b"\xef\xbb\xbf"

# Phrases no asset may hold, because each contradicts a locked block. A locked block
# (`{{locked:name}}`) is a guardrail the code fills in and the asset cannot change: the
# financial-topic guidance (rule 4 of .github/copilot-instructions.md), a topic's writing rules, the
# reply format a parser relies on. The template rules already make the locked blocks end the
# prompt; these catch the free text before them telling the model to set them aside.
#
# Small on purpose, and each entry is an instruction that only ever contradicts a guardrail: never
# a topic a guardrail itself names ("financial advice", "invented sources"), which a careful prompt
# repeats in the negative ("never give financial advice"). A phrase is matched as whole words,
# without regard to letter case or line breaks, and not when the word just before it negates it
# (NEGATIONS: "do not ignore the rules below" is a guardrail, not a breach of one). A phrase here is
# not a secret: the lint names the phrase it found, never the text around it.
GUARDRAIL_PHRASES = (
    # Setting aside what follows: the locked blocks always follow the free text.
    "ignore the rules below",
    "ignore the following rules",
    "ignore the instructions below",
    "ignore the following instructions",
    "disregard the rules below",
    "disregard the following rules",
    "disregard the instructions below",
    "disregard the following instructions",
    "the rules below do not apply",
    "the following rules do not apply",
    "ignore the format below",
    "ignore the output format",
    # The classic override, of the code's instructions as much as anyone's.
    "ignore previous instructions",
    "ignore all previous instructions",
    # The financial-topic guidance: an asset may not license what it forbids.
    "you may give financial advice",
    "you can give financial advice",
    "you may give investment advice",
    "you can give investment advice",
)
# A word that, just before a phrase, turns it into the guardrail it would otherwise contradict.
NEGATIONS = frozenset(
    {"not", "never", "don't", "dont", "cannot", "can't", "won't", "nor", "mustn't", "shouldn't"}
)

# What each of prompts.validate's codes means, said the way a person fixing the file needs it.
_RULES = {
    "missing_placeholder": "a {{value}} the spec requires is missing",
    "unknown_placeholder": "it has a placeholder the spec does not name",
    "locked_missing": "a {{locked:...}} block of the spec is missing",
    "locked_order": "the locked blocks must each appear once, in the spec's order, and end the file",
    "too_large": "it is longer than the spec's max_chars",
}


class LintError(Exception):
    """The lint could not run (a ref git does not know, a release whose registry would not load)."""


@dataclass(frozen=True)
class AssetFile:
    """One file of the tree: where it is, which prompt and version it is, and its bytes."""

    path: str  # from the repository root, with forward slashes
    prompt_id: str
    folder: str  # LATEST or ARCHIVE
    version: str  # "v2"
    raw: bytes

    @property
    def number(self) -> int:
        return int(self.version[1:])

    @property
    def text(self) -> str:
        """The file as the Lambdas read it (UTF-8, `\\r\\n` as `\\n`); only for a file that decodes."""
        return self.raw.decode("utf-8").replace("\r\n", "\n")

    @property
    def sha256(self) -> str:
        """The publisher's hash of the file (`prompts.sha256_text`). For bytes that are not UTF-8,
        which the lint refuses anyway, the hash of the raw bytes, so a comparison never fails."""
        try:
            return sha256_text(self.raw.decode("utf-8"))
        except UnicodeDecodeError:
            return "raw:" + hashlib.sha256(self.raw).hexdigest()


@dataclass(frozen=True)
class Finding:
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.path}: {self.message}"


@dataclass
class Report:
    """What the lint found: the files, the errors, and the prompts production would skip, by id,
    each with why (empty when the second pass did not run)."""

    files: list[AssetFile] = field(default_factory=list)
    errors: list[Finding] = field(default_factory=list)
    skip_at_production: dict[str, str] = field(default_factory=dict)
    release: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def latest(self) -> list[AssetFile]:
        return [f for f in self.files if f.folder == LATEST]

    @property
    def ok(self) -> bool:
        return not self.errors


# --- the tree --------------------------------------------------------------------------------------


def scan(root: Path) -> tuple[list[AssetFile], list[Finding]]:
    """Every file under `root/prompts`, and every one that is not where a prompt file goes. A tree
    that does not exist is an empty one."""
    tree = root / TREE
    files, errors = [], []
    if not tree.is_dir():
        return files, errors
    for directory, folders, names in os.walk(tree):
        folders.sort()
        # os.walk lists a symlinked folder but never enters it: refused like a symlinked file,
        # rather than passed over in silence.
        for name in [folder for folder in folders if (Path(directory) / folder).is_symlink()]:
            relative = (Path(directory) / name).relative_to(root).as_posix()
            errors.append(Finding(relative, "is a symlink; the tree holds plain files only"))
        for name in sorted(names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            parts = path.relative_to(tree).parts
            if path.is_symlink():
                # git keeps a symlink as a link: published, it would be whatever it points at.
                errors.append(Finding(relative, "is a symlink; the tree holds plain files only"))
                continue
            if len(parts) == 1 and name in TOP_LEVEL_FILES:
                continue
            if len(parts) != 4 or parts[2] not in (LATEST, ARCHIVE):
                errors.append(
                    Finding(relative, "not where a prompt file goes: prompts/<lambda_id>/<prompt_name>/"
                            "latest/ or archive/, then vN.txt")
                )
                continue
            prompt_id = f"{parts[0]}/{parts[1]}"
            if not prompts.is_prompt_id(prompt_id):
                message = f"{prompt_id!r} is not a prompt id (<lambda_id>/<prompt_name>)"
                errors.append(Finding(relative, message))
                continue
            match = VERSION_FILE.fullmatch(name)
            if not match:
                errors.append(Finding(relative, "a file's name is its version: v1.txt, v2.txt, ..."))
                continue
            files.append(AssetFile(relative, prompt_id, parts[2], match.group(1), path.read_bytes()))
    return files, errors


def encoding_error(raw: bytes) -> str | None:
    """Why these bytes are not a prompt file the tree accepts, or None: UTF-8, with no byte-order
    mark, and LF line endings only (a file is hashed with `\\n` newlines, and git keeps the tree LF)."""
    if raw.startswith(UTF8_BOM):
        return "starts with a UTF-8 byte-order mark; save it without one"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "is not UTF-8"
    if b"\r" in raw:
        return "has a carriage return; the tree is LF line endings only"
    return None


def _collapsed(text: str) -> tuple[str, list[int]]:
    """`text` in lower case with every run of whitespace one space, and the line each character
    came from, so a phrase broken across lines is still found and its line still named."""
    out: list[str] = []
    lines: list[int] = []
    line, space = 1, False
    for char in text:
        if char.isspace():
            space = bool(out)
            line += char == "\n"
            continue
        if space:
            out.append(" ")
            lines.append(line)
            space = False
        out.append(char.lower())
        lines.append(line)
    return "".join(out), lines


def guardrail_hits(text: str) -> list[tuple[int, str]]:
    """(line, phrase) for each GUARDRAIL_PHRASES entry in `text`, at its first occurrence that is
    not negated by the word before it (NEGATIONS)."""
    collapsed, lines = _collapsed(text)
    hits = []
    for phrase in GUARDRAIL_PHRASES:
        for match in re.finditer(rf"(?<![\w']){re.escape(phrase)}(?![\w'])", collapsed):
            before = collapsed[: match.start()].split()
            if before and before[-1].strip(".,;:!?\"()") in NEGATIONS:
                continue
            hits.append((lines[match.start()], phrase))
            break
    return sorted(hits)


def _template_detail(spec: PromptSpec, text: str, code: str) -> str:
    """What `prompts.validate`'s code is about in this file: the placeholder names involved (names
    only, which are the grammar's [a-z_] words, never the text around them), or the size."""
    found = [(m.group(1) is not None, m.group(2)) for m in prompts.PLACEHOLDER.finditer(text)]
    if code == "missing_placeholder":
        names = sorted(spec.required - {name for locked, name in found if not locked})
        return ", ".join(f"{{{{{name}}}}}" for name in names)
    if code == "unknown_placeholder":
        unknown = sorted(
            {f"{{{{locked:{name}}}}}" for locked, name in found if locked and name not in spec.locked}
            | {
                f"{{{{{name}}}}}"
                for locked, name in found
                if not locked and name not in spec.required | spec.optional
            }
        )
        return ", ".join(unknown)
    if code == "locked_missing":
        seen = {name for locked, name in found if locked}
        return ", ".join(f"{{{{locked:{name}}}}}" for name in spec.locked if name not in seen)
    if code == "locked_order":
        return "expected " + " then ".join(f"{{{{locked:{name}}}}}" for name in spec.locked)
    if code == "too_large":
        return f"{len(text)} characters, at most {spec.max_chars}"
    return ""


def check_tree(files: list[AssetFile], specs: Iterable[PromptSpec]) -> list[Finding]:
    """Every rule that needs only this tree and this registry."""
    by_id = {spec.prompt_id: spec for spec in specs}
    errors: list[Finding] = []
    seen_versions: dict[tuple[str, str], str] = {}
    # Every file by its bytes, latest/ first, so a file in latest/ is named against the one it copies.
    by_bytes: dict[str, list[str]] = {}
    for file in sorted(files, key=lambda f: (f.folder != LATEST, f.path)):
        by_bytes.setdefault(file.sha256, []).append(file.path)
    for file in files:
        spec = by_id.get(file.prompt_id)
        if spec is None:
            errors.append(Finding(file.path, f"the registry has no spec for {file.prompt_id}"))
        elif not spec.asset:
            errors.append(
                Finding(file.path, f"{file.prompt_id} is code-only (asset=False): it has no files here")
            )
        other = seen_versions.setdefault((file.prompt_id, file.version), file.path)
        if other != file.path:
            errors.append(Finding(file.path, f"{file.version} is also {other}: a version is used once"))
        problem = encoding_error(file.raw)
        if problem:
            errors.append(Finding(file.path, problem))
            continue
        if file.folder != LATEST:
            continue  # archive/ is never published: only its name and encoding are held to the rules
        twins = [path for path in by_bytes[file.sha256] if path != file.path]
        if twins:
            message = f"has the same bytes as {twins[0]}: a text keeps its version (restore that one)"
            errors.append(Finding(file.path, message))
        text = file.text
        for line, phrase in guardrail_hits(text):
            errors.append(
                Finding(f"{file.path}:{line}", f"contradicts a locked block (guardrail phrase {phrase!r})")
            )
        if spec is not None and spec.asset:
            code = prompts.validate(spec, text)
            if code is not None:
                detail = _template_detail(spec, text, code)
                message = f"{code}: {_RULES.get(code, code)}" + (f" ({detail})" if detail else "")
                errors.append(Finding(file.path, message))
    return errors


# --- git -------------------------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True, text: bool = True) -> subprocess.CompletedProcess:
    """Run git in `repo`. With `check`, a failure is a LintError naming the command and git's last
    line (git's messages name refs and paths, never a file's contents)."""
    try:
        done = subprocess.run(  # noqa: S603 - argv is a list built here, never a shell string
            ["git", "-C", str(repo), *args], capture_output=True, text=text, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LintError(f"git {args[0]} could not run: {exc!r}") from exc
    if check and done.returncode != 0:
        stderr = done.stderr if text else done.stderr.decode("utf-8", errors="replace")
        last = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit {done.returncode}"
        raise LintError(f"git {args[0]} failed: {last}")
    return done


def resolve(repo: Path, ref: str) -> str:
    """The commit `ref` names. LintError when git does not know it, or when it starts with a dash
    (git would read it as an option)."""
    if not ref or ref.startswith("-"):
        raise LintError(f"{ref!r} is not a ref")
    done = _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False)
    if done.returncode != 0 or not done.stdout.strip():
        raise LintError(f"git does not know the ref {ref!r}")
    return done.stdout.strip()


def newest_tag(repo: Path) -> str | None:
    """The most recently made release-shaped tag (`v*`, as a release is tagged), or None when there
    is none or `repo` is not a git repository. Another tag, such as one marking a submission that is
    no release, is never taken for the release production runs."""
    done = _git(
        repo, "for-each-ref", "--sort=-creatordate", "--count=1", "--format=%(refname:short)",
        "refs/tags/v*", check=False,
    )
    tag = done.stdout.strip() if done.returncode == 0 else ""
    return tag or None


def tree_at(repo: Path, ref: str) -> list[AssetFile]:
    """The prompt files of the tree at `ref` (the ones in their right places; anything else there
    was that tree's own error)."""
    listed = _git(repo, "ls-tree", "-r", "--name-only", "-z", ref, "--", TREE).stdout
    files = []
    for path in sorted(filter(None, listed.split("\0"))):
        parts = path.split("/")[1:]
        if len(parts) != 4 or parts[2] not in (LATEST, ARCHIVE):
            continue
        match = VERSION_FILE.fullmatch(parts[3])
        prompt_id = f"{parts[0]}/{parts[1]}"
        if not match or not prompts.is_prompt_id(prompt_id):
            continue
        raw = _git(repo, "cat-file", "blob", f"{ref}:{path}", text=False).stdout
        files.append(AssetFile(path, prompt_id, parts[2], match.group(1), raw))
    return files


def check_versions_against(files: list[AssetFile], old: list[AssetFile], label: str) -> list[Finding]:
    """A version keeps its bytes, and a version that is new since the earlier tree is numbered above
    every version that tree had for the prompt. Moving a file between latest/ and archive/ is
    neither: it is the same version with the same bytes."""
    before: dict[str, dict[str, str]] = {}
    for file in old:
        before.setdefault(file.prompt_id, {})[file.version] = file.sha256
    errors = []
    for file in files:
        versions = before.get(file.prompt_id, {})
        if file.version in versions:
            if versions[file.version] != file.sha256:
                message = f"{file.version} has other bytes in {label}: a changed text is a new version"
                errors.append(Finding(file.path, message))
        elif versions:
            highest = max(int(version[1:]) for version in versions)
            if file.number <= highest:
                errors.append(
                    Finding(
                        file.path,
                        f"{file.version} is new since {label} but not numbered above v{highest}: "
                        "number a new version above every earlier one",
                    )
                )
    return errors


# --- the release's registry --------------------------------------------------------------------------

# Run in the release's own worktree, with that release's lambdas/ first on the path: its registry
# and its own validate, so the answer is the one production's Lambdas would give. It reads the
# files to check as JSON on stdin and prints JSON on stdout; it never prints a prompt's text.
_RELEASE_READER = r"""
import json
import sys

sys.path.insert(0, sys.argv[1])
from common import prompts, prompts_registry

specs = {spec.prompt_id: spec for spec in prompts_registry.all_specs()}
request = json.load(sys.stdin)
codes = {}
for item in request["files"]:
    spec = specs.get(item["prompt_id"])
    codes[item["path"]] = None if spec is None or not spec.asset else prompts.validate(spec, item["text"])
registry = {pid: {"asset": bool(s.asset), "vanilla_version": s.vanilla_version} for pid, s in specs.items()}
json.dump({"specs": registry, "codes": codes}, sys.stdout)
"""


@dataclass(frozen=True)
class ReleaseView:
    """What a release's code makes of this tree: its registry (`prompt_id` -> `asset`,
    `vanilla_version`) and its `validate` code for each file in latest/ (None: accepted)."""

    label: str
    specs: dict[str, dict]
    codes: dict[str, str | None]


def release_view(
    repo: Path, ref: str, files: list[AssetFile], *, python: str = sys.executable
) -> ReleaseView:
    """Read `ref`'s registry in a worktree of it (its lambdas/ only) with a subprocess, and have its
    `validate` judge each file in latest/. A release from before the registry existed has an
    empty one. LintError when the release's registry cannot be read."""
    commit = resolve(repo, ref)
    has_registry = _git(repo, "cat-file", "-e", f"{commit}:{REGISTRY_FILE}", check=False).returncode == 0
    latest = [f for f in files if f.folder == LATEST and encoding_error(f.raw) is None]
    if not has_registry:
        return ReleaseView(ref, {}, {f.path: None for f in latest})
    request = json.dumps(
        {"files": [{"path": f.path, "prompt_id": f.prompt_id, "text": f.text} for f in latest]}
    )
    with tempfile.TemporaryDirectory(prefix="prompt-lint-") as scratch:
        where = Path(scratch) / "release"
        _git(repo, "worktree", "add", "--detach", "--no-checkout", str(where), commit)
        try:
            # Only the Lambda package (the registry and whatever it imports), not the whole tree.
            _git(where, "checkout", commit, "--", "lambdas", ":(exclude)lambdas/tests")
            # The release's code is only imported, never handed this run's AWS credentials.
            environment = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            done = subprocess.run(  # noqa: S603 - argv is a list built here, never a shell string
                [python, "-I", "-c", _RELEASE_READER, str(where / "lambdas")],
                input=request,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                cwd=scratch,
                check=False,
            )
        finally:
            removed = _git(repo, "worktree", "remove", "--force", str(where), check=False)
            if removed.returncode != 0:
                _git(repo, "worktree", "prune", check=False)
    if done.returncode != 0:
        # Only the kind of error: a traceback would quote the release's source, prompts included.
        last = done.stderr.strip().splitlines()[-1] if done.stderr.strip() else ""
        kind = last.split(":", 1)[0].strip() or "no message"
        raise LintError(f"{ref}'s registry could not be read (exit {done.returncode}: {kind})")
    try:
        answer = json.loads(done.stdout)
        return ReleaseView(ref, dict(answer["specs"]), dict(answer["codes"]))
    except (ValueError, KeyError, TypeError) as exc:
        raise LintError(f"{ref}'s registry answered something that is not its JSON: {exc!r}") from exc


def skip_at_production(files: list[AssetFile], view: ReleaseView) -> dict[str, str]:
    """The prompts the production publish leaves as they are, each with why: the release's registry
    has no spec for it, has it as code-only, or its rules refuse one of its files in latest/."""
    skips: dict[str, str] = {}
    for file in files:
        if file.folder != LATEST or file.prompt_id in skips:
            continue
        spec = view.specs.get(file.prompt_id)
        if spec is None:
            skips[file.prompt_id] = f"not in {view.label}'s registry"
        elif not spec.get("asset", True):
            skips[file.prompt_id] = f"code-only (asset=False) in {view.label}"
        elif view.codes.get(file.path):
            skips[file.prompt_id] = f"{view.label}'s rules refuse {file.path}: {view.codes[file.path]}"
    return skips


# --- all of it -------------------------------------------------------------------------------------


def lint(
    root: Path = ROOT,
    *,
    specs: Iterable[PromptSpec] | None = None,
    release_ref: str | None = None,
    base_ref: str | None = None,
) -> Report:
    """Every check on the tree under `root`. `specs` defaults to the registry's. With `base_ref`,
    versions are also held to that tree; with `release_ref`, to the release's tree, and the second
    pass flags what production would skip. LintError when a ref cannot be read."""
    if specs is None:
        from common import prompts_registry

        specs = prompts_registry.all_specs()
    specs = list(specs)
    files, errors = scan(root)
    report = Report(files=files, errors=errors)
    report.errors += check_tree(files, specs)
    if base_ref:
        report.errors += check_versions_against(files, tree_at(root, resolve(root, base_ref)), base_ref)
    if release_ref:
        report.release = release_ref
        report.errors += check_versions_against(files, tree_at(root, resolve(root, release_ref)), release_ref)
        report.skip_at_production = skip_at_production(files, release_view(root, release_ref, files))
    return report


def print_report(report: Report, specs_count: int, out=None) -> None:
    """What the lint found, for a person: counts, then each error and flag (stdout by default,
    looked up when called so a test's capture sees it)."""
    out = out or sys.stdout
    latest = len(report.latest)
    archived = len(report.files) - latest
    ids = len({f.prompt_id for f in report.files})
    print(
        f"prompts: {latest} file(s) in latest/ and {archived} in archive/ across {ids} prompt(s); "
        f"the registry has {specs_count} spec(s).",
        file=out,
    )
    for note in report.notes:
        print(f"note: {note}", file=out)
    for finding in report.errors:
        print(f"error: {finding}", file=out)
    for prompt_id, reason in sorted(report.skip_at_production.items()):
        print(f"skip-at-production: {prompt_id}: {reason}", file=out)
    if report.release and not report.skip_at_production:
        print(f"production ({report.release}) can read every prompt here.", file=out)
    print(f"prompt lint: {len(report.errors)} error(s).", file=out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="the repository to check (default: this one)")
    release = parser.add_mutually_exclusive_group()
    release.add_argument(
        "--release-ref",
        help="the release production runs, for the second pass (default: the newest v* tag)",
    )
    release.add_argument("--no-release", action="store_true", help="skip the second pass")
    parser.add_argument("--base-ref", help="also hold versions to the tree at this ref (a PR's base)")
    args = parser.parse_args(argv)

    from common import prompts_registry

    specs = prompts_registry.all_specs()
    notes = []
    release_ref = None
    if not args.no_release:
        release_ref = args.release_ref or newest_tag(args.root)
        if release_ref is None:
            notes.append("no release tag found: the second pass did not run")
    try:
        report = lint(args.root, specs=specs, release_ref=release_ref, base_ref=args.base_ref)
    except LintError as exc:
        print(f"prompt lint: {exc}", file=sys.stderr)
        return 1
    report.notes = notes + report.notes
    print_report(report, len(specs))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
