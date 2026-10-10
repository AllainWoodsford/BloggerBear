# -----------------------------------------------------------------------
# The vision worker: OpenCV 5 on Graviton (arm64), next to the imagery.
# Design: docs/enhancements/opencv-agentic-vision-enhancement.md (sections 2 to 4).
#
#   research tick (home region) -- satellite_vision adapter -- common/vision_client.py
#        | lambda:InvokeFunction, across regions (the client uses the region in this ARN)
#        v
#   this Lambda (var.region, us-west-2 by default: where sentinel-cogs is)
#        | HTTPS range reads of the public Sentinel-2 COGs; nothing else
#        v
#   metrics + one small PNG in the reply. It stores nothing and reads no table or bucket of ours.
#
# What is different from the pipeline Lambdas, each on purpose:
#
# 1. Its own Region. Every resource here sets `region` itself (AWS provider 6.x, as
#    infra/modules/web-search does), so the calling root needs no aliased provider.
# 2. arm64 and python3.12. Graviton is what the competition's COOL award is about; python3.12
#    because numpy 2.4's wheels need glibc 2.27+, which python3.11's Amazon Linux 2 lacks
#    (lambdas/requirements-vision.txt).
# 3. Its own package, uploaded through S3. OpenCV and numpy are ~130 MB unpacked and ~51 MB
#    zipped, at Lambda's 50 MB direct-upload limit, so the zip goes to a small artifacts bucket in
#    the function's own Region (Lambda reads a package only from a bucket in its Region).
# 4. Its own role, with nothing but its log group: the imagery is public and read over HTTPS.
#
# The calling root creates this only when var.vision_enabled is true; merging this module deploys
# nothing until then. The deploy role needs Lambda and log-group rights in var.region first
# (infra/bootstrap's VisionWorker statements), applied by hand like the rest of the bootstrap.
# -----------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # 6.0: the per-resource `region` argument.
      version = ">= 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name        = "${var.unique_name_prefix}-${var.environment_name}-vision-worker"
  lambdas_dir = "${path.module}/../../../lambdas"
  build_dir   = "${path.module}/lambda-build/vision-worker-${var.environment_name}"
  # Bucket names are global: the account id keeps a fork's from colliding with this one's, and
  # the Region says where it is.
  artifacts_bucket = "${var.unique_name_prefix}-${var.environment_name}-vision-artifacts-${data.aws_caller_identity.current.account_id}-${var.region}"
}

# -----------------------------------------------------------------------
# Package: the vision core, the handler and the one shared module it imports
# -----------------------------------------------------------------------

# Built like the ops assistant's packages (infra/modules/ops-assistant/agent.tf): a staging
# directory, pip told the target platform. What differs:
#
# - The platform is Linux aarch64 and CPython 3.12. Both manylinux tags are given: OpenCV ships
#   manylinux2014, numpy 2.4 manylinux_2_28 (glibc 2.28; the python3.12 runtime has 2.34).
# - What is copied: vision/, the handler, and from common/ only __init__.py and
#   vision_contract.py, which import nothing pip installs beyond requirements-vision.txt.
# - Tests, type stubs and OpenCV's Haar cascades (cv2/data, unused) are removed: ~20 MB.
resource "terraform_data" "package" {
  triggers_replace = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      build_dir="${local.build_dir}"
      rm -rf "$build_dir"
      mkdir -p "$build_dir/common"
      cp -r "${local.lambdas_dir}/vision" "$build_dir/vision"
      cp "${local.lambdas_dir}/vision_worker_handler.py" "$build_dir/vision_worker_handler.py"
      cp "${local.lambdas_dir}/common/__init__.py" "$build_dir/common/__init__.py"
      cp "${local.lambdas_dir}/common/vision_contract.py" "$build_dir/common/vision_contract.py"
      if python3 -c "" >/dev/null 2>&1; then
        py_cmd="python3"
      else
        py_cmd="py -3"
      fi
      $py_cmd -m pip install --upgrade --no-cache-dir \
        --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
        --implementation cp --python-version 3.12 --only-binary=:all: \
        -r "${local.lambdas_dir}/requirements-vision.txt" \
        -t "$build_dir"
      find "$build_dir" -type d \( -name tests -o -name __pycache__ \) -prune -exec rm -rf {} +
      find "$build_dir" -name "*.pyi" -delete
      rm -rf "$build_dir/cv2/data"
    EOT
  }
}

# output_path carries the build's id so the directory is read at apply, after the build (see
# data.archive_file.lambdas in infra/environments/dev/main.tf).
data "archive_file" "package" {
  type        = "zip"
  source_dir  = local.build_dir
  output_path = "${path.module}/lambda-build/vision-worker-${var.environment_name}-${terraform_data.package.id}.zip"
}

resource "aws_s3_bucket" "artifacts" {
  # checkov:skip=CKV_AWS_21:each package is written once under its own MD5 key and expires after 14 days; the repository and Terraform rebuild it, so there is no history to keep
  region        = var.region
  bucket        = local.artifacts_bucket
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  region = var.region
  bucket = aws_s3_bucket.artifacts.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "artifacts" {
  region = var.region
  bucket = aws_s3_bucket.artifacts.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# AVD-AWS-0132 (no customer-managed KMS key): SSE-S3, as every bucket in this project; see the
# content bucket's comment in infra/environments/dev/main.tf for the reasoning.
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  region = var.region
  bucket = aws_s3_bucket.artifacts.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  region = var.region
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "expire-old-packages"
    status = "Enabled"

    filter {
      prefix = "vision-worker/"
    }

    expiration {
      days = 14
    }
  }

  # An apply interrupted mid-upload would otherwise leave the parts of a ~50 MB zip behind, billed, with
  # no object to show for them. Checkov (CKV_AWS_300) wants this on a rule that covers the whole bucket,
  # hence the empty filter rather than the prefix above.
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

resource "aws_s3_object" "package" {
  region = var.region
  bucket = aws_s3_bucket.artifacts.id
  key    = "vision-worker/${data.archive_file.package.output_md5}.zip"
  source = data.archive_file.package.output_path
  # A new key per build, so the function is only told to update when the code changed.
  source_hash = data.archive_file.package.output_base64sha256
}

# -----------------------------------------------------------------------
# Role: its own log group, nothing else
# -----------------------------------------------------------------------

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Named "...-lambda-exec" so it falls under the pattern the CI deploy role may create and pass
# (infra/bootstrap/main.tf's LambdaExecRole). IAM is global; the role works in any Region.
resource "aws_iam_role" "worker" {
  name               = "${local.name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    sid     = "OwnLogGroup"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      "${aws_cloudwatch_log_group.worker.arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "worker" {
  name   = "${local.name}-logs"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_cloudwatch_log_group" "worker" {
  region            = var.region
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 90
}

# -----------------------------------------------------------------------
# The function
# -----------------------------------------------------------------------

resource "aws_lambda_function" "worker" {
  region        = var.region
  function_name = local.name
  depends_on    = [aws_cloudwatch_log_group.worker, aws_iam_role_policy.worker]
  role          = aws_iam_role.worker.arn
  handler       = "vision_worker_handler.lambda_handler"
  runtime       = "python3.12"
  architectures = ["arm64"]
  # OpenCV's work is a few hundred ms on a site; most of a call is waiting on range reads. Memory
  # also buys CPU on Lambda, and the benchmark (PR 8) will say whether 2 GB is the right size.
  memory_size = var.memory_size
  timeout     = var.timeout

  s3_bucket        = aws_s3_object.package.bucket
  s3_key           = aws_s3_object.package.key
  source_code_hash = data.archive_file.package.output_base64sha256

  environment {
    variables = {
      VISION_BACKEND = "opencv"
    }
  }
}
