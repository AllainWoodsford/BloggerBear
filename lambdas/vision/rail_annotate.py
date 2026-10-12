"""The rail access figure: the heat map over the scene, with deserts, lines, stations, hubs, flags
and simulated links drawn, and a legend under it.

The red band, contrast-stretched, carries the city's shape; the heat map is laid over it in an
inferno colour map where the scene was usable, and the unmeasured parts get annotate.py's slate
tint so a cloud never looks like an empty suburb. Deserts are outlined, the graph's edges are
drawn thicker the more paths run through them, stations larger the busier their catchment and
with a thicker ring in the top quarter of isolation weight, and hubs marked. The analysis's
flags and suggestions are on the figure too, since they are what the entry promises: a flagged
station gets a second, red ring (on a hub, clear of the hub's marker), a ghost line a red
polyline through its stations, and a simulated link (orbital or feeder) a dashed line in the hub
green. The legend is a dark strip appended under the map, so it never covers anything drawn: the
image grows by the strip's height, and the entries sit in as many columns as fit the width (three
on a city's 1024 px figure, one per row on the narrowest). Every figure at least
LEGEND_MIN_WIDTH (256 px) wide has one, in the caption's font at the caption's scale (never
below CAPTION_MIN_SCALE). Three caption lines are burned into the map's top-left corner: the
processed-imagery notice, the Copernicus sentence and the OpenStreetMap credit (the page carries
the full attribution with its (c), which Hershey fonts lack).
"""

from __future__ import annotations

import math

import cv2
import numpy as np

from vision.annotate import (
    CAPTION_BACKGROUND,
    CAPTION_PAD,
    CAPTION_TEXT,
    FONT,
    UNMEASURED_TINT,
    burn_caption,
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
# A flagged hub's ring is never smaller than this, or the hub marker drawn over it (its 2 px line
# reaching a pixel past half HUB_MARKER_SIZE) would cover most of it on a quiet station.
HUB_FLAG_RING_RADIUS = HUB_MARKER_SIZE // 2 + 3
GHOST_LINE_THICKNESS = 2
# Suggestions: a proposed link belongs to the hub story, so it is dashed in the hub green; an
# orbital link (a new edge) is drawn thicker than a feeder corridor (an existing path to a hub).
SUGGESTION_COLOUR = (80, 255, 80)
ORBITAL_THICKNESS, FEEDER_THICKNESS = 2, 1
# OpenCV has no dashed line: segments this long with this gap stand in for one.
DASH_ON, DASH_OFF = 6, 4
# The legend: a strip in the caption's colours appended under the map, never over it, so it hides
# nothing however small the figure. Each entry is a glyph, drawn as the map draws its symbol, then
# one or two lines of words, set in the caption's font at the caption's scale (which grows with
# the width and never goes below CAPTION_MIN_SCALE). The widest entry is about 170 px at that
# smallest scale, so every figure at least LEGEND_MIN_WIDTH wide fits one column; a narrower one
# (a thumbnail) gets no legend. The entries fill as many columns as fit, row by row: three at
# 1024 px, two at 400, one per row at 256.
LEGEND_MIN_WIDTH = 256
LEGEND_ENTRIES = (
    ("station", ("station, larger = busier catchment,", "thick ring = high isolation weight")),
    ("hub", ("hub",)),
    ("flag", ("flagged (isolated high demand,", "single point of failure, ghost line)")),
    ("suggestion", ("suggested link",)),
    ("desert", ("beyond walking reach",)),
)
CAPTION_LINES = (
    "Processed imagery - BloggerBear",
    "Contains modified Copernicus Sentinel data {year}",
    "Map data (c) OpenStreetMap contributors",
)


def annotate_rail(analysis: RailAnalysis, red: np.ndarray, year: str | int, max_side: int = 1024) -> bytes:
    """PNG bytes of the figure: the map, scaled so its longer side is at most `max_side`, with the
    legend strip (`legend_strip`) appended under it, so the image is taller than the map by the
    strip's height. Drawn on the map in this order: deserts, edges, suggested links, ghost lines,
    stations, flag rings, hubs, the caption. A flag or suggestion naming a station without a
    position, or an id the graph does not hold, draws nothing; every other part of it is still
    drawn."""
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
    hubs = layers["hubs"]
    for sid in sorted(sid for sid in flagged if sid in radius_of):
        radius = radius_of[sid] + FLAG_RING_OFFSET
        if sid in hubs:
            radius = max(radius, HUB_FLAG_RING_RADIUS)
        cv2.circle(image, at(*position_of[sid]), radius, FLAG_COLOUR, FLAG_RING_THICKNESS, cv2.LINE_AA)

    for sid in hubs:
        if sid in radius_of:
            cv2.drawMarker(image, at(*position_of[sid]), HUB_COLOUR, cv2.MARKER_DIAMOND, HUB_MARKER_SIZE, 2)

    burn_caption(image, [line.format(year=year) for line in CAPTION_LINES])
    strip = legend_strip(image)
    if strip is not None:
        image = np.vstack((image, strip))
    return encode_png(image)


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


def legend_strip(image: np.ndarray) -> np.ndarray | None:
    """The legend for a figure as wide as `image` (BGR): a strip of the caption's background to
    stack under it, in the caption's font at the caption's scale, `LEGEND_ENTRIES` laid out row
    by row in as many columns as fit its width (as few rows as fit; each column as wide as its
    widest entry). None when `image` is narrower than `LEGEND_MIN_WIDTH`."""
    w = image.shape[1]
    if w < LEGEND_MIN_WIDTH:
        return None
    scale = caption_font_scale(image)
    sizes = [[cv2.getTextSize(line, FONT, scale, 1) for line in lines] for _, lines in LEGEND_ENTRIES]
    th = max(th for entry in sizes for (_, th), _ in entry)
    base = max(base for entry in sizes for _, base in entry)
    glyph = max(4, th)  # the glyph cell: a text height tall, three times as wide
    pitch = th + base + CAPTION_PAD  # one line of words, as the caption's lines are spaced
    indent = 3 * glyph + 2 * CAPTION_PAD  # where the words start, from the entry's left edge
    widths = [indent + max(tw for (tw, _), _ in entry) for entry in sizes]
    gap = 2 * glyph  # between one column's widest words and the next column's glyphs
    count = len(LEGEND_ENTRIES)
    for row_count in range(1, count + 1):
        columns = -(-count // row_count)
        column_widths = [max(widths[i::columns]) for i in range(columns)]
        if columns == 1 or 2 * CAPTION_PAD + sum(column_widths) + gap * (columns - 1) <= w:
            break
    lefts = [CAPTION_PAD + sum(column_widths[:i]) + gap * i for i in range(columns)]
    rows = [LEGEND_ENTRIES[i : i + columns] for i in range(0, count, columns)]
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
    elif kind == "desert":
        cv2.line(image, (cx - half, cy), (cx + half, cy), DESERT_COLOUR, 1)
