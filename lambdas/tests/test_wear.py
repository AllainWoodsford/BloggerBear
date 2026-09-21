"""Tests for common/wear.py: what feedback does to the gear an article was written with."""

from __future__ import annotations

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from common import wear

REGION = "ap-southeast-2"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("PROMPT_REFINEMENTS_TABLE", "PromptRefinements")
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    with mock_aws():
        boto3.client("dynamodb", region_name=REGION).create_table(
            TableName="PromptRefinements",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements")


def put(table, version, *, topic="t1", slot="ring", scope="topic", durability=5, top=10, **fields):
    """A piece of gear that is being worn (unless `fields` say otherwise)."""
    item = {
        "topic_id": topic,
        "version": version,
        "prompt_changes": f"Guidance {version}.",
        "status": "approved",
        "equipped": True,
        "slot": slot,
        "scope": scope,
        "rarity": "common",
        "durability": durability,
        "max_durability": top,
        **fields,
    }
    table.put_item(Item=item)
    return item


def spare(table, version, *, topic="t1", reason="parked", scope="topic", durability=8, top=10):
    """A piece in the backpack."""
    return put(
        table,
        version,
        topic=topic,
        slot=None,
        scope=scope,
        durability=durability,
        top=top,
        equipped=False,
        unequipped_reason=reason,
    )


def get(table, version, topic="t1"):
    return table.get_item(Key={"topic_id": topic, "version": version})["Item"]


def article(*pieces):
    return {"equipment_used": [{"topic_id": t, "version": v, "slot": s} for t, v, s in pieces]}


# --- damage and repair -------------------------------------------------------------------


def test_a_downvote_costs_each_piece_used_one_point(table):
    put(table, "a", slot="helmet", scope="global", topic="t9")
    put(table, "b")

    changes = wear.apply_feedback(article(("t9", "a", "helmet"), ("t1", "b", "ring")), "down")

    assert [c["durability"] for c in changes] == [4, 4]
    assert get(table, "a", "t9")["durability"] == 4 and get(table, "b")["durability"] == 4


def test_an_upvote_gives_one_point_back(table):
    put(table, "a", durability=5)

    wear.apply_feedback(article(("t1", "a", "ring")), "up")

    assert get(table, "a")["durability"] == 6


def test_repair_never_goes_over_the_maximum(table):
    put(table, "a", durability=10, top=10)

    changes = wear.apply_feedback(article(("t1", "a", "ring")), "up")

    assert changes == []  # already full: nothing to do
    assert get(table, "a")["durability"] == 10


def test_a_piece_the_article_did_not_use_is_untouched(table):
    put(table, "used")
    put(table, "left-out", slot="helmet", scope="global")

    wear.apply_feedback(article(("t1", "used", "ring")), "down")

    assert get(table, "left-out")["durability"] == 5


def test_an_article_written_with_no_gear_wears_nothing(table):
    put(table, "a")

    assert wear.apply_feedback({"equipment_used": []}, "down") == []
    assert wear.apply_feedback({}, "down") == []  # written before gear existed
    assert get(table, "a")["durability"] == 5


def test_a_piece_listed_twice_is_worn_once(table):
    put(table, "a")

    wear.apply_feedback(article(("t1", "a", "ring"), ("t1", "a", "ring")), "down")

    assert get(table, "a")["durability"] == 4


def test_legacy_pieces_and_unknown_pieces_are_skipped(table):
    put(table, "real")

    changes = wear.apply_feedback(
        article(("t1", "old", "legacy"), ("t1", "missing", "ring"), ("t1", "real", "ring")), "down"
    )

    assert [c["version"] for c in changes] == ["real"]


def test_a_piece_with_no_durability_is_left_alone(table):
    table.put_item(
        Item={"topic_id": "t1", "version": "old", "status": "approved", "equipped": True, "slot": "ring"}
    )

    assert wear.apply_feedback(article(("t1", "old", "ring")), "down") == []
    assert "durability" not in get(table, "old")


# --- only worn gear ---------------------------------------------------------------------


def test_gear_that_is_not_being_worn_is_not_worn_down(table):
    spare(table, "benched", reason="benched", durability=5)

    assert wear.apply_feedback(article(("t1", "benched", "ring")), "down") == []
    assert get(table, "benched")["durability"] == 5


def test_an_upvote_does_not_revive_gear_that_wore_out(table):
    spare(table, "dead", reason="worn_out", durability=0)

    assert wear.apply_feedback(article(("t1", "dead", "ring")), "up") == []
    assert get(table, "dead")["durability"] == 0  # only an admin repairs it


def test_gear_that_was_rejected_or_pending_is_ignored(table):
    put(table, "rejected", status="rejected")
    put(table, "pending", status="pending")

    assert wear.apply_feedback(article(("t1", "rejected", "ring"), ("t1", "pending", "ring")), "down") == []


# --- wearing out -------------------------------------------------------------------------


def test_a_piece_at_zero_is_taken_off_and_says_why(table):
    put(table, "a", slot="helmet", scope="global", durability=1)

    (change,) = wear.apply_feedback(article(("t1", "a", "helmet")), "down")

    assert change["worn_out"] is True and change["durability"] == 0
    stored = get(table, "a")
    assert stored["equipped"] is False and "slot" not in stored
    assert stored["unequipped_reason"] == "worn_out" and stored["status"] == "approved"


def test_durability_never_goes_below_zero(table):
    put(table, "a", durability=1)

    wear.apply_feedback(article(("t1", "a", "ring")), "down")
    again = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert again == []  # already off, nothing left to lose
    assert get(table, "a")["durability"] == 0


def test_a_piece_is_retired_and_replaced_only_once_however_many_downvotes_follow(table):
    put(table, "a", durability=1)
    spare(table, "s", durability=9)

    first = wear.apply_feedback(article(("t1", "a", "ring")), "down")
    second = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert first[0]["worn_out"] and second == []
    assert get(table, "s")["equipped"] is True  # replaced exactly once


# --- replacement -------------------------------------------------------------------------


def test_a_parked_ring_for_the_same_topic_takes_the_place_of_a_worn_out_one(table):
    put(table, "a", durability=1)
    spare(table, "s")

    (change,) = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert change["replaced_by"] == {"topic_id": "t1", "version": "s"}
    taken = get(table, "s")
    assert taken["equipped"] is True and taken["slot"] == "ring" and taken["scope"] == "topic"


def test_the_spare_with_the_most_durability_is_chosen(table):
    put(table, "a", durability=1)
    spare(table, "low", durability=3)
    spare(table, "high", durability=9)
    spare(table, "mid", durability=6)

    wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert get(table, "high")["equipped"] is True
    assert get(table, "low")["equipped"] is False and get(table, "mid")["equipped"] is False


@pytest.mark.parametrize("reason", ["benched", "shelved", "displaced", "worn_out"])
def test_anything_an_admin_put_away_is_never_put_back_on_automatically(table, reason):
    put(table, "a", durability=1)
    spare(table, "s", reason=reason)

    (change,) = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert change["replaced_by"] is None
    assert get(table, "s")["equipped"] is False


def test_a_spare_for_another_topic_does_not_replace_a_ring(table):
    put(table, "a", topic="t1", durability=1)
    spare(table, "elsewhere", topic="t2")

    (change,) = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert change["replaced_by"] is None
    assert get(table, "elsewhere", "t2")["equipped"] is False


def test_a_worn_out_ring_is_not_replaced_by_a_global_spare(table):
    put(table, "ring", durability=1)
    spare(table, "armor-spare", scope="global")

    (change,) = wear.apply_feedback(article(("t1", "ring", "ring")), "down")

    assert change["replaced_by"] is None
    assert get(table, "armor-spare")["equipped"] is False


def test_worn_out_armor_is_not_replaced_by_a_ring_spare(table):
    put(table, "helm", slot="helmet", scope="global", durability=1)
    spare(table, "ring-spare", topic="t1", scope="topic")

    (change,) = wear.apply_feedback(article(("t1", "helm", "helmet")), "down")

    assert change["replaced_by"] is None
    assert get(table, "ring-spare")["equipped"] is False


def test_worn_out_armor_is_replaced_by_a_parked_global_spare_in_the_same_slot(table):
    put(table, "helm", slot="helmet", scope="global", durability=1)
    spare(table, "s", scope="global")

    (change,) = wear.apply_feedback(article(("t1", "helm", "helmet")), "down")

    assert change["replaced_by"] == {"topic_id": "t1", "version": "s"}
    taken = get(table, "s")
    assert taken["slot"] == "helmet" and taken["scope"] == "global" and taken["equipped"] is True


def test_a_worn_out_spare_is_not_a_candidate(table):
    put(table, "a", durability=1)
    spare(table, "empty", durability=0)

    (change,) = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert change["replaced_by"] is None


def test_a_spare_never_pushes_something_else_out(table):
    put(table, "a", durability=1)
    spare(table, "s")
    for n in range(4):  # four other rings fill the set once "a" is off
        put(table, f"other{n}", durability=5)
    put(table, "extra", durability=5)  # a sixth: rings are already over the cap of five

    (change,) = wear.apply_feedback(article(("t1", "a", "ring")), "down")

    assert change["replaced_by"] is None
    assert get(table, "s")["equipped"] is False


# --- it never raises into the feedback path --------------------------------------------------


def test_a_failure_is_swallowed_and_the_other_pieces_still_wear(table):
    put(table, "good")
    real = wear.apply_prompt_refinement_wear

    def flaky(topic_id, version, delta):
        if version == "bad":
            raise RuntimeError("throttled")
        return real(topic_id, version, delta)

    with patch("common.wear.apply_prompt_refinement_wear", side_effect=flaky):
        changes = wear.apply_feedback(article(("t1", "bad", "ring"), ("t1", "good", "ring")), "down")

    assert [c["version"] for c in changes] == ["good"]
    assert get(table, "good")["durability"] == 4


def test_even_a_malformed_article_does_not_raise():
    assert wear.apply_feedback({"equipment_used": "nonsense"}, "down") == []
    assert wear.apply_feedback({"equipment_used": [None, {}, {"topic_id": "t"}]}, "down") == []
