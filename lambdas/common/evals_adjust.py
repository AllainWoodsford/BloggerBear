"""An operator's hands on continuous evals' scores: forgive or punish a model or a prompt version,
hold a model's gates open for a while, and leave a source's events out of the rollup
(docs/enhancements/model-ranch-prompt-assets-evals.md, sections 4.3 to 4.6, PR13b). The admin API's
POST /evals/adjust, POST /evals/ungate and POST /evals/revoke call these; `admin_cli evals forgive`,
`punish`, `ungate` and `revoke` send them.

**adjust** (Admin Forgive, Admin Punish) writes a forgive or punish of one model or one prompt
version, by a whole number, into no window (section 4.3's "total only"): a forgive cannot hide a bad
day or week from the daily and weekly gates, nor a punish put a model under them. It writes the
`adjust#model#<id>` or `adjust#prompt#<prompt_id>#<version>` row of the evals table itself
(`ts#<time>`: `delta`, `reason`, `by`), then emits the event, whose id is `adjust:<time>:<target>`
with the same time and the row's entity (less `adjust#`) as the target. The rollup scores nothing
for the event. A model's adjust# rows count in its `total_perf` from the rollup's next run
(common/evals_rollup.py), and the total gate and culling (PR16) read that total. A prompt version's
row is kept, and nothing adds prompt versions' adjustments up yet: no total, window or gate of a
prompt version moves.

The amount is at most 100 either way (section 4.3) unless `allow_cull` is sent (then MAX_AMOUNT).
For a model the answer shows its total before and after, as the next rollup would put it on the
models row (the model's day rows and adjust# rows, the judge's verdicts counted as the last rollup
counted them), and `total_gate` beside them: information only, which refuses nothing. The
adjustment is checked as the event it will be (evals.event) before anything is written, so a row
never stands for an event the schema would refuse. A model must be in the registry, whose row may be
kept under the model's id or under an inference profile's ARN for it (_registry_row); its adjust#
row and its event always name the canonical id, the key the rollup reads. The row is written only
where none is (the same target at the same microsecond), so one adjustment never replaces another.

**ungate** sets `ungated_until` on the model's registry row (found as an adjustment finds it) for
some hours (0 ends the override now): the model's gates are held open until then
(evals_settings.ungated). The write is conditional on the row being there
(dynamo.set_model_fields), so a culled model is never written back.

**revoke** appends `{source, since}` to the evals settings' `excluded_sources` (at most
evals_settings.MAX_EXCLUDED_SOURCES): the rollup leaves that source's events out from `since` on, or
all of them without it (evals_settings.excluded). The scheduled rollup adds up only the days after
its watermark, so the days already final would keep those events: the caller starts a rollup of the
days from the one `since` falls on (readd_days) to today, and the answer says it was started, or
gives the `evals rollup --from --to` command that does it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

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
# DynamoDB's longest partition key, in bytes: a longer id as sent is never looked up as it is.
KEY_BYTES = 2048

# Why an adjustment's event would be refused, for the fields an operator sends.
_FIELD_ERRORS = {
    "bad_model_id": "'model_id' must be a model id",
    "bad_prompt_id": "'prompt_id' must be a prompt id: <lambda_id>/<prompt_name>, in lower case",
    "bad_prompt_version": "'version' must be a prompt version: v<N>, or vanilla:v<N>",
}
APPLIED = {
    "model": "total_perf on the models row moves at the next hourly rollup (`evals rollup` runs it now); "
    "the daily, weekly and monthly windows never do",
    "prompt": "kept as this prompt version's adjust# row and its event; nothing adds prompt versions' "
    "adjustments up yet (the rollup totals only a model's), so no total, window or gate moves",
}
REVOKED = "The hourly rollup leaves these events out of every day it adds up from now on"


class Refused(Exception):
    """A request that is not carried out: `status` (400, 404 or 409) and why, in words."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _whole(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _model_id(value) -> str | None:
    """The canonical id `value` names (an inference profile's ARN gives its id): the one a model's
    evals rows and events are kept under. None when `value` is not a model id. The registry may key
    the model's row by either; _registry_row finds it."""
    value = canonical_model_id(value)
    if not isinstance(value, str) or value.endswith("\n") or evidence.check("model", value) is None:
        return None
    return value


def _registry_row(given: str, model_id: str) -> dict | None:
    """The registry row of the model whose canonical id is `model_id`: the row under the id as sent
    (`given`, an inference profile's ARN or the id itself), else the row under `model_id`, else the
    row whose own id is an ARN that names it (as the rollup and the digest match them). None when
    the registry has no row for it."""
    for key in dict.fromkeys((given, model_id)):
        if len(key.encode("utf-8")) <= KEY_BYTES:
            row = dynamo.get_model(key)
            if row is not None:
                return row
    return next(
        (row for row in dynamo.list_models() if canonical_model_id(row["model_id"]) == model_id), None
    )


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
    if not isinstance(direction, str) or direction not in DIRECTIONS:
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
        if _registry_row(body["model_id"], model_id) is None:
            raise Refused(404, f"no model '{model_id}' in the registry (admin_cli models list)")
        settings = evals_settings.get_settings()
        before = model_total(model_id, settings, now, table)
        totals = {
            "total_perf_before": before,
            "total_perf": before + delta,
            "total_gate": settings["total_gate"],
        }

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
    """Hold a model's gates open for `hours` (0 ends an override now): `ungated_until` on its
    registry row, never on a row that is not there. The answer names the row's own id, as `evals
    status` does. Raises Refused."""
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
    row = _registry_row(body["model_id"], model_id)
    if row is None or not dynamo.set_model_fields(row["model_id"], {"ungated_until": until}, expected={}):
        raise Refused(404, f"no model '{model_id}' in the registry (admin_cli models list)")
    return {"model_id": row["model_id"], "hours": hours, "ungated_until": until}


def _entry(entry) -> dict:
    """An `excluded_sources` entry as an object: a plain selector means `{source}`."""
    return {"source": entry} if isinstance(entry, str) else dict(entry)


def readd_days(since: str | None, now: datetime, zone) -> tuple[date, date] | None:
    """The days a revoke's events must be taken out of again, for the rollup's `from` and `to`:
    from the day `since` falls on in `zone` (or the oldest day a rollup may add up again,
    evals_rollup.OLDEST_DAY_BACK back, when that day is older or there is no `since`) to today.
    None when `since` is after today: no day holds those events yet."""
    today = evals_rollup.local_day(now, zone)
    first = today - timedelta(days=evals_rollup.OLDEST_DAY_BACK)
    if since is not None:
        moment = datetime.fromisoformat(since.replace("Z", "+00:00"))
        day = evals_rollup.local_day(moment if moment.tzinfo else moment.replace(tzinfo=UTC), zone)
        if day > today:
            return None
        first = max(first, day)
    return first, today


def _revoked(excluded: list, added: dict | None, days: tuple[date, date] | None, start_rollup) -> dict:
    """revoke's answer, once the entry is in `excluded_sources`: the days already final are added up
    again by the rollup `start_rollup(from, to)` starts (it returns None when it started one, else
    why not, in words), or the answer gives the command that does it."""
    answer = {"excluded_sources": excluded, "added": added}
    if days is None:
        applied = f"{REVOKED}; its 'since' is after today, so no day holds those events yet."
        return {**answer, "rollup": None, "applied": applied}
    first, last = (day.isoformat() for day in days)
    why_not = start_rollup(first, last) if start_rollup else "no rollup was started"
    answer["rollup"] = {"from": first, "to": last, "started": why_not is None}
    if why_not is None:
        applied = (
            f"{REVOKED}, and the days {first} to {last} are being added up again now (in eval-jobs), so "
            "the days already final leave them out too."
        )
    else:
        applied = (
            f"{REVOKED}. The days already final keep them until `evals rollup --from {first} --to {last}` "
            f"adds those days up again: {why_not}."
        )
    return {**answer, "applied": applied}


def revoke(body: dict, now: datetime | None = None, start_rollup=None) -> dict:
    """Append `{source, since}` to `excluded_sources` (an entry already there is not added twice),
    then have `start_rollup(from, to)` add up again the days already final that hold its events
    (readd_days; also when the entry was there, so sending it again retries a start that failed).
    The list in effect afterwards, the entry added (None when it was there) and the days asked
    for. Raises Refused, and then starts nothing."""
    now = now or datetime.now(UTC)
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
    effective = evals_settings.effective_settings(row)
    current = effective["excluded_sources"]
    days = readd_days(since, now, evals_settings.zone(effective))
    if any(_entry(existing) == entry for existing in current):
        return _revoked(current, None, days, start_rollup)
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
    return _revoked(updated, entry, days, start_rollup)
