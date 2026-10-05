#!/usr/bin/env python3
"""Write the Admin CLI's reference for the operator's assistant: lambdas/ops_mcp/cli_reference.json.

The assistant answers "how do I ...?" by showing a command's own help, and builds a command only
from flags that really exist (lambdas/ops_mcp/cli_guide.py). It runs in a Lambda whose package holds
`common/` and `ops_mcp/` and not `scripts/`, so it cannot import admin_cli. This script reads
`admin_cli.build_parser()` and writes down everything the assistant needs:

- every command path ("topics update") with its one-line help;
- every argument and flag: its names, help, type, choices, default, whether it is required;
- the help **as the CLI prints it** (`python scripts/admin_cli.py topics update --help`), from
  argparse's own `format_help()`, at a fixed width so it does not depend on the terminal;
- which commands delete or take something down, and which flags force. The assistant never fills
  those in: it shows them as a template with `<placeholders>`. The rule is here, read off the
  parser, and not a list kept by hand: a command whose last word is one of DESTRUCTIVE_VERBS, and
  any flag in DESTRUCTIVE_FLAGS.

**Run it after any change to admin_cli.py's commands, flags or help text:**

    python scripts/generate_cli_reference.py          # rewrite the file
    python scripts/generate_cli_reference.py --check  # exit 1 if the file is out of date

scripts/tests/test_generate_cli_reference.py runs the same check, so a CLI change that is not
reflected fails CI and says to run this.

**The help text and Python versions.** argparse lays help out a little differently from one
Python release to the next (3.13 prints `-i, --instructions INSTRUCTIONS` where 3.11 prints
`-i INSTRUCTIONS, --instructions INSTRUCTIONS`). The file records which release wrote its help
(`help_python`). Everything else in it is the same on every release and is always compared exactly;
the help text is compared exactly on the release that wrote it, and on any other the check is that
every flag and every help string of the command is in it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import admin_cli

REFERENCE_PATH = Path(__file__).resolve().parents[1] / "lambdas" / "ops_mcp" / "cli_reference.json"
PROGRAM = "python scripts/admin_cli.py"
REGENERATE = "python scripts/generate_cli_reference.py"

# argparse asks the terminal for its width (through COLUMNS) and uses two columns fewer. Fixed, so
# the stored help is the same whoever generates it.
HELP_COLUMNS = 100

# A command whose last word is one of these deletes something or takes it down. Wider than the
# commands that exist today (delete, unpublish, reject), so one added later under another of
# these names is covered without anyone remembering this list.
DESTRUCTIVE_VERBS = frozenset(
    {"delete", "unpublish", "reject", "remove", "purge", "destroy", "drop", "wipe", "clear"}
)
# A flag that makes any command skip a safeguard.
DESTRUCTIVE_FLAGS = frozenset({"--force"})


def python_release() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _subparsers(parser: argparse.ArgumentParser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _kind(action: argparse.Action) -> str:
    if not action.option_strings:
        return "positional"
    return "flag" if action.nargs == 0 else "option"


def _argument(action: argparse.Action) -> dict:
    """One argument or flag, as plain JSON. `flags` is empty for a positional; `value` is what a
    flag (one that takes no value) stores when it is given."""
    kind = _kind(action)
    metavar = list(action.metavar) if isinstance(action.metavar, tuple) else action.metavar
    described = {
        "name": action.option_strings[0] if action.option_strings else action.dest,
        "dest": action.dest,
        "kind": kind,
        "flags": list(action.option_strings),
        "help": action.help,
        "type": action.type.__name__ if action.type is not None else "str",
        "choices": list(action.choices) if action.choices is not None else None,
        "default": action.default,
        "required": bool(action.required) if action.option_strings else action.nargs not in ("?", "*"),
        "nargs": action.nargs,
        "metavar": metavar,
        "destructive": any(flag in DESTRUCTIVE_FLAGS for flag in action.option_strings),
    }
    if kind == "flag":
        described["value"] = action.const
    return described


def _arguments(parser: argparse.ArgumentParser) -> list[dict]:
    return [
        _argument(action)
        for action in parser._actions
        if not isinstance(action, argparse._HelpAction | argparse._SubParsersAction)
    ]


def _help_text(parser: argparse.ArgumentParser) -> str:
    """What `--help` prints for this parser, at HELP_COLUMNS wide whatever the terminal is."""
    before = {name: os.environ.get(name) for name in ("COLUMNS", "LINES")}
    os.environ["COLUMNS"], os.environ["LINES"] = str(HELP_COLUMNS), "24"
    try:
        return parser.format_help()
    finally:
        for name, value in before.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _walk(parser: argparse.ArgumentParser, path: tuple[str, ...], summary: str | None, commands: dict):
    sub = _subparsers(parser)
    if path:
        entry = {"help": summary, "group": sub is not None}
        if sub is None:
            entry["arguments"] = _arguments(parser)
            entry["destructive"] = path[-1] in DESTRUCTIVE_VERBS
        else:
            entry["subcommands"] = list(sub.choices)
        entry["help_text"] = _help_text(parser)
        commands[" ".join(path)] = entry
    if sub is None:
        return
    summaries = {choice.dest: choice.help for choice in sub._get_subactions()}
    for name, child in sub.choices.items():
        _walk(child, (*path, name), summaries.get(name), commands)


def build_reference(parser: argparse.ArgumentParser | None = None) -> dict:
    """The whole reference, from the parser the CLI itself runs on."""
    parser = parser or admin_cli.build_parser()
    commands: dict = {}
    _walk(parser, (), None, commands)
    return {
        "about": f"Generated from scripts/admin_cli.py by `{REGENERATE}`. Do not edit by hand.",
        "program": PROGRAM,
        "help_python": python_release(),
        "help_columns": HELP_COLUMNS,
        "destructive_verbs": sorted(DESTRUCTIVE_VERBS),
        "destructive_flags": sorted(DESTRUCTIVE_FLAGS),
        "description": parser.description,
        "global_options": _arguments(parser),
        "help_text": _help_text(parser),
        "commands": commands,
    }


def render(reference: dict) -> str:
    return json.dumps(reference, indent=2, ensure_ascii=False) + "\n"


def _without_help_text(reference: dict) -> dict:
    """The reference minus the parts that depend on the Python release: the help as printed, and
    the note of which release printed it."""
    stripped = {k: v for k, v in reference.items() if k not in ("help_text", "help_python")}
    stripped["commands"] = {
        path: {k: v for k, v in entry.items() if k != "help_text"}
        for path, entry in reference.get("commands", {}).items()
    }
    return stripped


def _help_mentions(entry: dict) -> list[str]:
    """What a command's help must hold whichever release printed it: each flag, and the words of
    each help string (argparse re-wraps them, so they are compared with the spacing taken out)."""
    wanted = []
    for argument in entry.get("arguments", []):
        wanted.extend(argument["flags"])
        if argument["help"]:
            wanted.append(argument["help"])
    return wanted


def _squash(text: str) -> str:
    return "".join(text.split())


def problems(stored: dict, fresh: dict | None = None) -> list[str]:
    """Why the stored reference is out of date, in words; empty when it is not."""
    fresh = fresh or build_reference()
    found = []
    if _without_help_text(stored) != _without_help_text(fresh):
        ours, theirs = _without_help_text(fresh)["commands"], _without_help_text(stored)["commands"]
        changed = sorted(path for path in set(ours) | set(theirs) if ours.get(path) != theirs.get(path))
        found.append("commands, flags or help changed: " + (", ".join(changed) or "the global options"))
    if stored.get("help_python") == fresh["help_python"]:
        stale = sorted(
            path
            for path, entry in fresh["commands"].items()
            if stored.get("commands", {}).get(path, {}).get("help_text") != entry["help_text"]
        )
        if stale or stored.get("help_text") != fresh["help_text"]:
            found.append("the printed help changed: " + (", ".join(stale) or "the top-level help"))
    else:
        for path, entry in stored.get("commands", {}).items():
            printed = _squash(entry.get("help_text") or "")
            missing = [text for text in _help_mentions(entry) if _squash(text) not in printed]
            if missing:
                found.append(f"the printed help of '{path}' does not mention: {missing}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="change nothing; exit 1 if out of date")
    args = parser.parse_args(argv)

    fresh = build_reference()
    if args.check:
        if not REFERENCE_PATH.is_file():
            print(f"{REFERENCE_PATH} is missing. Run: {REGENERATE}", file=sys.stderr)
            return 1
        found = problems(json.loads(REFERENCE_PATH.read_text(encoding="utf-8")), fresh)
        for problem in found:
            print(problem, file=sys.stderr)
        if found:
            print(f"The CLI reference is out of date. Run: {REGENERATE}", file=sys.stderr)
        return 1 if found else 0

    REFERENCE_PATH.write_text(render(fresh), encoding="utf-8", newline="\n")
    count = len(fresh["commands"])
    print(f"Wrote {REFERENCE_PATH} ({count} commands, help from Python {fresh['help_python']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
