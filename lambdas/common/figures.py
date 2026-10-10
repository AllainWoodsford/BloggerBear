"""Figures on the way from a Finding to an article page (docs/enhancements/rail-access-monitor.md
§4, PR C).

An adapter that draws something (a map, a chart) stores the PNG in the private content bucket and
hands the research tick `[{"key", "caption", "alt"}]` through `Adapter.figures`; the tick keeps the
list on the Finding, the daily cycle gathers the figures of the findings an article was written
from onto the article, and the page copies each PNG into the site bucket under
`articles/figures/<article_id>/<n>.png` -- the only prefix the site bucket may be written under.
Nothing here is specific to any adapter, and nothing here touches AWS: this module shapes and
checks the lists, so a careless adapter or a tampered item never puts an odd key or a long caption
on a page. common/static_pages.py does the copying; this stays importable by every Lambda.
"""

from __future__ import annotations

import re

# A page is an article with a map or two on it, not a gallery; three keeps it that and keeps the
# copies and the CloudFront paths per article small.
MAX_FIGURES_PER_ARTICLE = 3
# A caption is a sentence or two under the image; alt text is read aloud, so it is shorter.
MAX_CAPTION_CHARS = 300
MAX_ALT_CHARS = 200

# The only keys a figure has, on the way in and on the way out. Anything else on a stored entry
# is dropped rather than passed through to a page or an API response.
FIGURE_FIELDS = ("key", "caption", "alt")

# A content-bucket key for a PNG, and nothing that could reach outside it: no leading slash, no
# "..", plain path characters only, capped, and the PNG extension (CopyObject writes the type the
# caller states, so the extension is the one promise that the bytes are an image).
_KEY = re.compile(r"^(?!/)(?!.*\.\.)[A-Za-z0-9_./-]{1,200}\.png$")
# What a caption or alt text may not carry into a page, an attribute or an API body, whatever an
# adapter or a tampered item hands over. C0 and C1 controls (NUL, CR, LF, ESC) and the line and
# paragraph separators become a space, so a line break still separates words; zero-width and
# bidi-override characters (U+202E would reverse the visible caption) are removed outright, so a
# word they sat inside stays one word. Whitespace is then collapsed: one plain line.
_CONTROLS = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
# How many figures one Finding may carry: more than an article shows, so a tick that measured
# several sites keeps a figure for each, but bounded, since a Findings item is capped at 400 KB.
MAX_FIGURES_PER_FINDING = 10

# Where a published article's figures live in the site bucket (and so under the site's origin).
PUBLIC_FIGURE_PREFIX = "articles/figures/"


def plain_text(value: str) -> str:
    """`value` as one line of plain text: controls to spaces, invisible characters out, whitespace
    runs collapsed."""
    return " ".join(_INVISIBLE.sub("", _CONTROLS.sub(" ", value)).split())


def clean_figure(figure) -> dict | None:
    """One figure as exactly {"key", "caption", "alt"}, or None if it is not usable: the key a
    well-formed PNG key, the caption a non-empty string within its limit, the alt text a string
    within its limit (a missing or blank alt falls back to the caption, cut to the alt limit, so
    a figure is never lost for want of it; an over-long one is a malformed entry and drops it)."""
    if not isinstance(figure, dict):
        return None
    key = figure.get("key")
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        return None
    caption = figure.get("caption")
    if not isinstance(caption, str):
        return None
    caption = plain_text(caption)
    if not caption or len(caption) > MAX_CAPTION_CHARS:
        return None
    alt = figure.get("alt")
    if alt is None or (isinstance(alt, str) and not alt.strip()):
        alt = caption[:MAX_ALT_CHARS]
    if not isinstance(alt, str):
        return None
    alt = plain_text(alt)
    if len(alt) > MAX_ALT_CHARS:
        return None
    return {"key": key, "caption": caption, "alt": alt}


def clean_figures(figures) -> list[dict]:
    """`figures` as a list of well-formed entries, in order, each key once. Used on everything
    that is about to be stored, copied, rendered or returned -- an adapter's answer and a list
    read back from a Findings or Articles item alike -- so a malformed or tampered entry is left
    out, never copied or rendered."""
    if not isinstance(figures, list | tuple):
        return []
    cleaned: list[dict] = []
    seen: set[str] = set()
    for figure in figures:
        item = clean_figure(figure)
        if item is None or item["key"] in seen:
            continue
        seen.add(item["key"])
        cleaned.append(item)
    return cleaned


def figures_for_findings(findings) -> list[dict]:
    """The figures an article drawn from `findings` shows: the findings' figures in the order the
    findings are given (newest first, as common/dynamo.py's list_recent_findings returns them),
    each key once, and at most MAX_FIGURES_PER_ARTICLE -- so a topic that draws a map on every
    tick shows the newest few, not a day's worth."""
    gathered: list[dict] = []
    for finding in findings or []:
        if not isinstance(finding, dict):
            continue
        gathered.extend(clean_figures(finding.get("figures")))
    return clean_figures(gathered)[:MAX_FIGURES_PER_ARTICLE]


def figure_key(article_id: str, index: int) -> str:
    """The site-bucket key of an article's `index`th figure (from 1): the position in the
    article's stored list, so a take-down knows every key from the count alone (the Lambda role
    cannot list the bucket)."""
    return f"{PUBLIC_FIGURE_PREFIX}{article_id}/{index}.png"


def public_figures(article_id: str, figures) -> list[dict]:
    """`figures` as the page and the public API show them: `src` is the site path of the copy
    (`/articles/figures/<article_id>/<n>.png`), with the caption and alt text; the content-bucket
    key stays private."""
    return [
        {"src": "/" + figure_key(article_id, index), "caption": figure["caption"], "alt": figure["alt"]}
        for index, figure in enumerate(clean_figures(figures), start=1)
    ]
