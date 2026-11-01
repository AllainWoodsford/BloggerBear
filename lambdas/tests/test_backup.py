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
from table_schemas import KEYS, create_table

import admin_api_handler
from common import backup
from common.dispatch import Dispatcher

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
    "evals": ("Evals", *KEYS["Evals"]),
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
    # A model's day total and an operator's adjustment (common/evals.py, section 4.4's rows).
    _table("evals").put_item(
        Item={"entity": "model#m1", "sk": "day#2026-10-01", "perf": Decimal(4), "events": Decimal(3),
              "good": Decimal(2), "bad": Decimal(0), "by_reason": {"Article Completion": Decimal(4)}}
    )  # fmt: skip
    _table("evals").put_item(
        Item={"entity": "adjust#model#m1", "sk": "ts#2026-10-02T00:00:00Z", "delta": Decimal(-5),
              "by": "human"}
    )  # fmt: skip
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
                      "stats_history", "view_counts", "evals"}  # fmt: skip
    # The evals table's day totals and adjustments: no TTL, kept for good, so in every snapshot.
    assert lines[-1]["counts"]["evals"] == 2
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
    names = ("topics", "musings", "view_counts", "stats_history", "evals")
    before = {name: _table(name).scan()["Items"] for name in names}
    assert len(before["evals"]) == 2
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


# A topic on the dispatcher's queue (common/dispatch.py). Sydney on 2026-10-25 (AEDT, UTC+11): with
# the seed's topic beside it, slot 1 and the seed's 5-minute stagger, its daily turn is 09:15.
QUEUE_TOPIC = {
    "topic_id": "rail",
    "name": "Rail",
    "dispatch_mode": "queue",
    "research_cadence": "rate(1 hour)",
    "daily_cadence": "cron(0 9 * * ? *)",
    "daily_timezone": "Australia/Sydney",
    "stagger_slot": Decimal(1),
    "last_daily_dispatched_on": "2026-10-23",
    "daily_claim_n": Decimal(3),
}
BEFORE_TURN = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)  # 08:00
DAILY_TURN = datetime(2026, 10, 24, 22, 15, tzinfo=UTC)  # 09:15
AFTER_TURN = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # 10:00


def _restore_at(monkeypatch, moment: datetime, key: str) -> dict:
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(backup, "datetime", _Clock)
    job = _run(backup.start_restore(key), hooks=_hooks())
    assert job["status"] == "succeeded", job
    return _table("topics").get_item(Key={"topic_id": "rail"})["Item"]


def _daily_runs_started(*moments: datetime) -> list[str]:
    sfn = MagicMock()
    run = Dispatcher(
        sqs=MagicMock(), sfn=sfn, queue_url="https://queue", state_machine_arn="arn:aws:states:x"
    )
    for moment in moments:
        run.tick(moment)
    return [call.kwargs["name"] for call in sfn.start_execution.call_args_list]


def test_a_topic_on_the_queue_restored_after_its_daily_turn_starts_no_second_run_that_day(
    seeded, monkeypatch
):
    """The snapshot is from before today's run, which has started since (the live row says today).
    Written back as it was, the older day would have the next tick start today's run again."""
    _table("topics").put_item(Item=QUEUE_TOPIC)
    key = _snapshot(seeded)["result"]["key"]
    _table("topics").put_item(
        Item={**QUEUE_TOPIC, "last_daily_dispatched_on": "2026-10-25", "daily_claim_n": 4}
    )

    row = _restore_at(monkeypatch, AFTER_TURN, key)

    assert row == {**QUEUE_TOPIC, "last_daily_dispatched_on": "2026-10-25"}
    assert _daily_runs_started(AFTER_TURN + timedelta(minutes=1), AFTER_TURN + timedelta(hours=3)) == []
    assert _daily_runs_started(DAILY_TURN + timedelta(days=1)) == ["rail-2026-10-26-4"]


def test_a_topic_on_the_queue_restored_before_its_daily_turn_runs_today(seeded, monkeypatch):
    _table("topics").put_item(Item=QUEUE_TOPIC)
    key = _snapshot(seeded)["result"]["key"]

    row = _restore_at(monkeypatch, BEFORE_TURN, key)

    assert row == QUEUE_TOPIC
    assert _daily_runs_started(DAILY_TURN) == ["rail-2026-10-25-4"]


def test_a_topic_on_the_queue_restored_before_its_restored_turn_after_today_s_run_starts_no_second_run(
    seeded, monkeypatch
):
    """The snapshot's cadence is 10:00 (its turn 10:15); since then the topic moved to 08:00, and
    today's run started at 08:15 (the live row says today). Restored at 09:00, before the restored
    turn, the snapshot's older day would start a second run at 10:15, under a new name: the
    snapshot's claim count is five behind the live row's."""
    _table("topics").put_item(
        Item={**QUEUE_TOPIC, "daily_cadence": "cron(0 10 * * ? *)", "last_daily_dispatched_on": "2026-10-20",
              "daily_claim_n": Decimal(1)}
    )  # fmt: skip
    key = _snapshot(seeded)["result"]["key"]
    _table("topics").put_item(
        Item={**QUEUE_TOPIC, "daily_cadence": "cron(0 8 * * ? *)", "last_daily_dispatched_on": "2026-10-25",
              "daily_claim_n": Decimal(6)}
    )  # fmt: skip

    row = _restore_at(monkeypatch, BEFORE_TURN + timedelta(hours=1), key)

    assert (row["daily_cadence"], row["last_daily_dispatched_on"], row["daily_claim_n"]) == (
        "cron(0 10 * * ? *)",
        "2026-10-25",
        1,
    )
    restored_turn = datetime(2026, 10, 24, 23, 15, tzinfo=UTC)  # 10:15
    ticks = (BEFORE_TURN + timedelta(hours=1, minutes=1), restored_turn, restored_turn + timedelta(hours=3))
    assert _daily_runs_started(*ticks) == []
    assert _daily_runs_started(restored_turn + timedelta(days=1)) == ["rail-2026-10-26-2"]


def test_a_topic_deleted_since_the_snapshot_moves_the_restored_turn_later_and_starts_no_second_run(
    seeded, monkeypatch
):
    """The snapshot held three topics, so rail's turn was 09:20; one was deleted since, which moved
    it to 09:15, and today's run started there. Restored at 09:17, between the two turns, with the
    deleted topic back, the snapshot's older day would start a second run at 09:20."""
    ferry = {**_on_schedules(), "topic_id": "ferry", "name": "Ferry", "stagger_slot": Decimal(2)}
    claimed = {"last_daily_dispatched_on": "2026-10-20", "daily_claim_n": Decimal(1)}
    _table("topics").put_item(Item={**QUEUE_TOPIC, **claimed})
    _table("topics").put_item(Item=ferry)
    key = _snapshot(seeded)["result"]["key"]
    _table("topics").delete_item(Key={"topic_id": "ferry"})
    claimed_today = {"last_daily_dispatched_on": "2026-10-25", "daily_claim_n": Decimal(6)}
    _table("topics").put_item(Item={**QUEUE_TOPIC, **claimed_today})

    row = _restore_at(monkeypatch, DAILY_TURN + timedelta(minutes=2), key)

    assert (row["last_daily_dispatched_on"], row["daily_claim_n"]) == ("2026-10-25", 1)
    restored_turn = DAILY_TURN + timedelta(minutes=5)  # 09:20, with the deleted topic back
    ticks = (DAILY_TURN + timedelta(minutes=3), restored_turn, restored_turn + timedelta(hours=3))
    assert _daily_runs_started(*ticks) == []
    assert _daily_runs_started(restored_turn + timedelta(days=1)) == ["rail-2026-10-26-2"]


def test_a_live_claim_made_in_another_zone_is_not_taken_for_a_day_of_the_restored_one(seeded, monkeypatch):
    """The snapshot reads its daily turn in UTC (09:15); since then the topic moved to Sydney at
    00:00, and its 00:15 run there was claimed as 2026-10-25, still the 24th in UTC. Restored at
    14:00 UTC, past the restored turn, the row says the 24th: the Sydney date would have kept the
    restored topic from running on the 25th in UTC."""
    in_utc = {**QUEUE_TOPIC, "daily_timezone": "UTC", "last_daily_dispatched_on": "2026-10-20",
              "daily_claim_n": Decimal(1)}  # fmt: skip
    _table("topics").put_item(Item=in_utc)
    key = _snapshot(seeded)["result"]["key"]
    _table("topics").put_item(
        Item={**QUEUE_TOPIC, "daily_cadence": "cron(0 0 * * ? *)", "last_daily_dispatched_on": "2026-10-25",
              "daily_claim_n": 6}
    )  # fmt: skip

    row = _restore_at(monkeypatch, datetime(2026, 10, 24, 14, 0, tzinfo=UTC), key)

    assert row["last_daily_dispatched_on"] == "2026-10-24"
    assert _daily_runs_started(datetime(2026, 10, 25, 9, 15, tzinfo=UTC)) == ["rail-2026-10-25-2"]


@pytest.mark.parametrize(
    "unreadable",
    [{"daily_timezone": "Mars/Olympus"}, {"daily_cadence": "cron(0 9 * 1 ? *)"}],
    ids=["zone_unknown", "cadence_refused"],
)
def test_a_topic_on_the_queue_whose_cadences_cannot_be_read_is_restored_as_it_was_and_the_restore_succeeds(
    seeded, monkeypatch, capsys, unreadable
):
    """The dispatcher skips such a topic, so there is no turn to judge: one row like it must not
    fail the whole restore. The live row is renamed since, so the row read back is the snapshot's."""
    topic = {**QUEUE_TOPIC, **unreadable}
    _table("topics").put_item(Item=topic)
    key = _snapshot(seeded)["result"]["key"]
    _table("topics").put_item(Item={**topic, "name": "Renamed since"})

    assert _restore_at(monkeypatch, AFTER_TURN, key) == topic
    assert "backup: rail is on the queue, its cadences unreadable" in capsys.readouterr().out


def test_a_restore_that_cannot_read_the_pipeline_config_judges_the_turn_with_the_stagger_s_defaults(
    seeded, monkeypatch, capsys
):
    _table("topics").put_item(Item=QUEUE_TOPIC)
    key = _snapshot(seeded)["result"]["key"]

    def unreadable():
        raise RuntimeError("throttled")

    monkeypatch.setattr(backup.dynamo, "get_pipeline_config", unreadable)

    # The default stagger is the seed's 5 minutes: the turn is 09:15, so at 10:00 it has passed.
    assert _restore_at(monkeypatch, AFTER_TURN, key)["last_daily_dispatched_on"] == "2026-10-25"
    assert "backup: could not read the pipeline config" in capsys.readouterr().out


def _on_schedules() -> dict:
    return {key: value for key, value in QUEUE_TOPIC.items() if key != "dispatch_mode"}


def test_a_topic_on_its_schedules_restored_beside_one_on_the_queue_is_left_exactly_as_it_was(
    seeded, monkeypatch
):
    """Only a topic on the queue is given today's claim. Three topics: rail's turn is 09:20 and
    ferry's 09:25, both passed at 10:00, so a claim given to every row would reach ferry too."""
    ferry = {**_on_schedules(), "topic_id": "ferry", "name": "Ferry", "stagger_slot": Decimal(2)}
    _table("topics").put_item(Item=QUEUE_TOPIC)
    _table("topics").put_item(Item=ferry)
    key = _snapshot(seeded)["result"]["key"]

    rail = _restore_at(monkeypatch, AFTER_TURN, key)

    assert rail == {**QUEUE_TOPIC, "last_daily_dispatched_on": "2026-10-25"}
    assert _table("topics").get_item(Key={"topic_id": "ferry"})["Item"] == ferry


def test_a_snapshot_with_no_topic_on_the_queue_is_restored_exactly_as_it_was(seeded, monkeypatch):
    _table("topics").put_item(Item=_on_schedules())
    key = _snapshot(seeded)["result"]["key"]

    assert _restore_at(monkeypatch, AFTER_TURN, key) == _on_schedules()


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
