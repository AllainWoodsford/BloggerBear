"""Equipment: approved prompt refinements, worn by the bear.

An approved refinement is a piece of guidance. Equipment decides which approved guidance is
actually *worn* -- and so injected into the ideation and drafting prompts:

- **Armor** (helmet, chest, gloves, boots, sword, shield) holds *global* guidance, applied to
  every topic. One item per slot; equipping into a full slot sends the occupant back to the
  backpack.
- **Rings** hold *topic* guidance, applied only to the topic the item was proposed for. Up to
  MAX_RINGS in all; when they are full, equipping another means naming the ring it replaces.
- The **backpack** is every approved refinement that is not worn. It is only a count anywhere
  public; nothing in it is injected.

This module is pure -- it decides and describes, and never touches DynamoDB -- so the rules are
easy to test. The refinement items themselves are plain PromptRefinements dicts; the fields this
adds are `equipped` (bool), `slot`, `scope` ("global" | "topic"), `equipped_at`, `unequipped_at`.

A refinement approved before equipment existed has no `equipped` field at all. It is *legacy*: it
keeps working exactly as it did (the latest one per topic is injected) until the topic has a ring
of its own. Once an item has been equipped or unequipped it has the field and is never legacy.
"""

from __future__ import annotations

ARMOR_SLOTS = ("helmet", "chest", "gloves", "boots", "sword", "shield")
RING_SLOT = "ring"
MAX_RINGS = 5
SCOPE_GLOBAL = "global"
SCOPE_TOPIC = "topic"
LEGACY_SLOT = "legacy"

# Worn guidance is folded into every prompt; keep the total from swamping them.
MAX_GUIDANCE_CHARS = 4000


class EquipError(Exception):
    """An equip request that cannot be done. `status` is the HTTP status the API answers with:
    400 for a request that makes no sense, 409 for one that conflicts with what is worn."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def ref(item: dict) -> dict:
    """The identity of a refinement, which is all a reference to one needs."""
    return {"topic_id": item["topic_id"], "version": item["version"]}


def is_equipped(item: dict) -> bool:
    return item.get("equipped") is True


def is_legacy(item: dict) -> bool:
    """An approved item the equipment system has never touched."""
    return item.get("status") == "approved" and "equipped" not in item


def equipped_items(items: list[dict]) -> list[dict]:
    return [item for item in items if item.get("status") == "approved" and is_equipped(item)]


def backpack(items: list[dict]) -> list[dict]:
    """Approved and not worn. Legacy items are not in the backpack: they are still in use."""
    return [i for i in items if i.get("status") == "approved" and i.get("equipped") is False]


def _slot_order(item: dict) -> tuple:
    slot = item.get("slot")
    rank = ARMOR_SLOTS.index(slot) if slot in ARMOR_SLOTS else len(ARMOR_SLOTS)
    return (rank, item.get("equipped_at") or "", item.get("version") or "")


def occupant(items: list[dict], slot: str) -> dict | None:
    """The worn item in an armor slot, or None. (Rings are many; see `rings`.)"""
    return next((i for i in equipped_items(items) if i.get("slot") == slot), None)


def rings(items: list[dict]) -> list[dict]:
    return sorted(
        (i for i in equipped_items(items) if i.get("slot") == RING_SLOT),
        key=lambda i: (i.get("equipped_at") or "", i.get("version") or ""),
    )


def free_armor_slots(items: list[dict]) -> list[str]:
    taken = {i.get("slot") for i in equipped_items(items)}
    return [slot for slot in ARMOR_SLOTS if slot not in taken]


def suggest_slot(items: list[dict], scope: str) -> str | None:
    """A sensible place for a new item: the first empty armor slot (global) or a ring (topic),
    or None when that kind is full and the caller must choose what to replace."""
    if scope == SCOPE_TOPIC:
        return RING_SLOT if len(rings(items)) < MAX_RINGS else None
    free = free_armor_slots(items)
    return free[0] if free else None


def plan_equip(
    items: list[dict],
    target: dict,
    *,
    scope: str,
    slot: str | None = None,
    replace: dict | None = None,
) -> dict:
    """Work out how to wear `target`, without changing anything.

    `items` is every refinement (only the worn ones matter). Returns
    {"scope", "slot", "displaced"}: where it goes, and the worn item it pushes out (or None).
    Raises EquipError when it cannot be done.
    """
    if scope not in (SCOPE_GLOBAL, SCOPE_TOPIC):
        raise EquipError(400, f"scope must be '{SCOPE_GLOBAL}' or '{SCOPE_TOPIC}'")
    worn = [i for i in equipped_items(items) if ref(i) != ref(target)]

    if scope == SCOPE_GLOBAL:
        if slot is None:
            slot = suggest_slot(worn, SCOPE_GLOBAL)
            if slot is None:
                raise EquipError(409, "every armor slot is worn: choose one to replace with a slot")
        if slot not in ARMOR_SLOTS:
            raise EquipError(400, f"global guidance goes in an armor slot: {', '.join(ARMOR_SLOTS)}")
        if replace is not None:
            raise EquipError(400, "replace is only for rings; naming the armor slot replaces its item")
        return {"scope": scope, "slot": slot, "displaced": occupant(worn, slot)}

    if slot not in (None, RING_SLOT):
        raise EquipError(400, "topic guidance goes in a ring")
    worn_rings = rings(worn)
    if replace is None:
        if len(worn_rings) >= MAX_RINGS:
            raise EquipError(
                409, f"all {MAX_RINGS} rings are worn: choose one to replace, or leave it in the backpack"
            )
        return {"scope": scope, "slot": RING_SLOT, "displaced": None}
    victim = next((r for r in worn_rings if ref(r) == _clean_ref(replace)), None)
    if victim is None:
        raise EquipError(400, "replace must be a ring that is currently worn")
    return {"scope": scope, "slot": RING_SLOT, "displaced": victim}


def _clean_ref(value) -> dict:
    if not isinstance(value, dict):
        return {}
    return {"topic_id": value.get("topic_id"), "version": value.get("version")}


def guidance_for(topic_id: str, items: list[dict]) -> tuple[str | None, list[dict]]:
    """The guidance to inject for a topic, and a record of what it came from.

    Worn armor (global) comes first in slot order, then the topic's rings, oldest first. One item
    is used verbatim; several become a bullet list. Returns (None, []) when nothing applies, and
    callers must then leave their prompts exactly as they were.

    The record is a list of {"topic_id", "version", "slot"}, one per piece actually used -- it is
    stored on the article so later analysis and wear can be tied to the gear that wrote it.
    """
    worn = equipped_items(items)
    chosen = sorted(
        [i for i in worn if i.get("scope") == SCOPE_GLOBAL and i.get("slot") in ARMOR_SLOTS], key=_slot_order
    )
    topic_rings = [i for i in rings(worn) if i.get("topic_id") == topic_id]
    chosen += topic_rings

    if not topic_rings:
        legacy = [i for i in items if is_legacy(i) and i.get("topic_id") == topic_id]
        if legacy:
            latest = max(legacy, key=lambda i: i["version"])
            chosen.append({**latest, "slot": LEGACY_SLOT})

    pieces = []
    total = 0
    for item in chosen:
        text = (item.get("prompt_changes") or "").strip()
        if not text:
            continue
        if pieces and total + len(text) > MAX_GUIDANCE_CHARS:
            continue
        pieces.append((item, text))
        total += len(text)

    if not pieces:
        return None, []
    used = [{**ref(item), "slot": item.get("slot")} for item, _ in pieces]
    if len(pieces) == 1:
        return pieces[0][1], used
    return "\n".join(f"- {text}" for _, text in pieces), used


def describe(items: list[dict]) -> dict:
    """The whole loadout, for the admin: what each slot holds, the rings, and the backpack."""
    approved = [i for i in items if i.get("status") == "approved"]
    armor = {slot: occupant(approved, slot) for slot in ARMOR_SLOTS}
    pack = backpack(approved)
    return {
        "armor": armor,
        "rings": rings(approved),
        "max_rings": MAX_RINGS,
        "backpack": sorted(pack, key=lambda i: i.get("version") or ""),
        "backpack_count": len(pack),
        "legacy": [i for i in approved if is_legacy(i)],
    }
