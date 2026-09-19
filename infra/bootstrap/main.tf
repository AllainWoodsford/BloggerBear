terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  # No backend block here on purpose: bootstrap creates the state bucket
  # itself, so its own state is kept locally (or wherever the human running
  # it keeps it) -- it can never depend on the bucket it's creating.
}

provider "aws" {
  region = var.aws_region
}

# -----------------------------------------------------------------------
# Terraform state backend
#
# Bucket name is a real, working default ("bloggerbear-terraform-state"),
# not a placeholder token -- it only needs to change if it collides with
# an existing bucket somewhere in AWS (S3 bucket names are globally
# unique). If you do change var.state_bucket_name, copy the exact same
# literal string into the `backend "s3" { bucket = "..." }` blocks in BOTH
# infra/environments/dev/main.tf and infra/environments/production/main.tf
# -- those blocks cannot reference variables or interpolation. See
# outputs.tf for the same note next to the bucket name output.
#
# prevent_destroy protects the bucket resource itself from accidental
# deletion (e.g. a careless `terraform destroy` run against this
# directory); it does not affect objects within it.
# -----------------------------------------------------------------------
resource "aws_s3_bucket" "terraform_state" {
  bucket = var.state_bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# AVD-AWS-0132 ("no customer-managed KMS key") ignored deliberately --
# SSE-S3 (AES256) rather than a customer-managed KMS key, same
# cost/complexity trade-off as every other bucket/topic/queue in this
# project (see the same comment on infra/environments/dev/main.tf's
# aws_s3_bucket.content for the full rationale).
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "terraform_state" {
  bucket = aws_s3_bucket.terraform_state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# No DynamoDB lock table anywhere -- Terraform >= 1.10's native S3 locking
# (`use_lockfile = true` in each environment's backend block) replaces it
# entirely. Each environment's own state key gets its own `.tflock`
# companion object, so dev and production never contend for the same lock.

# -----------------------------------------------------------------------
# GitHub Actions OIDC federation
# -----------------------------------------------------------------------

# thumbprint_list is deliberately omitted: recent aws provider versions
# (this project pins >= 5.0, and the resolved lock file uses a current
# 6.x release) fetch the GitHub OIDC issuer's certificate thumbprint
# automatically via TLS when this argument is left unset, which is more
# reliable than hardcoding a published thumbprint from memory (GitHub has
# rotated its intermediate CA before, which changes the correct value).
resource "aws_iam_openid_connect_provider" "github_actions" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

# Shared permission policy for both deploy roles. Started in Phase 0 with
# only S3, CloudFront, WAFv2, Route 53, and ACM; Phase 1 adds narrowly
# scoped DynamoDB, Lambda, CloudWatch Logs, and IAM statements below for
# the app-data tables and pipeline Lambdas -- each added only when the
# phase that needs it lands, not granted up front. Bedrock remains
# deliberately excluded even now; see the comment at the end of this
# policy.
data "aws_iam_policy_document" "gha_deploy" {
  # State backend: list the bucket (needed by the S3 backend/native
  # locking) and read/write the state object + its .tflock companion for
  # both environments.
  #
  # NOTE: SiteBuckets below now grants s3:* on resources = ["*"], which
  # technically makes these next two statements redundant in practice
  # (their grants are a strict subset). Kept anyway as documentation of
  # intent -- the minimum S3 access this deploy role actually needs for
  # the state backend itself, independent of whatever SiteBuckets ends up
  # covering -- and because narrowing SiteBuckets back down later (e.g. if
  # a future change makes exact site-bucket ARNs knowable) should not
  # accidentally take the state backend access with it.
  statement {
    sid    = "TerraformStateBucketList"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetBucketLocation",
    ]
    resources = [aws_s3_bucket.terraform_state.arn]
  }

  statement {
    sid    = "TerraformStateObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
    ]
    resources = [
      "${aws_s3_bucket.terraform_state.arn}/dev/*",
      "${aws_s3_bucket.terraform_state.arn}/production/*",
    ]
  }

  # Site buckets this project's environments create/manage via the
  # static-site module. Bucket names aren't known here (they're created
  # later, per environment), so this is scoped by action rather than by
  # resource ARN -- full S3 admin is acceptable for a single-operator
  # portfolio project, per the phase-0 spec, as long as it stays within S3.
  #
  # s3:* rather than the Get*/Put*/List*/CreateBucket/DeleteBucket/*Object
  # subset this grew into piece by piece -- confirmed the hard way (via
  # s3:GetAccelerateConfiguration, which doesn't match a "GetBucket*"
  # prefix despite being a per-bucket setting) that S3's action naming
  # isn't consistent enough to enumerate safely, and the same gap would
  # exist on the delete side too (e.g. s3:DeleteBucketPolicy doesn't match
  # Put*/Get*/List*/DeleteBucket either). Since resources is already
  # unrestricted within S3 ("full S3 admin is acceptable ... as long as it
  # stays within S3" above), going the rest of the way to s3:* removes an
  # entire category of future one-apply-at-a-time surprises for zero
  # additional blast radius.
  statement {
    sid       = "SiteBuckets"
    effect    = "Allow"
    actions   = ["s3:*"]
    resources = ["*"]
  }

  statement {
    sid       = "CloudFront"
    effect    = "Allow"
    actions   = ["cloudfront:*"]
    resources = ["*"]
  }

  # WAFv2 is only actually used in us-east-1 (CloudFront scope) -- see
  # infra/environments/production/main.tf -- but IAM actions aren't
  # region-scopable, so this grants the API surface and relies on the
  # resources only ever being created in us-east-1.
  statement {
    sid       = "WAF"
    effect    = "Allow"
    actions   = ["wafv2:*"]
    resources = ["*"]
  }

  statement {
    sid       = "Route53"
    effect    = "Allow"
    actions   = ["route53:*"]
    resources = ["*"]
  }

  statement {
    sid       = "ACM"
    effect    = "Allow"
    actions   = ["acm:*"]
    resources = ["*"]
  }

  # Phase 1: application data tables. Scoped to the bloggerbear-* table
  # name prefix (not "*") -- more sensitive than the S3/CloudFront/etc.
  # wildcards above, since these are real app tables, not per-environment
  # buckets created fresh each time.
  #
  # dynamodb:* rather than an enumerated action list: three straight
  # rounds of "one AccessDenied at a time" (DescribeContinuousBackups here,
  # plus the same pattern on IAM/SQS/S3 below) made clear that Terraform
  # providers call a long tail of Describe*/List* actions during normal
  # create/read/update/delete that aren't obvious from the resource's own
  # arguments, and enumerating them by hitting each one is not a
  # sustainable way to build this policy. The resource ARN pattern below
  # is what actually bounds the blast radius (only bloggerbear-* tables,
  # never "*") -- widening the action list within that boundary costs
  # nothing security-wise, since anything DynamoDB lets you do to a table
  # was already reachable via the enumerated actions this replaces, minus
  # the ones that kept surfacing as gaps.
  statement {
    sid       = "DynamoDBAppTables"
    effect    = "Allow"
    actions   = ["dynamodb:*"]
    resources = ["arn:aws:dynamodb:ap-southeast-2:*:table/bloggerbear-*"]
  }

  # Phase 1: the two pipeline Lambda functions. Scoped to the
  # bloggerbear-* function name prefix. AddPermission/RemovePermission
  # (added here in Phase 4, though the gap predates it -- Phase 2's
  # aws_lambda_permission.admin_api_apigw already needed these) is what
  # aws_lambda_permission resources need to create/update/destroy the
  # resource-based policy statement that lets API Gateway invoke a
  # function; both the Phase 2 admin API and the Phase 4 public API
  # permissions fall under this same bloggerbear-* scoped statement.
  # lambda:* rather than an enumerated list -- see DynamoDBAppTables above
  # for why. Scoped to the bloggerbear-* function name prefix, same as
  # before.
  statement {
    sid       = "LambdaFunctions"
    effect    = "Allow"
    actions   = ["lambda:*"]
    resources = ["arn:aws:lambda:ap-southeast-2:*:function:bloggerbear-*"]
  }

  # Phase 1: the CloudWatch log groups Lambda creates on first invocation
  # (and that Terraform may come to manage directly for retention).
  # Scoped to the /aws/lambda/bloggerbear-* log group prefix. Not
  # currently exercised by any resource in infra/environments (no
  # aws_cloudwatch_log_group targets this pattern yet -- Lambda creates
  # these itself on first invocation, outside Terraform), so this
  # statement is unproven against a real apply; widened to logs:* and
  # given both ARN forms (with and without the trailing `:*`) alongside
  # WafLogGroups below for the same reason, rather than leaving an
  # unexercised guess in place to fail the same way WafLogGroups did.
  statement {
    sid     = "LambdaLogGroups"
    effect  = "Allow"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:ap-southeast-2:*:log-group:/aws/lambda/bloggerbear-*",
      "arn:aws:logs:ap-southeast-2:*:log-group:/aws/lambda/bloggerbear-*:*",
    ]
  }

  # Phase 1 (extended in Phase 3): IAM for the Lambda execution role, plus
  # (Phase 3) the Step Functions and EventBridge Scheduler roles. This is
  # the sensitive one -- IAM actions are deliberately NEVER granted
  # against resources = ["*"] anywhere in this policy, unlike the
  # S3/CloudFront/WAF/Route53/ACM statements above. `resources` is scoped
  # to exactly the bloggerbear-*-lambda-exec / bloggerbear-*-states-exec /
  # bloggerbear-*-scheduler-invoke role name patterns, and nothing else.
  # This is what stops a compromised (or merely buggy) CI deploy role from
  # creating or passing an arbitrary, more-privileged IAM role --
  # including PassRole, which is the specific permission that would
  # otherwise let it hand any role to any service. If this policy is
  # extended later (more roles, more actions), preserve this scoping:
  # widen the resource pattern only as far as this naming convention
  # requires, never to a bare "*".
  #
  # iam:* rather than an enumerated action list -- three separate rounds
  # of AccessDenied (ListRolePolicies, then ListAttachedRolePolicies, then
  # ListInstanceProfilesForRole -- all provider-internal reads during
  # create/refresh/delete that aren't obvious from this config's own
  # arguments) made the enumerate-as-you-go approach clearly unsustainable.
  # This is still safe: the security property this policy protects is
  # resource scoping (this role can only ever touch these three exact role
  # names, never an arbitrary one), not action counting -- iam:* on roles/
  # policies OUTSIDE this resource list remains fully denied, and PassRole
  # (the specific action that would let a compromised role hand off a
  # more-privileged role to a service) was already granted before this
  # change, so nothing here increases what this role could actually do
  # beyond these three roles.
  statement {
    sid     = "LambdaExecRole"
    effect  = "Allow"
    actions = ["iam:*"]
    resources = [
      "arn:aws:iam::*:role/bloggerbear-*-lambda-exec",
      "arn:aws:iam::*:role/bloggerbear-*-states-exec",
      "arn:aws:iam::*:role/bloggerbear-*-scheduler-invoke",
    ]
  }

  # Phase 2: API Gateway HTTP API for the admin console (see
  # infra/environments/*/main.tf's aws_apigatewayv2_api.admin and related
  # resources). API Gateway management-API ARNs deliberately don't carry
  # an account ID -- this is the correct ARN shape for apigateway:*
  # actions, not an oversight -- so this can't be scoped down to
  # bloggerbear-* the way Lambda/DynamoDB/logs are above; it's scoped by
  # action + region instead.
  statement {
    sid     = "ApiGateway"
    effect  = "Allow"
    actions = ["apigateway:*"]
    resources = [
      "arn:aws:apigateway:ap-southeast-2::/apis",
      "arn:aws:apigateway:ap-southeast-2::/apis/*",
    ]
  }

  # Phase 3: the Step Functions state machine that wraps the daily_cycle
  # Lambda invocation for retries + a DLQ on failure. Scoped to the
  # bloggerbear-* state machine name prefix. states:* rather than an
  # enumerated list -- see DynamoDBAppTables above for why (this also
  # preemptively covers states:ListTagsForResource, which Step Functions
  # needs separately from DescribeStateMachine to drift-detect tags and
  # which the enumerated list below never had, so would have failed the
  # same way on the next apply).
  statement {
    sid       = "StepFunctions"
    effect    = "Allow"
    actions   = ["states:*"]
    resources = ["arn:aws:states:ap-southeast-2:*:stateMachine:bloggerbear-*"]
  }

  # Phase 3: the dead-letter queue the state machine sends failed
  # executions to. Scoped to the bloggerbear-* queue name prefix.
  # sqs:* rather than an enumerated list -- see DynamoDBAppTables above for
  # why (ListQueueTags was the specific gap that surfaced here).
  statement {
    sid       = "SQS"
    effect    = "Allow"
    actions   = ["sqs:*"]
    resources = ["arn:aws:sqs:ap-southeast-2:*:bloggerbear-*"]
  }

  # Phase 5: unlike the per-topic schedules research_tick/daily_cycle use
  # (created dynamically at runtime by admin_api_handler's
  # common/scheduler.py via the Lambda execution role's own scheduler:*
  # grant -- see infra/environments/*/main.tf's
  # aws_iam_role_policy.scheduler_manage -- which is why no such statement
  # existed here before Phase 5), the weekly reflection job's schedule
  # (aws_scheduler_schedule.weekly_reflection) IS a Terraform-managed
  # resource, since it's one static, global, non-per-topic cron. That means
  # Terraform/CI itself -- not the runtime Lambda execution role -- needs
  # to create/read/update/delete/tag it, so this CI deploy role needs its
  # own scheduler:* grant. Scoped to the same default schedule group and
  # bloggerbear-* name prefix as scheduler_manage's grant above, not to a
  # bare "*".
  # scheduler:* rather than an enumerated list -- see DynamoDBAppTables
  # above for why.
  statement {
    sid       = "SchedulerStaticSchedules"
    effect    = "Allow"
    actions   = ["scheduler:*"]
    resources = ["arn:aws:scheduler:ap-southeast-2:*:schedule/default/bloggerbear-*"]
  }

  # Phase 6: the per-environment SNS alerts topic (module.observability's
  # aws_sns_topic.alerts) plus the optional email subscription gated on
  # var.alert_email. This whole statement was missing before the first
  # real apply -- Phase 6 built the module but the deploy policy was never
  # updated to match, so every SNS call failed AccessDenied. Scoped to the
  # bloggerbear-* topic name prefix.
  # sns:* rather than an enumerated list -- see DynamoDBAppTables above for
  # why.
  statement {
    sid       = "SNSAlerts"
    effect    = "Allow"
    actions   = ["sns:*"]
    resources = ["arn:aws:sns:ap-southeast-2:*:bloggerbear-*"]
  }

  # Phase 6: the Lambda error/throttle, DLQ-depth, and Step Functions
  # failure alarms (module.observability's aws_cloudwatch_metric_alarm.*),
  # each publishing to the SNS topic above. Same "missing since Phase 6"
  # gap as SNSAlerts. Scoped to the bloggerbear-* alarm name prefix.
  # cloudwatch:* rather than an enumerated list -- see DynamoDBAppTables
  # above for why.
  statement {
    sid       = "CloudWatchAlarms"
    effect    = "Allow"
    actions   = ["cloudwatch:*"]
    resources = ["arn:aws:cloudwatch:ap-southeast-2:*:alarm:bloggerbear-*"]
  }

  # Phase 6: the pipeline-health dashboard (module.observability's
  # aws_cloudwatch_dashboard.pipeline). Separate statement from
  # CloudWatchAlarms above since dashboard ARNs are a different shape --
  # no region segment. Same "missing since Phase 6" gap.
  # cloudwatch:* rather than an enumerated list -- see DynamoDBAppTables
  # above for why. Separate statement from CloudWatchAlarms since
  # dashboard ARNs are a different shape (no region segment).
  statement {
    sid       = "CloudWatchDashboard"
    effect    = "Allow"
    actions   = ["cloudwatch:*"]
    resources = ["arn:aws:cloudwatch::*:dashboard/bloggerbear-*"]
  }

  # Phase 0/6: the CloudWatch Logs log groups the WAF logging
  # configurations (infra/environments/*/main.tf's
  # aws_wafv2_web_acl_logging_configuration.*) write into
  # (aws_cloudwatch_log_group.waf_admin/waf_public_api). Also missing
  # before the first real apply. Scoped to the aws-waf-logs-bloggerbear-*
  # log group prefix -- the trailing `:*` matches CloudWatch Logs' own
  # documented ARN format for the log-group resource type, not a
  # log-stream scoping (unlike LambdaLogGroups above, which predates this
  # fix and hasn't been proven against a real apply yet since no
  # aws_cloudwatch_log_group resource currently targets it).
  # logs:* rather than an enumerated list -- see DynamoDBAppTables above
  # for why. Both ARN forms (with and without the trailing `:*`) listed
  # since it's unclear which of the widened action set expects which
  # shape, and listing both costs nothing.
  statement {
    sid     = "WafLogGroups"
    effect  = "Allow"
    actions = ["logs:*"]
    resources = [
      "arn:aws:logs:ap-southeast-2:*:log-group:aws-waf-logs-bloggerbear-*",
      "arn:aws:logs:ap-southeast-2:*:log-group:aws-waf-logs-bloggerbear-*:*",
    ]
  }

  # Phase 0/6: the CloudWatch Logs resource policy that lets the WAF
  # service itself write into the log groups above
  # (aws_cloudwatch_log_resource_policy.waf_logs). These three actions
  # operate on the account/region's log delivery configuration as a
  # whole, not a specific log group -- AWS doesn't support resource-level
  # scoping for them, same reasoning as the CloudFront/WAF/Route53/ACM
  # statements above using resources = ["*"].
  statement {
    sid    = "WafLogResourcePolicy"
    effect = "Allow"
    actions = [
      "logs:PutResourcePolicy",
      "logs:DeleteResourcePolicy",
      "logs:DescribeResourcePolicies",
    ]
    resources = ["*"]
  }

  # Deliberately excluded: bedrock:* of any kind. Bedrock is only ever
  # invoked by the Lambda execution role at runtime (see
  # infra/environments/*/main.tf's aws_iam_role_policy.lambda_exec) --
  # never by CI/Terraform itself, which has no reason to call Bedrock.
}

resource "aws_iam_policy" "gha_deploy" {
  name   = "bloggerbear-gha-deploy"
  policy = data.aws_iam_policy_document.gha_deploy.json
}

# -----------------------------------------------------------------------
# Dev deploy role -- assumable only by workflow runs actually triggered
# off the `dev` branch.
#
# The sub condition below matches on wildcarded owner/repo segments
# (`OWNER@*` / `REPO@*`), not the plain `repo:OWNER/REPO:...` form GitHub's
# docs lead with. That's deliberate: this account/repo has GitHub's
# "immutable subject claims" behavior on (confirmed via `gh api
# repos/<owner>/<repo>/actions/oidc/customization/sub`, which returns
# use_immutable_subject: true), so the actual sub claim GitHub issues is
# `repo:OWNER@<owner-id>/REPO@<repo-id>:ref:refs/heads/dev` -- numeric IDs
# appended to stop an old trust policy from matching after a repo
# rename/transfer. A plain (non-wildcarded) condition here silently never
# matches, and the failure only ever surfaces at the STS API as a generic
# "Not authorized to perform sts:AssumeRoleWithWebIdentity" -- no hint
# it's the sub format. Confirmed via CloudTrail's AssumeRoleWithWebIdentity
# error events, which show the real principalId/sub GitHub actually sent.
data "aws_iam_policy_document" "gha_dev_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${split("/", var.github_repo)[0]}@*/${split("/", var.github_repo)[1]}@*:ref:refs/heads/dev"]
    }
  }
}

resource "aws_iam_role" "gha_dev_deploy" {
  name               = "gha-bloggerbear-dev-deploy"
  assume_role_policy = data.aws_iam_policy_document.gha_dev_trust.json
}

resource "aws_iam_role_policy_attachment" "gha_dev_deploy" {
  role       = aws_iam_role.gha_dev_deploy.name
  policy_arn = aws_iam_policy.gha_deploy.arn
}

# -----------------------------------------------------------------------
# Production deploy role -- assumable only by workflow runs associated
# with the `production` GitHub Environment (required-reviewer gated).
# Even if workflow logic were edited to skip that gate, AWS itself still
# refuses credentials without the Environment context in the token.
#
# See the identical wildcard + comment on gha_dev_trust above for why the
# sub condition matches OWNER@*/REPO@* rather than the plain OWNER/REPO
# GitHub's docs lead with -- this account/repo has immutable subject
# claims on, so the real sub carries numeric owner/repo IDs.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "gha_prod_trust" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github_actions.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${split("/", var.github_repo)[0]}@*/${split("/", var.github_repo)[1]}@*:environment:production"]
    }
  }
}

resource "aws_iam_role" "gha_prod_deploy" {
  name               = "gha-bloggerbear-prod-deploy"
  assume_role_policy = data.aws_iam_policy_document.gha_prod_trust.json
}

resource "aws_iam_role_policy_attachment" "gha_prod_deploy" {
  role       = aws_iam_role.gha_prod_deploy.name
  policy_arn = aws_iam_policy.gha_deploy.arn
}

# -----------------------------------------------------------------------
# Phase 6: Bedrock-spend budget. AWS Budgets is account-level, not a
# per-region or per-environment resource, so this lives here alongside
# the other account-level, one-time-applied resources (OIDC provider,
# state bucket) rather than in infra/environments/dev or production --
# there is exactly one of these regardless of how many environments
# exist. count-gated on var.budget_alert_email rather than guessing an
# address: AWS Budgets requires at least one notification subscriber, so
# with no email configured this creates nothing rather than failing the
# apply (same fail-closed-by-omission pattern as
# infra/environments/dev's var.admin_allowed_cidrs).
# -----------------------------------------------------------------------
resource "aws_budgets_budget" "bedrock_spend" {
  count = var.budget_alert_email != "" ? 1 : 0

  name         = "bloggerbear-bedrock-spend"
  budget_type  = "COST"
  limit_amount = var.bedrock_budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Bedrock"]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
