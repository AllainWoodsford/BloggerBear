# Copilot Instructions — BloggerBear

Read `/docs/project-plan.md` before making non-trivial changes.

## Non-negotiable rules
1. No PII in stored data or public forms.
2. Research tick is **diff-first**; do not call Bedrock without material change.
3. No publish without compliance review.
4. Financial topics require stricter language constraints and manual moderation.
5. Add new domains through adapters, not core branching logic.
6. Terraform apply is CI-only (except one-time bootstrap).
7. Keep security workflow strict; do not weaken blocking checks.

## Build/validation defaults
- Terraform: `terraform fmt -check`, `terraform validate`, `terraform plan`
- Python: `ruff check .`, `pytest`
- Security: `trivy config infra/`, `trivy fs --scanners vuln,secret lambdas/`, `bandit -r lambdas/ -ll`

## Scope control
Use `docs/project-plan.md` §10 and only implement the active phase.
