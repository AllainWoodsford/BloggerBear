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
# (the AU Claude Haiku 4.5 inference profile, built from
# var.bedrock_inference_profile_id) actually takes effect. Set a real
# value in *this* file again only if a value different from that default
# is ever needed for dev specifically. Outside Australia, set
# bedrock_inference_profile_id here instead (docs/deployment-runsheet.md).

# The admin API's address allowlist is not a Terraform variable: it is an SSM
# parameter that scripts/admin_allowlist.py writes and copies into the WAF IP
# set and the API's resource policy, so no address is in git or a plan.

# Manual follow-up: after infra/environments/production has been applied
# at least once and its WAF Web ACL exists, set this to that ACL's ARN
# (see production's `wafv2_web_acl_arn` output) so dev shares the one ACL
# instead of going unprotected.
web_acl_arn = ""
