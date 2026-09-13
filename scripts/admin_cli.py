#!/usr/bin/env python3
"""BloggerBear admin operator CLI.

The Phase 2 Admin API is authenticated with AWS SigV4 (IAM) and additionally
restricted by a WAF IP allowlist (see docs/project-plan.md Phase 2 and this
PR's description for why: a browser SPA would need Cognito plus a way to
hold long-lived IAM credentials safely, which is more infrastructure than a
single-operator portfolio project needs). This script is the practical
"admin UI" for that auth model -- it signs requests with whatever AWS
credentials the operator's own environment already has configured
(`aws configure`, SSO, environment variables, ...) via botocore's default
credential chain, the same chain boto3 uses.

This script is NOT deployed anywhere -- it's meant to be run locally:

    python scripts/admin_cli.py topics list

Configuration (see scripts/README.md for details):
  --api-url / BLOGGERBEAR_ADMIN_API_URL   Admin API base URL (from
                                           `terraform output` after apply).
  --region / AWS_REGION / AWS_DEFAULT_REGION   AWS region the API is deployed in.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

SERVICE_NAME = "execute-api"


class CliError(Exception):
    """Raised for user-facing configuration/usage errors."""


def _resolve_api_url(args: argparse.Namespace) -> str:
    api_url = args.api_url or os.environ.get("BLOGGERBEAR_ADMIN_API_URL")
    if not api_url:
        raise CliError(
            "Admin API URL not configured. Pass --api-url or set "
            "BLOGGERBEAR_ADMIN_API_URL (see scripts/README.md)."
        )
    return api_url.rstrip("/")


def _resolve_region(args: argparse.Namespace) -> str:
    region = args.region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        raise CliError(
            "AWS region not configured. Pass --region or set AWS_REGION / "
            "AWS_DEFAULT_REGION."
        )
    return region


def signed_request(
    method: str,
    api_url: str,
    path: str,
    region: str,
    body: dict | None = None,
) -> requests.Response:
    """Send a SigV4-signed request to the Admin API using local AWS credentials.

    Signs with the caller's default credential chain (whatever `aws
    configure`, SSO, or environment variables provide) via botocore, then
    sends the request with `requests`.
    """
    session = BotocoreSession()
    credentials = session.get_credentials()
    if credentials is None:
        raise CliError(
            "No AWS credentials found. Configure them with `aws configure`, "
            "SSO, or environment variables."
        )

    url = f"{api_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}

    aws_request = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(credentials, SERVICE_NAME, region).add_auth(aws_request)
    prepared_headers = dict(aws_request.headers.items())

    return requests.request(method, url, headers=prepared_headers, data=data, timeout=30)


def _print_response_and_exit_on_error(response: requests.Response) -> None:
    try:
        parsed = response.json()
        printed = json.dumps(parsed, indent=2)
    except ValueError:
        printed = response.text

    if 200 <= response.status_code < 300:
        print(printed)
        return

    print(f"Request failed with status {response.status_code}:", file=sys.stderr)
    print(printed, file=sys.stderr)
    sys.exit(1)


def _do_request(args: argparse.Namespace, method: str, path: str, body: dict | None = None) -> None:
    api_url = _resolve_api_url(args)
    region = _resolve_region(args)
    response = signed_request(method, api_url, path, region, body=body)
    _print_response_and_exit_on_error(response)


# --- topics subcommands ------------------------------------------------


def _cmd_topics_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/topics")


def _cmd_topics_get(args: argparse.Namespace) -> None:
    _do_request(args, "GET", f"/topics/{args.topic_id}")


def _cmd_topics_create(args: argparse.Namespace) -> None:
    adapter_config = {}
    if args.config_json:
        try:
            adapter_config = json.loads(args.config_json)
        except json.JSONDecodeError as exc:
            raise CliError(f"--config-json is not valid JSON: {exc}") from exc

    body = {
        "topic_id": args.topic_id,
        "name": args.name,
        "adapter": args.adapter,
        "adapter_config": adapter_config,
        "is_financial": args.financial,
    }
    _do_request(args, "POST", "/topics", body=body)


def _cmd_topics_update(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.name is not None:
        body["name"] = args.name
    if args.adapter_config_json is not None:
        try:
            body["adapter_config"] = json.loads(args.adapter_config_json)
        except json.JSONDecodeError as exc:
            raise CliError(f"--adapter-config-json is not valid JSON: {exc}") from exc
    if args.financial is not None:
        body["is_financial"] = args.financial

    if not body:
        raise CliError("topics update requires at least one field to change")

    _do_request(args, "PUT", f"/topics/{args.topic_id}", body=body)


def _cmd_topics_delete(args: argparse.Namespace) -> None:
    _do_request(args, "DELETE", f"/topics/{args.topic_id}")


def _cmd_topics_trigger(args: argparse.Namespace) -> None:
    _do_request(
        args, "POST", f"/topics/{args.topic_id}/trigger", body={"pipeline": args.pipeline}
    )


def _cmd_topics_candidates(args: argparse.Namespace) -> None:
    _do_request(args, "GET", f"/topics/{args.topic_id}/candidates")


# --- moderation subcommands ---------------------------------------------


def _cmd_moderation_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/moderation-queue")


def _cmd_moderation_approve(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/moderation-queue/{args.queue_id}/approve")


def _cmd_moderation_reject(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/moderation-queue/{args.queue_id}/reject")


# --- argument parsing -----------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="admin_cli.py", description="BloggerBear admin operator CLI"
    )
    parser.add_argument(
        "--api-url",
        help="Admin API base URL (default: $BLOGGERBEAR_ADMIN_API_URL)",
    )
    parser.add_argument(
        "--region",
        help="AWS region (default: $AWS_REGION / $AWS_DEFAULT_REGION)",
    )

    subparsers = parser.add_subparsers(dest="resource", required=True)

    topics_parser = subparsers.add_parser("topics", help="Manage Topics")
    topics_sub = topics_parser.add_subparsers(dest="action", required=True)

    topics_sub.add_parser("list", help="List all topics").set_defaults(func=_cmd_topics_list)

    get_parser = topics_sub.add_parser("get", help="Get one topic")
    get_parser.add_argument("topic_id")
    get_parser.set_defaults(func=_cmd_topics_get)

    create_parser = topics_sub.add_parser("create", help="Create a topic")
    create_parser.add_argument("--topic-id", required=True, dest="topic_id")
    create_parser.add_argument("--name", required=True)
    create_parser.add_argument("--adapter", required=True)
    create_parser.add_argument("--config-json", dest="config_json", default=None)
    create_parser.add_argument("--financial", action="store_true", default=False)
    create_parser.set_defaults(func=_cmd_topics_create)

    update_parser = topics_sub.add_parser("update", help="Update a topic")
    update_parser.add_argument("topic_id")
    update_parser.add_argument("--name", default=None)
    update_parser.add_argument("--adapter-config-json", dest="adapter_config_json", default=None)
    update_parser.add_argument(
        "--financial", dest="financial", action="store_true", default=None
    )
    update_parser.add_argument(
        "--no-financial", dest="financial", action="store_false"
    )
    update_parser.set_defaults(func=_cmd_topics_update)

    delete_parser = topics_sub.add_parser("delete", help="Delete a topic")
    delete_parser.add_argument("topic_id")
    delete_parser.set_defaults(func=_cmd_topics_delete)

    trigger_parser = topics_sub.add_parser("trigger", help="Manually trigger a pipeline run")
    trigger_parser.add_argument("topic_id")
    trigger_parser.add_argument(
        "--pipeline", required=True, choices=["research_tick", "daily_cycle"]
    )
    trigger_parser.set_defaults(func=_cmd_topics_trigger)

    candidates_parser = topics_sub.add_parser(
        "candidates", help="List candidate ideas considered for a topic"
    )
    candidates_parser.add_argument("topic_id")
    candidates_parser.set_defaults(func=_cmd_topics_candidates)

    moderation_parser = subparsers.add_parser("moderation", help="Manage the moderation queue")
    moderation_sub = moderation_parser.add_subparsers(dest="action", required=True)

    moderation_sub.add_parser("list", help="List pending moderation items").set_defaults(
        func=_cmd_moderation_list
    )

    approve_parser = moderation_sub.add_parser("approve", help="Approve a moderation item")
    approve_parser.add_argument("queue_id")
    approve_parser.set_defaults(func=_cmd_moderation_approve)

    reject_parser = moderation_sub.add_parser("reject", help="Reject a moderation item")
    reject_parser.add_argument("queue_id")
    reject_parser.set_defaults(func=_cmd_moderation_reject)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
