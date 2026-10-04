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
  }
  content_bucket_name     = "bloggerbear-test-content"
  content_bucket_arn      = "arn:aws:s3:::bloggerbear-test-content"
  stage_name              = "test"
  hosted_ui_domain_prefix = "bloggerbear-test-ops"
  callback_urls           = ["https://example.com/ask.html"]
  logout_urls             = ["https://example.com/ask.html"]
  mfa_configuration       = "OPTIONAL"
  throttling_rate_limit   = 5
  throttling_burst_limit  = 10
}

run "the_role_can_only_read" {
  command = plan

  # An allowlist, not a search for "Put" or "Delete": an action nobody thought to forbid fails
  # this too. Adding a read action for a new tool means adding it here, on purpose.
  assert {
    condition = length(setsubtract(
      toset(flatten(data.aws_iam_policy_document.ops_mcp.statement[*].actions)),
      toset([
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
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
