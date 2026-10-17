"""common/goal_rotation.py: a topic's editorial goals taken in turn."""

import pytest

from common import goal_rotation
from common.adapters.base import SUBJECT_KEY
from common.editorial_resolver import MAX_GOAL_TEXT_CHARS

ROTATION = [
    {"name": "access", "primary_focus": "The served share and the area beyond reach."},
    {"name": "network", "primary_focus": "The flags and what each measures."},
    {"name": "simulations", "primary_focus": "The simulated links, in conditional words."},
]
GOALS = goal_rotation.rotation_goals({goal_rotation.FIELD: ROTATION})


def _state(goal="access", used=("access",), since="2026-10-10T00:00:00+00:00", until="2026-10-11T00:00:00Z"):
    return {"goal": goal, "used": list(used), "from": since, "to": until}


# --- validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, [], ROTATION, ROTATION[:2]])
def test_valid_rotations(value):
    assert goal_rotation.validate_goal_rotation(value) is None


@pytest.mark.parametrize(
    "value, words",
    [
        ({"name": "a"}, "must be a list"),
        (ROTATION[:1], "2 to 7"),
        ([*ROTATION, *[{"name": f"g{i}", "primary_focus": "x"} for i in range(5)]], "2 to 7"),
        ([ROTATION[0], "access"], "must be an object"),
        ([ROTATION[0], {**ROTATION[1], "tone": "x"}], "unknown field"),
        ([ROTATION[0], {**ROTATION[1], "name": "Network"}], "lower-case"),
        ([ROTATION[0], {**ROTATION[1], "name": "_net"}], "lower-case"),
        ([ROTATION[0], {**ROTATION[1], "name": "x" * 33}], "lower-case"),
        ([ROTATION[0], {**ROTATION[1], "name": "access"}], "used twice"),
        ([ROTATION[0], {"name": "network"}], "non-empty string"),
        ([ROTATION[0], {**ROTATION[1], "primary_focus": "  "}], "non-empty string"),
        ([ROTATION[0], {**ROTATION[1], "primary_focus": "x" * (MAX_GOAL_TEXT_CHARS + 1)}], "at most"),
    ],
)
def test_invalid_rotations_are_refused_with_a_reason(value, words):
    assert words in goal_rotation.validate_goal_rotation(value)


def test_normalize_strips_the_focus_and_keeps_the_order():
    value = [{"name": "b", "primary_focus": "  B  "}, {"name": "a", "primary_focus": "A"}]
    assert goal_rotation.normalize_goal_rotation(value) == [
        {"name": "b", "primary_focus": "B"},
        {"name": "a", "primary_focus": "A"},
    ]
    assert goal_rotation.normalize_goal_rotation(None) == []


def test_a_stored_rotation_that_is_not_valid_reads_as_none():
    assert goal_rotation.rotation_goals({}) == []
    assert goal_rotation.rotation_goals({goal_rotation.FIELD: ROTATION[:1]}) == []
    assert [goal.name for goal in GOALS] == ["access", "network", "simulations"]


# --- state ----------------------------------------------------------------------------------------


def test_the_topic_wide_key_can_never_be_a_subject_key():
    assert not SUBJECT_KEY.fullmatch(goal_rotation.TOPIC_WIDE_KEY)
    assert goal_rotation.state_key(None) == goal_rotation.TOPIC_WIDE_KEY
    assert goal_rotation.state_key("sydney") == "sydney"


def test_read_state_takes_only_the_shape_it_writes():
    good = _state()
    states = {"sydney": good, "bad": {**good, "used": "access"}, "extra": {**good, "x": 1}}
    topic = {goal_rotation.STATE_FIELD: states}
    assert goal_rotation.read_state(topic, "sydney") == good
    assert goal_rotation.read_state(topic, "bad") is None
    assert goal_rotation.read_state(topic, "extra") is None
    assert goal_rotation.read_state(topic, "melbourne") is None
    assert goal_rotation.read_state({goal_rotation.STATE_FIELD: "not a map"}, "sydney") is None


# --- picking --------------------------------------------------------------------------------------

FINDINGS = [
    {"captured_at": "2026-10-12T05:00:00+00:00", "summary": "newest"},
    {"captured_at": "2026-10-11T05:00:00+00:00", "summary": "older"},
]


def test_new_findings_take_the_first_goal_the_first_time():
    pick = goal_rotation.pick_for_new_findings(GOALS, None, FINDINGS)
    assert pick.goal.name == "access"
    assert pick.revisit is False
    assert pick.findings is FINDINGS
    assert pick.state == {
        "goal": "access",
        "used": ["access"],
        "from": "2026-10-11T05:00:00+00:00",
        "to": "2026-10-12T05:00:00+00:00",
    }


@pytest.mark.parametrize(
    "last, expected",
    [("access", "network"), ("network", "simulations"), ("simulations", "access"), ("gone", "access")],
)
def test_new_findings_take_the_goal_after_the_last_one_used(last, expected):
    pick = goal_rotation.pick_for_new_findings(GOALS, _state(goal=last, used=(last,)), FINDINGS)
    assert pick.goal.name == expected
    assert pick.state["used"] == [expected]  # a new set of Findings: nothing used on it yet


def test_a_revisit_takes_the_next_goal_not_yet_used_on_the_set():
    state = _state(goal="network", used=("network",))
    goal = goal_rotation.next_unused(GOALS, state)
    assert goal.name == "simulations"
    pick = goal_rotation.pick_for_revisit(state, goal, FINDINGS)
    assert pick.revisit is True
    assert pick.state == {**state, "goal": "simulations", "used": ["network", "simulations"]}
    # ... then wraps round to the one left
    assert goal_rotation.next_unused(GOALS, pick.state).name == "access"


def test_no_revisit_once_every_goal_is_used_or_without_a_set():
    every_goal = _state(goal="access", used=("network", "simulations", "access"))
    assert goal_rotation.next_unused(GOALS, every_goal) is None
    assert goal_rotation.next_unused(GOALS, None) is None
    assert goal_rotation.next_unused(GOALS, _state(since="")) is None


def test_a_goal_removed_from_the_list_is_no_longer_counted_as_used_or_picked():
    goals = goal_rotation.rotation_goals({goal_rotation.FIELD: ROTATION[:2]})
    removed = _state(goal="simulations", used=("simulations",))
    assert goal_rotation.next_unused(goals, removed).name == "access"
