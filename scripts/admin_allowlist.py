#!/usr/bin/env python3
"""The admin API's address allowlist: one SSM parameter per environment, and what enforces it.

    python scripts/admin_allowlist.py show --env dev
    python scripts/admin_allowlist.py set --env dev --my-ip
    python scripts/admin_allowlist.py set --env production 203.0.113.7/32 198.51.100.0/28
    python scripts/admin_allowlist.py sync --env dev

The operator's addresses are not Terraform's. They live in one SecureString parameter,
`/<prefix>/<env>/admin-allowed-cidrs` (comma-separated IPv4 CIDRs), and this script is the only
thing that writes it. `set` writes it and then does what `sync` does: copies the list into the two
places that enforce it on the admin API, both of which Terraform creates locked and then leaves
alone (infra/environments/*/main.tf):

- the WAF IP set `<prefix>-<env>-admin-allowlist`, when the admin web ACL exists (the
  WAF_ADMIN_ALLOWLIST switch, docs/cost-saving.md); it applies at once;
- the API's resource policy, a Deny of every call from outside the list, which API Gateway enforces
  only from the next deployment, so `sync` also deploys the stage.

The operator's assistant reads the same parameter when a request meets its `allowlist` setting
(lambdas/ops_mcp/access.py), so it needs nothing from here. So an operator whose address changed
runs `set --my-ip`, with no apply and no GitHub secret, and no address is ever in git or a plan.

`show` only reads, and says whether the three agree. The credentials are the operator's own (the
admin API is reached from the same account): `ssm:GetParameter`/`PutParameter`, `wafv2:ListIPSets`,
`GetIPSet`, `UpdateIPSet`, `apigateway:GET`/`PATCH`/`POST` and `sts:GetCallerIdentity`. Names are
built from `--prefix` and `--env` as the Terraform roots build them; the region is the home one.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import urllib.request
from dataclasses import dataclass

from setup_repo import DEFAULT_PREFIX, DEFAULT_REGION

ENVIRONMENTS = ("dev", "production")
MY_IP_URL = "https://checkip.amazonaws.com"
# Wider than this needs --allow-wide: a /15 is 131,072 addresses, nobody's home connection.
NARROWEST_WIDE_PREFIX = 16


class Refused(Exception):
    """Said to the operator, and nothing written."""


def parameter_name(prefix: str, env: str) -> str:
    return f"/{prefix}/{env}/admin-allowed-cidrs"


def ip_set_name(prefix: str, env: str) -> str:
    return f"{prefix}-{env}-admin-allowlist"


def api_name(prefix: str, env: str) -> str:
    return f"{prefix}-{env}-admin-api"


def check_cidrs(values: list[str], *, allow_wide: bool = False) -> list[str]:
    """The ranges as the parameter holds them, in order and without repeats; refuses anything else.

    IPv4 only: the WAF IP set is IPV4 (an IPv6 entry fails the update). A bare address is its /32.
    A range must start at its first address (203.0.113.7/24 is a typo for one of two things). /0 is
    refused outright, and anything wider than /16 without --allow-wide."""
    if not values:
        raise Refused("give at least one range, such as 203.0.113.7/32, or --my-ip")
    ranges: list[str] = []
    for value in values:
        for part in value.split(","):
            if not part.strip():
                continue
            try:
                network = ipaddress.ip_network(part.strip(), strict=True)
            except ValueError:
                raise Refused(
                    f"{part.strip()!r} is not a range: use an address and a size, such as "
                    "203.0.113.7/32, where the address is the first one in its range"
                ) from None
            if network.version != 4:
                raise Refused(f"{network} is IPv6: the admin allowlist takes IPv4 ranges only")
            if network.prefixlen == 0:
                raise Refused("0.0.0.0/0 is every address on the internet; give your own")
            if network.prefixlen < NARROWEST_WIDE_PREFIX and not allow_wide:
                raise Refused(
                    f"{network} covers {network.num_addresses:,} addresses; pass --allow-wide if "
                    "that is really meant"
                )
            if str(network) not in ranges:
                ranges.append(str(network))
    if not ranges:
        raise Refused("give at least one range, such as 203.0.113.7/32, or --my-ip")
    return ranges


def my_ip(opener=urllib.request.urlopen) -> str:
    """This machine's public IPv4 address as a /32, as AWS's own echo service sees it."""
    with opener(MY_IP_URL, timeout=10) as response:
        text = response.read().decode("ascii", "replace").strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        raise Refused(f"{MY_IP_URL} did not answer with an address") from None
    if address.version != 4:
        raise Refused(f"{MY_IP_URL} answered an IPv6 address; give your IPv4 range instead")
    return f"{address}/32"


def resource_policy(cidrs: list[str], execution_arn: str) -> str:
    """The admin API's resource policy for `cidrs`: a Deny of every call from outside them, or of
    every call at all when there are none (Terraform's starting policy). Deny-only, so AWS_IAM
    authorization still decides who, from an allowed address, gets in."""
    statement = {
        "Sid": "DenyOutsideAdminAllowlist",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "execute-api:Invoke",
        "Resource": f"{execution_arn}/*",
    }
    if cidrs:
        statement["Condition"] = {"NotIpAddress": {"aws:SourceIp": cidrs}}
    return json.dumps({"Version": "2012-10-17", "Statement": [statement]}, separators=(",", ":"))


def policy_cidrs(policy: str | None) -> list[str] | None:
    """The addresses a resource policy admits, [] for a Deny of everything, None when it is not
    one this script writes (or there is none)."""
    if not policy:
        return None
    try:
        # API Gateway hands the policy back with its quotes escaped.
        statements = json.loads(policy.replace('\\"', '"'))["Statement"]
        (statement,) = statements
        if statement.get("Sid") != "DenyOutsideAdminAllowlist" or statement.get("Effect") != "Deny":
            return None
        if "Condition" not in statement:
            return []
        cidrs = statement["Condition"]["NotIpAddress"]["aws:SourceIp"]
        return [cidrs] if isinstance(cidrs, str) else list(cidrs)
    except (ValueError, KeyError, TypeError):
        return None


@dataclass
class Aws:
    """The clients, made once; a test passes fakes."""

    ssm: object
    wafv2: object
    apigateway: object
    sts: object

    @classmethod
    def connect(cls, region: str) -> Aws:
        import boto3

        session = boto3.session.Session(region_name=region)
        return cls(*(session.client(name) for name in ("ssm", "wafv2", "apigateway", "sts")))


def read_parameter(aws: Aws, name: str) -> list[str] | None:
    """The stored list, or None when the parameter is not there yet."""
    try:
        value = aws.ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except aws.ssm.exceptions.ParameterNotFound:
        return None
    return [entry.strip() for entry in value.split(",") if entry.strip()]


def write_parameter(aws: Aws, name: str, cidrs: list[str]) -> None:
    aws.ssm.put_parameter(
        Name=name,
        Value=",".join(cidrs),
        Type="SecureString",
        Overwrite=True,
        Description="BloggerBear admin API allowlist (scripts/admin_allowlist.py)",
    )


def find_ip_set(aws: Aws, name: str) -> dict | None:
    """The IP set's summary (Name, Id), or None when the admin web ACL is switched off."""
    marker = None
    while True:
        page = aws.wafv2.list_ip_sets(Scope="REGIONAL", **({"NextMarker": marker} if marker else {}))
        for summary in page.get("IPSets", []):
            if summary["Name"] == name:
                return summary
        marker = page.get("NextMarker")
        if not marker or not page.get("IPSets"):
            return None


def find_api(aws: Aws, name: str) -> dict:
    position = None
    while True:
        page = aws.apigateway.get_rest_apis(limit=500, **({"position": position} if position else {}))
        for api in page.get("items", []):
            if api["name"] == name:
                return api
        position = page.get("position")
        if not position:
            raise Refused(f"no REST API named {name}: is this environment deployed, in this region?")


def execution_arn(aws: Aws, region: str, api_id: str) -> str:
    account = aws.sts.get_caller_identity()["Account"]
    return f"arn:aws:execute-api:{region}:{account}:{api_id}"


def sync(aws: Aws, *, prefix: str, env: str, region: str, cidrs: list[str], say=print) -> None:
    """Put `cidrs` in the IP set (if there is one) and the API's resource policy, then deploy the
    stage so the policy is enforced. The IP set first: it applies at once."""
    ip_set = find_ip_set(aws, ip_set_name(prefix, env))
    if ip_set is None:
        say(f"WAF IP set {ip_set_name(prefix, env)}: none (the admin web ACL is switched off)")
    else:
        current = aws.wafv2.get_ip_set(Name=ip_set["Name"], Scope="REGIONAL", Id=ip_set["Id"])
        aws.wafv2.update_ip_set(
            Name=ip_set["Name"],
            Scope="REGIONAL",
            Id=ip_set["Id"],
            Addresses=cidrs,
            LockToken=current["LockToken"],
        )
        say(f"WAF IP set {ip_set['Name']}: updated")

    api = find_api(aws, api_name(prefix, env))
    policy = resource_policy(cidrs, execution_arn(aws, region, api["id"]))
    aws.apigateway.update_rest_api(
        restApiId=api["id"], patchOperations=[{"op": "replace", "path": "/policy", "value": policy}]
    )
    deployment = aws.apigateway.create_deployment(
        restApiId=api["id"],
        stageName=env,
        description="admin allowlist (scripts/admin_allowlist.py)",
    )
    say(f"API {api['name']}: resource policy updated, stage {env} deployed ({deployment['id']})")


def show(aws: Aws, *, prefix: str, env: str, lookup_ip: bool, say=print) -> bool:
    """What each place holds, and whether they agree. Only reads."""
    stored = read_parameter(aws, parameter_name(prefix, env))
    say(f"parameter {parameter_name(prefix, env)}: {', '.join(stored) if stored else 'not set'}")

    places = []
    ip_set = find_ip_set(aws, ip_set_name(prefix, env))
    if ip_set is None:
        say(f"WAF IP set {ip_set_name(prefix, env)}: none (the admin web ACL is switched off)")
    else:
        held = aws.wafv2.get_ip_set(Name=ip_set["Name"], Scope="REGIONAL", Id=ip_set["Id"])
        addresses = held["IPSet"]["Addresses"]
        say(f"WAF IP set {ip_set['Name']}: {', '.join(addresses) or 'empty (blocks everything)'}")
        places.append(addresses)

    api = find_api(aws, api_name(prefix, env))
    in_policy = policy_cidrs(api.get("policy"))
    if in_policy is None:
        say(f"API {api['name']} resource policy: not one this script writes")
    else:
        say(f"API {api['name']} resource policy: {', '.join(in_policy) or 'denies everything'}")
    places.append(in_policy)

    agree = stored is not None and all(
        place is not None and sorted(place) == sorted(stored) for place in places
    )
    say("in agreement" if agree else f"NOT in agreement: run `sync --env {env}` (or `set`)")
    if lookup_ip:
        here = my_ip()
        listed = any(ipaddress.ip_network(here).subnet_of(ipaddress.ip_network(c)) for c in stored or [])
        say(f"this machine: {here}, {'listed' if listed else 'NOT listed'}")
    return agree


def main(argv: list[str] | None = None, *, aws: Aws | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="the resources' name prefix")
    parser.add_argument("--region", default=DEFAULT_REGION, help="the home region")
    commands = parser.add_subparsers(dest="command", required=True)

    show_parser = commands.add_parser("show", help="what each place holds, and whether they agree")
    show_parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    show_parser.add_argument("--no-lookup", action="store_true", help="do not look up this machine's address")

    set_parser = commands.add_parser("set", help="store the list, then sync it")
    set_parser.add_argument("--env", choices=ENVIRONMENTS, required=True)
    set_parser.add_argument("cidrs", nargs="*", help="IPv4 ranges, such as 203.0.113.7/32")
    set_parser.add_argument("--my-ip", action="store_true", help="this machine's public address, as a /32")
    set_parser.add_argument("--allow-wide", action="store_true", help="allow ranges wider than /16")

    sync_parser = commands.add_parser("sync", help="copy the stored list into the IP set and the policy")
    sync_parser.add_argument("--env", choices=ENVIRONMENTS, required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "set":
            given = list(args.cidrs) + ([my_ip()] if args.my_ip else [])
            cidrs = check_cidrs(given, allow_wide=args.allow_wide)
        aws = aws or Aws.connect(args.region)
        if args.command == "show":
            return 0 if show(aws, prefix=args.prefix, env=args.env, lookup_ip=not args.no_lookup) else 1
        name = parameter_name(args.prefix, args.env)
        if args.command == "set":
            write_parameter(aws, name, cidrs)
            print(f"parameter {name}: {', '.join(cidrs)}")
        else:
            cidrs = read_parameter(aws, name)
            if not cidrs:
                raise Refused(f"parameter {name} is not set: run `set --env {args.env}` first")
        sync(aws, prefix=args.prefix, env=args.env, region=args.region, cidrs=cidrs)
    except Refused as problem:
        print(f"refused: {problem}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
