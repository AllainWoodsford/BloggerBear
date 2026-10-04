variable "force_destroy" {
  type        = bool
  default     = true
  description = "Whether the site S3 bucket can be destroyed even when non-empty. Dev defaults to true so `terraform destroy` never chokes on a non-empty bucket -- dev is meant to be torn down and rebuilt freely."
}

variable "bedrock_model_id" {
  type = string
  # au.anthropic.claude-sonnet-5 (the original default) turned out not to
  # be enabled for this account -- confirmed the hard way via a real
  # research_tick invocation: AccessDeniedException, "anthropic.claude-
  # sonnet-5 is not available for this account". Switched to the AU Claude
  # Haiku inference profile, confirmed working via the AWS CLI
  # (bedrock-runtime converse --model-id) before landing here. Full ARN
  # (not just the au.anthropic.claude-haiku-4-5-20251001-v1:0 short form)
  # since that's the exact value confirmed to work.
  default     = "arn:aws:bedrock:ap-southeast-2:547610822592:inference-profile/au.anthropic.claude-haiku-4-5-20251001-v1:0"
  description = <<-EOT
    Bedrock model ID or cross-region inference profile ID (or its full
    ARN) the Lambda handlers pass to bedrock:InvokeModel via the Converse
    API (lambdas/common/bedrock.py) -- NOT Anthropic/Claude-specific.
    Converse normalizes the request/response shape across every model
    family Bedrock supports through it (Anthropic, Amazon Nova, Meta,
    Mistral, Cohere, ...), so this can be pointed at any Bedrock-invokable
    model or inference profile without a code change, as long as the
    chosen model/profile is actually enabled for this account (Bedrock
    model access is opt-in per model, requested in the console) and
    reachable via Converse -- confirm with a real
    `aws bedrock-runtime converse --model-id ... --messages ...` call
    before changing this, since neither `terraform plan`/`validate` nor
    this project's test suite can catch a model being unavailable for the
    account. Every Claude model AWS offers directly (non-inference-profile)
    in ap-southeast-2 requires routing through a cross-region inference
    profile rather than on-demand invocation (confirmed via
    `aws bedrock list-foundation-models`); other providers may differ.
  EOT
}

variable "admin_allowed_cidrs" {
  type        = list(string)
  default     = []
  sensitive   = true
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
    repo-level GitHub Actions secret `ADMIN_ALLOWED_CIDRS_DEV` (never in
    the repo). Environment variables are Terraform's
    lowest-precedence value source, so this only works because
    terraform.tfvars doesn't also set this variable -- if it did, the
    tfvars value would silently win and the CI-supplied one would be
    ignored. A local `terraform apply` without that env var set falls back
    to this variable's `[]` default (fail closed), same as before.

    sensitive = true matters as much as the secret: GitHub masks only the
    secret's exact text, and a plan prints each list element separately,
    so before this the bare IP appeared in apply logs.
  EOT
}

variable "alert_email" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    Email address subscribed to the Phase 6 pipeline-health SNS topic
    (module.observability). Left empty by default -- alarms are created
    and fire either way, this only controls whether a human gets
    notified. AWS SNS requires confirming the subscription (a
    confirmation email/link) before it goes active. CI supplies it as
    TF_VAR_alert_email from the `ALERT_EMAIL_DEV` secret; never put it in
    terraform.tfvars (that commits it). Sensitive, so plans print
    (sensitive value) instead of the address.
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

variable "coingecko_api_plan" {
  type        = string
  default     = "demo"
  description = "Which CoinGecko key type the SSM parameter holds: \"demo\" (free key) or \"pro\" (paid key, uses CoinGecko's pro host). Ignored while there is no parameter."

  validation {
    condition     = contains(["demo", "pro"], var.coingecko_api_plan)
    error_message = "coingecko_api_plan must be \"demo\" or \"pro\"."
  }
}

variable "ops_assistant_mfa" {
  type        = string
  default     = "OPTIONAL"
  description = <<-EOT
    MFA on the operator's assistant's user pool (module.ops_assistant): "OFF", "OPTIONAL" or "ON".
    OPTIONAL in dev: the operator's own user can have an authenticator app, and the judges' login,
    which several people must be able to use from the testing instructions alone, can go without.
    Production's pool will require it ("ON").
  EOT
}
