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

    for ref in source_refs or []:
        if not isinstance(ref, dict):
            continue
        url = ref.get("url") or ""
        title = ref.get("title") or ""
        existing = _find_matching_ref(deduped, url=url, title=title)
        if existing is None:
            deduped.append(dict(ref))
            continue

        if not existing.get("title") and title:
            existing["title"] = title
        if not existing.get("accessed_at") and ref.get("accessed_at"):
            existing["accessed_at"] = ref["accessed_at"]

    return deduped


def _find_matching_ref(deduped: list[dict], *, url: str, title: str) -> dict | None:
    normalized_url = _normalized_url(url) if url else ""

    for existing in deduped:
        existing_url = existing.get("url") or ""
        existing_title = existing.get("title") or ""

        if normalized_url and existing_url and _normalized_url(existing_url) == normalized_url:
            if not title or not existing_title or existing_title == title:
                return existing
            continue

        if not normalized_url and title and not existing_url and existing_title == title:
            return existing

    return None
