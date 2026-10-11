"""The vision core: OpenCV 5 image analysis for the vision worker, with no AWS in it.

docs/enhancements/opencv-agentic-vision-enhancement.md is the design. This package is the
*perception* step of the loop: it is given a site's bands as arrays and returns what it measured.
Apart from cog.py's range reads (through a fetch function it is handed) it reads nothing, stores
nothing and calls no model, so the same code runs on stock OpenCV (the `opencv` backend) and on
COOL (the `cool` backend), and tests run it on synthetic images.

- masks.py   which pixels can be measured: the site polygon, water, cloud and no-data
- detect.py  bright compact objects on water, filtered by length and elongation
- analyse.py the whole pass for one site: masks, detect, count, coverage, density
- annotate.py the figure: detections drawn on a contrast-stretched crop, as PNG bytes
- build.py   which OpenCV build did the work (so a "cool" result can be proved to be COOL)
- cog.py     a window of a tiled GeoTIFF, or of one of its overviews, over HTTP range requests
             (the only part that reads)
- geo.py     lon/lat polygons to a UTM scene's pixels, and pixels back to lon/lat

The rail access task (docs/enhancements/rail-access-monitor.md) adds, on the same footing:

- urban.py        where the city is built up (NDBI, NDVI, SCL) and the heat map of it
- access.py       distance to stations: served area, transit deserts, catchments, intermodal points
- network.py      OpenStreetMap rail ways rasterised, thinned and read back as a graph (networkx)
- rail_analyse.py the whole pass for one city: heat, reach, graph measures, flags, suggestions
- rail_annotate.py the figure: heat over the scene, deserts, lines, stations and hubs

Imported only by the vision worker (and its tests): the shared pipeline zip has no OpenCV.
"""
