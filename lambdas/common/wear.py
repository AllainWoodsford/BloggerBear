"""Wear: what reader feedback does to the gear an article was written with.

A stored downvote costs each piece of gear the article used one point of durability; a stored upvote
gives one point back, never above the gear's maximum. Durability is how gear's record shows: gear that
keeps being written into well-received articles stays sharp, and gear that isn't wears out and is
taken off.

What counts is exactly what the feedback path stores: a submission is turned away (and never counted)
if its comment is screened out, its token is bad, or a limit is hit, so only feedback that was kept
ever reaches here.

- Only the gear the article actually *used* is touched (`Articles.equipment_used`: the topic's rings and
  the armor the bear took in that time), so gear the bear left out is neither blamed nor rewarded.
- Only gear that is being *worn* now is touched. A piece that was taken off or has worn out is not
  revived by an upvote on some old article; only an admin repairs it.
- When a piece reaches 0 it is taken off (`worn_out`). If a suitable spare is waiting in the backpack it
  takes the piece's place (common/equipment.py, `pick_replacement`); otherwise the slot stays empty.
- Gear that predates durability (a "legacy" piece) has none, and is left alone.

This never raises into the feedback path: a failure here is logged and the feedback is still recorded.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common import equipment
from common.dynamo import (
    apply_prompt_refinement_wear,
    list_prompt_refinements,
    set_prompt_refinement_equipment,
)

DAMAGE = -1
REPAIR = 1


def apply_feedback(article: dict, vote: str) -> list[dict]:
    """Wear the gear `article` used for one stored `vote` ("up" or "down"). Never raises.

    Returns what changed, for logging and tests: one entry per piece touched, with its new durability,
    whether it wore out, and what (if anything) replaced it.
    """
    delta = DAMAGE if vote == "down" else REPAIR
    changes = []
    seen = set()
    try:
        for piece in article.get("equipment_used") or []:
            if not isinstance(piece, dict):
                continue
            key = (piece.get("topic_id"), piece.get("version"))
            if None in key or key in seen or piece.get("slot") == equipment.LEGACY_SLOT:
                continue
            seen.add(key)
            try:
                change = _wear_one(piece, delta)
            except Exception as exc:  # noqa: BLE001 - one piece failing must not stop the others
                print(f"wear: could not wear {key}: {exc!r}")
                continue
            if change is not None:
                changes.append(change)
    except Exception as exc:  # noqa: BLE001 - never raise into the feedback path
        print(f"wear: could not apply feedback wear: {exc!r}")
    return changes


def _wear_one(piece: dict, delta: int) -> dict | None:
    result = apply_prompt_refinement_wear(piece["topic_id"], piece["version"], delta)
    if result is None:
        return None
    change = {
        "topic_id": piece["topic_id"],
        "version": piece["version"],
        "slot": piece.get("slot"),
        "durability": result["durability"],
        "max_durability": result["max_durability"],
        "worn_out": False,
        "replaced_by": None,
    }
    if delta < 0 and result["durability"] <= 0:
        change["worn_out"] = True
        change["replaced_by"] = _retire_and_replace(piece)
    return change


def _retire_and_replace(piece: dict) -> dict | None:
    """Take a worn-out piece off, then put the best parked spare in its place. Returns the spare's
    reference, or None if nothing took its place."""
    now = datetime.now(UTC).isoformat()
    set_prompt_refinement_equipment(
        piece["topic_id"], piece["version"], equipped=False, at=now, reason=equipment.WORN_OUT
    )
    print(f"wear: {piece['topic_id']} {piece['version']} wore out and was taken off")
    items = list_prompt_refinements(status="approved")
    retired = next((i for i in items if equipment.ref(i) == equipment.ref(piece)), None)
    if retired is None:
        return None
    # Taken off just now, so the slot is free; look at what remains worn and what is parked.
    spare = equipment.pick_replacement(items, {**retired, "slot": piece.get("slot")})
    if spare is None:
        return None
    scope = spare.get("scope") or equipment.SCOPE_TOPIC
    slot = piece.get("slot") if piece.get("slot") in equipment.ARMOR_SLOTS else None
    try:
        plan = equipment.plan_equip(items, spare, scope=scope, slot=slot)
    except equipment.EquipError as exc:
        print(f"wear: no room to put a spare in: {exc.message}")
        return None
    if plan["displaced"] is not None:  # a spare never pushes something else out
        return None
    set_prompt_refinement_equipment(
        spare["topic_id"], spare["version"], equipped=True, at=now, slot=plan["slot"], scope=plan["scope"]
    )
    print(f"wear: {spare['topic_id']} {spare['version']} took its place")
    return equipment.ref(spare)
