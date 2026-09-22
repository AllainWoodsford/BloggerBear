"""The "Equipment used" record on a static article page (common/static_pages.py).

A snapshot of the gear an article was written with, baked into the page once, at render time,
and never looked up again -- so it survives that gear later being deleted, repaired, worn out, or
having its rarity bumped. Never touches the Lineage code; it sits beside it in the same HTML.
"""

from __future__ import annotations

import boto3
import pytest
from moto import mock_aws

from common import static_pages

REGION = "ap-southeast-2"
ENV = {
    "CONTENT_BUCKET": "bloggerbear-content-test",
    "SITE_BUCKET": "bloggerbear-site-test",
    "PROMPT_REFINEMENTS_TABLE": "PromptRefinements",
    "TOPICS_TABLE": "Topics",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    static_pages._s3_client = None
    import common.dynamo as dynamo_module

    dynamo_module._dynamodb_resource = None


@pytest.fixture
def aws():
    with mock_aws():
        s3 = boto3.client("s3", region_name=REGION)
        s3.create_bucket(
            Bucket=ENV["CONTENT_BUCKET"], CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        s3.create_bucket(Bucket=ENV["SITE_BUCKET"], CreateBucketConfiguration={"LocationConstraint": REGION})
        dynamodb = boto3.client("dynamodb", region_name=REGION)
        dynamodb.create_table(
            TableName="PromptRefinements",
            KeySchema=[
                {"AttributeName": "topic_id", "KeyType": "HASH"},
                {"AttributeName": "version", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "topic_id", "AttributeType": "S"},
                {"AttributeName": "version", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        dynamodb.create_table(
            TableName="Topics",
            KeySchema=[{"AttributeName": "topic_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "topic_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield {
            "s3": s3,
            "refinements": boto3.resource("dynamodb", region_name=REGION).Table("PromptRefinements"),
            "topics": boto3.resource("dynamodb", region_name=REGION).Table("Topics"),
        }


def _put_gear(table, topic_id, version, **fields):
    item = {
        "topic_id": topic_id,
        "version": version,
        "status": "approved",
        "prompt_changes": "Be plain.",
        "theme": "Plain Speaking",
        "rarity": "epic",
        "durability": 20,
        "max_durability": 24,
        **fields,
    }
    table.put_item(Item=item)
    return item


def _render(aws, **overrides):
    kwargs = dict(
        article_id="a1",
        title="A Title",
        body_markdown="Body.",
        topic_name="GitHub Trending",
        published_at="2026-09-22T00:00:00+00:00",
        source_refs=[],
        view_count=0,
    )
    kwargs.update(overrides)
    static_pages.render_and_publish_article_page(**kwargs)
    return (
        aws["s3"].get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a1.html")["Body"].read().decode("utf-8")
    )


# --- nothing to show ------------------------------------------------------------------------


def test_no_widget_at_all_when_the_article_used_no_gear(aws):
    html = _render(aws, equipment_used=None)

    assert "equipment-footer" not in html and "Equipment used" not in html


def test_no_widget_for_an_empty_list_either(aws):
    assert "equipment-footer" not in _render(aws, equipment_used=[])


# --- the record itself -----------------------------------------------------------------------


def test_armor_used_is_shown_with_its_name_rarity_and_slot(aws):
    _put_gear(
        aws["refinements"],
        "global",
        "v1",
        slot="helmet",
        scope="global",
        theme="Front Loaded Facts",
        rarity="legendary",
    )

    html = _render(aws, equipment_used=[{"topic_id": "global", "version": "v1", "slot": "helmet"}])

    assert '<footer class="equipment-footer" aria-label="Equipment used">' in html
    assert "<h2>Equipment used</h2>" in html
    assert '<li class="equipment-item rarity-legendary">' in html
    assert "Helm of Front Loaded Facts" in html
    assert "Legendary" in html and "Helmet" in html and "Every topic" in html
    assert "Be plain." in html


def test_a_ring_names_its_topic(aws):
    aws["topics"].put_item(Item={"topic_id": "github-trending", "name": "GitHub Trending"})
    _put_gear(aws["refinements"], "github-trending", "v1", slot="ring", scope="topic", theme="Repo Focus")

    html = _render(
        aws,
        equipment_used=[{"topic_id": "github-trending", "version": "v1", "slot": "ring"}],
        topic_name="GitHub Trending",
    )

    assert "Ring of Repo Focus" in html
    widget = html.split("<h2>Equipment used</h2>")[1]
    assert "GitHub Trending" in widget  # in the widget itself, not just the page's own topic header
    assert "Every topic" not in widget


def test_several_pieces_all_appear(aws):
    _put_gear(aws["refinements"], "global", "v1", slot="helmet", scope="global", theme="Helm Theme")
    _put_gear(aws["refinements"], "global", "v2", slot="shield", scope="global", theme="Shield Theme")

    html = _render(
        aws,
        equipment_used=[
            {"topic_id": "global", "version": "v1", "slot": "helmet"},
            {"topic_id": "global", "version": "v2", "slot": "shield"},
        ],
    )

    assert html.count('class="equipment-item') == 2
    assert "Helm Theme" in html and "Shield Theme" in html


def test_a_duplicate_entry_is_shown_only_once(aws):
    _put_gear(aws["refinements"], "global", "v1", slot="helmet", scope="global")

    html = _render(
        aws,
        equipment_used=[
            {"topic_id": "global", "version": "v1", "slot": "helmet"},
            {"topic_id": "global", "version": "v1", "slot": "helmet"},
        ],
    )

    assert html.count('class="equipment-item') == 1


# --- it never depends on the gear existing after render time --------------------------------


def test_gear_deleted_after_the_page_was_rendered_still_shows_on_the_page(aws):
    _put_gear(aws["refinements"], "global", "v1", slot="helmet", scope="global", theme="Steadfast")

    html = _render(aws, equipment_used=[{"topic_id": "global", "version": "v1", "slot": "helmet"}])
    aws["refinements"].delete_item(Key={"topic_id": "global", "version": "v1"})  # deleted after the fact

    assert "Helm of Steadfast" in html  # the page already has it baked in; nothing re-fetches it


def test_gear_already_deleted_before_the_first_render_is_left_out_gracefully(aws):
    html = _render(aws, equipment_used=[{"topic_id": "global", "version": "gone"}])

    assert "equipment-footer" not in html  # nothing to show, and no error


def test_only_the_state_at_render_time_is_shown_a_later_repair_or_bump_never_appears(aws):
    _put_gear(
        aws["refinements"], "global", "v1", slot="helmet", scope="global", theme="Steadfast", rarity="common"
    )

    html = _render(aws, equipment_used=[{"topic_id": "global", "version": "v1", "slot": "helmet"}])
    aws["refinements"].update_item(
        Key={"topic_id": "global", "version": "v1"},
        UpdateExpression="SET rarity = :r",
        ExpressionAttributeValues={":r": "legendary"},
    )  # bumped after this page was rendered

    assert "rarity-common" in html and "rarity-legendary" not in html


# --- malformed input never breaks the page ----------------------------------------------------


def test_malformed_equipment_used_entries_are_skipped_not_fatal(aws):
    html = _render(
        aws,
        equipment_used=[None, {}, {"topic_id": "global"}, {"version": "v1"}, "not even a dict"],
    )

    assert "equipment-footer" not in html  # nothing usable in any entry


def test_untitled_gear_still_shows_with_a_plain_name(aws):
    aws["refinements"].put_item(
        Item={"topic_id": "global", "version": "v1", "status": "approved", "prompt_changes": "Be plain."}
    )

    html = _render(aws, equipment_used=[{"topic_id": "global", "version": "v1"}])

    assert "equipment-footer" in html and "Charm of Global Lore" in html  # no theme/slot: still named


# --- lineage is untouched, and layout sits the two footers together -------------------------


def test_the_lineage_footer_is_unaffected_and_both_sit_in_one_wrapper(aws):
    _put_gear(aws["refinements"], "global", "v1", slot="helmet", scope="global")

    html = _render(
        aws,
        equipment_used=[{"topic_id": "global", "version": "v1", "slot": "helmet"}],
        lineage={
            "models_used": [],
            "calls": [],
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "cost_aud": None,
            "cost_note": None,
        },
        published_by="ai_only",
    )

    assert '<div class="article-footers">' in html
    wrapper = html.split('<div class="article-footers">')[1].split("</main>")[0]
    lineage_index = wrapper.index("lineage-footer")
    equipment_index = wrapper.index("equipment-footer")
    assert lineage_index < equipment_index  # lineage first in the DOM, so it stacks above on narrow screens
    assert "<h2>Lineage</h2>" in wrapper  # untouched: still renders as before


def test_articles_with_no_equipment_still_get_the_wrapper_around_lineage_alone(aws):
    html = _render(aws)

    assert '<div class="article-footers">' in html and "lineage-footer" in html
    assert "equipment-footer" not in html
