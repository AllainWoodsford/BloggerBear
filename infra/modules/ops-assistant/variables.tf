variable "environment_name" {
  type        = string
  description = "\"dev\" or \"production\": part of every resource's name, and passed to the function as ENVIRONMENT_NAME."
}

variable "tables" {
  type = map(object({
    name = string
    arn  = string
  }))
  description = <<-EOT
    The DynamoDB tables the assistant's tools read, keyed by the environment variable
    lambdas/common/dynamo.py looks each one up by, e.g.
    { TOPICS_TABLE = { name = module.app_data.topics_table_name, arn = module.app_data.topics_table_arn } }.
    The one map decides both what the function is told (each key becomes an environment variable
    holding the table's name) and what its role may read (GetItem, Query, Scan and BatchGetItem on
    each ARN and its indexes, nothing else), so the two cannot drift apart. Leave a table out and
    the function can neither find it nor read it.
  EOT
}

variable "content_bucket_name" {
  type        = string
  description = "The content bucket's name, passed to the function as CONTENT_BUCKET."
}

variable "content_bucket_arn" {
  type        = string
  description = "The content bucket's ARN. The role may GetObject under its articles/ prefix only."
}

variable "stage_name" {
  type        = string
  description = "The API's stage name. It is part of the URL: https://<api id>.execute-api.<region>.amazonaws.com/<stage>/mcp."
}

variable "hosted_ui_domain_prefix" {
  type        = string
  description = "Prefix of Cognito's hosted sign-in domain (<prefix>.auth.<region>.amazoncognito.com). Unique across every AWS account in the region, and Cognito refuses a prefix containing \"aws\", \"amazon\" or \"cognito\"."
}

variable "callback_urls" {
  type        = list(string)
  description = "Where the hosted sign-in page may send the browser back to with an authorization code: the assistant's page. Cognito compares the whole URL exactly, and accepts only https (or http://localhost)."

  validation {
    condition     = length(var.callback_urls) > 0
    error_message = "callback_urls needs at least one URL: Cognito refuses an OAuth client with none."
  }
}

variable "logout_urls" {
  type        = list(string)
  description = "Where the hosted page may send the browser after signing out."
}

variable "mfa_configuration" {
  type        = string
  description = "Cognito's own three values. \"OFF\": no second factor. \"OPTIONAL\": each user may set up an authenticator app, and is asked for a code once they have. \"ON\": every user must; one who has not set it up is made to at their next sign-in."

  validation {
    condition     = contains(["OFF", "OPTIONAL", "ON"], var.mfa_configuration)
    error_message = "mfa_configuration must be \"OFF\", \"OPTIONAL\" or \"ON\"."
  }
}

variable "allowed_origins" {
  type        = list(string)
  default     = []
  description = "Origins (e.g. \"https://bloggerbear.com\") a browser page may call the MCP endpoint from, passed to the function as OPS_MCP_ALLOWED_ORIGINS. The server answers 403 to any other Origin. Empty is right while only the agent Lambda calls it: a request with no Origin header is not a browser's and is let through."
}

variable "allowed_cidrs" {
  type        = list(string)
  default     = []
  sensitive   = true
  description = "The operator's addresses (CIDRs), passed to the function as OPS_ASSISTANT_ALLOWED_CIDRS. Only read when the config table's assistant_access setting is \"allowlist\"; the default setting, \"open\", never looks at it. Pass the same list the admin API's WAF allowlist uses, so there is one list of the operator's addresses per environment. Sensitive for the reason that list is: a plan must not print a home IP address."
}

variable "throttling_rate_limit" {
  type        = number
  description = "Steady-state requests per second the stage accepts before API Gateway answers 429."
}

variable "throttling_burst_limit" {
  type        = number
  description = "How many requests the stage accepts at once above the steady rate."
}

variable "access_log_retention_days" {
  type        = number
  default     = 30
  description = "How long the stage's access log (one line per request, no caller details) is kept."
}

variable "log_retention_days" {
  type        = number
  default     = 90
  description = "How long the function's own log is kept. 90 days is what every other Lambda's log group has."
}
