"""The vision core: OpenCV 5 image analysis for the vision worker, with no AWS in it.

docs/enhancements/opencv-agentic-vision-enhancement.md is the design. This package is the
*perception* step of the loop: it is given a site's bands as arrays and returns what it measured.
It reads nothing, stores nothing and calls no model, so the same code runs on stock OpenCV (the
`opencv` backend) and on COOL (the `cool` backend), and tests run it on synthetic images.

- masks.py   which pixels can be measured: the site polygon, water, cloud and no-data
- detect.py  bright compact objects on water, filtered by length and elongation
- analyse.py the whole pass for one site: masks, detect, count, coverage, density
- annotate.py the figure: detections drawn on a contrast-stretched crop, as PNG bytes
- build.py   which OpenCV build did the work (so a "cool" result can be proved to be COOL)

Imported only by the vision worker (and its tests): the shared pipeline zip has no OpenCV.
"""
