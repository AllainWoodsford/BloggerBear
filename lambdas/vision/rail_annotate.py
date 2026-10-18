"""The rail access figure: the heat map over the scene, with deserts, lines, stations, hubs, flags
and theoretical links drawn, and the caption and a legend in a strip under it; and the transit
reach figure (`annotate_modes`): the same scene in grey with the built-up ground filled by what
reaches it, a station, a stop or neither.

The red band, contrast-stretched, carries the city's shape; the heat map is laid over it in an
inferno colour map where the scene was usable, and the unmeasured parts get annotate.py's slate
tint so a cloud never looks like an empty suburb. Deserts are outlined, the graph's edges are
drawn thicker the more paths run through them, stations larger the busier their catchment and
with a thicker ring in the top quarter of isolation weight, and hubs marked. The analysis's
flags and suggestions are on the figure too, since they are what the entry promises: a flagged
station gets a second, red ring (on a hub, clear of the hub's marker), a ghost line a red
polyline through its stations, and a theoretical link (orbital or feeder) a dashed line in the
hub green, or in WET_LINK_COLOUR when its straight line is mostly over water. Nothing else is
drawn on the map: the words go in a dark strip stacked under it, so they never cover a station,
and the image grows by the strip's height. The strip holds the three caption lines at its
top-left (the processed-imagery notice, the Copernicus sentence and the OpenStreetMap credit;
the page carries the full attribution with its (c), which Hershey fonts lack), then, on every
figure at least LEGEND_MIN_WIDTH (256 px) wide, the legend, its entries in as many columns as
fit the width (three on a city's 1024 px figure, one per row on the narrowest). Both are set in
the caption's font at the caption's scale (never below CAPTION_MIN_SCALE). The PNG is encoded
at zlib's strongest level, and a figure over FIGURE_BYTE_BUDGET is drawn again smaller, so a
crowded, noisy city still fits the pipeline's cap.

The transit reach figure keeps the grey scene and the slate tint and drops the heat: three
translucent fills over the built-up pixels only (within a station's reach; beyond it but within
a stop's; beyond both, outlined so it reads without colour), the rail lines thin in white and
the stations as small white rings, no hubs, flags or links, and the same strip with its own
legend, which names each reach in metres and in walking minutes at the contract's
WALK_M_PER_MIN. It has its own, smaller byte budget: flat fills compress well.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import cv2
import numpy as np

from common.vision_contract import WALK_M_PER_MIN
from vision.annotate import (
    CAPTION_BACKGROUND,
    CAPTION_PAD,
    CAPTION_TEXT,
    FONT,
    UNMEASURED_TINT,
    burn_caption,
    caption_bar_size,
    caption_font_scale,
    encode_png,
)
from vision.detect import stretch
from vision.rail_analyse import MOSTLY_WATER, RailAnalysis

# Reflectance mapped to 255 in the red band: L2A x 10000, bright roofs around 3000.
STRETCH_MAX = 3000.0
# How much of the heat map shows through the scene.
HEAT_WEIGHT, SCENE_WEIGHT = 0.55, 0.45
DESERT_COLOUR = (0, 215, 255)  # BGR amber, as the ship boxes
EDGE_COLOUR = (255, 240, 170)  # BGR pale cyan: nothing in inferno is that colour
STATION_COLOUR = (255, 255, 255)
HUB_COLOUR = (80, 255, 80)  # BGR green
HUB_MARKER_SIZE = 14
# A station ring is this thick in the top quarter of isolation weight (the quartile taken as
# rail_analyse._quartiles takes it, over the stations that have a weight) and 1 otherwise; with
# no spread there is no top quarter and every ring stays 1.
ISOLATED_RING_THICKNESS = 2
# Flags: nothing else in the figure is red, and the inferno map's reds are darker and never a
# thin ring. A flagged station's second ring sits outside the largest station ring.
FLAG_COLOUR = (60, 60, 255)  # BGR red
FLAG_RING_OFFSET, FLAG_RING_THICKNESS = 4, 2
# A flagged hub's ring is never smaller than this, or the hub marker drawn over it (its 2 px line
# reaching a pixel past half HUB_MARKER_SIZE) would cover most of it on a quiet station.
HUB_FLAG_RING_RADIUS = HUB_MARKER_SIZE // 2 + 3
GHOST_LINE_THICKNESS = 2
# Suggestions: a theoretical link belongs to the hub story, so it is dashed in the hub green; an
# orbital link (a new edge) is drawn thicker than a feeder corridor (an existing path to a hub).
SUGGESTION_COLOUR = (80, 255, 80)
ORBITAL_THICKNESS, FEEDER_THICKNESS = 2, 1
# An orbital link whose straight line is mostly over water (water_share at least MOSTLY_WATER)
# is dashed in azure instead: nothing else on the map is blue (the hub green, the red flags, the
# amber deserts and the inferno map run from black through purple and red to yellow; the edges
# are a pale cyan, far lighter and greyer than this).
WET_LINK_COLOUR = (255, 160, 0)  # BGR azure
# OpenCV has no dashed line: segments this long with this gap stand in for one.
DASH_ON, DASH_OFF = 6, 4
# The legend: rows in the caption's colours under the caption, in the strip under the map, so it
# hides nothing however small the figure. Each entry is a glyph, drawn as the map draws its
# symbol, then one or two lines of words, set in the caption's font at the caption's scale (which
# grows with the width and never goes below CAPTION_MIN_SCALE). The widest entry is about 170 px
# at that smallest scale, so every figure at least LEGEND_MIN_WIDTH wide fits one column; a
# narrower one (a thumbnail) gets the caption alone. The entries fill as many columns as fit, row
# by row: three at 1024 px, two at 400, one per row at 256.
LEGEND_MIN_WIDTH = 256
# Columns fit when LEGEND_MIN_GAP glyph sizes (a text height each) separate them, and are spread
# up to LEGEND_GAP apart. A Hershey font's text sizes step with its scale, so a fit asking for the
# full two fell from three columns to two just past some of the steps as the width grew (at 622,
# 681, 918 and 977 px); with one glyph size of slack the count never falls as the width grows.
LEGEND_MIN_GAP, LEGEND_GAP = 1, 2
# A 1 px rule between the caption and the legend, dimmer than either, so the caption's last line
# does not read as a legend entry.
RULE_COLOUR = (96, 96, 96)
LEGEND_ENTRIES = (
    ("station", ("station, larger = busier catchment,", "thick ring = high isolation weight")),
    ("hub", ("hub",)),
    ("flag", ("flagged (isolated high demand,", "single point of failure, ghost line)")),
    # "theoretical", as the planner notes and the articles say: a what-if on the mapped graph
    # and straight lines, never a route or a recommendation.
    ("suggestion", ("theoretical link",)),
    ("desert", ("beyond walking reach",)),
    # Last, under the flag entry of the same width, so a 1024 px figure keeps its three columns.
    ("wet_suggestion", ("theoretical link (mostly over water)",)),
)
CAPTION_LINES = (
    "Processed imagery - BloggerBear",
    "Contains modified Copernicus Sentinel data {year}",
    "Map data (c) OpenStreetMap contributors",
)
# The transit reach figure's fills (BGR), each blended at FILL_ALPHA over the grey scene on the
# built-up pixels only. Chosen with the dataviz skill's validator: the three hues are the first
# slots of its palette that pass the all-pairs colour-vision checks together (aqua #1baf7a,
# orange #eb6834, violet #6250d6: worst pair a deuteranopia Delta E of 9.2, over the 8 target;
# 22.6 from the slate tint under normal vision), and after blending they still read apart from
# each other, from the slate tint (a dark, greyish blue) and from the white lines and rings,
# which was checked by eye on the synthetic city. The residual gets an outline too, so it reads
# without colour; the outline is the violet's own pale step, lighter than every fill.
STATION_FILL = (122, 175, 27)  # BGR aqua: built up, within a station's reach
STOP_FILL = (52, 104, 235)  # BGR orange: beyond that, within a stop's reach
BEYOND_FILL = (214, 80, 98)  # BGR violet: beyond both
BEYOND_OUTLINE = (245, 191, 200)  # BGR pale violet, 1 px round every residual patch
FILL_ALPHA = 0.55
# The legend's swatch of a fill is the fill blended over this mid grey, as it looks over roofs.
SWATCH_GROUND = 128
# The rail lines and stations on the transit reach figure: thin and small, in white, so the fills
# carry the figure and the network only places them.
MODES_EDGE_THICKNESS, MODES_STATION_RADIUS = 1, 3
# zlib's strongest level: the synthetic city's figure in less than half the bytes of OpenCV's
# fast default (50,251 against 112,077) and the 1024 px city's in about a third (138,398 against
# 402,077), for about 0.4 s more encoding a 1024 px figure.
PNG_COMPRESSION = 9
# The figure's byte budget. The pipeline refuses a figure over MAX_IMAGE_BYTES (3 MiB,
# common/vision_client.py, which the worker does not import). The worst case the tests build
# (test_vision_rail_core.crowded_city: 2,000 stations each joined to its two nearest neighbours,
# 400 flags, 10 suggestions, a maximum-entropy red band, all of it usable, a 1024 px map) was
# 2,767,213 bytes with OpenCV's default compression and the caption on the map: 12 % under that
# cap. At PNG_COMPRESSION, with the caption in the strip, it is 2,458,747 bytes at 1024 px, over
# this budget, so it is drawn again at 0.8x, an 819 px map: 1,505,542 bytes, under half the cap.
# A figure over the budget is drawn again with its map FIGURE_SHRINK times as large until it
# fits; at FIGURE_MIN_SIDE (the map's longer side) it is returned as it is, even over the budget,
# since the cap it must meet is still the client's 3 MiB (the worst case at 512 px is 608,842
# bytes).
FIGURE_BYTE_BUDGET = 2 * 1024 * 1024
FIGURE_SHRINK, FIGURE_MIN_SIDE = 0.8, 512
# The transit reach figure's budget: a quarter of the map's. Its fills are flat and its scene
# grey, so it compresses far better than the inferno blend: the synthetic city's is 15,411 bytes
# against the rail map's 50,251, the 1024 px city's 34,506 against 138,398. The crowded city's
# worst case (the same maximum-entropy red band, which no fill flattens, 2,000 stations and their
# edges) is 1,835,228 bytes at 1024 px, so it is drawn again at 819 px (1,148,282), 655 px
# (735,917) and 524 px (474,521, under the budget), four draws and encodes in 0.9 s; at the
# 512 px floor it is 467,836 bytes. A real scene is nothing like that noise: Sydney's compresses
# like the synthetic city's.
MODES_FIGURE_BYTE_BUDGET = 512 * 1024


def annotate_rail(analysis: RailAnalysis, red: np.ndarray, year: str | int, max_side: int = 1024) -> bytes:
    """PNG bytes of the figure: the map, scaled so its longer side is at most `max_side`, with the
    strip (`figure_strip`: the caption, then the legend) stacked under it, so the image is taller
    than the map by the strip's height and nothing but the analysis is drawn on the map. Drawn on
    the map in this order: deserts, edges, theoretical links, ghost lines, stations, flag rings,
    hubs. A flag or suggestion naming a station without a position, or an id the graph does not
    hold, draws nothing; every other part of it is still drawn. A figure over FIGURE_BYTE_BUDGET
    is drawn again with its map FIGURE_SHRINK times as large, until it fits or the map's longer
    side is down to FIGURE_MIN_SIDE."""
    layers, metrics = analysis.layers, analysis.metrics
    if red.shape != layers["heat"].shape:
        raise ValueError("the red band must be on the analysis grid")
    scene = _scene(layers, red)
    # Outline the deserts the metrics report: clusters of at least min_desert_km2, not every speck.
    params = metrics["params"]
    min_px = params["min_desert_km2"] / (params["pixel_size_m"] / 1000.0) ** 2
    count, labels, stats, _ = cv2.connectedComponentsWithStats(layers["desert"], connectivity=8)
    large = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] >= min_px)
    reported = np.isin(labels, large[large > 0]).astype(np.uint8)
    deserts, _ = cv2.findContours(reported, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    caption = [line.format(year=year) for line in CAPTION_LINES]
    longer = max(scene.shape[:2])
    side = min(max_side, longer)
    while True:
        image = _draw_map(scene, deserts, analysis, side / longer)
        png = encode_png(np.vstack((image, figure_strip(image, caption))), PNG_COMPRESSION)
        if len(png) <= FIGURE_BYTE_BUDGET or side <= FIGURE_MIN_SIDE:
            return png
        side = max(FIGURE_MIN_SIDE, int(side * FIGURE_SHRINK))


def annotate_modes(
    analysis: RailAnalysis, red: np.ndarray, year: str | int, max_side: int = 1024
) -> bytes | None:
    """PNG bytes of the transit reach figure, or None when the analysis has no stops layer (the
    quality flag no_stops_layer: nothing was measured, so nothing is drawn). The grey scene with
    the unmeasured tint, the built-up pixels filled by what reaches them (STATION_FILL within
    a station's reach, STOP_FILL beyond it but within a stop's, BEYOND_FILL and a BEYOND_OUTLINE
    contour beyond both), the rail lines thin in white and the stations as small white rings, the
    map scaled so its longer side is at most `max_side`, and `figure_strip` under it with the
    caption and `modes_legend_entries`. A figure over MODES_FIGURE_BYTE_BUDGET is drawn again
    with its map FIGURE_SHRINK times as large, until it fits or the map's longer side is down to
    FIGURE_MIN_SIDE, as `annotate_rail` does."""
    layers, metrics = analysis.layers, analysis.metrics
    if "no_stops_layer" in metrics["quality_flags"]:
        return None
    if red.shape != layers["built"].shape:
        raise ValueError("the red band must be on the analysis grid")
    scene = _ground(layers, red)
    built, desert, stop = layers["built"] > 0, layers["desert"] > 0, layers["stop_reach"] > 0
    beyond = desert & ~stop
    for mask, colour in ((built & ~desert, STATION_FILL), (desert & stop, STOP_FILL), (beyond, BEYOND_FILL)):
        _fill(scene, mask, colour)
    outlines, _ = cv2.findContours(beyond.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    caption = [line.format(year=year) for line in CAPTION_LINES]
    entries = modes_legend_entries(metrics["params"])
    longer = max(scene.shape[:2])
    side = min(max_side, longer)
    while True:
        image = _draw_modes(scene, outlines, analysis, side / longer)
        png = encode_png(np.vstack((image, figure_strip(image, caption, entries))), PNG_COMPRESSION)
        if len(png) <= MODES_FIGURE_BYTE_BUDGET or side <= FIGURE_MIN_SIDE:
            return png
        side = max(FIGURE_MIN_SIDE, int(side * FIGURE_SHRINK))


def modes_legend_entries(params: dict) -> tuple:
    """The transit reach figure's legend entries for the analysis's echoed `params`: each reach
    in metres (or whole kilometres) and in walking minutes at WALK_M_PER_MIN."""
    station, stop = _walk_text(params["reach_m"]), _walk_text(params["stop_reach_m"])
    return (
        ("fill_station", (f"built up, within {station}", "of a station")),
        ("fill_stop", (f"beyond that, within {stop}", "of a bus, tram or ferry stop")),
        ("fill_beyond", ("beyond both (outlined)",)),
        ("line", ("rail line, station",)),
    )


def _walk_text(metres: float) -> str:
    """"1 km (about 12 min walk)" or "400 m (5 min walk)": a straight-line reach restated as
    minutes at WALK_M_PER_MIN, "about" when the minutes are not whole; never a journey time."""
    if metres >= 1000 and metres % 1000 == 0:
        distance = f"{metres / 1000:g} km"
    else:
        distance = f"{metres:g} m"
    minutes = metres / WALK_M_PER_MIN
    whole = max(1, round(minutes))
    about = "" if minutes == whole else "about "
    return f"{distance} ({about}{whole} min walk)"


def _scene(layers: dict, red: np.ndarray) -> np.ndarray:
    """The heat map over the stretched red band where the scene was usable, the unmeasured tint
    elsewhere: BGR on the analysis grid."""
    heat = layers["heat"]
    scene_bgr = cv2.cvtColor(stretch(red, STRETCH_MAX), cv2.COLOR_GRAY2BGR)
    heat_bgr = cv2.applyColorMap(np.clip(heat * 255.0, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    blended = cv2.addWeighted(scene_bgr, SCENE_WEIGHT, heat_bgr, HEAT_WEIGHT, 0)
    return _tint_unmeasured(blended, scene_bgr, layers["usable"])


def _ground(layers: dict, red: np.ndarray) -> np.ndarray:
    """The stretched red band in grey where the scene was usable, the unmeasured tint elsewhere:
    BGR on the analysis grid, the transit reach figure's scene (no heat)."""
    scene_bgr = cv2.cvtColor(stretch(red, STRETCH_MAX), cv2.COLOR_GRAY2BGR)
    return _tint_unmeasured(scene_bgr, scene_bgr, layers["usable"])


def _tint_unmeasured(image: np.ndarray, scene_bgr: np.ndarray, usable: np.ndarray) -> np.ndarray:
    """`image` where the scene was usable, `scene_bgr` under the slate tint elsewhere."""
    tint = np.empty_like(scene_bgr)
    tint[:] = UNMEASURED_TINT
    tinted = cv2.addWeighted(scene_bgr, 0.4, tint, 0.6, 0)
    return np.where((usable > 0)[:, :, None], image, tinted)


def _fill(image: np.ndarray, mask: np.ndarray, colour: tuple[int, int, int]) -> None:
    """Blend `colour` at FILL_ALPHA into `image` (BGR, in place) where `mask` is set."""
    tint = np.empty_like(image)
    tint[:] = colour
    blended = cv2.addWeighted(image, 1.0 - FILL_ALPHA, tint, FILL_ALPHA, 0)
    image[mask] = blended[mask]


def _swatch(colour: tuple[int, int, int]) -> tuple[int, int, int]:
    """A fill as the legend shows it: blended at FILL_ALPHA over SWATCH_GROUND grey."""
    return tuple(int(round(FILL_ALPHA * c + (1.0 - FILL_ALPHA) * SWATCH_GROUND)) for c in colour)


def _draw_modes(scene: np.ndarray, outlines, analysis: RailAnalysis, scale: float) -> np.ndarray:
    """The transit reach map: the filled `scene` scaled by `scale` (at most 1), the residual's
    `outlines` (contours on the analysis grid), the rail lines and the stations drawn on it."""
    layers, metrics = analysis.layers, analysis.metrics
    if scale < 1.0:
        h, w = scene.shape[:2]
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(scene, size, interpolation=cv2.INTER_AREA)
    else:
        image = scene.copy()  # the scene may be drawn again at another scale

    def at(x, y):
        return int(round(x * scale)), int(round(y * scale))

    if outlines:
        scaled = [np.round(c.astype(np.float64) * scale).astype(np.int32) for c in outlines]
        cv2.drawContours(image, scaled, -1, BEYOND_OUTLINE, 1)
    graph = layers["graph"]
    for edge in metrics["edges"]:
        a, b = graph.nodes[edge["from"]], graph.nodes[edge["to"]]
        ends = at(a["x"], a["y"]), at(b["x"], b["y"])
        cv2.line(image, *ends, STATION_COLOUR, MODES_EDGE_THICKNESS, cv2.LINE_AA)
    for position in layers["stations_px"]:
        if position is not None:
            cv2.circle(image, at(*position), MODES_STATION_RADIUS, STATION_COLOUR, 1, cv2.LINE_AA)
    return image


def _draw_map(scene: np.ndarray, deserts, analysis: RailAnalysis, scale: float) -> np.ndarray:
    """The map: `scene` scaled by `scale` (at most 1), the analysis drawn on it."""
    layers, metrics = analysis.layers, analysis.metrics
    if scale < 1.0:
        h, w = scene.shape[:2]
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(scene, size, interpolation=cv2.INTER_AREA)
    else:
        image = scene.copy()  # the scene may be drawn again at another scale

    def at(x, y):
        return int(round(x * scale)), int(round(y * scale))

    if deserts:
        scaled = [np.round(c.astype(np.float64) * scale).astype(np.int32) for c in deserts]
        cv2.drawContours(image, scaled, -1, DESERT_COLOUR, 1)

    graph = layers["graph"]
    top = max((e["betweenness"] for e in metrics["edges"]), default=0.0)
    for edge in metrics["edges"]:
        a, b = graph.nodes[edge["from"]], graph.nodes[edge["to"]]
        thickness = 1 + (round(3 * edge["betweenness"] / top) if top > 0 else 0)
        cv2.line(image, at(a["x"], a["y"]), at(b["x"], b["y"]), EDGE_COLOUR, thickness, cv2.LINE_AA)

    # Theoretical links, between graph nodes as the edges are; an id the graph lacks draws
    # nothing. An orbital link mostly over water gets its own colour.
    for suggestion in metrics["suggestions"]:
        colour = SUGGESTION_COLOUR
        if suggestion.get("type") == "orbital_link":
            ends, thickness = (suggestion.get("from"), suggestion.get("to")), ORBITAL_THICKNESS
            if _is_wet(suggestion.get("water_share")):
                colour = WET_LINK_COLOUR
        elif suggestion.get("type") == "feeder_corridor":
            ends, thickness = (suggestion.get("station"), suggestion.get("hub")), FEEDER_THICKNESS
        else:
            continue
        if all(end in graph for end in ends):
            a, b = graph.nodes[ends[0]], graph.nodes[ends[1]]
            _dashed_line(image, at(a["x"], a["y"]), at(b["x"], b["y"]), colour, thickness)

    # Flags name stations; their positions are the projected ones (None outside the window).
    stations = metrics["stations"]
    position_of = {s["id"]: p for s, p in zip(stations, layers["stations_px"], strict=False)}
    flags = metrics["flags"]
    for flag in flags:
        if flag.get("type") != "ghost_line":
            continue
        points = [position_of.get(sid) for sid in flag.get("stations", ())]
        points = [at(x, y) for x, y in (p for p in points if p is not None)]
        if len(points) >= 2:
            polyline = np.asarray(points, np.int32).reshape(-1, 1, 2)
            cv2.polylines(image, [polyline], False, FLAG_COLOUR, GHOST_LINE_THICKNESS, cv2.LINE_AA)

    weights = np.array([s["isolation_weight"] for s in stations if s["isolation_weight"] is not None], float)
    low, high = np.percentile(weights, (25, 75)) if len(weights) else (0.0, 0.0)
    thick_from = float(high) if high > low else None  # no spread, no top quarter
    radius_of = {}
    for station in stations:
        position = position_of.get(station["id"])
        if position is None:
            continue
        radius = 2 + round(4 * (station["activity"] or 0.0))  # unmeasured (None) draws as the smallest
        radius_of[station["id"]] = radius
        weight = station["isolation_weight"]
        thick = thick_from is not None and weight is not None and weight >= thick_from
        thickness = ISOLATED_RING_THICKNESS if thick else 1
        cv2.circle(image, at(*position), radius, STATION_COLOUR, thickness, cv2.LINE_AA)

    flagged = {
        flag.get("station")
        for flag in flags
        if flag.get("type") in ("isolated_high_demand", "single_point_of_failure")
    }
    hubs = layers["hubs"]
    for sid in sorted(sid for sid in flagged if sid in radius_of):
        radius = radius_of[sid] + FLAG_RING_OFFSET
        if sid in hubs:
            radius = max(radius, HUB_FLAG_RING_RADIUS)
        cv2.circle(image, at(*position_of[sid]), radius, FLAG_COLOUR, FLAG_RING_THICKNESS, cv2.LINE_AA)

    for sid in hubs:
        if sid in radius_of:
            cv2.drawMarker(image, at(*position_of[sid]), HUB_COLOUR, cv2.MARKER_DIAMOND, HUB_MARKER_SIZE, 2)
    return image


def _is_wet(water_share) -> bool:
    """Whether a link's `water_share` (a number, else not measured) is at least MOSTLY_WATER."""
    measured = isinstance(water_share, int | float) and not isinstance(water_share, bool)
    return measured and water_share >= MOSTLY_WATER


def _dashed_line(
    image: np.ndarray, a, b, colour, thickness: int, on: int = DASH_ON, off: int = DASH_OFF
) -> None:
    """A dashed line from pixel `a` to pixel `b` (both (x, y)): `on` px drawn, `off` px left,
    from `a` onwards. An anti-aliased line's end caps reach half its thickness and half a pixel
    past each end, so each dash is drawn that much shorter and the gaps stay `off` px wide. Only
    the dashes that can touch the image are drawn (the segment is clipped to it first, the dashes
    still counted from `a`), so an end far outside costs nothing."""
    if on + off <= 0:
        raise ValueError("a dash and its gap must add up to at least one pixel")
    (ax, ay), (bx, by) = (int(v) for v in a), (int(v) for v in b)
    length = math.hypot(bx - ax, by - ay)
    if length == 0:
        return
    h, w = image.shape[:2]
    reach = thickness + 1  # how far past the image a line's pixels can still land in it
    visible, *ends = cv2.clipLine((-reach, -reach, w + 2 * reach, h + 2 * reach), (ax, ay), (bx, by))
    if not visible:
        return
    ux, uy = (bx - ax) / length, (by - ay) / length
    first_seen, last_seen = sorted((x - ax) * ux + (y - ay) * uy for x, y in ends)
    period = on + off
    # From a dash boundary a period before the clipped part (clipLine rounds to whole pixels) to a
    # period past it: the same dashes an unclipped draw would put inside the image.
    start = max(0, math.floor(first_seen / period) - 1) * period
    stop = min(length, last_seen + period)
    cap = thickness / 2.0 + 0.5
    while start < stop:
        end = min(start + on, length)
        first, last = start + cap, end - cap
        if last > first:
            p = (round(ax + ux * first), round(ay + uy * first))
            q = (round(ax + ux * last), round(ay + uy * last))
            cv2.line(image, p, q, colour, thickness, cv2.LINE_AA)
        start = end + off


def figure_strip(image: np.ndarray, caption: Sequence[str], entries: Sequence = LEGEND_ENTRIES) -> np.ndarray:
    """The strip to stack under a map as wide as `image` (BGR), in the caption's colours: the
    `caption` lines at its top-left, burned as `burn_caption` burns them into the ships figure,
    then, when the map is wide enough for one, a rule and the legend (`legend_strip` of
    `entries`: the rail map's LEGEND_ENTRIES unless told otherwise)."""
    w = image.shape[1]
    block = np.empty((caption_bar_size(image, caption)[1], w, 3), np.uint8)
    block[:] = CAPTION_BACKGROUND
    burn_caption(block, caption)
    legend = legend_strip(image, entries)
    if legend is None:
        return block
    rule = np.empty((1, w, 3), np.uint8)
    rule[:] = CAPTION_BACKGROUND
    rule[:, CAPTION_PAD : w - CAPTION_PAD] = RULE_COLOUR
    return np.vstack((block, rule, legend))


def legend_strip(image: np.ndarray, entries: Sequence = LEGEND_ENTRIES) -> np.ndarray | None:
    """The legend for a figure as wide as `image` (BGR): rows of the caption's background, in the
    caption's font at the caption's scale, the `entries` (a glyph kind and its lines of words
    each; the rail map's LEGEND_ENTRIES unless told otherwise) laid out row by row in as many
    columns as fit its width (as few rows as fit; each column as wide as its widest entry, the
    columns a glyph to two glyphs apart). None when `image` is narrower than `LEGEND_MIN_WIDTH`."""
    w = image.shape[1]
    if w < LEGEND_MIN_WIDTH:
        return None
    scale = caption_font_scale(image)
    sizes = [[cv2.getTextSize(line, FONT, scale, 1) for line in lines] for _, lines in entries]
    th = max(th for entry in sizes for (_, th), _ in entry)
    base = max(base for entry in sizes for _, base in entry)
    glyph = max(4, th)  # the glyph cell: a text height tall, three times as wide
    pitch = th + base + CAPTION_PAD  # one line of words, as the caption's lines are spaced
    indent = 3 * glyph + 2 * CAPTION_PAD  # where the words start, from the entry's left edge
    widths = [indent + max(tw for (tw, _), _ in entry) for entry in sizes]
    count = len(entries)
    for row_count in range(1, count + 1):
        columns = -(-count // row_count)
        column_widths = [max(widths[i::columns]) for i in range(columns)]
        room = w - 2 * CAPTION_PAD - sum(column_widths)  # for the gaps between the columns
        if columns == 1 or room >= LEGEND_MIN_GAP * glyph * (columns - 1):
            break
    # Between one column's widest words and the next column's glyphs: what the fit left, shared
    # out, up to LEGEND_GAP glyphs.
    gap = min(LEGEND_GAP * glyph, room // (columns - 1)) if columns > 1 else 0
    lefts = [CAPTION_PAD + sum(column_widths[:i]) + gap * i for i in range(columns)]
    rows = [entries[i : i + columns] for i in range(0, count, columns)]
    heights = [max(len(lines) for _, lines in row) * pitch for row in rows]
    # A pad above the first row and between rows, beyond the pad each line of words ends with.
    strip = np.empty((CAPTION_PAD + sum(heights) + CAPTION_PAD * (len(rows) - 1), w, 3), np.uint8)
    strip[:] = CAPTION_BACKGROUND
    top = CAPTION_PAD
    for row, height in zip(rows, heights, strict=True):
        for left, (kind, lines) in zip(lefts, row, strict=False):
            middle = top + ((len(lines) - 1) * pitch + th) // 2  # of the entry's words
            _legend_glyph(strip, kind, (left + 3 * glyph // 2, middle), glyph)
            for index, line in enumerate(lines):
                origin = (left + indent, top + index * pitch + th)
                cv2.putText(strip, line, origin, FONT, scale, CAPTION_TEXT, 1, cv2.LINE_AA)
        top += height + CAPTION_PAD
    return strip


def _legend_glyph(image: np.ndarray, kind: str, centre: tuple[int, int], size: int) -> None:
    """The legend's symbol for `kind`, drawn as the figure draws it, centred in a cell `size`
    tall and three times as wide."""
    cx, cy = centre
    radius = max(3, size // 2)  # a 2 px ring smaller than this fills in and reads as a dot
    half = 3 * size // 2 - 1  # a line's reach either side of the centre
    if kind == "station":
        # A small thin ring beside a larger thick one: both things a station's ring says.
        cv2.circle(image, (cx - 3 * size // 4, cy), max(2, size // 3), STATION_COLOUR, 1, cv2.LINE_AA)
        cv2.circle(image, (cx + size // 2, cy), radius, STATION_COLOUR, ISOLATED_RING_THICKNESS, cv2.LINE_AA)
    elif kind == "hub":
        cv2.drawMarker(image, centre, HUB_COLOUR, cv2.MARKER_DIAMOND, max(4, size), 2)
    elif kind == "flag":
        cv2.circle(image, centre, radius, FLAG_COLOUR, FLAG_RING_THICKNESS, cv2.LINE_AA)
    elif kind == "suggestion":
        _dashed_line(image, (cx - half, cy), (cx + half, cy), SUGGESTION_COLOUR, ORBITAL_THICKNESS)
    elif kind == "wet_suggestion":
        _dashed_line(image, (cx - half, cy), (cx + half, cy), WET_LINK_COLOUR, ORBITAL_THICKNESS)
    elif kind == "desert":
        cv2.line(image, (cx - half, cy), (cx + half, cy), DESERT_COLOUR, 1)
    elif kind in ("fill_station", "fill_stop", "fill_beyond"):
        # A swatch of the fill as it looks over mid-grey roofs; the residual's with its outline.
        fill = {"fill_station": STATION_FILL, "fill_stop": STOP_FILL, "fill_beyond": BEYOND_FILL}[kind]
        corners = (cx - half, cy - size // 2), (cx + half, cy + size // 2)
        cv2.rectangle(image, *corners, _swatch(fill), -1)
        if kind == "fill_beyond":
            cv2.rectangle(image, *corners, BEYOND_OUTLINE, 1)
    elif kind == "line":
        cv2.line(image, (cx - half, cy), (cx + half, cy), STATION_COLOUR, MODES_EDGE_THICKNESS, cv2.LINE_AA)
        cv2.circle(image, centre, MODES_STATION_RADIUS, STATION_COLOUR, 1, cv2.LINE_AA)
