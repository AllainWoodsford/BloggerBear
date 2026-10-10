# Deploying your own: separate AWS accounts

How to run dev in one AWS account and production in another. It is an addition to
[deployment-runsheet.md](deployment-runsheet.md), not a replacement: follow that guide, and use
this page wherever it says "one account or two".

No code changes are needed. Two accounts is a matter of applying the bootstrap twice and giving
GitHub two sets of values.

Account IDs on this page are placeholders: `111111111111` is the dev account and `123456789012`
is the production account. `your-name/your-fork` is your repository and `acme-blog` is your name
prefix. Use your own.

## Should you?

**One account is the simpler choice, and it is what the original deployment uses.** Names never
collide (`<prefix>-dev-...` and `<prefix>-production-...`), and one of everything below is enough.

Two accounts buy you isolation that one account cannot give:

- **Quotas.** The Lambda concurrency quota is per account. In one account, a busy dev can
  throttle production's pipeline.
- **Permissions.** In one account the two deploy roles are kept apart twice over. First, by who
  may use them: the dev role can only be assumed by a workflow on your `dev` branch, and the
  production role only from the `production` environment, after your approval. No access keys
  exist for either. Second, by what they may touch: each role carries a policy that refuses it
  the other environment's resources, by name and by tag
  ([how](#in-one-account-each-role-is-refused-the-other-environment)). That stops a deploy that
  is wrong from reaching the other environment. It is not a wall against someone who can change
  what the dev branch deploys. Two accounts are: a dev deploy then has no path to production's
  resources, whatever it is asked to do.
- **Teardown.** Destroying dev cannot touch production.
- **The bill.** Each account has its own, so you can see what dev costs.

What they cost you:

- Everything in [What you do twice](#what-you-do-twice).
- Dev's CloudFront distributions lose the shared firewall ([The firewall](#the-firewall)).
- Logs, alarms and dashboards are in two places.

## What you do twice

| What | Why it is per account |
|---|---|
| An admin identity with MFA (IAM or IAM Identity Center), and root MFA | You apply the bootstrap by hand in each account. |
| A budget alarm on the whole account | Set it up before anything is applied. Bootstrap's Bedrock budget is in addition to it, and is also per account. |
| The bootstrap apply, and its local state file | Bootstrap creates account-level things. Keep both state files safe; neither is in git. |
| The Terraform state bucket | Each bootstrap creates one, in its own account. **S3 bucket names are unique across all of AWS, so the two must have different names.** |
| The GitHub OIDC provider and the deploy roles | GitHub signs in to each account directly ([What the OIDC side looks like](#what-the-oidc-side-looks-like)). |
| API Gateway's CloudWatch Logs role | One setting per account and region. Bootstrap sets it. |
| The Lambda concurrency quota | A new account often allows only 10. Ask for more in both. |
| DynamoDB's tag-based access control | An account setting, per region: [check it in both](deployment-runsheet.md#dynamodb-tag-based-access-control-abac). |
| Access to the model in Bedrock | Run the `converse` test from [Before you start](deployment-runsheet.md#before-you-start) with each account's credentials. |
| The CoinGecko and GitHub API keys in SSM, if you use them | Each environment reads its own parameter, in its own account. |
| The bootstrap's `vision_region`, if you turn the vision worker on | The deploy roles' rights in that region are per account: [Vision (optional)](#vision-optional). |

Only one of these exists once: **the Route 53 hosted zone**, in the production account. Dev has
no custom domain.

## Bootstrap, once per account

Bootstrap deploys to **whichever account your local AWS credentials belong to** when you run it.
Nothing in the repository chooses the account for you. `aws_account_id` is a check, not a
selector: the apply refuses to run if your credentials are for a different account, which is
what stops the second apply landing in the first account because the wrong profile was still
selected.

Bootstrap keeps its state in a local file, so the two applies must not share one. A Terraform
workspace per account keeps them apart.

**The dev account first.** No domain, so no hosted zone is created here:

```bash
cd infra/bootstrap
terraform init
export AWS_PROFILE=acme-dev                       # however you sign in to the dev account
aws sts get-caller-identity --query Account       # expect 111111111111
terraform workspace new dev
terraform apply \
  -var="aws_account_id=111111111111" \
  -var="github_repo=your-name/your-fork" \
  -var="state_bucket_name=acme-blog-terraform-state-dev" \
  -var="unique_name_prefix=acme-blog" \
  -var="domain_name=" \
  -var="budget_alert_email=you@example.com"
terraform output        # keep dev_deploy_role_arn and state_bucket_name
```

**Then the production account**, with its own bucket name and your domain:

```bash
export AWS_PROFILE=acme-prod                      # the production account
aws sts get-caller-identity --query Account       # expect 123456789012
terraform workspace new production
terraform apply \
  -var="aws_account_id=123456789012" \
  -var="github_repo=your-name/your-fork" \
  -var="state_bucket_name=acme-blog-terraform-state-prod" \
  -var="unique_name_prefix=acme-blog" \
  -var="domain_name=example.com" \
  -var="budget_alert_email=you@example.com"
terraform output        # keep prod_deploy_role_arn, state_bucket_name, hosted_zone_id, hosted_zone_name_servers
```

What must be the same in both: `github_repo`, `unique_name_prefix`, and `aws_region` if you set
it. What must differ: `aws_account_id` and `state_bucket_name`.

If you give both applies the same bucket name, the second one fails at once with
`BucketAlreadyExists`. Nothing is harmed; choose another name and apply again.

**Each apply creates both deploy roles.** The dev account ends up with a
`gha-acme-blog-prod-deploy` role and the production account with a `gha-acme-blog-dev-deploy`
role. They are never used: GitHub is only ever given the dev role's ARN from the dev account and
the production role's ARN from the production account. You may leave them. Each still trusts
only your repository's `dev` branch or `production` environment.

To come back to one account's bootstrap later, select its workspace first
(`terraform workspace select dev`) and sign in to that account.

## GitHub settings that differ

`scripts/setup_repo.py` asks for all of these. Set by hand, they are under **Settings → Secrets and variables → Actions**; the
full table is in [configuration.md](configuration.md#github-secrets-and-variables).

| Setting | Dev (repository level) | Production (`production` environment) |
|---|---|---|
| Account ID | `AWS_DEV_ACCOUNT_ID` = `111111111111` | `AWS_PROD_ACCOUNT_ID` = `123456789012` |
| Deploy role | `AWS_DEV_DEPLOY_ROLE_ARN` = `arn:aws:iam::111111111111:role/gha-acme-blog-dev-deploy` | `AWS_PROD_DEPLOY_ROLE_ARN` = `arn:aws:iam::123456789012:role/gha-acme-blog-prod-deploy` |
| State bucket | `TF_STATE_BUCKET_DEV` = `acme-blog-terraform-state-dev` | `TF_STATE_BUCKET_PROD` = `acme-blog-terraform-state-prod` |

`UNIQUE_NAME_PREFIX` and `AWS_REGION` stay single values, shared by both.

**The role decides the account.** Each workflow assumes one role, and whatever account that role
lives in is the account Terraform changes. The account ID is then checked against it, and the
run stops before planning if they disagree.

**Running Terraform on your own machine.** The environments' `backend "s3"` blocks name the
original deployment's bucket, because a backend block cannot read a variable. CI passes yours to
`terraform init`; a local init needs the same flag, with the bucket for that environment:

```bash
cd infra/environments/dev
terraform init -backend-config="bucket=acme-blog-terraform-state-dev"
```

## What the OIDC side looks like

You do not write any of this: `infra/bootstrap` creates it in each account. It is shown here so
you can read what you are applying, check it in the IAM console afterwards, or build it yourself
if your organisation manages IAM some other way.

No access keys are stored anywhere. A workflow asks GitHub for a short-lived token that says
which repository, branch and environment it is running for. AWS checks that token against a
role's trust policy and, if it matches, hands back temporary credentials for that role.

**1. The identity provider**, one per account. It tells AWS to accept tokens GitHub has signed:

```hcl
resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}
```

**2. The dev role's trust policy**, in the dev account. Only a workflow run on your fork's `dev`
branch matches it:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::111111111111:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:your-name@*/your-fork@*:ref:refs/heads/dev"
      }
    }
  }]
}
```

**3. The production role's trust policy**, in the production account. Only a job running in your
fork's `production` GitHub environment matches it, which is the environment your approval gates:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::123456789012:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:your-name@*/your-fork@*:environment:production"
      }
    }
  }]
}
```

The owner and repository in both come from bootstrap's `github_repo` variable. **A fork must set
it**: the default is the original repository, and a role that trusts the original repository
refuses your workflows.

**About `your-name@*/your-fork@*`.** GitHub writes the `sub` claim in one of two forms:

| Your repository has | The `sub` GitHub sends for the `dev` branch |
|---|---|
| Immutable subject claims on | `repo:your-name@12345678/your-fork@987654321:ref:refs/heads/dev` |
| Immutable subject claims off | `repo:your-name/your-fork:ref:refs/heads/dev` |

Bootstrap writes the pattern for the first form, with `@*` standing in for the numeric IDs. To
see which your repository uses:

```bash
gh api repos/your-name/your-fork/actions/oidc/customization/sub
```

If that shows `"use_immutable_subject": true`, bootstrap's pattern matches as it is. If it does
not, the pattern has no `@` to match and every sign-in is refused: edit the two `sub` values in
`infra/bootstrap/main.tf` (`gha_dev_trust` and `gha_prod_trust`) to the plain form,
`repo:your-name/your-fork:ref:refs/heads/dev` and `repo:your-name/your-fork:environment:production`,
and apply bootstrap again in both accounts.

**4. The workflow side**, already in `.github/workflows/`. The permission lets the job ask GitHub
for a token, and the step exchanges it for the role's credentials:

```yaml
jobs:
  apply-dev:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@<pinned commit>
        with:
          aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}
          role-to-assume: ${{ secrets.AWS_DEV_DEPLOY_ROLE_ARN }}
```

Production's job is the same with `environment: production` on the job and
`AWS_PROD_DEPLOY_ROLE_ARN` as the role. The dev job deliberately runs in **no** GitHub
environment: putting it in one changes the `sub` GitHub sends from `ref:refs/heads/dev` to
`environment:<name>`, and the dev role would stop trusting it.

**When a sign-in is refused** the error is always the same, `Not authorized to perform
sts:AssumeRoleWithWebIdentity`, and never says why. Look in the account's CloudTrail event
history for `AssumeRoleWithWebIdentity`: the failed event shows the `sub` GitHub really sent.
The usual causes are `github_repo` left at its default, the wrong `sub` form, the role ARN of
one account stored under the other's secret, and a release published from outside the
`production` environment.

## In one account, each role is refused the other environment

This is what one account does in place of an account boundary. With two accounts it is still
applied, and has nothing to refuse.

Both deploy roles carry the same policy, which allows what a deploy needs on resources named
`<prefix>-*`. Bootstrap adds a second policy to each, and it only ever says no:

| Role | Its second policy | Refuses |
|---|---|---|
| `gha-<prefix>-dev-deploy` | `<prefix>-gha-dev-deploy-not-production` | anything of production's |
| `gha-<prefix>-prod-deploy` | `<prefix>-gha-production-deploy-not-dev` | anything of dev's |

"Anything of the other environment's" is decided three ways:

- **By name:** any resource whose name starts `<prefix>-production-` (or `<prefix>-dev-`):
  tables, functions, roles, queues, alarms, dashboards, schedules, log groups, buckets.
- **By tag:** any resource tagged `Environment = production` (or `dev`). This is for what has
  no name of yours in its address: CloudFront distributions, REST APIs, user pools,
  certificates. Every resource Terraform makes carries the tag.
- **Its Terraform state:** the other environment's key in the state bucket.

**Why it is written as "refuse" and not as two narrower "allow" policies.** A refusal that names
only the other environment cannot take away anything a role does in its own. So the permissions
your deploys depend on are exactly what they were, and adding this cannot be what breaks a
deploy. If something is not recognised it stays allowed: the worst case is "not separated",
never "refused".

**What both roles can still reach, on purpose:**

- The shared CloudFront web ACL (`<prefix>-shared`), its log group and its log policy.
  Production creates and owns them, so the production role must manage them. The dev role is not
  refused them either, because in one account dev's distributions may be attached to that ACL.
- Bootstrap's own resources: the state bucket (each role only its own key), the hosted zone and
  the OIDC provider.

**What it does not do.** The dev role can still write the policies of dev's own Lambda roles,
and a role it writes could be given more than the dev role has. So this protects against a
mistake (a wrong variable, the wrong directory, a careless destroy), not against someone who
can change what the dev branch deploys. That is what two accounts are for.

**If a deploy is ever refused by it,** the error says "explicit deny" and names one of the two
policies above. To get the deploy through first and investigate after, apply bootstrap again
with `-var="separate_environment_permissions=false"`, which detaches both.

**Do not name the state bucket for an environment under your prefix** (`<prefix>-dev-...` or
`<prefix>-production-...`): the name rule would refuse it to one of the roles. Bootstrap stops
with a message if you do. `<prefix>-terraform-state-dev` is fine.

## The firewall

There are two kinds of web ACL, and two accounts treats them differently.

| Web ACL | Scope | Created by | With two accounts |
|---|---|---|---|
| `<prefix>-shared`, in front of the CloudFront distributions | Global (CloudFront), kept in `us-east-1` | Production only | Protects production. **Dev cannot use it.** |
| `<prefix>-<env>-public-api` and `<prefix>-<env>-admin`, in front of the APIs | Regional | Each environment, for itself | Dev and production each have their own. Nothing is lost. |

The region is not what gets in the way. A CloudFront web ACL is global, but AWS WAF can only
attach it to distributions in the account that owns it.

- **Leave dev's `web_acl_arn` empty** in `infra/environments/dev/terraform.tfvars` (it is empty
  by default). If you set it to production's ACL and `AWS_DEV_ACCOUNT_ID` is set, the plan stops
  with a message saying the ACL is in another account.
- **What dev goes without:** the rate limit and AWS's managed rules at the edge, on its two
  CloudFront distributions (the site, and the public API's cache).
- **What dev keeps:** its own regional web ACLs on the public API and the admin API, the admin
  API's address allowlist, and the secret header that stops the public API being called around
  its CloudFront distribution.

## Logs, alarms and dashboards

- Each environment's log groups, alarms, dashboards and alert topic are in its own account. To
  look at production you sign in to the production account.
- Dev's dashboard has a panel for the shared web ACL. It stays empty, because those
  metrics are in the production account.
- The operator's assistant in each environment reads only its own account. Production's
  firewall review reads the shared ACL's logs, which are in its account already.
- Alert emails are separate settings (`ALERT_EMAIL_DEV`, `ALERT_EMAIL_PROD`), and each needs its
  subscription confirmed.

## Vision (optional)

The vision worker ([deployment-runsheet.md, Vision (optional)](deployment-runsheet.md#vision-optional))
is off by default and nothing of it exists until you turn it on. With two accounts:

- **The bootstrap, in each account you turn it on in.** The deploy roles' rights to create the
  worker (`<prefix>-*-vision-*` Lambda functions and log groups in `vision_region`, `us-west-2`
  by default) are part of each account's bootstrap. Re-apply it in the dev account before
  turning the worker on for dev, and in the production account before production; select the
  account's workspace first (`terraform workspace select dev`). Pass the same `vision_region` to
  both if you change it.
- **The variable, per environment.** `VISION_ENABLED` on the repository turns the worker on for
  dev (and for production, unless the `production` environment has its own value). Set it on
  the `production` environment alone to turn it on only there. Removing it, or setting anything
  but `true`, takes the worker down on the next apply; topics keep their state.

The worker in each account reads the same public imagery and holds no state; dev's and
production's never meet.

## Checklist before the first deploy

- [ ] Root MFA, an admin identity and a budget alarm in **both** accounts.
- [ ] Bootstrap applied in both, each in its own workspace, with different `state_bucket_name`s
      and the same `unique_name_prefix` and `github_repo`.
- [ ] `terraform output` from each kept: the dev role ARN and bucket from the dev account; the
      production role ARN, bucket, hosted zone ID and name servers from the production account.
- [ ] The six settings in [GitHub settings that differ](#github-settings-that-differ) set, each
      role ARN under its own account's secret.
- [ ] `web_acl_arn` empty in dev's `terraform.tfvars`.
- [ ] Lambda concurrency, DynamoDB tag-based access control and the Bedrock model checked in both.
- [ ] `gh api repos/your-name/your-fork/actions/oidc/customization/sub` agrees with the `sub`
      pattern in the trust policies.

Then carry on with [step 4 of the deployment runsheet](deployment-runsheet.md#4-first-deploy-dev).

## Enhancement: a deployment account that assumes a role in each environment's account

**Not built, and not tested.** This is a sketch for anyone who wants GitHub to trust one small
"deployment" account, which then assumes a role in the dev account and another in the production
account. It suits an AWS Organization where workload accounts must not have their own OIDC
provider. The two-account setup above is simpler: each account has its own OIDC provider and its
own deploy role, and GitHub assumes each one directly.

It is a third account, not a way to make one account safer. Dev and production are still in
separate accounts, so everything above about the firewall and the logs still applies.

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
