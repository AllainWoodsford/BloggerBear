"""The day-subject round: one topic that rotates through the subjects it is configured to watch.

docs/enhancements/dailysubject_hook.md (PR S1). The crypto feed varies its subject day by day
because its day goal is a pure function of the date (common/editorial_goals.py). This is that idea
for any adapter, through two optional hooks (common/adapters/base.py):

- `Adapter.subjects(new_state)`: which subjects an observation reports. The research tick keeps
  them on the Finding as `subjects` (`clean_subjects` below), only when there are any.
- `Adapter.day_subject(topic, at)`: the subject a topic writes about at the daily run's start, as a
  `DaySubject` (key, label, mandate, style), or None.

An adapter that returns None from the second (every adapter until one opts in) leaves its topics
exactly as they were: the same window, the same prompts, the same Findings and articles.

Topic-agnostic: what a subject is, whose day it is and the words for it are the adapter's. Nothing
here names an adapter or a domain (docs/project-plan.md §2 rule 5).
"""

from __future__ import annotations

from common.adapters.base import SUBJECT_KEY

# The Finding's field (research_tick_handler.py, common/dynamo.put_finding).
SUBJECTS_FIELD = "subjects"
# How many subjects one Finding keeps. A tick that measures one site reports one; the rail adapter
# measures at most a few per tick. Ten keys of at most 64 characters are under 1 KB of a Findings
# item's 400 KB.
MAX_SUBJECTS_PER_FINDING = 10


def clean_subjects(raw) -> list[str]:
    """The subjects a Finding keeps, from what an adapter's `subjects` returned: the strings that
    are SUBJECT_KEY-shaped, each once, in the order given, the first MAX_SUBJECTS_PER_FINDING.
    Anything that is not a list or tuple is none."""
    if not isinstance(raw, list | tuple):
        return []
    kept: list[str] = []
    for value in raw:
        if isinstance(value, str) and SUBJECT_KEY.fullmatch(value) and value not in kept:
            kept.append(value)
            if len(kept) == MAX_SUBJECTS_PER_FINDING:
                break
    return kept
