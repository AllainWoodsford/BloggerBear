# -----------------------------------------------------------------------
# Phase 6 -- pipeline health observability. One reusable module (like
# app-data/static-site) rather than duplicating ~150 lines of alarm
# definitions across dev/main.tf and production/main.tf -- the only thing
# that differs between environments is which Lambda function names/ARNs
# get passed in, per docs/project-plan.md's Phase 6 scope
# ("CloudWatch dashboards/alarms for pipeline health").
#
# Every alarm publishes to the same per-environment SNS topic; whether
# anyone actually gets paged depends on var.alert_email being set (see
# variables.tf) -- that's a deliberate manual follow-up, same pattern as
# var.admin_allowed_cidrs / var.bedrock_model_id elsewhere in this
# project: code complete, real value supplied via terraform.tfvars once
# the human has an inbox to point it at.
# -----------------------------------------------------------------------
# AVD-AWS-0095 ("topic does not have encryption enabled") ignored
# deliberately -- every bucket/topic/queue in this project uses AWS's
# default managed-key encryption, not a customer-managed KMS key (see
# the same comment on infra/environments/dev/main.tf's
# aws_s3_bucket.content for the full cost/complexity rationale).
# trivy:ignore:AVD-AWS-0095
resource "aws_sns_topic" "alerts" {
  name = "bloggerbear-${var.environment_name}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count = var.alert_email != "" ? 1 : 0

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# One Errors alarm + one Throttles alarm per Lambda function, via for_each
# over the caller-supplied list rather than five hand-copied blocks (there
# are 5 pipeline Lambdas as of Phase 5: research_tick, daily_cycle,
# admin_api, public_api, weekly_reflection). threshold = 0 /
# comparison_operator = GreaterThanThreshold means "alarm on the first
# error/throttle in the period", not some tolerance band -- this is a
# single-operator portfolio project, not a high-traffic service, so any
# error is worth a look. treat_missing_data = "notBreaching" so a quiet
# function (nothing invoked that period) never falsely alarms.
resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = toset(var.lambda_function_names)

  alarm_name          = "bloggerbear-${var.environment_name}-${each.value}-errors"
  alarm_description   = "bloggerbear-${var.environment_name}: ${each.value} had at least one invocation error in the last 5 minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  for_each = toset(var.lambda_function_names)

  alarm_name          = "bloggerbear-${var.environment_name}-${each.value}-throttles"
  alarm_description   = "bloggerbear-${var.environment_name}: ${each.value} was throttled in the last 5 minutes."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = each.value }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# A message sitting in the DLQ means a daily_cycle execution failed both
# of Step Functions' retry attempts (see aws_sfn_state_machine.daily_cycle
# in dev/production main.tf) and needs a human to look at it -- there's no
# automatic replay.
resource "aws_cloudwatch_metric_alarm" "pipeline_dlq_messages" {
  alarm_name          = "bloggerbear-${var.environment_name}-pipeline-dlq-messages"
  alarm_description   = "bloggerbear-${var.environment_name}: the daily-cycle dead-letter queue has at least one failed execution waiting on it."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = var.dlq_queue_name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Belt-and-suspenders alongside the DLQ alarm above: this fires the moment
# an execution fails, before/regardless of whether it ultimately lands in
# the DLQ (e.g. it also catches a failure on the first retry attempt).
resource "aws_cloudwatch_metric_alarm" "daily_cycle_executions_failed" {
  alarm_name          = "bloggerbear-${var.environment_name}-daily-cycle-executions-failed"
  alarm_description   = "bloggerbear-${var.environment_name}: the daily-cycle Step Functions state machine had a failed execution."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  dimensions          = { StateMachineArn = var.state_machine_arn }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Feedback spam. Rejected submissions are stored nowhere and count against no feedback limit (so
# junk cannot use up the room real feedback needs), which also means nothing else notices a flood of
# them. These read public_api_handler.py's own log line -- the reason code only, never the comment --
# into two metrics: every rejection, and rejections because the day's model checks ran out
# (common/feedback_limits.py's screening_limit), after which no comment can be kept until the day
# resets. Proof-of-work starts well before that (feedback_limits.load_percent); this is the human's
# signal.
resource "aws_cloudwatch_log_metric_filter" "feedback_rejected" {
  name           = "bloggerbear-${var.environment_name}-feedback-rejected"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"rejected a feedback submission\""

  metric_transformation {
    name          = "FeedbackRejected"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "feedback_screening_budget_used_up" {
  name           = "bloggerbear-${var.environment_name}-feedback-screening-budget-used-up"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"rejected a feedback submission (screening_budget)\""

  metric_transformation {
    name          = "FeedbackScreeningBudgetUsedUp"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

# For the Lambda runs dashboard: what was kept, next to what was rejected. The same handler logs
# "accepted a feedback submission (comment kept|vote only)" -- a tag, never the comment.
resource "aws_cloudwatch_log_metric_filter" "feedback_accepted" {
  name           = "bloggerbear-${var.environment_name}-feedback-accepted"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"accepted a feedback submission\""

  metric_transformation {
    name          = "FeedbackAccepted"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "feedback_comment_kept" {
  name           = "bloggerbear-${var.environment_name}-feedback-comment-kept"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"accepted a feedback submission (comment kept)\""

  metric_transformation {
    name          = "FeedbackCommentKept"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_log_metric_filter" "feedback_model_dropped" {
  name           = "bloggerbear-${var.environment_name}-feedback-model-dropped"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"rejected a feedback submission (model_dropped)\""

  metric_transformation {
    name          = "FeedbackModelDropped"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "feedback_rejections_spike" {
  alarm_name          = "bloggerbear-${var.environment_name}-feedback-rejections-spike"
  alarm_description   = "bloggerbear-${var.environment_name}: at least ${var.feedback_rejections_alarm_threshold} feedback submissions were rejected in the last hour -- likely comment spam. Check the public API WAF log group for the source."
  namespace           = "BloggerBear/${var.environment_name}"
  metric_name         = aws_cloudwatch_log_metric_filter.feedback_rejected.metric_transformation[0].name
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = var.feedback_rejections_alarm_threshold
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "feedback_screening_budget_used_up" {
  alarm_name          = "bloggerbear-${var.environment_name}-feedback-screening-budget-used-up"
  alarm_description   = "bloggerbear-${var.environment_name}: today's comment model checks are used up, so every comment is being rejected until the day resets (Australia/Sydney). See admin_cli.py feedback-config get."
  namespace           = "BloggerBear/${var.environment_name}"
  metric_name         = aws_cloudwatch_log_metric_filter.feedback_screening_budget_used_up.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}

# Single dashboard: one widget per Lambda (Invocations/Errors/Duration/
# Throttles) plus one widget for the daily-cycle state machine + DLQ.
# The region each widget reads is var.aws_region, handed in by the calling root (its own
# var.aws_region, the deployment's home region) rather than read from a data "aws_region"
# source: a variable is a plain string at plan time, so the dashboard body is fully known in
# the plan and in this module's tests.
resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "bloggerbear-${var.environment_name}-pipeline"

  # Opens on the last 7 days at one point per hour. With no start or period it opened on CloudWatch's
  # 3-hour default, where a job that runs every few hours is a dot or two and the dashboard looked
  # empty. Duration (milliseconds, often thousands) has its own right-hand axis so it no longer
  # flattens the counts next to it.
  dashboard_body = jsonencode({
    start          = "-P7D"
    periodOverride = "inherit"
    widgets = concat(
      [
        {
          type   = "text"
          width  = 24
          height = 2
          properties = {
            markdown = "## Pipeline health (${var.environment_name})\nPer Lambda: runs, errors and throttles (left axis, per hour) and average duration in ms (right axis). Recent errors from every Lambda's log are at the bottom. Run counts over any span are on the **bloggerbear-${var.environment_name}-lambda-runs** dashboard."
          }
        }
      ],
      [
        for fn in var.lambda_function_names : {
          type   = "metric"
          width  = 12
          height = 6
          properties = {
            title  = fn
            region = var.aws_region
            view   = "timeSeries"
            period = 3600
            yAxis  = { left = { min = 0, label = "count" }, right = { min = 0, label = "ms" } }
            metrics = [
              ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum", label = "runs" }],
              ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum", label = "errors" }],
              ["AWS/Lambda", "Throttles", "FunctionName", fn, { stat = "Sum", label = "throttles" }],
              ["AWS/Lambda", "Duration", "FunctionName", fn, { stat = "Average", label = "avg duration", yAxis = "right" }],
            ]
          }
        }
      ],
      [
        {
          type   = "metric"
          width  = 12
          height = 6
          properties = {
            title  = "Daily cycle: Step Functions + dead-letter queue"
            region = var.aws_region
            view   = "timeSeries"
            period = 3600
            yAxis  = { left = { min = 0 } }
            metrics = [
              ["AWS/States", "ExecutionsFailed", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "failed" }],
              ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn, { stat = "Sum", label = "succeeded" }],
              ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_queue_name, { stat = "Maximum", label = "waiting in DLQ" }],
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
              [for fn in var.lambda_function_names : "SOURCE '/aws/lambda/${fn}'"],
              [
                "fields @timestamp, @log, @message",
                "filter @message like /unhandled exception|Traceback|ERROR|Task timed out/",
                "sort @timestamp desc",
                "limit 50",
              ],
            ))
          }
        }
      ]
    )
  })
}

# How often each Lambda ran, and what happened to feedback, over whatever span the dashboard's time
# picker is set to: the number tiles add up the whole selected range (setPeriodToTimeRange), and the
# chart below them shows the same runs per day. Opens on the last 24 hours.
resource "aws_cloudwatch_dashboard" "lambda_runs" {
  dashboard_name = "bloggerbear-${var.environment_name}-lambda-runs"

  dashboard_body = jsonencode({
    start          = "-PT24H"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "text"
        width  = 24
        height = 2
        properties = {
          markdown = "## Lambda runs (${var.environment_name})\nThe tiles count runs over the **whole** time range picked above (top right). Research ticks are every topic's checks together; the daily cycle is one run per topic per day."
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 5
        properties = {
          title                = "Runs in the selected range"
          region               = var.aws_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          metrics = [
            for fn in var.lambda_function_names :
            ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum", label = trimprefix(fn, "bloggerbear-${var.environment_name}-") }]
          ]
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 4
        properties = {
          title                = "Feedback in the selected range"
          region               = var.aws_region
          view                 = "singleValue"
          setPeriodToTimeRange = true
          metrics = [
            ["BloggerBear/${var.environment_name}", "FeedbackAccepted", { stat = "Sum", label = "accepted" }],
            ["BloggerBear/${var.environment_name}", "FeedbackCommentKept", { stat = "Sum", label = "comments kept" }],
            ["BloggerBear/${var.environment_name}", "FeedbackRejected", { stat = "Sum", label = "rejected" }],
            ["BloggerBear/${var.environment_name}", "FeedbackModelDropped", { stat = "Sum", label = "dropped by the screening model" }],
            ["BloggerBear/${var.environment_name}", "FeedbackScreeningBudgetUsedUp", { stat = "Sum", label = "rejected: model checks used up" }],
          ]
        }
      },
      {
        type   = "metric"
        width  = 24
        height = 7
        properties = {
          title   = "Runs per day"
          region  = var.aws_region
          view    = "bar"
          stacked = true
          period  = 86400
          metrics = [
            for fn in var.lambda_function_names :
            ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum", label = trimprefix(fn, "bloggerbear-${var.environment_name}-") }]
          ]
        }
      },
    ]
  })
}

# Security events (lambdas/common/security_events.py): an incident that is, or becomes, high
# severity logs one "SECURITY_ALERT ..." line, once per incident, from whichever Lambda recorded
# it (the security-events Lambda for WAF blocks, the public API for screened comments). One metric
# filter per log group feeds one metric; the alarm fires on the first. Lower severities are only
# recorded in the SecurityEvents table, never emailed.
resource "aws_cloudwatch_log_metric_filter" "security_high_severity" {
  for_each       = toset(var.security_alert_log_groups)
  name           = "bloggerbear-${var.environment_name}-security-high-severity"
  log_group_name = each.value
  pattern        = "\"SECURITY_ALERT\""

  metric_transformation {
    name          = "SecurityHighSeverityIncidents"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "security_high_severity" {
  count               = length(var.security_alert_log_groups) > 0 ? 1 : 0
  alarm_name          = "bloggerbear-${var.environment_name}-security-high-severity"
  alarm_description   = "bloggerbear-${var.environment_name}: a high-severity security incident was recorded (an exploit attempt such as SQL injection, file inclusion, SSRF or RCE, or a sustained attack). Open incidents, with suggested next steps, are in the SecurityEvents table (index by_status_last_seen, status = open)."
  namespace           = "BloggerBear/${var.environment_name}"
  metric_name         = "SecurityHighSeverityIncidents"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.security_high_severity]
}
