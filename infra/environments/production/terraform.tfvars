# TODO: set before first production apply -- see
# docs/specs/phase-0-foundations.md "Open questions" (domain name and
# registrar not yet decided/registered). Production's static-site module
# call has enable_custom_domain = true, so these must be non-empty before
# that first apply succeeds.
domain_name    = ""
hosted_zone_id = ""

bedrock_model_id = "" # TODO: set after confirming Bedrock model access/availability in ap-southeast-2 (see docs/project-plan.md §3) -- any Bedrock provider's model/inference-profile ID works, not just Anthropic's, see the variable's own description

# admin_allowed_cidrs is deliberately NOT set here -- see its description in
# variables.tf. It's supplied in CI via a TF_VAR_admin_allowed_cidrs env var
# sourced from the production-Environment-scoped ADMIN_ALLOWED_CIDRS_PROD
# GitHub Actions secret instead, so a real IP never lands in git history. A
# local apply without that env var falls back to the variable's `[]`
# fail-closed default.
