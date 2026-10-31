"""The Model Ranch, for the operator's assistant: how each model is scoring and whether the gates hold
it back, and how the versions of a prompt compare (docs/enhancements/model-ranch-prompt-assets-evals.md,
PR20). The owner's spec named the two tools `get_model_ranch_health` and `get_prompt_ab_metrics`.

**model_ranch** is one row per model that has scores: its windows (today, the last seven days, the
month so far, all time) with the events behind each and the mean per event, as the hourly rollup put
them on its models row (common/evals_rollup.py); its gate, decided as the rotation guard decides it
(common/model_ranch.py: gated, kept as the default, fallback or judge, or held open by an operator's
`ungated_until`); its roles and purpose; this week against last week, from its day rows by the
rollup's own rule (evals_rollup.gating); and its weakest pairings with a prompt version over the last
DAYS_DEFAULT days (its `cell#` rows), ranked by section 4.9's statistic. Beside them: the judge's
agreement with people (the index row the rollup writes, evals_status.judge_summary) and the weekly
cull proposal (model-catalog-state).

**prompt_metrics** is one row per prompt version with events in the last `days` days (DAYS_DEFAULT):
its `n` (events above or below zero, section 4.9's count) and good share with its Wilson interval
(evals_status.sum_days), its mean per event, and the same per model (the version's `cell#` rows);
"insufficient data" while `n` is under the settings' `min_events`. Each version's all-time total is
its own field (`all_time`: every one of its day rows and its `adjust#prompt#` rows, an operator's
forgive or punish, added up by the rule a model's total_perf is, evals_rollup.windows), never folded
into the window's n, mean or good share.

**Findings** (suggestions.py): a gated model (`model_gated`, about its model id), a judge that is on
but not calibrated (`judge_uncalibrated`), a cull proposal waiting (`cull_proposed`; while culling is
off its card names `evals set --cull on` first, suggestions.VARIANTS) and a prompt that fell back to
the copy in the code in the last FALLBACK_DAYS days (`prompt_fallback`, about its prompt id: the
`Prompt Fallback` events its day rows count).

**What is said.** Everything here is an id, a code or a number: model ids, prompt ids and versions as
the rollup keyed them (each matched to its shape again before it is used), purposes and roles as
codes, and counts. No model's name, no topic's text and no reader's comment is read, so a voice
caller is given the same result as the page, with nothing under `untrusted`. `spoken` stays under
SPOKEN_MAX_WORDS words, naming at most a few ids.

**Reads.** The models table (one Scan) and the evals table, which the assistant's role reads and never
writes (both roots' `tables` map). The evals table is read by its index's day rows, which list the
entities each day has, and then one Query per entity, at most READS_MAX in one call: what is left out
past that is counted (`complete` false), never guessed. Registered only where evals are on and the
function is told the table (available).
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime, timedelta

from boto3.dynamodb.conditions import Key

from common import dynamo, evals_rollup, evals_settings, evals_status, evidence, model_catalog, model_fleet
from common import model_ranch as ranch_rules
from common.model_pricing import canonical_model_id
from common.prompts_registry import all_specs
from ops_mcp.suggestions import CULL_OFF, MODEL_ID_PATTERN, PROMPT_ID_PATTERN, finding
from ops_mcp.tools import _clamp_days, _join, _now, _parse

EVALS_TABLE_ENV = "EVALS_TABLE"
DAYS_DEFAULT = 30
# The digest's limit too (evals_status.DIGEST_MAX_DAYS): the evals log keeps 120 days.
DAYS_MAX = evals_status.DIGEST_MAX_DAYS
WEEK_DAYS = 7
# Recent enough for a prompt's fallbacks to be a finding: today and yesterday.
FALLBACK_DAYS = 2
# One call answers inside the agent's turn, which API Gateway ends at 29 seconds: at most this many
# Queries of the evals table, each a few milliseconds.
READS_MAX = 150
WORST_CELLS = 2
SPOKEN_IDS = 2
SPOKEN_VERSIONS = 4
SPOKEN_MAX_WORDS = 120
# A prompt version as common/evals.py checks it: an asset's `v2`, or a code fallback's `vanilla:v1`.
VERSION_PATTERN = re.compile(r"^(?:vanilla:)?v[1-9][0-9]{0,5}$")
FALLBACK_REASON = "Prompt Fallback"
DAY = evals_rollup.DAY

# A model's gate, as `state`.
GATED, PROTECTED, HELD_OPEN, OPEN = "gated", "protected", "held_open", "open"
GATE_ORDER = (GATED, PROTECTED, HELD_OPEN, OPEN)
# What memory.py's follow_up and watch_list are told about a model or a prompt they hold.
CHECK_OPEN, CHECK_FIXED, CHECK_GONE = "open", "fixed", "gone"

EVALS_OFF = "Evals are off here, so there are no model or prompt scores to look at."
NOT_A_PROMPT = "That isn't a prompt id: a prompt id looks like daily_cycle/draft."
INSUFFICIENT = "insufficient data"


def available() -> bool:
    """Whether the tools are registered: evals are on (EVALS_ENABLED is the exact word "true") and
    the function is told the evals table, which the root's `tables` map gives it with its read
    grant."""
    return bool(os.environ.get(EVALS_TABLE_ENV)) and os.environ.get("EVALS_ENABLED") == "true"


# --- small helpers --------------------------------------------------------------------------------


class _Reads:
    """The Queries one call may make (READS_MAX); one past them is not made and is counted."""

    def __init__(self, table) -> None:
        self.table = table
        self.left = READS_MAX
        self.skipped = 0

    def query(self, condition) -> list[dict] | None:
        if self.left <= 0:
            self.skipped += 1
            return None
        self.left -= 1
        return evals_rollup.query_all(self.table, condition)


def _model_id(value) -> str | None:
    return value if isinstance(value, str) and MODEL_ID_PATTERN.fullmatch(value) else None


def _prompt(prompt_id, version) -> tuple[str, str] | None:
    if not isinstance(prompt_id, str) or not PROMPT_ID_PATTERN.fullmatch(prompt_id):
        return None
    if not isinstance(version, str) or not VERSION_PATTERN.fullmatch(version):
        return None
    return prompt_id, version


def _cell_key(entity: str) -> tuple[str, str, str] | None:
    """(model, prompt id, version) of a `cell#<model>#<prompt_id>#<version>` entity, each shaped as
    its kind is; None for anything else."""
    parts = entity[len("cell#") :].rsplit("#", 2) if entity.startswith("cell#") else []
    if len(parts) != 3 or _model_id(parts[0]) is None or _prompt(parts[1], parts[2]) is None:
        return None
    return parts[0], parts[1], parts[2]


def _prompt_key(entity: str) -> tuple[str, str] | None:
    if not entity.startswith("prompt#"):
        return None
    prompt_id, _, version = entity[len("prompt#") :].rpartition("#")
    return _prompt(prompt_id, version)


def _version_order(version: str) -> tuple[int, int]:
    """The code's copy before the assets, then by number: vanilla:v1, v1, v2, v10."""
    return (0 if version.startswith("vanilla:") else 1, int(version.rsplit("v", 1)[1]))


def _mean(perf: int, events: int) -> float | None:
    return round(perf / events, 2) if events else None


def _share(good: int, n: int) -> float | None:
    return round(good / n, 4) if n else None


def _pct(share: float | None) -> str:
    return "no" if share is None else f"{round(share * 100)}%"


def _interval(interval: list) -> str:
    low, high = interval
    return "" if low is None else f"{_pct(low)} to {_pct(high)}"


def _iso(value) -> str | None:
    """A stored time, if it is one; None for anything else."""
    return value if evidence.check("iso", value) else None


def _signed(number: int) -> str:
    return f"+{number}" if number > 0 else str(number)


def _entities(first: date, last: date, table) -> set[str]:
    """Every entity the index lists for a day from `first` to `last`."""
    return set().union(*evals_rollup.day_entities(first, last, table).values())


def _span(first: date, last: date):
    return Key("sk").between(f"{DAY}{first.isoformat()}", f"{DAY}{last.isoformat()}")


def _scored(totals: dict, min_events: int) -> dict:
    """One entity's totals over some days (evals_status.sum_days) as the tools show them."""
    return {
        "events": totals["events"],
        "perf": totals["perf"],
        "mean": _mean(totals["perf"], totals["events"]),
        "n": totals["n"],
        "good": totals["good"],
        "bad": totals["bad"],
        "good_share": _share(totals["good"], totals["n"]),
        "interval": totals["interval"],
        "insufficient_data": totals["n"] < min_events,
    }


def _clip(sentences: list[str]) -> str:
    """The sentences that fit in SPOKEN_MAX_WORDS words, in order; the first always."""
    kept, words = [], 0
    for sentence in sentences:
        count = len(sentence.split())
        if kept and words + count >= SPOKEN_MAX_WORDS:
            break
        kept.append(sentence)
        words += count
    return " ".join(kept)


def _ids(ids: list[str]) -> str:
    """A few ids for speech, and how many more."""
    shown = ids[:SPOKEN_IDS]
    rest = len(ids) - len(shown)
    return _join(shown + ([f"{rest} more"] if rest > 0 else []))


# --- the gate -------------------------------------------------------------------------------------


def _guard(settings: dict, now: datetime) -> ranch_rules.Guard:
    """The rotation guard as it stands, built as `evals status` builds it (evals_status._gated_models):
    from these settings and one read of the model-config default row."""
    config = dynamo.get_model_config() or {}
    return ranch_rules.Guard(
        settings=settings, protected=ranch_rules.never_gated(settings, config), config=config, now=now
    )


def _gate(row: dict, guard: ranch_rules.Guard) -> dict:
    windows = ranch_rules.gated_windows(row, guard.settings)
    role = guard.role(row["model_id"])
    if not windows:
        state = OPEN
    elif role is not None:
        state = PROTECTED
    elif guard.gated(row):
        state = GATED
    else:
        state = HELD_OPEN
    gate = {"state": state, "windows": windows}
    if role is not None:
        gate["role"] = role
    if state == HELD_OPEN:
        until = row.get("ungated_until")
        gate["ungated_until"] = until if evidence.check("iso", until) else None
    return gate


def _gated_finding(model_id: str, windows: list[str]) -> dict:
    which = _join(windows)
    return finding(
        "model_gated",
        f"Held back from every candidate list: its {which} score is below its gate",
        model_id,
        model=model_id,
        windows=", ".join(windows),
    )


def _windows(row: dict) -> dict:
    events = row.get("perf_events") if isinstance(row.get("perf_events"), dict) else {}
    found = {}
    for window in evals_rollup.WINDOWS:
        perf = evals_rollup.count_of(row.get(f"{window}_perf"))
        count = evals_rollup.count_of(events.get(window))
        found[window] = {"perf": perf, "n": count, "mean": _mean(perf, count)}
    return found


def _codes(value) -> list[str]:
    return [code for code in value if evidence.check("code", code)] if isinstance(value, list) else []


def _registry() -> dict[str, dict]:
    """{canonical id: models row} for every row whose id is shaped as a model's."""
    return {
        canonical_model_id(row["model_id"]): row
        for row in dynamo.list_models()
        if _model_id(row.get("model_id")) is not None
    }


# --- model_ranch ----------------------------------------------------------------------------------


def _week_over_week(rows: list[dict], today: date, calibrated: bool) -> dict:
    """This week's and last week's gated perf and events, from a model's day rows by the rollup's own
    rule (evals_rollup.gating), and the change."""
    weeks = {"this_week": [0, 0], "last_week": [0, 0]}
    for row in rows:
        try:
            day = date.fromisoformat(str(row.get("sk", ""))[len(DAY) :])
        except ValueError:
            continue
        age = (today - day).days
        if 0 <= age < WEEK_DAYS:
            name = "this_week"
        elif WEEK_DAYS <= age < 2 * WEEK_DAYS:
            name = "last_week"
        else:
            continue
        perf, events = evals_rollup.gating(row.get("by_reason"), calibrated)
        weeks[name][0] += perf
        weeks[name][1] += events
    found = {name: {"perf": perf, "n": events} for name, (perf, events) in weeks.items()}
    return {**found, "change": found["this_week"]["perf"] - found["last_week"]["perf"]}


def _worst(cells: list[dict]) -> list[dict]:
    """The weakest pairings with enough data: the lowest ceiling on their good share first (the
    Wilson interval's upper end, section 4.9's interval read from the other side), then the lowest
    share, then the lowest mean."""
    judged = [cell for cell in cells if not cell["insufficient_data"]]
    judged.sort(
        key=lambda c: (c["interval"][1], c["good_share"], c["mean"] or 0, c["prompt_id"], c["version"])
    )
    return judged[:WORST_CELLS]


def _has_scores(entry: dict) -> bool:
    """Whether a model has anything to show: events in a window, a total an adjustment moved, or a
    gate. The rollup writes zeros on every registry model's row, most of which never ran."""
    windows = entry["windows"]
    return bool(any(w["n"] for w in windows.values()) or windows["total"]["perf"] or entry["gate"]["windows"])


def _cull(settings: dict, now: datetime) -> dict | None:
    """The weekly cull proposal in place (model-catalog-state's `cull_proposal`), its ids and numbers
    only; None before the first."""
    proposal = model_catalog.get_state().get(model_fleet.CULL_PROPOSAL_FIELD)
    if not isinstance(proposal, dict):
        return None
    expires = _parse(proposal.get("expires_at"))
    models = [
        {
            "model_id": entry["model_id"],
            "total_perf": evals_rollup.count_of(entry.get("total_perf")),
            "events": evals_rollup.count_of(entry.get("events")),
        }
        for entry in proposal.get("models") or []
        if isinstance(entry, dict) and _model_id(entry.get("model_id"))
    ]
    return {
        "proposed_at": _iso(proposal.get("proposed_at")),
        "expires_at": expires.isoformat() if expires else None,
        "expired": expires is None or now >= expires,
        "models": models,
        "cull_enabled": settings["cull_enabled"],
    }


def _judge_words(judge: dict) -> str:
    if not judge["judge_enabled"]:
        return "The judge is off."
    agreement = judge["agreement"]
    agrees = "with no pairs yet"
    if agreement is not None:
        agrees = f"agreeing with people {_pct(agreement)} of the time"
    if judge["calibrated"]:
        return f"The judge is calibrated, {agrees} over {judge['pairs']} pairs."
    return (
        f"The judge isn't calibrated yet, {agrees} over {judge['pairs']} of the {judge['min_pairs']} pairs "
        "it needs, so its verdicts don't count towards the gates."
    )


def _ranch_spoken(models: list[dict], judge: dict, cull: dict | None, weakest) -> str:
    if not models:
        sentences = ["No model has scored events yet, so nothing is gated."]
    else:
        sentences = [f"{len(models)} model{'s have' if len(models) != 1 else ' has'} scores."]
        by_state = {
            state: [m["model_id"] for m in models if m["gate"]["state"] == state] for state in GATE_ORDER
        }
        gated = by_state[GATED]
        if gated:
            verb = "is" if len(gated) == 1 else "are"
            sentences.append(f"{len(gated)} {verb} gated: {_ids(gated)}.")
        else:
            sentences.append("None is gated.")
        if by_state[PROTECTED]:
            count = len(by_state[PROTECTED])
            sentences.append(
                f"{count} would be gated but {'is' if count == 1 else 'are'} kept as the default, the "
                "fallback or the judge."
            )
        if by_state[HELD_OPEN]:
            count = len(by_state[HELD_OPEN])
            sentences.append(f"{count} {'is' if count == 1 else 'are'} held open by an override.")
    sentences.append(_judge_words(judge))
    if cull and not cull["expired"] and cull["models"]:
        count = len(cull["models"])
        off = "; culling is off" if not cull["cull_enabled"] else ""
        sentences.append(f"A cull proposal of {count} model{'s' if count != 1 else ''} is waiting{off}.")
    if weakest is not None:
        model_id, cell = weakest
        sentences.append(
            f"The weakest pairing with enough data is {model_id} with {cell['prompt_id']} {cell['version']}, "
            f"good {_pct(cell['good_share'])} of {cell['n']}."
        )
    if models:
        sentences.append("The table is on screen.")
    return _clip(sentences)


def _ranch_table(models: list[dict]) -> dict:
    rows = []
    for entry in models:
        gate, windows, weekly = entry["gate"], entry["windows"], entry["week_over_week"]
        if gate["state"] == GATED:
            said = f"gated ({', '.join(gate['windows'])})"
        elif gate["state"] == PROTECTED:
            said = f"kept: {gate['role']}"
        elif gate["state"] == HELD_OPEN:
            said = "held open"
        else:
            said = "open"
        weakest = ""
        for cell in entry["worst_cells"][:1]:
            weakest = f"{cell['prompt_id']} {cell['version']}: {_pct(cell['good_share'])} good of {cell['n']}"
        rows.append(
            [
                entry["model_id"],
                said,
                windows["daily"]["perf"],
                windows["weekly"]["perf"],
                windows["monthly"]["perf"],
                windows["total"]["perf"],
                windows["weekly"]["n"],
                "" if windows["weekly"]["mean"] is None else windows["weekly"]["mean"],
                _signed(weekly["change"]) if weekly else "",
                entry["purpose"] or "",
                ", ".join(entry["roles"]),
                weakest,
            ]
        )
    return {
        "title": f"Model Ranch ({len(models)} model{'s' if len(models) != 1 else ''})",
        "columns": [
            "Model", "Gate", "Today", "Week", "Month", "Total", "Week events", "Week mean",
            "Week on week", "Purpose", "Roles", "Weakest pairing",
        ],
        "rows": rows,
    }  # fmt: skip


def model_ranch(*, now: datetime | None = None) -> dict:
    """Every model with scores: its windows, n and mean, its gate, roles and purpose, this week against
    last, and its weakest pairings with a prompt version; the judge and the cull proposal beside them."""
    now = _now(now)
    if not available():
        return {"spoken": EVALS_OFF, "findings": [], "models": []}
    settings = evals_settings.get_settings()
    zone = evals_settings.zone(settings)
    today = evals_rollup.local_day(now, zone)
    table = evals_rollup.evals_table()
    index = evals_rollup.get_index(table)
    calibrated = evals_rollup.index_calibrated(index, settings)
    guard = _guard(settings, now)
    reads = _Reads(table)

    week_from = today - timedelta(days=2 * WEEK_DAYS - 1)
    cells_from = today - timedelta(days=DAYS_DEFAULT - 1)
    by_day = evals_rollup.day_entities(min(week_from, cells_from), today, table)
    entities = set().union(*(found for day, found in by_day.items() if day >= cells_from))
    week_entities = set().union(*(found for day, found in by_day.items() if day >= week_from))

    entries, unscored = [], 0
    for key, row in sorted(_registry().items()):
        entry = {
            "model_id": row["model_id"],
            "purpose": row["model_purpose"] if row.get("model_purpose") in model_catalog.PURPOSES else None,
            "roles": _codes(row.get("model_roles")),
            "gate": _gate(row, guard),
            "windows": _windows(row),
            "week_over_week": None,
            "worst_cells": [],
            "perf_updated_at": _iso(row.get("perf_updated_at")),
        }
        if not _has_scores(entry):
            unscored += 1
            continue
        entry["_key"] = key
        entries.append(entry)
    entries.sort(
        key=lambda e: (GATE_ORDER.index(e["gate"]["state"]), e["windows"]["total"]["perf"], e["model_id"])
    )

    # This week against last, then the pairings, in the order the models are shown: a call that runs
    # out of reads leaves out the ones at the bottom.
    for entry in entries:
        if f"model#{entry['_key']}" in week_entities:
            rows = reads.query(Key("entity").eq(f"model#{entry['_key']}") & _span(week_from, today))
            if rows is not None:
                entry["week_over_week"] = _week_over_week(rows, today, calibrated)
    order = {entry["_key"]: n for n, entry in enumerate(entries)}
    cells: dict[str, list[dict]] = {}
    keyed = [(key, entity) for entity in entities if (key := _cell_key(entity)) and key[0] in order]
    for (model_key, prompt_id, version), entity in sorted(keyed, key=lambda k: (order[k[0][0]], k[1])):
        rows = reads.query(Key("entity").eq(entity) & _span(cells_from, today))
        if rows is None:
            continue
        cell = {"prompt_id": prompt_id, "version": version}
        cell.update(_scored(evals_status.sum_days(rows), settings["min_events"]))
        cells.setdefault(model_key, []).append(cell)
    weakest = None
    for entry in entries:
        entry["worst_cells"] = _worst(cells.get(entry.pop("_key"), []))
        for cell in entry["worst_cells"][:1]:
            if weakest is None or (cell["interval"][1], cell["good_share"]) < (
                weakest[1]["interval"][1],
                weakest[1]["good_share"],
            ):
                weakest = (entry["model_id"], cell)

    judge = evals_status.judge_summary(settings, index)
    cull = _cull(settings, now)
    findings = [
        _gated_finding(entry["model_id"], entry["gate"]["windows"])
        for entry in entries
        if entry["gate"]["state"] == GATED
    ]
    if judge["judge_enabled"] and not judge["calibrated"]:
        findings.append(
            finding(
                "judge_uncalibrated",
                "The judge's verdicts don't count towards the gates: it has too few pairs with people's "
                "verdicts, or agrees with them too rarely",
                pairs=judge["pairs"],
                needs=judge["min_pairs"],
            )
        )
    if cull and not cull["expired"] and cull["models"]:
        findings.append(
            finding(
                "cull_proposed",
                f"The weekly cull proposal names {len(cull['models'])} model(s) below the total gate",
                variant=None if cull["cull_enabled"] else CULL_OFF,
                models=len(cull["models"]),
                expires_at=cull["expires_at"],
            )
        )
    return {
        "spoken": _ranch_spoken(entries, judge, cull, weakest),
        "findings": findings,
        "models": entries,
        "unscored": unscored,
        "judge": judge,
        "cull_proposal": cull,
        "gates": {name: settings[name] for name in ("daily_gate", "weekly_gate", "total_gate", "min_events")},
        "pairings_days": DAYS_DEFAULT,
        "timezone": settings["timezone"],
        "last_rolled_day": index.get("last_rolled_day"),
        "complete": reads.skipped == 0,
        "table": _ranch_table(entries),
        "as_of": now.isoformat(),
    }


# --- prompt_metrics -------------------------------------------------------------------------------


def _fallbacks(rows: list[dict], since: date | None = None) -> int:
    count = 0
    for row in rows:
        if since is not None and str(row.get("sk", "")) < f"{DAY}{since.isoformat()}":
            continue
        counts = (row.get("by_reason") or {}).get(FALLBACK_REASON)
        count += evals_rollup.count_of(counts.get("events")) if isinstance(counts, dict) else 0
    return count


def _version(
    prompt_id: str, version: str, reads: _Reads, first: date, today: date, settings: dict, calibrated: bool
) -> dict | None:
    """One version's window and all-time totals, or None when the reads ran out before it."""
    days = reads.query(Key("entity").eq(f"prompt#{prompt_id}#{version}") & Key("sk").begins_with(DAY))
    if days is None:
        return None
    adjustments = reads.query(Key("entity").eq(f"adjust#prompt#{prompt_id}#{version}"))
    if adjustments is None:
        return None
    window = [row for row in days if f"{DAY}{first.isoformat()}" <= str(row.get("sk", "")) <= f"{DAY}{today}"]
    all_time = evals_rollup.windows(days, adjustments, today, calibrated)
    return {
        "version": version,
        **_scored(evals_status.sum_days(window), settings["min_events"]),
        "fallbacks": _fallbacks(window),
        "recent_fallbacks": _fallbacks(days, today - timedelta(days=FALLBACK_DAYS - 1)),
        # Every day row and every adjust#prompt# row, by the rule a model's total_perf is added up by:
        # apart from the window's numbers above, never part of them.
        "all_time": {
            "total_perf": all_time["total_perf"],
            "events": all_time["perf_events"]["total"],
            "adjustments": sum(evals_rollup.count_of(row.get("delta")) for row in adjustments),
        },
        # Filled from the version's cell rows below; false when the reads ran out before all of them.
        "per_model": [],
        "per_model_complete": True,
    }


def _version_words(entry: dict, min_events: int) -> str:
    if entry["insufficient_data"]:
        needed = f"{entry['n']} judged events of the {min_events} needed"
        return f"{entry['version']} has too little data, {needed}"
    low, high = entry["interval"]
    return (
        f"{entry['version']} is good {_pct(entry['good_share'])} of the time over {entry['n']} judged "
        f"events, between {_pct(low)} and {_pct(high)}, a mean of {entry['mean']} per event"
    )


def _comparison(versions: list[dict]) -> str | None:
    judged = [entry for entry in versions if not entry["insufficient_data"]]
    if len(judged) < 2:
        return None
    best = max(judged, key=lambda entry: (entry["interval"][0], entry["good_share"]))
    if all(best["interval"][0] > other["interval"][1] for other in judged if other is not best):
        return f"{best['version']} is clearly ahead: its interval is above the others'."
    return "Their intervals overlap, so none is clearly ahead yet."


def _metrics_spoken(prompts: list[dict], days: int, settings: dict, asked: str | None, complete: bool) -> str:
    min_events = settings["min_events"]
    if not prompts:
        what = asked or "No prompt version"
        return f"{what} has no scored events in the last {days} days."
    if asked is not None:
        versions = prompts[0]["versions"]
        lines = [_version_words(entry, min_events) for entry in versions[:SPOKEN_VERSIONS]]
        sentences = [f"{asked} over the last {days} days: {'; '.join(lines)}."]
        comparison = _comparison(versions)
        if comparison:
            sentences.append(comparison)
        sentences.append("The numbers per model are on screen.")
        return _clip(sentences)
    versions = [entry for prompt in prompts for entry in prompt["versions"]]
    judged = sum(1 for entry in versions if not entry["insufficient_data"])
    sentences = [
        f"{len(versions)} version{'s' if len(versions) != 1 else ''} of {len(prompts)} "
        f"prompt{'s have' if len(prompts) != 1 else ' has'} scored events in the last {days} days, and "
        f"{judged} {'has' if judged == 1 else 'have'} enough data to judge."
    ]
    split = [prompt["prompt_id"] for prompt in prompts if len(prompt["versions"]) > 1]
    if split:
        are = "prompts are" if len(split) != 1 else "prompt is"
        sentences.append(f"{len(split)} {are} split between versions: {_ids(split)}.")
    fell_back = [p["prompt_id"] for p in prompts if any(v["recent_fallbacks"] for v in p["versions"])]
    if fell_back:
        sentences.append(f"{_ids(fell_back)} fell back to the copy in the code in the last two days.")
    more = "" if complete else " Not every version was read: ask about one prompt for all of it."
    sentences.append(
        f"The table is on screen; ask about one prompt for each version against each model.{more}"
    )
    return _clip(sentences)


def _metrics_table(prompts: list[dict]) -> dict:
    rows = [
        [
            prompt["prompt_id"],
            entry["version"],
            entry["n"],
            INSUFFICIENT if entry["insufficient_data"] else _pct(entry["good_share"]),
            _interval(entry["interval"]),
            "" if entry["mean"] is None else entry["mean"],
            entry["events"],
            entry["fallbacks"],
            entry["all_time"]["total_perf"],
        ]
        for prompt in prompts
        for entry in prompt["versions"]
    ]
    return {
        "title": f"Prompt versions ({len(rows)})",
        "columns": [
            "Prompt", "Version", "n", "Good share", "Interval", "Mean", "Events", "Fallbacks",
            "All-time total",
        ],
        "rows": rows,
    }  # fmt: skip


def prompt_metrics(prompt_id: str | None = None, days=DAYS_DEFAULT, *, now: datetime | None = None) -> dict:
    """Each prompt version with events in the last `days` days (1 to DAYS_MAX), or `prompt_id`'s only:
    n, good share with its interval, mean, the same per model, "insufficient data" under min_events,
    and its all-time total apart."""
    now = _now(now)
    if not available():
        return {"spoken": EVALS_OFF, "findings": [], "prompts": []}
    asked_badly = not isinstance(prompt_id, str) or not PROMPT_ID_PATTERN.fullmatch(prompt_id)
    if prompt_id is not None and asked_badly:
        return {"spoken": NOT_A_PROMPT, "findings": [], "prompts": []}
    try:
        days = _clamp_days(days, DAYS_MAX)
    except (TypeError, ValueError):
        days = DAYS_DEFAULT
    settings = evals_settings.get_settings()
    zone = evals_settings.zone(settings)
    today = evals_rollup.local_day(now, zone)
    first = today - timedelta(days=days - 1)
    table = evals_rollup.evals_table()
    calibrated = evals_rollup.index_calibrated(evals_rollup.get_index(table), settings)
    reads = _Reads(table)
    entities = _entities(first, today, table)

    found: dict[str, set[str]] = {}
    for entity in entities:
        key = _prompt_key(entity)
        if key and (prompt_id is None or key[0] == prompt_id):
            found.setdefault(key[0], set()).add(key[1])
    # A prompt split between versions first: the comparison is the point.
    order = sorted(found, key=lambda pid: (len(found[pid]) < 2, pid))
    prompts = []
    for pid in order:
        versions = [
            entry
            for version in sorted(found[pid], key=_version_order)
            if (entry := _version(pid, version, reads, first, today, settings, calibrated)) is not None
        ]
        if versions:
            prompts.append({"prompt_id": pid, "versions": versions})

    cells = [(key, entity) for entity in entities if (key := _cell_key(entity)) and key[1] in found]
    by_version = {(p["prompt_id"], v["version"]): v for p in prompts for v in p["versions"]}
    rank = {pid: n for n, pid in enumerate(order)}
    for (model_key, pid, version), entity in sorted(cells, key=lambda c: (rank[c[0][1]], c[1])):
        entry = by_version.get((pid, version))
        if entry is None:
            continue
        rows = reads.query(Key("entity").eq(entity) & _span(first, today))
        if rows is None:
            entry["per_model_complete"] = False
            continue
        model = {"model_id": model_key, **_scored(evals_status.sum_days(rows), settings["min_events"])}
        entry["per_model"].append(model)

    findings = []
    for prompt in prompts:
        recent = sum(entry["recent_fallbacks"] for entry in prompt["versions"])
        if recent:
            fell = [entry["version"] for entry in prompt["versions"] if entry["recent_fallbacks"]]
            findings.append(
                finding(
                    "prompt_fallback",
                    f"Fell back to the copy in the code {recent} time(s) in the last two days",
                    prompt["prompt_id"],
                    prompt=prompt["prompt_id"],
                    versions=", ".join(fell),
                )
            )
    complete = reads.skipped == 0
    return {
        "spoken": _metrics_spoken(prompts, days, settings, prompt_id, complete),
        "findings": findings,
        "prompt_id": prompt_id,
        "days": days,
        "since": first.isoformat(),
        "until": today.isoformat(),
        "timezone": settings["timezone"],
        "min_events": settings["min_events"],
        "prompts": prompts,
        "complete": complete,
        "table": _metrics_table(prompts),
        "as_of": now.isoformat(),
    }


# --- for the memory (memory.py): a watched model or prompt, and the findings it follows up ------------


def _row_for(model_id: str) -> dict | None:
    """The registry row of the model `model_id` names: by its key, else the row whose canonical id is
    the same (an inference profile's ARN for it)."""
    row = dynamo.get_model(model_id)
    if row is not None:
        return row
    key = canonical_model_id(model_id)
    return next((r for r in dynamo.list_models() if canonical_model_id(r.get("model_id")) == key), None)


def known_prompt(prompt_id) -> bool:
    return isinstance(prompt_id, str) and any(spec.prompt_id == prompt_id for spec in all_specs())


def watchable(kind: str, target_id) -> str | None:
    """Why a model or a prompt cannot be watched, in words for speech, or None if it can."""
    if not available():
        return EVALS_OFF
    if kind == "model":
        if _model_id(target_id) is None or _row_for(target_id) is None:
            return "I don't know a model by that id in the registry."
        return None
    if not isinstance(target_id, str) or not PROMPT_ID_PATTERN.fullmatch(target_id):
        return NOT_A_PROMPT
    return None if known_prompt(target_id) else "I don't know a prompt by that id."


def watched(kind: str, target_id: str, now: datetime) -> tuple[dict, str]:
    """A watched model's or prompt's state now, and a few words for speech."""
    if not available():
        return {"state": "off"}, f"{target_id}, whose scores aren't kept while evals are off"
    if kind == "model":
        row = _row_for(target_id)
        if row is None:
            return {"state": "gone"}, "a model that is no longer in the registry"
        gate = _gate(row, _guard(evals_settings.get_settings(), now))
        weekly = _windows(row)["weekly"]
        state = {"state": "read", "gate": gate, "weekly": weekly}
        if gate["state"] == GATED:
            return state, f"{target_id} is gated on its {_join(gate['windows'])} score"
        score = f"its weekly score is {weekly['perf']} over {weekly['n']} events"
        return state, f"{target_id} isn't gated; {score}"
    result = prompt_metrics(target_id, WEEK_DAYS, now=now)
    versions = (result.get("prompts") or [{}])[0].get("versions") or []
    state = {
        "state": "read",
        "versions": [
            {key: entry[key] for key in ("version", "n", "good_share", "interval", "insufficient_data")}
            for entry in versions
        ],
    }
    if not versions:
        return state, f"{target_id} has no scored events in the last week"
    said = [
        f"{entry['version']} has too little data"
        if entry["insufficient_data"]
        else f"{entry['version']} is good {_pct(entry['good_share'])} of {entry['n']}"
        for entry in versions[:SPOKEN_VERSIONS]
    ]
    return state, f"over the last week, {target_id}'s {_join(said)}"


def gate_check(model_id: str, now: datetime) -> tuple[str, dict | None]:
    """For a `model_gated` finding followed up: still gated (with the finding as model_ranch would
    give it now), no longer, or gone (no such model, or evals are off)."""
    if not available():
        return CHECK_GONE, None
    row = _row_for(model_id)
    if row is None:
        return CHECK_GONE, None
    gate = _gate(row, _guard(evals_settings.get_settings(), now))
    if gate["state"] == GATED:
        return CHECK_OPEN, _gated_finding(model_id, gate["windows"])
    return CHECK_FIXED, None


def fallback_check(prompt_id: str, now: datetime) -> tuple[str, dict | None]:
    """For a `prompt_fallback` finding followed up: whether the prompt fell back in the last
    FALLBACK_DAYS days (with the finding as prompt_metrics would give it now), or gone (evals are off)."""
    if not available():
        return CHECK_GONE, None
    result = prompt_metrics(prompt_id, FALLBACK_DAYS, now=now)
    found = next((f for f in result.get("findings") or [] if f["kind"] == "prompt_fallback"), None)
    return (CHECK_OPEN, found) if found else (CHECK_FIXED, None)
