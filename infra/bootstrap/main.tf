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

# Shared permission policy for both deploy roles: only what Phase 0 infra
# touches (S3, CloudFront, WAFv2, Route 53, ACM). Deliberately excludes
# Bedrock, Lambda, and app-table DynamoDB permissions -- those get added
# phase by phase, not granted up front.
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

  # Deliberately excluded (Phase 0 is infra skeleton only, zero application
  # logic): Bedrock, Lambda, and app-table DynamoDB permissions.
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
