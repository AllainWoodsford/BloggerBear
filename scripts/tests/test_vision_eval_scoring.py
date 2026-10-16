"""Tests for the evaluation's pure parts: the agent cases' derived networks
(scripts/vision_eval_networks.py) and the scores, counterfactual counts and stability rates
(scripts/vision_eval_scoring.py).

The derivations run on the benchmark's synthetic network over the synthetic city's polygon, so
what each one adds and removes is checked against a network whose every line and station is
known. Nothing here touches the network: requests, boto3 and socket connections raise.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

import vision_eval_networks as nets
import vision_eval_scoring as scoring

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "lambdas") not in sys.path:
    sys.path.insert(0, str(ROOT / "lambdas"))

SIZE = 512  # the synthetic city's side in 20 m pixels: 10.24 km


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import socket

    import boto3
    import requests

    def refuse(*args, **kwargs):
        raise AssertionError("the tests must not touch the network")

    for name in ("get", "head", "put", "post", "request"):
        monkeypatch.setattr(requests, name, refuse)
    monkeypatch.setattr(requests.Session, "request", refuse)
    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)
    monkeypatch.setattr(socket.socket, "connect", refuse)


def city():
    """The synthetic city's network (vision_benchmark.synthetic_network) and its polygon."""
    from vision import geo

    import vision_benchmark as vb

    network = vb.synthetic_network(SIZE)
    network.update(source="overpass", endpoint="https://overpass.example/api", query_url="q", truncated=False)
    nets.recount(network)
    corners = ((0, 0), (SIZE, 0), (SIZE, SIZE), (0, SIZE))
    polygon = [list(geo.pixel_to_lonlat(vb.CITY_TRANSFORM, vb.CITY_EPSG, x, y)) for x, y in corners]
    return network, polygon


def ids(items):
    return {str(item["id"]) for item in items}


def distance_m(plane, a, b):
    return math.dist(plane.xy(*a), plane.xy(*b))


# --- the derived networks --------------------------------------------------------------------------


def test_every_derivation_leaves_a_later_recounted_copy_and_never_touches_a():
    network, polygon = city()
    before = json.dumps(network, sort_keys=True)
    for name in nets.DERIVATIONS:
        derived, change = nets.derive(network, {"derive": name}, polygon)
        assert json.dumps(network, sort_keys=True) == before, f"{name} changed fixture A"
        assert change["recipe"] == name
        assert derived["fetched_at"] == "2026-01-02T00:00:00+00:00"  # a day after A's
        assert derived["counts"] == {
            "ways": len(derived["ways"]), "stations": len(derived["stations"]), "pois": len(derived["pois"]),
            "points": sum(len(w["points"]) for w in derived["ways"]),
        }  # fmt: skip
        assert (derived["source"], derived["endpoint"], derived["query_url"]) == ("overpass",
                                                                                 network["endpoint"], "q")
        assert ids(derived["stations"]) == (ids(network["stations"]) - set(change["removed_stations"])
                                            | set(change["added_stations"]))  # fmt: skip
        assert all(sid.startswith("eval-") and len(sid) <= 32 for sid in change["added_stations"])
        # Every derived network is one the worker contract accepts.
        from common import vision_contract

        vision_contract.validate_network(derived)


def test_add_line_lays_its_stations_through_the_anchor_inside_the_site_and_beyond_reach():
    network, polygon = city()
    plane = nets.plane_for(polygon)
    anchor = nets.farthest_point(network, polygon)
    nearest = min(distance_m(plane, anchor, (s["lon"], s["lat"])) for s in network["stations"])
    assert nearest > nets.REACH_M  # the farthest point is beyond every station's reach
    deserts = [{"lon": anchor[0], "lat": anchor[1], "area_km2": 3.0}]
    recipe = {"derive": "add_line", "stations": 5, "anchor": "largest_desert"}
    derived, change = nets.derive(network, recipe, polygon, deserts=deserts)
    added = [s for s in derived["stations"] if s["id"] in change["added_stations"]]
    assert len(added) == 5 and change["added_ways"] == ["eval-w1"] and change["anchor"] == anchor
    poly = [plane.xy(*p) for p in polygon]
    assert all(nets.inside(plane.xy(s["lon"], s["lat"]), poly) for s in added)
    gaps = [distance_m(plane, (a["lon"], a["lat"]), (b["lon"], b["lat"])) for a, b in zip(added, added[1:])]
    assert all(abs(gap - change["spacing_m"]) < 1.0 for gap in gaps)
    # One station sits on the anchor.
    on_anchor = next(s for s in added if s["id"] == change["anchor_station"])
    assert distance_m(plane, anchor, (on_anchor["lon"], on_anchor["lat"])) < 1.0
    way = next(w for w in derived["ways"] if w["id"] == "eval-w1")
    assert way["points"] == [[s["lon"], s["lat"]] for s in added]  # first station to last
    assert way["name"] == "Evaluation line" and way["tunnel"] is False
    assert change["anchor_station"] in change["added_stations"]


def test_add_line_without_room_anywhere_is_refused_by_name():
    network, polygon = city()
    with pytest.raises(nets.DeriveError, match="no room for a line of 40 stations"):
        nets.add_line(network, polygon, stations=40)


def test_the_deserts_anchor_add_line_on_the_largest_and_extend_line_on_every_one():
    deserts = [{"lon": 151.0, "lat": -33.9, "area_km2": 2.0}, {"lon": 151.1, "lat": -33.8, "area_km2": 6.5},
               {"lon": "x"}]  # fmt: skip
    assert nets.largest_desert(deserts) == [151.1, -33.8]
    assert nets.desert_points(deserts) == [[151.1, -33.8], [151.0, -33.9]]  # largest first, the bad one out
    assert nets.largest_desert([]) is None and nets.largest_desert(None) is None
    assert nets.ANCHORS == {"add_line": ("largest_desert",), "extend_line": ("deserts",)}


def test_extend_line_continues_a_terminus_outward_inside_the_site():
    network, polygon = city()
    plane = nets.plane_for(polygon)
    derived, change = nets.derive(network, {"derive": "extend_line", "stations": 3}, polygon)
    extended = next(w for w in network["ways"] if w["id"] == change["extended_way"])
    new_way = next(w for w in derived["ways"] if w["id"] == "eval-w1")
    # The extension starts at one end of the way it extends and carries its name.
    assert new_way["points"][0] in (extended["points"][0], extended["points"][-1])
    assert new_way["name"] == extended["name"] and len(change["added_stations"]) == 3
    # Each new station is further from the way's other end than the last: outward, not back.
    first, last = extended["points"][0], extended["points"][-1]
    other_end = last if new_way["points"][0] == first else first
    added = [s for s in derived["stations"] if s["id"] in change["added_stations"]]
    far = [distance_m(plane, other_end, (s["lon"], s["lat"])) for s in added]
    assert far == sorted(far)
    assert change["spacing_m"] <= nets.EXTENSION_SPACING_M  # laid closer only because the site is small


def test_remove_line_takes_one_named_line_and_only_the_stations_that_snap_to_it_alone():
    network, polygon = city()
    plane = nets.plane_for(polygon)
    derived, change = nets.derive(network, {"derive": "remove_line", "min_stations": 3}, polygon)
    gone = [w for w in network["ways"] if w["id"] in change["removed_ways"]]
    assert gone and len({w["name"] for w in gone}) == 1
    assert not any(w["name"] == gone[0]["name"] for w in derived["ways"])
    assert len(change["removed_stations"]) >= 3
    index = nets.WayIndex(derived["ways"], plane)
    for station in network["stations"]:
        if station["id"] in change["removed_stations"]:
            # A removed station snaps to no way that is left.
            assert not index.nearest(*plane.xy(station["lon"], station["lat"]), nets.SNAP_M)
    with pytest.raises(nets.DeriveError, match="no named line has 50 stations"):
        nets.remove_line(network, polygon, min_stations=50)


def test_add_served_station_puts_one_station_on_a_way_within_reach_of_two_others():
    network, polygon = city()
    plane = nets.plane_for(polygon)
    derived, change = nets.derive(network, {"derive": "add_served_station"}, polygon)
    (sid,) = change["added_stations"]
    new = next(s for s in derived["stations"] if s["id"] == sid)
    a, b = (next(s for s in network["stations"] if s["id"] == i) for i in change["between"])
    for old in (a, b):
        assert distance_m(plane, (new["lon"], new["lat"]), (old["lon"], old["lat"])) <= nets.REACH_M
    assert nets.WayIndex(network["ways"], plane).nearest(*plane.xy(new["lon"], new["lat"]), 1.0)


def test_duplicate_stations_maps_three_stations_twice_under_new_ids_at_the_same_places():
    network, polygon = city()
    derived, change = nets.derive(network, {"derive": "duplicate_stations", "stations": 3}, polygon)
    assert len(derived["stations"]) == len(network["stations"]) + 3 and derived["ways"] == network["ways"]
    by_id = {s["id"]: s for s in network["stations"]}
    copies = [s for s in derived["stations"] if s["id"] in change["added_stations"]]
    for copy_, original in zip(copies, change["duplicated"], strict=True):
        assert (copy_["lon"], copy_["lat"], copy_["name"]) == (by_id[original]["lon"], by_id[original]["lat"],
                                                               by_id[original]["name"])
    assert len(set(change["duplicated"])) == 3


def test_an_unknown_recipe_is_refused_and_a_time_that_cannot_be_read_is_kept():
    network, polygon = city()
    with pytest.raises(nets.DeriveError, match="unknown derivation 'teleport'"):
        nets.derive(network, {"derive": "teleport"}, polygon)
    assert nets.later("not a time") == "not a time" and nets.later(None) is None
    assert nets.later("2026-10-16T22:00:00Z") == "2026-10-17T22:00:00+00:00"


# --- the agent's scores ------------------------------------------------------------------------------


def run(case, expected, verdict, stop="answered", **extra):
    return {"case": case, "expected": expected, "verdict": verdict, "stop": stop, **extra}


def test_wilson_matches_the_formula_and_stays_inside_zero_and_one():
    assert scoring.wilson(0, 0) == (None, None)
    low, high = scoring.wilson(0, 10)
    assert low == 0.0 and 0.27 < high < 0.28
    low, high = scoring.wilson(10, 10)
    assert high == 1.0 and 0.72 < low < 0.73
    assert scoring.wilson(5, 10) == (0.2366, 0.7634)
    assert scoring.wilson(16, 16)[0] == 0.8064


def test_a_failed_closed_run_is_its_own_column_and_scores_for_neither_class():
    runs = [
        run("C1", "real", "real"), run("C1", "real", "artefact"),
        run("C1", "real", "artefact", "bedrock_error"),
        run("C2", "artefact", "artefact"), run("C2", "artefact", "artefact", "deadline"),
        run("C2", "artefact", "real"),
    ]  # fmt: skip
    matrix = scoring.confusion(runs)
    assert matrix == {"real": {"real": 1, "artefact": 1, "failed_closed": 1},
                      "artefact": {"real": 1, "artefact": 1, "failed_closed": 1}}  # fmt: skip
    scores = scoring.class_scores(matrix)
    assert scores["recall_real"] == 0.3333 and scores["recall_artefact"] == 0.3333
    assert scores["balanced_accuracy"] == 0.3333 and scores["failed_closed"] == 2
    assert scores["n_real"] == 3 and scores["recall_real_wilson"] == list(scoring.wilson(1, 3))
    # A malformed answer is failed closed too, though its verdict reads artefact.
    assert scoring.label(run("C9", None, "artefact", "malformed")) == "failed_closed"


def test_with_no_bedrock_access_every_run_is_counted_failed_closed_never_dropped():
    runs = [run(c, e, "artefact", "bedrock_error") for c, e in (("C1", "real"), ("C2", "artefact"))] * 5
    scores = scoring.agent_scores(runs, [])
    agent = scores["agent"]
    assert agent["n_real"] == 5 and agent["n_artefact"] == 5 and agent["failed_closed"] == 10
    assert agent["recall_real"] == 0.0 and agent["recall_artefact"] == 0.0
    assert agent["balanced_accuracy"] == 0.0


def test_both_baselines_score_half_whatever_the_class_mix():
    for reals, artefacts in ((4, 1), (1, 4), (4, 4)):
        runs = [run("C1", "real", "real")] * reals + [run("C2", "artefact", "real")] * artefacts
        scores = scoring.agent_scores(runs, [])
        for kind in ("numeric_only", "always_artefact"):
            assert scores["baselines"][kind]["balanced_accuracy"] == 0.5
        assert scores["baselines"]["numeric_only"]["recall_real"] == 1.0
        assert scores["baselines"]["always_artefact"]["recall_artefact"] == 1.0


def test_stability_is_the_share_of_repeats_agreeing_with_each_cases_majority():
    result = scoring.stability({"C1": ["real", "real", "artefact", "real", "failed_closed"],
                                "C2": ["artefact"] * 5, "C3": []})  # fmt: skip
    assert result["per_case"]["C1"] == {"majority": "real", "agree": 0.6, "repeats": 5}
    assert result["per_case"]["C2"]["agree"] == 1.0 and "C3" not in result["per_case"]
    assert result["mean"] == 0.8
    # A tie goes to the first label in LABELS order.
    assert scoring.stability({"C4": ["artefact", "real"]})["per_case"]["C4"]["majority"] == "real"


def test_fail_closed_counts_the_failure_cases_that_ended_as_required():
    runs = [run("C7", "artefact", "artefact", "deadline", expected_stop="deadline"),
            run("C8", "artefact", "artefact", "bedrock_error", expected_stop="bedrock_error"),
            run("C9", "artefact", "artefact", "answered", expected_stop="malformed"),
            run("C6", None, "real")]  # fmt: skip
    assert scoring.fail_closed(runs) == {"as_required": 2, "runs": 3, "all": False}
    assert scoring.fail_closed([])["all"] is None


def test_tool_use_latency_tokens_and_cost():
    runs = [
        run("C1", "real", "real", tools=["look_again", "web_context"], elapsed_ms=1000, input_tokens=1000,
            output_tokens=100, cost_usd=0.01, cost_aud=0.015),
        run("C2", "artefact", "artefact", tools=["previous_scene"], elapsed_ms=3000, input_tokens=500,
            output_tokens=50, cost_usd=None, cost_aud=None),
        run("C3", "artefact", "artefact", tools=[], elapsed_ms=2000, input_tokens=0, output_tokens=0,
            cost_usd=0.03, cost_aud=0.045),
    ]  # fmt: skip
    assert scoring.tool_use(runs) == {"tool_calls_mean": 1.0, "look_again_share": 0.3333,
                                      "web_context_share": 0.3333}  # fmt: skip
    money = scoring.costs(runs)
    assert (money["latency_p50_ms"], money["latency_p95_ms"]) == (2000, 2900)
    assert money["tokens_mean"] == 550.0 and money["cost_known"] == 2
    assert (money["cost_usd_mean"], money["cost_aud_mean"], money["cost_usd_total"]) == (0.02, 0.03, 0.04)
    assert scoring.percentile([], 50) is None and scoring.percentile([7], 95) == 7.0


def test_recall_by_input_and_the_flat_summary():
    runs = [
        run("C1", "real", "real", driven_by="network"),
        run("C2", "artefact", "real", driven_by="network"),
        run("C3", "artefact", "artefact", driven_by="imagery"),
    ]
    failures = [run("C7", "artefact", "artefact", "deadline", expected_stop="deadline")]
    scores = scoring.agent_scores(runs, failures)
    assert scores["by_driven"]["network"]["recall"] == 0.5 and scores["by_driven"]["imagery"]["recall"] == 1.0
    summary = scoring.agent_summary(scores, cases=4, gate_closed=1, runs=4)
    assert summary["recall_network_driven"] == 0.5 and summary["fail_closed"] == 1
    assert summary["balanced_accuracy"] == 0.75 and summary["gate_closed"] == 1
    assert all(value is None or isinstance(value, int | float) for value in summary.values())
    assert summary["recall_real_wilson_low"] == scoring.wilson(1, 1)[0]


# --- the counterfactuals -------------------------------------------------------------------------------


def test_agent_counterfactual_pairs_repeats_that_both_called_look_again_and_counts_flips():
    called = ["look_again"]
    measured = [run("C1", "real", "real", tools=called), run("C1", "real", "real", tools=called),
                run("C1", "real", "real", tools=[])]  # fmt: skip
    baseline = [run("C1", "real", "artefact", tools=called), run("C1", "real", "real", tools=called),
                run("C1", "real", "artefact", tools=called)]  # fmt: skip
    row = scoring.agent_counterfactual({"measured": measured, "baseline": baseline})
    assert row["called_look_again"] == {"measured": 2, "baseline": 3}
    assert (row["pairs"], row["flips"], row["flip_rate"]) == (2, 1, 0.5)
    assert row["labels"] == {"measured": {"real": 3}, "baseline": {"artefact": 2, "real": 1}}
    above = {"coverage": 0.62, "agent_called": True, "finding": True}
    below = {"coverage": 0.58, "agent_called": False, "finding": False}
    gate = scoring.gate_counterfactual(above, below)
    assert gate["flipped"] and gate["finding_flipped"]
    summary = scoring.counterfactual_summary({"C1": row}, {"C1": gate})
    assert summary == {"agent_pairs": 2, "agent_called_look_again": 5, "agent_flips": 1, "gate_pairs": 1,
                       "gate_flips": 1}  # fmt: skip


# --- E3 --------------------------------------------------------------------------------------------


def test_false_alarms_count_only_compared_decisions_with_a_wilson_interval_by_month():
    rows = [
        {"scene": "a", "captured_at": "2024-01-24T00:00:00Z", "code": "first_observation", "material": True},
        {"scene": "b", "captured_at": "2024-01-29T00:00:00Z", "code": "within_range", "material": False},
        {"scene": "c", "captured_at": "2024-03-14T00:00:00Z", "code": "reported", "material": True},
        {"scene": "d", "captured_at": "2024-03-24T00:00:00Z", "code": "below_floor", "material": False},
        {"scene": "e", "captured_at": "2024-04-23T00:00:00Z", "code": "within_range", "material": False},
    ]
    result = scoring.false_alarms(rows)
    assert (result["decisions"], result["false_material"], result["false_material_rate"]) == (3, 1, 0.3333)
    assert (result["wilson_low"], result["wilson_high"]) == scoring.wilson(1, 3)
    assert result["by_month"]["2024-03"] == {"decisions": 1, "false_material": 1, "rate": 1.0,
                                             "wilson": list(scoring.wilson(1, 1))}  # fmt: skip
    assert set(result["by_month"]) == {"2024-01", "2024-03", "2024-04"}
    assert result["codes"]["below_floor"] == 1


def test_stability_spread_iou_and_jaccards():
    rows = [
        {"scene": "a", "served_share": 0.70, "desert_km2": 10.0, "built_up_km2": 400.0, "hubs": ["s1", "s2"],
         "flags": [["spof", "s1"]], "iou_previous": None, "iou_first": None},
        {"scene": "b", "served_share": 0.72, "desert_km2": 12.0, "built_up_km2": 410.0, "hubs": ["s1", "s3"],
         "flags": [["spof", "s1"]], "iou_previous": 0.9, "iou_first": 0.9},
        {"scene": "c", "served_share": 0.69, "desert_km2": 11.0, "built_up_km2": 390.0, "hubs": ["s1", "s3"],
         "flags": [], "iou_previous": 0.95, "iou_first": 0.88},
    ]  # fmt: skip
    result = scoring.stability_spread(rows)
    assert result["served_share_spread"] == 0.03 and result["desert_rel_spread"] == 0.1818
    assert result["built_up_rel_spread"] == 0.05
    assert (result["mask_iou_min"], result["mask_iou_first_min"]) == (0.9, 0.88)
    assert result["hub_jaccard"] == [0.3333, 1.0] and result["flag_jaccard"] == [1.0, 0.0]
    summary = scoring.stability_summary(scoring.false_alarms([]), result)
    assert list(summary) == [
        "decisions", "false_material", "false_material_rate", "wilson_low", "wilson_high",
        "served_share_spread", "desert_rel_spread", "built_up_rel_spread", "mask_iou_min", "hub_jaccard_min",
        "flag_jaccard_min",
    ]  # fmt: skip
    assert summary["decisions"] == 0 and summary["wilson_low"] is None
    assert scoring.jaccard([], []) == 1.0 and scoring.iou_from_counts(0, 0) is None
    assert scoring.relative_spread([0.0, 0.0]) is None and scoring.spread([]) is None
