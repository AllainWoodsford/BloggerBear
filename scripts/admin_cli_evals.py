"""The Admin CLI's eval commands: the `evals` group of scripts/admin_cli.py, in its own module.

Continuous evals and the Model Ranch (docs/enhancements/model-ranch-prompt-assets-evals.md) give
the CLI an `evals` group. This module is its home, wired into admin_cli's parser by `register`,
which admin_cli.build_parser calls. Today's commands:

- `evals get` and `evals set`: the `evals` settings row (GET/PUT /evals-config). The gates, the
  events a gate needs and the judge's calibration thresholds have no flag: they are edited on the
  row in DynamoDB only.
- `evals rollup [--from DAY [--to DAY]]`: run the hourly rollup now (POST /evals/rollup), from its
  watermark, or for those days again.
- `evals status`: the switches, the rollup, late events, the judge, gated models, the cull
  proposal, the eval-work and work.fifo queues and the topics by dispatch mode, each part on its
  own (GET /evals/status).
- `evals digest [--since DAY]`: the day totals per model and prompt version since a day, a week by
  default (GET /evals/digest).

forgive, punish, ungate, revoke, cull, score and runs follow with their routes. After any change to
these commands run `python scripts/generate_cli_reference.py`, so the assistant's CLI reference
(lambdas/ops_mcp/cli_reference.json) matches.
"""

from __future__ import annotations

import argparse
from urllib.parse import quote

# `evals set`'s flags: (flag, the setting it sets, what it takes, its help). "switch" takes on, off
# or '' (clears it, back to the default); "whole" a whole number; "amount" a number of Australian
# dollars; "text" a value as given; "list" comma-separated values. '' clears any of them.
SET_FLAGS = (
    ("--cull", "cull_enabled", "switch",
     "Turn culling on or off (default off; nothing is deleted until `evals cull --apply`)"),
    ("--cull-max-per-run", "cull_max_per_run", "whole",
     "Models one cull may delete, 1 to 50 (default 3)"),
    ("--judge", "judge_enabled", "switch",
     "Run the LLM judge on new Findings and articles (default on)"),
    ("--judge-interval-hours", "judge_interval_hours", "whole",
     "Hours between judge runs, 1 to 24 (default 4)"),
    ("--judge-max-items", "judge_max_items", "whole",
     "Items one judge run may review, 1 to 50 (default 10)"),
    ("--judge-model-id", "judge_model_id", "text",
     "The judge's model (default: model-config's fallback, else the deployment's default model)"),
    ("--score-max-cost", "score_max_cost_aud", "amount",
     "A-dollars an offline scoring run may cost before it needs --yes, 0 to 100 (default 5)"),
    ("--daily-spend-cap", "evals_daily_spend_cap_aud", "amount",
     "A-dollars the judge and scoring runs may spend a day, 0 to 100 (default 2)"),
    ("--trace-calls", "trace_calls", "switch",
     "Store each model call's prompt and reply for review (default off)"),
    ("--timezone", "timezone", "text",
     "The IANA time zone whose days the rollup counts (default Australia/Sydney)"),
    ("--excluded-sources", "excluded_sources", "list",
     "Comma-separated sources the rollup leaves out: human, judge, system, run:<id>, or a Lambda's code"),
)  # fmt: skip

# What each flag's value is called in the help.
METAVARS = {
    "whole": "N",
    "amount": "AUD",
    "list": "SOURCES",
    "judge_model_id": "MODEL_ID",
    "timezone": "ZONE",
}


def _value(flag: str, kind: str, raw: str, cli_error):
    """One flag's value as the admin API takes it; None for '' (which clears the setting)."""
    if raw == "":
        return None
    if kind == "switch":
        return raw == "on"
    if kind == "list":
        return [part.strip() for part in raw.split(",") if part.strip()]
    if kind == "text":
        return raw
    try:
        return int(raw) if kind == "whole" else float(raw)
    except ValueError as exc:
        what = "a whole number" if kind == "whole" else "a number"
        raise cli_error(f"{flag} must be {what}, or '' to clear it") from exc


def register(subparsers: argparse._SubParsersAction, helpers: dict) -> None:
    """Add the `evals` commands to admin_cli's top-level subparsers.

    `helpers` is what admin_cli lends this module, by name: "do_request" (its signed call to the
    Admin API, which prints the answer) and "CliError" (what a command raises to stop with a
    message). They are passed in rather than imported because admin_cli imports this module, and
    an import back would be a cycle.
    """
    do_request = helpers["do_request"]
    cli_error = helpers["CliError"]

    def cmd_set(args: argparse.Namespace) -> None:
        body = {}
        for flag, setting, kind, _ in SET_FLAGS:
            raw = getattr(args, setting)
            if raw is not None:
                body[setting] = _value(flag, kind, raw, cli_error)
        if not body:
            raise cli_error("evals set needs at least one setting (see --help)")
        do_request(args, "PUT", "/evals-config", body=body)

    def cmd_rollup(args: argparse.Namespace) -> None:
        if args.day_to is not None and args.day_from is None:
            raise cli_error("--to needs --from")
        body = {}
        if args.day_from is not None:
            body["from"] = args.day_from
        if args.day_to is not None:
            body["to"] = args.day_to
        do_request(args, "POST", "/evals/rollup", body=body)

    def cmd_digest(args: argparse.Namespace) -> None:
        path = "/evals/digest" if args.since is None else f"/evals/digest?since={quote(args.since, safe='')}"
        do_request(args, "GET", path)

    evals_parser = subparsers.add_parser(
        "evals", help="Continuous evals and the Model Ranch: settings, the rollup, status and a digest"
    )
    evals_sub = evals_parser.add_subparsers(dest="action", required=True)

    evals_sub.add_parser(
        "get", help="Show the evals settings, the values in effect and the judge's model"
    ).set_defaults(func=lambda args: do_request(args, "GET", "/evals-config"))

    set_parser = evals_sub.add_parser(
        "set",
        help="Change evals settings (a setting not sent is unchanged; '' clears one); the gates are "
        "edited in DynamoDB only",
    )
    for flag, setting, kind, what in SET_FLAGS:
        if kind == "switch":
            set_parser.add_argument(flag, dest=setting, choices=["on", "off", ""], default=None, help=what)
        else:
            metavar = METAVARS.get(setting) or METAVARS[kind]
            set_parser.add_argument(flag, dest=setting, default=None, metavar=metavar, help=what)
    set_parser.set_defaults(func=cmd_set)

    rollup_parser = evals_sub.add_parser(
        "rollup",
        help="Run the hourly rollup now: from its watermark, or for the days --from to --to again",
    )
    rollup_parser.add_argument(
        "--from",
        dest="day_from",
        default=None,
        metavar="DAY",
        help="First day to add up again, YYYY-MM-DD (118 days back at most)",
    )
    rollup_parser.add_argument(
        "--to",
        dest="day_to",
        default=None,
        metavar="DAY",
        help="Last day to add up again, YYYY-MM-DD (default today)",
    )
    rollup_parser.set_defaults(func=cmd_rollup)

    evals_sub.add_parser(
        "status",
        help="Switches, the rollup, late events, the judge, gated models, the cull proposal, the eval-work "
        "and work.fifo queues and topics by dispatch mode",
    ).set_defaults(func=lambda args: do_request(args, "GET", "/evals/status"))

    digest_parser = evals_sub.add_parser(
        "digest", help="Each model's and prompt version's day totals since a day (default: the last 7 days)"
    )
    digest_parser.add_argument(
        "--since",
        default=None,
        metavar="DAY",
        help="The first day, YYYY-MM-DD, or an ISO time (120 days back at most)",
    )
    digest_parser.set_defaults(func=cmd_digest)
