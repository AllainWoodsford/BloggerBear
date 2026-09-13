# TODO: set before first production apply -- see
# docs/specs/phase-0-foundations.md "Open questions" (domain name and
# registrar not yet decided/registered). Production's static-site module
# call has enable_custom_domain = true, so these must be non-empty before
# that first apply succeeds.
domain_name    = ""
hosted_zone_id = ""

bedrock_model_id = "" # TODO: set after confirming Bedrock model access/availability in ap-southeast-2 (see docs/project-plan.md §3)

# TODO: set to the operator's own public IP as a /32 CIDR (e.g.
# ["203.0.113.7/32"]) before the admin API becomes reachable at all --
# until then, the regional WAF Web ACL's default-block behavior means
# NOTHING can call this API. That is the deliberately safe default (fail
# closed), not a bug.
admin_allowed_cidrs = []
