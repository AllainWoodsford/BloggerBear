"""Call traces: while the evals setting `trace_calls` is on, every model call that builds a lineage
call is stored whole, for a person to read back what a prompt version was given and what it wrote.

docs/enhancements/model-ranch-prompt-assets-evals.md, PR18c. One JSON object per call, at
`traces/<subject>/<stage>.json` in the content bucket: `{prompt_id, version, model_id,
rendered_prompt, reply, tokens}`. The subject is the one continuous evals score (section 4.3): an
article's id for the daily cycle's calls and a Re-Write's, `<topic_id>#<captured_at>` for the
research call that wrote a Finding. The stage is the lineage call's (`ideation`, `draft`, `title`,
`adversarial_review`, `revision`, `compliance_review`, `research`, `rewrite`); a Re-Write's reviews
are `rewrite_adversarial_review` and `rewrite_compliance_review`, so they never overwrite the reviews
of the text the article was first written with. A later Re-Write's traces replace an earlier one's.

**Who may read them.** Traces hold whole prompts and replies, so only the admin API reads them
(GET /evals/traces/{article_id}, `admin_cli evals trace`). The content bucket has no public path:
no CloudFront origin, no bucket policy, every public access blocked, and the public API reads only
an article row's own `body_s3_key` (test_terraform_wiring.py and test_call_traces.py hold all
four). The bucket's lifecycle drops `traces/` after 14 days in both environments.

**Outside text is stored as its sha256.** Every value the call site treats as untrusted (what it
sets down as data in a delimited block or defangs; in the daily cycle's ideation and draft, the
findings, which its reviews defang, and the gear's guidance, which is written from reader comments)
is replaced, wherever it appears in the prompt or the reply, by the hex sha256 of its UTF-8 bytes.
The trace then shows where each one went and which one it was, never its text. A research prompt
sets its source's data down as plain values and is stored whole. Comment screening
(common/comment_screening.py) builds no lineage call and is never traced.

**Never in the way.** A trace is written only inside `bind` (the daily cycle's run, a Re-Write,
the research tick's Finding), only while `trace_calls` is on (read once per bound run, from the
evals row: common/evals_settings.py, whose default the dev root turns on), and never raises: a
put has one second to connect, two to answer and no retry, and the first one that fails stops the
rest of the run's, with one line, so a slow S3 costs a run at most one bounded put.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass

import boto3
from botocore.config import Config

from common import evals_settings

PREFIX = "traces/"
# The setting that turns them on (common/evals_settings.py).
SETTING = "trace_calls"
# One second to connect, two to answer, and one attempt in all (`total_max_attempts` counts the
# first; botocore's legacy `max_attempts` would count retries only).
CLIENT_CONFIG = Config(connect_timeout=1, read_timeout=2, retries={"total_max_attempts": 1})
_STAGE = re.compile(r"[a-z][a-z0-9_]{0,39}")
# An article id, or a Finding's `<topic_id>#<captured_at>`: one key segment, so never a `/`.
_SUBJECT = re.compile(r"[A-Za-z0-9_.:#+-]{1,200}")
_client = None


@dataclass
class _Run:
    """What one bound run knows about its traces."""

    subject: str
    stage_prefix: str
    on: bool | None = None  # read on the run's first call
    stopped: bool = False  # a put failed: the run writes no more
    said: bool = False  # the log has named where its traces go


_run: contextvars.ContextVar[_Run | None] = contextvars.ContextVar("call_traces_run", default=None)


def _s3():
    global _client
    if _client is None:
        _client = boto3.client("s3", config=CLIENT_CONFIG)
    return _client


def finding_subject(topic_id: str, captured_at: str) -> str:
    """The subject of the research call that wrote a Finding (section 4.3)."""
    return f"{topic_id}#{captured_at}"


def key(subject: str, stage: str) -> str:
    return f"{PREFIX}{subject}/{stage}.json"


def valid_subject(subject) -> bool:
    return isinstance(subject, str) and _SUBJECT.fullmatch(subject) is not None


@contextlib.contextmanager
def bind(subject: str | None, *, stage_prefix: str = ""):
    """The subject every trace inside this block is stored under (None traces nothing).
    `stage_prefix` names a run that is not the article's first (a Re-Write: `rewrite`): its calls'
    stages are prefixed with it, all but the one already named so. Reset on exit, like
    prompts.bind_run, so a warm container never carries one run's subject into the next."""
    token = _run.set(_Run(subject, stage_prefix) if subject else None)
    try:
        yield
    finally:
        _run.reset(token)


def sha256_hex(value: str) -> str:
    """The hex sha256 of `value`'s UTF-8 bytes: what a trace stores in an untrusted value's place."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def redact(text: str, untrusted: Iterable[str]) -> str:
    """`text` with every occurrence of each untrusted value replaced by its sha256 hex. Longest
    first, so a value that holds another is replaced whole. The call sites pass the strings exactly
    as they were rendered (block tags and defanging included), and rendering is one pass that never
    changes a value (common/prompts.py), so each is found as it was sent."""
    values = sorted({str(value) for value in untrusted if value}, key=len, reverse=True)
    for value in values:
        text = text.replace(value, sha256_hex(value))
    return text


def named(values: dict, names: Iterable[str], text: str) -> list[str]:
    """The untrusted values a prompt builder rendered (`names` of its `values`, as it filled them in
    through `trace_values`). When any is missing, the prompt was built some other way, so the whole
    `text` is the untrusted value: nothing is stored unhashed that was not checked."""
    names = list(names)
    if any(name not in values for name in names):
        return [text]
    return [values[name] for name in names]


def _trace_calls_on() -> bool:
    """`trace_calls` in effect (the evals row's, else the deployment's default)."""
    return bool(evals_settings.get_settings()[SETTING])


def _on(run: _Run) -> bool:
    if run.on is None:
        try:
            run.on = _trace_calls_on()
        except Exception as exc:  # noqa: BLE001 - an unreadable setting traces nothing
            print(f"call_traces: could not read {SETTING}, tracing nothing for {run.subject}: {exc!r}")
            run.on = False
    return run.on


def _stage_name(run: _Run, stage) -> str | None:
    if not isinstance(stage, str):
        return None
    if run.stage_prefix and stage != run.stage_prefix:
        stage = f"{run.stage_prefix}_{stage}"
    return stage if _STAGE.fullmatch(stage) else None


def record(
    call: dict | None,
    prompt: str,
    reply: str,
    *,
    untrusted: Iterable[str] = (),
    stage: str | None = None,
) -> bool:
    """Store one call's trace under the bound subject, while `trace_calls` is on: `call` is the
    lineage call the site built (its model, tokens, prompt id and version), `prompt` the text sent
    and `reply` the text that came back. `stage` defaults to the call's own. True when it was
    stored. Never raises, and does nothing outside `bind` or after a put of the run failed."""
    run = _run.get()
    if run is None or run.stopped or not call:
        return False
    name = None
    try:
        name = _stage_name(run, stage or call.get("stage"))
        if not _on(run):
            return False
        if name is None or not valid_subject(run.subject):
            print(f"call_traces: no trace for stage {name!r} of subject {run.subject!r:.80}: not a key")
            return False
        untrusted = list(untrusted)
        trace = {
            "prompt_id": call.get("prompt_id"),
            "version": call.get("prompt_version"),
            "model_id": call.get("model_id"),
            "rendered_prompt": redact(prompt or "", untrusted),
            "reply": redact(reply or "", untrusted),
            "tokens": {"input": call.get("input_tokens"), "output": call.get("output_tokens")},
        }
        _s3().put_object(
            Bucket=os.environ["CONTENT_BUCKET"],
            Key=key(run.subject, name),
            Body=json.dumps(trace, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        if not run.said:
            # Once a run: how a run that stored no article (and so names no id) is found again.
            print(f"call_traces: tracing this run's model calls under {PREFIX}{run.subject}/")
            run.said = True
        return True
    except Exception as exc:  # noqa: BLE001 - a trace never fails the call it traces
        run.stopped = True
        print(f"call_traces: could not store the {name} trace of {run.subject}, tracing no more: {exc!r}")
        return False


# --- reading them back (the admin API) ----------------------------------------------------------------


def read(subject: str, client=None) -> list[dict]:
    """Every trace stored under `subject`, by stage: `{stage, key, stored_at, trace}`. Raises what
    S3 raises; the admin route answers it."""
    client = client or boto3.client("s3")
    bucket = os.environ["CONTENT_BUCKET"]
    prefix = f"{PREFIX}{subject}/"
    found = []
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if not name.endswith(".json") or "/" in name:
                continue
            body = client.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
            found.append(
                {
                    "stage": name[: -len(".json")],
                    "key": obj["Key"],
                    "stored_at": obj["LastModified"].isoformat(),
                    "trace": json.loads(body),
                }
            )
    return sorted(found, key=lambda entry: entry["stored_at"])


def read_one(subject: str, stage: str, client=None) -> dict | None:
    """One trace, `{stage, key, stored_at, trace}`, or None when there is none."""
    client = client or boto3.client("s3")
    try:
        response = client.get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=key(subject, stage))
    except client.exceptions.NoSuchKey:
        return None
    return {
        "stage": stage,
        "key": key(subject, stage),
        "stored_at": response["LastModified"].isoformat(),
        "trace": json.loads(response["Body"].read()),
    }
