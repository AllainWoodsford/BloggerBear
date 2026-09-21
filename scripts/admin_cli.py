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
import time
import urllib.parse

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session as BotocoreSession

SERVICE_NAME = "execute-api"

# `topics trigger`'s target Lambda invocations are fire-and-forget
# (admin_api_handler.py's _trigger_topic uses InvocationType="Event"), so
# a 202 response only means "accepted", not "finished". Left un-polled,
# the easy mistake is firing research_tick then daily_cycle back to back:
# daily_cycle reads Findings written by research_tick, and if it runs
# before research_tick's Lambda has actually completed, it just returns
# {"status": "no_findings"} with nothing to show for it -- no error,
# nothing obviously wrong, just silently empty. See --no-wait below to
# skip this and get the old fire-and-forget behavior back.
_POLL_INTERVAL_SECONDS = 3
_RESEARCH_TICK_TIMEOUT_SECONDS = 30
# daily_cycle makes multiple sequential Bedrock calls (ideate, draft,
# compliance review) -- allow more time than research_tick's single call.
_DAILY_CYCLE_TIMEOUT_SECONDS = 90


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


def _apply_model_flags(body: dict, args: argparse.Namespace) -> None:
    """Add the optional per-topic model fields to a create/update body.

    Unset flags are omitted entirely (so an update leaves the field alone).
    An empty string clears the field -- sent as null for model_id/
    fallback_model_id, and as [] for the rotation candidates -- since the
    Admin API validates "non-empty string if provided" and accepts null/[]
    as "unset".
    """
    if args.model_id is not None:
        body["model_id"] = args.model_id or None
    if args.fallback_model_id is not None:
        body["fallback_model_id"] = args.fallback_model_id or None
    if args.model_candidates is not None:
        body["model_id_candidates"] = [
            candidate.strip() for candidate in args.model_candidates.split(",") if candidate.strip()
        ]


def _add_model_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model-id",
        dest="model_id",
        default=None,
        help="Pin this topic to one Bedrock model (overrides the global default); '' clears it",
    )
    parser.add_argument(
        "--fallback-model-id",
        dest="fallback_model_id",
        default=None,
        help="Model to retry with if this topic's primary model call fails; '' clears it",
    )
    parser.add_argument(
        "--model-candidates",
        dest="model_candidates",
        default=None,
        help=(
            "Comma-separated model IDs to rotate between -- one is picked at random per "
            "daily run, ahead of --model-id; '' clears rotation"
        ),
    )


def _parse_editorial_goals_json(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise CliError(f"--editorial-goals-json is not valid JSON: {exc}") from exc


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
        "adapter_config": adapter_config,
        "is_financial": args.financial,
    }
    # Omitted entirely (not sent as null) when not passed, so the Admin
    # API's own defaults (the web_search adapter, _DEFAULT_RESEARCH_CADENCE /
    # _DEFAULT_DAILY_CADENCE / _DEFAULT_DAILY_TIMEZONE) apply -- matching create_topic's own
    # body.get(..., default) behavior.
    if args.adapter is not None:
        body["adapter"] = args.adapter
    if args.editorial_goals_json is not None:
        body["editorial_goals"] = _parse_editorial_goals_json(args.editorial_goals_json)
    if args.research_cadence is not None:
        body["research_cadence"] = args.research_cadence
    if args.daily_cadence is not None:
        body["daily_cadence"] = args.daily_cadence
    if args.daily_timezone is not None:
        body["daily_timezone"] = args.daily_timezone
    _apply_model_flags(body, args)
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
    if args.editorial_goals_json is not None:
        body["editorial_goals"] = _parse_editorial_goals_json(args.editorial_goals_json)
    if args.financial is not None:
        body["is_financial"] = args.financial
    if args.research_cadence is not None:
        body["research_cadence"] = args.research_cadence
    if args.daily_cadence is not None:
        body["daily_cadence"] = args.daily_cadence
    if args.daily_timezone is not None:
        body["daily_timezone"] = args.daily_timezone
    _apply_model_flags(body, args)

    if not body:
        raise CliError("topics update requires at least one field to change")

    _do_request(args, "PUT", f"/topics/{args.topic_id}", body=body)


def _cmd_topics_delete(args: argparse.Namespace) -> None:
    _do_request(args, "DELETE", f"/topics/{args.topic_id}")


def _get_json_or_none(args: argparse.Namespace, path: str) -> dict | None:
    """GET `path`, returning the parsed JSON body, or None on a 404 or any
    other non-2xx response. Used only for polling baselines/completion
    checks, where a transient/expected "not there yet" response should
    make the caller keep waiting rather than crash the whole command.
    """
    response = signed_request("GET", _resolve_api_url(args), path, _resolve_region(args))
    if not (200 <= response.status_code < 300):
        return None
    try:
        return response.json()
    except ValueError:
        return None


def _latest_finding_captured_at(args: argparse.Namespace, topic_id: str) -> str | None:
    finding = _get_json_or_none(args, f"/topics/{topic_id}/findings/latest")
    return finding.get("captured_at") if finding else None


def _candidate_count(args: argparse.Namespace, topic_id: str) -> int:
    body = _get_json_or_none(args, f"/topics/{topic_id}/candidates")
    return len(body["candidates"]) if body else 0


def _poll_until(predicate, timeout_seconds: float) -> bool:
    """Calls `predicate()` every _POLL_INTERVAL_SECONDS until it returns
    True or `timeout_seconds` elapses. Returns whether it succeeded.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)


def _cmd_topics_trigger(args: argparse.Namespace) -> None:
    topic_id = args.topic_id
    pipeline = args.pipeline

    body: dict = {"pipeline": pipeline}
    if args.force:
        if pipeline != "daily_cycle":
            raise CliError("--force only applies to --pipeline daily_cycle")
        body["force"] = True

    # Captured before firing the trigger so "finished" can mean "produced
    # something new", not just "produced something" -- a topic already
    # sitting on findings/candidates from a previous run would otherwise
    # look instantly "done" without this pipeline run having done anything.
    baseline_captured_at = None
    baseline_candidate_count = None
    if args.wait:
        if pipeline == "research_tick":
            baseline_captured_at = _latest_finding_captured_at(args, topic_id)
        else:
            baseline_candidate_count = _candidate_count(args, topic_id)

    _do_request(args, "POST", f"/topics/{topic_id}/trigger", body=body)

    if not args.wait:
        return

    print(
        f"Waiting for {pipeline} to actually finish (its Lambda invocation is "
        "asynchronous, so the response above only means it was accepted)...",
        file=sys.stderr,
    )

    if pipeline == "research_tick":
        finished = _poll_until(
            lambda: (current := _latest_finding_captured_at(args, topic_id)) is not None
            and current != baseline_captured_at,
            _RESEARCH_TICK_TIMEOUT_SECONDS,
        )
        if finished:
            print(f"research_tick finished: a new Finding was written for '{topic_id}'.")
        else:
            print(
                f"No new Finding after {_RESEARCH_TICK_TIMEOUT_SECONDS}s. This can be a "
                "legitimate outcome (the adapter found no material change since last time), "
                "or a real failure -- check the logs:\n"
                "  aws logs tail /aws/lambda/<research-tick function name> --since 5m",
                file=sys.stderr,
            )
    else:
        finished = _poll_until(
            lambda: _candidate_count(args, topic_id) > baseline_candidate_count,
            _DAILY_CYCLE_TIMEOUT_SECONDS,
        )
        if finished:
            print(
                f"daily_cycle finished: new candidate ideas were generated for '{topic_id}'. "
                "Check `moderation list` or the site for the final published/queued result."
            )
        else:
            print(
                f"No new candidates after {_DAILY_CYCLE_TIMEOUT_SECONDS}s. This can mean there "
                "were no Findings newer than the topic's last article (run research_tick first "
                "and confirm it produced a Finding, or pass --force to rewrite from the "
                "whole last-24h window), or a real failure -- check the logs:\n"
                "  aws logs tail /aws/lambda/<daily-cycle function name> --since 5m",
                file=sys.stderr,
            )


def _cmd_topics_candidates(args: argparse.Namespace) -> None:
    _do_request(args, "GET", f"/topics/{args.topic_id}/candidates")


def _cmd_topics_findings(args: argparse.Namespace) -> None:
    _do_request(args, "GET", f"/topics/{args.topic_id}/findings/latest")


# --- articles subcommands ------------------------------------------------


def _cmd_articles_publish(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/articles/{args.article_id}/publish")


def _cmd_articles_unpublish(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/articles/{args.article_id}/unpublish")


# --- moderation subcommands ---------------------------------------------


def _cmd_moderation_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/moderation-queue")


def _cmd_moderation_approve(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/moderation-queue/{args.queue_id}/approve")


def _cmd_moderation_reject(args: argparse.Namespace) -> None:
    _do_request(args, "POST", f"/moderation-queue/{args.queue_id}/reject")


def _cmd_moderation_stats(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/moderation-queue/stats")


# --- failed-executions subcommands -----------------------------------------


def _cmd_failed_executions_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/failed-executions")


# --- models / model-config subcommands --------------------------------------


def _cmd_models_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/models")


def _cmd_models_add(args: argparse.Namespace) -> None:
    body = {
        "model_id": args.model_id,
        "display_name": args.display_name,
        "provider": args.provider,
        "input_price_usd_per_1k_tokens": args.input_price,
        "output_price_usd_per_1k_tokens": args.output_price,
        "enabled": args.enabled,
    }
    _do_request(args, "POST", "/models", body=body)


def _cmd_model_config_get(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/model-config")


def _cmd_model_config_set(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.model_id is not None:
        body["model_id"] = args.model_id
    if args.fallback_model_id is not None:
        body["fallback_model_id"] = args.fallback_model_id
    if not body:
        raise CliError("model-config set requires --model-id and/or --fallback-model-id")
    _do_request(args, "PUT", "/model-config", body=body)


# --- refinements subcommands -----------------------------------------------


def _cmd_refinements_list(args: argparse.Namespace) -> None:
    query: dict = {}
    if args.topic_id:
        query["topic_id"] = args.topic_id
    if args.status:
        query["status"] = args.status

    path = "/prompt-refinements"
    if query:
        path += "?" + urllib.parse.urlencode(query)
    _do_request(args, "GET", path)


def _cmd_refinements_approve(args: argparse.Namespace) -> None:
    # `version` is an ISO-8601 timestamp and contains ':' characters, which
    # must be percent-encoded before they can go in a URL path segment.
    version = urllib.parse.quote(args.version, safe="")
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/approve")


def _cmd_refinements_reject(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/reject")


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
    create_parser.add_argument(
        "--adapter",
        default=None,
        help="Adapter key (default: web_search -- independent web research on the topic's name)",
    )
    create_parser.add_argument("--config-json", dest="config_json", default=None)
    create_parser.add_argument(
        "--editorial-goals-json",
        dest="editorial_goals_json",
        default=None,
        help=(
            "Topic-specific goal, e.g. "
            "'{\"primary_focus\": \"...\", \"exclusion_criteria\": \"...\"}'; "
            "omit to inherit the adapter/global default"
        ),
    )
    create_parser.add_argument("--financial", action="store_true", default=False)
    create_parser.add_argument(
        "--research-cadence",
        dest="research_cadence",
        default=None,
        help="EventBridge Scheduler expression, e.g. 'rate(1 hour)' (default: rate(1 hour))",
    )
    create_parser.add_argument(
        "--daily-cadence",
        dest="daily_cadence",
        default=None,
        help="EventBridge Scheduler expression, e.g. 'cron(0 9 * * ? *)' (default: cron(0 9 * * ? *))",
    )
    create_parser.add_argument(
        "--daily-timezone",
        dest="daily_timezone",
        default=None,
        help="IANA zone the daily cron is read in (default: Australia/Sydney)",
    )
    _add_model_flags(create_parser)
    create_parser.set_defaults(func=_cmd_topics_create)

    update_parser = topics_sub.add_parser("update", help="Update a topic")
    update_parser.add_argument("topic_id")
    update_parser.add_argument("--name", default=None)
    update_parser.add_argument("--adapter-config-json", dest="adapter_config_json", default=None)
    update_parser.add_argument(
        "--editorial-goals-json",
        dest="editorial_goals_json",
        default=None,
        help="Replaces the topic's whole editorial_goals block; '{}' clears it (back to the default)",
    )
    update_parser.add_argument(
        "--financial", dest="financial", action="store_true", default=None
    )
    update_parser.add_argument(
        "--no-financial", dest="financial", action="store_false"
    )
    update_parser.add_argument(
        "--research-cadence",
        dest="research_cadence",
        default=None,
        help="EventBridge Scheduler expression, e.g. 'rate(1 hour)'",
    )
    update_parser.add_argument(
        "--daily-cadence",
        dest="daily_cadence",
        default=None,
        help="EventBridge Scheduler expression, e.g. 'cron(0 9 * * ? *)'",
    )
    update_parser.add_argument(
        "--daily-timezone",
        dest="daily_timezone",
        default=None,
        help=(
            "IANA zone the daily cron is read in, e.g. 'Australia/Sydney'. A topic "
            "created before this option existed is on UTC until you set it"
        ),
    )
    _add_model_flags(update_parser)
    update_parser.set_defaults(func=_cmd_topics_update)

    delete_parser = topics_sub.add_parser("delete", help="Delete a topic")
    delete_parser.add_argument("topic_id")
    delete_parser.set_defaults(func=_cmd_topics_delete)

    trigger_parser = topics_sub.add_parser("trigger", help="Manually trigger a pipeline run")
    trigger_parser.add_argument("topic_id")
    trigger_parser.add_argument(
        "--pipeline", required=True, choices=["research_tick", "daily_cycle"]
    )
    trigger_parser.add_argument(
        "--no-wait",
        dest="wait",
        action="store_false",
        default=True,
        help=(
            "Don't poll for completion after triggering -- just fire the request and "
            "return immediately (the old behavior). Default is to wait and report when "
            "the pipeline has actually finished, since the trigger itself is asynchronous."
        ),
    )
    trigger_parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "daily_cycle only: write an article from the whole last-24h window even if "
            "the topic already has one since (the default is to write only from findings "
            "newer than its last article, so a repeat run with nothing new is a no-op)"
        ),
    )
    trigger_parser.set_defaults(func=_cmd_topics_trigger)

    candidates_parser = topics_sub.add_parser(
        "candidates", help="List candidate ideas considered for a topic"
    )
    candidates_parser.add_argument("topic_id")
    candidates_parser.set_defaults(func=_cmd_topics_candidates)

    findings_parser = topics_sub.add_parser(
        "findings", help="Show the most recent research Finding for a topic"
    )
    findings_parser.add_argument("topic_id")
    findings_parser.set_defaults(func=_cmd_topics_findings)

    articles_parser = subparsers.add_parser("articles", help="Manage articles")
    articles_sub = articles_parser.add_subparsers(dest="action", required=True)

    publish_parser = articles_sub.add_parser(
        "publish",
        help="Force-publish an article regardless of its current status",
    )
    publish_parser.add_argument("article_id")
    publish_parser.set_defaults(func=_cmd_articles_publish)

    unpublish_parser = articles_sub.add_parser(
        "unpublish",
        help=(
            "Take a published article down: delete its page, mark it rejected, "
            "remove its musings and clear the CDN cache"
        ),
    )
    unpublish_parser.add_argument("article_id")
    unpublish_parser.set_defaults(func=_cmd_articles_unpublish)

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

    moderation_sub.add_parser(
        "stats", help="Summarize what compliance review has flagged, across all history"
    ).set_defaults(func=_cmd_moderation_stats)

    failed_executions_parser = subparsers.add_parser(
        "failed-executions", help="View daily_cycle executions that exhausted their retries"
    )
    failed_executions_sub = failed_executions_parser.add_subparsers(dest="action", required=True)

    failed_executions_sub.add_parser(
        "list", help="List failed daily_cycle executions recorded by the DLQ consumer"
    ).set_defaults(func=_cmd_failed_executions_list)

    models_parser = subparsers.add_parser("models", help="Manage the AI model registry")
    models_sub = models_parser.add_subparsers(dest="action", required=True)

    models_sub.add_parser("list", help="List the supported-models registry").set_defaults(
        func=_cmd_models_list
    )

    models_add_parser = models_sub.add_parser(
        "add", help="Add or update a model in the registry"
    )
    models_add_parser.add_argument("--model-id", required=True, dest="model_id")
    models_add_parser.add_argument("--display-name", required=True, dest="display_name")
    models_add_parser.add_argument("--provider", required=True)
    models_add_parser.add_argument(
        "--input-price", required=True, type=float, dest="input_price",
        help="USD per 1k input tokens",
    )
    models_add_parser.add_argument(
        "--output-price", required=True, type=float, dest="output_price",
        help="USD per 1k output tokens",
    )
    models_add_parser.add_argument(
        "--disabled", dest="enabled", action="store_false", default=True,
        help="Register the model as disabled (default: enabled)",
    )
    models_add_parser.set_defaults(func=_cmd_models_add)

    model_config_parser = subparsers.add_parser(
        "model-config", help="Manage the global default/fallback model"
    )
    model_config_sub = model_config_parser.add_subparsers(dest="action", required=True)

    model_config_sub.add_parser(
        "get", help="Show the current global default/fallback model"
    ).set_defaults(func=_cmd_model_config_get)

    model_config_set_parser = model_config_sub.add_parser(
        "set", help="Set the global default and/or fallback model"
    )
    model_config_set_parser.add_argument("--model-id", dest="model_id", default=None)
    model_config_set_parser.add_argument(
        "--fallback-model-id", dest="fallback_model_id", default=None
    )
    model_config_set_parser.set_defaults(func=_cmd_model_config_set)

    refinements_parser = subparsers.add_parser("refinements", help="Manage prompt refinements")
    refinements_sub = refinements_parser.add_subparsers(dest="action", required=True)

    refinements_list_parser = refinements_sub.add_parser(
        "list", help="List prompt refinement proposals"
    )
    refinements_list_parser.add_argument("--topic-id", dest="topic_id", default=None)
    refinements_list_parser.add_argument("--status", dest="status", default=None)
    refinements_list_parser.set_defaults(func=_cmd_refinements_list)

    refinements_approve_parser = refinements_sub.add_parser(
        "approve", help="Approve a pending prompt refinement"
    )
    refinements_approve_parser.add_argument("topic_id")
    refinements_approve_parser.add_argument("version")
    refinements_approve_parser.set_defaults(func=_cmd_refinements_approve)

    refinements_reject_parser = refinements_sub.add_parser(
        "reject", help="Reject a pending prompt refinement"
    )
    refinements_reject_parser.add_argument("topic_id")
    refinements_reject_parser.add_argument("version")
    refinements_reject_parser.set_defaults(func=_cmd_refinements_reject)

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
