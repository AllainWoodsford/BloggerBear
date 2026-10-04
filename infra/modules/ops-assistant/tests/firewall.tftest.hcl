# firewall_review's IAM (firewall.tf), planned with a mocked AWS provider like the other tests here.
# The operator's rule: dev's assistant must not see the firewall; production's may. Held here: no
# policy and no log groups for the function unless account_wide_data is on and groups are given;
# with both, StartQuery on exactly those groups; and a group that belongs to another environment
# is refused before anything is planned.

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
  aws_region       = "ap-southeast-2"
  environment_name = "production"
  tables = {
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-production-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-production-model-config"
    }
  }
  content_bucket_name     = "bloggerbear-production-content"
  content_bucket_arn      = "arn:aws:s3:::bloggerbear-production-content"
  stage_name              = "production"
  hosted_ui_domain_prefix = "bloggerbear-production-ops"
  callback_urls           = ["https://example.com/ask.html"]
  logout_urls             = ["https://example.com/ask.html"]
  mfa_configuration       = "ON"
  throttling_rate_limit   = 5
  throttling_burst_limit  = 10
  agent_model_id          = "au.example.test-model-v1:0"
}

run "dev_has_no_right_to_any_firewall_log_and_no_groups_to_read" {
  command = plan

  variables {
    environment_name        = "dev"
    stage_name              = "dev"
    hosted_ui_domain_prefix = "bloggerbear-dev-ops"
    account_wide_data       = false
    waf_log_groups          = [{ region = "ap-southeast-2", name = "aws-waf-logs-bloggerbear-dev-admin" }]
  }

  assert {
    condition     = length(aws_iam_role_policy.ops_mcp_firewall) == 0 && length(data.aws_iam_policy_document.ops_mcp_firewall) == 0
    error_message = "without account_wide_data there is no firewall policy, whatever groups are passed"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_WAF_LOG_GROUPS"] == ""
    error_message = "and the function is told no groups, so it registers no tool"
  }
}

run "production_without_groups_has_no_policy_either" {
  command = plan

  variables {
    account_wide_data = true
  }

  assert {
    condition     = length(aws_iam_role_policy.ops_mcp_firewall) == 0
    error_message = "no groups, no policy"
  }
}

run "production_may_query_exactly_its_groups" {
  command = plan

  variables {
    account_wide_data = true
    waf_log_groups = [
      { region = "ap-southeast-2", name = "aws-waf-logs-bloggerbear-production-admin" },
      { region = "us-east-1", name = "aws-waf-logs-bloggerbear-shared" },
    ]
  }

  assert {
    condition     = length(aws_iam_role_policy.ops_mcp_firewall) == 1
    error_message = "production with groups gets the policy"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_firewall[0].statement : statement.resources if statement.sid == "QueryFirewallLogs"
      ])) == toset([
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:aws-waf-logs-bloggerbear-production-admin",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:aws-waf-logs-bloggerbear-production-admin:*",
      "arn:aws:logs:us-east-1:111111111111:log-group:aws-waf-logs-bloggerbear-shared",
      "arn:aws:logs:us-east-1:111111111111:log-group:aws-waf-logs-bloggerbear-shared:*",
    ])
    error_message = "StartQuery on the named groups only"
  }

  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_mcp_firewall[0].statement[*].actions)) == toset([
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:StopQuery",
    ])
    error_message = "query the logs, and nothing else: no reading log events, no describing groups"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables["OPS_WAF_LOG_GROUPS"] == "ap-southeast-2:aws-waf-logs-bloggerbear-production-admin,us-east-1:aws-waf-logs-bloggerbear-shared"
    error_message = "the function is told the groups as region:name"
  }
}

run "another_environments_group_is_refused" {
  command = plan

  variables {
    account_wide_data = true
    waf_log_groups    = [{ region = "ap-southeast-2", name = "aws-waf-logs-bloggerbear-dev-admin" }]
  }

  expect_failures = [var.waf_log_groups]
}
