variable "name" {
  type        = string
  description = "Base name for this API's resources, e.g. \"bloggerbear-dev-admin\"."
}

variable "stage_name" {
  type        = string
  description = "Stage name -- also the path prefix in the invoke URL. Unlike HTTP APIs' $default stage (no prefix), REST API stages always appear in the URL, e.g. \".../dev/topics\" rather than \".../topics\"."
}

variable "lambda_invoke_arn" {
  type        = string
  description = "invoke_arn of the Lambda function every route proxies to (AWS_PROXY integration -- one Lambda handles all routing internally, same as this project's HTTP API predecessor)."
}

variable "lambda_function_name" {
  type        = string
  description = "function_name of the same Lambda, for the resource-based invoke permission."
}

variable "authorization" {
  type        = string
  description = "API Gateway method authorization for every route -- \"AWS_IAM\" (SigV4) or \"NONE\" (public)."
  validation {
    condition     = contains(["AWS_IAM", "NONE"], var.authorization)
    error_message = "authorization must be \"AWS_IAM\" or \"NONE\"."
  }
}

variable "routes" {
  type        = set(string)
  description = "Route keys in \"METHOD /path/{param}\" form, e.g. [\"GET /topics\", \"POST /topics/{topic_id}/trigger\"]. Every unique path segment -- including intermediate ones with no method of their own, e.g. \"/moderation-queue/{queue_id}\" when only its /approve and /reject children have routes -- gets its own aws_api_gateway_resource node, built automatically from this set."
}

variable "enable_cors" {
  type        = bool
  default     = false
  description = "Add a Lambda-proxied OPTIONS method on every unique path in var.routes, for browser CORS preflight. REST APIs have no declarative equivalent of HTTP APIs' cors_configuration block when every route (OPTIONS included) is AWS_PROXY -- the proxied Lambda function itself must handle OPTIONS and add CORS response headers to every response. Only meaningful for authorization = \"NONE\" APIs called from browser JS (this project's public API); the admin API is called via SigV4-signed CLI requests, never a browser, so never needs this."
}

variable "throttling_rate_limit" {
  type        = number
  description = "Steady-state requests per second the whole stage accepts before API Gateway answers 429 (every method, unless method_throttling sets its own)."
}

variable "throttling_burst_limit" {
  type        = number
  description = "How many requests the stage accepts at once above the steady rate (API Gateway's token-bucket size)."
}

variable "method_throttling" {
  type = map(object({
    rate_limit  = number
    burst_limit = number
  }))
  default     = {}
  description = "Per-route throttling that overrides the stage default, keyed by route exactly as in var.routes, e.g. { \"POST /articles/{article_id}/feedback\" = { rate_limit = 2, burst_limit = 5 } }."
}

variable "access_log_retention_days" {
  type        = number
  default     = 30
  description = "How long the stage's access log (one line per request, no visitor details) is kept."
}

variable "web_acl_id" {
  type        = string
  default     = ""
  description = "ARN of a REGIONAL-scope WAFv2 Web ACL to associate with this API's stage. Only used when associate_web_acl = true."
}

variable "associate_web_acl" {
  type        = bool
  default     = false
  description = <<-EOT
    Associate web_acl_id with the stage. This is a separate flag, set by the caller, because the ACL is
    usually created in the same apply: its ARN is not known at plan time, and Terraform cannot decide
    `count` from an unknown value ("Invalid count argument"). That is exactly what stopped a from-scratch
    production apply, while an environment that already had its ACL never noticed. A caller that passes
    web_acl_id MUST set this to true: lambdas/tests/test_terraform_wiring.py fails if it does not.
  EOT
}

variable "aws_region" {
  type        = string
  description = "The deployment's home region (the calling root's var.aws_region), which is part of the API's execute-api host name (the api_domain output). A variable and not a data source so that output stays a plain string built from the REST API alone. No default, so a root cannot forget to pass it."
}
