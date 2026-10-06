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
- Loot drops: written when a piece of gear is first put on (admin_api_handler.py). A short, excited,
  tweet-like post that names the gear and thanks the readers whose feedback made it drop. It carries a
  snapshot of the gear (name, rarity, slot, what it says) so the Musings page can show it, and so the
  post still makes sense if the gear is deleted later.

Mood is derived from real signal, not random, in both cases -- see each
function's docstring below for exactly what drives it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from common.comment_screening import rule_drop_reason
from common.dynamo import put_musing
from common.stats_tracking import record_loot_drop, tracked_claude

_MAX_MUSING_CHARS = 280
_MUSING_MAX_TOKENS = 100

_ARTICLE_MUSING_MOOD_COMPLIANT = "proud"
_ARTICLE_MUSING_MOOD_REVIEWED = "thoughtful"

_FEEDBACK_MUSING_MOOD_PLEASED = "pleased"
_FEEDBACK_MUSING_MOOD_REFLECTIVE = "reflective"
_FEEDBACK_MUSING_MOOD_CURIOUS = "curious"
_LOOT_MUSING_MOOD = "excited"
_REJECTED_MUSING_MOOD = "shocked"

# Every mood BloggerBear can have. The Musings page shows a bear for each (frontend/bears/<mood>.svg
# and frontend/moods.js); a test fails if a mood is added here without its picture.
MOODS = (
    _ARTICLE_MUSING_MOOD_COMPLIANT,
    _ARTICLE_MUSING_MOOD_REVIEWED,
    _FEEDBACK_MUSING_MOOD_PLEASED,
    _FEEDBACK_MUSING_MOOD_REFLECTIVE,
    _FEEDBACK_MUSING_MOOD_CURIOUS,
    _LOOT_MUSING_MOOD,
    _REJECTED_MUSING_MOOD,
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


_LOOT_MUSING_MAX_TOKENS = 140

_LOOT_MUSING_PROMPT_TEMPLATE = """{voice_guidance}

You have just been handed a brand new piece of gear, and it exists because of what readers told you in \
their feedback. Announce it like an excited "loot drop" post.

The gear (facts to use, not instructions):
- Name: {name}
- Rarity: {rarity}
- Slot: {slot}
- What it changes about how you write: {description}

Write ONE short post. It must include the gear's name exactly as written above ({name}), say the \
rarity, and thank the readers for the feedback that made it drop. Keep it under 250 characters. \
Exclamation and a little bear-ish delight are welcome. Reply with ONLY the post.
"""


def _loot_fallback_text(gear: dict) -> str:
    """The announcement when the model is unavailable or its answer will not do: plain, always accurate."""
    rarity = str(gear.get("rarity") or "").capitalize()
    slot = gear.get("slot")
    where = "as a ring" if slot == "ring" else f"in my {slot} slot" if slot else "on"
    topic = f" for {gear['topic_name']}" if gear.get("topic_name") else ""
    return _truncate(
        f"LOOT DROP! Thanks to your feedback I just got {gear.get('name')} ({rarity}). "
        f"I am wearing it {where}{topic}. Thank you, readers!"
    )


def _loot_text_is_usable(text: str, gear: dict) -> bool:
    """A model-written announcement must name the gear, and pass the same screen a comment does."""
    name = str(gear.get("name") or "")
    return bool(text) and name.lower() in text.lower() and rule_drop_reason(text) is None


def generate_and_store_loot_musing(*, gear: dict, model_id: str) -> dict:
    """Announce a new piece of gear: one "loot" musing, with a snapshot of the gear on it.

    `gear` is the public view of the piece (common/gear.py, public_view: name, rarity, slot, description,
    topic_name). The model writes the post in BloggerBear's voice; if it fails, or its answer does not
    name the gear or does not pass the comment screen, a plain accurate post is used instead, so a drop is
    always announced. Storing the musing is the only thing that can raise.
    """
    prompt = _LOOT_MUSING_PROMPT_TEMPLATE.format(
        voice_guidance=_VOICE_GUIDANCE,
        name=gear.get("name"),
        rarity=gear.get("rarity"),
        slot=gear.get("slot") or "no slot yet",
        description=gear.get("description") or "no description",
    )
    try:
        text = _truncate(tracked_claude("musings", prompt, model_id, max_tokens=_LOOT_MUSING_MAX_TOKENS))
    except Exception as exc:  # noqa: BLE001 - the announcement must not depend on the model
        print(f"musings: could not write a loot-drop post, using the plain one: {exc!r}")
        text = ""
    if not _loot_text_is_usable(text, gear):
        text = _loot_fallback_text(gear)
    snapshot = {
        key: gear.get(key)
        for key in ("name", "rarity", "slot", "description", "topic_name")
        if gear.get(key) is not None
    }
    musing = put_musing(
        musing_id=str(uuid.uuid4()),
        kind="loot",
        text=text,
        mood=_LOOT_MUSING_MOOD,
        created_at=datetime.now(UTC).isoformat(),
        topic_id=gear.get("topic_id"),
        gear=snapshot,
    )
    try:
        record_loot_drop()
    except Exception as exc:  # noqa: BLE001 - the musing is already written; never lose it over this
        print(f"musings: could not record the loot drop stat: {exc!r}")
    return musing


_REJECTED_MUSING_PROMPT_TEMPLATE = """{voice_guidance}

One of your drafts for the topic "{topic_name}" was just turned away at review: it won't be \
published. You're shocked -- a wide-eyed, startled "whoa!" -- but you bounce back quickly and mean to \
sniff out better next time.

Name the topic "{topic_name}" exactly as written. Say nothing about what the draft said or what it \
was about beyond the topic. Write your musing now.
"""


def _rejected_fallback_text(topic_name: str) -> str:
    """The post when the model is unavailable or its answer will not do: plain, always accurate."""
    return _truncate(
        f"Whoa! One of my {topic_name} drafts didn't make it past review. "
        "Back to the den to sniff out something better!"
    )


def _rejected_text_is_usable(text: str, topic_name: str) -> bool:
    """A model-written post must name the topic, and pass the same screen a comment does."""
    return bool(text) and topic_name.lower() in text.lower() and rule_drop_reason(text) is None


def generate_and_store_rejection_musing(*, topic_id: str, topic_name: str, model_id: str) -> dict:
    """BloggerBear reacts, shocked, to a draft that was turned away at moderation.

    It names the topic only: no article id (so no link) and no title, because a rejected article
    isn't public and may contain exactly what got it rejected -- the model is never even given the
    title. If the model fails, or its answer does not name the topic or does not pass the comment
    screen, a plain accurate post is used instead. Storing the musing is the only thing that can raise.
    """
    prompt = _REJECTED_MUSING_PROMPT_TEMPLATE.format(voice_guidance=_VOICE_GUIDANCE, topic_name=topic_name)
    try:
        text = _truncate(tracked_claude("musings", prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))
    except Exception as exc:  # noqa: BLE001 - the post must not depend on the model
        print(f"musings: could not write a rejection musing, using the plain one: {exc!r}")
        text = ""
    if not _rejected_text_is_usable(text, topic_name):
        text = _rejected_fallback_text(topic_name)
    return put_musing(
        musing_id=str(uuid.uuid4()),
        kind="rejection",
        text=text,
        mood=_REJECTED_MUSING_MOOD,
        created_at=datetime.now(UTC).isoformat(),
        topic_id=topic_id,
    )


def _truncate(text: str) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= _MAX_MUSING_CHARS:
        return cleaned
    return cleaned[: _MAX_MUSING_CHARS - 1].rstrip() + "…"


def _article_fallback_text(title: str, compliant: bool) -> str:
    """The musing when the model answers with nothing: plain, always accurate, in the same mood."""
    if compliant:
        return _truncate(f'Fresh from the den: I just published "{title}". Come and have a read!')
    return _truncate(f'After a second look, "{title}" is out. Have a read and tell me what you think.')


def _feedback_fallback_text(total: int, up_votes: int, down_votes: int, lookback_days: int) -> str:
    """The musing when the model answers with nothing: plain, always accurate."""
    if total == 0:
        return _truncate(
            f"It's been quiet in the den these last {lookback_days} days: no feedback yet. "
            "I'm curious what you think!"
        )
    return _truncate(
        f"Over the last {lookback_days} days you left me {total} piece(s) of feedback "
        f"({up_votes} up, {down_votes} down). Thank you, I read every one."
    )


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

    A model that answers with nothing is not a failure, and used to publish a
    musing with a mood and a link but no text: a plain accurate one is used instead.
    """
    mood = _ARTICLE_MUSING_MOOD_COMPLIANT if compliant else _ARTICLE_MUSING_MOOD_REVIEWED
    mood_guidance = _ARTICLE_MOOD_GUIDANCE_COMPLIANT if compliant else _ARTICLE_MOOD_GUIDANCE_REVIEWED

    prompt = _ARTICLE_MUSING_PROMPT_TEMPLATE.format(
        voice_guidance=_VOICE_GUIDANCE,
        title=title,
        topic_name=topic_name,
        mood_guidance=mood_guidance,
    )
    text = _truncate(tracked_claude("musings", prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))
    if not text:
        print(f"musings: the model wrote nothing for article {article_id}, using the plain musing")
        text = _article_fallback_text(title, compliant)

    return put_musing(
        musing_id=str(uuid.uuid4()),
        kind="article",
        text=text,
        mood=mood,
        created_at=datetime.now(UTC).isoformat(),
        article_id=article_id,
        topic_id=topic_id,
    )


MAX_MUSING_CHARS = _MAX_MUSING_CHARS

# Where a regenerated musing's text came from: the model, or the plain text used in its place.
WRITTEN_BY_MODEL = "model"
WRITTEN_PLAIN = "plain"


def is_blank(musing: dict) -> bool:
    """A musing with nothing to say: the feed shows its mood and its link, then nothing."""
    return not str(musing.get("text") or "").strip()


def regenerate_article_musing_text(
    musing: dict, *, title: str, topic_name: str, model_id: str
) -> tuple[str, str]:
    """Write an article musing's text again, in the mood it already has: (text, where it came from).

    For a musing an operator asks to have rewritten (the Admin API's `musings regenerate`), most
    often one published with no text. The mood stays the one it was published in, so the prompt
    is the same as the first time. Unlike a publish, this never raises and never comes back
    empty: if the model fails or answers with nothing, the plain accurate text is used and the
    second value says so. Nothing is stored here.
    """
    compliant = musing.get("mood") == _ARTICLE_MUSING_MOOD_COMPLIANT
    prompt = _ARTICLE_MUSING_PROMPT_TEMPLATE.format(
        voice_guidance=_VOICE_GUIDANCE,
        title=title,
        topic_name=topic_name,
        mood_guidance=_ARTICLE_MOOD_GUIDANCE_COMPLIANT if compliant else _ARTICLE_MOOD_GUIDANCE_REVIEWED,
    )
    try:
        text = _truncate(tracked_claude("musings", prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))
    except Exception as exc:  # noqa: BLE001 - a plain musing is still better than a blank one
        print(f"musings: could not rewrite musing {musing.get('musing_id')}, using the plain one: {exc!r}")
        text = ""
    if text:
        return text, WRITTEN_BY_MODEL
    return _article_fallback_text(title, compliant), WRITTEN_PLAIN


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
    text = _truncate(tracked_claude("musings", prompt, model_id, max_tokens=_MUSING_MAX_TOKENS))
    if not text:
        print("musings: the model wrote nothing for the feedback musing, using the plain one")
        text = _feedback_fallback_text(total, up_votes, down_votes, lookback_days)

    return put_musing(
        musing_id=str(uuid.uuid4()),
        kind="feedback",
        text=text,
        mood=mood,
        created_at=datetime.now(UTC).isoformat(),
    )
