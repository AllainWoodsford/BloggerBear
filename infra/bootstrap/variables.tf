variable "aws_region" {
  type        = string
  default     = "ap-southeast-2"
  description = "Region for the bootstrap resources (state bucket, OIDC provider, IAM roles), and the region the deploy roles' permissions are scoped to: it must be the same as the environments' aws_region (the AWS_REGION GitHub variable), or their applies are refused. Not sensitive. Bootstrap is applied once, locally, by a human -- never through CI."

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must look like an AWS region, e.g. eu-west-1 or us-west-2."
  }
}

variable "unique_name_prefix" {
  type        = string
  default     = "bloggerbear"
  description = <<-EOT
    What every resource name in the deployment starts with (<prefix>-<env>-<resource>), without a
    trailing hyphen. Here it names the two deploy roles (gha-<prefix>-dev-deploy,
    gha-<prefix>-prod-deploy) and is every name scope in their policy: they may create, change
    and delete only resources named <prefix>-*.

    It must be the same value the environments are deployed with (their unique_name_prefix, which
    CI takes from the `UNIQUE_NAME_PREFIX` GitHub Actions variable). If the two differ, every
    apply is refused: the roles would be scoped to names the environments never use. A fork sets
    its own (the default is taken, see infra/environments/dev/variables.tf) and passes the same
    word in both places. Changing it later renames the deploy roles, so the GitHub secrets
    AWS_DEV_DEPLOY_ROLE_ARN and AWS_PROD_DEPLOY_ROLE_ARN have to be set again.

    At most 14 characters. The name that sets the limit is a topic's EventBridge Scheduler
    schedule in production, <prefix>-production-<topic_id>-research-tick: Scheduler allows 64
    characters, the fixed parts take 26, and 14 leaves 24 for the topic id, the longest one the
    project's own examples use (finance-crypto-investing). A shorter prefix leaves more: the
    default's 11 characters leave 27. Every other name has room to spare at 14: the longest IAM
    role, <prefix>-production-ops-mcp-scheduler-invoke, is 50 of 64; the longest Lambda function,
    <prefix>-production-cost-explorer-poll, 44 of 64; the content bucket, 33 of 63.

    The state bucket is not named from this: it has its own variable, state_bucket_name.
  EOT

  validation {
    condition     = can(regex("^[a-z]([a-z0-9-]{0,12}[a-z0-9])?$", var.unique_name_prefix))
    error_message = "unique_name_prefix must be 1 to 14 characters: lowercase letters, digits and hyphens, starting with a letter and not ending with a hyphen (no trailing \"-\": the names add it), such as \"bloggerbear\" or \"acme-blog\"."
  }

  validation {
    condition     = !strcontains(var.unique_name_prefix, "--")
    error_message = "unique_name_prefix must not contain two hyphens in a row (the web search gateway's name does not allow it)."
  }

  validation {
    condition     = !can(regex("aws|amazon|cognito", var.unique_name_prefix))
    error_message = "unique_name_prefix must not contain \"aws\", \"amazon\" or \"cognito\": Cognito refuses a sign-in host name that does, and the operator's assistant's is <prefix>-<env>-ops."
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
