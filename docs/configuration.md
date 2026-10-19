# Configuration

Every setting an operator has to think about, and where it goes. The steps that use them are in
[deployment-runsheet.md](deployment-runsheet.md) (dev) and
[production-runsheet.md](production-runsheet.md) (production and the domain).

Settings live in six places:

| Where | What kind of setting | How you set it |
|---|---|---|
| [GitHub secrets and variables](#github-secrets-and-variables) | Which account, region, bucket and name prefix a deploy uses; who may reach the admin API | `python scripts/setup_repo.py`, or Settings → Secrets and variables → Actions |
| [Bootstrap variables](#bootstrap-variables) | The state bucket, the deploy roles, the DNS zone | `-var` on the one-time local `terraform apply` |
| [Environment Terraform variables](#environment-terraform-variables) | The domain, the model, a few switches | `infra/environments/<env>/terraform.tfvars`, by pull request |
| [SSM Parameter Store](#ssm-parameter-store) | API keys for the data sources | `aws ssm put-parameter`, or the console |
| [DynamoDB](#dynamodb-settings-changed-with-no-deploy) | Models, pipeline behaviour, topics | `python scripts/admin_cli.py`, or the console |
| [Your own machine](#your-own-machine) | Where the admin CLI points | Environment variables |

Two more are set in a console and nowhere else: [AWS account settings](#aws-account-settings) and
[GitHub repository settings](#github-repository-settings). The optional vision work spans
several of these, so its settings are gathered in [Vision and rail access](#vision-and-rail-access).

Account IDs here are AWS's documentation placeholders. `<env>` is `dev` or `production`.
`<prefix>` is your name prefix, the [`UNIQUE_NAME_PREFIX`](#github-secrets-and-variables) setting:
every resource name starts with it (`<prefix>-<env>-<resource>`), and unset it is `bloggerbear`.

## GitHub secrets and variables

Under **Settings → Secrets and variables → Actions**. "Repo" means repository level. Dev's
settings must be at repository level, because the dev job runs in no environment. Production's can
be on the `production` environment or on the repository.

| Name | Kind | Where | What it is | Example |
|---|---|---|---|---|
| `AWS_DEV_DEPLOY_ROLE_ARN` | secret | repo | Bootstrap's `dev_deploy_role_arn` output. Required. The role is named `gha-<prefix>-dev-deploy`. | `arn:aws:iam::111111111111:role/gha-bloggerbear-dev-deploy` |
| `AWS_PROD_DEPLOY_ROLE_ARN` | secret | `production` | Bootstrap's `prod_deploy_role_arn` output. Required. The role is named `gha-<prefix>-prod-deploy`. | `arn:aws:iam::123456789012:role/gha-bloggerbear-prod-deploy` |
| `AWS_DEV_ACCOUNT_ID` | secret | repo | The account dev must land in. Optional, recommended. | `111111111111` |
| `AWS_PROD_ACCOUNT_ID` | secret | `production` | The account production must land in. Optional, recommended. | `123456789012` |
| `TF_STATE_BUCKET_DEV` | secret | repo | The state bucket dev uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `TF_STATE_BUCKET_PROD` | secret | `production` | The state bucket production uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `AWS_REGION` | variable | repo | The region everything is deployed to. Optional: unset, it is `ap-southeast-2`. Read [Deploying to another region](deployment-runsheet.md#deploying-to-another-region) before setting it. | `eu-west-1` |
| `TF_STATE_REGION` | variable | repo | The region of the state bucket, only if it is not `AWS_REGION`. Optional, and rarely needed. | `eu-west-1` |
| `VISION_ENABLED` | variable | repo | Set to `true` to deploy the vision worker (OpenCV on arm64 in `vision_region`, for the `satellite_vision` and `rail_access` topics; [the vision docs](enhancements/archive/vision.md)). Read as a switch: `true`, in any letter case (GitHub Actions compares strings ignoring case), turns the worker on; any other value, or unset, is read as off, so nothing of it is created and a deployed worker is removed on the next apply. The bootstrap must have been re-applied with `vision_region` first. Set on the repository, it also turns the worker on in production at the next release: the release job runs in the `production` environment, whose `vars.VISION_ENABLED` falls back to the repository's value. To keep production off until the dev trial is done, set `VISION_ENABLED` to `false` on the `production` environment. Set to `true` on the `production` environment alone, it turns the worker on there alone. | `true` |
| `UNIQUE_NAME_PREFIX` | variable | repo | What every resource name starts with: `<prefix>-<env>-<resource>`. Optional: unset, it is `bloggerbear`. **Required for a fork.** Bucket names and the sign-in host name must be unique across all of AWS, and the default ones are taken. Lowercase letters, digits and hyphens, starting with a letter, at most 14, with no hyphen at the end (at most 12 if the vision worker will run in production: [the bucket that limits it](#environment-terraform-variables)). No two hyphens in a row, and no `aws`, `amazon` or `cognito` in it: the plan refuses them. Must be the same as bootstrap's `unique_name_prefix`. Set it before the first deploy and never change it. | `acme-blog` |
| `ADMIN_ALLOWED_CIDRS_DEV` | secret | repo | Your public IP, as a Terraform list. Without it nothing can call dev's admin API. | `["203.0.113.7/32"]` |
| `ADMIN_ALLOWED_CIDRS_PROD` | secret | `production` | The same, for production. | `["203.0.113.7/32"]` |
| `WAF_REGIONAL_ENABLED` | variable | repo or `production` | A cost-saving switch ([cost-saving.md](cost-saving.md)): `false` creates no regional web ACL in front of the environment's two APIs. Read as `WAF_REGIONAL_ENABLED_DEV` / `WAF_REGIONAL_ENABLED_PROD` first, then this shared name, then `true`. Only `true` or `false`; anything else fails the plan. Optional: unset is the firewall as it has always been. | `false` |
| `WAF_ADMIN_ALLOWLIST` | variable | repo or `production` | A cost-saving switch: `false` keeps the admin API's address allowlist as its resource policy alone, with no admin web ACL. `WAF_ADMIN_ALLOWLIST_DEV` / `WAF_ADMIN_ALLOWLIST_PROD` first, then this, then `true`. | `false` |
| `WAF_SITE_ENABLED` | variable | repo or `production` | A cost-saving switch, production only: `false` creates no shared CloudFront web ACL for the site and the public API's CDN. `WAF_SITE_ENABLED_PROD` first, then this, then `true`. | `false` |
| `WAF_LOG_ALL_REQUESTS` | variable | repo or `production` | A cost-saving switch: `false` has the admin web ACL log only what a rule blocked or counted. `WAF_LOG_ALL_REQUESTS_DEV` / `WAF_LOG_ALL_REQUESTS_PROD` first, then this, then `true`. | `false` |
| `ALERT_EMAIL_DEV` | secret | repo | Where dev's alarm emails go. Optional: without it the alarms still fire, but nobody is told. | `you@example.com` |
| `ALERT_EMAIL_PROD` | secret | `production` | Where production's alarm emails go. Optional. | `you@example.com` |
| `PII_DENYLIST` | secret | repo | Your own personal strings (a name, a home address), one per line. The `pii-denylist` check refuses a pull request that adds one. Optional. | not shown, on purpose |
| `RUFF_AUTOFIX_TOKEN` | secret | repo | A fine-grained token with contents:write on this repository only, so the `ruff-autofix` workflow can push safe lint fixes to a pull request's branch. Optional: without it the workflow only reports the fixes. Not set by the setup script. | not shown |
| `CLAUDE_CODE_OAUTH_TOKEN` | secret | repo | For the `claude-issue-worker` workflow, which only the repository owner can trigger. Optional: leave it unset and the workflow is unused. Not set by the setup script. | not shown |

**Not in this table, on purpose: the CoinGecko API key and the GitHub API token.** The data
sources' keys are not GitHub secrets and the setup script does not set them. They are kept in
AWS, one per environment: see [SSM Parameter Store](#ssm-parameter-store) below.

Why some are secrets and some are variables:

- A **variable** is printed in plain text in every step's log. On a public repository those logs
  are public.
- An AWS account ID is an identifier, not a credential. Knowing it does not let anyone in. This
  repository still keeps account IDs out of its logs and its code (the secret scanner refuses
  them), so the account IDs and the role ARNs, which contain one, are **secrets**. GitHub then
  masks them wherever a log would print them. Terraform's `aws_account_id` variable is marked
  `sensitive` as well, so a plan prints `(sensitive value)` where it would print the ID.
- The state bucket names are **secrets** too. A bucket name is not a credential either, but it
  says where your state is kept, and nothing needs it in a public log.
- `AWS_DEV_ACCOUNT_ID`, `AWS_PROD_ACCOUNT_ID`, `TF_STATE_BUCKET_DEV` and `TF_STATE_BUCKET_PROD`
  are read **only** from secrets. A variable with one of those names is ignored, so the value
  cannot end up unmasked by mistake.
- The role ARNs and the alert emails also fall back to a variable of the same name. Use the
  secret.
- Your IP and your alert email are never put in `terraform.tfvars`: a value checked in there
  stays in git history for good. CI passes them to Terraform at apply time.
- The name prefix and the region are **variables**. They are part of bucket names, addresses and
  resource names, which the logs print anyway. The `production` environment may set its own
  `AWS_REGION` and `TF_STATE_REGION`, which win for production. Do not give it its own
  `UNIQUE_NAME_PREFIX`: dev and production share one prefix, and the environment's name is what
  tells their resources apart.

## Bootstrap variables

Passed with `-var` when you apply `infra/bootstrap` by hand
([step 1](deployment-runsheet.md#1-bootstrap-once-by-hand)). In a two-account setup, once per
account ([separate AWS accounts](deployment-separate-accounts.md#bootstrap-once-per-account)).

| Variable | What it is | Default |
|---|---|---|
| `aws_account_id` | The account you mean to apply to. The apply refuses any other. | empty: no check |
| `github_repo` | `owner/repo` allowed to assume the deploy roles. **A fork must set this.** | the original repository |
| `state_bucket_name` | The Terraform state bucket. **A fork must choose its own.** It is not built from the prefix below. | `bloggerbear-terraform-state` |
| `unique_name_prefix` | What every resource name starts with. Names the deploy roles (`gha-<prefix>-dev-deploy`, `gha-<prefix>-prod-deploy`) and limits them to resources named `<prefix>-*`. **A fork must set it**, to the same word as `UNIQUE_NAME_PREFIX`: if the two differ, every deploy is refused. | `bloggerbear` |
| `domain_name` | Your site's domain; creates the Route 53 zone. `""` for no zone. **A fork must set this.** | `bloggerbear.com` |
| `budget_alert_email` | Where the Bedrock budget alert goes. Empty creates no budget. | empty |
| `separate_environment_permissions` | Whether each deploy role is refused the other environment's resources (the dev role production's, and the reverse). Deny only: it changes nothing a role may do in its own environment. Pass `false` to detach it if a deploy is ever refused by it. | `true` |
| `bedrock_budget_limit_usd` | The monthly Bedrock spend, in USD, that triggers the budget alert. | `20` |
| `aws_region` | Region for the state bucket and the one the deploy roles may work in. Must match `AWS_REGION`. | `ap-southeast-2` |

Its outputs feed the other tables: `dev_deploy_role_arn`, `prod_deploy_role_arn`,
`state_bucket_name`, `hosted_zone_id`, `hosted_zone_name_servers`.

## Environment Terraform variables

In `infra/environments/dev/terraform.tfvars` and `infra/environments/production/terraform.tfvars`,
changed by pull request. A value in that file wins over anything CI passes. Most deployments set
only production's domain.

| Variable | Environment | What it is | Default |
|---|---|---|---|
| `domain_name` | production | The site's domain. **A fork must change it**: the file ships with the original site's. | none: required |
| `hosted_zone_id` | production | The Route 53 zone, bootstrap's `hosted_zone_id` output. **A fork must change it.** | none: required |
| `bedrock_inference_profile_id` | both | The inference profile the Lambdas call. **Set it if you deploy outside Australia.** | `au.anthropic.claude-haiku-4-5-20251001-v1:0` |
| `bedrock_model_id` | both | A full model id or ARN, to use instead of the profile above. Any provider's model that Bedrock's Converse API reaches. | empty: use the profile |
| `web_acl_arn` | dev | Production's CloudFront web ACL, if dev should share it. One-account setups only. | empty: dev's CloudFront runs without it |
| `coingecko_api_plan` | both | `"pro"` if your CoinGecko key is a paid one. | `"demo"` |
| `ops_alexa_redirect_uris` | both | Alexa's account-linking redirect URLs, for the Alexa+ add-on ([alexa/README.md](../alexa/README.md)). | empty: no Alexa client |
| `ops_assistant_mfa` | dev | MFA on the assistant's sign-in: `"OFF"`, `"OPTIONAL"` or `"ON"`. Production always requires it. | `"OPTIONAL"` |
| `force_destroy` | dev | Whether dev's site bucket can be destroyed while not empty. | `true` |
| `vision_region` | both | Where the vision worker runs, beside the Sentinel-2 imagery. Must match the bootstrap's `vision_region`. In production, with the worker on, it and the prefix must fit one bucket name (below). | `"us-west-2"` |

Do not set these in `terraform.tfvars`; CI supplies them from the GitHub settings above:
`admin_allowed_cidrs`, `alert_email`, `aws_account_id`, `unique_name_prefix`, `aws_region`,
`vision_enabled` (the `VISION_ENABLED` variable above), and the firewall switches
`waf_regional_enabled`, `waf_admin_allowlist`, `waf_log_all_requests` and, in production only,
`waf_site_enabled` (the `WAF_*` variables above).

`unique_name_prefix` is the `UNIQUE_NAME_PREFIX` setting: every name in the environment is
`<prefix>-<env>-<resource>`, and its default is `bloggerbear`. It is at most 14 characters. The
name that sets that limit is a topic's schedule in production,
`<prefix>-production-<topic_id>-research-tick`, which must fit EventBridge Scheduler's 64
characters: 14 leaves a topic id 24, and a shorter prefix leaves more (the default leaves 27).
It also decides the `Project` tag on every resource: `BloggerBear` with the default prefix, and
the prefix itself with any other.

**With the vision worker on in production, the prefix is at most 12 characters.** The worker's
artifacts bucket is `<prefix>-<env>-vision-artifacts-<account id>-<vision_region>`
(`infra/modules/vision-worker/main.tf`), and S3 allows 63 characters. In production, with the
12-digit account id, that leaves 21 for the prefix and `vision_region` together: with `us-west-2`
(the default, 9 characters) or any other 9-character Region, a prefix of at most 12; with
`ap-southeast-2` (14), at most 7, so not even the default `bloggerbear` (11) fits. A longer name
fails the production apply that creates the worker. Dev (`-dev-`) leaves 28, so every allowed
prefix fits there with a Region name of up to 14 characters, `ap-southeast-2` included. No test
checks this bucket's length yet, and the variable's description in `variables.tf` ("every other
name has room to spare at 14") predates the worker; shortening the name, and testing it, is a code
follow-up ([vision.md §10](enhancements/archive/vision.md#10-still-open)).

The times of the fixed jobs (the weekly reflection, the Stats rollover, the daily digest, the cost
poll and the feedback musing) are not variables: they are written in each environment's `main.tf`.

## SSM Parameter Store

The API keys for the data sources. Both are optional: without one, the adapter uses the public
API with no key, at a lower rate limit.

Each is a `SecureString` parameter that **you create by hand, once per environment**, in that
environment's AWS account and region. Terraform never creates them and they are not GitHub
secrets, so a key is never in Terraform state, a Lambda's environment variables or GitHub.
Terraform only allows the Lambdas to read these exact names.

| Parameter | What it is | Where to get it | Without it |
|---|---|---|---|
| `/<prefix>/dev/coingecko-api-key` | CoinGecko API key, for the crypto adapter. | A free "Demo" key is enough: <https://www.coingecko.com/en/api>, then the developer dashboard. | The keyless public API, which is throttled often enough to lose some research ticks. |
| `/<prefix>/production/coingecko-api-key` | The same, for production. | The same key will do. | The same. |
| `/<prefix>/dev/github-api-token` | A GitHub token for the GitHub adapter, which searches public repositories. | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens. Repository access: "Public repositories". **No permissions at all.** | Unauthenticated search: 10 requests a minute, shared with others. With a token, 30 a minute of your own. |
| `/<prefix>/production/github-api-token` | The same, for production. | The same token will do. | The same. |

`<prefix>` is your `UNIQUE_NAME_PREFIX`. With the default it is `bloggerbear`, so dev's CoinGecko
parameter is `/bloggerbear/dev/coingecko-api-key`.

**To create them**, with the AWS CLI signed in to that environment's account. Put your own prefix
and region in:

```bash
aws ssm put-parameter --name /bloggerbear/dev/coingecko-api-key --type SecureString --overwrite   --region ap-southeast-2 --value YOUR_COINGECKO_API_KEY
aws ssm put-parameter --name /bloggerbear/dev/github-api-token --type SecureString --overwrite   --region ap-southeast-2 --value YOUR_GITHUB_TOKEN

aws ssm put-parameter --name /bloggerbear/production/coingecko-api-key --type SecureString --overwrite   --region ap-southeast-2 --value YOUR_COINGECKO_API_KEY
aws ssm put-parameter --name /bloggerbear/production/github-api-token --type SecureString --overwrite   --region ap-southeast-2 --value YOUR_GITHUB_TOKEN
```

- **In Git Bash on Windows**, put `MSYS_NO_PATHCONV=1` in front of each command, or the name is
  rewritten as a file path.
- **To keep the key out of your shell's history**, use the console instead: Systems Manager →
  Parameter Store → Create parameter, type `SecureString`, with the name above.
- **To check they exist** (this lists names and never shows a value):
  ```bash
  aws ssm describe-parameters --region ap-southeast-2     --parameter-filters "Key=Name,Option=BeginsWith,Values=/bloggerbear/" --query "Parameters[].Name"
  ```
- **A new or changed key is picked up the next time the Lambdas start cold.** A deploy forces that.
- **A paid (Pro) CoinGecko key** also needs `coingecko_api_plan = "pro"` in that environment's
  `terraform.tfvars`.
- Never put a key in a topic's `adapter_config`: that is stored in DynamoDB.

The longer explanation is in the deployment runsheet:
[API keys for the data sources](deployment-runsheet.md#api-keys-for-the-data-sources).

## DynamoDB: settings changed with no deploy

Tables are named `<prefix>-<env>-<name>`, where `<prefix>` is your `UNIQUE_NAME_PREFIX`: with the
default, `bloggerbear-dev-topics`. Use the admin CLI
([scripts/README.md](../scripts/README.md)); editing the item in the console works too.

| Setting | Table and item | Command | Default |
|---|---|---|---|
| The model registry: each model's name, provider and price, whether it is switched on (`enabled`), and whether the reprice may change its price (`price_locked`). The model catalog adds every model the account can call (`enabled: false`, with `region`, `discovered_at`, `pricing_unit`) and keeps its price current (`price_source`, `price_checked_at`, `price_evidence_hash`) | `<prefix>-<env>-models`, one item per `model_id` | `models add` (also switches a discovered model on; `--lock-price`), `models list`, `models discover`, `models reprice` | Empty until the first Tuesday's discover. Claude Haiku 4.5 has a built-in price |
| The model catalog: pause the weekly discover (Tuesdays 03:00 Sydney) or reprice (Wednesdays 03:00 Sydney), and how a changed price is checked (`discover_enabled`, `reprice_enabled`, `review_after_hours`, `review_tolerance_pct`, `max_change_factor`, `max_web_lookups`; [model-catalog-refresh.md](enhancements/model-catalog-refresh.md)) | `<prefix>-<env>-model-config`, item `config_id` = `model-catalog` | `model-catalog get`, `model-catalog set --discover on\|off --reprice on\|off --review-after-hours 1,2 ...` | Both on; checked 1 and 2 hours later, within 5%, at most 10x from the old price, 10 web searches a run. The days are the schedules' crons, in Terraform |
| What the model catalog last did (`last_discover_at`, `last_reprice_at` and their results) and the pricing run under review (`active_run_id`). Written by the job only | `<prefix>-<env>-model-config`, item `config_id` = `model-catalog-state` | `model-catalog get` | No item until a job runs |
| A pricing run: each changed model's old and new price, its check times and what each check found. Pruned by TTL 14 days after it closes | `<prefix>-<env>-model-config`, items `config_id` = `pricing-run#<run id>` | `model-catalog get` (the run under review) | None |
| The global default and fallback model | `<prefix>-<env>-model-config`, item `config_id` = `default` | `model-config set --model-id … --fallback-model-id …` | No item: the model Terraform set (`BEDROCK_MODEL_ID`), no fallback. |
| How often research really runs, for every topic | `<prefix>-<env>-model-config`, item `config_id` = `pipeline` | `pipeline-config set --research-interval-hours N` | 1 hour |
| The stagger: the minutes between one topic's schedules and the next (`stagger_minutes`; 0 turns it off), then `topics reschedule` to apply it | same item | `pipeline-config set --stagger-minutes N`, `topics schedule`, `topics reschedule` | 5 minutes: slot 1 researches at :05 past, slot 2 at :10, ...; the daily cycles start one slot past the hour's last heartbeat (topics × 5 minutes), then one slot apart |
| A topic's slot in the stagger (`stagger_slot`), and the schedules written for it (`research_schedule`, `daily_schedule`) | `<prefix>-<env>-topics`, one item per topic | `topics create\|update --stagger-slot N` (`""` for the next free one); `topics schedule` shows them | The next free slot when the topic is created or first edited |
| A topic paused: no research tick, no candidate ideas, no article until it is resumed (`paused`, `paused_at`, `resumed_at`); its schedules disabled; "Paused" on its page; a musing each way | same item | `topics pause <topic_id>`, `topics resume <topic_id>` | Running |
| The fresh-data review of each draft | same item | `pipeline-config set --review-mode off\|shadow\|enforce`, `--review-on-unavailable hold\|note` | `shadow`; hold |
| Who may reach the operator's assistant | same item | `pipeline-config set --assistant-access open\|allowlist\|off` | `open` |
| Security incidents: listing them, marking one seen or dealt with, opening one by hand | `<prefix>-<env>-security-events`, one item per incident | `security list`, `security acknowledge\|resolve\|reopen <event_id>`, `security open --severity … --summary …` | Opened automatically; a high one emails the alert address |
| A user locked out of the assistant after failed sign-ins | `<prefix>-<env>-sign-ins`, one item per sign-in event | `sign-ins list`, `sign-ins unlock <username>` | Locked after 5 failures in 15 minutes; lifts by itself |
| Reader feedback limits and lockdown | the feedback configuration | `feedback-config set …`; for one article, `articles feedback-lock\|feedback-unlock <article_id>` | See `feedback-config --help` |
| A topic: its adapter, schedule, time zone, editorial goals, model, rotation, whether it is financial | `<prefix>-<env>-topics`, one item per topic | `topics create`, `topics update` | Daily at 9 AM `Australia/Sydney`; hourly research heartbeat |
| How far back a topic's daily article reads findings (`findings_window_hours`), never past its last article. Widen it for a topic that does not write every day about the same thing (a rotating topic, whose cities each have some days of the week, or a weekly topic), so a finding made on a day it does not write about its subject is still drafted. Whole hours, 1 to 336: findings expire after 14 days | same item | `topics create\|update --findings-window-hours N` (`""` clears it) | 24 hours |
| A topic that rotates through its subjects, one a day ([the day-subject hook](enhancements/archive/dailysubject_hook.md)): `rotation` in its `adapter_config`, either the seven weekdays each naming a subject (`{"MON": "sydney", ...}`) or a list of subjects cycled one a day (or one a week, with `rotation_period` `"week"`), every subject at least once, and `rotation_timezone`, the zone whose calendar day decides (an IANA name). Read by an adapter that names a day subject: `rail_access` (its sites are the subjects, and `rotation_timezone` defaults to the topic's `daily_timezone`; [A `rail_access` topic](#a-rail_access-topic)). The daily cycle then writes about the day's subject only, from the Findings that report it (at most 48 of that subject's own), and keeps that subject's last article (`last_article_by_subject`, beside `last_article_at`: `topics get` and the assistant's `topics_overview` for one topic show it). `findings list --next-article` and the assistant's `topic_findings` answer for the day's subject. Set `findings_window_hours` to the longest gap between one subject's days, plus a day. **Adopt a rota on a new topic, not on one that has published:** each subject's first window starts from `findings_window_hours`, not from the topic's last article (a subject with no last article of its own is bounded by the window alone), so a topic changed with `topics update` would draft again, on each subject's first day, Findings its last article already used. Create the rotating topic and pause or delete the old one ([restart, not migrate](enhancements/archive/dailysubject_hook.md#44-migrating-the-four-deployed-topics)) | same item | `topics create --config-json`, or `topics update --adapter-config-json` on a topic that has not published yet (the whole config: read it, edit it, send it back) | No rota: the topic writes about everything it watches. `rotation_timezone` unset: the topic's `daily_timezone` if the adapter passes it (`rail_access` does), else UTC |
| A topic's goal rotation (`editorial_goal_rotation`): 2 to 7 editorial goals, each `{"name", "primary_focus"}`, taken in turn so one set of findings is written up from different sides ([common/goal_rotation.py](../lambdas/common/goal_rotation.py)). Each goal's focus stands in for `editorial_goals.primary_focus`; the topic's `exclusion_criteria` apply to every goal. New findings get the goal after the one used last; with nothing new, the daily run *revisits* the findings of the last set (still unexpired, the day's subject's only on a rotating topic) under the next goal not yet used on them, and stops (`no_findings`) once every goal has had them. The state is `goal_rotation_state` on the topic item (subject key, or `*` for the whole topic, to the goal used last, the goals used on the current set and its oldest and newest `captured_at`), written by the daily cycle. An article records its goal as `editorial_goal`, and `revisit` when it was one; a revisit's research is not counted again. Ignored on a topic with a daily editorial goal (the crypto feed) | same item | `topics create\|update --goal-rotation-json '[...]'` (`'[]'` clears it; the state is kept) | No rotation: one goal, and nothing new writes nothing |

Seeding the model tables, with a sample item, is in
[The model registry](deployment-runsheet.md#the-model-registry-seeding-and-rotation).

## Your own machine

For the admin CLI and the helper scripts ([scripts/QUICKSTART.md](../scripts/QUICKSTART.md)).

| Setting | What it is |
|---|---|
| `BLOGGERBEAR_ADMIN_API_URL` | The admin API's address: `terraform -chdir=infra/environments/<env> output -raw admin_api_url`. Each environment has its own. Or pass `--api-url`. |
| `AWS_REGION` / `AWS_DEFAULT_REGION` | The region the deployment is in. Or pass `--region`. |
| Your AWS credentials | The CLI signs each request with them (`aws configure`, SSO, or `AWS_PROFILE`). |
| `.pii-denylist` | Your own personal strings, one per line, at the repo root. Gitignored; the commit hook checks against it. |
| `git config core.hooksPath .githooks` | Turns the commit hook on, once per clone. |

## AWS account settings

Set in the AWS console, per account. Nothing in this repository can set or check them.

| Setting | Where | Why |
|---|---|---|
| A budget alarm on the account | Billing → Budgets | Before anything is applied. |
| Lambda concurrent executions | Service Quotas → AWS Lambda | New accounts often allow only 10, shared by every function. |
| The model is available to the account | Bedrock, in your region | A model the account cannot call fails at the first run, not at deploy time. |
| DynamoDB attribute-based access control | DynamoDB → Settings, per region | The assistant's `table_sample` tool needs it ([why](deployment-runsheet.md#dynamodb-tag-based-access-control-abac)). On by default in most accounts. |
| The alert email subscription | A confirmation email from AWS | Nothing is delivered until you click it. |

## GitHub repository settings

Set in GitHub, not in code (the archived [public-repo-runsheet.md](enhancements/archive/public-repo-runsheet.md) has the
commands).

| Setting | Why |
|---|---|
| A `prod` branch, and `dev` as the default | Dev deploys from `dev`; a release must be on `prod`. |
| A `production` environment with a required reviewer, limited to `v*` tags | A production deploy waits for your approval. |
| A ruleset on `dev` and `prod`: pull requests only, no force-push, no deletion | Needs a public repository or a paid plan. |
| Actions' default `GITHUB_TOKEN` read-only; fork pull requests wait for approval | So a workflow from a stranger's pull request cannot write or deploy. |
| Secret scanning, push protection, Dependabot alerts, private vulnerability reporting | Public repositories only, or a paid plan. |

## Vision and rail access

The vision work (the `satellite_vision` and `rail_access` topics, the vision worker and the
triage agent; [the pipeline page](architecture/blogger-vision.md)) is off by default. Its
settings sit in several of the tables above; this section gathers them.

### The switch

One setting turns it on: the [`VISION_ENABLED`](#github-secrets-and-variables) repository
variable, read as a switch. `true`, in any letter case, turns the worker on (GitHub Actions
compares strings ignoring case, so `TRUE` and `True` count too); any other value, or unset, is
read as off, and takes a deployed worker down on the next apply. CI passes it to Terraform as
`vision_enabled` (`TF_VAR_vision_enabled: ${{ vars.VISION_ENABLED == 'true' && 'true' || 'false' }}`
in both apply workflows); the variable defaults to `false` in both roots and is never set in
`terraform.tfvars`, where a value would beat the one CI passes. Set on the repository, it also
turns the worker on in production at the next release: `terraform-production-release.yml` runs in
`environment: production` and reads `vars.VISION_ENABLED`, which falls back to the repository's
value. To keep production off until the dev trial is done, set `VISION_ENABLED` to `false` on the
`production` environment; set it to `true` there to turn it on there alone. The bootstrap must have
been re-applied with `vision_region` first: that is what gives the deploy role the right to create
the worker ([the runsheet's steps](deployment-runsheet.md#vision-optional)).

### Terraform variables

| Variable | Where | Default | What it is |
|---|---|---|---|
| `vision_enabled` | dev and production roots, from CI | `false` | Create the worker, its artifacts bucket and its log group in `vision_region`; give the research tick the worker's ARN, and the pipeline's shared execution role (`lambda_exec`, which twelve functions use) the right to invoke it. Also passed to the operator's assistant (`module "ops_assistant"`), which may then read the worker's log and each topic's running state ([below](#the-ops-assistants-environment-variables)). Off, none of it exists. |
| `vision_region` | dev and production roots (`terraform.tfvars`) | `"us-west-2"` | Where the worker runs: beside the `sentinel-cogs` bucket, so its range reads stay in one region and only a few KB of metrics and one small PNG cross back. Must equal the bootstrap's. The home region works too, the reads then crossing instead, but in production only if the prefix and the Region fit the worker's bucket name: their lengths together at most 21, so `ap-southeast-2` fits only a prefix of 7 characters or fewer ([the limit](#environment-terraform-variables)). |
| `vision_region` | `infra/bootstrap` | `"us-west-2"` | Where the deploy roles may create Lambda functions and log groups named `<prefix>-*-vision-*`, and nowhere else outside `aws_region`. |
| `memory_size`, `timeout` | `infra/modules/vision-worker` | 2048 MB, 60 s | The worker's size and ceiling. 2048 MB is a little over one vCPU, which is what the rail task's window cap was measured against. The timeout stays under the client's default 90 s read timeout, so a caller on that default sees the worker's own error, not its own. Both vision adapters give up first, by design: they wait only 58 s (`WORKER_READ_TIMEOUT` in `common/vision_sites.py`, one rule for `rail_access` and `satellite_vision` since #298), to keep the tick's 120 s for the agent and the summary, so a run that slow is an `invoke` error to them, not the worker's, and the site is tried again next tick. |

### The worker's environment variables

Read by `lambdas/vision_worker_handler.py` on `<prefix>-<env>-vision-worker`, whose docstring
documents them. Terraform sets only `VISION_BACKEND`; the other two are set by hand (or by a
future COOL module). Terraform owns the stock worker's and the research tick's whole environment
(no `ignore_changes`), so a value added to either in the console lasts only until the next apply,
and a dev apply runs on every push to `dev` that touches `infra/`, `lambdas/` or `frontend/`. A
hand-set `VISION_ALLOWED_URL_PREFIXES` is then dropped from both, and both fall back to the
`sentinel-cogs` default without notice; keeping one needs a Terraform variable passed to both
functions, a follow-up ([vision.md §10](enhancements/archive/vision.md#10-still-open)). A COOL worker,
which Terraform does not create yet, keeps what is set on it:

| Variable | Set on | Meaning |
|---|---|---|
| `VISION_BACKEND` | the worker | `opencv` (stock `opencv-python-headless`) or `cool` (OpenCV's COOL build): what this deployment is. A request for the other backend is refused (`backend_mismatch`), so a result never claims a build it did not run on. The module sets `opencv`. |
| `COOL_BUILD_SHA256` | a COOL worker only | The fingerprint of the pinned COOL build (`lambdas/vision/build.py`). A `cool` worker whose `cv2` does not match refuses every request (`not_cool`). |
| `VISION_ALLOWED_URL_PREFIXES` | the worker, and the research tick | Optional, comma-separated: the URL prefixes an asset may be read from. Each entry must be `https://<host>/` and a path (`https://<host>/<path>`, the slash after the host required, since the check is a plain prefix match); any other entry is skipped, and a line naming it is printed. Unset, or with every entry skipped, the public `sentinel-cogs` bucket (`https://sentinel-cogs.s3.us-west-2.amazonaws.com/`). The worker and the research tick's client read it with one parser (`vision_contract.allowed_prefixes`), so a request the client sends is one the worker accepts. The worker follows no redirect, whatever the list: a 3xx is `unreadable_scene`. |

### The research tick's environment variables

| Variable | Set on | Meaning |
|---|---|---|
| `VISION_WORKER_ARN` | the research tick | The stock worker's ARN. Terraform sets it when `vision_enabled` is true and leaves it empty otherwise, which the client reads as "not configured": the adapter records `last_error` on each site and measures nothing. |
| `VISION_COOL_WORKER_ARN` | the research tick | The COOL worker's, once one exists; optional. A topic whose `backend` is `cool` needs it. |

### The ops assistant's environment variables

Set by Terraform on the assistant's MCP server (`<prefix>-<env>-ops-mcp`,
`infra/modules/ops-assistant/main.tf`); nothing to set by hand
([ops-assistant-vision.md](enhancements/archive/ops-assistant-vision.md)).

| Variable | Set on | Meaning |
|---|---|---|
| `OPS_VISION_REGION` | the ops MCP server | `vision_region` where `vision_enabled` is true, `""` elsewhere: one variable is both the switch and the Region. Empty, `vision_status` reads no running state and says the worker is off, and `log_review` neither reads nor refuses the worker. The IAM that lets it read the worker's log there and the running states (`vision.tf`) exists only when it is set. |
| `OPS_SCREEN_CLIENT_IDS` | the ops MCP server | The ask page's Cognito app client id. Only a caller whose token was issued to one of these clients is sent a table's "(unverified)" columns (site names, error text, a Finding's summary); any other client, Alexa+ among them, gets counts and reasons only. Comma-separated; empty, every caller is a voice caller. |
| `FINDINGS_TABLE` | the ops MCP server | One of the `tables` map every root passes to `module "ops_assistant"` (each key is both the environment variable that names the table and the read grant on its ARN). Findings is among them for `topic_findings`, which Queries one topic's rows; without it the tool answers "I haven't been told where the findings are." |

### A `satellite_vision` topic

Every key is in `adapter_config`, documented in the adapter's docstring
(`lambdas/common/adapters/satellite_vision.py`). `force_manual_review` is forced on and cannot
be unset. A topic created without `editorial_goals` (or without a `primary_focus` in them) is
drafted under the adapter's default goal (`ADAPTER_DEFAULTS` in
`lambdas/common/editorial_resolver.py`, #298), not the global "randomly sampling 5 to 10 highly
relevant articles": report only the measured counts with their capture dates, name no ship,
vessel, owner, company or person, give no cause, forecast, price, market or trading view, never
call an area safe, clear or unaffected, and use no number or place name that is not in the
findings.

| Key | Default | Meaning |
|---|---|---|
| `sites` | required | 1 to 10 of `{"id", "polygon": [[lon, lat], ...]}`, polygons of 3 to 64 points, each with an optional `"name"` (the id when absent). Names are used in articles exactly as given. |
| `object_noun` | `"objects"` | What articles call what is counted, e.g. `"large vessels"`. |
| `backend` | `"opencv"` | Or `"cool"`, once a COOL worker exists. |
| `params` | `{}` | Detector overrides: `stretch_max`, `block_size` (odd), `offset`, `min_length_m`, `max_length_m`, `min_elongation`, `edge_buffer_px`, within the contract's bounds. |
| `max_cloud_cover` | 60 | Scene-level cloud filter in the search, %. |
| `lookback_days` | 10 | How far back to search for a scene. |
| `coverage_floor` | 0.7 | Below it a count is never material and never part of a baseline. |
| `history_size` | 8 | Scenes kept per site. |
| `min_baseline` | 2 | Earlier clear scenes needed before a change can be material. |
| `relative_threshold`, `absolute_threshold` | 0.35, 5 | Both must be crossed against the baseline median. |
| `max_sites_per_tick` | 5 | Bound on worker calls per tick; the time budget usually allows fewer (next row). |
| `time_budget_seconds` | 60 | The measuring budget, 45 to 80 s (the research tick has 120 s). The worker is asked only with 45 s of it left (`MEASURE_RESERVE_SECONDS`) and waited on for at most 58 s (`vision_sites.time_to_measure`, the rule `rail_access` keeps too), so with the default 60 a tick asks the worker only in about its first 15 s: only the sites whose new scene is found by then are measured, and how many that is depends on the search's and the worker's times, often fewer than `max_sites_per_tick`. The rest rotate to later ticks and keep their place. Under 45, no tick could ever ask the worker, so it is refused. |
| `triage` | true | Run the agent on a numeric change. |
| `triage_max_tool_calls` | 3 | The agent's tool budget; it gets that plus two model turns. |
| `triage_deadline_seconds` | 85 | Seconds after the tick started by which the agent must have answered; out of time is "artefact". |

### A `rail_access` topic

Every key is in `adapter_config`, with the defaults of the adapter's `DEFAULTS`
(`lambdas/common/adapters/rail_access.py`, whose docstring documents them). This table is the one
list of them; [vision.md](enhancements/archive/vision.md#5-configuration) points here.
`force_manual_review` is forced on and cannot be unset. A topic created without
`editorial_goals` (or without a `primary_focus` in them) is drafted under the adapter's default
goal (`ADAPTER_DEFAULTS` in `lambdas/common/editorial_resolver.py`, #298), not the global one:
report only what was measured, with the capture dates; give no ridership, patronage, journey time,
cost, cause, forecast or recommendation (the flags and simulations are proxies, never
recommendations); use no number or place name that is not in the findings. One ready topic body,
`rail-cities`, over the four starting cities, is in `scripts/rail_topics.example.json`, with a goal
that names no city: its `rotation` gives each city its days of the week (Sydney Monday and Friday,
Melbourne Tuesday and Saturday, Singapore Wednesday and Sunday, Boston Thursday), the daily cycle
runs every day and writes about the day's city alone, and the topic field `findings_window_hours`
is 192 ([DynamoDB settings](#dynamodb-settings-changed-with-no-deploy)): Boston's 7 days between
its Thursdays plus a day, so each article drafts every Finding about its city since that city's
last. The runsheet's [rail_access topics](deployment-runsheet.md#rail_access-topics) creates it
and says how to add a city. In dev, `rail-cities` has since moved to a city a week, written up
from three sides ([RailVisionSeeding.md, section 7](RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides)).

| Key | Default | Meaning |
|---|---|---|
| `sites` | required | 1 to 8 cities (`MAX_RAIL_SITES`: a week of one city a day, and one more; each city's network is its own object, `vision/<topic>/<site>/network.json`, so the running state holds only the histories), each `{"id", "polygon": [[lon, lat], ...]}` with an optional `"name"` (the id when absent), a polygon of 3 to 64 points. The polygon must sit inside one Sentinel-2 tile: the scene search takes only scenes that hold the whole site. |
| `rotation` | none | Optional: one city a day (or a week, with `rotation_period` below). The seven weekdays each naming a site (`{"MON": "sydney", ..., "SUN": "singapore"}`) or a list of sites cycled one a day; every site needs a day, and a day that names no site is refused. With it the daily cycle writes about the day's city alone, from the Findings that report it ([the day-subject hook](enhancements/archive/dailysubject_hook.md)); without it, about every city, every run. The research tick never reads it. Set `findings_window_hours` to the longest gap between one city's days plus a day. |
| `rotation_timezone` | the topic's `daily_timezone`, else UTC | The IANA zone whose calendar day decides the rota's day, so "Tuesday" is Tuesday there. Only with a `rotation`. |
| `rotation_period` | `"day"` | With a list `rotation` only: `"day"` cycles one city a day, `"week"` gives each city in turn a whole calendar week, Monday to Sunday in `rotation_timezone` (the weeks since Monday 1970-01-05, so every reader agrees). A city then comes back only after every other city's week, longer than a Finding lives (14 days), so the research tick writes a **weekly reading**: on the first tick of a city's week that reports nothing else about it, its newest clear scene (one the agent did not doubt) is reported as that week's reading, no change claimed, with its map and planner notes, once a week (`reported_period` on the city's record). With a [goal rotation](#dynamodb-settings-changed-with-no-deploy) the following days write it up from other sides. Set `findings_window_hours` to at least 24 (the reading is written in the week's first hours). A weekday `rotation` refuses `"week"`. |
| `backend` | `"opencv"` | Or `"cool"`, once a COOL worker exists. |
| `params` | `{}` | Rail parameter overrides, any of the `RailParams` fields in [vision.md's table](enhancements/archive/vision.md#the-rail-tasks-request), within the worker contract's bounds (checked before any call). |
| `reach_m` | 1000 | Walking reach of a station, straight-line metres, 200 to 5000 (the contract's bounds). Always sent to the worker; a `reach_m` in `params` wins over it. |
| `osm_ttl_days` | 30 | How long a city's stored OpenStreetMap network serves before it is fetched again; more than 0 and at most 365. |
| `max_cloud_cover` | 40 | Scene-level cloud filter in the search, %. |
| `lookback_days` | 20 | How far back to search for a scene. |
| `coverage_floor` | 0.6 | 0 to 1. Below it a scene is never material and never part of a baseline, and never the first observation. |
| `history_size` | 6 | Scenes kept per site, 2 to 20. Only the newest keeps the station, flag, suggestion and desert lists. |
| `min_baseline` | 1 | Earlier clear scenes, measured with the same polygon and parameters, needed before a change can be material; at least 1 and under `history_size`. |
| `served_share_threshold` | 0.02 | A move of the served share of at least this from the baseline median is material. Not negative. |
| `desert_relative_threshold` | 0.10 | A move of the desert area of at least this times the baseline median (at least 1 km²) is material. Not negative. A changed station count is material on its own. |
| `max_sites_per_tick` | 1 | 1 to 8. One city per tick: the network fetch, the worker and the agent share the tick's 120 s. A site skipped for this cap, or deferred for time, keeps its place in the rotation. |
| `time_budget_seconds` | 70 | The measuring budget, 45 to 80 s. The network is fetched only with 30 s of it left (a refresh of a cached one within those 30 s; a city's first network within the rest of the budget less 10 s, so set 80 for a heavy city, as the runsheet's `rail_access` topics says), the worker asked only with 45 s left (`vision_sites.time_to_measure`, the rule `satellite_vision` keeps too) and waited on for at most 58 s. |
| `triage` | true | Run the agent on a numeric change. |
| `triage_max_tool_calls` | 3 | The agent's tool budget, 0 to 5; it gets that plus two model turns. |
| `triage_deadline_seconds` | 85 | Seconds after the tick started by which the agent must have answered, more than 0 and at most 90; out of time is "artefact". |
| `web_context` | true | Give the agent the news search tool (GDELT) for the city's rail stations and lines. |

Every number must be finite, a site id is letters, digits, `_` and `-` (at most 64), unique in
the topic, and a site name is 1 to 80 characters of plain text, used in articles exactly as given.
The admin API stores `adapter_config` as given: a config the adapter refuses fails the topic's
research tick, with the reason in the research tick's log, until it is fixed with
`topics update <id> --adapter-config-json '...'`. That flag replaces the whole `adapter_config`,
not the keys it names: `'{"time_budget_seconds": 70}'` alone drops `sites`, and the tick then
fails on that instead. Read the config with `topics get <id>`, edit it, and send all of it back;
for a rail city, keep its `sites` entry with the same `id` and polygon, so its history and its
network still match, and keep the `rotation` (without it the topic writes about every city every
day).

### No keys

The rail task needs no new secret, parameter or variable: `VISION_ENABLED` is the only setting it
shares with the ship task, and nothing else is added. Earth Search STAC, the Sentinel-2 COGs in the
`sentinel-cogs` bucket, the Overpass API and GDELT need no key, secret or parameter: nothing is
added to the tables above for them.
