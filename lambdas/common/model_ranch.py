"""The rotation guard of the Model Ranch: which models the gates keep out of the candidates
(docs/enhancements/model-ranch-prompt-assets-evals.md, section 4.5 and PR15).

A model is **gated** while one of its windows (`daily`, `weekly`, `total`) is below its gate with at
least `min_events` events behind it (evals_settings.gated_windows, over the scores the rollup puts
on its models row), unless the row's `ungated_until`, an operator's override, is still ahead. A
gated model is left out of every candidate list until its windows recover: resolve_model's rotation
draw and the registry pick it routes an unfit model to (common/model_routing.py), the models a
rotation may gain (model_fleet.rotation_picks, before prune_mode orders them), the shared pool's
roll forward (model_fleet.rotation_pool, which daily_cycle_handler._assign_rotated_model draws
from), and the rewrite picker (GET /models says which are gated; scripts/review_inbox.py ranks the
rest). Nothing is taken out of a rotation or a pool: a model whose windows recover is a candidate
again at the next draw. `evals status` (common/evals_status.py) lists the gated models by the same
rule (Guard.gated, gate and held_open), with the protected and the held-open ones apart.

**Never gated** (Guard.protected): the default (the model-config default row's model, and
BEDROCK_MODEL_ID), the fallback (that row's fallback) and the judge (evals_settings.judge_model's
chain). The pipeline cannot do without them, so they are served whatever their scores, and each
time one the gates would hold back is served, a `model_gated_protected` evidence line says so. A
topic's own single `model_id` is the operator's choice and is never gated either: only lists are
filtered. When every model in a rotation is gated, the draw falls to the topic's `model_id`, then
to the default, never back to the whole list.

**Fails open.** A row that cannot be read (not a map, an `ungated_until` that is not a time) is
never gated, and a window whose score or count is not a number never gates (gated_windows). When
the evals row or the model-config default row cannot be read, nothing is gated: without them the
guard cannot tell what is protected, and a gate never holds a model back on a guess.

**Off with evals.** While EVALS_ENABLED is not "true", nothing is filtered and nothing is read.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

from common import dynamo, evals_settings, evidence
from common.model_pricing import canonical_model_id

# The roles the gate never applies to, in the order a model playing several is named by.
PROTECTED_ROLES = ("default", "fallback", "judge")
# Why an operator's `ungated_until` holds a gate open (held_open): a time still ahead, or a value
# that is not a time (the row cannot be read, and a gate never holds a model back on a guess).
HELD_OPEN_UNTIL = "until"
HELD_OPEN_UNREADABLE = "unreadable"


def enabled() -> bool:
    """Whether the gates apply at all: evals are on."""
    return os.environ.get("EVALS_ENABLED") == "true"


def _key(model_id) -> str | None:
    """The id models are compared by: the canonical one (an ARN reduces to the profile it names)."""
    return canonical_model_id(model_id) if isinstance(model_id, str) and model_id else None


def held_open(row: dict, now: datetime) -> str | None:
    """Whether the models row's `ungated_until`, an operator's override, holds its gate open at
    `now`, and why: HELD_OPEN_UNTIL while it is a time still ahead; HELD_OPEN_UNREADABLE when it is
    not a time (read as evals_settings reads one), or `now` has no zone to compare it with, since a
    row that cannot be read is never gated; None when there is none, or it has passed. The one rule
    both the guard (gate) and `evals status` (common/evals_status.py) apply."""
    until = row.get("ungated_until")
    if until is None:
        return None
    parsed = evals_settings._parse(until)
    if parsed is None:
        return HELD_OPEN_UNREADABLE
    try:
        return HELD_OPEN_UNTIL if parsed > now else None
    except TypeError:  # a `now` without a zone cannot be compared: no gate on a guess
        return HELD_OPEN_UNREADABLE


def gated_windows(row, settings) -> list[str]:
    """The windows that gate `row` (evals_settings.gated_windows), or [] when it cannot be read."""
    try:
        return evals_settings.gated_windows(row, settings) if isinstance(row, dict) else []
    except Exception as exc:  # noqa: BLE001 - a gate never holds a model back on a guess
        print(f"model_ranch: could not read the scores of a models row: {exc!r}")
        return []


def gate(row, settings, now) -> bool:
    """Whether the models row `row` is gated under the evals `settings` in effect
    (evals_settings.effective_settings) at `now`: one of its windows is below its gate with at least
    `min_events` events behind it, and its `ungated_until` is not still ahead. False for a row or an
    override that cannot be read, so a malformed row is never held back."""
    if not isinstance(row, dict) or not isinstance(settings, dict) or not isinstance(now, datetime):
        return False
    held = held_open(row, now)
    if held == HELD_OPEN_UNREADABLE:
        print(f"model_ranch: {row.get('model_id')!r} has an ungated_until that cannot be read; not gated")
    return held is None and bool(gated_windows(row, settings))


def never_gated(settings: dict, config: dict) -> dict[str, str]:
    """{canonical id: role} of the models the gate never applies to: the default (the model-config
    default row `config`'s model, and BEDROCK_MODEL_ID), the fallback (that row's fallback) and the
    judge (evals_settings.judge_model: `judge_model_id`, else that fallback, else
    BEDROCK_MODEL_ID). The first role that applies names a model."""
    found: dict[str, str] = {}

    def protect(model_id, role):
        key = _key(model_id)
        if key and key not in found:
            found[key] = role

    protect(config.get("model_id"), "default")
    protect(os.environ.get("BEDROCK_MODEL_ID"), "default")
    protect(config.get("fallback_model_id"), "fallback")
    protect(evals_settings.judge_model(settings, config), "judge")
    return found


@dataclass
class Guard:
    """The gate for one invocation, read once: the evals settings in effect (None while evals are
    off, or when the evals row or the default row could not be read: nothing is gated then), the
    models it never applies to ({canonical id: role}), the model-config default row it read (None
    when it read none) and the time it judges by."""

    settings: dict | None = None
    protected: dict[str, str] = field(default_factory=dict)
    config: dict | None = None
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def on(self) -> bool:
        return self.settings is not None

    def role(self, model_id) -> str | None:
        """The protected role `model_id` plays (default, fallback, judge), or None."""
        return self.protected.get(_key(model_id))

    def gated(self, row) -> bool:
        """Whether the models row `row` is held back: gated (gate), and not a model the gate never
        applies to. False while the guard is off, and for a model with no row."""
        if not self.on or not isinstance(row, dict) or self.role(row.get("model_id")) is not None:
            return False
        return gate(row, self.settings, self.now)

    def healthy(self, models: list[dict]) -> list[dict]:
        """`models` (registry rows) less the ones held back, in their order."""
        return [model for model in models if not self.gated(model)]

    def served(
        self,
        model_id: str,
        row,
        *,
        caller: str,
        slot: str = "model",
        role: str | None = None,
        topic_id: str | None = None,
    ) -> None:
        """`model_id` (registry row `row`) is being served by `caller` as `slot` (the model, or the
        fallback): when the gates would hold it back, print one `model_gated_protected` line, naming
        the role that keeps it (its protected role, else `role`, e.g. a topic's own model)."""
        if not self.on or not gate(row, self.settings, self.now):
            return
        evidence.emit(
            "model_gated_protected",
            model_id=model_id,
            role=self.role(model_id) or role,
            slot=slot,
            windows=gated_windows(row, self.settings),
            caller=caller,
            topic_id=topic_id,
        )


def load_guard(config: dict | None = None, *, now: datetime | None = None) -> Guard:
    """The gate for this invocation: the evals row (one read) and the model-config default row
    (`config` when the caller has read it, else one read). While evals are off it reads nothing and
    gates nothing; when either row cannot be read it gates nothing (fail open)."""
    now = now or datetime.now(UTC)
    if not enabled():
        return Guard(now=now)
    try:
        settings = evals_settings.get_settings()
        if config is None:
            config = dynamo.get_model_config() or {}
    except Exception as exc:  # noqa: BLE001 - without the rows nothing is known gated, or protected
        print(f"model_ranch: could not read the evals or model settings; nothing is gated: {exc!r}")
        return Guard(now=now)
    return Guard(settings=settings, protected=never_gated(settings, config), config=config, now=now)


def filter_healthy(models: list[dict], guard: Guard | None = None) -> list[dict]:
    """`models` (registry rows) less the gated ones, in their order (Guard.healthy). `guard` is the
    invocation's when the caller has one; else one is loaded (load_guard), which reads nothing while
    evals are off, and then every model is kept."""
    return (guard if guard is not None else load_guard()).healthy(list(models))
