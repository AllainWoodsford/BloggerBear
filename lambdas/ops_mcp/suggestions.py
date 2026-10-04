"""What the assistant may suggest: a fixed catalogue of admin_cli commands, one per kind of finding.

The model never writes, edits or chooses a command. It reads articles, review notes and log lines,
any of which can carry text someone wrote to steer it; if it could put a command on the operator's
screen, that text could too. So a finding names its *kind*, the kind picks its template here, and
the only thing filled in is an id read from a table and checked against ID_PATTERN. Nothing in the
catalogue deletes anything.

tests/test_ops_mcp_suggestions.py parses every command here with admin_cli's own argument parser,
so a renamed command or flag fails the build instead of handing the operator a command that errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ADMIN_CLI = "python scripts/admin_cli.py"

# Topic ids are slugs and article ids are UUIDs: letters, digits, "-" and "_". Anything else is
# refused, so an id can never carry a quote, a space or a second command into the template.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class Suggestion:
    """One catalogue entry. `arguments` is everything after ADMIN_CLI, with `{id}` where the
    finding's id goes (no `{id}` means the command takes none)."""

    action: str  # what to do, in a few words
    arguments: str
    what_it_does: str  # what running it changes, so the operator knows before they do


# Kinds of finding -> what to suggest. The keys are the only kinds a tool may report.
CATALOGUE: dict[str, Suggestion] = {
    "draft_truncated": Suggestion(
        action="Rewrite the article so it is finished",
        arguments='articles rewrite {id} -i "the draft was cut short"',
        what_it_does=(
            "Rewrites the article in the background, with your note as the thing to fix. "
            "The rewrite goes through the reviews again and comes back to the inbox."
        ),
    ),
    "awaiting_review": Suggestion(
        action="Go through the articles waiting for you",
        arguments="approve --source moderation",
        what_it_does=(
            "Shows each waiting article in turn: y approves, r rejects, z skips. "
            "Nothing changes until you press a key."
        ),
    ),
    "research_overdue": Suggestion(
        action="Run the topic's research now",
        arguments="topics trigger {id} --pipeline research_tick",
        what_it_does=(
            "Runs one research check for the topic and waits for it to finish. "
            "It writes findings, not an article."
        ),
    ),
    "no_article_today": Suggestion(
        action="Run the topic's daily cycle now",
        arguments="topics trigger {id} --pipeline daily_cycle",
        what_it_does=(
            "Writes an article from the findings since the topic's last one, if there are any, "
            "and publishes it or holds it for review as usual."
        ),
    ),
    "run_failed": Suggestion(
        action="Look at the runs that ran out of retries",
        arguments="failed-executions list",
        what_it_does="Lists the failed daily runs and their errors. It changes nothing.",
    ),
}


def suggest(kind: str, target_id: str | None = None) -> dict | None:
    """The suggestion for a finding of `kind` about `target_id`, ready to show: `action`,
    `command` and `what_it_does`. None when the kind is not in the catalogue, or its command
    needs an id and `target_id` is missing or fails ID_PATTERN -- a finding with no command is
    still worth reporting, a command built from an id we do not trust is not."""
    entry = CATALOGUE.get(kind)
    if entry is None:
        return None
    arguments = entry.arguments
    if "{id}" in arguments:
        if not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
            return None
        arguments = arguments.replace("{id}", target_id)
    return {
        "action": entry.action,
        "command": f"{ADMIN_CLI} {arguments}",
        "what_it_does": entry.what_it_does,
    }


def finding(kind: str, noticed: str, target_id: str | None = None, **where) -> dict:
    """A finding as a tool returns it: its kind, the id it is about, what was noticed (fixed
    words, written here in code), where (ids and labels for the card), and the suggestion, if
    the catalogue has one."""
    return {
        "kind": kind,
        "id": target_id,
        "noticed": noticed,
        "where": {key: value for key, value in where.items() if value is not None},
        "suggestion": suggest(kind, target_id),
    }
