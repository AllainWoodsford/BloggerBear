"""The Admin CLI's eval commands (scripts/admin_cli_evals.py): wired into admin_cli's parser by
`register` (docs/enhancements/model-ranch-prompt-assets-evals.md, E0), `evals get|set|rollup|
status|digest` (PR13), `evals forgive|punish|ungate|revoke` (PR13b) and `evals cull` (PR16), each
sending what its Admin API route takes, and `evals score` and `evals runs list|get|cancel|resume`
(PR18b): a run is priced and shown with its environment first, asked about before it starts (over
score_max_cost_aud, or with --record, only --yes starts it), and followed until it settles."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

import admin_cli
import admin_cli_evals

# Section 4.5's settings, by kind. Written out: scripts/tests also run on their own, with only scripts/
# on the path (scripts/pyproject.toml), so common/evals_settings.py cannot be imported here.
DYNAMODB_ONLY = {
    "daily_gate", "weekly_gate", "total_gate", "min_events", "judge_min_agreement", "judge_min_pairs",
}  # fmt: skip
CLI_SETTINGS = {
    "cull_enabled", "cull_max_per_run", "judge_enabled", "judge_interval_hours", "judge_max_items",
    "judge_model_id", "score_max_cost_aud", "evals_daily_spend_cap_aud", "trace_calls", "timezone",
    "excluded_sources", "digest_email",
}  # fmt: skip


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def cli(*argv):
    """Run `admin_cli.py evals ...` against a recording stand-in for the signed request. The stand-in
    is in place before the parser is built, as the eval commands are handed admin_cli's helper then."""
    calls = []

    def record(_args, method, path, body=None):
        calls.append((method, path, body))

    with patch.object(admin_cli, "_do_request", record):
        args = admin_cli.build_parser().parse_args(["evals", *argv])
        args.func(args)
    return calls


def test_build_parser_hands_the_eval_module_its_subparsers_and_helpers():
    with patch.object(admin_cli_evals, "register") as register:
        parser = admin_cli.build_parser()
    register.assert_called_once()
    subparsers, helpers = register.call_args.args
    assert subparsers is _subparsers(parser)
    assert set(helpers) == {"do_request", "fetch_json", "get_json_or_none", "CliError"}
    assert helpers["CliError"] is admin_cli.CliError
    # Each request helper is admin_cli's own, looked up when it is called (so a patch reaches it).
    for name, target in [
        ("do_request", "_do_request"),
        ("fetch_json", "_fetch_json"),
        ("get_json_or_none", "_get_json_or_none"),
    ]:
        with patch.object(admin_cli, target, return_value="answer") as helper:
            assert helpers[name]("args", "GET", "/x") == "answer"
        helper.assert_called_once_with("args", "GET", "/x")


def test_register_adds_the_evals_group_and_its_commands():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="resource")
    helpers = {"do_request": None, "fetch_json": None, "get_json_or_none": None, "CliError": Exception}
    admin_cli_evals.register(subparsers, helpers)
    assert set(subparsers.choices) == {"evals"}
    commands = _subparsers(subparsers.choices["evals"]).choices
    assert set(commands) == {
        "get", "set", "rollup", "status", "digest", "forgive", "punish", "ungate", "revoke", "cull",
        "score", "runs", "trace",
    }  # fmt: skip
    assert set(_subparsers(commands["runs"]).choices) == {"list", "get", "cancel", "resume"}


def test_admin_cli_imports_the_eval_module_only_inside_build_parser():
    """At the top of admin_cli, the import would run while admin_cli itself is half loaded, and
    the commands that need its helpers could not have them: the lazy import is the cycle's guard,
    so it stays inside the function."""
    source = admin_cli.__loader__.get_source(admin_cli.__name__)
    top_level = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in top_level if "admin_cli_evals" in line]
    body = source.split("def build_parser() -> argparse.ArgumentParser:", 1)[1].split("\ndef ", 1)[0]
    assert "    from admin_cli_evals import register as register_eval_commands\n" in body


def test_get_status_and_digest_read_their_routes():
    assert cli("get") == [("GET", "/evals-config", None)]
    assert cli("status") == [("GET", "/evals/status", None)]
    assert cli("digest") == [("GET", "/evals/digest", None)]
    assert cli("digest", "--since", "2026-10-20") == [("GET", "/evals/digest?since=2026-10-20", None)]
    # An ISO time's `+` and `:` are sent encoded, so API Gateway does not read the `+` as a space.
    assert cli("digest", "--since", "2026-10-20T00:00:00+11:00") == [
        ("GET", "/evals/digest?since=2026-10-20T00%3A00%3A00%2B11%3A00", None)
    ]


def test_set_sends_each_setting_as_the_api_takes_it():
    [(method, path, body)] = cli(
        "set", "--cull", "on", "--judge", "", "--judge-interval-hours", "6", "--judge-max-items", "",
        "--judge-model-id", "maker.judge-v1:0", "--score-max-cost", "2.5", "--daily-spend-cap", "3",
        "--trace-calls", "off", "--timezone", "UTC", "--excluded-sources", "judge, run:r-1,,",
        "--cull-max-per-run", "4",
    )  # fmt: skip
    assert (method, path) == ("PUT", "/evals-config")
    assert body == {
        "cull_enabled": True,
        "cull_max_per_run": 4,
        "judge_enabled": None,
        "judge_interval_hours": 6,
        "judge_max_items": None,
        "judge_model_id": "maker.judge-v1:0",
        "score_max_cost_aud": 2.5,
        "evals_daily_spend_cap_aud": 3.0,
        "trace_calls": False,
        "timezone": "UTC",
        "excluded_sources": ["judge", "run:r-1"],
    }


def test_set_switches_the_friday_digest_email_on_and_off():
    assert cli("set", "--digest-email", "on") == [("PUT", "/evals-config", {"digest_email": True})]
    assert cli("set", "--digest-email", "off") == [("PUT", "/evals-config", {"digest_email": False})]
    assert cli("set", "--digest-email", "") == [("PUT", "/evals-config", {"digest_email": None})]


def test_set_clears_with_an_empty_value_and_has_no_flag_for_the_dynamodb_only_settings():
    assert cli("set", "--excluded-sources", "", "--judge-model-id", "") == [
        ("PUT", "/evals-config", {"excluded_sources": None, "judge_model_id": None})
    ]
    flags = {flag for action in _set_parser()._actions for flag in action.option_strings}
    for name in DYNAMODB_ONLY:
        assert "--" + name.replace("_", "-") not in flags
    # Every other setting has its flag.
    assert {setting for _, setting, _, _ in admin_cli_evals.SET_FLAGS} == CLI_SETTINGS


def _set_parser() -> argparse.ArgumentParser:
    evals = _subparsers(admin_cli.build_parser()).choices["evals"]
    return _subparsers(evals).choices["set"]


def test_set_refuses_nothing_to_set_and_a_number_that_is_not_one():
    with pytest.raises(admin_cli.CliError):
        cli("set")
    with pytest.raises(admin_cli.CliError):
        cli("set", "--judge-max-items", "ten")
    with pytest.raises(admin_cli.CliError):
        cli("set", "--daily-spend-cap", "two dollars")


def test_rollup_runs_from_the_watermark_or_for_the_days_named():
    assert cli("rollup") == [("POST", "/evals/rollup", {})]
    assert cli("rollup", "--from", "2026-10-01") == [("POST", "/evals/rollup", {"from": "2026-10-01"})]
    assert cli("rollup", "--from", "2026-10-01", "--to", "2026-10-03") == [
        ("POST", "/evals/rollup", {"from": "2026-10-01", "to": "2026-10-03"})
    ]
    with pytest.raises(admin_cli.CliError):
        cli("rollup", "--to", "2026-10-03")


# --- forgive, punish, ungate, revoke --------------------------------------------------------------


def test_forgive_and_punish_send_a_models_adjustment():
    assert cli("forgive", "--model", "maker.alpha-v1:0", "--amount", "30") == [
        (
            "POST",
            "/evals/adjust",
            {
                "target": "model", "model_id": "maker.alpha-v1:0", "direction": "forgive", "amount": 30,
                "allow_cull": False,
            },
        )
    ]  # fmt: skip
    [(_, _, body)] = cli("punish", "--model", "maker.alpha-v1:0", "--amount", "250", "--allow-cull")
    assert (body["direction"], body["amount"], body["allow_cull"]) == ("punish", 250, True)


def test_forgive_and_punish_send_a_prompt_versions_adjustment():
    assert cli("punish", "--prompt", "daily_cycle/draft", "--version", "v2", "--amount", "5") == [
        (
            "POST",
            "/evals/adjust",
            {
                "target": "prompt", "prompt_id": "daily_cycle/draft", "version": "v2", "direction": "punish",
                "amount": 5, "allow_cull": False,
            },
        )
    ]  # fmt: skip


@pytest.mark.parametrize(
    "argv",
    [
        ["--amount", "5"],
        ["--model", "maker.alpha-v1:0", "--prompt", "daily_cycle/draft", "--version", "v2", "--amount", "5"],
        ["--prompt", "daily_cycle/draft", "--amount", "5"],
        ["--model", "maker.alpha-v1:0", "--version", "v2", "--amount", "5"],
    ],
)
def test_an_adjustment_names_exactly_one_model_or_one_prompt_version(argv):
    with pytest.raises(admin_cli.CliError):
        cli("forgive", *argv)


def test_an_adjustment_needs_a_whole_amount():
    with pytest.raises(SystemExit):
        cli("punish", "--model", "maker.alpha-v1:0")
    with pytest.raises(SystemExit):
        cli("punish", "--model", "maker.alpha-v1:0", "--amount", "lots")


def test_ungate_holds_a_models_gates_open_for_some_hours():
    assert cli("ungate", "--model", "maker.alpha-v1:0", "--hours", "48") == [
        ("POST", "/evals/ungate", {"model_id": "maker.alpha-v1:0", "hours": 48})
    ]
    assert cli("ungate", "--model", "maker.alpha-v1:0", "--hours", "0")[0][2]["hours"] == 0
    with pytest.raises(SystemExit):
        cli("ungate", "--model", "maker.alpha-v1:0")


def test_revoke_sends_the_source_and_its_time_when_given():
    assert cli("revoke", "--source", "judge", "--since", "2026-10-27T00:00:00Z") == [
        ("POST", "/evals/revoke", {"source": "judge", "since": "2026-10-27T00:00:00Z"})
    ]
    assert cli("revoke", "--source", "run:r-1") == [("POST", "/evals/revoke", {"source": "run:r-1"})]
    with pytest.raises(SystemExit):
        cli("revoke")


# --- cull -----------------------------------------------------------------------------------------

PROPOSAL = {
    "proposed_at": "2026-10-23T04:00:00+00:00",
    "expires_at": "2026-10-29T17:00:00+00:00",
    "models": [
        {"model_id": "maker.worst-v1:0", "total_perf": -5000, "events": 40},
        {"model_id": "maker.poor-v1:0", "total_perf": -1200, "events": 12},
    ],
}


def cull(*argv, status=None, answer=None):
    """Run `evals cull ...` against a GET /evals/status answering `status` (its `cull` part) and a
    recording stand-in for the POST, answering the question with `answer`. (calls, what it printed)."""
    calls = []

    def record(_args, method, path, body=None):
        calls.append((method, path, body))

    def fetch(_args, method, path, body=None):
        calls.append((method, path, body))
        return {"cull": status} if status is not None else {}

    with patch.object(admin_cli, "_do_request", record), patch.object(admin_cli, "_fetch_json", fetch), patch(
        "builtins.input", side_effect=[answer] if answer is not None else EOFError
    ) as asked:
        args = admin_cli.build_parser().parse_args(["evals", "cull", *argv])
        args.func(args)
    return calls, asked


def test_cull_shows_the_proposal_and_the_last_cull_and_deletes_nothing(capsys):
    part = {"proposal": PROPOSAL, "last_apply": None, "expired": False}
    calls, asked = cull(status=part)
    assert calls == [("GET", "/evals/status", None)]
    assert json.loads(capsys.readouterr().out) == part and not asked.called


def test_cull_apply_shows_the_models_and_asks_before_it_sends_anything(capsys):
    calls, asked = cull("--apply", status={"proposal": PROPOSAL, "expired": False}, answer="y")

    assert calls == [("GET", "/evals/status", None), ("POST", "/evals/cull/apply", {})]
    shown = capsys.readouterr().out
    assert "maker.worst-v1:0: total -5000 over 40 events" in shown and "maker.poor-v1:0" in shown
    assert "[y/N]" in asked.call_args.args[0] and "`models restore <id>`" in asked.call_args.args[0]


@pytest.mark.parametrize("answer", ["n", "", None])
def test_cull_apply_sends_nothing_unless_the_answer_is_yes(capsys, answer):
    with pytest.raises(SystemExit) as stopped:
        cull("--apply", status={"proposal": PROPOSAL, "expired": False}, answer=answer)
    assert stopped.value.code == 1
    assert "Nothing was culled." in capsys.readouterr().err


def test_cull_apply_with_yes_sends_at_once_without_showing_or_asking():
    calls, asked = cull("--apply", "--yes")
    assert calls == [("POST", "/evals/cull/apply", {})] and not asked.called


@pytest.mark.parametrize(
    ("status", "said"),
    [
        ({"proposal": None, "detail": "no cull proposal yet"}, "there is no cull proposal"),
        ({"proposal": {**PROPOSAL, "models": []}, "expired": False}, "there is no cull proposal"),
        ({"proposal": PROPOSAL, "expired": True}, "expired at 2026-10-29T17:00:00"),
        ({"status": "could not check", "error": "RuntimeError"}, "could not read the cull proposal"),
    ],
)
def test_cull_apply_asks_nothing_and_sends_nothing_when_there_is_nothing_to_apply(status, said):
    with pytest.raises(admin_cli.CliError, match=said):
        cull("--apply", status=status, answer="y")


def test_cull_yes_goes_with_apply():
    with pytest.raises(admin_cli.CliError, match="--yes goes with --apply"):
        cull("--yes")


# --- offline scoring runs (`evals score`, `evals runs ...`) --------------------------------------------

PLAN = {
    "dry_run": True,
    "environment": "production",
    "prompt_id": "daily_cycle/draft",
    "judge_model_id": "maker.judge-v1:0",
    "combinations": ["maker.a-v1:0@daily_cycle/draft=v2", "maker.a-v1:0@daily_cycle/draft=vanilla:v1"],
    "sample": 2,
    "items": 2,
    "messages": 4,
    "estimate_aud": 0.51,
    "remaining_aud": 2.0,
    "score_max_cost_aud": 5,
    "record": False,
}
SCORE = ["score", "--judge", "maker.judge-v1:0", "--models", "maker.a-v1:0", "--prompt",
         "daily_cycle/draft=v2,vanilla", "--sample", "2"]  # fmt: skip
ASKED = {
    "judge_model_id": "maker.judge-v1:0",
    "model_ids": ["maker.a-v1:0"],
    "prompt_id": "daily_cycle/draft",
    "versions": ["v2", "vanilla"],
    "sample": 2,
    "record": False,
}


def run_cli(*argv, plan=None, answer="y", reads=()):
    """`admin_cli.py evals ...` against stand-ins for the signed calls (each recorded as it is sent),
    the y/N question and the clock. The calls, and the question's stand-in."""
    sent = []
    reads = list(reads)

    def fetch(_args, method, path, body=None):
        sent.append((method, path, body))
        if method == "GET":
            return reads.pop(0) if reads else {"run_id": "score-1", "status": "running", "progress": {}}
        return {**PLAN, **(plan or {})} if body and body.get("dry_run") else {"run_id": "score-1", "sent": 4}

    def poll(_args, path):
        sent.append(("GET", path, None))
        return reads.pop(0) if reads else {"run_id": "score-1", "status": "done", "progress": {}}

    def request(_args, method, path, body=None):
        sent.append((method, path, body))

    with (
        patch.object(admin_cli, "_fetch_json", fetch),
        patch.object(admin_cli, "_get_json_or_none", poll),
        patch.object(admin_cli, "_do_request", request),
        patch("builtins.input", return_value=answer) as asked,
        patch.object(admin_cli_evals.time, "sleep"),
    ):
        args = admin_cli.build_parser().parse_args(["evals", *argv])
        args.func(args)
    return sent, asked


def test_score_prices_the_run_shows_it_with_its_environment_and_asks_before_it_starts(capsys):
    sent, asked = run_cli(*SCORE, "--no-wait")

    assert sent == [
        ("POST", "/evals/runs", {**ASKED, "dry_run": True}),
        ("POST", "/evals/runs", {**ASKED, "yes": False}),
    ]
    asked.assert_called_once_with("Start this run on production? [y/N] ")
    out = capsys.readouterr().out
    assert "Environment:   production" in out and "Estimate:      about A$0.5100" in out
    assert "maker.a-v1:0@daily_cycle/draft=vanilla:v1" in out and '"run_id": "score-1"' in out


def test_score_starts_nothing_when_the_answer_is_not_yes(capsys):
    with pytest.raises(SystemExit) as stopped:
        run_cli(*SCORE, answer="n")
    assert stopped.value.code == 1
    assert "Not started." in capsys.readouterr().err


def test_record_needs_yes_before_anything_is_sent():
    with pytest.raises(admin_cli.CliError) as refused:
        run_cli(*SCORE, "--record")
    assert "--record" in str(refused.value) and "--yes" in str(refused.value)


def test_over_score_max_cost_only_yes_starts_the_run():
    with pytest.raises(admin_cli.CliError) as refused:
        run_cli(*SCORE, plan={"estimate_aud": 6.0, "remaining_aud": 10.0})
    assert "score_max_cost_aud" in str(refused.value)

    over = {"estimate_aud": 6.0, "remaining_aud": 10.0}
    sent, asked = run_cli(*SCORE, "--yes", "--record", "--no-wait", plan=over)
    assert [body.get("yes") for _, _, body in sent] == [None, True]
    assert sent[1][2]["record"] is True
    asked.assert_not_called()


def test_over_what_today_leaves_the_run_is_refused_after_its_plan_is_shown(capsys):
    with pytest.raises(admin_cli.CliError) as refused:
        run_cli(*SCORE, "--yes", plan={"estimate_aud": 1.5, "remaining_aud": 0.2})
    assert "daily-spend-cap" in str(refused.value)
    assert "Environment:" in capsys.readouterr().out


@pytest.mark.parametrize("prompt", ["daily_cycle/draft", "=v2", "daily_cycle/draft=", "daily_cycle/draft=,"])
def test_a_prompt_flag_without_its_versions_is_refused(prompt):
    argv = [*SCORE[:5], "--prompt", prompt]
    with pytest.raises(admin_cli.CliError):
        run_cli(*argv)


def test_score_follows_the_run_until_it_settles(capsys):
    running = {"run_id": "score-1", "status": "running", "spent_aud": 0.01,
               "progress": {"messages": 4, "results": 1, "scoring": 1}}  # fmt: skip
    done = {**running, "status": "done", "progress": {"messages": 4, "results": 4, "scoring": 0}}
    sent, _ = run_cli(*SCORE, reads=[None, running, running, done])

    assert sent[2:] == [("GET", "/evals/runs/score-1", None)] * 4
    err = capsys.readouterr().err
    assert err.count("run score-1: running, 1 of 4 with a result (1 being scored)") == 1
    assert "run score-1: done, 4 of 4 with a result" in err


def test_a_run_paused_by_the_spend_cap_stops_the_following_and_says_how_it_goes_on(capsys):
    running = {"run_id": "score-1", "status": "running", "progress": {}}
    paused = {"run_id": "score-1", "status": "paused: spend cap", "progress": {}}
    sent, _ = run_cli("runs", "get", "score-1", "--wait", reads=[running, paused])
    assert sent == [("GET", "/evals/runs/score-1", None)] * 2
    captured = capsys.readouterr()
    assert "evals runs resume score-1" in captured.err
    assert '"status": "paused: spend cap"' in captured.out


def test_runs_get_wait_on_a_settled_run_prints_it_without_following():
    done = {"run_id": "score-1", "status": "done", "progress": {}}
    sent, _ = run_cli("runs", "get", "score-1", "--wait", reads=[done])
    assert sent == [("GET", "/evals/runs/score-1", None)]


def test_the_runs_commands_send_what_their_routes_take():
    assert run_cli("runs", "list")[0] == [("GET", "/evals/runs?limit=20", None)]
    assert run_cli("runs", "list", "--limit", "5")[0] == [("GET", "/evals/runs?limit=5", None)]
    assert run_cli("runs", "get", "score-1")[0] == [("GET", "/evals/runs/score-1", None)]
    assert run_cli("runs", "cancel", "score-1")[0] == [("DELETE", "/evals/runs/score-1", None)]
    assert run_cli("runs", "resume", "score-1")[0] == [("POST", "/evals/runs/score-1/resume", {})]


def test_trace_reads_one_articles_call_traces():
    """PR18c: GET /evals/traces/{article_id}, the id quoted so nothing in it can name another path."""
    assert cli("trace", "0b7e4c1e-5d3a-4f8e-9c2b-1a2b3c4d5e6f") == [
        ("GET", "/evals/traces/0b7e4c1e-5d3a-4f8e-9c2b-1a2b3c4d5e6f", None)
    ]
    assert cli("trace", "a/../b") == [("GET", "/evals/traces/a%2F..%2Fb", None)]
