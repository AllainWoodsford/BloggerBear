"""Generic "search the web" adapter -- the reusable research source.

Any topic can use it by setting `adapter = "web_search"` and an
`adapter_config`; nothing here is specific to one domain (per
docs/project-plan.md §6). It is a thin adapter over common/web_search.py's
`search_web`, which is also what other adapters (e.g. the crypto feed's
"web aggregator" mode) call directly when they need web results as *part*
of a richer state.

adapter_config (all optional except one of `queries`/`query`):
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


class WebSearchAdapter(Adapter):
    """Runs the configured search queries and snapshots the merged results."""

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        queries = configured_queries(adapter_config)
        if not queries:
            raise ValueError("web_search adapter needs adapter_config 'queries' (or 'query')")

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
