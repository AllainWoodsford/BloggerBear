"""Static article page rendering (docs/project-plan.md §11, "Static article
publishing").

Renders a published Articles item as a standalone static HTML page and
writes it into the site bucket -- the same CloudFront-fronted bucket
(`SITE_BUCKET`, private + OAC-only, see infra/modules/static-site) that
already serves `frontend/`'s SPA files -- under `articles/{article_id}.html`.
Once written, reading that article is a direct S3/CloudFront hit with no
Lambda or API Gateway round-trip. Deliberately NOT written into
`CONTENT_BUCKET`: that bucket is explicitly never served publicly (see
infra/environments/*/main.tf's aws_s3_bucket.content comment) -- it stays
Lambda-only storage for the raw markdown source.

Three different code paths can cause an article to become (or re-become)
published -- daily_cycle_handler's own compliant-draft branch, the
moderation-approve route, and the force-publish route (both in
admin_api_handler.py) -- and per the risk called out in the project plan,
all three call `render_and_publish_article_page` below rather than
duplicating the rendering/upload logic, so the static page can never drift
out of sync with whichever path actually published the article.
"""

from __future__ import annotations

import os
from html import escape

import boto3
import markdown

from .source_refs import dedupe_source_refs

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def read_article_body(body_s3_key: str) -> str:
    """Read an article's raw markdown body from the content bucket.

    Small, separately-named helper (rather than inlining this at each call
    site) so admin_api_handler's two publish paths -- which don't already
    have the draft text in memory the way daily_cycle_handler does right
    after drafting it -- can fetch it, and so tests can mock this one call
    without needing a real S3 bucket/object.
    """
    response = _get_s3_client().get_object(
        Bucket=os.environ["CONTENT_BUCKET"], Key=body_s3_key
    )
    return response["Body"].read().decode("utf-8")


def render_and_publish_article_page(
    *,
    article_id: str,
    title: str,
    body_markdown: str,
    topic_name: str,
    published_at: str | None,
    source_refs: list[dict] | None = None,
    view_count: int = 0,
) -> str:
    """Render `article_id` as a static HTML page and upload it to the site
    bucket. Returns the S3 key it was written to.

    The body is converted from markdown to HTML server-side and trusted
    as-is -- it's Bedrock-authored content that has already passed
    common.compliance.review_draft, the same trust boundary the rest of
    this pipeline already applies to it (e.g. source_refs URLs are linked
    unvalidated elsewhere in this project's public API/frontend). The
    CloudFront distribution's response headers policy sets a strict CSP
    (script-src 'self', no unsafe-inline -- see
    infra/modules/static-site/main.tf) so even an unexpected inline
    <script> surviving into the body could never execute; that CSP is the
    actual backstop here, not output sanitization.
    """
    body_html = markdown.markdown(body_markdown)
    source_refs = dedupe_source_refs(source_refs)

    source_refs_html = ""
    if source_refs:
        items_html = "".join(
            "<li>"
            f'<a href="{escape(ref.get("url", ""))}" '
            f'aria-label="{escape(ref.get("title") or ref.get("url", ""))} (external source link)">'
            f"{escape(ref.get('title') or ref.get('url', ''))}</a>"
            "</li>"
            for ref in source_refs
        )
        source_refs_html = (
            '<footer class="sources-footer" aria-label="Sources">'
            "<h2>Sources</h2>"
            f"<ul>{items_html}</ul>"
            "</footer>"
        )

    published_label = escape(published_at) if published_at else "unpublished"

    page_html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>{escape(title)} -- BloggerBear</title>
<link rel="stylesheet" href="/normalize.css" />
<link rel="stylesheet" href="/styles.css" />
<link rel="icon" href="/logo.svg" type="image/svg+xml" />
</head>
<body>
<header id="top">
<a class="site-title" href="/">BloggerBear</a>
</header>
<main id="content" data-article-id="{escape(article_id)}">
<h1>{escape(title)}</h1>
<p class="article-meta">
<span>Published {published_label}</span>
&#183;
<span class="view-count" data-role="view-count">{view_count} views</span>
&#183;
<span data-role="topic-name">{escape(topic_name or "")}</span>
</p>
<div class="article-body">{body_html}</div>
{source_refs_html}
<section class="feedback" data-role="feedback">
<h2>Feedback</h2>
<div class="feedback-buttons">
<button type="button" data-role="upvote" aria-label="Upvote this article">Upvote</button>
<button type="button" data-role="downvote" aria-label="Downvote this article">Downvote</button>
</div>
<p class="feedback-status" data-role="feedback-status" aria-live="polite" aria-atomic="true"></p>
</section>
</main>
<script src="/config.js"></script>
<script src="/article-widgets.js" defer></script>
</body>
</html>
"""

    key = f"articles/{article_id}.html"
    _get_s3_client().put_object(
        Bucket=os.environ["SITE_BUCKET"],
        Key=key,
        Body=page_html.encode("utf-8"),
        ContentType="text/html",
    )
    # No CloudFront invalidation on republish -- a known, accepted gap.
    # Overwriting the S3 object is enough for a first-ever publish (the
    # common case); a rare republish of the same article_id (e.g.
    # force-publish correcting a bad moderation call) can keep serving a
    # cached copy until CloudFront's TTL naturally expires. Not worth the
    # extra IAM permission + API call for how infrequently that happens on
    # this single-operator project.
    return key
