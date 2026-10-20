# Deployment runsheet

How to run your own copy of BloggerBear: from an empty AWS account to a dev site that researches
and publishes by itself. Production and a custom domain are the next runsheet,
[production-runsheet.md](production-runsheet.md), which picks up where this one stops.

You set it up with GitHub secrets and variables, one local Terraform apply, and a few commands.
You should not need to edit Terraform, except for what is listed under
[What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment).
Every setting, and where it goes, is in one table: [configuration.md](configuration.md).
Doing this for the first time? [setup-guide.md](setup-guide.md) is the same journey from the very
beginning (installing the tools, an AWS account and CLI profile), with a placeholder for every
value you choose.

The account ID on this page, `111111111111`, is a placeholder.
Use your own. Commands name `ap-southeast-2` (Sydney), the default region; use yours if you chose
another ([Deploying to another region](#deploying-to-another-region)).

## Contents

- [Quick start: the setup script](#quick-start-the-setup-script)
- [How a deploy picks its account](#how-a-deploy-picks-its-account)
- [Before you start](#before-you-start)
- [1. Bootstrap, once, by hand](#1-bootstrap-once-by-hand)
- [2. GitHub settings](#2-github-settings)
- [3. One account or two](#3-one-account-or-two)
- [4. First deploy: dev](#4-first-deploy-dev)
- [5. After the first deploy](#5-after-the-first-deploy)
  - [Interrogating a topic's Findings](#interrogating-a-topics-findings)
- [Optional, but recommended](#optional-but-recommended)
  - [API keys for the data sources](#api-keys-for-the-data-sources)
  - [The model registry: seeding and rotation](#the-model-registry-seeding-and-rotation)
  - [DynamoDB tag-based access control (ABAC)](#dynamodb-tag-based-access-control-abac)
  - [CloudWatch Logs tags (the assistant's log tools)](#cloudwatch-logs-tags-the-assistants-log-tools)
  - [The personal-data and secret checks](#the-personal-data-and-secret-checks)
  - [Vision (optional)](#vision-optional)
  - [`rail_access` topics](#rail_access-topics)
  - [Evidence: what the pipeline measured and decided](#evidence-what-the-pipeline-measured-and-decided)
  - [The COOL backend and the benchmark](#the-cool-backend-and-the-benchmark)
  - [The evaluation's real-data runs](#the-evaluations-real-data-runs)
  - [The evidence export, the trace and the labels (O9)](#the-evidence-export-the-trace-and-the-labels-o9)
- [Deploying to another region](#deploying-to-another-region)
- [Tearing down and cost control](#tearing-down-and-cost-control)
  - [Keeping dev's content across a teardown](#keeping-devs-content-across-a-teardown)
- [Local development](#local-development)
- [What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment)

## Quick start: the setup script

`scripts/setup_repo.py` asks for every GitHub setting a deployment needs, checks each answer, shows
what is already set on your repository, and then sets what is missing. It needs Python 3.11 or
newer and the GitHub CLI (`gh`), signed in (`gh auth login`). Nothing else: no AWS credentials, no
packages.

Start with a dry run. It asks the same questions and runs the same checks, then prints the
commands it would run, with every value replaced by `<redacted>`. It sets nothing.

```bash
python scripts/setup_repo.py --dry-run
python scripts/setup_repo.py                      # the real thing
python scripts/setup_repo.py --repo your-name/your-fork
python scripts/setup_repo.py --only ADMIN_ALLOWED_CIDRS_DEV ADMIN_ALLOWED_CIDRS_PROD
```

What it does, in order:

1. Checks `gh` is installed and signed in, shows which repository it will change and asks you to
   confirm it. **In a fork, `gh` can pick the original repository. Read the name.**
2. Lists what is already set, by name. A secret's value cannot be read back, by anyone, and the
   script never tries. Settings that are already set are skipped unless you ask to replace them.
3. Asks for each setting that is left, with a short explanation and a check. The region comes
   first (`AWS_REGION`; press Enter for the default, `ap-southeast-2`), then the name prefix
   (`UNIQUE_NAME_PREFIX`; a fork must choose its own), because the steps it prints later are
   built from both. Then your allowed address ranges, the alert emails, the two account IDs,
   the two deploy role ARNs (with the steps to create them and the ARN it expects, which you can
   accept by pressing Enter), the state bucket names, and `PII_DENYLIST`. Press Enter to leave
   an optional setting unset.
4. Shows a summary. Secret values are masked: you see a length and the last two characters.
5. Asks once more, then sets everything.
6. Tells you how to turn on the git hook that stops personal data being committed, and offers to
   run that one command (`git config core.hooksPath .githooks`) for this clone.
7. Explains the optional CoinGecko API key, which is kept in AWS and not on GitHub
   ([API keys for the data sources](#api-keys-for-the-data-sources)). It prints the command for
   you to run, with a placeholder where the key goes. **It does not store the key and never asks
   for it.** A run narrowed with `--only` skips this step.

**What "all or nothing" means here.** Nothing is set until every answer has passed its check and
you have confirmed the summary. Cancel at any point before that (Ctrl-C works at every question)
and nothing has changed. GitHub has no way to set several secrets as one step, though. If a write
fails part-way, the script stops at once and lists exactly which settings were set and which
were not. It does not try to put old values back: they cannot be read. Run it again and it picks
up what is missing.

The script sets up GitHub. It changes nothing in AWS: you still run the bootstrap in step 1
yourself. Its only AWS calls are three reads for step 7 (`aws --version`,
`aws sts get-caller-identity`, `aws ssm describe-parameters`, which lists names and never returns
a value), and it works without the AWS CLI. It also needs the `production` environment to exist
(step 2) before it can put production's secrets there, and tells you if it does not.

## How a deploy picks its account

- **The role decides the account.** Each workflow assumes one IAM role through GitHub OIDC:
  `AWS_DEV_DEPLOY_ROLE_ARN` for dev, `AWS_PROD_DEPLOY_ROLE_ARN` for production. Whatever account
  that role lives in is the account Terraform changes. No access keys are stored anywhere.
- **The account ID is a check, not a selector.** `AWS_DEV_ACCOUNT_ID` and `AWS_PROD_ACCOUNT_ID`
  tell Terraform which account you *meant*. If the role turns out to be in a different one, the
  run stops before it plans anything.
- **Dev deploys from the `dev` branch. Production deploys from a release**, through the GitHub
  environment named `production`. There is deliberately no `dev` GitHub environment: the dev role
  trusts the branch, and putting the dev job in an environment would change the identity GitHub
  presents and break that trust.
- **Terraform is never applied ad hoc.** Merging to `dev` applies dev. Publishing a release from
  `prod` applies production, after your approval. The one exception is the bootstrap in step 1,
  which is always applied by hand and never by CI.

## Before you start

- **An AWS account, or better, two.** If you have two accounts, put dev in one and production in
  the other: a quota, a bad permission change or a teardown in dev then cannot touch production.
  One account works too, and is what the original deployment uses; the names never collide.
  [Step 3](#3-one-account-or-two) says what differs, and
  [separate AWS accounts](deployment-separate-accounts.md) is the guide for two.
- Root MFA on in each account, and an admin identity (IAM or IAM Identity Center, MFA on) that you
  can use from your own machine for the one-time bootstrap. Do not use root day to day.
- **A budget alarm on each account, before anything else is applied.** The Bedrock budget that
  bootstrap creates is in addition to this one, not a replacement.
- **The model available to your account, in the region you deploy to.** The default is the AU
  Claude Haiku 4.5 inference profile, which exists only in Australian regions
  ([Deploying to another region](#deploying-to-another-region)). Prove it works before the first
  deploy, because nothing checks it at deploy time:
  ```bash
  aws bedrock-runtime converse --region ap-southeast-2 \
    --model-id au.anthropic.claude-haiku-4-5-20251001-v1:0 \
    --messages '[{"role":"user","content":[{"text":"Say hello"}]}]'
  ```
- Terraform 1.10 or newer, the AWS CLI, Python 3.11 or newer and the GitHub CLI.
- Your fork on GitHub, with Actions enabled, a `dev` branch (the default) and a `prod` branch.
- Room in your Lambda quota. A new AWS account often allows only 10 concurrent Lambda executions
  for the whole account. Everything here runs within that, but it is shared by every function in
  both environments: ask for more under Service Quotas > AWS Lambda > "Concurrent executions"
  before you rely on the site.
- For production only: a domain you control, bought from any registrar. Production always uses a
  custom domain. Dev needs none.

## 1. Bootstrap, once, by hand

`infra/bootstrap` creates the Terraform state bucket, the GitHub OIDC provider, the two deploy
roles and (if you give it a domain) the Route 53 hosted zone. It has no backend of its own,
because it creates the bucket a backend would use. CI never applies it.

```bash
cd infra/bootstrap
terraform init
terraform apply \
  -var="aws_account_id=111111111111" \
  -var="github_repo=your-name/your-fork" \
  -var="state_bucket_name=yourname-bloggerbear-terraform-state" \
  -var="unique_name_prefix=acme-blog" \
  -var="domain_name=example.com" \
  -var="budget_alert_email=you@example.com"
```

| Variable | What it is | Default |
|---|---|---|
| `aws_account_id` | The account you mean to apply to. The apply refuses any other. | empty: no check |
| `github_repo` | `owner/repo` allowed to assume the deploy roles. **A fork must set this**, or the roles trust the original repository and your workflows are refused. | the original repository |
| `state_bucket_name` | The state bucket. Bucket names are unique across all of AWS, so **a fork must choose its own**. | `bloggerbear-terraform-state` |
| `unique_name_prefix` | What every resource name starts with: `<prefix>-<env>-<resource>`. Here it names the deploy roles (`gha-<prefix>-dev-deploy`, `gha-<prefix>-prod-deploy`) and limits them to resources named `<prefix>-*`. **A fork must set it**: the default's bucket names are taken. No hyphen at the end. | `bloggerbear` |
| `domain_name` | Your site's domain. Creates the hosted zone. Pass `""` for no zone (dev only, or until you have a domain). **A fork must set this**, or it creates a zone for the original domain. | `bloggerbear.com` |
| `budget_alert_email` | Where the Bedrock budget alert goes. Empty creates no budget. | empty |
| `separate_environment_permissions` | Whether each deploy role is refused the other environment's resources (the dev role production's, and the reverse). Deny only: it changes nothing a role may do in its own environment. Pass `false` to detach it if a deploy is ever refused by it. | `true` |
| `aws_region` | Region for the state bucket, and the region the deploy roles are allowed to work in. Must be the same as the `AWS_REGION` GitHub variable. | `ap-southeast-2` |
| `vision_region` | Where the vision worker runs. The deploy roles may create its Lambda functions and log groups (`<prefix>-*-vision-*`) there. Must be the same as each environment's `vision_region` ([Vision (optional)](#vision-optional)). | `us-west-2` |

Keep the outputs: `state_bucket_name`, `dev_deploy_role_arn`, `prod_deploy_role_arn`,
`hosted_zone_id`, `hosted_zone_name_servers`.

**The prefix here and the `UNIQUE_NAME_PREFIX` GitHub variable (step 2) must be the same word.**
Bootstrap allows the deploy roles only resources named `<prefix>-*`, and the deploys name
everything `<prefix>-<env>-<resource>`. If the two differ, every deploy is refused with
AccessDenied. Changing the prefix later means applying bootstrap again, which renames the two
roles, so set both role ARN secrets again too.

Bootstrap keeps its state in a local file. Keep that file somewhere safe; it is not in git.

## 2. GitHub settings

**The branches.** `dev` is the default branch and deploys dev. Create `prod` from it:

```bash
git fetch origin && git branch prod origin/dev && git push -u origin prod
```

**The `production` environment.** Create it under **Settings → Environments**, with yourself as a
required reviewer and deployments limited to `v*` tags. That gate is what makes a production
release a deliberate, approved act. From the command line:

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # check this is YOUR repository
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}]}" |
  gh api -X PUT "repos/$REPO/environments/production" --input -
```

**The secrets and variables.** Run the setup script (above). To set them by hand, they go under
**Settings → Secrets and variables → Actions**; the full table, with what each one is and an
example, is in [configuration.md](configuration.md#github-secrets-and-variables). The ones a first
deploy cannot do without:

- `AWS_DEV_DEPLOY_ROLE_ARN` and `AWS_PROD_DEPLOY_ROLE_ARN`: the bootstrap outputs.
- `TF_STATE_BUCKET_DEV` and `TF_STATE_BUCKET_PROD`: your state bucket.
- `UNIQUE_NAME_PREFIX`: what every resource name starts with (`<prefix>-<env>-<resource>`). The
  same word you gave bootstrap as `unique_name_prefix`. Unset, it is `bloggerbear`, which only
  the original deployment can use: bucket names and the sign-in host name must be unique across
  all of AWS.
- `ADMIN_ALLOWED_CIDRS_DEV` and `ADMIN_ALLOWED_CIDRS_PROD`: your public IP. Without it nothing can
  call the admin API: the allowlist fails closed.

Dev's settings must be at repository level, because the dev job runs in no environment.
Production's can be on the `production` environment or on the repository.

Set `UNIQUE_NAME_PREFIX` **before your first deploy and never change it**. Every name starts
with it, and a bucket or a table cannot be renamed: changing the prefix later makes Terraform
delete them and create empty ones. Write it without a hyphen at the end (`acme-blog`, not
`acme-blog-`). Lowercase letters, digits and hyphens, starting with a letter, at most 14
characters: a topic's schedule is named `<prefix>-production-<topic_id>-research-tick` and AWS
allows 64, so a 14-character prefix leaves a topic id 24 and a shorter one leaves more. If the
vision worker will run in production, keep it to 12: the worker's bucket,
`<prefix>-production-vision-artifacts-<account id>-<vision_region>`, must fit S3's 63 characters,
which leaves the prefix and `vision_region` 21 together (12 beside the default `us-west-2`; a
home-Region `ap-southeast-2` fits only a prefix of 7 or fewer;
[configuration.md](configuration.md#environment-terraform-variables)).

**Protecting the branches.** Require a pull request on `dev` and `prod`, and block force-pushes
and deletions. GitHub offers rulesets and branch protection only on public repositories or paid
plans; the archived [public-repo-runsheet.md](enhancements/archive/public-repo-runsheet.md) has the commands. Without
them the AWS side still holds: the deploy roles trust only the `dev` branch and the `production`
environment, and nothing is applied before the security scans, lint and tests pass in the same run.

## 3. One account or two

**Two accounts (recommended if you have them):** everything is the same, except that the
account-level work is done twice. In short:

- Apply bootstrap **twice**, once with each account's credentials, each with its own
  `aws_account_id` and its own `state_bucket_name` (bucket names are unique across all of AWS).
- `AWS_DEV_ACCOUNT_ID` and `AWS_PROD_ACCOUNT_ID` differ. So do the two role ARNs, and
  `TF_STATE_BUCKET_DEV` and `TF_STATE_BUCKET_PROD`.
- **Leave dev's `web_acl_arn` empty.** A CloudFront distribution can only use a web ACL owned by
  its own account, so dev's distributions run without the shared one.
- Account-level settings (the Lambda quota,
  [DynamoDB's tag-based access control](#dynamodb-tag-based-access-control-abac)) must be right
  in both.

**[Deploying your own: separate AWS accounts](deployment-separate-accounts.md)** is the full
guide: what you do twice, both bootstrap commands, what the GitHub OIDC trust looks like for
your fork, what dev's firewall loses, and a checklist.

**One account:**

- Apply bootstrap once.
- Both role ARNs are in the same account. Both account IDs are the same. Both state bucket
  secrets name the same bucket; dev and production use different keys inside it.
- Dev may share production's CloudFront web ACL: after production's first apply, put its
  `wafv2_web_acl_arn` output in `infra/environments/dev/terraform.tfvars` as `web_acl_arn`.
- You cannot run two copies of the same environment under one prefix in one account: resource
  names are `<prefix>-<environment>-<resource>`.

A third arrangement, one deployment account whose role assumes a role in each environment's
account, is not built.
[Enhancement: a deployment account](deployment-separate-accounts.md#enhancement-a-deployment-account-that-assumes-a-role-in-each-environments-account)
sketches it.

## 4. First deploy: dev

1. Merge anything under `infra/`, `lambdas/` or `frontend/` to `dev`. The `terraform` workflow
   runs the security scans, lint and tests, then applies dev. No approval is needed.
2. In the `apply-dev` job's log, the init step should succeed (your state bucket's name shows as
   `***`, because it is a secret), and the plan should list resources named with your prefix. The
   first run creates everything: the tables, the buckets, the Lambda functions, both APIs, the web
   ACLs, the dashboards and the static site.
3. To prove the account check works, set `AWS_DEV_ACCOUNT_ID` to a wrong 12-digit number and
   re-run. It must fail during the plan with "AWS account ID not allowed". Put the right value
   back. (The error names the account the role is really in.)
4. `aws sts get-caller-identity` with your own credentials shows which account you are looking
   at. The dev site's address is the `terraform output` of `infra/environments/dev`.

To run Terraform for an environment from your own machine (to read outputs, or to plan), give
`init` the same bucket CI uses, and the region if it is not `ap-southeast-2`:

```bash
terraform -chdir=infra/environments/dev init \
  -backend-config="bucket=yourname-bloggerbear-terraform-state" \
  -backend-config="region=ap-southeast-2"
TF_VAR_aws_region=ap-southeast-2 terraform -chdir=infra/environments/dev plan
```

## 5. After the first deploy

Dev is up once the `terraform` workflow goes green. What is left is by hand.

1. **Find your addresses.** `terraform -chdir=infra/environments/dev output` (after the `init`
   shown above) prints the site's address, the public and admin API URLs, and the assistant's
   `ops_ask_url`, `ops_mcp_url`, `ops_user_pool_id`, `ops_app_client_id` and
   `ops_hosted_ui_domain`.
2. **Confirm the alert email.** AWS sends a confirmation to `ALERT_EMAIL_DEV`; nothing is
   delivered until you click it.
3. **Point the admin CLI at dev** ([scripts/QUICKSTART.md](../scripts/QUICKSTART.md)):
   ```bash
   pip install -r scripts/requirements.txt
   export AWS_REGION=ap-southeast-2
   export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/dev output -raw admin_api_url)
   python scripts/admin_cli.py topics list
   ```
   The admin API only answers from the addresses in `ADMIN_ALLOWED_CIDRS_DEV`, with your own AWS
   credentials.
4. **Seed a topic, and run it once by hand:**
   ```bash
   python scripts/admin_cli.py topics create \
     --topic-id github-trending --name "GitHub Trending" \
     --adapter github_trending

   python scripts/admin_cli.py topics trigger github-trending --pipeline research_tick
   python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle
   python scripts/admin_cli.py inbox
   ```
   Creating a topic creates its two schedules; after this it runs unattended. A new topic writes
   its daily article after 9 AM `Australia/Sydney`, at the minute its stagger slot gives it
   (`topics schedule` shows when); `--daily-cadence` and `--daily-timezone` change
   that. [scripts/README.md](../scripts/README.md) is the full CLI reference.
5. **The operator's assistant.** A sign-in page at `<site>/ask.html`, in front of an agent that
   reports what needs your attention and shows the `admin_cli` command for each thing. It never
   runs anything. Its user pool has self sign-up off, so create your user by hand:
   ```bash
   POOL=$(terraform -chdir=infra/environments/dev output -raw ops_user_pool_id)
   aws cognito-idp admin-create-user --user-pool-id "$POOL" --username <you> \
     --user-attributes Name=email,Value=<your email> --message-action SUPPRESS
   aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username <you> \
     --password '<a long password>' --permanent
   ```
   To restrict it to your own addresses, or switch it off, without a deploy:
   `python scripts/admin_cli.py pipeline-config set --assistant-access allowlist` (or `off`;
   `open` is the default). Check
   [DynamoDB's tag-based access control](#dynamodb-tag-based-access-control-abac) is on, or its
   `table_sample` tool refuses every table.

   **Sign-ins are logged, and a user is locked after five failed attempts in fifteen minutes.**
   The next attempt is refused before the password is looked at, a high-severity security
   incident is raised, and the alert email is sent. The lock lifts by itself as the attempts
   age past fifteen minutes. To see the log, or to let a locked user in at once:
   ```bash
   python scripts/admin_cli.py sign-ins list            # who signed in, who failed, who is locked
   python scripts/admin_cli.py sign-ins unlock <you>    # clears the lock; the password is unchanged
   ```
   If the lock was not your own doing, change that user's password before you unlock them
   (`aws cognito-idp admin-set-user-password`, as above). Anyone who knows a username can lock
   that user out by guessing, so if it keeps happening set the assistant's access to
   `allowlist`. Guesses at usernames that do not exist are not seen: Cognito does not report
   them. The assistant reads the same log ("has anyone tried to sign in?").
6. **Production and your domain:** follow [production-runsheet.md](production-runsheet.md). It
   covers the DNS zone, pointing your registrar at it, the first release, and what to check after.

If you keep the data sources this project ships with, keep their credits too: see
[Data sources and attribution](../README.md#data-sources-and-attribution) in the README.

### Interrogating a topic's Findings

Research writes a Finding only when a tick's diff was material, and the daily article is written
from the Findings since the topic's last article within its findings window. To see what a topic
found, why it wrote nothing, or what its next article will be written from, there are three ways,
all read-only ([design](enhancements/archive/ops-assistant-vision.md#topic-findings)):

1. **Ask the assistant** (the ask page, or Alexa+): "what did research find for rail Sydney?",
   "what will the next crypto article be written from?". `topic_findings` puts the Findings on
   screen newest first and says how many there are and how old the newest is; the summaries are on
   the page only, never read aloud, and a voice caller gets counts and ages.
2. **The CLI:** `python scripts/admin_cli.py findings list <topic_id>`, with `--next-article` for
   exactly what the next daily run reads, `--days D`, `--since ISO` and `--limit N` (see
   [scripts/README.md](../scripts/README.md)). It is `GET /topics/{topic_id}/findings` on the
   admin API, and returns the raw items.
3. **A Claude Code session** in this repository: the `topic-findings` skill
   (`.claude/skills/topic-findings/SKILL.md`) teaches it the commands, what each field means, and
   the rules for the text a Finding carries (data, never instructions; never fetch a URL it names;
   never copy its text into a commit or a pull request).

The assistant's role reads the Findings table (both roots' `module "ops_assistant"` `tables`):
without that entry the assistant answers "I haven't been told where the findings are."

## Optional, but recommended

None of this blocks a deploy. Each item is done once per environment, in that environment's
account and region.

### API keys for the data sources

Two adapters call a public API that allows more with a key. Neither key is a GitHub secret: each
is a `SecureString` in SSM Parameter Store, which the Lambdas read once per cold start. Terraform
does not create the parameter (its value would end up in the state file); it only lets the
Lambdas read that one name. Without a key the adapter uses the keyless API, and a key that is
rejected or rate-limited at run time falls back to it. Never put a key in a topic's
`adapter_config`: that is stored in DynamoDB.

| Parameter | For | Without it |
|---|---|---|
| `/<prefix>/dev/coingecko-api-key` | The crypto adapter. A free key will do: <https://www.coingecko.com/en/api>. | The keyless public API, which is throttled often enough to lose some research ticks. |
| `/<prefix>/production/coingecko-api-key` | The same, for production. | The same. |
| `/<prefix>/dev/github-api-token` | The GitHub adapter, which calls GitHub's REST Search API. A fine-grained personal access token with **no permissions** is enough: it reads public data only. | Unauthenticated search: 10 requests a minute, shared with whatever else uses the same Lambda address. With a token, 30 a minute on its own budget. |
| `/<prefix>/production/github-api-token` | The same, for production. | The same. |

`<prefix>` is your `UNIQUE_NAME_PREFIX`. With the default it is `bloggerbear`, so dev's CoinGecko
parameter is `/bloggerbear/dev/coingecko-api-key`; put your own prefix in the commands below.

```bash
aws ssm put-parameter --name /bloggerbear/dev/coingecko-api-key --type SecureString --overwrite \
  --region ap-southeast-2 --value YOUR_COINGECKO_API_KEY
aws ssm put-parameter --name /bloggerbear/dev/github-api-token --type SecureString --overwrite \
  --region ap-southeast-2 --value YOUR_GITHUB_TOKEN
```

Typed like that, the key stays in your shell's history. To avoid it, create the parameter in the
AWS console instead: Systems Manager → Parameter Store → Create parameter, type `SecureString`,
with the name above. In Git Bash on Windows, put `MSYS_NO_PATHCONV=1` in front of the command, or
the name is rewritten as a file path. A paid (Pro) CoinGecko key also needs
`coingecko_api_plan = "pro"` in that environment's `terraform.tfvars`. A new key is picked up the
next time the Lambdas start cold; a deploy forces that.

**CoinGecko's terms require the site to credit them, with or without a key.** It does, with
"Powered by CoinGecko API" under each crypto article and topic title and on the About page.

### The model registry: seeding and rotation

A fresh deployment works with nothing set: every call uses the model Terraform gives the Lambdas
(`BEDROCK_MODEL_ID`, by default the AU Claude Haiku 4.5 inference profile), priced from a built-in
entry for that one model. Set up the registry when you want to change the model without a deploy,
have a fallback, rotate between models, or get costs right for a model that has no built-in price.

Which model a call uses, first match wins:

1. the topic's rotation list (`model_id_candidates`): one is picked at random per run, so every
   call that goes into one article uses the same model;
2. the topic's own `model_id`;
3. the global default, the `default` row of the `<prefix>-<env>-model-config` table;
4. `BEDROCK_MODEL_ID`, from Terraform.

The fallback model resolves separately (topic, then global). When a call to the chosen model
fails, it is retried once with the fallback.

**1. Register each model, with its price.** This is what the Stats page and each article's cost
are worked out from. A model with no registry row and no built-in price is recorded as unpriced,
never guessed at. Prices are USD per 1,000 tokens; confirm them against the Bedrock price page for
your region, because a geographic inference profile can cost more than the provider's list price.

```bash
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.001 --output-price 0.005
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --display-name "Claude Sonnet 4.5" --provider anthropic \
    --input-price 0.003 --output-price 0.015
python scripts/admin_cli.py models list
```

**2. Set the global default and fallback.**

```bash
python scripts/admin_cli.py model-config set \
    --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --fallback-model-id "au.anthropic.claude-sonnet-4-5-20250929-v1:0"
python scripts/admin_cli.py model-config get
```

That writes one item to the `<prefix>-dev-model-config` table (`<prefix>-production-model-config`
in production; with the default prefix, `bloggerbear-dev-model-config`). For `ap-southeast-2`, the item looks like this, and you can create or edit it
straight in the DynamoDB console instead:

```json
{
  "config_id": { "S": "default" },
  "model_id": { "S": "au.anthropic.claude-haiku-4-5-20251001-v1:0" },
  "fallback_model_id": { "S": "au.anthropic.claude-sonnet-4-5-20250929-v1:0" }
}
```

The same table holds other items: `config_id` = `pipeline` for the pipeline-wide settings
(`pipeline-config set`), and the model catalog's `model-catalog`, `model-catalog-state` and
`pricing-run#<id>` (step 4). Leave them alone here; none overwrites another.

**3. Rotate between models on a topic (optional).** Useful for comparing models on the same
topic: each article records which model wrote it.

```bash
python scripts/admin_cli.py topics update github-trending \
    --model-candidates "au.anthropic.claude-haiku-4-5-20251001-v1:0,au.anthropic.claude-sonnet-4-5-20250929-v1:0" \
    --fallback-model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"
python scripts/admin_cli.py topics update github-trending --model-candidates ""    # stop rotating
```

Before you add a model anywhere:

- **Check the id exists in your region.** `aws bedrock list-inference-profiles --region
  ap-southeast-2` lists the profiles on offer. In Sydney, Anthropic's models are reached through
  an inference profile (the `au.` or `apac.` prefix), not by the bare model id. The Sonnet id
  above is an example: use what the list shows.
- **Check your account can call it**, with the `aws bedrock-runtime converse` command under
  [Before you start](#before-you-start). Nothing validates an id when you save it: a wrong one
  shows up as a failed run (and a fallback, if you set one).
- Any provider's model works. Every call goes through Bedrock's Converse API.
- The registry is per environment. Do it again for production.

**4. Let the model catalog keep the registry current (on by default).** Nothing to set up: once
deployed, the `model-catalog` Lambda adds every model the account can call in this region, from
every provider, to the registry **switched off** (Tuesdays 03:00 Sydney), and reprices every model
(Wednesdays 03:00 Sydney). A changed price is used at once and checked 1 and 2 hours later; one a
check cannot confirm goes back to the old price, shows in `failed-executions list` and in the
assistant's pipeline health. A model you registered by hand above is repriced too: add
`--lock-price` to `models add` to keep a price you chose. To fill the registry now rather than
waiting for Tuesday, and to check it:

```bash
python scripts/admin_cli.py models discover          # starts in the background; a minute or two
python scripts/admin_cli.py model-catalog get        # what it added and priced, any run being checked
python scripts/admin_cli.py models list
python scripts/admin_cli.py models add --model-id "<a discovered id>" --display-name "<name>" \
    --provider <provider> --input-price <usd per 1k> --output-price <usd per 1k>   # switch one on
python scripts/admin_cli.py model-catalog set --reprice off   # pause a job ('' or on resumes it)
```

The pause flags are the only settings: `discover_enabled` and `reprice_enabled` on the
`model-catalog` item of the same table, on unless set to `false`. `MODEL_CATALOG_FUNCTION_NAME`
is not one of them: it only tells the admin API which Lambda to start, and Terraform sets it
([configuration.md](configuration.md#the-model-catalogs-environment-variables)). Details:
[model-catalog-refresh.md](enhancements/model-catalog-refresh.md).

### DynamoDB tag-based access control (ABAC)

The operator's assistant has a `table_sample` tool ("what is in this table?"). Its permission to
read a table is written as tag conditions: a table is readable only if it is tagged
`ManagedBy = Terraform`, this deployment's `Project` and an `Environment` that assistant may
read. `Project` is `BloggerBear` by default, and is your `UNIQUE_NAME_PREFIX` if you set one
(`Project = acme-blog`), so one deployment's assistant never reads another's tables. Terraform
puts the same tags on every table it makes, so there is nothing to tag by hand.
DynamoDB applies tag conditions only where attribute-based access control is switched on for the
account, and it is an account setting, per region.

- **Check it:** DynamoDB console → **Settings** → **Attribute-based access control**, in the
  region you deploy to. It should say enabled. AWS has it on by default for most accounts.
- **If it is off, switch it on there.** Until then `table_sample` refuses every table and says
  AWS refused. Nothing else is affected: the tables the assistant reads by name stay readable.
- **Two accounts:** check both. **One account:** one setting covers dev and production, as long
  as they share a region.
- **Terraform does not manage it.** It is an account setting changed in the console; we know of
  no Terraform resource for it, and nothing in this repository checks it at deploy time.


### CloudWatch Logs tags (the assistant's log tools)

The operator's assistant reads logs itself (`log_review`, `api_errors`; [design](enhancements/archive/ops-assistant-log-reader.md)).
Its permission is written like `table_sample`'s: `logs:StartQuery` on `/aws/lambda/<prefix>-<env>-*`
and `/aws/apigateway/<prefix>-<env>-*` only when the log group is tagged `ManagedBy = Terraform`,
this deployment's `Project`, and an `Environment` that assistant may read (dev: `dev`;
production: `production` and `shared`). Unlike DynamoDB there is no account setting to switch on.
With vision on ([Vision (optional)](#vision-optional)), it may also query the worker's log group,
`/aws/lambda/<prefix>-<env>-vision-worker` in `vision_region`, under the same tags with
`Environment` exactly this environment, and read each vision topic's running state,
`snapshots/*/running-state.json` in the content bucket (`infra/modules/ops-assistant/vision.tf`).
No manual step: the same apply that turns the worker on grants it.

- **Check it** after the first apply: ask the assistant "any errors in the logs?". It answers with
  counts, or "I found no errors", for every function in the home Region, and for the vision worker
  in `vision_region` where vision is on.
- **If it says AWS refused**, or names a log group it was not allowed to read: that group is
  missing a tag. Usually a Lambda wrote its first log before Terraform made the group, so the group
  is not Terraform's: delete the empty group and apply again, or import it. CloudWatch console →
  Log groups → the group → **Tags** shows what it has.
- **Nothing to tag by hand**: every log group Terraform makes carries the provider's default tags.
### The personal-data and secret checks

`pr-checks.yml` runs Gitleaks (secrets, email addresses and AWS account IDs, per
`.gitleaks.toml`) and `scripts/pii_denylist_check.py` (your own exact personal strings) on every
pull request. Those run after a push, when the content is already on GitHub, so run the same
checks before each commit too:

```bash
git config core.hooksPath .githooks          # once per clone: enables .githooks/pre-commit
winget install Gitleaks.Gitleaks              # or brew install gitleaks / your package manager
```

Then list your own personal strings (a name, a home IP, a personal address), one per line, in
`.pii-denylist` at the repo root. It is gitignored, and the hook refuses to commit it. For CI, put
the same list in the `PII_DENYLIST` repository secret. Neither check ever prints what it found, so
the logs stay safe to publish.

### Vision (optional)

The vision worker, for the `satellite_vision` and `rail_access` topics
([how it all fits](architecture/blogger-vision.md); the design is
[enhancements/vision.md](enhancements/archive/vision.md)). **Off by default**, in both environments:
nothing of it is created until you turn it on, and you turn it on per environment with a GitHub
variable, not with a tfvars edit. Every setting it uses is in
[configuration.md](configuration.md#vision-and-rail-access).

1. **Re-apply the bootstrap by hand, first** (it is never applied by CI). It adds two statements
   to the deploy roles: Lambda functions and log groups named `<prefix>-*-vision-*` in
   `vision_region` (default `us-west-2`, beside the Sentinel-2 imagery). Pass the same other
   variables as your first apply, and check the plan touches only the roles' policies. If you
   change `vision_region`, pass the same value to the bootstrap and to the environment. Until
   this is done, an apply with the worker turned on is refused.
2. **Set the `VISION_ENABLED` repository variable to `true`** (GitHub → Settings → Secrets and
   variables → Actions → Variables; [configuration.md](configuration.md)) and run a deploy: merge
   a change under `infra/`, `lambdas/` or `frontend/` to `dev` (the Terraform workflow's paths
   filter starts no apply for a docs-only merge), or `workflow_dispatch` the Terraform workflow
   from the `dev` branch. Nothing in `terraform.tfvars` changes (a `vision_enabled` there would
   beat the variable). The apply creates, in
   `vision_region`: the worker (arm64, python3.12), its role (its own log group only), its log
   group, and a small artifacts bucket its package is uploaded through. In the home region it
   gives the research tick the worker's ARN (`VISION_WORKER_ARN`), and gives the right to invoke
   that one function to the pipeline's shared execution role (`lambda_exec`), which twelve
   functions use: the research tick, the daily cycle, the admin API and nine more. Both apply
   workflows read the variable as a switch: `true`, in any letter case (GitHub Actions compares
   strings ignoring case, so `TRUE` and `True` count too), turns the worker on, and any other
   value, or unset, is read as off. This also turns the worker on in production at the next
   release: `terraform-production-release.yml` runs in `environment: production` and reads
   `vars.VISION_ENABLED`, which falls back to the repository's value. To keep production off until
   the dev trial is done, set `VISION_ENABLED` to `false` on the `production` environment. A
   value on the `production` environment wins over the repository's, for production alone.
3. **Create a topic.** A `satellite_vision` topic needs `adapter: "satellite_vision"` and
   `sites` in its `adapter_config` (one to ten lon/lat polygons, each with an `id` and
   optionally a `name`), and usually an `object_noun`; everything else has a default
   (`common/adapters/satellite_vision.py` documents every key); a topic without
   `editorial_goals` is drafted under the adapter's default goal (`ADAPTER_DEFAULTS` in
   `common/editorial_resolver.py`, #298). With the default 60 s `time_budget_seconds`, a tick
   asks the worker only while 45 s of the budget remain, so in about its first 15 s: only the
   sites whose new scene is found by then are measured, how many depending on the search's and
   the worker's times (often fewer than `max_sites_per_tick`); the rest rotate to later ticks and
   keep their place. A `rail_access` topic watches up to eight cities, each inside one Sentinel-2
   tile, and writes about one of them a day: [`rail_access` topics](#rail_access-topics), below,
   creates one topic over the four starting cities from a file. Both get `force_manual_review`
   whether you ask or not:
   every article waits for `admin_cli approve`. Trigger the first tick by hand
   (`topics trigger <id> --pipeline research_tick`); the first scene measured at or above the
   topic's coverage floor is its first observation, reported once, so a Finding, then a held
   article, is what success looks like. The CLI waits only 30 s for the Finding, and a tick that
   measures a scene can run longer (up to 58 s of it is the worker, inside the function's 120 s),
   so "No new Finding after 30s" is expected here: wait about two minutes, or until
   `topics findings <id>` shows the Finding, before you trigger the daily cycle. A tick that
   measures nothing (the worker off, every site in `last_error`), or only a scene under the floor
   (mostly cloud), writes no Finding.
4. **To turn it off again:** set the variable to `false` (any value other than `true` in any
   letter case), or remove it (either is read as off; on the `production` environment, removing
   it falls back to the repository's value, so set it to `false` there instead), and run a
   deploy. The next apply removes the worker, its bucket, its log group and the invoke right;
   topics keep their state and their history, and record `last_error` on each site until the
   worker is back. Disable or delete the topics if you do not want those errors.

The worker reads public data over HTTPS (Sentinel-2 from the `sentinel-cogs` bucket, nothing
else) and needs no key and no other access. It has no alarm of its own: observability's alarms
are in the home Region, and the worker is not; read its log group,
`/aws/lambda/<prefix>-<env>-vision-worker`, in `vision_region`. The ops assistant reads it there
(`log_review`, "any errors in the vision worker?"), and each topic's running state (`vision_status`,
"how is the vision pipeline doing?"), granted by the same apply: nothing to do by hand
([ops-assistant-vision.md](enhancements/archive/ops-assistant-vision.md)). After turning vision on, ask it
"is the satellite worker on?": it should say on, and in which Region.

### `rail_access` topics

The Rail Access Monitor is one topic that watches several cities and writes about one of them a
day, by a fixed weekly rota ([what it measures](architecture/blogger-vision.md#part-two-the-trains-explainer);
every key and default: [configuration.md](configuration.md#a-rail_access-topic); the design:
[dailysubject_hook.md](enhancements/archive/dailysubject_hook.md)). Turn the worker on first (above):
without it each tick records `last_error` on the city and writes nothing.

This section follows the example's rota, a city a day. Dev's `rail-cities` has since moved to a
city a week (`rotation_period` `"week"`), its Monday weekly reading written up from three angles in
turn: [RailVisionSeeding.md §7](RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides).

**Create the topic.** `scripts/rail_topics.example.json` holds one complete admin API topic body,
`rail-cities`: the four starting cities (Sydney, Melbourne, Singapore and Boston, each with its box
from [rail-access-monitor.md §2](enhancements/archive/rail-access-monitor.md#2-the-angle-rail-access-not-rail-yards)),
`reach_m` 1000, the 80 s `time_budget_seconds` cap (see "A large city's first network can take a
minute", below), the rota (`rotation`, read in `rotation_timezone`), an editorial goal, a daily run
every day and a findings window of 192 hours (`findings_window_hours`). `topics create` takes a
topic as flags, not as a file, so read each field out of it, from the repository root:

```bash
field() { python -c 'import json, sys; v = json.load(open("scripts/rail_topics.example.json"))["topics"][0][sys.argv[1]]; print(v if isinstance(v, str) else json.dumps(v))' "$1"; }
python scripts/admin_cli.py topics create --topic-id "$(field topic_id)" --name "$(field name)" \
    --adapter rail_access --config-json "$(field adapter_config)" \
    --editorial-goals-json "$(field editorial_goals)" \
    --goal-rotation-json "$(field editorial_goal_rotation)" \
    --daily-cadence "$(field daily_cadence)" --daily-timezone "$(field daily_timezone)" \
    --findings-window-hours "$(field findings_window_hours)"
python scripts/admin_cli.py topics get rail-cities
```

The answer shows `"force_manual_review": true`, which was not sent: the admin API forces it for
this adapter. The example's editorial goal names no city: it asks for the measured figures of "the
day's city", the capture date and the coverage, with reach to a stop as the same straight-line
proxy as reach to a station and minutes as that distance at 80 m a minute; it calls every
simulated link theoretical, justified by its numbers and by what its straight line crosses; and
it rules out ridership, journey time, cost, recommendations, causes and any complaint, political
or resource-allocation view. Its `editorial_goal_rotation` carries the four angles (access,
network, simulations, transit) taken in turn on a city's Findings
([RailVisionSeeding.md §3 and §7](RailVisionSeeding.md#3-the-editorial-goal)); each day's mandate
names the city (below). A rail topic created without a goal is drafted
under the adapter's own (`ADAPTER_DEFAULTS` in `lambdas/common/editorial_resolver.py`, #298), which
restates the summary prompt's limits (only what was measured, with the capture dates; no
ridership, journey time, cost, cause, forecast or recommendation). A topic's own goal wins over it.

**The first-deploy check: one tick per city.** Trigger `topics trigger rail-cities --pipeline
research_tick` once per city, a few minutes apart, and once more for each city whose first tick
only fetched its network: each tick works on the city whose turn it is (one never attempted first,
in the config's order: Sydney, Melbourne, Singapore, Boston), and a city whose first network took
the time it had to measure keeps its place and is measured on the next tick ("A large city's first
network can take a minute", below), so four to eight ticks cover the four. Then the ops assistant's
vision status (or the running state, "Where things land") should show each city with a network
fetched, and `topics findings rail-cities` a first-observation Finding for each city that had a
clear scene, each listing its city in `subjects`.

**How the cadence works.** Like every topic, `rail-cities` has two schedules. The research
heartbeat (`rate(1 hour)` by default) asks whether the topic is due (its `research_interval_hours`,
else the pipeline-wide interval) and, when it is, the adapter takes the cities in order (the one
never attempted first, else the one attempted longest ago) and each tick looks at the cities in that
order until it has measured one new scene (`max_sites_per_tick` 1 caps measured scenes, not
cities looked at). For each it searches Earth Search for the newest scene over the whole city in
the last 20 days. Sentinel-2 passes every few days, so most visits find a scene the city already
has: nothing is measured, no model is called, the tick may instead refresh a network past its
30-day TTL, and it goes on to the next city. A new scene is measured; only a material change (or
the first observation) makes a Finding, and the Finding lists the city it reports in `subjects`.
The tick never reads the rota: with an hourly heartbeat every city is looked at about every hour,
and at least one is measured when it has a new scene, whatever the day, so its Findings are
waiting when its day comes.

The daily cycle runs on `daily_cadence` in `daily_timezone`, every day after 09:00
Sydney time, at the minute its stagger slot gives it (`topics schedule` shows it), not at the
example's 09:05. At the start of each run it asks the rota whose day it is, on the
calendar of `rotation_timezone` (Sydney's, so "Tuesday" is Tuesday in Sydney): Sydney on Monday and
Friday, Melbourne on Tuesday and Saturday, Singapore on Wednesday and Sunday, Boston on Thursday.
The run writes about that city alone: it drafts the Findings that list the city in `subjects`, made
since the topic's last article **on that city** (`last_article_by_subject` on the topic item, beside
`last_article_at`), reaching back at most `findings_window_hours`
(`lambdas/common/findings_window.py`; 24 hours when it is not set, at most 336). The day's mandate
in its prompts names the city. With no new Finding about the day's city it stops before any model
call (`no_findings`, naming the city) and stamps nothing, and it never borrows another city's
Findings. A Finding that reports two cities (a tick that measured two) is drafted on each one's
day. The log says `daily_cycle_handler: day_subject=<city> topic_id=rail-cities`, and the held
article keeps the city as its `subject`.

The window must cover the longest gap between two of a city's days, plus a day for a clock change
or a late run: 24 hours a day of it. The two-day cities are at most 4 days apart (120 hours would
do), but Boston's Thursday is 7 days from the next, so the example sets 192 hours, 8 days. Four
cities cannot all have two of the seven days; a list rota (`"rotation": ["sydney", "melbourne",
"singapore", "boston"]`) gives each one day in four and needs 120 hours, but no city has a fixed
weekday. The window never reaches back past the city's own last article, so no Finding is drafted
twice: the daily cycle stamps the city and the topic with the run's start time on every article it
writes, published or held for review (every rail article is held). One Finding can still be
missed: the research tick dates a Finding (`captured_at`) when its fetch starts, at the start of
the tick, and writes it at the tick's end, so a Finding whose tick is still running when the daily
cycle starts is not there for that run and, once that run has written an article on its city, is
dated before that city's next window starts. That predates PR K and is a code follow-up
([vision.md §10](enhancements/archive/vision.md#10-still-open)). Findings expire 14 days after they are
made (`FINDING_TTL_DAYS` in `lambdas/research_tick_handler.py`), which is why the window is capped
at 336 hours. Daylight saving does not matter: the rota reads Sydney's calendar, and the window
starts at the city's last article whatever the gap. Re-Write from the review inbox rebuilds a rail
article's sources from the Findings of the article's own city in the same window, looking back
`findings_window_hours` from the article's creation (`_findings_for` in
`lambdas/common/rewrite.py`), and falls back to the whole window, saying so in its log, if none
are left (a city since removed). Rail Findings are few (the first observation, then material
changes), so most runs draft nothing.

**When a run writes nothing.** A run that writes nothing stamps nothing. That covers three cases:
it found no Finding about the day's city; it had no usable angle; or it ended in an error. An
error the handler catches is quiet: the daily cycle returns `"status": "error"`, so Step Functions
sees a successful invoke and does not retry it, and nothing reaches the DLQ or an alarm. The error
is in the execution's output, not in the function's log (the handler's top-level guard prints
nothing, a code follow-up in [vision.md §10](enhancements/archive/vision.md#10-still-open)); the sign is a
city's day with a new Finding but no new item in the review inbox. A broken rota (a slot that is
not a site, a site with no day, a zone that is not one) is such an error on every day, and the
research tick records it on the topic's `last_error` too. A timeout (the function's `timeout` is
300 s) or a crash is not caught: the state machine retries it twice and then sends it to the DLQ
(`admin_cli failed-executions list`), so it is not silent. The city's next day then still starts at
its last article, but never more than 192 hours back. A run that found no Finding loses nothing:
its window was empty, and the city's next run reaches back past it. After either of the other two,
a two-day city still gets everything on its next day (its days are 3 and 4 days apart, so its last
article is at most 7 days old). For Boston, the once-a-week city, or after two such days in a row,
Findings older than 192 hours drop out: for Boston that is about six of the week's seven days, and
for a two-day city after two missed days, two or three days.

After a run that had no usable angle or ended in an error, run the daily cycle again by hand the
same day, before midnight Sydney time: a run writes about the day's city, so the next day's run
would be about another city.

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline daily_cycle
```

It reaches back to the city's last article, up to 192 hours. Leave out `--force`: it ignores both
stamps and drafts the city's whole 192-hour window, so it would draft again the Findings an earlier
article on the city already used (it still keeps to the day's city).

**What the first tick does.** Trigger it by hand rather than wait for the heartbeat:
`python scripts/admin_cli.py topics trigger rail-cities --pipeline research_tick` (a manual tick
ignores the interval). In order: the scene search for the city whose turn it is (Sydney, first in
the config, on a new topic); the city's OpenStreetMap network from the Overpass API, since none is
stored (started when 30 s of the measuring budget remain, and given the rest of the budget less
10 s, both endpoints included: about 70 s at the example's 80 s budget; a failure is `last_error`
`osm` and the city waits for its next turn); then, if 45 s of the budget remain, the worker measures
the newest scene with that network (if the fetch was slow, the measurement is deferred to the
city's next turn, which reads the stored network). If the scene's coverage is at least 0.6, that is
the topic's first observation: a Finding, its summary written by the model from "First observation.
Sydney: served share … %, … stations, … km² beyond 1 km of a station (coverage … %)" (the area
beyond reach printed to one decimal, such as 12.3 km²) and the planner notes, with the figure
attached, `subjects` ["sydney"], and no triage (there is no baseline to check against; a person
reads every article anyway). Each other city's first clear scene is its own first observation,
reported once in the same way ("First observation. Melbourne: …"), so each city's day has an
article to start with; after that a city is reported when it changes. A scene under the floor
writes no Finding, and the city's next clear one is its first observation. All of that takes longer than
the CLI's 30 s wait (`topics trigger` polls for a new Finding for 30 s,
`_RESEARCH_TICK_TIMEOUT_SECONDS` in `scripts/admin_cli.py`; the tick has 120 s), so the CLI
printing "No new Finding after 30s. This can be a legitimate outcome …" does not mean it failed.
Wait about two minutes, or until `python scripts/admin_cli.py topics findings rail-cities` shows
the new Finding. Then `topics trigger rail-cities --pipeline daily_cycle` drafts the article **if
it is that city's day** (on Sydney's days, Monday and Friday, for Sydney's first Finding; on any
other day the run says `no_findings` for the day's city, and Sydney's Finding waits for Sydney's
day). The `force_manual_review` hold keeps the article in the review inbox for `admin_cli approve`,
whatever the compliance review says. A daily cycle started before the Finding is written finds
nothing to draft (run it again); on a later day, with other Findings about the city in its window,
it drafts without the new one, which is then lost ("One Finding can still be missed", above).

**A large city's first network can take a minute.** Overpass answers a big city's rail query
slowly: Sydney's took 54 s from a workstation on 16 October (5,925 elements, 5.5 MB). So the first
fetch gets the tick's measuring time, not the 30 s a refresh of a stored network gets (#312): the
rest of the budget less 10 s, each Overpass hop reading for as long as that leaves and the query
asking the server for the same (`FIRST_FETCH_MARGIN_SECONDS` and `FIRST_FETCH_TIMEOUT_SECONDS` in
`lambdas/common/adapters/rail_access.py`). Such a fetch leaves too little of the budget to measure,
so **the first tick fetches and the next measures**: the first tick's `vision_site` line says
`deferred` for `time_budget`, and the ops assistant's vision status shows the network's
`fetched_at` from that tick; the city's next turn measures it against the stored network, which
then serves for `osm_ttl_days` (a refresh that fails keeps it). The fetch ends by its deadline even
if Overpass goes silent mid-reply: a reply still arriving then is cut, and the tick records a
timeout. The example sets `time_budget_seconds` to the cap, 80, which gives the fetch about 70 s; a
topic created before it did, like dev's `trains-sydney`, does not: read the config with
`topics get <id>`, add `"time_budget_seconds": 80` to the whole `adapter_config` and send all of it
back with `topics update <id> --adapter-config-json '...'` (it replaces the config: see `reach_m`
below). A failed first fetch is tried again on the city's next turn, with no 6-hour wait: nothing is
measured without a network. If the status keeps saying `osm` with "OpenStreetMap timed out" at the
80 s budget (Overpass took 39 to 75 s to start answering for Sydney on 16 October, so a busy hour
can miss), fetch the networks out of band:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline network_prefetch
```

It runs the research tick's function in a mode that only fetches each city's network, in the
config's order, with up to 90 s per Overpass reply and 100 s in all, and stores each in its own
object, `vision/<topic>/<site>/network.json` in the content bucket: the same object the hourly tick
reads and writes, so the next tick that reaches the city measures with it. It writes no Finding, no
running state and no research time, so the CLI does not wait: each city's result is a
`network_prefetch` line in the research-tick log (`stored`, `failed` with the Overpass error, or
`deferred` when the time ran out before the next city): a slow city can leave too little of the
100 s for the next one. A second prefetch starts again at the first city (it skips no stored
network, however fresh), so it can defer the same cities again; seed those with ordinary research
ticks ([RailVisionSeeding.md §4](RailVisionSeeding.md#4-seeding-runsheet-dev)). When a city's
network goes stale (`osm_ttl_days`, 30 by default) and the tick's 30 s refresh keeps failing, the
old network is still measured against; run the prefetch again to replace it. If even the prefetch fails with a timeout, the query
is too big for Overpass to answer: shrink the polygon (it starts a new baseline, and a new network
is fetched for it).

**After deploying the stops layer** (every bus and tram stop in the network, the stop measures and
the transit-reach map): a network stored before it carries no `stops`, so the city's Findings keep
reporting `no_stops_layer`, with no stop measures and no second map, until the network is
refreshed at its TTL (30 days) or replaced. Once the deploy is green, run the prefetch once:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline network_prefetch
```

Each city's `network_prefetch` line carries the Overpass body's size as `bytes` beside its
`counts` (which gain `stops` and `stops_by_mode`); read it against the client's 20 MB cap
(`MAX_RESPONSE_BYTES` in `lambdas/common/osm.py`): a big city's bus stops add a few MB to its rail
query (Sydney's rail reply alone was 5.5 MB), and a city near the cap is a polygon to shrink. A
`deferred` city is seeded by its next research ticks, as above. Then push the goals, whose text
changed with the layer (every simulated link theoretical with what it crosses, minutes as walking
time at 80 m a minute, a constructive register, and the fourth rotating goal, `transit`): write
them out from [RailVisionSeeding.md §3 and §7](RailVisionSeeding.md#3-the-editorial-goal) and send
both in one update, which changes nothing else of the topic:

```bash
python scripts/admin_cli.py topics update rail-cities --goal-rotation-json "$(cat rail_goal_rotation.json)" --editorial-goals-json "$(cat goals.json)"
```

Until the rows are pushed, the adapter's own default mandate and drafting guidance already carry
the framing. The benchmark's committed networks (`scripts/vision_benchmark_networks/*.json`)
predate the layer too and keep working without it (their runs report `no_stops_layer`); refetch
one with `python scripts/vision_benchmark.py fetch-network --scene <name>` on a machine that can
reach Overpass ([scripts/README.md](../scripts/README.md)), which prints the file's counts and
`bytes`.

**Where things land.** In the content bucket, `<prefix>-<env>-content`: the figures as the worker
drew them, the rail map `vision/<topic>/<site>/<scene>.png` (for example
`vision/rail-cities/sydney/S2A_56HLH_20240105_0_L2A.png`) and, once the city's network carries
its stops, the transit-reach map `vision/<topic>/<site>/<scene>-modes.png`; each city's network,
its only copy, `vision/<topic>/<site>/network.json`, written by the tick's fetches and by `network_prefetch` alike
and kept outside `snapshots/` (whose objects the bucket expires after 21 days); the running state,
rewritten every tick, `snapshots/<topic>/running-state.json`; and each Finding's own snapshot,
`snapshots/<topic>/<captured_at>.json`. The running state holds, per city, the history (the newest
entry with its station, flag, suggestion and desert lists), what it knows of the network without
holding it (`network_key`, `network_fetched_at`, the polygon's `network_site_key` and
`network_counts`), and any `last_error`, `network_error` or `failed_scene`; and `first_observed_at`
for the topic, and each city's own `first_observed_at`. When the article is published, each figure
is copied to the site bucket, `<prefix>-<env>-site`, as `articles/figures/<article>/<n>.png`, and
the page shows it under the credits with its caption.

```bash
aws s3 cp s3://<prefix>-dev-content/snapshots/rail-cities/running-state.json - | python -m json.tool | less
aws s3 cp s3://<prefix>-dev-content/vision/rail-cities/sydney/network.json - | python -c 'import json, sys; n = json.load(sys.stdin); print(n["fetched_at"], n["counts"], n.get("bytes"))'
```

**Reading the agent's trail.** From the second clear scene of a city on, a numeric change goes to
the triage agent before it can become a Finding. `topics findings rail-cities` prints the latest
Finding: its `summary` (the model wrote it from a diff that, for a change the agent judged real,
quoted the agent's reason as `the vision agent's note (not a measurement): "…"`), `figures`,
`planner_notes`, `subjects` and `raw_snapshot_s3_key`. That snapshot (and the running state) holds
the whole trail under `triage`, keyed by site id: `verdict`, `reason`, `tool_calls` (each tool's
`name`, its `input` and whether it was `ok`), `steps` (each tool's code-derived result), `stop`,
`model_id`, the tokens and `model_calls`; the newest history entry repeats the verdict and reason.
A change the agent judged an artefact writes no Finding, so its trail is in the site's decision
ring in the running state (the last 12 decisions) and in the logs (the `vision_triage` line, 90
days: [Evidence](#evidence-what-the-pipeline-measured-and-decided), below). The review evidence, which the fresh-data
review checks the draft against, carries each city's measured numbers and planner notes, not the
trail: to see why a change was or was not reported, read `triage`.

**Moving from city topics to `rail-cities`.** A topic made before the day-subject round, like dev's
`trains-sydney` (one city, no rota), keeps working after the deploy: it has no rota, so its daily
cycle drafts every Finding as before, and its first tick moves the network its running state
carried to the city's own object (`vision/trains-sydney/sydney/network.json`) without fetching it
again. To change to one rotating topic, start it fresh rather than migrate state
([dailysubject_hook.md §4.4](enhancements/archive/dailysubject_hook.md#44-migrating-the-four-deployed-topics)):
create `rail-cities` (above), run the first-deploy check (one tick per city), and once each city has
had its first tick, pause the old topic, which stops its ticks and daily runs and keeps its
published articles under its own id:

```bash
python scripts/admin_cli.py topics pause trains-sydney
```

(`topics delete trains-sydney` removes it for good.) Each city in `rail-cities` starts with a first
observation again: the baseline needs one earlier clear scene (`min_baseline` 1), so each city is
back to reporting changes after its second clear scene, a few days. Pause any other city topics
the same way.

**Adding a city.** A new city is a new site in the topic's `sites` and a day for it in the rota:
every site needs a day, and a rota day that names no site is refused. Read the config with
`topics get rail-cities`, add the site (with a new `id`, at most 64 letters, digits, `_` or `-`, its
name and its box) and give it a day, taken from a city that has two (for Friday, `"FRI": "perth"`)
or by changing the rota to a list cycled one a day, then send the whole config back with
`topics update rail-cities --adapter-config-json '...'` (it replaces the config: see `reach_m`
below). Keep `findings_window_hours` at least the longest gap between a city's days plus a day
(`topics update rail-cities --findings-window-hours 192`): 192 hours covers any city that has at
least one day a week. A topic may hold up to eight cities (`MAX_RAIL_SITES`), measured one per tick
in turn; each city's network is in its own object, so the running state holds only the histories
(about 7 MB at eight cities, `MAX_RAIL_SITES`'s comment in
`lambdas/common/adapters/rail_access.py` has the measurements). The new city's box must sit inside
one Sentinel-2 tile, because the scene search keeps only scenes whose footprint holds the whole
site. A tile is a 100 km square of the UTM grid and 9.8 km more to the east and south (Sydney's
56HLH runs from easting 300,000 to 409,800 and northing 6,190,240 to 6,300,040), so a box whose
corners fall in one 100 km square fits. Check it with `lambdas/vision/geo.py` from the repository
root (any Python 3.11 or later; `geo.py` needs only the standard library), putting your box's west,
south, east and north in place of Sydney's:

```bash
python -c 'import sys, math; sys.path.insert(0, "lambdas"); from vision import geo; w, s, e, n = 150.86, -34.16, 151.56, -33.58; z = int((w + e) / 2 + 180) // 6 + 1; c = [geo.lonlat_to_utm(x, y, z, s < 0) for x in (w, e) for y in (s, n)]; xs, ys = [p[0] for p in c], [p[1] for p in c]; print("zone", z, "S" if s < 0 else "N", "| 100 km squares", sorted({(int(x // 1e5), int(y // 1e5)) for x, y in c}), "| margin km", round(min(min(xs) % 1e5, 1e5 - max(xs) % 1e5, min(ys) % 1e5, 1e5 - max(ys) % 1e5) / 1e3, 1), "| window px", math.ceil(max(xs) / 20) - math.floor(min(xs) / 20) + 32, "x", math.ceil(max(ys) / 20) - math.floor(min(ys) / 20) + 32)'
```

For Sydney it prints `zone 56 S | 100 km squares [(3, 62)] | margin km 1.4 | window px 3327 x 3306`.
Three things to read: **one** square in the list (two means the box straddles a grid line: move
or shrink it); a margin of a kilometre or more from the square's nearest edge (the four boxes have
1.0 to 3.0); and a window of at most 3,584 px a side (the worker's `RAIL_MAX_WINDOW_PX`, 71.7 km
at 20 m with the 16 px pad), or the worker refuses the city as `window_too_large`. A scene refused
as `window_too_large`, `site_outside_scene` or `bad_request` is recorded in the running state as
`failed_scene` (`id`, `code`, `at`, `site_key`, `params`) and not sent again until a newer scene
appears, 6 hours pass, or the site's polygon or parameters change, so a corrected box is sent on
the next tick; `unreadable_scene` and any other failure are retried on the next tick. Some passes
cover only part of a tile; the search skips those and waits for one that holds the whole city.

`reach_m` is the walking reach of a station in a straight line, 200 to 5000 m. Keep 1000 (about a
12-minute walk) unless there is a reason: the served share and the deserts are only comparable
between cities at the same reach, and a larger reach describes stations people drive or ride a
bus to. Changing it, or a polygon, on a running topic starts a new baseline: the next scene
reports "baseline still building" rather than a change. Either is changed with
`topics update <id> --adapter-config-json '...'`, which replaces the whole `adapter_config`, not
the keys it names: read it with `topics get <id>`, edit it, and send all of it back, keeping each
city's `sites` entry with the same `id` (and polygon, unless that is what changes) and the
`rotation`. Sent with only the changed key, the config loses `sites` and every tick fails until it
is whole again; any other key left out falls back to its default (a lost `rotation` makes the topic
write about every city every day); a city under a new `id` starts with no history.

### Evidence: what the pipeline measured and decided

Every step the vision pipeline takes leaves one JSON line in the logs, and every decision an
entry in the site's decision ring in the running state ([the evidence
trail](enhancements/archive/vision-evidence.md)). The lines are read in the CloudWatch console (Logs →
Logs Insights, pick the Region, the log group and a window) with the fixed queries below, which
filter by field equality: a query on one topic id never matches another that it prefixes (a
`rail-sydney` beside `rail-sydney-demo`). The operator's assistant and Alexa+ read the same lines
with `vision_evidence` ([Asking the assistant](enhancements/archive/vision-evidence.md#asking-the-assistant)).

- (a), (b), (c), (c2) and (f) run on `/aws/lambda/<prefix>-<env>-research-tick` in the home
  Region;
- (d) and (g) on `/aws/lambda/<prefix>-<env>-vision-worker` in `us-west-2` (the vision Region);
- (e) on `/aws/lambda/<prefix>-<env>-admin-api` in the home Region.

```
(a) A topic's decisions (one topic, by equality: rail-cities never matches rail-sydney-demo)
fields @timestamp, site_id, scene_id, code, moved, network_changed, verdict, reported, coverage, tick, run
| filter evidence = "bloggerbear/1" and event = "vision_decision" and topic_id = "rail-cities"
| sort @timestamp desc | limit 50

(b) What the agent did
fields @timestamp, topic_id, site_id, scene_id, verdict, stop, tools, look_again, steps_total, elapsed_ms, cost_usd
| filter evidence = "bloggerbear/1" and event = "vision_triage"
| sort @timestamp desc | limit 50

(c) One tick, end to end, with Lambda's own lines (paste its tick id)
fields @timestamp, event, run, site_id, outcome, code, stored, worker_request_ids, worker_request_id, @message
| filter tick = "<tick id>" or @requestId = "<tick id>"
| sort @timestamp asc

(c2) The agent's whole trail for that tick (read the JSON; do not name flattened step fields)
fields @timestamp, @message
| filter evidence = "bloggerbear/1" and event = "vision_triage" and tick = "<tick id>"

(d) The worker, by build and CPU (us-west-2; evaluation and benchmark calls left out)
filter evidence = "bloggerbear/1" and event = "vision_measure" and site_id not like /^(eval|bench)-/
| stats count(*) as runs, pct(total_ms, 50) as p50_ms, pct(total_ms, 95) as p95_ms,
        max(total_ms) as max_ms, max(rss_peak_mb) as peak_mb by backend, build, cpu, ok, code

(e) What people decided (the admin-api group)
fields @timestamp, event, topic_id, article_id, queue_id, decision, from_status, action, config_keys
| filter evidence = "bloggerbear/1" and event in ["moderation_decision", "topic_changed"]
| sort @timestamp desc

(f) Ticks by outcome, and the attempts that timed out (two queries on the research-tick group)
filter evidence = "bloggerbear/1" and event = "research_tick"
| stats count(*) by topic_id, outcome, stored

filter @type = "REPORT"
| stats count(*) as runs, sum(strcontains(@message, "Status: timeout")) as timeouts

(g) The worker's runs as Lambda saw them (us-west-2): the ones killed before they could log
filter @type = "REPORT"
| stats count(*) as runs, sum(strcontains(@message, "Status: timeout")) as timeouts,
        max(@maxMemoryUsed) as max_memory_bytes, pct(@duration, 95) as p95_ms
```

Reading a line: `event` says the step, `tick` the research tick's Lambda request id and `run`
the attempt (a tick Lambda retried after a timeout keeps its request id; `run` is new each time).
A decision is **kept** when its tick's `research_tick` line says `stored: true`; a tick that
failed after deciding says `stored: false`, a tick that timed out has no `research_tick` line at
all, and in both cases the scene is measured and decided again on the next tick. A decision whose
tick's line carries `finding` equal to its `at` wrote a Finding. The worker's lines carry no topic
id: join them to a tick through `worker_request_id` (c), or read them by site id (d), which is why
the demo topic's site is `demo-sydney`, not `sydney`.

**The first-deploy check**, once, on dev, right after the deploy that carries this:

1. Force a tick (`python scripts/admin_cli.py topics trigger rail-cities --pipeline
   research_tick`) and run (c) with its tick id (the `START RequestId:` line of the invocation,
   or the `tick` field of any evidence line in the window). Every line of the tick must come
   back with Lambda's own `START`, `END` and `REPORT` lines.
2. Take a `worker_request_id` from the `vision_site` line. In `us-west-2`, on the worker's group,
   run `filter @requestId = "<id>" or request_id = "<id>"`: the worker's `vision_measure` line
   and its `REPORT` line must both come back.
3. Check that (a) discovers its fields (the console offers `site_id`, `code`, `moved` in the
   fields pane), and that one line of each event parses. Paste one line into the parser, from
   the repository root:

   ```bash
   echo '<one evidence line, as the console shows it>' | python -c "import sys; sys.path.insert(0, 'lambdas'); from common import evidence; print(evidence.parse(sys.stdin.readline()))"
   ```

   The parser prints the line's fields as a dict, or `None` for a line it does not take. In the
   same window, confirm that a multi-line print (any `Traceback`) came through as several
   CloudWatch events, one per line, which is what the Text log format does and what the
   evidence writer's single-line ASCII relies on.
4. **The retry key.** Look in the research-tick group for two `START RequestId: <id>` lines with
   the same id (a tick Lambda retried after a timeout). If a retried attempt ever shows a new id,
   `run` is still the attempt key and nothing changes; record which it is here.

**The demo topic's two-tick procedure.** A city's second clear scene can come days after the
deploy, and the agent runs only when the numbers move. The demo topic makes the first live
triage happen on a known day: thresholds of 0 send its second clear scene to the agent whatever
the numbers did. The report and the video disclose it as such, beside its thresholds, and its
articles are rejected in the inbox, never published.

1. **Pick two scenes.** List the Sydney tile's (56HLH) items of the last 30 days with each
   item's STAC cloud cover and its coverage over the Sydney polygon. Choose **A**, an older item
   with site coverage at least 0.95; **B**, the newest item with STAC cloud cover at most 40 that
   covers the site whole, with site coverage at least 0.6 (and B is not A); and a cloud cover
   **X** with `A.cloud <= X <` the cloud cover of every item newer than A that covers the site
   whole, so that the newest item at cloud cover X or less is A (the scene search sorts newest
   first and keeps the first that covers the site whole). If no such pair exists in the last 30
   days, use the evaluation's replay trace and say so.
2. **Create the topic** `rail-sydney-demo`, named "Rail access: Sydney (demo)" (so every spoken
   count says which topic it is), with the Sydney polygon but **site id `demo-sydney`** (so the
   worker's lines, which carry no topic id, are told apart from the city's),
   `served_share_threshold: 0`, `desert_relative_threshold: 0`, `min_baseline: 1`,
   `max_cloud_cover: X`, `lookback_days` large enough to reach A, and Sydney's cadence.
3. **Tick 1.** Force a tick. A is measured; it is the topic's first observation (a Finding, then
   a held article: reject it).
4. **Tick 2.** `python scripts/admin_cli.py topics update rail-sydney-demo --adapter-config-json
   '<the same config with max_cloud_cover 40 and lookback_days 20>'` (`adapter_config` is a
   whole-block replace: send all of it), then force a tick. B is not in the site's history, so
   it is measured; with both thresholds at 0 the verdict is material and goes to the agent.
5. **Check** with (b) and (c), and with the assistant's `vision_evidence` once it is live.
6. **Afterwards**, once its articles are rejected and the trace is captured, pause the demo topic
   (`topics pause rail-sydney-demo`): every later clear scene would otherwise cost a worker run
   and a Bedrock triage, measuring Sydney's scenes a second time. Delete it (`topics delete`) only
   after the last export.

### The COOL backend and the benchmark

The COOL session has its own runsheet: **[COOL benchmark runsheet](cool-benchmark-runsheet.md)**.

What it is for: the competition's COOL award scores COOL, OpenCV's Graviton4-tuned build ("Cloud
Optimized OpenCV for AWS Graviton4" on AWS Marketplace). The runsheet is the by-hand session that
shows COOL running this project's real workload: on one Graviton4 instance in us-west-2 it times
the vision worker's own code under COOL and under stock OpenCV 5, on the same Sydney scenes and
network, checks that both give the same rail metrics, and pushes the run files that prove which
build ran and how fast. Nothing in it is in Terraform, and the deployed worker keeps running stock
OpenCV 5 on Lambda.

### The evaluation's real-data runs

The evaluation's subcommands (`scripts/vision_eval.py`; options in
[scripts/README.md](../scripts/README.md#the-evaluation-proper-vision_evalpy-replay-failures-access-baseline-index))
run in CI on a synthetic city with scripted parts. These are the runs on real data: the
operator's, and their run files are the evidence the report cites
([the evidence README](hackathon/evidence/README.md#running-the-evaluation-for-real)). Run them
from a checkout of the code being evaluated, in a venv; they use the committed Sydney network
fixture (`scripts/vision_benchmark_networks/sydney.json`, #349) unless given
`--network <file>`. They read the pinned scenes from `sentinel-cogs` in us-west-2 through a cache
that the first run fills (about 54 MB a scene, eight scenes in all) and later runs read from; on the
Graviton instance (step 8's `$C` in the [COOL benchmark runsheet](cool-benchmark-runsheet.md)) or any machine in us-west-2 that is fast, elsewhere it is a few
minutes a scene through a slow link. Nothing here writes to AWS.

1. **The venv and the names.**

   ```sh
   python3.11 -m venv .venv && . .venv/bin/activate
   pip install -r lambdas/requirements-dev.txt -r scripts/requirements.txt
   E=docs/hackathon/evidence/runs
   C="--cache $HOME/cogs"
   ```

   ```powershell
   py -3.11 -m venv .venv; .\.venv\Scripts\Activate.ps1
   pip install -r lambdas\requirements-dev.txt -r scripts\requirements.txt
   ```

   `lambdas/requirements-dev.txt` is what CI installs: the Lambda and worker requirements, plus
   pytest and moto, which `failures` needs to check that pytest collects the catalogue's tests
   (without them it refuses rather than call the tests missing). The PowerShell lines below run
   `python`, the activated venv's own: `py -3.11` would run the global 3.11, without the venv's
   packages. They spell `--cache $HOME\cogs` and `--out docs\hackathon\evidence\runs` out in full.
2. **The failure catalogue (2.5)**: every test it names is checked to be collected, then the real
   rows run through the worker handler in this process (the cloudy and no-data scenes, the smoke
   scene, a site off the tile, a window past the rail cap, a missing key; six scenes read).

   ```sh
   python scripts/vision_eval.py failures $C --out $E
   ```

   ```powershell
   python scripts\vision_eval.py failures --cache $HOME\cogs --out docs\hackathon\evidence\runs
   ```

   It exits 1 when a row did not do what the catalogue says, and the run file says which and how.
3. **E5, the access baseline**, on `sydney-2024-08-26`:

   ```sh
   python scripts/vision_eval.py access-baseline $C --out $E
   ```

   ```powershell
   python scripts\vision_eval.py access-baseline --cache $HOME\cogs --out docs\hackathon\evidence\runs
   ```
4. **The replay with the deployed worker and the real model (2.6.4).** Credentials that may invoke
   the worker (`lambda:InvokeFunction` on `<prefix>-dev-vision-worker` in us-west-2) and the
   topics' model (`bedrock:InvokeModel` on the inference profile the topics use, in the home
   Region); `M` is that profile's id (`python scripts/admin_cli.py model-config get` shows it).
   An application inference profile is given by its ARN, which Converse needs; the run file and
   the trace then name no model, because that ARN carries the account id (a system profile's ARN
   is recorded as its id).
   Every site id the worker is sent is `eval-sydney`, so these runs never pass for production's in
   its logs; the run file names the worker's Region and code, never its ARN. About 10 worker calls
   and a few cents of model calls a sequence.

   ```sh
   W=$(aws lambda get-function --function-name <prefix>-dev-vision-worker --region us-west-2 --query Configuration.FunctionArn --output text)
   M=<the topics' inference profile id>
   for s in sydney-demo sydney-winter sydney-c1; do
     python scripts/vision_eval.py replay --sequence $s --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --out $E --trace
   done
   ```

   ```powershell
   $W = aws lambda get-function --function-name <prefix>-dev-vision-worker --region us-west-2 --query Configuration.FunctionArn --output text
   $M = "<the topics' inference profile id>"
   foreach ($s in "sydney-demo", "sydney-winter", "sydney-c1") {
     python scripts\vision_eval.py replay --sequence $s --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --out docs\hackathon\evidence\runs --trace
   }
   ```

   Each writes its run file and figures to `$E` and its trace to
   `docs/hackathon/evidence/replay/<sequence>.md`. **sydney-demo is the headline A2 trace**: with
   the demo topic's thresholds of 0, every scene after the first goes to the agent, on the same
   network fixture throughout, so whatever moved came from the imagery; its trace says the
   thresholds are 0. sydney-c1's last tick adds a constructed five-station line and a constructed
   news item, and its trace says so.
5. **Re-run each recording without AWS**, the way a judge will (2.6.4's assertions: every
   measurement recomputed in this process, held to the recording within 2.7.1's tolerances):

   ```sh
   for s in sydney-demo sydney-winter sydney-c1; do
     python scripts/vision_eval.py replay --sequence $s --agent recorded --from "$(ls $E/*-eval-replay-*-$s-bedrock.json | tail -n 1)" $C --out $E
   done
   ```

   ```powershell
   foreach ($s in "sydney-demo", "sydney-winter", "sydney-c1") {
     $from = (Get-ChildItem "docs\hackathon\evidence\runs\*-eval-replay-*-$s-bedrock.json" | Select-Object -Last 1).FullName
     python scripts\vision_eval.py replay --sequence $s --agent recorded --from $from --cache $HOME\cogs --out docs\hackathon\evidence\runs
   }
   ```

   Each exits 0 when it reproduced its recording; a failed check names the tick and the field. A
   recording refused before it runs ("input changed", or a recorded response that calls a tool the
   profile no longer offers) names what moved.
6. **The agent's evaluation (2.6.1-2.6.3)**, with the same `W` and `M`: the thirteen cases in
   `scripts/vision_eval_cases/` (the [evidence README](hackathon/evidence/README.md#the-agents-cases)
   lists them), each case's history through the real adapter, then its newest tick asked of the
   real model five times from the same state, against the deployed worker. C7 and C9 run their
   scripts; C8 is asked with an invalid model id on purpose; C3 and C10 count only if their scene
   passes the gate. About 55 live runs of up to five turns: a few US dollars of model calls (the
   run file totals them in USD and AUD, by the built-in price table) and cents of worker time.

   ```sh
   python scripts/vision_eval.py agent --worker "$W" --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
   ```

   ```powershell
   python scripts\vision_eval.py agent --worker $W --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
   ```

   It exits 1 when an unconditional case never reached the agent, a failure case did not fail
   closed as it must, or the worker did not measure a case's newest scene (the run keeps its code
   and scores that repeat nowhere); `--case C1` (repeatable) re-runs one. Without Bedrock access
   every case ends `bedrock_error`, counted, never dropped: check the summary's
   `failed_closed_task_runs` before committing a run that only measured that. To run the worker
   in this process instead, add `$C` and pass `--worker in-process`; without a cache it is refused
   off an EC2 instance in us-west-2.
7. **The counterfactuals (2.6.6)**: C1 and C10 (C4 in C10's place if C10 does not pass the gate),
   five repeats per arm with only `look_again`'s numbers differing, and the same scene clouded to
   coverage 0.62 and 0.58 in this process (arm (b), which needs the cache). About 22 model runs.

   ```sh
   python scripts/vision_eval.py counterfactual --worker "$W" $C --agent bedrock --model-id "$M" --region ap-southeast-2 --repeats 5 --out $E
   ```

   ```powershell
   python scripts\vision_eval.py counterfactual --worker $W --cache $HOME\cogs --agent bedrock --model-id $M --region ap-southeast-2 --repeats 5 --out docs\hackathon\evidence\runs
   ```

   Its checks hold that both sides were measured at their coverage and that no model was asked
   below the floor. Arm (a)'s `baseline` arm is a constructed tool output, and the report says so.
8. **E3, stability over the year**: the manifest's `year-2024` set (17 scenes) through the
   production rule, triage off, on one network, in this process so the built-up masks can be
   compared: about 1 GB read once into the cache, so run it on the Graviton instance or another
   machine in us-west-2. No model, no worker invocation.

   ```sh
   python scripts/vision_eval.py stability $C --out $E
   ```

   ```powershell
   python scripts\vision_eval.py stability --cache $HOME\cogs --out docs\hackathon\evidence\runs
   ```

   Its summary's `false_material` of `decisions`, with `wilson_low` and `wilson_high`, is the
   false-alarm rate a quiet city would call the agent at; `results.false_alarms.by_month` breaks it
   down. If a summer scene against winter ones crossed a threshold here, C3 reaches the agent in
   step 6; otherwise its gate held, which the report says.
9. **E8, the built-up check (2.4.1)**: the worker's built-up mask on `sydney-2024-08-26` against
   ESA WorldCover 2021's class 50, and the served share over each mask with the fixture's stations.
   It reads the scene from the cache and the WorldCover tile `S36E150` from the `esa-worldcover`
   bucket (eu-central-1, no credentials) over the site's box only, about 10 MB of a 20 MB object,
   kept in the cache too; a HEAD first checks the object's pinned ETag and size. No model, no worker
   invocation.

   ```sh
   python scripts/vision_eval.py builtup-check $C --out $E
   ```

   ```powershell
   python scripts\vision_eval.py builtup-check --cache $HOME\cogs --out docs\hackathon\evidence\runs
   ```

   Its summary's `iou`, `precision` and `recall` compare the masks over the pixels both saw;
   `results.ours_by_reference_class` says what WorldCover calls the pixels we call built-up. "input
   changed: reference tile S36E150" means the object is not the one pinned on 17 October
   (`REFERENCE_PINS` in `scripts/vision_eval_reference.py`): check it with `curl -sI <the URL the
   message names>`, and pin the new ETag and size in a commit of its own that says why, before
   the run.
10. **Index and commit.**

   ```sh
   python scripts/vision_eval.py index && python scripts/vision_eval.py index --check
   python scripts/pii_denylist_check.py --staged    # after `git add`
   ```

   ```powershell
   python scripts\vision_eval.py index; python scripts\vision_eval.py index --check
   python scripts\pii_denylist_check.py --staged    # after `git add`
   ```

   Run `index --check` here before pushing: CI runs it too, on the pull request (python-ci lists
   `docs/hackathon/**`), but a failure there costs a round trip. Run the PII check (your `.pii-denylist`) on what is staged, and read each trace before
   committing it: besides scene and station ids, a trace quotes the agent's note under each
   decision it was asked about (model text, through `redact.scrub`, of up to 300 characters),
   which can name a suburb or a station.

   Commit the run files and figures, `docs/hackathon/evidence/replay/`, `index.json` and
   `RESULTS.md`, and update the tracker's A2, A3 and S7 rows. A run file made before the submission tag
   on code that changed since in a way that matters to it goes in
   `docs/hackathon/evidence/older-code.json` with the reason, and `index` again.

### The evidence export, the trace and the labels (O9)

What the deployed system measured, decided and did goes into git with
`scripts/evidence_export.py` ([the live evidence](hackathon/evidence/README.md#the-live-evidence-live)
says what each file holds). Run it on Saturday 24 October, and again by Thursday 29, from a checkout
of the code being submitted, in the venv above. It only reads: nothing here writes to AWS.

**What it needs.**

- Credentials for the dev account (the profile you deploy with will do) that may:
  - `s3:GetObject` on `<prefix>-dev-content` (`snapshots/<topic>/running-state.json`, each city's
    `vision/<topic>/<site>/network.json`, the figures);
  - `dynamodb:Scan` on `<prefix>-dev-topics`, `dynamodb:Query` on `<prefix>-dev-findings` and on
    `<prefix>-dev-articles` (its `by_topic_created_at` index);
  - `logs:StartQuery`, `logs:GetQueryResults` and `logs:StopQuery` on
    `/aws/lambda/<prefix>-dev-research-tick` and `/aws/lambda/<prefix>-dev-admin-api` in the home
    Region, and on `/aws/lambda/<prefix>-dev-vision-worker` in us-west-2;
  - `lambda:GetFunctionConfiguration` on those three functions.
- The Regions: the home one is `ap-southeast-2` and the vision one `us-west-2`, Terraform's defaults
  (`--region` and `--vision-region` otherwise); the prefix is `bloggerbear` (`--prefix` otherwise).
- `gh`, signed in (`gh auth status`), for the commit that deployed each function's code
  (`provenance.json`); without it that commit is null, with the reason, and the export still
  finishes.
- Your `.pii-denylist` at the repository's root (or `PII_DENYLIST`): the export runs the check on
  every file it writes, and refuses to start writing without a list, since nothing would then check
  the files before they are committed. `--no-denylist` writes unchecked, and says so.

The logs are read through Logs Insights, a few queries at a time, each batch waited for at most
`--logs-wait` seconds (120); a 90-day scan of the three groups is a few GB at most, cents.

1. **Export.**

   ```sh
   python scripts/evidence_export.py export --env dev --networks --figures 12
   ```

   ```powershell
   python scripts\evidence_export.py export --env dev --networks --figures 12
   ```

   It writes `docs/hackathon/evidence/live/<UTC date>/`, and with `--networks` each city's network
   as `scripts/vision_benchmark_networks/<site>.json` (fill each new file's row in that directory's
   `NOTICE.md` from its `fetched_at`, `endpoint` and `query_url` and its sha256, which
   `provenance.json` gives under `networks`). A committed fixture is left byte for byte when it
   holds the same network (its `kept` says `unchanged`), and kept, with a line saying so, when it
   holds another: the evaluation's real-data runs pin its sha256, and `replay --agent recorded`
   refuses a file that changed, so do not pass `--replace-networks` once those runs are made. It
   prints any log read that did not complete: "not_checked" means a query did not finish within
   `--logs-wait`; run it again with `--logs-wait 300 --replace` (`--replace` removes only the
   export's own files: your `labels.csv` stays). Exit 2 with `pii-denylist: <file>:<line> contains
   denylist entry #n` means a personal string reached a file: the export is moved to
   `live/<date>.refused/` (git ignores it), your `labels.csv` is left where it was, and every
   network file it wrote is put back as it was; find where the string came from, then export
   again. Leave out `--with-notes` for the committed export unless you have read every note it
   adds (model and third-party text, scrubbed and labelled, but quoted).
2. **Read it before committing**: `summary.json`'s `logs` (each `complete`), `redacted` (0
   expected; anything else, `redacted_at` names each file, line and field: find out why),
   `observed_failures.worker.not_checked` (worker runs whose own line was not read: 0 when every
   log read completed), `human_control.completeness.unmatched` (a line nothing corroborates: the
   report says so), and the traces under `traces/`.
3. **The trace** the report and the video quote (the latest decision of a city, or of the demo
   topic; `--scene <scene id>` for one in particular):

   ```sh
   python scripts/evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs/hackathon/evidence/live/<date>
   python scripts/evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest
   ```

   ```powershell
   python scripts\evidence_export.py trace --topic rail-cities --site sydney --scene latest --from docs\hackathon\evidence\live\<date>
   python scripts\evidence_export.py trace --env dev --topic rail-sydney-demo --scene latest
   ```

   The second reads live (the logs and the running state, now) and writes nothing; in the video,
   run query (c) on the same tick id the committed trace shows.
4. **Label (O9).** Copy `labels-template.csv` to `labels.csv` in the same directory and fill each
   row's `label` (`agree` or `disagree` with the agent's verdict) and `reason` (one of the codes in
   the file's header), reading the trace and the figure the row names. Save it as UTF-8 CSV. Then:

   ```sh
   python scripts/evidence_export.py agreement --from docs/hackathon/evidence/live/<date>
   python scripts/vision_eval.py index --check
   ```

   ```powershell
   python scripts\evidence_export.py agreement --from docs\hackathon\evidence\live\<date>
   python scripts\vision_eval.py index --check
   ```

   `agreement` writes the k of n and its Wilson interval into that export's `summary.json`, the
   cities and the demo topic apart.
5. **Commit** the export directory, `docs/hackathon/evidence/live/README.md` and the network files,
   after `python scripts/pii_denylist_check.py --staged` (PowerShell: `python
   scripts\pii_denylist_check.py --staged`). Update the tracker's O9, S7 and A3 rows.

## Deploying to another region

`ap-southeast-2` (Sydney) is the default, and what you get with nothing set. To deploy somewhere
else, choose the region **before your first deploy**. AWS cannot move a resource between regions:
changing the region of a deployment that already exists makes Terraform plan to build everything
again in the new one, and the data does not follow.

What to set:

1. **Bootstrap:** apply it with `-var="aws_region=eu-west-1"` (your region). The state bucket is
   created there, and the deploy roles are only allowed to work there. If the roles were made for
   another region, every apply is refused with `AccessDenied`.
2. **GitHub:** set the `AWS_REGION` variable to the same region (`scripts/setup_repo.py` asks for
   it). The workflows pass it to Terraform (as `aws_region`), to the AWS credentials step, and to
   `terraform init` as the state bucket's region. Set `TF_STATE_REGION` as well only if your state
   bucket is somewhere other than `AWS_REGION`.
3. **The model. This one is easy to miss.** The default model is an inference profile whose id
   starts with `au.`, and that profile exists only in Australian regions. Outside Australia, add
   a line to each environment's `terraform.tfvars` naming the profile for your geography, for
   example `bedrock_inference_profile_id = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"`.
   The prefix is `us.`, `eu.`, `apac.`, `global.` and so on;
   `aws bedrock list-inference-profiles --region eu-west-1` lists what your region offers. The
   model must also be available to your account in that region. Nothing checks either of these at
   deploy time: a wrong id, or a model your account cannot call, shows up as an error the first
   time the pipeline calls the model. Test it first with
   `aws bedrock-runtime converse --region eu-west-1 --model-id <the id> ...`. If you set
   `bedrock_model_id` yourself, this setting is ignored.

What follows the region by itself: every resource, the deploy roles' permissions, the API and
sign-in host names, the site's content security policy, the dashboards, and the Lambda Web
Adapter layer the assistant uses (its project publishes it under the same name in each region).

What stays where it is, whatever you choose:

- **`us-east-1`, for CloudFront.** The site's certificate, the shared web ACL in front of
  CloudFront, that ACL's logs and metrics, and CloudFront's own metrics exist only in
  `us-east-1`. AWS requires it. The cost report also calls Cost Explorer there, because that is
  its only endpoint. These keep `us-east-1` written in the code, each beside a comment saying why.
- **The web search gateway** is in `ap-northeast-1` (Tokyo). AWS offers that tool in only three
  regions (`us-east-1`, `eu-west-1`, `ap-northeast-1`). It is its own setting, the `region`
  variable of `infra/modules/web-search`, and changing it is a code edit.
- **The time zones.** A new topic's daily article is scheduled in `Australia/Sydney` unless you
  pass `--daily-timezone`, and the fixed weekly and daily jobs have their times written in each
  environment's `main.tf`. A time zone is not a region: change them to suit where you are.

Check by hand, because nothing in this repository can:

- The Lambda Web Adapter layer version pinned in `infra/modules/ops-assistant/main.tf` must have
  been published in your region. Its README lists the regions.
- Every service used (Bedrock, Cognito, EventBridge Scheduler, Step Functions, WAF) must be
  offered in your region.
- [DynamoDB's tag-based access control](#dynamodb-tag-based-access-control-abac) is per region.
- `frontend/privacy.html` tells readers the logs are kept in AWS's Sydney region. Change the
  wording to match where yours are.
- The commands in these guides and in `scripts/QUICKSTART.md` name `ap-southeast-2`. Use your
  region in its place (`--region`, or `AWS_REGION` / `AWS_DEFAULT_REGION`).

## Tearing down and cost control

- **To taper, not stop:** lower a topic's cadence or research interval, or pause it
  (`topics pause <topic_id>`: its schedules are disabled and nothing of it runs until
  `topics resume`). Free, and reversible at once. `pipeline-config set --research-interval-hours`
  does it for every topic.
- **Dev:** run the `destroy-dev` workflow by hand (type `destroy` to confirm). CloudFront
  distributions take 10 to 20 minutes to disable before they can be deleted; that is normal, not
  a stuck job.
- **Production** is deliberately awkward to destroy:
  see [production-runsheet.md](production-runsheet.md#rolling-back).
- **To stop outright:** destroy dev, then production, in that order. Decide first whether to keep
  the domain and its hosted zone.
- What it costs, roughly: [production-runsheet.md](production-runsheet.md#what-it-costs-list-prices-roughly).

The firewall is the biggest fixed line on the bill (about US$39 a month for a full deployment,
at list price), and four switches can trim it for a deployment that only tries the project out:
[docs/cost-saving.md](cost-saving.md). Every switch is on by default, and the admin API's address
allowlist stays on whatever you switch off. A fork that stores real people's data should leave
them alone.

### Keeping dev's content across a teardown

Destroying dev deletes its tables and buckets, and with them every article. The bootstrap state
bucket is not part of dev, so a snapshot kept there survives
([`admin_cli backup`](../scripts/README.md#backups-restores-and-article-exports-backup)):

1. **Before:** `python scripts/admin_cli.py backup snapshot`, and note the key it prints. If you
   want the articles as plain files too, `backup export` (or `--bucket` one of yours).
2. Run the `destroy-dev` workflow.
3. **After the rebuild** (the next merge to `dev` applies it), point the CLI at the new Admin API
   URL and run `python scripts/admin_cli.py backup restore <key>`. Topics, gear, settings, costs,
   musings and articles come back; the topics' schedules are written and the published pages
   rendered again.

The same file restores into another region or account: copy it into that deployment's state
bucket under `backup/`, then restore it there. Snapshots are also taken weekly on their own.

## Local development

From the repo root. These are the commands CI runs; the tests use mocked AWS (moto), so they need
no credentials.

```bash
python -m venv .venv && . .venv/Scripts/activate   # or: source .venv/bin/activate
pip install -r lambdas/requirements-dev.txt -r scripts/requirements.txt
pytest lambdas/ scripts/
ruff check --fix lambdas/ scripts/
```

Use the ruff version pinned in `lambdas/requirements-dev.txt`: a newer one reports and fixes
different things than CI does. Never pass `--unsafe-fixes`, and do not run `ruff format` across
the repository; the tree has never been formatted.

```bash
pip install bandit==1.7.10
bandit -r lambdas/ scripts/ --severity-level high --confidence-level high
```

Checkov scans the Terraform, from the repository root, with what `.checkov.yaml` runs and skips
(every skip there, and each `checkov:skip` comment on a resource, says why):

```bash
pip install checkov==3.3.25
checkov --config-file .checkov.yaml
```

```bash
terraform fmt -check -recursive infra/
cd infra/environments/dev && terraform init -backend=false && terraform validate
```

Turn on the commit hook too: [The personal-data and secret checks](#the-personal-data-and-secret-checks).

## What is still tied to the original deployment

These need a hand edit in your fork, or cannot be changed yet.

- **Production's domain.** `infra/environments/production/terraform.tfvars` sets `domain_name` and
  `hosted_zone_id` to the original site's. Change both to yours (`hosted_zone_id` is a bootstrap
  output). A value in that file wins over anything CI passes, so it has to be edited there.
  Production cannot be deployed without a domain. Any registrar will do: see
  [production-runsheet.md](production-runsheet.md) for pointing it at the zone.
- **The site itself.** The pages, the privacy policy, the bear's voice in the prompts and some
  tests name BloggerBear and `bloggerbear.com`.
- **The backend blocks** in both environments name `bloggerbear-terraform-state` in
  `ap-southeast-2`. Terraform does not allow a variable there. CI overrides the name with
  `TF_STATE_BUCKET_DEV` / `TF_STATE_BUCKET_PROD`, and the region with `AWS_REGION` (or
  `TF_STATE_REGION`); if you forget the bucket, the init is refused, because that bucket is not
  yours. The state bucket's name is not built from `UNIQUE_NAME_PREFIX`: it has its own
  bootstrap variable, `state_bucket_name`.
- **The OIDC trust.** It is a bootstrap variable (`github_repo`), not a GitHub setting, because
  it is part of the roles. If you rename or transfer your fork, re-apply bootstrap.
- **The model.** Leaving `bedrock_model_id` unset uses the AU Claude Haiku 4.5 inference profile
  in your account and region. Outside Australia that profile does not exist: set
  `bedrock_inference_profile_id` in the environment's `terraform.tfvars` (see
  [Deploying to another region](#deploying-to-another-region)). To use a different model
  altogether, set `bedrock_model_id` there, or set a default in
  [the model registry](#the-model-registry-seeding-and-rotation) with no deploy.
- **The web search gateway's region** (`ap-northeast-1`) and the privacy page's wording about
  where logs are kept: see the same section.
- **Repository protection** (rulesets, required reviewers, secret scanning) is set in GitHub, not
  in code. The archived [public-repo-runsheet.md](enhancements/archive/public-repo-runsheet.md) lists the settings a
  public copy should have.
