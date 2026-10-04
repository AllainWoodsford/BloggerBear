# Deploying your own copy

This is for someone who has forked BloggerBear and wants to run it in their own AWS account, or in
two accounts (one for dev, one for production). You do it by setting GitHub secrets and variables.
You should not need to edit Terraform, with the exceptions listed under
[What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment).

The original deployment sets none of the new settings. Every one of them is optional and, left
unset, does exactly what the code did before it existed.

Account IDs on this page are AWS's documentation placeholders (`111111111111`, `123456789012`).
Use your own.

## Quick start: the setup script

`scripts/setup_repo.py` asks for every setting on this page, checks each answer, shows what is
already set on your repository, and then sets what is missing. It needs Python 3.11 or newer and
the GitHub CLI (`gh`), signed in (`gh auth login`). Nothing else: no AWS credentials, no packages.

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
   first (`AWS_REGION`, a plain variable; press Enter for the default, `ap-southeast-2`),
   because the steps it prints later name it. If you choose another region it lists what else
   you must change by hand (see [Deploying to another region](#deploying-to-another-region))
   and adds the region to the bootstrap command it shows. Then: your allowed
   address ranges, the alert emails, the two account IDs, the two deploy role ARNs (with the
   steps to create them and the ARN it expects, which you can accept by pressing Enter), the
   state bucket names, the name suffix, and `PII_DENYLIST`. Press Enter to leave an optional
   setting unset.
4. Shows a summary. Secret values are masked: you see a length and the last two characters.
5. Asks once more, then sets everything.
6. Tells you how to turn on the git hook that stops personal data being committed, and offers to
   run that one command (`git config core.hooksPath .githooks`) for this clone.

**What "all or nothing" means here.** Nothing is set until every answer has passed its check and
you have confirmed the summary. Cancel at any point before that (Ctrl-C works at every question)
and nothing has changed. GitHub has no way to set several secrets as one step, though. If a write
fails part-way, the script stops at once and lists exactly which settings were set and which
were not. It does not try to put old values back: they cannot be read. Run it again and it picks
up what is missing.

A dry run cannot set anything: one function does all the writing, a dry run never reaches it,
and a dry run's `gh` calls are limited to ones that only read.

The script sets up GitHub. It does not touch AWS: you still run the bootstrap in step 1 yourself.
It also needs the `production` environment to exist (step 2) before it can put production's
secrets there, and tells you if it does not.

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

## Prerequisites

- An AWS account (or two) with root MFA on, and an admin identity you can use from your own
  machine for the one-time bootstrap.
- A budget alarm on each account, before anything else.
- Bedrock model access, in the region you deploy to, for the model you will use. The default is
  the AU Claude Haiku 4.5 inference profile, which exists only in Australian regions: see
  [Deploying to another region](#deploying-to-another-region).
- Terraform 1.10 or newer, and the AWS CLI.
- Your fork on GitHub, with Actions enabled, a `dev` branch and a `prod` branch.
- For production only: a domain you control, bought from any registrar. Production always uses a
  custom domain. You do not move the domain to AWS: you point its name servers at a Route 53 zone
  (the original deployment's registrar is GoDaddy, which the runsheet uses as its worked example).
- Room in your Lambda quota. A new AWS account often allows only 10 concurrent Lambda executions
  for the whole account. Everything here runs within that, but it is shared by every function in
  both environments: ask for more under Service Quotas > AWS Lambda > "Concurrent executions"
  before you rely on the site.

## 1. Bootstrap, once, by hand

`infra/bootstrap` creates the Terraform state bucket, the GitHub OIDC provider, the two deploy
roles and (if you give it a domain) the Route 53 hosted zone. CI never applies it.

```bash
cd infra/bootstrap
terraform init
terraform apply \
  -var="aws_account_id=111111111111" \
  -var="github_repo=your-name/your-fork" \
  -var="state_bucket_name=yourname-bloggerbear-terraform-state" \
  -var="domain_name=example.com" \
  -var="budget_alert_email=you@example.com"
```

| Variable | What it is | Default |
|---|---|---|
| `aws_account_id` | The account you mean to apply to. The apply refuses any other. | empty: no check |
| `github_repo` | `owner/repo` allowed to assume the deploy roles. **A fork must set this**, or the roles trust the original repository and your workflows are refused. | the original repository |
| `state_bucket_name` | The state bucket. Bucket names are unique across all of AWS, so **a fork must choose its own**. | `bloggerbear-terraform-state` |
| `domain_name` | Your site's domain. Creates the hosted zone. Pass `""` for no zone. **A fork must set this**, or it creates a zone for the original domain. | `bloggerbear.com` |
| `budget_alert_email` | Where the Bedrock budget alert goes. Empty creates no budget. | empty |
| `aws_region` | Region for the state bucket, and the region the deploy roles are allowed to work in. Must be the same as the `AWS_REGION` GitHub variable below. | `ap-southeast-2` |

Keep the outputs: `state_bucket_name`, `dev_deploy_role_arn`, `prod_deploy_role_arn`,
`hosted_zone_id`, `hosted_zone_name_servers`.

Bootstrap keeps its state in a local file. Keep that file somewhere safe; it is not in git.

## 2. GitHub settings

Create these under **Settings → Secrets and variables → Actions**. Create the `production`
environment under **Settings → Environments**, with yourself as a required reviewer and deployments
limited to `v*` tags.

"Repo" means repository level. Dev's settings must be at repository level, because the dev job
runs in no environment. Production's can be on the `production` environment or on the repository.

| Name | Kind | Where | What it is | Example |
|---|---|---|---|---|
| `AWS_DEV_DEPLOY_ROLE_ARN` | secret | repo | Bootstrap's `dev_deploy_role_arn` output. Required. | `arn:aws:iam::111111111111:role/gha-bloggerbear-dev-deploy` |
| `AWS_PROD_DEPLOY_ROLE_ARN` | secret | `production` | Bootstrap's `prod_deploy_role_arn` output. Required. | `arn:aws:iam::123456789012:role/gha-bloggerbear-prod-deploy` |
| `AWS_DEV_ACCOUNT_ID` | secret | repo | The account dev must land in. Optional, recommended. | `111111111111` |
| `AWS_PROD_ACCOUNT_ID` | secret | `production` | The account production must land in. Optional, recommended. | `123456789012` |
| `TF_STATE_BUCKET_DEV` | secret | repo | The state bucket dev uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `TF_STATE_BUCKET_PROD` | secret | `production` | The state bucket production uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `AWS_REGION` | variable | repo | The region everything is deployed to. Optional: unset, it is `ap-southeast-2`. See [Deploying to another region](#deploying-to-another-region) before setting it. | `eu-west-1` |
| `TF_STATE_REGION` | variable | repo | The region of the state bucket, only if it is not `AWS_REGION`. Optional, and rarely needed. | `eu-west-1` |
| `UNIQUE_NAME_SUFFIX` | variable | repo | Added to the bucket names and the sign-in host name, which must be unique across all of AWS. **Required for a fork.** Lowercase letters, digits and hyphens, at most 20. | `-yourname` |
| `ADMIN_ALLOWED_CIDRS_DEV` | secret | repo | Your public IP, as a Terraform list. Without it nothing can call dev's admin API. | `["203.0.113.7/32"]` |
| `ADMIN_ALLOWED_CIDRS_PROD` | secret | `production` | The same, for production. | `["203.0.113.7/32"]` |
| `ALERT_EMAIL_DEV` | secret | repo | Where dev's alarm emails go. Optional. | `you@example.com` |
| `ALERT_EMAIL_PROD` | secret | `production` | Where production's alarm emails go. Optional. | `you@example.com` |
| `PII_DENYLIST` | secret | repo | Your own personal strings (a name, a home address), one per line. The `pii-denylist` check refuses a pull request that adds one. Optional. | not shown, on purpose |

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
- The role ARNs and the alert emails still fall back to a variable of the same name. That is
  left over from when they were variables; use the secret.
- The name suffix is a **variable**. It is part of your bucket names and your sign-in address,
  which are public anyway.
- The region is a **variable** too. It is not a secret: it is part of every address and resource
  name the logs print. `AWS_REGION` and `TF_STATE_REGION` are read at repository level; the
  `production` environment may set its own, which wins for production.

Set `UNIQUE_NAME_SUFFIX` **before your first deploy and never change it**. A bucket cannot be
renamed: changing the suffix later makes Terraform delete the buckets and create empty ones.

## 3. One account or two

**One account** (what the original deployment does):

- Apply bootstrap once.
- Both role ARNs are in the same account. Both account IDs are the same. Both state bucket
  secrets name the same bucket; dev and production use different keys inside it.
- Dev may share production's CloudFront web ACL: after production's first apply, put its
  `wafv2_web_acl_arn` output in `infra/environments/dev/terraform.tfvars` as `web_acl_arn`.

**Two accounts:**

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

## 4. Deploy and check it worked

1. Merge anything under `infra/`, `lambdas/` or `frontend/` to `dev`. The `terraform` workflow
   applies dev.
2. In the `apply-dev` job's log, the init step should succeed (your state bucket's name shows as
   `***`, because it is a secret), and the plan should list resources named with your suffix.
3. To prove the account check works, set `AWS_DEV_ACCOUNT_ID` to a wrong 12-digit number and
   re-run. It must fail during the plan with "AWS account ID not allowed". Put the right value
   back. (The error names the account the role is really in.)
4. `aws sts get-caller-identity` with your own credentials shows which account you are looking
   at. The dev site's address is the `terraform output` of `infra/environments/dev`.
5. For production: set the domain in `infra/environments/production/terraform.tfvars` (below),
   merge `dev` into `prod`, publish a release tagged `v*` from `prod`, and approve the deployment.

To run Terraform for an environment from your own machine, give `init` the same bucket CI uses
(and the bucket's region, if you deploy outside `ap-southeast-2`), and give `plan` the region:

```bash
terraform -chdir=infra/environments/dev init \
  -backend-config="bucket=yourname-bloggerbear-terraform-state" \
  -backend-config="region=eu-west-1"
TF_VAR_aws_region=eu-west-1 terraform -chdir=infra/environments/dev plan
```

## 5. After the first deploy

Dev is up once the `terraform` workflow goes green. What is left is by hand, and none of it is in
GitHub's settings.

1. **Find your addresses.** `terraform -chdir=infra/environments/dev output` (after the `init`
   shown above) prints the site's address, the public and admin API URLs, and the assistant's
   `ops_ask_url`, `ops_mcp_url`, `ops_user_pool_id`, `ops_app_client_id` and
   `ops_hosted_ui_domain`.
2. **Confirm the alert email.** AWS sends a confirmation to `ALERT_EMAIL_DEV`; nothing is
   delivered until you click it.
3. **Point the admin CLI at dev and seed a topic** (`scripts/QUICKSTART.md`, README step 6):
   ```bash
   export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/dev output -raw admin_api_url)
   python scripts/admin_cli.py topics list
   ```
   The admin API only answers from the addresses in `ADMIN_ALLOWED_CIDRS_DEV`, with your own AWS
   credentials. A topic creates its own schedules; trigger it once by hand to see it work.
4. **Optional: a CoinGecko API key**, if you run a crypto topic. It is not a GitHub secret: the
   Lambda reads it at run time from SSM Parameter Store, a SecureString named
   `/bloggerbear/dev/coingecko-api-key` (production: `/bloggerbear/production/coingecko-api-key`).
   Without one the adapter uses CoinGecko's keyless public API.
   ```bash
   aws ssm put-parameter --name /bloggerbear/dev/coingecko-api-key \
     --type SecureString --value <key> --overwrite
   ```
   In Git Bash on Windows put `MSYS_NO_PATHCONV=1` in front, or the leading `/` is rewritten.
5. **The operator assistant** (dev only for now): a sign-in page at `<dev site>/ask.html` in front
   of an agent that reports what needs your attention and shows the `admin_cli` command for each
   thing. It never runs anything. It has its own user pool, with self sign-up off, so create your
   user by hand:
   ```bash
   aws cognito-idp admin-create-user --user-pool-id <ops_user_pool_id> \
     --username <a name> --temporary-password <one>
   ```
   Sign in on the page to set a password. To restrict it to your own addresses, or switch it off,
   without a deploy: `python scripts/admin_cli.py pipeline-config set --assistant-access allowlist`
   (or `off`; `open` is the default).
6. **Production and your domain:** follow [production-runsheet.md](production-runsheet.md). It
   covers the DNS zone, pointing your registrar at it, the first release, and what to check after.

If you keep the data sources this project ships with, keep their credits too: see
[Data sources and attribution](../README.md#data-sources-and-attribution) in the README.

## Deploying to another region

The original deployment is in `ap-southeast-2` (Sydney), and that is what you get with nothing
set. To deploy somewhere else, choose the region **before your first deploy**. AWS cannot move a
resource between regions: changing the region of a deployment that already exists makes Terraform
plan to build everything again in the new one, and the data does not follow.

What to set:

1. **Bootstrap:** apply it with `-var="aws_region=eu-west-1"` (your region). The state bucket is
   created there, and the deploy roles are only allowed to work there. If the roles were made for
   another region, every apply is refused with `AccessDenied`.
2. **GitHub:** set the `AWS_REGION` variable to the same region (`scripts/setup_repo.py` asks for
   it). The workflows pass it to
   Terraform (as `aws_region`), to the AWS credentials step, and to `terraform init` as the state
   bucket's region. Set `TF_STATE_REGION` as well only if your state bucket is somewhere other
   than `AWS_REGION`.
3. **The model. This one is easy to miss.** The default model is an inference profile whose id
   starts with `au.`, and that profile exists only in Australian regions. Outside Australia, add
   a line to each environment's `terraform.tfvars` naming the profile for your geography, for
   example `bedrock_inference_profile_id = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"`.
   The prefix is `us.`, `eu.`, `apac.`, `global.` and so on;
   `aws bedrock list-inference-profiles --region eu-west-1` lists what your region offers. The
   model must also be enabled for your account in that region (Bedrock model access). Nothing
   checks either of these at deploy time: a wrong id, or a model that is not enabled, shows up as
   an error the first time the pipeline calls the model. Test it first with
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
- **The schedules** run on `Australia/Sydney` time. That is a time zone, not a region.

Check by hand, because nothing in this repository can:

- The Lambda Web Adapter layer version pinned in `infra/modules/ops-assistant/main.tf` must have
  been published in your region. Its README lists the regions.
- Every service used (Bedrock, Cognito, EventBridge Scheduler, Step Functions, WAF) must be
  offered in your region.
- `frontend/privacy.html` tells readers the logs are kept in AWS's Sydney region. Change the
  wording to match where yours are.
- The helper commands in `README.md` and `scripts/QUICKSTART.md` name `ap-southeast-2`. Use your
  region in its place (`--region`, or `AWS_REGION` / `AWS_DEFAULT_REGION`).

## What is still tied to the original deployment

These need a hand edit in your fork, or cannot be changed yet.

- **Production's domain.** `infra/environments/production/terraform.tfvars` sets `domain_name` and
  `hosted_zone_id` to the original site's. Change both to yours (`hosted_zone_id` is a bootstrap
  output). A value in that file wins over anything CI passes, so it has to be edited there.
  Production cannot be deployed without a domain. Any registrar will do: see
  [production-runsheet.md](production-runsheet.md) for pointing it at the zone.
- **The site itself.** The pages, the privacy policy and some tests name `bloggerbear.com`.
- **The backend blocks** in both environments name `bloggerbear-terraform-state` in
  `ap-southeast-2`. Terraform does not allow a variable there. CI overrides the name with
  `TF_STATE_BUCKET_DEV` / `TF_STATE_BUCKET_PROD`, and the region with `AWS_REGION` (or
  `TF_STATE_REGION`); if you forget the bucket, the init is refused, because that bucket is not
  yours.
- **The OIDC trust.** It is a bootstrap variable (`github_repo`), not a GitHub setting, because
  it is part of the roles. If you rename or transfer your fork, re-apply bootstrap.
- **Resource names** other than the three covered by `UNIQUE_NAME_SUFFIX` all start with
  `bloggerbear-`. They only need to be unique within an account, so they work as they are, but
  you cannot run two copies of the same environment in one account.
- **The model.** Leaving `bedrock_model_id` unset uses the AU Claude Haiku 4.5 inference profile
  in your account and region. Outside Australia that profile does not exist: set
  `bedrock_inference_profile_id` in the environment's `terraform.tfvars` (see
  [Deploying to another region](#deploying-to-another-region)). To use a different model
  altogether, set `bedrock_model_id` there.
- **The web search gateway's region** (`ap-northeast-1`) and the privacy page's wording about
  where logs are kept: see the same section.
- **Repository protection** (rulesets, required reviewers, secret scanning) is set in GitHub, not
  in code. `docs/todo/public-repo-runsheet.md` lists what the original repository uses.
