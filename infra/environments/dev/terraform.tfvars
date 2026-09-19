force_destroy = true

bedrock_model_id = "" # TODO: set after confirming Bedrock model access/availability in ap-southeast-2 (see docs/project-plan.md §3) -- any Bedrock provider's model/inference-profile ID works, not just Anthropic's, see the variable's own description

# admin_allowed_cidrs is deliberately NOT set here -- see its description in
# variables.tf. It's supplied in CI via a TF_VAR_admin_allowed_cidrs env var
# sourced from the ADMIN_ALLOWED_CIDRS_DEV GitHub Actions secret instead, so
# a real IP never lands in git history. A local apply without that env var
# falls back to the variable's `[]` fail-closed default.

# Manual follow-up: after infra/environments/production has been applied
# at least once and its WAF Web ACL exists, set this to that ACL's ARN
# (see production's `wafv2_web_acl_arn` output) so dev shares the one ACL
# instead of going unprotected.
web_acl_arn = ""
