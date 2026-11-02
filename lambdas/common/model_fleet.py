"""The model fleet: keeps the Models registry inside its cap and the topics' model rotations
working and fresh (docs/enhancements/archive/model-catalog-refresh.md, "Pruning and cycling").

* **Pruning** (prune, run by discover once the registry holds `max_models`): removes models until
  the next discover has room again, at most `prune_max_per_run` a run. Inactive models go first,
  then unpriced ones (including the image, video and embedding models, which are never priced),
  then the ones `prune_mode` values least: `value_for_money` (the default) keeps the cheaper
  models and prunes the most expensive; `frontier` keeps the high-end models and prunes the
  cheapest. A pruned model's price is kept in the `model-archive` config row, so the Stats page
  still prices the articles that used it, and discovery never adds it back.
* **Protected** models are never pruned: the application's default and fallback (the
  ModelConfig `default` row and BEDROCK_MODEL_ID), every topic's model, fallback and rotation
  (`model_id_candidates`), the shared rotation pool (`model-rotation`), the evals judge (the
  `evals` row's `judge_model_id`, when that row exists), and any price_locked row.
* **Access**: a model an access probe found the account cannot call (`access: denied`,
  model_catalog.probe_access) is pruned before anything else, and never joins a rotation.
* **Suitability** (suitable): what a model is for is its `model_purpose` (model_catalog records
  it from Bedrock's output modalities: `text`, `image`, `embedding`, ...) and `model_inputs` (what
  it takes). A job names what it needs (NEEDS): the pipeline's text work needs a text model, the
  vision triage one that also takes an image. A row from before purposes were recorded is judged
  by the old rule, token-priced means text, until `models classify` fills it in. Nothing here
  picks, routes to or accepts a model that is not suitable for the need.
* **Cycling** (cycle, the weekly `cycle` job): each topic with a rotation loses the models that
  have gone inactive, denied or unsuitable and gains one new model while it has fewer than
  `rotation_max` (10): an active, callable, priced text model that is nobody's fallback and not
  already in it, the cheapest in `value_for_money` mode, the most expensive in `frontier`.
* **Deactivation** (deactivated, when the admin API disables a model): the model leaves every
  topic's rotation (replaced by the cycler's pick when there is one), model and fallback, and the
  shared rotation pool.

Settings are the model catalog's (common/model_catalog.SETTINGS, the `model-catalog` row).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from common import dynamo, evidence
from common.model_pricing import canonical_model_id

PRUNE_MODES = ("value_for_money", "frontier")
# How many model ids a result lists (its counts are always whole).
RESULT_IDS_MAX = 20

# What a job needs of a model: a model whose purpose is text, or one that also takes an image as
# input (the vision triage shows the model the figure).
TEXT = "TEXT"
TEXT_WITH_IMAGE_INPUT = "TEXT_WITH_IMAGE_INPUT"
NEEDS = (TEXT, TEXT_WITH_IMAGE_INPUT)
# The purposes a row may record (model_catalog.model_purpose), and the one a row from before
# purposes were recorded is taken to have while its pricing unit says it is billed by tokens.
TEXT_PURPOSE = "text"
# The `evals` settings row (PR13 adds the module that owns it; until then this is a plain read of
# one attribute) and the role its judge model plays in protection.
EVALS_CONFIG_ID = "evals"
JUDGE_FIELD = "judge_model_id"


def _key(model_id) -> str | None:
    """The id models are compared by: the canonical one (an ARN reduces to the profile it names)."""
    return canonical_model_id(model_id) if isinstance(model_id, str) and model_id else None


def is_active(model: dict | None) -> bool:
    """A registry row is active unless it says `enabled: false` (rows from before the flag are)."""
    return model is not None and model.get("enabled") is not False


def is_usable(model: dict | None) -> bool:
    """Active, and not found uncallable by an access probe (model_catalog.probe_access): a model
    whose access is unknown or never checked counts as usable."""
    return is_active(model) and model.get("access") != "denied"


def blended_price(model: dict) -> float | None:
    """Input plus output USD per 1K tokens, or None for a model with no price."""
    if model.get("input_price_usd_per_1k_tokens") is None:
        return None
    output = model.get("output_price_usd_per_1k_tokens") or 0
    return float(model["input_price_usd_per_1k_tokens"]) + float(output)


def purpose(model: dict) -> str | None:
    """What a registry row says its model is for: its `model_purpose`, or for a row from before
    purposes were recorded, `text` when it is billed by tokens (the only kind the pipeline ever
    called) and `other` when its pricing unit says it is not; None only for an empty row."""
    if not model:
        return None
    recorded = model.get("model_purpose")
    if recorded:
        return recorded
    return TEXT_PURPOSE if (model.get("pricing_unit") or "tokens") == "tokens" else "other"


def suitable(model: dict | None, need: str = TEXT) -> bool:
    """Whether a registry row's model can do the work `need` names (NEEDS): TEXT, a model whose
    purpose is text; TEXT_WITH_IMAGE_INPUT, a text model whose recorded inputs include IMAGE. A
    row from before purposes were recorded (no `model_purpose`) is judged by the old rule: a
    token-priced model is a text model, and, its inputs unrecorded, is not held to the image rule,
    so nothing the pipeline routes to today stops until `models classify` has said otherwise."""
    if not model or purpose(model) != TEXT_PURPOSE:
        return False
    if need == TEXT_WITH_IMAGE_INPUT:
        inputs = model.get("model_inputs")
        return inputs is None or "IMAGE" in {str(m).upper() for m in inputs}
    return need == TEXT


def judge_model(evals: dict | None = None) -> str | None:
    """The evals judge's model id, from the `evals` settings row's `judge_model_id` (read here
    when not given); None while there is no row, or it names none."""
    if evals is None:
        evals = dynamo.get_config_row(EVALS_CONFIG_ID)
    model_id = (evals or {}).get(JUDGE_FIELD)
    return model_id if isinstance(model_id, str) and model_id else None


def application_roles(model_id: str, config: dict | None = None) -> list[str]:
    """What the application uses `model_id` for: "default" (the ModelConfig default row's model,
    or BEDROCK_MODEL_ID) and/or "fallback" (the default row's fallback). [] for neither."""
    config = dynamo.get_model_config() if config is None else config
    config = config or {}
    key = _key(model_id)
    roles = []
    if key in {_key(config.get("model_id")), _key(os.environ.get("BEDROCK_MODEL_ID"))}:
        roles.append("default")
    if key == _key(config.get("fallback_model_id")):
        roles.append("fallback")
    return roles


def protected_models(
    topics: list[dict], config: dict | None, pool: list[str], evals: dict | None = None
) -> dict[str, str]:
    """{canonical id: role} for every model pruning must never remove; the role is the first that
    applies, as a code: `default` (the ModelConfig default row's model, or BEDROCK_MODEL_ID),
    `fallback`, `topic_model`, `topic_fallback`, `rotation` (in a topic's `model_id_candidates`),
    `pool` (the shared rotation pool) or `judge` (the evals settings row's judge model, `evals`;
    read here when not given, and nothing while that row does not exist)."""
    config = config or {}
    protected: dict[str, str] = {}

    def protect(model_id, role):
        key = _key(model_id)
        if key and key not in protected:
            protected[key] = role

    protect(config.get("model_id"), "default")
    protect(os.environ.get("BEDROCK_MODEL_ID"), "default")
    protect(config.get("fallback_model_id"), "fallback")
    for topic in topics:
        protect(topic.get("model_id"), "topic_model")
        protect(topic.get("fallback_model_id"), "topic_fallback")
        for candidate in topic.get("model_id_candidates") or []:
            protect(candidate, "rotation")
    for candidate in pool:
        protect(candidate, "pool")
    protect(judge_model(evals), "judge")
    return protected


def prune_order(models: list[dict], mode: str) -> list[dict]:
    """`models` in the order pruning takes them: ones the account cannot call first, then inactive,
    then unpriced, then the least valued by `mode` (value_for_money: the most expensive; frontier:
    the cheapest)."""

    def key(model):
        price = blended_price(model) if suitable(model, TEXT) else None
        rank = 0.0 if price is None else (-price if mode == "value_for_money" else price)
        return (model.get("access") != "denied", is_active(model), price is not None, rank, model["model_id"])

    return sorted(models, key=key)


def prune(settings: dict, now: datetime) -> dict:
    """Prune the registry down to `max_models - discover_max_added` (room for the next discover),
    at most `prune_max_per_run` models, never a protected one. Archives each pruned model's price
    first."""
    models = dynamo.list_models()
    cap = settings["max_models"]
    if len(models) < cap:
        result = {"status": "under_cap", "models": len(models), "max_models": cap}
        evidence.emit("model_catalog_job", job="prune", **result)
        return result
    target = max(0, cap - settings["discover_max_added"])
    wanted = min(len(models) - target, settings["prune_max_per_run"])
    protected = protected_models(
        dynamo.list_topics(), dynamo.get_model_config(), dynamo.get_model_rotation_candidates()
    )
    candidates = [
        model
        for model in prune_order(models, settings["prune_mode"])
        if _key(model["model_id"]) not in protected and not model.get("price_locked")
    ]
    chosen = candidates[:wanted]
    pruned_at = now.astimezone(UTC).isoformat()
    dynamo.archive_models(
        {
            model["model_id"]: {
                "display_name": model.get("display_name") or model["model_id"],
                "provider": model.get("provider"),
                "input_price_usd_per_1k_tokens": model.get("input_price_usd_per_1k_tokens"),
                "output_price_usd_per_1k_tokens": model.get("output_price_usd_per_1k_tokens"),
                "pruned_at": pruned_at,
            }
            for model in chosen
        }
    )
    for model in chosen:
        dynamo.delete_model(model["model_id"])
    print(f"model_fleet: pruned {len(chosen)} of {len(models)} models ({settings['prune_mode']})")
    result = {
        "status": "pruned",
        "mode": settings["prune_mode"],
        "models": len(models),
        "max_models": cap,
        "pruned": len(chosen),
        "pruned_ids": [model["model_id"] for model in chosen][:RESULT_IDS_MAX],
        "protected": len(protected),
        "short_by": wanted - len(chosen),
    }
    evidence.emit("model_catalog_job", job="prune", **{k: v for k, v in result.items() if k != "pruned_ids"})
    return result


def rotation_picks(models: list[dict], config: dict | None, topics: list[dict], mode: str) -> list[dict]:
    """The models a rotation may gain, best first: usable (active, not denied), priced models
    suitable for text that are nobody's fallback, the cheapest first in value_for_money mode, the
    most expensive in frontier."""
    config = config or {}
    fallbacks = {_key(config.get("fallback_model_id"))} | {_key(t.get("fallback_model_id")) for t in topics}
    eligible = [
        model
        for model in models
        if is_usable(model)
        and suitable(model, TEXT)
        and blended_price(model) is not None
        and _key(model["model_id"]) not in fallbacks
    ]
    return sorted(
        eligible,
        key=lambda m: (blended_price(m) if mode == "value_for_money" else -blended_price(m), m["model_id"]),
    )


def _pick_for(rotation: list[str], picks: list[dict]) -> str | None:
    held = {_key(candidate) for candidate in rotation}
    return next((m["model_id"] for m in picks if _key(m["model_id"]) not in held), None)


def rotation_pool(candidates: list[str], models: list[dict] | None = None) -> list[str]:
    """The shared pool's candidates a topic may be rolled to (daily_cycle_handler's
    _assign_rotated_model): each one whose registry row is usable and suitable for text. One with
    no row is kept, and so is every one when the registry cannot be read: rolling a model forward
    never fails over a registry read, as routing has it."""
    try:
        rows = dynamo.list_models() if models is None else models
    except Exception as exc:  # noqa: BLE001 - the pool stands as configured
        print(f"model_fleet: could not read the models to pick from the pool: {exc!r}")
        return list(candidates)
    by_key = {_key(m["model_id"]): m for m in rows}
    return [
        c
        for c in candidates
        if _key(c) not in by_key or (is_usable(by_key[_key(c)]) and suitable(by_key[_key(c)], TEXT))
    ]


def cycle(settings: dict, now: datetime) -> dict:
    """The weekly cycle: every topic with a rotation loses its inactive, denied and unsuitable
    models and, below `rotation_max`, gains the best pick it does not have."""
    models = dynamo.list_models()
    by_key = {_key(m["model_id"]): m for m in models}
    topics = dynamo.list_topics()
    picks = rotation_picks(models, dynamo.get_model_config(), topics, settings["prune_mode"])
    changed, added, dropped = [], 0, 0
    for topic in topics:
        rotation = [c for c in topic.get("model_id_candidates") or [] if c]
        if not rotation:
            continue
        # A model with no registry row is kept: only one known to be inactive, denied or not a
        # text model goes.
        kept = [
            c
            for c in rotation
            if _key(c) not in by_key or (is_usable(by_key[_key(c)]) and suitable(by_key[_key(c)], TEXT))
        ]
        new = _pick_for(kept, picks) if len(kept) < settings["rotation_max"] else None
        updated = kept + ([new] if new else [])
        if updated != rotation:
            dynamo.set_topic_model_fields(topic["topic_id"], {"model_id_candidates": updated})
            changed.append(topic["topic_id"])
            added += bool(new)
            dropped += len(rotation) - len(kept)
    evidence.emit(
        "model_catalog_job",
        job="cycle",
        status="done",
        mode=settings["prune_mode"],
        topics=len(changed),
        added=added,
        dropped_inactive=dropped,
    )
    return {
        "status": "done",
        "mode": settings["prune_mode"],
        "topics_changed": changed[:RESULT_IDS_MAX],
        "added": added,
        "dropped_inactive": dropped,
    }


def deactivated(model_id: str, settings: dict) -> dict:
    """`model_id` was just disabled: take it out of every topic's rotation (putting the cycler's
    pick in its place when there is one), model and fallback, and out of the shared rotation pool.
    A topic left with no model falls through to the application's default (resolve_model)."""
    key = _key(model_id)
    models = dynamo.list_models()
    topics = dynamo.list_topics()
    picks = rotation_picks(models, dynamo.get_model_config(), topics, settings["prune_mode"])
    picks = [m for m in picks if _key(m["model_id"]) != key]
    updated_topics = []
    for topic in topics:
        updates: dict = {}
        rotation = [c for c in topic.get("model_id_candidates") or [] if c]
        if any(_key(c) == key for c in rotation):
            kept = [c for c in rotation if _key(c) != key]
            replacement = _pick_for(kept, picks)
            updates["model_id_candidates"] = kept + ([replacement] if replacement else [])
        if _key(topic.get("model_id")) == key:
            updates["model_id"] = None
        if _key(topic.get("fallback_model_id")) == key:
            updates["fallback_model_id"] = None
        if updates:
            dynamo.set_topic_model_fields(topic["topic_id"], updates)
            updated_topics.append(topic["topic_id"])
    pool = dynamo.get_model_rotation_candidates()
    pool_updated = any(_key(c) == key for c in pool)
    if pool_updated:
        dynamo.put_model_rotation_candidates([c for c in pool if _key(c) != key])
    return {"topics_updated": updated_topics, "rotation_pool_updated": pool_updated}
