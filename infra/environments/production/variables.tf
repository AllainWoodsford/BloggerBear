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

# TODO: set before first production apply -- see
# docs/specs/phase-0-foundations.md "Open questions" (domain name and
# registrar not yet decided/registered).
variable "domain_name" {
  type        = string
  default     = ""
  description = "Custom domain for the production site. Required (non-empty) before the first production apply, since enable_custom_domain = true for this environment."
}

variable "hosted_zone_id" {
  type        = string
  default     = ""
  description = "Route 53 hosted zone ID that domain_name lives in. Required (non-empty) before the first production apply."
}
