"""The Admin CLI's eval commands (scripts/admin_cli_evals.py): wired into admin_cli's parser by
`register`, which adds nothing yet (docs/enhancements/model-ranch-prompt-assets-evals.md, E0)."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import admin_cli
import admin_cli_evals


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def test_build_parser_hands_the_eval_module_its_subparsers_and_helpers():
    with patch.object(admin_cli_evals, "register") as register:
        parser = admin_cli.build_parser()
    register.assert_called_once()
    subparsers, helpers = register.call_args.args
    assert subparsers is _subparsers(parser)
    assert helpers == {"do_request": admin_cli._do_request, "CliError": admin_cli.CliError}


def test_register_adds_no_command_yet_so_the_cli_reference_is_unchanged():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="resource")
    admin_cli_evals.register(subparsers, {"do_request": None, "CliError": Exception})
    assert subparsers.choices == {}
    assert "evals" not in _subparsers(admin_cli.build_parser()).choices


def test_admin_cli_imports_the_eval_module_only_inside_build_parser():
    """At the top of admin_cli, the import would run while admin_cli itself is half loaded, and
    the commands that will need its helpers could not have them: the lazy import is the cycle's
    guard, so it stays inside the function."""
    source = admin_cli.__loader__.get_source(admin_cli.__name__)
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top_level if "admin_cli_evals" in line]
    body = source.split("def build_parser() -> argparse.ArgumentParser:", 1)[1].split("\ndef ", 1)[0]
    assert "    from admin_cli_evals import register as register_eval_commands\n" in body
