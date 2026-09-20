from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import research_tick_handler
from common.adapters.crypto_feed import CryptoFeedAdapter
from common.adapters.github_trending import GitHubTrendingAdapter
from common.adapters.hacker_news import HackerNewsAdapter
from common.adapters.web_search import WebSearchAdapter

REGION = "ap-southeast-2"


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
    monkeypatch.setenv("CONTENT_BUCKET", "bloggerbear-content-test")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
    # research_tick_handler / common.dynamo cache boto3 clients/resources at
    # module scope -- reset them so each test gets one bound to moto's mock.
    research_tick_handler._s3_client = None
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


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

    with patch("research_tick_handler.invoke_claude", return_value="Claude summary here") as mock_invoke:
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

    s3 = boto3.client("s3", region_name=REGION)
    stored = s3.get_object(Bucket="bloggerbear-content-test", Key=finding["raw_snapshot_s3_key"])
    assert json.loads(stored["Body"].read()) == new_state


def test_no_material_change_skips_bedrock_and_does_not_write_finding(aws_resources, monkeypatch):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)

    with patch("research_tick_handler.invoke_claude", return_value="unused") as mock_invoke:
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

    with patch("research_tick_handler.invoke_claude", return_value="ok") as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    assert mock_invoke.call_args.args[0] == "ADAPTER PROMPT"


def test_adapters_without_a_prompt_get_the_generic_one(aws_resources, monkeypatch):
    state = {"repos": [_repo("a/b", 100)], "fetched_at": "2026-09-13T00:00:00+00:00"}
    monkeypatch.setattr(GitHubTrendingAdapter, "fetch_state", lambda self, topic_config: state)

    with patch("research_tick_handler.invoke_claude", return_value="ok") as mock_invoke:
        research_tick_handler.handler({"topic_id": "github-trending-python"}, None)

    prompt = mock_invoke.call_args.args[0]
    assert 'monitoring the topic "Python trending repos"' in prompt
    assert "do not give financial or investment advice" in prompt
    # The research summary is anchored to the active topic, so off-topic
    # noise in the source data is ignored rather than summarized.
    assert "RELEVANCE RULE: this digest covers 'Python trending repos' and nothing else" in prompt
    assert prompt.index("Current state") < prompt.index("RELEVANCE RULE")
    assert prompt.index("RELEVANCE RULE") < prompt.index("Summarize what changed")


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

    with patch("research_tick_handler.invoke_claude", return_value="ok"):
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
        patch("research_tick_handler.invoke_claude", return_value="AI summary") as mock_invoke,
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
        patch("research_tick_handler.invoke_claude", return_value="ok") as mock_invoke,
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

    assert prompt.index("OPERATIONAL EDITORIAL GOAL") < prompt.index("What changed:")
    assert prompt.index("What changed:") < prompt.index("Current state")
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
        patch("research_tick_handler.invoke_claude", return_value="ok"),
    ):
        research_tick_handler.handler({"topic_id": "sec-threats"}, None)

    assert mock_search.call_args.args[0] == "(Cybersecurity OR Infrastructure OR Threats)"
