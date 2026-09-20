"""Shared constants for the cross-topic "trending everywhere" digest.

Lives in `common/` rather than inside `trending_digest_handler.py` (which
originally owned both) because `admin_api_handler.py`'s moderation-approve
and force-publish routes also need to resolve a friendly display name for
a digest article -- `get_topic("digest")` returns None (it isn't a real
Topics-table row), so without this, those paths would fall back to the
raw topic_id string "digest" instead of a proper name when rendering a
digest article's static page or generating its musing. Confirmed the hard
way: this exact gap existed until a digest article's *own* direct publish
path (trending_digest_handler.py's compliant branch) was wired up to
static-page rendering and musings, at which point the inconsistency
between the two paths' topic names became real and visible.
"""

from __future__ import annotations

DIGEST_TOPIC_ID = "digest"

# Matches frontend/app.js's own hardcoded heading for this topic_id
# (renderArticleList's DIGEST_TOPIC_ID special case) -- keep both in sync
# if either ever changes.
DIGEST_TOPIC_NAME = "Trending Everywhere"
