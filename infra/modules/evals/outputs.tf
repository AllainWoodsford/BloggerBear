output "table_name" {
  description = "The evals table's name: every pipeline Lambda's EVALS_TABLE, whether or not evals are on."
  value       = aws_dynamodb_table.evals.name
}

output "table_arn" {
  value = aws_dynamodb_table.evals.arn
}

output "log_group_name" {
  description = "The evals log group's name: every pipeline Lambda's EVALS_LOG_GROUP, whether or not evals are on."
  value       = aws_cloudwatch_log_group.evals.name
}

output "log_group_arn" {
  value = aws_cloudwatch_log_group.evals.arn
}

output "work_queue_url" {
  description = "eval-work's URL, or \"\" while evals are off: every pipeline Lambda's EVAL_WORK_QUEUE_URL. The code takes \"\" as no queue."
  value       = try(aws_sqs_queue.eval_work[0].url, "")
}

output "function_names" {
  description = "The functions this module makes (eval-jobs while evals are on, else none), for the root's local.pipeline_lambda_function_names (module.observability's errors and throttles alarms and its dashboards)."
  value       = aws_lambda_function.eval_jobs[*].function_name
}

output "alarm_names" {
  description = "The alarms this module makes while evals are on: the rollup's heartbeat and eval-work's oldest job."
  value = concat(
    aws_cloudwatch_metric_alarm.rollup_heartbeat[*].alarm_name,
    aws_cloudwatch_metric_alarm.eval_work_age[*].alarm_name,
  )
}
