"""The moods BloggerBear can have, and the bear picture for each (frontend/moods.js, frontend/bears/).

Contract tests: adding a mood on the backend without its picture, or a picture without serving it,
fails here rather than showing a broken image on the Musings page.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from common import musings

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
BEARS = FRONTEND / "bears"
MOODS_JS = FRONTEND / "moods.js"
NODE = shutil.which("node")

SVG = "{http://www.w3.org/2000/svg}"


def _moods_js() -> dict:
    """Run moods.js under Node and return what it exports (MOODS, FALLBACK_IMAGE)."""
    script = "const m=require(process.argv[1]);process.stdout.write(JSON.stringify(m))"
    result = subprocess.run(
        [NODE, "-e", script, str(MOODS_JS)], capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(result.stdout)


def _describe(*values) -> list[dict]:
    script = (
        "const m=require(process.argv[1]);"
        "const v=JSON.parse(process.argv[2]);"
        "process.stdout.write(JSON.stringify(v.map(x=>m.describe(x))))"
    )
    result = subprocess.run(
        [NODE, "-e", script, str(MOODS_JS), json.dumps(list(values))],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


# --- the backend's moods, and the pictures -----------------------------------------------------------


def test_bloggerbear_has_five_moods():
    assert set(musings.MOODS) == {"proud", "thoughtful", "pleased", "reflective", "curious"}
    assert len(musings.MOODS) == 5


def test_every_backend_mood_has_a_bear_picture():
    for mood in musings.MOODS:
        assert (BEARS / f"{mood}.svg").is_file(), f"no picture for the {mood!r} mood"
    assert (BEARS / "default.svg").is_file()


def test_every_picture_is_a_mood_the_default_or_the_tummy_toys():
    names = {path.stem for path in BEARS.glob("*.svg")}

    # The two tummy pictures belong to the tummy-scratch toy (tummy.js), not to a mood.
    assert names == set(musings.MOODS) | {"default", "tummy", "tummy-happy"}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_front_end_knows_exactly_the_backends_moods():
    assert _moods_js()["MOODS"] == list(musings.MOODS)


@pytest.mark.parametrize("path", sorted(BEARS.glob("*.svg")), ids=lambda p: p.name)
def test_each_picture_is_a_small_square_safe_svg(path):
    text = path.read_text(encoding="utf-8")
    root = ET.fromstring(text)

    assert root.tag == f"{SVG}svg"
    assert root.get("viewBox") == "0 0 64 64"  # square, so it fits the 56px slot
    assert len(text.encode()) < 8_000
    # An image, not a page: nothing that runs, loads, or links out.
    assert not re.search(r"<script|<foreignObject|<iframe|<image|javascript:|data:text", text, re.I)
    assert not re.search(r"\son[a-z]+\s*=", text, re.I)
    assert "http://" not in text.replace('xmlns="http://www.w3.org/2000/svg"', "")
    assert "href" not in text


def test_the_pictures_are_all_served_by_both_environments():
    for env in ("dev", "production"):
        terraform = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
        assert '"moods.js"' in terraform, env
        for path in BEARS.glob("*.svg"):
            entry = re.search(rf'"bears/{path.name}"\s*=\s*"image/svg\+xml"', terraform)
            assert entry, f"{env} does not serve bears/{path.name}"


def test_the_pictures_revalidate_so_a_replacement_shows_up_at_once():
    for env in ("dev", "production"):
        terraform = (ROOT / "infra" / "environments" / env / "main.tf").read_text(encoding="utf-8")
        assert 'startswith(each.key, "bears/")' in terraform, env


def test_the_page_loads_moods_js_before_app_js():
    html = (FRONTEND / "index.html").read_text(encoding="utf-8")

    assert html.index('src="moods.js"') < html.index('src="app.js"')


# --- describe() ------------------------------------------------------------------------------------


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("mood", ["proud", "thoughtful", "pleased", "reflective", "curious"])
def test_a_known_mood_gets_its_own_bear_and_its_word(mood):
    (result,) = _describe(mood)

    assert result == {"label": mood, "image": f"bears/{mood}.svg", "known": True}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_case_and_spaces_do_not_matter():
    (result,) = _describe("  Proud ")

    assert result == {"label": "proud", "image": "bears/proud.svg", "known": True}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_new_mood_with_no_picture_yet_shows_its_word_and_the_plain_bear():
    (result,) = _describe("excited")

    assert result == {"label": "excited", "image": "bears/default.svg", "known": False}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("value", [None, "", "   ", 5, True, ["proud"], {"a": 1}])
def test_no_mood_means_no_line_and_the_plain_bear(value):
    (result,) = _describe(value)

    assert result == {"label": None, "image": "bears/default.svg", "known": False}


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize(
    "value",
    ["<script>alert(1)</script>", "proud<b>", "x" * 40, "../../etc/passwd", "proud.svg", "1proud", "a\nb"],
)
def test_a_mood_that_is_not_plain_words_is_never_shown_or_used_as_a_path(value):
    (result,) = _describe(value)

    assert result["label"] is None
    assert result["image"] == "bears/default.svg"  # never a path built from the value


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_two_word_mood_is_shown_but_never_becomes_a_picture_name():
    (result,) = _describe("over the moon")

    assert result == {"label": "over the moon", "image": "bears/default.svg", "known": False}
