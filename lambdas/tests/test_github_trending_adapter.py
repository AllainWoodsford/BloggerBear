from __future__ import annotations

from unittest.mock import Mock, patch

from common.adapters.github_trending import GitHubTrendingAdapter


def _fixture_html(repos: list[tuple[str, str, int, str]]) -> str:
    """Build a minimal github.com/trending-shaped HTML fixture.

    Each repo tuple is (owner_slash_name, description, stars, language).
    Doesn't need to be a real full page -- just enough structure for the
    adapter's parser (article rows, an h2 anchor, a stargazers link, and an
    itemprop=programmingLanguage span).
    """
    rows = []
    for name, description, stars, language in repos:
        owner, repo = name.split("/")
        rows.append(f"""
        <article class="Box-row">
          <h2 class="h3 lh-condensed">
            <a href="/{owner}/{repo}">{owner} /\n {repo}</a>
          </h2>
          <p class="col-9 color-fg-muted my-1 pr-4">{description}</p>
          <span itemprop="programmingLanguage">{language}</span>
          <a href="/{owner}/{repo}/stargazers">{stars:,}</a>
        </article>
        """)
    return f"<html><body>{''.join(rows)}</body></html>"


def _mock_response(html: str) -> Mock:
    response = Mock()
    response.text = html
    response.raise_for_status = Mock()
    return response


def test_fetch_state_parses_repos_from_html():
    html = _fixture_html(
        [
            ("octocat/hello-world", "A friendly greeting repo", 1234, "Python"),
            ("acme/widgets", "Widgets for everyone", 42, "Go"),
        ]
    )
    adapter = GitHubTrendingAdapter()

    with patch("common.adapters.github_trending.requests.get", return_value=_mock_response(html)) as mock_get:
        state = adapter.fetch_state({"adapter_config": {}})

    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == "https://github.com/trending"
    assert "fetched_at" in state
    assert len(state["repos"]) == 2

    first = state["repos"][0]
    assert first["name"] == "octocat/hello-world"
    assert first["url"] == "https://github.com/octocat/hello-world"
    assert first["description"] == "A friendly greeting repo"
    assert first["stars"] == 1234
    assert first["language"] == "Python"


def test_fetch_state_uses_language_scoped_url_when_configured():
    html = _fixture_html([("acme/widgets", "desc", 1, "Go")])
    adapter = GitHubTrendingAdapter()

    with patch("common.adapters.github_trending.requests.get", return_value=_mock_response(html)) as mock_get:
        adapter.fetch_state({"adapter_config": {"language": "go"}})

    assert mock_get.call_args.args[0] == "https://github.com/trending/go"


def test_material_diff_true_on_first_observation():
    adapter = GitHubTrendingAdapter()
    new_state = {"repos": [{"name": "a/b", "url": "u", "stars": 1, "language": None, "description": ""}]}

    changed, summary = adapter.material_diff(None, new_state)

    assert changed is True
    assert "initial observation" in summary


def test_material_diff_false_on_pure_reorder():
    repo_a = {"name": "a/b", "url": "ua", "stars": 10, "language": None, "description": ""}
    repo_b = {"name": "c/d", "url": "uc", "stars": 20, "language": None, "description": ""}
    old_state = {"repos": [repo_a, repo_b]}
    new_state = {"repos": [repo_b, repo_a]}  # same set, different order

    adapter = GitHubTrendingAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is False
    assert summary == "no material change"


def test_material_diff_true_when_repo_set_changes():
    old_state = {"repos": [{"name": "a/b", "url": "ua", "stars": 10, "language": None, "description": ""}]}
    new_state = {"repos": [{"name": "e/f", "url": "ue", "stars": 5, "language": None, "description": ""}]}

    adapter = GitHubTrendingAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "entered: e/f" in summary
    assert "left: a/b" in summary


def test_material_diff_true_on_large_star_jump():
    old_state = {"repos": [{"name": "a/b", "url": "ua", "stars": 100, "language": None, "description": ""}]}
    new_state = {"repos": [{"name": "a/b", "url": "ua", "stars": 500, "language": None, "description": ""}]}

    adapter = GitHubTrendingAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is True
    assert "star jumps" in summary


def test_material_diff_false_on_small_star_wobble():
    old_state = {"repos": [{"name": "a/b", "url": "ua", "stars": 1000, "language": None, "description": ""}]}
    new_state = {"repos": [{"name": "a/b", "url": "ua", "stars": 1010, "language": None, "description": ""}]}

    adapter = GitHubTrendingAdapter()
    changed, summary = adapter.material_diff(old_state, new_state)

    assert changed is False


def test_source_refs_one_per_repo():
    new_state = {
        "fetched_at": "2026-09-13T00:00:00+00:00",
        "repos": [
            {"name": "a/b", "url": "https://github.com/a/b", "stars": 1, "language": None, "description": ""},
            {"name": "c/d", "url": "https://github.com/c/d", "stars": 2, "language": None, "description": ""},
        ],
    }

    adapter = GitHubTrendingAdapter()
    refs = adapter.source_refs(new_state)

    assert refs == [
        {"url": "https://github.com/a/b", "title": "a/b", "accessed_at": "2026-09-13T00:00:00+00:00"},
        {"url": "https://github.com/c/d", "title": "c/d", "accessed_at": "2026-09-13T00:00:00+00:00"},
    ]


# --- topic relevance filtering -----------------------------------------------------

MIXED_TRENDING = [
    ("acme/vuln-scanner", "Find known vulnerabilities in your dependencies", 900, "Go"),
    ("bob/pizza-oven-tracker", "Track your backyard pizza oven temperature", 800, "Python"),
    ("carol/cookbook", "A collection of recipes for cooking", 700, "Ruby"),
    ("dave/cookbook-sec", "Exploit development cookbook for red teams", 600, "C"),
]


def _fetch_trending(adapter_config):
    with patch(
        "common.adapters.github_trending.requests.get",
        return_value=_mock_response(_fixture_html(MIXED_TRENDING)),
    ):
        return GitHubTrendingAdapter().fetch_state({"adapter_config": adapter_config})


def test_keywords_filter_repos_by_name_and_description():
    state = _fetch_trending({"keywords": ["vulnerabilities", "exploit"]})

    assert [r["name"] for r in state["repos"]] == ["acme/vuln-scanner", "dave/cookbook-sec"]
    assert state["off_topic_dropped"] == 2


def test_without_keywords_all_repos_are_kept():
    state = _fetch_trending({})

    assert len(state["repos"]) == 4
    assert "off_topic_dropped" not in state


def test_no_relevant_repos_is_never_material():
    adapter = GitHubTrendingAdapter()
    empty = {"repos": [], "off_topic_dropped": 4}

    assert adapter.material_diff(None, empty) == (False, "no relevant repos to report")
