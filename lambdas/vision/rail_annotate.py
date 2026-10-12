"""The rail access figure: the heat map over the scene, with deserts, lines, stations, flags and
suggested links drawn, and a legend.

The red band, contrast-stretched, carries the city's shape; the heat map is laid over it in an
inferno colour map where the scene was usable, and the unmeasured parts get annotate.py's slate
tint so a cloud never looks like an empty suburb. Deserts are outlined, the graph's edges are
drawn thicker the more paths run through them, stations larger the busier their catchment and
with a thicker ring in the top quarter of isolation weight, and hubs marked. The analysis's
flags and suggestions are on the figure too, since they are what the entry promises: a flagged
station gets a second, red ring, a ghost line a red polyline through its stations, and a
simulated link (orbital or feeder) a dashed line in the hub green. A legend under the caption
bar names each symbol, on any figure large enough to read it (a city's, at 1024 px, always is).
Three caption lines are burned in: the processed-imagery notice, the Copernicus sentence and the
OpenStreetMap credit (the page carries the full attribution with its (c), which Hershey fonts
lack).
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from vision.annotate import (
    CAPTION_MIN_SCALE,
    CAPTION_PAD,
    FONT,
    UNMEASURED_TINT,
    burn_caption,
    caption_bar_size,
    caption_font_scale,
    encode_png,
)
from vision.detect import stretch
from vision.rail_analyse import RailAnalysis

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
GHOST_LINE_THICKNESS = 2
# Suggestions: a proposed link belongs to the hub story, so it is dashed in the hub green; an
# orbital link (a new edge) is drawn thicker than a feeder corridor (an existing path to a hub).
SUGGESTION_COLOUR = (80, 255, 80)
ORBITAL_THICKNESS, FEEDER_THICKNESS = 2, 1
# OpenCV has no dashed line: segments this long with this gap stand in for one.
DASH_ON, DASH_OFF = 6, 4
# The legend: one line per symbol, a glyph then its words, in a black box under the caption bar
# with this many rows of the image showing between the two.
LEGEND_MARGIN = 1
# The legend may cover at most these shares of the image's height and width. Its font is the
# caption's, shrunk until the box fits but never below the caption's own smallest scale: a figure
# too small for a legible legend (under about 400 px; a city's figure is 1024) gets none, since an
# unreadable legend only hides the map.
LEGEND_MAX_HEIGHT, LEGEND_MAX_WIDTH = 0.25, 0.75
# Each step down in font scale while the legend does not fit.
LEGEND_SHRINK = 0.95
LEGEND_LINES = (
    ("station", "station, larger = busier catchment, thick ring = high isolation weight"),
    ("hub", "hub"),
    ("flag", "flagged (isolated high demand, single point of failure, ghost line)"),
    ("suggestion", "suggested link"),
    ("desert", "beyond walking reach"),
)
CAPTION_LINES = (
    "Processed imagery - BloggerBear",
    "Contains modified Copernicus Sentinel data {year}",
    "Map data (c) OpenStreetMap contributors",
)


def annotate_rail(analysis: RailAnalysis, red: np.ndarray, year: str | int, max_side: int = 1024) -> bytes:
    """PNG bytes of the figure, scaled so the longer side is at most `max_side`. Drawn in this
    order: deserts, edges, suggested links, ghost lines, stations, flag rings, hubs, the legend,
    the caption. A flag or suggestion naming a station without a position, or an id the graph
    does not hold, draws nothing; every other part of it is still drawn."""
    layers, metrics = analysis.layers, analysis.metrics
    heat, usable, desert = layers["heat"], layers["usable"], layers["desert"]
    if red.shape != heat.shape:
        raise ValueError("the red band must be on the analysis grid")

    scene_bgr = cv2.cvtColor(stretch(red, STRETCH_MAX), cv2.COLOR_GRAY2BGR)
    heat_bgr = cv2.applyColorMap(np.clip(heat * 255.0, 0, 255).astype(np.uint8), cv2.COLORMAP_INFERNO)
    blended = cv2.addWeighted(scene_bgr, SCENE_WEIGHT, heat_bgr, HEAT_WEIGHT, 0)
    tint = np.empty_like(scene_bgr)
    tint[:] = UNMEASURED_TINT
    tinted = cv2.addWeighted(scene_bgr, 0.4, tint, 0.6, 0)
    image = np.where((usable > 0)[:, :, None], blended, tinted)

    h, w = image.shape[:2]
    scale = min(1.0, max_side / float(max(h, w)))
    if scale < 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        image = cv2.resize(image, size, interpolation=cv2.INTER_AREA)
    image = np.ascontiguousarray(image)

    def at(x, y):
        return int(round(x * scale)), int(round(y * scale))

    # Outline the deserts the metrics report: clusters of at least min_desert_km2, not every speck.
    params = metrics["params"]
    min_px = params["min_desert_km2"] / (params["pixel_size_m"] / 1000.0) ** 2
    count, labels, stats, _ = cv2.connectedComponentsWithStats(desert, connectivity=8)
    large = np.flatnonzero(stats[:, cv2.CC_STAT_AREA] >= min_px)
    reported = np.isin(labels, large[large > 0]).astype(np.uint8)
    contours, _ = cv2.findContours(reported, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        scaled = [np.round(c.astype(np.float64) * scale).astype(np.int32) for c in contours]
        cv2.drawContours(image, scaled, -1, DESERT_COLOUR, 1)

    graph = layers["graph"]
    top = max((e["betweenness"] for e in metrics["edges"]), default=0.0)
    for edge in metrics["edges"]:
        a, b = graph.nodes[edge["from"]], graph.nodes[edge["to"]]
        thickness = 1 + (round(3 * edge["betweenness"] / top) if top > 0 else 0)
        cv2.line(image, at(a["x"], a["y"]), at(b["x"], b["y"]), EDGE_COLOUR, thickness, cv2.LINE_AA)

    # Suggested links, between graph nodes as the edges are; an id the graph lacks draws nothing.
    for suggestion in metrics["suggestions"]:
        if suggestion.get("type") == "orbital_link":
            ends, thickness = (suggestion.get("from"), suggestion.get("to")), ORBITAL_THICKNESS
        elif suggestion.get("type") == "feeder_corridor":
            ends, thickness = (suggestion.get("station"), suggestion.get("hub")), FEEDER_THICKNESS
        else:
            continue
        if all(end in graph for end in ends):
            a, b = graph.nodes[ends[0]], graph.nodes[ends[1]]
            _dashed_line(image, at(a["x"], a["y"]), at(b["x"], b["y"]), SUGGESTION_COLOUR, thickness)

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
    for sid in sorted(sid for sid in flagged if sid in radius_of):
        radius = radius_of[sid] + FLAG_RING_OFFSET
        cv2.circle(image, at(*position_of[sid]), radius, FLAG_COLOUR, FLAG_RING_THICKNESS, cv2.LINE_AA)

    for sid in layers["hubs"]:
        if sid in radius_of:
            cv2.drawMarker(image, at(*position_of[sid]), HUB_COLOUR, cv2.MARKER_DIAMOND, HUB_MARKER_SIZE, 2)

    lines = [line.format(year=year) for line in CAPTION_LINES]
    # cv2.rectangle fills both corners, so the caption bar's last row is its height.
    burn_legend(image, top=caption_bar_size(image, lines)[1] + 1 + LEGEND_MARGIN)
    burn_caption(image, lines)
    return encode_png(image)


def _dashed_line(
    image: np.ndarray, a, b, colour, thickness: int, on: int = DASH_ON, off: int = DASH_OFF
) -> None:
    """A dashed line from pixel `a` to pixel `b` (both (x, y)): `on` px drawn, `off` px left,
    from `a` onwards. An anti-aliased line's end caps reach half its thickness and half a pixel
    past each end, so each dash is drawn that much shorter and the gaps stay `off` px wide."""
    (ax, ay), (bx, by) = a, b
    length = math.hypot(bx - ax, by - ay)
    if length == 0:
        return
    ux, uy = (bx - ax) / length, (by - ay) / length
    cap = thickness / 2.0 + 0.5
    start = 0.0
    while start < length:
        end = min(start + on, length)
        first, last = start + cap, end - cap
        if last > first:
            p = (round(ax + ux * first), round(ay + uy * first))
            q = (round(ax + ux * last), round(ay + uy * last))
            cv2.line(image, p, q, colour, thickness, cv2.LINE_AA)
        start = end + off


def burn_legend(image: np.ndarray, top: int = 0) -> np.ndarray:
    """Burn the legend (`LEGEND_LINES`: a glyph, then its words) on a black box at the left edge
    of `image` (BGR, in place) from row `top` down. The font is the caption's, shrunk until the
    box covers no more than `LEGEND_MAX_HEIGHT` of the height and `LEGEND_MAX_WIDTH` of the
    width; when that would take it below `CAPTION_MIN_SCALE`, nothing is drawn."""
    h, w = image.shape[:2]
    scale = caption_font_scale(image)
    while True:
        sizes = [cv2.getTextSize(text, FONT, scale, 1) for _, text in LEGEND_LINES]
        glyph = max(4, max(th for (_, th), _ in sizes))  # the glyph cell: a text height tall, twice as wide
        width = 3 * CAPTION_PAD + 2 * glyph + max(tw for (tw, _), _ in sizes)
        height = sum(max(th, glyph) + base + CAPTION_PAD for (_, th), base in sizes) + CAPTION_PAD
        # cv2.rectangle fills both corners: the box is one px wider and taller than its size.
        if width + 1 <= LEGEND_MAX_WIDTH * w and height + 1 <= LEGEND_MAX_HEIGHT * h:
            break
        if scale <= CAPTION_MIN_SCALE:
            return image
        scale = max(CAPTION_MIN_SCALE, scale * LEGEND_SHRINK)
    cv2.rectangle(image, (0, top), (width, top + height), (0, 0, 0), thickness=-1)
    y = top + CAPTION_PAD
    for (kind, text), ((_, th), base) in zip(LEGEND_LINES, sizes, strict=True):
        row = max(th, glyph)
        _legend_glyph(image, kind, (CAPTION_PAD + glyph, y + row // 2), glyph)
        origin = (2 * CAPTION_PAD + 2 * glyph, y + (row + th) // 2)
        cv2.putText(image, text, origin, FONT, scale, (255, 255, 255), 1, cv2.LINE_AA)
        y += row + base + CAPTION_PAD
    return image


def _legend_glyph(image: np.ndarray, kind: str, centre: tuple[int, int], size: int) -> None:
    """The legend's symbol for `kind`, drawn as the figure draws it, centred in a cell `size`
    tall and twice as wide."""
    cx, cy = centre
    radius = max(3, size // 2)  # a 2 px ring smaller than this fills in and reads as a dot
    if kind == "station":
        cv2.circle(image, centre, radius, STATION_COLOUR, 1, cv2.LINE_AA)
    elif kind == "hub":
        cv2.drawMarker(image, centre, HUB_COLOUR, cv2.MARKER_DIAMOND, max(4, size), 2)
    elif kind == "flag":
        cv2.circle(image, centre, radius, FLAG_COLOUR, FLAG_RING_THICKNESS, cv2.LINE_AA)
    elif kind == "suggestion":
        _dashed_line(image, (cx - size, cy), (cx + size, cy), SUGGESTION_COLOUR, ORBITAL_THICKNESS)
    elif kind == "desert":
        cv2.line(image, (cx - size, cy), (cx + size, cy), DESERT_COLOUR, 1)
