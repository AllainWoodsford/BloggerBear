# -----------------------------------------------------------------------
# The operator's assistant: the ops MCP server (lambdas/ops_mcp), deployed.
# Design: docs/enhancements/alexa-plus-operator-assistant-enhancement.md (sections 1, 5 and 6).
#
# What one instance of this module is:
#
#   Cognito user pool (sign-in, hosted page, a "read" scope)
#        | access token
#        v
#   API Gateway REST API -- POST /mcp, Cognito authorizer requiring that scope
#        | Lambda proxy integration (one JSON response; no streaming)
#        v
#   Lambda -- the MCP SDK's own web app under uvicorn, behind the Lambda Web Adapter layer
#        | its OWN role: read-only, on named tables
#        v
#   DynamoDB (and, for tools still to come, the content bucket's articles and CloudWatch alarms)
#
# Three things here are different from every other Lambda in this project, each on purpose:
#
# 1. Its own deployment package. The pipeline Lambdas share one zip built from lambdas/ and
#    requirements.txt. The `mcp` package and what it pulls in (pydantic, uvicorn, cryptography;
#    about 50 MB unpacked) are needed by this function alone, so they are kept out of that zip.
# 2. Its own IAM role. The pipeline Lambdas share one role with write and delete on every app
#    table. This function is reachable from the internet by anyone holding a token, so "the
#    assistant is read-only" has to be true of its role, not just of its code.
# 3. A sign-in. The admin API takes IAM-signed requests from a CLI; a web page must never hold
#    IAM keys, so this API takes a Cognito access token.
#
# The module is written for both environments (section 6 of the design); only dev calls it so far.
# -----------------------------------------------------------------------

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = ">= 2.4"
    }
  }
}

locals {
  name = "${var.unique_name_prefix}-${var.environment_name}-ops-mcp"

  # The deployment's home region, handed in by the calling root and not read from the provider,
  # so every ARN and host name below is a plain string at plan time (and in this module's tests).
  aws_region = var.aws_region

  # The AWS Lambda Web Adapter, as a layer: it turns each API Gateway event into an ordinary
  # HTTP request to the web app listening on AWS_LWA_PORT, and the app's response back into the
  # Lambda's. Taken from the adapter's README, "Zip Packages", x86_64, on 2026-10-04:
  #
  #   https://github.com/awslabs/aws-lambda-web-adapter#zip-packages
  #   arn:aws:lambda:${AWS::Region}:753240598075:layer:LambdaAdapterLayerX86:30
  #
  # with the region filled in from var.aws_region: the adapter project publishes the layer under
  # the same account and name in each region, and a function can only use the copy in its own.
  # A layer ARN from another region fails at apply, and so does a version that region never
  # received (check the README's list before deploying somewhere new). The account id is the adapter project's publishing
  # account; it and the version are copied, never guessed. To move to a newer adapter, change the
  # version here to the one the README then shows. x86_64 because the pipeline Lambdas are, and
  # the package below is built for it (the design's "arm64" was written before that was checked).
  web_adapter_layer_arn = "arn:aws:lambda:${local.aws_region}:753240598075:layer:LambdaAdapterLayerX86:30"

  # Where the adapter sends requests, and where run.sh tells uvicorn to listen. 8080 is the
  # adapter's default; it is set anyway so the two halves read the same value from one place.
  web_adapter_port = "8080"

  # The scope a token must carry to call the API: "<resource server identifier>/<scope name>".
  # Built from literals (not read back from aws_cognito_resource_server.ops) so it is known at
  # plan time and the module's tests can check the method asks for it.
  resource_server_identifier = "bloggerbear-ops"
  read_scope                 = "${local.resource_server_identifier}/read"

  # The Host header every request arrives with: API Gateway passes the caller's through, and a
  # caller of the execute-api URL sends the API's own domain. See OPS_MCP_ALLOWED_HOSTS below.
  api_host = "${aws_api_gateway_rest_api.this.id}.execute-api.${local.aws_region}.amazonaws.com"

  # The Environment tags this assistant may read data from: its own, and in production also
  # "shared" (bootstrap's resources, which serve both environments). Dev never reads production's
  # or the shared ones. Used by the table_sample statement below, by the Deny in isolation.tf, and
  # by the code (ops_mcp/samples.py, which holds the same rule and is tested against this line).
  readable_environments = concat([var.environment_name], var.environment_name == "production" ? ["shared"] : [])


  lambdas_dir = "${path.module}/../../../lambdas"
  build_dir   = "${path.module}/lambda-build/ops-mcp-package"
}

# =========================================================================
# The deployment package
# =========================================================================

# Built the way the shared package is (infra/environments/*/main.tf's
# terraform_data.lambda_package; its comments explain the staging directory, the bare "bash"
# interpreter and why this re-runs on every apply), with three differences:
#
# - Only what the server imports is copied: common/ and ops_mcp/, not the pipeline's handlers.
# - run.sh (next to this file) goes in the package root, marked executable: it is the function's
#   "handler", and the adapter's wrapper execs it. Carriage returns are stripped on the way in,
#   so a Windows checkout cannot produce a script Linux refuses to run ("bad interpreter").
# - pip is told which platform to install for. requirements.txt is pure Python, so the shared
#   package gets away with whatever the build machine is. `mcp` is not: pydantic-core,
#   cryptography, cffi and rpds-py are compiled, and a wheel built for the runner's own Python or
#   OS fails at import in the Lambda. --platform/--implementation/--python-version name the
#   Lambda runtime (Linux x86_64, CPython 3.11), and --only-binary=:all: (which pip requires with
#   --platform) makes a dependency with no such wheel fail the build here, not the first request.
#   Checked 2026-10-04: every package in mcp 2.1.1's dependency tree has a manylinux2014 cp311
#   wheel, and uvicorn is in that tree, so requirements-ops-mcp.txt needs no line for it.
#
# This still has to run on Linux, which the CI runner that performs every real apply is. pip
# decides which dependencies apply from the machine it runs on, not from --platform, so on
# Windows it also asks for mcp's Windows-only dependency (pywin32), finds no Linux wheel for it,
# and stops. The runner's Python is 3.11, the same as the runtime, so the versions pip picks
# there are the ones the runtime needs.
resource "terraform_data" "package" {
  triggers_replace = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      build_dir="${local.build_dir}"
      rm -rf "$build_dir"
      mkdir -p "$build_dir"
      cp -r "${local.lambdas_dir}/common" "$build_dir/common"
      cp -r "${local.lambdas_dir}/ops_mcp" "$build_dir/ops_mcp"
      find "$build_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
      tr -d '\r' < "${path.module}/run.sh" > "$build_dir/run.sh"
      chmod 755 "$build_dir/run.sh"
      if python3 -c "" >/dev/null 2>&1; then
        py_cmd="python3"
      else
        py_cmd="py -3"
      fi
      $py_cmd -m pip install --upgrade --no-cache-dir \
        --platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all: \
        -r "${local.lambdas_dir}/requirements.txt" \
        -r "${local.lambdas_dir}/requirements-ops-mcp.txt" \
        -t "$build_dir"
    EOT
  }
}

# output_path carries the build's id for the same reason the shared package's does: it is
# unknown until apply, which is what makes Terraform read this after the build has run and not
# during the plan, when the directory does not exist yet.
data "archive_file" "package" {
  type        = "zip"
  source_dir  = local.build_dir
  output_path = "${path.module}/lambda-build/ops-mcp-${var.environment_name}-${terraform_data.package.id}.zip"
}

# =========================================================================
# The Lambda's role: read-only, on named resources
# =========================================================================

data "aws_iam_policy_document" "assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Named "...-lambda-exec" so it falls under the role name pattern the CI deploy role may already
# manage and pass (infra/bootstrap/main.tf's LambdaExecRole: <prefix>-*-lambda-exec). It is
# NOT the role of that name the pipeline Lambdas share: that one is <prefix>-<env>-lambda-exec.
resource "aws_iam_role" "ops_mcp" {
  name               = "${local.name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Everything this function may do. The rule for adding to it: only read actions, only on
# resources named here, and only for a tool that exists or is next (section 1's table). The one
# write the design allows, on the assistant's own suggestions table, is in a policy of its own
# beside that table (memory.tf); nothing else ever should be. tests/ops_assistant.tftest.hcl and
# test_terraform_wiring.py both fail if a write action appears here, or anywhere but on that
# table.
data "aws_iam_policy_document" "ops_mcp" {
  # The tables the tools read, and their indexes (an index has its own ARN, <table>/index/<name>,
  # which the table's ARN does not cover; common/dynamo.py Queries the Articles, ModerationQueue
  # and SecurityEvents ones). Scan is here because the "list everything" reads are Scans
  # (list_topics, list_failed_executions); BatchGetItem because the week's Stats row is summed
  # from several items fetched together.
  statement {
    sid    = "ReadAppTables"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:BatchGetItem",
    ]
    resources = concat(
      [for table in values(var.tables) : table.arn],
      [for table in values(var.tables) : "${table.arn}/index/*"],
    )
  }

  # Article bodies, for the content checks (an article whose whole body is one code fence).
  # articles/ only: the bucket also holds raw source snapshots, which no tool reads.
  statement {
    sid       = "ReadArticleBodies"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.content_bucket_arn}/articles/*"]
  }

  # The alarms tool: what is in ALARM, and since when. Every alarm in this account and region,
  # not <prefix>-* only, although the tool will ask for that prefix: a DescribeAlarms call that
  # lists by prefix is checked against "alarm:*", so a narrower resource here would refuse the
  # very call the tool makes. It reads names and states, and this account holds no other alarms.
  #
  # That includes the OTHER environment's alarms: dev and production are one account. IAM cannot
  # separate them here (the tag Deny in isolation.tf has no single alarm to look at on a list
  # call), so the tool does: it asks only for "<prefix>-<this environment>-", which is how
  # infra/modules/observability names every alarm, and drops anything else that comes back.
  statement {
    sid       = "DescribeAlarms"
    effect    = "Allow"
    actions   = ["cloudwatch:DescribeAlarms"]
    resources = ["arn:aws:cloudwatch:${local.aws_region}:*:alarm:*"]
  }

  # table_sample (lambdas/ops_mcp/samples.py): a few rows of any of the project's tables, for
  # "what is in this table?" and "is it being written as expected?". The rule is the tags, not a
  # list: a table named <prefix>-* is readable only if it carries this project's default tags
  # (ManagedBy and Project, exactly as the root's provider puts them) AND an Environment this
  # assistant may read (locals.readable_environments: its own; production also "shared"). Three
  # conditions, ANDed. Read actions only, and ListTagsOfResource so the code can check the same
  # tags itself before it reads (it does, and refuses on any difference).
  #
  # This rests on DynamoDB's tag-based access control. Where it is not on for the account and
  # region (the DynamoDB console's Settings page), a tag condition sees no tags and the statement
  # grants nothing: it fails closed, and table_sample says AWS refused. The tables named in
  # ReadAppTables above stay readable by name either way.
  statement {
    sid    = "SampleTaggedTables"
    effect = "Allow"
    actions = [
      "dynamodb:Query",
      "dynamodb:Scan",
      "dynamodb:ListTagsOfResource",
    ]
    resources = [
      "arn:aws:dynamodb:${local.aws_region}:*:table/${var.unique_name_prefix}-*",
      "arn:aws:dynamodb:${local.aws_region}:*:table/${var.unique_name_prefix}-*/index/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/ManagedBy"
      values   = [var.default_tags["ManagedBy"]]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Project"
      values   = [var.default_tags["Project"]]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:ResourceTag/Environment"
      values   = local.readable_environments
    }
  }


  # Its own log group and no other. No CreateLogGroup: Terraform creates the group below, before
  # the function exists.
  statement {
    sid    = "OwnLogGroup"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ops_mcp" {
  name   = "${local.name}-read-only"
  role   = aws_iam_role.ops_mcp.id
  policy = data.aws_iam_policy_document.ops_mcp.json
}

# =========================================================================
# The Lambda
# =========================================================================

# Created before the function (depends_on below), for the reason every other Lambda's is: a
# function invoked before its log group exists makes its own, with no retention, and the next
# apply fails with ResourceAlreadyExistsException.
resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${local.name}"
  retention_in_days = var.log_retention_days
}

# The settings the adapter's README asks of a zip-packaged function:
#
# - layers: the adapter (see local.web_adapter_layer_arn).
# - AWS_LAMBDA_EXEC_WRAPPER = /opt/bootstrap: the layer's wrapper script, which Lambda runs in
#   place of the runtime's own start-up. It execs "$LAMBDA_TASK_ROOT/$_HANDLER".
# - handler = run.sh: so that is what it execs. Not a Python "module.function" on purpose.
# - AWS_LWA_PORT: where the adapter sends requests; run.sh starts uvicorn on the same port.
#
# Left at the adapter's default, deliberately: invoke mode stays "buffered" (AWS_LWA_INVOKE_MODE
# unset). The server answers every request with one JSON object, so nothing here is, or should
# become, a response stream.
#
# 512 MB, not the 256 MB the other API Lambdas have: a cold start imports the SDK, pydantic and
# uvicorn before the first request can be answered, and Lambda gives CPU in proportion to memory.
# 30 s matches the other API Lambdas; API Gateway gives up at 29 s whatever this says.
resource "aws_lambda_function" "ops_mcp" {
  function_name = local.name
  # The log group first (see it), and the policy too: a function that exists before its role can
  # read anything would answer its first requests with AccessDenied.
  depends_on    = [aws_cloudwatch_log_group.lambda, aws_iam_role_policy.ops_mcp, aws_iam_role_policy.ops_mcp_memory, aws_iam_role_policy.ops_mcp_briefings, aws_iam_role_policy.ops_mcp_firewall, aws_iam_role_policy.ops_mcp_logs]
  role          = aws_iam_role.ops_mcp.arn
  handler       = "run.sh"
  runtime       = "python3.11"
  architectures = ["x86_64"]
  timeout       = 30
  memory_size   = 512
  layers        = [local.web_adapter_layer_arn]

  filename         = data.archive_file.package.output_path
  source_code_hash = data.archive_file.package.output_base64sha256

  environment {
    variables = merge(
      # One variable per table, named as common/dynamo.py looks it up (TOPICS_TABLE, ...): the
      # caller's var.tables is keyed by exactly those names. A table that is not in the map is a
      # table this function is neither told about nor allowed to read.
      { for env_name, table in var.tables : env_name => table.name },
      {
        AWS_LAMBDA_EXEC_WRAPPER = "/opt/bootstrap"
        AWS_LWA_PORT            = local.web_adapter_port

        # "Is the app up?" is asked by opening the port, not by sending it a request. The
        # adapter's default is GET /, counted as ready on any status from 100 to 499. That
        # request would go through the server's access check like any other (it reads the
        # config table, and answers 403 to a caller it cannot place), so a cold start would cost
        # a DynamoDB read and depend on what the check answers. A TCP check depends on nothing
        # but uvicorn listening.
        AWS_LWA_READINESS_CHECK_PROTOCOL = "tcp"

        # The assistant_access switch (design, section 5): the server reads the config table's
        # `pipeline` row on every request (MODEL_CONFIG_TABLE, from var.tables above) and, when
        # the row says "allowlist", admits only these addresses. It takes the caller's address
        # from the x-amzn-request-context header, which the adapter adds to every request it
        # forwards (identity.sourceIp, as API Gateway recorded it; a caller cannot set it).
        OPS_ASSISTANT_ALLOWED_CIDRS = join(",", var.allowed_cidrs)

        # The key the agent (agent.tf) sends with the operator's address, since its own requests
        # arrive here from an address of Lambda's. A request carrying this key is judged, under
        # "allowlist", by the address it vouches for (ops_mcp/access.py). Empty: no request is
        # taken as the agent's, and "allowlist" refuses every question asked through it.
        OPS_AGENT_FORWARD_KEY = var.agent_forward_key

        CONTENT_BUCKET = var.content_bucket_name

        # table_sample: the tags a table must carry, and the Environment tags it may read. The code
        # checks a table's own tags against both before reading a row; IAM checks the same
        # (SampleTaggedTables above), from the same values.
        OPS_DEFAULT_TAGS          = jsonencode(var.default_tags)
        OPS_READABLE_ENVIRONMENTS = join(",", local.readable_environments)

        # Which environment this assistant is for. The alarms tool builds the only prefix it asks
        # CloudWatch for from it, "<prefix>-<environment>-", because the role cannot be held
        # to one environment's alarms (see DescribeAlarms above); with this unset the tool
        # refuses, it does not fall back to every <prefix>- alarm (ops_mcp/account.py).
        ENVIRONMENT_NAME = var.environment_name

        # What every resource name in this deployment starts with. The catalogue of resources
        # (ops_mcp/architecture.py), the alarm prefix and the firewall's log group names are
        # all built from it, so the assistant never assumes the original deployment's names.
        # An environment variable, not an SSM parameter: it costs nothing per cold start and
        # needs no permission.
        NAME_PREFIX = var.unique_name_prefix

        # Whether the tools may report what is the whole account's (the AWS bill; later the
        # shared firewall). The code switches it on for the exact word "true" and nothing else.
        OPS_ACCOUNT_WIDE_DATA = var.account_wide_data ? "true" : "false"

        # The assistant's own table (memory.tf), the one thing it may write to: what it has
        # suggested and what it is watching (ops_mcp/memory.py).
        OPERATOR_SUGGESTIONS_TABLE = aws_dynamodb_table.operator_suggestions.name
        # The latest briefing per user, and the agent to start one with (briefings.tf).
        OPS_BRIEFINGS_TABLE = aws_dynamodb_table.briefings.name
        OPS_AGENT_FUNCTION  = aws_lambda_function.ops_agent.arn
        # The firewall's log groups, as "<region>:<name>", only where firewall_review may exist
        # (firewall.tf); empty everywhere else, and then the tool is not registered.
        OPS_WAF_LOG_GROUPS = local.firewall_enabled ? join(",", [for group in var.waf_log_groups : "${group.region}:${group.name}"]) : ""

        # The SDK refuses any request whose Host header is not on this list (421), and an empty
        # list refuses everything (ops_mcp/server.py). Behind API Gateway the Host a caller sends
        # is the API's own execute-api domain, so that is the one value. If this API is ever put
        # behind a custom domain or a CloudFront distribution, that name must be added here.
        OPS_MCP_ALLOWED_HOSTS = local.api_host

        # Origins allowed to call from a browser. Empty by default: the page talks to the agent,
        # and the agent (a Lambda, which sends no Origin header) talks to this server.
        OPS_MCP_ALLOWED_ORIGINS = join(",", var.allowed_origins)
      },
    )
  }
}

# =========================================================================
# Sign-in: a Cognito user pool
# =========================================================================

# One pool per environment: a token from dev's is worthless at production's.
#
# - Nobody can sign themselves up. Users are created by an administrator, by hand, with the
#   operator's own IAM credentials (aws cognito-idp admin-create-user, against the pool id this
#   module outputs). Terraform creates none: a user's password would end up in the state file.
# - Sign-in is by a plain username. No email address or phone number is asked for, stored or
#   verified, so the pool holds no personal data and Cognito never has a message to send.
# - A forgotten password is reset by an administrator only, for the same reason: there is no
#   mailbox to send a code to, and none that could be taken over to obtain one.
# - MFA follows var.mfa_configuration. The authenticator-app method is switched on whenever MFA
#   is not OFF: Cognito refuses OPTIONAL or ON with no method enabled, and SMS would cost money
#   and need a phone number.
# - Every sign-in passes through one Lambda, before the password is checked and again once the
#   user is in (lambda_config): it logs the attempt and the success, and refuses a user with too
#   many failures (lambdas/common/sign_ins.py). Cognito's own back-off on wrong passwords is
#   still there underneath, but it is neither recorded nor announced.
resource "aws_cognito_user_pool" "this" {
  name = "${var.unique_name_prefix}-${var.environment_name}-ops-assistant"

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  mfa_configuration = var.mfa_configuration

  dynamic "software_token_mfa_configuration" {
    for_each = var.mfa_configuration == "OFF" ? [] : [1]
    content {
      enabled = true
    }
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "admin_only"
      priority = 1
    }
  }

  lambda_config {
    pre_authentication  = var.sign_in_trigger_function_arn
    post_authentication = var.sign_in_trigger_function_arn
  }
}

# The pool may invoke the sign-in function, and only this pool: without source_arn any user pool
# in any account could.
resource "aws_lambda_permission" "sign_in_trigger" {
  statement_id  = "AllowUserPoolSignInTriggers"
  action        = "lambda:InvokeFunction"
  function_name = var.sign_in_trigger_function_name
  principal     = "cognito-idp.amazonaws.com"
  source_arn    = aws_cognito_user_pool.this.arn
}

# Cognito's hosted sign-in page, at <prefix>.auth.<region>.amazoncognito.com. The prefix is
# unique across every AWS account in the region, so it is a variable: if someone else holds the
# name, the apply says so and the caller picks another.
resource "aws_cognito_user_pool_domain" "this" {
  domain       = var.hosted_ui_domain_prefix
  user_pool_id = aws_cognito_user_pool.this.id
}

# What the API is, to Cognito, and the one thing a token can be allowed to do with it. An access
# token issued for this scope is what the authorizer below looks for.
#
# No apostrophes in these strings: Cognito only allows a resource server's name to match
# [\w\s+=,.@-]+, and says so at apply, not at validate or plan. "operator's" stopped the first
# dev apply part-way, with the user pool created and this refused.
resource "aws_cognito_resource_server" "ops" {
  identifier   = local.resource_server_identifier
  name         = "BloggerBear operator assistant"
  user_pool_id = aws_cognito_user_pool.this.id

  scope {
    scope_name        = "read"
    scope_description = "Read the state of the pipeline through the operator assistant."
  }
}

# The sign-in page's client: authorization code with PKCE, and no client secret.
#
# - generate_secret = false: the client is a static web page, and anything shipped to a browser
#   is public. A secret there would protect nothing; PKCE is what protects the code exchange.
#   (Cognito accepts PKCE from any client and has no setting that demands it; a client with no
#   secret is what makes it the only thing standing behind the code. The page must send it.)
# - "code" only: the implicit flow, which puts tokens in the URL, is not enabled.
# - explicit_auth_flows: SRP is what the hosted page itself uses; the refresh flow cannot be
#   left out. No flow that sends a password to this client directly is enabled.
# - Tokens are short: an hour for the access token, a day for the refresh token. The page keeps
#   them in memory only, so a refresh token outlives the tab only if it was stolen.
resource "aws_cognito_user_pool_client" "page" {
  name         = "${local.name}-page"
  user_pool_id = aws_cognito_user_pool.this.id

  generate_secret = false

  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", local.read_scope]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = var.callback_urls
  logout_urls                          = var.logout_urls

  explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true

  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 1

  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # local.read_scope is a literal, so nothing above tells Terraform the scope has to exist first.
  depends_on = [aws_cognito_resource_server.ops]
}

# =========================================================================
# The API: POST /mcp, behind the Cognito authorizer
# =========================================================================

# Built here and not with infra/modules/rest-api, which the admin and public APIs use: that
# module gives every route one of two authorization types, "AWS_IAM" or "NONE" (its variable
# refuses anything else), and its methods have nowhere to put an authorizer id or the scopes a
# token must carry. Teaching it both would touch the two APIs that are live for one route here.
# What it does that matters is repeated below the same way: a regional endpoint, a deployment
# that is replaced when the route changes, access logs with no caller details, stage throttling.
resource "aws_api_gateway_rest_api" "this" {
  name = local.name

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "mcp"
}

# API Gateway checks the token against the pool before the Lambda is invoked: a request with no
# token, an expired one or one from another pool is answered 401 here and costs no Lambda time.
resource "aws_api_gateway_authorizer" "cognito" {
  name            = "${local.name}-cognito"
  rest_api_id     = aws_api_gateway_rest_api.this.id
  type            = "COGNITO_USER_POOLS"
  provider_arns   = [aws_cognito_user_pool.this.arn]
  identity_source = "method.request.header.Authorization"
}

# The one route. POST only: the server is stateless and answers each POST with one JSON object,
# so there is no GET stream to open and no DELETE to end a session (ops_mcp/server.py).
#
# authorization_scopes is what makes this an access-token check: with a scope listed, API
# Gateway requires an access token carrying it (403 without), and an ID token is not enough.
resource "aws_api_gateway_method" "mcp" {
  rest_api_id          = aws_api_gateway_rest_api.this.id
  resource_id          = aws_api_gateway_resource.mcp.id
  http_method          = "POST"
  authorization        = "COGNITO_USER_POOLS"
  authorizer_id        = aws_api_gateway_authorizer.cognito.id
  authorization_scopes = [local.read_scope]
}

# An ordinary Lambda proxy integration. The response transfer mode is left at its default
# (buffered): no response streaming is configured here, on the function, or in the adapter.
resource "aws_api_gateway_integration" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp.http_method
  # AWS_PROXY integrations always call Lambda with POST, whatever the route's own method is.
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.ops_mcp.invoke_arn
}

# A deployment is a snapshot; the stage serves whichever one it points at. The trigger hashes
# what the snapshot must reflect, by value where an id would not change: switching the method's
# authorization or its scopes updates the method in place and keeps its id, and a stage left on
# the old snapshot would go on serving the old rule.
resource "aws_api_gateway_deployment" "this" {
  rest_api_id = aws_api_gateway_rest_api.this.id

  triggers = {
    redeployment = sha1(jsonencode({
      resource      = aws_api_gateway_resource.mcp.id
      method        = aws_api_gateway_method.mcp.id
      authorization = aws_api_gateway_method.mcp.authorization
      authorizer    = aws_api_gateway_authorizer.cognito.id
      user_pools    = aws_api_gateway_authorizer.cognito.provider_arns
      scopes        = aws_api_gateway_method.mcp.authorization_scopes
      integration   = aws_api_gateway_integration.mcp.id
      uri           = aws_api_gateway_integration.mcp.uri
      # The agent's two routes on /ask (agent.tf), by the same rule.
      agent = local.agent_redeployment
      # The OAuth metadata documents (alexa.tf), by the same rule.
      alexa = local.alexa_redeployment
    }))
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Access logs in the shape infra/modules/rest-api writes them, so the same Logs Insights queries
# read both: one JSON line per request saying what happened to it, and nothing about who sent
# it. No IP address, no user agent, and no token claim either: which signed-in user called is
# not logged. errorType is where a refused token shows (UNAUTHORIZED, ACCESS_DENIED), which is
# what a count of failed token checks will be built on. There is no wafStatus: no web ACL is
# attached (the design leaves the address allowlist to a setting the code reads, section 5).
resource "aws_api_gateway_stage" "this" {
  # checkov:skip=CKV2_AWS_29:every route but the OAuth metadata needs a Cognito token, and Alexa+ calls from Amazon's addresses; see the comment above
  # checkov:skip=CKV2_AWS_4:execution logging is off on purpose, as on the other APIs; the access log above records every request
  rest_api_id   = aws_api_gateway_rest_api.this.id
  deployment_id = aws_api_gateway_deployment.this.id
  stage_name    = var.stage_name

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.access.arn
    format = join("", [
      "{",
      "\"requestId\":\"$context.requestId\",",
      "\"requestTime\":\"$context.requestTime\",",
      "\"httpMethod\":\"$context.httpMethod\",",
      "\"resourcePath\":\"$context.resourcePath\",",
      "\"status\":$context.status,",
      "\"responseLatency\":$context.responseLatency,",
      "\"integrationLatency\":\"$context.integrationLatency\",",
      "\"responseLength\":\"$context.responseLength\",",
      "\"errorType\":\"$context.error.responseType\"",
      "}",
    ])
  }
}

# Written through the account-wide CloudWatch Logs role infra/bootstrap sets
# (aws_api_gateway_account), like the other APIs' access logs. The name keeps their prefix,
# /aws/apigateway/<prefix>-*, which is what the deploy role may create.
resource "aws_cloudwatch_log_group" "access" {
  name              = "/aws/apigateway/${local.name}-access"
  retention_in_days = var.access_log_retention_days
}

# A ceiling for the whole API: beyond it API Gateway answers 429 and the Lambda is not invoked.
# It bounds what a stolen token, or a client stuck in a loop, can cost. Per-method metrics and
# execution logging stay off, as on the other APIs.
resource "aws_api_gateway_method_settings" "all" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  stage_name  = aws_api_gateway_stage.this.stage_name
  method_path = "*/*"

  settings {
    throttling_rate_limit  = var.throttling_rate_limit
    throttling_burst_limit = var.throttling_burst_limit
    metrics_enabled        = false
    logging_level          = "OFF"
  }
}

# Only this API, and only its one route, may invoke the function.
resource "aws_lambda_permission" "apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ops_mcp.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/POST/mcp"
}
