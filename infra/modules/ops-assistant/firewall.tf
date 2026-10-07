# =============================================================================
# firewall_review: the deep dive into the firewall's logs (lambdas/ops_mcp/firewall.py;
# docs/enhancements/alexa-plus.md, section 4.4). Production only.
#
# The operator's rule: dev's assistant must not be able to see or reason about the firewall;
# production's may. The firewall's logs are not one environment's (the CloudFront firewall in
# front of both sites logs to one shared group), so this is held three ways, any one of which is
# enough to keep dev out:
#
#   1. this policy exists only where account_wide_data is on and log groups are given (dev
#      passes neither): dev's role has no right to start a query on any WAF log group;
#   2. the function is told the groups only then (OPS_WAF_LOG_GROUPS), and registers the tool
#      only when it has them and may report account-wide data;
#   3. the function refuses a group that is neither this environment's nor the shared one.
#
# Logs Insights' GetQueryResults and StopQuery take no resource (they act on a query id), so they
# are on "*"; without StartQuery on a group there is no query of the role's own to read. The
# query text is fixed in code: the model cannot steer it to another log group.
# =============================================================================

locals {
  firewall_enabled = var.account_wide_data && length(var.waf_log_groups) > 0
}

data "aws_iam_policy_document" "ops_mcp_firewall" {
  # checkov:skip=CKV_AWS_356:GetQueryResults and StopQuery take no resource (they act on a query id)
  count = local.firewall_enabled ? 1 : 0

  statement {
    sid     = "QueryFirewallLogs"
    effect  = "Allow"
    actions = ["logs:StartQuery"]
    # Both ARN forms: IAM matches StartQuery against the log group with or without the trailing
    # ":*", depending on how the call names it.
    resources = flatten([
      for group in var.waf_log_groups : [
        "arn:aws:logs:${group.region}:${data.aws_caller_identity.current.account_id}:log-group:${group.name}",
        "arn:aws:logs:${group.region}:${data.aws_caller_identity.current.account_id}:log-group:${group.name}:*",
      ]
    ])
  }

  statement {
    sid       = "ReadOwnQueries"
    effect    = "Allow"
    actions   = ["logs:GetQueryResults", "logs:StopQuery"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "ops_mcp_firewall" {
  count = local.firewall_enabled ? 1 : 0

  name   = "${local.name}-firewall"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp_firewall[0].json
}
