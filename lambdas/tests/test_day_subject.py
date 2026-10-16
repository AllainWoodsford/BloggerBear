"""common/day_subject.py and the adapter hooks behind it (common/adapters/base.py): what a Finding
keeps of the subjects an observation reports."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

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
