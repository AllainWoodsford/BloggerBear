# -----------------------------------------------------------------------
# The operator's assistant, second half: the agent (lambdas/ops_agent and
# lambdas/ops_agent_handler.py), deployed next to the MCP server in main.tf.
# Design: docs/enhancements/alexa-plus-operator-assistant-enhancement.md (sections 2, 5 and 6).
#
#   the page (ask.html), holding the caller's access token
#        | POST /ask            (OPTIONS /ask is the browser's preflight, with no token)
#        v
#   the SAME REST API and Cognito authorizer as /mcp, requiring the same scope
#        | Lambda proxy integration
#        v
#   Lambda -- a plain Python handler: no web app, no Web Adapter layer
#        | its OWN role: invoke the model, read one row, write its own log
#        +--> Bedrock (Converse, through Strands)
#        +--> POST /mcp on this module's own API, with the caller's token passed on
#
# It is a third Lambda package and a third role, each for the reason the MCP server has its own:
#
# 1. Its own deployment package. The agent framework (strands-agents) and what it pulls in are
#    needed by this function alone. Measured 2026-10-04: 48 packages, about 63 MB unpacked and
#    29 MB zipped, against Lambda's 250 MB and 50 MB (the second is the ceiling for a zip uploaded
#    directly, which is how every function here is deployed). If a later release pushes the zip
#    past 50 MB, the function must take its code from S3 (s3_bucket and s3_key in place of
#    filename); the apply says so in its error, it does not fail quietly.
# 2. Its own IAM role. This function can call Bedrock, which the MCP server must never be able to
#    (a tool that could spend money is not "read-only"); and it reads one row of one table, where
#    the MCP server reads nine tables. Neither role contains the other.
#
# Everything is in this file so the agent can be read, and removed, as one piece. The two places
# it touches main.tf: the API deployment's trigger hashes local.agent_redeployment (below), and
# this file reuses that file's REST API, authorizer, assume-role document and locals.
# -----------------------------------------------------------------------

locals {
  agent_name      = "bloggerbear-${var.environment_name}-ops-agent"
  agent_build_dir = "${path.module}/lambda-build/ops-agent-package"

  # The MCP endpoint the agent calls: this module's own. Written out from the API's id and the
  # stage's name, and NOT read from aws_api_gateway_stage.this.invoke_url (which outputs.tf uses
  # for the same URL): the stage depends on the deployment, the deployment's trigger hashes this
  # function's integrations, and the integrations depend on the function. Reading the stage here
  # would close that loop and Terraform would refuse the plan with "Cycle". The host is
  # local.api_host, the one value the MCP server accepts in a Host header (OPS_MCP_ALLOWED_HOSTS),
  # so the two cannot disagree.
  agent_mcp_url = "https://${local.api_host}/${var.stage_name}/mcp"
}

# The account id, for the inference profile ARN in the role below (a profile is the account's
# own resource; a foundation model is not, and its ARN has no account id).
data "aws_caller_identity" "current" {}

# =========================================================================
# The agent's deployment package
# =========================================================================

# Built the way the MCP package is (main.tf's terraform_data.package; its comments explain the
# staging directory, why pip is told the platform, and why this only works on Linux: on Windows
# pip also asks for mcp's Windows-only dependency, pywin32, and stops). What differs:
#
# - What is copied: ops_agent/ and the handler; common/, of which the handler imports dynamo.py
#   (and through ops_mcp/access.py, assistant_access.py), neither of which needs anything pip
#   installs; and from ops_mcp/ two files only, __init__.py and access.py, the access switch's
#   rule. The MCP server itself (server.py, the tools) is not in this package.
# - There is no run.sh: the function's handler is a Python function.
# - requirements.txt is not installed: nothing here imports what it lists (the pipeline's
#   requests, markdown). requirements-ops-agent.txt names strands-agents and
#   includes requirements-ops-mcp.txt, so the MCP client is the release the server is.
#
# Checked 2026-10-04 by resolving the whole tree for the Lambda runtime (Linux x86_64, CPython
# 3.11, binary wheels only): all 48 packages have a wheel that fits. Six are compiled
# (cryptography, pydantic-core, rpds-py, cffi, pyyaml, wrapt) and each has a manylinux2014 or
# older x86_64 cp311 wheel; the rest are pure Python. boto3 and botocore come with strands-agents
# and so are in the zip (they are the largest part of it), newer than the runtime's own copies.
resource "terraform_data" "agent_package" {
  triggers_replace = {
    always_run = timestamp()
  }

  provisioner "local-exec" {
    interpreter = ["bash", "-c"]
    command     = <<-EOT
      set -eu
      build_dir="${local.agent_build_dir}"
      rm -rf "$build_dir"
      mkdir -p "$build_dir/ops_mcp"
      cp -r "${local.lambdas_dir}/ops_agent" "$build_dir/ops_agent"
      cp "${local.lambdas_dir}/ops_agent_handler.py" "$build_dir/ops_agent_handler.py"
      cp -r "${local.lambdas_dir}/common" "$build_dir/common"
      cp "${local.lambdas_dir}/ops_mcp/__init__.py" "$build_dir/ops_mcp/__init__.py"
      cp "${local.lambdas_dir}/ops_mcp/access.py" "$build_dir/ops_mcp/access.py"
      cp "${local.lambdas_dir}/ops_mcp/briefings.py" "$build_dir/ops_mcp/briefings.py"
      find "$build_dir" -type d -name __pycache__ -prune -exec rm -rf {} +
      if python3 -c "" >/dev/null 2>&1; then
        py_cmd="python3"
      else
        py_cmd="py -3"
      fi
      $py_cmd -m pip install --upgrade --no-cache-dir \
        --platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all: \
        -r "${local.lambdas_dir}/requirements-ops-agent.txt" \
        -t "$build_dir"
    EOT
  }
}

# output_path carries the build's id so that Terraform reads the directory at apply, after the
# build, and not during the plan (see data.archive_file.package in main.tf).
data "archive_file" "agent_package" {
  type        = "zip"
  source_dir  = local.agent_build_dir
  output_path = "${path.module}/lambda-build/ops-agent-${var.environment_name}-${terraform_data.agent_package.id}.zip"
}

# =========================================================================
# The agent's role: invoke the model, read the access switch, write its own log
# =========================================================================

# Named "...-lambda-exec" for the reason the MCP server's role is: it falls under the pattern the
# CI deploy role may create and pass (infra/bootstrap/main.tf's LambdaExecRole,
# bloggerbear-*-lambda-exec), so nothing has to be added there. It is neither the role the
# pipeline Lambdas share (bloggerbear-<env>-lambda-exec) nor the MCP server's
# (bloggerbear-<env>-ops-mcp-lambda-exec).
resource "aws_iam_role" "ops_agent" {
  name               = "${local.agent_name}-lambda-exec"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

# Three statements, and the rule for adding a fourth is that there should not be one. The agent
# learns about the pipeline by asking the MCP server, with the caller's token, like any other
# client: it has no table to read for an answer, no bucket, no alarm. So nothing a prompt could
# talk the model into can reach data the tools do not already return. No DynamoDB write and no S3
# action at all: tests/ops_agent.tftest.hcl and test_terraform_wiring.py both fail if one appears.
data "aws_iam_policy_document" "ops_agent" {
  # The same two resources the shared role's BedrockInvoke statement names
  # (infra/environments/*/main.tf, data.aws_iam_policy_document.lambda_exec, whose comment has the
  # full reasoning): the model is reached through a cross-region inference profile, and invoking
  # one needs permission on the profile (this account's, in this region) AND on the foundation
  # models it may route to (any region, and their ARNs carry no account id, so they cannot be
  # narrowed further). InvokeModel alone: the agent calls Converse without streaming
  # (ops_agent/agent.py, streaming=False), so InvokeModelWithResponseStream is not needed.
  statement {
    sid     = "BedrockInvoke"
    effect  = "Allow"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:${local.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    ]
  }

  # The assistant_access switch (design, section 5): the handler reads the config table's
  # `pipeline` row before anything else, on every request. GetItem, on that one table, and no
  # index. It cannot write the row, so a stolen token cannot switch an allowlist back off.
  statement {
    sid       = "ReadAccessSwitch"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [var.tables["MODEL_CONFIG_TABLE"].arn]
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
    resources = ["${aws_cloudwatch_log_group.agent.arn}:*"]
  }
}

resource "aws_iam_role_policy" "ops_agent" {
  name   = "${local.agent_name}-invoke-model"
  role   = aws_iam_role.ops_agent.id
  policy = data.aws_iam_policy_document.ops_agent.json
}

# =========================================================================
# The agent Lambda
# =========================================================================

# Created before the function, like every other Lambda's (see aws_cloudwatch_log_group.lambda).
# What the handler logs is which turn it was, which tools were called and how many findings came
# back: never the question, the answer or the token.
resource "aws_cloudwatch_log_group" "agent" {
  name              = "/aws/lambda/${local.agent_name}"
  retention_in_days = var.log_retention_days
}

# An ordinary Python Lambda: handler = module.function, no layer, no exec wrapper.
#
# - timeout = 29: API Gateway gives up on an integration after 29 seconds and answers 504. A
#   function allowed to run longer would go on calling Bedrock, and being paid for, with nobody
#   left to hear the answer. The agent's own waits are shorter than this (ops_agent/agent.py).
# - memory: a cold start imports strands, the MCP client, pydantic and botocore before the first
#   question can be answered, and Lambda gives CPU in proportion to memory.
# - reserved_concurrent_executions: -1, no reservation, unless the variable says otherwise. A
#   reservation would be a ceiling on how many questions can be at Bedrock at once, so a burst
#   from the page could not take the Bedrock quota the daily authoring cycle needs (design,
#   section 6). But it is set aside from the account's pool, and Lambda refuses one that leaves
#   the account less unreserved concurrency than its minimum. This account's whole quota is that
#   minimum, 10, so the first dev apply failed here (PutFunctionConcurrency: "decreases account's
#   UnreservedConcurrentExecution below its minimum value of [10]"). Until the quota is raised,
#   the stage's throttle below is the only bound on the agent.
resource "aws_lambda_function" "ops_agent" {
  function_name = local.agent_name
  # The log group first, and the policy too: a function that exists before its role can read the
  # access switch would refuse its first requests (which is the safe way to be wrong, but wrong).
  depends_on    = [aws_cloudwatch_log_group.agent, aws_iam_role_policy.ops_agent, aws_iam_role_policy.ops_agent_briefings, aws_iam_role_policy.ops_agent_stats]
  role          = aws_iam_role.ops_agent.arn
  handler       = "ops_agent_handler.handler"
  runtime       = "python3.11"
  architectures = ["x86_64"]
  timeout       = 29
  memory_size   = var.agent_memory_size

  reserved_concurrent_executions = var.agent_reserved_concurrency

  filename         = data.archive_file.agent_package.output_path
  source_code_hash = data.archive_file.agent_package.output_base64sha256

  # Nothing named OTEL_* is set, on purpose. Strands brings OpenTelemetry with it and switches
  # tracing on from the environment; a trace of an agent run carries the prompt and the answer,
  # and the rule for this function is that the operator's words are stored nowhere.
  environment {
    variables = {
      # The Bedrock model or inference profile, passed to Converse as it is given.
      OPS_AGENT_MODEL_ID = var.agent_model_id

      # This module's own MCP endpoint (see local.agent_mcp_url for why it is built by hand).
      OPS_MCP_URL = local.agent_mcp_url

      # The one origin whose pages may read a response (the CORS headers the handler answers
      # with). Empty means no browser can, which is the safe default.
      OPS_AGENT_ALLOWED_ORIGIN = var.agent_allowed_origin

      # The assistant_access switch, enforced here as well as on the MCP server: the handler
      # reads the `pipeline` row of this table first, and under "allowlist" admits only these
      # addresses, the same list the MCP server is given. It takes the caller's address from the
      # event's requestContext.identity.sourceIp, as API Gateway recorded it.
      MODEL_CONFIG_TABLE          = var.tables["MODEL_CONFIG_TABLE"].name
      OPS_ASSISTANT_ALLOWED_CIDRS = join(",", var.allowed_cidrs)

      # The key this function sends to the MCP server with the address of an operator it has
      # admitted, so that under "allowlist" the server judges the request by that address and
      # not by this function's own (ops_mcp/access.py). The same value the server is given.
      OPS_AGENT_FORWARD_KEY = var.agent_forward_key
      # Where each briefing is written for latest_briefing (briefings.tf), and where each
      # user's questions are counted against the daily cap (ops_agent/quota.py).
      OPS_BRIEFINGS_TABLE          = aws_dynamodb_table.briefings.name
      OPS_AGENT_DAILY_QUESTION_CAP = tostring(var.agent_daily_question_cap)
      # Each run's tokens and cost, onto the week's Stats row (common/stats_tracking.py's
      # "assistant" category). Empty where the caller passed no Stats table: then nothing is
      # recorded, and the page and the answer are unaffected.
      STATS_CURRENT_TABLE = local.agent_stats_table == null ? "" : local.agent_stats_table.name
    }
  }
}

# =========================================================================
# The routes: POST /ask behind the authorizer, OPTIONS /ask without
# =========================================================================

resource "aws_api_gateway_resource" "ask" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  parent_id   = aws_api_gateway_rest_api.this.root_resource_id
  path_part   = "ask"
}

# The same check as POST /mcp, by the same authorizer: an access token from this environment's
# pool, carrying the read scope. A request without one is answered 401 by API Gateway and costs
# no Lambda time and no model call.
resource "aws_api_gateway_method" "ask" {
  rest_api_id          = aws_api_gateway_rest_api.this.id
  resource_id          = aws_api_gateway_resource.ask.id
  http_method          = "POST"
  authorization        = "COGNITO_USER_POOLS"
  authorizer_id        = aws_api_gateway_authorizer.cognito.id
  authorization_scopes = [local.read_scope]
}

resource "aws_api_gateway_integration" "ask" {
  rest_api_id = aws_api_gateway_rest_api.this.id
  resource_id = aws_api_gateway_resource.ask.id
  http_method = aws_api_gateway_method.ask.http_method
  # AWS_PROXY integrations always call Lambda with POST, whatever the route's own method is.
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.ops_agent.invoke_arn
}

# The browser's preflight. The page is served from the site's origin and this API is another, so
# before a POST carrying an Authorization header the browser asks, with OPTIONS, whether it may.
# A preflight never carries the token (browsers send it without credentials), so this method can
# have no authorizer: behind one, every preflight would be a 401 and the page could never call.
# It is proxied to the function, which answers 204 with the CORS headers for its one allowed
# origin and does nothing else: no config read, no model, no MCP call.
#
# What that costs: this is the one way to invoke the function without signing in. It is bounded
# by the stage's throttle and by the function's reserved concurrency, and an OPTIONS runs for
# milliseconds; a flood of them could make real questions wait, and could not reach Bedrock.
resource "aws_api_gateway_method" "ask_options" {
  rest_api_id   = aws_api_gateway_rest_api.this.id
  resource_id   = aws_api_gateway_resource.ask.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "ask_options" {
  rest_api_id             = aws_api_gateway_rest_api.this.id
  resource_id             = aws_api_gateway_resource.ask.id
  http_method             = aws_api_gateway_method.ask_options.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.ops_agent.invoke_arn
}

# The answers API Gateway gives by itself, before the function runs, and so without the CORS
# headers the handler puts on its own. The page calls from another origin: a response with no
# Access-Control-Allow-Origin is one the browser will not let it read, and the page is told
# "network error" where it should be told "sign in again" (401) or "slow down" (429).
#
#   UNAUTHORIZED   401  no token, or an expired one, at the Cognito authorizer
#   ACCESS_DENIED  403  a token without the scope
#   THROTTLED      429  the stage's throttle
#   DEFAULT_5XX         everything else that goes wrong on API Gateway's side, including the
#                       504 at 29 seconds and the function refusing an invocation above its
#                       reserved concurrency
#
# Only the header is set. No status code and no template is given, so each keeps API Gateway's
# own status and its own body ({"message": ...}). The origin is the one the function answers
# with, never "*". The single quotes are API Gateway's way of writing a fixed value; they are
# the only apostrophes in this file's code, and they are not part of any name.
#
# A gateway response belongs to the API, not to a route, so /mcp's errors carry the header too.
# That gives a browser nothing: reading a 401 from another origin was never the protection, and
# the MCP server still refuses a request whose Origin it does not expect (OPS_MCP_ALLOWED_ORIGINS).
# With no origin configured the header is left off, and no page can read these, as before.
#
# The 401 carries one more header, WWW-Authenticate, for MCP clients (alexa.tf).
resource "aws_api_gateway_gateway_response" "cors" {
  for_each = toset(["UNAUTHORIZED", "ACCESS_DENIED", "THROTTLED", "DEFAULT_5XX"])

  rest_api_id   = aws_api_gateway_rest_api.this.id
  response_type = each.key

  response_parameters = merge(
    var.agent_allowed_origin == "" ? {} : {
      "gatewayresponse.header.Access-Control-Allow-Origin" = "'${var.agent_allowed_origin}'"
    },
    # A 401 also says where to find out how to sign in (alexa.tf): the MCP authorization spec's
    # discovery starts from this header.
    each.key == "UNAUTHORIZED" ? local.www_authenticate : {},
  )
}

locals {
  # What the API's deployment must be replaced for, on the agent's side. Hashed into
  # aws_api_gateway_deployment.this's trigger in main.tf next to the /mcp route's values, and by
  # value for the same reason: a changed authorization or scope updates a method in place and
  # keeps its id, and a stage left on the old snapshot would go on serving the old rule. Without
  # this, the two routes would exist in the API and be missing from what the stage serves.
  agent_redeployment = {
    resource              = aws_api_gateway_resource.ask.id
    method                = aws_api_gateway_method.ask.id
    authorization         = aws_api_gateway_method.ask.authorization
    authorizer            = aws_api_gateway_method.ask.authorizer_id
    scopes                = aws_api_gateway_method.ask.authorization_scopes
    integration           = aws_api_gateway_integration.ask.id
    uri                   = aws_api_gateway_integration.ask.uri
    options_method        = aws_api_gateway_method.ask_options.id
    options_authorization = aws_api_gateway_method.ask_options.authorization
    options_integration   = aws_api_gateway_integration.ask_options.id
    options_uri           = aws_api_gateway_integration.ask_options.uri
    # A gateway response is part of the snapshot too: changed and not redeployed, the stage goes
    # on answering with the old headers.
    gateway_responses = {
      for response_type, response in aws_api_gateway_gateway_response.cors :
      response_type => response.response_parameters
    }
  }
}

# Only this API, and only these two methods on /ask, may invoke the function. One permission per
# method, each with the method written out, where a single "/*/*/ask" would also admit a GET or
# a DELETE somebody added to the resource later.
resource "aws_lambda_permission" "agent_ask" {
  statement_id  = "AllowAPIGatewayInvokeAsk"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ops_agent.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/POST/ask"
}

resource "aws_lambda_permission" "agent_ask_options" {
  statement_id  = "AllowAPIGatewayInvokeAskPreflight"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ops_agent.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.this.execution_arn}/*/OPTIONS/ask"
}
