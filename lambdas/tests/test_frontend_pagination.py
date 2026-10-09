"""Paged listings in the single-page frontend (frontend/app.js's renderPagination): the Musings
page shows one page of the API's results (15 at a time, public_api_handler.py's MUSINGS_PAGE_SIZE)
with a nav under it -- "Newer", page numbers, "Older" -- and the page is part of the route
("#/musings?page=2"), so it can be linked to.

Runs the real app.js under Node with the stand-in DOM from test_frontend_attribution.py, against a
fake API that only answers the exact paths listed, so a test also pins which URL the page fetched.
"""

from __future__ import annotations

import functools
import tempfile
from pathlib import Path

import pytest
from test_frontend_attribution import APP_JS, FRONTEND, _content, _flat, _links, needs_node


@functools.cache
def _page_js() -> Path:
    """app.js with moods.js ahead of it, as index.html loads them: the Musings page needs both."""
    bundle = Path(tempfile.mkdtemp()) / "bundle.js"
    parts = [(FRONTEND / "moods.js").read_text(encoding="utf-8"), APP_JS.read_text(encoding="utf-8")]
    bundle.write_text("\n".join(parts), encoding="utf-8")
    return bundle


def _render(hash_, responses):
    return _content(hash_, responses, _page_js())


def _musings(count, start=0):
    return [
        {
            "musing_id": f"m{n}",
            "kind": "feedback",
            "text": f"Musing {n}",
            "mood": "proud",
            "created_at": "2026-09-12T00:00:00+00:00",
        }
        for n in range(start, start + count)
    ]


def _musings_page(hash_, page, total_pages, count=15):
    return _render(
        hash_,
        {
            "/topics": {"topics": []},
            ("/musings" if page == 1 else f"/musings?page={page}"): {
                "musings": _musings(count, (page - 1) * 15),
                "page": page,
                "page_size": 15,
                "total": (total_pages - 1) * 15 + count,
                "total_pages": total_pages,
            },
        },
    )


def _nav(children):
    navs = [child for child in children if child.get("className") == "pagination"]
    assert len(navs) <= 1
    return navs[0] if navs else None


def _find(node, class_name):
    found = [node] if node.get("className") == class_name else []
    for child in node.get("children", []):
        found.extend(_find(child, class_name))
    return found


def _numbers(nav):
    """The page numbers the nav offers, as a reader sees them ("..." for a gap)."""
    (pages,) = _find(nav, "pagination-pages")
    return [_flat(item) for item in pages["children"]]


def _items(children):
    (listing,) = (child for child in children if child.get("className") == "musings-list")
    return [_flat(item) for item in listing["children"]]


@needs_node
def test_the_first_page_shows_fifteen_musings_and_a_way_to_older_ones():
    children = _musings_page("#/musings", 1, 3)

    assert len(_items(children)) == 15
    nav = _nav(children)
    assert nav["tag"] == "nav" and nav["attributes"]["aria-label"] == "Pages of musings"
    hrefs = {link["attributes"].get("rel"): link["attributes"]["href"] for link in _links(nav)}
    assert hrefs["next"] == "#/musings?page=2"
    assert "prev" not in hrefs  # nothing newer than the newest
    assert _numbers(nav) == ["1", "2", "3"]
    (status,) = _find(nav, "pagination-status")
    assert status["textContent"] == "Page 1 of 3"


@needs_node
def test_a_middle_page_links_both_ways_and_page_one_is_the_bare_route():
    nav = _nav(_musings_page("#/musings?page=2", 2, 3))

    hrefs = {link["attributes"].get("rel"): link["attributes"]["href"] for link in _links(nav)}
    assert hrefs["prev"] == "#/musings"
    assert hrefs["next"] == "#/musings?page=3"
    (current,) = _find(nav, "pagination-current")
    assert current["attributes"]["aria-current"] == "page"
    assert current["textContent"] == "2"


@needs_node
def test_the_last_page_has_no_older_link():
    nav = _nav(_musings_page("#/musings?page=3", 3, 3, count=4))

    rels = [link["attributes"].get("rel") for link in _links(nav)]
    assert "next" not in rels and "prev" in rels


@needs_node
@pytest.mark.parametrize(
    ("page", "total", "numbers"),
    [
        (5, 12, ["1", "…", "4", "5", "6", "…", "12"]),
        (1, 12, ["1", "2", "…", "12"]),
        (12, 12, ["1", "…", "11", "12"]),
        # A gap that would hide just one page shows the page instead.
        (4, 12, ["1", "2", "3", "4", "5", "…", "12"]),
        (3, 5, ["1", "2", "3", "4", "5"]),
    ],
)
def test_long_runs_of_pages_collapse_to_a_gap(page, total, numbers):
    nav = _nav(_musings_page(f"#/musings?page={page}", page, total))

    assert _numbers(nav) == numbers


@needs_node
def test_everything_on_one_page_means_no_nav():
    children = _musings_page("#/musings", 1, 1, count=7)

    assert len(_items(children)) == 7
    assert _nav(children) is None


@needs_node
@pytest.mark.parametrize("hash_", ["#/musings?page=abc", "#/musings?page=0", "#/musings?page=-2"])
def test_a_mistyped_page_loads_the_first(hash_):
    # The fake API answers only /musings (page 1), so anything else would render the error message.
    assert len(_items(_musings_page(hash_, 1, 2))) == 15


@needs_node
def test_a_page_past_the_end_says_so_and_links_to_the_newest():
    children = _render(
        "#/musings?page=9",
        {
            "/topics": {"topics": []},
            "/musings?page=9": {"musings": [], "page": 9, "page_size": 15, "total": 20, "total_pages": 2},
        },
    )

    text = " ".join(_flat(child) for child in children)
    assert "There's no page 9 any more." in text
    assert [link["attributes"]["href"] for link in _links(children[-1])] == ["#/musings"]
    assert _nav(children) is None


@needs_node
def test_no_musings_at_all_still_says_so():
    children = _render(
        "#/musings",
        {
            "/topics": {"topics": []},
            "/musings": {"musings": [], "page": 1, "page_size": 15, "total": 0, "total_pages": 1},
        },
    )

    assert "No musings yet" in " ".join(_flat(child) for child in children)
