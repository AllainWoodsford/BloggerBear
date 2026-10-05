"""api_errors (ops_mcp/api_errors.py): failed requests in the APIs' access logs, who answered and why.

Held here: the root cause comes from status and errorType in code; the one value put into a query
is a checked status; only this environment's access logs are read, through the access check; the
error total comes from the timeline, which is not cut short; 5XXs point at log_review for the
Lambda; and the check-it-yourself cards carry the queries that ran.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ops_mcp import api_errors
from ops_mcp.suggestions import CATALOGUE

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
PUBLIC = "/aws/apigateway/bloggerbear-dev-public-api-access"


@pytest.fixture(autouse=True)
def dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")


def everything_readable(names):
    return list(names), []


class Run:
    def __init__(self, answers=None, missing=()):
        self.answers = answers or {}
        self.missing = set(missing)
        self.jobs: list = []

    def __call__(self, jobs):
        self.jobs = jobs
        return {key: (None if key in self.missing else self.answers.get(key, [])) for key, *_ in jobs}


def call(run, **kwargs):
    readable = kwargs.pop("readable", everything_readable)
    return api_errors.api_errors(now=NOW, run=run, readable=readable, **kwargs)


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (403, "WAF_FILTERED", "firewall_blocked"),
        (429, "THROTTLED", "throttled"),
        (429, "-", "throttled"),
        (403, "MISSING_AUTHENTICATION_TOKEN", "no_such_route"),
        (401, "UNAUTHORIZED", "auth_refused"),
        (403, "ACCESS_DENIED", "auth_refused"),
        (400, "BAD_REQUEST_BODY", "bad_request"),
        (400, "-", "handler_4xx"),
        (404, "", "handler_4xx"),
        (504, "INTEGRATION_TIMEOUT", "integration_timeout"),
        (502, "INTEGRATION_FAILURE", "lambda_failed"),
        (500, "-", "lambda_failed"),
        (500, "DEFAULT_5XX", "aws_5xx"),
        (418, "SOMETHING_NEW", "other"),
    ],
)
def test_each_error_gets_a_root_cause_from_status_and_error_type(status, error_type, expected):
    assert api_errors.cause(status, error_type).key == expected


def test_every_cause_has_a_catalogue_suggestion_with_no_command():
    for key in api_errors.CAUSES:
        entry = CATALOGUE[f"api_{key}"]
        assert entry.arguments is None and entry.action


@pytest.mark.parametrize(
    ("status", "clean"), [(400, 400), ("502", 502), (99, None), (600, None), ("4xx", None), (True, None)]
)
def test_only_a_real_status_reaches_a_query(status, clean):
    assert api_errors.clean_status(status) == clean


def test_the_status_is_the_only_thing_put_into_a_query():
    assert "status = 400" in api_errors.queries(400)["breakdown"]
    assert "status >= 400" in api_errors.queries(None)["timeline"]
    with pytest.raises(ValueError):
        api_errors.queries("400 | stats")


def test_with_no_api_named_all_three_access_logs_of_this_environment_are_read():
    run = Run()
    call(run)
    groups = {job[2] for job in run.jobs}
    assert groups == {
        PUBLIC,
        "/aws/apigateway/bloggerbear-dev-admin-api-access",
        "/aws/apigateway/bloggerbear-dev-ops-mcp-access",
    }


def test_an_api_we_do_not_have_is_refused():
    run = Run()
    assert call(run, api="billing")["findings"] == [] and run.jobs == []


def test_a_refused_log_is_not_read_and_is_said():
    run = Run()
    result = call(run, readable=lambda names: ([PUBLIC], [{"log_group": "x", "why": "no"}]))
    assert {job[2] for job in run.jobs} == {PUBLIC}
    assert "wasn't allowed to read 1" in result["spoken"]


def _a_bad_morning():
    return Run(
        {
            ("public", "breakdown"): [
                {
                    "status": "400",
                    "errorType": "-",
                    "resourcePath": "/articles/{article_id}/feedback",
                    "httpMethod": "POST",
                    "requests": "80",
                },
                {
                    "status": "502",
                    "errorType": "-",
                    "resourcePath": "/articles",
                    "httpMethod": "GET",
                    "requests": "15",
                },
                {
                    "status": "403",
                    "errorType": "WAF_FILTERED",
                    "resourcePath": "-",
                    "httpMethod": "GET",
                    "requests": "5",
                },
            ],
            ("public", "total"): [{"requests": "2000"}],
            ("public", "timeline"): [
                {"bin(1h)": "2026-10-05 01:00:00.000", "errors": "0"},
                {"bin(1h)": "2026-10-05 02:00:00.000", "errors": "70"},
                {"bin(1h)": "2026-10-05 03:00:00.000", "errors": "40"},
            ],
        }
    )


def test_errors_are_broken_down_by_cause_with_the_total_from_the_timeline():
    result = call(_a_bad_morning(), api="public", start="2026-10-05T00:00:00Z", end="2026-10-05T06:00:00Z")
    row = result["by_api"][0]
    assert row["requests"] == 2000 and row["errors"] == 110  # the timeline's, above the breakdown's 100
    assert [(c["cause"], c["count"]) for c in row["causes"]] == [
        ("handler_4xx", 80),
        ("lambda_failed", 15),
        ("firewall_blocked", 5),
    ]
    assert row["first_error_hour"] == "2026-10-05 02:00:00.000"
    assert row["peak_hour"] == {"hour": "2026-10-05 02:00:00.000", "errors": 70}
    assert "_by_cause" not in row


def test_a_5xx_the_lambda_answered_points_at_its_log():
    result = call(_a_bad_morning(), api="public")
    failed = next(f for f in result["findings"] if f["kind"] == "api_lambda_failed")
    assert failed["id"] == "public-api"
    assert failed["root_cause"]["next"]["tool"] == "log_review"
    assert failed["root_cause"]["next"]["function"] == "public-api"
    assert failed["root_cause"]["fix_type"] == "code"
    assert "I can read it next" in result["spoken"]


def test_firewall_blocks_point_at_the_firewall_only_in_production(monkeypatch):
    dev_result = call(_a_bad_morning(), api="public")
    blocked = next(f for f in dev_result["findings"] if f["kind"] == "api_firewall_blocked")
    assert blocked["root_cause"]["next"] is None
    monkeypatch.setenv("ENVIRONMENT_NAME", "production")
    run = _a_bad_morning()
    run.answers = {("public", name): rows for (_, name), rows in run.answers.items()}
    prod = call(run, api="public")
    blocked = next(f for f in prod["findings"] if f["kind"] == "api_firewall_blocked")
    assert blocked["root_cause"]["next"] == {"tool": "firewall_review"}
    assert "look at the firewall" in prod["spoken"]


def test_spoken_is_counts_and_fixed_words():
    spoken = call(_a_bad_morning(), api="public")["spoken"]
    assert spoken.startswith(
        "In the last 24 hours, the public API answered 110 errors out of 2000 requests "
        "(5.5%; 94.5% succeeded)."
    )
    assert (
        "Most were 80 4XXs the handler chose itself: that looks like the caller sending something wrong"
        in spoken
    )
    assert "/articles" not in spoken
    assert "The first came in the hour from 2026-10-05 02:00:00.000 UTC." in spoken


def test_a_quiet_api_says_so():
    run = Run({("admin", "total"): [{"requests": "40"}]})
    assert call(run, api="admin", status=400)["spoken"].startswith(
        "In the last 24 hours, the admin API answered no 400 responses out of 40 requests."
    )


def test_a_query_that_did_not_answer_is_said():
    run = Run(missing={("public", "timeline")})
    result = call(run, api="public")
    assert not result["complete"] and "didn't answer in time" in result["spoken"]


def test_check_it_yourself_cards_carry_the_queries_that_ran():
    run = Run()
    result = call(run, api="public", status=400)
    cards = [f for f in result["findings"] if f["kind"] == "how_to"]
    ran = {job[3] for job in run.jobs}
    assert len(cards) == 2 and all(card["suggestion"]["command"] in ran for card in cards)
    assert cards[0]["where"]["open"].startswith("https://ap-southeast-2.console.aws.amazon.com/cloudwatch/")


def test_success_rate_is_reported_per_api_and_not_claimed_under_a_status_filter():
    row = call(_a_bad_morning(), api="public")["by_api"][0]
    assert row["success_rate"] == 94.5
    quiet = Run({("admin", "total"): [{"requests": "40"}]})
    assert "all succeeded" in call(quiet, api="admin")["spoken"]
    assert "all succeeded" not in call(quiet, api="admin", status=400)["spoken"]
