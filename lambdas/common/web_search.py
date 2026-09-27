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

**Fallback.** GDELT times out and rate-limits requests from Lambda often
enough to leave whole topics with no findings, so when the AgentCore web
search gateway is configured (`AGENTCORE_WEB_SEARCH_URL`, set by
infra/modules/web-search) a failed GDELT search is retried once through it
(`AgentCoreProvider`). GDELT then gets a short time budget of its own, so a
bad GDELT day costs seconds, not minutes, before the fallback runs. AgentCore
is priced per query, which is why it is the fallback rather than the
default; a topic can still ask for it directly (`provider: "agentcore"`).

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

import json
import os
import re
import time
from abc import ABC, abstractmethod
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import boto3
import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from common.http_retry import get_json_with_backoff
from common.relevance import matches_keywords, normalize_keywords
from common.stats_tracking import record_web_search_fallback, record_web_search_query

DEFAULT_PROVIDER = "gdelt"
REQUEST_USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# Per request. GDELT can be very slow (a single search has been seen to take 80s+ with
# retries), so a caller with a time budget passes `deadline` to cap the whole search.
_REQUEST_TIMEOUT_SECONDS = 30.0

# Backends return more than asked for so filtering/de-duplication still
# leaves enough results to fill max_results.
_OVERFETCH_FACTOR = 3


class WebSearchProvider(ABC):
    """One search backend."""

    @abstractmethod
    def search(
        self, query: str, *, max_results: int, max_age_hours: int, deadline: float | None = None
    ) -> list[dict]:
        """Return up to about `max_results` raw results, newest first, no
        older than `max_age_hours`. May return more or fewer -- `search_web`
        does the final filtering, de-duplication and capping.

        `deadline` (an absolute `time.monotonic()` value) is a total time budget: a
        provider should give up and raise rather than run past it."""
        raise NotImplementedError


class GdeltProvider(WebSearchProvider):
    """GDELT DOC 2.0 API: keyless, news-focused, headlines and metadata only.

    Asks callers to keep to about one request every 5 seconds, so its
    retries start at a 5s backoff.
    """

    URL = "https://api.gdeltproject.org/api/v2/doc/doc"

    def search(
        self, query: str, *, max_results: int, max_age_hours: int, deadline: float | None = None
    ) -> list[dict]:
        timeout = _REQUEST_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no time left in the search budget")
            timeout = min(timeout, remaining)
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
            timeout=timeout,
            base_delay=5.0,
            max_delay=20.0,
            deadline=deadline,
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


class AgentCoreProvider(WebSearchProvider):
    """Amazon Bedrock AgentCore's Web Search Tool, through this stack's gateway
    (infra/modules/web-search): Amazon's own web index, with snippets and publish dates.

    One SigV4-signed MCP `tools/call` per search -- no API key, no session handshake. Takes a
    natural-language query of at most 200 characters, so GDELT-style operators are stripped
    first (`natural_query`). The age window is sent as the connector's published-date filter.
    No retries: it is the fallback, and a caller's deadline bounds it.
    """

    MCP_PROTOCOL_VERSION = "2025-11-25"
    MAX_QUERY_CHARS = 200
    MAX_RESULTS = 25

    def search(
        self, query: str, *, max_results: int, max_age_hours: int, deadline: float | None = None
    ) -> list[dict]:
        url = os.environ.get("AGENTCORE_WEB_SEARCH_URL")
        region = os.environ.get("AGENTCORE_WEB_SEARCH_REGION")
        tool = os.environ.get("AGENTCORE_WEB_SEARCH_TOOL")
        if not (url and region and tool):
            raise RuntimeError("AgentCore web search is not configured (AGENTCORE_WEB_SEARCH_*)")
        timeout = _AGENTCORE_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no time left in the search budget")
            timeout = min(timeout, remaining)

        now = datetime.now(UTC)
        arguments = {
            "query": natural_query(query, self.MAX_QUERY_CHARS),
            "maxResults": min(self.MAX_RESULTS, max_results * _OVERFETCH_FACTOR),
            "filters": {
                "publishedDateFilter": {
                    "from": _iso_z(now - timedelta(hours=max_age_hours)),
                    "to": _iso_z(now),
                }
            },
        }
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "search",
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            }
        )
        endpoint = url if url.rstrip("/").endswith("/mcp") else f"{url.rstrip('/')}/mcp"
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.MCP_PROTOCOL_VERSION,
        }
        signed = AWSRequest(method="POST", url=endpoint, data=body, headers=headers)
        SigV4Auth(boto3.Session().get_credentials(), "bedrock-agentcore", region).add_auth(signed)
        response = requests.post(endpoint, data=body, headers=dict(signed.headers), timeout=timeout)
        response.raise_for_status()
        # Counted once the gateway has accepted the request (HTTP 2xx), before its reply is read:
        # a tool-level error inside a 2xx still reached the paid search, so it is counted rather
        # than risk under-reporting spend. A request refused outright (4xx/5xx, a timeout) is not.
        record_web_search_query()
        return [_agentcore_result(item) for item in _agentcore_items(response)]


# Per request; the fallback path's own deadline usually caps it first.
_AGENTCORE_TIMEOUT_SECONDS = 20.0
_OPERATOR = re.compile(r"\b[a-z]+:\S+", re.IGNORECASE)  # GDELT's sourcelang:english, domain:x.com, ...


def natural_query(query: str, max_chars: int = AgentCoreProvider.MAX_QUERY_CHARS) -> str:
    """`query` as plain words for a natural-language search: GDELT operators, parentheses and
    OR dropped (quoted phrases kept), cut to `max_chars` at a word boundary."""
    text = _OPERATOR.sub(" ", query)
    text = re.sub(r"[()]", " ", text)
    text = re.sub(r"\bOR\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars + 1].rsplit(" ", 1)[0]
    if cut.count('"') % 2:  # never leave a phrase's quote open
        cut = cut.rsplit('"', 1)[0].rstrip()
    return cut


def _iso_z(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _jsonrpc_message(response) -> dict:
    """The JSON-RPC reply, from a plain JSON body or a server-sent-events stream (the
    gateway may answer either way; the last `data:` event carrying a result wins)."""
    if "text/event-stream" in (response.headers.get("Content-Type") or ""):
        message = None
        for line in response.text.splitlines():
            if line.startswith("data:"):
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(event, dict) and ("result" in event or "error" in event):
                    message = event
        if message is None:
            raise RuntimeError("AgentCore web search: no result in the event stream")
        return message
    return response.json()


def _agentcore_items(response) -> list[dict]:
    """The connector's result rows, or an exception for any kind of failure."""
    message = _jsonrpc_message(response)
    if message.get("error"):
        raise RuntimeError(f"AgentCore web search failed: {message['error']}")
    result = message.get("result") or {}
    if result.get("isError"):
        detail = " ".join(c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict))
        raise RuntimeError(f"AgentCore web search tool error: {detail[:300]}")
    payload = result.get("structuredContent")
    if not isinstance(payload, dict):
        texts = [c.get("text") for c in result.get("content") or [] if isinstance(c, dict)]
        text = next((t for t in texts if isinstance(t, str) and t.strip()), None)
        try:
            payload = json.loads(text) if text else {}
        except ValueError as exc:
            raise RuntimeError("AgentCore web search returned text that is not JSON") from exc
    items = payload.get("results") if isinstance(payload, dict) else None
    return [item for item in items or [] if isinstance(item, dict)]


def _agentcore_result(item: dict) -> dict:
    url = item.get("url") or ""
    host = urlsplit(url).netloc.lower()
    return {
        "title": (item.get("title") or "").strip(),
        "url": url,
        "source": host[4:] if host.startswith("www.") else host,
        "published_at": _parse_published_date(item.get("publishedDate")),
        "snippet": (item.get("text") or "").strip() or None,
    }


def _parse_published_date(raw) -> str | None:
    """A publish date ("2026-09-26" or a full ISO timestamp) as an ISO UTC string."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat()


PROVIDERS: dict[str, type[WebSearchProvider]] = {"gdelt": GdeltProvider, "agentcore": AgentCoreProvider}

FALLBACK_PROVIDER = "agentcore"
# With a fallback available, GDELT gets at most this long (its own retries included)...
PRIMARY_BUDGET_WITH_FALLBACK_SECONDS = 20.0
# ...and always leaves at least this much of a caller's deadline for the fallback.
FALLBACK_RESERVE_SECONDS = 8.0


def _fallback_configured(name: str) -> bool:
    return name != FALLBACK_PROVIDER and bool(os.environ.get("AGENTCORE_WEB_SEARCH_URL"))


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
    deadline: float | None = None,
) -> list[dict]:
    """Search the web for `query` and return up to `max_results` de-duplicated
    results from the last `max_age_hours`, newest first.

    `title_keywords`, if given, keeps only results whose title contains at
    least one of them as a whole word, case-insensitive (see
    common/relevance.py: a trailing "*" makes a keyword a prefix match) -- a
    cheap relevance filter for backends that match on full page text and so
    return tangential pages.
    `deadline` (an absolute `time.monotonic()` value) caps the whole search,
    retries included -- past it the search raises instead of waiting.
    When the AgentCore gateway is configured, a failed search is retried once
    through it (see the module docstring); the fallback's own failure is what
    is raised then.
    Raises on backend failure (network, rate limit exhausted, bad response);
    an empty list means the search worked and found nothing.
    """
    name = provider or os.environ.get("WEB_SEARCH_PROVIDER") or DEFAULT_PROVIDER
    provider_cls = PROVIDERS.get(name)
    if provider_cls is None:
        raise ValueError(f"unknown web search provider {name!r} (known: {sorted(PROVIDERS)})")

    def run(cls: type[WebSearchProvider], until: float | None) -> list[dict]:
        # Only passed when set, so a provider written before `deadline` existed still works.
        budget = {"deadline": until} if until is not None else {}
        return cls().search(query, max_results=max_results, max_age_hours=max_age_hours, **budget)

    if not _fallback_configured(name):
        raw_results = run(provider_cls, deadline)
    else:
        primary_deadline = time.monotonic() + PRIMARY_BUDGET_WITH_FALLBACK_SECONDS
        if deadline is not None:
            primary_deadline = min(primary_deadline, deadline - FALLBACK_RESERVE_SECONDS)
        try:
            raw_results = run(provider_cls, primary_deadline)
        except Exception as exc:  # noqa: BLE001 - any failure is what the fallback is for
            print(f"web_search: {name} failed ({exc!r}); trying the AgentCore web search instead")
            record_web_search_fallback()
            raw_results = run(PROVIDERS[FALLBACK_PROVIDER], deadline)

    keywords = normalize_keywords(title_keywords)
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    results: list[dict] = []
    for result in raw_results:
        title, url = result.get("title") or "", result.get("url") or ""
        if not title or not url:
            continue
        if not matches_keywords(title, keywords):
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
