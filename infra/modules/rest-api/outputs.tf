output "invoke_url" {
  value       = aws_api_gateway_stage.this.invoke_url
  description = "Full base URL to invoke this API, stage included (e.g. https://{id}.execute-api.{region}.amazonaws.com/dev) -- unlike the HTTP API predecessor's $default stage, the stage name is always part of the URL."
}

output "execution_arn" {
  value = aws_api_gateway_rest_api.this.execution_arn
}

output "stage_arn" {
  value = aws_api_gateway_stage.this.arn
}
