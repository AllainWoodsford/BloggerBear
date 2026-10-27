"""The dispatcher Lambda: one tick of the queue platform a minute
(docs/enhancements/model-ranch-prompt-assets-evals.md section 5.1).

EventBridge Scheduler invokes it every minute (infra/modules/work-queue). It builds a Dispatcher
(common/dispatch.py) with the queue and Step Functions clients, the work queue's URL and the
daily-cycle state machine's ARN from its environment, runs one tick, and prints one
`dispatcher_tick` evidence line with what the tick did: how many topics it considered, how many
research jobs it sent, how many daily cycles it started, and how many cadences it could not read.
`outcome` is `ok`, or `error` when the tick raised; the work-queue module's heartbeat alarm counts
the `ok` lines, so a dispatcher that stopped running and one that fails every minute both alarm.

While every topic is on its two schedules (`dispatch_mode` unset or `schedule`, which every topic
is until the per-topic cutover), the tick is shadow mode only: it sends and starts nothing and
records on each row what it would have done (dispatch.py's docstring says how).

**It raises**, unlike the pipeline's other handlers: a tick that could not run (the Topics scan,
a queue send, a throttled StartExecution: DispatchError, or the last_tick write) is printed and
raised again, so the function's Errors alarm fires. A missed minute costs nothing: the next tick
looks back to the last one that finished (`last_tick`). One topic's own trouble never reaches
here; the tick prints it and goes on to the next topic.
"""

from __future__ import annotations

import os
import time

import boto3

from common import evidence
from common.dispatch import Dispatcher
from common.lambda_timing import track_lambda_duration

_sqs_client = None
_sfn_client = None


def _clients():
    """The queue and Step Functions clients, made once per container."""
    global _sqs_client, _sfn_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    if _sfn_client is None:
        _sfn_client = boto3.client("stepfunctions")
    return _sqs_client, _sfn_client


@track_lambda_duration("dispatcher")
def handler(event, context) -> dict:
    tick = getattr(context, "aws_request_id", None)
    with evidence.bind(tick=tick, run=evidence.new_run()):
        started = time.monotonic()
        counts: dict = {}
        outcome, error_type = "error", None
        try:
            sqs, sfn = _clients()
            dispatcher = Dispatcher(
                sqs=sqs,
                sfn=sfn,
                queue_url=os.environ["WORK_QUEUE_URL"],
                state_machine_arn=os.environ["STATE_MACHINE_ARN"],
            )
            counts = dispatcher.tick()
            outcome = "ok"
            return {"status": "ok", **counts}
        except Exception as exc:
            error_type = type(exc).__name__
            print(f"dispatcher_handler: the tick failed: {exc!r}")
            raise
        finally:
            evidence.emit(
                "dispatcher_tick",
                **counts,
                outcome=outcome,
                ms=int((time.monotonic() - started) * 1000),
                error_type=error_type,
            )
