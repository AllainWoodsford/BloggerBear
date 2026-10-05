# -----------------------------------------------------------------------
# The assistant's memory: the list of what it has suggested, and what it was asked to watch
# (lambdas/ops_mcp/memory.py; the design's section 4).
#
# One table, and the only thing in the account the MCP server's role may write to.
#
# It is made here and not in infra/modules/app-data on purpose. Every table app-data makes has
# its ARN handed to the role the pipeline Lambdas share, which may write and delete on all of
# them. This table is the other way round: writable by the assistant alone, and by nothing in the
# pipeline. Keeping it in this module means no caller has an ARN to hand to anybody else.
# -----------------------------------------------------------------------

# A variable of this table's own, declared beside it. It mirrors app-data's variable of the same
# name: production passes true, and dev's tables are disposable.
variable "protect_data" {
  type        = bool
  default     = false
  description = "Whether the suggestions table is protected like production's app tables: no accidental delete, and point-in-time recovery. Pass what the environment passes to app-data."
}

# Rows are per signed-in user (user_id is the Cognito subject) and of two sorts, told apart by
# how `item` starts: "suggestion#<kind>#<id>" and "watch#<kind>#<id>". Reading one user's rows of
# one sort is a Query on the table's own key, so there is no index.
#
# What a row holds is kinds, ids, booleans and timestamps: never text a model wrote, never a
# command. That rule is kept by the code that writes (memory.py's _write) and by its tests.
resource "aws_dynamodb_table" "operator_suggestions" {
  name         = "${var.unique_name_prefix}-${var.environment_name}-operator-suggestions"
  billing_mode = "PAY_PER_REQUEST"
  # Production only (var.protect_data), as on the app tables: the table cannot be deleted by
  # accident, and can be restored to any second in the last 35 days. Off in dev.
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "user_id"
  range_key = "item"

  attribute {
    name = "user_id"
    type = "S"
  }

  attribute {
    name = "item"
    type = "S"
  }

  # Every row expires 30 days after it was last mentioned (memory.py sets expires_at on each
  # write), so nothing lingers if the assistant is not used for a while.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# The one write the role has, in a policy of its own so that main.tf's stays what it says it is:
# read-only. Five actions, each one memory.py makes:
#
#   Query       a user's suggestion rows, or their watch rows
#   GetItem     one row
#   UpdateItem  every write is an upsert of one row (a repeat updates it; it never adds another)
#   PutItem     allowed with UpdateItem, which may create the row it updates
#   DeleteItem  a suggestion that was fixed or whose article is gone; an item no longer watched
#
# No Scan (nothing reads across users), no BatchWriteItem, and no index: the resource is this
# table's ARN and nothing else. Every other table stays read-only (ReadAppTables in main.tf).
data "aws_iam_policy_document" "ops_mcp_memory" {
  statement {
    sid    = "OwnSuggestionsTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ]
    resources = [aws_dynamodb_table.operator_suggestions.arn]
  }
}

resource "aws_iam_role_policy" "ops_mcp_memory" {
  name   = "${local.name}-own-suggestions"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp_memory.json
}

output "operator_suggestions_table_name" {
  value       = aws_dynamodb_table.operator_suggestions.name
  description = "The assistant's own table: what it has suggested and what it is watching. For looking at, not for handing to another role."
}
