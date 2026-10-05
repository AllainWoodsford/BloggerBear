# The async briefing (briefings.tf), planned with a mocked AWS provider like the other tests here.
# What is held: each role gains exactly what the design gives it (the MCP server reads and starts,
# the agent writes, neither more), a failed async run is never retried into another model run,
# and keep_warm makes nothing unless asked.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111111111111"
    }
  }
}

override_resource {
  target          = aws_dynamodb_table.briefings
  override_during = plan
  values = {
    arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-ops-briefings"
    name = "bloggerbear-test-ops-briefings"
  }
}

override_resource {
  target          = aws_lambda_function.ops_agent
  override_during = plan
  values = {
    arn = "arn:aws:lambda:ap-southeast-2:111111111111:function:bloggerbear-test-ops-agent"
  }
}

variables {
  # The roots' provider default_tags, without Environment and TerraformRoot.
  default_tags = {
    ManagedBy = "Terraform"
    Project   = "BloggerBear"
  }

  aws_region       = "ap-southeast-2"
  environment_name = "test"
  tables = {
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-test-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"
    }
  }
  content_bucket_name     = "bloggerbear-test-content"
  content_bucket_arn      = "arn:aws:s3:::bloggerbear-test-content"
  stage_name              = "test"
  hosted_ui_domain_prefix = "bloggerbear-test-ops"
  callback_urls           = ["https://example.com/ask.html"]
  logout_urls             = ["https://example.com/ask.html"]
  mfa_configuration       = "OPTIONAL"
  throttling_rate_limit   = 5
  throttling_burst_limit  = 10
  agent_model_id          = "au.example.test-model-v1:0"
}

run "each_role_gains_only_its_part_of_the_briefings" {
  command = plan

  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_mcp_briefings.statement[*].actions)) == toset([
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
      "lambda:InvokeFunction",
    ])
    error_message = "the MCP server may read and start a briefing, and invoke the agent: nothing else"
  }

  assert {
    condition = alltrue([
      for statement in data.aws_iam_policy_document.ops_mcp_briefings.statement :
      statement.resources == toset(statement.sid == "StartTheAgent" ? ["arn:aws:lambda:ap-southeast-2:111111111111:function:bloggerbear-test-ops-agent"] : ["arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-ops-briefings"])
    ])
    error_message = "the MCP server's new rights are on the briefings table and the agent function, by ARN"
  }

  assert {
    condition     = toset(flatten(data.aws_iam_policy_document.ops_agent_briefings.statement[*].actions)) == toset(["dynamodb:PutItem", "dynamodb:UpdateItem"])
    error_message = "the agent writes briefings and never reads them back"
  }

  assert {
    condition     = toset(flatten(data.aws_iam_policy_document.ops_agent_briefings.statement[*].resources)) == toset(["arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-ops-briefings"])
    error_message = "the agent's write is on the briefings table only"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_BRIEFINGS_TABLE"] == "bloggerbear-test-ops-briefings" && aws_lambda_function.ops_mcp.environment[0].variables["OPS_AGENT_FUNCTION"] == "arn:aws:lambda:ap-southeast-2:111111111111:function:bloggerbear-test-ops-agent"
    error_message = "the MCP server is told the table and the agent to start"
  }
}

run "a_failed_async_run_is_not_retried" {
  command = plan

  assert {
    condition     = aws_lambda_function_event_invoke_config.ops_agent.maximum_retry_attempts == 0
    error_message = "a retried async invoke is another model run for one start_briefing"
  }

  assert {
    condition     = aws_dynamodb_table.briefings.ttl[0].enabled && aws_dynamodb_table.briefings.ttl[0].attribute_name == "expires_at"
    error_message = "briefings expire"
  }
}

run "keep_warm_makes_nothing_unless_asked" {
  command = plan

  assert {
    condition     = length(aws_scheduler_schedule.keep_warm) == 0 && length(aws_iam_role.keep_warm) == 0
    error_message = "no schedule and no role by default"
  }
}

run "keep_warm_pings_the_mcp_function_every_five_minutes" {
  command = plan

  variables {
    keep_warm = true
  }

  override_resource {
    target          = aws_lambda_function.ops_mcp
    override_during = plan
    values = {
      arn = "arn:aws:lambda:ap-southeast-2:111111111111:function:bloggerbear-test-ops-mcp"
    }
  }

  assert {
    condition     = aws_scheduler_schedule.keep_warm[0].schedule_expression == "rate(5 minutes)"
    error_message = "one ping every five minutes"
  }

  # The deploy role may only manage roles named to its patterns (infra/bootstrap/main.tf):
  # anything else fails at apply with AccessDenied, after plan and validate have passed.
  assert {
    condition     = endswith(aws_iam_role.keep_warm[0].name, "-scheduler-invoke") && startswith(aws_iam_role.keep_warm[0].name, "bloggerbear-")
    error_message = "the schedule's role must fit the deploy role's bloggerbear-*-scheduler-invoke pattern"
  }

  assert {
    condition     = jsondecode(aws_iam_role_policy.keep_warm[0].policy).Statement[0].Action == "lambda:InvokeFunction" && jsondecode(aws_iam_role_policy.keep_warm[0].policy).Statement[0].Resource == "arn:aws:lambda:ap-southeast-2:111111111111:function:bloggerbear-test-ops-mcp"
    error_message = "the schedule's role may invoke the MCP function, nothing more"
  }
}
