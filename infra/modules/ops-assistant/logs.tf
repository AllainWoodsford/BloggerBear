# =============================================================================
# Reading logs: log_review and api_errors (lambdas/ops_mcp/logs.py, log_review.py, api_errors.py).
#
# The owner's rule: the assistant may read the logs of the environments it may read, by
# environment, project and ManagedBy tags. Dev reads dev's; production reads production's and
# what is "shared"; dev never reads production's or the shared ones, production never reads dev's.
# Held three ways, any one of which is enough to keep dev out of production's logs:
#
#   1. this policy: logs:StartQuery only on the Lambda and API access log groups named for a
#      readable environment (/aws/lambda/bloggerbear-<env>-*, /aws/apigateway/bloggerbear-<env>-*),
#      and only when the group carries the project's default tags and that Environment;
#   2. the Deny in isolation.tf, on anything tagged with another Environment;
#   3. the function checks the group's name and lists its tags before every query, and refuses
#      on any difference (ops_mcp/logs.py check_group).
#
# Not the firewall's logs: those are firewall.tf's, production only.
#
# CloudWatch Logs evaluates aws:ResourceTag on the log-group resource for StartQuery and
# ListTagsForResource (Service Authorization Reference, list_amazoncloudwatchlogs.html: both
# actions are on `log-group`, which lists aws:ResourceTag/${TagKey}). Every log group these
# patterns can match is made by Terraform (infra/environments/*/main.tf's aws_cloudwatch_log_group
# resources, and this module's and rest-api's), so it carries the provider's default tags. A group
# that does not (one a Lambda made for itself before Terraform did) is not readable: the statement
# fails closed, and the tool says AWS refused.
#
# Logs Insights' GetQueryResults and StopQuery take no resource (they act on a query id), so they
# are on "*"; without StartQuery on a group there is no query of the role's own to read. The query
# text is fixed in code: the model cannot write one, or point one at another group.
# =============================================================================

locals {
  # Both ARN forms for each pattern: IAM matches StartQuery against the log group with or
  # without the trailing ":*", depending on how the call names it.
  readable_log_group_arns = flatten([
    for env in local.readable_environments : [
      for service in ["lambda", "apigateway"] : [
        "arn:aws:logs:${local.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/${service}/bloggerbear-${env}-*",
        "arn:aws:logs:${local.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/${service}/bloggerbear-${env}-*:*",
      ]
    ]
  ])
}

data "aws_iam_policy_document" "ops_mcp_logs" {
  statement {
    sid       = "QueryReadableEnvironmentLogs"
    effect    = "Allow"
    actions   = ["logs:StartQuery", "logs:ListTagsForResource"]
    resources = local.readable_log_group_arns

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
      values   = local.readable_environments
    }
  }

  statement {
    sid       = "ReadOwnLogQueries"
    effect    = "Allow"
    actions   = ["logs:GetQueryResults", "logs:StopQuery"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ops_mcp_logs" {
  name   = "${local.name}-logs-read"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp_logs.json
}
