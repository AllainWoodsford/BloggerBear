"""The evals rollup (common/evals_rollup.py; docs/enhancements/model-ranch-prompt-assets-evals.md,
sections 4.4, 4.6, 4.9 and 5.3), against moto tables and a Logs Insights stand-in that answers from
a list of log lines as Logs would (whole seconds, both ends included, at most `limit` rows).

What is held: each event counts for the rows its reason applies to, the model once per article for
a reason written once per prompt, even across midnight; the watermark moves to a day two hours after
the midnight that ends it, never back, and an event written after that is late, counted once and in
no row, whether or not the watermark has reached its day; a copy of an event logged again counts as
its first copy did; two runs over one log write the same rows; a week is seven days and weeks and
months are summed across a month's end without a day counted twice; days are those of the settings'
time zone, Sydney's by default and across daylight saving; a model with no row is never given one
back, and the judge in effect is among a model's roles; a purpose written while Bedrock is listed is
kept; excluded sources are left out and their rows deleted when a day is added up again; a window of
10,000 rows is split and every event counted, and a minute that alone holds them raises rather than
splitting forever; the judge's agreement comes from pair# rows, and its verdicts gate only while it
is calibrated; readers' votes reach cells and never a gate; the line the rollup ends with is the one
the heartbeat filter counts.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import boto3
import pytest
from moto import mock_aws
from table_schemas import KEYS

from common import dynamo, evals, evals_rollup, evidence

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"
GROUP = "acme-dev-evals"
SYDNEY = ZoneInfo("Australia/Sydney")
# Not real models' names: the rollup only cares about the shape.
ALPHA = "maker.alpha-v1:0"
BETA = "maker.beta-v1:0"
GONE = "maker.gone-v1:0"
DRAFT, TITLE, IDEATION = "daily_cycle/draft", "daily_cycle/title", "daily_cycle/ideation"
WINDOWS = ("daily", "weekly", "monthly", "total")


def sydney(text: str) -> datetime:
    """A Sydney wall-clock time, as the UTC moment it is."""
    return datetime.fromisoformat(text).replace(tzinfo=SYDNEY).astimezone(UTC)


# 14:00 on Tuesday 27 October 2026 in Sydney (daylight saving time, UTC+11).
NOW = sydney("2026-10-27T14:00")


class FakeLogs:
    """Logs Insights over a list of (logged at, message) lines: each query returns the lines logged
    within its whole seconds, both ends included, oldest first, at most `limit` of them; a query for
    `subject in [...]` returns those subjects' Article Completion events only."""

    def __init__(self):
        self.lines: list[tuple[datetime, str]] = []
        self.queries: list[dict] = []
        self.fail = False
        # When set, a query past this many fails, so a read that would split forever stops.
        self.max_queries: int | None = None
        self._results: dict[str, list] = {}

    def put(self, message: str, logged_at: datetime) -> None:
        self.lines.append((logged_at.astimezone(UTC), message))

    def start_query(self, *, logGroupName, startTime, endTime, queryString, limit):  # noqa: N803
        if self.max_queries is not None and len(self.queries) >= self.max_queries:
            raise AssertionError("split without end")
        self.queries.append(
            {"group": logGroupName, "start": startTime, "end": endTime, "query": queryString, "limit": limit}
        )
        rows = sorted(line for line in self.lines if startTime <= int(line[0].timestamp()) <= endTime)
        subjects = re.search(r"subject in \[(.*)\]", queryString)
        if subjects:
            wanted = set(re.findall(r'"([^"]+)"', subjects.group(1)))
            rows = [
                line
                for line in rows
                if (record := json.loads(line[1]))["reason"] == "Article Completion"
                and record.get("subject") in wanted
            ]
        query_id = f"q{len(self.queries)}"
        self.queries[-1]["returned"] = min(len(rows), limit)
        self._results[query_id] = [
            [
                {"field": "@timestamp", "value": logged.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]},
                {"field": "@message", "value": message},
            ]
            for logged, message in rows[:limit]
        ]
        return {"queryId": query_id}

    def get_query_results(self, queryId):  # noqa: N803
        if self.fail:
            return {"status": "Failed", "results": []}
        return {"status": "Complete", "results": self._results[queryId]}

    def stop_query(self, queryId):  # noqa: N803
        pass


class FakeBedrock:
    """Bedrock's listings: foundation models (any lifecycle) and no inference profiles."""

    def __init__(self, summaries=()):
        self.summaries = list(summaries)
        self.listings = 0

    def list_foundation_models(self):
        self.listings += 1
        return {"modelSummaries": self.summaries}

    def list_inference_profiles(self, **kwargs):
        return {"inferenceProfileSummaries": []}


@pytest.fixture
def aws(monkeypatch):
    for key, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "EVALS_TABLE": "Evals",
        "EVALS_LOG_GROUP": GROUP,
        "MODELS_TABLE": "Models",
        "MODEL_CONFIG_TABLE": "ModelConfig",
        "TOPICS_TABLE": "Topics",
        "BEDROCK_MODEL_ID": ALPHA,
    }.items():
        monkeypatch.setenv(key, value)
    dynamo._dynamodb_resource = None
    with mock_aws():
        client = boto3.client("dynamodb", region_name=REGION)
        hash_key, range_key = KEYS["Evals"]
        client.create_table(
            TableName="Evals",
            KeySchema=[
                {"AttributeName": hash_key, "KeyType": "HASH"},
                {"AttributeName": range_key, "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": hash_key, "AttributeType": "S"},
                {"AttributeName": range_key, "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        for name, key in {"Models": "model_id", "ModelConfig": "config_id", "Topics": "topic_id"}.items():
            client.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        resource = boto3.resource("dynamodb", region_name=REGION)
        for model_id in (ALPHA, BETA):
            put_model(resource, model_id)
        yield resource
    dynamo._dynamodb_resource = None


def put_model(aws, model_id, **fields):
    item = {"model_id": model_id, "display_name": model_id, "provider": "maker", "model_purpose": "text"}
    aws.Table("Models").put_item(Item={**item, **fields})


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(logs: FakeLogs, reason: str, at: datetime, *, parts: dict, logged_at=None, **fields) -> dict:
    """Write one event as emit() would, logged a second after it happened unless told otherwise."""
    fields.setdefault("source", "daily_cycle")
    event_id = evals.event_id(reason, **parts)
    record, refused = evals.event(reason, event_id=event_id, timestamp=iso(at), **fields)
    assert refused is None, refused
    logs.put(evals.line(record), logged_at or at + timedelta(seconds=1))
    return record


def completion(logs, article, prompt_id, at, *, model=ALPHA, version="v1", **kw):
    return log(
        logs, "Article Completion", at, parts={"article_id": article, "prompt_id": prompt_id},
        model_id=model, prompt_id=prompt_id, prompt_version=version, subject=article, topic_id="topic-a",
        **kw,
    )  # fmt: skip


def rewrite(logs, rewrite_id, article, at, *, model=ALPHA, **kw):
    return log(
        logs, "Article Rewrite", at, parts={"rewrite_id": rewrite_id}, model_id=model, subject=article,
        topic_id="topic-a", source="admin_api", **kw,
    )  # fmt: skip


def vote(logs, feedback_id, article, delta, at, *, model=ALPHA, **kw):
    return log(
        logs, "User Feedback", at, parts={"feedback_id": feedback_id}, model_id=model, subject=article,
        topic_id="topic-a", rating_delta=delta, source="public_api", **kw,
    )  # fmt: skip


def judged(logs, subject, run_id, delta, at, *, model=ALPHA, **kw):
    return log(
        logs, "Research Review", at, parts={"subject": subject, "run_id": run_id}, model_id=model,
        subject=subject, topic_id="topic-a", rating_delta=delta, source="eval_jobs", by="judge", **kw,
    )  # fmt: skip


def rollup(logs, now=NOW, bedrock=None, **kwargs) -> dict:
    bedrock = bedrock or FakeBedrock()
    return evals_rollup.run(now=now, logs_client=lambda region: logs, bedrock=bedrock, **kwargs)


def row(aws, entity: str, day: str) -> dict | None:
    return aws.Table("Evals").get_item(Key={"entity": entity, "sk": f"day#{day}"}).get("Item")


def index(aws) -> dict:
    return aws.Table("Evals").get_item(Key=evals_rollup.INDEX_KEY).get("Item") or {}


def model_row(aws, model_id) -> dict | None:
    return aws.Table("Models").get_item(Key={"model_id": model_id}).get("Item")


def entities(aws) -> set[tuple[str, str]]:
    return {(item["entity"], item["sk"]) for item in aws.Table("Evals").scan()["Items"]}


def plain(value):
    return dynamo._plain_numbers(value)


# --- what an event counts for ---------------------------------------------------------------------


def test_an_articles_completion_counts_once_for_its_model_and_once_for_each_prompt_and_cell(aws):
    logs = FakeLogs()
    at = sydney("2026-10-27T09:00")
    for prompt_id in (IDEATION, DRAFT, TITLE):
        completion(logs, "a1", prompt_id, at)
    rewrite(logs, "rw1", "a1", at + timedelta(minutes=5), prompt_id=DRAFT, prompt_version="v1")

    summary = rollup(logs)

    model = plain(row(aws, f"model#{ALPHA}", "2026-10-27"))
    # One article: +2 once, not once per prompt; and the rewrite's -2 (a model-only reason).
    assert (model["perf"], model["events"]) == (0, 2)
    assert model["by_reason"] == {
        "Article Completion": {"perf": 2, "events": 1, "good": 1, "bad": 0},
        "Article Rewrite": {"perf": -2, "events": 1, "good": 0, "bad": 1},
    }
    # Section 4.9's good and bad leave the Completion out.
    assert (model["good"], model["bad"]) == (0, 1)
    for prompt_id in (IDEATION, DRAFT, TITLE):
        prompt = plain(row(aws, f"prompt#{prompt_id}#v1", "2026-10-27"))
        assert (prompt["perf"], prompt["events"], prompt["good"], prompt["bad"]) == (2, 1, 0, 0)
    # The rewrite names a prompt: it counts in that model and prompt cell, never in the prompt's row
    # (section 3, deviation 15).
    assert plain(row(aws, f"cell#{ALPHA}#{DRAFT}#v1", "2026-10-27"))["perf"] == 0
    assert plain(row(aws, f"cell#{ALPHA}#{TITLE}#v1", "2026-10-27"))["perf"] == 2
    assert summary["events"] == 4 and summary["status"] == "done"
    listed = aws.Table("Evals").get_item(Key={"entity": "index", "sk": "day#2026-10-27"})["Item"]["entities"]
    assert listed == {
        f"model#{ALPHA}", *(f"prompt#{p}#v1" for p in (IDEATION, DRAFT, TITLE)),
        *(f"cell#{ALPHA}#{p}#v1" for p in (IDEATION, DRAFT, TITLE)),
    }  # fmt: skip


def test_an_article_written_across_midnight_counts_once_for_its_model_after_the_day_is_final(aws):
    logs = FakeLogs()
    completion(logs, "a1", IDEATION, sydney("2026-10-26T23:59:50"))
    completion(logs, "a1", DRAFT, sydney("2026-10-27T00:00:10"))
    rollup(logs, now=sydney("2026-10-27T14:00"))
    assert index(aws)["last_rolled_day"] == "2026-10-26"

    rollup(logs, now=sydney("2026-10-27T15:00"))  # adds up the 27th alone: the 26th is final

    model = plain(row(aws, f"model#{ALPHA}", "2026-10-26"))
    assert model["by_reason"]["Article Completion"]["events"] == 1
    assert row(aws, f"model#{ALPHA}", "2026-10-27") is None
    # Only the model's share is taken once: each prompt still counts on its own day.
    assert plain(row(aws, f"prompt#{IDEATION}#v1", "2026-10-26"))["perf"] == 2
    assert plain(row(aws, f"prompt#{DRAFT}#v1", "2026-10-27"))["perf"] == 2
    alpha = plain(model_row(aws, ALPHA))
    assert (alpha["total_perf"], alpha["perf_events"]["total"]) == (2, 1)


def test_a_prompt_fallback_is_counted_for_its_prompt_version_and_scores_nothing(aws):
    logs = FakeLogs()
    log(
        logs, "Prompt Fallback", sydney("2026-10-27T08:00"),
        parts={"request_id": "req-1", "prompt_id": DRAFT}, prompt_id=DRAFT, prompt_version="v2",
    )  # fmt: skip

    rollup(logs)

    prompt = plain(row(aws, f"prompt#{DRAFT}#v2", "2026-10-27"))
    assert (prompt["perf"], prompt["events"], prompt["good"], prompt["bad"]) == (0, 1, 0, 0)
    assert row(aws, f"model#{ALPHA}", "2026-10-27") is None


def test_a_duplicate_event_counts_once_and_an_unreadable_line_not_at_all(aws):
    logs = FakeLogs()
    record = completion(logs, "a1", DRAFT, sydney("2026-10-27T09:00"))
    logs.put(evals.line(record), sydney("2026-10-27T09:05"))  # a retried invocation's copy
    logs.put('{"schema":"evals/1","reason":"Made Up"}', sydney("2026-10-27T09:06"))

    summary = rollup(logs)

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["events"] == 1
    assert (summary["duplicates"], summary["unreadable"], summary["events"]) == (1, 1, 1)


def test_an_operators_forgive_is_not_a_day_row_and_adjust_rows_move_only_the_total(aws):
    logs = FakeLogs()
    at = sydney("2026-10-27T09:00")
    completion(logs, "a1", DRAFT, at)
    log(
        logs, "Admin Forgive", at, parts={"ts": iso(at), "target": ALPHA}, model_id=ALPHA,
        rating_delta=50, source="admin_api", by="human",
    )  # fmt: skip
    adjustment = {"entity": f"adjust#model#{ALPHA}", "sk": f"ts#{iso(at)}", "delta": 50}
    aws.Table("Evals").put_item(Item=adjustment)

    summary = rollup(logs)

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["by_reason"].keys() == {"Article Completion"}
    alpha = plain(model_row(aws, ALPHA))
    assert [alpha[f"{window}_perf"] for window in WINDOWS] == [2, 2, 2, 52]
    assert alpha["perf_events"] == {"daily": 1, "weekly": 1, "monthly": 1, "total": 1}
    assert summary["adjustments"] == 1


# --- readers' votes -------------------------------------------------------------------------------


def test_a_vote_reaches_the_cells_of_the_articles_prompts_and_never_a_prompts_row(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T08:00"))
    completion(logs, "a1", TITLE, sydney("2026-10-27T08:00"), version="v3")
    vote(logs, "f1", "a1", -3, sydney("2026-10-27T10:00"))

    rollup(logs)

    assert plain(row(aws, f"cell#{ALPHA}#{DRAFT}#v1", "2026-10-27"))["perf"] == 2 - 3
    title = plain(row(aws, f"cell#{ALPHA}#{TITLE}#v3", "2026-10-27"))
    assert title["by_reason"]["User Feedback"]["perf"] == -3
    assert plain(row(aws, f"prompt#{DRAFT}#v1", "2026-10-27"))["perf"] == 2
    model = plain(row(aws, f"model#{ALPHA}", "2026-10-27"))
    assert (model["perf"], model["good"], model["bad"]) == (2 - 3, 0, 1)


def test_a_vote_on_an_older_article_joins_its_completions_from_further_back_in_the_log(aws):
    logs = FakeLogs()
    written = sydney("2026-09-30T08:00")
    completion(logs, "old-1", DRAFT, written)
    vote(logs, "f1", "old-1", 1, sydney("2026-10-27T10:00"))
    aws.Table("Evals").put_item(Item={**evals_rollup.INDEX_KEY, "last_rolled_day": "2026-10-26"})

    rollup(logs)

    assert plain(row(aws, f"cell#{ALPHA}#{DRAFT}#v1", "2026-10-27"))["perf"] == 1
    lookup = [query for query in logs.queries if "subject in" in query["query"]]
    assert len(lookup) == 1 and '"old-1"' in lookup[0]["query"]
    # Further back than this run's read, which starts at the watermark's day.
    assert lookup[0]["end"] == int(evals_rollup.day_start(date(2026, 10, 26), SYDNEY).timestamp())


def test_readers_votes_score_in_the_day_rows_and_never_in_the_windows_the_gates_read(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T08:00"))
    for n in range(4):
        vote(logs, f"f{n}", "a1", -3, sydney("2026-10-27T10:00") + timedelta(minutes=n))

    rollup(logs)

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["perf"] == 2 - 12
    alpha = plain(model_row(aws, ALPHA))
    assert (alpha["daily_perf"], alpha["perf_events"]["daily"]) == (2, 1)


# --- the watermark and late events ----------------------------------------------------------------


def test_the_watermark_moves_to_yesterday_only_once_its_midnight_is_two_hours_past(aws):
    logs = FakeLogs()

    early = rollup(logs, now=sydney("2026-10-27T01:59"))
    assert (early["last_rolled_day"], index(aws)["last_rolled_day"]) == ("2026-10-25", "2026-10-25")

    rollup(logs, now=sydney("2026-10-27T02:01"))
    assert index(aws)["last_rolled_day"] == "2026-10-26"

    rollup(logs, now=sydney("2026-10-27T01:00"))  # a later run whose clock is behind: never moves it back
    assert index(aws)["last_rolled_day"] == "2026-10-26"


def test_a_run_that_read_the_watermark_before_another_moved_it_never_moves_it_back(aws, monkeypatch):
    logs = FakeLogs()
    aws.Table("Evals").put_item(Item={**evals_rollup.INDEX_KEY, "last_rolled_day": "2026-10-24"})
    read = evals_rollup.get_index

    def read_then_overtaken(table=None):
        held = read(table)
        # The scheduled run and `evals rollup` at once: the other one saves its watermark first.
        aws.Table("Evals").update_item(
            Key=evals_rollup.INDEX_KEY,
            UpdateExpression="SET last_rolled_day = :day",
            ExpressionAttributeValues={":day": "2026-10-26"},
        )
        return held

    monkeypatch.setattr(evals_rollup, "get_index", read_then_overtaken)
    summary = rollup(logs, now=sydney("2026-10-27T01:00"))

    # This run tried to move it to its own newest final day, and was refused.
    assert summary["last_rolled_day"] == "2026-10-25"
    assert index(aws)["last_rolled_day"] == "2026-10-26"


def test_an_event_written_late_is_counted_once_in_late_dropped_and_in_no_day_row(aws):
    logs = FakeLogs()
    # Written half an hour after its day ended: still in time, while the day is open.
    completion(logs, "a1", DRAFT, sydney("2026-10-26T23:00"), logged_at=sydney("2026-10-27T01:30"))
    rollup(logs, now=sydney("2026-10-27T01:45"))
    rollup(logs, now=sydney("2026-10-27T03:00"))
    assert index(aws)["last_rolled_day"] == "2026-10-26"
    counted = plain(row(aws, f"model#{ALPHA}", "2026-10-26"))
    assert counted["perf"] == 2

    # Written after its day became final (02:00 on the 27th): late.
    completion(logs, "a2", DRAFT, sydney("2026-10-26T22:00"), logged_at=sydney("2026-10-27T04:00"))
    first = rollup(logs, now=sydney("2026-10-27T05:00"))
    again = rollup(logs, now=sydney("2026-10-27T06:00"))

    assert (first["late"], first["late_dropped"]) == (1, 1)
    assert (again["late"], again["late_dropped"]) == (1, 1)
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-26")) == counted
    assert plain(index(aws))["late_days"] == {"2026-10-27": 1}

    # While the watermark moves on, the day it was written on is read again whole, then frozen.
    for now in ("2026-10-28T03:00", "2026-10-29T03:00", "2026-10-30T03:00", "2026-10-31T03:00"):
        assert rollup(logs, now=sydney(now))["late_dropped"] == 1
    assert plain(index(aws))["late_dropped"] == 1
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-26")) == counted


def test_an_event_written_after_its_day_became_final_is_late_before_the_watermark_reaches_that_day(aws):
    logs = FakeLogs()
    # Written at 02:01 on the 27th: a minute after the 26th became final, and before the 02:05 run
    # that moves the watermark to it.
    completion(logs, "a1", DRAFT, sydney("2026-10-26T23:00"), logged_at=sydney("2026-10-27T02:01"))
    rollup(logs, now=sydney("2026-10-27T01:45"))
    assert index(aws)["last_rolled_day"] == "2026-10-25"

    moving = rollup(logs, now=sydney("2026-10-27T02:05"))
    assert index(aws)["last_rolled_day"] == "2026-10-26"
    after = rollup(logs, now=sydney("2026-10-27T03:05"))

    for summary in (moving, after):
        assert (summary["late"], summary["late_dropped"]) == (1, 1)
    assert row(aws, f"model#{ALPHA}", "2026-10-26") is None
    # Added up again, the day leaves it out by the same rule.
    the_26th = date(2026, 10, 26)
    rollup(logs, now=sydney("2026-10-27T04:00"), trigger="admin", day_from=the_26th, day_to=the_26th)
    assert row(aws, f"model#{ALPHA}", "2026-10-26") is None
    assert plain(index(aws))["late_dropped"] == 1


def test_a_first_run_counts_an_event_written_after_its_day_became_final_as_late(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-26T23:00"), logged_at=sydney("2026-10-27T02:01"))

    summary = rollup(logs, now=sydney("2026-10-27T02:05"))

    assert (summary["late"], summary["late_dropped"]) == (1, 1)
    assert row(aws, f"model#{ALPHA}", "2026-10-26") is None


def test_a_copy_of_an_event_logged_after_its_day_is_final_is_not_late(aws):
    logs = FakeLogs()
    record = completion(logs, "a1", DRAFT, sydney("2026-10-26T23:00"))
    rollup(logs, now=sydney("2026-10-27T01:00"))
    rollup(logs, now=sydney("2026-10-27T03:00"))
    assert index(aws)["last_rolled_day"] == "2026-10-26"
    counted = plain(row(aws, f"model#{ALPHA}", "2026-10-26"))
    logs.put(evals.line(record), sydney("2026-10-27T04:00"))  # a retried step's copy, after 02:00

    summary = rollup(logs, now=sydney("2026-10-27T05:00"))

    assert (summary["late"], summary["late_dropped"], summary["duplicates"]) == (0, 0, 1)
    assert counted["perf"] == 2 and plain(row(aws, f"model#{ALPHA}", "2026-10-26")) == counted
    # Added up again, the day keeps the event its first copy counted in time.
    the_26th = date(2026, 10, 26)
    rollup(logs, now=sydney("2026-10-27T06:00"), trigger="admin", day_from=the_26th, day_to=the_26th)
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-26"))["perf"] == 2


def test_two_runs_over_the_same_log_write_the_same_rows(aws):
    logs = FakeLogs()
    for day in ("2026-10-24", "2026-10-26", "2026-10-27"):
        completion(logs, f"a-{day}", DRAFT, sydney(f"{day}T09:00"))
        vote(logs, f"f-{day}", f"a-{day}", 1, sydney(f"{day}T11:00"))
        at = sydney(f"{day}T12:00")
        judged(logs, f"a-{day}", "r1", -2, at, model=BETA, prompt_id=DRAFT, prompt_version="v1")

    def snapshot():
        evals_items = [
            plain(item) for item in aws.Table("Evals").scan()["Items"] if item["sk"] != "index"
        ]
        held = {key: value for key, value in plain(index(aws)).items() if key != "last_run"}
        return sorted(evals_items, key=lambda item: (item["entity"], item["sk"])), held, plain(
            sorted(aws.Table("Models").scan()["Items"], key=lambda item: item["model_id"])
        )

    rollup(logs)
    once = snapshot()
    rollup(logs)

    assert snapshot() == once


# --- windows --------------------------------------------------------------------------------------


def test_weeks_and_months_are_summed_across_a_months_end_without_a_day_counted_twice(aws):
    logs = FakeLogs()
    for day in ("2026-10-20", "2026-10-27", "2026-10-31", "2026-11-01", "2026-11-02"):
        completion(logs, f"a-{day}", DRAFT, sydney(f"{day}T09:00"))

    rollup(logs, now=sydney("2026-11-02T14:00"))

    alpha = plain(model_row(aws, ALPHA))
    # The week is 27 October to 2 November; the month only 1 and 2 November.
    assert [alpha[f"{window}_perf"] for window in WINDOWS] == [2, 8, 4, 10]
    assert alpha["perf_events"] == {"daily": 1, "weekly": 4, "monthly": 2, "total": 5}


def test_the_windows_are_sums_of_the_day_rows_read_back():
    # 26 December is the week's first day; 25 December, the day before it, counts in the total only.
    rows = [
        {"sk": f"day#{day}", "by_reason": {"Article Completion": {"perf": 2, "events": 1}}}
        for day in ("2026-12-25", "2026-12-26", "2026-12-31", "2027-01-01")
    ]
    rows.append({"sk": "day#not-a-date", "by_reason": {"Article Completion": {"perf": 99, "events": 9}}})

    found = evals_rollup.windows(rows, [{"delta": Decimal("-5")}], date(2027, 1, 1), calibrated=False)

    assert found == {
        "daily_perf": 2, "weekly_perf": 6, "monthly_perf": 2, "total_perf": 3,
        "perf_events": {"daily": 1, "weekly": 3, "monthly": 1, "total": 4},
    }  # fmt: skip


def test_the_pickers_good_and_bad_counts_are_the_day_rows_summed_by_window():
    rows = [
        {"sk": "day#2026-12-25", "good": 4, "bad": 1},
        {"sk": "day#2026-12-26", "good": Decimal("2"), "bad": Decimal("3")},
        {"sk": "day#2027-01-01", "good": 1, "bad": 0},
        {"sk": "day#not-a-date", "good": 99, "bad": 99},
        {"sk": "day#2027-01-01-x"},
    ]

    found = evals_rollup.good_and_bad(rows, date(2027, 1, 1))

    assert found == {
        "perf_good": {"daily": 1, "weekly": 3, "monthly": 1, "total": 7},
        "perf_bad": {"daily": 0, "weekly": 3, "monthly": 0, "total": 4},
    }


def test_the_pickers_counts_go_on_the_models_row_with_votes_and_the_judge_and_never_completions(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-20T09:00"))
    vote(logs, "f1", "a1", 1, sydney("2026-10-20T10:00"))
    rewrite(logs, "r1", "a1", sydney("2026-10-26T09:00"))
    completion(logs, "a2", DRAFT, sydney("2026-10-27T08:00"))
    vote(logs, "f2", "a2", 3, sydney("2026-10-27T09:00"))
    vote(logs, "f3", "a2", -1, sydney("2026-10-27T09:30"))
    judged(logs, "a2", "j1", 2, sydney("2026-10-27T10:00"))  # uncalibrated: counted here, not gated

    rollup(logs)

    alpha = plain(model_row(aws, ALPHA))
    assert alpha["perf_good"] == {"daily": 2, "weekly": 2, "monthly": 3, "total": 3}
    assert alpha["perf_bad"] == {"daily": 1, "weekly": 2, "monthly": 2, "total": 2}
    # The gates' windows are what they were: completions and the rewrite, no votes, no judge.
    assert (alpha["daily_perf"], alpha["perf_events"]["daily"]) == (2, 1)
    assert plain(model_row(aws, BETA))["perf_good"] == dict.fromkeys(WINDOWS, 0)


def test_days_are_sydney_days_across_daylight_saving(aws):
    logs = FakeLogs()
    # 23:30 on Saturday 3 October (UTC+10), and 00:30 on Monday 5 October (UTC+11): 24 hours apart,
    # two calendar days apart, and Sunday 4 October (23 hours long) holds neither.
    completion(logs, "before", DRAFT, datetime(2026, 10, 3, 13, 30, tzinfo=UTC))
    completion(logs, "after", DRAFT, datetime(2026, 10, 4, 13, 30, tzinfo=UTC))

    rollup(logs, now=datetime(2026, 10, 5, 3, 0, tzinfo=UTC))

    assert row(aws, f"model#{ALPHA}", "2026-10-03") and row(aws, f"model#{ALPHA}", "2026-10-05")
    assert row(aws, f"model#{ALPHA}", "2026-10-04") is None
    # A day is final two hours after the midnight that ends it, however long the day was.
    assert evals_rollup.final_at(date(2026, 10, 3), SYDNEY) == datetime(2026, 10, 3, 16, 0, tzinfo=UTC)
    newest_final = evals_rollup.newest_final_day
    assert newest_final(datetime(2026, 10, 3, 15, 59, tzinfo=UTC), SYDNEY) == date(2026, 10, 2)
    assert newest_final(datetime(2026, 10, 3, 16, 0, tzinfo=UTC), SYDNEY) == date(2026, 10, 3)
    # And when the clocks go back (5 April 2026), a 25-hour day.
    assert evals_rollup.final_at(date(2026, 4, 4), SYDNEY) == datetime(2026, 4, 4, 15, 0, tzinfo=UTC)
    assert evals_rollup.day_start(date(2026, 4, 6), SYDNEY) - evals_rollup.day_start(
        date(2026, 4, 5), SYDNEY
    ) == timedelta(hours=25)


def test_days_are_those_of_the_settings_time_zone_and_so_is_the_watermark(aws):
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "timezone": "UTC"})
    logs = FakeLogs()
    # 20:00 on the 26th in UTC is 07:00 on the 27th in Sydney.
    completion(logs, "a1", DRAFT, datetime(2026, 10, 26, 20, 0, tzinfo=UTC))

    rollup(logs, now=datetime(2026, 10, 27, 1, 59, tzinfo=UTC))

    assert row(aws, f"model#{ALPHA}", "2026-10-26") is not None
    assert row(aws, f"model#{ALPHA}", "2026-10-27") is None
    # The 26th becomes final at 02:00 UTC on the 27th; Sydney's 26th had been since 15:00 UTC.
    assert index(aws)["last_rolled_day"] == "2026-10-25"
    rollup(logs, now=datetime(2026, 10, 27, 2, 1, tzinfo=UTC))
    assert index(aws)["last_rolled_day"] == "2026-10-26"


# --- the models row -------------------------------------------------------------------------------


def test_a_culled_models_row_is_never_recreated(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T09:00"), model=GONE)

    summary = rollup(logs)

    assert model_row(aws, GONE) is None
    assert summary["not_in_registry"] == 1
    # Its history stays in the evals table.
    assert plain(row(aws, f"model#{GONE}", "2026-10-27"))["perf"] == 2


def test_every_registry_model_gets_its_windows_roles_and_time_and_keeps_its_other_fields(aws):
    config = aws.Table("ModelConfig")
    config.put_item(Item={"config_id": "default", "model_id": ALPHA, "fallback_model_id": BETA})
    aws.Table("ModelConfig").put_item(Item={"config_id": "model-rotation", "candidates": [BETA]})
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_model_id": BETA})
    aws.Table("Topics").put_item(Item={"topic_id": "t1", "model_id": BETA})
    put_model(aws, "maker.locked-v1:0", price_locked=True, input_price_usd_per_1k_tokens=Decimal("0.001"))

    rollup(FakeLogs())

    beta = plain(model_row(aws, BETA))
    assert beta["model_roles"] == ["fallback", "topic_model", "pool", "judge"]
    assert (beta["daily_perf"], beta["total_perf"]) == (0, 0)
    assert beta["perf_updated_at"] == NOW.isoformat()
    assert plain(model_row(aws, ALPHA))["model_roles"] == ["default"]
    locked = plain(model_row(aws, "maker.locked-v1:0"))
    assert locked["model_roles"] == ["price_locked"] and locked["input_price_usd_per_1k_tokens"] == 0.001
    assert locked["display_name"] == "maker.locked-v1:0" and locked["model_purpose"] == "text"


def test_the_judge_in_effect_is_among_a_models_roles_when_no_judge_model_id_is_set(aws):
    config = {"config_id": "default", "model_id": ALPHA, "fallback_model_id": BETA}
    aws.Table("ModelConfig").put_item(Item=config)
    # An evals row that names no judge: the judge is the default row's fallback (section 4.5).
    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "judge_enabled": True})

    rollup(FakeLogs())

    assert plain(model_row(aws, BETA))["model_roles"] == ["fallback", "judge"]
    assert plain(model_row(aws, ALPHA))["model_roles"] == ["default"]


def test_models_without_a_purpose_are_classified_from_everything_bedrock_lists(aws):
    put_model(aws, "maker.legacy-v1:0", model_purpose=None)
    put_model(aws, "maker.vanished-v1:0", model_purpose=None)
    aws.Table("Models").update_item(
        Key={"model_id": "maker.legacy-v1:0"}, UpdateExpression="REMOVE model_purpose"
    )
    aws.Table("Models").update_item(
        Key={"model_id": "maker.vanished-v1:0"}, UpdateExpression="REMOVE model_purpose"
    )
    bedrock = FakeBedrock(
        [
            # LEGACY: account_models would leave it out, so `unknown` would wrongly say it is gone.
            {
                "modelId": "maker.legacy-v1:0",
                "outputModalities": ["IMAGE"],
                "inputModalities": ["TEXT"],
                "modelLifecycle": {"status": "LEGACY"},
            },
        ]
    )

    summary = rollup(FakeLogs(), bedrock=bedrock)

    assert bedrock.listings == 1
    assert model_row(aws, "maker.legacy-v1:0")["model_purpose"] == "image"
    assert model_row(aws, "maker.legacy-v1:0")["model_inputs"] == ["TEXT"]
    assert model_row(aws, "maker.vanished-v1:0")["model_purpose"] == "unknown"
    assert model_row(aws, ALPHA)["model_purpose"] == "text"
    assert (summary["classify"], summary["classified"], summary["unknown"]) == ("done", 1, 1)

    quiet = FakeBedrock()
    assert rollup(FakeLogs(), bedrock=quiet)["classify"] == "none" and quiet.listings == 0


def test_a_listing_that_fails_leaves_models_unclassified_and_the_run_finishes(aws, capsys):
    aws.Table("Models").update_item(Key={"model_id": BETA}, UpdateExpression="REMOVE model_purpose")

    class Broken(FakeBedrock):
        def list_foundation_models(self):
            raise RuntimeError("bedrock\nunavailable")

    summary = rollup(FakeLogs(), bedrock=Broken())

    assert summary["classify"] == "failed" and "model_purpose" not in model_row(aws, BETA)
    said = "could not list Bedrock's models to classify 1: RuntimeError('bedrock\\nunavailable')"
    assert f"evals_rollup: {said}" in capsys.readouterr().out


def test_a_purpose_another_job_writes_while_bedrock_is_listed_is_kept(aws):
    aws.Table("Models").update_item(Key={"model_id": BETA}, UpdateExpression="REMOVE model_purpose")

    class Raced(FakeBedrock):
        def list_foundation_models(self):
            # The classify job records the purpose after the rollup read the registry.
            aws.Table("Models").update_item(
                Key={"model_id": BETA},
                UpdateExpression="SET model_purpose = :p",
                ExpressionAttributeValues={":p": "image"},
            )
            return super().list_foundation_models()

    listed = {
        "modelId": BETA, "outputModalities": ["TEXT"], "inputModalities": ["TEXT"],
        "modelLifecycle": {"status": "ACTIVE"},
    }  # fmt: skip
    summary = rollup(FakeLogs(), bedrock=Raced([listed]))

    beta = model_row(aws, BETA)
    assert beta["model_purpose"] == "image" and "model_inputs" not in beta
    assert (summary["classify"], summary["classified"], summary["unknown"]) == ("done", 0, 0)


# --- excluded sources -----------------------------------------------------------------------------


def test_an_excluded_source_is_left_out_and_its_rows_deleted_when_the_day_is_added_up_again(aws):
    logs = FakeLogs()
    at = sydney("2026-10-27T09:00")
    completion(logs, "a1", DRAFT, at)
    judged(logs, "a1", "r1", -2, at, prompt_id="reviews/revision", prompt_version="v1")
    rollup(logs)
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["perf"] == 0
    assert row(aws, f"cell#{ALPHA}#reviews/revision#v1", "2026-10-27") is not None

    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "excluded_sources": ["judge"]})
    summary = rollup(logs)

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["perf"] == 2
    assert row(aws, f"cell#{ALPHA}#reviews/revision#v1", "2026-10-27") is None
    assert row(aws, "prompt#reviews/revision#v1", "2026-10-27") is None
    assert (summary["excluded"], summary["stale_rows"]) == (1, 2)


def test_an_excluded_source_since_a_time_keeps_what_came_before(aws):
    logs = FakeLogs()
    judged(logs, "a1", "r1", 2, sydney("2026-10-27T08:00"))
    judged(logs, "a2", "r2", -2, sydney("2026-10-27T10:00"))
    since = iso(sydney("2026-10-27T09:00"))
    aws.Table("ModelConfig").put_item(
        Item={"config_id": "evals", "excluded_sources": [{"source": "eval_jobs", "since": since}]}
    )

    summary = rollup(logs)

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["perf"] == 2
    assert summary["excluded"] == 1


def test_a_range_added_up_again_redoes_final_days_by_the_same_rule_and_leaves_the_watermark(aws):
    logs = FakeLogs()
    at = sydney("2026-10-25T09:00")
    completion(logs, "a1", DRAFT, at)
    judged(logs, "a1", "r1", -2, at)
    completion(logs, "late", DRAFT, sydney("2026-10-25T10:00"), logged_at=sydney("2026-10-27T09:00"))
    aws.Table("Evals").put_item(Item={**evals_rollup.INDEX_KEY, "last_rolled_day": "2026-10-24"})
    rollup(logs, now=sydney("2026-10-26T09:00"))
    assert index(aws)["last_rolled_day"] == "2026-10-25"
    late_before = rollup(logs)["late_dropped"]
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-25"))["perf"] == 0 and late_before == 1

    aws.Table("ModelConfig").put_item(Item={"config_id": "evals", "excluded_sources": ["judge"]})
    summary = rollup(logs, trigger="admin", day_from=date(2026, 10, 25), day_to=date(2026, 10, 25))

    assert plain(row(aws, f"model#{ALPHA}", "2026-10-25"))["perf"] == 2  # the late event still left out
    assert (summary["from_day"], summary["to_day"]) == ("2026-10-25", "2026-10-25")
    assert index(aws)["last_rolled_day"] == "2026-10-26"
    assert plain(index(aws))["late_dropped"] == 1


@pytest.mark.parametrize(
    ("day_from", "day_to", "problem"),
    [
        ("2026-10-20", None, None),
        ("2026-10-20", "2026-10-22", None),
        ("2026-07-01", "2026-07-02", None),
        ("2026-06-30", None, "118 days back"),
        ("2026-10-22", "2026-10-20", "must not be after"),
        ("2026-10-20", "2026-10-28", "after today"),
        ("20 October", None, "must be dates"),
        (None, "2026-10-22", "must be dates"),
    ],
)
def test_a_range_to_add_up_again_must_be_days_the_log_still_holds_whole(day_from, day_to, problem):
    found = evals_rollup.manual_range(day_from, day_to, NOW, SYDNEY)
    if problem is None:
        assert found == (date.fromisoformat(day_from), date.fromisoformat(day_to or "2026-10-27"))
    else:
        assert isinstance(found, str) and problem in found


def test_a_first_run_adds_up_what_the_log_still_holds_whole(aws):
    logs = FakeLogs()
    completion(logs, "kept", DRAFT, sydney("2026-07-01T09:00"))
    completion(logs, "expiring", DRAFT, sydney("2026-06-30T09:00"))

    summary = rollup(logs)

    assert summary["from_day"] == "2026-07-01" and row(aws, f"model#{ALPHA}", "2026-07-01")
    assert row(aws, f"model#{ALPHA}", "2026-06-30") is None


# --- reading the log ------------------------------------------------------------------------------


def test_a_window_of_ten_thousand_rows_is_split_and_every_event_counted(aws):
    logs = FakeLogs()
    start = sydney("2026-10-27T00:00")
    for n in range(10_001):
        rewrite(logs, f"rw{n}", "a1", start + timedelta(seconds=n))

    summary = rollup(logs)

    assert summary["splits"] >= 1 and summary["rows"] == summary["events"] == 10_001
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["events"] == 10_001
    assert {query["limit"] for query in logs.queries} == {10_000}
    # The windows read whole tile the first one: each starts the second after the one before it
    # ends, so no second is read twice or missed.
    first = logs.queries[0]
    assert first["returned"] == 10_000
    whole = sorted((query["start"], query["end"]) for query in logs.queries if query["returned"] < 10_000)
    assert whole[0][0] == first["start"] and whole[-1][1] == first["end"]
    assert all(later[0] == earlier[1] + 1 for earlier, later in zip(whole, whole[1:], strict=False))


def test_a_minute_that_alone_holds_ten_thousand_rows_raises_rather_than_splitting_forever(aws):
    logs = FakeLogs()
    # Halving a first run's 119 days to a minute takes under forty queries; a read that split
    # without end fails here, as a failed query, instead of hanging.
    logs.max_queries = 100
    at = sydney("2026-10-27T09:00")
    for n in range(10_000):
        rewrite(logs, f"rw{n}", "a1", at)

    with pytest.raises(evals_rollup.RollupError, match="minute"):
        rollup(logs)

    assert entities(aws) == set() and len(logs.queries) < logs.max_queries


def test_a_query_that_fails_raises_and_writes_nothing(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T09:00"))
    logs.fail = True

    with pytest.raises(evals_rollup.RollupError):
        rollup(logs)

    assert entities(aws) == set() and "daily_perf" not in model_row(aws, ALPHA)


def test_a_table_that_cannot_be_written_raises_rollup_error(aws, monkeypatch):
    monkeypatch.setenv("EVALS_TABLE", "NoSuchTable")
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T09:00"))

    with pytest.raises(evals_rollup.RollupError) as raised:
        rollup(logs)
    assert "ResourceNotFoundException" in repr(raised.value.__cause__)


# --- the judge ------------------------------------------------------------------------------------


def pair(aws, subject, judge=None, human=None):
    for sk, verdict in (("judge", judge), ("human", human)):
        if verdict is not None:
            item = {"entity": f"pair#{subject}", "sk": sk, "verdict": verdict, "delta": 2}
            aws.Table("Evals").put_item(Item=item)


def test_the_judges_agreement_comes_from_the_pair_rows_holding_both_verdicts(aws):
    pair(aws, "a1", "good", "good")
    pair(aws, "a2", "bad", "bad")
    pair(aws, "a3", "good", "bad")
    pair(aws, "a4", judge="good")
    pair(aws, "a5", human="bad")

    summary = rollup(FakeLogs())

    assert (summary["judge_agreement"], summary["judge_pairs"], summary["calibrated"]) == (0.6667, 3, False)
    assert plain(index(aws))["judge_agreement"] == 0.6667 and plain(index(aws))["judge_pairs"] == 3


def test_the_judges_verdicts_move_the_gated_windows_only_while_it_is_calibrated(aws):
    logs = FakeLogs()
    completion(logs, "a1", DRAFT, sydney("2026-10-27T08:00"))
    judged(logs, "a1", "r1", -2, sydney("2026-10-27T09:00"))
    rollup(logs)
    alpha = plain(model_row(aws, ALPHA))
    assert (alpha["daily_perf"], alpha["perf_events"]["daily"]) == (2, 1)

    # The thresholds are DynamoDB-only settings: set on the row itself.
    aws.Table("ModelConfig").put_item(
        Item={"config_id": "evals", "judge_min_pairs": 2, "judge_min_agreement": Decimal("0.5")}
    )
    pair(aws, "x1", "good", "good")
    pair(aws, "x2", "bad", "bad")
    assert rollup(logs)["calibrated"] is True

    alpha = plain(model_row(aws, ALPHA))
    assert (alpha["daily_perf"], alpha["perf_events"]["daily"]) == (0, 2)
    # The day row itself never changes with calibration: it holds every event.
    assert plain(row(aws, f"model#{ALPHA}", "2026-10-27"))["perf"] == 0


# --- the line it ends with ------------------------------------------------------------------------


def test_the_line_a_finished_run_prints_is_the_one_the_heartbeat_filter_counts(aws, capsys):
    """infra/modules/evals' rollup heartbeat counts log events matching `{ $.event = "rollup_done" }`:
    a JSON object whose `event` is rollup_done. eval-jobs prints in Lambda's text format, so the
    line is the whole log event, and must parse as that object."""
    main = (ROOT / "infra" / "modules" / "evals" / "main.tf").read_text(encoding="utf-8")
    block = main[main.index('resource "aws_cloudwatch_log_metric_filter" "rollups"') :]
    pattern = re.search(r'pattern\s*=\s*"(.*)"\n', block).group(1).replace('\\"', '"')
    assert pattern == '{ $.event = "rollup_done" }'
    wanted = re.fullmatch(r'\{ \$\.event = "(\w+)" \}', pattern).group(1)

    summary = rollup(FakeLogs())

    [line] = [text for text in capsys.readouterr().out.splitlines() if text.startswith(evidence.PREFIX)]
    assert json.loads(line)["event"] == wanted == "rollup_done"
    parsed = evidence.parse(line)
    assert {key: parsed.get(key) for key in summary if key in parsed} == {
        key: value for key, value in summary.items() if key in parsed
    }
    assert "dropped" not in parsed and wanted in evidence.EVENTS
