# Production runsheet

Everything between "dev works" and "bloggerbear.com is live", in order, with the exact commands. Each step
says who does it and how to tell it worked. Run the steps top to bottom; tick them off as you go.

Wherever it says **check it**, `python scripts/domain_check.py` is the read-only tool that tells you where the
domain stands (it needs no AWS login and changes nothing).

## Where things stand (checked 2026-09-22)

I looked at the live account, the repo settings and the domain itself. What was missing is why the steps
below exist.

| Thing | State | What it means |
|---|---|---|
| Terraform state bucket, GitHub OIDC provider, both deploy roles | Done | Bootstrap has been applied. The production role only trusts jobs running in the `production` GitHub environment. |
| Deploy role may manage Route 53, ACM, CloudFront (incl. Functions) | Done | No IAM change needed for the domain. |
| **Route 53 hosted zone** for the domain | **Missing** | None exists. Step 1 creates it. |
| **Domain's name servers** | **Still GoDaddy's** (`ns19` / `ns20.domaincontrol.com`) | The domain is not pointed at AWS. Step 2. |
| What the domain shows today | A GoDaddy-hosted page titled "Blogger Bear" (HTTP 200) | Step 2 replaces it. I found no MX or TXT records, so no email or verification records to preserve; look in GoDaddy's DNS panel to be sure. |
| **`hosted_zone_id`** in production's `terraform.tfvars` | **Empty** | Cannot be filled until the zone exists. Step 4. |
| **Bedrock budget alarm** | **Never created** | Bootstrap was applied without `budget_alert_email`; only AWS's default "My Zero-Spend Budget" exists. Step 1 adds it. |
| **`prod` branch** | **Missing** | The release workflow refuses to run without it. Step 5. |
| **`production` GitHub environment** and `AWS_PROD_DEPLOY_ROLE_ARN` | **Missing** (only a `copilot` environment exists) | The workflow cannot assume the production role without them. Step 5. |
| `ADMIN_ALLOWED_CIDRS_PROD`, `COINGECKO_API_KEY_PROD` | Set (repo secrets) | Fine: repo-level secrets are visible to the production job too. |
| `ALERT_EMAIL_DEV` / `ALERT_EMAIL_PROD` | Set as **variables**, but the workflows read **secrets** | So no alarm email has ever been subscribed, on dev either. This change makes the workflows accept either. |
| A first-ever production apply | **Failed at plan time** | `Invalid count argument` in the API module. Fixed in this change (see below). |
| Production data protection | **None** | No point-in-time recovery, no deletion protection, no bucket versioning. Added in this change. |

### What this change fixes in the Terraform

- **The first production apply would have failed.** The API module decided whether to attach the WAF from the
  ACL's ARN, which does not exist until the same apply creates it. Dev never hit it because, as far as I can
  tell, its ACLs already existed by the time that code landed. There is now an explicit `associate_web_acl` flag, and a test fails if a caller names an
  ACL without setting it (naming one without the flag would leave the API unprotected).
- **Domain support**: `www.bloggerbear.com` now works and redirects to `bloggerbear.com` (certificate names,
  a second CloudFront alias, DNS records, and a small CloudFront Function). Without it a visitor typing `www`
  would get an error, and once DNS leaves GoDaddy there is nothing else to forward it. If the zone ID is
  missing the plan stops with a plain-English message instead of an AWS error halfway through.
- **The DNS zone lives in `infra/bootstrap`**, not in production. Its four name servers are what you type
  into GoDaddy, and they must not change if production is ever destroyed and rebuilt. The zone has
  `prevent_destroy`.
- **Production keeps its data**: every DynamoDB table gets deletion protection and point-in-time recovery,
  and the content bucket (every article body) is versioned, with old versions expiring after 30 days.

## 1. Create the DNS zone and the budget alarm (you, locally, once)

Bootstrap is always applied by hand, never by CI. It also adds the Bedrock budget alarm that was skipped the
first time.

```bash
cd infra/bootstrap
terraform init
terraform plan  -var domain_name=bloggerbear.com -var budget_alert_email=you@example.com
```

**Check the plan says `Plan: 2 to add, 0 to change, 0 to destroy`** (the zone and the budget). If it shows
anything being changed or destroyed, stop and ask. Then:

```bash
terraform apply -var domain_name=bloggerbear.com -var budget_alert_email=you@example.com
terraform output hosted_zone_id
terraform output hosted_zone_name_servers
```

Keep the four name servers (they look like `ns-123.awsdns-45.org`) for the next step.

## 2. Point GoDaddy at Route 53 (you, in GoDaddy)

Why Route 53 and not GoDaddy's own DNS: the site is served from CloudFront on the bare domain, and DNS does
not allow the bare domain to be an alias of another name. Route 53 can; GoDaddy's DNS cannot. GoDaddy stays the
registrar (you still renew the domain there); only who answers DNS questions changes.

**Before you change anything**

- **This replaces whatever GoDaddy is serving now.** The domain currently shows a GoDaddy-hosted page. If it
  is only a placeholder, fine. If you pay for a GoDaddy website or email plan, decide about it first.
- Look through GoDaddy's DNS records for anything you use (MX for email, TXT for verification). Moving the name
  servers drops all of them. Recreate what you need in the Route 53 zone afterwards.
- **If DNSSEC is switched on at GoDaddy, switch it off first** (DNS page, DNSSEC). Leaving it on while changing
  name servers makes the domain unreachable for many resolvers.

**Then**: GoDaddy > My Products > the domain > **DNS** > **Nameservers** > **Change Nameservers** > **I'll use
my own nameservers**, paste the four from step 1 (no trailing dots), and save. GoDaddy's screens move around a
little over time; you are looking for "custom nameservers".

## 3. Wait for the change to spread, then check it

It is usually minutes; allow up to a few hours (rarely up to 48). Nothing is lost while you wait.

```bash
python scripts/domain_check.py --expect-ns <the four from step 1>
```

Re-run it until the first line says `[ OK ] Name servers`. Everything after that is skipped until it does, on
purpose: until then whatever answers on the domain is GoDaddy's, and judging it would only confuse.

**Do not run the production release until this is OK.** The certificate is proved by a DNS record, and it can
only be seen once the name servers have moved. If you run it too early Terraform waits on the certificate and
then fails; that is safe to re-run, just slow.

## 4. Put the zone ID in production (a small PR)

In `infra/environments/production/terraform.tfvars`:

```hcl
domain_name    = "bloggerbear.com"
hosted_zone_id = "Z0123456789ABC"   # from: terraform -chdir=infra/bootstrap output hosted_zone_id
```

Neither value is sensitive, so it is fine in git. Open a PR into `dev` and merge it. (Merging to `dev` only
applies dev, which has no custom domain, so nothing changes there.)

## 5. GitHub setup for production (you)

```bash
# the prod branch the release workflow requires (from the merged dev tip)
git fetch origin && git branch prod origin/dev && git push -u origin prod

# the production environment, with you as the required approver
echo "{\"reviewers\":[{\"type\":\"User\",\"id\":$(gh api user --jq .id)}]}" |
  gh api -X PUT repos/AllainWoodsford/BloggerBear/environments/production --input -

# the role the job assumes (a secret: a variable would print in public logs)
gh secret set AWS_PROD_DEPLOY_ROLE_ARN --env production \
  --body "$(terraform -chdir=infra/bootstrap output -raw prod_deploy_role_arn)"
```

Also worth doing while you are in Settings:

- Protect `dev` and `prod` (require a PR and the `terraform` and `security` checks). On a private repo this
  needs a paid GitHub plan; the AWS trust policy still gates production either way.
- **Delete the `CLAUDE_CODE_OAUTH_TOKEN` repository _variable_** (`gh variable delete CLAUDE_CODE_OAUTH_TOKEN`)
  and keep only the secret of the same name. Variables are shown in plain text and are not masked in logs; a
  credential should only ever be a secret. Rotate that token afterwards, because it has been readable.
- The alert emails are variables today. Either works now, but secrets keep the address out of logs:
  `gh secret set ALERT_EMAIL_PROD` (and `_DEV`), then delete the variables.

## 6. Release to production (you approve, CI applies)

```bash
gh pr create --base prod --head dev --title "Release v0.1.0" --body "First production release"
# merge it, then:
gh release create v0.1.0 --target prod --title v0.1.0 --generate-notes
```

Publishing the release starts `terraform-production-release`. It confirms the tagged commit is on `prod`,
waits for your approval on the `production` environment, then applies. Watch it in the Actions tab.

Expect 15 to 25 minutes on the first run: about 290 resources, the certificate has to validate, and a new
CloudFront distribution takes 5 to 15 minutes to deploy. If it stops on the certificate, see step 3 and re-run
the workflow.

## 7. After the first apply

```bash
terraform -chdir=infra/environments/production output          # the URLs
python scripts/domain_check.py --expect-ns <the four from step 1>   # all nine lines should say OK
```

Then, in order:

1. **Confirm the alert email.** AWS sends one confirmation mail to the address; nothing is delivered until you
   click it (there is one each for the alarms and the budget).
2. **Point the CLI at production** (each environment has its own URL; see `scripts/QUICKSTART.md`):
   ```bash
   export BLOGGERBEAR_ADMIN_API_URL=$(terraform -chdir=infra/environments/production output -raw admin_api_url)
   python scripts/admin_cli.py topics list
   ```
3. **Seed the topics** you want (README step 6). Creating a topic creates its schedules; after that it runs
   unattended. Trigger one by hand first: `topics trigger <id> --pipeline research_tick`, then `daily_cycle`.
4. **Smoke-test in a browser**: the home page, an article, the Stats page (the gear section), `/rss.xml`, a
   thumbs-up on an article (this exercises the feedback token), and `https://www.bloggerbear.com` (should land
   on the bare domain).
5. **Optional:** production creates the shared CloudFront WAF ACL. Put `terraform output wafv2_web_acl_arn`
   into `dev/terraform.tfvars` as `web_acl_arn` if you want dev to share it.

## Rolling back

- **Point the domain back**: GoDaddy > Nameservers > **Reset to GoDaddy default nameservers**. Spreading takes
  the same minutes-to-hours as before; the Route 53 zone can stay.
- **A bad release**: publish a new release from an earlier commit on `prod`.
- **Tearing production down** is deliberately awkward: tables have deletion protection (set
  `protect_data = false` and apply, then destroy) and the bucket is not force-destroyed. The DNS zone is in
  bootstrap with `prevent_destroy`, so it survives, and the GoDaddy settings stay valid when you rebuild.
- **An accidental delete or overwrite**: tables can be restored to any second in the last 35 days
  (DynamoDB point-in-time recovery); an overwritten article body can be recovered from the bucket's previous
  version for 30 days.

## What it costs (list prices, roughly)

- Route 53: US$0.50 a month for the zone, plus a few cents of queries.
- WAF is the biggest fixed cost: production has three web ACLs and eight rules, about US$5 per ACL and US$1 per
  rule a month (roughly US$23). Dev has its own.
- CloudFront, Lambda, DynamoDB and API Gateway are pay-per-use and small at this traffic; Bedrock scales with how
  much is written, which is what the budget alarm from step 1 is watching.
- ACM certificates are free and renew themselves.

Check the AWS pricing pages before relying on these numbers.

## Not done, on purpose

- **One AWS account for both environments.** Names never collide, but a bad permission change touches both.
  Splitting them is a bigger job.
- **No custom domain for the API.** The site talks to the API on its own AWS address, which is fine and needs
  no change.
- **No email at the domain.** If you want it later, the MX records go in the Route 53 zone.
