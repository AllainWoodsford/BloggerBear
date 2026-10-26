"""`vision_evidence`: what the vision pipeline measured and decided, and what a person did about it,
read from the pipeline's logs alone. "What did the vision agent do for rail Sydney this week?",
"why wasn't that change reported?", "walk me through the latest Sydney scene". The owner's ask of
the submission round: the assistant and Alexa+ can interrogate the evidence, via logs if necessary.

**What is read.** The evidence lines (common/evidence.py) of the research-tick and admin-api log
groups in the home Region, through three fixed Logs Insights queries started together and waited
for within one budget (WAIT_SECONDS): a topic's `vision_decision` and `vision_triage` lines, the
`research_tick` lines of the attempts that made a decision, and the `moderation_decision` lines of
what people did. Then, for each topic with a Finding written, one short DynamoDB Query on the
Articles table's topic index (common/dynamo.list_articles_for_topic, which reads only the fields
the join uses) for the articles the Findings fed. A read that would start after DEADLINE_SECONDS
is skipped, and one that times out or runs out of time between pages is stopped; that topic's
article counts and what people did are then not checked, as when a read fails, never a partial
count. Nothing else: `vision_site` lines are never read (24 a day per site would fill the
1,000-row cap within weeks), no `stats` aggregate is used (it would count lines, so a failed
attempt's decisions would count twice), and the running state is not opened.

**One limit for the whole call** (CALL_LIMIT_SECONDS, 12 s from its start: Alexa+ gives up after
roughly 15 s, API Gateway after 29). Every AWS call is held to the time left before it: the Topics
read, the access check (the log groups' tags and the account id), starting the queries, polling
them, stopping the ones still running, and the Articles reads. Each request goes through a client
built for it with a one-second connect, the time left as its read timeout and one attempt
(_Budget, _Bounded), and one that would start at or after the limit is not made. A request Logs
throttles is asked again above botocore instead (logs.run_queries' recover_throttles): a poll next
round, a start once more, each a new request held to the time left. A step out of time ends as
when it fails, never as a partial count or an exception. The worst case for a request that stalls
is 12 + 1 + 0.1 + 0.2 = 13.3 s: the limit, one connect, the read timeout's floor and the request's
client build (0.2 s measured cold); the name lookup each request makes before it connects is
outside these timeouts, and each request makes its own. A read timeout is an idle bound, though: a
request whose bytes keep trickling in is bounded only by the Lambda's 30 s timeout, and a hard end
per request would need a thread, which reads this small (a DynamoDB Query of at most 200 small
items) do not warrant.

**Parsed, never trusted** (the submission round's rule 7). The only values put into a query are a
topic id that passed suggestions.ID_PATTERN and a scene id that passed SCENE_ID, each a quoted
string compared by equality, never in a regex: `rail-sydney` never matches `rail-sydney-demo`.
Every `@message` goes through evidence.parse, and a row is then dropped, and counted under
`dropped`, when its topic id is not one asked for (re-checked here), its event not one its query
asked for, its decision code not one of vision_decisions.CODES, its stop not one of
vision_triage.STOPS, its verdict not one of vision_triage.VERDICTS, a tool not one of the triage
profiles' tools or `other`, a moved measure not in the union of the adapters' declared
vocabularies, its coverage outside 0 to 1 (a share of the site), or it lacks the ids it is joined
by. A line that is not JSON is dropped too.

**Kept, reported and written are three facts** (docs/enhancements/archive/vision-evidence.md). A
`vision_triage` row joins its decision by (tick, run, site, scene) and a decision its attempt by
(tick, run). An attempt whose `research_tick` line says `stored: false`, or has none (the tick timed
out), did not keep its decisions: the next tick measured the scene again. Rows are de-duplicated by
(topic, site, scene), keeping the kept copy; every not-kept copy is counted under `not_kept`, never
as a decision (a scene with no kept copy yet is shown once, "not kept: measured again"). A Finding
was written only when the attempt's `finding` is the decision's `at`; the article that Finding fed
is the one whose lineage holds that `captured_at`, and its status, with the time of the newest
`moderation_decision` row for it, says what a person did.

**No adapter is named** (rule 5). The topics that keep decision evidence are those whose adapter
declares a non-empty `decision_vocabulary()`; the words for the measures, the moved measures and
the look_again settings come from that vocabulary, and the fixed words for every step from
vision_decisions.TRACE_WORDS. The scene view is vision_decisions.trace, the one rendering of a
decision's chain that every reader shares: its look_again rows state what was re-measured at which
settings beside the same scene's numbers at the topic's settings, and never compare a
re-measurement with the baseline.

**Everything is code-derived.** Codes, ids, times and numbers, so there is no "(unverified)" column
and no `untrusted` key: the page and Alexa+ get the same result. `spoken` is fixed templates over
counts, topics named by tools.spoken_label and the vocabularies' words, under SPOKEN_MAX_WORDS;
never a site's name or id, a scene id, the agent's reason, a news host or a build's hex. Spend is
shown in AUD at the project's fixed rate (common/costing.py), the currency the Stats page uses: the
cost of every triage the logs hold, a failed attempt's included (it was spent).

**Topics are never summed together.** Each topic is counted on its own, under its entry in
`topics`; the all-topics answer says one sentence per topic, and has no top-level `counts` or
`spend_aud` (they are null: read each topic's from `topics[].counts` and `topics[].spend_aud`), so
the demo topic's numbers never inflate Sydney's. A topic with several sites keeps one sentence, and
the table splits it by site.

**A named exception to logs.window's limits** (LOOKBACK_DAYS = 30, MAX_HOURS = 168): this module's
queries are fixed text, filter by field equality on one topic, and read only the research-tick and
admin-api groups, so a 90-day scan stays small. The logs' own retention is 90 days. The window
helper itself is unchanged; the exception is EVIDENCE_LOOKBACK_DAYS here.

**Read-only.** The Topics read (dynamo.list_topics, or topic_match's lookup), StartQuery,
GetQueryResults and StopQuery through logs.run_queries (after its tag check), and a DynamoDB Query
per topic with a Finding written. Nothing here writes.
"""

from __future__ import annotations

import math
import os
import re
import time
from datetime import UTC, datetime, timedelta
from functools import partial

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from common import evidence, vision_decisions
from common.adapters.registry import ADAPTER_REGISTRY
from common.costing import USD_TO_AUD_RATE
from common.dynamo import list_articles_for_topic, list_topics
from common.vision_triage import PROFILES, STOPS, VERDICTS
from ops_mcp import architecture, logs, topic_match
from ops_mcp.suggestions import ID_PATTERN
from ops_mcp.tools import _join, spoken_label

DAYS_DEFAULT = 7
# A named exception to logs.window's limits (LOOKBACK_DAYS = 30, MAX_HOURS = 168, logs.py): this
# module's queries are fixed text, filter by field equality on one topic, and read only the
# research-tick and admin-api groups, so a 90-day scan stays small. The logs' own retention is 90
# days. logs.window itself is unchanged.
EVIDENCE_LOOKBACK_DAYS = 90
# The Logs wait: one budget for every query of one call, counted from when they are started, and
# cut short by CALL_LIMIT_SECONDS when that comes first.
WAIT_SECONDS = 8.0
# The whole call's hard limit, from its start. Alexa+ gives up after roughly 15 s (alexa/README.md),
# and the tool answers through API Gateway, which gives up after 29 s (on the ask page the ops
# agent's own model calls share those 29 s, ops_agent/agent.py), inside the ops-mcp Lambda's 30 s.
# So every AWS call the tool makes is held to the time left before it: the Topics read, the access
# check (the two log groups' tags, and the account id their ARNs need), starting the queries,
# polling them, stopping the ones still running, and the Articles reads. Each request goes through
# a client built for it with a one-second connect, the time left as its read timeout (at least
# MIN_READ_SECONDS) and one attempt (_Budget, _Bounded); a request that would start at or after the
# limit is not made (OutOfTime). A step out of time ends as when it fails: the access check refuses
# the log, a query is not checked ("didn't answer in time"), a stop is skipped (a query left running
# is cheap, and ends by itself), an Articles read is not checked, the Topics read is said as not
# read (TOPICS_NOT_READ). Normally the steps before the wait are a few small requests, the wait
# takes at most its 8 s, and the Articles reads have the rest of the 12 s.
# The worst case for a request that stalls, from the call's start, is a sum: the last request starts
# before the limit and ends by its connect, its read timeout and the build of its client, which comes
# after the time left is read: 12 + 1 + 0.1 + 0.2 = 13.3 s (the build measured 0.2 s for the first
# client of a service in a process, 10 ms after), inside Alexa+'s 15 s and API Gateway's 29 s. The
# name lookup each request makes before it connects is outside these timeouts, and each request
# makes its own (a client per request reuses no connection).
# A read timeout is an idle bound, the longest wait between two reads of the socket, so a request
# that stalls ends as above but one whose bytes keep trickling in is bounded only by the Lambda's
# 30 s timeout: a hard end per request would need a thread, which a DynamoDB Query of at most 200
# small items, or a Logs answer of at most 1,000 short rows, does not warrant.
CALL_LIMIT_SECONDS = 12.0
# The Articles reads' deadline, from the call's start: a read that would start at or after it is
# skipped, every page of one that has started is given only the time left before it, and that
# topic's articles and person step are then said as not checked. It is the call's limit: the reads
# are its last AWS calls, so a later deadline could not be kept and an earlier one would only skip
# reads that have time to run. A page that stalls at it ends, as any request, by 12 + 1 + 0.1 + 0.2
# = 13.3 s from the call's start (above), and one that trickles by the Lambda's 30 s.
DEADLINE_SECONDS = CALL_LIMIT_SECONDS
# Each request's connect, as vision_status._s3's, and the least read timeout it is given (botocore
# needs one above zero). One attempt per request, so a request that stalls is never sent twice. A
# request Logs throttles is asked again above botocore instead (logs.run_queries' recover_throttles):
# a poll counts as still running and is asked again next round, within the wait, and a start is tried
# once more after one pause, each through a client _Budget builds with the time left, so none is
# made at or after the limit and the sum above still holds.
CONNECT_SECONDS = 1.0
MIN_READ_SECONDS = 0.1
# The functions whose log groups are read, by their catalogue keys (architecture.py).
TICK_FUNCTION = "research-tick"
ADMIN_FUNCTION = "admin-api"
# What `scene` may be: a scene id as the adapters write them (a Sentinel-2 item id), or the word for
# the newest decision.
SCENE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
LATEST = "latest"
SPOKEN_MAX_WORDS = 120
# The largest measure `spoken` says, after the vocabulary's scale: the adapters' measures are
# shares, areas of a city in km², and counts of stations or objects, all far below a million. A
# larger value on a line is corrupt or forged, and read aloud it would be noise, so the scene answer
# says "OpenCV measured it." instead.
SPOKEN_MEASURE_MAX = 1e6
# policy.TABLE_TITLE_MAX_CHARS (ops_agent/policy.py), which cuts a table's title for the page. The
# ops-mcp package copies only common/ and ops_mcp/ (infra/modules/ops-assistant/main.tf), so it is
# not imported here; a test holds the two equal.
TABLE_TITLE_MAX_CHARS = 120
# The fewest letters of a topic's label a shortened title keeps.
TITLE_LABEL_MIN_CHARS = 12
SPEND_NOTE = "AUD, at the project's fixed rate"
SOURCE = "logs"
EVIDENCE = "evidence"  # the first half of every job's key in logs.run_queries

# The three queries, and the events each may return: a row of any other event is dropped.
QUERY_KINDS = ("decisions", "attempts", "people")
QUERY_EVENTS = {
    "decisions": ("vision_decision", "vision_triage"),
    "attempts": ("research_tick",),
    "people": ("moderation_decision",),
}
# What each query's state is called in the result (`queries`).
COMPLETE, CUT, NOT_CHECKED = "complete", "cut", "not_checked"
# Every tool a triage profile offers, and `other` (the trail's name for a tool the model invented).
TOOL_NAMES = frozenset(
    tool["toolSpec"]["name"] for profile in PROFILES.values() for tool in profile.tools
) | {"other"}
# A moderation decision's codes (common/evidence.py's moderation_decision; the trace's person row
# reads them). A test holds this to vision_decisions' own table.
MODERATION_DECISIONS = ("approved", "rejected", "force_published", "unpublished", "sent_back")

# The fixed words of the tables and the spoken answer, beside vision_decisions.TRACE_WORDS. A test
# holds TOOL_WORDS' keys to TOOL_NAMES, so a new tool gets its words.
TOOL_WORDS = {
    "look_again": "the agent asked OpenCV to measure the scene again at other settings",
    "previous_scene": "the agent looked at the previous scene",
    "site_history": "the agent read the site's history",
    "web_context": "the agent read the news",
    "other": "the agent asked for a tool it does not have",
}
OUTCOME_CELLS = {
    "finding_written": "finding written",
    "no_finding": "no finding",
    "not_kept": "not kept: measured again",
    "finding_lost": "finding lost",
    "finding_would_be_written": "finding would be written",
}
ARTICLE_CELLS = {
    "held": "waiting for review",
    "approved": "approved",
    "rejected": "rejected",
    "unpublished": "taken down",
    "published_unreviewed": "published with no review",
    "no_article_yet": "no article yet",
    "finding_expired": "finding expired",
}
FROM_CELLS = {"imagery": "the imagery", "network": "OpenStreetMap", "imagery_and_network": "both"}
GATE_SENTENCES = {
    "reported_first": "It was the first observation, so it was reported without a baseline.",
    "kept_out_cloud": "Too much of the site was cloud, so it was kept out.",
    "kept_out_baseline": "The baseline is still building, so it was kept out.",
    "kept_out_range": "The numbers stayed within the usual range, so it was kept out.",
}
ACTION_SENTENCES = {
    "finding_written": "A finding was written.",
    "no_finding": "No finding was written.",
    "not_kept": "The tick failed, so the scene is measured again.",
    "finding_lost": "The tick failed before the finding was written.",
    "finding_would_be_written": "A finding would be written.",
}
PERSON_SENTENCES = {
    "held": "Its article is waiting for review.",
    "approved": "Its article was approved.",
    "rejected": "Its article was rejected.",
    "unpublished": "Its article was taken down.",
    "published_unreviewed": "Its article was published with no review.",
    "no_article_yet": "No article has been drafted yet.",
    "finding_expired": "The finding expired before an article was drafted.",
}
NOT_IN_TIME = "The decision logs didn't answer in time. Ask again in a minute."
# The Topics read failed or ran out of time: which topics to read is not known.
TOPICS_NOT_READ = "I couldn't read the topics just now. Ask again in a minute."
PEOPLE_NOT_CHECKED = "What people decided wasn't checked this time."
ON_SCREEN = "The decisions are on screen."
STEPS_ON_SCREEN = "The steps are on screen."
# A topic whose decisions were all left out as unknown (the attempts query cut at its row cap): the
# cut note follows it, so it never says that nothing was measured. With copies known to be not kept
# beside them, it keeps their count (UNKNOWN_KEPT_FAILED).
UNKNOWN_KEPT = "For {label}, I couldn't tell which of its decisions {window} were kept."
UNKNOWN_KEPT_FAILED = (
    "For {label}, I couldn't tell which of its decisions {window} were kept; {failed} came from ticks "
    "that failed, and {were} measured again."
)
UNKNOWN_KEPT_SHORT = "For {label}, I couldn't tell which decisions were kept."
# The topics an all-topics answer that runs long leaves out, by what the table holds for them: only
# those with a row are said to be on screen.
REST_ON_SCREEN = "{topics} {are} on screen."
REST_NO_DECISIONS = "{topics} made no decisions."
REST_UNKNOWN_KEPT = "For {topics}, I couldn't tell which decisions were kept."
# One scene id decided for several sites of a topic (a tick decides every site in one tile).
MANY_SITES = "That scene was decided for {n} sites; the newest one's steps are on screen."
NO_EVIDENCE_TOPIC = (
    "That topic keeps no decision evidence: this covers the topics that measure satellite imagery."
)
DECISION_COLUMNS = [
    "Topic", "Site", "Scene", "Clear", "Measure", "Moved", "From", "Agent", "OpenCV re-measured",
    "Outcome", "Article", "Measured by",
]  # fmt: skip
TRACE_COLUMNS = ["Step", "What happened", "Numbers", "What OpenCV's output did", "Time"]
COUNT_KEYS = (
    "decisions", "first_observation", "below_floor", "baseline_building", "within_range", "triaged",
    "real", "artefact", "failed_closed", "look_again", "web_context", "reported", "findings_written",
    "not_kept", "articles_held", "articles_approved", "articles_rejected",
)  # fmt: skip
# What the join reads of an article beside its lineage (dynamo.ARTICLES_FOR_TOPIC_FIELDS is these and
# `lineage`; a test holds the two together).
ARTICLE_FIELDS = ("article_id", "status", "published_by", "created_at", "published_at")
# The decision codes counted as they are; the agent's verdicts are counted from the trace.
_GATE_CODES = ("first_observation", "below_floor", "baseline_building", "within_range")
_ARTICLE_COUNTS = {"held": "articles_held", "approved": "articles_approved", "rejected": "articles_rejected"}


# --- the queries -----------------------------------------------------------------------------------


def queries(topic_ids, scene_id: str | None = None) -> dict[str, str]:
    """The three queries, as text. The only values put into them are topic ids that matched
    ID_PATTERN and a scene id that matched SCENE_ID, each a quoted string compared by equality:
    neither can carry a quote, a regex or a command, and `rail-sydney` never matches
    `rail-sydney-demo`."""
    ids = list(dict.fromkeys(topic_ids))
    if not ids or any(not isinstance(each, str) or not ID_PATTERN.match(each) for each in ids):
        raise ValueError("not a topic this module queries")
    if scene_id is not None and (not isinstance(scene_id, str) or not SCENE_ID.match(scene_id)):
        raise ValueError("not a scene this module queries")
    if len(ids) == 1:
        topic = f'topic_id = "{ids[0]}"'
    else:
        topic = "topic_id in [" + ", ".join(f'"{each}"' for each in ids) + "]"
    scene = f' and scene_id = "{scene_id}"' if scene_id is not None else ""
    head = f'fields @timestamp, @message\n| filter evidence = "{evidence.VERSION}" and '
    tail = f"\n| sort @timestamp desc | limit {logs.QUERY_LIMIT}"
    return {
        "decisions": f'{head}event in ["vision_decision", "vision_triage"]\n    and {topic}{scene}{tail}',
        "attempts": f'{head}event = "research_tick" and {topic} and decisions > 0{tail}',
        "people": f'{head}event = "moderation_decision" and {topic}{tail}',
    }


def declared_vocabularies() -> dict[str, dict]:
    """{adapter key: its decision_vocabulary()} over the registry, for the adapters that declare a
    non-empty one (rule 5: the opt-in hook, no adapter named here). The topics those adapters run
    are the ones that keep decision evidence."""
    return {
        adapter_key: declared
        for adapter_key, adapter_cls in ADAPTER_REGISTRY.items()
        if isinstance(declared := adapter_cls.decision_vocabulary(), dict) and declared
    }


def moved_codes(vocabularies: dict[str, dict]) -> frozenset[str]:
    """The union of the measures the adapters' `moved` words cover: a `moved` code outside it is
    not one the pipeline writes."""
    return frozenset(code for each in vocabularies.values() for code in (each.get("moved") or {}))


def _lambda_group(key: str, env: str) -> str:
    """A catalogue function's log group in this environment, as log_review names it."""
    return architecture.fill(f"/aws/lambda/{architecture.by_key('function', key).name}", env)


def _days(value) -> tuple[int, bool]:
    """`days` kept between one and EVIDENCE_LOOKBACK_DAYS, DAYS_DEFAULT when unreadable (as
    logs.window treats its hours), and whether it was moved."""
    try:
        days = int(value) if value is not None else DAYS_DEFAULT
    except (TypeError, ValueError):
        return DAYS_DEFAULT, True
    kept = max(1, min(EVIDENCE_LOOKBACK_DAYS, days))
    return kept, kept != days


def _scene(value) -> str | None:
    """A scene id that passed SCENE_ID, LATEST, or None for anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if text.lower() == LATEST:
        return LATEST
    return text if SCENE_ID.match(text) else None


def _keeps_evidence(item, vocabularies: dict[str, dict]) -> bool:
    return (
        isinstance(item, dict)
        and isinstance(item.get("topic_id"), str)
        and bool(ID_PATTERN.match(item["topic_id"]))
        and bool(vocabularies.get(item.get("adapter")))
    )


# --- parse, then check ------------------------------------------------------------------------------


def _log_time(value) -> str | None:
    """A Logs Insights @timestamp ("2026-10-16 03:00:00.000", UTC) as ISO 8601, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for layout in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, layout).replace(tzinfo=UTC).isoformat(timespec="microseconds")
        except ValueError:
            continue
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat(timespec="microseconds")


def _accepted(line: dict, kind: str, asked: set[str], moved_ok: frozenset[str]) -> bool:
    """Whether a parsed line is one this query may use: its event, its topic (equality, re-checked
    here), the ids it is joined by, and every code it carries against the fixed lists."""
    if line.get("event") not in QUERY_EVENTS[kind] or line.get("topic_id") not in asked:
        return False
    event = line["event"]
    joined = event in ("vision_decision", "vision_triage", "research_tick")
    if joined and not isinstance(line.get("run"), str):
        return False  # every attempt binds a run (research_tick_handler): a line without one joins nothing
    if event in ("vision_decision", "vision_triage"):
        if not all(isinstance(line.get(key), str) for key in ("site_id", "scene_id")):
            return False
        if "stop" in line and line["stop"] not in STOPS:
            return False
        if "verdict" in line and line["verdict"] not in VERDICTS:
            return False
    if event == "vision_decision":
        if line.get("code") not in vision_decisions.CODES or not isinstance(line.get("at"), str):
            return False
        if any(code not in moved_ok for code in line.get("moved") or []):
            return False
        coverage = line.get("coverage")
        if coverage is not None and not 0 <= coverage <= 1:
            return False  # a share of the site: anything else is corrupt or forged
    elif event == "vision_triage":
        if any(tool not in TOOL_NAMES for tool in line.get("tools") or []):
            return False
        if any(step.get("tool") not in TOOL_NAMES for step in line.get("steps") or []):
            return False
    elif event == "moderation_decision":
        if line.get("decision") not in MODERATION_DECISIONS or not isinstance(line.get("article_id"), str):
            return False
    return True


def parse_rows(rows, kind: str, asked: set[str], moved_ok: frozenset[str]) -> tuple[list[dict], int]:
    """The rows of one query through evidence.parse and the checks above: (the lines kept, each
    with its log time under `logged`; how many rows were dropped)."""
    kept, dropped = [], 0
    for row in rows or []:
        line = evidence.parse(row.get("@message")) if isinstance(row, dict) else None
        if line is None or not _accepted(line, kind, asked, moved_ok):
            dropped += 1
            continue
        line["logged"] = _log_time(row.get("@timestamp"))
        kept.append(line)
    return kept, dropped


# --- the join ---------------------------------------------------------------------------------------


def _triage_part(triage: dict | None, decision: dict) -> dict | None:
    """The agent's part of an entry, in the ring's shape (vision_decisions._triage_part), from the
    triage line; from the decision line's own verdict and stop when the triage line is missing
    (cut at the row cap). Never a reason or a host: the logs hold neither."""
    if triage is None and not decision.get("triaged"):
        return None
    given = triage or {}
    return {
        "verdict": given.get("verdict", decision.get("verdict")),
        "stop": given.get("stop", decision.get("stop")),
        "model_id": given.get("model_id"),
        "model_calls": given.get("model_calls"),
        "input_tokens": given.get("input_tokens"),
        "output_tokens": given.get("output_tokens"),
        "cost_usd": given.get("cost_usd"),
        "elapsed_ms": given.get("elapsed_ms"),
        "deadline_left_s": given.get("deadline_left_s"),
        "figure": given.get("figure"),
        "steps": list(given.get("steps") or []),
        "steps_total": given.get("steps_total"),
        "budget_refusals": given.get("budget_refusals"),
        "tools": list(given.get("tools") or []),
    }


def _entry(decision: dict, triage: dict | None, attempt: dict | None, kept: bool) -> dict:
    """A ring entry's shape (vision_decisions.record), rebuilt from the lines, so the trace reads it
    as it reads the ring; plus `kept` and the attempt it joined."""
    return {
        "v": vision_decisions.DECISION_VERSION,
        "at": decision["at"],
        "tick": decision.get("tick"),
        "run": decision.get("run"),
        "topic_id": decision["topic_id"],
        "site_id": decision["site_id"],
        "scene_id": decision["scene_id"],
        "captured_at": decision.get("captured_at"),
        "coverage": decision.get("coverage"),
        "measure": decision.get("measure") or {},
        "baseline": decision.get("baseline"),
        "delta": decision.get("delta"),
        "relative": decision.get("relative"),
        "baseline_scenes": decision.get("baseline_scenes"),
        "thresholds": decision.get("thresholds") or {},
        "code": decision["code"],
        "moved": list(decision.get("moved") or []),
        "network_changed": decision.get("network_changed"),
        "triage": _triage_part(triage, decision),
        "reported": bool(decision.get("reported")),
        "measured_by": {
            "backend": decision.get("backend"),
            "build": decision.get("build"),
            "cpu": decision.get("cpu"),
            "worker_request_id": decision.get("worker_request_id"),
            "worker_ms": None,  # only the vision_site line carries it, and it is never read
        },
        "kept": kept,
        "attempt": attempt,
        "logged": decision.get("logged"),
    }


def join(
    decisions: list[dict], triages: list[dict], attempts: list[dict], *, horizon: str | None = None
) -> tuple[list[dict], dict[str, int], dict[str, int]]:
    """Kept, reported and written: a triage joins its decision by (tick, run, site, scene), a
    decision its attempt by (tick, run); an attempt with `stored: false` or no line makes its
    decisions not kept. One entry per (topic, site, scene), the kept copy preferred, else the
    newest; newest first. And, per topic, how many not-kept copies there were (each counted, never
    as a decision). `horizon` is the oldest attempt line read when that query was cut at its row
    cap: a decision logged before it with no attempt is left out, unknown rather than not kept, and
    the third part says, per topic, how many scenes were left out so and not shown otherwise."""
    attempt_by: dict[tuple, dict] = {}
    for attempt in attempts:  # newest first: the first line of an attempt wins
        attempt_by.setdefault((attempt.get("tick"), attempt["run"]), attempt)
    triage_by: dict[tuple, dict] = {}
    for triage in triages:
        key = (triage.get("tick"), triage["run"], triage["site_id"], triage["scene_id"])
        triage_by.setdefault(key, triage)
    shown: dict[tuple, dict] = {}
    not_kept: dict[str, int] = {}
    left_out: set[tuple] = set()
    for decision in decisions:
        attempt = attempt_by.get((decision.get("tick"), decision["run"]))
        if attempt is None and horizon is not None and (decision.get("logged") or "") < horizon:
            left_out.add((decision["topic_id"], decision["site_id"], decision["scene_id"]))
            continue
        kept = bool(attempt and attempt.get("stored") is True)
        triage = triage_by.get(
            (decision.get("tick"), decision["run"], decision["site_id"], decision["scene_id"])
        )
        entry = _entry(decision, triage, attempt, kept)
        if not kept:
            not_kept[decision["topic_id"]] = not_kept.get(decision["topic_id"], 0) + 1
        key = (decision["topic_id"], decision["site_id"], decision["scene_id"])
        current = shown.get(key)
        if current is None or (kept and not current["kept"]):
            shown[key] = entry
    ordered = sorted(shown.values(), key=lambda entry: (entry["at"], entry["logged"] or ""), reverse=True)
    unknown: dict[str, int] = {}
    for topic_id, _site, _scene in left_out - set(shown):
        unknown[topic_id] = unknown.get(topic_id, 0) + 1
    return ordered, not_kept, unknown


def _article_by_captured_at(articles: list[dict]) -> dict[str, dict]:
    """{captured_at: the newest article whose lineage names it}: an article drafted from two
    Findings is the article of both decisions."""
    found: dict[str, dict] = {}
    for article in articles:
        lineage = article.get("lineage") if isinstance(article, dict) else None
        research = lineage.get("research") if isinstance(lineage, dict) else None
        calls = research.get("calls") if isinstance(research, dict) else None
        for call in calls if isinstance(calls, list) else []:
            captured = call.get("captured_at") if isinstance(call, dict) else None
            if isinstance(captured, str) and captured not in found:
                found[captured] = article
    return found


def _article_part(article: dict | None) -> dict | None:
    """What the trace reads of an article: its id, status, who published it and when."""
    if article is None:
        return None
    return {key: article.get(key) if isinstance(article.get(key), str) else None for key in ARTICLE_FIELDS}


# --- one decision, rendered -------------------------------------------------------------------------


def _step_rows(rows: list[dict]) -> dict:
    """The trace's single rows by step (the tool rows are a list under "tool")."""
    by_step: dict = {"tool": []}
    for row in rows:
        if row["step"] == "tool":
            by_step["tool"].append(row)
        else:
            by_step[row["step"]] = row
    return by_step


def _sources_cell(sources: dict) -> str:
    """"From": "the imagery", "OpenStreetMap" or "both", over every moved measure's source."""
    kinds = set(sources.values())
    if not kinds:
        return ""
    if kinds == {"imagery"}:
        return FROM_CELLS["imagery"]
    if kinds == {"network"}:
        return FROM_CELLS["network"]
    return FROM_CELLS["imagery_and_network"]


def _summary(entry: dict, rows: list[dict], article: dict | None) -> dict:
    """The public row of one decision: codes, ids, times and numbers, with what the trace decided
    (the gate's effect, the verdict, the action, the person) read off its rows so every reader
    agrees."""
    by = _step_rows(rows)
    compared, verdict, action, person = by.get("compared"), by.get("verdict"), by["action"], by.get("person")
    triage = entry.get("triage") or {}
    return {
        "topic_id": entry["topic_id"],
        "site_id": entry["site_id"],
        "scene_id": entry["scene_id"],
        "at": entry["at"],
        "captured_at": entry.get("captured_at"),
        "tick": entry.get("tick"),
        "run": entry.get("run"),
        "kept": entry["kept"],
        "code": entry["code"],
        "reported": entry["reported"],
        "triaged": entry.get("triage") is not None,
        "coverage": entry.get("coverage"),
        "measure": dict(entry.get("measure") or {}),
        "baseline": entry.get("baseline"),
        "delta": entry.get("delta"),
        "relative": entry.get("relative"),
        "baseline_scenes": entry.get("baseline_scenes"),
        "thresholds": dict(entry.get("thresholds") or {}),
        "moved": list(entry.get("moved") or []),
        "sources": dict((compared or {}).get("sources") or {}),
        "network_changed": entry.get("network_changed"),
        "effect": (compared or {}).get("effect"),
        "verdict": (verdict or {}).get("code"),
        "stop": triage.get("stop"),
        "model_id": triage.get("model_id"),
        "tools": list(triage.get("tools") or []),
        "look_again": sum(1 for row in by["tool"] if row.get("effect") == "re_measured"),
        "web_context": sum(1 for row in by["tool"] if row.get("code") == "web_context"),
        "cost_aud": (verdict or {}).get("numbers", {}).get("cost_aud"),
        "action": action["code"],
        "finding_at": action.get("time") if action["code"] == "finding_written" else None,
        "person": (person or {}).get("code"),
        "person_at": (person or {}).get("time"),
        "hours_after_finding": (person or {}).get("numbers", {}).get("hours_after_finding"),
        "article_id": (article or {}).get("article_id"),
        "measured_by": dict(entry.get("measured_by") or {}),
    }


def _is_number(value) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _value(value, unit: str, *, signed: bool = False) -> str:
    """A number with its unit, as the vocabulary declares it: one decimal for a percentage or a
    non-whole number, a whole number as it is; with its sign for a change."""
    if not _is_number(value):
        return ""
    sign = "+" if signed else ""
    if unit == "%":
        return f"{value:{sign}.1f}%"
    if isinstance(value, float) and not value.is_integer():
        text = f"{value:{sign}.3g}" if abs(value) < 1 else f"{value:{sign}.1f}"
    else:
        text = f"{int(value):{sign}d}"
    return f"{text} {unit}".strip()


def _measure_word(code: str, value, vocabulary: dict, *, signed: bool = False) -> str:
    """One measure with the vocabulary's label and unit, scaled as it declares: "built-up area
    served 70.0%"; the coverage every measurement carries as "93% clear"."""
    measures = (vocabulary or {}).get("measures") or {}
    item = measures.get(code) if isinstance(measures.get(code), dict) else None
    if item is None:
        if code == "coverage" and _is_number(value):
            return f"{value * 100:.0f}% clear"
        return f"{code.replace('_', ' ')} {_value(value, '', signed=signed)}".strip()
    scaled = value * (item.get("scale") or 1) if _is_number(value) else value
    return f"{item.get('label') or code} {_value(scaled, item.get('unit') or '', signed=signed)}".strip()


def _measure_words(measure: dict, vocabulary: dict, codes=None) -> str:
    """The measures with the vocabulary's words, in its order (only `codes`, when given):
    "built-up area served 70.0%, stations 180"."""
    measures = (vocabulary or {}).get("measures") or {}
    return ", ".join(
        _measure_word(code, measure[code], vocabulary)
        for code in measures
        if (codes is None or code in codes) and _is_number(measure.get(code))
    )


def _moved_words(moved: list, vocabulary: dict) -> str:
    words = (vocabulary or {}).get("moved") or {}
    return _join([words[code] for code in moved if isinstance(words.get(code), str)])


def _setting_words(code: str, value, vocabulary: dict) -> str:
    settings = (vocabulary or {}).get("settings") or {}
    item = settings.get(code) if isinstance(settings.get(code), dict) else {}
    return f"{item.get('label') or code.replace('_', ' ')} {_value(value, item.get('unit') or '')}".strip()


def _re_measured_cell(entry: dict, rows: list[dict], vocabulary: dict) -> str:
    """"OpenCV re-measured": "no", or per look_again "reach 800 m: built-up area served 64.1%
    (70.0% at the topic's settings)"; a look_again that did not re-measure, by its code. Facts
    only: nothing here names the baseline."""
    said = []
    steps = (entry.get("triage") or {}).get("steps") or []
    measures = (vocabulary or {}).get("measures") or {}
    for row in rows:
        if row["step"] != "tool":
            continue
        step = steps[row["n"] - 1] if 0 < row["n"] <= len(steps) else {}
        if step.get("tool") != "look_again":
            continue
        if row.get("effect") != "re_measured":
            said.append(f"did not re-measure ({str(row['code']).replace('_', ' ')})")
            continue
        numbers = row["numbers"]
        settings = [
            _setting_words(key.removeprefix("setting_"), value, vocabulary)
            for key, value in numbers.items()
            if key.startswith("setting_")
        ]
        measured = []
        for key, value in numbers.items():
            if not key.startswith("measured_"):
                continue
            code = key.removeprefix("measured_")
            words = _measure_word(code, value, vocabulary)
            own = numbers.get(f"at_topic_settings_{code}")
            if own is not None:
                item = measures.get(code) if isinstance(measures.get(code), dict) else {}
                own_words = _value(own * (item.get("scale") or 1), item.get("unit") or "")
                words += f" ({own_words} at the topic's settings)"
            measured.append(words)
        said.append(f"{', '.join(settings) or 'other settings'}: {', '.join(measured) or 'no numbers'}")
    return "; ".join(said) or "no"


def _agent_cell(row: dict | None) -> str:
    verdict = (row or {}).get("code")
    if verdict in VERDICTS:
        return verdict
    if verdict == "failed_closed":
        return f"failed closed ({str((row.get('ids') or {}).get('stop') or 'no verdict').replace('_', ' ')})"
    return ""


def _measured_by_cell(who: dict) -> str:
    """"opencv, build 1a2b3c4d5e6f, Neoverse N1 (Graviton2)": on screen only."""
    parts = [who.get("backend")]
    if who.get("build"):
        parts.append(f"build {who['build']}")
    parts.append(who.get("cpu"))
    return ", ".join(part for part in parts if isinstance(part, str) and part)


def decision_row(label: str, summary: dict, entry: dict, rows: list[dict], vocabulary: dict) -> list:
    """One row of the decisions table (DECISION_COLUMNS)."""
    coverage = entry.get("coverage")
    return [
        label,
        summary["site_id"],
        summary["scene_id"],
        f"{coverage * 100:.0f}%" if _is_number(coverage) else "",
        _measure_words(entry.get("measure") or {}, vocabulary),
        _moved_words(summary["moved"], vocabulary),
        _sources_cell(summary["sources"]),
        _agent_cell(_step_rows(rows).get("verdict")),
        _re_measured_cell(entry, rows, vocabulary) if entry.get("triage") is not None else "no",
        OUTCOME_CELLS.get(summary["action"], summary["action"]),
        ARTICLE_CELLS.get(summary["person"], "") if summary["person"] else "",
        _measured_by_cell(entry.get("measured_by") or {}),
    ]


# --- the scene trace --------------------------------------------------------------------------------


def _numbers_words(numbers: dict, vocabulary: dict) -> str:
    """A trace row's numbers with words: the vocabulary's for the measures and settings, fixed ones
    for the rest. The look_again row's are the re-measurement and the same scene at the topic's
    settings, never the baseline (the trace puts no baseline number in a tool row)."""
    measures = (vocabulary or {}).get("measures") or {}
    words = []
    for key, value in numbers.items():
        if key == "coverage":
            words.append(f"{value * 100:.0f}% clear")
        elif key in measures:
            words.append(_measure_word(key, value, vocabulary))
        elif key.startswith("baseline_") and key.removeprefix("baseline_") in measures:
            words.append(f"baseline {_measure_word(key.removeprefix('baseline_'), value, vocabulary)}")
        elif key.startswith("delta_") and key.removeprefix("delta_") in measures:
            code = key.removeprefix("delta_")
            words.append(f"change {_measure_word(code, value, vocabulary, signed=True)}")
        elif key.startswith("relative_"):
            words.append(f"relative change {value * 100:+.1f}%")
        elif key.startswith("threshold_"):
            words.append(f"{key.removeprefix('threshold_').replace('_', ' ')} {value:g}")
        elif key.startswith("setting_"):
            words.append(_setting_words(key.removeprefix("setting_"), value, vocabulary))
        elif key.startswith("measured_"):
            words.append(f"re-measured {_measure_word(key.removeprefix('measured_'), value, vocabulary)}")
        elif key.startswith("at_topic_settings_"):
            code = key.removeprefix("at_topic_settings_")
            words.append(f"at the topic's settings {_measure_word(code, value, vocabulary)}")
        elif key == "baseline_scenes":
            words.append(f"{value:g} earlier clear scene{'' if value == 1 else 's'}")
        elif key == "worker_ms":
            words.append(f"{value:g} ms on the worker")
        elif key in ("ms", "elapsed_ms"):
            words.append(f"{value:g} ms")
        elif key == "cost_aud":
            words.append(f"A${value:.4f} ({SPEND_NOTE})")
        elif key == "deadline_left_s":
            words.append(f"{value:g} s left")
        elif key == "hours_after_finding":
            words.append(f"{value:g} hours after the finding")
        elif key == "figure":
            words.append("with a figure" if value else "no figure")
        else:
            words.append(f"{value:g} {key.replace('_', ' ')}")
    return ", ".join(words)


def _happened(row: dict, entry: dict) -> str:
    """The "What happened" cell: TRACE_WORDS for every step, the tool's words for a tool row."""
    step, code = row["step"], row.get("code")
    words = vision_decisions.TRACE_WORDS
    if step == "measured":
        return f"{words['steps']['measured']}: {_measured_by_cell(row.get('ids') or {})}".rstrip(": ")
    if step == "compared":
        return words["codes"].get(code, str(code))
    if step == "tool":
        steps = (entry.get("triage") or {}).get("steps") or []
        given = steps[row["n"] - 1] if 0 < row["n"] <= len(steps) else {}
        said = TOOL_WORDS.get(given.get("tool"), TOOL_WORDS["other"])
        if not given.get("ok", True):
            said += f", which did not run ({str(code).replace('_', ' ')})"
        return said
    if step == "verdict":
        said = words["verdicts"].get(code, str(code))
        stop = (row.get("ids") or {}).get("stop")
        if code == "failed_closed" and stop:
            said += f" ({str(stop).replace('_', ' ')})"
        return f"{words['steps']['verdict']}: {said}"
    if step == "action":
        return words["actions"].get(code, str(code))
    if step == "person":
        return words["persons"].get(code, str(code))
    return str(code)


def _did(row: dict, vocabulary: dict) -> str:
    """The "What OpenCV's output did" cell: the effect's fixed words, and for the compared row each
    moved measure's source. Facts of the code only."""
    effect = row.get("effect")
    said = vision_decisions.TRACE_WORDS["effects"].get(effect, "") if effect else ""
    sources = row.get("sources") or {}
    if sources:
        moved = (vocabulary or {}).get("moved") or {}
        parts = [
            f"{moved.get(code, code)} {vision_decisions.TRACE_WORDS['sources'].get(source, source)}"
            for code, source in sources.items()
        ]
        said = f"{said}: {'; '.join(parts)}" if said else "; ".join(parts)
    return said


def trace_rows(rows: list[dict], entry: dict, vocabulary: dict) -> list[list]:
    """The scene trace table's rows (TRACE_COLUMNS), one per trace row."""
    table = []
    for row in rows:
        step = vision_decisions.TRACE_WORDS["steps"].get(row["step"], row["step"])
        if row["step"] == "tool":
            step = f"{step} ({row['n']})"
        table.append([
            step,
            _happened(row, entry),
            _numbers_words(row.get("numbers") or {}, vocabulary),
            _did(row, vocabulary),
            row.get("time") or "",
        ])  # fmt: skip
    return table


# --- spoken -----------------------------------------------------------------------------------------


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _was(n: int) -> str:
    return "was" if n == 1 else "were"


def _is(n: int) -> str:
    return "is" if n == 1 else "are"


def _sentence(text: str) -> str:
    """A sentence that starts with a topic's label starts with a capital ("a topic" included)."""
    return text[:1].upper() + text[1:]


def _words(sentences: list[str]) -> str:
    return " ".join(sentence for sentence in sentences if sentence)


def _fits(text: str) -> bool:
    return len(text.split()) < SPOKEN_MAX_WORDS


def _first_that_fits(candidates: list[list[str]]) -> str:
    """The first candidate under SPOKEN_MAX_WORDS, else the last (the shortest form)."""
    for sentences in candidates:
        text = _words(sentences)
        if _fits(text):
            return text
    return _words(candidates[-1])


def _shows_rows(counts: dict) -> bool:
    """Whether the table has a row for the topic: a kept decision, or a scene not kept."""
    return bool(counts["decisions"] or counts["not_kept"])


def _main_sentence(label: str, counts: dict, days: int) -> str:
    """The "None" or "Some" template of one topic. Of the gate's counts only those above zero are
    said; the agent's always is, as it is what the question is about. With none kept and some left
    out as unknown (the attempts query cut), UNKNOWN_KEPT: never "no new clear scene was measured";
    with copies known to be not kept beside them, UNKNOWN_KEPT_FAILED keeps their count."""
    n, window = counts["decisions"], f"in the last {_plural(days, 'day')}"
    if n == 0 and counts["unknown"] and counts["not_kept"]:
        failed = counts["not_kept"]
        return UNKNOWN_KEPT_FAILED.format(label=label, window=window, failed=failed, were=_was(failed))
    if n == 0 and counts["unknown"]:
        return UNKNOWN_KEPT.format(label=label, window=window)
    if n == 0 and counts["not_kept"]:
        failed = counts["not_kept"]
        return _sentence(
            f"{label} kept no decisions {window}: {_plural(failed, 'decision')} came from ticks that failed, "
            f"and {_was(failed)} measured again."
        )
    if n == 0:
        return _sentence(f"{label} made no decisions {window}: no new clear scene was measured.")
    parts = [
        f"{_plural(counts['first_observation'], 'first observation')}" if counts["first_observation"] else "",
        f"{counts['below_floor']} too cloudy to count" if counts["below_floor"] else "",
        f"{counts['baseline_building']} still building a baseline" if counts["baseline_building"] else "",
        f"{counts['within_range']} within the usual range" if counts["within_range"] else "",
        f"{_plural(counts['untriaged'], 'change')} reported with triage off" if counts["untriaged"] else "",
    ]
    parts = [part for part in parts if part]
    agent = f"{_plural(counts['triaged'], 'change')} checked by the agent"
    if len(parts) > 1:
        listed = f"{', '.join(parts)}, and {agent}"
    else:
        listed = " and ".join([*parts, agent])
    return _sentence(f"{label} made {_plural(n, 'decision')} {window}: {listed}.")


def _agent_sentence(counts: dict) -> str:
    if not counts["triaged"]:
        return ""
    judged = f"The agent judged {counts['real']} real and {_plural(counts['artefact'], 'artefact')}"
    if counts["failed_closed"]:
        judged += f", and failed closed {_plural(counts['failed_closed'], 'time')}"
    return (
        f"{judged}; it re-measured with OpenCV {_times(counts['look_again'])} and read the news "
        f"{_times(counts['web_context'])}."
    )


def _findings_sentence(counts: dict) -> str:
    """The findings written and what became of their articles; only the findings when the topic's
    Articles read was skipped or failed (its article counts are None: not checked, never zero)."""
    if not counts["decisions"]:
        return ""
    written, held = counts["findings_written"], counts["articles_held"]
    approved, rejected = counts["articles_approved"], counts["articles_rejected"]
    if held is None or approved is None or rejected is None:
        return f"{_plural(written, 'finding')} {_was(written)} written."
    return (
        f"{_plural(written, 'finding')} {_was(written)} written; {_plural(held, 'article')} {_is(held)} "
        f"waiting for review, {approved} {_was(approved)} approved and {rejected} rejected."
    )


def _not_kept_sentence(counts: dict) -> str:
    if not counts["decisions"] or not counts["not_kept"]:
        return ""
    failed = counts["not_kept"]
    return f"{failed} more came from ticks that failed, and {_was(failed)} measured again."


def _short_sentence(label: str, counts: dict, lead: str = "") -> str:
    """One topic in a few words, for an answer that would otherwise run long; `lead` says the
    window before the first."""
    n = counts["decisions"]
    if n == 0 and counts["unknown"]:
        said = UNKNOWN_KEPT_SHORT.format(label=label)
        return f"{lead}{said[:1].lower()}{said[1:]}" if lead else said
    if n == 0:
        return _sentence(f"{lead}{label} made no decisions.")
    return _sentence(
        f"{lead}{label} made {_plural(n, 'decision')}: {_plural(counts['triaged'], 'change')} checked by the "
        f"agent, {_plural(counts['findings_written'], 'finding')} written."
    )


def _topic_spoken(took: str, label: str, counts: dict, days: int, notes: list[str]) -> str:
    """One topic: the templates of 1.17.4 in order, the details dropped (the not-kept sentence,
    then the agent's, then the findings') until the answer is under SPOKEN_MAX_WORDS. The decisions
    are said to be on screen only when the table has a row."""
    main = _main_sentence(label, counts, days)
    agent, findings = _agent_sentence(counts), _findings_sentence(counts)
    not_kept = _not_kept_sentence(counts)
    on_screen = ON_SCREEN if _shows_rows(counts) else ""
    closing = [*notes, on_screen]
    return _first_that_fits([
        [took, main, agent, findings, not_kept, *closing],
        [took, main, agent, findings, *closing],
        [took, main, findings, *closing],
        [took, main, *closing],
        [took, _short_sentence(label, counts), *closing],
        [_short_sentence(label, counts), on_screen],
    ])  # fmt: skip


def _rest_sentences(rest: list[dict]) -> list[str]:
    """The topics an answer that runs long leaves out, by what the table holds for them: those with
    a row are on screen; of the others, those with decisions left out as unknown are said as such,
    and the rest made no decisions. Never "on screen" for a topic with no row."""
    shown = sum(1 for counts in rest if _shows_rows(counts))
    unknown = sum(1 for counts in rest if not _shows_rows(counts) and counts["unknown"])
    none = len(rest) - shown - unknown
    return [
        REST_ON_SCREEN.format(topics=_plural(shown, "more topic"), are=_is(shown)) if shown else "",
        REST_NO_DECISIONS.format(topics=_plural(none, "more topic")) if none else "",
        REST_UNKNOWN_KEPT.format(topics=_plural(unknown, "more topic")) if unknown else "",
    ]


def _all_topics_spoken(labels: list[str], per_topic: list[dict], days: int, notes: list[str]) -> str:
    """Every topic: one "None" or "Some" sentence each, never summed; when that runs long, a short
    sentence each, and then as many as fit with the rest said for what they are (_rest_sentences).
    The decisions are said to be on screen only when the table has a row."""
    closing = [*notes, ON_SCREEN if any(_shows_rows(counts) for counts in per_topic) else ""]
    full = [_main_sentence(label, counts, days) for label, counts in zip(labels, per_topic, strict=True)]
    lead = f"In the last {_plural(days, 'day')}, "
    short = [
        _short_sentence(label, counts, lead if n == 0 else "")
        for n, (label, counts) in enumerate(zip(labels, per_topic, strict=True))
    ]
    candidates = [[*full, *closing], [*short, *closing]]
    for kept in range(len(short) - 1, 0, -1):
        candidates.append([*short[:kept], *_rest_sentences(per_topic[kept:]), *closing])
    return _first_that_fits(candidates)


def _plausible(code: str, value, vocabulary: dict) -> bool:
    """Whether a measure may be said aloud: a number whose value, scaled as the vocabulary declares,
    is below SPOKEN_MEASURE_MAX."""
    if not _is_number(value):
        return False
    item = ((vocabulary or {}).get("measures") or {}).get(code)
    scaled = value * ((item.get("scale") if isinstance(item, dict) else None) or 1)
    return math.isfinite(scaled) and abs(scaled) < SPOKEN_MEASURE_MAX


def _scene_spoken(
    took: str,
    label: str,
    summary: dict,
    entry: dict,
    vocabulary: dict,
    notes: list[str],
    closing: str = STEPS_ON_SCREEN,
) -> str:
    """One decision's chain in fixed sentences: facts only, nothing about the baseline after the
    re-measurement, never a site or scene id. OpenCV's measure is said for the vocabulary's first
    measure and every measure that moved, each only when it is plausible (SPOKEN_MEASURE_MAX)."""
    coverage = entry.get("coverage")
    clear = f"{coverage * 100:.0f}% clear" if _is_number(coverage) else "of unknown cloud cover"
    opening = f"That {label} scene was {clear}."
    measures = list((vocabulary or {}).get("measures") or {})
    measure = entry.get("measure") or {}
    plausible = {code: value for code, value in measure.items() if _plausible(code, value, vocabulary)}
    said = _measure_words(plausible, vocabulary, codes={*measures[:1], *summary["moved"]})
    measured = f"OpenCV measured {said}." if said else "OpenCV measured it."
    gate = ""
    effect = summary.get("effect")
    if effect in GATE_SENTENCES:
        gate = GATE_SENTENCES[effect]
    elif effect in ("sent_to_agent", "reported_untriaged"):
        moved = _moved_words(summary["moved"], vocabulary) or "a measure"
        kinds = set(summary["sources"].values())
        source = next(iter(kinds)) if len(kinds) == 1 else "imagery_and_network"
        origin = vision_decisions.TRACE_WORDS["sources"][source] if kinds else ""
        past = "the topic's threshold" if len(summary["moved"]) <= 1 else "the topic's thresholds"
        where = f", {origin}," if origin else ""
        if effect == "sent_to_agent":
            gate = _sentence(f"{moved} moved past {past}{where} so the agent was asked.")
        else:
            gate = _sentence(
                f"{moved} moved past {past}{where} and triage is off for this topic, so it was reported."
            )
    agent = ""
    verdict = summary.get("verdict")
    if verdict == "failed_closed":
        stop = str(summary.get("stop") or "no verdict").replace("_", " ")
        agent = f"The agent failed closed ({stop}), so no verdict was given."
    elif verdict in VERDICTS:
        used = []
        if summary["look_again"]:
            used.append(f"re-measured the scene with OpenCV {_times(summary['look_again'])}")
        if summary["web_context"]:
            used.append(f"read the news {_times(summary['web_context'])}")
        judged = vision_decisions.TRACE_WORDS["verdicts"][verdict]
        agent = f"It {', '.join(used)}, and judged the change {judged}." if used else (
            f"It judged the change {judged} without re-measuring or reading the news."
        )
    action = ACTION_SENTENCES.get(summary["action"], "")
    person = PERSON_SENTENCES.get(summary.get("person"), "")
    return _first_that_fits([
        [took, opening, measured, gate, agent, action, person, *notes, closing],
        [took, opening, gate, agent, action, person, *notes, closing],
        [took, opening, gate, action, closing],
    ])  # fmt: skip


# --- the counts ---------------------------------------------------------------------------------------


def _empty_counts() -> dict:
    """COUNT_KEYS, and `untriaged` (a change reported with triage off) and `unknown` (the scenes
    left out as unknown, join's third part), which only the spoken sentences use."""
    return {key: 0 for key in COUNT_KEYS} | {"untriaged": 0, "unknown": 0}


def _count(counts: dict, summary: dict, articles_seen: set, findings_seen: set) -> None:
    """One kept decision's part of its topic's counts: its gate code, the agent's verdict and tools,
    whether it was reported, and its Finding and its article, each once. One tick writes one Finding
    for every site it reports (each decision's `at` is the tick's, the Finding's captured_at), so
    Findings are counted by that key, not by decision; an article can hold several Findings."""
    counts["decisions"] += 1
    if summary["code"] in _GATE_CODES:
        counts[summary["code"]] += 1
    if summary["triaged"]:
        counts["triaged"] += 1
        if summary["verdict"] in ("real", "artefact", "failed_closed"):
            counts[summary["verdict"]] += 1
        counts["look_again"] += summary["look_again"]
        counts["web_context"] += summary["web_context"]
    elif summary["code"] == "reported":
        counts["untriaged"] += 1
    if summary["reported"]:
        counts["reported"] += 1
    if summary["action"] == "finding_written" and summary["finding_at"] not in findings_seen:
        findings_seen.add(summary["finding_at"])
        counts["findings_written"] += 1
    key = summary["article_id"] or (summary["topic_id"], summary["at"])
    if summary["person"] in _ARTICLE_COUNTS and key not in articles_seen:
        articles_seen.add(key)
        counts[_ARTICLE_COUNTS[summary["person"]]] += 1


def _spend_aud(triages: list[dict]) -> float | None:
    """What the agent cost over these triage lines, in AUD at the project's fixed rate: 0.0 with no
    triage, None when none was priced (the model's price unknown), else the priced ones' sum."""
    if not triages:
        return 0.0
    priced = [line["cost_usd"] for line in triages if _is_number(line.get("cost_usd"))]
    return round(sum(priced) * USD_TO_AUD_RATE, 4) if priced else None


# --- every AWS call, held to the call's limit -----------------------------------------------------


class OutOfTime(BotoCoreError):
    """A request this call did not make: its time had run out. A botocore error, so each step takes
    it as it takes a request that failed (logs.check_group refuses the group, logs.run_queries says
    the query did not finish or skips the stop)."""

    fmt = "The call's time ran out before this request was made."


def _config(read_timeout: float) -> Config:
    """One request's client, as vision_status._s3 builds its own: a short connect, `read_timeout`
    (the time left; at least MIN_READ_SECONDS) and one attempt in all. The shared clients keep
    longer bounds (logs_insights._LOGS_CONFIG: 3 s, 8 s and two attempts; common/dynamo's resource:
    botocore's 60 s each way and ten attempts), which no limit here could hold."""
    return Config(
        connect_timeout=CONNECT_SECONDS,
        read_timeout=max(read_timeout, MIN_READ_SECONDS),
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


def _articles_table(read_timeout: float):
    """The Articles table for one page of one read."""
    return boto3.resource("dynamodb", config=_config(read_timeout)).Table(os.environ["ARTICLES_TABLE"])


def _topics_table(read_timeout: float):
    """The Topics table for one request of the Topics read (a GetItem, or one page of the Scan)."""
    return boto3.resource("dynamodb", config=_config(read_timeout)).Table(os.environ["TOPICS_TABLE"])


class _Bounded:
    """A table as this call reads it, through dynamo's own functions (`table=`): each request (a
    GetItem, or one page of a Query or a Scan) goes through a table `build` makes for it with the
    time left before `deadline` on `clock` as its read timeout, and a request that would start at or
    after the deadline is not made (OutOfTime). A read cut between pages so fails whole, never a
    partial count. Only reads are passed on."""

    READS = frozenset({"get_item", "query", "scan"})

    def __init__(self, build, clock, deadline: float):
        self._build, self._clock, self._deadline = build, clock, deadline

    def __getattr__(self, name: str):
        if name not in _Bounded.READS:
            raise AttributeError(name)
        left = self._deadline - self._clock()
        if left <= 0:
            raise OutOfTime()
        return getattr(self._build(left), name)


class _Budget:
    """The time a call has left before CALL_LIMIT_SECONDS from its start, on `clock`, and the AWS
    clients that hold each of its requests to it: each built for one request, or OutOfTime when no
    time is left. `logs` and `sts` are what logs.readable and logs.run_queries ask with, `sleep`
    their pause between polls, and `topics` the Topics table."""

    def __init__(self, clock, started: float):
        self.clock, self.limit = clock, started + CALL_LIMIT_SECONDS

    def left(self) -> float:
        left = self.limit - self.clock()
        if left <= 0:
            raise OutOfTime()
        return left

    def logs(self, region: str):
        return boto3.client("logs", region_name=region, config=_config(self.left()))

    def sts(self):
        return boto3.client("sts", config=_config(self.left()))

    def sleep(self, seconds: float) -> None:
        time.sleep(max(0.0, min(seconds, self.limit - self.clock())))

    def topics(self) -> _Bounded:
        return _Bounded(_topics_table, self.clock, self.limit)


def _read_articles(topic_id: str, since: str, *, clock, deadline: float) -> list[dict]:
    """A topic's articles created since `since` (dynamo.list_articles_for_topic), every page of the
    read given the time left before `deadline` on `clock` (_Bounded)."""
    return list_articles_for_topic(topic_id, since, table=_Bounded(_articles_table, clock, deadline))


# --- the tool ---------------------------------------------------------------------------------------


def _refusal(spoken: str, matched: dict | None = None) -> dict:
    result = {"spoken": spoken, "findings": []}
    if matched is not None:
        result["matched_topic"] = matched
    return result


def _table(title: str, columns: list[str], rows: list) -> dict:
    return {"title": title, "columns": list(columns), "rows": rows}


def _title(label: str, tail: str) -> str:
    """A table's title within TABLE_TITLE_MAX_CHARS that keeps `tail` whole (the spend and its AUD
    label, or a scene's ids): the topic's label is shortened first, to no fewer than
    TITLE_LABEL_MIN_CHARS letters and an ellipsis; only a tail too long even for that is cut."""
    if len(label) + len(tail) <= TABLE_TITLE_MAX_CHARS:
        return label + tail
    room = max(TABLE_TITLE_MAX_CHARS - len(tail) - 1, TITLE_LABEL_MIN_CHARS)
    head = label if len(label) <= room else label[:room].rstrip() + "…"
    return (head + tail)[:TABLE_TITLE_MAX_CHARS]


def vision_evidence(
    topic: str | None = None,
    days: int = DAYS_DEFAULT,
    scene: str | None = None,
    *,
    now: datetime | None = None,
    run=None,
    readable=None,
    articles=None,
    clock=None,
) -> dict:
    """What the vision pipeline measured and decided and what a person did about it, from the
    logs, over the last `days` (1 to EVIDENCE_LOOKBACK_DAYS): for `topic` (as the operator said
    it), or every topic that keeps decision evidence; with `scene` (a scene id, or "latest") one
    decision's chain step by step. Every AWS call it makes is held to CALL_LIMIT_SECONDS from its
    start (_Budget). `run`, `readable`, `articles` and `clock` are the tests' seams for
    logs.run_queries, logs.readable, the bounded Articles read (_read_articles) and
    time.monotonic."""
    clock = clock or time.monotonic
    started = clock()
    budget = _Budget(clock, started)
    now = (now or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
    env = architecture.environment()
    if env is None:
        return _refusal("I don't know which environment I'm in, so I won't read any logs.")
    days, clamped = _days(days)
    asked_scene = _scene(scene) if scene is not None else None
    if scene is not None and asked_scene is None:
        return _refusal("That isn't a scene id I can look up.")
    if asked_scene is not None and topic is None:
        return _refusal("Say which topic the scene belongs to, and I'll walk through it.")
    vocabularies = declared_vocabularies()
    matched = None
    try:  # the Topics read, held to the call's limit: not read is said, never raised
        if topic is not None:
            # As the operator said it; named with spoken_label throughout, since a voice caller gets
            # the whole result (topic_match.pick_spoken, the vision round's way of naming topics).
            one, matched, refusal = topic_match.pick_spoken(topic, table=budget.topics())
        else:
            listed = list_topics(table=budget.topics())
    except (BotoCoreError, ClientError) as exc:  # OutOfTime among them
        print(f"ops_vision_evidence: topics read failed error={type(exc).__name__}")
        return _refusal(TOPICS_NOT_READ)
    if topic is not None:
        if one is None:
            return _refusal(refusal, matched)
        if not isinstance(one.get("topic_id"), str) or not ID_PATTERN.match(one["topic_id"]):
            return _refusal("That topic's id isn't one I can look up.", matched)
        if not vocabularies.get(one.get("adapter")):
            return _refusal(topic_match.took(matched) + NO_EVIDENCE_TOPIC, matched)
        items = [one]
    else:
        items = sorted(
            (item for item in listed if _keeps_evidence(item, vocabularies)),
            key=lambda item: item["topic_id"],
        )
    took = topic_match.took(matched).strip() if matched is not None else ""
    one_topic = topic is not None
    since, until = now - timedelta(days=days), now
    window = {
        "since": since.isoformat(), "until": until.isoformat(), "days": days, "clamped": clamped,
        "source": SOURCE,
    }  # fmt: skip
    labels = {item["topic_id"]: spoken_label(item) for item in items}
    result = {
        "spoken": "",
        "findings": [],
        "environment": env,
        "window": {**window, "complete": False},
        "topics": [{"topic_id": i, "label": labels[i], "decisions": 0} for i in labels],
        "counts": _public_counts(_empty_counts()) if one_topic else None,
        "rows": [],
        "table": _table(f"Vision decisions in {env}", DECISION_COLUMNS, []),
        "dropped": 0,
        "spend_aud": None,
        "spend_note": SPEND_NOTE,
        "queries": {},
        "as_of": now.isoformat(),
    }
    if matched is not None:
        result["matched_topic"] = matched
    if not items:
        result["spoken"] = (
            f"No topic in {env} keeps decision evidence: this covers the topics that measure satellite "
            "imagery."
        )
        result["window"]["complete"] = True
        return result

    # The two log groups, named from the catalogue, through the access check (logs.py), whose tags
    # and account id are asked for within the call's limit.
    region = logs.home_region()
    tick_group, admin_group = _lambda_group(TICK_FUNCTION, env), _lambda_group(ADMIN_FUNCTION, env)
    if readable is None:
        readable = lambda names, where: logs.readable(  # noqa: E731
            names, region=where, clients=budget.logs, sts=budget.sts
        )
    allowed, refused = readable([tick_group, admin_group], region)
    if tick_group not in allowed:
        why = next((each["why"] for each in refused if each.get("log_group") == tick_group), None)
        return _refusal(
            f"I'm not allowed to read the research tick's log: {why or 'no log group to read.'}", matched
        )
    ids = [item["topic_id"] for item in items]
    text = queries(ids, asked_scene if asked_scene not in (None, LATEST) else None)
    jobs = [
        ((EVIDENCE, "decisions"), region, tick_group, text["decisions"], since, until),
        ((EVIDENCE, "attempts"), region, tick_group, text["attempts"], since, until),
    ]
    if admin_group in allowed:
        jobs.append(((EVIDENCE, "people"), region, admin_group, text["people"], since, until))
    if run is None:  # started, polled and stopped within the call's limit
        run = partial(
            logs.run_queries, client=budget.logs, wait_seconds=WAIT_SECONDS, sleep=budget.sleep,
            label="ops_vision_evidence", recover_throttles=True,
        )  # fmt: skip
    results = run(jobs)

    # Complete or not; then parse, then check.
    asked, moved_ok = set(ids), moved_codes(vocabularies)
    status, parsed = {}, {}
    for kind in QUERY_KINDS:
        rows = results.get((EVIDENCE, kind)) if any(job[0] == (EVIDENCE, kind) for job in jobs) else None
        if rows is None:
            status[kind], parsed[kind] = NOT_CHECKED, []
            continue
        status[kind] = CUT if len(rows) >= logs.QUERY_LIMIT else COMPLETE
        parsed[kind], lost = parse_rows(rows, kind, asked, moved_ok)
        result["dropped"] += lost
    result["queries"] = status
    result["window"]["complete"] = all(state == COMPLETE for state in status.values())
    if status["decisions"] == NOT_CHECKED or status["attempts"] == NOT_CHECKED:
        # Nothing can be counted without both: not checked is never "none".
        result["spoken"] = _words([took, NOT_IN_TIME])
        result["counts"] = None
        for each in result["topics"]:
            each["decisions"] = None
        return result

    # The join, then the person's part and one trace per decision.
    decisions = [line for line in parsed["decisions"] if line["event"] == "vision_decision"]
    triages = [line for line in parsed["decisions"] if line["event"] == "vision_triage"]
    horizon = None
    if status["attempts"] == CUT:
        horizon = min((line["logged"] for line in parsed["attempts"] if line.get("logged")), default=None)
    entries, not_kept, unknown = join(decisions, triages, parsed["attempts"], horizon=horizon)
    people_by_article: dict[str, dict] = {}
    for line in parsed["people"]:  # newest first: an article's newest decision
        people_by_article.setdefault(line["article_id"], line)
    vocabulary_of = {item["topic_id"]: vocabularies[item["adapter"]] for item in items}
    if articles is None:  # every page is given the time left before the deadline (_Bounded)
        articles = partial(_read_articles, clock=clock, deadline=started + DEADLINE_SECONDS)
    article_by_at: dict[str, dict[str, dict] | None] = {}
    rendered = []
    for entry in entries:
        topic_id, vocabulary = entry["topic_id"], vocabulary_of[entry["topic_id"]]
        article = person = None
        unread = False
        first = vision_decisions.trace(entry, vocabulary=vocabulary, attempt=entry["attempt"], now=now)
        if _step_rows(first)["action"]["code"] == "finding_written":
            if topic_id not in article_by_at and clock() - started >= DEADLINE_SECONDS:
                # Too late to start another read: not checked, as a failed read is.
                print("ops_vision_evidence: articles read skipped, past the deadline")
                article_by_at[topic_id] = None
            if topic_id not in article_by_at:
                try:
                    article_by_at[topic_id] = _article_by_captured_at(articles(topic_id, since.isoformat()))
                except Exception as exc:  # noqa: BLE001 - the decisions are still worth saying
                    print(f"ops_vision_evidence: articles read failed error={type(exc).__name__}")
                    article_by_at[topic_id] = None
            found = article_by_at[topic_id]
            unread = found is None
            article = _article_part((found or {}).get(entry["at"]))
            decided = people_by_article.get((article or {}).get("article_id") or "")
            if decided is not None:
                person = {"decision": decided["decision"], "at": decided.get("logged")}
        rows = vision_decisions.trace(
            entry, vocabulary=vocabulary, attempt=entry["attempt"], article=article, person=person, now=now
        )
        if unread:  # not read is never "no article yet": the person's step is left out
            rows = [row for row in rows if row["step"] != "person"]
        rendered.append((entry, rows, _summary(entry, rows, article)))

    # The counts, per topic: never one topic's into another's.
    per_topic = {topic_id: _empty_counts() for topic_id in ids}
    seen: dict[str, tuple[set, set]] = {topic_id: (set(), set()) for topic_id in ids}
    for entry, _rows, summary in rendered:
        if entry["kept"]:
            _count(per_topic[entry["topic_id"]], summary, *seen[entry["topic_id"]])
    for topic_id, copies in not_kept.items():
        per_topic[topic_id]["not_kept"] += copies
    for topic_id, scenes in unknown.items():
        per_topic[topic_id]["unknown"] += scenes
    for topic_id, found in article_by_at.items():
        if found is None:  # its Articles read was skipped or failed: not checked, never zero
            per_topic[topic_id].update(dict.fromkeys(_ARTICLE_COUNTS.values()))
    spend = {topic_id: _spend_aud([t for t in triages if t["topic_id"] == topic_id]) for topic_id in ids}
    result["topics"] = [
        {
            "topic_id": topic_id,
            "label": labels[topic_id],
            "decisions": per_topic[topic_id]["decisions"],
            "counts": _public_counts(per_topic[topic_id]),
            "spend_aud": spend[topic_id],
        }
        for topic_id in ids
    ]
    if one_topic:
        result["counts"] = _public_counts(per_topic[ids[0]])
        result["spend_aud"] = spend[ids[0]]
    notes = []
    if CUT in status.values():
        notes.append(f"Only the newest {logs.QUERY_LIMIT} log lines were read.")
    if status["people"] == NOT_CHECKED or None in article_by_at.values():
        notes.append(PEOPLE_NOT_CHECKED)

    if asked_scene is not None:
        label = labels[ids[0]]
        chosen = next((r for r in rendered if asked_scene == LATEST or r[0]["scene_id"] == asked_scene), None)
        if chosen is None:
            none = f"I found no decision for that {label} scene in the last {_plural(days, 'day')}."
            result["spoken"] = _words([took, none, *notes])
            result["table"] = _table(_title(label, ": one decision, step by step"), TRACE_COLUMNS, [])
            return result
        entry, rows, summary = chosen
        vocabulary = vocabulary_of[entry["topic_id"]]
        # One scene id, several sites (a tick decides every site in one tile): the newest is shown,
        # and the others' site ids are kept for the page.
        others = [
            other["site_id"]
            for other, _rows, _summary in rendered
            if other is not entry and other["scene_id"] == entry["scene_id"]
        ]
        result["rows"] = [{**row, "numbers": dict(row["numbers"]), "ids": dict(row["ids"])} for row in rows]
        result["decision"] = summary
        result["other_sites"] = others
        result["table"] = _table(
            _title(label, f": scene {entry['scene_id']} at {entry['site_id']}, step by step"),
            TRACE_COLUMNS,
            trace_rows(rows, entry, vocabulary),
        )
        closing = MANY_SITES.format(n=len(others) + 1) if others else STEPS_ON_SCREEN
        result["spoken"] = _scene_spoken(took, label, summary, entry, vocabulary, notes, closing)
        return result

    result["rows"] = [summary for _entry, _rows, summary in rendered]
    title = f"Vision decisions in {env}, the last {_plural(days, 'day')}"
    if one_topic:
        tail = f", the last {_plural(days, 'day')}"
        if spend[ids[0]] is not None:
            tail += f": the agent cost A${spend[ids[0]]:.4f} ({SPEND_NOTE})"
        title = _title(labels[ids[0]], tail)
    result["table"] = _table(
        title,
        DECISION_COLUMNS,
        [
            decision_row(labels[entry["topic_id"]], summary, entry, rows, vocabulary_of[entry["topic_id"]])
            for entry, rows, summary in rendered
        ],
    )
    if one_topic:
        result["spoken"] = _topic_spoken(took, labels[ids[0]], per_topic[ids[0]], days, notes)
    else:
        result["spoken"] = _all_topics_spoken(
            [labels[i] for i in ids], [per_topic[i] for i in ids], days, notes
        )
    return result


def _public_counts(counts: dict) -> dict:
    """The counts as the result gives them: COUNT_KEYS, in that order."""
    return {key: counts[key] for key in COUNT_KEYS}
