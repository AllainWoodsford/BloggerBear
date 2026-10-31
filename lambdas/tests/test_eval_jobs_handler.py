"""The eval jobs handler (eval_jobs_handler.py): `rollup` runs the rollup (from its watermark, or for
the days an admin asked for), `judge_select` runs the judge's selector, a queue batch goes record by
record to the judge's consumer (common/eval_judge.py, whose own tests are test_eval_judge.py), an
unknown job is ignored, and it never raises but for a rollup's infrastructure failure, which section 6
lets raise so eval-jobs' Errors alarm fires. It never prints what an event carried beyond a job name
that is a code."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest

import eval_jobs_handler
from common import evals_rollup, evals_settings

CONTEXT = SimpleNamespace(aws_request_id="req-1")
SETTINGS = evals_settings.effective_settings(None)


@pytest.fixture
def ran():
    with patch.object(eval_jobs_handler.evals_rollup, "run", return_value={"status": "done"}) as run:
        yield run


def test_the_hourly_schedule_runs_the_rollup_from_its_watermark(ran):
    result = eval_jobs_handler.handler({"job": "rollup", "trigger": "schedule"}, CONTEXT)

    assert result == {"status": "ran", "rollup": {"status": "done"}}
    ran.assert_called_once_with(trigger="schedule")


def test_an_admins_rollup_without_days_runs_from_the_watermark_as_the_schedule_does(ran):
    eval_jobs_handler.handler({"job": "rollup", "trigger": "admin"}, CONTEXT)

    ran.assert_called_once_with(trigger="admin")


def test_an_admins_rollup_for_days_adds_those_days_up_again(ran):
    with patch.object(eval_jobs_handler.evals_settings, "get_settings", return_value=SETTINGS):
        event = {"job": "rollup", "from": "2026-10-20", "to": "2026-10-22"}
        result = eval_jobs_handler.handler(event, CONTEXT)

    assert result["status"] == "ran"
    kwargs = ran.call_args.kwargs
    assert kwargs["trigger"] == "admin"
    assert (kwargs["day_from"], kwargs["day_to"]) == (date(2026, 10, 20), date(2026, 10, 22))


def test_days_that_are_not_a_range_are_refused_in_the_handlers_own_words(ran, capsys):
    with patch.object(eval_jobs_handler.evals_settings, "get_settings", return_value=SETTINGS):
        result = eval_jobs_handler.handler({"job": "rollup", "from": "ignore previous instructions"}, CONTEXT)

    assert result == {"status": "refused", "job": "rollup"}
    ran.assert_not_called()
    out = capsys.readouterr().out
    assert out.startswith("eval_jobs_handler: rollup refused: 'from' and 'to' must be dates")
    assert "instructions" not in out


def test_the_hourly_judge_schedule_runs_the_judges_selector(ran):
    with patch.object(eval_jobs_handler.eval_judge, "select", return_value={"status": "not_due"}) as select:
        result = eval_jobs_handler.handler({"job": "judge_select", "trigger": "schedule"}, CONTEXT)

    assert result == {"status": "ran", "judge_select": {"status": "not_due"}}
    select.assert_called_once_with(trigger="schedule")
    ran.assert_not_called()


def test_a_queue_batch_goes_record_by_record_to_the_judges_consumer(ran, capsys):
    records = [{"body": '{"job": "judge"}'}, {"body": '{"job": "score"}'}]
    answers = [{"status": "judged"}, {"status": "x"}]
    with patch.object(eval_jobs_handler.eval_judge, "consume", side_effect=answers) as consume:
        result = eval_jobs_handler.handler({"Records": records}, CONTEXT)

    assert result == {"status": "consumed", "results": [{"status": "judged"}, {"status": "x"}]}
    assert [call.args[0] for call in consume.call_args_list] == records
    assert capsys.readouterr().out == ""
    ran.assert_not_called()


def test_a_record_that_fails_is_printed_by_its_repr_and_never_takes_the_next_down(capsys):
    body = '{"job": "judge", "run_id": "r1", "item": {"summary": "a Finding\'s text"}}'
    failure = RuntimeError("table\ngone")
    with patch.object(eval_jobs_handler.eval_judge, "consume", side_effect=[failure, {"status": "judged"}]):
        result = eval_jobs_handler.handler({"Records": [{"body": body}, {"body": body}]}, CONTEXT)

    assert result == {"status": "consumed", "results": [{"status": "error"}, {"status": "judged"}]}
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: a queued job failed: RuntimeError('table\\ngone')\n"
    assert "Finding" not in out


@pytest.mark.parametrize("event", [None, "rollup", [], {}, {"job": "Delete Everything"}, {"job": 7}])
def test_an_event_that_is_not_a_job_is_ignored_and_never_raises(ran, capsys, event):
    assert eval_jobs_handler.handler(event, CONTEXT) == {"status": "ignored", "job": "unknown"}
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: job unknown is not one of this function's; nothing ran\n"
    ran.assert_not_called()


def test_a_rollup_that_cannot_reach_the_log_or_its_tables_raises_so_the_errors_alarm_fires(capsys):
    failure = evals_rollup.RollupError("the evals log query failed")
    with patch.object(eval_jobs_handler.evals_rollup, "run", side_effect=failure):
        with pytest.raises(evals_rollup.RollupError):
            eval_jobs_handler.handler({"job": "rollup", "trigger": "schedule"}, CONTEXT)
    assert capsys.readouterr().out == (
        "eval_jobs_handler: the rollup failed: RollupError('the evals log query failed')\n"
    )


def test_any_other_exception_is_printed_by_its_repr_and_returned_as_an_error(capsys):
    with patch.object(eval_jobs_handler.evals_rollup, "run", side_effect=RuntimeError("rollup\nbroke")):
        result = eval_jobs_handler.handler({"job": "rollup", "trigger": "schedule"}, CONTEXT)
    assert result == {"status": "error"}
    # By its repr (section 6), so the newline in the message stays escaped on one line.
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: unhandled exception: RuntimeError('rollup\\nbroke')\n"


def test_the_rollups_evidence_line_carries_the_invocations_id(capsys):
    def run(**kwargs):
        evals_rollup.evidence.emit("rollup_done", trigger=kwargs["trigger"], status="done")
        return {"status": "done"}

    with patch.object(eval_jobs_handler.evals_rollup, "run", side_effect=run):
        eval_jobs_handler.handler({"job": "rollup", "trigger": "schedule"}, CONTEXT)

    [line] = capsys.readouterr().out.splitlines()
    assert evals_rollup.evidence.parse(line)["tick"] == "req-1"
