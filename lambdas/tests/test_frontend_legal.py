"""The Terms of Service and Privacy Policy are static pages (frontend/terms.html, privacy.html),
like about.html, not text inside app.js: readable with JavaScript off, indexed at their own URLs,
and linkable section by section. The old #/terms and #/privacy routes redirect to them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
LEGAL_PAGES = ("terms.html", "privacy.html")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", LEGAL_PAGES)
def test_a_legal_page_needs_no_javascript(name):
    html = _read(FRONTEND / name)

    assert "<script" not in html
    assert html.count("<h1>") == 1
    assert re.search(r'Last updated: <time datetime="\d{4}-\d{2}-\d{2}">', html)


@pytest.mark.parametrize("name", LEGAL_PAGES)
def test_every_section_of_a_legal_page_can_be_linked_to(name):
    html = _read(FRONTEND / name)
    ids = re.findall(r'<section id="([a-z0-9-]+)">\s*<h2>', html)

    assert len(ids) == html.count("<h2>") >= 5
    assert len(set(ids)) == len(ids)


def test_links_into_the_legal_pages_point_at_sections_that_exist():
    ids = {name: set(re.findall(r'id="([a-z0-9-]+)"', _read(FRONTEND / name))) for name in LEGAL_PAGES}
    checked = 0
    for page in FRONTEND.glob("*.html"):
        html = _read(page)
        for target, anchor in re.findall(r'href="/?((?:terms|privacy)\.html)?#([a-z0-9-]+)"', html):
            target = target or (page.name if page.name in LEGAL_PAGES else None)
            if target is None:
                continue  # an in-page anchor (#content, #top) on a page that isn't a legal page
            assert anchor in ids[target], f"{page.name} links to {target}#{anchor}, which doesn't exist"
            checked += 1
    assert checked >= 3


def test_the_old_hash_routes_redirect_to_the_static_pages():
    app = _read(FRONTEND / "app.js")

    assert 'LEGAL_PAGE_URLS = { terms: "/terms.html", privacy: "/privacy.html" }' in app
    assert "window.location.replace(LEGAL_PAGE_URLS[pageKey])" in app
    assert re.search(r'path === "/terms"\) \{\s*return \{ name: "legal", pageKey: "terms" \}', app)
    assert re.search(r'path === "/privacy"\) \{\s*return \{ name: "legal", pageKey: "privacy" \}', app)
    # The policy text lives in one place only.
    assert "LEGAL_PAGES" not in app
    assert "Firewall and security logs" not in app


def test_nothing_links_to_the_old_hash_routes_any_more():
    sources = [*FRONTEND.glob("*.html"), ROOT / "lambdas" / "common" / "static_pages.py"]
    for path in sources:
        text = _read(path)
        # The two pages' own head comments mention the route they replace; links are what matter.
        assert not re.search(r'href="/?#/(terms|privacy)"', text), path.name
        assert '"/#/terms"' not in text and '"/#/privacy"' not in text, path.name


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_legal_pages_are_deployed(env):
    main = _read(ROOT / "infra" / "environments" / env / "main.tf")
    for name in LEGAL_PAGES:
        assert re.search(rf'^\s*"{re.escape(name)}"\s*=\s*"text/html"$', main, re.M), (env, name)
