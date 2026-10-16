"""common/day_subject.py and the adapter hooks behind it (common/adapters/base.py): what a Finding
keeps of the subjects an observation reports, which Findings are a subject's, asking a topic's
adapter for the day's subject, and the generic rota an adapter can name it by."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from common import day_subject
from common.adapters.base import SUBJECT_KEY, Adapter, DaySubject
from common.adapters.registry import ADAPTER_REGISTRY
from common.vision_sites import SITE_ID


def test_a_subject_key_is_shaped_like_a_site_id_so_every_site_id_is_one():
    assert SUBJECT_KEY.pattern == SITE_ID.pattern
    assert SUBJECT_KEY.fullmatch("sydney") and SUBJECT_KEY.fullmatch("new_york-2")
    for bad in ("", "has space", "../up", "a.b", "x" * 65, "sydney\n"):
        assert not SUBJECT_KEY.fullmatch(bad)


def test_clean_subjects_keeps_plain_keys_once_in_order_at_most_the_cap():
    raw = ["sydney", "melbourne", "sydney", "not a key", None, 3, "", *[f"c{n}" for n in range(20)]]

    kept = day_subject.clean_subjects(raw)

    assert kept[:3] == ["sydney", "melbourne", "c0"]
    assert len(kept) == day_subject.MAX_SUBJECTS_PER_FINDING == 10
    assert day_subject.clean_subjects(("a", "b")) == ["a", "b"]


def test_anything_but_a_list_is_no_subjects():
    for raw in (None, "sydney", {"sydney": 1}, {"sydney"}, 7):
        assert day_subject.clean_subjects(raw) == []


def test_every_registered_adapter_names_no_day_subject_and_no_subjects_by_default():
    """S1 adds the hooks and no adapter uses them: every topic is as it was (S2 opts rail in)."""
    topic = {"topic_id": "t", "adapter": "web_search", "adapter_config": {}}
    at = datetime(2026, 10, 16, 9, 5, tzinfo=UTC)
    for key, adapter_class in ADAPTER_REGISTRY.items():
        assert adapter_class.day_subject is Adapter.day_subject, key
        assert adapter_class.subjects is Adapter.subjects, key
        assert adapter_class().day_subject({**topic, "adapter": key}, at) is None


def test_a_day_subject_is_plain_frozen_data():
    subject = DaySubject(key="sydney", label="Sydney", mandate="Report for Sydney.", style="Plain.")

    assert subject == DaySubject("sydney", "Sydney", "Report for Sydney.", "Plain.")
    assert hash(subject) == hash(DaySubject("sydney", "Sydney", "Report for Sydney.", "Plain."))
    with pytest.raises(dataclasses.FrozenInstanceError):
        subject.key = "melbourne"


# --- which Findings are a subject's ---------------------------------------------------------------


def test_a_finding_reports_a_subject_only_when_its_list_names_it():
    assert day_subject.reports_subject({"subjects": ["north", "south"]}, "south")
    assert day_subject.reports_subject({"subjects": {"north"}}, "north")  # a set, as boto3 reads an SS
    assert not day_subject.reports_subject({"subjects": ["north"]}, "south")
    assert not day_subject.reports_subject({}, "north")  # no field: a plain topic's Finding
    assert not day_subject.reports_subject({"subjects": None}, "north")
    assert not day_subject.reports_subject({"subjects": "northern"}, "north")  # never a substring
    assert not day_subject.reports_subject(None, "north")


def test_subject_findings_keeps_the_subjects_in_the_order_given():
    findings = [
        {"summary": "both", "subjects": ["north", "south"]},
        {"summary": "south", "subjects": ["south"]},
        {"summary": "none"},
        {"summary": "north", "subjects": ["north"]},
    ]

    assert [f["summary"] for f in day_subject.subject_findings(findings, "north")] == ["both", "north"]
    assert day_subject.subject_findings(findings, "east") == []


# --- asking the topic's adapter -------------------------------------------------------------------

SUBJECT = DaySubject(key="north", label="North", mandate="Report for North.", style="Plain.")


class _Named(Adapter):
    named = SUBJECT

    def fetch_state(self, topic_config):
        return {}

    def material_diff(self, old_state, new_state):
        return False, ""

    def source_refs(self, new_state):
        return []

    def day_subject(self, topic_config, at):
        return self.named


@pytest.mark.parametrize(
    "returned, words",
    [
        ("north", "is a str, not a DaySubject"),
        (DaySubject(key="no th", label="N", mandate="M", style="S"), "not a plain subject key"),
        (DaySubject(key="north", label=" ", mandate="M", style="S"), "has no label"),
        (DaySubject(key="north", label="N", mandate="", style="S"), "has no mandate"),
        (DaySubject(key="north", label="N", mandate="M", style=None), "has no style"),
    ],
)
def test_what_is_not_a_usable_day_subject_is_refused_with_why(monkeypatch, returned, words):
    monkeypatch.setitem(ADAPTER_REGISTRY, "named", _Named)
    monkeypatch.setattr(_Named, "named", returned)

    assert words in day_subject.day_subject_error(returned)
    with pytest.raises(ValueError, match=re.escape(words)):
        day_subject.resolve_day_subject({"adapter": "named"}, AT)


def test_resolve_asks_the_registered_adapter_and_passes_the_topic_and_the_moment(monkeypatch):
    asked = []

    class _Recording(_Named):
        def day_subject(self, topic_config, at):
            asked.append((topic_config, at))
            return SUBJECT

    monkeypatch.setitem(ADAPTER_REGISTRY, "named", _Recording)
    topic = {"topic_id": "t", "adapter": "named"}

    assert day_subject.resolve_day_subject(topic, AT) is SUBJECT
    assert asked == [(topic, AT)]


def test_resolve_names_no_subject_for_an_adapter_that_names_none_or_is_not_registered():
    assert day_subject.resolve_day_subject({"adapter": "github_trending"}, AT) is None
    for adapter in ("nope", None, ["web_search"]):
        assert day_subject.resolve_day_subject({"adapter": adapter}, AT) is None
    assert day_subject.resolve_day_subject({}, AT) is None


def test_whatever_the_hook_raises_goes_through(monkeypatch):
    class _Broken(_Named):
        def day_subject(self, topic_config, at):
            raise RuntimeError("the rota broke")

    monkeypatch.setitem(ADAPTER_REGISTRY, "named", _Broken)

    with pytest.raises(RuntimeError, match="the rota broke"):
        day_subject.resolve_day_subject({"adapter": "named"}, AT)


# --- the rota ---------------------------------------------------------------------------------------

AT = datetime(2026, 10, 16, 9, 5, tzinfo=UTC)
CITIES = ("sydney", "melbourne", "singapore", "boston")
WEEK = {"MON": "sydney", "TUE": "melbourne", "WED": "singapore", "THU": "boston",
        "FRI": "sydney", "SAT": "melbourne", "SUN": "singapore"}  # fmt: skip


def _sydney(day: int, hour: int, month: int = 10) -> datetime:
    return datetime(2026, month, day, hour, 5, tzinfo=ZoneInfo("Australia/Sydney")).astimezone(UTC)


def test_no_rotation_is_no_rota():
    assert day_subject.parse_rotation({}, CITIES) is None
    assert day_subject.parse_rotation(None, CITIES) is None
    assert day_subject.parse_rotation({"sites": []}, CITIES) is None


def test_a_weekly_rota_reads_the_weekday_names_and_defaults_its_zone():
    rota = day_subject.parse_rotation({"rotation": {k.lower(): v for k, v in WEEK.items()}}, CITIES)

    slots = tuple(WEEK[day] for day in day_subject.WEEKDAYS)
    assert rota == {"kind": "weekly", "slots": slots, "timezone": "UTC"}
    assert day_subject.parse_rotation({"rotation": WEEK}, CITIES, "Australia/Sydney")["timezone"] == (
        "Australia/Sydney"
    )
    assert day_subject.parse_rotation(
        {"rotation": WEEK, "rotation_timezone": "Asia/Singapore"}, CITIES, "Australia/Sydney"
    )["timezone"] == "Asia/Singapore"


def test_the_rota_is_read_on_its_zones_calendar_across_a_clock_change():
    """Sydney's daylight saving starts on Sunday 2026-10-04: 09:05 there is 23:05 UTC the day before
    until then and 22:05 after. The UTC date is the day before throughout; the rota never uses it."""
    rota = day_subject.parse_rotation({"rotation": WEEK, "rotation_timezone": "Australia/Sydney"}, CITIES)
    expected = {2: "sydney", 3: "melbourne", 4: "singapore", 5: "sydney", 6: "melbourne"}  # Fri .. Tue

    for day, city in expected.items():
        at = _sydney(day, 9)
        assert at.date().day == day - 1  # the UTC calendar says yesterday
        assert day_subject.rotation_day(rota, at).day == day
        assert day_subject.rotation_subject(rota, at) == city
    assert _sydney(3, 9).hour == 23 and _sydney(4, 9).hour == 22  # the clock did change
    # Just either side of local midnight.
    assert day_subject.rotation_subject(rota, _sydney(4, 0) - timedelta(minutes=10)) == "melbourne"
    assert day_subject.rotation_subject(rota, _sydney(4, 0)) == "singapore"


def test_a_list_rota_cycles_one_a_day_from_1970():
    rota = day_subject.parse_rotation({"rotation": ["sydney", "boston", "melbourne", "singapore"]}, CITIES)
    start = datetime(1970, 1, 1, 12, tzinfo=UTC)

    assert rota["kind"] == "cycle"
    assert [day_subject.rotation_subject(rota, start + timedelta(days=n)) for n in range(6)] == [
        "sydney", "boston", "melbourne", "singapore", "sydney", "boston",
    ]
    # A naive moment is UTC, and the same moment always gives the same subject.
    naive = AT.replace(tzinfo=None)
    assert day_subject.rotation_subject(rota, naive) == day_subject.rotation_subject(rota, AT)


@pytest.mark.parametrize(
    "config, words",
    [
        ({"rotation": {**WEEK, "MON": "perth"}}, "'perth', which is not one of the topic's subjects"),
        ({"rotation": {**WEEK, "THU": "sydney"}}, "gives no day to boston"),
        ({"rotation": {k: v for k, v in WEEK.items() if k != "SUN"}}, "must name each of MON"),
        ({"rotation": {**WEEK, "FUNDAY": "boston"}}, "must name each of MON"),
        ({"rotation": {**WEEK, "mon": "sydney"}}, "must name each of MON"),  # MON twice
        ({"rotation": []}, "1 to 62 entries"),
        ({"rotation": ["sydney"] * 63}, "1 to 62 entries"),
        ({"rotation": ["sydney", "melbourne", "singapore", 4]}, "'4', which is not one"),
        ({"rotation": "sydney"}, "an object of weekdays or a list"),
        ({"rotation_timezone": "UTC"}, "'rotation_timezone' is set but there is no 'rotation'"),
        ({"rotation": WEEK, "rotation_timezone": "not a zone"}, "IANA zone name"),
        ({"rotation": WEEK, "rotation_timezone": "Mars/Olympus_Mons"}, "is not a zone this system knows"),
    ],
)
def test_a_rota_that_cannot_be_used_says_why(config, words):
    with pytest.raises(ValueError, match=re.escape(words)):
        day_subject.parse_rotation(config, CITIES)
