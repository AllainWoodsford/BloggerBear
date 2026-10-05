variable "aws_region" {
  type        = string
  default     = "ap-southeast-2"
  description = "Region for the bootstrap resources (state bucket, OIDC provider, IAM roles), and the region the deploy roles' permissions are scoped to: it must be the same as the environments' aws_region (the AWS_REGION GitHub variable), or their applies are refused. Not sensitive. Bootstrap is applied once, locally, by a human -- never through CI."

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must look like an AWS region, e.g. eu-west-1 or us-west-2."
  }
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

    If it collides (it always does for a fork: the original deployment
    holds this name), pass something unique with
    `-var="state_bucket_name=..."`. The environments' `backend "s3"` blocks
    cannot reference variables, so they still say
    "bloggerbear-terraform-state"; CI is told the real name through the
    GitHub Actions secrets TF_STATE_BUCKET_DEV and TF_STATE_BUCKET_PROD
    (it runs `terraform init -backend-config="bucket=..."` when they are
    set), and a local init needs the same `-backend-config` flag. See
    docs/deployment-runsheet.md.
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
    registrar, and they must not change when production is destroyed and rebuilt. A zone that
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

variable "aws_account_id" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    The 12-digit ID of the AWS account this bootstrap is meant to be applied to. When set, every AWS
    provider in this root refuses to run against any other account (allowed_account_ids), so
    credentials for the wrong account fail at the first step instead of creating half a deployment
    somewhere unexpected. Empty (the default) checks nothing, which is how this ran before the
    variable existed.

    Bootstrap is applied by hand, so pass it on the command line
    (`-var="aws_account_id=..."`) or as TF_VAR_aws_account_id. With two accounts (one for dev, one
    for production) bootstrap is applied once in each, and this is what stops the second apply
    landing in the first account because the wrong profile was still selected.

    Sensitive, so a plan or an error prints (sensitive value) instead of the ID. GitHub masks only
    the secret's exact text; this covers the places Terraform would print it itself. Nothing but
    the provider blocks (and, in dev, web_acl_arn's check) reads it, and no output may expose it.
    If the account check fails, the provider's error names the account the credentials really
    belong to, which is the one you did not expect.
  EOT

  validation {
    condition     = var.aws_account_id == "" || can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "aws_account_id must be empty or exactly 12 digits, with no spaces or dashes."
  }
}
