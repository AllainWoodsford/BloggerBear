"""Adapter for https://github.com/trending.

Picked as the Phase 1 adapter per docs/PROGRESS.md: no auth, no rate-limit
pain, no compliance sensitivity. Pure HTML scrape via `requests` +
`beautifulsoup4` -- no API key, no financial-advice concerns.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

import requests
from bs4 import BeautifulSoup

from common.relevance import matches_keywords, normalize_keywords

from .base import Adapter

TRENDING_URL = "https://github.com/trending"
REQUEST_TIMEOUT_SECONDS = 10
MAX_REPOS = 25
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# A repo's star count jumping by at least this many, OR growing by at least
# STAR_JUMP_RATIO, counts as a material change even if the repo list itself
# is unchanged.
STAR_JUMP_ABSOLUTE_THRESHOLD = 100
STAR_JUMP_RATIO_THRESHOLD = 1.2


# What GitHub's terms say about this adapter (read 2026-10-04), stated plainly because the page
# is scraped -- there is no API for Trending:
#
#   GitHub Acceptable Use Policies, section 7 "Information Usage Restrictions"
#   https://docs.github.com/en/site-policy/acceptable-use-policies/github-acceptable-use-policies
#   "You may use information from our Service for the following reasons, regardless of whether
#   the information was scraped, collected through our API, or obtained otherwise: Researchers
#   may use public, non-personal information from the Service for research purposes, only if any
#   publications resulting from that research are open access. Archivists may use public
#   information from the Service for archival purposes."
#   "You may not use information from the Service (whether scraped, collected through our API, or
#   obtained otherwise) for spamming purposes, including for the purposes of sending unsolicited
#   emails to users or selling personal information"
#   Section 4 forbids "any form of excessive automated bulk activity" and placing "undue burden on
#   our servers through automated means".
#
# So: scraping is not banned outright, and what this adapter does is small (one page per research
# tick, a User-Agent that names the project, nothing sold, no one contacted). But the only uses
# that section expressly permits are open-access research and archiving, and a blog summarising
# the page is neither in so many words. The terms ask for no attribution and prescribe no wording,
# so the line below is a plain source credit; it does not by itself make the use authorised.
# Whether to keep, demote or drop this topic is the owner's decision (see the rules check in
# docs/enhancements/alexa-plus-operator-assistant-enhancement.md).
GITHUB_TRENDING_SOURCE = {
    "text": "Data sourced from GitHub Trending",
    "label": "GitHub Trending",
    "url": TRENDING_URL,
}


class GitHubTrendingAdapter(Adapter):
    """Scrapes the public GitHub Trending page (optionally per-language)."""

    sources = (GITHUB_TRENDING_SOURCE,)

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        language = adapter_config.get("language")
        url = f"{TRENDING_URL}/{language}" if language else TRENDING_URL

        response = requests.get(
            url,
            timeout=REQUEST_TIMEOUT_SECONDS,
            headers={"User-Agent": USER_AGENT},
        )
        response.raise_for_status()

        repos = self._parse_repos(response.text)[:MAX_REPOS]

        # Trending is language-scoped at best, never topic-scoped, so a topic
        # can configure `keywords` to drop repos that aren't about it (matched
        # against the repo name and description).
        keywords = normalize_keywords(adapter_config.get("keywords"))
        state = {"repos": repos, "fetched_at": datetime.now(UTC).isoformat()}
        if keywords:
            relevant = [
                repo
                for repo in repos
                if matches_keywords(f"{repo['name']} {repo['description']}", keywords)
            ]
            state["off_topic_dropped"] = len(repos) - len(relevant)
            state["repos"] = relevant
        return state

    @staticmethod
    def _parse_repos(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        repos: list[dict] = []

        for row in soup.find_all("article"):
            heading = row.find("h2") or row.find("h1")
            anchor = heading.find("a") if heading else None
            href = anchor.get("href", "").strip() if anchor else ""
            if not href:
                continue

            name = " ".join(anchor.get_text(strip=True).split())
            name = name.replace(" / ", "/")
            url = f"https://github.com{href}" if href.startswith("/") else href

            desc_tag = row.find("p")
            description = desc_tag.get_text(strip=True) if desc_tag else ""

            language_tag = row.find(attrs={"itemprop": "programmingLanguage"})
            language = language_tag.get_text(strip=True) if language_tag else None

            stars = 0
            star_anchor = row.find("a", href=re.compile(r"/stargazers$"))
            if star_anchor:
                stars = _parse_int(star_anchor.get_text(strip=True))

            repos.append(
                {
                    "name": name,
                    "url": url,
                    "description": description,
                    "stars": stars,
                    "language": language,
                }
            )

        return repos

    def item_keys(self, state: dict) -> set[str]:
        return {r["name"] for r in state.get("repos", [])}

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if not new_state.get("repos"):
            return False, "no relevant repos to report"
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_repos = {r["name"]: r for r in old_state.get("repos", [])}
        new_repos = {r["name"]: r for r in new_state.get("repos", [])}

        # New = never reported for this topic before, not merely absent from the
        # last snapshot. A repo leaving the list is context, not news.
        known = self.known_keys(old_state)
        entered = sorted(n for n in new_repos if n not in known)
        left = sorted(set(old_repos) - set(new_repos))

        star_jumps = []
        for name in sorted(set(old_repos) & set(new_repos)):
            old_stars = old_repos[name].get("stars", 0) or 0
            new_stars = new_repos[name].get("stars", 0) or 0
            delta = new_stars - old_stars
            if delta <= 0:
                continue
            is_big_absolute_jump = delta >= STAR_JUMP_ABSOLUTE_THRESHOLD
            is_big_relative_jump = old_stars > 0 and new_stars >= old_stars * STAR_JUMP_RATIO_THRESHOLD
            if is_big_absolute_jump or is_big_relative_jump:
                star_jumps.append((name, old_stars, new_stars, delta))

        if not entered and not star_jumps:
            return False, "no new information"

        parts = []
        if entered:
            parts.append(f"entered: {', '.join(entered)}")
        if left:
            parts.append(f"left: {', '.join(left)}")
        if star_jumps:
            jump_desc = ", ".join(f"{n} {o}->{s} stars (+{d})" for n, o, s, d in star_jumps)
            parts.append(f"star jumps: {jump_desc}")

        return True, "; ".join(parts)

    def source_refs(self, new_state: dict) -> list[dict]:
        accessed_at = new_state.get("fetched_at")
        return [
            {"url": repo["url"], "title": repo["name"], "accessed_at": accessed_at}
            for repo in new_state.get("repos", [])
        ]


def _parse_int(text: str) -> int:
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0
