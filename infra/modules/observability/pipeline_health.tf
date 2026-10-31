# -----------------------------------------------------------------------
# Model Ranch PR11b (docs/enhancements/model-ranch-prompt-assets-evals.md): one dashboard for the
# whole pipeline, <prefix>-<env>-pipeline-health, in place of the pipeline, lambda-runs and edge
# dashboards. CloudWatch bills about US$3 a month for each dashboard past an account's first three
# (of up to 50 metrics each). Each environment now has this one and, while evals are on, the evals
# module's ranch-and-prompts: four where dev and production share an account, one of them paid. So
# this one stays within 50 metrics: a test counts every entry of every widget's metrics list.
#
# The per-Lambda panels are SEARCH expressions over the pipeline's functions by their exact names,
# one line per function from a single entry, which is what keeps up to two dozen functions inside
# the 50. A search is cut into chunks of 12 names so that none passes CloudWatch's 1,024 characters
# for one search, whatever the prefix. A search finds a function once it has reported in the last
# two weeks, so a function that has not run since draws no line.
#
# Opens on the last 7 days at one point per hour, like the dashboards before it: a job that runs
# every few hours is a dot or two on CloudWatch's 3-hour default. The tiles add up the whole range
# picked at the top right (setPeriodToTimeRange); the trend lines read 30 days whatever it says.
# The recent-errors table is a Logs Insights query, billed per GB scanned each time it loads.
# -----------------------------------------------------------------------

locals {
  pipeline_namespace = "BloggerBear/${var.environment_name}"

  # `FunctionName="a" OR FunctionName="b" ...`, a chunk of names at a time (see the header).
  lambda_search_terms = [
    for chunk in chunklist(sort(var.lambda_function_names), 12) :
    join(" OR ", [for name in chunk : "FunctionName=\"${name}\""])
  ]

  # The functions grouped by timeout, each group searched once and scaled by its own timeout, so
  # every function's p95 duration is drawn as a share of its own timeout (100% is timing out).
  # Functions without a timeout are left out here; the dashboard's precondition refuses them.
  lambda_names_by_timeout = {
    for name in var.lambda_function_names :
    format("%06d", var.lambda_timeouts[name]) => name... if contains(keys(var.lambda_timeouts), name)
  }
  duration_share_searches = flatten([
    for padded, names in local.lambda_names_by_timeout : [
      for chunk in chunklist(sort(names), 12) : {
        timeout_ms = tonumber(padded) * 1000
        terms      = join(" OR ", [for name in chunk : "FunctionName=\"${name}\""])
      }
    ]
  ])

  # The model-catalog counter row (section 2): each counter a model_catalog_job evidence line can
  # carry (lambdas/common/evidence.py), by its field, and the metric it adds to. "reverted" is a
  # review's `reverted` (prices it failed) plus its `unconfirmed` (prices the last review put back
  # because no review could look them up), so both fields add to one metric; drops are a review's
  # `review_dropped` (prices someone else changed meanwhile). `dropped` itself is never read: on an
  # evidence line it names the fields that failed their checks.
  model_catalog_counters = {
    discovered     = "ModelCatalogDiscovered"
    pruned         = "ModelCatalogPruned"
    classified     = "ModelCatalogClassified"
    denied         = "ModelCatalogDenied"
    repriced       = "ModelCatalogRepriced"
    reverted       = "ModelCatalogReverted"
    unconfirmed    = "ModelCatalogReverted"
    review_dropped = "ModelCatalogDropped"
  }
  model_catalog_tiles = [
    ["ModelCatalogDiscovered", "discovered"],
    ["ModelCatalogPruned", "pruned"],
    ["ModelCatalogClassified", "classified"],
    ["ModelCatalogDenied", "denied"],
    ["ModelCatalogRepriced", "repriced"],
    ["ModelCatalogReverted", "reverted"],
    ["ModelCatalogDropped", "dropped"],
  ]

  # The four operator tiles: the metrics behind the alarms that count log lines and that nothing
  # else on this dashboard draws, by metric name alone (no dimension, no search, no expression).
  # The dispatcher's ticks and the rollups are the work-queue and evals modules' own metrics, which
  # this module cannot reference (the evals module takes this module's alerts topic), so they are
  # named here, and a test holds each name to the metric filter that makes it. A tile that reads
  # nothing is a switch that is off (evals) or a quiet week (fallbacks, incidents).
  operator_tiles = [
    ["DispatcherTicks", "dispatcher ticks"],
    ["EvalRollups", "evals rollups"],
    ["PromptFallback", "prompt fallbacks"],
    ["SecurityHighSeverityIncidents", "high-severity security incidents"],
  ]

  # From the API Gateway and firewall locals (api_waf_dashboards.tf): each API's requests, 4XX and
  # 5XX, and each web ACL's blocks per rule, with the CloudFront ACL's searched in both of its
  # dimension shapes.
  api_requests_widgets = [
    for widget in local.api_sections : widget if endswith(try(widget.properties.title, ""), ": requests and errors")
  ]
  waf_per_rule_widgets = [
    for widget in concat(local.waf_regional_sections, local.waf_cloudfront_section) :
    widget if strcontains(try(widget.properties.title, ""), ": blocked per rule")
  ]

  pipeline_health_widgets = concat(
    [
      {
        type   = "text"
        width  = 24
        height = 3
        properties = {
          markdown = "## Pipeline health (${var.environment_name})\nThe tiles add up the **whole** range picked at the top right. Below them: every pipeline Lambda's errors, throttles and p95 duration as a share of its own timeout (one line each), the daily cycle's runs, feedback, each API and web ACL, 30 days of trend, and the latest errors from every Lambda's log. The model ranch and prompts are on **${var.unique_name_prefix}-${var.environment_name}-ranch-and-prompts** while evals are on."
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 4
        properties = {
          title                = "Operator tiles: in the selected range"
          region               = var.aws_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          metrics              = [for tile in local.operator_tiles : [local.pipeline_namespace, tile[0], { stat = "Sum", label = tile[1] }]]
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 4
        properties = {
          title                = "Model catalog: in the selected range"
          region               = var.aws_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          metrics              = [for tile in local.model_catalog_tiles : [local.pipeline_namespace, tile[0], { stat = "Sum", label = tile[1] }]]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Errors per hour, per Lambda"
          region = var.aws_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            for index, terms in local.lambda_search_terms :
            [{ id = "errors${index}", label = "", expression = "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Errors\" (${terms})', 'Sum', 3600)" }]
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Throttles per hour, per Lambda"
          region = var.aws_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            for index, terms in local.lambda_search_terms :
            [{ id = "throttles${index}", label = "", expression = "SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Throttles\" (${terms})', 'Sum', 3600)" }]
          ]
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 6
        properties = {
          title  = "p95 duration as a share of each Lambda's own timeout (100% is timing out)"
          region = var.aws_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "% of timeout" } }
          metrics = [
            for index, search in local.duration_share_searches :
            [{ id = "share${index}", label = "", expression = "100 * SEARCH('{AWS/Lambda,FunctionName} MetricName=\"Duration\" (${search.terms})', 'p95', 3600) / ${search.timeout_ms}" }]
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title  = "Daily cycle: Step Functions runs and the dead-letter queue"
          region = var.aws_region
          view   = "timeSeries"
          period = 3600
          yAxis  = { left = { min = 0, label = "per hour" } }
          metrics = [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "started" }],
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "succeeded" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "failed" }],
            ["AWS/States", "ExecutionsTimedOut", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "timed out" }],
            ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_queue_name, { stat = "Maximum", label = "waiting in the dead-letter queue" }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 12
        height = 6
        properties = {
          title                = "Feedback in the selected range: accepted, then rejected"
          region               = var.aws_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          metrics = [
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_accepted.metric_transformation[0].name, { stat = "Sum", label = "accepted" }],
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_comment_kept.metric_transformation[0].name, { stat = "Sum", label = "of which comments kept" }],
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_rejected.metric_transformation[0].name, { stat = "Sum", label = "rejected" }],
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_model_dropped.metric_transformation[0].name, { stat = "Sum", label = "of which dropped by the screening model" }],
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_screening_budget_used_up.metric_transformation[0].name, { stat = "Sum", label = "of which the model checks were used up" }],
          ]
        }
      },
    ],
    local.api_requests_widgets,
    # `for _ in (cond ? [] : [1])`, not a conditional around the list: the same idiom as the locals
    # it sits beside, for the same reason (api_waf_dashboards.tf).
    [for _ in(length(var.api_dashboard_apis) == 0 ? [] : [1]) : {
      type   = "metric"
      width  = 8
      height = 6
      properties = {
        title   = "API latency, p95 (ms)"
        region  = local.api_region
        view    = "timeSeries"
        period  = 3600
        yAxis   = { left = { min = 0, label = "ms" } }
        metrics = [for api in var.api_dashboard_apis : ["AWS/ApiGateway", "Latency", "ApiName", api.api_name, "Stage", api.stage, { stat = "p95", label = api.label }]]
      }
    }],
    local.waf_per_rule_widgets,
    [
      {
        type   = "metric"
        width  = 24
        height = 6
        properties = {
          title  = "The last 30 days, per day"
          region = var.aws_region
          view   = "timeSeries"
          start  = "-P30D"
          period = 86400
          yAxis  = { left = { min = 0, label = "per day" } }
          metrics = [
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "daily runs succeeded" }],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "daily runs failed" }],
            [local.pipeline_namespace, aws_cloudwatch_log_metric_filter.feedback_accepted.metric_transformation[0].name, { stat = "Sum", label = "feedback accepted" }],
          ]
        }
      },
      {
        type   = "log"
        width  = 24
        height = 8
        properties = {
          title  = "Recent errors (every Lambda)"
          region = var.aws_region
          view   = "table"
          query = join(" | ", concat(
            [for name in var.lambda_function_names : "SOURCE '/aws/lambda/${name}'"],
            [
              "fields @timestamp, @log, @message",
              "filter @message like /unhandled exception|Traceback|ERROR|Task timed out/",
              "sort @timestamp desc",
              "limit 50",
            ],
          ))
        }
      },
    ],
  )
}

# The counters for the model-catalog row, one filter per field, on the model-catalog Lambda's log,
# where every model_catalog_job line is printed. A filter matches only lines that carry its field
# (the numeric test fails on a line without it), and adds the field's value. No default value: a
# quiet log publishes nothing, so a counter costs only in the hours it has something to count.
resource "aws_cloudwatch_log_metric_filter" "model_catalog" {
  for_each       = local.model_catalog_counters
  name           = "${var.unique_name_prefix}-${var.environment_name}-model-catalog-${replace(each.key, "_", "-")}"
  log_group_name = var.model_catalog_log_group_name
  pattern        = "{ $.event = \"model_catalog_job\" && $.${each.key} >= 0 }"

  metric_transformation {
    name      = each.value
    namespace = local.pipeline_namespace
    value     = "$.${each.key}"
  }
}

resource "aws_cloudwatch_dashboard" "pipeline_health" {
  dashboard_name = "${var.unique_name_prefix}-${var.environment_name}-pipeline-health"

  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets        = local.pipeline_health_widgets
  })

  lifecycle {
    precondition {
      condition     = alltrue([for name in var.lambda_function_names : contains(keys(var.lambda_timeouts), name)])
      error_message = "Every function in lambda_function_names needs its timeout in lambda_timeouts, for the duration panel."
    }
  }
}
