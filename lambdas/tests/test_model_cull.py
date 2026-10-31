"""Culling in proposal mode (docs/enhancements/model-ranch-prompt-assets-evals.md, PR16): the weekly
cycle's proposal (common/model_fleet.cull_proposal), applying it (POST /evals/cull/apply,
common/model_cull.apply), putting a model back (POST /models/{model_id}/restore,
common/model_cull.restore), the `cull` part of GET /evals/status, and the routes' place in both
environments' Terraform.

What is held: a model is proposed only while its total is below the total gate with at least
min_events events behind it, it plays no role as the roles stand now (never as its stored
model_roles say), its price is not locked and no ungated_until holds it open; the protected models
never are, whatever their scores. The proposal is written to model-catalog-state with the time of
the next cycle, deletes nothing, and is not made while evals are off. An apply is refused while
culling is off, while evals are off, without a proposal and once it has expired; it culls at most
cull_max_per_run, worst first, each checked again against the live rules, archiving then deleting
(so neither discover nor the rollup brings one back), and records itself for `evals status`. A
restore puts the archived row back, deletes the archive entry, writes an Admin Forgive that brings
the total to zero and holds the gates open for 7 days, so the model is not proposed again at the
next cycle; it finds the row it put back although an eventually consistent read would not, holds
open a row it missed, and is refused before anything is forgiven when the row goes meanwhile.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
import test_admin_evals
from test_admin_evals import ALPHA, AT, Clock, adjust_rows, call, events_of, model_row, rolled_up
from test_evals_rollup import completion, sydney

import admin_api_handler
from common import (
    dynamo,
    evals_adjust,
    evals_rollup,
    evals_settings,
    evals_status,
    evidence,
    model_catalog,
    model_fleet,
)

# The admin evals tests' fixtures, under their own names: the tables with evals on (`aws`), the evals
# log the admin API writes and the rollup reads at Clock's time (`logs`), and the queues (`queue`).
aws, logs, queue = test_admin_evals.aws, test_admin_evals.logs, test_admin_evals.queue
SETTINGS = evals_settings.effective_settings(None)  # total gate -1000, min_events 10, cap 3
POOR = "maker.poor-v1:0"
WORSE = "maker.worse-v1:0"
WORST = "maker.worst-v1:0"
BAD = "maker.bad-v1:0"
FINE = "maker.fine-v1:0"
# AT is Tuesday 27 October 2026, 14:00 in Sydney: the next weekly cycle is Friday 04:00 there.
NEXT_CYCLE = sydney("2026-10-30T04:00")
CULL_ON = {"config_id": "evals", "cull_enabled": True}


@pytest.fixture
def evals_on(aws):
    """The aws fixture has evals on; these tests say so where they rely on it."""
    return aws


def put_scored(aws, model_id, total=-1200, events=40, **fields):
    """A registry row as the rollup leaves it: `total` over `events` events, nothing in the windows."""
    aws.Table("Models").put_item(
        Item={
            "model_id": model_id, "display_name": model_id.split(".")[1], "provider": "maker",
            "model_purpose": "text", "model_inputs": ["TEXT"], "enabled": True, "region": "ap-southeast-2",
            "input_price_usd_per_1k_tokens": Decimal("0.001"),
            "output_price_usd_per_1k_tokens": Decimal("0.004"),
            "daily_perf": 0, "weekly_perf": 0, "monthly_perf": 0, "total_perf": total,
            "perf_events": {"daily": 0, "weekly": 0, "monthly": 0, "total": events},
            "model_roles": [], **fields,
        }
    )  # fmt: skip


def poor_history(aws, model_id, perf=-1200, events=40):
    """A day row of Article Rewrites (a reason that gates) long before the rollup's watermark, as an
    earlier rollup left it, so every later rollup counts it in the model's total and leaves it be."""
    aws.Table("Evals").put_item(
        Item={
            "entity": f"model#{model_id}", "sk": "day#2026-10-20", "perf": perf, "events": events,
            "good": 0, "bad": events,
            "by_reason": {"Article Rewrite": {"perf": perf, "events": events, "good": 0, "bad": events}},
        }
    )  # fmt: skip
    aws.Table("Evals").put_item(Item={**evals_rollup.INDEX_KEY, "last_rolled_day": "2026-10-26"})


def propose(now=AT, settings=None):
    return model_fleet.cull_proposal(settings or evals_settings.get_settings(), now)


def state() -> dict:
    return model_catalog.get_state()


def proposed_ids() -> list[str]:
    return [entry["model_id"] for entry in state()["cull_proposal"]["models"]]


def registry() -> list[str]:
    return sorted(model["model_id"] for model in dynamo.list_models())


def cull_lines(capsys) -> list[dict]:
    lines = [evidence.parse(line) for line in capsys.readouterr().out.splitlines()]
    return [line for line in lines if line and line["event"] == "model_cull"]


# --- the proposal ---------------------------------------------------------------------------------


def test_a_model_below_the_total_gate_with_enough_events_and_no_role_is_proposed_worst_first(
    evals_on, capsys
):
    put_scored(evals_on, POOR, total=-1200)
    put_scored(evals_on, WORST, total=-5000)
    put_scored(evals_on, WORSE, total=-2000, events=10)
    put_scored(evals_on, FINE, total=-999)

    result = propose()

    assert (result["status"], result["proposed"]) == ("proposed", 3)
    assert result["proposed_ids"] == [WORST, WORSE, POOR]
    proposal = state()["cull_proposal"]
    assert proposal["models"][0] == {
        "model_id": WORST, "total_perf": -5000, "events": 40,
        "reason": "its total (-5000) is below the total gate (-1000) over 40 events, and it plays no role",
    }  # fmt: skip
    assert [entry["total_perf"] for entry in proposal["models"]] == [-5000, -2000, -1200]
    assert proposal["proposed_at"] == AT.isoformat()
    assert proposal["expires_at"] == result["expires_at"] == NEXT_CYCLE.isoformat()
    assert (proposal["total_gate"], proposal["min_events"]) == (-1000, 10)
    # A proposal deletes nothing.
    assert registry() == sorted([ALPHA, POOR, WORST, WORSE, FINE])
    [line] = cull_lines(capsys)
    assert line.items() >= {"job": "propose", "status": "proposed", "models": 5, "proposed": 3}.items()
    assert POOR not in json.dumps(line)


def test_a_cycle_that_finds_nothing_to_cull_still_writes_an_empty_proposal_that_expires(evals_on):
    put_scored(evals_on, FINE, total=-10)

    assert propose()["proposed"] == 0
    assert state()["cull_proposal"]["models"] == []
    assert state()["cull_proposal"]["expires_at"] == NEXT_CYCLE.isoformat()


ROLES = {
    "the default row's model": ("model", lambda aws: config(aws, model_id=POOR)),
    "BEDROCK_MODEL_ID": ("model", lambda aws: None),
    "the fallback": ("model", lambda aws: config(aws, fallback_model_id=POOR)),
    "a topic's model": ("model", lambda aws: topic(aws, model_id=POOR)),
    "a topic's fallback": ("model", lambda aws: topic(aws, fallback_model_id=POOR)),
    "a topic's rotation": ("model", lambda aws: topic(aws, model_id_candidates=[FINE, POOR])),
    "the shared pool": ("model", lambda aws: pool(aws, [POOR])),
    "the judge": ("model", lambda aws: aws.Table("ModelConfig").put_item(
        Item={"config_id": "evals", "judge_model_id": POOR}
    )),
    "a locked price": ("price_locked", lambda aws: None),
}  # fmt: skip


def config(aws, **fields):
    aws.Table("ModelConfig").put_item(Item={"config_id": "default", "model_id": None, **fields})


def topic(aws, **fields):
    aws.Table("Topics").put_item(Item={"topic_id": "t1", "name": "t1", **fields})


def pool(aws, candidates):
    aws.Table("ModelConfig").put_item(Item={"config_id": "model-rotation", "candidates": candidates})


@pytest.mark.parametrize("role", sorted(ROLES))
def test_protected_models_are_never_proposed_whatever_their_scores(evals_on, monkeypatch, role):
    """The roles as they stand now (model_fleet.cull_roles): the default, the fallback, a topic's
    model, fallback and rotation, the shared pool, the judge and a locked price."""
    kind, give_role = ROLES[role]
    if role == "BEDROCK_MODEL_ID":
        monkeypatch.setenv("BEDROCK_MODEL_ID", POOR)
    put_scored(evals_on, POOR, total=-100_000, **({"price_locked": True} if kind == "price_locked" else {}))
    put_scored(evals_on, BAD, total=-100_000)
    give_role(evals_on)

    propose()

    assert proposed_ids() == [BAD]


def test_the_stored_model_roles_are_not_what_protects_a_model(evals_on):
    """Deviation 7: model_roles on the row are written for display and drift; culling derives them."""
    put_scored(evals_on, POOR, model_roles=["rotation"])  # no topic holds it any longer
    put_scored(evals_on, BAD, model_roles=[])  # a topic took it since the last rollup
    topic(evals_on, model_id_candidates=[BAD])

    propose()

    assert proposed_ids() == [POOR]


@pytest.mark.parametrize(
    ("fields", "why"),
    [
        ({"ungated_until": "2026-11-01T00:00:00Z"}, "held open by an operator"),
        ({"ungated_until": "soon"}, "an ungated_until that cannot be read holds it open"),
        ({"perf_events": {"total": 9}}, "one event short of min_events"),
        ({"total_perf": -1000}, "at the gate, not below it"),
        ({"total_perf": "lots"}, "a total that is not a number"),
        ({"perf_events": None}, "no events counted yet"),
    ],
)
def test_a_model_held_open_or_short_of_events_or_not_below_the_gate_is_not_proposed(evals_on, fields, why):
    put_scored(evals_on, POOR, **fields)
    put_scored(evals_on, BAD)

    propose()

    assert proposed_ids() == [BAD], why


def test_nothing_is_proposed_while_evals_are_off_and_the_last_proposal_stands_until_it_expires(
    evals_on, monkeypatch
):
    put_scored(evals_on, POOR)
    propose()
    monkeypatch.setenv("EVALS_ENABLED", "false")

    assert propose(now=NEXT_CYCLE)["status"] == "off"
    assert proposed_ids() == [POOR]
    assert state()["cull_proposal"]["expires_at"] == NEXT_CYCLE.isoformat()


def test_a_proposal_expires_at_the_next_cycle_fridays_at_four_in_sydney():
    assert model_fleet.next_cycle_at(AT) == NEXT_CYCLE
    # At the cycle's own time the next is a week on; just before it, that same morning.
    assert model_fleet.next_cycle_at(NEXT_CYCLE) == sydney("2026-11-06T04:00")
    assert model_fleet.next_cycle_at(NEXT_CYCLE - timedelta(seconds=1)) == NEXT_CYCLE
    # Across the end of daylight saving (Sunday 5 April 2026): 04:00 in Sydney either side of it.
    assert model_fleet.next_cycle_at(sydney("2026-04-03T05:00")) == datetime(2026, 4, 9, 18, 0, tzinfo=UTC)
    assert model_fleet.next_cycle_at(sydney("2026-04-02T12:00")) == datetime(2026, 4, 2, 17, 0, tzinfo=UTC)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_the_cycles_time_is_the_one_its_schedule_runs_at(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    block = main[main.index('resource "aws_scheduler_schedule" "model_catalog_cycle"') :].split("\n}\n", 1)[0]
    weekday = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")[model_fleet.CYCLE_WEEKDAY]
    assert f'schedule_expression          = "cron(0 {model_fleet.CYCLE_HOUR} ? * {weekday} *)"' in block
    assert f'schedule_expression_timezone = "{model_fleet.CYCLE_ZONE}"' in block
    assert model_catalog.STATE_ID == model_fleet.STATE_ID == "model-catalog-state"


def test_the_weekly_cycle_proposes_after_refreshing_the_rotations(evals_on):
    """A disabled model leaves the rotations in the cycle; with no role left, it is proposed."""
    put_scored(evals_on, POOR, enabled=False)
    put_scored(evals_on, FINE, total=0)
    topic(evals_on, model_id_candidates=[FINE, POOR])

    result = model_catalog.handle({"job": "cycle", "trigger": "schedule"}, AT)

    assert result["status"] == "ran" and result["cycle"]["dropped_inactive"] == 1
    assert result["cycle"]["cull"]["proposed_ids"] == [POOR]
    assert state()["last_cycle_result"]["cull"]["status"] == "proposed"
    assert proposed_ids() == [POOR]


def test_the_weekly_cycle_proposes_under_the_gates_the_evals_row_stores_not_the_defaults(evals_on):
    """Section 4.5: total_gate and min_events are set in DynamoDB only; the cycle reads them there."""
    evals_on.Table("ModelConfig").put_item(Item={"config_id": "evals", "total_gate": -500, "min_events": 50})
    put_scored(evals_on, POOR, total=-600, events=50)  # below the stored gate, above the default one
    put_scored(evals_on, BAD, total=-1200, events=40)  # enough events for the default, not the stored 50

    result = model_catalog.handle({"job": "cycle", "trigger": "schedule"}, AT)

    assert result["cycle"]["cull"]["proposed_ids"] == [POOR]
    assert proposed_ids() == [POOR]
    assert (state()["cull_proposal"]["total_gate"], state()["cull_proposal"]["min_events"]) == (-500, 50)


def test_a_proposal_that_fails_leaves_the_cycles_rotations_done(evals_on, capsys):
    put_scored(evals_on, POOR, enabled=False)
    topic(evals_on, model_id_candidates=[POOR])

    with patch.object(model_fleet, "cull_proposal", side_effect=RuntimeError("no\nrow")):
        result = model_catalog.handle({"job": "cycle", "trigger": "schedule"}, AT)

    assert result["cycle"]["cull"] == {"status": "error", "error": "RuntimeError"}
    assert result["cycle"]["dropped_inactive"] == 1
    said = "model_catalog: could not propose models to cull: RuntimeError('no\\nrow')"
    assert said in capsys.readouterr().out


# --- applying the proposal ------------------------------------------------------------------------


@pytest.fixture
def proposed(evals_on, logs):
    """Four models proposed this week, worst first: WORST, WORSE, BAD, POOR."""
    for model_id, total in ((POOR, -1100), (BAD, -1500), (WORSE, -2000), (WORST, -5000)):
        put_scored(evals_on, model_id, total=total)
    propose()
    assert proposed_ids() == [WORST, WORSE, BAD, POOR]
    return evals_on


def cull_on(aws, **fields):
    aws.Table("ModelConfig").put_item(Item={**CULL_ON, **fields})


def test_apply_is_refused_while_culling_is_off_and_deletes_nothing(proposed):
    status, body = call("POST /evals/cull/apply")

    assert status == 409
    assert body["error"].startswith("culling is off (cull_enabled is false): nothing was archived or deleted")
    assert "`evals set --cull on`" in body["error"]
    assert registry() == sorted([ALPHA, POOR, BAD, WORSE, WORST])
    assert "last_cull_apply" not in state() and dynamo.get_model_archive() == {}


def test_apply_is_refused_while_evals_are_off(proposed, monkeypatch):
    cull_on(proposed)
    monkeypatch.setenv("EVALS_ENABLED", "false")

    status, body = call("POST /evals/cull/apply")

    assert status == 409 and body["error"].startswith("evals are off (EVALS_ENABLED)")
    assert len(registry()) == 5


def test_apply_is_refused_without_a_proposal(evals_on, logs):
    cull_on(evals_on)
    put_scored(evals_on, POOR)

    status, body = call("POST /evals/cull/apply")

    assert status == 409 and body["error"].startswith("there is no cull proposal")
    assert POOR in registry()


def test_apply_is_refused_once_the_proposal_has_expired_at_the_next_cycle(proposed, monkeypatch):
    cull_on(proposed)
    monkeypatch.setattr(Clock, "at", NEXT_CYCLE)

    status, body = call("POST /evals/cull/apply")

    assert status == 409
    assert body["error"] == (
        f"the cull proposal made {AT.isoformat()} expired at {NEXT_CYCLE.isoformat()}: the next weekly "
        "cycle proposes again"
    )
    assert len(registry()) == 5


def test_apply_culls_at_most_cull_max_per_run_worst_first_archiving_then_deleting(proposed, capsys):
    cull_on(proposed)
    capsys.readouterr()

    status, body = call("POST /evals/cull/apply")

    assert status == 200
    assert body["culled_ids"] == [WORST, WORSE, BAD] and body["left"] == [POOR] and body["kept"] == []
    assert (body["cap"], body["applied_at"], body["proposed_at"]) == (3, AT.isoformat(), AT.isoformat())
    assert registry() == sorted([ALPHA, POOR])
    archived = dynamo.get_model_archive()
    assert set(archived) == {WORST, WORSE, BAD}
    # The prune path's entry, and the row for a restore, less the rollup's scores.
    entry = archived[WORST]
    assert entry.items() >= {
        "display_name": "worst-v1:0", "provider": "maker", "input_price_usd_per_1k_tokens": 0.001,
        "output_price_usd_per_1k_tokens": 0.004, "pruned_at": AT.isoformat(), "culled": True,
    }.items()  # fmt: skip
    assert entry["row"]["model_purpose"] == "text" and entry["row"]["enabled"] is True
    assert not set(entry["row"]) & set(model_fleet.SCORE_FIELDS)
    assert state()["last_cull_apply"] == {
        "applied_at": AT.isoformat(), "proposed_at": AT.isoformat(), "culled_ids": [WORST, WORSE, BAD],
        "kept": [], "left": [POOR],
    }  # fmt: skip
    [line] = cull_lines(capsys)
    assert line.items() >= {
        "job": "apply", "status": "applied", "proposed": 4, "culled": 3, "kept": 0, "left": 1, "cap": 3,
    }.items()  # fmt: skip

    # The proposal stands: a second apply takes the one left, the others being gone already.
    status, body = call("POST /evals/cull/apply")
    assert status == 200 and body["culled_ids"] == [POOR] and body["left"] == []
    assert [kept["model_id"] for kept in body["kept"]] == [WORST, WORSE, BAD]
    assert {kept["reason"] for kept in body["kept"]} == {"it is no longer in the registry"}
    assert registry() == [ALPHA]


def test_apply_takes_cull_max_per_run_from_the_settings(proposed):
    cull_on(proposed, cull_max_per_run=1)

    _, body = call("POST /evals/cull/apply")

    assert body["culled_ids"] == [WORST] and body["left"] == [WORSE, BAD, POOR]


def test_apply_checks_each_model_again_and_keeps_one_that_no_longer_qualifies(proposed):
    """A model given a role, a locked price or an override, or that recovered, since the proposal."""
    cull_on(proposed, cull_max_per_run=50)
    topic(proposed, model_id_candidates=[WORST])
    dynamo.set_model_fields(WORSE, {"price_locked": True}, expected={})
    dynamo.set_model_fields(BAD, {"total_perf": -10}, expected={})
    dynamo.set_model_fields(POOR, {"ungated_until": "2026-10-28T00:00:00Z"}, expected={})

    status, body = call("POST /evals/cull/apply")

    assert status == 200 and body["culled_ids"] == []
    assert {kept["model_id"]: kept["reason"] for kept in body["kept"]} == {
        WORST: "it plays a role: rotation",
        WORSE: "it plays a role: price_locked",
        BAD: "its total (-10) is not below the total gate (-1000)",
        POOR: "its gates are held open until 2026-10-28T00:00:00Z",
    }
    assert len(registry()) == 5 and dynamo.get_model_archive() == {}


def test_a_culled_model_is_never_brought_back_by_discover_or_the_rollup(proposed, logs, monkeypatch):
    """Deviation 8: archived first, so discover passes it over; the rollup only updates rows there."""
    cull_on(proposed)
    call("POST /evals/cull/apply")
    found = [{"model_id": WORST, "display_name": "w", "provider": "maker", "pricing_unit": "tokens"}]
    monkeypatch.setattr(model_catalog, "account_models", lambda region, client=None: found)
    completion(logs, "a1", "daily_cycle/draft", sydney("2026-10-27T09:00"), model=WORST)

    settings = model_catalog.effective_settings(None)
    discovered = model_catalog.discover("schedule", settings, AT, probe=lambda model_id: ("granted", None))
    rolled_up(logs)

    assert discovered["added"] == 0 and model_row(proposed, WORST) is None


# --- restoring a model ----------------------------------------------------------------------------


def culled(aws, logs, model_id=POOR):
    """`model_id` with a total of -1200 over 40 events, proposed and culled."""
    put_scored(aws, model_id, total=0, events=0)
    poor_history(aws, model_id)
    rolled_up(logs)
    assert dynamo.get_model(model_id)["total_perf"] == -1200
    propose()
    cull_on(aws)
    assert call("POST /evals/cull/apply")[1]["culled_ids"] == [model_id]


def test_restore_puts_the_row_back_forgives_the_total_to_zero_and_holds_the_gates_open(
    evals_on, logs, capsys
):
    culled(evals_on, logs)
    capsys.readouterr()

    status, body = route_restore(POOR)

    assert status == 200
    assert (body["model_id"], body["restored"], body["was_culled"]) == (POOR, True, True) and body["enabled"]
    until = (AT + timedelta(days=7)).isoformat(timespec="seconds")
    assert body["ungated_until"] == until and body["total_perf_before"] == -1200
    # The forgive is PR13b's own (evals_adjust.adjust), over 100 and so sent with allow_cull.
    forgive = body["forgive"]
    assert (forgive["reason"], forgive["delta"], forgive["total_perf"]) == ("Admin Forgive", 1200, 0)
    assert adjust_rows(evals_on) == [
        {
            "entity": f"adjust#model#{POOR}", "sk": f"ts#{forgive['ts']}", "delta": 1200,
            "reason": "Admin Forgive", "by": "human",
        }
    ]  # fmt: skip
    [event] = events_of(logs, "Admin Forgive")
    assert (event["model_id"], event["rating_delta"], event["by"]) == (POOR, 1200, "human")
    row = dynamo.get_model(POOR)
    assert row["ungated_until"] == until and row["model_purpose"] == "text" and row["enabled"] is True
    assert row["input_price_usd_per_1k_tokens"] == 0.001 and "total_perf" not in row
    assert POOR not in dynamo.get_model_archive()
    [line] = cull_lines(capsys)
    restored = {"job": "restore", "status": "restored", "model_id": POOR, "forgiven": 1200}
    assert line.items() >= restored.items()
    # The next rollup puts the forgiven total, zero, on the row.
    rolled_up(logs)
    assert (dynamo.get_model(POOR)["total_perf"], dynamo.get_model(POOR)["perf_events"]["total"]) == (0, 40)


def test_a_restored_model_is_not_proposed_again_at_the_next_cycle(evals_on, logs, monkeypatch):
    culled(evals_on, logs)
    route_restore(POOR)
    rolled_up(logs)

    # Its total is zero: nothing to cull at the next cycle.
    propose(now=NEXT_CYCLE)
    assert proposed_ids() == []

    # Punished below the gate again, ungated_until still holds it for the 7 days...
    monkeypatch.setattr(Clock, "at", AT + timedelta(minutes=5))
    punish = {"target": "model", "model_id": POOR, "direction": "punish", "amount": 1500, "allow_cull": True}
    assert call("POST /evals/adjust", punish)[0] == 201
    rolled_up(logs)
    assert dynamo.get_model(POOR)["total_perf"] == -1500
    propose(now=NEXT_CYCLE)
    assert proposed_ids() == []
    # ...and once they are over, the rules are the rules.
    propose(now=AT + timedelta(days=7, seconds=1))
    assert proposed_ids() == [POOR]


def test_restore_of_a_model_not_in_the_archive_or_not_a_model_is_refused(evals_on, logs):
    status, body = route_restore(ALPHA)
    assert status == 404 and body["error"].startswith(f"no model '{ALPHA}' in the archive")
    assert route_restore("not a model") == (400, {"error": "'model_id' must be a model id"})


def test_restore_is_refused_while_evals_are_off_and_writes_nothing(evals_on, logs, monkeypatch):
    culled(evals_on, logs)
    monkeypatch.setenv("EVALS_ENABLED", "false")

    status, body = route_restore(POOR)

    assert status == 409 and body["error"].startswith("evals are off (EVALS_ENABLED)")
    assert POOR not in registry() and POOR in dynamo.get_model_archive() and adjust_rows(evals_on) == []


def test_a_pruned_model_comes_back_disabled_with_its_price_and_no_forgive_while_its_total_is_not_below_zero(
    evals_on, logs
):
    dynamo.archive_models(
        {
            FINE: {
                "display_name": "Fine", "provider": "maker", "input_price_usd_per_1k_tokens": 0.002,
                "output_price_usd_per_1k_tokens": None, "pruned_at": "2026-10-01T00:00:00+00:00",
            }
        }
    )  # fmt: skip

    status, body = route_restore(FINE)

    assert status == 200 and body["forgive"] is None and body["total_perf_before"] == 0
    assert (body["restored"], body["was_culled"], body["enabled"]) == (True, False, False)
    row = dynamo.get_model(FINE)
    assert {key: row[key] for key in row if key != "ungated_until"} == {
        "model_id": FINE, "display_name": "Fine", "provider": "maker", "input_price_usd_per_1k_tokens": 0.002,
        "enabled": False,
    }  # fmt: skip
    assert adjust_rows(evals_on) == [] and dynamo.get_model_archive() == {}


def test_a_model_put_back_by_hand_is_forgiven_and_held_open_and_its_row_left_as_it_is(evals_on, logs):
    culled(evals_on, logs)
    put_scored(evals_on, POOR, total=-1200, display_name="By hand")

    status, body = route_restore(POOR)

    assert status == 200 and body["restored"] is False and body["forgive"]["delta"] == 1200
    row = dynamo.get_model(POOR)
    assert row["display_name"] == "By hand" and row["ungated_until"] == body["ungated_until"]
    assert POOR not in dynamo.get_model_archive()
    # Restored once: a second restore finds nothing in the archive, and forgives nothing more.
    assert route_restore(POOR)[0] == 404 and len(adjust_rows(evals_on)) == 1


def test_a_restore_that_fails_after_putting_the_row_back_can_be_sent_again_without_a_second_forgive(
    evals_on, logs
):
    culled(evals_on, logs)
    with patch.object(dynamo, "unarchive_model", side_effect=RuntimeError("throttled")):
        assert route_restore(POOR)[0] == 500

    status, body = route_restore(POOR)

    assert status == 200 and body["restored"] is False and body["forgive"] is None
    assert body["total_perf_before"] == 0 and len(adjust_rows(evals_on)) == 1
    assert POOR not in dynamo.get_model_archive()


def test_a_restore_forgives_and_unarchives_although_an_eventually_consistent_read_misses_the_row_it_put_back(
    evals_on, logs, monkeypatch
):
    """The forgive's registry check reads the row by key with a strongly consistent read: an
    eventually consistent one made just after the put, or the scan, may not see the row yet."""
    culled(evals_on, logs)
    add_model_if_missing, get_model = dynamo.add_model_if_missing, dynamo.get_model
    list_models = dynamo.list_models
    added = []

    def add(item):
        added.append(add_model_if_missing(item))
        return added[-1]

    def stale_get(model_id, *, consistent=False):
        return get_model(model_id, consistent=consistent) if consistent or not any(added) else None

    def stale_list():
        return [row for row in list_models() if not any(added) or row["model_id"] != POOR]

    monkeypatch.setattr(dynamo, "add_model_if_missing", add)
    monkeypatch.setattr(dynamo, "get_model", stale_get)
    monkeypatch.setattr(dynamo, "list_models", stale_list)

    status, body = route_restore(POOR)

    assert added == [True]
    assert (status, body["restored"], body["forgive"]["delta"]) == (200, True, 1200)
    assert [(row["delta"], row["reason"]) for row in adjust_rows(evals_on)] == [(1200, "Admin Forgive")]
    [event] = events_of(logs, "Admin Forgive")
    assert event["model_id"] == POOR
    assert POOR not in dynamo.get_model_archive()


def test_a_row_the_restores_lookup_missed_is_held_open_when_the_add_finds_it_there(
    evals_on, logs, monkeypatch
):
    """A resend after a restore that stopped part-way, or a row put back by hand, that the lookup did
    not see: the add is refused, and the row there still gets its ungated_until."""
    culled(evals_on, logs)
    put_scored(evals_on, POOR, total=-1200, display_name="By hand")
    registry_row, looked_up = evals_adjust._registry_row, []

    def missed_once(given, model_id):
        looked_up.append(given)
        return None if len(looked_up) == 1 else registry_row(given, model_id)

    monkeypatch.setattr(evals_adjust, "_registry_row", missed_once)

    status, body = route_restore(POOR)

    assert status == 200 and body["restored"] is False and body["forgive"]["delta"] == 1200
    row = dynamo.get_model(POOR)
    assert row["ungated_until"] == body["ungated_until"] and row["display_name"] == "By hand"
    assert POOR not in dynamo.get_model_archive()


def test_a_restore_whose_row_is_deleted_meanwhile_is_refused_before_anything_is_forgiven(evals_on, logs):
    culled(evals_on, logs)
    put_scored(evals_on, POOR, total=-1200)  # put back by hand, then deleted just after the restore reads it
    set_model_fields = dynamo.set_model_fields

    def deleted_first(model_id, updates, *, expected):
        dynamo.delete_model(model_id)
        return set_model_fields(model_id, updates, expected=expected)

    with patch.object(dynamo, "set_model_fields", deleted_first):
        status, body = route_restore(POOR)

    assert status == 409
    assert body["error"] == (
        f"the registry's row for '{POOR}' was deleted while it was being restored: nothing was forgiven "
        "and the archive still holds it, so send it again"
    )
    assert POOR not in registry() and POOR in dynamo.get_model_archive() and adjust_rows(evals_on) == []
    # Sent again, it puts the archived row back and forgives it.
    status, body = route_restore(POOR)
    assert (status, body["restored"], body["forgive"]["delta"]) == (200, True, 1200)
    assert POOR in registry() and dynamo.get_model_archive() == {}


POOR_ARN = f"arn:aws:bedrock:{test_admin_evals.REGION}:123456789012:inference-profile/{POOR}"


def test_a_model_the_registry_kept_under_its_arn_is_restored_by_its_id(evals_on, logs):
    put_scored(evals_on, POOR_ARN, total=0, events=0)
    poor_history(evals_on, POOR)  # evals rows are kept under the canonical id
    rolled_up(logs)
    assert dynamo.get_model(POOR_ARN)["total_perf"] == -1200
    propose()
    cull_on(evals_on)
    assert call("POST /evals/cull/apply")[1]["culled_ids"] == [POOR_ARN]
    # Archived under the ARN, so only matching the archive's keys as the registry's rows finds it.
    assert set(dynamo.get_model_archive()) == {POOR_ARN}

    status, body = route_restore(POOR)

    assert (status, body["model_id"], body["restored"]) == (200, POOR_ARN, True)
    assert (body["forgive"]["model_id"], body["forgive"]["delta"]) == (POOR, 1200)
    [event] = events_of(logs, "Admin Forgive")
    assert event["model_id"] == POOR
    assert dynamo.get_model(POOR_ARN)["ungated_until"] == body["ungated_until"]
    # Back under the ARN, with no second row under the canonical id.
    assert dynamo.get_model(POOR) is None and dynamo.get_model_archive() == {}


def test_unarchiving_a_model_the_archive_does_not_hold_returns_false_and_writes_nothing(evals_on):
    assert dynamo.unarchive_model(POOR) is False
    assert dynamo.get_config_row("model-archive") is None
    entry = {"display_name": "Fine", "provider": "maker", "pruned_at": "2026-10-01T00:00:00+00:00"}
    dynamo.archive_models({FINE: entry})

    assert dynamo.unarchive_model(POOR) is False
    assert dynamo.get_model_archive() == {FINE: entry}
    assert dynamo.unarchive_model(FINE) is True
    assert dynamo.get_model_archive() == {}


def route_restore(model_id):
    event = {"routeKey": "POST /models/{model_id}/restore", "pathParameters": {"model_id": model_id}}
    result = admin_api_handler.handler(event, None)
    return result["statusCode"], json.loads(result["body"])


# --- evals status ---------------------------------------------------------------------------------


def test_status_shows_no_proposal_and_no_cull_before_the_first_cycle(evals_on, logs, queue):
    _, body = call("GET /evals/status")

    assert body["cull"] == {"proposal": None, "last_apply": None, "detail": "no cull proposal yet"}


def test_status_shows_the_proposal_in_place_and_when_it_has_expired(proposed, queue, monkeypatch):
    monkeypatch.setattr(evals_status, "datetime", Clock)
    _, body = call("GET /evals/status")

    assert body["cull"]["proposal"] == state()["cull_proposal"]
    assert [entry["model_id"] for entry in body["cull"]["proposal"]["models"]] == [WORST, WORSE, BAD, POOR]
    assert (body["cull"]["expired"], body["cull"]["last_apply"]) == (False, None)
    monkeypatch.setattr(Clock, "at", NEXT_CYCLE)
    assert call("GET /evals/status")[1]["cull"]["expired"] is True


def test_status_shows_the_last_cull_applied_when_and_which_models(proposed, queue):
    cull_on(proposed)
    call("POST /evals/cull/apply")

    _, body = call("GET /evals/status")

    last = body["cull"]["last_apply"]
    assert (last["applied_at"], last["culled_ids"]) == (AT.isoformat(), [WORST, WORSE, BAD])
    assert last["left"] == [POOR] and last["kept"] == []
    assert body["cull"]["proposal"]["proposed_at"] == last["proposed_at"]


# --- Terraform ------------------------------------------------------------------------------------


@pytest.mark.parametrize("env", ["dev", "production"])
def test_both_culling_routes_are_in_the_environments_terraform(env):
    main = (Path(__file__).resolve().parents[2] / "infra" / "environments" / env / "main.tf").read_text(
        encoding="utf-8"
    )
    admin_routes = main[: main.index('"GET /articles/{article_id}/feedback-status"')]
    culling = re.compile(r"/cull/|^POST /models/.*/restore$")
    routes = {route for route in admin_api_handler._ROUTES if culling.search(route)}
    assert routes == {"POST /evals/cull/apply", "POST /models/{model_id}/restore"}
    for route in routes:
        assert f'"{route}",' in admin_routes, route
