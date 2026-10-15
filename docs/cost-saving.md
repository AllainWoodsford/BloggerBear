# Cost saving: the firewall switches

**Status:** built · **Date:** 2026-10-15 · **Default:** every switch on, which is the firewall
exactly as it was before the switches existed.

The firewall (AWS WAF) is the biggest line on the bill, and almost all of it is fixed monthly
charges, not traffic: at list price about US$5 for each web ACL and US$1 for each rule, whether
one request or a million passes through. A full deployment of this project has five web ACLs
and fourteen rules, about US$39 a month:

| Web ACL | Where | Rules | US$ a month |
|---|---|---|---|
| `<prefix>-shared` (CloudFront) | production: the site and the public API's CDN | 2 | 7 |
| production public API (regional) | `production/main.tf`, `aws_wafv2_web_acl.public_api` | 5 | 10 |
| production admin API (regional) | `aws_wafv2_web_acl.admin` and its IP set | 1 | 6 |
| dev public API (regional) | `dev/main.tf` | 5 | 10 |
| dev admin API (regional) | `dev/main.tf` | 1 | 6 |

Each switch below turns one of those pieces off. They exist for a deployment that only tries the
project out, or a dev environment that nobody but its operator ever calls.

> **At your own risk.** The switches remove protection, and they are documented here so the
> trade is a conscious one. They are fine for testing the project. A fork that stores real
> people's data (reader feedback, comments, anything the privacy policy covers) should keep every
> switch on: the per-visitor rate limits, the AWS managed rule set and the firewall's logs are
> what stand between a scripted abuse of the public API and your bill, and what the security
> incidents and the operator's assistant's firewall review are built on.

## How a switch is set

Each switch is a GitHub Actions **variable** (not a secret: a switch is not a secret, and a
variable prints in plain text, which is fine for `true` or `false`). The deploy workflows pass it
to Terraform, reading three names in order:

1. the environment-specific one, `<SWITCH>_DEV` for dev or `<SWITCH>_PROD` for production;
2. else the shared one, `<SWITCH>`, which applies to both;
3. else `true`.

Set it under **Settings → Secrets and variables → Actions → Variables**, then **re-run the
deploy**: a push to `dev` (or the `terraform` workflow by hand) for dev, a GitHub Release from
`prod` for production. Nothing changes until that apply runs. Production's names may also be set
on the `production` environment, which wins over the repository's, as any environment variable
does.

The Terraform variables are `bool`, so a typo (`ture`, `yes`) fails the plan with "a bool is
required" instead of quietly picking a mode. Use `true` or `false`.

To undo any switch, delete the variable (or set it back to `true`) and re-run the deploy. The
resources come back exactly as they were, under the same names.

| GitHub variable (`_DEV` / `_PROD` first, then the shared name) | Terraform variable | `false` means | Saves (US$ a month) |
|---|---|---|---|
| `WAF_REGIONAL_ENABLED` | `waf_regional_enabled` | no regional web ACLs for that environment: neither the public API's nor the admin API's | about 16 per environment |
| `WAF_ADMIN_ALLOWLIST` | `waf_admin_allowlist` | the admin API's allowlist by its resource policy alone: no admin web ACL | about 6 per environment |
| `WAF_SITE_ENABLED` (production only) | `waf_site_enabled` | no shared CloudFront ACL on the site and the public API's CDN | about 7 |
| `WAF_LOG_ALL_REQUESTS` | `waf_log_all_requests` | the admin ACL's log keeps only what a rule blocked or counted | CloudWatch Logs ingestion (the Hosting line) |

The first apply after this page's pull request destroys or creates **no firewall resource** with
every switch on: every one that gained a `count` has a `moved` block to its new index. What that
apply does add is the admin API's resource policy (below), the admin API's four new routes (pause,
resume, the schedule listing and the rewrite of every schedule) and one redeployment of each
API's stage, which the policy's hash in the deployment's triggers brings about. The plan for a
switch turned off lists exactly the resources it removes; read it before approving.

## The admin API's allowlist is always on

Whatever the switches say, the admin API only answers the addresses in `ADMIN_ALLOWED_CIDRS_DEV`
and `ADMIN_ALLOWED_CIDRS_PROD`. Before the switches, that was the admin web ACL's job alone. Now
it is the API's own **resource policy** first (`infra/modules/rest-api`, `resource_policy`): a
Deny of every `execute-api:Invoke` whose address is not in the list, which API Gateway enforces
before anything is run and which costs nothing. With the list empty (the default until the
secret is set), it is a Deny of every call: the same fail-closed behaviour as the admin web ACL's
default block. SigV4 (`AWS_IAM`) authorization is unchanged: a Deny-only policy never admits
anyone on its own, and a caller in the same account from an allowed address is admitted by its
own IAM policy, as before.

With `WAF_ADMIN_ALLOWLIST` on (the default), the admin web ACL is a **second** layer over the
same list, so the default is more hardened than it was, not less. With it off, a refused caller
gets API Gateway's 403 and the attempt shows in the admin API's **access log**, which the
security-events Lambda already reads: a burst of admin 403s opens an incident whether or not a
firewall saw them (`security_events_handler.counts_as_admin_error`).

The addresses are no more exposed than they were:

- the policy is built from the same `TF_VAR_admin_allowed_cidrs` the IP set is built from, which
  is `sensitive = true`, so the plan prints it as `(sensitive value)`; the root writes its
  resource as API Gateway's shorthand `execute-api:/*` and the module writes it out as the API's
  full ARN, which API Gateway would otherwise do by itself and Terraform would then read back as
  a change on every plan;
- it lives in the account and in the Terraform state, where the IP set already lives;
- no Terraform output carries it;
- a refused caller's 403 says only that the call was denied.

## The switches, one by one

### `WAF_REGIONAL_ENABLED`

**Off:** neither of the environment's regional web ACLs is created: not the public API's (its
general rate limit, the feedback route's rate limit, the limits for requests through the CDN,
and the AWS managed common rule set) and not the admin API's. Their log groups, logging
configurations, the log resource policy, the security-events subscriptions on those logs and
their rows on the edge dashboard go with them. The admin API keeps its resource-policy
allowlist. Both APIs keep API Gateway's own throttling (a ceiling for the whole API, not per
visitor).

**Lost:** per-visitor rate limits on the public API, above all on the feedback route, which can
cost a model call; the managed rule set; the firewall's logs, so no WAF-block incidents and no
`firewall_review` for the operator's assistant. In production, also the blocks the shared
CloudFront ACL does not see, because it sits in front of the CDN and not in front of the
execute-api address that RSS readers and scripts can still call directly.

**Saves:** about US$16 a month per environment.

**Set:** `WAF_REGIONAL_ENABLED_DEV` or `WAF_REGIONAL_ENABLED_PROD` (or the shared
`WAF_REGIONAL_ENABLED`) to `false`, then re-run the deploy. **Undo:** delete it, re-run.

### `WAF_ADMIN_ALLOWLIST`

**Off:** the admin web ACL, its IP set and its log group are not created; the allowlist is
enforced by the resource policy alone. Ignored when `WAF_REGIONAL_ENABLED` is off, which removes
the admin ACL anyway.

**Lost:** the second layer in front of the admin API, and the full WAF record of every admin
call. Refused attempts still show in the admin API's access log.

**Saves:** about US$6 a month per environment.

**Set:** `WAF_ADMIN_ALLOWLIST_DEV`, `WAF_ADMIN_ALLOWLIST_PROD` or `WAF_ADMIN_ALLOWLIST` to
`false`, then re-run the deploy. **Undo:** delete it, re-run.

### `WAF_SITE_ENABLED` (production only)

**Off:** the shared CloudFront web ACL (`<prefix>-shared`: a per-visitor rate limit and the AWS
managed common rule set at the edge, in front of the cache), its us-east-1 log group, logging
configuration and policy are not created, and neither the site nor the public API's CDN has a
web ACL. Production's `wafv2_web_acl_arn` output is then `null`, and dev's `web_acl_arn` (the
hand-set sharing of that ACL) must stay empty.

**Lost:** the only firewall in front of the site itself, and the edge rate limit on the public
API's CDN. CloudFront's own protections and AWS Shield Standard remain. The public API's
regional ACL, if on, still guards the origin.

**Saves:** about US$7 a month.

**Set:** `WAF_SITE_ENABLED_PROD` or `WAF_SITE_ENABLED` to `false`, then publish a release.
**Undo:** delete it, release again. Dev never owns this ACL, so there is no `_DEV` name.

### `WAF_LOG_ALL_REQUESTS`

**Off:** the admin web ACL's logging keeps only requests a rule blocked or counted, as the two
visitor ACLs' logs already do (they never logged allowed requests: privacy policy, section 5).
Nothing else changes.

**Lost:** the full record of every admin call, which only the operator's allowlisted addresses
make. That record is an audit trail; a deployment with one operator may not need it.

**Saves:** CloudWatch Logs ingestion, on the Hosting line of the bill. Small, unless the admin
API is busy.

**Set:** `WAF_LOG_ALL_REQUESTS_DEV`, `WAF_LOG_ALL_REQUESTS_PROD` or `WAF_LOG_ALL_REQUESTS` to
`false`, then re-run the deploy. **Undo:** delete it, re-run.

## What a switch never changes

- The admin API's address allowlist and SigV4 authorization.
- API Gateway's throttling on both APIs, and the lower limit on the feedback route.
- The application's own feedback limits, screening and verification (`common/feedback_limits.py`).
- The privacy posture: the visitor ACLs never logged allowed requests, and a switch cannot make
  any ACL log more.
- Anything outside the firewall: the pipeline, the tables, the assistant, the site.

## Where the code is

- The switches: `infra/environments/{dev,production}/variables.tf` (`waf_regional_enabled`,
  `waf_admin_allowlist`, `waf_log_all_requests`; `waf_site_enabled` in production).
- What they turn off: the `count` on each firewall resource in both `main.tf` files, the
  `moved` blocks beside them, and the conditional lists the dashboards, the security-events
  subscriptions and the assistant's `waf_log_groups` are built from.
- The always-on allowlist: `local.admin_api_resource_policy` in each `main.tf`, attached by
  `infra/modules/rest-api` (`resource_policy`, `resource_policy_enabled`), which also redeploys
  the API when the policy's text changes.
- How CI passes them: `.github/workflows/terraform.yml`, `terraform-production-release.yml` and
  `destroy-dev.yml`.
- The checks: `lambdas/tests/test_terraform_cost_switches.py`.
