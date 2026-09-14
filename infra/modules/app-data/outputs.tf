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

output "table_arns" {
  value = [
    aws_dynamodb_table.topics.arn,
    aws_dynamodb_table.findings.arn,
    aws_dynamodb_table.candidate_ideas.arn,
    aws_dynamodb_table.articles.arn,
    aws_dynamodb_table.moderation_queue.arn,
    aws_dynamodb_table.feedback.arn,
    aws_dynamodb_table.prompt_refinements.arn,
  ]
  description = "All 7 table ARNs as a list, convenient for building an IAM policy resources list in the calling environment."
}
