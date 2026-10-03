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

output "admin_api_url" {
  value       = module.admin_api.invoke_url
  description = "Invoke URL for the admin API's dev stage (REST API -- the stage name is part of this URL, unlike the HTTP API predecessor's $default stage). Copy into BLOGGERBEAR_ADMIN_API_URL for scripts/admin_cli.py. Unreachable until var.admin_allowed_cidrs is set (see that variable's description)."
}

output "daily_cycle_state_machine_arn" {
  value       = aws_sfn_state_machine.daily_cycle.arn
  description = "ARN of the Step Functions state machine wrapping the daily_cycle Lambda invocation (retries + DLQ on failure). Also published as STATE_MACHINE_ARN in local.lambda_env_variables for admin_api_handler's common/scheduler.py."
}

output "pipeline_dlq_url" {
  value       = aws_sqs_queue.pipeline_dlq.url
  description = "URL of the daily_cycle dead-letter queue. Consumed automatically by aws_lambda_function.dlq_handler; also useful for manually sending a synthetic test message (see scripts/README.md's DLQ testing section)."
}

output "public_api_url" {
  value       = module.public_api.invoke_url
  description = "Invoke URL for the public API's dev stage (REST API -- the stage name is part of this URL, unlike the HTTP API predecessor's $default stage). Same value baked into config.js as window.PUBLIC_API_URL for the frontend. Unauthenticated -- reachable by anyone, rate-limited (not IP-allowlisted) by aws_wafv2_web_acl.public_api."
}

output "public_api_cdn_url" {
  value       = module.public_api_cdn.url
  description = "The public API through its CloudFront distribution (Scaling PR C) -- what config.js gives the frontend as window.PUBLIC_API_URL. Cacheable GETs are answered from the edge; public_api_url above still works directly."
}
