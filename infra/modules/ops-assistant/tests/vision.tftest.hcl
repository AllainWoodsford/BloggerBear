# The assistant's vision round (vision.tf), planned with a mocked AWS provider like the other tests
# here. Held: nothing is granted and no Region is told where vision is off; where it is on, the
# worker's one log group in vision_region under the project's tags and exactly this environment,
# and GetObject on the running states only; vision on with no Region, or a Region that is not one,
# is refused at plan; and only the page's app client is a screen caller.

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

# A known id for the page's app client, so the environment variable is a plain string at plan time.
override_resource {
  target          = aws_cognito_user_pool_client.page
  override_during = plan
  values = {
    id = "pageclient0123456789abcdef"
  }
}

variables {
  # What a root passes when UNIQUE_NAME_PREFIX is not set: the original deployment's prefix.
  unique_name_prefix = "bloggerbear"

  # The roots' provider default_tags, without Environment and TerraformRoot.
  default_tags = {
    ManagedBy = "Terraform"
    Project   = "BloggerBear"
  }

  aws_region       = "ap-southeast-2"
  environment_name = "dev"
  tables = {
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-dev-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-dev-model-config"
    }
  }
  content_bucket_name           = "bloggerbear-dev-content"
  content_bucket_arn            = "arn:aws:s3:::bloggerbear-dev-content"
  sign_in_trigger_function_arn  = "arn:aws:lambda:ap-southeast-2:123456789012:function:bloggerbear-test-sign-in-events"
  sign_in_trigger_function_name = "bloggerbear-test-sign-in-events"
  stage_name                    = "dev"
  hosted_ui_domain_prefix       = "bloggerbear-dev-ops"
  callback_urls                 = ["https://example.com/ask.html"]
  logout_urls                   = ["https://example.com/ask.html"]
  mfa_configuration             = "ON"
  throttling_rate_limit         = 5
  throttling_burst_limit        = 10
  agent_model_id                = "au.example.test-model-v1:0"
}

run "vision_off_grants_nothing_and_tells_no_region" {
  command = plan

  variables {
    vision_enabled = false
    vision_region  = "us-west-2"
  }

  assert {
    condition     = length(data.aws_iam_policy_document.ops_mcp_vision) == 0 && length(aws_iam_role_policy.ops_mcp_vision) == 0
    error_message = "with vision off there is no vision policy, whatever Region is passed"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_VISION_REGION"] == ""
    error_message = "and the function is told no Region, so it reads nothing of the vision pipeline"
  }
}

run "vision_on_reads_the_workers_log_there_and_the_running_states_only" {
  command = plan

  variables {
    vision_enabled = true
    vision_region  = "us-west-2"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_vision[0].statement : statement.resources if statement.sid == "QueryVisionWorkerLog"
      ])) == toset([
      "arn:aws:logs:us-west-2:111111111111:log-group:/aws/lambda/bloggerbear-dev-vision-worker",
      "arn:aws:logs:us-west-2:111111111111:log-group:/aws/lambda/bloggerbear-dev-vision-worker:*",
    ])
    error_message = "exactly the worker's own log group, in vision_region, in both ARN forms"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp_vision[0].statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "QueryVisionWorkerLog"
      ])) == toset([
      "StringEquals aws:ResourceTag/ManagedBy Terraform",
      "StringEquals aws:ResourceTag/Project BloggerBear",
      "StringEquals aws:ResourceTag/Environment dev",
    ])
    error_message = "the worker's log is readable only with the project's tags and this environment's"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_vision[0].statement : statement.resources if statement.sid == "ReadVisionRunningState"
    ])) == toset(["arn:aws:s3:::bloggerbear-dev-content/snapshots/*/running-state.json"])
    error_message = "GetObject on the running states only: never the per-capture snapshots"
  }

  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_mcp_vision[0].statement[*].actions)) == toset([
      "logs:StartQuery",
      "logs:ListTagsForResource",
      "s3:GetObject",
    ])
    error_message = "query the worker's log, read its tags and read the running states: nothing else"
  }

  assert {
    condition     = aws_iam_role_policy.ops_mcp_vision[0].name == "bloggerbear-dev-ops-mcp-vision-read"
    error_message = "the MCP server's own policy (test_terraform_wiring.py holds that it is on that role)"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_VISION_REGION"] == "us-west-2"
    error_message = "the function is told where the worker runs"
  }
}

run "production_reads_only_productions_worker_never_shared" {
  command = plan

  variables {
    environment_name        = "production"
    stage_name              = "production"
    hosted_ui_domain_prefix = "bloggerbear-production-ops"
    content_bucket_arn      = "arn:aws:s3:::bloggerbear-production-content"
    vision_enabled          = true
    vision_region           = "us-west-2"
  }

  assert {
    condition = one([
      for statement in data.aws_iam_policy_document.ops_mcp_vision[0].statement : [
        for condition in statement.condition : condition.values if condition.variable == "aws:ResourceTag/Environment"
      ] if statement.sid == "QueryVisionWorkerLog"
    ]) == [["production"]]
    error_message = "the worker is never shared: production's Environment tag is production's alone"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_vision[0].statement : statement.resources if statement.sid == "QueryVisionWorkerLog"
      ])) == toset([
      "arn:aws:logs:us-west-2:111111111111:log-group:/aws/lambda/bloggerbear-production-vision-worker",
      "arn:aws:logs:us-west-2:111111111111:log-group:/aws/lambda/bloggerbear-production-vision-worker:*",
    ])
    error_message = "production's worker, in vision_region"
  }
}

run "vision_on_without_a_region_is_refused" {
  command = plan

  variables {
    vision_enabled = true
    vision_region  = null
  }

  expect_failures = [data.aws_iam_policy_document.ops_mcp_vision]
}

run "a_region_that_is_not_one_is_refused" {
  command = plan

  variables {
    vision_enabled = true
    vision_region  = "nowhere"
  }

  expect_failures = [var.vision_region]
}

run "only_the_pages_app_client_is_a_screen_caller" {
  command = plan

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_SCREEN_CLIENT_IDS"] == "pageclient0123456789abcdef"
    error_message = "the function is told the page's app client id, and no other"
  }
}
