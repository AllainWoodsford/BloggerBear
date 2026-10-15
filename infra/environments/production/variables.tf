variable "bedrock_model_id" {
  type = string
  # au.anthropic.claude-sonnet-5 (the original default) turned out not to
  # be enabled for this account -- confirmed the hard way via a real
  # research_tick invocation on dev: AccessDeniedException, "anthropic.
  # claude-sonnet-5 is not available for this account". Switched to the
  # AU Claude Haiku inference profile, confirmed working via the AWS CLI
  # (bedrock-runtime converse --model-id) before landing here. Full ARN
  # (not just the au.anthropic.claude-haiku-4-5-20251001-v1:0 short form)
  # since that's the exact value confirmed to work.
  #
  # Empty now means "that same profile, in the account being applied to": see
  # local.bedrock_model_id in main.tf, which builds the ARN from the caller's account. The ARN
  # used to be written out here with one account's ID in it, so a fork in its own account handed
  # its Lambdas a profile it could never call.
  default     = ""
  description = <<-EOT
    Leave empty for the default: the AU Claude Haiku 4.5 inference profile in the account
    being applied to (local.bedrock_model_id in main.tf). Otherwise, the
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
    in Sydney (the default home region) requires routing through a cross-region inference
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
    admin_allowed_cidrs` environment variable in CI (see
    .github/workflows/terraform.yml's apply-dev job and
    terraform-production-release.yml), sourced from a GitHub Actions
    secret (never in the repo) -- a repo-level secret
    `ADMIN_ALLOWED_CIDRS_DEV` for dev, a `production`-Environment-scoped
    secret `ADMIN_ALLOWED_CIDRS_PROD` for production. Environment variables
    are Terraform's lowest-precedence value source, so this only works
    because terraform.tfvars doesn't also set this variable -- if it did,
    the tfvars value would silently win and the CI-supplied one would be
    ignored. A local `terraform apply` without that env var set falls back
    to this variable's `[]` default (fail closed), same as before.

    sensitive = true matters as much as the secret: GitHub masks only the
    secret's exact text, and a plan prints each list element separately,
    so before this the bare IP appeared in apply logs.
  EOT
}

# TODO: set before first production apply -- see
# docs/specs/phase-0-foundations.md "Open questions" (domain name and
# registrar not yet decided/registered).
variable "domain_name" {
  type        = string
  default     = ""
  description = "Custom domain for the production site, e.g. bloggerbear.com (bare: no https://, no www, no trailing dot). Required (non-empty) before the first production apply, since enable_custom_domain = true for this environment. www.<domain_name> is served too, and redirects here."

  validation {
    condition     = var.domain_name == "" || can(regex("^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a bare lowercase domain such as bloggerbear.com: no https://, no www, no trailing dot."
  }
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
    TF_VAR_alert_email from the `ALERT_EMAIL_PROD` secret; never put it in
    terraform.tfvars (that commits it). Sensitive, so plans print
    (sensitive value) instead of the address.
  EOT
}

variable "hosted_zone_id" {
  type        = string
  default     = ""
  description = "Route 53 hosted zone ID that domain_name lives in (looks like Z0123456789ABC). Required (non-empty) before the first production apply. Created by infra/bootstrap: `terraform -chdir=infra/bootstrap output hosted_zone_id`."

  validation {
    condition     = var.hosted_zone_id == "" || can(regex("^Z[A-Z0-9]+$", var.hosted_zone_id))
    error_message = "hosted_zone_id must look like Z0123456789ABC. Get it from `terraform -chdir=infra/bootstrap output hosted_zone_id`."
  }
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

variable "aws_account_id" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    The 12-digit ID of the AWS account production is meant to be applied to. When set, every AWS
    provider in this root refuses to run against any other account (allowed_account_ids), so
    credentials for the wrong account fail at the first step instead of creating half a deployment
    somewhere unexpected. Empty (the default) checks nothing, which is how this ran before the
    variable existed.

    CI supplies it as TF_VAR_aws_account_id from the `AWS_PROD_ACCOUNT_ID` secret, on the
    `production` environment or the repo (.github/workflows/terraform-production-release.yml). An
    account ID is an identifier, not a credential, but this repo keeps them out of its public logs
    all the same, and GitHub masks a secret's text wherever it would be printed. Do not set it in
    terraform.tfvars: that commits it.

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

variable "unique_name_prefix" {
  type        = string
  default     = "bloggerbear"
  description = <<-EOT
    What every resource name in this environment starts with: <prefix>-production-<resource>, for
    example bloggerbear-production-topics. Write it without a trailing hyphen; the names add it. It is
    also the first part of the SSM parameter paths (/<prefix>/production/...), and every Lambda is told
    it as NAME_PREFIX.

    A second deployment of this project (a fork, in its own account) must set its own: S3 bucket
    names (<prefix>-production-content, <prefix>-production-site) and the operator's assistant's Cognito
    sign-in host (<prefix>-production-ops) are unique across every AWS account, so the default is
    taken while the original deployment exists. It must be the same value infra/bootstrap was
    applied with (-var="unique_name_prefix=..."): the deploy role may only create resources whose
    names start with it.

    NEVER change this on a deployment that already exists. Every name changes with it, and a
    bucket or a table cannot be renamed: Terraform would destroy each one and create a new, empty
    one.

    At most 14 characters. The name that sets the limit is a topic's EventBridge Scheduler
    schedule in production, <prefix>-production-<topic_id>-research-tick: Scheduler allows 64
    characters, the fixed parts take 26, and 14 leaves 24 for the topic id, the longest one the
    project's own examples use (finance-crypto-investing). A shorter prefix leaves more: the
    default's 11 characters leave 27. Every other name has room to spare at 14: the longest IAM
    role, <prefix>-production-ops-mcp-scheduler-invoke, is 50 of 64; the longest Lambda function,
    <prefix>-production-cost-explorer-poll, 44 of 64; the content bucket, 33 of 63.

    CI supplies it as TF_VAR_unique_name_prefix from the repo-level `UNIQUE_NAME_PREFIX` variable
    (a variable, not a secret: the prefix ends up in public bucket and sign-in host names anyway),
    falling back to "bloggerbear" when that is not set.
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

variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
  # Deliberately not sensitive: a region is not a secret, it is in every ARN and host name this
  # root outputs, and marking it sensitive would hide those in every plan.
  description = <<-EOT
    The deployment's home region: where everything this root creates lives, except the few
    things AWS only hosts in us-east-1 (CloudFront's certificate and its web ACL, which keep
    that region written out beside a comment saying why). The default is the original
    deployment's region, so leaving it unset changes nothing. CI passes it as TF_VAR_aws_region
    from the AWS_REGION GitHub Actions variable (docs/deployment-runsheet.md).

    Not a setting to change on a deployment that already exists: AWS cannot move a resource
    between regions, so a different value here plans to create everything again somewhere else.
    A deployment in another geography must also set var.bedrock_inference_profile_id, and the
    bootstrap root must have been applied with the same aws_region (its deploy roles' permissions
    are scoped to it).
  EOT

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))
    error_message = "aws_region must look like an AWS region, e.g. eu-west-1 or us-west-2."
  }
}

variable "bedrock_inference_profile_id" {
  type    = string
  default = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
  # No validation of the geography prefix, on purpose: which geographies exist, and which models
  # each one carries, is AWS's list and it grows. A wrong value is not caught by plan or
  # validate; it shows up as an AccessDenied or ValidationException the first time a Lambda
  # calls the model.
  description = <<-EOT
    The inference profile the Lambdas call when var.bedrock_model_id is left empty, as the
    profile's id alone (local.bedrock_model_id in main.tf builds the full ARN around it from
    var.aws_region and the account being applied to). The default is the AU Claude Haiku 4.5
    profile, which only exists in Australian regions.

    A deployment whose var.aws_region is in another geography MUST set this to that geography's
    profile for the same model (the same id with us., eu., apac., ... or global. in place of
    au.; `aws bedrock list-inference-profiles --region <region>` lists the ones on offer), and
    the model must be enabled for the account there. Ignored when var.bedrock_model_id is set.
  EOT
}

variable "ops_alexa_redirect_uris" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Alexa's account-linking redirect URLs for production's Alexa+ add-on, as `alexa-ai
    configure-account-linking` prints them (alexa/README.md). Empty (the default): no Alexa app
    client, and no Alexa account can be linked to production. Never dev's URLs: each environment's
    add-on is its own, and links only to its own user pool. Setting it also keeps the MCP
    function warm (module.ops_assistant's keep_warm).
  EOT
}

variable "vision_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether to deploy the vision worker (infra/modules/vision-worker): OpenCV 5 on arm64 in
    var.vision_region, invoked by the satellite_vision adapter. Off: dev runs it first. Before
    turning it on, the bootstrap must have been re-applied by hand with the same vision_region
    (its VisionWorker statements), or the apply is refused.
    Design: docs/enhancements/opencv-agentic-vision-enhancement.md.
  EOT
}

variable "vision_region" {
  type        = string
  default     = "us-west-2"
  description = "Where the vision worker runs: us-west-2 is where the Sentinel-2 COGs are. Must be the vision_region infra/bootstrap was applied with."

  validation {
    condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.vision_region))
    error_message = "vision_region must look like an AWS region, e.g. us-west-2."
  }
}
