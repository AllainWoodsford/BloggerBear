"""Adapter for GitHub's fast-rising repositories, via the official REST Search API.

This used to scrape https://github.com/trending's HTML. That page has no API and
scraping it sits badly with GitHub's Terms of Service, so the adapter now asks the
documented Search API (https://docs.github.com/en/rest/search/search#search-repositories)
the closest question it can answer: which repositories created in the last few days
have the most stars. That is the same signal the Trending page shows -- new projects
taking off -- from an endpoint GitHub publishes for exactly this use.

One request per tick. Unauthenticated, the Search API allows 10 requests a minute per
IP, and Lambda's egress IPs are shared, so **an optional GitHub token** raises that to
30 a minute on the token's own budget. A fine-grained token with no permissions is
enough: it only reads public data. Like the CoinGecko key (crypto_feed.py), in AWS it
is a SecureString in SSM Parameter Store, named by `GITHUB_API_TOKEN_PARAMETER` (set by
Terraform) and read once per cold start; a plain `GITHUB_API_TOKEN` is still honoured,
for local runs. A token GitHub rejects (401) falls back to the same request without it,
so the token can only ever help. A rate-limited or failed call raises, so the research
tick records nothing and retries on the next heartbeat.

The state keeps the scraper's shape (`name` is still "owner/repo"), so existing
snapshots, seen-sets and star-jump detection carry on unchanged.

adapter_config (all optional):
    language            only repos in this language (e.g. "python", "go")
    created_within_days how far back "new" reaches (default 7, max 30)
    min_stars           ignore repos below this many stars (default 0)
    keywords            keep only repos whose name or description mentions one of these
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

import boto3
import requests
from botocore.exceptions import ClientError

from common.relevance import matches_keywords, normalize_keywords

from .base import Adapter

SEARCH_URL = "https://api.github.com/search/repositories"
API_VERSION = "2022-11-28"
TOKEN_ENV = "GITHUB_API_TOKEN"
TOKEN_PARAMETER_ENV = "GITHUB_API_TOKEN_PARAMETER"
REQUEST_TIMEOUT_SECONDS = 10
MAX_REPOS = 25
# Repos examined when `adapter_config.keywords` filters the results by topic (the
# kept repos are still capped at MAX_REPOS). 100 is the API's per-page maximum, so
# the wider pool still costs a single request.
FILTERED_CANDIDATE_LIMIT = 100
DEFAULT_CREATED_WITHIN_DAYS = 7
MAX_CREATED_WITHIN_DAYS = 30
USER_AGENT = "BloggerBearResearchBot/1.0 (+https://github.com/AllainWoodsford/BloggerBear)"

# A repo's star count jumping by at least this many, OR growing by at least
# STAR_JUMP_RATIO, counts as a material change even if the repo list itself
# is unchanged.
STAR_JUMP_ABSOLUTE_THRESHOLD = 100
STAR_JUMP_RATIO_THRESHOLD = 1.2


class GitHubTrendingAdapter(Adapter):
    """Finds the most-starred recently created repos (optionally per-language)."""

    def fetch_state(self, topic_config: dict) -> dict:
        adapter_config = topic_config.get("adapter_config") or {}
        keywords = normalize_keywords(adapter_config.get("keywords"))
        now = datetime.now(UTC)

        response = _search(
            {
                "q": build_query(adapter_config, now),
                "sort": "stars",
                "order": "desc",
                "per_page": FILTERED_CANDIDATE_LIMIT if keywords else MAX_REPOS,
            }
        )

        repos = [_repo_from_item(item) for item in response.json().get("items") or []]

        # The search is language-scoped at best, never topic-scoped, so a topic
        # can configure `keywords` to drop repos that aren't about it (matched
        # against the repo name and description).
        state = {"repos": repos[:MAX_REPOS], "fetched_at": now.isoformat()}
        if keywords:
            relevant = [
                repo
                for repo in repos
                if matches_keywords(f"{repo['name']} {repo['description']}", keywords)
            ]
            state["off_topic_dropped"] = len(repos) - len(relevant)
            state["repos"] = relevant[:MAX_REPOS]
        return state

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


def build_query(adapter_config: dict, now: datetime) -> str:
    """The Search API `q` string: created within the window, plus the optional
    language and star floor. Bad numbers fall back to the defaults rather than
    failing the tick."""
    days = _bounded_int(
        adapter_config.get("created_within_days"),
        DEFAULT_CREATED_WITHIN_DAYS,
        low=1,
        high=MAX_CREATED_WITHIN_DAYS,
    )
    since = (now - timedelta(days=days)).date().isoformat()
    qualifiers = [f"created:>={since}"]

    language = str(adapter_config.get("language") or "").strip()
    if language:
        # Quoted so multi-word languages ("Jupyter Notebook") stay one qualifier.
        qualifiers.append(f'language:"{language}"')

    min_stars = _bounded_int(adapter_config.get("min_stars"), 0, low=0)
    if min_stars:
        qualifiers.append(f"stars:>={min_stars}")
    return " ".join(qualifiers)


def _search(params: dict) -> requests.Response:
    """One Search API call, with the token if there is one. A rejected token (401: revoked,
    expired, mistyped) retries the same request keyless rather than failing the tick. The
    token only travels in a header and is never logged."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": API_VERSION,
        "User-Agent": USER_AGENT,
    }
    token = _api_token()
    response = requests.get(
        SEARCH_URL,
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={**headers, "Authorization": f"Bearer {token}"} if token else headers,
    )
    if token and response.status_code == 401:
        print("github_trending: GitHub rejected the API token; retrying without it")
        response = requests.get(SEARCH_URL, params=params, timeout=REQUEST_TIMEOUT_SECONDS, headers=headers)
    response.raise_for_status()
    return response


def _api_token() -> str | None:
    return (os.environ.get(TOKEN_ENV) or "").strip() or _api_token_from_ssm()


# The token read from SSM, once per Lambda container (cold start). _UNREAD until the first read;
# None afterwards means "no token". Only a definite answer (the token, or no such parameter) is kept:
# a failed read is retried on the next run rather than leaving the container keyless for its life.
_UNREAD = object()
_ssm_api_token: object = _UNREAD


def _api_token_from_ssm() -> str | None:
    """The token from the SecureString named by GITHUB_API_TOKEN_PARAMETER, or None (no parameter
    configured, none created yet, or SSM unreachable). Only the parameter's name and the error's
    type are ever logged."""
    global _ssm_api_token
    if _ssm_api_token is not _UNREAD:
        return _ssm_api_token
    name = (os.environ.get(TOKEN_PARAMETER_ENV) or "").strip()
    if not name:
        return None
    try:
        response = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ParameterNotFound":
            print(f"github_trending: no GitHub token at {name}; searching unauthenticated")
            _ssm_api_token = None
            return None
        print(f"github_trending: could not read the GitHub token at {name} ({type(exc).__name__})")
        return None
    except Exception as exc:  # noqa: BLE001 - an unreadable token only costs the rate limit
        print(f"github_trending: could not read the GitHub token at {name} ({type(exc).__name__})")
        return None
    _ssm_api_token = (response.get("Parameter", {}).get("Value") or "").strip() or None
    return _ssm_api_token


def _repo_from_item(item: dict) -> dict:
    return {
        "name": item["full_name"],
        "url": item.get("html_url") or f"https://github.com/{item['full_name']}",
        "description": item.get("description") or "",
        "stars": item.get("stargazers_count") or 0,
        "language": item.get("language"),
    }


def _bounded_int(value, default: int, *, low: int, high: int | None = None) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    number = max(low, number)
    return min(high, number) if high is not None else number
