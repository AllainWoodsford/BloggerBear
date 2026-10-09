"""The "Articles in the Pipeline" box on a topic page (frontend/app.js's pipelineItemsFor and
renderPipelineSection): at most three articles awaiting review are listed, then one plain
"...more" line -- text, not a link or button -- when there are more than that.

Runs the real app.js under Node with the stand-in DOM from test_frontend_attribution.py.
"""

from __future__ import annotations

import pytest
from test_frontend_attribution import _content, _flat, needs_node


def _pending(n):
    return [
        {"status": "pending_review", "label": "Pending review", "title": f"Pending {i}"} for i in range(n)
    ]


RESEARCHING = {"status": "researching", "label": "Researching", "title": "Bitcoin"}


def _box(activity) -> dict | None:
    children = _content(
        "#/topic/crypto",
        {
            "/topics": {"topics": []},
            "/articles?topic_id=crypto": {"topic_id": "crypto", "articles": []},
            "/topics/crypto/activity": activity,
        },
    )
    boxes = [child for child in children if child.get("className") == "pipeline-box"]
    return boxes[0] if boxes else None


def _rows(box: dict) -> list[dict]:
    (pipeline_list,) = (child for child in box["children"] if child.get("className") == "pipeline-list")
    return pipeline_list["children"]


def _texts(box: dict) -> list[str]:
    return [" | ".join(_flat(part) for part in row["children"]) or row["textContent"] for row in _rows(box)]


@needs_node
@pytest.mark.parametrize("count", [1, 2, 3])
def test_three_or_fewer_pending_articles_are_all_listed_with_no_more_line(count):
    box = _box({"pending_review_count": count, "pipeline_items": _pending(count) + [RESEARCHING]})

    assert _texts(box) == [f"Pending review | Pending {i}" for i in range(count)] + ["Researching | Bitcoin"]
    assert not any(row["className"] == "pipeline-more" for row in _rows(box))


@needs_node
def test_more_than_three_pending_articles_lists_three_then_a_plain_more_line():
    # The API names only three (PIPELINE_PENDING_LIMIT); the count says there are seven.
    box = _box({"pending_review_count": 7, "pipeline_items": _pending(3) + [RESEARCHING]})

    rows = _rows(box)
    assert _texts(box) == [
        "Pending review | Pending 0",
        "Pending review | Pending 1",
        "Pending review | Pending 2",
        "...more",
        "Researching | Bitcoin",
    ]
    more = rows[3]
    # Plain text: an <li> with no link, button, or anything focusable inside it.
    assert more["tag"] == "li" and more["className"] == "pipeline-more"
    assert more["children"] == []
    assert "tabindex" not in more["attributes"] and "role" not in more["attributes"]


@needs_node
def test_an_older_api_that_sends_every_pending_article_is_still_capped_at_three():
    box = _box({"pending_review_count": 5, "pipeline_items": _pending(5)})

    assert _texts(box) == [
        "Pending review | Pending 0",
        "Pending review | Pending 1",
        "Pending review | Pending 2",
        "...more",
    ]


@needs_node
def test_the_count_only_fallback_is_capped_at_three_too():
    box = _box({"researching": True, "pending_review_count": 6})

    assert _texts(box) == ["Pending review"] * 3 + ["...more", "Researching"]


@needs_node
def test_no_pending_articles_means_no_more_line():
    box = _box({"pending_review_count": 0, "pipeline_items": [RESEARCHING]})

    assert _texts(box) == ["Researching | Bitcoin"]
