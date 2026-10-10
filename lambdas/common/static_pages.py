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

**Equipment used.** When an article was written with gear equipped (see common/equipment.py,
Articles.equipment_used), a small "Equipment used" record sits beside the Lineage footer. It is a
snapshot of each piece (name, rarity, slot, what it says) taken *once*, the moment this page is
rendered, and baked into the static HTML -- deliberately not looked up again after that, so it
stays exactly as it was even if the gear is later deleted, repaired, worn out, or bumped in rarity.
No script or API call is involved; it is as static as the Lineage footer next to it.
"""

from __future__ import annotations

import os
import time
from html import escape

import boto3
import markdown

from .attribution import clean_sources
from .dynamo import get_prompt_refinement, get_topic
from .figures import PUBLIC_FIGURE_PREFIX, clean_figures, figure_key
from .gear import public_view as _gear_public_view
from .source_refs import dedupe_source_refs

_s3_client = None
_cloudfront_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3")
    return _s3_client


def _get_cloudfront_client():
    global _cloudfront_client
    if _cloudfront_client is None:
        _cloudfront_client = boto3.client("cloudfront")
    return _cloudfront_client


def article_page_key(article_id: str) -> str:
    """The site-bucket key an article's static page lives at."""
    return f"articles/{article_id}.html"


def remove_article_figures(article_id: str, count: int) -> None:
    """Delete the copies of an article's figures from the site bucket: the keys for positions
    1..`count` (common/figures.py's figure_key), `count` being how many the article stores. The
    keys are counted rather than listed because the Lambda role may put and delete under
    `articles/` but never list the bucket. Best effort per key, and it never raises: a copy that
    is not there is not an error (S3 answers 204), and one that cannot be deleted is said and
    skipped so the page itself still comes down. Safe to repeat."""
    s3 = _get_s3_client()
    for index in range(1, int(count or 0) + 1):
        key = figure_key(article_id, index)
        try:
            s3.delete_object(Bucket=os.environ["SITE_BUCKET"], Key=key)
        except Exception as exc:  # noqa: BLE001 - best effort, see docstring
            print(f"static_pages: could not remove the figure {key}: {exc!r}")


def remove_article_page(article_id: str, figure_count: int = 0) -> str:
    """Delete an article's static page from the site bucket and return its key.

    `figure_count` is how many figures the article stores (common/figures.py); their copies on
    the site come down after the page, best effort (remove_article_figures). Deleting a page
    that isn't there is not an error (S3 answers 204), so this is safe to repeat.
    """
    key = article_page_key(article_id)
    _get_s3_client().delete_object(Bucket=os.environ["SITE_BUCKET"], Key=key)
    remove_article_figures(article_id, figure_count)
    return key


def invalidate_article_page(article_id: str) -> bool:
    """Ask CloudFront to drop its cached copy of an article's page, and of its figures.

    Best effort, and it never raises: by the time this is called the page is
    already gone from the origin, so a failure only means viewers may keep a
    cached copy until CloudFront's TTL expires. Returns whether an invalidation
    was requested (False when CLOUDFRONT_DISTRIBUTION_ID isn't configured).
    """
    distribution_id = os.environ.get("CLOUDFRONT_DISTRIBUTION_ID")
    if not distribution_id:
        return False
    # The figures are cached for a day (publish_figures), unlike the page, which revalidates on
    # every load: a wildcard covers however many the article had.
    paths = [f"/{article_page_key(article_id)}", f"/{PUBLIC_FIGURE_PREFIX}{article_id}/*"]
    try:
        _get_cloudfront_client().create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": len(paths), "Items": paths},
                "CallerReference": f"unpublish-{article_id}-{time.time_ns()}",
            },
        )
    except Exception as exc:  # noqa: BLE001 - best effort, see docstring
        print(f"static_pages: could not invalidate the cached page for {article_id}: {exc!r}")
        return False
    return True


_PUBLISHED_BY_LABELS = {
    "ai_only": "AI only",
    "humans": "Humans",
    "humans_and_ai": "Humans & AI",
}


def _published_by_label(published_by: str | None) -> str:
    if published_by is None:
        return "No data"
    return _PUBLISHED_BY_LABELS.get(published_by, published_by)


def _format_cost_label(lineage: dict) -> str:
    cost_aud = lineage.get("cost_aud")
    if cost_aud is not None:
        return f"~${cost_aud:.2f} AUD"
    return lineage.get("cost_note") or "No data"


def _summary_cost_label(lineage: dict) -> str:
    """The cost figure for the compact one-line summary: the article's total cost (authoring +
    research, common/costing.py's build_lineage) when a research tally exists, since that -- not
    the authoring-only cost_aud -- is what a reader means by "the cost" of the article. Checked
    via the "research" key itself, not just total_cost_aud being present: a research tally that
    couldn't be priced still means there IS a real, larger cost than cost_aud alone, so this says
    "No data" rather than silently understating it by falling back to the authoring figure.

    Falls back to the plain cost label when there's no research tally at all (e.g.
    trending_digest_handler.py's cross-topic synthesis, which was never split into research vs.
    authoring in the first place) -- cost_aud there already *is* the whole cost.
    """
    if "research" in lineage:
        total = lineage.get("total_cost_aud")
        return f"~${total:.2f} AUD" if total is not None else "No data"
    return _format_cost_label(lineage)


def _model_name(lineage: dict, model_id: str) -> str:
    """A model's readable name (recorded in the lineage at build time), else its id."""
    return (lineage.get("model_labels") or {}).get(model_id) or model_id


def _research_text(research: dict) -> str:
    """One line for the research tally: findings summarised and their tokens."""
    tracked = research.get("tracked_findings", 0)
    untracked = research.get("untracked_findings", 0)
    if not tracked:
        if untracked:
            return f"Not tracked ({untracked} finding(s) predate research tracking)"
        return "No research calls"
    text = (
        f"{tracked} finding(s): {int(research.get('input_tokens', 0)):,} in / "
        f"{int(research.get('output_tokens', 0)):,} out"
    )
    if untracked:
        text += f" (+{untracked} earlier finding(s) not tracked)"
    return text


def _per_model_token_breakdown_text(lineage: dict) -> str:
    """Plain-text "model: X in / Y out" per model actually used, summed
    across every call that used it (a single article can span more than
    one model, e.g. a fallback kicking in partway through). Unescaped --
    callers escape at the point they embed it in HTML, so the compact
    summary line (which escapes its whole string once) and the footer
    block don't double-escape."""
    totals: dict[str, dict[str, int]] = {}
    order: list[str] = []
    for call in lineage.get("calls") or []:
        model_id = call.get("model_id", "")
        if model_id not in totals:
            totals[model_id] = {"input": 0, "output": 0}
            order.append(model_id)
        totals[model_id]["input"] += int(call.get("input_tokens", 0))
        totals[model_id]["output"] += int(call.get("output_tokens", 0))
    if not order:
        return "No data"
    return ", ".join(
        f"{_model_name(lineage, model_id)}: "
        f"{totals[model_id]['input']:,} in / {totals[model_id]['output']:,} out"
        for model_id in order
    )


def _per_model_token_breakdown_html(lineage: dict) -> str:
    return escape(_per_model_token_breakdown_text(lineage))


def _render_lineage_summary_line_html(lineage: dict | None, published_by: str | None) -> str:
    """Compact one-line lineage summary for next to the existing gray-text
    published date -- the terse counterpart to the fuller footer block
    above. Same "No data" wording when there's nothing to show."""
    if lineage is None and published_by is None:
        return "No data"

    models_used = (lineage or {}).get("models_used") or []
    models_text = (
        ", ".join(_model_name(lineage or {}, m) for m in models_used) if models_used else "no data"
    )
    tokens_text = _per_model_token_breakdown_text(lineage) if lineage is not None else "no data"
    cost_text = _summary_cost_label(lineage) if lineage is not None else "No data"
    approved_text = _published_by_label(published_by)

    return escape(
        f"models [{models_text}] · tokens [{tokens_text}] · "
        f"approved by {approved_text} · {cost_text}"
    )


def render_attribution_html(attribution: list[dict] | None) -> str:
    """The source credit line that sits under the title block (common/attribution.py), or ""
    when there is nothing to credit.

    One italic sentence per source, the declared `label` inside it linked to the declared URL.
    Every piece is escaped here, and `clean_sources` has already dropped anything that is not a
    plain {"text", "label", "url"} with an https URL, so neither a careless declaration nor a
    tampered Articles item can put markup or a `javascript:` link on the page. The links open in
    a new tab with rel="noopener noreferrer": the source's site gets no handle on this page and
    no referrer.

    The italics are in the markup (<em>) as well as the stylesheet, so the credit reads as one
    even on a cached older styles.css. CoinGecko's terms set a minimum size for it: see
    .source-attribution in frontend/styles.css.
    """
    credits = []
    for source in clean_sources(attribution):
        before, label, after = source["text"].partition(source["label"])
        credits.append(
            f"<em>{escape(before)}"
            f'<a href="{escape(source["url"], quote=True)}" target="_blank" rel="noopener noreferrer">'
            f"{escape(label)}</a>"
            f"{escape(after)}</em>"
        )
    if not credits:
        return ""
    return f'<p class="source-attribution" data-role="source-attribution">{" &#183; ".join(credits)}</p>'


# The site serves an article's figures from the site bucket (CloudFront caches an object for a
# day, the page itself revalidates on every load: see render_and_publish_article_page). A figure
# never changes once the article is written, so a day is right; a take-down invalidates them.
_FIGURE_CACHE_CONTROL = "public, max-age=86400"


def publish_figures(article_id: str, figures: list[dict] | None) -> list[dict]:
    """Copy an article's figures (common/figures.py: [{"key", "caption", "alt"}], each key a PNG
    in the content bucket) into the site bucket under articles/figures/<article_id>/<n>.png, `n`
    the figure's position in the list from 1, and return the copies as the page shows them:
    [{"src", "caption", "alt"}], `src` the site path.

    One S3 CopyObject per figure, server side (the Lambda role may read the content bucket and
    write under `articles/` in the site bucket; nothing passes through this function). The copy
    is stored as image/png with its own cache header, whatever the source object said
    (MetadataDirective REPLACE). A copy that fails -- the source is gone, a permission -- is said
    and skipped: the article still publishes, without that figure, and a re-render copies again.
    The positions of the others are kept, so the stored list, the copies and a later take-down
    (remove_article_figures) always agree on which key is which.
    """
    cleaned = clean_figures(figures)
    if not cleaned:
        return []
    s3 = _get_s3_client()
    content_bucket = os.environ["CONTENT_BUCKET"]
    site_bucket = os.environ["SITE_BUCKET"]
    published: list[dict] = []
    for index, figure in enumerate(cleaned, start=1):
        key = figure_key(article_id, index)
        try:
            s3.copy_object(
                Bucket=site_bucket,
                Key=key,
                CopySource={"Bucket": content_bucket, "Key": figure["key"]},
                ContentType="image/png",
                MetadataDirective="REPLACE",
                CacheControl=_FIGURE_CACHE_CONTROL,
            )
        except Exception as exc:  # noqa: BLE001 - the article publishes without this figure
            print(f"static_pages: could not copy the figure {figure['key']} to {key}: {exc!r}")
            continue
        published.append({"src": "/" + key, "caption": figure["caption"], "alt": figure["alt"]})
    return published


def render_figures_html(figures: list[dict] | None) -> str:
    """The figures as they sit between the credit line and the body: one
    <figure class="article-figure"> per entry of publish_figures' answer, the image lazily
    loaded with its alt text, the caption under it. Every value is escaped here, and the CSP
    forbids inline styles, so there is no `style=` anywhere in it. "" when there are none: a
    page without figures has no empty block to explain."""
    blocks = []
    for figure in figures or []:
        blocks.append(
            '<figure class="article-figure">'
            f'<img src="{escape(figure["src"], quote=True)}" alt="{escape(figure["alt"], quote=True)}"'
            ' loading="lazy" />'
            f"<figcaption>{escape(figure['caption'])}</figcaption>"
            "</figure>"
        )
    return "".join(blocks)


def _render_lineage_footer_html(
    lineage: dict | None, published_by: str | None, fact_check: str | None = None
) -> str:
    """Render the "Lineage" footer block (docs/project-plan.md §11, PR 3 of
    5) -- always present, even when there's nothing to show, so an
    article published before this feature existed reads as an explicit
    "No data" rather than a rendering gap that looks like a bug.
    """
    if lineage is None:
        models_html = "No data"
        tokens_html = "No data"
        cost_html = "No data"
    else:
        models_used = lineage.get("models_used") or []
        models_html = (
            escape(", ".join(_model_name(lineage, m) for m in models_used)) if models_used else "No data"
        )
        tokens_html = _per_model_token_breakdown_html(lineage)
        cost_html = escape(_format_cost_label(lineage))

    approved_html = escape(_published_by_label(published_by))

    # The research the article's findings cost (tallied hourly, bundled in when
    # the article is written). Absent on an article made before research tracking.
    research_rows = ""
    research = (lineage or {}).get("research")
    if research is not None:
        total = lineage.get("total_cost_aud")
        total_text = f"~${total:.2f} AUD" if total is not None else "No data"
        research_rows = (
            f"<dt>Research tokens</dt><dd>{escape(_research_text(research))}</dd>"
            f"<dt>Research cost</dt><dd>{escape(_format_cost_label(research))}</dd>"
            f"<dt>Total cost</dt><dd>{escape(total_text)}</dd>"
        )

    # Only present when the fresh-data review was enforced for this article (see
    # common/fact_check.py): a shadow-mode review changed nothing, so claiming a check
    # would overstate it.
    fact_check_row = f"<dt>Fact check</dt><dd>{escape(fact_check)}</dd>" if fact_check else ""

    return (
        '<footer class="lineage-footer" aria-label="Article lineage">'
        "<h2>Lineage</h2>"
        "<dl>"
        f"<dt>Models</dt><dd>{models_html}</dd>"
        f"<dt>Tokens</dt><dd>{tokens_html}</dd>"
        f"<dt>Approved by</dt><dd>{approved_html}</dd>"
        f"<dt>Approx. cost</dt><dd>{cost_html}</dd>"
        f"{research_rows}"
        f"{fact_check_row}"
        "</dl>"
        "</footer>"
    )


# The noun shown for each slot in the "Equipment used" record -- mirrors frontend/gear.js's
# SLOT_LABELS, kept as a separate copy here since this file renders server-side HTML text, not
# the JS the Stats page draws with.
_EQUIPMENT_SLOT_LABELS = {
    "helmet": "Helmet",
    "chest": "Chest",
    "gloves": "Gloves",
    "boots": "Boots",
    "sword": "Sword",
    "shield": "Shield",
    "ring": "Ring",
    "legacy": "Guidance",
}


def equipment_snapshot(equipment_used: list[dict] | None) -> list[dict]:
    """The gear `equipment_used` names, as it is right now -- read once, here, so the caller can
    bake it into the page and never ask again. A piece that no longer exists (deleted before this
    page was ever rendered) is left out; there is nothing left to describe. Each piece appears
    once even if it is listed more than once (e.g. two armor pieces cited by the same version by
    mistake never happens, but a defensive de-dupe costs nothing).

    Public (not `_`-prefixed): also called live, per request, by public_api_handler.py's
    `_get_article_detail` for the SPA's own article view -- unlike the static page below, which
    calls this once and bakes the result into HTML that then sits in S3/CloudFront until the page
    is re-rendered, the SPA re-fetches an article's JSON on every visit anyway, so showing today's
    actual gear state there (rather than a frozen historical snapshot) is consistent with
    everything else it already shows live (view count, feedback, ...)."""
    if not equipment_used:
        return []
    topic_names: dict[str, str] = {}
    snapshot = []
    seen: set[tuple] = set()
    for piece in equipment_used:
        if not isinstance(piece, dict):
            continue
        topic_id, version = piece.get("topic_id"), piece.get("version")
        if not topic_id or not version or (topic_id, version) in seen:
            continue
        seen.add((topic_id, version))
        item = get_prompt_refinement(topic_id, version)
        if item is None:
            continue
        if item.get("slot") == "ring" and topic_id not in topic_names:
            topic = get_topic(topic_id)
            if topic is not None:
                topic_names[topic_id] = topic.get("name", topic_id)
        snapshot.append(_gear_public_view(item, topic_names))
    return snapshot


def _equipment_item_html(piece: dict) -> str:
    slot_label = _EQUIPMENT_SLOT_LABELS.get(piece.get("slot"), "Gear")
    rarity = str(piece.get("rarity") or "common")
    applies_to = piece.get("topic_name") or "Every topic"
    meta = f"{rarity.capitalize()} \u00b7 {slot_label} \u00b7 {escape(applies_to)}"
    description_html = (
        f'<p class="equipment-desc">{escape(piece["description"])}</p>' if piece.get("description") else ""
    )
    return (
        f'<li class="equipment-item rarity-{escape(rarity)}">'
        f'<p class="equipment-name">{escape(piece.get("name") or "Unnamed gear")}</p>'
        f'<p class="equipment-meta">{meta}</p>'
        f"{description_html}"
        "</li>"
    )


def _render_equipment_footer_html(snapshot: list[dict]) -> str:
    """The "Equipment used" record, or "" when the article used none -- unlike the Lineage
    footer, this is never shown as an explicit "No data": most articles (and every one from
    before this feature) simply have nothing here, and that is not a gap worth calling out."""
    if not snapshot:
        return ""
    items_html = "".join(_equipment_item_html(piece) for piece in snapshot)
    return (
        '<footer class="equipment-footer" aria-label="Equipment used">'
        "<h2>Equipment used</h2>"
        f'<ul class="equipment-list">{items_html}</ul>'
        "</footer>"
    )


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


# The article title is the page's one <h1>, so a "#" in the body becomes an <h2> (and "##" an
# <h3>, ...): screen-reader users navigate by heading level. "tables" and "sane_lists" cover
# the markdown the drafts actually use; "toc" is what applies the base level (and gives each
# heading an id to link to).
_BODY_HEADING_BASE_LEVEL = 2
_MARKDOWN_EXTENSIONS = ["tables", "sane_lists", "toc"]
_MARKDOWN_EXTENSION_CONFIGS = {"toc": {"baselevel": _BODY_HEADING_BASE_LEVEL}}


def render_body_html(body_markdown: str) -> str:
    """An article body's markdown as HTML, with its headings below the page's <h1>."""
    return markdown.markdown(
        body_markdown,
        extensions=_MARKDOWN_EXTENSIONS,
        extension_configs=_MARKDOWN_EXTENSION_CONFIGS,
    )


# The site's own sections, as absolute links because a static article page lives at
# /articles/<id>.html, not at the SPA's root. Mirrors index.html's header and footer.
_SITE_SECTIONS = (
    ("/#/topic/digest", "Trending Everywhere"),
    ("/#/musings", "Musings"),
    ("/#/stats", "Stats"),
)


# A paw print after every footer link but the last. The link and its paw sit in one nowrap span,
# so a wrapped line never starts with a paw; the paw is hidden from screen readers. Mirrors
# index.html.
_PAW_HTML = '<span class="paw" aria-hidden="true">&#128062;</span>'


def _links_html(links, paws: bool = False) -> str:
    anchors = [f'<a href="{href}">{escape(label)}</a>' for href, label in links]
    if not paws:
        return "".join(anchors)
    items = [f'<span class="footer-item">{anchor}{_PAW_HTML}</span>' for anchor in anchors[:-1]]
    return " ".join([*items, anchors[-1]])


def _site_sections_links_html(paws: bool = False) -> str:
    return _links_html(_SITE_SECTIONS, paws)


_LEGAL_LINKS = (
    ("/", "Home"),
    ("/about.html", "About"),
    ("/terms.html", "Terms of Service"),
    ("/privacy.html", "Privacy Policy"),
)


def render_and_publish_article_page(
    *,
    article_id: str,
    title: str,
    body_markdown: str,
    topic_name: str,
    published_at: str | None,
    source_refs: list[dict] | None = None,
    view_count: int = 0,
    lineage: dict | None = None,
    published_by: str | None = None,
    fact_check: str | None = None,
    equipment_used: list[dict] | None = None,
    attribution: list[dict] | None = None,
    figures: list[dict] | None = None,
) -> str:
    """Render `article_id` as a static HTML page and upload it to the site
    bucket. Returns the S3 key it was written to.

    `attribution` is the article's source credit (common/attribution.py: what was stored on the
    article when it was drafted, or its topic's adapter's sources for an older one). It is
    shown in italics just under the title block; see render_attribution_html.

    `figures` are the article's stored figures (common/figures.py). Their PNGs are copied into
    the site bucket here, on every render (publish_figures: a re-render after a take-down puts
    them back), and the page shows the copies between the credit line and the body.

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
    body_html = render_body_html(body_markdown)
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
    lineage_summary_line_html = _render_lineage_summary_line_html(lineage, published_by)
    attribution_html = render_attribution_html(attribution)
    figures_html = render_figures_html(publish_figures(article_id, figures))
    lineage_footer_html = _render_lineage_footer_html(lineage, published_by, fact_check)
    equipment_footer_html = _render_equipment_footer_html(equipment_snapshot(equipment_used))
    footers_html = f'<div class="article-footers">{lineage_footer_html}{equipment_footer_html}</div>'
    site_sections_html = _site_sections_links_html()
    footer_sections_html = _site_sections_links_html(paws=True)
    footer_legal_html = _links_html(_LEGAL_LINKS, paws=True)

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
<a class="skip-link" href="#content">Skip to content</a>
<header id="top">
<a class="site-title" href="/">BloggerBear</a>
<nav id="nav-site" aria-label="Site sections">{site_sections_html}</nav>
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
<p class="lineage-summary">{lineage_summary_line_html}</p>
{attribution_html}
{figures_html}
<div class="article-body">{body_html}</div>
{source_refs_html}
{footers_html}
<section class="feedback" data-role="feedback">
<h2>Feedback</h2>
<div hidden aria-hidden="true">
<label for="feedback-referral">Referral code (optional)</label>
<input type="text" id="feedback-referral" name="referral_code" data-role="referral"
 tabindex="-1" autocomplete="off" />
</div>
<div class="feedback-buttons" hidden>
<button type="button" data-role="upvote" aria-label="Upvote this article">Upvote</button>
<button type="button" data-role="downvote" aria-label="Downvote this article">Downvote</button>
</div>
<p class="feedback-status" data-role="feedback-status" aria-live="polite" aria-atomic="true"></p>
</section>
</main>
<footer class="site-footer">
<nav class="footer-nav" aria-label="Explore">{footer_sections_html}</nav>
<nav class="legal-nav" aria-label="Legal">{footer_legal_html}</nav>
<div class="footer-links"><a class="back-to-top" href="#top">Back to top &#8593;</a></div>
</footer>
<script src="/config.js"></script>
<script src="/verify.js" defer></script>
<script src="/tummy.js" defer></script>
<script src="/article-widgets.js" defer></script>
</body>
</html>
"""

    key = article_page_key(article_id)
    _get_s3_client().put_object(
        Bucket=os.environ["SITE_BUCKET"],
        Key=key,
        Body=page_html.encode("utf-8"),
        ContentType="text/html",
        # Revalidate on every load (a cheap conditional request): a re-rendered page (a republish,
        # a template fix) shows up straight away instead of after CloudFront's 24-hour default.
        CacheControl="no-cache",
    )
    # No CloudFront invalidation on republish: the page is stored with
    # Cache-Control: no-cache, so CloudFront and browsers revalidate it on
    # each request and a republish of the same article_id (e.g. force-publish
    # correcting a bad moderation call) shows up without an invalidation.
    # (unpublish still invalidates: see invalidate_article_page.)
    return key
