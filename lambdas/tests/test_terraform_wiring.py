"""Checks on the Terraform that a plan cannot catch, and on the www redirect that runs at the edge.

`terraform validate` proves the files are well formed; it cannot tell you that a from-scratch
production apply would stop with "Invalid count argument", or that a caller forgot a flag that turns a
security control off silently. These are the mistakes that only show up on the first real apply, so
they are checked here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from terraform_text import (
    DEFAULT_PREFIX,
    PREFIX_REFERENCE,
    read_terraform,
    terraform_files,
    with_default_prefix,
)

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
NODE = shutil.which("node")
DOMAIN = "bloggerbear.com"
# How the home region is written wherever a root needs it (see the region tests at the end).
_HOME = "${var.aws_region}"


def _read(*parts: str) -> str:
    """An infra/ file with the default name prefix written in (terraform_text.py): the names these
    tests hold the Terraform to are the original deployment's, bloggerbear-<env>-<resource>."""
    return read_terraform(INFRA.joinpath(*parts))


def _module_blocks(text: str, source_fragment: str) -> list[str]:
    """The body of every `module "x" { ... }` whose source contains `source_fragment`."""
    blocks = []
    for match in re.finditer(r'^module "[^"]+" \{\n(.*?)^\}', text, re.S | re.M):
        if re.search(rf'source\s*=\s*"[^"]*{re.escape(source_fragment)}"', match.group(1)):
            blocks.append(match.group(1))
    return blocks


# --- a WAF ACL created in the same apply -----------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_api_that_names_a_web_acl_also_says_to_associate_it(env):
    """web_acl_id is often an ARN that does not exist until apply, so the module cannot decide from it
    (that is what broke a from-scratch production apply). The flag is what turns the association on:
    naming an ACL without it would silently leave the API unprotected."""
    blocks = _module_blocks(_read("environments", env, "main.tf"), "modules/rest-api")

    assert len(blocks) == 2, "expected the admin and public APIs"
    for block in blocks:
        assert re.search(r"^\s*web_acl_id\s*=", block, re.M), block[:80]
        assert re.search(
            r"^\s*associate_web_acl\s*=\s*true\s*$", block, re.M
        ), f"{env}: a rest-api module names a web_acl_id but does not set associate_web_acl = true"


def test_the_association_is_decided_by_the_flag_never_by_the_arn():
    module = _read("modules", "rest-api", "main.tf")
    association = re.search(
        r'resource "aws_wafv2_web_acl_association" "this" \{(.*?)\n\}', module, re.S
    ).group(1)

    assert re.search(r"count\s*=\s*var\.associate_web_acl\s*\?\s*1\s*:\s*0", association)
    assert "var.web_acl_id !=" not in association  # an unknown ARN cannot decide count


def test_no_count_is_decided_directly_from_another_resources_attribute():
    """The same trap written out in full: `count = aws_x.y.arn ...`. (The subtler form, through a
    variable, is what the rest-api tests above cover.)"""
    for path in INFRA.rglob("*.tf"):
        if "lambda-build" in path.parts or ".terraform" in path.parts:
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if re.match(r"\s*count\s*=", line):
                assert not re.search(
                    r"\baws_[a-z0-9_]+\.[a-z0-9_]+\.(arn|id|zone_id)\b", line
                ), f"{path.relative_to(ROOT)}: {line.strip()}"


# --- the domain -------------------------------------------------------------------------------------


def test_production_serves_the_bare_domain_and_redirects_www():
    site = _module_blocks(_read("environments", "production", "main.tf"), "modules/static-site")[0]

    assert re.search(r"enable_custom_domain\s*=\s*true", site)
    assert re.search(r"redirect_www\s*=\s*true", site)


def test_dev_never_takes_the_custom_domain_path():
    site = _module_blocks(_read("environments", "dev", "main.tf"), "modules/static-site")[0]

    assert re.search(r"enable_custom_domain\s*=\s*false", site)
    assert "redirect_www" not in site


def test_the_domain_in_tfvars_is_a_bare_lowercase_domain_and_the_zone_id_is_empty_or_a_zone_id():
    tfvars = _read("environments", "production", "terraform.tfvars")

    domain = re.search(r'^domain_name\s*=\s*"([^"]*)"', tfvars, re.M).group(1)
    zone = re.search(r'^hosted_zone_id\s*=\s*"([^"]*)"', tfvars, re.M).group(1)
    assert domain == DOMAIN
    assert zone == "" or re.fullmatch(r"Z[A-Z0-9]+", zone)


def test_the_module_stops_at_plan_time_with_a_plain_message_if_the_zone_is_missing():
    module = _read("modules", "static-site", "main.tf")

    assert "precondition" in module and "hosted_zone_id" in module
    assert "docs/production-runsheet.md" in module


def test_the_distribution_compresses_text_responses():
    """Confirmed missing against the real site (every static asset came back uncompressed
    despite Accept-Encoding: gzip, br) -- compress is off by default on this resource, so it has
    to be set, not just left alone."""
    module = _read("modules", "static-site", "main.tf")
    behavior = re.search(r"default_cache_behavior \{(.*?)\n  \}", module, re.S).group(1)

    assert re.search(r"^\s*compress\s*=\s*true\s*$", behavior, re.M)


def test_the_certificate_and_the_distribution_both_cover_www():
    module = _read("modules", "static-site", "main.tf")

    assert "subject_alternative_names = local.www_redirect" in module
    assert "local.www_redirect ? [local.www_name]" in module  # alias
    assert (
        'resource "aws_route53_record" "www_a"' in module
        and 'resource "aws_route53_record" "www_aaaa"' in module
    )


def test_the_zone_lives_in_bootstrap_and_cannot_be_destroyed_by_accident():
    bootstrap = _read("bootstrap", "main.tf")
    zone = re.search(r'resource "aws_route53_zone" "site" \{(.*?)\n\}', bootstrap, re.S).group(1)

    assert "prevent_destroy = true" in zone
    assert 'count = var.domain_name != "" ? 1 : 0' in zone
    outputs = _read("bootstrap", "outputs.tf")
    assert 'output "hosted_zone_id"' in outputs and 'output "hosted_zone_name_servers"' in outputs


# --- production keeps its data ---------------------------------------------------------------------


def test_every_table_is_protected_when_asked_and_production_asks():
    tables = _read("modules", "app-data", "main.tf")

    assert tables.count('resource "aws_dynamodb_table"') == 15
    assert tables.count("deletion_protection_enabled = var.protect_data") == 15
    assert (
        len(re.findall(r"^\s+enabled\s*=\s*var\.protect_data", tables, re.M)) == 15
    )  # point-in-time recovery
    assert re.search(r"protect_data\s*=\s*true", _read("environments", "production", "main.tf"))
    assert "protect_data" not in _read("environments", "dev", "main.tf")


# --- sharded counters (Scaling PR B) ---------------------------------------------------------------


def test_view_counters_have_their_own_table_and_reach_the_lambda_policy():
    tables = _read("modules", "app-data", "main.tf")
    outputs = _read("modules", "app-data", "outputs.tf")

    view_counts = _resource_block(tables, "aws_dynamodb_table", "view_counts")
    assert 'hash_key = "counter_id"' in view_counts
    table_arns = re.search(r'output "table_arns" \{(.*?)\n\}', outputs, re.S).group(1)
    assert "aws_dynamodb_table.view_counts.arn" in table_arns


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_lambda_knows_the_view_counter_table_and_may_batch_read_counters(env):
    text = _read("environments", env, "main.tf")
    policy = re.search(r'sid\s*=\s*"DynamoDBAppTables"(.*?)\n  \}', text, re.S).group(1)

    assert re.search(r"VIEW_COUNTS_TABLE\s*=\s*module\.app_data\.view_counts_table_name", text)
    assert '"dynamodb:BatchGetItem"' in policy


def test_the_content_bucket_is_versioned_and_old_versions_expire():
    production = _read("environments", "production", "main.tf")

    assert 'resource "aws_s3_bucket_versioning" "content"' in production
    assert "noncurrent_days = 30" in production
    assert "depends_on = [aws_s3_bucket_versioning.content]" in production


# --- Cleanup PR: snapshots expire, TTLs were added, log retention was set ---------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_raw_source_snapshots_expire_separately_from_the_rest_of_the_bucket(env):
    """21 days, not the Findings table's own 14-day FINDING_TTL_DAYS -- a safety margin over how
    long a DynamoDB TTL deletion can lag past a Finding's actual expiry, so a live Finding's
    raw_snapshot_s3_key is never left pointing at an already-deleted object."""
    text = _read("environments", env, "main.tf")
    start = re.search(r'id\s*=\s*"expire-old-snapshots"', text).start()
    rule = text[start : start + 400]

    assert 'prefix = "snapshots/"' in rule
    assert "days = 21" in rule


def test_four_more_tables_gained_a_ttl_in_the_cleanup_pr():
    """Findings and ModelConfig already had one; CandidateIdeas, ModerationQueue,
    PromptRefinements and FailedExecutions are the four this PR adds. SecurityEvents (120 days
    after last seen) came later."""
    tables = _read("modules", "app-data", "main.tf")

    assert tables.count('attribute_name = "expires_at"') == 7
    assert tables.count("ttl {") == 7


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_pipeline_lambda_gets_one_90_day_log_group(env):
    """One for_each block, not one resource per function. Its names are literals (a function that
    depends on its log group can't also name it), checked against every function in
    test_every_lambda_has_a_log_group_made_before_it."""
    text = _read("environments", env, "main.tf")

    assert text.count("retention_in_days = 90") == 1
    assert 'resource "aws_cloudwatch_log_group" "lambda"' in text
    assert "for_each          = toset(local.lambda_log_group_function_names)" in text
    function_names = re.findall(r"aws_lambda_function\.[a-z_]+\.function_name,", text)
    assert len(function_names) == 11  # module.observability's list: each function named once
    assert len(set(function_names)) == 11


# --- the www redirect, as CloudFront will run it ---------------------------------------------------


def _redirect(*events: dict) -> list:
    """Render the function for DOMAIN and run it under Node against each event."""
    source = (INFRA / "modules" / "static-site" / "www_redirect.js.tftpl").read_text(encoding="utf-8")
    code = source.replace("${domain}", DOMAIN)
    script = f"{code}\nprocess.stdout.write(JSON.stringify(JSON.parse(process.argv[1]).map(handler)))"
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(events)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=30,
    )
    return json.loads(result.stdout)


def _event(host, uri="/", querystring=None):
    request = {"uri": uri, "headers": {"host": {"value": host}}, "querystring": querystring or {}}
    return {"request": request}


needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@needs_node
def test_www_goes_to_the_bare_domain_with_a_permanent_redirect():
    (reply,) = _redirect(_event(f"www.{DOMAIN}", "/"))

    assert reply["statusCode"] == 301
    assert reply["headers"]["location"]["value"] == f"https://{DOMAIN}/"


@needs_node
def test_the_path_and_query_string_survive_the_redirect():
    query = {"utm": {"value": "a b"}, "tag": {"value": "x", "multiValue": [{"value": "x"}, {"value": "y"}]}}

    (reply,) = _redirect(_event(f"www.{DOMAIN}", "/article/abc", query))

    assert reply["headers"]["location"]["value"] == f"https://{DOMAIN}/article/abc?utm=a b&tag=x&tag=y"


@needs_node
def test_the_bare_domain_is_passed_through_untouched():
    event = _event(DOMAIN, "/article/abc")

    (reply,) = _redirect(event)

    assert reply == event["request"]


@needs_node
@pytest.mark.parametrize(
    "host", ["www.evil.example", "www.bloggerbear.com.evil.example", "wwww.bloggerbear.com", ""]
)
def test_a_forged_host_header_cannot_use_it_as_an_open_redirect(host):
    event = _event(host, "/x")

    (reply,) = _redirect(event)

    assert reply == event["request"]  # passed on, never redirected anywhere


@needs_node
def test_the_host_is_compared_case_insensitively():
    (reply,) = _redirect(_event(f"WWW.{DOMAIN.upper()}", "/"))

    assert reply["statusCode"] == 301


@needs_node
def test_a_request_with_no_host_header_does_not_break_it():
    event = {"request": {"uri": "/", "headers": {}, "querystring": {}}}

    (reply,) = _redirect(event)

    assert reply == event["request"]


def test_the_template_only_interpolates_the_domain():
    source = (INFRA / "modules" / "static-site" / "www_redirect.js.tftpl").read_text(encoding="utf-8")

    assert set(re.findall(r"\$\{([^}]*)\}", source)) == {"domain"}


# --- what the deploy role is allowed to create -------------------------------------------------------


def _deploy_policy_log_group_patterns() -> set[tuple[str, str]]:
    """(region, name pattern) for every log group ARN the CI deploy role may manage."""
    bootstrap = _read("bootstrap", "main.tf")
    patterns = set()
    # The region is either written out (us-east-1, for CloudFront) or the home region's variable.
    for match in re.finditer(
        r'"arn:aws:logs:(\$\{var\.aws_region\}|[a-z0-9-]+):\*:log-group:([^"]*?)(?::\*)?"', bootstrap
    ):
        patterns.add((match.group(1), match.group(2)))
    return patterns


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_deploy_role_may_create_every_log_group_an_environment_declares(env):
    """A log group in another region needs its own ARN in the deploy role's policy. Production's shared
    CloudFront WAF logs to us-east-1; only the home region was allowed, so its first apply was refused
    with AccessDenied on logs:CreateLogGroup. Dev could not have shown it (no CloudFront ACL)."""
    import fnmatch

    text = _read("environments", env, "main.tf")
    allowed = _deploy_policy_log_group_patterns()
    found = 0
    for match in re.finditer(r'^resource "aws_cloudwatch_log_group" "[^"]+" \{\n(.*?)^\}', text, re.S | re.M):
        body = match.group(1)
        if "for_each" in body:
            # aws_cloudwatch_log_group.lambda (Cleanup PR): named "/aws/lambda/${each.value}" --
            # a for_each this static-analysis loop can't resolve to a literal name. Checked
            # properly instead by test_every_pipeline_lambdas_function_name_is_covered_too below.
            continue
        region = (
            "us-east-1" if re.search(r"^\s*provider\s*=\s*aws\.us_east_1", body, re.M) else _HOME
        )
        name = re.sub(r"\$\{[^}]*\}", "x", re.search(r'^\s*name\s*=\s*"([^"]+)"', body, re.M).group(1))
        found += 1
        assert any(r == region and fnmatch.fnmatch(name, pattern) for r, pattern in allowed), (
            f"{env}: log group {name!r} in {region} is not covered by the deploy role's policy "
            "(infra/bootstrap/main.tf, WafLogGroups / LambdaLogGroups)"
        )
    assert found >= 2


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_pipeline_lambdas_function_name_is_covered_too(env):
    """The other half of the skip above: aws_cloudwatch_log_group.lambda's for_each can't be
    resolved to a literal name by regex, so this checks the thing that actually decides whether
    the deploy role can create it -- every function_name feeding that for_each really does start
    with "bloggerbear-", which is exactly what infra/bootstrap/main.tf's LambdaLogGroups
    statement (/aws/lambda/bloggerbear-*) covers."""
    text = _read("environments", env, "main.tf")

    names = re.findall(r'function_name\s*=\s*"([^"]+)"', text)
    assert len(names) >= 10
    assert all(name.startswith("bloggerbear-") for name in names)


def test_the_shared_cloudfront_waf_log_group_really_is_in_us_east_1():
    production = _read("environments", "production", "main.tf")
    block = re.search(
        r'resource "aws_cloudwatch_log_group" "waf_shared" \{\n(.*?)^\}', production, re.S | re.M
    ).group(1)

    assert "provider = aws.us_east_1" in block
    assert ("us-east-1", "aws-waf-logs-bloggerbear-*") in _deploy_policy_log_group_patterns()


# --- the frontend deploys from a minified build, not frontend/ itself -------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_frontend_deploys_from_the_minified_build_directory(env):
    """frontend_dir has to point at frontend-dist/ (scripts/minify_frontend.py's output), not
    frontend/ itself, or the deploy ships the unminified source -- see that local's own comment
    on why, and CI's "Minify frontend assets" step (terraform.yml/terraform-production-release.yml)
    for where frontend-dist/ actually gets built before apply."""
    text = _read("environments", env, "main.tf")

    assert 'frontend_dir = "${path.module}/../../../frontend-dist"' in text


@pytest.mark.parametrize("workflow", ["terraform.yml", "terraform-production-release.yml"])
def test_every_apply_workflow_minifies_the_frontend_first(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    assert "python scripts/minify_frontend.py" in text
    minify_step = text.index("- name: Minify frontend assets")
    apply_step = text.index("- name: Terraform apply")
    assert minify_step < apply_step


# --- the AgentCore web search gateway ----------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_each_environment_has_the_web_search_gateway_and_can_call_it(env):
    text = _read("environments", env, "main.tf")

    assert len(_module_blocks(text, "modules/web-search")) == 1
    for var in ("AGENTCORE_WEB_SEARCH_URL", "AGENTCORE_WEB_SEARCH_REGION", "AGENTCORE_WEB_SEARCH_TOOL"):
        assert re.search(rf"{var}\s*=\s*module\.web_search\.", text), var
    grant = re.search(r'data "aws_iam_policy_document" "lambda_web_search" \{(.*?)\n\}', text, re.S)
    assert grant and "bedrock-agentcore:InvokeGateway" in grant.group(1)
    assert "module.web_search.gateway_arn" in grant.group(1)


def test_the_web_search_connector_version_has_the_date_filter_the_app_sends():
    module = _read("modules", "web-search", "main.tf")

    assert re.search(r'connector_id\s*=\s*"web-search"', module)
    major, minor, _ = re.search(r'version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', module).groups()
    assert (int(major), int(minor)) >= (1, 2)  # publishedDateFilter arrived in 1.2.0
    assert re.search(r'authorizer_type\s*=\s*"AWS_IAM"', module)


def test_the_gateway_speaks_the_mcp_version_the_app_sends():
    module = _read("modules", "web-search", "main.tf")
    source = (ROOT / "lambdas" / "common" / "web_search.py").read_text(encoding="utf-8")

    sent = re.search(r'MCP_PROTOCOL_VERSION = "([^"]+)"', source).group(1)
    assert f'"{sent}"' in re.search(r"supported_versions\s*=\s*\[([^\]]*)\]", module).group(1)


def test_the_deploy_role_can_create_the_gateway_and_pass_it_its_role():
    bootstrap = _read("bootstrap", "main.tf")

    assert "arn:aws:iam::*:role/bloggerbear-*-agentcore-gateway" in bootstrap
    statement = re.search(r'sid\s*=\s*"AgentCoreWebSearchGateway"(.*?)\n  \}', bootstrap, re.S).group(1)
    region = re.search(r'variable\s*=\s*"aws:RequestedRegion"\s*values\s*=\s*\["([^"]+)"\]', statement).group(
        1
    )
    default = re.search(
        r'variable "region" \{.*?default\s*=\s*"([^"]+)"',
        _read("modules", "web-search", "variables.tf"),
        re.S,
    )
    assert region == default.group(1)


# --- WAF logs for visitor traffic: blocks only, fingerprint headers redacted, 14 days ------------
# The Privacy Policy's section 5 (frontend/privacy.html) promises exactly this, so a change here that
# quietly logged allowed requests again, or kept them longer, would make the policy untrue.

_VISITOR_WAF_LOGGING = [
    ("dev", "public_api", "waf_public_api"),
    ("production", "public_api", "waf_public_api"),
    ("production", "shared", "waf_shared"),
]


def _resource_block(text: str, kind: str, name: str) -> str:
    return re.search(rf'^resource "{kind}" "{name}" \{{\n(.*?)^\}}', text, re.S | re.M).group(1)


def _redacted_headers(text: str) -> list[str]:
    body = re.search(r"waf_log_redacted_headers = \[(.*?)\]", text, re.S).group(1)
    return re.findall(r'"([^"]+)"', body)


@pytest.mark.parametrize(("env", "logging_config", "log_group"), _VISITOR_WAF_LOGGING)
def test_visitor_waf_logs_keep_only_blocked_or_counted_requests(env, logging_config, log_group):
    text = _read("environments", env, "main.tf")
    block = _resource_block(text, "aws_wafv2_web_acl_logging_configuration", logging_config)

    assert re.search(r'default_behavior\s*=\s*"DROP"', block)
    assert re.search(r'behavior\s*=\s*"KEEP"', block)
    kept = set(re.findall(r'action\s*=\s*"([A-Z_]+)"', block))
    assert kept == {"BLOCK", "COUNT"}


@pytest.mark.parametrize(("env", "logging_config", "log_group"), _VISITOR_WAF_LOGGING)
def test_visitor_waf_logs_redact_fingerprinting_headers(env, logging_config, log_group):
    text = _read("environments", env, "main.tf")
    block = _resource_block(text, "aws_wafv2_web_acl_logging_configuration", logging_config)

    assert re.search(r"for_each\s*=\s*local\.waf_log_redacted_headers", block)
    assert "single_header" in block
    headers = _redacted_headers(text)
    for header in ("user-agent", "referer", "accept-language", "sec-ch-ua", "sec-ch-ua-platform"):
        assert header in headers
    assert all(header == header.lower() for header in headers)
    assert len(headers) <= 100  # WAF's limit on redacted fields


@pytest.mark.parametrize(("env", "logging_config", "log_group"), _VISITOR_WAF_LOGGING)
def test_visitor_waf_logs_are_kept_for_14_days(env, logging_config, log_group):
    text = _read("environments", env, "main.tf")
    block = _resource_block(text, "aws_cloudwatch_log_group", log_group)

    assert re.search(r"retention_in_days\s*=\s*local\.waf_visitor_log_retention_days", block)
    assert re.search(r"waf_visitor_log_retention_days\s*=\s*14\b", text)


def test_both_environments_redact_the_same_headers():
    assert _redacted_headers(_read("environments", "dev", "main.tf")) == _redacted_headers(
        _read("environments", "production", "main.tf")
    )


def test_the_privacy_policy_states_the_waf_log_retention():
    policy = (ROOT / "frontend" / "privacy.html").read_text(encoding="utf-8")
    days = re.search(
        r"waf_visitor_log_retention_days\s*=\s*(\d+)", _read("environments", "production", "main.tf")
    ).group(1)

    assert f"for {days} days, then deleted automatically" in policy
    assert "Only requests the firewall blocks or flags are logged" in policy


# --- feedback spam: the per-IP WAF cap, and the alarms on rejected feedback ---------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_waf_feedback_cap_is_ten_per_ip_per_five_minutes(env):
    text = _read("environments", env, "main.tf")
    rule = re.search(r'name\s*=\s*"feedback-rate-limit"(.*?)visibility_config', text, re.S).group(1)

    assert re.search(r"limit\s*=\s*10\b", rule)  # WAF's lowest allowed rate limit
    assert re.search(r"evaluation_window_sec\s*=\s*300\b", rule)
    assert re.search(r'aggregate_key_type\s*=\s*"IP"', rule)


def test_the_feedback_alarms_read_the_public_api_handlers_own_log_line():
    module = _read("modules", "observability", "main.tf")
    handler = (ROOT / "lambdas" / "public_api_handler.py").read_text(encoding="utf-8")
    logged = "print(f\"public_api_handler: rejected a feedback submission ({screened['dropped_because']})\")"

    assert logged in handler
    assert r'pattern        = "\"rejected a feedback submission\""' in module
    assert r'pattern        = "\"rejected a feedback submission (screening_budget)\""' in module
    assert 'MODEL_BUDGET = "screening_budget"' in (
        ROOT / "lambdas" / "common" / "comment_screening.py"
    ).read_text(encoding="utf-8")
    for alarm in ("feedback_rejections_spike", "feedback_screening_budget_used_up"):
        block = re.search(
            rf'resource "aws_cloudwatch_metric_alarm" "{alarm}" \{{(.*?)\n\}}', module, re.S
        ).group(1)
        assert "alarm_actions = [aws_sns_topic.alerts.arn]" in block


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_feedback_alarms_watch_the_public_api_log_group(env):
    block = re.search(
        r'module "observability" \{(.*?)\n\}', _read("environments", env, "main.tf"), re.S
    ).group(1)

    assert re.search(
        r"feedback_log_group_name\s*=\s*aws_cloudwatch_log_group\.lambda\[aws_lambda_function\.public_api\.function_name\]\.name",
        block,
    )


# --- the deploy role can still read an event source mapping that has gone ----------------------


def test_the_deploy_role_can_read_event_source_mappings_that_no_longer_exist():
    """A deleted mapping is authorized against "*", not its ARN: without this, refresh fails with
    AccessDeniedException instead of "not found", and Terraform can never recreate it."""
    bootstrap = _read("bootstrap", "main.tf")
    statement = re.search(r'sid\s*=\s*"LambdaEventSourceMappingReads"(.*?)\n  \}', bootstrap, re.S).group(1)

    assert set(re.findall(r'"(lambda:[A-Za-z]+)"', statement)) == {
        "lambda:GetEventSourceMapping",
        "lambda:ListEventSourceMappings",
    }
    assert re.search(r'resources\s*=\s*\["\*"\]', statement)


# --- every schedule may invoke what it targets --------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_schedule_using_the_invoke_role_may_invoke_its_target(env):
    """stats-rollover and cost-explorer-poll were scheduled with this role but never allowed to
    invoke their Lambdas, so neither ever ran. Every target must be in the role's policy."""
    text = _read("environments", env, "main.tf")
    policy = re.search(r'data "aws_iam_policy_document" "scheduler_invoke" \{(.*?)\n\}', text, re.S).group(1)
    allowed = set(re.findall(r"resources\s*=\s*\[([a-z_.]+)\.arn\]", policy))

    targets = []
    for match in re.finditer(r'^resource "aws_scheduler_schedule" "[a-z_]+" \{\n(.*?)^\}', text, re.S | re.M):
        body = match.group(1)
        if re.search(r"role_arn\s*=\s*aws_iam_role\.scheduler_invoke\.arn", body):
            targets.append(re.search(r"\barn\s*=\s*([a-z_.]+)\.arn", body).group(1))

    assert len(targets) >= 5  # research ticks are per topic, created at runtime, not here
    assert set(targets) <= allowed, set(targets) - allowed


# --- a Lambda's log group exists before the Lambda can be invoked ------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_lambda_has_a_log_group_made_before_it(env):
    """A function invoked before its log group exists makes its own (no retention), and the next
    apply fails with ResourceAlreadyExistsException -- dev hit this rebuilding after a destroy."""
    text = _read("environments", env, "main.tf")
    functions = re.findall(
        r'^resource "aws_lambda_function" "[a-z_]+" \{\n  function_name = "([^"]+)"\n  depends_on\s*=\s*'
        r"\[aws_cloudwatch_log_group\.lambda\]",
        text,
        re.M,
    )
    all_functions = re.findall(
        r'^resource "aws_lambda_function" "[a-z_]+" \{\n  function_name = "([^"]+)"', text, re.M
    )
    listed = re.findall(
        r'"([^"]+)"', re.search(r"lambda_log_group_function_names = \[(.*?)\]", text, re.S).group(1)
    )

    assert functions == all_functions and len(all_functions) >= 10
    assert sorted(listed) == sorted(all_functions)
    assert re.search(r"for_each\s*=\s*toset\(local\.lambda_log_group_function_names\)", text)


# --- dashboards --------------------------------------------------------------------------------


def test_the_dashboards_open_on_a_span_that_shows_something():
    module = _read("modules", "observability", "main.tf")
    pipeline = re.search(r'resource "aws_cloudwatch_dashboard" "pipeline" \{(.*?)\n\}\n', module, re.S).group(
        1
    )
    runs = re.search(r'resource "aws_cloudwatch_dashboard" "lambda_runs" \{(.*?)\n\}\n', module, re.S).group(
        1
    )

    assert 'start          = "-P7D"' in pipeline and "period = 3600" in pipeline
    assert 'yAxis = "right"' in pipeline  # duration off the count axis
    assert "setPeriodToTimeRange = true" in runs


def test_the_runs_dashboard_counts_match_the_handlers_log_lines():
    module = _read("modules", "observability", "main.tf")
    handler = (ROOT / "lambdas" / "public_api_handler.py").read_text(encoding="utf-8")

    assert 'kept = "comment kept" if final_comment else "vote only"' in handler
    assert 'print(f"public_api_handler: accepted a feedback submission ({kept})")' in handler
    assert r'pattern        = "\"accepted a feedback submission\""' in module
    assert r'pattern        = "\"accepted a feedback submission (comment kept)\""' in module
    assert r'pattern        = "\"rejected a feedback submission (model_dropped)\""' in module
    assert 'MODEL_DROPPED = "model_dropped"' in (
        ROOT / "lambdas" / "common" / "comment_screening.py"
    ).read_text(encoding="utf-8")


def _dashboards() -> str:
    return _read("modules", "observability", "api_waf_dashboards.tf")


def _dashboard_resource(name: str) -> str:
    pattern = rf'resource "aws_cloudwatch_dashboard" "{name}" \{{(.*?)\n\}}\n'
    return re.search(pattern, _dashboards(), re.S).group(1)


def test_one_edge_dashboard_opens_on_a_week_of_hourly_points():
    body = _dashboard_resource("edge")

    assert 'start          = "-P7D"' in body
    assert 'dashboard_name = "bloggerbear-${var.environment_name}-edge"' in body
    assert "concat(local.api_gateway_widgets, local.waf_widgets)" in body
    assert "period = 3600" in _dashboards()
    # One dashboard, not one each: every dashboard past the account's first three is US$3 a month.
    assert _dashboards().count('resource "aws_cloudwatch_dashboard"') == 1


def test_the_edge_dashboard_is_created_in_production_only():
    assert "var.edge_dashboard_enabled &&" in _dashboard_resource("edge")
    variables = _read("modules", "observability", "variables.tf")
    variable = re.search(r'variable "edge_dashboard_enabled" \{(.*?)\n\}', variables, re.S).group(1)
    assert "default     = false" in variable
    assert "edge_dashboard_enabled = true" in _read("environments", "production", "main.tf")
    assert "edge_dashboard_enabled = true" not in _read("environments", "dev", "main.tf")


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_dashboards_cover_both_apis_and_every_web_acl(env):
    text = _read("environments", env, "main.tf")
    block = re.search(r'^module "observability" \{\n(.*?)^\}', text, re.S | re.M).group(1)

    for api in ("public_api", "admin_api"):
        assert f"api_name         = module.{api}.api_name" in block
        assert f"access_log_group = module.{api}.access_log_group_name" in block
    assert "distribution_id            = module.public_api_cdn.distribution_id" in block
    assert "aws_wafv2_web_acl.public_api.visibility_config[0].metric_name" in block
    assert "aws_wafv2_web_acl.admin.visibility_config[0].metric_name" in block
    assert "waf_cloudfront_acl = {" in block


def test_every_cloudfront_widget_reads_us_east_1_with_the_global_region_dimension():
    """CloudFront's metrics (and a CLOUDFRONT-scope ACL's) exist only in us-east-1; a widget pointed
    at the API's own region draws nothing, which is exactly how a dashboard ends up empty."""
    text = _dashboards()

    for line in text.splitlines():
        if '"AWS/CloudFront"' in line:
            assert '"Region", "Global"' in line, line
    cdn = re.search(r"cdn_section = (.*?)\n  \)\]\)\n", text, re.S).group(1)
    assert 'region = local.api_region' not in cdn.replace('region = local.api_region }', '')
    assert cdn.count('region = local.cloudfront_region') >= 3
    assert re.search(r'^  cloudfront_region = "us-east-1"$', text, re.M)
    cloudfront_acl = re.search(r"waf_cloudfront_section = (.*?)\n  \]\]\)\n", text, re.S).group(1)
    assert "local.api_region" not in cloudfront_acl
    # Both possible dimension shapes for a CloudFront ACL's metrics, so neither guess leaves it blank.
    assert "{AWS/WAFV2,Rule,WebACL}" in cloudfront_acl
    assert "{AWS/WAFV2,Region,Rule,WebACL}" in cloudfront_acl


def test_the_api_dashboard_has_every_widget_the_operator_asked_for():
    text = _dashboards()

    for metric in ("Count", "4XXError", "5XXError", "Latency", "IntegrationLatency"):
        assert f'"AWS/ApiGateway", "{metric}"' in text
    for stat in ("p50", "p90", "p99"):
        assert f'"{stat}"' in text
    assert "filter status = 429" in text  # API Gateway's own throttling
    assert "stats count(*) as requests by status" in text  # the 400/403/429/500/502/504 split
    assert '"AWS/CloudFront", "CacheHitRate"' in text  # when the additional metrics are on
    assert '"AWS/CloudFront", "Requests"' in text  # always: the CDN against API Gateway


def test_the_waf_dashboard_has_totals_per_rule_and_the_top_blocked_requests():
    text = _dashboards()

    for metric in ("AllowedRequests", "BlockedRequests", "CountedRequests"):
        assert f'"{metric}"' in text
    assert 'NOT Rule=\\"ALL\\"' in text  # per rule, rate limits included, without the total
    assert 'filter action = \\"BLOCK\\"' in text
    assert "stats count(*) as blocked by rule, address, path" in text
    # Behind the CDN the client address is an edge: the visitor's is in x-viewer-ip.
    assert "coalesce(viewerIp, httpRequest.clientIp)" in text


# --- API Gateway throttling and access logs (Scaling PR C) -----------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_apis_are_throttled_and_the_feedback_post_more_tightly(env):
    text = _read("environments", env, "main.tf")
    public = re.search(r'^module "public_api" \{\n(.*?)^\}', text, re.S | re.M).group(1)
    admin = re.search(r'^module "admin_api" \{\n(.*?)^\}', text, re.S | re.M).group(1)

    for block in (public, admin):
        assert re.search(r"throttling_rate_limit\s*=\s*\d+", block)
        assert re.search(r"throttling_burst_limit\s*=\s*\d+", block)
    assert '"POST /articles/{article_id}/feedback" = { rate_limit = 2, burst_limit = 5 }' in public
    assert '"POST /articles/{article_id}/feedback",' in public  # the override names a real route


def test_a_route_override_names_its_method_the_way_api_gateway_expects():
    """API Gateway names a method by its path with every "/" written as "~1" (RFC 6901), and the
    provider passes method_path through untouched."""
    module = _read("modules", "rest-api", "main.tf")

    assert 'method_path = "*/*"' in module
    escaped = 'method_path = "${replace(split(" ", each.key)[1], "/", "~1")}/${split(" ", each.key)[0]}"'
    assert escaped in module


def test_access_logs_record_what_happened_never_who_asked():
    module = _read("modules", "rest-api", "main.tf")
    stage = re.search(r'resource "aws_api_gateway_stage" "this" \{(.*?)\n\}', module, re.S).group(1)

    assert '"\\"status\\":$context.status,"' in stage  # a number Logs Insights can compare
    assert "$context.error.responseType" in stage
    assert "$context.identity" not in stage  # no source IP, user agent or caller
    assert "userAgent" not in stage and "sourceIp" not in stage


def test_the_deploy_role_may_create_the_access_log_groups_and_api_gateway_may_write_them():
    bootstrap = _read("bootstrap", "main.tf")
    module = _read("modules", "rest-api", "main.tf")

    assert 'name              = "/aws/apigateway/${var.name}-access"' in module
    assert f'"arn:aws:logs:{_HOME}:*:log-group:/aws/apigateway/bloggerbear-*"' in bootstrap
    assert 'resource "aws_api_gateway_account" "this"' in bootstrap
    assert "AmazonAPIGatewayPushToCloudWatchLogs" in bootstrap


# --- the public API's CDN (Scaling PR C) -----------------------------------------------------------


def test_the_api_cdn_caches_only_what_the_api_marks_cacheable():
    module = _read("modules", "api-cdn", "main.tf")
    policy = _resource_block(module, "aws_cloudfront_cache_policy", "api")

    assert "default_ttl = 0" in policy and "min_ttl     = 0" in policy
    assert 'cached_methods           = ["GET", "HEAD"]' in module
    # Its own distribution: the site's maps every 403/404 to an HTML page, and the feedback form
    # reads the JSON body of a 403.
    assert "custom_error_response {" not in module


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_frontend_calls_the_api_through_its_cdn_and_the_csp_allows_it(env):
    text = _read("environments", env, "main.tf")

    assert 'window.PUBLIC_API_URL = "${module.public_api_cdn.url}";' in text
    site = _module_blocks(text, "modules/static-site")[0]
    assert re.search(r"extra_connect_src = \[\s*module\.public_api_cdn\.domain_name\b", site)
    assert "api_domain           = module.public_api.api_domain" in text


def test_the_cdn_origin_never_depends_on_the_lambda():
    """The site's CSP names the API's CDN, and dev's Lambdas are told the site's URL: if the CDN
    depended on the API's deployment (and so its Lambda), that would be a dependency cycle."""
    outputs = _read("modules", "rest-api", "outputs.tf")
    api_domain = re.search(r'output "api_domain" \{(.*?)\n\}', outputs, re.S).group(1)

    assert "aws_api_gateway_rest_api.this.id" in api_domain
    assert "deployment" not in api_domain and "stage" not in api_domain.split("description")[0]


@needs_node
def test_the_viewer_ip_header_is_always_cloudfronts_own_record_of_the_visitor():
    source = (INFRA / "modules" / "api-cdn" / "viewer_ip.js").read_text(encoding="utf-8")
    event = {"viewer": {"ip": "198.51.100.7"}, "request": {"headers": {"x-viewer-ip": {"value": "1.2.3.4"}}}}
    script = f"{source}\nprocess.stdout.write(JSON.stringify(handler(JSON.parse(process.argv[1]))))"
    result = subprocess.run(
        [NODE, "-e", script, json.dumps(event)], capture_output=True, text=True, check=True, timeout=30
    )

    assert json.loads(result.stdout)["headers"]["x-viewer-ip"] == {"value": "198.51.100.7"}


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_waf_trusts_the_viewer_ip_only_on_requests_carrying_the_cdn_secret(env):
    acl = _resource_block(_read("environments", env, "main.tf"), "aws_wafv2_web_acl", "public_api")
    rules = dict(re.findall(r'rule \{\n    name     = "([^"]+)"(.*?)\n  \}\n', acl, re.S))

    for name in ("rate-limit-via-cdn", "feedback-rate-limit-via-cdn"):
        body = rules[name]
        assert re.search(r'aggregate_key_type\s*=\s*"FORWARDED_IP"', body)
        assert 'header_name       = "x-viewer-ip"' in body
        assert 'fallback_behavior = "NO_MATCH"' in body
        assert "search_string         = random_password.api_origin_verify.result" in body
        assert "not_statement" not in body
    for name in ("rate-limit", "feedback-rate-limit"):
        body = rules[name]
        assert re.search(r'aggregate_key_type\s*=\s*"IP"', body)
        assert "not_statement" in body and "random_password.api_origin_verify.result" in body
    assert '"/feedback"' in rules["feedback-rate-limit-via-cdn"]


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_cdn_secret_never_reaches_a_waf_log(env):
    assert "x-origin-verify" in _redacted_headers(_read("environments", env, "main.tf"))


def test_the_privacy_policy_describes_the_api_request_log():
    policy = (ROOT / "frontend" / "privacy.html").read_text(encoding="utf-8")

    assert "The public API also keeps a request log" in policy
    assert "holds no IP address" in policy


# --- a production release runs the same checks as a PR -----------------------------------------


def test_a_production_release_waits_for_the_security_scans_and_tests():
    workflows = ROOT / ".github" / "workflows"
    release = (workflows / "terraform-production-release.yml").read_text(encoding="utf-8")

    assert re.search(r"^  security:\n    uses: \./\.github/workflows/security\.yml", release, re.M)
    assert re.search(r"^  lint-test:\n    uses: \./\.github/workflows/python-ci\.yml", release, re.M)
    assert release.count("ref: ${{ github.event.release.tag_name }}") >= 3  # both checks and the apply
    assert re.search(r"^  apply:\n    needs: \[security, lint-test\]", release, re.M)
    for name in ("security.yml", "python-ci.yml"):
        called = (workflows / name).read_text(encoding="utf-8")
        assert "  workflow_call:" in called
        assert "ref: ${{ inputs.ref }}" in called


def test_a_dev_apply_waits_for_the_same_checks_in_one_run():
    workflows = ROOT / ".github" / "workflows"
    dev = (workflows / "terraform.yml").read_text(encoding="utf-8")

    assert re.search(r"^  security:\n    uses: \./\.github/workflows/security\.yml", dev, re.M)
    assert re.search(r"^  lint-test:\n    uses: \./\.github/workflows/python-ci\.yml", dev, re.M)
    assert "    needs: [security, lint-test]" in dev
    for path in ("lambdas/**", "frontend/**"):  # a code- or site-only merge still deploys
        assert f"      - '{path}'" in dev
    # ...and the checks don't also run as separate workflows for the same push.
    for name in ("security.yml", "python-ci.yml"):
        called = (workflows / name).read_text(encoding="utf-8")
        assert "[dev" not in called and "- dev" not in called
    assert not (workflows / "dev-gatekeeper.yml").exists()


def test_pull_request_only_checks_live_in_their_own_workflow():
    """A push or release run shows only what it does: no skipped validate or secret-scan boxes."""
    workflows = ROOT / ".github" / "workflows"
    pr_checks = (workflows / "pr-checks.yml").read_text(encoding="utf-8")

    assert re.search(r"^on:\n  pull_request:\n", pr_checks, re.M)
    assert "\n  validate:\n" in pr_checks and "terraform -chdir=\"$dir\" validate" in pr_checks
    assert "\n  secret-scan:\n" in pr_checks and "trufflesecurity/trufflehog@" in pr_checks
    for name in ("terraform.yml", "security.yml", "terraform-production-release.yml"):
        text = (workflows / name).read_text(encoding="utf-8")
        assert "\n  validate:\n" not in text and "\n  secret-scan:\n" not in text
        assert "github.event_name == 'pull_request'" not in text
    assert "  pull_request:" not in (workflows / "terraform.yml").read_text(encoding="utf-8")


@pytest.mark.parametrize("env", ["dev", "production"])
def test_personal_values_never_print_in_public_ci_logs(env):
    """The admin IP and alert email are secrets, but GitHub masks only a secret's exact text, and a
    plan prints each list element on its own: both appeared in apply logs before they were sensitive."""
    variables = _read("environments", env, "variables.tf")
    for name in ("admin_allowed_cidrs", "alert_email"):
        block = variables.split(f'variable "{name}" {{')[1].split("\n}\n")[0]
        assert "\n  sensitive   = true\n" in block, name
    module = _read("modules", "observability", "variables.tf")
    assert "\n  sensitive   = true\n" in module.split('variable "alert_email" {')[1].split("\n}\n")[0]


def test_deploy_role_arns_come_from_secrets():
    """A variable prints in plain text in every step's log; on a public repo the logs are public."""
    workflows = ROOT / ".github" / "workflows"
    for name, role in (
        ("terraform.yml", "AWS_DEV_DEPLOY_ROLE_ARN"),
        ("destroy-dev.yml", "AWS_DEV_DEPLOY_ROLE_ARN"),
        ("terraform-production-release.yml", "AWS_PROD_DEPLOY_ROLE_ARN"),
    ):
        text = (workflows / name).read_text(encoding="utf-8")
        assert f"role-to-assume: ${{{{ secrets.{role} ||" in text, name
        assert f"role-to-assume: ${{{{ vars.{role} }}}}" not in text, name


# --- Configurable AWS accounts (docs/deployment-runsheet.md) ---------------------------------------
#
# A fork deploys to its own account, or to two, by setting GitHub secrets and variables. The
# original deployment sets none of them, so every one must fall back to exactly what ran before.

_ROOTS = (("bootstrap",), ("environments", "dev"), ("environments", "production"))

# The only 12-digit account IDs allowed in the tree, the same ones .gitleaks.toml allows: AWS's
# documentation placeholders, and the account the AWS Lambda Web Adapter project publishes its
# public layer from (printed in its README; the layer ARN has to be written out in full).
_PLACEHOLDER_ACCOUNTS = {"123456789012", "111111111111", "000000000000"}
_PUBLIC_ACCOUNTS = {"753240598075"}
_ACCOUNT_ID_PATTERNS = (
    # In an ARN's account position, and next to the word "account": .gitleaks.toml's two rules.
    re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12}):"),
    re.compile(r"(?i)\baccount[ _-]?(?:id)?\b[^0-9\n]{0,30}\b(\d{12})\b"),
    # And on its own, as a whole quoted string: how one would be written into a variable's default.
    re.compile(r"[\"'](\d{12})[\"']"),
)
_SCANNED_SUFFIXES = {".tf", ".tfvars", ".hcl", ".yml", ".yaml", ".py", ".sh", ".md", ".json", ".toml", ".txt"}
_SKIPPED_DIRS = {".terraform", "node_modules", "__pycache__", ".venv", "venv", ".pytest_cache", ".ruff_cache"}


def _provider_blocks(text: str) -> list[str]:
    return re.findall(r'^provider "aws" \{\n.*?^\}\n', text, re.M | re.S)


def test_no_aws_account_id_is_written_into_the_code():
    """An account ID in the code ties it to one deployment: the Bedrock model's default was an ARN
    in one account, which no other account could call."""
    found = []
    for top in ("infra", ".github", "scripts", "lambdas"):
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES:
                continue
            if _SKIPPED_DIRS & set(path.relative_to(ROOT).parts):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in _ACCOUNT_ID_PATTERNS:
                for account in pattern.findall(text):
                    if account not in _PLACEHOLDER_ACCOUNTS | _PUBLIC_ACCOUNTS:
                        found.append(f"{path.relative_to(ROOT).as_posix()}: {account[:2]}..........")
    assert not found, found


def test_the_one_public_account_id_is_only_ever_the_web_adapter_layer():
    for top in ("infra", ".github", "scripts", "lambdas"):
        for path in sorted((ROOT / top).rglob("*")):
            if not path.is_file() or path.suffix not in _SCANNED_SUFFIXES or path == Path(__file__):
                continue
            if _SKIPPED_DIRS & set(path.relative_to(ROOT).parts):
                continue
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                for account in _PUBLIC_ACCOUNTS:
                    if account in line:
                        assert f":{account}:layer:LambdaAdapterLayerX86:" in line, (path.name, line)


@pytest.mark.parametrize("root", _ROOTS, ids=lambda parts: parts[-1])
def test_every_aws_provider_refuses_any_account_but_the_expected_one(root):
    """Each alias is a provider of its own: one left out would still run against the wrong account."""
    blocks = _provider_blocks(_read(*root, "main.tf"))
    assert len(blocks) == (1 if root == ("bootstrap",) else 2)
    for block in blocks:
        assert (
            '\n  allowed_account_ids = var.aws_account_id == "" ? null : [var.aws_account_id]\n' in block
        ), block

    variable = _read(*root, "variables.tf").split('variable "aws_account_id" {')[1].split("\n}\n")[0]
    # Empty by default, and empty means no check: nothing changes for a deployment that sets nothing.
    assert '\n  default     = ""\n' in variable
    assert 'var.aws_account_id == "" || can(regex("^[0-9]{12}$", var.aws_account_id))' in variable


@pytest.mark.parametrize("root", _ROOTS, ids=lambda parts: parts[-1])
def test_the_account_id_is_sensitive_and_nothing_can_print_it(root):
    """GitHub masks only the secret's exact text. `sensitive` covers what Terraform prints itself;
    it also makes Terraform refuse an error message or an unmarked output built from the value, so
    neither may exist."""
    variables = _read(*root, "variables.tf")
    variable = variables.split('variable "aws_account_id" {')[1].split("\n}\n")[0]
    assert '\n  sensitive   = true\n' in variable
    # No validation message, here or on a variable checked against it, repeats the value.
    for message in re.findall(r"^\s*error_message\s*=\s*(.*)$", variables, re.M):
        assert "var.aws_account_id" not in message and "${" not in message, message
    # It reaches the provider blocks and nothing else: no output, no local, no module argument.
    main = _uncommented(_read(*root, "main.tf"))
    assert main.count("var.aws_account_id") == 2 * len(_provider_blocks(_read(*root, "main.tf")))
    for name in ("outputs.tf", "locals.tf"):
        path = INFRA.joinpath(*root, name)
        if path.is_file():
            assert "aws_account_id" not in _uncommented(path.read_text(encoding="utf-8")), name


def test_production_really_has_a_us_east_1_provider_and_it_is_guarded_too():
    blocks = _provider_blocks(_read("environments", "production", "main.tf"))
    alias = [block for block in blocks if 'alias  = "us_east_1"' in block]
    assert len(alias) == 1 and 'region = "us-east-1"' in alias[0]
    assert "allowed_account_ids" in alias[0]


def test_no_module_configures_a_provider_of_its_own():
    """Or the roots' account check would not cover it."""
    for path in (INFRA / "modules").rglob("*.tf"):
        assert not re.search(r'^provider "', path.read_text(encoding="utf-8"), re.M), path


@pytest.mark.parametrize(
    "workflow, account, bucket",
    [
        ("terraform.yml", "AWS_DEV_ACCOUNT_ID", "TF_STATE_BUCKET_DEV"),
        ("destroy-dev.yml", "AWS_DEV_ACCOUNT_ID", "TF_STATE_BUCKET_DEV"),
        ("terraform-production-release.yml", "AWS_PROD_ACCOUNT_ID", "TF_STATE_BUCKET_PROD"),
    ],
)
def test_each_deploy_workflow_passes_the_account_settings_and_falls_back_to_what_it_did(
    workflow, account, bucket
):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    # Secrets, and only secrets: a variable prints in plain text in public logs, so neither the
    # account ID nor the state bucket's name may be read from one, even as a fallback.
    assert f"      TF_VAR_aws_account_id: ${{{{ secrets.{account} }}}}\n" in text
    assert f"      TF_STATE_BUCKET: ${{{{ secrets.{bucket} }}}}\n" in text
    assert f"vars.{account}" not in text and f"vars.{bucket}" not in text
    # The name prefix is a plain variable (it is in public names anyway), and unset it is the
    # prefix this project has always used: an empty string would fail the variable's validation.
    assert "      TF_VAR_unique_name_prefix: ${{ vars.UNIQUE_NAME_PREFIX || 'bloggerbear' }}\n" in text
    assert "UNIQUE_NAME_SUFFIX" not in text and "unique_name_suffix" not in text

    # Unset, the init is the bare command it always was; set, only the bucket and the bucket's
    # region are overridden, each only when its own setting is there.
    assert (
        "          init_args=()\n"
        '          if [ -n "${TF_STATE_BUCKET:-}" ]; then\n'
        '            init_args+=("-backend-config=bucket=${TF_STATE_BUCKET}")\n'
        "          fi\n"
        '          if [ -n "${TF_STATE_REGION:-}" ]; then\n'
        '            init_args+=("-backend-config=region=${TF_STATE_REGION}")\n'
        "          fi\n"
        '          if [ "${#init_args[@]}" -gt 0 ]; then\n'
        '            terraform -chdir="$target_dir" init "${init_args[@]}"\n'
        "          else\n"
        '            terraform -chdir="$target_dir" init\n'
        "          fi\n"
    ) in text
    assert text.count('terraform -chdir="$target_dir" init') == 2
    assert text.count("-backend-config") == 2

    # What they used before, untouched: the role, the region (now the fallback of the AWS_REGION
    # variable: see the region tests below), and no account ID or role ARN written into the
    # workflow itself.
    role = "AWS_PROD_DEPLOY_ROLE_ARN" if "production" in workflow else "AWS_DEV_DEPLOY_ROLE_ARN"
    assert f"          role-to-assume: ${{{{ secrets.{role} || vars.{role} }}}}\n" in text
    assert "          aws-region: ${{ vars.AWS_REGION || 'ap-southeast-2' }}\n" in text
    assert "arn:aws:iam::" not in text


def test_dev_still_deploys_from_the_branch_and_production_from_its_environment():
    """The deploy roles' trust is on these two claims; an `environment:` on dev would break its one."""
    workflows = ROOT / ".github" / "workflows"
    for name in ("terraform.yml", "destroy-dev.yml"):
        assert not re.search(r"^    environment:", (workflows / name).read_text(encoding="utf-8"), re.M), name
    release = (workflows / "terraform-production-release.yml").read_text(encoding="utf-8")
    assert "\n    environment: production\n" in release


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_backends_and_state_keys_have_not_moved(env):
    main = _read("environments", env, "main.tf")
    backend = re.search(r'  backend "s3" \{\n(.*?)\n  \}', main, re.S).group(1)
    assert [line.strip() for line in backend.splitlines()] == [
        'bucket       = "bloggerbear-terraform-state"',
        f'key          = "{env}/terraform.tfstate"',
        'region       = "ap-southeast-2"',
        "encrypt      = true",
        "use_lockfile = true",
    ]


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_globally_unique_names_are_the_old_ones_unless_a_prefix_is_set(env):
    """A changed bucket name replaces the bucket, so with the prefix left at its default the three
    names that are unique across all of AWS must be exactly what they were. The files are read as
    written here (not through _read), so this holds the expressions and the default separately."""
    main = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")
    variables = (INFRA / "environments" / env / "variables.tf").read_text(encoding="utf-8")
    prefix = variables.split('variable "unique_name_prefix" {')[1].split("\n}\n")[0]
    assert '\n  default     = "bloggerbear"\n' in prefix

    content = _resource_block(main, "aws_s3_bucket", "content")
    assert re.search(rf'bucket\s*=\s*"\$\{{var\.unique_name_prefix\}}-{env}-content"', content)
    site_call = _module_blocks(main, "modules/static-site")[0]
    assert re.search(r"^\s*unique_name_prefix\s*=\s*var\.unique_name_prefix$", site_call, re.M)
    module = (INFRA / "modules" / "static-site" / "main.tf").read_text(encoding="utf-8")
    assert 'bucket        = "${var.unique_name_prefix}-${var.environment_name}-site"' in module
    assert f'hosted_ui_domain_prefix = "${{var.unique_name_prefix}}-{env}-ops"' in main

    # Written out with the default: the names the original deployment's buckets and sign-in host have.
    assert f'"bloggerbear-{env}-content"' in with_default_prefix(content)
    assert '"bloggerbear-${var.environment_name}-site"' in with_default_prefix(module)
    assert f'hosted_ui_domain_prefix = "bloggerbear-{env}-ops"' in with_default_prefix(main)

    # The setting it replaces is gone everywhere: a name is never built from both.
    for path in terraform_files():
        text = path.read_text(encoding="utf-8")
        assert "unique_name_suffix" not in text and "bucket_name_suffix" not in text, path


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_default_model_is_the_same_profile_in_whichever_account_is_applied_to(env):
    """The default was this ARN with one account's ID in it. Built from the caller's account it is
    the same string for that account, so its Lambdas' environment does not change. The region and
    the profile id are variables too, whose defaults are the two strings that were written here."""
    main = _read("environments", env, "main.tf")
    variables = _read("environments", env, "variables.tf")
    variable = variables.split('variable "bedrock_model_id" {')[1].split("\n}\n")[0]
    assert '\n  default     = ""\n' in variable

    assert (
        '  bedrock_model_id = var.bedrock_model_id != "" ? var.bedrock_model_id : '
        '"arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}'
        ':inference-profile/${var.bedrock_inference_profile_id}"\n'
    ) in main
    profile = variables.split('variable "bedrock_inference_profile_id" {')[1].split("\n}\n")[0]
    assert '\n  default = "au.anthropic.claude-haiku-4-5-20251001-v1:0"\n' in profile
    # Which geographies exist is AWS's list, so the id is not checked against one.
    assert "validation {" not in profile
    # Nothing reads the variable directly any more, or an empty one would reach a Lambda.
    assert _uncommented(main).count("var.bedrock_model_id") == 2
    assert re.search(r"BEDROCK_MODEL_ID\s*=\s*local\.bedrock_model_id", main)
    # A tfvars value beats the default, and an empty one here once broke every invocation.
    tfvars = _uncommented(_read("environments", env, "terraform.tfvars"))
    assert "bedrock_model_id" not in tfvars and "aws_account_id" not in tfvars


def test_the_deploy_roles_trust_whichever_repository_bootstrap_is_told():
    """A fork passes its own owner/repo; the default is this repository."""
    variables = _read("bootstrap", "variables.tf")
    repo = variables.split('variable "github_repo" {')[1].split("\n}\n")[0]
    assert '\n  default     = "AllainWoodsford/BloggerBear"\n' in repo

    main = _read("bootstrap", "main.tf")
    owner_repo = 'repo:${split("/", var.github_repo)[0]}@*/${split("/", var.github_repo)[1]}@*'
    assert f'values   = ["{owner_repo}:ref:refs/heads/dev"]' in main
    assert f'values   = ["{owner_repo}:environment:production"]' in main
    # Those two are the only subjects trusted, and neither names an owner or a repository itself.
    assert len(re.findall(r'values\s*=\s*\["repo:', main)) == 2
    # And the state bucket's name is a variable too, with the name the backends are written for.
    bucket = variables.split('variable "state_bucket_name" {')[1].split("\n}\n")[0]
    assert '\n  default     = "bloggerbear-terraform-state"\n' in bucket


def test_dev_refuses_a_shared_web_acl_from_another_account():
    """CloudFront can only use a web ACL in its own account, so a two-account deployment leaves
    web_acl_arn empty. Checked at plan time, and only when both values are set."""
    variables = _read("environments", "dev", "variables.tf")
    acl = variables.split('variable "web_acl_arn" {')[1].split("\n}\n")[0]
    assert '\n  default     = ""\n' in acl
    assert (
        'condition     = var.web_acl_arn == "" || var.aws_account_id == "" || '
        'try(split(":", var.web_acl_arn)[4], "") == var.aws_account_id'
    ) in acl
    # Today's value: dev does not use the shared ACL at all, so the rule has nothing to refuse.
    assert re.search(r'^web_acl_arn = ""$', _read("environments", "dev", "terraform.tfvars"), re.M)


def test_the_fork_guide_names_every_setting_the_workflows_read():
    guide = (ROOT / "docs" / "deployment-runsheet.md").read_text(encoding="utf-8")
    # The settings table is its own page, which the guide and the README both link to.
    table = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "(docs/deployment-runsheet.md)" in readme and "(docs/configuration.md)" in readme
    assert "(configuration.md" in guide
    settings = set()
    for name in ("terraform.yml", "destroy-dev.yml", "terraform-production-release.yml"):
        text = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
        settings |= set(re.findall(r"\$\{\{[^}]*?\b(?:secrets|vars)\.([A-Z0-9_]+)", text))
        settings |= set(re.findall(r"\|\| vars\.([A-Z0-9_]+)", text))
    new = {"AWS_DEV_ACCOUNT_ID", "AWS_PROD_ACCOUNT_ID", "TF_STATE_BUCKET_DEV", "UNIQUE_NAME_PREFIX"}
    assert new <= settings
    # The setting the prefix replaced is read by nothing, so it is documented nowhere.
    assert "UNIQUE_NAME_SUFFIX" not in settings
    for page in (guide, table, readme):
        assert "UNIQUE_NAME_SUFFIX" not in page and "unique_name_suffix" not in page
    # The prefix is a plain variable, a fork must set it, and the guide says the two things that
    # go wrong otherwise: it cannot change later, and bootstrap has to be given the same word.
    row = re.search(r"^\| `UNIQUE_NAME_PREFIX` \| variable \| repo \|(.*)$", table, re.M)
    assert row and "**Required for a fork.**" in row.group(1)
    assert '-var="unique_name_prefix=' in guide and "never change it" in guide
    for setting in sorted(settings):
        assert f"`{setting}`" in table, setting
    # The account IDs and the state buckets are documented as secrets, the only way they are read.
    sensitive = ("AWS_DEV_ACCOUNT_ID", "AWS_PROD_ACCOUNT_ID", "TF_STATE_BUCKET_DEV", "TF_STATE_BUCKET_PROD")
    for setting in sensitive:
        assert re.search(rf"^\| `{setting}` \| secret \|", table, re.M), setting
    # And the setup script is the first thing the guide offers, dry run first.
    quick = guide.split("## Quick start: the setup script")[1].split("\n## ")[0]
    assert quick.index("setup_repo.py --dry-run") < quick.index("setup_repo.py   ")
    assert guide.index("## Quick start: the setup script") < guide.index("## How a deploy picks its account")


def test_security_scans_cover_the_whole_repo_with_pinned_tools():
    """security.yml used to scan only lambdas/ (missing scripts/ and the dev requirements), report
    nothing below HIGH, and install whatever Trivy apt had; Trufflehog ran `version: latest`."""
    workflows = ROOT / ".github" / "workflows"
    security = (workflows / "security.yml").read_text(encoding="utf-8")

    assert "apt-get install -y trivy" not in security
    assert re.search(r"VERSION=\d+\.\d+\.\d+\n\s+SHA256=[0-9a-f]{64}\n", security)
    assert 'sha256sum -c -' in security
    fs_runs = re.findall(r"trivy fs (.*?)\n\n", security, re.S)  # each command, up to its blank line
    assert len(fs_runs) == 2
    # Trivy reads requirements.txt by name and anything else only by this pattern, so every other
    # requirements file in the repo must match it: a new one that doesn't would go unscanned.
    others = sorted(
        path.name
        for path in workflows.parents[1].glob("*/requirements-*.txt")
        if "node_modules" not in path.parts
    )
    assert "requirements-dev.txt" in others and "requirements-ops-mcp.txt" in others
    for run in fs_runs:
        pattern = re.search(r"--file-patterns 'pip:(.+?)'", run)
        assert pattern and run.rstrip().endswith(".")
        for name in others:
            assert re.fullmatch(pattern.group(1), name), f"{name} is not scanned"
    assert "--severity MEDIUM,HIGH,CRITICAL --exit-code 0" in fs_runs[0]  # reported
    assert "--severity HIGH,CRITICAL --exit-code 1" in fs_runs[1]  # gated
    assert "bandit -r lambdas/ scripts/ -x lambdas/tests,scripts/tests" in security

    pr_checks = (workflows / "pr-checks.yml").read_text(encoding="utf-8")
    trufflehog = pr_checks.split("uses: trufflesecurity/trufflehog@")[1].split("\n  gitleaks:\n")[0]
    assert re.search(r"\n          version: \d+\.\d+\.\d+\n", trufflehog)


def test_the_on_demand_scan_checks_everything_and_deploys_nothing():
    scan = (ROOT / ".github" / "workflows" / "on-demand-scan.yml").read_text(encoding="utf-8")

    # Started by hand or by a collaborator's label, never by an ordinary PR event.
    assert re.search(r"^on:\n  pull_request:\n    types: \[labeled\]\n  workflow_dispatch:\n", scan, re.M)
    jobs = scan.split("\njobs:\n")[1]
    assert jobs.count("github.event.label.name == 'security-scan'") == 4  # every job, summary too

    # No AWS access, no plan, no apply.
    for text in ("configure-aws-credentials", "id-token"):
        assert text not in scan
    assert not re.search(r"^\s*terraform [^\n]*\b(plan|apply)\b", scan, re.M)
    assert "permissions:\n  contents: read\n" in scan

    # Whole history and every file, with the same pinned tools, hash-verified.
    assert 'gitleaks git --redact' in scan and '--log-opts="--all"' in scan
    assert "trufflehog git file://. --results=verified,unverified,unknown --fail" in scan
    assert "fetch-depth: 0" in scan
    assert "scripts/pii_denylist_check.py --all" in scan and "secrets.PII_DENYLIST" in scan
    assert len(re.findall(r"SHA256=[0-9a-f]{64}\n", scan)) == 3  # trivy, gitleaks, trufflehog
    assert "terraform -chdir=\"$dir\" validate" in scan and "infra/bootstrap" in scan
    # The ref reaches the summary through env, not interpolated into the script.
    assert "SCANNED: ${{ inputs.ref" in scan and "echo \"## On-demand scan: \\`${SCANNED}\\`\"" in scan

    assert (ROOT / ".gitleaksignore").is_file()


def test_pull_requests_are_checked_for_personal_data_without_publishing_it():
    """docs/friction.md 7.11: a PR added a personal email; no secret scanner looks for one."""
    pr_checks = (ROOT / ".github" / "workflows" / "pr-checks.yml").read_text(encoding="utf-8")
    gitleaks = pr_checks.split("\n  gitleaks:\n")[1].split("\n  pii-denylist:\n")[0]
    denylist = pr_checks.split("\n  pii-denylist:\n")[1]

    # Gitleaks pinned by commit, binary pinned, our config, and nothing it found ever re-published
    # (no PR comments, no artifact; the action itself runs with --redact).
    assert re.search(r"uses: gitleaks/gitleaks-action@[0-9a-f]{40}\n", gitleaks)
    assert re.search(r"GITLEAKS_VERSION: \d+\.\d+\.\d+\n", gitleaks)
    assert "GITLEAKS_CONFIG: .gitleaks.toml" in gitleaks
    assert "GITLEAKS_ENABLE_COMMENTS: false" in gitleaks
    assert "GITLEAKS_ENABLE_UPLOAD_ARTIFACT: false" in gitleaks
    assert "pull-requests: read" in gitleaks and "write" not in gitleaks

    # The exact-string list comes from a secret, and only the PR's own range is checked.
    assert "PII_DENYLIST: ${{ secrets.PII_DENYLIST }}" in denylist
    assert 'scripts/pii_denylist_check.py --range "$BASE" "$HEAD"' in denylist

    config = (ROOT / ".gitleaks.toml").read_text(encoding="utf-8")
    assert "useDefault = true" in config
    for rule in ("email-address", "aws-account-id-in-arn", "aws-account-id-labelled"):
        assert f'id = "{rule}"' in config

    # The same checks run before a commit, and the local list can never be committed.
    hook = (ROOT / ".githooks" / "pre-commit").read_text(encoding="utf-8")
    assert "scripts/pii_denylist_check.py --staged" in hook and "--config .gitleaks.toml" in hook
    assert b"\r" not in (ROOT / ".githooks" / "pre-commit").read_bytes()  # sh can't run CRLF
    assert re.search(r"^\.pii-denylist$", (ROOT / ".gitignore").read_text(encoding="utf-8"), re.M)


# --- the CoinGecko key: SSM Parameter Store, never Terraform state or a Lambda's environment ------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_coingecko_key_is_read_from_ssm_not_passed_in(env):
    text = _read("environments", env, "main.tf")
    variables = _read("environments", env, "variables.tf")
    parameter = f"/bloggerbear/{env}/coingecko-api-key"

    # Terraform names the parameter and grants reading it -- nothing more.
    assert f'coingecko_api_key_parameter = "{parameter}"' in text
    assert "COINGECKO_API_KEY_PARAMETER = local.coingecko_api_key_parameter" in text
    assert 'resource "aws_ssm_parameter"' not in text  # its value would be read back into state
    assert 'variable "coingecko_api_key"' not in variables and "COINGECKO_API_KEY " not in text

    policy = re.search(
        r'data "aws_iam_policy_document" "lambda_coingecko_key" \{(.*?)\n\}', text, re.S
    ).group(1)
    assert re.findall(r'"(ssm:[A-Za-z]+)"', policy) == ["ssm:GetParameter"]
    assert "parameter${local.coingecko_api_key_parameter}" in policy

    # Only the two crypto Lambdas are told where the key is.
    told = re.findall(
        r'^resource "aws_lambda_function" "([a-z_]+)" \{(?:(?!^\}).)*local\.coingecko_env_variables',
        text,
        re.S | re.M,
    )
    assert sorted(told) == ["daily_cycle", "research_tick"]


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_github_token_is_read_from_ssm_not_passed_in(env):
    text = _read("environments", env, "main.tf")
    parameter = f"/bloggerbear/{env}/github-api-token"

    assert f'github_api_token_parameter = "{parameter}"' in text
    assert "GITHUB_API_TOKEN_PARAMETER = local.github_api_token_parameter" in text
    assert 'resource "aws_ssm_parameter"' not in text
    assert "GITHUB_API_TOKEN " not in text

    policy = re.search(
        r'data "aws_iam_policy_document" "lambda_github_token" \{(.*?)\n\}', text, re.S
    ).group(1)
    assert re.findall(r'"(ssm:[A-Za-z]+)"', policy) == ["ssm:GetParameter"]
    assert "parameter${local.github_api_token_parameter}" in policy

    # The two Lambdas that run adapters (fetch, and the fresh-data review's re-fetch).
    told = re.findall(
        r'^resource "aws_lambda_function" "([a-z_]+)" \{(?:(?!^\}).)*local\.github_env_variables',
        text,
        re.S | re.M,
    )
    assert sorted(told) == ["daily_cycle", "research_tick"]


def test_no_workflow_passes_a_coingecko_key_any_more():
    workflows = ROOT / ".github" / "workflows"
    for path in workflows.glob("*.yml"):
        assert "COINGECKO_API_KEY" not in path.read_text(encoding="utf-8"), path.name


# --- DynamoDB indexes (Scaling PR A) ---------------------------------------------------------------


def _table_indexes(table: str) -> list[tuple[str, str, str, str]]:
    """(name, hash key, range key, projection) of every global_secondary_index on a table."""
    block = _resource_block(_read("modules", "app-data", "main.tf"), "aws_dynamodb_table", table)
    indexes = []
    for body in re.findall(r"global_secondary_index \{\n(.*?)\n  \}", block, re.S):
        fields = dict(re.findall(r'^\s*(\w+)\s*=\s*"([^"]+)"', body, re.M))
        indexes.append((fields["name"], fields["hash_key"], fields["range_key"], fields["projection_type"]))
    return indexes


@pytest.mark.parametrize(
    ("table", "fixture_name"),
    [
        ("articles", "Articles"),
        ("moderation_queue", "ModerationQueue"),
        ("security_events", "SecurityEvents"),
    ],
)
def test_the_test_fixtures_create_exactly_the_indexes_terraform_does(table, fixture_name):
    """moto only knows the indexes a fixture creates, so a fixture that drifted from Terraform would
    let a Query on a missing (or differently keyed) index pass here and fail in AWS."""
    from table_schemas import INDEXES

    assert sorted(_table_indexes(table)) == sorted(INDEXES[fixture_name])


@pytest.mark.parametrize("table", ["articles", "moderation_queue", "security_events"])
def test_every_index_key_is_declared_as_a_string_attribute(table):
    block = _resource_block(_read("modules", "app-data", "main.tf"), "aws_dynamodb_table", table)
    declared = dict(re.findall(r'attribute \{\n\s*name = "(\w+)"\n\s*type = "(\w)"', block))

    for _, hash_key, range_key, _ in _table_indexes(table):
        assert declared.get(hash_key) == "S" and declared.get(range_key) == "S"


def test_the_index_names_the_code_queries_are_the_ones_terraform_creates():
    from common import dynamo

    articles = {name for name, *_ in _table_indexes("articles")}
    moderation = {name for name, *_ in _table_indexes("moderation_queue")}

    assert {dynamo.ARTICLES_BY_STATUS_INDEX, dynamo.ARTICLES_BY_TOPIC_INDEX} == articles
    assert {dynamo.MODERATION_BY_STATUS_INDEX, dynamo.MODERATION_BY_ARTICLE_INDEX} == moderation


def test_no_index_sorts_on_published_at_which_is_stored_as_null_until_publish():
    """DynamoDB rejects a write whose index key attribute holds a null, and put_article stores
    published_at as an explicit null for every draft -- an index on it would fail every draft."""
    for table in ("articles", "moderation_queue"):
        for _, hash_key, range_key, _ in _table_indexes(table):
            assert "published_at" not in (hash_key, range_key)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_lambdas_may_query_the_table_indexes_and_nothing_more(env):
    policy = _read("environments", env, "main.tf")
    statement = re.search(r'sid\s*=\s*"DynamoDBAppIndexes"(.*?)\n  \}', policy, re.S).group(1)

    assert re.search(r'actions\s*=\s*\["dynamodb:Query"\]', statement)
    assert 'resources = [for arn in module.app_data.table_arns : "${arn}/index/*"]' in statement


def test_dashboard_widget_lists_never_branch_on_a_conditional():
    """`cond ? [] : concat(...)` over widgets of different shapes passes validate and fails every
    plan ("Inconsistent conditional result types"), which broke the dev apply once. The idiom that
    works is `flatten([for _ in (cond ? [] : [1]) : ...])`; the module's terraform test plans it."""
    text = _dashboards()

    assert not re.search(r"= .*\? \[\] : concat\(", text)
    assert (ROOT / "infra" / "modules" / "observability" / "tests" / "observability.tftest.hcl").exists()
    assert "terraform -chdir=\"$dir\" test" in (ROOT / ".github" / "workflows" / "pr-checks.yml").read_text(
        encoding="utf-8"
    )


# --- Security events (common/security_events.py) ------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_regional_waf_log_groups_feed_the_security_events_lambda_blocks_only(env):
    text = _read("environments", env, "main.tf")

    assert 'handler       = "security_events_handler.handler"' in text
    assert "public_api = aws_cloudwatch_log_group.waf_public_api" in text
    assert "admin      = aws_cloudwatch_log_group.waf_admin" in text
    assert 'filter_pattern  = "{ $.action = \\"BLOCK\\" }"' in text
    assert 'principal     = "logs.amazonaws.com"' in text
    assert 'source_arn    = "${each.value.arn}:*"' in text
    assert "SECURITY_EVENTS_TABLE = module.app_data.security_events_table_name" in text
    # The CloudFront ACL's log group is in us-east-1: a subscription can't reach this region's Lambda.
    # (Only the security-events section is looked at: the operator's assistant, further down, does
    # read the shared group, through Logs Insights, which can.)
    section = text.split('resource "aws_lambda_function" "security_events"')[1]
    section = section.split('module "ops_assistant"')[0]
    assert "waf_shared" not in section


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_high_severity_alarm_watches_both_lambdas_that_record_events(env):
    block = re.search(
        r'^module "observability" \{\n(.*?)^\}', _read("environments", env, "main.tf"), re.S | re.M
    ).group(1)

    assert "aws_lambda_function.security_events.function_name].name" in block
    assert "security_alert_log_groups = [" in block


def test_the_alarm_counts_the_marker_the_code_logs():
    from common import security_events

    module = _read("modules", "observability", "main.tf")
    assert f'pattern        = "\\"{security_events.ALERT_MARKER}\\""' in module
    assert 'metric_name         = "SecurityHighSeverityIncidents"' in module


def test_security_events_expire_and_have_an_open_incidents_index():
    from common import dynamo, security_events

    block = _resource_block(_read("modules", "app-data", "main.tf"), "aws_dynamodb_table", "security_events")
    assert 'attribute_name = "expires_at"' in block
    names = [name for name, *_ in _table_indexes("security_events")]
    assert names == [dynamo.SECURITY_EVENTS_BY_STATUS_INDEX]
    assert security_events.RETENTION_DAYS == 120


# --- The operator's assistant (infra/modules/ops-assistant) -------------------------------------
# "Read-only, behind a sign-in" is a claim about the deployment, not the code: these hold the parts
# of it that a plan would happily change. The module's own terraform test
# (tests/ops_assistant.tftest.hcl) checks the same role and authorizer with planned values.

# Everything the MCP server's role may do. A new tool that needs another read action adds it here,
# and in the module's test, on purpose.
_OPS_MCP_ALLOWED_ACTIONS = {
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:BatchGetItem",
    # table_sample (ops_mcp/samples.py): the table's tags, which the code checks before reading.
    "dynamodb:ListTagsOfResource",
    "s3:GetObject",
    "cloudwatch:DescribeAlarms",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


def _ops_module() -> str:
    return _read("modules", "ops-assistant", "main.tf")


def _ops_policy() -> str:
    return re.search(
        r'^data "aws_iam_policy_document" "ops_mcp" \{\n(.*?)^\}', _ops_module(), re.S | re.M
    ).group(1)


def _uncommented(text: str) -> str:
    """The Terraform without its comment lines, which say in words what the code must not do."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_the_mcp_package_ships_the_cli_reference_next_to_the_code_that_reads_it():
    """ops_mcp/cli_guide.py reads cli_reference.json from its own directory. The package build
    copies the whole ops_mcp directory (not only its .py files) and zips the whole build
    directory, so the file is in the Lambda; a build changed to copy by pattern, or to leave
    files out of the zip, would break the guide at the first question and fails here."""
    module = _uncommented(_ops_module())
    package = ROOT / "lambdas" / "ops_mcp"

    assert (package / "cli_reference.json").is_file()
    code = (package / "cli_guide.py").read_text(encoding="utf-8")
    assert 'REFERENCE_FILE = Path(__file__).with_name("cli_reference.json")' in code

    build = _resource_block(module, "terraform_data", "package")
    assert 'cp -r "${local.lambdas_dir}/ops_mcp" "$build_dir/ops_mcp"' in build
    # Nothing in the build removes or filters what was copied, apart from the bytecode caches.
    assert re.findall(r"\brm\b[^\n]*", build) == ['rm -rf "$build_dir"', "rm -rf {} +"]
    assert "__pycache__" in build and "*.json" not in build and "--include" not in build

    archive = re.search(r'^data "archive_file" "package" \{\n(.*?)^\}', module, re.S | re.M).group(1)
    assert re.search(r"source_dir\s*=\s*local\.build_dir", archive)
    assert "excludes" not in archive
    # The agent's package holds only access.py of ops_mcp, and needs no more: it asks the server,
    # and never imports the guide or reads the reference itself.
    agent_files = [*(ROOT / "lambdas" / "ops_agent").glob("*.py"), ROOT / "lambdas" / "ops_agent_handler.py"]
    for path in agent_files:
        imports = re.findall(r"^(?:from|import) .*$", path.read_text(encoding="utf-8"), re.M)
        assert not any("cli_guide" in line or "cli_reference" in line for line in imports), path.name


def test_dev_deploys_the_ops_assistant_once():
    blocks = _module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")

    assert len(blocks) == 1
    assert re.search(r"mfa_configuration\s*=\s*var\.ops_assistant_mfa", blocks[0])
    variable = re.search(
        r'variable "ops_assistant_mfa" \{(.*?)\n\}', _read("environments", "dev", "variables.tf"), re.S
    ).group(1)
    assert re.search(r'default\s*=\s*"OPTIONAL"', variable)


def test_the_mcp_server_does_not_run_as_the_shared_lambda_role():
    """The shared role may write and delete on every table. The assistant's function has a role
    of its own, made inside the module, and the module has no way to be handed another."""
    import fnmatch

    module = _ops_module()
    function = _resource_block(module, "aws_lambda_function", "ops_mcp")
    call = _module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")[0]

    assert re.search(r"^\s*role\s*=\s*aws_iam_role\.ops_mcp\.arn$", function, re.M)
    assert "lambda_exec" not in _uncommented(call)
    variables = re.findall(r'^variable "([^"]+)"', _read("modules", "ops-assistant", "variables.tf"), re.M)
    assert variables and not any("role" in variable for variable in variables)
    name = re.search(r'name\s*=\s*"([^"]+)"', _resource_block(module, "aws_iam_role", "ops_mcp")).group(1)
    prefix = re.search(r'^  name = "([^"]+)"$', module, re.M).group(1)
    dev_name = name.replace("${local.name}", prefix).replace("${var.environment_name}", "dev")
    # Its own name, not the shared role's, and one the deploy role may create and pass.
    shared = _resource_block(_read("environments", "dev", "main.tf"), "aws_iam_role", "lambda_exec")
    assert dev_name == "bloggerbear-dev-ops-mcp-lambda-exec"
    assert f'"{dev_name}"' not in shared and '"bloggerbear-dev-lambda-exec"' in shared
    patterns = re.findall(r'"arn:aws:iam::\*:role/([^"]+)"', _read("bootstrap", "main.tf"))
    assert any(fnmatch.fnmatch(dev_name, pattern) for pattern in patterns)


def test_the_mcp_servers_role_has_no_write_action_and_no_wildcard():
    policy = _uncommented(_ops_policy())
    actions = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', policy))

    assert actions == _OPS_MCP_ALLOWED_ACTIONS, actions ^ _OPS_MCP_ALLOWED_ACTIONS
    assert not re.search(r"dynamodb:(Put|Update|Delete|BatchWrite|TransactWrite)", policy)
    assert "logs:*" not in policy and "logs:CreateLogGroup" not in policy
    assert '"*"' not in policy  # no statement is on every resource
    assert "not_actions" not in policy and "not_resources" not in policy
    assert len(re.findall(r'effect\s*=\s*"Allow"', policy)) == policy.count("statement {")
    # Its own log group, the articles/ prefix, and the tables it is handed: nothing wider.
    assert 'resources = ["${aws_cloudwatch_log_group.lambda.arn}:*"]' in policy
    assert 'resources = ["${var.content_bucket_arn}/articles/*"]' in policy
    assert "[for table in values(var.tables) : table.arn]," in policy


def test_table_sample_reads_only_tables_carrying_the_default_tags_and_a_readable_environment():
    """The owner's rule for what the assistant may read rows of: a bloggerbear-* table with the
    project's default tags (ManagedBy, Project) and an Environment it may read. Three conditions
    on one statement, all StringEquals, so all must hold; reads and ListTagsOfResource only."""
    policy = _uncommented(_ops_policy())
    statement = re.search(r'sid\s*=\s*"SampleTaggedTables"(.*?)\n  \}\n', policy, re.S).group(1)

    assert set(re.findall(r'"(dynamodb:[A-Za-z]+)"', statement)) == {
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:ListTagsOfResource",
    }
    assert '"arn:aws:dynamodb:${local.aws_region}:*:table/bloggerbear-*",' in statement
    assert '"arn:aws:dynamodb:${local.aws_region}:*:table/bloggerbear-*/index/*",' in statement
    conditions = re.findall(
        r'condition \{\n\s*test\s*=\s*"([^"]+)"\n\s*variable\s*=\s*"([^"]+)"\n\s*values\s*=\s*([^\n]+)',
        statement,
    )
    assert sorted(conditions) == [
        ("StringEquals", "aws:ResourceTag/Environment", "local.readable_environments"),
        ("StringEquals", "aws:ResourceTag/ManagedBy", '[var.default_tags["ManagedBy"]]'),
        ("StringEquals", "aws:ResourceTag/Project", '[var.default_tags["Project"]]'),
    ]
    assert "ssm:" not in policy  # the tags come from Terraform, not a parameter
    module = _ops_module()
    function = _uncommented(_resource_block(module, "aws_lambda_function", "ops_mcp"))
    assert re.search(r"OPS_DEFAULT_TAGS\s*=\s*jsonencode\(var\.default_tags\)", function)
    assert re.search(r'OPS_READABLE_ENVIRONMENTS\s*=\s*join\(",", local\.readable_environments\)', function)


def test_dev_reads_only_dev_and_production_also_reads_shared():
    """One rule, written twice (the module's IAM and the code), held to the same words here: dev
    never reads production's or the shared resources; production reads its own and the shared
    ones."""
    module = _ops_module()
    assert (
        'readable_environments = concat([var.environment_name], var.environment_name == "production" ? '
        '["shared"] : [])' in module
    )
    samples = (ROOT / "lambdas" / "ops_mcp" / "samples.py").read_text(encoding="utf-8")
    assert 'rule = [env, "shared"] if env == "production" else [env]' in samples


def test_the_root_hands_the_assistant_its_own_default_tags():
    """IAM's tag conditions and the code's check both compare against var.default_tags, so it must
    be the root's provider default_tags, as every resource of the root really carries them."""
    for environment in ("dev", "production"):
        text = _read("environments", environment, "main.tf")
        tags = re.search(r"default_tags = \{\n(.*?)\n  \}", text, re.S).group(1)
        root_tags = dict(re.findall(r"(\w+)\s*=\s*(.+)", tags))
        assert root_tags["ManagedBy"] == '"Terraform"', environment
        # Project is the deployment's: "BloggerBear" with the default name prefix, which is what
        # the original deployment's tables carry, and the prefix itself in any other deployment.
        assert root_tags["Project"] == "local.project_tag", environment
        assert (
            '  project_tag = "bloggerbear" == "bloggerbear" ? "BloggerBear" : "bloggerbear"\n'
            in text.replace("var.unique_name_prefix", '"bloggerbear"')
        ), environment
    for environment in ("dev", "production"):
        call = _module_blocks(_read("environments", environment, "main.tf"), "modules/ops-assistant")[0]
        assert "ManagedBy = local.default_tags.ManagedBy" in call, environment
        assert "Project   = local.default_tags.Project" in call, environment


def test_the_assistant_is_told_about_exactly_the_tables_it_may_read():
    """One map gives the function its table names and its role its table ARNs. Every key must be
    a variable common/dynamo.py reads, and the tables the tools use today must be among them."""
    call = _module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")[0]
    tables = re.search(r"^  tables = \{\n(.*?)^  \}", call, re.S | re.M).group(1)
    passed = dict(
        re.findall(r"^\s*([A-Z_]+)\s*=\s*\{ name = module\.app_data\.([a-z_]+)_table_name,", tables, re.M)
    )
    dynamo = (ROOT / "lambdas" / "common" / "dynamo.py").read_text(encoding="utf-8")
    read_by_code = set(re.findall(r'os\.environ\["([A-Z_]+_TABLE)"\]', dynamo))

    assert set(passed) == {
        "TOPICS_TABLE",
        "ARTICLES_TABLE",
        "MODERATION_QUEUE_TABLE",
        "FAILED_EXECUTIONS_TABLE",
        "MODEL_CONFIG_TABLE",
        "MUSINGS_TABLE",
        "SECURITY_EVENTS_TABLE",
        "STATS_CURRENT_TABLE",
        "STATS_HISTORY_TABLE",
    }
    assert set(passed) <= read_by_code
    for env_name, table in passed.items():  # the name and the ARN on a line are the same table's
        assert env_name == f"{table.upper()}_TABLE"
        assert f"arn = module.app_data.{table}_table_arn }}" in tables
    module = _ops_module()
    assert "{ for env_name, table in var.tables : env_name => table.name }," in module
    # Article bodies are in the content bucket, and the function is told which one.
    assert re.search(r"CONTENT_BUCKET\s*=\s*var\.content_bucket_name", module)
    assert re.search(r"content_bucket_name\s*=\s*aws_s3_bucket\.content\.bucket", call)
    # common/static_pages.py imports markdown at module top: the package installs requirements.txt
    # (checked with the build, below), and that file is what names it.
    requirements = (ROOT / "lambdas" / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^markdown==", requirements, re.M)


def test_the_assistant_can_read_its_access_switch_and_can_never_change_it():
    """The assistant_access setting lives in the config table's pipeline row. The server reads it
    on every request, so it needs the table's name and GetItem on it; it must not be able to write
    it, or a stolen token could switch the allowlist off. Only the operator's CLI changes it."""
    call = _module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")[0]
    policy = _uncommented(_ops_policy())
    function = _resource_block(_ops_module(), "aws_lambda_function", "ops_mcp")

    assert re.search(
        r"MODEL_CONFIG_TABLE\s*=\s*\{ name = module\.app_data\.model_config_table_name, "
        r"arn = module\.app_data\.model_config_table_arn \}",
        call,
    )
    assert '"dynamodb:GetItem"' in policy
    # Two DynamoDB statements, the tables handed in and table_sample's tag-conditioned one, and
    # every action in them is a read (ListTagsOfResource reads tags): neither could grant a write
    # on this table or any other.
    dynamodb_actions = set(re.findall(r'"(dynamodb:[A-Za-z*]+)"', policy))
    assert dynamodb_actions == {
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchGetItem",
        "dynamodb:ListTagsOfResource",
    }
    assert policy.count("dynamodb:GetItem") == 1 and policy.count("var.tables") == 2
    # The operator's addresses come from the list the admin API's WAF allowlist uses.
    dev = _read("environments", "dev", "main.tf")
    assert re.search(r"addresses\s*=\s*var\.admin_allowed_cidrs", dev)
    assert re.search(r"^\s*allowed_cidrs\s*=\s*var\.admin_allowed_cidrs$", call, re.M)
    assert re.search(r'OPS_ASSISTANT_ALLOWED_CIDRS\s*=\s*join\(",", var\.allowed_cidrs\)', function)
    variable = re.search(
        r'variable "allowed_cidrs" \{(.*?)\n\}', _read("modules", "ops-assistant", "variables.tf"), re.S
    ).group(1)
    assert re.search(r"sensitive\s*=\s*true", variable)  # a plan must not print a home address


# The assistant's memory (infra/modules/ops-assistant/memory.tf, lambdas/ops_mcp/memory.py): the one
# table the role may write to, and the only write anywhere in the module.
_OPS_MEMORY_ACTIONS = {
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:PutItem",
    "dynamodb:UpdateItem",
    "dynamodb:DeleteItem",
}


def _ops_memory() -> str:
    return _read("modules", "ops-assistant", "memory.tf")


def test_the_assistants_only_write_is_on_its_own_suggestions_table():
    """Write actions on that table alone. The read-only policy in main.tf is unchanged (the test
    above holds it); the write is one statement in a policy of its own, whose only resource is
    the table made beside it."""
    memory = _uncommented(_ops_memory())
    policy = re.search(
        r'^data "aws_iam_policy_document" "ops_mcp_memory" \{\n(.*?)^\}', memory, re.S | re.M
    ).group(1)

    assert set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', policy)) == _OPS_MEMORY_ACTIONS
    assert policy.count("statement {") == 1 and len(re.findall(r'effect\s*=\s*"Allow"', policy)) == 1
    assert re.findall(r"resources\s*=\s*(.*)", policy) == ["[aws_dynamodb_table.operator_suggestions.arn]"]
    assert "*" not in policy and "not_actions" not in policy and "not_resources" not in policy
    attached = _resource_block(memory, "aws_iam_role_policy", "ops_mcp_memory")
    assert re.search(r"role\s*=\s*aws_iam_role\.ops_mcp\.id", attached)
    assert re.search(r"policy\s*=\s*data\.aws_iam_policy_document\.ops_mcp_memory\.json", attached)
    # No other file of the module grants the role anything: main.tf's read-only policy, this one,
    # and nothing a later file could slip in unnoticed (the agent's own role is not this one).
    for path in sorted((INFRA / "modules" / "ops-assistant").glob("*.tf")):
        text = _uncommented(path.read_text(encoding="utf-8"))
        pattern = r'^resource "aws_iam_role_policy(?:_attachment)?" "([^"]+)" \{\n(.*?)^\}'
        grants = re.findall(pattern, text, re.S | re.M)
        for name, body in grants:
            if "aws_iam_role.ops_mcp." in body:
                # The third is a Deny (isolation.tf): it grants nothing, and the test below holds
                # that a Deny is all it is.
                assert (path.name, name) in {
                    ("main.tf", "ops_mcp"),
                    ("memory.tf", "ops_mcp_memory"),
                    ("isolation.tf", "ops_mcp_other_environments_denied"),
                    # The latest briefing per user (the test below holds what it may do).
                    ("briefings.tf", "ops_mcp_briefings"),
                    # Production's firewall deep dive (the test below holds what it may do).
                    ("firewall.tf", "ops_mcp_firewall"),
                }
        if path.name not in ("memory.tf", "briefings.tf"):
            for write in ("PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems"):
                assert f"dynamodb:{write}" not in text or "aws_iam_role.ops_mcp." not in text, path.name


def test_the_briefings_rights_are_one_table_and_one_function_for_each_role():
    """The async briefing (briefings.tf; docs/enhancements/alexa-plus.md, section 4.3). The MCP
    server may read and mark one table and invoke one function; the agent may write that table
    and never read it back. Nothing else, and nothing on "*"."""
    text = _uncommented(_read("modules", "ops-assistant", "briefings.tf"))

    def document(name: str) -> str:
        pattern = rf'^data "aws_iam_policy_document" "{name}" \{{\n(.*?)^\}}'
        return re.search(pattern, text, re.S | re.M).group(1)

    server = document("ops_mcp_briefings")
    assert set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', server)) == {
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "lambda:InvokeFunction",
    }
    assert sorted(re.findall(r"resources\s*=\s*(.*)", server)) == [
        "[aws_dynamodb_table.briefings.arn]",
        "[aws_lambda_function.ops_agent.arn]",
    ]
    agent_side = document("ops_agent_briefings")
    agent_actions = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', agent_side))
    assert agent_actions == {"dynamodb:PutItem", "dynamodb:UpdateItem"}
    assert re.findall(r"resources\s*=\s*(.*)", agent_side) == ["[aws_dynamodb_table.briefings.arn]"]
    for policy in (server, agent_side):
        assert "*" not in policy and "not_actions" not in policy and "Deny" not in policy
    # A failed async run is not retried into another model run.
    assert re.search(r"maximum_retry_attempts\s*=\s*0", text)


def test_the_firewall_policy_is_production_only_and_queries_named_groups():
    """firewall_review (firewall.tf; docs/enhancements/alexa-plus.md, section 4.4). The policy
    exists only where account_wide_data is on and groups are given; StartQuery is on those groups
    by ARN; the two query-id actions are the only ones on "*"; dev's root passes neither."""
    text = _uncommented(_read("modules", "ops-assistant", "firewall.tf"))

    enabled = r"firewall_enabled\s*=\s*var\.account_wide_data && length\(var\.waf_log_groups\) > 0"
    assert re.search(enabled, text)
    assert len(re.findall(r"count\s*=\s*local\.firewall_enabled \? 1 : 0", text)) == 2
    actions = set(re.findall(r'"(logs:[A-Za-z]+)"', text))
    assert actions == {"logs:StartQuery", "logs:GetQueryResults", "logs:StopQuery"}
    on_star = r'actions\s*=\s*\["logs:GetQueryResults", "logs:StopQuery"\]\s*resources\s*=\s*\["\*"\]'
    star = re.search(on_star, text)
    assert star, "only the query-id actions are on *"
    assert text.count('["*"]') == 1
    dev = _uncommented(_read("environments", "dev", "main.tf"))
    module = re.search(r'^module "ops_assistant" \{\n(.*?)^\}', dev, re.S | re.M).group(1)
    assert not re.search(r"^\s*(waf_log_groups|account_wide_data)\s*=", module, re.M)


# One environment each (infra/modules/ops-assistant/isolation.tf; the design's section 6). Dev and
# production are one AWS account, so "the dev assistant reads only dev's things" is held by the
# roles' named resources, by a Deny on anything tagged for another environment, and, where IAM
# cannot tell the two apart (alarms, the account's bill), by the code. The module's
# tests/environment_isolation.tftest.hcl checks the same with planned values.


def _ops_isolation() -> str:
    return _read("modules", "ops-assistant", "isolation.tf")


def test_both_assistant_roles_are_denied_anything_tagged_for_another_environment():
    isolation = _uncommented(_ops_isolation())
    document = re.search(
        r'^data "aws_iam_policy_document" "other_environments_denied" \{\n(.*?)^\}', isolation, re.S | re.M
    ).group(1)

    # One statement: every action, every resource, and two conditions that are both required.
    assert document.count("statement {") == 1 and re.search(r'effect\s*=\s*"Deny"', document)
    assert re.search(r'actions\s*=\s*\["\*"\]', document) and re.search(r'resources\s*=\s*\["\*"\]', document)
    assert "not_actions" not in document and "not_resources" not in document
    conditions = re.findall(
        r'condition \{\n\s*test\s*=\s*"([^"]+)"\n\s*variable\s*=\s*"([^"]+)"\n'
        r"\s*values\s*=\s*\[?([^\]\n]+?)\]?\n",
        document,
    )
    # Null = false first in importance: without it StringNotEquals alone is true of every request
    # that carries no resource tag, and the statement would deny nearly everything.
    assert sorted(conditions) == [
        ("Null", "aws:ResourceTag/Environment", '"false"'),
        ("StringNotEquals", "aws:ResourceTag/Environment", "local.readable_environments"),
    ]

    # Attached to both roles, each in a policy of its own, and nowhere else.
    role_policy = r'^resource "aws_iam_role_policy" "([^"]+)" \{\n(.*?)^\}'
    attached = dict(re.findall(role_policy, isolation, re.S | re.M))
    assert set(attached) == {"ops_mcp_other_environments_denied", "ops_agent_other_environments_denied"}
    for name, role in (
        ("ops_mcp_other_environments_denied", "ops_mcp"),
        ("ops_agent_other_environments_denied", "ops_agent"),
    ):
        assert re.search(rf"role\s*=\s*aws_iam_role\.{role}\.id", attached[name])
        assert re.search(
            r"policy\s*=\s*data\.aws_iam_policy_document\.other_environments_denied\.json", attached[name]
        )
    # It is the module's only Deny, and the file holds no Allow: nothing is granted from here.
    assert '"Allow"' not in isolation
    for path in sorted((INFRA / "modules" / "ops-assistant").glob("*.tf")):
        if path.name != "isolation.tf":
            assert '"Deny"' not in _uncommented(path.read_text(encoding="utf-8")), path.name

    # The deploy role may put a policy on roles of these names (it already makes them).
    import fnmatch

    patterns = re.findall(r'"arn:aws:iam::\*:role/([^"]+)"', _read("bootstrap", "main.tf"))
    for role_name in ("bloggerbear-dev-ops-mcp-lambda-exec", "bloggerbear-dev-ops-agent-lambda-exec"):
        assert any(fnmatch.fnmatch(role_name, pattern) for pattern in patterns)


def test_the_deny_compares_against_the_tag_the_environment_really_puts_on_its_resources():
    """The Deny refuses a resource whose Environment tag is not var.environment_name. Every
    resource of dev's carries the provider's default tag, so if the root's tag and the name it
    hands the module were different words, the assistant would be refused its own tables."""
    dev = _read("environments", "dev", "main.tf")
    tags = re.search(r"default_tags = \{\n(.*?)\n  \}", dev, re.S).group(1)
    tag = re.search(r'Environment\s*=\s*"([^"]+)"', tags).group(1)
    call = _module_blocks(dev, "modules/ops-assistant")[0]
    name = re.search(r'^\s*environment_name\s*=\s*"([^"]+)"', call, re.M).group(1)

    assert tag == name == "dev"
    # Every provider block of the root applies those tags, so nothing the module makes (its own
    # suggestions table, the two log groups) can be left without one or given another.
    providers = re.findall(r'^provider "aws" \{\n(.*?)^\}', dev, re.S | re.M)
    assert providers and all("tags = local.default_tags" in provider for provider in providers)
    # Nothing in this project switches on S3's tag-based access for a bucket: the module's comment
    # says the Deny does nothing for S3 today, and this is what that rests on.
    for path in INFRA.rglob("*.tf"):
        if ".terraform" not in path.parts:
            assert "aws_s3_bucket_abac" not in path.read_text(encoding="utf-8"), path


def test_the_alarms_tool_is_told_its_environment_and_every_alarm_is_named_for_one():
    """The role's DescribeAlarms covers every alarm in the account, both environments', so the
    tool asks by name. That only separates them if every alarm really is named
    <prefix>-<environment>-... (bloggerbear-dev-... in the original deployment), and the function
    is told the same prefix and the same environment name the alarms were made with."""
    function = _uncommented(_resource_block(_ops_module(), "aws_lambda_function", "ops_mcp"))
    code = (ROOT / "lambdas" / "ops_mcp" / "account.py").read_text(encoding="utf-8")

    assert re.search(r"^\s*ENVIRONMENT_NAME\s*=\s*var\.environment_name$", function, re.M)
    assert re.search(r"^\s*NAME_PREFIX\s*=\s*var\.unique_name_prefix$", function, re.M)
    # The prefix is the deployment's, from the variable above (common/naming.py), never written out.
    assert 'ENVIRONMENT_ENV = "ENVIRONMENT_NAME"' in code and 'ALARM_PREFIX = f"{NAME_PREFIX}-"' in code
    assert "from common.naming import NAME_PREFIX" in code and 'ALARM_PREFIX = "bloggerbear-"' not in code
    assert 'return f"{ALARM_PREFIX}{name}-" if _ENVIRONMENT_NAME.fullmatch(name) else None' in code
    assert "AlarmNamePrefix=prefix" in code and "AlarmNamePrefix=ALARM_PREFIX" not in code

    # Every alarm Terraform makes for an environment, wherever it is made.
    names = []
    for path in INFRA.rglob("*.tf"):
        if ".terraform" in path.parts or "bootstrap" in path.parts:
            continue
        text = _uncommented(path.read_text(encoding="utf-8"))
        names += re.findall(r'^\s*alarm_name\s*=\s*"([^"]+)"', text, re.M)
    assert len(names) >= 6
    assert all(name.startswith(PREFIX_REFERENCE + "-${var.environment_name}-") for name in names), names
    # ...and none is made outside an environment (a shared alarm would answer to neither prefix).
    assert "aws_cloudwatch_metric_alarm" not in _uncommented(_read("bootstrap", "main.tf"))

    # Dev's alarms and dev's assistant are given the same name.
    dev = _read("environments", "dev", "main.tf")
    for source in ("modules/observability", "modules/ops-assistant"):
        (block,) = _module_blocks(dev, source)
        assert re.search(r'^\s*environment_name\s*=\s*"dev"$', block, re.M), source

    # The name is held to one pattern in both places: what the module accepts is what the code
    # accepts, so a name the plan lets through never leaves the tool refusing to answer.
    variable = re.search(
        r'variable "environment_name" \{(.*?)\n\}', _read("modules", "ops-assistant", "variables.tf"), re.S
    ).group(1)
    in_terraform = re.search(r'can\(regex\("\^([^"]+)\$", var\.environment_name\)\)', variable).group(1)
    in_code = re.search(r'_ENVIRONMENT_NAME = re\.compile\(r"([^"]+)"\)', code).group(1)
    assert in_terraform == in_code == "[a-z][a-z0-9]{1,31}"


def test_account_wide_data_is_off_unless_the_caller_switches_it_on():
    """The AWS bill is the whole account's, production and dev together. The spend tool reports it
    only where the module's account_wide_data says so, and dev does not say so."""
    variable = re.search(
        r'variable "account_wide_data" \{(.*?)\n\}', _read("modules", "ops-assistant", "variables.tf"), re.S
    ).group(1)
    function = _uncommented(_resource_block(_ops_module(), "aws_lambda_function", "ops_mcp"))
    code = (ROOT / "lambdas" / "ops_mcp" / "account.py").read_text(encoding="utf-8")

    assert re.search(r"type\s*=\s*bool", variable) and re.search(r"default\s*=\s*false", variable)
    assert re.search(
        r'^\s*OPS_ACCOUNT_WIDE_DATA\s*=\s*var\.account_wide_data \? "true" : "false"$', function, re.M
    )
    # The code takes the one word the module writes for "on", and nothing else.
    assert 'ACCOUNT_WIDE_ENV = "OPS_ACCOUNT_WIDE_DATA"' in code
    assert 'return os.environ.get(ACCOUNT_WIDE_ENV, "") == "true"' in code
    # Dev leaves it at the default. The agent is told neither variable: it has no tool of its own.
    call = _uncommented(_module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")[0])
    assert "account_wide_data" not in call
    agent = _uncommented(_resource_block(_agent_module(), "aws_lambda_function", "ops_agent"))
    assert "OPS_ACCOUNT_WIDE_DATA" not in agent and "ENVIRONMENT_NAME" not in agent


def test_the_suggestions_table_is_the_assistants_alone_and_matches_what_the_code_expects():
    """Made inside the module, not in app-data, so its ARN is never among those handed to the role
    the pipeline Lambdas share. Its keys, TTL attribute and environment variable are the ones
    ops_mcp/memory.py uses."""
    import fnmatch

    table = _resource_block(_ops_memory(), "aws_dynamodb_table", "operator_suggestions")
    name = re.search(r'name\s*=\s*"([^"]+)"', table).group(1)

    assert name == "bloggerbear-${var.environment_name}-operator-suggestions"
    assert re.search(r'billing_mode\s*=\s*"PAY_PER_REQUEST"', table)
    assert re.search(r'hash_key\s*=\s*"user_id"', table) and re.search(r'range_key\s*=\s*"item"', table)
    assert re.findall(r'name = "(\w+)"\n\s*type = "S"', table) == ["user_id", "item"]
    assert 'attribute_name = "expires_at"' in table and re.search(r"enabled\s*=\s*true", table)
    # Point-in-time recovery and deletion protection as the app tables have them.
    topics = _resource_block(_read("modules", "app-data", "main.tf"), "aws_dynamodb_table", "topics")
    for setting in (
        r"deletion_protection_enabled\s*=\s*var\.protect_data",
        r"point_in_time_recovery \{\n\s*enabled\s*=\s*var\.protect_data\n",
    ):
        assert re.search(setting, topics) and re.search(setting, table)

    # Not in app-data, not passed in by the caller, and not given to the shared role.
    assert "operator-suggestions" not in _read("modules", "app-data", "main.tf")
    dev = _read("environments", "dev", "main.tf")
    assert "operator_suggestions" not in _uncommented(dev)
    outputs = re.findall(r'^output "([^"]+)"', "".join(
        path.read_text(encoding="utf-8") for path in (INFRA / "modules" / "ops-assistant").glob("*.tf")
    ), re.M)
    assert not any("suggestions" in output and output.endswith("_arn") for output in outputs)

    # The function is told the table's name under the variable memory.py reads.
    function = _resource_block(_ops_module(), "aws_lambda_function", "ops_mcp")
    code = (ROOT / "lambdas" / "ops_mcp" / "memory.py").read_text(encoding="utf-8")
    assert re.search(
        r"OPERATOR_SUGGESTIONS_TABLE\s*=\s*aws_dynamodb_table\.operator_suggestions\.name", function
    )
    assert 'TABLE_ENV = "OPERATOR_SUGGESTIONS_TABLE"' in code
    assert "aws_iam_role_policy.ops_mcp_memory" in re.search(r"depends_on\s*=\s*\[(.*?)\]", function).group(1)
    assert '"user_id": user_id, "item":' in code and '"expires_at"' in code

    # The deploy role may create a table of this name.
    bootstrap = _read("bootstrap", "main.tf")
    patterns = re.findall(r'"arn:aws:dynamodb:\$\{var\.aws_region\}:\*:table/([^"]+)"', bootstrap)
    assert any(fnmatch.fnmatch("bloggerbear-dev-operator-suggestions", pattern) for pattern in patterns)


def test_a_cold_start_cannot_be_held_up_by_the_access_check():
    """The adapter's default readiness check is a GET through the app, which the access check
    would answer. A TCP check asks only whether uvicorn is listening."""
    function = _resource_block(_ops_module(), "aws_lambda_function", "ops_mcp")

    assert re.search(r'AWS_LWA_READINESS_CHECK_PROTOCOL\s*=\s*"tcp"', function)


def test_the_web_adapter_layer_is_the_home_regions_x86_one_at_a_pinned_version():
    """Layers are regional: an ARN from another region fails at apply, so the region is the one
    the module is given. The account id and version come from the adapter's README, whose URL is
    recorded beside them."""
    module = _ops_module()
    arns = re.findall(r'"(arn:aws:lambda:[^"]*:layer:[^"]*)"', module)

    assert len(arns) == 1
    assert re.fullmatch(
        r"arn:aws:lambda:\$\{local\.aws_region\}:\d{12}:layer:LambdaAdapterLayerX86:\d+", arns[0]
    )
    assert "https://github.com/awslabs/aws-lambda-web-adapter" in module
    function = _resource_block(module, "aws_lambda_function", "ops_mcp")
    assert re.search(r"layers\s*=\s*\[local\.web_adapter_layer_arn\]", function)
    assert re.search(r'architectures\s*=\s*\["x86_64"\]', function)
    # The deploy role may read that layer, and no other.
    statement = re.search(
        r'sid\s*=\s*"LambdaWebAdapterLayer"(.*?)\n  \}', _read("bootstrap", "main.tf"), re.S
    ).group(1)
    assert re.search(r'actions\s*=\s*\["lambda:GetLayerVersion"\]', statement)
    in_bootstrap = arns[0].rsplit(":", 1)[0].replace("${local.aws_region}", _HOME)
    assert f'"{in_bootstrap}:*"' in statement


def test_the_function_starts_the_web_app_the_way_the_adapter_expects():
    module = _ops_module()
    function = _resource_block(module, "aws_lambda_function", "ops_mcp")
    run_sh = (INFRA / "modules" / "ops-assistant" / "run.sh").read_bytes()
    server = (ROOT / "lambdas" / "ops_mcp" / "server.py").read_text(encoding="utf-8")

    assert re.search(r'handler\s*=\s*"run\.sh"', function)
    assert re.search(r'runtime\s*=\s*"python3\.11"', function)
    assert re.search(r'AWS_LAMBDA_EXEC_WRAPPER\s*=\s*"/opt/bootstrap"', function)
    assert re.search(r"AWS_LWA_PORT\s*=\s*local\.web_adapter_port", function)
    assert run_sh.startswith(b"#!/bin/bash\n")
    assert b"\r" not in run_sh  # a CRLF shebang is "bad interpreter" on Linux
    text = run_sh.decode("utf-8")
    assert "exec python -m uvicorn --factory ops_mcp.server:create_app" in text
    assert '--port "${AWS_LWA_PORT:-8080}"' in text
    assert re.search(r'web_adapter_port\s*=\s*"8080"', module)
    assert 'PYTHONPATH="$LAMBDA_TASK_ROOT:' in text
    assert "def create_app()" in server
    # The package is run.sh, the two source packages and both requirements files, for the runtime.
    build = re.search(r'^resource "terraform_data" "package" \{\n(.*?)^\}', module, re.S | re.M).group(1)
    for needed in (
        "/common",
        "/ops_mcp",
        'chmod 755 "$build_dir/run.sh"',
        "--platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all:",
        "/requirements.txt",
        "/requirements-ops-mcp.txt",
    ):
        assert needed in build, needed


def test_uvicorn_comes_with_the_mcp_package_or_is_pinned():
    """run.sh starts uvicorn, and nothing lists it but the `mcp` package's own requirements. If a
    later release drops it, requirements-ops-mcp.txt must name it."""
    from importlib import metadata

    pinned = (ROOT / "lambdas" / "requirements-ops-mcp.txt").read_text(encoding="utf-8")
    from_mcp = any(re.match(r"uvicorn\b", requirement) for requirement in metadata.requires("mcp") or [])

    assert from_mcp or re.search(r"^uvicorn==", pinned, re.M)


def test_the_server_is_told_its_own_host_or_it_refuses_every_request():
    """ops_mcp/server.py answers 421 to any Host not on OPS_MCP_ALLOWED_HOSTS, and an empty list
    refuses everything. Behind API Gateway the Host is the API's execute-api domain."""
    module = _ops_module()
    function = _resource_block(module, "aws_lambda_function", "ops_mcp")
    server = (ROOT / "lambdas" / "ops_mcp" / "server.py").read_text(encoding="utf-8")

    assert re.search(r"OPS_MCP_ALLOWED_HOSTS\s*=\s*local\.api_host", function)
    assert (
        'api_host = "${aws_api_gateway_rest_api.this.id}.execute-api.${local.aws_region}.amazonaws.com"'
        in module
    )
    assert re.search(r"aws_region\s*=\s*var\.aws_region", module)
    assert '_from_env("OPS_MCP_ALLOWED_HOSTS")' in server


def test_the_one_route_is_post_mcp_behind_the_cognito_authorizer_with_the_scope():
    module = _ops_module()
    method = _resource_block(module, "aws_api_gateway_method", "mcp")
    server = (ROOT / "lambdas" / "ops_mcp" / "server.py").read_text(encoding="utf-8")

    assert module.count('resource "aws_api_gateway_method"') == 1
    assert re.search(r'http_method\s*=\s*"POST"', method)
    assert re.search(r'authorization\s*=\s*"COGNITO_USER_POOLS"', method)
    assert re.search(r"authorizer_id\s*=\s*aws_api_gateway_authorizer\.cognito\.id", method)
    assert re.search(r"authorization_scopes\s*=\s*\[local\.read_scope\]", method)
    assert 'resource_server_identifier = "bloggerbear-ops"' in module
    assert 'read_scope                 = "${local.resource_server_identifier}/read"' in module
    assert re.search(r'path_part\s*=\s*"mcp"', _resource_block(module, "aws_api_gateway_resource", "mcp"))
    assert 'MCP_PATH = "/mcp"' in server
    authorizer = _resource_block(module, "aws_api_gateway_authorizer", "cognito")
    assert re.search(r"provider_arns\s*=\s*\[aws_cognito_user_pool\.this\.arn\]", authorizer)
    # A changed authorization or scope updates the method in place; the stage must be redeployed.
    deployment = _resource_block(module, "aws_api_gateway_deployment", "this")
    assert "aws_api_gateway_method.mcp.authorization_scopes" in deployment
    assert "aws_api_gateway_authorizer.cognito.id" in deployment


def test_nothing_about_the_assistants_api_streams():
    """One JSON object per request: a plain proxy integration, buffered end to end."""
    code = _uncommented(_ops_module())

    assert re.search(r'type\s*=\s*"AWS_PROXY"', code)
    assert "response_transfer_mode" not in code
    assert "AWS_LWA_INVOKE_MODE" not in code and "response_stream" not in code.lower()
    assert "invoke_mode" not in code and "aws_lambda_function_url" not in code


def test_the_assistants_api_is_throttled_and_logs_what_happened_never_who_asked():
    module = _ops_module()
    stage = _resource_block(module, "aws_api_gateway_stage", "this")

    assert re.search(r"throttling_rate_limit\s*=\s*var\.throttling_rate_limit", module)
    assert re.search(r"throttling_burst_limit\s*=\s*var\.throttling_burst_limit", module)
    call = _module_blocks(_read("environments", "dev", "main.tf"), "modules/ops-assistant")[0]
    assert re.search(r"throttling_rate_limit\s*=\s*\d+", call)
    assert '"\\"status\\":$context.status,"' in stage
    assert "$context.error.responseType" in stage
    assert "$context.identity" not in stage and "$context.authorizer" not in stage
    assert 'name              = "/aws/apigateway/${local.name}-access"' in module
    assert 'name              = "/aws/lambda/${local.name}"' in module
    function = _resource_block(module, "aws_lambda_function", "ops_mcp")
    assert "aws_cloudwatch_log_group.lambda" in re.search(r"depends_on\s*=\s*\[(.*?)\]", function).group(1)


def test_the_user_pool_has_no_self_sign_up_no_client_secret_and_no_terraform_made_user():
    module = _ops_module()
    pool = _resource_block(module, "aws_cognito_user_pool", "this")
    client = _resource_block(module, "aws_cognito_user_pool_client", "page")

    assert re.search(r"allow_admin_create_user_only\s*=\s*true", pool)
    assert re.search(r"mfa_configuration\s*=\s*var\.mfa_configuration", pool)
    assert re.search(r"generate_secret\s*=\s*false", client)
    assert re.search(r'allowed_oauth_flows\s*=\s*\["code"\]', client)  # never "implicit"
    assert re.search(r"callback_urls\s*=\s*var\.callback_urls", client)
    assert re.search(r"logout_urls\s*=\s*var\.logout_urls", client)
    assert "ALLOW_USER_PASSWORD_AUTH" not in client and "ALLOW_ADMIN_USER_PASSWORD_AUTH" not in client
    assert 'resource "aws_cognito_user_pool_domain" "this"' in module
    for path in INFRA.rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        assert 'resource "aws_cognito_user" ' not in path.read_text(encoding="utf-8"), path


def test_the_deploy_role_may_create_the_assistants_user_pool():
    bootstrap = _read("bootstrap", "main.tf")
    pools = re.search(r'sid\s*=\s*"CognitoUserPools"(.*?)\n  \}', bootstrap, re.S).group(1)
    unscoped = re.search(r'sid\s*=\s*"CognitoNotResourceScopable"(.*?)\n  \}', bootstrap, re.S).group(1)

    assert f'"arn:aws:cognito-idp:{_HOME}:*:userpool/*"' in pools
    assert '"*"' not in re.search(r"resources\s*=\s*\[(.*?)\]", pools, re.S).group(1).split(",")
    assert set(re.findall(r'"(cognito-idp:[A-Za-z]+)"', unscoped)) == {
        "cognito-idp:CreateUserPool",
        "cognito-idp:DescribeUserPoolDomain",
        "cognito-idp:ListUserPools",
    }


def test_dev_outputs_what_the_page_and_the_agent_need():
    outputs = _read("environments", "dev", "outputs.tf")

    for name, source in (
        ("ops_mcp_url", "mcp_url"),
        ("ops_user_pool_id", "user_pool_id"),
        ("ops_app_client_id", "app_client_id"),
        ("ops_hosted_ui_domain", "hosted_ui_domain"),
    ):
        assert re.search(rf'output "{name}" \{{\n\s*value\s*=\s*module\.ops_assistant\.{source}\n', outputs)


# --- The operator's assistant, the agent (infra/modules/ops-assistant/agent.tf) -----------------
# The agent can spend money on the model. These hold that it can do nothing else, that the only
# way to it is the authorizer the MCP server sits behind, and that what the code reads is what the
# deployment sets. The module's tests/ops_agent.tftest.hcl checks the same with planned values.

_OPS_AGENT_ALLOWED_ACTIONS = {
    "bedrock:InvokeModel",
    "dynamodb:GetItem",
    "logs:CreateLogStream",
    "logs:PutLogEvents",
}


def _agent_module() -> str:
    return _read("modules", "ops-assistant", "agent.tf")


def _agent_policy() -> str:
    return re.search(
        r'^data "aws_iam_policy_document" "ops_agent" \{\n(.*?)^\}', _agent_module(), re.S | re.M
    ).group(1)


def _agent_environment() -> dict[str, str]:
    function = _uncommented(_resource_block(_agent_module(), "aws_lambda_function", "ops_agent"))
    variables = re.search(r"environment \{\n\s*variables = \{\n(.*?)\n    \}", function, re.S).group(1)
    return dict(re.findall(r"^\s*([A-Z_]+)\s*=\s*(.+)$", variables, re.M))


def test_the_agent_runs_as_a_role_of_its_own():
    """Not the shared role (write and delete on every table) and not the MCP server's (which must
    never be able to call Bedrock): a third, which the deploy role may create and pass."""
    import fnmatch

    module = _agent_module()
    function = _resource_block(module, "aws_lambda_function", "ops_agent")
    role = _resource_block(module, "aws_iam_role", "ops_agent")

    assert re.search(r"^\s*role\s*=\s*aws_iam_role\.ops_agent\.arn$", function, re.M)
    attached = _resource_block(module, "aws_iam_role_policy", "ops_agent")
    assert re.search(r"role\s*=\s*aws_iam_role\.ops_agent\.id", attached)
    assert 'agent_name      = "bloggerbear-${var.environment_name}-ops-agent"' in module
    name = re.search(r'name\s*=\s*"([^"]+)"', role).group(1)
    dev_name = name.replace("${local.agent_name}", "bloggerbear-dev-ops-agent")
    assert dev_name == "bloggerbear-dev-ops-agent-lambda-exec"
    patterns = re.findall(r'"arn:aws:iam::\*:role/([^"]+)"', _read("bootstrap", "main.tf"))
    assert any(fnmatch.fnmatch(dev_name, pattern) for pattern in patterns)
    # The MCP server's policy is not attached to it, and its own is not attached to the server.
    assert "aws_iam_role.ops_mcp" not in _uncommented(module)
    assert "ops_agent" not in _uncommented(_ops_policy())


def test_the_agents_role_may_invoke_the_model_read_the_switch_and_log():
    policy = _uncommented(_agent_policy())
    actions = set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', policy))

    assert actions == _OPS_AGENT_ALLOWED_ACTIONS, actions ^ _OPS_AGENT_ALLOWED_ACTIONS
    assert not re.search(r"dynamodb:(Put|Update|Delete|BatchWrite|TransactWrite|Query|Scan)", policy)
    assert "s3:" not in policy and "content_bucket" not in policy
    assert "logs:*" not in policy and "logs:CreateLogGroup" not in policy
    assert '"*"' not in policy  # no statement is on every resource
    assert "not_actions" not in policy and "not_resources" not in policy
    assert policy.count("statement {") == 3
    assert len(re.findall(r'effect\s*=\s*"Allow"', policy)) == 3
    # One row's table, its own log group, and nothing handed in wholesale.
    assert 'resources = [var.tables["MODEL_CONFIG_TABLE"].arn]' in policy
    assert 'resources = ["${aws_cloudwatch_log_group.agent.arn}:*"]' in policy
    assert "values(var.tables)" not in policy
    # Nothing else in the file grants the role anything: one inline policy, no managed one.
    module = _uncommented(_agent_module())
    assert module.count('resource "aws_iam_role_policy"') == 1
    assert "aws_iam_role_policy_attachment" not in module and "managed_policy_arns" not in module


def test_everything_the_agents_role_is_granted_is_named_here():
    """Across the module, the agent's role has its own policy (agent.tf), the briefings write
    (briefings.tf), the run-cost tally (costs.tf) and the cross-environment Deny (isolation.tf),
    and nothing else. The tally is UpdateItem on the Stats table alone."""
    granted = set()
    for path in sorted((INFRA / "modules" / "ops-assistant").glob("*.tf")):
        text = _uncommented(path.read_text(encoding="utf-8"))
        pattern = r'^resource "aws_iam_role_policy(?:_attachment)?" "([^"]+)" \{\n(.*?)^\}'
        for name, body in re.findall(pattern, text, re.S | re.M):
            if "aws_iam_role.ops_agent." in body:
                granted.add((path.name, name))
    assert granted == {
        ("agent.tf", "ops_agent"),
        ("briefings.tf", "ops_agent_briefings"),
        ("costs.tf", "ops_agent_stats"),
        ("isolation.tf", "ops_agent_other_environments_denied"),
    }, granted
    costs = _uncommented(_read("modules", "ops-assistant", "costs.tf"))
    assert set(re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', costs)) == {"dynamodb:UpdateItem"}
    assert "resources = [local.agent_stats_table.arn]" in costs
    assert 'lookup(var.tables, "STATS_CURRENT_TABLE", null)' in costs


def test_the_agents_bedrock_statement_names_what_the_shared_roles_does():
    """Invoking through an inference profile needs the profile and the foundation models behind
    it. The shared role's statement is the one proven against the account; the agent's is the same
    two resources, with the region read from the module's own local."""

    def bedrock_resources(text: str) -> set[str]:
        statement = re.search(r'sid\s*=\s*"BedrockInvoke"(.*?)\n  \}', text, re.S).group(1)
        assert re.search(r'actions\s*=\s*\["bedrock:InvokeModel"\]', statement)
        return set(re.findall(r'"(arn:aws:bedrock:[^"]+)"', statement))

    shared = bedrock_resources(_read("environments", "dev", "main.tf"))
    agent = {
        resource.replace("${local.aws_region}", _HOME)
        for resource in bedrock_resources(_agent_module())
    }

    assert agent == shared and len(shared) == 2
    assert re.search(r"aws_region\s*=\s*var\.aws_region", _ops_module())
    # Converse without streaming is InvokeModel; streaming would need a second action.
    agent_code = (ROOT / "lambdas" / "ops_agent" / "agent.py").read_text(encoding="utf-8")
    assert "streaming=False" in agent_code


def test_the_agent_is_a_plain_python_function_with_a_ceiling():
    module = _agent_module()
    function = _uncommented(_resource_block(module, "aws_lambda_function", "ops_agent"))
    variables = _read("modules", "ops-assistant", "variables.tf")

    assert re.search(r'handler\s*=\s*"ops_agent_handler\.handler"', function)
    handler = (ROOT / "lambdas" / "ops_agent_handler.py").read_text(encoding="utf-8")
    assert handler.count("\ndef handler(") == 1
    assert re.search(r'runtime\s*=\s*"python3\.11"', function)
    assert re.search(r'architectures\s*=\s*\["x86_64"\]', function)
    # No web adapter: no layer, no exec wrapper, no run.sh.
    assert "layers" not in function and "AWS_LAMBDA_EXEC_WRAPPER" not in function
    assert "AWS_LWA" not in function and "run.sh" not in _uncommented(module)
    # API Gateway gives up at 29 seconds; the agent's own waits must fit inside that.
    assert re.search(r"timeout\s*=\s*29$", function, re.M)
    assert re.search(r"memory_size\s*=\s*var\.agent_memory_size", function)
    assert re.search(r"reserved_concurrent_executions\s*=\s*var\.agent_reserved_concurrency", function)
    # No reservation by default: the account's whole concurrency quota is Lambda's minimum, so
    # reserving any of it is refused at apply (the first dev apply of the agent failed on 2).
    for name, default in (("agent_memory_size", "1024"), ("agent_reserved_concurrency", "-1")):
        block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', variables, re.S).group(1)
        assert re.search(rf"default\s*=\s*{default}$", block, re.M), name
    # Its own log group, made first, and the policy before the first invocation.
    assert 'name              = "/aws/lambda/${local.agent_name}"' in module
    depends_on = re.search(r"depends_on\s*=\s*\[(.*?)\]", function).group(1)
    assert "aws_cloudwatch_log_group.agent" in depends_on and "aws_iam_role_policy.ops_agent" in depends_on


def test_the_agent_is_told_everything_its_code_reads_and_no_tracing_is_switched_on():
    environment = _agent_environment()
    read_by_code = set()
    for path in (
        "ops_agent_handler.py",
        "ops_agent/agent.py",
        "ops_agent/policy.py",
        "ops_mcp/access.py",
        "ops_mcp/briefings.py",
        "ops_agent/quota.py",
    ):
        text = (ROOT / "lambdas" / path).read_text(encoding="utf-8")
        read_by_code |= set(re.findall(r'os\.environ(?:\.get\(|\[)"([A-Z_]+)"', text))
        read_by_code |= set(re.findall(r'^[A-Z_]+_ENV = "([A-Z_]+)"', text, re.M))
    read_by_code -= {"AWS_REGION", "AWS_DEFAULT_REGION"}  # set by Lambda itself
    read_by_code.add("MODEL_CONFIG_TABLE")  # common/dynamo.py's get_pipeline_config
    read_by_code.add("STATS_CURRENT_TABLE")  # common/stats_tracking.py's record_assistant_run
    # briefings.py's agent to start: read only by the MCP server, which starts one, never by the
    # agent, which is the one started.
    read_by_code.discard("OPS_AGENT_FUNCTION")
    # The deployment's name prefix: set on every function, read in common/naming.py (which is in
    # this package with the rest of common/), so that no code has to assume what things are called.
    naming = (ROOT / "lambdas" / "common" / "naming.py").read_text(encoding="utf-8")
    assert 'NAME_PREFIX_ENV = "NAME_PREFIX"' in naming
    read_by_code.add("NAME_PREFIX")

    assert set(environment) == read_by_code, set(environment) ^ read_by_code
    assert environment == {
        "OPS_AGENT_MODEL_ID": "var.agent_model_id",
        "NAME_PREFIX": "var.unique_name_prefix",
        "OPS_MCP_URL": "local.agent_mcp_url",
        "OPS_AGENT_ALLOWED_ORIGIN": "var.agent_allowed_origin",
        "MODEL_CONFIG_TABLE": 'var.tables["MODEL_CONFIG_TABLE"].name',
        "OPS_ASSISTANT_ALLOWED_CIDRS": 'join(",", var.allowed_cidrs)',
        "OPS_AGENT_FORWARD_KEY": "var.agent_forward_key",
        "OPS_BRIEFINGS_TABLE": "aws_dynamodb_table.briefings.name",
        "OPS_AGENT_DAILY_QUESTION_CAP": "tostring(var.agent_daily_question_cap)",
        "STATS_CURRENT_TABLE": 'local.agent_stats_table == null ? "" : local.agent_stats_table.name',
    }
    # A trace of an agent run carries the question and the answer: nothing switches one on.
    assert "OTEL_" not in _uncommented(_agent_module())
    assert "tracing_config" not in _uncommented(_agent_module())
    dynamo = (ROOT / "lambdas" / "common" / "dynamo.py").read_text(encoding="utf-8")
    assert 'get_table(os.environ["MODEL_CONFIG_TABLE"])' in dynamo
    # The same allowlist, written the same way, as the MCP server's.
    mcp_function = _resource_block(_ops_module(), "aws_lambda_function", "ops_mcp")
    assert re.search(r'OPS_ASSISTANT_ALLOWED_CIDRS\s*=\s*join\(",", var\.allowed_cidrs\)', mcp_function)


def test_the_agent_and_the_server_share_one_forward_key_and_no_plan_prints_it():
    """Under `allowlist` the server judges the agent's requests by the address the agent vouches
    for, and believes it only for the key (ops_mcp/access.py). Two different keys, or a key on
    one function only, and every question asked through the agent is refused."""
    from ops_mcp import access

    mcp_function = _uncommented(_resource_block(_ops_module(), "aws_lambda_function", "ops_mcp"))
    agent_function = _uncommented(_resource_block(_agent_module(), "aws_lambda_function", "ops_agent"))
    wired = rf"^\s*{access.FORWARD_KEY_ENV}\s*=\s*var\.agent_forward_key$"
    assert access.FORWARD_KEY_ENV == "OPS_AGENT_FORWARD_KEY"
    assert len(re.findall(wired, mcp_function, re.M)) == 1
    assert len(re.findall(wired, agent_function, re.M)) == 1
    # Nowhere else: not an output, not a tag, not a description.
    for name in ("main.tf", "agent.tf", "memory.tf", "outputs.tf"):
        code = _uncommented(_read("modules", "ops-assistant", name))
        assert code.count("agent_forward_key") == (1 if name in ("main.tf", "agent.tf") else 0), name

    variable = re.search(
        r'variable "agent_forward_key" \{(.*?)\n\}', _read("modules", "ops-assistant", "variables.tf"), re.S
    ).group(1)
    assert re.search(r"^\s*sensitive\s*=\s*true$", variable, re.M)
    assert re.search(r'^\s*default\s*=\s*""$', variable, re.M)  # no key: vouching is off
    # Empty, or as long as the code needs before it counts the key at all, and nothing a header
    # could not carry.
    pattern = re.search(r'regex\("([^"]+)", var\.agent_forward_key\)', variable).group(1)
    assert pattern == f"^[A-Za-z0-9]{{{access.MIN_FORWARD_KEY_CHARS},}}$"
    assert 'var.agent_forward_key == "" ||' in variable

    dev = _read("environments", "dev", "main.tf")
    call = _module_blocks(dev, "modules/ops-assistant")[0]
    assert re.search(
        r"^\s*agent_forward_key\s*=\s*random_password\.ops_agent_forward_key\.result$", call, re.M
    )
    password = _uncommented(_resource_block(dev, "random_password", "ops_agent_forward_key"))
    length = int(re.search(r"length\s*=\s*(\d+)", password).group(1))
    assert length == 48 and length >= access.MIN_FORWARD_KEY_CHARS
    assert re.search(r"special\s*=\s*false", password)  # letters and digits: what the variable takes
    assert "keepers" not in password  # made once: it does not change from one apply to the next
    # The key is used for the module and nothing else, and no output shows it.
    assert _uncommented(dev).count("random_password.ops_agent_forward_key") == 1
    assert "forward_key" not in _read("environments", "dev", "outputs.tf")
    assert re.search(r'source\s*=\s*"hashicorp/random"', dev)


def test_the_agent_calls_this_modules_own_mcp_endpoint_at_the_host_the_server_accepts():
    """The server refuses any Host not on OPS_MCP_ALLOWED_HOSTS, which is local.api_host. The URL
    the agent is given is built on the same local, and not on the stage's invoke_url: the stage
    depends on the deployment, which depends on the agent's integrations, which depend on the
    function this URL is an input of."""
    module = _agent_module()
    function = _uncommented(_resource_block(module, "aws_lambda_function", "ops_agent"))

    assert 'agent_mcp_url = "https://${local.api_host}/${var.stage_name}/mcp"' in module
    assert "aws_api_gateway_stage" not in function and "aws_api_gateway_deployment" not in function
    assert re.search(r"OPS_MCP_ALLOWED_HOSTS\s*=\s*local\.api_host", _ops_module())
    outputs = _read("modules", "ops-assistant", "outputs.tf")
    assert 'value       = "${aws_api_gateway_stage.this.invoke_url}/mcp"' in outputs
    stage = _resource_block(_ops_module(), "aws_api_gateway_stage", "this")
    assert re.search(r"stage_name\s*=\s*var\.stage_name", stage)


def test_dev_gives_the_agent_the_pipelines_model_and_the_sites_origin():
    dev = _read("environments", "dev", "main.tf")
    call = _module_blocks(dev, "modules/ops-assistant")[0]

    assert re.search(r"^\s*agent_model_id\s*=\s*local\.bedrock_model_id$", call, re.M)
    assert re.search(r"BEDROCK_MODEL_ID\s*=\s*local\.bedrock_model_id", dev)
    assert re.search(r"^\s*agent_allowed_origin\s*=\s*local\.site_url$", call, re.M)
    assert 'callback_urls = ["${local.site_url}/ask.html"]' in call
    # An origin has no path and no trailing slash, or a browser's Origin header never equals it.
    assert 'site_url = "https://${module.static_site.distribution_domain_name}"' in dev
    # Dev leaves the reservation at the module's default (none), and never sets 0, which would
    # switch the agent off.
    assert not re.search(r"agent_reserved_concurrency\s*=", call)


def test_ask_is_behind_the_same_authorizer_and_scope_and_only_the_preflight_is_open():
    module = _agent_module()
    post = _resource_block(module, "aws_api_gateway_method", "ask")
    options = _resource_block(module, "aws_api_gateway_method", "ask_options")
    mcp = _resource_block(_ops_module(), "aws_api_gateway_method", "mcp")

    assert module.count('resource "aws_api_gateway_method"') == 2
    assert re.search(r'path_part\s*=\s*"ask"', _resource_block(module, "aws_api_gateway_resource", "ask"))
    assert re.search(r"rest_api_id\s*=\s*aws_api_gateway_rest_api\.this\.id", post)  # the module's one API
    assert 'resource "aws_api_gateway_rest_api"' not in module
    assert 'resource "aws_api_gateway_authorizer"' not in module
    assert re.search(r'http_method\s*=\s*"POST"', post)
    for line in (
        r'authorization\s*=\s*"COGNITO_USER_POOLS"',
        r"authorizer_id\s*=\s*aws_api_gateway_authorizer\.cognito\.id",
        r"authorization_scopes\s*=\s*\[local\.read_scope\]",
    ):
        assert re.search(line, post), line
        assert re.search(line, mcp), line
    assert re.search(r'http_method\s*=\s*"OPTIONS"', options)
    assert re.search(r'authorization\s*=\s*"NONE"', options)
    assert "authorizer_id" not in options and "authorization_scopes" not in options
    # The handler answers the preflight before it reads anything, and routes on "POST /ask".
    handler = (ROOT / "lambdas" / "ops_agent_handler.py").read_text(encoding="utf-8")
    assert '_route_key(event) != "POST /ask"' in handler
    body = handler[handler.index("\ndef handler(") :]
    assert body.index('== "OPTIONS"') < body.index("_admitted(event)") < body.index("_route_key(event)")
    for name in ("ask", "ask_options"):
        integration = _resource_block(module, "aws_api_gateway_integration", name)
        assert re.search(r'type\s*=\s*"AWS_PROXY"', integration)
        assert re.search(r"uri\s*=\s*aws_lambda_function\.ops_agent\.invoke_arn", integration)
    assert "response_transfer_mode" not in module and "aws_lambda_function_url" not in module


def test_the_stage_is_redeployed_when_the_agents_routes_change_and_throttles_them():
    """A deployment is a snapshot. Routes added to the API and not to the deployment's trigger
    exist and are not served; a scope changed in place would go on being served as it was."""
    deployment = _resource_block(_ops_module(), "aws_api_gateway_deployment", "this")
    module = _agent_module()
    redeployment = re.search(r"agent_redeployment = \{\n(.*?)\n  \}", module, re.S).group(1)

    assert re.search(r"agent\s*=\s*local\.agent_redeployment", deployment)
    for needed in (
        "aws_api_gateway_resource.ask.id",
        "aws_api_gateway_method.ask.id",
        "aws_api_gateway_method.ask.authorization",
        "aws_api_gateway_method.ask.authorizer_id",
        "aws_api_gateway_method.ask.authorization_scopes",
        "aws_api_gateway_integration.ask.id",
        "aws_api_gateway_integration.ask.uri",
        "aws_api_gateway_method.ask_options.id",
        "aws_api_gateway_method.ask_options.authorization",
        "aws_api_gateway_integration.ask_options.id",
        "aws_api_gateway_integration.ask_options.uri",
    ):
        assert re.search(rf"=\s*{re.escape(needed)}$", redeployment, re.M), needed
    # The stage's throttle is on every method of the stage, so on these two as well.
    settings = _resource_block(_ops_module(), "aws_api_gateway_method_settings", "all")
    assert re.search(r'method_path\s*=\s*"\*/\*"', settings)


def test_only_the_two_ask_methods_may_invoke_the_agent():
    module = _uncommented(_agent_module())
    permissions = re.findall(r'^resource "aws_lambda_permission" "[^"]+" \{\n(.*?)^\}', module, re.S | re.M)

    assert len(permissions) == 2
    sources = set()
    statement_ids = set()
    for permission in permissions:
        assert re.search(r"function_name\s*=\s*aws_lambda_function\.ops_agent\.function_name", permission)
        assert re.search(r'principal\s*=\s*"apigateway\.amazonaws\.com"', permission)
        assert re.search(r'action\s*=\s*"lambda:InvokeFunction"', permission)
        sources.add(re.search(r'source_arn\s*=\s*"([^"]+)"', permission).group(1))
        statement_ids.add(re.search(r'statement_id\s*=\s*"([^"]+)"', permission).group(1))
    assert sources == {
        "${aws_api_gateway_rest_api.this.execution_arn}/*/POST/ask",
        "${aws_api_gateway_rest_api.this.execution_arn}/*/OPTIONS/ask",
    }
    assert len(statement_ids) == 2
    # And the MCP server's permission still names its own route only.
    mcp_permission = _resource_block(_ops_module(), "aws_lambda_permission", "apigw")
    assert '/*/POST/mcp"' in mcp_permission


def test_the_agents_package_holds_what_the_handler_imports_built_for_the_runtime():
    import ast
    import sys

    module = _agent_module()
    build = re.search(
        r'^resource "terraform_data" "agent_package" \{\n(.*?)^\}', module, re.S | re.M
    ).group(1)

    for needed in (
        'cp -r "${local.lambdas_dir}/ops_agent" "$build_dir/ops_agent"',
        'cp "${local.lambdas_dir}/ops_agent_handler.py" "$build_dir/ops_agent_handler.py"',
        'cp -r "${local.lambdas_dir}/common" "$build_dir/common"',
        'cp "${local.lambdas_dir}/ops_mcp/__init__.py" "$build_dir/ops_mcp/__init__.py"',
        'cp "${local.lambdas_dir}/ops_mcp/access.py" "$build_dir/ops_mcp/access.py"',
        'cp "${local.lambdas_dir}/ops_mcp/briefings.py" "$build_dir/ops_mcp/briefings.py"',
        "--platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all:",
        '-r "${local.lambdas_dir}/requirements-ops-agent.txt"',
        '-t "$build_dir"',
        "always_run = timestamp()",
    ):
        assert needed in build, needed
    # Not the MCP server, and not the pipeline's own requirements.
    assert "/ops_mcp\" " not in build and "server.py" not in build
    assert "/requirements.txt" not in build and "run.sh" not in build
    archive = re.search(r'^data "archive_file" "agent_package" \{\n(.*?)^\}', module, re.S | re.M).group(1)
    assert "source_dir  = local.agent_build_dir" in archive
    assert "${terraform_data.agent_package.id}.zip" in archive  # read at apply, after the build
    function = _resource_block(module, "aws_lambda_function", "ops_agent")
    assert "data.archive_file.agent_package.output_path" in function
    assert "data.archive_file.agent_package.output_base64sha256" in function
    assert "lambda-build/" in (ROOT / ".gitignore").read_text(encoding="utf-8")

    # The requirements file brings the agent framework and, through the MCP server's own file, the
    # same release of the MCP SDK.
    requirements = (ROOT / "lambdas" / "requirements-ops-agent.txt").read_text(encoding="utf-8")
    assert re.search(r"^strands-agents==", requirements, re.M)
    assert re.search(r"^-r requirements-ops-mcp\.txt$", requirements, re.M)

    # What the handler imports of this repo is what is copied, and the copied modules it reaches
    # need nothing pip does not install here: the standard library, boto3 (which strands-agents
    # brings), and each other.
    def imported(path: str) -> set[str]:
        tree = ast.parse((ROOT / "lambdas" / path).read_text(encoding="utf-8"))
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names |= {f"{node.module}.{alias.name}" for alias in node.names} | {node.module}
        return names

    ours = {"common", "ops_agent", "ops_mcp"}
    handler_imports = {name for name in imported("ops_agent_handler.py") if name.split(".")[0] in ours}
    assert handler_imports == {
        "common",
        "common.dynamo",
        "ops_agent",
        "ops_agent.agent",
        "ops_agent.policy",
        "ops_agent.quota",
        "ops_mcp",
        "ops_mcp.access",
        "ops_mcp.briefings",
    }
    assert (ROOT / "lambdas" / "ops_mcp" / "__init__.py").read_text(encoding="utf-8").strip().endswith('"""')
    allowed = set(sys.stdlib_module_names) | {"boto3", "botocore", "common"}
    for path in (
        "common/__init__.py",
        "common/dynamo.py",
        "common/assistant_access.py",
        "ops_mcp/access.py",
        "ops_mcp/briefings.py",
    ):
        roots = {name.split(".")[0] for name in imported(path)}
        assert roots <= allowed, (path, roots - allowed)
    access_imports = {name for name in imported("ops_mcp/access.py") if name.startswith("common")}
    assert {name.rsplit(".", 1)[0] for name in access_imports if "." in name} <= {
        "common",
        "common.assistant_access",
        "common.dynamo",
    }


def test_the_deploy_role_can_already_make_everything_the_agent_adds():
    """Nothing is added to infra/bootstrap for the agent: each thing it creates falls under a
    statement that is already there. If one of those is narrowed, this says what the agent needs."""
    import fnmatch

    bootstrap = _read("bootstrap", "main.tf")

    def statement(sid: str) -> str:
        return re.search(rf'sid\s*=\s*"{sid}"(.*?)\n  \}}', bootstrap, re.S).group(1)

    # The function, its code, its permissions and its reserved concurrency
    # (lambda:PutFunctionConcurrency) are all lambda:* on the function's ARN.
    functions = statement("LambdaFunctions")
    assert re.search(r'actions\s*=\s*\["lambda:\*"\]', functions)
    pattern = re.search(r'"arn:aws:lambda:\$\{var\.aws_region\}:\*:function:([^"]+)"', functions).group(1)
    assert fnmatch.fnmatch("bloggerbear-dev-ops-agent", pattern)
    # Its log group.
    log_groups = statement("LambdaLogGroups")
    log_patterns = re.findall(r'"arn:aws:logs:\$\{var\.aws_region\}:\*:log-group:([^"]+)"', log_groups)
    assert any(fnmatch.fnmatch("/aws/lambda/bloggerbear-dev-ops-agent", name) for name in log_patterns)
    # Its role: created, given an inline policy and passed to Lambda.
    roles = statement("LambdaExecRole")
    assert re.search(r'actions\s*=\s*\["iam:\*"\]', roles)
    assert '"arn:aws:iam::*:role/bloggerbear-*-lambda-exec"' in roles
    # The routes: resources, methods and integrations on a REST API, and its deployments.
    assert f'"arn:aws:apigateway:{_HOME}::/restapis/*"' in statement("ApiGateway")
    # No layer is used, so nothing like the Web Adapter's layer statement is needed.
    assert "layers" not in _uncommented(_resource_block(_agent_module(), "aws_lambda_function", "ops_agent"))


def test_the_module_and_dev_output_the_agents_url():
    module_outputs = _read("modules", "ops-assistant", "outputs.tf")
    dev_outputs = _read("environments", "dev", "outputs.tf")

    assert re.search(
        r'output "ops_ask_url" \{\n\s*value\s*=\s*"\$\{aws_api_gateway_stage\.this\.invoke_url\}/ask"\n',
        module_outputs,
    )
    assert re.search(
        r'output "ops_ask_url" \{\n\s*value\s*=\s*module\.ops_assistant\.ops_ask_url\n', dev_outputs
    )


def test_nothing_the_agent_names_for_aws_holds_an_apostrophe():
    """An apostrophe in a name or description given to an AWS service stopped an apply of this
    module once (Cognito). Comments may have them; strings may not."""
    code = _uncommented(_agent_module())
    # The one place quotes belong: API Gateway's way of writing a fixed header value.
    code = code.replace("\"'${var.agent_allowed_origin}'\"", "")

    assert "'" not in code


def test_api_gateways_own_errors_carry_the_cors_header_for_the_one_origin():
    """A 401 from the authorizer, a 403 for a missing scope, a 429 from the throttle and a 5xx
    never reach the handler, so its CORS headers are not on them. Without the header the page is
    told "network error" and cannot tell an expired token from an outage."""
    module = _agent_module()
    responses = _resource_block(module, "aws_api_gateway_gateway_response", "cors")
    code = _uncommented(responses)

    types = re.search(r"for_each\s*=\s*toset\(\[(.*?)\]\)", code).group(1)
    assert set(re.findall(r'"([A-Z0-9_]+)"', types)) == {
        "UNAUTHORIZED",
        "ACCESS_DENIED",
        "THROTTLED",
        "DEFAULT_5XX",
    }
    assert re.search(r"response_type\s*=\s*each\.key", code)
    assert re.search(r"rest_api_id\s*=\s*aws_api_gateway_rest_api\.this\.id", code)
    # The one origin the function itself answers with: never "*", and left off when there is none.
    assert (
        '"gatewayresponse.header.Access-Control-Allow-Origin" = "\'${var.agent_allowed_origin}\'"' in code
    )
    assert code.count("gatewayresponse.") == 1 and "*" not in code
    assert re.search(
        r'response_parameters\s*=\s*merge\(\s*var\.agent_allowed_origin == "" \? \{\} : \{', code
    )
    # The one other header: a 401's WWW-Authenticate, for MCP clients (alexa.tf).
    assert re.search(r'each\.key == "UNAUTHORIZED" \? local\.www_authenticate : \{\}', code)
    assert re.search(r"OPS_AGENT_ALLOWED_ORIGIN\s*=\s*var\.agent_allowed_origin", module)
    # The status and the body stay API Gateway's own.
    assert "status_code" not in code and "response_templates" not in code
    # And the stage is redeployed when they change.
    redeployment = re.search(r"agent_redeployment = \{\n(.*?)\n  \}\n\}", module, re.S).group(1)
    assert "in aws_api_gateway_gateway_response.cors" in redeployment
    assert "response.response_parameters" in redeployment
    # The deploy role may write them: they are under the REST API's own path.
    bootstrap = _read("bootstrap", "main.tf")
    api_gateway = re.search(r'sid\s*=\s*"ApiGateway"(.*?)\n  \}', bootstrap, re.S).group(1)
    assert re.search(r'actions\s*=\s*\["apigateway:\*"\]', api_gateway)
    assert f'"arn:aws:apigateway:{_HOME}::/restapis/*"' in api_gateway


# --- Configurable region (docs/deployment-runsheet.md, "Deploying to another region") --------------
#
# One plain setting, the AWS_REGION GitHub variable (var.aws_region in Terraform), chooses the
# deployment's home region. Unset it is the region this project has always used, so the original
# deployment renders every ARN and host name exactly as before. us-east-1 stays written out only
# where AWS serves the thing nowhere else.

_ORIGINAL_REGION = "ap-southeast-2"
_REGION_FALLBACK = "${{ vars.AWS_REGION || 'ap-southeast-2' }}"

# The only lines that may still write the original region out, by file. Each is a default or a
# fallback: the value used when nothing is set. Anything else that names it is a place the setting
# does not reach, which is the bug these tests exist to catch.
_REGION_LITERAL_ALLOWED = {
    # var.aws_region's default, in each root.
    "infra/bootstrap/variables.tf": [r'default\s*=\s*"ap-southeast-2"'],
    "infra/environments/dev/variables.tf": [r'default\s*=\s*"ap-southeast-2"'],
    "infra/environments/production/variables.tf": [r'default\s*=\s*"ap-southeast-2"'],
    # The backend blocks: Terraform allows no variable there, and the original deployment's init
    # must stay the bare command, so its state bucket's region stays written (CI overrides it with
    # -backend-config when AWS_REGION or TF_STATE_REGION is set).
    "infra/environments/dev/main.tf": [r'region\s*=\s*"ap-southeast-2"'],
    "infra/environments/production/main.tf": [r'region\s*=\s*"ap-southeast-2"'],
    # The workflows' fallback when the AWS_REGION variable is unset.
    ".github/workflows/terraform.yml": [
        r"aws-region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
        r"TF_VAR_aws_region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
    ],
    ".github/workflows/destroy-dev.yml": [
        r"aws-region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
        r"TF_VAR_aws_region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
    ],
    ".github/workflows/terraform-production-release.yml": [
        r"aws-region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
        r"TF_VAR_aws_region: \$\{\{ vars\.AWS_REGION \|\| 'ap-southeast-2' \}\}",
    ],
    # The setup script's one copy of the default, which its own tests hold to Terraform's.
    "scripts/setup_repo.py": [r'DEFAULT_REGION = "ap-southeast-2"'],
    # An example to `source` by hand: whatever region is already exported wins, else the default.
    "scripts/force_publish_example.sh": [
        r'export AWS_DEFAULT_REGION="\$\{AWS_DEFAULT_REGION:-ap-southeast-2\}"'
    ],
}
_REGION_SCANNED_SUFFIXES = {
    ".tf", ".tfvars", ".hcl", ".tftpl", ".yml", ".yaml", ".py", ".sh", ".js", ".html", ".css", ".json",
}

# Why a us-east-1 written in code is allowed to be there: a comment beside it has to give one of
# these reasons. CloudFront (its certificate, its web ACL with that ACL's logs and metrics, its own
# metrics) and Cost Explorer's only endpoint are the things AWS serves from that region alone.
_US_EAST_1_REASON = re.compile(r"cloudfront|cost explorer", re.I)
_US_EAST_1_COMMENT_REACH = 6  # lines above the literal in which the comment must sit


def _region_scanned_files():
    """Deployed code and configuration: not tests (a test may name a region), not documentation."""
    for top in ("infra", ".github/workflows", "lambdas", "frontend", "scripts"):
        for path in sorted((ROOT / top).rglob("*")):
            parts = set(path.relative_to(ROOT).parts)
            if not path.is_file() or path.suffix not in _REGION_SCANNED_SUFFIXES:
                continue
            if parts & (_SKIPPED_DIRS | {"tests"}):
                continue
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8", errors="replace")


def test_the_original_region_is_written_only_as_a_default_or_a_fallback():
    stray = []
    seen = {name: 0 for name in _REGION_LITERAL_ALLOWED}
    for name, text in _region_scanned_files():
        for number, line in enumerate(text.splitlines(), 1):
            if _ORIGINAL_REGION not in line:
                continue
            if any(re.fullmatch(pattern, line.strip()) for pattern in _REGION_LITERAL_ALLOWED.get(name, [])):
                seen[name] += 1
            else:
                stray.append(f"{name}:{number}: {line.strip()[:120]}")
    assert not stray, "the home region is var.aws_region / AWS_REGION, not a literal:\n" + "\n".join(stray)
    # And the list above describes the tree: one line per pattern, none of it stale.
    assert seen == {name: len(patterns) for name, patterns in _REGION_LITERAL_ALLOWED.items()}


def test_every_us_east_1_left_in_code_sits_beside_a_comment_saying_why():
    """us-east-1 is acceptable only where AWS requires it. In prose (a comment, a description) it is
    the explanation; written as a value (a quoted region, or the region of an ARN) it needs one."""
    unexplained = []
    values = 0
    for name, text in _region_scanned_files():
        lines = text.splitlines()
        for index, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*")):
                continue
            if not re.search(r"""["']us-east-1["']|:us-east-1:""", line):
                continue
            values += 1
            before = lines[max(0, index - _US_EAST_1_COMMENT_REACH) : index]
            comments = [other for other in before if other.strip().startswith(("#", "//"))]
            if not any(_US_EAST_1_REASON.search(comment) for comment in comments):
                unexplained.append(f"{name}:{index + 1}: {stripped[:120]}")
    assert not unexplained, "say why this must be us-east-1, in a comment just above:\n" + "\n".join(
        unexplained
    )
    # Production's provider alias, the dashboards' CloudFront region, two log group ARN pairs and
    # the Cost Explorer client: if this drops to nothing the scan has stopped finding them.
    assert values >= 5


@pytest.mark.parametrize("root", _ROOTS, ids=lambda parts: parts[-1])
def test_each_root_has_one_region_variable_with_the_old_default_and_it_is_not_sensitive(root):
    variables = _read(*root, "variables.tf")
    assert variables.count('variable "aws_region" {') == 1
    block = variables.split('variable "aws_region" {')[1].split("\n}\n")[0]

    assert re.search(r'^  default\s*=\s*"ap-southeast-2"$', block, re.M)
    # Region shaped, and no more than that: which regions exist is AWS's list, not this file's.
    assert 'condition     = can(regex("^[a-z]{2}(-[a-z]+)+-[0-9]+$", var.aws_region))' in block
    # A region is not a secret, and a sensitive one would hide every ARN in the plan.
    assert not re.search(r"^\s*sensitive\s*=", block, re.M)

    for block in _provider_blocks(_read(*root, "main.tf")):
        if 'region = "us-east-1"' in block:
            continue  # production's real us-east-1 alias, checked above
        assert "\n  region = var.aws_region\n" in block, block


def test_the_region_shape_check_accepts_real_regions_and_refuses_the_rest():
    shape = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]+$")
    for region in ("ap-southeast-2", "us-east-1", "eu-west-1", "us-gov-west-1", "me-central-1"):
        assert shape.match(region), region
    for wrong in ("", "Sydney", "ap-southeast", "EU-WEST-1", "eu-west-1 ", "eu_west_1", "eu-west-1a"):
        assert not shape.match(wrong), wrong


@pytest.mark.parametrize("module", ["observability", "ops-assistant", "rest-api", "static-site"])
def test_each_module_that_names_a_region_is_given_it_by_the_root(module):
    """No default in the module, so a root that forgets to pass it fails validate instead of quietly
    building something for the wrong region."""
    variables = _read("modules", module, "variables.tf")
    block = variables.split('variable "aws_region" {')[1].split("\n}\n")[0]
    assert "default" not in block.replace("No default", "")

    for env in ("dev", "production"):
        calls = _module_blocks(_read("environments", env, "main.tf"), f"modules/{module}")
        assert calls or (module, env) == ("ops-assistant", "production")
        for call in calls:
            assert re.search(r"^  aws_region\s*=\s*var\.aws_region$", call, re.M), (env, module)


def test_everything_the_page_and_the_browser_are_told_follows_the_region():
    """The API host names, the sign-in host name and the site's CSP each contain the region."""
    assert (
        'value       = "${aws_api_gateway_rest_api.this.id}.execute-api.${var.aws_region}.amazonaws.com"'
        in _read("modules", "rest-api", "outputs.tf")
    )
    assert (
        "[\"connect-src 'self' https://*.execute-api.${var.aws_region}.amazonaws.com\"]"
        in _read("modules", "static-site", "main.tf")
    )
    assert (
        '"${aws_cognito_user_pool_domain.this.domain}.auth.${local.aws_region}.amazoncognito.com"'
        in _read("modules", "ops-assistant", "outputs.tf")
    )
    assert re.search(
        r'layer_arn = "arn:aws:lambda:\$\{local\.aws_region\}:\d{12}:layer:LambdaAdapterLayerX86:\d+"',
        _ops_module(),
    )
    dashboards = _dashboards()
    assert "\n  api_region = var.aws_region\n" in dashboards
    assert 'region = "' not in _uncommented(_read("modules", "observability", "main.tf"))


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_pipeline_may_call_profiles_in_the_home_region_and_models_in_any(env):
    """A cross-region inference profile lives in the region it is called in and routes to models
    in several, so the profile half follows the setting and the model half stays a wildcard."""
    main = _read("environments", env, "main.tf")
    statement = re.search(r'sid\s*=\s*"BedrockInvoke"(.*?)\n  \}', main, re.S).group(1)
    assert set(re.findall(r'"(arn:aws:bedrock:[^"]+)"', statement)) == {
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/*",
    }


@pytest.mark.parametrize(
    "workflow", ["terraform.yml", "destroy-dev.yml", "terraform-production-release.yml"]
)
def test_each_deploy_workflow_reads_the_region_from_a_plain_variable(workflow):
    text = (ROOT / ".github" / "workflows" / workflow).read_text(encoding="utf-8")

    # A variable, never a secret, with the original region as the fallback: Terraform and the
    # credentials step are both handed what they were always handed when nothing is set.
    assert f"      TF_VAR_aws_region: {_REGION_FALLBACK}\n" in text
    assert f"          aws-region: {_REGION_FALLBACK}\n" in text
    assert "secrets.AWS_REGION" not in text and "secrets.TF_STATE_REGION" not in text
    # The state bucket's region: its own setting, else the home region, else nothing at all, which
    # leaves the init the bare command (checked with the bucket, above).
    assert "      TF_STATE_REGION: ${{ vars.TF_STATE_REGION || vars.AWS_REGION }}\n" in text
    assert text.count("aws-region:") == 1


def test_the_fork_guide_says_what_another_region_needs():
    guide = (ROOT / "docs" / "deployment-runsheet.md").read_text(encoding="utf-8")
    section = guide.split("## Deploying to another region")[1].split("\n## ")[0]

    for needed in ("`AWS_REGION`", "`TF_STATE_REGION`", "bedrock_inference_profile_id", "`us-east-1`"):
        assert needed in section, needed
    # In the settings table (docs/configuration.md) as a variable, not a secret.
    table = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert re.search(r"^\| `AWS_REGION` \| variable \| repo \|", table, re.M)
    assert re.search(r"^\| `TF_STATE_REGION` \| variable \| repo \|", table, re.M)
    # And no longer listed as something a fork cannot change.
    tied = guide.split("## What is still tied to the original deployment")[1]
    assert "**The region.**" not in tied


# --- The name prefix --------------------------------------------------------------------------------
#
# Every resource is "<prefix>-<env>-<resource>". The prefix is var.unique_name_prefix (the
# UNIQUE_NAME_PREFIX GitHub Actions variable), "bloggerbear" by default, so the original deployment's
# names are what they always were and a second deployment can have its own. The tests above read the
# Terraform with the default written in (_read); these hold the things that makes honest.

_NAME_ROOTS = ("bootstrap", "environments/dev", "environments/production")
# Not resource-name prefixes, so not built from the variable: the state bucket named in the backend
# blocks (a backend cannot read a variable; a fork overrides it with TF_STATE_BUCKET_*) and its
# default in bootstrap, which has a variable of its own; the Cognito scope; the site's domain; the
# marker in the keep-warm event; and the rule for the Project tag, which is where the default
# prefix is compared against (test_resource_tags.py holds that rule).
_NOT_A_NAME_PREFIX = (
    'var.unique_name_prefix == "bloggerbear" ? "BloggerBear" : var.unique_name_prefix',
    "bloggerbear-terraform-state",
    '"bloggerbear-ops"',
    "bloggerbear.com",
    "bloggerbear.keep-warm",
)
# The longest prefix the variable accepts: its first validation is [a-z]([a-z0-9-]{0,12}[a-z0-9])?.
_LONGEST_PREFIX = "x" * 14


def _prefix_variable(root: str) -> str:
    variables = (INFRA / root / "variables.tf").read_text(encoding="utf-8")
    return variables.split('variable "unique_name_prefix" {')[1].split("\n}\n")[0]


@pytest.mark.parametrize("root", _NAME_ROOTS)
def test_the_name_prefix_defaults_to_the_original_deployments_and_is_validated(root):
    block = _prefix_variable(root)
    # The default is what keeps every existing name: change it and Terraform replaces everything.
    assert re.search(r'^  default\s*=\s*"bloggerbear"$', block, re.M)
    assert DEFAULT_PREFIX == "bloggerbear"
    # Lowercase letters, digits and hyphens; starts with a letter; no trailing hyphen; 14 at most.
    assert 'can(regex("^[a-z]([a-z0-9-]{0,12}[a-z0-9])?$", var.unique_name_prefix))' in block
    assert '!strcontains(var.unique_name_prefix, "--")' in block
    assert '!can(regex("aws|amazon|cognito", var.unique_name_prefix))' in block
    # The same three rules in all three roots: bootstrap scopes the deploy roles to the very
    # prefix the environments name things with, so a value one accepts, the others must.
    rules = re.findall(r"^\s*condition\s*=\s*(.+)$", block, re.M)
    assert rules == re.findall(r"^\s*condition\s*=\s*(.+)$", _prefix_variable(_NAME_ROOTS[0]), re.M)
    assert len(rules) == 3
    # The description says which name sets the limit, and that it cannot change later.
    assert "<prefix>-production-<topic_id>-research-tick" in block and "At most 14 characters." in block
    if root != "bootstrap":
        assert "NEVER change this" in block and "UNIQUE_NAME_PREFIX" in block


def test_no_resource_name_is_written_with_the_prefix_spelled_out():
    """The point of the setting: outside comments and descriptions, nothing in infra/ names a
    resource "bloggerbear-..." or a parameter "/bloggerbear/...". A name written out would be the
    one resource a deployment with another prefix cannot create (the deploy role is scoped to its
    own prefix) or, worse, one it shares with the original deployment."""
    checked = 0
    for path in terraform_files():
        prose = path.name in ("variables.tf", "outputs.tf")
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            if prose and not re.match(r"\s*(condition|default)\s*=", line):
                continue  # a description or an error message: words, where the default is an example
            rest = line
            for allowed in _NOT_A_NAME_PREFIX:
                rest = rest.replace(allowed, "")
            if re.match(r'\s*default\s*=\s*"bloggerbear"$', rest):
                continue  # the default itself
            assert "bloggerbear" not in rest, f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
            checked += PREFIX_REFERENCE in line
    # And the names really are built from the variable: there are hundreds of them.
    assert checked > 200


def test_every_module_that_names_resources_is_passed_the_prefix_by_both_environments():
    """A module has no default for it, on purpose: a caller that forgot would get the original
    deployment's names inside another deployment. `terraform validate` then refuses a call
    without it; this says which modules those are and that the value is the root's variable."""
    modules = sorted(path.parent.name for path in (INFRA / "modules").glob("*/variables.tf"))
    naming = []
    for module in modules:
        uses = any(
            "var.unique_name_prefix" in path.read_text(encoding="utf-8")
            for path in (INFRA / "modules" / module).glob("*.tf")
            if path.name != "variables.tf"
        )
        variables = (INFRA / "modules" / module / "variables.tf").read_text(encoding="utf-8")
        declared = 'variable "unique_name_prefix" {' in variables
        assert declared == uses, module
        if declared:
            block = variables.split('variable "unique_name_prefix" {')[1].split("\n}\n")[0]
            assert "default" not in re.sub(r"description\s*=.*", "", block), module
            naming.append(module)
    assert naming == ["api-cdn", "app-data", "observability", "ops-assistant", "static-site"]
    for env in ("dev", "production"):
        main = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")
        for module in naming:
            blocks = _module_blocks(main, f"modules/{module}")
            assert blocks, (env, module)
            for block in blocks:
                assert re.search(r"^\s*unique_name_prefix\s*=\s*var\.unique_name_prefix$", block, re.M), (
                    env,
                    module,
                )
        # The two modules that are handed whole names are handed ones built from it.
        assert re.search(r'^\s*name\s*=\s*"\$\{var\.unique_name_prefix\}-' + env + '"$', main, re.M)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_lambda_is_told_the_prefix(env):
    """NAME_PREFIX on every function: the code that builds a name (the per-topic schedules) or
    recognises one (the assistant) reads it from there, common/naming.py, and never assumes it.
    An environment variable costs nothing to read; an SSM parameter would cost a call on every
    cold start and a permission on every role."""
    main = (INFRA / "environments" / env / "main.tf").read_text(encoding="utf-8")
    shared = main.split("  lambda_env_variables = {")[1].split("\n}\n")[0]
    assert re.search(r"^\s*NAME_PREFIX\s*=\s*var\.unique_name_prefix$", shared, re.M)
    functions = re.findall(r'^resource "aws_lambda_function" "(\w+)" \{\n(.*?)^\}', main, re.S | re.M)
    assert len(functions) >= 11
    for name, body in functions:
        assert "local.lambda_env_variables" in body, name

    naming = (ROOT / "lambdas" / "common" / "naming.py").read_text(encoding="utf-8")
    assert 'NAME_PREFIX_ENV = "NAME_PREFIX"' in naming and 'DEFAULT_NAME_PREFIX = "bloggerbear"' in naming
    assert "NAME_PREFIX = os.environ.get(NAME_PREFIX_ENV) or DEFAULT_NAME_PREFIX" in naming
    # The assistant's two functions, which the module makes.
    for text, resource in ((_ops_module(), "ops_mcp"), (_agent_module(), "ops_agent")):
        function = _uncommented(_resource_block(text, "aws_lambda_function", resource))
        assert re.search(r"^\s*NAME_PREFIX\s*=\s*var\.unique_name_prefix$", function, re.M), resource
    # Nothing reads the prefix from SSM: no parameter is declared for it, in any root.
    for path in terraform_files():
        assert not re.search(r"name-prefix|name_prefix_parameter", path.read_text(encoding="utf-8")), path


def test_the_deploy_roles_are_named_and_scoped_by_the_prefix():
    """Bootstrap's half. With another prefix the roles must be allowed that prefix's resources and
    no longer the original deployment's: every name scope in the policy follows the variable."""
    raw = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    code = _uncommented(raw)
    assert 'name               = "gha-${var.unique_name_prefix}-dev-deploy"' in code
    assert 'name               = "gha-${var.unique_name_prefix}-prod-deploy"' in code
    scopes = re.findall(r'"(arn:aws:[^"]*\$\{var\.unique_name_prefix\}[^"]*)"', code)
    assert len(scopes) >= 20
    for kind in (
        "table/", "function:", "log-group:/aws/lambda/", "log-group:/aws/apigateway/", "stateMachine:",
        "schedule/default/", "alarm:", "dashboard/", "log-group:aws-waf-logs-", ":role/",
    ):  # fmt: skip
        assert any(f"{kind}${{var.unique_name_prefix}}-" in scope for scope in scopes), kind
    # With the default they are, to the character, the scopes the live roles have.
    for expected in (
        "arn:aws:dynamodb:${var.aws_region}:*:table/bloggerbear-*",
        "arn:aws:lambda:${var.aws_region}:*:function:bloggerbear-*",
        "arn:aws:iam::*:role/bloggerbear-*-lambda-exec",
        "arn:aws:scheduler:${var.aws_region}:*:schedule/default/bloggerbear-*",
        "arn:aws:logs:us-east-1:*:log-group:aws-waf-logs-bloggerbear-*:*",
    ):
        assert f'"{expected}"' in with_default_prefix(code), expected


def _name_literals(text: str, attribute: str) -> list[str]:
    return re.findall(rf'^\s*{attribute}\s*=\s*"([^"]+)"', _uncommented(text), re.M)


def test_the_longest_names_still_fit_with_the_longest_prefix():
    """The variable's description says which name sets the 14-character limit and how much room
    the others have. This works the same sums from the files, with a prefix of that length in
    production (the longer environment name), so a longer resource name added later fails here
    instead of at somebody's first apply."""
    assert re.fullmatch(r"[a-z]([a-z0-9-]{0,12}[a-z0-9])?", _LONGEST_PREFIX)
    assert not re.fullmatch(r"[a-z]([a-z0-9-]{0,12}[a-z0-9])?", _LONGEST_PREFIX + "x")

    def longest(names: list[str]) -> str:
        filled = [
            name.replace(PREFIX_REFERENCE, _LONGEST_PREFIX).replace("${var.environment_name}", "production")
            for name in names
        ]
        assert filled and all("${" not in name for name in filled), filled
        return max(filled, key=len)

    main = (INFRA / "environments" / "production" / "main.tf").read_text(encoding="utf-8")
    # Lambda function names: 64.
    functions = _name_literals(main, "function_name")
    assert len(functions) >= 11 and len(longest(functions)) <= 64, longest(functions)
    assert longest(functions) == f"{_LONGEST_PREFIX}-production-cost-explorer-poll"  # 44, as described
    # S3 bucket names: 63.
    site = _read_raw("modules", "static-site", "main.tf")
    buckets = [*_name_literals(main, "bucket"), *_name_literals(site, "bucket")]
    buckets = [name for name in buckets if PREFIX_REFERENCE in name]
    assert len(buckets) == 2 and len(longest(buckets)) <= 63
    # The Cognito sign-in host's first label: 63.
    assert len(longest(_name_literals(main, "hosted_ui_domain_prefix"))) <= 63

    # IAM role names: 64. The roots' own, and the assistant module's, whose names hang off two
    # locals (the MCP server's and the agent's).
    roles = [
        re.search(r'^\s*name\s*=\s*"([^"]+)"', body, re.M).group(1)
        for body in re.findall(r'^resource "aws_iam_role" "\w+" \{\n(.*?)^\}', main, re.S | re.M)
    ]
    assistant = sorted((INFRA / "modules" / "ops-assistant").glob("*.tf"))
    module = "\n".join(path.read_text(encoding="utf-8") for path in assistant)
    mcp_name = re.search(r'^  name = "(\$\{var\.unique_name_prefix\}[^"]+)"$', module, re.M)
    local = {
        "${local.name}": mcp_name.group(1),
        "${local.agent_name}": re.search(r'^  agent_name\s*=\s*"([^"]+)"$', module, re.M).group(1),
    }
    for body in re.findall(r'^resource "aws_iam_role" "\w+" \{\n(.*?)^\}', module, re.S | re.M):
        name = re.search(r'^\s*name\s*=\s*"([^"]+)"', body, re.M).group(1)
        for reference, value in local.items():
            name = name.replace(reference, value)
        roles.append(name)
    # ...and the web search gateway's, which is handed "<prefix>-<env>" as var.name.
    gateway = _read_raw("modules", "web-search", "main.tf")
    roles += [
        name.replace("${var.name}", f"{PREFIX_REFERENCE}-production")
        for name in re.findall(r'^resource "aws_iam_role" "\w+" \{\n\s*name\s*=\s*"([^"]+)"', gateway, re.M)
    ]
    assert len(roles) >= 7 and len(longest(roles)) <= 64, longest(roles)
    assert longest(roles) == f"{_LONGEST_PREFIX}-production-ops-mcp-scheduler-invoke"  # 50, as described
    # The deploy roles themselves.
    bootstrap = (INFRA / "bootstrap" / "main.tf").read_text(encoding="utf-8")
    assert "gha-${var.unique_name_prefix}-prod-deploy" in bootstrap
    assert len(f"gha-{_LONGEST_PREFIX}-prod-deploy") <= 64

    # EventBridge Scheduler: 64. The fixed schedules Terraform makes...
    schedules = [
        re.search(r'^\s*name\s*=\s*"([^"]+)"', body, re.M).group(1)
        for body in re.findall(r'^resource "aws_scheduler_schedule" "\w+" \{\n(.*?)^\}', main, re.S | re.M)
    ]
    assert len(schedules) >= 4 and len(longest(schedules)) <= 64
    # ...and the one that sets the limit: a topic's own, made at run time by common/scheduler.py
    # as <prefix>-<env>-<topic_id>-research-tick. 26 characters are fixed, so the longest prefix
    # leaves a topic id 24, which is the longest id the project's own examples use.
    scheduler = (ROOT / "lambdas" / "common" / "scheduler.py").read_text(encoding="utf-8")
    built = '''f"{environment_prefix(os.environ['ENVIRONMENT_NAME'])}{topic_id}-{suffix}"'''
    assert f"return {built}" in scheduler
    assert '"research-tick"' in scheduler and '"daily-cycle"' in scheduler
    fixed = len("-production-") + len("-research-tick")
    assert fixed == 26
    longest_example_topic = "finance-crypto-investing"
    assert longest_example_topic in (ROOT / "scripts" / "README.md").read_text(encoding="utf-8") or any(
        longest_example_topic in path.read_text(encoding="utf-8") for path in (ROOT / "docs").rglob("*.md")
    )
    assert len(_LONGEST_PREFIX) + fixed + len(longest_example_topic) == 64
    assert 64 - fixed - len(DEFAULT_PREFIX) == 27  # what the default leaves, as the description says


def _read_raw(*parts: str) -> str:
    """An infra/ file as written, with the variable still in it."""
    return INFRA.joinpath(*parts).read_text(encoding="utf-8")


def _string_constants(path: Path) -> list[tuple[int, str]]:
    """Every string in a Python file that is not a docstring, f-string pieces included."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings
    ]


def test_no_lambda_code_writes_the_prefix_out():
    """The Python half of the same rule. Any string that looks like a resource name or a parameter
    path starting with the original deployment's prefix is a name that would be wrong in every
    other deployment. What is left is not a resource name: the user-agent strings, the MCP server's
    own name and the Cognito scope."""
    allowed = {
        "bloggerbear-ops-agent",  # ops_agent/agent.py: a user-agent suffix and an application name
        "bloggerbear-ops",  # ops_mcp/server.py: the MCP server's name (and the Cognito scope's)
    }
    lambdas = ROOT / "lambdas"
    files = [
        path
        for path in sorted(lambdas.rglob("*.py"))
        if "tests" not in path.parts and "lambda-build" not in path.parts
    ]
    assert len(files) > 40
    for path in files:
        for line, value in _string_constants(path):
            if value in allowed:
                continue
            where = f"{path.relative_to(ROOT)}:{line}: {value!r}"
            assert not re.search(r"bloggerbear-|/bloggerbear/", value), where
    naming = (lambdas / "common" / "naming.py").read_text(encoding="utf-8")
    assert 'DEFAULT_NAME_PREFIX = "bloggerbear"' in naming  # the one place the default is written
