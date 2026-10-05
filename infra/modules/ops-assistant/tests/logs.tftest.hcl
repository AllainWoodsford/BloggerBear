# Reading logs (logs.tf): log_review and api_errors. Planned with a mocked AWS provider like the other tests here.

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
  # The roots' provider default_tags, without Environment and TerraformRoot.
  default_tags = {
    ManagedBy = "Terraform"
    Project   = "BloggerBear"
  }

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

# The owner's rule: environment, project and ManagedBy tags. Dev reads dev's logs alone; production
# reads production's and the shared ones; neither reads the other's.

run "dev_queries_only_dev_lambda_and_access_logs_with_dev_tags" {
  command = plan

  variables {
    environment_name        = "dev"
    stage_name              = "dev"
    hosted_ui_domain_prefix = "bloggerbear-dev-ops"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_logs.statement : statement.resources if statement.sid == "QueryReadableEnvironmentLogs"
      ])) == toset([
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-dev-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-dev-*:*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-dev-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-dev-*:*",
    ])
    error_message = "dev may query dev's Lambda and access logs only: never production's, never shared"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp_logs.statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "QueryReadableEnvironmentLogs"
      ])) == toset([
      "StringEquals aws:ResourceTag/ManagedBy Terraform",
      "StringEquals aws:ResourceTag/Project BloggerBear",
      "StringEquals aws:ResourceTag/Environment dev",
    ])
    error_message = "a log group is readable only with the project's default tags and dev's Environment"
  }

  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_mcp_logs.statement[*].actions)) == toset([
      "logs:StartQuery",
      "logs:ListTagsForResource",
      "logs:GetQueryResults",
      "logs:StopQuery",
    ])
    error_message = "query logs and read their tags, nothing else: no GetLogEvents, no FilterLogEvents, no writes"
  }

  assert {
    condition     = aws_iam_role_policy.ops_mcp_logs.name == "bloggerbear-dev-ops-mcp-logs-read"
    error_message = "the MCP server's own policy (test_terraform_wiring.py holds that it is on that role)"
  }
}

run "production_also_reads_shared_and_never_dev" {
  command = plan

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.ops_mcp_logs.statement : statement.resources if statement.sid == "QueryReadableEnvironmentLogs"
      ])) == toset([
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-production-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-production-*:*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-production-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-production-*:*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-shared-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/lambda/bloggerbear-shared-*:*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-shared-*",
      "arn:aws:logs:ap-southeast-2:111111111111:log-group:/aws/apigateway/bloggerbear-shared-*:*",
    ])
    error_message = "production reads its own logs and the shared ones, and nothing of dev's"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp_logs.statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "QueryReadableEnvironmentLogs"
      ])) == toset([
      "StringEquals aws:ResourceTag/ManagedBy Terraform",
      "StringEquals aws:ResourceTag/Project BloggerBear",
      "StringEquals aws:ResourceTag/Environment production,shared",
    ])
    error_message = "production's tag condition allows production and shared only, with the project's default tags"
  }
}
