"""The Admin CLI's eval commands: the `evals` group of scripts/admin_cli.py, in its own module.

Continuous evals and the Model Ranch (docs/enhancements/model-ranch-prompt-assets-evals.md) give
the CLI an `evals` group. This module is its home, wired into admin_cli's parser by `register`,
which admin_cli.build_parser calls. Today's commands:

- `evals get` and `evals set`: the `evals` settings row (GET/PUT /evals-config). The gates, the
  events a gate needs and the judge's calibration thresholds have no flag: they are edited on the
  row in DynamoDB only.
- `evals rollup [--from DAY [--to DAY]]`: run the hourly rollup now (POST /evals/rollup), from its
  watermark, or for those days again.
- `evals status`: the switches, the prompt release and the prompt fallbacks of the last day, the
  rollup, late events, the judge, gated models, the cull proposal, the eval-work and work.fifo queues
  and the topics by dispatch mode, each part on its own (GET /evals/status).
- `evals digest [--since DAY]`: the day totals per model and prompt version since a day, a week by
  default (GET /evals/digest).
- `evals forgive|punish --model ID | --prompt ID --version V --amount N [--allow-cull]`: forgive or
  punish one model or prompt version by N, in no window (POST /evals/adjust). A model's total moves
  at the next hourly rollup; a prompt version's adjustment is kept, and nothing adds those up yet.
  N is at most 100 without --allow-cull.
- `evals ungate --model ID --hours N`: hold a model's gates open for N hours, 0 to end that now
  (POST /evals/ungate).
- `evals revoke --source SOURCE [--since TIME]`: leave a source's events out of the rollup from a
  time on, or all of them (POST /evals/revoke). The days already final that hold them are added up
  again by a rollup the route starts, or by the `evals rollup` command its answer gives.
- `evals cull [--apply [--yes]]`: the weekly cull proposal and the last cull applied (the `cull`
  part of GET /evals/status); with --apply, archive then delete up to cull_max_per_run of the
  proposal's models, each checked again first (POST /evals/cull/apply), only while culling is on.
  It shows the proposal and asks first, as the other commands that delete do, unless --yes.
  `models restore` (scripts/admin_cli.py) puts a culled model back.
- `evals score --judge ID --models A,B --prompt PROMPT_ID=V2,V3 [--sample N] [--record] [--yes]`: an
  offline scoring run (POST /evals/runs). It prices the run first and shows the estimate and the
  environment, then asks before it starts; over `score_max_cost_aud`, or with --record, it needs --yes.
  It then follows the run's progress, as `topics trigger` follows its pipeline (--no-wait returns).
- `evals runs list|get|cancel|resume`: the judge's and the scoring runs, one run's progress and
  results (`get --wait` follows it), cancelling one, and sending a scoring run's unscored items again
  (GET /evals/runs, GET|DELETE /evals/runs/{run_id}, POST /evals/runs/{run_id}/resume).
- `evals trace ARTICLE_ID`: one article's call traces, each model call's prompt and reply as stored
  while `trace_calls` was on, the gear's guidance and the operator's note as their sha256, and those
  of the research behind it (GET /evals/traces/{article_id}; lambdas/common/call_traces.py).

After any change to these commands run
`python scripts/generate_cli_reference.py`, so the assistant's CLI reference
(lambdas/ops_mcp/cli_reference.json) matches.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from urllib.parse import quote

# `evals set`'s flags: (flag, the setting it sets, what it takes, its help). "switch" takes on, off
# or '' (clears it, back to the default); "whole" a whole number; "amount" a number of Australian
# dollars; "text" a value as given; "list" comma-separated values. '' clears any of them.
SET_FLAGS = (
    ("--cull", "cull_enabled", "switch",
     "Let `evals cull --apply` archive and delete the proposed models (default off); the weekly "
     "proposal is made while evals are on either way"),
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
     "Store each model call's prompt and reply for 14 days, for `evals trace` (default off; on in dev)"),
    ("--timezone", "timezone", "text",
     "The IANA time zone whose days the rollup counts (default Australia/Sydney)"),
    ("--excluded-sources", "excluded_sources", "list",
     "Comma-separated sources the rollup leaves out: human, judge, system, run:<id>, or a Lambda's code"),
    ("--digest-email", "digest_email", "switch",
     "Email the week's digest (as `evals digest` shows it) to the alerts topic on Fridays (default off)"),
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
    Admin API, which prints the answer), "fetch_json" (the same call, handing the answer back; a
    non-2xx answer is printed and exits), "get_json_or_none" (a GET whose answer is None when it is
    not a 2xx, for polling) and "CliError" (what a command raises to stop with a message). They are
    passed in rather than imported because admin_cli imports this module, and an import back would
    be a cycle.
    """
    do_request = helpers["do_request"]
    fetch_json = helpers["fetch_json"]
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

    def adjuster(direction: str):
        def cmd_adjust(args: argparse.Namespace) -> None:
            if (args.model is None) == (args.prompt is None):
                raise cli_error(f"evals {direction} needs --model ID, or --prompt ID with --version V")
            if args.prompt is not None and args.version is None:
                raise cli_error("--prompt needs --version")
            if args.model is not None and args.version is not None:
                raise cli_error("--version goes with --prompt, not --model")
            if args.model is not None:
                body = {"target": "model", "model_id": args.model}
            else:
                body = {"target": "prompt", "prompt_id": args.prompt, "version": args.version}
            body.update(direction=direction, amount=args.amount, allow_cull=args.allow_cull)
            do_request(args, "POST", "/evals/adjust", body=body)

        return cmd_adjust

    def cmd_ungate(args: argparse.Namespace) -> None:
        do_request(args, "POST", "/evals/ungate", body={"model_id": args.model, "hours": args.hours})

    def cmd_revoke(args: argparse.Namespace) -> None:
        body = {"source": args.source}
        if args.since is not None:
            body["since"] = args.since
        do_request(args, "POST", "/evals/revoke", body=body)

    def cmd_cull(args: argparse.Namespace) -> None:
        if args.yes and not args.apply:
            raise cli_error("--yes goes with --apply")
        if args.apply and args.yes:
            do_request(args, "POST", "/evals/cull/apply", body={})
            return
        cull = (fetch_json(args, "GET", "/evals/status") or {}).get("cull") or {}
        if not args.apply:
            print(json.dumps(cull, indent=2))
            return
        proposal = cull.get("proposal")
        if cull.get("status") is not None:
            raise cli_error(
                "could not read the cull proposal (`evals status` says why); --yes applies it unseen"
            )
        if not isinstance(proposal, dict) or not proposal.get("models"):
            raise cli_error("there is no cull proposal with models in it to apply (`evals cull` shows it)")
        if cull.get("expired"):
            expired = proposal.get("expires_at")
            raise cli_error(
                f"the cull proposal expired at {expired}: the next weekly cycle proposes again while the "
                "cycle runs (`model-catalog set --cycle on`)"
            )
        print(f"The cull proposal of {proposal.get('proposed_at')} (until {proposal.get('expires_at')}):")
        for entry in proposal["models"]:
            total, events = entry.get("total_perf"), entry.get("events")
            print(f"  {entry.get('model_id')}: total {total} over {events} events")
        question = (
            "Archive and delete up to cull_max_per_run of these, each checked again first "
            "(`models restore <id>` puts one back)? [y/N] "
        )
        try:
            sure = input(question).strip().lower() in ("y", "yes")
        except EOFError:
            sure = False
        if not sure:
            print("Nothing was culled.", file=sys.stderr)
            raise SystemExit(1)
        do_request(args, "POST", "/evals/cull/apply", body={})

    evals_parser = subparsers.add_parser(
        "evals",
        help="Continuous evals and the Model Ranch: settings, the rollup, status, a digest, an "
        "operator's forgive, punish, gate override and revoked source, and culling",
    )
    # metavar, as topics and articles do: the twelve commands' choice list is wider than the help's
    # 100 columns, which argparse cannot wrap.
    evals_sub = evals_parser.add_subparsers(dest="action", required=True, metavar="<action>")

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
        help="Switches, the prompt release and the last 24 hours' prompt fallbacks, the rollup, late events, "
        "the judge, gated models, the cull proposal and the last cull applied, the eval-work and "
        "work.fifo queues and topics by dispatch mode",
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

    for direction, what in (
        ("forgive", "Raise a model's total by N, and no window, or record a forgive of a prompt version"),
        ("punish", "Lower a model's total by N, and no window, or record a punish of a prompt version"),
    ):
        adjust_parser = evals_sub.add_parser(
            direction,
            help=f"{what} (an Admin {direction.capitalize()} event): a model's total, which the total gate "
            "and culling read, moves at the next hourly rollup; nothing adds up a prompt version's "
            "adjustments yet",
        )
        adjust_parser.add_argument("--model", default=None, metavar="MODEL_ID", help="The model to adjust")
        adjust_parser.add_argument(
            "--prompt",
            default=None,
            metavar="PROMPT_ID",
            help="The prompt to adjust instead, <lambda_id>/<prompt_name>; needs --version",
        )
        adjust_parser.add_argument(
            "--version", default=None, metavar="VERSION", help="The prompt's version: v<N>, or vanilla:v<N>"
        )
        adjust_parser.add_argument(
            "--amount",
            type=int,
            required=True,
            metavar="N",
            help="How far, 1 to 100 (more with --allow-cull)",
        )
        adjust_parser.add_argument(
            "--allow-cull",
            action="store_true",
            default=False,
            help="Allow more than 100 (a punish that large can take a model below the total gate, where "
            "it is gated and may be culled)",
        )
        adjust_parser.set_defaults(func=adjuster(direction))

    ungate_parser = evals_sub.add_parser(
        "ungate", help="Hold a model's gates open for some hours, whatever its scores (ungated_until)"
    )
    ungate_parser.add_argument("--model", required=True, metavar="MODEL_ID", help="The model")
    ungate_parser.add_argument(
        "--hours",
        type=int,
        required=True,
        metavar="N",
        help="How long, 1 to 720 hours from now; 0 ends an override now",
    )
    ungate_parser.set_defaults(func=cmd_ungate)

    revoke_parser = evals_sub.add_parser(
        "revoke",
        help="Leave a source's events out of the rollup (added to excluded_sources); the days already "
        "final that hold them are added up again, by a rollup it starts or the `evals rollup` command it "
        "gives",
    )
    revoke_parser.add_argument(
        "--source",
        required=True,
        metavar="SOURCE",
        help="human, judge or system (who decided), run:<run id> (an offline run), or a Lambda's code",
    )
    revoke_parser.add_argument(
        "--since",
        default=None,
        metavar="TIME",
        help="Only its events from this ISO time on, e.g. 2026-10-27T00:00:00Z (default: all of them)",
    )
    revoke_parser.set_defaults(func=cmd_revoke)

    cull_parser = evals_sub.add_parser(
        "cull",
        help="Show the weekly cull proposal (models below the total gate that play no role) and the last "
        "cull applied; --apply archives then deletes up to cull_max_per_run of them, while culling is on",
    )
    cull_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Cull now: each proposed model is checked again first, and `models restore` puts one back",
    )
    cull_parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="With --apply: do not show the proposal or ask first",
    )
    cull_parser.set_defaults(func=cmd_cull)

    _register_runs(evals_sub, helpers)
    _register_trace(evals_sub, do_request)


# --- offline scoring runs (`evals score`, `evals runs ...`) --------------------------------------------

# How often a followed run is read, and for how long: a sample of a few items finishes in minutes, a
# large one can take an hour at eval-jobs' two at a time.
RUN_POLL_SECONDS = 10
RUN_WAIT_SECONDS = 1800
# A run in one of these is not going to move while it is followed: finished, cancelled, or waiting for
# the next day's spend.
SETTLED = ("done", "cancelled", "paused: spend cap")


def _prompt_versions(text: str, cli_error) -> tuple[str, list[str]]:
    """`--prompt`'s value: `<prompt_id>=<version>[,<version>...]`."""
    prompt_id, separator, versions = text.partition("=")
    parts = [part.strip() for part in versions.split(",") if part.strip()]
    if not separator or not prompt_id.strip() or not parts:
        raise cli_error("--prompt is <prompt_id>=<version>[,<version>...], e.g. daily_cycle/draft=v2,vanilla")
    return prompt_id.strip(), parts


def _print_plan(plan: dict) -> None:
    """What a run would do and cost, before it is asked for."""
    combos = plan.get("combinations") or []
    print(f"Environment:   {plan.get('environment') or 'unknown'}")
    print(f"Prompt:        {plan.get('prompt_id')}")
    print(f"Judge:         {plan.get('judge_model_id')}")
    print(f"Combinations:  {len(combos)} (models x versions)")
    for code in combos:
        print(f"               {code}")
    print(f"Items:         {plan.get('items')} past article(s) of the {plan.get('sample')} asked for")
    print(f"Messages:      {plan.get('messages')}")
    print(
        f"Estimate:      about A${plan.get('estimate_aud', 0):.4f} (today's evals cap leaves "
        f"A${plan.get('remaining_aud', 0):.4f}; over A${plan.get('score_max_cost_aud', 0)} needs --yes)"
    )
    records = "yes, as Offline Review events" if plan.get("record") else "no (item rows only)"
    print(f"Records:       {records}")


def _progress_line(run: dict) -> str:
    progress = run.get("progress") or {}
    return (
        f"run {run.get('run_id')}: {run.get('status')}, {progress.get('results', 0)} of "
        f"{progress.get('messages', 0)} with a result ({progress.get('scoring', 0)} being scored), "
        f"A${run.get('spent_aud', 0):.4f} spent"
    )


def _follow(args: argparse.Namespace, run_id: str, get_json_or_none) -> dict | None:
    """Read the run every RUN_POLL_SECONDS until it settles or RUN_WAIT_SECONDS pass, printing a line
    whenever its progress changes. The run as last read (None if it never could be)."""
    path = f"/evals/runs/{quote(run_id, safe='')}"
    deadline = time.monotonic() + RUN_WAIT_SECONDS
    last_line, run = None, None
    while True:
        read = get_json_or_none(args, path)
        if read is not None:
            run = read
            line = _progress_line(run)
            if line != last_line:
                print(line, file=sys.stderr)
                last_line = line
            if run.get("status") in SETTLED:
                break
        if time.monotonic() >= deadline:
            print(
                f"Still going after {RUN_WAIT_SECONDS}s: `evals runs get {run_id} --wait` follows it again.",
                file=sys.stderr,
            )
            break
        time.sleep(RUN_POLL_SECONDS)
    if run is not None and run.get("status") == "paused: spend cap":
        print(
            "Paused by the day's evals spend cap: its items wait on the queue and go on when the cap "
            f"allows (`evals set --daily-spend-cap`, or `evals runs resume {run_id}` tomorrow).",
            file=sys.stderr,
        )
    return run


def _register_runs(evals_sub, helpers: dict) -> None:
    """`evals score` and `evals runs list|get|cancel|resume` (offline scoring runs)."""
    do_request = helpers["do_request"]
    fetch_json = helpers["fetch_json"]
    get_json_or_none = helpers["get_json_or_none"]
    cli_error = helpers["CliError"]

    def cmd_score(args: argparse.Namespace) -> None:
        if args.record and not args.yes:
            raise cli_error("--record writes Offline Review events, which can gate models: it needs --yes")
        prompt_id, versions = _prompt_versions(args.prompt, cli_error)
        models = [model.strip() for model in args.models.split(",") if model.strip()]
        if not models:
            raise cli_error("--models needs at least one model id")
        body = {
            "judge_model_id": args.judge,
            "model_ids": models,
            "prompt_id": prompt_id,
            "versions": versions,
            "sample": args.sample,
            "record": args.record,
        }
        plan = fetch_json(args, "POST", "/evals/runs", body={**body, "dry_run": True})
        _print_plan(plan)
        if plan["estimate_aud"] > plan["remaining_aud"]:
            raise cli_error(
                "the estimate is more than today's evals spend cap leaves: try tomorrow, or raise it "
                "with `evals set --daily-spend-cap`"
            )
        if plan["estimate_aud"] > plan["score_max_cost_aud"] and not args.yes:
            raise cli_error("the estimate is more than score_max_cost_aud: run it again with --yes")
        if not args.yes:
            question = f"Start this run on {plan.get('environment') or 'this environment'}? [y/N] "
            try:
                sure = input(question).strip().lower() in ("y", "yes")
            except EOFError:
                sure = False
            if not sure:
                print("Not started.", file=sys.stderr)
                raise SystemExit(1)
        started = fetch_json(args, "POST", "/evals/runs", body={**body, "yes": args.yes})
        print(json.dumps(started, indent=2))
        if args.wait:
            _follow(args, started["run_id"], get_json_or_none)

    def cmd_runs_get(args: argparse.Namespace) -> None:
        path = f"/evals/runs/{quote(args.run_id, safe='')}"
        if not args.wait:
            do_request(args, "GET", path)
            return
        # Read once first, so a run that is not there stops here rather than being waited for.
        run = fetch_json(args, "GET", path)
        if run.get("status") not in SETTLED:
            run = _follow(args, args.run_id, get_json_or_none) or run
        print(json.dumps(run, indent=2))

    score_parser = evals_sub.add_parser(
        "score",
        help="Score models and prompt versions on past articles against the live version, judged by a "
        "model that is none of them; shows the estimate and asks first",
    )
    score_parser.add_argument(
        "--judge",
        required=True,
        metavar="MODEL_ID",
        help="The model that compares the drafts: none of --models",
    )
    score_parser.add_argument(
        "--models", required=True, metavar="MODEL_IDS", help="Comma-separated models to score, 1 to 5"
    )
    score_parser.add_argument(
        "--prompt",
        required=True,
        metavar="PROMPT_ID=VERSIONS",
        help="The prompt and its versions to score, e.g. daily_cycle/draft=v2,v3: each one this "
        "deployment published (live, or retired since), or vanilla (the copy in the code)",
    )
    score_parser.add_argument(
        "--sample", type=int, default=10, metavar="N", help="Past articles to score, 1 to 50 (default 10)"
    )
    score_parser.add_argument(
        "--record",
        action="store_true",
        default=False,
        help="Write each verdict as an Offline Review event, which counts towards the gates while the "
        "judge is calibrated (needs --yes)",
    )
    score_parser.add_argument(
        "--yes",
        action="store_true",
        default=False,
        help="Do not ask; needed over score_max_cost_aud and with --record",
    )
    score_parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        default=True,
        help="Return once the run is started instead of following its progress",
    )
    score_parser.set_defaults(func=cmd_score)

    runs_parser = evals_sub.add_parser(
        "runs", help="The judge's and the offline scoring runs: list, progress and results, cancel, resume"
    )
    runs_sub = runs_parser.add_subparsers(dest="runs_action", required=True)

    list_parser = runs_sub.add_parser("list", help="The newest runs, judge and scoring, with their status")
    list_parser.add_argument(
        "--limit", type=int, default=20, metavar="N", help="How many, 1 to 100 (default 20)"
    )
    list_parser.set_defaults(func=lambda args: do_request(args, "GET", f"/evals/runs?limit={args.limit}"))

    get_parser = runs_sub.add_parser(
        "get", help="One run's progress, read from its item rows, and each combination's results"
    )
    get_parser.add_argument("run_id")
    get_parser.add_argument(
        "--wait", action="store_true", default=False, help="Follow its progress until it finishes or pauses"
    )
    get_parser.set_defaults(func=cmd_runs_get)

    cancel_parser = runs_sub.add_parser(
        "cancel", help="Cancel a queued, running or paused run: its items still on the queue are dropped"
    )
    cancel_parser.add_argument("run_id")
    cancel_parser.set_defaults(
        func=lambda args: do_request(args, "DELETE", f"/evals/runs/{quote(args.run_id, safe='')}")
    )

    resume_parser = runs_sub.add_parser(
        "resume",
        help="Send a scoring run's items that have no result again (a paused or cancelled run's, or "
        "ones that failed), under today's spend cap",
    )
    resume_parser.add_argument("run_id")
    resume_parser.set_defaults(
        func=lambda args: do_request(
            args, "POST", f"/evals/runs/{quote(args.run_id, safe='')}/resume", body={}
        )
    )


def _register_trace(evals_sub: argparse._SubParsersAction, do_request) -> None:
    """`evals trace` (PR18c): one article's call traces, read back through the admin API."""
    trace_parser = evals_sub.add_parser(
        "trace",
        help="One article's call traces: each model call's prompt and reply as stored while trace_calls "
        "was on (the gear's guidance and the operator's note as their sha256), and those of the research "
        "behind it",
    )
    trace_parser.add_argument(
        "article_id", help="The article's id, or the id a run that stored no article traced its calls under"
    )
    trace_parser.set_defaults(
        func=lambda args: do_request(args, "GET", f"/evals/traces/{quote(args.article_id, safe='')}")
    )
