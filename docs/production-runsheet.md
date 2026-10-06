# Production runsheet

Everything between "dev works" and "your domain serves the site", in order, with the exact commands.
Each step says who does it and how to tell it worked. Run the steps top to bottom; tick them off as
you go.

It is written for anyone running their own copy. Where the original deployment's values appear
(`bloggerbear.com`, GoDaddy as the registrar), they are examples: use your own domain and whichever
registrar you bought it from. If you have not set up dev yet, do
[deployment-runsheet.md](deployment-runsheet.md) first; this picks up where it stops.

Wherever it says **check it**, `python scripts/domain_check.py` is the read-only tool that tells you where
the domain stands. It reads the domain from production's `terraform.tfvars` (or pass one:
`python scripts/domain_check.py example.com`), needs no AWS login and changes nothing.

## Before you start

| You need | How to tell |
|---|---|
| Dev deployed and working | The `terraform` workflow's last run on `dev` is green, and the dev site loads. |
| Bootstrap applied (state bucket, GitHub OIDC provider, both deploy roles) | `terraform -chdir=infra/bootstrap output` lists `dev_deploy_role_arn` and `prod_deploy_role_arn`. |
| A domain you control, at any registrar | You can sign in to the registrar and change the domain's name servers. |
| The GitHub settings for production | `python scripts/setup_repo.py --dry-run` shows what is set and what is missing. |
| A `prod` branch and a `production` GitHub environment | Step 5 creates both if they are missing. |
| The model available to your account in your region | The `converse` test in [deployment-runsheet.md](deployment-runsheet.md#before-you-start) answers. |
| DynamoDB tag-based access control on, in the production account and region | DynamoDB console > Settings. The assistant's `table_sample` tool needs it: [why](deployment-runsheet.md#dynamodb-tag-based-access-control-abac). |

Two things about the account that are worth knowing before the first release:

- **Lambda concurrency.** A new AWS account often has a quota of only 10 concurrent Lambda executions
  for the whole account, shared by every function in dev and production. The site works within it,
  but nothing can be reserved for one function (Lambda refuses: friction log 10.17), and a burst
  can throttle the pipeline. Ask for more under Service Quotas > AWS Lambda > "Concurrent executions".
- **One account or two.** Dev and production can share an account or have one each. Two is
  recommended if you have them; the original deployment shares one.
  [deployment-runsheet.md](deployment-runsheet.md#3-one-account-or-two) says what differs.

## 1. Create the DNS zone and the budget alarm (you, locally, once)

Bootstrap is always applied by hand, never by CI. If you gave it your domain and an alert email the
first time, this step is already done: `terraform -chdir=infra/bootstrap output hosted_zone_id`
prints a zone ID. Otherwise:

```bash
cd infra/bootstrap
terraform init
terraform plan  -var domain_name=example.com -var budget_alert_email=you@example.com
```

Pass the same other variables you used for the first bootstrap apply (`aws_account_id`,
`github_repo`, `state_bucket_name`, `aws_region`). **Check the plan only adds the zone and the
budget.** If it shows anything else being changed or destroyed, stop and find out why. Then:

```bash
terraform apply -var domain_name=example.com -var budget_alert_email=you@example.com
terraform output hosted_zone_id
terraform output hosted_zone_name_servers
```

Keep the four name servers (they look like `ns-123.awsdns-45.org`) for the next step.

The zone lives in bootstrap, not in production, on purpose: its four name servers are what you give
your registrar, and they must not change if production is ever destroyed and rebuilt. The zone has
`prevent_destroy`.

## 2. Point your registrar at Route 53 (you, at the registrar)

Why Route 53 and not the registrar's own DNS: the site is served from CloudFront on the bare domain,
and DNS does not allow a bare domain to be an alias of another name. Route 53 can do it; most
registrars' DNS cannot. Your registrar stays the registrar (you still renew the domain there); only
who answers DNS questions changes.

**Before you change anything**

- **This replaces whatever the domain shows now.** If that is only a parked page, fine. If you pay
  the registrar for a website or email plan on this domain, decide about it first.
- Look through the registrar's DNS records for anything you use (MX for email, TXT for
  verification). Moving the name servers drops all of them. Recreate what you need in the Route 53
  zone afterwards.
- **If DNSSEC is switched on at the registrar, switch it off first.** Leaving it on while changing
  name servers makes the domain unreachable for many resolvers.

**Then** find the domain's name server setting and replace the registrar's own with the four from
step 1, with no trailing dots. Every registrar calls it something like "custom name servers" or
"use my own name servers".

> **Example: GoDaddy.** My Products > the domain > **DNS** > **Nameservers** > **Change
> Nameservers** > **I'll use my own nameservers**, paste the four, save. The screens move around
> over time.
>
> Other registrars (Namecheap, Cloudflare Registrar, Squarespace Domains, Porkbun, and so on) have
> the same setting under the domain's DNS or name server page. If the domain is registered with
> Route 53 itself, set the name servers under Route 53 > Registered domains.

## 3. Wait for the change to spread, then check it

It is usually minutes; allow up to a few hours (rarely up to 48). Nothing is lost while you wait.

```bash
python scripts/domain_check.py --expect-ns <the four from step 1>
```

Re-run it until the first line says `[ OK ] Name servers`. Everything after that is skipped until it
does, on purpose: until then whatever answers on the domain is still the registrar's, and judging
it would only confuse.

**Do not run the production release until this is OK.** The certificate is proved by a DNS record,
and it can only be seen once the name servers have moved. If you run it too early Terraform waits on
the certificate and then fails; that is safe to re-run, just slow.

## 4. Put the domain and zone ID in production (a small PR)

In `infra/environments/production/terraform.tfvars`:

```hcl
domain_name    = "example.com"
hosted_zone_id = "Z0123456789ABC"   # from: terraform -chdir=infra/bootstrap output hosted_zone_id
```

Neither value is sensitive, so it is fine in git. A fork must change both: the file ships with the
original site's values. Open a PR into `dev` and merge it. (Merging to `dev` only applies dev, which
has no custom domain, so nothing changes there.)

`www.<your domain>` is set up too and redirects to the bare domain. If the zone ID is missing, the
plan stops with a plain message instead of an AWS error half-way through.

## 5. GitHub setup for production (you)

```bash
REPO=$(gh repo view --json nameWithOwner --jq .nameWithOwner)   # check this is YOUR repository

# the prod branch the release workflow requires (from the merged dev tip)
git fetch origin && git branch prod origin/dev && git push -u origin prod

# the production environment, with you as the required approver
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}]}" |
  gh api -X PUT "repos/$REPO/environments/production" --input -
```

Then the settings. The setup script asks for each one, checks it, and puts production's on the
`production` environment:

```bash
python scripts/setup_repo.py --dry-run     # asks and checks, sets nothing
python scripts/setup_repo.py
```

For production it sets `AWS_PROD_DEPLOY_ROLE_ARN` and `ADMIN_ALLOWED_CIDRS_PROD` (both required),
and, if you give them, `AWS_PROD_ACCOUNT_ID`, `TF_STATE_BUCKET_PROD` and `ALERT_EMAIL_PROD`. The
full table, and why each is a secret or a variable, is in
[configuration.md](configuration.md#github-secrets-and-variables).

Also worth doing while you are in Settings:

- Protect `dev` and `prod` (require a pull request, block force-pushes and deletions). On a
  private repository this needs a paid GitHub plan; the AWS trust policy still gates production
  either way. The commands are in [todo/public-repo-runsheet.md](todo/public-repo-runsheet.md).
- Check no credential is stored as a repository **variable**. Variables are shown in plain text and
  are not masked in logs; a credential should only ever be a secret.

## 6. Release to production (you approve, CI applies)

```bash
gh pr create --base prod --head dev --title "Release v0.1.0" --body "First production release"
# merge it, then:
gh release create v0.1.0 --target prod --title v0.1.0 --generate-notes
```

Publishing the release starts `terraform-production-release`. It confirms the tagged commit is on
`prod`, waits for your approval on the `production` environment, then applies. Watch it in the
Actions tab. Later releases are the same two commands with the next version number.

Expect 15 to 25 minutes on the first run: a few hundred resources, the certificate has to validate,
and a new CloudFront distribution takes 5 to 15 minutes to deploy. If it stops on the certificate,
see step 3 and re-run the workflow.

**If an apply fails part-way**, read the error, fix the cause in a pull request to `dev`, and
release again. Terraform picks up where it stopped. Some things are only checked by AWS at apply
time, so a release can fail on something dev never exercised: a resource only production creates,
a name AWS refuses, a quota. The friction log (`docs/friction.md`, entries 2.9, 10.16 and 10.17)
has the ones this project has met.

## 7. After the first apply

```bash
terraform -chdir=infra/environments/production output          # the URLs
python scripts/domain_check.py --expect-ns <the four from step 1>   # every line should say OK
```

Then, in order:

1. **Confirm the alert email.** AWS sends one confirmation mail to the address; nothing is delivered
   until you click it (there is one each for the alarms and the budget).
2. **Point the CLI at production** (each environment has its own URL; see `scripts/QUICKSTART.md`):
   ```bash
   export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/production output -raw admin_api_url)
   python scripts/admin_cli.py topics list
   ```
3. **Seed the topics** you want
   ([deployment-runsheet.md, step 5](deployment-runsheet.md#5-after-the-first-deploy)). Creating a
   topic creates its schedules; after that it runs unattended. Trigger one by hand first:
   `topics trigger <id> --pipeline research_tick`, then `daily_cycle`.
4. **Optional, but recommended: the API keys and the model registry.** Each is per environment,
   so what you set up for dev is not here yet. Store production's CoinGecko key and GitHub token
   (`/<prefix>/production/coingecko-api-key`, `/<prefix>/production/github-api-token`) and
   seed production's model tables (`<prefix>-production-models`,
   `<prefix>-production-model-config`) the same way as dev's (`<prefix>` is your
   `UNIQUE_NAME_PREFIX`, `bloggerbear` by default):
   [deployment-runsheet.md, "Optional, but recommended"](deployment-runsheet.md#optional-but-recommended).
5. **Smoke-test in a browser**: the home page, an article, the Stats page, `/rss.xml`, a thumbs-up
   on an article (this exercises the feedback token), and `https://www.<your domain>` (should land
   on the bare domain).
6. **Optional:** production creates the shared CloudFront web ACL. In a one-account setup, put
   `terraform output wafv2_web_acl_arn` into `infra/environments/dev/terraform.tfvars` as
   `web_acl_arn` if you want dev to share it. It cannot be shared across accounts.

7. **The operator's assistant** (`/ask.html`; README, "The operator's assistant and Alexa+"). Its
   user pool has self-sign-up off and MFA required, so make your own user by hand:
   ```bash
   POOL=$(terraform -chdir=infra/environments/production output -raw ops_user_pool_id)
   aws cognito-idp admin-create-user --user-pool-id "$POOL" --username <you> \
     --user-attributes Name=email,Value=<your email> --message-action SUPPRESS
   aws cognito-idp admin-set-user-password --user-pool-id "$POOL" --username <you> \
     --password '<a long password>' --permanent
   ```
   Open `https://<your domain>/ask.html`, sign in, and set up the authenticator app it asks for.
   Press **Test voice**, then **What needs my attention?**. "What's happening with the firewall?"
   as a follow-up is production's deep dive. "Any errors in the logs?" and "any API failures?" read
   production's logs (and shared ones) and answer with root causes and the API calls by status code; if they say AWS refused, see
   [CloudWatch Logs tags](deployment-runsheet.md#cloudwatch-logs-tags-the-assistants-log-tools).
   Check `pipeline-config get` shows `assistant_access` as `open` (or absent), unless you mean to
   lock it to your addresses. Five failed sign-ins in fifteen minutes lock a user and email the
   alert address; `sign-ins unlock <you>` clears it
   ([deployment-runsheet.md, step 5](deployment-runsheet.md#5-after-the-first-deploy)).
8. **Optional: Alexa+.** Production's own add-on, linked to production's pool only, is a one-time
   bootstrap: [alexa/README.md](../alexa/README.md). Putting Alexa's redirect URLs in
   `ops_alexa_redirect_uris` also keeps the MCP function warm (about 8,600 invocations a month).

## Rolling back

- **Point the domain back**: at the registrar, restore its default name servers (GoDaddy, for
  example: Nameservers > **Reset to GoDaddy default nameservers**). Spreading takes the same
  minutes-to-hours as before; the Route 53 zone can stay.
- **A bad release**: publish a new release from an earlier commit on `prod`.
- **Tearing production down** is deliberately awkward: tables have deletion protection (set
  `protect_data = false` and apply, then destroy) and the content bucket is not force-destroyed.
  The DNS zone is in bootstrap with `prevent_destroy`, so it survives, and the registrar's settings
  stay valid when you rebuild.
- **An accidental delete or overwrite**: tables can be restored to any second in the last 35 days
  (DynamoDB point-in-time recovery); an overwritten article body can be recovered from the bucket's
  previous version for 30 days.

## What it costs (list prices, roughly)

- Route 53: US$0.50 a month for the zone, plus a few cents of queries.
- WAF is the biggest fixed cost: production has three web ACLs and eight rules, about US$5 per ACL
  and US$1 per rule a month (roughly US$23). Dev has its own.
- CloudWatch dashboards: the first three in an account are free, then about US$3 a month each.
- CloudFront, Lambda, DynamoDB and API Gateway are pay-per-use and small at this traffic; Bedrock
  scales with how much is written, which is what the budget alarm from step 1 is watching.
- ACM certificates are free and renew themselves.

Check the AWS pricing pages before relying on these numbers.

## Not done, on purpose

- **No custom domain for the API.** The site talks to the API on its own AWS address, which is fine
  and needs no change.
- **No email at the domain.** If you want it later, the MX records go in the Route 53 zone.
- **The original deployment keeps both environments in one AWS account.** Names never collide, but
  a quota or a bad permission change touches both. Two accounts are supported:
  see [deployment-runsheet.md](deployment-runsheet.md#3-one-account-or-two).
