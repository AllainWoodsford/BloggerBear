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
  # s3:Get*/s3:Put* (not just the GetBucket*/PutBucket* subset this
  # started as) -- confirmed necessary the hard way: the provider's own
  # post-create read of aws_s3_bucket calls s3:GetAccelerateConfiguration,
  # which doesn't match a "GetBucket*" prefix despite being a per-bucket
  # setting (S3's action naming isn't fully consistent here). Rather than
  # enumerate every such exception as they surface one apply at a time,
  # this widens to the full Get*/Put* surface already implied by the
  # "full S3 admin is acceptable ... as long as it stays within S3" call
  # above.
  statement {
    sid    = "SiteBuckets"
    effect = "Allow"
    actions = [
      "s3:*Object",
      "s3:Get*",
      "s3:Put*",
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:List*",
    ]
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
  statement {
    sid    = "DynamoDBAppTables"
    effect = "Allow"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:DescribeTable",
      "dynamodb:UpdateTable",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:UpdateTimeToLive",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource",
      # The provider's post-create read of aws_dynamodb_table always
      # calls DescribeContinuousBackups (point-in-time recovery status),
      # regardless of whether the config sets point_in_time_recovery --
      # confirmed the hard way on the first real apply.
      "dynamodb:DescribeContinuousBackups",
    ]
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
  statement {
    sid    = "LambdaFunctions"
    effect = "Allow"
    actions = [
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:GetFunction",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:TagResource",
      "lambda:ListVersionsByFunction",
      "lambda:GetPolicy",
      "lambda:AddPermission",
      "lambda:RemovePermission",
    ]
    resources = ["arn:aws:lambda:ap-southeast-2:*:function:bloggerbear-*"]
  }

  # Phase 1: the CloudWatch log groups Lambda creates on first invocation
  # (and that Terraform may come to manage directly for retention).
  # Scoped to the /aws/lambda/bloggerbear-* log group prefix.
  statement {
    sid    = "LambdaLogGroups"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DescribeLogGroups",
      "logs:TagResource",
    ]
    resources = ["arn:aws:logs:ap-southeast-2:*:log-group:/aws/lambda/bloggerbear-*"]
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
  statement {
    sid    = "LambdaExecRole"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:TagRole",
      "iam:PassRole",
      # The provider's post-create read of aws_iam_role always calls
      # ListRolePolicies (inline policies) AND ListAttachedRolePolicies
      # (managed policy attachments) to drift-detect both, regardless of
      # whether this config manages either -- confirmed the hard way,
      # across two separate applies (ListRolePolicies surfaced first,
      # ListAttachedRolePolicies only showed up once a destroy actually
      # reached these roles).
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      # aws_iam_role's delete path checks for (and would need to detach)
      # any instance profiles still using the role before it can be
      # deleted, regardless of whether this config ever creates one --
      # confirmed the hard way on a destroy.
      "iam:ListInstanceProfilesForRole",
    ]
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
  # bloggerbear-* state machine name prefix.
  statement {
    sid    = "StepFunctions"
    effect = "Allow"
    actions = [
      "states:CreateStateMachine",
      "states:DeleteStateMachine",
      "states:DescribeStateMachine",
      "states:UpdateStateMachine",
      "states:TagResource",
    ]
    resources = ["arn:aws:states:ap-southeast-2:*:stateMachine:bloggerbear-*"]
  }

  # Phase 3: the dead-letter queue the state machine sends failed
  # executions to. Scoped to the bloggerbear-* queue name prefix.
  statement {
    sid    = "SQS"
    effect = "Allow"
    actions = [
      "sqs:CreateQueue",
      "sqs:DeleteQueue",
      "sqs:GetQueueAttributes",
      "sqs:SetQueueAttributes",
      "sqs:TagQueue",
      # The provider's post-create read of aws_sqs_queue always calls
      # ListQueueTags to drift-detect tags, regardless of whether the
      # config sets any -- confirmed the hard way on a destroy.
      "sqs:ListQueueTags",
    ]
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
  statement {
    sid    = "SchedulerStaticSchedules"
    effect = "Allow"
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:GetSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:TagResource",
    ]
    resources = ["arn:aws:scheduler:ap-southeast-2:*:schedule/default/bloggerbear-*"]
  }

  # Phase 6: the per-environment SNS alerts topic (module.observability's
  # aws_sns_topic.alerts) plus the optional email subscription gated on
  # var.alert_email. This whole statement was missing before the first
  # real apply -- Phase 6 built the module but the deploy policy was never
  # updated to match, so every SNS call failed AccessDenied. Scoped to the
  # bloggerbear-* topic name prefix.
  statement {
    sid    = "SNSAlerts"
    effect = "Allow"
    actions = [
      "sns:CreateTopic",
      "sns:DeleteTopic",
      "sns:GetTopicAttributes",
      "sns:SetTopicAttributes",
      "sns:TagResource",
      "sns:ListTagsForResource",
      "sns:Subscribe",
      "sns:Unsubscribe",
      "sns:GetSubscriptionAttributes",
    ]
    resources = ["arn:aws:sns:ap-southeast-2:*:bloggerbear-*"]
  }

  # Phase 6: the Lambda error/throttle, DLQ-depth, and Step Functions
  # failure alarms (module.observability's aws_cloudwatch_metric_alarm.*),
  # each publishing to the SNS topic above. Same "missing since Phase 6"
  # gap as SNSAlerts. Scoped to the bloggerbear-* alarm name prefix.
  statement {
    sid    = "CloudWatchAlarms"
    effect = "Allow"
    actions = [
      "cloudwatch:PutMetricAlarm",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DeleteAlarms",
      "cloudwatch:TagResource",
    ]
    resources = ["arn:aws:cloudwatch:ap-southeast-2:*:alarm:bloggerbear-*"]
  }

  # Phase 6: the pipeline-health dashboard (module.observability's
  # aws_cloudwatch_dashboard.pipeline). Separate statement from
  # CloudWatchAlarms above since dashboard ARNs are a different shape --
  # no region segment. Same "missing since Phase 6" gap.
  statement {
    sid    = "CloudWatchDashboard"
    effect = "Allow"
    actions = [
      "cloudwatch:PutDashboard",
      "cloudwatch:GetDashboard",
      "cloudwatch:DeleteDashboards",
      "cloudwatch:ListDashboards",
    ]
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
  statement {
    sid    = "WafLogGroups"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DescribeLogGroups",
      "logs:TagResource",
    ]
    resources = ["arn:aws:logs:ap-southeast-2:*:log-group:aws-waf-logs-bloggerbear-*:*"]
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
