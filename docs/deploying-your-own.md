# Deploying your own copy

This is for someone who has forked BloggerBear and wants to run it in their own AWS account, or in
two accounts (one for dev, one for production). You do it by setting GitHub secrets and variables.
You should not need to edit Terraform, with the exceptions listed under
[What is still tied to the original deployment](#what-is-still-tied-to-the-original-deployment).

The original deployment sets none of the new settings. Every one of them is optional and, left
unset, does exactly what the code did before it existed.

Account IDs on this page are AWS's documentation placeholders (`111111111111`, `123456789012`).
Use your own.

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
- Bedrock model access in `ap-southeast-2` for the model you will use. The default is the AU
  Claude Haiku 4.5 inference profile.
- Terraform 1.10 or newer, and the AWS CLI.
- Your fork on GitHub, with Actions enabled, a `dev` branch and a `prod` branch.
- For production only: a domain you control. Production always uses a custom domain.

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
| `aws_region` | Region for the bootstrap resources. Leave it: see the last section. | `ap-southeast-2` |

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
| `TF_STATE_BUCKET_DEV` | variable | repo | The state bucket dev uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `TF_STATE_BUCKET_PROD` | variable | `production` | The state bucket production uses. **Required for a fork.** | `yourname-bloggerbear-terraform-state` |
| `UNIQUE_NAME_SUFFIX` | variable | repo | Added to the bucket names and the sign-in host name, which must be unique across all of AWS. **Required for a fork.** Lowercase letters, digits and hyphens, at most 20. | `-yourname` |
| `ADMIN_ALLOWED_CIDRS_DEV` | secret | repo | Your public IP, as a Terraform list. Without it nothing can call dev's admin API. | `["203.0.113.7/32"]` |
| `ADMIN_ALLOWED_CIDRS_PROD` | secret | `production` | The same, for production. | `["203.0.113.7/32"]` |
| `ALERT_EMAIL_DEV` | secret | repo | Where dev's alarm emails go. Optional. | `you@example.com` |
| `ALERT_EMAIL_PROD` | secret | `production` | Where production's alarm emails go. Optional. | `you@example.com` |

Why some are secrets and some are variables:

- A **variable** is printed in plain text in every step's log. On a public repository those logs
  are public.
- An AWS account ID is an identifier, not a credential. Knowing it does not let anyone in. This
  repository still keeps account IDs out of its logs and its code (the secret scanner refuses
  them), so the account IDs and the role ARNs, which contain one, are **secrets**. GitHub then
  masks them wherever a log would print them. Each also falls back to a variable of the same name,
  if you would rather not use a secret.
- The state bucket name and the name suffix are **variables**. They are bucket names, not
  identities, and they appear in the logs anyway.

Set `UNIQUE_NAME_SUFFIX` **before your first deploy and never change it**. A bucket cannot be
renamed: changing the suffix later makes Terraform delete the buckets and create empty ones.

## 3. One account or two

**One account** (what the original deployment does):

- Apply bootstrap once.
- Both role ARNs are in the same account. Both account IDs are the same. Both state bucket
  variables name the same bucket; dev and production use different keys inside it.
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
2. In the `apply-dev` job's log, the init step should say it is using your state bucket, and the
   plan should list resources named with your suffix.
3. To prove the account check works, set `AWS_DEV_ACCOUNT_ID` to a wrong 12-digit number and
   re-run. It must fail during the plan with "AWS account ID not allowed". Put the right value
   back. (The error names the account the role is really in.)
4. `aws sts get-caller-identity` with your own credentials shows which account you are looking
   at. The dev site's address is the `terraform output` of `infra/environments/dev`.
5. For production: set the domain in `infra/environments/production/terraform.tfvars` (below),
   merge `dev` into `prod`, publish a release tagged `v*` from `prod`, and approve the deployment.

To run Terraform for an environment from your own machine, give `init` the same bucket CI uses:

```bash
terraform -chdir=infra/environments/dev init -backend-config="bucket=yourname-bloggerbear-terraform-state"
```

## What is still tied to the original deployment

These need a hand edit in your fork, or cannot be changed yet.

- **Production's domain.** `infra/environments/production/terraform.tfvars` sets `domain_name` and
  `hosted_zone_id` to the original site's. Change both to yours (`hosted_zone_id` is a bootstrap
  output). A value in that file wins over anything CI passes, so it has to be edited there.
  Production cannot be deployed without a domain.
- **The site itself.** The pages, the privacy policy and some tests name `bloggerbear.com`.
- **The region.** `ap-southeast-2` is written into the workflows, the backends, the deploy roles'
  permissions and the default model. `us-east-1` is used only where CloudFront requires it (the
  certificate and the shared web ACL, in production). Moving region is a code change.
- **The backend blocks** in both environments name `bloggerbear-terraform-state`. Terraform does
  not allow a variable there. CI overrides the name with `TF_STATE_BUCKET_DEV` /
  `TF_STATE_BUCKET_PROD`; if you forget to set them, the init is refused, because that bucket is
  not yours.
- **The OIDC trust.** It is a bootstrap variable (`github_repo`), not a GitHub setting, because
  it is part of the roles. If you rename or transfer your fork, re-apply bootstrap.
- **Resource names** other than the three covered by `UNIQUE_NAME_SUFFIX` all start with
  `bloggerbear-`. They only need to be unique within an account, so they work as they are, but
  you cannot run two copies of the same environment in one account.
- **The model.** Leaving `bedrock_model_id` unset uses the AU Claude Haiku 4.5 inference profile
  in your account. To use another, set `bedrock_model_id` in the environment's
  `terraform.tfvars`.
- **Repository protection** (rulesets, required reviewers, secret scanning) is set in GitHub, not
  in code. `docs/todo/public-repo-runsheet.md` lists what the original repository uses.
