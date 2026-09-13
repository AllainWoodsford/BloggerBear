output "wafv2_web_acl_arn" {
  value       = aws_wafv2_web_acl.this.arn
  description = "ARN of the shared WAF Web ACL. Copy this into infra/environments/dev/terraform.tfvars as web_acl_arn so dev shares the same ACL."
}

output "distribution_domain_name" {
  value = module.static_site.distribution_domain_name
}

output "distribution_id" {
  value = module.static_site.distribution_id
}

output "bucket_name" {
  value = module.static_site.bucket_name
}

output "custom_domain_url" {
  value = module.static_site.custom_domain_url
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
