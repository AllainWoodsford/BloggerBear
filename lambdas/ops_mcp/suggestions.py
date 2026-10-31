"""What the assistant may suggest: a fixed catalogue, one entry per kind of finding. Most entries are
an admin_cli command; some have no command, only what to look at.

The model never writes, edits or chooses a command. It reads articles, review notes and log lines,
any of which can carry text someone wrote to steer it; if it could put a command on the operator's
screen, that text could too. So a finding names its *kind*, the kind picks its template here, and
the only thing filled in is an id read from a table and checked against ID_PATTERN (a model's id
against MODEL_ID_PATTERN, a prompt's against PROMPT_ID_PATTERN: valid_id). Nothing in the catalogue
deletes anything without asking first: `evals cull --apply`, the one command here that can delete
(models culled into the archive), shows the proposal and asks y/N before it does, and
`models restore` puts a culled model back.

A kind whose fix depends on a switch has a second entry in VARIANTS, which the tool that finds it
picks while the switch is off, so the card names the step that has to come first.

tests/test_ops_mcp_suggestions.py parses every command here with admin_cli's own argument parser,
so a renamed command or flag fails the build instead of handing the operator a command that errors.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from common.naming import NAME_PREFIX

ADMIN_CLI = "python scripts/admin_cli.py"
DOUBLE_CHECK_WARNING = (
    "⚠️ Suggested by the assistant: double-check it is the right article or topic before you run "
    "it. Nothing runs until you do."
)

# Topic ids are slugs and article ids are UUIDs: letters, digits, "-" and "_". Anything else is
# refused, so an id can never carry a quote, a space or a second command into the template.
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
# A model's id has dots and a colon (maker.alpha-v1:0), and an inference profile's ARN slashes: the
# characters common/evidence.py's model ids may hold, and still no quote, space, `;`, `$` or `#`.
MODEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
# A prompt's id is `<lambda_id>/<prompt_name>`, each a code (common/evals.py's prompt id).
PROMPT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,39}/[a-z][a-z0-9_]{0,39}$")
# The kinds whose id is not a topic's or an article's: what each is checked against instead.
ID_PATTERNS = {"model_gated": MODEL_ID_PATTERN, "prompt_fallback": PROMPT_ID_PATTERN}


def valid_id(kind: str, target_id) -> bool:
    """Whether `target_id` is shaped as the id of a finding of `kind` is (ID_PATTERNS, else
    ID_PATTERN), matched whole: a trailing newline is not part of any id."""
    pattern = ID_PATTERNS.get(kind, ID_PATTERN)
    return isinstance(target_id, str) and pattern.fullmatch(target_id) is not None


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
    # Professor Grizzlewick approved a financial article (common/reviewer.py): a person checks it.
    "reviewer_financial_check": Suggestion(
        action=(
            "Read the financial article the reviewer bear approved; if it is fine, mark it checked "
            "(articles checked <id>), and if not, take it down (articles unpublish <id>)"
        ),
        arguments="articles get {id}",
        what_it_does="Shows the article and its full text. Nothing changes.",
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
    # The model catalog put a price back that its review could not confirm
    # (common/model_catalog.py): the old price is in use again, so nothing is wrong now.
    "model_pricing_reverted": Suggestion(
        action="Check the model prices in use (models list), then reprice again",
        arguments="models reprice",
        what_it_does=(
            "Looks up every model's price again in the background. A changed price applies at "
            "once and is reviewed twice more, and is put back again if a review is not confident."
        ),
    ),
    # A blank musing is fixed where it is: the article is fine, so it is not rewritten.
    "musing_no_text": Suggestion(
        action="Have the article's blank musing written again",
        arguments="musings regenerate --article {id}",
        what_it_does=(
            "Writes every blank musing about the article again, in the mood it was published "
            "with (one model call each; a plain sentence if the model answers with nothing). "
            "The article, its page and its date are not touched. `musings edit` puts your own "
            "words on one instead."
        ),
    ),
    # Published things that look wrong (content.py). The words after -i are fixed per kind, for
    # the operator to edit before running: never the title or the body itself.
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
    # Sign-ins to the assistant itself (sign_in_tool.py). The id is a username an administrator made.
    "sign_in_locked": Suggestion(
        action="If it is you, unlock the user; if it is not, change that user's password first",
        arguments="sign-ins unlock {id}",
        what_it_does=(
            "Clears the lock, so the user's earlier failed attempts stop counting and they can "
            "sign in again at once. It does not change the password."
        ),
    ),
    "sign_in_failures": Suggestion(
        action="See who failed to sign in, and when",
        arguments="sign-ins list",
        what_it_does="Lists each user's sign-ins, failures and refusals. It changes nothing.",
    ),
    # The Model Ranch (model_ranch.py): what continuous evals' scores say. A gated model's id is its
    # model id (MODEL_ID_PATTERN). The gate lifts by itself when its windows recover; the command is
    # for a model that is needed before then, or whose scores are wrong.
    "model_gated": Suggestion(
        action=(
            "Look at why the model is gated (its scores are on screen); if it is needed now, or its "
            "scores are wrong, hold its gates open for two days"
        ),
        arguments="evals ungate --model {id} --hours 48",
        what_it_does=(
            "Sets the model's ungated_until 48 hours ahead, so no gate holds it back until then, "
            "whatever its scores. Its scores are not changed; `--hours 0` ends the override sooner."
        ),
    ),
    # A prompt fell back to the copy in the code (common/prompts.py): what is live comes first. The
    # id is the prompt's (PROMPT_ID_PATTERN); the command takes none.
    "prompt_fallback": Suggestion(
        action=(
            "See which of the prompt's versions are live, released and approved, then read the "
            "prompt_fallback lines in its Lambda's log for why it fell back"
        ),
        arguments="prompts list",
        what_it_does=(
            "Lists each prompt's files under latest/ with their version, release, hash status and "
            "pause state. It changes nothing."
        ),
    ),
    # The judge's verdicts count towards the gates only once it agrees with people often enough, over
    # enough pairs (common/evals_settings.py); each Finding a person reviews makes one.
    "judge_uncalibrated": Suggestion(
        action="Review some research Findings yourself, so the judge has verdicts of yours to agree with",
        arguments="approve --source findings",
        what_it_does=(
            "Shows each Finding in turn without the judge's verdict: y marks it good, r bad, z skips. "
            "Each one you mark is a Human Review, and a pair with the judge's verdict when it has one. "
            "Nothing changes until you press a key."
        ),
    ),
    # The weekly cycle's cull proposal (common/model_fleet.cull_proposal): while culling is off, the
    # card is VARIANTS' instead, which names `evals set --cull on` first.
    "cull_proposed": Suggestion(
        action="Read the weekly cull proposal, and cull the models in it if you agree",
        arguments="evals cull --apply",
        what_it_does=(
            "Shows the proposal and asks first; on yes, archives then deletes up to cull_max_per_run of "
            "the proposed models, each checked again against the live rules. `models restore <id>` "
            "puts one back."
        ),
    ),
    # No command from here down: what to look at, and where.
    "musing_dangling": Suggestion(
        action="Look at the article the musing links to: a reader who follows the link finds nothing",
    ),
    # A high-severity incident: the next steps are on screen; the command marks it as seen, which
    # is what takes it out of the briefing.
    "security_incident": Suggestion(
        action=(
            f"Read the incident's next steps on screen; the edge dashboard ({NAME_PREFIX}-<env>-edge) "
            "shows what the firewall blocked. Then mark it as seen"
        ),
        arguments="security acknowledge {id}",
        what_it_does=(
            "Marks the incident as acknowledged, so it stops being reported as open. It blocks "
            "nobody and changes nothing else; `security resolve` closes it when it is dealt with."
        ),
    ),
    "alarm_firing": Suggestion(
        action=(
            f"Open the alarm in CloudWatch; the pipeline dashboard ({NAME_PREFIX}-<env>-pipeline) "
            "shows what led up to it"
        ),
    ),
    # Something unusual that is not an incident yet: the command opens one, so it is written
    # down and followed up. The words after --summary are fixed, for the operator to edit.
    "firewall_spike": Suggestion(
        action=(
            f"Look at the edge dashboard ({NAME_PREFIX}-<env>-edge) for which rule is blocking; if it "
            "is not ordinary noise, open a security incident for it"
        ),
        arguments='security open --severity medium --summary "the firewall is blocking far more than usual"',
        what_it_does=(
            "Opens a security incident with your summary, so the spike is on record and shows in "
            "`security list` until you resolve it. It blocks nobody. High severity would also "
            "send the alert email."
        ),
    ),
    "spend_unusual": Suggestion(
        action="Look at the Stats page, then Cost Explorer by service, for what grew this week",
    ),
    # A vision topic's sites (vision_status.py): one kind per reason a site was not measured, so
    # each card says what to do about that reason, and dismissing one does not hide another. No
    # command: a topic's sites config is written by hand (cli_guide.py never fills one in), and the
    # worker is a deployment setting. The id is the environment for the worker, else the topic.
    "vision_worker_off": Suggestion(
        action=(
            "Turn the vision worker on: re-apply infra/bootstrap by hand with vision_region first "
            "(docs/deployment-runsheet.md), set the VISION_ENABLED repository variable to true, and let "
            "the next deploy (a merge to dev, or a production release) create the worker. Until then "
            "these topics record every site as not measured"
        ),
    ),
    "vision_config_error": Suggestion(
        action=(
            "Fix the topic's sites config by hand (topics update; scripts/rail_topics.example.json "
            "shows the shape). Research stops before measuring, or a backend with no worker is asked for"
        ),
    ),
    "vision_site_refused": Suggestion(
        action=(
            "Edit the site's polygon or parameters by hand (topics update): the worker refuses this "
            "request as the site is set up"
        ),
    ),
    "vision_worker_error": Suggestion(
        action=(
            "Read the vision worker's log in the vision Region (log_review with the vision-worker "
            "function, or the runsheet)"
        ),
    ),
    "vision_source_error": Suggestion(
        action=(
            "Usually passes by itself: the next tick tries again. If it lasts, the satellite image "
            "search or the OpenStreetMap mirror is down"
        ),
    ),
    "vision_site_starved": Suggestion(
        action=(
            "Give the topic a larger time budget (topics update): the worker is asked only with 45 "
            "seconds of it left after the image search, so its sites wait for time and are never measured"
        ),
    ),
    # What log_review found in a function's log (log_review.py), one kind per root cause, about the
    # function. No command: none of these is fixed from admin_cli. The finding's `root_cause`
    # carries the longer advice and where the change is made; this is the line on the card.
    "log_lambda_timeout": Suggestion(
        action="Raise the function's timeout in Terraform, or find what it waits on (longest run on screen)",
    ),
    "log_out_of_memory": Suggestion(action="Raise the function's memory_size in Terraform"),
    "log_packaging": Suggestion(action="Add the missing dependency to the requirements, then redeploy"),
    "log_scene_unreadable": Suggestion(
        action="Usually passes: the next tick reads the scene again. If every scene fails, check the "
        "worker's allowed URL prefixes and the imagery bucket",
    ),
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

# A kind's card while a switch it depends on is off: (kind, variant) -> the entry used instead of
# CATALOGUE's. Picked by the tool that finds it (`variant=`), never by a model.
CULL_OFF = "cull_off"
VARIANTS: dict[tuple[str, str], Suggestion] = {
    # `evals cull --apply` is refused while cull_enabled is false, so the step before it comes first.
    ("cull_proposed", CULL_OFF): Suggestion(
        action=(
            "Culling is off: turn it on first (evals set --cull on), then apply the proposal "
            "(evals cull --apply), which shows it and asks before it deletes anything"
        ),
        arguments="evals set --cull on",
        what_it_does=(
            "Lets `evals cull --apply` archive and delete the proposed models; it deletes nothing "
            "itself. `evals set --cull off` turns it off again."
        ),
    ),
}


def suggest(kind: str, target_id: str | None = None, variant: str | None = None) -> dict | None:
    """The suggestion for a finding of `kind` about `target_id`, ready to show: `action`,
    `command` and `what_it_does`. `command` and `what_it_does` are None for a kind that has no
    command. The whole suggestion is None when the kind is not in the catalogue, or its command
    needs an id and `target_id` is missing or is not shaped as that kind's id (valid_id) -- a
    finding with no command is still worth reporting, a command built from an id we do not trust
    is not. `variant` picks the kind's VARIANTS entry, when it has one by that name."""
    if kind not in CATALOGUE:
        return None
    entry = VARIANTS.get((kind, variant)) or CATALOGUE[kind]
    arguments = entry.arguments
    if arguments is None:
        return {"action": entry.action, "command": None, "what_it_does": None}
    if "{id}" in arguments:
        if not valid_id(kind, target_id):
            return None
        arguments = arguments.replace("{id}", target_id)
    return {
        "action": entry.action,
        "command": f"{ADMIN_CLI} {arguments}",
        "what_it_does": entry.what_it_does,
        # The owner's rule for every Admin CLI command on screen: check it before running it.
        "warning": DOUBLE_CHECK_WARNING,
    }


def finding(
    kind: str, noticed: str, target_id: str | None = None, *, variant: str | None = None, **where
) -> dict:
    """A finding as a tool returns it: its kind, the id it is about, what was noticed (fixed
    words, written here in code), where (ids and labels for the card), and the suggestion, if
    the catalogue has one (its `variant`, when the tool names one: VARIANTS)."""
    return {
        "kind": kind,
        "id": target_id,
        "noticed": noticed,
        "where": {key: value for key, value in where.items() if value is not None},
        "suggestion": suggest(kind, target_id, variant),
    }
