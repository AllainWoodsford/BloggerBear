"""Generic "search the web" adapter -- the reusable research source.

Any topic can use it by setting `adapter = "web_search"` and an
`adapter_config`; nothing here is specific to one domain (per
docs/project-plan.md §6). It is a thin adapter over common/web_search.py's
`search_web`, which is also what other adapters (e.g. the crypto feed's
"web aggregator" mode) call directly when they need web results as *part*
of a richer state.

This is also the default adapter for a topic created without one: with no
`queries`/`query` configured it searches on the topic's own name (see
`default_query_for_topic`), so a bare topic still does independent web research.

adapter_config (all optional; a search string defaults to the topic's name):
    queries          list of search strings (or `query`, a single string)
    max_results      total results kept across all queries (default 10, max 25)
    max_age_hours    only results newer than this (default 24)
    title_keywords   keep only results whose title mentions one of these (whole
                     words; a trailing "*" is a prefix match). If omitted, each
                     query's own topical words are used, so a page that matched
                     the query only in passing is dropped; set [] to turn the
                     filter off.
    provider        search backend name (default: WEB_SEARCH_PROVIDER env
                     var, else GDELT -- see common/web_search.py)
    min_new_results  how many never-before-seen results make a tick
                     "material" (default 3)
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from common.relevance import keywords_from_query, normalize_keywords
from common.web_search import search_web

from .base import Adapter

DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_LIMIT = 25
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_MIN_NEW_RESULTS = 3


def configured_queries(adapter_config: dict) -> list[str]:
    queries = adapter_config.get("queries")
    if not queries and adapter_config.get("query"):
        queries = [adapter_config["query"]]
    return [q for q in (queries or []) if isinstance(q, str) and q.strip()]


_NAME_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "into", "about"})
_NAME_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.-]*")
_MAX_NAME_TERMS = 8


def default_query_for_topic(topic_config: dict) -> str | None:
    """A baseline search for a topic with no configured query: its name's
    meaningful words OR-ed together, so any new topic can do independent web
    research with no adapter_config at all. Deliberately broad -- a configured
    `query` is always sharper -- and the topic's editorial goal and relevance
    guardrails keep what it finds on topic. None if the name has no usable words.
    """
    name = topic_config.get("name")
    if not isinstance(name, str) or not name.strip():
        # A topic id like "security-trends" reads as words; "zero-day" in a
        # name is a term, so only ids get their separators split.
        name = str(topic_config.get("topic_id") or "").replace("-", " ").replace("_", " ")
    words: list[str] = []
    seen: set[str] = set()
    for word in _NAME_WORD_RE.findall(name):
        word = word.strip(".-")
        if len(word) >= 3 and word.lower() not in _NAME_STOPWORDS and word.lower() not in seen:
            seen.add(word.lower())
            words.append(word)
    words = words[:_MAX_NAME_TERMS]
    if not words:
        return None
    return words[0] if len(words) == 1 else "(" + " OR ".join(words) + ")"


class WebSearchAdapter(Adapter):
    """Runs the configured search queries and snapshots the merged results."""

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        queries = configured_queries(adapter_config)
        if not queries:
            fallback = default_query_for_topic(topic_config)
            queries = [fallback] if fallback else []
        if not queries:
            raise ValueError(
                "web_search adapter needs adapter_config 'queries' (or 'query'), "
                "or a topic name to search for"
            )

        max_results = min(
            int(adapter_config.get("max_results") or DEFAULT_MAX_RESULTS), MAX_RESULTS_LIMIT
        )
        max_age_hours = int(adapter_config.get("max_age_hours") or DEFAULT_MAX_AGE_HOURS)

        configured_keywords = adapter_config.get("title_keywords")

        merged: list[dict] = []
        seen_urls: set[str] = set()
        for query in queries:
            if configured_keywords is None:
                title_keywords = keywords_from_query(query) or None
            else:
                title_keywords = normalize_keywords(configured_keywords) or None
            for result in search_web(
                query,
                max_results=max_results,
                max_age_hours=max_age_hours,
                title_keywords=title_keywords,
                provider=adapter_config.get("provider"),
            ):
                if result["url"] not in seen_urls:
                    seen_urls.add(result["url"])
                    merged.append(result)

        merged.sort(key=lambda r: r.get("published_at") or "", reverse=True)
        return {
            "queries": queries,
            "results": merged[:max_results],
            # Carried in the snapshot because material_diff only sees
            # states, not the topic's adapter_config.
            "min_new_results": int(adapter_config.get("min_new_results") or DEFAULT_MIN_NEW_RESULTS),
            "fetched_at": datetime.now(UTC).isoformat(),
        }

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if not new_state.get("results"):
            return False, "no relevant results to report"
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_urls = {r["url"] for r in old_state.get("results", [])}
        fresh = [r for r in new_state.get("results", []) if r["url"] not in old_urls]
        threshold = new_state.get("min_new_results", DEFAULT_MIN_NEW_RESULTS)
        if len(fresh) < threshold:
            return False, "no material change"

        titles = "; ".join(r["title"] for r in fresh[:5])
        return True, f"{len(fresh)} new results: {titles}"

    def source_refs(self, new_state: dict) -> list[dict]:
        accessed_at = new_state.get("fetched_at")
        return [
            {"url": r["url"], "title": r.get("title") or r["url"], "accessed_at": accessed_at}
            for r in new_state.get("results", [])
        ]
