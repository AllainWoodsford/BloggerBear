from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError

from common.adapters import github_trending
from common.adapters.github_trending import SEARCH_URL, GitHubTrendingAdapter, build_query


@pytest.fixture(autouse=True)
def _no_token(monkeypatch):
    """Every test starts keyless, on a fresh "cold start"."""
    monkeypatch.delenv("GITHUB_API_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_API_TOKEN_PARAMETER", raising=False)
    monkeypatch.setattr(github_trending, "_ssm_api_token", github_trending._UNREAD)


def _search_payload(repos: list[tuple[str, str | None, int, str | None]]) -> dict:
    """A minimal GitHub Search API (search/repositories) response body.

    Each repo tuple is (owner_slash_name, description, stars, language) -- only
    the fields the adapter reads.
    """
    return {
        "total_count": len(repos),
        "incomplete_results": False,
        "items": [
            {
                "full_name": name,
                "html_url": f"https://github.com/{name}",
                "description": description,
                "stargazers_count": stars,
                "language": language,
            }
            for name, description, stars, language in repos
        ],
    }


def _mock_response(payload: dict, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json = Mock(return_value=payload)
    response.raise_for_status = Mock()
    return response


def test_fetch_state_reads_repos_from_the_search_api():
    payload = _search_payload(
        [
            ("octocat/hello-world", "A friendly greeting repo", 1234, "Python"),
            ("acme/widgets", None, 42, None),
        ]
    )
    adapter = GitHubTrendingAdapter()

    with patch(
        "common.adapters.github_trending.requests.get", return_value=_mock_response(payload)
    ) as mock_get:
        state = adapter.fetch_state({"adapter_config": {}})

    mock_get.assert_called_once()
    assert mock_get.call_args.args[0] == SEARCH_URL == "https://api.github.com/search/repositories"
    params = mock_get.call_args.kwargs["params"]
    assert params["sort"] == "stars" and params["order"] == "desc" and params["per_page"] == 25
    assert params["q"].startswith("created:>=")
    headers = mock_get.call_args.kwargs["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert "BloggerBear" in headers["User-Agent"]
    assert "fetched_at" in state
    assert len(state["repos"]) == 2

    first, second = state["repos"]
    assert first == {
        "name": "octocat/hello-world",
        "url": "https://github.com/octocat/hello-world",
        "description": "A friendly greeting repo",
        "stars": 1234,
        "language": "Python",
    }
    # A null description is normalised so keyword matching never sees None.
    assert second["description"] == "" and second["language"] is None


def test_fetch_state_never_touches_the_trending_html_page():
    with patch(
        "common.adapters.github_trending.requests.get",
        return_value=_mock_response(_search_payload([])),
    ) as mock_get:
        GitHubTrendingAdapter().fetch_state({"adapter_config": {"language": "go"}})

    assert "github.com/trending" not in mock_get.call_args.args[0]


def test_fetch_state_scopes_the_query_by_language_when_configured():
    with patch(
        "common.adapters.github_trending.requests.get",
        return_value=_mock_response(_search_payload([])),
    ) as mock_get:
        GitHubTrendingAdapter().fetch_state({"adapter_config": {"language": "go"}})

    assert 'language:"go"' in mock_get.call_args.kwargs["params"]["q"]


def _headers_sent() -> list[dict]:
    with patch(
        "common.adapters.github_trending.requests.get",
        return_value=_mock_response(_search_payload([])),
    ) as mock_get:
        GitHubTrendingAdapter().fetch_state({"adapter_config": {}})
    return [call.kwargs["headers"] for call in mock_get.call_args_list]


def test_fetch_state_sends_a_token_only_when_one_is_configured(monkeypatch):
    assert "Authorization" not in _headers_sent()[0]

    monkeypatch.setenv("GITHUB_API_TOKEN", "ghp_example")
    assert _headers_sent()[0]["Authorization"] == "Bearer ghp_example"


def test_the_token_is_read_from_ssm_once_per_cold_start(monkeypatch):
    monkeypatch.setenv("GITHUB_API_TOKEN_PARAMETER", "/bloggerbear/dev/github-api-token")
    ssm = Mock()
    ssm.get_parameter.return_value = {"Parameter": {"Value": "ghp_from_ssm\n"}}

    with patch("common.adapters.github_trending.boto3.client", return_value=ssm):
        first, second = _headers_sent(), _headers_sent()

    assert first[0]["Authorization"] == second[0]["Authorization"] == "Bearer ghp_from_ssm"
    ssm.get_parameter.assert_called_once_with(Name="/bloggerbear/dev/github-api-token", WithDecryption=True)


def test_no_parameter_in_ssm_means_unauthenticated(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_API_TOKEN_PARAMETER", "/bloggerbear/dev/github-api-token")
    ssm = Mock()
    ssm.get_parameter.side_effect = ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": "nope"}}, "GetParameter"
    )

    with patch("common.adapters.github_trending.boto3.client", return_value=ssm):
        assert "Authorization" not in _headers_sent()[0]

    assert "no GitHub token at /bloggerbear/dev/github-api-token" in capsys.readouterr().out


def test_a_rejected_token_retries_the_search_without_it(monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_API_TOKEN", "ghp_revoked")
    rejected = _mock_response({}, status_code=401)
    ok = _mock_response(_search_payload([("a/b", "desc", 5, "Go")]))

    with patch("common.adapters.github_trending.requests.get", side_effect=[rejected, ok]) as mock_get:
        state = GitHubTrendingAdapter().fetch_state({"adapter_config": {}})

    assert [r["name"] for r in state["repos"]] == ["a/b"]
    first, second = (call.kwargs["headers"] for call in mock_get.call_args_list)
    assert "Authorization" in first and "Authorization" not in second
    assert "ghp_revoked" not in capsys.readouterr().out


def test_fetch_state_raises_when_the_api_refuses():
    response = _mock_response({})
    response.raise_for_status.side_effect = RuntimeError("403 rate limit exceeded")

    with patch("common.adapters.github_trending.requests.get", return_value=response):
        # Raising means the tick records nothing and retries on the next heartbeat.
        with pytest.raises(RuntimeError, match="rate limit"):
            GitHubTrendingAdapter().fetch_state({"adapter_config": {}})


NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def test_build_query_defaults_to_repos_created_in_the_last_week():
    assert build_query({}, NOW) == "created:>=2026-09-27"


def test_build_query_combines_window_language_and_star_floor():
    query = build_query(
        {"created_within_days": 3, "language": "Jupyter Notebook", "min_stars": 50}, NOW
    )

    assert query == 'created:>=2026-10-01 language:"Jupyter Notebook" stars:>=50'


def test_build_query_bounds_bad_numbers_instead_of_failing():
    assert build_query({"created_within_days": "lots", "min_stars": -5}, NOW) == "created:>=2026-09-27"
    assert build_query({"created_within_days": 999}, NOW) == "created:>=2026-09-04"
    assert build_query({"created_within_days": 0}, NOW) == "created:>=2026-10-03"


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
    assert summary == "no new information"


def test_material_diff_a_repo_already_reported_is_not_new_when_it_returns():
    repo_a = {"name": "a/b", "url": "ua", "stars": 10, "language": None, "description": ""}
    repo_b = {"name": "c/d", "url": "uc", "stars": 20, "language": None, "description": ""}
    old_state = {"repos": [repo_a], "_seen": {"a/b": "2026-09-20", "c/d": "2026-09-20"}}
    new_state = {"repos": [repo_a, repo_b]}  # c/d left the list earlier and is back

    assert GitHubTrendingAdapter().material_diff(old_state, new_state) == (False, "no new information")


def test_material_diff_a_repo_leaving_the_list_is_not_news_but_a_new_one_is():
    repo_a = {"name": "a/b", "url": "ua", "stars": 10, "language": None, "description": ""}
    repo_b = {"name": "c/d", "url": "uc", "stars": 20, "language": None, "description": ""}
    repo_c = {"name": "e/f", "url": "ue", "stars": 5, "language": None, "description": ""}
    adapter = GitHubTrendingAdapter()

    assert adapter.material_diff({"repos": [repo_a, repo_b]}, {"repos": [repo_a]})[0] is False
    changed, summary = adapter.material_diff({"repos": [repo_a, repo_b]}, {"repos": [repo_a, repo_c]})
    assert changed is True and "entered: e/f" in summary and "left: c/d" in summary


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
        return_value=_mock_response(_search_payload(MIXED_TRENDING)),
    ) as mock_get:
        state = GitHubTrendingAdapter().fetch_state({"adapter_config": adapter_config})
    state["_per_page"] = mock_get.call_args.kwargs["params"]["per_page"]
    return state


def test_keywords_filter_repos_by_name_and_description():
    state = _fetch_trending({"keywords": ["vulnerabilities", "exploit"]})

    assert [r["name"] for r in state["repos"]] == ["acme/vuln-scanner", "dave/cookbook-sec"]
    assert state["off_topic_dropped"] == 2
    # Filtering by topic widens the candidate pool, still in one request.
    assert state["_per_page"] == 100


def test_without_keywords_all_repos_are_kept():
    state = _fetch_trending({})

    assert len(state["repos"]) == 4
    assert "off_topic_dropped" not in state
    assert state["_per_page"] == 25


def test_no_relevant_repos_is_never_material():
    adapter = GitHubTrendingAdapter()
    empty = {"repos": [], "off_topic_dropped": 4}

    assert adapter.material_diff(None, empty) == (False, "no relevant repos to report")
