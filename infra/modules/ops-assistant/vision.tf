# =============================================================================
# The assistant's vision round: vision_status, the vision topics in pipeline_health, and
# log_review on the vision worker (lambdas/ops_mcp/vision_status.py, log_review.py;
# docs/enhancements/archive/ops-assistant-vision.md). Only where the root deploys the worker
# (vision_enabled), and least privilege:
#
#   1. StartQuery and ListTagsForResource on the worker's one log group, in vision_region, under
#      the same tag conditions as logs.tf (ManagedBy, Project, and exactly this environment: the
#      worker is never "shared"). GetQueryResults and StopQuery are already logs.tf's, on "*",
#      in any Region.
#   2. GetObject on each topic's running state, snapshots/<topic>/running-state.json in the
#      content bucket, and nothing else under snapshots/.
#
# The main policy document (main.tf) is unchanged. With vision off neither statement exists, and
# the function is told no Region (OPS_VISION_REGION = ""), so it reads nothing.
# =============================================================================

locals {
  # The vision-worker module's own name for it (infra/modules/vision-worker/main.tf local.name);
  # test_terraform_wiring.py holds the two templates equal.
  vision_worker_name          = "${var.unique_name_prefix}-${var.environment_name}-vision-worker"
  vision_worker_log_group_arn = "arn:aws:logs:${coalesce(var.vision_region, "unset")}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.vision_worker_name}"
}

data "aws_iam_policy_document" "ops_mcp_vision" {
  count = var.vision_enabled ? 1 : 0

  lifecycle {
    precondition {
      condition     = var.vision_region != null
      error_message = "vision_enabled needs vision_region: pass the root's var.vision_region."
    }
  }

  # The worker's log group is made by Terraform before the function (vision-worker/main.tf), with
  # the root's default provider and a per-resource region, so it carries the root's default_tags.
  # Both ARN forms: IAM matches StartQuery with or without the trailing ":*".
  statement {
    sid       = "QueryVisionWorkerLog"
    effect    = "Allow"
    actions   = ["logs:StartQuery", "logs:ListTagsForResource"]
    resources = [local.vision_worker_log_group_arn, "${local.vision_worker_log_group_arn}:*"]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/ManagedBy"
      values   = [var.default_tags["ManagedBy"]]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.default_tags["Project"]]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = [var.environment_name]
    }
  }

  # The running state of every topic whose adapter keeps one (snapshots/<topic>/running-state.json),
  # and nothing else under snapshots/: not the per-capture snapshots (snapshots/<topic>/<time>.json,
  # the raw source data of every topic). IAM's * also matches "/", so a topic created with a "/" in
  # its id (the admin API accepts any non-empty id) has its state covered too; the code builds keys
  # only from ids that match ID_PATTERN (ops_mcp/vision_status.py), and refuses any other id before
  # calling S3.
  statement {
    sid       = "ReadVisionRunningState"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.content_bucket_arn}/snapshots/*/running-state.json"]
  }
}

resource "aws_iam_role_policy" "ops_mcp_vision" {
  count = var.vision_enabled ? 1 : 0

  name   = "${local.name}-vision-read"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp_vision[0].json
}
