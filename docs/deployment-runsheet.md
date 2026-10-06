# Deployment runsheet

How to run your own copy of BloggerBear: from an empty AWS account to a dev site that researches
and publishes by itself. Production and a custom domain are the next runsheet,
[production-runsheet.md](production-runsheet.md), which picks up where this one stops.

You set it up with GitHub secrets and variables, one local Terraform apply, and a few commands.
You should not need to edit Terraform, except for what is listed under
[What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment).
Every setting, and where it goes, is in one table: [configuration.md](configuration.md).

Account IDs on this page are placeholders (`111111111111`, `123456789012`, `000000000000`).
Use your own. Commands name `ap-southeast-2` (Sydney), the default region; use yours if you chose
another ([Deploying to another region](#deploying-to-another-region)).

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
  [Step 3](#3-one-account-or-two) says what differs.
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
| `aws_region` | Region for the state bucket, and the region the deploy roles are allowed to work in. Must be the same as the `AWS_REGION` GitHub variable. | `ap-southeast-2` |

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
allows 64, so a 14-character prefix leaves a topic id 24 and a shorter one leaves more.

**Protecting the branches.** Require a pull request on `dev` and `prod`, and block force-pushes
and deletions. GitHub offers rulesets and branch protection only on public repositories or paid
plans; [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md) has the commands. Without
them the AWS side still holds: the deploy roles trust only the `dev` branch and the `production`
environment, and nothing is applied before the security scans, lint and tests pass in the same run.

## 3. One account or two

**Two accounts (recommended if you have them):**

- Apply bootstrap **twice**, once with each account's credentials, each with its own
  `aws_account_id` and its own `state_bucket_name`. Keep the two state files apart, for example
  with `terraform workspace new production` before the second apply. Pass `domain_name=""` in the
  dev account so the hosted zone exists only in the production account.
- Each apply creates both deploy roles. Use the dev role from the dev account and the production
  role from the production account. The other two are never assumed; nothing is given their ARNs.
- `AWS_DEV_ACCOUNT_ID` and `AWS_PROD_ACCOUNT_ID` differ. So do `TF_STATE_BUCKET_DEV` and
  `TF_STATE_BUCKET_PROD`.
- **Leave dev's `web_acl_arn` empty.** A CloudFront distribution can only use a web ACL owned by
  its own account; AWS WAF cannot attach one across accounts. Dev's two CloudFront distributions
  then run without the shared ACL. Dev's APIs keep their own regional web ACLs, which dev creates
  itself. If you set `web_acl_arn` to an ACL in another account, and `AWS_DEV_ACCOUNT_ID` is set,
  the plan stops with a message saying so.
- Dev's dashboard has a panel for the shared ACL's metrics. In a two-account setup it stays empty,
  because those metrics are in the production account.
- Account-level settings are per account: the Lambda quota, and
  [DynamoDB's tag-based access control](#dynamodb-tag-based-access-control-abac), must be right
  in both.

**One account:**

- Apply bootstrap once.
- Both role ARNs are in the same account. Both account IDs are the same. Both state bucket
  secrets name the same bucket; dev and production use different keys inside it.
- Dev may share production's CloudFront web ACL: after production's first apply, put its
  `wafv2_web_acl_arn` output in `infra/environments/dev/terraform.tfvars` as `web_acl_arn`.
- You cannot run two copies of the same environment under one prefix in one account: resource
  names are `<prefix>-<environment>-<resource>`.

A third arrangement, one deployment account whose role assumes a role in each environment's
account, is not built. [Enhancement: a deployment account](#enhancement-a-deployment-account-that-assumes-a-role-in-each-environments-account)
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
   its daily article at 9 AM `Australia/Sydney`; `--daily-cadence` and `--daily-timezone` change
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

The same table holds a second item, `config_id` = `pipeline`, for the pipeline-wide settings
(`pipeline-config set`). Leave it alone here; the two never overwrite each other.

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

The operator's assistant reads logs itself (`log_review`, `api_errors`; [design](enhancements/ops-assistant-log-reader.md)).
Its permission is written like `table_sample`'s: `logs:StartQuery` on `/aws/lambda/<prefix>-<env>-*`
and `/aws/apigateway/<prefix>-<env>-*` only when the log group is tagged `ManagedBy = Terraform`,
this deployment's `Project`, and an `Environment` that assistant may read (dev: `dev`;
production: `production` and `shared`). Unlike DynamoDB there is no account setting to switch on.

- **Check it** after the first apply: ask the assistant "any errors in the logs?". It answers with
  counts, or "I found no errors", for every function.
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

### The vision worker (satellite_vision topics)

Off by default (`vision_enabled = false` in both environments); nothing is created until it is
turned on. Design: [docs/enhancements/opencv-agentic-vision-enhancement.md](enhancements/opencv-agentic-vision-enhancement.md).

1. **Re-apply the bootstrap by hand** (it is never applied by CI). It adds two statements to the
   deploy roles: Lambda functions and log groups named `<prefix>-*-vision-*` in `vision_region`
   (default `us-west-2`, beside the Sentinel-2 imagery). If you change `vision_region`, pass the same
   value to the bootstrap and to the environment.
2. **Set `vision_enabled = true`** in `infra/environments/dev/terraform.tfvars` and merge. The apply
   creates, in `vision_region`: the worker (arm64, python3.12), its role (its own log group only),
   its log group, and a small artifacts bucket its package is uploaded through. The research tick
   is given the worker's ARN (`VISION_WORKER_ARN`) and may invoke that one function.
3. **Create a topic** with `adapter: "satellite_vision"` and `sites` in its `adapter_config`
   (`common/adapters/satellite_vision.py` documents every key).

The worker reads public data over HTTPS and needs no other access. Its errors alarm is not in the
pipeline dashboard: observability's alarms are in the home Region, and the worker is not.

### The COOL backend and the benchmark

COOL (OpenCV's Graviton-tuned OpenCV 5) is what the competition's COOL award scores, and it is
measured against the stock worker above. Nothing here is in Terraform yet; these are by hand.

1. **Subscribe** to *Cloud Optimized OpenCV For AWS Graviton4* (or *Graviton3*) on AWS Marketplace.
   It has a 7-day free trial, then usage pricing: note the price, and unsubscribe when done.
2. **Measure on Graviton4, both builds**, on a `c8g` instance launched from the COOL AMI (Ubuntu
   24.04, COOL preinstalled), in the vision Region:

   ```sh
   git clone <this repository> && cd BloggerBear
   python3 -m pip install requests                      # COOL's cv2 and numpy are already there
   python3 scripts/vision_benchmark.py kernels --out cool-g4.json
   python3 -m venv stock && stock/bin/pip install -r lambdas/requirements-vision.txt
   stock/bin/python scripts/vision_benchmark.py kernels --out stock-g4.json
   ```

   Each report names its CPU (Neoverse V2 = Graviton4) and its OpenCV build's fingerprint. Add the
   deployed stock worker end to end (`worker --arn <VISION_WORKER_ARN>`) and an x86 run for the
   other rows. **Terminate the instance afterwards.**
3. **Pin the fingerprint**: `build_sha256` from the COOL report is what a COOL worker's
   `COOL_BUILD_SHA256` is set to; a `cool` worker whose `cv2` is anything else refuses every
   request (`not_cool`).
4. **The COOL worker image**: `docker/vision-cool/Dockerfile` builds the same handler on top of the
   COOL Docker image (its URI from the subscription, pinned by digest; the file says how). Whether it
   runs as a Lambda on Lambda's arm64 CPUs, or needs ECS on a Graviton4 instance, is the open question
   in the design's §8; run the image's `kernels` benchmark on Lambda-class hardware before wiring it.

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

## Enhancement: a deployment account that assumes a role in each environment's account

**Not built, and not tested.** This is a sketch for anyone who wants GitHub to trust one small
"deployment" account, which then assumes a role in the dev account and another in the production
account. It suits an AWS Organization where workload accounts must not have their own OIDC
provider. Today's two-account setup is simpler: each account has its own OIDC provider and its own
deploy role, and GitHub assumes each one directly.

It is mostly configuration, but not only: the workflows assume exactly one role, so the second
hop needs **one more step in each deploy workflow**. Terraform itself needs no change.

1. **In the deployment account:** the GitHub OIDC provider, and a role GitHub may assume (the
   same trust policy `infra/bootstrap` writes today). Its only permission is to assume the
   target roles:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": ["sts:AssumeRole", "sts:TagSession"],
       "Resource": [
         "arn:aws:iam::111111111111:role/gha-<prefix>-dev-deploy",
         "arn:aws:iam::123456789012:role/gha-<prefix>-prod-deploy"
       ]
     }]
   }
   ```
2. **In each environment's account:** the deploy role with the permissions `infra/bootstrap`
   gives it today, but trusting the deployment account's role instead of GitHub:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": { "AWS": "arn:aws:iam::000000000000:role/gha-<prefix>-hub" },
       "Action": ["sts:AssumeRole", "sts:TagSession"]
     }]
   }
   ```
   This trust no longer sees GitHub's branch or environment claims, so the branch and release
   rules must be enforced on the first hop, with one hub role for dev and another for production.
3. **In the workflow,** after the existing credentials step (which now takes the hub role's ARN),
   one more step that chains into the target role. `AWS_DEV_TARGET_ROLE_ARN` is a new secret:
   ```yaml
   - name: Assume the environment's deploy role
     uses: aws-actions/configure-aws-credentials@<the commit the step above is pinned to>
     with:
       aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}
       role-to-assume: ${{ secrets.AWS_DEV_TARGET_ROLE_ARN }}
       role-chaining: true
   ```

What to check before relying on it:

- A chained session lasts one hour at most. A first production apply takes 15 to 25 minutes, so
  it fits, but a stuck CloudFront or certificate wait can run past it.
- The state bucket must be readable by the target role: keep it in the environment's own account.
- `AWS_DEV_ACCOUNT_ID` and `AWS_PROD_ACCOUNT_ID` then name the target accounts, which is what the
  account check compares.
- `infra/bootstrap` would need a variable for the trusted principal, so that it can write the
  trust policy in step 2. That is the one Terraform change, and it is in bootstrap only.

## Tearing down and cost control

- **To taper, not stop:** lower a topic's cadence or research interval, or disable its schedules.
  Free, and reversible at once. `pipeline-config set --research-interval-hours` does it for every
  topic.
- **Dev:** run the `destroy-dev` workflow by hand (type `destroy` to confirm). CloudFront
  distributions take 10 to 20 minutes to disable before they can be deleted; that is normal, not
  a stuck job.
- **Production** is deliberately awkward to destroy:
  see [production-runsheet.md](production-runsheet.md#rolling-back).
- **To stop outright:** destroy dev, then production, in that order. Decide first whether to keep
  the domain and its hosted zone.
- What it costs, roughly: [production-runsheet.md](production-runsheet.md#what-it-costs-list-prices-roughly).

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
  in code. [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md) lists what the original
  repository uses.
