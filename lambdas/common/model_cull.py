"""Culling in proposal mode, an operator's half: applying the weekly proposal and restoring a model
(docs/enhancements/model-ranch-prompt-assets-evals.md, sections 3, 4.5 and 4.6, PR16). The admin
API's POST /evals/cull/apply and POST /models/{model_id}/restore call these; `admin_cli evals cull
--apply` and `models restore` send them.

The weekly cycle proposes (common/model_fleet.cull_proposal: the state row's `cull_proposal`, which
expires at the next cycle) and never deletes. Both routes are refused while evals are off, as an
adjustment is (common/evals_adjust.py): a cull reads the scores the rollup keeps, and a restore
writes an Admin Forgive event.

**apply** archives then deletes up to `cull_max_per_run` of the proposal's models, worst total
first, and only while `cull_enabled` is true; it is refused when there is no proposal or it has
expired. Each model is checked again against the live rules first (model_fleet.cull_check, over
the registry, the topics, the default row, the pool and the evals settings as they are now), so a
model that has since been given a role, had its price locked or its gates held open, recovered
above the gate, or left the registry is kept, and the answer says why. The culled models go
through the prune path (model_fleet.retire): each archive entry keeps the model's row, and
discovery never adds one back (deviation 8). The proposal is left as it was, so another apply
takes the next ones, checked again. What was done is recorded on the state row as
`last_cull_apply` (when, the ids culled, those kept and why, those left for another apply), which
`evals status` shows.

**restore** puts a model the archive holds (culled, or pruned) back in the registry: the row the
archive kept for a culled model (a pruned one's price and names only, disabled, for `models add`
to enable), with `ungated_until` RESTORE_UNGATED (7 days) ahead, so no gate holds it back while its
scores settle. When its total is below zero it writes an Admin Forgive that brings it to zero
through common/evals_adjust.adjust (allow_cull when the forgive is over 100), which writes the
adjust# row and the event as `evals forgive` does: the models row's total moves at the next hourly
rollup. Then it deletes the archive entry. A model the registry already holds (put back by hand)
is not written again; it is forgiven and held open all the same. Steps are in that order so that a
restore that fails part-way can be sent again: the forgive is worked out from the total as it then
is, so a second one is never written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from common import dynamo, evals_adjust, evals_settings, evidence, model_fleet
from common.evals_adjust import Refused

# How long a restored model's gates are held open (section 4.6's `ungated_until`).
RESTORE_UNGATED = timedelta(days=7)
CULL_OFF = (
    "culling is off (cull_enabled is false): nothing was archived or deleted. `evals set --cull on` "
    "turns it on; the proposal in place stays until it expires"
)
NO_PROPOSAL = (
    "there is no cull proposal: the weekly cycle (Fridays 04:00 Sydney time) writes one while evals are on"
)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


def _proposal(state: dict, now: datetime) -> dict:
    """The state row's cull proposal while it holds models to look at and has not expired. Raises
    Refused (409) otherwise."""
    proposal = state.get(model_fleet.CULL_PROPOSAL_FIELD)
    if not isinstance(proposal, dict) or not isinstance(proposal.get("models"), list):
        raise Refused(409, NO_PROPOSAL)
    expires = evals_settings._parse(proposal.get("expires_at"))
    if expires is None or now >= expires:
        raise Refused(
            409,
            f"the cull proposal made {proposal.get('proposed_at')} expired at {proposal.get('expires_at')}: "
            "the next weekly cycle proposes again",
        )
    return proposal


def apply(now: datetime | None = None) -> dict:
    """Cull up to `cull_max_per_run` of the current proposal's models (see the module's docstring):
    the ids culled, those kept and why, those left for another apply. Raises Refused."""
    now = now or datetime.now(UTC)
    settings = evals_settings.get_settings()
    if not settings["cull_enabled"]:
        raise Refused(409, CULL_OFF)
    proposal = _proposal(dynamo.get_config_row(model_fleet.STATE_ID) or {}, now)
    models = dynamo.list_models()
    by_id = {model["model_id"]: model for model in models}
    roles = model_fleet.cull_roles(settings, models)
    cap = settings["cull_max_per_run"]
    chosen, kept, left = [], [], []
    for entry in proposal["models"]:
        model_id = entry.get("model_id") if isinstance(entry, dict) else None
        row = by_id.get(model_id) if isinstance(model_id, str) else None
        if row is None:
            kept.append({"model_id": model_id, "reason": "it is no longer in the registry"})
            continue
        why = model_fleet.cull_check(row, roles, settings, now)
        if why is not None:
            kept.append({"model_id": model_id, "reason": why})
        elif len(chosen) < cap:
            chosen.append(row)
        else:
            left.append(model_id)
    model_fleet.retire(chosen, now, culled=True)
    record = {
        "applied_at": _iso(now),
        "proposed_at": proposal.get("proposed_at"),
        "culled_ids": [row["model_id"] for row in chosen],
        "kept": kept,
        "left": left,
    }
    dynamo.update_config_row(model_fleet.STATE_ID, {model_fleet.LAST_CULL_FIELD: record})
    print(f"model_cull: culled {len(chosen)} of {len(proposal['models'])} proposed models (cap {cap})")
    evidence.emit(
        "model_cull",
        job="apply",
        status="applied",
        proposed=len(proposal["models"]),
        culled=len(chosen),
        kept=len(kept),
        left=len(left),
        cap=cap,
    )
    applied = "archived, then deleted; `models restore <id>` puts one back"
    if left:
        applied += f"; {len(left)} more passed the checks: `evals cull --apply` again takes them"
    return {**record, "cap": cap, "applied": applied}


def _archived(given: str, model_id: str) -> tuple[str, dict] | None:
    """(its key, its entry) for the archive's entry of the model `given` (as sent) or `model_id`
    (its canonical id) names, matched as the registry's rows are; None when there is none."""
    archive = dynamo.get_model_archive()
    for key in dict.fromkeys((given, model_id)):
        if key in archive:
            return key, archive[key]
    return next(
        ((key, entry) for key, entry in archive.items() if model_fleet._key(key) == model_id), None
    )


def _row(key: str, entry: dict) -> dict:
    """The registry row a restore writes: the row a culled model's entry kept, else (a pruned model)
    its price and names, disabled. Never the scores (the next rollup puts them back) or an override."""
    kept = entry.get("row")
    if isinstance(kept, dict):
        row = {name: value for name, value in kept.items() if name not in model_fleet.SCORE_FIELDS}
    else:
        row = {
            "display_name": entry.get("display_name") or key,
            "provider": entry.get("provider"),
            "input_price_usd_per_1k_tokens": entry.get("input_price_usd_per_1k_tokens"),
            "output_price_usd_per_1k_tokens": entry.get("output_price_usd_per_1k_tokens"),
            "enabled": False,
        }
    return {name: value for name, value in {**row, "model_id": key}.items() if value is not None}


def restore(given, now: datetime | None = None) -> dict:
    """Put an archived model back (see the module's docstring): its row, `ungated_until` 7 days
    ahead, an Admin Forgive to zero when its total is below it, and its archive entry deleted.
    Raises Refused."""
    now = now or datetime.now(UTC)
    model_id = evals_adjust._model_id(given)
    if model_id is None:
        raise Refused(400, "'model_id' must be a model id")
    found = _archived(given, model_id)
    if found is None:
        raise Refused(
            404,
            f"no model '{model_id}' in the archive: only a culled or pruned model can be restored, once "
            "(admin_cli models list shows the registry)",
        )
    key, entry = found
    until = (now + RESTORE_UNGATED).astimezone(UTC).isoformat(timespec="seconds")
    existing = evals_adjust._registry_row(given, model_id)
    if existing is None:
        added = dynamo.add_model_if_missing({**_row(key, entry), "ungated_until": until})
        registry_id = key
    else:
        added = False
        registry_id = existing["model_id"]
        dynamo.set_model_fields(registry_id, {"ungated_until": until}, expected={})

    settings = evals_settings.get_settings()
    total = evals_adjust.model_total(model_id, settings, now)
    forgive = None
    if total < 0:
        amount = min(-total, evals_adjust.MAX_AMOUNT)
        forgive = evals_adjust.adjust(
            {
                "target": "model",
                "model_id": registry_id,
                "direction": "forgive",
                "amount": amount,
                "allow_cull": amount > evals_adjust.CAP,
            },
            now,
        )
    dynamo.unarchive_model(key)
    print(f"model_cull: restored {registry_id} (forgiven {forgive['delta'] if forgive else 0})")
    evidence.emit(
        "model_cull",
        job="restore",
        status="restored" if added else "in_registry",
        model_id=model_id,
        forgiven=forgive["delta"] if forgive else 0,
    )
    return {
        "model_id": registry_id,
        "restored": added,
        "was_culled": entry.get("culled") is True,
        "enabled": (existing or _row(key, entry)).get("enabled") is not False,
        "ungated_until": until,
        "total_perf_before": total,
        "forgive": forgive,
        "applied": (
            "back in the registry"
            if added
            else "in the registry already (put back by hand), so its row was left as it is"
        )
        + f"; its gates are held open until {until}"
        + (
            "; an Admin Forgive brings its total to zero at the next hourly rollup"
            if forgive
            else "; its total is not below zero, so nothing was forgiven"
        ),
    }
