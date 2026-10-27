output "work_queue_url" {
  description = "The FIFO work queue's URL: every pipeline Lambda's WORK_QUEUE_URL, where the dispatcher (and, from Q2, the admin API's manual triggers) send research jobs."
  value       = aws_sqs_queue.work.url
}

output "work_queue_arn" {
  value = aws_sqs_queue.work.arn
}

output "work_queue_name" {
  value = aws_sqs_queue.work.name
}

output "work_dlq_url" {
  value = aws_sqs_queue.work_dlq.url
}

output "work_dlq_arn" {
  value = aws_sqs_queue.work_dlq.arn
}

output "work_dlq_name" {
  value = aws_sqs_queue.work_dlq.name
}

output "dispatcher_function_name" {
  value = aws_lambda_function.dispatcher.function_name
}

output "function_names" {
  description = "The functions this module makes, for the root's local.pipeline_lambda_function_names (module.observability's errors and throttles alarms and its dashboards)."
  value       = [aws_lambda_function.dispatcher.function_name]
}

output "alarm_names" {
  description = "The alarms this module makes: the work queue's oldest message, the dead-letter queue's two, and the dispatcher's heartbeat."
  value = [
    aws_cloudwatch_metric_alarm.work_queue_age.alarm_name,
    aws_cloudwatch_metric_alarm.work_dlq_messages.alarm_name,
    aws_cloudwatch_metric_alarm.work_dlq_received.alarm_name,
    aws_cloudwatch_metric_alarm.dispatcher_heartbeat.alarm_name,
  ]
}
