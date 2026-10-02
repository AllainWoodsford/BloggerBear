output "sns_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "ARN of the SNS topic every pipeline-health alarm publishes to."
}

output "dashboard_name" {
  value       = aws_cloudwatch_dashboard.pipeline.dashboard_name
  description = "Name of the CloudWatch dashboard summarizing pipeline health."
}

output "lambda_runs_dashboard_name" {
  value       = aws_cloudwatch_dashboard.lambda_runs.dashboard_name
  description = "Name of the CloudWatch dashboard counting Lambda runs and feedback outcomes over a chosen time range."
}
