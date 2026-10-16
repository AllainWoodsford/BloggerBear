"""`topic_findings`: a topic's research Findings, newest first, as a table on screen. "What did
research find for rail Sydney?", "what will the next crypto article be written from?", "why did the
topic write nothing today?".

Not `ops_mcp/findings.py`: every tool has a local variable `findings`, the attention cards (rule 4
of the design: `findings` never means research Findings, which go under `rows` and `table`).

**What is read.** The topic, as the operator said it (topic_match.pick_spoken), then the Findings
table, by that one topic's key and only ever with Query (common/dynamo.py's list_findings and
count_findings; a test holds the table to `query`). Three windows: everything that has not expired
(the default), the last `days` (1 to 14, the Finding TTL), or with `next_article` exactly what the
next daily run reads: from the topic's last article within its findings window (the one rule,
common/findings_window.window_start), expired or not, at most MAX_WINDOW_FINDINGS. `limit` (1 to
20) is how many rows go on screen; the count covers the window. Nothing in S3 is read: no snapshot,
no figure. With `next_article`, `subject` (a topic that rotates through subjects,
common/day_subject.py) narrows it to what the next article on that subject's day reads, as the
daily cycle reads it: the window from that subject's own last article, then only the Findings that
report it. The subject is never spoken (it is the operator's own text); the result echoes it.

**Why the reads are bounded.** A Finding's summary can be 16 KB, so a result holds at most
LIMIT_MAX rows, each summary cell is cut to SUMMARY_CELL_CHARS and the result's "(unverified)"
cells together to UNTRUSTED_BUDGET_CHARS, newest first; past that a cell says it was left out and
`untrusted_left_out` counts it. The count is a Select=COUNT Query over a bounded number of pages
(`count_is_at_least` when they ran out).

**What is never spoken, and what is never returned.** `spoken` is fixed templates over counts, ages
and the topic named by `tools.spoken_label`: never a summary's words, a caption, a planner note, a
source's title or a URL. A summary, written by a model from text off the web, goes scrubbed
(redact.scrub) into the one "Summary (unverified)" column, only for a caller with a screen
(memory.screen_caller): a voice caller's table has no such column and the result holds no
third-party text at all. Captions, alt text, planner-note text, source titles and URLs are not
returned by this tool: `admin_cli findings list` and the topic-findings skill show them to a person.
No `untrusted` block: the page shows only `spoken`, `findings` and `table`, and a block the page
never shows would reach only the model and Alexa+. Every row value is code-derived (counts, ages,
a checked model id, a stop reason from a fixed list), and every Decimal becomes a plain number.
"""

from __future__ import annotations

import math
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from botocore.exceptions import BotoCoreError, ClientError

from common.adapters.base import SUBJECT_KEY
from common.day_subject import subject_findings
from common.dynamo import count_findings, finding_is_expired, list_findings
from common.editorial_goals import resolve_goal_for_topic
from common.figures import clean_figure
from common.findings_window import last_article_at, window_start
from common.model_pricing import canonical_model_id
from ops_mcp import redact, topic_match
from ops_mcp.suggestions import ID_PATTERN
from ops_mcp.tools import _age, _parse, spoken_label

LIMIT_DEFAULT, LIMIT_MAX = 5, 20
DAYS_MAX = 14  # research_tick_handler.FINDING_TTL_DAYS (a test holds them equal)
MAX_WINDOW_FINDINGS = 48  # daily_cycle_handler.MAX_WINDOW_FINDINGS (a test holds them equal)
SUMMARIES_MAX_CHARS = 40_000  # daily_cycle_handler.SUMMARIES_MAX_CHARS (a test holds them equal)
SUMMARY_CELL_CHARS = 280
UNTRUSTED_BUDGET_CHARS = 6_000  # all "(unverified)" cells of one result, together
SPOKEN_MAX_WORDS = 120
# A paused topic (`topics pause`) has no next article until it is resumed: the daily cycle returns
# `paused` before it reads the window (daily_cycle_handler._run_daily_cycle, the same `is True`).
# Said after the next-article window's counts, and only there; the word cap never drops it.
PAUSED_NO_NEXT_ARTICLE = "It is paused, so there is no next article until it is resumed."
FINDINGS_TABLE_ENV = "FINDINGS_TABLE"
LEFT_OUT = "(left out: ask for fewer)"

# The windows, as `window.rule` names them.
RULE_DAYS, RULE_NEXT_ARTICLE, RULE_UNEXPIRED = "days", "next_article", "unexpired"
# A research call's stop reason is one of Bedrock's; anything else is "other".
STOP_REASONS = ("end_turn", "max_tokens", "stop_sequence", "tool_use")
NOTE_KINDS = ("flag", "simulation")
# A model id as it may be shown: a plain id, or the canonical id of a cross-Region profile ARN
# (common/model_pricing.canonical_model_id). Never an ARN: an application-inference-profile ARN
# comes back from canonical_model_id unchanged, and would carry the account id.
_MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,160}$")
_NOTE_TYPE = re.compile(r"^[a-z_]{1,40}$")

COLUMNS_SCREEN = [
    "Captured",
    "Age",
    "Expires in",
    "Maps",
    "Planner notes",
    "Sources",
    "Model",
    "Summary (unverified)",
]
COLUMNS_VOICE = [column for column in COLUMNS_SCREEN if not column.endswith("(unverified)")]

NO_TABLE = "I haven't been told where the findings are."
NOT_AN_ID = "That topic's id isn't one I can look up."
NOT_A_SUBJECT = "That subject isn't one I can look up."
SUBJECT_NEEDS_NEXT_ARTICLE = "A subject narrows only what the next article reads: ask with next_article."
REFUSED_ACCESS = "AWS refused to let me read the findings table."
REFUSED_MISSING = "The findings table isn't where I was told it is."
REFUSED_OTHER = "I couldn't read the findings just now."
REFUSED_UNREACHABLE = "I couldn't reach DynamoDB just now."


# --- the rows: every value code-derived ------------------------------------------------------------


def _plain(value):
    """A stored number as a JSON number: a whole Decimal an int, another a float; anything that is
    not a finite number is None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return None
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def _count(value) -> int:
    number = _plain(value)
    return int(number) if isinstance(number, int | float) and number >= 0 else 0


def _epoch(value) -> datetime | None:
    """Epoch seconds as a datetime, or None for anything that is not one: a non-number, a negative,
    or a value past the year 9999 (a millisecond epoch, a hand-edited TTL), which datetime refuses."""
    seconds = _plain(value)
    if not isinstance(seconds, int | float) or seconds < 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _model_id(value) -> str | None:
    model_id = canonical_model_id(value)
    if not isinstance(model_id, str) or model_id.startswith("arn:") or not _MODEL_ID.match(model_id):
        return None
    return model_id


def _research(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None:
        stop_reason = stop_reason if stop_reason in STOP_REASONS else "other"
    return {
        "model_id": _model_id(raw.get("model_id")),
        "used_fallback": raw.get("used_fallback") is True,
        "stop_reason": stop_reason,
        "cut_off": stop_reason == "max_tokens",
        "input_tokens": _count(raw.get("input_tokens")),
        "output_tokens": _count(raw.get("output_tokens")),
    }


def _planner_notes(raw) -> dict:
    counted = {"flag": 0, "simulation": 0, "types": {}}
    for note in raw if isinstance(raw, list) else []:
        if not isinstance(note, dict) or note.get("kind") not in NOTE_KINDS:
            continue
        counted[note["kind"]] += 1
        kind_of = note.get("type")
        kind_of = kind_of if isinstance(kind_of, str) and _NOTE_TYPE.match(kind_of) else "other"
        counted["types"][kind_of] = counted["types"].get(kind_of, 0) + 1
    return counted


def row(item: dict, now: datetime) -> dict:
    """One Finding as the result shows it: when, how old, when it expires, and counts of what it
    holds. No text from it: the summary goes only into the table's "(unverified)" cell."""
    captured = _parse(item.get("captured_at"))
    expires = _epoch(item.get("expires_at"))
    expired = finding_is_expired(item, int(now.timestamp()))
    if expires is None:
        expires_in = None
    elif expired:
        expires_in = "expired"
    else:
        expires_in = _age(now, expires)
    summary = item.get("summary")
    figures = item.get("figures")
    sources = item.get("source_refs")
    return {
        "captured_at": captured.isoformat() if captured else None,
        "age": _age(captured, now),
        "expires_at": expires.isoformat() if expires else None,
        "expires_in": expires_in,
        "expired": expired,  # only a next_article read keeps expired items
        # Fixed: a Finding exists only when a tick's diff was material (research_tick_handler.py);
        # there is no field for it.
        "material": True,
        "figures": sum(1 for figure in figures if clean_figure(figure)) if isinstance(figures, list) else 0,
        "planner_notes": _planner_notes(item.get("planner_notes")),
        "sources": len(sources) if isinstance(sources, list) else 0,
        "research": _research(item.get("research_call")),
        "summary_chars": len(summary) if isinstance(summary, str) else 0,
    }


# --- the table --------------------------------------------------------------------------------------


def _notes_cell(notes: dict) -> str:
    parts = []
    if notes["flag"]:
        parts.append(f"{notes['flag']} flag{'s' if notes['flag'] != 1 else ''}")
    if notes["simulation"]:
        parts.append(f"{notes['simulation']} simulation{'s' if notes['simulation'] != 1 else ''}")
    return ", ".join(parts)


def _model_cell(research: dict | None) -> str:
    if not research:
        return ""
    text = research["model_id"] or ""
    if research["cut_off"]:
        text = f"{text} (cut off)".strip()
    if research["used_fallback"]:
        text = f"{text} (fallback)".strip()
    return text


def _table(label: str, rows: list[dict], items: list[dict], *, screen: bool) -> tuple[dict, int]:
    """The table, and how many summary cells were left out of it. The summary cells are filled
    newest first while the result's "(unverified)" characters stay within the budget; a voice
    caller's table has no such column at all."""
    columns = COLUMNS_SCREEN if screen else COLUMNS_VOICE
    used, left_out = 0, 0
    table_rows = []
    for shown, item in zip(rows, items, strict=True):
        captured = _parse(shown["captured_at"])
        cells = [
            captured.strftime("%Y-%m-%d %H:%M") if captured else "",
            f"{shown['age']} ago" if captured else "",
            shown["expires_in"] or "",
            shown["figures"],
            _notes_cell(shown["planner_notes"]),
            shown["sources"],
            _model_cell(shown["research"]),
        ]
        if screen:
            summary = redact.scrub(item.get("summary"), SUMMARY_CELL_CHARS)
            if used + len(summary) <= UNTRUSTED_BUDGET_CHARS:
                used += len(summary)
            else:
                summary, left_out = LEFT_OUT, left_out + 1
            cells.append(summary)
        table_rows.append(cells)
    return {"title": f"{label}: research findings", "columns": list(columns), "rows": table_rows}, left_out


# --- what is spoken ---------------------------------------------------------------------------------


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _window_words(
    rule: str, days: int | None, *, since_last_article: bool, one: bool, subject: bool = False
) -> str:
    if rule == RULE_DAYS:
        return "in the last day" if days == 1 else f"in the last {days} days"
    if rule == RULE_NEXT_ARTICLE and subject:
        # The subject itself is never spoken: it is the operator's text, and on screen already.
        return "for its next article on that subject" + (" since its last one" if since_last_article else "")
    if rule == RULE_NEXT_ARTICLE:
        return "for its next article" + (" since its last article" if since_last_article else "")
    return "that hasn't expired" if one else "that haven't expired"


def _spoken(
    topic: dict,
    rows: list[dict],
    *,
    count: int,
    at_least: bool,
    window: str,
    rule: str,
    over_draft_limit: bool,
    now: datetime,
    prefix: str,
) -> str:
    """The spoken answer, from fixed templates (the module docstring says what is never in it),
    under SPOKEN_MAX_WORDS."""
    label = spoken_label(topic)
    paused = f" {PAUSED_NO_NEXT_ARTICLE}" if rule == RULE_NEXT_ARTICLE and topic.get("paused") is True else ""
    if count == 0:
        researched = _parse(topic.get("last_research_at"))
        if researched is None:
            return f"{prefix}{label} has no findings {window}, and it hasn't been researched yet.{paused}"
        return (
            f"{prefix}{label} has no findings {window}. Research last ran {_age(researched, now)} ago: "
            f"a tick writes a finding only when something changed.{paused}"
        )
    newest = _parse(rows[0]["captured_at"]) if rows else None
    how_many = f"at least {_plural(count, 'finding')}" if at_least else _plural(count, "finding")
    sentences = [f"{prefix}{label} has {how_many} {window}, the newest {_age(newest, now)} ago."]
    if count > len(rows):
        sentences.append(f"The newest {len(rows)} are on screen.")
    else:
        sentences.append("It's on screen." if count == 1 else "They're on screen.")
    with_maps = sum(1 for shown in rows if shown["figures"])
    with_notes = sum(1 for shown in rows if _notes_cell(shown["planner_notes"]))
    if len(rows) == 1 and (with_maps or with_notes):
        has = [name for name, some in (("maps", with_maps), ("planner notes", with_notes)) if some]
        sentences.append(f"It has {' and '.join(has)}.")
    elif with_maps or with_notes:
        parts = []
        if with_maps:
            parts.append(f"{with_maps} {'has' if with_maps == 1 else 'have'} maps")
        if with_notes:
            parts.append(f"{with_notes} {'has' if with_notes == 1 else 'have'} planner notes")
        sentences.append(f"Of those, {' and '.join(parts)}.")
    cut_off = sum(1 for shown in rows if shown["research"] and shown["research"]["cut_off"])
    if cut_off == 1:
        sentences.append("One summary was cut off at its token limit.")
    elif cut_off:
        sentences.append(f"{cut_off} summaries were cut off at their token limit.")
    if rule == RULE_NEXT_ARTICLE:
        if count > MAX_WINDOW_FINDINGS:
            sentences.append(f"The next article reads at most {MAX_WINDOW_FINDINGS}.")
        if over_draft_limit:
            sentences.append(
                "Their summaries are longer than the draft reads, so the oldest may be left out."
            )
        if resolve_goal_for_topic(topic, now.date()) is not None:
            sentences.append("A topic with editorial goals may use only some of them.")
    # The cap drops sentences from the end, never the first two. The paused sentence is the most
    # decisive thing the answer says, so it is kept out of what the cap drops: the others are
    # trimmed to leave it room, then it is added. Words add up exactly over a join with one space,
    # so the cap holds as it did before: under SPOKEN_MAX_WORDS, unless only the first two are left.
    kept = [PAUSED_NO_NEXT_ARTICLE] if paused else []
    room = SPOKEN_MAX_WORDS - sum(len(sentence.split()) for sentence in kept)
    text = " ".join(sentences)
    while len(text.split()) >= room and len(sentences) > 2:
        sentences.pop()
        text = " ".join(sentences)
    return " ".join([*sentences, *kept])


# --- the tool ---------------------------------------------------------------------------------------


def _refusal(spoken: str, matched: dict | None) -> dict:
    return {"spoken": spoken, "findings": [], "rows": [], "matched_topic": matched}


def _refused(exc: ClientError) -> str:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ResourceNotFoundException":
        return REFUSED_MISSING
    if code in ("AccessDeniedException", "UnauthorizedOperation"):
        return REFUSED_ACCESS
    return REFUSED_OTHER


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high)


def topic_findings(
    topic: str,
    limit: int = LIMIT_DEFAULT,
    days: int | None = None,
    next_article: bool = False,
    *,
    screen: bool,
    now: datetime | None = None,
    subject: str | None = None,
) -> dict:
    """A topic's research Findings, newest first: the unexpired ones, the last `days`, or with
    `next_article` what the next daily run reads (with `subject`, the next run on that subject's
    day, for a topic that rotates). `screen` is whether the caller reads a screen
    (memory.screen_caller): a voice caller's table has no "Summary (unverified)" column."""
    now = now or datetime.now(UTC)
    if subject is not None and (not isinstance(subject, str) or not SUBJECT_KEY.fullmatch(subject)):
        return _refusal(NOT_A_SUBJECT, None)
    if subject is not None and not next_article:
        return _refusal(SUBJECT_NEEDS_NEXT_ARTICLE, None)
    one, matched, refusal = topic_match.pick_spoken(topic)
    if one is None:
        return _refusal(refusal, matched)
    topic_id = one.get("topic_id")
    if not isinstance(topic_id, str) or not ID_PATTERN.match(topic_id):
        return _refusal(NOT_AN_ID, matched)
    if not os.environ.get(FINDINGS_TABLE_ENV):
        return _refusal(NO_TABLE, matched)
    prefix = topic_match.took(matched)

    limit = _clamp(limit, 1, LIMIT_MAX, LIMIT_DEFAULT)
    days = _clamp(days, 1, DAYS_MAX, 1) if days is not None else None
    since_last_article = False
    over_draft_limit = False
    try:
        if next_article and subject is not None:
            # What the daily run on the subject's day reads: from the subject's last article within
            # the window, expired or not, the run's cap of it (one more is read, to know whether
            # there was more), then only the Findings that report the subject.
            rule, since = RULE_NEXT_ARTICLE, window_start(one, now, subject_key=subject)
            previous = _parse(last_article_at(one, subject))
            since_last_article = previous is not None and previous.isoformat() == since
            window_items, expired_left_out, _ = list_findings(
                topic_id, since=since, limit=MAX_WINDOW_FINDINGS + 1, unexpired_at=None
            )
            read = subject_findings(window_items[:MAX_WINDOW_FINDINGS], subject)
            count, at_least = len(read), len(window_items) > MAX_WINDOW_FINDINGS
            over_draft_limit = sum(len(each.get("summary") or "") for each in read) > SUMMARIES_MAX_CHARS
            items = read[:limit]
        elif next_article:
            # What the daily run reads: from the last article within the window, expired or not, at
            # most the cap (one more is read, to know whether there are more than it reads).
            rule, since = RULE_NEXT_ARTICLE, window_start(one, now)
            previous = _parse(one.get("last_article_at"))
            since_last_article = previous is not None and previous.isoformat() == since
            window_items, expired_left_out, _ = list_findings(
                topic_id, since=since, limit=MAX_WINDOW_FINDINGS + 1, unexpired_at=None
            )
            count, at_least = len(window_items), len(window_items) > MAX_WINDOW_FINDINGS
            read = window_items[:MAX_WINDOW_FINDINGS]
            summary_chars = sum(len(each.get("summary") or "") for each in read)
            over_draft_limit = summary_chars > SUMMARIES_MAX_CHARS
            items = window_items[:limit]
        else:
            unexpired_at = int(now.timestamp())
            if days is not None:
                rule, since = RULE_DAYS, (now - timedelta(days=days)).isoformat()
            else:
                rule, since = RULE_UNEXPIRED, None
            items, expired_left_out, _ = list_findings(
                topic_id, since=since, limit=limit, unexpired_at=unexpired_at
            )
            count, complete = count_findings(topic_id, since=since, unexpired_at=unexpired_at)
            at_least = not complete
    except ClientError as exc:
        return _refusal(_refused(exc), matched)
    except BotoCoreError:
        return _refusal(REFUSED_UNREACHABLE, matched)

    rows = [row(item, now) for item in items]
    table, untrusted_left_out = _table(spoken_label(one), rows, items, screen=screen)
    window = _window_words(
        rule, days, since_last_article=since_last_article, one=count == 1, subject=subject is not None
    )
    return {
        "spoken": _spoken(
            one,
            rows,
            count=count,
            at_least=at_least,
            window=window,
            rule=rule,
            over_draft_limit=over_draft_limit,
            now=now,
            prefix=prefix,
        ),
        "findings": [],
        "matched_topic": matched,
        "topic_id": topic_id,
        "window": {
            "since": since,
            "until": now.isoformat(),
            "rule": rule,
            **({"subject": subject} if subject is not None else {}),
        },
        "count": count,
        "count_is_at_least": at_least,
        "expired_left_out": expired_left_out,
        "untrusted_left_out": untrusted_left_out,
        "over_draft_limit": over_draft_limit,
        "more": count > len(rows),
        "rows": rows,
        "table": table,
        "as_of": now.isoformat(),
    }
