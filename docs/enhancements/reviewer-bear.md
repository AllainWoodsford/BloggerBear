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
   - An article rewritten three times already is **rejected** (the backoff).
   - A fault the pipeline wrote itself (draft truncated, a title that reads like a refusal, a
     rewrite that could not be saved) is sent for a **rewrite**.
   - Otherwise the **minor** fresh-data claims are ignored and the **majors** (each reason the
     compliance review held it for, each major fresh-data claim) are read by the topic's model as
     a skeptic: is it really a problem? A date is personal data only when it is tied to an
     identifiable person, so Wow Forever's beta windows and test dates are not. Any real major:
     **rewrite**, with his notes. None: **approve**, if he may (below). A model answer he cannot
     read leaves it for a person: he never approves on a non-answer.
   - **Financial topics:** no majors at all, he **approves** and flags it for a person; a real
     major, **rewrite**; majors that turned out not to matter, it **waits for a person**.
3. **Newly published articles**, at most five a run: the newest he has not reviewed, skipping the
   ones he tagged (`reviewer_reviewed_at`) and looking further back. Fine: tagged. Not: sent for a
   rewrite, and the tag taken off, since it needs reviewing again.
4. **His flags.** Each financial article he approved carries `reviewer_flag`. While it stands he
   checks its feedback since he approved it: a downvote whose comment got through screening, and
   he **takes it down and puts it back in the inbox**, shocked (`reviewer_hold`), for a person to
   review or rewrite. He only checks *that* a downvote carried a comment; the text is never read.
   An article he took down this way is **never his to decide again**: however long it waits, it is
   left for a person.

## How he acts

Through the admin API's own functions (`reviewer_approve`, `reviewer_reject`, `reviewer_rewrite`,
`reviewer_regenerate_musings`, `reviewer_hold`), so every decision takes exactly an operator's path:
the same page rendering, musings, Stats refresh and queue states. Each is logged as a
`moderation_decision` with `by: reviewer`.

**Honest about who decided.** His approvals are `published_by: "reviewer"`: the page's lineage line
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

## Switches

| Setting | Default | Effect |
|---|---|---|
| `REVIEWER_ENABLED` (GitHub variable) → `reviewer_enabled` | on | Deploys his Lambda and schedule; `false` deploys neither |
| `REVIEWER_MAY_APPROVE` (GitHub variable) → `reviewer_may_approve` → his Lambda's `REVIEWER_MAY_APPROVE` | on | The project's default for whether he may approve, financial or not |
| a topic's `reviewer_may_approve` | unset | Overrides the project's default (`topics update --reviewer-may-approve on\|off`, `""` to clear) |
| a topic's `force_manual_review` | off | On: he never approves its articles (the vision topics) |

The constants (5 published articles a run, 2 days before a held article is his, 3 rewrites before he
rejects, 2 tasks a tick, 5 minutes between ticks) are in `common/reviewer.py`.

## Infrastructure

One Lambda (`<prefix>-<env>-reviewer`, 600 s, 512 MB; he imports the admin API's code) on the shared
Lambda role, which already has every table, the content bucket, Bedrock, invoking the daily cycle
(for rewrites) and making `<prefix>-<env>-*` schedules (his ticks). New: the scheduler role's
`InvokeReviewer` statement, his log group and his place in the alarms, and the admin route
`POST /articles/{article_id}/checked`. His schedule is `cron(0 5 1/2 * ? *)` Sydney, two hours after
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
