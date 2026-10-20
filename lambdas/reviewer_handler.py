"""Professor Grizzlewick's Lambda: the reviewer bear (common/reviewer.py,
docs/enhancements/reviewer-bear.md).

Invoked two ways:

* by its static EventBridge Scheduler cron, every second day, with `{"job": "run"}`: plans a run
  and does its first tick;
* by the one-shot tick schedules a run creates, a few minutes apart, with
  `{"job": "tick", "run_id": ..., "tick": n}`: the next few tasks, then the next schedule, or his
  report when the queue is done.

He acts through the admin API's own functions (admin_api_handler.reviewer_*), so each decision
takes an operator's path. Deployed only when Terraform's `reviewer_enabled` is on (the
REVIEWER_ENABLED GitHub variable, on by default).

Never raises unhandled: like every other scheduled handler, a top-level try/except logs the real
exception and returns an error dict.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

import boto3

from common import reviewer
from common.lambda_timing import track_lambda_duration
from common.model_routing import resolve_model
from common.naming import environment_prefix

_scheduler_client = None


def _get_scheduler():
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = boto3.client("scheduler")
    return _scheduler_client


def tick_schedule_name(run_id: str, tick: int) -> str:
    """<prefix>-<env>-reviewer-tick-<run id>-<n>: inside the `<prefix>-<env>-*` names the role may
    create schedules under (aws_iam_role_policy.scheduler_manage)."""
    return f"{environment_prefix(os.environ['ENVIRONMENT_NAME'])}reviewer-tick-{run_id}-{tick}"


def schedule_tick(state: dict, own_arn: str) -> None:
    """The run's next tick: a one-shot schedule that deletes itself once it has fired."""
    at = datetime.fromisoformat(state["next_tick_at"]).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    _get_scheduler().create_schedule(
        Name=tick_schedule_name(state["run_id"], state["tick"]),
        GroupName="default",
        ScheduleExpression=f"at({at})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": own_arn,
            "RoleArn": os.environ["SCHEDULER_INVOKE_ROLE_ARN"],
            "Input": json.dumps({"job": "tick", "run_id": state["run_id"], "tick": state["tick"]}),
        },
    )


# --- his hands -----------------------------------------------------------------------------------


def _ask(prompt: str, model_id: str) -> str:
    from common.stats_tracking import tracked_claude

    return tracked_claude("reviewer", prompt, model_id, max_tokens=reviewer.ANSWER_MAX_TOKENS)


def _musing_model() -> str:
    return resolve_model(None)[0]


def build_hands() -> reviewer.Hands:
    import admin_api_handler as admin
    from common import musings
    from common.static_pages import read_article_body

    return reviewer.Hands(
        approve=admin.reviewer_approve,
        reject=admin.reviewer_reject,
        rewrite=admin.reviewer_rewrite,
        regenerate_musings=admin.reviewer_regenerate_musings,
        hold=admin.reviewer_hold,
        replace_body=admin.reviewer_replace_body,
        ask=_ask,
        model_for=lambda topic: resolve_model(topic)[0],
        read_body=read_article_body,
        rejection_musing=lambda **kw: musings.generate_and_store_reviewer_rejection(
            model_id=_musing_model(), **kw
        ),
        hold_musing=lambda **kw: musings.generate_and_store_reviewer_hold(model_id=_musing_model(), **kw),
    )


def _report(state: dict) -> None:
    """His report on the run, unless he did nothing worth saying: then he slept through it, and the
    feed is not filled with "nothing to do" every second day."""
    from common import musings

    facts = reviewer.report_facts(state.get("counts") or {})
    if not facts:
        return
    try:
        musings.generate_and_store_reviewer_report(
            facts=facts,
            woken=bool(state.get("woken")),
            model_id=_musing_model(),
        )
    except Exception as exc:  # noqa: BLE001 - the run is done; never fail it over its report
        print(f"reviewer_handler: could not post the report musing: {exc!r}")


def _advance(state: dict, own_arn: str | None, now: datetime, settings: dict, hands=None) -> dict:
    state = reviewer.tick(state, hands or build_hands(), now, settings)
    if state.get("queue"):
        try:
            schedule_tick(state, own_arn)
        except Exception as exc:  # noqa: BLE001 - the next run carries on from an overdue tick
            print(f"reviewer_handler: could not schedule tick {state.get('tick')}: {exc!r}")
    else:
        _report(state)
    return {
        "run_id": state.get("run_id"),
        "left": len(state.get("queue") or []),
        "counts": state.get("counts") or {},
    }


def handle(event: dict, own_arn: str | None, now: datetime | None = None, hands=None) -> dict:
    """`{"job": "run"}` (his schedule) plans a run, unless the `enabled` setting is off;
    `{"job": "tick", ...}` does a run's next tick, finishing a run already started even then.
    The settings are read fresh on every invocation, so a change needs no deploy."""
    now = now or datetime.now(UTC)
    settings = reviewer.get_settings()
    job = (event or {}).get("job", "run")
    if job == "run":
        if not settings["enabled"]:
            return {"status": "paused"}
        started = reviewer.start(now, settings)
        if started["status"] == "busy":
            return started
        return {"status": started["status"], **_advance(started, own_arn, now, settings, hands)}
    if job == "tick":
        state = reviewer.get_state()
        if state.get("run_id") != event.get("run_id") or state.get("tick") != event.get("tick"):
            return {"status": "stale", "run_id": event.get("run_id")}
        if not state.get("queue"):
            return {"status": "done", "run_id": state.get("run_id")}
        return {"status": "ticked", **_advance(state, own_arn, now, settings, hands)}
    return {"status": "ignored", "reason": f"unknown job {job!r}"}


@track_lambda_duration("reviewer")
def handler(event, context) -> dict:
    try:
        own_arn = getattr(context, "invoked_function_arn", None)
        return handle(event if isinstance(event, dict) else {}, own_arn)
    except Exception as exc:  # noqa: BLE001 - top-level Lambda guard, never raise unhandled
        print(f"reviewer_handler: unhandled exception: {exc!r}")
        return {"status": "error", "error": str(exc)}
