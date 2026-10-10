"""Adapter contract shared by every research-tick data source.

Per docs/project-plan.md §6: new domains are implemented as adapters, not
core pipeline branches. `research_tick_handler.py` only ever talks to this
interface -- it must never import a concrete adapter's internals directly
outside of the small registry lookup.
"""
from __future__ import annotations

import json
from abc import ABC, abstractmethod

# Reserved snapshot key. The research tick records, in every snapshot it
# stores, which items it has already reported (item key -> the UTC date it was
# first seen), so "new" means new to the topic, not merely absent from the last
# snapshot. An item that drops out of a feed and comes back is not news.
# Adapters never write it -- they only read it through `known_keys`.
SEEN_KEY = "_seen"

# The most fresh evidence, as text, handed to the fresh-data reviewer.
REVIEW_EVIDENCE_MAX_CHARS = 6000


def render_review_evidence(payload: dict, max_chars: int = REVIEW_EVIDENCE_MAX_CHARS) -> str:
    """Compact JSON text for the reviewer. Keys starting with "_" are internal
    bookkeeping (like SEEN_KEY) and never shown; the text is capped, and says so."""
    shown = {key: value for key, value in payload.items() if not str(key).startswith("_")}
    text = json.dumps(shown, separators=(",", ":"), default=str)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text



class Adapter(ABC):
    """Base contract every domain adapter must implement."""

    # Opt-in: an adapter whose fetch is expensive but partly reusable within
    # a day (e.g. slow-changing history) sets this True and declares
    # `fetch_state(self, topic_config, previous_state=None)`. The research
    # tick then passes the last recorded snapshot (None on the first tick)
    # so the adapter can carry forward what it already fetched. Adapters
    # that leave this False keep the plain `fetch_state(topic_config)`.
    uses_previous_state: bool = False

    # Opt-in: an adapter whose state is a running record of every observation (a baseline built
    # from all of them, not only those that were reported) sets this True, usually with
    # `uses_previous_state`. The research tick then also stores the state after a tick that
    # found nothing material, at one fixed key per topic, and hands back the newest state as
    # `previous_state` and as `old_state` to `material_diff`. Without it, a no-change tick's
    # state is dropped and the next tick sees the last *reported* snapshot.
    keeps_running_state: bool = False

    # Required: where this adapter's data comes from, as the credit shown to readers (see
    # common/attribution.py). One {"text", "label", "url"} dict per source: `text` is the whole
    # sentence, `label` is the part of it that becomes the link, `url` is an https address.
    # Plain data, never HTML: every page escapes it. Use the source's own required wording where
    # its terms prescribe one, and quote the sentence relied on (with its URL) in a comment beside
    # the declaration. An adapter that can draw on several sources lists each. A registered
    # adapter with no source fails tests/test_attribution.py, so a new adapter cannot ship
    # uncredited.
    sources: tuple[dict, ...] = ()

    # Optional, for a source whose numbers move while an article is being written (prices). The
    # pipeline stays topic-agnostic: it reads these three and knows nothing else about the domain.
    #
    # `figure_tolerance_percent`: how far a figure in an article may sit from the source's before
    # it counts as wrong. The fresh-data reviewer is told not to flag a difference inside it, a
    # claim it flags anyway is dropped by code (common/fresh_review.py), and a correction or
    # Re-Write may state a figure that close to a source's. 0 (the default) keeps every check exact.
    figure_tolerance_percent: float = 0.0
    # `figure_guidance`: how to write such figures so they stay true ("more than 30%", not
    # "36.2%"). Added to the drafting, correction and Re-Write prompts.
    figure_guidance: str = ""
    # `drafting_guidance`: anything else this source's articles should always do (their shape,
    # how they end). Added to the drafting prompt, and to a Re-Write so it keeps that shape.
    drafting_guidance: str = ""

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

    def review_evidence(self, topic_config: dict, latest_state: dict | None) -> str | None:
        """Current data for the fresh-data review (docs/project-plan.md §11): what the
        source says *now*, as text, for comparing an article's claims against.

        `latest_state` is the topic's most recent stored snapshot (None if it cannot be
        loaded), so an adapter can re-check *what it was looking at*. The default
        re-runs the adapter's normal fetch; an adapter whose fetch samples something
        new every time (the crypto feed's random pool) overrides this to look at the
        same things again. Return None to opt the topic out of the review. Raising is
        fine: the caller treats any failure as "review unavailable", never as a pass.
        """
        if self.uses_previous_state:
            state = self.fetch_state(topic_config, previous_state=latest_state)
        else:
            state = self.fetch_state(topic_config)
        return render_review_evidence(state)

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
