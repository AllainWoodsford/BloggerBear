# Adapter registry key for common/adapters/crypto_feed.py's
# CryptoFeedAdapter. Shared here (rather than each importer hardcoding the
# literal string) so research_tick_handler.py's ADAPTER_REGISTRY and
# admin_api_handler.py's forced is_financial=True logic (see
# crypto_feed.py's module docstring for why that split exists) can never
# drift apart.
CRYPTO_FEED_ADAPTER_KEY = "crypto_feed"
