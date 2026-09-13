terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  backend "s3" {
    bucket       = "bloggerbear-terraform-state"
    key          = "dev/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-southeast-2"
}

# The static-site module declares a required `aws.us_east_1` provider
# alias (needed for the ACM certificate path used only when
# enable_custom_domain = true -- see infra/modules/static-site/main.tf).
# Terraform requires every module call to supply a provider for each
# configuration_alias the module declares, even when this environment
# never exercises that code path: enable_custom_domain = false below means
# the ACM/Route53 resources in the module all have count = 0, so this
# alias is never actually invoked here. We satisfy the requirement by
# pointing the alias at the same default ap-southeast-2 provider rather
# than declaring a real us-east-1 provider -- production is the only place
# in this codebase with an actual us-east-1 provider block.
provider "aws" {
  alias  = "us_east_1"
  region = "ap-southeast-2"
}

module "static_site" {
  source = "../../modules/static-site"

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment_name     = "dev"
  enable_custom_domain = false
  force_destroy        = var.force_destroy
  web_acl_id           = var.web_acl_arn
}
