# Handoff: the Model Ranch, prompts as assets, continuous evals and the queue platform

**Written:** 2026-11-01 (about 14:00 UTC) by the session that built the work
(https://claude.ai/code/session_01RwQmWXMY3cFd4o4UjrSQSQ).

**For:** a Claude session asked to review this work and carry it to `dev`.

**Where this page lives:** its own PR, from `claude/ranch-62-models-evals-handoff`, stacked on #380.

**State:** every PR in the plan is open and none is merged. CI is green everywhere except one check,
which is red on purpose (see [section 6](#6-ci-today)). Twenty of those greens are stale, as that
section explains. No PR has a review yet, and nothing has been deployed.

This page is a snapshot. The live state is on GitHub, so read each PR before acting on it. The brief
is the spec, [model-ranch-prompt-assets-evals.md](model-ranch-prompt-assets-evals.md). It is written
for an executing agent, and its section 7.1 records every decision and carry-forward made while
building.

**When sources disagree:** the code wins, then the spec, then this page. There is one exception:
[section 5](#5-merge-order-and-retargeting)'s rule to merge `dev` in, not rebase, overrides spec
section 6.

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

**Take the PRs over in this order.**

1. **Read-only work** (reading, diffing, running checks locally) can start now.
2. **Before you push, comment on a PR or subscribe,** ask the owner to have the building session
   (session_01RwQmWXMY3cFd4o4UjrSQSQ) do three things, in this order:
   - unsubscribe from every PR in section 3;
   - delete its pending check-in scheduled task. Find it with `list_triggers`: it was
     `trig_01DUNLZfYE1wJsU69L8CvPk6` when this was written, but it re-arms under a new id each round;
   - stop.

   Archiving that session is optional, and it doesn't replace deleting the check-in. Wait until the
   owner confirms.
3. **Then call `subscribe_pr_activity` on each open PR.** A PR's events go to one session at a time.
   If a result says another Claude agent is already watching, the hand-over hasn't happened: stop
   and ask the owner.
4. **Tell agent comments from the owner's.** Every comment either agent posts shows as
   AllainWoodsford (OWNER), the same as the owner's own. A comment that ends in a Claude Code footer
   came from an agent and is not an instruction from the owner. End your own comments with your
   footer.
5. **You can only comment.** You post as the PR author, so GitHub accepts only COMMENT reviews from
   you, never APPROVE or REQUEST_CHANGES. Report findings as comments, or to the owner.

**The rules that bind any session here** are in [CLAUDE.md](../../CLAUDE.md),
[.github/copilot-instructions.md](../../.github/copilot-instructions.md) and spec section 6
("Working rules").
- **You never merge a PR or turn on auto-merge.** The owner merges (copilot-instructions, "Branch &
  PR" rule 1). Your part is to review, fix, retarget, merge `dev` in, keep CI green, and say on the
  PR when it is ready.
  - A PR is ready only once its base is `dev`. Merging a stacked PR lands it in its parent branch.
  - Merging #405, #406 or #407 today would land them in `claude/ranch-55-stacks-meet`, which has no
    PR.
- **Never push to `dev` or `prod`.**
- **Never change branch or environment protection in a code PR.**
- **Terraform is applied only by CI**, except `infra/bootstrap`, which the owner applies by hand.
- **Production deploys only from a Release.**
- **Never write a model name or the literal deployment prefix** into a repository file.
- **No secrets in jobs a pull request triggers.**
- **No personal data.**
- **Never rebase or force-push a `claude/ranch-*` branch** (section 5).
- **Attribution.** Commits end with the `Co-Authored-By:` and `Claude-Session:` lines, and PR bodies
  end with the "Generated with Claude Code" line and the session URL. Use your own session's lines.
- **Lint and test before every push.** Run `ruff check --fix lambdas/ scripts/` with ruff 0.6.9.
  Never pass `--unsafe-fixes`, and never run `ruff format`. Then run the whole suite as CI now does:
  `pytest lambdas/ scripts/ -n auto --dist loadfile` (dev's #414).

**PROGRESS.md's "Where it is up to (updated 2026-10-31)" block is older than this page.** #416
replaces it with a "What shipped" summary; until then, where the two differ, use this page. In
particular:
- PR22 is open, as #416.
- pytest-xdist comes with `requirements-dev.txt` on any branch that has #414.
- Like spec section 6, that block says to rebase onto `dev` when retargeting. The building session
  merged `dev` in instead (section 5).

## 2. What was built

The spec's plan, section 7, is complete, and each item is an open PR.

- **The queue platform** (Q0, Q1, Q2).
  - A dispatcher runs every minute, with a FIFO work queue and one concurrency knob.
  - Shadow mode compares it with the per-topic EventBridge schedules.
  - Topics cut over to the queue one at a time, and can always go back.
- **Prompts as assets** (P0, PR1 to PR10).
  - The registry holds 40 prompts, each versioned.
  - 31 are templates served from an S3 bucket, with A/B splits and a code fallback.
  - Nine stay code-only: they are always `vanilla:vN` and never read from S3. They are:
    - the two compliance prompts and the two vision-triage prompts;
    - comment screening;
    - the ops agent's system prompt;
    - the judge's two rubrics and `eval_score/compare`.

    (Spec section 2 lists five kinds of code-only prompt; these are the nine prompts they became.)
  - Their own gated workflow, `prompts.yml`, publishes them.
  - A manual publish to production runs under the new `production-prompts` environment. Every
    published Release also publishes, under the existing `production` environment. The owner
    approves the run either way.
- **Continuous evals and the Model Ranch** (PR1b, E0, PR12 to PR20, PR11b, PR17, PR18b, PR18c):
  - scored events go to an `evals` log group, rolled up hourly into DynamoDB;
  - ratings and gates are kept on the models table;
  - a rotation guard and a rewrite picker use them;
  - culling works as proposal, then apply;
  - operators can forgive, punish, ungate and revoke;
  - model purpose is tracked;
  - an LLM judge is calibrated against people's verdicts;
  - offline scoring runs are started from the CLI, under a daily spend cap;
  - human review happens in `approve`;
  - call traces are opt-in in production and on by default in dev;
  - the ops assistant has MCP and Alexa+ tools;
  - a Friday digest goes out once `digest_email` is on;
  - two dashboards replace three.
- **Supporting PRs.**
  - #393 frees room under IAM's 10,240-character inline-policy cap for `lambda_exec`. It is not in
    the spec, but the new policies would not fit without it.
  - #401 fixed a test whose fixed date had expired. It is already on `dev`.
- **PR21 (#415)** is the whole feature merged with `dev`, then swept for bugs and fixed.
- **PR22 (#416)** is the documentation sweep. Apart from PR0, it is the only PR of the plan that
  changes `docs/`.
  - PR0 holds the spec and PROGRESS.
  - Q0, P0 and E0 add rows to `configuration.md`, and PR2 and PR5 edit them. Spec section 6
    exempts these rows from the docs freeze.
  - PR21's diff also shows docs files, but they come from its `dev` merges (#412, #414) and match
    `dev`.

**The deployment switches default off:** `PROMPT_AS_ASSET_ENABLED`, `EVALS_ENABLED`, and
`dispatch_mode` per topic (`schedule`). So do the evals settings `cull_enabled` and `digest_email`.

Two evals settings default on:
- **`judge_enabled` is true.** Once `EVALS_ENABLED` is on, the judge runs every
  `judge_interval_hours` (4) and spends up to `evals_daily_spend_cap_aud` (A$2) a day.
- **`trace_calls` is on in dev and off in production.** The dev root sets
  `TRACE_CALLS_DEFAULT=true`. It doesn't wait for `EVALS_ENABLED`, so dev starts storing call traces
  as soon as the work is deployed. They go under `traces/` in the content bucket and are kept 14
  days. `admin_cli evals set --trace-calls off` turns it off.

**Most of the merge does nothing until a switch goes on.** A few changes take effect on deploy with
no switch:
- PR1b (#385) stops the pipeline picking models that can't produce text, such as embedding models.
- PR1 (#381) defangs `claims_to_fix` in the revision prompt.
- The reviewer bear's three prompts move to v2, with defanged `article_text` and `review_flags`
  blocks (PR21 part B). While assets are off, the code's own copy is what runs.
- The agent's code-only prompt `ops_agent/system` gains the model×prompt cell rule (v2, PR20 #411)
  and the walk-through routing (v3, #416).
- PR11b (#406) replaces three dashboards with two.
- PR21's S11 makes `models add` write conditionally and answer 409 after a second conflict.

**IAM.** The inline policies come to dev 9,043 and production 9,421 characters, against a 9,700
working cap.

## 3. The PRs

Milestones are "Prompt as Asset" for the prompts stack and "Model Evals" for everything else.
"Parent" is the PR's GitHub base. A PR's diff shows only the commits that aren't on its base branch:
its own, plus those of any other PR merged into its branch (section 4). The parent's own commits are
never in it.

| Plan item | PR | Branch | Parent (base) | Milestone | What it does |
|---|---|---|---|---|---|
| PR0 spec | #380 | `claude/jolly-lovelace-1ty0ba` | `dev` | Model Evals | The spec and the PROGRESS checklist |
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
| PR16 [P] | #404 | `claude/ranch-47-culling` | PR15 (carries PR13b) | Model Evals | Culling in proposal mode, `evals cull --apply`, `models restore` |
| PR14 [P] | #405 | `claude/ranch-45-emission` | stacks-meet | Model Evals | The pipeline emits eval events |
| PR11b | #406 | `claude/ranch-31-dashboards` | stacks-meet | Model Evals | `pipeline_health` and `ranch_and_prompts` replace three dashboards |
| PR18 [P] | #407 | `claude/ranch-49-judge` | stacks-meet | Model Evals | The judge and the eval-work consumer |
| PR19 | #408 | `claude/ranch-52-human-review` | PR18 (carries PR14) | Model Evals | Findings review in `approve`; people's verdicts as calibration pairs |
| PR18b | #409 | `claude/ranch-50-offline-runs` | PR18 | Model Evals | Offline scoring runs from the CLI |
| PR18c | #410 | `claude/ranch-51-call-traces` | PR14 | Model Evals | Call traces, private, 14 days |
| PR20 | #411 | `claude/ranch-53-mcp-tools` | PR19 (carries PR13b, PR15, PR16) | Model Evals | `model_ranch` and `prompt_metrics` tools, the agent's cell rule, the Friday digest |
| PR21 | #415 | `claude/ranch-60-bug-sweep` | PR20 | Model Evals | Whole feature plus `dev`, swept and fixed |
| PR22 | #416 | `claude/ranch-61-docs-sweep` | PR21 | Model Evals | The documentation sweep |

Labels, from spec section 7: **[B]** needs the owner's `infra/bootstrap` apply first; **[P]**
changes production behaviour once released and switched on.

## 4. How the branches fit together

- **Three stacks.** The queue stack (Q), the prompts stack (P) and the evals stack (E) start from
  the spec branch. #393 starts from `dev`.
- **The stacks-meet branch** (`claude/ranch-55-stacks-meet`, 809ce54) has no PR. It is PR13's tip
  with #393, Q2, PR10, PR17, PR4, PR5, PR2 and #401 merged in. It is the base of PR14, PR11b and
  PR18, which need the evals and prompts stacks together.
- **The evals-meet branch** (`claude/ranch-56-evals-meet`, 384ac59) has no PR and is no PR's base.
  - It is the PR18 and PR14 merge, with the culling branch (PR13b, PR15, PR16) and then PR19 merged
    in.
  - PR20 is built on it.
  - Its meet commit 1cda0a6 lost its second parent (it has only 8057937). PR20's branch records it
    again with a merge that changes nothing (d7b4364).
- **PRs that carry other PRs:**
  - PR16 carries PR13b.
  - PR19 carries PR14.
  - PR20 carries PR13b, PR15 and PR16. PR19 is its base, so PR19 is not in its diff.
  - PR21 carries PR18b, PR18c, PR11b and `dev`'s #401, #412 and #414 beyond PR20.

  A diff keeps showing a carried PR's commits until those commits reach the PR's base. In practice
  that means until they reach `dev` and the PR is retargeted.
- **PR21's history**, in order:
  1. It merges the PR branches together and resolves the conflicts (c6aa146).
  2. It merges `dev` (003a563, which brings #401 and #412) and adds the review fix 728670e.
  3. It merges the sweep's parts: B (0aa149b), D, E, F (7752960, carrying A) and G (ff760db,
     carrying C).
  4. It adds part H (26af22e) and a review fix (bec4b9e).
  5. Last, it merges `dev` again for #414 (28ca33f, the tip).

  The sweep's own changes are `git diff 003a563 bec4b9e`. One commit from the PRs above is not in
  it: PR18b's last commit, ad471b5. 728670e makes the same change to `test_eval_score.py`, so
  merging PR18b later is clean.
- **PR22** is PR21 plus the documentation sweep, with PR21's final tip and the spec branch's latest
  merged in.

## 5. Merge order and retargeting

The owner merges. You prepare each PR (retarget, merge `dev` in, keep CI green) and say on it when it
is ready.

**The order:**
1. **#380 and #393 go first.** #393 must reach `dev` before, or with, the first of #389, #390, #391,
   #395 and #400. Without it, their policies push `lambda_exec` over the IAM cap and the apply
   fails.
2. **The three stacks are independent.** Q needs only #380 and #393; P and E need only #380. Within
   each, a PR follows its parent, and a release can bundle several. The exceptions:
   - **#395 (PR12) waits for the bootstrap.** It needs #386 and the owner's "bootstrap applied"
     (spec section 7, PR12's entry), because the deploy role needs the evals log-group right that
     PR2 adds. Until then, say on #395 that it waits for the apply.
   - **#399 (PR17)** waits for #388.
   - **#391 (E0).** Spec section 7 lists PR3 (#384) as a dependency, but #391's branch is built on
     PR1b alone and uses nothing from PR3. Merging #391 after #384 follows the spec, but the code
     doesn't require it.
3. **The stacks-meet PRs (#405, #406, #407)** are based on stacks-meet, which never merges.
   Retarget them to `dev` once everything stacks-meet holds is on `dev`: #380, #393, #383, #389,
   #396, #382, #381, #386, #384, #390, #394, #387, #388, #392, #397 (re-migrated per #412, below),
   #398, #399, #385, #391, #395 and #400. #401 is already there. #402, #403 and #404 are not in
   stacks-meet, so they don't gate it.
4. **After those:**
   - After #405 merges: #410.
   - After #407 merges: #409. Also retarget #408 then, but merge it only once #405 is in.
   - Once #403 merges: retarget #404, but merge it only once #402 is in.
   - Once #408 merges: retarget #411, but merge it only once #402, #403 and #404 are in too.
5. **#415, then #416, last.**

**Merge every PR with "Create a merge commit",** never "Squash and merge" or "Rebase and merge". The
repository allows all three. Only a merge commit keeps the children's and PR21's copies of the
commits identical to `dev`'s.

**Retargeting.** This overrides spec section 6 and PROGRESS's general note, which say
`git rebase --onto origin/dev <old-parent-tip>`.
- **Never rebase or force-push a `claude/ranch-*` branch.** Several of them have merge commits in
  their own range, and a rebase would drop those merges and their conflict resolutions:
  - PR13b and PR15 carry #401;
  - PR16 has 5081da6;
  - PR19 has 8057937;
  - PR20 has 384ac59 and d7b4364;
  - PR21 has c6aa146, 003a563, the part merges and 28ca33f;
  - PR22 has its merges of PR21 and the spec.

  A rebase would also replay other PRs' commits under new shas that PR21, PR22 and both meet
  branches already hold as originals. And it needs a force-push, which strands every child branch
  and checkout.
- **Instead,** retarget with `gh pr edit <n> --base dev` (or the GitHub tools), then:
  1. `git merge origin/dev`;
  2. ruff (merges are where import order, I001, breaks);
  3. the parallel suite;
  4. push.

  `dev` takes PRs as merge commits (a3c7ec2, 3609234, 74cec57), so merging it in brings nothing in
  twice. #415's and #416's descriptions say the same.

**`dev` has moved since most PRs were opened.**
- **#412** made Re-Write edit the article instead of writing a new one. It touches `rewrite.py`,
  `compliance.py` and `review_inbox.py`, which several PRs also change. PR21's merge commit 003a563
  and the fix commit after it, 728670e, hold the worked resolutions. When each PR is retargeted:
  - **PR7 (#388):**
    - keep dev's `fresh_data`, `_FRESH_DATA_LABEL` and `_source_text`;
    - render `get_prompt("reviews/compliance", {"source": _defang(source), ...})`;
    - have `_review_verdict` read the source before it is defanged;
    - the largest-render test passes `fresh_data`, and the registry's compliance comments name it
      (as in 728670e).
  - **PR9 (#397):** as for PR7, plus:
    - re-migrate `rewrite/rewrite` v1 to #412's text, taking `_REWRITE`, the sha
      `d239dc3a1106cda6d065bda37cb07da7ae8ec59fe58185706b77635e851aed6d` and `v1.txt` from PR21's
      branch;
    - `_REPLY_RULE` becomes #412's edits contract;
    - in `test_rewrite_reviewer_prompts.py`, re-pin `V1_SHA256` and mock an edits reply.

    PR10 (#398) then takes the new PR9.
  - **PR14 (#405):** `test_rewrite.py`'s `_run` takes both `compliance_error` and #412's `mocks`.
  - **PR18c (#410):** the second Re-Write in `test_call_traces.py` quotes a passage the first one
    left (`SECOND_EDIT`).
  - **PR19 (#408):** `review_inbox.py` keeps both `FindingsSource` and #412's `_is_problem_note`.
  - **PR18b (#409):** already carries the run-id pin in `test_eval_score.py` (ad471b5).
- **#417 is coming** (open at 15:20 UTC, from another session, base `dev`): "Split rewrite into Fix
  (edits) and Re-Write (new article) modes". It rewrites `common/rewrite.py` (230 lines added, 114
  removed) and changes `common/reviewer.py`, `scripts/review_inbox.py`, `admin_api_handler.py`,
  `scripts/admin_cli.py` and `cli_reference.json`. If it reaches `dev` before PR9, the
  `rewrite/rewrite` migration must follow it again, as it did for #412. That means re-checking the
  following against #417's code, not #412's:
  - the PR9 and PR21 v1 text and sha, possibly with a second prompt for the new mode;
  - PR21 part B's reviewer-bear blocks in `reviewer.py`;
  - PR14's rewrite-route events;
  - PR19's `review_inbox.py`.
- **#414** makes CI run `pytest lambdas/ scripts/ -n auto --dist loadfile` and pins pytest-xdist
  3.8.0 and execnet 2.1.2. PR21 merged it, and the whole suite passes in parallel there.
- **#401** is on `dev`, and the branches built after 2026-10-31 carry it. The 20 older ones don't
  (section 6).

**The stacks conflict with each other, and siblings within a stack conflict too.** Q, P and E were
built in parallel from the spec branch. Q0, P0 and PR1b all retarget to `dev` once #380 merges, so
whichever lands first makes the others conflict; Q0 against P0 already conflicts in 8 files. From
then on, most PRs retargeted after another stack has landed will conflict when `dev` is merged in.
- **The files the stacks share:**
  - both roots' `main.tf` and `variables.tf`, and both deploy workflows;
  - `scripts/setup_repo.py` and its test, and `docs/configuration.md`;
  - `common/evidence.py` and its test;
  - `scripts/admin_cli.py`, `admin_api_handler.py`, `model_catalog.py`, `vision_triage.py` and
    `daily_cycle_handler.py`;
  - `ops_mcp/architecture.py` and its test, and `ops_mcp/cli_guide.py` and its test;
  - `test_terraform_wiring.py`, `test_no_raw_exception_prints.py` and `cli_reference.json`.
- **Sibling conflicts.** PR2, PR4 and PR5 sit beside PR6 to PR10:
  - PR4 and PR5 conflict with PR10 in `admin_api_handler.py`;
  - PR2 conflicts with PR5 in `configuration.md`;
  - #402 and #403 conflict in `evals_rollup.py`, its test and the raw-exception guard list.
- **Some clean merges are still wrong.**
  - Three tests assumed the other stack was absent, and cee404e fixes them: the pipeline function
    list and the folded `BackupTables` statement in `test_terraform_wiring.py`, and Q1's
    `due_checked_at` keyword in `test_research_prompts.py`.
  - PR4 and PR13b both add `dynamo.replace_config_field`, with different signatures. Their merge
    leaves two definitions, which ruff reports as F811. Keep PR4's
    `(config_id, field, expected, value)` and change `evals_adjust`'s call to match (1cda0a6).
- **The worked resolutions:**
  - the stacks-meet merges f234479, 1599453, 63bbad0, 5c945cd, 7586e80, 112eb87, 1380ab3 and
    809ce54, plus the reconcile commit cee404e;
  - 1cda0a6, for PR13b, PR15 and PR16 against the P and Q stacks;
  - PR16's 5081da6, for #402 against #403: keep `good_and_bad` beside `model_windows` and
    `index_calibrated`, and keep PR15's form of `update_models`, which reads adjustments through
    `ADJUST_MODEL` (spec 7.1).
- **How to resolve:**
  - in `test_ops_mcp_architecture.py`, keep both sides (spec 7.1);
  - regenerate `cli_reference.json` rather than resolving it by hand (section 14);
  - check each merge with ruff and the parallel suite, not only for conflict markers.

## 6. CI today

The snapshot was taken at about 12:30 UTC on 2026-11-01:
- **Mergeability:** every open PR's state is `clean`, except #415, which is `unstable` because of
  the one red check below.
- **Reviews:** no PR has a review or an unresolved review thread.
- **Comments:** the only PR comment is the building session's explanation on #415.

| PR | Checks |
|---|---|
| #380 | 6 success. Its diff is docs only, so the Python and prompts workflows don't run |
| #381 to #393, #395 to #406, #408, #410 | 8 success each |
| #394, #407, #409, #411 | 9 success. The prompts workflow's `publish-dev` and `publish-production` are skipped, as on every pull request |
| #415 | 8 success, 2 skipped, 1 failure: `gates` (the prompts workflow), red on purpose, see below |
| #416 | 9 success, 2 skipped; `gates` passes. Its last commit (64b82a0, spec text only) was pushed after the snapshot |

**When the publish jobs run.** `publish-dev` runs only on a push to `dev` or a dispatch from `dev`.
`publish-production` runs only on a published Release or a dispatch from `dev`. Both run only with
`PROMPT_AS_ASSET_ENABLED` on.

**These greens are stale for 20 PRs:** #393, #383, #389, #396, #382, #381, #386, #384, #390, #394,
#387, #388, #392, #397, #398, #385, #391, #395, #400 and #399. Their branches don't carry #401, and
their last CI ran between 2026-10-25 and 2026-10-27, before `test_rail_access_adapter.py`'s fixed
date expired at 2026-10-31T03:00Z.
- **A re-run fails on all 20.** It tests the same merge commit as the original run.
- **A new push fails on the 19 stacked PRs.** CI tests the head merged into the parent, and the
  parent lacks #401 too. They stay red until a push brings `dev` in, merged into the branch or with
  the PR retargeted to `dev` (section 5).
- **A new push to #393 passes.** Its base is `dev`, so GitHub merges the push with today's `dev`,
  which has #401.

Expect this red when you push a fix forward (section 13). It is not a regression.

**Why #415's `gates` check is red.** The prompt lint holds each prompt version to the bytes it had on
the PR's base, which for #415 is PR20's branch. That branch still has PR9's `rewrite/rewrite` v1,
written before #412. #415 merged `dev`, and #412's code needs #412's prompt text, so v1's bytes
changed.

Section 7.1 ("PR21 decided (#412 on dev)") keeps the name v1 rather than adding a v2, because no v1
was ever published: PR9 isn't merged, and production's registry (v0.3.8) doesn't have the prompt.
The check turns green once PR9 is re-migrated (section 5) and #415 is retargeted to `dev`. A v2
would turn it green now, but the next `dev` merge would have to undo it. The building session
explained this in a comment on #415 and offered the owner the v2 if they want it.

## 7. The bug sweep (PR21, #415)

#415's description is the record. Each part went through implement, three review lenses, three
skeptics per finding, and a fix stage.

- **Parts A to C: the carry-forwards in section 7.1.**
  - **The Q2 shadow counters.** The cutover needs `shadow_clean_days >= 1`.
  - **Restore and resume.** They don't start a second daily run, and the cutover deletes the
    schedules once.
  - **The reviewer bear's prompts** move to v2, with defanged `article_text` and `review_flags`
    blocks.
  - **Prompt Fallback events** are emitted.
  - **`POST /evals/adjust`** checks the prompt version.
  - **Pair rows** carry `rewrite_number`.
  - **The judge's spend** is held on the day's `spend` row (`held_aud`).
  - **The weekly reflection** caps the comments it lists (`COMMENTS_MAX_CHARS`, 48,000) and says how
    many it left out, so one busy topic can no longer fail the run for every topic (e4cc78f).
  - **Stagger** wraps a range across midnight (`22-2` two hours on is `0-4`, not `24-4`) and refuses
    it on schedules for named days (78d060b).
  - **The judge's item** still counts as finished when something fails after the run header is read,
    so the run closes and checks the tripwire (0c9843b).
- **Parts D to G: 14 findings from a whole-feature sweep.** Eight finders went over the feature by
  area. Each finding was reproduced by a failing test and checked by a skeptic.
  - **Queue:**
    - S0: a move to the queue is refused until `topics reschedule` has written the current
      schedules.
    - S2: a failed fetch is not a shadow mismatch (`last_research_attempt_at`).
    - S6: an infrastructure refusal of a daily start raises after the whole tick.
    - S7: a released claim gives back `daily_claim_n`.
  - **Ranch:**
    - S3: inference profiles are classified.
    - S11: `models add` writes conditionally, with a 409 on a second conflict.
    - S12: the cull refusals name `model-catalog set --cycle on`.
  - **Assistant:** S5: the cull card counts only what an apply would take.
  - **Evals:** S10: the digest's "gated" follows the rotation guard.
  - **Prompts:**
    - S8: the last good settings read is kept, and a cold container fails closed.
    - S9: the fallback filters cover eval-jobs' logs.
  - **Judge:**
    - S1: the author check compares base model ids.
    - S4: a run over the whole daily cap gets a 409.
    - S13: pre-claim failures close the message, and eval-work redelivers through
      `batchItemFailures`.
- **Part H: five small fixes.**
  - evidence tokens are matched whole;
  - on-demand-scan runs the bootstrap's plan test;
  - the bootstrap's `domain_name` description is corrected;
  - `FALLBACK_DAYS` is 3;
  - a scoring result whose bookkeeping failed twice is delivered again.

**Checks on the branch:**
- **Tests:** the full suite passed serially on bec4b9e and in parallel on 28ca33f: 10,335 passed,
  6 skipped, 1 xfailed.
- **Lint and the CLI reference:** both pass.
- **Terraform:** `terraform fmt` is clean, `validate` passes offline for both roots, and the module
  tests pass for `evals` and `bootstrap`.
- **Secrets and links:** gitleaks and the link check are clean.

## 8. The docs sweep (PR22, #416)

#416's description is the record.
- **Two new architecture pages:** `docs/architecture/queue-platform.md` and
  `docs/architecture/prompts-and-evals.md`.
- **The operating docs:** configuration, the runsheets and observability.
- **The using docs:** models, the evaluation runsheet, CI and `scripts/README.md`.
- **The record:**
  - every PROGRESS box is ticked;
  - the spec and the old dispatcher design move to `docs/enhancements/archive/`;
  - links are updated across the repository.
- **Code changes:**
  - two `FEATURES` walk-throughs (`queue-platform`, `prompts-and-evals`) in
    `lambdas/ops_mcp/architecture.py`;
  - the code-only prompt `ops_agent/system` goes from v2 to v3 to route to them. This is the PR's
    only behaviour change;
  - the wrong `prompts pause` and `prompts set` help text is corrected, and the CLI reference
    regenerated.

It was written against PR21's code, and five agents then reconciled it with PR21's last fix parts.
The full suite passes in parallel there: 10,359 passed, 6 skipped, 1 xfailed. Its archived spec
was corrected on the four points of section 9 in 64b82a0.

**This page postdates #416.** When #416 is next updated, merge this page's branch into it, move this
file to `docs/enhancements/archive/` beside the spec (its relative links then still resolve), and add
an index row there.

## 9. Decisions, and where the code differs from this branch's spec

- **Every decision taken while building is a line in spec section 7.1:**
  - "PR21 decided (...)" lines;
  - per-PR "(from ...)" carry-forwards;
  - "PR22 (from ...)" documentation obligations.

  Don't reopen what the spec calls decided. Section 3 of the spec lists the deviations from the
  owner's original spec, each with its reason.
- **Four points where the code is right and this branch's 7.1 is wrong.** They are not decisions to
  defend. PR22's archived copy of the spec (on #416, 64b82a0) and the docs on #416 follow the code
  on all four:
  1. **A research mismatch** doesn't clear the recorded research turn. It is dated in
     `shadow_research_mismatch_at`, and the cutover pre-flight waits for a later
     `shadow_research_matched_at`. `topics schedule` shows neither date.
  2. **A restore** keeps the newest of three claims: the snapshot's, the live row's in the same zone,
     and today's once its turn has passed (`backup._newest_claim`). A resume writes today's.
  3. **The cutover pre-flight** needs one clean day (`shadow_clean_days >= 1`). "A few days clean in
     a row" is advice.
  4. **A pause or the kill switch** lands within 5 minutes, the TTL of the cached settings row
     (`CACHE_TTL_S`). The spec's 600 s is a superseded version's worst case (the cache plus
     `RETIRE_GRACE_S`).
- **Added since:** S4's 409 tells the operator to raise the daily spend cap or score cheaper models
  (`common/eval_score.py`).

## 10. Open questions for the owner

These are unanswered.
- Questions 1 to 5 are on #415, under "Questions for the owner".
- Questions 1 to 6 are in 7.1's "Owner (from PR21)" line. #415 lists question 6 under "Known and
  left", not as a question.
- Question 7 came from the owner in the building session's conversation and is written down nowhere
  else.
- Question 8 is on #416's "For the owner" list, and 7.1 has its own line for it ("After PR11b's dev
  apply").

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
5. **`ValidationException`.** Should it leave `dispatch._TOPICS_OWN`? StartExecution can raise it for
   the shared state machine ARN, and today that only skips topics, with no alarm.
6. **The vision worker's own evidence patterns.** `vision_worker_handler.py` keeps an anchored copy,
   because it can't import `evidence`. Should it match whole values too?
7. **Evals as a CI gate (not yet specced).** The owner asked how to run evals as a separate job in
   the prompts workflow, beside lint and security. The building session answered with a design and
   offered to spec it as a new PR, and had no reply yet.
8. **Dashboard billing.** After PR11b's dev apply, check whether pipeline-health counts as a free
   dashboard. The docs name the Cost Explorer usage types to look at.

## 11. Known issues left on purpose

All are low impact, and each is recorded on #415 or #416.
- **The restore race.** The dispatcher can claim today between a restore's read and its batch put.
  Closing that needs a conditional write, which a batch put can't do.
- **Two restore cases.** Both fall back to the snapshot:
  - a queue snapshot restored over a schedules row whose Scheduler ran today;
  - a topic whose time zone changed since the snapshot.
- **The fallback alarm while the kill switch is on.** It is in ALARM in every hour that has an
  affected render, and back to OK (with an email) in an hour that has none. Only asset prompts write
  fallback lines.
- **The heartbeat alarm's description.** The work-queue heartbeat alarm says nothing is being
  dispatched. That isn't true for a tick that raises after a refused start: research still goes out.
  The Terraform text is left for a code PR.
- **A judge run whose header read keeps failing** may never close.
- **A non-ClientError inside `_start_daily`** ends the tick at that topic.

## 12. What the owner has to do

- **Review and merge** in the order of section 5, always with "Create a merge commit".
- **The bootstrap** (around #386):
  - **Apply `infra/bootstrap` by hand in each account whose bootstrap predates this work.** That is
    dev's account, and production's too if it is separate.
  - **Do it before the first deploy that carries PR12 (#395) there:** before #395 merges to `dev`,
    and before the production Release that brings it. From PR12 on, every deploy creates the
    `<prefix>-<env>-evals` log group, whether evals are on or off, and old deploy roles stop on
    `logs:CreateLogGroup`. The apply also makes the two prompts publisher roles.
  - **Create the `production-prompts` GitHub environment.** The owner is its required reviewer, and
    its deployment branch is `dev`.
  - **Set `AWS_DEV_PROMPTS_ROLE_ARN` and `AWS_PROD_PROMPTS_ROLE_ARN`** at repository level from the
    bootstrap's outputs. With two accounts, take each ARN from its own account's outputs.
  - **Tell the session that holds the PRs** (after the hand-over, the reviewing session) "bootstrap
    applied", so it can mark #395 ready.
- **Turn the parts on** in the order of spec section 8, which PR22's production runsheet also gives:
  1. **The queue.** Move topics to the queue one at a time, after shadow mode is clean.
  2. **Prompt publishing.** Only after the bootstrap apply and the two `AWS_*_PROMPTS_ROLE_ARN`
     secrets, because the first publish needs the publisher roles:
     1. set the `production` environment's own `PROMPT_AS_ASSET_ENABLED` to `false`, so a Release
        keeps production's Lambdas on their code copies;
     2. set `PROMPT_AS_ASSET_ENABLED=true` as a **repository** variable. Dev's deploy and both
        publish jobs read only the repository value, so a value set on an environment won't turn
        publishing on;
     3. deploy dev and publish dev's prompts.
  3. **Evals.** Turn `EVALS_ENABLED` on in an environment only once #415 (PR21) is deployed there.
     That also means after PR13 (#400) is deployed. There are two reasons:
     - PR12 creates the `eval-rollup-heartbeat` alarm, which treats missing data as breaching, and
       only PR13 writes the `rollup_done` line it counts.
     - Before #415's S3 fix, the hourly rollup's classify marks ARN-listed models `unknown`.

     As with step 2, set it to `false` on the `production` environment first, or the next Release
     turns evals on in production too.
  4. **Production,** after a Release.
- **Run `admin_cli models classify`** once per environment after #415 is deployed there, not after
  PR1b's first deploy.
  - Before #415's S3 fix, classify marks a model registered by an application-inference-profile ARN
    (or a custom, provisioned or imported model's ARN) as `unknown`.
  - That makes the model unsuitable everywhere: a topic using it is moved off it for good, a
    fallback naming it is dropped, and the model-config and pool validators refuse it.
  - Until a row is classified, the old rule decides (a token-priced row counts as a text model), so
    waiting changes nothing.
- **Answer section 10.**

## 13. How to review it

1. **Read the spec,** sections 2 to 6 and 7.1, then #415's and #416's descriptions.
2. **Review the whole feature as it will land:** `git diff origin/dev...origin/claude/ranch-60-bug-sweep`.
   For per-PR review, each PR's diff against its parent is smaller. For PR16, PR19, PR20 and PR21 it
   also includes the PRs merged into them (section 4).
3. **The areas most worth an independent look:**
   - **IAM:** `lambda_exec`'s inline policies and the 10,240 cap (#393); the publisher roles' trust
     (`sub` strings) in `infra/bootstrap/main.tf`; the deploy role's evals log-group right.
   - **Double runs:**
     - the dispatcher's claims (`common/dispatch.py`);
     - the cutover and its way back (`admin_api_handler._update_topic`, `_move_to_queue`);
     - the restore and resume claims (`common/backup.py`, `_set_topic_paused`).
   - **Prompts:** the hash check, pause and kill switch (`common/prompts.py`); the publisher's release
     order (`scripts/publish_prompts.py`); no secrets in `prompts.yml` jobs a pull request can
     trigger.
   - **Evals:**
     - judge ≠ author (`common/eval_judge.py`);
     - the spend cap at enqueue and at consume;
     - offline runs never touching prompt assets (a test scans for it);
     - culling archives a model before deleting it.
   - **Personal data and untrusted text:** events carry ids and codes only, never free text; traces
     hash untrusted values; voice callers hear no untrusted text.
4. **Run the checks** (section 14):
   - ruff and the parallel suite;
   - `generate_cli_reference.py --check`;
   - `terraform fmt -check -recursive infra/`;
   - `terraform test` in `infra/bootstrap` and each module with tests;
   - `terraform validate` in both roots;
   - gitleaks over the branch's commits;
   - the link check.
5. **Report what you find** on the PR it belongs to, or to the owner.

**A fix goes on the PR it belongs to,** then gets merged forward along the real ancestry, not only
the GitHub bases:
- **PR13, or anything under stacks-meet** (#393, Q, P0 to PR10, PR17, PR1b, E0, PR12): merge into
  its GitHub children, and also into `claude/ranch-55-stacks-meet`. That branch has no PR, but it is
  the base of PR14, PR11b and PR18. Then merge onward from those three.
- **PR14:** merge into PR18c and PR19 (PR19 contains PR14).
- **PR18:** merge into PR18b and PR19.
- **PR13b or PR15:** merge into PR16.
- **PR13b, PR15, PR16 or PR19:** merge straight into PR20's branch, as d7b4364 did. Don't route
  through `claude/ranch-56-evals-meet`: it has no PR and is no PR's base, and since its meet commit
  lost a parent, merging those branches into it conflicts.
- **Finally:** merge into PR21, then PR22.

Use `git merge-base --is-ancestor` to check that each branch carries the fix.

**Keep the docs freeze** (spec section 2). Docs change only in #380 and #416. The exceptions are a
`configuration.md` row for a new GitHub variable or secret, and `prompts/README.md`. If a fix
changes behaviour, update #416's docs to match and add a "PR22 (from …)" line to spec 7.1.

## 14. Setting up a fresh cloud session

- **Python 3.11:**
  `python3.11 -m venv .venv && . .venv/bin/activate && pip install -r lambdas/requirements-dev.txt -r scripts/requirements.txt`.
  On a branch without #414, also run `pip install pytest-xdist==3.8.0 execnet==2.1.2`. Never commit
  `.venv`.
- **ruff:** the SessionStart hook (`.claude/hooks/session-start.sh`) puts ruff 0.6.9 first on PATH,
  and `ruff --version` must say 0.6.9.
- **CLI reference:** whenever a parser changes, regenerate `lambdas/ops_mcp/cli_reference.json` on
  the Python release named in its `help_python`. That is 3.13 today (spec section 6).
  - **You need a 3.13 environment with the CLI's dependencies.** The `python3.13` first on PATH is
    the ruff hook's venv, which has neither requests nor botocore. Build one outside the repository
  (`.gitignore` covers only `.venv/` and `venv/`):

    ```sh
    /usr/bin/python3.13 -m venv ~/venv313
    ~/venv313/bin/pip install -r scripts/requirements.txt
    ~/venv313/bin/python scripts/generate_cli_reference.py
    ```
  - **Don't regenerate from the 3.11 `.venv`.** That changes `help_python` to 3.11 and rewrites the
    stored help of every command argparse lays out differently, which churns the file across the
    stacked branches.
  - `--check` passes on either release, and CI runs it on 3.11.
  - Resolve a conflict in the file by regenerating it on 3.13, never by hand.
- **Terraform offline.** The registry is blocked in cloud sessions.
  - Download Terraform 1.16.2 from `releases.hashicorp.com`, and the providers into a filesystem
    mirror: aws 6.64.0, archive 2.8.1 and random 3.9.1.
  - Point `TF_CLI_CONFIG_FILE` at a `.terraformrc` with
    `provider_installation { filesystem_mirror { path = "<mirror>" include = ["registry.terraform.io/hashicorp/*"] } direct { exclude = ["registry.terraform.io/hashicorp/*"] } }`.
  - **Module tests:**
    `TF_DATA_DIR=<scratch> terraform -chdir=infra/modules/<m> init -backend=false && terraform test`,
    then delete the `.terraform.lock.hcl` it writes.
  - **Roots:** `init -backend=false && validate`. Back up the committed lock files first, and restore
    them after.
  - **Never add a `plugin_cache_dir`.** Its providers fail the committed lock files' checksums, and
    every root `validate` breaks.
- **gitleaks:** download the release binary, then run
  `gitleaks git --log-opts="<base>..HEAD" --redact --no-banner --config .gitleaks.toml .`. Also run
  `python3 scripts/pii_denylist_check.py --staged`.
- **Link check:** the repository has no link checker for its markdown. The building session used the
  one in the [appendix](#appendix-the-link-checker).
- **Merges with conflicts:**
  `git -c merge.conflictStyle=diff3 merge --no-ff -X diff-algorithm=histogram <branch>`. diff3 shows
  the base side, and histogram aligns functions better than the default diff.
- **Container limits.**
  - The disk allowance is fixed, and it filled once. Delete Terraform data directories and spare
    worktrees as soon as you are done with them.
  - The container restarted twice mid-work. Commit as soon as the suites you touched pass, and push
    finished branches (a push to `claude/ranch-*` starts no workflow).
  - The full suite took 11 to 19 minutes serially on 4 CPUs, and about 5 in parallel. Run it in the
    background either way, and read its log.

## 15. What did not survive the building session

- **Workflow scripts.** Each PR went through a Workflow script: implement, three review lenses,
  three skeptics per finding, then a fix. The bug sweep and docs sweep had the same shape. The
  scripts lived in that session's own directories, not in the repository; their results are in each
  PR's description and commits.
- **The bug sweep's part branches** (`claude/ranch-60-sweep-a` to `-g`) were local only and never
  pushed. Their commits are in `claude/ranch-60-bug-sweep` through its merge commits.
- **The scratchpad** held worktrees, logs, the Terraform mirror and the gitleaks binary. It goes when
  the container is reclaimed.

## Appendix: the link checker

Run it with `python3 linkcheck.py <repo root>`. It checks every relative markdown link, and its
`#anchor`, in the repository's `.md` files outside `.git` and `node_modules`. It prints one line per
problem, then the count on stderr.

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
