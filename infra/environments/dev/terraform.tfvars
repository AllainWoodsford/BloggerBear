force_destroy = true

bedrock_model_id = "" # TODO: set after confirming Bedrock model access/availability in ap-southeast-2 (see docs/project-plan.md §3)

# Manual follow-up: after infra/environments/production has been applied
# at least once and its WAF Web ACL exists, set this to that ACL's ARN
# (see production's `wafv2_web_acl_arn` output) so dev shares the one ACL
# instead of going unprotected.
web_acl_arn = ""
