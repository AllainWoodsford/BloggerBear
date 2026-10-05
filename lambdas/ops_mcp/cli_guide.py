"""The assistant as a guide to the Admin CLI: it shows the operator how to do a thing, and never
does it. Plain functions, as tools.py's are; server.py registers them.

    cli_reference  the commands there are, one line each (or one command's arguments)
    cli_help       a command's own `--help`, as the CLI prints it, on a card
    cli_guides     a few short guides written by hand: how a feature works, which commands it uses
    cli_command    one exact command, built here from a command path and values
    topics_overview  the topics and their settings, as a table

**Where the knowledge comes from.** The Lambda's package has no `scripts/` in it, so nothing here
imports admin_cli. `cli_reference.json`, next to this file, is written from admin_cli's own parser
by scripts/generate_cli_reference.py and checked in; a test regenerates it and fails when the two
differ, so the reference cannot drift from the CLI. Its text is the CLI's help: ours, and trusted.

**The order of an answer to "how do I ...?"** First the help: `cli_help` (or a guide, which brings
the help of its commands with it) puts the command's `--help` on screen, with a Copy button for the
one line that prints it. Most of the time that is the answer. Only when the operator has given the
specifics, or asks for the exact command, is `cli_command` used.

**A command is built in code, never written by the model.** `cli_command` takes a command path and a
mapping of options. The path must be in the reference; every option must be a real argument or flag
of that command; every value must fit its declared type and choices. What is missing is returned as
questions to ask the operator. Each value becomes exactly one shell word (shlex.quote), so a value
is data for its flag and can never be more command: `; topics delete x` is one quoted word, a value
that starts with a dash is attached to its flag with `=`, and a value with a line break or a control
character is refused. tests/test_ops_mcp_cli_guide.py splits every command built here with shlex
and parses it with admin_cli's real parser.

**A command that deletes or takes something down is never filled in.** Which commands those are is
read from the parser by the generator (a last word such as delete, unpublish or reject; any
`--force`) and recorded in the reference. For those, whatever values were supplied are ignored and
the card shows a template with `<placeholders>`, marked `destructive`, with a warning. So text
hidden in an article can at most make the model ask for `topics delete`; what reaches the screen
has no id in it and does not run as it stands.

**Everything is a `how_to` finding.** Help, a built command and a template all go to the page as
findings of kind `how_to`, shaped like any other (suggestions.py): the page's existing card and
Copy button show them, and the agent copies them from the tool's result in code. `how_to` is not in
the suggestion catalogue, so memory.py never records one, and server.py does not pass these tools
through it.

**Read-only.** Nothing here writes anywhere. topics_overview and the first-topic guide read the
Topics table through common/dynamo.py, as tools.py does.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shlex
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from common.dynamo import get_pipeline_config, list_topics
from common.editorial_resolver import GOAL_FIELDS, validate_editorial_goals
from common.research_schedule import resolve_interval_hours
from common.security_events import untrusted_text
from ops_mcp import topic_match
from ops_mcp.tools import _join, _parse

REFERENCE_FILE = Path(__file__).with_name("cli_reference.json")
HOW_TO = "how_to"

# How many commands' help one answer may put on screen. Each is a card a screen tall.
HELP_MAX = 3
# A value longer than this is not a flag's value, it is a document.
VALUE_MAX_CHARS = 2000
WHAT_IT_DOES_MAX_CHARS = 700

REVIEW_MODES = ("off", "shadow", "enforce")
DEFAULT_REVIEW_MODE = "shadow"

OVERVIEW_DEFAULT_LIMIT = 5
OVERVIEW_MAX_LIMIT = 50
CELL_MAX_CHARS = 60
DETAIL_MAX_CHARS = 300

# The quoting shlex produces is a POSIX shell's. The repo's own examples (scripts/README.md) are
# written the same way; PowerShell passes quotes inside a value to a program differently from one
# version to the next, so no PowerShell form is offered.
SHELL_NOTE = (
    "The quoting is for a POSIX shell (Git Bash on Windows, macOS, Linux). In PowerShell, run it "
    "from Git Bash instead."
)

DESTRUCTIVE_WARNING = (
    "This command deletes something or takes it down, or skips a safeguard, and the assistant "
    "never fills one in. It is a template: replace each <placeholder> yourself, from what the CLI "
    "shows you, and read it again before you run it."
)

# On every Admin CLI command the assistant puts on screen (the owner's rule): it is a suggestion,
# built by code from what the operator said, and they check it before it runs in their terminal.
DOUBLE_CHECK_WARNING = (
    "⚠️ Suggested by the assistant: double-check every value against the help above and what "
    "you meant before you run it. Nothing runs until you do."
)

# Commands that change settings and refuse to run with nothing to change (admin_cli raises
# CliError before any request). argparse cannot say so, so it is said here; the test runs each
# one's handler with no options and expects that refusal, and expects no other command to have it.
AT_LEAST_ONE_OPTION = frozenset(
    {"topics update", "pipeline-config set", "feedback-config set", "model-config set"}
)

# A line break or any other control character in a value. Quoting would keep it inside one word,
# but a command that spans lines on a card is not something to copy into a terminal.
_CONTROL = re.compile(r"[\x00-\x1f\x7f\x85  ]")
_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_POSITIONAL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,127}$")
_WHOLE_NUMBER = re.compile(r"^-?\d{1,12}$")
# What a model may put in front of or after a command path without changing which command it means.
_PROGRAM_WORDS = frozenset({"python", "python3", "py", "scripts/admin_cli.py", "admin_cli.py", "admin_cli"})
_HELP_WORDS = frozenset({"--help", "-h"})


@lru_cache(maxsize=1)
def reference() -> dict:
    """The CLI's reference, read once per process."""
    return json.loads(REFERENCE_FILE.read_text(encoding="utf-8"))


def command_paths(*, groups: bool = False) -> list[str]:
    """Every command path a command can be built for, in the CLI's own order."""
    return [path for path, entry in reference()["commands"].items() if groups or not entry["group"]]


# --- finding a command ---------------------------------------------------------------------------


def _path(command) -> str | None:
    """A command path as the reference keys it ("topics update"), or None if `command` is not
    text. Forgiving about what surrounds it: the program's name in front, `--help` behind."""
    if not isinstance(command, str):
        return None
    words = [word for word in command.strip().lower().split() if word not in _HELP_WORDS]
    while words and words[0] in _PROGRAM_WORDS:
        words = words[1:]
    return " ".join(words) or None


def _lookup(command) -> tuple[str | None, dict | None]:
    path = _path(command)
    return path, reference()["commands"].get(path) if path else None


def _unknown(command) -> dict:
    """The answer to a command path that is not in the reference: the nearest ones that are. What
    was asked for is not repeated (it is the model's text); the suggestions are the reference's."""
    path = _path(command) or ""
    near = difflib.get_close_matches(path, command_paths(groups=True), n=4, cutoff=0.5)
    first_word = path.split(" ")[0] if path else ""
    group = reference()["commands"].get(first_word)
    if group and group["group"]:
        near = [f"{first_word} {name}" for name in group["subcommands"]]
    spoken = "That is not a command the Admin CLI has."
    if near:
        spoken += f" The nearest are {_join(near)}."
    return {"spoken": spoken, "findings": [], "known": False, "nearest": near}


def _slug(path: str) -> str:
    return path.replace(" ", "-")


def _summary(entry: dict) -> str:
    return (entry.get("help") or "").strip().rstrip(".")


def _how_to(finding_id: str, noticed: str, path: str, suggestion: dict, **extra) -> dict:
    """A `how_to` finding, shaped as suggestions.finding shapes the others."""
    return {
        "kind": HOW_TO,
        "id": finding_id,
        "noticed": noticed,
        "where": {"command": path},
        "suggestion": suggestion,
        **extra,
    }


# --- cli_reference: what commands there are ------------------------------------------------------


def _argument_row(argument: dict) -> dict:
    """One argument as the model reads it: the name to pass to cli_command, and the CLI's help."""
    row = {
        "option": argument["name"],
        "kind": argument["kind"],
        "required": argument["required"],
        "help": argument["help"],
    }
    if argument["choices"] is not None:
        row["choices"] = argument["choices"]
    if argument["kind"] != "flag" and argument["type"] != "str":
        row["type"] = argument["type"]
    if argument["nargs"] not in (None, 0):
        row["values"] = argument["nargs"]
    if argument["destructive"]:
        row["destructive"] = True
    return row


def _described(path: str, entry: dict) -> dict:
    described = {"command": path, "help": entry["help"]}
    if entry["group"]:
        described["subcommands"] = [f"{path} {name}" for name in entry["subcommands"]]
    else:
        described["arguments"] = [_argument_row(argument) for argument in entry["arguments"]]
        described["destructive"] = entry["destructive"]
        if path in AT_LEAST_ONE_OPTION:
            described["needs"] = "at least one option"
    return described


def cli_reference(command: str | None = None) -> dict:
    """With nothing: every command group and command, one line each. With a command path: that
    command's arguments and flags with their help (and, for a group, its commands). Data for
    choosing a command; it puts nothing on screen (cli_help does)."""
    commands = reference()["commands"]
    if command is None or not str(command).strip():
        listing = [
            {"command": path, "help": entry["help"], "group": entry["group"]}
            for path, entry in commands.items()
        ]
        top = [path for path in commands if " " not in path]
        return {
            "spoken": f"The Admin CLI has {len(command_paths())} commands in {len(top)} groups.",
            "findings": [],
            "program": reference()["program"],
            "commands": listing,
        }
    path, entry = _lookup(command)
    if entry is None:
        return _unknown(command)
    described = _described(path, entry)
    if entry["group"]:
        spoken = f"{path} has {len(entry['subcommands'])} commands: {_join(entry['subcommands'])}."
    else:
        spoken = f"{path}: {_summary(entry)}." if _summary(entry) else f"That is {path}."
    return {"spoken": spoken, "findings": [], "known": True, **described}


# --- cli_help: a command's own --help, on a card -------------------------------------------------


def _help_command(path: str) -> str:
    return f"{reference()['program']} {path} --help"


def _help_finding(path: str, entry: dict) -> dict:
    summary = _summary(entry)
    return _how_to(
        f"help-{_slug(path)}",
        f"{path}: {summary}" if summary else path,
        path,
        {
            "action": "Read the options below, or print them yourself",
            "command": _help_command(path),
            "what_it_does": "Prints the help shown here. It changes nothing.",
        },
        # The CLI's own text, as `--help` prints it. For the page to show in a block; never spoken.
        help=entry["help_text"],
    )


def _help_findings(paths: list[str]) -> list[dict]:
    """The help cards for the first HELP_MAX of `paths` that exist, in order, each once."""
    commands, findings, seen = reference()["commands"], [], set()
    for path in paths:
        if path in commands and path not in seen and len(findings) < HELP_MAX:
            seen.add(path)
            findings.append(_help_finding(path, commands[path]))
    return findings


def cli_help(commands, options: Mapping | None = None) -> dict:
    """The `--help` of up to HELP_MAX commands, most relevant first, each on a card with the one
    line that prints it, and under each a suggested command (`draft`): the first command filled
    in from `options` (the values the operator gave, as cli_command takes them), every other one
    with <placeholders>. `commands` is a list of command paths (or one path)."""
    asked = [commands] if isinstance(commands, str) else list(commands or [])
    paths = [path for path in (_path(item) for item in asked) if path]
    known = [path for path in paths if path in reference()["commands"]]
    if not known:
        return _unknown(paths[0] if paths else "")
    helps = _help_findings(known)
    findings = []
    for index, help_card in enumerate(helps):
        findings.append(help_card)
        drafted = draft(help_card["where"]["command"], options if index == 0 else None)
        if drafted is not None:
            findings.append(drafted)
    shown = [found["where"]["command"] for found in helps]
    left_out = len(dict.fromkeys(known)) - len(shown)
    spoken = (
        f"The help for {_join(shown)} is on screen, with a suggested command under it to check "
        "before you run it."
    )
    if left_out > 0:
        spoken += f" I show {HELP_MAX} at a time; ask for the other{'s' if left_out > 1 else ''}."
    return {
        "spoken": spoken,
        "findings": findings,
        "commands": [_described(path, reference()["commands"][path]) for path in shown],
        "unknown": len(paths) - len(known),
    }


# --- cli_command: one exact command, built here --------------------------------------------------


def _normal(name) -> str:
    return str(name).strip().lstrip("-").replace("-", "_").lower()


def _names(arguments: list[dict]) -> dict[str, dict]:
    """Every name an option may be given by -> its argument. A flag's own spelling comes first
    ("--no-financial" and "no_financial" both mean that flag); the name argparse stores it under
    (`dest`) is accepted too, where only one argument has it."""
    index: dict[str, dict] = {}
    for argument in arguments:
        for flag in argument["flags"] or [argument["dest"]]:
            index.setdefault(_normal(flag), argument)
    by_dest: dict[str, list[dict]] = {}
    for argument in arguments:
        by_dest.setdefault(_normal(argument["dest"]), []).append(argument)
    for dest, owners in by_dest.items():
        if len(owners) == 1:
            index.setdefault(dest, owners[0])
    return index


def _placeholder(argument: dict) -> str:
    return f"<{argument['dest']}>"


class _Refused(Exception):
    """A value that cannot go into a command. The message is ours, and never quotes the value."""


def _text(value: str, argument: dict) -> str:
    if _CONTROL.search(value):
        raise _Refused("has a line break or a control character in it")
    if len(value) > VALUE_MAX_CHARS:
        raise _Refused(f"is longer than {VALUE_MAX_CHARS} characters")
    if argument["choices"] is not None and value not in argument["choices"]:
        shown = ", ".join(repr(choice) for choice in argument["choices"])
        raise _Refused(f"must be one of {shown}")
    return value


def _json_text(value, argument: dict) -> str:
    """The value of a `--...-json` flag: an object, given as one or as its JSON text. Written out
    again by json.dumps, so what goes into the command is JSON whatever was sent."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise _Refused("is not valid JSON") from exc
    if not isinstance(value, dict):
        raise _Refused("must be a JSON object")
    if argument["dest"] == "editorial_goals_json":
        # The Admin API's own check (common/editorial_resolver.py): the keys it takes, their limits.
        # Its message for an unknown key quotes the key, which here is the model's text: that case
        # is answered in our own words first.
        if set(value) - set(GOAL_FIELDS):
            raise _Refused(f"may hold only the keys {_join(list(GOAL_FIELDS))}")
        problem = validate_editorial_goals(value)
        if problem:
            raise _Refused(problem)
    return json.dumps(value, ensure_ascii=False)


def _scalar(value, argument: dict) -> str:
    """One value as the text argparse will be handed, checked against the argument's type."""
    if argument["dest"].endswith("_json"):
        return _text(_json_text(value, argument), argument)
    if isinstance(value, bool) or value is None:
        raise _Refused("needs a value")
    kind = argument["type"]
    if kind == "int":
        if not isinstance(value, int) and not (isinstance(value, str) and _WHOLE_NUMBER.match(value)):
            raise _Refused("must be a whole number")
        return _text(str(int(value)), argument)
    if kind == "float":
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise _Refused("must be a number") from exc
        if number != number or number in (float("inf"), float("-inf")):
            raise _Refused("must be a number")
        return _text(repr(number) if not isinstance(value, int) else str(value), argument)
    if isinstance(value, int | float):
        return _text(str(value), argument)  # "--research-interval-hours 3": text to the CLI
    if not isinstance(value, str):
        raise _Refused("must be text")
    return _text(value, argument)


def _words(argument: dict, value) -> tuple[list[str], bool]:
    """The shell words one option adds to the command, and whether any of them needed quoting.
    Raises _Refused for a value that does not fit."""
    kind = argument["kind"]
    if kind == "flag":
        if not isinstance(value, bool):
            raise _Refused("is a switch: true to include it, false to leave it out")
        return ([argument["flags"][0]] if value else []), False

    if isinstance(argument["nargs"], int) and argument["nargs"] > 1:
        if not isinstance(value, list | tuple) or len(value) != argument["nargs"]:
            raise _Refused(f"takes {argument['nargs']} values, as a list")
        texts = [_scalar(item, argument) for item in value]
        if any(not text or text.startswith("-") for text in texts):
            raise _Refused("has a value that is empty or starts with a dash")
        quoted = [shlex.quote(text) for text in texts]
        return [argument["flags"][0], *quoted], quoted != texts

    text = _scalar(value, argument)
    quoted = shlex.quote(text)
    if kind == "positional":
        # Every positional in the CLI is an id or a version (an ISO timestamp): one plain word,
        # never empty, never something argparse would read as a flag, never a sentence.
        if not _POSITIONAL.match(text):
            raise _Refused("must be an id: letters, digits and . _ : + - only")
        return [quoted], quoted != text
    if not text or text.startswith("-"):
        # `--flag=value` is one word: the value cannot be taken for another flag, and an empty one
        # (which several flags use to mean "clear it") is not lost by a shell that drops ''.
        return [f"{argument['flags'][0]}={quoted}"], True
    return [argument["flags"][0], quoted], quoted != text


def _question(argument: dict) -> dict:
    name = argument["dest"].replace("_", " ")
    ask = f"What is the {name}?"
    if argument["choices"]:
        ask = f"Which {name}: {' or '.join(str(choice) for choice in argument['choices'] if choice != '')}?"
    return {"option": argument["name"], "ask": ask, "help": argument["help"]}


def _what_it_does(path: str, entry: dict, used: list[dict]) -> str:
    """What running it changes, in the CLI's own help: the command's line, then each flag used."""
    parts = [f"{_summary(entry)}." if _summary(entry) else f"Runs {path}."]
    for argument in used:
        if argument["help"] and argument["flags"]:
            parts.append(f"{argument['flags'][0]}: {argument['help'].strip().rstrip('.')}.")
    text = " ".join(parts)
    return text if len(text) <= WHAT_IT_DOES_MAX_CHARS else text[: WHAT_IT_DOES_MAX_CHARS - 1] + "…"


def _template(path: str, entry: dict, chosen: list[dict]) -> dict:
    """The card for a command that deletes, takes down or forces: placeholders, never values."""
    words = []
    for argument in entry["arguments"]:
        wanted = argument["required"] or argument in chosen
        if not wanted:
            continue
        if argument["kind"] == "positional":
            words.append(_placeholder(argument))
        elif argument["kind"] == "flag":
            words.append(argument["flags"][0])
        else:
            count = argument["nargs"] if isinstance(argument["nargs"], int) else 1
            names = argument["metavar"] if isinstance(argument["metavar"], list) else None
            holders = [f"<{name.lower()}>" for name in names] if names else [_placeholder(argument)] * count
            words.extend([argument["flags"][0], *holders])
    command = " ".join([reference()["program"], path, *words])
    summary = _summary(entry)
    found = _how_to(
        f"{_slug(path)}-template",
        f"{path}: {summary}" if summary else path,
        path,
        {
            "action": "Fill in the template yourself, then run it in your own terminal",
            "command": command,
            "what_it_does": _what_it_does(path, entry, [a for a in entry["arguments"] if a in chosen]),
        },
        destructive=True,
        warning=DESTRUCTIVE_WARNING,
    )
    return {
        "spoken": (
            f"{path} deletes something, takes it down or skips a safeguard, so I have not filled it in. "
            "The template is on screen: put the values in yourself."
        ),
        "findings": [found],
        "command": path,
        "destructive": True,
        "built": True,
    }


def cli_command(command: str, options: Mapping | None = None) -> dict:
    """One exact Admin CLI command, built here from a command path and a mapping of options.

    The path must be a command in the reference. An option is named by its flag ("--name",
    "name") or the name argparse stores it under ("topic_id"); a positional by its name. A switch
    takes true or false; a `--...-json` flag takes an object; `--replace` takes a list of two.

    Returns a `how_to` finding with the command, or what stops it: `problems` (an option that does
    not exist, a value that does not fit) and `questions` (what is required and missing), for the
    operator to be asked. A command that deletes or takes something down comes back as a template
    with placeholders, whatever values were given."""
    path, entry = _lookup(command)
    if entry is None:
        return _unknown(command)
    if entry["group"]:
        return {
            "spoken": f"{path} is a group of commands: {_join(entry['subcommands'])}. Which one?",
            "findings": [],
            "command": path,
            "built": False,
            "subcommands": [f"{path} {name}" for name in entry["subcommands"]],
        }
    if options is not None and not isinstance(options, Mapping):
        return {
            "spoken": "The options must be a mapping of option name to value.",
            "findings": [],
            "command": path,
            "built": False,
            "problems": [{"option": None, "problem": "options must be a mapping of name to value"}],
        }

    index, problems = _names(entry["arguments"]), []
    chosen: dict[str, tuple[dict, object]] = {}
    for key, value in (options or {}).items():
        argument = index.get(_normal(key))
        if argument is None:
            # The key is the model's text: named only if it is a plain word.
            shown = key if isinstance(key, str) and _WORD.match(key.lstrip("-")) else "(not a name)"
            problems.append({"option": shown, "problem": f"not an option of {path}"})
        elif argument["name"] in chosen:
            problems.append({"option": argument["name"], "problem": "given twice"})
        else:
            chosen[argument["name"]] = (argument, value)

    given = [argument for argument, _ in chosen.values()]
    forced = any(argument["destructive"] and value is not False for argument, value in chosen.values())
    if entry["destructive"] or forced:
        if problems:
            return _cannot_build(path, problems, [])
        return _template(path, entry, given)

    words_by_name: dict[str, list[str]] = {}
    quoted_any = False
    for name, (argument, value) in chosen.items():
        try:
            words_by_name[name], quoted = _words(argument, value)
            quoted_any = quoted_any or quoted
        except _Refused as refused:
            problems.append({"option": name, "problem": str(refused)})

    questions = [
        _question(argument)
        for argument in entry["arguments"]
        if argument["required"] and argument["name"] not in chosen
    ]
    changes = [words for words in words_by_name.values() if words and words[0].startswith("-")]
    if path in AT_LEAST_ONE_OPTION and not changes and not problems:
        optional = [a["name"] for a in entry["arguments"] if a["flags"]]
        questions.append(
            {
                "option": None,
                "ask": "What do you want to change?",
                "help": f"At least one of {_join(optional)}",
            }
        )
    if problems or questions:
        return _cannot_build(path, problems, questions)

    # Positionals first, then flags, each in the order the CLI declares them.
    ordered = sorted(entry["arguments"], key=lambda argument: bool(argument["flags"]))
    words = [word for argument in ordered for word in words_by_name.get(argument["name"], [])]
    built = " ".join([reference()["program"], path, *words])
    used = [argument for argument in ordered if words_by_name.get(argument["name"])]
    summary = _summary(entry)
    where = {"shell": SHELL_NOTE} if quoted_any else {}
    found = _how_to(
        f"{_slug(path)}-{hashlib.sha256(built.encode('utf-8')).hexdigest()[:8]}",
        f"{path}: {summary}" if summary else path,
        path,
        {
            "action": "Read it, then run it in your own terminal",
            "command": built,
            "what_it_does": _what_it_does(path, entry, used),
        },
        warning=DOUBLE_CHECK_WARNING,
    )
    found["where"].update(where)
    return {
        "spoken": f"The {path} command is on screen. Read it before you run it.",
        "findings": [found],
        "command": path,
        "destructive": False,
        "built": True,
    }


_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")


def _slug_from(name) -> str | None:
    """A topic id made from a topic's name, the way the ids in this project look ("Watering
    vegetables" -> "watering-vegetables"): derived by code from what the operator said, never
    invented. None when nothing usable is left."""
    if not isinstance(name, str):
        return None
    slug = _SLUG_SEPARATORS.sub("-", name.lower()).strip("-")[:64].strip("-")
    return slug if slug and _POSITIONAL.match(slug) else None


def draft(
    command: str,
    options: Mapping | None = None,
    *,
    quiet_unknown: bool = False,
    show: tuple[str, ...] | list[str] = (),
) -> dict | None:
    """The suggested command under a command's help: built as cli_command builds it, but never
    refused. A value the operator gave that fits goes in; one that does not, and anything required
    and not given, is a <placeholder>, listed on the card. A topic's id missing beside its name is
    made from the name (and said so). A command that deletes or takes something down is the
    template, as always. None for a group of commands or one not in the reference."""
    path, entry = _lookup(command)
    if entry is None or entry["group"]:
        return None
    if entry["destructive"]:
        return {**_template(path, entry, [])["findings"][0], "draft": True}
    index = _names(entry["arguments"])
    given: dict[str, object] = {}
    unknown: list[str] = []
    if isinstance(options, Mapping):
        for key, value in options.items():
            argument = index.get(_normal(key))
            if argument is None:
                # The key is the model's text: named only if it is a plain word.
                plain = isinstance(key, str) and _WORD.match(key.lstrip("-"))
                unknown.append(key if plain else "(not a name)")
            elif not argument["destructive"]:
                given[argument["name"]] = value

    derived: list[str] = []
    # Only when the command makes something new: on `topics update`, a name is the new name of a
    # topic that already has its id, and an id made from it would point at another topic.
    creates = path.endswith(" create")
    if creates and "topic_id" in index and index["topic_id"]["name"] not in given and "name" in index:
        slug = _slug_from(given.get(index["name"]["name"]))
        if slug is not None:
            given[index["topic_id"]["name"]] = slug
            derived.append(index["topic_id"]["name"])

    words_by_name: dict[str, list[str]] = {}
    placeholders: list[str] = []
    quoted_any = False
    for argument in entry["arguments"]:
        name = argument["name"]
        if name in given:
            try:
                words_by_name[name], quoted = _words(argument, given[name])
                quoted_any = quoted_any or quoted
                continue
            except _Refused:
                pass  # what was given does not fit: a placeholder, and the operator fills it in
        # `show`: optional arguments a guide wants seen even when not given (its step's "ask").
        if argument["required"] or name in given or argument["dest"] in show:
            holder = _placeholder(argument)
            placeholders.append(name)
            positional = argument["kind"] == "positional"
            words_by_name[name] = [holder] if positional else [argument["flags"][0], holder]
    changes = [w for w in words_by_name.values() if w and w[0].startswith("-") and w[-1][:1] != "<"]
    if path in AT_LEAST_ONE_OPTION and not changes:
        words_by_name["(option)"] = ["<--option value>"]
        placeholders.append("an option to change")

    ordered = sorted(entry["arguments"], key=lambda argument: bool(argument["flags"]))
    words = [word for argument in ordered for word in words_by_name.get(argument["name"], [])]
    words += words_by_name.get("(option)", [])
    built = " ".join([reference()["program"], path, *words])
    used = [argument for argument in ordered if argument["name"] in words_by_name]
    where = {"shell": SHELL_NOTE} if quoted_any else {}
    if placeholders:
        where["fill in"] = ", ".join(name if name.startswith("an ") else f"<{name}>" for name in placeholders)
    if derived:
        where["made from the name"] = ", ".join(derived)
    if unknown and not quiet_unknown:
        where["left out"] = ", ".join(f"{name} (not an option of {path})" for name in unknown)
    found = _how_to(
        f"draft-{_slug(path)}",
        f"Suggested {path} command, from what you said",
        path,
        {
            "action": "Check every value, replace any <placeholder>, then run it in your own terminal",
            "command": built,
            "what_it_does": _what_it_does(path, entry, used),
        },
        draft=True,
        warning=DOUBLE_CHECK_WARNING,
    )
    found["where"].update(where)
    return found


def _cannot_build(path: str, problems: list[dict], questions: list[dict]) -> dict:
    sentences = []
    if problems:
        named = [f"{problem['option']} {problem['problem']}" for problem in problems if problem["option"]]
        sentences.append("I can't build that as asked" + (f": {'; '.join(named)}." if named else "."))
    if questions:
        asks = [question["ask"] for question in questions]
        sentences.append(f"Before I can build the {path} command I need to know: {' '.join(asks)}")
    return {
        "spoken": " ".join(sentences),
        "findings": [],
        "command": path,
        "built": False,
        "problems": problems,
        "questions": questions,
    }


# --- cli_guides: how a feature works, and which commands it uses ---------------------------------

# The adapters a topic can use, as common/adapters/registry.py's ADAPTER_REGISTRY lists them (not
# imported: it pulls in every adapter and their HTTP libraries). The test holds the two equal.
ADAPTERS = {
    # common/adapters/web_search.py: the default; searches on the topic's name with no config.
    "web_search": "searches the web for the topic's name, or for the queries in its config (the default)",
    # common/adapters/github_trending.py: the GitHub Search API's most-starred new repos, optional
    # `language` in the config.
    "github_trending": "reads GitHub's trending repositories",
    # common/adapters/hacker_news.py: the public Hacker News API's top stories.
    "hacker_news": "reads Hacker News's top stories",
    # common/adapters/crypto_feed.py; admin_api_handler._create_topic forces is_financial for it.
    "crypto_feed": "reads crypto market data; always treated as financial",
}

# The owner's worked example for an editorial goal. `exclusion_criteria` because the validator
# (common/editorial_resolver.py) takes two keys only, and this is a rule about how to write, not
# what the topic is for: `primary_focus` would replace the adapter's own focus, while
# `exclusion_criteria` is added under whichever focus applies, as "Strict Constraints".
STAR_COUNT_GOAL = {
    "exclusion_criteria": (
        "Star counts need not be exact: round down to the nearest ten and write them like "
        '"2,630+ stars" for a repository at 2,637. The count is not the story: mention it '
        "briefly near the start of the article as a titbit, and do not build the piece around it."
    )
}

# Each guide: a title, a few sentences on how the feature works (each fact from the file named
# beside it), the commands involved (most relevant first: the first HELP_MAX get their help shown),
# the steps as cli_command entries (`ask` names the options to get from the operator), and
# sometimes one worked example, which is built by cli_command like any other command.
GUIDES: dict[str, dict] = {
    "costs": {
        "title": "Cutting costs: how often topics run, and which model writes",
        "keywords": ("cost", "cheap", "spend", "often", "frequen", "interval", "cadence", "schedule", "fire"),
        "explanation": [
            # common/research_schedule.py: the heartbeat, is_due and the precedence.
            "Each topic has a research heartbeat (hourly by default) and a research interval. A "
            "heartbeat does real research, and so costs a fetch and a model call, only when the "
            "interval has passed since the topic was last checked. To spend less, raise the "
            "interval; leave the heartbeat alone.",
            # research_schedule.resolve_interval_hours; admin_cli `pipeline-config set`, `topics update`.
            "The interval is whole hours, 1 to 168. Set it for every topic with pipeline-config "
            "set, or for one with topics update; a topic's own value wins, and an empty value "
            "clears it. What it trades away: the findings are that much older when the article is written.",
            # admin_api_handler._DEFAULT_DAILY_CADENCE; admin_cli's _DAILY_CYCLE_TIMEOUT comment.
            "The daily cycle (9 am Sydney time by default) writes the article with several model "
            "calls, so it is most of the spend. A cheaper model for everything is model-config "
            "set; for one topic, topics update with a model id, or model candidates to rotate "
            "between (common/model_routing.py). models list shows the registry and its prices.",
        ],
        "commands": ["pipeline-config set", "topics update", "model-config set", "models list"],
        "steps": [
            {
                "say": "Every topic: research less often",
                "command": "pipeline-config set",
                "options": {},
                "ask": ["research_interval_hours"],
            },
            {
                "say": "One topic: research less often",
                "command": "topics update",
                "options": {},
                "ask": ["topic_id", "research_interval_hours"],
            },
            {"say": "See the models and their prices", "command": "models list", "options": {}, "ask": []},
            {
                "say": "Every topic: a cheaper model",
                "command": "model-config set",
                "options": {},
                "ask": ["model_id"],
            },
        ],
    },
    "gear": {
        "title": "Gear: armor, rings and the backpack",
        "keywords": ("gear", "equipment", "armor", "armour", "ring", "backpack", "equip", "rarity", "repair"),
        "explanation": [
            # common/equipment.py's module docstring.
            "Gear is writing guidance the bear wears. Armor (helmet, chest, gloves, boots, sword, "
            "shield) is guidance for every topic, one piece per slot; a ring is guidance for one "
            "topic, five rings at most. The backpack holds approved gear that is not worn, and "
            "nothing in it is used.",
            # common/gear.py (rarity, durability), common/wear.py (votes).
            "Each piece has a rarity, which sets how much wear it can take. A reader's downvote "
            "costs the gear that article used a point of durability and an upvote gives one back; "
            "at zero the piece comes off.",
            # scripts/gear_create.py; admin_cli's equipment subcommands.
            "equipment create makes a piece and puts it on (with no text it asks you what it "
            "needs, one question at a time). equip wears one from the backpack, bump raises its "
            "rarity, repair restores durability, announce posts a loot drop to the Musings, and "
            "equipment list shows every piece with the topic id and version the others ask for.",
        ],
        "commands": [
            "equipment create",
            "equipment equip",
            "equipment bump",
            "equipment repair",
            "equipment announce",
            "equipment list",
            "equipment unequip",
        ],
        "steps": [
            {
                "say": "Make a piece, answering its questions",
                "command": "equipment create",
                "options": {},
                "ask": [],
            },
            {
                "say": "Make a piece in one line",
                "command": "equipment create",
                "options": {},
                "ask": ["text", "scope"],
            },
            {
                "say": "See what is worn and what is in the backpack",
                "command": "equipment list",
                "options": {},
                "ask": [],
            },
            {
                "say": "Wear a piece from the backpack",
                "command": "equipment equip",
                "options": {},
                "ask": ["topic_id", "version"],
            },
            {
                "say": "Raise its rarity",
                "command": "equipment bump",
                "options": {},
                "ask": ["topic_id", "version"],
            },
            {
                "say": "Restore its durability",
                "command": "equipment repair",
                "options": {},
                "ask": ["topic_id", "version"],
            },
            {
                "say": "Post its loot drop",
                "command": "equipment announce",
                "options": {},
                "ask": ["topic_id", "version"],
            },
        ],
    },
    "editorial-goals": {
        "title": "Editorial goals: standing guidance for one topic",
        "keywords": (
            "editorial",
            "goal",
            "guidance",
            "focus",
            "style",
            "tone",
            "stars",
            "exclusion",
            "instruction",
        ),
        "explanation": [
            # common/editorial_resolver.py: GOAL_FIELDS, MAX_GOAL_TEXT_CHARS, resolve_editorial_goals.
            "A topic's editorial goals are a small block with two keys, each up to 1,000 "
            "characters. primary_focus says what the topic is for, and replaces the default focus "
            "of its adapter. exclusion_criteria is added under whichever focus applies, as strict "
            "constraints. Both go into the research, ideas and drafting prompts.",
            # editorial_resolver.validate_editorial_goals accepts no other key.
            "There is no key just for writing style. A rule about how to write, like the example "
            "here, fits exclusion_criteria: it is kept as a constraint and the topic's focus stays.",
            # admin_cli: topics update --editorial-goals-json "Replaces the topic's whole
            # editorial_goals block; '{}' clears it".
            "topics update with the editorial goals JSON replaces the whole block: read the "
            "current one with topics get first and put back any key you want to keep. An empty "
            "object clears it. " + SHELL_NOTE,
            # common/equipment.py: a ring is topic guidance too, but it is gear.
            "A ring is also guidance for one topic, but it is gear: it wears out with reader "
            "votes and is shown on the site. See the gear guide.",
        ],
        "commands": ["topics update", "topics get"],
        "steps": [
            {
                "say": "Read the topic's current goals",
                "command": "topics get",
                "options": {},
                "ask": ["topic_id"],
            },
            {
                "say": "Set the goals (this replaces the block)",
                "command": "topics update",
                "options": {},
                "ask": ["topic_id", "editorial_goals_json"],
            },
        ],
        "example": {
            "say": "Example: approximate star counts for the GitHub Trending topic",
            "command": "topics update",
            # scripts/README.md's own examples use this topic id; yours is in `topics list`.
            "options": {"topic_id": "github-trending", "editorial_goals_json": STAR_COUNT_GOAL},
        },
    },
    "first-topic": {
        "title": "Getting started: your first topic",
        "keywords": (
            "start",
            "first",
            "begin",
            "welcome",
            "setup",
            "seed",
            "new topic",
            "create a topic",
            "add a topic",
        ),
        "explanation": [
            # admin_api_handler._create_topic: topic_id and name are required; the rest default.
            "A topic needs an id (a short slug; 'global' is reserved) and a name. Everything else "
            "has a default: the web_search adapter, research every hour, an article at 9 am "
            "Sydney time.",
            # common/compliance.py: a financial topic always goes to a person.
            "Mark a topic financial if it is about money or investing: its articles then always "
            "wait in the inbox for you instead of publishing by themselves.",
            # admin_cli's comment on `topics trigger`: daily_cycle reads what research_tick wrote.
            "After creating it, run its research once and then its daily cycle once, in that "
            "order: the daily cycle writes from what research found.",
        ],
        "commands": ["topics create", "topics trigger", "topics list"],
        "questions": [
            "What do you want it to write about? (That gives the name, and a short id.)",
            "Where should it look: " + "; ".join(f"{key} {what}" for key, what in ADAPTERS.items()) + "?",
            "How many hours between research runs? (Leave it out for every hour.)",
            "Is it about money or investing? (If so it is financial, and every article waits for you.)",
        ],
        "steps": [
            {
                "say": "Create the topic",
                "command": "topics create",
                "options": {},
                "ask": ["topic_id", "name", "adapter"],
            },
            {
                "say": "Run its research once",
                "command": "topics trigger",
                "options": {"pipeline": "research_tick"},
                "ask": ["topic_id"],
            },
            {
                "say": "Then write its first article",
                "command": "topics trigger",
                "options": {"pipeline": "daily_cycle"},
                "ask": ["topic_id"],
            },
        ],
    },
    # For "there are too many options, mock up what I'm trying to do": one topic set up fully. The
    # mapping from what the operator says to which option holds it is the point of this guide.
    "topic-setup": {
        "title": "Setting a topic up fully: focus, keywords, exclusions and models",
        "keywords": (
            "mock",
            "too many options",
            "everything",
            "all the settings",
            "fully",
            "keyword",
            "phrase",
            "ignore",
            "exclude",
            "fallback",
            "set up a topic",
        ),
        "explanation": [
            "Most of a topic is said in three options. What it is about and what to leave out go in "
            "--editorial-goals-json: primary_focus (what to write about) and exclusion_criteria (what "
            "to ignore, and any rule about how to write), up to 1,000 characters each.",
            # common/adapters/web_search.py: queries, title_keywords, max_age_hours.
            "Where it looks goes in --config-json, for the web_search adapter: queries (what to "
            "search for), title_keywords (keep only results whose title has one of these words; "
            "a trailing * matches a prefix) and max_age_hours (default 24). There is no list of "
            "words to ignore: that is exclusion_criteria.",
            # admin_cli topics create --model-id / --fallback-model-id; models list shows the ids.
            "--model-id pins its model and --fallback-model-id is the one tried when that fails; "
            "both take an exact id from models list. --financial makes every article wait for you. "
            + SHELL_NOTE,
        ],
        "commands": ["topics create", "models list", "topics get"],
        "questions": [
            "What should it write about? (the name, and its focus)",
            "What should it ignore, or how should it write?",
            "What should it search for, and which words must a result's title have?",
            "Which model, and which fallback? (exact ids from models list)",
            "Is it about money or investing?",
        ],
        "steps": [
            {"say": "See the model ids", "command": "models list", "options": {}, "ask": []},
            {
                "say": "Create the topic with everything set",
                "command": "topics create",
                "options": {},
                "ask": ["topic_id", "name", "editorial_goals_json", "config_json", "fallback_model_id"],
            },
            {"say": "Check what was saved", "command": "topics get", "options": {}, "ask": ["topic_id"]},
        ],
        "example": {
            "say": "Worked example: a vegetable-garden topic, set up fully",
            "command": "topics create",
            "options": {
                "topic_id": "watering-vegetables",
                "name": "Watering vegetables",
                "editorial_goals_json": {
                    "primary_focus": "Practical watering for home vegetable gardens: timing, amounts, "
                    "drip and soaker systems, and saving water in hot weather.",
                    "exclusion_criteria": "Ignore lawns, ornamental flowers and product promotions.",
                },
                "config_json": {
                    "queries": ["vegetable garden watering", "drip irrigation vegetables"],
                    "title_keywords": ["water*", "irrigat*", "drip"],
                },
            },
        },
    },
    "review": {
        "title": "Reviewing and publishing",
        "keywords": (
            "review",
            "publish",
            "approve",
            "inbox",
            "moderat",
            "reject",
            "unpublish",
            "rewrite",
            "held",
        ),
        "explanation": [
            # scripts/review_inbox.py through admin_cli's `inbox` and `approve`.
            "inbox shows what is waiting for you at a glance. approve goes through it one "
            "keystroke each (y approve, r reject, z skip, v read it all, q quit); its --mock "
            "flag practises on made-up items.",
            # admin_cli's moderation and articles subcommands, in their own help.
            "For one item, moderation list shows the queue and moderation approve publishes one. "
            "articles rewrite sends an article back with what to fix; articles publish forces "
            "one out whatever its status. Rejecting and unpublishing take things down, so the "
            "assistant only ever shows those as a template.",
        ],
        "commands": [
            "approve",
            "inbox",
            "articles rewrite",
            "moderation list",
            "moderation approve",
            "articles publish",
            "moderation reject",
            "articles unpublish",
        ],
        "steps": [
            {"say": "See what is waiting", "command": "inbox", "options": {}, "ask": []},
            {
                "say": "Go through the waiting articles",
                "command": "approve",
                "options": {"source": "moderation"},
                "ask": [],
            },
            {
                "say": "Send one back to be rewritten",
                "command": "articles rewrite",
                "options": {},
                "ask": ["article_id", "instructions"],
            },
            {
                "say": "Publish one whatever its status",
                "command": "articles publish",
                "options": {},
                "ask": ["article_id"],
            },
            {
                "say": "Take a published one down",
                "command": "articles unpublish",
                "options": {},
                "ask": ["article_id"],
            },
        ],
    },
}


def _guide_for(topic) -> str | None:
    """The guide a few words point at: its id, or the one whose keywords they mention most."""
    if not isinstance(topic, str) or not topic.strip():
        return None
    words = topic.strip().lower()
    if words in GUIDES:
        return words
    scores = {
        # A keyword counts where a word starts with it ("frequen" finds "frequently"; "ring" does
        # not find "during").
        key: sum(1 for keyword in guide["keywords"] if re.search(r"\b" + re.escape(keyword), words))
        for key, guide in GUIDES.items()
    }
    best = max(scores, key=lambda key: scores[key])
    return best if scores[best] > 0 else None


def _topics_sentence() -> tuple[str, int | None]:
    """What the first-topic guide opens with, from the Topics table as it is now."""
    try:
        count = len(list_topics())
    except Exception as exc:  # noqa: BLE001 - the guide is still worth giving
        print(f"ops_cli_guide: could not count topics ({type(exc).__name__})")
        return "I could not read the topics just now, so I can't say whether you have any.", None
    if count == 0:
        return "Welcome. You have no topics yet, so let's seed your first one.", 0
    plural = "topics" if count != 1 else "topic"
    return f"You already have {count} {plural}; this is how you would add another.", count


def cli_guides(topic: str | None = None, options: Mapping | None = None) -> dict:
    """With nothing: the guides there are. With a guide's id, or a few words about what the
    operator wants: that guide (how the feature works, the commands involved, the steps as
    cli_command entries) and, on screen, the help of its main commands and its worked example."""
    key = _guide_for(topic)
    if key is None:
        listing = [{"id": guide_id, "title": guide["title"]} for guide_id, guide in GUIDES.items()]
        opening = "I don't have a guide for that. " if topic and str(topic).strip() else ""
        subjects = [guide["title"].split(":")[0].lower() for guide in GUIDES.values()]
        return {
            "spoken": f"{opening}I have guides on {_join(subjects)}.",
            "findings": [],
            "guides": listing,
        }

    guide = GUIDES[key]
    explanation = list(guide["explanation"])
    result: dict = {}
    if key == "first-topic":
        opening, count = _topics_sentence()
        explanation.insert(0, opening)
        result["topics"] = count
        result["adapters"] = dict(ADAPTERS)

    findings = []
    # Each main command's help, and under it the suggested command for the guide's step that uses
    # it: the step's own options, then what the operator gave (`options`), placeholders for the rest.
    step_options: dict[str, dict] = {}
    step_asks: dict[str, list[str]] = {}
    for step in guide["steps"]:
        step_options.setdefault(step["command"], dict(step.get("options") or {}))
        step_asks.setdefault(step["command"], list(step.get("ask") or []))
    for help_card in _help_findings(guide["commands"]):
        findings.append(help_card)
        path = help_card["where"]["command"]
        wanted = {**step_options.get(path, {}), **(dict(options) if isinstance(options, Mapping) else {})}
        drafted = draft(path, wanted, quiet_unknown=True, show=step_asks.get(path, ()))
        if drafted is not None:
            findings.append(drafted)
    example = guide.get("example")
    if example:
        built = cli_command(example["command"], example["options"])
        for found in built["findings"]:
            findings.append({**found, "id": f"example-{key}", "noticed": example["say"]})

    shown = [found["where"]["command"] for found in findings if "help" in found]
    spoken = (
        f"{explanation[0]} The help for {_join(shown)} is on screen, each with a suggested "
        "command under it to check before you run it."
    )
    if example:
        spoken += " So is a worked example."
    return {
        "spoken": spoken,
        "findings": findings,
        "guide": {
            "id": key,
            "title": guide["title"],
            "explanation": explanation,
            "commands": guide["commands"],
            "questions": guide.get("questions", []),
            "steps": guide["steps"],
        },
        **result,
    }


# --- topics_overview: the topics and their settings, as a table ----------------------------------

OVERVIEW_COLUMNS = [
    "Name", "Topic id", "Adapter", "Research heartbeat", "Research interval", "Daily cadence",
    "Timezone", "Model", "Financial", "Review mode", "Last researched", "Last article",
]  # fmt: skip


def _cell(value, limit: int = CELL_MAX_CHARS) -> str:
    """A stored value as a table cell: one line, cut short. Topic settings are the operator's own
    (set through the Admin API), but a table row can also be edited by hand."""
    return untrusted_text(value, limit) if value not in (None, "") else ""


def _when(timestamp) -> str:
    parsed = _parse(timestamp)
    return parsed.strftime("%Y-%m-%d %H:%M UTC") if parsed else "never"


def _interval(topic: dict, config: dict | None) -> str:
    hours = resolve_interval_hours(topic, config)
    # The topic's own value won if the answer does not move when the pipeline's does.
    own = resolve_interval_hours(topic, {"research_interval_hours": 1}) == resolve_interval_hours(
        topic, {"research_interval_hours": 2}
    )
    return f"{hours} h" if own else f"{hours} h (inherited)"


def _model(topic: dict) -> str:
    # common/model_routing.py: rotation candidates come before the topic's own model.
    candidates = [candidate for candidate in (topic.get("model_id_candidates") or []) if candidate]
    if candidates:
        return _cell("rotates: " + ", ".join(str(candidate) for candidate in candidates))
    return _cell(topic.get("model_id")) or "pipeline default"


def _review(topic: dict, config: dict | None) -> str:
    """The fresh-data review mode in force: the topic's own, else the pipeline's, else the default.
    common/fresh_review.resolve_review_mode's rule, restated because that module imports every
    adapter and Bedrock; the test holds the two to the same answers."""
    if topic.get("review_mode") in REVIEW_MODES:
        return topic["review_mode"]
    inherited = (config or {}).get("review_mode")
    return f"{inherited if inherited in REVIEW_MODES else DEFAULT_REVIEW_MODE} (inherited)"


def _overview_row(topic: dict, config: dict | None) -> list[str]:
    return [
        _cell(topic.get("name")) or _cell(topic.get("topic_id")),
        _cell(topic.get("topic_id")),
        _cell(topic.get("adapter")) or "web_search",
        _cell(topic.get("research_cadence")),
        _interval(topic, config),
        _cell(topic.get("daily_cadence")),
        # A topic created before the option existed has none and runs on UTC (admin_cli's help).
        _cell(topic.get("daily_timezone")) or "UTC",
        _model(topic),
        "yes" if topic.get("is_financial") else "no",
        _review(topic, config),
        _when(topic.get("last_research_at")),
        _when(topic.get("last_article_at")),
    ]


def _as_json(value) -> str:
    return _cell(json.dumps(value, default=str, ensure_ascii=False), DETAIL_MAX_CHARS) if value else ""


def topics_overview(limit: int = OVERVIEW_DEFAULT_LIMIT, topic: str | None = None) -> dict:
    """The topics and their settings as a table: the first `limit` (1 to 50) by topic id, and how
    many more there are. With `topic` (a topic id), that topic alone, every setting."""
    config = get_pipeline_config()
    if topic is not None:
        # The topic as the operator said it (topic_match.py): its id, its name, or something close.
        one, matched, refusal = topic_match.pick(topic)
        if one is None:
            return {"spoken": refusal, "findings": [], "matched_topic": matched}
        rows = [[column, value] for column, value in zip(OVERVIEW_COLUMNS, _overview_row(one, config))]
        rows.extend(
            [
                ["Fallback model", _cell(one.get("fallback_model_id")) or "pipeline default"],
                ["Editorial goals", _as_json(one.get("editorial_goals")) or "the adapter's default"],
                ["Adapter config", _as_json(one.get("adapter_config")) or "none"],
            ]
        )
        name = rows[0][1]
        return {
            "spoken": f"{topic_match.took(matched)}{name}'s settings are on screen.",
            "matched_topic": matched,
            "findings": [],
            "table": {"title": f"{name}: settings", "columns": ["Setting", "Value"], "rows": rows},
            "total": 1,
            "shown": 1,
            "more": 0,
        }

    limit = min(max(int(limit), 1), OVERVIEW_MAX_LIMIT)
    topics = sorted(list_topics(), key=lambda item: item.get("topic_id") or "")
    if not topics:
        return {"spoken": "There are no topics yet.", "findings": [], "total": 0, "shown": 0, "more": 0}
    shown = topics[:limit]
    more = len(topics) - len(shown)
    count = len(topics)
    spoken = f"You have {count} topic{'s' if count != 1 else ''}."
    if more:
        spoken += (
            f" The first {len(shown)} are on screen, and there {'are' if more != 1 else 'is'} {more} more."
        )
    else:
        spoken += " They are on screen." if count != 1 else " It is on screen."
    title = f"Topics ({len(shown)} of {count})" if more else f"Topics ({count})"
    return {
        "spoken": spoken,
        "findings": [],
        "table": {
            "title": title,
            "columns": list(OVERVIEW_COLUMNS),
            "rows": [_overview_row(item, config) for item in shown],
        },
        "total": count,
        "shown": len(shown),
        "more": more,
    }
