output "mcp_url" {
  value       = "${aws_api_gateway_stage.this.invoke_url}/mcp"
  description = "The MCP endpoint: POST here, with an access token carrying the read scope in the Authorization header."
}

output "user_pool_id" {
  value       = aws_cognito_user_pool.this.id
  description = "The Cognito user pool's id. Users are created in it by hand (aws cognito-idp admin-create-user), never by Terraform."
}

output "app_client_id" {
  value       = aws_cognito_user_pool_client.page.id
  description = "The app client the sign-in page uses (authorization code with PKCE; it has no secret)."
}

output "hosted_ui_domain" {
  value       = "${aws_cognito_user_pool_domain.this.domain}.auth.${local.aws_region}.amazoncognito.com"
  description = "Host name of Cognito's hosted sign-in page: https://<this>/oauth2/authorize, /oauth2/token and /logout."
}

output "read_scope" {
  value       = local.read_scope
  description = "The scope a token must carry to call the endpoint, for the sign-in page to request."
}

output "function_name" {
  value       = aws_lambda_function.ops_mcp.function_name
  description = "The MCP server Lambda's name."
}

output "role_name" {
  value       = aws_iam_role.ops_mcp.name
  description = "The MCP server Lambda's own, read-only role."
}

# --- The agent (agent.tf) ---

output "ops_ask_url" {
  value       = "${aws_api_gateway_stage.this.invoke_url}/ask"
  description = "The agent endpoint: POST a question here, with an access token carrying the read scope in the Authorization header."
}

output "agent_function_name" {
  value       = aws_lambda_function.ops_agent.function_name
  description = "The agent Lambda's name."
}

output "agent_role_name" {
  value       = aws_iam_role.ops_agent.name
  description = "The agent Lambda's own role: invoke the model, read the access switch, write its own log."
}

output "oauth_protected_resource_url" {
  description = "The MCP server's Protected Resource Metadata (RFC 9728): where an MCP client, Alexa+ included, finds out how to sign in. Every 401 from the API names it in WWW-Authenticate."
  value       = "${local.stage_url}/.well-known/oauth-protected-resource"
}

output "oauth_authorization_server_url" {
  description = "The authorization server metadata (RFC 8414) the protected resource names: Cognito's hosted endpoints, with S256 listed."
  value       = "${local.stage_url}/.well-known/oauth-authorization-server"
}

output "oauth_authorize_url" {
  description = "Cognito's authorization endpoint, for `alexa-ai configure-account-linking`."
  value       = "${local.hosted_ui_base}/oauth2/authorize"
}

output "oauth_token_url" {
  description = "Cognito's token endpoint, for `alexa-ai configure-account-linking`."
  value       = "${local.hosted_ui_base}/oauth2/token"
}

output "alexa_client_id" {
  description = "The Alexa app client's id, or null where alexa_redirect_uris is empty."
  value       = local.alexa_enabled ? aws_cognito_user_pool_client.alexa[0].id : null
}

output "alexa_client_secret" {
  description = "The Alexa app client's secret, or null. Read it with `terraform output -raw` on the operator's own machine, for `alexa-ai configure-account-linking`; never paste it anywhere else."
  value       = local.alexa_enabled && var.alexa_client_secret ? aws_cognito_user_pool_client.alexa[0].client_secret : null
  sensitive   = true
}
