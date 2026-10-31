# Plans the observability module with a mocked AWS provider, so expressions are evaluated with real values the
# way `terraform plan` does. `terraform validate` cannot: it passed while the old edge dashboard's
# widget lists failed every plan with "Inconsistent conditional result types". Run by
# .github/workflows/pr-checks.yml (`terraform test` in each module that has a tests/ folder).

mock_provider "aws" {}

variables {
  # What a root passes when UNIQUE_NAME_PREFIX is not set: the original deployment's prefix.
  unique_name_prefix = "bloggerbear"

  # What a root passes when nothing is set: the original deployment's region.
  aws_region = "ap-southeast-2"

  environment_name = "test"
  # Production's pipeline: every function observability is given, eval-jobs and the dispatcher
  # included, with their real timeouts (seven different ones).
  lambda_function_names = [
    "acme-test-research-tick", "acme-test-daily-cycle", "acme-test-admin-api",
    "acme-test-public-api", "acme-test-weekly-reflection", "acme-test-dlq-handler",
    "acme-test-musing-feedback", "acme-test-stats-rollover", "acme-test-cost-explorer-poll",
    "acme-test-model-catalog", "acme-test-security-events", "acme-test-sign-in-events",
    "acme-test-reviewer", "acme-test-eval-jobs", "acme-test-dispatcher",
  ]
  lambda_timeouts = {
    "acme-test-research-tick"      = 120
    "acme-test-daily-cycle"        = 300
    "acme-test-admin-api"          = 30
    "acme-test-public-api"         = 30
    "acme-test-weekly-reflection"  = 120
    "acme-test-dlq-handler"        = 30
    "acme-test-musing-feedback"    = 60
    "acme-test-stats-rollover"     = 30
    "acme-test-cost-explorer-poll" = 30
    "acme-test-model-catalog"      = 600
    "acme-test-security-events"    = 60
    "acme-test-sign-in-events"     = 5
    "acme-test-reviewer"           = 600
    "acme-test-eval-jobs"          = 900
    "acme-test-dispatcher"         = 60
  }
  state_machine_arn            = "arn:aws:states:ap-southeast-2:111111111111:stateMachine:test"
  dlq_queue_name               = "bloggerbear-test-pipeline-dlq"
  feedback_log_group_name      = "/aws/lambda/bloggerbear-test-public-api"
  model_catalog_log_group_name = "/aws/lambda/acme-test-model-catalog"
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

# One dashboard for the pipeline (the pipeline, lambda-runs and edge dashboards are gone): every
# panel the spec lists, each widget's metrics a list of lists (CloudWatch checks the body only at
# apply), and within the 50 metrics of a free dashboard (past 50 it is paid, like a fourth one).
# The count takes each entry of a widget's metrics as one metric, which is how AWS words the free
# tier ("up to 50 metrics") but has not been checked against a bill: a search entry draws a line
# per function, and whether the free tier counts it once or per line is checked after the dev apply.
run "pipeline_health_has_every_panel_within_50_metrics" {
  command = plan

  assert {
    condition     = aws_cloudwatch_dashboard.pipeline_health.dashboard_name == "${var.unique_name_prefix}-${var.environment_name}-pipeline-health"
    error_message = "the dashboard is <prefix>-<env>-pipeline-health"
  }

  # The header, the operator tiles, the model-catalog row, errors, throttles, duration, the daily
  # cycle, feedback, two APIs' requests and their latency, three web ACLs' blocks per rule, the
  # 30-day trend and the recent errors.
  assert {
    condition     = length(jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets) == 16
    error_message = "sixteen widgets with two APIs and three web ACLs"
  }

  assert {
    condition = sum([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      length(try(widget.properties.metrics, []))
    ]) <= 50
    error_message = "at most 50 metrics on the dashboard, counting every entry of every widget's metrics"
  }

  assert {
    condition = alltrue(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      [for metric in try(widget.properties.metrics, []) : can(metric[0])]
    ]))
    error_message = "every widget's metrics must be a list of lists"
  }

  assert {
    condition = (
      jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).start == "-P7D" &&
      jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).periodOverride == "inherit"
    )
    error_message = "it opens on a week, at the period each widget asks for"
  }

  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      widget.properties.title if can(widget.properties.title)
      ]) == toset([
      "Operator tiles: in the selected range",
      "Model catalog: in the selected range",
      "Errors per hour, per Lambda",
      "Throttles per hour, per Lambda",
      "p95 duration as a share of each Lambda's own timeout (100% is timing out)",
      "Daily cycle: Step Functions runs and the dead-letter queue",
      "Feedback in the selected range: accepted, then rejected",
      "Public API: requests and errors",
      "Admin API: requests and errors",
      "API latency, p95 (ms)",
      "Public API: blocked per rule (incl. rate limits)",
      "Admin API: blocked per rule (incl. rate limits)",
      "Site: blocked per rule (incl. rate limit)",
      "The last 30 days, per day",
      "Recent errors (every Lambda)",
    ])
    error_message = "the panels the spec lists, each once"
  }
}

# The per-Lambda panels draw every function from a search over the exact names, cut so that no
# search passes CloudWatch's 1,024 characters; the duration panel scales each timeout's functions by
# that timeout, so every function is in exactly one search there.
run "every_lambda_is_searched_once_per_panel_by_its_exact_name" {
  command = plan

  assert {
    condition = alltrue([
      for title in ["Errors per hour, per Lambda", "Throttles per hour, per Lambda", "p95 duration as a share of each Lambda's own timeout (100% is timing out)"] :
      alltrue([
        for name in var.lambda_function_names : length(flatten([
          for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
            for metric in widget.properties.metrics : metric[0].expression
            if strcontains(metric[0].expression, "FunctionName=\"${name}\"")
          ] if try(widget.properties.title, "") == title
        ])) == 1
      ])
    ])
    error_message = "each function is in exactly one search on each per-Lambda panel"
  }

  # Seven timeouts, seven searches, each divided by its own timeout in milliseconds.
  assert {
    condition = toset(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in widget.properties.metrics : regex("/ ([0-9]+)$", metric[0].expression)[0]
      ] if try(widget.properties.title, "") == "p95 duration as a share of each Lambda's own timeout (100% is timing out)"
    ])) == toset(["5000", "30000", "60000", "120000", "300000", "600000", "900000"])
    error_message = "one search per timeout, scaled by that timeout"
  }

  assert {
    condition = contains(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      [for metric in try(widget.properties.metrics, []) : try(metric[0].expression, "")]
    ]), "100 * SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Duration\" (FunctionName=\"acme-test-eval-jobs\")', 'p95', 3600) / 900000")
    error_message = "eval-jobs' p95 as a share of its fifteen minutes"
  }

  # Errors and throttles are counts: each panel searches its own metric, summed per hour, in every
  # chunk. (A renamed panel leaves nothing to check here; the title set above catches that.)
  assert {
    condition = alltrue(flatten([
      for panel in [["Errors per hour, per Lambda", "Errors"], ["Throttles per hour, per Lambda", "Throttles"]] : [
        for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
          for metric in widget.properties.metrics :
          startswith(metric[0].expression, "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"${panel[1]}\" (") &&
          endswith(metric[0].expression, ")', 'Sum', 3600)")
        ] if try(widget.properties.title, "") == panel[0]
      ]
    ]))
    error_message = "the errors panel searches Errors and the throttles panel Throttles, each summed per hour"
  }
}

run "no_search_passes_cloudwatchs_limit_with_the_longest_prefix" {
  command = plan

  variables {
    unique_name_prefix = "fourteen-chars"
    environment_name   = "production"
    # Two dozen functions, so that one search over them all would pass 1,024 characters.
    lambda_function_names = [
      for suffix in ["research-tick", "daily-cycle", "admin-api", "public-api", "weekly-reflection", "dlq-handler",
        "musing-feedback", "stats-rollover", "cost-explorer-poll", "model-catalog", "security-events",
        "sign-in-events", "reviewer", "eval-jobs", "dispatcher", "ops-agent", "ops-mcp", "vision-worker",
        "prompts-publisher", "comment-screening", "gear-refinement", "musing-writer", "reader-digest",
      "rewrite-worker"] : "fourteen-chars-production-${suffix}"
    ]
    lambda_timeouts = {
      for suffix in ["research-tick", "daily-cycle", "admin-api", "public-api", "weekly-reflection", "dlq-handler",
        "musing-feedback", "stats-rollover", "cost-explorer-poll", "model-catalog", "security-events",
        "sign-in-events", "reviewer", "eval-jobs", "dispatcher", "ops-agent", "ops-mcp", "vision-worker",
        "prompts-publisher", "comment-screening", "gear-refinement", "musing-writer", "reader-digest",
      "rewrite-worker"] : "fourteen-chars-production-${suffix}" => 30
    }
  }

  assert {
    condition = alltrue(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in try(widget.properties.metrics, []) :
        length(regex("SEARCH\\('([^']*)'", metric[0].expression)[0]) <= 1024 if can(metric[0].expression)
      ]
    ]))
    error_message = "every search string is at most 1,024 characters"
  }

  # Twenty-four functions in one timeout: every per-Lambda panel searches them twelve at a time.
  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      length(widget.properties.metrics) if contains([
        "Errors per hour, per Lambda", "Throttles per hour, per Lambda",
        "p95 duration as a share of each Lambda's own timeout (100% is timing out)",
      ], try(widget.properties.title, ""))
    ] == [2, 2, 2]
    error_message = "each per-Lambda panel searches the functions twelve at a time: two searches each"
  }
}

run "a_function_without_its_timeout_is_refused" {
  command = plan

  variables {
    lambda_timeouts = { "acme-test-research-tick" = 120 }
  }

  expect_failures = [aws_cloudwatch_dashboard.pipeline_health]
}

# The operator tiles name their metrics alone: no dimension, no search, no expression.
run "the_operator_tiles_are_four_metrics_by_name_alone" {
  command = plan

  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in widget.properties.metrics : [metric[0], metric[1], length(metric)]
      ] if try(widget.properties.title, "") == "Operator tiles: in the selected range"
      ] == [[
        ["BloggerBear/test", "DispatcherTicks", 3],
        ["BloggerBear/test", "EvalRollups", 3],
        ["BloggerBear/test", "PromptFallback", 3],
        ["BloggerBear/test", "SecurityHighSeverityIncidents", 3],
    ]]
    error_message = "four tiles, each a namespace, a metric name and its options"
  }
}

# The model-catalog row: discovered, pruned, classified, denied, repriced, reverted (a review's
# reverted plus its unconfirmed, two filters into one metric) and dropped (review_dropped, never the
# evidence line's own `dropped`).
run "the_model_catalog_row_counts_the_evidence_lines_fields" {
  command = plan

  assert {
    condition = {
      for field, filter in aws_cloudwatch_log_metric_filter.model_catalog :
      field => [filter.pattern, filter.metric_transformation[0].name, filter.metric_transformation[0].value, filter.log_group_name]
      } == {
      for field, metric in {
        discovered     = "ModelCatalogDiscovered"
        pruned         = "ModelCatalogPruned"
        classified     = "ModelCatalogClassified"
        denied         = "ModelCatalogDenied"
        repriced       = "ModelCatalogRepriced"
        reverted       = "ModelCatalogReverted"
        unconfirmed    = "ModelCatalogReverted"
        review_dropped = "ModelCatalogDropped"
      } : field => ["{ $.event = \"model_catalog_job\" && $.${field} >= 0 }", metric, "$.${field}", "/aws/lambda/acme-test-model-catalog"]
    }
    error_message = "one filter per field on the model-catalog log, adding the field's value to its metric"
  }

  assert {
    condition     = alltrue([for filter in aws_cloudwatch_log_metric_filter.model_catalog : filter.metric_transformation[0].default_value == null])
    error_message = "no default value: a quiet log publishes nothing"
  }

  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in widget.properties.metrics : [metric[1], metric[2].label]
      ] if try(widget.properties.title, "") == "Model catalog: in the selected range"
      ] == [[
        ["ModelCatalogDiscovered", "discovered"], ["ModelCatalogPruned", "pruned"],
        ["ModelCatalogClassified", "classified"], ["ModelCatalogDenied", "denied"],
        ["ModelCatalogRepriced", "repriced"], ["ModelCatalogReverted", "reverted"],
        ["ModelCatalogDropped", "dropped"],
    ]]
    error_message = "the row's seven tiles, in order"
  }
}

# The daily cycle, feedback and trend panels draw the metrics the spec lists, with the dimensions
# the module was given. The feedback names are written out (the names main.tf's filters set), so a
# panel pointed at the wrong filter fails too.
run "the_daily_cycle_feedback_and_trend_panels_draw_what_the_spec_lists" {
  command = plan

  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in widget.properties.metrics : [metric[0], metric[1], metric[2], metric[3]]
      ] if try(widget.properties.title, "") == "Daily cycle: Step Functions runs and the dead-letter queue"
      ] == [[
        ["AWS/States", "ExecutionsStarted", "StateMachineArn", var.state_machine_arn],
        ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn],
        ["AWS/States", "ExecutionsFailed", "StateMachineArn", var.state_machine_arn],
        ["AWS/States", "ExecutionsTimedOut", "StateMachineArn", var.state_machine_arn],
        ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_queue_name],
    ]]
    error_message = "started, succeeded, failed and timed out on the state machine, then the dead-letter queue"
  }

  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : [
        for metric in widget.properties.metrics : [metric[0], metric[1]]
      ] if try(widget.properties.title, "") == "Feedback in the selected range: accepted, then rejected"
      ] == [[
        ["BloggerBear/test", "FeedbackAccepted"],
        ["BloggerBear/test", "FeedbackCommentKept"],
        ["BloggerBear/test", "FeedbackRejected"],
        ["BloggerBear/test", "FeedbackModelDropped"],
        ["BloggerBear/test", "FeedbackScreeningBudgetUsedUp"],
    ]]
    error_message = "the funnel's five counts, in order"
  }

  # The trend reads 30 days a point a day, whatever range is picked at the top right.
  assert {
    condition = [
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      [try(widget.properties.start, ""), widget.properties.period, [for metric in widget.properties.metrics : metric[1]]]
      if try(widget.properties.title, "") == "The last 30 days, per day"
    ] == [["-P30D", 86400, ["ExecutionsSucceeded", "ExecutionsFailed", "FeedbackAccepted"]]]
    error_message = "30 days, one point a day: daily runs succeeded and failed, and feedback accepted"
  }
}

# The three tile widgets add up the whole range picked (setPeriodToTimeRange), and the recent
# errors table reads every pipeline Lambda's log once, for the lines an error leaves. The table's
# query moved unchanged from the old pipeline dashboard.
run "the_tiles_add_up_the_range_and_the_recent_errors_read_every_lambdas_log" {
  command = plan

  assert {
    condition = (
      length([
        for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
        widget if try(widget.properties.view, "") == "singleValue"
      ]) == 3 &&
      alltrue([
        for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
        try(widget.properties.setPeriodToTimeRange, false) == true if try(widget.properties.view, "") == "singleValue"
      ])
    )
    error_message = "the operator, model-catalog and feedback tiles each add up the whole range picked"
  }

  assert {
    condition = length([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : widget if widget.type == "log"
    ]) == 1
    error_message = "one Logs Insights widget: the recent errors"
  }

  assert {
    condition = alltrue(flatten([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets : concat(
        [for name in var.lambda_function_names : length(regexall("SOURCE '/aws/lambda/${name}'", widget.properties.query)) == 1],
        [length(regexall("SOURCE '", widget.properties.query)) == length(var.lambda_function_names)],
        [strcontains(widget.properties.query, " | filter @message like /unhandled exception|Traceback|ERROR|Task timed out/ | ")],
      ) if widget.type == "log"
    ]))
    error_message = "every pipeline Lambda's log group once and no other, filtered on the lines an error leaves"
  }
}

# With no API and no web ACL the panels for them go, and the plan still works.
run "nothing_at_the_edge_plans_cleanly" {
  command = plan
  variables {
    api_dashboard_apis = []
    api_cdn            = null
    waf_regional_acls  = []
    waf_cloudfront_acl = null
  }
  assert {
    condition     = length(jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets) == 10
    error_message = "ten widgets with no API and no web ACL"
  }
}

# The API Gateway and firewall widget locals are kept (api_waf_dashboards.tf). Pipeline health
# draws some of them, and the rest is held to the shape CloudWatch insists on.
run "the_kept_api_and_firewall_widgets_are_still_lists_of_lists" {
  command = plan
  assert {
    condition = alltrue(flatten([
      for widget in concat(local.api_gateway_widgets, local.waf_widgets) :
      [for metric in try(widget.properties.metrics, []) : can(metric[0])]
    ]))
    error_message = "every kept widget's metrics must be a list of lists"
  }
  assert {
    condition     = length(local.api_gateway_widgets) == 17 && length(local.waf_widgets) == 15
    error_message = "two APIs, the CDN, two regional web ACLs and the CloudFront one"
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

# A prompt asset that cannot be used is a quiet failure (the pipeline carries on with the code's
# copy), so the evidence line it leaves in whichever Lambda's log raises an alarm of its own.
run "a_prompt_fallback_in_any_pipeline_log_group_raises_one_alarm" {
  command = plan
  variables {
    prompt_fallback_log_groups = ["/aws/lambda/bloggerbear-test-daily-cycle", "/aws/lambda/bloggerbear-test-research-tick"]
  }
  assert {
    condition     = length(aws_cloudwatch_log_metric_filter.prompt_fallback) == 2 && length(aws_cloudwatch_metric_alarm.prompt_fallback) == 1
    error_message = "one metric filter per log group, and one alarm"
  }
  assert {
    condition = alltrue([
      for filter in aws_cloudwatch_log_metric_filter.prompt_fallback :
      filter.pattern == "{ $.event = \"prompt_fallback\" }" && filter.metric_transformation[0].name == aws_cloudwatch_metric_alarm.prompt_fallback[0].metric_name
    ])
    error_message = "every filter selects the prompt_fallback evidence line and feeds the alarm's metric"
  }
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.prompt_fallback[0].alarm_name == "bloggerbear-test-prompt-fallback" &&
      aws_cloudwatch_metric_alarm.prompt_fallback[0].period == 3600 &&
      aws_cloudwatch_metric_alarm.prompt_fallback[0].threshold == 0 &&
      aws_cloudwatch_metric_alarm.prompt_fallback[0].comparison_operator == "GreaterThanThreshold" &&
      aws_cloudwatch_metric_alarm.prompt_fallback[0].treat_missing_data == "notBreaching"
    )
    error_message = "the first fallback in an hour is enough, and no data is not an alarm"
  }
  assert {
    condition     = output.prompt_fallback_metric == { namespace = "BloggerBear/test", name = aws_cloudwatch_metric_alarm.prompt_fallback[0].metric_name }
    error_message = "the output names the filters' metric, for the ranch-and-prompts dashboard"
  }
}

run "no_prompt_fallback_alarm_without_log_groups" {
  command = plan
  assert {
    condition     = length(aws_cloudwatch_metric_alarm.prompt_fallback) == 0
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
# original deployment's region the dashboard names the home region, and us-east-1 for CloudFront.
run "by_default_the_dashboard_reads_the_original_region" {
  command = plan
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["ap-southeast-2", "us-east-1"])
    error_message = "every widget reads the home region, but the CloudFront web ACL's, in us-east-1"
  }
}

# Given another region, the Lambda, API and regional web ACL widgets follow it. The CloudFront one
# does not: a CLOUDFRONT-scope web ACL's metrics exist only in us-east-1, whichever region the
# deployment calls home.
run "another_region_moves_every_widget_but_the_cloudfront_one" {
  command = plan
  variables {
    aws_region        = "eu-west-1"
    state_machine_arn = "arn:aws:states:eu-west-1:111111111111:stateMachine:test"
  }
  assert {
    condition = toset([
      for widget in jsondecode(aws_cloudwatch_dashboard.pipeline_health.dashboard_body).widgets :
      widget.properties.region if can(widget.properties.region)
    ]) == toset(["eu-west-1", "us-east-1"])
    error_message = "every widget should follow the home region, but the CloudFront web ACL's"
  }
  assert {
    condition     = !strcontains(aws_cloudwatch_dashboard.pipeline_health.dashboard_body, "ap-southeast-2")
    error_message = "nothing on the dashboard should still name the original deployment's region"
  }
}

# The prefix is the caller's (var.unique_name_prefix): with the original deployment's, every name
# is what it has always been; with another, they all move.
run "the_default_prefix_keeps_the_names_they_have_always_had" {
  command = plan
  assert {
    condition     = aws_sns_topic.alerts.name == "bloggerbear-test-alerts" && aws_cloudwatch_dashboard.pipeline_health.dashboard_name == "bloggerbear-test-pipeline-health"
    error_message = "with the default prefix the topic and the dashboard are bloggerbear-<env>-<what>"
  }
}

run "another_name_prefix_moves_every_name" {
  command = plan
  variables {
    unique_name_prefix = "acme-blog"
  }
  assert {
    condition     = aws_sns_topic.alerts.name == "acme-blog-test-alerts" && aws_cloudwatch_dashboard.pipeline_health.dashboard_name == "acme-blog-test-pipeline-health"
    error_message = "the topic and the dashboard are <prefix>-<env>-<what>"
  }
  assert {
    condition     = alltrue([for alarm in aws_cloudwatch_metric_alarm.lambda_errors : startswith(alarm.alarm_name, "acme-blog-test-")])
    error_message = "every alarm starts with <prefix>-<env>-, which is the prefix the assistant asks CloudWatch for"
  }
  assert {
    condition     = alltrue([for filter in aws_cloudwatch_log_metric_filter.model_catalog : startswith(filter.name, "acme-blog-test-model-catalog-")])
    error_message = "every counter's filter is <prefix>-<env>-model-catalog-<field>"
  }
}

# A dropped scheduled run is silent (the function is never invoked), so it has an alarm of its own.
run "a_dropped_scheduled_run_raises_an_alarm" {
  command = plan
  assert {
    condition = (
      aws_cloudwatch_metric_alarm.schedule_invocations_dropped.namespace == "AWS/Scheduler" &&
      aws_cloudwatch_metric_alarm.schedule_invocations_dropped.metric_name == "InvocationDroppedCount" &&
      aws_cloudwatch_metric_alarm.schedule_invocations_dropped.dimensions["ScheduleGroup"] == "default" &&
      aws_cloudwatch_metric_alarm.schedule_invocations_dropped.threshold == 0 &&
      aws_cloudwatch_metric_alarm.schedule_invocations_dropped.treat_missing_data == "notBreaching"
    )
    error_message = "one dropped run in the default schedule group is enough, and no data is not an alarm"
  }
  assert {
    condition     = aws_cloudwatch_metric_alarm.schedule_invocations_dropped.alarm_name == "bloggerbear-test-schedule-invocations-dropped"
    error_message = "named <prefix>-<env>-..., the prefix the deploy role and the assistant's alarms tool go by"
  }
}
