output "distribution_domain_name" {
  value = module.static_site.distribution_domain_name
}

output "distribution_id" {
  value = module.static_site.distribution_id
}

output "bucket_name" {
  value = module.static_site.bucket_name
}

output "content_bucket_name" {
  value = aws_s3_bucket.content.bucket
}

output "research_tick_function_name" {
  value = aws_lambda_function.research_tick.function_name
}

output "daily_cycle_function_name" {
  value = aws_lambda_function.daily_cycle.function_name
}
