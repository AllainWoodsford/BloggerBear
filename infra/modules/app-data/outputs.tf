output "topics_table_name" {
  value       = aws_dynamodb_table.topics.name
  description = "Name of the Topics DynamoDB table."
}

output "topics_table_arn" {
  value       = aws_dynamodb_table.topics.arn
  description = "ARN of the Topics DynamoDB table."
}

output "findings_table_name" {
  value       = aws_dynamodb_table.findings.name
  description = "Name of the Findings DynamoDB table."
}

output "findings_table_arn" {
  value       = aws_dynamodb_table.findings.arn
  description = "ARN of the Findings DynamoDB table."
}

output "candidate_ideas_table_name" {
  value       = aws_dynamodb_table.candidate_ideas.name
  description = "Name of the CandidateIdeas DynamoDB table."
}

output "candidate_ideas_table_arn" {
  value       = aws_dynamodb_table.candidate_ideas.arn
  description = "ARN of the CandidateIdeas DynamoDB table."
}

output "articles_table_name" {
  value       = aws_dynamodb_table.articles.name
  description = "Name of the Articles DynamoDB table."
}

output "articles_table_arn" {
  value       = aws_dynamodb_table.articles.arn
  description = "ARN of the Articles DynamoDB table."
}

output "moderation_queue_table_name" {
  value       = aws_dynamodb_table.moderation_queue.name
  description = "Name of the ModerationQueue DynamoDB table."
}

output "moderation_queue_table_arn" {
  value       = aws_dynamodb_table.moderation_queue.arn
  description = "ARN of the ModerationQueue DynamoDB table."
}

output "feedback_table_name" {
  value       = aws_dynamodb_table.feedback.name
  description = "Name of the Feedback DynamoDB table."
}

output "feedback_table_arn" {
  value       = aws_dynamodb_table.feedback.arn
  description = "ARN of the Feedback DynamoDB table."
}

output "prompt_refinements_table_name" {
  value       = aws_dynamodb_table.prompt_refinements.name
  description = "Name of the PromptRefinements DynamoDB table."
}

output "prompt_refinements_table_arn" {
  value       = aws_dynamodb_table.prompt_refinements.arn
  description = "ARN of the PromptRefinements DynamoDB table."
}

output "failed_executions_table_name" {
  value       = aws_dynamodb_table.failed_executions.name
  description = "Name of the FailedExecutions DynamoDB table."
}

output "failed_executions_table_arn" {
  value       = aws_dynamodb_table.failed_executions.arn
  description = "ARN of the FailedExecutions DynamoDB table."
}

output "musings_table_name" {
  value       = aws_dynamodb_table.musings.name
  description = "Name of the Musings DynamoDB table."
}

output "musings_table_arn" {
  value       = aws_dynamodb_table.musings.arn
  description = "ARN of the Musings DynamoDB table."
}

output "models_table_name" {
  value       = aws_dynamodb_table.models.name
  description = "Name of the Models DynamoDB table."
}

output "models_table_arn" {
  value       = aws_dynamodb_table.models.arn
  description = "ARN of the Models DynamoDB table."
}

output "model_config_table_name" {
  value       = aws_dynamodb_table.model_config.name
  description = "Name of the ModelConfig DynamoDB table."
}

output "model_config_table_arn" {
  value       = aws_dynamodb_table.model_config.arn
  description = "ARN of the ModelConfig DynamoDB table."
}

output "stats_current_table_name" {
  value       = aws_dynamodb_table.stats_current.name
  description = "Name of the StatsCurrent DynamoDB table."
}

output "stats_current_table_arn" {
  value       = aws_dynamodb_table.stats_current.arn
  description = "ARN of the StatsCurrent DynamoDB table."
}

output "stats_history_table_name" {
  value       = aws_dynamodb_table.stats_history.name
  description = "Name of the StatsHistory DynamoDB table."
}

output "stats_history_table_arn" {
  value       = aws_dynamodb_table.stats_history.arn
  description = "ARN of the StatsHistory DynamoDB table."
}

output "view_counts_table_name" {
  value       = aws_dynamodb_table.view_counts.name
  description = "Name of the ViewCounts DynamoDB table (sharded article view counters)."
}

output "view_counts_table_arn" {
  value       = aws_dynamodb_table.view_counts.arn
  description = "ARN of the ViewCounts DynamoDB table."
}

output "table_arns" {
  value = [
    aws_dynamodb_table.topics.arn,
    aws_dynamodb_table.findings.arn,
    aws_dynamodb_table.candidate_ideas.arn,
    aws_dynamodb_table.articles.arn,
    aws_dynamodb_table.moderation_queue.arn,
    aws_dynamodb_table.feedback.arn,
    aws_dynamodb_table.prompt_refinements.arn,
    aws_dynamodb_table.failed_executions.arn,
    aws_dynamodb_table.musings.arn,
    aws_dynamodb_table.models.arn,
    aws_dynamodb_table.model_config.arn,
    aws_dynamodb_table.stats_current.arn,
    aws_dynamodb_table.stats_history.arn,
    aws_dynamodb_table.view_counts.arn,
  ]
  description = "All 14 table ARNs as a list, convenient for building an IAM policy resources list in the calling environment."
}
