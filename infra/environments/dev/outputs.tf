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

output "ops_mcp_url" {
  value       = module.ops_assistant.mcp_url
  description = "The operator's assistant's MCP endpoint (POST, Streamable HTTP, JSON responses). Every request needs an access token from the user pool below, carrying the bloggerbear-ops/read scope, in the Authorization header."
}

output "ops_ask_url" {
  value       = module.ops_assistant.ops_ask_url
  description = "The operator's assistant's agent endpoint: POST {\"question\": ...} here with the same access token the MCP endpoint takes. What the assistant's page calls."
}

output "ops_user_pool_id" {
  value       = module.ops_assistant.user_pool_id
  description = "The assistant's Cognito user pool. It starts empty: create the operator's user by hand (aws cognito-idp admin-create-user --user-pool-id <this> --username <name>). Terraform never creates one."
}

output "ops_app_client_id" {
  value       = module.ops_assistant.app_client_id
  description = "The app client the assistant's sign-in page uses: authorization code with PKCE, no secret."
}

output "ops_hosted_ui_domain" {
  value       = module.ops_assistant.hosted_ui_domain
  description = "Host name of Cognito's hosted sign-in page for the assistant (https://<this>/oauth2/authorize)."
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
  description = "Dev's Alexa app client id, once ops_alexa_redirect_uris is set; null before."
}

output "ops_alexa_client_secret" {
  value       = module.ops_assistant.alexa_client_secret
  description = "Dev's Alexa app client secret: `terraform output -raw ops_alexa_client_secret`, on the operator's own machine only."
  sensitive   = true
}
