# The agent half of the module (agent.tf), planned with a mocked AWS provider like
# ops_assistant.tftest.hcl next to this file (its header says how, and why the policy documents'
# `json` is given a value). What is held here is what "the agent can spend money on the model and
# do nothing else" rests on: what its role may do, that POST /ask cannot be called without a token
# carrying the scope, that the preflight is the only thing that can, and that nothing switches
# tracing on.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111111111111"
    }
  }
}

variables {
  # What a root passes when UNIQUE_NAME_PREFIX is not set: the original deployment's prefix.
  unique_name_prefix = "bloggerbear"

  # What a root passes when nothing is set: the original deployment's region.
  aws_region = "ap-southeast-2"

  # The roots' provider default_tags, without Environment and TerraformRoot.
  default_tags = {
    ManagedBy = "Terraform"
    Project   = "BloggerBear"
  }

  environment_name = "test"
  tables = {
    TOPICS_TABLE = {
      name = "bloggerbear-test-topics"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-topics"
    }
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-test-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"
    }
  }
  content_bucket_name           = "bloggerbear-test-content"
  sign_in_trigger_function_arn  = "arn:aws:lambda:ap-southeast-2:123456789012:function:bloggerbear-test-sign-in-events"
  sign_in_trigger_function_name = "bloggerbear-test-sign-in-events"
  content_bucket_arn            = "arn:aws:s3:::bloggerbear-test-content"
  stage_name                    = "test"
  hosted_ui_domain_prefix       = "bloggerbear-test-ops"
  callback_urls                 = ["https://example.com/ask.html"]
  logout_urls                   = ["https://example.com/ask.html"]
  mfa_configuration             = "OPTIONAL"
  throttling_rate_limit         = 5
  throttling_burst_limit        = 10
  allowed_cidrs                 = ["203.0.113.0/24", "2001:db8::/32"]
  agent_model_id                = "au.example.test-model-v1:0"
  agent_allowed_origin          = "https://example.com"
  agent_forward_key             = "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
}

run "the_agent_role_can_invoke_the_model_and_read_one_row" {
  command = plan

  # An allowlist, like the MCP server's: an action nobody thought to forbid fails this too.
  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_agent.statement[*].actions)) == toset([
      "bedrock:InvokeModel",
      "dynamodb:GetItem",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ])
    error_message = "the agent's role allows something other than: invoke the model, read the access switch, write its log"
  }

  assert {
    condition = alltrue([
      for action in flatten(data.aws_iam_policy_document.ops_agent.statement[*].actions) :
      !strcontains(action, "*") && !startswith(action, "s3:")
    ])
    error_message = "no action in the agent's role may be a wildcard, and none may be on S3"
  }

  # Said on its own, so the message names the rule: the one DynamoDB action is GetItem.
  assert {
    condition = toset([
      for action in flatten(data.aws_iam_policy_document.ops_agent.statement[*].actions) :
      action if startswith(action, "dynamodb:")
    ]) == toset(["dynamodb:GetItem"])
    error_message = "the agent may read the access switch and must not be able to write it, or anything else in DynamoDB"
  }

  assert {
    condition = alltrue([
      for statement in data.aws_iam_policy_document.ops_agent.statement : statement.effect == "Allow"
    ])
    error_message = "the role's policy is a list of what is allowed; nothing else is expected in it"
  }

  # The config table, by its own ARN: no index, and none of the other tables the module is handed.
  assert {
    condition     = toset(data.aws_iam_policy_document.ops_agent.statement[1].resources) == toset(["arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"])
    error_message = "the DynamoDB statement should cover the config table and nothing else"
  }

  # The two resources the shared role's Bedrock statement names.
  assert {
    condition = toset(data.aws_iam_policy_document.ops_agent.statement[0].resources) == toset([
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:ap-southeast-2:111111111111:inference-profile/*",
    ])
    error_message = "the Bedrock statement should name foundation models and this account's inference profiles, as the shared role's does"
  }

  assert {
    condition     = aws_iam_role.ops_agent.name == "bloggerbear-test-ops-agent-lambda-exec"
    error_message = "the agent has a role of its own, named to fit the pattern the deploy role may create"
  }

  assert {
    condition     = aws_iam_role.ops_agent.name != aws_iam_role.ops_mcp.name
    error_message = "the agent must not run as the MCP server's role"
  }
}

run "post_ask_needs_a_token_with_the_scope_and_the_preflight_needs_none" {
  command = plan

  assert {
    condition     = aws_api_gateway_resource.ask.path_part == "ask" && aws_api_gateway_method.ask.http_method == "POST"
    error_message = "the agent's route is POST /ask"
  }

  assert {
    condition     = aws_api_gateway_method.ask.authorization == "COGNITO_USER_POOLS"
    error_message = "POST /ask must sit behind the Cognito authorizer"
  }

  assert {
    condition     = aws_api_gateway_method.ask.authorization_scopes == toset(["bloggerbear-ops/read"])
    error_message = "POST /ask must require the bloggerbear-ops/read scope, as POST /mcp does"
  }

  assert {
    condition     = aws_api_gateway_method.ask.authorization_scopes == aws_api_gateway_method.mcp.authorization_scopes
    error_message = "/ask and /mcp must ask for the same scope: the agent passes the caller's token on"
  }

  assert {
    condition     = aws_api_gateway_method.ask_options.http_method == "OPTIONS" && aws_api_gateway_method.ask_options.authorization == "NONE"
    error_message = "the preflight carries no token, so OPTIONS /ask can have no authorizer"
  }

  assert {
    condition     = aws_api_gateway_method.ask_options.authorization_scopes == null
    error_message = "OPTIONS /ask asks for no scope"
  }

  assert {
    condition     = aws_api_gateway_integration.ask.type == "AWS_PROXY" && aws_api_gateway_integration.ask_options.type == "AWS_PROXY"
    error_message = "both methods are plain Lambda proxy integrations"
  }

  assert {
    condition     = aws_lambda_permission.agent_ask.action == "lambda:InvokeFunction" && aws_lambda_permission.agent_ask_options.action == "lambda:InvokeFunction"
    error_message = "API Gateway is allowed to invoke the function, and nothing more"
  }
}

run "api_gateways_own_errors_can_be_read_by_the_one_origin" {
  command = plan

  assert {
    condition     = toset(keys(aws_api_gateway_gateway_response.cors)) == toset(["UNAUTHORIZED", "ACCESS_DENIED", "THROTTLED", "DEFAULT_5XX"])
    error_message = "the 401, the 403, the 429 and the 5xx API Gateway answers by itself each need the CORS header"
  }

  assert {
    condition = alltrue([
      for type, response in aws_api_gateway_gateway_response.cors :
      tomap({ for name, value in response.response_parameters : name => value if name != "gatewayresponse.header.WWW-Authenticate" }) == tomap({ "gatewayresponse.header.Access-Control-Allow-Origin" = "'https://example.com'" })
    ])
    error_message = "each gateway response names the one allowed origin, never *, and sets no other header but the 401's WWW-Authenticate (alexa.tf)"
  }

  # Nothing but the header: API Gateway keeps its own status and body.
  assert {
    condition = alltrue([
      for response in aws_api_gateway_gateway_response.cors :
      response.status_code == null && response.response_templates == null
    ])
    error_message = "the gateway responses must not change the status or the body"
  }
}

run "with_no_origin_configured_no_page_can_read_the_errors" {
  command = plan
  variables {
    agent_allowed_origin = ""
  }

  assert {
    condition = alltrue([
      for type, response in aws_api_gateway_gateway_response.cors :
      length({ for name, value in response.response_parameters : name => value if name != "gatewayresponse.header.WWW-Authenticate" }) == 0
    ])
    error_message = "an empty origin must leave the header off, not answer with an empty or wildcard origin"
  }
}

run "the_agent_is_a_plain_python_function_with_a_ceiling" {
  command = plan

  assert {
    condition     = aws_lambda_function.ops_agent.function_name == "bloggerbear-test-ops-agent"
    error_message = "the function is bloggerbear-<env>-ops-agent"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.handler == "ops_agent_handler.handler" && aws_lambda_function.ops_agent.runtime == "python3.11"
    error_message = "a Python handler on the 3.11 runtime, which the package's wheels are built for"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.layers == null
    error_message = "no layer: the Web Adapter is for the MCP server's web app, and this is a plain handler"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.timeout == 29
    error_message = "29 seconds: API Gateway gives up then, and a longer run is paid for with nobody listening"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.memory_size == 1024 && aws_lambda_function.ops_agent.reserved_concurrent_executions == -1
    error_message = "memory and reserved concurrency follow their variables, whose defaults are 1024 and -1 (no reservation: this account has no concurrency to reserve)"
  }

  assert {
    condition     = aws_cloudwatch_log_group.agent.name == "/aws/lambda/bloggerbear-test-ops-agent"
    error_message = "the log group is the one the function writes to, created by Terraform"
  }
}

run "the_agent_is_told_what_it_needs_and_tracing_is_not_switched_on" {
  command = plan

  # Exactly these: a variable added later has to be added here on purpose.
  assert {
    condition = toset(keys(aws_lambda_function.ops_agent.environment[0].variables)) == toset([
      "OPS_AGENT_MODEL_ID",
      # What every resource name starts with (var.unique_name_prefix), as on every function.
      "NAME_PREFIX",
      "OPS_MCP_URL",
      "OPS_AGENT_ALLOWED_ORIGIN",
      "MODEL_CONFIG_TABLE",
      "OPS_ASSISTANT_ALLOWED_CIDRS",
      "OPS_AGENT_FORWARD_KEY",
      "OPS_BRIEFINGS_TABLE",
      "OPS_AGENT_DAILY_QUESTION_CAP",
      "STATS_CURRENT_TABLE",
    ])
    error_message = "the agent's environment holds a variable this test does not expect, or lacks one it does"
  }

  # The key the agent vouches for the operator's address with: the caller's, on both functions.
  assert {
    condition     = nonsensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_FORWARD_KEY) == "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
    error_message = "the agent is given the caller's forward key, unchanged"
  }

  assert {
    condition     = nonsensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_FORWARD_KEY == aws_lambda_function.ops_mcp.environment[0].variables.OPS_AGENT_FORWARD_KEY)
    error_message = "the agent and the MCP server must hold the same forward key, or allowlist refuses every question"
  }

  assert {
    condition     = issensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_FORWARD_KEY) && issensitive(aws_lambda_function.ops_mcp.environment[0].variables.OPS_AGENT_FORWARD_KEY)
    error_message = "the forward key is sensitive, so a plan must not print it on either function"
  }

  assert {
    condition = alltrue([
      for name in keys(aws_lambda_function.ops_agent.environment[0].variables) : !startswith(name, "OTEL_")
    ])
    error_message = "no OTEL_ variable: a trace of an agent run would carry the question and the answer"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_MODEL_ID == "au.example.test-model-v1:0"
    error_message = "the model id is the caller's, passed through unchanged"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_ALLOWED_ORIGIN == "https://example.com"
    error_message = "the allowed origin is the caller's"
  }

  assert {
    condition     = aws_lambda_function.ops_agent.environment[0].variables.MODEL_CONFIG_TABLE == "bloggerbear-test-model-config"
    error_message = "the handler reads the access switch from the config table, so it must be told its name"
  }

  # The same list, joined the same way, as the MCP server is given.
  assert {
    condition     = nonsensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_ASSISTANT_ALLOWED_CIDRS) == "203.0.113.0/24,2001:db8::/32"
    error_message = "the agent is given the operator's addresses for the allowlist setting"
  }

  assert {
    condition     = nonsensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_ASSISTANT_ALLOWED_CIDRS == aws_lambda_function.ops_mcp.environment[0].variables.OPS_ASSISTANT_ALLOWED_CIDRS)
    error_message = "the agent and the MCP server must be given the same allowlist"
  }
}

run "a_reservation_can_be_set_once_the_account_has_room" {
  command = plan
  variables {
    agent_reserved_concurrency = 2
  }
  assert {
    condition     = aws_lambda_function.ops_agent.reserved_concurrent_executions == 2
    error_message = "the reservation should follow the variable"
  }
}

run "the_agent_cannot_be_switched_off_by_a_reservation_of_zero" {
  command = plan
  variables {
    agent_reserved_concurrency = 0
  }
  expect_failures = [var.agent_reserved_concurrency]
}

# The code ignores a key under 32 characters (ops_mcp/access.py), so one would look set and do
# nothing; and the key is sent as an HTTP header, so it is letters and digits only.
run "a_forward_key_too_short_to_count_is_refused" {
  command = plan
  variables {
    agent_forward_key = "kkkkkkkkkkkkkkkkkkkkkkkkkkkkkkk"
  }
  expect_failures = [var.agent_forward_key]
}

run "a_forward_key_that_could_not_be_a_header_is_refused" {
  command = plan
  variables {
    agent_forward_key = "kkkkkkkkkkkkkkkkkkkk kkkkkkkkkkkkkkkkkkkk"
  }
  expect_failures = [var.agent_forward_key]
}

run "with_no_forward_key_both_functions_hold_none" {
  command = plan
  variables {
    agent_forward_key = ""
  }

  assert {
    condition     = nonsensitive(aws_lambda_function.ops_agent.environment[0].variables.OPS_AGENT_FORWARD_KEY) == "" && nonsensitive(aws_lambda_function.ops_mcp.environment[0].variables.OPS_AGENT_FORWARD_KEY) == ""
    error_message = "an empty key is passed on as empty, which switches vouching off on both sides"
  }
}

# An inference profile is a resource of the region it is called in, so the profile half of the
# Bedrock statement follows var.aws_region. The foundation-model half stays a wildcard on the
# region in every deployment: a cross-region profile routes to models in several.
run "another_region_moves_the_inference_profiles_the_agent_may_call" {
  command = plan

  variables {
    aws_region = "eu-west-1"
  }

  assert {
    condition = toset(data.aws_iam_policy_document.ops_agent.statement[0].resources) == toset([
      "arn:aws:bedrock:*::foundation-model/*",
      "arn:aws:bedrock:eu-west-1:111111111111:inference-profile/*",
    ])
    error_message = "the profiles the agent may call are this account's in the home region; foundation models stay any-region"
  }
}
