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

locals {
  lambda_env_variables = {
    TOPICS_TABLE           = module.app_data.topics_table_name
    FINDINGS_TABLE         = module.app_data.findings_table_name
    CANDIDATE_IDEAS_TABLE  = module.app_data.candidate_ideas_table_name
    ARTICLES_TABLE         = module.app_data.articles_table_name
    MODERATION_QUEUE_TABLE = module.app_data.moderation_queue_table_name
    CONTENT_BUCKET         = aws_s3_bucket.content.bucket
    BEDROCK_MODEL_ID       = var.bedrock_model_id
  }
}

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
