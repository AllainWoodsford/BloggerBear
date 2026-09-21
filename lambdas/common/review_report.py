"""Aggregate report on the fresh-data review (docs/project-plan.md §11, "(C)").

The review runs in shadow mode first: it records a result on every article and changes
nothing. Before anyone turns enforcement on, the questions that matter are numeric --
how often does it flag something, how often is it unavailable, and, above all, *how many
articles would enforcement have held back?* This module answers them from the records
already stored, so the decision is made from data rather than a hunch.

Pure functions over the articles and topics the caller already fetched (no AWS calls
here), like common/stats.py and common/lineage_tools.py.

**"Would have" numbers describe enforcement as scoped, not as built.** Under the plan, for
a non-financial topic a review with any `major` claim, or one that was `unavailable`, is
*held* for moderation; one with only `minor` claims is *revised*; `clean` and `skipped`
pass. Financial topics are always moderated whatever the review says, so they are counted
separately and left out of the hold/revise rates (enforcement would add notes, not routing).

**What it cannot tell you.** Whether a flagged claim is *right*. The sample of recent
flagged claims exists so a person can check that by eye; the readiness figures leave
precision as a manual step.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

# Starting points from the plan's go/no-go list -- to be argued with once there is data.
MIN_REVIEWED_NON_FINANCIAL = 20
MIN_DAYS_COVERED = 7
MIN_TOPICS_COVERED = 2
MAX_UNAVAILABLE_RATE = 0.10
MAX_WOULD_HOLD_RATE = 0.25

DEFAULT_SAMPLE_SIZE = 10
MAX_SAMPLE_SIZE = 50


def _created(article: dict) -> datetime | None:
    value = article.get("created_at")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _reason_bucket(reason) -> str:
    """Group unavailable reasons by their stable head ("could not fetch fresh data: ..."
    -> "could not fetch fresh data"), so one flaky source is one line, not fifty."""
    text = str(reason or "unknown").strip()
    head = text.split(":", 1)[0].strip()
    return head[:80] or "unknown"


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _empty_topic_row(topic_id: str, topics: dict[str, dict]) -> dict:
    topic = topics.get(topic_id) or {}
    return {
        "topic_id": topic_id,
        "name": topic.get("name") or topic_id,
        "is_financial": bool(topic.get("is_financial")),
        "articles": 0,
        "reviewed": 0,
        "clean": 0,
        "minor": 0,
        "major": 0,
        "unavailable": 0,
        "skipped": 0,
    }


def build_review_report(
    articles: list[dict],
    topics: list[dict],
    *,
    sample_size: int = DEFAULT_SAMPLE_SIZE,
    now: datetime | None = None,
) -> dict:
    sample_size = max(0, min(int(sample_size), MAX_SAMPLE_SIZE))
    topic_by_id = {topic["topic_id"]: topic for topic in topics}

    by_status: Counter = Counter()
    by_outcome: Counter = Counter()
    unavailable_reasons: Counter = Counter()
    claims_by_problem: Counter = Counter()
    claims_by_severity: Counter = Counter()
    rows: dict[str, dict] = {}
    flagged: list[dict] = []
    reviewed_dates: list[datetime] = []

    with_review = 0
    # For the enforcement preview: non-financial articles that carry a review record.
    would = Counter()

    for article in articles:
        topic_id = article.get("topic_id") or "unknown"
        row = rows.setdefault(topic_id, _empty_topic_row(topic_id, topic_by_id))
        row["articles"] += 1

        record = article.get("review")
        if not isinstance(record, dict):
            continue
        with_review += 1
        row["reviewed"] += 1

        status = record.get("status") or "unknown"
        by_status[status] += 1
        created = _created(article)
        if created is not None:
            reviewed_dates.append(created)

        outcome = None
        if status == "reviewed":
            outcome = record.get("outcome") or "unknown"
            by_outcome[outcome] += 1
            if outcome in ("clean", "minor", "major"):
                row[outcome] += 1
            for claim in record.get("claims") or []:
                claims_by_problem[claim.get("problem") or "unknown"] += 1
                claims_by_severity[claim.get("severity") or "unknown"] += 1
                flagged.append(
                    {
                        "article_id": article.get("article_id"),
                        "topic_id": topic_id,
                        "created_at": article.get("created_at"),
                        "claim": claim.get("claim"),
                        "problem": claim.get("problem"),
                        "severity": claim.get("severity"),
                        "evidence": claim.get("evidence"),
                    }
                )
        elif status == "unavailable":
            row["unavailable"] += 1
            unavailable_reasons[_reason_bucket(record.get("reason"))] += 1
        elif status == "skipped":
            row["skipped"] += 1

        if not row["is_financial"]:
            if status == "unavailable" or outcome == "major":
                would["hold"] += 1
            elif outcome == "minor":
                would["revise"] += 1
            else:
                would["pass"] += 1

    non_financial_reviewed = would["hold"] + would["revise"] + would["pass"]
    financial_reviewed = sum(r["reviewed"] for r in rows.values() if r["is_financial"])
    unavailable_non_financial = sum(r["unavailable"] for r in rows.values() if not r["is_financial"])

    for row in rows.values():
        row["flag_rate"] = _rate(row["minor"] + row["major"], row["reviewed"])

    flagged.sort(key=lambda item: item.get("created_at") or "", reverse=True)

    topics_covered = sum(1 for r in rows.values() if not r["is_financial"] and r["reviewed"])
    days_covered = (
        (max(reviewed_dates).date() - min(reviewed_dates).date()).days + 1 if reviewed_dates else 0
    )
    unavailable_rate = _rate(unavailable_non_financial, non_financial_reviewed)
    would_hold_rate = _rate(would["hold"], non_financial_reviewed)

    readiness = {
        "enough_articles": non_financial_reviewed >= MIN_REVIEWED_NON_FINANCIAL,
        "enough_days": days_covered >= MIN_DAYS_COVERED,
        "enough_topics": topics_covered >= MIN_TOPICS_COVERED,
        "unavailable_rate_ok": unavailable_rate is not None and unavailable_rate <= MAX_UNAVAILABLE_RATE,
        "would_hold_rate_ok": would_hold_rate is not None and would_hold_rate <= MAX_WOULD_HOLD_RATE,
        # Only a person can judge whether flagged claims are real: see `sample`.
        "precision_needs_a_manual_check": True,
    }
    readiness["all_measurable_criteria_met"] = all(
        value for key, value in readiness.items() if key != "precision_needs_a_manual_check"
    )

    return {
        "generated_at": (now or datetime.now(UTC)).isoformat(),
        "articles": len(articles),
        "with_review": with_review,
        "without_review": len(articles) - with_review,
        "days_covered": days_covered,
        "by_status": dict(by_status),
        "by_outcome": dict(by_outcome),
        "unavailable_reasons": dict(unavailable_reasons.most_common()),
        "claims": {
            "total": sum(claims_by_problem.values()),
            "by_problem": dict(claims_by_problem),
            "by_severity": dict(claims_by_severity),
        },
        "by_topic": sorted(rows.values(), key=lambda r: (-r["reviewed"], r["topic_id"])),
        "enforcement_preview": {
            "non_financial_reviewed": non_financial_reviewed,
            "would_hold": would["hold"],
            "would_revise": would["revise"],
            "would_pass": would["pass"],
            "would_hold_rate": would_hold_rate,
            "would_revise_rate": _rate(would["revise"], non_financial_reviewed),
            "unavailable_rate": unavailable_rate,
            "financial_reviewed_always_moderated": financial_reviewed,
        },
        "thresholds": {
            "min_reviewed_non_financial": MIN_REVIEWED_NON_FINANCIAL,
            "min_days_covered": MIN_DAYS_COVERED,
            "min_topics_covered": MIN_TOPICS_COVERED,
            "max_unavailable_rate": MAX_UNAVAILABLE_RATE,
            "max_would_hold_rate": MAX_WOULD_HOLD_RATE,
        },
        "readiness": readiness,
        "sample": flagged[:sample_size],
    }
