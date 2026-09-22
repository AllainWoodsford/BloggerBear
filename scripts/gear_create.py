"""Making gear for BloggerBear by hand: `admin_cli.py equipment create` and `equipment delete`.

Most gear comes from readers: the weekly reflection proposes a change and you approve it. This is the
other way in: you write the guidance yourself, and it becomes a piece of gear at once (approved, named,
given a rarity and durability) and, by default, is put on.

    equipment create                          # guided: it asks you what it needs
    equipment create --text "Lead with the most useful fact." --scope global --slot helmet
    equipment create --text "Name the repository." --topic-id github-trending --rarity epic
    equipment delete TOPIC VERSION            # take a piece away for good (asks first)

Guided mode is a short conversation. Every question has a sensible Enter: skip them all and the bear
names the gear, suggests where it goes, and rolls the rarity, exactly as it does for a reader's proposal.

Everything goes through the signed Admin API, like every other command. The functions here take the
API wrapper and the input/output as arguments so they are tested with no AWS and no terminal.
"""

from __future__ import annotations

import sys
from collections.abc import Callable

from review_inbox import Api, ApiError

RARITIES = ("common", "uncommon", "rare", "epic", "legendary")
ARMOR_SLOTS = ("helmet", "chest", "gloves", "boots", "sword", "shield")
MAX_TEXT = 1000


def _say(out, text: str = "") -> None:
    print(text, file=out, flush=True)


def _short(text, limit: int = 60) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _ask(ask: Callable[[str], str], prompt: str, default: str = "") -> str:
    """One line of input; an empty answer means `default`. End of input counts as the default too."""
    try:
        answer = ask(prompt)
    except EOFError:
        return default
    answer = (answer or "").strip()
    return answer or default


def _choose(ask, out, title: str, options: list[tuple[str, str]], default: str = "") -> str | None:
    """Show numbered `options` (value, label) and return the chosen value. Enter takes `default`
    (a value, or "" for none). Returns None if the person types q to back out."""
    _say(out, title)
    for number, (_value, label) in enumerate(options, start=1):
        _say(out, f"   {number}  {label}")
    while True:
        raw = _ask(ask, "  Choose a number (Enter for the default, q to cancel): ", "")
        if raw.lower() == "q":
            return None
        if raw == "":
            return default
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        _say(out, f"  Please type a number from 1 to {len(options)}.")


def guided_body(api: Api, ask: Callable[[str], str], out=None) -> dict | None:
    """Ask what a new piece of gear needs and return the request body, or None if cancelled."""
    out = out or sys.stdout
    _say(out, "Let's make something for BloggerBear to wear. Press Enter to accept any suggestion.")
    _say(out)

    text = ""
    while not text:
        try:
            text = (ask("What should it tell BloggerBear to do when writing? ") or "").strip()
        except EOFError:  # nothing more to read: there is no text to make gear from
            return None
        if text.lower() in ("q", "quit"):
            return None
        if len(text) > MAX_TEXT:
            _say(out, f"  That is {len(text)} characters; the limit is {MAX_TEXT}. Please shorten it.")
            text = ""
    body: dict = {"text": text}

    kind = _choose(
        ask,
        out,
        "\nWhere does it apply?",
        [
            ("armor", "Every topic (armor: helmet, chest, gloves, boots, sword or shield)"),
            ("ring", "One topic only (a ring; five at most)"),
        ],
        default="armor",
    )
    if kind is None:
        return None

    if kind == "ring":
        topics = api.get("/topics").get("topics") or []
        if not topics:
            _say(out, "There are no topics yet, so there is nothing for a ring to be tied to.")
            return None
        picked = _choose(
            ask,
            out,
            "\nWhich topic?",
            [(t["topic_id"], f"{t.get('name')} ({t['topic_id']})") for t in topics],
        )
        if not picked:
            return None
        body["topic_id"] = picked
    else:
        loadout = api.get("/equipment").get("armor") or {}
        options = [("", "Let the bear suggest one (an empty slot, by what the guidance is about)")]
        for slot in ARMOR_SLOTS:
            held = loadout.get(slot)
            state = "empty" if not held else f"worn: {_short(held.get('name'), 40)}  <- it would be replaced"
            options.append((slot, f"{slot.capitalize()}: {state}"))
        slot = _choose(ask, out, "\nWhich armor slot?", options, default="")
        if slot is None:
            return None
        if slot:
            body["slot"] = slot

    rarity = _choose(
        ask,
        out,
        "\nHow rare should it be?",
        [("", "Roll for it, like a drop (mostly common; sometimes better)")]
        + [(r, r.capitalize()) for r in RARITIES],
        default="",
    )
    if rarity is None:
        return None
    if rarity:
        body["rarity"] = rarity

    theme = _ask(
        ask, "\nWhat is it about, in two to four words (for its name)? Enter lets the bear name it: ", ""
    )
    if theme:
        body["theme"] = theme

    equip = _ask(ask, "\nPut it on straight away? [Y/n] ", "y").lower()
    body["equip"] = equip not in ("n", "no")

    _say(out, "\nHere is what will be made:")
    _say(out, f"   Guidance: {_short(text, 90)}")
    _say(
        out,
        f"   Kind:     {'a ring for ' + body['topic_id'] if 'topic_id' in body else 'armor for every topic'}",
    )
    _say(out, f"   Slot:     {body.get('slot') or 'suggested by the bear'}")
    _say(out, f"   Rarity:   {body.get('rarity') or 'rolled'}")
    _say(out, f"   Name:     {'... of ' + theme if theme else 'chosen by the bear'}")
    _say(out, f"   Worn now: {'yes' if body['equip'] else 'no, into the backpack'}")
    if _ask(ask, "\nMake it? [Y/n] ", "y").lower() in ("n", "no"):
        return None
    return body


def describe_created(response: dict) -> str:
    """One friendly line about what was made and where it went."""
    created = response.get("created") or {}
    item = created.get("item") or {}
    placement = created.get("placement") or {}
    wear = f"durability {item.get('durability')}/{item.get('max_durability')}"
    head = f"Made {item.get('name')} ({item.get('rarity')}, {wear})"
    if not placement.get("equipped"):
        return head + ", and put it in the backpack."
    slot = placement.get("slot")
    where = "as a ring for its topic" if slot == "ring" else f"in the {slot} slot"
    displaced = placement.get("displaced")
    replaced = f", replacing {displaced.get('topic_id')} {displaced.get('version')}" if displaced else ""
    return f"{head}, and BloggerBear is now wearing it {where}{replaced}."


def create(api: Api, body: dict | None, ask: Callable[[str], str], out=None) -> int:
    """Create gear. With `body` None, ask for it first. Returns a process exit code."""
    out = out or sys.stdout
    if body is None:
        body = guided_body(api, ask, out)
        if body is None:
            _say(out, "Cancelled: nothing was made.")
            return 1
    try:
        response = api.post("/equipment", body)
    except ApiError as exc:
        _say(out, f"! Could not make it: {exc.message}")
        return 1
    _say(out, describe_created(response))
    return 0


def delete(
    api: Api, topic_id: str, version: str, ask: Callable[[str], str], assume_yes: bool = False, out=None
) -> int:
    """Delete a piece of gear for good, after showing what it is and asking. Returns an exit code."""
    out = out or sys.stdout
    try:
        rows = api.get(f"/prompt-refinements?topic_id={topic_id}").get("refinements") or []
    except ApiError as exc:
        _say(out, f"! Could not look it up: {exc.message}")
        return 1
    row = next((r for r in rows if r.get("version") == version), None)
    if row is None:
        _say(out, f"There is no gear {topic_id} / {version}. (`equipment list` shows what exists.)")
        return 1
    worn = "worn right now" if row.get("equipped") is True else "not being worn"
    _say(out, f"{row.get('name')} ({row.get('rarity') or 'no rarity yet'}), {worn}.")
    _say(out, f"   {_short(row.get('prompt_changes'), 100)}")
    _say(
        out,
        "Deleting removes it for good. Articles already written with it keep their own record of it.",
    )
    if not assume_yes and _ask(ask, "Delete it? [y/N] ", "n").lower() not in ("y", "yes"):
        _say(out, "Kept: nothing was deleted.")
        return 1
    try:
        api.delete(f"/prompt-refinements/{topic_id}/{version}")
    except ApiError as exc:
        _say(out, f"! Could not delete it: {exc.message}")
        return 1
    _say(out, f"Deleted {row.get('name')}.")
    return 0
