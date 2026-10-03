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

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
NODE = shutil.which("node")
DOMAIN = "bloggerbear.com"


def _read(*parts: str) -> str:
    return (INFRA.joinpath(*parts)).read_text(encoding="utf-8")


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

    assert tables.count('resource "aws_dynamodb_table"') == 13
    assert tables.count("deletion_protection_enabled = var.protect_data") == 13
    assert (
        len(re.findall(r"^\s+enabled\s*=\s*var\.protect_data", tables, re.M)) == 13
    )  # point-in-time recovery
    assert re.search(r"protect_data\s*=\s*true", _read("environments", "production", "main.tf"))
    assert "protect_data" not in _read("environments", "dev", "main.tf")


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
    PromptRefinements and FailedExecutions are the four this PR adds."""
    tables = _read("modules", "app-data", "main.tf")

    assert tables.count('attribute_name = "expires_at"') == 6
    assert tables.count("ttl {") == 6


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
    assert len(function_names) == 10  # module.observability's list: each function named once
    assert len(set(function_names)) == 10


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
    for match in re.finditer(r'"arn:aws:logs:([a-z0-9-]+):\*:log-group:([^"]*?)(?::\*)?"', bootstrap):
        patterns.add((match.group(1), match.group(2)))
    return patterns


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_deploy_role_may_create_every_log_group_an_environment_declares(env):
    """A log group in another region needs its own ARN in the deploy role's policy. Production's shared
    CloudFront WAF logs to us-east-1; only ap-southeast-2 was allowed, so its first apply was refused
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
            "us-east-1" if re.search(r"^\s*provider\s*=\s*aws\.us_east_1", body, re.M) else "ap-southeast-2"
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
    ("table", "fixture_name"), [("articles", "Articles"), ("moderation_queue", "ModerationQueue")]
)
def test_the_test_fixtures_create_exactly_the_indexes_terraform_does(table, fixture_name):
    """moto only knows the indexes a fixture creates, so a fixture that drifted from Terraform would
    let a Query on a missing (or differently keyed) index pass here and fail in AWS."""
    from table_schemas import INDEXES

    assert sorted(_table_indexes(table)) == sorted(INDEXES[fixture_name])


@pytest.mark.parametrize("table", ["articles", "moderation_queue"])
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
