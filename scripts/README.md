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
python scripts/admin_cli.py topics trigger github-trending --pipeline daily_cycle --no-wait
python scripts/admin_cli.py topics candidates github-trending
python scripts/admin_cli.py topics findings github-trending

python scripts/admin_cli.py moderation list
python scripts/admin_cli.py moderation approve <queue_id>
python scripts/admin_cli.py moderation reject <queue_id>

python scripts/admin_cli.py articles publish <article_id>

python scripts/admin_cli.py failed-executions list

python scripts/admin_cli.py models list
python scripts/admin_cli.py models add --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0" \
    --display-name "Claude Haiku 4.5" --provider anthropic \
    --input-price 0.0008 --output-price 0.004
python scripts/admin_cli.py model-config get
python scripts/admin_cli.py model-config set --model-id "au.anthropic.claude-haiku-4-5-20251001-v1:0"

# Per-topic: pin one model, set a fallback, or rotate between several
# (one picked at random per daily run). '' clears a value.
python scripts/admin_cli.py topics update github-trending     --model-candidates "model-id-a,model-id-b" --fallback-model-id "model-id-a"
python scripts/admin_cli.py topics update github-trending --model-candidates ""
```

`models`/`model-config` back the AI lineage/cost-tracking enhancement's
DynamoDB-backed model registry (docs/project-plan.md §11) -- adding a
model or changing the global default/fallback never needs a Terraform
apply. Resolution order (`common/model_routing.py`'s `resolve_model`):
a topic's `model_id_candidates` rotation list (one picked at random per
run, so all of one article's Bedrock calls use the same model and its
lineage stays coherent), if set → the topic's own `model_id` override →
the global `model-config` default → the Terraform-set `BEDROCK_MODEL_ID`
env var. `fallback_model_id` resolves independently (topic → global).

`articles publish` force-sets an article's status to `published` regardless
of its current state -- unlike `moderation approve`, which only acts on an
item still sitting `pending` in the moderation queue. Use it to publish
something that was never routed to moderation in the first place, or to
override a stuck/undesired status. If a moderation queue item exists for
the article and is still `pending`, it's marked `approved` too so the two
records don't disagree.

`failed-executions list` shows every daily_cycle Step Functions execution
that exhausted its retries and landed on the pipeline dead-letter queue
(see `lambdas/dlq_handler.py`) -- each item records the topic, the error,
and the raw message, for manual follow-up. There's no automatic replay; to
retry a topic after fixing whatever caused the failure, use
`topics trigger <topic_id> --pipeline daily_cycle` again.

### Testing the DLQ consumer manually

`dlq_handler.py` is only exercised for real when a `daily_cycle` execution
actually fails both of its Step Functions retries -- to verify it works
without waiting for (or forcing) a real failure, send it a synthetic
message shaped like what the state machine's `Catch` state produces:

```bash
QUEUE_URL=$(terraform -chdir=infra/environments/dev output -raw pipeline_dlq_url)
aws sqs send-message \
  --queue-url "$QUEUE_URL" \
  --message-body '{"topic_id": "test-topic", "error": {"Error": "States.TaskFailed", "Cause": "synthetic test message"}}'
```

Then confirm it landed:

```bash
python scripts/admin_cli.py failed-executions list
```

and check `aws logs tail /aws/lambda/bloggerbear-dev-dlq-handler --since 5m`
for the `dlq_handler: daily_cycle failed for topic_id=test-topic: ...` log
line. If `pipeline_dlq_url` isn't an existing Terraform output yet, get the
queue URL instead with
`aws sqs get-queue-url --queue-name bloggerbear-dev-pipeline-dlq`.

Responses are pretty-printed JSON on stdout. A non-2xx response prints the
error body to stderr and exits non-zero.

`topics trigger`'s target Lambda invocation is fire-and-forget (async), so
by default the command polls afterward and only returns once it sees actual
new output -- a new Finding for `research_tick`, a new candidate idea for
`daily_cycle` -- or times out (30s / 90s respectively) and says so. Pass
`--no-wait` to skip this and get the old immediate-return behavior back.
This also means running `daily_cycle` right after `research_tick` for the
same topic is safe without a manual delay in between; running it beforehand
still isn't -- `daily_cycle` needs a Finding to already exist.
