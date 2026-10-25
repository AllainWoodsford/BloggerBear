"""Model resolution for the AI lineage/cost-tracking enhancement (PR 1 of 5,
docs/project-plan.md §11).

Resolves which Bedrock model (and optional fallback) a given call should
use, without ever requiring a Terraform apply to change it:

    topic rotation candidates  ->  topic-level override
        ->  ModelConfig "default" row  ->  env var

A topic's `model_id_candidates` list (PR 4 of 5) enables rotation: one
candidate is picked at random per resolve_model call. Callers resolve
once per run, so every Bedrock call that goes into one article uses the
same picked model -- an article's lineage stays coherent, and the
variation happens *between* runs, which is the point (e.g. try a
different model on a topic for a while and compare the results).

Each step is optional -- a topic with no override, or a ModelConfig table
with no "default" row yet (fresh deploy, or nobody's configured it),
falls through to the next step rather than failing. `os.environ[
"BEDROCK_MODEL_ID"]` (Terraform-set, today's only source of truth) is the
final safety net and always works. It is also the fallback whenever none is
configured and the picked model is a different one.

**An inactive or unsuitable model is routed around** (docs/enhancements/archive/model-catalog-refresh.md,
"Pruning and cycling"). When the model a topic's rotation (or its own `model_id`) gives is one the
Models registry marks `enabled: false`, or one not suitable for what the call needs
(common/model_fleet.suitable: a text model, or for the vision triage one that also takes an image),
the call goes to the fallback if one is configured and fit, else to the default. If those are unfit
too, it goes to a random usable, suitable model from the registry. A model that is inactive or no
text model at all is replaced by that pick in the topic's rotation (or as its `model_id`), so the
next run starts there; one that only lacks what a stricter need asks for (an image input) stays on
the topic, and only this call goes elsewhere. A model with no registry row counts as fit. Every
lookup here fails open: a registry that cannot be read routes exactly as before.
"""

from __future__ import annotations

import os
import random

from common import dynamo, model_fleet
from common.dynamo import get_model_config


def resolve_model(topic: dict | None = None, need: str = model_fleet.TEXT) -> tuple[str, str | None]:
    """Return (model_id, fallback_model_id) for a call, honoring precedence.

    `topic` is an optional Topics item (as returned by common.dynamo.
    get_topic) -- pass it when the call is on behalf of a specific topic,
    so that topic's own model_id/fallback_model_id override (if set) wins.
    Pass None for topic-agnostic calls (e.g. the model catalog's reading of
    a web search result).

    `need` (model_fleet.NEEDS) is what the call needs of the model: TEXT, or
    TEXT_WITH_IMAGE_INPUT for the vision triage. A topic's model that cannot
    meet it is routed around (see the module docstring).
    """
    model_id = None
    fallback_model_id = None
    from_topic = None  # "rotation" or "model": where a topic's own model came from
    config = None

    if topic:
        candidates = [c for c in (topic.get("model_id_candidates") or []) if c]
        if candidates:
            model_id = random.choice(candidates)
            from_topic = "rotation"
        else:
            model_id = topic.get("model_id") or None
            from_topic = "model" if model_id else None
        fallback_model_id = topic.get("fallback_model_id") or None

    if model_id is None or fallback_model_id is None:
        config = get_model_config() or {}
        if model_id is None:
            model_id = config.get("model_id") or None
        if fallback_model_id is None:
            fallback_model_id = config.get("fallback_model_id") or None

    if model_id is None:
        model_id = os.environ["BEDROCK_MODEL_ID"]

    if from_topic:
        row = _registry_row(model_id)
        if _unfit(row, need):
            model_id, fallback_model_id = _route_around(
                topic,
                model_id,
                fallback_model_id,
                from_topic,
                config,
                need,
                replace=_unfit(row, model_fleet.TEXT),
            )

    # Nobody configured a fallback: retry with the Terraform default rather than have no
    # fallback at all, so a model Bedrock retires (or never enabled for the account) that a
    # topic or ModelConfig still names costs that run its first model, not the day's article.
    # No fallback when the default *is* the model: retrying the same call buys nothing.
    if fallback_model_id is None:
        env_default = os.environ.get("BEDROCK_MODEL_ID") or None
        if env_default != model_id:
            fallback_model_id = env_default

    return model_id, fallback_model_id


def _registry_row(model_id: str) -> dict | None:
    """The model's registry row, or None when it has none or the registry cannot be read."""
    try:
        return dynamo.get_model(model_id)
    except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
        print(f"model_routing: could not read model {model_id}: {exc!r}")
        return None


def _unfit(row: dict | None, need: str) -> bool:
    """True only for a model the registry has a row for that says `enabled: false`, or that is not
    suitable for `need` (model_fleet.suitable); a model with no row is fit."""
    return row is not None and (row.get("enabled") is False or not model_fleet.suitable(row, need))


def _route_around(
    topic: dict,
    model_id: str,
    fallback_model_id: str | None,
    from_topic: str,
    config: dict | None,
    need: str,
    *,
    replace: bool,
) -> tuple[str, str | None]:
    """The topic's model is inactive or unsuitable: the fallback, else the default, else a random
    usable registry model suitable for `need`, which then takes the model's place on the topic
    when `replace` (the model is inactive or no text model: nothing on the topic should use it)."""
    if config is None:
        try:
            config = get_model_config() or {}
        except Exception as exc:  # noqa: BLE001 - routing never fails over a config read
            print(f"model_routing: could not read the model config: {exc!r}")
            config = {}
    default = config.get("model_id") or os.environ.get("BEDROCK_MODEL_ID") or None
    for choice in (fallback_model_id, default):
        if choice and choice != model_id and not _unfit(_registry_row(choice), need):
            topic_id = topic.get("topic_id")
            print(f"model_routing: {model_id} is unfit for {need}; topic {topic_id} uses {choice}")
            return choice, (default if choice != default else None)
    try:
        active = [
            m["model_id"]
            for m in dynamo.list_models()
            if model_fleet.is_usable(m) and model_fleet.suitable(m, need) and m["model_id"] != model_id
        ]
    except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
        print(f"model_routing: could not list the models: {exc!r}")
        active = []
    if not active:
        return model_id, fallback_model_id  # nothing better to go to: as before
    choice = random.choice(active)
    topic_id = topic.get("topic_id")
    print(f"model_routing: {model_id} and its fallbacks are unfit for {need}; "
          f"topic {topic_id} moves to {choice}")
    if not replace:
        return choice, fallback_model_id if fallback_model_id != choice else None
    try:
        if from_topic == "rotation":
            rotation = [choice if c == model_id else c for c in topic.get("model_id_candidates") or []]
            rotation = list(dict.fromkeys(rotation))
            dynamo.set_topic_model_fields(topic["topic_id"], {"model_id_candidates": rotation})
        else:
            dynamo.set_topic_model_fields(topic["topic_id"], {"model_id": choice})
    except Exception as exc:  # noqa: BLE001 - the run still goes ahead on the new model
        print(f"model_routing: could not update topic {topic.get('topic_id')}: {exc!r}")
    return choice, fallback_model_id if fallback_model_id != choice else None
