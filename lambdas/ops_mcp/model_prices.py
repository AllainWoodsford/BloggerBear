"""The assistant's view of the model catalog (common/model_catalog.py,
docs/enhancements/model-catalog-refresh.md): whether discover and reprice are paused, when they
run, what each last did, and the pricing run under review with when its next check is.

Read-only: one read of the catalog's rows in the ModelConfig table (its settings, its state and
the run under review), which the assistant's role may already read for its access switch. Model
ids come from Bedrock, so they are on screen only, never in what is spoken. A price the catalog put
back is pipeline_health's finding, not this tool's.
"""

from __future__ import annotations

from datetime import UTC, datetime

from common import model_catalog
from ops_mcp.tools import _age, _parse

# Fixed in Terraform (aws_scheduler_schedule.model_catalog_discover / _reprice), not a setting.
SCHEDULES = {
    "discover": "Tuesdays 03:00 Sydney time",
    "reprice": "Wednesdays 03:00 Sydney time",
}


def _last(job: str, state: dict, now: datetime) -> tuple[dict | None, str]:
    """A job's last run as data, and a sentence about it."""
    at = _parse(state.get(f"last_{job}_at"))
    result = state.get(f"last_{job}_result") if isinstance(state.get(f"last_{job}_result"), dict) else {}
    if at is None:
        return None, f"{job.capitalize()} has not run yet."
    ago = _age(at, now)
    if job == "discover":
        added = int(result.get("added") or 0)
        said = f"Discover last ran {ago} ago and added {added} model{'' if added == 1 else 's'}."
    elif result.get("status") == "review_pending":
        said = f"Reprice last ran {ago} ago but waited, as another pricing run was under review."
    else:
        changed = int(result.get("changed") or 0)
        said = f"Reprice last ran {ago} ago and changed {changed} price{'' if changed == 1 else 's'}."
    data = {
        "at": at.isoformat(),
        "trigger": result.get("trigger"),
        "status": result.get("status"),
        "added": result.get("added"),
        "changed": result.get("changed"),
        "confirmed": result.get("confirmed"),
        "unpriced_count": result.get("unpriced_count"),
    }
    return {k: v for k, v in data.items() if v is not None}, said


def model_catalog_status(now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    summary = model_catalog.summary()
    settings = summary["effective"]
    state = summary["state"]
    run = summary["under_review"]

    paused = [job for job in model_catalog.JOBS if not settings[f"{job}_enabled"]]
    spoken = ["Models are discovered on Tuesdays and repriced on Wednesdays, at 3 am Sydney time."]
    if paused:
        names = " and ".join(job.capitalize() for job in paused)
        spoken.append(f"{names} {'is' if len(paused) == 1 else 'are'} paused.")
    jobs = {}
    for job in model_catalog.JOBS:
        last, said = _last(job, state, now)
        jobs[job] = {
            "runs": SCHEDULES[job],
            "paused": job in paused,
            "last": last,
        }
        spoken.append(said)

    under_review = None
    if run is not None:
        count = len(run.get("models") or [])
        next_at = _parse(run.get("next_review_at"))
        when = (
            f"its next check is in {_age(now, next_at)}"
            if next_at and next_at > now
            else "its next check is due now"
        )
        spoken.append(
            f"{count} model price{'' if count == 1 else 's'} {'is' if count == 1 else 'are'} under review: "
            f"check {run.get('reviews_done', 0) + 1} of {run.get('reviews')}, and {when}."
        )
        under_review = {
            "run_id": run.get("run_id"),
            "applied_at": run.get("applied_at"),
            "models": run.get("models") or [],
            "reviews_done": run.get("reviews_done"),
            "reviews": run.get("reviews"),
            "next_review_at": run.get("next_review_at"),
            "put_back": [r.get("model_id") for r in run.get("reverted") or []],
        }
    else:
        spoken.append("No pricing run is under review.")

    return {
        "spoken": " ".join(spoken),
        "findings": [],
        "jobs": jobs,
        "reviews": {
            "after_hours": settings["review_after_hours"],
            "tolerance_pct": settings["review_tolerance_pct"],
            "max_change_factor": settings["max_change_factor"],
            "max_web_lookups": settings["max_web_lookups"],
        },
        "under_review": under_review,
        "as_of": now.isoformat(),
    }
