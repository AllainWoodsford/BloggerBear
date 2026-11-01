# Handoff: the Model Ranch, prompts as assets, continuous evals and the queue platform

**Written:** 2026-11-01 (about 12:40 UTC) by the session that built the work
(https://claude.ai/code/session_01RwQmWXMY3cFd4o4UjrSQSQ).

**For:** a Claude session asked to review this work and carry it to `dev`.

**State:** every PR in the plan is open and none is merged. CI is green everywhere except one check that
is red on purpose (see [CI](#6-ci-today)). No PR has a review yet. Nothing has been deployed.

This page is a snapshot. The live state is on GitHub: read each PR before acting on it. The brief
is the spec, [model-ranch-prompt-assets-evals.md](model-ranch-prompt-assets-evals.md). It is
written for an executing agent, and its section 7.1 records every decision and carry-forward made
while building. Where this page and the spec disagree, the code wins, then the spec, then this page.

## Contents

- [1. Before you start](#1-before-you-start)
- [2. What was built](#2-what-was-built)
- [3. The PRs](#3-the-prs)
- [4. How the branches fit together](#4-how-the-branches-fit-together)
- [5. Merge order and retargeting](#5-merge-order-and-retargeting)
- [6. CI today](#6-ci-today)
- [7. The bug sweep (PR21, #415)](#7-the-bug-sweep-pr21-415)
- [8. The docs sweep (PR22, #416)](#8-the-docs-sweep-pr22-416)
- [9. Decisions, and where the code differs from this branch's spec](#9-decisions-and-where-the-code-differs-from-this-branchs-spec)
- [10. Open questions for the owner](#10-open-questions-for-the-owner)
- [11. Known issues left on purpose](#11-known-issues-left-on-purpose)
- [12. What the owner has to do](#12-what-the-owner-has-to-do)
- [13. How to review it](#13-how-to-review-it)
- [14. Setting up a fresh cloud session](#14-setting-up-a-fresh-cloud-session)
- [15. What did not survive the building session](#15-what-did-not-survive-the-building-session)
- [Appendix: the link checker](#appendix-the-link-checker)

## 1. Before you start

- **Another session may still be acting on these PRs.** The building session is subscribed to every
  PR below, so it wakes on their CI results, reviews and merges. It also has a safety-net check-in
  (`trig_01DUNLZfYE1wJsU69L8CvPk6`, due 2026-11-01 15:22 UTC). That check-in re-arms itself about
  every 4 hours and stops after three quiet ones. If you take the PRs over, ask the owner to tell
  that session to stand down (or to archive it), so that two sessions don't push to the same
  branches.
- **The rules that bind any session here** are in [CLAUDE.md](../../CLAUDE.md),
  [.github/copilot-instructions.md](../../.github/copilot-instructions.md) and spec section 6
  ("Working rules"):
  - Never push to `dev` or `prod`.
  - Never change branch or environment protection in a code PR.
  - Terraform is applied only by CI, except `infra/bootstrap`, which the owner applies by hand.
  - Production deploys only from a Release.
  - Never write a model name or the literal deployment prefix into a repository file.
  - No secrets in jobs that a pull request triggers.
  - No personal data.
- **Commits and PR bodies end with attribution lines.** Commits end with the `Co-Authored-By:` and
  `Claude-Session:` lines; PR bodies end with the "Generated with Claude Code" line and the session
  URL. Use your own session's lines.
- **Lint and tests before every push.** Run `ruff check --fix lambdas/ scripts/` with ruff 0.6.9.
  Never pass `--unsafe-fixes`, and never run `ruff format`. Before you push, run the whole suite
  as CI now does: `pytest lambdas/ scripts/ -n auto --dist loadfile` (dev's #414).

## 2. What was built

The spec's plan, section 7, is complete. Each item below is an open PR:

- **The queue platform.** The stack is Q0, Q1 and Q2.
  - A dispatcher runs every minute, with a FIFO work queue and one concurrency knob.
  - Shadow mode compares it with the per-topic EventBridge schedules.
  - Topics cut over to the queue one at a time, and can always go back.
- **Prompts as assets.** The stack is P0 and PR1 to PR10.
  - The pipeline's roughly 38 prompts are versioned templates, served from an S3 bucket, with A/B
    splits and a code fallback.
  - Their own gated workflow, `prompts.yml`, publishes them. Production publishing needs the
    `production-prompts` environment and the owner's review.
  - Five prompts stay code-only.
- **Continuous evals and the Model Ranch.** The stack is PR1b, E0, PR12 to PR20, PR11b, PR17,
  PR18b and PR18c:
  - scored events go to an `evals` log group, with an hourly rollup into DynamoDB;
  - ratings and gates are kept on the models table;
  - a rotation guard and a rewrite picker use those ratings;
  - culling works as proposal, then apply;
  - operators can forgive, punish, ungate and revoke;
  - model purpose is tracked;
  - an LLM judge is calibrated against people's verdicts;
  - offline scoring runs are started from the CLI, under a daily spend cap;
  - human review happens in `approve`;
  - call traces are opt-in;
  - the ops assistant has MCP and Alexa+ tools, and a Friday digest goes out;
  - two dashboards replace three.
- **Supporting PRs.**
  - #393 frees room under IAM's 10,240-character inline-policy cap for `lambda_exec`. It was not in
    the spec but is needed: the new policies would not fit otherwise.
  - #401 fixed a test whose fixed date had expired. It is already on `dev`.
- **PR21 (#415)** is the whole feature merged together with `dev`, then swept for bugs and fixed.
- **PR22 (#416)** is the documentation sweep. It is the only PR of the plan that changes `docs/`.

**Every switch defaults off.** The new switches are `PROMPT_AS_ASSET_ENABLED`, `EVALS_ENABLED`,
`dispatch_mode` per topic, `cull_enabled` and `trace_calls`. The owner turns them on in the order
of spec section 8. Until then, merging changes production behaviour in only one way: PR1b (#385)
stops the pipeline picking models that can't produce text, such as embedding models. The IAM
inline policies come to dev 9,043 and production 9,421 characters, against a 9,700 working cap.

## 3. The PRs

Milestones: "Prompt as Asset" for the prompts stack, "Model Evals" for everything else. "Parent"
is the PR's GitHub base. A PR's diff shows its parent's unmerged commits until that parent merges.

| Plan item | PR | Branch | Parent (base) | Milestone | What it does |
|---|---|---|---|---|---|
| PR0 spec | #380 | `claude/jolly-lovelace-1ty0ba` | `dev` | Model Evals | The spec, the PROGRESS checklist, and this page |
| `lambda_exec` budget | #393 | `claude/ranch-05-exec-role-budget` | `dev` | Model Evals | Shrinks `lambda_exec`'s inline policies so the feature's grants fit |
| Q0 | #383 | `claude/ranch-10-queue-wiring` | PR0 | Model Evals | Cron matcher, dispatch decisions, pipeline-config settings; inert |
| Q1 | #389 | `claude/ranch-11-work-queue` | Q0 | Model Evals | `work-queue` module: dispatcher every minute, `work.fifo`, alarms; shadow mode only |
| Q2 | #396 | `claude/ranch-12-queue-cutover` | Q1 | Model Evals | Per-topic cutover, its pre-flight, the way back, manual triggers |
| P0 | #382 | `claude/ranch-20-prompts-wiring` | PR0 | Prompt as Asset | Prompts switch and env keys, `prompt_fallback` event and alarm; inert |
| PR1 | #381 | `claude/ranch-21-delimiter-tag` | P0 | Prompt as Asset | Defangs `claims_to_fix` in the revision prompt |
| PR2 [B] | #386 | `claude/ranch-22-bootstrap-roles` | P0 | Prompt as Asset | Two publisher roles and the evals log-group right in `infra/bootstrap` |
| PR3 | #384 | `claude/ranch-23-prompts-core` | PR1 | Prompt as Asset | `common/prompts.py` and the registry: asset or vanilla, fallback codes |
| PR4 | #390 | `claude/ranch-24-prompts-infra` | PR3 | Prompt as Asset | `prompts` module (bucket, hash table), admin routes, `prompts` CLI |
| PR5 | #394 | `claude/ranch-25-prompts-publisher` | PR4 | Prompt as Asset | `prompts.yml`, `prompt_lint.py`, `publish_prompts.py` |
| PR6 | #387 | `claude/ranch-26-migrate-drafting` | PR3 | Prompt as Asset | Daily cycle prompts through `get_prompt` |
| PR7 | #388 | `claude/ranch-27-migrate-reviews` | PR6 | Prompt as Asset | Review prompts (compliance stays code-only) |
| PR8 | #392 | `claude/ranch-28-migrate-research` | PR7 | Prompt as Asset | Research and vision summary prompts |
| PR9 | #397 | `claude/ranch-29-migrate-rewrite` | PR8 | Prompt as Asset | Rewrite and reviewer-bear prompts |
| PR10 | #398 | `claude/ranch-30-migrate-small-prompts` | PR9 | Prompt as Asset | Screening, gear, reflection, pricing, musings, agent prompts |
| PR17 | #399 | `claude/ranch-48-review-steering` | PR7 | Model Evals | `topic_review_steering`: steers the fresh-data review, never compliance |
| PR1b [P] | #385 | `claude/ranch-40-model-purpose` | PR0 | Model Evals | Model purpose and suitability, field safety, catalog counters |
| E0 | #391 | `claude/ranch-41-evals-wiring` | PR1b | Model Evals | Evals switch, shared Logs Insights runner, evals stats category; inert |
| PR12 [B] | #395 | `claude/ranch-42-evals-core` | E0 | Model Evals | Event schema, `emit`/`parse`, the `evals` module with its switch off |
| PR13 | #400 | `claude/ranch-43-evals-rollup` | PR12 | Model Evals | Hourly rollup, `evals` settings row, `evals status` and digest |
| PR13b | #402 | `claude/ranch-44-evals-adjust` | PR13 | Model Evals | Forgive, punish, ungate, revoke |
| PR15 [P] | #403 | `claude/ranch-46-rotation-guard` | PR13 | Model Evals | Rotation guard and rewrite picker |
| PR16 [P] | #404 | `claude/ranch-47-culling` | PR15 (with PR13b merged in) | Model Evals | Culling in proposal mode, `evals cull --apply`, `models restore` |
| PR14 [P] | #405 | `claude/ranch-45-emission` | stacks-meet | Model Evals | The pipeline emits eval events |
| PR11b | #406 | `claude/ranch-31-dashboards` | stacks-meet | Model Evals | `pipeline_health` and `ranch_and_prompts` replace three dashboards |
| PR18 [P] | #407 | `claude/ranch-49-judge` | stacks-meet | Model Evals | The judge and the eval-work consumer |
| PR19 | #408 | `claude/ranch-52-human-review` | PR18 (with PR14 merged in) | Model Evals | Findings review in `approve`; people's verdicts as calibration pairs |
| PR18b | #409 | `claude/ranch-50-offline-runs` | PR18 | Model Evals | Offline scoring runs from the CLI |
| PR18c | #410 | `claude/ranch-51-call-traces` | PR14 | Model Evals | Opt-in call traces, private, 14 days |
| PR20 | #411 | `claude/ranch-53-mcp-tools` | PR19 (evals-meet) | Model Evals | `model_ranch` and `prompt_metrics` tools, the agent's cell rule, the Friday digest |
| PR21 | #415 | `claude/ranch-60-bug-sweep` | PR20 | Model Evals | Whole feature plus `dev`, swept and fixed |
| PR22 | #416 | `claude/ranch-61-docs-sweep` | PR21 | Model Evals | The documentation sweep |

The labels come from spec section 7:
- **[B]** needs the owner's `infra/bootstrap` apply first.
- **[P]** changes production behaviour once released and switched on.

## 4. How the branches fit together

- **Three stacks.** The queue stack (Q), the prompts stack (P) and the evals stack (E) all start
  from the spec branch. #393 starts from `dev`.
- **The stacks-meet branch,** `claude/ranch-55-stacks-meet` (809ce54), has no PR. It is PR13's tip
  with #393, Q2, PR10, PR17, PR4, PR5, PR2 and #401 merged in, and it is the base of PR14, PR11b
  and PR18. It exists because those three PRs need the evals and prompts stacks together.
- **The evals-meet branch,** `claude/ranch-56-evals-meet` (384ac59), has no PR either. It is the
  PR18 and PR14 merge, with PR13b, PR15 and PR16 (the culling branch) and then PR19 merged in. PR20
  is built on it. The meet commit 1cda0a6 had lost its second parent, so PR20's branch records it
  again with a merge that changes nothing (d7b4364).
- **PRs that carry other PRs.** PR16 contains PR13b, PR19 contains PR14, and PR20 contains PR13b,
  PR15, PR16 and PR19. Each one's diff shows those commits until they merge.
- **PR21 merges everything.** It contains every PR above, merged together and conflict-resolved
  (c6aa146). It then merges `dev` twice (003a563 brings #401 and #412; 28ca33f brings #414),
  followed by the sweep's fix parts A to H.
- **PR22 adds the docs.** It is PR21 plus the documentation sweep, with PR21's final tip and the
  spec branch's latest merged in.

## 5. Merge order and retargeting

- **#380 and #393 go first.** #393 must reach `dev` before, or together with, the first of #389,
  #390, #391, #395 and #400. Without it, their policies push `lambda_exec` over the IAM cap and the
  apply fails.
- **Then each PR after its parent.** Releases can bundle several.
- **PR12 (#395) waits for the bootstrap.** It merges only after the owner says "bootstrap applied"
  (spec section 7, the PR12 entry). The deploy role needs the evals log-group right that PR2 (#386)
  adds to the bootstrap.
- **#415 and #416 go last.**

**Retargeting.** When a parent merges, set each child's base to `dev`, merge `dev` into the child
and push. Then run ruff again, because merges are where import order (I001) breaks, and run the
parallel suite. The building session merged rather than rebased: a merge keeps everyone's
checkouts valid.

**`dev` has moved since most PRs were opened.**
- **#412** made Re-Write edit the article instead of writing a new one. It touches `rewrite.py`,
  `compliance.py` and `review_inbox.py`, which several PRs also change. PR21's merge commits
  003a563 and 728670e hold the worked resolutions. When each PR is retargeted:
  - **PR7 (#388):** keep dev's `fresh_data`, `_FRESH_DATA_LABEL` and `_source_text`. Render
    `get_prompt("reviews/compliance", {"source": _defang(source), ...})`, and have
    `_review_verdict` read the source before defanging.
  - **PR9 (#397):** as for PR7, plus re-migrate `rewrite/rewrite` v1 to #412's text. Take
    `_REWRITE`, the sha `d239dc3a1106cda6d065bda37cb07da7ae8ec59fe58185706b77635e851aed6d` and
    `v1.txt` from PR21's branch. `_REPLY_RULE` becomes #412's edits contract. In
    `test_rewrite_reviewer_prompts.py`, re-pin `V1_SHA256` and mock an edits reply. PR10 (#398)
    then takes the new PR9.
  - **PR14 (#405):** `test_rewrite.py`'s `_run` takes both `compliance_error` and #412's `mocks`.
  - **PR18c (#410):** the second Re-Write in `test_call_traces.py` quotes a passage the first one
    left (`SECOND_EDIT`).
  - **PR19 (#408):** `review_inbox.py` keeps both `FindingsSource` and #412's
    `_is_problem_note`.
  - **PR18b (#409):** already carries the run-id pin in `test_eval_score.py` (ad471b5).
- **#414** makes CI run `pytest lambdas/ scripts/ -n auto --dist loadfile`, and pins pytest-xdist
  3.8.0 and execnet 2.1.2. PR21 merged it, and the whole suite passes in parallel there. The other
  branches pick it up when `dev` is merged in.
- **#401** is on `dev` too, and every branch built after 2026-10-31 already carries it.

## 6. CI today

The snapshot was taken at about 12:30 UTC on 2026-11-01:
- **Mergeability.** Every open PR's mergeable state is `clean`, except #415's, which is `unstable`
  because of the one red check below.
- **Reviews.** No PR has a review or an unresolved review thread.
- **Comments.** The only PR comment is the building session's explanation on #415.

| PR | Checks |
|---|---|
| #380 | 6 success (its diff is docs only, so the Python and prompts workflows don't run) |
| #381 to #393, #395 to #406, #408, #410 | 8 success each |
| #394, #407, #409, #411 | 9 success; the prompts workflow's `publish-dev` and `publish-production` are skipped by design while publishing is off |
| #415 | 8 success, 2 skipped, 1 failure: `gates` (the prompts workflow), red on purpose, see below |
| #416 | 9 success, 2 skipped; `gates` passes |

**Why #415's `gates` check is red.** The prompt lint holds each prompt version to the bytes it had
on the PR's base, and #415's base is PR20's branch. That branch still has PR9's `rewrite/rewrite`
v1, written before #412. #415 merged `dev`, and #412's code needs #412's prompt text, so v1's bytes
changed. Section 7.1 ("PR21 decided (#412 on dev)") keeps the name v1 rather than adding a v2,
because no v1 was ever published: PR9 isn't merged, and production's registry (v0.3.8) doesn't
have the prompt.

The check turns green once PR9 is re-migrated (section 5) and #415 is retargeted to `dev`. A
v2 would turn it green now, but the next `dev` merge would have to undo it, so the building
session explained this in a comment on #415 instead and offered the owner the v2 if they want it.

## 7. The bug sweep (PR21, #415)

#415's description is the full record. In short, three kinds of fix went into the branch, each
through implement, three review lenses, three skeptics per finding, and a fix stage:

- **Parts A to C: the carry-forwards in section 7.1.**
  - **Q2 shadow counters.** `shadow_clean_days` was added, and the cutover needs at least one clean
    day.
  - **Restore, resume and cutover.** A restore or a resume writes today's daily claim once today's
    turn has passed, and the cutover deletes the schedules once.
  - **The reviewer bear's prompts.** They move to v2, with defanged `article_text` and
    `review_flags` blocks.
  - **Prompt Fallback events.** They are now emitted.
  - **`POST /evals/adjust`.** It now checks the prompt version it is given.
  - **Pair rows.** They carry `rewrite_number`.
  - **The judge's spend hold.** It is held on the day's `spend` row (`held_aud`).
- **Parts D to G: 14 findings from a whole-feature sweep.** Eight finders swept the feature by
  area. Each finding was reproduced by a failing test and checked by a skeptic before it was fixed.
  - **Queue:**
    - **S0:** a move to the queue is refused until `topics reschedule` has written the current
      schedules.
    - **S2:** a failed fetch is no longer counted as a shadow mismatch (`last_research_attempt_at`).
    - **S6:** an infrastructure refusal of a daily start raises after the whole tick.
    - **S7:** a released claim gives back `daily_claim_n`.
  - **Ranch:**
    - **S3:** inference profiles are classified, not marked `unknown`.
    - **S11:** `models add` writes conditionally, and answers 409 on a second conflict.
    - **S12:** the cull refusals name `model-catalog set --cycle on`.
  - **Assistant:**
    - **S5:** the cull card counts only what an apply would take.
  - **Evals:**
    - **S10:** the digest's "gated" follows the rotation guard.
  - **Prompts:**
    - **S8:** the last good settings read is kept, and a cold container fails closed.
    - **S9:** the fallback metric filters cover eval-jobs' logs.
  - **Judge:**
    - **S1:** the author check compares base model ids.
    - **S4:** a run over the whole daily cap is refused with 409.
    - **S13:** pre-claim failures close the message, and eval-work redelivers through
      `batchItemFailures`.
- **Part H: five small fixes.**
  - Evidence tokens are matched whole.
  - on-demand-scan runs the bootstrap's plan test.
  - The bootstrap's `domain_name` description is corrected.
  - `FALLBACK_DAYS` is now 3.
  - A scoring result whose bookkeeping failed twice is delivered again.

**Checks on the branch:**
- **Tests.** The full suite passed serially on bec4b9e and in parallel on 28ca33f: 10,335 passed,
  6 skipped, 1 xfailed.
- **Linting and generated files.** ruff and the CLI reference check pass.
- **Terraform.** `terraform fmt` is clean, and `validate` passes offline for both roots. The
  module tests pass for `evals` and `bootstrap`.
- **Secrets and links.** gitleaks and the link check are clean.

## 8. The docs sweep (PR22, #416)

#416's description is the full record:

- **Two new architecture pages:** `docs/architecture/queue-platform.md` and
  `docs/architecture/prompts-and-evals.md`.
- **The operating docs**, covering configuration, the runsheets and observability.
- **The using docs**, covering models, the evaluation runsheet, CI and `scripts/README.md`.
- **The record:**
  - every PROGRESS box is ticked;
  - the spec and the old dispatcher design move to `docs/enhancements/archive/`;
  - links are updated across the repository.

It also changes some code:
- **The assistant's walk-throughs.** Two new `FEATURES` walk-throughs (`queue-platform`,
  `prompts-and-evals`) are added in `lambdas/ops_mcp/architecture.py`.
- **The agent's prompt.** To route to the walk-throughs, the code-only prompt `ops_agent/system`
  goes from v2 to v3. This is the PR's only behaviour change.
- **Help text.** Wrong `prompts pause`/`prompts set` help is corrected, and the CLI reference is
  regenerated.

It was written against PR21's code. Five agents then reconciled it with PR21's last fix parts.
The full suite passes in parallel there: 10,359 passed, 6 skipped, 1 xfailed.

**This page postdates #416.** When #416 is next updated, move this file to
`docs/enhancements/archive/` with the other archived specs, and add an index row there.

## 9. Decisions, and where the code differs from this branch's spec

- **The decisions are in the spec.** Every decision taken while building is a line in spec section
  7.1:
  - "PR21 decided (...)" lines;
  - per-PR "(from ...)" carry-forwards;
  - "PR22 (from ...)" documentation obligations.

  Don't reopen what the spec calls decided. Section 3 lists the deviations from the owner's
  original spec, each with its reason.
- **Where this branch's copy of 7.1 is wrong.** PR22's reconcile found four points where the code
  differs from 7.1 as this branch has it. PR22's archived copy of the spec
  (`docs/enhancements/archive/model-ranch-prompt-assets-evals.md` on #416) is corrected, and the
  docs follow the code:
  1. **A research mismatch.** It doesn't clear the recorded research turn. It is dated in
     `shadow_research_mismatch_at`, and the cutover pre-flight waits for a later
     `shadow_research_matched_at`. `topics schedule` shows neither date.
  2. **A restore's daily claim.** A restore keeps the newest of three claims: the snapshot's, the
     live row's in the same zone, and today's once its turn has passed.
  3. **The cutover pre-flight.** It needs one clean day (`shadow_clean_days >= 1`). "A few days
     clean in a row" is advice, not a rule.
  4. **A pause or the kill switch.** It lands within 5 minutes (the settings cache, `CACHE_TTL_S`).
     The spec's 600 s applies only to a superseded version, which can be served for up to ten
     minutes (cache plus retire grace).

## 10. Open questions for the owner

These are unanswered. They are on #415 and in 7.1's "Owner (from PR21)" line:

1. **Judge calibration.** Should it use Cohen's kappa, or a per-verdict agreement with its own floor
   for people's "bad" verdicts? Should Findings and articles be measured apart? Today it is a raw
   pooled share.
2. **Web text under plain labels.** `research_tick/summary`, the `crypto_feed/*` summaries,
   `model_catalog/web_price` and the daily cycle's findings blocks put web-derived text under plain
   labels, with no block and no defang. Should they be wrapped as the reviewer bear's prompts now
   are?
3. **The double comma.** With a person's note, #412's rewrite prompt reads "issues_to_fix,,
   changing". A fix on `dev` before PR9 ships keeps v1; a fix after it needs a v2.
4. **The digest's 500.** The digest answers 500, and the Friday email retries an hour later, when
   the model-config default row can't be read. Is that acceptable?
5. **`ValidationException`.** Should it leave `dispatch._TOPICS_OWN`? StartExecution can raise it
   for the shared state machine ARN, and today that only skips topics, with no alarm.
6. **The vision worker's own evidence patterns.** `vision_worker_handler.py` keeps an anchored copy,
   because it can't import `evidence`. Should it match whole values too?
7. **Evals as a CI gate (from the owner, not yet specced).** The owner asked how to run evals as a
   separate job in the prompts workflow, beside lint and security. The building session answered
   with a design and offered to spec it as a new PR. There was no reply yet.
8. **Dashboard billing.** After PR11b's dev apply, check whether pipeline-health counts as a free
   dashboard. The docs name the Cost Explorer usage types to look at.

## 11. Known issues left on purpose

All are low impact, and each is recorded on #415 or #416:

- **The restore race.** The dispatcher can claim today between a restore's read and its batch put.
  Closing it needs a conditional write, which a batch put can't do.
- **Two restore cases.** Both fall back to the snapshot:
  - a queue snapshot restored over a schedules row whose Scheduler ran today;
  - a topic whose time zone changed since the snapshot.
- **The fallback alarm while the kill switch is on.** It is in ALARM in every hour that has an
  affected render, and back to OK (with an email) in an hour that has none. Only asset prompts
  write fallback lines.
- **The heartbeat alarm's description.** The work-queue heartbeat alarm says nothing is being
  dispatched. That isn't true for a tick that raises after a refused start, because research still
  goes out. The Terraform text is left for a code PR.
- **A judge run whose header read keeps failing** may never close.
- **A non-ClientError inside `_start_daily`** ends the tick at that topic.

## 12. What the owner has to do

- Review and merge in the order of section 5.
- After #386 merges:
  - apply `infra/bootstrap` by hand;
  - create the `production-prompts` GitHub environment (required reviewer: the owner; deployment
    branch `dev`);
  - set `AWS_DEV_PROMPTS_ROLE_ARN` and `AWS_PROD_PROMPTS_ROLE_ARN` at repository level from the
    bootstrap's outputs;
  - tell the agent "bootstrap applied" so that #395 can merge.
- Turn the parts on in the order of spec section 8, which is also in PR22's production runsheet:
  1. topics move to the queue one at a time, after shadow mode is clean;
  2. `PROMPT_AS_ASSET_ENABLED` on in dev, with the `production` environment's own value `false`
     until production should read assets;
  3. `EVALS_ENABLED` on;
  4. production after a Release.
- Run `admin_cli models classify` once per environment after PR1b's first deploy. Until a row is
  classified, the old rule for unclassified rows decides.
- Answer section 10.

## 13. How to review it

1. **Read the spec,** sections 2 to 6 and 7.1, then #415's and #416's descriptions.
2. **Review the whole feature as it will land.** Diff PR21 against dev:
   `git diff origin/dev...origin/claude/ranch-60-bug-sweep`, about 30 PRs at once. For
   per-PR review, each PR's own diff against its parent is smaller, but it shows its parent's
   unmerged commits.
3. **The areas most worth an independent look:**
   - **IAM:**
     - `lambda_exec`'s inline policies and the 10,240 cap (#393);
     - the publisher roles' trust (`sub` strings) in `infra/bootstrap/main.tf`;
     - the deploy role's evals log-group right.
   - **Double runs:**
     - the dispatcher's claims (`common/dispatch.py`);
     - the cutover and its way back (`admin_api_handler._update_topic`, `_move_to_queue`);
     - the restore and resume claims (`common/backup.py`, `_set_topic_paused`).
   - **Prompts:**
     - the hash check, pause and kill switch (`common/prompts.py`);
     - the publisher's release order (`scripts/publish_prompts.py`);
     - no secrets in `prompts.yml` jobs that a pull request can trigger.
   - **Evals:**
     - judge ≠ author (`common/eval_judge.py`);
     - the spend cap at enqueue and at consume;
     - offline runs never touching prompt assets (a test scans for it);
     - culling archives a model before deleting it.
   - **Personal data and untrusted text:**
     - events carry ids and codes only, never free text;
     - traces hash untrusted values;
     - voice callers hear no untrusted text.
4. **Run the checks** (section 14):
   - ruff;
   - the parallel suite;
   - `generate_cli_reference.py --check`;
   - `terraform fmt -check -recursive infra/`;
   - `terraform test` in `infra/bootstrap` and each module with tests;
   - `terraform validate` in both roots;
   - gitleaks over the branch's commits;
   - the link check.
5. **Report what you find.** Do it on the PR it belongs to, or to the owner. A fix to a stacked PR
   means merging that PR's branch forward into its children, then into PR21 and PR22.

## 14. Setting up a fresh cloud session

- **Python 3.11.**
  - Create the environment:
    `python3.11 -m venv .venv && . .venv/bin/activate && pip install -r lambdas/requirements-dev.txt -r scripts/requirements.txt`.
  - On a branch without #414, also run `pip install pytest-xdist==3.8.0 execnet==2.1.2`.
  - Never commit `.venv`.
- **ruff.** The SessionStart hook (`.claude/hooks/session-start.sh`) puts ruff 0.6.9 first on
  PATH, and `ruff --version` must say 0.6.9.
- **CLI reference.** Regenerate `lambdas/ops_mcp/cli_reference.json` with
  `python scripts/generate_cli_reference.py` whenever a parser changes. Check it with `--check`;
  CI runs Python 3.11.
- **Terraform offline.** The registry is blocked in cloud sessions.
  - Download Terraform 1.16.2 from `releases.hashicorp.com`.
  - Download the providers into a filesystem mirror: aws 6.64.0, archive 2.8.1 and random 3.9.1.
  - Point `TF_CLI_CONFIG_FILE` at a `.terraformrc` with
    `provider_installation { filesystem_mirror { path = "<mirror>" include = ["registry.terraform.io/hashicorp/*"] } direct { exclude = ["registry.terraform.io/hashicorp/*"] } }`.
  - Module tests: `TF_DATA_DIR=<scratch> terraform -chdir=infra/modules/<m> init -backend=false && terraform test`.
    Then delete the `.terraform.lock.hcl` it writes.
  - Roots: `init -backend=false && validate`. Back up the committed lock files first and restore
    them after.
- **gitleaks.** Download the release binary, then run
  `gitleaks git --log-opts="<base>..HEAD" --redact --no-banner --config .gitleaks.toml .`.
  Also run `python3 scripts/pii_denylist_check.py --staged`.
- **Link check.** The repository has no link checker for its markdown. The building session used
  the one in the [appendix](#appendix-the-link-checker).
- **Merges with conflicts.** Use `git -c merge.conflictStyle=diff3 merge --no-ff -X diff-algorithm=histogram <branch>`:
  diff3 shows the base side, and histogram aligns functions better than the default diff.

## 15. What did not survive the building session

- **Workflow scripts.** The building session ran each PR through a Workflow script: implement,
  three review lenses, three skeptics per finding, then a fix. It used the same shape for the bug
  sweep and the docs sweep. Those scripts lived in that session's own directories, not in the
  repository. Their results are in each PR's description and commits.
- **The bug sweep's part branches.** `claude/ranch-60-sweep-a` to `-g` were local only and were
  never pushed. Their commits are in `claude/ranch-60-bug-sweep` through its merge commits.
- **The scratchpad.** It held worktrees, logs, the Terraform mirror and the gitleaks binary, and
  is gone once the container is reclaimed.

## Appendix: the link checker

Run it with `python3 linkcheck.py <repo root>`. It checks every relative markdown link, and its
`#anchor`, in the repository's `.md` files outside `.git` and `node_modules`. It prints one line
per problem, then the count on stderr.

```python
"""Check every relative markdown link (and its #anchor) in the repo's .md files."""
import re
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
FENCE = re.compile(r"^\s*(```|~~~)")


def anchors(path: Path) -> set[str]:
    out, seen, fenced = set(), {}, False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not m:
            for a in re.findall(r'<a\s+(?:name|id)="([^"]+)"', line):
                out.add(a)
            continue
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(2))
        slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
        n = seen.get(slug, 0)
        seen[slug] = n + 1
        out.add(slug if n == 0 else f"{slug}-{n}")
    return out


cache: dict[Path, set[str]] = {}
bad = []
for md in sorted(ROOT.rglob("*.md")):
    if ".git" in md.parts or "node_modules" in md.parts:
        continue
    fenced = False
    for lineno, line in enumerate(md.read_text(encoding="utf-8").splitlines(), 1):
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = re.sub(r"`[^`]*`", "``", line)
        for _, target in LINK.findall(line):
            if re.match(r"^[a-z]+:", target) or target.startswith("mailto:"):
                continue
            path_part, _, frag = target.partition("#")
            if path_part.startswith("/"):
                dest = ROOT / path_part.lstrip("/")
            elif path_part:
                dest = (md.parent / path_part).resolve()
            else:
                dest = md
            if not dest.exists():
                bad.append(f"{md.relative_to(ROOT)}:{lineno}: missing file {target}")
                continue
            if frag and dest.suffix == ".md":
                if dest not in cache:
                    cache[dest] = anchors(dest)
                if frag not in cache[dest]:
                    bad.append(f"{md.relative_to(ROOT)}:{lineno}: missing anchor {target}")
print("\n".join(bad))
print(f"{len(bad)} problems", file=sys.stderr)
```
