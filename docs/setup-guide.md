# Setup guide: your first deploy

This guide takes you from nothing installed to a running **dev** copy of this project in your own
AWS account. It is written for someone forking the project for the first time: it explains what
each tool and setting is for, then points to the detailed runsheets when you need more.

- Already comfortable with Terraform, AWS and GitHub Actions? The
  [deployment runsheet](deployment-runsheet.md) is the shorter version.
- Every setting, and where it goes, is in [configuration.md](configuration.md).
- Production and a custom domain come after this, in the [production runsheet](production-runsheet.md).

> **About the examples.** Everything in `<angle brackets>` is a placeholder for **your** value.
> The example after it shows the *shape*, not a real value. Never copy an account ID, ARN, bucket
> name or domain from this repository's history or its original deployment: those belong to someone
> else, and AWS will refuse them or, worse, point your deploy at the wrong place. Account IDs in the
> examples are AWS's documentation placeholders (`111111111111`, `123456789012`).

---

## 0. What you are building, in one paragraph

You fork the repository and run **one** Terraform apply from your own machine (the *bootstrap*).
It creates a bucket for Terraform's state, and two IAM roles that GitHub Actions may assume
through **OIDC**, so no AWS access keys are stored in GitHub. You then put a few names and IDs into
your fork's GitHub settings. From then on, every merge to your `dev` branch makes GitHub Actions
run the security scans, lint and tests, then apply Terraform to your dev environment. Nobody runs
`terraform apply` by hand again, apart from that first bootstrap.

```
your machine ──(once)── terraform apply infra/bootstrap ──> state bucket + OIDC provider + deploy roles
GitHub (merge to dev) ──OIDC──> assumes gha-<your-prefix>-dev-deploy ──> terraform apply dev
```

---

## 1. Fill in your values first

Decide these before you start, and write them down. Each one is used several times, and several
**must match exactly** in two places.

| Placeholder | What it is | Example shape | Rules |
|---|---|---|---|
| `<your-github-owner>/<your-fork>` | Your fork's `owner/repo` on GitHub | `octo-dev/my-blog-bot` | Exactly as GitHub shows it |
| `<your-prefix>` | What every AWS resource name starts with: `<your-prefix>-dev-topics` | `acme-blog` | Lowercase letters, digits, hyphens; starts with a letter; at most 14 characters; **no** hyphen at the end. **Never change it after the first deploy.** |
| `<your-region>` | The AWS region everything runs in | `ap-southeast-2` | Must offer Amazon Bedrock and the model you choose ([step 3](#3d-make-sure-your-account-can-call-the-model)) |
| `<your-dev-account-id>` | The 12-digit AWS account for dev | `111111111111` | `aws sts get-caller-identity` shows it |
| `<your-prod-account-id>` | The account for production (the same as dev's if you use one account) | `123456789012` | Same |
| `<your-state-bucket>` | The S3 bucket that holds Terraform's state | `acme-blog-tf-state-7f3k` | Unique across **all** of AWS: add something random |
| `<your-aws-profile>` | The AWS CLI profile on your machine with admin rights | `acme-admin` | Any name you like |
| `<your-email>` | Where alarms and budget alerts go | `you@example.com` | You must confirm a subscription email |
| `<your-ip>/32` | Your public IP, the only address allowed to call the admin API | `203.0.113.7/32` | `curl https://checkip.amazonaws.com` shows it |
| `<your-domain>` | Production only: a domain you own | `example.com` | Not needed for dev |

Two derived values you will see later:

- Dev deploy role ARN: `arn:aws:iam::<your-dev-account-id>:role/gha-<your-prefix>-dev-deploy`
  (shape: `arn:aws:iam::111111111111:role/gha-acme-blog-dev-deploy`)
- Production deploy role ARN: `arn:aws:iam::<your-prod-account-id>:role/gha-<your-prefix>-prod-deploy`

---

## 2. Install the tools

| Tool | Version | What it is for | Check |
|---|---|---|---|
| Git | any recent | Cloning your fork, branches | `git --version` |
| Terraform | 1.10 or newer | The one-time bootstrap, and reading outputs | `terraform version` |
| AWS CLI | v2 | Signing in to AWS, a few one-off commands | `aws --version` |
| Python | 3.11 or newer | The setup script and the admin CLI | `python --version` |
| GitHub CLI (`gh`) | any recent | The setup script sets your GitHub secrets with it | `gh --version` |

How to install them:

- **macOS** (with [Homebrew](https://brew.sh)):
  ```bash
  brew tap hashicorp/tap && brew install hashicorp/tap/terraform
  brew install awscli python@3.12 gh git
  ```
- **Windows** (PowerShell, with winget). Use Git Bash for the commands in this guide:
  ```powershell
  winget install Hashicorp.Terraform Amazon.AWSCLI Python.Python.3.12 GitHub.cli Git.Git
  ```
- **Linux**: Terraform from [HashiCorp's install page](https://developer.hashicorp.com/terraform/install),
  the AWS CLI from [AWS's install guide](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html),
  `gh` from [cli.github.com](https://cli.github.com), and Python from your package manager.

Then sign the GitHub CLI in: `gh auth login`.

---

## 3. Set up AWS

### 3a. The account(s)

You need one AWS account, or ideally two (dev and production apart, so a mistake in dev cannot
touch production). In each account:

1. Turn on **MFA for the root user**, and then stop using root.
2. Create a **budget alarm** before anything else (Billing → Budgets → Create budget → a monthly
   cost budget, alerting `<your-email>`). The project's own Bedrock budget is in addition to this.
3. Create an **admin identity for yourself** to use from your machine. Pick one:
   - **IAM Identity Center** (recommended): enable it, create a user, and give it the
     `AdministratorAccess` permission set on the account.
   - **An IAM user** with `AdministratorAccess` and MFA, if you cannot use Identity Center.

Admin rights are needed only for the bootstrap, which creates IAM roles. After that, GitHub deploys
with its own role, and your day-to-day use (the admin CLI) needs much less.

### 3b. Point the AWS CLI at your account

With IAM Identity Center:

```bash
aws configure sso --profile <your-aws-profile>
#   SSO start URL:  https://<your-identity-center-subdomain>.awsapps.com/start
#   SSO region:     <the region Identity Center lives in>
#   pick the account <your-dev-account-id> and the AdministratorAccess role
#   CLI default region: <your-region>
aws sso login --profile <your-aws-profile>
```

With an IAM user's access key (keep it on your machine only; never put it in GitHub):

```bash
aws configure --profile <your-aws-profile>
```

Then use the profile for the rest of this guide, and check which account you are in:

```bash
export AWS_PROFILE=<your-aws-profile>
export AWS_REGION=<your-region>
aws sts get-caller-identity          # "Account" must be <your-dev-account-id>
```

### 3c. Ask for enough Lambda concurrency

A new account often allows only **10** concurrent Lambda executions for the whole account. Ask for
more under Service Quotas → AWS Lambda → "Concurrent executions" (approval can take a day). It
works without the increase, but slowly.

### 3d. Make sure your account can call the model

The default model is an Australian Claude Haiku 4.5 inference profile, which exists only in
Australian regions. In another region, pick a model or inference profile that exists there, and
set it as described in [Deploying to another region](deployment-runsheet.md#deploying-to-another-region).
Prove the call works **before** the first deploy, because nothing checks it at deploy time:

```bash
aws bedrock-runtime converse --region <your-region> \
  --model-id <your-model-or-inference-profile-id> \
  --messages '[{"role":"user","content":[{"text":"Say hello"}]}]'
#   e.g. --model-id au.anthropic.claude-haiku-4-5-20251001-v1:0  (Australian regions)
```

An `AccessDeniedException` here usually means the model is not enabled for your account yet:
open Amazon Bedrock → Model access in `<your-region>` and request it.

---

## 4. Fork and clone

1. On GitHub, fork the repository to `<your-github-owner>/<your-fork>`. Enable Actions on the fork
   (the **Actions** tab asks you to the first time).
2. Make `dev` the default branch (Settings → General → Default branch), and create `prod`:
   ```bash
   git clone https://github.com/<your-github-owner>/<your-fork>.git
   cd <your-fork>
   git checkout dev
   git push origin dev:prod
   ```
3. Turn on the commit hook that stops personal data being committed:
   `git config core.hooksPath .githooks` ([why](deployment-runsheet.md#the-personal-data-and-secret-checks)).

---

## 5. Bootstrap AWS (the only manual Terraform apply)

This creates, in the account your profile points at:

- the **state bucket** `<your-state-bucket>`;
- the **GitHub OIDC identity provider** (`token.actions.githubusercontent.com`), which lets AWS
  trust short-lived tokens that GitHub Actions issues;
- two **deploy roles**:
  - `gha-<your-prefix>-dev-deploy`, which only workflow runs on your fork's `dev` branch may assume;
  - `gha-<your-prefix>-prod-deploy`, which only runs in your fork's `production` environment may assume.
  Both may only touch resources named `<your-prefix>-*`;
- optionally a Route 53 hosted zone (production's domain) and a Bedrock budget alarm.

```bash
cd infra/bootstrap
terraform init
terraform apply \
  -var="aws_region=<your-region>" \
  -var="aws_account_id=<your-dev-account-id>" \
  -var="github_repo=<your-github-owner>/<your-fork>" \
  -var="state_bucket_name=<your-state-bucket>" \
  -var="unique_name_prefix=<your-prefix>" \
  -var="domain_name=" \
  -var="budget_alert_email=<your-email>"
cd ../..
```

Read the plan before typing `yes`. Every name in it should start with `<your-prefix>`, or be
`<your-state-bucket>`. **Each of `github_repo`, `state_bucket_name`, `unique_name_prefix` and
`domain_name` defaults to the original deployment's value, so pass all four.** `domain_name=` (empty)
creates no hosted zone, which is what you want until you set up production.

Keep the outputs (`terraform -chdir=infra/bootstrap output` shows them again):

| Output | Shape | Goes into |
|---|---|---|
| `dev_deploy_role_arn` | `arn:aws:iam::111111111111:role/gha-<your-prefix>-dev-deploy` | GitHub secret `AWS_DEV_DEPLOY_ROLE_ARN` |
| `prod_deploy_role_arn` | `arn:aws:iam::111111111111:role/gha-<your-prefix>-prod-deploy` | GitHub secret `AWS_PROD_DEPLOY_ROLE_ARN` |
| `state_bucket_name` | `<your-state-bucket>` | GitHub secrets `TF_STATE_BUCKET_DEV` / `_PROD` |

The bootstrap keeps its own state in a **local** file, `infra/bootstrap/terraform.tfstate`. It is
gitignored. Back it up somewhere private; you need it to change the bootstrap later.

**Two accounts?** Apply the bootstrap once per account, with that account's profile, its own
`aws_account_id` and its own state bucket. Use the dev role from the dev account and the production
role from the production account. [One account or two](deployment-runsheet.md#3-one-account-or-two)
has the details.

---

## 6. Tell GitHub about your AWS setup

### 6a. The `production` environment

Create it under Settings → Environments → New environment → `production`, add yourself as a
**required reviewer**, and limit deployment tags to `v*`. Production then only deploys from a
release that you approve. (Dev needs no environment: the dev role trusts the `dev` branch itself.)

### 6b. Secrets and variables: the setup script

`scripts/setup_repo.py` asks for each setting, checks it, and sets it with `gh`. Start with a dry
run, which changes nothing, and **check the repository name it shows you**: in a fork, `gh` can
pick the original repository.

```bash
python scripts/setup_repo.py --dry-run --repo <your-github-owner>/<your-fork>
python scripts/setup_repo.py --repo <your-github-owner>/<your-fork>
```

What it asks for, and what to answer:

| Setting | Kind | Your answer |
|---|---|---|
| `AWS_REGION` | variable | `<your-region>` |
| `UNIQUE_NAME_PREFIX` | variable | `<your-prefix>`, **exactly** the word you gave the bootstrap |
| `ADMIN_ALLOWED_CIDRS_DEV` / `_PROD` | secret | `["<your-ip>/32"]` |
| `ALERT_EMAIL_DEV` / `_PROD` | secret | `<your-email>` |
| `AWS_DEV_ACCOUNT_ID` / `AWS_PROD_ACCOUNT_ID` | secret | `<your-dev-account-id>` / `<your-prod-account-id>` |
| `AWS_DEV_DEPLOY_ROLE_ARN` / `AWS_PROD_DEPLOY_ROLE_ARN` | secret | the bootstrap outputs |
| `TF_STATE_BUCKET_DEV` / `TF_STATE_BUCKET_PROD` | secret | `<your-state-bucket>` (one per account if you have two) |
| `PII_DENYLIST` | secret | optional: your own name, address and so on, one per line, so a pull request that adds one is refused |

Prefer to set them by hand? Settings → Secrets and variables → Actions. The full table, with what
each one is for and why some are secrets, is in
[configuration.md](configuration.md#github-secrets-and-variables). Dev's settings must be at
repository level; production's can be on the `production` environment.

### 6c. How the pieces connect (the IAM and GitHub setup in one picture)

```
GitHub Actions run on <your-fork>, branch dev
  │  asks GitHub for an OIDC token: "repo <your-github-owner>/<your-fork>, ref refs/heads/dev"
  ▼
AWS STS AssumeRoleWithWebIdentity(role = AWS_DEV_DEPLOY_ROLE_ARN)
  │  the role's trust policy accepts only that repo + that branch (prod role: only the
  │  `production` environment), and only for the audience sts.amazonaws.com
  ▼
short-lived credentials (about an hour) ──> terraform init (state in TF_STATE_BUCKET_DEV)
  ──> terraform plan/apply, refused if the account is not AWS_DEV_ACCOUNT_ID
  ──> may only create or change resources named <your-prefix>-*
```

Nothing long-lived ever leaves AWS. If someone copied your workflow into another repository, AWS
would refuse its token, because the repository in the token would not match.

---

## 7. The first deploy

1. Make any small change on a branch off `dev` (a line in a doc is enough), open a pull request
   into `dev`, and watch the checks: the security scans, lint, tests,
   Terraform validate, and the secret and personal-data scans. They need no AWS access.
2. Merge it. The `terraform` workflow runs the same checks again, assumes the dev role and applies
   `infra/environments/dev`. The first apply creates everything (tables, buckets, Lambdas, APIs,
   web ACLs, dashboards, the static site). It takes about 10 to 20 minutes, mostly CloudFront.
3. Confirm the alarm subscription email AWS sends to `<your-email>`.
4. Read your addresses from the dev state:
   ```bash
   terraform -chdir=infra/environments/dev init \
     -backend-config="bucket=<your-state-bucket>" \
     -backend-config="region=<your-region>"
   terraform -chdir=infra/environments/dev output
   ```
   The output lists the site's address (a `https://<something>.cloudfront.net` URL), the admin API
   URL and the operator's assistant's settings.

---

## 8. Seed your first topic

The site is empty until a topic exists. The admin CLI talks to your admin API, signed with your
own AWS credentials, and only from `<your-ip>`:

```bash
pip install -r scripts/requirements.txt
export AWS_REGION=<your-region>
export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/dev output -raw admin_api_url)

python scripts/admin_cli.py topics create \
  --topic-id <your-topic-id> --name "<Your Topic Name>" --adapter hacker_news
#   e.g. --topic-id hn-daily --name "Hacker News Daily"

python scripts/admin_cli.py topics trigger <your-topic-id> --pipeline research_tick
python scripts/admin_cli.py topics trigger <your-topic-id> --pipeline daily_cycle
python scripts/admin_cli.py inbox
```

(`BLOGGERBEAR_ADMIN_API_URL` is the variable's real name in the code; keep it as written.)

From here the topic runs by itself on its schedules. The [admin CLI quick start](../scripts/QUICKSTART.md)
covers the next commands; the [CLI reference](../scripts/README.md) has all of them.

Optional, and done in AWS, never in GitHub: API keys for the data sources (a CoinGecko key, a
GitHub token) are SSM SecureStrings named `/<your-prefix>/dev/coingecko-api-key` and
`/<your-prefix>/dev/github-api-token`. [How to store them](deployment-runsheet.md#api-keys-for-the-data-sources).

---

## 9. When something goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Bootstrap: `EntityAlreadyExists` for the OIDC provider | The account already has GitHub's OIDC provider (one is allowed per account) | Import it, then apply again: `terraform -chdir=infra/bootstrap import aws_iam_openid_connect_provider.github_actions arn:aws:iam::<your-dev-account-id>:oidc-provider/token.actions.githubusercontent.com` |
| Bootstrap: `BucketAlreadyExists` | Someone, anywhere, already has that bucket name | Pick a more unique `<your-state-bucket>` |
| Deploy: `Not authorized to perform sts:AssumeRoleWithWebIdentity` | The token's subject does not match the role's trust policy: wrong `github_repo` in the bootstrap, a run not on `dev`, or a different subject format | Check `github_repo` was `<your-github-owner>/<your-fork>`. CloudTrail's failed `AssumeRoleWithWebIdentity` event shows the exact subject GitHub sent. The roles expect GitHub's immutable form, `repo:<owner>@<owner-id>/<repo>@<repo-id>:ref:refs/heads/dev`; if yours is the plain `repo:<owner>/<repo>:ref:refs/heads/dev`, `gh api repos/<your-github-owner>/<your-fork>/actions/oidc/customization/sub` shows the repository's setting |
| Deploy: `AccessDenied` creating resources | `UNIQUE_NAME_PREFIX` differs from the bootstrap's `unique_name_prefix`, or `AWS_REGION` differs from its `aws_region` | Make them the same word / region |
| Deploy: "AWS account ID not allowed" | The role is in a different account than `AWS_DEV_ACCOUNT_ID` | Fix whichever is wrong; the error names the real account |
| Admin CLI: 403 | Your IP changed, or is not in `ADMIN_ALLOWED_CIDRS_DEV` | Update the secret and merge any change to redeploy |
| Articles never appear; the daily cycle errors | The model cannot be called in your region | Re-run [step 3](#3d-make-sure-your-account-can-call-the-model) |
| `gh` set secrets on the wrong repository | `gh` picked the upstream in a fork | Re-run the script with `--repo <your-github-owner>/<your-fork>` |

More, from building and running the original: [friction.md](friction.md).

---

## 10. Where next

- **Production and your domain:** [production-runsheet.md](production-runsheet.md).
- **Protect your branches** (pull requests only, no force-pushes): [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md).
- **The operator's assistant** (`<site>/ask.html`): create a user as in
  [After the first deploy](deployment-runsheet.md#5-after-the-first-deploy), step 5.
- **Costs and tearing down:** [Tearing down and cost control](deployment-runsheet.md#tearing-down-and-cost-control).
  The `destroy-dev` workflow removes dev completely.
- **Data you publish:** keep each data source's credits; see
  [Data sources and attribution](../README.md#data-sources-and-attribution).
