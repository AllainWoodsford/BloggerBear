# -----------------------------------------------------------------------
# Continuous evals (docs/enhancements/model-ranch-prompt-assets-evals.md, section 5.3; PR12): where
# the pipeline's scored events go, the table they are added up in, and the jobs that do the adding.
#
#   pipeline Lambdas -- common/evals.py emit() --> the evals log group (one JSON line an event)
#        | hourly: eval-jobs' rollup reads it with Logs Insights (PR13)
#        v
#   the evals table (day totals per model, prompt version and model x prompt cell)
#
#   eval-jobs' judge selector (hourly) --> eval-work --(one job at a time, two at once)--> eval-jobs
#
# Always instantiated; var.enabled (the root's evals_enabled) decides what runs.
#
# Unconditional: the table, the log group and the rights on the shared lambda_exec role. The table
# and the log group are kept whatever the switch says, so turning evals off loses no history and
# every Lambda is always told real names (EVALS_TABLE, EVALS_LOG_GROUP). The rights hold either
# way, so switching never changes the role.
#
# Only while enabled: the eval-work queue, the eval-jobs function with its log group, its two
# schedules and its queue mapping, the metric filters on the evals log group, and the alarms. With
# evals off nothing reads or writes the table, emit() writes nothing, and nothing is invoked.
#
# Rights. Every pipeline Lambda runs as lambda_exec, so the grants are on that role, in one inline
# policy, given here as var.lambda_exec_role_name: writing events and querying the log group, the
# table (read, write, and what a backup snapshot and restore need: Scan, DescribeTable,
# BatchWriteItem, common/backup.py), sending to and consuming eval-work, publishing to the alerts
# topic (the evals digest; the grant E0 put in the roots, moved here), invoking eval-jobs (the admin
# API's rollup now) and reading eval-work's age metric (evals status). eval-work's and eval-jobs'
# grants are on the ARNs their names make, so they hold whether or not the two exist. Every Lambda
# that runs as lambda_exec, the admin API included (offline runs, PR18b), then holds these rights;
# accepted.
#
# eval-jobs' errors and throttles alarms are module.observability's: the roots add function_names
# below to local.pipeline_lambda_function_names, as for every pipeline Lambda. The alarms here are
# the rollup's heartbeat and the queue's oldest job.
#
# The log group's name is outside the deploy role's /aws/lambda/<prefix>-* grant; infra/bootstrap's
# evals statement (PR2) covers it, and has to be applied by hand before this module's first apply.
#
# eval-jobs (lambdas/eval_jobs_handler.py) runs the rollup (common/evals_rollup.py), which prints the
# rollup_done line the heartbeat counts, and the judge (common/eval_judge.py): the hourly selector
# sends judge jobs to eval-work, and the queue's mapping hands each one back to eval-jobs. The judge
# reads Findings, articles and their content-bucket snapshots and bodies through lambda_exec's own
# grants on the app tables and the content bucket, so it needs no right of its own here.
# -----------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  eval_jobs_name = "${var.unique_name_prefix}-${var.environment_name}-eval-jobs"
  # eval-work's and eval-jobs' ARNs as their names make them, for grants that hold whether or not
  # the queue and the function exist.
  eval_work_arn    = "arn:aws:sqs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${var.unique_name_prefix}-${var.environment_name}-eval-work"
  eval_jobs_arn    = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${local.eval_jobs_name}"
  metric_namespace = "BloggerBear/${var.environment_name}"
}

# -----------------------------------------------------------------------
# Unconditional: the table and the log group
# -----------------------------------------------------------------------

# Day totals per entity (model#<id>, prompt#<prompt_id>#<version>, cell#<model>#<prompt_id>#<version>),
# operator adjustments, offline runs, judge and human pairs, and the rollup's watermark (section 4.4).
# No TTL: totals are kept for good. Protected like the app tables in production (var.protect_data):
# a revert of this module leaves the table, and the history in it.
resource "aws_dynamodb_table" "evals" {
  name                        = "${var.unique_name_prefix}-${var.environment_name}-evals"
  billing_mode                = "PAY_PER_REQUEST"
  deletion_protection_enabled = var.protect_data

  point_in_time_recovery {
    enabled = var.protect_data
  }

  hash_key  = "entity"
  range_key = "sk"

  attribute {
    name = "entity"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }
}

# One JSON line per scored event (common/evals.py), one stream per container a day. 120 days: the
# rollup only reads back to its watermark, and the day totals outlive the lines.
resource "aws_cloudwatch_log_group" "evals" {
  name              = "${var.unique_name_prefix}-${var.environment_name}-evals"
  retention_in_days = 120
}

# -----------------------------------------------------------------------
# Unconditional: the rights on the shared lambda_exec role
# -----------------------------------------------------------------------

data "aws_iam_policy_document" "evals" {
  # checkov:skip=CKV_AWS_356:GetQueryResults and StopQuery take no resource (they act on a query id), nor does GetMetricStatistics; StartQuery is on the evals group only

  # emit(): a stream of its own per container a day, and the events.
  statement {
    sid       = "EvalsLogWrite"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.evals.arn}:*"]
  }

  # The rollup's Logs Insights queries (common/logs_insights.py). Both ARN forms: IAM matches
  # StartQuery against the group with or without the trailing ":*", depending on how the call names it.
  statement {
    sid       = "EvalsLogRead"
    effect    = "Allow"
    actions   = ["logs:StartQuery"]
    resources = [aws_cloudwatch_log_group.evals.arn, "${aws_cloudwatch_log_group.evals.arn}:*"]
  }

  statement {
    sid       = "EvalsLogQueries"
    effect    = "Allow"
    actions   = ["logs:GetQueryResults", "logs:StopQuery"]
    resources = ["*"]
  }

  # The rollup's day rows and windows, the runs, the pairs; and a backup's snapshot (Scan,
  # DescribeTable for its progress line) and restore (BatchWriteItem), which run in the admin API.
  # The root's own backup grant is over the app tables only, and this table is not one of them.
  statement {
    sid    = "EvalsTable"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchWriteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [aws_dynamodb_table.evals.arn, "${aws_dynamodb_table.evals.arn}/index/*"]
  }

  # Sending judge and scoring jobs (the judge selector; the admin API's offline runs), and what
  # Lambda's poller needs for eval-jobs' mapping on the queue, which runs as this role too.
  statement {
    sid    = "EvalWork"
    effect = "Allow"
    actions = [
      "sqs:SendMessage",
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [local.eval_work_arn]
  }

  # The evals digest (PR20) goes to the alerts topic, the one the alarms email through. The only
  # SNS right lambda_exec holds.
  statement {
    sid       = "PublishAlerts"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
  }

  # The admin API's POST /evals/rollup (`admin_cli evals rollup`) starts a rollup in eval-jobs, and
  # POST /evals/revoke one of the days that hold the revoked events, by the ARN its name makes, so
  # the grant holds whether or not the function exists (neither starts one while evals are off).
  statement {
    sid       = "InvokeEvalJobs"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [local.eval_jobs_arn]
  }

  # GET /evals/status shows how long each queue's oldest job has waited (eval-work's, and the queue
  # platform's work.fifo): SQS's ApproximateAgeOfOldestMessage metric, which GetQueueAttributes does
  # not give. Reading a metric takes no resource; it reads numbers only.
  statement {
    sid       = "EvalWorkAge"
    effect    = "Allow"
    actions   = ["cloudwatch:GetMetricStatistics"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "evals" {
  name   = "${var.unique_name_prefix}-${var.environment_name}-lambda-evals"
  role   = var.lambda_exec_role_name
  policy = data.aws_iam_policy_document.evals.json
}

# -----------------------------------------------------------------------
# Only while enabled: the queue
# -----------------------------------------------------------------------

# Judge and scoring jobs (section 4.7), one per item. 5400 s of visibility is six times eval-jobs'
# 900 s timeout, AWS's advice for a queue a Lambda consumes. Three receives without finishing and a
# job goes to the pipeline's dead-letter queue, where dlq_handler records it in failed-executions.
# AVD-AWS-0096 ("queue is not encrypted") ignored deliberately: AWS-managed encryption, as for the
# pipeline DLQ (infra/environments/*/main.tf, aws_sqs_queue.pipeline_dlq).
# trivy:ignore:AVD-AWS-0096
resource "aws_sqs_queue" "eval_work" {
  count                      = var.enabled ? 1 : 0
  name                       = "${var.unique_name_prefix}-${var.environment_name}-eval-work"
  visibility_timeout_seconds = 5400

  redrive_policy = jsonencode({
    deadLetterTargetArn = var.pipeline_dlq_arn
    maxReceiveCount     = 3
  })
}

# -----------------------------------------------------------------------
# Only while enabled: eval-jobs
# -----------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "eval_jobs" {
  count             = var.enabled ? 1 : 0
  name              = "/aws/lambda/${local.eval_jobs_name}"
  retention_in_days = 90
}

# The shared package and environment every pipeline Lambda has, told this module's own names on
# top. Its log group and its rights come first: its schedules invoke it within the hour.
resource "aws_lambda_function" "eval_jobs" {
  count         = var.enabled ? 1 : 0
  function_name = local.eval_jobs_name
  depends_on    = [aws_cloudwatch_log_group.eval_jobs, aws_iam_role_policy.evals] # its log group and rights first
  role          = var.lambda_exec_role_arn
  handler       = "eval_jobs_handler.handler"
  runtime       = "python3.11"
  timeout       = 900
  memory_size   = 256

  filename         = var.lambda_package_path
  source_code_hash = var.lambda_package_hash

  environment {
    variables = merge(var.lambda_environment, {
      EVALS_TABLE         = aws_dynamodb_table.evals.name
      EVALS_LOG_GROUP     = aws_cloudwatch_log_group.evals.name
      EVAL_WORK_QUEUE_URL = aws_sqs_queue.eval_work[0].url
    })
  }
}

# Named to end in -scheduler-invoke: the deploy role may manage only roles that fit its patterns
# (infra/bootstrap/main.tf, LambdaExecRole), and this is the one for a schedule's role.
resource "aws_iam_role" "schedule" {
  count = var.enabled ? 1 : 0
  name  = "${var.unique_name_prefix}-${var.environment_name}-evals-scheduler-invoke"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "schedule" {
  count = var.enabled ? 1 : 0
  name  = "${var.unique_name_prefix}-${var.environment_name}-evals-invoke"
  role  = aws_iam_role.schedule[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.eval_jobs[0].arn
    }]
  })
}

# The rollup, at five past every hour (Sydney time, as the windows' days are; Sydney's offsets are
# whole hours, so it is five past in UTC too). It reads from its own watermark, so a run that
# fails or is skipped costs nothing but lateness.
resource "aws_scheduler_schedule" "rollup" {
  count                        = var.enabled ? 1 : 0
  name                         = "${var.unique_name_prefix}-${var.environment_name}-eval-rollup"
  group_name                   = "default"
  schedule_expression          = "cron(5 * * * ? *)"
  schedule_expression_timezone = "Australia/Sydney"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.eval_jobs[0].arn
    role_arn = aws_iam_role.schedule[0].arn
    input    = jsonencode({ job = "rollup", trigger = "schedule" })
  }
}

# The judge's selector, hourly. How often the judge really runs is judge_interval_hours on the
# evals settings row, read at run time: the job compares it with its last run, so changing it needs
# no deploy.
resource "aws_scheduler_schedule" "judge" {
  count               = var.enabled ? 1 : 0
  name                = "${var.unique_name_prefix}-${var.environment_name}-eval-judge"
  group_name          = "default"
  schedule_expression = "rate(1 hour)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.eval_jobs[0].arn
    role_arn = aws_iam_role.schedule[0].arn
    input    = jsonencode({ job = "judge_select", trigger = "schedule" })
  }
}

# One job per invocation, two at most at once (the smallest maximum_concurrency SQS accepts), so
# offline runs never crowd the pipeline's model calls. The rights come first: Lambda checks them
# when it makes the mapping.
resource "aws_lambda_event_source_mapping" "eval_work" {
  count            = var.enabled ? 1 : 0
  event_source_arn = aws_sqs_queue.eval_work[0].arn
  function_name    = aws_lambda_function.eval_jobs[0].arn
  batch_size       = 1
  depends_on       = [aws_iam_role_policy.evals]

  scaling_config {
    maximum_concurrency = 2
  }
}

# -----------------------------------------------------------------------
# Only while enabled: metrics and alarms (each alarm notifies var.alerts_topic_arn)
# -----------------------------------------------------------------------

# The sum of every event's rating_delta, by performance (good, bad, neutral), and the count of
# events: the raw flow of scores, for the dashboard (PR11b), before any rollup.
resource "aws_cloudwatch_log_metric_filter" "eval_rating_delta" {
  count          = var.enabled ? 1 : 0
  name           = "${var.unique_name_prefix}-${var.environment_name}-eval-rating-delta"
  log_group_name = aws_cloudwatch_log_group.evals.name
  pattern        = "{ $.schema = \"evals/1\" }"

  metric_transformation {
    name       = "EvalRatingDelta"
    namespace  = local.metric_namespace
    value      = "$.rating_delta"
    dimensions = { Performance = "$.performance" }
  }
}

resource "aws_cloudwatch_log_metric_filter" "eval_events" {
  count          = var.enabled ? 1 : 0
  name           = "${var.unique_name_prefix}-${var.environment_name}-eval-events"
  log_group_name = aws_cloudwatch_log_group.evals.name
  pattern        = "{ $.schema = \"evals/1\" }"

  metric_transformation {
    name      = "EvalEvents"
    namespace = local.metric_namespace
    value     = "1"
  }
}

# The rollup's heartbeat: one per run that finished (its rollup_done evidence line, PR13). No
# default value, so a quiet log is missing data, and missing data breaches: a rollup that is not
# being invoked has no errors to alarm on, and this is what notices. Three quiet hours in a row.
resource "aws_cloudwatch_log_metric_filter" "rollups" {
  count          = var.enabled ? 1 : 0
  name           = "${local.eval_jobs_name}-rollups"
  log_group_name = aws_cloudwatch_log_group.eval_jobs[0].name
  pattern        = "{ $.event = \"rollup_done\" }"

  metric_transformation {
    name      = "EvalRollups"
    namespace = local.metric_namespace
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "rollup_heartbeat" {
  count               = var.enabled ? 1 : 0
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-eval-rollup-heartbeat"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: the evals rollup has not finished in 3 hours, so the evals table and the models' scores are going stale."
  namespace           = aws_cloudwatch_log_metric_filter.rollups[0].metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.rollups[0].metric_transformation[0].name
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}

# A judge or scoring job that has waited two hours: eval-jobs is failing, or too slow for the work.
resource "aws_cloudwatch_metric_alarm" "eval_work_age" {
  count               = var.enabled ? 1 : 0
  alarm_name          = "${var.unique_name_prefix}-${var.environment_name}-eval-work-age"
  alarm_description   = "${var.unique_name_prefix}-${var.environment_name}: a job on the eval-work queue has waited more than 2 hours."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.eval_work[0].name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 7200
  treat_missing_data  = "notBreaching"

  alarm_actions = [var.alerts_topic_arn]
  ok_actions    = [var.alerts_topic_arn]
}
