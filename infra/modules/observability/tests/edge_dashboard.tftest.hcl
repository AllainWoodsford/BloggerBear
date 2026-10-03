# Plans the module with a mocked AWS provider, so expressions are evaluated with real values the
# way `terraform plan` does. `terraform validate` cannot: it passed while the edge dashboard's
# widget lists failed every plan with "Inconsistent conditional result types". Run by
# .github/workflows/pr-checks.yml (`terraform test` in each module that has a tests/ folder).

mock_provider "aws" {}

variables {
  environment_name        = "test"
  lambda_function_names   = ["bloggerbear-test-research-tick"]
  state_machine_arn       = "arn:aws:states:ap-southeast-2:111111111111:stateMachine:test"
  dlq_queue_name          = "bloggerbear-test-pipeline-dlq"
  feedback_log_group_name = "/aws/lambda/bloggerbear-test-public-api"
  edge_dashboard_enabled  = true
  api_dashboard_apis = [
    { label = "Public API", api_name = "public", stage = "v1", access_log_group = "/aws/apigateway/public" },
    { label = "Admin API", api_name = "admin", stage = "v1", access_log_group = "/aws/apigateway/admin" },
  ]
  api_cdn = { distribution_id = "E123", additional_metrics_enabled = false, api_name = "public", stage = "v1" }
  waf_regional_acls = [
    { label = "Public API", metric_name = "public-acl", log_group = "aws-waf-logs-public" },
    { label = "Admin API", metric_name = "admin-acl", log_group = "aws-waf-logs-admin" },
  ]
  waf_cloudfront_acl = { label = "Site", metric_name = "shared", log_group = "aws-waf-logs-shared" }
}

run "production_creates_the_edge_dashboard" {
  command = plan
  assert {
    condition     = length(aws_cloudwatch_dashboard.edge) == 1
    error_message = "production should get one edge dashboard"
  }
}

run "dev_does_not" {
  command = plan
  variables {
    edge_dashboard_enabled = false
  }
  assert {
    condition     = length(aws_cloudwatch_dashboard.edge) == 0
    error_message = "the edge dashboard should be off unless enabled"
  }
}

run "nothing_to_show_plans_cleanly" {
  command = plan
  variables {
    api_dashboard_apis = []
    api_cdn            = null
    waf_regional_acls  = []
    waf_cloudfront_acl = null
  }
  assert {
    condition     = length(aws_cloudwatch_dashboard.edge) == 0
    error_message = "no widgets should mean no dashboard"
  }
}
