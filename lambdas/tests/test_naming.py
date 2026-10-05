"""The deployment's name prefix (common/naming.py) and everything built from it.

Every resource is "<prefix>-<env>-<resource>". The prefix is NAME_PREFIX, which Terraform sets on
every function from var.unique_name_prefix, and "bloggerbear" when it is not set.

It is read once, when common.naming is imported, so "another prefix" cannot be tried by setting
the variable inside this process: the modules are long since imported, and reloading them would
hand every other test file stale copies of the catalogue. Each such check runs in a fresh
interpreter instead, with NAME_PREFIX set, which is also exactly how a Lambda meets it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import common.naming as naming
import common.scheduler as scheduler
from ops_mcp import account, architecture, firewall, runsheets, suggestions

LAMBDAS = Path(__file__).resolve().parents[1]
OTHER = "acme-blog"  # a hyphen in it, on purpose: the prefix is not always one word


def _in_a_deployment_named(prefix: str | None, program: str, **environment: str) -> dict:
    """Run `program` in a new interpreter, as a function in a deployment with this prefix would
    start, and return the JSON it prints. None leaves NAME_PREFIX unset."""
    env = {key: value for key, value in os.environ.items() if key != naming.NAME_PREFIX_ENV}
    env.update(
        PYTHONPATH=str(LAMBDAS),
        AWS_DEFAULT_REGION="ap-southeast-2",
        AWS_ACCESS_KEY_ID="testing",
        AWS_SECRET_ACCESS_KEY="testing",
        **environment,
    )
    if prefix is not None:
        env[naming.NAME_PREFIX_ENV] = prefix
    done = subprocess.run(
        [sys.executable, "-c", program], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


# --- the default -----------------------------------------------------------------------------------


def test_unset_it_is_the_original_deployments_prefix_with_no_trailing_hyphen():
    assert naming.NAME_PREFIX_ENV == "NAME_PREFIX"
    assert naming.DEFAULT_NAME_PREFIX == "bloggerbear"
    assert naming.NAME_PREFIX == "bloggerbear"  # nothing sets it in the test run
    assert naming.environment_prefix("dev") == "bloggerbear-dev-"
    assert naming.environment_prefix("production") == "bloggerbear-production-"


@pytest.mark.parametrize("value", [None, ""])
def test_unset_or_empty_both_mean_the_default(value):
    program = "import json, common.naming as n; print(json.dumps({'prefix': n.NAME_PREFIX}))"
    assert _in_a_deployment_named(value, program) == {"prefix": "bloggerbear"}


def test_with_the_default_every_name_is_what_it_has_always_been(monkeypatch):
    """The names the live deployment has. A character's difference here is a resource the code
    looks for and does not find."""
    monkeypatch.setenv("ENVIRONMENT_NAME", "dev")
    name = scheduler._schedule_name
    assert name("hacker-news", "research-tick") == "bloggerbear-dev-hacker-news-research-tick"
    assert name("hacker-news", "daily-cycle") == "bloggerbear-dev-hacker-news-daily-cycle"
    assert architecture.PREFIX == "bloggerbear-" and account.ALARM_PREFIX == "bloggerbear-"
    assert account.alarm_prefix() == "bloggerbear-dev-"
    assert firewall.SHARED_LOG_GROUP == "aws-waf-logs-bloggerbear-shared"
    assert architecture.SHARED_WAF_LOG_GROUP == "aws-waf-logs-bloggerbear-shared"
    assert architecture.log_group_region("aws-waf-logs-bloggerbear-shared") == "us-east-1"
    names = {architecture.fill(component.name, "dev") for component in architecture.CATALOGUE}
    for expected in (
        "bloggerbear-dev-topics",
        "bloggerbear-dev-research-tick",
        "bloggerbear-dev-pipeline-dlq",
        "bloggerbear-dev-alerts",
        "bloggerbear-dev-content",
        "/aws/apigateway/bloggerbear-dev-public-api-access",
        "aws-waf-logs-bloggerbear-dev-admin",
        "aws-waf-logs-bloggerbear-shared",
    ):
        assert expected in names, expected
    assert runsheets._ALARMS.where == "CloudWatch console > Alarms > filter bloggerbear-{env}-"
    assert "(bloggerbear-<env>-edge)" in suggestions.CATALOGUE["security_incident"].action
    assert "(bloggerbear-<env>-pipeline)" in suggestions.CATALOGUE["alarm_firing"].action


# --- another prefix --------------------------------------------------------------------------------

_SCHEDULES = """
import json
import common.scheduler as scheduler
print(json.dumps({
    "research": scheduler._schedule_name("hacker-news", "research-tick"),
    "daily": scheduler._schedule_name("hacker-news", "daily-cycle"),
}))
"""


def test_another_prefix_names_the_per_topic_schedules():
    names = _in_a_deployment_named(OTHER, _SCHEDULES, ENVIRONMENT_NAME="production")
    assert names == {
        "research": "acme-blog-production-hacker-news-research-tick",
        "daily": "acme-blog-production-hacker-news-daily-cycle",
    }


def test_the_schedules_are_created_under_the_prefix_the_scheduler_role_is_scoped_to():
    """End to end against moto: the schedules the admin API makes for a topic carry the prefix,
    which is what the Lambda role's and the deploy role's schedule/default/<prefix>-<env>-* scopes
    allow."""
    program = """
import json
import boto3
from moto import mock_aws
import common.scheduler as scheduler

with mock_aws():
    scheduler.upsert_topic_schedules("hacker-news", "rate(6 hours)", "cron(0 9 * * ? *)", "Australia/Sydney")
    found = boto3.client("scheduler").list_schedules()["Schedules"]
    print(json.dumps(sorted(schedule["Name"] for schedule in found)))
"""
    account_id = "111111111111"
    names = _in_a_deployment_named(
        OTHER,
        program,
        ENVIRONMENT_NAME="dev",
        RESEARCH_TICK_FUNCTION_ARN=f"arn:aws:lambda:ap-southeast-2:{account_id}:function:acme-blog-dev-research-tick",
        STATE_MACHINE_ARN=f"arn:aws:states:ap-southeast-2:{account_id}:stateMachine:acme-blog-dev-daily-cycle",
        SCHEDULER_INVOKE_ROLE_ARN=f"arn:aws:iam::{account_id}:role/acme-blog-dev-scheduler-invoke",
    )
    assert names == ["acme-blog-dev-hacker-news-daily-cycle", "acme-blog-dev-hacker-news-research-tick"]


_CATALOGUE = """
import json
from ops_mcp import account, architecture, firewall, runsheets, suggestions

def resolved(text):
    found = architecture.resolve(text)
    return {
        "keys": sorted(f"{c.kind}:{c.key}" for c in found.matches),
        "asked_env": found.asked_env,
        "env_word": found.env_word,
    }

print(json.dumps({
    "prefix": architecture.PREFIX,
    "names": sorted(architecture.fill(c.name, "dev") for c in architecture.CATALOGUE),
    "everything": json.dumps([
        [c.name, c.log_groups, c.dashboards, c.alarms, c.details] for c in architecture.CATALOGUE
    ]),
    "alarm_prefix": account.alarm_prefix(),
    "alarm_label": account._alarm_label("acme-blog-dev-pipeline-dlq-messages"),
    "shared_log_group": firewall.SHARED_LOG_GROUP,
    "log_groups": firewall.log_groups(),
    "shared_region": architecture.log_group_region(firewall.SHARED_LOG_GROUP),
    "group_label": firewall._group_label("aws-waf-logs-acme-blog-dev-public-api"),
    "alarms_step": runsheets._ALARMS.where,
    "suggestion": suggestions.CATALOGUE["security_incident"].action,
    "full_name": resolved("acme-blog-dev-candidate-ideas"),
    "other_environment": resolved("acme-blog-prod-candidate-ideas"),
    "camel_case": resolved("Acme-Blog-Dev-CandidateIdeas"),
    "arn": resolved("arn:aws:dynamodb:ap-southeast-2:111111111111:table/acme-blog-dev-topics"),
    "log_group": resolved("/aws/lambda/acme-blog-dev-research-tick"),
    "waf_log_group": resolved("aws-waf-logs-acme-blog-dev-admin"),
    "not_an_environment": resolved("acme-blog-staging-topics"),
    "short_name": resolved("candidate ideas"),
    "the_original_deployments_name": resolved("bloggerbear-dev-candidate-ideas"),
    "spoken": architecture.architecture("candidate ideas")["spoken"],
}))
"""


@pytest.fixture(scope="module")
def other() -> dict:
    return _in_a_deployment_named(
        OTHER,
        _CATALOGUE,
        ENVIRONMENT_NAME="dev",
        OPS_WAF_LOG_GROUPS=(
            "ap-southeast-2:aws-waf-logs-acme-blog-dev-admin,us-east-1:aws-waf-logs-acme-blog-shared"
        ),
        OPS_ACCOUNT_WIDE_DATA="true",
    )


def test_another_prefix_renames_the_whole_catalogue(other):
    """The assistant learns what things are called from configuration: with another prefix, no
    name it knows, in any field, is the original deployment's."""
    assert other["prefix"] == "acme-blog-"
    assert "bloggerbear" not in other["everything"]
    for expected in (
        "acme-blog-dev-topics",
        "acme-blog-dev-research-tick",
        "acme-blog-dev-pipeline-dlq",
        "acme-blog-dev-content",
        "/aws/apigateway/acme-blog-dev-public-api-access",
        "aws-waf-logs-acme-blog-dev-admin",
        "aws-waf-logs-acme-blog-shared",
    ):
        assert expected in other["names"], expected
    # The same catalogue, name for name, as the default's: only the prefix differs.
    default = sorted(architecture.fill(component.name, "dev") for component in architecture.CATALOGUE)
    assert sorted(name.replace("acme-blog-", "bloggerbear-") for name in other["names"]) == default


def test_another_prefix_is_what_the_alarms_and_firewall_tools_ask_for(other):
    assert other["alarm_prefix"] == "acme-blog-dev-"
    assert other["alarm_label"] == "dev pipeline dlq messages"
    assert other["shared_log_group"] == "aws-waf-logs-acme-blog-shared"
    assert other["shared_region"] == "us-east-1"
    # This deployment's own log groups are accepted...
    assert other["log_groups"] == [
        ["ap-southeast-2", "aws-waf-logs-acme-blog-dev-admin"],
        ["us-east-1", "aws-waf-logs-acme-blog-shared"],
    ]
    assert other["group_label"] != "aws-waf-logs-acme-blog-dev-public-api"  # the prefix came off
    assert other["alarms_step"] == "CloudWatch console > Alarms > filter acme-blog-{env}-"
    assert "(acme-blog-<env>-edge)" in other["suggestion"]


def test_the_original_deployments_log_groups_are_refused_under_another_prefix():
    """...and the original deployment's are not: a firewall log group that is not this
    deployment's turns the tool off, as a log group of another environment's does."""
    program = (
        "import json; from ops_mcp import firewall; print(json.dumps({'groups': firewall.log_groups()}))"
    )
    found = _in_a_deployment_named(
        OTHER,
        program,
        ENVIRONMENT_NAME="dev",
        OPS_WAF_LOG_GROUPS="ap-southeast-2:aws-waf-logs-bloggerbear-dev-admin",
    )
    assert found == {"groups": None}


def test_a_pasted_name_is_recognised_by_this_deployments_prefix(other):
    table = ["table:candidate-ideas"]
    # The prefix (two words here) comes off, then the environment, which is noted as it always was.
    assert other["full_name"] == {"keys": table, "asked_env": "dev", "env_word": "dev"}
    assert other["short_name"] == {"keys": table, "asked_env": None, "env_word": None}
    assert other["camel_case"]["keys"] == table
    # The other environment's name is still answered for this one, and says whose it was.
    assert other["other_environment"] == {"keys": table, "asked_env": "production", "env_word": "prod"}
    assert other["arn"]["keys"] == ["table:topics"]
    assert other["log_group"]["keys"] == ["function:research-tick"]
    assert other["waf_log_group"]["keys"] == ["log_group:waf-admin"]
    # A word in the environment's place that is not one is noticed only after the prefix.
    staging = {"keys": ["table:topics"], "asked_env": "staging", "env_word": "staging"}
    assert other["not_an_environment"] == staging
    # Another deployment's prefix is not this one's: its name is not taken for one of ours.
    assert other["the_original_deployments_name"]["keys"] == []
    # And what the operator is told is this deployment's name for it.
    assert other["spoken"].startswith("acme-blog-dev-candidate-ideas is the DynamoDB table")


def test_the_default_recognises_the_same_names_it_always_did():
    """The same questions with nothing set, asked in this process."""
    table = architecture.by_key("table", "candidate-ideas")
    for text in ("bloggerbear-dev-candidate-ideas", "BloggerBear-Dev-CandidateIdeas", "candidate ideas"):
        assert architecture.resolve(text).matches == (table,), text
    other_env = architecture.resolve("bloggerbear-prod-candidate-ideas")
    assert other_env.matches == (table,) and other_env.asked_env == "production"
    staging = architecture.resolve("bloggerbear-staging-topics")
    assert staging.asked_env == "staging" and staging.matches == (architecture.by_key("table", "topics"),)
    # A name under some other deployment's prefix is not one of this deployment's.
    assert architecture.resolve("acme-blog-dev-candidate-ideas").matches == ()
