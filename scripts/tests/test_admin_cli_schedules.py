"""The admin CLI's schedule commands (docs/project-plan.md §11, Staggered schedules): `topics
schedule`, `topics reschedule`, `topics pause`, `topics resume`, the `--stagger-slot` flag and
`pipeline-config set --stagger-minutes`."""

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
    assert "starts 10 minutes after its hour" in out
    assert "crypto  (slot 1, running)" in out and "wow  (slot 2, PAUSED)" in out
    assert "every 4 hours at :05 past" in out and "cron(5 */4 * * ? *)  UTC" in out
    assert "daily at 09:10  Australia/Sydney" in out and "cron(10 9 * * ? *)" in out
    assert "real research every 4 h" in out and "last researched 2026-10-15T04:05:10+00:00" in out
    assert "pipeline-config set --stagger-minutes N" in out and "topics reschedule" in out


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
