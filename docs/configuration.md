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
[GitHub repository settings](#github-repository-settings).

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
| `UNIQUE_NAME_PREFIX` | variable | repo | What every resource name starts with: `<prefix>-<env>-<resource>`. Optional: unset, it is `bloggerbear`. **Required for a fork.** Bucket names and the sign-in host name must be unique across all of AWS, and the default ones are taken. Lowercase letters, digits and hyphens, starting with a letter, at most 14, with no hyphen at the end. Must be the same as bootstrap's `unique_name_prefix`. Set it before the first deploy and never change it. | `acme-blog` |
| `ADMIN_ALLOWED_CIDRS_DEV` | secret | repo | Your public IP, as a Terraform list. Without it nothing can call dev's admin API. | `["203.0.113.7/32"]` |
| `ADMIN_ALLOWED_CIDRS_PROD` | secret | `production` | The same, for production. | `["203.0.113.7/32"]` |
| `ALERT_EMAIL_DEV` | secret | repo | Where dev's alarm emails go. Optional: without it the alarms still fire, but nobody is told. | `you@example.com` |
| `ALERT_EMAIL_PROD` | secret | `production` | Where production's alarm emails go. Optional. | `you@example.com` |
| `PII_DENYLIST` | secret | repo | Your own personal strings (a name, a home address), one per line. The `pii-denylist` check refuses a pull request that adds one. Optional. | not shown, on purpose |
| `RUFF_AUTOFIX_TOKEN` | secret | repo | A fine-grained token with contents:write on this repository only, so the `ruff-autofix` workflow can push safe lint fixes to a pull request's branch. Optional: without it the workflow only reports the fixes. Not set by the setup script. | not shown |
| `CLAUDE_CODE_OAUTH_TOKEN` | secret | repo | For the `claude-issue-worker` workflow, which only the repository owner can trigger. Optional: leave it unset and the workflow is unused. Not set by the setup script. | not shown |

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
account.

| Variable | What it is | Default |
|---|---|---|
| `aws_account_id` | The account you mean to apply to. The apply refuses any other. | empty: no check |
| `github_repo` | `owner/repo` allowed to assume the deploy roles. **A fork must set this.** | the original repository |
| `state_bucket_name` | The Terraform state bucket. **A fork must choose its own.** It is not built from the prefix below. | `bloggerbear-terraform-state` |
| `unique_name_prefix` | What every resource name starts with. Names the deploy roles (`gha-<prefix>-dev-deploy`, `gha-<prefix>-prod-deploy`) and limits them to resources named `<prefix>-*`. **A fork must set it**, to the same word as `UNIQUE_NAME_PREFIX`: if the two differ, every deploy is refused. | `bloggerbear` |
| `domain_name` | Your site's domain; creates the Route 53 zone. `""` for no zone. **A fork must set this.** | `bloggerbear.com` |
| `budget_alert_email` | Where the Bedrock budget alert goes. Empty creates no budget. | empty |
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

Do not set these in `terraform.tfvars`; CI supplies them from the GitHub settings above:
`admin_allowed_cidrs`, `alert_email`, `aws_account_id`, `unique_name_prefix`, `aws_region`.

`unique_name_prefix` is the `UNIQUE_NAME_PREFIX` setting: every name in the environment is
`<prefix>-<env>-<resource>`, and its default is `bloggerbear`. It is at most 14 characters. The
name that sets that limit is a topic's schedule in production,
`<prefix>-production-<topic_id>-research-tick`, which must fit EventBridge Scheduler's 64
characters: 14 leaves a topic id 24, and a shorter prefix leaves more (the default leaves 27).
It also decides the `Project` tag on every resource: `BloggerBear` with the default prefix, and
the prefix itself with any other.

The times of the fixed jobs (the weekly reflection, the Stats rollover, the daily digest and the
cost poll) are not variables: they are written in each environment's `main.tf`.

## SSM Parameter Store

`SecureString` parameters, created by you once per environment, in that environment's account and
region ([how](deployment-runsheet.md#api-keys-for-the-data-sources)). Terraform never creates
them, so a key is never in Terraform state, a Lambda's environment variables or GitHub.

| Parameter | What it is | Without it |
|---|---|---|
| `/<prefix>/<env>/coingecko-api-key` | CoinGecko API key, for the crypto adapter. | The keyless public API. |
| `/<prefix>/<env>/github-api-token` | A GitHub token with no permissions, for the GitHub adapter. | Unauthenticated search, at a lower rate limit. |

`<prefix>` is your `UNIQUE_NAME_PREFIX`: with the default, `/bloggerbear/dev/coingecko-api-key`.

## DynamoDB: settings changed with no deploy

Tables are named `<prefix>-<env>-<name>`, where `<prefix>` is your `UNIQUE_NAME_PREFIX`: with the
default, `bloggerbear-dev-topics`. Use the admin CLI
([scripts/README.md](../scripts/README.md)); editing the item in the console works too.

| Setting | Table and item | Command | Default |
|---|---|---|---|
| The model registry: each model's name, provider and price | `<prefix>-<env>-models`, one item per `model_id` | `models add`, `models list` | Empty. Claude Haiku 4.5 has a built-in price; any other model is unpriced until registered. |
| The global default and fallback model | `<prefix>-<env>-model-config`, item `config_id` = `default` | `model-config set --model-id … --fallback-model-id …` | No item: the model Terraform set (`BEDROCK_MODEL_ID`), no fallback. |
| How often research really runs, for every topic | `<prefix>-<env>-model-config`, item `config_id` = `pipeline` | `pipeline-config set --research-interval-hours N` | 1 hour |
| The fresh-data review of each draft | same item | `pipeline-config set --review-mode off\|shadow\|enforce`, `--review-on-unavailable hold\|note` | `shadow`; hold |
| Who may reach the operator's assistant | same item | `pipeline-config set --assistant-access open\|allowlist\|off` | `open` |
| A user locked out of the assistant after failed sign-ins | `<prefix>-<env>-sign-ins`, one item per sign-in event | `sign-ins list`, `sign-ins unlock <username>` | Locked after 5 failures in 15 minutes; lifts by itself |
| Reader feedback limits and lockdown | the feedback configuration | `feedback-config set …`, `feedback-lock`, `feedback-unlock` | See `feedback-config --help` |
| A topic: its adapter, schedule, time zone, editorial goals, model, rotation, whether it is financial | `<prefix>-<env>-topics`, one item per topic | `topics create`, `topics update` | Daily at 9 AM `Australia/Sydney`; hourly research heartbeat |

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

Set in GitHub, not in code ([todo/public-repo-runsheet.md](todo/public-repo-runsheet.md) has the
commands).

| Setting | Why |
|---|---|
| A `prod` branch, and `dev` as the default | Dev deploys from `dev`; a release must be on `prod`. |
| A `production` environment with a required reviewer, limited to `v*` tags | A production deploy waits for your approval. |
| A ruleset on `dev` and `prod`: pull requests only, no force-push, no deletion | Needs a public repository or a paid plan. |
| Actions' default `GITHUB_TOKEN` read-only; fork pull requests wait for approval | So a workflow from a stranger's pull request cannot write or deploy. |
| Secret scanning, push protection, Dependabot alerts, private vulnerability reporting | Public repositories only, or a paid plan. |
