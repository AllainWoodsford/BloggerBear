from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from botocore.credentials import Credentials

import admin_cli


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BLOGGERBEAR_ADMIN_API_URL", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


# --- config resolution -----------------------------------------------------


def test_resolve_api_url_prefers_flag():
    args = MagicMock(api_url="https://flag-url")
    assert admin_cli._resolve_api_url(args) == "https://flag-url"


def test_resolve_api_url_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("BLOGGERBEAR_ADMIN_API_URL", "https://env-url/")
    args = MagicMock(api_url=None)
    assert admin_cli._resolve_api_url(args) == "https://env-url"


def test_resolve_api_url_missing_raises():
    args = MagicMock(api_url=None)
    with pytest.raises(admin_cli.CliError):
        admin_cli._resolve_api_url(args)


def test_resolve_region_missing_raises():
    args = MagicMock(region=None)
    with pytest.raises(admin_cli.CliError):
        admin_cli._resolve_region(args)


def test_resolve_region_falls_back_to_aws_default_region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    args = MagicMock(region=None)
    assert admin_cli._resolve_region(args) == "ap-southeast-2"


# --- signed_request ---------------------------------------------------------


def test_signed_request_adds_sigv4_authorization_header():
    fake_creds = Credentials("AKIA_TEST", "secret", token=None)
    with (
        patch.object(admin_cli.BotocoreSession, "get_credentials", return_value=fake_creds),
        patch("admin_cli.requests.request", return_value=FakeResponse(200, {})) as mock_request,
    ):
        admin_cli.signed_request("GET", "https://api.example.com", "/topics", "ap-southeast-2")

    mock_request.assert_called_once()
    _, kwargs = mock_request.call_args
    assert "Authorization" in kwargs["headers"]
    assert kwargs["headers"]["Authorization"].startswith("AWS4-HMAC-SHA256")


def test_signed_request_no_credentials_raises():
    with patch.object(admin_cli.BotocoreSession, "get_credentials", return_value=None):
        with pytest.raises(admin_cli.CliError):
            admin_cli.signed_request("GET", "https://api.example.com", "/topics", "ap-southeast-2")


# --- CLI command dispatch (signed_request mocked out) -----------------------


COMMON_ARGS = ["--api-url", "https://api.example.com", "--region", "ap-southeast-2"]


def _run(argv):
    admin_cli.main(COMMON_ARGS + argv)


def test_topics_list_calls_get_topics():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {"topics": []})) as m:
        _run(["topics", "list"])
    m.assert_called_once_with("GET", "https://api.example.com", "/topics", "ap-southeast-2", body=None)


def test_topics_get_calls_get_with_topic_id():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "get", "my-topic"])
    m.assert_called_once_with(
        "GET", "https://api.example.com", "/topics/my-topic", "ap-southeast-2", body=None
    )


def test_topics_create_builds_expected_body():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics",
                "create",
                "--topic-id",
                "new-topic",
                "--name",
                "New Topic",
                "--adapter",
                "github_trending",
                "--config-json",
                '{"language": "python"}',
                "--financial",
            ]
        )
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/topics",
        "ap-southeast-2",
        body={
            "topic_id": "new-topic",
            "name": "New Topic",
            "adapter": "github_trending",
            "adapter_config": {"language": "python"},
            "is_financial": True,
        },
    )


def test_topics_create_defaults_config_and_financial():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics",
                "create",
                "--topic-id",
                "new-topic",
                "--name",
                "New Topic",
                "--adapter",
                "github_trending",
            ]
        )
    body = m.call_args.kwargs["body"]
    assert body["adapter_config"] == {}
    assert body["is_financial"] is False


def test_topics_create_invalid_json_exits_nonzero(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(
                [
                    "topics",
                    "create",
                    "--topic-id",
                    "t",
                    "--name",
                    "n",
                    "--adapter",
                    "a",
                    "--config-json",
                    "not-json",
                ]
            )
    assert exc_info.value.code != 0
    m.assert_not_called()
    assert "not valid JSON" in capsys.readouterr().err


def test_topics_update_partial_body():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "my-topic", "--name", "Renamed", "--no-financial"])
    m.assert_called_once_with(
        "PUT",
        "https://api.example.com",
        "/topics/my-topic",
        "ap-southeast-2",
        body={"name": "Renamed", "is_financial": False},
    )


def test_topics_create_with_custom_cadence():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics",
                "create",
                "--topic-id",
                "new-topic",
                "--name",
                "New Topic",
                "--adapter",
                "github_trending",
                "--research-cadence",
                "rate(30 minutes)",
                "--daily-cadence",
                "cron(0 18 * * ? *)",
            ]
        )
    body = m.call_args.kwargs["body"]
    assert body["research_cadence"] == "rate(30 minutes)"
    assert body["daily_cadence"] == "cron(0 18 * * ? *)"


def test_topics_create_omits_cadence_when_not_passed():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics",
                "create",
                "--topic-id",
                "new-topic",
                "--name",
                "New Topic",
                "--adapter",
                "github_trending",
            ]
        )
    body = m.call_args.kwargs["body"]
    assert "research_cadence" not in body
    assert "daily_cadence" not in body


def test_topics_update_with_custom_cadence():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(
            [
                "topics",
                "update",
                "my-topic",
                "--research-cadence",
                "rate(2 hours)",
                "--daily-cadence",
                "cron(0 9 * * ? *)",
            ]
        )
    m.assert_called_once_with(
        "PUT",
        "https://api.example.com",
        "/topics/my-topic",
        "ap-southeast-2",
        body={"research_cadence": "rate(2 hours)", "daily_cadence": "cron(0 9 * * ? *)"},
    )


def test_topics_update_can_move_a_topic_to_sydney_time():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(
            [
                "topics",
                "update",
                "my-topic",
                "--daily-cadence",
                "cron(0 9 * * ? *)",
                "--daily-timezone",
                "Australia/Sydney",
            ]
        )
    m.assert_called_once_with(
        "PUT",
        "https://api.example.com",
        "/topics/my-topic",
        "ap-southeast-2",
        body={"daily_cadence": "cron(0 9 * * ? *)", "daily_timezone": "Australia/Sydney"},
    )


def test_topics_create_with_daily_timezone():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics",
                "create",
                "--topic-id",
                "new-topic",
                "--name",
                "New Topic",
                "--adapter",
                "github_trending",
                "--daily-timezone",
                "America/New_York",
            ]
        )
    body = m.call_args.kwargs["body"]
    assert body["daily_timezone"] == "America/New_York"


def test_topics_update_with_no_fields_errors(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "update", "my-topic"])
    assert exc_info.value.code != 0
    m.assert_not_called()
    assert "at least one field" in capsys.readouterr().err


def test_topics_delete_calls_delete():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "delete", "my-topic"])
    m.assert_called_once_with(
        "DELETE", "https://api.example.com", "/topics/my-topic", "ap-southeast-2", body=None
    )


def test_topics_trigger_builds_body():
    # --no-wait: this test is about the POST body only, not the polling
    # behavior covered separately below.
    with patch("admin_cli.signed_request", return_value=FakeResponse(202, {})) as m:
        _run(["topics", "trigger", "my-topic", "--pipeline", "daily_cycle", "--no-wait"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/topics/my-topic/trigger",
        "ap-southeast-2",
        body={"pipeline": "daily_cycle"},
    )


def test_topics_trigger_force_is_sent_for_daily_cycle():
    with patch("admin_cli.signed_request", return_value=FakeResponse(202, {})) as m:
        _run(["topics", "trigger", "my-topic", "--pipeline", "daily_cycle", "--force", "--no-wait"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/topics/my-topic/trigger",
        "ap-southeast-2",
        body={"pipeline": "daily_cycle", "force": True},
    )


def test_topics_trigger_force_is_refused_for_research_tick(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "trigger", "my-topic", "--pipeline", "research_tick", "--force"])
    assert exc_info.value.code != 0
    m.assert_not_called()
    assert "--force only applies" in capsys.readouterr().err


def test_topics_trigger_invalid_pipeline_rejected_by_argparse():
    with pytest.raises(SystemExit):
        _run(["topics", "trigger", "my-topic", "--pipeline", "not_a_pipeline"])


def test_topics_trigger_no_wait_skips_polling():
    # No baseline GET, no polling GET -- only the POST itself.
    with patch("admin_cli.signed_request", return_value=FakeResponse(202, {})) as m:
        _run(["topics", "trigger", "my-topic", "--pipeline", "research_tick", "--no-wait"])
    assert m.call_count == 1
    assert m.call_args.args[0] == "POST"


def test_topics_trigger_research_tick_waits_for_new_finding(capsys):
    # Baseline (no finding yet) -> POST accepted -> poll sees a finding.
    responses = [
        FakeResponse(404),
        FakeResponse(202, {}),
        FakeResponse(200, {"captured_at": "2026-01-01T00:00:00+00:00"}),
    ]
    with patch("admin_cli.signed_request", side_effect=responses) as m:
        _run(["topics", "trigger", "my-topic", "--pipeline", "research_tick"])
    assert m.call_count == 3
    assert "finished: a new Finding was written" in capsys.readouterr().out


def test_topics_trigger_research_tick_times_out(monkeypatch, capsys):
    # GET (baseline + every poll) always reports "no finding yet"; the POST
    # trigger itself still succeeds -- polling exhausts its (shrunk, for
    # test speed) timeout and reports it rather than hanging.
    monkeypatch.setattr(admin_cli, "_RESEARCH_TICK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(admin_cli, "_POLL_INTERVAL_SECONDS", 0.01)

    def _fake(method, *args, **kwargs):
        return FakeResponse(202, {}) if method == "POST" else FakeResponse(404)

    with patch("admin_cli.signed_request", side_effect=_fake):
        _run(["topics", "trigger", "my-topic", "--pipeline", "research_tick"])
    assert "No new Finding after" in capsys.readouterr().err


def test_topics_trigger_daily_cycle_waits_for_new_candidate(capsys):
    # Baseline: 0 candidates -> POST accepted -> poll sees 1 candidate.
    responses = [
        FakeResponse(200, {"candidates": []}),
        FakeResponse(202, {}),
        FakeResponse(200, {"candidates": [{"angle": "an angle", "status": "considered"}]}),
    ]
    with patch("admin_cli.signed_request", side_effect=responses) as m:
        _run(["topics", "trigger", "my-topic", "--pipeline", "daily_cycle"])
    assert m.call_count == 3
    assert "finished: new candidate ideas were generated" in capsys.readouterr().out


def test_topics_trigger_daily_cycle_times_out(monkeypatch, capsys):
    monkeypatch.setattr(admin_cli, "_DAILY_CYCLE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(admin_cli, "_POLL_INTERVAL_SECONDS", 0.01)
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {"candidates": []})):
        _run(["topics", "trigger", "my-topic", "--pipeline", "daily_cycle"])
    assert "No new candidates after" in capsys.readouterr().err


def test_topics_findings_calls_get():
    with patch(
        "admin_cli.signed_request", return_value=FakeResponse(200, {"captured_at": "x"})
    ) as m:
        _run(["topics", "findings", "my-topic"])
    m.assert_called_once_with(
        "GET",
        "https://api.example.com",
        "/topics/my-topic/findings/latest",
        "ap-southeast-2",
        body=None,
    )


def test_topics_candidates_calls_get():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "candidates", "my-topic"])
    m.assert_called_once_with(
        "GET",
        "https://api.example.com",
        "/topics/my-topic/candidates",
        "ap-southeast-2",
        body=None,
    )


def test_articles_publish_calls_post():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["articles", "publish", "article-1"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/articles/article-1/publish",
        "ap-southeast-2",
        body=None,
    )


def test_failed_executions_list_calls_get():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {"items": []})) as m:
        _run(["failed-executions", "list"])
    m.assert_called_once_with(
        "GET", "https://api.example.com", "/failed-executions", "ap-southeast-2", body=None
    )


def test_moderation_list_calls_get():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {"items": []})) as m:
        _run(["moderation", "list"])
    m.assert_called_once_with(
        "GET", "https://api.example.com", "/moderation-queue", "ap-southeast-2", body=None
    )


def test_moderation_approve_calls_post():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["moderation", "approve", "queue-1"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/moderation-queue/queue-1/approve",
        "ap-southeast-2",
        body=None,
    )


def test_moderation_reject_calls_post():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["moderation", "reject", "queue-1"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/moderation-queue/queue-1/reject",
        "ap-southeast-2",
        body=None,
    )


def test_non_2xx_response_exits_nonzero_and_prints_error(capsys):
    with patch(
        "admin_cli.signed_request",
        return_value=FakeResponse(404, {"error": "topic 'x' not found"}),
    ):
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "get", "x"])
    assert exc_info.value.code != 0
    assert "topic 'x' not found" in capsys.readouterr().err


def test_success_response_prints_pretty_json(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {"topics": []})):
        _run(["topics", "list"])
    out = capsys.readouterr().out
    assert json.loads(out) == {"topics": []}


# --- per-topic model flags (rotation, PR 4 of 5) ----------------------------


def test_topics_create_sends_model_flags_only_when_passed():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics", "create", "--topic-id", "t", "--name", "T", "--adapter", "github_trending",
                "--model-id", "model-a",
                "--fallback-model-id", "model-b",
                "--model-candidates", "model-a, model-b ,model-c",
            ]
        )
    body = m.call_args.kwargs["body"]
    assert body["model_id"] == "model-a"
    assert body["fallback_model_id"] == "model-b"
    assert body["model_id_candidates"] == ["model-a", "model-b", "model-c"]


def test_topics_create_omits_model_fields_when_not_passed():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(["topics", "create", "--topic-id", "t", "--name", "T", "--adapter", "github_trending"])
    body = m.call_args.kwargs["body"]
    assert "model_id" not in body
    assert "fallback_model_id" not in body
    assert "model_id_candidates" not in body


def test_topics_update_empty_string_clears_model_fields():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(
            [
                "topics", "update", "t",
                "--model-id", "", "--fallback-model-id", "", "--model-candidates", "",
            ]
        )
    m.assert_called_once_with(
        "PUT",
        "https://api.example.com",
        "/topics/t",
        "ap-southeast-2",
        body={"model_id": None, "fallback_model_id": None, "model_id_candidates": []},
    )


def test_topics_create_without_an_adapter_omits_it_so_the_api_default_applies():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(["topics", "create", "--topic-id", "bare", "--name", "Bare Topic"])

    body = m.call_args.kwargs["body"]
    assert "adapter" not in body
    assert "editorial_goals" not in body


def test_topics_create_passes_editorial_goals_through():
    goals = {"primary_focus": "Track zero-days", "exclusion_criteria": "No marketing"}
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            [
                "topics", "create", "--topic-id", "sec", "--name", "Security",
                "--editorial-goals-json", json.dumps(goals),
            ]
        )  # fmt: skip

    assert m.call_args.kwargs["body"]["editorial_goals"] == goals


def test_topics_update_sets_and_clears_editorial_goals():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "sec", "--editorial-goals-json", '{"primary_focus": "New"}'])
        assert m.call_args.kwargs["body"] == {"editorial_goals": {"primary_focus": "New"}}

        _run(["topics", "update", "sec", "--editorial-goals-json", "{}"])
        assert m.call_args.kwargs["body"] == {"editorial_goals": {}}


def test_topics_editorial_goals_invalid_json_exits_nonzero(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "update", "sec", "--editorial-goals-json", "{not json"])

    assert exc_info.value.code != 0
    assert "--editorial-goals-json is not valid JSON" in capsys.readouterr().err
    m.assert_not_called()


def test_articles_unpublish_posts_to_the_unpublish_route():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["articles", "unpublish", "article-1"])
    m.assert_called_once_with(
        "POST",
        "https://api.example.com",
        "/articles/article-1/unpublish",
        "ap-southeast-2",
        body=None,
    )


def test_lineage_audit_gets_the_audit_route():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["lineage", "audit"])
    m.assert_called_once_with(
        "GET", "https://api.example.com", "/lineage/audit", "ap-southeast-2", body=None
    )


def test_lineage_backfill_is_a_dry_run_unless_apply_is_passed():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["lineage", "backfill"])
    assert m.call_args.kwargs["body"] == {"apply": False}
    assert m.call_args.args[:3] == ("POST", "https://api.example.com", "/lineage/backfill")


def test_lineage_backfill_apply_writes():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["lineage", "backfill", "--apply"])
    assert m.call_args.kwargs["body"] == {"apply": True}


def test_topics_update_sets_a_research_interval():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "my-topic", "--research-interval-hours", "2"])
    assert m.call_args.kwargs["body"] == {"research_interval_hours": 2}


def test_topics_update_clears_a_research_interval_with_an_empty_string():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "my-topic", "--research-interval-hours", ""])
    assert m.call_args.kwargs["body"] == {"research_interval_hours": None}


def test_topics_create_accepts_a_research_interval():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            ["topics", "create", "--topic-id", "t", "--name", "T", "--adapter", "github_trending",
             "--research-interval-hours", "3"]
        )
    assert m.call_args.kwargs["body"]["research_interval_hours"] == 3


def test_a_non_numeric_research_interval_is_refused_before_any_request(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "update", "my-topic", "--research-interval-hours", "soon"])
    assert exc_info.value.code != 0
    m.assert_not_called()
    assert "whole number of hours" in capsys.readouterr().err


def test_pipeline_config_get():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "get"])
    m.assert_called_once_with(
        "GET", "https://api.example.com", "/pipeline-config", "ap-southeast-2", body=None
    )


def test_pipeline_config_set_and_clear():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--research-interval-hours", "2"])
    assert m.call_args.args[:3] == ("PUT", "https://api.example.com", "/pipeline-config")
    assert m.call_args.kwargs["body"] == {"research_interval_hours": 2}

    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--research-interval-hours", ""])
    assert m.call_args.kwargs["body"] == {"research_interval_hours": None}


def test_pipeline_config_set_review_mode():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--review-mode", "off"])
    assert m.call_args.args[:3] == ("PUT", "https://api.example.com", "/pipeline-config")
    assert m.call_args.kwargs["body"] == {"review_mode": "off"}


def test_pipeline_config_clear_review_mode_with_an_empty_string():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--review-mode", ""])
    assert m.call_args.kwargs["body"] == {"review_mode": None}


def test_pipeline_config_set_can_send_both_settings():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--research-interval-hours", "2", "--review-mode", "shadow"])
    assert m.call_args.kwargs["body"] == {"research_interval_hours": 2, "review_mode": "shadow"}


def test_pipeline_config_set_with_nothing_to_set_is_refused(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit) as exc_info:
            _run(["pipeline-config", "set"])
    assert exc_info.value.code != 0
    m.assert_not_called()
    assert "needs --research-interval-hours, --review-mode and/or --review-on-unavailable" in (
        capsys.readouterr().err
    )


def test_pipeline_config_review_mode_rejects_a_value_that_does_not_exist():
    with pytest.raises(SystemExit):
        _run(["pipeline-config", "set", "--review-mode", "bogus"])


def test_pipeline_config_can_turn_enforcement_on():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--review-mode", "enforce"])
    assert m.call_args.kwargs["body"] == {"review_mode": "enforce"}


def test_pipeline_config_sets_and_clears_what_to_do_when_a_review_cannot_run():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--review-on-unavailable", "note"])
    assert m.call_args.kwargs["body"] == {"review_on_unavailable": "note"}

    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["pipeline-config", "set", "--review-on-unavailable", ""])
    assert m.call_args.kwargs["body"] == {"review_on_unavailable": None}


def test_pipeline_config_rejects_an_unavailable_action_that_does_not_exist():
    with pytest.raises(SystemExit):
        _run(["pipeline-config", "set", "--review-on-unavailable", "publish"])


def test_pipeline_config_can_send_all_three_settings_at_once():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(
            [
                "pipeline-config", "set", "--research-interval-hours", "2",
                "--review-mode", "enforce", "--review-on-unavailable", "hold",
            ]
        )
    assert m.call_args.kwargs["body"] == {
        "research_interval_hours": 2,
        "review_mode": "enforce",
        "review_on_unavailable": "hold",
    }


def test_topics_update_sets_and_clears_a_topics_own_review_mode():
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "my-topic", "--review-mode", "enforce"])
    assert m.call_args.kwargs["body"] == {"review_mode": "enforce"}

    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "my-topic", "--review-mode", ""])
    assert m.call_args.kwargs["body"] == {"review_mode": None}


def test_topics_create_accepts_a_review_mode():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(
            ["topics", "create", "--topic-id", "t", "--name", "T", "--adapter", "github_trending",
             "--review-mode", "shadow"]
        )
    assert m.call_args.kwargs["body"]["review_mode"] == "shadow"


def test_a_topic_review_mode_that_does_not_exist_is_refused_by_the_cli():
    with pytest.raises(SystemExit):
        _run(["topics", "update", "my-topic", "--review-mode", "bogus"])
