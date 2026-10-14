# Adapter registry key for common/adapters/crypto_feed.py's
# CryptoFeedAdapter. Shared here (rather than each importer hardcoding the
# literal string) so research_tick_handler.py's ADAPTER_REGISTRY and
# admin_api_handler.py's forced is_financial=True logic (see
# crypto_feed.py's module docstring for why that split exists) can never
# drift apart.
CRYPTO_FEED_ADAPTER_KEY = "crypto_feed"

# Adapter registry key for common/adapters/web_search.py's WebSearchAdapter --
# the generic, reusable "search the web for these queries" adapter any
# research topic can be configured with (see common/web_search.py).
WEB_SEARCH_ADAPTER_KEY = "web_search"

# Adapter registry key for common/adapters/satellite_vision.py's SatelliteVisionAdapter -- counts
# what the vision worker sees at fixed sites in Sentinel-2 scenes (OpenCV competition entry,
# docs/enhancements/opencv-agentic-vision-enhancement.md).
SATELLITE_VISION_ADAPTER_KEY = "satellite_vision"

# Adapter registry key for common/adapters/rail_access.py's RailAccessAdapter -- maps how much of
# a city's built-up area lies within walking reach of a rail station, from Sentinel-2 scenes and
# OpenStreetMap's rail network (docs/enhancements/rail-access-monitor.md).
RAIL_ACCESS_ADAPTER_KEY = "rail_access"

# The adapters whose every article a person sees before it is published: admin_api_handler.py
# forces `force_manual_review` on for a topic using one of them, on create and on update, as it
# forces is_financial for the crypto feed. Processed imagery and maps of a city are published only
# once someone has looked at them.
MANUAL_REVIEW_ADAPTERS = frozenset({SATELLITE_VISION_ADAPTER_KEY, RAIL_ACCESS_ADAPTER_KEY})
