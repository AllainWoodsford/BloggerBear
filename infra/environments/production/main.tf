terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
    # Scaling PR C: the secret the public API's CloudFront distribution sends to the API
    # (random_password.api_origin_verify).
    random = {
      source  = "hashicorp/random"
      version = ">= 3.6"
    }
  }

  backend "s3" {
    bucket       = "bloggerbear-terraform-state"
    key          = "production/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

# Every resource this root creates carries these tags (the provider's default_tags), so the
# account can be filtered by them -- by a person, or by an agent looking for orphaned resources
# (Project = BloggerBear but no ManagedBy) or ones to import (TerraformRoot says which state owns
# it). Resources the app creates at runtime (per-topic schedules) carry ManagedBy = "admin-api"
# instead: they are not Terraform's, and must never be imported into it.
locals {
  default_tags = {
    ManagedBy     = "Terraform"
    Project       = "BloggerBear"
    Environment   = "production"
    TerraformRoot = "infra/environments/production"
  }
}

provider "aws" {
  region = var.aws_region
  # Refuses to plan or apply against any account but var.aws_account_id, when that is set: a
  # run that picked up the wrong credentials stops at once instead of half-working. Unset (the
  # default) is null here, which is the same as not writing the argument at all.
  allowed_account_ids = var.aws_account_id == "" ? null : [var.aws_account_id]
  default_tags {
    tags = local.default_tags
  }
}

# The CloudFront-scope WAF Web ACL and the ACM certificate used by
# CloudFront must both be declared in us-east-1 regardless of hosting
# region -- an AWS platform requirement (CloudFront only reads
# global-scope WAF ACLs and viewer certificates from that region), not a
# mistake. This is the one place in the whole codebase with a genuine
# us-east-1 provider block.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"
  # The same guard as the default provider above: an alias is a provider of its own, and checks
  # nothing unless told to.
  allowed_account_ids = var.aws_account_id == "" ? null : [var.aws_account_id]
  default_tags {
    tags = local.default_tags
  }
}

# -----------------------------------------------------------------------
# Shared WAF Web ACL -- the ONE ACL for both dev and production
# distributions (see infra/environments/dev/terraform.tfvars for how dev
# wires in this ACL's ARN as a manual follow-up). Created here, in
# production, rather than in the reusable module, since it's a singleton
# shared across environments.
# -----------------------------------------------------------------------
resource "aws_wafv2_web_acl" "this" {
  provider = aws.us_east_1

  name = "bloggerbear-shared"
  # No apostrophe/parens here -- aws_wafv2_web_acl's description is
  # validated against a restrictive AWS-side regex
  # (^[\w+=:#@/\-,.][\w+=:#@/\-,.\s]+[\w+=:#@/\-,.]$) that rejects them,
  # confirmed the hard way (ValidationException) on the first real apply.
  description = "Shared WAF Web ACL for BloggerBear CloudFront distributions -- dev and production."
  scope       = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-managed-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-shared-acl"
    sampled_requests_enabled   = true
  }
}

module "static_site" {
  source = "../../modules/static-site"

  aws_region = var.aws_region

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment_name     = "production"
  enable_custom_domain = true
  force_destroy        = false
  domain_name          = var.domain_name
  hosted_zone_id       = var.hosted_zone_id
  redirect_www         = true
  web_acl_id           = aws_wafv2_web_acl.this.arn
  bucket_name_suffix   = var.unique_name_suffix

  # The frontend calls the public API through its CDN (module.public_api_cdn).
  extra_connect_src = [module.public_api_cdn.domain_name]
}

# =========================================================================
# Phase 1 -- app data tables, content storage, and the Lambda pipeline.
# See docs/project-plan.md §3/§5 and infra/modules/app-data for the
# DynamoDB table set; lambdas/ (Python, owned by the application-code
# workstream) for the two handlers packaged below.
# =========================================================================

module "app_data" {
  source = "../../modules/app-data"

  environment_name = "production"
  protect_data     = true
}

# -----------------------------------------------------------------------
# Content bucket -- read/written directly by Lambda via the SDK, never
# served publicly, so no CloudFront/OAC. Same private-bucket pattern
# (ownership controls + public access block) as the static-site module's
# site bucket. force_destroy left at the default (false) -- unlike dev,
# an accidental destroy must not silently delete real content.
#
# AVD-AWS-0132 ("no customer-managed KMS key") ignored deliberately.
# Every bucket/topic/queue in this project uses AWS's default managed-key
# encryption (SSE-S3 / SSE-SNS / SSE-SQS), not a customer-managed KMS
# key -- a cost/complexity trade-off for a single-operator portfolio
# project: each additional CMK is a recurring per-key charge plus
# key-policy/rotation overhead, for a threat model (this operator's own
# AWS account, not shared-tenancy or regulated data) where AWS-managed
# encryption at rest is judged sufficient. Same ignore recurs at every
# other resource this applies to in infra/.
# trivy:ignore:AVD-AWS-0132
resource "aws_s3_bucket" "content" {
  bucket = "bloggerbear-production-content${var.unique_name_suffix}"
}

resource "aws_s3_bucket_ownership_controls" "content" {
  bucket = aws_s3_bucket.content.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "content" {
  bucket = aws_s3_bucket.content.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Every article body lives here, and a bad write or delete would otherwise be permanent. Versioning
# keeps the previous copy; the rule below drops old copies after 30 days so it does not grow forever.
resource "aws_s3_bucket_versioning" "content" {
  bucket = aws_s3_bucket.content.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "content" {
  bucket = aws_s3_bucket.content.id

  # Versioning has to be on before a lifecycle rule can refer to noncurrent versions.
  depends_on = [aws_s3_bucket_versioning.content]

  rule {
    id     = "expire-old-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Cleanup PR: raw source snapshots (research_tick_handler.py's
  # snapshots/{topic_id}/{captured_at}.json, one file per capture, never overwritten) age out
  # here too. 21 days, not the Findings table's 14-day FINDING_TTL_DAYS: daily_cycle_handler.py's
  # fresh-data review reads a Finding's raw_snapshot_s3_key at draft time, and DynamoDB TTL
  # deletion can lag up to ~48h past a Finding's actual expiry -- the extra week is a safety
  # margin over that lag, not a second independent retention decision. Versioning is on here
  # (unlike dev), so this expiration only retires the *current* version to a delete marker; the
  # rule above then expires that noncurrent copy after its own 30 days -- storage is not freed
  # as fast here as in dev, by design (see that rule's own comment).
  rule {
    id     = "expire-old-snapshots"
    status = "Enabled"

    filter {
      prefix = "snapshots/"
    }

    expiration {
      days = 21
    }
  }
}

# -----------------------------------------------------------------------
# Lambda deployment package -- both functions ship from the same zip
# (one `lambdas/` source tree with a shared `common/` package).
#
# Bugfix: this used to zip lambdas/ directly, which meant NONE of
# requirements.txt's third-party dependencies (requests --
# every adapter's HTTP client: common/adapters/github_trending.py,
# hacker_news.py, crypto_feed.py all import requests) ever made it into
# the deployment package -- the Lambda Python 3.11 runtime does not
# include them. Confirmed the hard way: the very first real invocation of
# research_tick (any topic, any adapter) failed at import time with
# "No module named 'requests'", meaning no adapter-based pipeline could
# ever have produced a Finding, regardless of triggering/timing. Never
# caught by the test suite because pytest imports these handlers in an
# environment where requirements.txt (including requirements-dev.txt) IS
# installed -- there's no test that runs against a deployment package
# built the way Terraform actually builds it.
#
# Fixed by staging lambdas/ source + `pip install -t` of its dependencies
# into one combined build directory first (terraform_data.lambda_package
# below), then zipping THAT -- archive_file itself has no way to merge a
# source directory with pip-installed packages into one zip, so the
# staging step happens as a local-exec provisioner. local-exec assumes a
# Linux/bash environment (python3 + pip on PATH) -- true for the CI
# runner (ubuntu-latest) that always performs the real apply, per this
# project's "Terraform apply is never manual/ad hoc" rule.
#
# Bugfix #2: triggers_replace originally hashed requirements.txt + every
# tracked .py file's content, on the reasonable-looking theory that the
# expensive rebuild step should only run when something actually changed.
# That's wrong for this specific case: the "something to skip re-doing"
# is a purely local, on-disk build artifact, but the CI runner
# (ubuntu-latest via GitHub Actions) is a brand new, empty VM on every
# single run -- there is no "previous run's disk" for a content-hash-based
# skip to safely assume still has anything on it. Confirmed the hard way:
# an apply correctly saw (per its OWN remote state) that neither
# requirements.txt nor any handler had changed since the prior apply, so
# it skipped re-running this provisioner entirely -- and then failed with
# "could not archive missing directory", because that prior apply ran on
# a different, now-gone VM. Fixed by making this always re-run
# (triggers_replace keyed on timestamp()), accepting the small, known
# cost that every apply now re-stages and re-installs (a few seconds for
# two small pure-Python packages) even when nothing changed.
resource "terraform_data" "lambda_package" {
  triggers_replace = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    # Bugfix: no interpreter override used to mean Terraform's own
    # platform default -- /bin/sh -c on Unix (fine, this script is plain
    # POSIX sh), but cmd.exe /C on Windows, which cannot parse this
    # script's POSIX syntax at all (confirmed the hard way: a local
    # Windows apply failed with "Environment variable -eu not defined",
    # cmd.exe trying and failing to interpret `set -eu` as its own `set`
    # builtin). A prior attempt to fix this by hardcoding
    # interpreter = ["/bin/bash", "-c"] made things worse, not better: a
    # native Windows Terraform binary can't resolve a bare POSIX absolute
    # path like /bin/bash at all, even from inside a Git Bash shell,
    # since Windows process creation doesn't understand "/"-rooted paths
    # the way Unix does. The actual fix is a *bare command name*,
    # ["bash", "-c"] with no leading path -- this resolves via each
    # platform's normal PATH lookup instead: Git Bash's bash.exe on
    # Windows (already on PATH in any Git Bash session, which a
    # Terraform child process inherits), and /usr/bin/bash on the
    # ubuntu-latest CI runner (both bash's are always on PATH on their
    # respective platforms). Confirmed locally in an isolated throwaway
    # terraform_data resource before touching this one: bare "bash"
    # correctly resolved to Git Bash's bash.exe and ran the script.
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      build_dir="${path.module}/lambda-build/package"
      rm -rf "$build_dir"
      mkdir -p "$build_dir"
      cp -r "${path.module}/../../../lambdas/." "$build_dir/"
      rm -rf "$build_dir/tests" "$build_dir/__pycache__" "$build_dir/.pytest_cache" "$build_dir/.ruff_cache"
      rm -f "$build_dir/requirements.txt" "$build_dir/requirements-dev.txt" "$build_dir/pyproject.toml"
      # Bugfix: plain `python3` is real on the ubuntu-latest CI runner,
      # but on a Windows machine where Python was installed via the `py`
      # launcher (not a standalone python.org installer), `python3`/
      # `python` on PATH resolve to Windows' own App Execution Alias
      # stubs instead -- confirmed the hard way: those "ran" but failed
      # with "Permission denied" (exit 126) the moment pip actually tried
      # to do anything, since the stub isn't a real interpreter. `py -3`
      # is the actual, always-real interpreter on that kind of Windows
      # setup, but doesn't exist at all on Linux -- so try python3 first
      # and only fall back to `py -3` if it's not genuinely runnable,
      # rather than picking one and breaking the other platform.
      if python3 -c "" >/dev/null 2>&1; then
        py_cmd="python3"
      else
        py_cmd="py -3"
      fi
      $py_cmd -m pip install --upgrade --no-cache-dir -r "${path.module}/../../../lambdas/requirements.txt" -t "$build_dir"
    EOT
  }
}

# Bugfix #3: a plain depends_on (as this had before) is NOT enough to make
# Terraform defer reading a data source until apply time -- that only
# happens when the data source's own config references a value that's
# genuinely unknown until apply. Confirmed the hard way: "Archive creation
# error ... could not archive missing directory" during planning, before
# terraform_data.lambda_package's local-exec had run at all. output_path
# below embeds that resource's own id -- always unknown-until-apply now
# that it's forced to replace on every apply (see the always_run comment
# above) -- purely to force this correct ordering; the id itself is
# otherwise meaningless here. One consequence: the Lambda functions below
# show as needing a (harmless, idempotent) code update on every apply as
# a result, not just when the code actually changed -- an acceptable cost
# given the package is genuinely rebuilt every apply anyway.
data "archive_file" "lambdas" {
  type        = "zip"
  source_dir  = "${path.module}/lambda-build/package"
  output_path = "${path.module}/lambda-build/lambdas-${terraform_data.lambda_package.id}.zip"
}

# -----------------------------------------------------------------------
# Shared Lambda execution role -- both handlers read/write the same
# tables, bucket, and model, so one role covers both.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda_exec" {
  name               = "bloggerbear-production-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda_exec" {
  statement {
    sid    = "DynamoDBAppTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:Query",
      "dynamodb:UpdateItem",
      # Bugfix: Scan and DeleteItem were missing even though
      # common/dynamo.py uses both extensively -- table.scan() backs
      # every "list all" read (list_topics, list_pending_moderation,
      # list_published_articles, list_prompt_refinements,
      # list_feedback_since, list_all_moderation_items,
      # get_top_voted_articles) and table.delete_item() backs
      # delete_topic. Without these, the admin API's list/delete routes,
      # the entire public API (articles list/detail/RSS all read via
      # list_published_articles), the daily cycle's few-shot/prompt-
      # refinement lookups, weekly_reflection, and trending_digest would
      # all fail closed with AccessDeniedException. Never caught by the
      # test suite because moto's mocked DynamoDB doesn't enforce IAM.
      "dynamodb:Scan",
      "dynamodb:DeleteItem",
      # Scaling PR B: the sharded counters (the week's Stats row, article view counts) are
      # summed from several items, fetched in one BatchGetItem rather than one GetItem each.
      "dynamodb:BatchGetItem",
    ]
    resources = module.app_data.table_arns
  }

  # Scaling PR A: common/dynamo.py Queries the Articles and ModerationQueue tables' global
  # secondary indexes instead of Scanning them. An index has its own ARN (<table>/index/<name>),
  # which the table ARNs above do not cover, and nothing but Query is ever run against one.
  statement {
    sid       = "DynamoDBAppIndexes"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = [for arn in module.app_data.table_arns : "${arn}/index/*"]
  }

  statement {
    sid    = "ContentBucket"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
    ]
    resources = ["${aws_s3_bucket.content.arn}/*"]
  }

  # Static article publishing (docs/project-plan.md §11): lets
  # common/static_pages.py write a rendered article page into the site
  # bucket -- scoped to the articles/ prefix only, not the whole bucket,
  # since the rest of it holds the deployed frontend/ SPA files
  # (aws_s3_object.frontend below) that Lambda has no business touching.
  statement {
    sid       = "SitePublishing"
    effect    = "Allow"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["arn:aws:s3:::${module.static_site.bucket_name}/articles/*"]
  }

  # Unpublishing (admin_api_handler.py's _unpublish_article): deletes a page
  # from the articles/ prefix above and asks CloudFront to drop its cached copy.
  statement {
    sid       = "CloudFrontInvalidation"
    effect    = "Allow"
    actions   = ["cloudfront:CreateInvalidation"]
    resources = ["arn:aws:cloudfront::${data.aws_caller_identity.current.account_id}:distribution/${module.static_site.distribution_id}"]
  }

  # Foundation-model ARNs don't carry an account ID -- this is the exact
  # ARN pattern for on-demand Bedrock model invocation. See
  # var.bedrock_model_id below re: confirming a working model ID in this
  # region before Phase 1 can run end-to-end.
  # See the identical statement + comment in infra/environments/dev/main.tf
  # for why both resource ARNs below are needed (cross-region inference
  # profile required for every Claude model in Sydney, the default home region, and likely
  # other providers too -- var.bedrock_model_id isn't Anthropic-specific).
  statement {
    sid     = "BedrockInvoke"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
  }

  # cost_explorer_poll_handler.py's daily API Gateway spend read. Cost Explorer's
  # GetCostAndUsage does not support resource-level permissions -- AWS requires
  # "*" here, there is no ARN to scope it to.
  statement {
    sid       = "CostExplorerRead"
    effect    = "Allow"
    actions   = ["ce:GetCostAndUsage"]
    resources = ["*"]
  }

  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${var.aws_region}:*:log-group:/aws/lambda/bloggerbear-production-*"]
  }
}

resource "aws_iam_role_policy" "lambda_exec" {
  name   = "bloggerbear-production-lambda-exec"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_exec.json
}

# Phase 4: the public site's URL, used by public_api_handler.py (e.g. to
# build absolute links in rss.xml). Production always enables the custom
# domain path (enable_custom_domain = true in the module.static_site call
# above), so module.static_site.custom_domain_url is always non-null
# here -- unlike dev, which has no custom domain and builds this from the
# bare CloudFront domain instead. Also reused below for the generated
# config.js's window.SITE_URL, so the frontend and the Lambda agree on
# the same value.
locals {
  site_url = module.static_site.custom_domain_url
}

locals {
  lambda_env_variables = {
    TOPICS_TABLE           = module.app_data.topics_table_name
    FINDINGS_TABLE         = module.app_data.findings_table_name
    CANDIDATE_IDEAS_TABLE  = module.app_data.candidate_ideas_table_name
    ARTICLES_TABLE         = module.app_data.articles_table_name
    MODERATION_QUEUE_TABLE = module.app_data.moderation_queue_table_name
    CONTENT_BUCKET         = aws_s3_bucket.content.bucket
    BEDROCK_MODEL_ID       = local.bedrock_model_id
    # Phase 2: lets the admin-api handler invoke the other two pipeline
    # Lambdas on demand (e.g. POST /topics/{topic_id}/trigger). Harmless
    # on research_tick/daily_cycle themselves -- they just never read it.
    #
    # Literal strings, not aws_lambda_function.research_tick.function_name
    # / .daily_cycle.function_name -- those resources' own `environment`
    # blocks consume this same local, so referencing their attributes here
    # would create a dependency cycle. function_name is a fixed literal
    # (not computed), so it's identical either way; keep these in sync
    # with the function_name arguments on aws_lambda_function.research_tick
    # and aws_lambda_function.daily_cycle below.
    RESEARCH_TICK_FUNCTION_NAME = "bloggerbear-production-research-tick"
    DAILY_CYCLE_FUNCTION_NAME   = "bloggerbear-production-daily-cycle"

    # Phase 3: lets admin_api_handler's common/scheduler.py create/update/
    # delete per-topic EventBridge Scheduler schedules at runtime (topics
    # are runtime data -- there's no fixed list for Terraform to enumerate
    # here). RESEARCH_TICK_FUNCTION_ARN and STATE_MACHINE_ARN are the two
    # possible per-topic invocation targets; SCHEDULER_INVOKE_ROLE_ARN is
    # the role EventBridge Scheduler assumes to call them (see
    # aws_iam_role.scheduler_invoke below). ENVIRONMENT_NAME is a literal
    # string, not computed, for the same dependency-cycle reason as the
    # *_FUNCTION_NAME entries above.
    #
    # RESEARCH_TICK_FUNCTION_ARN and STATE_MACHINE_ARN are built from
    # data.aws_caller_identity.current.account_id plus the same fixed
    # literal names used elsewhere (function_name below /
    # aws_sfn_state_machine.daily_cycle's name), rather than referencing
    # aws_lambda_function.research_tick.arn / aws_sfn_state_machine.
    # daily_cycle.arn directly -- the state machine's definition already
    # references aws_lambda_function.daily_cycle.arn, so a direct
    # STATE_MACHINE_ARN = aws_sfn_state_machine.daily_cycle.arn reference
    # here would create daily_cycle -> local.lambda_env_variables ->
    # state_machine -> daily_cycle, a dependency cycle Terraform refuses
    # to plan. data.aws_caller_identity has no such dependency, so this
    # sidesteps the cycle the same way the literal function_name strings
    # do. SCHEDULER_INVOKE_ROLE_ARN has no such issue (scheduler_invoke's
    # own attributes don't depend on any Lambda/state-machine resource) so
    # it's referenced directly.
    RESEARCH_TICK_FUNCTION_ARN = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:bloggerbear-production-research-tick"
    STATE_MACHINE_ARN          = "arn:aws:states:${var.aws_region}:${data.aws_caller_identity.current.account_id}:stateMachine:bloggerbear-production-daily-cycle"
    SCHEDULER_INVOKE_ROLE_ARN  = aws_iam_role.scheduler_invoke.arn
    ENVIRONMENT_NAME           = "production"

    # Phase 4: consumed by public_api_handler.py.
    SITE_URL = local.site_url

    # Phase 5: the weekly reflection job's two new tables (see
    # infra/modules/app-data's aws_dynamodb_table.feedback /
    # prompt_refinements). Consumed by public_api_handler.py (feedback
    # writes) and weekly_reflection_handler.py (reads feedback, writes
    # refinements). aws_iam_role_policy.lambda_exec below already covers
    # both -- its DynamoDB statement is `resources =
    # module.app_data.table_arns`, which now includes these two ARNs
    # automatically, no separate IAM change needed.
    FEEDBACK_TABLE           = module.app_data.feedback_table_name
    PROMPT_REFINEMENTS_TABLE = module.app_data.prompt_refinements_table_name

    # dlq_handler.py's target table (see aws_lambda_function.dlq_handler
    # below) -- harmless on every other Lambda, they just never read it.
    FAILED_EXECUTIONS_TABLE = module.app_data.failed_executions_table_name

    # Static article publishing (docs/project-plan.md §11): where
    # common/static_pages.py writes each rendered article page --
    # module.static_site's EXISTING Phase 0 bucket (see the "Frontend
    # static files" section below), not a new bucket. Consumed by
    # daily_cycle_handler.py and admin_api_handler.py; harmless on every
    # other Lambda, they just never read it.
    SITE_BUCKET = module.static_site.bucket_name

    # Unpublishing: the distribution common/static_pages.py invalidates when an
    # article page is deleted. Read by admin_api_handler.py only; unset means
    # the invalidation is skipped and a cached copy lingers until its TTL.
    CLOUDFRONT_DISTRIBUTION_ID = module.static_site.distribution_id

    # Musings: common/musings.py's target table (article musings, called
    # from daily_cycle_handler.py and admin_api_handler.py) and
    # musing_feedback_handler.py's periodic feedback musings (see
    # aws_lambda_function.musing_feedback below). Harmless on every other
    # Lambda, they just never read it.
    MUSINGS_TABLE = module.app_data.musings_table_name

    # AI lineage/cost-tracking enhancement (docs/project-plan.md §11, PR 1
    # of 5): the "supported models" registry and the single-row global
    # default/fallback model config, both read/written by
    # common/bedrock.py's resolve_model and admin_api_handler.py's
    # /models, /model-config routes. Harmless on every other Lambda.
    MODELS_TABLE       = module.app_data.models_table_name
    MODEL_CONFIG_TABLE = module.app_data.model_config_table_name

    # Observability enhancement, PR 1: Bedrock usage that is not part of any one article's
    # lineage (musings, the weekly reflection, gear identity, comment screening), plus reader
    # activity counters -- see common/stats_tracking.py. Harmless on every other Lambda.
    STATS_CURRENT_TABLE = module.app_data.stats_current_table_name
    STATS_HISTORY_TABLE = module.app_data.stats_history_table_name

    # Scaling PR B: sharded article view counters (common/dynamo.py's increment_view_count /
    # get_view_count), read by the public and admin APIs. Harmless on every other Lambda.
    VIEW_COUNTS_TABLE = module.app_data.view_counts_table_name

    # Security events (common/security_events.py): written by the security-events Lambda and the
    # public API (screened comments). Harmless on every other Lambda.
    SECURITY_EVENTS_TABLE = module.app_data.security_events_table_name

    # The AgentCore web search gateway (module.web_search below): the
    # fallback search backend common/web_search.py uses when GDELT fails,
    # and the "agentcore" provider a topic can ask for directly. Read by
    # every Lambda that searches (research_tick, daily_cycle); harmless on
    # the rest.
    AGENTCORE_WEB_SEARCH_URL    = module.web_search.gateway_url
    AGENTCORE_WEB_SEARCH_REGION = module.web_search.region
    AGENTCORE_WEB_SEARCH_TOOL   = module.web_search.tool_name
  }
}

module "web_search" {
  source = "../../modules/web-search"
  name   = "bloggerbear-production"
}

# Lets the Lambdas call the web search gateway (IAM inbound auth -- see
# infra/modules/web-search). A separate policy on the shared exec role, like
# lambda_invoke_pipeline, so the grant stays visibly scoped to one gateway.
data "aws_iam_policy_document" "lambda_web_search" {
  statement {
    sid       = "InvokeWebSearchGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [module.web_search.gateway_arn]
  }
}

resource "aws_iam_role_policy" "lambda_web_search" {
  name   = "bloggerbear-production-lambda-web-search"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_web_search.json
}

# Used only to construct RESEARCH_TICK_FUNCTION_ARN / STATE_MACHINE_ARN
# above without a direct resource reference (see the comment there for
# why a direct reference would create a dependency cycle).
data "aws_caller_identity" "current" {}

# The model the Lambdas call when var.bedrock_model_id is left empty: the AU Claude Haiku
# inference profile, as a full ARN, in whichever account this is being applied to. The variable's
# default used to be this same ARN with one account's ID written into it, which a deployment in
# any other account could not call. Built from the caller's account instead, it is the same
# string as before for that account, and the right one for every other.
locals {
  bedrock_model_id = var.bedrock_model_id != "" ? var.bedrock_model_id : "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${var.bedrock_inference_profile_id}"
}

# 120s / 512MB (was 60s / 256MB): on the first tick of each UTC day the crypto feed
# makes a markets call plus up to ~10 CoinGecko history calls (with backoff on
# 429s) or a web search, then a Bedrock summary of a much larger state. Later
# ticks reuse that day history and are far cheaper.
resource "aws_lambda_function" "research_tick" {
  function_name = "bloggerbear-production-research-tick"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "research_tick_handler.handler"
  runtime       = "python3.11"
  timeout       = 120
  memory_size   = 512

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = merge(local.lambda_env_variables, local.coingecko_env_variables, local.github_env_variables)
  }
}

# 300s / 512MB (was 120s, and 60s before that): headroom for the sequential Bedrock
# calls (ideate, draft, title, fresh-data review, compliance review) over a larger
# data payload, the review's fetch of current data (time-boxed at 45s), and a
# fallback-model retry if the primary call fails.
# The CoinGecko key lives in SSM Parameter Store as a SecureString, at a fixed name, and only the
# two Lambdas that run the crypto adapter are told where: research_tick (its hourly fetch) and
# daily_cycle (the fresh-data review re-reads current prices). They read it once per cold start
# (common/adapters/crypto_feed.py). It is never in Terraform state, a Lambda's environment or a
# GitHub secret: Terraform doesn't create the parameter -- a managed SecureString's value is read
# back into state on every refresh -- it only grants read access to this one name. The operator
# creates it once (see README.md):
#
#   aws ssm put-parameter --name /bloggerbear/production/coingecko-api-key --type SecureString --value <key> --overwrite
#
# No parameter -> the adapter uses CoinGecko's keyless public API, exactly as with no key before.
locals {
  coingecko_api_key_parameter = "/bloggerbear/production/coingecko-api-key"
  coingecko_env_variables = {
    COINGECKO_API_KEY_PARAMETER = local.coingecko_api_key_parameter
    COINGECKO_API_PLAN          = var.coingecko_api_plan
  }
}

# Read access to that one parameter, on the shared exec role (the Lambdas share one role, so this is
# what "only the crypto Lambdas" can mean here: only they are told the name). SecureString with the
# default aws/ssm key, whose key policy already lets the account's principals decrypt through SSM,
# so no KMS grant is needed.
data "aws_iam_policy_document" "lambda_coingecko_key" {
  statement {
    sid       = "ReadCoinGeckoKey"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.coingecko_api_key_parameter}"]
  }
}

resource "aws_iam_role_policy" "lambda_coingecko_key" {
  name   = "bloggerbear-production-lambda-coingecko-key"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_coingecko_key.json
}

# The optional GitHub API token, the same way: a SecureString at a fixed name that Terraform never
# creates, only grants read access to. The GitHub Trending adapter calls the REST Search API, which
# allows 10 requests a minute per IP unauthenticated (and Lambda's egress IPs are shared); a token,
# fine-grained with no permissions, raises that to 30 on its own budget. research_tick fetches and
# daily_cycle's fresh-data review re-fetches, so both are told where it is
# (common/adapters/github_trending.py). The operator creates it once (see README.md):
#
#   aws ssm put-parameter --name /bloggerbear/production/github-api-token --type SecureString --value <token> --overwrite
#
# No parameter -> unauthenticated search; a token GitHub rejects falls back to unauthenticated too.
locals {
  github_api_token_parameter = "/bloggerbear/production/github-api-token"
  github_env_variables = {
    GITHUB_API_TOKEN_PARAMETER = local.github_api_token_parameter
  }
}

data "aws_iam_policy_document" "lambda_github_token" {
  statement {
    sid       = "ReadGitHubToken"
    effect    = "Allow"
    actions   = ["ssm:GetParameter"]
    resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.github_api_token_parameter}"]
  }
}

resource "aws_iam_role_policy" "lambda_github_token" {
  name   = "bloggerbear-production-lambda-github-token"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_github_token.json
}

resource "aws_lambda_function" "daily_cycle" {
  function_name = "bloggerbear-production-daily-cycle"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "daily_cycle_handler.handler"
  runtime       = "python3.11"
  timeout       = 300
  memory_size   = 512

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = merge(local.lambda_env_variables, local.coingecko_env_variables, local.github_env_variables)
  }
}

# =========================================================================
# Phase 2 -- Admin console API: a third Lambda (from the same shared
# deployment package above) fronted by an IAM-authenticated API Gateway
# HTTP API and a regional WAF IP allowlist. Per docs/PROGRESS.md's Phase 2
# line, this project uses IAM (SigV4) auth + a WAF IP allowlist rather than
# Cognito -- simpler and cheaper for a single operator driving this
# entirely through a local CLI (scripts/admin_cli.py, Python workstream),
# never a browser app. This is a separate, REGIONAL-scope Web ACL from the
# CLOUDFRONT-scope aws_wafv2_web_acl.this above -- that one protects the
# CloudFront distributions; this one protects only this API.
# =========================================================================

resource "aws_lambda_function" "admin_api" {
  function_name = "bloggerbear-production-admin-api"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "admin_api_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Second inline policy on the same shared exec role (rather than folding
# into aws_iam_role_policy.lambda_exec above) so this grant stays visibly
# scoped to exactly the two pipeline Lambda ARNs -- deliberately NOT
# aws_lambda_function.admin_api.arn (no self-invoke) and NOT "*".
data "aws_iam_policy_document" "lambda_invoke_pipeline" {
  statement {
    sid       = "InvokePipelineLambdas"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.research_tick.arn, aws_lambda_function.daily_cycle.arn]
  }
}

resource "aws_iam_role_policy" "lambda_invoke_pipeline" {
  name   = "bloggerbear-production-lambda-invoke-pipeline"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_invoke_pipeline.json
}

# -----------------------------------------------------------------------
# Admin API -- a REST API (v1), not the simpler/cheaper HTTP API (v2) this
# started as. AWS WAFv2 cannot associate with HTTP APIs at all (only REST
# APIs, ALB, AppSync, Cognito, App Runner, Verified Access) -- discovered
# the hard way when the first real apply's WAF association failed with
# "The ARN isn't valid" against an apigatewayv2 stage ARN, not a
# permissions problem. See infra/modules/rest-api's own header comment
# for the full migration rationale. authorization = "AWS_IAM" on every
# route is what enforces SigV4 auth. Reachability is further restricted
# to the operator's own IP by the regional WAF Web ACL below (now
# actually attachable, which is the entire point of this being a REST
# API instead of HTTP API).
#
# One route per admin_cli.py operation -- keep this set and
# scripts/admin_cli.py's routes in sync.
# -----------------------------------------------------------------------
module "admin_api" {
  source = "../../modules/rest-api"

  aws_region = var.aws_region

  name                 = "bloggerbear-production-admin-api"
  stage_name           = "production"
  lambda_invoke_arn    = aws_lambda_function.admin_api.invoke_arn
  lambda_function_name = aws_lambda_function.admin_api.function_name
  authorization        = "AWS_IAM"
  web_acl_id           = aws_wafv2_web_acl.admin.arn
  associate_web_acl    = true

  # Scaling PR C: one operator's CLI and review inbox never come close to this; it only caps
  # what a leaked credential or a runaway script could make the admin Lambda do.
  throttling_rate_limit  = 10
  throttling_burst_limit = 20

  routes = toset([
    "GET /topics",
    "POST /topics",
    "GET /topics/{topic_id}",
    "PUT /topics/{topic_id}",
    "DELETE /topics/{topic_id}",
    "POST /topics/{topic_id}/trigger",
    "GET /topics/{topic_id}/candidates",
    # research_tick's trigger is fire-and-forget (async Lambda invoke) --
    # this is what scripts/admin_cli.py's `topics trigger` polls to find
    # out when it's actually finished, since there's no dedicated job-
    # status system. See admin_api_handler.py's _get_latest_finding_route.
    "GET /topics/{topic_id}/findings/latest",
    # Force-publish override -- publishes an article regardless of its
    # current status, unlike the moderation approve/reject routes below
    # which only act on a pending ModerationQueue item. See
    # admin_api_handler.py's _publish_article and scripts/admin_cli.py's
    # `articles publish` subcommand.
    "POST /articles/{article_id}/publish",
    # Inverse of the above: takes a published article down (deletes its page,
    # marks it rejected, removes its musings, invalidates the CDN cache). See
    # admin_api_handler.py's _unpublish_article and `articles unpublish`.
    "POST /articles/{article_id}/unpublish",
    # A rewrite steered by what a person says is wrong: takes a published article
    # down first, and the result waits in the inbox. See admin_api_handler.py's
    # _rewrite_article and `articles rewrite`.
    "POST /articles/{article_id}/rewrite",
    # Lineage/cost repair: `audit` lists articles with missing lineage or cost and
    # models with no known price; `backfill` recomputes cost from stored tokens
    # (a dry run unless {"apply": true}). See admin_api_handler.py's _lineage_*
    # and `admin_cli lineage`.
    # How the fresh-data review is doing (counts, and what enforcement would have held).
    # See admin_api_handler.py's _review_report and `admin_cli review report`.
    "GET /review/report",
    "GET /lineage/audit",
    "POST /lineage/backfill",
    # Observability enhancement, PR 5: one-time catch-up folding every existing article's
    # already-recorded lineage cost into StatsHistory's all-time row (a dry run unless
    # {"apply": true}, and refuses to double-count on a second run -- see admin_api_handler.py's
    # _stats_backfill_articles and `admin_cli stats backfill-articles`).
    "POST /stats/backfill-articles",
    "GET /moderation-queue",
    "POST /moderation-queue/{queue_id}/approve",
    "POST /moderation-queue/{queue_id}/reject",
    "POST /moderation-queue/{queue_id}/rewrite",
    # Phase 6: "what's actually been flagged so far" visibility -- see
    # admin_api_handler.py's _moderation_queue_stats and
    # scripts/admin_cli.py's `moderation stats` subcommand.
    "GET /moderation-queue/stats",
    # Phase 5: prompt refinement approval workflow -- see
    # admin_api_handler.py's _ROUTES dict and scripts/admin_cli.py's
    # `refinements` subcommand.
    "GET /prompt-refinements",
    "POST /prompt-refinements/{topic_id}/{version}/approve",
    "POST /prompt-refinements/{topic_id}/{version}/reject",
    "POST /prompt-refinements/{topic_id}/{version}/equip",
    "POST /prompt-refinements/{topic_id}/{version}/unequip",
    "POST /prompt-refinements/{topic_id}/{version}/rarity",
    "POST /prompt-refinements/{topic_id}/{version}/repair",
    "GET /equipment",
    "POST /equipment",
    "POST /prompt-refinements/{topic_id}/{version}/announce",
    "DELETE /prompt-refinements/{topic_id}/{version}",
    # DLQ-consumer visibility -- see admin_api_handler.py's
    # _list_failed_executions and scripts/admin_cli.py's
    # `failed-executions list` subcommand.
    "GET /failed-executions",
    # AI lineage/cost-tracking enhancement (docs/project-plan.md §11, PR 1
    # of 5): the DynamoDB-backed model registry and global default/
    # fallback model config -- see admin_api_handler.py's _list_models/
    # _put_model/_get_model_config/_put_model_config and
    # scripts/admin_cli.py's `models`/`model-config` subcommands.
    "GET /models",
    "POST /models",
    "GET /model-config",
    "PUT /model-config",
    # Pipeline-wide settings (today: the default research interval). See
    # admin_api_handler.py's _*_pipeline_config_route and `admin_cli pipeline-config`.
    "GET /pipeline-config",
    "PUT /pipeline-config",
    # Feedback limits (lockdown, rate limit, daily limit, per-article limit) and locking one
    # article. See admin_api_handler.py's _*_feedback_* routes and `admin_cli feedback-config`.
    # One article in any status (title, text, cost, sources) for the review inbox: see
    # admin_api_handler.py's _get_article and scripts/review_inbox.py.
    "GET /articles/{article_id}",
    "GET /feedback-config",
    "PUT /feedback-config",
    "PUT /articles/{article_id}/feedback-lock",
  ])
}

# -----------------------------------------------------------------------
# Regional WAF IP allowlist -- a different Web ACL from aws_wafv2_web_acl.
# this above (CLOUDFRONT scope, us-east-1, shared by both distributions).
# This one is REGIONAL scope, created in this environment's default
# home-region provider (regional WAF for API Gateway lives in the
# API's own region, no us-east-1 alias needed), and protects only the
# admin API.
#
# default_action = block: until var.admin_allowed_cidrs is set to the
# operator's real public IP (a /32 CIDR), the IP set is empty and NOTHING
# can call this API. That is the deliberately safe default -- fail closed
# -- not a bug.
# -----------------------------------------------------------------------
resource "aws_wafv2_ip_set" "admin_allowlist" {
  name               = "bloggerbear-production-admin-allowlist"
  scope              = "REGIONAL"
  ip_address_version = "IPV4"
  addresses          = var.admin_allowed_cidrs
}

resource "aws_wafv2_web_acl" "admin" {
  name = "bloggerbear-production-admin-api"
  # See the identical regex-safety comment on production's shared
  # aws_wafv2_web_acl.this above -- no semicolons/apostrophes/parens.
  description = "Regional WAF Web ACL for the BloggerBear production admin API -- allows only the operators allowlisted IPs, blocks everything else by default."
  scope       = "REGIONAL"

  default_action {
    block {}
  }

  rule {
    name     = "allow-admin-ips"
    priority = 1

    action {
      allow {}
    }

    statement {
      ip_set_reference_statement {
        arn = aws_wafv2_ip_set.admin_allowlist.arn
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-admin-allow"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-production-admin-acl"
    sampled_requests_enabled   = true
  }
}

# WAF association for the admin API is handled inside module "admin_api"
# above (its web_acl_id input) -- REST API stage ARNs are a
# WAFv2-supported association target, unlike the HTTP API stage ARN this
# used to be.

# =========================================================================
# Phase 3 -- Automation: a Step Functions state machine wraps the single
# daily_cycle Lambda invocation purely to get retries + a dead-letter
# queue on failure (see docs/project-plan.md §4 -- the daily authoring
# cycle is deliberately one Lambda, not four Step-Functions-orchestrated
# stages; rewriting working Phase 1 code into a multi-stage pipeline for a
# single-operator portfolio project isn't worth it). The hourly research
# tick does NOT go through Step Functions at all -- EventBridge Scheduler
# invokes it directly, since it's already a single self-contained
# diff-and-maybe-summarize operation with nothing to orchestrate.
#
# Per-topic schedules themselves are NOT Terraform resources -- topics are
# runtime data (created/edited/deleted via the admin API), so there's no
# fixed list for Terraform to enumerate. They're created dynamically at
# runtime by admin_api_handler's common/scheduler.py via the AWS SDK.
# Terraform only creates the IAM role those dynamically-created schedules
# assume (aws_iam_role.scheduler_invoke) and grants the Lambda execution
# role permission to manage them (aws_iam_role_policy.scheduler_manage).
# =========================================================================
# AVD-AWS-0096 ("queue is not encrypted") ignored deliberately -- see the
# AVD-AWS-0132 comment on aws_s3_bucket.content above for the same
# AWS-managed-vs-customer-managed-key rationale.
# trivy:ignore:AVD-AWS-0096
resource "aws_sqs_queue" "pipeline_dlq" {
  name = "bloggerbear-production-pipeline-dlq"
}

# dlq_handler.py (below) consumes this queue via an event source mapping --
# grants it the three SQS permissions Lambda's poller needs on top of the
# DynamoDB/S3/Bedrock/Logs access aws_iam_role_policy.lambda_exec already
# grants every pipeline Lambda.
data "aws_iam_policy_document" "lambda_consume_dlq" {
  statement {
    sid    = "ConsumePipelineDlq"
    effect = "Allow"
    actions = [
      "sqs:ReceiveMessage",
      "sqs:DeleteMessage",
      "sqs:GetQueueAttributes",
    ]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "lambda_consume_dlq" {
  name   = "bloggerbear-production-lambda-consume-dlq"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.lambda_consume_dlq.json
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "states_exec" {
  name               = "bloggerbear-production-states-exec"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

data "aws_iam_policy_document" "states_exec" {
  statement {
    sid       = "InvokeDailyCycle"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.daily_cycle.arn]
  }

  statement {
    sid       = "SendToDeadLetterQueue"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.pipeline_dlq.arn]
  }
}

resource "aws_iam_role_policy" "states_exec" {
  name   = "bloggerbear-production-states-exec"
  role   = aws_iam_role.states_exec.id
  policy = data.aws_iam_policy_document.states_exec.json
}

resource "aws_sfn_state_machine" "daily_cycle" {
  name     = "bloggerbear-production-daily-cycle"
  role_arn = aws_iam_role.states_exec.arn

  definition = jsonencode({
    StartAt = "RunDailyCycle"
    States = {
      RunDailyCycle = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.daily_cycle.arn
          "Payload.$"  = "$"
        }
        Retry = [
          {
            ErrorEquals     = ["States.ALL"]
            IntervalSeconds = 30
            MaxAttempts     = 2
            BackoffRate     = 2.0
          }
        ]
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            Next        = "SendToDeadLetterQueue"
            ResultPath  = "$.error"
          }
        ]
        End = true
      }
      SendToDeadLetterQueue = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage"
        Parameters = {
          QueueUrl        = aws_sqs_queue.pipeline_dlq.url
          "MessageBody.$" = "$"
        }
        End = true
      }
    }
  })
}

# DLQ consumer -- an eighth Lambda (from the same shared deployment package
# above, sharing the same aws_iam_role.lambda_exec plus the
# lambda_consume_dlq policy above) that turns each pipeline_dlq message
# into a FailedExecutions record for admin visibility (see
# lambdas/dlq_handler.py and scripts/admin_cli.py's `failed-executions
# list` subcommand). Before this existed, the queue had no consumer at
# all -- only the CloudWatch alarm on queue depth (see
# infra/modules/observability's pipeline_dlq_messages alarm).
resource "aws_lambda_function" "dlq_handler" {
  function_name = "bloggerbear-production-dlq-handler"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "dlq_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# No second-level DLQ configured on this mapping -- a message that fails
# dlq_handler itself (e.g. a genuine DynamoDB outage, not the malformed-body
# case dlq_handler already handles defensively) will simply be retried by
# SQS against pipeline_dlq indefinitely, per its own default visibility-
# timeout/redrive behavior, rather than escalating anywhere further. Adding
# a DLQ-for-the-DLQ is out of scope for a single-operator project at this
# scale -- a stuck message here would still surface via
# infra/modules/observability's pipeline_dlq_messages alarm (queue depth
# never reaches zero) and this Lambda's own Errors alarm.
resource "aws_lambda_event_source_mapping" "dlq_handler" {
  event_source_arn = aws_sqs_queue.pipeline_dlq.arn
  function_name    = aws_lambda_function.dlq_handler.function_name
  batch_size       = 10
}

# -----------------------------------------------------------------------
# EventBridge Scheduler invocation role -- assumed by EventBridge
# Scheduler (not by Lambda or Step Functions) whenever a per-topic
# schedule fires. The per-topic schedules themselves are created
# dynamically at runtime by admin_api_handler's common/scheduler.py, not
# by Terraform -- see the Phase 3 header comment above.
# -----------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler_invoke" {
  name               = "bloggerbear-production-scheduler-invoke"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

data "aws_iam_policy_document" "scheduler_invoke" {
  statement {
    sid       = "InvokeResearchTick"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.research_tick.arn]
  }

  statement {
    sid       = "StartDailyCycleExecution"
    effect    = "Allow"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.daily_cycle.arn]
  }

  # Phase 5: the weekly reflection job's single static schedule (see
  # aws_scheduler_schedule.weekly_reflection below) also assumes this same
  # role -- a third, separately-listed resource, same tight per-resource
  # scoping as the two statements above, deliberately not merged into
  # InvokeResearchTick's resources list or widened to a wildcard.
  statement {
    sid       = "InvokeWeeklyReflection"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.weekly_reflection.arn]
  }

  # Phase 8: the trending-digest job's single static schedule (see
  # aws_scheduler_schedule.trending_digest below) -- same pattern as
  # InvokeWeeklyReflection above, its own separately-listed statement.
  statement {
    sid       = "InvokeTrendingDigest"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.trending_digest.arn]
  }

  # Musings: the periodic feedback-musing job's single static schedule (see
  # aws_scheduler_schedule.musing_feedback below) -- same pattern as
  # InvokeWeeklyReflection/InvokeTrendingDigest above.
  statement {
    sid       = "InvokeMusingFeedback"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.musing_feedback.arn]
  }

  # The weekly stats rollover and the daily Cost Explorer poll. Both schedules were added (with this
  # role as their role_arn) without these statements, so neither Lambda was ever invoked: the
  # scheduler failed and retried every attempt, the Historic totals stayed at zero and the actual
  # (from the bill) costs stayed null. test_terraform_wiring.py now checks every schedule's target
  # against this policy.
  statement {
    sid       = "InvokeStatsRollover"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.stats_rollover.arn]
  }

  statement {
    sid       = "InvokeCostExplorerPoll"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.cost_explorer_poll.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name   = "bloggerbear-production-scheduler-invoke"
  role   = aws_iam_role.scheduler_invoke.id
  policy = data.aws_iam_policy_document.scheduler_invoke.json
}

# Third inline policy on the shared lambda_exec role (same pattern as
# Phase 2's lambda_invoke_pipeline above) -- lets admin_api_handler's
# common/scheduler.py manage per-topic EventBridge Scheduler schedules at
# runtime. Scoped to the default schedule group (no custom group is
# created) and the bloggerbear-production-* name prefix, never "*".
# iam:PassRole is scoped to exactly the one scheduler_invoke role ARN --
# CreateSchedule / UpdateSchedule calls pass that role for EventBridge to
# assume, and IAM requires the caller to hold explicit PassRole on it;
# this must never be widened beyond that single role ARN (see
# bootstrap/main.tf's LambdaExecRole comment for why IAM statements in
# this project are never scoped to "*").
data "aws_iam_policy_document" "scheduler_manage" {
  statement {
    sid    = "ManageTopicSchedules"
    effect = "Allow"
    actions = [
      "scheduler:CreateSchedule",
      "scheduler:UpdateSchedule",
      "scheduler:DeleteSchedule",
      "scheduler:GetSchedule",
    ]
    resources = ["arn:aws:scheduler:${var.aws_region}:*:schedule/default/bloggerbear-production-*"]
  }

  statement {
    sid       = "PassSchedulerInvokeRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.scheduler_invoke.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_manage" {
  name   = "bloggerbear-production-scheduler-manage"
  role   = aws_iam_role.lambda_exec.id
  policy = data.aws_iam_policy_document.scheduler_manage.json
}

# =========================================================================
# Phase 4 -- Public frontend: a fourth Lambda (from the same shared
# deployment package above, sharing the same aws_iam_role.lambda_exec --
# it already has read/write on all app tables and the content bucket from
# Phase 1, which is everything public_api_handler.py needs; no new IAM
# grant required) fronted by a PUBLIC, unauthenticated API Gateway HTTP
# API with CORS enabled -- deliberately the opposite security posture
# from Phase 2's admin API (which is IAM-SigV4-gated and IP-allowlisted).
# Protected instead by a rate-limiting regional WAF Web ACL that defaults
# to allow (vs. Phase 2's ACL, which defaults to block; also distinct
# from aws_wafv2_web_acl.this above, the CLOUDFRONT-scope shared ACL for
# the site distributions). Also uploads the static frontend (frontend/,
# plain HTML/CSS/JS, no build step) to the EXISTING Phase 0 site bucket
# (module.static_site.bucket_name) -- no new bucket is created here.
# =========================================================================

resource "aws_lambda_function" "public_api" {
  function_name = "bloggerbear-production-public-api"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "public_api_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Public API -- REST API (v1), same migration and rationale as
# module "admin_api" above (see infra/modules/rest-api's header comment).
# Every route is authorization = "NONE" -- unauthenticated on purpose,
# this is public read data (topics/articles/rss) plus an anonymous view
# counter. enable_cors = true is what lets the frontend's JS, served from
# the CloudFront domain (a different origin than this API Gateway's own
# domain), call these endpoints from the browser -- REST API has no
# declarative cors_configuration block like HTTP API did, so this adds a
# Lambda-proxied OPTIONS method per path instead; public_api_handler.py
# handles OPTIONS itself and adds CORS headers to every response.
#
# Keep this route set in sync with public_api_handler.py's _ROUTES dict.
# -----------------------------------------------------------------------
module "public_api" {
  source = "../../modules/rest-api"

  aws_region = var.aws_region

  name                 = "bloggerbear-production-public-api"
  stage_name           = "production"
  lambda_invoke_arn    = aws_lambda_function.public_api.invoke_arn
  lambda_function_name = aws_lambda_function.public_api.function_name
  authorization        = "NONE"
  enable_cors          = true
  web_acl_id           = aws_wafv2_web_acl.public_api.arn
  associate_web_acl    = true

  # Scaling PR C: a ceiling for the whole API, well above what the site needs (most reads are
  # now answered by module.public_api_cdn's cache) and well below what would run up a bill.
  # The feedback POST, which can cost a model call, gets its own lower one; per-visitor limits
  # stay with aws_wafv2_web_acl.public_api. Access logs carry no visitor details and are kept
  # as long as the visitor WAF logs.
  throttling_rate_limit  = 25
  throttling_burst_limit = 50
  method_throttling = {
    "POST /articles/{article_id}/feedback" = { rate_limit = 2, burst_limit = 5 }
  }
  access_log_retention_days = local.waf_visitor_log_retention_days

  routes = toset([
    "GET /topics",
    # Static article publishing (docs/project-plan.md §11): a derived
    # boolean only -- never raw Findings/CandidateIdeas -- so the frontend
    # can show a "researching this topic" placeholder. See
    # public_api_handler.py's _topic_activity.
    "GET /topics/{topic_id}/activity",
    "GET /articles",
    "GET /articles/{article_id}",
    "POST /articles/{article_id}/view",
    # Phase 5: anonymous thumbs up/down + optional comment -- see
    # public_api_handler.py's _submit_feedback.
    # Whether feedback is open for an article, and if not why (the page swaps its form for the
    # reason) -- see public_api_handler.py's _feedback_status and common/feedback_limits.py.
    "GET /articles/{article_id}/feedback-status",
    "POST /articles/{article_id}/feedback",
    # BloggerBear's musings feed -- see public_api_handler.py's
    # _list_musings.
    "GET /musings",
    # Aggregate AI cost/token statistics -- see public_api_handler.py's
    # _stats and common/stats.py. Aggregates only, never article content.
    "GET /stats",
    # What BloggerBear is wearing (the Stats page): gear and a backpack count, never the backpack --
    # see public_api_handler.py's _equipment and common/gear.py.
    "GET /equipment",
    "GET /rss.xml",
  ])
}

# -----------------------------------------------------------------------
# Scaling PR C: the public API behind its own CloudFront distribution (see
# infra/modules/api-cdn for why it isn't a behaviour on the site's), so listings, articles and
# RSS are served from the edge. The frontend calls it (config.js below); the execute-api URL keeps
# working for anything already pointed at it, such as RSS readers.
#
# The secret it sends with every request tells aws_wafv2_web_acl.public_api which requests came
# through it, so their per-visitor limits can use the visitor address CloudFront records
# (x-viewer-ip) rather than the edge address every visitor shares.
# -----------------------------------------------------------------------
resource "random_password" "api_origin_verify" {
  length  = 32
  special = false
}

module "public_api_cdn" {
  source = "../../modules/api-cdn"

  environment_name     = "production"
  api_domain           = module.public_api.api_domain
  stage_name           = module.public_api.stage_name
  origin_verify_secret = random_password.api_origin_verify.result
  # The shared CLOUDFRONT-scope ACL above: per-visitor rate limiting and the managed rules at the
  # edge, in front of the cache.
  web_acl_id = aws_wafv2_web_acl.this.arn
}

# -----------------------------------------------------------------------
# Rate-limiting regional WAF Web ACL -- protects the anonymous
# POST /articles/{id}/view endpoint (and the rest of this public API)
# from scripted abuse, without blocking legitimate public traffic.
# default_action = allow is the deliberate opposite of Phase 2's admin
# ACL (which defaults to block-everything): this is a public API meant to
# be reachable by anyone. The one rule blocks only an individual source
# IP once it exceeds 500 requests within WAF's fixed (non-configurable)
# 5-minute rate-based window -- generous enough for a real visitor
# browsing the site, low enough to blunt a scripted hammering of the view
# counter.
# -----------------------------------------------------------------------
resource "aws_wafv2_web_acl" "public_api" {
  name = "bloggerbear-production-public-api"
  # See the identical regex-safety comment above -- no semicolons.
  description = "Regional WAF Web ACL for the BloggerBear production public API -- allows all traffic by default, rate-limits any single source IP past 500 requests per 5-minute window."
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-limit"
    priority = 1

    action {
      block {}
    }

    # Scaling PR C: direct calls only (no x-origin-verify). Through the CDN every request arrives
    # from an edge address many visitors share, so those are limited per visitor by
    # rate-limit-via-cdn below instead.
    statement {
      rate_based_statement {
        limit              = 500
        aggregate_key_type = "IP"

        scope_down_statement {
          not_statement {
            statement {
              byte_match_statement {
                search_string         = random_password.api_origin_verify.result
                positional_constraint = "EXACTLY"

                field_to_match {
                  single_header {
                    name = "x-origin-verify"
                  }
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-public-api-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  # Phase 6: baseline anti-abuse hardening on top of the rate limit above
  # -- the same AWS Managed Common Rule Set already used by the
  # CLOUDFRONT-scope shared ACL (aws_wafv2_web_acl.this above), applied
  # here too since this REGIONAL ACL is the only thing directly in front
  # of the public API Gateway (including
  # requests through module.public_api_cdn, which has an edge WAF only
  # where the shared ACL is attached). Not added to aws_wafv2_web_acl.admin
  # below -- that ACL already default-blocks everything except the
  # operator's own allowlisted IP, which is stricter than any managed
  # rule set could add.
  # The feedback route only: at most 10 submissions per 5 minutes from one IP (WAF's lowest allowed
  # limit; lowered from 20, since a rejected comment counts against no app-side limit and this is
  # the only per-IP cap on retrying one).
  # The general limit above (500 across the whole API) is far too loose for something that costs a
  # model call, and this stores nothing about the visitor: WAF counts the source address itself
  # and forgets it.
  # The path match is on the end of the path so it catches POST .../articles/{id}/feedback and
  # not GET .../feedback-status. Blocked requests are answered by WAF before they reach the Lambda.
  rule {
    name     = "feedback-rate-limit"
    priority = 3

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = 10
        evaluation_window_sec = 300
        aggregate_key_type    = "IP"

        # Direct calls only, as for rate-limit above; feedback-rate-limit-via-cdn covers the rest.
        scope_down_statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "/feedback"
                positional_constraint = "ENDS_WITH"

                field_to_match {
                  uri_path {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }

            statement {
              not_statement {
                statement {
                  byte_match_statement {
                    search_string         = random_password.api_origin_verify.result
                    positional_constraint = "EXACTLY"

                    field_to_match {
                      single_header {
                        name = "x-origin-verify"
                      }
                    }

                    text_transformation {
                      priority = 0
                      type     = "NONE"
                    }
                  }
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-public-api-feedback-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-managed-common"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-public-api-common-rule-set"
      sampled_requests_enabled   = true
    }
  }

  # Scaling PR C: the same two limits for requests that came through module.public_api_cdn (they
  # carry its secret), counted per visitor from the x-viewer-ip header its CloudFront Function
  # sets. Only cache misses and POSTs get this far, so a visitor browsing cached pages uses none
  # of it. A request whose header is missing or unreadable is never blocked by these (NO_MATCH).
  rule {
    name     = "rate-limit-via-cdn"
    priority = 4

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 500
        aggregate_key_type = "FORWARDED_IP"

        forwarded_ip_config {
          header_name       = "x-viewer-ip"
          fallback_behavior = "NO_MATCH"
        }

        scope_down_statement {
          byte_match_statement {
            search_string         = random_password.api_origin_verify.result
            positional_constraint = "EXACTLY"

            field_to_match {
              single_header {
                name = "x-origin-verify"
              }
            }

            text_transformation {
              priority = 0
              type     = "NONE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-public-api-rate-limit-via-cdn"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "feedback-rate-limit-via-cdn"
    priority = 5

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = 10
        evaluation_window_sec = 300
        aggregate_key_type    = "FORWARDED_IP"

        forwarded_ip_config {
          header_name       = "x-viewer-ip"
          fallback_behavior = "NO_MATCH"
        }

        scope_down_statement {
          and_statement {
            statement {
              byte_match_statement {
                search_string         = "/feedback"
                positional_constraint = "ENDS_WITH"

                field_to_match {
                  uri_path {}
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }

            statement {
              byte_match_statement {
                search_string         = random_password.api_origin_verify.result
                positional_constraint = "EXACTLY"

                field_to_match {
                  single_header {
                    name = "x-origin-verify"
                  }
                }

                text_transformation {
                  priority = 0
                  type     = "NONE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "bloggerbear-production-public-api-feedback-rate-limit-via-cdn"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "bloggerbear-production-public-api-acl"
    sampled_requests_enabled   = true
  }
}

# -----------------------------------------------------------------------
# Phase 6: WAF logging -- every ACL's traffic (allowed and blocked)
# streams to CloudWatch Logs so the rate-limit/managed-rule thresholds
# above can eventually be tuned from real observed traffic, rather than
# guessed. Covers all three ACLs in this environment: the two REGIONAL
# ones (admin, public_api) plus the CLOUDFRONT-scope shared one
# (aws_wafv2_web_acl.this) -- that one's log group and resource policy
# must live in us-east-1 (provider = aws.us_east_1), same region
# requirement as the ACL itself. Log group names MUST start with
# "aws-waf-logs-" -- an AWS WAFv2 requirement for logging directly to
# CloudWatch Logs (no Kinesis Firehose needed).
#
# Data minimisation (the Privacy Policy's section 5 describes exactly this, so
# change both together): the two ACLs anonymous visitors pass through -- the
# public API and the shared CloudFront one -- log only requests a rule
# blocked or counted (logging_filter drops ALLOW), with the browser-
# fingerprinting headers in local.waf_log_redacted_headers redacted, and keep
# them for 14 days. WAF cannot redact the source IP, or `matchedData` (the bit
# of a blocked request that tripped a rule, which can be part of a comment).
# The admin ACL is unchanged: only the operator's allowlisted IP gets through
# it, and a full record of that is the audit trail.
# -----------------------------------------------------------------------
locals {
  # Request headers that identify a browser more than they help explain a block. Matched
  # case-insensitively by WAF; a header a request does not send is simply absent.
  waf_log_redacted_headers = [
    "user-agent",
    "referer",
    "accept",
    "accept-language",
    "accept-encoding",
    "cookie",
    "x-forwarded-for",
    "dnt",
    "sec-gpc",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-arch",
    "sec-ch-ua-bitness",
    "sec-ch-ua-model",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-wow64",
    # Not a fingerprint: the secret module.public_api_cdn sends, which must not end up in a log.
    "x-origin-verify",
  ]
  waf_visitor_log_retention_days = 14
}

resource "aws_cloudwatch_log_group" "waf_admin" {
  name              = "aws-waf-logs-bloggerbear-production-admin"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "waf_public_api" {
  name              = "aws-waf-logs-bloggerbear-production-public-api"
  retention_in_days = local.waf_visitor_log_retention_days
}

data "aws_iam_policy_document" "waf_logs" {
  statement {
    sid    = "AllowWAFLogging"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions   = ["logs:PutLogEvents", "logs:CreateLogStream"]
    resources = ["arn:aws:logs:${var.aws_region}:*:log-group:aws-waf-logs-bloggerbear-production-*:*"]
  }
}

resource "aws_cloudwatch_log_resource_policy" "waf_logs" {
  policy_name     = "bloggerbear-production-waf-logs"
  policy_document = data.aws_iam_policy_document.waf_logs.json
}

resource "aws_wafv2_web_acl_logging_configuration" "admin" {
  resource_arn            = aws_wafv2_web_acl.admin.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_admin.arn]

  depends_on = [aws_cloudwatch_log_resource_policy.waf_logs]
}

resource "aws_wafv2_web_acl_logging_configuration" "public_api" {
  resource_arn            = aws_wafv2_web_acl.public_api.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_public_api.arn]

  dynamic "redacted_fields" {
    for_each = local.waf_log_redacted_headers
    content {
      single_header {
        name = redacted_fields.value
      }
    }
  }

  logging_filter {
    default_behavior = "DROP"

    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"

      condition {
        action_condition {
          action = "BLOCK"
        }
      }

      condition {
        action_condition {
          action = "COUNT"
        }
      }
    }
  }

  depends_on = [aws_cloudwatch_log_resource_policy.waf_logs]
}

# The shared CloudFront-scope ACL's log group/policy/logging-config trio
# -- all in us-east-1, same as aws_wafv2_web_acl.this itself.
resource "aws_cloudwatch_log_group" "waf_shared" {
  provider = aws.us_east_1

  name              = "aws-waf-logs-bloggerbear-shared"
  retention_in_days = local.waf_visitor_log_retention_days
}

# -----------------------------------------------------------------------
# Cleanup PR: every pipeline/API Lambda's own log group (/aws/lambda/<function
# name>, distinct from the WAF logging above), which Lambda otherwise
# creates itself on first invocation with no retention at all, growing
# forever. IAM for this was already provisioned ahead of time in
# infra/bootstrap/main.tf's LambdaLogGroups statement -- see its own comment.
#
# Every one of these functions has almost certainly already run at least
# once in this environment, so its log group already exists in AWS. Terraform
# cannot adopt an existing resource by just declaring it -- the first apply
# after this change needs each one imported first, or it fails with
# ResourceAlreadyExistsException:
#
#   terraform import 'aws_cloudwatch_log_group.lambda["bloggerbear-production-research-tick"]' /aws/lambda/bloggerbear-production-research-tick
#   (repeat for each function_name below)
# -----------------------------------------------------------------------
# Every Lambda below depends_on this resource, so its log group exists before the function can be
# invoked. Without that, a function created in the same apply and called straight away (the public
# API, the moment its stage is deployed) makes its own log group first -- with no retention -- and
# this resource then fails with ResourceAlreadyExistsException. Dev hit exactly that rebuilding
# after a destroy (2026-10-02). It also orders a destroy: functions go first, so nothing can recreate
# a log group after Terraform has deleted it.
#
# Literal names, not local.pipeline_lambda_function_names: that list reads each function's
# function_name, and a function that depends on these groups can't also name them (a cycle).
# test_terraform_wiring.py checks this list matches every aws_lambda_function's function_name.
locals {
  lambda_log_group_function_names = [
    "bloggerbear-production-research-tick",
    "bloggerbear-production-daily-cycle",
    "bloggerbear-production-admin-api",
    "bloggerbear-production-dlq-handler",
    "bloggerbear-production-public-api",
    "bloggerbear-production-weekly-reflection",
    "bloggerbear-production-stats-rollover",
    "bloggerbear-production-cost-explorer-poll",
    "bloggerbear-production-trending-digest",
    "bloggerbear-production-musing-feedback",
    "bloggerbear-production-security-events",
  ]
}

resource "aws_cloudwatch_log_group" "lambda" {
  for_each          = toset(local.lambda_log_group_function_names)
  name              = "/aws/lambda/${each.value}"
  retention_in_days = 90
}

data "aws_iam_policy_document" "waf_logs_shared" {
  statement {
    sid    = "AllowWAFLogging"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["delivery.logs.amazonaws.com"]
    }
    actions = ["logs:PutLogEvents", "logs:CreateLogStream"]
    # us-east-1 written out, not var.aws_region: this is the CloudFront-scope web ACL's log group,
    # and AWS only hosts a CLOUDFRONT-scope ACL, and so its logs, in that one region.
    resources = ["arn:aws:logs:us-east-1:*:log-group:aws-waf-logs-bloggerbear-shared:*"]
  }
}

resource "aws_cloudwatch_log_resource_policy" "waf_logs_shared" {
  provider = aws.us_east_1

  policy_name     = "bloggerbear-shared-waf-logs"
  policy_document = data.aws_iam_policy_document.waf_logs_shared.json
}

resource "aws_wafv2_web_acl_logging_configuration" "shared" {
  provider = aws.us_east_1

  resource_arn            = aws_wafv2_web_acl.this.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf_shared.arn]

  dynamic "redacted_fields" {
    for_each = local.waf_log_redacted_headers
    content {
      single_header {
        name = redacted_fields.value
      }
    }
  }

  logging_filter {
    default_behavior = "DROP"

    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"

      condition {
        action_condition {
          action = "BLOCK"
        }
      }

      condition {
        action_condition {
          action = "COUNT"
        }
      }
    }
  }

  depends_on = [aws_cloudwatch_log_resource_policy.waf_logs_shared]
}

# WAF association for the public API is handled inside module
# "public_api" above (its web_acl_id input) -- REST API stage ARNs are a
# WAFv2-supported association target, unlike the HTTP API stage ARN this
# used to be.

# -----------------------------------------------------------------------
# Frontend static files -- uploaded to the EXISTING Phase 0 site bucket
# (module.static_site.bucket_name), not a new bucket (Phase 0 already
# created one per environment). config.js is generated here (not read
# from frontend/) since it needs this environment's own API Gateway
# invoke URL and site URL, both only known once the resources above
# exist; frontend/index.html loads it before app.js to pick up
# window.PUBLIC_API_URL and window.SITE_URL.
#
# fileexists()-guarded count: the frontend/ directory (owned by a
# concurrent workstream) may not exist yet when this is first applied in
# some environments/orderings; these resources simply create nothing
# until the files land, rather than failing terraform validate/plan.
#
# frontend_dir points at frontend-dist/, not frontend/ itself -- a mirror
# scripts/minify_frontend.py builds with .js/.css minified (frontend/ stays
# exactly as committed: plain, comment-rich, no build step to read it
# yourself). Generated fresh by CI before every apply (see
# .github/workflows/terraform.yml/terraform-production-release.yml) and
# never committed, the same "regenerated, gitignored" relationship
# lambda-build/ already has with lambdas/ -- see .gitignore's comment on
# that one. Anyone applying by hand needs to run that script first too, or
# every frontend_files entry below evaluates fileexists() to false and
# uploads nothing (safe -- see the guard above -- just not what you want).
# -----------------------------------------------------------------------
locals {
  frontend_dir = "${path.module}/../../../frontend-dist"
  frontend_files = {
    "index.html"    = "text/html"
    "error.html"    = "text/html"
    "about.html"    = "text/html"
    "terms.html"    = "text/html"
    "privacy.html"  = "text/html"
    "ask.html"      = "text/html" # the operator's assistant: unlinked, noindex; with no settings in config.js it only says it is not available here
    "ask.css"       = "text/css"
    "ask.js"        = "application/javascript"
    "styles.css"    = "text/css"
    "normalize.css" = "text/css"
    "app.js"        = "application/javascript"
    "markdown.js"   = "application/javascript"
    "verify.js"     = "application/javascript"
    "moods.js"      = "application/javascript"
    "tummy.js"      = "application/javascript"
    "gear.js"       = "application/javascript"
    # Legacy shim: only article pages published during PR #125's preload+swap experiment load
    # this; keep it deployed until those are re-rendered (see the file's own docstring).
    "preload-styles.js" = "application/javascript"
    # Sets #site-notice's initial visibility before first paint (index.html only) -- see the
    # file's own docstring for the layout-shift bug this replaces.
    "notice.js"             = "application/javascript"
    "bears/proud.svg"       = "image/svg+xml"
    "bears/thoughtful.svg"  = "image/svg+xml"
    "bears/pleased.svg"     = "image/svg+xml"
    "bears/reflective.svg"  = "image/svg+xml"
    "bears/curious.svg"     = "image/svg+xml"
    "bears/excited.svg"     = "image/svg+xml"
    "bears/default.svg"     = "image/svg+xml"
    "bears/tummy.svg"       = "image/svg+xml"
    "bears/tummy-happy.svg" = "image/svg+xml"
    "bears/shocked.svg"     = "image/svg+xml"
    # Static article publishing (docs/project-plan.md §11): the external
    # script the pages rendered by common/static_pages.py load -- must be
    # a real file at the bucket root, not inline, per the CSP comment on
    # aws_cloudfront_response_headers_policy.security (script-src 'self',
    # no unsafe-inline).
    "article-widgets.js"   = "application/javascript"
    "robots.txt"           = "text/plain"
    "logo.svg"             = "image/svg+xml"
    "logo.webp"            = "image/webp"
    "favicon.ico"          = "image/x-icon"
    "apple-touch-icon.png" = "image/png"
  }
}

resource "aws_s3_object" "frontend" {
  for_each = {
    for name, content_type in local.frontend_files :
    name => content_type if fileexists("${local.frontend_dir}/${name}")
  }

  bucket       = module.static_site.bucket_name
  key          = each.key
  source       = "${local.frontend_dir}/${each.key}"
  etag         = filemd5("${local.frontend_dir}/${each.key}")
  content_type = each.value
  # The files aren't fingerprinted, so a browser or CloudFront must not keep serving an old
  # copy after a deploy: with no Cache-Control at all they did (CloudFront's default TTL is 24
  # hours and browsers guess), so a new page could arrive with the old stylesheet. no-cache means
  # "revalidate every time" -- a cheap conditional request -- for everything but images. (The bear
  # pictures under bears/ revalidate too, so replacing one with your own shows up at once.)
  cache_control = startswith(each.value, "image/") && !startswith(each.key, "bears/") ? "public, max-age=86400" : "no-cache"
}

resource "aws_s3_object" "frontend_config" {
  bucket        = module.static_site.bucket_name
  key           = "config.js"
  content_type  = "application/javascript"
  cache_control = "no-cache"

  content = <<-EOT
    window.PUBLIC_API_URL = "${module.public_api_cdn.url}";
    window.SITE_URL = "${local.site_url}";
  EOT
}

# =========================================================================
# Phase 5 -- Feedback loop: a fifth Lambda (from the same shared deployment
# package above, sharing the same aws_iam_role.lambda_exec -- it already has
# DynamoDB access to the two new Phase 5 tables via
# module.app_data.table_arns, plus bedrock:InvokeModel from Phase 1, which
# is everything weekly_reflection_handler.py needs; no new IAM role or
# policy resource required) on a single, static, Terraform-managed weekly
# schedule.
#
# Unlike Phase 3's per-topic dynamic scheduling (research_tick/daily_cycle
# cadence is genuinely per-topic-configurable), this is ONE global
# analytical job that internally loops over all topics with recent
# feedback -- so it gets one fixed weekly cron here, not a runtime-created
# per-topic schedule.
# =========================================================================

resource "aws_lambda_function" "weekly_reflection" {
  function_name = "bloggerbear-production-weekly-reflection"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "weekly_reflection_handler.handler"
  runtime       = "python3.11"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Static weekly schedule -- Monday 9am UTC, a fixed literal (not
# topic-driven config), since there's nothing per-topic to configure about
# this global job. group_name = "default" matches the same schedule group
# Phase 3's dynamically-created per-topic schedules use.
resource "aws_scheduler_schedule" "weekly_reflection" {
  name                = "bloggerbear-production-weekly-reflection"
  group_name          = "default"
  schedule_expression = "cron(0 9 ? * MON *)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.weekly_reflection.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# Observability enhancement, PR 2: closes out this week's Stats row and opens next week's. Scheduled
# 15 minutes after weekly_reflection above, so that Monday's reflection cost is tallied into the week
# it is reflecting on, not the new week that is just starting -- see stats_rollover_handler.py.
resource "aws_lambda_function" "stats_rollover" {
  function_name = "bloggerbear-production-stats-rollover"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "stats_rollover_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

resource "aws_scheduler_schedule" "stats_rollover" {
  name                = "bloggerbear-production-stats-rollover"
  group_name          = "default"
  schedule_expression = "cron(15 9 ? * MON *)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.stats_rollover.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# Observability enhancement, PR 3: a daily poll of Cost Explorer for API Gateway's own spend, which
# Bedrock/DynamoDB cost tracking above doesn't cover -- see common/cost_explorer.py for why daily,
# and why a rolling 30-day window ending yesterday rather than today.
resource "aws_lambda_function" "cost_explorer_poll" {
  function_name = "bloggerbear-production-cost-explorer-poll"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "cost_explorer_poll_handler.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

resource "aws_scheduler_schedule" "cost_explorer_poll" {
  name                = "bloggerbear-production-cost-explorer-poll"
  group_name          = "default"
  schedule_expression = "cron(0 10 * * ? *)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.cost_explorer_poll.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# =========================================================================
# Phase 6 -- Observability & hardening: CloudWatch alarms/dashboard for
# all 5 pipeline Lambdas + the daily-cycle state machine/DLQ (see
# infra/modules/observability), a Bedrock-spend budget alarm (see
# infra/bootstrap/main.tf -- account-level, not per-environment, so it
# lives in bootstrap rather than here), and the WAF managed-rule-set +
# logging additions above (aws_wafv2_web_acl.public_api's second rule,
# aws_wafv2_web_acl_logging_configuration.admin/public_api/shared).
# =========================================================================

locals {
  # Every pipeline/API Lambda's function_name -- shared by module.observability's alarms/
  # dashboard below and this environment's own log-group retention (aws_cloudwatch_log_group.lambda,
  # Cleanup PR), so the two can never drift out of sync when a function is added or removed.
  pipeline_lambda_function_names = [
    aws_lambda_function.research_tick.function_name,
    aws_lambda_function.daily_cycle.function_name,
    aws_lambda_function.admin_api.function_name,
    aws_lambda_function.public_api.function_name,
    aws_lambda_function.weekly_reflection.function_name,
    aws_lambda_function.trending_digest.function_name,
    aws_lambda_function.dlq_handler.function_name,
    aws_lambda_function.musing_feedback.function_name,
    aws_lambda_function.stats_rollover.function_name,
    aws_lambda_function.cost_explorer_poll.function_name,
    aws_lambda_function.security_events.function_name,
  ]
}

module "observability" {
  source = "../../modules/observability"

  aws_region = var.aws_region

  environment_name      = "production"
  lambda_function_names = local.pipeline_lambda_function_names
  state_machine_arn     = aws_sfn_state_machine.daily_cycle.arn
  dlq_queue_name        = aws_sqs_queue.pipeline_dlq.name
  alert_email           = var.alert_email

  # Its own log group resource rather than a hand-built name, so the metric filters depend on it.
  feedback_log_group_name = aws_cloudwatch_log_group.lambda[aws_lambda_function.public_api.function_name].name

  # The Lambdas that record security events, each logging one SECURITY_ALERT line per
  # high-severity incident (see the Security events section below).
  security_alert_log_groups = [
    aws_cloudwatch_log_group.lambda[aws_lambda_function.security_events.function_name].name,
    aws_cloudwatch_log_group.lambda[aws_lambda_function.public_api.function_name].name,
  ]

  # Scaling PR C: the edge dashboard, API Gateway and WAF (api_waf_dashboards.tf in the module).
  # Production only: dashboards past the account's first three cost US$3 a month each.
  edge_dashboard_enabled = true
  api_dashboard_apis = [
    {
      label            = "Public API"
      api_name         = module.public_api.api_name
      stage            = module.public_api.stage_name
      access_log_group = module.public_api.access_log_group_name
    },
    {
      label            = "Admin API"
      api_name         = module.admin_api.api_name
      stage            = module.admin_api.stage_name
      access_log_group = module.admin_api.access_log_group_name
    },
  ]
  api_cdn = {
    distribution_id            = module.public_api_cdn.distribution_id
    additional_metrics_enabled = module.public_api_cdn.additional_metrics_enabled
    api_name                   = module.public_api.api_name
    stage                      = module.public_api.stage_name
  }
  waf_regional_acls = [
    {
      label       = "Public API"
      metric_name = aws_wafv2_web_acl.public_api.visibility_config[0].metric_name
      log_group   = aws_cloudwatch_log_group.waf_public_api.name
    },
    {
      label       = "Admin API"
      metric_name = aws_wafv2_web_acl.admin.visibility_config[0].metric_name
      log_group   = aws_cloudwatch_log_group.waf_admin.name
    },
  ]
  waf_cloudfront_acl = {
    label       = "Site (CloudFront)"
    metric_name = aws_wafv2_web_acl.this.visibility_config[0].metric_name
    log_group   = aws_cloudwatch_log_group.waf_shared.name
  }
}

# =========================================================================
# Phase 8 -- Stretch: a seventh Lambda (from the same shared deployment
# package above, sharing the same aws_iam_role.lambda_exec -- it already
# has everything trending_digest_handler.py needs: read on Topics/
# Findings, read+write on Articles/ModerationQueue, the content bucket,
# and Bedrock, all granted since Phase 1/5; no new IAM role or policy
# resource required) on a single, static, Terraform-managed daily
# schedule.
#
# Same "one global job, not per-topic" pattern as Phase 5's
# weekly_reflection: this looks across every topic at once, so there's
# nothing per-topic to configure about its schedule. The "Public read
# API / RSS" half of Phase 8's scope needed no new code at all -- Phase
# 4's public API (GET /topics, GET /articles, GET /articles/{id},
# GET /rss.xml) already covers it; see docs/PROGRESS.md.
# =========================================================================

resource "aws_lambda_function" "trending_digest" {
  function_name = "bloggerbear-production-trending-digest"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "trending_digest_handler.handler"
  runtime       = "python3.11"
  timeout       = 120
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# Static daily schedule -- 7am UTC, an hour after the daily-cycle's own
# default per-topic cadence (cron(0 6 * * ? *), see
# admin_api_handler.py's _DEFAULT_DAILY_CADENCE), so a typical day's
# freshly-published articles/findings have a chance to land before the
# digest synthesizes across them. A fixed literal, not topic-driven
# config, for the same reason as weekly_reflection's schedule above.
resource "aws_scheduler_schedule" "trending_digest" {
  name                = "bloggerbear-production-trending-digest"
  group_name          = "default"
  schedule_expression = "cron(0 7 * * ? *)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.trending_digest.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# =========================================================================
# Musings -- BloggerBear's short in-character reflections feed (see
# common/musings.py, docs). Article musings are generated inline by
# daily_cycle_handler.py/admin_api_handler.py at publish time -- no new
# Lambda needed for that half. This eighth Lambda (same shared deployment
# package, same aws_iam_role.lambda_exec -- it already has everything
# musing_feedback_handler.py needs: read on Feedback, write on the new
# Musings table via module.app_data.table_arns, and Bedrock, all granted
# above; no new IAM role or policy resource required beyond the
# InvokeMusingFeedback scheduler-invoke statement above) covers the other
# half: a periodic reflection on reader feedback, on its own static
# schedule, same "one global job, not per-topic" pattern as
# weekly_reflection/trending_digest.
# =========================================================================

resource "aws_lambda_function" "musing_feedback" {
  function_name = "bloggerbear-production-musing-feedback"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "musing_feedback_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

# rate(4 days): EventBridge Scheduler's rate expressions natively support a
# "days" unit, so this needs no cron-arithmetic workaround. A fixed literal,
# not topic-driven config, for the same reason as weekly_reflection's/
# trending_digest's schedules above -- this isn't per-topic.
resource "aws_scheduler_schedule" "musing_feedback" {
  name                = "bloggerbear-production-musing-feedback"
  group_name          = "default"
  schedule_expression = "rate(4 days)"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.musing_feedback.arn
    role_arn = aws_iam_role.scheduler_invoke.arn
  }
}

# =========================================================================
# Security events (lambdas/common/security_events.py): every request a REGIONAL WAF blocks (public
# API, admin API) reaches security_events_handler.py through a CloudWatch Logs subscription filter,
# which groups them into incidents in the SecurityEvents table: category, severity, suggested next
# steps, status, a keyed hash of the client (never the IP), kept 120 days. The public API adds
# comments that comment screening dropped as attacks. A high-severity incident raises
# module.observability's security alarm once.
#
# The shared CloudFront ACL is not subscribed: its log group is in us-east-1, and a subscription
# filter can only deliver to a Lambda in its own region. Public API traffic passes the regional ACL
# too, so it is covered.
# =========================================================================
resource "aws_lambda_function" "security_events" {
  function_name = "bloggerbear-production-security-events"
  depends_on    = [aws_cloudwatch_log_group.lambda] # its log group first; see that resource
  role          = aws_iam_role.lambda_exec.arn
  handler       = "security_events_handler.handler"
  runtime       = "python3.11"
  timeout       = 60
  memory_size   = 256

  filename         = data.archive_file.lambdas.output_path
  source_code_hash = data.archive_file.lambdas.output_base64sha256

  environment {
    variables = local.lambda_env_variables
  }
}

locals {
  security_event_waf_log_groups = {
    public_api = aws_cloudwatch_log_group.waf_public_api
    admin      = aws_cloudwatch_log_group.waf_admin
  }
}

resource "aws_lambda_permission" "security_events_from_waf_logs" {
  for_each      = local.security_event_waf_log_groups
  statement_id  = "AllowWafLogs-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.security_events.function_name
  principal     = "logs.amazonaws.com"
  source_arn    = "${each.value.arn}:*"
}

# BLOCK records only: the public ACL logs blocks and counts, the admin ACL logs everything.
resource "aws_cloudwatch_log_subscription_filter" "security_events" {
  for_each        = local.security_event_waf_log_groups
  name            = "bloggerbear-production-security-events-${each.key}"
  log_group_name  = each.value.name
  filter_pattern  = "{ $.action = \"BLOCK\" }"
  destination_arn = aws_lambda_function.security_events.arn

  depends_on = [aws_lambda_permission.security_events_from_waf_logs]
}
