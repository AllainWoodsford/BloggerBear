"""Helpers for normalizing article/finding source references."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def _normalized_url(url: str) -> str:
    """Normalize trivial URL variants so equivalent sources dedupe cleanly."""
    parts = urlsplit(url)
    normalized_path = parts.path.rstrip("/")
    return urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            normalized_path,
            parts.query,
            parts.fragment,
        )
    )


def dedupe_source_refs(source_refs: list[dict] | None) -> list[dict]:
    """Return source refs in first-seen order, deduplicated by URL/title.

    Articles can aggregate refs from multiple Findings, and the same source
    can legitimately appear in more than one Finding. Preserve the first-seen
    order for stable rendering, while filling any missing title/accessed_at
    fields from later duplicates when available.
    """
    deduped: list[dict] = []
    seen_indexes: dict[tuple[str, str], int] = {}

    for ref in source_refs or []:
        if not isinstance(ref, dict):
            continue
        url = ref.get("url") or ""
        title = ref.get("title") or ""
        if url:
            key = ("url", _normalized_url(url))
        elif title:
            key = ("title", title)
        else:
            deduped.append(dict(ref))
            continue

        if key not in seen_indexes:
            deduped.append(dict(ref))
            seen_indexes[key] = len(deduped) - 1
            continue

        existing = deduped[seen_indexes[key]]
        if not existing.get("title") and title:
            existing["title"] = title
        if not existing.get("accessed_at") and ref.get("accessed_at"):
            existing["accessed_at"] = ref["accessed_at"]

    return deduped
