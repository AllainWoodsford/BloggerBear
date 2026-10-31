"""GET /evals/status's two prompt parts (common/evals_status.py; docs/enhancements/
model-ranch-prompt-assets-evals.md, PR13 and PR14, where prompts as assets meet evals): the prompt
release in place, from the publisher's releases/current.json, and the prompt fallbacks of the last
24 hours, from the observability module's PromptFallback metric.

What is held: each part answers, read through prompts.CURRENT_RELEASE_FIELD and
PINNED_RELEASE_FIELD and the metric the module's filter feeds; each is "off", reading nothing, while
its deployment has no prompts bucket or does not read assets; and each that cannot be read says
"could not check" while the other parts still answer.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import pytest
import test_admin_evals as admin_evals
from test_admin_evals import REGION, call

from common import evals_status, prompts

ROOT = Path(__file__).resolve().parents[2]
BUCKET = "acme-dev-prompts"
NOW = datetime(2026, 10, 31, 2, 0, tzinfo=UTC)
RELEASE = "dev-20261030T0101-abc1234"
EARLIER = "dev-20261029T2310-def5678"

# The admin evals tests' fixtures, under their own names: the moto tables with evals on, and the
# SQS and CloudWatch clients status reads.
aws = admin_evals.aws
queue = admin_evals.queue


@pytest.fixture
def assets(aws, monkeypatch):
    """A deployment that reads prompt assets, with its (empty) prompts bucket."""
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "true")
    monkeypatch.setenv("PROMPTS_BUCKET", BUCKET)
    monkeypatch.setenv("PROMPT_HASHES_TABLE", "acme-dev-prompt-hashes")
    prompts._reset()
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION})
    yield s3
    prompts._reset()


def _current(s3, current: dict) -> None:
    s3.put_object(Bucket=BUCKET, Key=prompts.CURRENT_RELEASE_KEY, Body=json.dumps(current).encode("utf-8"))


def _fallbacks(cloudwatch, datapoints):
    """The fallback metric answers with `datapoints`; the queues' age as the queue fixture does."""
    queues = cloudwatch.get_metric_statistics.return_value

    def answer(**kwargs):
        return {"Datapoints": datapoints} if kwargs["MetricName"] == evals_status.FALLBACK_METRIC else queues

    cloudwatch.get_metric_statistics.side_effect = answer


def _fallback_calls(cloudwatch) -> list[dict]:
    return [
        found.kwargs
        for found in cloudwatch.get_metric_statistics.call_args_list
        if found.kwargs["MetricName"] == evals_status.FALLBACK_METRIC
    ]


# --- the release ----------------------------------------------------------------------------------


def test_the_release_part_is_the_release_in_place_and_the_one_a_rollback_holds_to(assets, queue):
    _current(assets, {prompts.CURRENT_RELEASE_FIELD: RELEASE, prompts.PINNED_RELEASE_FIELD: EARLIER})

    status, body = call("GET /evals/status")

    assert status == 200
    assert body["prompt_release"] == {
        "bucket": BUCKET, "assets_enabled": True, "current": RELEASE, "pinned": EARLIER,
    }  # fmt: skip


def test_with_no_rollback_held_the_pinned_release_is_none(assets, queue):
    _current(assets, {prompts.CURRENT_RELEASE_FIELD: RELEASE, prompts.PINNED_RELEASE_FIELD: None})

    _, body = call("GET /evals/status")

    assert body["prompt_release"]["current"] == RELEASE and body["prompt_release"]["pinned"] is None


def test_before_the_first_publish_the_release_part_says_nothing_is_published(assets, queue):
    _, body = call("GET /evals/status")

    assert body["prompt_release"] == {
        "bucket": BUCKET, "assets_enabled": True, "current": None, "pinned": None,
        "detail": "nothing has been published yet",
    }  # fmt: skip


def test_a_deployment_that_reads_no_assets_still_shows_its_bucket_s_release(assets, queue, monkeypatch):
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "false")
    _current(assets, {prompts.CURRENT_RELEASE_FIELD: RELEASE, prompts.PINNED_RELEASE_FIELD: None})

    _, body = call("GET /evals/status")

    assert body["prompt_release"]["assets_enabled"] is False and body["prompt_release"]["current"] == RELEASE


def test_with_no_prompts_bucket_the_release_part_is_off_and_reads_nothing(aws, queue, monkeypatch):
    monkeypatch.setenv("PROMPTS_BUCKET", "")
    monkeypatch.setattr(prompts, "current_release", lambda: pytest.fail("current.json is not read"))

    _, body = call("GET /evals/status")

    assert body["prompt_release"] == {"status": "off", "detail": evals_status.PROMPTS_OFF["release"]}


def test_a_release_that_cannot_be_read_could_not_be_checked_and_the_other_parts_answer(assets, queue, capsys):
    _fallbacks(queue[1], [])
    assets.put_object(Bucket=BUCKET, Key=prompts.CURRENT_RELEASE_KEY, Body=b"[not, an, object")

    _, body = call("GET /evals/status")

    assert body["prompt_release"] == {"status": "could not check", "error": "JSONDecodeError"}
    assert body["prompt_fallbacks"]["last_24_hours"] == 0
    assert body["switches"]["evals_enabled"] is True and "gated" in body["gated_models"]
    assert "evals_status: could not check prompt_release: JSONDecodeError(" in capsys.readouterr().out


# --- the fallbacks --------------------------------------------------------------------------------


def test_the_fallbacks_part_adds_up_the_last_24_hours_of_the_fallback_metric(assets, queue, monkeypatch):
    _fallbacks(
        queue[1],
        [
            {"Timestamp": NOW - timedelta(hours=5), "Sum": 3.0},
            {"Timestamp": NOW - timedelta(hours=2), "Sum": 1.0},
            {"Timestamp": NOW - timedelta(hours=1), "Sum": 0.0},
        ],
    )
    monkeypatch.setattr(evals_status, "datetime", _Frozen)

    _, body = call("GET /evals/status")

    assert body["prompt_fallbacks"] == {
        "last_24_hours": 4,
        "hours_with_fallbacks": 2,
        "latest_hour": (NOW - timedelta(hours=2)).isoformat(),
        "detail": evals_status.FALLBACK_DETAIL,
    }
    (asked,) = _fallback_calls(queue[1])
    assert asked == {
        "Namespace": evals_status.FALLBACK_NAMESPACE.format(environment="dev"),
        "MetricName": "PromptFallback",
        "StartTime": NOW - timedelta(hours=24),
        "EndTime": NOW,
        "Period": 3600,
        "Statistics": ["Sum"],
    }


def test_a_quiet_day_is_no_fallback_at_all(assets, queue):
    _fallbacks(queue[1], [])

    _, body = call("GET /evals/status")

    assert body["prompt_fallbacks"] == {
        "last_24_hours": 0, "hours_with_fallbacks": 0, "latest_hour": None,
        "detail": evals_status.FALLBACK_DETAIL,
    }  # fmt: skip


def test_while_assets_are_off_the_fallbacks_part_is_off_and_asks_cloudwatch_nothing(aws, queue, monkeypatch):
    monkeypatch.setenv("PROMPT_AS_ASSET_ENABLED", "false")

    _, body = call("GET /evals/status")

    assert body["prompt_fallbacks"] == {"status": "off", "detail": evals_status.PROMPTS_OFF["fallbacks"]}
    assert _fallback_calls(queue[1]) == []


def test_fallbacks_that_cannot_be_read_could_not_be_checked_and_the_other_parts_answer(assets, queue):
    queues = queue[1].get_metric_statistics.return_value

    def answer(**kwargs):
        if kwargs["MetricName"] == evals_status.FALLBACK_METRIC:
            raise RuntimeError("access denied")
        return queues

    queue[1].get_metric_statistics.side_effect = answer
    _current(assets, {prompts.CURRENT_RELEASE_FIELD: RELEASE, prompts.PINNED_RELEASE_FIELD: None})

    _, body = call("GET /evals/status")

    assert body["prompt_fallbacks"] == {"status": "could not check", "error": "RuntimeError"}
    assert body["prompt_release"]["current"] == RELEASE
    assert body["queue_age"]["eval-work"]["oldest_seconds"] == 900


def test_the_metric_read_is_the_one_the_observability_modules_filter_feeds():
    """The namespace and the metric's name are the prompt_fallback filter's, the environment filled
    in the same way: a rename on either side would leave this part reading a metric nothing writes."""
    module = (ROOT / "infra" / "modules" / "observability" / "main.tf").read_text(encoding="utf-8")
    start = r'resource "aws_cloudwatch_log_metric_filter" "prompt_fallback" \{'
    block = re.search(start + r".*?\n\}", module, re.S)
    assert block, "the observability module's prompt_fallback filter"
    namespace = evals_status.FALLBACK_NAMESPACE.format(environment="${var.environment_name}")
    assert re.search(rf'name\s+=\s+"{evals_status.FALLBACK_METRIC}"', block.group(0))
    assert re.search(rf'namespace\s+=\s+"{re.escape(namespace)}"', block.group(0))
    assert re.search(r'pattern\s+=\s+"\{ \$\.event = \\"prompt_fallback\\" \}"', block.group(0))


class _Frozen(datetime):
    """evals_status's clock, stopped at NOW, so the window asked for is exact."""

    @classmethod
    def now(cls, tz=None):
        return NOW if tz is None else NOW.astimezone(tz)
