# The bootstrap (main.tf), planned with a mocked AWS provider like the modules' tests, so no
# credentials are needed. The bootstrap is applied once, by hand, and never by CI, so before this
# nothing planned it before the owner's apply did. `terraform validate` checks types, not values: a
# resource whose for_each is a data source gets that data source's whole object as each.value,
# which validate cannot see yet, and a name built from "${each.value}" passed validate and stopped
# every plan. Preconditions, too, run only at plan. Held here: the roles and policies plan with the
# defaults (the top-level domain_name = "" leaves the zone out of the first runs), the separation
# policies follow their switch, a state bucket the separation would refuse is stopped before
# anything is made, and the zone and the budget the owner's apply creates (a domain and a budget
# email set) plan too. The prefix is another deployment's, so every name is seen to follow it.

mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

variables {
  unique_name_prefix = "acme-blog"
  state_bucket_name  = "acme-blog-terraform-state"
  github_repo        = "acme/blog"
  domain_name        = ""
}

run "the_bootstrap_plans_with_every_role_and_policy_keyed_by_its_environment" {
  command = plan

  assert {
    condition = (
      aws_iam_role.gha_dev_deploy.name == "gha-acme-blog-dev-deploy"
      && aws_iam_role.gha_prod_deploy.name == "gha-acme-blog-prod-deploy"
      && aws_iam_policy.gha_deploy.name == "acme-blog-gha-deploy"
    )
    error_message = "the deploy roles and their shared policy are named from the prefix"
  }

  assert {
    condition = (
      aws_iam_role.gha_dev_prompts.name == "gha-acme-blog-dev-prompts"
      && aws_iam_role.gha_prod_prompts.name == "gha-acme-blog-prod-prompts"
      && toset(keys(aws_iam_policy.gha_prompts)) == toset(["dev", "prod"])
      && aws_iam_policy.gha_prompts["dev"].name == "acme-blog-gha-dev-prompts"
      && aws_iam_policy.gha_prompts["prod"].name == "acme-blog-gha-prod-prompts"
    )
    error_message = "one publisher policy per environment, each for_each over the data source, named from its key"
  }

  assert {
    condition = (
      aws_iam_policy.gha_prompts["prod"].description == "What the production prompts publisher may do: its environment's prompts bucket and prompt-hashes table, and nothing else."
    )
    error_message = "the description names the environment, never the data source's whole object"
  }

  assert {
    condition = (
      toset(keys(aws_iam_policy.gha_deploy_other_environment)) == toset(["dev", "production"])
      && aws_iam_policy.gha_deploy_other_environment["dev"].name == "acme-blog-gha-dev-deploy-not-production"
      && aws_iam_policy.gha_deploy_other_environment["production"].name == "acme-blog-gha-production-deploy-not-dev"
      && length(aws_iam_role_policy_attachment.gha_dev_deploy_not_production) == 1
      && length(aws_iam_role_policy_attachment.gha_prod_deploy_not_dev) == 1
    )
    error_message = "each deploy role is kept out of the other environment by default"
  }

  assert {
    condition     = length(aws_route53_zone.site) == 0 && length(aws_budgets_budget.bedrock_spend) == 0
    error_message = "no domain and no budget email: no zone and no budget"
  }
}

run "without_the_separation_no_deny_policy_is_made_or_attached" {
  command = plan

  variables {
    separate_environment_permissions = false
  }

  assert {
    condition = (
      length(aws_iam_policy.gha_deploy_other_environment) == 0
      && length(aws_iam_role_policy_attachment.gha_dev_deploy_not_production) == 0
      && length(aws_iam_role_policy_attachment.gha_prod_deploy_not_dev) == 0
    )
    error_message = "separate_environment_permissions = false detaches both"
  }
}

run "a_state_bucket_the_separation_would_refuse_to_one_role_is_stopped_at_plan" {
  command = plan

  variables {
    state_bucket_name = "acme-blog-dev-terraform-state"
  }

  expect_failures = [aws_iam_policy.gha_deploy_other_environment]
}

# Planning this run also evaluates the zone's two outputs on their one-zone path; their values are
# computed, so unknown under the mock, and are not asserted.
run "with_a_domain_and_a_budget_email_the_zone_and_the_budget_plan_from_the_prefix" {
  command = plan

  variables {
    domain_name        = "example.org"
    budget_alert_email = "ops@example.org"
  }

  assert {
    condition = (
      length(aws_route53_zone.site) == 1
      && aws_route53_zone.site[0].name == "example.org"
      && length(aws_budgets_budget.bedrock_spend) == 1
      && aws_budgets_budget.bedrock_spend[0].name == "acme-blog-bedrock-spend"
    )
    error_message = "a domain plans its zone, and a budget email the Bedrock budget named from the prefix"
  }
}
