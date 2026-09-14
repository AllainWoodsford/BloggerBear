terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }

  backend "s3" {
    bucket       = "bloggerbear-terraform-state"
    key          = "dev/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-southeast-2"
}

# The static-site module declares a required `aws.us_east_1` provider
# alias (needed for the ACM certificate path used only when
# enable_custom_domain = true -- see infra/modules/static-site/main.tf).
# Terraform requires every module call to supply a provider for each
# configuration_alias the module declares, even when this environment
# never exercises that code path: enable_custom_domain = false below means
# the ACM/Route53 resources in the module all have count = 0, so this
# alias is never actually invoked here. We satisfy the requirement by
# pointing the alias at the same default ap-southeast-2 provider rather
# than declaring a real us-east-1 provider -- production is the only place
# in this codebase with an actual us-east-1 provider block.
provider "aws" {
  alias  = "us_east_1"
  region = "ap-southeast-2"
}

module "static_site" {
  source = "../../modules/static-site"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment_name     = "dev"
  enable_custom_domain = false
  force_destroy        = var.force_destroy
  web_acl_id           = var.web_acl_arn
}

# =========================================================================
# Phase 1 -- app data tables, content storage, and the Lambda pipeline.
# See docs/project-plan.md §3/§5 and infra/modules/app-data for the
# DynamoDB table set; lambdas/ (Python, owned by the application-code
# workstream) for the two handlers packaged below.
# =========================================================================

module "app_data" {
  source = "../../modules/app-data"

  environment_name = "dev"
}

# -----------------------------------------------------------------------
# Content bucket -- read/written directly by Lambda via the SDK, never
# served publicly, so no CloudFront/OAC. Same private-bucket pattern
# (ownership controls + public access block) as the static-site module's
# site bucket. force_destroy = true here only: dev is meant to be torn
# down and rebuilt freely.
# -----------------------------------------------------------------------
resource "aws_s3_bucket" "content" {
  bucket        = "bloggerbear-dev-content"
  force_destroy = true
}

resource "aws_s3_bucket_ownership_controls" "content" {
  bucket = aws_s3_bucket.content.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "content" {
  bucket = aws_s3_bucket.content.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------
# Lambda deployment package -- both functions ship from the same zip
# (one `lambdas/` source tree with a shared `common/` package). Excludes
# are a reasonable assumption about the Python workstream's layout (dev
# tooling, test suite, cache dirs); double-check against the actual
# lambdas/ contents once that work has landed, since it was being written
# concurrently with this file.
# -----------------------------------------------------------------------
data "archive_file" "lambdas" {
  type        = "zip"
  source_dir  = "${path.module}/../../../lambdas"
  output_path = "${path.module}/lambda-build/lambdas.zip"

  excludes = [
    "tests",
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
  ]
}

# -----------------------------------------------------------------------
# Shared Lambda execution role -- both handlers read/write the same
# tables, bucket, and model, so one role covers both.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "bloggerbear-dev-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_exec" {
  statement {
    sid    = "DynamoDBAppTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
    ]
    resources = module.app_data.table_arns
  }

  statement {
    sid    = "ContentBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.content.arn}/*"]
  }

  # Foundation-model ARNs don't carry an account ID -- this is the exact
  # ARN pattern for on-demand Bedrock model invocation. See
  # var.bedrock_model_id below re: confirming a working model ID in this
  # region before Phase 1 can run end-to-end.
  statement {
    sid       = "BedrockInvoke"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel"]
    resources = ["arn:aws:bedrock:ap-southeast-2::foundation-model/*"]
  }

  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:ap-southeast-2:*:log-group:/aws/lambda/bloggerbear-dev-*"]
  }
}

resource "aws_iam_role_policy" "lambda_exec" {
  name   = "bloggerbear-dev-lambda-exec"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_exec.json
}

# Phase 4: the public site's URL, used by public_api_handler.py (e.g. to
# build absolute links in rss.xml). Built from the module's bare
# *.cloudfront.net domain output rather than a dedicated "site URL"
# output on the module (there isn't one) -- dev never enables the custom
# domain path, so this is always the CloudFront default domain. Also
# reused below for the generated config.js's window.SITE_URL, so the
# frontend and the Lambda agree on the same value.
locals {
  site_url = "https://${module.static_site.distribution_domain_name}"
}

locals {
  lambda_env_variables = {
    TOPICS_TABLE           = module.app_data.topics_table_name
    FINDINGS_TABLE         = module.app_data.findings_table_name
    CANDIDATE_IDEAS_TABLE  = module.app_data.candidate_ideas_table_name
    ARTICLES_TABLE         = module.app_data.articles_table_name
    MODERATION_QUEUE_TABLE = module.app_data.moderation_queue_table_name
    CONTENT_BUCKET         = aws_s3_bucket.content.bucket
    BEDROCK_MODEL_ID       = var.bedrock_model_id
    # Phase 2: lets the admin-api handler invoke the other two pipeline
    # Lambdas on demand (e.g. POST /topics/{topic_id}/trigger). Harmless
    # on research_tick/daily_cycle themselves -- they just never read it.
    #
    # Literal strings, not aws_lambda_function.research_tick.function_name
    # / .daily_cycle.function_name -- those resources' own `environment`
    # blocks consume this same local, so referencing their attributes here
    # would create a dependency cycle. function_name is a fixed literal
    # (not computed), so it's identical either way; keep these in sync
    # with the function_name arguments on aws_lambda_function.research_tick
    # and aws_lambda_function.daily_cycle below.
    RESEARCH_TICK_FUNCTION_NAME = "bloggerbear-dev-research-tick"
    DAILY_CYCLE_FUNCTION_NAME   = "bloggerbear-dev-daily-cycle"

    # Phase 3: lets admin_api_handler's common/scheduler.py create/update/
    # delete per-topic EventBridge Scheduler schedules at runtime (topics
    # are runtime data -- there's no fixed list for Terraform to enumerate
    # here). RESEARCH_TICK_FUNCTION_ARN and STATE_MACHINE_ARN are the two
    # possible per-topic invocation targets; SCHEDULER_INVOKE_ROLE_ARN is
    # the role EventBridge Scheduler assumes to call them (see
    # aws_iam_role.scheduler_invoke below). ENVIRONMENT_NAME is a literal
    # string, not computed, for the same dependency-cycle reason as the
    # *_FUNCTION_NAME entries above.
    #
    # RESEARCH_TICK_FUNCTION_ARN and STATE_MACHINE_ARN are built from
    # data.aws_caller_identity.current.account_id plus the same fixed
    # literal names used elsewhere (function_name below /
    # aws_sfn_state_machine.daily_cycle's name), rather than referencing
    # aws_lambda_function.research_tick.arn / aws_sfn_state_machine.
    # daily_cycle.arn directly -- the state machine's definition already
    # references aws_lambda_function.daily_cycle.arn, so a direct
    # STATE_MACHINE_ARN = aws_sfn_state_machine.daily_cycle.arn reference
    # here would create daily_cycle -> local.lambda_env_variables ->
    # state_machine -> daily_cycle, a dependency cycle Terraform refuses
    # to plan. data.aws_caller_identity has no such dependency, so this
    # sidesteps the cycle the same way the literal function_name strings
    # do. SCHEDULER_INVOKE_ROLE_ARN has no such issue (scheduler_invoke's
    # own attributes don't depend on any Lambda/state-machine resource) so
    # it's referenced directly.
    RESEARCH_TICK_FUNCTION_ARN = "arn:aws:lambda:ap-southeast-2:${data.aws_caller_identity.current.account_id}:function:bloggerbear-dev-research-tick"
    STATE_MACHINE_ARN          = "arn:aws:states:ap-southeast-2:${data.aws_caller_identity.current.account_id}:stateMachine:bloggerbear-dev-daily-cycle"
    SCHEDULER_INVOKE_ROLE_ARN  = aws_iam_role.scheduler_invoke.arn
    ENVIRONMENT_NAME           = "dev"

    # Phase 4: consumed by public_api_handler.py.
    SITE_URL = local.site_url

    # Phase 5: the weekly reflection job's two new tables (see
    # infra/modules/app-data's aws_dynamodb_table.feedback /
    # prompt_refinements). Consumed by public_api_handler.py (feedback
    # writes) and weekly_reflection_handler.py (reads feedback, writes
    # refinements). aws_iam_role_policy.lambda_exec below already covers
    # both -- its DynamoDB statement is `resources =
    # module.app_data.table_arns`, which now includes these two ARNs
    # automatically, no separate IAM change needed.
    FEEDBACK_TABLE           = module.app_data.feedback_table_name
    PROMPT_REFINEMENTS_TABLE = module.app_data.prompt_refinements_table_name
  }
}

# Used only to construct RESEARCH_TICK_FUNCTION_ARN / STATE_MACHINE_ARN
# above without a direct resource reference (see the comment there for
# why a direct reference would create a dependency cycle).
data "aws_caller_identity" "current" {}

resource "aws_lambda_function" "research_tick" {
  function_name = "bloggerbear-dev-research-tick"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "research_tick_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

resource "aws_lambda_function" "daily_cycle" {
  function_name = "bloggerbear-dev-daily-cycle"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "daily_cycle_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# =========================================================================
# Phase 2 -- Admin console API: a third Lambda (from the same shared
# deployment package above) fronted by an IAM-authenticated API Gateway
# HTTP API and a regional WAF IP allowlist. Per docs/PROGRESS.md's Phase 2
# line, this project uses IAM (SigV4) auth + a WAF IP allowlist rather than
# Cognito -- simpler and cheaper for a single operator driving this
# entirely through a local CLI (scripts/admin_cli.py, Python workstream),
# never a browser app. See infra/environments/production/main.tf for the
# unrelated Phase 0 CLOUDFRONT-scope Web ACL -- this is a separate,
# REGIONAL-scope ACL that protects only this API.
# =========================================================================

resource "aws_lambda_function" "admin_api" {
  function_name = "bloggerbear-dev-admin-api"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "admin_api_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Second inline policy on the same shared exec role (rather than folding
# into aws_iam_role_policy.lambda_exec above) so this grant stays visibly
# scoped to exactly the two pipeline Lambda ARNs -- deliberately NOT
# aws_lambda_function.admin_api.arn (no self-invoke) and NOT "*".
data "aws_iam_policy_document" "lambda_invoke_pipeline" {
  statement {
    sid       = "InvokePipelineLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.research_tick.arn, aws_lambda_function.daily_cycle.arn]
  }
}

resource "aws_iam_role_policy" "lambda_invoke_pipeline" {
  name   = "bloggerbear-dev-lambda-invoke-pipeline"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_invoke_pipeline.json
}

# -----------------------------------------------------------------------
# API Gateway HTTP API. authorization_type = "AWS_IAM" on every route is
# what enforces SigV4 auth -- HTTP APIs need no separate authorizer
# resource for IAM auth, unlike REST APIs. Reachability is further
# restricted to the operator's own IP by the regional WAF Web ACL below.
# -----------------------------------------------------------------------
resource "aws_apigatewayv2_api" "admin" {
  name          = "bloggerbear-dev-admin-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "admin_lambda" {
  api_id                 = aws_apigatewayv2_api.admin.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.admin_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_lambda_permission" "admin_api_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.admin_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.admin.execution_arn}/*/*"
}

# One route per admin_cli.py operation. for_each over the route-key list
# below creates one aws_apigatewayv2_route resource instance per entry
# (rather than 10 hand-copied blocks) -- keep this list and
# scripts/admin_cli.py's routes in sync.
locals {
  admin_api_routes = toset([
    "GET /topics",
    "POST /topics",
    "GET /topics/{topic_id}",
    "PUT /topics/{topic_id}",
    "DELETE /topics/{topic_id}",
    "POST /topics/{topic_id}/trigger",
    "GET /topics/{topic_id}/candidates",
    "GET /moderation-queue",
    "POST /moderation-queue/{queue_id}/approve",
    "POST /moderation-queue/{queue_id}/reject",
    # Phase 6: "what's actually been flagged so far" visibility -- see
    # admin_api_handler.py's _moderation_queue_stats and
    # scripts/admin_cli.py's `moderation stats` subcommand.
    "GET /moderation-queue/stats",
    # Phase 5: prompt refinement approval workflow -- see
    # admin_api_handler.py's _ROUTES dict and scripts/admin_cli.py's
    # `refinements` subcommand.
    "GET /prompt-refinements",
    "POST /prompt-refinements/{topic_id}/{version}/approve",
    "POST /prompt-refinements/{topic_id}/{version}/reject",
  ])
}

resource "aws_apigatewayv2_route" "admin" {
  for_each = local.admin_api_routes

  api_id             = aws_apigatewayv2_api.admin.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.admin_lambda.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.admin.id
  name        = "$default"
  auto_deploy = true
}

# -----------------------------------------------------------------------
# Regional WAF IP allowlist -- a different Web ACL from the Phase 0
# CLOUDFRONT-scope one in production/main.tf (that one is shared by both
# CloudFront distributions, us-east-1 only). This one is REGIONAL scope,
# created in this environment's default ap-southeast-2 provider (regional
# WAF for API Gateway lives in the API's own region, no us-east-1 alias
# needed), and protects only the admin API.
#
# default_action = block: until var.admin_allowed_cidrs is set to the
# operator's real public IP (a /32 CIDR), the IP set is empty and NOTHING
# can call this API. That is the deliberately safe default -- fail closed
# -- not a bug.
# -----------------------------------------------------------------------
resource "aws_wafv2_ip_set" "admin_allowlist" {
  name               = "bloggerbear-dev-admin-allowlist"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = var.admin_allowed_cidrs
}

resource "aws_wafv2_web_acl" "admin" {
  name        = "bloggerbear-dev-admin-api"
  description = "Regional WAF Web ACL for the BloggerBear dev admin API -- allows only the operator's allowlisted IP(s); blocks everything else by default."
  scope       = "REGIONAL"

  default_action {
    block {}
  }

  rule {
    name     = "allow-admin-ips"
    priority = 1

    action {
      allow {}
    }

    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.admin_allowlist.arn
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-dev-admin-allow"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-dev-admin-acl"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "admin" {
  resource_arn = aws_apigatewayv2_stage.default.arn
  web_acl_arn  = aws_wafv2_web_acl.admin.arn
}

# =========================================================================
# Phase 3 -- Automation: a Step Functions state machine wraps the single
# daily_cycle Lambda invocation purely to get retries + a dead-letter
# queue on failure (see docs/project-plan.md §4 -- the daily authoring
# cycle is deliberately one Lambda, not four Step-Functions-orchestrated
# stages; rewriting working Phase 1 code into a multi-stage pipeline for a
# single-operator portfolio project isn't worth it). The hourly research
# tick does NOT go through Step Functions at all -- EventBridge Scheduler
# invokes it directly, since it's already a single self-contained
# diff-and-maybe-summarize operation with nothing to orchestrate.
#
# Per-topic schedules themselves are NOT Terraform resources -- topics are
# runtime data (created/edited/deleted via the admin API), so there's no
# fixed list for Terraform to enumerate. They're created dynamically at
# runtime by admin_api_handler's common/scheduler.py via the AWS SDK.
# Terraform only creates the IAM role those dynamically-created schedules
# assume (aws_iam_role.scheduler_invoke) and grants the Lambda execution
# role permission to manage them (aws_iam_role_policy.scheduler_manage).
# =========================================================================

resource "aws_sqs_queue" "pipeline_dlq" {
  name = "bloggerbear-dev-pipeline-dlq"
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "states_exec" {
  name               = "bloggerbear-dev-states-exec"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

data "aws_iam_policy_document" "states_exec" {
  statement {
    sid       = "InvokeDailyCycle"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.daily_cycle.arn]
  }

  statement {
    sid       = "SendToDeadLetterQueue"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "states_exec" {
  name   = "bloggerbear-dev-states-exec"
  role   = aws_iam_role.states_exec.id
  policy = data.aws_iam_policy_document.states_exec.json
}

resource "aws_sfn_state_machine" "daily_cycle" {
  name     = "bloggerbear-dev-daily-cycle"
  role_arn = aws_iam_role.states_exec.arn

  definition = jsonencode({
    StartAt = "RunDailyCycle"
    States = {
      RunDailyCycle = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.daily_cycle.arn
          "Payload.$"  = "$"
        }
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 30
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "SendToDeadLetterQueue"
            ResultPath  = "$.error"
          }
        ]
        End = true
      }
      SendToDeadLetterQueue = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage"
        Parameters = {
          QueueUrl        = aws_sqs_queue.pipeline_dlq.url
          "MessageBody.$" = "$"
        }
        End = true
      }
    }
  })
}

# -----------------------------------------------------------------------
# EventBridge Scheduler invocation role -- assumed by EventBridge
# Scheduler (not by Lambda or Step Functions) whenever a per-topic
# schedule fires. The per-topic schedules themselves are created
# dynamically at runtime by admin_api_handler's common/scheduler.py, not
# by Terraform -- see the Phase 3 header comment above.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler_invoke" {
  name               = "bloggerbear-dev-scheduler-invoke"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    sid       = "InvokeResearchTick"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.research_tick.arn]
  }

  statement {
    sid       = "StartDailyCycleExecution"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.daily_cycle.arn]
  }

  # Phase 5: the weekly reflection job's single static schedule (see
  # aws_scheduler_schedule.weekly_reflection below) also assumes this same
  # role -- a third, separately-listed resource, same tight per-resource
  # scoping as the two statements above, deliberately not merged into
  # InvokeResearchTick's resources list or widened to a wildcard.
  statement {
    sid       = "InvokeWeeklyReflection"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.weekly_reflection.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "bloggerbear-dev-scheduler-invoke"
  role   = aws_iam_role.scheduler_invoke.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

# Third inline policy on the shared lambda_exec role (same pattern as
# Phase 2's lambda_invoke_pipeline above) -- lets admin_api_handler's
# common/scheduler.py manage per-topic EventBridge Scheduler schedules at
# runtime. Scoped to the default schedule group (no custom group is
# created) and the bloggerbear-dev-* name prefix, never "*". iam:PassRole
# is scoped to exactly the one scheduler_invoke role ARN -- CreateSchedule
# / UpdateSchedule calls pass that role for EventBridge to assume, and IAM
# requires the caller to hold explicit PassRole on it; this must never be
# widened beyond that single role ARN (see bootstrap/main.tf's
# LambdaExecRole comment for why IAM statements in this project are never
# scoped to "*").
data "aws_iam_policy_document" "scheduler_manage" {
  statement {
    sid    = "ManageTopicSchedules"
    effect = "Allow"
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:GetSchedule",
    ]
    resources = ["arn:aws:scheduler:ap-southeast-2:*:schedule/default/bloggerbear-dev-*"]
  }

  statement {
    sid       = "PassSchedulerInvokeRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler_invoke.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_manage" {
  name   = "bloggerbear-dev-scheduler-manage"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.scheduler_manage.json
}

# =========================================================================
# Phase 4 -- Public frontend: a fourth Lambda (from the same shared
# deployment package above, sharing the same aws_iam_role.lambda_exec --
# it already has read/write on all app tables and the content bucket from
# Phase 1, which is everything public_api_handler.py needs; no new IAM
# grant required) fronted by a PUBLIC, unauthenticated API Gateway HTTP
# API with CORS enabled -- deliberately the opposite security posture
# from Phase 2's admin API (which is IAM-SigV4-gated and IP-allowlisted).
# Protected instead by a rate-limiting regional WAF Web ACL that defaults
# to allow (vs. Phase 2's ACL, which defaults to block). Also uploads the
# static frontend (frontend/, plain HTML/CSS/JS, no build step) to the
# EXISTING Phase 0 site bucket (module.static_site.bucket_name) -- no new
# bucket is created here.
# =========================================================================

resource "aws_lambda_function" "public_api" {
  function_name = "bloggerbear-dev-public-api"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "public_api_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# -----------------------------------------------------------------------
# Public API Gateway HTTP API. Every route below is authorization_type =
# "NONE" -- unauthenticated on purpose, this is public read data (topics/
# articles/rss) plus an anonymous view counter. cors_configuration with
# allow_origins = ["*"] is what lets the frontend's JS, served from the
# CloudFront domain (a different origin than this API Gateway's own
# domain), call these endpoints from the browser.
# -----------------------------------------------------------------------
resource "aws_apigatewayv2_api" "public" {
  name          = "bloggerbear-dev-public-api"
  protocol_type = "HTTP"

  cors_configuration {
    allow_origins = ["*"]
    allow_methods = ["GET", "POST", "OPTIONS"]
    allow_headers = ["content-type"]
  }
}

resource "aws_apigatewayv2_integration" "public_lambda" {
  api_id                 = aws_apigatewayv2_api.public.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.public_api.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_lambda_permission" "public_api_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.public_api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.public.execution_arn}/*/*"
}

# for_each over the route-key list below (same pattern as Phase 2's
# admin_api_routes) -- keep this list in sync with public_api_handler.py's
# _ROUTES dict.
locals {
  public_api_routes = toset([
    "GET /topics",
    "GET /articles",
    "GET /articles/{article_id}",
    "POST /articles/{article_id}/view",
    # Phase 5: anonymous thumbs up/down + optional comment -- see
    # public_api_handler.py's _submit_feedback.
    "POST /articles/{article_id}/feedback",
    "GET /rss.xml",
  ])
}

resource "aws_apigatewayv2_route" "public" {
  for_each = local.public_api_routes

  api_id             = aws_apigatewayv2_api.public.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.public_lambda.id}"
  authorization_type = "NONE"
}

resource "aws_apigatewayv2_stage" "public_default" {
  api_id      = aws_apigatewayv2_api.public.id
  name        = "$default"
  auto_deploy = true
}

# -----------------------------------------------------------------------
# Rate-limiting regional WAF Web ACL -- protects the anonymous
# POST /articles/{id}/view endpoint (and the rest of this public API)
# from scripted abuse, without blocking legitimate public traffic.
# default_action = allow is the deliberate opposite of Phase 2's admin
# ACL (which defaults to block-everything): this is a public API meant to
# be reachable by anyone. The one rule blocks only an individual source
# IP once it exceeds 500 requests within WAF's fixed (non-configurable)
# 5-minute rate-based window -- generous enough for a real visitor
# browsing the site, low enough to blunt a scripted hammering of the view
# counter.
# -----------------------------------------------------------------------
resource "aws_wafv2_web_acl" "public_api" {
  name        = "bloggerbear-dev-public-api"
  description = "Regional WAF Web ACL for the BloggerBear dev public API -- allows all traffic by default; rate-limits any single source IP past 500 requests per 5-minute window."
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 500
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-dev-public-api-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Phase 6: baseline anti-abuse hardening on top of the rate limit above
  # -- the same AWS Managed Common Rule Set already used by the
  # CLOUDFRONT-scope shared ACL (see aws_wafv2_web_acl.this in
  # production/main.tf), applied here too since this REGIONAL ACL is the
  # only thing directly in front of the public API Gateway (CloudFront
  # doesn't sit in front of API Gateway in this architecture). Not added
  # to aws_wafv2_web_acl.admin below -- that ACL already default-blocks
  # everything except the operator's own allowlisted IP, which is
  # stricter than any managed rule set could add.
  rule {
    name     = "aws-managed-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-dev-public-api-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-dev-public-api-acl"
    sampled_requests_enabled   = true
  }
}

# -----------------------------------------------------------------------
# Phase 6: WAF logging -- both regional ACLs' traffic (allowed and
# blocked) streams to CloudWatch Logs so the rate-limit/managed-rule
# thresholds above can eventually be tuned from real observed traffic,
# rather than guessed. Log group names MUST start with "aws-waf-logs-" --
# an AWS WAFv2 requirement for logging directly to CloudWatch Logs (no
# Kinesis Firehose needed). aws_cloudwatch_log_resource_policy grants the
# WAFv2 service principal permission to write to any log group matching
# that prefix in this account/region; without it, aws_wafv2_web_acl_
# logging_configuration silently delivers nothing.
# -----------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "waf_admin" {
  name              = "aws-waf-logs-bloggerbear-dev-admin"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "waf_public_api" {
  name              = "aws-waf-logs-bloggerbear-dev-public-api"
  retention_in_days = 30
}

data "aws_iam_policy_document" "waf_logs" {
  statement {
    sid    = "AllowWAFLogging"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["logs:PutLogEvents", "logs:CreateLogStream"]
    resources = ["arn:aws:logs:ap-southeast-2:*:log-group:aws-waf-logs-bloggerbear-dev-*:*"]
  }
}

resource "aws_cloudwatch_log_resource_policy" "waf_logs" {
  policy_name     = "bloggerbear-dev-waf-logs"
  policy_document = data.aws_iam_policy_document.waf_logs.json
}

resource "aws_wafv2_web_acl_logging_configuration" "admin" {
  resource_arn            = aws_wafv2_web_acl.admin.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_admin.arn]

  depends_on = [aws_cloudwatch_log_resource_policy.waf_logs]
}

resource "aws_wafv2_web_acl_logging_configuration" "public_api" {
  resource_arn            = aws_wafv2_web_acl.public_api.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_public_api.arn]

  depends_on = [aws_cloudwatch_log_resource_policy.waf_logs]
}

resource "aws_wafv2_web_acl_association" "public_api" {
  resource_arn = aws_apigatewayv2_stage.public_default.arn
  web_acl_arn  = aws_wafv2_web_acl.public_api.arn
}

# -----------------------------------------------------------------------
# Frontend static files -- uploaded to the EXISTING Phase 0 site bucket
# (module.static_site.bucket_name), not a new bucket (Phase 0 already
# created one per environment). config.js is generated here (not read
# from frontend/) since it needs this environment's own API Gateway
# invoke URL and site URL, both only known once the resources above
# exist; frontend/index.html loads it before app.js to pick up
# window.PUBLIC_API_URL and window.SITE_URL.
#
# fileexists()-guarded count: the frontend/ directory (owned by a
# concurrent workstream) may not exist yet when this is first applied in
# some environments/orderings; these resources simply create nothing
# until the files land, rather than failing terraform validate/plan.
# -----------------------------------------------------------------------
locals {
  frontend_dir = "${path.module}/../../../frontend"
  frontend_files = {
    "index.html" = "text/html"
    "styles.css" = "text/css"
    "app.js"     = "application/javascript"
  }
}

resource "aws_s3_object" "frontend" {
  for_each = {
    for name, content_type in local.frontend_files :
    name => content_type if fileexists("${local.frontend_dir}/${name}")
  }

  bucket       = module.static_site.bucket_name
  key          = each.key
  source       = "${local.frontend_dir}/${each.key}"
  etag         = filemd5("${local.frontend_dir}/${each.key}")
  content_type = each.value
}

resource "aws_s3_object" "frontend_config" {
  bucket       = module.static_site.bucket_name
  key          = "config.js"
  content_type = "application/javascript"

  content = <<-EOT
    window.PUBLIC_API_URL = "${aws_apigatewayv2_stage.public_default.invoke_url}";
    window.SITE_URL = "${local.site_url}";
  EOT
}

# =========================================================================
# Phase 5 -- Feedback loop: a fifth Lambda (from the same shared deployment
# package above, sharing the same aws_iam_role.lambda_exec -- it already has
# DynamoDB access to the two new Phase 5 tables via
# module.app_data.table_arns, plus bedrock:InvokeModel from Phase 1, which
# is everything weekly_reflection_handler.py needs; no new IAM role or
# policy resource required) on a single, static, Terraform-managed weekly
# schedule.
#
# Unlike Phase 3's per-topic dynamic scheduling (research_tick/daily_cycle
# cadence is genuinely per-topic-configurable), this is ONE global
# analytical job that internally loops over all topics with recent
# feedback -- so it gets one fixed weekly cron here, not a runtime-created
# per-topic schedule.
# =========================================================================

resource "aws_lambda_function" "weekly_reflection" {
  function_name = "bloggerbear-dev-weekly-reflection"
  role          = aws_iam_role.lambda_exec.arn
  handler       = "weekly_reflection_handler.handler"
  runtime       = "python3.11"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Static weekly schedule -- Monday 9am UTC, a fixed literal (not
# topic-driven config), since there's nothing per-topic to configure about
# this global job. group_name = "default" matches the same schedule group
# Phase 3's dynamically-created per-topic schedules use.
resource "aws_scheduler_schedule" "weekly_reflection" {
  name                = "bloggerbear-dev-weekly-reflection"
  group_name          = "default"
  schedule_expression = "cron(0 9 ? * MON *)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.weekly_reflection.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# =========================================================================
# Phase 6 -- Observability & hardening: CloudWatch alarms/dashboard for
# all 5 pipeline Lambdas + the daily-cycle state machine/DLQ (see
# infra/modules/observability), a Bedrock-spend budget alarm (see
# infra/bootstrap/main.tf -- account-level, not per-environment, so it
# lives in bootstrap rather than here), and the WAF managed-rule-set +
# logging additions above (aws_wafv2_web_acl.public_api's second rule,
# aws_wafv2_web_acl_logging_configuration.admin/public_api).
# =========================================================================

module "observability" {
  source = "../../modules/observability"

  environment_name = "dev"
  lambda_function_names = [
    aws_lambda_function.research_tick.function_name,
    aws_lambda_function.daily_cycle.function_name,
    aws_lambda_function.admin_api.function_name,
    aws_lambda_function.public_api.function_name,
    aws_lambda_function.weekly_reflection.function_name,
  ]
  state_machine_arn = aws_sfn_state_machine.daily_cycle.arn
  dlq_queue_name    = aws_sqs_queue.pipeline_dlq.name
  alert_email       = var.alert_email
}
