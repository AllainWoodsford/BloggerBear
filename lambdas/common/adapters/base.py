"""Adapter contract shared by every research-tick data source.

Per docs/project-plan.md §6: new domains are implemented as adapters, not
core pipeline branches. `research_tick_handler.py` only ever talks to this
interface -- it must never import a concrete adapter's internals directly
outside of the small registry lookup.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

# Reserved snapshot key. The research tick records, in every snapshot it
# stores, which items it has already reported (item key -> the UTC date it was
# first seen), so "new" means new to the topic, not merely absent from the last
# snapshot. An item that drops out of a feed and comes back is not news.
# Adapters never write it -- they only read it through `known_keys`.
SEEN_KEY = "_seen"


class Adapter(ABC):
    """Base contract every domain adapter must implement."""

    # Opt-in: an adapter whose fetch is expensive but partly reusable within
    # a day (e.g. slow-changing history) sets this True and declares
    # `fetch_state(self, topic_config, previous_state=None)`. The research
    # tick then passes the last recorded snapshot (None on the first tick)
    # so the adapter can carry forward what it already fetched. Adapters
    # that leave this False keep the plain `fetch_state(topic_config)`.
    uses_previous_state: bool = False

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

    def item_keys(self, state: dict) -> set[str]:
        """Stable identifiers (a URL, an id, a repo name) for each individual
        item in `state`. An adapter whose source is made of discrete items
        returns them here so the research tick can remember what it has
        already reported; the default is none, which leaves the adapter
        comparing snapshots itself.
        """
        return set()

    def known_keys(self, old_state: dict | None) -> set[str]:
        """Every item key already reported for this topic, as of `old_state`
        (the last stored snapshot; None on the first tick).

        Use it in `material_diff` to decide what counts as new information:
        an item is new only if its key is not in this set. Snapshots stored
        before `SEEN_KEY` existed fall back to the items they contain.
        """
        if not old_state:
            return set()
        return set(old_state.get(SEEN_KEY) or {}) | self.item_keys(old_state)

    @abstractmethod
    def source_refs(self, new_state: dict) -> list[dict]:
        """Return one {"url", "title", "accessed_at"} dict per source item
        referenced by `new_state`, for citation in the resulting Finding.
        """
        raise NotImplementedError

    def build_summary_prompt(
        self, topic: dict, diff_summary: str, new_state: dict
    ) -> str | None:
        """Optionally return the exact Bedrock prompt used to summarize a
        material change, for sources whose state needs domain-specific
        framing (e.g. a rotating editorial focus). Return None -- the
        default -- to use the research tick's generic prompt.

        Lives on the adapter rather than in research_tick_handler.py so that
        handler stays topic-agnostic (docs/project-plan.md §2 rule 5, §6).
        """
        return None
