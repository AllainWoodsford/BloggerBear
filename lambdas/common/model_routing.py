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
the topic, and only this call goes elsewhere. A stricter need holds the default to it as well: when
the topic names no model and the default (ModelConfig's, or BEDROCK_MODEL_ID) takes no image, the
triage's call goes elsewhere the same way, and nothing is written. For the text work the defaults
are taken as they are. The fallback handed back is held to the need too: one that is not suitable
(an embeddings model, one Bedrock no longer lists) gives way to BEDROCK_MODEL_ID, as when none is
configured. A model with no registry row counts as fit. Every lookup here fails open: a registry
that cannot be read routes exactly as before.

**A gated model is no candidate** (common/model_ranch.py, the rotation guard; section 4.5 of
docs/enhancements/model-ranch-prompt-assets-evals.md). While evals are on, the rotation draw is over
the topic's candidates the Model Ranch has not gated, and the registry pick an unfit model is routed
to is never a gated one, so no gated model is ever written to a topic either. When every candidate
is gated, the topic's own `model_id` is used, else the default: never the whole list again. The
default, the fallback and the judge are never gated, nor is a topic's own `model_id`: they are
served whatever their scores, and a `model_gated_protected` evidence line says so when the gates
would have held one back. For that, a call lists the registry once and reads the evals row once,
and only while evals are on; a listing that fails gates nothing.
"""

from __future__ import annotations

import os
import random

from common import dynamo, model_fleet, model_ranch
from common.dynamo import get_model_config
from common.model_pricing import canonical_model_id


class _Ranch:
    """The rotation guard for one resolve_model (common/model_ranch.py): its Guard and the
    registry's rows, each read once, and only while evals are on. Off (nothing gated, nothing read)
    while evals are off, and when the registry cannot be listed."""

    def __init__(self, guard: model_ranch.Guard, models: list[dict] | None):
        self.guard = guard
        self.models = models
        self._by_key = {_key(model.get("model_id")): model for model in models or ()}

    @classmethod
    def load(cls) -> _Ranch:
        guard = model_ranch.load_guard()
        if not guard.on:
            return cls(guard, None)
        try:
            return cls(guard, dynamo.list_models())
        except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
            print(f"model_routing: could not list the models for the rotation guard: {exc!r}")
            return cls(guard, None)

    @property
    def on(self) -> bool:
        return self.guard.on and self.models is not None

    def row(self, model_id) -> dict | None:
        return self._by_key.get(_key(model_id))

    def gated(self, model_id) -> bool:
        return self.on and self.guard.gated(self.row(model_id))


def _key(model_id) -> str | None:
    return canonical_model_id(model_id) if isinstance(model_id, str) and model_id else None


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

    While evals are on, a model the Model Ranch has gated is never drawn from
    the rotation nor picked to route around an unfit one (model_ranch).
    """
    model_id = None
    fallback_model_id = None
    from_topic = None  # "rotation" or "model": where a topic's own model came from
    ranch = _Ranch.load()
    # While evals are on the guard has read the default row (it protects its model and fallback).
    config = ranch.guard.config

    if topic:
        candidates = [c for c in (topic.get("model_id_candidates") or []) if c]
        if candidates and ranch.on:
            healthy = [c for c in candidates if not ranch.gated(c)]
            if not healthy:
                print(
                    f"model_routing: every model in topic {topic.get('topic_id')}'s rotation is gated; "
                    "it uses its own model, else the default"
                )
            candidates = healthy
        if candidates:
            model_id = random.choice(candidates)
            from_topic = "rotation"
        else:
            model_id = topic.get("model_id") or None
            from_topic = "model" if model_id else None
        fallback_model_id = topic.get("fallback_model_id") or None

    if model_id is None or fallback_model_id is None:
        if config is None:
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
                ranch=ranch,
            )
    elif need != model_fleet.TEXT and _unsuitable(_registry_row(model_id), need):
        # The default (ModelConfig's, or BEDROCK_MODEL_ID) cannot do what a stricter need asks (the
        # triage's image): this call goes elsewhere, and nothing is written, the default being no
        # topic's to change. For the text work the defaults are taken as they are.
        model_id, fallback_model_id = _route_around(
            topic, model_id, fallback_model_id, None, config, need, replace=False, ranch=ranch
        )

    # A fallback that cannot do the call's work (an embeddings model, one Bedrock no longer lists,
    # set before writes were checked) is no retry at all: the Terraform default takes its place.
    if fallback_model_id is not None and _unsuitable(_registry_row(fallback_model_id), need):
        print(f"model_routing: fallback {fallback_model_id} is not suitable for {need}; using the default")
        fallback_model_id = None

    # Nobody configured a fallback: retry with the Terraform default rather than have no
    # fallback at all, so a model Bedrock retires (or never enabled for the account) that a
    # topic or ModelConfig still names costs that run its first model, not the day's article.
    # No fallback when the default *is* the model: retrying the same call buys nothing. For the
    # text work it is taken as it is (the final safety net); a stricter need still holds it to that.
    if fallback_model_id is None:
        env_default = os.environ.get("BEDROCK_MODEL_ID") or None
        if env_default and env_default != model_id:
            if need == model_fleet.TEXT or not _unsuitable(_registry_row(env_default), need):
                fallback_model_id = env_default

    if ranch.on:
        # Served whatever their scores (the default, the fallback, the judge, a topic's own model):
        # a line says so when the gates would have held one back.
        own = {}
        if topic:
            own[_key(topic.get("fallback_model_id"))] = "topic_fallback"
            own[_key(topic.get("model_id"))] = "topic_model"
        topic_id = (topic or {}).get("topic_id")
        for slot, served in (("model", model_id), ("fallback", fallback_model_id)):
            if served:
                ranch.guard.served(
                    served, ranch.row(served), caller="routing", slot=slot, role=own.get(_key(served)),
                    topic_id=topic_id,
                )  # fmt: skip
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


def _unsuitable(row: dict | None, need: str) -> bool:
    """True only for a model the registry has a row for that is not suitable for `need`
    (model_fleet.suitable), whether or not it is enabled; a model with no row is suitable."""
    return row is not None and not model_fleet.suitable(row, need)


def _route_around(
    topic: dict | None,
    model_id: str,
    fallback_model_id: str | None,
    from_topic: str | None,
    config: dict | None,
    need: str,
    *,
    replace: bool,
    ranch: _Ranch | None = None,
) -> tuple[str, str | None]:
    """The model is inactive or unsuitable: the fallback, else the default, else a random usable
    registry model suitable for `need` that the rotation guard (`ranch`) has not gated. A topic's
    own model (`from_topic`: "rotation" or "model") is then replaced on the topic by that pick when
    `replace` (the model is inactive or no text model: nothing on the topic should use it). The
    default (`from_topic` None, `replace` False) never is: only this call goes elsewhere."""
    if config is None:
        try:
            config = get_model_config() or {}
        except Exception as exc:  # noqa: BLE001 - routing never fails over a config read
            print(f"model_routing: could not read the model config: {exc!r}")
            config = {}
    topic_id = (topic or {}).get("topic_id")
    whose = f"topic {topic_id}'s model {model_id}" if from_topic else f"the default {model_id}"
    default = config.get("model_id") or os.environ.get("BEDROCK_MODEL_ID") or None
    for choice in (fallback_model_id, default):
        if choice and choice != model_id and not _unfit(_registry_row(choice), need):
            print(f"model_routing: {whose} is unfit for {need}; this call uses {choice}")
            return choice, (default if choice != default else None)
    try:
        listed = ranch.models if ranch is not None and ranch.on else dynamo.list_models()
        active = [
            m["model_id"]
            for m in listed
            if model_fleet.is_usable(m)
            and model_fleet.suitable(m, need)
            and m["model_id"] != model_id
            # A gated model is no pick: it would be served, and written to the topic.
            and not (ranch is not None and ranch.guard.gated(m))
        ]
    except Exception as exc:  # noqa: BLE001 - routing never fails over a registry read
        print(f"model_routing: could not list the models: {exc!r}")
        active = []
    if not active:
        return model_id, fallback_model_id  # nothing better to go to: as before
    choice = random.choice(active)
    if not (replace and from_topic and topic):
        print(f"model_routing: {whose} and its fallbacks are unfit for {need}; this call uses {choice}")
        return choice, fallback_model_id if fallback_model_id != choice else None
    print(f"model_routing: {whose} and its fallbacks are unfit for {need}; topic moves to {choice}")
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
