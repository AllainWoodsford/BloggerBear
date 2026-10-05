"""The assistant's CLI reference (lambdas/ops_mcp/cli_reference.json) is what admin_cli's parser
says today. A change to the CLI's commands, flags or help that was not followed by

    python scripts/generate_cli_reference.py

fails here, with that line in the message."""

from __future__ import annotations

import argparse
import json

import pytest

import admin_cli
import generate_cli_reference as generator

OUT_OF_DATE = (
    "lambdas/ops_mcp/cli_reference.json is out of date with scripts/admin_cli.py. "
    f"Run: {generator.REGENERATE}"
)


@pytest.fixture(scope="module")
def stored() -> dict:
    assert generator.REFERENCE_PATH.is_file(), f"the reference is missing. Run: {generator.REGENERATE}"
    return json.loads(generator.REFERENCE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def fresh() -> dict:
    return generator.build_reference()


def _leaf_parsers(parser: argparse.ArgumentParser, path=()):
    """Every command path and its own argparse parser, walked here and not by the generator."""
    sub = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if path:
        yield " ".join(path), parser
    if sub is not None:
        for name, child in sub.choices.items():
            yield from _leaf_parsers(child, (*path, name))


def test_the_checked_in_reference_is_what_the_parser_says_today(stored, fresh):
    assert generator.problems(stored, fresh) == [], OUT_OF_DATE


def test_everything_but_the_printed_help_is_compared_exactly_on_every_python(stored, fresh):
    assert generator._without_help_text(stored) == generator._without_help_text(fresh), OUT_OF_DATE


def test_the_stored_help_is_what_the_cli_prints_for_every_command(stored, monkeypatch):
    """Byte for byte `format_help()` at the fixed width, on the Python release that wrote the
    file. argparse lays help out differently between releases, so on another one the test is
    that every flag and help string is in the stored text (the generator's own fallback)."""
    monkeypatch.setenv("COLUMNS", str(generator.HELP_COLUMNS))
    paths = dict(_leaf_parsers(admin_cli.build_parser()))

    assert set(stored["commands"]) == set(paths), OUT_OF_DATE
    if stored["help_python"] != generator.python_release():
        for path, entry in stored["commands"].items():
            printed = generator._squash(entry["help_text"])
            for wanted in generator._help_mentions(entry):
                assert generator._squash(wanted) in printed, (path, wanted)
        pytest.skip(f"the help was written by Python {stored['help_python']}; compared by content")
    for path, parser in paths.items():
        assert stored["commands"][path]["help_text"] == parser.format_help(), f"{path}: {OUT_OF_DATE}"
        assert stored["commands"][path]["help_text"].startswith(f"usage: admin_cli.py {path} ")


def test_on_another_python_the_help_is_checked_by_what_it_says(stored, fresh):
    """What CI does when its Python is not the one that wrote the file: no false alarm for the
    file as it is, and a help string that is gone from the printed help is still caught."""
    elsewhere = {**fresh, "help_python": "0.0"}

    assert generator.problems(stored, elsewhere) == []

    broken = json.loads(json.dumps(stored))
    broken["commands"]["topics update"]["help_text"] = "usage: admin_cli.py topics update [-h] topic_id\n"
    found = generator.problems(broken, elsewhere)
    assert found and "topics update" in found[0]


def test_the_help_does_not_depend_on_the_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "40")
    narrow = generator.build_reference()
    monkeypatch.setenv("COLUMNS", "200")
    wide = generator.build_reference()

    assert narrow == wide
    longest = max(len(line) for e in wide["commands"].values() for line in e["help_text"].splitlines())
    assert longest <= generator.HELP_COLUMNS


def test_every_argument_of_every_command_is_recorded(fresh):
    for path, parser in _leaf_parsers(admin_cli.build_parser()):
        entry = fresh["commands"][path]
        if entry["group"]:
            continue
        actions = [
            a for a in parser._actions if not isinstance(a, argparse._HelpAction | argparse._SubParsersAction)
        ]
        assert [a["dest"] for a in entry["arguments"]] == [a.dest for a in actions], path
        for recorded, action in zip(entry["arguments"], actions):
            assert recorded["flags"] == list(action.option_strings)
            assert recorded["help"] == action.help
            assert recorded["choices"] == (list(action.choices) if action.choices is not None else None)


def test_a_change_to_the_cli_is_reported_with_the_command_to_run(stored, capsys, monkeypatch):
    """A flag added to the CLI and not regenerated: the check names the command and what to run."""
    parser = admin_cli.build_parser()
    topics = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction)).choices["topics"]
    update = next(a for a in topics._actions if isinstance(a, argparse._SubParsersAction)).choices["update"]
    update.add_argument("--brand-new-flag", help="something new")
    monkeypatch.setattr(generator, "build_reference", lambda built=generator.build_reference: built(parser))

    found = generator.problems(stored, generator.build_reference())
    assert found and "topics update" in found[0]
    assert generator.main(["--check"]) == 1
    assert generator.REGENERATE in capsys.readouterr().err


def test_which_commands_are_destructive_is_read_from_the_parser(fresh):
    """The whole list, so a change to it is seen in review. Each is a command whose last word
    deletes or takes something down, or a flag that forces."""
    commands = fresh["commands"]

    assert sorted(path for path, entry in commands.items() if entry.get("destructive")) == [
        "articles unpublish",
        "equipment delete",
        "moderation reject",
        "refinements reject",
        "topics delete",
    ]
    assert sorted(
        (path, argument["name"])
        for path, entry in commands.items()
        for argument in entry.get("arguments", [])
        if argument["destructive"]
    ) == [("articles rewrite", "--force"), ("topics trigger", "--force")]
    # Nothing the rule would miss: no other command or flag is named like one that destroys.
    for path, entry in commands.items():
        words = set(path.split())
        if not entry.get("destructive"):
            assert not words & generator.DESTRUCTIVE_VERBS, path


def test_the_file_on_disk_is_exactly_what_the_generator_writes(stored, fresh):
    if stored["help_python"] != fresh["help_python"]:
        pytest.skip(f"the help was written by Python {stored['help_python']}")
    assert generator.REFERENCE_PATH.read_text(encoding="utf-8") == generator.render(fresh), OUT_OF_DATE
