"""The admin API's side of Professor Grizzlewick (admin_api_handler.reviewer_*, the topic flag,
`articles checked`), how his approvals are labelled for readers, his musings, and the assistant's
finding for his flags."""

from __future__ import annotations

import json
from unittest.mock import patch

import boto3
import pytest
from test_admin_api_handler import (  # noqa: F401 - the fixtures, used by name
    REGION,
    _event,
    _put_article,
    _put_moderation_item,
    _put_topic,
    aws_env,
    aws_resources,
)

import admin_api_handler
from common import fact_check, musings, reviewer, static_pages

# Every test here runs against the admin API tests' moto tables.
pytestmark = pytest.mark.usefixtures("aws_resources")


def _article(article_id="article-1"):
    return boto3.resource("dynamodb", region_name=REGION).Table("Articles").get_item(
        Key={"article_id": article_id}
    )["Item"]


def _queue_items():
    return boto3.resource("dynamodb", region_name=REGION).Table("ModerationQueue").scan()["Items"]


# --- his approvals are his, never a person's ---------------------------------------------------------


def test_his_approval_publishes_as_the_ai_reviewer_and_is_logged_as_his():
    _put_article()
    _put_moderation_item()
    _put_topic()
    decisions = []
    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page") as render,
        patch("admin_api_handler.generate_and_store_article_musing"),
        patch("admin_api_handler.evidence.emit", lambda name, **kw: decisions.append(kw)),
    ):
        status, body = admin_api_handler.reviewer_approve("queue-1")

    assert (status, body) == (200, {"approved": "queue-1", "article_id": "article-1"})
    assert _article()["published_by"] == "reviewer"
    assert render.call_args.kwargs["published_by"] == "reviewer"
    assert decisions[-1]["by"] == "reviewer" and decisions[-1]["decision"] == "approved"


def test_a_persons_approval_is_still_a_persons():
    _put_article()
    _put_moderation_item()
    _put_topic()
    decisions = []
    with (
        patch("admin_api_handler.read_article_body", return_value="# Body"),
        patch("admin_api_handler.render_and_publish_article_page"),
        patch("admin_api_handler.generate_and_store_article_musing"),
        patch("admin_api_handler.evidence.emit", lambda name, **kw: decisions.append(kw)),
    ):
        event = _event("POST /moderation-queue/{queue_id}/approve", path_params={"queue_id": "queue-1"})
        assert admin_api_handler.handler(event, None)["statusCode"] == 200
    assert _article()["published_by"] == "humans"
    assert decisions[-1]["by"] == "human"


def test_his_reject_is_logged_as_his():
    _put_article()
    _put_moderation_item()
    _put_topic()
    decisions = []
    with (
        patch("admin_api_handler.generate_and_store_rejection_musing"),
        patch("admin_api_handler.evidence.emit", lambda name, **kw: decisions.append(kw)),
    ):
        status, _ = admin_api_handler.reviewer_reject("queue-1")
    assert status == 200
    assert _article()["status"] == "rejected"
    assert decisions[-1]["by"] == "reviewer"


def test_the_page_says_an_ai_reviewer_approved_it_never_a_person():
    held = {"mode": "enforce", "status": "reviewed", "held": True}
    assert fact_check.fact_check_label(held, "reviewer") == fact_check.CHECKED_BY_REVIEWER
    assert "person" not in fact_check.CHECKED_BY_REVIEWER
    assert fact_check.fact_check_label(held, "humans") == fact_check.CHECKED_BY_PERSON
    unavailable = {"mode": "enforce", "status": "unavailable"}
    assert fact_check.fact_check_label(unavailable, "reviewer") == fact_check.NOT_CHECKED
    assert static_pages._published_by_label("reviewer") == "AI reviewer"


# --- holding a downvoted article -----------------------------------------------------------------


def test_he_takes_a_published_article_down_and_puts_it_back_in_the_inbox():
    _put_article(status="published")
    with (
        patch("admin_api_handler.remove_article_page") as removed,
        patch("admin_api_handler._clear_article_traces", return_value={"musings_removed": 1}),
        patch("admin_api_handler.refresh_articles_snapshot"),
    ):
        status, body = admin_api_handler.reviewer_hold("article-1")

    assert status == 200 and body["held"] == "article-1" and body["musings_removed"] == 1
    removed.assert_called_once()
    assert _article()["status"] == "pending_moderation"
    [item] = [i for i in _queue_items() if i["queue_id"] == body["queue_id"]]
    assert item["status"] == "pending"
    assert item["reasons"] == [admin_api_handler.HELD_BY_REVIEWER_REASON]


def test_he_never_holds_what_is_not_published():
    _put_article(status="pending_moderation")
    assert admin_api_handler.reviewer_hold("article-1")[0] == 409
    assert admin_api_handler.reviewer_hold("nope")[0] == 404


# --- the topic flag and `articles checked` -------------------------------------------------------


def test_a_topic_takes_a_reviewer_flag_and_null_puts_it_back_to_the_project_default():
    base = {
        "topic_id": "flagged",
        "name": "Flagged",
        "adapter": "github_trending",
        "adapter_config": {},
        "is_financial": False,
    }
    with patch("admin_api_handler._apply_topic_schedules"):
        bad = admin_api_handler.handler(
            _event("POST /topics", body={**base, "reviewer_may_approve": "yes"}), None
        )
        assert bad["statusCode"] == 400
        made = admin_api_handler.handler(
            _event("POST /topics", body={**base, "reviewer_may_approve": False}), None
        )
        assert made["statusCode"] in (200, 201), made["body"]
        topics = boto3.resource("dynamodb", region_name=REGION).Table("Topics")
        assert topics.get_item(Key={"topic_id": "flagged"})["Item"]["reviewer_may_approve"] is False

        update = _event(
            "PUT /topics/{topic_id}", path_params={"topic_id": "flagged"}, body={"reviewer_may_approve": None}
        )
        cleared = admin_api_handler.handler(update, None)
        assert cleared["statusCode"] == 200, cleared["body"]
        assert "reviewer_may_approve" not in topics.get_item(Key={"topic_id": "flagged"})["Item"]


def test_articles_checked_clears_his_flag():
    _put_article(status="published")
    event = _event("POST /articles/{article_id}/checked", path_params={"article_id": "article-1"})
    assert admin_api_handler.handler(event, None)["statusCode"] == 409  # nothing to clear
    admin_api_handler.set_article_fields(
        "article-1", {reviewer.FLAG_FIELD: reviewer.FLAG_FINANCIAL, reviewer.FLAGGED_AT_FIELD: "x"}
    )
    result = admin_api_handler.handler(event, None)
    assert (result["statusCode"], json.loads(result["body"])) == (200, {"checked": "article-1"})
    assert reviewer.FLAG_FIELD not in _article() and reviewer.FLAGGED_AT_FIELD not in _article()


# --- his musings ---------------------------------------------------------------------------------


@pytest.fixture
def stored(monkeypatch):
    kept = []
    monkeypatch.setattr(musings, "put_musing", lambda **kw: kept.append(kw) or kw)
    return kept


def test_his_rejection_names_the_topic_with_a_bear_joke_and_falls_back_when_the_model_will_not(
    stored, monkeypatch
):
    monkeypatch.setattr(musings, "tracked_claude", lambda *a, **k: "Bah, nothing to see here.")
    musings.generate_and_store_reviewer_rejection(
        topic_id="wow", topic_name="Wow Forever", model_id="m", created_at="2026-10-20T00:00:00+00:00"
    )
    [musing] = stored
    assert musing["character"] == "grizzlewick" and musing["kind"] == "reviewer_rejection"
    assert musing["mood"] == "stern" and musing["created_at"] == "2026-10-20T00:00:00+00:00"
    assert "Wow Forever" in musing["text"] and "pub test" in musing["text"]  # the fallback
    assert any(joke in musing["text"] for joke in musings._BEAR_JOKES)


def test_his_report_opens_with_being_woken_and_takes_the_models_words_when_usable(stored, monkeypatch):
    seen = []
    monkeypatch.setattr(
        musings,
        "tracked_claude",
        lambda category, prompt, model_id, max_tokens: seen.append(prompt)
        or "I've been woken up from slumber, the admins are falling behind! Approved 2, sent 1 back.",
    )
    musings.generate_and_store_reviewer_report(facts=["approved 2 held articles"], woken=True, model_id="m")
    assert "- approved 2 held articles" in seen[0] and "word for word" in seen[0]
    assert stored[0]["text"].startswith("I've been woken up from slumber")
    assert stored[0]["mood"] == "roused" and stored[0]["character"] == "grizzlewick"


def test_his_shocked_hold_falls_back_when_the_model_fails(stored, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("unavailable")

    monkeypatch.setattr(musings, "tracked_claude", boom)
    musings.generate_and_store_reviewer_hold(topic_id="crypto", topic_name="Crypto", model_id="m")
    assert stored[0]["mood"] == "shocked" and "Crypto" in stored[0]["text"]


# --- the assistant reports his flags ---------------------------------------------------------------


def test_the_assistant_reports_each_flagged_published_article_with_a_command():
    from ops_mcp import suggestions, tools

    _put_topic()
    _put_article(status="published")
    admin_api_handler.set_article_fields("article-1", {reviewer.FLAG_FIELD: reviewer.FLAG_FINANCIAL})
    [found] = tools.reviewer_flag_findings()
    assert found["kind"] == "reviewer_financial_check" and found["id"] == "article-1"
    assert found["suggestion"]["command"].endswith("articles get article-1")
    assert "reviewer_financial_check" in suggestions.CATALOGUE

    admin_api_handler.set_article_fields("article-1", {reviewer.FLAG_FIELD: None})
    assert tools.reviewer_flag_findings() == []


# --- his settings, through the admin API -------------------------------------------------------------


def test_his_settings_are_read_and_changed_with_no_deploy():
    read = admin_api_handler.handler(_event("GET /reviewer-config"), None)
    body = json.loads(read["body"])
    assert body["effective"] == reviewer.SETTINGS and body["run"] is None

    bad = admin_api_handler.handler(_event("PUT /reviewer-config", body={"max_rewrites": 0}), None)
    assert bad["statusCode"] == 400
    changed = admin_api_handler.handler(
        _event("PUT /reviewer-config", body={"may_approve": False, "defang_after_days": 14}), None
    )
    effective = json.loads(changed["body"])["effective"]
    assert effective["may_approve"] is False and effective["defang_after_days"] == 14
    cleared = admin_api_handler.handler(_event("PUT /reviewer-config", body={"may_approve": None}), None)
    assert json.loads(cleared["body"])["effective"]["may_approve"] is True


def test_the_cli_sends_each_reviewer_setting_as_the_api_takes_it():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import admin_cli

    sent = []
    argv = ["reviewer", "set", "--may-approve", "off", "--defang-held", "", "--max-rewrites", "4"]
    args = admin_cli.build_parser().parse_args([*argv, "--tick-minutes", ""])

    def record(_args, method, path, body=None):
        sent.append((method, path, body))

    with patch.object(admin_cli, "_do_request", record):
        args.func(args)
    expected = {"may_approve": False, "defang_held": None, "max_rewrites": 4, "tick_minutes": None}
    assert sent == [("PUT", "/reviewer-config", expected)]


def test_his_hold_marks_the_article_for_good_and_writes_the_inbox_item_first():
    _put_article(status="published")
    order = []
    real_put = admin_api_handler.put_moderation_item
    real_status = admin_api_handler.update_article_status

    def put(**kwargs):
        order.append("item")
        return real_put(**kwargs)

    def status(*args, **kwargs):
        order.append("status")
        return real_status(*args, **kwargs)

    with (
        patch("admin_api_handler.put_moderation_item", put),
        patch("admin_api_handler.update_article_status", status),
        patch("admin_api_handler.remove_article_page", lambda *a, **k: order.append("page")),
        patch("admin_api_handler._clear_article_traces", return_value={}),
        patch("admin_api_handler.refresh_articles_snapshot"),
    ):
        assert admin_api_handler.reviewer_hold("article-1")[0] == 200
    assert order == ["item", "status", "page"]
    assert _article()[reviewer.HELD_FIELD]


def test_a_hold_that_cannot_take_the_article_down_voids_its_item_and_leaves_it_up():
    _put_article(status="published")

    def broken(*args, **kwargs):
        raise RuntimeError("throttled")

    with (
        patch("admin_api_handler.update_article_status", broken),
        patch("admin_api_handler.remove_article_page") as removed,
        pytest.raises(RuntimeError),
    ):
        admin_api_handler.reviewer_hold("article-1")
    removed.assert_not_called()
    assert _article()["status"] == "published"
    assert all(item["status"] != "pending" for item in _queue_items())
