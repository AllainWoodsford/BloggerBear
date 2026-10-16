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
plans; [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md) has the commands. Without
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
  counts, or "I found no errors", for every function in the home Region (the vision worker, in
  `vision_region`, is left out; see [Vision (optional)](#vision-optional)).
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
[enhancements/vision.md](enhancements/vision.md)). **Off by default**, in both environments:
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
   keep their place. A `rail_access` topic is one city inside one Sentinel-2 tile:
   [`rail_access` topics](#rail_access-topics), below, creates the four starting cities from a
   file. Both get `force_manual_review` whether you ask or not:
   every article waits for `admin_cli approve`, and the daily trending digest, which publishes
   with no person, leaves these topics out (#298). Trigger the first tick by hand
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
`/aws/lambda/<prefix>-<env>-vision-worker`, in `vision_region`. The ops assistant does not read it
either: its log review leaves the worker out (its logs are outside the home Region), and asked for
it by name it says "That function's logs are kept outside this Region, so I can't read them yet."
Reading them is planned work, the assistant's vision round.

### `rail_access` topics

The Rail Access Monitor is one topic per city
([what it measures](architecture/blogger-vision.md#part-two-the-trains-explainer); every key and
default: [configuration.md](configuration.md#a-rail_access-topic)). Turn the worker on first
(above): without it each tick records `last_error` on the city and writes nothing.

**Create the four city topics.** `scripts/rail_topics.example.json` holds four complete admin API
topic bodies, Sydney, Melbourne, Singapore and Boston: each city's box from
[rail-access-monitor.md §2](enhancements/rail-access-monitor.md#2-the-angle-rail-access-not-rail-yards),
`reach_m` 1000, an editorial goal, the city's days of the week (`daily_cadence`) and a findings
window of 192 hours (`findings_window_hours`). `topics create` takes a topic as flags, not as a
file, so read each field out of it, from the repository root:

```bash
field() { python -c 'import json, sys; v = json.load(open("scripts/rail_topics.example.json"))["topics"][int(sys.argv[1])][sys.argv[2]]; print(v if isinstance(v, str) else json.dumps(v))' "$1" "$2"; }
for i in 0 1 2 3; do
  python scripts/admin_cli.py topics create --topic-id "$(field $i topic_id)" --name "$(field $i name)" \
      --adapter rail_access --config-json "$(field $i adapter_config)" \
      --editorial-goals-json "$(field $i editorial_goals)" \
      --daily-cadence "$(field $i daily_cadence)" --daily-timezone "$(field $i daily_timezone)" \
      --findings-window-hours "$(field $i findings_window_hours)"
done
python scripts/admin_cli.py topics get rail-sydney
```

Each answer shows `"force_manual_review": true`, which was not sent: the admin API forces it for
this adapter. The example's editorial goal is the city's own: it asks for the measured figures,
the capture date and the coverage, and rules out ridership, cost, recommendations and causes. A
rail topic created without one is no longer drafted under the global default ("independent web
research ... randomly sampling 5 to 10 highly relevant articles"), which does not fit a
measurement: since #298 the `rail_access` adapter has a default goal of its own
(`ADAPTER_DEFAULTS` in `lambdas/common/editorial_resolver.py`), which restates the summary
prompt's limits (only what was measured, with the capture dates; no ridership, journey time, cost,
cause, forecast or recommendation). A topic's own goal wins over it.

**How the cadence works.** Like every topic, each city has two schedules. The research heartbeat
(`rate(1 hour)` by default) asks whether the topic is due (its `research_interval_hours`, else the
pipeline-wide interval) and, when it is, the adapter searches Earth Search for the newest scene
over the whole city in the last 20 days. Sentinel-2 passes every few days, so most ticks find a
scene the city already has: nothing is measured, no model is called, and the tick may instead
refresh a network past its 30-day TTL. A new scene is measured; only a material change (or the
first observation) makes a Finding. The daily cycle runs on `daily_cadence` in `daily_timezone`,
and the cities take turns, one a day, each at 09:05 Sydney time, after the other topics' 09:00 to
09:03: Sydney on Monday and Friday (`cron(5 9 ? * MON,FRI *)`), Melbourne on Tuesday and Saturday,
Singapore on Wednesday and Sunday, Boston on Thursday. More cities can join the rotation (see
"Adding a city" below).

Each run drafts every Finding made since the city's last article, reaching back at most the topic's
`findings_window_hours` (`lambdas/common/findings_window.py`; 24 hours when it is not set, at most
336); with none, it stops before any model call. The example sets 192 hours, 8 days: the longest
gap between two of a city's runs is Boston's 7 days, and the eighth day covers a clock change or a
late run. The window never reaches back past the topic's `last_article_at`, so no Finding is
drafted twice: the daily cycle stamps it with the run's start time on every article it writes,
published or held for review (every rail article is held). One Finding can still be missed: the
research tick dates a Finding (`captured_at`) when its fetch starts, at the start of the tick, and
writes it at the tick's end, so a Finding whose tick is still running when the city's daily cycle
starts is not there for that run and, once that run has written an article, is dated before the
next window starts. It lands in neither window. That predates PR K and is a code follow-up
([vision.md §10](enhancements/vision.md#10-still-open)). Findings expire 14 days after they are
made (`FINDING_TTL_DAYS` in `lambdas/research_tick_handler.py`), which is why the window is capped
at 336 hours. Daylight saving no longer matters: when the clocks move, two runs are an hour more or
less than whole days apart, and the window starts at the last article whatever the gap. Re-Write
from the review inbox rebuilds a rail article's sources from the same window, looking back
`findings_window_hours` from the article's creation (`_findings_for` in `lambdas/common/rewrite.py`,
#297), so a rewritten article still sees the Findings of the city's off days (and may also see some
an earlier article used, since the previous article is not known there). Rail Findings are few (the
first observation, then material changes), so most runs draft nothing. Within a topic, which holds
at most three cities, one city is measured per tick (`max_sites_per_tick`): those never attempted
first, then the one attempted longest ago; a city skipped for that cap, or deferred because the
tick ran short of time, keeps its place.

**When a run writes nothing.** A run that writes nothing stamps nothing. That covers three cases:
it found no Finding; it had no usable angle; or it ended in an error. An error the handler catches
is quiet: the daily cycle returns `"status": "error"`, so Step Functions sees a successful invoke
and does not retry it, and nothing reaches the DLQ or an alarm. The error is in the execution's
output, not in the function's log (the handler's top-level guard prints nothing, a code follow-up
in [vision.md §10](enhancements/vision.md#10-still-open)); the sign is a city's run day with a new
Finding but no new item in the review inbox. A timeout (the function's `timeout` is 300 s) or a
crash is not caught: the state machine retries it twice and then sends it to the DLQ
(`admin_cli failed-executions list`), so it is not silent. The next run then still starts at the
city's last article, but never more than 192 hours back. A run that found no Finding loses
nothing: its window was empty, and the next run's 192 hours reach back past it (no city's runs are
more than 7 days apart), so they cover everything made since. After either of the other two, a
two-day city still gets everything on its next run: its runs are 3 and 4 days apart, so its last
article is at most 7 days old. For Boston, the once-a-week city, or after two such runs in a row,
Findings older than 192 hours drop out: for Boston that is about six of the week's seven days, and
for a two-day city after two missed runs, two or three days.

After a run that had no usable angle or ended in an error, the remedy is to run the city's daily
cycle again by hand soon after:

```bash
python scripts/admin_cli.py topics trigger rail-boston --pipeline daily_cycle
```

It reaches back to the city's last article, up to 192 hours, so for Boston run it within about 24
hours of the run that wrote nothing, while the last article is still under 192 hours old. Leave out
`--force`: it ignores the last article and drafts the whole 192-hour window, so it would draft
again the Findings an earlier article already used.

**What the first tick does.** Trigger it by hand rather than wait for the heartbeat:
`python scripts/admin_cli.py topics trigger rail-sydney --pipeline research_tick` (a manual tick
ignores the interval). In order: the scene search; the city's OpenStreetMap network from the
Overpass API, since none is cached (within 30 s, both endpoints included; a failure is
`last_error` `osm` and the city waits for the next tick); then, if 45 s of the 70 s measuring
budget remain, the worker measures the newest scene with that network (if the fetch was slow, the
measurement is deferred to the next tick, which uses the cached network). If the scene's coverage
is at least 0.6, that is the first observation: a Finding, its summary written by the model from
"First observation. Sydney: served share … %, … stations, … km² beyond 1 km of a station
(coverage … %)" (the area beyond reach printed to one decimal, such as 12.3 km²) and the planner
notes, with the figure attached, and no triage (there is no baseline to check against; a person
reads every article anyway). A scene under the floor writes no Finding, and the next clear one is
the first observation. All of that takes longer than the CLI's 30 s wait (`topics trigger` polls
for a new Finding for 30 s, `_RESEARCH_TICK_TIMEOUT_SECONDS` in `scripts/admin_cli.py`; the tick
has 120 s), so the CLI printing "No new Finding after 30s. This can be a legitimate outcome …" does
not mean it failed. Wait about two minutes, or until
`python scripts/admin_cli.py topics findings rail-sydney` shows the new Finding. Then
`topics trigger rail-sydney --pipeline daily_cycle` drafts the article, which the
`force_manual_review` hold keeps in the review inbox for `admin_cli approve`, whatever the
compliance review says. A daily cycle started before the Finding is written finds nothing to
draft on a first tick (run it again); on a later tick, with other Findings in the window, it
drafts without the new one, which is then lost ("One Finding can still be missed", above).

**Where things land.** In the content bucket, `<prefix>-<env>-content`: the figure as the worker
drew it, `vision/<topic>/<site>/<scene>.png` (for example
`vision/rail-sydney/sydney/S2A_56HLH_20240105_0_L2A.png`); the running state, rewritten every
tick, `snapshots/<topic>/running-state.json`; and each Finding's own snapshot,
`snapshots/<topic>/<captured_at>.json`. The running state holds, per city, the history (the
newest entry with its station, flag, suggestion and desert lists), the cached network, and any
`last_error`, `network_error` or `failed_scene`; and `first_observed_at` for the topic. When the
article is published, each figure is copied to the site bucket, `<prefix>-<env>-site`, as
`articles/figures/<article>/<n>.png`, and the page shows it under the credits with its caption.

```bash
aws s3 cp s3://<prefix>-dev-content/snapshots/rail-sydney/running-state.json - | python -m json.tool | less
```

**Reading the agent's trail.** From the second clear scene on, a numeric change goes to the
triage agent before it can become a Finding. `topics findings rail-sydney` prints the latest
Finding: its `summary` (the model wrote it from a diff that, for a change the agent judged real,
quoted the agent's reason as `the vision agent's note (not a measurement): "…"`), `figures`,
`planner_notes` and `raw_snapshot_s3_key`. That snapshot (and the running state) holds the whole
trail under `triage`, keyed by site id: `verdict`, `reason`, `tool_calls` (each tool's `name`, its
`input` and whether it was `ok`), `model_id`, the tokens and `model_calls`; the newest history entry
repeats the verdict and reason. A change the agent judged an artefact writes no Finding, so its
trail is only in the running state. The review evidence, which the fresh-data review checks the
draft against, carries each city's measured numbers and planner notes, not the trail: to see why
a change was or was not reported, read `triage`.

**Adding a city.** A new city is a new topic: copy one of the four bodies, with a new `topic_id`
(at most 24 characters), name, site and days. Every day of the week already features a city, so
give the new one a day of its own, taken from a city that has two (for Friday,
`topics update rail-sydney --daily-cadence "cron(5 9 ? * MON *)"`), or share a day with another
city, which then has two articles. Keep `findings_window_hours` at least the longest gap between
a city's days plus a day: 192 hours covers any city that runs at least once a week. Its box must
sit inside one Sentinel-2 tile, because the scene search keeps only scenes whose footprint holds the whole site. A tile is a
100 km square of the UTM grid and 9.8 km more to the east and south (Sydney's 56HLH runs from
easting 300,000 to 409,800 and northing 6,190,240 to 6,300,040), so a box whose corners fall in
one 100 km square fits. Check it with `lambdas/vision/geo.py` from the repository root (any
Python 3.11 or later; `geo.py` needs only the standard library), putting your box's west, south,
east and north in place of Sydney's:

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
bus to. Changing it, or the polygon, on a running topic starts a new baseline: the next scene
reports "baseline still building" rather than a change. Either is changed with
`topics update <id> --adapter-config-json '...'`, which replaces the whole `adapter_config`, not
the keys it names: read it with `topics get <id>`, edit it, and send all of it back, keeping the
city's `sites` entry with the same `id` (and polygon, unless that is what changes). Sent with
only the changed key, the config loses `sites` and every tick fails until it is whole again; any
other key left out falls back to its default; a city under a new `id` starts with no history.

A topic may hold up to three cities (`MAX_RAIL_SITES`), measured one per tick, each with its own
network in the running state; one city per topic is the design, since each city's network is
about 3 MB of the state and articles are about one city.

### The COOL backend and the benchmark

COOL (OpenCV's Graviton-tuned OpenCV 5, "Cloud Optimized OpenCV for AWS Graviton4" on AWS
Marketplace) is what the competition's COOL award scores, and it is measured against the stock
worker above on one Graviton4 instance, in one session. Nothing here is in Terraform; these are by
hand, and every command below is what the session runs, in this order. The evidence it leaves
behind is listed at the end; the run files it writes are the only source of the COOL numbers in
the report ([the evidence README](hackathon/evidence/README.md) has the schema). The script's own
options are in [scripts/README.md](../scripts/README.md#timing-the-vision-worker-vision_benchmarkpy).

**Venvs, not the system pip.** Ubuntu 24.04 refuses a system-wide `pip install` (PEP 668,
"externally-managed-environment"). Every install below goes into a venv: never `sudo pip`, never
`--break-system-packages`, and never `opencv-python-headless` or `numpy` into the COOL venv, which
must see the product's own `cv2` and the numpy it was built against (a stock install there would
shadow them, and the handler would answer `not_cool`).

0. **Reconnaissance (Friday 16, 20 minutes, right after subscribing).** Record the answers in
   `docs/hackathon/evidence/cool/recon.md`:
   - the Marketplace product id, version and price, and its **delivery type**: an AMI or a
     container image;
   - whether it is offered in **us-west-2**, beside `sentinel-cogs`, and with `c8g` instances;
   - on the smallest instance it allows: `python3 -V`;
     `python3 -c 'import cv2; print(cv2.__version__, cv2.__file__)'`; `lscpu`;
     `grep -m1 'CPU part' /proc/cpuinfo`.

   Then decide:

   | Found | Then |
   |---|---|
   | an AMI; `cv2` imports and is 5.x; Python 3.11 or newer | go: the steps below as written |
   | `cv2` imports, but is not 5.x | record it; every claim is scoped to "COOL <version>"; add a stock venv of the same major version, so the comparison is like for like |
   | a container image, not an AMI | launch a `c8g` with Docker; build `docker/vision-cool/Dockerfile` from the image by digest (its 5.x gate applies) and run every step inside it (`docker run ... python3 bench/vision_benchmark.py ...`) |
   | no Python binding (C++ only) | the container route if one exists; otherwise the COOL proof (C3) is "not shown" and the report says why; the COOL configuration (C1) and the stock rows still stand |
   | Python under 3.11 | the stock venvs need a 3.11+ interpreter (numpy 2.4.4 requires it): install `python3.12` for them (step 3) and record that the backends ran on different Pythons |
   | not in us-west-2 | the nearest Region with `c8g`; fill `--cache` once there (the reads then cross Regions only once) |
   | no trial, or more than about USD 5 an hour | decide before launching |
1. **Subscribe** on AWS Marketplace to *Cloud Optimized OpenCV for AWS Graviton4* on Friday 16.
   Note the price; unsubscribe at the end unless the subscription is kept.
2. **Launch** a `c8g.xlarge` (4 vCPU, 8 GiB: enough for the 1.32 GB worst case) in us-west-2,
   from the COOL AMI (or a plain Ubuntu arm64 AMI with Docker, for a container image). Require
   IMDSv2. Connect through SSM Session Manager, with no inbound ports. The instance role has
   `AmazonSSMManagedInstanceCore` only (step 12 adds one statement for its own hour).
3. **The host.** On the instance:

   ```sh
   lscpu > lscpu.txt
   grep -m1 'CPU part' /proc/cpuinfo      # expect 0xd4f: Neoverse V2, Graviton4
   python3 -V && uname -r
   ```

   If `python3 -V` is under 3.11, the stock venvs need a 3.11+ interpreter: install `python3.12`
   (`sudo apt-get install python3.12-venv`) and use it in step 7; record it in `recon.md`.
4. **The COOL build.**

   ```sh
   python3 -c 'import cv2, sys; sys.stdout.write(cv2.getBuildInformation())' > cool-buildinfo.txt
   sha256sum cool-buildinfo.txt
   python3 -c 'import cv2; print(cv2.__version__, cv2.__file__)'
   ```

   The sha256 is the COOL fingerprint: it must equal `build_info()["build_sha256"]`, which is the
   hash of exactly that text (`lambdas/vision/build.py`), and it is what `COOL_BUILD_SHA256` is
   pinned to. Expect `kleidicv` True.
5. **The code and the inputs.** Clone the repository at the commit to be benchmarked and record
   it: `git rev-parse HEAD` (the submission tag does not exist yet; the evidence index says how an
   earlier commit is still cited). The repository may still be private: `gh auth login` in the
   session, or copy a `git archive`.

   Two inputs must be in that checkout before the runs:

   - **The Sydney network fixture**, `scripts/vision_benchmark_networks/sydney.json`: the network
     the deployed worker was sent, taken from the running state after the first Sydney tick, on
     your own machine:

     ```sh
     aws s3 cp s3://<prefix>-dev-content/snapshots/rail-sydney/running-state.json - | jq '.sites.sydney.network | {ways, stations, pois, fetched_at, source, endpoint, query_url, counts}' > scripts/vision_benchmark_networks/sydney.json
     ```

     ```powershell
     aws s3 cp s3://<prefix>-dev-content/snapshots/rail-sydney/running-state.json - | jq '.sites.sydney.network | {ways, stations, pois, fetched_at, source, endpoint, query_url, counts}' | Out-File -Encoding utf8 scripts\vision_benchmark_networks\sydney.json
     ```

     The alternative is `python scripts/vision_benchmark.py fetch-network --scene sydney-rail-2024-08-26`
     on any machine that reaches Overpass (the network is then whatever OpenStreetMap holds that
     day). Fill the row in `scripts/vision_benchmark_networks/NOTICE.md` from the file's
     `fetched_at`, `endpoint` and `query_url` and `sha256sum`, run
     `python scripts/pii_denylist_check.py --staged` before committing it (Sydney's suburb and
     station names could match the denylist; if they do, decide whether to allow the term or keep
     the file out of git, in which case the run files still cite its sha256), and commit it. Without
     the file the two rail scenes report `error: network_file missing` and only the ship scene is
     timed.
   - **The pinned scenes.** `scripts/vision_eval_manifest.json` was pinned on 16 October (every
     asset's ETag and size, from the build container). Confirm nothing moved, on your own machine
     or the instance:

     ```sh
     python scripts/vision_eval.py pin        # prints "the manifest matches the bucket", or what changed
     ```

     ```powershell
     py -3.11 scripts\vision_eval.py pin
     ```

     A changed ETag is said and exits 1: every benchmark run then refuses that scene ("input
     changed") until the manifest is re-pinned with `--write`, which is a new input, not the one
     earlier runs read. `coverage` was run on 16 October too (its run file is in
     `docs/hackathon/evidence/runs/`); it need not be re-run for the session, and when it is (a
     re-pinned scene, or a scene added), it runs anywhere with internet, about 60 range requests
     and 0.7 MB a scene:

     ```sh
     python scripts/vision_eval.py coverage --write --out docs/hackathon/evidence/runs
     ```

     ```powershell
     py -3.11 scripts\vision_eval.py coverage --write --out docs\hackathon\evidence\runs
     ```
6. **The COOL venv**, on the product's own Python, with the system site-packages visible so the
   product's `cv2` and numpy are the ones imported:

   ```sh
   python3 -m venv --system-site-packages ~/cool
   ~/cool/bin/pip install requests==2.34.2 networkx==3.4.2 boto3==1.34.162
   ~/cool/bin/python -c 'import cv2; print(cv2.__file__)'    # must be outside ~/cool
   ~/cool/bin/python -c 'import cv2, hashlib; print(hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest())'   # must equal step 4
   ```

   These are `lambdas/requirements-vision-cool.txt`'s pins without `awslambdaric`, plus boto3
   for step 12. **Never install opencv or numpy here.** If the AMI puts `cv2` on a custom
   `PYTHONPATH`, keep that variable in the environment of every COOL command.
7. **The stock venvs**: OpenCV 5 as the deployed worker runs it, and the newest OpenCV 4 that has
   an aarch64 wheel for this Python and numpy 2, so the report can say what OpenCV 5 changed for
   this workload:

   ```sh
   python3 -m venv ~/stock && ~/stock/bin/pip install -r lambdas/requirements-vision.txt
   python3 -m venv ~/stock4 && ~/stock4/bin/pip install "opencv-python-headless==4.14.0.94" numpy==2.4.4 requests==2.34.2 networkx==3.4.2
   for v in stock stock4; do ~/$v/bin/python -c 'import cv2, sys; sys.stdout.write(cv2.getBuildInformation())' > $v-buildinfo.txt; done
   sha256sum stock-buildinfo.txt stock4-buildinfo.txt
   ```

   `opencv-python-headless==4.14.0.94` is the 4.x pin: PyPI's newest 4.x release (28 July 2026),
   with `cp37-abi3` aarch64 wheels (manylinux_2_17 and 2_28); it was installed beside
   `numpy==2.4.4` on 16 October and imports as `cv2` 4.14.0 (the evidence README records the
   check). Use `python3.12` in place of `python3` here if step 3 said so. Stock aarch64 builds can
   include KleidiCV too, which is why the proof is the fingerprint and the mapped libraries, not
   the flag.
8. **The runs.** Interleave the backends, so a slow minute on the instance does not land on one
   side. Set the names once:

   ```sh
   E=docs/hackathon/evidence/runs
   D=$(date -u +%F)                        # the UTC date that starts every run id
   CPU=neoverse-v2-graviton4               # the cpu slug in every run id: check it against the first file written
   SHA=<step 4's sha256>
   L="--imds --label cool_product=<product id> --label cool_version=<version> --label cool_delivery=<ami|container>"
   C="--cache $HOME/cogs"
   ```

   Each run writes `$E/$D-bench-<mode>-$CPU-<backend>-<tag>.json` (`--out $E` with `--tag`), and
   `scenes` writes each scene's figure beside it. `--imds` records the instance type, AMI, Region,
   architecture and the Marketplace product codes, nothing else of the identity document (the
   account id never reaches a file). `taskset -c 0` pins a run to one core, the deployed worker's
   shape; the affinity is recorded, so the one-core rows say so themselves (`--pin-cpu 0` does the
   same from inside the script). `--isolate` runs each scene in a fresh interpreter, so its peak
   memory is its own.

   ```sh
   for r in 1 2; do
     taskset -c 0 ~/stock/bin/python scripts/vision_benchmark.py kernels --threads 1 $L --out $E --tag 1t-r$r
     taskset -c 0 ~/cool/bin/python  scripts/vision_benchmark.py kernels --threads 1 --backend cool --cool-sha $SHA $L --out $E --tag 1t-r$r
     ~/stock/bin/python scripts/vision_benchmark.py kernels $L --out $E --tag 4t-r$r
     ~/cool/bin/python  scripts/vision_benchmark.py kernels --backend cool --cool-sha $SHA $L --out $E --tag 4t-r$r
   done
   ~/stock/bin/python scripts/vision_benchmark.py scenes --repeats 1 $C --out /tmp/warm.json                      # fills the cache: the timed runs read no network
   taskset -c 0 ~/stock/bin/python scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C $L --out $E --tag 1t
   taskset -c 0 ~/cool/bin/python  scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C --backend cool --cool-sha $SHA $L --out $E --tag 1t
   ~/stock/bin/python scripts/vision_benchmark.py scenes --repeats 5 --isolate $C $L --out $E --tag 4t
   ~/cool/bin/python  scripts/vision_benchmark.py scenes --repeats 5 --isolate $C --backend cool --cool-sha $SHA $L --out $E --tag 4t
   taskset -c 0 ~/stock4/bin/python scripts/vision_benchmark.py kernels --threads 1 $L --out $E --tag 4x-1t
   taskset -c 0 ~/stock4/bin/python scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C $L --out $E --tag 4x-1t
   for pair in "scenes opencv-1t cool-1t" "scenes opencv-4t cool-4t" "kernels opencv-1t-r1 cool-1t-r1" "scenes opencv-4x-1t opencv-1t"; do
     set -- $pair
     ~/stock/bin/python scripts/vision_benchmark.py compare $E/$D-bench-$1-$CPU-$2.json $E/$D-bench-$1-$CPU-$3.json --out $E --tag "$1-$2-vs-$3"
   done
   ~/cool/bin/python  scripts/vision_benchmark.py controls --cool-sha $SHA --backend cool $C $L --out $E
   ~/stock/bin/python scripts/vision_benchmark.py controls --cool-sha $SHA $C $L --out $E
   ```

   What to expect: a `kernels --backend cool` run on any build but the pinned one stops with
   `not_cool` before timing anything; the `scenes --backend cool` rows are `ok` only from the COOL
   venv (the handler's own guard answers `not_cool` elsewhere, and that row is what gets
   recorded). `compare` prints the speed-up of the second run over the first (median A / median
   B) with its bootstrap interval, the headline on the compute stages (`analyse` and `annotate`;
   the read is reported apart, and `graph` is inside `analyse`), each stage's share, and `parity:
   pass` when the rail metrics agree; it exits 1 when they do not, and refuses two runs from
   different machines or inputs. The **headline speed-up is the pinned one-core scenes pair on the
   compute stages**: it matches the deployed worker's one vCPU. The 4-thread pair shows what more
   cores add; the `4x-1t` pair says what OpenCV 5 changed against 4.14. `controls` from the COOL
   venv must show `ok`, `not_cool`, `backend_mismatch` (`as_expected` 3); from the stock venv
   `not_cool`, `not_cool`, `backend_mismatch` (also 3). Every `scenes` and `controls` run file
   lists the OpenCV shared objects mapped into the process (`environment.libraries`, with each
   file's sha256 and package) and the build lines that tell a vendor build apart.
9. **Optional: where the time went.**

   ```sh
   sudo perf record -g -o perf.data -- ~/cool/bin/python scripts/vision_benchmark.py scenes --repeats 1 --threads 1 $C --backend cool --cool-sha $SHA --only sydney-rail-2024-08-26
   perf report -i perf.data --sort dso --stdio > perf-dso.txt
   ```

   The share of samples in `libkleidicv*` and `libopencv*`/`cv2*.so` is direct evidence that the
   vendor's code executed. Commit `perf-dso.txt`, not `perf.data`.
10. **Optional, the container route** (when the product is an AMI): build
    `docker/vision-cool/Dockerfile` with the COOL image by digest and print `build_info()` inside
    it. The image's fingerprint may differ from the AMI's; record both, and say which one ran.
11. **Stop and commit.** Terminate the instance (or stop it, if step 12 follows within two days),
    and unsubscribe unless the subscription is kept. Record the instance hours and the software
    fee in `docs/hackathon/evidence/cool/session.md`. Commit the run files and figures from `$E`,
    and `docs/hackathon/evidence/cool/` (`recon.md`, `cool-buildinfo.txt`, `stock-buildinfo.txt`,
    `stock4-buildinfo.txt`, `lscpu.txt`, `session.md`, `perf-dso.txt` if made); then the
    tracker's C1, C2, C3 and S7 rows, and O5. Nothing in `$E` holds an account id, an instance id
    or an IP: `--imds` keeps five fields, and the run files say which.

    **What the session must leave behind** (the proof that COOL executed the claimed core workload,
    all committed):

    - [ ] the `scenes --backend cool` run files, with every scene `ok` and `environment.opencv.build_sha256`
      equal to `sha256sum cool-buildinfo.txt`, taken from the product's own `cv2`;
    - [ ] in those run files, `environment.libraries`: the `cv2` extension and the OpenCV and
      KleidiCV shared objects by path, size, sha256 and owning package or AMI path, and
      `environment.build_lines`;
    - [ ] optionally `perf-dso.txt` (step 9);
    - [ ] the CPU is Neoverse V2 (`lscpu.txt`, and `environment.cpu` in every run);
    - [ ] `environment.host.marketplace_product_codes` names the COOL product;
    - [ ] the two `controls` run files: `not_cool` under stock with the same pin, `not_cool` with
      no pin, `backend_mismatch` for an `opencv` request;
    - [ ] the `compare` run files with `parity: pass` for the stock-COOL pairs, on the same scene
      and network;
    - [ ] the speed-up on the compute stages, per stage, with its interval, from the same files;
    - [ ] optionally the agent session's replay run (step 12).

    **The claim's scope**, as the report words it: "the same worker handler, run on Graviton4 with
    COOL, on real Sydney scenes and its real OpenStreetMap network, and (if step 12 ran) inside the
    agent's loop; the deployed worker runs stock OpenCV 5 on Lambda arm64 (CPU named in its
    logs)". Not "COOL is deployed": a deployed COOL worker (Terraform for `VISION_COOL_WORKER_ARN`
    and a container Lambda or ECS) is out of scope until after the deadline, and whether COOL runs
    on Lambda's CPUs is the open question in [vision.md §10](enhancements/vision.md#10-still-open).
12. **Optional: the agent session (Wednesday 21, about an hour, once the evaluation's `replay`
    and `stability` exist).** On the same kind of instance, with the cache from step 8:
    - give the instance role, for this hour only, one statement: `bedrock:InvokeModel` on the one
      inference profile the topics use and its foundation models, and remove it afterwards;
    - `~/cool/bin/python scripts/vision_eval.py replay --sequence sydney-demo --agent bedrock --worker in-process --backend cool --cool-sha $SHA $C --out $E`:
      COOL then executes the agent's `look_again` re-measurements inside the agentic loop, in
      us-west-2, with production's deadline;
    - `~/stock/bin/python scripts/vision_eval.py stability --set year-2024 $C --out $E`: the year of
      clear Sydney scenes through the production rule, in-Region.

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
  in code. [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md) lists what the original
  repository uses.
