"""Re-Write: an operator-requested rewrite of an article the reviews held.

From the review inbox (scripts/review_inbox.py, key `w`) the operator picks a model; the Admin
API (`POST /moderation-queue/{queue_id}/rewrite`) claims the queue item (pending -> rewriting)
and invokes the daily-cycle Lambda asynchronously with `{"action": "rewrite", ...}`, which runs
`run_rewrite` below. The API call returns at once, so the inbox moves straight on.

**Any article, steered by a person.** `admin_cli articles rewrite <id> --instructions "..."`
(`POST /articles/{article_id}/rewrite`) does the same for an article in any state, with the
person's note as the main thing to fix. It goes through the reviews again and waits for approval
like any other draft.

**A published article stays up until its rewrite is ready.** Nothing about it changes while the
rewrite runs. Only when the rewrite has been written, has passed the guards and has been through
the reviews is the article taken down (status, page, musings, CDN cache) and the new text put in
the inbox. If the rewrite fails, the article is still published exactly as it was, and its queue
item ends as `rewrite_failed` with the reason: it never enters the inbox, where approving or
rejecting it would act on an article that is still public. `--force` is the other order: take it
down at once, then rewrite (for an article that must not stay up meanwhile).

**What it does.** The article's text, the reasons it was held, the research it was written from
and freshly fetched data go to the chosen model with one job: fix those issues and nothing else.
The result gets the same plain-code guards as the automatic revision pass (no figure or link
that appears in none of the sources; a sensible title; a bounded length change), then the
fresh-data review and the compliance review again. It always goes back to a person: the old
queue item becomes `rewritten` and a new `pending` item carries the rewritten article, with
whatever the reviews now say. An Article that was `pending_moderation` stays so throughout.

**A correction, not a new article.** The model does not write the article out again. It replies
with edits: each one a passage quoted from the article and the text that replaces it. Code applies
them (`apply_edits`), so every sentence no edit quotes comes through exactly as it was, and the
reviews that run again find nothing new in it that the rewrite wrote. A quote that is not in the
article, or is in it more than once, overlapping edits, a reply that changes nothing, and edits
that replace more than half the article (`REWRITE_MIN_KEPT`) all fail the rewrite. Only real
problems are sent as issues to fix (`rewrite_issues`): the fresh-data review's notes that say
something about the review, not the text, are left out. The compliance review of the result sees
the fresh data too, so a figure the rewrite corrected from it is not called fabricated.

**Nothing is lost.** The text it replaces is kept in S3 (`articles/{id}.before-rewrite-{n}.md`).
Any failure (the model call, a guard, output that isn't the expected JSON) puts the *original*
item back to `pending` untouched, with the reason in `last_rewrite_error` (`rewrite_failed` for
an article that is still published, above). A rewrite that never finishes (the Lambda timed out)
is released the same way the next time the queue is listed (`release_stale_rewrites`).

**Cost is tracked.** Every call a rewrite makes -- the rewrite itself, and the reviews run on
its result -- is added to the article's lineage (stage `rewrite`, with the chosen model and the
prompt's id and version: rewrite/rewrite, common/prompts_registry.py) and to the week's Stats
totals, including a rewrite whose result was rejected: those tokens were spent. Every call of a
rewrite that failed is marked `failed` (FAILED_MARK), because its text was thrown away: continuous
evals leave those calls out of the article's prompts and its writer (common/evals_emission.py).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta

import boto3

from common import call_traces, compliance, fresh_review, prompts
from common.adapters.registry import ADAPTER_REGISTRY
from common.bedrock import invoke_model_tracked
from common.costing import build_lineage, pricing_for
from common.day_subject import ARTICLE_SUBJECT_FIELD, subject_findings
from common.dynamo import (
    REWRITE_FAILED_STATUS,
    delete_musings_for_article,
    finish_moderation_rewrite,
    get_article,
    get_moderation_item,
    get_pipeline_config,
    get_topic,
    list_findings,
    list_moderation_by_status,
    put_moderation_item,
    update_article_after_rewrite,
    update_article_status,
)
from common.findings_window import SUBJECT_READ_LIMIT, findings_window_hours, read_stopped_at
from common.model_pricing import model_label
from common.model_routing import resolve_model
from common.relevance import topic_label
from common.static_pages import invalidate_article_page, read_article_body, remove_article_page
from common.stats_tracking import record_article_lineage

REWRITE_STAGE = "rewrite"
# The mark on every call of a rewrite that failed (_fail). The calls stay in the lineage and the
# Stats for their cost, but the text they wrote was thrown away, so continuous evals credit neither
# their model nor their prompts with the article (common/evals_emission.py reads it).
FAILED_MARK = "failed"
# The reason on the queue item `articles rewrite` makes for an article that had none (one that was
# published, or rejected): it says why the article is in the inbox, and is not an issue to fix.
SENT_BACK_REASON = "sent back by a person for a rewrite"
MAX_INSTRUCTIONS_CHARS = 2000
REWRITE_MAX_TOKENS = 8192
# Looser than the automatic revision's (0.65, 1.35) and no heading check: fixing "investment
# advice" or "fabricated claim" can mean removing a whole paragraph or section.
REWRITE_BODY_LENGTH_BOUNDS = (0.4, 1.5)
# At least this share of the article must come through untouched (no edit quotes it). Below it the
# edits are a new article, not a correction of this one, and the reviews would start over on it.
REWRITE_MIN_KEPT = 0.5
# Past this a `rewriting` item is assumed dead (the Lambda's own timeout is 300s).
STALE_REWRITE_MINUTES = 15
MAX_FINDINGS = 48
FINDINGS_MAX_CHARS = 12_000
# Compliance's fixed reason for a financial topic (common/compliance.py): routine, nothing to fix.
_FINANCIAL_REASON = "financial topic"
# For a topic whose adapter gives its articles a fixed ending (Adapter.drafting_guidance: a
# summary table, takeaways). A rewrite fixes issues and keeps the length, so it is not asked to
# add that ending to an older article, only not to leave one it finds out of step with the text.
_CLOSING_SECTIONS_RULE = (
    "If the draft ends with summary sections (a table, a list of takeaways) that repeat something "
    "you change, edit them too so they still match the article."
)
# Every block the rewrite prompt uses (fresh_review._defang knows only its own three).
_DELIMITER_TAG = re.compile(r"<(/?)(draft|findings|fresh_data|issues_to_fix|editor_note)", re.IGNORECASE)
# The fresh-data review's notes (fresh_review.review_notes) all start with this. Only those that
# flag a claim ("<claim> -- stale (minor): ...") are problems in the text; the rest say that the
# review could not run, that figures moved inside the topic's tolerance and were not flagged, or
# that the daily cycle corrected the draft or tried to. Asked to "fix" those, a model changes
# sentences nothing was wrong with, and the reviews then find new things in them.
_FRESH_REVIEW_NOTE = "fresh-data review"
_CLAIM_NOTE = re.compile(
    rf" -- (?:{'|'.join(fresh_review.PROBLEMS)}) \((?:{'|'.join(fresh_review.SEVERITIES)})\)"
)
_JSON_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")
# Stands in for a deletion while edits are applied, so the line it leaves can be tidied
# (`_tidy_deletions`); a private-use character, never in an article.
_DELETED = "\ue000"
# A line holding nothing but whitespace or a list marker: what deleting a line's text leaves.
_BARE_LINE = re.compile(r"[ \t]*(?:[-*+]|\d+[.)])?[ \t]*")


class EditError(ValueError):
    """An edit that cannot be applied to the article as written; the message says which."""


def _defang(text: str) -> str:
    """Break any of the prompt's block tags inside the material, so nothing in it (web text, or
    a note pasted from somewhere) can close a block early and pose as what follows it."""
    return _DELIMITER_TAG.sub(lambda m: f"< {m.group(1)}{m.group(2)}", text)


def _is_problem_note(note: str) -> bool:
    return not note.startswith(_FRESH_REVIEW_NOTE) or bool(_CLAIM_NOTE.search(note))


def rewrite_issues(item: dict) -> list[str]:
    """What a rewrite is asked to fix: the item's hold reasons and the claims the fresh-data
    review flagged, without the routine "financial topic" reason (that is a routing rule, not a
    problem in the text), the "sent back" reason (that says why it is in the inbox; the person's
    instructions say what), or a review note that is about the review rather than the text."""
    reasons = [
        str(r)
        for r in item.get("reasons") or []
        if _FINANCIAL_REASON not in str(r).lower() and str(r) != SENT_BACK_REASON
    ]
    return reasons + [str(n) for n in item.get("review_notes") or [] if _is_problem_note(str(n))]


# The reply contract `parse_edits` reads: the locked tail of the prompt.
_REPLY_RULE = (
    "Reply with JSON only, no prose and no code fences: "
    '{"title": "...", "edits": [{"find": "...", "replace": "..."}]}\n'
    '- "title": the article\'s title, exactly as it is unless an issue is about it.\n'
    '- "find": a passage copied character for character from the body of the draft (never '
    "its Title line): the sentence or sentences to change, long enough to appear only once "
    "in the body.\n"
    '- "replace": the text that takes its place; "" deletes it.\n'
    "Edits must not overlap. Whatever no edit quotes is kept exactly as it is, so do not "
    "repeat it."
)


def rewrite_prompt(
    topic_name: str,
    title: str,
    body: str,
    issues: list[str],
    findings_text: str,
    evidence: str,
    as_of: str,
    instructions: str = "",
    writing_guidance: str = "",
) -> prompts.Prompt:
    """The rewrite's prompt, rewrite/rewrite (common/prompts_registry.py), with the version that
    rendered it. `writing_guidance` is how the topic's adapter wants its articles written (how to
    state figures that move: Adapter.figure_guidance), so a rewrite does not put back the exact
    figures that got the article flagged. Ours, not data, and it lifts none of the rules. Every
    piece of material, the person's note included, is defanged here, before it is rendered."""
    house_style = (
        f"\n\nHow articles on this topic are written (within the rules above):\n{writing_guidance}"
        if writing_guidance
        else ""
    )
    listed = "\n".join(f"{n}. {issue}" for n, issue in enumerate(issues, 1)) or (
        "(none: the reviews flagged nothing)"
    )
    draft = _defang(f"Title: {title}\n\n{body}")
    # The person's note is the one thing here that is not data: it comes from the operator who
    # asked for the rewrite (Admin API, IAM-signed), not from the web. It still cannot lift the
    # rules below, and the plain-code guards check the result whatever it says.
    editor_note = (
        "The site's editor read the article and says this is wrong with it. Treat it as the main "
        "thing to fix, within the rules below:\n"
        f"<editor_note>\n{_defang(instructions)}\n</editor_note>\n\n"
        if instructions
        else ""
    )
    to_resolve = (
        "what the editor_note describes, and every issue in issues_to_fix,"
        if instructions
        else "every issue in issues_to_fix"
    )
    values = {
        "topic_name": topic_name,
        "editor_note_block": editor_note,
        "draft_block": f"<draft>\n{draft}\n</draft>",
        "issues_block": f"<issues_to_fix>\n{_defang(listed)}\n</issues_to_fix>",
        "findings_block": f"<findings>\n{_defang(findings_text[:FINDINGS_MAX_CHARS])}\n</findings>",
        "fresh_data_block": f'<fresh_data as_of="{as_of}">\n{_defang(evidence)}\n</fresh_data>',
        "to_resolve": to_resolve,
        "length_proviso": " unless the editor_note asks otherwise" if instructions else "",
        "house_style": house_style,
    }
    return prompts.get_prompt("rewrite/rewrite", values, {"reply_rule": _REPLY_RULE})


def build_rewrite_prompt(
    topic_name: str,
    title: str,
    body: str,
    issues: list[str],
    findings_text: str,
    evidence: str,
    as_of: str,
    instructions: str = "",
    writing_guidance: str = "",
) -> str:
    """The text `rewrite_prompt` renders, for a caller that needs only what is sent."""
    return rewrite_prompt(
        topic_name,
        title,
        body,
        issues,
        findings_text,
        evidence,
        as_of,
        instructions,
        writing_guidance=writing_guidance,
    ).text


def parse_edits(text: str) -> tuple[str, list[tuple[str, str]]] | None:
    """(title, [(find, replace), ...]) from the rewrite's reply, or None when it is not the
    expected JSON (any edit that is not a non-empty `find` with a string `replace` included).
    The title is "" when the reply gives none: the article keeps its own."""
    if not isinstance(text, str):
        return None
    cleaned = _JSON_FENCE.sub("", text.strip()).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("edits"), list):
        return None
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return None
    edits = []
    for edit in data["edits"]:
        if not isinstance(edit, dict):
            return None
        find, replace = edit.get("find"), edit.get("replace", "")
        if not isinstance(find, str) or not find.strip() or not isinstance(replace, str):
            return None
        edits.append((find, replace))
    return (title or "").strip(), edits


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _locate(body: str, quote: str) -> list[tuple[int, int]]:
    """Where `quote` is in `body`, as (start, end) spans: word for word, or failing that with any
    run of whitespace standing for any other (a model copying a passage often re-wraps it)."""
    spans = [(m.start(), m.end()) for m in re.finditer(re.escape(quote), body)]
    if spans:
        return spans
    words = quote.split()
    if not words:
        return []
    pattern = r"\s+".join(re.escape(word) for word in words)
    return [(m.start(), m.end()) for m in re.finditer(pattern, body)]


def _tidy_deletions(text: str) -> str:
    """Close up what deletions left (each marked `_DELETED`): a space left at the end of a line,
    or doubled where a sentence was taken out of one, goes; a line they emptied, or left as a bare
    list marker, goes, and so does a blank line that would then be doubled. Lines no deletion
    touched are not looked at."""
    lines: list[str] = []
    dropped_at: list[int] = []
    for line in text.split("\n"):
        if _DELETED not in line:
            lines.append(line)
            continue
        line = re.sub(rf"[ \t]*{_DELETED}[ \t]*$", "", line)
        line = re.sub(rf"(^|[ \t]){_DELETED}[ \t]+", r"\1", line)
        line = line.replace(_DELETED, "")
        if _BARE_LINE.fullmatch(line):
            dropped_at.append(len(lines))
            continue
        lines.append(line)
    for at in reversed(dropped_at):
        if 0 < at < len(lines) and lines[at - 1] == "" and lines[at] == "":
            del lines[at]
    return "\n".join(lines)


def apply_edits(body: str, edits: list[tuple[str, str]]) -> tuple[str, int]:
    """`body` with each (find, replace) edit made, and how many of its characters the edits
    replaced. Every `find` is placed in the body as given, before any edit is made, so an edit can
    never act on another's output. Raises EditError when a `find` is not in the body, is in it
    more than once, or overlaps another edit's."""
    placed = []
    for n, (find, replace) in enumerate(edits, 1):
        spans = _locate(body, find)
        if not spans:
            raise EditError(f"edit {n} quotes text that is not in the article: {_short(find)!r}")
        if len(spans) > 1:
            raise EditError(
                f"edit {n} quotes text that is in the article {len(spans)} times: {_short(find)!r}"
            )
        placed.append((*spans[0], replace, n))
    placed.sort()
    for (_, end, _, first), (start, _, _, second) in zip(placed, placed[1:]):
        if start < end:
            raise EditError(f"edits {first} and {second} overlap")
    # An article that holds the mark itself could not tell it from ours: its gaps are left as they are.
    mark = "" if _DELETED in body else _DELETED
    edited = body
    for start, end, replace, _ in reversed(placed):
        edited = edited[:start] + (replace if replace.strip() else mark) + edited[end:]
    if mark:
        edited = _tidy_deletions(edited)
    return edited.strip(), sum(end - start for start, end, _, _ in placed)


def release_stale_rewrites(now: datetime | None = None) -> int:
    """Put every `rewriting` item older than STALE_REWRITE_MINUTES back to `pending`, noting
    why, so a rewrite that died (a Lambda timeout) never strands an article. Returns how many.
    Called whenever the queue is listed; a rewrite that later finishes after all finds it no
    longer owns the item and discards its result. An item whose article is still published ends
    as `rewrite_failed` instead: that article was never in the inbox."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(minutes=STALE_REWRITE_MINUTES)).isoformat()
    released = 0
    for item in list_moderation_by_status("rewriting"):
        requested_at = item.get("rewrite_requested_at") or ""
        if requested_at and requested_at >= cutoff:
            continue
        if finish_moderation_rewrite(
            item["queue_id"],
            rewrite_id=item.get("rewrite_id") or "",
            status=_failed_status(get_article(item["article_id"]), item),
            fields={"last_rewrite_error": f"the rewrite started {requested_at or 'earlier'} never finished"},
        ):
            released += 1
    return released


def _is_live(article: dict | None) -> bool:
    """Whether the article is public right now: it is being rewritten while it stays up."""
    return article is not None and article.get("status") == "published"


def _failed_status(article: dict | None, item: dict | None = None) -> str:
    """Where a failed rewrite's item goes: back to the inbox, unless the article is still
    published -- it was never in the inbox, and must not appear there as if it were held. When
    the article could not even be read, the item's own `article_still_published` (set when the
    rewrite of a published article was requested) decides."""
    if article is None:
        live = bool((item or {}).get("article_still_published"))
    else:
        live = _is_live(article)
    return REWRITE_FAILED_STATUS if live else "pending"


def _take_down(article: dict) -> str | None:
    """Take a published article off the site now that its rewrite is ready: status first (the
    public API stops serving it, and a failure here leaves it published and whole), then its
    page and the copies of its figures (as many as the article stores; common/figures.py).
    Returns a note for the reviewer if the page could not be removed, else None. Safe to
    repeat."""
    article_id = article["article_id"]
    update_article_status(article_id, "pending_moderation")
    try:
        remove_article_page(article_id, figure_count=len(article.get("figures") or []))
    except Exception as exc:  # noqa: BLE001 - the rewrite still goes to the inbox, with this said
        print(f"rewrite: could not remove the page of {article_id}: {exc!r}")
        return (
            f"its old page could not be removed ({exc}) and is still reachable by its link: "
            "approving this replaces it; if you reject this, run `articles unpublish` afterwards"
        )
    return None


def _clear_traces(article: dict) -> dict:
    """What is left of a taken-down article: the musings written about it, and its page (and its
    figures, if it has any) in the CDN's cache (as admin_api_handler.py's unpublish does). Best
    effort: the article is already down and in the inbox."""
    article_id = article["article_id"]
    try:
        musings_removed = delete_musings_for_article(article_id)
    except Exception as exc:  # noqa: BLE001
        print(f"rewrite: could not remove the musings about {article_id}: {exc!r}")
        musings_removed = 0
    invalidated = invalidate_article_page(article_id, figure_count=len(article.get("figures") or []))
    return {"musings_removed": musings_removed, "cache_invalidated": invalidated}


def _strip_disclaimer(body: str) -> tuple[str, bool]:
    """The body without the standing financial disclaimer (appended in code, not by a model,
    and put back the same way after the rewrite), and whether it was there."""
    return compliance.strip_financial_disclaimer(body)


def findings_for(article: dict, topic: dict) -> list[dict]:
    """The findings the article was most likely written from: those captured in the topic's
    findings window before it was created (newest first). That is the reach the daily cycle read
    it from (common/findings_window.py): a day, or more for a topic that writes on some days only,
    whose article drew on the findings of its off days too. The previous article is not known
    here, so a wide window can also hold findings an earlier article used: more context, never
    less. Empty once they have aged out of the Findings table.

    An article written on a rotating topic's day subject (its `subject`, common/day_subject.py)
    was drafted from that subject's Findings only, so only those are kept, at most MAX_FINDINGS of
    them, as the daily cycle caps them: the window is read further first (SUBJECT_READ_LIMIT, as
    the daily cycle reads it), so other subjects' newer Findings never push the subject's out. When
    the window holds more than that read reaches, the log says where it stopped, as the daily
    cycle's does. The fresh-data review and the compliance review then see its own subject's
    research, not its neighbours'. If none of the window's Findings report it (the subject has since
    left the topic's rota, say), the newest MAX_FINDINGS of the whole window are used and the log
    says so: never an empty prompt.

    Either way the read is bounded at both ends, the window's start and the article's `created_at`,
    so a read long after the article (a late rewrite, or an offline scoring run rebuilding it,
    common/eval_score.py) spends none of it on Findings captured since: on a topic that researches
    hourly, the newest MAX_FINDINGS from the window's start to now would soon be all of them."""
    created_at = article.get("created_at") or datetime.now(UTC).isoformat()
    since = (datetime.fromisoformat(created_at) - timedelta(hours=findings_window_hours(topic))).isoformat()
    subject = article.get(ARTICLE_SUBJECT_FIELD)
    if not isinstance(subject, str) or not subject:
        findings, _, _ = list_findings(
            article["topic_id"], since=since, until=created_at, limit=MAX_FINDINGS, unexpired_at=None
        )
        return findings
    findings, _, more = list_findings(
        article["topic_id"], since=since, until=created_at, limit=SUBJECT_READ_LIMIT, unexpired_at=None
    )
    if more:
        print(
            f"rewrite: subject {subject!r:.70} topic_id={article['topic_id']} read {len(findings)} findings "
            f"of its window and stopped at {read_stopped_at(len(findings))}; any of its older ones were "
            "not read"
        )
    own = subject_findings(findings, subject)
    if own or not findings:
        return own[:MAX_FINDINGS]
    newest = findings[:MAX_FINDINGS]
    print(
        f"rewrite: no findings on the article's subject {subject!r:.70} in its window; "
        f"rewriting from the window's {len(newest)} newest findings instead"
    )
    return newest


def _load_snapshot(findings: list[dict]) -> dict | None:
    key = (findings[0].get("raw_snapshot_s3_key") if findings else None) or None
    if not key:
        return None
    try:
        body = boto3.client("s3").get_object(Bucket=os.environ["CONTENT_BUCKET"], Key=key)["Body"]
        return json.loads(body.read())
    except Exception as exc:  # noqa: BLE001 - the adapter then re-checks without it
        print(f"rewrite: could not load the latest snapshot: {exc!r}")
        return None


def _evidence(topic: dict, snapshot: dict | None) -> str:
    """Current data for the rewrite to correct claims against, or "" if there is none."""
    adapter_cls = ADAPTER_REGISTRY.get(topic.get("adapter"))
    if adapter_cls is None:
        return ""
    try:
        return fresh_review._fetch_evidence(adapter_cls(), topic, snapshot) or ""
    except Exception as exc:  # noqa: BLE001 - findings alone can still settle most issues
        print(f"rewrite: no fresh data for the rewrite ({exc!r})")
        return ""


def _pipeline_config() -> dict:
    try:
        return get_pipeline_config() or {}
    except Exception as exc:  # noqa: BLE001
        print(f"rewrite: could not read the pipeline config, using defaults: {exc!r}")
        return {}


def _record_calls(article: dict, calls: list[dict]) -> tuple[dict, float | None]:
    """The article's lineage with `calls` added, and what those calls alone cost (AUD, None if
    unpriced). Also tallies them onto the week's Stats (fails open)."""
    calls = [c for c in calls if c is not None]
    lineage = article.get("lineage") or {}
    rebuilt = build_lineage(list(lineage.get("calls") or []) + calls, research=lineage.get("research"))
    added = build_lineage(calls) if calls else None
    if added is not None:
        record_article_lineage(added)
    return rebuilt, (added or {}).get("cost_aud")


def _fail(item: dict, rewrite_id: str, reason: str, article: dict | None, calls: list[dict]) -> dict:
    """Put the original item back to pending with the reason (or close it as `rewrite_failed` if
    the article is still published: nothing about it has changed), recording any tokens spent."""
    print(f"rewrite: failed for queue_id={item['queue_id']}: {reason}")
    if article is not None and any(c is not None for c in calls):
        try:
            # All of them belong to the one failed attempt: the rewrite and any review of its text.
            lineage, _ = _record_calls(article, [{**c, FAILED_MARK: True} for c in calls if c is not None])
            update_article_after_rewrite(
                article["article_id"],
                title=article.get("title") or "",
                lineage=lineage,
                review=None,
                rewrites=list(article.get("rewrites") or []),
            )
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not hide the real failure
            print(f"rewrite: could not record the failed rewrite's cost: {exc!r}")
    released = finish_moderation_rewrite(
        item["queue_id"],
        rewrite_id=rewrite_id,
        status=(status := _failed_status(article, item)),
        fields={"last_rewrite_error": reason},
    )
    result = {"status": "failed", "queue_id": item["queue_id"], "reason": reason, "released": released}
    if status == REWRITE_FAILED_STATUS:
        result["still_published"] = True
    return result


def run_rewrite(queue_id: str, rewrite_id: str) -> dict:
    """Rewrite the article on `queue_id`, if it is still `rewriting` under `rewrite_id`.
    Never raises: every failure returns the item to the inbox with the reason."""
    item = get_moderation_item(queue_id)
    if item is None or item.get("status") != "rewriting" or item.get("rewrite_id") != rewrite_id:
        return {"status": "skipped", "queue_id": queue_id, "reason": "no longer waiting for this rewrite"}
    # Its model calls are traced under the article (common/call_traces.py, while trace_calls is on),
    # its reviews as rewrite_adversarial_review and rewrite_compliance_review, so they never replace
    # the reviews of the text the article was written with. A Re-Write overwrites only the stages it
    # reaches, so each trace carries this rewrite_id, and the reader marks an earlier Re-Write's
    # reviews that a later one did not reach as superseded: the rewrite call is always traced first.
    with call_traces.bind(item.get("article_id"), stage_prefix=REWRITE_STAGE, run=rewrite_id):
        return _rewrite(item, queue_id, rewrite_id)


def _rewrite(item: dict, queue_id: str, rewrite_id: str) -> dict:
    """`run_rewrite`'s work, once the item is known to be waiting for this rewrite."""
    article = None
    calls: list[dict] = []
    try:
        article = get_article(item["article_id"])
        topic = get_topic(item["topic_id"]) or {"topic_id": item["topic_id"], "name": item["topic_id"]}
        if article is None:
            return _fail(item, rewrite_id, "the article no longer exists", None, calls)
        model_id = item["rewrite_model_id"]
        issues = rewrite_issues(item)
        instructions = str(item.get("rewrite_instructions") or "")

        original_title = article.get("title") or ""
        stored_body = read_article_body(article["body_s3_key"])
        body, had_disclaimer = _strip_disclaimer(stored_body)

        findings = findings_for(article, topic)
        findings_text = "\n".join(f"- {f.get('summary', '')}" for f in findings)
        snapshot = _load_snapshot(findings)
        evidence = _evidence(topic, snapshot)

        as_of = datetime.now(UTC).isoformat()
        rules = fresh_review.writing_rules(topic)
        closing_rule = _CLOSING_SECTIONS_RULE if rules["drafting_guidance"] else ""
        prompt = rewrite_prompt(
            topic_label(topic),
            original_title,
            body,
            issues,
            findings_text,
            evidence,
            as_of,
            instructions,
            writing_guidance="\n".join(filter(None, [rules["figure_guidance"], closing_rule])),
        )
        try:
            result = invoke_model_tracked(prompt.text, model_id, max_tokens=REWRITE_MAX_TOKENS)
        except Exception as exc:  # noqa: BLE001
            return _fail(item, rewrite_id, f"the rewrite model call failed: {exc}", article, calls)
        calls.append(
            {
                "stage": REWRITE_STAGE,
                "model_id": result["model_id"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "used_fallback": result["used_fallback"],
                "stop_reason": result.get("stop_reason"),
                **prompts.lineage_fields(prompt),
            }
        )
        # Stored as sent: the draft, the issues, the research, the fresh data and the editor's note
        # are the material the rewrite was given, and none of it is reader text (common/call_traces.py).
        call_traces.record(calls[-1], prompt.text, result["text"])
        if result.get("stop_reason") == "max_tokens":
            return _fail(item, rewrite_id, "the rewrite was cut off before it finished", article, calls)
        parsed = parse_edits(result["text"])
        if parsed is None:
            return _fail(item, rewrite_id, "the rewrite was not the expected JSON", article, calls)
        new_title, edits = parsed
        new_title = new_title or original_title
        try:
            new_body, replaced = apply_edits(body, edits)
        except EditError as exc:
            return _fail(item, rewrite_id, f"the rewrite's edits could not be applied: {exc}", article, calls)
        if new_title == original_title and new_body == body.strip():
            return _fail(item, rewrite_id, "the rewrite changed nothing", article, calls)
        kept = 1 - replaced / max(len(body), 1)
        if kept < REWRITE_MIN_KEPT:
            return _fail(
                item,
                rewrite_id,
                f"the rewrite replaced {round((1 - kept) * 100)}% of the article: a Re-Write corrects "
                "the passages with a problem and keeps the rest",
                article,
                calls,
            )
        violations = fresh_review.revision_violations(
            original_title,
            body,
            new_title,
            new_body,
            sources=[findings_text, evidence],
            body_length_bounds=REWRITE_BODY_LENGTH_BOUNDS,
            check_headings=False,
            figure_tolerance=rules["figure_tolerance"],
        )
        if violations:
            return _fail(
                item, rewrite_id, "the rewrite could not be trusted: " + "; ".join(violations), article, calls
            )
        if had_disclaimer or compliance.is_financial_topic(topic):
            new_body = compliance.append_financial_disclaimer(new_body)

        # Review the result the same way a fresh draft is reviewed, with the topic's own models.
        review_model, review_fallback = resolve_model(topic)
        fresh_record = None
        mode = fresh_review.resolve_review_mode(_pipeline_config(), topic)
        if mode != "off":
            fresh_record = fresh_review.run_review(
                topic=topic,
                title=new_title,
                draft=_strip_disclaimer(new_body)[0],
                findings_text=findings_text,
                latest_state=snapshot,
                model_id=review_model,
                fallback_model_id=review_fallback,
                mode=mode,
            )
            calls.append(fresh_record.pop("lineage_call", None))
            fresh_record.pop("evidence", None)
        # The fresh data counts as a source: the rewrite was told to correct figures from it.
        compliance_review = compliance.review_draft(
            new_body,
            topic,
            review_model,
            fallback_model_id=review_fallback,
            source_material=findings_text,
            fresh_data=evidence,
        )
        calls.append(compliance_review["lineage_call"])
    except Exception as exc:  # noqa: BLE001 - top-level guard: never strand the item
        return _fail(item, rewrite_id, f"the rewrite failed ({exc})", article, calls)

    # Claim the finish before changing anything, so a rewrite that was released as stuck (or a
    # duplicate delivery) never overwrites an article a person may already be looking at.
    number = len(article.get("rewrites") or []) + 1
    new_queue_id = str(uuid.uuid4())
    if not finish_moderation_rewrite(
        queue_id, rewrite_id=rewrite_id, status="rewritten", fields={"rewritten_to": new_queue_id}
    ):
        return {"status": "discarded", "queue_id": queue_id, "reason": "the item was released meanwhile"}

    # A published article has stayed up, untouched, until now: the rewrite is written, guarded
    # and reviewed, so this is the moment it comes down and the new text takes its place.
    was_live = _is_live(article)
    taken_down = False
    try:
        page_note = None
        if was_live:
            page_note = _take_down(article)
            taken_down = True
        _save(
            item=item,
            article=article,
            number=number,
            new_queue_id=new_queue_id,
            stored_body=stored_body,
            new_title=new_title,
            new_body=new_body,
            calls=calls,
            fresh_record=fresh_record,
            compliance_review=compliance_review,
            issues=issues,
            instructions=instructions,
            extra_reasons=[page_note] if page_note else [],
            edits=len(edits),
            kept_percent=round(kept * 100),
        )
    except Exception as exc:  # noqa: BLE001 - the old item is already "rewritten": re-queue
        print(f"rewrite: saving rewrite #{number} failed part-way: {exc!r}")
        if was_live and not taken_down:
            # It failed before anything was written, so the article is still published and whole.
            # Try once more; if it still cannot come down, it must not go to the inbox.
            try:
                _take_down(article)
                taken_down = True
            except Exception as down_exc:  # noqa: BLE001
                print(f"rewrite: could not take {article['article_id']} down: {down_exc!r}")
                reason = f"the rewrite was ready but the article could not be taken down ({down_exc})"
                put_moderation_item(
                    queue_id=new_queue_id,
                    article_id=article["article_id"],
                    topic_id=item["topic_id"],
                    reasons=[SENT_BACK_REASON],
                    created_at=datetime.now(UTC).isoformat(),
                    status=REWRITE_FAILED_STATUS,
                    extra={
                        "last_rewrite_error": reason,
                        "rewrite_requested_at": item.get("rewrite_requested_at"),
                    },
                )
                return {"status": "failed", "queue_id": queue_id, "reason": reason, "still_published": True}
        put_moderation_item(
            queue_id=new_queue_id,
            article_id=article["article_id"],
            topic_id=item["topic_id"],
            reasons=[
                f"a rewrite finished but could not be saved completely ({exc}): "
                "read the whole text before approving"
            ],
            created_at=datetime.now(UTC).isoformat(),
        )
    result = {
        "status": "rewritten",
        "queue_id": queue_id,
        "new_queue_id": new_queue_id,
        "article_id": article["article_id"],
    }
    if was_live:
        result.update({"unpublished": True, **_clear_traces(article)})
    return result


def _save(
    *,
    item: dict,
    article: dict,
    number: int,
    new_queue_id: str,
    stored_body: str,
    new_title: str,
    new_body: str,
    calls: list[dict],
    fresh_record: dict | None,
    compliance_review: dict,
    issues: list[str],
    instructions: str = "",
    extra_reasons: list[str] | None = None,
    edits: int = 0,
    kept_percent: int = 100,
) -> None:
    """Write a finished rewrite: keep the replaced text, store the new one, update the article's
    title/lineage/review/history, and put it back in the inbox as `new_queue_id`. `edits` is how
    many passages it changed and `kept_percent` how much of the article no edit touched."""
    article_id = article["article_id"]
    model_id = item["rewrite_model_id"]
    previous_key = f"articles/{article_id}.before-rewrite-{number}.md"
    s3 = boto3.client("s3")
    bucket = os.environ["CONTENT_BUCKET"]
    s3.put_object(
        Bucket=bucket, Key=previous_key, Body=stored_body.encode("utf-8"), ContentType="text/markdown"
    )
    s3.put_object(
        Bucket=bucket, Key=article["body_s3_key"], Body=new_body.encode("utf-8"), ContentType="text/markdown"
    )

    lineage, cost_aud = _record_calls(article, calls)
    label = model_label(model_id, pricing_for(model_id))
    finished_at = datetime.now(UTC).isoformat()
    rewrite = {
        "number": number,
        "model_id": model_id,
        "model_label": label,
        "requested_at": item.get("rewrite_requested_at"),
        "finished_at": finished_at,
        "cost_aud": cost_aud,
        "previous_title": article.get("title") or "",
        "previous_body_s3_key": previous_key,
        "issues": issues,
        "edits": edits,
        "kept_percent": kept_percent,
    }
    if instructions:
        rewrite["instructions"] = instructions
    update_article_after_rewrite(
        article_id,
        title=new_title,
        lineage=lineage,
        review=fresh_record,
        rewrites=list(article.get("rewrites") or []) + [rewrite],
    )
    put_moderation_item(
        queue_id=new_queue_id,
        article_id=article_id,
        topic_id=item["topic_id"],
        reasons=list(extra_reasons or []) + list(compliance_review["reasons"]),
        created_at=finished_at,
        review_notes=fresh_review.review_notes(fresh_record),
        rewrite={
            k: rewrite[k]
            for k in (
                "number",
                "model_id",
                "model_label",
                "cost_aud",
                "previous_title",
                "instructions",
                "edits",
                "kept_percent",
            )
            if k in rewrite
        },
    )
    print(
        f"rewrite: rewrote article_id={article_id} (#{number}, {label}; {edits} edit(s), "
        f"{kept_percent}% kept as it was); back in the inbox"
    )
