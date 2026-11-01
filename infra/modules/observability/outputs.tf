output "sns_topic_arn" {
  value       = aws_sns_topic.alerts.arn
  description = "ARN of the SNS topic every pipeline-health alarm publishes to."
}

output "pipeline_health_dashboard_name" {
  value       = aws_cloudwatch_dashboard.pipeline_health.dashboard_name
  description = "Name of the pipeline-health dashboard (pipeline_health.tf)."
}

output "prompt_fallback_metric" {
  value       = { namespace = "BloggerBear/${var.environment_name}", name = "PromptFallback" }
  description = "The metric the prompt_fallback filters feed (one per pipeline log group), for the evals module's ranch-and-prompts dashboard. Its name whether or not any filter exists yet."
}
