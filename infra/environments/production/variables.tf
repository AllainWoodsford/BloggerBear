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

# -----------------------------------------------------------------------
# Cost saving: the firewall switches (docs/cost-saving.md). Every one defaults to true, which is
# the firewall exactly as it was before the switches existed, and CI passes each one from a
# GitHub variable (WAF_<SWITCH>_<ENV>, else WAF_<SWITCH>, else true: see .github/workflows).
# They are bool, so a typo ("ture") fails the plan with "a bool is required" rather than
# silently picking a mode. Turning one off is for a deployment that only tries the project out:
# a fork that stores real people's data should keep every switch on.
#
# Whatever these say, the admin API's address allowlist (scripts/admin_allowlist.py) is always
# enforced by the API's own resource policy (module.admin_api's resource_policy, free), so no
# switch ever leaves the admin API reachable from another address.
# -----------------------------------------------------------------------
variable "waf_regional_enabled" {
  type        = bool
  default     = true
  description = <<-EOT
    Create the REGIONAL WAF web ACLs in front of this environment's two REST APIs (the public
    API's rate limits and managed rules, the admin API's address allowlist), with their log
    groups, logging and the security-events subscriptions that read them. About US$16 a month
    at list price per environment (2 web ACLs, 6 rules). Off, the APIs keep API Gateway's own
    throttling and the admin API keeps its resource-policy allowlist, but lose the per-visitor
    rate limits, the AWS managed rule set and the firewall's logs, dashboards and incidents.
    Read docs/cost-saving.md before turning it off. Default true: the firewall as it has always
    been.
  EOT
}

variable "waf_admin_allowlist" {
  type        = bool
  default     = true
  description = <<-EOT
    Keep the admin API's WAF web ACL (its address allowlist as a firewall rule, an IP set and
    the admin WAF log group) as a second layer on top of the API's always-on resource policy.
    About US$6 a month at list price. Off, the allowlist is enforced by the resource policy
    alone (the same addresses, from the same secret) and refused calls show in the API's
    access log rather than in a WAF log. Ignored when waf_regional_enabled is false. Read
    docs/cost-saving.md before turning it off. Default true.
  EOT
}

variable "waf_log_all_requests" {
  type        = bool
  default     = true
  description = <<-EOT
    Have the admin API's WAF web ACL log every request it sees (allowed ones included: only
    the operator's addresses get through, and a full record of that is the audit trail). Off,
    its log keeps only what a rule blocked or counted, like the visitor ACLs' logs already do,
    which saves CloudWatch Logs ingestion on the Hosting line. Nothing else changes. Read
    docs/cost-saving.md for what the full log is for. Default true.
  EOT
}

variable "waf_site_enabled" {
  type        = bool
  default     = true
  description = <<-EOT
    Create the shared CLOUDFRONT-scope web ACL (aws_wafv2_web_acl.this: the site's and the
    public API CDN's per-visitor rate limit and the AWS managed common rule set, with its
    us-east-1 log group) and attach it to both distributions. About US$7 a month at list price.
    Off, the distributions run without a web ACL and keep only CloudFront's and Shield
    Standard's own protections; dev's web_acl_arn must then be left empty, and the
    wafv2_web_acl_arn output is null. Read docs/cost-saving.md before turning it off. Default
    true. Production only: dev never owns this ACL.
  EOT
}

# TODO: set before first production apply -- see
# docs/enhancements/archive/phase-0-foundations.md "Open questions" (domain name and
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
    Design: docs/enhancements/archive/opencv-agentic-vision-enhancement.md.
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

variable "backup_bucket_name" {
  type        = string
  default     = ""
  description = <<-EOT
    The bucket backups are kept in (lambdas/common/backup.py): the bootstrap state bucket, under
    backup/<prefix>-<env>/. Empty means the backend block's default, bloggerbear-terraform-state;
    CI passes the TF_STATE_BUCKET_* secret, the same name the backend is initialised with.
  EOT
}

variable "backup_export_buckets" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Buckets besides the backup bucket that `admin_cli backup export --bucket` may copy articles
    into, and `backup restore --bucket` may read a snapshot from. Each is granted to the Lambda role
    (GetObject/PutObject on its objects); the bucket itself is made and kept outside Terraform.
  EOT
}

variable "reviewer_enabled" {
  type        = bool
  default     = true
  description = <<-EOT
    Whether to deploy Professor Grizzlewick, the reviewer bear (lambdas/reviewer_handler.py): every
    second day he reviews held and newly published articles, fixes content findings, and writes to
    the musings feed. On by default; false deploys neither his function nor his schedule. Set from
    the REVIEWER_ENABLED GitHub variable. His behaviour (tasks, limits, pause flag, whether he may
    approve) is the `reviewer` row of the model-config table, changed with no deploy. Design:
    docs/enhancements/archive/reviewer-bear.md.
  EOT
}

variable "web_search_region" {
  type        = string
  default     = "ap-northeast-1"
  description = "Where the AgentCore web search gateway runs (infra/modules/web-search). The Web Search Tool is only offered in us-east-1, eu-west-1 and ap-northeast-1 (Tokyo, the closest to Sydney, and what an unset WEB_SEARCH_REGION GitHub variable gives). Must be one of the bootstrap's web_search_regions, or the apply is refused (docs/configuration.md)."

  validation {
    # AWS offers the Web Search Tool in these three Regions only.
    condition     = contains(["us-east-1", "eu-west-1", "ap-northeast-1"], var.web_search_region)
    error_message = "web_search_region must be one of the Web Search Tool's regions: us-east-1, eu-west-1 or ap-northeast-1."
  }
}

variable "evals_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    Whether continuous evals run (docs/enhancements/model-ranch-prompt-assets-evals.md): the scored
    events the pipeline writes, the hourly rollup of them into the evals table, the judge and the
    offline scoring runs. Off by default. Handed to every Lambda as EVALS_ENABLED ("true" or
    "false"); nothing reads it until the evals module is added. Set from the EVALS_ENABLED GitHub
    variable (docs/configuration.md).
  EOT
}
