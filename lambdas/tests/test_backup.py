"""Backups (common/backup.py): a snapshot file, restoring it into an empty deployment, exports, and
the Admin API's job plumbing (admin_api_handler.py's backup routes and its "backup_job" event)."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from moto.s3 import models as moto_s3_models
from table_schemas import create_table

import admin_api_handler
from common import backup

REGION = "ap-southeast-2"
BACKUP_BUCKET = "state-bucket"
CONTENT_BUCKET = "content-bucket"
EXPORT_BUCKET = "archive-bucket"

# Logical name -> (test table name, hash key, range key or None)
SCHEMAS = {
    "topics": ("Topics", "topic_id", None),
    "models": ("Models", "model_id", None),
    "model_config": ("ModelConfig", "config_id", None),
    "prompt_refinements": ("PromptRefinements", "topic_id", "version"),
    "articles": ("Articles", "article_id", None),
    "moderation_queue": ("ModerationQueue", "queue_id", None),
    "musings": ("Musings", "musing_id", None),
    "feedback": ("Feedback", "article_id", "feedback_id"),
    "stats_current": ("StatsCurrent", "stats_id", None),
    "stats_history": ("StatsHistory", "week_start", None),
    "view_counts": ("ViewCounts", "counter_id", None),
    # Left out of every snapshot.
    "findings": ("Findings", "topic_id", "captured_at"),
}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    monkeypatch.setenv("BACKUP_BUCKET", BACKUP_BUCKET)
    monkeypatch.setenv("BACKUP_EXPORT_BUCKETS", EXPORT_BUCKET)
    monkeypatch.setenv("CONTENT_BUCKET", CONTENT_BUCKET)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "bloggerbear-dev-admin-api")
    monkeypatch.setenv("FINDINGS_TABLE", "Findings")
    for name, env_var in backup.TABLES.items():
        monkeypatch.setenv(env_var, SCHEMAS[name][0])
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None
    backup._s3_client = backup._dynamodb_client = backup._lambda_client = None


def _create_tables():
    client = boto3.client("dynamodb", region_name=REGION)
    for table, hash_key, range_key in SCHEMAS.values():
        keys = [{"AttributeName": hash_key, "KeyType": "HASH"}]
        attrs = [{"AttributeName": hash_key, "AttributeType": "S"}]
        if range_key:
            keys.append({"AttributeName": range_key, "KeyType": "RANGE"})
            attrs.append({"AttributeName": range_key, "AttributeType": "S"})
        create_table(
            client, TableName=table, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST"
        )


def _create_buckets():
    s3 = boto3.client("s3", region_name=REGION)
    for bucket in (BACKUP_BUCKET, CONTENT_BUCKET, EXPORT_BUCKET):
        s3.create_bucket(Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION})
    return s3


@pytest.fixture
def aws():
    with mock_aws():
        _create_tables()
        yield _create_buckets()


def _table(name):
    return boto3.resource("dynamodb", region_name=REGION).Table(SCHEMAS[name][0])


def _article(article_id, *, status="published", figures=None, **extra):
    item = {
        "article_id": article_id,
        "topic_id": "crypto",
        "status": status,
        "created_at": f"2026-10-0{article_id[-1]}T00:00:00+00:00",
        "title": f"Title {article_id}",
        "body_s3_key": f"articles/{article_id}.md",
        "lineage": {"total_cost_aud": Decimal("0.0123"), "models_used": ["m1"]},
        **extra,
    }
    if status == "published":
        item["published_at"] = item["created_at"]
        item["published_by"] = "bear"
    if figures:
        item["figures"] = figures
    return item


def _seed(s3, articles=3):
    _table("topics").put_item(
        Item={"topic_id": "crypto", "name": "Crypto", "research_interval_hours": Decimal("2")}
    )
    _table("models").put_item(Item={"model_id": "m1", "display_name": "Model One"})
    _table("model_config").put_item(Item={"config_id": "default", "default_model_id": "m1"})
    _table("model_config").put_item(Item={"config_id": "pipeline", "stagger_minutes": Decimal(5)})
    _table("model_config").put_item(Item={"config_id": "verification-secret", "secret": "s3cr3t"})
    _table("model_config").put_item(Item={"config_id": "fb-window-1", "count": 3, "expires_at": 1})
    _table("prompt_refinements").put_item(
        Item={"topic_id": "crypto", "version": "2026-10-01", "status": "approved", "rarity": "rare"}
    )
    _table("musings").put_item(Item={"musing_id": "mu-1", "text": "A thought.", "tags": {"a", "b"}})
    _table("stats_history").put_item(Item={"week_start": "all-time", "cost_aud": Decimal("12.5")})
    _table("view_counts").put_item(Item={"counter_id": "a1#0", "count": Decimal(7)})
    _table("findings").put_item(Item={"topic_id": "crypto", "captured_at": "x"})
    for n in range(1, articles + 1):
        article_id = f"a{n}"
        extra = {}
        if n == 1:
            extra = {
                "body_original_s3_key": "articles/a1.original.md",
                "rewrites": [{"number": 1, "previous_body_s3_key": "articles/a1.before-rewrite-1.md",
                              "previous_title": "Old", "cost_aud": Decimal("0.01")}],
            }  # fmt: skip
        figures = [{"key": "vision/crypto/site/scene.png", "caption": "c", "alt": "a"}] if n == 2 else None
        _table("articles").put_item(Item=_article(article_id, figures=figures, **extra))
        s3.put_object(Bucket=CONTENT_BUCKET, Key=f"articles/{article_id}.md", Body=f"# Body {n}\n".encode())
    s3.put_object(Bucket=CONTENT_BUCKET, Key="articles/a1.original.md", Body=b"original")
    s3.put_object(Bucket=CONTENT_BUCKET, Key="articles/a1.before-rewrite-1.md", Body=b"before")
    s3.put_object(Bucket=CONTENT_BUCKET, Key="snapshots/crypto/1.json", Body=b"{}")
    s3.put_object(Bucket=CONTENT_BUCKET, Key="vision/crypto/site/scene.png", Body=PNG)


@pytest.fixture
def seeded(aws):
    _seed(aws)
    return aws


PNG = b"\x89PNG\r\n\x1a\n\xff\x00"


def _forever():
    return 600_000


def _budget(checks):
    """A step's clock that has time for its first `checks` looks, then none: each step gets a
    little done and leaves the rest to the next, as a real one does when its 30 seconds run low."""
    left = {"n": checks}

    def time_left():
        left["n"] -= 1
        return 600_000 if left["n"] >= 0 else 0

    return time_left


def _run(job, time_left=_forever, hooks=None, checks=None):
    for _ in range(500):
        clock = _budget(checks) if checks else time_left
        job = backup.run_step(job, time_left_ms=clock, hooks=hooks)
        if job["status"] != "running":
            return job
    raise AssertionError("the job never finished")


def _lines(s3, key):
    body = s3.get_object(Bucket=BACKUP_BUCKET, Key=key)["Body"].read()
    return [json.loads(line) for line in gzip.decompress(body).decode().splitlines() if line]


def _snapshot(s3, **kwargs):
    job = _run(backup.start_snapshot("manual"), **kwargs)
    assert job["status"] == "succeeded", job
    return job


def _hooks():
    return backup.Hooks(
        apply_schedules=MagicMock(return_value={"written": 1, "failed": []}),
        render_page=MagicMock(),
        refresh_stats=MagicMock(),
        load_topics=lambda: _table("topics").scan()["Items"],
        query_published=backup.published_page,
    )


# --- snapshot ----------------------------------------------------------------------------------


def test_a_snapshot_is_one_file_of_content_and_configuration(seeded):
    aws = seeded
    job = _snapshot(aws)

    key = job["result"]["key"]
    assert key.startswith("backup/bloggerbear-dev/snapshots/") and key.endswith("-manual.jsonl.gz")
    lines = _lines(aws, key)
    assert lines[0]["t"] == "header" and lines[0]["format"] == backup.FORMAT
    assert lines[-1]["t"] == "end"
    tables = {line["table"] for line in lines if line["t"] == "item"}
    assert tables == {"topics", "models", "model_config", "prompt_refinements", "articles", "musings",
                      "stats_history", "view_counts"}  # fmt: skip
    # Settings rows, not secrets or rows set to expire.
    config_ids = {line["item"]["config_id"]["S"] for line in lines if line.get("table") == "model_config"}
    assert config_ids == {"default", "pipeline"}
    # The final bodies and the figure, never the research snapshots or older texts.
    objects = {line["key"] for line in lines if line["t"] == "object"}
    assert objects == {"articles/a1.md", "articles/a2.md", "articles/a3.md", "vision/crypto/site/scene.png"}
    assert lines[-1]["counts"]["articles"] == 3 and lines[-1]["counts"]["objects"] == 4
    # Parts are cleaned up.
    left = aws.list_objects_v2(Bucket=BACKUP_BUCKET, Prefix=f"backup/bloggerbear-dev/jobs/{job['job_id']}/")
    assert left.get("KeyCount", 0) == 0


def test_an_article_keeps_its_rewrite_history_but_not_pointers_to_texts_left_out(seeded):
    lines = _lines(seeded, _snapshot(seeded)["result"]["key"])

    articles = [line["item"] for line in lines if line.get("table") == "articles"]
    (a1,) = (item for item in articles if item["article_id"]["S"] == "a1")
    assert "body_original_s3_key" not in a1
    (rewrite,) = a1["rewrites"]["L"]
    assert "previous_body_s3_key" not in rewrite["M"]
    assert rewrite["M"]["previous_title"] == {"S": "Old"}
    assert a1["lineage"]["M"]["total_cost_aud"] == {"N": "0.0123"}


def test_a_snapshot_spread_over_many_steps_is_the_same_file(aws, monkeypatch):
    _seed(aws, articles=7)
    monkeypatch.setattr(backup, "_SCAN_PAGE", 2)
    # So parts are both copied whole and joined (moto enforces S3's own minimum too).
    monkeypatch.setattr(backup, "_MIN_PART", 300)
    monkeypatch.setattr(moto_s3_models, "S3_UPLOAD_PART_MIN_SIZE", 300)

    job = _snapshot(aws, checks=2)
    assert job["step"] > 5

    lines = _lines(aws, job["result"]["key"])
    assert sum(1 for line in lines if line.get("table") == "articles") == 7
    assert [line["t"] for line in lines].count("header") == 1
    assert lines[-1]["t"] == "end"


def test_scheduled_snapshots_are_pruned_to_the_newest_few_and_manual_ones_kept(seeded):
    prefix = backup.snapshots_prefix()
    for n in range(backup.KEEP_SCHEDULED + 2):
        key = f"{prefix}2026010{n}T000000Z-schedule.jsonl.gz"
        seeded.put_object(Bucket=BACKUP_BUCKET, Key=key, Body=b"x")
    seeded.put_object(Bucket=BACKUP_BUCKET, Key=f"{prefix}20250101T000000Z-manual.jsonl.gz", Body=b"x")

    job = _run(backup.start_snapshot("schedule"))

    keys = [s["key"] for s in backup.list_snapshots()]
    assert len([k for k in keys if k.endswith("-schedule.jsonl.gz")]) == backup.KEEP_SCHEDULED
    assert any(k.endswith("20250101T000000Z-manual.jsonl.gz") for k in keys)
    assert len(job["result"]["pruned"]) == 3  # 10 seeded and the new one, less the 8 kept
    assert job["result"]["key"] in keys


# --- restore -----------------------------------------------------------------------------------


def _empty_everything(s3):
    client = boto3.client("dynamodb", region_name=REGION)
    for table, _, _ in SCHEMAS.values():
        client.delete_table(TableName=table)
    _create_tables()
    for obj in s3.list_objects_v2(Bucket=CONTENT_BUCKET).get("Contents", []):
        s3.delete_object(Bucket=CONTENT_BUCKET, Key=obj["Key"])


def test_a_restore_loads_the_snapshot_into_an_empty_deployment(seeded):
    key = _snapshot(seeded)["result"]["key"]
    names = ("topics", "musings", "view_counts", "stats_history")
    before = {name: _table(name).scan()["Items"] for name in names}
    _empty_everything(seeded)

    hooks = _hooks()
    job = _run(backup.start_restore(key), hooks=hooks)

    assert job["status"] == "succeeded", job
    for name, items in before.items():
        assert _table(name).scan()["Items"] == items
    a2 = _table("articles").get_item(Key={"article_id": "a2"})["Item"]
    assert a2["lineage"]["total_cost_aud"] == Decimal("0.0123")
    assert _table("musings").get_item(Key={"musing_id": "mu-1"})["Item"]["tags"] == {"a", "b"}
    body = seeded.get_object(Bucket=CONTENT_BUCKET, Key="articles/a1.md")["Body"].read()
    assert body == b"# Body 1\n"
    assert seeded.get_object(Bucket=CONTENT_BUCKET, Key="vision/crypto/site/scene.png")["Body"].read() == PNG
    assert _table("findings").scan()["Items"] == []
    # Re-attached to this deployment through the Admin API's own functions.
    hooks.apply_schedules.assert_called_once()
    assert hooks.render_page.call_count == 3
    hooks.refresh_stats.assert_called_once()


def test_a_restore_can_be_run_again_and_changes_nothing(seeded):
    key = _snapshot(seeded)["result"]["key"]
    _run(backup.start_restore(key), hooks=_hooks())
    once = _table("articles").scan()["Items"]
    _run(backup.start_restore(key), hooks=_hooks())
    assert sorted(_table("articles").scan()["Items"], key=str) == sorted(once, key=str)


def test_a_restore_over_many_steps_resumes_where_it_stopped(seeded, monkeypatch):
    key = _snapshot(seeded)["result"]["key"]
    _empty_everything(seeded)
    monkeypatch.setattr(backup, "_RENDER_PAGE", 1)
    hooks = _hooks()
    job = _run(backup.start_restore(key, pages=True), checks=3, hooks=hooks)
    assert job["status"] == "succeeded"
    assert len(_table("articles").scan()["Items"]) == 3
    assert hooks.render_page.call_count == 3


def test_a_restore_can_skip_pages_and_schedules(seeded):
    key = _snapshot(seeded)["result"]["key"]
    hooks = _hooks()
    _run(backup.start_restore(key, pages=False, schedules=False), hooks=hooks)
    hooks.apply_schedules.assert_not_called()
    hooks.render_page.assert_not_called()


def test_a_cut_off_snapshot_is_refused_before_anything_is_written(seeded):
    key = _snapshot(seeded)["result"]["key"]
    whole = gzip.decompress(seeded.get_object(Bucket=BACKUP_BUCKET, Key=key)["Body"].read())
    cut = f"{backup.snapshots_prefix()}cut.jsonl.gz"
    half = whole[: len(whole) // 2].rsplit(b"\n", 1)[0]
    seeded.put_object(Bucket=BACKUP_BUCKET, Key=cut, Body=gzip.compress(half))
    _empty_everything(seeded)

    job = _run(backup.start_restore(cut), hooks=_hooks())

    assert job["status"] == "failed" and "cut off" in job["error"]
    assert _table("articles").scan()["Items"] == []


def test_a_restore_must_name_a_snapshot_in_an_allowed_bucket(seeded):
    with pytest.raises(backup.BackupError):
        backup.start_restore("backup/x/notes.txt")
    with pytest.raises(backup.BackupError, match="not the backup bucket"):
        backup.start_restore("x.jsonl.gz", bucket="someone-elses-bucket")
    with pytest.raises(backup.BackupError, match="no snapshot"):
        backup.start_restore("backup/missing.jsonl.gz")


# --- export ------------------------------------------------------------------------------------


def test_an_export_copies_every_article_and_its_text_to_another_bucket(seeded):
    job = _run(backup.start_export(bucket=EXPORT_BUCKET, prefix="dev-teardown"))

    assert job["status"] == "succeeded"
    keys = {obj["Key"] for obj in seeded.list_objects_v2(Bucket=EXPORT_BUCKET)["Contents"]}
    assert keys == {
        "dev-teardown/manifest.json",
        "dev-teardown/articles/a1.json", "dev-teardown/articles/a2.json", "dev-teardown/articles/a3.json",
        "dev-teardown/articles/a1.md", "dev-teardown/articles/a2.md", "dev-teardown/articles/a3.md",
        "dev-teardown/vision/crypto/site/scene.png",
    }  # fmt: skip
    exported = seeded.get_object(Bucket=EXPORT_BUCKET, Key="dev-teardown/articles/a1.json")
    row = json.loads(exported["Body"].read())
    assert row["title"] == "Title a1" and "body_original_s3_key" not in row


def test_an_export_defaults_to_the_backup_bucket_and_refuses_other_buckets(seeded):
    job = backup.start_export()
    assert job["params"]["bucket"] == BACKUP_BUCKET
    assert job["params"]["prefix"].startswith("backup/bloggerbear-dev/exports/")
    with pytest.raises(backup.BackupError):
        backup.start_export(bucket="not-allowed")
    with pytest.raises(backup.BackupError):
        backup.start_export(bucket=BACKUP_BUCKET, prefix="dev/terraform.tfstate")


# --- the Admin API's side ----------------------------------------------------------------------


def _request(method, resource, **extra):
    return {"httpMethod": method, "resource": resource, **extra}


def _context():
    return MagicMock(get_remaining_time_in_millis=_forever, aws_request_id="req-1")


def test_a_route_saves_the_job_and_starts_its_first_step(seeded):
    with patch.object(backup, "invoke_step") as invoke:
        response = admin_api_handler.handler(_request("POST", "/backups/snapshot"), _context())

    assert response["statusCode"] == 202
    job = json.loads(response["body"])["job"]
    assert job["status"] == "running" and job["step"] == 0
    invoke.assert_called_once()
    assert backup.load_job(job["job_id"])["kind"] == "snapshot"


def test_a_restore_route_refuses_a_bad_key_with_a_400(seeded):
    response = admin_api_handler.handler(
        _request("POST", "/backups/restore", body=json.dumps({"key": "nope"})), _context()
    )
    assert response["statusCode"] == 400


def test_each_step_invokes_the_next_until_the_job_is_done(seeded):
    job = backup.start_snapshot("manual")
    with patch.object(backup, "invoke_step") as invoke:
        result = admin_api_handler.handler({"backup_job": {"job_id": job["job_id"], "step": 0}}, _context())
    # The seeded deployment is small enough for one step.
    assert result["status"] == "succeeded"
    invoke.assert_not_called()


def test_a_step_that_is_not_the_current_one_does_nothing(seeded):
    job = backup.start_snapshot("manual")
    with patch.object(backup, "run_step") as run:
        result = admin_api_handler.handler({"backup_job": {"job_id": job["job_id"], "step": 3}}, _context())
    assert result == {"status": "skipped"}
    run.assert_not_called()


def test_the_weekly_schedule_starts_and_runs_a_scheduled_snapshot(seeded):
    event = {"backup_job": {"start": "snapshot", "trigger": "schedule"}}
    with patch.object(backup, "invoke_step") as invoke:
        started = admin_api_handler.handler(event, _context())
    assert started["status"] == "started"
    invoke.assert_called_once()
    step = {"backup_job": {"job_id": started["job_id"], "step": 0}}
    assert admin_api_handler.handler(step, _context())["status"] == "succeeded"
    (snapshot,) = backup.list_snapshots()
    assert snapshot["key"].endswith("-schedule.jsonl.gz")


def test_resume_restarts_only_a_stalled_job(seeded):
    job = backup.start_snapshot("manual")
    path = _request("POST", "/backups/jobs/{job_id}/resume", pathParameters={"job_id": job["job_id"]})
    with patch.object(backup, "invoke_step") as invoke:
        assert admin_api_handler.handler(path, _context())["statusCode"] == 409
        job["updated_at"] = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
        key = backup._job_key(job["job_id"])
        backup._s3().put_object(Bucket=BACKUP_BUCKET, Key=key, Body=json.dumps(job))
        assert admin_api_handler.handler(path, _context())["statusCode"] == 202
    invoke.assert_called_once()


def test_a_job_status_hides_its_cursor(seeded):
    job = backup.start_snapshot("manual")
    response = admin_api_handler.handler(
        _request("GET", "/backups/jobs/{job_id}", pathParameters={"job_id": job["job_id"]}),
        _context(),
    )
    shown = json.loads(response["body"])["job"]
    assert "cursor" not in shown and shown["stalled"] is False


def test_an_unknown_or_malformed_job_id_is_a_404(seeded):
    for job_id in ("20261019T000000Z-snapshot-abcdef", "../../dev/terraform.tfstate"):
        response = admin_api_handler.handler(
            _request("GET", "/backups/jobs/{job_id}", pathParameters={"job_id": job_id}), _context()
        )
        assert response["statusCode"] == 404
