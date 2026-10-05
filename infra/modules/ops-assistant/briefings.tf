# =============================================================================
# The latest briefing per user, and how a client that cannot wait for the agent gets one
# (lambdas/ops_mcp/briefings.py; docs/enhancements/alexa-plus.md, section 4.3).
#
# Alexa+ wants an answer in well under a second; a briefing takes the agent ten to twenty-five.
# So the MCP server's start_briefing invokes the agent asynchronously, as the caller (their own
# token, passed in the event), and latest_briefing reads back what the agent last wrote. Every
# briefing asked on the page is written too.
#
# What each role gains, and nothing more:
#   - the MCP server: read and update this one table, and invoke this one function;
#   - the agent: write this one table. It never reads it: what the agent wrote after reading
#     hostile text is never put back in front of it (the memory table's rule, kept by keeping
#     the text out of that table and this table out of the agent's reach).
#
# And keep_warm: Alexa+'s latency limit is about the function's cold start more than anything
# the two tools do, so an environment that links Alexa+ can keep one instance warm.
# =============================================================================

resource "aws_dynamodb_table" "briefings" {
  name         = "${var.unique_name_prefix}-${var.environment_name}-ops-briefings"
  billing_mode = "PAY_PER_REQUEST"

  # Overwritten by every briefing and gone after two days: nothing here is worth restoring, and
  # a destroy may take it. Unlike the suggestions table, no protect_data.
  hash_key = "user_id"

  attribute {
    name = "user_id"
    type = "S"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

data "aws_iam_policy_document" "ops_mcp_briefings" {
  statement {
    sid       = "ReadAndStartBriefings"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.briefings.arn]
  }

  # Asynchronously, as the caller (ops_mcp/briefings.py): the one function, by its ARN.
  statement {
    sid       = "StartTheAgent"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.ops_agent.arn]
  }
}

resource "aws_iam_role_policy" "ops_mcp_briefings" {
  name   = "${local.name}-briefings"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp_briefings.json
}

data "aws_iam_policy_document" "ops_agent_briefings" {
  statement {
    sid       = "WriteBriefings"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.briefings.arn]
  }
}

resource "aws_iam_role_policy" "ops_agent_briefings" {
  name   = "${local.agent_name}-briefings"
  role   = aws_iam_role.ops_agent.id
  policy = data.aws_iam_policy_document.ops_agent_briefings.json
}

# An async invoke that fails is retried twice by default, which here would mean two more model
# runs for one start_briefing. The handler never raises, so a failure is recorded, not retried;
# this makes that so for the cases it cannot catch (a timeout, out of memory). An event that has
# waited five minutes is dropped: the caller has long since been told it did not finish.
resource "aws_lambda_function_event_invoke_config" "ops_agent" {
  function_name                = aws_lambda_function.ops_agent.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

# --- keep_warm -------------------------------------------------------------------------------------

# Named to end in -scheduler-invoke: the deploy role may manage only roles that fit its patterns
# (infra/bootstrap/main.tf, LambdaExecRole), and this is the one for a schedule's role.
resource "aws_iam_role" "keep_warm" {
  count = var.keep_warm ? 1 : 0

  name = "${local.name}-scheduler-invoke"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "keep_warm" {
  count = var.keep_warm ? 1 : 0

  name = "${local.name}-keep-warm"
  role = aws_iam_role.keep_warm[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.ops_mcp.arn
    }]
  })
}

# Every five minutes, an invoke that is not HTTP: the Web Adapter hands it to POST /events, which
# answers 204 and does nothing else (ops_mcp/server.py). About 8,600 invocations a month.
resource "aws_scheduler_schedule" "keep_warm" {
  count = var.keep_warm ? 1 : 0

  name                = "${local.name}-keep-warm"
  schedule_expression = "rate(5 minutes)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.ops_mcp.arn
    role_arn = aws_iam_role.keep_warm[0].arn
    input    = jsonencode({ source = "bloggerbear.keep-warm" })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
