"""`table_sample`: the newest row of one of the project's tables, and whether it is being written as
expected. "What's in bloggerbear-dev-candidate-ideas?", "are candidate ideas working?".

**Which table.** The name is resolved by architecture.py, so the operator can paste it from either
environment ("bloggerbear-prod-candidate-ideas" in dev reads dev's) or say it in words. A name for
neither environment (`data_allowed` false) is refused: its table is described, and nothing is read.

**The rule for what may be read is the tags, checked twice.**

1. IAM (infra/modules/ops-assistant/main.tf, SampleTaggedTables): reads on a <prefix>-* table
   are allowed only when it carries the project's default tags (ManagedBy, Project) and an
   Environment this assistant may read; the Deny in isolation.tf refuses every other Environment.
2. Here, before any row is read: the table's own tags are listed and compared with the default
   tags the function is given (OPS_DEFAULT_TAGS, the root's provider default_tags, the same values
   IAM's conditions are built from), and anything that differs is refused. Dev reads dev's alone;
   production reads production's and what is shared; dev never reads what is shared.

**Fields that are never read.** SecurityEvents' `untrusted` holds what a blocked client sent (the
path and the matched text: an attack payload, by definition), and `client_hash` identifies a
client. The read names the fields it wants (a ProjectionExpression from SECURITY_EVENT_FIELDS), so
those two are never fetched from DynamoDB at all; FORBIDDEN_FIELDS removes them again if they
somehow arrive. A field can only be added to the allowlist on purpose.

**No personal data out.** Every value shown passes through `redact`: e-mail addresses and IP
addresses become placeholders, fields that identify a person (a user id, an address) are replaced
whole, and text is cut short and stripped of control characters (common/security_events.
untrusted_text). The values go under `untrusted`, never into `spoken`: the spoken answer says how
old the newest row is and whether writes look on time, nothing a row contains.

**Cheap.** One ListTagsOfResource, then a Query with Limit 1 per topic, or per status on an index,
or a Scan capped at SCAN_MAX_ITEMS. The tags come from the environment: no lookup at all.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import BotoCoreError, ClientError

from common.dynamo import get_pipeline_config, get_table, list_topics
from common.research_schedule import resolve_interval_hours
from common.security_events import untrusted_text
from ops_mcp import architecture
from ops_mcp.suggestions import ID_PATTERN
from ops_mcp.tools import DAY_WINDOW, _age, _join, _parse, _topic_label

DEFAULT_TAGS_ENV = "OPS_DEFAULT_TAGS"
READABLE_ENVIRONMENTS_ENV = "OPS_READABLE_ENVIRONMENTS"
REQUIRED_TAG_KEYS = ("ManagedBy", "Project")

ROWS_DEFAULT = 1
ROWS_MAX = 3
SCAN_PAGE = 100
SCAN_MAX_ITEMS = 300
TOPICS_MAX = 50
VALUE_MAX_CHARS = 300
FIELDS_MAX = 30

# Tables whose rows are per topic, newest last in the sort key: the newest is a Query per topic.
# Each with how stale the newest row of a topic may be before it is worth saying (None: no rule).
_PER_TOPIC = {
    "findings": "captured_at",
    "candidate-ideas": "created_at",
    "prompt-refinements": "version",
}
# Tables with a status index sorted by time: the newest is a Query per status.
_BY_STATUS = {
    "articles": ("by_status_created_at", ("published", "pending_moderation", "rejected"), "created_at"),
    "moderation-queue": (
        "by_status_created_at",
        ("pending", "approved", "rejected", "rewriting", "rewrite_failed"),
        "created_at",
    ),
    "security-events": ("by_status_last_seen", ("open", "acknowledged", "resolved"), "last_seen"),
}
# For any other table: the newest of a capped Scan, by the first of these it has.
_TIME_FIELDS = ("created_at", "captured_at", "last_seen", "updated_at", "proposed_at", "week_start")

# SecurityEvents: the only fields ever fetched. NOT `untrusted` (the blocked client's own path and
# matched text: an attack payload) and NOT `client_hash` (it identifies a client). Read by name, so
# DynamoDB never returns the others.
SECURITY_EVENT_FIELDS = (
    "event_id",
    "environment",
    "source",
    "rule",
    "category",
    "severity",
    "status",
    "country",
    "method",
    "window_start",
    "first_seen",
    "last_seen",
    "request_count",
    "suggested_next_steps",
    "analysis",
    "alerted_at",
)
# Removed from any row of these tables even if they arrive: belt and braces under the projection.
FORBIDDEN_FIELDS = {"security-events": frozenset({"untrusted", "client_hash"})}
# Tables never read, whatever their tags. ops-briefings holds what the agent wrote after reading
# hostile text; infra/modules/ops-assistant/briefings.tf keeps it out of the agent's reach so it is
# never put back in front of it, and a row read here would go straight to the agent.
NEVER_SAMPLED = {
    "ops-briefings": "That table holds what the assistant itself wrote after reading untrusted text, "
    "and is never shown back to it.",
}
# Fields that identify a person, on any table: replaced whole, never shown.
_PERSONAL_FIELDS = re.compile(
    r"^(user_id|client_hash|client_ip|source_ip|ip|ip_address|email|e_mail|phone|username|sub)$", re.I
)
REDACTED = "[redacted]"

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![0-9A-Za-z:])")

_dynamodb_client = None
_sts_client = None
_account: list[str] = []


def _dynamodb():
    global _dynamodb_client
    if _dynamodb_client is None:
        _dynamodb_client = boto3.client("dynamodb")
    return _dynamodb_client


def _account_id() -> str:
    """This account's id, for a table's ARN. GetCallerIdentity needs no permission."""
    global _sts_client
    if not _account:
        if _sts_client is None:
            _sts_client = boto3.client("sts")
        _account.append(_sts_client.get_caller_identity()["Account"])
    return _account[0]


# --- what may be read -------------------------------------------------------------------------------


class NotAllowed(Exception):
    """Reading this table is refused. The message is fixed words of ours, safe to speak."""


def default_tags() -> dict[str, str]:
    """The tags a table must carry, as the function was given them (OPS_DEFAULT_TAGS: the root's
    provider default_tags, ManagedBy and Project). Anything else refuses every read."""
    try:
        tags = json.loads(os.environ.get(DEFAULT_TAGS_ENV, ""))
    except ValueError as exc:
        raise NotAllowed("I haven't been told the project's default tags, so I won't read tables.") from exc
    if (
        not isinstance(tags, dict)
        or set(tags) != set(REQUIRED_TAG_KEYS)
        or not all(isinstance(value, str) and value for value in tags.values())
    ):
        raise NotAllowed("The project's default tags aren't in the shape I expect, so I won't read tables.")
    return tags


def readable_environments(env: str) -> list[str]:
    """What this assistant may read: what its module told it AND the owner's rule (only production
    reads "shared"), so neither can widen the other."""
    told = [e for e in os.environ.get(READABLE_ENVIRONMENTS_ENV, "").split(",") if e]
    rule = [env, "shared"] if env == "production" else [env]
    return [e for e in rule if e in told]


def check_tags(table_name: str, env: str, required: dict[str, str]) -> dict:
    """The table's own tags, if they allow reading it; otherwise NotAllowed."""
    where = architecture.region() or boto3.session.Session().region_name
    arn = f"arn:aws:dynamodb:{where}:{_account_id()}:table/{table_name}"
    try:
        listed = _dynamodb().list_tags_of_resource(ResourceArn=arn).get("Tags", [])
    except ClientError as exc:
        raise _refused(exc) from exc
    tags = {tag["Key"]: tag["Value"] for tag in listed}
    for key, value in required.items():
        if tags.get(key) != value:
            raise NotAllowed(f"That table isn't tagged {key} = {value}, so I won't read it.")
    if tags.get("Environment") not in readable_environments(env):
        raise NotAllowed("That table is tagged for an environment I'm not allowed to read.")
    return {key: tags[key] for key in (*REQUIRED_TAG_KEYS, "Environment")}


def _refused(exc: ClientError) -> NotAllowed:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "ResourceNotFoundException":
        return NotAllowed("That table doesn't exist in this environment.")
    if code in ("AccessDeniedException", "UnauthorizedOperation"):
        return NotAllowed(
            "AWS refused to let me read that table. Either its tags don't allow it, or tag-based "
            "access control is off for DynamoDB in this region (the DynamoDB console's Settings page)."
        )
    return NotAllowed("I couldn't read that table.")


# --- what is shown ----------------------------------------------------------------------------------


def redact(value: Any, key: str = "") -> Any:
    """A stored value as it may be shown: personal fields replaced whole, e-mail and IP addresses
    replaced inside text, text cut short and cleaned, numbers as numbers, structures as short JSON."""
    if key and _PERSONAL_FIELDS.match(key):
        return REDACTED
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, int | float):
        return value
    if isinstance(value, dict):
        value = {k: redact(v, str(k)) for k, v in list(value.items())[:FIELDS_MAX]}
        return _redact_text(json.dumps(value, default=str, ensure_ascii=False))
    if isinstance(value, list | tuple | set):
        value = [redact(v) for v in list(value)[:FIELDS_MAX]]
        return _redact_text(json.dumps(value, default=str, ensure_ascii=False))
    return _redact_text(str(value))


def _redact_text(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _IPV4.sub(lambda m: "[ip]" if _is_ip(m.group(0)) else m.group(0), text)
    text = _IPV6.sub(lambda m: "[ip]" if _is_ip(m.group(0)) else m.group(0), text)
    return untrusted_text(text, VALUE_MAX_CHARS) or ""


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def shown(row: dict, table_key: str) -> dict:
    """One row as it may leave this module: forbidden fields gone, everything else redacted."""
    forbidden = FORBIDDEN_FIELDS.get(table_key, frozenset())
    return {
        key: redact(value, key)
        for key, value in sorted(row.items())[: FIELDS_MAX + len(forbidden)]
        if key not in forbidden
    }


# --- reading ----------------------------------------------------------------------------------------


def _projection(table_key: str) -> dict:
    """The arguments that limit a read to SECURITY_EVENT_FIELDS, for SecurityEvents; none else."""
    if table_key != "security-events":
        return {}
    names = {f"#f{n}": field for n, field in enumerate(SECURITY_EVENT_FIELDS)}
    return {"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}


def _newest_per_topic(table, table_key: str, topic: str | None) -> tuple[list[dict], list[dict]]:
    """The newest row of each topic (or of the one asked about), newest first, and every topic
    looked at, with or without a row."""
    sort_key = _PER_TOPIC[table_key]
    topics = [{"topic_id": topic}] if topic else sorted(list_topics(), key=lambda t: t.get("topic_id") or "")
    rows, looked = [], []
    for item in topics[:TOPICS_MAX]:
        topic_id = item.get("topic_id")
        if not topic_id:
            continue
        found = table.query(
            KeyConditionExpression=Key("topic_id").eq(topic_id), ScanIndexForward=False, Limit=1
        ).get("Items", [])
        looked.append({"topic": item, "newest": found[0] if found else None, "sort_key": sort_key})
        rows.extend(found)
    rows.sort(key=lambda row: str(row.get(sort_key) or ""), reverse=True)
    return rows, looked


def _newest_by_status(table, table_key: str) -> list[dict]:
    index, statuses, sort_key = _BY_STATUS[table_key]
    rows = []
    for status in statuses:
        rows.extend(
            table.query(
                IndexName=index,
                KeyConditionExpression=Key("status").eq(status),
                ScanIndexForward=False,
                Limit=1,
                **_projection(table_key),
            ).get("Items", [])
        )
    rows.sort(key=lambda row: str(row.get(sort_key) or ""), reverse=True)
    return rows


def _newest_scanned(table, table_key: str) -> tuple[list[dict], bool]:
    """The rows of a capped Scan, newest first by the first time field they have, and whether the
    Scan stopped before the end of the table."""
    rows: list[dict] = []
    arguments: dict = {"Limit": SCAN_PAGE, **_projection(table_key)}
    while True:
        page = table.scan(**arguments)
        rows.extend(page.get("Items", []))
        last = page.get("LastEvaluatedKey")
        if not last or len(rows) >= SCAN_MAX_ITEMS:
            break
        arguments["ExclusiveStartKey"] = last
    field = next((f for f in _TIME_FIELDS if any(f in row for row in rows)), None)
    if field:
        rows.sort(key=lambda row: str(row.get(field) or ""), reverse=True)
    return rows, bool(last)


# --- is it working ----------------------------------------------------------------------------------


def _freshness(table_key: str, looked: list[dict], now: datetime) -> list[dict]:
    """Per topic: how old its newest row is, and whether that is on time. Findings are written each
    research interval (late after two); candidate ideas each daily run (late after a day and two
    hours, as pipeline_health's window). Other tables have no rule."""
    if table_key not in ("findings", "candidate-ideas"):
        return []
    config = get_pipeline_config() if table_key == "findings" else None
    out = []
    for entry in looked:
        topic, newest = entry["topic"], entry["newest"]
        at = _parse((newest or {}).get(entry["sort_key"]))
        if table_key == "findings":
            allowed = timedelta(hours=resolve_interval_hours(topic, config) * 2)
        else:
            allowed = DAY_WINDOW
        out.append(
            {
                "topic_id": topic.get("topic_id"),
                "topic": _topic_label(topic, topic.get("topic_id") or ""),
                "newest_at": at.isoformat() if at else None,
                "age": _age(at, now) if at else None,
                "on_time": bool(at and now - at <= allowed),
                "expected_within_hours": round(allowed.total_seconds() / 3600, 1),
            }
        )
    return out


# --- the tool ---------------------------------------------------------------------------------------


def _refusal(spoken: str, **more) -> dict:
    return {"spoken": spoken, "findings": [], "read": False, **more}


def table_sample(
    name: str, topic: str | None = None, rows: int = ROWS_DEFAULT, *, now: datetime | None = None
) -> dict:
    """The newest rows (1 to 3) of one of the project's tables in this environment, redacted, and
    for findings and candidate ideas whether each topic's newest is on time."""
    now = now or datetime.now(UTC)
    env = architecture.environment()
    if env is None:
        return _refusal("I haven't been told which environment I'm for, so I won't read tables.")
    if topic is not None and not ID_PATTERN.match(str(topic)):
        return _refusal("That isn't a topic id I can look up.")
    count = min(max(int(rows), 1), ROWS_MAX)

    resolved = architecture.resolve(name, kind="table") if isinstance(name, str) else None
    if not resolved or not resolved.matches:
        return _refusal("I don't know a table by that name. The architecture tool lists them.")
    component = resolved.matches[0]
    table_name = architecture.fill(component.name, env)
    rewritten = resolved.asked_env in architecture.ENVIRONMENTS and resolved.asked_env != env
    if resolved.asked_env is not None and resolved.asked_env not in architecture.ENVIRONMENTS:
        return _refusal(
            f"That name isn't for either environment I know, so I won't read data for it. "
            f"{table_name} is {_lower(component.purpose)}",
            table_name=table_name,
            data_allowed=False,
        )
    if component.key in NEVER_SAMPLED:
        return _refusal(NEVER_SAMPLED[component.key], table_name=table_name)
    if not architecture.exists_in(component, env):
        return _refusal(f"{table_name} isn't deployed in {env}.", table_name=table_name)
    if topic is not None and component.key not in _PER_TOPIC:
        topic = None  # only the per-topic tables are keyed by topic

    try:
        tags = check_tags(table_name, env, default_tags())
        table = get_table(table_name)
        looked: list[dict] = []
        partial = False
        if component.key in _PER_TOPIC:
            found, looked = _newest_per_topic(table, component.key, topic)
        elif component.key in _BY_STATUS:
            found = _newest_by_status(table, component.key)
        else:
            found, partial = _newest_scanned(table, component.key)
    except NotAllowed as refused:
        return _refusal(str(refused), table_name=table_name)
    except ClientError as exc:
        return _refusal(str(_refused(exc)), table_name=table_name)
    except BotoCoreError:
        return _refusal("I couldn't reach DynamoDB just now.", table_name=table_name)

    sample = [shown(row, component.key) for row in found[:count]]
    freshness = _freshness(component.key, looked, now)
    spoken = _spoken(table_name, component, sample, freshness, rewritten, env)
    return {
        "spoken": spoken,
        "findings": [],
        "read": True,
        "environment": env,
        "table_name": table_name,
        "rewritten": rewritten,
        "tags": tags,
        "rows_shown": len(sample),
        "scan_was_partial": partial,
        "withheld_fields": sorted(FORBIDDEN_FIELDS.get(component.key, ())),
        "freshness": freshness,
        # What the rows hold was written by the pipeline, by models, by readers or by attackers:
        # for the page, never for speech.
        "untrusted": {"rows": sample},
        "table": _table(table_name, sample, freshness, component.key),
    }


def _lower(text: str) -> str:
    return text[:1].lower() + text[1:]


def _spoken(
    table_name: str, component, sample: list[dict], freshness: list[dict], rewritten: bool, env: str
) -> str:
    parts = []
    if rewritten:
        parts.append(f"That name is the other environment's; this is {env}'s, {table_name}.")
    if not sample:
        parts.append(f"{table_name} has no rows I could find.")
    else:
        stamp = next((sample[0].get(f) for f in (*_TIME_FIELDS, "version") if sample[0].get(f)), None)
        at = _parse(stamp) if isinstance(stamp, str) else None
        when = f", from {_age(at, datetime.now(UTC))} ago" if at else ""
        parts.append(f"The newest row in {table_name} is on screen{when}.")
    if freshness:
        late = [f["topic"] for f in freshness if not f["on_time"]]
        what = "finding" if component.key == "findings" else "candidate idea"
        if late:
            parts.append(
                f"{_join(late)} {'has' if len(late) == 1 else 'have'} no recent {what}: "
                "that looks like something isn't running."
            )
        else:
            parts.append(f"Every topic has a recent {what}, so it looks to be working.")
    if component.key in FORBIDDEN_FIELDS:
        parts.append("What the blocked client sent is never read, and identifying fields are withheld.")
    return " ".join(parts)


def _table(table_name: str, sample: list[dict], freshness: list[dict], table_key: str) -> dict:
    rows: list[list] = []
    for number, row in enumerate(sample, start=1):
        prefix = f"Row {number}: " if len(sample) > 1 else ""
        rows.extend(
            [f"{prefix}{key}", value if isinstance(value, int | float) else str(value)]
            for key, value in row.items()
        )
    for field in sorted(FORBIDDEN_FIELDS.get(table_key, ())):
        rows.append([field, "withheld: never read"])
    for entry in freshness:
        state = (
            "on time" if entry["on_time"] else f"late (expected within {entry['expected_within_hours']} h)"
        )
        newest = f"newest {entry['age']} ago" if entry["age"] else "no rows"
        rows.append([f"Topic {entry['topic']}", f"{newest}, {state}"])
    return {
        "title": f"{table_name}: newest row{'s' if len(sample) > 1 else ''}",
        "columns": ["Field", "Value"],
        "rows": rows,
    }
