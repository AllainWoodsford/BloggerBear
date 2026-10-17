"""A topic's goal rotation: several standing editorial goals it takes in turn, so one set of
Findings can be written up from different sides instead of once.

A topic may set `editorial_goal_rotation`, a list of 2 to MAX_GOALS goals, each
`{"name", "primary_focus"}`. Each one replaces the topic's own `editorial_goals.primary_focus` for
the article it is picked for; the topic's `editorial_goals.exclusion_criteria` (and the adapter's
own rules) apply to every one of them, so the limits are written once.

Which goal an article is written under (`pick`), per subject for a topic that rotates through
subjects (common/day_subject.py) and once for the topic otherwise (TOPIC_WIDE_KEY):

- **New Findings** since the last article (the daily cycle's window): the goal after the one used
  last, in list order (the first, the first time). The Findings drafted become the subject's
  current set, and that goal is the first used on it.
- **Nothing new:** a *revisit*. The current set is read back (Findings captured between its oldest
  and newest, still unexpired) and written up under the next goal not yet used on it. Once every
  goal has been used on it, or its Findings have expired, there is nothing to write: the run stops
  as it always did (`no_findings`).

So a quiet subject gets at most one article per goal per set of Findings, and never the same goal
on the same Findings twice. The state is one small map on the topic item (`goal_rotation_state`,
subject key to {"goal", "used", "from", "to"}), written by the daily cycle after the article
exists (common/dynamo.set_topic_goal_rotation_state). A topic with a daily editorial goal (the
crypto feed, common/editorial_goals.py) has its own rotation and ignores this one.

Pure: nothing here reads or writes AWS. Topic-agnostic: nothing here knows any adapter.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from common.editorial_resolver import MAX_GOAL_TEXT_CHARS

FIELD = "editorial_goal_rotation"
STATE_FIELD = "goal_rotation_state"
# The state key of a topic that does not rotate through subjects. Not a valid subject key
# (common/adapters/base.SUBJECT_KEY: letters, digits, `_` and `-`), so the two never collide.
TOPIC_WIDE_KEY = "*"
MIN_GOALS, MAX_GOALS = 2, 7
GOAL_FIELDS = ("name", "primary_focus")
_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_STATE_FIELDS = ("goal", "used", "from", "to")


@dataclass(frozen=True)
class RotatingGoal:
    name: str
    primary_focus: str


@dataclass(frozen=True)
class Pick:
    """The goal an article is written under, the Findings it is written from, the state to store
    once it exists, and whether it revisits Findings an earlier article used."""

    goal: RotatingGoal
    findings: list[dict]
    state: dict
    revisit: bool


def validate_goal_rotation(value) -> str | None:
    """A message if `value` is not a valid `editorial_goal_rotation`, else None. None and [] are
    valid: no rotation."""
    if value is None or value == []:
        return None
    if not isinstance(value, list):
        return f"'{FIELD}' must be a list of goals if provided"
    if not MIN_GOALS <= len(value) <= MAX_GOALS:
        return f"'{FIELD}' must hold {MIN_GOALS} to {MAX_GOALS} goals ([] clears it)"
    names = set()
    for index, goal in enumerate(value):
        where = f"'{FIELD}[{index}]'"
        if not isinstance(goal, dict):
            return f"{where} must be an object"
        unknown = sorted(set(goal) - set(GOAL_FIELDS))
        if unknown:
            return f"{where} has unknown field(s) {unknown}; allowed: {list(GOAL_FIELDS)}"
        name = goal.get("name")
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            return (
                f"{where}.name must be 1 to 32 lower-case letters, digits, '_' or '-', starting with "
                "a letter or digit"
            )
        if name in names:
            return f"{where}.name {name!r} is used twice"
        names.add(name)
        focus = goal.get("primary_focus")
        if not isinstance(focus, str) or not focus.strip():
            return f"{where}.primary_focus must be a non-empty string"
        if len(focus.strip()) > MAX_GOAL_TEXT_CHARS:
            return f"{where}.primary_focus must be at most {MAX_GOAL_TEXT_CHARS} characters"
    return None


def normalize_goal_rotation(value) -> list[dict]:
    """A validated rotation with its text stripped (None becomes [])."""
    return [{"name": goal["name"], "primary_focus": goal["primary_focus"].strip()} for goal in value or []]


def rotation_goals(topic: dict) -> list[RotatingGoal]:
    """The topic's rotation as stored, or [] when it has none or the stored one is not valid
    (edited by hand): never trusted, so a broken rotation writes as if there were none."""
    value = (topic or {}).get(FIELD)
    if not value or validate_goal_rotation(value) is not None:
        return []
    return [RotatingGoal(goal["name"], goal["primary_focus"].strip()) for goal in value]


def state_key(subject_key: str | None) -> str:
    """Where the rotation's state is kept for an article on `subject_key` (None: a topic that does
    not rotate through subjects)."""
    return subject_key if subject_key is not None else TOPIC_WIDE_KEY


def read_state(topic: dict, key: str) -> dict | None:
    """The stored state under `key`, or None when there is none or it is not the shape this module
    writes (edited by hand): read as no state, so the rotation starts again at its first goal."""
    states = (topic or {}).get(STATE_FIELD)
    state = states.get(key) if isinstance(states, dict) else None
    if not isinstance(state, dict) or set(state) != set(_STATE_FIELDS):
        return None
    used = state["used"]
    if not isinstance(used, list) or not all(isinstance(name, str) for name in used):
        return None
    if not all(isinstance(state[field], str) for field in ("goal", "from", "to")):
        return None
    return state


def _after(goals: list[RotatingGoal], name: str | None) -> list[RotatingGoal]:
    """The goals in list order, starting after `name` and wrapping round; from the first when
    `name` is not in the list (no state, or a goal since removed)."""
    names = [goal.name for goal in goals]
    start = names.index(name) + 1 if name in names else 0
    return goals[start:] + goals[:start]


def captured_range(findings: list[dict]) -> tuple[str, str] | None:
    """The oldest and newest `captured_at` of `findings`, or None when none has one."""
    stamps = sorted(f["captured_at"] for f in findings if isinstance(f.get("captured_at"), str))
    return (stamps[0], stamps[-1]) if stamps else None


def pick_for_new_findings(goals: list[RotatingGoal], state: dict | None, findings: list[dict]) -> Pick:
    """New Findings: the goal after the one used last, and the Findings become the current set."""
    goal = _after(goals, state["goal"] if state else None)[0]
    stamps = captured_range(findings)
    since, until = stamps if stamps else ("", "")
    return Pick(goal, findings, {"goal": goal.name, "used": [goal.name], "from": since, "to": until}, False)


def next_unused(goals: list[RotatingGoal], state: dict | None) -> RotatingGoal | None:
    """The next goal not yet used on the current set, after the one used last; None when there is
    no current set (nothing was ever written, or its range was not recorded) or every goal has been
    used on it."""
    if state is None or not state["from"] or not state["to"]:
        return None
    used = set(state["used"])
    return next((goal for goal in _after(goals, state["goal"]) if goal.name not in used), None)


def pick_for_revisit(state: dict, goal: RotatingGoal, findings: list[dict]) -> Pick:
    """A revisit: `findings` (the current set, read back) under `goal`, added to the goals used."""
    used = [*state["used"], goal.name]
    new_state = {"goal": goal.name, "used": used, "from": state["from"], "to": state["to"]}
    return Pick(goal, findings, new_state, True)
