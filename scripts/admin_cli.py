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


def _parse_interval_hours(text: str):
    """`--research-interval-hours` value: a whole number of hours, or '' meaning
    "clear it and inherit the pipeline-wide default" (sent as null)."""
    if text == "":
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise CliError(
            f"--research-interval-hours must be a whole number of hours (or ''): {text!r}"
        ) from exc


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
    if args.research_interval_hours is not None:
        body["research_interval_hours"] = _parse_interval_hours(args.research_interval_hours)
    if args.review_mode is not None:
        body["review_mode"] = args.review_mode or None  # '' clears it (inherit the pipeline mode)
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
    if args.research_interval_hours is not None:
        body["research_interval_hours"] = _parse_interval_hours(args.research_interval_hours)
    if args.review_mode is not None:
        body["review_mode"] = args.review_mode or None  # '' clears it (inherit the pipeline mode)
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


def _cmd_articles_rewrite(args: argparse.Namespace) -> None:
    body = {"instructions": args.instructions}
    if args.model_id:
        body["model_id"] = args.model_id
    _do_request(args, "POST", f"/articles/{args.article_id}/rewrite", body)


# --- feedback subcommands -----------------------------------------------


def _parse_whole(flag: str, text: str):
    """A whole-number flag value, or '' meaning "clear it, back to the default" (sent as null)."""
    if text == "":
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise CliError(f"{flag} must be a whole number (or ''): {text!r}") from exc


def _parse_bool(flag: str, text: str):
    if text == "":
        return None
    if text.lower() in ("true", "yes", "on"):
        return True
    if text.lower() in ("false", "no", "off"):
        return False
    raise CliError(f"{flag} must be true or false (or ''): {text!r}")


def _cmd_feedback_config_get(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/feedback-config")


def _cmd_feedback_config_set(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.locked_down is not None:
        body["locked_down"] = _parse_bool("--locked-down", args.locked_down)
    if args.lockdown_reason is not None:
        body["lockdown_reason"] = args.lockdown_reason or None
    if args.rate_limit is not None:
        body["rate_limit_count"] = _parse_whole("--rate-limit", args.rate_limit)
    if args.rate_window_minutes is not None:
        body["rate_limit_window_minutes"] = _parse_whole(
            "--rate-window-minutes", args.rate_window_minutes
        )
    if args.daily_limit is not None:
        body["daily_limit"] = _parse_whole("--daily-limit", args.daily_limit)
    if args.article_limit is not None:
        body["article_limit"] = _parse_whole("--article-limit", args.article_limit)
    if args.screening_limit is not None:
        body["screening_limit"] = _parse_whole("--screening-limit", args.screening_limit)
    if args.verification_required is not None:
        body["verification_required"] = _parse_bool(
            "--verification-required", args.verification_required
        )
    if args.token_delay_min_ms is not None:
        body["token_delay_min_ms"] = _parse_whole("--token-delay-min-ms", args.token_delay_min_ms)
    if args.token_delay_max_ms is not None:
        body["token_delay_max_ms"] = _parse_whole("--token-delay-max-ms", args.token_delay_max_ms)
    if args.pow_threshold_percent is not None:
        body["pow_threshold_percent"] = _parse_whole(
            "--pow-threshold-percent", args.pow_threshold_percent
        )
    if args.pow_difficulty_bits is not None:
        body["pow_difficulty_bits"] = _parse_whole(
            "--pow-difficulty-bits", args.pow_difficulty_bits
        )
    if args.daily_timezone is not None:
        body["daily_timezone"] = args.daily_timezone or None
    if not body:
        raise CliError(
            "feedback-config set needs at least one of --locked-down, --lockdown-reason, "
            "--rate-limit, --rate-window-minutes, --daily-limit, --article-limit, "
            "--screening-limit, --verification-required, --token-delay-min-ms, "
            "--token-delay-max-ms, --pow-threshold-percent, --pow-difficulty-bits, --daily-timezone"
        )
    _do_request(args, "PUT", "/feedback-config", body=body)


def _cmd_articles_feedback_lock(args: argparse.Namespace) -> None:
    _do_request(
        args, "PUT", f"/articles/{args.article_id}/feedback-lock", body={"locked": True}
    )


def _cmd_articles_feedback_unlock(args: argparse.Namespace) -> None:
    _do_request(
        args,
        "PUT",
        f"/articles/{args.article_id}/feedback-lock",
        body={"locked": False, "reset_count": bool(args.reset_count)},
    )


# --- inbox and review: what is waiting for you -----------------------------


def _review_api(args: argparse.Namespace):
    """The review inbox's view of the Admin API: the same signed requests every other command
    makes, with failures turned into errors the inbox can report and carry on from."""
    import review_inbox

    api_url = _resolve_api_url(args)
    region = _resolve_region(args)
    return review_inbox.Api(
        lambda method, path, body=None: signed_request(method, api_url, path, region, body=body)
    )


def _is_mock(args: argparse.Namespace) -> bool:
    return bool(args.mock) or os.environ.get("BLOGGERBEAR_REVIEW_MOCK", "").lower() in ("1", "true", "yes")


def _cmd_inbox(args: argparse.Namespace) -> None:
    import review_inbox

    if _is_mock(args):
        print(review_inbox.mock_inbox_report())
        return
    print(review_inbox.inbox_report(_review_api(args)))


def _cmd_approve(args: argparse.Namespace) -> None:
    import review_inbox

    mock = _is_mock(args)
    if args.limit < 1:
        raise CliError("--limit must be at least 1")
    store = review_inbox.SkipStore(review_inbox_state_path(mock))
    if args.reset_skipped:
        print(f"Forgot {store.clear()} skipped item(s).")
    sources = review_inbox.build_sources(None if mock else _review_api(args), args.source, mock)
    if mock:
        print("PRACTICE MODE: made-up items; nothing is read from or sent to AWS.")
    try:
        review_inbox.review(
            sources,
            limit=args.limit,
            store=store,
            skip_hours=args.skip_hours,
            include_skipped=args.include_skipped,
            dry_run=args.dry_run,
        )
    except KeyboardInterrupt:
        print("\nStopped. Nothing you already decided is lost.")


def review_inbox_state_path(mock: bool):
    """Where skips are remembered. Practice mode uses its own file so it never hides real items."""
    import review_inbox

    store = review_inbox.SkipStore()
    return store.path.with_name("review-skips-practice.json") if mock else store.path


# --- pipeline-config subcommands ----------------------------------------


def _cmd_pipeline_config_get(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/pipeline-config")


def _cmd_pipeline_config_set(args: argparse.Namespace) -> None:
    body: dict = {}
    if args.research_interval_hours is not None:
        body["research_interval_hours"] = _parse_interval_hours(args.research_interval_hours)
    if args.review_mode is not None:
        body["review_mode"] = args.review_mode or None  # '' clears it (back to the default)
    if args.review_on_unavailable is not None:
        body["review_on_unavailable"] = args.review_on_unavailable or None
    if not body:
        raise CliError(
            "pipeline-config set needs --research-interval-hours, --review-mode "
            "and/or --review-on-unavailable"
        )
    _do_request(args, "PUT", "/pipeline-config", body=body)


# --- review subcommands -------------------------------------------------


def _cmd_review_report(args: argparse.Namespace) -> None:
    path = "/review/report"
    if args.sample is not None:
        path += f"?sample={args.sample}"
    _do_request(args, "GET", path)


# --- lineage subcommands ------------------------------------------------


def _cmd_lineage_audit(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/lineage/audit")


def _cmd_lineage_backfill(args: argparse.Namespace) -> None:
    _do_request(args, "POST", "/lineage/backfill", body={"apply": args.apply})


# --- stats subcommands ---------------------------------------------------


def _cmd_stats_backfill_articles(args: argparse.Namespace) -> None:
    _do_request(args, "POST", "/stats/backfill-articles", body={"apply": args.apply})


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


def _placement_body(args: argparse.Namespace) -> dict | None:
    """The optional {scope, slot, replace} of an approve or equip call, from --scope, --slot and
    --replace TOPIC_ID VERSION. None when none were given, so the API's own default applies."""
    body: dict = {}
    if args.scope:
        body["scope"] = args.scope
    if args.slot:
        body["slot"] = args.slot
    if args.replace:
        body["replace"] = {"topic_id": args.replace[0], "version": args.replace[1]}
    if getattr(args, "no_announce", False):
        body["announce"] = False
    return body or None


def _add_placement_arguments(parser: argparse.ArgumentParser, scopes: tuple[str, ...]) -> None:
    parser.add_argument(
        "--scope",
        choices=scopes,
        default=None,
        help="topic: a ring for the item's own topic; global: an armor slot for every topic"
        + ("; backpack: approve but do not wear" if "backpack" in scopes else ""),
    )
    parser.add_argument(
        "--slot",
        choices=["helmet", "chest", "gloves", "boots", "sword", "shield"],
        default=None,
        help="the armor slot, for global guidance (default: the first empty one)",
    )
    parser.add_argument(
        "--replace",
        nargs=2,
        metavar=("TOPIC_ID", "VERSION"),
        default=None,
        help="when all rings are worn, the worn ring this one replaces",
    )
    parser.add_argument(
        "--no-announce",
        dest="no_announce",
        action="store_true",
        default=False,
        help="do not post a loot drop to the Musings when it is put on",
    )


def _cmd_refinements_approve(args: argparse.Namespace) -> None:
    # `version` is an ISO-8601 timestamp and contains ':' characters, which
    # must be percent-encoded before they can go in a URL path segment.
    version = urllib.parse.quote(args.version, safe="")
    _do_request(
        args,
        "POST",
        f"/prompt-refinements/{args.topic_id}/{version}/approve",
        body=_placement_body(args),
    )


def _cmd_refinements_reject(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/reject")


# --- equipment subcommands -------------------------------------------------


def _cmd_equipment_list(args: argparse.Namespace) -> None:
    _do_request(args, "GET", "/equipment")


def _cmd_equipment_equip(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    _do_request(
        args,
        "POST",
        f"/prompt-refinements/{args.topic_id}/{version}/equip",
        body=_placement_body(args) or {},
    )


def _cmd_equipment_create(args: argparse.Namespace) -> None:
    import gear_create

    body = None
    if args.text is not None:
        body = {"text": args.text, "equip": not args.no_equip}
        for key in ("scope", "topic_id", "slot", "rarity", "theme"):
            if getattr(args, key):
                body[key] = getattr(args, key)
        if args.replace:
            body["replace"] = {"topic_id": args.replace[0], "version": args.replace[1]}
        if args.no_announce:
            body["announce"] = False
    code = gear_create.create(_review_api(args), body, input)
    if code:
        raise SystemExit(code)


def _cmd_equipment_announce(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/announce")


def _cmd_equipment_delete(args: argparse.Namespace) -> None:
    import gear_create

    code = gear_create.delete(_review_api(args), args.topic_id, args.version, input, assume_yes=args.yes)
    if code:
        raise SystemExit(code)


def _cmd_equipment_bump(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    body = {"rarity": args.to} if args.to else {}
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/rarity", body=body)


def _cmd_equipment_repair(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    body = {"amount": args.amount} if args.amount else {}
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/repair", body=body)


def _cmd_equipment_unequip(args: argparse.Namespace) -> None:
    version = urllib.parse.quote(args.version, safe="")
    _do_request(args, "POST", f"/prompt-refinements/{args.topic_id}/{version}/unequip")


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
        "--research-interval-hours",
        dest="research_interval_hours",
        default=None,
        help=(
            "Whole hours between real research runs (the --research-cadence schedule is only "
            "the heartbeat). Omit to inherit the pipeline-wide default"
        ),
    )
    create_parser.add_argument(
        "--review-mode",
        dest="review_mode",
        default=None,
        choices=["off", "shadow", "enforce"],
        help="This topic's fresh-data review mode; omit to inherit the pipeline-wide mode",
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
        "--research-interval-hours",
        dest="research_interval_hours",
        default=None,
        help=(
            "Whole hours between real research runs for this topic; '' clears it "
            "(inherit the pipeline-wide default)"
        ),
    )
    update_parser.add_argument(
        "--review-mode",
        dest="review_mode",
        default=None,
        choices=["off", "shadow", "enforce", ""],
        help=(
            "This topic's fresh-data review mode, overriding the pipeline-wide one: 'off', "
            "'shadow' (record only) or 'enforce' (act on it); '' clears it (inherit)"
        ),
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

    rewrite_parser = articles_sub.add_parser(
        "rewrite",
        help=(
            "Rewrite an article to fix what you say is wrong with it. A published article is "
            "taken down first; the rewrite goes through the reviews again and waits in the inbox"
        ),
    )
    rewrite_parser.add_argument("article_id")
    rewrite_parser.add_argument(
        "--instructions",
        "-i",
        required=True,
        help='What is wrong with the article, e.g. "the second section confuses staking with lending"',
    )
    rewrite_parser.add_argument(
        "--model",
        dest="model_id",
        help="A registered model to rewrite with (admin_cli models list). Default: the topic's model",
    )
    rewrite_parser.set_defaults(func=_cmd_articles_rewrite)

    feedback_lock_parser = articles_sub.add_parser(
        "feedback-lock",
        help="Stop taking feedback on one article (its feedback_locked flag, set to true)",
    )
    feedback_lock_parser.add_argument("article_id")
    feedback_lock_parser.set_defaults(func=_cmd_articles_feedback_lock)

    feedback_unlock_parser = articles_sub.add_parser(
        "feedback-unlock",
        help=(
            "Take feedback on one article again (feedback_locked false). An article locked "
            "because it reached its limit also needs --reset-count, or it stays at the limit"
        ),
    )
    feedback_unlock_parser.add_argument("article_id")
    feedback_unlock_parser.add_argument(
        "--reset-count",
        dest="reset_count",
        action="store_true",
        default=False,
        help="Also zero the article's feedback count, so it has its full allowance again",
    )
    feedback_unlock_parser.set_defaults(func=_cmd_articles_feedback_unlock)

    feedback_config_parser = subparsers.add_parser(
        "feedback-config",
        help="When BloggerBear takes feedback: lockdown, rate limit, daily limit, per-article limit",
    )
    feedback_config_sub = feedback_config_parser.add_subparsers(dest="action", required=True)
    feedback_config_sub.add_parser(
        "get", help="Show the feedback settings, the ones in force, and today's usage"
    ).set_defaults(func=_cmd_feedback_config_get)
    feedback_config_set = feedback_config_sub.add_parser(
        "set", help="Set feedback settings (any of them; a setting not sent is unchanged)"
    )
    feedback_config_set.add_argument(
        "--locked-down",
        dest="locked_down",
        default=None,
        help="true stops taking feedback site-wide (the page says so); false reopens; '' clears",
    )
    feedback_config_set.add_argument(
        "--lockdown-reason",
        dest="lockdown_reason",
        default=None,
        help="What the page says while locked down (up to 100 characters); '' clears it",
    )
    feedback_config_set.add_argument(
        "--rate-limit",
        dest="rate_limit",
        default=None,
        help="At most this many pieces of feedback per rate window (default 20); '' clears it",
    )
    feedback_config_set.add_argument(
        "--rate-window-minutes",
        dest="rate_window_minutes",
        default=None,
        help="The rate window, in minutes (default 5); '' clears it",
    )
    feedback_config_set.add_argument(
        "--daily-limit",
        dest="daily_limit",
        default=None,
        help="At most this many a day, resetting at the start of the day (default 100)",
    )
    feedback_config_set.add_argument(
        "--article-limit",
        dest="article_limit",
        default=None,
        help="At most this many on one article before it is locked (default 50)",
    )
    feedback_config_set.add_argument(
        "--screening-limit",
        dest="screening_limit",
        default=None,
        help=(
            "At most this many comments a day are sent to the model check (default 300). "
            "Rejected feedback does not count against the other limits, so this bounds its cost"
        ),
    )
    feedback_config_set.add_argument(
        "--verification-required",
        dest="verification_required",
        default=None,
        help=(
            "true (the default) requires the token from the feedback-status call on every "
            "submission; false switches that off, in an emergency; '' clears it"
        ),
    )
    feedback_config_set.add_argument(
        "--token-delay-min-ms",
        dest="token_delay_min_ms",
        default=None,
        help="A token becomes valid at least this many ms after it is issued (default 500)",
    )
    feedback_config_set.add_argument(
        "--token-delay-max-ms",
        dest="token_delay_max_ms",
        default=None,
        help="...and at most this many (default 2000); each token gets a random time between",
    )
    feedback_config_set.add_argument(
        "--pow-threshold-percent",
        dest="pow_threshold_percent",
        default=None,
        help=(
            "When the site is this percent full (of its daily or rate limit), tokens need proof "
            "of work (default 70)"
        ),
    )
    feedback_config_set.add_argument(
        "--pow-difficulty-bits",
        dest="pow_difficulty_bits",
        default=None,
        help="How much work: leading zero bits, about a second or two at 16 (default 16); 0 = never",
    )
    feedback_config_set.add_argument(
        "--daily-timezone",
        dest="daily_timezone",
        default=None,
        help="Where a day starts, an IANA zone (default Australia/Sydney)",
    )
    feedback_config_set.set_defaults(func=_cmd_feedback_config_set)


    inbox_parser = subparsers.add_parser(
        "inbox", help="What is waiting for you (articles, prompt changes, failed runs), at a glance"
    )
    inbox_parser.add_argument(
        "--mock", action="store_true", default=False, help="Made-up numbers; no AWS needed"
    )
    inbox_parser.set_defaults(func=_cmd_inbox)

    review_parser_cli = subparsers.add_parser(
        "approve",
        help=(
            "Go through what is waiting for you, one keystroke each: y approve, r reject, "
            "z skip, v read it all, q quit. Up to 30 at a time; run it again for the next batch"
        ),
    )
    review_parser_cli.add_argument(
        "--limit", type=int, default=30, help="How many to go through this time (default 30)"
    )
    review_parser_cli.add_argument(
        "--source",
        choices=["all", "moderation", "refinements"],
        default="all",
        help="Only articles (moderation) or only prompt changes (refinements). Default: both",
    )
    review_parser_cli.add_argument(
        "--skip-hours",
        dest="skip_hours",
        type=int,
        default=24,
        help=(
            "A skipped item is hidden from your next reviews for this many hours "
            "(default 24; 0 = until cleared)"
        ),
    )
    review_parser_cli.add_argument(
        "--include-skipped",
        dest="include_skipped",
        action="store_true",
        default=False,
        help="Show items you skipped earlier too",
    )
    review_parser_cli.add_argument(
        "--reset-skipped",
        dest="reset_skipped",
        action="store_true",
        default=False,
        help="Forget everything you skipped before starting",
    )
    review_parser_cli.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=False,
        help="Go through the motions but change nothing",
    )
    review_parser_cli.add_argument(
        "--mock",
        action="store_true",
        default=False,
        help=(
            "Practise the keys on made-up items: no AWS, nothing sent "
            "(or set BLOGGERBEAR_REVIEW_MOCK=1)"
        ),
    )
    review_parser_cli.set_defaults(func=_cmd_approve)

    pipeline_config_parser = subparsers.add_parser(
        "pipeline-config",
        help="Pipeline-wide settings (the default research interval, the draft review mode)",
    )
    pipeline_config_sub = pipeline_config_parser.add_subparsers(dest="action", required=True)
    pipeline_config_sub.add_parser("get", help="Show the pipeline-wide settings").set_defaults(
        func=_cmd_pipeline_config_get
    )
    pipeline_config_set = pipeline_config_sub.add_parser(
        "set", help="Set pipeline-wide settings (send either or both; a setting not sent is unchanged)"
    )
    pipeline_config_set.add_argument(
        "--research-interval-hours",
        dest="research_interval_hours",
        default=None,
        help="Whole hours between real research runs (1-168); '' clears it (back to 1)",
    )
    pipeline_config_set.add_argument(
        "--review-mode",
        dest="review_mode",
        default=None,
        choices=["off", "shadow", "enforce", ""],
        help=(
            "Fresh-data review of drafts: 'shadow' runs and records it without changing any "
            "outcome (the default), 'enforce' acts on it (revises minor problems, holds major "
            "ones), 'off' skips it; '' clears it (back to the default). A topic's own "
            "--review-mode overrides this"
        ),
    )
    pipeline_config_set.add_argument(
        "--review-on-unavailable",
        dest="review_on_unavailable",
        default=None,
        choices=["hold", "note", ""],
        help=(
            "In enforce mode, when the review could not run: 'hold' the article for a person "
            "(the default) or 'note' the gap and publish; '' clears it"
        ),
    )
    pipeline_config_set.set_defaults(func=_cmd_pipeline_config_set)

    review_parser = subparsers.add_parser(
        "review", help="How the fresh-data review of drafts is doing"
    )
    review_sub = review_parser.add_subparsers(dest="action", required=True)
    review_report_parser = review_sub.add_parser(
        "report",
        help=(
            "Counts by outcome and topic, and what enforcement would have held and "
            "revised, from the records stored on articles"
        ),
    )
    review_report_parser.add_argument(
        "--sample",
        type=int,
        default=None,
        help="How many recent flagged claims to include for checking by eye (default 10, max 50)",
    )
    review_report_parser.set_defaults(func=_cmd_review_report)

    lineage_parser = subparsers.add_parser("lineage", help="Audit and repair article lineage/cost data")
    lineage_sub = lineage_parser.add_subparsers(dest="action", required=True)
    lineage_sub.add_parser(
        "audit",
        help="Show articles with no lineage or no cost, and models with no known price",
    ).set_defaults(func=_cmd_lineage_audit)
    backfill_parser = lineage_sub.add_parser(
        "backfill",
        help=(
            "Recompute each article's cost from its recorded tokens at today's prices "
            "(a dry run unless --apply)"
        ),
    )
    backfill_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the recomputed lineage (default: only report what would change)",
    )
    backfill_parser.set_defaults(func=_cmd_lineage_backfill)

    stats_parser = subparsers.add_parser("stats", help="Observability: one-time catch-up jobs")
    stats_sub = stats_parser.add_subparsers(dest="action", required=True)
    stats_backfill_parser = stats_sub.add_parser(
        "backfill-articles",
        help=(
            "Fold every existing article's already-recorded cost into StatsHistory's all-time "
            "total (a dry run unless --apply; refuses to run twice)"
        ),
    )
    stats_backfill_parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write the totals onto StatsHistory (default: only report what they would be)",
    )
    stats_backfill_parser.set_defaults(func=_cmd_stats_backfill_articles)

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
    _add_placement_arguments(refinements_approve_parser, ("topic", "global", "backpack"))
    refinements_approve_parser.set_defaults(func=_cmd_refinements_approve)

    refinements_reject_parser = refinements_sub.add_parser(
        "reject", help="Reject a pending prompt refinement"
    )
    refinements_reject_parser.add_argument("topic_id")
    refinements_reject_parser.add_argument("version")
    refinements_reject_parser.set_defaults(func=_cmd_refinements_reject)

    equipment_parser = subparsers.add_parser(
        "equipment",
        help="What the bear wears: armor (global guidance), rings (topic guidance), the backpack",
    )
    equipment_sub = equipment_parser.add_subparsers(dest="action", required=True)

    equipment_list_parser = equipment_sub.add_parser(
        "list", help="Show every slot, the rings, and what is in the backpack"
    )
    equipment_list_parser.set_defaults(func=_cmd_equipment_list)

    equipment_equip_parser = equipment_sub.add_parser(
        "equip",
        help="Wear an approved refinement (from the backpack or elsewhere); it replaces what is in the slot",
    )
    equipment_equip_parser.add_argument("topic_id")
    equipment_equip_parser.add_argument("version")
    _add_placement_arguments(equipment_equip_parser, ("topic", "global"))
    equipment_equip_parser.set_defaults(func=_cmd_equipment_equip)

    equipment_create_parser = equipment_sub.add_parser(
        "create",
        help=(
            "Make a new piece of gear yourself and (by default) put it on. With no --text it asks you "
            "what it needs, one question at a time"
        ),
    )
    equipment_create_parser.add_argument(
        "--text", default=None, help="what the gear tells BloggerBear to do (omit to be asked)"
    )
    equipment_create_parser.add_argument(
        "--scope",
        choices=["global", "topic"],
        default=None,
        help="global: armor for every topic; topic: a ring",
    )
    equipment_create_parser.add_argument(
        "--topic-id", dest="topic_id", default=None, help="the topic, for a ring (implies --scope topic)"
    )
    equipment_create_parser.add_argument(
        "--slot",
        choices=["helmet", "chest", "gloves", "boots", "sword", "shield"],
        default=None,
        help="the armor slot (default: the bear suggests one)",
    )
    equipment_create_parser.add_argument(
        "--rarity",
        choices=["common", "uncommon", "rare", "epic", "legendary"],
        default=None,
        help="the rarity (default: rolled at random, like a drop)",
    )
    equipment_create_parser.add_argument(
        "--theme",
        default=None,
        help="two to four words for its name, e.g. 'Plain Speaking' (default: the bear names it)",
    )
    equipment_create_parser.add_argument(
        "--no-equip",
        dest="no_equip",
        action="store_true",
        default=False,
        help="create it into the backpack, not worn",
    )
    equipment_create_parser.add_argument(
        "--replace",
        nargs=2,
        metavar=("TOPIC_ID", "VERSION"),
        default=None,
        help="when all rings are worn, the ring this one replaces",
    )
    equipment_create_parser.add_argument(
        "--no-announce",
        dest="no_announce",
        action="store_true",
        default=False,
        help="do not post a loot drop to the Musings",
    )
    equipment_create_parser.set_defaults(func=_cmd_equipment_create)

    equipment_announce_parser = equipment_sub.add_parser(
        "announce",
        help="Post a loot drop to the Musings for a piece of gear (again, if it was already announced)",
    )
    equipment_announce_parser.add_argument("topic_id")
    equipment_announce_parser.add_argument("version")
    equipment_announce_parser.set_defaults(func=_cmd_equipment_announce)

    equipment_delete_parser = equipment_sub.add_parser(
        "delete",
        help="Delete a piece of gear for good (it asks first). Articles written with it keep their record",
    )
    equipment_delete_parser.add_argument("topic_id")
    equipment_delete_parser.add_argument("version")
    equipment_delete_parser.add_argument("--yes", action="store_true", default=False, help="do not ask")
    equipment_delete_parser.set_defaults(func=_cmd_equipment_delete)

    equipment_bump_parser = equipment_sub.add_parser(
        "bump",
        help="Raise a piece of gear's rarity (only up): a higher maximum durability, one step by default",
    )
    equipment_bump_parser.add_argument("topic_id")
    equipment_bump_parser.add_argument("version")
    equipment_bump_parser.add_argument(
        "--to",
        choices=["uncommon", "rare", "epic", "legendary"],
        default=None,
        help="the rarity to raise it to (default: one step up)",
    )
    equipment_bump_parser.set_defaults(func=_cmd_equipment_bump)

    equipment_repair_parser = equipment_sub.add_parser(
        "repair",
        help="Restore a piece of gear's durability (all of it by default), never above its maximum",
    )
    equipment_repair_parser.add_argument("topic_id")
    equipment_repair_parser.add_argument("version")
    equipment_repair_parser.add_argument(
        "--amount", type=int, default=None, help="how many points to restore (default: to full)"
    )
    equipment_repair_parser.set_defaults(func=_cmd_equipment_repair)

    equipment_unequip_parser = equipment_sub.add_parser(
        "unequip", help="Take a worn refinement off; it goes to the backpack and is no longer used"
    )
    equipment_unequip_parser.add_argument("topic_id")
    equipment_unequip_parser.add_argument("version")
    equipment_unequip_parser.set_defaults(func=_cmd_equipment_unequip)

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
