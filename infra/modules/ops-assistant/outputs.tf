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
