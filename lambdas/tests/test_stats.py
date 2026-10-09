from __future__ import annotations

from datetime import date, timedelta

import pytest

from common.costing import USD_TO_AUD_RATE
from common.stats import build_stats, rewindow_daily

TODAY = date(2026, 9, 20)

MODELS = [
    {
        "model_id": "model-a",
        "display_name": "Model A",
        "input_price_usd_per_1k_tokens": 1.0,
        "output_price_usd_per_1k_tokens": 2.0,
    },
    {
        "model_id": "model-b",
        "display_name": "Model B",
        "input_price_usd_per_1k_tokens": 0.5,
        "output_price_usd_per_1k_tokens": 1.0,
    },
]

TOPICS = [
    {"topic_id": "github-trending", "name": "GitHub Trending"},
    {"topic_id": "hacker-news", "name": "Hacker News"},
]


def _call(model_id, input_tokens, output_tokens, stage="draft"):
    return {
        "stage": stage,
        "model_id": model_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "used_fallback": False,
    }


def _article(topic_id, calls, *, status="published", created_at="2026-09-20T03:00:00+00:00"):
    lineage = None
    if calls is not None:
        lineage = {"calls": calls}
    return {"topic_id": topic_id, "status": status, "created_at": created_at, "lineage": lineage}


def _build(articles, models=MODELS, topics=TOPICS, **kwargs):
    return build_stats(articles, topics, models, today=TODAY, **kwargs)


def test_empty_data_is_all_zero_with_a_full_daily_window():
    stats = _build([])

    assert stats["totals"]["articles"] == 0
    assert stats["totals"]["cost_aud"] == 0.0
    assert stats["totals"]["avg_cost_aud"] is None
    assert stats["by_model"] == []
    assert stats["by_topic"] == []
    assert len(stats["daily"]) == 30
    assert stats["daily"][0]["date"] == "2026-08-22"
    assert stats["daily"][-1]["date"] == "2026-09-20"


def test_totals_tokens_and_cost_per_call():
    # model-a: 2k in * $1 + 1k out * $2 = $4 ; model-b: 2k in * $0.5 + 1k out * $1 = $2
    stats = _build(
        [
            _article(
                "github-trending",
                [_call("model-a", 2000, 1000), _call("model-b", 2000, 1000)],
            )
        ]
    )

    totals = stats["totals"]
    assert totals["input_tokens"] == 4000
    assert totals["output_tokens"] == 2000
    assert totals["calls"] == 2
    assert totals["unpriced_calls"] == 0
    assert totals["cost_aud"] == pytest.approx(6.0 * USD_TO_AUD_RATE)
    assert totals["avg_cost_aud"] == pytest.approx(6.0 * USD_TO_AUD_RATE)


def test_by_model_by_topic_and_daily_all_agree_with_totals():
    stats = _build(
        [
            _article("github-trending", [_call("model-a", 1000, 0)]),
            _article("hacker-news", [_call("model-b", 2000, 0)]),
            _article("hacker-news", [_call("model-a", 1000, 0)], created_at="2026-09-19T00:00:00+00:00"),
        ]
    )

    total_cost = stats["totals"]["cost_aud"]
    assert sum(row["cost_aud"] for row in stats["by_model"]) == pytest.approx(total_cost)
    assert sum(row["cost_aud"] for row in stats["by_topic"]) == pytest.approx(total_cost)
    assert sum(day["cost_aud"] for day in stats["daily"]) == pytest.approx(total_cost)

    by_topic = {row["topic_id"]: row for row in stats["by_topic"]}
    assert by_topic["hacker-news"]["articles"] == 2
    assert by_topic["hacker-news"]["name"] == "Hacker News"
    assert by_topic["github-trending"]["articles"] == 1

    daily = {day["date"]: day for day in stats["daily"]}
    assert daily["2026-09-20"]["articles"] == 2
    assert daily["2026-09-19"]["articles"] == 1


def test_by_model_sorted_by_cost_descending_and_uses_display_name():
    stats = _build(
        [
            _article("github-trending", [_call("model-b", 1000, 0), _call("model-a", 1000, 0)]),
        ]
    )

    assert [row["model_id"] for row in stats["by_model"]] == ["model-a", "model-b"]
    assert stats["by_model"][0]["display_name"] == "Model A"


def test_unpriced_model_counts_tokens_but_not_cost_and_is_reported():
    stats = _build(
        [_article("github-trending", [_call("model-a", 1000, 0), _call("mystery-model", 5000, 500)])]
    )

    totals = stats["totals"]
    assert totals["input_tokens"] == 6000
    assert totals["cost_aud"] == pytest.approx(1.0 * USD_TO_AUD_RATE)  # only model-a is priced
    assert totals["unpriced_calls"] == 1

    mystery = next(row for row in stats["by_model"] if row["model_id"] == "mystery-model")
    # Fully unpriced -> None (page can say "unpriced"), not a misleading $0.00.
    assert mystery["cost_aud"] is None
    assert mystery["unpriced_calls"] == 1
    assert mystery["display_name"] == "mystery-model"


def test_articles_without_lineage_are_counted_but_add_no_tokens_or_cost():
    stats = _build(
        [_article("github-trending", None), _article("github-trending", [_call("model-a", 1000, 0)])]
    )

    assert stats["totals"]["articles"] == 2
    assert stats["totals"]["articles_with_lineage"] == 1
    assert stats["totals"]["input_tokens"] == 1000
    # Average is over articles that actually have cost data.
    assert stats["totals"]["avg_cost_aud"] == pytest.approx(1.0 * USD_TO_AUD_RATE)


def test_unpublished_articles_still_count_toward_spend():
    stats = _build(
        [
            _article("github-trending", [_call("model-a", 1000, 0)], status="published"),
            _article("github-trending", [_call("model-a", 1000, 0)], status="pending_moderation"),
            _article("github-trending", [_call("model-a", 1000, 0)], status="rejected"),
        ]
    )

    assert stats["totals"]["articles"] == 3
    assert stats["totals"]["published"] == 1
    assert stats["totals"]["cost_aud"] == pytest.approx(3.0 * USD_TO_AUD_RATE)


def test_articles_outside_the_daily_window_count_in_totals_but_not_daily():
    stats = _build(
        [_article("github-trending", [_call("model-a", 1000, 0)], created_at="2026-01-01T00:00:00+00:00")]
    )

    assert stats["totals"]["cost_aud"] > 0
    assert sum(day["cost_aud"] for day in stats["daily"]) == 0.0
    assert sum(day["articles"] for day in stats["daily"]) == 0


def test_digest_topic_gets_its_friendly_name():
    stats = _build([_article("digest", [_call("model-a", 1000, 0)])])

    assert stats["by_topic"][0]["name"] == "Trending Everywhere"


def test_output_contains_only_aggregates_no_article_identifiers():
    article = _article("github-trending", [_call("model-a", 1000, 0)])
    article["article_id"] = "secret-article-id"
    article["title"] = "A Secret Title"

    rendered = str(_build([article]))

    assert "secret-article-id" not in rendered
    assert "A Secret Title" not in rendered


def test_bad_created_at_does_not_crash():
    stats = _build([_article("github-trending", [_call("model-a", 1000, 0)], created_at="not-a-date")])

    assert stats["totals"]["input_tokens"] == 1000
    assert sum(day["articles"] for day in stats["daily"]) == 0


# --- The 30-day table, read on a later day than it was built -----------------------------------


def _built_daily():
    return _build([_article("github-trending", [_call("model-a", 1000, 500)])])["daily"]


def test_rewindow_on_the_day_it_was_built_changes_nothing():
    daily = _built_daily()

    assert rewindow_daily(daily, today=TODAY) == daily


def test_rewindow_on_a_later_day_slides_the_window_and_fills_the_new_days_with_zeros():
    daily = _built_daily()

    moved = rewindow_daily(daily, today=TODAY + timedelta(days=2))

    assert len(moved) == 30
    assert [row["date"] for row in moved[-3:]] == ["2026-09-20", "2026-09-21", "2026-09-22"]
    assert moved[-3] == daily[-1] and moved[-3]["articles"] == 1
    assert moved[-1] == {
        "date": "2026-09-22",
        "articles": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cost_aud": 0.0,
    }
    assert moved[0]["date"] == daily[2]["date"]


def test_rewindow_long_after_the_last_build_is_all_zeros():
    moved = rewindow_daily(_built_daily(), today=TODAY + timedelta(days=45))

    assert len(moved) == 30
    assert all(row["articles"] == 0 and row["cost_aud"] == 0.0 for row in moved)
