# The queue platform's module (main.tf), planned with a mocked AWS provider like the other modules'
# tests. What is held: both queues are FIFO with content-based deduplication off and the work queue
# redrives to the FIFO dead-letter queue; the dispatcher's policy on lambda_exec grants exactly its
# four rights on exactly their resources, and the consumers' exactly theirs; the research tick
# takes one job at a time, at most worker_concurrency at once, never fewer than 2; the dispatcher
# runs every minute with its own log group, and neither Scheduler nor Lambda retries a failed tick;
# the heartbeat alarm breaches when ticks stop; and both dead-letter alarms fire on a single job.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

override_resource {
  target          = aws_sqs_queue.work
  override_during = plan
  values = {
    arn = "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work.fifo"
    url = "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-work.fifo"
  }
}

override_resource {
  target          = aws_sqs_queue.work_dlq
  override_during = plan
  values = {
    arn = "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work-dlq.fifo"
    url = "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-work-dlq.fifo"
  }
}

override_resource {
  target          = aws_lambda_function.dispatcher
  override_during = plan
  values = {
    arn = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-dispatcher"
  }
}

override_resource {
  target          = aws_iam_role.dispatcher_schedule
  override_during = plan
  values = {
    arn = "arn:aws:iam::111111111111:role/acme-dev-dispatcher-scheduler-invoke"
  }
}

variables {
  # Another deployment's prefix: nothing here may depend on the original one.
  unique_name_prefix    = "acme"
  environment_name      = "dev"
  lambda_exec_role_arn  = "arn:aws:iam::111111111111:role/acme-dev-lambda-exec"
  lambda_exec_role_name = "acme-dev-lambda-exec"
  lambda_package_path   = "lambda-build/lambdas.zip"
  lambda_package_hash   = "c2hhMjU2LW9mLXRoZS1wYWNrYWdl"
  lambda_environment = {
    TOPICS_TABLE       = "acme-dev-topics"
    MODEL_CONFIG_TABLE = "acme-dev-model-config"
    NAME_PREFIX        = "acme"
    ENVIRONMENT_NAME   = "dev"
    WORK_QUEUE_URL     = ""
  }
  research_tick_arn  = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-research-tick"
  dlq_handler_arn    = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-dlq-handler"
  state_machine_arn  = "arn:aws:states:eu-west-1:111111111111:stateMachine:acme-dev-daily-cycle"
  worker_concurrency = 2
  alerts_topic_arn   = "arn:aws:sns:eu-west-1:111111111111:acme-dev-alerts"
}

run "both_queues_are_fifo_without_content_dedup_and_the_work_queue_redrives_to_the_fifo_dlq" {
  command = plan

  assert {
    condition     = aws_sqs_queue.work.name == "acme-dev-work.fifo" && aws_sqs_queue.work.fifo_queue == true
    error_message = "the work queue is <prefix>-<env>-work.fifo, a FIFO queue"
  }

  assert {
    condition     = aws_sqs_queue.work.content_based_deduplication == false && aws_sqs_queue.work_dlq.content_based_deduplication == false
    error_message = "content-based deduplication is off on both: the dispatcher sets the deduplication id"
  }

  assert {
    condition     = aws_sqs_queue.work.visibility_timeout_seconds == 720
    error_message = "six times the research tick's 120 s timeout"
  }

  assert {
    condition = jsondecode(aws_sqs_queue.work.redrive_policy) == {
      deadLetterTargetArn = "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work-dlq.fifo"
      maxReceiveCount     = 3
    }
    error_message = "three receives, then the work queue's own dead-letter queue"
  }

  assert {
    condition     = aws_sqs_queue.work_dlq.name == "acme-dev-work-dlq.fifo" && aws_sqs_queue.work_dlq.fifo_queue == true
    error_message = "a FIFO queue's dead-letter queue must be FIFO too"
  }
}

run "the_dispatchers_policy_grants_exactly_its_four_rights_on_exactly_their_resources" {
  command = plan

  assert {
    condition     = aws_iam_role_policy.dispatcher.role == "acme-dev-lambda-exec"
    error_message = "the policy is on the shared lambda_exec role the dispatcher runs as"
  }

  assert {
    condition = toset([for statement in data.aws_iam_policy_document.dispatcher.statement : statement.sid]) == toset([
      "SendResearchJobs", "StartDailyCycles", "ListDailyCycles", "DescribeDailyCycles",
    ])
    error_message = "four statements and no more"
  }

  assert {
    condition = alltrue([
      for statement in data.aws_iam_policy_document.dispatcher.statement : length(statement.actions) == 1 && statement.effect == "Allow"
    ])
    error_message = "one action each"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.dispatcher.statement : statement.resources if statement.sid == "SendResearchJobs" && contains(tolist(statement.actions), "sqs:SendMessage")
    ])) == toset(["arn:aws:sqs:eu-west-1:111111111111:acme-dev-work.fifo"])
    error_message = "SendMessage on work.fifo only"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.dispatcher.statement : statement.resources if statement.sid == "StartDailyCycles" && contains(tolist(statement.actions), "states:StartExecution")
    ])) == toset(["arn:aws:states:eu-west-1:111111111111:stateMachine:acme-dev-daily-cycle"])
    error_message = "StartExecution on the daily-cycle state machine only"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.dispatcher.statement : statement.resources if statement.sid == "ListDailyCycles" && contains(tolist(statement.actions), "states:ListExecutions")
    ])) == toset(["arn:aws:states:eu-west-1:111111111111:stateMachine:acme-dev-daily-cycle"])
    error_message = "ListExecutions on the state machine itself"
  }

  assert {
    condition = toset(flatten([
      for statement in data.aws_iam_policy_document.dispatcher.statement : statement.resources if statement.sid == "DescribeDailyCycles" && contains(tolist(statement.actions), "states:DescribeExecution")
    ])) == toset(["arn:aws:states:eu-west-1:111111111111:execution:acme-dev-daily-cycle:*"])
    error_message = "DescribeExecution on that state machine's executions only"
  }
}

run "the_consumers_may_receive_and_delete_on_the_two_queues_and_nothing_else" {
  command = plan

  assert {
    condition     = aws_iam_role_policy.consume.role == "acme-dev-lambda-exec" && length(data.aws_iam_policy_document.consume.statement) == 1
    error_message = "one statement, on lambda_exec"
  }

  assert {
    condition = toset(data.aws_iam_policy_document.consume.statement[0].actions) == toset([
      "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes",
    ])
    error_message = "what Lambda's poller needs, and no SendMessage"
  }

  assert {
    condition = toset(data.aws_iam_policy_document.consume.statement[0].resources) == toset([
      "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work.fifo",
      "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work-dlq.fifo",
    ])
    error_message = "on the two queues of this module only"
  }
}

run "the_research_tick_takes_one_job_at_a_time_at_most_worker_concurrency_at_once" {
  command = plan

  variables {
    worker_concurrency = 5
  }

  assert {
    condition     = aws_lambda_event_source_mapping.research_tick.event_source_arn == "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work.fifo" && aws_lambda_event_source_mapping.research_tick.function_name == var.research_tick_arn
    error_message = "the work queue feeds the research tick"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.research_tick.batch_size == 1
    error_message = "one job per invocation"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.research_tick.scaling_config[0].maximum_concurrency == 5
    error_message = "the one knob is the mapping's maximum_concurrency"
  }

  assert {
    condition     = aws_lambda_event_source_mapping.work_dlq.event_source_arn == "arn:aws:sqs:eu-west-1:111111111111:acme-dev-work-dlq.fifo" && aws_lambda_event_source_mapping.work_dlq.function_name == var.dlq_handler_arn
    error_message = "the dead-letter queue feeds dlq_handler"
  }
}

run "fewer_than_two_workers_is_refused" {
  command = plan

  variables {
    worker_concurrency = 1
  }

  expect_failures = [var.worker_concurrency]
}

run "a_fraction_of_a_worker_is_refused" {
  command = plan

  variables {
    worker_concurrency = 2.5
  }

  expect_failures = [var.worker_concurrency]
}

run "the_dispatcher_runs_every_minute_as_lambda_exec_with_its_own_log_group" {
  command = plan

  assert {
    condition     = aws_lambda_function.dispatcher.function_name == "acme-dev-dispatcher" && aws_lambda_function.dispatcher.role == var.lambda_exec_role_arn
    error_message = "<prefix>-<env>-dispatcher, as the shared role"
  }

  assert {
    condition     = aws_lambda_function.dispatcher.handler == "dispatcher_handler.handler" && aws_lambda_function.dispatcher.timeout == 60
    error_message = "the dispatcher handler, with a minute to run"
  }

  assert {
    condition     = aws_cloudwatch_log_group.dispatcher.name == "/aws/lambda/acme-dev-dispatcher" && aws_cloudwatch_log_group.dispatcher.retention_in_days == 90
    error_message = "its own log group, kept 90 days like every pipeline Lambda's"
  }

  assert {
    condition     = aws_lambda_function.dispatcher.environment[0].variables["WORK_QUEUE_URL"] == "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-work.fifo"
    error_message = "told its own queue, whatever the shared environment says"
  }

  assert {
    condition     = aws_lambda_function.dispatcher.environment[0].variables["STATE_MACHINE_ARN"] == var.state_machine_arn && aws_lambda_function.dispatcher.environment[0].variables["TOPICS_TABLE"] == "acme-dev-topics"
    error_message = "and the state machine, beside the shared environment"
  }

  assert {
    condition     = aws_scheduler_schedule.dispatcher.schedule_expression == "rate(1 minute)" && aws_scheduler_schedule.dispatcher.target[0].arn == "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-dispatcher"
    error_message = "invoked every minute"
  }

  assert {
    condition     = aws_scheduler_schedule.dispatcher.target[0].retry_policy[0].maximum_retry_attempts == 0
    error_message = "Scheduler never retries delivering a tick: the next tick covers it"
  }

  assert {
    condition     = aws_lambda_function_event_invoke_config.dispatcher.function_name == "acme-dev-dispatcher" && aws_lambda_function_event_invoke_config.dispatcher.maximum_retry_attempts == 0
    error_message = "nor does Lambda retry a failed async tick: a replay can run beside the next tick with the same last_tick"
  }

  assert {
    condition     = aws_lambda_function_event_invoke_config.dispatcher.maximum_event_age_in_seconds == 60
    error_message = "a tick event held over a minute is dropped: a newer tick has covered its window"
  }

  assert {
    condition     = aws_scheduler_schedule.dispatcher.target[0].role_arn == "arn:aws:iam::111111111111:role/acme-dev-dispatcher-scheduler-invoke" && aws_iam_role.dispatcher_schedule.name == "acme-dev-dispatcher-scheduler-invoke"
    error_message = "through a role of its own, named for the deploy role's -scheduler-invoke pattern"
  }

  assert {
    condition = jsondecode(aws_iam_role_policy.dispatcher_schedule.policy).Statement == [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = "arn:aws:lambda:eu-west-1:111111111111:function:acme-dev-dispatcher"
    }]
    error_message = "which may invoke the dispatcher and nothing else"
  }
}

run "the_heartbeat_breaches_when_no_tick_finishes_for_a_quarter_of_an_hour" {
  command = plan

  assert {
    condition     = aws_cloudwatch_log_metric_filter.dispatcher_ticks.log_group_name == "/aws/lambda/acme-dev-dispatcher" && aws_cloudwatch_log_metric_filter.dispatcher_ticks.pattern == "{ $.event = \"dispatcher_tick\" && $.outcome = \"ok\" }"
    error_message = "the dispatcher's finished ticks, in its own log group"
  }

  assert {
    condition     = aws_cloudwatch_log_metric_filter.dispatcher_ticks.metric_transformation[0].default_value == null
    error_message = "no default value: a quiet log must be missing data, not zeros"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.dispatcher_heartbeat.treat_missing_data == "breaching" && aws_cloudwatch_metric_alarm.dispatcher_heartbeat.period * aws_cloudwatch_metric_alarm.dispatcher_heartbeat.evaluation_periods == 900
    error_message = "missing data breaches, over 15 minutes"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.dispatcher_heartbeat.datapoints_to_alarm == aws_cloudwatch_metric_alarm.dispatcher_heartbeat.evaluation_periods
    error_message = "every period of the quarter hour must be quiet: one quiet five minutes is not a stopped dispatcher"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.dispatcher_heartbeat.comparison_operator == "LessThanThreshold" && aws_cloudwatch_metric_alarm.dispatcher_heartbeat.threshold == 1
    error_message = "fewer than one finished tick is an alarm"
  }
}

run "the_alarms_are_named_for_the_environment_and_notify_the_alerts_topic" {
  command = plan

  assert {
    condition = output.alarm_names == [
      "acme-dev-work-queue-age", "acme-dev-work-dlq-messages", "acme-dev-work-dlq-received",
      "acme-dev-dispatcher-heartbeat",
    ]
    error_message = "the module's four alarms"
  }

  assert {
    condition = alltrue([
      for alarm in [
        aws_cloudwatch_metric_alarm.work_queue_age, aws_cloudwatch_metric_alarm.work_dlq_messages,
        aws_cloudwatch_metric_alarm.work_dlq_received, aws_cloudwatch_metric_alarm.dispatcher_heartbeat,
      ] : alarm.alarm_actions == toset([var.alerts_topic_arn]) && alarm.ok_actions == toset([var.alerts_topic_arn])
    ])
    error_message = "each notifies the alerts topic, on alarm and on recovery"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.work_queue_age.metric_name == "ApproximateAgeOfOldestMessage" && aws_cloudwatch_metric_alarm.work_queue_age.threshold == 900 && aws_cloudwatch_metric_alarm.work_queue_age.dimensions["QueueName"] == "acme-dev-work.fifo"
    error_message = "a job older than 900 s on the work queue"
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.work_dlq_messages.dimensions["QueueName"] == "acme-dev-work-dlq.fifo" && aws_cloudwatch_metric_alarm.work_dlq_received.dimensions["QueueName"] == "acme-dev-work-dlq.fifo"
    error_message = "both alarms watch the work queue's dead-letter queue"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.work_dlq_messages.namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.work_dlq_messages.metric_name == "ApproximateNumberOfMessagesVisible" &&
      aws_cloudwatch_metric_alarm.work_dlq_messages.statistic == "Maximum" &&
      aws_cloudwatch_metric_alarm.work_dlq_messages.comparison_operator == "GreaterThanThreshold" &&
      aws_cloudwatch_metric_alarm.work_dlq_messages.threshold == 0
    )
    error_message = "one job still visible on the dead-letter queue: dlq_handler could not take it"
  }

  assert {
    condition = (
      aws_cloudwatch_metric_alarm.work_dlq_received.namespace == "AWS/SQS" &&
      aws_cloudwatch_metric_alarm.work_dlq_received.metric_name == "NumberOfMessagesReceived" &&
      aws_cloudwatch_metric_alarm.work_dlq_received.statistic == "Sum" &&
      aws_cloudwatch_metric_alarm.work_dlq_received.comparison_operator == "GreaterThanThreshold" &&
      aws_cloudwatch_metric_alarm.work_dlq_received.threshold == 0
    )
    error_message = "one job received from the dead-letter queue at all: a job that failed three times"
  }

  assert {
    condition     = output.function_names == ["acme-dev-dispatcher"] && output.work_queue_url == "https://sqs.eu-west-1.amazonaws.com/111111111111/acme-dev-work.fifo"
    error_message = "what the roots read: the function for observability, the URL for every Lambda"
  }
}
