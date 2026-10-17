"""The Musings page names the topic an article musing is for, in grey italics beside "read the
article". Rendered by app.js in Node against a fake API (the harness in test_frontend_attribution),
so the test sees the page as a reader does."""

from __future__ import annotations

from test_frontend_attribution import _flat, _links, needs_node
from test_frontend_pagination import _find, _render


def _musing(**over):
    musing = {
        "musing_id": "m1",
        "kind": "article",
        "text": "I'm quite proud of this one.",
        "mood": "proud",
        "created_at": "2026-09-12T00:00:00+00:00",
        "article_id": "article-1",
        "topic_id": "github-trending",
        "topic_name": "GitHub Trending",
    }
    musing.update(over)
    return musing


def _metas(musings):
    children = _render(
        "#/musings",
        {
            "/topics": {"topics": []},
            "/musings": {
                "musings": musings,
                "page": 1,
                "page_size": 15,
                "total": len(musings),
                "total_pages": 1,
            },
        },
    )
    return _find({"children": children}, "musing-meta")


def _topic_names(meta):
    return [child for child in meta["children"] if child.get("className") == "musing-topic"]


@needs_node
def test_an_article_musing_names_its_topic_beside_the_link():
    (meta,) = _metas([_musing()])

    # The date, then the link, then the topic, each after a dot.
    assert _flat(meta).endswith(" · read the article · GitHub Trending")
    (link,) = _links(meta)
    assert link["attributes"]["href"] == "#/article/article-1"
    (topic,) = _topic_names(meta)
    # An <em>, so it is italic with or without the stylesheet; the meta line's colour is the grey.
    assert topic["tag"] == "em" and topic["textContent"] == "GitHub Trending"
    assert meta["children"].index(topic) > meta["children"].index(link)


@needs_node
def test_a_musing_with_no_topic_name_or_no_article_shows_no_topic():
    metas = _metas(
        [
            _musing(musing_id="m1", topic_name=None),  # the topic has since been deleted
            _musing(musing_id="m2", kind="rejection", article_id=None),  # the text names the topic itself
        ]
    )
    assert len(metas) == 2
    for meta in metas:
        assert _topic_names(meta) == []
        assert "GitHub Trending" not in _flat(meta)
