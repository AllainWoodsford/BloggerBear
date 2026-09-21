"""BloggerBear's "musings" -- short, first-person, in-character reflections.

Two kinds, both written to the Musings table (see common/dynamo.py) for the
public site's reverse-chronological Musings feed:

- Article musings: one generated every time an article gets published, from
  any of the three publish paths (daily_cycle_handler's compliant branch,
  admin_api_handler's moderation-approve, admin_api_handler's force-publish)
  -- same centralization requirement docs/project-plan.md §11 already
  established for common/static_pages.py's render_and_publish_article_page,
  for the same reason: three code paths that could each duplicate this logic
  would drift out of sync with each other.
- Feedback musings: generated periodically by musing_feedback_handler.py,
  reflecting on the volume/sentiment of reader feedback in a lookback window.

Mood is derived from real signal, not random, in both cases -- see each
function's docstring below for exactly what drives it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from common.bedrock import invoke_claude
from common.dynamo import put_musing

_MAX_MUSING_CHARS = 280
_MUSING_MAX_TOKENS = 100

_ARTICLE_MUSING_MOOD_COMPLIANT = "proud"
_ARTICLE_MUSING_MOOD_REVIEWED = "thoughtful"

_FEEDBACK_MUSING_MOOD_PLEASED = "pleased"
_FEEDBACK_MUSING_MOOD_REFLECTIVE = "reflective"
_FEEDBACK_MUSING_MOOD_CURIOUS = "curious"

# Every mood BloggerBear can have. The Musings page shows a bear for each (frontend/bears/<mood>.svg
# and frontend/moods.js); a test fails if a mood is added here without its picture.
MOODS = (
    _ARTICLE_MUSING_MOOD_COMPLIANT,
    _ARTICLE_MUSING_MOOD_REVIEWED,
    _FEEDBACK_MUSING_MOOD_PLEASED,
    _FEEDBACK_MUSING_MOOD_REFLECTIVE,
    _FEEDBACK_MUSING_MOOD_CURIOUS,
)

_VOICE_GUIDANCE = (
    "You are BloggerBear, an autonomous bear who researches and writes blog "
    "articles. Write ONE short, first-person musing in your own voice -- "
    "warm, a little whimsical, never breaking character, never mentioning "
    "being an AI, a language model, or any underlying technology. Bear-ish "
    "framing (paws, sniffing out sources, den, etc.) is welcome but don't "
    "force it into every sentence. Reply with ONLY the musing text itself, "
    "no quotation marks, no preamble, no hashtags. Target about 150 "
    "characters -- tweet-length, not a paragraph."
)

_ARTICLE_MUSING_PROMPT_TEMPLATE = """{voice_guidance}

You just published an article titled "{title}" for the topic "{topic_name}". \
{mood_guidance}

Write your musing now.
"""

_ARTICLE_MOOD_GUIDANCE_COMPLIANT = (
    "It sailed straight through review on the first pass -- you're feeling "
    "proud and a bit excited about it."
)
_ARTICLE_MOOD_GUIDANCE_REVIEWED = (
    "It needed a second look (compliance review or a human editor) before "
    "it went out -- you're feeling more thoughtful and measured about this "
    "one, not deflated, just reflective."
)

_FEEDBACK_MUSING_PROMPT_TEMPLATE = """{voice_guidance}

Over the last {lookback_days} days, readers left you {total} piece(s) of \
feedback ({up_votes} upvote(s), {down_votes} downvote(s)). {mood_guidance}

Write your musing now, mentioning the actual number of feedback pieces.
"""

_FEEDBACK_MOOD_GUIDANCE = {
    _FEEDBACK_MUSING_MOOD_PLEASED: (
        "On the whole, that's a well-received stretch -- you're feeling "
        "pleased and a little encouraged."
    ),
    _FEEDBACK_MUSING_MOOD_REFLECTIVE: (
        "On the whole, that's a mixed or less-positive stretch -- you're "
        "feeling reflective, thinking about what to do differently, not "
        "discouraged, just thoughtful."
    ),
    _FEEDBACK_MUSING_MOOD_CURIOUS: (
        "It's been quiet -- barely any feedback at all -- and you're "
        "feeling a little curious about that, wondering what readers are "
        "thinking, in a light and unbothered way."
    ),
}


def _truncate(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= _MAX_MUSING_CHARS:
        return cleaned
    return cleaned[: _MAX_MUSING_CHARS - 1].rstrip() + "…"


def generate_and_store_article_musing(
    *,
    article_id: str,
    topic_id: str,
    topic_name: str,
    title: str,
    compliant: bool,
    model_id: str,
) -> dict:
    """Generate and store one article musing. `compliant` drives the mood:

    True (published cleanly on the first pass, from daily_cycle_handler's
    own compliant branch) -> proud/excited. False (needed moderation-approve
    or a force-publish override first) -> more measured/thoughtful.

    Called the same way, and with the same failure semantics, as its sibling
    common.static_pages.render_and_publish_article_page at each of the three
    publish call sites: no local try/except here or at the call site -- a
    failure propagates up to that handler's own top-level "never raise
    unhandled" guard, consistent with how a static-page-render failure is
    already treated.
    """
    mood = _ARTICLE_MUSING_MOOD_COMPLIANT if compliant else _ARTICLE_MUSING_MOOD_REVIEWED
    mood_guidance = _ARTICLE_MOOD_GUIDANCE_COMPLIANT if compliant else _ARTICLE_MOOD_GUIDANCE_REVIEWED

    prompt = _ARTICLE_MUSING_PROMPT_TEMPLATE.format(
        voice_guidance=_VOICE_GUIDANCE,
        title=title,
        topic_name=topic_name,
        mood_guidance=mood_guidance,
    )
    text = _truncate(invoke_claude(prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))

    return put_musing(
        musing_id=str(uuid.uuid4()),
        kind="article",
        text=text,
        mood=mood,
        created_at=datetime.now(UTC).isoformat(),
        article_id=article_id,
        topic_id=topic_id,
    )


def derive_feedback_mood(*, up_votes: int, down_votes: int) -> str:
    """Derive a feedback musing's mood from the actual up/down tally.

    Zero feedback at all -> curious. Otherwise: net upvotes -> pleased,
    net-negative or a tie (mixed) -> reflective.
    """
    total = up_votes + down_votes
    if total == 0:
        return _FEEDBACK_MUSING_MOOD_CURIOUS
    net = up_votes - down_votes
    if net > 0:
        return _FEEDBACK_MUSING_MOOD_PLEASED
    return _FEEDBACK_MUSING_MOOD_REFLECTIVE


def generate_and_store_feedback_musing(
    *,
    up_votes: int,
    down_votes: int,
    lookback_days: int,
    model_id: str,
) -> dict:
    """Generate and store one periodic feedback musing.

    Mood is derived from the real up/down tally via derive_feedback_mood --
    called even when there's zero feedback in the window, so the feed
    doesn't go silent for lookback_days at a stretch; that case gets a
    "curious"-mood musing instead of no musing at all.
    """
    total = up_votes + down_votes
    mood = derive_feedback_mood(up_votes=up_votes, down_votes=down_votes)

    prompt = _FEEDBACK_MUSING_PROMPT_TEMPLATE.format(
        voice_guidance=_VOICE_GUIDANCE,
        lookback_days=lookback_days,
        total=total,
        up_votes=up_votes,
        down_votes=down_votes,
        mood_guidance=_FEEDBACK_MOOD_GUIDANCE[mood],
    )
    text = _truncate(invoke_claude(prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))

    return put_musing(
        musing_id=str(uuid.uuid4()),
        kind="feedback",
        text=text,
        mood=mood,
        created_at=datetime.now(UTC).isoformat(),
    )
