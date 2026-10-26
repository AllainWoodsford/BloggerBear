"""The Admin API's prompts routes (admin_api_handler.py; docs/enhancements/model-ranch-prompt-assets-evals.md,
PR4): GET /prompts (each registered prompt's files under latest/ with version, release, sha256, hash
status and pause state), POST /prompts/pause and /prompts/resume (the `prompts` row's
paused_versions), GET /prompts/releases and PUT /prompts/config (the kill switch), against moto's S3
and DynamoDB. Each answers with the deployment's switch off, and with no bucket at all."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

import admin_api_handler
from common import dynamo, prompts, prompts_registry
from common.prompts import PromptSpec, sha256_text

REGION = "ap-southeast-2"
BUCKET = "acme-blog-dev-prompts"
HASHES = "PromptHashes"
CONFIG = "ModelConfig"
ROOT = Path(__file__).resolve().parents[2]

VANILLA = "Write about {{topic}}.\n{{locked:rules}}\n"
DRAFT = PromptSpec(
    prompt_id="daily_cycle/draft",
    vanilla=VANILLA,
    vanilla_version="v1",
    vanilla_sha256=sha256_text(VANILLA),
    required=frozenset({"topic"}),
    optional=frozenset(),
    locked=("rules",),
    max_chars=500,
)
TITLE_VANILLA = "Title for {{topic}}.\n"
TITLE = PromptSpec(
    prompt_id="daily_cycle/title",
    vanilla=TITLE_VANILLA,
    vanilla_version="v1",
    vanilla_sha256=sha256_text(TITLE_VANILLA),
    required=frozenset({"topic"}),
    optional=frozenset(),
    locked=(),
    max_chars=500,
)
CODE_ONLY_VANILLA = "Check {{draft}}.\n"
CODE_ONLY = PromptSpec(
    prompt_id="reviews/compliance",
    vanilla=CODE_ONLY_VANILLA,
    vanilla_version="v3",
    vanilla_sha256=sha256_text(CODE_ONLY_VANILLA),
    required=frozenset({"draft"}),
    optional=frozenset(),
    locked=(),
    max_chars=500,
    asset=False,
)
ASSET_V2 = "Asset two: {{topic}}.\n{{locked:rules}}\n"
ASSET_V3 = "Asset three: {{topic}}.\n{{locked:rules}}\n"


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "PROMPT_AS_ASSET_ENABLED": "true",
        "PROMPTS_BUCKET": BUCKET,
        "PROMPT_HASHES_TABLE": HASHES,
        "MODEL_CONFIG_TABLE": CONFIG,
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(prompts_registry, "all_specs", lambda: [DRAFT, TITLE, CODE_ONLY])
    dynamo._dynamodb_resource = None
    prompts._reset()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, key in {HASHES: "prompt_hash", CONFIG: "config_id"}.items():
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        yield {"s3": s3, "hashes": resource.Table(HASHES), "config": resource.Table(CONFIG)}
    prompts._reset()


def publish(aws, prompt_id, version, body, *, release="dev-20261026T0300-abc1234", status="live", **row):
    """What the publisher leaves for one file: the object under latest/ with its three tags, and
    its hash row (status None: no row)."""
    sha = sha256_text(body)
    tags = f"version={version}&release={release}&sha256={sha}"
    key = f"{prompt_id}/latest/{version}.txt"
    aws["s3"].put_object(Bucket=BUCKET, Key=key, Body=body.encode(), Tagging=tags)
    if status is not None:
        aws["hashes"].put_item(
            Item={"prompt_hash": sha, "prompt_id": prompt_id, "version": version, "status": status, **row}
        )
    return sha


def api(route, body=None, query=None):
    event = {"routeKey": route, "queryStringParameters": query}
    if body is not None:
        event["body"] = json.dumps(body)
    response = admin_api_handler.handler(event, None)
    return response["statusCode"], json.loads(response["body"])


def stored_row(aws):
    return aws["config"].get_item(Key={"config_id": "prompts"}).get("Item")


def by_id(answer):
    return {prompt["prompt_id"]: prompt for prompt in answer["prompts"]}


# --- GET /prompts ----------------------------------------------------------------------------------


def test_list_shows_each_file_with_its_version_release_sha_hash_status_and_pause(aws):
    sha2 = publish(aws, "daily_cycle/draft", "v2", ASSET_V2)
    retired = {"status": "retired", "retired_at": "2026-10-26T03:00:00+00:00"}
    sha3 = publish(aws, "daily_cycle/draft", "v3", ASSET_V3, **retired)
    aws["config"].put_item(Item={"config_id": "prompts", "paused_versions": ["daily_cycle/draft@v3"]})

    status, answer = api("GET /prompts")

    assert status == 200
    draft = by_id(answer)["daily_cycle/draft"]
    assert draft["asset"] is True and draft["vanilla_version"] == "vanilla:v1"
    assert draft["files"] == [
        {
            "key": "daily_cycle/draft/latest/v2.txt",
            "version": "v2",
            "release": "dev-20261026T0300-abc1234",
            "tagged_sha256": sha2,
            "sha256": sha2,
            "hash_status": "live",
            "retired_at": None,
            "paused": False,
        },
        {
            "key": "daily_cycle/draft/latest/v3.txt",
            "version": "v3",
            "release": "dev-20261026T0300-abc1234",
            "tagged_sha256": sha3,
            "sha256": sha3,
            "hash_status": "retired",
            "retired_at": "2026-10-26T03:00:00+00:00",
            "paused": True,
        },
    ]
    assert answer["serving"] is True and answer["deployment_switch"] == "on" and answer["bucket"] == BUCKET
    assert answer["effective"] == {"prompt_assets": "on", "paused_versions": ["daily_cycle/draft@v3"]}
    assert answer["settings"] == {"prompt_assets": None, "paused_versions": ["daily_cycle/draft@v3"]}


def test_the_sha_shown_is_the_files_own_and_a_file_with_no_hash_row_is_missing(aws):
    """The hash check uses the bytes' sha256, never the publisher's tag (common/prompts.py), so the
    listing shows the same: a tag that says otherwise is shown beside it, and the row looked up is
    the bytes'."""
    body = ASSET_V2
    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="daily_cycle/draft/latest/v2.txt",
        Body=body.encode(),
        Tagging=f"version=v2&release=r1&sha256={'0' * 64}",
    )
    aws["hashes"].put_item(Item={"prompt_hash": "0" * 64, "status": "live"})

    (found,) = by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]

    assert found["tagged_sha256"] == "0" * 64 and found["sha256"] == sha256_text(body)
    assert found["hash_status"] == "missing"


def test_a_code_only_prompt_lists_no_files_and_says_why(aws):
    publish(aws, "reviews/compliance", "v1", CODE_ONLY_VANILLA)  # even if something is there

    compliance = by_id(api("GET /prompts")[1])["reviews/compliance"]

    assert compliance["asset"] is False and compliance["files"] == []
    assert compliance["vanilla_version"] == "vanilla:v3" and "code-only" in compliance["note"]


def test_a_file_that_is_not_utf8_is_listed_with_what_was_read(aws):
    aws["s3"].put_object(
        Bucket=BUCKET,
        Key="daily_cycle/draft/latest/v2.txt",
        Body=b"\xff\xfe",
        Tagging="version=v2&release=r1",
    )

    (found,) = by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]

    assert found["version"] == "v2" and found["sha256"] is None and found["hash_status"] is None
    assert found["error"] == "its bytes are not UTF-8"


def test_a_file_whose_tags_cannot_be_read_says_so_and_still_shows_its_sha_and_hash_status(aws, capsys):
    sha = publish(aws, "daily_cycle/draft", "v2", ASSET_V2)

    with patch.object(prompts, "_tags", side_effect=RuntimeError("throttled")):
        (found,) = by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]

    assert found["error"] == "could not read its tags"
    assert found["version"] is None and found["release"] is None and found["tagged_sha256"] is None
    assert found["paused"] is False
    assert found["sha256"] == sha and found["hash_status"] == "live"
    assert "RuntimeError('throttled')" in capsys.readouterr().out


def test_a_file_whose_body_cannot_be_read_says_so_and_still_shows_its_tags(aws, capsys):
    publish(aws, "daily_cycle/draft", "v2", ASSET_V2)

    with patch.object(prompts._s3(), "get_object", side_effect=RuntimeError("throttled")):
        (found,) = by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]

    assert found["error"] == "could not read it"
    assert found["version"] == "v2" and found["sha256"] is None and found["hash_status"] is None
    assert "RuntimeError('throttled')" in capsys.readouterr().out


def test_a_file_whose_hash_row_cannot_be_read_says_so_and_shows_no_hash_status(aws, capsys):
    """An unreadable row is not shown as `missing`: that would say the publisher never approved it."""
    sha = publish(aws, "daily_cycle/draft", "v2", ASSET_V2)

    with patch.object(prompts, "_hash_row", side_effect=RuntimeError("throttled")):
        (found,) = by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]

    assert found["error"] == "could not read its hash row"
    assert found["version"] == "v2" and found["tagged_sha256"] == sha and found["sha256"] == sha
    assert found["hash_status"] is None and found["retired_at"] is None
    assert "RuntimeError('throttled')" in capsys.readouterr().out


def test_one_prompts_listing_failing_is_said_and_the_others_are_still_listed(aws, capsys):
    publish(aws, "daily_cycle/title", "v2", "Title two {{topic}}.\n")
    real = prompts._list_latest

    def flaky(prompt_id):
        if prompt_id == "daily_cycle/draft":
            raise RuntimeError("throttled")
        return real(prompt_id)

    with patch.object(prompts, "_list_latest", flaky):
        status, answer = api("GET /prompts")

    assert status == 200
    assert by_id(answer)["daily_cycle/draft"]["error"] == "could not list latest/"
    assert [f["version"] for f in by_id(answer)["daily_cycle/title"]["files"]] == ["v2"]
    assert "RuntimeError('throttled')" in capsys.readouterr().out


def test_list_reads_the_bucket_again_on_every_call_rather_than_from_the_lambdas_cache(aws):
    publish(aws, "daily_cycle/draft", "v2", ASSET_V2)
    assert len(by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]) == 1
    publish(aws, "daily_cycle/draft", "v3", ASSET_V3)
    assert len(by_id(api("GET /prompts")[1])["daily_cycle/draft"]["files"]) == 2


@pytest.mark.parametrize(
    ("env", "words"),
    [
        ({"PROMPT_AS_ASSET_ENABLED": "false"}, "PROMPT_AS_ASSET_ENABLED is off"),
        ({"PROMPTS_BUCKET": ""}, "No prompts bucket"),
        ({"PROMPT_HASHES_TABLE": ""}, "No prompt-hash table"),
    ],
)
def test_list_says_when_this_deployment_serves_no_asset_and_why(aws, monkeypatch, env, words):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    publish(aws, "daily_cycle/draft", "v2", ASSET_V2)

    status, answer = api("GET /prompts")

    assert status == 200 and answer["serving"] is False and words in answer["serving_note"]
    draft = by_id(answer)["daily_cycle/draft"]
    assert "error" not in draft
    # With a bucket the files are still listed, so what would be served can be checked first.
    assert len(draft["files"]) == (0 if env.get("PROMPTS_BUCKET") == "" else 1)


def test_list_with_no_bucket_reads_nothing_from_s3(aws, monkeypatch):
    monkeypatch.setenv("PROMPTS_BUCKET", "")
    with patch.object(prompts, "_s3") as s3:
        status, answer = api("GET /prompts")
    # The route catches a failing read per prompt, so a raising fake would pass unnoticed: the
    # fake records instead, and no call at all is what this pins.
    s3.assert_not_called()
    assert status == 200 and answer["bucket"] is None
    assert all(prompt["files"] == [] and "error" not in prompt for prompt in answer["prompts"])
    assert any(prompt["asset"] for prompt in answer["prompts"])  # one reached the bucket check


def test_list_says_when_the_kill_switch_is_off(aws):
    aws["config"].put_item(Item={"config_id": "prompts", "prompt_assets": "off"})
    _, answer = api("GET /prompts")
    assert answer["serving"] is False and "kill switch is off" in answer["serving_note"]


# --- POST /prompts/pause and /prompts/resume ---------------------------------------------------------


def test_pause_adds_the_version_and_the_lambda_then_serves_the_vanilla_with_paused(aws):
    publish(aws, "daily_cycle/draft", "v2", ASSET_V2)
    assert prompts.get_prompt("daily_cycle/draft", {"topic": "bears"}, {"rules": "R"}).version == "v2"

    status, answer = api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})

    assert status == 200 and answer["changed"] is True and answer["entry"] == "daily_cycle/draft@v2"
    assert answer["effective"]["paused_versions"] == ["daily_cycle/draft@v2"]
    assert stored_row(aws)["paused_versions"] == ["daily_cycle/draft@v2"]
    assert "300 seconds" in answer["note"]
    prompts._reset()  # past the container's cache
    served = prompts.get_prompt("daily_cycle/draft", {"topic": "bears"}, {"rules": "R"})
    assert (served.source, served.fallback_code) == ("vanilla", "paused")


def test_pausing_twice_changes_nothing_and_a_second_version_is_added_after_the_first(aws):
    api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    status, again = api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    assert status == 200 and again["changed"] is False
    api("POST /prompts/pause", {"prompt_id": "daily_cycle/title", "version": "v4"})
    assert stored_row(aws)["paused_versions"] == ["daily_cycle/draft@v2", "daily_cycle/title@v4"]


def test_resume_takes_the_version_out_and_leaves_the_others(aws):
    aws["config"].put_item(
        Item={"config_id": "prompts", "paused_versions": ["daily_cycle/draft@v2", "daily_cycle/title@v4"]}
    )

    status, answer = api("POST /prompts/resume", {"prompt_id": "daily_cycle/draft", "version": "v2"})

    assert status == 200 and answer["changed"] is True
    assert stored_row(aws)["paused_versions"] == ["daily_cycle/title@v4"]
    status, again = api("POST /prompts/resume", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    assert status == 200 and again["changed"] is False


def test_resume_takes_out_an_entry_for_a_prompt_the_registry_no_longer_has(aws):
    aws["config"].put_item(Item={"config_id": "prompts", "paused_versions": ["gear/theme@v2"]})
    status, answer = api("POST /prompts/resume", {"prompt_id": "gear/theme", "version": "v2"})
    assert status == 200 and answer["changed"] is True and stored_row(aws)["paused_versions"] == []


@pytest.mark.parametrize(
    "body",
    [
        {"prompt_id": "daily_cycle/draft"},
        {"version": "v2"},
        {"prompt_id": "daily_cycle/draft", "version": "2"},
        {"prompt_id": "daily_cycle/draft", "version": "v0"},
        {"prompt_id": "draft", "version": "v2"},
        {"prompt_id": "Daily_Cycle/draft", "version": "v2"},
        {"prompt_id": "daily_cycle/draft@v1", "version": "v2"},
        {"prompt_id": ["daily_cycle/draft"], "version": "v2"},
        {"prompt_id": "daily_cycle/draft", "version": 2},
        {"prompt_id": "daily_cycle/draft", "version": "v2\n"},
        {"prompt_id": "daily_cycle/draft\n", "version": "v2"},
    ],
)
@pytest.mark.parametrize("route", ["POST /prompts/pause", "POST /prompts/resume"])
def test_a_malformed_prompt_or_version_is_refused_and_nothing_is_written(aws, route, body):
    status, answer = api(route, body)
    assert status == 400 and "send prompt_id" in answer["error"]
    assert stored_row(aws) is None


def test_pause_refuses_a_prompt_the_registry_does_not_have_and_a_code_only_one(aws):
    status, answer = api("POST /prompts/pause", {"prompt_id": "daily_cycle/drafts", "version": "v2"})
    assert status == 400 and "not a registered prompt" in answer["error"]
    status, answer = api("POST /prompts/pause", {"prompt_id": "reviews/compliance", "version": "v2"})
    assert status == 400 and "code-only" in answer["error"]
    assert stored_row(aws) is None


def test_a_stored_list_that_is_not_valid_is_replaced_on_the_first_pause(aws):
    aws["config"].put_item(Item={"config_id": "prompts", "paused_versions": "daily_cycle/draft@v2"})
    status, answer = api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v3"})
    assert status == 200 and answer["changed"] is True
    assert stored_row(aws)["paused_versions"] == ["daily_cycle/draft@v3"]


@pytest.mark.parametrize(
    ("before", "meanwhile"),
    [
        (None, ["daily_cycle/title@v4"]),
        (["daily_cycle/title@v4"], ["daily_cycle/title@v4", "gear/theme@v1"]),
    ],
    ids=["no-list-yet", "a-list-already"],
)
def test_a_pause_never_drops_a_change_made_since_the_row_was_read(aws, before, meanwhile):
    """Two pauses at once: the second's write is conditional on the list it read (or on there
    being none), so it reads again and keeps the first's entry instead of writing over it."""
    if before is not None:
        aws["config"].put_item(Item={"config_id": "prompts", "paused_versions": before})
    real_read = admin_api_handler.get_config_row
    reads = []

    def racing_read(config_id):
        row = real_read(config_id)
        if not reads:  # the other writer gets in between this read and this write
            aws["config"].put_item(Item={"config_id": "prompts", "paused_versions": meanwhile})
        reads.append(config_id)
        return row

    with patch.object(admin_api_handler, "get_config_row", racing_read):
        status, answer = api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})

    assert status == 200 and answer["changed"] is True
    assert stored_row(aws)["paused_versions"] == [*meanwhile, "daily_cycle/draft@v2"]


def test_a_row_that_keeps_changing_is_answered_409_after_the_last_try(aws):
    with patch.object(admin_api_handler, "replace_config_field", return_value=False) as write:
        status, answer = api("POST /prompts/pause", {"prompt_id": "daily_cycle/draft", "version": "v2"})
    assert status == 409 and "send it again" in answer["error"]
    assert write.call_count == admin_api_handler._PAUSE_ATTEMPTS


def test_replace_config_field_writes_only_over_the_value_it_was_given(aws):
    assert dynamo.replace_config_field("prompts", "paused_versions", None, ["a/b@v1"]) is True
    assert dynamo.replace_config_field("prompts", "paused_versions", None, ["a/b@v2"]) is False
    assert dynamo.replace_config_field("prompts", "paused_versions", ["a/b@v9"], ["a/b@v2"]) is False
    assert dynamo.replace_config_field("prompts", "paused_versions", ["a/b@v1"], ["a/b@v2"]) is True
    assert stored_row(aws)["paused_versions"] == ["a/b@v2"]


# --- PUT /prompts/config ---------------------------------------------------------------------------


def test_the_kill_switch_turns_every_asset_off_and_null_turns_it_back_on(aws):
    publish(aws, "daily_cycle/draft", "v2", ASSET_V2)

    status, answer = api("PUT /prompts/config", {"prompt_assets": "off"})

    assert status == 200 and answer["effective"]["prompt_assets"] == "off" and answer["serving"] is False
    assert stored_row(aws)["prompt_assets"] == "off" and "300 seconds" in answer["note"]
    served = prompts.get_prompt("daily_cycle/draft", {"topic": "bears"}, {"rules": "R"})
    assert (served.source, served.fallback_code) == ("vanilla", "assets_off")

    status, answer = api("PUT /prompts/config", {"prompt_assets": None})
    assert status == 200 and answer["effective"]["prompt_assets"] == "on"
    assert "prompt_assets" not in stored_row(aws)


@pytest.mark.parametrize(
    ("body", "words"),
    [
        ({}, "send prompt_assets"),
        ({"prompt_assets": "maybe"}, "must be on or off"),
        ({"prompt_assets": True}, "must be on or off"),
        ({"paused_versions": []}, "one version at a time"),
        ({"prompt_assets": "on", "paused_versions": None}, "one version at a time"),
        ({"colour": "blue"}, "unknown setting"),
    ],
)
def test_config_refuses_anything_but_the_kill_switch_and_writes_nothing(aws, body, words):
    status, answer = api("PUT /prompts/config", body)
    assert status == 400 and words in answer["error"]
    assert stored_row(aws) is None


def test_config_refuses_a_body_that_is_not_json(aws):
    response = admin_api_handler.handler({"routeKey": "PUT /prompts/config", "body": "{not json"}, None)
    assert response["statusCode"] == 400


# --- GET /prompts/releases -------------------------------------------------------------------------


def test_releases_are_listed_newest_first_with_the_current_and_pinned_ones_marked(aws):
    for release_id in ("dev-20261020T0300-aaaaaaa", "dev-20261024T0300-bbbbbbb", "dev-20261026T0300-ccccccc"):
        aws["s3"].put_object(Bucket=BUCKET, Key=f"releases/{release_id}.json", Body=b"{}")
    aws["s3"].put_object(Bucket=BUCKET, Key="releases/archive/old.json", Body=b"{}")  # not a release
    current = {"release_id": "dev-20261026T0300-ccccccc", "pinned_release": "dev-20261020T0300-aaaaaaa"}
    aws["s3"].put_object(Bucket=BUCKET, Key="releases/current.json", Body=json.dumps(current).encode())

    status, answer = api("GET /prompts/releases")

    assert status == 200 and answer["total"] == 3 and answer["current"] == current
    # Written within the same second here, so the id (which starts with the time) breaks the tie.
    assert [(r["release_id"], r["current"], r["pinned"]) for r in answer["releases"]] == [
        ("dev-20261026T0300-ccccccc", True, False),
        ("dev-20261024T0300-bbbbbbb", False, False),
        ("dev-20261020T0300-aaaaaaa", False, True),
    ]
    assert all(datetime.fromisoformat(r["written_at"]).tzinfo == UTC for r in answer["releases"])


def test_current_json_is_read_by_the_field_names_the_publisher_writes():
    """The fixture above writes the literal names, so renaming a constant fails it as well as this."""
    assert (prompts.CURRENT_RELEASE_FIELD, prompts.PINNED_RELEASE_FIELD) == ("release_id", "pinned_release")


def test_releases_are_sorted_by_when_they_were_written(aws):
    listed = [
        {"Key": "releases/b.json", "LastModified": datetime(2026, 10, 20, tzinfo=UTC)},
        {"Key": "releases/a.json", "LastModified": datetime(2026, 10, 26, tzinfo=UTC)},
    ]

    class Pages:
        def paginate(self, **kwargs):
            return [{"Contents": listed}]

    class Client:
        exceptions = boto3.client("s3", region_name=REGION).exceptions

        def get_paginator(self, name):
            return Pages()

    with patch.object(prompts, "_s3", return_value=Client()):
        assert prompts.releases(10) == (
            [
                {"release_id": "a", "written_at": "2026-10-26T00:00:00+00:00"},
                {"release_id": "b", "written_at": "2026-10-20T00:00:00+00:00"},
            ],
            2,
        )


def test_the_limit_keeps_the_newest_and_the_total_says_how_many_there_are(aws):
    for day in range(10, 15):
        aws["s3"].put_object(Bucket=BUCKET, Key=f"releases/dev-202610{day}T0300-0000000.json", Body=b"{}")

    status, answer = api("GET /prompts/releases", query={"limit": "2"})

    assert status == 200 and answer["total"] == 5
    newest = [release["release_id"] for release in answer["releases"]]
    assert newest == ["dev-20261014T0300-0000000", "dev-20261013T0300-0000000"]


@pytest.mark.parametrize("limit", ["0", "201", "ten", "-1", "1.5"])
def test_a_limit_out_of_range_is_refused(aws, limit):
    status, answer = api("GET /prompts/releases", query={"limit": limit})
    assert status == 400 and "from 1 to 200" in answer["error"]


def test_before_the_first_publish_there_is_nothing_and_it_says_so(aws):
    status, answer = api("GET /prompts/releases")
    assert status == 200 and answer["releases"] == [] and answer["total"] == 0 and answer["current"] is None
    assert answer["note"] == "Nothing has been published to this bucket yet."
    assert "error" not in answer  # no current.json yet is what a fresh bucket looks like
    assert prompts.current_release() is None


def test_a_current_json_that_cannot_be_read_is_said_and_the_releases_are_still_listed(aws, capsys):
    aws["s3"].put_object(Bucket=BUCKET, Key="releases/dev-20261026T0300-ccccccc.json", Body=b"{}")
    aws["s3"].put_object(Bucket=BUCKET, Key="releases/current.json", Body=b"[1, 2]")

    status, answer = api("GET /prompts/releases")

    assert status == 200 and answer["current"] is None and "releases/current.json" in answer["error"]
    assert [r["release_id"] for r in answer["releases"]] == ["dev-20261026T0300-ccccccc"]
    assert answer["releases"][0]["current"] is False


def test_a_bucket_that_cannot_be_listed_is_a_plain_502(aws, monkeypatch, capsys):
    monkeypatch.setenv("PROMPTS_BUCKET", "no-such-bucket")
    status, answer = api("GET /prompts/releases")
    assert status == 502 and answer["error"] == "could not list releases/ in the prompts bucket"
    assert "NoSuchBucket" in capsys.readouterr().out


def test_with_no_bucket_the_releases_answer_says_nothing_is_published(aws, monkeypatch):
    monkeypatch.setenv("PROMPTS_BUCKET", "")
    with patch.object(prompts, "_s3", side_effect=AssertionError("no S3 call")):
        status, answer = api("GET /prompts/releases")
    assert status == 200 and answer["bucket"] is None and answer["releases"] == []
    assert "No prompts bucket" in answer["note"]


# --- every route with the switch off ---------------------------------------------------------------

PROMPT_ROUTES = {
    "GET /prompts": None,
    "POST /prompts/pause": {"prompt_id": "daily_cycle/draft", "version": "v2"},
    "POST /prompts/resume": {"prompt_id": "daily_cycle/draft", "version": "v2"},
    "GET /prompts/releases": None,
    "PUT /prompts/config": {"prompt_assets": "off"},
}


@pytest.mark.parametrize("route", sorted(PROMPT_ROUTES))
def test_every_prompts_route_answers_with_the_switch_off_and_no_bucket(aws, monkeypatch, route):
    """What a deployment before PR4's apply (or with the module's outputs empty) looks like: the
    switch off and no bucket or table named. Every route still answers, none with a 500."""
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "false")
    monkeypatch.setenv("PROMPTS_BUCKET", "")
    monkeypatch.setenv("PROMPT_HASHES_TABLE", "")
    status, answer = api(route, PROMPT_ROUTES[route])
    assert status == 200, answer
    assert answer.get("serving", False) is False


def test_the_prompts_routes_are_exactly_these_five():
    routes = {route for route in admin_api_handler._ROUTES if re.search(r" /prompts(/|$)", route)}
    assert routes == set(PROMPT_ROUTES)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_all_five_prompts_routes_are_in_the_environments_terraform(env):
    main = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    for route in PROMPT_ROUTES:
        assert f'"{route}",' in admin_routes, route
