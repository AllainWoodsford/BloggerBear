"""The reader-facing line about the fresh-data review: "Checked against current data ...".

Kept apart from common/fresh_review.py (which imports the adapters, the model client and the
rest) so the public API, the static page renderer and the admin publish routes can all use it
without loading any of that.

**Honest about what actually happened.** A line appears only when the review was *enforced*
for the article. In shadow mode the review ran but changed nothing, so telling readers the
article was "checked" would overclaim; those articles get no line at all, and neither do ones
the adapter opted out of reviewing.
"""

from __future__ import annotations

CHECKED_CLEAN = "Checked against current data: no problems found"
CHECKED_CORRECTED = "Checked against current data: corrected before publishing"
CHECKED_BY_PERSON = "Checked against current data: reviewed by a person before publishing"
NOT_CHECKED = "Not checked against current data (the check was unavailable)"


def fact_check_label(record: dict | None, published_by: str | None) -> str | None:
    """The line for an article, or None when there is nothing honest to say.

    `record` is the article's stored `review`; `published_by` is "ai_only" or "humans"
    (a person approved it), which matters only for an article whose review could not run.
    """
    if not isinstance(record, dict) or record.get("mode") != "enforce":
        return None
    status = record.get("status")
    if status == "unavailable":
        return CHECKED_BY_PERSON if published_by == "humans" else NOT_CHECKED
    if status != "reviewed":
        return None  # skipped: the adapter had nothing to check against
    if record.get("revised"):
        return CHECKED_CORRECTED
    if record.get("held"):
        return CHECKED_BY_PERSON  # it could only have been published after a person approved it
    return CHECKED_CLEAN
