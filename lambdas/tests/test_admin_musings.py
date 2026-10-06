"""Fixing a musing after it went out (the Admin API's /musings routes, common/musings.py's
regenerate_article_musing_text): listing them, replacing one's text by hand, and having an
article musing written again. A musing is written once, at publish, so one that went out with no
text stayed blank until a person fixed it in DynamoDB."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from table_schemas import create_table

import admin_api_handler
from common import musings

REGION = "ap-southeast-2"


@pytest.fixture
def tables(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "MUSINGS_TABLE": "Musings",
        "ARTICLES_TABLE": "Articles",
        "TOPICS_TABLE": "Topics",
        "BEDROCK_MODEL_ID": "a-model",
    }.items():
        monkeypatch.setenv(key, value)
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        for name, key in (("Musings", "musing_id"), ("Articles", "article_id"), ("Topics", "topic_id")):
            create_table(
                client,
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        resource.Table("Topics").put_item(Item={"topic_id": "crypto", "name": "Crypto"})
        yield resource


def put_article(tables, article_id="a1", status="published", title="Staking Yields This Week"):
    tables.Table("Articles").put_item(
        Item={"article_id": article_id, "topic_id": "crypto", "title": title, "status": status}
    )


def put_musing(tables, musing_id, text, *, article_id="a1", kind="article", mood="proud", day=1):
    tables.Table("Musings").put_item(
        Item={
            "musing_id": musing_id,
            "kind": kind,
            "article_id": article_id,
            "topic_id": "crypto",
            "text": text,
            "mood": mood,
            "created_at": f"2026-10-{day:02d}T00:00:00+00:00",
        }
    )


def stored(tables, musing_id):
    return tables.Table("Musings").get_item(Key={"musing_id": musing_id}).get("Item")


def api(route, *, path=None, query=None, body=None, reply="Paws up, a new one is out!"):
    """One request to the Admin API, with the model answering `reply` (an exception is raised)."""
    event = {"routeKey": route, "pathParameters": path, "queryStringParameters": query}
    if body is not None:
        event["body"] = body if isinstance(body, str) else json.dumps(body)
    answer = {"side_effect": reply} if isinstance(reply, Exception) else {"return_value": reply}
    with patch("common.musings.tracked_claude", **answer) as model:
        result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"]), model


# --- musings list ---------------------------------------------------------------------------------


def test_list_shows_the_newest_first_and_says_which_are_blank(tables):
    put_musing(tables, "m1", "An old one", day=1)
    put_musing(tables, "m2", "", day=2)
    put_musing(tables, "m3", "The newest", article_id="a2", day=3)

    status, body, _ = api("GET /musings")

    assert status == 200 and body["count"] == body["matching"] == 3
    assert [(m["musing_id"], m["blank"]) for m in body["musings"]] == [
        ("m3", False),
        ("m2", True),
        ("m1", False),
    ]
    assert body["musings"][1] == {
        "musing_id": "m2",
        "kind": "article",
        "mood": "proud",
        "created_at": "2026-10-02T00:00:00+00:00",
        "edited_at": None,
        "article_id": "a1",
        "topic_id": "crypto",
        "text": "",
        "blank": True,
    }


def test_list_can_keep_the_blank_ones_or_one_articles(tables):
    put_musing(tables, "m1", "Has text", day=1)
    put_musing(tables, "m2", "  \n", day=2)
    put_musing(tables, "m3", "", article_id="a2", day=3)

    blank = api("GET /musings", query={"blank": "true"})[1]
    about = api("GET /musings", query={"article_id": "a1"})[1]
    both = api("GET /musings", query={"blank": "true", "article_id": "a1"})[1]

    assert [m["musing_id"] for m in blank["musings"]] == ["m3", "m2"]
    assert [m["musing_id"] for m in about["musings"]] == ["m2", "m1"]
    assert [m["musing_id"] for m in both["musings"]] == ["m2"]


def test_list_cuts_to_the_limit_after_filtering_and_says_how_many_matched(tables):
    for day in range(1, 6):
        put_musing(tables, f"m{day}", "" if day <= 3 else "text", day=day)

    body = api("GET /musings", query={"blank": "true", "limit": "2"})[1]

    # The two newest musings have text: the limit must not be spent on them.
    assert [m["musing_id"] for m in body["musings"]] == ["m3", "m2"]
    assert body["count"] == 2 and body["matching"] == 3


@pytest.mark.parametrize("query", [{"limit": "0"}, {"limit": "201"}, {"limit": "many"}, {"blank": "yes"}])
def test_list_refuses_a_limit_or_a_flag_it_does_not_understand(tables, query):
    assert api("GET /musings", query=query)[0] == 400


# --- musings edit ---------------------------------------------------------------------------------


def test_edit_replaces_the_text_and_nothing_else(tables):
    put_musing(tables, "m1", "", mood="thoughtful")

    status, body, model = api("PUT /musings/{musing_id}", path={"musing_id": "m1"}, body={"text": "  Mine. "})

    assert status == 200
    assert body["updated"]["text"] == "Mine." and body["updated"]["was_blank"] is True
    assert body["updated"]["blank"] is False and body["updated"]["edited_at"]
    item = stored(tables, "m1")
    assert item["text"] == "Mine." and item["edited_at"] == body["updated"]["edited_at"]
    assert (item["mood"], item["created_at"], item["article_id"], item["kind"]) == (
        "thoughtful",
        "2026-10-01T00:00:00+00:00",
        "a1",
        "article",
    )
    model.assert_not_called()


def test_edit_works_on_any_kind_of_musing(tables):
    put_musing(tables, "loot-1", "LOOT DROP!", article_id=None, kind="loot", mood="excited")

    status, body, _ = api("PUT /musings/{musing_id}", path={"musing_id": "loot-1"}, body={"text": "New!"})

    assert status == 200 and body["updated"]["was_blank"] is False
    assert stored(tables, "loot-1")["text"] == "New!"


@pytest.mark.parametrize(
    "body",
    [{}, {"text": ""}, {"text": "   "}, {"text": 5}, {"text": "x" * 281}, ["text"], "{not json"],
)
def test_edit_refuses_text_that_is_missing_empty_or_too_long(tables, body):
    put_musing(tables, "m1", "As it was")

    assert api("PUT /musings/{musing_id}", path={"musing_id": "m1"}, body=body)[0] == 400
    assert stored(tables, "m1")["text"] == "As it was"


def test_edit_takes_text_of_exactly_the_longest_a_musing_can_be(tables):
    put_musing(tables, "m1", "")
    longest = "x" * musings.MAX_MUSING_CHARS

    assert api("PUT /musings/{musing_id}", path={"musing_id": "m1"}, body={"text": longest})[0] == 200
    assert stored(tables, "m1")["text"] == longest


def test_edit_does_not_create_a_musing_that_is_not_there(tables):
    status, body, _ = api("PUT /musings/{musing_id}", path={"musing_id": "nope"}, body={"text": "Hello"})

    assert status == 404 and "nope" in body["error"]
    assert tables.Table("Musings").scan()["Items"] == []


# --- musings regenerate ---------------------------------------------------------------------------


def test_regenerate_writes_a_blank_musing_again_in_the_mood_it_has(tables):
    put_article(tables)
    put_musing(tables, "m1", "", mood="thoughtful")

    status, body, model = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "m1"})

    assert status == 200
    (changed,) = body["regenerated"]
    assert changed["text"] == "Paws up, a new one is out!" and changed["written_by"] == "model"
    assert changed["was_blank"] is True and changed["blank"] is False and changed["edited_at"]
    item = stored(tables, "m1")
    assert item["text"] == "Paws up, a new one is out!" and item["mood"] == "thoughtful"
    assert item["created_at"] == "2026-10-01T00:00:00+00:00"
    # The same prompt as at publish: the article's title, its topic's name, the musing's own mood.
    purpose, prompt, model_id = model.call_args.args
    assert (purpose, model_id) == ("musings", "a-model")
    assert '"Staking Yields This Week"' in prompt and '"Crypto"' in prompt
    assert musings._ARTICLE_MOOD_GUIDANCE_REVIEWED in prompt
    assert musings._ARTICLE_MOOD_GUIDANCE_COMPLIANT not in prompt


def test_regenerate_rewrites_a_musing_that_has_text_when_asked_by_id(tables):
    put_article(tables)
    put_musing(tables, "m1", "Something I do not like")

    status, body, model = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "m1"})

    assert status == 200 and body["regenerated"][0]["was_blank"] is False
    assert stored(tables, "m1")["text"] == "Paws up, a new one is out!"
    assert musings._ARTICLE_MOOD_GUIDANCE_COMPLIANT in model.call_args.args[1]  # it was "proud"


@pytest.mark.parametrize("reply", ["", "   \n", None, RuntimeError("bedrock is down")])
def test_regenerate_never_leaves_a_musing_blank(tables, reply):
    """The model answering with nothing is how the musing went blank in the first place."""
    put_article(tables)
    put_musing(tables, "m1", "")

    status, body, _ = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "m1"}, reply=reply)

    assert status == 200 and body["regenerated"][0]["written_by"] == "plain"
    text = stored(tables, "m1")["text"]
    assert text == body["regenerated"][0]["text"] and "Staking Yields This Week" in text


def test_regenerate_cuts_a_long_answer_to_a_musings_length(tables):
    put_article(tables)
    put_musing(tables, "m1", "")

    api("POST /musings/{musing_id}/regenerate", path={"musing_id": "m1"}, reply="word " * 200)

    assert 0 < len(stored(tables, "m1")["text"]) <= musings.MAX_MUSING_CHARS


def test_regenerate_is_only_for_musings_about_articles(tables):
    put_musing(tables, "loot-1", "", article_id=None, kind="loot", mood="excited")

    status, body, model = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "loot-1"})

    assert status == 409 and "musings edit" in body["error"]
    assert stored(tables, "loot-1")["text"] == ""
    model.assert_not_called()


@pytest.mark.parametrize("article_status", [None, "rejected", "pending_moderation"])
def test_regenerate_refuses_a_musing_whose_article_is_not_published(tables, article_status):
    if article_status:
        put_article(tables, status=article_status)
    put_musing(tables, "m1", "")

    status, body, model = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "m1"})

    assert status == 409 and "not published" in body["error"]
    assert stored(tables, "m1")["text"] == ""
    model.assert_not_called()


def test_regenerate_an_unknown_musing_is_a_404(tables):
    status, _, model = api("POST /musings/{musing_id}/regenerate", path={"musing_id": "nope"})

    assert status == 404
    model.assert_not_called()


# --- musings regenerate --article -----------------------------------------------------------------

BY_ARTICLE = "POST /articles/{article_id}/musings/regenerate"


def test_regenerate_by_article_writes_its_blank_musings_and_leaves_the_rest(tables):
    put_article(tables)
    put_article(tables, "a2", title="Another One")
    put_musing(tables, "m1", "", day=1)
    put_musing(tables, "m2", "Already fine", day=2)
    put_musing(tables, "m3", " ", day=3)
    put_musing(tables, "other", "", article_id="a2", day=4)

    status, body, model = api(BY_ARTICLE, path={"article_id": "a1"})

    assert status == 200
    assert sorted(m["musing_id"] for m in body["regenerated"]) == ["m1", "m3"]
    assert all(m["was_blank"] and m["written_by"] == "model" for m in body["regenerated"])
    assert model.call_count == 2
    assert stored(tables, "m1")["text"] == stored(tables, "m3")["text"] == "Paws up, a new one is out!"
    assert stored(tables, "m2")["text"] == "Already fine" and "edited_at" not in stored(tables, "m2")
    assert stored(tables, "other")["text"] == ""


def test_regenerate_by_article_with_nothing_blank_changes_nothing(tables):
    put_article(tables)
    put_musing(tables, "m1", "Already fine")

    status, body, model = api(BY_ARTICLE, path={"article_id": "a1"})

    assert status == 409 and "nothing to regenerate" in body["error"]
    assert stored(tables, "m1")["text"] == "Already fine"
    model.assert_not_called()


def test_regenerate_by_article_needs_the_article_to_exist_and_be_published(tables):
    assert api(BY_ARTICLE, path={"article_id": "nope"})[0] == 404

    put_article(tables, status="rejected")
    put_musing(tables, "m1", "")
    status, _, model = api(BY_ARTICLE, path={"article_id": "a1"})

    assert status == 409 and stored(tables, "m1")["text"] == ""
    model.assert_not_called()


# --- the routes are declared where API Gateway reads them ------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_musings_route_is_in_the_environments_terraform(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    for route in admin_api_handler._ROUTES:
        if "musings" in route:
            assert f'"{route}",' in admin_routes, route
