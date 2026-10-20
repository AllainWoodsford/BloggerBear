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

**An inactive model is routed around** (docs/enhancements/model-catalog-refresh.md, "Pruning and
cycling"). When the model a topic's rotation (or its own `model_id`) gives is one the Models
registry marks `enabled: false`, the call goes to the fallback if one is configured, else to the
default. If those are inactive too, it goes to a random active model from the registry, which
replaces the inactive one in the topic's rotation (or as its `model_id`), so the next run starts
there. A model with no registry row counts as active. Every lookup here fails open: a registry
that cannot be read routes exactly as before.
"""

from __future__ import annotations

import os
import random

from common import dynamo
from common.dynamo import get_model_config


def resolve_model(topic: dict | None = None) -> tuple[str, str | None]:
    """Return (model_id, fallback_model_id) for a call, honoring precedence.

    `topic` is an optional Topics item (as returned by common.dynamo.
    get_topic) -- pass it when the call is on behalf of a specific topic,
    so that topic's own model_id/fallback_model_id override (if set) wins.
    Pass None for topic-agnostic calls (e.g. trending_digest_handler.py's
    cross-topic synthesis).
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

    if from_topic and _inactive(model_id):
        model_id, fallback_model_id = _route_around(topic, model_id, fallback_model_id, from_topic, config)

    # Nobody configured a fallback: retry with the Terraform default rather than have no
    # fallback at all, so a model Bedrock retires (or never enabled for the account) that a
    # topic or ModelConfig still names costs that run its first model, not the day's article.
    # No fallback when the default *is* the model: retrying the same call buys nothing.
    if fallback_model_id is None:
        env_default = os.environ.get("BEDROCK_MODEL_ID") or None
        if env_default != model_id:
            fallback_model_id = env_default

    return model_id, fallback_model_id


def _inactive(model_id: str) -> bool:
    """True only for a model the registry has a row for that says `enabled: false`."""
    try:
        row = dynamo.get_model(model_id)
    except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
        print(f"model_routing: could not read model {model_id}: {exc!r}")
        return False
    return row is not None and row.get("enabled") is False


def _route_around(
    topic: dict, model_id: str, fallback_model_id: str | None, from_topic: str, config: dict | None
) -> tuple[str, str | None]:
    """The topic's model is inactive: the fallback, else the default, else a random active
    registry model, which then takes the inactive model's place on the topic."""
    if config is None:
        try:
            config = get_model_config() or {}
        except Exception as exc:  # noqa: BLE001 - routing never fails over a config read
            print(f"model_routing: could not read the model config: {exc!r}")
            config = {}
    default = config.get("model_id") or os.environ.get("BEDROCK_MODEL_ID") or None
    for choice in (fallback_model_id, default):
        if choice and choice != model_id and not _inactive(choice):
            print(f"model_routing: {model_id} is inactive; topic {topic.get('topic_id')} uses {choice}")
            return choice, (default if choice != default else None)
    try:
        active = [
            m["model_id"]
            for m in dynamo.list_models()
            if m.get("enabled") is not False
            and m.get("access") != "denied"
            and (m.get("pricing_unit") or "tokens") == "tokens"
            and m["model_id"] != model_id
        ]
    except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
        print(f"model_routing: could not list the models: {exc!r}")
        active = []
    if not active:
        return model_id, fallback_model_id  # nothing better to go to: as before
    choice = random.choice(active)
    topic_id = topic.get("topic_id")
    print(f"model_routing: {model_id} and its fallbacks are inactive; topic {topic_id} moves to {choice}")
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
