variable "unique_name_prefix" {
  type        = string
  description = "What every resource name starts with, without a trailing hyphen (the calling root's var.unique_name_prefix, \"bloggerbear\" by default). Every alarm, dashboard, metric filter and the alerts topic is <prefix>-<environment_name>-<what>."
}

variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name the SNS topic/alarms/dashboard (<prefix>-<environment_name>-...)."
}

variable "lambda_function_names" {
  type        = list(string)
  description = "Names of every pipeline Lambda function to monitor. Gets one Errors alarm and one Throttles alarm per entry, and a line each on pipeline health's errors, throttles and duration panels."
}

variable "lambda_timeouts" {
  type        = map(number)
  description = "Each function in lambda_function_names, by name, with its timeout in seconds: pipeline health draws each function's p95 duration as a share of its own timeout. The dashboard refuses a function left out."
}

variable "model_catalog_log_group_name" {
  type        = string
  description = "The model-catalog Lambda's log group, where every model_catalog_job evidence line is printed (common/evidence.py): one metric filter per counter feeds pipeline health's model-catalog row."
}

variable "state_machine_arn" {
  type        = string
  description = "ARN of the daily-cycle Step Functions state machine -- alarmed on ExecutionsFailed and charted on pipeline health."
}

variable "dlq_queue_name" {
  type        = string
  description = "Name of the daily-cycle pipeline's dead-letter queue -- alarmed on ApproximateNumberOfMessagesVisible > 0 (a failed execution is sitting there un-investigated)."
}

variable "alert_email" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    Email address to subscribe to the alerts SNS topic. Left empty by
    default -- every alarm below is created and will still publish to the
    topic either way; this only controls whether a human is actually
    notified. AWS SNS requires manually confirming the subscription (a
    confirmation email/link) before it goes active -- confirm the
    subscription once applied. Sensitive: plans never print the address.
  EOT
}

variable "feedback_log_group_name" {
  type        = string
  description = "The public API Lambda's log group. public_api_handler.py writes one \"rejected a feedback submission (<reason>)\" line per rejected submission; metric filters on it drive the two feedback alarms."
}

variable "security_alert_log_groups" {
  type        = list(string)
  default     = []
  description = "Log groups of the Lambdas that record security events (common/security_events.py): each logs one \"SECURITY_ALERT\" line per high-severity incident, which drives the security alarm. Empty: no alarm."
}

variable "prompt_fallback_log_groups" {
  type        = list(string)
  default     = []
  description = "Log groups of the pipeline Lambdas, for the prompt-fallback alarm: a Lambda that cannot use a prompt asset prints one prompt_fallback evidence line (common/evidence.py) and uses the copy in its code. One metric filter per log group feeds the alarm. Empty: no alarm."
}

variable "api_dashboard_apis" {
  type = list(object({
    label            = string # e.g. "Public API"
    api_name         = string # the REST API's name: the ApiName metric dimension
    stage            = string
    access_log_group = string
  }))
  default     = []
  description = "The REST APIs on pipeline health (their requests, 4XX, 5XX and latency) and in the API Gateway widgets (api_waf_dashboards.tf). Empty: no API panels."
}

variable "api_cdn" {
  type = object({
    distribution_id            = string
    additional_metrics_enabled = bool
    api_name                   = string # the API behind it, to set its origin requests beside the CDN's
    stage                      = string
  })
  default     = null
  description = "The public API's CloudFront distribution, for the cache widgets in api_waf_dashboards.tf. Null: no cache widgets."
}

variable "waf_regional_acls" {
  type = list(object({
    label       = string
    metric_name = string # the ACL's visibility_config metric_name: the WebACL metric dimension
    log_group   = string
  }))
  default     = []
  description = "The REGIONAL web ACLs (in var.aws_region): their blocks per rule on pipeline health, and the firewall widgets in api_waf_dashboards.tf."
}

variable "waf_cloudfront_acl" {
  type = object({
    label       = string
    metric_name = string
    log_group   = string # in us-east-1
  })
  default     = null
  description = "The CLOUDFRONT-scope web ACL (metrics and logs in us-east-1), as for waf_regional_acls. Null: left off."
}

variable "feedback_rejections_alarm_threshold" {
  type        = number
  default     = 150
  description = "Rejected feedback submissions in one hour that raise the spam alarm. Half the default daily model-check budget (screening_limit, 300): a real reader rarely has more than a comment or two turned away."
}

variable "aws_region" {
  type        = string
  description = "The deployment's home region (the calling root's var.aws_region): the region every widget reads its metrics and logs from, except the CloudFront ones, which only exist in us-east-1 (local.cloudfront_region). No default, so a root cannot forget to pass it."
}
