variable "aws_region" {
  type        = string
  default     = "ap-southeast-2"
  description = "Region for the bootstrap resources (state bucket, OIDC provider, IAM roles). Bootstrap is applied once, locally, by a human -- never through CI."
}

variable "state_bucket_name" {
  type        = string
  default     = "bloggerbear-terraform-state"
  description = <<-EOT
    Name of the S3 bucket used as the Terraform state backend for every
    environment in this project. S3 bucket names are globally unique across
    ALL AWS accounts, not just this one -- confirm this exact name is
    actually available (e.g. `aws s3api head-bucket --bucket
    bloggerbear-terraform-state`, expecting a 404/NoSuchBucket error) BEFORE
    running the one-time bootstrap apply.

    If it collides, change this default to something unique (e.g. append
    your account ID or a random suffix) -- but you MUST then copy-paste
    that exact literal string into the `backend "s3" { bucket = "..." }`
    block in BOTH infra/environments/dev/main.tf and
    infra/environments/production/main.tf, since Terraform backend blocks
    cannot reference variables or interpolation of any kind.
  EOT
}

variable "github_repo" {
  type        = string
  default     = "AllainWoodsford/BloggerBear"
  description = "GitHub <owner>/<repo> slug allowed to assume the deploy roles via OIDC."
}

variable "domain_name" {
  type        = string
  default     = "bloggerbear.com"
  description = <<-EOT
    The site's domain (e.g. "bloggerbear.com"). When set, this creates the Route 53 hosted zone for it
    HERE, not in an environment, on purpose: the zone's four name servers are what you type into your
    registrar (GoDaddy), and they must not change when production is destroyed and rebuilt. A zone that
    lived in the production environment would be deleted with it and come back with different name
    servers, which means changing the registrar again. Empty (the default) creates nothing.

    The zone has prevent_destroy, so removing it takes a deliberate edit, not a stray apply.
  EOT

  validation {
    condition     = var.domain_name == "" || can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a bare lowercase domain such as bloggerbear.com: no https://, no www, no trailing dot."
  }
}

variable "bedrock_budget_limit_usd" {
  type        = string
  default     = "20"
  description = "Monthly USD threshold for the Phase 6 Bedrock-spend budget alarm (aws_budgets_budget.bedrock_spend below). See docs/PROGRESS.md's cost-tapering section for context on what levels are reasonable for a single-operator project."
}

variable "budget_alert_email" {
  type        = string
  default     = ""
  description = <<-EOT
    Email address notified when Bedrock spend crosses 80% (actual) or is
    forecast to cross 100% (forecasted) of var.bedrock_budget_limit_usd
    for the current month. Left empty by default -- no aws_budgets_budget
    resource is created at all until this is set, since AWS Budgets
    requires at least one notification subscriber; that's the safe
    default (nothing half-configured), not a bug.

    This is IN ADDITION to the general AWS Budget alarm already called
    out as a manual prerequisite in docs/PROGRESS.md (that one watches
    total account spend across every service). This one is scoped
    specifically to the Amazon Bedrock service line item, per
    docs/PROGRESS.md's Phase 6 scope ("Cost/budget alarms specifically
    watching Bedrock spend") -- Bedrock is the one service this project's
    own cost section calls out as the likely biggest and most variable
    line item, since spend scales with how much content actually gets
    generated. Set the real value before running the one-time bootstrap
    apply.
  EOT
}
