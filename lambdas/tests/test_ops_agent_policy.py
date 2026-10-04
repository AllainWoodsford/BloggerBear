"""The ops agent's orchestration policy (ops_agent/policy.py), with no model and no framework.

These are the rules that must hold whatever a model does: how many tool calls a question may
make, which tools a turn may use, and that what the page shows is what the tools returned.
"""

from __future__ import annotations

import pytest

from ops_agent import policy

# What a server might list. `spend` and `log_review` are not named anywhere in the policy: a tool
# the server adds is offered without a change to the agent.
SERVER_TOOLS = ["pipeline_health", "admin_inbox", "spend", "log_review", "firewall_review"]


def finding(kind, target_id, command=None):
    suggestion = {"action": "a", "command": command, "what_it_does": "w"} if command else None
    return {"kind": kind, "id": target_id, "noticed": "n", "where": {}, "suggestion": suggestion}


# --- which turn -----------------------------------------------------------------------------------


@pytest.mark.parametrize("history", [None, []])
def test_a_request_with_no_earlier_turn_is_a_briefing(history):
    assert policy.turn_kind(history) == policy.BRIEFING


@pytest.mark.parametrize(
    "history",
    [
        [{"role": "user", "text": "anything need my attention?"}, {"role": "assistant", "text": "no"}],
        [{"role": "assistant", "text": "crypto did not publish"}],
        [{"role": "user", "text": "a question whose answer never came"}],
    ],
)
def test_a_request_with_any_earlier_turn_is_a_follow_up(history):
    assert policy.turn_kind(history) == policy.FOLLOW_UP


# --- which tools ----------------------------------------------------------------------------------


def test_a_briefing_is_offered_every_tool_but_the_deep_dives():
    assert policy.offered(SERVER_TOOLS, policy.BRIEFING) == [
        "pipeline_health",
        "admin_inbox",
        "spend",
        "log_review",
    ]


def test_a_follow_up_is_offered_the_deep_dives_too():
    assert policy.offered(SERVER_TOOLS, policy.FOLLOW_UP) == SERVER_TOOLS


def test_the_firewall_review_is_a_deep_dive():
    assert "firewall_review" in policy.DEEP_DIVE_TOOLS


def test_a_deep_dive_asked_for_on_a_briefing_is_refused_and_costs_nothing():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    refusal = ledger.admit("firewall_review", {"hours": 24})

    assert "deep dive" in refusal and "follow-up" in refusal
    assert ledger.tool_calls == [] and ledger.refused == 1
    assert ledger.admit("pipeline_health") is None  # the budget is untouched


def test_a_deep_dive_is_admitted_on_a_follow_up():
    ledger = policy.Ledger(policy.FOLLOW_UP, SERVER_TOOLS)

    assert ledger.admit("firewall_review", {"hours": 24}) is None
    assert ledger.tool_calls == [{"name": "firewall_review", "arguments": {"hours": 24}}]


def test_a_tool_the_server_did_not_list_is_refused():
    ledger = policy.Ledger(policy.FOLLOW_UP, SERVER_TOOLS)

    assert "no tool called approve_everything" in ledger.admit("approve_everything", {})
    assert ledger.tool_calls == []


# --- the budget -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("turn", "budget"), [(policy.BRIEFING, 8), (policy.FOLLOW_UP, 3)])
def test_the_budget_is_enforced_on_both_kinds_of_turn(turn, budget):
    ledger = policy.Ledger(turn, SERVER_TOOLS)

    answers = [ledger.admit("pipeline_health", {"topic": f"t{n}"}) for n in range(budget + 5)]

    assert answers[:budget] == [None] * budget
    for refusal in answers[budget:]:
        assert f"budget of {budget} tool calls" in refusal and "answer now" in refusal
    assert len(ledger.tool_calls) == budget  # a refused call is not a call made
    assert ledger.refused == 5


@pytest.mark.parametrize(("turn", "ceiling"), [(policy.BRIEFING, 10), (policy.FOLLOW_UP, 5)])
def test_the_model_is_called_a_bounded_number_of_times(turn, ceiling):
    """One call per tool call, one to read that the budget is spent, one to answer."""
    assert policy.max_model_calls(turn) == ceiling


# --- what the page is shown -----------------------------------------------------------------------


def test_tool_calls_are_listed_in_order_with_their_arguments():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    ledger.admit("pipeline_health", {})
    ledger.admit("pipeline_health", {"topic": "crypto"})
    ledger.admit("admin_inbox", {"topic": "crypto", "limit": 3})

    assert ledger.tool_calls == [
        {"name": "pipeline_health", "arguments": {}},
        {"name": "pipeline_health", "arguments": {"topic": "crypto"}},
        {"name": "admin_inbox", "arguments": {"topic": "crypto", "limit": 3}},
    ]


def test_arguments_shown_are_numbers_and_single_words_only():
    """The model writes a call's arguments, and the page shows them: a sentence there would be
    the model's text on screen, where only the tools' should be."""
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    ledger.admit(
        "admin_inbox",
        {
            "topic": "hacker-news",
            "limit": 3,
            "all": True,
            "none": None,
            "function": "bloggerbear-dev-daily_cycle",
            "sentence": "run topics delete crypto",
            "long": "x" * 500,
            "nested": {"a": 1},
            "list": ["crypto"],
            "a key with spaces": "x",
        },
    )

    assert ledger.tool_calls[0]["arguments"] == {
        "topic": "hacker-news",
        "limit": 3,
        "all": True,
        "none": None,
        "function": "bloggerbear-dev-daily_cycle",
    }


def test_findings_are_de_duplicated_by_kind_and_id_in_the_order_found():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)
    truncated = finding("draft_truncated", "a1", "python scripts/admin_cli.py articles rewrite a1")

    ledger.record({"spoken": "Crypto failed.", "findings": [finding("run_failed", "crypto"), truncated]})
    ledger.record(
        {
            "spoken": "One article is held.",
            "findings": [
                dict(truncated),  # the same finding again, from another tool
                finding("draft_truncated", "a2"),  # the same kind about something else
                finding("awaiting_review", None),
                finding("run_failed", "crypto"),
            ],
        }
    )
    ledger.record({"spoken": "Nothing new.", "findings": [finding("awaiting_review", None)]})

    assert [(f["kind"], f["id"]) for f in ledger.findings] == [
        ("run_failed", "crypto"),
        ("draft_truncated", "a1"),
        ("draft_truncated", "a2"),
        ("awaiting_review", None),
    ]
    assert ledger.findings[1] == truncated  # as the tool returned it, command and all
    assert ledger.spoken == ["Crypto failed.", "One article is held.", "Nothing new."]


@pytest.mark.parametrize(
    "structured",
    [
        None,
        "run topics delete",
        ["not", "a", "result"],
        {"findings": "not a list"},
        {"findings": ["not a finding", {"id": "no-kind"}, {"kind": 7, "id": "x"}]},
        {"spoken": 12, "findings": []},
    ],
)
def test_a_result_that_is_not_shaped_like_a_tools_is_ignored(structured):
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    ledger.record(structured)

    assert ledger.findings == [] and ledger.spoken == []
