"""Observability enhancement, PR 2: roughly how long each pipeline Lambda actually ran, tallied
onto this week's Stats row.

AWS's own "Billed Duration" (the number in a Lambda invocation's CloudWatch `REPORT` line) is
computed by the platform *after* a function returns -- a running function cannot read or log that
exact figure about itself. Two ways to get it anyway: pull the `REPORT` lines (or the `AWS/Lambda`
`Duration` metric) back out of CloudWatch after the fact, or have the function time its own
wall-clock execution and use that as a close stand-in. This project takes the second path
(the owner's call, favouring a self-contained build over a second AWS integration): `perf_counter()`
around a handler's own body is within a few milliseconds of the real Billed Duration on a warm
invocation (both round to whole milliseconds), and adds no measurable latency of its own -- reading
the clock twice costs nanoseconds against calls that spend seconds on Bedrock and DynamoDB. What it
does **not** capture is a cold start's separate Init Duration (module import, client construction,
before `handler` is ever entered) -- this is a lower bound on a cold invocation, not the whole bill.

Applied only to the scheduled pipeline handlers (research_tick, daily_cycle, weekly_reflection,
musing_feedback, trending_digest, dlq_handler, stats_rollover) -- deliberately **not**
admin_api_handler or public_api_handler, which serve live requests on every hit: adding a
synchronous DynamoDB write to those would add real latency and cost to traffic that has none today,
for a number ("how long did this one API request take") nobody asked to see on the Stats page.

Recording never fails the invocation it is timing: a DynamoDB failure here is logged and swallowed,
the handler's own return value (or exception) is exactly what it would have been without this
decorator, same as every other cross-cutting concern in this codebase.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable

from common.stats_tracking import record_lambda_duration


def track_lambda_duration(function_name: str) -> Callable:
    """Decorate a Lambda `handler(event, context)` with self-timed wall-clock duration, tallied
    onto this week's Stats row under `function_name`. Times the whole call, success or failure --
    an exception still propagates exactly as it would without this decorator, after being timed."""

    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(event, context):
            start = time.perf_counter()
            try:
                return fn(event, context)
            finally:
                elapsed_ms = int((time.perf_counter() - start) * 1000)
                try:
                    record_lambda_duration(function_name, elapsed_ms)
                except Exception as exc:  # noqa: BLE001 - never let timing break the real invocation
                    print(f"lambda_timing: could not record {function_name}'s duration: {exc!r}")

        return wrapper

    return decorator
