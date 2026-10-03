"""The DynamoDB global secondary indexes Terraform gives the app tables, for moto fixtures.

common/dynamo.py Queries the Articles and ModerationQueue tables' indexes, and moto refuses a
Query on an index the table was not created with, so every fixture that creates one of those
tables goes through `create_table` here instead of calling the client directly. The definitions
mirror infra/modules/app-data/main.tf; test_terraform_wiring.py fails if the two drift apart.
"""
from __future__ import annotations

# Table name (as the fixtures call it) -> [(index name, hash key, range key, projection type)].
INDEXES: dict[str, list[tuple[str, str, str, str]]] = {
    "Articles": [
        ("by_status_created_at", "status", "created_at", "ALL"),
        ("by_topic_created_at", "topic_id", "created_at", "ALL"),
    ],
    "ModerationQueue": [
        ("by_status_created_at", "status", "created_at", "ALL"),
        ("by_article_created_at", "article_id", "created_at", "KEYS_ONLY"),
    ],
    "SecurityEvents": [
        ("by_status_last_seen", "status", "last_seen", "ALL"),
    ],
}


def create_table(dynamodb, **kwargs):
    """`dynamodb.create_table(**kwargs)`, adding the table's indexes (and their key attributes)
    when it is one that has any. Works with a boto3 client or resource alike."""
    indexes = INDEXES.get(kwargs.get("TableName"))
    if indexes:
        definitions = list(kwargs.get("AttributeDefinitions", []))
        defined = {d["AttributeName"] for d in definitions}
        for _, hash_key, range_key, _ in indexes:
            for name in (hash_key, range_key):
                if name not in defined:
                    definitions.append({"AttributeName": name, "AttributeType": "S"})
                    defined.add(name)
        kwargs["AttributeDefinitions"] = definitions
        kwargs["GlobalSecondaryIndexes"] = [
            {
                "IndexName": name,
                "KeySchema": [
                    {"AttributeName": hash_key, "KeyType": "HASH"},
                    {"AttributeName": range_key, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": projection},
            }
            for name, hash_key, range_key, projection in indexes
        ]
    return dynamodb.create_table(**kwargs)
