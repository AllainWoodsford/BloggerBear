"""content_checks: published things that look wrong. The same rules as tools.py: read-only, and
what is said aloud is written here.

Four checks, over the articles published in the last `days` and the musings written in them:

- a musing about an article with no text: the feed shows a mood and a link, then nothing;
- a title with markup in it (`**`, a leading `#`, a backtick, an HTML tag, quote marks around the
  whole of it): the page shows the markup as it is;
- a body that is one code fence: the page shows the whole article as a block of code;
- a musing that links to an article that is not published: the link leads nowhere.

Titles, bodies and musings are text a model wrote, so none of it is spoken and none of it reaches
a command: a finding says which *kind* of thing is wrong, and the title and the musing's text go
under `untrusted` for the page. The body is read and never returned.

**One S3 read per article.** The body is not in the table, so checking it is a GetObject each;
CONTENT_MAX_ARTICLES caps how many articles one call checks, newest first, and the answer says
how many were left out.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from common.dynamo import get_topic, list_musings, list_published_articles
from common.security_events import untrusted_text
from common.static_pages import read_article_body
from ops_mcp.suggestions import finding
from ops_mcp.tools import TITLE_MAX_CHARS, _clamp_days, _join, _now, _parse, _topic_label

CONTENT_DEFAULT_DAYS = 7
CONTENT_MAX_DAYS = 30
CONTENT_MAX_ARTICLES = 40
# The musings table has no index to ask by date: the newest this many are read, which is far more
# than a month of them.
CONTENT_MAX_MUSINGS = 500
CONTENT_SPOKEN_LINES = 5
MUSING_MAX_CHARS = 200

_FENCE = "```"
_HTML_TAG = re.compile(r"</?[A-Za-z][^<>]*>")
# Opening quote mark -> the mark that closes it.
_QUOTE_PAIRS = {'"': '"', "'": "'", "“": "”", "‘": "’", "«": "»"}

_ARTICLE_SPOKEN = {
    "title_markup": "has markup in its title",
    "body_code_fence": "has its whole body inside a code fence",
    "title_markup_and_body_code_fence": "has markup in its title and its whole body inside a code fence",
    "musing_no_text": "has a musing that went out with a link but no text",
}


def _title_has_markup(title) -> bool:
    text = str(title or "").strip()
    if not text:
        return False
    if "**" in text or "`" in text or text.startswith("#") or _HTML_TAG.search(text):
        return True
    # Quote marks around the whole of it: one pair, opening the title and closing it. A title
    # that only begins and ends with quoted words ('"Up" beats "down"') has more inside.
    closing = _QUOTE_PAIRS.get(text[0])
    if closing is None or len(text) < 2 or text[-1] != closing:
        return False
    # Single marks are also apostrophes, so more of them inside says nothing.
    inside = text[1:-1]
    return closing in ("'", "’") or (text[0] not in inside and closing not in inside)


def _body_is_one_code_fence(body: str) -> bool:
    text = body.strip()
    return len(text) >= 2 * len(_FENCE) and text.startswith(_FENCE) and text.endswith(_FENCE)


def _published_at(article: dict) -> datetime | None:
    return _parse(article.get("published_at")) or _parse(article.get("created_at"))


def _musing_has_no_text(musing: dict) -> bool:
    """A musing about an article that went out with nothing to say."""
    return musing.get("kind") == "article" and not str(musing.get("text") or "").strip()


def _title_and_body_problem(article: dict) -> str | None:
    """Which of the title and body kinds a published article has, or None. One kind, not two,
    when both are wrong: one card. This is the one S3 read per article (see the module
    docstring), and it raises if the body cannot be read."""
    title = _title_has_markup(article.get("title"))
    fenced = _body_is_one_code_fence(read_article_body(article["body_s3_key"]))
    if title and fenced:
        return "title_markup_and_body_code_fence"
    if title:
        return "title_markup"
    return "body_code_fence" if fenced else None


def _article_finding(kind: str, name: str, article_id) -> dict:
    """The finding for a published article with one of _ARTICLE_SPOKEN's problems (memory.py
    rebuilds it when it follows a suggestion up)."""
    noticed = f"A published {name} article {_ARTICLE_SPOKEN[kind]}"
    return finding(kind, noticed, article_id, topic=name, article_id=article_id)


def content_checks(days: int = CONTENT_DEFAULT_DAYS, *, now: datetime | None = None) -> dict:
    """Published things that look wrong, among the articles published in the last `days` (1 to
    30) and the musings written in them."""
    now = _now(now)
    days = _clamp_days(days, CONTENT_MAX_DAYS)
    since = now - timedelta(days=days)

    published = {a.get("article_id"): a for a in list_published_articles()}
    recent = sorted(
        (a for a in published.values() if (_published_at(a) or since) >= since),
        key=lambda a: a.get("published_at") or a.get("created_at") or "",
        reverse=True,
    )
    checked, left_out = recent[:CONTENT_MAX_ARTICLES], max(len(recent) - CONTENT_MAX_ARTICLES, 0)
    musings = [
        m
        for m in list_musings(CONTENT_MAX_MUSINGS)
        if m.get("article_id") and (_parse(m.get("created_at")) or since) >= since
    ]

    names: dict[str, str] = {}

    def label(topic_id) -> str:
        if not topic_id:
            return "an unknown topic"
        if topic_id not in names:
            names[topic_id] = _topic_label(get_topic(topic_id), topic_id)
        return names[topic_id]

    # article_id -> what is wrong with it, in the order the checks are listed above.
    wrong: dict[str, list[str]] = {}
    dangling, unreadable = [], 0

    for musing in musings:
        article_id = musing["article_id"]
        if article_id not in published:
            dangling.append(musing)
        elif _musing_has_no_text(musing):
            wrong.setdefault(article_id, [])
            if "musing_no_text" not in wrong[article_id]:
                wrong[article_id].append("musing_no_text")

    for article in checked:
        article_id = article.get("article_id")
        try:
            kind = _title_and_body_problem(article)
        except Exception as exc:  # noqa: BLE001 - one unreadable body must not hide the other checks
            unreadable += 1
            print(f"content_checks: could not read the body of {article_id!r}: {exc!r}")
            kind = "title_markup" if _title_has_markup(article.get("title")) else None
        if kind:
            wrong.setdefault(article_id, []).append(kind)

    rows, findings = [], []
    for article_id, kinds in wrong.items():
        article = published[article_id]
        name = label(article.get("topic_id"))
        when = _published_at(article)
        rows.append(
            {
                "article_id": article_id,
                "topic_id": article.get("topic_id"),
                "topic": name,
                "published_at": when.isoformat() if when else None,
                "problems": kinds,
                # A model wrote the title: for the page, never for speech.
                "untrusted": {"title": untrusted_text(article.get("title"), TITLE_MAX_CHARS)},
            }
        )
        for kind in kinds:
            findings.append(_article_finding(kind, name, article_id))

    dangling_rows = []
    for musing in dangling:
        musing_id, article_id = musing.get("musing_id"), musing["article_id"]
        dangling_rows.append(
            {
                "musing_id": musing_id,
                "article_id": article_id,
                "topic": label(musing.get("topic_id")),
                "created_at": musing.get("created_at"),
                "untrusted": {"text": untrusted_text(musing.get("text"), MUSING_MAX_CHARS)},
            }
        )
        findings.append(
            finding(
                "musing_dangling",
                "A musing links to an article that is not published",
                musing_id,
                musing_id=musing_id,
            )
        )

    return {
        "spoken": _content_spoken(len(checked), days, rows, len(dangling_rows), left_out, unreadable),
        "findings": findings,
        "days": days,
        "articles_checked": len(checked),
        "articles_left_out": left_out,
        "bodies_unreadable": unreadable,
        "articles": rows,
        "dangling_musings": dangling_rows,
        "as_of": now.isoformat(),
    }


def _content_spoken(
    checked: int, days: int, rows: list[dict], dangling: int, left_out: int, unreadable: int
) -> str:
    window = f"the last {days} days" if days != 1 else "the last day"
    opening = f"I checked {checked} article{'s' if checked != 1 else ''} published in {window}"
    if left_out:
        opening += f", the newest; {left_out} more were not checked"
    sentences = [opening + "."]
    for row in rows[:CONTENT_SPOKEN_LINES]:
        sentences.append(f"A {row['topic']} article {_join([_ARTICLE_SPOKEN[k] for k in row['problems']])}.")
    if len(rows) > CONTENT_SPOKEN_LINES:
        sentences.append(f"And {len(rows) - CONTENT_SPOKEN_LINES} more articles look wrong.")
    if dangling:
        link = "musings link" if dangling != 1 else "musing links"
        sentences.append(f"{dangling} {link} to an article that is not published.")
    if unreadable:
        body = "bodies" if unreadable != 1 else "body"
        sentences.append(f"I could not read {unreadable} article {body}.")
    if not rows and not dangling:
        sentences.append("Nothing looks wrong.")
    return " ".join(sentences)
