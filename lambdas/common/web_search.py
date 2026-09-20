"""Reusable "search the web for X" capability.

Any research topic can call `search_web(query, max_results=..., max_age_hours=...)`
(or configure the generic `web_search` adapter in common/adapters/web_search.py)
without knowing which search backend is behind it. Backends are small
provider classes registered in `PROVIDERS`; the default is GDELT's public
DOC API because it needs no API key or secret. Adding a provider with
richer results (snippets/bodies -- Brave, Tavily, ...) means one new class
here plus one line in `PROVIDERS`; nothing that calls `search_web` changes.
Select a backend with the `WEB_SEARCH_PROVIDER` env var (or the `provider`
argument).

Every result is a plain dict:
    {"title": str, "url": str, "source": str, "published_at": iso str | None,
     "snippet": str | None}
`snippet` is None for providers that only return headlines (GDELT does) --
callers must not assume article text is available.

Search backends return noisy results (the same syndicated story on several
domains, loosely-related pages that merely mention the query terms), so
`search_web` de-duplicates by URL and by normalized title, and can drop
results whose title mentions none of `title_keywords`.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from datetime import UTC, datetime
from urllib.parse import urlsplit

from common.http_retry import get_json_with_backoff

DEFAULT_PROVIDER = "gdelt"
REQUEST_USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# Backends return more than asked for so filtering/de-duplication still
# leaves enough results to fill max_results.
_OVERFETCH_FACTOR = 3


class WebSearchProvider(ABC):
    """One search backend."""

    @abstractmethod
    def search(self, query: str, *, max_results: int, max_age_hours: int) -> list[dict]:
        """Return up to about `max_results` raw results, newest first, no
        older than `max_age_hours`. May return more or fewer -- `search_web`
        does the final filtering, de-duplication and capping."""
        raise NotImplementedError


class GdeltProvider(WebSearchProvider):
    """GDELT DOC 2.0 API: keyless, news-focused, headlines and metadata only.

    Asks callers to keep to about one request every 5 seconds, so its
    retries start at a 5s backoff.
    """

    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def search(self, query: str, *, max_results: int, max_age_hours: int) -> list[dict]:
        if "sourcelang:" not in query:
            query = f"{query} sourcelang:english"
        payload = get_json_with_backoff(
            self.URL,
            params={
                "query": query,
                "mode": "artlist",
                "format": "json",
                "maxrecords": min(250, max_results * _OVERFETCH_FACTOR),
                "timespan": f"{max_age_hours}h",
                "sort": "datedesc",
            },
            headers={"User-Agent": REQUEST_USER_AGENT},
            timeout=30.0,
            base_delay=5.0,
            max_delay=20.0,
        )
        return [
            {
                "title": (article.get("title") or "").strip(),
                "url": article.get("url") or "",
                "source": article.get("domain") or "",
                "published_at": _parse_gdelt_date(article.get("seendate")),
                "snippet": None,
            }
            for article in payload.get("articles") or []
        ]


def _parse_gdelt_date(raw: str | None) -> str | None:
    """GDELT dates look like 20260920T121500Z."""
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).isoformat()
    except ValueError:
        return None


PROVIDERS: dict[str, type[WebSearchProvider]] = {"gdelt": GdeltProvider}


def _normalized_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def _url_key(url: str) -> str:
    """Identity of a page for de-duplication: host + path + query, ignoring
    scheme (backends return both http and https forms of one page), case
    of the host, a trailing slash, and any fragment."""
    parts = urlsplit(url)
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}?{parts.query}"


def search_web(
    query: str,
    *,
    max_results: int = 10,
    max_age_hours: int = 24,
    title_keywords: list[str] | None = None,
    provider: str | None = None,
) -> list[dict]:
    """Search the web for `query` and return up to `max_results` de-duplicated
    results from the last `max_age_hours`, newest first.

    `title_keywords`, if given, keeps only results whose title contains at
    least one of them (case-insensitive) -- a cheap relevance filter for
    backends that match on full page text and so return tangential pages.
    Raises on backend failure (network, rate limit exhausted, bad response);
    an empty list means the search worked and found nothing.
    """
    name = provider or os.environ.get("WEB_SEARCH_PROVIDER") or DEFAULT_PROVIDER
    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"unknown web search provider {name!r} (known: {sorted(PROVIDERS)})")

    raw_results = provider_cls().search(query, max_results=max_results, max_age_hours=max_age_hours)

    keywords = [keyword.lower() for keyword in title_keywords or [] if keyword]
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    results: list[dict] = []
    for result in raw_results:
        title, url = result.get("title") or "", result.get("url") or ""
        if not title or not url:
            continue
        if keywords and not any(keyword in title.lower() for keyword in keywords):
            continue
        url_key = _url_key(url)
        normalized_title = _normalized_title(title)
        if url_key in seen_urls or normalized_title in seen_titles:
            continue
        seen_urls.add(url_key)
        seen_titles.add(normalized_title)
        results.append(result)
        if len(results) >= max_results:
            break
    return results
