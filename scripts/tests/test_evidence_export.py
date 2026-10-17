"""scripts/evidence_export.py: the evidence export, the trace and the agreement recount.

The evidence lines are the real ones. The real rail access adapter runs the synthetic city tick by
tick (scripts/vision_eval_driver.py, its scripted agent), with the worker handler answering in this
process under a Lambda request id of its own, for two topics: the rotating city topic and the demo
topic; every line printed is kept as CloudWatch keeps it, in its function's group, with the
research tick's `research_tick` line and the admin API's people's lines written by their own
emitters, and Lambda's REPORT lines beside them. A fake Logs Insights answers the export's queries
from those groups, filtering topics sloppily (a substring of the line) so the export's own equality
re-check is what keeps another topic's lines out, and planting `@log`, `@ptr` and an ARN in what it
returns. The running states (each topic's real final state), the figures, the networks and the
Findings, Articles and Topics tables are in moto. `gh` is a fake subprocess. Nothing touches the
network: requests, urllib and socket.connect refuse, and only moto answers boto3.

What is held: every file of the table and only schema fields in it; the planted `@log`, `@ptr`,
ARN and account id never reaching a file; a forged line reported as unmatched, and lines of another
topic or with a code the pipeline never writes dropped; a failed attempt's copy under `not_kept`;
the observed failures with their zero rows; E11; the worker by caller, build and CPU; the people's
decisions and the held share; the demo topic apart; `--with-notes`, `--networks` and `--figures`;
a topic with no state; queries that did not finish (said, never zero); a PII hit (exit 2, moved
aside); the provenance (functions without ARN or role, the deploy's commit, every line's group,
stream, time and query); `trace` from an export and live; the labels template that `index --check`
reads once filled in; and the query text, the kept rule and the redaction backstop themselves.
"""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
# common.* and ops_mcp.* (scripts/pyproject.toml puts only scripts/ on the path when a scripts test runs
# on its own), and lambdas/tests for its table schemas.
for _path in (ROOT / "lambdas", ROOT / "lambdas" / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from common import evidence, vision_client, vision_decisions  # noqa: E402
from ops_mcp import logs, vision_evidence  # noqa: E402
from table_schemas import create_table  # noqa: E402

import evidence_export as ee  # noqa: E402
import vision_eval  # noqa: E402
import vision_eval_driver as driver  # noqa: E402

REGION, VISION = "ap-southeast-2", "us-west-2"
TICK_GROUP = "/aws/lambda/bloggerbear-dev-research-tick"
ADMIN_GROUP = "/aws/lambda/bloggerbear-dev-admin-api"
WORKER_GROUP = "/aws/lambda/bloggerbear-dev-vision-worker"
BUCKET = "bloggerbear-dev-content"
CITIES, DEMO, EMPTY, OTHER = "rail-cities", "rail-sydney-demo", "rail-empty", "rail-cities-old"
CITY_SITE, DEMO_SITE = "sydney", "demo-sydney"
# The allowlisted test account id: planted where only the backstop or the schema can stop it.
ACCOUNT = "123456789012"
PLANTED_LOG = f"{ACCOUNT}:{TICK_GROUP}"
PLANTED_PTR = "CmAKJwojMTIzNDU2Nzg5MDEyOnBsYW50ZWQQABI1GhgCBkSdplanted"
PLANTED_ARN = f"arn:aws:iam::{ACCOUNT}:role/bloggerbear-dev-research-tick"
PROFILE_ARN = f"arn:aws:bedrock:{REGION}:{ACCOUNT}:application-inference-profile/abcdef123456"
FORGED_SCENE = "S2X_99XXX_20240718_0_L2A"
REASON_MARK = "Scripted answer, no model was asked"
SECRET = "Quentin Placeholder"
DEPLOY_SHA, LATER_SHA = "a" * 40, "b" * 40


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket
    import urllib.request

    import requests

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not touch the network")

    for name in ("get", "head", "put", "post", "request"):
        monkeypatch.setattr(requests, name, refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.delenv("PII_DENYLIST", raising=False)


# --- the pipeline, run for real on the synthetic city --------------------------------------------------


class _Lambda(driver._InProcessLambda):
    """The worker handler in this process, under a Lambda request id of its own (`ids`), which its
    vision_measure line carries and the invoke's response metadata hands the client."""

    def __init__(self, fetch, ids):
        super().__init__(fetch)
        self.ids = ids

    def invoke(self, FunctionName=None, InvocationType=None, Payload=b""):  # noqa: N803 - boto3's names
        import vision_worker_handler

        request_id = next(self.ids)
        reply = vision_worker_handler.lambda_handler(
            json.loads(Payload), SimpleNamespace(aws_request_id=request_id), fetch=self.fetch
        )
        return {
            "Payload": io.BytesIO(json.dumps(reply).encode("utf-8")),
            "ResponseMetadata": {"RequestId": request_id},
        }


def _measure(fetch, ids):
    client = _Lambda(fetch, ids)

    def measure(site, scene, backend="opencv", **kwargs):
        kwargs.pop("client", None)
        with driver.environment(
            {driver.WORKER_ENV[backend]: driver.IN_PROCESS_ARN, **driver.SYNTHETIC_ENVIRON}
        ):
            return vision_client.measure(site, scene, backend=backend, client=client, **kwargs)

    return measure


def _run_topic(topic_id: str, site_id: str, prefix: str):
    """The synthetic city's four ticks through the real adapter, as `topic_id` at `site_id`, each
    tick bound to ids of its own (`<prefix>-tick-<n>`, `<prefix>-run-<n>`), as the research tick
    binds a Lambda request id and a run."""
    site, scenes = driver.sequence_scenes("synthetic-city", None)
    network_a = driver.load_network_file(driver.SYNTHETIC_NETWORK_FILE)
    ids = (f"wrk-{prefix}-{n:03d}" for n in range(1, 100))
    real_bind = evidence.bind

    def bind(**fields):
        n = fields["tick"].rsplit("-", 1)[1]
        return real_bind(**{**fields, "tick": f"{prefix}-tick-{n}", "run": f"{prefix}-run-{n}"})

    with patch.object(driver, "SITE_PREFIX", ""), patch.object(evidence, "bind", bind):
        topic = driver.topic_config(
            "synthetic-city", site_id, site["polygon"], site_name="Synthetic city", thresholds="demo"
        )
        topic["topic_id"] = topic_id
        run = driver.Driver(
            topic,
            measure=_measure(driver.synthetic_fetch(), ids),
            agent=driver.ScriptedAgent(),
            clock=lambda n: driver.Clock.frozen(),
        )
        specs = driver.tick_specs(
            "synthetic-city", scenes, network_a, driver.line_on_largest_desert(network_a, run.site_id)
        )
        ticks = run.run(specs)
    return run, ticks


def _stamp(when: datetime) -> str:
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _report(request_id: str, *, ms=4200.0, memory=2048, used=700, status=None) -> str:
    text = (
        f"REPORT RequestId: {request_id}\tDuration: {ms:.2f} ms\tBilled Duration: {int(ms) + 1} ms\t"
        f"Memory Size: {memory} MB\tMax Memory Used: {used} MB\t"
    )
    return text + (f"Status: {status}" if status else "")


class Pipeline:
    """The two topics run, their states, and the three log groups as CloudWatch keeps them."""

    def __init__(self):
        self.groups = {TICK_GROUP: [], ADMIN_GROUP: [], WORKER_GROUP: []}
        self.city, city_ticks = _run_topic(CITIES, CITY_SITE, "c")
        self.demo, demo_ticks = _run_topic(DEMO, DEMO_SITE, "d")
        self.ticks = {CITIES: city_ticks, DEMO: demo_ticks}
        self.last_at = max(datetime.fromisoformat(t["at"]) for t in city_ticks + demo_ticks)
        self.now = (self.last_at + timedelta(days=2)).replace(microsecond=0)
        for topic_id, ticks in self.ticks.items():
            for tick in ticks:
                self._tick(topic_id, tick)
        self._failed_attempt(city_ticks[3])
        self._timed_out_attempt(city_ticks[1])
        self._forged(city_ticks[2])
        self._people()
        lost = self.last_at + timedelta(hours=3)
        self.add(WORKER_GROUP, lost, _report("wrk-lost-1", ms=60000.0, status="timeout"))
        self.add(WORKER_GROUP, lost + timedelta(seconds=1), _report("wrk-lost-2", used=2048, status="error"))

    def add(self, group: str, when: datetime, message: str, stream: str | None = None) -> None:
        stream = stream or f"{when:%Y/%m/%d}/[$LATEST]0123456789abcdef0123456789abcdef"
        self.groups[group].append(
            {
                "@timestamp": _stamp(when),
                "@logStream": stream,
                "@message": message,
                "@log": PLANTED_LOG,
                "@ptr": PLANTED_PTR,
            }
        )

    def _tick(self, topic_id: str, tick: dict, *, run_suffix: str = "", stored: bool = True, offset: int = 0):
        at = datetime.fromisoformat(tick["at"]) + timedelta(minutes=offset)
        events = []
        for k, line in enumerate(tick["lines"]):
            record = json.loads(line)
            if run_suffix and record["event"] == "vision_measure":
                continue  # a failed attempt's worker calls are the worker's own runs, already in its group
            if run_suffix and record.get("run"):
                line = line.replace(f'"run": "{record["run"]}"', f'"run": "{record["run"]}{run_suffix}"')
            group = WORKER_GROUP if record["event"] == "vision_measure" else TICK_GROUP
            self.add(group, at + timedelta(seconds=k), line)
            events.append(record)
            if record["event"] == "vision_measure" and not run_suffix:
                self.add(
                    WORKER_GROUP, at + timedelta(seconds=k, milliseconds=500), _report(record["request_id"])
                )
        bound = next(r for r in events if r.get("tick"))
        decided = [r for r in events if r["event"] == "vision_decision"]
        triages = sum(1 for r in events if r["event"] == "vision_triage")
        reported = any(r.get("reported") for r in decided)
        with evidence.bind(tick=bound["tick"], run=bound["run"] + run_suffix, topic_id=topic_id):
            line = evidence.line(
                "research_tick",
                adapter="rail_access",
                outcome="material_change" if reported else "no_change",
                stored=stored,
                decisions=len(decided),
                triages=triages,
                finding=tick["at"] if reported and stored else None,
                ms=4000 + 9000 * triages,
                error_type=None if stored else "RuntimeError",
            )
        self.add(TICK_GROUP, at + timedelta(seconds=len(events) + 1), line)

    def _failed_attempt(self, tick: dict) -> None:
        """Tick 4's first attempt: it decided, then failed before storing (`stored: false`)."""
        self._tick(CITIES, tick, run_suffix="f", stored=False, offset=-30)

    def _timed_out_attempt(self, tick: dict) -> None:
        """An attempt that timed out after its site's turn: a vision_site line and no research_tick
        line, and Lambda's REPORT with Status: timeout under its request id."""
        line = next(line for line in tick["lines"] if '"event": "vision_site"' in line)
        record = json.loads(line)
        line = line.replace(f'"tick": "{record["tick"]}"', '"tick": "c-tick-timeout"')
        line = line.replace(f'"run": "{record["run"]}"', '"run": "c-run-timeout"')
        when = datetime.fromisoformat(tick["at"]) + timedelta(hours=1)
        self.add(TICK_GROUP, when, line)
        self.add(
            TICK_GROUP,
            when + timedelta(minutes=15),
            _report("c-tick-timeout", ms=900000.0, memory=512, used=300, status="timeout"),
        )

    def _forged(self, tick: dict) -> None:
        """Three lines no pipeline wrote as such: a decision for a scene the ring never held, under a
        kept attempt (unmatched); a decision with a code the pipeline never writes (dropped); and a
        decision of another topic whose id the city topic's prefixes (dropped by equality)."""
        line = next(line for line in tick["lines"] if '"event": "vision_decision"' in line)
        record = json.loads(line)
        at = datetime.fromisoformat(tick["at"])
        self.add(
            TICK_GROUP,
            at + timedelta(seconds=30),
            line.replace(record["scene_id"], FORGED_SCENE),
            stream=f"{at:%Y/%m/%d}/[$LATEST]{PLANTED_ARN}",
        )
        self.add(
            TICK_GROUP,
            at + timedelta(seconds=31),
            line.replace(f'"code": "{record["code"]}"', '"code": "approved_by_me"'),
        )
        self.add(
            TICK_GROUP,
            at + timedelta(seconds=32),
            line.replace(f'"topic_id": "{CITIES}"', f'"topic_id": "{OTHER}"'),
        )

    def _people(self) -> None:
        import contextlib

        import admin_api_handler

        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            with evidence.bind(tick="api-0001", api_request_id="apigw-0001"):
                admin_api_handler._moderation_decision(
                    "approved",
                    topic_id=CITIES,
                    article_id="art-cities-1",
                    queue_id="q-1",
                    from_status="pending_moderation",
                )
            with evidence.bind(tick="api-0002", api_request_id="apigw-0002"):
                admin_api_handler._moderation_decision(
                    "rejected",
                    topic_id=DEMO,
                    article_id="art-demo-1",
                    queue_id="q-2",
                    from_status="pending_moderation",
                )
            with evidence.bind(tick="api-0003", api_request_id="apigw-0003"):
                admin_api_handler._topic_changed(
                    "updated",
                    {"topic_id": CITIES, "adapter_config": {"reach_m": 1000}},
                    {"topic_id": CITIES, "adapter_config": {"reach_m": 800, "triage": True}},
                )
        for k, line in enumerate(printed.getvalue().splitlines()):
            self.add(ADMIN_GROUP, self.last_at + timedelta(days=1, minutes=k), line)

    def state(self, topic_id: str) -> dict:
        state = json.loads(json.dumps((self.city if topic_id == CITIES else self.demo).state))
        if topic_id == CITIES:
            ring = state["sites"][CITY_SITE]["decisions"]
            triaged = [entry for entry in ring if entry.get("triage")]
            triaged[0]["triage"]["model_id"] = PROFILE_ARN  # an ARN the schema must never let through
        return state

    def ring(self, topic_id: str) -> list[dict]:
        site = CITY_SITE if topic_id == CITIES else DEMO_SITE
        return self.state(topic_id)["sites"][site]["decisions"]


@pytest.fixture(scope="module")
def pipeline():
    return Pipeline()


# --- the fakes: Logs Insights, Lambda, gh --------------------------------------------------------------


class FakeLogs:
    """Logs Insights over the Pipeline's groups. It reads each query's events and topic ids from its
    text but matches a topic as a substring of the line (careless, on purpose); REPORT queries
    return the group's REPORT lines (only those with a status when the query asks for it); rows
    within the query's window, newest first, at most its limit, each with @log and @ptr planted.
    `unfinished` kinds (by an event name or "REPORT" in the text) stay Running for ever."""

    def __init__(self, pipeline, unfinished=()):
        self.pipeline, self.unfinished = pipeline, set(unfinished)
        self.queries: dict[str, dict] = {}
        self.regions: list[tuple[str, str]] = []
        self.stopped: list[str] = []

    def client(self, region: str):
        fake = self

        class Client:
            def start_query(self, **kwargs):
                group = kwargs["logGroupName"]
                fake.regions.append((region, group))
                assert region == (VISION if group == WORKER_GROUP else REGION), (region, group)
                query_id = f"00000000-0000-4000-8000-{len(fake.queries):012x}"
                fake.queries[query_id] = kwargs
                return {"queryId": query_id}

            def get_query_results(self, queryId):  # noqa: N803
                kwargs = fake.queries[queryId]
                text = kwargs["queryString"]
                if any(mark in text for mark in fake.unfinished):
                    return {"status": "Running", "results": []}
                rows = fake.rows(kwargs)
                return {
                    "status": "Complete",
                    "statistics": {"recordsMatched": len(rows), "bytesScanned": 1024.0},
                    "results": [[{"field": k, "value": v} for k, v in row.items()] for row in rows],
                }

            def stop_query(self, queryId):  # noqa: N803
                fake.stopped.append(queryId)

        return Client()

    def rows(self, kwargs) -> list[dict]:
        text, group = kwargs["queryString"], kwargs["logGroupName"]
        start, end = kwargs["startTime"], kwargs["endTime"]
        out = []
        for row in self.pipeline.groups[group]:
            when = (
                datetime.strptime(row["@timestamp"], "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC).timestamp()
            )
            if not start <= when <= end + 1:
                continue
            message = row["@message"]
            if '@type = "REPORT"' in text:
                if not message.startswith("REPORT") or ("Status: " in text and "Status: " not in message):
                    continue
                request_id = re.search(r"RequestId: (\S+)", message).group(1)
                out.append({**row, "@requestId": request_id})
                continue
            if not message.startswith(evidence.PREFIX):
                continue
            events = re.findall(r'"(vision_\w+|research_tick|moderation_decision|topic_changed)"', text)
            if json.loads(message)["event"] not in events:
                continue
            one = re.search(r'topic_id = "([^"]+)"', text)
            many = re.search(r"topic_id in \[([^\]]+)\]", text)
            ids = [one.group(1)] if one else re.findall(r'"([^"]+)"', many.group(1)) if many else None
            if ids is not None and not any(each in message for each in ids):
                continue
            out.append(dict(row))
        out.sort(key=lambda row: row["@timestamp"], reverse=True)
        return out[: kwargs["limit"]]


class FakeLambda:
    """GetFunctionConfiguration, with the ARN and the role a real answer carries."""

    def __init__(self, refused=()):
        self.refused = set(refused)

    def get_function_configuration(self, FunctionName):  # noqa: N803
        if FunctionName in self.refused:
            from botocore.exceptions import ClientError

            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": f"{PLANTED_ARN} is not"}},
                "GetFunctionConfiguration",
            )
        return {
            "FunctionName": FunctionName,
            "FunctionArn": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{FunctionName}",
            "Role": PLANTED_ARN,
            "CodeSha256": "c2hhMjU2LW9mLXRoZS1jb2Rl",
            "LastModified": "2024-07-28T00:00:00.000+0000",
            "Version": "$LATEST",
            "Runtime": "python3.11",
            "Architectures": ["arm64"],
            "MemorySize": 2048,
            "Timeout": 900,
        }


def fake_gh(command, **kwargs):
    assert command[:3] == ["gh", "run", "list"] and "terraform.yml" in command and "dev" in command
    runs = [
        {"headSha": LATER_SHA, "createdAt": "2024-07-28T06:00:00Z", "conclusion": "success"},
        {"headSha": DEPLOY_SHA, "createdAt": "2024-07-27T23:50:00Z", "conclusion": "success"},
        {"headSha": "c" * 40, "createdAt": "2024-07-20T00:00:00Z", "conclusion": "failure"},
    ]
    return subprocess.CompletedProcess(command, 0, stdout=json.dumps(runs), stderr="")


class FakeAws(ee.Aws):
    """The export's clients: moto's S3 and DynamoDB, the fake Logs and Lambda."""

    def __init__(self, fake_logs, fake_lambda=None):
        super().__init__(REGION, VISION)
        self.fake_logs, self.fake_lambda = fake_logs, fake_lambda or FakeLambda()

    def logs(self, region):
        return self.fake_logs.client(region)

    def lambda_(self, region):
        return self.fake_lambda


# --- moto: the states, figures, networks and tables ----------------------------------------------------


def _ddb(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


@pytest.fixture
def aws(pipeline, monkeypatch, tmp_path):
    from moto import mock_aws

    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(ee, "NETWORKS_DIR", tmp_path / "networks")
    with mock_aws():
        import boto3

        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
        for topic_id, run in ((CITIES, pipeline.city), (DEMO, pipeline.demo)):
            s3.put_object(
                Bucket=BUCKET,
                Key=f"snapshots/{topic_id}/running-state.json",
                Body=json.dumps(pipeline.state(topic_id)).encode(),
            )
            site = run.site_id
            for entry in run.state["sites"][site]["history"]:
                if entry.get("image_key"):
                    s3.put_object(
                        Bucket=BUCKET, Key=entry["image_key"], Body=run.store.load_image(entry["image_key"])
                    )
            network = run.store.load_network(topic_id, site)
            s3.put_object(
                Bucket=BUCKET,
                Key=f"vision/{topic_id}/{site}/network.json",
                Body=json.dumps({**network, "site_key": "abc"}).encode(),
            )
        ddb = boto3.client("dynamodb", region_name=REGION)
        for name, keys in (
            ("bloggerbear-dev-topics", ["topic_id"]),
            ("bloggerbear-dev-findings", ["topic_id", "captured_at"]),
            ("Articles", ["article_id"]),
        ):
            create_table(
                ddb,
                TableName=name,
                BillingMode="PAY_PER_REQUEST",
                KeySchema=[
                    {"AttributeName": k, "KeyType": t} for k, t in zip(keys, ("HASH", "RANGE"), strict=False)
                ],
                AttributeDefinitions=[{"AttributeName": k, "AttributeType": "S"} for k in keys],
            )
        monkeypatch.setattr(ee, "resource_names", _names_with_articles)
        resource = boto3.resource("dynamodb", region_name=REGION)
        topics = resource.Table("bloggerbear-dev-topics")
        for item in (
            {"topic_id": CITIES, "adapter": "rail_access"},
            {"topic_id": DEMO, "adapter": "rail_access"},
            {"topic_id": EMPTY, "adapter": "rail_access"},
            {"topic_id": "crypto-investing", "adapter": "crypto_feed"},
        ):
            topics.put_item(Item=item)
        findings = resource.Table("bloggerbear-dev-findings")
        articles = resource.Table("Articles")
        for topic_id, run in ((CITIES, pipeline.city), (DEMO, pipeline.demo)):
            for entry in run.state["sites"][run.site_id]["decisions"]:
                if not entry["reported"]:
                    continue
                key = next(
                    (
                        h.get("image_key")
                        for h in run.state["sites"][run.site_id]["history"]
                        if h["scene_id"] == entry["scene_id"]
                    ),
                    None,
                )
                findings.put_item(
                    Item=_ddb(
                        {
                            "topic_id": topic_id,
                            "captured_at": entry["at"],
                            "expires_at": 1800000000,
                            "summary": f"The served share moved. Written by {SECRET} for the test.",
                            "figures": [{"key": key, "caption": "c", "alt": "a"}] if key else [],
                            "planner_notes": [
                                {"kind": "flag", "type": "single_point_of_failure", "text": "x"}
                            ],
                            "research_call": {
                                "model_id": "au.anthropic.claude-haiku-4-5-20251001-v1:0",
                                "input_tokens": 1000,
                                "output_tokens": 200,
                                "used_fallback": False,
                            },
                            "subjects": [run.site_id],
                            "raw_snapshot_s3_key": "snapshots/x.json",
                            "source_refs": [],
                        }
                    )
                )
        city_ats = [e["at"] for e in pipeline.ring(CITIES) if e["reported"]]
        demo_ats = [e["at"] for e in pipeline.ring(DEMO) if e["reported"]]
        day = timedelta(days=1)
        for article_id, topic_id, ats, status, by in (
            ("art-cities-1", CITIES, city_ats, "published", "humans"),
            ("art-demo-1", DEMO, demo_ats, "rejected", None),
        ):
            created = datetime.fromisoformat(ats[-1]) + day / 2
            articles.put_item(
                Item=_ddb(
                    {
                        "article_id": article_id,
                        "topic_id": topic_id,
                        "status": status,
                        "published_by": by,
                        "created_at": created.isoformat(),
                        "published_at": (created + day / 4).isoformat() if by else None,
                        "title": f"by {SECRET}",
                        "body_s3_key": "articles/x.md",
                        "lineage": {
                            "research": {"calls": [{"stage": "research", "captured_at": at} for at in ats]}
                        },
                    }
                )
            )
        yield FakeAws(FakeLogs(pipeline))


_REAL_NAMES = ee.resource_names


def _names_with_articles(prefix: str, env: str) -> dict:
    """The Articles table under the name table_schemas gives its topic index (moto refuses a Query
    on an index the table was not created with)."""
    return {**_REAL_NAMES(prefix, env), "articles": "Articles"}


def _args(out: Path, *extra: str) -> list[str]:
    return ["export", "--env", "dev", "--out", str(out), "--logs-wait", "5", *extra]


def run_export(aws, out: Path, *extra: str, now=None, gh=fake_gh, pii=None) -> int:
    parser_args = _parse(_args(out, *extra))
    return ee.export(
        parser_args, aws, now=now or aws.fake_logs.pipeline.now, gh=gh, sleep=lambda s: None, pii=pii
    )


def _parse(argv: list[str]):
    """main's parser, without running: the export's own argument names and defaults."""
    captured = {}

    def grab(args, *a, **k):
        captured["args"] = args
        return 0

    with patch.object(ee, "export", grab), patch.object(ee, "trace", grab):
        ee.main(argv)
    return captured["args"]


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def every_text(directory: Path) -> str:
    return "\n".join(
        p.read_text(encoding="utf-8", errors="replace")
        for p in sorted(directory.rglob("*"))
        if p.is_file() and p.suffix != ".png"
    )


@pytest.fixture
def exported(aws, tmp_path):
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out) == 0
    return out


# --- the export ----------------------------------------------------------------------------------------


def test_the_export_writes_every_file_of_the_table_and_the_credits(exported):
    for name in ee.FILES:
        assert (exported / name).is_file(), name
    readme = (exported.parent / "README.md").read_text(encoding="utf-8")
    assert "© OpenStreetMap contributors" in readme and "Open Database Licence" in readme
    assert "Copernicus Sentinel data" in readme
    for path in exported.rglob("*"):
        if path.is_file():
            assert b"\r\n" not in path.read_bytes(), path  # \n line ends, on Windows too


def test_the_planted_log_ptr_arn_and_account_never_reach_a_file(exported):
    text = every_text(exported) + every_text(exported.parent)
    for planted in (PLANTED_LOG, PLANTED_PTR, ACCOUNT, "arn:", "@log", "@ptr", PROFILE_ARN):
        assert planted not in text, planted
    summary = json.loads((exported / "summary.json").read_text(encoding="utf-8"))
    assert summary["redacted"] >= 1  # the planted log stream's ARN, caught by the backstop
    provenance = (exported / "provenance.json").read_text(encoding="utf-8")
    assert '"log_stream": "' in provenance and "[$LATEST][redacted]" in provenance


def test_only_schema_fields_are_written_and_no_model_or_third_party_text(exported, pipeline):
    text = every_text(exported)
    assert REASON_MARK not in text and SECRET not in text and "Synthetic city" not in text
    for record in read_jsonl(exported / "decisions.jsonl"):
        assert "note" not in record and "hosts" not in record
        assert set(record) <= {
            "topic_id",
            "site_id",
            "source",
            "kept",
            *ee.ENTRY_KINDS,
            "triage",
            "measured_by",
            "logged",
            "not_kept",
            "finding_written",
        }
        assert "reason" not in (record.get("triage") or {})
    for line in read_jsonl(exported / "triage.jsonl"):
        assert set(line) <= {"event", *evidence.COMMON, *evidence.EVENTS["vision_triage"], "logged"}
    for name in ("ticks.jsonl", "people.jsonl"):
        for line in read_jsonl(exported / name):
            assert set(line) <= {"event", *evidence.COMMON, *evidence.EVENTS[line["event"]], "logged"}
    history = json.loads((exported / "history.json").read_text(encoding="utf-8"))
    entry = history[CITIES]["sites"][CITY_SITE]["history"][-1]
    for gone in ("stations", "flags", "suggestions", "deserts", "hubs", "assets", "image_key", "network"):
        assert gone not in entry
    assert entry["scene_id"] and entry["build"] and "served_share" in entry
    articles = json.loads((exported / "articles.json").read_text(encoding="utf-8"))["articles"]
    assert {a["article_id"] for a in articles} == {"art-cities-1", "art-demo-1"}
    assert all(
        set(a)
        == {
            "article_id",
            "topic_id",
            "status",
            "published_by",
            "created_at",
            "published_at",
            "research_captured_at",
        }
        for a in articles
    )
    findings = json.loads((exported / "findings.json").read_text(encoding="utf-8"))
    assert findings["findings"] and all("summary" not in f for f in findings["findings"])
    assert findings["findings"][0]["planner_notes"] == {"single_point_of_failure": 1}
    assert (
        findings["findings"][0]["research_call"]["model_id"] == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_the_ring_is_written_kept_with_the_log_only_decisions_after_and_not_kept_copies(exported, pipeline):
    records = read_jsonl(exported / "decisions.jsonl")
    city = [r for r in records if r["topic_id"] == CITIES]
    from_state = [r for r in city if r["source"] == "state"]
    assert [r["scene_id"] for r in from_state] == [e["scene_id"] for e in pipeline.ring(CITIES)]
    assert [r["code"] for r in from_state] == ["first_observation", "below_floor", "artefact", "reported"]
    assert all(r["kept"] for r in from_state)
    # The ARN planted as a triage's model id in the state is not written.
    assert [r["triage"].get("model_id") for r in from_state if r["triage"]] == [None, "scripted"]
    # The forged decision is only in the logs: written as such, after the ring.
    forged = [r for r in city if r["scene_id"] == FORGED_SCENE]
    assert (
        len(forged) == 1
        and forged[0]["source"] == "logs"
        and records.index(forged[0]) > records.index(from_state[-1])
    )
    # Tick 4's failed first attempt is its not-kept copy.
    last = from_state[-1]
    assert [(c["run"], c["why"]) for c in last["not_kept"]] == [("c-run-4f", "stored_false")]
    assert last["finding_written"] is True and from_state[2]["finding_written"] is False
    assert {r["topic_id"] for r in records} == {CITIES, DEMO}  # never the topic that only prefixes it


def test_the_summary_counts_the_cities_and_the_demo_apart_with_zero_rows(exported):
    summary = json.loads((exported / "summary.json").read_text(encoding="utf-8"))
    assert summary["demo_topics"] == [DEMO]
    decisions = summary["decisions"]
    assert decisions["demo"]["by_code"] == {
        "first_observation": 1,
        "below_floor": 1,
        "baseline_building": 0,
        "within_range": 0,
        "reported": 1,
        "artefact": 1,
    }
    assert decisions["cities"]["by_code"]["artefact"] == 2  # the ring's and the forged copy's
    assert decisions["cities"]["triaged"] == 3 and decisions["demo"]["triaged"] == 2
    assert decisions["cities"]["not_kept_copies"] == 1 and decisions["complete"] is True
    failures = summary["observed_failures"]
    city = failures["cities"]
    assert (
        city["site_turns_by_outcome"]["measured"] >= 4 and city["site_turns_by_outcome"]["not_reached"] == 0
    )
    assert city["site_turns_by_code"]["unreadable_scene"] == 0 and city["site_turns_by_code"]["osm"] == 0
    assert city["ticks_stored_false"] == 1 and city["attempts_without_research_tick_line"] == 1
    assert set(city["triage_stops"]) >= {"answered", "deadline", "bedrock_error", "no_model"}
    assert city["triage_stops"]["deadline"] == 0 and city["triage_failed"] == 0
    assert set(city["network_errors_now"]) >= {"timeout", "http", "rate_limited"}
    worker = failures["worker"]
    assert set(worker["refusals_by_code"]["production"]) >= {
        "bad_request",
        "unreadable_scene",
        "window_too_large",
    }
    assert all(n == 0 for n in worker["refusals_by_code"]["production"].values())
    assert worker["ended_without_a_reply"] == {"timeout": 1, "out_of_memory": 1, "error": 0, "other": 0}
    assert failures["research_tick"]["timeouts"] == 1
    e11 = summary["e11_tick_time"]
    assert e11["cities"]["with_agent"]["n"] == 3 and e11["cities"]["with_agent"]["p50"] == 13000
    assert e11["cities"]["measured_without_agent"]["n"] == 2 and e11["demo"]["with_agent"]["n"] == 2
    rows = summary["worker"]["rows"]
    assert {(r["caller"], r["runs"]) for r in rows} >= {("production", 6), ("demo", 6)}
    production = next(r for r in rows if r["caller"] == "production")
    assert production["build"] and production["cpu"] and production["total_ms"]["p95"] is not None
    assert production["billed_ms"]["n"] == production["runs"]
    human = summary["human_control"]
    assert human["cities"]["published"] == 1 and human["cities"]["held_share"] == 1.0
    assert human["cities"]["decisions"]["approved"] == 1 and human["demo"]["decisions"]["rejected"] == 1
    assert human["cities"]["decisions"]["sent_back"] == 0
    assert human["cities"]["hours_from_finding_to_decision"]["approved"]["n"] == 1
    assert human["worker_lines_with_build_and_cpu"]["share"] == 1.0
    assert human["agreement"]["cities"] == {
        "triaged": 3,
        "labelled": 0,
        "agree": 0,
        "share": None,
        "wilson_low": None,
        "wilson_high": None,
        "disagree_reasons": {},
    }
    spend = summary["spend"]
    assert spend["rate"] == 1.5 and spend["cities"]["research_usd"] > 0
    assert spend["cities"]["research_aud"] == pytest.approx(spend["cities"]["research_usd"] * 1.5, abs=1e-6)


def test_the_forged_line_is_unmatched_and_the_others_are_dropped(exported):
    summary = json.loads((exported / "summary.json").read_text(encoding="utf-8"))
    complete = summary["human_control"]["completeness"]
    unmatched = complete["unmatched"]
    assert [(u["kind"], u["scene_id"]) for u in unmatched] == [("vision_decision", FORGED_SCENE)]
    assert unmatched[0]["from"]["log_group"] == TICK_GROUP and unmatched[0]["from"]["query"]
    assert complete["decision_lines"]["with_ring_entry"] >= 8 and complete["triage_lines"]["unmatched"] == 0
    workers = complete["worker_request_ids"]
    assert workers["request_ids"] > 0 and workers["with_vision_measure"] == workers["request_ids"]
    assert summary["logs"]["decisions"]["dropped"] == 2  # the unknown code, and the other topic


def test_provenance_names_each_lines_group_stream_time_and_query_and_the_deployed_code(exported):
    provenance = json.loads((exported / "provenance.json").read_text(encoding="utf-8"))
    for key, config in provenance["functions"].items():
        assert "FunctionArn" not in config and "Role" not in config
        assert config["CodeSha256"] and config["LastModified"] and config["deploy"]["head_sha"] == DEPLOY_SHA
    assert provenance["functions"]["vision_worker"]["region"] == VISION
    assert provenance["functions"]["research_tick"]["FunctionName"] == "bloggerbear-dev-research-tick"
    ticks = provenance["lines"]["ticks.jsonl"]
    assert ticks and all(
        row["log_group"] == TICK_GROUP and row["query"] in provenance["queries"] for row in ticks
    )
    query = provenance["queries"][ticks[0]["query"]]
    assert query["query_id"] and query["state"] == "complete" and query["run_ms"] is not None
    assert query["region"] == REGION and query["log_group"] == TICK_GROUP
    decisions = provenance["lines"]["decisions.jsonl"]
    assert (
        decisions[0]["from"] == "state"
        and decisions[0]["state"]["key"] == f"snapshots/{CITIES}/running-state.json"
    )
    assert any(row.get("log") for row in decisions)
    worker = provenance["lines"]["worker.jsonl"]
    assert all(row["log_group"] == WORKER_GROUP for row in worker)
    assert provenance["exporter"]["script"] == "scripts/evidence_export.py"


def test_the_worker_file_joins_each_report_and_lists_the_runs_that_ended_without_a_reply(exported):
    lines = read_jsonl(exported / "worker.jsonl")
    measured = [line for line in lines if line["event"] == "vision_measure"]
    assert measured and all(line["billed_ms"] and line["max_memory_mb"] == 700 for line in measured)
    assert {line["caller"] for line in measured} == {"production", "demo"}
    lost = [line for line in lines if line.get("ended_without_a_reply")]
    assert [(line["request_id"], line["cause"]) for line in lost] == [
        ("wrk-lost-2", "out_of_memory"),
        ("wrk-lost-1", "timeout"),
    ]
    assert all("message" not in line and "@message" not in line for line in lines)


def test_the_labels_template_is_one_row_per_triaged_decision_and_index_check_reads_it_filled(
    exported, tmp_path
):
    text = (exported / ee.TEMPLATE_FILE).read_text(encoding="utf-8")
    comments = [line for line in text.splitlines() if line.startswith("#")]
    assert comments and all(code in " ".join(comments) for code in vision_eval.LABEL_REASONS)
    rows = list(csv.DictReader(line for line in text.splitlines() if not line.startswith("#")))
    assert list(rows[0]) == list(vision_eval.LABEL_COLUMNS)
    # Every triaged live decision: the forged one's too (the logs hold it; completeness lists it).
    assert sorted(row["decision"].split("/")[0] for row in rows) == [CITIES, CITIES, CITIES, DEMO, DEMO]
    assert {row["verdict"] for row in rows} <= {"real", "artefact"} and all(row["trace"] for row in rows)
    for row in rows:
        assert (exported / row["trace"]).is_file()
        row["label"], row["reason"] = "agree", "looks_real"
    evidence_dir = tmp_path / "evidence"
    (evidence_dir / "live" / "2024-07-29").mkdir(parents=True)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=vision_eval.LABEL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    (evidence_dir / "live" / "2024-07-29" / "labels.csv").write_text(buffer.getvalue(), encoding="utf-8")
    problems, read = vision_eval.label_problems(evidence_dir)
    assert problems == [] and read == 5


def test_agreement_is_recounted_once_the_labels_are_filled_in(exported):
    text = (exported / ee.TEMPLATE_FILE).read_text(encoding="utf-8")
    rows = list(csv.DictReader(line for line in text.splitlines() if not line.startswith("#")))
    for n, row in enumerate(rows):
        row["label"], row["reason"] = ("agree", "looks_real") if n else ("disagree", "cloud_or_haze")
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=vision_eval.LABEL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    (exported / "labels.csv").write_text(buffer.getvalue(), encoding="utf-8")
    assert ee.main(["agreement", "--from", str(exported)]) == 0
    agreement = json.loads((exported / "summary.json").read_text(encoding="utf-8"))["human_control"][
        "agreement"
    ]
    total = agreement["cities"]["labelled"] + agreement["demo"]["labelled"]
    agreed = agreement["cities"]["agree"] + agreement["demo"]["agree"]
    assert (total, agreed) == (5, 4)
    group = "cities" if rows[0]["decision"].startswith(CITIES + "/") else "demo"
    assert agreement[group]["disagree_reasons"] == {"cloud_or_haze": 1}
    assert (
        agreement[group]["wilson_low"] is not None
        and agreement[group]["wilson_low"] < agreement[group]["share"]
    )


def test_with_notes_adds_the_reasons_and_summaries_scrubbed_and_labelled(aws, tmp_path):
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, "--with-notes") == 0
    noted = [r for r in read_jsonl(out / "decisions.jsonl") if r.get("note")]
    assert noted and all(
        r["note"]["label"] == ee.NOTE_LABEL and REASON_MARK in r["note"]["text"] for r in noted
    )
    findings = json.loads((out / "findings.json").read_text(encoding="utf-8"))["findings"]
    assert all(f["summary"]["label"] == ee.SUMMARY_LABEL for f in findings)
    trace = next((out / "traces").glob("*.md")).read_text(encoding="utf-8")
    assert "The agent's note (unverified, model text)" in trace


def test_networks_and_figures_are_written_in_their_shapes(aws, tmp_path):
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, "--networks", "--figures", "2") == 0
    networks = sorted((tmp_path / "networks").glob("*.json"))
    assert [p.name for p in networks] == [f"{CITY_SITE}.json"]  # the demo topic's is not the fixture
    fixture = json.loads(networks[0].read_text(encoding="utf-8"))
    assert set(fixture) <= set(ee.NETWORK_FIELDS) and fixture["ways"] and "site_key" not in fixture
    figures = sorted((out / "figures").glob("*.png"))
    assert len(figures) == 2 and all(p.read_bytes().startswith(b"\x89PNG") for p in figures)
    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert all(v["key"].startswith("vision/") for v in provenance["figures"].values())
    assert list(provenance["networks"].values())[0]["from"] == f"vision/{CITIES}/{CITY_SITE}/network.json"
    template = (out / ee.TEMPLATE_FILE).read_text(encoding="utf-8")
    assert sum(1 for name in template.split(",") if name.startswith("figures/")) == 2


def test_a_topic_with_no_state_is_said_and_the_rest_still_exported(aws, tmp_path):
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, "--topics", f"{CITIES},{EMPTY}") == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    states = {t["topic_id"]: t["state"] for t in summary["topics"]}
    assert states == {CITIES: "read", EMPTY: "missing"}
    assert not any(r["topic_id"] == DEMO for r in read_jsonl(out / "decisions.jsonl"))


def test_logs_that_did_not_finish_are_said_never_counted_as_zero(aws, tmp_path):
    aws.fake_logs.unfinished = {"vision_measure", "REPORT"}
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, "--logs-wait", "0.05") == 0
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    for kind in ("measures", "worker_reports", "tick_reports"):
        assert summary["logs"][kind]["state"] == "not_checked" and summary["logs"][kind]["missing"]
    assert summary["worker"]["complete"] is False and summary["observed_failures"]["complete"] is False
    workers = summary["human_control"]["completeness"]["worker_request_ids"]
    assert workers["not_checked"] == workers["request_ids"] > 0 and workers["unmatched"] == 0
    assert summary["decisions"]["complete"] is True
    assert aws.fake_logs.stopped  # the unfinished queries were stopped, so they cost no more
    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert {q["state"] for q in provenance["queries"].values() if q["kind"] == "measures"} == {"not_checked"}


def test_a_pii_hit_refuses_the_export_names_the_file_and_line_and_moves_it_aside(
    aws, tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("PII_DENYLIST", SECRET)
    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, "--with-notes", "--networks") == 2
    err = capsys.readouterr().err
    assert re.search(r"findings\.json:\d+ contains denylist entry #1", err) and SECRET not in err
    assert not out.exists() and (tmp_path / "live" / "2024-07-29.refused" / "summary.json").is_file()
    assert not list((tmp_path / "networks").glob("*.json"))  # never left in the tracked directory
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "docs/hackathon/evidence/live/*.refused/" in ignored


def test_without_gh_the_deploy_is_null_with_its_reason_and_the_export_finishes(aws, tmp_path):
    def missing(command, **kwargs):
        raise FileNotFoundError("gh")

    out = tmp_path / "live" / "2024-07-29"
    assert run_export(aws, out, gh=missing) == 0
    provenance = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    deploy = provenance["functions"]["research_tick"]["deploy"]
    assert deploy["head_sha"] is None and deploy["reason"] == "gh is not installed"


def test_usage_is_refused_before_anything_is_read(aws, tmp_path, capsys):
    out = tmp_path / "live" / "2024-07-29"
    assert ee.main(_args(out, "--since", "2999-01-01")) == 2
    assert ee.main(_args(out, "--topics", 'rail" or topic_id = "x')) == 2
    assert not aws.fake_logs.queries
    out.mkdir(parents=True)
    (out / "summary.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ee.Refused, match="pass --replace"):  # main says it and exits 2
        run_export(aws, out)
    assert run_export(aws, out, "--replace") == 0
    assert json.loads((out / "summary.json").read_text(encoding="utf-8"))["schema"] == ee.SCHEMA


# --- trace ---------------------------------------------------------------------------------------------


def test_trace_from_an_export_prints_the_chain_with_the_persons_step(exported, capsys):
    assert ee.main(["trace", "--topic", CITIES, "--scene", "latest", "--from", str(exported)]) == 0
    text = capsys.readouterr().out
    assert text.startswith(f"# Trace: {CITIES} / {CITY_SITE} / ")
    assert "| " + " | ".join(vision_evidence.TRACE_COLUMNS) + " |" in text
    assert "a Finding was written" in text and "approved by a person" in text
    assert "re-measured the same scene at other settings" in text
    assert "Measured and decided 1 more time(s) by ticks that failed" in text
    assert "© OpenStreetMap contributors" in text


def test_trace_takes_a_scene_id_and_a_site_and_refuses_what_it_cannot_tell_apart(exported, pipeline, capsys):
    scene = pipeline.ring(CITIES)[2]["scene_id"]
    assert (
        ee.main(["trace", "--topic", CITIES, "--site", CITY_SITE, "--scene", scene, "--from", str(exported)])
        == 0
    )
    text = capsys.readouterr().out
    assert "changed, and the triage agent judged it an artefact" in text and "no Finding" in text
    records = [
        {"topic_id": "t", "site_id": site, "scene_id": "s1", "kept": True, "at": "2024-01-01T00:00:00+00:00"}
        for site in ("a", "b")
    ]
    with pytest.raises(ee.Refused, match="--site"):
        ee.pick(records, "t", None, "latest")
    assert ee.pick(records, "t", "b", "s1")["site_id"] == "b"
    assert ee.main(["trace", "--topic", CITIES, "--scene", "S2Z_NOPE", "--from", str(exported)]) == 2


def test_trace_live_reads_the_logs_and_state_without_writing_anything(aws, pipeline, tmp_path, capsys):
    argv = ["trace", "--env", "dev", "--topic", DEMO, "--scene", "latest", "--logs-wait", "5"]
    args = _parse(argv)
    assert ee.trace(args, aws, now=pipeline.now, sleep=lambda s: None) == 0
    text = capsys.readouterr().out
    assert (
        text.startswith(f"# Trace: {DEMO} / {DEMO_SITE} / ")
        and "the live logs and running state of dev" in text
    )
    assert "rejected by a person" in text
    assert all(
        f'"{DEMO}"' in q["queryString"]
        for q in aws.fake_logs.queries.values()
        if "topic_id" in q["queryString"]
    )


# --- the parts -----------------------------------------------------------------------------------------


def test_the_queries_are_the_assistants_text_with_the_stream_added():
    texts = ee.queries([CITIES, DEMO])
    base = vision_evidence.queries([CITIES, DEMO])
    assert texts["decisions"][1] == base["decisions"].replace(
        "fields @timestamp, @message\n", "fields @timestamp, @logStream, @message\n"
    )
    assert texts["ticks"][1].replace("@logStream, ", "") == base["attempts"].replace(" and decisions > 0", "")
    assert '["moderation_decision", "topic_changed"]' in texts["people"][1] and texts["people"][0] == "admin"
    assert "topic_id" not in texts["measures"][1] and texts["measures"][0] == "worker"
    assert 'event = "vision_site"' in texts["sites"][1]
    for kind, (_group, text) in texts.items():
        assert "@log," not in text and "@ptr" not in text and f"limit {logs.QUERY_LIMIT}" in text
        if "REPORT" not in text:
            assert (
                " like " not in text and f'topic_id in ["{CITIES}", "{DEMO}"]' in text or kind == "measures"
            )
    with pytest.raises(ValueError):
        ee._derive("fields x", "absent", "y")


def test_logs_are_split_while_cut_read_once_and_said_not_checked_when_unfinished(monkeypatch):
    monkeypatch.setattr(logs, "QUERY_LIMIT", 4)
    since = datetime(2026, 10, 1, tzinfo=UTC)
    stamps = [since + timedelta(hours=n * 7) for n in range(10)]
    calls = []

    def run(jobs, *, client, wait_seconds, sleep, label, recover_throttles):
        assert recover_throttles and wait_seconds == 30.0
        out = {}
        for key, _region, _group, _text, start, end in jobs:
            calls.append((start, end))
            if key[0] == "never":
                out[key] = None
                continue
            rows = [
                {"@timestamp": _stamp(s), "@logStream": "s", "@message": f"m{n}", "@log": PLANTED_LOG}
                for n, s in enumerate(stamps)
                if start <= s <= end
            ]
            out[key] = sorted(rows, key=lambda r: r["@timestamp"], reverse=True)[: logs.QUERY_LIMIT]
        return out

    specs = {"lines": (REGION, TICK_GROUP, "q"), "never": (REGION, TICK_GROUP, "q2")}
    until = since + timedelta(days=3)
    kinds, slices = ee.read_logs(
        specs, since, until, client=None, wait=30.0, run=run, min_slice=timedelta(minutes=15)
    )
    assert kinds["lines"]["state"] == "complete"
    assert sorted(row["@message"] for row in kinds["lines"]["rows"]) == sorted(f"m{n}" for n in range(10))
    assert all("@log" not in row for row in kinds["lines"]["rows"])
    assert any(s["state"] == "split" for s in slices.values())
    assert kinds["never"]["state"] == "not_checked" and kinds["never"]["missing"] == [
        {"start": ee._iso(since), "end": ee._iso(until)}
    ]
    # A slice that cannot be split further is kept, and said as cut.
    kinds, _slices = ee.read_logs(
        {"lines": specs["lines"]}, since, until, client=None, wait=30.0, run=run, min_slice=timedelta(days=10)
    )
    assert kinds["lines"]["state"] == "cut" and len(kinds["lines"]["rows"]) == 4


def test_the_kept_rule_is_the_assistants(pipeline):
    rows = [{"@timestamp": r["@timestamp"], "@message": r["@message"]} for r in pipeline.groups[TICK_GROUP]]
    asked = {CITIES, DEMO}
    moved = vision_evidence.moved_codes(vision_evidence.declared_vocabularies())
    lines, _ = vision_evidence.parse_rows(rows, "decisions", asked, moved)
    attempts, _ = vision_evidence.parse_rows(rows, "attempts", asked, moved)
    decisions = [line for line in lines if line["event"] == "vision_decision"]
    attempt_by = {}
    for attempt in attempts:
        attempt_by.setdefault((attempt.get("tick"), attempt["run"]), attempt)
    for line in decisions:
        entries, _not_kept, _unknown = vision_evidence.join([line], [], attempts)
        assert entries[0]["kept"] == ee.attempt_of(line, attempt_by)[1]


def test_the_backstop_redacts_account_ids_and_arns_but_not_a_uuids_last_group():
    value = {
        "a": f"x {PLANTED_ARN} y",
        "b": int(ACCOUNT),
        "c": [f"id {ACCOUNT}."],
        ACCOUNT: 1,
        "d": "0f0e0d0c-1111-4222-8333-123456789012",
        "e": 1234567890123,
        "f": 12345678901.0,
    }
    cleaned, count = ee.backstop(value)
    assert (
        cleaned["a"] == "x [redacted] y"
        and cleaned["b"] == "[redacted]"
        and cleaned["c"] == ["id [redacted]."]
    )
    assert "[redacted]" in cleaned and cleaned["d"] == value["d"] and cleaned["e"] == value["e"]
    assert cleaned["f"] == value["f"] and count == 4


def test_report_lines_are_read_as_fields_never_as_text():
    row = {
        "@timestamp": "2026-10-20 01:00:00.000",
        "@logStream": "s",
        "@requestId": "req-1",
        "@message": _report("req-1", ms=58000.5, memory=2048, used=2048, status="error")
        + "\tError Type: Runtime.OutOfMemory",
    }
    report = ee.parse_report(row)
    assert (
        report["request_id"] == "req-1" and report["duration_ms"] == 58000.5 and report["billed_ms"] == 58001
    )
    assert (report["memory_mb"], report["max_memory_mb"], report["status"]) == (2048, 2048, "error")
    assert report["error_type"] == "Runtime.OutOfMemory" and ee.no_reply_cause(report) == "out_of_memory"
    assert ee.parse_report({"@message": "START RequestId: x"}) is None
    assert ee.no_reply_cause({"status": "ok"}) == "other"
    assert [ee.caller_of(s) for s in ("eval-sydney", "bench-x", "demo-sydney", "sydney", None)] == [
        "eval",
        "bench",
        "demo",
        "production",
        "production",
    ]


def test_the_deploy_is_the_newest_run_before_the_functions_last_modified():
    runs, why = ee.deploy_runs("dev", run=fake_gh)
    assert why is None and [r["head_sha"] for r in runs] == [LATER_SHA, DEPLOY_SHA, "c" * 40]
    assert ee.deploy_for("2024-07-28T00:00:00.000+0000", runs, None)["head_sha"] == DEPLOY_SHA
    assert ee.deploy_for("2024-07-01T00:00:00.000+0000", runs, None)["head_sha"] is None
    signed_out = lambda command, **kwargs: subprocess.CompletedProcess(command, 4, stdout="", stderr="auth")  # noqa: E731
    assert ee.deploy_runs("dev", run=signed_out) == (
        None,
        "gh exited 4 (signed out, or no access to the " "repository's runs)",
    )

    def slow(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 60)

    assert ee.deploy_runs("dev", run=slow)[1] == "gh did not answer in 60 s"


def test_names_keys_and_the_demo_topics_are_the_codes_and_the_trackers():
    import research_tick_handler

    names = _REAL_NAMES("bloggerbear", "dev")
    assert (
        names["research_tick_group"] == TICK_GROUP
        and names["vision_worker"] == "bloggerbear-dev-vision-worker"
    )
    assert names["bucket"] == BUCKET and names["articles"] == "bloggerbear-dev-articles"
    assert ee.RUNNING_STATE_KEY.format(topic_id="t") == research_tick_handler._running_state_key("t")
    import test_hackathon_docs

    tracker = ee.TRACKER.read_text(encoding="utf-8")
    assert ee.demo_topics(tracker) == test_hackathon_docs.demo_topics(tracker) == [DEMO]
    assert vision_decisions.CODES == (
        "first_observation",
        "below_floor",
        "baseline_building",
        "within_range",
        "reported",
        "artefact",
    )


def test_importing_the_export_needs_no_cv2_numpy_or_networkx():
    code = (
        "import sys; sys.path[:0] = ['scripts', 'lambdas']; import evidence_export; "
        "print(sorted(m for m in ('cv2', 'numpy', 'networkx') if m in sys.modules))"
    )
    done = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[]"
