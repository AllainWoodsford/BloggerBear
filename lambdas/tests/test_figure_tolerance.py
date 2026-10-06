"""Figures that move while an article is written (the crypto feed's prices), and how its
articles end.

An adapter declares a tolerance and how to write such figures (common/adapters/base.py). The
drafting prompt asks for approximate figures and a closing table and takeaways; the fresh-data
review does not flag a figure inside the tolerance (the prompt says so, and code drops one the
model flags anyway); a correction or a Re-Write may state an approximate figure; and the
standing disclaimer says the figures may be off. A topic whose adapter declares nothing is
checked exactly as before.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import daily_cycle_handler
from common import compliance, rewrite
from common import fresh_review as fr
from common.adapters.base import Adapter
from common.adapters.crypto_feed import CryptoFeedAdapter
from common.adapters.registry import ADAPTER_REGISTRY
from common.editorial_goals import EditorialGoal

CRYPTO = {
    "topic_id": "crypto",
    "name": "Crypto",
    "adapter": "crypto_feed",
    "adapter_config": {},
    "is_financial": True,
}
PLAIN = {"topic_id": "hn", "name": "Hacker News", "adapter": "hacker_news", "adapter_config": {}}
TOLERANCE = 0.07


def claim(text, evidence, problem="stale", severity="minor"):
    return {"claim": text, "problem": problem, "evidence": evidence, "severity": severity}


# --- what an adapter declares ---------------------------------------------------------------------


def test_the_crypto_feed_declares_a_tolerance_and_how_to_write_its_figures():
    rules = fr.writing_rules(CRYPTO)

    assert rules["figure_tolerance"] == pytest.approx(0.07)
    figures, shape = rules["figure_guidance"], rules["drafting_guidance"]
    assert "more than 30%" in figures and "at the time of checking" in figures
    assert "## At a glance" in shape and "markdown table" in shape and "## Key takeaways" in shape


def test_every_other_adapter_and_an_unknown_one_declare_nothing():
    quiet = {"figure_tolerance": 0.0, "figure_guidance": "", "drafting_guidance": ""}
    for key, adapter_cls in ADAPTER_REGISTRY.items():
        if adapter_cls is not CryptoFeedAdapter:
            assert fr.writing_rules({"adapter": key}) == quiet, key
    assert fr.writing_rules({"adapter": "no-such-adapter"}) == quiet
    assert fr.writing_rules({}) == quiet and fr.writing_rules(None) == quiet
    assert Adapter.figure_tolerance_percent == 0.0
    assert Adapter.figure_guidance == Adapter.drafting_guidance == ""


# --- the drafting prompt --------------------------------------------------------------------------


def _draft_prompt(topic, goal=None):
    reply = {"text": "Body.", "model_id": "m", "input_tokens": 1, "output_tokens": 1, "used_fallback": False}
    with patch("daily_cycle_handler.invoke_model_tracked", return_value=reply) as invoke:
        daily_cycle_handler._draft_article(topic, "an angle", "- a finding", "m", None, goal=goal)
    return invoke.call_args.args[0]


@pytest.mark.parametrize("goal", list(EditorialGoal))
def test_a_crypto_draft_is_asked_for_approximate_figures_and_a_summary_at_the_end(goal):
    prompt = _draft_prompt(CRYPTO, goal)

    assert "How articles on this topic are written (mandatory):" in prompt
    assert CryptoFeedAdapter.figure_guidance in prompt
    assert CryptoFeedAdapter.drafting_guidance in prompt
    # The financial rules are still there, and come first.
    financial = prompt.index(compliance.FINANCIAL_DRAFTING_GUIDANCE)
    assert financial < prompt.index(CryptoFeedAdapter.figure_guidance)


def test_a_draft_for_any_other_topic_is_asked_for_nothing_new():
    assert "How articles on this topic are written" not in _draft_prompt(PLAIN)


# --- the reviewer is told, and code holds it to it ------------------------------------------------


def test_the_reviewer_is_told_the_tolerance_only_when_there_is_one():
    plain = fr.build_review_prompt("Crypto", "draft", "findings", "evidence", "now")
    tolerant = fr.build_review_prompt("Crypto", "draft", "findings", "evidence", "now", figure_tolerance=0.07)

    assert "within about" not in plain
    assert "do NOT flag a number that is within about 7% of the fresh_data value" in tolerant
    assert "its direction (up or down) is wrong" in tolerant
    added = tolerant[tolerant.index(" Figures on this topic") : tolerant.index("\n\nReply")]
    assert tolerant.replace(added, "") == plain


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("Bitcoin is at $81,000", "fresh_data shows Bitcoin at $80,500"),
        ("Bitcoin trades around $81,000", "price 85,900"),  # 5.7% off
        ("Solana is up 36% over 3 months", "3-month change is now 34.2%"),
        ("ETH gained 12.0% in 7d", "change_7d: 11.6"),
        ("XRP fell 8% this week", "change_7d: -8.3"),
    ],
)
def test_a_figure_that_only_drifted_inside_the_tolerance_is_not_a_problem(text, evidence):
    assert fr.within_tolerance(claim(text, evidence), TOLERANCE) is True
    assert fr.within_tolerance(claim(text, evidence, problem="contradicted"), TOLERANCE) is True


@pytest.mark.parametrize(
    ("text", "evidence"),
    [
        ("Bitcoin is at $81,000", "fresh_data shows Bitcoin at $70,000"),  # 15.7% off
        ("Solana is up 36% over 3 months", "3-month change is now 20%"),
        ("ETH rose 5% today", "ETH fell 5% today"),  # the same figure, the other way
        ("ETH is up 5% on the day", "change_24h: -5.1"),
        ("Bitcoin is at $81,000 and Ethereum at $2,650", "Bitcoin 80,900"),  # one figure unchecked
        ("Bitcoin hit a record", "Bitcoin 80,900"),  # nothing to compare
        ("Bitcoin is at $81,000", ""),  # the reviewer gave no figure
    ],
)
def test_a_figure_that_is_really_off_or_cannot_be_checked_stays_flagged(text, evidence):
    assert fr.within_tolerance(claim(text, evidence), TOLERANCE) is False


def test_an_unsupported_claim_is_never_waved_through_and_no_tolerance_means_no_change():
    close = ("Bitcoin is at $81,000", "fresh_data shows $80,500")

    assert fr.within_tolerance(claim(*close, problem="unsupported"), TOLERANCE) is False
    assert fr.within_tolerance(claim(*close), 0.0) is False


class _Prices(Adapter):
    figure_tolerance_percent = 7.0

    def fetch_state(self, topic_config):
        return {}

    def material_diff(self, old_state, new_state):
        return False, ""

    def source_refs(self, new_state):
        return []

    def review_evidence(self, topic_config, latest_state):
        return '{"bitcoin": 80500}'


def _review(monkeypatch, adapter_cls, claims):
    monkeypatch.setitem(fr.ADAPTER_REGISTRY, "prices", adapter_cls)
    import json

    reply = {
        "text": json.dumps({"claims": claims}),
        "model_id": "m",
        "input_tokens": 1,
        "output_tokens": 1,
        "used_fallback": False,
    }
    with patch("common.fresh_review.invoke_model_tracked", return_value=reply) as invoke:
        record = fr.run_review(
            topic={"topic_id": "t", "name": "T", "adapter": "prices"},
            draft="Bitcoin is at $81,000.",
            findings_text="- Bitcoin at $81,000",
            latest_state=None,
            model_id="m",
            fallback_model_id=None,
            mode="enforce",
        )
    return record, invoke.call_args.args[0]


FLAGGED = [
    claim("Bitcoin is at $81,000", "fresh_data shows $80,500", severity="major"),
    claim("Ethereum is at $2,650", "fresh_data shows $2,100", severity="minor"),
]


def test_a_review_drops_what_is_inside_the_tolerance_and_says_how_many(monkeypatch):
    record, prompt = _review(monkeypatch, _Prices, FLAGGED)

    assert "within about 7%" in prompt
    assert [c["claim"] for c in record["claims"]] == ["Ethereum is at $2,650"]
    assert record["within_tolerance"] == 1
    # The one major claim was only drift: what is left is minor, so enforce mode corrects it
    # and no longer holds the article for a person.
    assert record["outcome"] == "minor"
    assert fr.enforcement_action(record, "hold") == ("revise", None)
    assert any("1 figure(s) had moved a little" in note for note in fr.review_notes(record))


def test_a_review_with_nothing_left_after_the_tolerance_is_clean(monkeypatch):
    record, _ = _review(monkeypatch, _Prices, FLAGGED[:1])

    assert record["outcome"] == "clean" and record["claims"] == [] and record["within_tolerance"] == 1


def test_an_adapter_with_no_tolerance_keeps_every_claim(monkeypatch):
    class Exact(_Prices):
        figure_tolerance_percent = 0.0

    record, prompt = _review(monkeypatch, Exact, FLAGGED)

    assert "within about" not in prompt
    assert len(record["claims"]) == 2 and record["outcome"] == "major"
    assert "within_tolerance" not in record


# --- a correction or a Re-Write may be approximate ------------------------------------------------

ORIGINAL = "## Heading\n\nSolana is up 36.2% over three months and Bitcoin trades at $81,744.\n\nMore here."


def _violations(new_body, tolerance):
    sources = ["- ethereum at $2,648.78"]
    return fr.revision_violations(
        "A Title", ORIGINAL, "A Title", new_body, sources=sources, figure_tolerance=tolerance
    )


@pytest.mark.parametrize(
    "sentence",
    [
        "Solana is up more than 30% over three months and Bitcoin trades around $80,000.",  # rounded down
        "Solana is up roughly 36% over three months and Bitcoin trades near $82,000.",  # rounded
        "Solana is up about 35% over three months and Bitcoin trades near $79,500.",  # within 7%
        "Solana is up 36.2% over three months and Ethereum is around $2,600.",
    ],
)
def test_an_approximate_figure_passes_when_the_topic_has_a_tolerance(sentence):
    body = f"## Heading\n\n{sentence}\n\nMore here."

    assert _violations(body, TOLERANCE) == []
    assert any("figure(s) found in none of the sources" in v for v in _violations(body, 0.0))


@pytest.mark.parametrize(
    "sentence",
    [
        "Solana is up more than 50% over three months and Bitcoin trades at $81,744.",  # rounded up, far
        "Solana is up 36.2% over three months and Bitcoin trades at $95,000.",
        "Solana is up 36.2% over three months, Bitcoin trades at $81,744 and Cardano is at $777.",
        "Solana is up more than 10% over three months and Bitcoin trades at $81,744.",  # says too little
    ],
)
def test_a_figure_that_approximates_nothing_is_still_refused(sentence):
    body = f"## Heading\n\n{sentence}\n\nMore here."

    assert any("figure(s) found in none of the sources" in v for v in _violations(body, TOLERANCE))


def test_the_correction_prompt_says_how_to_write_figures_only_when_the_adapter_does():
    args = ("Crypto", "T", "Body", [claim("a", "b")], "- finding", "evidence", "now")

    assert "How to write figures" not in fr.build_revision_prompt(*args)
    guided = fr.build_revision_prompt(*args, figure_guidance=CryptoFeedAdapter.figure_guidance)
    assert f"How to write figures: {CryptoFeedAdapter.figure_guidance}" in guided


def test_the_rewrite_prompt_carries_the_adapters_figure_rule_and_keeps_a_closing_summary():
    args = ("Crypto", "T", "Body", ["an issue"], "- finding", "evidence", "now")

    assert "How articles on this topic are written" not in rewrite.build_rewrite_prompt(*args)
    guidance = f"{CryptoFeedAdapter.figure_guidance}\n{rewrite._CLOSING_SECTIONS_RULE}"
    prompt = rewrite.build_rewrite_prompt(*args, writing_guidance=guidance)
    assert CryptoFeedAdapter.figure_guidance in prompt and "keep them and update them" in prompt
    # It comes after the rules and does not replace them.
    rules = prompt.index("do not add any claim, number, name or link")
    assert rules < prompt.index("How articles on this topic")


# --- the disclaimer -------------------------------------------------------------------------------


def test_the_financial_disclaimer_says_the_figures_may_be_off():
    text = compliance.FINANCIAL_DISCLAIMER

    assert "does not constitute financial or investment advice" in text
    assert "Figures are approximate" in text and "may be inaccurate or out of date" in text
    assert compliance.append_financial_disclaimer("Body.") == "Body." + text


def test_an_article_keeps_one_disclaimer_whichever_wording_it_was_published_with():
    (earlier,) = compliance._EARLIER_FINANCIAL_DISCLAIMERS

    assert compliance.strip_financial_disclaimer("Body." + compliance.FINANCIAL_DISCLAIMER) == ("Body.", True)
    assert compliance.strip_financial_disclaimer("Body." + earlier) == ("Body.", True)
    assert compliance.strip_financial_disclaimer("Body.") == ("Body.", False)
    # An earlier one is replaced by today's, never stacked under it.
    body, had = compliance.strip_financial_disclaimer("Body." + earlier)
    assert had and compliance.append_financial_disclaimer(body).count("informational purposes only") == 1
