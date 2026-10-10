"""The rail access figure: the heat map over the scene, with deserts, lines and stations drawn.

The red band, contrast-stretched, carries the city's shape; the heat map is laid over it in an
inferno colour map where the scene was usable, and the unmeasured parts get annotate.py's slate
tint so a cloud never looks like an empty suburb. Deserts are outlined, the graph's edges are
drawn thicker the more paths run through them, stations larger the busier their catchment, and
hubs marked. Three caption lines are burned in: the processed-imagery notice, the Copernicus
sentence and the OpenStreetMap credit (the page carries the full attribution with its (c), which
Hershey fonts lack).
"""

from __future__ import annotations

import cv2
import numpy as np

from vision.annotate import UNMEASURED_TINT, burn_caption, encode_png
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
CAPTION_LINES = (
    "Processed imagery - BloggerBear",
    "Contains modified Copernicus Sentinel data {year}",
    "Map data (c) OpenStreetMap contributors",
)


def annotate_rail(analysis: RailAnalysis, red: np.ndarray, year: str | int, max_side: int = 1024) -> bytes:
    """PNG bytes of the figure, scaled so the longer side is at most `max_side`."""
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

    hubs = set(layers["hubs"])
    for station, position in zip(metrics["stations"], layers["stations_px"], strict=False):
        if position is None:
            continue
        centre = at(position[0], position[1])
        radius = 2 + round(4 * (station["activity"] or 0.0))  # unmeasured (None) draws as the smallest
        cv2.circle(image, centre, radius, STATION_COLOUR, 1, cv2.LINE_AA)
        if station["id"] in hubs:
            cv2.drawMarker(image, centre, HUB_COLOUR, cv2.MARKER_DIAMOND, HUB_MARKER_SIZE, 2)

    burn_caption(image, [line.format(year=year) for line in CAPTION_LINES])
    return encode_png(image)
