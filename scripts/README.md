# BloggerBear admin CLI

A local operator CLI for the Phase 2 Admin API. This is a plain script you
run on your own machine -- it is **not** deployed to Lambda or anywhere
else.

## Why a CLI, not a web page

The Admin API requires AWS SigV4 (IAM) auth and is additionally restricted
to the admin's IP by a WAF allowlist. A browser page can't safely hold
long-lived IAM credentials to sign requests with, so this CLI signs
requests using whatever AWS credentials your local environment already has
configured (the same default credential chain `boto3`/`aws` CLI use --
`aws configure`, SSO, environment variables, etc).

## Setup

```bash
pip install -r scripts/requirements.txt
```

Make sure you have AWS credentials configured locally with permission to
call the Admin API (`aws configure`, `aws sso login`, or equivalent), and
that your IP is on the WAF allowlist Terraform configured for the
environment you're targeting.

## Configuration

The CLI needs two things:

1. **Admin API base URL** -- not known until `infra/` has been applied,
   since API Gateway assigns it. After applying, get it with:

   ```bash
   terraform -chdir=infra/environments/<dev-or-production> output admin_api_url
   ```

   Then either export it:

   ```bash
   export BLOGGERBEAR_ADMIN_API_URL="https://<api-id>.execute-api.<region>.amazonaws.com"
   ```

   or pass `--api-url` on every invocation.

2. **AWS region** -- read from `--region`, or falls back to `AWS_REGION` /
   `AWS_DEFAULT_REGION` (already set if you've run `aws configure`).

## Usage

```bash
python scripts/admin_cli.py topics list
python scripts/admin_cli.py topics get github-trending
python scripts/admin_cli.py topics create --topic-id github-trending \
    --name "GitHub Trending" --adapter github_trending \
    --config-json '{"language": "python"}'
python scripts/admin_cli.py topics update github-trending --name "Renamed" --financial
python scripts/admin_cli.py topics delete github-trending
python scripts/admin_cli.py topics trigger github-trending --pipeline research_tick
python scripts/admin_cli.py topics candidates github-trending

python scripts/admin_cli.py moderation list
python scripts/admin_cli.py moderation approve <queue_id>
python scripts/admin_cli.py moderation reject <queue_id>
```

Responses are pretty-printed JSON on stdout. A non-2xx response prints the
error body to stderr and exits non-zero.
