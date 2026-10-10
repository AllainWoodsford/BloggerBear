"""The figures in the single-page frontend (frontend/app.js's articleFigures): the pictures an open
article shows between its credit line and its body, from the API's `figures`
(lambdas/common/figures.py: [{src, caption, alt}], `src` a site path under /articles/figures/).

Runs the real app.js under Node with test_frontend_attribution's stand-in DOM, which has no HTML
parser at all: a caption can only ever arrive as a text node, and an image only as attributes.
"""

from __future__ import annotations

import re

import pytest
from test_frontend_attribution import APP_JS, FRONTEND, GITHUB, _content, _flat, needs_node

MAP = {
    "src": "/articles/figures/a1/1.png",
    "caption": "Botany Bay, 15 September 2026",
    "alt": "A map of the bay",
}
CHART = {"src": "/articles/figures/a1/2.png", "caption": "Ships per scene", "alt": "A chart of ship counts"}


def _article(figures=None, *, attribution=None, omit_figures=False):
    article = {
        "article_id": "a1",
        "title": "A Title",
        "body": "Body.",
        "published_at": "2026-09-20T00:00:00+00:00",
        "source_refs": [],
        "view_count": 1,
        "lineage": None,
        "published_by": None,
        "fact_check": None,
        "equipment_used": [],
        "attribution": attribution,
    }
    if not omit_figures:
        article["figures"] = figures
    return article


def _article_page(figures=None, *, attribution=None, omit_figures=False, app_js=APP_JS) -> list[dict]:
    article = _article(figures, attribution=attribution, omit_figures=omit_figures)
    return _content("#/article/a1", {"/topics": {"topics": []}, "/articles/a1": article}, app_js)


def _figures(children: list[dict]) -> list[dict]:
    return [child for child in children if child.get("className") == "article-figure"]


def _classes(children: list[dict]) -> list[str]:
    return [child.get("className") or child.get("tag") for child in children]


@needs_node
def test_an_open_article_shows_its_figures_between_the_credit_line_and_the_body():
    children = _article_page([MAP, CHART], attribution=[GITHUB])

    classes = _classes(children)
    at = classes.index("source-attribution")
    assert classes[at + 1 : at + 4] == ["article-figure", "article-figure", "article-body"]

    first, second = _figures(children)
    assert first["tag"] == "figure"
    image, caption = first["children"]
    assert image["tag"] == "img"
    assert image["attributes"] == {"src": MAP["src"], "alt": MAP["alt"], "loading": "lazy"}
    assert image["children"] == []
    assert caption["tag"] == "figcaption" and caption["textContent"] == MAP["caption"]
    assert second["children"][0]["attributes"]["src"] == CHART["src"]


@needs_node
def test_without_a_credit_line_the_figures_still_sit_right_above_the_body():
    children = _article_page([MAP])

    expected = ["h1", "article-meta", "lineage-summary", "article-figure", "article-body"]
    assert _classes(children)[:5] == expected


@needs_node
@pytest.mark.parametrize("figures", [None, [], "omitted"])
def test_no_figures_means_no_figure_elements(figures):
    if figures == "omitted":
        children = _article_page(omit_figures=True)  # an older API without the field
    else:
        children = _article_page(figures)

    assert _figures(children) == []
    assert _classes(children)[:4] == ["h1", "article-meta", "lineage-summary", "article-body"]


@needs_node
@pytest.mark.parametrize(
    "src",
    [
        "https://evil.example/x.png",  # another origin
        "//evil.example/x.png",
        "/other/x.png",  # the site, but not where figures live
        "/articles/x.png",
        "articles/figures/a1/1.png",  # relative: it would resolve against the current page
        "/articles/figures/../../x.png",  # back out of the prefix
        "/articles/figures/a1/../../../x.png",
        "/articles/figures/%2e%2e/x.png",  # the same, percent-encoded: a browser decodes it
        "/articles/figures/a1/.%2e/x.png",
        "/articles/figures//evil.example/x.png",
        "/articles/figures/a1/%ZZ.png",  # not decodable at all
        "data:image/png;base64,AAAA",
        "javascript:alert(1)",
        "",
        5,
        None,
    ],
)
def test_a_figure_whose_src_is_not_a_site_figure_path_is_ignored(src):
    assert _figures(_article_page([{**MAP, "src": src}])) == []
    # ...and it does not take a good one down with it.
    (figure,) = _figures(_article_page([{**MAP, "src": src}, CHART]))
    assert figure["children"][0]["attributes"]["src"] == CHART["src"]


@needs_node
def test_something_that_is_not_a_figure_is_ignored():
    assert _figures(_article_page([None, "x", 5, {}, {"caption": "no src"}])) == []


@needs_node
def test_markup_in_a_caption_or_alt_is_text_never_parsed():
    hostile = {
        "src": "/articles/figures/a1/1.png",
        "caption": 'A map <img src=x onerror=alert(1)> "quoted" & co',
        "alt": '"><script>alert(2)</script>',
    }

    (figure,) = _figures(_article_page([hostile]))

    image, caption = figure["children"]
    assert image["attributes"]["alt"] == hostile["alt"]  # an attribute value, set with setAttribute
    assert _flat(caption) == hostile["caption"]  # one text, character for character
    assert [child.get("tag") for child in caption["children"]] == []


@needs_node
def test_a_figure_without_a_caption_has_no_empty_caption_and_its_alt_is_never_missing():
    (figure,) = _figures(_article_page([{"src": MAP["src"]}]))

    (image,) = figure["children"]
    assert image["tag"] == "img" and image["attributes"]["alt"] == ""


@needs_node
def test_the_figures_survive_the_deploy_time_minifier(tmp_path):
    """The site is deployed minified (scripts/minify_frontend.py runs rjsmin over app.js). The
    minified file must draw the same figure."""
    rjsmin = pytest.importorskip("rjsmin")
    minified = tmp_path / "app.js"
    minified.write_text(rjsmin.jsmin(APP_JS.read_text(encoding="utf-8")), encoding="utf-8")

    (figure,) = _figures(_article_page([MAP], app_js=minified))

    image, caption = figure["children"]
    assert image["attributes"] == {"src": MAP["src"], "alt": MAP["alt"], "loading": "lazy"}
    assert caption["textContent"] == MAP["caption"]


# --- the source itself ----------------------------------------------------------------------------


def _section() -> str:
    """The figures helper in app.js, up to the article view that uses it."""
    source = APP_JS.read_text(encoding="utf-8")
    start = source.index("function articleFigures(")
    return source[start : source.index("function renderArticle(", start)]


def test_the_figures_are_built_from_nodes_and_attributes_never_from_markup():
    section = _section()
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", section)
    assert "indexOf(FIGURE_PATH_PREFIX) !== 0" in section
    assert 'indexOf("..") !== -1' in section
    assert 'loading: "lazy"' in section
    assert 'FIGURE_PATH_PREFIX = "/articles/figures/"' in APP_JS.read_text(encoding="utf-8")


def test_the_article_view_shows_the_apis_figures_after_the_credit_line_and_before_the_body():
    source = APP_JS.read_text(encoding="utf-8")
    at = source.index("articleFigures(article.figures)")
    assert source.rindex("sourceAttribution(article.attribution)", 0, at) < at
    assert at < source.index('className: "article-body"', at)


def test_the_figure_fills_the_column_and_its_caption_is_small_and_muted_with_no_inline_style():
    css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
    figure = re.search(r"^\.article-figure \{(.*?)\}", css, re.S | re.M).group(1)
    image = re.search(r"^\.article-figure img \{(.*?)\}", css, re.S | re.M).group(1)
    caption = re.search(r"^\.article-figure figcaption \{(.*?)\}", css, re.S | re.M).group(1)

    assert "margin: 0 0 1.5em" in figure
    assert "display: block" in image and "max-width: 100%" in image and "height: auto" in image
    assert "color: var(--muted)" in caption and "font-size: 0.85rem" in caption
    # The CSP forbids inline styles, so the page never sets one: the stylesheet is the only place.
    assert "style" not in _section()
