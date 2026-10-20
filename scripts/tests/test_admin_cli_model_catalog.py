"""The Admin CLI's model catalog commands: `models reprice`, `models discover` and `model-catalog
get|set` (docs/enhancements/model-catalog-refresh.md)."""

from __future__ import annotations

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
        "--max-web-lookups", "", "--review-tolerance-pct", "2",
    )
    assert (method, path) == ("PUT", "/model-catalog-config")
    assert body == {
        "reprice_enabled": False,
        "discover_enabled": None,
        "review_tolerance_pct": 2,
        "max_web_lookups": None,
        "review_after_hours": [1, 3],
    }


def test_the_cli_starts_the_jobs_and_reads_the_settings():
    assert cli("models", "reprice") == [("POST", "/models/reprice", None)]
    assert cli("models", "discover") == [("POST", "/models/discover", None)]
    assert cli("model-catalog", "get") == [("GET", "/model-catalog-config", None)]


def test_the_cli_refuses_a_set_with_nothing_in_it_or_a_bad_number():
    with pytest.raises(admin_cli.CliError):
        cli("model-catalog", "set")
    with pytest.raises(admin_cli.CliError):
        cli("model-catalog", "set", "--max-change-factor", "three")
