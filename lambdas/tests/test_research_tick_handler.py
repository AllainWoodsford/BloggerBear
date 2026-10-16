from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import research_tick_handler
from common import evidence
from common.adapters.base import Adapter
from common.adapters.crypto_feed import CryptoFeedAdapter
from common.adapters.github_trending import GitHubTrendingAdapter
from common.adapters.hacker_news import HackerNewsAdapter
from common.adapters.rail_access import RailAccessAdapter
from common.adapters.satellite_vision import SatelliteVisionAdapter
from common.adapters.web_search import WebSearchAdapter
from common.day_subject import MAX_SUBJECTS_PER_FINDING

REGION = "ap-southeast-2"


def _tracked(
    text,
    *,
    model_id="au.anthropic.claude-haiku-4-5-20251001-v1:0",
    input_tokens=120,
    output_tokens=45,
    used_fallback=False,
    stop_reason="end_turn",
):
    """What common.bedrock.invoke_model_tracked returns."""
    return {
        "text": text,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": used_fallback,
        "stop_reason": stop_reason,
        "attempts": 1,
    }



@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    monkeypatch.setenv("CANDIDATE_IDEAS_TABLE", "CandidateIdeas")
    monkeypatch.setenv("ARTICLES_TABLE", "Articles")
    monkeypatch.setenv("MODERATION_QUEUE_TABLE", "ModerationQueue")
    monkeypatch.setenv("MODEL_CONFIG_TABLE", "ModelConfig")
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    # research_tick_handler / common.dynamo cache boto3 clients/resources at
    # module scope -- reset them so each test gets one bound to moto's mock.
    research_tick_handler._s3_client = None
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture(autouse=True)
def _no_interval_recording(monkeypatch):
    """These tests run several ticks back to back to test diffing. Recording each
    check would make the second one `not_due`; the interval has its own tests
    (test_research_interval.py)."""
    monkeypatch.setattr(research_tick_handler, "set_topic_last_research_at", lambda topic_id, ts: None)


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Findings",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "captured_at", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "captured_at", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="ModelConfig",
            KeySchema=[{"AttributeName": "config_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "config_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket="bloggerbear-content-test",
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )

        table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
        table.put_item(
            Item={
                "topic_id": "github-trending-python",
                "name": "Python trending repos",
                "adapter": "github_trending",
                "adapter_config": {"language": "python"},
                "is_financial": False,
            }
        )

        yield


def _repo(name: str, stars: int) -> dict:
    return {
        "name": name,
        "url": f"https://github.com/{name}",
        "description": "",
        "stars": stars,
        "language": None,
    }


def test_first_tick_is_always_material_and_calls_bedrock(aws_resources, monkeypatch):
    new_state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: new_state)

    tracked = _tracked("Claude summary here")
    with patch("research_tick_handler.invoke_model_tracked", return_value=tracked) as mock_invoke:
        result = research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    mock_invoke.assert_called_once()
    assert result == {"status": "material_change", "summary": "Claude summary here"}

    findings_table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
    items = findings_table.scan()["Items"]
    assert len(items) == 1
    finding = items[0]
    assert finding["topic_id"] == "github-trending-python"
    assert finding["captured_at"] == "2026-09-13T00:00:00+00:00"
    assert finding["summary"] == "Claude summary here"
    assert finding["raw_snapshot_s3_key"] == "snapshots/github-trending-python/2026-09-13T00:00:00+00:00.json"
    assert finding["source_refs"] == [
        {"url": "https://github.com/a/b", "title": "a/b", "accessed_at": "2026-09-13T00:00:00+00:00"}
    ]
    # The Bedrock call behind the summary is kept on the Finding, so its cost can be
    # bundled into the article this Finding later feeds.
    assert finding["research_call"] == {
        "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
        "input_tokens": 120,
        "output_tokens": 45,
        "used_fallback": False,
        "stop_reason": "end_turn",
    }

    s3 = boto3.client("s3", region_name=REGION)
    stored = s3.get_object(Bucket="bloggerbear-content-test", Key=finding["raw_snapshot_s3_key"])
    today = datetime.now(UTC).date().isoformat()
    # the snapshot also carries which items have now been reported (for the next tick)
    assert json.loads(stored["Body"].read()) == {**new_state, "_seen": {"a/b": today}}


def test_research_runs_on_the_topics_current_model_with_its_fallback(aws_resources, monkeypatch):
    """The topic's model_id (rolled forward after each publish) and fallback win over the
    Terraform default, same as the daily cycle."""
    boto3.resource("dynamodb", region_name=REGION).Table("Topics").update_item(
        Key={"topic_id": "github-trending-python"},
        UpdateExpression="SET model_id = :m, fallback_model_id = :f",
        ExpressionAttributeValues={":m": "deepseek.v3-v1:0", ":f": "amazon.nova-micro-v1:0"},
    )
    new_state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: new_state)

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    assert mock_invoke.call_args.args[1] == "deepseek.v3-v1:0"
    assert mock_invoke.call_args.kwargs["fallback_model_id"] == "amazon.nova-micro-v1:0"


def test_research_falls_back_to_the_model_config_default_then_the_env_var(aws_resources, monkeypatch):
    new_state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: new_state)

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)
    assert mock_invoke.call_args.args[1] == "anthropic.claude-3-haiku-20240307-v1:0"
    assert mock_invoke.call_args.kwargs["fallback_model_id"] is None

    boto3.resource("dynamodb", region_name=REGION).Table("ModelConfig").put_item(
        Item={"config_id": "default", "model_id": "mistral.magistral-small-2509", "fallback_model_id": None}
    )
    new_state = {"repos": [_repo("c/d", 5)], "fetched_at": "2026-09-13T01:00:00+00:00"}
    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)
    assert mock_invoke.call_args.args[1] == "mistral.magistral-small-2509"


def test_no_material_change_skips_bedrock_and_does_not_write_finding(aws_resources, monkeypatch):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("unused")) as mock_invoke:
        first = research_tick_handler.handler({"topic_id": "github-trending-python"}, None)
        assert first["status"] == "material_change"

        # Second tick observes the exact same state -> no material change.
        same_state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T01:00:00+00:00"}
        monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: same_state)

        mock_invoke.reset_mock()
        second = research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    assert second == {"status": "no_change"}
    mock_invoke.assert_not_called()

    findings_table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
    items = findings_table.scan()["Items"]
    assert len(items) == 1  # only the first tick's finding


def test_missing_topic_id_returns_error():
    result = research_tick_handler.handler({}, None)
    assert result == {"status": "error", "reason": "event missing required 'topic_id'"}


def test_none_event_returns_error():
    result = research_tick_handler.handler(None, None)
    assert result == {"status": "error", "reason": "event missing required 'topic_id'"}


def test_unhandled_exception_returns_error_dict_not_raised(aws_resources):
    # Bugfix regression guard: a transient failure deep in the flow (here,
    # the adapter's fetch_state blowing up) must never propagate out of
    # handler() unhandled -- every other handler in this codebase
    # guarantees this, and research_tick_handler.py used to be the one
    # exception.
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(
        Item={
            "topic_id": "flaky-topic",
            "name": "Flaky",
            "adapter": "github_trending",
            "adapter_config": {},
            "is_financial": False,
        }
    )

    with patch.object(GitHubTrendingAdapter, "fetch_state", side_effect=RuntimeError("network blip")):
        result = research_tick_handler.handler({"topic_id": "flaky-topic"}, None)

    assert result == {"status": "error", "topic_id": "flaky-topic", "reason": "network blip"}


def test_unknown_topic_returns_error(aws_resources):
    result = research_tick_handler.handler({"topic_id": "does-not-exist"}, None)
    assert result == {"status": "error", "reason": "unknown topic_id: does-not-exist"}


def test_unknown_adapter_returns_error(aws_resources):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(
        Item={
            "topic_id": "mystery-topic",
            "name": "Mystery",
            "adapter": "not_a_real_adapter",
            "adapter_config": {},
            "is_financial": False,
        }
    )

    result = research_tick_handler.handler({"topic_id": "mystery-topic"}, None)

    assert result == {"status": "error", "reason": "unknown adapter: not_a_real_adapter"}


def test_adapter_registry_has_every_registered_adapter():
    # Adding a domain is a new registry entry, never a change to the flow.
    assert research_tick_handler.ADAPTER_REGISTRY == {
        "github_trending": GitHubTrendingAdapter,
        "hacker_news": HackerNewsAdapter,
        "crypto_feed": CryptoFeedAdapter,
        "web_search": WebSearchAdapter,
        "satellite_vision": SatelliteVisionAdapter,
        "rail_access": RailAccessAdapter,
    }


def _put_topic(topic_id, adapter, adapter_config=None):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    table.put_item(
        Item={
            "topic_id": topic_id,
            "name": topic_id.title(),
            "adapter": adapter,
            "adapter_config": adapter_config or {},
            "is_financial": False,
        }
    )


def test_an_adapter_supplied_summary_prompt_replaces_the_generic_one(aws_resources, monkeypatch):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)
    monkeypatch.setattr(
        GitHubTrendingAdapter, "build_summary_prompt", lambda self, topic, diff, new: "ADAPTER PROMPT"
    )

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    assert mock_invoke.call_args.args[0] == "ADAPTER PROMPT"


def test_adapters_without_a_prompt_get_the_generic_one(aws_resources, monkeypatch):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    prompt = mock_invoke.call_args.args[0]
    assert 'monitoring the topic "Python trending repos"' in prompt
    assert "do not give financial or investment advice" in prompt
    # The research summary is anchored to the active topic, so off-topic
    # noise in the source data is ignored rather than summarized.
    assert "RELEVANCE RULE: this digest covers 'Python trending repos' and nothing else" in prompt
    assert prompt.index("Current state") < prompt.index("RELEVANCE RULE")
    assert prompt.index("RELEVANCE RULE") < prompt.index("Summarize only what is new")


def test_opt_in_adapters_receive_the_prior_snapshot_others_do_not(aws_resources, monkeypatch):
    _put_topic("crypto-topic", "crypto_feed")
    seen = []

    def fake_fetch(self, topic_config, previous_state=None):
        seen.append(previous_state)
        return {
            "fetched_at": f"2026-09-13T0{len(seen)}:00:00+00:00",
            "editorial_goal": "ALTCOIN_DEEP_DIVE",
            "market_anchors": {},
            "analyzed_pool": [],
        }

    monkeypatch.setattr(CryptoFeedAdapter, "fetch_state", fake_fetch)
    monkeypatch.setattr(CryptoFeedAdapter, "material_diff", lambda self, old, new: (True, "d"))

    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")):
        research_tick_handler.handler({"topic_id": "crypto-topic"}, None)
        research_tick_handler.handler({"topic_id": "crypto-topic"}, None)

    assert seen[0] is None
    assert seen[1]["fetched_at"] == "2026-09-13T01:00:00+00:00"  # the first tick's stored snapshot


def test_a_generic_web_search_topic_runs_end_to_end(aws_resources):
    _put_topic("ai-news", "web_search", {"queries": ["ai regulation"], "max_age_hours": 12})
    result_item = {
        "title": "New AI rules",
        "url": "https://news.example/ai",
        "source": "news.example",
        "published_at": "2026-09-13T00:00:00+00:00",
        "snippet": None,
    }

    with (
        patch("common.adapters.web_search.search_web", return_value=[result_item]),
        patch(
            "research_tick_handler.invoke_model_tracked", return_value=_tracked("AI summary")
        ) as mock_invoke,
    ):
        result = research_tick_handler.handler({"topic_id": "ai-news"}, None)

    assert result == {"status": "material_change", "summary": "AI summary"}
    assert 'monitoring the topic "Ai-News"' in mock_invoke.call_args.args[0]
    assert "this digest covers 'Ai-News' and nothing else" in mock_invoke.call_args.args[0]
    finding = boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"][0]
    assert [ref["url"] for ref in finding["source_refs"]] == ["https://news.example/ai"]


# --- hierarchical editorial goals in the research summary --------------------------


def _summary_prompt(topic_id, adapter, adapter_config=None, editorial_goals=None):
    _put_topic(topic_id, adapter, adapter_config)
    if editorial_goals is not None:
        table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
        item = table.get_item(Key={"topic_id": topic_id})["Item"]
        table.put_item(Item={**item, "editorial_goals": editorial_goals})
    result_item = {
        "title": "Zero-day exploited in the wild",
        "url": "https://news.example/z",
        "source": "news.example",
        "published_at": "2026-09-13T00:00:00+00:00",
        "snippet": None,
    }
    with (
        patch("common.adapters.web_search.search_web", return_value=[result_item]),
        patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")) as mock_invoke,
    ):
        research_tick_handler.handler({"topic_id": topic_id}, None)
    return mock_invoke.call_args.args[0]


def test_a_topic_specific_goal_drives_the_research_summary(aws_resources):
    prompt = _summary_prompt(
        "sec",
        "web_search",
        {"query": "zero-day"},
        editorial_goals={"primary_focus": "Track zero-days.", "exclusion_criteria": "No marketing."},
    )

    assert (
        "OPERATIONAL EDITORIAL GOAL:\nTopic-Specific Focus: Track zero-days.\n"
        "Strict Constraints: No marketing."
    ) in prompt
    assert "Global Default Goal" not in prompt
    assert "why it is highly relevant based on the Operational Editorial Goal above" in prompt


def test_a_bare_topic_summarises_through_independent_web_research(aws_resources):
    prompt = _summary_prompt("bare", "web_search", {"query": "zero-day"})

    assert "OPERATIONAL EDITORIAL GOAL:\nGlobal Default Goal: Execute independent web research" in prompt


def test_the_goal_block_precedes_the_data_and_the_safety_lines_survive(aws_resources):
    prompt = _summary_prompt("bare", "web_search", {"query": "zero-day"})

    assert prompt.index("OPERATIONAL EDITORIAL GOAL") < prompt.index("What's new:")
    assert prompt.index("What's new:") < prompt.index("Current state")
    assert "RELEVANCE RULE: this digest covers" in prompt
    assert "do not give financial or investment advice" in prompt


def test_a_topic_without_a_configured_query_researches_its_own_name(aws_resources):
    boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(
        Item={
            "topic_id": "sec-threats",
            "name": "Cybersecurity & Infrastructure Threats",
            "adapter": "web_search",
            "adapter_config": {},
            "is_financial": False,
        }
    )
    with (
        patch("common.adapters.web_search.search_web", return_value=[]) as mock_search,
        patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("ok")),
    ):
        research_tick_handler.handler({"topic_id": "sec-threats"}, None)

    assert mock_search.call_args.args[0] == "(Cybersecurity OR Infrastructure OR Threats)"


# --- what counts as new: judged against everything already reported ---------


def _tick(monkeypatch, repos, fetched_at):
    state = {"repos": [_repo(n, s) for n, s in repos], "fetched_at": fetched_at}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)
    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("summary")) as mock_invoke:
        result = research_tick_handler.handler({"topic_id": "github-trending-python"}, None)
    return result, mock_invoke


def test_an_item_that_drops_out_and_returns_is_not_reported_again(aws_resources, monkeypatch):
    result, _ = _tick(monkeypatch, [("a/b", 10), ("c/d", 10)], "2026-09-13T01:00:00+00:00")
    assert result["status"] == "material_change"

    # e/f is new, so this tick is stored -- and c/d is no longer in the list
    result, _ = _tick(monkeypatch, [("a/b", 10), ("e/f", 10)], "2026-09-13T02:00:00+00:00")
    assert result["status"] == "material_change"

    # c/d is back. The last stored snapshot no longer lists it, but it was reported.
    result, mock_invoke = _tick(
        monkeypatch, [("a/b", 10), ("e/f", 10), ("c/d", 10)], "2026-09-13T03:00:00+00:00"
    )
    assert result == {"status": "no_change"}
    mock_invoke.assert_not_called()


def test_a_genuinely_new_item_is_reported_and_only_it_is_asked_about(aws_resources, monkeypatch):
    _tick(monkeypatch, [("a/b", 10)], "2026-09-13T01:00:00+00:00")

    result, mock_invoke = _tick(monkeypatch, [("a/b", 10), ("x/y", 10)], "2026-09-13T02:00:00+00:00")

    assert result["status"] == "material_change"
    prompt = mock_invoke.call_args.args[0]
    assert "What's new: entered: x/y" in prompt
    assert "_seen" not in prompt


def test_the_generic_prompt_reports_only_what_is_new_and_forbids_invention(aws_resources, monkeypatch):
    _, mock_invoke = _tick(monkeypatch, [("a/b", 10)], "2026-09-13T01:00:00+00:00")

    prompt = mock_invoke.call_args.args[0]
    assert "Summarize only what is new" in prompt
    assert "Do not restate items that are not listed as new" in prompt
    assert "never add causes, quotes, numbers or detail that it does not contain" in prompt
    # nothing domain-specific leaks into a topic that isn't that domain
    assert "crypto" not in prompt.lower() and "bitcoin" not in prompt.lower()


def test_the_seen_set_is_stored_in_the_snapshot_and_carried_forward(aws_resources, monkeypatch):
    _tick(monkeypatch, [("a/b", 10)], "2026-09-13T01:00:00+00:00")
    _tick(monkeypatch, [("c/d", 10)], "2026-09-13T02:00:00+00:00")

    s3 = boto3.client("s3", region_name=REGION)
    key = "snapshots/github-trending-python/2026-09-13T02:00:00+00:00.json"
    stored = json.loads(s3.get_object(Bucket="bloggerbear-content-test", Key=key)["Body"].read())

    assert set(stored["_seen"]) == {"a/b", "c/d"}  # a/b is no longer listed but is remembered


class _KeyedAdapter(GitHubTrendingAdapter):
    def item_keys(self, state):
        return set(state.get("keys", []))


def test_merge_seen_forgets_items_older_than_the_retention_window():
    today = date(2026, 9, 20)
    old_state = {"_seen": {"stale": "2026-09-01", "recent": "2026-09-18"}}

    seen = research_tick_handler._merge_seen(_KeyedAdapter(), old_state, {"keys": ["fresh"]}, today)

    assert seen == {"recent": "2026-09-18", "fresh": "2026-09-20"}


def test_merge_seen_keeps_the_first_seen_date_of_an_item_that_returns():
    seen = research_tick_handler._merge_seen(
        _KeyedAdapter(), {"_seen": {"a": "2026-09-18"}}, {"keys": ["a"]}, date(2026, 9, 20)
    )

    assert seen == {"a": "2026-09-18"}


def test_merge_seen_treats_a_snapshot_stored_before_the_seen_set_as_reported():
    seen = research_tick_handler._merge_seen(
        _KeyedAdapter(), {"keys": ["old"]}, {"keys": ["new"]}, date(2026, 9, 20)
    )

    assert seen == {"old": "2026-09-20", "new": "2026-09-20"}


def test_merge_seen_is_capped_dropping_the_oldest_first(monkeypatch):
    monkeypatch.setattr(research_tick_handler, "SEEN_MAX_KEYS", 2)
    old_state = {"_seen": {"a": "2026-09-16", "b": "2026-09-19"}}

    seen = research_tick_handler._merge_seen(_KeyedAdapter(), old_state, {"keys": ["c"]}, date(2026, 9, 20))

    assert seen == {"b": "2026-09-19", "c": "2026-09-20"}


def test_an_adapter_with_no_item_keys_gets_no_seen_set(aws_resources, monkeypatch):
    monkeypatch.setattr(GitHubTrendingAdapter, "item_keys", lambda self, state: set())
    _tick(monkeypatch, [("a/b", 10)], "2026-09-13T01:00:00+00:00")

    s3 = boto3.client("s3", region_name=REGION)
    key = "snapshots/github-trending-python/2026-09-13T01:00:00+00:00.json"
    stored = json.loads(s3.get_object(Bucket="bloggerbear-content-test", Key=key)["Body"].read())

    assert "_seen" not in stored


# --- a summary that runs out of tokens -------------------------------------------------------------


def _run_one_tick(monkeypatch, tracked):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)
    with patch("research_tick_handler.invoke_model_tracked", return_value=tracked) as mock_invoke:
        result = research_tick_handler.handler({"topic_id": "github-trending-python"}, None)
    return result, mock_invoke


def test_the_summary_is_given_room_and_one_automatic_retry(aws_resources, monkeypatch):
    _, mock_invoke = _run_one_tick(monkeypatch, _tracked("a summary"))

    kwargs = mock_invoke.call_args.kwargs
    assert kwargs["max_tokens"] == research_tick_handler.RESEARCH_MAX_TOKENS == 2048
    assert kwargs["retry_max_tokens"] == research_tick_handler.RESEARCH_RETRY_MAX_TOKENS == 4096


def test_a_summary_cut_off_even_after_the_retry_is_kept_and_said_so(aws_resources, monkeypatch, capsys):
    result, _ = _run_one_tick(monkeypatch, _tracked("the summary stops mid-sen", stop_reason="max_tokens"))

    assert result["status"] == "material_change"  # what was found is not thrown away
    finding = boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"][0]
    assert finding["summary"] == "the summary stops mid-sen"
    assert finding["research_call"]["stop_reason"] == "max_tokens"
    assert "was cut off at its token limit" in capsys.readouterr().out


def test_a_complete_summary_says_nothing_about_being_cut_off(aws_resources, monkeypatch, capsys):
    _run_one_tick(monkeypatch, _tracked("all of it"))

    assert "cut off" not in capsys.readouterr().out


# --- figures (common/figures.py) ------------------------------------------------------------------


class _DrawingAdapter(GitHubTrendingAdapter):
    """An adapter that draws: one good figure, and one whose key reaches outside the bucket."""

    def figures(self, new_state):
        return [
            {"key": "vision/t/s/scene.png", "caption": "A map", "alt": "The map, described", "extra": 1},
            {"key": "../outside.png", "caption": "Bad", "alt": "bad"},
        ]


class _ProlificAdapter(GitHubTrendingAdapter):
    """An adapter that draws more figures than a Finding may carry."""

    def figures(self, new_state):
        return [
            {"key": f"vision/t/s/scene-{n}.png", "caption": f"Map {n}", "alt": f"Map {n}"} for n in range(14)
        ]


def _the_finding():
    (finding,) = boto3.resource("dynamodb", region_name=REGION).Table("Findings").scan()["Items"]
    return finding


def test_an_adapters_figures_are_stored_on_the_finding_well_formed_ones_only(aws_resources, monkeypatch):
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", _DrawingAdapter)

    result, _ = _run_one_tick(monkeypatch, _tracked("a summary"))

    assert result["status"] == "material_change"
    assert _the_finding()["figures"] == [
        {"key": "vision/t/s/scene.png", "caption": "A map", "alt": "The map, described"}
    ]


def test_a_finding_carries_at_most_the_figure_cap(aws_resources, monkeypatch):
    """A Findings item is capped at 400 KB; an adapter that draws without limit is cut at the cap,
    the first ones kept (common/figures.py MAX_FIGURES_PER_FINDING)."""
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", _ProlificAdapter)

    _run_one_tick(monkeypatch, _tracked("a summary"))

    stored = _the_finding()["figures"]
    assert len(stored) == research_tick_handler.MAX_FIGURES_PER_FINDING == 10
    assert [f["key"] for f in stored][:2] == ["vision/t/s/scene-0.png", "vision/t/s/scene-1.png"]


def test_an_adapter_draws_nothing_unless_it_says_so_and_its_finding_has_no_figures_field(
    aws_resources, monkeypatch
):
    assert GitHubTrendingAdapter().figures({"repos": []}) == []  # the base class's default

    _run_one_tick(monkeypatch, _tracked("a summary"))

    assert "figures" not in _the_finding()


# --- planner notes (Adapter.planner_notes) --------------------------------------------------------


class _NotingAdapter(GitHubTrendingAdapter):
    """An adapter with more rule-based notes than a Finding keeps, one carrying a number."""

    def planner_notes(self, new_state):
        notes = [{"kind": "flag", "type": "t", "stations": [f"s{n}"], "text": f"Note {n}."} for n in range(9)]
        notes[0]["ratio"] = 2.5
        return [*notes[:1], "not a note", *notes[1:]]


class _BrokenNotesAdapter(GitHubTrendingAdapter):
    def planner_notes(self, new_state):
        raise RuntimeError("the rules broke")


class _NanNotesAdapter(GitHubTrendingAdapter):
    def planner_notes(self, new_state):
        return [{"kind": "flag", "type": "t", "stations": [], "text": "Odd.", "ratio": float("nan")}]


def test_an_adapters_planner_notes_are_stored_on_the_finding_capped_and_dynamodb_ready(
    aws_resources, monkeypatch
):
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", _NotingAdapter)

    result, _ = _run_one_tick(monkeypatch, _tracked("a summary"))

    assert result["status"] == "material_change"
    stored = _the_finding()["planner_notes"]
    assert len(stored) == research_tick_handler.MAX_PLANNER_NOTES_PER_FINDING == 7
    assert [note["text"] for note in stored] == [f"Note {n}." for n in range(7)]  # dicts only, in order
    assert stored[0]["ratio"] == Decimal("2.5") and stored[0]["stations"] == ["s0"]


@pytest.mark.parametrize("adapter_class", [_BrokenNotesAdapter, _NanNotesAdapter])
def test_notes_that_fail_or_cannot_be_stored_cost_the_notes_never_the_finding(
    aws_resources, monkeypatch, capsys, adapter_class
):
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", adapter_class)

    result, _ = _run_one_tick(monkeypatch, _tracked("a summary"))

    assert result["status"] == "material_change"
    finding = _the_finding()
    assert finding["summary"] == "a summary" and "planner_notes" not in finding
    assert "planner notes dropped" in capsys.readouterr().out


def test_an_adapter_has_no_planner_notes_unless_it_says_so_and_its_finding_has_no_such_field(
    aws_resources, monkeypatch
):
    assert GitHubTrendingAdapter().planner_notes({"repos": []}) == []  # the base class's default

    _run_one_tick(monkeypatch, _tracked("a summary"))

    assert "planner_notes" not in _the_finding()


# --- the subjects an observation reports (Adapter.subjects, common/day_subject.py) ----------------


class _SubjectsAdapter(GitHubTrendingAdapter):
    """An adapter that reports more subjects than a Finding keeps, with repeats and junk among them."""

    def subjects(self, new_state):
        return ["sydney", "sydney", "not a key", 7, "melbourne", *[f"city-{n}" for n in range(12)]]


class _BrokenSubjectsAdapter(GitHubTrendingAdapter):
    def subjects(self, new_state):
        raise RuntimeError("the rota broke")


def test_an_adapters_subjects_are_stored_on_the_finding_cleaned_and_capped(aws_resources, monkeypatch):
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", _SubjectsAdapter)

    result, _ = _run_one_tick(monkeypatch, _tracked("a summary"))

    assert result["status"] == "material_change"
    stored = _the_finding()["subjects"]
    assert len(stored) == MAX_SUBJECTS_PER_FINDING == 10
    assert stored == ["sydney", "melbourne", *[f"city-{n}" for n in range(8)]]  # plain keys, once, in order


def test_subjects_that_fail_cost_the_subjects_never_the_finding(aws_resources, monkeypatch, capsys):
    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", _BrokenSubjectsAdapter)

    result, _ = _run_one_tick(monkeypatch, _tracked("a summary"))

    assert result["status"] == "material_change"
    finding = _the_finding()
    assert finding["summary"] == "a summary" and "subjects" not in finding
    assert "subjects dropped: RuntimeError" in capsys.readouterr().out


def test_an_adapter_reports_no_subjects_unless_it_says_so_and_its_finding_has_no_such_field(
    aws_resources, monkeypatch
):
    for adapter_class in research_tick_handler.ADAPTER_REGISTRY.values():
        assert adapter_class.subjects is Adapter.subjects  # no registered adapter rotates yet (S2)
    assert GitHubTrendingAdapter().subjects({"repos": []}) == []  # the base class's default

    _run_one_tick(monkeypatch, _tracked("a summary"))

    assert "subjects" not in _the_finding()


# --- the evidence line (common/evidence.py's research_tick) --------------------------------------


class Context:
    aws_request_id = "7c1d2e3f-0000-4000-8000-00000000abcd"


def tick_lines(capsys):
    out = capsys.readouterr().out
    return [evidence.parse(line) for line in out.splitlines() if line.startswith(evidence.PREFIX)]


def _github_tick(monkeypatch, state, context=Context(), **event):
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)
    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("a summary")):
        return research_tick_handler.handler({"topic_id": "github-trending-python", **event}, context)


def test_every_invocation_leaves_one_research_tick_line_with_its_outcome(aws_resources, monkeypatch, capsys):
    """One line per invocation, in a finally: the tick's ids, the adapter as typed, the outcome,
    whether anything was stored, the Finding's time when one was written, and the returns are
    what they always were."""
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    result = _github_tick(monkeypatch, state)
    assert result["status"] == "material_change"
    (line,) = tick_lines(capsys)
    assert line["event"] == "research_tick" and line["tick"] == Context.aws_request_id
    assert line["topic_id"] == "github-trending-python" and line["adapter"] == "github_trending"
    assert line["outcome"] == "material_change" and line["stored"] is True
    assert line["finding"] == state["fetched_at"] and line["decisions"] == 0 and line["triages"] == 0
    assert line["force"] is False and line["ms"] >= 0 and "error_type" not in line
    assert len(line["run"]) == 12
    first_run = line["run"]

    # Nothing new: no Finding, nothing stored (this adapter keeps no running state), a new run.
    result = _github_tick(monkeypatch, state, force=True)
    assert result == {"status": "no_change"}
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "no_change" and line["stored"] is False and "finding" not in line
    assert line["force"] is True and line["run"] != first_run

    # Not due: the schedule's heartbeat found the interval not elapsed.
    not_due = {"status": "not_due", "topic_id": "github-trending-python", "interval_hours": 2,
               "next_due_at": None}  # fmt: skip
    monkeypatch.setattr(research_tick_handler, "_not_due", lambda topic, now: not_due)
    assert _github_tick(monkeypatch, state) == not_due
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "not_due" and "adapter" not in line and line["stored"] is False

    # Without a Lambda context (a test, an in-process run) there is no tick id, and still a line.
    assert _github_tick(monkeypatch, state, context=None) == not_due
    (line,) = tick_lines(capsys)
    assert "tick" not in line and line["outcome"] == "not_due"


def test_a_failed_paused_or_unknown_tick_says_so_in_its_line(aws_resources, monkeypatch, capsys):
    table = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    for topic_id, adapter, extra in (
        ("flaky-topic", "github_trending", {}),
        ("mystery-topic", "not_a_real_adapter", {}),
        ("resting-topic", "github_trending", {"paused": True}),
    ):
        table.put_item(Item={"topic_id": topic_id, "name": "T", "adapter": adapter, "adapter_config": {},
                             "is_financial": False, **extra})  # fmt: skip

    with patch.object(GitHubTrendingAdapter, "fetch_state", side_effect=RuntimeError("network blip")):
        result = research_tick_handler.handler({"topic_id": "flaky-topic"}, Context())
    assert result == {"status": "error", "topic_id": "flaky-topic", "reason": "network blip"}
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "error" and line["error_type"] == "RuntimeError" and line["stored"] is False
    assert line["adapter"] == "github_trending" and "network blip" not in json.dumps(line)

    assert research_tick_handler.handler({"topic_id": "does-not-exist"}, Context()) == {
        "status": "error", "reason": "unknown topic_id: does-not-exist",
    }  # fmt: skip
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "unknown_topic" and line["topic_id"] == "does-not-exist"
    assert "adapter" not in line

    result = research_tick_handler.handler({"topic_id": "mystery-topic"}, Context())
    assert result == {"status": "error", "reason": "unknown adapter: not_a_real_adapter"}
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "unknown_adapter" and "adapter" not in line  # never a key not in the registry

    assert research_tick_handler.handler({"topic_id": "resting-topic"}, Context())["status"] == "paused"
    (line,) = tick_lines(capsys)
    assert line["outcome"] == "paused" and line["stored"] is False

    # An event with no topic id is refused before anything is bound: no line.
    assert research_tick_handler.handler({}, Context())["status"] == "error"
    assert tick_lines(capsys) == []


def test_a_running_state_stored_on_a_quiet_tick_is_said_so(aws_resources, monkeypatch, capsys):
    """An adapter that keeps a running state stores it on a quiet tick too: `stored` is true with
    no Finding, which is how a reader tells a kept decision from a lost one."""

    class Keeps(GitHubTrendingAdapter):
        keeps_running_state = True

        def fetch_state(self, topic_config, previous_state=None):
            return {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}

    monkeypatch.setitem(research_tick_handler.ADAPTER_REGISTRY, "github_trending", Keeps)
    with patch("research_tick_handler.invoke_model_tracked", return_value=_tracked("a summary")):
        first = research_tick_handler.handler({"topic_id": "github-trending-python"}, Context())
        second = research_tick_handler.handler({"topic_id": "github-trending-python"}, Context())
    assert first["status"] == "material_change" and second == {"status": "no_change"}
    lines = tick_lines(capsys)
    assert [(line["outcome"], line["stored"], "finding" in line) for line in lines] == [
        ("material_change", True, True), ("no_change", True, False),
    ]  # fmt: skip


# --- the prefetch_network event (the Admin API's network_prefetch trigger) -----------------------


def _put_rail_topic(paused=False):
    item = {
        "topic_id": "trains",
        "name": "Trains",
        "adapter": "rail_access",
        "adapter_config": {
            "sites": [{"id": "sydney", "name": "Sydney", "polygon": [
                [Decimal(lon), Decimal(lat)]
                for lon, lat in (("150.86", "-34.16"), ("151.56", "-34.16"),
                                 ("151.56", "-33.58"), ("150.86", "-33.58"))
            ]}],
        },  # fmt: skip
        "is_financial": False,
    }
    if paused:
        item["paused"] = True
    boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(Item=item)


def _overpass_network(polygon, *, now=None, **kwargs):
    return {"source": "overpass", "fetched_at": now.isoformat(), "query_url": "https://overpass-api.de/x",
            "ways": [], "stations": [], "pois": [],
            "counts": {"ways": 0, "stations": 0, "pois": 0, "points": 0}, "truncated": False}  # fmt: skip


def test_a_prefetch_event_stores_the_network_in_s3_and_records_no_research(aws_resources, capsys):
    from common import vision_sites

    _put_rail_topic()
    with patch.object(RailAccessAdapter, "fetch_network", staticmethod(_overpass_network)):
        result = research_tick_handler.handler({"topic_id": "trains", "prefetch_network": True}, None)

    assert result["status"] == "network_prefetched" and result["topic_id"] == "trains"
    assert [site["status"] for site in result["sites"]] == ["stored"]
    stored = vision_sites.load_network("trains", "sydney")
    assert stored["site_key"] and stored["fetched_at"] == result["sites"][0]["fetched_at"]
    assert vision_sites.load_network("trains", "melbourne") is None  # a missing object is None
    topics = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
    topic = topics.get_item(Key={"topic_id": "trains"})
    assert "last_research_at" not in topic["Item"]
    s3 = boto3.client("s3", region_name=REGION)
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="bloggerbear-content-test").get("Contents", [])]
    assert keys == [vision_sites.network_key("trains", "sydney")]  # no running state, no snapshot
    assert "network_prefetch trains/sydney: stored" in capsys.readouterr().out


def test_a_prefetch_event_refuses_a_paused_topic_or_one_with_no_network(aws_resources):
    _put_rail_topic(paused=True)
    with patch.object(RailAccessAdapter, "fetch_network") as fetch:
        result = research_tick_handler.handler({"topic_id": "trains", "prefetch_network": True}, None)
    assert result == {"status": "paused", "topic_id": "trains"} and not fetch.called

    boto3.resource("dynamodb", region_name=REGION).Table("Topics").put_item(
        Item={"topic_id": "gh", "name": "GH", "adapter": "github_trending", "adapter_config": {},
              "is_financial": False}  # fmt: skip
    )
    result = research_tick_handler.handler({"topic_id": "gh", "prefetch_network": True}, None)
    assert result == {"status": "error", "reason": "adapter github_trending has no network to prefetch"}
    result = research_tick_handler.handler({"topic_id": "nope", "prefetch_network": True}, None)
    assert result == {"status": "error", "reason": "unknown topic_id: nope"}
