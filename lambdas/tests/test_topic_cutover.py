"""The per-topic cutover (docs/enhancements/model-ranch-prompt-assets-evals.md, Q2): a topic's
`dispatch_mode` on the admin API, the pre-flight that refuses a move to the queue, the confirmation
before its two schedules are deleted, the cutover-day claim, the way back (one topic, and every
topic at once with `topics reschedule`), the cadence check for a topic on the queue, and manual runs
of a topic on the queue.

The cutover-day tests drive the dispatcher itself (common/dispatch.py) after the route, with moto's
DynamoDB and fake queue and Step Functions clients: what matters is the run it starts, or does not.
Times are Sydney's on 2026-10-25 (AEDT, UTC+11): one topic in slot 1 with the default stagger has its
research heartbeat at :05 and its daily cycle at 09:10 (common/stagger.py).
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
from test_admin_api_handler import _event

import admin_api_handler
from common import dispatch, dynamo, evidence
from common.dispatch import Dispatcher
from common.scheduler import topic_schedule_names

REGION = "ap-southeast-2"
QUEUE_URL = "https://sqs.ap-southeast-2.amazonaws.com/123456789012/work.fifo"
STATE_MACHINE_ARN = "arn:aws:states:ap-southeast-2:123456789012:stateMachine:daily-cycle"

aws_env = admin_tests.aws_env
aws_resources = admin_tests.aws_resources

# 2026-10-25 in Sydney. The daily turn is 09:10 local, 22:10 UTC the day before.
BEFORE_TURN = datetime(2026, 10, 24, 21, 0, tzinfo=UTC)  # 08:00
DAILY_TURN = datetime(2026, 10, 24, 22, 10, tzinfo=UTC)  # 09:10
AFTER_TURN = datetime(2026, 10, 24, 23, 0, tzinfo=UTC)  # 10:00
DAY = timedelta(days=1)


def ready(topic_id="github-trending", **fields) -> dict:
    """A topic on its schedules that shadow mode has watched cleanly: one checked day, no mismatch,
    no unchecked day, a research turn recorded, and yesterday's daily turn as the last daily record
    (so nothing but the cutover-day claim can stop today's run)."""
    row = {
        "topic_id": topic_id,
        "name": topic_id,
        "adapter": "github_trending",
        "adapter_config": {},
        "research_cadence": "rate(1 hour)",
        "daily_cadence": "cron(0 9 * * ? *)",
        "daily_timezone": "Australia/Sydney",
        "stagger_slot": 1,
        "shadow_last_research_dispatch_at": "2026-10-24T20:05:30+00:00",
        "shadow_last_daily_dispatch_at": "2026-10-23T22:10:30+00:00",
        "shadow_mismatches": 0,
        "shadow_unchecked": 0,
        "shadow_checked_days": 1,
    }
    row.update(fields)
    return {key: value for key, value in row.items() if value is not None}


def topics_table():
    return boto3.resource("dynamodb", region_name=REGION).Table("Topics")


def put_topics(*rows) -> None:
    for row in rows:
        topics_table().put_item(Item=row)


def stored(topic_id="github-trending") -> dict:
    return topics_table().get_item(Key={"topic_id": topic_id}).get("Item") or {}


@pytest.fixture
def world(aws_resources, monkeypatch):
    """The admin API with fake Scheduler calls, fake queue, Step Functions and Lambda clients, and a
    clock it reads; plus a dispatcher with its own fake Step Functions client, to count the runs."""
    monkeypatch.setenv("WORK_QUEUE_URL", QUEUE_URL)
    deleted, upserted = MagicMock(), MagicMock()
    monkeypatch.setattr(admin_api_handler, "delete_topic_schedules", deleted)
    monkeypatch.setattr(admin_api_handler, "upsert_topic_schedules", upserted)
    sqs, sfn, lambda_client = MagicMock(), MagicMock(), MagicMock()
    monkeypatch.setattr(admin_api_handler, "_get_sqs_client", lambda: sqs)
    monkeypatch.setattr(admin_api_handler, "_get_sfn_client", lambda: sfn)
    monkeypatch.setattr(admin_api_handler, "_get_lambda_client", lambda: lambda_client)
    clock = {"now": BEFORE_TURN}

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"] if tz is None else clock["now"].astimezone(tz)

    monkeypatch.setattr(admin_api_handler, "datetime", _Clock)
    # The dispatcher's daily check in shadow mode, for a tick while a topic is on its schedules:
    # yesterday's run of this topic, so a check answers and finds it.
    runs = MagicMock()
    runs.list_executions.return_value = {
        "executions": [
            {"executionArn": "arn:x:yesterday", "startDate": datetime(2026, 10, 23, 22, 10, 5, tzinfo=UTC)}
        ]
    }
    runs.describe_execution.return_value = {"input": json.dumps({"topic_id": "github-trending"})}
    return {
        "deleted": deleted,
        "upserted": upserted,
        "sqs": sqs,
        "sfn": sfn,
        "lambda": lambda_client,
        "clock": clock,
        "runs": runs,
        "dispatcher": Dispatcher(
            sqs=MagicMock(), sfn=runs, queue_url=QUEUE_URL, state_machine_arn=STATE_MACHINE_ARN
        ),
    }


def update(world, at: datetime, topic_id="github-trending", **body) -> tuple[int, dict]:
    world["clock"]["now"] = at
    result = admin_api_handler.handler(
        _event("PUT /topics/{topic_id}", path_params={"topic_id": topic_id}, body=body), None
    )
    return result["statusCode"], json.loads(result["body"])


def create(world, at: datetime, **body) -> tuple[int, dict]:
    world["clock"]["now"] = at
    result = admin_api_handler.handler(_event("POST /topics", body=body), None)
    return result["statusCode"], json.loads(result["body"])


def reschedule(**body) -> tuple[int, dict]:
    result = admin_api_handler.handler(_event("POST /topics/reschedule", body=body), None)
    return result["statusCode"], json.loads(result["body"])


def trigger(world, at: datetime, topic_id="github-trending", **body) -> tuple[int, dict]:
    world["clock"]["now"] = at
    result = admin_api_handler.handler(
        _event("POST /topics/{topic_id}/trigger", path_params={"topic_id": topic_id}, body=body), None
    )
    return result["statusCode"], json.loads(result["body"])


def tick(world, *moments: datetime) -> None:
    for moment in moments:
        world["dispatcher"].tick(moment)


def started(world) -> list[str]:
    """The names of the daily runs the dispatcher started, in order."""
    return [call.kwargs["name"] for call in world["runs"].start_execution.call_args_list]


# --- the move to the queue -------------------------------------------------------------------------


def test_a_topic_shadow_mode_watched_cleanly_moves_to_the_queue_once_confirmed(world):
    put_topics(ready())

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 200 and body["dispatch_mode"] == "queue"
    # Deleted before the row says queue (_move_to_queue), and once more after it, finding none.
    assert [deleted.args for deleted in world["deleted"].call_args_list] == [("github-trending",)] * 2
    world["upserted"].assert_not_called()  # a topic on the queue has no schedules
    row = stored()
    assert row["dispatch_mode"] == "queue"
    # What the dispatcher reads is still on the row, for `topics schedule`.
    assert row["research_schedule"] == "cron(5 * * * ? *)" and row["daily_schedule"] == "cron(10 9 * * ? *)"


def test_moving_to_the_queue_is_refused_until_confirmed_and_nothing_is_deleted_meanwhile(world):
    put_topics(ready())

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue")

    assert status == 409 and body["confirm_required"] is True
    assert body["schedules"] == topic_schedule_names("github-trending")
    assert all(name in body["error"] for name in body["schedules"])
    world["deleted"].assert_not_called()
    world["upserted"].assert_not_called()
    assert "dispatch_mode" not in stored()


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"paused": True}, "it is paused"),
        ({"shadow_mismatches": 2}, "shadow mode counted 2 mismatches with its schedules"),
        ({"shadow_unchecked": 1}, "shadow mode could not check 1 of its days"),
        ({"shadow_checked_days": 0}, "shadow mode has not yet watched a full day of it"),
        # A row from before the dispatcher counted checked days: never taken for a day watched.
        ({"shadow_checked_days": None}, "shadow mode has not yet watched a full day of it"),
        ({"shadow_checked_days": "lots"}, "its shadow record is unreadable (shadow_checked_days)"),
        ({"shadow_last_research_dispatch_at": None}, "shadow mode has recorded none of its research turns"),
        (
            {"daily_cadence": "cron(0 9 * 1 ? *)"},
            "the dispatcher cannot read its daily cadence 'cron(0 9 * 1 ? *)' (written as "
            "'cron(10 9 * 1 ? *)'): month '1': only '*' is read",
        ),
        (
            {"research_cadence": "at(2026-12-01T00:00:00)"},
            "the dispatcher cannot read its research cadence 'at(2026-12-01T00:00:00)': not a rate(...)",
        ),
        # The default stagger writes it as a cron, but a stagger of 0 (or a daily offset that comes
        # to 0) would hand the dispatcher the rate itself.
        (
            {"daily_cadence": "rate(1 day)"},
            "the dispatcher cannot read its daily cadence 'rate(1 day)': a rate; a daily cadence must be "
            "a cron",
        ),
        (
            {"daily_timezone": "Mars/Olympus"},
            "daily_timezone 'Mars/Olympus' is not a zone this runtime knows",
        ),
    ],
    ids=[
        "paused",
        "mismatches",
        "unchecked_days",
        "no_checked_day",
        "checked_days_never_counted",
        "unreadable_count",
        "no_research_turn_recorded",
        "daily_cadence_unreadable",
        "research_cadence_unreadable",
        "daily_rate_hidden_by_the_stagger",
        "zone_unknown",
    ],
)
def test_the_pre_flight_refuses_a_move_to_the_queue_with_its_reason(world, fields, reason):
    put_topics(ready(**fields))
    before = stored()

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 400
    assert reason in body["error"] and any(reason in line for line in body["reasons"])
    world["deleted"].assert_not_called()
    assert stored() == before


def test_every_reason_is_given_at_once(world):
    put_topics(ready(paused=True, shadow_mismatches=1, shadow_unchecked=3))

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 400 and len(body["reasons"]) == 3
    assert body["error"].startswith("topic 'github-trending' cannot move to the queue: it is paused")


def test_a_topic_whose_slug_another_topic_shares_is_refused(world):
    # Both are github-trending as a slug: their daily runs would take each other's names.
    put_topics(ready(), ready("GitHub_Trending", stagger_slot=2))

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 400
    assert "its slug 'github-trending' is also topic 'GitHub_Trending''s" in body["error"]


def test_a_topic_whose_slug_has_no_letter_or_digit_is_refused(world):
    put_topics(ready("___"))

    status, body = update(world, BEFORE_TURN, topic_id="___", dispatch_mode="queue", confirm=True)

    assert status == 400 and "its slug '---' (the start of its daily runs' execution names)" in body["error"]


# A trailing newline: its slug ('rail-') is fine, so only the queue's own rule can refuse it.
@pytest.mark.parametrize("topic_id", ["rail and sea", "rail\n"], ids=["space", "trailing_newline"])
def test_a_topic_id_the_work_queue_cannot_take_as_a_message_group_is_refused(world, topic_id):
    put_topics(ready(topic_id))

    status, body = update(world, BEFORE_TURN, topic_id=topic_id, dispatch_mode="queue", confirm=True)

    assert status == 400 and "its topic id cannot be the work queue's message group id" in body["error"]
    world["deleted"].assert_not_called()


def test_a_topic_id_too_long_for_the_manual_deduplication_id_is_refused(world):
    long_id = "a" * 80  # its slug is 'a' * 40, which no other topic here has
    put_topics(ready(long_id))

    status, body = update(world, BEFORE_TURN, topic_id=long_id, dispatch_mode="queue", confirm=True)

    assert status == 400 and "at most 79 letters, digits and punctuation" in body["error"]
    assert dispatch.queue_id_problem("a" * 79) is None


def test_a_rate_research_cadence_is_judged_by_its_parse_and_its_daily_record_alone(world):
    """rate(30 minutes) stays a rate through the stagger: shadow mode records its turns but never
    judges them, so no research turn recorded is no refusal; a daily mismatch still is."""
    put_topics(ready(research_cadence="rate(30 minutes)", shadow_last_research_dispatch_at=None))

    status, _ = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)
    assert status == 200

    put_topics(ready("hacker-news", stagger_slot=2, research_cadence="rate(30 minutes)", shadow_mismatches=1))
    status, body = update(world, BEFORE_TURN, topic_id="hacker-news", dispatch_mode="queue", confirm=True)
    assert status == 400
    assert body["reasons"] == [
        "shadow mode counted 1 mismatch with its schedules (`topics schedule` shows the record)"
    ]


def test_a_delete_that_fails_leaves_the_topic_on_its_schedules_and_says_so(world):
    put_topics(ready())
    world["deleted"].side_effect = RuntimeError("Scheduler throttled")

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 502 and "stays on them" in body["error"]
    assert "dispatch_mode" not in stored()


@pytest.mark.parametrize("value", ["Queue", "", None, 1])
def test_an_unknown_dispatch_mode_is_refused_on_update_and_create(world, value):
    put_topics(ready())

    status, body = update(world, BEFORE_TURN, dispatch_mode=value)
    assert status == 400 and body["error"] == "'dispatch_mode' must be 'schedule' or 'queue'"

    status, body = create(world, BEFORE_TURN, topic_id="new", name="New", dispatch_mode=value)
    assert status == 400 and body["error"] == "'dispatch_mode' must be 'schedule' or 'queue'"
    assert stored("new") == {}


def test_confirm_must_be_a_boolean(world):
    put_topics(ready())

    status, body = update(world, BEFORE_TURN, dispatch_mode="queue", confirm="yes")

    assert status == 400 and "'confirm' must be a boolean" in body["error"]
    world["deleted"].assert_not_called()


# --- the cutover day --------------------------------------------------------------------------------


def test_flipped_after_the_daily_turn_it_starts_nothing_today_and_its_first_run_tomorrow(world):
    put_topics(ready())

    status, body = update(world, AFTER_TURN, dispatch_mode="queue", confirm=True)

    assert status == 200
    row = stored()
    assert row["last_daily_dispatched_on"] == "2026-10-25" and "daily_claim_n" not in row
    last_minute_of_the_day = datetime(2026, 10, 25, 12, 59, tzinfo=UTC)  # 23:59 in Sydney
    tick(world, AFTER_TURN + timedelta(minutes=1), AFTER_TURN + timedelta(hours=5), last_minute_of_the_day)
    assert started(world) == []
    tick(world, DAILY_TURN + DAY)
    assert started(world) == ["github-trending-2026-10-26-1"]


def test_flipped_before_the_daily_turn_it_starts_once_today(world):
    put_topics(ready())

    status, _ = update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)

    assert status == 200 and "last_daily_dispatched_on" not in stored()
    tick(world, DAILY_TURN - timedelta(minutes=1), DAILY_TURN, DAILY_TURN + timedelta(minutes=1), AFTER_TURN)
    assert started(world) == ["github-trending-2026-10-25-1"]


def test_the_cutover_day_claim_leaves_the_claim_count_as_it_is(world):
    put_topics(ready(daily_claim_n=4, last_daily_dispatched_on="2026-10-20"))

    update(world, AFTER_TURN, dispatch_mode="queue", confirm=True)

    row = stored()
    assert row["daily_claim_n"] == 4 and row["last_daily_dispatched_on"] == "2026-10-25"
    tick(world, AFTER_TURN + timedelta(minutes=1), DAILY_TURN + DAY)
    assert started(world) == ["github-trending-2026-10-26-5"]


def test_queue_schedule_queue_after_the_daily_turn_starts_no_second_run(world):
    put_topics(ready())

    # Day one: on the queue before the turn; the dispatcher starts the day's run, then the topic
    # goes back to its schedules and on to the queue again the same morning.
    assert update(world, BEFORE_TURN, dispatch_mode="queue", confirm=True)[0] == 200
    tick(world, DAILY_TURN)
    assert update(world, AFTER_TURN, dispatch_mode="schedule")[0] == 200
    tick(world, AFTER_TURN + timedelta(minutes=30))  # on its schedules: shadow mode only
    assert update(world, AFTER_TURN + timedelta(hours=1), dispatch_mode="queue", confirm=True)[0] == 200
    tick(world, AFTER_TURN + timedelta(hours=1, minutes=1), AFTER_TURN + timedelta(hours=8))
    assert started(world) == ["github-trending-2026-10-25-1"]

    # Day two: back on its schedules before the turn, so the Scheduler runs the day (no tick sees
    # it, as with shadow mode off), then on to the queue after the turn: no second run that day.
    assert update(world, BEFORE_TURN + DAY, dispatch_mode="schedule")[0] == 200
    assert update(world, AFTER_TURN + DAY, dispatch_mode="queue", confirm=True)[0] == 200
    assert stored()["last_daily_dispatched_on"] == "2026-10-26"
    tick(world, AFTER_TURN + DAY + timedelta(minutes=1), AFTER_TURN + DAY + timedelta(hours=8))
    assert started(world) == ["github-trending-2026-10-25-1"]
    tick(world, DAILY_TURN + 2 * DAY)
    assert started(world) == ["github-trending-2026-10-25-1", "github-trending-2026-10-27-2"]


# --- the way back ------------------------------------------------------------------------------------


def test_moving_back_to_the_schedules_recreates_both_without_asking(world):
    put_topics(ready(dispatch_mode="queue"))

    status, body = update(world, AFTER_TURN, dispatch_mode="schedule")

    assert status == 200 and body["dispatch_mode"] == "schedule"
    world["upserted"].assert_called_once_with(
        "github-trending", "cron(5 * * * ? *)", "cron(10 9 * * ? *)", "Australia/Sydney", enabled=True
    )
    world["deleted"].assert_not_called()
    assert stored()["dispatch_mode"] == "schedule"


def test_a_topic_whose_schedules_fail_on_the_way_back_is_told_nothing_may_be_running_it(world):
    put_topics(ready(dispatch_mode="queue"))
    mode_at_upsert = []

    def throttled(topic_id, *args, **kwargs):
        mode_at_upsert.append(stored(topic_id).get("dispatch_mode"))
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "CreateSchedule")

    world["upserted"].side_effect = throttled

    status, body = update(world, AFTER_TURN, dispatch_mode="schedule")

    assert status == 502
    assert "is off the queue (its row says schedule)" in body["error"]
    assert "nothing may be running it" in body["error"]
    assert "`topics update github-trending --dispatch-mode schedule` again" in body["error"]
    # The row first, so the dispatcher had let go of it before any schedule was written.
    assert mode_at_upsert == ["schedule"] and stored()["dispatch_mode"] == "schedule"

    world["upserted"].side_effect = None
    status, _ = update(world, AFTER_TURN, dispatch_mode="schedule")
    assert status == 200 and world["upserted"].call_count == 2


# Weeks before the undo: the topic's last shadow daily turn before it went on the queue.
STALE_SHADOW_DAY = "2026-10-02T22:10:30+00:00"
# A page of the day-cycle's executions, every one newer than that day (some 100 topics' runs a day).
NEWER_RUNS = [
    {"executionArn": f"arn:x:run-{n}", "startDate": DAILY_TURN - timedelta(minutes=n)} for n in range(100)
]


@pytest.mark.parametrize(
    "pages",
    [
        {"executions": NEWER_RUNS, "nextToken": "more"},  # the check runs out of pages: unchecked
        {"executions": NEWER_RUNS},  # the list ends before that day: a mismatch
    ],
    ids=["pages_run_out", "list_ends"],
)
@pytest.mark.parametrize("way_back", ["one_topic", "every_topic"])
def test_after_the_way_back_the_first_shadow_day_asks_nothing_about_the_day_before_the_cutover(
    world, way_back, pages
):
    put_topics(ready(dispatch_mode="queue", shadow_last_daily_dispatch_at=STALE_SHADOW_DAY))

    if way_back == "one_topic":
        assert update(world, BEFORE_TURN, dispatch_mode="schedule")[0] == 200
    else:
        status, body = reschedule(dispatch_mode="schedule")
        assert status == 200 and body["moved_to_schedule"] == ["github-trending"]
        assert body["topics"][0]["shadow_last_daily_dispatch_at"] is None  # as stored
    assert "shadow_last_daily_dispatch_at" not in stored()
    world["runs"].list_executions.return_value = pages

    tick(world, DAILY_TURN)  # on its schedules: shadow mode records today's turn

    row = stored()
    world["runs"].list_executions.assert_not_called()
    assert row["shadow_unchecked"] == 0 and row["shadow_mismatches"] == 0
    assert row["shadow_checked_days"] == 1  # the record from before the cutover still counts
    assert update(world, AFTER_TURN, dispatch_mode="queue", confirm=True)[0] == 200


def test_a_topic_whose_row_the_bulk_undo_could_not_write_stays_on_the_queue_and_is_not_reported_moved(
    world, monkeypatch, capsys
):
    put_topics(ready("alpha", dispatch_mode="queue"), ready("bravo", stagger_slot=2, dispatch_mode="queue"))
    write = admin_api_handler.set_topic_fields

    def throttled_for_alpha(topic_id, fields, **kwargs):
        if topic_id == "alpha":
            raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")
        return write(topic_id, fields, **kwargs)

    monkeypatch.setattr(admin_api_handler, "set_topic_fields", throttled_for_alpha)

    status, body = reschedule(dispatch_mode="schedule")

    assert status == 200 and body["moved_to_schedule"] == ["bravo"]
    alpha, bravo = body["topics"]
    assert alpha["dispatch_mode"] == "queue" and "ProvisionedThroughputExceededException" in alpha["error"]
    assert "off the queue" not in alpha["error"]
    assert bravo["dispatch_mode"] == "schedule" and "error" not in bravo
    assert stored("alpha")["dispatch_mode"] == "queue" and stored("bravo")["dispatch_mode"] == "schedule"
    assert [upsert.args[0] for upsert in world["upserted"].call_args_list] == ["bravo"]
    lines = [evidence.parse(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["topic_id"] for line in lines if line and line["event"] == "topic_changed"] == ["bravo"]


def test_a_topic_whose_schedules_fail_in_the_bulk_undo_is_told_nothing_may_be_running_it(world):
    put_topics(
        ready("alpha", dispatch_mode="queue"),
        ready("bravo", stagger_slot=2, dispatch_mode="queue"),
        ready("charlie", stagger_slot=3),
    )
    mode_at_upsert = {}

    def throttled_for_alpha(topic_id, *args, **kwargs):
        mode_at_upsert[topic_id] = stored(topic_id).get("dispatch_mode")
        if topic_id == "alpha":
            raise ClientError({"Error": {"Code": "ThrottlingException"}}, "CreateSchedule")

    world["upserted"].side_effect = throttled_for_alpha

    status, body = reschedule(dispatch_mode="schedule")

    assert status == 200 and body["moved_to_schedule"] == ["alpha", "bravo"]
    alpha = body["topics"][0]
    assert alpha["dispatch_mode"] == "schedule"
    assert "it is off the queue" in alpha["error"] and "nothing may be running it" in alpha["error"]
    assert "ThrottlingException" in alpha["error"]
    assert all("error" not in row for row in body["topics"][1:])
    assert stored("alpha")["dispatch_mode"] == "schedule"
    # Each row said schedule before its schedules were written.
    assert mode_at_upsert == {"alpha": "schedule", "bravo": "schedule", "charlie": None}

    world["upserted"].side_effect = None
    world["upserted"].reset_mock()
    reschedule()
    assert "alpha" in [upsert.args[0] for upsert in world["upserted"].call_args_list]


def test_the_bulk_undo_moves_every_topic_on_the_queue_back_and_writes_their_schedules(world, capsys):
    put_topics(
        ready("alpha", dispatch_mode="queue"),
        ready("bravo", stagger_slot=2, dispatch_mode="queue", paused=True),
        ready("charlie", stagger_slot=3),
    )

    status, body = reschedule(dispatch_mode="schedule")

    assert status == 200 and body["moved_to_schedule"] == ["alpha", "bravo"]
    assert [row["dispatch_mode"] for row in body["topics"]] == ["schedule"] * 3
    assert all(stored(topic_id)["dispatch_mode"] == "schedule" for topic_id in ("alpha", "bravo"))
    assert "dispatch_mode" not in stored("charlie")
    written = {call.args[0]: call.kwargs["enabled"] for call in world["upserted"].call_args_list}
    assert written == {"alpha": True, "bravo": False, "charlie": True}  # a paused topic's are disabled
    lines = [evidence.parse(line) for line in capsys.readouterr().out.splitlines()]
    changed = [line for line in lines if line and line["event"] == "topic_changed"]
    assert [line["topic_id"] for line in changed] == ["alpha", "bravo"]
    assert all("dispatch_mode" in line["fields"] for line in changed)


def test_the_bulk_undo_as_a_dry_run_changes_nothing(world):
    put_topics(ready("alpha", dispatch_mode="queue"), ready("charlie", stagger_slot=3))

    status, body = reschedule(dispatch_mode="schedule", dry_run=True)

    assert status == 200 and body["dry_run"] is True and body["moved_to_schedule"] == ["alpha"]
    assert stored("alpha")["dispatch_mode"] == "queue"
    assert stored("alpha")["shadow_last_daily_dispatch_at"] == ready()["shadow_last_daily_dispatch_at"]
    world["upserted"].assert_not_called()


def test_the_bulk_undo_never_moves_topics_to_the_queue(world):
    put_topics(ready())

    status, body = reschedule(dispatch_mode="queue")

    assert status == 400 and "may only be 'schedule' here" in body["error"]
    world["upserted"].assert_not_called()


def test_rescheduling_without_the_undo_writes_no_schedules_for_a_topic_on_the_queue_and_removes_any_left(
    world,
):
    put_topics(ready("alpha", dispatch_mode="queue"), ready("charlie", stagger_slot=3))
    dynamo.put_pipeline_config(stagger_minutes=10)

    status, body = reschedule()

    assert status == 200 and "moved_to_schedule" not in body
    assert [call.args[0] for call in world["upserted"].call_args_list] == ["charlie"]
    world["deleted"].assert_called_once_with("alpha")
    assert stored("alpha")["dispatch_mode"] == "queue"
    # The row still records what the dispatcher reads with the new stagger.
    assert stored("alpha")["research_schedule"] == "cron(10 * * * ? *)"


# --- the cadence check for a topic on the queue ----------------------------------------------------


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"daily_cadence": "cron(0 9 * 1 ? *)"}, "month '1': only '*' is read"),
        ({"research_cadence": "at(2026-12-01T00:00:00)"}, "not a rate(...) or a six-field cron(...)"),
        ({"daily_cadence": "cron(0 9 ? * FRI-MON *)"}, "runs backwards; a weekday range does not wrap"),
        ({"daily_timezone": "Mars/Olympus"}, "not a zone this runtime knows"),
    ],
)
def test_a_topic_on_the_queue_is_refused_a_cadence_the_dispatcher_cannot_read(world, change, reason):
    put_topics(ready(dispatch_mode="queue"))
    before = stored()

    status, body = update(world, AFTER_TURN, **change)

    assert status == 400 and reason in body["error"]
    assert stored() == before


def test_a_topic_on_the_queue_is_refused_a_daily_rate(world):
    put_topics(ready(dispatch_mode="queue"))

    status, body = update(world, AFTER_TURN, daily_cadence="rate(1 day)")

    assert status == 400
    assert body["error"] == (
        "the dispatcher cannot read its daily cadence 'rate(1 day)': a rate; a daily cadence must be a cron"
    )


def test_rescheduling_names_a_topic_on_the_queue_the_dispatcher_cannot_read_and_leaves_its_schedules(world):
    # Put in DynamoDB straight, as a row from before this check (or a restore) could hold it.
    put_topics(
        ready("alpha", dispatch_mode="queue", daily_cadence="rate(1 day)"), ready("charlie", stagger_slot=3)
    )

    status, body = reschedule()

    assert status == 200
    alpha, charlie = body["topics"]
    assert alpha["error"] == (
        "the dispatcher cannot read its daily cadence 'rate(1 day)': a rate; a daily cadence must be a cron"
    )
    assert "error" not in charlie
    assert [upsert.args[0] for upsert in world["upserted"].call_args_list] == ["charlie"]
    world["deleted"].assert_not_called()  # the dispatcher skips it: its schedules are all that run it


def test_a_topic_on_its_schedules_keeps_the_scheduler_s_check_only(world):
    put_topics(ready())

    status, _ = update(world, AFTER_TURN, daily_cadence="cron(0 9 * 1 ? *)")

    assert status == 200
    world["upserted"].assert_called_once()


def test_an_ordinary_edit_of_a_topic_on_the_queue_writes_no_schedules_and_asks_nothing(world):
    put_topics(ready(dispatch_mode="queue"))

    status, body = update(world, AFTER_TURN, name="GitHub Trending")

    assert status == 200 and body["name"] == "GitHub Trending" and body["dispatch_mode"] == "queue"
    world["upserted"].assert_not_called()
    world["deleted"].assert_called_once_with("github-trending")  # any it still had; none is fine
    assert "last_daily_dispatched_on" not in stored()  # only a move to the queue claims the day


# --- a new topic on the queue ----------------------------------------------------------------------


def test_a_new_topic_can_start_on_the_queue_with_no_schedules(world):
    status, body = create(world, BEFORE_TURN, topic_id="new-topic", name="New", dispatch_mode="queue")

    assert status == 201 and body["dispatch_mode"] == "queue"
    assert "last_daily_dispatched_on" not in body  # its turn is still to come today
    world["upserted"].assert_not_called()
    assert stored("new-topic")["daily_schedule"] == "cron(10 9 * * ? *)"


def test_a_new_topic_started_on_the_queue_after_its_daily_turn_waits_for_tomorrow(world):
    status, body = create(world, AFTER_TURN, topic_id="new-topic", name="New", dispatch_mode="queue")

    assert status == 201 and body["last_daily_dispatched_on"] == "2026-10-25"


def test_a_new_topic_on_the_queue_is_refused_a_cadence_the_dispatcher_cannot_read(world):
    status, body = create(
        world, BEFORE_TURN, topic_id="new-topic", name="New", dispatch_mode="queue",
        daily_cadence="cron(0 9 * 1 ? *)",
    )

    assert status == 400
    assert body["error"].startswith("topic 'new-topic' cannot start on the queue: the dispatcher cannot read")
    assert stored("new-topic") == {}


def test_a_new_topic_on_the_queue_is_refused_a_slug_another_topic_has(world):
    put_topics(ready())

    status, body = create(world, BEFORE_TURN, topic_id="GitHub-Trending", name="Twin", dispatch_mode="queue")

    assert status == 400 and "is also topic 'github-trending''s" in body["error"]
    assert stored("GitHub-Trending") == {}


def test_a_new_topic_on_the_queue_is_refused_a_daily_rate_whatever_the_stagger(world):
    status, body = create(
        world, BEFORE_TURN, topic_id="new-topic", name="New", dispatch_mode="queue",
        daily_cadence="rate(1 day)",
    )

    assert status == 400
    assert "the dispatcher cannot read its daily cadence 'rate(1 day)'" in body["error"]
    assert stored("new-topic") == {}


def _room_for_a_topic_id() -> int:
    """How long a topic id may be before its longer schedule name passes EventBridge Scheduler's 64
    characters, with this test environment's prefix."""
    return 64 - max(len(name) for name in topic_schedule_names(""))


@pytest.mark.parametrize("too_long", [False, True], ids=["a_character_schedule_names_refuse", "too_long"])
def test_a_new_topic_on_the_queue_is_refused_an_id_its_schedules_could_never_be_named_by(world, too_long):
    # The environment's prefix is known only once the fixture has set it.
    topic_id = "a" * (_room_for_a_topic_id() + 1) if too_long else "rail:sea"

    status, body = create(world, BEFORE_TURN, topic_id=topic_id, name="New", dispatch_mode="queue")

    assert status == 400
    assert "its topic id could never go back to its schedules" in body["error"]
    assert f"at most {_room_for_a_topic_id()} characters here" in body["error"]
    assert stored(topic_id) == {}


def test_a_new_topic_on_the_queue_may_have_an_id_that_brings_its_schedule_name_to_64_characters(world):
    topic_id = "a" * _room_for_a_topic_id()
    assert max(len(name) for name in topic_schedule_names(topic_id)) == 64

    status, _ = create(world, BEFORE_TURN, topic_id=topic_id, name="New", dispatch_mode="queue")

    assert status == 201 and stored(topic_id)["dispatch_mode"] == "queue"


# --- a restore -------------------------------------------------------------------------------------


def test_a_restore_of_a_topic_on_the_queue_removes_the_schedules_it_went_back_to(world):
    """A snapshot taken while the topic was on the queue, restored after it went back to its
    schedules: the load phase writes the row saying queue, and the schedules phase must not leave
    the Scheduler running it too."""
    row = ready(dispatch_mode="queue")
    put_topics(row)

    assert admin_api_handler._restore_schedules([row]) == {"written": 1, "failed": []}

    world["deleted"].assert_called_once_with("github-trending")
    world["upserted"].assert_not_called()


# --- manual runs -------------------------------------------------------------------------------------


def test_a_manual_research_run_of_a_topic_on_the_queue_is_a_forced_job_on_the_work_queue(world):
    put_topics(ready(dispatch_mode="queue"))

    status, body = trigger(world, AFTER_TURN, pipeline="research_tick")

    assert status == 202 and body["dispatch_mode"] == "queue"
    world["lambda"].invoke.assert_not_called()
    sent = world["sqs"].send_message.call_args.kwargs
    assert sent["QueueUrl"] == QUEUE_URL and sent["MessageGroupId"] == "github-trending"
    assert json.loads(sent["MessageBody"]) == {
        "topic_id": "github-trending",
        "job": "research",
        "force": True,
        "due_checked_at": "2026-10-24T23:00:00+00:00",
        "dispatched_at": "2026-10-24T23:00:00+00:00",
    }
    assert re.fullmatch(r"github-trending:research:manual:[0-9a-f]{32}", sent["MessageDeduplicationId"])
    assert body["deduplication_id"] == sent["MessageDeduplicationId"]


def test_two_manual_research_runs_in_the_same_minute_are_two_jobs(world):
    put_topics(ready(dispatch_mode="queue"))

    trigger(world, AFTER_TURN, pipeline="research_tick")
    trigger(world, AFTER_TURN, pipeline="research_tick")

    ids = {call.kwargs["MessageDeduplicationId"] for call in world["sqs"].send_message.call_args_list}
    assert len(ids) == 2
    assert dispatch.research_dedup_id("github-trending", AFTER_TURN) not in ids  # nor the dispatcher's


@pytest.mark.parametrize(
    ("force", "sent_input"),
    [(False, {"topic_id": "github-trending"}), (True, {"topic_id": "github-trending", "force": True})],
)
def test_a_manual_daily_run_of_a_topic_on_the_queue_starts_a_manually_named_execution(
    world, force, sent_input
):
    put_topics(ready(dispatch_mode="queue"))
    body = {"pipeline": "daily_cycle", **({"force": True} if force else {})}

    status, answer = trigger(world, AFTER_TURN + timedelta(seconds=7), **body)

    assert status == 202 and answer["execution_name"] == "github-trending-2026-10-25-manual-100007"
    world["lambda"].invoke.assert_not_called()
    call = world["sfn"].start_execution.call_args.kwargs
    assert call["stateMachineArn"] == STATE_MACHINE_ARN
    assert call["name"] == "github-trending-2026-10-25-manual-100007"  # the topic's local clock
    assert json.loads(call["input"]) == sent_input


def test_a_second_manual_daily_run_in_the_same_second_is_refused_plainly(world):
    put_topics(ready(dispatch_mode="queue"))
    world["sfn"].start_execution.side_effect = ClientError(
        {"Error": {"Code": "ExecutionAlreadyExists"}}, "StartExecution"
    )

    status, body = trigger(world, AFTER_TURN, pipeline="daily_cycle")

    assert status == 409 and "github-trending-2026-10-25-manual-100000 has already started" in body["error"]


@pytest.mark.parametrize("pipeline", ["research_tick", "daily_cycle"])
def test_a_topic_on_its_schedules_is_still_run_by_a_direct_invoke(world, pipeline):
    put_topics(ready())

    status, body = trigger(world, AFTER_TURN, pipeline=pipeline)

    assert status == 202 and body == {"triggered": pipeline, "topic_id": "github-trending"}
    world["lambda"].invoke.assert_called_once()
    world["sqs"].send_message.assert_not_called()
    world["sfn"].start_execution.assert_not_called()


def test_a_network_prefetch_of_a_rail_topic_on_the_queue_is_still_a_direct_invoke(world):
    put_topics(ready("trains", adapter="rail_access", dispatch_mode="queue"))

    status, _ = trigger(world, AFTER_TURN, topic_id="trains", pipeline="network_prefetch")

    assert status == 202
    world["lambda"].invoke.assert_called_once()
    world["sqs"].send_message.assert_not_called()


def test_a_manual_research_run_with_no_work_queue_deployed_says_so(world, monkeypatch):
    put_topics(ready(dispatch_mode="queue"))
    monkeypatch.setenv("WORK_QUEUE_URL", "")

    status, body = trigger(world, AFTER_TURN, pipeline="research_tick")

    assert status == 500 and "no work queue" in body["error"]
    world["sqs"].send_message.assert_not_called()


# --- what `topics schedule` shows ------------------------------------------------------------------


def test_topics_schedule_shows_each_topic_s_dispatch_mode_and_checked_days(world):
    old = ready("older", stagger_slot=3)
    del old["shadow_checked_days"]
    put_topics(ready("alpha", dispatch_mode="queue"), ready("bravo", stagger_slot=2), old)

    result = admin_api_handler.handler(_event("GET /topics/schedule"), None)

    rows = json.loads(result["body"])["topics"]
    assert [(row["topic_id"], row["dispatch_mode"]) for row in rows] == [
        ("alpha", "queue"),
        ("bravo", "schedule"),
        ("older", "schedule"),
    ]
    assert [row["shadow_checked_days"] for row in rows] == [1, 1, None]
