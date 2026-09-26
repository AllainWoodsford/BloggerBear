"""Equipment: approved prompt refinements, worn by the bear.

An approved refinement is a piece of guidance. Equipment decides which approved guidance is
actually *worn* -- and so injected into the ideation and drafting prompts:

- **Armor** (helmet, chest, gloves, boots, sword, shield) holds *global* guidance, applied to
  every topic. One item per slot; equipping into a full slot sends the occupant back to the
  backpack.
- **Rings** hold *topic* guidance, applied only to the topic the item was proposed for. Up to
  MAX_RINGS in all; when they are full, equipping another means naming the ring it replaces.
- Wearing is not the same as using. Every ring for the topic is used in each article, but the bear
  takes in only some of its worn armor, chosen at random each time (`pick_armor`): anywhere from none
  of it to all of it, and never the exact same combination two articles running (it remembers the
  last draw -- common/dynamo.py's get_last_armor_versions/set_last_armor_versions -- and redraws
  rather than repeat it, so long as a genuinely different combination is possible). What was used is
  recorded on the article.
- The **backpack** is every approved refinement that is not worn. It is only a count anywhere
  public; nothing in it is injected.

This module is pure -- it decides and describes, and never touches DynamoDB -- so the rules are
easy to test. The refinement items themselves are plain PromptRefinements dicts; the fields this
adds are `equipped` (bool), `slot`, `scope` ("global" | "topic"), `equipped_at`, `unequipped_at`.

**Wear.** A stored downvote costs each piece of gear the article used 1 durability, and an upvote gives 1
back (never above the maximum); only gear that is being worn is affected (common/wear.py). Gear that reaches
0 is taken off (`worn_out`) and, if there is a suitable spare in the backpack, replaced. Only a spare that was
*parked* (approved when there was no room) may be put on automatically: anything an admin benched, took off,
or displaced stays where they left it, and a worn-out piece stays worn out until an admin repairs it.

A refinement approved before equipment existed has no `equipped` field at all. It is *legacy*: it
keeps working exactly as it did (the latest one per topic is injected) until the topic has a ring
of its own. Once an item has been equipped or unequipped it has the field and is never legacy.
"""

from __future__ import annotations

import random

ARMOR_SLOTS = ("helmet", "chest", "gloves", "boots", "sword", "shield")
RING_SLOT = "ring"
MAX_RINGS = 5
SCOPE_GLOBAL = "global"
SCOPE_TOPIC = "topic"
LEGACY_SLOT = "legacy"

# Armor an admin creates by hand is not about any one topic, but every refinement is keyed by a topic id.
# It is filed under this pseudo-topic (no real topic may use the id: see the admin API's create-topic).
GLOBAL_TOPIC_ID = "global"
# The longest guidance an admin can write for a piece of gear (it is injected into prompts).
MAX_GUIDANCE_LENGTH = 1000

# Why an item is in the backpack (`unequipped_reason`). Only PARKED may be put on again automatically.
PARKED = "parked"  # approved when there was no room for it
SHELVED = "shelved"  # approved straight to the backpack by choice
BENCHED = "benched"  # taken off by an admin
DISPLACED = "displaced"  # pushed out by something else being worn
WORN_OUT = "worn_out"  # its durability ran out

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


def suggest_slot(items: list[dict], scope: str, hint: str | None = None) -> str | None:
    """A sensible place for a new item: for armor (global) the slot the bear would pick (`hint`) if
    it is empty, else the first empty one; a ring (topic). None when that kind is full and the caller
    must choose what to replace."""
    if scope == SCOPE_TOPIC:
        return RING_SLOT if len(rings(items)) < MAX_RINGS else None
    free = free_armor_slots(items)
    if hint in free:
        return hint
    return free[0] if free else None


def durability_left(item: dict) -> bool:
    """True unless the item has a durability and it is used up. (No durability: it predates gear.)"""
    durability = item.get("durability")
    return durability is None or int(durability) > 0


def pick_replacement(items: list[dict], retired: dict) -> dict | None:
    """The spare that should take the place of a piece of gear that has just worn out, or None.

    Only parked spares qualify (see the module docstring), that still have durability, and are the
    same kind: a ring for the same topic, or armor for another armor slot. The one with the most
    durability left wins, the newest breaking ties.
    """
    ring = retired.get("slot") == RING_SLOT
    candidates = []
    for item in items:
        if item.get("status") != "approved" or item.get("equipped") is not False:
            continue
        if item.get("unequipped_reason") != PARKED or not durability_left(item) or "durability" not in item:
            continue
        if ref(item) == ref(retired):
            continue
        is_ring = item.get("scope") != SCOPE_GLOBAL
        if ring != is_ring or (ring and item.get("topic_id") != retired.get("topic_id")):
            continue
        candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=lambda i: (int(i["durability"]), i.get("version") or ""))


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
    if not durability_left(target):
        raise EquipError(409, "it is worn out: an admin has to repair it before it can be worn")
    worn = [i for i in equipped_items(items) if ref(i) != ref(target)]

    if scope == SCOPE_GLOBAL:
        if slot is None:
            slot = suggest_slot(worn, SCOPE_GLOBAL, target.get("slot_hint"))
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


MAX_ARMOR_REDRAWS = 8


def pick_armor(armor: list[dict], rng=None, avoid_versions: set[str] | None = None) -> list[dict]:
    """The armor the bear takes in for one article: a random number of the worn pieces --
    anywhere from none of it to all of it -- chosen at random, returned in slot order. Rings are
    not part of this: they always apply.

    `avoid_versions`, if given, is the exact set of versions drawn last time (see
    common/dynamo.py's get_last_armor_versions) -- redrawn (up to MAX_ARMOR_REDRAWS times) rather
    than repeat that same combination back to back, as long as a genuinely different one is
    possible. With only one piece of armor equipped (or none), "no armor" and "that one piece"
    are the only two outcomes there ever are; a redraw still eventually lands on whichever of the
    two wasn't used last time. The last attempt is kept even if it still matches -- true only when
    every possible combination has already been ruled out, which never happens with more than
    one piece equipped.
    """
    if not armor:
        return []
    rng = rng or random
    chosen: list[dict] = []
    for _ in range(MAX_ARMOR_REDRAWS):
        chosen = rng.sample(armor, rng.randint(0, len(armor)))
        if avoid_versions is None or {item["version"] for item in chosen} != avoid_versions:
            break
    return sorted(chosen, key=_slot_order)


def guidance_for(
    topic_id: str, items: list[dict], rng=None, avoid_armor_versions: set[str] | None = None
) -> tuple[str | None, list[dict]]:
    """The guidance to inject for a topic, and a record of what it came from.

    Some of the worn armor (global; see `pick_armor`, in slot order) comes first, then all of the
    topic's rings, oldest first. One item is used verbatim; several become a bullet list. Returns
    (None, []) when nothing applies, and callers must then leave their prompts exactly as they were.
    `rng` is for tests; it needs `randint` and `sample`. `avoid_armor_versions` is `pick_armor`'s
    own -- the exact armor combination drawn last time, so this one avoids repeating it.

    The record is a list of {"topic_id", "version", "slot"}, one per piece actually used -- it is
    stored on the article so later analysis and wear can be tied to the gear that wrote it, and so
    the caller can pick the armor entries back out of it to record for the *next* call's own
    `avoid_armor_versions`.
    """
    worn = equipped_items(items)
    chosen = pick_armor(
        [i for i in worn if i.get("scope") == SCOPE_GLOBAL and i.get("slot") in ARMOR_SLOTS],
        rng,
        avoid_versions=avoid_armor_versions,
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


def describe(items: list[dict], decorate=None) -> dict:
    """The whole loadout, for the admin: what each slot holds, the rings, and the backpack.
    `decorate`, if given, is applied to every item shown (to add a name, say)."""
    decorate = decorate or (lambda item: item)
    approved = [i for i in items if i.get("status") == "approved"]
    pack = backpack(approved)
    held = occupant
    return {
        "armor": {slot: (decorate(i) if (i := held(approved, slot)) else None) for slot in ARMOR_SLOTS},
        "rings": [decorate(i) for i in rings(approved)],
        "max_rings": MAX_RINGS,
        "backpack": [decorate(i) for i in sorted(pack, key=lambda i: i.get("version") or "")],
        "backpack_count": len(pack),
        "legacy": [decorate(i) for i in approved if is_legacy(i)],
    }
