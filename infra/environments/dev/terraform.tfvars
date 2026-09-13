force_destroy = true

bedrock_model_id = "" # TODO: set after confirming Bedrock model access/availability in ap-southeast-2 (see docs/project-plan.md §3)

# TODO: set to the operator's own public IP as a /32 CIDR (e.g.
# ["203.0.113.7/32"]) before the admin API becomes reachable at all --
# until then, the regional WAF Web ACL's default-block behavior means
# NOTHING can call this API. That is the deliberately safe default (fail
# closed), not a bug.
admin_allowed_cidrs = []

# Manual follow-up: after infra/environments/production has been applied
# at least once and its WAF Web ACL exists, set this to that ACL's ARN
# (see production's `wafv2_web_acl_arn` output) so dev shares the one ACL
# instead of going unprotected.
web_acl_arn = ""
