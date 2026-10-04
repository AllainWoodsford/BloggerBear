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

output "admin_api_url" {
  value       = module.admin_api.invoke_url
  description = "Invoke URL for the admin API's production stage (REST API -- the stage name is part of this URL, unlike the HTTP API predecessor's $default stage). Copy into BLOGGERBEAR_ADMIN_API_URL for scripts/admin_cli.py. Unreachable until var.admin_allowed_cidrs is set (see that variable's description)."
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
  description = "Invoke URL for the public API's production stage (REST API -- the stage name is part of this URL, unlike the HTTP API predecessor's $default stage). Same value baked into config.js as window.PUBLIC_API_URL for the frontend. Unauthenticated -- reachable by anyone, rate-limited (not IP-allowlisted) by aws_wafv2_web_acl.public_api."
}

output "public_api_cdn_url" {
  value       = module.public_api_cdn.url
  description = "The public API through its CloudFront distribution (Scaling PR C) -- what config.js gives the frontend as window.PUBLIC_API_URL. Cacheable GETs are answered from the edge; public_api_url above still works directly."
}

# The operator's assistant (module.ops_assistant), as dev outputs it.
output "ops_mcp_url" {
  value       = module.ops_assistant.mcp_url
  description = "Production's ops MCP server, behind the Cognito authorizer (MFA required)."
}

output "ops_ask_url" {
  value       = module.ops_assistant.ops_ask_url
  description = "POST /ask, the Strands agent the page calls."
}

output "ops_user_pool_id" {
  value       = module.ops_assistant.user_pool_id
  description = "Production's assistant user pool: make the operator's user with `aws cognito-idp admin-create-user`."
}

output "ops_app_client_id" {
  value       = module.ops_assistant.app_client_id
  description = "The page's public app client id."
}

output "ops_hosted_ui_domain" {
  value       = module.ops_assistant.hosted_ui_domain
  description = "Cognito's hosted sign-in domain for the assistant."
}

output "ops_oauth_protected_resource_url" {
  value       = module.ops_assistant.oauth_protected_resource_url
  description = "The MCP server's Protected Resource Metadata (RFC 9728), for the Alexa+ bootstrap (alexa/README.md)."
}

output "ops_oauth_authorize_url" {
  value       = module.ops_assistant.oauth_authorize_url
  description = "Cognito's authorization endpoint, for `alexa-ai configure-account-linking`."
}

output "ops_oauth_token_url" {
  value       = module.ops_assistant.oauth_token_url
  description = "Cognito's token endpoint, for `alexa-ai configure-account-linking`."
}

output "ops_alexa_client_id" {
  value       = module.ops_assistant.alexa_client_id
  description = "Production's Alexa app client id, once ops_alexa_redirect_uris is set; null before."
}

output "ops_alexa_client_secret" {
  value       = module.ops_assistant.alexa_client_secret
  description = "Production's Alexa app client secret: `terraform output -raw ops_alexa_client_secret`, on the operator's own machine only."
  sensitive   = true
}
