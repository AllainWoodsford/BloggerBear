"""Which log groups the assistant may read (ops_mcp/logs.py), and the shared Logs Insights runner.

The owner's rule, held here as in IAM: by environment, project and ManagedBy. Dev reads dev's;
production reads production's and "shared"; dev never reads production's or shared, production
never reads dev's. A group must also carry the project's default tags, checked against its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

from ops_mcp import logs, samples

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TAGS = {"ManagedBy": "Terraform", "Project": "BloggerBear"}


class FakeTags:
    def __init__(self, tags_by_name: dict[str, dict] | None = None, error: str | None = None):
        self.tags_by_name = tags_by_name or {}
        self.error = error
        self.asked: list[str] = []

    def list_tags_for_resource(self, resourceArn):  # noqa: N803 - boto3's own name
        self.asked.append(resourceArn)
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "no"}}, "ListTagsForResource")
        name = resourceArn.split(":log-group:", 1)[1]
        return {"tags": self.tags_by_name.get(name, {})}


def _environment(monkeypatch, env: str, readable: str):
    monkeypatch.setenv("ENVIRONMENT_NAME", env)
    monkeypatch.setenv("AWS_REGION", "ap-southeast-2")
    monkeypatch.setenv(samples.READABLE_ENVIRONMENTS_ENV, readable)
    monkeypatch.setenv(samples.DEFAULT_TAGS_ENV, json.dumps(TAGS))
    monkeypatch.setattr(samples, "_account_id", lambda: "111111111111")
    logs._tags.clear()


@pytest.fixture
def dev(monkeypatch):
    _environment(monkeypatch, "dev", "dev")


@pytest.fixture
def production(monkeypatch):
    _environment(monkeypatch, "production", "production,shared")


def _fake(monkeypatch, fake):
    monkeypatch.setattr(logs, "client", lambda region: fake)
    return fake


# --- by name ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "/aws/lambda/bloggerbear-dev-research-tick",
        "/aws/apigateway/bloggerbear-dev-public-api-access",
    ],
)
def test_dev_may_read_its_own_lambda_and_access_logs_by_name(dev, name):
    assert logs.check_name(name) == "dev"


@pytest.mark.parametrize(
    "name",
    [
        "/aws/lambda/bloggerbear-production-research-tick",  # production's
        "/aws/lambda/bloggerbear-shared-anything",  # shared
        "aws-waf-logs-bloggerbear-dev-admin",  # the firewall's: firewall.tf's, not this
        "/aws/lambda/someone-elses-function",
        "/aws/lambda/bloggerbear-dev-x/../production",
        "",
    ],
)
def test_dev_may_not_read_anything_else(dev, name):
    with pytest.raises(logs.NotAllowed):
        logs.check_name(name)


def test_production_reads_its_own_and_shared_and_never_devs(production):
    assert logs.check_name("/aws/lambda/bloggerbear-production-daily-cycle") == "production"
    assert logs.check_name("/aws/lambda/bloggerbear-shared-anything") == "shared"
    with pytest.raises(logs.NotAllowed):
        logs.check_name("/aws/lambda/bloggerbear-dev-daily-cycle")


def test_the_modules_list_cannot_widen_the_owners_rule(monkeypatch):
    # A dev function told it may read production (a mistake in the module) still may not.
    _environment(monkeypatch, "dev", "dev,production,shared")
    with pytest.raises(logs.NotAllowed):
        logs.check_name("/aws/lambda/bloggerbear-production-daily-cycle")


def test_no_environment_reads_nothing(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT_NAME", raising=False)
    with pytest.raises(logs.NotAllowed):
        logs.check_name("/aws/lambda/bloggerbear-dev-daily-cycle")


# --- by tags ---------------------------------------------------------------------------------------


def test_a_group_with_the_projects_tags_and_its_environment_is_readable(dev, monkeypatch):
    name = "/aws/lambda/bloggerbear-dev-research-tick"
    fake = _fake(monkeypatch, FakeTags({name: {**TAGS, "Environment": "dev", "TerraformRoot": "x"}}))
    assert logs.check_group(name) == {**TAGS, "Environment": "dev"}
    assert fake.asked == [f"arn:aws:logs:ap-southeast-2:111111111111:log-group:{name}"]


@pytest.mark.parametrize(
    "tags",
    [
        {"Project": "BloggerBear", "Environment": "dev"},  # not ManagedBy Terraform
        {"ManagedBy": "admin-api", "Project": "BloggerBear", "Environment": "dev"},
        {"ManagedBy": "Terraform", "Project": "Other", "Environment": "dev"},
        {**TAGS, "Environment": "production"},  # named dev, tagged production
        {**TAGS},
        {},
    ],
)
def test_a_group_whose_tags_differ_is_refused(dev, monkeypatch, tags):
    name = "/aws/lambda/bloggerbear-dev-research-tick"
    _fake(monkeypatch, FakeTags({name: tags}))
    with pytest.raises(logs.NotAllowed):
        logs.check_group(name)


def test_production_reads_a_shared_group_only_if_it_is_tagged_shared(production, monkeypatch):
    name = "/aws/lambda/bloggerbear-shared-anything"
    _fake(monkeypatch, FakeTags({name: {**TAGS, "Environment": "shared"}}))
    assert logs.check_group(name)["Environment"] == "shared"
    logs._tags.clear()
    _fake(monkeypatch, FakeTags({name: {**TAGS, "Environment": "production"}}))
    with pytest.raises(logs.NotAllowed):
        logs.check_group(name)


@pytest.mark.parametrize("code", ["AccessDeniedException", "ResourceNotFoundException", "Throttling"])
def test_a_tag_lookup_aws_refuses_is_a_refusal(dev, monkeypatch, code):
    _fake(monkeypatch, FakeTags(error=code))
    with pytest.raises(logs.NotAllowed):
        logs.check_group("/aws/lambda/bloggerbear-dev-research-tick")


def test_a_refused_name_never_reaches_aws(dev, monkeypatch):
    fake = _fake(monkeypatch, FakeTags())
    with pytest.raises(logs.NotAllowed):
        logs.check_group("/aws/lambda/bloggerbear-production-research-tick")
    assert fake.asked == []


def test_tags_are_remembered_for_a_while(dev, monkeypatch):
    name = "/aws/lambda/bloggerbear-dev-research-tick"
    fake = _fake(monkeypatch, FakeTags({name: {**TAGS, "Environment": "dev"}}))
    logs.check_group(name, now=0)
    logs.check_group(name, now=10)
    assert len(fake.asked) == 1
    logs.check_group(name, now=logs.TAG_CACHE_SECONDS + 1)
    assert len(fake.asked) == 2


def test_readable_splits_allowed_from_refused_with_reasons(dev, monkeypatch):
    good = "/aws/lambda/bloggerbear-dev-research-tick"
    _fake(monkeypatch, FakeTags({good: {**TAGS, "Environment": "dev"}}))
    allowed, refused = logs.readable([good, "/aws/lambda/bloggerbear-production-x", good])
    assert allowed == [good]
    assert refused == [{"log_group": "/aws/lambda/bloggerbear-production-x", "why": refused[0]["why"]}]
    assert refused[0]["why"]


# --- the runner ------------------------------------------------------------------------------------


class FakeLogs:
    def __init__(self, statuses):
        self.statuses = statuses
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start_query(self, **kwargs):
        group = kwargs["logGroupName"]
        if group == "broken":
            raise RuntimeError("no")
        assert kwargs["limit"] == logs.QUERY_LIMIT
        self.started.append(group)
        return {"queryId": group}

    def get_query_results(self, queryId):  # noqa: N803
        queue = self.statuses[queryId]
        status = queue.pop(0) if len(queue) > 1 else queue[0]
        rows = [[{"field": "n", "value": "2"}]]
        return {"status": status, "results": rows if status == "Complete" else []}

    def stop_query(self, queryId):  # noqa: N803
        self.stopped.append(queryId)


def test_queries_run_together_and_one_still_running_is_stopped():
    fake = FakeLogs({"done": ["Running", "Complete"], "bad": ["Failed"], "slow": ["Running"]})
    jobs = [
        (("done", "a"), "ap-southeast-2", "done", "q", NOW, NOW),
        (("bad", "a"), "ap-southeast-2", "bad", "q", NOW, NOW),
        (("slow", "a"), "ap-southeast-2", "slow", "q", NOW, NOW),
        (("broken", "a"), "ap-southeast-2", "broken", "q", NOW, NOW),
    ]
    results = logs.run_queries(jobs, client=lambda region: fake, wait_seconds=0.05, sleep=lambda s: None)
    assert results == {
        ("done", "a"): [{"n": "2"}],
        ("bad", "a"): None,
        ("slow", "a"): None,
        ("broken", "a"): None,
    }
    assert fake.stopped == ["slow"]


@pytest.mark.parametrize(("value", "counted"), [("3", 3), ("2.0", 2), ("-1", 0), (None, 0), ("x", 0)])
def test_count(value, counted):
    assert logs.count(value) == counted
