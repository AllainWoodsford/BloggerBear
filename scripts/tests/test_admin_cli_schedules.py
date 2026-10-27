"""The admin CLI's schedule commands (docs/project-plan.md §11, Staggered schedules): `topics
schedule`, `topics reschedule`, `topics pause`, `topics resume`, the `--stagger-slot` flag and
`pipeline-config set --stagger-minutes`; and the per-topic cutover's (docs/enhancements/
model-ranch-prompt-assets-evals.md, Q2): `topics update --dispatch-mode` with its confirmation,
`topics reschedule --dispatch-mode schedule` and each topic's mode in `topics schedule`."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from test_admin_cli import FakeResponse, _run, clean_env  # noqa: F401

API = "https://api.example.com"
REGION = "ap-southeast-2"

LISTING = {
    "stagger_minutes": 5,
    "topic_count": 2,
    "daily_delay_minutes": 10,
    "topics": [
        {
            "topic_id": "crypto",
            "stagger_slot": 1,
            "paused": False,
            "research_schedule": "cron(5 */4 * * ? *)",
            "research_schedule_text": "every 4 hours at :05 past",
            "daily_schedule": "cron(10 9 * * ? *)",
            "daily_schedule_text": "daily at 09:10",
            "daily_timezone": "Australia/Sydney",
            "research_interval_hours": 4,
            "last_research_at": "2026-10-15T04:05:10+00:00",
            "last_article_at": "2026-10-14T23:10:03+00:00",
        },
        {
            "topic_id": "wow",
            "stagger_slot": 2,
            "paused": True,
            "research_schedule": "cron(10 * * * ? *)",
            "research_schedule_text": "every hour at :10 past",
            "daily_schedule": "cron(15 9 * * ? *)",
            "daily_schedule_text": "daily at 09:15",
            "daily_timezone": "UTC",
        },
    ],
}


def test_topics_schedule_prints_each_topic_in_plain_words_and_as_written(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, LISTING)) as m:
        _run(["topics", "schedule"])

    m.assert_called_once_with("GET", API, "/topics/schedule", REGION, body=None)
    out = capsys.readouterr().out
    assert "Stagger: 5 minutes between topics (slot 1 at :05 past, slot 2 at :10, ...); 2 topics" in out
    assert "start 10 minutes after their hour (one slot past the last heartbeat)" in out
    assert "crypto  (slot 1, running)" in out and "wow  (slot 2, PAUSED)" in out
    assert "every 4 hours at :05 past" in out and "cron(5 */4 * * ? *)  UTC" in out
    assert "daily at 09:10  Australia/Sydney" in out and "cron(10 9 * * ? *)" in out
    assert "real research every 4 h" in out and "last researched 2026-10-15T04:05:10+00:00" in out
    assert "pipeline-config set --stagger-minutes N" in out and "topics reschedule" in out


def test_topics_schedule_shows_the_dispatchers_shadow_record_when_there_is_one(capsys):
    shadowed = {
        **LISTING["topics"][0],
        "shadow_last_research_dispatch_at": "2026-10-15T04:05:30+00:00",
        "shadow_last_daily_dispatch_at": "2026-10-14T23:10:30+00:00",
        "shadow_mismatches": 0,
        "shadow_unchecked": 1,
        "shadow_checked_days": 2,
    }
    answer = {**LISTING, "topics": [shadowed, LISTING["topics"][1]]}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)):
        _run(["topics", "schedule"])

    out = capsys.readouterr().out
    assert (
        "  shadow: 0 mismatches · 1 unchecked day · 2 checked days · last research turn "
        "2026-10-15T04:05:30+00:00 · last daily turn 2026-10-14T23:10:30+00:00"
    ) in out
    assert out.count("shadow:") == 1  # the topic with no shadow turn yet shows none


@pytest.mark.parametrize(
    ("recorded", "line"),
    [
        # The first shadow day: research turns are recorded hours before the first daily turn, and
        # a research mismatch already counts (_write_shadow leaves shadow_unchecked unset).
        (
            {"shadow_last_research_dispatch_at": "2026-10-15T04:05:30+00:00", "shadow_mismatches": 1},
            "  shadow: 1 mismatch · 0 unchecked days · 0 checked days · last research turn "
            "2026-10-15T04:05:30+00:00\n",
        ),
        # A daily turn that came before the first research turn: research every 4 hours, and shadow
        # mode switched on between two of them.
        (
            {
                "shadow_last_daily_dispatch_at": "2026-10-14T23:10:30+00:00",
                "shadow_mismatches": 0,
                "shadow_unchecked": 0,
                "shadow_checked_days": 1,
            },
            "  shadow: 0 mismatches · 0 unchecked days · 1 checked day · last daily turn "
            "2026-10-14T23:10:30+00:00\n",
        ),
    ],
    ids=["research_turn_only", "daily_turn_only"],
)
def test_topics_schedule_shows_the_shadow_record_of_a_topic_with_only_one_kind_of_shadow_turn_so_far(
    capsys, recorded, line
):
    answer = {**LISTING, "topics": [{**LISTING["topics"][0], **recorded}, LISTING["topics"][1]]}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)):
        _run(["topics", "schedule"])

    out = capsys.readouterr().out
    assert line in out  # the whole line: nothing about the other kind of turn follows it
    assert out.count("shadow:") == 1


def test_topics_schedule_can_print_the_raw_json(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, LISTING)):
        _run(["topics", "schedule", "--json"])

    assert '"stagger_minutes": 5' in capsys.readouterr().out


def test_topics_reschedule_posts_and_prints_the_same_table(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, LISTING)) as m:
        _run(["topics", "reschedule"])

    m.assert_called_once_with("POST", API, "/topics/reschedule", REGION, body={})
    assert "crypto  (slot 1, running)" in capsys.readouterr().out


def test_topics_reschedule_dry_run_says_so(capsys):
    answer = {**LISTING, "dry_run": True}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)) as m:
        _run(["topics", "reschedule", "--dry-run"])

    m.assert_called_once_with("POST", API, "/topics/reschedule", REGION, body={"dry_run": True})
    assert "Dry run: nothing was changed" in capsys.readouterr().out


def test_a_topic_that_could_not_be_rescheduled_shows_its_error(capsys):
    broken = {**LISTING["topics"][0], "error": "schedule expression 'whenever' must start with"}
    answer = {**LISTING, "topics": [broken]}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)):
        _run(["topics", "reschedule"])

    assert "ERROR: schedule expression 'whenever'" in capsys.readouterr().out


def test_staggering_off_is_said_plainly(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {**LISTING, "stagger_minutes": 0})):
        _run(["topics", "schedule"])

    assert "Stagger: off (0 minutes); 2 topics run at their cadences' own minutes." in capsys.readouterr().out


def test_a_failed_listing_exits_with_the_apis_answer(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(403, {"error": "forbidden"})):
        with pytest.raises(SystemExit) as exc_info:
            _run(["topics", "schedule"])

    assert exc_info.value.code != 0


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_topics_pause_and_resume_post_to_the_topic(action):
    answer = FakeResponse(200, {"paused": action == "pause"})
    with patch("admin_cli.signed_request", return_value=answer) as m:
        _run(["topics", action, "wow"])

    m.assert_called_once_with("POST", API, f"/topics/wow/{action}", REGION, body=None)


def test_a_slot_can_be_given_on_create_and_update():
    with patch("admin_cli.signed_request", return_value=FakeResponse(201, {})) as m:
        _run(["topics", "create", "--topic-id", "wow", "--name", "WoW", "--stagger-slot", "3"])
    assert m.call_args.kwargs["body"]["stagger_slot"] == 3

    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "wow", "--stagger-slot", "4"])
    m.assert_called_once_with("PUT", API, "/topics/wow", REGION, body={"stagger_slot": 4})

    with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
        _run(["topics", "update", "wow", "--stagger-slot", ""])
    m.assert_called_once_with("PUT", API, "/topics/wow", REGION, body={"stagger_slot": None})


def test_a_slot_that_is_not_a_number_is_refused_before_any_request(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit):
            _run(["topics", "update", "wow", "--stagger-slot", "first"])
    m.assert_not_called()
    assert "--stagger-slot must be a whole number" in capsys.readouterr().err


def test_the_stagger_can_be_set_turned_off_and_cleared(capsys):
    for text, sent in (("3", 3), ("0", 0), ("", None)):
        with patch("admin_cli.signed_request", return_value=FakeResponse(200, {})) as m:
            _run(["pipeline-config", "set", "--stagger-minutes", text])
        m.assert_called_once_with("PUT", API, "/pipeline-config", REGION, body={"stagger_minutes": sent})
    assert "run `topics reschedule` to apply it to every topic now" in capsys.readouterr().err


def test_a_stagger_that_is_not_a_number_is_refused_before_any_request(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit):
            _run(["pipeline-config", "set", "--stagger-minutes", "five"])
    m.assert_not_called()
    assert "--stagger-minutes must be a whole number of minutes" in capsys.readouterr().err


# --- the per-topic cutover (Q2) ----------------------------------------------------------------------


def test_topics_schedule_shows_where_each_topic_is_dispatched_from(capsys):
    on_queue = {**LISTING["topics"][0], "dispatch_mode": "queue"}
    on_schedules = {**LISTING["topics"][1], "dispatch_mode": "schedule"}
    answer = FakeResponse(200, {**LISTING, "topics": [on_queue, on_schedules]})
    with patch("admin_cli.signed_request", return_value=answer):
        _run(["topics", "schedule"])

    out = capsys.readouterr().out
    assert (
        "crypto  (slot 1, running)\n"
        "  dispatch: queue (the dispatcher runs it; it has no EventBridge schedules)"
    ) in out
    assert "wow  (slot 2, PAUSED)\n  dispatch: schedule (its two EventBridge schedules run it)" in out


def test_an_answer_from_before_dispatch_modes_reads_as_every_topic_on_its_schedules(capsys):
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, LISTING)):
        _run(["topics", "schedule"])

    assert capsys.readouterr().out.count("dispatch: schedule (its two EventBridge schedules run it)") == 2


@pytest.mark.parametrize(
    ("argv", "body", "said"),
    [
        (["--dispatch-mode", "schedule"], {"dispatch_mode": "schedule"}, "Moved back from the queue"),
        (
            ["--dispatch-mode", "schedule", "--dry-run"],
            {"dry_run": True, "dispatch_mode": "schedule"},
            "Would move back from the queue",
        ),
    ],
    ids=["undo", "dry_run"],
)
def test_topics_reschedule_can_move_every_topic_on_the_queue_back_to_its_schedules(capsys, argv, body, said):
    answer = {**LISTING, "dry_run": "--dry-run" in argv, "moved_to_schedule": ["crypto"]}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)) as m:
        _run(["topics", "reschedule", *argv])

    m.assert_called_once_with("POST", API, "/topics/reschedule", REGION, body=body)
    assert f"{said} to their schedules: crypto" in capsys.readouterr().out


def test_the_undo_with_no_topic_on_the_queue_says_so(capsys):
    answer = {**LISTING, "moved_to_schedule": []}
    with patch("admin_cli.signed_request", return_value=FakeResponse(200, answer)):
        _run(["topics", "reschedule", "--dispatch-mode", "schedule"])

    out = capsys.readouterr().out
    assert "Moved back from the queue to their schedules: none (no topic is on the queue)" in out


def test_topics_reschedule_moves_nothing_to_the_queue(capsys):
    with patch("admin_cli.signed_request") as m:
        with pytest.raises(SystemExit):
            _run(["topics", "reschedule", "--dispatch-mode", "queue"])
    m.assert_not_called()


CONFIRM = FakeResponse(
    409,
    {
        "error": "moving 'wow' to the queue deletes its two EventBridge schedules",
        "confirm_required": True,
        "schedules": ["prefix-dev-wow-research-tick", "prefix-dev-wow-daily-cycle"],
    },
)


def _update(argv, responses, answer=None):
    """`topics update` against canned answers; returns the bodies PUT, and the question asked."""
    sent, asked = [], []

    def fake_request(method, api_url, path, region, body=None):
        assert (method, path) == ("PUT", "/topics/wow")
        sent.append(body)
        return responses.pop(0)

    def fake_input(prompt):
        asked.append(prompt)
        if answer is EOFError:  # no terminal to answer from
            raise EOFError
        return answer

    with patch("admin_cli.signed_request", fake_request), patch("builtins.input", fake_input):
        _run(["topics", "update", "wow", *argv])
    return sent, asked


def test_moving_a_topic_to_the_queue_asks_first_and_resends_confirmed(capsys):
    moved = FakeResponse(200, {"dispatch_mode": "queue"})
    sent, asked = _update(["--dispatch-mode", "queue"], [CONFIRM, moved], "y")

    assert sent == [{"dispatch_mode": "queue"}, {"dispatch_mode": "queue", "confirm": True}]
    assert len(asked) == 1
    assert "prefix-dev-wow-research-tick, prefix-dev-wow-daily-cycle" in asked[0] and "[y/N]" in asked[0]
    assert '"dispatch_mode": "queue"' in capsys.readouterr().out


def test_moving_a_topic_to_the_queue_with_yes_does_not_ask():
    sent, asked = _update(["--dispatch-mode", "queue", "--yes"], [CONFIRM, FakeResponse(200, {})])

    assert asked == [] and sent[-1] == {"dispatch_mode": "queue", "confirm": True}


@pytest.mark.parametrize("answer", ["n", "", EOFError], ids=["no", "nothing", "no_terminal"])
def test_not_confirming_leaves_the_topic_on_its_schedules(capsys, answer):
    with pytest.raises(SystemExit) as exc_info:
        _update(["--dispatch-mode", "queue"], [CONFIRM], answer)  # one request: never resent

    assert exc_info.value.code == 1
    assert "Not moved: the topic stays on its schedules." in capsys.readouterr().err


def test_a_pre_flight_refusal_is_printed_with_its_reasons(capsys):
    refusal = FakeResponse(
        400, {"error": "topic 'wow' cannot move to the queue: it is paused", "reasons": ["it is paused"]}
    )
    with pytest.raises(SystemExit) as exc_info:
        _update(["--dispatch-mode", "queue"], [refusal], "y")

    assert exc_info.value.code == 1
    assert "cannot move to the queue: it is paused" in capsys.readouterr().err


def test_moving_a_topic_back_to_its_schedules_is_one_request():
    sent, asked = _update(["--dispatch-mode", "schedule"], [FakeResponse(200, {"dispatch_mode": "schedule"})])

    assert sent == [{"dispatch_mode": "schedule"}] and asked == []
