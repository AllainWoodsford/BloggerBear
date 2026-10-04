"""Source attribution: the credit line that says where a topic's data came from.

Some of the data this pipeline researches comes with a duty to say so. CoinGecko's API terms
require a visible "Powered by CoinGecko" on every plan; GDELT's ask for a citation and a link.
Others (GitHub Trending, Hacker News) ask for nothing, and are credited anyway because a reader
should be able to see where a page's facts came from.

**The credit belongs to the adapter, and is declared in code.** Each adapter class lists its
sources in `Adapter.sources` (common/adapters/base.py) as plain data:

    {"text": "Powered by CoinGecko API", "label": "CoinGecko API",
     "url": "https://www.coingecko.com/en/api"}

`text` is the whole sentence, `label` the words in it that become the link, `url` where the link
goes. It is never HTML and never comes from a Topics row or the admin API, so nobody can type
markup into it; every page that shows it escapes it anyway (`clean_sources` below is the one
place its shape is checked). Because the topic only *names* its adapter, every topic using an
adapter carries that adapter's credit automatically, including topics a fork adds.

**An article keeps the credit it was published with.** The sources are copied onto the Articles
item (`attribution`) when the article is drafted, so a later change to an adapter's declaration,
or to which adapter a topic uses, does not rewrite history. An article from before this existed
has no such field and falls back to its topic's adapter as it is today (`sources_for_article`).

**The cross-topic digest** (topic id `digest`, not a real Topics row) draws on several topics, so
it credits the union of their sources (`sources_for_topics`).

Where it shows: under the title on each static article page (common/static_pages.py), under the
open article's title and the topic's title in the single-page frontend (through
public_api_handler.py), and in the "Data sources" list on frontend/about.html.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from .adapters.registry import ADAPTER_REGISTRY
from .digest import DIGEST_TOPIC_ID

# The only keys a source has, on the way in and on the way out. Anything else on a stored item is
# dropped rather than passed through to a public page or API response.
_SOURCE_KEYS = ("text", "label", "url")
_MAX_FIELD_CHARS = 200


def _clean_source(source) -> dict | None:
    """One source as exactly {"text", "label", "url"}, or None if it is not a usable credit:
    every field a non-empty string, an `https://` URL with no whitespace, and the label somewhere
    in the text (it is the part of the sentence that gets linked)."""
    if not isinstance(source, dict):
        return None
    cleaned = {}
    for key in _SOURCE_KEYS:
        value = source.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > _MAX_FIELD_CHARS:
            return None
        cleaned[key] = value.strip()
    url = cleaned["url"]
    if not url.startswith("https://") or any(ch.isspace() for ch in url):
        return None
    if cleaned["label"] not in cleaned["text"]:
        return None
    return cleaned


def clean_sources(sources) -> list[dict]:
    """`sources` as a list of well-formed credits, in order, each once. Used on everything that
    is about to be shown or returned -- a declaration from code and a list read back from an
    Articles item alike -- so a malformed or tampered entry is left out, never rendered."""
    if not isinstance(sources, list | tuple):
        return []
    cleaned: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for source in sources:
        item = _clean_source(source)
        if item is None:
            continue
        key = (item["text"], item["url"])
        if key not in seen:
            seen.add(key)
            cleaned.append(item)
    return cleaned


def sources_for_adapter(adapter_key: str | None) -> list[dict]:
    """The sources the adapter registered as `adapter_key` declares; none for an unknown key
    (a topic whose adapter was removed has nothing to credit, and nothing researching it)."""
    adapter_cls = ADAPTER_REGISTRY.get(adapter_key) if isinstance(adapter_key, str) else None
    if adapter_cls is None:
        return []
    return clean_sources(adapter_cls.sources)


def sources_for_topic(topic: dict | None) -> list[dict]:
    """The credit for one topic: its adapter's sources."""
    if not topic:
        return []
    return sources_for_adapter(topic.get("adapter"))


def sources_for_topics(topics: Iterable[dict]) -> list[dict]:
    """The union of several topics' sources, each once, in the order first met -- what the
    cross-topic digest credits."""
    merged: list[dict] = []
    for topic in topics:
        merged.extend(sources_for_topic(topic))
    return clean_sources(merged)


def all_declared_sources() -> list[dict]:
    """Every source any registered adapter declares (the About page's "Data sources" list)."""
    merged: list[dict] = []
    for adapter_cls in ADAPTER_REGISTRY.values():
        merged.extend(adapter_cls.sources)
    return clean_sources(merged)


def sources_for_article(
    article: dict,
    *,
    get_topic: Callable[[str], dict | None],
    list_topics: Callable[[], list[dict]],
) -> list[dict]:
    """The credit to show for `article`: what was stored on it when it was drafted, else (an
    article from before attribution was stored) what its topic's adapter declares today.

    A stored empty list is an answer, not a gap: the article was drafted with nothing to credit.
    A digest with nothing stored cannot know which topics it drew on any more, so it credits
    every current topic's sources: too many rather than too few.

    The two lookups are passed in by the caller (each handler's own imports of common/dynamo.py)
    so this module reads nothing by itself and a handler's tests keep control of the reads.
    """
    stored = article.get("attribution")
    if stored is not None:
        return clean_sources(stored)
    topic_id = article.get("topic_id")
    if topic_id == DIGEST_TOPIC_ID:
        return sources_for_topics(list_topics())
    return sources_for_topic(get_topic(topic_id)) if topic_id else []
