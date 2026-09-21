"""BloggerBear's gear on the Stats page (frontend/gear.js, styles.css, app.js).

gear.js is pure, so it runs under Node here. The contract tests keep it in step with the backend
(rarities, slots), keep the pictures to plain data the site's CSP allows, and hold the colours to the
game convention (orange legendary, purple epic, blue rare, green uncommon, grey common) with enough
contrast to read.
"""

from __future__ import annotations

import colorsys
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common import equipment, gear

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
GEAR_JS = FRONTEND / "gear.js"
STYLES = (FRONTEND / "styles.css").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")
NODE = shutil.which("node")

needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


def _run(expression: str):
    """Evaluate a JS expression with the module as `g`, and return its JSON."""
    script = f"const g=require(process.argv[1]);process.stdout.write(JSON.stringify({expression}))"
    result = subprocess.run(
        [NODE, "-e", script, str(GEAR_JS)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


# --- in step with the backend -----------------------------------------------------------------


@needs_node
def test_the_front_end_knows_exactly_the_backends_rarities_and_slots():
    assert _run("g.RARITIES") == list(gear.RARITIES)
    assert _run("g.ARMOR_SLOTS") == list(equipment.ARMOR_SLOTS)
    assert set(_run("Object.keys(g.SLOT_LABELS)")) == set(equipment.ARMOR_SLOTS) | {equipment.RING_SLOT}
    assert set(_run("Object.keys(g.RARITY_LABELS)")) == set(gear.RARITIES)


@needs_node
def test_every_slot_has_a_picture_of_its_own():
    shapes = {
        slot: json.dumps(_run(f"g.iconSpec('{slot}', null).nodes"))
        for slot in [*equipment.ARMOR_SLOTS, equipment.RING_SLOT]
    }

    assert len(set(shapes.values())) == len(shapes)


# --- condition ---------------------------------------------------------------------------------


@needs_node
@pytest.mark.parametrize(
    "percent, tier, text",
    [
        (100, "good", "100%"),
        (75, "good", "75%"),
        (74, "fair", "74%"),
        (50, "fair", "50%"),
        (49, "worn", "49%"),
        (25, "worn", "25%"),
        (24, "critical", "24%"),
        (1, "critical", "1%"),
        (0, "critical", "0%"),
        (74.6, "good", "75%"),  # rounded first
        (150, "good", "100%"),
        (-5, "critical", "0%"),
    ],
)
def test_the_lower_the_durability_the_worse_the_tier(percent, tier, text):
    result = _run(f"g.durability({percent})")

    assert (result["tier"], result["text"]) == (tier, text)


@needs_node
@pytest.mark.parametrize("value", ["null", "undefined", "'80'", "NaN", "Infinity", "{}"])
def test_a_condition_that_is_not_a_number_is_unknown_not_a_guess(value):
    result = _run(f"g.durability({value})")

    assert result["percent"] is None and result["tier"] == "unknown" and result["text"] == "n/a"


@needs_node
def test_the_tiers_are_labelled_in_words_so_colour_is_never_the_only_signal():
    labels = [tier["label"] for tier in _run("g.TIERS")]

    assert labels == ["Good condition", "Showing wear", "Badly worn", "About to break"]


# --- describe ----------------------------------------------------------------------------------


@needs_node
def test_a_piece_is_described_for_the_page_and_for_a_screen_reader():
    info = _run(
        "g.describe({name:'Helm of Plain Speaking',rarity:'epic',slot:'helmet',"
        "description:'Be plain.',topic_name:null,durability_percent:88},'helmet')"
    )

    assert info["name"] == "Helm of Plain Speaking" and info["rarityLabel"] == "Epic"
    assert info["slotLabel"] == "Helmet" and info["scope"] == "Every topic"
    assert info["durability"]["tier"] == "good"
    assert info["accessibleName"] == "Helmet: Helm of Plain Speaking, Epic, durability 88%"


@needs_node
def test_a_ring_says_which_topic_it_is_tied_to():
    info = _run(
        "g.describe({name:'Ring of Repo Focus',rarity:'rare',slot:'ring',description:'x',"
        "topic_name:'GitHub Trending',durability_percent:40},'ring')"
    )

    assert info["topic"] == "GitHub Trending" and info["scope"] == "Topic: GitHub Trending"


@needs_node
def test_an_unknown_rarity_is_shown_as_common():
    for rarity in ("'mythic'", "null", "42", "'LEGENDARY '"):
        info = _run(f"g.describe({{name:'x',rarity:{rarity},durability_percent:50}},'ring')")
        assert info["rarity"] in {"common", "legendary"}
    assert _run("g.describe({rarity:'mythic'},'ring').rarity") == "common"
    assert _run("g.describe({rarity:' Legendary '},'ring').rarity") == "legendary"


@needs_node
def test_a_piece_with_nothing_in_it_still_describes_without_throwing():
    for bad in ("null", "undefined", "42", "'text'", "[]", "{}"):
        info = _run(f"g.describe({bad}, 'helmet')")
        assert info["name"] == "Unnamed gear" and info["rarity"] == "common"
        assert info["description"] == "No description." and info["slotLabel"] == "Helmet"


@needs_node
def test_long_text_is_cut_and_markup_is_left_as_text():
    info = _run(
        "g.describe({name:'x'.repeat(500),description:'<img src=x onerror=alert(1)> '+'y'.repeat(900),"
        "rarity:'rare',durability_percent:10},'sword')"
    )

    assert len(info["name"]) <= 80 and info["name"].endswith("…")
    assert len(info["description"]) <= 400
    assert info["description"].startswith("<img")  # kept as text: app.js only ever uses textContent


@needs_node
@pytest.mark.parametrize(
    "count, text",
    [
        (0, "The backpack is empty"),
        (1, "1 item in the backpack"),
        (2, "2 items in the backpack"),
        (14, "14 items in the backpack"),
        (2.9, "2 items in the backpack"),
        (-3, "The backpack is empty"),
        ("'5'", "The backpack is empty"),
        ("null", "The backpack is empty"),
        ("NaN", "The backpack is empty"),
    ],
)
def test_the_backpack_is_only_ever_a_count(count, text):
    assert _run(f"g.backpackText({count})") == text


# --- the pictures ---------------------------------------------------------------------------------

ALLOWED_TAGS = {"path", "circle", "rect"}
ALLOWED_ATTRS = {"class", "d", "cx", "cy", "r", "x", "y", "width", "height", "rx"}


def _all_specs():
    slots = [*equipment.ARMOR_SLOTS, equipment.RING_SLOT]
    return _run(
        "Object.fromEntries("
        f"{json.dumps(slots)}.flatMap(s=>[[s+'/empty',g.iconSpec(s,null)],"
        f"...{json.dumps(list(gear.RARITIES))}.map(r=>[s+'/'+r,g.iconSpec(s,r)])]))"
    )


@needs_node
def test_every_slot_and_rarity_has_a_picture_made_only_of_plain_shapes():
    specs = _all_specs()

    assert len(specs) == 7 * (5 + 1)
    for name, spec in specs.items():
        assert spec["viewBox"] == "0 0 48 48" and spec["nodes"], name
        for node in spec["nodes"]:
            assert node["tag"] in ALLOWED_TAGS, (name, node)
            assert set(node["attrs"]) <= ALLOWED_ATTRS, (name, node)  # no style, no on*, no href
            assert isinstance(node["attrs"]["class"], str)


@needs_node
def test_the_bag_is_plain_shapes_too():
    bag = _run("g.BAG")

    assert bag["viewBox"] == "0 0 24 24"
    for node in bag["nodes"]:
        assert node["tag"] in ALLOWED_TAGS and set(node["attrs"]) <= ALLOWED_ATTRS


@needs_node
def test_every_class_a_picture_uses_is_styled():
    used = {node["attrs"]["class"] for spec in _all_specs().values() for node in spec["nodes"]}
    used |= {node["attrs"]["class"] for node in _run("g.BAG")["nodes"]}

    for name in used:
        assert re.search(rf"\.{re.escape(name)}\b", STYLES), f"no style for .{name}"


@needs_node
def test_the_better_the_gear_the_more_it_shines():
    for slot in [*equipment.ARMOR_SLOTS, equipment.RING_SLOT]:
        counts = [len(_run(f"g.iconSpec('{slot}','{rarity}').nodes")) for rarity in gear.RARITIES]
        assert counts[0] == len(_run(f"g.iconSpec('{slot}',null).nodes"))  # common is the plain shape
        assert counts == sorted(counts), (slot, counts)
        assert counts[0] < counts[-1]
        distinct = {json.dumps(_run(f"g.iconSpec('{slot}','{rarity}').nodes")) for rarity in gear.RARITIES}
        assert len(distinct) == len(gear.RARITIES), f"{slot}: two rarities look the same"


@needs_node
def test_a_slot_it_does_not_know_still_draws_something():
    assert _run("g.iconSpec('hat','rare').nodes.length") > 0


@needs_node
def test_all_numbers_in_the_pictures_are_finite_and_on_the_canvas():
    for name, spec in _all_specs().items():
        for node in spec["nodes"]:
            for key in ("cx", "cy", "r", "x", "y", "width", "height", "rx"):
                if key in node["attrs"]:
                    value = node["attrs"][key]
                    assert isinstance(value, int | float) and 0 <= value <= 48, (name, node)


# --- colours: the game convention, with contrast --------------------------------------------------


def _token(block: str, name: str) -> str:
    match = re.search(rf"--{re.escape(name)}:\s*(#[0-9a-fA-F]{{6}})", block)
    assert match, f"--{name} not found"
    return match.group(1)


def _blocks() -> dict:
    """The light tokens, the dark-mode tokens, and the tooltip's own tokens, from styles.css."""
    light = STYLES[STYLES.index("--rarity-common: #6b6b6b") - 40 :]
    light = light[: light.index("}")]
    dark_start = STYLES.index("--rarity-common: #b0b0b0")
    dark = STYLES[STYLES.rfind(":root", 0, dark_start) : STYLES.index("}", dark_start)]
    tip_start = STYLES.index(".gear-tip {")
    tip = STYLES[tip_start : STYLES.index("}", tip_start)]
    return {"light": light, "dark": dark, "tooltip": tip}


def _hue_saturation(hex_colour: str) -> tuple[float, float]:
    red, green, blue = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    hue, _light, saturation = (
        colorsys.rgb_to_hls(red, green, blue)[0],
        0,
        colorsys.rgb_to_hls(red, green, blue)[2],
    )
    return hue * 360, saturation


def _luminance(hex_colour: str) -> float:
    def channel(value: int) -> float:
        v = value / 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    red, green, blue = (channel(int(hex_colour[i : i + 2], 16)) for i in (1, 3, 5))
    return 0.2126 * red + 0.7152 * green + 0.0722 * blue


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


@pytest.mark.parametrize("mode", ["light", "dark", "tooltip"])
def test_the_rarity_colours_are_the_game_convention(mode):
    block = _blocks()[mode]
    hue = {rarity: _hue_saturation(_token(block, f"rarity-{rarity}")) for rarity in gear.RARITIES}

    assert 15 <= hue["legendary"][0] <= 45, "legendary should be orange"
    assert 255 <= hue["epic"][0] <= 290, "epic should be purple"
    assert 195 <= hue["rare"][0] <= 225, "rare should be blue"
    assert 100 <= hue["uncommon"][0] <= 150, "uncommon should be green"
    assert hue["common"][1] < 0.05, "common should be grey"


@pytest.mark.parametrize(
    "mode, background, minimum",
    [("light", "#ffffff", 4.5), ("dark", "#16181c", 4.5), ("tooltip", "#12141a", 4.5)],
)
def test_rarity_and_condition_colours_can_be_read_where_they_are_used(mode, background, minimum):
    block = _blocks()[mode]
    names = [f"rarity-{r}" for r in gear.RARITIES] + [
        f"dur-{t}" for t in ("good", "fair", "worn", "critical")
    ]

    for name in names:
        ratio = _contrast(_token(block, name), background)
        assert ratio >= minimum, f"--{name} on {background} in {mode} mode is only {ratio:.2f}:1"


def test_the_condition_colours_run_green_yellow_orange_red():
    hues = {
        tier: _hue_saturation(_token(_blocks()["tooltip"], f"dur-{tier}"))[0]
        for tier in ("good", "fair", "worn", "critical")
    }

    assert 100 <= hues["good"] <= 150 and 40 <= hues["fair"] <= 60
    assert 15 <= hues["worn"] <= 35 and (hues["critical"] <= 10 or hues["critical"] >= 350)


def test_every_rarity_and_tier_the_script_can_produce_has_a_class():
    for rarity in gear.RARITIES:
        assert f".rarity-{rarity} {{" in STYLES
    for tier in ("good", "fair", "worn", "critical", "unknown"):
        assert f".dur-{tier} {{" in STYLES


# --- the page ---------------------------------------------------------------------------------------


def _gear_section_of_app() -> str:
    start = APP_JS.index("// --- What BloggerBear is wearing")
    return APP_JS[start : APP_JS.index("// --- Article body (markdown)", start)]


def test_the_gear_section_never_sets_a_style_attribute_or_markup():
    section = _gear_section_of_app()

    # The CSP is style-src 'self': no inline styles. And everything is text, never markup.
    assert not re.search(r"\.style\b|['\"]style['\"]|cssText", section)
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", section)


def test_the_tooltip_can_be_reached_by_hover_focus_tap_and_dismissed_with_escape():
    section = _gear_section_of_app()

    assert "pointerenter" in section and "focusin" in section and 'addEventListener("click"' in section
    assert "Escape" in section
    assert 'role: "tooltip"' in section and "aria-describedby" in section
    assert 'type: "button"' in section  # a real button, so it is focusable and works with Enter and Space


def test_the_gear_can_also_be_read_as_a_list_so_nothing_depends_on_hovering():
    assert "View the gear as a list" in _gear_section_of_app()


def test_the_backpack_is_shown_only_as_a_count():
    section = _gear_section_of_app()

    assert "backpackText" in section and "backpack_count" in section
    assert not re.search(r"\.backpack\b|\[.backpack.\]", section)  # only the count is ever read


def test_the_page_loads_gear_js_before_app_js_and_both_environments_serve_it():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")
    assert html.index('src="gear.js"') < html.index('src="app.js"')
    for env in ("dev", "production"):
        terraform = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
        assert re.search(r'"gear\.js"\s*=\s*"application/javascript"', terraform), env


def test_the_stats_page_asks_for_the_gear_separately_so_a_failure_never_hides_the_numbers():
    assert 'apiUrl("/equipment")' in _gear_section_of_app()
    assert "loadGear(gearSection)" in APP_JS
    assert "Could not load the gear right now." in _gear_section_of_app()


def _render_stats_body() -> str:
    start = APP_JS.index("function renderStats(stats)")
    return APP_JS[start : APP_JS.index("function loadStats()", start)]


def test_the_stats_page_opens_with_the_gear_and_the_numbers_follow():
    body = _render_stats_body()

    order = [
        body.index('el("h1", { text: "Stats" })'),
        body.index("loadGear(gearSection)"),
        body.index('text: "Spend and usage"'),
        body.index("stats.cost_basis"),
        body.index('className: "stats-tiles"'),
        body.index("Estimated spend per day"),
    ]
    assert order == sorted(order), "the page should read: title, gear, then spend and usage"


def test_the_original_stats_are_all_still_there_below_the_gear():
    body = _render_stats_body()
    after_gear = body[body.index("loadGear(gearSection)") :]

    for heading in ("Estimated spend per day", "By model", "By topic"):
        assert heading in after_gear
    for tile in (
        "Estimated AI spend",
        "Articles drafted",
        "Average cost per article",
        "Tokens",
        "Research spend",
    ):
        assert f'"{tile}"' in after_gear
