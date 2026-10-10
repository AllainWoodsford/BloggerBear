"""common/figures.py: the shape a figure keeps from a Finding to an article page -- a PNG key in
the content bucket, a caption, alt text -- and the rules the pipeline applies to a list of them: a
malformed entry is dropped, a key appears once, an article shows at most a few, and what the
public sees is a site path, never the key. Pure functions, no AWS."""

from __future__ import annotations

import pytest

from common import figures
from common.figures import (
    MAX_ALT_CHARS,
    MAX_CAPTION_CHARS,
    MAX_FIGURES_PER_ARTICLE,
    clean_figure,
    clean_figures,
    figure_key,
    figures_for_findings,
    public_figures,
)

KEY = "vision/sydney-rail/botany-bay/S2A_56HLH_20260915_0_L2A.png"
GOOD = {
    "key": KEY,
    "caption": "Botany Bay, 15 September 2026: ships on the water.",
    "alt": "A map of the bay",
}


def _figure(key=KEY, caption="A caption", alt="Alt text", **extra):
    return {"key": key, "caption": caption, "alt": alt, **extra}


# --- one figure -------------------------------------------------------------------------------------


def test_a_well_formed_figure_is_kept_as_exactly_its_three_fields():
    assert clean_figure({**GOOD, "width": 800, "_private": "x"}) == GOOD


@pytest.mark.parametrize(
    "key",
    [
        "../etc/passwd.png",  # reaches out of wherever it is read from
        "vision/../secrets.png",
        "vision/sydney/..png",
        "/vision/absolute.png",  # not a bucket key
        "vision/sydney/scene.jpg",  # not a PNG
        "vision/sydney/scene.PNG",
        "vision/sydney/scene.png.exe",
        "vision/sydney/scene png.png",  # whitespace
        "vision/sydney/scene.png\n",  # a trailing newline, which `$` alone would let through
        "vision/sydney/scène.png",  # outside the plain character set
        "vision/sydney/sc%2ene.png",
        "a" * 201 + ".png",  # too long
        ".png",
        "",
        None,
        42,
    ],
)
def test_a_key_that_is_not_a_plain_png_path_drops_the_figure(key):
    assert clean_figure(_figure(key=key)) is None


@pytest.mark.parametrize("key", [KEY, "figures/a-b_c.1.png", "a" * 200 + ".png", "x.png"])
def test_a_plain_png_key_is_accepted(key):
    assert clean_figure(_figure(key=key))["key"] == key


def test_a_caption_is_trimmed_and_capped():
    assert clean_figure(_figure(caption="  Trimmed.  "))["caption"] == "Trimmed."
    assert clean_figure(_figure(caption="c" * MAX_CAPTION_CHARS)) is not None
    assert clean_figure(_figure(caption="c" * (MAX_CAPTION_CHARS + 1))) is None


def test_captions_and_alt_text_come_out_as_one_plain_line():
    """Controls, bidi overrides and zero-width characters are stripped and whitespace runs collapsed,
    whatever an adapter or a tampered item handed over: a U+202E in a name would otherwise reverse
    the visible caption on the page."""
    figure = clean_figure(
        _figure(caption="Map of \u202eMelbourne\u202c\x00 line\r\nbreak\x1b[31m  wide", alt="a\x00b\u200e\tc")
    )
    assert figure["caption"] == "Map of Melbourne line break [31m wide"
    assert figure["alt"] == "a b c"


@pytest.mark.parametrize("caption", ["", "   ", None, 7, ["a caption"]])
def test_a_figure_without_a_caption_is_dropped(caption):
    assert clean_figure(_figure(caption=caption)) is None


def test_alt_text_is_trimmed_and_capped():
    assert clean_figure(_figure(alt="  Read aloud.  "))["alt"] == "Read aloud."
    assert clean_figure(_figure(alt="a" * MAX_ALT_CHARS)) is not None
    assert clean_figure(_figure(alt="a" * (MAX_ALT_CHARS + 1))) is None
    assert clean_figure(_figure(alt=["not", "text"])) is None


def test_missing_alt_text_falls_back_to_the_caption_cut_to_its_limit():
    """A figure is never lost for want of alt text: the caption is the next best thing a screen
    reader can say."""
    figure = {"key": KEY, "caption": "The caption"}
    assert clean_figure(figure)["alt"] == "The caption"
    assert clean_figure({**figure, "alt": "   "})["alt"] == "The caption"
    long_caption = "c" * (MAX_ALT_CHARS + 50)
    assert clean_figure({"key": KEY, "caption": long_caption})["alt"] == "c" * MAX_ALT_CHARS


@pytest.mark.parametrize("entry", [None, "vision/x.png", 5, ["vision/x.png"], {}])
def test_something_that_is_not_a_figure_is_dropped(entry):
    assert clean_figure(entry) is None


# --- a list of them ---------------------------------------------------------------------------------


def test_a_list_keeps_the_good_entries_in_order_and_each_key_once():
    first = _figure(key="v/1.png", caption="first")
    second = _figure(key="v/2.png", caption="second")
    again = _figure(key="v/1.png", caption="the same key, a later caption")

    assert clean_figures([first, "junk", _figure(key="../x.png"), second, again]) == [first, second]


@pytest.mark.parametrize("not_a_list", [None, "v/1.png", {"key": KEY}, 3])
def test_anything_but_a_list_is_no_figures(not_a_list):
    assert clean_figures(not_a_list) == []


def test_a_tuple_is_as_good_as_a_list():
    assert clean_figures((GOOD,)) == [GOOD]


# --- from the findings an article is written from ---------------------------------------------------


def _finding(*keys):
    return {"summary": "...", "figures": [_figure(key=key, caption=f"Figure {key}") for key in keys]}


def test_the_figures_of_the_findings_come_newest_first_each_once_and_capped():
    """Findings are given newest first (common/dynamo.py's list_recent_findings): the article
    shows the newest few, and a map that repeats shows once, at its newest position."""
    assert MAX_FIGURES_PER_ARTICLE == 3
    findings = [
        _finding("v/c.png", "v/b.png"),  # newest
        _finding("v/b.png", "v/a.png"),
        _finding("v/z.png"),  # oldest: over the cap
    ]

    assert [f["key"] for f in figures_for_findings(findings)] == ["v/c.png", "v/b.png", "v/a.png"]


def test_findings_without_figures_and_things_that_are_not_findings_are_passed_over():
    findings = [{"summary": "no figures"}, None, "junk", {"figures": "not a list"}, _finding("v/a.png")]

    assert [f["key"] for f in figures_for_findings(findings)] == ["v/a.png"]
    assert figures_for_findings([]) == []
    assert figures_for_findings(None) == []


def test_a_malformed_figure_on_a_finding_does_not_take_the_others_down():
    findings = [{"figures": [_figure(key="../bad.png"), GOOD, {"key": KEY}]}]

    assert figures_for_findings(findings) == [GOOD]


# --- where the copies live --------------------------------------------------------------------------


def test_a_figure_key_is_the_articles_prefix_and_its_position_from_one():
    assert figure_key("a1", 1) == "articles/figures/a1/1.png"
    assert figure_key("a1", 3) == "articles/figures/a1/3.png"
    assert figure_key("a1", 1).startswith(figures.PUBLIC_FIGURE_PREFIX)


def test_public_figures_are_site_paths_by_position_with_the_caption_and_alt_and_no_key():
    stored = [GOOD, _figure(key="v/2.png", caption="Second", alt="Two")]

    assert public_figures("a1", stored) == [
        {"src": "/articles/figures/a1/1.png", "caption": GOOD["caption"], "alt": GOOD["alt"]},
        {"src": "/articles/figures/a1/2.png", "caption": "Second", "alt": "Two"},
    ]


def test_public_figures_clean_first_so_a_tampered_item_shows_nothing_odd():
    assert public_figures("a1", [_figure(key="../x.png"), "junk"]) == []
    assert public_figures("a1", None) == []
