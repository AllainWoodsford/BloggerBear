# =============================================================================
# Prompts as assets: the bucket the prompt templates are published to, and the table of the
# hashes the publisher approved (docs/enhancements/model-ranch-prompt-assets-evals.md, sections
# 4.2 and 5.2).
#
# Terraform owns the bucket and the table, never what is in them. The publisher (its own role,
# gha-<prefix>-dev-prompts or gha-<prefix>-prod-prompts, made by infra/bootstrap) writes every
# object and every hash row; the Lambdas only read, through common/prompts.py, and only while the
# root's prompt_as_asset_enabled is on (PROMPT_AS_ASSET_ENABLED). Merging this module therefore
# makes an empty bucket and an empty table that nothing reads.
#
# The bucket's keys:
#   <lambda_id>/<prompt_name>/latest/vN.txt   what is served (one file: the prompt; two or more:
#                                             an A/B split), tagged version, release and sha256
#   <lambda_id>/<prompt_name>/versions/<sha256>.txt   every file ever published, written once
#   releases/<release_id>.json, releases/current.json  what each publish did, and the last one
#
# Who may do what, held three ways:
#   1. lambda_exec gets the reads below and nothing that writes: GetObject and GetObjectTagging
#      on latest/ and versions/, GetObject on releases/ (the admin API's GET /prompts/releases,
#      which runs as lambda_exec), ListBucket, and GetItem on the hash table;
#   2. with a publisher named, the bucket policy refuses every write to an object (put, delete,
#      tag) by anyone else, whatever their own policies allow;
#   3. a Lambda serves an asset only when the hash table says the publisher approved its bytes.
# =============================================================================

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

locals {
  # Whether a publisher is named: the bucket policy below exists only then.
  has_publisher = var.publisher_role_arn != ""
}

# -----------------------------------------------------------------------
# The bucket. Private, versioned, SSE-S3, TLS only (with the policy below).
#
# AVD-AWS-0132 ("no customer-managed KMS key") is deliberately ignored, for the reason given on
# the content bucket in infra/environments/dev/main.tf: every bucket, topic and queue in this
# project uses AWS's default managed-key encryption (SSE-S3 / SSE-SNS / SSE-SQS), not a customer-
# managed KMS key -- a cost/complexity trade-off for a single-operator portfolio project, where
# each additional CMK is a recurring per-key charge plus key-policy/rotation overhead, for a
# threat model (this operator's own AWS account, not shared-tenancy or regulated data) where
# AWS-managed encryption at rest is judged sufficient. The prompts are the code's own text, no
# more secret than the repository they are published from.
#
# The content bucket's two checkov skips (CKV_AWS_21, no versioning in dev; CKV_AWS_300, no
# multipart abort) do not apply: this bucket is versioned in every environment and its lifecycle
# aborts incomplete uploads.
# -----------------------------------------------------------------------
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket" "prompts" {
  bucket = "${var.unique_name_prefix}-${var.environment_name}-prompts"
  # Dev is disposable, and the publisher rebuilds the bucket from the repository; production's
  # cannot be destroyed while it holds a prompt.
  force_destroy = !var.protect_data
}

resource "aws_s3_bucket_ownership_controls" "prompts" {
  bucket = aws_s3_bucket.prompts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "prompts" {
  bucket = aws_s3_bucket.prompts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# The same reason as on the bucket above.
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "prompts" {
  bucket = aws_s3_bucket.prompts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A file the publisher replaces or removes under latest/ stays recoverable as an older version,
# and a rollback is a publish of the old files, so nothing in the bucket is ever only in S3.
resource "aws_s3_bucket_versioning" "prompts" {
  bucket = aws_s3_bucket.prompts.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "prompts" {
  bucket = aws_s3_bucket.prompts.id

  # Versioning has to be on before a lifecycle rule can refer to noncurrent versions.
  depends_on = [aws_s3_bucket_versioning.prompts]

  # The whole bucket: a replaced latest/ file or releases/current.json is kept 90 days after it
  # was replaced, long enough to see what was served and when; versions/ is written once, so its
  # files never become noncurrent. A publish interrupted mid-upload leaves no parts behind.
  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 90
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# -----------------------------------------------------------------------
# The bucket policy: TLS only, and no write to an object but the publisher's.
#
# Only with a publisher named (count): with none, nothing is ever published, and the policy
# would have no one to except. The exception is a condition on aws:PrincipalArn, never a
# Principal or NotPrincipal naming the role: S3 refuses a policy whose Principal names a role
# that does not exist, so an ARN set before its role (the stub setup_repo offers, or a deployment
# that has not applied the bootstrap again since the publisher roles were added) would fail every
# deploy. A condition is only compared, so it is accepted either way, and until the role exists
# the policy simply refuses every writer. aws:PrincipalArn is the role's own ARN for every
# session of it, which is what a GitHub Actions run signs in as.
#
# The writes refused: every call that changes an object's bytes, its tags (the Lambdas read the
# version from the `version` tag) or which of its versions is current (deleting the current
# version brings the one before back). S3's lifecycle expiry is not a principal and is never
# refused. Terraform itself writes no object here; a destroy removes this policy before the bucket.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "bucket" {
  count = local.has_publisher ? 1 : 0

  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.prompts.arn, "${aws_s3_bucket.prompts.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "OnlyThePublisherWrites"
    effect = "Deny"
    actions = [
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:DeleteObjectVersion",
      "s3:PutObjectTagging",
      "s3:PutObjectVersionTagging",
      "s3:DeleteObjectTagging",
      "s3:DeleteObjectVersionTagging",
    ]
    resources = ["${aws_s3_bucket.prompts.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "ArnNotEquals"
      variable = "aws:PrincipalArn"
      values   = [var.publisher_role_arn]
    }
  }
}

resource "aws_s3_bucket_policy" "prompts" {
  count = local.has_publisher ? 1 : 0

  bucket = aws_s3_bucket.prompts.id
  policy = data.aws_iam_policy_document.bucket[0].json

  # S3 checks a new policy against the public-access block, so the block is in place first.
  depends_on = [aws_s3_bucket_public_access_block.prompts]
}

# -----------------------------------------------------------------------
# The hash table: one row per approved file, by the sha256 of its bytes (prompt_id, version,
# release, approved_at, status live|pending|retired, retired_at). No TTL: a retired row is the
# record of what was once approved. Not in the backups (common/backup.TABLES): publishing writes
# it again from the repository. Written only by the publisher (bootstrap gives its role PutItem
# and UpdateItem here); the Lambdas only GetItem.
# -----------------------------------------------------------------------
resource "aws_dynamodb_table" "prompt_hashes" {
  name         = "${var.unique_name_prefix}-${var.environment_name}-prompt-hashes"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data): a table cannot be deleted by accident, and can be restored to any
  # second in the last 35 days. Off in dev, where tables are disposable.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key = "prompt_hash"

  attribute {
    name = "prompt_hash"
    type = "S"
  }
}

# -----------------------------------------------------------------------
# What the shared Lambda role may do here: read, and nothing else. common/prompts.py lists a
# prompt's latest/ files, reads the arm's tags and body, and looks its sha256 up in the hash table;
# the offline scoring runs render a candidate from versions/ (section 5.4 of the spec); the admin
# API (also lambda_exec) lists releases/ and reads current.json for GET /prompts/releases.
# ListBucket is a bucket action, on the bucket's own ARN; it is also what lets a GetObject of a
# key that is not there (releases/current.json before the first publish) answer 404 NoSuchKey
# rather than 403.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_read" {
  statement {
    sid     = "ReadPromptFiles"
    effect  = "Allow"
    actions = ["s3:GetObject", "s3:GetObjectTagging"]
    resources = [
      "${aws_s3_bucket.prompts.arn}/*/latest/*",
      "${aws_s3_bucket.prompts.arn}/*/versions/*",
    ]
  }

  statement {
    sid       = "ReadPromptReleases"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.prompts.arn}/releases/*"]
  }

  statement {
    sid       = "ListPromptFiles"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.prompts.arn]
  }

  statement {
    sid       = "ReadPromptHashes"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.prompt_hashes.arn]
  }
}

resource "aws_iam_role_policy" "lambda_read" {
  name   = "${var.unique_name_prefix}-${var.environment_name}-prompts-read"
  role   = var.lambda_exec_role_id
  policy = data.aws_iam_policy_document.lambda_read.json
}
