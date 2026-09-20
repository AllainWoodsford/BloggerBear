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
