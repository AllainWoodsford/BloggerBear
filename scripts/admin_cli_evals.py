"""The Admin CLI's eval commands: the `evals` group of scripts/admin_cli.py, in its own module.

Continuous evals and the Model Ranch (docs/enhancements/model-ranch-prompt-assets-evals.md) give
the CLI an `evals` group (get, set, rollup, status, digest, then forgive, punish, ungate, revoke,
cull, score and runs). This module is its home, wired into admin_cli's parser by `register`, which
admin_cli.build_parser calls. The commands arrive with the evals module and its Admin API routes;
until then `register` adds nothing, so the assistant's CLI reference
(lambdas/ops_mcp/cli_reference.json) is unchanged.
"""

from __future__ import annotations

import argparse


def register(subparsers: argparse._SubParsersAction, helpers: dict) -> None:
    """Add the `evals` commands to admin_cli's top-level subparsers.

    `helpers` is what admin_cli lends this module, by name: "do_request" (its signed call to the
    Admin API, which prints the answer) and "CliError" (what a command raises to stop with a
    message). They are passed in rather than imported because admin_cli imports this module, and
    an import back would be a cycle. Nothing is registered yet.
    """
