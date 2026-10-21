# Draft: report to the COOL vendor

A draft, for the owner to send through the Marketplace listing's support contact. Nothing here has
been sent. The evidence is in [session.md](session.md) and the run files it cites.

---

**Subject:** COOL 3.1 (AMI, Graviton4): heap corruption with `connectedComponentsWithStats` at more
than one thread, and an `adaptiveThreshold` regression

**Environment.** COOL 3.1 from AWS Marketplace, `ami-08dacb72c289c8261` (us-west-2), on a
`c8g.xlarge` (Neoverse V2). Your `python_3.12` environment, activated: `cv2` 5.1.0-dev from
`/opt/cool/python_3.12/site-packages/cv2/python-3.12/`, numpy 2.5.1. `cv2.getBuildInformation()`
sha256 `12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4` (KleidiCV 0.7.0 in the
custom HAL). Compared with `opencv-python-headless==5.0.0.93` on the same instance and Python.

**1. Heap corruption at two or more threads.** Our satellite-imagery pipeline (open source:
https://github.com/AllainWoodsford/BloggerBear, `lambdas/vision/`) aborts with glibc's
`double free or corruption (!prev)` on real Sentinel-2 scenes of about 3300 × 3300 pixels when
`cv2.setNumThreads(2)` or more. It completes at one thread, and stock 5.0.0 completes at four.

- Forcing only `cv2.connectedComponentsWithStats` (8-connectivity, `uint8`, contiguous) to one
  thread, and leaving the other 15 OpenCV functions the pipeline uses at two threads, removes the
  abort; forcing any other single function does not.
- The four inputs that call receives, saved and replayed alone, at one to four threads, ten times in
  one process with `MALLOC_CHECK_=3`, do not abort, and give the same label counts at every thread
  count. So the fault seems to need the state around the call (other OpenCV calls in the same process
  before it), not the input alone.
- Reproduction: the repository at commit `4582abc`, then
  `python scripts/vision_benchmark.py scenes --threads 2 --repeats 1 --backend cool --cool-sha <the sha256 above> --only sydney-rail-2024-08-26 --out /tmp/out.json`
  from your activated environment (it reads the public `sentinel-cogs` bucket; no AWS account
  needed). Our runsheet, `docs/cool-benchmark-runsheet.md`, sets up the environment in steps 4 to 6.

**2. `adaptiveThreshold` is slower than stock.** `cv2.adaptiveThreshold` with
`ADAPTIVE_THRESH_GAUSSIAN_C` and a block size of 31, on a synthetic image, one thread: a median of
36.6 ms under COOL against 24.4 ms under stock 5.0.0 (about 1.5 times as long), over two
interleaved runs each. `GaussianBlur`, `findContours`, `distanceTransform` and
`connectedComponentsWithStats` were 8 to 21 % faster under COOL. The per-kernel run files are in
`docs/hackathon/evidence/runs/` (`2026-10-20-bench-kernels-…`).

We are happy to run a fixed build against the same scenes.
