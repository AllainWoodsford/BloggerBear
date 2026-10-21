"""scripts/admin_allowlist.py: the admin API's allowlist, stored in SSM and copied to what enforces it.

Against fakes of the four clients, which keep what is written, so a test reads back what the WAF
IP set, the resource policy and the stage were left holding.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import admin_allowlist as al

ACCOUNT = "123456789012"
REGION = "ap-southeast-2"
API_ID = "abc123"
HOME = "203.0.113.7/32"


class ParameterNotFound(Exception):
    pass


class FakeSsm:
    class exceptions:
        ParameterNotFound = ParameterNotFound

    def __init__(self, value=None):
        self.value = value
        self.writes = []

    def get_parameter(self, Name, WithDecryption):
        assert WithDecryption
        if self.value is None:
            raise ParameterNotFound(Name)
        return {"Parameter": {"Name": Name, "Value": self.value}}

    def put_parameter(self, **kwargs):
        assert kwargs["Type"] == "SecureString" and kwargs["Overwrite"]
        self.writes.append(kwargs)
        self.value = kwargs["Value"]


class FakeWaf:
    def __init__(self, addresses=None):
        self.sets = {} if addresses is None else {"bloggerbear-dev-admin-allowlist": list(addresses)}
        self.updates = []

    def list_ip_sets(self, Scope, NextMarker=None):
        assert Scope == "REGIONAL"
        if NextMarker is None:  # a first page of somebody else's set, as a real listing pages
            return {"IPSets": [{"Name": "other", "Id": "o"}], "NextMarker": "m"}
        return {"IPSets": [{"Name": name, "Id": "id-" + name} for name in self.sets]}

    def get_ip_set(self, Name, Scope, Id):
        return {"IPSet": {"Addresses": list(self.sets[Name])}, "LockToken": "lock"}

    def update_ip_set(self, Name, Scope, Id, Addresses, LockToken):
        assert LockToken == "lock" and Id == "id-" + Name
        self.updates.append(list(Addresses))
        self.sets[Name] = list(Addresses)


class FakeApiGateway:
    def __init__(self, policy=None):
        self.policy = policy
        self.deployments = []

    def get_rest_apis(self, limit, position=None):
        if position is None:
            return {"items": [{"id": "pub", "name": "bloggerbear-dev-public-api"}], "position": "p"}
        api = {"id": API_ID, "name": "bloggerbear-dev-admin-api"}
        if self.policy is not None:
            # API Gateway hands a policy back with its quotes escaped.
            api["policy"] = self.policy.replace('"', '\\"')
        return {"items": [api]}

    def update_rest_api(self, restApiId, patchOperations):
        assert restApiId == API_ID
        (operation,) = patchOperations
        assert operation["op"] == "replace" and operation["path"] == "/policy"
        self.policy = operation["value"]

    def create_deployment(self, restApiId, stageName, description):
        self.deployments.append((restApiId, stageName, self.policy))
        return {"id": f"d{len(self.deployments)}"}


class FakeSts:
    def get_caller_identity(self):
        return {"Account": ACCOUNT}


def fakes(*, stored=None, ip_set=(), policy=None):
    return al.Aws(FakeSsm(stored), FakeWaf(ip_set), FakeApiGateway(policy), FakeSts())


def admitted(aws):
    return al.policy_cidrs(aws.apigateway.policy)


# --- the checks ----------------------------------------------------------------------------------


def test_ranges_are_checked_kept_in_order_and_not_repeated():
    given = ["203.0.113.7", "198.51.100.0/28,203.0.113.7/32", " "]
    assert al.check_cidrs(given) == ["203.0.113.7/32", "198.51.100.0/28"]


@pytest.mark.parametrize(
    ("given", "said"),
    [
        ([], "at least one"),
        ([" , "], "at least one"),
        (["home"], "is not a range"),
        (["203.0.113.7/24"], "is not a range"),  # not the first address of its range
        (["2001:db8::/32"], "IPv6"),
        (["0.0.0.0/0"], "every address"),
        (["10.0.0.0/8"], "--allow-wide"),
    ],
)
def test_anything_else_is_refused(given, said):
    with pytest.raises(al.Refused, match=said):
        al.check_cidrs(given)


def test_a_wide_range_needs_saying_so():
    assert al.check_cidrs(["10.0.0.0/8"], allow_wide=True) == ["10.0.0.0/8"]


def test_my_ip_is_aws_echo_answer_as_a_32():
    def opener(url, timeout):
        assert url == al.MY_IP_URL
        return io.BytesIO(b"203.0.113.7\n")

    assert al.my_ip(opener) == HOME
    with pytest.raises(al.Refused):
        al.my_ip(lambda url, timeout: io.BytesIO(b"<html>"))


# --- the policy ----------------------------------------------------------------------------------


def test_the_policy_denies_every_call_from_outside_the_list_and_reads_back():
    arn = f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{API_ID}"
    policy = json.loads(al.resource_policy([HOME], arn))
    (statement,) = policy["Statement"]
    assert statement["Effect"] == "Deny" and statement["Resource"] == f"{arn}/*"
    assert statement["Condition"] == {"NotIpAddress": {"aws:SourceIp": [HOME]}}
    assert al.policy_cidrs(al.resource_policy([HOME], arn)) == [HOME]
    # With no addresses, Terraform's starting policy: a Deny of everything.
    assert "Condition" not in json.loads(al.resource_policy([], arn))["Statement"][0]
    assert al.policy_cidrs(al.resource_policy([], arn)) == []


def test_the_policy_terraform_used_to_write_reads_back_too():
    # The shape the variable-built policy had: one address, as a string, with API Gateway's escaping.
    old = (
        '{\\"Version\\":\\"2012-10-17\\",\\"Statement\\":[{\\"Sid\\":\\"DenyOutsideAdminAllowlist\\",'
        '\\"Effect\\":\\"Deny\\",\\"Principal\\":\\"*\\",\\"Action\\":\\"execute-api:Invoke\\",'
        '\\"Resource\\":\\"arn:aws:execute-api:ap-southeast-2:123456789012:abc123\\/*\\",'
        '\\"Condition\\":{\\"NotIpAddress\\":{\\"aws:SourceIp\\":\\"198.51.100.9\\/32\\"}}}]}'
    )
    assert al.policy_cidrs(old) == ["198.51.100.9/32"]
    assert al.policy_cidrs(None) is None and al.policy_cidrs('{"Statement": []}') is None


# --- the commands --------------------------------------------------------------------------------


def test_set_stores_the_list_then_puts_it_everywhere_and_deploys(capsys):
    aws = fakes(stored="198.51.100.9/32", ip_set=["198.51.100.9/32"])

    assert al.main(["set", "--env", "dev", HOME], aws=aws) == 0

    assert aws.ssm.value == HOME
    assert aws.ssm.writes[0]["Name"] == "/bloggerbear/dev/admin-allowed-cidrs"
    assert aws.wafv2.sets["bloggerbear-dev-admin-allowlist"] == [HOME]
    assert admitted(aws) == [HOME]
    # Deployed after the policy was written, so the stage enforces the new one.
    assert aws.apigateway.deployments == [(API_ID, "dev", aws.apigateway.policy)]
    assert HOME in capsys.readouterr().out


def test_set_with_the_web_acl_switched_off_still_sets_the_policy(capsys):
    aws = fakes(ip_set=None)

    assert al.main(["set", "--env", "dev", HOME], aws=aws) == 0

    assert admitted(aws) == [HOME] and len(aws.apigateway.deployments) == 1
    assert "switched off" in capsys.readouterr().out


def test_a_refused_range_writes_nothing(capsys):
    aws = fakes(stored="198.51.100.9/32", ip_set=["198.51.100.9/32"])

    assert al.main(["set", "--env", "dev", "0.0.0.0/0"], aws=aws) == 2

    assert aws.ssm.writes == [] and aws.wafv2.updates == [] and aws.apigateway.deployments == []
    assert "refused" in capsys.readouterr().err


def test_set_my_ip_looks_the_address_up(monkeypatch):
    monkeypatch.setattr(al, "my_ip", lambda: HOME)
    aws = fakes()

    assert al.main(["set", "--env", "dev", "--my-ip"], aws=aws) == 0
    assert aws.ssm.value == HOME and admitted(aws) == [HOME]


def test_sync_copies_the_stored_list_and_refuses_when_there_is_none(capsys):
    aws = fakes(stored=f"{HOME},198.51.100.0/28")
    assert al.main(["sync", "--env", "dev"], aws=aws) == 0
    assert admitted(aws) == [HOME, "198.51.100.0/28"]
    assert aws.ssm.writes == []  # sync never writes the parameter

    empty = fakes()
    assert al.main(["sync", "--env", "dev"], aws=empty) == 2
    assert empty.apigateway.deployments == [] and "not set" in capsys.readouterr().err


def test_the_environment_and_prefix_name_everything():
    aws = fakes()
    api = {"id": API_ID, "name": "acme-production-admin-api"}
    aws.apigateway.get_rest_apis = lambda limit, position=None: {"items": [api]}
    aws.wafv2.sets = {"acme-production-admin-allowlist": []}

    assert al.main(["--prefix", "acme", "set", "--env", "production", HOME], aws=aws) == 0
    assert aws.ssm.writes[0]["Name"] == "/acme/production/admin-allowed-cidrs"
    assert aws.wafv2.sets["acme-production-admin-allowlist"] == [HOME]
    assert aws.apigateway.deployments[0][1] == "production"


def test_a_missing_api_is_refused():
    aws = fakes(stored=HOME)
    aws.apigateway.get_rest_apis = lambda limit, position=None: {"items": []}
    assert al.main(["sync", "--env", "dev"], aws=aws) == 2


def test_show_says_whether_the_three_agree(capsys):
    agreeing = fakes(stored=HOME, ip_set=[HOME])
    al.main(["set", "--env", "dev", HOME], aws=agreeing)
    capsys.readouterr()
    assert al.main(["show", "--env", "dev", "--no-lookup"], aws=agreeing) == 0
    assert "in agreement" in capsys.readouterr().out

    # Terraform's starting state: the parameter set, the IP set empty, the policy denying all.
    locked = al.resource_policy([], f"arn:aws:execute-api:{REGION}:{ACCOUNT}:{API_ID}")
    drifted = fakes(stored=HOME, ip_set=[], policy=locked)
    assert al.main(["show", "--env", "dev", "--no-lookup"], aws=drifted) == 1
    out = capsys.readouterr().out
    assert "NOT in agreement" in out and "denies everything" in out
    assert drifted.ssm.writes == [] and drifted.wafv2.updates == [] and drifted.apigateway.deployments == []


# --- what the script and Terraform must agree on -------------------------------------------------


@pytest.mark.parametrize("env", al.ENVIRONMENTS)
def test_terraform_makes_both_places_locked_and_leaves_them_to_the_script(env):
    root = Path(__file__).resolve().parents[2] / "infra"
    main = (root / "environments" / env / "main.tf").read_text(encoding="utf-8")
    ip_set = main.split('resource "aws_wafv2_ip_set" "admin_allowlist" {')[1].split("\n}\n")[0]
    # IPv4, which is why check_cidrs refuses IPv6; created empty, and its addresses then ignored.
    assert 'ip_address_version = "IPV4"' in ip_set
    assert "addresses = []" in ip_set and "ignore_changes = [addresses]" in ip_set
    assert f'"${{var.unique_name_prefix}}-{env}-admin-allowlist"' in ip_set
    # The parameter's name, as the script builds it, is the one the assistant is told to read.
    assert f'name = "/${{var.unique_name_prefix}}/{env}/admin-allowed-cidrs"' in main
    assert al.parameter_name("bloggerbear", env) == f"/bloggerbear/{env}/admin-allowed-cidrs"
    assert "admin_allowed_cidrs" not in main  # no address reaches Terraform any more
    policy = (root / "modules" / "rest-api" / "main.tf").read_text(encoding="utf-8")
    policy = policy.split('resource "aws_api_gateway_rest_api_policy" "this" {')[1].split("\n}\n")[0]
    assert "ignore_changes = [policy]" in policy
