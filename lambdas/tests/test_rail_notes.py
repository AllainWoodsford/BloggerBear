"""Tests for common/rail_notes.py: the rail network's flags and simulated links as sentences.

The entries are built by hand with the keys common/adapters/rail_access.py stores and
vision/rail_analyse.py's `flag_stations` and `suggest` return (test_vision_rail_core.py holds
those); what is tested here is the wording, what a missing value does to it, the order and the
caps, and that nothing in it trips the compliance checks an article goes through.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from common import compliance, rail_notes
from common.rail_notes import NOTES_LEAD, format_notes, planner_notes


def station(sid, name, degree, betweenness, activity, d_hub_m, isolation_weight):
    """A station row as a history entry keeps it (rail_access.STATION_FIELDS)."""
    return {"id": sid, "name": name, "lon": 151.2, "lat": -33.87, "degree": degree,
            "betweenness": betweenness, "activity": activity, "d_hub_m": d_hub_m,
            "isolation_weight": isolation_weight, "intermodal": {"bus_stop_near": 1}}  # fmt: skip


def entry(**changes):
    """One history entry with every kind of flag and suggestion, in the order the analysis makes
    them (isolated high demand, single point of failure, ghost line; orbital links, feeders)."""
    base = {
        "scene_id": "S1",
        "hubs": ["s:H1", "s:H2"],
        "stations": [
            station("s:H1", "Central", 4, 0.5, 0.81, 0.0, 0.0),
            station("s:H2", "Parramatta", 3, 0.2, 0.7, 0.0, 0.0),
            station("s:A", "Epping", 2, 0.05, 0.62, 3400.0, 5.4839),
            station("s:B", "Hornsby", 1, 0.01, 0.3, 12300.0, 41.0),
            station("s:X1", "Ourimbah", 2, 0.0, 0.02, 30000.0, 600.0),
            station("s:X2", "Lisarow", 2, 0.0, 0.02, 31000.0, 620.0),
            station("s:X3", "Niagara Park", 2, 0.0, 0.03, 32000.0, 640.0),
        ],
        "flags": [
            {"type": "isolated_high_demand", "station": "s:A", "name": "Epping", "activity": 0.62,
             "degree": 2, "isolation_weight": 5.4839},
            {"type": "single_point_of_failure", "station": "s:H1", "name": "Central", "ratio": 2.5},
            {"type": "ghost_line", "stations": ["s:X1", "s:X2", "s:X3"],
             "names": ["Ourimbah", "Lisarow", "Niagara Park"]},
        ],
        "suggestions": [
            {"type": "orbital_link", "from": "s:A", "to": "s:B", "from_name": "Epping", "to_name": "Hornsby",
             "straight_km": 3.1234, "delta_top_hub_betweenness": -0.06, "delta_mean_d_hub_m": -640.4},
            {"type": "feeder_corridor", "station": "s:B", "name": "Hornsby", "hub": "s:H1",
             "hub_name": "Central", "length_m": 12300.0},
        ],
    }  # fmt: skip
    return {**base, **changes}


SPOF = (
    "Central lies on 2.5 times as many shortest paths through the mapped rail network as any other station: "
    "the pipeline flags it as a single point of failure, where a disruption would cut more routes than "
    "anywhere else on the map."
)
ISOLATED = (
    "Epping has one of the busiest catchments in the city by built-up density (activity 0.62, top quarter), "
    "2 track directions and 3.4 km by rail to the nearest hub: the pipeline flags it as isolated high demand."
)
GHOST = (
    "3 consecutive stations from Ourimbah to Niagara Park sit in the quietest quarter of built-up density "
    "along the network: the pipeline flags the stretch as a possible ghost line, where track runs through "
    "little built-up area."
)
# 3.1234 km in a straight line, simulated 20 % longer: 3.7 km; -0.06 of the top hub's 0.5: -12 %.
ORBITAL = (
    "Simulation: a 3.7 km link between Epping and Hornsby (the straight line plus 20 %) would bring the "
    "mapped network's stations 640 m closer to a hub on average by rail, and change the busiest hub's share "
    "of shortest paths by -12 %."
)
FEEDER = (
    "Simulation: Hornsby is among the most isolated stations for the size of its catchment (isolation "
    "weight 41.0); a feeder corridor, such as an express bus, to Central, 12.3 km away by rail today, is "
    "the link the pipeline's rules point to."
)


def texts(e, **caps):
    return [note["text"] for note in planner_notes(e, **caps)]


def only(e, kind_of):
    (note,) = (note for note in planner_notes(e) if note["type"] == kind_of)
    return note["text"]


# --- the sentences -------------------------------------------------------------------------------


def test_each_flag_and_simulation_reads_as_one_sentence_from_the_measured_values():
    notes = planner_notes(entry())
    assert notes == [
        {"kind": "flag", "type": "single_point_of_failure", "stations": ["s:H1"], "text": SPOF},
        {"kind": "flag", "type": "isolated_high_demand", "stations": ["s:A"], "text": ISOLATED},
        {"kind": "flag", "type": "ghost_line", "stations": ["s:X1", "s:X2", "s:X3"], "text": GHOST},
        {"kind": "simulation", "type": "orbital_link", "stations": ["s:A", "s:B"], "text": ORBITAL},
        {"kind": "simulation", "type": "feeder_corridor", "stations": ["s:B", "s:H1"], "text": FEEDER},
    ]
    json.dumps(notes, allow_nan=False)  # JSON-clean, for the state, the evidence and the Finding


def test_the_notes_are_introduced_as_proxies_and_nothing_is_said_without_any():
    notes = planner_notes(entry())
    lead = "Rule-based flags and simulations (proxies, not recommendations): "
    assert format_notes(notes) == lead + " ".join(n["text"] for n in notes)
    assert format_notes(notes).startswith(NOTES_LEAD)
    for empty in (entry(flags=[], suggestions=[]), entry(flags=None, suggestions=None), {}, None, "S1"):
        assert planner_notes(empty) == []
    assert format_notes([]) == "" and format_notes(planner_notes({})) == ""


def test_the_length_named_is_the_simulated_link_with_its_article():
    for straight_km, phrase in ((7.0, "an 8.4 km link"), (9.25, "an 11.1 km link"), (15.0, "an 18.0 km link"),
                                (1.0, "a 1.2 km link"), (16.0, "a 19.2 km link")):  # fmt: skip
        link = {**entry()["suggestions"][0], "straight_km": straight_km}
        assert f"Simulation: {phrase} between Epping" in only(entry(suggestions=[link]), "orbital_link")


# --- what was not measured -----------------------------------------------------------------------


def test_a_value_not_measured_drops_the_clause_that_needs_it_and_never_prints_none():
    nan = float("nan")
    # The single point of failure with no second station to compare with.
    spof = {"type": "single_point_of_failure", "station": "s:H1", "name": "Central", "ratio": None}
    assert only(entry(flags=[spof]), "single_point_of_failure") == (
        "Central lies on more shortest paths through the mapped rail network than any other station: the "
        "pipeline flags it as a single point of failure, where a disruption would cut more routes than "
        "anywhere else on the map."
    )
    # Isolated high demand: no activity, one track direction, then nothing but the quarter.
    rows = copy.deepcopy(entry()["stations"])
    rows[2].update(activity=None, degree=1)
    flag = {"type": "isolated_high_demand", "station": "s:A", "name": "Epping", "activity": None, "degree": 1}
    assert only(entry(stations=rows, flags=[flag]), "isolated_high_demand") == (
        "Epping has one of the busiest catchments in the city by built-up density (top quarter), 1 track "
        "direction and 3.4 km by rail to the nearest hub: the pipeline flags it as isolated high demand."
    )
    rows[2].update(degree=None, d_hub_m=nan)
    bare = {"type": "isolated_high_demand", "station": "s:A", "name": "Epping"}
    assert only(entry(stations=rows, flags=[bare]), "isolated_high_demand") == (
        "Epping has one of the busiest catchments in the city by built-up density (top quarter): the "
        "pipeline flags it as isolated high demand."
    )
    # An orbital link without its straight line or the hub's betweenness change.
    link = {**entry()["suggestions"][0], "straight_km": None, "delta_top_hub_betweenness": None}
    assert only(entry(suggestions=[link]), "orbital_link") == (
        "Simulation: a link between Epping and Hornsby (the straight line plus 20 %) would bring the mapped "
        "network's stations 640 m closer to a hub on average by rail."
    )
    # The share clause needs every hub's betweenness now, and a non-zero one to divide by.
    no_share = "the busiest hub's share"
    assert no_share not in only(entry(hubs=["s:H1", "s:gone"]), "orbital_link")
    assert no_share not in only(entry(hubs=[]), "orbital_link")
    flat = copy.deepcopy(entry()["stations"])
    flat[0]["betweenness"] = flat[1]["betweenness"] = 0.0
    assert no_share not in only(entry(stations=flat), "orbital_link")
    # A link without the fall in distance (or with none) is not a simulation result: no note.
    for closer in (None, nan, 0.0, -0.4, 12.0):
        pointless = {**entry()["suggestions"][0], "delta_mean_d_hub_m": closer}
        assert planner_notes(entry(flags=[], suggestions=[pointless])) == []
    # A feeder from a station without a row: no weight, no distance, and an unnamed hub.
    feeder = {"type": "feeder_corridor", "station": "s:gone", "name": "Asquith", "hub": "s:gone-hub",
              "hub_name": None, "length_m": None}  # fmt: skip
    assert only(entry(suggestions=[feeder]), "feeder_corridor") == (
        "Simulation: Asquith is among the most isolated stations for the size of its catchment; a feeder "
        "corridor, such as an express bus, to its nearest hub is the link the pipeline's rules point to."
    )
    everything_missing = entry(
        stations=[{"id": "s:A"}, {"id": "s:B"}, {"id": "s:H1"}],
        flags=[spof, bare, {"type": "ghost_line", "stations": ["s:X1", "s:X2"], "names": ["P", "Q"]}],
        suggestions=[link, feeder],
    )
    written = texts(everything_missing)
    assert len(written) == 5 and not any("None" in text or "nan" in text for text in written)


def test_figures_from_sampled_betweenness_are_said_to_be_estimates():
    sampled = entry(quality_flags=["betweenness_sampled"])
    assert only(sampled, "single_point_of_failure").startswith("Central lies on about 2.5 times as many")
    assert "share of shortest paths by about -12 %." in only(sampled, "orbital_link")
    # Nothing else reads betweenness, and an exact one is stated as it is.
    for kind_of in ("isolated_high_demand", "ghost_line", "feeder_corridor"):
        assert only(sampled, kind_of) == only(entry(), kind_of)
    assert "about" not in format_notes(planner_notes(entry(quality_flags=["low_coverage"])))


def test_a_station_missing_from_the_rows_uses_the_name_it_carries_or_is_skipped():
    gone = {"type": "single_point_of_failure", "station": "s:gone", "name": "Asquith", "ratio": 3.0}
    assert only(entry(flags=[gone]), "single_point_of_failure").startswith("Asquith lies on 3.0 times")
    unnamed = {"type": "single_point_of_failure", "station": "s:gone", "ratio": 3.0}
    blank = {"type": "isolated_high_demand", "station": "s:gone", "name": " ​ "}
    ghost = {"type": "ghost_line", "stations": ["s:gone", "s:X2", "s:X3"]}
    link = {**entry()["suggestions"][0], "to": "s:gone", "to_name": None}
    feeder = {**entry()["suggestions"][1], "station": "s:gone", "name": None}
    assert planner_notes(entry(flags=[unnamed, blank, ghost], suggestions=[link, feeder])) == []
    # A station that is in the rows is named as its row stores it.
    renamed = copy.deepcopy(entry()["stations"])
    renamed[0]["name"] = "Sydney Central"
    assert only(entry(stations=renamed), "single_point_of_failure").startswith("Sydney Central lies on")
    # Ghost lines without names in the flag take them from the rows.
    ghost = {"type": "ghost_line", "stations": ["s:X1", "s:X2", "s:X3"]}
    assert only(entry(flags=[ghost]), "ghost_line").startswith("3 consecutive stations from Ourimbah to")


def test_names_with_control_or_bidi_characters_come_out_clean():
    rows = copy.deepcopy(entry()["stations"])
    rows[0]["name"] = "Cen‮tral\x00 Station\n⁦Platform⁩ 1"
    text = only(entry(stations=rows), "single_point_of_failure")
    assert text.startswith("Central Station Platform 1 lies on 2.5 times")
    flag = {**entry()["flags"][2], "stations": ["s:new1", "s:X2", "s:new3"],
            "names": ["Up​per\tWest", "x", "Lower\r\nEast‭"]}  # fmt: skip
    assert "from Upper West to Lower East sit" in only(entry(flags=[flag]), "ghost_line")


# --- order and caps ------------------------------------------------------------------------------


def test_flags_keep_a_fixed_order_and_the_cap():
    flags = [
        {"type": "ghost_line", "stations": ["s:X1", "s:X2", "s:X3"]},
        {"type": "isolated_high_demand", "station": "s:A", "name": "Epping", "isolation_weight": 3.0},
        {"type": "ghost_line", "stations": ["s:X1", "s:X2", "s:X3", "s:A", "s:B"]},
        {"type": "isolated_high_demand", "station": "s:B", "name": "Hornsby", "isolation_weight": 9.0},
        {"type": "single_point_of_failure", "station": "s:H1", "name": "Central", "ratio": 2.5},
        {"type": "a_flag_from_the_future", "station": "s:A"},
    ]
    notes = planner_notes(entry(flags=flags, suggestions=[]))
    assert [(n["type"], n["stations"][0]) for n in notes] == [
        ("single_point_of_failure", "s:H1"),
        ("isolated_high_demand", "s:B"),  # the higher isolation weight first
        ("isolated_high_demand", "s:A"),
        ("ghost_line", "s:X1"),  # the longest stretch; the shorter one is past the cap of four
    ]
    assert len(notes[3]["stations"]) == 5 and notes[3]["text"].startswith("5 consecutive stations")
    # The same entry in any order reads the same.
    assert planner_notes(entry(flags=flags[::-1], suggestions=[])) == notes
    # A flag that cannot be written does not take a place.
    unnamed = {"type": "single_point_of_failure", "station": "s:gone"}
    assert planner_notes(entry(flags=[unnamed, *flags], suggestions=[]))[0]["stations"] == ["s:H1"]
    assert [n["type"] for n in planner_notes(entry(flags=flags, suggestions=[]), max_flags=1)] == [
        "single_point_of_failure"
    ]


def test_simulations_keep_a_fixed_order_and_the_cap():
    base = entry()["suggestions"][0]
    suggestions = [
        {"type": "feeder_corridor", "station": "s:A", "name": "Epping", "hub": "s:H1", "length_m": 3400.0},
        {**base, "delta_mean_d_hub_m": -100.0},
        {"type": "feeder_corridor", "station": "s:B", "name": "Hornsby", "hub": "s:H1", "length_m": 12300.0},
        {**base, "from": "s:H2", "from_name": "Parramatta", "delta_mean_d_hub_m": -300.0},
    ]
    notes = planner_notes(entry(flags=[], suggestions=suggestions))
    assert [(n["type"], n["stations"][0]) for n in notes] == [
        ("orbital_link", "s:H2"),  # the larger fall in distance to a hub first
        ("orbital_link", "s:A"),
        ("feeder_corridor", "s:B"),  # the higher isolation weight; the other is past the cap of three
    ]
    assert planner_notes(entry(flags=[], suggestions=suggestions[::-1])) == notes
    one = planner_notes(entry(flags=[], suggestions=suggestions), max_simulations=1)
    assert [n["type"] for n in one] == ["orbital_link"]
    # Flags first, then simulations, at most seven in all.
    both = planner_notes(entry(suggestions=suggestions))
    assert [n["kind"] for n in both] == ["flag"] * 3 + ["simulation"] * 3
    assert rail_notes.MAX_NOTES == 7


# --- what the module is --------------------------------------------------------------------------


def test_the_module_imports_without_numpy_opencv_or_networkx():
    # In a fresh interpreter none of them enters sys.modules on import: the adapter that imports this
    # is imported by every pipeline Lambda, and only the worker's package has them.
    lambdas_dir = Path(rail_notes.__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1]); import common.rail_notes; "
        "heavy = {'numpy', 'cv2', 'networkx', 'boto3'}; "
        "print(sorted(heavy & {m.split('.')[0] for m in sys.modules}))"
    )
    out = subprocess.run(
        [sys.executable, "-I", "-c", code, str(lambdas_dir)], capture_output=True, text=True, check=True
    )
    assert out.stdout.strip() == "[]"


def test_the_detour_named_is_the_one_the_analysis_simulates():
    from vision import rail_analyse  # networkx: the worker's module, imported only here

    assert rail_notes.ORBITAL_DETOUR_PERCENT == round((rail_analyse.ORBITAL_DETOUR - 1) * 100) == 20


# --- compliance ----------------------------------------------------------------------------------

# Words a flag or a simulation never uses: it is not advice, an instruction or a forecast.
NEVER = ("recommend", "should", "must", "need to", "you ", "best", "worst", "collapse", "fail ")


@pytest.mark.parametrize("text", [SPOF, ISOLATED, GHOST, ORBITAL, FEEDER])
def test_the_sentences_pass_the_compliance_checks(text):
    # common/compliance.py decides in code which nominated items hold an article: advice only with
    # an advice word, PII only with contact details its redaction catches or a street address. None
    # of the sentences gives either anything to hold, whatever the reviewing model nominates.
    assert not compliance._item_stands("ADVICE", text, source="")
    assert not compliance._item_stands("PII", text, source="")
    assert compliance.regex_redact(text) == text
    # Every figure and name in it is in the research it came from (the diff summary carries it).
    source = format_notes(planner_notes(entry()))
    assert not compliance._item_stands("FABRICATED", text, source=source)
    assert compliance.invented_figures(text, source) == []
    lowered = text.lower()
    assert not [word for word in NEVER if word in lowered]
    assert ("flags" in text) != text.startswith("Simulation: ")
    # The lead says what they are; its one "recommendations" is the denial.
    assert compliance.regex_redact(NOTES_LEAD) == NOTES_LEAD
    assert not compliance._item_stands("ADVICE", NOTES_LEAD, source="")
