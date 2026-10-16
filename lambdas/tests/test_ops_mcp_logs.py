"""Which log groups the assistant may read (ops_mcp/logs.py), and the shared Logs Insights runner.

The owner's rule, held here as in IAM: by environment, project and ManagedBy. Dev reads dev's;
production reads production's and "shared"; dev never reads production's or shared, production
never reads dev's. A group must also carry the project's default tags, checked against its own.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from botocore.awsrequest import AWSResponse
from botocore.exceptions import ClientError

from ops_mcp import logs, samples

NOW = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
TAGS = {"ManagedBy": "Terraform", "Project": "BloggerBear"}
_ACCOUNT_ID = samples._account_id  # the real one: the fixtures below stub it


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


def test_the_tag_cache_is_kept_per_region_and_name(dev, monkeypatch):
    """The vision worker's log group is read in the vision Region (the assistant's vision round): a
    group of the same name in another Region is another group, with its own tags."""
    name = "/aws/lambda/bloggerbear-dev-vision-worker"
    fake = _fake(monkeypatch, FakeTags({name: {**TAGS, "Environment": "dev"}}))
    logs.check_group(name, region="us-west-2", now=0)
    logs.check_group(name, region="us-west-2", now=10)
    assert len(fake.asked) == 1 and ":us-west-2:" in fake.asked[0]
    logs.check_group(name, region="ap-southeast-2", now=10)
    assert len(fake.asked) == 2 and ":ap-southeast-2:" in fake.asked[1]
    assert set(logs._tags) == {("us-west-2", name), ("ap-southeast-2", name)}


def test_readable_splits_allowed_from_refused_with_reasons(dev, monkeypatch):
    good = "/aws/lambda/bloggerbear-dev-research-tick"
    _fake(monkeypatch, FakeTags({good: {**TAGS, "Environment": "dev"}}))
    allowed, refused = logs.readable([good, "/aws/lambda/bloggerbear-production-x", good])
    assert allowed == [good]
    assert refused == [{"log_group": "/aws/lambda/bloggerbear-production-x", "why": refused[0]["why"]}]
    assert refused[0]["why"]


class FakeSts:
    def __init__(self):
        self.asked = 0

    def get_caller_identity(self):
        self.asked += 1
        return {"Account": "111111111111"}


def test_a_caller_may_ask_with_its_own_clients_and_the_shared_ones_are_never_used(dev, monkeypatch):
    """vision_evidence's way in: `clients` and `sts` make the clients the tags and the account id
    are asked with, each built when its request is about to be made (the account first, so the Logs
    client is built with the time left after it). Neither shared client is asked or built."""
    monkeypatch.setattr(samples, "_account_id", _ACCOUNT_ID)
    monkeypatch.setattr(samples, "_account", [])
    monkeypatch.setattr(samples, "_sts_client", None)
    monkeypatch.setattr(samples.boto3, "client", lambda *args, **kwargs: pytest.fail("a client was built"))
    monkeypatch.setattr(logs, "client", lambda region: pytest.fail("the shared Logs client was asked"))
    good = "/aws/lambda/bloggerbear-dev-research-tick"
    tags, sts, built = FakeTags({good: {**TAGS, "Environment": "dev"}}), FakeSts(), []

    def clients(region):
        built.append(("logs", region))
        return tags

    def account():
        built.append(("sts",))
        return sts

    allowed, refused = logs.readable([good], region="ap-southeast-2", clients=clients, sts=account)
    assert (allowed, refused) == ([good], [])
    assert built == [("sts",), ("logs", "ap-southeast-2")] and sts.asked == 1
    assert tags.asked == [f"arn:aws:logs:ap-southeast-2:111111111111:log-group:{good}"]


def test_with_no_sts_given_group_arn_is_asked_as_before(dev, monkeypatch):
    """A caller that passes no `sts` asks group_arn(name, region), the call it made before `sts`
    existed, so a (name, region) stand-in for it (test_ops_mcp_api_errors.py's, log_review's) fits."""
    good = "/aws/lambda/bloggerbear-dev-research-tick"
    monkeypatch.setattr(
        logs, "group_arn", lambda name, region: f"arn:aws:logs:{region}:123456789012:log-group:{name}"
    )
    fake = _fake(monkeypatch, FakeTags({good: {**TAGS, "Environment": "dev"}}))
    assert logs.readable([good], region="ap-southeast-2") == ([good], [])
    assert fake.asked == [f"arn:aws:logs:ap-southeast-2:123456789012:log-group:{good}"]


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


THROTTLES = ["LimitExceededException", "ThrottlingException"]


def _throttle(code: str, operation: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "Rate exceeded"}}, operation)


class ThrottledLogs(FakeLogs):
    """FakeLogs, on which the first `starts` StartQuery requests and the first `polls` GetQueryResults
    requests of each query are throttled (`code`), as Logs answers a burst."""

    def __init__(self, statuses, *, starts=0, polls=0, code="ThrottlingException"):
        super().__init__(statuses)
        self.starts, self.polls, self.code = starts, dict.fromkeys(statuses, polls), code
        self.asked: list[str] = []

    def start_query(self, **kwargs):
        self.asked.append("StartQuery")
        if self.starts:
            self.starts -= 1
            raise _throttle(self.code, "StartQuery")
        return super().start_query(**kwargs)

    def get_query_results(self, queryId):  # noqa: N803
        self.asked.append("GetQueryResults")
        if self.polls[queryId]:
            self.polls[queryId] -= 1
            raise _throttle(self.code, "GetQueryResults")
        return super().get_query_results(queryId)


def _run(fake, jobs, **kwargs):
    slept = []
    results = logs.run_queries(jobs, client=lambda region: fake, sleep=slept.append, **kwargs)
    return results, slept


def test_the_throttles_recovered_are_logs_own():
    assert logs.THROTTLES == set(THROTTLES)


@pytest.mark.parametrize("code", THROTTLES)
def test_a_throttled_poll_is_asked_again_only_for_a_caller_that_opts_in(code, capsys):
    """vision_evidence's clients make one attempt each, so it asks run_queries to recover a throttle:
    the throttled poll counts as still running, and the next round's answer is the query's. Without
    the opt-in (log_review, api_errors, firewall: the shared client has retried already) it fails
    the query, as before."""
    jobs = [(("done", "a"), "ap-southeast-2", "done", "q", NOW, NOW)]
    fake = ThrottledLogs({"done": ["Complete"]}, polls=1, code=code)
    results, slept = _run(fake, jobs, wait_seconds=5, recover_throttles=True)
    assert results == {("done", "a"): [{"n": "2"}]}
    assert fake.asked == ["StartQuery", "GetQueryResults", "GetQueryResults"] and slept == [logs.POLL_SECONDS]
    assert "ops_logs: results throttled, asked again" in capsys.readouterr().out
    fake = ThrottledLogs({"done": ["Complete"]}, polls=1, code=code)
    results, slept = _run(fake, jobs, wait_seconds=5)
    assert results == {("done", "a"): None}
    assert fake.asked == ["StartQuery", "GetQueryResults"] and slept == [] and fake.stopped == []
    assert "ops_logs: results failed error=ClientError" in capsys.readouterr().out


def test_polls_throttled_for_the_whole_wait_end_the_query_as_not_finished():
    """A throttle is never an answer: a query whose every poll is throttled is polled every round
    until the wait ends, then said as not finished and stopped, as one still running is."""
    jobs = [(("slow", "a"), "ap-southeast-2", "slow", "q", NOW, NOW)]
    fake = ThrottledLogs({"slow": ["Complete"]}, polls=10**6)
    results, slept = _run(fake, jobs, wait_seconds=0.05, recover_throttles=True)
    assert results == {("slow", "a"): None} and fake.stopped == ["slow"]
    assert fake.asked.count("GetQueryResults") == len(slept) >= 1


@pytest.mark.parametrize("code", THROTTLES)
def test_a_throttled_start_is_tried_once_more_only_for_a_caller_that_opts_in(code, capsys):
    """One more try, after one pause, for each throttled start; a second throttle, or any other error,
    fails the query as before. Without the opt-in a throttled start fails at once."""
    jobs = [
        (("done", "a"), "ap-southeast-2", "done", "q", NOW, NOW),
        (("also", "a"), "ap-southeast-2", "also", "q", NOW, NOW),
    ]
    statuses = {"done": ["Complete"], "also": ["Complete"]}
    fake = ThrottledLogs(statuses, starts=1, code=code)
    results, slept = _run(fake, jobs, wait_seconds=5, recover_throttles=True)
    assert results == {("done", "a"): [{"n": "2"}], ("also", "a"): [{"n": "2"}]}
    assert fake.asked.count("StartQuery") == 3 and fake.started == ["also", "done"]
    assert slept == [logs.POLL_SECONDS]  # the one pause before the try more; none while polling
    assert "ops_logs: start throttled, tried once more" in capsys.readouterr().out
    fake = ThrottledLogs(statuses, starts=3, code=code)  # both throttled, then the first again
    results, slept = _run(fake, jobs, wait_seconds=5, recover_throttles=True)
    assert results == {("done", "a"): None, ("also", "a"): [{"n": "2"}]}
    assert fake.asked.count("StartQuery") == 4 and fake.started == ["also"]
    assert slept == [logs.POLL_SECONDS]  # one pause for both retried starts, not one each
    fake = ThrottledLogs(statuses, starts=1, code=code)
    results, slept = _run(fake, jobs, wait_seconds=5)
    assert results == {("done", "a"): None, ("also", "a"): [{"n": "2"}]}
    assert fake.asked.count("StartQuery") == 2 and slept == []
    assert "ops_logs: start failed error=ClientError" in capsys.readouterr().out


def test_only_a_throttle_is_recovered():
    """Another refusal (AccessDenied) or a server error fails the query at once, with or without the
    opt-in: only a throttle says the same request may succeed when asked again."""
    jobs = [(("done", "a"), "ap-southeast-2", "done", "q", NOW, NOW)]
    for recover in (False, True):
        fake = ThrottledLogs({"done": ["Complete"]}, starts=1, code="AccessDeniedException")
        assert _run(fake, jobs, wait_seconds=5, recover_throttles=recover)[0] == {("done", "a"): None}
        assert fake.asked == ["StartQuery"]
        fake = ThrottledLogs({"done": ["Complete"]}, polls=1, code="ServiceUnavailableException")
        assert _run(fake, jobs, wait_seconds=5, recover_throttles=recover)[0] == {("done", "a"): None}
        assert fake.asked == ["StartQuery", "GetQueryResults"]


def test_the_tools_on_the_shared_client_never_opt_in():
    """log_review, api_errors and firewall ask through logs.py's shared client, whose own retry
    rides out a throttle: none passes recover_throttles, so each is answered as before. Only
    vision_evidence, whose clients make one attempt each, does."""
    root = Path(logs.__file__).parent
    callers = {
        path.name: path.read_text(encoding="utf-8")
        for path in root.glob("*.py")
        if "run_queries(" in path.read_text(encoding="utf-8") and path.name != "logs.py"
    }
    assert set(callers) == {"log_review.py", "api_errors.py", "firewall.py"}
    assert not any("recover_throttles" in text for text in callers.values())
    assert "recover_throttles=True" in (root / "vision_evidence.py").read_text(encoding="utf-8")


class _Raw:
    def __init__(self, body: bytes):
        self.body = body

    def stream(self, **kwargs):
        yield self.body


def test_logs_is_asked_with_short_timeouts_and_one_retry(monkeypatch):
    """What botocore resolves, not the dict handed to it: its `max_attempts` counts retries, so a
    throttled call is sent twice in all, one retry, not three times."""
    for name, value in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(logs, "_clients", {})
    client = logs.client("ap-southeast-2")
    assert client is logs.client("ap-southeast-2")
    assert client.meta.config.connect_timeout == 3 and client.meta.config.read_timeout == 8
    assert client.meta.config.retries["total_max_attempts"] == 2
    sent = []
    throttled = json.dumps({"__type": "ThrottlingException", "message": "Rate exceeded"}).encode()

    def answer(request, **kwargs):
        sent.append(request.url)
        return AWSResponse(request.url, 400, {}, _Raw(throttled))

    client.meta.events.register("before-send.cloudwatch-logs.GetQueryResults", answer)
    with pytest.raises(ClientError) as err:
        client.get_query_results(queryId="q-1")
    assert err.value.response["Error"]["Code"] == "ThrottlingException"
    assert len(sent) == 2


@pytest.mark.parametrize(("value", "counted"), [("3", 3), ("2.0", 2), ("-1", 0), (None, 0), ("x", 0)])
def test_count(value, counted):
    assert logs.count(value) == counted
