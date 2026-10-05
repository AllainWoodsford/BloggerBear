# "The dev assistant reads only dev's things; the production assistant only production's"
# (isolation.tf, and the design's section 6), planned with a mocked AWS provider like the two
# files next to this one (ops_assistant.tftest.hcl's header says how, and why the policy
# documents' `json` is given a value). What is held here, for dev and for a second environment:
# both roles carry the Deny on anything tagged for another environment, with the condition that
# makes it do nothing where no tag is supplied; the MCP function is told its environment and
# whether it may report account-wide data; and that flag is off unless the caller switches it on.

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

  environment_name = "dev"
  tables = {
    TOPICS_TABLE = {
      name = "bloggerbear-dev-topics"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-dev-topics"
    }
    MODEL_CONFIG_TABLE = {
      name = "bloggerbear-dev-model-config"
      arn  = "arn:aws:dynamodb:ap-southeast-2:111111111111:table/bloggerbear-dev-model-config"
    }
  }
  agent_model_id                = "au.example.test-model-v1:0"
  content_bucket_name           = "bloggerbear-dev-content"
  sign_in_trigger_function_arn  = "arn:aws:lambda:ap-southeast-2:123456789012:function:bloggerbear-test-sign-in-events"
  sign_in_trigger_function_name = "bloggerbear-test-sign-in-events"
  content_bucket_arn            = "arn:aws:s3:::bloggerbear-dev-content"
  stage_name                    = "dev"
  hosted_ui_domain_prefix       = "bloggerbear-dev-ops"
  callback_urls                 = ["https://example.com/ask.html"]
  logout_urls                   = ["https://example.com/ask.html"]
  mfa_configuration             = "OPTIONAL"
  throttling_rate_limit         = 5
  throttling_burst_limit        = 10
}

run "dev_is_denied_anything_tagged_for_another_environment" {
  command = plan

  # One statement, a Deny, on everything: the conditions are the whole of what narrows it.
  assert {
    condition = length(data.aws_iam_policy_document.other_environments_denied.statement) == 1 && alltrue([
      for statement in data.aws_iam_policy_document.other_environments_denied.statement :
      statement.effect == "Deny" && toset(statement.actions) == toset(["*"]) && toset(statement.resources) == toset(["*"])
    ])
    error_message = "the isolation policy is one Deny of every action on every resource, narrowed only by its conditions"
  }

  # Both, and nothing else. Null = false is what keeps the statement from denying every request
  # that carries no resource tag (StringNotEquals alone is true when the key is absent).
  assert {
    condition = toset([
      for condition in data.aws_iam_policy_document.other_environments_denied.statement[0].condition :
      "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ]) == toset([
      "Null aws:ResourceTag/Environment false",
      "StringNotEquals aws:ResourceTag/Environment dev",
    ])
    error_message = "the Deny applies only where the resource carries an Environment tag, and that tag is not dev"
  }

  # No not_actions, not_resources or principals: nothing that would turn the statement inside out.
  assert {
    condition = alltrue([
      for statement in data.aws_iam_policy_document.other_environments_denied.statement :
      statement.not_actions == null && statement.not_resources == null
    ])
    error_message = "the Deny names actions and resources directly"
  }

  # On both roles, each under a name of its own that says what it is. (Which role each is
  # attached to is an id unknown until apply; test_terraform_wiring.py holds that from the code.)
  assert {
    condition     = aws_iam_role_policy.ops_mcp_other_environments_denied.name == "bloggerbear-dev-ops-mcp-other-environments-denied"
    error_message = "the MCP server's role carries the Deny"
  }

  assert {
    condition     = aws_iam_role_policy.ops_agent_other_environments_denied.name == "bloggerbear-dev-ops-agent-other-environments-denied"
    error_message = "the agent's role carries the Deny"
  }

  # The Allow policies are untouched by it: still lists of what is allowed, with no Deny in them.
  assert {
    condition = alltrue([
      for statement in concat(
        data.aws_iam_policy_document.ops_mcp.statement,
        data.aws_iam_policy_document.ops_mcp_memory.statement,
        data.aws_iam_policy_document.ops_agent.statement,
      ) : statement.effect == "Allow"
    ])
    error_message = "the Deny lives in a policy of its own; the roles' other policies only allow"
  }
}

run "dev_is_told_its_environment_and_has_no_account_wide_data" {
  command = plan

  # Dev reads only dev: never production's, never the shared resources.
  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.OPS_READABLE_ENVIRONMENTS == "dev"
    error_message = "dev's assistant reads dev's resources alone"
  }

  assert {
    condition     = jsondecode(aws_lambda_function.ops_mcp.environment[0].variables.OPS_DEFAULT_TAGS) == { ManagedBy = "Terraform", Project = "BloggerBear" }
    error_message = "the function is told the default tags a table must carry, as the root gives them"
  }

  # table_sample: a bloggerbear-* table, only with the project's tags and dev's Environment.
  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp.statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "SampleTaggedTables"
      ])) == toset([
      "StringEquals aws:ResourceTag/ManagedBy Terraform",
      "StringEquals aws:ResourceTag/Project BloggerBear",
      "StringEquals aws:ResourceTag/Environment dev",
    ])
    error_message = "table_sample reads a table only if it carries the default tags and dev's Environment"
  }


  assert {
    condition     = var.account_wide_data == false
    error_message = "account_wide_data is off unless the caller switches it on"
  }

  # What the alarms tool builds its prefix from: bloggerbear-dev-.
  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.ENVIRONMENT_NAME == "dev"
    error_message = "the MCP function is told which environment it is for, as ENVIRONMENT_NAME"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.OPS_ACCOUNT_WIDE_DATA == "false"
    error_message = "with the flag off the function is told so, in the one word the code does not take as on"
  }

  # The agent has no tool of its own and reads no alarm or bill: it is told neither.
  assert {
    condition = alltrue([
      for name in ["ENVIRONMENT_NAME", "OPS_ACCOUNT_WIDE_DATA"] :
      !contains(keys(aws_lambda_function.ops_agent.environment[0].variables), name)
    ])
    error_message = "the agent learns about the pipeline from the MCP server alone"
  }
}

# A second environment, as production will call the module: the Deny follows the name, and the
# flag reaches the function.
run "production_is_denied_anything_not_tagged_production_and_may_report_the_account" {
  command = plan

  variables {
    environment_name  = "production"
    account_wide_data = true
  }

  assert {
    condition = toset([
      for condition in data.aws_iam_policy_document.other_environments_denied.statement[0].condition :
      "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ]) == toset([
      "Null aws:ResourceTag/Environment false",
      "StringNotEquals aws:ResourceTag/Environment production,shared",
    ])
    error_message = "the Deny applies only where the resource carries an Environment tag, and that tag is neither production nor shared"
  }

  # Production may read what is shared, and is told so; its table reads require the same.
  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.OPS_READABLE_ENVIRONMENTS == "production,shared"
    error_message = "production's assistant reads production's and the shared resources"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp.statement : [
        for condition in statement.condition : join(",", condition.values)
        if condition.variable == "aws:ResourceTag/Environment"
      ] if statement.sid == "SampleTaggedTables"
    ])) == toset(["production,shared"])
    error_message = "production's table_sample reads tables tagged production or shared"
  }

  assert {
    condition     = aws_iam_role_policy.ops_mcp_other_environments_denied.name == "bloggerbear-production-ops-mcp-other-environments-denied" && aws_iam_role_policy.ops_agent_other_environments_denied.name == "bloggerbear-production-ops-agent-other-environments-denied"
    error_message = "both of production's roles carry the Deny"
  }

  assert {
    condition     = aws_lambda_function.ops_mcp.environment[0].variables.ENVIRONMENT_NAME == "production" && aws_lambda_function.ops_mcp.environment[0].variables.OPS_ACCOUNT_WIDE_DATA == "true"
    error_message = "production's function is told it is production, and that it may report account-wide data"
  }
}

# The alarms tool tells environments apart by the prefix bloggerbear-<environment_name>-. A name
# with a hyphen would make one environment's prefix the start of another's.
run "an_environment_name_with_a_hyphen_is_refused" {
  command = plan

  variables {
    environment_name = "dev-old"
  }

  expect_failures = [var.environment_name]
}

run "an_environment_name_that_is_not_lowercase_is_refused" {
  command = plan

  variables {
    environment_name = "Production"
  }

  expect_failures = [var.environment_name]
}

# Project is the deployment's tag: "BloggerBear" in the original deployment (every run above), and
# a deployment's own name prefix when it has one (the roots' local.project_tag). Whatever the root
# hands over is what the function is told and what IAM compares against, so another deployment's
# assistant reads tables tagged with its own Project and never ones tagged "BloggerBear".
run "another_deployments_project_tag_is_the_one_the_tables_must_carry" {
  command = plan

  variables {
    unique_name_prefix = "acme-blog"
    default_tags = {
      ManagedBy = "Terraform"
      Project   = "acme-blog"
    }
  }

  assert {
    condition     = jsondecode(aws_lambda_function.ops_mcp.environment[0].variables.OPS_DEFAULT_TAGS) == { ManagedBy = "Terraform", Project = "acme-blog" }
    error_message = "the function is told this deployment's Project tag, not the original deployment's"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp.statement : [
        for condition in statement.condition : "${condition.test} ${condition.variable} ${join(",", condition.values)}"
      ] if statement.sid == "SampleTaggedTables"
      ])) == toset([
      "StringEquals aws:ResourceTag/ManagedBy Terraform",
      "StringEquals aws:ResourceTag/Project acme-blog",
      "StringEquals aws:ResourceTag/Environment dev",
    ])
    error_message = "table_sample reads a table only if it carries this deployment's Project tag"
  }

  assert {
    condition = toset(one([
      for statement in data.aws_iam_policy_document.ops_mcp.statement : statement.resources
      if statement.sid == "SampleTaggedTables"
      ])) == toset([
      "arn:aws:dynamodb:ap-southeast-2:*:table/acme-blog-*",
      "arn:aws:dynamodb:ap-southeast-2:*:table/acme-blog-*/index/*",
    ])
    error_message = "and only tables named with this deployment's prefix"
  }
}
