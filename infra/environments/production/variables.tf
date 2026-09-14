variable "bedrock_model_id" {
  type        = string
  default     = ""
  description = <<-EOT
    Bedrock model ID (or cross-region inference profile ARN/ID) the Lambda
    handlers pass to bedrock:InvokeModel. Deliberately defaults to an
    empty string rather than a guessed model ID -- per
    docs/project-plan.md §3, which specific Claude model IDs are directly
    invokable in ap-southeast-2 versus which require routing through a
    cross-region inference profile is an open question that must be
    confirmed in the Bedrock console before Phase 1 can run end-to-end.
    Set the real value in terraform.tfvars once that's confirmed.
  EOT
}

variable "admin_allowed_cidrs" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Public IP CIDRs (as /32s, e.g. ["203.0.113.7/32"]) allowed through the
    regional WAF Web ACL in front of the admin API. This MUST be set to
    the operator's own public IP before the admin API becomes reachable at
    all -- with this left empty, the Web ACL's default-block action means
    NOTHING can call the API. That is the deliberately safe default (fail
    closed, consistent with this project's compliance-review posture),
    not a bug. Set the real value in terraform.tfvars.
  EOT
}

# TODO: set before first production apply -- see
# docs/specs/phase-0-foundations.md "Open questions" (domain name and
# registrar not yet decided/registered).
variable "domain_name" {
  type        = string
  default     = ""
  description = "Custom domain for the production site. Required (non-empty) before the first production apply, since enable_custom_domain = true for this environment."
}

variable "alert_email" {
  type        = string
  default     = ""
  description = <<-EOT
    Email address subscribed to the Phase 6 pipeline-health SNS topic
    (module.observability). Left empty by default -- alarms are created
    and fire either way, this only controls whether a human gets
    notified. AWS SNS requires confirming the subscription (a
    confirmation email/link) before it goes active. Set the real value in
    terraform.tfvars.
  EOT
}

variable "hosted_zone_id" {
  type        = string
  default     = ""
  description = "Route 53 hosted zone ID that domain_name lives in. Required (non-empty) before the first production apply."
}
