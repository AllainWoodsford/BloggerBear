"""The Admin CLI's prompt commands: the `prompts` group of scripts/admin_cli.py, in its own module.

Prompts as assets (docs/enhancements/model-ranch-prompt-assets-evals.md) gives the CLI a `prompts`
group (list, pause, resume, releases, set). This module is its home, wired into admin_cli's parser
by `register`, which admin_cli.build_parser calls. The commands arrive with the prompts module and
its Admin API routes; until then `register` adds nothing, so the assistant's CLI reference
(lambdas/ops_mcp/cli_reference.json) is unchanged.
"""

from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction, helpers: dict) -> None:
    """Add the `prompts` commands to admin_cli's top-level subparsers.

    `helpers` is what admin_cli lends this module, by name: "do_request" (its signed call to the
    Admin API, which prints the answer) and "CliError" (what a command raises to stop with a
    message). They are passed in rather than imported because admin_cli imports this module, and
    an import back would be a cycle. Nothing is registered yet.
    """
