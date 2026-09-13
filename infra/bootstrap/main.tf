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
  statement {
    sid    = "SiteBuckets"
    effect = "Allow"
    actions = [
      "s3:*Object",
      "s3:GetBucket*",
      "s3:PutBucket*",
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
# -----------------------------------------------------------------------
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
      values   = ["repo:${var.github_repo}:ref:refs/heads/dev"]
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
      values   = ["repo:${var.github_repo}:environment:production"]
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
