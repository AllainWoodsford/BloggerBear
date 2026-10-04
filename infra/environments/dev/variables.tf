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

    Only when dev and production are in the SAME AWS account: a CloudFront distribution can only
    use a web ACL owned by its own account (AWS WAF has no cross-account association), so a
    two-account deployment leaves this empty and dev's two distributions go without the shared
    ACL. The regional ACLs in front of dev's APIs are dev's own and are not affected. See
    docs/deploying-your-own.md.
  EOT

  # Catches the two-account mistake at plan time, when var.aws_account_id says which account this
  # is. Without it the apply fails later, at CloudFront, with a much less obvious message.
  validation {
    condition     = var.web_acl_arn == "" || var.aws_account_id == "" || try(split(":", var.web_acl_arn)[4], "") == var.aws_account_id
    error_message = "web_acl_arn names a web ACL in a different AWS account from aws_account_id. CloudFront can only use a web ACL in its own account: leave web_acl_arn empty when dev and production are in separate accounts."
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

variable "ops_alexa_redirect_uris" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Alexa's account-linking redirect URLs for dev's Alexa+ add-on, as `alexa-ai
    configure-account-linking` prints them (alexa/README.md). Empty (the default): no Alexa app
    client, and no Alexa account can be linked to dev. Never production's URLs: each environment's
    add-on is its own, and links only to its own user pool.
  EOT
}

variable "aws_account_id" {
  type        = string
  default     = ""
  sensitive   = true
  description = <<-EOT
    The 12-digit ID of the AWS account dev is meant to be applied to. When set, every AWS
    provider in this root refuses to run against any other account (allowed_account_ids), so
    credentials for the wrong account fail at the first step instead of creating half a deployment
    somewhere unexpected. Empty (the default) checks nothing, which is how this ran before the
    variable existed.

    CI supplies it as TF_VAR_aws_account_id from the repo-level `AWS_DEV_ACCOUNT_ID` secret
    (.github/workflows/terraform.yml and destroy-dev.yml). An account ID is an identifier, not a
    credential, but this repo keeps them out of its public logs all the same, and GitHub masks a
    secret's text wherever it would be printed. Do not set it in terraform.tfvars: that commits it.

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

variable "unique_name_suffix" {
  type        = string
  default     = ""
  description = <<-EOT
    Added to the end of the three names in this environment that must be unique across every AWS
    account, not just this one: the content bucket (bloggerbear-dev-content), the site bucket
    (bloggerbear-dev-site) and the operator's assistant's Cognito sign-in host
    (bloggerbear-dev-ops). A second deployment of this project, in another account, cannot create
    any of them under the plain name while the first deployment exists, so a fork sets this to
    something of its own, such as "-yourname".

    Empty (the default) keeps the plain names. NEVER change this on a deployment that already
    exists: a bucket cannot be renamed, so Terraform would destroy it and create a new, empty one.

    CI supplies it as TF_VAR_unique_name_suffix from the repo-level `UNIQUE_NAME_SUFFIX` variable (a
    variable, not a secret: the suffix ends up in public bucket and sign-in host names anyway).
  EOT

  validation {
    condition     = var.unique_name_suffix == "" || can(regex("^[a-z0-9-]{0,19}[a-z0-9]$", var.unique_name_suffix))
    error_message = "unique_name_suffix must be empty, or up to 20 lowercase letters, digits and hyphens ending in a letter or digit (it becomes part of S3 bucket names), such as \"-yourname\"."
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
    from the AWS_REGION GitHub Actions variable (docs/deploying-your-own.md).

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
