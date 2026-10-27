# -----------------------------------------------------------------------
# The queue platform (docs/enhancements/model-ranch-prompt-assets-evals.md, section 5.1; phase Q1):
# one dispatcher a minute in place of two EventBridge schedules per topic, a FIFO work queue in
# front of the research tick, and one knob (var.worker_concurrency) that caps how many queue jobs
# run at once however many topics there are.
#
#   this module's schedule (rate(1 minute)) -> the dispatcher (lambdas/dispatcher_handler.py)
#        | research due: claim the topic, SendMessage to work.fifo (group = the topic id)
#        | daily due: claim the day, StartExecution on the daily-cycle state machine
#        v
#   work.fifo --(event source mapping, one message at a time, at most worker_concurrency at once)-->
#   the research tick --(three receives without finishing)--> work-dlq.fifo --> dlq_handler
#
# Shadow mode first. A topic stays on its two schedules until its dispatch_mode is "queue" (the
# per-topic cutover, Q2), and while it is the dispatcher sends and starts nothing for it: it only
# records on the topic's row what it would have done and counts where that differs from what the
# schedules did (lambdas/common/dispatch.py). Until then this module's queue stays empty.
#
# The research tick never raises (it returns an error result instead), so a job that reaches
# work-dlq.fifo is one that timed out or crashed the runtime, never one that failed cleanly.
#
# Rights. The dispatcher runs as the shared lambda_exec role, given here as var.lambda_exec_role_*,
# and this module adds two inline policies to that role: the dispatcher's (send to work.fifo; start,
# list and describe the daily cycle's executions, the last two for shadow mode's daily check), and
# the two event source mappings' (receive and delete on both queues). Every Lambda that runs as
# lambda_exec, the admin API included, then holds these rights; accepted (the admin API needs the
# first two for manual triggers from Q2 on).
#
# The dispatcher's errors and throttles alarms are module.observability's: the roots add
# function_names below to local.pipeline_lambda_function_names, as for every pipeline Lambda. The
# alarms here are the platform's own: the queue's oldest message, the dead-letter queue, and the
# dispatcher's heartbeat.
# -----------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

locals {
  name = "${var.unique_name_prefix}-${var.environment_name}-dispatcher"
  # The executions of the daily-cycle state machine: arn:aws:states:<region>:<account>:execution:
  # <state machine name>:<execution name>, which DescribeExecution is authorized against.
  executions_arn = "${replace(var.state_machine_arn, ":stateMachine:", ":execution:")}:*"
}

# -----------------------------------------------------------------------
# The queues
# -----------------------------------------------------------------------

# FIFO so one topic's jobs run one at a time and in order (MessageGroupId = the topic id), and a
# stale job behind a newer one is harmless (the research tick decides at the job's own
# due_checked_at). Content-based deduplication is off: the dispatcher sets MessageDeduplicationId to
# <topic_id>:research:<minute>, so the same minute is never queued twice while a manual job with the
# same body still is. 720 s of visibility is six times the research tick's 120 s timeout, AWS's
# advice for a queue a Lambda consumes. Three receives without finishing and a job goes to
# work-dlq.fifo (a FIFO queue's dead-letter queue must be FIFO too).
# AVD-AWS-0096 ("queue is not encrypted") ignored deliberately: AWS-managed encryption, as for the
# pipeline DLQ (infra/environments/*/main.tf, aws_sqs_queue.pipeline_dlq).
# trivy:ignore:AVD-AWS-0096
resource "aws_sqs_queue" "work" {
  name                        = "${var.unique_name_prefix}-${var.environment_name}-work.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = 720

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 3
  })
}

# dlq_handler takes each message off this queue as it arrives and records it in failed-executions
# (`failed-executions list`). 180 s of visibility is six times dlq_handler's 30 s timeout; 14 days of
# retention keeps a message the handler could not take for as long as SQS allows.
# trivy:ignore:AVD-AWS-0096
resource "aws_sqs_queue" "work_dlq" {
  name                        = "${var.unique_name_prefix}-${var.environment_name}-work-dlq.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = 180
  message_retention_seconds   = 1209600
}

# -----------------------------------------------------------------------
# Rights on the shared lambda_exec role
# -----------------------------------------------------------------------

data "aws_iam_policy_document" "dispatcher" {
  statement {
    sid       = "SendResearchJobs"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work.arn]
  }

  statement {
    sid       = "StartDailyCycles"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [var.state_machine_arn]
  }

  # Shadow mode's daily check (common/dispatch.py's _daily_ran): which executions started on a
  # day, and whose topic each was for. Without these every check is refused, and each day is
  # counted in the topic's shadow_unchecked instead of passing as a match.
  statement {
    sid       = "ListDailyCycles"
    effect    = "Allow"
    actions   = ["states:ListExecutions"]
    resources = [var.state_machine_arn]
  }

  statement {
    sid       = "DescribeDailyCycles"
    effect    = "Allow"
    actions   = ["states:DescribeExecution"]
    resources = [local.executions_arn]
  }
}

resource "aws_iam_role_policy" "dispatcher" {
  name   = "${var.unique_name_prefix}-${var.environment_name}-lambda-dispatch"
  role   = var.lambda_exec_role_name
  policy = data.aws_iam_policy_document.dispatcher.json
}

# What Lambda's poller needs for the two event source mappings below, the shape of the roots'
# lambda_consume_dlq policy.
data "aws_iam_policy_document" "consume" {
  statement {
    sid    = "ConsumeWorkQueues"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.work.arn, aws_sqs_queue.work_dlq.arn]
  }
}

resource "aws_iam_role_policy" "consume" {
  name   = "${var.unique_name_prefix}-${var.environment_name}-lambda-consume-work"
  role   = var.lambda_exec_role_name
  policy = data.aws_iam_policy_document.consume.json
}

# -----------------------------------------------------------------------
# The dispatcher
# -----------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "dispatcher" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = 90
}

# The shared package and environment every pipeline Lambda has, with this module's own queue and the
# state machine it was given. Its log group and its rights come first: the schedule invokes it the
# minute it exists.
resource "aws_lambda_function" "dispatcher" {
  function_name = local.name
  depends_on    = [aws_cloudwatch_log_group.dispatcher, aws_iam_role_policy.dispatcher] # its log group and rights first
  role          = var.lambda_exec_role_arn
  handler       = "dispatcher_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 256

  filename         = var.lambda_package_path
  source_code_hash = var.lambda_package_hash

  environment {
    variables = merge(var.lambda_environment, {
      WORK_QUEUE_URL    = aws_sqs_queue.work.url
      STATE_MACHINE_ARN = var.state_machine_arn
    })
  }
}

# EventBridge Scheduler hands the tick to Lambda's asynchronous queue, so the schedule's own
# retry_policy below never sees the function fail, and Lambda retries a failed async invoke twice by
# default, about one and three minutes later. The dispatcher raises on purpose (so its Errors alarm
# fires), and such a replay can run beside the next scheduled tick, both starting from the same
# last_tick, and count the same shadow turns twice (in Q2, spend daily_starts_per_tick twice). A
# failed tick needs no retry: the next one looks back to last_tick. An event held longer than a
# minute (60 s, the least Lambda accepts) is dropped: a newer tick has already covered its window.
resource "aws_lambda_function_event_invoke_config" "dispatcher" {
  function_name                = aws_lambda_function.dispatcher.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

# Named to end in -scheduler-invoke: the deploy role may manage only roles that fit its patterns
# (infra/bootstrap/main.tf, LambdaExecRole), and this is the one for a schedule's role.
resource "aws_iam_role" "dispatcher_schedule" {
  name = "${local.name}-scheduler-invoke"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "dispatcher_schedule" {
  name = "${local.name}-invoke"
  role = aws_iam_role.dispatcher_schedule.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.dispatcher.arn
    }]
  })
}

# Every minute, never retried: neither Scheduler's delivery (retry_policy, below) nor Lambda's async
# invoke (aws_lambda_function_event_invoke_config.dispatcher, above) runs a failed tick again. A
# minute the dispatcher missed costs nothing, since the next tick looks back to the last one that
# finished (last_tick on the pipeline config row). Made after the invoke config, so the first tick
# already runs with Lambda's retries off.
resource "aws_scheduler_schedule" "dispatcher" {
  name                = local.name
  schedule_expression = "rate(1 minute)"
  depends_on          = [aws_lambda_function_event_invoke_config.dispatcher]

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.dispatcher.arn
    role_arn = aws_iam_role.dispatcher_schedule.arn
    input    = jsonencode({ trigger = "schedule" })

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}

# -----------------------------------------------------------------------
# The consumers
# -----------------------------------------------------------------------

# One research job per invocation, at most var.worker_concurrency at once (2 at least: the smallest
# maximum_concurrency SQS accepts). The rights come first: Lambda checks them when it makes the
# mapping.
resource "aws_lambda_event_source_mapping" "research_tick" {
  event_source_arn = aws_sqs_queue.work.arn
  function_name    = var.research_tick_arn
  batch_size       = 1
  depends_on       = [aws_iam_role_policy.consume]

  scaling_config {
    maximum_concurrency = var.worker_concurrency
  }
}

resource "aws_lambda_event_source_mapping" "work_dlq" {
  event_source_arn = aws_sqs_queue.work_dlq.arn
  function_name    = var.dlq_handler_arn
  batch_size       = 10
  depends_on       = [aws_iam_role_policy.consume]
}

# -----------------------------------------------------------------------
# Alarms (each notifies var.alerts_topic_arn)
# -----------------------------------------------------------------------

# A job that has waited a quarter of an hour: the workers are stuck or too few for the topics.
resource "aws_cloudwatch_metric_alarm" "work_queue_age" {
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-work-queue-age"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: a job on the work queue has waited more than 15 minutes."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.work.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 900
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}

# Two depths of the dead-letter queue. dlq_handler takes a message within seconds, so a message still
# visible here means the handler could not take it (the pipeline DLQ's alarm, the same way); and
# since a message it did take is gone before the visible depth is sampled, a second alarm counts
# the messages received from the queue at all: any is a job that failed three times.
resource "aws_cloudwatch_metric_alarm" "work_dlq_messages" {
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-work-dlq-messages"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: the work queue's dead-letter queue has a job waiting that dlq-handler has not recorded."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.work_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}

resource "aws_cloudwatch_metric_alarm" "work_dlq_received" {
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-work-dlq-received"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: a queue job failed three times and reached the work queue's dead-letter queue (see failed-executions list)."
  namespace           = "AWS/SQS"
  metric_name         = "NumberOfMessagesReceived"
  dimensions          = { QueueName = aws_sqs_queue.work_dlq.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}

# The dispatcher's heartbeat: one per tick that finished (its dispatcher_tick evidence line with
# outcome ok, lambdas/common/evidence.py). No default value, so a quiet log means no data, and no
# data is breaching: a dispatcher that is not being invoked (its schedule or its role gone) has no
# errors to alarm on, and this is what notices. Three five-minute periods: a quarter of an hour.
resource "aws_cloudwatch_log_metric_filter" "dispatcher_ticks" {
  name           = "${local.name}-ticks"
  log_group_name = aws_cloudwatch_log_group.dispatcher.name
  pattern        = "{ $.event = \"dispatcher_tick\" && $.outcome = \"ok\" }"

  metric_transformation {
    name      = "DispatcherTicks"
    namespace = "BloggerBear/${var.environment_name}"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "dispatcher_heartbeat" {
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-dispatcher-heartbeat"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: the dispatcher has not finished a tick in 15 minutes, so no research job or daily cycle is being dispatched."
  namespace           = aws_cloudwatch_log_metric_filter.dispatcher_ticks.metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.dispatcher_ticks.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}
