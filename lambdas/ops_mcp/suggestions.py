"""What the assistant may suggest: a fixed catalogue, one entry per kind of finding. Most entries are
an admin_cli command; some have no command, only what to look at.

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

from common.naming import NAME_PREFIX

ADMIN_CLI = "python scripts/admin_cli.py"
DOUBLE_CHECK_WARNING = (
    "Suggested by the assistant: double-check it is the right article or topic before you run it. "
    "Nothing runs until you do."
)

# Topic ids are slugs and article ids are UUIDs: letters, digits, "-" and "_". Anything else is
# refused, so an id can never carry a quote, a space or a second command into the template.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True)
class Suggestion:
    """One catalogue entry. `arguments` is everything after ADMIN_CLI, with `{id}` where the
    finding's id goes (no `{id}` means the command takes none). An entry with no `arguments` has
    no command: nothing in admin_cli fixes that kind of finding, so `action` says what to look at
    and where."""

    action: str  # what to do, in a few words
    arguments: str | None = None
    what_it_does: str | None = None  # what running it changes, so the operator knows before they do


_REWRITE_PUBLISHED = (
    "Rewrites the article in the background while it stays up, with your note as the thing to "
    "fix. When the rewrite is ready the article comes down, with its musings, and the rewrite "
    "waits in the inbox. If the rewrite fails, nothing changes."
)

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
    # Published things that look wrong (content.py). The words after -i are fixed per kind, for
    # the operator to edit before running: never the title, the body or the musing itself.
    "musing_no_text": Suggestion(
        action="Rewrite the article; its musing is replaced when it is republished",
        arguments='articles rewrite {id} -i "its musing was published with a link but no text"',
        what_it_does=_REWRITE_PUBLISHED,
    ),
    "title_markup": Suggestion(
        action="Rewrite the article so its title is plain text",
        arguments='articles rewrite {id} -i "the title has markdown around it"',
        what_it_does=_REWRITE_PUBLISHED,
    ),
    "body_code_fence": Suggestion(
        action="Rewrite the article so its body is not a block of code",
        arguments='articles rewrite {id} -i "the whole body is inside a code fence"',
        what_it_does=_REWRITE_PUBLISHED,
    ),
    "title_markup_and_body_code_fence": Suggestion(
        action="Rewrite the article so its title is plain text and its body is not a block of code",
        arguments=(
            'articles rewrite {id} -i "the title has markdown around it and the whole body is '
            'inside a code fence; remove both"'
        ),
        what_it_does=_REWRITE_PUBLISHED,
    ),
    # No command from here down: what to look at, and where.
    "musing_dangling": Suggestion(
        action="Look at the article the musing links to: a reader who follows the link finds nothing",
    ),
    "security_incident": Suggestion(
        action=(
            f"Read the incident's next steps on screen; the edge dashboard ({NAME_PREFIX}-<env>-edge) "
            "shows what the firewall blocked"
        ),
    ),
    "alarm_firing": Suggestion(
        action=(
            f"Open the alarm in CloudWatch; the pipeline dashboard ({NAME_PREFIX}-<env>-pipeline) "
            "shows what led up to it"
        ),
    ),
    "firewall_spike": Suggestion(
        action=(
            f"Look at the edge dashboard ({NAME_PREFIX}-<env>-edge) for which rule is blocking, and at "
            "the security incidents for what it caught"
        ),
    ),
    "spend_unusual": Suggestion(
        action="Look at the Stats page, then Cost Explorer by service, for what grew this week",
    ),
    # What log_review found in a function's log (log_review.py), one kind per root cause, about the
    # function. No command: none of these is fixed from admin_cli. The finding's `root_cause`
    # carries the longer advice and where the change is made; this is the line on the card.
    "log_lambda_timeout": Suggestion(
        action="Raise the function's timeout in Terraform, or find what it waits on (longest run on screen)",
    ),
    "log_out_of_memory": Suggestion(action="Raise the function's memory_size in Terraform"),
    "log_packaging": Suggestion(action="Add the missing dependency to the requirements, then redeploy"),
    "log_access_denied": Suggestion(action="Add the refused action to the function's role in Terraform"),
    "log_model_throttled": Suggestion(
        action="Usually passes; if it keeps happening, spread the topics' schedules or raise the quota",
    ),
    "log_token_limit": Suggestion(action="Raise that step's token limit, or tighten its prompt"),
    "log_source_rate_limited": Suggestion(
        action="Research the topic less often, or give its source an API key with a higher limit",
    ),
    "log_source_down": Suggestion(
        action="The source had server errors: usually passes, the next run retries"
    ),
    "log_source_timeout": Suggestion(action="A source was slow: usually passes, or raise the call's timeout"),
    "log_source_data": Suggestion(action="The source sent incomplete data: if it lasts, look at the adapter"),
    "log_configuration": Suggestion(action="Set the missing setting or key it fell back from"),
    "log_conflict": Suggestion(action="Nothing to do: the code expects this race"),
    "log_code_error": Suggestion(action="A code fix, and a test for the case it did not handle"),
    "log_other": Suggestion(action="Read the example lines on screen; the function's log has the rest"),
    # What api_errors found in an API's access log (api_errors.py), one kind per root cause, about
    # the Lambda behind the API. No command: these are fixed in Terraform, code or by the caller.
    "api_firewall_blocked": Suggestion(action="In production, ask about the firewall: rules and addresses"),
    "api_throttled": Suggestion(action="Raise the stage's throttle in Terraform, or fix a looping client"),
    "api_auth_refused": Suggestion(action="Renew the caller's credentials or sign-in"),
    "api_no_such_route": Suggestion(action="Nothing to do unless one route repeats: usually scanners"),
    "api_bad_request": Suggestion(action="Look at what the caller sends to the route on screen"),
    "api_handler_4xx": Suggestion(action="Not a bug unless the site sends it: look at the route on screen"),
    "api_integration_timeout": Suggestion(action="Read the Lambda's log for what it waited on; speed it up"),
    "api_lambda_failed": Suggestion(action="Read the Lambda's own log for the traceback (log_review)"),
    "api_aws_5xx": Suggestion(action="Usually passes; if it lasts, check the AWS Health Dashboard"),
    "api_other": Suggestion(action="Read the breakdown on screen"),
}


def suggest(kind: str, target_id: str | None = None) -> dict | None:
    """The suggestion for a finding of `kind` about `target_id`, ready to show: `action`,
    `command` and `what_it_does`. `command` and `what_it_does` are None for a kind that has no
    command. The whole suggestion is None when the kind is not in the catalogue, or its command
    needs an id and `target_id` is missing or fails ID_PATTERN -- a finding with no command is
    still worth reporting, a command built from an id we do not trust is not."""
    entry = CATALOGUE.get(kind)
    if entry is None:
        return None
    arguments = entry.arguments
    if arguments is None:
        return {"action": entry.action, "command": None, "what_it_does": None}
    if "{id}" in arguments:
        if not isinstance(target_id, str) or not ID_PATTERN.match(target_id):
            return None
        arguments = arguments.replace("{id}", target_id)
    return {
        "action": entry.action,
        "command": f"{ADMIN_CLI} {arguments}",
        "what_it_does": entry.what_it_does,
        # The owner's rule for every Admin CLI command on screen: check it before running it.
        "warning": DOUBLE_CHECK_WARNING,
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
