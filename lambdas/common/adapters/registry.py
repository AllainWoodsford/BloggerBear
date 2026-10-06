"""Maps a Topic's `adapter` field to the concrete Adapter implementation.

Shared by every handler that needs to ask an adapter something -- the research
tick (fetch and diff) and the daily cycle's fresh-data review -- so neither
imports concrete adapters itself and the two can never disagree about which
adapters exist.

Adding a new domain means adding one line here plus a new adapter module: no
change to any handler (docs/project-plan.md §2 rule 5). The crypto and web-search
keys come from common/adapters/__init__.py, shared with admin_api_handler.py's
forced is_financial logic, so they can't drift apart.
"""

from __future__ import annotations

from . import CRYPTO_FEED_ADAPTER_KEY, SATELLITE_VISION_ADAPTER_KEY, WEB_SEARCH_ADAPTER_KEY
from .base import Adapter
from .crypto_feed import CryptoFeedAdapter
from .github_trending import GitHubTrendingAdapter
from .hacker_news import HackerNewsAdapter
from .satellite_vision import SatelliteVisionAdapter
from .web_search import WebSearchAdapter

ADAPTER_REGISTRY: dict[str, type[Adapter]] = {
    "github_trending": GitHubTrendingAdapter,
    "hacker_news": HackerNewsAdapter,
    CRYPTO_FEED_ADAPTER_KEY: CryptoFeedAdapter,
    WEB_SEARCH_ADAPTER_KEY: WebSearchAdapter,
    SATELLITE_VISION_ADAPTER_KEY: SatelliteVisionAdapter,
}
