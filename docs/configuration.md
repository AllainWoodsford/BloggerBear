# Configuration

Every setting an operator has to think about, and where it goes. The steps that use them are in
[deployment-runsheet.md](deployment-runsheet.md) (dev) and
[production-runsheet.md](production-runsheet.md) (production and the domain).

Settings live in seven places:

| Where | What kind of setting | How you set it |
|---|---|---|
| [GitHub secrets and variables](#github-secrets-and-variables) | Which account, region, bucket and name prefix a deploy uses | `python scripts/setup_repo.py`, or Settings → Secrets and variables → Actions |
| [Bootstrap variables](#bootstrap-variables) | The state bucket, the deploy roles, the DNS zone | `-var` on the one-time local `terraform apply` |
| [Environment Terraform variables](#environment-terraform-variables) | The domain, the model, a few switches | `infra/environments/<env>/terraform.tfvars`, by pull request |
| [SSM Parameter Store](#ssm-parameter-store) | API keys for the data sources; [who may reach the admin API](#the-admin-apis-address-allowlist) | `aws ssm put-parameter`, or the console; the allowlist with `python scripts/admin_allowlist.py` |
| [DynamoDB](#dynamodb-settings-changed-with-no-deploy) | Models, pipeline behaviour, topics | `python scripts/admin_cli.py`, or the console |
| [The model catalog's environment variables](#the-model-catalogs-environment-variables) | Which Lambda `models reprice` and `models discover` start (not on/off switches; nothing to set) | Terraform, with a default in the code |
| [Your own machine](#your-own-machine) | Where the admin CLI points | Environment variables |

Two more are set in a console and nowhere else: [AWS account settings](#aws-account-settings) and
[GitHub repository settings](#github-repository-settings). The optional vision work spans
several of these, so its settings are gathered in [Vision and rail access](#vision-and-rail-access).
The `adapter_config` keys of the other four adapters are in [Adapter settings](#adapter-settings).

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
| `REVIEWER_ENABLED` | variable | repo or environment | Deploys Professor Grizzlewick, the reviewer bear ([reviewer-bear.md](enhancements/archive/reviewer-bear.md)). **On unless set to `false`** (any letter case): the opposite reading to `VISION_ENABLED`, so a deployment that never set it gets him. `false` deploys neither his Lambda nor his schedule, and removes them on the next apply. Set on the `production` environment alone to differ there. | `false` |
| `OPS_FIREWALL_REVIEW_DEV` | variable | repo | Lets dev's operator assistant read dev's own firewall logs: set to `true` and the next dev deploy gives its MCP server the `firewall_review` tool and the right to query the admin API's and the public API's WAF log groups (the ones that exist: see the `WAF_*` switches). Read as a switch, like `VISION_ENABLED`: only `true`, in any letter case, turns it on; unset or anything else is off, which is the default and means dev's assistant has no right to any firewall log. Never the CloudFront firewall's shared log: it carries production's visitors too, so only production's assistant reads it. Production's assistant always has the tool, so this changes dev only. A change takes effect at the next dev deploy: run `terraform` by hand from the Actions tab on `dev`. Each review is a few Logs Insights queries, billed by the data they scan. | `true` |
| `WEB_SEARCH_REGION` | variable | repo or `production` | Where the AgentCore web search gateway runs (the fallback search when GDELT fails). **Optional: unset, it is `ap-northeast-1` (Tokyo)**, where it has always been. Only `us-east-1`, `eu-west-1` or `ap-northeast-1`, the three Regions AWS offers the Web Search Tool in; anything else fails the plan. Read as `WEB_SEARCH_REGION_DEV` / `WEB_SEARCH_REGION_PROD` first, then this shared name, then `ap-northeast-1`, so each environment can have its own. The bootstrap's `web_search_regions` must include it, re-applied by hand first, or the apply is refused. Changing it moves the gateway: the next apply replaces it in the new Region. [The web search gateway's region](deployment-runsheet.md#the-web-search-gateways-region) has the steps. | `us-east-1` |
| `UNIQUE_NAME_PREFIX` | variable | repo | What every resource name starts with: `<prefix>-<env>-<resource>`. Optional: unset, it is `bloggerbear`. **Required for a fork.** Bucket names and the sign-in host name must be unique across all of AWS, and the default ones are taken. Lowercase letters, digits and hyphens, starting with a letter, at most 14, with no hyphen at the end (at most 12 if the vision worker will run in production: [the bucket that limits it](#environment-terraform-variables)). No two hyphens in a row, and no `aws`, `amazon` or `cognito` in it: the plan refuses them. Must be the same as bootstrap's `unique_name_prefix`. Set it before the first deploy and never change it. | `acme-blog` |
| `WAF_REGIONAL_ENABLED` | variable | repo or `production` | A cost-saving switch ([cost-saving.md](cost-saving.md)): `false` creates no regional web ACL in front of the environment's two APIs. Read as `WAF_REGIONAL_ENABLED_DEV` / `WAF_REGIONAL_ENABLED_PROD` first, then this shared name, then `true`. Only `true` or `false`; anything else fails the plan. Optional: unset is the firewall as it has always been. | `false` |
| `WAF_ADMIN_ALLOWLIST` | variable | repo or `production` | A cost-saving switch: `false` keeps the admin API's address allowlist as its resource policy alone, with no admin web ACL. `WAF_ADMIN_ALLOWLIST_DEV` / `WAF_ADMIN_ALLOWLIST_PROD` first, then this, then `true`. | `false` |
| `WAF_SITE_ENABLED` | variable | repo or `production` | A cost-saving switch, production only: `false` creates no shared CloudFront web ACL for the site and the public API's CDN. `WAF_SITE_ENABLED_PROD` first, then this, then `true`. | `false` |
| `WAF_LOG_ALL_REQUESTS` | variable | repo or `production` | A cost-saving switch: `false` has the admin web ACL log only what a rule blocked or counted. `WAF_LOG_ALL_REQUESTS_DEV` / `WAF_LOG_ALL_REQUESTS_PROD` first, then this, then `true`. | `false` |
| `ALERT_EMAIL_DEV` | secret | repo | Where dev's alarm emails go. Optional: without it the alarms still fire, but nobody is told. | `you@example.com` |
| `ALERT_EMAIL_PROD` | secret | `production` | Where production's alarm emails go. Optional. | `you@example.com` |
| `PII_DENYLIST` | secret | repo | Your own personal strings (a name, a home address), one per line. The `pii-denylist` check refuses a pull request that adds one. Optional. | not shown, on purpose |
| `RUFF_AUTOFIX_TOKEN` | secret | repo | A fine-grained token with contents:write on this repository only, so the `ruff-autofix` workflow can push safe lint fixes to a pull request's branch. Optional: without it the workflow only reports the fixes. Not set by the setup script. | not shown |
| `EVALS_ENABLED` | variable | repo or `production` | Set to `true` to run continuous evals: scored events from the pipeline, rolled up hourly into the evals table, and the judge ([the design](enhancements/model-ranch-prompt-assets-evals.md)). Read as a switch, like `VISION_ENABLED`: only `true`, in any letter case, turns it on; unset or anything else is off, which is the default. Until the evals module is deployed it only sets the Lambdas' `EVALS_ENABLED` environment variable, which nothing reads yet. Set on the `production` environment alone to differ there. | `true` |
| `WORKER_CONCURRENCY` | variable | repo or `production` | How many queue jobs the pipeline's workers may run at once: the work queue's `maximum_concurrency`, the one knob that caps the model calls and source requests in flight however many topics there are ([the queue platform](enhancements/model-ranch-prompt-assets-evals.md)). Optional: unset, it is `2`, the smallest value an SQS event source mapping accepts; less fails the plan. Read as `WORKER_CONCURRENCY` on the `production` environment, else the repository's, so each environment can have its own. Nothing reads it until the work-queue module is deployed. | `3` |

**Not in this table, on purpose: the CoinGecko API key, the GitHub API token and your IP
address.** None is a GitHub secret and the setup script sets none of them. They are kept in AWS,
one per environment: see [SSM Parameter Store](#ssm-parameter-store) below. (The IP used to be the
`ADMIN_ALLOWED_CIDRS_DEV` and `ADMIN_ALLOWED_CIDRS_PROD` secrets. Nothing reads them now: once
[the allowlist parameter](#the-admin-apis-address-allowlist) is set, delete them.)

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
| `web_search_regions` | The Regions the deploy roles may create the web search gateway in, as a list: `-var='web_search_regions=["ap-northeast-1","us-east-1"]'`. Every `WEB_SEARCH_REGION` value you use, dev's and production's, must be in it. Only `us-east-1`, `eu-west-1` and `ap-northeast-1`. | `["ap-northeast-1"]` |

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
| `backup_export_buckets` | both | Buckets besides the backup bucket that `admin_cli backup export --bucket` may copy articles into and `backup restore --bucket` may read a snapshot from. Each is granted to the Lambda role; the bucket itself is made and kept outside Terraform. | `[]` |

Do not set these in `terraform.tfvars`; CI supplies them from the GitHub settings above:

- `alert_email`, `aws_account_id`, `unique_name_prefix` and `aws_region`.
- `backup_bucket_name`: the `TF_STATE_BUCKET_*` secret again. Backups are kept in the state
  bucket, under `backup/<prefix>-<env>/`.
- `vision_enabled`: the `VISION_ENABLED` variable above.
- `reviewer_enabled`: the `REVIEWER_ENABLED` variable above.
- `ops_firewall_review` (dev only): the `OPS_FIREWALL_REVIEW_DEV` variable above.
- `web_search_region`: the `WEB_SEARCH_REGION` variables above; `ap-northeast-1` when unset.
- `waf_regional_enabled`, `waf_admin_allowlist`, `waf_log_all_requests` and, in production only,
  `waf_site_enabled`: the firewall switches, from the `WAF_*` variables above.

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

The times of the fixed jobs (the weekly reflection, the Stats rollover, the cost poll, the feedback
musing, the weekly backup, the reviewer bear and the model catalog's jobs) are not variables: they
are written in each environment's `main.tf`.

### The ops assistant's limits

The operator's assistant's agent Lambda (`<prefix>-<env>-ops-agent`) has three limits of its
own. They are variables of `infra/modules/ops-assistant`, not of the environment: neither root
passes them, so `terraform.tfvars` cannot set them. To change one, add it to the
`module "ops_assistant"` block in `infra/environments/<env>/main.tf`, by pull request.

| Variable | What it is | Default |
|---|---|---|
| `agent_daily_question_cap` | How many questions each signed-in user may ask per UTC day, briefings Alexa+ starts included. Past it, `POST /ask` answers 429 until midnight UTC. Terraform passes it to the agent as `OPS_AGENT_DAILY_QUESTION_CAP` (`lambdas/ops_agent/quota.py`). A whole number, 1 or more; the plan refuses anything else. | 100 |
| `agent_reserved_concurrency` | How many questions the agent may answer at once (its reserved concurrency), or -1 for no reservation. Lambda refuses a reservation that would leave the account less than its minimum of 10 unreserved, so keep -1 until the account's [Lambda concurrency quota](#aws-account-settings) is raised. 0 is refused: it would switch the agent off. | -1 |
| `agent_memory_size` | The agent Lambda's memory, in MB. Lambda gives CPU in proportion to it. | 1024 |

Terraform owns the agent's environment, so `OPS_AGENT_DAILY_QUESTION_CAP` changed on the function
in the console lasts only until the next apply. What the assistant costs, and the other limits on
it, are in [Costs and limits](architecture/ops-mcp-agent.md#costs-and-limits).

## SSM Parameter Store

The API keys for the data sources, and [the admin API's address
allowlist](#the-admin-apis-address-allowlist) (below). The keys are optional: without one, the
adapter uses the public API with no key, at a lower rate limit.

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

### The admin API's address allowlist

Only your addresses may call the admin API (what `admin_cli.py` talks to). The list is one
`SecureString` parameter per environment, and **`scripts/admin_allowlist.py` is the one thing
that writes it**. Terraform is never given an address: no variable, no GitHub secret, nothing in
a plan. (Its state still records what a refresh reads back from the IP set and the policy, in the
private state bucket, as it did before.)

| Parameter | What it is | Without it |
|---|---|---|
| `/<prefix>/dev/admin-allowed-cidrs` | Your public IPv4 ranges, comma-separated (`203.0.113.7/32`). | Nothing can call dev's admin API (it fails closed). |
| `/<prefix>/production/admin-allowed-cidrs` | The same, for production. | The same, for production. |

```bash
# Unless you use the defaults, put --prefix <your UNIQUE_NAME_PREFIX> (and --region <yours>) before the command,
# e.g. python scripts/admin_allowlist.py --prefix myprefix set --env dev --my-ip.
# The script reads neither from your environment.
python scripts/admin_allowlist.py set --env dev --my-ip     # this machine's address, as a /32
python scripts/admin_allowlist.py set --env production 203.0.113.7/32 198.51.100.0/28
python scripts/admin_allowlist.py show --env dev            # what each place holds; only reads
python scripts/admin_allowlist.py sync --env dev            # copy the stored list out again
```

- **Run `set` after the first deploy of an environment, and whenever your IP changes.** No apply
  and no pull request: the change is in force when the command returns.
- **Moving from the old `ADMIN_ALLOWED_CIDRS_*` secrets**: run `set` for each environment
  **before** the change that removed them deploys there (dev on merge, production on its release).
  The apply keeps what the IP set and the policy already hold, so the admin API is never shut;
  but an environment whose `assistant_access` is `allowlist` refuses the assistant until the
  parameter exists (it fails closed). Then delete the two secrets.
- **What `set` does**: it writes the parameter, then copies the list into the two places that
  enforce it: the WAF IP set `<prefix>-<env>-admin-allowlist` (when the `WAF_ADMIN_ALLOWLIST`
  switch is on; it applies at once) and the admin API's resource policy, a Deny of every call
  from any other address. A resource policy is enforced only from the next deployment, so it also
  deploys the stage. `sync` does the copying alone, from what is stored.
- **Terraform creates both places locked** (an empty IP set and a Deny of every call) and then
  ignores what is in them, so an apply never undoes `set`. After each apply, CI deploys the admin
  API's stage once more, so the stage keeps the policy the API holds (the `terraform.yml` and
  release workflows' "Redeploy the admin API" step).
- **IPv4 only**, because the IP set is. A bare address is its `/32`; `0.0.0.0/0` is refused, and
  anything wider than `/16` needs `--allow-wide`.
- **The ops assistant's `allowlist` mode uses the same list.** The mode itself
  (`assistant_access`: `open`, `allowlist` or `off`) is a separate setting in
  [DynamoDB](#dynamodb-settings-changed-with-no-deploy), `open` by default. Only under `allowlist`
  does the assistant read this parameter, on each request (`OPS_ASSISTANT_ALLOWED_CIDRS_PARAMETER`
  names it; its roles may read that one parameter). Under `open` or `off` it never looks.
- **Credentials**: your own, for that environment's account, with `ssm:GetParameter` and
  `PutParameter`, `wafv2:ListIPSets`, `GetIPSet` and `UpdateIPSet`, `apigateway:GET`, `PATCH`
  and `POST`, and `sts:GetCallerIdentity`.
- **`--prefix` and `--region`** default to `bloggerbear` and `ap-southeast-2`: a fork must pass
  its own `--prefix`, or it writes a parameter nothing reads.
- **In Git Bash on Windows** nothing needs `MSYS_NO_PATHCONV`: the script builds the name itself.

## DynamoDB: settings changed with no deploy

Tables are named `<prefix>-<env>-<name>`, where `<prefix>` is your `UNIQUE_NAME_PREFIX`: with the
default, `bloggerbear-dev-topics`. Use the admin CLI
([scripts/README.md](../scripts/README.md)); editing the item in the console works too.

| Setting | Table and item | Command | Default |
|---|---|---|---|
| The model registry: each model's name, provider and price, whether it is switched on (`enabled`), and whether the reprice may change its price (`price_locked`). The model catalog adds every model the account can call (`enabled: false`, with `region`, `discovered_at`, `pricing_unit`) and keeps its price current (`price_source`, `price_checked_at`, `price_evidence_hash`) | `<prefix>-<env>-models`, one item per `model_id` | `models add` (also switches a discovered model on; `--lock-price`), `models list`, `models discover`, `models reprice` | Empty until the first discover (every second day). Claude Haiku 4.5 has a built-in price |
| The model catalog: pause the discover (every second day, 02:00 Sydney), reprice (03:00) or rotation cycle (Fridays 04:00), and how a changed price is checked (`discover_enabled`, `reprice_enabled`, `cycle_enabled`, `review_after_hours`, `review_tolerance_pct`, `max_change_factor`, `max_web_lookups`; [model-catalog-refresh.md](enhancements/archive/model-catalog-refresh.md)) | `<prefix>-<env>-model-config`, item `config_id` = `model-catalog` | `model-catalog get`, `model-catalog set --discover on\|off --reprice on\|off --cycle on\|off --review-after-hours 1,2 ...` | All three on; checked 1 and 2 hours later, within 5%, at most 10x from the old price, 10 web searches a run. The days are the schedules' crons, in Terraform |
| What the model catalog last did (`last_discover_at`, `last_reprice_at` and their results) and the pricing run under review (`active_run_id`). Written by the job only | `<prefix>-<env>-model-config`, item `config_id` = `model-catalog-state` | `model-catalog get` | No item until a job runs |
| A pricing run: each changed model's old and new price, its check times and what each check found. Pruned by TTL 14 days after it closes | `<prefix>-<env>-model-config`, items `config_id` = `pricing-run#<run id>` | `model-catalog get` (the run under review) | None |
| The global default and fallback model | `<prefix>-<env>-model-config`, item `config_id` = `default` | `model-config set --model-id … --fallback-model-id …` | No item: the model Terraform set (`BEDROCK_MODEL_ID`), no fallback. |
| The shared rotation pool: the models a topic's `model_id` moves to, at random, after an article the daily cycle publishes by itself or a run with no usable angle ([models.md](models.md#the-shared-rotation-pool)) | `<prefix>-<env>-model-config`, item `config_id` = `model-rotation` | `model-rotation get`, `model-rotation set --candidates …` (`""` clears it) | No item: no pool, so nothing rotates this way |
| How often research really runs (`research_interval_hours`, whole hours, 1 to 168). The research schedule is only a heartbeat; each tick does work only once this much time has passed since the topic was last checked. A topic's own value wins over the pipeline-wide one, which every other topic inherits | `<prefix>-<env>-model-config`, item `config_id` = `pipeline`; a topic's own on its item in `<prefix>-<env>-topics` | `pipeline-config set --research-interval-hours N`; for one topic, `topics create\|update --research-interval-hours N` (`""` clears it) | 1 hour |
| The stagger: the minutes between one topic's schedules and the next (`stagger_minutes`; 0 turns it off), then `topics reschedule` to apply it | `<prefix>-<env>-model-config`, item `config_id` = `pipeline` | `pipeline-config set --stagger-minutes N`, `topics schedule`, `topics reschedule` | 5 minutes: slot 1 researches at :05 past, slot 2 at :10, ...; the daily cycles start one slot past the hour's last heartbeat (topics × 5 minutes), then one slot apart |
| A topic's slot in the stagger (`stagger_slot`), and the schedules written for it (`research_schedule`, `daily_schedule`) | `<prefix>-<env>-topics`, one item per topic | `topics create\|update --stagger-slot N` (`""` for the next free one); `topics schedule` shows them | The next free slot when the topic is created or first edited |
| A topic paused: no research tick, no candidate ideas, no article until it is resumed (`paused`, `paused_at`, `resumed_at`); its schedules disabled; "Paused" on its page; a musing each way | same item | `topics pause <topic_id>`, `topics resume <topic_id>` | Running |
| The fresh-data review of each draft (`review_mode`), and what enforce mode does when a review cannot run (`review_on_unavailable`, pipeline-wide only). A topic's own `review_mode` wins over the pipeline-wide one | `<prefix>-<env>-model-config`, item `config_id` = `pipeline`; a topic's own `review_mode` on its item in `<prefix>-<env>-topics` | `pipeline-config set --review-mode off\|shadow\|enforce`, `--review-on-unavailable hold\|note`; for one topic, `topics create\|update --review-mode off\|shadow\|enforce` (`""` clears it on update) | `shadow`; hold |
| Who may reach the operator's assistant | `<prefix>-<env>-model-config`, item `config_id` = `pipeline` | `pipeline-config set --assistant-access open\|allowlist\|off` | `open` |
| Security incidents: listing them, marking one seen or dealt with, opening one by hand ([the pipeline](architecture/security-incidents.md)) | `<prefix>-<env>-security-events`, one item per incident | `security list`, `security acknowledge\|resolve\|reopen <event_id>`, `security open --severity … --summary …` | Opened automatically; a high one emails the alert address |
| The key a client's address is hashed with before an incident is stored (`secret`), so the security-events table never holds an IP: an incident keeps `client_hash`, the first 16 hex characters of an HMAC-SHA256 of the address. Deleting the row makes a new key at the next incident; from then on the same client hashes differently, and its new incidents no longer group with its old ones. A backup leaves the row out, so a restored or new deployment makes its own key ([the keyed hash](architecture/security-incidents.md#the-keyed-hash-of-the-client)) | `<prefix>-<env>-model-config`, item `config_id` = `security-events-hash-key` | none: made on first use (`get_security_hash_key` in `lambdas/common/dynamo.py`); delete the row in the console to start a new key | No item until the first incident: a random 32-byte key, made with a conditional write so racing Lambdas share one |
| A user locked out of the assistant after failed sign-ins | `<prefix>-<env>-sign-ins`, one item per sign-in event | `sign-ins list`, `sign-ins unlock <username>` | Locked after 5 failures in 15 minutes; lifts by itself |
| Reader feedback limits and lockdown | the feedback configuration | `feedback-config set …`; for one article, `articles feedback-lock\|feedback-unlock <article_id>` | See `feedback-config --help` |
| A topic: its adapter, schedule, time zone, editorial goals, model, rotation, whether it is financial | `<prefix>-<env>-topics`, one item per topic | `topics create`, `topics update` | Daily at 9 AM `Australia/Sydney`; hourly research heartbeat |
| A topic whose every article waits for a person (`force_manual_review`, a Boolean). The daily cycle holds each draft with the reason "this topic's articles are always reviewed by a person before publishing" (`requires_manual_review` in `lambdas/common/compliance.py`). Unlike `is_financial`, it adds no financial guidance or disclaimer, and the compliance review still runs, so you see its verdict. The reviewer bear never publishes the topic's articles. The admin API forces it on for every `satellite_vision` and `rail_access` topic, on create and on each update | same item | No admin CLI flag. In the console, make the topic's item `force_manual_review` a Boolean both ways: `true` to turn it on, `false` (or remove the attribute) to turn it off. A String of either value, `"true"` or `"false"`, leaves it half on: the daily cycle adds no hold for it (`requires_manual_review` accepts only a Boolean `true`; a draft is still held for its other reasons, such as its compliance verdict), but the reviewer bear still treats the topic as a person's and never approves, reviews or defangs its articles (`lambdas/common/reviewer.py` tests the flag by truthiness). The admin API's `POST /topics` and `PUT /topics/{topic_id}` also take it in the body | Off; on for the vision topics |
| Whether the reviewer bear may approve this topic's held articles himself (`reviewer_may_approve`: `true`, `false`, or unset for his `may_approve` setting). A topic with `force_manual_review` always waits for a person | same item | `topics update --reviewer-may-approve on\|off` (`""` clears it) | Unset: the project's default, on |
| The reviewer bear's settings: his pause flag (`enabled`), whether he may approve (`may_approve`, a topic's own `reviewer_may_approve` wins), each of his five tasks (`fix_content`, `review_held`, `review_published`, `watch_flags`, `defang_held`) and his limits (`stale_after_hours`, `published_per_run`, `published_lookback`, `max_rewrites`, `content_days`, `tasks_per_tick`, `tick_minutes`, `defang_after_days`) and the bears' back-and-forth (`chat_chance_pct`, `reply_chance_pct`). Read at the start of each run | `<prefix>-<env>-model-config`, item `config_id` = `reviewer` | `reviewer get`, `reviewer set --enabled on\|off --may-approve on\|off --defang-after-days N ...` (`""` clears one) | Every switch on; 48 hours, 5 articles a run (from the newest 25), 3 rewrites, 7 days of content findings, 2 tasks a tick 5 minutes apart, 10 days before task 5 |
| The reviewer bear's run under way: its queue of tasks, its tick, what each task came to (`run_id`, `queue`, `counts`, `woken`, `next_tick_at`). Written by his Lambda only | `<prefix>-<env>-model-config`, item `config_id` = `reviewer-state` | none (read it in the console) | No item until his first run |
| On an article: when the reviewer bear last reviewed it and found it fine (`reviewer_reviewed_at`), that he took it down after readers downvoted it (`reviewer_held_at`: it is a person's to decide from
then on, for good), what he cut when he published it after `defang_after_days` (`reviewer_defanged`: when, how many sentences, for how many flags; the original body is kept as `articles/<id>.before-defang.md`), and his flag on a financial article he approved (`reviewer_flag` = `financial_approved`, `reviewer_flagged_at`), which the assistant reports until a person clears it | `<prefix>-<env>-articles`, the article's item | Read them on the article's item in the console (`articles get` does not show them); `articles checked <article_id>` clears the flag | None |
| How far back a topic's daily article reads findings (`findings_window_hours`), never past its last article. Widen it for a topic that does not write every day about the same thing (a rotating topic, whose cities each have some days of the week, or a weekly topic), so a finding made on a day it does not write about its subject is still drafted. Whole hours, 1 to 336: findings expire after 14 days | same item | `topics create\|update --findings-window-hours N` (`""` clears it) | 24 hours |
| A topic that rotates through its subjects, one a day ([the day-subject hook](enhancements/archive/dailysubject_hook.md)): `rotation` in its `adapter_config`, either the seven weekdays each naming a subject (`{"MON": "sydney", ...}`) or a list of subjects cycled one a day (or one a week, with `rotation_period` `"week"`), every subject at least once, and `rotation_timezone`, the zone whose calendar day decides (an IANA name). Read by an adapter that names a day subject: `rail_access` (its sites are the subjects, and `rotation_timezone` defaults to the topic's `daily_timezone`; [A `rail_access` topic](#a-rail_access-topic)). The daily cycle then writes about the day's subject only, from the Findings that report it (at most 48 of that subject's own), and keeps that subject's last article (`last_article_by_subject`, beside `last_article_at`: `topics get` and the assistant's `topics_overview` for one topic show it). `findings list --next-article` and the assistant's `topic_findings` answer for the day's subject. Set `findings_window_hours` to the longest gap between one subject's days, plus a day. **Adopt a rota on a new topic, not on one that has published:** each subject's first window starts from `findings_window_hours`, not from the topic's last article (a subject with no last article of its own is bounded by the window alone), so a topic changed with `topics update` would draft again, on each subject's first day, Findings its last article already used. Create the rotating topic and pause or delete the old one ([restart, not migrate](enhancements/archive/dailysubject_hook.md#44-migrating-the-four-deployed-topics)) | same item | `topics create --config-json`, or `topics update --adapter-config-json` on a topic that has not published yet (the whole config: read it, edit it, send it back) | No rota: the topic writes about everything it watches. `rotation_timezone` unset: the topic's `daily_timezone` if the adapter passes it (`rail_access` does), else UTC |
| A topic's goal rotation (`editorial_goal_rotation`): 2 to 7 editorial goals, each `{"name", "primary_focus"}`, taken in turn so one set of findings is written up from different sides ([common/goal_rotation.py](../lambdas/common/goal_rotation.py)). Each goal's focus stands in for `editorial_goals.primary_focus`; the topic's `exclusion_criteria` apply to every goal. New findings get the goal after the one used last; with nothing new, the daily run *revisits* the findings of the last set (still unexpired, the day's subject's only on a rotating topic) under the next goal not yet used on them, and stops (`no_findings`) once every goal has had them. A retry of a run that already stored its article never revisits: it finds its own run on the topic (`last_article_run`) and returns `already_written` ([runs that fail](architecture/article-research.md#runs-that-write-nothing-and-runs-that-fail)). The state is `goal_rotation_state` on the topic item (subject key, or `*` for the whole topic, to the goal used last, the goals used on the current set and its oldest and newest `captured_at`), written by the daily cycle. An article records its goal as `editorial_goal`, and `revisit` when it was one; a revisit's research is not counted again. Ignored on a topic with a daily editorial goal (the crypto feed) | same item | `topics create\|update --goal-rotation-json '[...]'` (`'[]'` clears it; the state is kept) | No rotation: one goal, and nothing new writes nothing |

Seeding the model tables, with a sample item, is in
[Seeding the registry](models.md#seeding-the-registry).

## The model catalog's environment variables

The two variables in the table below are not switches, and neither needs setting: Terraform sets
the first, and the code falls back to the same value without it.

The on/off switches are in DynamoDB instead: `discover_enabled`, `reprice_enabled` and
`cycle_enabled` on the `model-catalog` row of `<prefix>-<env>-model-config`. Each is **on unless
set to `false`**: an absent row or setting is on. Change them with `admin_cli.py model-catalog set
--discover off|on --reprice off|on --cycle off|on` ([DynamoDB: settings changed with no
deploy](#dynamodb-settings-changed-with-no-deploy)).

| Variable | Set on | Meaning | Default |
|---|---|---|---|
| `MODEL_CATALOG_FUNCTION_NAME` | every Lambda (read by the admin API) | Which Lambda `models reprice` and `models discover` start. Terraform sets it to the model-catalog function's name. | `<prefix>-<env>-model-catalog`, from `NAME_PREFIX` and `ENVIRONMENT_NAME`: the name Terraform gives the function |
| `MODEL_CATALOG_FUNCTION_ARN` | nothing, by default | What a pricing run's two review schedules invoke. Only to point them at another function. | The model-catalog Lambda's own ARN, which it reads from its invocation context |

The schedules (`<prefix>-<env>-model-discover` and `<prefix>-<env>-model-reprice`, every second
day at 02:00 and 03:00 Sydney, and `<prefix>-<env>-model-cycle`, Fridays 04:00) are Terraform's:
change their days or time there (`aws_scheduler_schedule.model_catalog_*` in each environment's
`main.tf`). Each run's review schedules (`<prefix>-<env>-price-review-<run>-<n>`) are made by the
Lambda and delete themselves once fired ([model-catalog-refresh.md](enhancements/archive/model-catalog-refresh.md)).

## The reviewer bear (Professor Grizzlewick)

An old, bespectacled, mustachioed bear who does the reviews the admins have fallen behind on, every
second day, and writes to the musings feed as himself ([how he works](architecture/reviewer-bear.md);
[the design](enhancements/archive/reviewer-bear.md)).

**One switch needs a deploy:** `REVIEWER_ENABLED` (GitHub variable) → `reviewer_enabled`
(Terraform), on unless `false`, deploys his Lambda (`<prefix>-<env>-reviewer`) and schedule.
**Everything else is a setting**, in the `reviewer` row of `<prefix>-<env>-model-config`, read at the
start of each of his runs: `python scripts/admin_cli.py reviewer get|set` ([the table
above](#dynamodb-settings-changed-with-no-deploy)).

| Setting | Default | What it does |
|---|---|---|
| `enabled` | on | Off: his scheduled runs are skipped (a run under way finishes) |
| `may_approve` | on | The project's default for whether he may approve a held article himself, financial or not. A topic's `reviewer_may_approve` (`topics update <id> --reviewer-may-approve on\|off`, `""` to clear) wins |
| `fix_content` | on | Task 1: fix the assistant's content findings from the last `content_days` (7) |
| `review_held` | on | Task 2: review articles held `stale_after_hours` (48) or more; reject after `max_rewrites` (3) |
| `review_published` | on | Task 3: review `published_per_run` (5) of the newest `published_lookback` (25) published articles |
| `watch_flags` | on | Task 4: take down a financial article he approved if readers downvote it with comments |
| `defang_held` | on | Task 5: an article held `defang_after_days` (10) or more is published with its flagged sentences cut, or rejected if it cannot stand without them |
| `tasks_per_tick`, `tick_minutes` | 2, 5 | How fast he works through a run |
| `chat_chance_pct`, `reply_chance_pct` | 25, 10 | The chance BloggerBear replies to his report on the Musings page, and that he answers back (0 turns it off) |

A topic with `force_manual_review` (every vision topic) always waits for a person, whatever these
say; tasks 2 and 5 never publish for it. Each financial article he approves is flagged for a person to check: the assistant reports it
(`reviewer_financial_check`) until `admin_cli articles checked <id>` clears it, and he takes it back
down himself if readers downvote it with comments meanwhile.

His schedule (`<prefix>-<env>-reviewer`, every second day at 05:00 Sydney) is Terraform's
(`aws_scheduler_schedule.reviewer`). A run's tick schedules (`<prefix>-<env>-reviewer-tick-<run>-<n>`,
`tick_minutes` apart) are made by his Lambda and delete themselves once fired.

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

## Adapter settings

A topic's `adapter_config` is an object on its item in `<prefix>-<env>-topics`, read only by the
topic's adapter. Set it with `topics create --config-json '{...}'`. `topics update <id>
--adapter-config-json '{...}'` replaces the whole object, so read it with `topics get <id>`, edit it,
and send all of it back. Every key below is optional, and each adapter's docstring
(`lambdas/common/adapters/`) is the source for them, except `hacker_news.py`, whose docstring
lists none: its two keys, `keywords` and `limit`, are read in `HackerNewsAdapter.fetch_state`. A
topic created without an adapter uses `web_search`. The vision adapters' keys are in [A `satellite_vision` topic](#a-satellite_vision-topic)
and [A `rail_access` topic](#a-rail_access-topic). The optional API keys are in
[SSM Parameter Store](#ssm-parameter-store), never in `adapter_config`.

A number an adapter cannot read (a word where a number goes) fails the topic's research tick, with
the reason in its log. The exception is `github_trending`, whose numbers fall back to their
defaults.

### A `github_trending` topic

One GitHub Search API request per tick, for the most-starred repositories created in the last few
days (`lambdas/common/adapters/github_trending.py`).

A tick is material when a repository never reported for the topic appears, or a known one gains
at least 100 stars or 20%. Only a material tick writes a Finding: the research tick has the model
summarise what is new and stores the summary as a Finding, which the topic's next article can be
written from. Any other tick ends as `no_change`, with no summary and no Finding.

| Key | Default | What it does |
|---|---|---|
| `language` | none | Only repositories in this language, e.g. `"python"`, as GitHub names it (`"Jupyter Notebook"` works). |
| `created_within_days` | 7 | How far back "new" reaches, 1 to 30. A value outside that range is moved to the nearest end. |
| `min_stars` | 0 | Leave out repositories with fewer stars than this. |
| `keywords` | none | Keep only repositories whose name or description mentions one of these as a whole word; a trailing `*` matches a prefix (`"crypto*"`). With keywords, 100 results are examined instead of 25; at most 25 are kept either way. |

### A `hacker_news` topic

The current Hacker News top stories, through the official API, one request per story
(`lambdas/common/adapters/hacker_news.py`). A tick is material when a story never reported for the
topic appears, or a known one gains at least 100 points or 50%.

| Key | Default | What it does |
|---|---|---|
| `keywords` | none | Keep only stories whose title mentions one of these as a whole word; a trailing `*` matches a prefix. With keywords, at most 25 of the matching stories are kept. |
| `limit` | 25, or 60 with `keywords` | How many of the top stories to read. Without `keywords`, every story read is kept. |

### A `web_search` topic

Runs the topic's searches and merges the results (`lambdas/common/adapters/web_search.py`, over
`lambdas/common/web_search.py`). A query whose search fails is skipped for that tick, and the
others still run.

| Key | Default | What it does |
|---|---|---|
| `queries` | the topic's name | A list of search strings, run in turn and merged. With neither `queries` nor `query`, the search is the meaningful words of the topic's name, joined with OR. |
| `query` | none | One search string, used when `queries` is not set. |
| `max_results` | 10 | How many results a tick keeps across all its queries, at most 25. Each query also asks for this many. |
| `max_age_hours` | 24 | Only results published this recently. |
| `title_keywords` | each query's own words | Keep only results whose title mentions one of these as a whole word; a trailing `*` matches a prefix. Unset, each query's own topical words are used: quoted phrases whole, without `OR`/`AND`/`NOT`, `field:value` operators, `-negated` terms and words under three letters. `[]` turns the filter off. |
| `provider` | the `WEB_SEARCH_PROVIDER` environment variable, else `"gdelt"` | The search backend: `"gdelt"` or `"agentcore"`. When the AgentCore gateway is configured, a failed GDELT search is retried once through it. An unknown name fails every query, so the topic finds nothing. |
| `min_new_results` | 1 | How many results never reported for the topic make a tick material. Raise it for fewer, larger Findings. |

**Choose a backend with `provider`, not with `WEB_SEARCH_PROVIDER`.** `search_web`
(`lambdas/common/web_search.py`) takes the topic's `provider` (a `crypto_feed` topic's
`web_search_provider`) first, then the `WEB_SEARCH_PROVIDER` environment variable, then `"gdelt"`
(`DEFAULT_PROVIDER`). Terraform never sets that variable, and it owns the whole environment of the two Lambdas that search, the research
tick and the daily cycle (for the fresh-data review), with no `ignore_changes`. A value you add in
the console lasts only until the next apply: in dev, every push to `dev` that touches `infra/`,
`lambdas/` or `frontend/`. The `adapter_config` key is the only choice that lasts.

An unknown name in `WEB_SEARCH_PROVIDER` fails every search of every topic that does not name its
own backend.

**`"agentcore"` turns the retry off.** A failed search is retried through the AgentCore gateway
only when the chosen backend is not `agentcore` and the gateway is configured
(`_fallback_configured`). With `agentcore` chosen, a failed search is not retried.

### A `crypto_feed` topic

CoinGecko market data and crypto news, shaped by the day's editorial goal
(`lambdas/common/adapters/crypto_feed.py`, `lambdas/common/editorial_goals.py`). The goal is one
of four, drawn at random for each UTC day from the date, so the research tick and the daily cycle
always agree on it.

- The market anchors, the coins every other coin is read against, are fixed: Bitcoin and
  Ethereum. A `MARKET_NEWS` day has none. The old `coin_ids` key is no longer read.
- The admin API forces `is_financial` on for this adapter, so every article is held for review.
- The daily cycle ignores a [goal rotation](#dynamodb-settings-changed-with-no-deploy)
  (`editorial_goal_rotation`) on this topic: the day's goal takes its place.

| Key | Default | What it does |
|---|---|---|
| `editorial_goal` | none: the day's draw | Pins one goal instead of the draw: `ALTCOIN_DEEP_DIVE`, `WEB_AGGREGATOR`, `TREND_INVENTOR` or `MARKET_NEWS` (any case). The research tick, the daily cycle and the fresh-data review all follow it. An unknown name is ignored, and the day's draw used. |
| `web_search_queries` | `(bitcoin OR ethereum OR cryptocurrency OR crypto)` | The crypto news searches, a list of strings: the whole of a `WEB_AGGREGATOR` day, and the headlines beside the coin data on an `ALTCOIN_DEEP_DIVE` or `TREND_INVENTOR` day. A result's title must contain a word from the adapter's fixed crypto list. |
| `market_news_queries` | one query on stocks, interest rates, inflation, central banks, earnings, bond yields, oil prices and the economy | The general-market news searches for a `MARKET_NEWS` day, a list of strings. A headline that mentions crypto is dropped. |
| `web_search_provider` | as `web_search`'s `provider` | The search backend for both kinds of news search. |

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
the worker ([the runsheet's steps](vision-runsheet.md#turning-the-worker-on)).

### Terraform variables

| Variable | Where | Default | What it is |
|---|---|---|---|
| `vision_enabled` | dev and production roots, from CI | `false` | Create the worker, its artifacts bucket and its log group in `vision_region`; give the research tick the worker's ARN, and the pipeline's shared execution role (`lambda_exec`, which thirteen functions use, the reviewer bear among them) the right to invoke it. Also passed to the operator's assistant (`module "ops_assistant"`), which may then read the worker's log and each topic's running state ([below](#the-ops-assistants-environment-variables)). Off, none of it exists. |
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
([ops-assistant-vision.md](enhancements/archive/ops-assistant-vision.md)). The agent Lambda's own
setting, its daily question cap, is in [The ops assistant's limits](#the-ops-assistants-limits).

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
last. The runsheet's [rail_access topics](vision-runsheet.md#rail_access-topics) creates it
and says how to add a city. In dev, `rail-cities` has since moved to a city a week, written up
from four sides ([RailVisionSeeding.md, section 7](RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides)).

| Key | Default | Meaning |
|---|---|---|
| `sites` | required | 1 to 8 cities (`MAX_RAIL_SITES`: a week of one city a day, and one more; each city's network is its own object, `vision/<topic>/<site>/network.json`, so the running state holds only the histories), each `{"id", "polygon": [[lon, lat], ...]}` with an optional `"name"` (the id when absent), a polygon of 3 to 64 points. The polygon must sit inside one Sentinel-2 tile: the scene search takes only scenes that hold the whole site. |
| `rotation` | none | Optional: one city a day (or a week, with `rotation_period` below). The seven weekdays each naming a site (`{"MON": "sydney", ..., "SUN": "singapore"}`) or a list of sites cycled one a day; every site needs a day, and a day that names no site is refused. With it the daily cycle writes about the day's city alone, from the Findings that report it ([the day-subject hook](enhancements/archive/dailysubject_hook.md)); without it, about every city, every run. The research tick checks it (a broken rota fails the tick) but measures every city in turn, whatever the day; it uses the rota only for a week rota's weekly reading (`rotation_period` below). Set `findings_window_hours` to the longest gap between one city's days plus a day. |
| `rotation_timezone` | the topic's `daily_timezone`, else UTC | The IANA zone whose calendar day decides the rota's day, so "Tuesday" is Tuesday there. Only with a `rotation`. |
| `rotation_period` | `"day"` | With a list `rotation` only: `"day"` cycles one city a day, `"week"` gives each city in turn a whole calendar week, Monday to Sunday in `rotation_timezone` (the weeks since Monday 1970-01-05, so every reader agrees). A city then comes back only after every other city's week, longer than a Finding lives (14 days), so the research tick writes a **weekly reading**: on the first tick of a city's week that reports nothing else about it, its newest clear scene (one the agent did not doubt) is reported as that week's reading, no change claimed, with its map and planner notes, once a week (`reported_period` on the city's record). With a [goal rotation](#dynamodb-settings-changed-with-no-deploy) the following days write it up from other sides. Set `findings_window_hours` to at least 24 (the reading is written in the week's first hours). A weekday `rotation` refuses `"week"`. |
| `backend` | `"opencv"` | Or `"cool"`, once a COOL worker exists. |
| `params` | `{}` | Rail parameter overrides, any of the `RailParams` fields in [vision.md's table](enhancements/archive/vision.md#the-rail-tasks-request), within the worker contract's bounds (checked before any call); among them `stop_reach_m` (400: the straight-line walking reach of a mapped bus, tram or ferry stop, 5 minutes at 80 m a minute) and `feeder_reach_m` (2000: how far beyond a station's own reach its hinterland is read for feeder coverage; at least `reach_m`). The stop measures are reported, never material, and these two are left out of the baseline's comparability key, so changing them starts no new baseline. |
| `reach_m` | 1000 | Walking reach of a station, straight-line metres, 200 to 5000 (the contract's bounds). Always sent to the worker; a `reach_m` in `params` wins over it. |
| `osm_ttl_days` | 30 | How long a city's stored OpenStreetMap network serves before it is fetched again; more than 0 and at most 365. A network stored before the stops layer (every bus and tram stop as bare points) carries none, so the city's stop measures stay absent (`no_stops_layer`, not zero) and it gets no transit-reach map until that refresh or a `network_prefetch` ([the runsheet](vision-runsheet.md#rail_access-topics)). |
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
