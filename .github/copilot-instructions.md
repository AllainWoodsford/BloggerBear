# Copilot Instructions — BloggerBear

Read `/docs/project-plan.md` before making non-trivial changes. Check
`/docs/PROGRESS.md` for current phase status before starting work.

## Non-negotiable rules
1. No PII in stored data or public forms.
2. Research tick is **diff-first**; do not call Bedrock without material change.
3. No publish without compliance review.
4. Financial topics require stricter language constraints and manual moderation.
5. Add new domains through adapters, not core branching logic.
6. Terraform apply is never manual/ad hoc: `dev` auto-applies on merge to
   `dev`; production only applies when a GitHub Release is published from
   `prod`, gated by the `production` environment's required review. The
   one-time `infra/bootstrap` apply is the sole exception, and it stays
   local/manual, never wired into CI.
7. Keep security workflow strict; do not weaken blocking checks.

## Branch & PR rules
1. Never push directly to `dev` or `prod`. Always work on a task branch off
   `dev`, open a PR, and wait for it to be reviewed and merged.
2. Promotion to `prod` happens only via a reviewed PR from `dev` — never a
   direct commit to `prod`.
3. Never modify branch protection rules, GitHub Environment protection rules,
   or the release/tagging setup as part of a normal code PR. If a task
   seems to need one of those changed, say so in the PR description instead
   of changing it.
4. A production deploy is triggered only by publishing a GitHub Release from
   `prod` — never by a workflow change that makes merges to `prod` deploy
   directly.

## Build/validation defaults
- Terraform: `terraform fmt -check`, `terraform validate`, `terraform plan`
- Python: `ruff check .`, `pytest`
- Security: `trivy config infra/`, `trivy fs --scanners vuln,secret lambdas/`, `bandit -r lambdas/ -ll`

## Scope control
Use `docs/project-plan.md` §10 and `docs/PROGRESS.md` to see the active
phase, and only implement that phase. Check off completed items in
`docs/PROGRESS.md` in the same PR that completes them.
