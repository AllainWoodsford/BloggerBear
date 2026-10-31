"""The Admin CLI's eval commands (scripts/admin_cli_evals.py): wired into admin_cli's parser by
`register` (docs/enhancements/model-ranch-prompt-assets-evals.md, E0), `evals get|set|rollup|
status|digest` (PR13), `evals forgive|punish|ungate|revoke` (PR13b) and `evals cull` (PR16), each
sending what its Admin API route takes."""

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
    "excluded_sources",
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
    assert helpers == {
        "do_request": admin_cli._do_request,
        "fetch_json": admin_cli._fetch_json,
        "CliError": admin_cli.CliError,
    }


def test_register_adds_the_evals_group_and_its_commands():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="resource")
    admin_cli_evals.register(subparsers, {"do_request": None, "fetch_json": None, "CliError": Exception})
    assert set(subparsers.choices) == {"evals"}
    commands = _subparsers(subparsers.choices["evals"]).choices
    assert set(commands) == {
        "get", "set", "rollup", "status", "digest", "forgive", "punish", "ungate", "revoke", "cull",
    }  # fmt: skip


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
