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
final safety net and always works.
"""

from __future__ import annotations

import os
import random

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

    if topic:
        candidates = [c for c in (topic.get("model_id_candidates") or []) if c]
        if candidates:
            model_id = random.choice(candidates)
        else:
            model_id = topic.get("model_id") or None
        fallback_model_id = topic.get("fallback_model_id") or None

    if model_id is None or fallback_model_id is None:
        config = get_model_config() or {}
        if model_id is None:
            model_id = config.get("model_id") or None
        if fallback_model_id is None:
            fallback_model_id = config.get("fallback_model_id") or None

    if model_id is None:
        model_id = os.environ["BEDROCK_MODEL_ID"]

    return model_id, fallback_model_id
