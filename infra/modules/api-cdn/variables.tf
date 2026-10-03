variable "environment_name" {
  type        = string
  description = "Short environment name (e.g. \"dev\", \"production\"), used to name resources."
}

variable "api_domain" {
  type        = string
  description = "The public API's execute-api hostname (module.public_api.api_domain)."
}

variable "stage_name" {
  type        = string
  description = "The public API's stage name; every request is sent to the origin under /<stage_name>."
}

variable "origin_verify_secret" {
  type        = string
  sensitive   = true
  description = "Sent to the API as the x-origin-verify header, so the regional WAF can tell requests that came through this distribution (and whose x-viewer-ip it can trust) from direct calls."
}

variable "web_acl_id" {
  type        = string
  default     = ""
  description = "ARN of a CLOUDFRONT-scope WAFv2 Web ACL (us-east-1) to associate with the distribution. Empty for none."
}

variable "enable_additional_metrics" {
  type        = bool
  default     = false
  description = "Turn on CloudFront's additional metrics (CacheHitRate and friends) for this distribution. Billed like CloudWatch custom metrics, about US$2.40 a month."
}
