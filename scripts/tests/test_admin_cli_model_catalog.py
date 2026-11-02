"""The Admin CLI's model catalog commands: `models list`, `models reprice`, `models discover`,
`models classify` and `model-catalog get|set` (docs/enhancements/archive/model-catalog-refresh.md)."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import admin_cli


def cli(*argv):
    calls = []
    args = admin_cli.build_parser().parse_args(list(argv))

    def record(_args, method, path, body=None):
        calls.append((method, path, body))

    with patch.object(admin_cli, "_do_request", record):
        args.func(args)
    return calls


def test_the_cli_sends_each_setting_as_the_api_takes_it():
    [(method, path, body)] = cli(
        "model-catalog", "set", "--reprice", "off", "--discover", "", "--review-after-hours", "1, 3",
        "--max-web-lookups", "", "--review-tolerance-pct", "2", "--discover-max-added", "3",
        "--reprice-max-queued", "20", "--max-models", "",
    )
    assert (method, path) == ("PUT", "/model-catalog-config")
    assert body == {
        "reprice_enabled": False,
        "discover_enabled": None,
        "max_models": None,
        "discover_max_added": 3,
        "reprice_max_queued": 20,
        "review_tolerance_pct": 2,
        "max_web_lookups": None,
        "review_after_hours": [1, 3],
    }


def test_the_cli_starts_the_jobs_and_reads_the_settings():
    assert cli("models", "reprice") == [("POST", "/models/reprice", None)]
    assert cli("models", "discover") == [("POST", "/models/discover", None)]
    assert cli("models", "classify") == [("POST", "/models/classify", None)]
    assert cli("model-catalog", "get") == [("GET", "/model-catalog-config", None)]


def test_the_cli_refuses_a_set_with_nothing_in_it_or_a_bad_number():
    with pytest.raises(admin_cli.CliError):
        cli("model-catalog", "set")
    with pytest.raises(admin_cli.CliError):
        cli("model-catalog", "set", "--max-change-factor", "three")


class FakeResponse:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


REFUSED = FakeResponse(409, {"error": "x", "confirm_required": True, "roles": ["default"]})


def run_disable(argv, responses, answer=None):
    """Run a disabling command against canned API responses; returns the bodies POSTed."""
    sent = []
    args = admin_cli.build_parser().parse_args(argv)

    def fake_request(method, api_url, path, region, body=None):
        if method == "POST":
            sent.append(body)
        return responses.pop(0)

    with (
        patch.object(admin_cli, "signed_request", fake_request),
        patch.object(admin_cli, "_resolve_api_url", lambda a: "https://api"),
        patch.object(admin_cli, "_resolve_region", lambda a: "ap-southeast-2"),
        patch("builtins.input", lambda prompt: answer),
    ):
        args.func(args)
    return sent


ROW = {
    "model_id": "main",
    "display_name": "Main",
    "provider": "anthropic",
    "input_price_usd_per_1k_tokens": 0.001,
    "output_price_usd_per_1k_tokens": 0.005,
    "enabled": True,
}


def test_disabling_the_default_asks_first_and_resends_confirmed_when_sure():
    listed = FakeResponse(200, {"models": [ROW]})
    sent = run_disable(
        ["models", "disable", "--model-id", "main"], [listed, REFUSED, FakeResponse(200, {})], answer="y"
    )
    assert [body.get("confirm") for body in sent] == [None, True]
    assert sent[0]["enabled"] is False
    assert sent[0]["input_price_usd_per_1k_tokens"] == 0.001


def test_disabling_the_default_stops_when_not_sure_and_yes_skips_the_question():
    listed = FakeResponse(200, {"models": [ROW]})
    with pytest.raises(SystemExit):
        run_disable(["models", "disable", "--model-id", "main"], [listed, REFUSED], answer="n")
    sent = run_disable(
        ["models", "add", "--model-id", "main", "--display-name", "Main", "--provider", "a",
         "--input-price", "0.001", "--output-price", "0.005", "--disabled", "--yes"],
        [REFUSED, FakeResponse(200, {})],
    )
    assert sent[1]["confirm"] is True


def test_disable_refuses_a_model_that_is_not_registered():
    with pytest.raises(admin_cli.CliError):
        run_disable(["models", "disable", "--model-id", "nope"], [FakeResponse(200, {"models": [ROW]})])


LISTING = {
    "models": [
        {
            "model_id": "words",
            "model_purpose": "text",
            "model_inputs": ["TEXT", "IMAGE"],
            "enabled": True,
            "access": "granted",
            "input_price_usd_per_1k_tokens": 0.001,
            "output_price_usd_per_1k_tokens": 0.005,
        },
        {"model_id": "embed", "model_purpose": "embedding", "enabled": False},
        {
            "model_id": "older",
            "input_price_usd_per_1k_tokens": 0.002,
            "output_price_usd_per_1k_tokens": 0.008,
        },
    ]
}


def run_list(argv, listing):
    """Run a listing command against a canned GET /models answer."""
    args = admin_cli.build_parser().parse_args(argv)
    with (
        patch.object(admin_cli, "signed_request", lambda *a, **k: FakeResponse(200, listing)),
        patch.object(admin_cli, "_resolve_api_url", lambda a: "https://api"),
        patch.object(admin_cli, "_resolve_region", lambda a: "ap-southeast-2"),
    ):
        args.func(args)


def test_models_list_is_a_table_with_each_models_purpose(capsys):
    run_list(["models", "list"], LISTING)

    out = capsys.readouterr().out
    lines = out.splitlines()
    assert lines[0].split() == ["MODEL", "ID", "PURPOSE", "ENABLED", "ACCESS", "IN", "$/1K", "OUT", "$/1K"]
    assert lines[1].split() == ["words", "text", "yes", "granted", "0.00100", "0.00500"]
    assert lines[2].split() == ["embed", "embedding", "no"]
    assert lines[3].split() == ["older", "unclassified", "yes", "0.00200", "0.00800"]
    assert lines[1].index("text") == lines[3].index("unclassified")  # one column, padded
    assert "`models classify`" in out  # how to class the older row


def test_models_list_says_when_the_registry_is_empty_and_prints_json_when_asked(capsys):
    run_list(["models", "list"], {"models": []})
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("MODEL ID") and "no models are registered" in out

    run_list(["models", "list", "--json"], LISTING)
    assert json.loads(capsys.readouterr().out) == LISTING
