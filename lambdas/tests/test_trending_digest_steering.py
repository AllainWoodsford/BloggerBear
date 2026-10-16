"""The Trending Everywhere digest's steering: no figures, nothing more specific than a theme and
its direction (common/digest.py's without_figures, held to in trending_digest_handler.py's
_compose_digest), and the prompt that asks the model for the same."""

from __future__ import annotations

import pytest

import trending_digest_handler
from common.digest import has_figure, without_figures


@pytest.mark.parametrize(
    "text",
    [
        "Bitcoin rose 4.2% on the day.",
        "The fund took in US$1.2 billion.",
        "Volume reached 12,500 contracts.",
        "It costs $5 a month.",
        "Returns of 3x were claimed.",
        "The repo gained 5k stars.",
        "About 15 percent of readers.",
        "A 12000-word thread.",
        "Priced at 0.25 per unit.",
        "Up 30 per cent since spring.",
        "USD 5 per token.",
        "EUR 10 a seat.",
        "It costs 5 dollars a month.",
        "Roughly twenty percent of readers.",
        "Two million users signed up.",
        "Half a cent per call.",
        "Several billion in inflows.",
    ],
)
def test_money_percentages_counts_and_big_numbers_are_figures(text):
    assert has_figure(text)


@pytest.mark.parametrize(
    "text",
    [
        "OpenCV 5 shipped this week.",
        "Python 3 is everywhere.",
        "A story from 2026 about three ships.",
        "Interest in small language models keeps growing.",
        "A third party audited the code.",
        "One more release landed.",
    ],
)
def test_names_with_a_digit_years_and_words_are_not(text):
    assert not has_figure(text)


def test_sentences_with_a_figure_are_dropped_and_the_rest_kept_in_order():
    text = (
        "Bitcoin rose 4.2% to US$61,000. Traders talked about regulation. Volume hit 12,500. "
        "OpenCV 5 shipped in 2026."
    )
    assert without_figures(text) == "Traders talked about regulation. OpenCV 5 shipped in 2026."
    assert without_figures("Up 30% on the week.") == ""
    assert without_figures("") == ""


def _contribution(topic_id, summary):
    return {"topic": {"topic_id": topic_id, "name": topic_id.title()}, "finding": {"summary": summary}}


def test_the_composed_digest_carries_no_figures_on_the_models_lines_or_the_fallback():
    contributions = [
        _contribution(
            "crypto", "Bitcoin rose 4.2% to US$61,000 on heavy volume. Traders talked about regulation."
        ),
        _contribution("github", "A repo gained 5k stars. Small language models are the theme of the week."),
    ]
    model_text = (
        "OVERVIEW: Attention is moving towards infrastructure. Spending rose 12% too.\n"
        "1: Prices moved sharply, up 4.2%. Regulation was the talk of the week.\n"
    )

    body = trending_digest_handler._compose_digest(contributions, model_text)

    assert body == (
        "Attention is moving towards infrastructure.\n\n"
        "## Crypto\n\nRegulation was the talk of the week.\n\n"
        "## Github\n\nSmall language models are the theme of the week."
    )


def test_a_line_that_was_only_figures_falls_back_to_the_finding_stripped_the_same_way():
    contributions = [_contribution("crypto", "Bitcoin rose 4.2%. Everyone watched the regulators.")]

    body = trending_digest_handler._compose_digest(contributions, "1: Up 4.2% on the day.")

    assert body == "## Crypto\n\nEveryone watched the regulators."


def test_a_finding_that_is_only_figures_leaves_the_heading_alone():
    contributions = [_contribution("crypto", "Bitcoin rose 4.2%. Ether fell 3%.")]
    assert trending_digest_handler._compose_digest(contributions, "") == "## Crypto"


def test_the_prompt_asks_for_a_general_survey_with_no_figures():
    prompt = trending_digest_handler._DIGEST_PROMPT_TEMPLATE
    assert "Do not include figures of any kind" in prompt
    assert "no prices, amounts, percentages, counts, rankings, dates or version numbers" in prompt
    assert "keep every line general" in prompt
    assert "A sentence with a figure in it will be removed before publication" in prompt
