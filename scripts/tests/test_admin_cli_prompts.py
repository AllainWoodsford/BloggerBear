"""The Admin CLI's prompt commands (scripts/admin_cli_prompts.py): wired into admin_cli's parser by
`register`, which adds nothing yet (docs/enhancements/model-ranch-prompt-assets-evals.md, P0)."""

from __future__ import annotations

import argparse
from unittest.mock import patch

import admin_cli
import admin_cli_prompts


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def test_build_parser_hands_the_prompt_module_its_subparsers_and_helpers():
    with patch.object(admin_cli_prompts, "register") as register:
        parser = admin_cli.build_parser()
    register.assert_called_once()
    subparsers, helpers = register.call_args.args
    assert subparsers is _subparsers(parser)
    assert helpers == {"do_request": admin_cli._do_request, "CliError": admin_cli.CliError}


def test_register_adds_no_command_yet_so_the_cli_reference_is_unchanged():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="resource")
    admin_cli_prompts.register(subparsers, {"do_request": None, "CliError": Exception})
    assert subparsers.choices == {}
    assert "prompts" not in _subparsers(admin_cli.build_parser()).choices
