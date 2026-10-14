"""Planner notes: the rail network's flags and simulated links as sentences a reader can follow.

vision/rail_analyse.py flags stations (`flag_stations`: a single point of failure, isolated high
demand, a possible ghost line) and simulates changes to the network (`suggest`: an orbital link
between two outlying stations, a feeder corridor from an isolated station to its hub). The worker
returns both, and common/adapters/rail_access.py keeps them in each history entry beside the
station rows. This module turns one entry's into one sentence each, for the diff summary (and so
the article), the review evidence and the Finding. Pure Python, with no numpy, OpenCV or networkx:
the adapter that imports it is imported by every pipeline Lambda.

The rules, which the tests hold:
- every sentence is written here from measured values, and says what the measure is;
- flags are called flags and simulations simulations. Both are hypotheses from proxies (built-up
  density from imagery, straight-line reach, OpenStreetMap's topology), not ridership, cost or
  recommendations;
- a value that was not measured (None, missing, not a finite number) drops the clause that needs
  it, never prints "None"; so does a figure that rounds to nothing as printed (a change of 0 %,
  0.0 km, 0 m), and a simulation whose one claim rounds to nothing is not written at all; a note
  whose station has no name to show is dropped whole; a figure read from betweenness the analysis
  estimated from a sample says "about";
- every station name is OpenStreetMap's, crowd-sourced, and reaches the summary model's prompt: it
  is written in double quotation marks, as one line without controls or bidi overrides (but with
  the two joiners Persian and Indic names are spelt with), so it reads as a quoted name and
  nothing else;
- the order is fixed (flags: single point of failure, isolated high demand by isolation weight,
  ghost line longest first; simulations: orbital links by the fall in distance to a hub, feeder
  corridors by isolation weight), so the same entry always reads the same.
"""

from __future__ import annotations

import math
from decimal import Decimal

from common import osm

# At most this many of each kind per entry: an article ends with at most three (the summary prompt
# says so), and the review evidence and the Finding carry at most seven.
MAX_FLAGS, MAX_SIMULATIONS = 4, 3
MAX_NOTES = MAX_FLAGS + MAX_SIMULATIONS
# How much longer than the straight line a simulated orbital link is, in per cent:
# vision.rail_analyse.ORBITAL_DETOUR (1.2). The worker does not echo it, and rail_analyse imports
# networkx, so it is copied here and a test holds the two equal.
ORBITAL_DETOUR_PERCENT = 20
# The quality flag by which vision/rail_analyse.py says the betweenness was estimated from a seeded
# sample of source nodes (a large graph): a ratio or a share read from it is then "about" that.
SAMPLED_BETWEENNESS_FLAG = "betweenness_sampled"
# What the notes are introduced with wherever they are shown as one text.
NOTES_LEAD = "Rule-based flags and simulations (proxies, not recommendations): "


def planner_notes(
    entry: dict, *, max_flags: int = MAX_FLAGS, max_simulations: int = MAX_SIMULATIONS
) -> list[dict]:
    """One note per flag and simulation of a rail_access history entry, in the fixed order and
    capped: [{"kind": "flag" | "simulation", "type": <the flag or suggestion type>, "stations":
    [station ids], "text": <one sentence>}]. An entry without flags or suggestions has none."""
    if not isinstance(entry, dict):
        return []
    rows = _station_rows(entry.get("stations"))
    flags = _notes("flag", entry.get("flags"), _FLAG_RULES, rows, entry, max_flags)
    suggestions = entry.get("suggestions")
    return flags + _notes("simulation", suggestions, _SIMULATION_RULES, rows, entry, max_simulations)


def format_notes(notes: list[dict]) -> str:
    """The notes as one text, introduced as what they are; "" for none."""
    texts = [note["text"] for note in notes or []]
    return NOTES_LEAD + " ".join(texts) if texts else ""


# --- one sentence per type ----------------------------------------------------------------------


def _single_point_of_failure(flag: dict, rows: dict, entry: dict) -> tuple[list[str], str] | None:
    station = _id(flag.get("station"))
    name = _name(rows, station, flag.get("name"))
    if name is None:
        return None
    ratio = _shown(_number(flag.get("ratio")), 1)
    if ratio is None:
        lead = f"{name} lies on more shortest paths through the mapped rail network than any other station"
    else:
        lead = (
            f"{name} lies on {_about(entry)}{ratio} times as many shortest paths through the mapped rail "
            "network as any other station"
        )
    text = (
        f"{lead}: the pipeline flags it as a single point of failure, where a disruption would cut more "
        "routes than anywhere else on the map."
    )
    return _ids(station), text


def _isolated_high_demand(flag: dict, rows: dict, entry: dict) -> tuple[list[str], str] | None:
    station = _id(flag.get("station"))
    name = _name(rows, station, flag.get("name"))
    if name is None:
        return None
    row = rows.get(station) or {}
    activity = _shown(_measured(flag, row, "activity"), 2)
    degree = _whole(_first(flag.get("degree"), row.get("degree")))
    d_hub_km = _km(row.get("d_hub_m"))
    measured = f"activity {activity}, " if activity is not None else ""
    parts = [f"one of the busiest catchments in the city by built-up density ({measured}top quarter)"]
    if degree is not None:
        parts.append(f"{degree} track direction{'' if degree == 1 else 's'}")
    if d_hub_km is not None:
        parts.append(f"{d_hub_km} km by rail to the nearest hub")
    return _ids(station), f"{name} has {_and(parts)}: the pipeline flags it as isolated high demand."


def _ghost_line(flag: dict, rows: dict, entry: dict) -> tuple[list[str], str] | None:
    chain = flag.get("stations")
    if not isinstance(chain, list) or len(chain) < 2:
        return None
    names = flag.get("names")
    carried = names if isinstance(names, list) and len(names) == len(chain) else [None] * len(chain)
    first = _name(rows, _id(chain[0]), carried[0])
    last = _name(rows, _id(chain[-1]), carried[-1])
    if first is None or last is None:
        return None
    # Connected, not consecutive, and two of them, not the two ends: where the quiet stretch forks,
    # rail_analyse._walk_chain lists the path it walked and then the other branches' stations, so
    # the list's last station can sit on a branch and its length is not one line's.
    text = (
        f"{len(chain)} connected stations, among them {first} and {last}, sit in the quietest quarter of "
        "built-up density along the network: the pipeline flags the stretch as a possible ghost line, where "
        "track runs through little built-up area."
    )
    return _ids(*chain), text


def _orbital_link(suggestion: dict, rows: dict, entry: dict) -> tuple[list[str], str] | None:
    a, b = _id(suggestion.get("from")), _id(suggestion.get("to"))
    a_name, b_name = _name(rows, a, suggestion.get("from_name")), _name(rows, b, suggestion.get("to_name"))
    closer = _number(suggestion.get("delta_mean_d_hub_m"))
    # The analysis keeps only links that bring the stations closer to a hub. That fall is the
    # note's one claim: without it, or with one that rounds to no metre at all, there is no note.
    fall = _shown(-closer if closer is not None else None, 0)
    if a_name is None or b_name is None or fall is None:
        return None
    straight_km = _number(suggestion.get("straight_km"))
    # The simulated link is the straight line plus the detour, and that is the length named.
    simulated_km = straight_km * (100 + ORBITAL_DETOUR_PERCENT) / 100 if straight_km is not None else None
    length = _shown(simulated_km, 1)
    link = f"{_a_or_an(length)} {length} km link" if length is not None else "a link"
    text = (
        f"Simulation: {link} between {a_name} and {b_name} (the straight line plus "
        f"{ORBITAL_DETOUR_PERCENT} %) would bring the mapped network's stations {fall} m closer to a hub on "
        "average by rail"
    )
    share = _top_hub_share_change(suggestion, rows, entry)
    if share:  # a change that rounds to 0 % is no change to report
        text += f", and change the busiest hub's share of shortest paths by {_about(entry)}{share:+d} %"
    return _ids(a, b), text + "."


def _feeder_corridor(suggestion: dict, rows: dict, entry: dict) -> tuple[list[str], str] | None:
    station, hub = _id(suggestion.get("station")), _id(suggestion.get("hub"))
    name = _name(rows, station, suggestion.get("name"))
    if name is None:
        return None
    # The hub is the station's nearest by construction, so an unnamed one is still described.
    hub_name = _name(rows, hub, suggestion.get("hub_name")) or "its nearest hub"
    row = rows.get(station) or {}
    weight = _shown(_number(row.get("isolation_weight")), 1)
    length_km = _km(_first(suggestion.get("length_m"), row.get("d_hub_m")))
    text = f"Simulation: {name} is among the most isolated stations for the size of its catchment"
    if weight is not None:
        text += f" (isolation weight {weight})"
    text += f"; a feeder corridor, such as an express bus, to {hub_name}"
    if length_km is not None:
        text += f", {length_km} km away by rail today,"
    return _ids(station, hub), text + " is the link the pipeline's rules point to."


def _top_hub_share_change(suggestion: dict, rows: dict, entry: dict) -> int | None:
    """The change in the busiest hub's betweenness the simulation measured, in per cent of its
    betweenness now (the highest among the entry's hubs), rounded to a whole per cent (0 is left
    out of the note); None unless both are known, which needs every hub's station row."""
    delta = _number(suggestion.get("delta_top_hub_betweenness"))
    hubs = entry.get("hubs")
    if delta is None or not isinstance(hubs, list) or not hubs:
        return None
    current = [_number((rows.get(_id(hub)) or {}).get("betweenness")) for hub in hubs]
    if any(value is None for value in current) or max(current) <= 0:
        return None
    return round(100 * delta / max(current))


# --- ordering ------------------------------------------------------------------------------------


def _single_point_order(flag: dict, rows: dict) -> tuple:
    return (_sort_id(flag.get("station")),)


def _isolation_order(item: dict, rows: dict) -> tuple:
    """Highest isolation weight first (the item's own, else its station row's), unmeasured last."""
    station = _id(item.get("station"))
    weight = _measured(item, rows.get(station) or {}, "isolation_weight")
    return (weight is None, -(weight or 0.0), _sort_id(station))


def _ghost_order(flag: dict, rows: dict) -> tuple:
    chain = flag.get("stations") if isinstance(flag.get("stations"), list) else []
    return (-len(chain), _sort_id(chain[0] if chain else None))


def _orbital_order(suggestion: dict, rows: dict) -> tuple:
    closer = _number(suggestion.get("delta_mean_d_hub_m"))
    return (closer is None, closer or 0.0, _sort_id(suggestion.get("from")), _sort_id(suggestion.get("to")))


# Type -> (sort key, sentence), in the order the types are shown.
_FLAG_RULES = {
    "single_point_of_failure": (_single_point_order, _single_point_of_failure),
    "isolated_high_demand": (_isolation_order, _isolated_high_demand),
    "ghost_line": (_ghost_order, _ghost_line),
}
_SIMULATION_RULES = {
    "orbital_link": (_orbital_order, _orbital_link),
    "feeder_corridor": (_isolation_order, _feeder_corridor),
}


def _notes(kind: str, items, rules: dict, rows: dict, entry: dict, limit: int) -> list[dict]:
    """The notes for `items` of the types in `rules`, ranked, the first `limit` that can be
    written (an item that cannot does not take a place)."""
    if not isinstance(items, list):
        return []
    types = list(rules)
    ranked = []
    for item in items:
        kind_of = item.get("type") if isinstance(item, dict) else None
        if isinstance(kind_of, str) and kind_of in rules:
            ranked.append(((types.index(kind_of), *rules[kind_of][0](item, rows)), item))
    ranked.sort(key=lambda pair: pair[0])
    notes = []
    for _, item in ranked:
        if len(notes) >= limit:
            break
        written = rules[item["type"]][1](item, rows, entry)
        if written is not None:
            stations, text = written
            notes.append({"kind": kind, "type": item["type"], "stations": stations, "text": text})
    return notes


# --- reading the entry ---------------------------------------------------------------------------


def _station_rows(stations) -> dict[str, dict]:
    if not isinstance(stations, list):
        return {}
    return {row["id"]: row for row in stations if isinstance(row, dict) and _id(row.get("id")) is not None}


def _id(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _ids(*values) -> list[str]:
    return [value for value in values if _id(value) is not None]


def _sort_id(value) -> str:
    return value if isinstance(value, str) else ""


def _name(rows: dict, station: str | None, carried) -> str | None:
    """The station's name as its row stores it, else the one the flag or suggestion carries,
    cleaned (`_clean_name`) and in double quotation marks, so a reader, and the summary model, sees
    where an OpenStreetMap name starts and ends; None when neither has one."""
    row = rows.get(station) if station is not None else None
    for value in ((row or {}).get("name"), carried):
        name = _clean_name(value)
        if name is not None:
            return f'"{name}"'
    return None


# What a quoted name may not carry: double quotation marks of any kind become single (they would
# close the name's quotes), angle brackets and backticks go (HTML tags, code spans) and square
# brackets become round (a markdown link needs them). Real station names have none of these.
_NAME_MARKUP = str.maketrans(
    {'"': "'", "\u201c": "'", "\u201d": "'", "\u201e": "'", "\u201f": "'", "\u00ab": "'", "\u00bb": "'",
     "<": None, ">": None, "`": None, "[": "(", "]": ")"}
)  # fmt: skip


def _clean_name(value) -> str | None:
    """`value` as a name fit to quote: common/osm.clean_name (controls, bidi overrides and isolates
    and every other formatting character out, except the joiners U+200C and U+200D that Persian,
    Arabic and Indic names are spelt with; at most osm.MAX_NAME_CHARS), whitespace collapsed again
    where it dropped something, every double quotation mark (straight or typographic) made single,
    so the name cannot close its own quotes, and HTML and markdown markup broken (_NAME_MARKUP: an
    OpenStreetMap name is anyone's edit and the article page renders HTML). None when nothing a
    reader would see is left, or it was not text. (common.figures.plain_text
    would also strip the two joiners and misspell those names.)"""
    name = osm.clean_name(value)
    if name is None:
        return None
    name = name.translate(_NAME_MARKUP)
    if not name.strip("\u200c\u200d \t"):
        return None  # only joiners: nothing a reader would see
    return " ".join(name.split()) or None


def _number(value) -> float | None:
    """A finite number as a float; None for anything else (None, a bool, text, NaN)."""
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _shown(value: float | None, places: int) -> str | None:
    """`value` as a note prints it, to `places` decimals; None when it was not measured or prints
    as zero or less (activity 0.00, a ratio of 0.0, 0 m): a clause that rounds to nothing is left
    out, never written as a claim. Every figure shown this way is a size, never below zero."""
    if value is None:
        return None
    text = f"{value:.{places}f}"
    return text if float(text) > 0 else None


def _km(metres) -> str | None:
    """A distance measured in metres as the notes print it, in km to one decimal; None when it was
    not measured or rounds to 0.0 km."""
    value = _number(metres)
    return _shown(value / 1000 if value is not None else None, 1)


def _whole(value) -> int | None:
    number = _number(value)
    return int(number) if number is not None and number.is_integer() and number >= 0 else None


def _first(*values):
    return next((value for value in values if value is not None), None)


def _measured(item: dict, row: dict, key: str) -> float | None:
    """`key` as the flag or suggestion carries it, else as its station row has it."""
    return _number(_first(item.get(key), row.get(key)))


def _about(entry: dict) -> str:
    """"about " before a figure read from betweenness the analysis estimated from a sample."""
    flags = entry.get("quality_flags")
    return "about " if isinstance(flags, list) and SAMPLED_BETWEENNESS_FLAG in flags else ""


def _and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _a_or_an(number: str) -> str:
    """The article before a number read aloud: "an 8.4", "an 11.0", "an 18.2", else "a"."""
    whole = number.split(".")[0]
    return "an" if whole.startswith("8") or whole in ("11", "18") else "a"
