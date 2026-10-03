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

# Built from the REST API resource alone, never from the stage or deployment: a CloudFront origin
# that points here must not depend on the Lambda (the deployment does, through its integrations),
# or dev's site URL -> Lambda environment -> API -> CDN -> site CSP chain becomes a cycle.
output "api_domain" {
  value       = "${aws_api_gateway_rest_api.this.id}.execute-api.ap-southeast-2.amazonaws.com"
  description = "The API's own execute-api hostname, without scheme or stage -- what a CloudFront origin points at."
}

output "stage_name" {
  value       = var.stage_name
  description = "The stage name, i.e. the first path segment of every invoke URL (a CloudFront origin's origin_path)."
}

output "api_name" {
  value       = aws_api_gateway_rest_api.this.name
  description = "The REST API's name -- the ApiName dimension of its AWS/ApiGateway metrics."
}

output "access_log_group_name" {
  value       = aws_cloudwatch_log_group.access.name
  description = "The stage's access log group (one JSON line per request)."
}
