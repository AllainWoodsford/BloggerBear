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

# Single dashboard: one widget per Lambda (Invocations/Errors/Duration/
# Throttles) plus one widget for the daily-cycle state machine + DLQ.
# Region is hardcoded to ap-southeast-2 rather than pulled from a
# data "aws_region" source -- every other resource in this codebase that
# needs the region as a literal (e.g. the BedrockInvoke IAM statement in
# dev/production main.tf) does the same, since this project's region is a
# fixed, non-negotiable choice per docs/project-plan.md §3, not a
# per-environment variable.
resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "bloggerbear-${var.environment_name}-pipeline"

  dashboard_body = jsonencode({
    widgets = concat(
      [
        for fn in var.lambda_function_names : {
          type   = "metric"
          width  = 12
          height = 6
          properties = {
            title  = fn
            region = "ap-southeast-2"
            view   = "timeSeries"
            metrics = [
              ["AWS/Lambda", "Invocations", "FunctionName", fn, { stat = "Sum" }],
              ["AWS/Lambda", "Errors", "FunctionName", fn, { stat = "Sum" }],
              ["AWS/Lambda", "Duration", "FunctionName", fn, { stat = "Average" }],
              ["AWS/Lambda", "Throttles", "FunctionName", fn, { stat = "Sum" }],
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
            region = "ap-southeast-2"
            view   = "timeSeries"
            metrics = [
              ["AWS/States", "ExecutionsFailed", "StateMachineArn", var.state_machine_arn, { stat = "Sum" }],
              ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", var.state_machine_arn, { stat = "Sum" }],
              ["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", var.dlq_queue_name, { stat = "Maximum" }],
            ]
          }
        }
      ]
    )
  })
}
