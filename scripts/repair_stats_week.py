#!/usr/bin/env python3
"""Mend StatsHistory after a week the rollover missed. Rarely needed; safe to run twice.

    python scripts/repair_stats_week.py --env production --missing-week 2026-09-28            # shows only
    python scripts/repair_stats_week.py --env production --missing-week 2026-09-28 --apply

The weekly rollover (lambdas/stats_rollover_handler.py) copies the week's counters into a
StatsHistory row and starts a new week. If it does not run one Monday, the counters keep adding
up, and the next rollover files two weeks of them under the first week's date. The second week
then has no row at all, and the cost poll, which only fills in rows the rollover made
(common/dynamo.py's set_stats_history_week_fields), never records that week's AWS bill: the
all-time bill is short by a week.

The counters cannot be pulled apart again: nothing recorded them by day. So this does the two
things that can be done, and nothing else:

1. Makes a placeholder row for the missing week, holding a note and no counters. The next daily
   cost poll fills in its AWS bill and adds it to the all-time bill. The poll re-reads the
   last six whole weeks (common/cost_explorer.py's COMPLETE_WEEKS), so run this within six
   weeks of the missing one.
2. Notes on the earlier week's row that its counters cover both weeks, and up to which day.

It never touches a counter or the all-time row, never overwrites a week that has a row, and
without --apply it writes nothing. It needs AWS credentials for the environment's account that
may read and write that one table, and the region it is deployed to (--region, or the AWS
CLI's configured default).
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime, timedelta

import boto3
from botocore.exceptions import ClientError

DEFAULT_PREFIX = "bloggerbear"


class RepairError(Exception):
    """Something about the request or the table means nothing should be written."""


def _monday(text: str) -> date:
    try:
        day = date.fromisoformat(text)
    except ValueError as exc:
        raise RepairError(f"{text!r} is not a date like 2026-09-28.") from exc
    if day.weekday() != 0:
        raise RepairError(f"{text} is not a Monday: a week's row is keyed by its Monday.")
    return day


def plan(table, missing_week: str, today: date | None = None) -> dict:
    """What would be written, after checking the table is in the state this mends. Reads only."""
    missing = _monday(missing_week)
    today = today or datetime.now(UTC).date()
    if missing + timedelta(days=7) > today:
        raise RepairError(f"The week of {missing_week} has not ended yet: there is nothing to mend.")
    if table.get_item(Key={"week_start": missing_week}).get("Item"):
        raise RepairError(f"The week of {missing_week} already has a row. Nothing to do.")
    earlier_week = (missing - timedelta(days=7)).isoformat()
    earlier = table.get_item(Key={"week_start": earlier_week}).get("Item")
    if not earlier:
        raise RepairError(
            f"There is no row for {earlier_week}, the week before. This only mends a week whose "
            "counters went into the week before it."
        )
    rolled_over = str(earlier.get("rolled_over_at", ""))[:10]
    if not rolled_over or rolled_over < (missing + timedelta(days=7)).isoformat():
        raise RepairError(
            f"The {earlier_week} row was rolled over on {rolled_over or 'an unknown day'}, before "
            f"the week of {missing_week} ended, so it cannot hold that week's counters."
        )
    return {"missing_week": missing_week, "earlier_week": earlier_week, "covers_through": rolled_over}


def apply(table, planned: dict, now: datetime | None = None) -> None:
    """The two writes. Each is conditional, so a second run changes nothing."""
    stamp = (now or datetime.now(UTC)).isoformat()
    missing, earlier = planned["missing_week"], planned["earlier_week"]
    try:
        table.put_item(
            Item={
                "week_start": missing,
                "backfilled_at": stamp,
                "counters_in_week": earlier,
                "note": (
                    f"Placeholder made by scripts/repair_stats_week.py. The rollover did not run "
                    f"for this week, so its counters are in the {earlier} row. This row exists so "
                    "the cost poll can record this week's AWS bill."
                ),
            },
            ConditionExpression="attribute_not_exists(week_start)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
    table.update_item(
        Key={"week_start": earlier},
        UpdateExpression="SET covers_through = :through, #note = :note",
        ConditionExpression="attribute_exists(week_start)",
        ExpressionAttributeNames={"#note": "note"},
        ExpressionAttributeValues={
            ":through": planned["covers_through"],
            ":note": (
                f"Covers two weeks: the rollover did not run for the week of {missing}, so the "
                f"counters here run from {earlier} to the rollover on {planned['covers_through']}. "
                f"The AWS bill on this row is for the week of {earlier} only. Noted {stamp}."
            ),
        },
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--env", required=True, choices=["dev", "production"])
    parser.add_argument("--missing-week", required=True, help="the Monday of the week with no row")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="your UNIQUE_NAME_PREFIX")
    parser.add_argument("--region", help="your AWS_REGION; unset, the AWS CLI's own default")
    parser.add_argument("--apply", action="store_true", help="write; without it, only show")
    args = parser.parse_args(argv)

    name = f"{args.prefix}-{args.env}-stats-history"
    table = boto3.resource("dynamodb", region_name=args.region).Table(name)
    try:
        planned = plan(table, args.missing_week)
    except RepairError as exc:
        print(f"Nothing written. {exc}")
        return 1
    print(f"Table: {name}")
    print(f"  1. Make a placeholder row for the week of {planned['missing_week']} (no counters).")
    print(
        f"  2. Note on the {planned['earlier_week']} row that its counters run to "
        f"{planned['covers_through']}."
    )
    if not args.apply:
        print("Nothing written: this was a dry run. Add --apply to write.")
        return 0
    apply(table, planned)
    print("Written. The next daily cost poll fills in the placeholder's AWS bill and the all-time bill.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
