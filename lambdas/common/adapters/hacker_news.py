"""Adapter for Hacker News top stories (https://news.ycombinator.com).

Phase 7's second adapter -- picked to prove the adapter pattern
generalizes beyond an HTML scrape (github_trending.py then; it has since
moved to GitHub's Search API): this one talks to
the official, public Hacker News API
(https://github.com/HackerNews/API, Firebase-backed), no auth, no API
key, no documented rate limit, no compliance sensitivity -- same low-risk
profile as the Phase 1 pick, just a different fetch shape (many small
JSON requests instead of one HTML page).
"""

from __future__ import annotations

from datetime import UTC, datetime

import requests

from common.relevance import matches_keywords, normalize_keywords

from .base import Adapter

BASE_URL = "https://hacker-news.firebaseio.com/v0"
REQUEST_TIMEOUT_SECONDS = 10
MAX_STORIES = 25
# Stories examined when `adapter_config.keywords` filters the feed by topic
# (the kept stories are still capped at MAX_STORIES).
FILTERED_CANDIDATE_LIMIT = 60
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# A story's score jumping by at least this many, OR growing by at least
# SCORE_JUMP_RATIO_THRESHOLD, counts as a material change even if the
# top-story list itself is unchanged -- same two-threshold pattern as
# github_trending.py's star-jump detection.
SCORE_JUMP_ABSOLUTE_THRESHOLD = 100
SCORE_JUMP_RATIO_THRESHOLD = 1.5


# The Hacker News API's documentation (read 2026-10-04) asks for no attribution and sets no terms
# of its own: https://github.com/HackerNews/API says only "There is currently no rate limit.", and
# the repository carries the MIT licence. Y Combinator's site terms
# (https://www.ycombinator.com/legal/) forbid scraping the site "Except as expressly authorized by
# Y Combinator"; the API is the channel they publish for this, which is why this adapter uses it
# and never the HTML. With no wording prescribed, this is a plain source credit.
HACKER_NEWS_SOURCE = {
    "text": "Data sourced from Hacker News",
    "label": "Hacker News",
    "url": "https://news.ycombinator.com/",
}


class HackerNewsAdapter(Adapter):
    """Fetches the current Hacker News top stories via the official API."""

    sources = (HACKER_NEWS_SOURCE,)

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        keywords = normalize_keywords(adapter_config.get("keywords"))
        # The top-stories feed is general-interest, so with a topic filter
        # look at a wider slice: most of the front page won't be on topic.
        default_limit = FILTERED_CANDIDATE_LIMIT if keywords else MAX_STORIES
        limit = int(adapter_config.get("limit") or default_limit)

        ids_response = requests.get(
            f"{BASE_URL}/topstories.json",
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        ids_response.raise_for_status()
        story_ids = ids_response.json()[:limit]

        stories = []
        off_topic_dropped = 0
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

            # Off-topic noise (a pizza-oven repo, a UFO essay) is dropped here,
            # before it can reach a summary, when the topic configures keywords.
            if not matches_keywords(item.get("title", ""), keywords):
                off_topic_dropped += 1
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

        state = {"stories": stories, "fetched_at": datetime.now(UTC).isoformat()}
        if keywords:
            state["stories"] = stories[:MAX_STORIES]
            state["off_topic_dropped"] = off_topic_dropped
        return state

    def item_keys(self, state: dict) -> set[str]:
        # Strings, not ints: the seen-set round-trips through JSON object keys.
        return {str(s["id"]) for s in state.get("stories", [])}

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if not new_state.get("stories"):
            return False, "no relevant stories to report"
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_stories = {s["id"]: s for s in old_state.get("stories", [])}
        new_stories = {s["id"]: s for s in new_state.get("stories", [])}

        # New = never reported for this topic before, not merely absent from the
        # last snapshot. A story leaving the list is context, not news.
        known = self.known_keys(old_state)
        entered = sorted(i for i in new_stories if str(i) not in known)
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

        if not entered and not score_jumps:
            return False, "no new information"

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
