"""common/dynamo.set_topic_goal_rotation_state: one entry of a topic's goal rotation state."""

import boto3
import pytest
from moto import mock_aws

import common.dynamo as dynamo


@pytest.fixture
def topics_table(monkeypatch):
    monkeypatch.setenv("TOPICS_TABLE", "Topics")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    monkeypatch.setattr(dynamo, "_dynamodb_resource", None)
    with mock_aws():
        boto3.client("dynamodb", region_name="ap-southeast-2").create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        table = boto3.resource("dynamodb", region_name="ap-southeast-2").Table("Topics")
        table.put_item(Item={"topic_id": "t", "name": "T"})
        yield table


def _state(goal):
    return {"goal": goal, "used": [goal], "from": "2026-10-01T00:00:00Z", "to": "2026-10-02T00:00:00Z"}


def test_each_entry_is_set_and_the_others_left_alone(topics_table):
    dynamo.set_topic_goal_rotation_state("t", "north", _state("access"))  # makes the map
    dynamo.set_topic_goal_rotation_state("t", "*", _state("network"))
    dynamo.set_topic_goal_rotation_state("t", "north", _state("network"))

    item = topics_table.get_item(Key={"topic_id": "t"})["Item"]
    states = {"north": _state("network"), "*": _state("network")}
    assert item == {"topic_id": "t", "name": "T", "goal_rotation_state": states}


def test_a_state_that_is_not_a_map_is_replaced_and_said(topics_table, capsys):
    topics_table.update_item(
        Key={"topic_id": "t"},
        UpdateExpression="SET goal_rotation_state = :s",
        ExpressionAttributeValues={":s": "hand edited"},
    )

    dynamo.set_topic_goal_rotation_state("t", "north", _state("access"))

    item = topics_table.get_item(Key={"topic_id": "t"})["Item"]
    assert item["goal_rotation_state"] == {"north": _state("access")}
    assert "not a map; replaced it" in capsys.readouterr().out


def test_a_missing_topic_is_not_created(topics_table):
    with pytest.raises(topics_table.meta.client.exceptions.ConditionalCheckFailedException):
        dynamo.set_topic_goal_rotation_state("deleted", "north", _state("access"))

    assert "Item" not in topics_table.get_item(Key={"topic_id": "deleted"})
