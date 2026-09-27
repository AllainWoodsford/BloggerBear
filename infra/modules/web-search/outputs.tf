output "gateway_arn" {
  value       = aws_bedrockagentcore_gateway.this.gateway_arn
  description = "For the callers' bedrock-agentcore:InvokeGateway grant."
}

output "gateway_url" {
  value       = aws_bedrockagentcore_gateway.this.gateway_url
  description = "The gateway's MCP endpoint (AGENTCORE_WEB_SEARCH_URL)."
}

output "region" {
  value       = var.region
  description = "The Region to SigV4-sign requests for (AGENTCORE_WEB_SEARCH_REGION)."
}

output "tool_name" {
  value       = "${var.target_name}___WebSearch"
  description = "The MCP tool name to call (AGENTCORE_WEB_SEARCH_TOOL)."
}
