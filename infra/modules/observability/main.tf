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
# var.bedrock_model_id elsewhere in this
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
  name = "${var.unique_name_prefix}-${var.environment_name}-alerts"
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

  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-${each.value}-errors"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: ${each.value} had at least one invocation error in the last 5 minutes."
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

  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-${each.value}-throttles"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: ${each.value} was throttled in the last 5 minutes."
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
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-pipeline-dlq-messages"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: the daily-cycle dead-letter queue has at least one failed execution waiting on it."
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
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-daily-cycle-executions-failed"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: the daily-cycle Step Functions state machine had a failed execution."
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

# A schedule whose run was given up on. EventBridge Scheduler drops an invocation when the target
# refuses it outright (the role may not invoke the function) or when its retries run out, and
# nothing else notices: the function is never invoked, so it has no error and no log line.
# Production's weekly stats rollover was dropped this way on 2026-09-28 (the scheduler's role
# could not invoke it), and the gap was found a week later, as two weeks of numbers in one
# StatsHistory row. Scheduler reports this metric per schedule group, not per schedule, and every
# schedule this project makes (the fixed ones and each topic's) is in "default": the alarm says
# that one of them was dropped, and the Scheduler console's metrics say which. Where dev and
# production share an account they share the group too, so both environments' alarms fire.
resource "aws_cloudwatch_metric_alarm" "schedule_invocations_dropped" {
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-schedule-invocations-dropped"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: EventBridge Scheduler dropped at least one scheduled run in the default schedule group, so a job (research, the daily cycle, the weekly rollover or the cost poll) did not run."
  namespace           = "AWS/Scheduler"
  metric_name         = "InvocationDroppedCount"
  dimensions          = { ScheduleGroup = "default" }
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
  name           = "${var.unique_name_prefix}-${var.environment_name}-feedback-rejected"
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
  name           = "${var.unique_name_prefix}-${var.environment_name}-feedback-screening-budget-used-up"
  log_group_name = var.feedback_log_group_name
  pattern        = "\"rejected a feedback submission (screening_budget)\""

  metric_transformation {
    name          = "FeedbackScreeningBudgetUsedUp"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

# For pipeline health's feedback funnel (pipeline_health.tf): what was kept, next to what was
# rejected. The same handler logs "accepted a feedback submission (comment kept|vote only)" -- a
# tag, never the comment.
resource "aws_cloudwatch_log_metric_filter" "feedback_accepted" {
  name           = "${var.unique_name_prefix}-${var.environment_name}-feedback-accepted"
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
  name           = "${var.unique_name_prefix}-${var.environment_name}-feedback-comment-kept"
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
  name           = "${var.unique_name_prefix}-${var.environment_name}-feedback-model-dropped"
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
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-feedback-rejections-spike"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: at least ${var.feedback_rejections_alarm_threshold} feedback submissions were rejected in the last hour -- likely comment spam. Check the public API WAF log group for the source."
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
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-feedback-screening-budget-used-up"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: today's comment model checks are used up, so every comment is being rejected until the day resets (Australia/Sydney). See admin_cli.py feedback-config get."
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

# Security events (lambdas/common/security_events.py): an incident that is, or becomes, high
# severity logs one "SECURITY_ALERT ..." line, once per incident, from whichever Lambda recorded
# it (the security-events Lambda for WAF blocks, the public API for screened comments). One metric
# filter per log group feeds one metric; the alarm fires on the first. Lower severities are only
# recorded in the SecurityEvents table, never emailed.
resource "aws_cloudwatch_log_metric_filter" "security_high_severity" {
  for_each       = toset(var.security_alert_log_groups)
  name           = "${var.unique_name_prefix}-${var.environment_name}-security-high-severity"
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
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-security-high-severity"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: a high-severity security incident was recorded (an exploit attempt such as SQL injection, file inclusion, SSRF or RCE, or a sustained attack). Open incidents, with suggested next steps, are in the SecurityEvents table (index by_status_last_seen, status = open)."
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

# Prompts as assets (docs/enhancements/model-ranch-prompt-assets-evals.md): a Lambda that finds a
# prompt asset unreadable, or failing its checks, uses the copy in its code and prints one
# prompt_fallback evidence line (common/evidence.py: the line is one JSON object whose `event` key
# names the event, which is what the filter selects on). The pipeline carries on, so nothing else
# notices: one metric filter per pipeline log group feeds one metric, and the alarm fires on the
# first fallback in an hour and emails on ALARM and on OK, like the feedback alarms.
resource "aws_cloudwatch_log_metric_filter" "prompt_fallback" {
  for_each       = toset(var.prompt_fallback_log_groups)
  name           = "${var.unique_name_prefix}-${var.environment_name}-prompt-fallback"
  log_group_name = each.value
  pattern        = "{ $.event = \"prompt_fallback\" }"

  metric_transformation {
    name          = "PromptFallback"
    namespace     = "BloggerBear/${var.environment_name}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "prompt_fallback" {
  count               = length(var.prompt_fallback_log_groups) > 0 ? 1 : 0
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-prompt-fallback"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: a Lambda fell back from a prompt asset to the copy in its code in the last hour. Its log's prompt_fallback lines name the prompt, the version and why."
  namespace           = "BloggerBear/${var.environment_name}"
  metric_name         = "PromptFallback"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  depends_on = [aws_cloudwatch_log_metric_filter.prompt_fallback]
}
