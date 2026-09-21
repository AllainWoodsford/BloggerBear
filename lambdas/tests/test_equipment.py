"""Tests for common/equipment.py: the rules for what the bear wears."""

from __future__ import annotations

import random

import pytest

from common import equipment as eq


class TakeAll:
    """A stand-in for the random choice that takes in every piece of armor, for exact assertions."""

    @staticmethod
    def randint(low, high):
        return high

    @staticmethod
    def sample(population, count):
        return list(population)


def item(topic="t1", version="v1", text="Be concise.", **fields):
    base = {"topic_id": topic, "version": version, "prompt_changes": text, "status": "approved"}
    return {**base, **fields}


def worn(slot, scope, topic="t1", version="v1", text="Be concise.", at="2026-09-01"):
    return item(topic, version, text, equipped=True, slot=slot, scope=scope, equipped_at=at)


def benched(topic="t1", version="v1", text="Be concise."):
    return item(topic, version, text, equipped=False)


# --- what counts as worn, benched, legacy -------------------------------------------------


def test_worn_benched_and_legacy_are_told_apart():
    assert eq.is_equipped(worn("helmet", "global"))
    assert not eq.is_equipped(benched())
    assert eq.is_legacy(item())  # approved, never touched by equipment
    assert not eq.is_legacy(benched())  # benched on purpose is not legacy
    assert not eq.is_legacy(item(status="pending"))


def test_the_backpack_is_approved_and_benched_only():
    items = [
        benched(version="a"),
        worn("ring", "topic", version="b"),
        item(version="c"),
        item(version="d", status="pending"),
    ]

    assert [i["version"] for i in eq.backpack(items)] == [
        "a"
    ]  # legacy is still in use; pending is not approved


# --- where a new item goes ----------------------------------------------------------------


def test_a_global_item_takes_the_first_empty_armor_slot():
    items = [worn("helmet", "global"), worn("gloves", "global", version="g")]

    assert eq.suggest_slot(items, "global") == "chest"


def test_a_topic_item_suggests_a_ring_until_they_are_full():
    rings = [worn("ring", "topic", version=f"r{n}") for n in range(eq.MAX_RINGS)]

    assert eq.suggest_slot(rings[:-1], "topic") == "ring"
    assert eq.suggest_slot(rings, "topic") is None


def test_no_armor_suggestion_when_every_slot_is_worn():
    items = [worn(slot, "global", version=slot) for slot in eq.ARMOR_SLOTS]

    assert eq.suggest_slot(items, "global") is None


def test_equipping_into_an_occupied_armor_slot_displaces_the_occupant():
    old = worn("helmet", "global", version="old")
    new = benched(version="new")

    plan = eq.plan_equip([old, new], new, scope="global", slot="helmet")

    assert plan["slot"] == "helmet"
    assert plan["displaced"]["version"] == "old"


def test_equipping_into_an_empty_slot_displaces_nothing():
    plan = eq.plan_equip(
        [worn("helmet", "global", version="a")], benched(version="b"), scope="global", slot="boots"
    )

    assert plan == {"scope": "global", "slot": "boots", "displaced": None}


def test_with_no_slot_a_global_item_goes_to_the_first_empty_one():
    plan = eq.plan_equip([worn("helmet", "global", version="a")], benched(version="b"), scope="global")

    assert plan["slot"] == "chest"


def test_with_every_armor_slot_worn_a_global_item_needs_a_slot_to_replace():
    items = [worn(slot, "global", version=slot) for slot in eq.ARMOR_SLOTS]

    with pytest.raises(eq.EquipError) as caught:
        eq.plan_equip(items, benched(version="new"), scope="global")

    assert caught.value.status == 409


def test_moving_a_worn_item_does_not_displace_itself():
    me = worn("helmet", "global", version="me")

    plan = eq.plan_equip([me], me, scope="global", slot="helmet")

    assert plan["displaced"] is None


def test_a_bad_slot_or_scope_is_a_400():
    target = benched()
    for kwargs in (
        {"scope": "everywhere"},
        {"scope": "global", "slot": "hat"},
        {"scope": "global", "slot": "ring"},
        {"scope": "topic", "slot": "helmet"},
        {"scope": "global", "slot": "helmet", "replace": {"topic_id": "t1", "version": "x"}},
    ):
        with pytest.raises(eq.EquipError) as caught:
            eq.plan_equip([], target, **kwargs)
        assert caught.value.status == 400, kwargs


def test_a_topic_item_takes_a_ring_while_there_is_room():
    plan = eq.plan_equip([worn("ring", "topic", version="r1")], benched(version="new"), scope="topic")

    assert plan == {"scope": "topic", "slot": "ring", "displaced": None}


def test_rings_are_capped_and_a_full_set_needs_a_ring_to_replace():
    rings = [worn("ring", "topic", version=f"r{n}") for n in range(eq.MAX_RINGS)]
    target = benched(version="new")

    with pytest.raises(eq.EquipError) as full:
        eq.plan_equip(rings, target, scope="topic")
    plan = eq.plan_equip(rings, target, scope="topic", replace={"topic_id": "t1", "version": "r2"})

    assert full.value.status == 409
    assert plan["displaced"]["version"] == "r2"


def test_replacing_something_that_is_not_a_worn_ring_is_a_400():
    rings = [worn("ring", "topic", version="r1")]

    with pytest.raises(eq.EquipError) as caught:
        eq.plan_equip(
            rings, benched(version="new"), scope="topic", replace={"topic_id": "t1", "version": "nope"}
        )

    assert caught.value.status == 400


# --- what gets injected -------------------------------------------------------------------


def test_nothing_worn_and_nothing_legacy_injects_nothing():
    assert eq.guidance_for("t1", []) == (None, [])
    assert eq.guidance_for("t1", [benched()]) == (None, [])
    assert eq.guidance_for("t1", [item(status="pending")]) == (None, [])


def test_one_piece_is_injected_verbatim():
    text, used = eq.guidance_for("t1", [worn("ring", "topic", text="Use tables.")])

    assert text == "Use tables."
    assert used == [{"topic_id": "t1", "version": "v1", "slot": "ring"}]


def test_armor_applies_to_every_topic_and_rings_only_to_their_own():
    items = [
        worn("helmet", "global", topic="origin", version="h", text="Be brief."),
        worn("ring", "topic", topic="t1", version="r1", text="Cite the repo."),
        worn("ring", "topic", topic="t2", version="r2", text="Mention the price."),
    ]

    text, used = eq.guidance_for("t1", items)

    assert text == "- Be brief.\n- Cite the repo."
    assert [u["slot"] for u in used] == ["helmet", "ring"]
    assert "price" not in text
    assert eq.guidance_for("t3", items)[0] == "Be brief."  # armor only, the rings are for other topics


def test_armor_comes_in_slot_order_not_the_order_worn():
    items = [
        worn("sword", "global", version="s", text="Sword.", at="2026-09-01"),
        worn("helmet", "global", version="h", text="Helmet.", at="2026-09-02"),
    ]

    assert eq.guidance_for("t1", items, TakeAll)[0] == "- Helmet.\n- Sword."


# --- the bear takes in only some of its armor ---------------------------------------------


def _armor(count):
    return [worn(slot, "global", version=slot, text=f"{slot} guidance.") for slot in eq.ARMOR_SLOTS[:count]]


def test_the_bear_takes_in_at_least_one_piece_and_never_more_than_it_wears():
    armor = _armor(4)

    sizes = {len(eq.pick_armor(armor, random.Random(seed))) for seed in range(200)}

    assert sizes == {1, 2, 3, 4}  # every count comes up; none is zero, none is over


def test_every_piece_gets_left_out_sometimes_and_taken_in_sometimes():
    armor = _armor(3)
    seen = [{i["slot"] for i in eq.pick_armor(armor, random.Random(seed))} for seed in range(200)]

    for slot in ("helmet", "chest", "gloves"):
        assert any(slot in taken for taken in seen) and any(slot not in taken for taken in seen)


def test_the_pieces_taken_come_back_in_slot_order():
    armor = list(reversed(_armor(5)))

    for seed in range(20):
        slots = [i["slot"] for i in eq.pick_armor(armor, random.Random(seed))]
        assert slots == sorted(slots, key=eq.ARMOR_SLOTS.index)


def test_no_armor_means_nothing_to_pick():
    assert eq.pick_armor([]) == []


def test_rings_are_always_used_but_armor_is_not():
    items = _armor(6) + [worn("ring", "topic", topic="t1", version="r", text="The ring.")]
    armor_counts = set()

    for seed in range(100):
        text, used = eq.guidance_for("t1", items, random.Random(seed))
        assert "The ring." in text  # every time
        assert used[-1]["slot"] == "ring"
        armor_counts.add(len(used) - 1)

    assert armor_counts == {1, 2, 3, 4, 5, 6}  # from one piece to all of it


def test_only_the_armor_taken_in_is_recorded_and_injected():
    text, used = eq.guidance_for("t1", _armor(6), random.Random(3))

    taken = {piece["slot"] for piece in used}
    assert taken < set(eq.ARMOR_SLOTS) or len(taken) == 6
    for slot in eq.ARMOR_SLOTS:
        assert (f"{slot} guidance." in text) == (slot in taken)


def test_a_benched_item_is_not_injected():
    items = [worn("helmet", "global", text="Worn."), benched(version="b", text="Benched.")]

    assert eq.guidance_for("t1", items)[0] == "Worn."


def test_a_legacy_approval_still_applies_until_the_topic_has_a_ring():
    old = item(version="2026-08-01", text="Old guidance.")
    newer = item(version="2026-09-01", text="Newer guidance.")

    text, used = eq.guidance_for("t1", [old, newer])

    assert text == "Newer guidance."  # the latest, as before equipment existed
    assert used == [{"topic_id": "t1", "version": "2026-09-01", "slot": "legacy"}]
    ring = worn("ring", "topic", version="r", text="A ring.")
    assert eq.guidance_for("t1", [old, newer, ring])[0] == "A ring."


def test_legacy_is_used_alongside_armor():
    text, _ = eq.guidance_for("t1", [worn("chest", "global", topic="x", text="Armor."), item(text="Legacy.")])

    assert text == "- Armor.\n- Legacy."


def test_guidance_is_capped_and_the_record_only_lists_what_was_used():
    long_text = "x" * (eq.MAX_GUIDANCE_CHARS - 10)
    items = [
        worn("helmet", "global", version="a", text=long_text),
        worn("chest", "global", version="b", text="This one does not fit."),
    ]

    text, used = eq.guidance_for("t1", items, TakeAll)

    assert text == long_text
    assert [u["version"] for u in used] == ["a"]


def test_an_empty_piece_is_ignored():
    text, used = eq.guidance_for(
        "t1",
        [worn("helmet", "global", text="  "), worn("chest", "global", text="Real.", version="c")],
        TakeAll,
    )

    assert text == "Real."
    assert [u["slot"] for u in used] == ["chest"]


# --- the loadout --------------------------------------------------------------------------


def test_describe_lays_out_the_slots_the_rings_and_the_backpack():
    items = [
        worn("helmet", "global", version="h"),
        worn("ring", "topic", version="r"),
        benched(version="b1"),
        benched(version="b2"),
        item(version="old"),
        item(version="p", status="pending"),
    ]

    view = eq.describe(items)

    assert view["armor"]["helmet"]["version"] == "h"
    assert view["armor"]["chest"] is None and list(view["armor"]) == list(eq.ARMOR_SLOTS)
    assert [r["version"] for r in view["rings"]] == ["r"]
    assert view["max_rings"] == eq.MAX_RINGS
    assert view["backpack_count"] == 2
    assert [i["version"] for i in view["legacy"]] == ["old"]
