"""The eval jobs handler (eval_jobs_handler.py), a skeleton until the rollup and the judge land: it
logs which job it was asked for, does nothing, never raises, and never prints what an event
carried beyond a job name that is a code."""

from __future__ import annotations

import pytest

import eval_jobs_handler


@pytest.mark.parametrize("job", ["rollup", "judge_select"])
def test_each_scheduled_job_is_logged_and_skipped(capsys, job):
    result = eval_jobs_handler.handler({"job": job, "trigger": "schedule"}, None)

    assert result == {"status": "skipped", "job": job}
    assert capsys.readouterr().out == f"eval_jobs_handler: job {job} received; nothing runs it yet\n"


def test_a_queue_batch_is_counted_and_its_bodies_never_printed(capsys):
    body = '{"job": "judge", "run_id": "r1", "item": {"summary": "a Finding\'s text"}}'
    result = eval_jobs_handler.handler({"Records": [{"body": body}]}, None)

    assert result == {"status": "skipped", "records": 1}
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: 1 queued job(s) received; nothing runs them yet\n"


@pytest.mark.parametrize("event", [None, "rollup", [], {}, {"job": "Delete Everything"}, {"job": 7}])
def test_an_event_that_is_not_a_job_is_logged_as_unknown_and_never_raises(capsys, event):
    assert eval_jobs_handler.handler(event, None) == {"status": "skipped", "job": "unknown"}
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: job unknown received; nothing runs it yet\n"


def test_an_exception_inside_the_handler_is_printed_by_its_repr_and_returned_as_an_error(capsys, monkeypatch):
    def broken(kind, value):
        raise RuntimeError("evidence\nbroke")

    # monkeypatch puts the shared common.evidence back afterwards.
    monkeypatch.setattr(eval_jobs_handler.evidence, "check", broken)

    assert eval_jobs_handler.handler({"job": "rollup", "trigger": "schedule"}, None) == {"status": "error"}
    # By its repr (section 6), so the newline in the message stays escaped on one line.
    out = capsys.readouterr().out
    assert out == "eval_jobs_handler: unhandled exception: RuntimeError('evidence\\nbroke')\n"
