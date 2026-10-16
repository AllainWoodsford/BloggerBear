"""The day-subject round: one topic that rotates through the subjects it is configured to watch.

docs/enhancements/dailysubject_hook.md (PR S1). The crypto feed varies its subject day by day
because its day goal is a pure function of the date (common/editorial_goals.py). This is that idea
for any adapter, through two optional hooks (common/adapters/base.py):

- `Adapter.subjects(new_state)`: which subjects an observation reports. The research tick keeps
  them on the Finding as `subjects` (`clean_subjects` below), only when there are any.
- `Adapter.day_subject(topic, at)`: the subject a topic writes about at the daily run's start, as a
  `DaySubject` (key, label, mandate, style), or None.

The daily cycle asks for the day's subject once, at the start of its run (`resolve_day_subject`),
and then: reads the window from the subject's own previous article
(common/findings_window.window_start with `subject_key`), keeps only the Findings that report the
subject (`subject_findings`), stops before any model call if none do (`no_findings`, nothing
stamped), folds the subject's mandate and style into its prompts, stores the subject's key on the
article (`subject`), and stamps the topic's `last_article_by_subject` for it. Re-Write reads the
article's subject's Findings back the same way.

An adapter that returns None from the second (every adapter until one opts in) leaves its topics
exactly as they were: the same window, the same prompts, the same Findings and articles.

**A rota** (`parse_rotation`, `rotation_subject`) is offered to any adapter that wants a fixed one,
read from two keys of its adapter_config: `rotation`, either the seven weekdays each naming a
subject ({"MON": "sydney", ...}) or a list of subjects cycled one a day
((date - 1970-01-01).days mod its length), every subject at least once; and `rotation_timezone`,
the zone whose calendar day decides (an IANA name; default the adapter's choice, then UTC), so
"Tuesday" is Tuesday where the subjects are. The adapter parses it with its config and names the
subject from it in `day_subject`. An adapter module may import this one at its top, for the rota,
so this module imports the adapter registry (which imports every adapter module) only inside
`resolve_day_subject`: at the top it would be an import cycle that every pipeline Lambda fails on.

Topic-agnostic: what a subject is, whose day it is and the words for it are the adapter's. Nothing
here names an adapter or a domain (docs/project-plan.md §2 rule 5).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from common.adapters.base import SUBJECT_KEY, DaySubject
from common.editorial_resolver import MAX_GOAL_TEXT_CHARS
from common.figures import plain_text
from common.scheduler import validate_timezone

# The Finding's field (research_tick_handler.py, common/dynamo.put_finding).
SUBJECTS_FIELD = "subjects"
# How many subjects one Finding keeps. A tick that measures one site reports one; the rail adapter
# measures at most a few per tick. Ten keys of at most 64 characters are under 1 KB of a Findings
# item's 400 KB.
MAX_SUBJECTS_PER_FINDING = 10
# How many values `clean_subjects` looks at, kept or not: far past any real hook (the rail adapter
# reports at most common/adapters/rail_access.MAX_RAIL_SITES), so a hook that yields duplicates or
# junk without end costs at most this many, never the research tick's timeout and its Finding. A
# set is sorted before it is read, so one longer than this is refused before the sort.
MAX_SUBJECT_VALUES_EXAMINED = 1_000
# The article's field: the key of the subject it was written about (daily_cycle_handler.py).
ARTICLE_SUBJECT_FIELD = "subject"
# How long a day subject's text may be where it reaches a prompt (`clean_day_subject`): its mandate
# and its style are a topic's editorial goal for the day, so they have the editorial goals' bound
# (common/editorial_resolver.MAX_GOAL_TEXT_CHARS, what the Admin API lets an operator store); its
# label names one of the topic's subjects, a vision site for the rail adapter, so it has a site
# name's (common/vision_sites.MAX_SITE_NAME_CHARS; a test holds the two equal), and a site's name is
# always a label.
MAX_DAY_SUBJECT_TEXT_CHARS = MAX_GOAL_TEXT_CHARS
MAX_DAY_SUBJECT_LABEL_CHARS = 80
# The bound is on the text once cleaned (plain_text collapses whitespace and drops control and
# invisible characters), and cleaning costs time and memory in proportion to the raw text. So raw
# text longer than this many times its bound is refused before it is cleaned: room for indentation,
# line breaks and invisible characters many times over, and a pathological string is refused at once.
DAY_SUBJECT_RAW_TEXT_MULTIPLE = 4

# The rota's two adapter_config keys, its weekday names (Monday first, as date.weekday counts), the
# day a list rota counts from, and how long a list rota may be (two months of days, one each).
ROTATION_KEY = "rotation"
ROTATION_TIMEZONE_KEY = "rotation_timezone"
WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
ROTATION_EPOCH = date(1970, 1, 1)
MAX_ROTATION_SLOTS = 62
DEFAULT_ROTATION_TIMEZONE = "UTC"


def clean_subjects(raw) -> list[str]:
    """The subjects a Finding keeps, from what an adapter's `subjects` returned: the strings that
    are SUBJECT_KEY-shaped, each once, in the order given, the first MAX_SUBJECTS_PER_FINDING.

    Any iterable of keys is read: a list or a tuple, a generator (the rail adapter's reported
    scenes are one), or a set, whose strings are read in sorted order so every tick keeps the same
    ones (only a string can be kept, so nothing else is sorted: a str() of each value could cost
    far more than the set). At most MAX_SUBJECT_VALUES_EXAMINED values are looked at, so a
    generator that never ends, or yields only duplicates or junk, still returns; a set longer than
    that raises ValueError before it is sorted. None is none. Anything else (a string, which would
    be read a character at a time; a mapping; a number) raises TypeError, so the research tick's
    log says the subjects were dropped and why (research_tick_handler._subjects), as it does for a
    hook that raises: a rotating topic never drafts a Finding without subjects, so a wrong type must
    not go quiet."""
    if raw is None:
        return []
    if isinstance(raw, str | bytes | bytearray | Mapping) or not isinstance(raw, Iterable):
        raise TypeError(f"subjects must be an iterable of subject keys, not a {type(raw).__name__}")
    if isinstance(raw, set | frozenset):
        if len(raw) > MAX_SUBJECT_VALUES_EXAMINED:
            raise ValueError(
                f"subjects is a set of {len(raw)} values, more than the {MAX_SUBJECT_VALUES_EXAMINED} read"
            )
        raw = sorted(value for value in raw if isinstance(value, str))
    kept: list[str] = []
    for value in islice(raw, MAX_SUBJECT_VALUES_EXAMINED):
        if isinstance(value, str) and SUBJECT_KEY.fullmatch(value) and value not in kept:
            kept.append(value)
            if len(kept) == MAX_SUBJECTS_PER_FINDING:
                break
    return kept


def reports_subject(finding: dict, key: str) -> bool:
    """Whether a Finding's stored `subjects` list `key`. A Finding without the field (every
    Finding of a topic that does not rotate, and those written before the field existed) reports
    none, and so is never drafted by a rotating topic. A field that is not a list reports none:
    a string would otherwise match its substrings."""
    stored = (finding or {}).get(SUBJECTS_FIELD)
    return isinstance(stored, list | tuple | set | frozenset) and key in stored


def subject_findings(findings: list[dict], key: str) -> list[dict]:
    """The Findings that report subject `key`, in the order given (newest first, as read)."""
    return [finding for finding in findings if reports_subject(finding, key)]


def day_subject_error(subject) -> str | None:
    """What is wrong with what an adapter's `day_subject` returned, or None: it must be a DaySubject
    whose key is SUBJECT_KEY-shaped and whose label, mandate and style are text with something in
    them once cleaned to one plain line (`clean_day_subject`: they go into the prompts), the label
    at most MAX_DAY_SUBJECT_LABEL_CHARS and the mandate and style at most
    MAX_DAY_SUBJECT_TEXT_CHARS. Too long is refused, not cut: a mandate cut short is not the one
    the adapter wrote, and a broken day subject writes nothing. Raw text over
    DAY_SUBJECT_RAW_TEXT_MULTIPLE times the bound is refused the same way before it is cleaned."""
    if not isinstance(subject, DaySubject):
        return f"is a {type(subject).__name__}, not a DaySubject"
    if not isinstance(subject.key, str) or not SUBJECT_KEY.fullmatch(subject.key):
        return "has a key that is not a plain subject key"
    for name, most in (
        ("label", MAX_DAY_SUBJECT_LABEL_CHARS),
        ("mandate", MAX_DAY_SUBJECT_TEXT_CHARS),
        ("style", MAX_DAY_SUBJECT_TEXT_CHARS),
    ):
        value = getattr(subject, name)
        if isinstance(value, str) and len(value) > DAY_SUBJECT_RAW_TEXT_MULTIPLE * most:
            return f"has a {name} longer than {most} characters"
        text = plain_text(value) if isinstance(value, str) else ""
        if not text:
            return f"has no {name}"
        if len(text) > most:
            return f"has a {name} longer than {most} characters"
    return None


def clean_day_subject(subject: DaySubject) -> DaySubject:
    """A usable DaySubject (day_subject_error says None) as the prompts get it: its label, mandate
    and style each one plain line (common/figures.plain_text, the rule a site's name is cleaned by:
    no newline or control character, no invisible one, whitespace collapsed), so nothing an
    adapter folds in from a topic's config can start a section of its own in a prompt. The same
    object when there is nothing to clean."""
    cleaned = dataclasses.replace(
        subject,
        label=plain_text(subject.label),
        mandate=plain_text(subject.mandate),
        style=plain_text(subject.style),
    )
    return subject if cleaned == subject else cleaned


def resolve_day_subject(topic: dict, at: datetime) -> DaySubject | None:
    """The subject the topic writes about at `at` (the daily run's start), from its adapter's
    `day_subject`, or None: the adapter names none (every adapter but one that rotates), or the
    topic's adapter is not registered. Raises ValueError for anything that is not a usable
    DaySubject, and lets whatever the hook raises through: a broken rota stops the run, it never
    becomes an article about every subject at once. A usable one comes back cleaned
    (`clean_day_subject`)."""
    # Imported when asked, not with this module: an adapter that names its subjects by the rota
    # below imports this module, and the registry imports every adapter.
    from common.adapters.registry import ADAPTER_REGISTRY

    adapter_key = (topic or {}).get("adapter")
    adapter_cls = ADAPTER_REGISTRY.get(adapter_key) if isinstance(adapter_key, str) else None
    if adapter_cls is None:
        return None
    subject = adapter_cls().day_subject(topic, at)
    if subject is None:
        return None
    problem = day_subject_error(subject)
    if problem:
        raise ValueError(f"the {adapter_key} adapter's day subject {problem}")
    return clean_day_subject(subject)


# --- a rota: which subject has which day ----------------------------------------------------------


def _zone(name) -> ZoneInfo:
    """The zone named `name`, or ValueError: an IANA name (common/scheduler.validate_timezone's
    rule) that this Python's tz database knows."""
    validate_timezone(name)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError(f"timezone {name!r} is not a zone this system knows") from exc


def parse_rotation(
    adapter_config: dict | None, subject_keys, default_timezone: str | None = None
) -> dict | None:
    """The topic's rota from its adapter_config, checked against the subjects it has
    (`subject_keys`), or None when the config sets no `rotation`.

    Returns {"kind": "weekly" | "cycle", "slots": (key, ...), "timezone": name}: a weekly rota's
    slots are Monday to Sunday. Raises ValueError, with the message an operator needs, for a rota
    that is not the seven weekdays or a list of 1 to MAX_ROTATION_SLOTS keys, a slot that is not one
    of the subjects, a subject that has no day (nobody would ever write about it), a
    `rotation_timezone` without a rota, or a zone that is not one. The zone is `rotation_timezone`,
    else `default_timezone` (an adapter may pass the topic's `daily_timezone`), else UTC.
    """
    config = adapter_config if isinstance(adapter_config, dict) else {}
    raw = config.get(ROTATION_KEY)
    zone_name = config.get(ROTATION_TIMEZONE_KEY)
    if raw is None:
        if zone_name is not None:
            raise ValueError(f"'{ROTATION_TIMEZONE_KEY}' is set but there is no '{ROTATION_KEY}'")
        return None
    keys = list(subject_keys)
    if isinstance(raw, dict):
        days = {str(day).strip().upper(): slot for day, slot in raw.items()}
        if len(days) != len(raw) or set(days) != set(WEEKDAYS):
            raise ValueError(
                f"'{ROTATION_KEY}' as an object must name each of {', '.join(WEEKDAYS)} once"
            )
        kind, slots = "weekly", tuple(days[day] for day in WEEKDAYS)
    elif isinstance(raw, list | tuple):
        if not 1 <= len(raw) <= MAX_ROTATION_SLOTS:
            raise ValueError(f"'{ROTATION_KEY}' as a list must have 1 to {MAX_ROTATION_SLOTS} entries")
        kind, slots = "cycle", tuple(raw)
    else:
        raise ValueError(f"'{ROTATION_KEY}' must be an object of weekdays or a list of subjects")
    for slot in slots:
        if not isinstance(slot, str) or slot not in keys:
            raise ValueError(
                f"'{ROTATION_KEY}' names {str(slot)[:64]!r}, which is not one of the topic's subjects"
            )
    unused = [key for key in keys if key not in slots]
    if unused:
        raise ValueError(f"'{ROTATION_KEY}' gives no day to {', '.join(unused)}: every subject needs one")
    if zone_name is None:
        zone_name = default_timezone or DEFAULT_ROTATION_TIMEZONE
    _zone(zone_name)
    return {"kind": kind, "slots": slots, "timezone": zone_name}


def rotation_day(rotation: dict, at: datetime) -> date:
    """The calendar day `at` falls on in the rota's zone (a naive `at` is UTC)."""
    moment = at if at.tzinfo is not None else at.replace(tzinfo=UTC)
    return moment.astimezone(_zone(rotation["timezone"])).date()


def rotation_subject(rotation: dict, at: datetime) -> str:
    """The subject key whose day `at` is, by a rota from `parse_rotation`: the weekday's slot, or
    the list's slot for the number of days since 1970-01-01. Pure: the same moment always gives
    the same key, whoever asks."""
    day = rotation_day(rotation, at)
    slots = rotation["slots"]
    if rotation["kind"] == "weekly":
        return slots[day.weekday()]
    return slots[(day - ROTATION_EPOCH).days % len(slots)]
