# =============================================================================
# What the assistant costs, recorded (docs/enhancements/alexa-plus.md; friction 6.9's sibling:
# what is not counted is not seen).
#
# Every question runs the agent, and every run is several model calls. Two controls live here and
# in the code they configure:
#
#   - each run's tokens and cost are tallied onto the week's Stats row, under the "assistant"
#     category (common/stats_tracking.py): on the Stats page, in the `spend` tool, and per
#     environment, the same week they are spent. What the assistant's other services cost (Lambda,
#     API Gateway, DynamoDB, Cognito, Logs Insights) is on the account's bill, which the daily Cost
#     Explorer poll already groups as Infrastructure;
#   - each user gets OPS_AGENT_DAILY_QUESTION_CAP questions a UTC day (lambdas/ops_agent/quota.py,
#     var.agent_daily_question_cap), counted in the briefings table the agent may already write.
# =============================================================================

# The week's Stats row, where each run's cost is tallied (common/stats_tracking.py). UpdateItem
# only (increment_current_stats is one ADD), on the one table, and only where it was passed.
locals {
  agent_stats_table = lookup(var.tables, "STATS_CURRENT_TABLE", null)
}

data "aws_iam_policy_document" "ops_agent_stats" {
  count = local.agent_stats_table == null ? 0 : 1

  statement {
    sid       = "TallyRunCost"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [local.agent_stats_table.arn]
  }
}

resource "aws_iam_role_policy" "ops_agent_stats" {
  count = local.agent_stats_table == null ? 0 : 1

  name   = "${local.agent_name}-stats"
  role   = aws_iam_role.ops_agent.id
  policy = data.aws_iam_policy_document.ops_agent_stats[0].json
}

