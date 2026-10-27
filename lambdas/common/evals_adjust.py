"""An operator's hands on continuous evals' scores: forgive or punish a model or a prompt version,
hold a model's gates open for a while, and leave a source's events out of the rollup
(docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.3 to 4.6, PR13b). The admin API's
POST /evals/adjust, POST /evals/ungate and POST /evals/revoke call these; `admin_cli evals forgive`,
`punish`, `ungate` and `revoke` send them.

**adjust** (Admin Forgive, Admin Punish) moves one model's or one prompt version's total by a whole
number, and no window. The gates read the windows (section 4.5), so a forgive cannot hide a bad week
and a punish cannot gate a model today; only the total, which culling reads (PR16), moves. It writes
the `adjust#model#<id>` or `adjust#prompt#<prompt_id>#<version>` row of the evals table itself
(`ts#<time>`: `delta`, `reason`, `by`), then emits the event, whose id is `adjust:<time>:<target>`
with the same time and the row's entity (less `adjust#`) as the target. The rollup scores nothing for
the event; it adds a model's adjust# rows to `total_perf` at its next run (common/evals_rollup.py).

The amount is at most 100 either way (section 4.3) unless `allow_cull` is sent; and a punish that
would take a model's total from at or above `total_gate` to below it, where the gates hold the model
and culling may take it, needs `allow_cull` whatever its size. The total it is judged by is the one
the next rollup would put on the models row: the model's day rows and adjust# rows, the judge's
verdicts counted as the last rollup counted them. The adjustment is checked as the event it will be
(evals.event) before anything is written, so a row never stands for an event the schema would
refuse; a model must be in the registry. The row is written only where none is (the same target at
the same microsecond), so one adjustment never replaces another.

**ungate** sets `ungated_until` on a models row for some hours (0 ends the override now): the
model's gates are held open until then (evals_settings.ungated). The write is conditional on the row
being there (dynamo.set_model_fields), so a culled model is never written back.

**revoke** appends `{source, since}` to the evals settings' `excluded_sources` (at most
evals_settings.MAX_EXCLUDED_SOURCES): the rollup leaves that source's events out from `since` on, or
all of them without it (evals_settings.excluded). The scheduled rollup adds up only the days after
its watermark, so days already final keep those events until `evals rollup --from` adds them up
again; the answer says so.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common import dynamo, evals, evals_rollup, evals_settings, evidence
from common.model_pricing import canonical_model_id

TARGETS = ("model", "prompt")
# The CLI's verbs and the reasons they write (section 4.3).
DIRECTIONS = {"forgive": "Admin Forgive", "punish": "Admin Punish"}
# Section 4.3: N <= 100, allow_cull for more. MAX_AMOUNT bounds even that, as the gates themselves
# are bounded (evals_settings): a slip of a few extra digits is refused, not written.
CAP = 100
MAX_AMOUNT = 1_000_000
MAX_UNGATE_HOURS = 720
# The events' `source` and `by`: the admin API is IAM-authenticated, so whoever sends it is a
# person (deviation 10: the role, never who).
SOURCE = "admin_api"
BY = "human"
ADJUST = "adjust#"
TS = "ts#"

# Why an adjustment's event would be refused, for the fields an operator sends.
_FIELD_ERRORS = {
    "bad_model_id": "'model_id' must be a model id",
    "bad_prompt_id": "'prompt_id' must be a prompt id: <lambda_id>/<prompt_name>, in lower case",
    "bad_prompt_version": "'version' must be a prompt version: v<N>, or vanilla:v<N>",
}
APPLIED = {
    "model": "total_perf on the models row moves at the next hourly rollup (`evals rollup` runs it now); "
    "no window moves, so no gate does",
    "prompt": "kept as this prompt version's adjustment: it counts in the version's total and in no window",
}
REVOKED = (
    "The hourly rollup leaves these events out of every day it adds up from now on. Days already final "
    "(up to its watermark, `evals status`) keep them until `evals rollup --from <day>` adds those days "
    "up again."
)


class Refused(Exception):
    """A request that is not carried out: `status` (400, 404 or 409) and why, in words."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _model_id(value) -> str | None:
    """A model id as the registry keys it (an inference profile's ARN gives its id), or None when
    `value` is not one."""
    value = canonical_model_id(value)
    if not isinstance(value, str) or value.endswith("\n") or evidence.check("model", value) is None:
        return None
    return value


def timestamp(now: datetime) -> str:
    """An adjustment's time, UTC to the microsecond: its row's `ts#` and its event's id and time."""
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def model_total(model_id: str, settings: dict, now: datetime, table=None) -> int:
    """The total the next rollup would put on a model's row (common/evals_rollup.model_windows):
    every day row and adjust# row it has, the judge's verdicts counted only if it was calibrated at
    the last rollup."""
    table = table or evals_rollup.evals_table()
    is_calibrated = evals_rollup.index_calibrated(evals_rollup.get_index(table), settings)
    today = evals_rollup.local_day(now, evals_settings.zone(settings))
    return evals_rollup.model_windows(table, model_id, today, is_calibrated)["total_perf"]


def _target(body: dict) -> tuple[str, dict]:
    """(the target's key, `model#<id>` or `prompt#<prompt_id>#<version>`; the event's fields that
    name it). Refused when a field is missing or belongs to the other kind of target."""
    target = body.get("target")
    if target not in TARGETS:
        raise Refused(400, "'target' must be model or prompt")
    others = ("prompt_id", "version") if target == "model" else ("model_id",)
    stray = [name for name in others if body.get(name) is not None]
    if stray:
        raise Refused(400, f"'{stray[0]}' does not go with target {target}")
    if target == "model":
        model_id = _model_id(body.get("model_id"))
        if model_id is None:
            raise Refused(400, "target model needs 'model_id', a model id")
        return f"model#{model_id}", {"model_id": model_id}
    prompt_id, version = body.get("prompt_id"), body.get("version")
    if not isinstance(prompt_id, str) or not isinstance(version, str):
        raise Refused(400, "target prompt needs 'prompt_id' and 'version'")
    return f"prompt#{prompt_id}#{version}", {"prompt_id": prompt_id, "prompt_version": version}


def adjust(body: dict, now: datetime | None = None) -> dict:
    """Forgive or punish one model or prompt version (see the module's docstring): write its
    adjust# row, then emit its event. What was done, the event's id and whether the event was
    written; for a model, its total before and after. Raises Refused."""
    now = now or datetime.now(UTC)
    key, fields = _target(body)
    direction = body.get("direction")
    if direction not in DIRECTIONS:
        raise Refused(400, "'direction' must be forgive or punish")
    allow_cull = body.get("allow_cull", False)
    if not isinstance(allow_cull, bool):
        raise Refused(400, "'allow_cull' must be true or false")
    amount = body.get("amount")
    if not _whole(amount) or not 1 <= amount <= MAX_AMOUNT:
        raise Refused(
            400, f"'amount' must be a whole number from 1 to {CAP} ({MAX_AMOUNT:,} with allow_cull)"
        )
    if amount > CAP and not allow_cull:
        raise Refused(
            400, f"'amount' may be at most {CAP} unless allow_cull is true (admin_cli: --allow-cull)"
        )

    reason = DIRECTIONS[direction]
    delta = amount * evals.SCORING[reason].sign
    ts = timestamp(now)
    event_id = evals.event_id(reason, ts=ts, target=key)
    event = {
        "event_id": event_id, "source": SOURCE, "rating_delta": delta, "by": BY, "timestamp": ts, **fields,
    }  # fmt: skip
    _, refused = evals.event(reason, **event)
    if refused:
        raise Refused(400, _FIELD_ERRORS.get(refused, f"not a valid {reason} event ({refused})"))

    table = evals_rollup.evals_table()
    totals = {}
    if "model_id" in fields:
        model_id = fields["model_id"]
        if dynamo.get_model(model_id) is None:
            raise Refused(404, f"no model '{model_id}' in the registry (admin_cli models list)")
        settings = evals_settings.get_settings()
        gate = settings["total_gate"]
        before = model_total(model_id, settings, now, table)
        after = before + delta
        if after < gate <= before and not allow_cull:
            raise Refused(
                400,
                f"punishing '{model_id}' by {amount} would take its total from {before} to {after}, below "
                f"total_gate ({gate}), where the gates hold a model and culling may take it: send "
                "allow_cull: true (admin_cli: --allow-cull) to do it anyway",
            )
        totals = {"total_perf_before": before, "total_perf": after, "total_gate": gate}

    item = {"entity": f"{ADJUST}{key}", "sk": f"{TS}{ts}", "delta": delta, "reason": reason, "by": BY}
    try:
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sk)")
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise Refused(
            409, "an adjustment of this target at this moment is already written: send it again"
        ) from None
    written = evals.emit(reason, **event)

    if "model_id" in fields:
        named = {"model_id": fields["model_id"]}
    else:
        named = {"prompt_id": fields["prompt_id"], "version": fields["prompt_version"]}
    return {
        "target": body["target"],
        **named,
        "direction": direction,
        "reason": reason,
        "delta": delta,
        "by": BY,
        "ts": ts,
        "event_id": event_id,
        "event_written": written,
        **totals,
        "applied": APPLIED[body["target"]],
    }


def ungate(body: dict, now: datetime | None = None) -> dict:
    """Hold a model's gates open for `hours` (0 ends an override now): `ungated_until` on its row,
    never on a row that is not there. Raises Refused."""
    now = now or datetime.now(UTC)
    model_id = _model_id(body.get("model_id"))
    if model_id is None:
        raise Refused(400, "'model_id' is required and must be a model id")
    hours = body.get("hours")
    if not _whole(hours) or not 0 <= hours <= MAX_UNGATE_HOURS:
        raise Refused(
            400, f"'hours' must be a whole number from 1 to {MAX_UNGATE_HOURS}, or 0 to end an override now"
        )
    until = (now + timedelta(hours=hours)).astimezone(UTC).isoformat(timespec="seconds") if hours else None
    if not dynamo.set_model_fields(model_id, {"ungated_until": until}, expected={}):
        raise Refused(404, f"no model '{model_id}' in the registry (admin_cli models list)")
    return {"model_id": model_id, "hours": hours, "ungated_until": until}


def _entry(entry) -> dict:
    """An `excluded_sources` entry as an object: a plain selector means `{source}`."""
    return {"source": entry} if isinstance(entry, str) else dict(entry)


def revoke(body: dict) -> dict:
    """Append `{source, since}` to `excluded_sources` (an entry already there is not added twice).
    The list in effect afterwards and the entry added (None when it was there). Raises Refused."""
    source = body.get("source")
    if source is None:
        raise Refused(400, "'source' is required: human, judge, system, run:<run id>, or a source's code")
    problem = evals_settings.source_selector_error(source)
    if problem:
        raise Refused(400, f"'source' {problem}")
    since = body.get("since")
    if since is not None and (
        not isinstance(since, str) or since.endswith("\n") or evidence.check("iso", since) is None
    ):
        raise Refused(400, "'since' must be an ISO time, e.g. 2026-10-27T00:00:00Z")
    entry = {"source": source, **({"since": since} if since is not None else {})}

    row = evals_settings.get_row()
    stored = row.get("excluded_sources")
    # The list in effect: a stored list that is not valid is already being ignored (the default, []).
    current = evals_settings.effective_settings(row)["excluded_sources"]
    if any(_entry(existing) == entry for existing in current):
        return {"excluded_sources": current, "added": None, "applied": REVOKED}
    updated = [*current, entry]
    if len(updated) > evals_settings.MAX_EXCLUDED_SOURCES:
        raise Refused(
            400,
            f"'excluded_sources' holds {evals_settings.MAX_EXCLUDED_SOURCES} entries already, the most it "
            "may: set the list again with `evals set --excluded-sources`",
        )
    problem = evals_settings.validate_setting("excluded_sources", updated)
    if problem:
        raise Refused(400, problem)
    replaced = dynamo.replace_config_field(
        evals_settings.CONFIG_ID, "excluded_sources", updated, expected=stored
    )
    if not replaced:
        raise Refused(409, "'excluded_sources' changed while this was being written: send it again")
    return {"excluded_sources": updated, "added": entry, "applied": REVOKED}
