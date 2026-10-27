"""The dispatcher Lambda (dispatcher_handler.py): one tick of common/dispatch.py a minute, one
`dispatcher_tick` evidence line, and an exception out of the handler when the tick could not run, so
the function's Errors alarm fires.

Q1 deploys it in shadow mode: no topic has dispatch_mode "queue" until the per-topic cutover (Q2), so
the first tests hold that a tick over topics on their schedules sends nothing and starts nothing.
The rest drive a topic that is on the queue through the handler, with moto's DynamoDB and fake queue
and Step Functions clients, for what Q2 will switch on.

One DynamoDB rule moto does not keep: a comparison with an attribute the item lacks is false in
DynamoDB, while moto lets `<>` pass. The daily claim's `attribute_not_exists` half exists for exactly
that case, so the first-claim test here judges the claim's condition as DynamoDB does
(DynamoSemantics) rather than trusting moto, and the claim's exact strings are held word for word in
test_dispatch.py.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import boto3
import pytest
import test_admin_api_handler as admin_tests
from botocore.exceptions import ClientError

import admin_api_handler
import dispatcher_handler
from common import dispatch, dynamo, evidence
from common.dispatch import Claim, Dispatcher, DispatchError

REGION = "ap-southeast-2"
QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/work.fifo"
STATE_MACHINE_ARN = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"

# 09:05:30 Sydney on 2026-10-25 (AEDT, UTC+11): with one topic in slot 1 and the default stagger,
# the research heartbeat is at :05 and the daily cycle at 09:10 (as in test_dispatch.py).
RESEARCH_TURN = datetime(2026, 10, 24, 22, 5, 30, tzinfo=UTC)
DAILY_TURN = datetime(2026, 10, 24, 22, 10, 0, tzinfo=UTC)

aws_env = admin_tests.aws_env
aws_resources = admin_tests.aws_resources


@pytest.fixture
def world(aws_resources, monkeypatch):
    """The handler's environment and its two clients (fakes), and a clock the tick reads."""
    monkeypatch.setenv("WORK_QUEUE_URL", QUEUE_URL)
    monkeypatch.setenv("STATE_MACHINE_ARN", STATE_MACHINE_ARN)
    sqs, sfn = MagicMock(), MagicMock()
    monkeypatch.setattr(dispatcher_handler, "_sqs_client", sqs)
    monkeypatch.setattr(dispatcher_handler, "_sfn_client", sfn)
    clock = {"now": RESEARCH_TURN}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz is None else clock["now"].astimezone(tz)

    monkeypatch.setattr(dispatch, "datetime", _Clock)
    return {"sqs": sqs, "sfn": sfn, "clock": clock}


def topic(topic_id="github-trending", **fields) -> dict:
    """A topic as the admin API stores one today: no dispatch_mode, so on its two schedules."""
    row = {
        "topic_id": topic_id,
        "name": topic_id,
        "adapter": "github_trending",
        "research_cadence": "rate(1 hour)",
        "daily_cadence": "cron(0 9 * * ? *)",
        "daily_timezone": "Australia/Sydney",
        "stagger_slot": 1,
    }
    row.update(fields)
    return row


def topics_table():
    return boto3.resource("dynamodb", region_name=REGION).Table("Topics")


def put_topics(*rows) -> None:
    for row in rows:
        topics_table().put_item(Item=row)


def stored(topic_id="github-trending") -> dict:
    return topics_table().get_item(Key={"topic_id": topic_id}).get("Item") or {}


def ticks(out: str) -> list[dict]:
    """The dispatcher_tick evidence lines in printed output, parsed as a reader would."""
    lines = [evidence.parse(line) for line in out.splitlines()]
    return [line for line in lines if line and line["event"] == "dispatcher_tick"]


def run(context=None):
    return dispatcher_handler.handler({"trigger": "schedule"}, context)


# --- shadow mode: what Q1 deploys -------------------------------------------------------------------


def test_while_every_topic_is_on_its_schedules_the_dispatcher_sends_nothing_and_starts_nothing(world, capsys):
    """Q1's behaviour on dev and production: no topic has dispatch_mode "queue", so through a
    research turn, the daily turn and the hour after, no job is sent and no cycle started; the tick
    only records what it would have done."""
    put_topics(topic(), topic("hacker-news", stagger_slot=2, dispatch_mode="schedule"))

    for minute in range(0, 61):
        world["clock"]["now"] = RESEARCH_TURN + timedelta(minutes=minute)
        result = run()
        assert result["status"] == "ok" and result["dispatched"] == 0 and result["started"] == 0

    world["sqs"].send_message.assert_not_called()
    world["sfn"].start_execution.assert_not_called()
    row = stored()
    assert row["shadow_last_research_dispatch_at"] == "2026-10-24T23:05:30+00:00"  # the hour's next turn
    # Two topics: two heartbeats, then slot 1's daily cycle at 09:15 (common/stagger.py).
    assert row["shadow_last_daily_dispatch_at"] == "2026-10-24T22:15:30+00:00"
    assert "research_claimed_at" not in row and "last_daily_dispatched_on" not in row
    lines = ticks(capsys.readouterr().out)
    assert len(lines) == 61
    assert all(line["outcome"] == "ok" and line["dispatched"] == 0 and line["started"] == 0 for line in lines)
    assert lines[0]["considered"] == 2


def test_topics_schedule_shows_the_shadow_record_a_day_of_ticks_leaves(world):
    """Q1's acceptance reads it here: after a day, `topics schedule` shows the shadow fields, and the
    mismatches and unchecked days the cutover asks to be zero."""
    put_topics(topic())
    world["sfn"].list_executions.return_value = {"executions": []}
    for when in (DAILY_TURN, DAILY_TURN + timedelta(days=1)):
        world["clock"]["now"] = when
        run()

    listing = admin_api_handler.handler(admin_tests._event("GET /topics/schedule"), None)
    (row,) = json.loads(listing["body"])["topics"]

    assert row["shadow_last_daily_dispatch_at"] == "2026-10-25T22:10:00+00:00"
    assert row["shadow_mismatches"] == 1  # no execution of the topic on the first day
    assert row["shadow_unchecked"] == 0
    assert "shadow_last_research_dispatch_at" in row


def test_with_shadow_mode_off_and_no_topic_on_the_queue_a_tick_considers_nothing(world):
    dynamo.put_pipeline_config(dispatch_shadow=False)
    put_topics(topic())

    assert run() == {"status": "ok", "considered": 0, "dispatched": 0, "started": 0, "skipped_unparsable": 0}
    assert stored().keys() == topic().keys()


# --- a topic on the queue (what Q2 switches on) -------------------------------------------------------


def test_a_queue_topic_due_for_research_gets_one_job_with_the_exact_body_and_dedup_id(world, capsys):
    put_topics(topic(dispatch_mode="queue"))

    result = run()

    assert result == {"status": "ok", "considered": 1, "dispatched": 1, "started": 0, "skipped_unparsable": 0}
    world["sqs"].send_message.assert_called_once_with(
        QueueUrl=QUEUE_URL,
        MessageBody=json.dumps(
            {
                "topic_id": "github-trending",
                "job": "research",
                "force": False,
                "due_checked_at": "2026-10-24T22:05:30+00:00",
                "dispatched_at": "2026-10-24T22:05:30+00:00",
            }
        ),
        MessageGroupId="github-trending",
        MessageDeduplicationId="github-trending:research:2026-10-24T22:05",
    )
    assert stored()["research_claimed_at"] == "2026-10-24T22:05:30+00:00"
    (line,) = ticks(capsys.readouterr().out)
    assert line["dispatched"] == 1 and line["outcome"] == "ok"


def test_a_queue_topics_daily_cycle_is_started_with_its_execution_name_and_input(world):
    put_topics(topic(dispatch_mode="queue"))
    world["clock"]["now"] = DAILY_TURN

    result = run()

    assert result["started"] == 1
    world["sfn"].start_execution.assert_called_once_with(
        stateMachineArn=STATE_MACHINE_ARN,
        name="github-trending-2026-10-25-1",
        input='{"topic_id": "github-trending"}',
    )
    assert stored()["last_daily_dispatched_on"] == "2026-10-25" and stored()["daily_claim_n"] == 1


def test_an_execution_that_already_exists_is_consumed_and_the_tick_still_succeeds(world, capsys):
    put_topics(topic(dispatch_mode="queue"))
    world["clock"]["now"] = DAILY_TURN
    world["sfn"].start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists", "Message": "exists"}}, "StartExecution"
    )

    result = run()

    assert result["status"] == "ok" and result["started"] == 0
    assert stored()["last_daily_dispatched_on"] == "2026-10-25"  # consumed: not released, not retried
    out = capsys.readouterr().out
    assert "execution exists topic_id=github-trending name=github-trending-2026-10-25-1; consumed" in out
    assert ticks(out)[0]["outcome"] == "ok"


def test_a_failed_send_releases_the_claim_and_the_handler_raises_so_the_alarm_fires(world, capsys):
    put_topics(topic(dispatch_mode="queue"))
    world["sqs"].send_message.side_effect = ClientError(
        {"Error": {"Code": "ServiceUnavailable", "Message": "down"}}, "SendMessage"
    )

    with pytest.raises(DispatchError):
        run()

    assert "research_claimed_at" not in stored()  # released: the next tick tries again
    assert "last_tick" not in (dynamo.get_config_row("pipeline") or {})
    out = capsys.readouterr().out
    assert "dispatcher_handler: the tick failed: DispatchError(" in out
    (line,) = ticks(out)
    assert line["outcome"] == "error" and line["error_type"] == "DispatchError"
    assert "dispatched" not in line and "considered" not in line  # no counts from a tick that raised


def test_a_throttled_start_releases_the_day_and_raises(world):
    put_topics(topic(dispatch_mode="queue"))
    world["clock"]["now"] = DAILY_TURN
    world["sfn"].start_execution.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "StartExecution"
    )

    with pytest.raises(DispatchError):
        run()

    assert "last_daily_dispatched_on" not in stored()


def test_a_failed_topics_scan_raises_so_the_errors_alarm_fires(world, monkeypatch, capsys):
    monkeypatch.setenv("TOPICS_TABLE", "NoSuchTable")

    with pytest.raises(DispatchError):
        run()

    (line,) = ticks(capsys.readouterr().out)
    assert line["outcome"] == "error" and line["error_type"] == "DispatchError"


def test_one_topics_trouble_is_printed_and_the_tick_goes_on(world, capsys):
    dynamo.put_pipeline_config(stagger_minutes=0)
    put_topics(
        topic("alpha", dispatch_mode="queue", daily_timezone="Mars/Olympus_Mons"),
        topic("beta", dispatch_mode="queue", stagger_slot=2),
    )
    world["clock"]["now"] = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # 10:30 Sydney

    result = run()

    assert result["status"] == "ok" and result["considered"] == 2 and result["started"] == 1
    assert world["sfn"].start_execution.call_args.kwargs["name"] == "beta-2026-10-25-1"
    assert "dispatcher: topic_id=alpha skipped: ValueError(" in capsys.readouterr().out


def test_a_misconfigured_function_raises_rather_than_ticking_with_no_queue(world, monkeypatch):
    monkeypatch.delenv("WORK_QUEUE_URL")
    put_topics(topic(dispatch_mode="queue"))

    with pytest.raises(KeyError):
        run()

    world["sqs"].send_message.assert_not_called()


def test_the_line_carries_the_invocations_request_id_and_parses(world, capsys):
    put_topics(topic())
    context = MagicMock(aws_request_id="6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f")

    run(context)

    (line,) = ticks(capsys.readouterr().out)
    assert line["tick"] == "6f1c2d3e-4b5a-4c6d-8e7f-0a1b2c3d4e5f"
    assert re.fullmatch(r"[0-9a-f]{12}", line["run"])
    assert set(line) >= {"considered", "dispatched", "started", "skipped_unparsable", "outcome", "ms"}
    assert "dropped" not in line


def test_the_clients_are_made_once_per_container(monkeypatch):
    made = []
    monkeypatch.setattr(dispatcher_handler, "_sqs_client", None)
    monkeypatch.setattr(dispatcher_handler, "_sfn_client", None)
    monkeypatch.setattr(dispatcher_handler.boto3, "client", lambda name: made.append(name) or name)

    assert dispatcher_handler._clients() == ("sqs", "stepfunctions")
    assert dispatcher_handler._clients() == ("sqs", "stepfunctions")
    assert made == ["sqs", "stepfunctions"]


# --- the first claim, as DynamoDB judges it ----------------------------------------------------------

_TOKEN = re.compile(r"\s*(\(|\)|<>|<=|>=|=|<|>|:[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*)")


def _holds(expression: str, item: dict, values: dict) -> bool:
    """Whether a condition expression of the claims' grammar (AND, OR, parentheses,
    attribute_exists, attribute_not_exists, and comparisons of an attribute with a value) holds for
    `item`, by DynamoDB's rules: a comparison with an attribute the item lacks is false."""
    tokens = _TOKEN.findall(expression)
    assert "".join(tokens) == re.sub(r"\s+", "", expression), expression
    position = 0

    def take() -> str:
        nonlocal position
        position += 1
        return tokens[position - 1]

    def peek() -> str | None:
        return tokens[position] if position < len(tokens) else None

    def either() -> bool:
        result = both()
        while peek() == "OR":
            take()
            result = both() or result
        return result

    def both() -> bool:
        result = one()
        while peek() == "AND":
            take()
            result = one() and result
        return result

    def one() -> bool:
        token = take()
        if token == "(":
            result = either()
            assert take() == ")"
            return result
        if token in ("attribute_exists", "attribute_not_exists"):
            assert take() == "("
            name = take()
            assert take() == ")"
            return (name in item) is (token == "attribute_exists")
        operator, value = take(), values[take()]
        if token not in item:
            return False  # DynamoDB: no attribute, no comparison
        actual = item[token]
        return {
            "=": actual == value, "<>": actual != value, "<": actual < value,
            "<=": actual <= value, ">": actual > value, ">=": actual >= value,
        }[operator]  # fmt: skip

    result = either()
    assert peek() is None, expression
    return result


class DynamoSemantics:
    """The Topics table, with every conditional update's condition judged by DynamoDB's rules
    (_holds) before moto applies it."""

    def __init__(self, table):
        self._table = table

    def update_item(self, **kwargs):
        condition = kwargs.get("ConditionExpression")
        if condition:
            item = self._table.get_item(Key=kwargs["Key"]).get("Item") or {}
            if not _holds(condition, item, kwargs.get("ExpressionAttributeValues", {})):
                error = {"Error": {"Code": "ConditionalCheckFailedException", "Message": "condition"}}
                raise ClientError(error, "UpdateItem")
        return self._table.update_item(**kwargs)

    def __getattr__(self, name):
        return getattr(self._table, name)


def test_the_judge_keeps_dynamodbs_rule_that_a_missing_attribute_compares_false():
    assert _holds("last_daily_dispatched_on <> :today", {}, {":today": "2026-10-25"}) is False
    assert _holds(
        "attribute_not_exists(last_daily_dispatched_on) OR last_daily_dispatched_on <> :today",
        {},
        {":today": "2026-10-25"},
    )
    assert _holds("a = :v AND (b < :w OR attribute_exists(c))", {"a": 1, "c": 0}, {":v": 1, ":w": 2})


def test_the_first_daily_claim_on_a_never_claimed_topic_succeeds_as_dynamodb_judges_it(aws_resources):
    """Mutation check (spec Q1): drop the `attribute_not_exists` half of DAILY_CLAIM_CONDITION and this
    fails, where a moto-only test would still pass."""
    put_topics(topic(dispatch_mode="queue"))
    table = DynamoSemantics(topics_table())

    assert dispatch.claim_daily(table, "github-trending", "2026-10-25") == Claim(previous=None, n=1)
    assert dispatch.claim_daily(table, "github-trending", "2026-10-25") is None
    assert dispatch.claim_daily(table, "github-trending", "2026-10-26") == Claim(previous="2026-10-25", n=2)


def test_a_tick_starts_a_never_claimed_topics_first_cycle_as_dynamodb_judges_the_claim(aws_resources):
    put_topics(topic(dispatch_mode="queue"))
    sfn = MagicMock()
    run_ = Dispatcher(
        sqs=MagicMock(),
        sfn=sfn,
        queue_url=QUEUE_URL,
        state_machine_arn=STATE_MACHINE_ARN,
        topics_table=DynamoSemantics(topics_table()),
    )

    assert run_.tick(DAILY_TURN)["started"] == 1
    assert sfn.start_execution.call_args.kwargs["name"] == "github-trending-2026-10-25-1"
