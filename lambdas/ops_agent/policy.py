"""The orchestration policy: what the agent may do on one question, decided in code.

The model chooses which tools to call and in what order. It does not choose how many, it does not
choose from tools it was never shown, and it does not write what the page shows. Those are here,
in plain Python with no model and no agent framework, so they are tested as ordinary code
(tests/test_ops_agent_policy.py) and hold whatever the model says or reads.

**Two kinds of turn** (turn_kind): a *briefing* is the first question of a conversation, and a
*follow-up* is any question after it.

**The tool list is not written here.** The server says which tools exist; this module only names
the ones that are deep dives. A tool added to the server is offered without a change here.

**Deep dives** (DEEP_DIVE_TOOLS) are left out of the tools a briefing is given (offered). The
model cannot call what it was not given, and a call to one is refused here as well (Ledger.admit),
so the rule does not rest on the prompt or on the framework.

**The budget** (BUDGETS) is counted per question. A call over it is refused with a message the
model reads in place of a result, telling it to answer. A model that keeps asking anyway runs out
of turns (max_model_calls), and the answer is then put together from what the tools said
(Ledger.spoken): a question always gets an answer, and its cost has a ceiling.

**What the page shows comes from the tools, never from the model's text.** `findings`, and the
commands inside them, are copied from tool results as the server returned them. The model's
answer is only ever the words spoken. The one other thing of the model's that is shown is the
arguments it gave each tool call, and those are cut down to numbers and single words (_plain).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

BRIEFING = "briefing"
FOLLOW_UP = "follow_up"

# Tool calls allowed per question. A briefing goes wide and then follows leads; a follow-up is
# about one thing.
BUDGETS = {BRIEFING: 8, FOLLOW_UP: 3}

# Tools that read the largest and most hostile data in the account (today: the firewall's logs).
# Only when the operator asks, never as part of a briefing.
DEEP_DIVE_TOOLS = frozenset({"firewall_review"})

# Model calls allowed beyond one per tool call: one to read "the budget is spent", one to answer.
_MODEL_CALLS_OVER_BUDGET = 2

# A tool call's arguments are written by the model and shown on the page, so they are the one
# place its text could reach the screen as something other than the spoken answer. Only plain
# values are shown, and text only when it is a single word shaped like an id (a topic, a function
# name, a period): never a sentence, so never a command.
_ARGUMENT_WORD = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
_ARGUMENTS_MAX = 8


def turn_kind(history: list | None) -> str:
    """Which kind of turn a request is.

    The rule: a request is a **briefing** when its history holds no earlier turn at all, and a
    **follow-up** when it holds at least one. Nothing else is looked at: not the question's
    words, and not who spoke the earlier turns. The conversation lives in the browser tab, which
    sends the last few turns with each question, so "no history" is exactly "the first question
    asked in this tab".
    """
    return FOLLOW_UP if history else BRIEFING


def offered(tool_names: Iterable[str], turn: str) -> list[str]:
    """The tools the model is given on a turn of this kind, in the order the server listed them:
    all of them on a follow-up, all but the deep dives on a briefing."""
    if turn == FOLLOW_UP:
        return list(tool_names)
    return [name for name in tool_names if name not in DEEP_DIVE_TOOLS]


def max_model_calls(turn: str) -> int:
    """The most times the model may be called for one question: the ceiling on what a question
    costs, even if the model spends every call on one tool and never stops asking for more."""
    return BUDGETS[turn] + _MODEL_CALLS_OVER_BUDGET


def _plain(arguments: Any) -> dict:
    """A tool call's arguments as the page may show them: numbers, booleans and single words.
    Anything else is left out of what is shown (the tool itself still receives what the model
    sent, and the server checks it)."""
    plain: dict = {}
    if not isinstance(arguments, Mapping):
        return plain
    for key, value in list(arguments.items())[:_ARGUMENTS_MAX]:
        if not isinstance(key, str) or not _ARGUMENT_WORD.match(key):
            continue
        if value is None or isinstance(value, bool | int | float):
            plain[key] = value
        elif isinstance(value, str) and _ARGUMENT_WORD.match(value):
            plain[key] = value
    return plain


class Ledger:
    """One question's account: the calls allowed and made, and what the tools returned.

    The agent asks `admit` before every tool call and gives `record` every result. Nothing in
    here is kept after the answer is sent.
    """

    def __init__(self, turn: str, tool_names: Iterable[str]) -> None:
        self.turn = turn
        self.budget = BUDGETS[turn]
        self.offered = frozenset(offered(tool_names, turn))
        self.tool_calls: list[dict] = []  # the calls made, in order: {"name", "arguments"}
        self.findings: list[dict] = []  # de-duplicated by (kind, id), in the order found
        self.spoken: list[str] = []  # each result's own summary, written by the server's code
        self.refused = 0
        self._seen: set[tuple] = set()

    def admit(self, name: str, arguments: Any = None) -> str | None:
        """Decide one tool call. None means go ahead, and the call is counted against the
        budget. Otherwise the text is the refusal, which the model reads in place of a result."""
        if name not in self.offered:
            self.refused += 1
            if name in DEEP_DIVE_TOOLS:
                return (
                    f"{name} is a deep dive and is not part of a briefing. Do not call it. Tell "
                    "the operator they can ask about it as a follow-up question."
                )
            return f"There is no tool called {name} on this turn. Answer from what you have."
        if len(self.tool_calls) >= self.budget:
            self.refused += 1
            return (
                f"The budget of {self.budget} tool calls for this question is spent. Do not call "
                "any more tools: answer now, from what the tools have already returned."
            )
        self.tool_calls.append({"name": name, "arguments": _plain(arguments)})
        return None

    def record(self, structured: Any) -> None:
        """Take what one tool call returned (its `structuredContent`): its findings, minus any
        already collected, and its `spoken` summary. Anything not shaped like a tool's result is
        ignored: only what the server's code built reaches the page."""
        if not isinstance(structured, Mapping):
            return
        spoken = structured.get("spoken")
        if isinstance(spoken, str) and spoken.strip() and spoken.strip() not in self.spoken:
            self.spoken.append(spoken.strip())
        findings = structured.get("findings")
        if not isinstance(findings, list):
            return
        for finding in findings:
            if not isinstance(finding, Mapping) or not isinstance(finding.get("kind"), str):
                continue
            finding_id = finding.get("id")
            key = (finding["kind"], finding_id if isinstance(finding_id, str) else None)
            if key in self._seen:
                continue
            self._seen.add(key)
            self.findings.append(dict(finding))


def suggested_fixes(findings: Iterable[Any]) -> int:
    """How many findings carry a command for the operator to run: the "suggested fixes on
    screen". A finding's `suggestion` has three shapes, and only the first counts:

    - an action with a `command`: something to run;
    - an action with `command: None` (an alarm, an incident, unusual spend): something to look
      at, with nothing to run;
    - None: a kind that has a command, about an id that failed the server's check.

    All three are passed to the page unchanged; this only counts."""
    count = 0
    for finding in findings:
        suggestion = finding.get("suggestion") if isinstance(finding, Mapping) else None
        command = suggestion.get("command") if isinstance(suggestion, Mapping) else None
        if isinstance(command, str) and command.strip():
            count += 1
    return count
