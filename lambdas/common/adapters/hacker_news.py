"""Adapter for Hacker News top stories (https://news.ycombinator.com).

Phase 7's second adapter -- picked to prove the adapter pattern
generalizes beyond an HTML scrape (github_trending.py): this one talks to
the official, public Hacker News API
(https://github.com/HackerNews/API, Firebase-backed), no auth, no API
key, no documented rate limit, no compliance sensitivity -- same low-risk
profile as the Phase 1 pick, just a different fetch shape (many small
JSON requests instead of one HTML page).
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from .base import Adapter

BASE_URL = "https://hacker-news.firebaseio.com/v0"
REQUEST_TIMEOUT_SECONDS = 10
MAX_STORIES = 25
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# A story's score jumping by at least this many, OR growing by at least
# SCORE_JUMP_RATIO_THRESHOLD, counts as a material change even if the
# top-story list itself is unchanged -- same two-threshold pattern as
# github_trending.py's star-jump detection.
SCORE_JUMP_ABSOLUTE_THRESHOLD = 100
SCORE_JUMP_RATIO_THRESHOLD = 1.5


class HackerNewsAdapter(Adapter):
    """Fetches the current Hacker News top stories via the official API."""

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        limit = int(adapter_config.get("limit") or MAX_STORIES)

        ids_response = requests.get(
            f"{BASE_URL}/topstories.json",
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        ids_response.raise_for_status()
        story_ids = ids_response.json()[:limit]

        stories = []
        for story_id in story_ids:
            item_response = requests.get(
                f"{BASE_URL}/item/{story_id}.json",
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"User-Agent": USER_AGENT},
            )
            item_response.raise_for_status()
            item = item_response.json()
            # A deleted/dead item can come back as null, or as a non-story
            # type (job/poll/comment) -- topstories.json should only ever
            # list stories, but skip defensively rather than trust that.
            if not item or item.get("type") != "story":
                continue

            stories.append(
                {
                    "id": item["id"],
                    "title": item.get("title", ""),
                    "url": item.get("url") or f"https://news.ycombinator.com/item?id={item['id']}",
                    "score": item.get("score", 0),
                    "by": item.get("by"),
                }
            )

        return {"stories": stories, "fetched_at": datetime.now(UTC).isoformat()}

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_stories = {s["id"]: s for s in old_state.get("stories", [])}
        new_stories = {s["id"]: s for s in new_state.get("stories", [])}

        entered = sorted(set(new_stories) - set(old_stories))
        left = sorted(set(old_stories) - set(new_stories))

        score_jumps = []
        for story_id in sorted(set(old_stories) & set(new_stories)):
            old_score = old_stories[story_id].get("score", 0) or 0
            new_score = new_stories[story_id].get("score", 0) or 0
            delta = new_score - old_score
            if delta <= 0:
                continue
            is_big_absolute_jump = delta >= SCORE_JUMP_ABSOLUTE_THRESHOLD
            is_big_relative_jump = old_score > 0 and new_score >= old_score * SCORE_JUMP_RATIO_THRESHOLD
            if is_big_absolute_jump or is_big_relative_jump:
                score_jumps.append((story_id, old_score, new_score, delta))

        if not entered and not left and not score_jumps:
            return False, "no material change"

        parts = []
        if entered:
            titles = ", ".join(new_stories[i]["title"] for i in entered)
            parts.append(f"entered: {titles}")
        if left:
            titles = ", ".join(old_stories[i]["title"] for i in left)
            parts.append(f"left: {titles}")
        if score_jumps:
            jump_desc = ", ".join(
                f"{new_stories[i]['title']} {o}->{n} points (+{d})" for i, o, n, d in score_jumps
            )
            parts.append(f"score jumps: {jump_desc}")

        return True, "; ".join(parts)

    def source_refs(self, new_state: dict) -> list[dict]:
        accessed_at = new_state.get("fetched_at")
        return [
            {"url": story["url"], "title": story["title"], "accessed_at": accessed_at}
            for story in new_state.get("stories", [])
        ]
