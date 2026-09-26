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
    """One for_each block, reusing the same function_name list module.observability's
    lambda_function_names already uses, not one resource (or one list) per function -- see
    that resource's own comment on why."""
    text = _read("environments", env, "main.tf")

    assert text.count("retention_in_days = 90") == 1
    assert 'resource "aws_cloudwatch_log_group" "lambda"' in text
    assert "for_each          = toset(local.pipeline_lambda_function_names)" in text
    function_names = re.findall(r"aws_lambda_function\.[a-z_]+\.function_name,", text)
    assert len(function_names) == 10  # named once each, in the one list both resources share
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
