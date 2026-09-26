"""Each static HTML shell has a non-empty <meta name="description"> (frontend/index.html,
about.html, error.html) -- flagged by a Google Lighthouse audit run against the live site
("Meta descriptions may be included in search results to concisely summarize page content"),
which fires whenever the tag is missing or empty. This app is a client-side-routed SPA under one
index.html (see frontend/app.js's hash routing), so that one description is what search results
and link previews show for every route, not just "/" -- there is no per-route server-side
rendering of <head> to vary it by page.
"""

from __future__ import annotations

import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def _description(html: str) -> str | None:
    match = re.search(r'<meta\s+name="description"\s+content="([^"]*)"', html, re.S)
    return match.group(1) if match else None


def test_every_static_page_has_a_meta_description():
    for name in ("index.html", "about.html", "error.html"):
        html = (FRONTEND / name).read_text(encoding="utf-8")
        description = _description(html)
        assert description, f"{name} has no <meta name=\"description\">"
        assert description.strip(), f"{name}'s <meta name=\"description\"> is empty"


def test_the_description_appears_before_the_title_in_the_head():
    """Not load-bearing for SEO, just keeps <head> in the conventional order (charset, viewport,
    description, title, ...) across all three files rather than tacking it on wherever."""
    for name in ("index.html", "about.html", "error.html"):
        html = (FRONTEND / name).read_text(encoding="utf-8")
        assert html.index('name="description"') < html.index("<title>")
