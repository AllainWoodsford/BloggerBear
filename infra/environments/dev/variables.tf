variable "force_destroy" {
  type        = bool
  default     = true
  description = "Whether the site S3 bucket can be destroyed even when non-empty. Dev defaults to true so `terraform destroy` never chokes on a non-empty bucket -- dev is meant to be torn down and rebuilt freely."
}

variable "bedrock_model_id" {
  type        = string
  default     = ""
  description = <<-EOT
    Bedrock model ID or cross-region inference profile ID the Lambda
    handlers pass to bedrock:InvokeModel via the Converse API
    (lambdas/common/bedrock.py) -- NOT Anthropic/Claude-specific. Converse
    normalizes the request/response shape across every model family Bedrock
    supports through it (Anthropic, Amazon Nova, Meta, Mistral, Cohere,
    ...), so this can be pointed at any Bedrock-invokable model or
    inference profile ID without a code change, e.g.
    "au.anthropic.claude-sonnet-5" or an Amazon Nova Pro inference profile
    ID, as long as the chosen model/profile is actually enabled for this
    account (Bedrock model access is opt-in per model, requested in the
    console) and reachable via Converse. Deliberately defaults to an empty
    string rather than a guessed model ID -- per docs/project-plan.md §3,
    every Claude model AWS offers in ap-southeast-2 requires routing
    through a cross-region inference profile rather than direct on-demand
    invocation (confirmed via `aws bedrock list-foundation-models`); other
    providers may differ. Set the real value in terraform.tfvars once
    confirmed.
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
    not a bug.

    Deliberately NOT set in terraform.tfvars -- a real home/office IP
    checked into git history is a personal-information leak (and stays
    leaked even if later removed/rotated) that also goes stale the moment
    the operator's IP changes. Supplied instead as a `TF_VAR_
    admin_allowed_cidrs` environment variable in this repo's
    .github/workflows/terraform.yml apply-dev job, sourced from a
    repo-level GitHub Actions secret `ADMIN_ALLOWED_CIDRS_DEV` (masked in
    logs, never in the repo). Environment variables are Terraform's
    lowest-precedence value source, so this only works because
    terraform.tfvars doesn't also set this variable -- if it did, the
    tfvars value would silently win and the CI-supplied one would be
    ignored. A local `terraform apply` without that env var set falls back
    to this variable's `[]` default (fail closed), same as before.
  EOT
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

variable "web_acl_arn" {
  type        = string
  default     = ""
  description = <<-EOT
    ARN of the shared WAFv2 Web ACL created in
    infra/environments/production (CLOUDFRONT scope, us-east-1). Dev does
    not own WAF creation -- one ACL is shared across both environments
    rather than creating one each. Leave empty until production's WAF ACL
    has been applied at least once; then set this in terraform.tfvars so
    dev's distribution shares the same ACL as production. This is a manual
    follow-up step, not automated.
  EOT
}
