"""A person's review of research Findings (common/human_review.py; docs/enhancements/
model-ranch-prompt-assets-evals.md, sections 3's deviation 10, 4.3, 4.4 and 5.3, PR19): the admin API's
GET /evals/findings and POST /evals/findings/review, and the person's verdict an article's approve or
reject keeps (common/evals_emission.py).

What is held: the queue is the Findings no person has reviewed, alive, with a model to score, the
judge's first and then the rest, oldest first within each, and it never returns the judge's verdict
(nor the model that wrote the Finding); a verdict writes one Human Review event for the Finding's
research call, the `pair#<subject>` row `human` and the person's marks, with the judge's string set and
condition; a retried POST writes no second event, pair row or review_count, and one whose event could
not be written writes it later under the first POST's time; the other verdict on a reviewed Finding is
refused; while evals are off the verdict and marks are kept and no event is written; a person's approve
or reject keeps their verdict as the article's `pair#<article_id>` row `human`, and Professor
Grizzlewick's decisions keep none; both routes are in both environments' Terraform.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
import test_evals_emission as emission_tests
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from table_schemas import KEYS, create_table
from test_admin_api_handler import REGION, aws_env, aws_resources  # noqa: F401 - the fixtures, used by name
from test_evals_emission import stamped
from test_evals_emission_admin import ARTICLE_ID, _by_reviewer, _decide, held  # noqa: F401 - `held` by name

import admin_api_handler
from common import dynamo, eval_judge, evals, evals_emission, evals_rollup, human_review

# The emission tests' fixtures: evals on with every event captured, and evals off.
events = emission_tests.events
evals_off = emission_tests.evals_off

TOPIC = "rail-sydney"
OTHER_TOPIC = "github-trending"
# Not real models' names: only which is which matters.
AUTHOR = "maker.author-v1:0"
VIEW_KEYS = {"topic_id", "captured_at", "subject", "summary", "source_refs", "judged"}


@pytest.fixture(autouse=True)
def evals_table(aws_resources, monkeypatch):  # noqa: F811 - the fixture, by name
    monkeypatch.setenv("EVALS_TABLE", "Evals")
    hash_key, range_key = KEYS["Evals"]
    create_table(
        boto3.client("dynamodb", region_name=REGION),
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
    resource = boto3.resource("dynamodb", region_name=REGION)
    for topic_id in (TOPIC, OTHER_TOPIC):
        resource.Table("Topics").put_item(Item={"topic_id": topic_id, "name": topic_id})
    return resource.Table("Evals")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def ago(**delta) -> str:
    return _iso(datetime.now(UTC) - timedelta(**delta))


def put_finding(captured_at: str, *, topic_id=TOPIC, author=AUTHOR, summary=None, judged=None, **extra):
    """A Finding as research-tick writes it; `judged` ("good" or "bad"), as the judge leaves one it
    reviewed: its pair row and its marks."""
    item = {
        "topic_id": topic_id,
        "captured_at": captured_at,
        "expires_at": int((datetime.now(UTC) + timedelta(days=10)).timestamp()),
        "summary": summary or f"On-time running on the T1 line was 91.2% ({captured_at}).",
        "raw_snapshot_s3_key": f"snapshots/{topic_id}/{captured_at}.json",
        "source_refs": [
            {"title": "Transport data", "url": "https://example.com/t1", "accessed_at": captured_at}
        ],
        **extra,
    }
    if author is not None:
        item["research_call"] = {
            "model_id": author, "input_tokens": 900, "output_tokens": 120, "used_fallback": False,
            "prompt_id": "research_tick/summary", "prompt_version": "vanilla:v1",
        }  # fmt: skip
    if judged is not None:
        item.update(review_count=1, reviewed_by={eval_judge.ROLE}, last_reviewed_at=captured_at)
        pair = evals_emission.pair_row(f"{topic_id}#{captured_at}", eval_judge.ROLE, judged, captured_at)
        evals_table_resource().put_item(Item={**pair, "run_id": "judge-20261030T000000"})
    boto3.resource("dynamodb", region_name=REGION).Table("Findings").put_item(Item=item)
    return item


def evals_table_resource():
    return boto3.resource("dynamodb", region_name=REGION).Table("Evals")


def finding(captured_at: str, topic_id=TOPIC) -> dict:
    table = boto3.resource("dynamodb", region_name=REGION).Table("Findings")
    return table.get_item(Key={"topic_id": topic_id, "captured_at": captured_at})["Item"]


def pair_rows(subject: str) -> dict[str, dict]:
    """The subject's pair rows, by role."""
    items = evals_table_resource().query(KeyConditionExpression=Key("entity").eq(f"pair#{subject}"))["Items"]
    return {item["sk"]: dynamo._plain_numbers(item) for item in items}


def get_queue(count=None) -> tuple[int, dict]:
    event = {"routeKey": "GET /evals/findings"}
    if count is not None:
        event["queryStringParameters"] = {"count": str(count)}
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def post_review(body) -> tuple[int, dict]:
    raw = body if isinstance(body, str) else json.dumps(body)
    event = {"routeKey": "POST /evals/findings/review", "body": raw}
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


def verdict(captured_at: str, given: str, topic_id=TOPIC) -> dict:
    return {"topic_id": topic_id, "captured_at": captured_at, "verdict": given}


# --- the queue ------------------------------------------------------------------------------------


def test_the_queue_gives_the_judges_reviewed_findings_first_then_the_rest_oldest_first_within_each():
    unjudged_3d, judged_1d, judged_2d, other_5d = ago(days=3), ago(days=1), ago(days=2), ago(days=5)
    put_finding(unjudged_3d)
    put_finding(judged_1d, judged="bad")
    put_finding(judged_2d, judged="good")
    put_finding(other_5d, topic_id=OTHER_TOPIC)

    status, body = get_queue()

    assert status == 200 and body["count"] == 4
    order = [(item["topic_id"], item["captured_at"], item["judged"]) for item in body["findings"]]
    assert order == [
        (TOPIC, judged_2d, True), (TOPIC, judged_1d, True),
        (OTHER_TOPIC, other_5d, False), (TOPIC, unjudged_3d, False),
    ]  # fmt: skip


def test_the_queue_leaves_out_reviewed_expired_old_and_unscorable_findings():
    waiting = ago(hours=3)
    put_finding(waiting)
    put_finding(ago(hours=2), reviewed_by={"human"}, review_count=1)
    put_finding(ago(hours=4), reviewed_by={"judge", "human"}, review_count=2)
    put_finding(ago(hours=5), expires_at=int((datetime.now(UTC) - timedelta(minutes=5)).timestamp()))
    put_finding(ago(days=15))
    put_finding(ago(hours=6), author=None)
    put_finding(ago(hours=7), summary="   ")

    _, body = get_queue()

    assert [item["captured_at"] for item in body["findings"]] == [waiting]


def test_the_queue_never_returns_the_judges_verdict_or_the_model_that_wrote_the_finding():
    captured_at = ago(hours=5)
    put_finding(captured_at, judged="bad", summary="The T1 line ran on time on most mornings.")

    _, body = get_queue()

    [item] = body["findings"]
    assert set(item) == VIEW_KEYS
    assert item == {
        "topic_id": TOPIC, "captured_at": captured_at, "subject": f"{TOPIC}#{captured_at}",
        "summary": "The T1 line ran on time on most mornings.",
        "source_refs": [{"title": "Transport data", "url": "https://example.com/t1"}], "judged": True,
    }  # fmt: skip
    text = json.dumps(body)
    assert "verdict" not in text and "bad" not in text and AUTHOR not in text


def test_count_bounds_the_queue_and_one_out_of_range_is_refused():
    for hours in (1, 2, 3):
        put_finding(ago(hours=hours))

    assert len(get_queue(2)[1]["findings"]) == 2
    for count in (0, 51, "two"):
        status, body = get_queue(count)
        assert status == 400 and "'count' must be a whole number from 1 to 50" in body["error"]


# --- a verdict ------------------------------------------------------------------------------------


def test_a_verdict_writes_one_human_review_event_the_human_pair_row_and_the_findings_marks(events):
    captured_at = ago(hours=5)
    subject = f"{TOPIC}#{captured_at}"
    put_finding(captured_at, judged="good")

    status, body = post_review(verdict(captured_at, "bad"))

    assert status == 200
    ts = body["reviewed_at"]
    assert body == {
        "subject": subject, "verdict": "bad", "reviewed_at": ts, "already_reviewed": False,
        "event_id": f"human:{subject}:{ts}", "event": "written",
    }  # fmt: skip
    assert stamped(events) == [
        {
            "schema": "evals/1",
            "event_id": f"human:{subject}:{ts}",
            "model_id": AUTHOR,
            "prompt_id": "research_tick/summary",
            "prompt_version": "vanilla:v1",
            "performance": "bad",
            "rating_delta": -2,
            "reason": "Human Review",
            "subject": subject,
            "topic_id": TOPIC,
            "source": "admin_api",
            "by": "human",
        }
    ]
    human = pair_rows(subject)["human"]
    kept = {key: human[key] for key in ("verdict", "delta", "ts")}
    assert kept == {"verdict": "bad", "delta": -2, "ts": ts}
    marked = finding(captured_at)
    assert (marked["review_count"], marked["reviewed_by"]) == (2, {"judge", "human"})
    assert marked["last_reviewed_at"] == ts
    # The rollup pairs the two: the judge said good, the person bad.
    assert evals_rollup.judge_agreement(evals_table_resource()) == (0.0, 1)


def test_a_retried_post_writes_one_event_one_pair_row_and_one_review_count(events):
    captured_at = ago(hours=5)
    subject = f"{TOPIC}#{captured_at}"
    put_finding(captured_at)
    first = datetime.now(UTC).replace(microsecond=0)
    answers = []
    with patch.object(human_review, "datetime") as clock:
        clock.now.side_effect = [first, first + timedelta(seconds=7), first + timedelta(minutes=3)]
        for _ in range(3):
            answers.append(post_review(verdict(captured_at, "good")))

    assert [status for status, _ in answers] == [200, 200, 200]
    event_id = f"human:{subject}:{_iso(first)}"
    assert [(a["already_reviewed"], a["event"], a["event_id"]) for _, a in answers] == [
        (False, "written", event_id), (True, "written_before", event_id), (True, "written_before", event_id),
    ]  # fmt: skip
    assert [event["event_id"] for event in events] == [event_id]
    rows = pair_rows(subject)
    assert list(rows) == ["human"] and rows["human"]["ts"] == _iso(first)
    assert finding(captured_at)["review_count"] == 1


def test_a_retry_after_the_event_could_not_be_written_writes_it_once_under_the_first_posts_time(events):
    captured_at = ago(hours=5)
    subject = f"{TOPIC}#{captured_at}"
    put_finding(captured_at)
    down = {"Error": {"Code": "ServiceUnavailableException", "Message": "down"}}
    refused = ClientError(down, "PutLogEvents")
    first = datetime.now(UTC).replace(microsecond=0)
    with patch.object(human_review, "datetime") as clock:
        clock.now.side_effect = [first, first + timedelta(minutes=2), first + timedelta(minutes=4)]
        with patch.object(evals, "_put", side_effect=refused):
            _, failed = post_review(verdict(captured_at, "good"))
        assert "event_id" not in pair_rows(subject)["human"]  # the claim was let go
        _, retried = post_review(verdict(captured_at, "good"))
        _, again = post_review(verdict(captured_at, "good"))

    assert (failed["event"], retried["event"], again["event"]) == ("not_written", "written", "written_before")
    assert [event["event_id"] for event in events] == [f"human:{subject}:{_iso(first)}"]
    assert finding(captured_at)["review_count"] == 1


def test_the_other_verdict_on_a_finding_a_person_reviewed_is_refused_and_the_first_stands(events):
    captured_at = ago(hours=5)
    subject = f"{TOPIC}#{captured_at}"
    put_finding(captured_at)
    post_review(verdict(captured_at, "good"))

    status, body = post_review(verdict(captured_at, "bad"))

    assert status == 409 and "already reviewed as good" in body["error"]
    assert [event["performance"] for event in events] == ["good"]
    assert pair_rows(subject)["human"]["verdict"] == "good"
    assert finding(captured_at)["review_count"] == 1


def test_a_persons_marks_use_the_judges_string_set_under_its_condition(events):
    captured_at = ago(hours=5)
    put_finding(captured_at, judged="bad")
    table = eval_judge._findings_table()
    spy = MagicMock(wraps=table)
    spy.meta = table.meta

    with patch.object(eval_judge, "_findings_table", return_value=spy):
        post_review(verdict(captured_at, "good"))

    [call] = spy.update_item.call_args_list
    assert call.kwargs["UpdateExpression"] == (
        "ADD review_count :one, reviewed_by :roles SET last_reviewed_at = :now"
    )
    assert call.kwargs["ConditionExpression"] == (
        "attribute_exists(topic_id) AND "
        "(attribute_not_exists(reviewed_by) OR NOT contains(reviewed_by, :role))"
    )
    values = call.kwargs["ExpressionAttributeValues"]
    assert (values[":one"], values[":roles"], values[":role"]) == (1, {"human"}, "human")
    assert finding(captured_at)["reviewed_by"] == {"judge", "human"}


def test_while_evals_are_off_the_verdict_and_marks_are_kept_and_no_event_is_written(evals_off):
    captured_at = ago(hours=5)
    subject = f"{TOPIC}#{captured_at}"
    put_finding(captured_at)

    status, body = post_review(verdict(captured_at, "good"))

    assert status == 200 and body["event"] == "not_written"
    human = pair_rows(subject)["human"]
    assert human["verdict"] == "good" and "event_id" not in human
    assert finding(captured_at)["reviewed_by"] == {"human"}


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"topic_id": TOPIC, "captured_at": "2026-10-30T00:00:00+00:00"},
        {"topic_id": TOPIC, "captured_at": "2026-10-30T00:00:00+00:00", "verdict": "good", "note": "x"},
        {"topic_id": TOPIC, "captured_at": "2026-10-30T00:00:00+00:00", "verdict": "meh"},
        {"topic_id": TOPIC, "captured_at": "yesterday", "verdict": "good"},
        {"topic_id": "Not An Id", "captured_at": "2026-10-30T00:00:00+00:00", "verdict": "good"},
        ["not", "an", "object"],
        "{not json",
    ],
)
def test_a_body_that_is_not_one_verdict_on_one_finding_is_refused(events, body):
    status, answer = post_review(body)

    assert status == 400 and answer["error"]
    assert events == [] and evals_table_resource().scan()["Items"] == []


def test_a_verdict_on_a_finding_that_is_gone_or_names_no_model_is_refused(events):
    unscorable = ago(hours=5)
    put_finding(unscorable, author=None)

    gone_status, gone = post_review(verdict(ago(hours=6), "good"))
    unscorable_status, no_model = post_review(verdict(unscorable, "good"))

    assert gone_status == 404 and "no Finding" in gone["error"]
    assert unscorable_status == 409 and "no model" in no_model["error"]
    assert events == [] and evals_table_resource().scan()["Items"] == []


def test_the_pair_prefix_is_the_one_the_rollup_reads():
    assert evals_emission.PAIR == evals_rollup.PAIR
    assert human_review.ROLE == "human" and eval_judge.ROLE == "judge"


# --- a person's approve or reject is their verdict on the article -----------------------------------


@pytest.mark.usefixtures("held")
@pytest.mark.parametrize("route,verdict_given,delta", [("approve", "good", 2), ("reject", "bad", -2)])
def test_a_persons_approve_or_reject_keeps_their_verdict_as_the_articles_human_pair(
    events, route, verdict_given, delta
):
    status, _ = _decide(route)

    assert status == 200
    human = pair_rows(ARTICLE_ID)["human"]
    assert {key: human[key] for key in ("verdict", "delta", "queue_id")} == {
        "verdict": verdict_given, "delta": delta, "queue_id": "queue-1",
    }  # fmt: skip
    assert abs((datetime.now(UTC) - datetime.fromisoformat(human["ts"])).total_seconds()) < 60
    # Its Admin Review events are written as before.
    assert events and {event["reason"] for event in events} == {"Admin Review"}


@pytest.mark.usefixtures("held")
@pytest.mark.parametrize("route", ["approve", "reject"])
def test_professor_grizzlewicks_decisions_keep_no_human_pair(events, route):
    status, _ = _by_reviewer(route)

    assert status == 200
    assert pair_rows(ARTICLE_ID) == {} and events == []


@pytest.mark.usefixtures("held", "evals_off")
def test_no_human_pair_is_kept_while_evals_are_off():
    status, _ = _decide("approve")

    assert status == 200 and pair_rows(ARTICLE_ID) == {}


@pytest.mark.usefixtures("held")
def test_a_human_pair_that_cannot_be_written_leaves_the_decision_and_its_events(events, monkeypatch, capsys):
    monkeypatch.setenv("EVALS_TABLE", "NoSuchTable")

    status, body = _decide("reject")

    assert status == 200 and body == {"rejected": "queue-1", "article_id": ARTICLE_ID}
    assert events and pair_rows(ARTICLE_ID) == {}
    assert "could not keep queue_id=queue-1's decision as a human verdict" in capsys.readouterr().out


# --- the routes are declared where API Gateway reads them -------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_human_review_routes_are_in_the_environments_terraform(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    routes = {route for route in admin_api_handler._ROUTES if "/evals/findings" in route}
    assert routes == {"GET /evals/findings", "POST /evals/findings/review"}
    for route in routes:
        assert f'"{route}",' in admin_routes, route
