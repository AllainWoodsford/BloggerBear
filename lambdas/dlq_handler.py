"""Dead-letter-queue consumer Lambda.

SQS-triggered on `pipeline_dlq` (see aws_lambda_event_source_mapping.dlq in
infra/environments/*/main.tf). Every message on that queue is a
daily_cycle Step Functions execution that failed both of its retry
attempts (see aws_sfn_state_machine.daily_cycle's Retry/Catch config) --
before this handler existed, that queue had no consumer at all and a
failure was only visible via a CloudWatch alarm on queue depth (see
infra/modules/observability/main.tf's pipeline_dlq_messages alarm), which
tells you *that* something failed but not *what* or *why* without manually
reading the raw SQS message body in the console.

The Catch state's `"MessageBody.$": "$"` sends the full state at failure
time: the Task's input (`{"topic_id": "...", "execution": {"id": "..."}}`:
the schedule's input, with the execution's ARN that the NameTheRun state
adds so the Lambda can tell a retry of its own run) merged with the
caught error under `$.error` (a Step Functions error object, normally
shaped `{"Error": "...", "Cause": "..."}`). The execution's ARN is kept in
the record's raw message, which is how to find the execution in the Step
Functions console. This handler parses that best-
effort and writes one FailedExecutions record per message for admin
visibility (see scripts/admin_cli.py's `failed-executions list`) -- it does
not attempt any automatic replay; per the DLQ alarm's own comment, a DLQ
message means "needs a human to look at it".

It also consumes the work queue's dead-letter queue (`work-dlq.fifo`, infra/modules/work-queue):
a queue job (a body with `job`, docs/enhancements/model-ranch-prompt-assets-evals.md section 4.7)
that was received three times without finishing. The research tick never raises, so such a job
timed out or crashed the runtime; its body is the job itself, with no `error`. The log line names
the job (`research job for topic_id=... failed`) rather than calling every failure a daily cycle.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import UTC, datetime

from common.dynamo import put_failed_execution
from common.lambda_timing import track_lambda_duration

# A queue job's name as the dispatcher writes it (`research`, later `judge`, `score`): a code, so a
# body that says anything else cannot put its own text in the log line.
_JOB = re.compile(r"[a-z][a-z0-9_]{0,39}")


@track_lambda_duration("dlq_handler")
def handler(event: dict, context) -> dict:
    records = (event or {}).get("Records", [])
    processed = 0

    for record in records:
        raw_message = record.get("body", "")
        topic_id = None
        error = None
        job = None

        try:
            parsed = json.loads(raw_message)
        except (json.JSONDecodeError, TypeError):
            parsed = None

        if isinstance(parsed, dict):
            topic_id = parsed.get("topic_id")
            error = parsed.get("error")
            job = parsed.get("job")

        if job is not None:
            name = job if isinstance(job, str) and _JOB.fullmatch(job) else "unknown"
            print(f"dlq_handler: {name} job for topic_id={topic_id} failed")
        else:
            print(f"dlq_handler: daily_cycle failed for topic_id={topic_id}: {error}")

        put_failed_execution(
            failure_id=str(uuid.uuid4()),
            topic_id=topic_id,
            error=error,
            raw_message=raw_message,
            created_at=datetime.now(UTC).isoformat(),
        )
        processed += 1

    return {"status": "ok", "processed": processed}
