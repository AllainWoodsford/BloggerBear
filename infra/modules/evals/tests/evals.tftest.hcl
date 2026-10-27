# The evals module (main.tf), planned with a mocked AWS provider like the other modules' tests.
# What is held: switched off, it makes the table, the log group and lambda_exec's rights, and
# nothing else, and still hands out real names; the rights are exactly section 5.3's (with what a
# backup needs on the table, what the queue mapping needs on eval-work, and what the admin API's
# rollup and status routes need) on exactly their resources, the same either way; switched on, it also makes the queue redriving to the pipeline's
# dead-letter queue, eval-jobs with its own log group, its two schedules, its mapping, the metric
# filters, the rollup's heartbeat and the queue's age alarm; production protects the table.

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

override_resource {
  target          = aws_dynamodb_table.evals
  override_during = plan
  values = {
    arn = "arn:aws:dynamodb:eu-west-1:111111111111:table/acme-dev-evals"
  }
}

override_resource {
  target          = aws_cloudwatch_log_group.evals
  override_during = plan
  values = {
    arn = "arn:aws:logs:eu-west-1:111111111111:log-group:acme-dev-evals"
  }
}

override_resource {
  target          = aws_sqs_queue.eval_work
  override_during = plan
  values = {
    arn = "arn:aws:sqs:eu-west-1:111111111111:acme-dev-eval-work"
    url = "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-eval-work"
  }
}

override_resource {
  target          = aws_lambda_function.eval_jobs
  override_during = plan
  values = {
    arn = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-eval-jobs"
  }
}

override_resource {
  target          = aws_iam_role.schedule
  override_during = plan
  values = {
    arn = "arn:aws:iam::111111111111:role/acme-dev-evals-scheduler-invoke"
  }
}

variables {
  # Another deployment's prefix: nothing here may depend on the original one.
  unique_name_prefix    = "acme"
  environment_name      = "dev"
  aws_region            = "eu-west-1"
  enabled               = false
  lambda_exec_role_arn  = "arn:aws:iam::111111111111:role/acme-dev-lambda-exec"
  lambda_exec_role_name = "acme-dev-lambda-exec"
  lambda_package_path   = "lambda-build/lambdas.zip"
  lambda_package_hash   = "c2hhMjU2LW9mLXRoZS1wYWNrYWdl"
  lambda_environment = {
    TOPICS_TABLE        = "acme-dev-topics"
    NAME_PREFIX         = "acme"
    ENVIRONMENT_NAME    = "dev"
    EVALS_ENABLED       = "true"
    EVALS_TABLE         = ""
    EVALS_LOG_GROUP     = ""
    EVAL_WORK_QUEUE_URL = ""
  }
  pipeline_dlq_arn = "arn:aws:sqs:eu-west-1:111111111111:acme-dev-pipeline-dlq"
  alerts_topic_arn = "arn:aws:sns:eu-west-1:111111111111:acme-dev-alerts"
}

run "switched_off_it_makes_the_table_the_log_group_and_the_rights_and_nothing_else" {
  command = plan

  assert {
    condition     = aws_dynamodb_table.evals.name == "acme-dev-evals" && aws_dynamodb_table.evals.hash_key == "entity" && aws_dynamodb_table.evals.range_key == "sk"
    error_message = "the table is <prefix>-<env>-evals, keyed entity and sk (section 4.4)"
  }

  assert {
    condition     = aws_dynamodb_table.evals.billing_mode == "PAY_PER_REQUEST" && length(aws_dynamodb_table.evals.ttl) == 0
    error_message = "on demand, and no TTL: totals are kept for good"
  }

  assert {
    condition     = aws_dynamodb_table.evals.deletion_protection_enabled == false && aws_dynamodb_table.evals.point_in_time_recovery[0].enabled == false
    error_message = "dev's table is unprotected unless protect_data says otherwise"
  }

  assert {
    condition     = aws_cloudwatch_log_group.evals.name == "acme-dev-evals" && aws_cloudwatch_log_group.evals.retention_in_days == 120
    error_message = "the log group is <prefix>-<env>-evals, kept 120 days"
  }

  assert {
    condition     = aws_iam_role_policy.evals.role == "acme-dev-lambda-exec" && aws_iam_role_policy.evals.name == "acme-dev-lambda-evals"
    error_message = "the rights are on the shared lambda_exec role, switch or no switch"
  }

  assert {
    condition = (
      length(aws_sqs_queue.eval_work) == 0 && length(aws_lambda_function.eval_jobs) == 0 &&
      length(aws_cloudwatch_log_group.eval_jobs) == 0 && length(aws_iam_role.schedule) == 0 &&
      length(aws_iam_role_policy.schedule) == 0 && length(aws_scheduler_schedule.rollup) == 0 &&
      length(aws_scheduler_schedule.judge) == 0 && length(aws_lambda_event_source_mapping.eval_work) == 0 &&
      length(aws_cloudwatch_log_metric_filter.eval_rating_delta) == 0 &&
      length(aws_cloudwatch_log_metric_filter.eval_events) == 0 &&
      length(aws_cloudwatch_log_metric_filter.rollups) == 0 &&
      length(aws_cloudwatch_metric_alarm.rollup_heartbeat) == 0 &&
      length(aws_cloudwatch_metric_alarm.eval_work_age) == 0
    )
    error_message = "nothing else: no queue, no function, no schedule, no mapping, no metric, no alarm"
  }

  assert {
    condition     = output.table_name == "acme-dev-evals" && output.log_group_name == "acme-dev-evals"
    error_message = "every Lambda is still told the real table and log group"
  }

  assert {
    condition     = output.work_queue_url == "" && output.function_names == [] && output.alarm_names == []
    error_message = "and an empty queue URL, no function for observability, no alarm"
  }
}

run "the_rights_are_exactly_section_5_3s_on_exactly_their_resources" {
  command = plan

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : statement.sid] == [
      "EvalsLogWrite", "EvalsLogRead", "EvalsLogQueries", "EvalsTable", "EvalWork", "PublishAlerts",
      "InvokeEvalJobs", "EvalWorkAge",
    ]
    error_message = "eight statements and no more"
  }

  assert {
    condition     = alltrue([for statement in data.aws_iam_policy_document.evals.statement : statement.effect == "Allow"])
    error_message = "every statement allows; none denies or conditions"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalsLogWrite"] == [{
      actions   = toset(["logs:CreateLogStream", "logs:PutLogEvents"])
      resources = toset(["arn:aws:logs:eu-west-1:111111111111:log-group:acme-dev-evals:*"])
    }]
    error_message = "emit() makes its stream and writes events in the evals group's streams only"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalsLogRead"] == [{
      actions = toset(["logs:StartQuery"])
      resources = toset([
        "arn:aws:logs:eu-west-1:111111111111:log-group:acme-dev-evals",
        "arn:aws:logs:eu-west-1:111111111111:log-group:acme-dev-evals:*",
      ])
    }]
    error_message = "the rollup starts queries on the evals group and no other"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalsLogQueries"] == [{
      actions   = toset(["logs:GetQueryResults", "logs:StopQuery"])
      resources = toset(["*"])
    }]
    error_message = "reading and stopping a query take no resource; no other Logs action is on *"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalsTable"] == [{
      actions = toset([
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem",
        "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem", "dynamodb:DescribeTable",
      ])
      resources = toset([
        "arn:aws:dynamodb:eu-west-1:111111111111:table/acme-dev-evals",
        "arn:aws:dynamodb:eu-west-1:111111111111:table/acme-dev-evals/index/*",
      ])
    }]
    error_message = "read and write on the evals table and its indexes, with a backup's Scan, DescribeTable and BatchWriteItem"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalWork"] == [{
      actions   = toset(["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"])
      resources = toset(["arn:aws:sqs:eu-west-1:111111111111:acme-dev-eval-work"])
    }]
    error_message = "send to and consume eval-work, by the ARN its name makes, though it does not exist yet"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "PublishAlerts"] == [{
      actions   = toset(["sns:Publish"])
      resources = toset(["arn:aws:sns:eu-west-1:111111111111:acme-dev-alerts"])
    }]
    error_message = "publish to the alerts topic it was given, and to no other"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "InvokeEvalJobs"] == [{
      actions   = toset(["lambda:InvokeFunction"])
      resources = toset(["arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-eval-jobs"])
    }]
    error_message = "the admin API starts a rollup in eval-jobs and no other function, by the ARN its name makes, though it does not exist yet"
  }

  assert {
    condition = [for statement in data.aws_iam_policy_document.evals.statement : {
      actions = toset(statement.actions), resources = toset(statement.resources)
      } if statement.sid == "EvalWorkAge"] == [{
      actions   = toset(["cloudwatch:GetMetricStatistics"])
      resources = toset(["*"])
    }]
    error_message = "evals status reads eval-work's age metric, which takes no resource; nothing more"
  }
}

run "switched_on_it_makes_everything" {
  command = plan

  variables {
    enabled = true
  }

  assert {
    condition = (
      length(aws_sqs_queue.eval_work) == 1 && length(aws_lambda_function.eval_jobs) == 1 &&
      length(aws_cloudwatch_log_group.eval_jobs) == 1 && length(aws_iam_role.schedule) == 1 &&
      length(aws_iam_role_policy.schedule) == 1 && length(aws_scheduler_schedule.rollup) == 1 &&
      length(aws_scheduler_schedule.judge) == 1 && length(aws_lambda_event_source_mapping.eval_work) == 1 &&
      length(aws_cloudwatch_log_metric_filter.eval_rating_delta) == 1 &&
      length(aws_cloudwatch_log_metric_filter.eval_events) == 1 &&
      length(aws_cloudwatch_log_metric_filter.rollups) == 1 &&
      length(aws_cloudwatch_metric_alarm.rollup_heartbeat) == 1 &&
      length(aws_cloudwatch_metric_alarm.eval_work_age) == 1
    )
    error_message = "one of everything"
  }

  assert {
    condition     = aws_dynamodb_table.evals.name == "acme-dev-evals" && aws_cloudwatch_log_group.evals.name == "acme-dev-evals" && aws_iam_role_policy.evals.role == "acme-dev-lambda-exec"
    error_message = "the table, the log group and the rights are the same ones as with the switch off"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.evals.statement : statement.resources if statement.sid == "EvalWork"
    ])) == toset([aws_sqs_queue.eval_work[0].arn])
    error_message = "the ARN the grant was built from is the queue's own"
  }

  assert {
    condition     = aws_sqs_queue.eval_work[0].name == "acme-dev-eval-work" && aws_sqs_queue.eval_work[0].visibility_timeout_seconds == 5400 && aws_sqs_queue.eval_work[0].fifo_queue != true
    error_message = "a standard queue, <prefix>-<env>-eval-work, visible again after six times eval-jobs' timeout"
  }

  assert {
    condition     = jsondecode(aws_sqs_queue.eval_work[0].redrive_policy) == { deadLetterTargetArn = var.pipeline_dlq_arn, maxReceiveCount = 3 }
    error_message = "three receives, then the pipeline's dead-letter queue"
  }

  assert {
    condition     = aws_lambda_function.eval_jobs[0].function_name == "acme-dev-eval-jobs" && aws_lambda_function.eval_jobs[0].role == var.lambda_exec_role_arn
    error_message = "<prefix>-<env>-eval-jobs, as the shared role"
  }

  assert {
    condition     = aws_lambda_function.eval_jobs[0].handler == "eval_jobs_handler.handler" && aws_lambda_function.eval_jobs[0].timeout == 900 && aws_lambda_function.eval_jobs[0].runtime == "python3.11"
    error_message = "the eval jobs handler, with fifteen minutes to run"
  }

  assert {
    condition     = aws_cloudwatch_log_group.eval_jobs[0].name == "/aws/lambda/acme-dev-eval-jobs" && aws_cloudwatch_log_group.eval_jobs[0].retention_in_days == 90
    error_message = "its own log group, kept 90 days like every pipeline Lambda's"
  }

  assert {
    condition = (
      aws_lambda_function.eval_jobs[0].environment[0].variables["EVALS_TABLE"] == "acme-dev-evals" &&
      aws_lambda_function.eval_jobs[0].environment[0].variables["EVALS_LOG_GROUP"] == "acme-dev-evals" &&
      aws_lambda_function.eval_jobs[0].environment[0].variables["EVAL_WORK_QUEUE_URL"] == "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-eval-work" &&
      aws_lambda_function.eval_jobs[0].environment[0].variables["TOPICS_TABLE"] == "acme-dev-topics"
    )
    error_message = "told its own table, log group and queue, beside the shared environment"
  }

  assert {
    condition = (
      aws_scheduler_schedule.rollup[0].name == "acme-dev-eval-rollup" &&
      aws_scheduler_schedule.rollup[0].schedule_expression == "cron(5 * * * ? *)" &&
      aws_scheduler_schedule.rollup[0].schedule_expression_timezone == "Australia/Sydney" &&
      jsondecode(aws_scheduler_schedule.rollup[0].target[0].input) == { job = "rollup", trigger = "schedule" }
    )
    error_message = "the rollup at five past every hour, Sydney time"
  }

  assert {
    condition = (
      aws_scheduler_schedule.judge[0].name == "acme-dev-eval-judge" &&
      aws_scheduler_schedule.judge[0].schedule_expression == "rate(1 hour)" &&
      jsondecode(aws_scheduler_schedule.judge[0].target[0].input) == { job = "judge_select", trigger = "schedule" }
    )
    error_message = "the judge's selector hourly; its own interval is a setting read at run time"
  }

  assert {
    condition = alltrue([
      for schedule in [aws_scheduler_schedule.rollup[0], aws_scheduler_schedule.judge[0]] :
      schedule.target[0].arn == "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-eval-jobs" &&
      schedule.target[0].role_arn == "arn:aws:iam::111111111111:role/acme-dev-evals-scheduler-invoke" &&
      schedule.group_name == "default"
    ])
    error_message = "both invoke eval-jobs through the module's own scheduler role, in the default group"
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.schedule[0].policy).Statement == [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-eval-jobs"
    }] && aws_iam_role.schedule[0].name == "acme-dev-evals-scheduler-invoke"
    error_message = "the schedules' role may invoke eval-jobs and nothing else, and is named for the deploy role's pattern"
  }

  assert {
    condition = (
      aws_lambda_event_source_mapping.eval_work[0].event_source_arn == "arn:aws:sqs:eu-west-1:111111111111:acme-dev-eval-work" &&
      aws_lambda_event_source_mapping.eval_work[0].function_name == "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-eval-jobs" &&
      aws_lambda_event_source_mapping.eval_work[0].batch_size == 1 &&
      aws_lambda_event_source_mapping.eval_work[0].scaling_config[0].maximum_concurrency == 2
    )
    error_message = "eval-work feeds eval-jobs one job at a time, two at most at once"
  }

  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.eval_rating_delta[0].log_group_name == "acme-dev-evals" &&
      aws_cloudwatch_log_metric_filter.eval_rating_delta[0].pattern == "{ $.schema = \"evals/1\" }" &&
      aws_cloudwatch_log_metric_filter.eval_rating_delta[0].metric_transformation[0].name == "EvalRatingDelta" &&
      aws_cloudwatch_log_metric_filter.eval_rating_delta[0].metric_transformation[0].value == "$.rating_delta" &&
      aws_cloudwatch_log_metric_filter.eval_rating_delta[0].metric_transformation[0].dimensions == tomap({ Performance = "$.performance" })
    )
    error_message = "EvalRatingDelta sums every event's delta, by performance"
  }

  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.eval_events[0].log_group_name == "acme-dev-evals" &&
      aws_cloudwatch_log_metric_filter.eval_events[0].pattern == "{ $.schema = \"evals/1\" }" &&
      aws_cloudwatch_log_metric_filter.eval_events[0].metric_transformation[0].name == "EvalEvents" &&
      aws_cloudwatch_log_metric_filter.eval_events[0].metric_transformation[0].value == "1"
    )
    error_message = "EvalEvents counts every evals/1 event"
  }

  assert {
    condition = (
      aws_cloudwatch_log_metric_filter.rollups[0].log_group_name == "/aws/lambda/acme-dev-eval-jobs" &&
      aws_cloudwatch_log_metric_filter.rollups[0].pattern == "{ $.event = \"rollup_done\" }" &&
      aws_cloudwatch_log_metric_filter.rollups[0].metric_transformation[0].default_value == null
    )
    error_message = "the heartbeat counts the rollup's rollup_done lines, with no default: a quiet log is missing data"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].alarm_name == "acme-dev-eval-rollup-heartbeat" &&
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].treat_missing_data == "breaching" &&
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].period * aws_cloudwatch_metric_alarm.rollup_heartbeat[0].evaluation_periods == 10800 &&
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].datapoints_to_alarm == aws_cloudwatch_metric_alarm.rollup_heartbeat[0].evaluation_periods &&
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].comparison_operator == "LessThanThreshold" &&
      aws_cloudwatch_metric_alarm.rollup_heartbeat[0].threshold == 1
    )
    error_message = "three hours without a finished rollup breaches, missing data included"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.eval_work_age[0].alarm_name == "acme-dev-eval-work-age" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].metric_name == "ApproximateAgeOfOldestMessage" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].dimensions["QueueName"] == "acme-dev-eval-work" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].comparison_operator == "GreaterThanThreshold" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].threshold == 7200 &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].statistic == "Maximum" &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].period * aws_cloudwatch_metric_alarm.eval_work_age[0].evaluation_periods <= 900 &&
      aws_cloudwatch_metric_alarm.eval_work_age[0].treat_missing_data == "notBreaching"
    )
    error_message = "the oldest job on eval-work older than two hours, noticed within 15 minutes; an idle queue, which SQS stops reporting on, does not alarm"
  }

  assert {
    condition = alltrue([
      for alarm in [aws_cloudwatch_metric_alarm.rollup_heartbeat[0], aws_cloudwatch_metric_alarm.eval_work_age[0]] :
      alarm.alarm_actions == toset([var.alerts_topic_arn]) && alarm.ok_actions == toset([var.alerts_topic_arn])
    ])
    error_message = "each notifies the alerts topic, on alarm and on recovery"
  }

  assert {
    condition = (
      output.work_queue_url == "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-eval-work" &&
      output.function_names == ["acme-dev-eval-jobs"] &&
      output.alarm_names == ["acme-dev-eval-rollup-heartbeat", "acme-dev-eval-work-age"]
    )
    error_message = "what the roots read: the queue's URL for every Lambda, the function for observability, the alarms"
  }
}

run "production_protects_the_table" {
  command = plan

  variables {
    environment_name = "production"
    protect_data     = true
  }

  assert {
    condition     = aws_dynamodb_table.evals.deletion_protection_enabled == true && aws_dynamodb_table.evals.point_in_time_recovery[0].enabled == true
    error_message = "deletion protection and point-in-time recovery, like the app tables"
  }

  assert {
    condition     = aws_dynamodb_table.evals.name == "acme-production-evals" && aws_cloudwatch_log_group.evals.name == "acme-production-evals"
    error_message = "named for the environment"
  }
}

run "an_environment_name_that_is_not_one_is_refused" {
  command = plan

  variables {
    environment_name = "Prod uction"
  }

  expect_failures = [var.environment_name]
}
