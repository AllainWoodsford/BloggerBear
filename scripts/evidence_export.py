#!/usr/bin/env python3
"""The evidence export: what the deployed system measured, decided and did, read from AWS into git.

    python scripts/evidence_export.py export --env dev [--topics a,b] [--since ISO] [--networks]
        [--figures N] [--with-notes] [--logs-wait SECONDS] [--out DIR] [--replace]
    python scripts/evidence_export.py trace --topic rail-cities [--site sydney] --scene ID|latest
        (--from docs/hackathon/evidence/live/<date> | --env dev) [--out FILE]
    python scripts/evidence_export.py agreement --from docs/hackathon/evidence/live/<date>

Judges cannot open the AWS account, so what the deployed pipeline did has to be in the repository:
the evidence lines of the research tick, the vision worker and the admin API (common/evidence.py),
the decision ring each vision topic keeps in its running state (common/vision_decisions.py), the
Articles and Findings tables, and where every one of those came from (`provenance.json`). `export`
writes `docs/hackathon/evidence/live/<UTC date>/` (docs/hackathon/evidence/README.md lists the
files); `trace` prints one decision's chain as Markdown, through vision_decisions.trace, from an
export or live; `agreement` recounts the operator's agreement with the agent once `labels.csv` is
filled in (O9).

**Read-only.** S3 GetObject, DynamoDB Scan of the Topics table and Query of the Articles and
Findings tables, Logs Insights StartQuery, GetQueryResults and StopQuery, Lambda
GetFunctionConfiguration, and `gh run list` (the deploy that produced the code). Nothing here writes
to AWS. Resource names are built from `--prefix` and `--env` as common/naming.py builds them; the
home Region and the vision Region are Terraform's defaults unless given.

**Only schema fields.** Each log line goes through evidence.parse and the operator's assistant's
own checks (ops_mcp/vision_evidence.py: its query text, its parser, its kept rule and its join are
imported, not copied, so the two readers cannot drift), and is written as the fields that survive,
never `@log` (it names the account), `@ptr` or a raw `@message`. A decision ring entry is re-checked
field by field with the same kinds. The agent's reason, the hosts of the news it read and the
Findings' summaries are third-party or model text: they are written only with `--with-notes`,
through redact.scrub and labelled as unverified. Before finishing, every 12-digit number and every
`arn:` string left anywhere is replaced with `[redacted]` (a backstop: no schema field holds one),
and scripts/pii_denylist_check.py --files reads every file written; a hit refuses the export (exit
2, naming the file and line) and moves the directory aside, where git ignores it.

**Logs are read within bounds.** The queries are the assistant's fixed text (topic ids compared by
equality), run through ops_mcp/logs.run_queries a few at a time, each batch waited for at most
`--logs-wait` seconds (an operator is at the terminal, so longer than the assistant's 12 s), every
request with a short connect and a read timeout. A query cut at Logs Insights' 1,000 rows is split
in two halves of its window and asked again, down to MIN_SLICE; a query that did not finish in time
is said as not checked in `summary.json`, never as zero.

Runs on Windows as on Linux (pathlib paths; every file written as UTF-8 with `\\n` line ends).
Needs lambdas/requirements.txt (boto3) and, for the provenance's deploy commit, a signed-in `gh`
(without one the commit is null, with the reason; the export still finishes).
"""

from __future__ import annotations

import argparse
import contextlib
import csv
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import vision_runs

ROOT = Path(__file__).resolve().parents[1]
LAMBDAS = ROOT / "lambdas"
if str(LAMBDAS) not in sys.path:
    sys.path.insert(0, str(LAMBDAS))

from common import evidence, osm, vision_contract, vision_decisions, vision_sites  # noqa: E402
from common.costing import USD_TO_AUD_RATE, call_cost_usd  # noqa: E402
from common.dynamo import list_articles_for_topic, list_topics  # noqa: E402
from common.model_pricing import default_model_entry  # noqa: E402
from common.naming import NAME_PREFIX  # noqa: E402
from common.vision_triage import STOPS, VERDICTS  # noqa: E402
from ops_mcp import logs, redact, vision_evidence  # noqa: E402
from ops_mcp.suggestions import ID_PATTERN  # noqa: E402

SCHEMA = "bloggerbear.evidence-export/1"
EVIDENCE_DIR = ROOT / "docs" / "hackathon" / "evidence"
LIVE_DIR = EVIDENCE_DIR / "live"
NETWORKS_DIR = ROOT / "scripts" / "vision_benchmark_networks"
TRACKER = ROOT / "docs" / "hackathon" / "rubric-tracker.md"
# The tracker's paragraph that names the demo topics (scripts/tests/test_hackathon_docs.py parses it
# the same way): every count reports them apart from the cities.
DEMO_TOPICS_PREFIX = "Demo topics:"
_BACKTICKED = re.compile(r"`([^`]+)`")
# Terraform's defaults (infra/environments/dev/variables.tf: aws_region, vision_region).
HOME_REGION = "ap-southeast-2"
VISION_REGION = "us-west-2"
# The logs keep 90 days (dev main.tf and vision-worker/main.tf); the assistant reads as far back.
LOOKBACK_DAYS = vision_evidence.EVIDENCE_LOOKBACK_DAYS
# How long one batch of queries is waited for. The assistant waits 8 s inside a 12 s call; an
# operator at the terminal can wait longer, and a 90-day scan of a busy group takes tens of seconds.
LOGS_WAIT_SECONDS = 120.0
# Queries started at once: Logs Insights runs at most 30 at a time per account (the assistant and
# the console share them), so a batch takes a few.
LOGS_BATCH = 6
# A query cut at logs.QUERY_LIMIT rows is split in halves of its window, down to this; a slice this
# short that is still cut is kept and said as cut.
MIN_SLICE = timedelta(minutes=15)
# At most this many queries in one export, splits included: a bound on its cost and its time.
MAX_QUERIES = 400
# Every request but the Logs ones (those are run_queries', below): a short connect, a read timeout,
# three attempts. Logs: one retry in botocore, throttles asked again by run_queries.
CONNECT_SECONDS, READ_SECONDS = 5, 30
LOGS_READ_SECONDS = 15
GH_TIMEOUT_SECONDS = 60
GH_RUNS = 100
# The functions whose code the provenance names, by their resource name's tail and their Region.
FUNCTIONS = {
    "research_tick": ("research-tick", "home"),
    "admin_api": ("admin-api", "home"),
    "vision_worker": ("vision-worker", "vision"),
}
# What a function's configuration gives the provenance: never its ARN or its role (both hold the
# account id).
FUNCTION_FIELDS = (
    "FunctionName",
    "CodeSha256",
    "LastModified",
    "Version",
    "Runtime",
    "Architectures",
    "MemorySize",
    "Timeout",
)
# Where the research tick keeps a topic's running state (research_tick_handler._running_state_key;
# a test holds the two equal).
RUNNING_STATE_KEY = "snapshots/{topic_id}/running-state.json"
# A figure's key as vision_sites.store_image writes it.
FIGURE_KEY = re.compile(r"^vision/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_-]{1,64}/[A-Za-z0-9_.:-]{1,128}\.png$")
# The network fixture's fields (scripts/vision_benchmark.py fetch-network, common/osm.fetch_network).
NETWORK_FIELDS = ("ways", "stations", "pois", "fetched_at", "source", "endpoint", "query_url", "counts")
NOTE_LABEL = "the agent's note (unverified, model text)"
HOSTS_LABEL = "the news hosts the agent read (unverified, third-party text)"
SUMMARY_LABEL = "the Finding's summary (unverified, model text)"
NOTE_MAX_CHARS = redact.TEXT_MAX_CHARS
SUMMARY_MAX_CHARS = 4000
REDACTED = "[redacted]"
# The backstop: an AWS account id is a 12-digit number; redact.py's rule (a run of exactly twelve
# digits, not inside a longer number or after a hyphen, so a UUID's last group is not one), and any
# ARN, which names the account.
_TWELVE_DIGITS = re.compile(r"(?<![\d-])\d{12}(?![\d-])")
_ARN = re.compile(r"\barn:[^\s\"',\]\}]*")
# A Lambda REPORT line's numbers and status, as Lambda writes them.
_REPORT_REQUEST = re.compile(r"RequestId: ([A-Za-z0-9-]{1,64})")
_REPORT_FIELDS = {
    "duration_ms": re.compile(r"(?<!Billed )Duration: ([0-9.]+) ms"),
    "billed_ms": re.compile(r"Billed Duration: ([0-9]+) ms"),
    "memory_mb": re.compile(r"Memory Size: ([0-9]+) MB"),
    "max_memory_mb": re.compile(r"Max Memory Used: ([0-9]+) MB"),
    "init_ms": re.compile(r"Init Duration: ([0-9.]+) ms"),
}
_REPORT_STATUS = re.compile(r"Status: ([a-z]+)")
_REPORT_ERROR = re.compile(r"Error Type: ([A-Za-z0-9_.]{1,60})")
# The worker's callers, from a site id (1.18.2): the evaluation, the benchmark, the demo topic, and
# production's cities, whose numbers are never mixed with the others'.
CALLERS = (("eval-", "eval"), ("bench-", "bench"), ("demo-", "demo"))
PRODUCTION = "production"
# What a site's turn came to (vision_site.outcome), every one counted, zeros included.
SITE_OUTCOMES = ("measured", "deferred", "not_reached", "none", "no_scene")
MODERATION_DECISIONS = vision_evidence.MODERATION_DECISIONS
LABEL_FILE, TEMPLATE_FILE = "labels.csv", "labels-template.csv"
FILES = (
    "decisions.jsonl",
    "triage.jsonl",
    "worker.jsonl",
    "ticks.jsonl",
    "people.jsonl",
    "articles.json",
    "findings.json",
    "history.json",
    TEMPLATE_FILE,
    "provenance.json",
    "summary.json",
)
REFUSED = 2
LIVE_README = """# Live evidence

Each directory here, `<UTC date>/`, is one run of `scripts/evidence_export.py export` against the
deployed dev system: what it measured, decided and did, read from its logs, its running states and
its tables, with `provenance.json` saying where every line came from and which code produced it.
Only code-derived fields are written (codes, ids, times, numbers); model and third-party text only
when the export was run with `--with-notes`, and then labelled as unverified.
docs/hackathon/evidence/README.md says what each file holds and how to run the export.

`labels.csv`, when present, is the operator's agreement with the agent on each triaged live
decision (`labels-template.csv`, filled in); `python scripts/vision_eval.py index --check` checks it.

## Credits

- Contains modified Copernicus Sentinel data (2024-2026), processed by ESA and distributed on AWS
  Open Data (`sentinel-cogs`): the scenes the decisions name and the figures under `figures/`.
- Rail network data © OpenStreetMap contributors, available under the Open Database Licence 1.0
  (https://opendatacommons.org/licenses/odbl/): the decisions name stations by their OpenStreetMap
  ids, and the figures draw the network.
"""


class Refused(Exception):
    """A usage the export refuses before reading anything (exit 2)."""


# --- names, topics, the window -------------------------------------------------------------------------


def resource_names(prefix: str, env: str) -> dict:
    """Every name the export reads, built as common/naming.py builds them: "<prefix>-<env>-<name>"."""
    base = f"{prefix}-{env}-"
    names = {
        "topics": base + "topics",
        "findings": base + "findings",
        "articles": base + "articles",
        "bucket": base + "content",
    }
    for key, (tail, _region) in FUNCTIONS.items():
        names[key] = base + tail
        names[key + "_group"] = f"/aws/lambda/{base}{tail}"
    return names


def demo_topics(text: str) -> list[str]:
    """The ids in the tracker's `Demo topics:` paragraph (the line that starts with the prefix and
    every line after it up to a blank one): its backticked tokens."""
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.startswith(DEMO_TOPICS_PREFIX)]
    if not starts:
        return []
    paragraph = []
    for line in lines[starts[0] :]:
        if not line.strip():
            break
        paragraph.append(line)
    return [token for token in _BACKTICKED.findall(" ".join(paragraph)) if ID_PATTERN.match(token)]


def group_of(topic_id: str, demos) -> str:
    return "demo" if topic_id in demos else "cities"


def parse_since(text: str | None, now: datetime) -> datetime:
    """`--since` as a UTC time (a date alone is its midnight), no earlier than the logs' retention
    and before now; LOOKBACK_DAYS back by default."""
    earliest = now - timedelta(days=LOOKBACK_DAYS)
    if not text:
        return earliest
    try:
        since = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused(f"--since {text!r} is not an ISO date or time") from exc
    since = since if since.tzinfo else since.replace(tzinfo=UTC)
    if since >= now:
        raise Refused(f"--since {text!r} is not in the past")
    return max(since.astimezone(UTC), earliest)


def _iso(when: datetime) -> str:
    return when.astimezone(UTC).isoformat(timespec="seconds")


def _time(value) -> datetime | None:
    """An ISO time (a trailing Z, or Lambda's "+0000", accepted) as an aware UTC datetime."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    text = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)


# --- AWS, read-only ------------------------------------------------------------------------------------


class Aws:
    """The clients the export reads with, each request held to a short connect and a read timeout.
    `logs` takes a Region (run_queries' client factory). The tests build one over moto and a fake
    Logs."""

    def __init__(self, home_region: str, vision_region: str):
        self.home_region, self.vision_region = home_region, vision_region
        self._clients: dict = {}

    def _config(self, read: float, attempts: int):
        from botocore.config import Config

        return Config(
            connect_timeout=CONNECT_SECONDS,
            read_timeout=read,
            retries={"total_max_attempts": attempts, "mode": "standard"},
        )

    def _client(self, service: str, region: str, read: float = READ_SECONDS, attempts: int = 3):
        import boto3

        key = (service, region)
        if key not in self._clients:
            self._clients[key] = boto3.client(
                service, region_name=region, config=self._config(read, attempts)
            )
        return self._clients[key]

    def region(self, which: str) -> str:
        return self.home_region if which == "home" else self.vision_region

    def logs(self, region: str):
        return self._client("logs", region, LOGS_READ_SECONDS, 2)

    def s3(self):
        return self._client("s3", self.home_region)

    def lambda_(self, region: str):
        return self._client("lambda", region)

    def table(self, name: str):
        import boto3

        return boto3.resource(
            "dynamodb", region_name=self.home_region, config=self._config(READ_SECONDS, 3)
        ).Table(name)


def _error_code(exc: Exception) -> str:
    """A failed request's code (AccessDenied, NoSuchKey, ...) or its type's name: never its message."""
    response = getattr(exc, "response", None)
    code = (response or {}).get("Error", {}).get("Code") if isinstance(response, dict) else None
    return (
        code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.]{1,80}", code) else type(exc).__name__
    )


def read_state(aws: Aws, bucket: str, topic_id: str) -> tuple[dict | None, dict]:
    """A topic's running state, and where it came from: {"key", "etag", "last_modified", "bytes",
    "read": "read" | "missing" | "error", "error"}."""
    key = RUNNING_STATE_KEY.format(topic_id=topic_id)
    meta = {"key": key, "etag": None, "last_modified": None, "version_id": None, "bytes": None}
    try:
        response = aws.s3().get_object(Bucket=bucket, Key=key)
        body = response["Body"].read()
    except Exception as exc:  # noqa: BLE001 - a missing or unreadable state is said, never raised
        code = _error_code(exc)
        return None, {**meta, "read": "missing" if code in ("NoSuchKey", "404") else "error", "error": code}
    meta.update(
        etag=str(response.get("ETag") or "").strip('"') or None,
        bytes=len(body),
        last_modified=_iso(response["LastModified"]) if response.get("LastModified") else None,
        version_id=response.get("VersionId"),
    )
    try:
        state = json.loads(body)
    except ValueError:
        return None, {**meta, "read": "error", "error": "not_json"}
    return (state if isinstance(state, dict) else None), {**meta, "read": "read", "error": None}


def read_topics(aws: Aws, table_name: str) -> list[dict]:
    return [item for item in list_topics(table=aws.table(table_name)) if isinstance(item, dict)]


# --- the logs: the assistant's queries, split while cut, each batch bounded ----------------------------

_ASSISTANT_FIELDS = "fields @timestamp, @message\n"
_EXPORT_FIELDS = "fields @timestamp, @logStream, @message\n"
_TOPIC_CLAUSE = re.compile(r' and topic_id (?:= "[^"]+"|in \[[^\]]+\])')
REPORT_QUERY = (
    'fields @timestamp, @logStream, @requestId, @message\n| filter @type = "REPORT"{extra}\n'
    f"| sort @timestamp desc | limit {logs.QUERY_LIMIT}"
)
# The research tick's REPORT lines are read only for the attempts that ended badly (a timeout, an
# error): one per invocation of every topic otherwise.
TICK_REPORT_EXTRA = " and @message like /Status: (timeout|error)/"


def _derive(text: str, old: str, new: str) -> str:
    """`text` with `old`, which must occur exactly once, replaced: a change to the assistant's query
    text fails here, loudly, rather than drifting."""
    if text.count(old) != 1:
        raise ValueError(
            f"the assistant's query no longer reads {old!r} once; update the export's derivation"
        )
    return text.replace(old, new)


def queries(topic_ids) -> dict[str, tuple[str, str]]:
    """{kind: (which group, query text)}. The evidence queries are the assistant's own
    (vision_evidence.queries) with `@logStream` added to the fields, for the provenance:
    `decisions` as it is, `ticks` its attempts query for every attempt (not only those that decided),
    `sites` the same for the per-site lines, `people` with the topic changes beside the moderation
    decisions, and `measures` the worker's lines, which carry no topic. The REPORT queries are
    Lambda's own lines."""
    base = vision_evidence.queries(topic_ids)
    ticks = _derive(base["attempts"], " and decisions > 0", "")
    sites = _derive(ticks, 'event = "research_tick"', 'event = "vision_site"')
    people = _derive(
        base["people"], 'event = "moderation_decision"', 'event in ["moderation_decision", "topic_changed"]'
    )
    measures = _derive(ticks, 'event = "research_tick"', 'event = "vision_measure"')
    if len(_TOPIC_CLAUSE.findall(measures)) != 1:
        raise ValueError("the assistant's query no longer names its topics in one clause")
    measures = _TOPIC_CLAUSE.sub("", measures)
    texts = {
        "decisions": base["decisions"],
        "ticks": ticks,
        "sites": sites,
        "people": people,
        "measures": measures,
    }
    out = {
        kind: ("tick" if kind != "people" else "admin", _derive(text, _ASSISTANT_FIELDS, _EXPORT_FIELDS))
        for kind, text in texts.items()
    }
    out["measures"] = ("worker", out["measures"][1])
    out["tick_reports"] = ("tick", REPORT_QUERY.format(extra=TICK_REPORT_EXTRA))
    out["worker_reports"] = ("worker", REPORT_QUERY.format(extra=""))
    return out


class QueryLog:
    """Every Logs Insights query the export started, by its id: the group, the window, when it was
    started and when Logs said it was complete, its status and its statistics (what it scanned).
    `client(region)` is the client factory run_queries asks with, recording as it goes."""

    def __init__(self, factory, clock=time.monotonic):
        self.factory, self.clock = factory, clock
        self.started: dict[str, dict] = {}
        self.by_job: dict[tuple, str] = {}

    def client(self, region: str):
        return _Recording(self, self.factory(region), region)


class _Recording:
    def __init__(self, log: QueryLog, client, region: str):
        self._log, self._client, self._region = log, client, region

    def start_query(self, **kwargs):
        response = self._client.start_query(**kwargs)
        query_id = response["queryId"]
        group = kwargs.get("logGroupName") or ",".join(kwargs.get("logGroupNames") or [])
        self._log.started[query_id] = {
            "region": self._region,
            "log_group": group,
            "status": "Running",
            "started": self._log.clock(),
            "run_ms": None,
            "statistics": None,
        }
        job = (self._region, group, kwargs.get("queryString"), kwargs.get("startTime"), kwargs.get("endTime"))
        self._log.by_job[job] = query_id
        return response

    def get_query_results(self, queryId):  # noqa: N803 - boto3's name
        response = self._client.get_query_results(queryId=queryId)
        record = self._log.started.get(queryId)
        status = response.get("status")
        if record is not None and record["run_ms"] is None and status not in ("Running", "Scheduled", None):
            record["status"] = status
            record["run_ms"] = int(round((self._log.clock() - record["started"]) * 1000))
            stats = response.get("statistics") or {}
            record["statistics"] = {
                key: stats[key]
                for key in ("recordsMatched", "recordsScanned", "bytesScanned")
                if isinstance(stats.get(key), int | float)
            }
        return response

    def stop_query(self, queryId):  # noqa: N803 - boto3's name
        record = self._log.started.get(queryId)
        if record is not None:
            record["status"] = "Stopped: not finished in time"
        return self._client.stop_query(queryId=queryId)


def read_logs(
    specs: dict[str, tuple[str, str, str]],
    since: datetime,
    until: datetime,
    *,
    client,
    wait: float,
    sleep=time.sleep,
    run=None,
    batch: int = LOGS_BATCH,
    min_slice: timedelta = MIN_SLICE,
    max_queries: int = MAX_QUERIES,
) -> tuple[dict, dict]:
    """Every query of `specs` ({kind: (region, group, text)}) over [since, until], a batch at a time
    through logs.run_queries (each batch waited for at most `wait` seconds): a slice cut at
    logs.QUERY_LIMIT rows is split in halves and asked again, down to `min_slice`, within
    `max_queries`. Returns ({kind: {"rows", "state", "missing"}}, {slice id: provenance}), where
    `state` is "complete", "cut" (a slice could not be split further: its newest rows were kept) or
    "not_checked" (a slice did not finish: its window is under `missing`, and nothing is said of
    it), and each row keeps only @timestamp, @logStream, @requestId and @message, under its slice."""
    run = run or logs.run_queries
    recorder = QueryLog(client)
    pending = [(kind, since, until) for kind in specs]
    slices: dict[str, dict] = {}
    out = {kind: {"rows": [], "state": "complete", "missing": [], "_seen": set()} for kind in specs}
    asked = 0
    while pending:
        chunk, pending = pending[:batch], pending[batch:]
        jobs = []
        for n, (kind, start, end) in enumerate(chunk):
            region, group, text = specs[kind]
            jobs.append(((kind, f"{asked + n}"), region, group, text, start, end))
        asked += len(chunk)
        results = run(
            jobs,
            client=recorder.client,
            wait_seconds=wait,
            sleep=sleep,
            label="evidence_export",
            recover_throttles=True,
        )
        for (kind, number), region, group, text, start, end in jobs:
            rows = results.get((kind, number))
            slice_id = f"{kind}-{number}"
            query_id = recorder.by_job.get(
                (region, group, text, int(start.timestamp()), int(end.timestamp()))
            )
            record = recorder.started.get(query_id, {}) if query_id else {}
            entry = {
                "kind": kind,
                "region": region,
                "log_group": group,
                "start": _iso(start),
                "end": _iso(end),
                "query_id": query_id,
                "run_ms": record.get("run_ms"),
                "status": record.get("status"),
                "statistics": record.get("statistics"),
                "rows": None,
                "state": None,
            }
            slices[slice_id] = entry
            if rows is None:
                entry["state"] = "not_checked"
                out[kind]["state"] = "not_checked"
                out[kind]["missing"].append({"start": _iso(start), "end": _iso(end)})
                continue
            entry["rows"] = len(rows)
            cut = len(rows) >= logs.QUERY_LIMIT
            if cut and end - start > min_slice and asked + len(pending) + 2 <= max_queries:
                middle = start + (end - start) / 2
                middle = middle.replace(microsecond=0)
                pending += [(kind, start, middle), (kind, middle, end)]
                entry["state"] = "split"
                continue
            entry["state"] = "cut" if cut else "complete"
            if cut and out[kind]["state"] == "complete":
                out[kind]["state"] = "cut"
            for row in rows:
                if not isinstance(row, dict):
                    continue
                kept = {key: row.get(key) for key in ("@timestamp", "@logStream", "@requestId", "@message")}
                identity = (group, kept["@logStream"], kept["@timestamp"], kept["@message"])
                if identity in out[kind]["_seen"]:
                    continue  # two halves meet on one second: the row is read once
                out[kind]["_seen"].add(identity)
                kept["_slice"] = slice_id
                kept["_group"] = group
                out[kind]["rows"].append(kept)
    for kind in out:
        out[kind].pop("_seen")
    return out, slices


# --- parse, then check ---------------------------------------------------------------------------------


def _from(row: dict) -> dict:
    """A line's provenance: the group the query named (never @log), its stream, its time, its query."""
    return {
        "log_group": row.get("_group"),
        "log_stream": row.get("@logStream"),
        "timestamp": row.get("@timestamp"),
        "query": row.get("_slice"),
    }


def _own(line: dict | None, event: str, asked: set[str] | None, ids: tuple[str, ...]) -> bool:
    """The checks the assistant's parser does not make, for the events it never reads: the event,
    the topic by equality (when the event carries one), the ids it is joined by."""
    if line is None or line.get("event") != event:
        return False
    if asked is not None and line.get("topic_id") not in asked:
        return False
    return all(isinstance(line.get(key), str) for key in ids)


def parse_lines(rows: list[dict], kind: str, asked: set[str], moved_ok: frozenset[str]) -> tuple[list, int]:
    """One kind's rows through evidence.parse and the checks: the assistant's own
    (vision_evidence.parse_rows, row by row so each line keeps its provenance) for the decisions,
    the attempts and the moderation decisions; the same kind of checks for the lines it never reads.
    (the lines, each with `logged` and `_from`; how many rows were dropped)."""
    kept, dropped = [], 0
    for row in rows:
        line = None
        if kind == "decisions":
            lines, _lost = vision_evidence.parse_rows([row], "decisions", asked, moved_ok)
            line = lines[0] if lines else None
        elif kind == "ticks":
            lines, _lost = vision_evidence.parse_rows([row], "attempts", asked, moved_ok)
            line = lines[0] if lines else None
        elif kind == "people":
            parsed = evidence.parse(row.get("@message"))
            if parsed is not None and parsed.get("event") == "topic_changed":
                line = parsed if _own(parsed, "topic_changed", asked, ()) else None
            else:
                lines, _lost = vision_evidence.parse_rows([row], "people", asked, moved_ok)
                line = lines[0] if lines else None
        elif kind == "sites":
            parsed = evidence.parse(row.get("@message"))
            ok = _own(parsed, "vision_site", asked, ("site_id", "run"))
            line = parsed if ok and parsed.get("outcome") in (*SITE_OUTCOMES, None) else None
        elif kind == "measures":
            parsed = evidence.parse(row.get("@message"))
            line = parsed if _own(parsed, "vision_measure", None, ("site_id",)) else None
        if line is None:
            dropped += 1
            continue
        line.setdefault("logged", vision_evidence._log_time(row.get("@timestamp")))
        line["_from"] = _from(row)
        kept.append(line)
    return kept, dropped


def _number(text) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def parse_report(row: dict) -> dict | None:
    """A Lambda REPORT line as fields: its request id, duration, billed duration, memory size, the
    most memory used, init time, and its status ("ok" when Lambda wrote none) and error type. None
    for anything else. Never the message itself."""
    message = row.get("@message")
    if not isinstance(message, str) or not message.startswith("REPORT"):
        return None
    request_id = row.get("@requestId") if isinstance(row.get("@requestId"), str) else None
    if request_id is None:
        found = _REPORT_REQUEST.search(message)
        request_id = found.group(1) if found else None
    request_id = evidence.check("id", request_id)
    if request_id is None:
        return None
    report = {"request_id": request_id}
    for key, pattern in _REPORT_FIELDS.items():
        found = pattern.search(message)
        value = _number(found.group(1)) if found else None
        if value is not None:
            report[key] = int(value) if key != "duration_ms" and key != "init_ms" else round(value, 2)
    status = _REPORT_STATUS.search(message)
    report["status"] = evidence.check("code", status.group(1)) if status else "ok"
    error = _REPORT_ERROR.search(message)
    if error:
        report["error_type"] = evidence.check("id", error.group(1))
    report["logged"] = vision_evidence._log_time(row.get("@timestamp"))
    report["_from"] = _from(row)
    return report


def no_reply_cause(report: dict) -> str:
    """Why a worker run wrote no line: Lambda's timeout, the memory limit, another error, or none
    that Lambda said (the line was lost, or not read)."""
    if report.get("status") == "timeout":
        return "timeout"
    memory, used = report.get("memory_mb"), report.get("max_memory_mb")
    if "OutOfMemory" in str(report.get("error_type") or "") or (memory and used and used >= memory):
        return "out_of_memory"
    if report.get("status") == "error":
        return "error"
    return "other"


def caller_of(site_id) -> str:
    text = str(site_id or "")
    return next((name for prefix, name in CALLERS if text.startswith(prefix)), PRODUCTION)


# --- the decisions: the ring and the logs, merged ------------------------------------------------------

ENTRY_KINDS = {
    "v": "int",
    "at": "iso",
    "tick": "id",
    "run": "id",
    "scene_id": "id",
    "captured_at": "iso",
    "coverage": "num",
    "measure": "numbers",
    "baseline": "numbers",
    "delta": "numbers",
    "relative": "numbers",
    "baseline_scenes": "int",
    "thresholds": "numbers",
    "code": "code",
    "moved": "codes",
    "network_changed": "bool",
    "reported": "bool",
}
TRIAGE_KINDS = {
    "verdict": "code",
    "stop": "code",
    "model_id": "model",
    "model_calls": "int",
    "input_tokens": "int",
    "output_tokens": "int",
    "cost_usd": "num",
    "elapsed_ms": "int",
    "deadline_left_s": "num",
    "figure": "bool",
    "steps": "steps",
    "steps_total": "int",
    "budget_refusals": "int",
    "tools": "codes",
}
MEASURED_BY_KINDS = {
    "backend": "code",
    "build": "hex12",
    "cpu": "cpu",
    "worker_request_id": "id",
    "worker_ms": "int",
}


def _checked(value: dict, kinds: dict) -> dict:
    """`value`'s fields through their kinds (evidence.check): a field that fails is null."""
    value = value if isinstance(value, dict) else {}
    return {
        key: evidence.check(kind, value.get(key)) if value.get(key) is not None else None
        for key, kind in kinds.items()
    }


def clean_entry(entry: dict, *, with_notes: bool) -> dict:
    """A ring entry (or a decision rebuilt from the lines) as the export writes it: every field of
    1.6.2 through its kind, the agent's part likewise, and the agent's reason and news hosts only
    with `with_notes`, scrubbed and labelled."""
    out = _checked(entry, ENTRY_KINDS)
    triage = entry.get("triage") if isinstance(entry.get("triage"), dict) else None
    out["triage"] = None
    if triage is not None:
        out["triage"] = {
            key: value
            for key, value in _checked(triage, TRIAGE_KINDS).items()
            if value is not None or key in ("verdict", "stop")
        }
        if with_notes and isinstance(triage.get("reason"), str) and triage["reason"]:
            out["note"] = {"label": NOTE_LABEL, "text": redact.scrub(triage["reason"], NOTE_MAX_CHARS)}
        hosts = [host for host in triage.get("hosts") or [] if isinstance(host, str)]
        if with_notes and hosts:
            out["hosts"] = {"label": HOSTS_LABEL, "hosts": [redact.scrub(host, 80) for host in hosts]}
    out["measured_by"] = _checked(entry.get("measured_by"), MEASURED_BY_KINDS)
    return out


def ring_records(topic_id: str, state: dict | None, *, with_notes: bool) -> list[dict]:
    """Every ring entry of a topic's running state, oldest first per site, as decision records."""
    records = []
    sites = (state or {}).get("sites") if isinstance((state or {}).get("sites"), dict) else {}
    for site_id in sorted(sites):
        if evidence.check("id", site_id) is None:
            continue
        ring = (sites[site_id] or {}).get("decisions") if isinstance(sites[site_id], dict) else None
        for entry in ring if isinstance(ring, list) else []:
            if not isinstance(entry, dict) or evidence.check("id", entry.get("scene_id")) is None:
                continue
            record = {
                "topic_id": topic_id,
                "site_id": site_id,
                "source": "state",
                "kept": True,
                **clean_entry(entry, with_notes=with_notes),
            }
            records.append(record)
    return records


def attempt_of(decision: dict, attempt_by: dict) -> tuple[dict | None, bool]:
    """The attempt a decision line joins, by (tick, run), and whether it kept its decisions: the
    assistant's kept rule (vision_evidence.join; a test holds the two to the same answer)."""
    attempt = attempt_by.get((decision.get("tick"), decision.get("run")))
    return attempt, bool(attempt and attempt.get("stored") is True)


def _inside(logged: str | None, windows: list[dict]) -> bool:
    when = _time(logged)
    return when is not None and any(_time(w["start"]) <= when <= _time(w["end"]) for w in windows)


def merge_decisions(
    records: list[dict],
    decisions: list[dict],
    triages: list[dict],
    attempts: list[dict],
    *,
    unread_ticks: list[dict],
    with_notes: bool,
) -> tuple[list[dict], dict]:
    """The decision records the export writes: every ring entry (the kept copy, `source: state`),
    then the decisions only the logs hold (`source: logs`), one per (topic, site, scene) by the
    assistant's join, the kept copy winning; each with its not-kept copies under `not_kept`. A
    decision line with no attempt line, logged inside a window of the attempts query that was not
    read (`unread_ticks`), is neither kept nor not kept: it is set apart as unknown. Returns the
    records and {"unknown": [...], "log_copy_of_ring": n}."""
    attempt_by: dict = {}
    for attempt in attempts:  # newest first, as the assistant takes them
        attempt_by.setdefault((attempt.get("tick"), attempt["run"]), attempt)
    certain, unknown = [], []
    for line in decisions:
        attempt, _kept = attempt_of(line, attempt_by)
        if attempt is None and _inside(line.get("logged"), unread_ticks):
            unknown.append(
                {
                    key: line.get(key)
                    for key in ("topic_id", "site_id", "scene_id", "tick", "run", "at", "logged")
                }
            )
            continue
        certain.append(line)
    entries, _not_kept, _unknown = vision_evidence.join(certain, triages, attempts)
    copies: dict[tuple, list] = {}
    for line in certain:
        attempt, kept = attempt_of(line, attempt_by)
        if kept:
            continue
        key = (line["topic_id"], line["site_id"], line["scene_id"])
        copies.setdefault(key, []).append(
            {
                "tick": line.get("tick"),
                "run": line.get("run"),
                "at": line.get("at"),
                "code": line.get("code"),
                "logged": line.get("logged"),
                "why": "stored_false" if attempt is not None else "no_attempt_line",
            }
        )
    by_key = {(r["topic_id"], r["site_id"], r["scene_id"]): r for r in records}
    kept_log = {(e["topic_id"], e["site_id"], e["scene_id"]): e for e in entries if e["kept"]}
    out, ring_copies = [], 0
    for record in records:
        key = (record["topic_id"], record["site_id"], record["scene_id"])
        log = kept_log.get(key)
        record["logged"] = None
        if log is not None and log["at"] == record["at"]:
            ring_copies += 1
            record["logged"] = log.get("logged")
            record["_log_from"] = _decision_from(decisions, log)
            record["_attempt"] = log.get("attempt")
        record["not_kept"] = copies.get(key, [])
        out.append(record)
    for entry in sorted(entries, key=lambda e: (e["topic_id"], e["site_id"], e["at"], e.get("logged") or "")):
        key = (entry["topic_id"], entry["site_id"], entry["scene_id"])
        if key in by_key:
            continue
        record = {
            "topic_id": entry["topic_id"],
            "site_id": entry["site_id"],
            "source": "logs",
            "kept": entry["kept"],
            **clean_entry(entry, with_notes=False),
        }
        record["logged"] = entry.get("logged")
        record["_log_from"] = _decision_from(decisions, entry)
        record["_attempt"] = entry.get("attempt")
        record["not_kept"] = [
            copy
            for copy in copies.get(key, [])
            if (copy["tick"], copy["run"]) != (entry.get("tick"), entry.get("run"))
        ]
        out.append(record)
    return out, {"unknown": unknown, "log_copy_of_ring": ring_copies}


def _decision_from(decisions: list[dict], entry: dict) -> dict | None:
    for line in decisions:
        if (line.get("tick"), line.get("run"), line.get("site_id"), line.get("scene_id")) == (
            entry.get("tick"),
            entry.get("run"),
            entry["site_id"],
            entry["scene_id"],
        ):
            return line.get("_from")
    return None


def finding_written(record: dict, attempt: dict | None, finding_times: set[str]) -> bool | None:
    """Whether the decision's tick wrote a Finding: from its research_tick line (`finding` is the
    decision's `at`); else from the Findings table and the articles' lineage (`finding_times`, the
    captured_at they hold); else unknown (None), never "no". A decision not kept wrote none."""
    if not record.get("kept"):
        return False
    if attempt is not None:
        return attempt.get("stored") is True and attempt.get("finding") == record.get("at")
    if record.get("at") in finding_times:
        return True
    return None


# --- the tables ----------------------------------------------------------------------------------------


def _plain(value):
    """DynamoDB's Decimals as ints or floats, through lists and maps."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def read_articles(aws: Aws, table_name: str, topic_id: str, since: str) -> tuple[list[dict], dict]:
    """A topic's articles created since `since` (dynamo.list_articles_for_topic: a Query on the topic
    index, never a Scan, never the body), as the export writes them."""
    try:
        items = list_articles_for_topic(topic_id, since, table=aws.table(table_name))
    except Exception as exc:  # noqa: BLE001 - said in the summary, never raised
        return [], {"read": "error", "error": _error_code(exc), "items": 0, "cut": False}
    out = []
    for item in items:
        lineage = item.get("lineage") if isinstance(item.get("lineage"), dict) else {}
        research = lineage.get("research") if isinstance(lineage.get("research"), dict) else {}
        calls = research.get("calls") if isinstance(research.get("calls"), list) else []
        captured = sorted(
            {
                c["captured_at"]
                for c in calls
                if isinstance(c, dict) and evidence.check("iso", c.get("captured_at"))
            }
        )
        out.append(
            {
                "article_id": evidence.check("id", item.get("article_id")),
                "topic_id": topic_id,
                "status": evidence.check("code", item.get("status")),
                "published_by": evidence.check("code", item.get("published_by")),
                "created_at": evidence.check("iso", item.get("created_at")),
                "published_at": evidence.check("iso", item.get("published_at")),
                "research_captured_at": captured,
            }
        )
    return out, {"read": "read", "error": None, "items": len(out), "cut": len(items) >= 200}


FINDING_FIELDS = ("captured_at", "expires_at", "figures", "planner_notes", "research_call", "subjects")


def read_findings(
    aws: Aws, table_name: str, topic_id: str, since: str, *, with_notes: bool
) -> tuple[list, dict]:
    """A topic's Findings captured since `since` (a Query on the table's own key, paginated), as the
    export writes them: never the summary without `with_notes`."""
    from boto3.dynamodb.conditions import Key

    fields = FINDING_FIELDS + (("summary",) if with_notes else ())
    names = {f"#f{n}": field for n, field in enumerate(fields)}
    kwargs = {
        "KeyConditionExpression": Key("topic_id").eq(topic_id) & Key("captured_at").gte(since),
        "ProjectionExpression": ", ".join(names),
        "ExpressionAttributeNames": names,
    }
    try:
        table = aws.table(table_name)
        response = table.query(**kwargs)
        items = list(response.get("Items", []))
        while "LastEvaluatedKey" in response:
            response = table.query(**kwargs, ExclusiveStartKey=response["LastEvaluatedKey"])
            items.extend(response.get("Items", []))
    except Exception as exc:  # noqa: BLE001 - said in the summary, never raised
        return [], {"read": "error", "error": _error_code(exc), "items": 0}
    out = []
    for item in (_plain(item) for item in items):
        figures = item.get("figures") if isinstance(item.get("figures"), list) else []
        notes = item.get("planner_notes") if isinstance(item.get("planner_notes"), list) else []
        call = item.get("research_call") if isinstance(item.get("research_call"), dict) else None
        record = {
            "topic_id": topic_id,
            "captured_at": evidence.check("iso", item.get("captured_at")),
            "expires_at": evidence.check("int", item.get("expires_at")),
            "figure_keys": [
                f["key"] for f in figures if isinstance(f, dict) and FIGURE_KEY.match(str(f.get("key")))
            ],
            "planner_notes": dict(
                sorted(
                    Counter(
                        n.get("type")
                        for n in notes
                        if isinstance(n, dict) and evidence.check("code", n.get("type"))
                    ).items()
                )
            ),
            "research_call": None
            if call is None
            else {
                "model_id": evidence.check("model", call.get("model_id")),
                "input_tokens": evidence.check("int", call.get("input_tokens")),
                "output_tokens": evidence.check("int", call.get("output_tokens")),
                "used_fallback": evidence.check("bool", call.get("used_fallback")),
            },
            "subjects": evidence.check("ids", item.get("subjects")) or [],
        }
        if with_notes and isinstance(item.get("summary"), str):
            record["summary"] = {
                "label": SUMMARY_LABEL,
                "text": redact.scrub(item["summary"], SUMMARY_MAX_CHARS),
            }
        out.append(record)
    return out, {"read": "read", "error": None, "items": len(out)}


# --- the running states' history -----------------------------------------------------------------------

HISTORY_STRINGS = {
    "scene_id": "id",
    "captured_at": "iso",
    "backend": "code",
    "network_fetched_at": "iso",
    "site_key": "id",
}
HISTORY_NUMBERS = ("timings_ms", "params")
RECORD_FIELDS = {
    "first_observed_at": "iso",
    "last_attempt_at": "iso",
    "network_fetched_at": "iso",
    "network_site_key": "id",
    "network_counts": "numbers",
}


def history_scalars(entry: dict) -> dict:
    """A history entry's scalars: numbers and switches under code-shaped keys, the few strings by
    their kinds, the build's prefix, the quality flags; never stations, flags, suggestions, deserts,
    hubs, assets or any other list or text."""
    out = {}
    for key, value in entry.items():
        if evidence.check("code", key) is None:
            continue
        if key in HISTORY_STRINGS:
            out[key] = evidence.check(HISTORY_STRINGS[key], value)
        elif key in HISTORY_NUMBERS:
            out[key] = evidence.check("numbers", value)
        elif key == "quality_flags":
            out[key] = evidence.check("codes", value) or []
        elif key == "build_sha256":
            out["build"] = vision_decisions.build_prefix(value)
        elif isinstance(value, bool) or (isinstance(value, int | float) and math.isfinite(value)):
            out[key] = value
    return out


def history_of(state: dict | None) -> dict:
    """The topic's state-level times and thresholds, and each site's record scalars and history
    scalars (history.json)."""
    state = state if isinstance(state, dict) else {}
    sites = state.get("sites") if isinstance(state.get("sites"), dict) else {}
    out = {
        "fetched_at": evidence.check("iso", state.get("fetched_at")),
        "first_observed_at": evidence.check("iso", state.get(vision_sites.FIRST_OBSERVED_KEY)),
        "thresholds": evidence.check("numbers", state.get("thresholds")) or {},
        "sites": {},
    }
    for site_id in sorted(sites):
        record = sites[site_id] if isinstance(sites[site_id], dict) else {}
        if evidence.check("id", site_id) is None:
            continue
        site = {key: evidence.check(kind, record.get(key)) for key, kind in RECORD_FIELDS.items()}
        for key in ("last_error", "network_error"):
            error = record.get(key) if isinstance(record.get(key), dict) else None
            site[key] = (
                None
                if error is None
                else {
                    "code": evidence.check("code", error.get("code")),
                    "at": evidence.check("iso", error.get("at")),
                }
            )
        failed = record.get("failed_scene") if isinstance(record.get("failed_scene"), dict) else None
        site["failed_scene"] = (
            None
            if failed is None
            else {
                "scene_id": evidence.check("id", failed.get("scene_id")),
                "code": evidence.check("code", failed.get("code")),
            }
        )
        history = record.get("history") if isinstance(record.get("history"), list) else []
        site["history"] = [history_scalars(entry) for entry in history if isinstance(entry, dict)]
        site["ring_entries"] = (
            len(record.get("decisions") or []) if isinstance(record.get("decisions"), list) else 0
        )
        out["sites"][site_id] = site
    return out


def figure_key_of(state: dict | None, site_id: str, scene_id: str) -> str | None:
    record = (((state or {}).get("sites") or {}).get(site_id)) or {}
    for entry in reversed(record.get("history") or []):
        if isinstance(entry, dict) and entry.get("scene_id") == scene_id:
            key = entry.get("image_key")
            return key if isinstance(key, str) and FIGURE_KEY.match(key) else None
    return None


# --- networks (--networks) -----------------------------------------------------------------------------


@contextlib.contextmanager
def _content_bucket(bucket: str):
    """vision_sites.load_network reads the bucket from CONTENT_BUCKET, as the tick does."""
    before = os.environ.get("CONTENT_BUCKET")
    os.environ["CONTENT_BUCKET"] = bucket
    try:
        yield
    finally:
        if before is None:
            os.environ.pop("CONTENT_BUCKET", None)
        else:
            os.environ["CONTENT_BUCKET"] = before


def fixture_of(network: dict) -> dict:
    """A stored network in the fixture's shape (the fields vision_benchmark.py fetch-network writes)."""
    return {key: network.get(key) for key in NETWORK_FIELDS if key in network}


def site_networks(topic_id: str, state: dict | None, bucket: str, load=None) -> list[tuple[str, dict, str]]:
    """(site id, network, where) for each site of a topic: the site's own object
    (vision/<topic>/<site>/network.json, through vision_sites.load_network), or the network a state
    written before kept inline (`legacy_inline`)."""
    load = load or vision_sites.load_network
    found = []
    sites = (state or {}).get("sites") if isinstance((state or {}).get("sites"), dict) else {}
    for site_id in sorted(sites):
        record = sites[site_id] if isinstance(sites[site_id], dict) else {}
        network, where = None, None
        if record.get("network_key"):
            with _content_bucket(bucket):
                network = load(topic_id, site_id)
            where = vision_sites.network_key(topic_id, site_id)
        if network is None and isinstance(record.get("network"), dict):
            network, where = record["network"], "legacy_inline"
        if isinstance(network, dict) and isinstance(network.get("ways"), list):
            found.append((site_id, network, where))
    return found


# --- provenance: the functions and the deploy that produced them ---------------------------------------


def function_configs(aws: Aws, names: dict) -> dict:
    out = {}
    for key, (_tail, which) in FUNCTIONS.items():
        region = aws.region(which)
        try:
            config = aws.lambda_(region).get_function_configuration(FunctionName=names[key])
        except Exception as exc:  # noqa: BLE001 - said, never raised
            out[key] = {"region": region, "error": _error_code(exc)}
            continue
        out[key] = {"region": region, **{field: config.get(field) for field in FUNCTION_FIELDS}}
    return out


def deploy_runs(branch: str, run=subprocess.run) -> tuple[list[dict] | None, str | None]:
    """The terraform.yml runs on `branch`, newest first, from `gh run list` (a subprocess): (runs,
    None), or (None, why) when gh is missing, signed out or does not answer."""
    command = [
        "gh",
        "run",
        "list",
        "--workflow",
        "terraform.yml",
        "--branch",
        branch,
        "--json",
        "headSha,createdAt,conclusion",
        "--limit",
        str(GH_RUNS),
    ]
    try:
        done = run(command, cwd=ROOT, capture_output=True, text=True, timeout=GH_TIMEOUT_SECONDS)
    except FileNotFoundError:
        return None, "gh is not installed"
    except subprocess.TimeoutExpired:
        return None, f"gh did not answer in {GH_TIMEOUT_SECONDS} s"
    except OSError as exc:
        return None, f"gh could not be run ({type(exc).__name__})"
    if done.returncode != 0:
        return None, f"gh exited {done.returncode} (signed out, or no access to the repository's runs)"
    try:
        runs = json.loads(done.stdout or "[]")
    except ValueError:
        return None, "gh's answer was not JSON"
    kept = []
    for item in runs if isinstance(runs, list) else []:
        sha = item.get("headSha") if isinstance(item, dict) else None
        if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{40}", sha) and _time(item.get("createdAt")):
            kept.append(
                {
                    "head_sha": sha,
                    "created_at": item["createdAt"],
                    "conclusion": evidence.check("code", item.get("conclusion")),
                }
            )
    return sorted(kept, key=lambda r: _time(r["created_at"]), reverse=True), None


def deploy_for(last_modified, runs: list[dict] | None, why: str | None) -> dict:
    """The deploy that produced a function's code: no deploy records a git commit, so it is the
    newest terraform.yml run on the branch created before the function's LastModified."""
    rule = "the newest terraform.yml run on the branch created before the function's LastModified"
    if runs is None:
        return {"head_sha": None, "reason": why, "rule": rule}
    when = _time(last_modified)
    if when is None:
        return {"head_sha": None, "reason": "the function's LastModified was not read", "rule": rule}
    before = [r for r in runs if _time(r["created_at"]) <= when]
    if not before:
        return {
            "head_sha": None,
            "reason": f"no run before LastModified among the newest {GH_RUNS}",
            "rule": rule,
        }
    return {**before[0], "reason": None, "rule": rule}


# --- the backstop --------------------------------------------------------------------------------------


def backstop(value) -> tuple[object, int]:
    """`value` with every 12-digit number and every `arn:` string replaced with REDACTED, keys
    included, and how many were. No schema field holds one: a count above zero says one got through."""
    count = 0

    def text(s: str) -> str:
        nonlocal count
        s, n = _ARN.subn(REDACTED, s)
        s, m = _TWELVE_DIGITS.subn(REDACTED, s)
        count += n + m
        return s

    def walk(item):
        nonlocal count
        if isinstance(item, str):
            return text(item)
        if isinstance(item, bool) or item is None:
            return item
        if isinstance(item, int | float):
            if (isinstance(item, int) or float(item).is_integer()) and len(str(abs(int(item)))) == 12:
                count += 1
                return REDACTED
            return item
        if isinstance(item, dict):
            return {text(str(key)): walk(val) for key, val in item.items()}
        if isinstance(item, list | tuple):
            return [walk(val) for val in item]
        return text(str(item))

    return walk(value), count


# --- the summary ---------------------------------------------------------------------------------------


def percentile(values: list, p: float):
    """The nearest-rank percentile of `values` (None when empty)."""
    ordered = sorted(v for v in values if isinstance(v, int | float) and not isinstance(v, bool))
    if not ordered:
        return None
    return ordered[max(0, math.ceil(p / 100.0 * len(ordered)) - 1)]


def _spread(values: list) -> dict:
    values = [v for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    return {
        "n": len(values),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
        "max": max(values) if values else None,
    }


def _zeros(keys, counter: Counter) -> dict:
    """Every known key with its count, zero included, then any other key seen."""
    out = {key: counter.get(key, 0) for key in keys}
    out.update({key: n for key, n in sorted(counter.items(), key=lambda kv: str(kv[0])) if key not in out})
    return out


def _state_note(kinds: dict, *names: str) -> dict:
    """Which of the log reads a block was counted from, and how each ended: a block counted from a
    read that was not checked is partial, never complete."""
    states = {name: kinds[name]["state"] for name in names if name in kinds}
    return {"logs": states, "complete": all(state == "complete" for state in states.values())}


def wilson(k: int, n: int):
    import vision_eval_scoring

    return vision_eval_scoring.wilson(k, n)


def read_labels(live_dir: Path) -> list[dict]:
    """Every operator label under `live/` (`live/labels.csv`, `live/*/labels.csv`), oldest export
    first: {decision, verdict, label, reason}."""
    rows = []
    for path in sorted({*Path(live_dir).glob(LABEL_FILE), *Path(live_dir).glob(f"*/{LABEL_FILE}")}):
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        lines = [line for line in text.splitlines() if not line.startswith("#")]
        for row in csv.DictReader(lines):
            if row.get("label") in ("agree", "disagree"):
                rows.append({key: row.get(key) for key in ("decision", "verdict", "label", "reason")})
    return rows


def agreement(labels: list[dict], decisions: list[dict], demos) -> dict:
    """The operator's agreement with the agent on triaged live decisions (O9's labels): k of n with
    a Wilson 95 % interval, cities and demo apart; a decision labelled twice counts once (the last)."""
    triaged = {
        f"{r['topic_id']}/{r['site_id']}/{r['scene_id']}": r
        for r in decisions
        if r.get("triage") and r.get("kept")
    }
    latest = {row["decision"]: row for row in labels if row.get("decision") in triaged}
    out = {}
    for group in ("cities", "demo"):
        rows = [row for key, row in latest.items() if group_of(triaged[key]["topic_id"], demos) == group]
        n = len(rows)
        k = sum(1 for row in rows if row["label"] == "agree")
        low, high = wilson(k, n)
        out[group] = {
            "triaged": sum(1 for r in triaged.values() if group_of(r["topic_id"], demos) == group),
            "labelled": n,
            "agree": k,
            "share": round(k / n, 4) if n else None,
            "wilson_low": low,
            "wilson_high": high,
            "disagree_reasons": dict(Counter(row["reason"] for row in rows if row["label"] == "disagree")),
        }
    return out


def summarise(data: dict) -> dict:
    """summary.json: 2.6.5's human control and observability, 2.5.1's observed failures (zero rows
    included), E11, the decision counts by code, the worker by caller, build and CPU, and the spend,
    each per group (the cities, the demo topics apart) and per topic."""
    demos, topics = data["demos"], data["topic_ids"]
    groups = ("cities", "demo")
    kinds = data["logs"]

    def by_group(items, topic_key="topic_id"):
        split = {group: [] for group in groups}
        for item in items:
            split[group_of(item.get(topic_key), demos)].append(item)
        return split

    # Decisions by code (kept), the agent's verdicts, not kept.
    decisions = data["decisions"]
    decision_counts = {}
    for group, items in by_group(decisions).items():
        kept = [r for r in items if r["kept"]]
        triaged = [r for r in kept if r.get("triage")]
        verdicts = Counter(
            (r["triage"] or {}).get("verdict")
            if (r["triage"] or {}).get("stop") == "answered"
            else "failed_closed"
            for r in triaged
        )
        decision_counts[group] = {
            "kept": len(kept),
            "by_code": _zeros(vision_decisions.CODES, Counter(r["code"] for r in kept)),
            "triaged": len(triaged),
            "verdicts": _zeros((*VERDICTS, "failed_closed"), verdicts),
            "reported": sum(1 for r in kept if r.get("reported")),
            "findings_written": sum(1 for r in kept if r.get("finding_written") is True),
            "not_kept_copies": sum(len(r.get("not_kept") or []) for r in items),
            "not_kept_only": sum(1 for r in items if not r["kept"]),
        }
    decision_counts["by_topic"] = {
        topic: dict(Counter(r["code"] for r in decisions if r["topic_id"] == topic and r["kept"]))
        for topic in topics
    }
    decision_counts["unknown_kept"] = len(data["merge"]["unknown"])
    decision_counts.update(_state_note(kinds, "decisions", "ticks"))

    # 2.5.1: failures observed in the deployment.
    sites, measures, triage_lines, ticks = data["sites"], data["measures"], data["triages"], data["ticks"]
    worker_reports, tick_reports = data["worker_reports"], data["tick_reports"]
    site_codes = (*vision_sites.STATUS_WORDS, "osm", "network_read")
    failures = {}
    attempts_without_line = data["attempts_without_line"]
    for group, items in by_group(sites).items():
        network_errors = Counter()
        for topic, history in data["history"].items():
            if group_of(topic, demos) != group:
                continue
            for site in history["sites"].values():
                if site.get("network_error") and site["network_error"].get("code"):
                    network_errors[site["network_error"]["code"]] += 1
        group_ticks = [t for t in ticks if group_of(t.get("topic_id"), demos) == group]
        group_triages = [t for t in triage_lines if group_of(t.get("topic_id"), demos) == group]
        failures[group] = {
            "site_turns_by_outcome": _zeros(SITE_OUTCOMES, Counter(s.get("outcome") for s in items)),
            "site_turns_by_code": _zeros(site_codes, Counter(s["code"] for s in items if s.get("code"))),
            "ticks_stored_false": sum(1 for t in group_ticks if t.get("stored") is False),
            "attempts_without_research_tick_line": attempts_without_line.get(group, 0),
            "osm_site_turns": sum(1 for s in items if s.get("code") == "osm"),
            "network_errors_now": _zeros(tuple(osm.ERROR_WORDS), network_errors),
            "triage_stops": _zeros(STOPS, Counter(t.get("stop") for t in group_triages)),
            "triage_failed": sum(1 for t in group_triages if t.get("stop") != "answered"),
        }
    production = [m for m in measures if caller_of(m.get("site_id")) == PRODUCTION]
    demo_measures = [m for m in measures if caller_of(m.get("site_id")) == "demo"]
    measured_ids = {m.get("request_id") for m in measures if m.get("request_id")}
    no_reply = [r for r in worker_reports if r["request_id"] not in measured_ids]
    failures["worker"] = {
        "refusals_by_code": {
            caller: _zeros(
                vision_contract.ERROR_CODES, Counter(m.get("code") for m in lines if m.get("ok") is False)
            )
            for caller, lines in (("production", production), ("demo", demo_measures))
        },
        "ended_without_a_reply": _zeros(
            ("timeout", "out_of_memory", "error", "other"), Counter(no_reply_cause(r) for r in no_reply)
        ),
        "note": (
            "a worker timeout never writes vision_measure: these are counted from Lambda's REPORT lines, "
            "of every caller (a run that wrote no line names no site)"
        ),
    }
    failures["research_tick"] = {
        "timeouts": sum(1 for r in tick_reports if r.get("status") == "timeout"),
        "errors": sum(1 for r in tick_reports if r.get("status") == "error"),
        "note": "REPORT lines of every topic's ticks: a timed-out tick writes no research_tick line",
    }
    failures.update(
        _state_note(kinds, "sites", "ticks", "decisions", "measures", "worker_reports", "tick_reports")
    )

    # E11: the tick's time with and without the agent.
    e11 = {}
    for group, items in by_group(ticks).items():
        e11[group] = {
            "with_agent": _spread([t.get("ms") for t in items if (t.get("triages") or 0) > 0]),
            "measured_without_agent": _spread(
                [t.get("ms") for t in items if (t.get("decisions") or 0) > 0 and not t.get("triages")]
            ),
            "no_decision": _spread([t.get("ms") for t in items if not t.get("decisions")]),
        }
    e11["unit"] = "ms, the research tick's own wall time (research_tick.ms), nearest-rank percentiles"
    e11.update(_state_note(kinds, "ticks"))

    # The worker: p50, p95 and the slowest by caller, build and CPU.
    report_by_id = {r["request_id"]: r for r in worker_reports}
    rows = {}
    for line in measures:
        key = (caller_of(line.get("site_id")), line.get("backend"), line.get("build"), line.get("cpu"))
        rows.setdefault(key, []).append(line)
    worker = []
    for (caller, backend, build, cpu), lines in sorted(
        rows.items(), key=lambda kv: tuple(str(v) for v in kv[0])
    ):
        billed = [
            report_by_id[m["request_id"]].get("billed_ms")
            for m in lines
            if m.get("request_id") in report_by_id
        ]
        worker.append(
            {
                "caller": caller,
                "backend": backend,
                "build": build,
                "cpu": cpu,
                "runs": len(lines),
                "ok": sum(1 for m in lines if m.get("ok") is True),
                "refused": sum(1 for m in lines if m.get("ok") is False),
                "total_ms": _spread([m.get("total_ms") for m in lines]),
                "billed_ms": _spread(billed),
                "peak_rss_mb": max(
                    (m["rss_peak_mb"] for m in lines if isinstance(m.get("rss_peak_mb"), int)), default=None
                ),
            }
        )
    production_lines = production or []
    with_identity = sum(1 for m in production_lines if m.get("build") and m.get("cpu"))
    worker_block = {
        "rows": worker,
        "unit": "ms; nearest-rank percentiles",
        "gb_seconds": round(
            sum(
                (r.get("billed_ms") or 0) / 1000.0 * (r.get("memory_mb") or 0) / 1024.0
                for r in worker_reports
            ),
            3,
        ),
        **_state_note(kinds, "measures", "worker_reports"),
    }

    # 2.6.5: human control and observability.
    articles, people = data["articles"], data["people"]
    article_by_id = {a["article_id"]: a for a in articles if a.get("article_id")}
    human = {}
    for group in groups:
        group_articles = [a for a in articles if group_of(a["topic_id"], demos) == group]
        published = [a for a in group_articles if a.get("status") == "published"]
        by_humans = sum(1 for a in published if a.get("published_by") == "humans")
        group_people = [
            p
            for p in people
            if p.get("event") == "moderation_decision" and group_of(p.get("topic_id"), demos) == group
        ]
        hours: dict[str, list] = {}
        for line in group_people:
            article = article_by_id.get(line.get("article_id"))
            first = min((_time(at) for at in (article or {}).get("research_captured_at") or []), default=None)
            decided = _time(line.get("logged"))
            if first and decided:
                hours.setdefault(line["decision"], []).append(
                    round((decided - first).total_seconds() / 3600.0, 2)
                )
        human[group] = {
            "articles": len(group_articles),
            "published": len(published),
            "published_by_humans": by_humans,
            "published_otherwise": len(published) - by_humans,
            "held_share": round(by_humans / len(published), 4) if published else None,
            "decisions": _zeros(MODERATION_DECISIONS, Counter(p["decision"] for p in group_people)),
            "hours_from_finding_to_decision": {d: _spread(v) for d, v in sorted(hours.items())},
        }
    human["agreement"] = agreement(data["labels"], decisions, demos)
    human["completeness"] = data["completeness"]
    human["worker_lines_with_build_and_cpu"] = {
        "production_lines": len(production_lines),
        "with_both": with_identity,
        "share": round(with_identity / len(production_lines), 4) if production_lines else None,
    }
    human.update(_state_note(kinds, "decisions", "ticks", "people", "measures"))

    # Spend: the agent's (the triage lines' cost_usd, failed attempts included: it was spent) and the
    # research summaries' (the Findings' research_call, at the built-in price table).
    spend = {"rate": USD_TO_AUD_RATE, "rate_note": "AUD at the project's fixed rate (common/costing.py)"}
    for group in groups:
        usd = sum(
            t["cost_usd"]
            for t in triage_lines
            if group_of(t.get("topic_id"), demos) == group and isinstance(t.get("cost_usd"), int | float)
        )
        research_usd, unpriced = 0.0, 0
        for finding in data["findings"]:
            call = finding.get("research_call")
            if group_of(finding["topic_id"], demos) != group or not call or not call.get("model_id"):
                continue
            cost = call_cost_usd(
                {
                    "input_tokens": call.get("input_tokens") or 0,
                    "output_tokens": call.get("output_tokens") or 0,
                },
                default_model_entry(call["model_id"]),
            )
            if cost is None:
                unpriced += 1
            else:
                research_usd += cost
        spend[group] = {
            "agent_usd": round(usd, 6),
            "agent_aud": round(usd * USD_TO_AUD_RATE, 6),
            "research_usd": round(research_usd, 6),
            "research_aud": round(research_usd * USD_TO_AUD_RATE, 6),
            "research_unpriced_calls": unpriced,
        }
    spend.update(_state_note(kinds, "decisions"))

    return {
        "schema": SCHEMA,
        "exported_at": data["exported_at"],
        "env": data["env"],
        "window": data["window"],
        "topics": data["topics_summary"],
        "demo_topics": sorted(demos),
        "logs": {
            kind: {
                "state": value["state"],
                "rows": len(value["rows"]),
                "missing": value["missing"],
                "dropped": data["dropped"].get(kind, 0),
            }
            for kind, value in kinds.items()
        },
        "decisions": decision_counts,
        "observed_failures": failures,
        "e11_tick_time": e11,
        "worker": worker_block,
        "human_control": human,
        "spend": spend,
        "redacted": None,  # filled once the backstop has run
    }


def completeness(data: dict) -> dict:
    """2.6.5's completeness, over kept attempts only: every kept vision_decision line has its ring
    entry (or the ring rolled over, said); every kept vision_triage line has its ring entry; every
    worker request id in a kept attempt's lines has a vision_measure line, or a REPORT line that
    ended without a reply, or is one the caller gave up on. What has none of these is unmatched, and
    listed."""
    attempt_by: dict = {}
    for attempt in data["ticks"]:
        attempt_by.setdefault((attempt.get("tick"), attempt["run"]), attempt)
    ring = {}
    oldest: dict[tuple, str] = {}
    for record in data["decisions"]:
        if record["source"] == "state":
            ring[(record["topic_id"], record["site_id"], record["scene_id"], record["at"])] = record
            site = (record["topic_id"], record["site_id"])
            if record.get("at") and (site not in oldest or record["at"] < oldest[site]):
                oldest[site] = record["at"]
    unmatched = []
    decisions = {"kept_lines": 0, "with_ring_entry": 0, "ring_rolled_over": 0, "unmatched": 0}
    at_of: dict[tuple, str] = {}
    for line in data["decision_lines"]:
        _attempt, kept = attempt_of(line, attempt_by)
        at_of[(line.get("tick"), line.get("run"), line["site_id"], line["scene_id"])] = line.get("at")
        if not kept:
            continue
        decisions["kept_lines"] += 1
        site = (line["topic_id"], line["site_id"])
        if (line["topic_id"], line["site_id"], line["scene_id"], line.get("at")) in ring:
            decisions["with_ring_entry"] += 1
        elif site in oldest and (line.get("at") or "") < oldest[site]:
            decisions["ring_rolled_over"] += 1
        elif not data["states_read"].get(line["topic_id"]):
            decisions["ring_rolled_over"] += 0
            decisions.setdefault("state_not_read", 0)
            decisions["state_not_read"] += 1
        else:
            decisions["unmatched"] += 1
            unmatched.append(_listed("vision_decision", line))
    triages = {"kept_lines": 0, "with_ring_entry": 0, "ring_rolled_over": 0, "unmatched": 0}
    for line in data["triages"]:
        _attempt, kept = attempt_of(line, attempt_by)
        if not kept:
            continue
        triages["kept_lines"] += 1
        at = at_of.get((line.get("tick"), line.get("run"), line["site_id"], line["scene_id"]))
        entry = ring.get((line["topic_id"], line["site_id"], line["scene_id"], at))
        site = (line["topic_id"], line["site_id"])
        if entry is not None and entry.get("triage"):
            triages["with_ring_entry"] += 1
        elif at and site in oldest and at < oldest[site]:
            triages["ring_rolled_over"] += 1
        else:
            triages["unmatched"] += 1
            unmatched.append(_listed("vision_triage", line))
    measured = {m.get("request_id") for m in data["measures"] if m.get("request_id")}
    reported = {r["request_id"] for r in data["worker_reports"]}
    gave_up = {s.get("worker_request_id") for s in data["sites"] if s.get("code") in ("invoke", "reply")}
    workers = {
        "request_ids": 0,
        "with_vision_measure": 0,
        "ended_without_a_reply": 0,
        "caller_gave_up": 0,
        "unmatched": 0,
        "not_checked": 0,
    }
    worker_read = (
        data["logs"]["measures"]["state"] == "complete"
        and data["logs"]["worker_reports"]["state"] == "complete"
    )
    seen = set()
    for line in [*data["sites"], *data["decision_lines"], *data["triages"]]:
        _attempt, kept = attempt_of(line, attempt_by)
        if not kept:
            continue
        ids = [line.get("worker_request_id")] + list(line.get("worker_request_ids") or [])
        for request_id in ids:
            if not isinstance(request_id, str) or request_id in seen:
                continue
            seen.add(request_id)
            workers["request_ids"] += 1
            if request_id in measured:
                workers["with_vision_measure"] += 1
            elif request_id in reported:
                workers["ended_without_a_reply"] += 1
            elif request_id in gave_up:
                workers["caller_gave_up"] += 1
            elif not worker_read:
                workers["not_checked"] += 1
            else:
                workers["unmatched"] += 1
                unmatched.append(
                    {
                        "kind": "worker_request_id",
                        "request_id": request_id,
                        "topic_id": line["topic_id"],
                        "site_id": line.get("site_id"),
                        "tick": line.get("tick"),
                        "run": line.get("run"),
                    }
                )
    return {
        "decision_lines": decisions,
        "triage_lines": triages,
        "worker_request_ids": workers,
        "unmatched": unmatched,
        "dropped": dict(data["dropped"]),
    }


def _listed(kind: str, line: dict) -> dict:
    return {
        "kind": kind,
        **{
            key: line.get(key)
            for key in ("topic_id", "site_id", "scene_id", "tick", "run", "at", "code", "logged")
        },
        "from": line.get("_from"),
    }


# --- collect: everything, in memory --------------------------------------------------------------------


def collect(
    args,
    aws: Aws,
    *,
    now: datetime,
    gh=subprocess.run,
    sleep=time.sleep,
    run=None,
    topics_only: list[str] | None = None,
    with_provenance: bool = True,
    live_dir: Path = LIVE_DIR,
) -> dict:
    """Read everything the export writes, in memory: the topics, their states, the logs, the tables,
    and (`with_provenance`) the functions and the deploy. `topics_only` narrows to those topics (the
    live trace); `live_dir` is where the operator's labels are read from."""
    names = resource_names(args.prefix, args.env)
    since = parse_since(getattr(args, "since", None), now)
    demos = set(demo_topics(TRACKER.read_text(encoding="utf-8"))) if TRACKER.is_file() else set()
    vocabularies = vision_evidence.declared_vocabularies()
    try:
        table_topics = read_topics(aws, names["topics"])
        topics_error = None
    except Exception as exc:  # noqa: BLE001 - the named topics can still be read
        table_topics, topics_error = [], _error_code(exc)
    adapter_of = {t["topic_id"]: t.get("adapter") for t in table_topics if isinstance(t.get("topic_id"), str)}
    wanted = topics_only or (
        [t for t in args.topics.split(",") if t] if getattr(args, "topics", None) else None
    )
    if wanted:
        bad = [t for t in wanted if not ID_PATTERN.match(t)]
        if bad:
            raise Refused(f"not a topic id: {', '.join(bad)}")
        topic_ids = list(dict.fromkeys(wanted))
    else:
        if topics_error is not None:
            raise Refused(
                f"the Topics table {names['topics']} could not be read ({topics_error}); name --topics"
            )
        topic_ids = sorted(
            t["topic_id"] for t in table_topics if vision_evidence._keeps_evidence(t, vocabularies)
        )
    if not topic_ids:
        raise Refused(f"no topic in {names['topics']} keeps decision evidence; name --topics")

    # The running states.
    states, state_meta = {}, {}
    for topic_id in topic_ids:
        states[topic_id], state_meta[topic_id] = read_state(aws, names["bucket"], topic_id)

    # The logs.
    texts = queries(topic_ids)
    groups = {
        "tick": (aws.home_region, names["research_tick_group"]),
        "admin": (aws.home_region, names["admin_api_group"]),
        "worker": (aws.vision_region, names["vision_worker_group"]),
    }
    specs = {kind: (*groups[which], text) for kind, (which, text) in texts.items()}
    kinds, slices = read_logs(specs, since, now, client=aws.logs, wait=args.logs_wait, sleep=sleep, run=run)
    asked = set(topic_ids)
    moved_ok = vision_evidence.moved_codes(vocabularies)
    parsed, dropped = {}, {}
    for kind in ("decisions", "ticks", "people", "sites", "measures"):
        parsed[kind], dropped[kind] = parse_lines(kinds[kind]["rows"], kind, asked, moved_ok)
    reports = {}
    for kind in ("worker_reports", "tick_reports"):
        reports[kind] = [r for r in (parse_report(row) for row in kinds[kind]["rows"]) if r is not None]
        dropped[kind] = len(kinds[kind]["rows"]) - len(reports[kind])
    decision_lines = [line for line in parsed["decisions"] if line["event"] == "vision_decision"]
    triage_lines = [line for line in parsed["decisions"] if line["event"] == "vision_triage"]

    # The decisions: the rings, then what only the logs hold.
    records = []
    for topic_id in topic_ids:
        records += ring_records(topic_id, states[topic_id], with_notes=args.with_notes)
    unread = kinds["ticks"]["missing"] + [
        {"start": s["start"], "end": s["end"]}
        for s in slices.values()
        if s["kind"] == "ticks" and s["state"] == "cut"
    ]
    decisions, merge = merge_decisions(
        records,
        decision_lines,
        triage_lines,
        parsed["ticks"],
        unread_ticks=unread,
        with_notes=args.with_notes,
    )

    # The tables.
    articles, findings, table_meta = [], [], {"articles": {}, "findings": {}}
    for topic_id in topic_ids:  # the trace's person step needs the articles, live or exported
        items, meta = read_articles(aws, names["articles"], topic_id, _iso(since))
        articles += items
        table_meta["articles"][topic_id] = meta
        items, meta = read_findings(aws, names["findings"], topic_id, _iso(since), with_notes=args.with_notes)
        findings += items
        table_meta["findings"][topic_id] = meta
    finding_times = {f["captured_at"] for f in findings if f.get("captured_at")}
    finding_times |= {at for a in articles for at in a.get("research_captured_at") or []}
    attempt_by: dict = {}
    for attempt in parsed["ticks"]:
        attempt_by.setdefault((attempt.get("tick"), attempt["run"]), attempt)
    for record in decisions:
        attempt = record.pop("_attempt", None) or attempt_by.get((record.get("tick"), record.get("run")))
        record["finding_written"] = finding_written(record, attempt, finding_times)
    # Attempts that decided but left no research_tick line (the tick timed out or crashed), by group.
    without_line: Counter = Counter()
    seen_attempts = set()
    for line in [*parsed["sites"], *decision_lines]:
        key = (line.get("tick"), line.get("run"))
        if key in attempt_by or key in seen_attempts or _inside(line.get("logged"), unread):
            continue
        seen_attempts.add(key)
        without_line[group_of(line.get("topic_id"), demos)] += 1

    topics_summary = []
    for topic_id in topic_ids:
        history = history_of(states[topic_id])
        topics_summary.append(
            {
                "topic_id": topic_id,
                "adapter": adapter_of.get(topic_id),
                "group": group_of(topic_id, demos),
                "in_topics_table": topic_id in adapter_of,
                "state": state_meta[topic_id]["read"],
                "state_error": state_meta[topic_id].get("error"),
                "first_observed_at": history["first_observed_at"],
                "sites": {
                    site: {"first_observed_at": s["first_observed_at"], "ring_entries": s["ring_entries"]}
                    for site, s in history["sites"].items()
                },
            }
        )
    data = {
        "exported_at": _iso(now),
        "env": args.env,
        "prefix": args.prefix,
        "names": names,
        "window": {"since": _iso(since), "until": _iso(now)},
        "demos": demos,
        "topic_ids": topic_ids,
        "topics_summary": topics_summary,
        "topics_error": topics_error,
        "states": states,
        "state_meta": state_meta,
        "states_read": {t: states[t] is not None for t in topic_ids},
        "logs": kinds,
        "slices": slices,
        "dropped": dropped,
        "decision_lines": decision_lines,
        "triages": triage_lines,
        "ticks": parsed["ticks"],
        "people": parsed["people"],
        "sites": parsed["sites"],
        "measures": parsed["measures"],
        "worker_reports": reports["worker_reports"],
        "tick_reports": reports["tick_reports"],
        "decisions": decisions,
        "merge": merge,
        "articles": articles,
        "findings": findings,
        "table_meta": table_meta,
        "history": {t: history_of(states[t]) for t in topic_ids},
        "attempts_without_line": dict(without_line),
        "vocabularies": vocabularies,
        "adapter_of": adapter_of,
        "labels": read_labels(live_dir),
    }
    data["completeness"] = completeness(data)
    if with_provenance:
        data["functions"] = function_configs(aws, names)
        runs, why = deploy_runs(args.deploy_branch, run=gh)
        for key, config in data["functions"].items():
            config["deploy"] = deploy_for(config.get("LastModified"), runs, why)
        data["deploy_runs_read"] = None if runs is None else len(runs)
    return data


# --- writing the export --------------------------------------------------------------------------------


def _public(line: dict, drop=("evidence",)) -> dict:
    """A parsed line as written: its schema fields and `logged`, never the private keys."""
    return {key: value for key, value in line.items() if not key.startswith("_") and key not in drop}


def _file_name(*parts: str) -> str:
    """A file name from ids: `:` (allowed in an id) is not allowed on Windows."""
    return "__".join(part.replace(":", "-") for part in parts)


def worker_lines(data: dict) -> list[dict]:
    """worker.jsonl: every vision_measure line with its REPORT's billed duration and most memory
    used, joined by the request id, and its caller; then every REPORT with no vision_measure line,
    which ended without a reply."""
    by_id = {r["request_id"]: r for r in data["worker_reports"]}
    out, measured = [], set()
    for line in data["measures"]:
        report = by_id.get(line.get("request_id"))
        measured.add(line.get("request_id"))
        out.append(
            {
                **_public(line),
                "caller": caller_of(line.get("site_id")),
                "billed_ms": (report or {}).get("billed_ms"),
                "max_memory_mb": (report or {}).get("max_memory_mb"),
                "memory_mb": (report or {}).get("memory_mb"),
                "_from": line["_from"],
            }
        )
    for report in data["worker_reports"]:
        if report["request_id"] in measured:
            continue
        out.append(
            {
                "event": "lambda_report",
                "ended_without_a_reply": True,
                "cause": no_reply_cause(report),
                "caller": None,
                **_public(report),
            }
        )
        out[-1]["_from"] = report["_from"]
    return out


def labels_template(decisions: list[dict], figures: dict, traces: dict) -> str:
    """labels-template.csv: one row per triaged live decision, for the operator (O9): the decision,
    the agent's verdict, its figure and trace files, and empty `label` and `reason`. Its columns and
    codes are vision_eval.py's (`index --check` reads the filled-in `labels.csv`)."""
    import vision_eval

    buffer = io.StringIO()
    buffer.write("# Label each triaged live decision: label is agree or disagree with the agent's verdict;\n")
    buffer.write(f"# reason is one of {', '.join(vision_eval.LABEL_REASONS)}. Save as {LABEL_FILE}.\n")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(vision_eval.LABEL_COLUMNS)
    for record in decisions:
        triage = record.get("triage") or {}
        if not record["kept"] or triage.get("verdict") not in VERDICTS:
            continue
        key = (record["topic_id"], record["site_id"], record["scene_id"])
        writer.writerow(["/".join(key), triage["verdict"], figures.get(key, ""), traces.get(key, ""), "", ""])
    return buffer.getvalue()


def trace_markdown(record: dict, data_like: dict, *, source: str) -> str:
    """One decision's chain as Markdown: vision_decisions.trace over the record, its attempt, its
    article and what the person did, rendered as the assistant's scene view renders it
    (vision_evidence.trace_rows)."""
    vocabulary = data_like["vocabularies"].get(
        data_like["adapter_of"].get(record["topic_id"]) or "rail_access", {}
    )
    attempt, article, person = trace_context(record, data_like)
    rows = vision_decisions.trace(
        record,
        vocabulary=vocabulary,
        attempt=attempt,
        article=article,
        person=person,
        now=_time(data_like["exported_at"]),
    )
    columns = vision_evidence.TRACE_COLUMNS
    lines = [
        f"# Trace: {record['topic_id']} / {record['site_id']} / {record['scene_id']}",
        "",
        f"From {source}. The decision at {record.get('at')}, {'kept' if record['kept'] else 'not kept'} "
        f"(its copy from the {'running state' if record['source'] == 'state' else 'logs'}).",
        "",
        "| " + " | ".join(columns) + " |",
        "|" + "---|" * len(columns),
    ]
    for row in vision_evidence.trace_rows(rows, record, vocabulary):
        lines.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    if attempt is None and record["source"] == "state":
        lines += [
            "",
            "The tick's research_tick line was not read (out of the window, or not checked): whether a "
            "Finding was written is read from the Findings table and the articles.",
        ]
    if record.get("not_kept"):
        lines += [
            "",
            f"Measured and decided {len(record['not_kept'])} more time(s) by ticks that failed "
            "(not kept: the next tick measured the scene again).",
        ]
    if record.get("note"):
        lines += [
            "",
            f"{record['note']['label'][:1].upper()}{record['note']['label'][1:]}: "
            f"\"{_cell(record['note']['text'])}\"",
        ]
    lines += [
        "",
        "Contains modified Copernicus Sentinel data; rail network data © OpenStreetMap contributors "
        "(ODbL).",
        "",
    ]
    return "\n".join(lines)


def _cell(text) -> str:
    return re.sub(r"\s+", " ", str(text)).replace("|", "/").strip()


def trace_context(record: dict, data_like: dict) -> tuple[dict | None, dict | None, dict | None]:
    """The attempt, the article and the person's decision a trace reads, from what the export holds."""
    attempt = next(
        (
            t
            for t in data_like["ticks"]
            if (t.get("tick"), t.get("run")) == (record.get("tick"), record.get("run"))
        ),
        None,
    )
    if attempt is None and record["source"] == "state":
        # Kept (it is in the stored ring); the Finding from the tables when the line was not read.
        written = record.get("finding_written")
        attempt = {"stored": True, "finding": record.get("at") if written else None}
    article = next(
        (
            a
            for a in data_like["articles"]
            if record.get("at") in (a.get("research_captured_at") or [])
            and a["topic_id"] == record["topic_id"]
        ),
        None,
    )
    person = None
    if article is not None:
        decided = [
            p
            for p in data_like["people"]
            if p.get("event") == "moderation_decision" and p.get("article_id") == article.get("article_id")
        ]
        if decided:
            newest = max(decided, key=lambda p: p.get("logged") or "")
            person = {"decision": newest["decision"], "at": newest.get("logged")}
    return attempt, article, person


def _jsonl(items: list[dict]) -> str:
    return "".join(json.dumps(item, ensure_ascii=False, sort_keys=False) + "\n" for item in items)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1) + "\n"


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def build_files(data: dict, *, figures: dict, traces: dict) -> tuple[dict[str, object], dict]:
    """{file name: content} for the export's own files, and the provenance of their lines."""

    def lines_of(items, name):
        provenance = []
        out = []
        for n, item in enumerate(items, start=1):
            source = item.get("_from")
            provenance.append({"line": n, **(source or {})})
            out.append(_public(item))
        return out, provenance

    by_line: dict[str, list] = {}
    decisions_out = []
    decision_provenance = []
    for n, record in enumerate(data["decisions"], start=1):
        entry = {"line": n, "from": record["source"]}
        if record["source"] == "state":
            meta = data["state_meta"][record["topic_id"]]
            entry["state"] = {key: meta.get(key) for key in ("key", "etag", "last_modified", "version_id")}
        if record.get("_log_from"):
            entry["log"] = record["_log_from"]
        decision_provenance.append(entry)
        decisions_out.append(_public(record))
    by_line["decisions.jsonl"] = decision_provenance
    files: dict[str, object] = {"decisions.jsonl": decisions_out}
    for name, items in (
        ("triage.jsonl", data["triages"]),
        ("ticks.jsonl", data["ticks"]),
        ("people.jsonl", data["people"]),
        ("worker.jsonl", worker_lines(data)),
    ):
        files[name], by_line[name] = lines_of(items, name)
    files["articles.json"] = {"topics": data["table_meta"]["articles"], "articles": data["articles"]}
    files["findings.json"] = {
        "topics": data["table_meta"]["findings"],
        "findings": data["findings"],
        "summaries": "included, through redact.scrub"
        if data.get("with_notes")
        else "left out (run with --with-notes)",
    }
    files["history.json"] = data["history"]
    files[TEMPLATE_FILE] = labels_template(data["decisions"], figures, traces)
    provenance = {
        "schema": SCHEMA,
        "exported_at": data["exported_at"],
        "env": data["env"],
        "prefix": data["prefix"],
        "regions": {"home": data["regions"][0], "vision": data["regions"][1]},
        "exporter": {"script": "scripts/evidence_export.py", **vision_runs.code_identity()},
        "functions": data.get("functions"),
        "deploy_runs_read": data.get("deploy_runs_read"),
        "states": {topic: {k: v for k, v in meta.items()} for topic, meta in data["state_meta"].items()},
        "tables": {
            "topics": data["names"]["topics"],
            "articles": data["names"]["articles"],
            "articles_index": "by_topic_created_at",
            "findings": data["names"]["findings"],
        },
        "queries": data["slices"],
        "lines": by_line,
        "figures": data.get("figure_provenance", {}),
        "networks": data.get("network_provenance", {}),
    }
    return files, provenance


def export(
    args,
    aws: Aws | None = None,
    *,
    now: datetime | None = None,
    gh=subprocess.run,
    sleep=time.sleep,
    run=None,
    pii=None,
) -> int:
    """`export`: read everything, write the directory, run the backstop and the PII check."""
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    aws = aws or Aws(args.region, args.vision_region)
    out_dir = Path(args.out) if args.out else LIVE_DIR / now.date().isoformat()
    if out_dir.exists() and any(out_dir.iterdir()) and not args.replace:
        raise Refused(
            f"{vision_runs.shown_path(out_dir)} already holds an export; pass --replace to write over it"
        )
    if args.figures is not None and args.figures < 0:
        raise Refused("--figures takes a number, 0 or more")
    data = collect(args, aws, now=now, gh=gh, sleep=sleep, run=run, live_dir=out_dir.parent)
    data["regions"] = (aws.home_region, aws.vision_region)
    data["with_notes"] = args.with_notes
    if out_dir.exists() and args.replace:
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    # The figures (at most N: the triaged decisions first, for the labels, then the other reported
    # ones; newest first) and one trace per triaged decision.
    figures, traces, figure_provenance = {}, {}, {}
    candidates = [r for r in data["decisions"] if r["kept"] and (r.get("triage") or r.get("reported"))]
    candidates = sorted(candidates, key=lambda r: r.get("at") or "", reverse=True)  # newest first
    candidates = sorted(candidates, key=lambda r: not r.get("triage"))  # stable: the triaged ones first
    for record in candidates[: args.figures or 0]:
        key = figure_key_of(data["states"].get(record["topic_id"]), record["site_id"], record["scene_id"])
        if key is None:
            continue
        try:
            response = aws.s3().get_object(Bucket=data["names"]["bucket"], Key=key)
            body = response["Body"].read()
        except Exception as exc:  # noqa: BLE001 - a figure not read is said, never raised
            figure_provenance[key] = {"error": _error_code(exc)}
            continue
        name = "figures/" + _file_name(record["topic_id"], record["site_id"], record["scene_id"]) + ".png"
        path = out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        written.append(path)
        figures[(record["topic_id"], record["site_id"], record["scene_id"])] = name
        figure_provenance[name] = {
            "key": key,
            "etag": str(response.get("ETag") or "").strip('"') or None,
            "last_modified": _iso(response["LastModified"]) if response.get("LastModified") else None,
        }
    data["figure_provenance"] = figure_provenance
    for record in data["decisions"]:
        if record["kept"] and (record.get("triage") or {}).get("verdict") in VERDICTS:
            name = "traces/" + _file_name(record["topic_id"], record["site_id"], record["scene_id"]) + ".md"
            traces[(record["topic_id"], record["site_id"], record["scene_id"])] = name

    # The networks (--networks): each city's object, in the fixture's shape.
    network_provenance = {}
    if args.networks:
        chosen: dict[str, tuple] = {}
        for topic_id in data["topic_ids"]:
            if group_of(topic_id, data["demos"]) == "demo":
                continue  # the demo topic measures Sydney's polygon again: the city's network is the fixture
            for site_id, network, where in site_networks(
                topic_id, data["states"][topic_id], data["names"]["bucket"]
            ):
                current = chosen.get(site_id)
                if current is None or str(network.get("fetched_at") or "") > str(
                    current[1].get("fetched_at") or ""
                ):
                    chosen[site_id] = (topic_id, network, where)
        for site_id, (topic_id, network, where) in sorted(chosen.items()):
            path = NETWORKS_DIR / f"{_file_name(site_id)}.json"
            write_text(path, json.dumps(fixture_of(network), ensure_ascii=False) + "\n")
            written.append(path)
            network_provenance[vision_runs.shown_path(path)] = {
                "topic_id": topic_id,
                "site_id": site_id,
                "from": where,
                "sha256": vision_runs.sha256_file(path),
                "fetched_at": network.get("fetched_at"),
            }
    data["network_provenance"] = network_provenance

    files, provenance = build_files(data, figures=figures, traces=traces)
    files["provenance.json"] = provenance
    for record in data["decisions"]:
        key = (record["topic_id"], record["site_id"], record["scene_id"])
        if key in traces:
            files[traces[key]] = trace_markdown(record, data, source=f"the export of {data['exported_at']}")
    # The backstop over every file's content, then the summary, which says how many it replaced.
    texts, total = {}, 0
    for name, content in files.items():
        cleaned, count = backstop(content)
        total += count
        texts[name] = (
            _jsonl(cleaned)
            if name.endswith(".jsonl")
            else _json(cleaned)
            if name.endswith(".json")
            else cleaned
        )
    summary = summarise(data)
    summary, count = backstop(summary)
    summary["redacted"] = total + count
    texts["summary.json"] = _json(summary)
    for name in (*FILES, *sorted(name for name in texts if name not in FILES)):
        path = out_dir / name
        write_text(path, texts[name])
        written.append(path)
    readme = out_dir.parent / "README.md"
    write_text(readme, LIVE_README)
    written.append(readme)

    # The PII check over every file written; a hit refuses the export and moves it aside.
    pii = pii or _pii_check
    if pii([str(path) for path in written]) != 0:
        aside = out_dir.with_name(out_dir.name + ".refused")
        if aside.exists():
            shutil.rmtree(aside)
        out_dir.rename(aside)
        for path in written:
            if path.parent == NETWORKS_DIR:
                path.unlink(missing_ok=True)  # a network file sits in a tracked directory: never left there
        print(
            f"refused: the personal-data check found a denylisted term (file and line above). The export was "
            f"moved to {vision_runs.shown_path(aside)}, which git ignores, and no network file was kept: "
            "remove the term at its source, or delete the directory; do not commit it.",
            file=sys.stderr,
        )
        return REFUSED
    _report(summary, out_dir, written)
    return 0


def _pii_check(paths: list[str]) -> int:
    import pii_denylist_check

    return pii_denylist_check.main(["--files", *paths])


def _report(summary: dict, out_dir: Path, written: list[Path]) -> None:
    print(f"wrote {len(written)} files under {vision_runs.shown_path(out_dir)}")
    for kind, state in summary["logs"].items():
        if state["state"] != "complete":
            print(f"  logs {kind}: {state['state']} (not checked is never zero)")
    counts = summary["decisions"]
    print(
        f"  decisions kept: cities {counts['cities']['kept']}, demo {counts['demo']['kept']}; "
        f"triaged: cities {counts['cities']['triaged']}, demo {counts['demo']['triaged']}"
    )
    if summary["redacted"]:
        print(f"  the backstop redacted {summary['redacted']} value(s): read why before committing")


# --- trace ---------------------------------------------------------------------------------------------


def load_export(directory: Path) -> dict:
    """An export read back for `trace` and `agreement`."""
    directory = Path(directory)
    if not (directory / "decisions.jsonl").is_file():
        raise Refused(f"{directory} is not an export (no decisions.jsonl)")

    def jsonl(name):
        path = directory / name
        return (
            [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            if path.is_file()
            else []
        )

    summary = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    articles = json.loads((directory / "articles.json").read_text(encoding="utf-8")).get("articles", [])
    return {
        "decisions": jsonl("decisions.jsonl"),
        "ticks": jsonl("ticks.jsonl"),
        "people": jsonl("people.jsonl"),
        "articles": articles,
        "summary": summary,
        "exported_at": summary.get("exported_at"),
        "adapter_of": {t["topic_id"]: t.get("adapter") for t in summary.get("topics") or []},
        "vocabularies": vision_evidence.declared_vocabularies(),
    }


def pick(decisions: list[dict], topic: str, site: str | None, scene: str) -> dict:
    """The decision `trace` shows: the topic's, of `site` (needed when the topic has several sites
    and the scene or "latest" does not say which), the scene asked or the newest; the kept copy."""
    mine = [r for r in decisions if r["topic_id"] == topic and (site is None or r["site_id"] == site)]
    if not mine:
        raise Refused(f"no decision of {topic}{'/' + site if site else ''} in this export")
    if scene != vision_evidence.LATEST:
        mine = [r for r in mine if r["scene_id"] == scene]
        if not mine:
            raise Refused(f"no decision for scene {scene} of {topic}")
    sites = sorted({r["site_id"] for r in mine})
    if site is None and len(sites) > 1:
        raise Refused(f"{topic} has decisions at {', '.join(sites)}: name one with --site")
    return max(mine, key=lambda r: (r.get("kept", False), r.get("at") or ""))


def trace(args, aws: Aws | None = None, *, now: datetime | None = None, sleep=time.sleep, run=None) -> int:
    if not ID_PATTERN.match(args.topic):
        raise Refused(f"not a topic id: {args.topic}")
    if args.site is not None and evidence.check("id", args.site) is None:
        raise Refused(f"not a site id: {args.site}")
    scene = vision_evidence._scene(args.scene)
    if scene is None:
        raise Refused(f"not a scene id: {args.scene}")
    if args.from_dir:
        data = load_export(Path(args.from_dir))
        source = f"the export {vision_runs.shown_path(Path(args.from_dir))}"
    else:
        if not args.env:
            raise Refused("name --env (live) or --from (an export)")
        now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
        aws = aws or Aws(args.region, args.vision_region)
        args.with_notes, args.since, args.topics = False, None, None
        data = collect(
            args, aws, now=now, sleep=sleep, run=run, topics_only=[args.topic], with_provenance=False
        )
        source = f"the live logs and running state of {args.env}, read at {data['exported_at']}"
    record = pick(data["decisions"], args.topic, args.site, scene)
    text = trace_markdown(record, data, source=source)
    if args.out:
        write_text(Path(args.out), text)
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


def recount_agreement(args) -> int:
    """`agreement`: summary.json's agreement block, recounted from the export's decisions and every
    labels.csv under live/, then the PII check on summary.json."""
    directory = Path(args.from_dir)
    data = load_export(directory)
    demos = set(data["summary"].get("demo_topics") or [])
    labels = read_labels(directory.parent)
    summary = data["summary"]
    summary.setdefault("human_control", {})["agreement"] = agreement(labels, data["decisions"], demos)
    path = directory / "summary.json"
    write_text(path, _json(summary))
    if _pii_check([str(path)]) != 0:
        return REFUSED
    print(json.dumps(summary["human_control"]["agreement"], indent=1))
    return 0


# --- main ----------------------------------------------------------------------------------------------


def _aws_options(parser) -> None:
    parser.add_argument(
        "--prefix", default=NAME_PREFIX, help=f"the resource name prefix (default {NAME_PREFIX})"
    )
    parser.add_argument("--region", default=HOME_REGION, help=f"the home Region (default {HOME_REGION})")
    parser.add_argument(
        "--vision-region", default=VISION_REGION, help=f"the vision worker's Region (default {VISION_REGION})"
    )
    parser.add_argument(
        "--logs-wait",
        type=float,
        default=LOGS_WAIT_SECONDS,
        metavar="SECONDS",
        help=f"how long each batch of Logs Insights queries is waited for (default {LOGS_WAIT_SECONDS:g})",
    )
    parser.add_argument(
        "--deploy-branch", default="dev", help="the branch whose terraform.yml runs deploy --env"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser(
        "export", help="write docs/hackathon/evidence/live/<UTC date>/ from the deployed system"
    )
    e.add_argument("--env", required=True, help="the environment (dev)")
    e.add_argument(
        "--topics", help="comma-separated topic ids (default: every topic that keeps decision evidence)"
    )
    e.add_argument("--since", help="the window's start, ISO (default: the logs' 90 days)")
    e.add_argument(
        "--networks",
        action="store_true",
        help="also write scripts/vision_benchmark_networks/<site>.json from the city topics' networks",
    )
    e.add_argument("--figures", type=int, default=0, metavar="N", help="write at most N figures (default 0)")
    e.add_argument(
        "--with-notes",
        action="store_true",
        help="add the agent's reasons, the news hosts and the Finding summaries (scrubbed, unverified)",
    )
    e.add_argument("--out", help="the directory to write (default: docs/hackathon/evidence/live/<UTC date>)")
    e.add_argument("--replace", action="store_true", help="write over an export already in that directory")
    _aws_options(e)
    t = sub.add_parser("trace", help="one decision's chain as Markdown, from an export or live")
    t.add_argument("--topic", required=True)
    t.add_argument("--site", help="the site (needed when the topic has several and the scene does not say)")
    t.add_argument("--scene", required=True, help="a scene id, or latest")
    t.add_argument(
        "--from", dest="from_dir", help="an export directory (docs/hackathon/evidence/live/<date>)"
    )
    t.add_argument("--env", help="read live from this environment instead")
    t.add_argument("--out", help="write the Markdown here instead of printing it")
    _aws_options(t)
    a = sub.add_parser("agreement", help="recount summary.json's agreement once labels.csv is filled in")
    a.add_argument("--from", dest="from_dir", required=True, help="the export directory")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            return export(args)
        if args.command == "trace":
            return trace(args)
        return recount_agreement(args)
    except Refused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main())
