#!/usr/bin/env bash
# Example: force-publish an article via the Admin API (POST
# /articles/{article_id}/publish), bypassing moderation entirely.
#
# This is a stub for `source`ing into your CURRENT shell, not for running
# as `./force_publish_example.sh` -- the exports below need to land in
# your interactive session, not a subshell that exits right after:
#
#   source scripts/force_publish_example.sh
#
# Requires: AWS credentials configured locally (aws configure / aws sso
# login) with permission to call the Admin API, your IP on the WAF
# allowlist for the target environment, and infra/ already applied there.
# See scripts/README.md for background on both exports below.

# --- 1. AWS region -----------------------------------------------------
export AWS_DEFAULT_REGION="ap-southeast-2"

# --- 2. Admin API URL ----------------------------------------------------
# Not hardcoded -- API Gateway only assigns this once infra/ has been
# applied, so it's looked up from Terraform's own output. Change
# "dev" to "production" to target the other environment.
export BLOGGERBEAR_ADMIN_API_URL="$(terraform -chdir=infra/environments/dev output -raw admin_api_url)"
echo "BLOGGERBEAR_ADMIN_API_URL=${BLOGGERBEAR_ADMIN_API_URL}"

# --- 3. Force-publish an article ------------------------------------------
# Replace with a real article_id before running -- e.g. from
# `python scripts/admin_cli.py moderation list`, or any article_id
# returned by a `topics trigger ... --pipeline daily_cycle` call.
ARTICLE_ID="replace-with-real-article-id"

python scripts/admin_cli.py articles publish "${ARTICLE_ID}"
