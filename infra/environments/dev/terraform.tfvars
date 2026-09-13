force_destroy = true

# Manual follow-up: after infra/environments/production has been applied
# at least once and its WAF Web ACL exists, set this to that ACL's ARN
# (see production's `wafv2_web_acl_arn` output) so dev shares the one ACL
# instead of going unprotected.
web_acl_arn = ""
