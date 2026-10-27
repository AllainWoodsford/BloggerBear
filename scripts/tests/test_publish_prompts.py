"""scripts/publish_prompts.py against moto's S3 and DynamoDB (the hash table as test_prompts.py
makes it): the publish order of section 4.2 of docs/enhancements/model-ranch-prompt-assets-evals.md
(and that a run stopped part-way leaves the previous release serving, read through
common/prompts.py as a Lambda reads it), the write-once guard on versions/, the hash table as the
record of every version, the retire grace, what the sync may delete, the skip rule (faked, and once
through the lint's real second pass), the pin and its clearing, current.json, the dry run, and that
nothing printed holds a prompt's text. Then the workflow that runs it, .github/workflows/prompts.yml."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
import yaml
from moto import mock_aws

# common.* (scripts/pyproject.toml puts only scripts/ on the path when a scripts test runs on its own).
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas"))

from common import dynamo, naming, prompts, prompts_registry
from common.prompts import PromptSpec, sha256_text

import prompt_lint
import publish_prompts as pp

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
REGION = "ap-southeast-2"
BUCKET = "publisher-test-prompts"
HASHES = "PublisherTestHashes"
CONFIG = "PublisherTestConfig"
NOW = datetime(2026, 10, 25, 3, 0, tzinfo=UTC)
LATER, LATER2, LATER3, LATER4 = (NOW + timedelta(hours=n) for n in (1, 2, 3, 4))
COMMIT = "abcdef0123456789abcdef0123456789abcdef01"


def spec(prompt_id: str, vanilla: str, required: tuple[str, ...], locked: tuple[str, ...] = ()) -> PromptSpec:
    return PromptSpec(
        prompt_id=prompt_id,
        vanilla=vanilla,
        vanilla_version="v1",
        vanilla_sha256=sha256_text(vanilla),
        required=frozenset(required),
        optional=frozenset(),
        locked=locked,
        max_chars=2000,
    )


DRAFT = spec("daily_cycle/draft", "Write about {{topic}}.\n{{locked:rules}}\n", ("topic",), ("rules",))
SUMMARY = spec("research_tick/summary", "Summarise {{findings}}.\n", ("findings",))
# Distinctive words in every asset, so a test can tell whether any of its text was printed.
MARKER = "QUOKKA"
DRAFT_V1 = f"{MARKER} one: write about {{{{topic}}}}.\n{{{{locked:rules}}}}\n"
DRAFT_V2 = f"{MARKER} two: write about {{{{topic}}}}, briefly.\n{{{{locked:rules}}}}\n"
DRAFT_V3 = f"{MARKER} three: {{{{topic}}}}.\n{{{{locked:rules}}}}\n"
SUMMARY_V1 = f"{MARKER} summary of {{{{findings}}}}.\n"
SUMMARY_V2 = f"{MARKER} summary two of {{{{findings}}}}.\n"
# The tree's keys, which are also the bucket's latest/ keys.
D1, D2, D3, D4 = (f"daily_cycle/draft/latest/v{n}.txt" for n in (1, 2, 3, 4))
S1, S2 = "research_tick/summary/latest/v1.txt", "research_tick/summary/latest/v2.txt"
# The lint as it is, before any test stands a fake in for it.
REAL_LINT = prompt_lint.lint


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
    monkeypatch.setattr(prompts_registry, "all_specs", lambda: [DRAFT, SUMMARY])
    dynamo._dynamodb_resource = None
    prompts._reset()
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        s3.put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
        ddb = boto3.resource("dynamodb", region_name=REGION)
        for name, key in {HASHES: "prompt_hash", CONFIG: "config_id"}.items():
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        yield SimpleNamespace(s3=s3, ddb=ddb, table=ddb.Table(HASHES))
    dynamo._dynamodb_resource = None
    prompts._reset()


def tree(root: Path, files: dict[str, str]) -> Path:
    """A prompts tree under `root`, replacing any: {"daily_cycle/draft/latest/v1.txt": text}."""
    base = root / "prompts"
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    (base / "README.md").write_text("the rules\n", encoding="utf-8")
    for relative, text in files.items():
        path = base / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    return root


def argv(root: Path, *args: str, environment: str = "dev", commit: str = COMMIT) -> list[str]:
    return ["--environment", environment, "--root", str(root), "--commit", commit,
            "--bucket", BUCKET, "--table", HASHES, *args]  # fmt: skip


def run(aws, root: Path, *args: str, now=NOW, environment="dev", s3=None, table=None) -> int:
    """One publisher run, with `s3` or `table` standing in for the real clients when given."""
    dynamodb = SimpleNamespace(Table=lambda _name: table) if table is not None else aws.ddb
    return pp.main(argv(root, *args, environment=environment), s3=s3 or aws.s3, dynamodb=dynamodb, now=now)


def release_id(now: datetime = NOW, environment: str = "dev") -> str:
    return pp.make_release_id(environment, COMMIT, now)


def keys(aws, prefix: str = "") -> set[str]:
    return {item["Key"] for item in aws.s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", [])}


def latest_keys(aws) -> list[str]:
    return sorted(key for key in keys(aws) if "/latest/" in key)


def body(aws, key: str) -> str:
    return aws.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read().decode("utf-8")


def tags(aws, key: str) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in aws.s3.get_object_tagging(Bucket=BUCKET, Key=key)["TagSet"]}


def row(aws, text: str) -> dict | None:
    return aws.table.get_item(Key={"prompt_hash": sha256_text(text)}).get("Item")


def versions_key(prompt_id: str, text: str) -> str:
    return f"{prompt_id}/versions/{sha256_text(text)}.txt"


def object_versions(aws, prefix: str) -> list[dict]:
    return aws.s3.list_object_versions(Bucket=BUCKET, Prefix=prefix).get("Versions", [])


def snapshot(aws) -> tuple:
    """Every object version and delete marker, and every hash row: what a write would change."""
    listed = aws.s3.list_object_versions(Bucket=BUCKET)
    versions = sorted((v["Key"], v["VersionId"]) for v in listed.get("Versions", []))
    markers = sorted((m["Key"], m["VersionId"]) for m in listed.get("DeleteMarkers", []))
    rows = sorted(json.dumps(item, sort_keys=True, default=str) for item in aws.table.scan()["Items"])
    return versions, markers, rows


def current(aws) -> dict | None:
    prompts._s3_client = aws.s3
    return prompts.current_release()


def served(aws, prompt_id: str = "daily_cycle/draft"):
    """What a Lambda would be given now (a cold container: nothing cached)."""
    prompts._reset()
    prompts._s3_client = aws.s3
    return prompts.get_prompt(prompt_id, {"topic": "bears", "findings": "F"}, {"rules": "RULES"})


def boom(*args, **kwargs):
    raise RuntimeError("boom")


class Failing:
    """An S3 client whose `method` raises from its `after`-th call on (0: the first)."""

    def __init__(self, inner, method: str, after: int = 0):
        self.inner, self.method, self.after, self.calls = inner, method, after, 0

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name != self.method:
            return attribute

        def failing(*args, **kwargs):
            self.calls += 1
            if self.calls > self.after:
                raise RuntimeError(f"{name} failed")
            return attribute(*args, **kwargs)

        return failing


class Recording:
    """A client (S3's, or the hash table) that records each call to the named methods, in order,
    as (method, what it names) in `calls`, which two of them can share."""

    def __init__(self, inner, *methods: str, calls: list | None = None):
        self.inner, self.methods = inner, methods
        self.calls = [] if calls is None else calls

    def __getattr__(self, name):
        attribute = getattr(self.inner, name)
        if name not in self.methods:
            return attribute

        def recorded(**kwargs):
            self.calls.append((name, self.named(kwargs)))
            return attribute(**kwargs)

        return recorded

    @staticmethod
    def named(kwargs: dict) -> str:
        if "Item" in kwargs:  # put_item: the row's status and version
            return f"{kwargs['Item']['status']} {kwargs['Item']['version']}"
        if "Key" in kwargs and isinstance(kwargs["Key"], dict):  # update_item: what it sets, and whose
            values = kwargs["ExpressionAttributeValues"]
            status = "retired" if ":retired" in values else values[":live"]
            return f"{status} {kwargs['Key']['prompt_hash'][:6]}"
        return kwargs["Key"]


# --- a publish ---------------------------------------------------------------------------------------


def test_a_first_publish_puts_each_file_live_in_latest_tagged_and_records_the_release(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1, S1: SUMMARY_V1})) == 0
    rid = release_id()
    assert rid == "dev-20261025T0300-abcdef0"
    assert latest_keys(aws) == [D1, S1]
    for key, text in ((D1, DRAFT_V1), (S1, SUMMARY_V1)):
        prompt_id = key.split("/latest/")[0]
        assert body(aws, key) == text
        assert tags(aws, key) == {"version": "v1", "release": rid, "sha256": sha256_text(text)}
        assert body(aws, versions_key(prompt_id, text)) == text  # the write-once copy
        assert row(aws, text) == {
            "prompt_hash": sha256_text(text),
            "prompt_id": prompt_id,
            "version": "v1",
            "release": rid,
            "approved_at": "2026-10-25T03:00:00Z",
            "status": "live",
        }
    assert aws.s3.head_object(Bucket=BUCKET, Key=D1)["ContentType"] == "text/plain; charset=utf-8"
    manifest = json.loads(body(aws, f"releases/{rid}.json"))
    assert manifest["release_id"] == rid and manifest["commit"] == COMMIT
    assert manifest["environment"] == "dev" and manifest["previous_release"] is None
    assert manifest["skipped"] == []
    assert [(f["prompt_id"], f["version"], f["carried_forward"]) for f in manifest["files"]] == [
        ("daily_cycle/draft", "v1", False),
        ("research_tick/summary", "v1", False),
    ]


def test_current_json_reads_back_through_prompts_current_release(aws, tmp_path):
    assert current(aws) is None  # before the first publish
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    found = current(aws)
    assert found[prompts.CURRENT_RELEASE_FIELD] == release_id()
    assert prompts.PINNED_RELEASE_FIELD in found and found[prompts.PINNED_RELEASE_FIELD] is None
    # And GET /prompts/releases lists the release by the record it wrote.
    listed, total = prompts.releases(10)
    assert total == 1 and listed[0]["release_id"] == release_id()


def test_the_lambdas_serve_what_was_published(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    prompt = served(aws)
    assert (prompt.source, prompt.version, prompt.fallback_code) == ("asset", "v1", None)
    assert prompt.text == f"{MARKER} one: write about bears.\nRULES\n"
    # GET /prompts sees the file, its tags and its live hash row.
    (described,) = prompts.published_files("daily_cycle/draft", [])
    assert described["sha256"] == described["tagged_sha256"] == sha256_text(DRAFT_V1)
    assert [described[name] for name in ("version", "release", "hash_status")] == ["v1", release_id(), "live"]


def test_two_files_in_latest_are_published_as_an_ab_split(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1, D2: DRAFT_V2})) == 0
    assert latest_keys(aws) == [D1, D2]
    assert row(aws, DRAFT_V1)["status"] == row(aws, DRAFT_V2)["status"] == "live"
    versions = set()
    for run_key in "abcdefgh":
        with prompts.bind_run(run_key):
            versions.add(served(aws).version)
    assert versions == {"v1", "v2"}


def test_archived_files_are_never_published(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2, "daily_cycle/draft/archive/v1.txt": DRAFT_V1})) == 0
    assert latest_keys(aws) == [D2]
    assert row(aws, DRAFT_V1) is None and versions_key("daily_cycle/draft", DRAFT_V1) not in keys(aws)


def test_an_empty_tree_publishes_an_empty_release_and_retires_what_was_live(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {}), now=LATER) == 0
    assert latest_keys(aws) == []
    assert row(aws, DRAFT_V1)["status"] == "retired"
    manifest = json.loads(body(aws, f"releases/{release_id(LATER)}.json"))
    assert manifest["files"] == [] and manifest["previous_release"] == release_id()
    assert served(aws).fallback_code == "no_files"  # the Lambdas serve their vanilla


def test_a_first_publish_of_an_empty_tree_writes_only_the_release_records(aws, tmp_path):
    assert run(aws, tree(tmp_path, {})) == 0
    assert keys(aws) == {f"releases/{release_id()}.json", "releases/current.json"}
    assert aws.table.scan()["Items"] == []


# --- the retire grace ----------------------------------------------------------------------------------


def test_a_replaced_version_is_retired_and_still_served_for_the_grace_only(aws, tmp_path, monkeypatch):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    assert latest_keys(aws) == [D2]
    old = row(aws, DRAFT_V1)
    assert old["status"] == "retired" and old["retired_at"] == "2026-10-25T04:00:00Z"
    assert row(aws, DRAFT_V2)["status"] == "live" and row(aws, DRAFT_V2)["release"] == release_id(LATER)
    # A container that listed latest/ before the release still holds v1's key: the Lambda accepts
    # the retired row for RETIRE_GRACE_S after retired_at, and not a second more.
    retired = LATER.timestamp()
    monkeypatch.setattr(prompts, "_now", lambda: retired + prompts.RETIRE_GRACE_S)
    assert prompts._approved(old) is True
    monkeypatch.setattr(prompts, "_now", lambda: retired + prompts.RETIRE_GRACE_S + 1)
    assert prompts._approved(old) is False


def test_only_live_rows_the_release_does_not_hold_are_retired(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    # A pending row (a run that stopped after step 1) is not live: it is left as it is.
    stopped = {"prompt_hash": "f" * 64, "prompt_id": "daily_cycle/draft", "version": "v9"}
    stopped["status"] = "pending"
    aws.table.put_item(Item=stopped)
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1, S1: SUMMARY_V1}), now=LATER) == 0
    assert row(aws, DRAFT_V1)["status"] == "live" and row(aws, DRAFT_V1)["release"] == release_id(LATER)
    assert aws.table.get_item(Key={"prompt_hash": "f" * 64})["Item"] == stopped


def test_a_pending_row_is_not_in_the_plan_to_retire(aws, tmp_path, capsys):
    """The plan's own rule, whatever step 4's condition would do with the row."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    aws.table.put_item(
        Item={"prompt_hash": "f" * 64, "prompt_id": "daily_cycle/draft", "version": "v9", "status": "pending"}
    )
    capsys.readouterr()
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1, S1: SUMMARY_V1}), "--dry-run", now=LATER) == 0
    assert "step 4: 0 row(s) to retire" in capsys.readouterr().out


def test_a_row_retired_since_the_plan_keeps_its_retired_at(aws, tmp_path):
    """Step 4 retires a row only while it is still live: one an overlapping run (an operator's own,
    beside the workflow's) retired after this run's plan keeps its retired_at, so its grace is never
    extended."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    publisher = pp.Publisher(s3=aws.s3, table=aws.table, bucket=BUCKET, environment="dev", say=lambda _: None)
    report = REAL_LINT(tree(tmp_path, {D2: DRAFT_V2}))
    plan = publisher.plan_publish(report.files, release_id=release_id(LATER), commit=COMMIT, skips={},
                                  current=publisher.current())  # fmt: skip
    assert plan.retire == [sha256_text(DRAFT_V1)]
    aws.table.update_item(
        Key={"prompt_hash": sha256_text(DRAFT_V1)},
        UpdateExpression="SET #status = :retired, retired_at = :at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":retired": "retired", ":at": "2026-10-25T03:30:00Z"},
    )
    before = row(aws, DRAFT_V1)
    publisher.apply(plan, LATER)
    assert row(aws, DRAFT_V1) == before


# --- write once, and the hash table as the record of every version -----------------------------------------


def test_a_file_published_before_is_never_uploaded_to_versions_again(aws, tmp_path):
    root = tree(tmp_path, {D1: DRAFT_V1})
    assert run(aws, root) == 0
    copy = versions_key("daily_cycle/draft", DRAFT_V1)
    before = object_versions(aws, copy)
    assert run(aws, root, now=LATER) == 0
    assert object_versions(aws, copy) == before
    # The file in latest/ kept its bytes and only had its tags replaced: no new object version.
    assert len(object_versions(aws, "daily_cycle/draft/latest/")) == 1
    assert tags(aws, D1)["release"] == release_id(LATER)


def test_the_conditional_hash_row_guards_versions_against_a_writer_since_the_plan(aws, tmp_path):
    """The guard is the row: a row that appears between the plan and step 1 wins, and its versions/
    copy is not written over."""
    report = REAL_LINT(tree(tmp_path, {D1: DRAFT_V1}))
    publisher = pp.Publisher(s3=aws.s3, table=aws.table, bucket=BUCKET, environment="dev", say=lambda _: None)
    plan = publisher.plan_publish(report.files, release_id=release_id(), commit=COMMIT, skips={},
                                  current=None)  # fmt: skip
    assert [item.version for item in plan.new_rows] == ["v1"]
    sha = sha256_text(DRAFT_V1)
    aws.table.put_item(
        Item={"prompt_hash": sha, "prompt_id": "daily_cycle/draft", "version": "v1", "status": "pending"}
    )
    copy = versions_key("daily_cycle/draft", DRAFT_V1)
    aws.s3.put_object(Bucket=BUCKET, Key=copy, Body=b"the other writer's copy")
    publisher.apply(plan, NOW)
    assert body(aws, copy) == "the other writer's copy"
    assert row(aws, DRAFT_V1)["status"] == "live"  # step 2 still makes the release's row live


def test_the_row_is_put_only_if_there_is_none(aws, tmp_path):
    calls = []
    real = aws.table.put_item

    def recorded(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    aws.table.put_item = recorded
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), table=aws.table) == 0
    assert calls == [
        {
            "Item": {
                "prompt_hash": sha256_text(DRAFT_V1),
                "prompt_id": "daily_cycle/draft",
                "version": "v1",
                "release": release_id(),
                "status": "pending",
            },
            "ConditionExpression": "attribute_not_exists(prompt_hash)",
        }
    ]


def test_a_pending_row_left_by_a_stopped_run_gets_its_upload_on_the_next_run(aws, tmp_path):
    sha = sha256_text(DRAFT_V1)
    aws.table.put_item(
        Item={"prompt_hash": sha, "prompt_id": "daily_cycle/draft", "version": "v1", "status": "pending"}
    )
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert body(aws, versions_key("daily_cycle/draft", DRAFT_V1)) == DRAFT_V1
    assert row(aws, DRAFT_V1)["status"] == "live"


def test_a_version_published_before_with_other_bytes_is_refused(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    before = snapshot(aws)
    assert run(aws, tree(tmp_path, {D1: DRAFT_V2}), now=LATER) == 1
    assert "daily_cycle/draft@v1 was published before with other bytes" in capsys.readouterr().err
    assert snapshot(aws) == before


def test_bytes_published_as_another_version_are_refused(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    before = snapshot(aws)
    # v1's text again, under a new name: the row would say v3 and lineage would lose v1.
    assert run(aws, tree(tmp_path, {D3: DRAFT_V1}), now=LATER2) == 1
    err = capsys.readouterr().err
    assert f"{D3} has the bytes published as daily_cycle/draft@v1: a text keeps its version" in err
    assert snapshot(aws) == before


def test_a_new_version_not_numbered_above_every_published_one_is_refused(aws, tmp_path, capsys):
    """git can forget a version (a revert), the hash table cannot."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D3: DRAFT_V3}), now=LATER) == 0
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), now=LATER2) == 0  # v3 reverted away
    before = snapshot(aws)
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER3) == 1
    assert "daily_cycle/draft@v2 is new but not numbered above v3" in capsys.readouterr().err
    assert snapshot(aws) == before
    assert run(aws, tree(tmp_path, {D4: DRAFT_V2}), now=LATER4) == 0  # the next number is fine


def test_two_files_of_one_release_with_the_same_bytes_are_refused(aws):
    """The lint refuses such a tree first; this is the publisher's own rule, for a caller that plans
    files the lint never saw. A hash row names one prompt id and one version."""
    raw = DRAFT_V1.encode("utf-8")
    files = [
        prompt_lint.AssetFile(f"prompts/{key}", key.split("/latest/")[0], prompt_lint.LATEST, "v1", raw)
        for key in (D1, S1)
    ]
    publisher = pp.Publisher(s3=aws.s3, table=aws.table, bucket=BUCKET, environment="dev", say=lambda _: None)
    with pytest.raises(pp.PublishError, match=f"^{S1} has the same bytes as {D1}$"):
        publisher.plan_publish(files, release_id=release_id(), commit=COMMIT, skips={}, current=None)
    assert snapshot(aws) == ([], [], [])


def test_a_revert_that_restores_a_published_file_serves_it_again(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), now=LATER2) == 0
    assert latest_keys(aws) == [D1] and served(aws).version == "v1"
    assert row(aws, DRAFT_V1)["status"] == "live" and "retired_at" not in row(aws, DRAFT_V1)
    assert row(aws, DRAFT_V2)["status"] == "retired"


# --- the order: a run stopped part-way -------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure", ["the row in step 1", "the upload in step 1", "step 2", "step 3's first copy"]
)
def test_a_run_that_stops_before_latest_changes_leaves_the_previous_release_serving(
    aws, tmp_path, monkeypatch, failure
):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    s3 = aws.s3
    table = aws.table
    if failure == "the row in step 1":
        monkeypatch.setattr(table, "put_item", boom)
    elif failure == "the upload in step 1":
        s3 = Failing(aws.s3, "put_object")
    elif failure == "step 2":
        monkeypatch.setattr(pp.Publisher, "_step2", boom)
    else:
        s3 = Failing(aws.s3, "copy_object")
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER, s3=s3, table=table) == 1
    assert current(aws)[prompts.CURRENT_RELEASE_FIELD] == release_id()
    assert latest_keys(aws) == [D1]
    assert row(aws, DRAFT_V1)["status"] == "live"
    prompt = served(aws)
    assert (prompt.source, prompt.version) == ("asset", "v1")
    # Whatever step 1 left is a pending row, which is never an approval, or nothing at all.
    left = row(aws, DRAFT_V2)
    assert left is None or left["status"] in ("pending", "live")


def test_step_2_never_makes_live_a_row_that_vanished_since_the_plan(aws, tmp_path):
    """Step 2 sets live only a row that exists (step 1 made it, or a run before): a row deleted
    since the plan is not made again from step 2's fields alone, and the previous release keeps
    serving."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    publisher = pp.Publisher(s3=aws.s3, table=aws.table, bucket=BUCKET, environment="dev", say=lambda _: None)
    report = REAL_LINT(tree(tmp_path, {D1: DRAFT_V1}))
    plan = publisher.plan_publish(report.files, release_id=release_id(LATER), commit=COMMIT, skips={},
                                  current=publisher.current())  # fmt: skip
    # Published before, so step 1 has nothing to put back.
    assert plan.new_rows == [] and [item.version for item in plan.live] == ["v1"]
    aws.table.delete_item(Key={"prompt_hash": sha256_text(DRAFT_V1)})
    with pytest.raises(pp.PublishError, match="^step 2 failed: "):
        publisher.apply(plan, LATER)
    assert row(aws, DRAFT_V1) is None
    assert latest_keys(aws) == [D1] and tags(aws, D1)["release"] == release_id()
    assert current(aws)[prompts.CURRENT_RELEASE_FIELD] == release_id()


def test_a_failure_is_reported_with_its_step_and_never_a_traceback(aws, tmp_path, capsys, monkeypatch):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    monkeypatch.setattr(pp.Publisher, "_step3", boom)
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 1
    assert capsys.readouterr().err == "publish: step 3 failed: RuntimeError('boom')\n"


def test_steps_run_in_the_order_of_section_4_2(aws, tmp_path, monkeypatch):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    order = []
    for number in range(1, 6):
        original = getattr(pp.Publisher, f"_step{number}")

        def recorded(self, plan, stamp, original=original, number=number):
            order.append(number)
            return original(self, plan, stamp)

        monkeypatch.setattr(pp.Publisher, f"_step{number}", recorded)
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    assert order == [1, 2, 3, 4, 5]


def test_every_write_of_a_publish_comes_in_the_order_of_section_4_2(aws, tmp_path):
    """Each new file's row then its upload, then the rows live, then latest/ (added, then removed),
    then the retirements, then the release's record and current.json last."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    calls = []
    s3 = Recording(aws.s3, "put_object", "copy_object", "put_object_tagging", "delete_object", calls=calls)
    table = Recording(aws.table, "put_item", "update_item", calls=calls)
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2, S1: SUMMARY_V1}), now=LATER, s3=s3, table=table) == 0
    assert calls == [
        ("put_item", "pending v2"),
        ("put_object", versions_key("daily_cycle/draft", DRAFT_V2)),
        ("put_item", "pending v1"),
        ("put_object", versions_key("research_tick/summary", SUMMARY_V1)),
        ("update_item", f"live {sha256_text(DRAFT_V2)[:6]}"),
        ("update_item", f"live {sha256_text(SUMMARY_V1)[:6]}"),
        ("copy_object", D2),
        ("copy_object", S1),
        ("delete_object", D1),
        ("update_item", f"retired {sha256_text(DRAFT_V1)[:6]}"),
        ("put_object", f"releases/{release_id(LATER)}.json"),
        ("put_object", "releases/current.json"),
    ]


def test_a_run_that_stopped_after_latest_changed_is_finished_by_the_next(aws, tmp_path, monkeypatch):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    root = tree(tmp_path, {D2: DRAFT_V2})
    with monkeypatch.context() as patched:
        patched.setattr(pp.Publisher, "_step4", boom)
        assert run(aws, root, now=LATER) == 1
    assert current(aws)[prompts.CURRENT_RELEASE_FIELD] == release_id()  # not recorded yet
    assert row(aws, DRAFT_V1)["status"] == "live"  # not retired yet
    assert run(aws, root, now=LATER2) == 0
    assert row(aws, DRAFT_V1)["status"] == "retired"  # every live row the release does not hold
    assert current(aws)[prompts.CURRENT_RELEASE_FIELD] == release_id(LATER2)


def test_current_json_is_written_after_the_releases_record(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    s3 = Failing(aws.s3, "put_object", after=2)  # the upload and the record go through; current.json fails
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER, s3=s3) == 1
    assert f"releases/{release_id(LATER)}.json" in keys(aws)
    assert current(aws)[prompts.CURRENT_RELEASE_FIELD] == release_id()


# --- what the sync may delete ----------------------------------------------------------------------------


def test_the_sync_deletes_only_stale_files_in_a_prompts_latest_folder(aws, tmp_path):
    others = [
        "daily_cycle/draft/versions/0000.txt",
        "releases/dev-20260101T0000-0000000.json",
        "notes/latest/readme.txt",
        "Not_A_Prompt/x/latest/v1.txt",
        "daily_cycle/draft/latest/",
        "daily_cycle/draft/latest/deeper/v1.txt",
    ]
    for key in [*others, "daily_cycle/draft/latest/v7.txt", "gear/theme/latest/v1.txt"]:
        aws.s3.put_object(Bucket=BUCKET, Key=key, Body=b"seeded")
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    left = keys(aws)
    assert set(others) <= left
    assert "daily_cycle/draft/latest/v7.txt" not in left and "gear/theme/latest/v1.txt" not in left
    assert D1 in left


@pytest.mark.parametrize(
    "key",
    [
        "daily_cycle/draft/versions/abc.txt",
        "releases/current.json",
        "daily_cycle/latest/v1.txt",
        "x/y/latest/a/b",
    ],
)
def test_the_one_delete_refuses_anything_outside_latest(aws, key):
    publisher = pp.Publisher(s3=aws.s3, table=aws.table, bucket=BUCKET, environment="dev")
    aws.s3.put_object(Bucket=BUCKET, Key=key, Body=b"kept")
    with pytest.raises(pp.PublishError, match="only latest/ files are ever deleted"):
        publisher._delete_latest(key)
    assert body(aws, key) == "kept"


def test_no_run_ever_deletes_outside_latest(aws, tmp_path):
    s3 = Recording(aws.s3, "delete_object", "delete_objects")
    for now, files, extra in (
        (NOW, {D1: DRAFT_V1, S1: SUMMARY_V1}, ()),
        (LATER, {D2: DRAFT_V2}, ()),
        (LATER2, {}, ("--release-id", release_id())),
        (LATER3, {D3: DRAFT_V3}, ("--clear-pin",)),
    ):
        assert run(aws, tree(tmp_path, files), *extra, now=now, s3=s3) == 0
    assert s3.calls and all(name == "delete_object" for name, _ in s3.calls)
    assert all(pp.LATEST_KEY.fullmatch(key) for _, key in s3.calls), s3.calls


# --- production and the skip rule ------------------------------------------------------------------------


def fake_lint(monkeypatch, skips: dict[str, str], seen: list | None = None):
    """The lint without its git second pass (these trees are not repositories), flagging `skips`
    the way the second pass would, and recording which release it was asked about."""

    def lint(root, *, specs=None, release_ref=None, base_ref=None):
        if seen is not None:
            seen.append(release_ref)
        report = REAL_LINT(root, specs=specs)
        report.skip_at_production = dict(skips)
        report.release = release_ref
        return report

    monkeypatch.setattr(prompt_lint, "lint", lint)


def test_production_leaves_a_skip_at_production_prompt_exactly_as_it_is(aws, tmp_path, monkeypatch):
    # Production already serves summary v1, published by an earlier release.
    fake_lint(monkeypatch, {})
    assert run(aws, tree(tmp_path, {S1: SUMMARY_V1}), "--release-ref", "v0.1", environment="production") == 0
    summary_v1 = object_versions(aws, "research_tick/summary/latest/")
    # The tree moves summary to v2, which the release production runs cannot read yet.
    seen = []
    reason = "v0.1's rules refuse it: unknown_placeholder"
    fake_lint(monkeypatch, {"research_tick/summary": reason}, seen)
    root = tree(tmp_path, {D1: DRAFT_V1, S2: SUMMARY_V2})
    tags_before, row_before = tags(aws, S1), row(aws, SUMMARY_V1)
    assert run(aws, root, "--release-ref", "v0.1", environment="production", now=LATER) == 0
    assert seen == ["v0.1"]
    assert latest_keys(aws) == [D1, S1]
    assert object_versions(aws, "research_tick/summary/latest/") == summary_v1
    # Its file keeps the earlier release's tags, and its row that release: carried, so neither
    # retired nor made live again by this one.
    assert tags(aws, S1) == tags_before and tags_before["release"] == release_id(NOW, "production")
    assert row(aws, SUMMARY_V1) == row_before and row_before["status"] == "live"
    assert row(aws, SUMMARY_V2) is None  # nothing of v2 was written: no row, no upload
    assert versions_key("research_tick/summary", SUMMARY_V2) not in keys(aws)
    manifest = json.loads(body(aws, f"releases/{release_id(LATER, 'production')}.json"))
    carried = {"prompt_id": "research_tick/summary", "version": "v1", "sha256": sha256_text(SUMMARY_V1),
               "key": S1, "carried_forward": True}  # fmt: skip
    assert carried in manifest["files"]
    assert manifest["skipped"] == [{"prompt_id": "research_tick/summary", "reason": reason}]


def test_the_skip_rule_applies_to_production_only(aws, tmp_path, monkeypatch):
    seen = []
    fake_lint(monkeypatch, {"research_tick/summary": "not in v0.1's registry"}, seen)
    assert run(aws, tree(tmp_path, {S1: SUMMARY_V1})) == 0
    assert seen == [None]  # dev never asks about the release
    assert latest_keys(aws) == [S1]


def test_production_needs_the_release_it_runs(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), environment="production") == 1
    assert "production needs --release-ref" in capsys.readouterr().err
    assert snapshot(aws) == ([], [], [])


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=publish test", "-c", "user.email=publish@example.com",
         "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return done.stdout


def test_production_skips_what_the_release_tags_own_registry_cannot_read(aws, tmp_path):
    """The real second pass, end to end: a release whose registry has only the draft prompt."""
    repo = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "lambdas" / "common", repo / "lambdas" / "common",
                    ignore=shutil.ignore_patterns("__pycache__"))  # fmt: skip
    (repo / "lambdas" / "common" / "prompts_registry.py").write_text(
        "from common.prompts import PromptSpec\n\n\n"
        "def all_specs():\n"
        f"    return [PromptSpec('daily_cycle/draft', {DRAFT.vanilla!r}, 'v1', {DRAFT.vanilla_sha256!r},\n"
        "                       frozenset({'topic'}), frozenset(), ('rules',), 2000)]\n",
        encoding="utf-8",
    )
    tree(repo, {D1: DRAFT_V1})
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "the release")
    git(repo, "tag", "v1.0.0")
    tree(repo, {D1: DRAFT_V1, S1: SUMMARY_V1})  # this code knows the summary; the release does not
    assert run(aws, repo, "--release-ref", "v1.0.0", environment="production") == 0
    assert latest_keys(aws) == [D1]
    manifest = json.loads(body(aws, f"releases/{release_id(environment='production')}.json"))
    skipped = {"prompt_id": "research_tick/summary", "reason": "not in v1.0.0's registry"}
    assert manifest["skipped"] == [skipped]


# --- the pin -----------------------------------------------------------------------------------------------


def test_a_pin_restores_an_earlier_release_and_holds_it_until_cleared(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), now=NOW) == 0
    root = tree(tmp_path, {D2: DRAFT_V2})
    assert run(aws, root, now=LATER) == 0
    assert row(aws, DRAFT_V1)["status"] == "retired"
    records = keys(aws, "releases/")
    record = f"releases/{release_id(NOW)}.json"
    before = object_versions(aws, record)

    # The rollback: release one again, from its record and versions/.
    assert run(aws, root, "--release-id", release_id(NOW), now=LATER2) == 0
    assert latest_keys(aws) == [D1] and body(aws, D1) == DRAFT_V1
    assert tags(aws, D1)["release"] == release_id(NOW)
    assert row(aws, DRAFT_V1)["status"] == "live" and row(aws, DRAFT_V1)["release"] == release_id(NOW)
    assert row(aws, DRAFT_V2)["status"] == "retired"
    found = current(aws)
    assert found[prompts.CURRENT_RELEASE_FIELD] == found[prompts.PINNED_RELEASE_FIELD] == release_id(NOW)
    assert served(aws).version == "v1"
    # A pin writes no release record: none of its own, and the pinned release's is left as it was
    # (its published_at, previous_release and skipped are that release's).
    assert keys(aws, "releases/") == records and object_versions(aws, record) == before

    # A later merge publishes the pinned release again, not the tree.
    capsys.readouterr()
    newer = tree(tmp_path, {D3: DRAFT_V3})
    assert run(aws, newer, now=LATER3) == 0
    assert "is pinned" in capsys.readouterr().out
    assert latest_keys(aws) == [D1] and row(aws, DRAFT_V3) is None
    assert current(aws)[prompts.PINNED_RELEASE_FIELD] == release_id(NOW)
    assert keys(aws, "releases/") == records and object_versions(aws, record) == before

    # Until a run clears it, which publishes the tree as a new release.
    assert run(aws, newer, "--clear-pin", now=LATER4) == 0
    assert latest_keys(aws) == [D3]
    found = current(aws)
    assert found[prompts.CURRENT_RELEASE_FIELD] == release_id(LATER4)
    assert found[prompts.PINNED_RELEASE_FIELD] is None
    assert row(aws, DRAFT_V1)["status"] == "retired"


def test_a_pin_holds_on_production_releases_too(aws, tmp_path, monkeypatch, capsys):
    fake_lint(monkeypatch, {})
    prod = {"environment": "production"}
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), "--release-ref", "v1", **prod) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), "--release-ref", "v2", now=LATER, **prod) == 0
    assert run(aws, tmp_path, "--release-id", release_id(NOW, "production"), now=LATER2, **prod) == 0
    capsys.readouterr()
    # The next release's publish honours the pin.
    assert run(aws, tree(tmp_path, {D3: DRAFT_V3}), "--release-ref", "v3", now=LATER3, **prod) == 0
    assert "is pinned" in capsys.readouterr().out and latest_keys(aws) == [D1]


def test_clearing_a_pin_that_is_not_set_is_an_ordinary_publish(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1}), "--clear-pin") == 0
    assert latest_keys(aws) == [D1] and current(aws)[prompts.PINNED_RELEASE_FIELD] is None


def test_a_pin_and_a_clear_in_one_run_are_refused_as_usage(aws, tmp_path):
    with pytest.raises(SystemExit) as exited:
        run(aws, tree(tmp_path, {}), "--release-id", release_id(), "--clear-pin")
    assert exited.value.code == 2


def test_a_pin_restores_the_files_a_production_release_carried_forward(aws, tmp_path, monkeypatch):
    def publish(files, skips, now):
        fake_lint(monkeypatch, skips)
        return run(aws, tree(tmp_path, files), "--release-ref", "v1", environment="production", now=now)

    assert publish({S1: SUMMARY_V1}, {}, NOW) == 0
    skips = {"research_tick/summary": "not in v1's registry"}
    assert publish({D1: DRAFT_V1, S2: SUMMARY_V2}, skips, LATER) == 0
    # A later release holds nothing; pinning the one that carried summary v1 brings it back.
    assert publish({}, {}, LATER2) == 0
    assert latest_keys(aws) == []
    pinned = ("--release-id", release_id(LATER, "production"))
    assert run(aws, tmp_path, *pinned, environment="production", now=LATER3) == 0
    assert latest_keys(aws) == [D1, S1]
    assert row(aws, SUMMARY_V1)["status"] == "live"


@pytest.mark.parametrize(
    ("given", "message"),
    [
        ("dev-20261025T0300-0000000", "there is no release"),
        ("production-20261025T0300-abcdef0", "is not a release id of dev"),
        ("dev-latest", "is not a release id of dev"),
        ("dev-20261025T0300-abcdef0; rm -rf /", "is not a release id of dev"),
        ("../current", "is not a release id of dev"),
    ],
)
def test_a_pin_refuses_a_release_this_deployment_does_not_have(aws, tmp_path, capsys, given, message):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    before = snapshot(aws)
    assert run(aws, tmp_path, "--release-id", given, now=LATER) == 1
    assert message in capsys.readouterr().err
    assert snapshot(aws) == before


@pytest.mark.parametrize(
    "record",
    [b"not json", b"[]", b'{"files": "none"}', b'{"files": [{"prompt_id": "daily_cycle/draft"}]}'],
)
def test_a_pin_refuses_a_release_record_it_cannot_read(aws, tmp_path, capsys, record):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    aws.s3.put_object(Bucket=BUCKET, Key="releases/dev-20261025T0400-abcdef0.json", Body=record)
    before = snapshot(aws)
    assert run(aws, tmp_path, "--release-id", "dev-20261025T0400-abcdef0", now=LATER) == 1
    assert capsys.readouterr().err.startswith("publish: ")
    assert snapshot(aws) == before


@pytest.mark.parametrize("gone", ["its versions/ copy", "its hash row"])
def test_a_pin_refuses_a_release_whose_file_it_cannot_restore_before_writing_anything(
    aws, tmp_path, capsys, gone
):
    """Checked for every file before step 1: otherwise step 2 would make the rows live and step 3's
    copy from versions/ would fail after them."""
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    if gone == "its versions/ copy":
        aws.s3.delete_object(Bucket=BUCKET, Key=versions_key("daily_cycle/draft", DRAFT_V1))
    else:
        aws.table.delete_item(Key={"prompt_hash": sha256_text(DRAFT_V1)})
    before = snapshot(aws)
    assert run(aws, tmp_path, "--release-id", release_id(), now=LATER2) == 1
    assert f"{release_id()}'s {D1} is not in versions/ with a hash row" in capsys.readouterr().err
    assert snapshot(aws) == before


def test_an_unreadable_current_json_stops_the_run(aws, tmp_path, capsys):
    aws.s3.put_object(Bucket=BUCKET, Key="releases/current.json", Body=b"{")
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 1
    assert "releases/current.json is not JSON" in capsys.readouterr().err
    assert row(aws, DRAFT_V1) is None


# --- the dry run, the lint, and what is printed -------------------------------------------------------------


def test_a_dry_run_prints_the_plan_and_writes_nothing(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    before = snapshot(aws)
    capsys.readouterr()
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2, S1: SUMMARY_V1}), "--dry-run", now=LATER) == 0
    assert snapshot(aws) == before
    out = capsys.readouterr().out
    assert f"release {release_id(LATER)} of commit abcdef0, in dev; previous: {release_id()}" in out
    assert f"daily_cycle/draft: one file: v2 ({sha256_text(DRAFT_V2)[:12]}, new)" in out
    assert "step 1: 2 hash row(s) to add as pending" in out
    assert "step 3: 2 file(s) into latest/, 0 retagged, 1 to remove" in out
    assert f"  remove {D1}" in out
    assert "step 4: 1 row(s) to retire" in out
    assert "dry run: nothing was written" in out and "published" not in out.splitlines()[-1]


def test_a_dry_run_of_a_first_publish_writes_nothing(aws, tmp_path):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1, D2: DRAFT_V2}), "--dry-run") == 0
    assert snapshot(aws) == ([], [], [])


def test_a_dry_run_of_a_pin_writes_nothing_either(aws, tmp_path, capsys):
    assert run(aws, tree(tmp_path, {D1: DRAFT_V1})) == 0
    assert run(aws, tree(tmp_path, {D2: DRAFT_V2}), now=LATER) == 0
    before = snapshot(aws)
    assert run(aws, tmp_path, "--release-id", release_id(), "--dry-run", now=LATER2) == 0
    assert snapshot(aws) == before
    assert f"release {release_id()} again (pinned)" in capsys.readouterr().out


def test_a_tree_the_lint_refuses_publishes_nothing(aws, tmp_path, capsys):
    bad = "Write about {{subject}}.\n{{locked:rules}}\n"  # a placeholder the spec does not name
    assert run(aws, tree(tmp_path, {D1: bad})) == 1
    err = capsys.readouterr().err
    assert "unknown_placeholder" in err and "nothing was published" in err
    assert snapshot(aws) == ([], [], [])


def test_nothing_printed_holds_a_prompt_text(aws, tmp_path, capsys):
    root = tree(tmp_path, {D1: DRAFT_V1, D2: DRAFT_V2})
    assert run(aws, root, "--dry-run") == 0
    assert run(aws, root) == 0
    assert run(aws, tree(tmp_path, {D1: DRAFT_V3}), now=LATER) == 1  # v1 with other bytes
    assert run(aws, tree(tmp_path, {D3: DRAFT_V1}), now=LATER) == 1  # v1's bytes as v3
    bad = f"{MARKER} {{{{subject}}}}\n{{{{locked:rules}}}}\n"
    assert run(aws, tree(tmp_path, {D4: bad}), now=LATER2) == 1
    assert run(aws, tmp_path, "--release-id", release_id(), now=LATER3) == 0
    captured = capsys.readouterr()
    assert captured.out and captured.err
    assert MARKER not in captured.out + captured.err


# --- names -----------------------------------------------------------------------------------------


def test_the_bucket_and_table_are_named_as_terraform_names_them():
    module = (REPO_ROOT / "infra" / "modules" / "prompts" / "main.tf").read_text(encoding="utf-8")
    assert 'bucket = "${var.unique_name_prefix}-${var.environment_name}-prompts"' in module
    assert 'name         = "${var.unique_name_prefix}-${var.environment_name}-prompt-hashes"' in module
    for environment in pp.ENVIRONMENTS:
        assert pp.bucket_name(environment) == f"{naming.NAME_PREFIX}-{environment}-prompts"
        assert pp.table_name(environment) == f"{naming.NAME_PREFIX}-{environment}-prompt-hashes"
        # Both roots name their environment the way the publisher does.
        root = (REPO_ROOT / "infra" / "environments" / environment / "main.tf").read_text(encoding="utf-8")
        block = root.split('module "prompts" {')[1].split("\n}\n")[0]
        assert re.search(rf'environment_name\s*=\s*"{environment}"', block)


def test_a_release_id_is_the_environment_the_minute_and_the_short_commit():
    stamp = datetime(2026, 1, 2, 3, 4, 59, tzinfo=UTC)
    assert pp.make_release_id("production", COMMIT, stamp) == "production-20260102T0304-abcdef0"
    assert pp.RELEASE_ID.fullmatch(pp.make_release_id("dev", COMMIT, NOW))


def test_the_hash_table_is_read_in_the_buckets_region_whatever_region_the_run_signed_in_to(aws, tmp_path):
    """A run under `production-prompts` cannot see a `production` value of AWS_REGION, so it may sign
    in somewhere else than production is: the publisher's own clients (none stood in here) find the
    bucket's region and read the table there."""
    elsewhere, bucket, hashes = "eu-west-1", f"{BUCKET}-elsewhere", f"{HASHES}Elsewhere"
    assert elsewhere != REGION
    boto3.client("s3", region_name=elsewhere).create_bucket(
        Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": elsewhere}
    )
    table = boto3.resource("dynamodb", region_name=elsewhere).create_table(
        TableName=hashes,
        KeySchema=[{"AttributeName": "prompt_hash", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "prompt_hash", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    root = tree(tmp_path, {D1: DRAFT_V1})
    given = ["--environment", "dev", "--root", str(root), "--commit", COMMIT,
             "--bucket", bucket, "--table", hashes]  # fmt: skip
    assert pp.main(given, now=NOW) == 0
    assert table.get_item(Key={"prompt_hash": sha256_text(DRAFT_V1)})["Item"]["status"] == "live"


def test_a_bucket_that_cannot_be_found_is_reported_and_never_a_traceback(aws, tmp_path, capsys):
    root = tree(tmp_path, {D1: DRAFT_V1})
    given = ["--environment", "dev", "--root", str(root), "--commit", COMMIT, "--bucket", "no-such-bucket"]
    assert pp.main(given, now=NOW) == 1
    assert capsys.readouterr().err.startswith("publish: AWS refused or could not be reached: ClientError(")


# --- the workflow: .github/workflows/prompts.yml ---------------------------------------------------

PATHS = [
    "prompts/**",
    "lambdas/common/prompts*.py",
    "scripts/prompt_lint.py",
    "scripts/publish_prompts.py",
    ".github/workflows/prompts.yml",
]


def workflow() -> dict:
    return yaml.safe_load((WORKFLOWS / "prompts.yml").read_text(encoding="utf-8"))


def triggers() -> dict:
    data = workflow()
    return data.get("on", data.get(True))  # YAML 1.1 reads a bare `on` key as True


def jobs() -> dict:
    return workflow()["jobs"]


def steps(job: str) -> list[dict]:
    return jobs()[job]["steps"]


def step(job: str, name: str) -> dict:
    return next(s for s in steps(job) if s.get("name") == name)


def uses(text: str, action: str) -> set[str]:
    return set(re.findall(rf"uses: ({re.escape(action)}@[0-9a-f]{{40}})\n", text))


def test_the_workflow_gates_pull_requests_and_publishes_merges_on_the_same_paths():
    on = triggers()
    assert on["pull_request"] == {"paths": PATHS}
    assert on["push"] == {"branches": ["dev"], "paths": PATHS}
    assert on["release"] == {"types": ["published"]}
    inputs = on["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"environment", "release_id", "clear_pin", "dry_run"}
    assert inputs["environment"]["options"] == ["production", "dev"]
    assert inputs["release_id"]["required"] is False and inputs["release_id"]["default"] == ""
    # Every path filter names something that exists, the prompts core among them.
    assert {path.name for path in (REPO_ROOT / "lambdas" / "common").glob("prompts*.py")} >= {
        "prompts.py",
        "prompts_registry.py",
    }
    for path in PATHS[2:]:
        assert (REPO_ROOT / path).is_file(), path
    assert set(jobs()) == {"gates", "publish-dev", "publish-production"}
    assert workflow()["permissions"] == {"contents": "read"}


def test_the_gates_run_only_for_a_pull_request_and_never_with_a_secret():
    """Every step of the gates job runs code from the pull request, so no secret may be in reach of
    the job: pr-checks.yml's pii-denylist job checks the list on every pull request, and the
    publish jobs check it again over what they publish."""
    gates = jobs()["gates"]
    assert gates["if"] == "github.event_name == 'pull_request'"
    assert gates["permissions"] == {"contents": "read", "pull-requests": "read"}
    text = yaml.safe_dump(gates, width=10**6)
    assert "secrets." not in text and "id-token" not in text and "configure-aws-credentials" not in text
    assert "environment" not in gates
    denylist = step("gates", "Personal-data denylist")
    assert "PII_DENYLIST" not in denylist.get("env", {})
    assert denylist["run"] == 'python3 scripts/pii_denylist_check.py --range "$BASE" "$HEAD"'

def test_no_publish_job_runs_for_a_pull_request_and_each_waits_for_the_switch():
    # Each folds to one line (no line more indented than the first, which YAML would keep).
    assert jobs()["publish-dev"]["if"] == (
        "vars.PROMPT_AS_ASSET_ENABLED == 'true' && github.ref == 'refs/heads/dev' && "
        "(github.event_name == 'push' || (github.event_name == 'workflow_dispatch' && "
        "inputs.environment == 'dev'))"
    )
    assert jobs()["publish-production"]["if"] == (
        "vars.PROMPT_AS_ASSET_ENABLED == 'true' && (github.event_name == 'release' || "
        "(github.event_name == 'workflow_dispatch' && inputs.environment == 'production' && "
        "github.ref == 'refs/heads/dev'))"
    )
    for name, job in jobs().items():
        signs_in = any("configure-aws-credentials" in str(s.get("uses", "")) for s in job["steps"])
        assert signs_in == (name != "gates"), name
        if signs_in:
            assert "pull_request" not in job["if"]
            assert job["if"].startswith("vars.PROMPT_AS_ASSET_ENABLED == 'true' && ")


def test_dev_publishes_in_no_environment_and_production_in_its_two():
    assert "environment" not in jobs()["publish-dev"]
    assert jobs()["publish-production"]["environment"] == (
        "${{ github.event_name == 'release' && 'production' || 'production-prompts' }}"
    )


def test_each_deployment_publishes_in_its_own_concurrency_group_and_none_is_cancelled():
    groups = {name: jobs()[name]["concurrency"] for name in ("publish-dev", "publish-production")}
    assert groups == {
        "publish-dev": {"group": "prompts-publish-dev", "cancel-in-progress": False},
        "publish-production": {"group": "prompts-publish-production", "cancel-in-progress": False},
    }


def test_the_publish_jobs_sign_in_as_the_publisher_through_the_action_terraform_yml_pins():
    terraform = (WORKFLOWS / "terraform.yml").read_text(encoding="utf-8")
    pinned = uses(terraform, "aws-actions/configure-aws-credentials")
    assert len(pinned) == 1
    roles = {"publish-dev": "AWS_DEV_PROMPTS_ROLE_ARN", "publish-production": "AWS_PROD_PROMPTS_ROLE_ARN"}
    for job, secret in roles.items():
        assert jobs()[job]["permissions"] == {"id-token": "write", "contents": "read"}
        sign_in = step(job, "Configure AWS credentials")
        assert sign_in["uses"] in pinned
        assert sign_in["with"] == {
            "aws-region": "${{ vars.AWS_REGION || 'ap-southeast-2' }}",
            "role-to-assume": f"${{{{ secrets.{secret} }}}}",
        }
        # The names come from the deploy's prefix (unset: common/naming.py's default).
        assert jobs()[job]["env"] == {"NAME_PREFIX": "${{ vars.UNIQUE_NAME_PREFIX }}"}


def test_the_gates_pin_the_same_tools_as_the_other_workflows():
    text = (WORKFLOWS / "prompts.yml").read_text(encoding="utf-8")
    pr_checks = (WORKFLOWS / "pr-checks.yml").read_text(encoding="utf-8")
    security = (WORKFLOWS / "security.yml").read_text(encoding="utf-8")
    python_ci = (WORKFLOWS / "python-ci.yml").read_text(encoding="utf-8")
    for action, source in (
        ("trufflesecurity/trufflehog", pr_checks),
        ("gitleaks/gitleaks-action", pr_checks),
        ("actions/checkout", security),
        ("actions/setup-python", python_ci),
    ):
        assert uses(text, action) == uses(source, action) and uses(text, action), action
    for pin in (r"\n          version: \d+\.\d+\.\d+\n", r"GITLEAKS_VERSION: \d+\.\d+\.\d+\n",
                r"GITLEAKS_CONFIG: \S+\n", r"VERSION=\d+\.\d+\.\d+\n", r"SHA256=[0-9a-f]{64}\n",
                r"CHECKOV_VERSION=\d+\.\d+\.\d+\n"):  # fmt: skip
        found = set(re.findall(pin, text))
        assert found and found <= set(re.findall(pin, pr_checks + security)), pin
    # Trivy runs exactly as security.yml runs it: the report, then the gate.
    assert re.findall(r"trivy fs (.*?)\n\n", text, re.S) == re.findall(r"trivy fs (.*?)\n\n", security, re.S)
    gates = jobs()["gates"]
    first = ["Prompt lint", "Personal-data denylist", "Trufflehog secret scan"]
    assert [s["name"] for s in gates["steps"]][3:6] == first
    assert step("gates", "Lint and publisher tests")["run"] == (
        "python -m pytest -q scripts/tests/test_prompt_lint.py scripts/tests/test_publish_prompts.py"
    )
    checkov = step("gates", "Checkov (this workflow and the prompts module)")["run"]
    assert "checkov --config-file .checkov.yaml --framework github_actions " in checkov
    assert "--file .github/workflows/prompts.yml\n" in checkov
    assert "checkov --config-file .checkov.yaml --directory infra/modules/prompts" in checkov
    # The lint holds versions to the base and reads the latest release; the scans read the PR's range.
    lint = step("gates", "Prompt lint")
    assert lint["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert '"--base-ref=$BASE"' in lint["run"] and '"--release-ref=$release"' in lint["run"]
    assert "python scripts/prompt_lint.py" in lint["run"]
    assert step("gates", "Checkout")["with"] == {"fetch-depth": 0}


@pytest.mark.parametrize(
    ("gh", "status", "passed"),
    [
        ("echo v0.4.0", 0, ["--base-ref=b4se", "--release-ref=v0.4.0"]),
        # gh's words when the repository has no release: production runs none.
        ("echo 'release not found' >&2; exit 1", 0, ["--base-ref=b4se", "--no-release"]),
        ("echo 'HTTP 502: Bad Gateway' >&2; exit 1", 1, None),
        ("true", 1, None),  # no failure, and no release named either
    ],
)
def test_the_gates_lint_against_the_release_gh_names_and_never_a_tag_of_their_own(
    tmp_path, gh, status, passed
):
    """The step's own script, with gh and python stood in for: the release `gh release view`
    names, none when gh says there is none, and a failed gate (never a guess) when gh fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    called = tmp_path / "called"
    for name, script in (("gh", gh), ("python", f'printf "%s\\n" "$@" > "{called}"')):
        (bin_dir / name).write_text(f"#!/bin/sh\n{script}\n", encoding="utf-8")
        (bin_dir / name).chmod(0o755)
    environment = {"PATH": f"{bin_dir}:/usr/bin:/bin", "BASE": "b4se", "TMPDIR": str(tmp_path)}
    done = subprocess.run(
        ["bash", "-c", step("gates", "Prompt lint")["run"]],
        env=environment, cwd=tmp_path, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert done.returncode == status, done.stderr
    if passed is None:
        assert not called.exists()  # the lint never ran
        assert "Could not tell which release production runs:" in done.stderr
    else:
        assert called.read_text(encoding="utf-8").splitlines() == ["scripts/prompt_lint.py", *passed]


def test_the_publish_jobs_check_the_tree_for_personal_data_with_the_list():
    for job in ("publish-dev", "publish-production"):
        check = step(job, "Personal-data denylist (the tree being published)")
        assert check["env"] == {"PII_DENYLIST": "${{ secrets.PII_DENYLIST }}"}
        assert 'python3 scripts/pii_denylist_check.py --files "${files[@]}"' in check["run"]
        names = [s.get("name") for s in steps(job)]
        assert names.index("Personal-data denylist (the tree being published)") < names.index("Publish")


def test_the_production_publish_names_the_release_production_runs():
    found = step("publish-production", "The release production runs")
    assert found["env"]["RELEASE_TAG"] == "${{ github.event.release.tag_name }}"
    assert "gh release view --json tagName --jq .tagName" in found["run"]
    assert 'echo "RELEASE_REF=$tag" >> "$GITHUB_ENV"' in found["run"]
    publish = step("publish-production", "Publish")["run"]
    assert 'args=(--environment production "--release-ref=$RELEASE_REF")' in publish
    # Every tag, for the worktree; a release's run checks out its tag (a dispatch's ref is empty, its
    # own), as terraform-production-release.yml's apply does.
    checkout = step("publish-production", "Checkout")["with"]
    assert checkout == {"ref": "${{ github.event.release.tag_name }}", "fetch-depth": 0}
    release = yaml.safe_load((WORKFLOWS / "terraform-production-release.yml").read_text(encoding="utf-8"))
    deploy = next(s for s in release["jobs"]["apply"]["steps"] if s.get("name") == "Checkout")
    assert checkout == deploy["with"]
    assert "args=(--environment dev)" in step("publish-dev", "Publish")["run"]


def test_a_release_publishes_production_only_when_its_commit_is_on_prod():
    """terraform-production-release.yml's guard, first: the deploy refuses a release cut from
    another branch, so its prompts must not reach production either, and no AWS sign-in is made for
    it. The commit is the checked-out tag's, never github.sha."""
    guard = step("publish-production", "Verify release commit is on prod")
    assert guard["if"] == "github.event_name == 'release'"
    release = (WORKFLOWS / "terraform-production-release.yml").read_text(encoding="utf-8")
    for line in (
        "git fetch origin prod",
        'release_sha="$(git rev-parse HEAD)"',
        'git merge-base --is-ancestor "$release_sha" origin/prod ||',
    ):
        assert line in guard["run"] and line in release, line
    assert "github.sha" not in guard["run"] and guard["run"].startswith("set -euo pipefail\n")
    names = [s.get("name") for s in steps("publish-production")]
    order = [names.index(name) for name in ("Checkout", "Verify release commit is on prod",
                                            "Configure AWS credentials", "Publish")]  # fmt: skip
    assert order == sorted(order) and order[1] == order[0] + 1


def test_no_expression_is_interpolated_into_a_shell_script():
    """Dispatch inputs and event text reach a script through env only, never its source."""
    for name, job in jobs().items():
        for item in job["steps"]:
            assert "${{" not in item.get("run", ""), (name, item.get("name"))
    for job in ("publish-dev", "publish-production"):
        assert step(job, "Publish")["env"] == {
            "RELEASE_ID": "${{ inputs.release_id }}",
            "CLEAR_PIN": "${{ inputs.clear_pin }}",
            "DRY_RUN": "${{ inputs.dry_run }}",
        }
        assert '"--release-id=$RELEASE_ID"' in step(job, "Publish")["run"]
