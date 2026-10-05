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


# The three shapes a finding's suggestion has (ops_mcp/suggestions.py).
RUN_THIS = finding("draft_truncated", "a1", "python scripts/admin_cli.py articles rewrite a1")
LOOK_AT_THIS = {
    **finding("alarm", "bloggerbear-dev-dlq-depth"),
    "suggestion": {"action": "Look at the alarm on the dashboard", "command": None, "what_it_does": None},
}
NO_SUGGESTION = finding("no_article_today", "not a valid id")  # the id failed the server's check


def test_a_finding_is_passed_on_unchanged_whatever_its_suggestion():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)
    returned = [RUN_THIS, LOOK_AT_THIS, NO_SUGGESTION]

    ledger.record({"spoken": "Three things.", "findings": returned})

    assert ledger.findings == returned
    assert ledger.findings[1]["suggestion"] == {
        "action": "Look at the alarm on the dashboard",
        "command": None,
        "what_it_does": None,
    }
    assert ledger.findings[2]["suggestion"] is None


def test_only_a_suggestion_with_a_command_counts_as_a_suggested_fix():
    assert policy.suggested_fixes([RUN_THIS, LOOK_AT_THIS, NO_SUGGESTION]) == 1
    assert policy.suggested_fixes([LOOK_AT_THIS, NO_SUGGESTION]) == 0
    assert policy.suggested_fixes([]) == 0


@pytest.mark.parametrize(
    "suggestion",
    [
        None,
        {"action": "Look", "command": None, "what_it_does": None},
        {"action": "Look", "command": "", "what_it_does": None},
        {"action": "Look", "command": "   "},
        {"action": "Look"},
        {"action": "Look", "command": 7},
        "python scripts/admin_cli.py topics delete crypto",
    ],
)
def test_a_suggestion_with_no_usable_command_is_not_counted(suggestion):
    assert policy.suggested_fixes([{**RUN_THIS, "suggestion": suggestion}, "not a finding"]) == 0


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

    assert ledger.findings == [] and ledger.spoken == [] and ledger.tables == []


# --- how-to cards and tables ---------------------------------------------------------------------


def test_a_how_to_card_has_a_command_but_is_not_counted_as_a_fix():
    help_card = {**RUN_THIS, "kind": "how_to", "id": "help-topics-update", "help": "usage: ..."}

    assert policy.suggested_fixes([help_card]) == 0
    assert policy.suggested_fixes([help_card, RUN_THIS]) == 1


def test_a_how_to_card_is_passed_to_the_page_with_its_help_and_its_warning():
    ledger = policy.Ledger(policy.FOLLOW_UP, SERVER_TOOLS)
    template = {
        "kind": "how_to",
        "id": "topics-delete-template",
        "noticed": "topics delete: Delete a topic",
        "where": {"command": "topics delete"},
        "suggestion": {"action": "Fill it in", "command": "x topics delete <topic_id>", "what_it_does": "y"},
        "destructive": True,
        "warning": "A template.",
        "help": "usage: admin_cli.py topics delete [-h] topic_id\n",
    }

    ledger.record({"spoken": "On screen.", "findings": [template, dict(template)]})

    assert ledger.findings == [template]  # unchanged, and once


def test_a_table_is_collected_from_a_result_as_plain_cells():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)
    table = {"title": "Topics (2)", "columns": ["Name", "Runs"], "rows": [["Crypto", 3], ["HN", 2.5]]}

    ledger.record({"spoken": "Two topics.", "findings": [], "table": table})
    ledger.record({"spoken": "Two topics.", "findings": [], "table": table})  # the same one again

    assert ledger.tables == [{**table, "rows_left_out": 0}]


def test_a_table_is_cut_to_size_and_its_cells_to_text_and_numbers():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)
    columns = [f"c{n}" for n in range(policy.TABLE_MAX_COLUMNS + 4)]
    rows = [[f"r{n}", True, None, ["a", "list"], {"an": "object"}, "x" * 1000] for n in range(60)]

    ledger.record({"table": {"title": "T" * 500, "columns": columns, "rows": [*rows, "not a row"]}})

    (table,) = ledger.tables
    assert len(table["title"]) == policy.TABLE_TITLE_MAX_CHARS
    assert len(table["columns"]) == policy.TABLE_MAX_COLUMNS
    assert len(table["rows"]) == policy.TABLE_MAX_ROWS and table["rows_left_out"] == 11
    first = table["rows"][0]
    assert len(first) == policy.TABLE_MAX_COLUMNS  # padded out to the columns
    assert first[:5] == ["r0", "yes", "", "", ""]
    assert len(first[5]) == policy.TABLE_CELL_MAX_CHARS
    for row in table["rows"]:
        assert all(isinstance(cell, str | int | float) and not isinstance(cell, bool) for cell in row)


@pytest.mark.parametrize(
    "block",
    [None, "a table", [], {}, {"columns": [], "rows": []}, {"columns": "N", "rows": []}, {"columns": ["a"]}],
)
def test_something_that_is_not_a_table_is_not_collected(block):
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    ledger.record({"spoken": "x", "findings": [], "table": block})

    assert ledger.tables == []


def test_at_most_a_few_tables_are_collected():
    ledger = policy.Ledger(policy.BRIEFING, SERVER_TOOLS)

    for number in range(policy.TABLES_MAX + 3):
        ledger.record({"table": {"title": f"T{number}", "columns": ["a"], "rows": [[number]]}})

    assert [table["title"] for table in ledger.tables] == [f"T{n}" for n in range(policy.TABLES_MAX)]


def test_a_how_to_question_changes_neither_the_turn_rule_nor_the_budgets():
    assert policy.turn_kind(None) == policy.BRIEFING and policy.turn_kind([]) == policy.BRIEFING
    assert policy.BUDGETS == {policy.BRIEFING: 8, policy.FOLLOW_UP: 3}
    guide = ["cli_help", "cli_guides", "cli_command", "cli_reference", "topics_overview"]
    names = [*SERVER_TOOLS, *guide]
    # The guide tools are ordinary tools: offered on a first question and on a later one.
    assert set(guide) <= set(policy.offered(names, policy.BRIEFING))
    assert set(guide) <= set(policy.offered(names, policy.FOLLOW_UP))
    assert not set(policy.DEEP_DIVE_TOOLS) & set(policy.offered(names, policy.BRIEFING))
