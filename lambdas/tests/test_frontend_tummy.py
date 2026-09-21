"""Tests for frontend/tummy.js, the bear-tummy toy shown around feedback. Run under Node.

The rules (how many scratches make it purr, how mouse movement turns into scratches, how often the
thank-you offers it) are pure and tested directly. mount() builds the card with plain DOM calls, so it
is driven here with a tiny fake DOM: what matters is that a click or a rub scratches, that the words
in the live region change only at a few points, that a finger does not rub, and that it is plain
decoration.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
TUMMY_JS = FRONTEND / "tummy.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_RUNNER = """
const tummy = require(process.argv[1]);
const scenario = JSON.parse(require("fs").readFileSync(0, "utf8"));

// A tiny fake DOM: enough for mount().
const doc = {};
function makeEl(tag) {
  const el = {
    tag, attrs: {}, children: [], listeners: {}, className: "", offsetWidth: 0, ownerDocument: doc,
    _text: "", texts: [],
    _classes: new Set(),
    classList: null,
    set textContent(v) { this._text = v; this.texts.push(v); },
    get textContent() { return this._text; },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); return c; },
    addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); },
    fire(t, ev) { (this.listeners[t] || []).forEach((f) => f(ev || {})); },
  };
  el.classList = {
    add: (c) => el._classes.add(c),
    remove: (c) => el._classes.delete(c),
    contains: (c) => el._classes.has(c),
  };
  return el;
}
doc.createElement = makeEl;

function find(el, tag) {
  if (el.tag === tag) return el;
  for (const c of el.children) { const f = find(c, tag); if (f) return f; }
  return null;
}

function run(steps) {
  const parent = makeEl("div");
  const card = tummy.mount(parent);
  const button = find(card, "button");
  const img = find(card, "img");
  const note = find(card, "p");
  const out = { mountedInParent: parent.children[0] === card, cardClass: card.className };
  out.button = { type: button.type, label: button.attrs["aria-label"] };
  out.img = { alt: img.attrs.alt, src: img.attrs.src, draggable: img.attrs.draggable };
  out.note = { role: note.attrs.role, live: note.attrs["aria-live"], text: note._text };
  out.snapshots = [];
  for (const step of steps) {
    if (step.click) for (let i = 0; i < step.click; i++) button.fire("click");
    if (step.down) button.fire("pointerdown", step.down);
    if (step.move) for (const ev of step.move) button.fire("pointermove", ev);
    if (step.up) button.fire("pointerup", {});
    if (step.leave) button.fire("pointerleave", {});
    out.snapshots.push({
      src: img.attrs.src,
      text: note._text,
      wiggle: card.classList.contains("tummy-wiggle"),
    });
  }
  out.textChanges = note.texts.length;
  return out;
}

let result;
if (scenario.mount) {
  result = run(scenario.mount);
} else if (scenario.game) {
  const g = tummy.game();
  result = scenario.game.map((step) => {
    if (step.scratch !== undefined) return g.scratch(step.scratch);
    if (step.rub !== undefined) return g.rub(step.rub);
    return g.state();
  });
} else if (scenario.offer) {
  result = scenario.offer.map(
    ([roll, chance]) => tummy.shouldOffer(roll, chance === null ? undefined : chance)
  );
} else {
  result = { words: tummy.WORDS, purrAt: tummy.PURR_AT, chance: tummy.OFFER_CHANCE };
}
process.stdout.write(JSON.stringify(result));
setTimeout(() => process.exit(0), 0);
"""


def run(scenario: dict):
    result = subprocess.run(
        [NODE, "-e", _RUNNER, str(TUMMY_JS)],
        input=json.dumps(scenario),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=True,
    )
    return json.loads(result.stdout)


# --- the rules ---------------------------------------------------------------------------------------


def test_the_bear_purrs_at_five_scratches_and_swaps_to_its_happy_picture():
    states = run({"game": [{"scratch": 1}] * 6})

    assert [s["purring"] for s in states] == [False, False, False, False, True, True]
    assert [s["image"] for s in states][:4] == ["bears/tummy.svg"] * 4
    assert [s["image"] for s in states][4:] == ["bears/tummy-happy.svg"] * 2


def test_the_words_change_only_at_a_few_stages_so_a_screen_reader_is_not_chatty():
    states = run({"game": [{"scratch": 1}] * 13})

    stages = [s["stage"] for s in states]
    assert stages == [1, 1, 2, 2, 3, 3, 3, 3, 3, 3, 3, 4, 4]
    words = {s["stage"]: s["words"] for s in states}
    assert words[1] == "Hehe, that tickles."
    assert words[3] == "BloggerBear purrs. Thank you!"
    assert len({s["words"] for s in states}) == 4  # four different lines in thirteen scratches


def test_before_any_scratch_it_is_the_invitation():
    (state,) = run({"game": [{"state": True}]})

    assert state["count"] == 0 and state["stage"] == 0 and not state["purring"]
    assert state["words"] == "Scratch BloggerBear's tummy"
    assert state["image"] == "bears/tummy.svg"


def test_rubbing_turns_mouse_movement_into_scratches():
    states = run({"game": [{"rub": 30}, {"rub": 30}, {"rub": 30}, {"rub": 40}]})

    # 40px is one scratch and the remainder carries over: 30, 60, 90, 130 pixels.
    assert [s["count"] for s in states] == [0, 1, 2, 3]


def test_one_huge_jump_of_the_mouse_is_capped():
    (state,) = run({"game": [{"rub": 100000}]})

    assert state["count"] == 2  # capped to one 80px step, not thousands of scratches


@pytest.mark.parametrize("junk", [0, -5, None, "far"])
def test_junk_rubbing_does_nothing(junk):
    (state,) = run({"game": [{"rub": junk}]})

    assert state["count"] == 0


@pytest.mark.parametrize("times", [0, -3, None, "many", 2.7])
def test_scratch_takes_a_whole_positive_number_or_counts_one(times):
    (state,) = run({"game": [{"scratch": times}]})

    assert state["count"] == (2 if times == 2.7 else 1)


def test_the_thank_you_offers_the_toy_about_one_time_in_three():
    rolls = [[0.0, None], [0.32, None], [0.34, None], [0.99, None]]

    assert run({"offer": rolls}) == [True, True, False, False]
    assert run({"offer": [[0.5, 1], [0.5, 0], [0.0, 0]]}) == [True, False, False]


@pytest.mark.parametrize("roll", [None, "0.1", -0.1, 1.5, True, [0.1], {"a": 1}])
def test_a_junk_or_out_of_range_roll_never_offers_it(roll):
    # Even with the odds at "always", only a real number from 0 up to 1 can win.
    assert run({"offer": [[roll, 1]]}) == [False]


# --- mount() -------------------------------------------------------------------------------------------


def test_the_card_is_a_button_with_a_name_a_decorative_picture_and_a_polite_status():
    out = run({"mount": []})

    assert out["mountedInParent"] is True and out["cardClass"] == "tummy-game"
    assert out["button"] == {"type": "button", "label": "Scratch BloggerBear's tummy"}
    assert out["img"]["alt"] == "" and out["img"]["src"] == "bears/tummy.svg"
    assert out["img"]["draggable"] == "false"
    assert out["note"] == {
        "role": "status",
        "live": "polite",
        "text": "Scratch BloggerBear's tummy",
    }


def test_clicking_scratches_and_the_bear_purrs_after_five():
    out = run({"mount": [{"click": 1}, {"click": 2}, {"click": 2}]})

    first, third, fifth = out["snapshots"]
    assert first["text"] == "Hehe, that tickles." and first["src"] == "bears/tummy.svg"
    assert third["text"] == "Mmm, a little more..."
    assert fifth["text"] == "BloggerBear purrs. Thank you!" and fifth["src"] == "bears/tummy-happy.svg"


def test_the_live_region_is_only_rewritten_when_the_stage_changes():
    out = run({"mount": [{"click": 1}] * 8})

    # The invitation, then three changes (1, 3 and 5 scratches): not one write per click.
    assert out["textChanges"] == 4


def test_each_scratch_gives_a_wiggle_class_for_the_stylesheet_to_animate_or_ignore():
    out = run({"mount": [{"click": 1}]})

    assert out["snapshots"][0]["wiggle"] is True


def test_rubbing_with_a_mouse_scratches_only_while_the_button_is_held():
    held = [{"clientX": 40 * n, "clientY": 0, "buttons": 1} for n in range(1, 5)]
    hovered = [{"clientX": 200 + 40 * n, "clientY": 0, "buttons": 0} for n in range(1, 5)]
    out = run(
        {
            "mount": [
                {"down": {"pointerType": "mouse", "clientX": 0, "clientY": 0}, "move": held},
                {"up": True, "down": {"pointerType": "mouse", "clientX": 200, "clientY": 0}, "move": hovered},
            ]
        }
    )

    after_held, after_hover = out["snapshots"]
    assert after_held["text"] == "Mmm, a little more..."  # four 40px moves: four scratches
    assert after_hover["text"] == after_held["text"]  # not held: nothing more counted


def test_a_finger_does_not_rub_so_the_page_can_still_scroll():
    moves = [{"clientX": 40 * n, "clientY": 0, "buttons": 1} for n in range(1, 8)]
    out = run(
        {"mount": [{"down": {"pointerType": "touch", "clientX": 0, "clientY": 0}, "move": moves}]}
    )

    assert out["snapshots"][0]["text"] == "Scratch BloggerBear's tummy"  # nothing happened


def test_leaving_the_bear_stops_the_rub():
    moves = [{"clientX": 40 * n, "clientY": 0, "buttons": 1} for n in range(1, 3)]
    out = run(
        {
            "mount": [
                {"down": {"pointerType": "mouse", "clientX": 0, "clientY": 0}, "leave": True},
                {"move": moves},
            ]
        }
    )

    assert out["snapshots"][1]["text"] == "Scratch BloggerBear's tummy"


# --- the pictures, and that it is only decoration -------------------------------------------------------


@pytest.mark.parametrize("name", ["tummy.svg", "tummy-happy.svg"])
def test_the_tummy_pictures_are_small_square_safe_svgs(name):
    text = (FRONTEND / "bears" / name).read_text(encoding="utf-8")
    root = ET.fromstring(text)

    assert root.get("viewBox") == "0 0 64 64"
    assert len(text.encode()) < 8_000
    assert not re.search(r"<script|<foreignObject|<iframe|<image|javascript:|href", text, re.I)


def test_the_toy_and_its_pictures_are_served_by_both_environments():
    for env in ("dev", "production"):
        terraform = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
        for served in ("tummy.js", "bears/tummy.svg", "bears/tummy-happy.svg"):
            assert f'"{served}"' in terraform, f"{env} does not serve {served}"


def test_the_pages_load_tummy_js_before_the_code_that_uses_it():
    index = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert index.index('src="tummy.js"') < index.index('src="app.js"')


def test_nothing_about_playing_is_sent_or_stored():
    source = TUMMY_JS.read_text(encoding="utf-8")

    for forbidden in ("fetch(", "XMLHttpRequest", "sendBeacon", "localStorage", "sessionStorage", "cookie"):
        assert forbidden not in source, forbidden


def test_it_can_never_break_feedback():
    for name in ("app.js", "article-widgets.js"):
        source = (FRONTEND / name).read_text(encoding="utf-8")
        # Every use of the toy is guarded and wrapped, so a problem in it is not a feedback error.
        assert source.count("window.BloggerTummy.mount(") == 2, name
        assert source.count("decoration only") >= 2, name
        assert '"unavailable"' in source, name  # no toy on "the site is simply broken"


def test_the_wiggle_only_exists_for_readers_who_have_not_asked_for_reduced_motion():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    animated = css.index("animation: tummy-wiggle")
    block_start = css.rindex("@media", 0, animated)
    assert css[block_start:animated].startswith("@media (prefers-reduced-motion: no-preference)")
    # ...and nothing else in the stylesheet animates it.
    assert css.count("tummy-wiggle") == 3  # the selector, the animation, the keyframes


def test_the_toy_is_a_button_so_the_keyboard_can_play_and_it_is_never_a_modal():
    source = TUMMY_JS.read_text(encoding="utf-8")
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")

    assert 'createElement("button")' in source and "aria-live" in source
    for modal in ("aria-modal", 'role", "dialog"', "position: fixed", "focus()"):
        assert modal not in source and modal not in css.split(".tummy-game")[1].split(".feedback-closed")[0]
    assert ".tummy-button:focus-visible" in css  # a visible focus ring for the keyboard
