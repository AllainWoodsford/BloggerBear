# Plans the ops-assistant module with a mocked AWS provider (no credentials), so what it would
# create is checked with real values the way `terraform plan` sees them. The three things held
# here are the ones the design's "read-only, behind a sign-in" rests on: what the Lambda's role
# may do, that the route cannot be called without a token carrying the scope, and that MFA is
# whatever the caller asked for. Run by .github/workflows/pr-checks.yml (`terraform test` in each
# module that has a tests/ folder). Nothing is built or zipped: a plan never runs the package's
# provisioner.

# The mock invents a random string for every computed attribute, and the provider still checks
# that a role's policy is JSON, so a policy document's rendered `json` is given a valid value
# here. The assertions below read the documents' statements (what the module wrote), never this.
mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
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
    ARTICLES_TABLE = {
      name = "bloggerbear-test-articles"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-articles"
    }
    # The config table is not optional: the access switch is a row in it, and the agent
    # (agent.tf) is given this one table by name.
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-test-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config"
    }
  }
  agent_model_id                = "au.example.test-model-v1:0"
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
}

# The role has two policies: the read-only one (main.tf) and the one write the design allows, on
# the assistant's own suggestions table (memory.tf). This run holds both: everything is a read,
# except five named actions on that one table.
run "the_role_can_only_read_and_write_its_own_table" {
  command = plan

  # The table's ARN is only known after apply; given here, so the plan can be asked which
  # resource the write statement names.
  override_resource {
    target          = aws_dynamodb_table.operator_suggestions
    override_during = plan
    values = {
      arn = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-operator-suggestions"
    }
  }

  # Exactly these, on exactly that table: no Scan, no batch write, no wildcard, no index.
  assert {
    condition = toset(flatten(data.aws_iam_policy_document.ops_mcp_memory.statement[*].actions)) == toset([
      "dynamodb:GetItem",
      "dynamodb:Query",
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:DeleteItem",
    ])
    error_message = "the memory policy allows something other than reading and writing single rows"
  }

  assert {
    condition = length(data.aws_iam_policy_document.ops_mcp_memory.statement) == 1 && alltrue([
      for statement in data.aws_iam_policy_document.ops_mcp_memory.statement :
      statement.effect == "Allow" && toset(statement.resources) == toset([
        "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-operator-suggestions",
      ])
    ])
    error_message = "the write actions must be on the suggestions table alone"
  }

  assert {
    condition     = aws_dynamodb_table.operator_suggestions.name == "bloggerbear-test-operator-suggestions" && aws_dynamodb_table.operator_suggestions.hash_key == "user_id" && aws_dynamodb_table.operator_suggestions.range_key == "item" && aws_dynamodb_table.operator_suggestions.billing_mode == "PAY_PER_REQUEST"
    error_message = "the suggestions table is bloggerbear-<env>-operator-suggestions, keyed by user_id and item, on demand"
  }

  assert {
    condition     = one(aws_dynamodb_table.operator_suggestions.ttl[*].attribute_name) == "expires_at" && one(aws_dynamodb_table.operator_suggestions.ttl[*].enabled) == true
    error_message = "rows expire on expires_at"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.OPERATOR_SUGGESTIONS_TABLE == "bloggerbear-test-operator-suggestions"
    error_message = "the function is told its own table's name as OPERATOR_SUGGESTIONS_TABLE"
  }

  # The role has these two policies of this module's and no others.
  assert {
    condition     = aws_iam_role_policy.ops_mcp.name == "bloggerbear-test-ops-mcp-read-only" && aws_iam_role_policy.ops_mcp_memory.name == "bloggerbear-test-ops-mcp-own-suggestions"
    error_message = "the role's two policies are named for what they allow"
  }

  # The read-only policy. An allowlist, not a search for "Put" or "Delete": an action nobody
  # thought to forbid fails this too. Adding a read action for a new tool means adding it here,
  # on purpose.
  assert {
    condition = length(setsubtract(
      toset(flatten(data.aws_iam_policy_document.ops_mcp.statement[*].actions)),
      toset([
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
        "dynamodb:ListTagsOfResource",
        "s3:GetObject",
        "cloudwatch:DescribeAlarms",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
      ]),
    )) == 0
    error_message = "the MCP server's role allows an action outside the read-only list"
  }

  assert {
    condition = alltrue([
      for action in flatten(data.aws_iam_policy_document.ops_mcp.statement[*].actions) :
      !strcontains(action, "*")
    ])
    error_message = "no action in the MCP server's role may be a wildcard"
  }

  assert {
    condition = alltrue([
      for statement in data.aws_iam_policy_document.ops_mcp.statement : statement.effect == "Allow"
    ])
    error_message = "the role's policy is a list of what is allowed; nothing else is expected in it"
  }

  # The tables statement names the caller's tables and their indexes, and nothing wider.
  assert {
    condition = toset(data.aws_iam_policy_document.ops_mcp.statement[0].resources) == toset([
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-topics",
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-topics/index/*",
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-articles",
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-articles/index/*",
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config",
      "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-test-model-config/index/*",
    ])
    error_message = "the DynamoDB statement should cover exactly the tables passed in and their indexes"
  }

  assert {
    condition     = toset(data.aws_iam_policy_document.ops_mcp.statement[1].resources) == toset(["arn:aws:s3:::bloggerbear-test-content/articles/*"])
    error_message = "the S3 statement should cover the content bucket's articles/ prefix only"
  }

  assert {
    condition     = aws_iam_role.ops_mcp.name == "bloggerbear-test-ops-mcp-lambda-exec"
    error_message = "the role has its own name; the shared role is bloggerbear-<env>-lambda-exec"
  }
}

run "the_route_needs_a_token_with_the_scope" {
  command = plan

  assert {
    condition     = aws_api_gateway_method.mcp.http_method == "POST" && aws_api_gateway_resource.mcp.path_part == "mcp"
    error_message = "the one route is POST /mcp"
  }

  assert {
    condition     = aws_api_gateway_method.mcp.authorization == "COGNITO_USER_POOLS"
    error_message = "POST /mcp must sit behind the Cognito authorizer"
  }

  assert {
    condition     = aws_api_gateway_method.mcp.authorization_scopes == toset(["bloggerbear-ops/read"])
    error_message = "POST /mcp must require the bloggerbear-ops/read scope"
  }

  assert {
    condition     = aws_api_gateway_authorizer.cognito.type == "COGNITO_USER_POOLS"
    error_message = "the authorizer checks tokens against the user pool"
  }

  assert {
    condition     = aws_cognito_resource_server.ops.identifier == "bloggerbear-ops" && one(aws_cognito_resource_server.ops.scope[*].scope_name) == "read"
    error_message = "the resource server defines bloggerbear-ops/read, the scope the method asks for"
  }

  assert {
    condition     = contains(aws_cognito_user_pool_client.page.allowed_oauth_scopes, "bloggerbear-ops/read")
    error_message = "the app client must be able to ask for the scope the method requires"
  }

  assert {
    condition     = aws_cognito_user_pool_client.page.generate_secret == false && aws_cognito_user_pool_client.page.allowed_oauth_flows == toset(["code"])
    error_message = "the app client is a public one: authorization code only, no secret"
  }

  assert {
    condition     = one(aws_cognito_user_pool.this.admin_create_user_config[*].allow_admin_create_user_only) == true
    error_message = "nobody may sign themselves up"
  }

  assert {
    condition     = aws_api_gateway_integration.mcp.type == "AWS_PROXY"
    error_message = "the route is a plain Lambda proxy integration"
  }
}

run "the_function_runs_the_web_app_through_the_adapter" {
  command = plan

  assert {
    condition     = aws_lambda_function.ops_mcp.handler == "run.sh" && aws_lambda_function.ops_mcp.environment[0].variables.AWS_LAMBDA_EXEC_WRAPPER == "/opt/bootstrap"
    error_message = "the adapter needs handler = run.sh and AWS_LAMBDA_EXEC_WRAPPER = /opt/bootstrap"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.layers == tolist(["arn:aws:lambda:ap-southeast-2:753240598075:layer:LambdaAdapterLayerX86:30"])
    error_message = "one layer: the Web Adapter's ap-southeast-2 x86_64 one, at its pinned version"
  }

  # Each table the caller names reaches the function under the name common/dynamo.py reads.
  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.TOPICS_TABLE == "bloggerbear-test-topics" && aws_lambda_function.ops_mcp.environment[0].variables.ARTICLES_TABLE == "bloggerbear-test-articles"
    error_message = "every table in var.tables becomes an environment variable holding its name"
  }

  # The agent's key (tests/ops_agent.tftest.hcl holds that both functions get the same one). No
  # key is given in this file, and the default is none: nothing is taken as the agent's.
  assert {
    condition     = nonsensitive(aws_lambda_function.ops_mcp.environment[0].variables.OPS_AGENT_FORWARD_KEY) == ""
    error_message = "the server is always told the forward key, and with none given it is empty"
  }
}

run "mfa_is_optional_when_asked" {
  command = plan

  assert {
    condition     = aws_cognito_user_pool.this.mfa_configuration == "OPTIONAL"
    error_message = "mfa_configuration should follow the variable"
  }
  assert {
    condition     = length(aws_cognito_user_pool.this.software_token_mfa_configuration) == 1
    error_message = "OPTIONAL needs the authenticator-app method enabled"
  }
}

run "mfa_is_required_when_asked" {
  command = plan
  variables {
    mfa_configuration = "ON"
  }

  assert {
    condition     = aws_cognito_user_pool.this.mfa_configuration == "ON"
    error_message = "mfa_configuration should follow the variable"
  }
  assert {
    condition     = one(aws_cognito_user_pool.this.software_token_mfa_configuration[*].enabled) == true
    error_message = "ON needs the authenticator-app method enabled, or Cognito refuses the pool"
  }
}

run "mfa_is_off_when_asked" {
  command = plan
  variables {
    mfa_configuration = "OFF"
  }

  assert {
    condition     = aws_cognito_user_pool.this.mfa_configuration == "OFF"
    error_message = "mfa_configuration should follow the variable"
  }
  assert {
    condition     = length(aws_cognito_user_pool.this.software_token_mfa_configuration) == 0
    error_message = "OFF should not enable an MFA method"
  }
}

run "an_unknown_mfa_value_is_refused" {
  command = plan
  variables {
    mfa_configuration = "REQUIRED"
  }
  expect_failures = [var.mfa_configuration]
}

# Cognito checks these at apply only, so the patterns it documents are held here. The resource
# server name's is [\w\s+=,.@-]+ (an apostrophe failed the first dev apply); a user pool's and an
# app client's is [\w\s+=,.@-]+ too; a domain prefix is lower-case letters, digits and hyphens.
run "cognito_names_fit_the_patterns_cognito_enforces_at_apply" {
  command = plan

  assert {
    condition     = can(regex("^[\\w\\s+=,.@-]+$", aws_cognito_resource_server.ops.name))
    error_message = "the resource server's name has a character Cognito refuses"
  }
  assert {
    condition     = can(regex("^[\\w\\s+=,.@-]+$", aws_cognito_user_pool.this.name))
    error_message = "the user pool's name has a character Cognito refuses"
  }
  assert {
    condition     = can(regex("^[\\w\\s+=,.@-]+$", aws_cognito_user_pool_client.page.name))
    error_message = "the app client's name has a character Cognito refuses"
  }
  assert {
    condition     = can(regex("^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", aws_cognito_user_pool_domain.this.domain))
    error_message = "the hosted domain prefix must be lower-case letters, digits and hyphens"
  }
  assert {
    condition = alltrue([
      for scope in aws_cognito_resource_server.ops.scope : !strcontains(scope.scope_description, "'")
    ])
    error_message = "keep apostrophes out of scope descriptions too"
  }
}

# The region is the caller's (var.aws_region): the adapter layer is published per region under one
# account and name, and a function can only attach the copy in its own region. The default-region
# run above (the_function_runs_the_web_app_through_the_adapter) pins the ARN the original
# deployment has always had; this one shows that everything that names a region moves together.
run "another_region_moves_the_layer_the_host_names_and_the_policy" {
  command = plan

  variables {
    aws_region = "eu-west-1"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.layers == tolist(["arn:aws:lambda:eu-west-1:753240598075:layer:LambdaAdapterLayerX86:30"])
    error_message = "the Web Adapter layer must be the copy in the region the function is created in"
  }

  assert {
    condition     = endswith(output.hosted_ui_domain, ".auth.eu-west-1.amazoncognito.com")
    error_message = "the sign-in page's host name is in the pool's own region"
  }

  assert {
    condition = contains(
      flatten([for statement in data.aws_iam_policy_document.ops_mcp.statement : statement.resources]),
      "arn:aws:cloudwatch:eu-west-1:*:alarm:*",
    )
    error_message = "the alarms the role may list are the ones in the region it runs in"
  }
}

# The prefix is the caller's (var.unique_name_prefix). Every run above uses the original
# deployment's, "bloggerbear", and pins the names it has always had; this one shows that another
# deployment's names all move together and that both functions are told the prefix.
run "another_name_prefix_moves_every_name_and_reaches_both_functions" {
  command = plan

  variables {
    unique_name_prefix = "acme-blog"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.function_name == "acme-blog-test-ops-mcp" && aws_lambda_function.ops_agent.function_name == "acme-blog-test-ops-agent"
    error_message = "both functions are <prefix>-<env>-<name>"
  }
  assert {
    condition     = aws_iam_role.ops_mcp.name == "acme-blog-test-ops-mcp-lambda-exec" && aws_iam_role.ops_agent.name == "acme-blog-test-ops-agent-lambda-exec"
    error_message = "both roles are named from the prefix (the deploy role may only manage <prefix>-*-lambda-exec)"
  }
  assert {
    condition     = aws_dynamodb_table.operator_suggestions.name == "acme-blog-test-operator-suggestions" && aws_dynamodb_table.briefings.name == "acme-blog-test-ops-briefings"
    error_message = "the assistant's own tables are named from the prefix"
  }
  assert {
    condition     = aws_cognito_user_pool.this.name == "acme-blog-test-ops-assistant" && aws_api_gateway_rest_api.this.name == "acme-blog-test-ops-mcp"
    error_message = "the user pool and the API are named from the prefix"
  }
  assert {
    condition     = aws_cloudwatch_log_group.lambda.name == "/aws/lambda/acme-blog-test-ops-mcp" && aws_cloudwatch_log_group.access.name == "/aws/apigateway/acme-blog-test-ops-mcp-access"
    error_message = "the log groups are named from the prefix"
  }
  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.NAME_PREFIX == "acme-blog" && aws_lambda_function.ops_agent.environment[0].variables.NAME_PREFIX == "acme-blog"
    error_message = "both functions must be told the prefix as NAME_PREFIX, with no trailing hyphen"
  }
  assert {
    condition     = aws_cognito_resource_server.ops.identifier == "bloggerbear-ops"
    error_message = "the scope's name is not a resource name: it does not follow the prefix"
  }
}

run "the_default_prefix_is_what_the_functions_are_told" {
  command = plan

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.NAME_PREFIX == "bloggerbear" && aws_lambda_function.ops_agent.environment[0].variables.NAME_PREFIX == "bloggerbear"
    error_message = "NAME_PREFIX is the prefix as given"
  }
}
