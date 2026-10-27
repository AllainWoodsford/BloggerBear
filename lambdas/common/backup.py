"""Backups: one snapshot file of a deployment's content and configuration, restoring it into a
deployment, and copying its articles to another bucket.

Terraform builds the skeleton (tables, buckets, functions); a snapshot is the data that is loaded
into it. What it keeps, and only this (TABLES below):

- **articles**: every Articles row (its metadata -- lineage, cost, sources, review, figures -- is
  on the row) and the final body it points at (`body_s3_key`), plus the figure PNGs a page shows.
  Not the research snapshots, the `.original.md` drafts or the before-rewrite copies: the row keeps
  its rewrite history (when, which model, what it cost, the issues, the previous title), and only
  the keys pointing at those texts are taken off it (clean_article_row), so a restored row never
  points at an object that is not there;
- **costing**: StatsCurrent, StatsHistory and ViewCounts;
- **gear and refinements**: PromptRefinements;
- **configuration**: Topics, Models and ModelConfig (the default model, pipeline and feedback
  settings), the musings the site shows, reader feedback and the moderation queue;
- **evals**: the Evals table's day totals, adjustments, offline runs and judge pairs (common/evals.py;
  the module's own policy, not the root's backup grant, lets the admin API scan and restore it).

Findings, candidate ideas (the research angles), failed executions, security events and sign-ins
are left out, and so is any row already set to expire (`expires_at`: TTL counters, nonces, rejected
items) and the feedback-signing key (`secret`; a new deployment makes its own).

**The file.** `backup/<prefix>-<env>/snapshots/<UTC time>-<trigger>.jsonl.gz` in the backup bucket
(the bootstrap state bucket, which outlives every environment), one gzip of JSON lines: a header,
one line per row (`{"t": "item", "table", "item"}`, the item in DynamoDB's own typed JSON so every
number and set comes back exactly) or object (`{"t": "object", "key", "content_type", "text" |
"data"}`), and an `end` line with the counts. A file without its end line is refused by restore.
Restoring writes every row and object back under its own key, so it can be run any number of times.

**The jobs.** A snapshot, a restore or an export can be thousands of articles, more than one
30-second Admin API invocation. Each runs as a job inside that same Lambda: a step does ~20 seconds
of work, saves where it got to (`backup/<prefix>-<env>/jobs/<job_id>.json`, which is also what
`backup status` shows), and invokes the function again, asynchronously, for the next step. No new
function, queue or table. A snapshot step writes its lines as one gzip member (`jobs/<id>/part-N`);
the last step joins them into the one file (gzip members joined end to end are one gzip stream).

A step that raises is retried by Lambda's own async retries; a step carries its number, and one
whose number is not the job's current step is a duplicate and does nothing. A job that stops moving
(`stalled` in its status) is restarted from where it got to by `backup resume`.
"""

from __future__ import annotations

import base64
import concurrent.futures
import gzip
import io
import json
import os
import re
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import boto3

from .dynamo import ARTICLES_BY_STATUS_INDEX
from .naming import NAME_PREFIX

FORMAT = "BloggerBear snapshot"
FORMAT_VERSION = 1

# Logical name (as written in the file) -> the environment variable that names this deployment's
# table. The order is the order they are written and restored in: settings before what uses them.
TABLES: dict[str, str] = {
    "topics": "TOPICS_TABLE",
    "models": "MODELS_TABLE",
    "model_config": "MODEL_CONFIG_TABLE",
    "prompt_refinements": "PROMPT_REFINEMENTS_TABLE",
    "articles": "ARTICLES_TABLE",
    "moderation_queue": "MODERATION_QUEUE_TABLE",
    "musings": "MUSINGS_TABLE",
    "feedback": "FEEDBACK_TABLE",
    "stats_current": "STATS_CURRENT_TABLE",
    "stats_history": "STATS_HISTORY_TABLE",
    "view_counts": "VIEW_COUNTS_TABLE",
    "evals": "EVALS_TABLE",
}

JOB_KINDS = ("snapshot", "restore", "export")
SNAPSHOT_SUFFIX = ".jsonl.gz"
# How many scheduled snapshots are kept; older ones are deleted after each scheduled run (the
# bucket is versioned, so even those can be recovered). Ones taken by hand are never deleted.
KEEP_SCHEDULED = 8
# A running job whose state has not changed for this long has stopped (a step is ~20 seconds).
STALLED_AFTER_SECONDS = 180

# A step stops starting new work when the invocation has less than this left, and when it holds
# this many bytes of lines (the function has 256 MB).
_RESERVE_MS = 10_000
_MAX_STEP_BYTES = 24 * 1024 * 1024
_SCAN_PAGE = 100
_RENDER_PAGE = 25
_THREADS = 16
# S3's smallest multipart part (except the last).
_MIN_PART = 5 * 1024 * 1024

_s3_client = None
_dynamodb_client = None
_lambda_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _dynamodb():
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def _lambda():
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    return _lambda_client


class BackupError(ValueError):
    """A request that cannot be done as asked (an unknown key, a bucket not allowed, a bad file)."""


# --- where things are --------------------------------------------------------------------------


def backup_bucket() -> str:
    bucket = os.environ.get("BACKUP_BUCKET")
    if not bucket:
        raise BackupError("no backup bucket is configured (BACKUP_BUCKET)")
    return bucket


def deployment_prefix() -> str:
    """`backup/<prefix>-<env>/`: dev and production share the state bucket, so each has its own."""
    return f"backup/{NAME_PREFIX}-{os.environ.get('ENVIRONMENT_NAME', 'dev')}/"


def snapshots_prefix() -> str:
    return deployment_prefix() + "snapshots/"


def _job_key(job_id: str) -> str:
    return f"{deployment_prefix()}jobs/{job_id}.json"


def _part_key(job_id: str, number: int) -> str:
    return f"{deployment_prefix()}jobs/{job_id}/part-{number:06d}.gz"


def export_buckets() -> list[str]:
    """The buckets an export may write to and a restore may read from besides the backup bucket:
    BACKUP_EXPORT_BUCKETS, comma separated (Terraform's var.backup_export_buckets, which is also
    what the Lambda role is granted)."""
    return [b.strip() for b in os.environ.get("BACKUP_EXPORT_BUCKETS", "").split(",") if b.strip()]


def _check_bucket(bucket: str) -> str:
    if bucket != backup_bucket() and bucket not in export_buckets():
        raise BackupError(
            f"bucket '{bucket}' is not the backup bucket or one of backup_export_buckets "
            "(the Lambda role may only use those; add it to the Terraform variable)"
        )
    return bucket


def _now() -> str:
    return datetime.now(UTC).isoformat()


# --- DynamoDB typed JSON <-> a JSON line ---------------------------------------------------------


def _to_json_value(value):
    """A low-level DynamoDB attribute value, with its binary parts base64'd so it fits in JSON."""
    (kind, inner), = value.items()
    if kind == "B":
        return {"B": base64.b64encode(inner).decode("ascii")}
    if kind == "BS":
        return {"BS": [base64.b64encode(b).decode("ascii") for b in inner]}
    if kind == "M":
        return {"M": {k: _to_json_value(v) for k, v in inner.items()}}
    if kind == "L":
        return {"L": [_to_json_value(v) for v in inner]}
    return value


def _from_json_value(value):
    (kind, inner), = value.items()
    if kind == "B":
        return {"B": base64.b64decode(inner)}
    if kind == "BS":
        return {"BS": [base64.b64decode(b) for b in inner]}
    if kind == "M":
        return {"M": {k: _from_json_value(v) for k, v in inner.items()}}
    if kind == "L":
        return {"L": [_from_json_value(v) for v in inner]}
    return value


def keep_item(table: str, item: dict) -> bool:
    """Whether a (low-level) row belongs in a snapshot: not one already set to expire, and not a
    secret (ModelConfig's feedback-signing key; a new deployment makes its own)."""
    if "expires_at" in item:
        return False
    if table == "model_config" and "secret" in item:
        return False
    return True


def _plain(value):
    """A low-level attribute value as plain Python (only what article_objects needs to read)."""
    (kind, inner), = value.items()
    if kind == "M":
        return {k: _plain(v) for k, v in inner.items()}
    if kind == "L":
        return [_plain(v) for v in inner]
    if kind == "NULL":
        return None
    return inner


def article_objects(item: dict) -> list[str]:
    """The content-bucket keys an Articles row needs: its final body and its figures' PNGs."""
    keys = []
    body = item.get("body_s3_key")
    if body and "S" in body:
        keys.append(body["S"])
    for figure in _plain(item["figures"]) if "figures" in item else []:
        if isinstance(figure, dict) and isinstance(figure.get("key"), str):
            keys.append(figure["key"])
    return list(dict.fromkeys(keys))


# What an Articles row points at that a snapshot does not carry (see the module docstring).
_DROPPED_ARTICLE_KEYS = ("body_original_s3_key",)
_DROPPED_REWRITE_KEYS = ("previous_body_s3_key",)


def clean_article_row(item: dict) -> dict:
    """A (low-level) Articles row without its pointers to the texts a snapshot leaves out: the
    pre-review original and each rewrite's replaced body. Everything else is kept as it is."""
    item = {k: v for k, v in item.items() if k not in _DROPPED_ARTICLE_KEYS}
    rewrites = item.get("rewrites")
    if rewrites and "L" in rewrites:
        item["rewrites"] = {
            "L": [
                {"M": {k: v for k, v in entry["M"].items() if k not in _DROPPED_REWRITE_KEYS}}
                if "M" in entry
                else entry
                for entry in rewrites["L"]
            ]
        }
    return item


def _object_line(key: str, body: bytes, content_type: str | None) -> dict:
    line = {"t": "object", "key": key, "content_type": content_type or "application/octet-stream"}
    try:
        line["text"] = body.decode("utf-8")
    except UnicodeDecodeError:
        line["data"] = base64.b64encode(body).decode("ascii")
    return line


def _line_bytes(line: dict) -> bytes:
    return (json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


# --- job state ---------------------------------------------------------------------------------


# What new_job names a job: the only ids looked up, so a path parameter can never name another key.
_JOB_ID = re.compile(r"^\d{8}T\d{6}Z-(snapshot|restore|export)-[0-9a-f]{6}$")


def load_job(job_id: str) -> dict | None:
    """A job's saved state, or None for an id that is not one or a job that does not exist."""
    if not _JOB_ID.fullmatch(job_id or ""):
        return None
    try:
        response = _s3().get_object(Bucket=backup_bucket(), Key=_job_key(job_id))
    except _s3().exceptions.ClientError as exc:
        # Without ListBucket on the exact key S3 answers a missing one 403, not 404.
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "AccessDenied"):
            return None
        raise
    return json.loads(response["Body"].read())


def save_job(job: dict) -> None:
    job["updated_at"] = _now()
    _s3().put_object(
        Bucket=backup_bucket(),
        Key=_job_key(job["job_id"]),
        Body=json.dumps(job).encode("utf-8"),
        ContentType="application/json",
    )


def job_view(job: dict) -> dict:
    """A job as `backup status` shows it: everything but its cursor, plus whether it has stalled."""
    view = {k: v for k, v in job.items() if k not in ("cursor", "parts")}
    if job.get("status") == "running":
        updated = datetime.fromisoformat(job["updated_at"])
        view["stalled"] = (datetime.now(UTC) - updated).total_seconds() > STALLED_AFTER_SECONDS
    return view


def list_jobs(limit: int = 10) -> list[dict]:
    """The most recent jobs, newest first."""
    prefix = f"{deployment_prefix()}jobs/"
    found = []
    for page in _s3().get_paginator("list_objects_v2").paginate(Bucket=backup_bucket(), Prefix=prefix):
        found.extend(
            obj for obj in page.get("Contents", [])
            if obj["Key"].endswith(".json") and "/" not in obj["Key"][len(prefix):]
        )  # fmt: skip
    found.sort(key=lambda obj: obj["LastModified"], reverse=True)
    jobs = []
    for obj in found[:limit]:
        job = load_job(obj["Key"][len(prefix) : -len(".json")])
        if job:
            jobs.append(job_view(job))
    return jobs


def list_snapshots() -> list[dict]:
    """Every snapshot file of this deployment, newest first."""
    snapshots = []
    paginator = _s3().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=backup_bucket(), Prefix=snapshots_prefix()):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(SNAPSHOT_SUFFIX):
                snapshots.append(
                    {
                        "key": obj["Key"],
                        "size_bytes": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
    snapshots.sort(key=lambda s: s["key"], reverse=True)
    return snapshots


def new_job(kind: str, *, trigger: str, params: dict | None = None) -> dict:
    if kind not in JOB_KINDS:
        raise BackupError(f"unknown job kind '{kind}'")
    now = datetime.now(UTC)
    job = {
        "job_id": f"{now.strftime('%Y%m%dT%H%M%SZ')}-{kind}-{uuid.uuid4().hex[:6]}",
        "kind": kind,
        "trigger": trigger,
        "status": "running",
        "step": 0,
        "phase": "start",
        "created_at": now.isoformat(),
        "params": params or {},
        "progress": {},
        "cursor": {},
        "parts": 0,
    }
    if kind == "snapshot":
        job["result"] = {
            "bucket": backup_bucket(),
            "key": f"{snapshots_prefix()}{job['job_id'][:16]}-{trigger}{SNAPSHOT_SUFFIX}",
        }
    return job


def invoke_step(job: dict) -> None:
    """Run the job's current step in a new, asynchronous invocation of this same function."""
    _lambda().invoke(
        FunctionName=os.environ["AWS_LAMBDA_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"backup_job": {"job_id": job["job_id"], "step": job["step"]}}).encode(),
    )


# --- running a step ----------------------------------------------------------------------------


class Hooks:
    """What a restore does once the data is back, taken from the Admin API so nothing here
    re-implements it: write a topic's schedules (`apply_schedules(topics)`, the same function
    `topics create` uses), render one published article's page (`render_page(article)`), and
    rebuild the Stats page's articles section (`refresh_stats()`)."""

    def __init__(
        self,
        *,
        apply_schedules: Callable[[list[dict]], dict],
        render_page: Callable[[dict], None],
        refresh_stats: Callable[[], None],
        load_topics: Callable[[], list[dict]],
        query_published: Callable[[dict | None, int], tuple[list[dict], dict | None]],
    ):
        self.apply_schedules = apply_schedules
        self.render_page = render_page
        self.refresh_stats = refresh_stats
        self.load_topics = load_topics
        self.query_published = query_published


def run_step(job: dict, *, time_left_ms: Callable[[], int], hooks: Hooks | None = None) -> dict:
    """Do one step of `job` (it is changed in place), save it, and return it. The caller invokes
    the next step while `status` is "running"."""
    try:
        if job["kind"] == "snapshot":
            _snapshot_step(job, time_left_ms)
        elif job["kind"] == "export":
            _export_step(job, time_left_ms)
        else:
            _restore_step(job, time_left_ms, hooks)
    except BackupError as exc:
        # Not something a retry fixes (a bad file, a missing key): the job stops here.
        job["status"] = "failed"
        job["error"] = str(exc)
    if job["status"] == "running":
        job["step"] += 1
    else:
        job["finished_at"] = _now()
    save_job(job)
    return job


def _has_time(time_left_ms, held_bytes: int = 0) -> bool:
    return time_left_ms() > _RESERVE_MS and held_bytes < _MAX_STEP_BYTES


def _fetch_objects(bucket: str, keys: list[str]) -> list[tuple[str, bytes | None, str | None]]:
    """Read several objects at once; a missing one comes back as None (counted, never fatal)."""

    def fetch(key):
        try:
            response = _s3().get_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001 - one missing body must not stop a backup
            print(f"backup: could not read s3://{bucket}/{key}: {exc!r}")
            return key, None, None
        return key, response["Body"].read(), response.get("ContentType")

    with concurrent.futures.ThreadPoolExecutor(max_workers=_THREADS) as pool:
        return list(pool.map(fetch, keys))


def _bump(job: dict, name: str, by: int = 1) -> None:
    job["progress"][name] = job["progress"].get(name, 0) + by


# --- snapshot ----------------------------------------------------------------------------------


def _table_estimates() -> dict:
    """Roughly how many rows each table holds (DescribeTable's count, refreshed every ~6 hours),
    so `backup status` can say how far along a snapshot is."""
    estimates = {}
    for name, env in TABLES.items():
        table = os.environ.get(env)
        if not table:
            continue
        try:
            estimates[name] = _dynamodb().describe_table(TableName=table)["Table"].get("ItemCount", 0)
        except Exception as exc:  # noqa: BLE001 - an estimate is only for the progress line
            print(f"backup: could not describe {table}: {exc!r}")
    return estimates


def _snapshot_step(job: dict, time_left_ms) -> None:
    lines: list[bytes] = []
    held = 0
    cursor = job["cursor"]

    if job["phase"] == "start":
        job["estimates"] = _table_estimates()
        lines.append(
            _line_bytes(
                {
                    "t": "header",
                    "format": FORMAT,
                    "format_version": FORMAT_VERSION,
                    "created_at": job["created_at"],
                    "trigger": job["trigger"],
                    "source": {
                        "name_prefix": NAME_PREFIX,
                        "environment": os.environ.get("ENVIRONMENT_NAME"),
                        "region": os.environ.get("AWS_REGION"),
                    },
                    "tables": list(TABLES),
                }
            )
        )
        job["phase"] = "tables"
        cursor.update({"table": 0, "start_key": None})

    content_bucket = os.environ.get("CONTENT_BUCKET")
    names = list(TABLES)
    while job["phase"] == "tables" and _has_time(time_left_ms, held):
        if cursor["table"] >= len(names):
            job["phase"] = "assemble"
            break
        name = names[cursor["table"]]
        table = os.environ.get(TABLES[name])
        if not table:
            cursor.update({"table": cursor["table"] + 1, "start_key": None})
            continue
        kwargs = {"TableName": table, "Limit": _SCAN_PAGE}
        if cursor["start_key"]:
            kwargs["ExclusiveStartKey"] = _from_json_value({"M": cursor["start_key"]})["M"]
        page = _dynamodb().scan(**kwargs)
        objects: list[str] = []
        for item in page.get("Items", []):
            if not keep_item(name, item):
                _bump(job, f"{name}_skipped")
                continue
            if name == "articles":
                objects.extend(article_objects(item))
                item = clean_article_row(item)
            line = _line_bytes({"t": "item", "table": name, "item": _to_json_value({"M": item})["M"]})
            lines.append(line)
            held += len(line)
            _bump(job, name)
        if objects and content_bucket:
            for key, body, content_type in _fetch_objects(content_bucket, list(dict.fromkeys(objects))):
                if body is None:
                    _bump(job, "objects_missing")
                    continue
                line = _line_bytes(_object_line(key, body, content_type))
                lines.append(line)
                held += len(line)
                _bump(job, "objects")
        last = page.get("LastEvaluatedKey")
        if last:
            cursor["start_key"] = _to_json_value({"M": last})["M"]
        else:
            cursor.update({"table": cursor["table"] + 1, "start_key": None})

    if lines:
        _s3().put_object(
            Bucket=backup_bucket(),
            Key=_part_key(job["job_id"], job["parts"]),
            Body=gzip.compress(b"".join(lines)),
        )
        job["parts"] += 1

    if job["phase"] == "assemble" and _has_time(time_left_ms):
        _assemble(job)
        job["status"] = "succeeded"
        job["phase"] = "done"
        if job["trigger"] == "schedule":
            job["result"]["pruned"] = prune_scheduled()


def _assemble(job: dict) -> None:
    """Join the job's parts, and an end line, into the one snapshot file, then delete the parts.

    A multipart upload: a part of 5 MB or more is copied server side; smaller ones are read and
    joined until they are big enough (S3 refuses a part under 5 MB unless it is the last)."""
    s3 = _s3()
    bucket = backup_bucket()
    counts = {k: v for k, v in job["progress"].items()}
    end = gzip.compress(_line_bytes({"t": "end", "counts": counts}))
    target = job["result"]["key"]
    upload = s3.create_multipart_upload(
        Bucket=bucket,
        Key=target,
        ContentType="application/gzip",
        Metadata={"backup-job": job["job_id"]},
    )
    upload_id = upload["UploadId"]
    done: list[dict] = []
    buffer = bytearray()

    def flush():
        number = len(done) + 1
        etag = s3.upload_part(
            Bucket=bucket, Key=target, UploadId=upload_id, PartNumber=number, Body=bytes(buffer)
        )["ETag"]
        done.append({"PartNumber": number, "ETag": etag})
        buffer.clear()

    try:
        for number in range(job["parts"]):
            key = _part_key(job["job_id"], number)
            size = s3.head_object(Bucket=bucket, Key=key)["ContentLength"]
            if not buffer and size >= _MIN_PART:
                part_number = len(done) + 1
                etag = s3.upload_part_copy(
                    Bucket=bucket,
                    Key=target,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    CopySource={"Bucket": bucket, "Key": key},
                )["CopyPartResult"]["ETag"]
                done.append({"PartNumber": part_number, "ETag": etag})
                continue
            buffer.extend(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
            if len(buffer) >= _MIN_PART:
                flush()
        buffer.extend(end)
        flush()
        s3.complete_multipart_upload(
            Bucket=bucket, Key=target, UploadId=upload_id, MultipartUpload={"Parts": done}
        )
    except Exception:
        s3.abort_multipart_upload(Bucket=bucket, Key=target, UploadId=upload_id)
        raise
    size = s3.head_object(Bucket=bucket, Key=target)["ContentLength"]
    job["result"]["size_bytes"] = size
    for number in range(job["parts"]):
        s3.delete_object(Bucket=bucket, Key=_part_key(job["job_id"], number))


def prune_scheduled(keep: int = KEEP_SCHEDULED) -> list[str]:
    """Delete all but the newest `keep` scheduled snapshots; return the keys deleted."""
    scheduled = [s["key"] for s in list_snapshots() if s["key"].endswith(f"-schedule{SNAPSHOT_SUFFIX}")]
    deleted = scheduled[keep:]
    for key in deleted:
        _s3().delete_object(Bucket=backup_bucket(), Key=key)
    return deleted


# --- restore -----------------------------------------------------------------------------------


def _snapshot_lines(bucket: str, key: str):
    """The snapshot's lines, parsed, streamed from S3 (never the whole file in memory)."""
    try:
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"]
    except _s3().exceptions.NoSuchKey as exc:
        raise BackupError(f"no snapshot at s3://{bucket}/{key}") from exc
    try:
        with gzip.GzipFile(fileobj=body) as unzipped:
            for raw in io.TextIOWrapper(unzipped, encoding="utf-8"):
                if raw.strip():
                    yield json.loads(raw)
    except (OSError, EOFError, json.JSONDecodeError) as exc:
        raise BackupError(f"s3://{bucket}/{key} is not a readable snapshot ({exc})") from exc


def check_snapshot(bucket: str, key: str) -> dict:
    """Read the whole file once: its header must be a snapshot this code reads, and its end line
    must be there (a cut-off file is refused before anything is written). Returns the header with
    the number of lines and the end line's counts."""
    header, end, count = None, None, 0
    for count, line in enumerate(_snapshot_lines(bucket, key), start=1):
        if count == 1:
            header = line
            if line.get("t") != "header" or line.get("format") != FORMAT:
                raise BackupError(f"s3://{bucket}/{key} is not a BloggerBear snapshot")
            if line.get("format_version", 0) > FORMAT_VERSION:
                raise BackupError(
                    f"the snapshot is format {line['format_version']}; this code reads up to {FORMAT_VERSION}"
                )
        elif line.get("t") == "end":
            end = line
    if header is None or end is None:
        raise BackupError(f"s3://{bucket}/{key} has no end line: it was cut off, refusing to restore it")
    return {**header, "lines": count, "counts": end.get("counts", {})}


def _write_rows(table: str, items: list[dict]) -> None:
    """BatchWriteItem in 25s, retrying what DynamoDB hands back unprocessed."""
    for start in range(0, len(items), 25):
        requests = {table: [{"PutRequest": {"Item": item}} for item in items[start : start + 25]]}
        for _ in range(8):
            unprocessed = _dynamodb().batch_write_item(RequestItems=requests).get("UnprocessedItems")
            if not unprocessed:
                break
            requests = unprocessed
        else:
            raise RuntimeError(f"DynamoDB kept {len(requests.get(table, []))} rows of {table} unprocessed")


def _put_object(line: dict) -> None:
    body = line["text"].encode("utf-8") if "text" in line else base64.b64decode(line["data"])
    _s3().put_object(
        Bucket=os.environ["CONTENT_BUCKET"], Key=line["key"], Body=body, ContentType=line["content_type"]
    )


def _restore_step(job: dict, time_left_ms, hooks: Hooks | None) -> None:
    params = job["params"]
    cursor = job["cursor"]

    if job["phase"] == "start":
        checked = check_snapshot(params["bucket"], params["key"])
        job["snapshot"] = {k: checked.get(k) for k in ("created_at", "trigger", "source", "lines", "counts")}
        # What `backup status` measures the restore against: the file's own counts.
        job["estimates"] = dict(checked.get("counts") or {})
        job["phase"] = "load"
        cursor["line"] = 1  # the header
        if not _has_time(time_left_ms):
            return

    if job["phase"] == "load":
        _restore_load(job, time_left_ms)
        if job["phase"] == "load":
            return

    if hooks is None:
        raise RuntimeError("a restore needs the Admin API's hooks")
    if not _has_time(time_left_ms):
        return

    if job["phase"] == "schedules":
        if params.get("schedules", True):
            topics = hooks.load_topics()
            outcome = hooks.apply_schedules(topics)
            job["progress"]["schedules_written"] = outcome.get("written", 0)
            if outcome.get("failed"):
                job["progress"]["schedules_failed"] = outcome["failed"]
        job["phase"] = "pages"
        cursor["page_key"] = None

    while job["phase"] == "pages" and _has_time(time_left_ms):
        if not params.get("pages", True):
            job["phase"] = "stats"
            break
        articles, next_key = hooks.query_published(cursor.get("page_key"), _RENDER_PAGE)
        for article in articles:
            try:
                hooks.render_page(article)
                _bump(job, "pages_rendered")
            except Exception as exc:  # noqa: BLE001 - one page must not stop the rest
                print(f"backup: could not render {article.get('article_id')}: {exc!r}")
                _bump(job, "pages_failed")
        cursor["page_key"] = next_key
        if not next_key:
            job["phase"] = "stats"

    if job["phase"] == "stats" and _has_time(time_left_ms):
        hooks.refresh_stats()
        job["phase"] = "done"
        job["status"] = "succeeded"


def _restore_load(job: dict, time_left_ms) -> None:
    """Write rows and objects from where the last step stopped. The file is a gzip stream, which
    cannot be entered in the middle, so each step reads from the top and skips what is done."""
    params = job["params"]
    cursor = job["cursor"]
    pending: dict[str, list[dict]] = {}
    pending_count = 0
    objects: list[dict] = []

    def flush():
        nonlocal pending_count
        for name, items in pending.items():
            _write_rows(os.environ[TABLES[name]], items)
        pending.clear()
        pending_count = 0
        if objects:
            with concurrent.futures.ThreadPoolExecutor(max_workers=_THREADS) as pool:
                list(pool.map(_put_object, objects))
            objects.clear()

    number = 0
    for number, line in enumerate(_snapshot_lines(params["bucket"], params["key"])):
        if number < cursor["line"]:
            continue
        kind = line.get("t")
        if kind == "item":
            env = TABLES.get(line["table"])
            if env is None or not os.environ.get(env):
                _bump(job, f"{line['table']}_not_here")
            else:
                pending.setdefault(line["table"], []).append(_from_json_value({"M": line["item"]})["M"])
                pending_count += 1
                _bump(job, line["table"])
        elif kind == "object":
            objects.append(line)
            _bump(job, "objects")
        elif kind == "end":
            flush()
            cursor["line"] = number + 1
            job["phase"] = "schedules"
            return
        if pending_count >= 100 or len(objects) >= 50:
            flush()
            cursor["line"] = number + 1
            if not _has_time(time_left_ms):
                return
    flush()
    cursor["line"] = number + 1


# --- export ------------------------------------------------------------------------------------


def _export_step(job: dict, time_left_ms) -> None:
    """Copy every article to `params.bucket` under `params.prefix`: its row as plain JSON
    (`articles/<id>.json`) and its body and figures under their own keys, copied server side."""
    params = job["params"]
    cursor = job["cursor"]
    target, prefix = params["bucket"], params["prefix"]
    content_bucket = os.environ["CONTENT_BUCKET"]
    if job["phase"] == "start":
        job["estimates"] = {"articles": _table_estimates().get("articles", 0)}
        job["phase"] = "articles"
        cursor["start_key"] = None

    def copy(key):
        try:
            _s3().copy_object(
                Bucket=target, Key=prefix + key, CopySource={"Bucket": content_bucket, "Key": key}
            )
            return True
        except Exception as exc:  # noqa: BLE001 - one missing body must not stop an export
            print(f"backup: could not copy {key}: {exc!r}")
            return False

    while job["phase"] == "articles" and _has_time(time_left_ms):
        kwargs = {"TableName": os.environ["ARTICLES_TABLE"], "Limit": _SCAN_PAGE}
        if cursor["start_key"]:
            kwargs["ExclusiveStartKey"] = _from_json_value({"M": cursor["start_key"]})["M"]
        page = _dynamodb().scan(**kwargs)
        keys = []
        for item in page.get("Items", []):
            row = _plain({"M": clean_article_row(item)})
            _s3().put_object(
                Bucket=target,
                Key=f"{prefix}articles/{row['article_id']}.json",
                Body=json.dumps(row, default=str, ensure_ascii=False).encode("utf-8"),
                ContentType="application/json",
            )
            _bump(job, "articles")
            keys.extend(article_objects(item))
        with concurrent.futures.ThreadPoolExecutor(max_workers=_THREADS) as pool:
            for ok in pool.map(copy, keys):
                _bump(job, "objects" if ok else "objects_missing")
        last = page.get("LastEvaluatedKey")
        cursor["start_key"] = _to_json_value({"M": last})["M"] if last else None
        if not last:
            job["phase"] = "manifest"

    if job["phase"] == "manifest":
        _s3().put_object(
            Bucket=target,
            Key=f"{prefix}manifest.json",
            Body=json.dumps(
                {"job_id": job["job_id"], "exported_at": _now(), "counts": job["progress"]}
            ).encode("utf-8"),
            ContentType="application/json",
        )
        job["result"] = {"bucket": target, "prefix": prefix}
        job["phase"] = "done"
        job["status"] = "succeeded"


# --- starting jobs (the Admin API's routes call these) ---------------------------------------


def start_snapshot(trigger: str = "manual") -> dict:
    job = new_job("snapshot", trigger=trigger)
    save_job(job)
    return job


def start_restore(key: str, *, bucket: str | None = None, pages: bool = True, schedules: bool = True) -> dict:
    if not key or not key.endswith(SNAPSHOT_SUFFIX):
        raise BackupError(f"'key' must be a snapshot file ({SNAPSHOT_SUFFIX})")
    bucket = _check_bucket(bucket or backup_bucket())
    try:
        _s3().head_object(Bucket=bucket, Key=key)
    except Exception as exc:  # noqa: BLE001 - 404 and 403 both mean "not one we can read"
        raise BackupError(f"no snapshot at s3://{bucket}/{key}") from exc
    job = new_job("restore", trigger="manual", params={
        "bucket": bucket, "key": key, "pages": pages, "schedules": schedules,
    })  # fmt: skip
    save_job(job)
    return job


def start_export(*, bucket: str | None = None, prefix: str | None = None) -> dict:
    bucket = _check_bucket(bucket or backup_bucket())
    if prefix is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        prefix = f"{deployment_prefix()}exports/{stamp}/" if bucket == backup_bucket() else f"{stamp}/"
    elif prefix and not prefix.endswith("/"):
        prefix += "/"
    if bucket == backup_bucket() and not prefix.startswith(deployment_prefix()):
        raise BackupError(f"an export into the backup bucket must go under {deployment_prefix()}")
    job = new_job("export", trigger="manual", params={"bucket": bucket, "prefix": prefix})
    save_job(job)
    return job


def published_page(start_key: dict | None, limit: int) -> tuple[list[dict], dict | None]:
    """One page of published articles (the Articles status index), for restore's pages phase."""
    from .dynamo import get_table  # the shared resource, so its numbers come back as Decimals

    table = get_table(os.environ["ARTICLES_TABLE"])
    kwargs = {
        "IndexName": ARTICLES_BY_STATUS_INDEX,
        "KeyConditionExpression": "#s = :published",
        "ExpressionAttributeNames": {"#s": "status"},
        "ExpressionAttributeValues": {":published": "published"},
        "Limit": limit,
    }
    if start_key:
        kwargs["ExclusiveStartKey"] = start_key
    response = table.query(**kwargs)
    return response.get("Items", []), _json_safe_key(response.get("LastEvaluatedKey"))


def _json_safe_key(key: dict | None) -> dict | None:
    """An index's LastEvaluatedKey holds only strings here (ids and ISO times), so it round-trips
    through the job's JSON as it is."""
    return dict(key) if key else None
