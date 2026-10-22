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
  - [The reviewer bear (on by default)](#the-reviewer-bear-on-by-default)
  - [Vision (optional)](#vision-optional): now its own runsheet,
    [vision-runsheet.md](vision-runsheet.md)
- [Deploying to another region](#deploying-to-another-region)
- [Tearing down and cost control](#tearing-down-and-cost-control)
  - [Keeping dev's content across a teardown](#keeping-devs-content-across-a-teardown)
- [Local development](#local-development)
- [What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment)
- [Sections that moved](#sections-that-moved): the rail topics and the evidence went to the
  [vision runsheet](vision-runsheet.md); the evaluation and the evidence export to the
  [evaluation runsheet](evaluation-runsheet.md); the COOL session to the
  [COOL benchmark runsheet](cool-benchmark-runsheet.md); the models to [models.md](models.md)

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
python scripts/setup_repo.py --only ALERT_EMAIL_DEV ALERT_EMAIL_PROD
```

What it does, in order:

1. Checks `gh` is installed and signed in, shows which repository it will change and asks you to
   confirm it. **In a fork, `gh` can pick the original repository. Read the name.**
2. Lists what is already set, by name. A secret's value cannot be read back, by anyone, and the
   script never tries. Settings that are already set are skipped unless you ask to replace them.
3. Asks for each setting that is left, with a short explanation and a check. The region comes
   first (`AWS_REGION`; press Enter for the default, `ap-southeast-2`). Next is the state
   bucket's region (`TF_STATE_REGION`; press Enter unless your state bucket is in another
   region). Then comes the name prefix (`UNIQUE_NAME_PREFIX`; a fork must choose its own),
   because the steps it prints later are built from the region and the prefix. Then the alert
   emails, the two account IDs,
   the two deploy role ARNs (with the steps to create them and the ARN it expects, which you can
   accept by pressing Enter), the state bucket names, and `PII_DENYLIST`. Press Enter to leave
   an optional setting unset.
4. Tells you how to turn on the git hook that stops personal data being committed, and offers to
   run that one command (`git config core.hooksPath .githooks`) for this clone. If you say yes, it
   runs with everything else, after you confirm. A dry run never runs it.
5. Explains the optional CoinGecko API key, which is kept in AWS and not on GitHub
   ([API keys for the data sources](#api-keys-for-the-data-sources)). It prints the command for
   you to run, with a placeholder where the key goes. **It does not store the key and never asks
   for it.** A run narrowed with `--only` skips this step.
6. Shows a summary. Secret values are masked: you see a length and the last two characters.
7. Asks once more, then sets everything.

**What "all or nothing" means here.** Nothing is set until every answer has passed its check and
you have confirmed the summary. Cancel at any point before that (Ctrl-C works at every question)
and nothing has changed. GitHub has no way to set several secrets as one step, though. If a write
fails part-way, the script stops at once and lists exactly which settings were set and which
were not. It does not try to put old values back: they cannot be read. Run it again and it picks
up what is missing.

The script sets up GitHub. It changes nothing in AWS: you still run the bootstrap in step 1
yourself. Its only AWS calls are three reads for step 5 (`aws --version`,
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
| `vision_region` | Where the vision worker runs. The deploy roles may create its Lambda functions and log groups (`<prefix>-*-vision-*`) there. Must be the same as each environment's `vision_region` ([vision-runsheet.md](vision-runsheet.md)). | `us-west-2` |
| `web_search_regions` | The Regions the deploy roles may create the AgentCore web search gateway in (a list). Must include each environment's `WEB_SEARCH_REGION` GitHub variable; leave it alone if you never set that variable ([The web search gateway's region](#the-web-search-gateways-region)). | `["ap-northeast-1"]` |

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
- Not your IP: the admin API's allowlist is an SSM parameter, set after the first deploy with
  `scripts/admin_allowlist.py` ([configuration.md](configuration.md#the-admin-apis-address-allowlist)). Until then nothing
  can call the admin API: it fails closed.

Dev's settings must be at repository level, because the dev job runs in no environment.
Production's can be on the `production` environment or on the repository.

Set `UNIQUE_NAME_PREFIX` **before your first deploy and never change it**. Every name starts
with it, and a bucket or a table cannot be renamed: changing the prefix later makes Terraform
delete them and create empty ones. Write it without a hyphen at the end (`acme-blog`, not
`acme-blog-`). Lowercase letters, digits and hyphens, starting with a letter, with no two
hyphens in a row. No `aws`, `amazon` or `cognito` anywhere in it, not even inside a word
(`jaws-news` is refused): the operator's assistant's sign-in host name is `<prefix>-<env>-ops`,
and Cognito refuses one that contains them. At most 14 characters: a topic's schedule is named
`<prefix>-production-<topic_id>-research-tick` and AWS allows 64, so a 14-character prefix leaves a topic id 24 and a shorter one leaves more. If the
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
```

That is enough to read outputs. To plan, also give Terraform the values CI gives it: the `env:`
of the `apply-dev` job in `.github/workflows/terraform.yml`. Any you leave out takes its
variable's default, and the plan then does not match what CI applies.

```bash
python scripts/minify_frontend.py   # builds frontend-dist/, which the site's files are planned from
export TF_VAR_aws_region=ap-southeast-2                                 # your AWS_REGION
export TF_VAR_unique_name_prefix=acme-blog                              # your UNIQUE_NAME_PREFIX
export TF_VAR_backup_bucket_name=yourname-bloggerbear-terraform-state   # your TF_STATE_BUCKET_DEV
export TF_VAR_alert_email=you@example.com                               # your ALERT_EMAIL_DEV, if set
export TF_VAR_aws_account_id=111111111111                               # your AWS_DEV_ACCOUNT_ID, if set
terraform -chdir=infra/environments/dev plan
```

- Without `TF_VAR_unique_name_prefix`, the plan uses `bloggerbear` and shows every
  `<prefix>-dev-*` resource being replaced.
- Without `TF_VAR_backup_bucket_name`, the plan points the backups at
  `bloggerbear-terraform-state` instead of your bucket.
- Without `TF_VAR_alert_email`, the plan removes the alert subscription.
- `TF_VAR_aws_account_id` changes nothing in the plan. It adds CI's account check.
- If you set any of CI's switches (`VISION_ENABLED`, `REVIEWER_ENABLED`,
  `WEB_SEARCH_REGION[_DEV]`, `WAF_*`), pass the matching variable too (`TF_VAR_vision_enabled`,
  `TF_VAR_reviewer_enabled`, `TF_VAR_web_search_region`, `TF_VAR_waf_regional_enabled`,
  `TF_VAR_waf_admin_allowlist`, `TF_VAR_waf_log_all_requests`), or the plan shows it going back
  to its default.

CI builds `frontend-dist/` (`frontend/` with its `.js` and `.css` minified) before every plan.
Without it, a local plan shows every file of the site being removed.

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
   The admin API only answers from the addresses in its allowlist, with your own AWS credentials.
   Set yours first, and again whenever your IP changes (no deploy needed):
   `python scripts/admin_allowlist.py --prefix <your prefix> set --env dev --my-ip`, adding
   `--region <your region>` before `set` too if your region is not `ap-southeast-2`. Both options
   belong to the script, not to `set`, so they must come before the subcommand. The script does
   not read either one from your environment
   ([configuration.md](configuration.md#the-admin-apis-address-allowlist)).
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
Lambdas read that one name. Without a key each adapter uses the keyless API. A CoinGecko key
that is rejected, rate-limited or failing at run time falls back to the keyless API. A GitHub
token falls back to unauthenticated search only when GitHub rejects it (401). If a search with
the token is rate-limited or fails, that tick records nothing, and the next heartbeat tries
again. Never put a key in a topic's `adapter_config`: that is stored in DynamoDB.

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
(`BEDROCK_MODEL_ID`, by default the AU Claude Haiku 4.5 inference profile). To change the model
without a deploy, give it a fallback, rotate between models on a topic, or see how the model
catalog discovers models and keeps their prices current by itself, read
**[models.md](models.md)**. The short version, for one environment:

```bash
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.001 --output-price 0.005
python scripts/admin_cli.py model-config set --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"
```

The registry and the default are per environment: do it again for production.

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
With vision on ([vision-runsheet.md](vision-runsheet.md)), it may also query the worker's log group,
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
winget install Gitleaks.Gitleaks              # or brew install gitleaks; v8.30.0 or newer
```

Then list your own personal strings (a name, a home IP, a personal address), one per line, in
`.pii-denylist` at the repo root. It is gitignored, and the hook refuses to commit it. For CI, put
the same list in the `PII_DENYLIST` repository secret. Neither check ever prints what it found, so
the logs stay safe to publish.

### The reviewer bear (on by default)

Professor Grizzlewick (`<prefix>-<env>-reviewer`, [reviewer-bear.md](enhancements/reviewer-bear.md))
is deployed with everything else: every second day at 05:00 Sydney he reviews what the admins have
not, and writes to the Musings page as himself.

**On his first run he works through the backlog as it stands.** Every article that has waited in the
inbox two days or more is his to decide: he may **approve** it (financial ones too, each flagged for
you to check), **send it back for a rewrite**, or **reject** one still wrong after three rewrites. If
you want to decide some yourself first, do so before his first run (he skips anything no longer
waiting), or turn his approvals off (`reviewer set --may-approve off`, or per topic). Anything that
has already waited 10 days or more goes to the de-fanged publish (task 5) instead.

- **Turn him off:** set the `REVIEWER_ENABLED` variable to `false` and deploy: his Lambda and
  schedule are removed. On the repository, it turns him off in dev and, at the next release, in
  production, unless the `production` environment sets its own value. On the `production`
  environment alone, it turns him off in production only. Dev's deploy runs in no GitHub
  environment, so it reads only the repository variable. To turn him off in dev alone, set
  `false` on the repository and `true` on the `production` environment.
- **Pause him, with no deploy:** `python scripts/admin_cli.py reviewer set --enabled off`.
- **Let him review but never approve:** `reviewer set --may-approve off` for the whole project, or
  per topic: `python scripts/admin_cli.py topics update <topic_id> --reviewer-may-approve off`.
- **Articles waiting 10 days or more** are published with their flagged sentences cut, or rejected
  (task 5): `reviewer set --defang-held off`, or `--defang-after-days N`, to change that. Every other
  task and limit is a setting too (`reviewer set --help`).
- **His financial approvals:** the assistant lists each one (`reviewer_financial_check`). Read it
  (`articles get <article_id>`), then `articles checked <article_id>` if it is fine, or
  `articles unpublish <article_id>` if not. Until you do, he takes it back down himself if readers
  downvote it with comments.
- **What he did:** his report on the Musings page; `moderation_decision` evidence lines with
  `by: reviewer`; the `reviewer-state` row in `<prefix>-<env>-model-config`.

### Vision (optional)

The vision worker, for the `satellite_vision` and `rail_access` topics (the Rail Access Monitor),
is **off by default** in both environments. Turning it on is a re-apply of the bootstrap by hand,
then the `VISION_ENABLED` repository variable set to `true` and a deploy. Every step, the rail
topics and the evidence they leave are in their own runsheet:
**[vision-runsheet.md](vision-runsheet.md)**.

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
- **The web search gateway** is in `ap-northeast-1` (Tokyo) unless you choose otherwise. AWS
  offers that tool in only three regions (`us-east-1`, `eu-west-1`, `ap-northeast-1`). It is its
  own setting, the `WEB_SEARCH_REGION` GitHub variable, per environment and independent of
  `AWS_REGION`: see [The web search gateway's region](#the-web-search-gateways-region).
- **The vision worker**, when it is on (`VISION_ENABLED`), is in `us-west-2`, beside the
  Sentinel-2 imagery, unless you choose otherwise. Its Lambda function, log group and artifacts
  bucket are made there. It is its own setting, the `vision_region` Terraform variable:
  `AWS_REGION` does not set it, and there is no GitHub variable for it. To move it, give the same
  value to the bootstrap (`-var="vision_region=..."`) and to each environment's
  `terraform.tfvars`, because the two must match
  ([vision-runsheet.md](vision-runsheet.md#1-re-apply-the-bootstrap-by-hand)). In production the
  prefix and `vision_region` must also fit the worker's bucket name ([step 2](#2-github-settings)).
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
  region in its place: in a `--region` flag, or in `AWS_REGION` / `AWS_DEFAULT_REGION`, which
  `admin_cli.py` reads. `admin_allowlist.py` and `evidence_export.py` read neither variable: they
  use `ap-southeast-2` unless you pass `--region`, even where a command does not show one. For
  `admin_allowlist.py` it goes before the subcommand
  (`python scripts/admin_allowlist.py --region eu-west-1 set --env dev --my-ip`).

### The web search gateway's region

The AgentCore web search gateway (`infra/modules/web-search`, the fallback search when GDELT
fails) runs in its own Region, because AWS offers the Web Search Tool in only `us-east-1`,
`eu-west-1` and `ap-northeast-1`. **With nothing set it is `ap-northeast-1` (Tokyo)**, the closest
to Sydney, and you need do nothing. To put it somewhere else, for example `eu-west-1` beside a
European deployment:

1. **Re-apply the bootstrap by hand, first** (it is never applied by CI), adding the Region to
   `web_search_regions`, with the same other variables as your first apply:
   `-var='web_search_regions=["ap-northeast-1","eu-west-1"]'`. Keep the old Region in the list
   until every environment has moved off it: the apply that moves the gateway also deletes the old
   one there. Check the plan changes only the deploy roles' policies. Two accounts: do this in each
   account whose environment moves.
2. **Set the GitHub variable.** `WEB_SEARCH_REGION_DEV` for dev (on the repository: the dev job
   runs in no environment), `WEB_SEARCH_REGION_PROD` for production (on the repository or the
   `production` environment), or the shared `WEB_SEARCH_REGION` for both. Each environment reads
   its own name first, then the shared one, then `ap-northeast-1`. Settings → Secrets and
   variables → Actions → Variables. A value other than the three Regions fails the plan, before
   anything changes.
3. **Deploy.** Dev applies on the next merge to `dev` that touches `infra/`, `lambdas/` or
   `frontend/` (or `workflow_dispatch` the Terraform workflow from `dev`); production at the next
   release ([production-runsheet.md](production-runsheet.md#the-web-search-gateways-region)). The
   apply replaces the gateway and its target in the new Region and hands the Lambdas its new URL
   and Region (`AGENTCORE_WEB_SEARCH_URL`, `AGENTCORE_WEB_SEARCH_REGION`). Nothing is stored in
   it, so nothing is lost; a fallback search made during the apply itself may fail.
4. Optionally, once no environment uses the old Region, re-apply the bootstrap without it.

To go back, unset the variable (or set it to `ap-northeast-1`) and deploy again. If an apply is
refused with `AccessDenied` on `bedrock-agentcore`, step 1 was missed or names a different Region.

## Tearing down and cost control

- **To taper, not stop:** lower a topic's cadence or research interval, or pause it
  (`topics pause <topic_id>`: its schedules are disabled and nothing of it runs until
  `topics resume`). Free, and reversible at once. `pipeline-config set --research-interval-hours`
  does it for every topic that has no interval of its own. A topic's own interval wins, so clear
  it first (`topics update <topic_id> --research-interval-hours ''`) if you want the pipeline
  value to apply.
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
   want the articles as plain files too, `backup export`. By default it writes them to the
   backup bucket, under `backup/<prefix>-dev/exports/`. To use a bucket of your own, first add it to dev's
   `backup_export_buckets` Terraform variable (in `infra/environments/dev/terraform.tfvars`) and
   deploy, then pass `--bucket <name>`. Any other bucket is refused.
2. Run the `destroy-dev` workflow.
3. **After the rebuild** (the next merge to `dev` applies it), copy your allowlist into the new
   admin API: `python scripts/admin_allowlist.py --prefix <your prefix> sync --env dev`, adding
   `--region <your region>` before `sync` too if your region is not `ap-southeast-2`. Your
   addresses survive the teardown, but the new API refuses every call until this runs. If your IP
   has changed since, run `set --env dev --my-ip` in place of `sync`, with the same options before
   it. Then point the CLI at the new Admin API URL and run
   `python scripts/admin_cli.py backup restore <key>`. Topics, gear, settings, costs,
   musings and articles come back; the topics' schedules are written and the published pages
   rendered again.

The same file restores into another region or account: copy it into that deployment's state
bucket under `backup/`, then restore it there. Snapshots are also taken weekly on their own.

## Local development

From the repo root. These are the core commands CI runs; the tests use mocked AWS (moto), so they need
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
bandit -r lambdas/ scripts/ -x lambdas/tests,scripts/tests --severity-level high --confidence-level high
```

Checkov scans the Terraform, from the repository root, with what `.checkov.yaml` runs and skips
(every skip there, and each `checkov:skip` comment on a resource, says why):

```bash
pip install checkov==3.3.25
checkov --config-file .checkov.yaml
```

```bash
terraform fmt -check -recursive infra/
for dir in infra/environments/*; do
  terraform -chdir="$dir" init -backend=false -input=false && terraform -chdir="$dir" validate
done
```

Turn on the commit hook too: [The personal-data and secret checks](#the-personal-data-and-secret-checks).
Every check CI runs, including Trivy, `terraform test` and the secret scans, with their pinned
versions, is in [ci-and-checks.md](ci-and-checks.md).

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
- **The web search gateway's region** (`ap-northeast-1` unless `WEB_SEARCH_REGION` says otherwise:
  [The web search gateway's region](#the-web-search-gateways-region)) and the privacy page's
  wording about where logs are kept (see the same section).
- **Repository protection** (rulesets, required reviewers, secret scanning) is set in GitHub, not
  in code. The archived [public-repo-runsheet.md](enhancements/archive/public-repo-runsheet.md) lists the settings a
  public copy should have.

## Sections that moved

These were part of this runsheet. Their headings stay here so that old links still land.

### `rail_access` topics

Now in the vision runsheet: [`rail_access` topics](vision-runsheet.md#rail_access-topics).

### Evidence: what the pipeline measured and decided

Now in the vision runsheet:
[Evidence](vision-runsheet.md#evidence-what-the-pipeline-measured-and-decided).

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

Now in the evaluation runsheet:
[The evaluation's real-data runs](evaluation-runsheet.md#the-evaluations-real-data-runs).

### The evidence export, the trace and the labels (O9)

Now in the evaluation runsheet:
[The evidence export](evaluation-runsheet.md#the-evidence-export-the-trace-and-the-labels-o9).
