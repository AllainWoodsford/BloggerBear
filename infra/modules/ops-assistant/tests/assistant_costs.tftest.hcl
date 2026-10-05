# The assistant's cost controls (agent.tf; lambdas/ops_agent/quota.py, common/stats_tracking.py),
# planned with a mocked AWS provider like the other tests here. Held: each run's cost can be
# tallied onto the Stats table and nothing else of it; with no Stats table passed there is no such
# right and nothing is tallied; the daily cap reaches the function and cannot be switched off.

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

variables {
  # What a root passes when UNIQUE_NAME_PREFIX is not set: the original deployment's prefix.
  unique_name_prefix = "bloggerbear"

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
    STATS_CURRENT_TABLE = {
      name = "bloggerbear-test-stats-current"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-stats-current"
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

run "the_agent_may_tally_its_cost_on_the_stats_table_and_nothing_more" {
  command = plan

  assert {
    condition     = toset(flatten(data.aws_iam_policy_document.ops_agent_stats[0].statement[*].actions)) == toset(["dynamodb:UpdateItem"])
    error_message = "one ADD per run: UpdateItem, nothing else"
  }

  assert {
    condition     = toset(flatten(data.aws_iam_policy_document.ops_agent_stats[0].statement[*].resources)) == toset(["arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-stats-current"])
    error_message = "on the Stats table only"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.environment[0].variables["STATS_CURRENT_TABLE"] == "bloggerbear-test-stats-current"
    error_message = "the agent is told where to tally"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.environment[0].variables["OPS_AGENT_DAILY_QUESTION_CAP"] == "100"
    error_message = "the cap is on by default: 100 questions a day per user"
  }
}

run "without_a_stats_table_there_is_no_right_and_nothing_is_tallied" {
  command = plan

  variables {
    tables = {
      MODEL_CONFIG_TABLE = {
        name = "bloggerbear-test-model-config"
        arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"
      }
    }
  }

  assert {
    condition     = length(aws_iam_role_policy.ops_agent_stats) == 0 && aws_lambda_function.ops_agent.environment[0].variables["STATS_CURRENT_TABLE"] == ""
    error_message = "no Stats table passed: no policy, and an empty table name"
  }
}

run "the_cap_cannot_be_zero" {
  command = plan

  variables {
    agent_daily_question_cap = 0
  }

  expect_failures = [var.agent_daily_question_cap]
}
