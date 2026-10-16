"""The cost-saving firewall switches (docs/cost-saving.md): four bool Terraform variables, true by
default, each fed from a GitHub variable by the deploy workflows. With every switch on, the
firewall is exactly what it was before the switches existed: every resource that gained `count`
has a `moved` block to index [0], so the default plan destroys and creates nothing. Whatever the
switches say, the admin API's address allowlist is enforced by the API's own resource policy.

These checks read the Terraform as text, like test_terraform_wiring.py: a plan cannot run here."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
WORKFLOWS = ROOT / ".github" / "workflows"

SWITCHES = ("waf_regional_enabled", "waf_admin_allowlist", "waf_log_all_requests")
GITHUB_NAMES = {
    "waf_regional_enabled": "WAF_REGIONAL_ENABLED",
    "waf_admin_allowlist": "WAF_ADMIN_ALLOWLIST",
    "waf_log_all_requests": "WAF_LOG_ALL_REQUESTS",
    "waf_site_enabled": "WAF_SITE_ENABLED",
}

# Every regional firewall resource that gained `count`, and the switch (as written) that decides it.
REGIONAL_COUNTED = {
    "aws_wafv2_ip_set.admin_allowlist": "local.waf_admin_enabled",
    "aws_wafv2_web_acl.admin": "local.waf_admin_enabled",
    "aws_cloudwatch_log_group.waf_admin": "local.waf_admin_enabled",
    "aws_wafv2_web_acl_logging_configuration.admin": "local.waf_admin_enabled",
    "aws_wafv2_web_acl.public_api": "local.waf_public_api_enabled",
    "aws_cloudwatch_log_group.waf_public_api": "local.waf_public_api_enabled",
    "aws_wafv2_web_acl_logging_configuration.public_api": "local.waf_public_api_enabled",
    "aws_cloudwatch_log_resource_policy.waf_logs": "var.waf_regional_enabled",
}
SITE_COUNTED = {
    "aws_wafv2_web_acl.this": "var.waf_site_enabled",
    "aws_cloudwatch_log_group.waf_shared": "var.waf_site_enabled",
    "aws_cloudwatch_log_resource_policy.waf_logs_shared": "var.waf_site_enabled",
    "aws_wafv2_web_acl_logging_configuration.shared": "var.waf_site_enabled",
}


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def _resource_block(text: str, address: str) -> str:
    kind, name = address.split(".")
    match = re.search(rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}', text, re.S | re.M)
    assert match, address
    return match.group(1)


def _variable_block(text: str, name: str) -> str:
    match = re.search(rf'^variable "{name}" \{{\n(.*?)^\}}', text, re.S | re.M)
    assert match, name
    return match.group(1)


# --- the variables -------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_switch_is_a_bool_that_defaults_to_true(env):
    variables = _read("infra", "environments", env, "variables.tf")
    names = SWITCHES + (("waf_site_enabled",) if env == "production" else ())
    for name in names:
        block = _variable_block(variables, name)
        assert re.search(r"^\s*type\s*=\s*bool\s*$", block, re.M), name
        assert re.search(r"^\s*default\s*=\s*true\s*$", block, re.M), name
        assert "docs/cost-saving.md" in block, name
    if env == "dev":
        assert 'variable "waf_site_enabled"' not in variables  # production owns the shared ACL


# --- the workflows -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "workflow, suffix",
    [("terraform.yml", "DEV"), ("destroy-dev.yml", "DEV"), ("terraform-production-release.yml", "PROD")],
)
def test_each_workflow_passes_every_switch_env_then_shared_then_true(workflow, suffix):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    names = dict(GITHUB_NAMES)
    if suffix == "DEV":
        del names["waf_site_enabled"]
    for variable, github in names.items():
        line = f"TF_VAR_{variable}: ${{{{ vars.{github}_{suffix} || vars.{github} || 'true' }}}}"
        assert line in text, (workflow, line)
    if suffix == "DEV":
        assert "waf_site_enabled" not in text


# --- the resources -------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_regional_firewall_resource_is_counted_by_its_switch_and_moved_to_index_zero(env):
    text = _read("infra", "environments", env, "main.tf")
    counted = dict(REGIONAL_COUNTED)
    if env == "production":
        counted.update(SITE_COUNTED)
    for address, switch in counted.items():
        block = _resource_block(text, address)
        assert re.search(rf"^\s*count\s*=\s*{re.escape(switch)} \? 1 : 0\s*$", block, re.M), address
        assert f"moved {{\n  from = {address}\n  to   = {address}[0]\n}}" in text, address
    assert 'data "aws_iam_policy_document" "waf_logs" {\n  count = var.waf_regional_enabled ? 1 : 0' in text
    # The two locals the switches fold into.
    assert "waf_public_api_enabled = var.waf_regional_enabled" in text
    assert "waf_admin_enabled      = var.waf_regional_enabled && var.waf_admin_allowlist" in text


@pytest.mark.parametrize("env", ["dev", "production"])
def test_nothing_still_names_a_counted_resource_without_its_index(env):
    text = _read("infra", "environments", env, "main.tf")
    counted = list(REGIONAL_COUNTED) + (list(SITE_COUNTED) if env == "production" else [])
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    for address in counted:
        # Any attribute read must go through [0] (or a splat); a bare `.arn` would fail the plan
        # the moment the switch is off, which is exactly when nobody is looking.
        bare = re.findall(rf"(?<![\w.]){re.escape(address)}\.(?!\[)[a-z_]+", code)
        assert bare == [], (address, bare)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_apis_associate_their_acl_exactly_when_the_switch_has_it_exist(env):
    text = _read("infra", "environments", env, "main.tf")
    admin = 'web_acl_id              = local.waf_admin_enabled ? aws_wafv2_web_acl.admin[0].arn : ""'
    public = 'web_acl_id        = local.waf_public_api_enabled ? aws_wafv2_web_acl.public_api[0].arn : ""'
    assert admin in text and "associate_web_acl       = local.waf_admin_enabled" in text
    assert public in text and "associate_web_acl = local.waf_public_api_enabled" in text


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_admin_allowlist_is_always_a_resource_policy_that_fails_closed(env):
    text = _read("infra", "environments", env, "main.tf")
    assert "resource_policy_enabled = true" in text
    assert "resource_policy         = local.admin_api_resource_policy" in text
    policy = re.search(
        r"admin_api_resource_policy = sensitive\(jsonencode\(\{(.*?)\}\)\)", text, re.S
    ).group(1)
    # A Deny, of the invoke action, on every path; conditioned on the address only when there are
    # addresses, and unconditional (deny all) when the list is empty.
    assert 'Effect    = "Deny"' in text and 'Action    = "execute-api:Invoke"' in text
    assert 'Resource  = "execute-api:/*"' in text
    assert 'NotIpAddress = { "aws:SourceIp" = var.admin_allowed_cidrs }' in policy
    assert "length(var.admin_allowed_cidrs) > 0 ? [1] : []" in policy
    assert "length(var.admin_allowed_cidrs) > 0 ? [] : [1]" in policy
    # Never an Allow: AWS_IAM authorization stays what admits a caller.
    assert '"Allow"' not in policy


def test_the_rest_api_module_attaches_the_policy_from_a_plain_flag_and_redeploys_on_a_change():
    module = _read("infra", "modules", "rest-api", "main.tf")
    policy = _resource_block(module, "aws_api_gateway_rest_api_policy.this")
    assert re.search(r"^\s*count\s*=\s*var\.resource_policy_enabled \? 1 : 0\s*$", policy, re.M)
    assert "var.resource_policy ==" not in policy  # sensitive: must not decide a count
    # The caller's "execute-api:/*" shorthand becomes this API's full ARN, so what API Gateway
    # stores is what Terraform wrote (the shorthand would read back as a change on every plan).
    expanded = (
        'replace(var.resource_policy, "execute-api:/*", '
        '"${aws_api_gateway_rest_api.this.execution_arn}/*")'
    )
    assert expanded in policy
    deployment = _resource_block(module, "aws_api_gateway_deployment.this")
    assert 'policy = var.resource_policy_enabled ? sha1(var.resource_policy) : ""' in deployment
    assert "depends_on = [aws_api_gateway_rest_api_policy.this]" in deployment
    variables = _read("infra", "modules", "rest-api", "variables.tf")
    flag = _variable_block(variables, "resource_policy_enabled")
    assert re.search(r"^\s*default\s*=\s*false\s*$", flag, re.M)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_admin_log_keeps_only_blocks_and_counts_when_asked(env):
    text = _read("infra", "environments", env, "main.tf")
    block = _resource_block(text, "aws_wafv2_web_acl_logging_configuration.admin")
    assert 'dynamic "logging_filter"' in block
    assert "for_each = var.waf_log_all_requests ? [] : [1]" in block
    assert 'default_behavior = "DROP"' in block
    assert block.count('action = "BLOCK"') == 1 and block.count('action = "COUNT"') == 1


def test_production_runs_the_site_and_the_api_cdn_without_an_acl_when_the_site_switch_is_off():
    text = _read("infra", "environments", "production", "main.tf")
    assert text.count('var.waf_site_enabled ? aws_wafv2_web_acl.this[0].arn : ""') == 2
    assert "waf_cloudfront_acl = var.waf_site_enabled ? {" in text
    assert 'for _ in(var.waf_site_enabled ? [1] : []) : { region = "us-east-1"' in text
    outputs = _read("infra", "environments", "production", "outputs.tf")
    assert "one(aws_wafv2_web_acl.this[*].arn)" in outputs


# --- the documentation ---------------------------------------------------------------------------


def test_the_cost_saving_page_covers_every_switch_and_warns():
    page = _read("docs", "cost-saving.md")
    for github in GITHUB_NAMES.values():
        assert f"`{github}`" in page, github
        assert f"`{github}_DEV`" in page or github == "WAF_SITE_ENABLED", github
        assert f"`{github}_PROD`" in page, github
    for variable in GITHUB_NAMES:
        assert f"`{variable}`" in page, variable
    assert "at your own risk" in page.lower()
    assert "resource policy" in page
    assert "sensitive value" in page
    table = _read("docs", "configuration.md")
    for github in GITHUB_NAMES.values():
        assert re.search(rf"^\| `{github}` \| variable \| ", table, re.M), github
    assert "cost-saving.md" in table
    readme = _read("README.md")
    assert "(docs/cost-saving.md)" in readme and "cost-saving.md" in readme.split("docs/\n")[1]
    plan = _read("docs", "project-plan.md")
    assert "### Cost saving: optional firewall switches, hardened by default" in plan
    progress = _read("docs", "PROGRESS.md")
    assert "cost-saving.md" in progress
