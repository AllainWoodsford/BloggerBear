"""Adapter contract shared by every research-tick data source.

Per docs/project-plan.md §6: new domains are implemented as adapters, not
core pipeline branches. `research_tick_handler.py` only ever talks to this
interface -- it must never import a concrete adapter's internals directly
outside of the small registry lookup.
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Adapter(ABC):
    """Base contract every domain adapter must implement."""

    @abstractmethod
    def fetch_state(self, topic_config: dict) -> dict:
        """Fetch and return a normalized snapshot of the source's current state.

        `topic_config` is the full Topic item from DynamoDB (includes
        `adapter_config`, a free-form dict of adapter-specific settings).
        """
        raise NotImplementedError

    @abstractmethod
    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        """Compare `old_state` to `new_state` and return (changed, diff_summary).

        `old_state` is None on the very first tick for a topic (no prior
        Finding exists yet). That case must always be treated as material --
        there is nothing to diff against, so the first observation always
        counts as a change.
        """
        raise NotImplementedError

    @abstractmethod
    def source_refs(self, new_state: dict) -> list[dict]:
        """Return one {"url", "title", "accessed_at"} dict per source item
        referenced by `new_state`, for citation in the resulting Finding.
        """
        raise NotImplementedError
