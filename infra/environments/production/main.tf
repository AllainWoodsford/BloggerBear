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
    key          = "production/terraform.tfstate"
    region       = "ap-southeast-2"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = "ap-southeast-2"
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

  name        = "bloggerbear-shared"
  description = "Shared WAF Web ACL for BloggerBear's CloudFront distributions (dev + production)."
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

  providers = {
    aws           = aws
    aws.us_east_1 = aws.us_east_1
  }

  environment_name     = "production"
  enable_custom_domain = true
  force_destroy        = false
  domain_name          = var.domain_name
  hosted_zone_id       = var.hosted_zone_id
  web_acl_id           = aws_wafv2_web_acl.this.arn
}
