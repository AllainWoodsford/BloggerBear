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
    key          = "production/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-southeast-2"
}

# The CloudFront-scope WAF Web ACL and the ACM certificate used by
# CloudFront must both be declared in us-east-1 regardless of hosting
# region -- an AWS platform requirement (CloudFront only reads
# global-scope WAF ACLs and viewer certificates from that region), not a
# mistake. This is the one place in the whole codebase with a genuine
# us-east-1 provider block.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
}

# -----------------------------------------------------------------------
# Shared WAF Web ACL -- the ONE ACL for both dev and production
# distributions (see infra/environments/dev/terraform.tfvars for how dev
# wires in this ACL's ARN as a manual follow-up). Created here, in
# production, rather than in the reusable module, since it's a singleton
# shared across environments.
# -----------------------------------------------------------------------
resource "aws_wafv2_web_acl" "this" {
  provider = aws.us_east_1

  name        = "bloggerbear-shared"
  description = "Shared WAF Web ACL for BloggerBear's CloudFront distributions (dev + production)."
  scope       = "CLOUDFRONT"

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
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-rate-limit"
      sampled_requests_enabled   = true
    }
  }

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
      metric_name                = "bloggerbear-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-shared-acl"
    sampled_requests_enabled   = true
  }
}

module "static_site" {
  source = "../../modules/static-site"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment_name     = "production"
  enable_custom_domain = true
  force_destroy        = false
  domain_name          = var.domain_name
  hosted_zone_id       = var.hosted_zone_id
  web_acl_id           = aws_wafv2_web_acl.this.arn
}

# =========================================================================
# Phase 1 -- app data tables, content storage, and the Lambda pipeline.
# See docs/project-plan.md §3/§5 and infra/modules/app-data for the
# DynamoDB table set; lambdas/ (Python, owned by the application-code
# workstream) for the two handlers packaged below.
# =========================================================================

module "app_data" {
  source = "../../modules/app-data"

  environment_name = "production"
}

# -----------------------------------------------------------------------
# Content bucket -- read/written directly by Lambda via the SDK, never
# served publicly, so no CloudFront/OAC. Same private-bucket pattern
# (ownership controls + public access block) as the static-site module's
# site bucket. force_destroy left at the default (false) -- unlike dev,
# an accidental destroy must not silently delete real content.
# -----------------------------------------------------------------------
resource "aws_s3_bucket" "content" {
  bucket = "bloggerbear-production-content"
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
  name               = "bloggerbear-production-lambda-exec"
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
    resources = ["arn:aws:logs:ap-southeast-2:*:log-group:/aws/lambda/bloggerbear-production-*"]
  }
}

resource "aws_iam_role_policy" "lambda_exec" {
  name   = "bloggerbear-production-lambda-exec"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_exec.json
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
    RESEARCH_TICK_FUNCTION_NAME = "bloggerbear-production-research-tick"
    DAILY_CYCLE_FUNCTION_NAME   = "bloggerbear-production-daily-cycle"
  }
}

resource "aws_lambda_function" "research_tick" {
  function_name = "bloggerbear-production-research-tick"
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
  function_name = "bloggerbear-production-daily-cycle"
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
# never a browser app. This is a separate, REGIONAL-scope Web ACL from the
# CLOUDFRONT-scope aws_wafv2_web_acl.this above -- that one protects the
# CloudFront distributions; this one protects only this API.
# =========================================================================

resource "aws_lambda_function" "admin_api" {
  function_name = "bloggerbear-production-admin-api"
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
  name   = "bloggerbear-production-lambda-invoke-pipeline"
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
  name          = "bloggerbear-production-admin-api"
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
# Regional WAF IP allowlist -- a different Web ACL from aws_wafv2_web_acl.
# this above (CLOUDFRONT scope, us-east-1, shared by both distributions).
# This one is REGIONAL scope, created in this environment's default
# ap-southeast-2 provider (regional WAF for API Gateway lives in the
# API's own region, no us-east-1 alias needed), and protects only the
# admin API.
#
# default_action = block: until var.admin_allowed_cidrs is set to the
# operator's real public IP (a /32 CIDR), the IP set is empty and NOTHING
# can call this API. That is the deliberately safe default -- fail closed
# -- not a bug.
# -----------------------------------------------------------------------
resource "aws_wafv2_ip_set" "admin_allowlist" {
  name               = "bloggerbear-production-admin-allowlist"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = var.admin_allowed_cidrs
}

resource "aws_wafv2_web_acl" "admin" {
  name        = "bloggerbear-production-admin-api"
  description = "Regional WAF Web ACL for the BloggerBear production admin API -- allows only the operator's allowlisted IP(s); blocks everything else by default."
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
      metric_name                = "bloggerbear-production-admin-allow"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-production-admin-acl"
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "admin" {
  resource_arn = aws_apigatewayv2_stage.default.arn
  web_acl_arn  = aws_wafv2_web_acl.admin.arn
}
