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


class GitHubTrendingAdapter(Adapter):
    """Scrapes the public GitHub Trending page (optionally per-language)."""

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

        repos = self._parse_repos(response.text)
        return {
            "repos": repos[:MAX_REPOS],
            "fetched_at": datetime.now(UTC).isoformat(),
        }

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

    def material_diff(self, old_state: dict | None, new_state: dict) -> tuple[bool, str]:
        if old_state is None:
            return True, "initial observation: no prior snapshot to compare against"

        old_repos = {r["name"]: r for r in old_state.get("repos", [])}
        new_repos = {r["name"]: r for r in new_state.get("repos", [])}

        entered = sorted(set(new_repos) - set(old_repos))
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

        if not entered and not left and not star_jumps:
            return False, "no material change"

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
