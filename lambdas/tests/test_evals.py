"""Continuous evals' events (common/evals.py; docs/enhancements/model-ranch-prompt-assets-evals.md
section 4.3).

What is held: the scoring table is section 4.3's; each reason's event id has its own shape; an
event is refused whole for an unknown reason, a delta the reason may not carry, a performance that
does not match the delta's sign, or any field that is not its kind (matched whole, so not with a
trailing newline either), so free text has nowhere to go; a prompt-scoped reason needs its prompt;
`parse` reads back exactly what `emit` writes and leaves out what the schema does not name; `emit`
writes one line to this container's stream, writes nothing and builds no client with the switch
off, and never raises whatever Logs does; `author_of` names the models that wrote an item and not
the ones that judged it; and `wilson` is the scripts' interval.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from moto import mock_aws

from common import evals

ROOT = Path(__file__).resolve().parents[2]
REGION = "ap-southeast-2"
GROUP = "acme-dev-evals"
FUNCTION = "acme-dev-daily-cycle"
# Not a real model's name: the schema only cares about the shape.
MODEL = "au.example.test-model-v1:0"
WHEN = "2026-10-25T03:04:05Z"


def _completion(**overrides) -> dict:
    """A valid Article Completion's keywords, as `event` and `emit` take them."""
    fields = {
        "event_id": "completion:a1b2:daily_cycle/draft",
        "source": "daily_cycle",
        "model_id": MODEL,
        "prompt_id": "daily_cycle/draft",
        "prompt_version": "v2",
        "subject": "a1b2",
        "topic_id": "github-trending",
        "timestamp": WHEN,
    }
    fields.update(overrides)
    return fields


def _line(**overrides) -> dict:
    """A valid event as it is read back from the log."""
    record, why = evals.event("Article Completion", **_completion())
    assert why is None
    record.update(overrides)
    return record


# --- the table ------------------------------------------------------------------------------------


def test_the_scoring_table_is_section_4_3s_in_its_order():
    assert evals.REASONS == (
        "Article Completion",
        "Article Rewrite",
        "User Feedback",
        "Admin Review",
        "Research Review",
        "Article Review",
        "Human Review",
        "Offline Review",
        "Admin Forgive",
        "Admin Punish",
        "Prompt Fallback",
    )
    deltas = {reason: scoring.deltas for reason, scoring in evals.SCORING.items()}
    assert deltas["Article Completion"] == {2} and deltas["Article Rewrite"] == {-2}
    assert deltas["User Feedback"] == {1, -1, 3, -3} and deltas["Prompt Fallback"] == {0}
    for reason in ("Admin Review", "Research Review", "Article Review", "Human Review", "Offline Review"):
        assert deltas[reason] == {2, -2}, reason
    assert deltas["Admin Forgive"] is None and evals.SCORING["Admin Forgive"].sign == 1
    assert deltas["Admin Punish"] is None and evals.SCORING["Admin Punish"].sign == -1
    gates = {reason: scoring.gates for reason, scoring in evals.SCORING.items()}
    assert gates["User Feedback"] == "no"  # reader votes score but never gate
    judged = ("Research Review", "Article Review", "Offline Review")
    assert {gates[reason] for reason in judged} == {"while_calibrated"}
    assert gates["Admin Forgive"] == gates["Admin Punish"] == "total_only"
    assert gates["Prompt Fallback"] == "counted"
    assert {gate for gate in gates.values()} <= set(evals.GATES)
    # Section 4.3's "applies to" column. Nothing reads it yet, so only this holds it.
    assert {reason: scoring.applies_to for reason, scoring in evals.SCORING.items()} == {
        "Article Completion": "model_and_prompts",
        "Article Rewrite": "model",
        "User Feedback": "model",
        "Admin Review": "model_and_prompts",
        "Research Review": "model_and_prompts",
        "Article Review": "model_and_prompts",
        "Human Review": "model_and_prompts",
        "Offline Review": "combination",
        "Admin Forgive": "target",
        "Admin Punish": "target",
        "Prompt Fallback": "prompt_version",
    }
    # The reasons whose event ids name a prompt (section 4.3's list) are the ones that need one.
    prompted = {reason for reason, scoring in evals.SCORING.items() if "prompt" in scoring.needs}
    assert prompted == {"Article Completion", "Admin Review", "Offline Review", "Prompt Fallback"}


def test_the_spec_example_is_written_byte_for_byte():
    """Section 4.3's example line (with a placeholder model id): compact JSON, fields in order."""
    record, why = evals.event("Article Completion", **_completion())
    assert why is None
    assert evals.line(record) == (
        '{"schema":"evals/1","event_id":"completion:a1b2:daily_cycle/draft",'
        '"timestamp":"2026-10-25T03:04:05Z","model_id":"au.example.test-model-v1:0",'
        '"prompt_id":"daily_cycle/draft","prompt_version":"v2","performance":"good","rating_delta":2,'
        '"reason":"Article Completion","subject":"a1b2","topic_id":"github-trending",'
        '"source":"daily_cycle"}'
    )


# --- event ids --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "parts", "expected"),
    [
        (
            "Article Completion",
            {"article_id": "a1b2", "prompt_id": "daily_cycle/draft"},
            "completion:a1b2:daily_cycle/draft",
        ),
        ("Article Rewrite", {"rewrite_id": "rw-7f3a"}, "rewrite:rw-7f3a"),
        ("User Feedback", {"feedback_id": "fb-91c2"}, "vote:fb-91c2"),
        (
            "Admin Review",
            {"queue_id": "q-5d1e", "decision": "approve", "prompt_id": "daily_cycle/title"},
            "admin:q-5d1e:approve:daily_cycle/title",
        ),
        (
            "Research Review",
            {"subject": "github-trending#2026-10-25T03:04:05+00:00", "run_id": "run-1"},
            "judge:github-trending#2026-10-25T03:04:05+00:00:run-1",
        ),
        ("Article Review", {"subject": "a1b2", "run_id": "run-1"}, "judge:a1b2:run-1"),
        ("Human Review", {"subject": "a1b2", "ts": WHEN}, f"human:a1b2:{WHEN}"),
        (
            "Offline Review",
            {"run_id": "run-9", "item": "3", "combo": f"{MODEL}@daily_cycle/draft=v3"},
            f"offline:run-9:3:{MODEL}@daily_cycle/draft=v3",
        ),
        ("Admin Forgive", {"ts": WHEN, "target": f"model#{MODEL}"}, f"adjust:{WHEN}:model#{MODEL}"),
        (
            "Admin Punish",
            {"ts": WHEN, "target": "prompt#daily_cycle/draft#v2"},
            f"adjust:{WHEN}:prompt#daily_cycle/draft#v2",
        ),
        (
            "Prompt Fallback",
            {"request_id": "req-0c4b", "prompt_id": "research_tick/summary"},
            "fallback:req-0c4b:research_tick/summary",
        ),
    ],
)
def test_each_reason_builds_its_own_event_id(reason, parts, expected):
    assert evals.event_id(reason, **parts) == expected
    # And an event carrying it is accepted for that reason and refused for one of another prefix.
    assert evals._event_id_value(expected) == expected
    other, delta = ("Article Rewrite", -2) if not expected.startswith("rewrite:") else ("User Feedback", 1)
    record, why = evals.event(other, **_completion(event_id=expected), rating_delta=delta)
    assert record is None and why == "event_id_reason"


def test_an_event_id_with_a_part_missing_extra_or_in_words_is_not_built():
    assert evals.event_id("Article Completion", article_id="a1b2") is None
    assert evals.event_id("Article Rewrite", rewrite_id="rw-1", prompt_id="daily_cycle/draft") is None
    assert evals.event_id("User Feedback", feedback_id="a reader's comment") is None
    assert evals.event_id("User Feedback", feedback_id="") is None
    assert evals.event_id("Article Liked", article_id="a1b2") is None
    record, why = evals.event("Article Completion", **_completion(event_id=None))
    assert record is None and why == "missing_event_id"


def test_an_event_id_over_512_characters_is_refused_by_the_writer_and_the_reader():
    at_cap = "completion:" + "a" * (evals.MAX_EVENT_ID_CHARS - len("completion:"))
    record, why = evals.event("Article Completion", **_completion(event_id=at_cap))
    assert why is None and evals.parse(record) == record
    over = at_cap + "a"
    assert evals.event("Article Completion", **_completion(event_id=over)) == (None, "bad_event_id")
    assert evals.parse(_line(event_id=over)) is None


def test_an_event_id_whose_parts_join_past_512_characters_is_not_built():
    # offline: + 200 + : + 200 + : + 102 is exactly 512 characters. Not User Feedback: its one part
    # is held to 200 characters, so its id can never reach the cap.
    assert len(evals.event_id("Offline Review", run_id="r" * 200, item="i" * 200, combo="c" * 102)) == 512
    assert evals.event_id("Offline Review", run_id="r" * 200, item="i" * 200, combo="c" * 103) is None
    assert evals.event_id("Admin Review", queue_id="q" * 200, decision="d" * 200, prompt_id="p" * 200) is None


# --- the schema -----------------------------------------------------------------------------------


def test_an_unknown_reason_is_refused_by_the_writer_and_the_reader():
    record, why = evals.event("Article Liked", **_completion())
    assert record is None and why == "unknown_reason"
    assert evals.parse(_line(reason="Article Liked")) is None
    assert evals.parse(_line(reason="article completion")) is None


def test_a_performance_that_does_not_match_the_deltas_sign_is_refused():
    assert evals.parse(_line()) is not None
    assert evals.parse(_line(performance="bad")) is None
    assert evals.parse(_line(performance="neutral")) is None
    # The writer cannot get it wrong: it derives performance from the delta.
    model_only = _completion(event_id="rewrite:rw-1", prompt_id=None, prompt_version=None)
    rewrite, _ = evals.event("Article Rewrite", **model_only)
    assert rewrite["performance"] == "bad" and rewrite["rating_delta"] == -2
    fallback, _ = evals.event(
        "Prompt Fallback", event_id="fallback:req-1:daily_cycle/draft", source="daily_cycle",
        prompt_id="daily_cycle/draft", prompt_version="vanilla:v1",
    )  # fmt: skip
    assert fallback["performance"] == "neutral" and fallback["rating_delta"] == 0


@pytest.mark.parametrize(
    ("reason", "delta"),
    [
        ("Article Completion", -2),
        ("Article Completion", 3),
        ("User Feedback", 2),
        ("User Feedback", 0),
        ("Admin Review", 1),
        ("Admin Forgive", -5),
        ("Admin Forgive", 0),
        ("Admin Punish", 5),
        ("Prompt Fallback", 1),
    ],
)
def test_a_delta_the_reason_may_not_carry_is_refused(reason, delta):
    record = _line(reason=reason, rating_delta=delta, performance=evals.performance_of(delta))
    assert evals.parse(record) is None
    assert evals._validated(record) == (None, "delta_not_allowed")


def test_a_delta_that_is_not_a_whole_number_is_refused():
    for delta in (2.0, "2", True, None):
        assert evals.parse(_line(rating_delta=delta)) is None, delta
        assert evals._validated(_line(rating_delta=delta)) == (None, "bad_rating_delta"), delta
    # True == 1, which a vote may carry: only the bool check refuses it.
    vote = _line(reason="User Feedback", event_id="vote:fb-1", rating_delta=True)
    assert evals.parse(vote) is None
    assert evals._validated(vote) == (None, "bad_rating_delta")
    assert evals.parse({**vote, "rating_delta": 1}) is not None
    record, why = evals.event("User Feedback", **_completion(event_id="vote:fb-1"), rating_delta=True)
    assert record is None and why == "bad_rating_delta"
    record, why = evals.event("User Feedback", **_completion(event_id="vote:fb-1"))
    assert record is None and why == "missing_rating_delta"  # +-1 or +-3: the caller says which


def test_free_text_is_dropped_by_the_reader_and_refused_by_the_writer():
    """A field the schema does not name never comes back; a named field holding words refuses the
    whole event, so a Completion never turns model-only by losing its prompt."""
    parsed = evals.parse(json.dumps({**_line(), "comment": "Loved it, but the second chart is wrong"}))
    assert parsed == _line() and "comment" not in parsed
    for field, words in (
        ("subject", "the article about trains"),
        ("event_id", "completion:a1b2:the draft prompt"),
        ("model_id", "the cheap one"),
        ("prompt_id", "Draft the article"),
        ("prompt_version", "the new one"),
        ("topic_id", "github trending"),
        ("source", "Daily Cycle"),
        # A code's shape but not a role (section 3, deviation 10): only the role codes are taken.
        ("by", "operator"),
        ("timestamp", "yesterday"),
    ):
        record, why = evals.event("Article Completion", **_completion(**{field: words}))
        assert record is None and why == f"bad_{field}", field
        assert evals.parse(_line(**{field: words})) is None, field
    for role in evals.BY:
        record, why = evals.event("Article Completion", **_completion(by=role))
        assert why is None and record["by"] == role, role


def test_a_trailing_newline_is_refused_by_the_writer_and_the_reader():
    """With `re.match` and `$`, one trailing newline passes, and the rollup groups and deduplicates
    by these values: `v2\\n` would be a prompt version of its own, and the same event id with and
    without one would count twice."""
    for field, value in (
        ("event_id", "completion:a1b2:daily_cycle/draft\n"),
        ("prompt_id", "daily_cycle/draft\n"),
        ("prompt_version", "v2\n"),
        ("model_id", f"{MODEL}\n"),
        ("subject", "a1b2\n"),
        ("subject", "github-trending\n#2026-10-25T03:04:05+00:00"),
        ("subject", "github-trending#2026-10-25T03:04:05+00:00\n"),
        ("topic_id", "github-trending\n"),
        ("source", "daily_cycle\n"),
    ):
        record, why = evals.event("Article Completion", **_completion(**{field: value}))
        assert record is None and why == f"bad_{field}", (field, value)
        assert evals.parse(_line(**{field: value})) is None, (field, value)
        assert evals.parse(json.dumps(_line(**{field: value}))) is None, (field, value)
    assert evals.event_id("Article Completion", article_id="a1b2", prompt_id="daily_cycle/draft\n") is None
    assert evals.event_id("Article Completion", article_id="a1b2\n", prompt_id="daily_cycle/draft") is None


def test_a_finding_subject_is_its_topic_and_capture_time():
    record, why = evals.event(
        "Research Review", **_completion(
            event_id="judge:github-trending#2026-10-25T03:04:05+00:00:run-1",
            subject="github-trending#2026-10-25T03:04:05+00:00", prompt_id=None, prompt_version=None,
        ), rating_delta=2, by="judge",
    )  # fmt: skip
    assert why is None and record["subject"] == "github-trending#2026-10-25T03:04:05+00:00"
    assert evals.parse(_line(subject="github-trending#not-a-time")) is None
    assert evals.parse(_line(subject="#2026-10-25T03:04:05+00:00")) is None


def test_a_prompt_id_comes_with_its_version_and_prompt_reasons_need_one():
    assert evals.event("Article Completion", **_completion(prompt_version=None)) == (None, "prompt_half")
    assert evals.event("Article Completion", **_completion(prompt_id=None)) == (None, "prompt_half")
    both_gone = {"prompt_id": None, "prompt_version": None}
    assert evals.event("Article Completion", **_completion(**both_gone)) == (None, "missing_prompt")
    # A model-only reason may still carry the prompt (section 3, deviation 15), or not.
    for fields in (_completion(event_id="rewrite:rw-1"), _completion(event_id="rewrite:rw-1", **both_gone)):
        record, why = evals.event("Article Rewrite", **fields)
        assert why is None and record["reason"] == "Article Rewrite"


@pytest.mark.parametrize(
    ("reason", "fields"),
    [
        ("Admin Review", _completion(event_id="admin:q-5d1e:approve:daily_cycle/draft", rating_delta=2)),
        (
            "Offline Review",
            _completion(event_id=f"offline:run-9:3:{MODEL}@daily_cycle/draft=v3", rating_delta=-2),
        ),
        (
            "Prompt Fallback",
            {
                "event_id": "fallback:req-1:daily_cycle/draft",
                "source": "daily_cycle",
                "prompt_id": "daily_cycle/draft",
                "prompt_version": "vanilla:v1",
            },
        ),
    ],
)
def test_a_prompt_scoped_reason_without_its_prompt_is_refused_by_the_writer_and_the_reader(reason, fields):
    """Section 4.3: these reasons' events are one per (subject, prompt_id), or a prompt version's."""
    record, why = evals.event(reason, **fields)
    assert why is None
    unprompted = {**fields, "prompt_id": None, "prompt_version": None}
    assert evals.event(reason, **unprompted) == (None, "missing_prompt")
    assert evals.parse({k: v for k, v in record.items() if k not in ("prompt_id", "prompt_version")}) is None


def test_every_reason_but_an_adjustment_or_a_fallback_needs_its_model_subject_and_topic():
    for missing in ("model_id", "subject", "topic_id"):
        record, why = evals.event("Article Completion", **_completion(**{missing: None}))
        assert record is None and why == f"missing_{missing}"
    record, why = evals.event(
        "Prompt Fallback", event_id="fallback:req-1:daily_cycle/draft", source="daily_cycle",
        prompt_id="daily_cycle/draft", prompt_version="vanilla:v1",
    )  # fmt: skip
    assert why is None and set(record) == {
        "schema", "event_id", "timestamp", "prompt_id", "prompt_version", "performance", "rating_delta",
        "reason", "source",
    }  # fmt: skip


def test_an_adjustment_names_exactly_one_target():
    common = {"event_id": f"adjust:{WHEN}:model#{MODEL}", "source": "admin_api", "rating_delta": 40}
    record, why = evals.event("Admin Forgive", **common, model_id=MODEL, by="human")
    assert why is None and record["rating_delta"] == 40 and record["performance"] == "good"
    prompt = {"prompt_id": "daily_cycle/draft", "prompt_version": "v2"}
    record, why = evals.event("Admin Punish", **{**common, "rating_delta": -150}, **prompt)
    assert why is None and record["performance"] == "bad"
    assert evals.event("Admin Forgive", **common) == (None, "bad_target")
    both = {"model_id": MODEL, "prompt_id": "daily_cycle/draft", "prompt_version": "v2"}
    assert evals.event("Admin Forgive", **common, **both) == (None, "bad_target")


def test_the_reader_refuses_what_is_not_an_event_of_this_schema():
    for message in ("", "not json", "[1, 2]", "null", json.dumps({**_line(), "schema": "evals/2"}), 7, None):
        assert evals.parse(message) is None, message
    assert evals.parse(evals.line(_line())) == _line()
    assert evals.parse({k: v for k, v in _line().items() if k != "source"}) is None


# --- writing ----------------------------------------------------------------------------------------


@pytest.fixture
def switched_on(monkeypatch):
    monkeypatch.setenv("EVALS_ENABLED", "true")
    monkeypatch.setenv("EVALS_LOG_GROUP", GROUP)
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", FUNCTION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setattr(evals, "_client", None)
    monkeypatch.setattr(evals, "_stream", None)


class FakeLogs:
    """A Logs client that records its calls, and raises what it is told to."""

    def __init__(self, create_error=None, put_error=None):
        self.calls = []
        self.create_error, self.put_error = create_error, put_error

    def create_log_stream(self, **kwargs):
        self.calls.append(("create", kwargs))
        if self.create_error:
            raise self.create_error

    def put_log_events(self, **kwargs):
        self.calls.append(("put", kwargs))
        if self.put_error:
            raise self.put_error


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": "refused"}}, "PutLogEvents")


def test_emit_writes_one_line_to_this_containers_stream(switched_on):
    with mock_aws():
        logs = boto3.client("logs", region_name=REGION)
        logs.create_log_group(logGroupName=GROUP)

        assert evals.emit("Article Completion", **_completion()) is True

        streams = logs.describe_log_streams(logGroupName=GROUP)["logStreams"]
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert [s["logStreamName"] for s in streams] == [f"{FUNCTION}/{today}/{evals.CONTAINER_ID}"]
        events = logs.get_log_events(logGroupName=GROUP, logStreamName=streams[0]["logStreamName"])["events"]
        assert [evals.parse(e["message"]) for e in events] == [_line()]


def test_the_stream_is_made_once_a_day_and_again_on_a_new_day(switched_on, monkeypatch):
    fake = FakeLogs()
    monkeypatch.setattr(evals, "_client", fake)

    assert evals.emit("Article Completion", **_completion()) is True
    title = _completion(prompt_id="daily_cycle/title", event_id="completion:a1b2:daily_cycle/title")
    assert evals.emit("Article Completion", **title) is True
    assert [kind for kind, _ in fake.calls] == ["create", "put", "put"]
    stream = fake.calls[0][1]["logStreamName"]
    assert fake.calls[0][1] == {"logGroupName": GROUP, "logStreamName": stream}
    assert all(call["logStreamName"] == stream and call["logGroupName"] == GROUP for _, call in fake.calls)
    assert len(fake.calls[1][1]["logEvents"]) == 1

    monkeypatch.setattr(evals, "_stream", f"{FUNCTION}/2026-10-24/{evals.CONTAINER_ID}")  # yesterday's
    evals.emit("Article Completion", **_completion())
    assert [kind for kind, _ in fake.calls[3:]] == ["create", "put"]
    assert evals.stream_name(datetime(2026, 10, 25, 23, 59, tzinfo=UTC)) != evals.stream_name(
        datetime(2026, 10, 26, 0, 0, tzinfo=UTC)
    )
    named = evals.stream_name(datetime(2026, 10, 25, tzinfo=UTC))
    assert named == f"{FUNCTION}/2026-10-25/{evals.CONTAINER_ID}"


def test_a_stream_another_container_or_invocation_already_made_is_written_to(switched_on, monkeypatch):
    fake = FakeLogs(create_error=_client_error("ResourceAlreadyExistsException"))
    monkeypatch.setattr(evals, "_client", fake)

    assert evals.emit("Article Completion", **_completion()) is True
    assert [kind for kind, _ in fake.calls] == ["create", "put"]


@pytest.mark.parametrize(
    "error",
    [
        _client_error("ThrottlingException"),
        _client_error("ResourceNotFoundException"),
        _client_error("AccessDeniedException"),
        EndpointConnectionError(endpoint_url="https://logs.ap-southeast-2.amazonaws.com"),
        RuntimeError("anything at all"),
    ],
)
def test_emit_swallows_whatever_logs_raises_and_never_raises(switched_on, monkeypatch, capsys, error):
    fake = FakeLogs(put_error=error)
    monkeypatch.setattr(evals, "_client", fake)

    assert evals.emit("Article Completion", **_completion()) is False
    assert "evals: could not write an event: " in capsys.readouterr().out
    # The stream is made again next time, in case it is what went.
    assert evals._stream is None
    fake.put_error = None
    assert evals.emit("Article Completion", **_completion()) is True
    assert [kind for kind, _ in fake.calls] == ["create", "put", "create", "put"]


def test_a_failure_to_make_the_stream_is_swallowed_too(switched_on, monkeypatch):
    fake = FakeLogs(create_error=_client_error("AccessDeniedException"))
    monkeypatch.setattr(evals, "_client", fake)

    assert evals.emit("Article Completion", **_completion()) is False
    assert [kind for kind, _ in fake.calls] == ["create"]


def test_a_refused_event_is_never_written_and_says_why_as_a_code(switched_on, monkeypatch, capsys):
    fake = FakeLogs()
    monkeypatch.setattr(evals, "_client", fake)

    assert evals.emit("Article Completion", **_completion(subject="the article about trains")) is False
    assert evals.emit("Article Completion", **_completion(), comment="free text") is False  # not a field
    out = capsys.readouterr().out
    assert "evals: event refused (bad_subject)" in out and "trains" not in out
    assert "free text" not in out
    assert fake.calls == []


def test_with_the_switch_off_or_no_group_nothing_is_written_and_no_client_is_built(
    switched_on, monkeypatch, capsys
):
    # Recorded, not raised: emit swallows every exception, so a raising stand-in would prove nothing.
    built, fake = [], FakeLogs()

    def recording_client(*args, **kwargs):
        built.append(args)
        return fake

    monkeypatch.setattr(evals.boto3, "client", recording_client)
    for name, value in (("EVALS_ENABLED", "false"), ("EVALS_ENABLED", ""), ("EVALS_ENABLED", "True")):
        monkeypatch.setenv(name, value)
        assert evals.emit("Article Completion", **_completion()) is False
        assert built == [], value
    monkeypatch.setenv("EVALS_ENABLED", "true")
    monkeypatch.setenv("EVALS_LOG_GROUP", "")
    assert evals.emit("Article Completion", **_completion()) is False
    assert built == [] and fake.calls == [] and evals._client is None
    # A write that was tried and swallowed would have said so.
    assert capsys.readouterr().out == ""


def test_the_client_waits_a_second_to_connect_and_to_read_and_retries_once():
    config = evals.CLIENT_CONFIG
    assert config.connect_timeout == 1 and config.read_timeout == 1
    # Section 4.3's retries={"max_attempts": 1}, which botocore's legacy mode reads as one retry.
    assert config.retries["total_max_attempts"] == 2


# --- who wrote it -----------------------------------------------------------------------------------


def test_the_author_of_an_article_is_every_model_that_wrote_it_and_none_that_judged_it():
    article = {
        "lineage": {
            "calls": [
                {"stage": "ideation", "model_id": "au.example.ideas-v1:0"},
                {"stage": "draft", "model_id": "au.example.writer-v1:0"},
                {"stage": "title", "model_id": "au.example.writer-v1:0"},
                {"stage": "adversarial_review", "model_id": "au.example.skeptic-v1:0"},
                {"stage": "revision", "model_id": "au.example.reviser-v1:0"},
                {"stage": "compliance_review", "model_id": "au.example.compliance-v1:0"},
                {"stage": "rewrite", "model_id": "au.example.rewriter-v1:0"},
            ],
            "research": {"calls": [{"stage": "research", "model_id": "au.example.researcher-v1:0"}]},
        }
    }
    assert evals.author_of(article) == {
        "au.example.ideas-v1:0",
        "au.example.writer-v1:0",
        "au.example.reviser-v1:0",
        "au.example.rewriter-v1:0",
    }


def test_the_author_of_a_finding_is_its_summarys_model_canonical():
    profile = "arn:aws:bedrock:ap-southeast-2:123456789012:inference-profile/au.example.researcher-v1:0"
    assert evals.author_of({"research_call": {"model_id": profile}}) == {"au.example.researcher-v1:0"}
    assert evals.author_of({"research_call": {"model_id": MODEL}}) == {MODEL}


@pytest.mark.parametrize(
    "item",
    [None, "a1b2", {}, {"lineage": None}, {"lineage": {"calls": "draft"}}, {"research_call": "x"},
     {"lineage": {"calls": [None, {"stage": "draft"}, {"stage": "draft", "model_id": 7}]}}],
)  # fmt: skip
def test_an_item_that_records_no_author_has_none_and_never_raises(item):
    assert evals.author_of(item) == frozenset()


# --- the interval -----------------------------------------------------------------------------------


def _scripts_wilson():
    path = ROOT / "scripts" / "vision_eval_scoring.py"
    spec = importlib.util.spec_from_file_location("vision_eval_scoring_for_evals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.wilson


def test_wilson_is_the_scripts_interval_exactly():
    original = _scripts_wilson()
    for n in range(0, 41):
        for k in range(0, n + 1):
            assert evals.wilson(k, n) == original(k, n), (k, n)


def test_wilson_stays_inside_zero_and_one_and_has_no_interval_without_trials():
    assert evals.wilson(0, 0) == (None, None)
    low, high = evals.wilson(0, 5)
    assert low == 0.0 and 0 < high < 1
    low, high = evals.wilson(5, 5)
    assert 0 < low < 1 and high == 1.0
    assert evals.wilson(8, 10)[0] > evals.wilson(4, 5)[0]  # more trials, a tighter lower bound
