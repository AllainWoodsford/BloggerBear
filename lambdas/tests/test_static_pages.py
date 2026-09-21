import boto3
import pytest
from moto import mock_aws

from common import static_pages

ENV = {
    "CONTENT_BUCKET": "bloggerbear-content-test",
    "SITE_BUCKET": "bloggerbear-site-test",
}


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    static_pages._s3_client = None


@pytest.fixture
def s3(monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="ap-southeast-2")
        client.create_bucket(
            Bucket=ENV["CONTENT_BUCKET"],
            CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
        )
        client.create_bucket(
            Bucket=ENV["SITE_BUCKET"],
            CreateBucketConfiguration={"LocationConstraint": "ap-southeast-2"},
        )
        yield client


def test_read_article_body_returns_decoded_text(s3):
    s3.put_object(Bucket=ENV["CONTENT_BUCKET"], Key="articles/a1.md", Body=b"# Hello\n\nWorld.")
    assert static_pages.read_article_body("articles/a1.md") == "# Hello\n\nWorld."


def test_render_and_publish_article_page_writes_expected_key(s3):
    key = static_pages.render_and_publish_article_page(
        article_id="a1",
        title="A Title",
        body_markdown="# Hello\n\nSome **bold** content.",
        topic_name="GitHub Trending",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[{"url": "https://example.com/x", "title": "example/x"}],
        view_count=3,
    )

    assert key == "articles/a1.html"
    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a1.html")
    html = stored["Body"].read().decode("utf-8")

    assert stored["ContentType"] == "text/html"
    assert "<title>A Title -- BloggerBear</title>" in html
    assert "<h1>A Title</h1>" in html
    assert "<strong>bold</strong>" in html
    assert 'data-article-id="a1"' in html
    assert "3 views" in html
    assert "GitHub Trending" in html
    assert 'href="https://example.com/x"' in html
    assert '<script src="/article-widgets.js" defer></script>' in html


def test_render_and_publish_article_page_omits_sources_footer_when_none(s3):
    static_pages.render_and_publish_article_page(
        article_id="a2",
        title="No Sources",
        body_markdown="Body text.",
        topic_name="Hacker News",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[],
        view_count=0,
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a2.html")
    html = stored["Body"].read().decode("utf-8")
    assert "sources-footer" not in html



def test_render_and_publish_article_page_lineage_footer_no_data(s3):
    """An article with no lineage/published_by (published before this
    feature existed) must render an explicit "No data" in both the
    compact summary line and the full footer -- not a blank gap."""
    static_pages.render_and_publish_article_page(
        article_id="a4",
        title="Old Article",
        body_markdown="Body text.",
        topic_name="GitHub Trending",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[],
        view_count=0,
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a4.html")
    html = stored["Body"].read().decode("utf-8")
    assert '<footer class="lineage-footer"' in html
    assert "<h2>Lineage</h2>" in html
    # No data at all -- every field says so explicitly, not blank.
    assert html.count("No data") >= 4
    assert '<p class="lineage-summary">No data</p>' in html


def test_render_and_publish_article_page_lineage_footer_with_data(s3):
    lineage = {
        "calls": [
            {
                "stage": "ideation",
                "model_id": "model-a",
                "input_tokens": 100,
                "output_tokens": 20,
                "used_fallback": False,
            },
            {
                "stage": "draft",
                "model_id": "model-a",
                "input_tokens": 400,
                "output_tokens": 300,
                "used_fallback": False,
            },
            {
                "stage": "title",
                "model_id": "model-b",
                "input_tokens": 50,
                "output_tokens": 5,
                "used_fallback": True,
            },
        ],
        "total_input_tokens": 550,
        "total_output_tokens": 325,
        "models_used": ["model-a", "model-b"],
        "cost_aud": 0.42,
        "cost_note": None,
    }
    static_pages.render_and_publish_article_page(
        article_id="a5",
        title="New Article",
        body_markdown="Body text.",
        topic_name="GitHub Trending",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[],
        view_count=0,
        lineage=lineage,
        published_by="ai_only",
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a5.html")
    html = stored["Body"].read().decode("utf-8")
    assert "model-a, model-b" in html
    # Per-model token breakdown, summed across every call using that model
    # (model-a appears in two calls: 100+400 in, 20+300 out).
    assert "model-a: 500 in / 320 out" in html
    assert "model-b: 50 in / 5 out" in html
    # The compact one-line summary near the date carries tokens too, in the
    # user's requested "models [], tokens [by model], approved by, cost" shape.
    assert (
        '<p class="lineage-summary">models [model-a, model-b] · '
        "tokens [model-a: 500 in / 320 out, model-b: 50 in / 5 out] · "
        "approved by AI only · ~$0.42 AUD</p>"
    ) in html
    assert "~$0.42 AUD" in html
    assert "AI only" in html
    assert "No data" not in html


def test_render_and_publish_article_page_lineage_unpriced_shows_cost_note(s3):
    lineage = {
        "calls": [
            {
                "stage": "draft",
                "model_id": "model-c",
                "input_tokens": 10,
                "output_tokens": 5,
                "used_fallback": False,
            }
        ],
        "total_input_tokens": 10,
        "total_output_tokens": 5,
        "models_used": ["model-c"],
        "cost_aud": None,
        "cost_note": "pricing not available for model-c",
    }
    static_pages.render_and_publish_article_page(
        article_id="a6",
        title="Unpriced Model Article",
        body_markdown="Body text.",
        topic_name="GitHub Trending",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[],
        view_count=0,
        lineage=lineage,
        published_by="humans",
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a6.html")
    html = stored["Body"].read().decode("utf-8")
    assert "pricing not available for model-c" in html
    assert "Humans" in html

def test_render_and_publish_article_page_dedupes_duplicate_sources(s3):
    static_pages.render_and_publish_article_page(
        article_id="a2b",
        title="Duplicate Sources",
        body_markdown="Body text.",
        topic_name="Hacker News",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=[
            {"url": "https://example.com/x", "title": "example/x"},
            {"url": "https://example.com/x", "title": "example/x"},
        ],
        view_count=0,
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a2b.html")
    html = stored["Body"].read().decode("utf-8")
    assert html.count('href="https://example.com/x"') == 1


def test_render_and_publish_article_page_escapes_title_and_body(s3):
    static_pages.render_and_publish_article_page(
        article_id="a3",
        title="<script>alert(1)</script>",
        body_markdown="Just text.",
        topic_name="GitHub Trending",
        published_at="2026-09-20T00:00:00+00:00",
        source_refs=None,
        view_count=0,
    )

    stored = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/a3.html")
    html = stored["Body"].read().decode("utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --- taking a page down ---------------------------------------------------------


def _publish(article_id):
    static_pages.render_and_publish_article_page(
        article_id=article_id,
        title="A Title",
        body_markdown="Body",
        topic_name="Topic",
        published_at="2026-09-20T00:00:00+00:00",
    )


def test_article_page_key_is_the_one_publishing_writes(s3):
    _publish("a9")

    assert static_pages.article_page_key("a9") == "articles/a9.html"
    s3.head_object(Bucket=ENV["SITE_BUCKET"], Key=static_pages.article_page_key("a9"))


def test_remove_article_page_deletes_only_that_page(s3):
    _publish("a1")
    _publish("a2")

    key = static_pages.remove_article_page("a1")

    assert key == "articles/a1.html"
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket=ENV["SITE_BUCKET"])["Contents"]]
    assert keys == ["articles/a2.html"]


def test_remove_article_page_is_safe_to_repeat(s3):
    _publish("a1")

    static_pages.remove_article_page("a1")
    static_pages.remove_article_page("a1")  # already gone: not an error


def test_invalidate_article_page_asks_cloudfront_to_drop_that_path(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    monkeypatch.setattr(static_pages, "_get_cloudfront_client", lambda: client)
    monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")

    assert static_pages.invalidate_article_page("a1") is True

    kwargs = client.create_invalidation.call_args.kwargs
    assert kwargs["DistributionId"] == "E123"
    assert kwargs["InvalidationBatch"]["Paths"] == {"Quantity": 1, "Items": ["/articles/a1.html"]}
    assert kwargs["InvalidationBatch"]["CallerReference"].startswith("unpublish-a1-")


def test_invalidate_article_page_uses_a_fresh_caller_reference_each_time(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    monkeypatch.setattr(static_pages, "_get_cloudfront_client", lambda: client)
    monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")

    static_pages.invalidate_article_page("a1")
    static_pages.invalidate_article_page("a1")

    calls = client.create_invalidation.call_args_list
    refs = [c.kwargs["InvalidationBatch"]["CallerReference"] for c in calls]
    assert refs[0] != refs[1]  # a repeated reference is rejected if its path set differs


def test_invalidate_article_page_without_a_distribution_does_nothing(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    monkeypatch.setattr(static_pages, "_get_cloudfront_client", lambda: client)
    monkeypatch.delenv("CLOUDFRONT_DISTRIBUTION_ID", raising=False)

    assert static_pages.invalidate_article_page("a1") is False
    client.create_invalidation.assert_not_called()


def test_invalidate_article_page_never_raises(monkeypatch):
    from unittest.mock import MagicMock

    client = MagicMock()
    client.create_invalidation.side_effect = RuntimeError("AccessDenied")
    monkeypatch.setattr(static_pages, "_get_cloudfront_client", lambda: client)
    monkeypatch.setenv("CLOUDFRONT_DISTRIBUTION_ID", "E123")

    assert static_pages.invalidate_article_page("a1") is False


# --- article body headings, and the site navigation ---------------------------------


def test_body_headings_sit_below_the_page_h1():
    html = static_pages.render_body_html("# Top\n\n## Next\n\n### Deeper\n\nText.")
    assert "<h1" not in html
    assert ">Top</h2>" in html
    assert ">Next</h3>" in html
    assert ">Deeper</h4>" in html


def test_body_headings_stop_at_h6():
    assert ">Deep</h6>" in static_pages.render_body_html("##### Deep")
    assert ">Deeper</h6>" in static_pages.render_body_html("###### Deeper")


def test_body_tables_and_lists_render_as_html():
    html = static_pages.render_body_html(
        "| Coin | Price |\n| --- | ---: |\n| BTC | 1 |\n\n- one\n- two\n\n1. first\n2. second"
    )
    assert "<table>" in html and "<th>Coin</th>" in html and "<td>BTC</td>" in html
    assert "<ul>" in html and "<ol>" in html


def test_rendered_page_has_one_h1_and_real_body_headings(s3):
    static_pages.render_and_publish_article_page(
        article_id="h1",
        title="The Title",
        body_markdown="# A body title\n\n## A section\n\nText.",
        topic_name="Topic",
        published_at="2026-09-20T00:00:00+00:00",
    )
    html = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/h1.html")["Body"].read().decode()
    assert html.count("<h1") == 1
    assert ">A body title</h2>" in html
    assert ">A section</h3>" in html
    assert "## A section" not in html


def test_rendered_page_links_the_site_sections_in_header_and_footer(s3):
    static_pages.render_and_publish_article_page(
        article_id="nav1",
        title="T",
        body_markdown="Body.",
        topic_name="Topic",
        published_at="2026-09-20T00:00:00+00:00",
    )
    html = s3.get_object(Bucket=ENV["SITE_BUCKET"], Key="articles/nav1.html")["Body"].read().decode()
    header = html.split("<header", 1)[1].split("</header>", 1)[0]
    footer = html.split('<footer class="site-footer">', 1)[1].split("</footer>", 1)[0]
    for region in (header, footer):
        assert 'href="/#/topic/digest">Trending Everywhere</a>' in region
        assert 'href="/#/musings">Musings</a>' in region
        assert 'href="/#/stats">Stats</a>' in region
    assert 'aria-label="Site sections"' in header
    assert 'aria-label="Explore"' in footer and 'aria-label="Legal"' in footer
    assert 'href="/#/privacy"' in footer and 'href="/about.html"' in footer
    assert 'class="skip-link"' in html
