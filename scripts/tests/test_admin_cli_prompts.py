"""The Admin CLI's prompt commands (scripts/admin_cli_prompts.py): `prompts list|pause|resume|releases|set`,
wired into admin_cli's parser by `register` with admin_cli's helpers lent to it
(docs/enhancements/model-ranch-prompt-assets-evals.md, P0 and PR4)."""

from __future__ import annotations

import argparse
import json
from unittest.mock import patch

import pytest

import admin_cli
import admin_cli_prompts


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    return next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))


def cli(*argv):
    """Run one command with the signed request recorded instead of sent: (method, path, body)."""
    calls = []
    args = admin_cli.build_parser().parse_args(list(argv))

    def record(_args, method, path, body=None):
        calls.append((method, path, body))

    with patch.object(admin_cli, "_do_request", record):
        args.func(args)
    return calls


def table(capsys, *argv, answer):
    """Run a command that prints a table, with the API answering `answer`; (calls, printed)."""
    calls = []
    args = admin_cli.build_parser().parse_args(list(argv))

    def fetch(_args, method, path, body=None):
        calls.append((method, path, body))
        return answer

    with patch.object(admin_cli, "_fetch_json", fetch), patch.object(admin_cli, "_do_request") as raw:
        args.func(args)
    raw.assert_not_called()
    return calls, capsys.readouterr().out


# --- the wiring ------------------------------------------------------------------------------------


def test_build_parser_hands_the_prompt_module_its_subparsers_and_helpers():
    with patch.object(admin_cli_prompts, "register") as register:
        parser = admin_cli.build_parser()
    register.assert_called_once()
    subparsers, helpers = register.call_args.args
    assert subparsers is _subparsers(parser)
    assert set(helpers) == {"do_request", "fetch_json", "print_table", "CliError"}
    assert helpers["CliError"] is admin_cli.CliError


def test_the_lent_helpers_are_admin_clis_own_looked_up_when_a_command_runs():
    """Lent by name and looked up at each call, so patching admin_cli's helper reaches the prompt
    commands too (the pattern every CLI test here uses)."""
    helpers = admin_cli._prompt_helpers()
    for name, attribute in (("do_request", "_do_request"), ("fetch_json", "_fetch_json"),
                            ("print_table", "_print_table")):  # fmt: skip
        with patch.object(admin_cli, attribute, return_value="patched") as patched:
            assert helpers[name]("a", "b", body={"c": 1}) == "patched"
        patched.assert_called_once_with("a", "b", body={"c": 1})


def test_the_prompts_group_has_its_five_commands():
    group = _subparsers(admin_cli.build_parser()).choices["prompts"]
    assert set(_subparsers(group).choices) == {"list", "pause", "resume", "releases", "set"}


# --- what each command sends -----------------------------------------------------------------------


def test_pause_and_resume_send_the_prompt_and_its_version():
    assert cli("prompts", "pause", "daily_cycle/draft", "v2") == [
        ("POST", "/prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    ]
    assert cli("prompts", "resume", "daily_cycle/draft", "v2") == [
        ("POST", "/prompts/resume", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    ]


def test_set_sends_the_kill_switch_and_an_empty_value_clears_it():
    assert cli("prompts", "set", "--assets", "off") == [("PUT", "/prompts/config", {"prompt_assets": "off"})]
    assert cli("prompts", "set", "--assets", "on") == [("PUT", "/prompts/config", {"prompt_assets": "on"})]
    assert cli("prompts", "set", "--assets", "") == [("PUT", "/prompts/config", {"prompt_assets": None})]


def test_set_with_nothing_to_set_is_refused_before_any_request():
    with pytest.raises(admin_cli.CliError, match="--assets"):
        cli("prompts", "set")


def test_set_refuses_a_value_that_is_not_on_or_off():
    with pytest.raises(SystemExit):
        admin_cli.build_parser().parse_args(["prompts", "set", "--assets", "maybe"])


def test_json_prints_the_raw_answer_through_the_signed_request():
    assert cli("prompts", "list", "--json") == [("GET", "/prompts", None)]
    assert cli("prompts", "releases", "--json") == [("GET", "/prompts/releases", None)]
    limited = cli("prompts", "releases", "--json", "--limit", "5")
    assert limited == [("GET", "/prompts/releases?limit=5", None)]


# --- the tables ------------------------------------------------------------------------------------

LISTING = {
    "serving": False,
    "serving_note": "PROMPT_AS_ASSET_ENABLED is off in this deployment: every prompt is the code's copy.",
    "deployment_switch": "off",
    "bucket": "acme-blog-dev-prompts",
    "effective": {"prompt_assets": "on", "paused_versions": ["daily_cycle/draft@v2"]},
    "prompts": [
        {
            "prompt_id": "daily_cycle/draft",
            "asset": True,
            "vanilla_version": "vanilla:v1",
            "files": [
                {
                    "key": "daily_cycle/draft/latest/v2.txt",
                    "version": "v2",
                    "release": "dev-20261026T0300-abc1234",
                    "sha256": "0123456789abcdef" * 4,
                    "hash_status": "live",
                    "retired_at": None,
                    "paused": True,
                },
                {
                    "key": "daily_cycle/draft/latest/v3.txt",
                    "version": "v3",
                    "release": "dev-20261020T0300-0000000",
                    "sha256": "fedcba9876543210" * 4,
                    "hash_status": "retired",
                    "retired_at": "2026-10-26T03:00:00+00:00",
                    "paused": False,
                },
            ],
        },
        {
            "prompt_id": "reviews/compliance",
            "asset": False,
            "vanilla_version": "vanilla:v3",
            "files": [],
            "note": "code-only: always the copy in the code, never read from the bucket",
        },
        {"prompt_id": "daily_cycle/title", "asset": True, "vanilla_version": "vanilla:v1", "files": [],
         "error": "could not list latest/"},  # fmt: skip
    ],
}


def test_list_prints_whether_assets_are_served_then_one_row_per_file(capsys):
    calls, printed = table(capsys, "prompts", "list", answer=LISTING)

    assert calls == [("GET", "/prompts", None)]
    lines = printed.splitlines()
    assert lines[0] == f"Serving assets: no. {LISTING['serving_note']}"
    assert "Deployment switch (PROMPT_AS_ASSET_ENABLED): off" in lines[1]
    assert "kill switch (prompt_assets): on" in lines[1] and "bucket: acme-blog-dev-prompts" in lines[1]
    assert lines[2] == "Paused: daily_cycle/draft@v2"
    header = next(line for line in lines if line.startswith("PROMPT"))
    assert header.split() == ["PROMPT", "VERSION", "RELEASE", "SHA256", "HASH", "PAUSED", "NOTE"]
    rows = lines[lines.index(header) + 1 :]
    assert rows[0].split() == ["daily_cycle/draft", "v2", "dev-20261026T0300-abc1234", "0123456789ab", "live",
                               "paused"]  # fmt: skip
    assert rows[1].split()[:6] == ["daily_cycle/draft", "v3", "dev-20261020T0300-0000000", "fedcba987654",
                                   "retired", "2026-10-26T03:00:00+00:00"]  # fmt: skip
    assert "vanilla:v3 (code)" in rows[2] and "code-only" in rows[2]
    assert "vanilla:v1 (code)" in rows[3] and "could not list latest/" in rows[3]
    # The columns line up: every row's VERSION starts where the header's does.
    column = header.index("VERSION")
    assert all(row[column - 2 : column] == "  " and row[column] != " " for row in rows)


def test_list_with_no_prompt_registered_says_so(capsys):
    _, printed = table(capsys, "prompts", "list", answer={**LISTING, "prompts": []})
    assert "No prompt is registered yet" in printed and "PROMPT " not in printed


def test_releases_prints_the_newest_first_with_the_one_in_place(capsys):
    answer = {
        "bucket": "acme-blog-dev-prompts",
        "total": 3,
        "releases": [
            {"release_id": "dev-20261026T0300-abc1234", "written_at": "2026-10-26T03:00:00+00:00",
             "current": True, "pinned": False},
            {"release_id": "dev-20261020T0300-0000000", "written_at": "2026-10-20T03:00:00+00:00",
             "current": False, "pinned": True},
        ],
        "current": {"release_id": "dev-20261026T0300-abc1234", "pinned_release": "dev-20261020T0300-0000000"},
    }  # fmt: skip
    calls, printed = table(capsys, "prompts", "releases", "--limit", "2", answer=answer)

    assert calls == [("GET", "/prompts/releases?limit=2", None)]
    lines = printed.splitlines()
    assert lines[0] == "2 of 3 releases, newest first:"
    assert lines[1].split() == ["RELEASE", "WRITTEN", "STATE"]
    assert lines[2].split() == ["dev-20261026T0300-abc1234", "2026-10-26T03:00:00+00:00", "current"]
    assert lines[3].split() == ["dev-20261020T0300-0000000", "2026-10-20T03:00:00+00:00", "pinned"]


def test_releases_with_nothing_published_prints_the_apis_note_and_no_table(capsys):
    answer = {"bucket": None, "releases": [], "total": 0, "current": None, "note": "No prompts bucket here."}
    _, printed = table(capsys, "prompts", "releases", answer=answer)
    assert printed.splitlines() == ["No prompts bucket here."]


def test_print_table_aligns_every_column_and_shows_none_as_a_dash(capsys):
    admin_cli._print_table(["A", "LONGER"], [["wide cell", None], ["x", "y"]])
    assert capsys.readouterr().out.splitlines() == ["A          LONGER", "wide cell  -", "x          y"]


def test_print_table_with_no_rows_prints_its_headers():
    with patch("builtins.print") as printed:
        admin_cli._print_table(["A", "B"], [])
    printed.assert_called_once_with("A  B")


def test_a_failed_answer_is_printed_and_exits_like_every_other_command(capsys):
    class Refused:
        status_code = 502

        def json(self):
            return {"error": "could not list releases/ in the prompts bucket"}

        text = ""

    args = admin_cli.build_parser().parse_args(["prompts", "releases"])
    with (
        patch.object(admin_cli, "signed_request", return_value=Refused()),
        patch.object(admin_cli, "_resolve_api_url", return_value="https://api"),
        patch.object(admin_cli, "_resolve_region", return_value="ap-southeast-2"),
        pytest.raises(SystemExit) as exited,
    ):
        args.func(args)
    assert exited.value.code == 1
    assert json.loads(capsys.readouterr().err.split("\n", 1)[1]) == Refused().json()
