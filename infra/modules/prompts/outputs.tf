output "bucket_name" {
  value       = aws_s3_bucket.prompts.bucket
  description = "The prompts bucket's name, for every Lambda's PROMPTS_BUCKET."
}

output "bucket_arn" {
  value       = aws_s3_bucket.prompts.arn
  description = "The prompts bucket's ARN."
}

output "hash_table_name" {
  value       = aws_dynamodb_table.prompt_hashes.name
  description = "The prompt-hash table's name, for every Lambda's PROMPT_HASHES_TABLE."
}

output "hash_table_arn" {
  value       = aws_dynamodb_table.prompt_hashes.arn
  description = "The prompt-hash table's ARN."
}
