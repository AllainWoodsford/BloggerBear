# Plans the observability module with a mocked AWS provider, so expressions are evaluated with real values the
# way `terraform plan` does. `terraform validate` cannot: it passed while the edge dashboard's
# widget lists failed every plan with "Inconsistent conditional result types". Run by
# .github/workflows/pr-checks.yml (`terraform test` in each module that has a tests/ folder).

mock_provider "aws" {}

variables {
  # What a root passes when nothing is set: the original deployment's region.
  aws_region = "ap-southeast-2"

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
  # CloudWatch only checks the body at apply, and dev never applies this dashboard, so the shape
  # it insists on is checked here: every entry of a widget's metrics is itself a list.
  assert {
    condition = alltrue(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.edge[0].dashboard_body).widgets :
      [for metric in try(widget.properties.metrics, []) : can(metric[0])]
    ]))
    error_message = "every widget's metrics must be a list of lists"
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

run "the_security_alarm_watches_every_recording_log_group" {
  command = plan
  variables {
    security_alert_log_groups = ["/aws/lambda/bloggerbear-test-security-events", "/aws/lambda/bloggerbear-test-public-api"]
  }
  assert {
    condition     = length(aws_cloudwatch_log_metric_filter.security_high_severity) == 2 && length(aws_cloudwatch_metric_alarm.security_high_severity) == 1
    error_message = "one metric filter per recording log group, and one alarm"
  }
}

run "no_security_alarm_without_log_groups" {
  command = plan
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.security_high_severity) == 0
    error_message = "no alarm without log groups"
  }
}

# alert_email is sensitive (kept out of plans and public CI logs) and drives the subscription's
# count. Terraform accepts a sensitive count (unlike for_each); these keep it that way, set or not.
run "a_sensitive_alert_email_still_subscribes" {
  command = plan
  variables {
    alert_email = "alerts@example.com"
  }
  assert {
    condition     = length(aws_sns_topic_subscription.alerts_email) == 1
    error_message = "an alert email should create one subscription"
  }
}

run "no_alert_email_no_subscription" {
  command = plan
  assert {
    condition     = length(aws_sns_topic_subscription.alerts_email) == 0
    error_message = "no alert email should mean no subscription"
  }
}

# The widgets read var.aws_region, which a root passes as its own var.aws_region. With the
# original deployment's region both dashboards name exactly the regions they always did.
run "by_default_the_dashboards_read_the_original_region" {
  command = plan
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline.dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["ap-southeast-2"])
    error_message = "every pipeline widget should read the home region, and nothing else"
  }
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.edge[0].dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["ap-southeast-2", "us-east-1"])
    error_message = "the edge dashboard reads the home region, and us-east-1 for CloudFront"
  }
}

# Given another region, the API, Lambda and regional web ACL widgets follow it. The CloudFront ones
# do not: CloudFront's metrics, and a CLOUDFRONT-scope web ACL's metrics and logs, exist only in
# us-east-1, whichever region the deployment calls home.
run "another_region_moves_every_widget_but_the_cloudfront_ones" {
  command = plan
  variables {
    aws_region        = "eu-west-1"
    state_machine_arn = "arn:aws:states:eu-west-1:111111111111:stateMachine:test"
  }
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline.dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["eu-west-1"])
    error_message = "every pipeline widget should follow the home region"
  }
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.edge[0].dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["eu-west-1", "us-east-1"])
    error_message = "the edge dashboard should read the home region, and still us-east-1 for CloudFront"
  }
  assert {
    condition     = !strcontains(aws_cloudwatch_dashboard.edge[0].dashboard_body, "ap-southeast-2") && !strcontains(aws_cloudwatch_dashboard.pipeline.dashboard_body, "ap-southeast-2")
    error_message = "nothing on either dashboard should still name the original deployment's region"
  }
}
