"""What the assistant's tools read and return. Plain functions over common/dynamo.py: no MCP here,
so they are tested like any other code, and server.py only has to register them. This module holds
the pipeline's own state (pipeline_health, admin_inbox) and the helpers the others share;
content.py and account.py hold the rest, under the same rules.

Every tool returns the same shape:

    spoken    a few sentences to be read aloud
    findings  what needs the operator's attention, each with its suggestion (suggestions.py)
    ...       the data behind it, for the page to show

**Read-only.** Nothing here writes to a table.

**What is said aloud is written here, not by a model and not by whoever wrote the article.** Hold
reasons from the compliance review and article titles are text a model produced from the web, so
they are never put into `spoken`: a held article is described by which *kind* of hold it has
(_hold_kinds). The structured data carries the title and reasons for the page, cut short and
stripped of control characters (common/security_events.untrusted_text), and marked `untrusted`.

**"Today" is the last 26 hours.** A rolling window, not a calendar day: it needs no time zone,
and the two hours over a day allow for a daily run that starts a little later than yesterday's.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common.dynamo import (
    get_article,
    get_newest_article_for_topic,
    get_pipeline_config,
    get_topic,
    list_failed_executions,
    list_pending_moderation,
    list_topics,
)
from common.research_schedule import resolve_interval_hours
from common.security_events import untrusted_text
from ops_mcp.suggestions import ID_PATTERN, finding

DAY_WINDOW = timedelta(hours=26)
# Research is late once two of its intervals have passed with no check: one missed heartbeat is
# jitter, two is a schedule that isn't running.
RESEARCH_LATE_INTERVALS = 2
INBOX_DEFAULT_LIMIT = 5
INBOX_MAX_LIMIT = 20
TITLE_MAX_CHARS = 120

# The reasons the pipeline itself writes (daily_cycle_handler.py, common/compliance.py,
# common/rewrite.py), matched by how they start. Anything else on a held article came from a
# review, in a model's words.
_HOLD_PREFIXES = (
    ("draft truncated", "draft_truncated"),
    ("title looks like a refusal", "implausible_title"),
    ("sent back by a person for a rewrite", "sent_back"),
    ("a rewrite finished but could not be saved", "rewrite_incomplete"),
)
_FINANCIAL = "financial topic"
_HOLD_SPOKEN = {
    "draft_truncated": "its draft was cut short",
    "implausible_title": "its title doesn't read like a title",
    "sent_back": "you sent it back for a rewrite",
    "rewrite_incomplete": "a rewrite of it could not be saved completely",
    "financial_topic": "it is on a financial topic",
    "review_flagged": "a review flagged it",
    "fresh_data_review": "the fresh-data review has notes on it",
    "rewrite_failed": "its last rewrite did not work",
}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def _parse(timestamp) -> datetime | None:
    """A stored ISO-8601 timestamp, or None if it is missing or unreadable. Stored in UTC."""
    if not isinstance(timestamp, str) or not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(timestamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _age(since: datetime | None, now: datetime) -> str:
    """How long ago, in words for speech: "40 minutes", "5 hours", "3 days"."""
    if since is None:
        return "an unknown time"
    minutes = max(int((now - since).total_seconds() // 60), 0)
    if minutes < 90:
        return f"{minutes} minute{'s' if minutes != 1 else ''}"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} hours"
    return f"{hours // 24} days"


def _join(parts: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _clamp_days(days, most: int) -> int:
    """A tool's `days` argument, kept between one day and `most`."""
    return min(max(int(days), 1), most)


def _topic_label(topic: dict | None, topic_id: str) -> str:
    """The topic's name as the operator set it, cut short, or its id."""
    return untrusted_text((topic or {}).get("name") or topic_id, 60)


# --- pipeline_health -----------------------------------------------------------------------------


def _research_state(topic: dict, config: dict | None, now: datetime) -> dict:
    interval = resolve_interval_hours(topic, config)
    last = _parse(topic.get("last_research_at"))
    late = last is None or now - last > timedelta(hours=interval * RESEARCH_LATE_INTERVALS)
    return {
        "interval_hours": interval,
        "last_research_at": last.isoformat() if last else None,
        "state": "late" if late else "on_time",
    }


def _article_state(topic_id: str, failures: list[dict], now: datetime, detail: bool) -> dict:
    """What became of the topic's daily run in the window: `published`, `held` (waiting for a
    person), `rejected`, `failed` (it ran out of retries) or `none` (nothing was written, which
    is also what a day with no new findings looks like)."""
    since = now - DAY_WINDOW
    newest = get_newest_article_for_topic(topic_id)
    created = _parse((newest or {}).get("created_at"))
    if newest is not None and created is not None and created >= since:
        state = {"published": "published", "pending_moderation": "held", "rejected": "rejected"}.get(
            newest.get("status"), "none"
        )
        return {"state": state, "article_id": newest.get("article_id"), "created_at": created.isoformat()}

    recent = [
        f
        for f in failures
        if f.get("topic_id") == topic_id and (_parse(f.get("created_at")) or since) >= since
    ]
    if not recent:
        return {"state": "none"}
    latest = max(recent, key=lambda f: f.get("created_at") or "")
    state = {"state": "failed", "failed_at": latest.get("created_at"), "failures": len(recent)}
    if detail:
        error = latest.get("error")
        name = error.get("Error") if isinstance(error, dict) else error
        state["error"] = untrusted_text(name, 80) or "unknown"
    return state


def _topic_check(
    item: dict, config: dict | None, failures: list[dict], now: datetime, *, detail: bool = False
) -> tuple[dict, list[dict]]:
    """One topic's row and the findings about it. pipeline_health runs it for every topic;
    memory.py runs it again for one topic, to see whether what it suggested still holds."""
    topic_id = item.get("topic_id") or ""
    label = _topic_label(item, topic_id)
    research = _research_state(item, config, now)
    article = _article_state(topic_id, failures, now, detail=detail)
    row = {"topic_id": topic_id, "name": label, "research": research, "article": article}

    findings = []
    if research["state"] == "late":
        last = _parse(research["last_research_at"])
        ago = f"last checked {_age(last, now)} ago" if last else "it has never been checked"
        findings.append(
            finding("research_overdue", f"{label} wasn't researched on time: {ago}", topic_id, topic=label)
        )
    if article["state"] == "failed":
        findings.append(
            finding("run_failed", f"{label}'s daily run failed and ran out of retries", topic_id, topic=label)
        )
    elif article["state"] == "none":
        findings.append(
            finding("no_article_today", f"{label} has no article in the last day", topic_id, topic=label)
        )
    return row, findings


def pipeline_health(topic: str | None = None, *, now: datetime | None = None) -> dict:
    """Per topic: was it researched on time, and what became of its daily run in the last 26
    hours. With `topic`, that topic alone, and the error of a run that failed."""
    now = _now(now)
    if topic is not None:
        if not ID_PATTERN.match(topic):
            return {"spoken": "That isn't a topic id I can look up.", "findings": [], "topics": []}
        one = get_topic(topic)
        if one is None:
            return {"spoken": "I can't find a topic with that id.", "findings": [], "topics": []}
        topics = [one]
    else:
        topics = sorted(list_topics(), key=lambda t: t.get("topic_id") or "")
    config = get_pipeline_config()
    failures = list_failed_executions()

    rows, findings = [], []
    for item in topics:
        row, found = _topic_check(item, config, failures, now, detail=topic is not None)
        rows.append(row)
        findings.extend(found)

    return {"spoken": _health_spoken(rows), "findings": findings, "topics": rows, "as_of": now.isoformat()}


def _health_spoken(rows: list[dict]) -> str:
    if not rows:
        return "There are no topics."
    by_state: dict[str, list[str]] = {}
    for row in rows:
        by_state.setdefault(row["article"]["state"], []).append(row["name"])
    late = [row["name"] for row in rows if row["research"]["state"] == "late"]

    sentences = []
    for state, words in (
        ("published", "published"),
        ("held", "wrote an article that is held for review"),
        ("rejected", "had its article rejected"),
        ("failed", "failed its daily run"),
        ("none", "has no article in the last day"),
    ):
        names = by_state.get(state)
        if names:
            sentences.append(f"{_join(names)} {words}.")
    if late:
        sentences.append(f"Research is late for {_join(late)}.")
    elif len(rows) > 1:
        sentences.append("Research is on time for every topic.")
    return " ".join(sentences)


# --- admin_inbox ---------------------------------------------------------------------------------


def _hold_kinds(item: dict) -> list[str]:
    """Why a queue item is held, as kinds from a fixed list -- never the reasons' own words."""
    kinds: list[str] = []
    for reason in item.get("reasons") or []:
        text = str(reason).lower()
        kind = next((kind for prefix, kind in _HOLD_PREFIXES if text.startswith(prefix)), None)
        if kind is None:
            kind = "financial_topic" if _FINANCIAL in text else "review_flagged"
        if kind not in kinds:
            kinds.append(kind)
    if item.get("review_notes"):
        kinds.append("fresh_data_review")
    if item.get("last_rewrite_error"):
        kinds.append("rewrite_failed")
    return kinds


def _truncated_finding(name: str, article_id) -> dict:
    """The finding for a held article whose draft was cut short (memory.py rebuilds it too)."""
    return finding(
        "draft_truncated",
        f"A held {name} article has a draft that was cut short",
        article_id,
        topic=name,
        article_id=article_id,
    )


def admin_inbox(
    topic: str | None = None, limit: int = INBOX_DEFAULT_LIMIT, *, now: datetime | None = None
) -> dict:
    """The articles waiting for a person, oldest first: how many, and for the first `limit`,
    the topic, how long it has waited and why it is held. With `topic`, that topic's alone."""
    now = _now(now)
    limit = min(max(int(limit), 1), INBOX_MAX_LIMIT)
    waiting = list_pending_moderation()  # oldest first
    if topic is not None:
        waiting = [item for item in waiting if item.get("topic_id") == topic]

    names: dict[str, str] = {}

    def label(topic_id: str) -> str:
        if not topic_id:
            return "an unknown topic"
        if topic_id not in names:
            names[topic_id] = _topic_label(get_topic(topic_id), topic_id)
        return names[topic_id]

    rows, findings = [], []
    for item in waiting[:limit]:
        article_id = item.get("article_id")
        article = get_article(article_id) if article_id else None
        kinds = _hold_kinds(item)
        waited_since = _parse(item.get("created_at"))
        name = label(item.get("topic_id") or "")
        rows.append(
            {
                "queue_id": item.get("queue_id"),
                "article_id": article_id,
                "topic_id": item.get("topic_id"),
                "topic": name,
                "waiting_since": item.get("created_at"),
                "waiting": _age(waited_since, now),
                "held_for": kinds,
                # A model wrote these from text off the web: for the page, never for speech.
                "untrusted": {
                    "title": untrusted_text((article or {}).get("title"), TITLE_MAX_CHARS),
                    "reasons": [untrusted_text(reason) for reason in item.get("reasons") or []],
                },
            }
        )
        if "draft_truncated" in kinds:
            findings.append(_truncated_finding(name, article_id))
    if waiting:
        count = len(waiting)
        noticed = f"{count} article{'s are' if count != 1 else ' is'} waiting for review"
        findings.append(finding("awaiting_review", noticed, None, count=count))

    return {
        "spoken": _inbox_spoken(len(waiting), rows),
        "findings": findings,
        "waiting": len(waiting),
        "items": rows,
        "as_of": now.isoformat(),
    }


def _inbox_spoken(count: int, rows: list[dict]) -> str:
    if count == 0:
        return "Nothing is waiting in the inbox."
    opening = f"{count} article{'s are' if count != 1 else ' is'} waiting in the inbox."
    lines = []
    for row in rows:
        why = _join([_HOLD_SPOKEN[kind] for kind in row["held_for"]]) or "no reason was recorded"
        lines.append(f"{row['topic']}, waiting {row['waiting']}: {why}.")
    rest = count - len(rows)
    more = f" And {rest} more." if rest > 0 else ""
    return f"{opening} Oldest first. {' '.join(lines)}{more}"
