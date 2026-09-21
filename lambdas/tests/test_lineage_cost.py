"""Model-id normalisation, fallback pricing, the research tally, and the lineage
audit/backfill -- the pieces that keep lineage cost from silently going blank."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from common import costing, lineage_tools, model_pricing
from common.bedrock import invoke_model_tracked

PROFILE = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
ARN = f"arn:aws:bedrock:ap-southeast-2:547610822592:inference-profile/{PROFILE}"


@pytest.fixture(autouse=True)
def _empty_registry():
    """By default the Models registry has nothing in it -- the state that left every
    new article's cost blank. Tests that want a registry row patch it themselves."""
    with patch("common.costing.get_model", return_value=None) as mock_get:
        yield mock_get


def _call(model_id=PROFILE, input_tokens=1000, output_tokens=1000, stage="draft"):
    return {"stage": stage, "model_id": model_id, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "used_fallback": False}


# --- model ids -------------------------------------------------------------------


def test_an_inference_profile_arn_reduces_to_its_profile_id():
    assert model_pricing.canonical_model_id(ARN) == PROFILE


def test_a_foundation_model_arn_reduces_to_its_model_id():
    arn = "arn:aws:bedrock:ap-southeast-2::foundation-model/anthropic.claude-haiku-4-5-20251001-v1:0"

    assert model_pricing.canonical_model_id(arn) == "anthropic.claude-haiku-4-5-20251001-v1:0"


@pytest.mark.parametrize(
    "value",
    [
        PROFILE,
        "anthropic.claude-haiku-4-5-20251001-v1:0",
        # an application profile's id says nothing about the model behind it: leave it alone
        "arn:aws:bedrock:ap-southeast-2:1:application-inference-profile/abc123",
        None,
        5,
    ],
)
def test_anything_else_is_left_unchanged(value):
    assert model_pricing.canonical_model_id(value) == value


@pytest.mark.parametrize("prefix", ["au.", "apac.", "us.", "eu.", "global.", ""])
def test_the_base_model_id_drops_a_geo_prefix(prefix):
    assert model_pricing.base_model_id(f"{prefix}anthropic.claude-haiku-4-5-20251001-v1:0") == (
        "anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_the_builtin_price_serves_every_geo_profile_of_a_model_and_the_arn():
    for model_id in (ARN, PROFILE, "apac.anthropic.claude-haiku-4-5-20251001-v1:0"):
        entry = model_pricing.default_model_entry(model_id)
        assert entry["input_price_usd_per_1k_tokens"] == 0.001
        assert entry["output_price_usd_per_1k_tokens"] == 0.005
        assert entry["model_id"] == model_pricing.canonical_model_id(model_id)


def test_an_unknown_model_has_no_builtin_price():
    assert model_pricing.default_model_entry("acme.mystery-model-v1") is None


def test_model_label_prefers_the_registry_then_the_builtin_then_the_id():
    assert model_pricing.model_label(PROFILE, {"display_name": "My Haiku"}) == "My Haiku"
    assert model_pricing.model_label(ARN) == "Claude Haiku 4.5"
    assert model_pricing.model_label("acme.mystery-model-v1") == "acme.mystery-model-v1"


# --- price resolution --------------------------------------------------------------


def test_the_registry_wins_over_the_builtin_price(_empty_registry):
    _empty_registry.return_value = {
        "model_id": PROFILE, "input_price_usd_per_1k_tokens": 0.002, "output_price_usd_per_1k_tokens": 0.01,
    }

    assert costing.pricing_for(PROFILE)["input_price_usd_per_1k_tokens"] == 0.002


def test_a_row_registered_under_the_profile_id_matches_a_call_recorded_by_arn(_empty_registry):
    row = {
        "model_id": PROFILE,
        "input_price_usd_per_1k_tokens": 0.002,
        "output_price_usd_per_1k_tokens": 0.01,
    }
    _empty_registry.side_effect = lambda model_id: row if model_id == PROFILE else None

    assert costing.pricing_for(ARN) is row


def test_a_registry_row_with_no_price_falls_through_to_the_builtin(_empty_registry):
    _empty_registry.return_value = {"model_id": PROFILE, "display_name": "Haiku"}

    assert costing.pricing_for(PROFILE)["input_price_usd_per_1k_tokens"] == 0.001


def test_cost_is_computed_with_an_empty_registry_and_the_real_arn_id():
    """The regression this change exists for: BEDROCK_MODEL_ID is an ARN and nothing
    seeds the registry, and cost used to come out None for every new article."""
    cost, note = costing.calculate_lineage_cost_aud([_call(ARN, 1000, 1000)])

    assert note is None
    assert cost == pytest.approx((0.001 + 0.005) * costing.USD_TO_AUD_RATE)


def test_an_unpriced_model_gives_no_cost_says_so_and_logs(capsys):
    cost, note = costing.calculate_lineage_cost_aud([_call("acme.mystery-model-v1")])

    assert cost is None
    assert note == "pricing not available for acme.mystery-model-v1"
    assert "no price for model 'acme.mystery-model-v1'" in capsys.readouterr().out


def test_one_unpriced_call_does_not_produce_a_partial_total():
    cost, _ = costing.calculate_lineage_cost_aud([_call(PROFILE), _call("acme.mystery-model-v1")])

    assert cost is None


def test_the_note_names_the_canonical_id_not_the_arn():
    arn = "arn:aws:bedrock:ap-southeast-2:1:inference-profile/x.y"

    _, note = costing.calculate_lineage_cost_aud([_call(arn)])

    assert note == "pricing not available for x.y"


# --- lineage --------------------------------------------------------------------------


def test_build_lineage_records_canonical_ids_and_readable_labels():
    lineage = costing.build_lineage([_call(ARN, 1000, 1000)])

    assert lineage["models_used"] == [PROFILE]
    assert lineage["calls"][0]["model_id"] == PROFILE
    assert lineage["model_labels"] == {PROFILE: "Claude Haiku 4.5"}
    assert lineage["cost_aud"] is not None and lineage["cost_note"] is None
    assert "research" not in lineage and "total_cost_aud" not in lineage


def _finding(captured_at="2026-09-21T01:00:00+00:00", **research_call):
    """A Finding that carries the Bedrock call behind its summary (overridable)."""
    return {
        "topic_id": "t",
        "captured_at": captured_at,
        "summary": "s",
        "research_call": {
            "model_id": PROFILE,
            "input_tokens": 400,
            "output_tokens": 100,
            "used_fallback": False,
            **research_call,
        },
    }


def test_the_research_tally_sums_every_findings_call():
    research = costing.build_research_lineage([_finding(), _finding("2026-09-21T02:00:00+00:00")])

    assert research["findings"] == research["tracked_findings"] == 2
    assert research["untracked_findings"] == 0
    assert (research["input_tokens"], research["output_tokens"]) == (800, 200)
    assert research["models_used"] == [PROFILE]
    assert research["cost_aud"] == pytest.approx((0.8 * 0.001 + 0.2 * 0.005) * costing.USD_TO_AUD_RATE)
    assert research["cost_note"] is None
    assert [c["stage"] for c in research["calls"]] == ["research", "research"]
    assert research["calls"][1]["captured_at"] == "2026-09-21T02:00:00+00:00"


def test_the_research_tally_reads_the_decimals_dynamodb_returns():
    finding = _finding(input_tokens=Decimal("400"), output_tokens=Decimal("100"))

    research = costing.build_research_lineage([finding])

    assert research["input_tokens"] == 400 and isinstance(research["input_tokens"], int)


def test_findings_from_before_research_tracking_are_counted_not_treated_as_free():
    older = {"topic_id": "t", "captured_at": "2026-09-20T01:00:00+00:00", "summary": "s"}

    research = costing.build_research_lineage([older, _finding()])

    assert (research["findings"], research["tracked_findings"], research["untracked_findings"]) == (2, 1, 1)
    assert research["cost_note"] == "excludes 1 finding(s) recorded before research tracking"
    assert research["cost_aud"] is not None  # the tracked call is still priced


def test_a_window_of_only_untracked_findings_has_no_calls_and_says_why():
    older = {"topic_id": "t", "captured_at": "2026-09-20T01:00:00+00:00", "summary": "s"}

    research = costing.build_research_lineage([older])

    assert research["calls"] == [] and research["input_tokens"] == 0
    assert research["cost_aud"] == 0.0
    assert "excludes 1 finding(s)" in research["cost_note"]


def test_a_window_with_no_findings_is_an_empty_tally():
    research = costing.build_research_lineage([])

    assert research["findings"] == 0 and research["calls"] == [] and research["cost_note"] is None


def test_an_unpriced_research_model_is_noted_and_keeps_no_cost():
    research = costing.build_research_lineage([_finding(model_id="acme.mystery-model-v1")])

    assert research["cost_aud"] is None
    assert research["cost_note"] == "pricing not available for acme.mystery-model-v1"


def test_the_total_is_authoring_plus_research():
    research = costing.build_research_lineage([_finding()])

    lineage = costing.build_lineage([_call(PROFILE, 1000, 1000)], research=research)

    assert lineage["research"] is research
    assert lineage["total_cost_aud"] == pytest.approx(lineage["cost_aud"] + research["cost_aud"])


def test_the_total_is_unknown_when_either_part_is_unknown():
    priced = costing.build_research_lineage([_finding()])
    unpriced = costing.build_research_lineage([_finding(model_id="acme.mystery-model-v1")])

    assert costing.build_lineage([_call("acme.mystery-model-v1")], research=priced)["total_cost_aud"] is None
    assert costing.build_lineage([_call(PROFILE)], research=unpriced)["total_cost_aud"] is None


def test_labels_cover_models_used_only_for_research():
    research = costing.build_research_lineage([_finding(model_id="acme.mystery-model-v1")])

    lineage = costing.build_lineage([_call(PROFILE)], research=research)

    assert set(lineage["model_labels"]) == {PROFILE, "acme.mystery-model-v1"}


# --- the tracked Bedrock call ---------------------------------------------------------


def test_a_tracked_call_invokes_by_the_configured_id_but_records_the_canonical_one():
    class _Client:
        invoked = []

        def converse(self, **kwargs):
            self.invoked.append(kwargs["modelId"])
            return {
                "output": {"message": {"content": [{"text": "hi"}]}},
                "usage": {"inputTokens": 7, "outputTokens": 3},
            }

    client = _Client()
    with patch("common.bedrock._get_client", return_value=client):
        result = invoke_model_tracked("prompt", ARN)

    assert client.invoked == [ARN]  # the ARN is still what Bedrock is called with
    assert result["model_id"] == PROFILE
    assert (result["input_tokens"], result["output_tokens"], result["used_fallback"]) == (7, 3, False)


# --- audit and backfill ----------------------------------------------------------------


def _article(article_id, lineage, status="published"):
    return {"article_id": article_id, "topic_id": "t", "status": status, "lineage": lineage}


def _legacy_lineage_recorded_by_arn():
    """What the tokenized-gold article looked like: tokens recorded, cost blank."""
    return {
        "calls": [_call(ARN, 1485, 194, "ideation"), _call(ARN, 1424, 1024, "draft")],
        "total_input_tokens": 2909,
        "total_output_tokens": 1218,
        "models_used": [ARN],
        "cost_aud": None,
        "cost_note": f"pricing not available for {ARN}",
    }


def test_recompute_prices_a_blank_cost_from_the_stored_tokens():
    updated = lineage_tools.recompute_lineage(_legacy_lineage_recorded_by_arn())

    expected_usd = (2909 / 1000) * 0.001 + (1218 / 1000) * 0.005
    assert updated["cost_aud"] == pytest.approx(expected_usd * costing.USD_TO_AUD_RATE)
    assert updated["cost_note"] is None
    assert updated["models_used"] == [PROFILE]
    assert [c["model_id"] for c in updated["calls"]] == [PROFILE, PROFILE]
    assert updated["model_labels"] == {PROFILE: "Claude Haiku 4.5"}
    # what happened is untouched
    assert (updated["total_input_tokens"], updated["total_output_tokens"]) == (2909, 1218)


def test_recompute_re_prices_a_stored_research_block():
    lineage = _legacy_lineage_recorded_by_arn()
    lineage["research"] = {
        "findings": 3, "tracked_findings": 2, "untracked_findings": 1,
        "calls": [_call(ARN, 400, 100, "research"), _call(ARN, 400, 100, "research")],
        "input_tokens": 800, "output_tokens": 200, "models_used": [ARN],
        "cost_aud": None, "cost_note": "pricing not available; excludes 1 finding(s)",
    }

    updated = lineage_tools.recompute_lineage(lineage)

    research = updated["research"]
    assert research["cost_aud"] is not None
    assert research["cost_note"] == "excludes 1 finding(s) recorded before research tracking"
    assert (research["findings"], research["untracked_findings"]) == (3, 1)
    assert updated["total_cost_aud"] == pytest.approx(updated["cost_aud"] + research["cost_aud"])


def test_recompute_is_a_no_op_on_lineage_that_is_already_right():
    once = lineage_tools.recompute_lineage(_legacy_lineage_recorded_by_arn())

    assert lineage_tools.recompute_lineage(once) == once


def test_the_backfill_plan_lists_only_articles_with_calls_and_flags_which_change():
    fixed = lineage_tools.recompute_lineage(_legacy_lineage_recorded_by_arn())
    articles = [
        _article("needs-fixing", _legacy_lineage_recorded_by_arn()),
        _article("already-right", fixed),
        _article("no-lineage", None),
        _article("empty-lineage", {"calls": []}),
    ]

    plan = lineage_tools.plan_backfill(articles)

    assert [(p["article_id"], p["changed"]) for p in plan] == [
        ("needs-fixing", True),
        ("already-right", False),
    ]
    assert plan[0]["cost_aud_before"] is None and plan[0]["cost_aud_after"] is not None


def test_the_audit_reports_what_is_missing_without_content():
    articles = [
        _article("ok", lineage_tools.recompute_lineage(_legacy_lineage_recorded_by_arn())),
        _article("no-cost", _legacy_lineage_recorded_by_arn()),
        _article("old", None),
    ]
    articles[0]["lineage"]["research"] = costing.build_research_lineage([_finding()])

    audit = lineage_tools.audit_lineage(articles)

    assert audit["articles"] == 3 and audit["with_lineage"] == 2
    assert audit["without_lineage"] == ["old"]
    assert audit["cost_missing"] == ["no-cost"]
    assert audit["without_research_tally"] == ["no-cost"]
    assert audit["models_used"][PROFILE] >= 4
    assert audit["unpriced_models"] == []


def test_the_audit_names_models_nothing_can_price():
    lineage = costing.build_lineage([_call("acme.mystery-model-v1")])

    audit = lineage_tools.audit_lineage([_article("a", lineage)])

    assert audit["unpriced_models"] == ["acme.mystery-model-v1"]
    assert audit["cost_missing"] == ["a"]
