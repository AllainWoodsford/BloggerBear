force_destroy = true

# bedrock_model_id is deliberately NOT set here anymore. It used to be
# pinned to "" (empty) right here, which -- since a tfvars value always
# wins over a variable's own `default` -- silently overrode a later
# change to raise that default to a real model ID in variables.tf: the
# variable looked configured, but every real invocation still got an
# empty modelId and failed with
# `ParamValidationError: Invalid length for parameter modelId, value: 0`.
# Confirmed the hard way via a real research_tick invocation. Leaving
# this variable entirely unset here means variables.tf's own default
# (currently "au.anthropic.claude-sonnet-5") actually takes effect. Set
# a real value in *this* file again only if a value different from that
# default is ever needed for dev specifically.

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
