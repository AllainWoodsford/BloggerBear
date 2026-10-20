# Enhancement: Professor Grizzlewick, the reviewer bear

**Status:** built · **First draft:** 2026-10-20

An old, bespectacled, mustachioed bear who does the reviews the human admins have fallen behind on.
He is woken every second day ("I've been woken up from slumber, the admins are falling behind on
their reviews!"), works through what needs deciding, and reports on the Musings page as himself,
beside BloggerBear, so the feed reads as a conversation between the two.

Code: `lambdas/common/reviewer.py` (what he decides), `lambdas/reviewer_handler.py` (his Lambda),
`admin_api_handler.reviewer_*` (how he acts), `common/musings.py` (his voice),
`frontend/bears/grizzlewick.svg` (his portrait).

## What he does each run

A run plans its tasks from the tables, with no model call, then works through them two at a time on
one-shot tick schedules five minutes apart, so he never hits the model or the site all at once. The
tasks, in order:

1. **Content findings.** The assistant's content checks (`ops_mcp/content.py`) over the last week's
   articles. A title with markup or a body that is one code fence: sent for a rewrite with a fixed
   instruction. A musing that went out with no text: written again. The assistant's own follow-up
   then sees each one acted on (`actioned`) and, once fixed, drops it.
2. **Held articles that have waited two days or more.**
   - A fault the pipeline wrote itself (draft truncated, a title that reads like a refusal, a
     rewrite that could not be saved) is sent for a **rewrite**.
   - Otherwise the **minors** are ignored: minor fresh-data claims, and the compliance review's
     notes (`NOTE: ...`, `... flagged but not held: ...`), which by its own rule never hold an
     article. The **majors** (each reason the compliance review held it for, and each major claim
     of an *enforced* fresh-data review made of the text as it is now; a shadow review is
     record-only, and one older than the last rewrite is about text that is gone) are read by the
     topic's model as a skeptic: is it really a problem? A date is personal data only when it is
     tied to an identifiable person, so Wow Forever's beta windows and test dates are not. Any real
     major: **rewrite**, with his notes.
   - **The backoff is judged, not counted:** an article that has had `max_rewrites` (3) rewrites is
     **rejected** only if a real problem is still there; a rewrite that fixed things is approved.
   - **Nothing of his is approved unread.** With nothing real left, the topic's model reads the
     whole article (the published-review check: advice, a private person's details, a plainly
     wrong claim, broken text) before he **approves** it, if he may. A read that finds a problem is
     a rewrite (or the rejection, at the limit); one that cannot say leaves it for a person.
   - **Financial topics:** no majors at all and a passing read, he **approves** and flags it for a
     person; a real major, **rewrite**; majors that turned out not to matter, it **waits for a
     person**. (Compliance makes no model call for a financial topic, so the read is the only one
     his approval rests on.)
   - **A person's rewrite that never landed** (the item still says "sent back by a person", or has
     `last_rewrite_error`) is left for that person: its text is the one they turned down.
3. **Newly published articles**, at most five a run: the newest he has not reviewed, skipping the
   ones he tagged (`reviewer_reviewed_at`) and looking further back. Fine: tagged. Not: sent for a
   rewrite, and the tag taken off, since it needs reviewing again.
4. **His flags.** Each financial article he approved carries `reviewer_flag`. While it stands he
   checks its feedback since he approved it: a downvote whose comment got through screening, and
   he **takes it down and puts it back in the inbox**, shocked (`reviewer_hold`), for a person to
   review or rewrite. He only checks *that* a downvote carried a comment; the text is never read.
   An article he took down this way is **never his to decide again**: `reviewer_held_at` on the
   article says so for good (a person's rewrite replaces the queue item's reasons, never this).
   The hold writes the inbox item before it takes the article down, and voids the item if it
   cannot, so a held article always has an item to show it.

5. **Articles held ten days or more: the de-fanged publish.** These have waited for a person who
   never came (a financial article whose majors did not matter, an answer he could not read). A
   final review by the topic's model judges each major again and, for each real one, quotes the
   exact sentences that carry it, and says whether the article stands without them: nothing left
   refers to what goes, and the problem does not run through the whole of it. **The cut is made in
   code by deleting those verbatim sentences** (`cut_sentences`), so it can only take text out,
   never add or reword any; list items left empty go, and at least half the article must remain.
   - nothing real to cut: approved;
   - a plausible cut: the original body kept as `articles/<id>.before-defang.md`, the cut one
     published, and `reviewer_defanged` on the article (when, how many sentences, for how many
     flags);
   - not plausible, too little left, or a fault in the draft itself: rejected (his rejection musing);
   - a sentence he quoted that is not in the article: his slip, not the article's, so it waits for
     the next run.
   Never for a topic he may not approve for. A financial one is flagged for a person, like any
   approval of his. Articles under the threshold go to task 2 as before.

**Never a person's topic.** A topic with `force_manual_review` (every vision topic) is never his:
no task of any kind is planned for its articles, published or held.

## How he acts

Through the admin API's own functions (`reviewer_approve`, `reviewer_reject`, `reviewer_rewrite`,
`reviewer_regenerate_musings`, `reviewer_hold`), so every decision takes exactly an operator's path:
the same page rendering, musings, Stats refresh and queue states. Each is logged as a
`moderation_decision` with `by: reviewer`.

**Where his decisions are logged.** His `moderation_decision` lines (`by: reviewer`) are printed
by his own Lambda, so they are in `/aws/lambda/<prefix>-<env>-reviewer`, not the admin API's log
group. That is deliberate: the readers that count what a *person* decided (the vision evidence,
`evidence_export`'s human-control counts) read the admin API's group, and his decisions are not a
person's. They cannot go stale either, since he never touches a vision topic.

**Honest about who decided.** His tag and (financial) his flag are written *before* the approve,
and taken off again if it fails, so a published financial approval of his can never lack the flag
that asks a person to check it. His approvals are `published_by: "reviewer"`: the page's lineage line
says "AI reviewer", and the fact-check line says "reviewed by our AI reviewer before publishing",
never "a person". A held-for-downvotes article goes back to `pending_moderation` with the reason
"held by Professor Grizzlewick: readers downvoted it with comments after he approved it", where
`admin_cli approve`, the review inbox and the assistant's `awaiting_review` pick it up.

**Model choice.** The topic's own (`model_routing.resolve_model`): a model from its rotation, else
the project's default. His rewrites go through the same route, which also defaults to that model.

## His flags and the assistant

A flag reaches the operator as the assistant's `reviewer_financial_check` finding (pipeline health),
with `articles get <id>` as its command. The assistant remembers and follows it up like any other.
A person clears it with `admin_cli articles checked <id>` once they have looked (or takes the article
down with `articles unpublish`); he clears it himself when he holds the article for downvotes, or
once it is no longer published. A cleared flag makes the finding fixed, so the assistant's
follow-up drops it.

**He never touches the assistant's table.** It is the assistant's alone ("for looking at, not for
handing to another role"): every flag lives on the article, and the assistant reads it from there.

## His musings

Written with `character: "grizzlewick"`; the Musings page shows his portrait and "Professor
Grizzlewick was feeling …". Three kinds:

| Kind | When | Mood |
|---|---|---|
| `reviewer_report` | the end of a run: what he did, in counts. Opens with being woken when anything had waited two days. A run where he did nothing worth saying posts nothing: he slept through it | roused |
| `reviewer_rejection` | he rejected a draft: "it unfortunately did not make the pub test", and a bear joke. Timestamped a moment before the reject, so BloggerBear's own shocked rejection musing reads as the reply | stern |
| `reviewer_hold` | he took down an article he approved, after downvotes | shocked |

Like BloggerBear's, they name a topic but never an article's title, pass the comment screen, and fall
back to a plain line if the model's answer will not do. The report's facts are counts in fixed words:
nothing a model or a reader wrote reaches its prompt.

## Settings: changed with no deploy

Whether he is deployed at all is `REVIEWER_ENABLED` (GitHub variable) → `reviewer_enabled`, on
unless `false`: the one thing that needs a deploy. Everything else is the `reviewer` row of the
ModelConfig table (the same table and pattern as the model catalog's `model-catalog` row), read at
the start of each run: `admin_cli reviewer get|set`, `GET`/`PUT /reviewer-config`. A setting left
unset, or holding something invalid, takes its default.

| Setting | Default | Range |
|---|---|---|
| `enabled` (his scheduled runs) | on | |
| `may_approve` (project default; a topic's `reviewer_may_approve` wins, `force_manual_review` never) | on | |
| `fix_content`, `review_held`, `review_published`, `watch_flags`, `defang_held` (tasks 1-5) | on | |
| `stale_after_hours` | 48 | 1-720 |
| `published_per_run` / `published_lookback` | 5 / 25 | 1-25 / 1-200 |
| `max_rewrites` | 3 | 1-10 |
| `content_days` | 7 | 1-30 |
| `defang_after_days` | 10 | 3-90 |
| `tasks_per_tick` / `tick_minutes` | 2 / 5 | 1-5 / 1-60 |

## Infrastructure

One Lambda (`<prefix>-<env>-reviewer`, 600 s, 512 MB; he imports the admin API's code) on the shared
Lambda role, which already has every table, the content bucket, Bedrock, invoking the daily cycle
(for rewrites) and making `<prefix>-<env>-*` schedules (his ticks). New: the scheduler role's
`InvokeReviewer` statement, his log group and his place in the alarms, and the admin routes
`POST /articles/{article_id}/checked` and `GET`/`PUT /reviewer-config`. His schedule is `cron(0 5 1/2 * ? *)` Sydney, two hours after
the model catalog's reprice. His model calls are tallied on the Stats page as **Reviewer bear**.

## What the public pages say

The Terms (§2 and §3), the Privacy Policy (§4) and the About page say a held draft is reviewed by a
person or, after about two days, by the AI reviewer; that a financial article he approves is flagged
for a person to check and taken down again on downvotes with comments; and that for that he checks
only that a downvote carried a comment, never its text.

## First run

He decides the whole backlog as it stands: everything waiting two days or more. The runsheets say
how to clear what you want to decide yourself first, or turn his approvals off for a topic or the
project.
