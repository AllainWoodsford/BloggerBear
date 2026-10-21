# The Graviton session, 20 October 2026

Steps 2 to 9 and 11 of the [COOL benchmark runsheet](../../../cool-benchmark-runsheet.md), done by
hand on one instance, one day later than the runsheet planned (Monday 19). Step 10 (the container
route) was not run; step 12 (the agent session) was not run. Every number below is from a committed
run file in [`runs/`](../runs/), indexed in [RESULTS.md](../RESULTS.md), except the diagnosis of the
crash, which is from the session's own terminal output, pasted here.

## The instance

| | |
|---|---|
| Type | `c8g.xlarge` (4 vCPU, 8 GiB), us-west-2, from the Marketplace subscription |
| AMI | `ami-08dacb72c289c8261`, COOL 3.1, product code `aajkmdd4qo3r7yhqg61a7aah9` (every run's `environment.host`) |
| CPU | Neoverse V2, `CPU part 0xd4f` ([lscpu.txt](lscpu.txt)) |
| Python | 3.12.3 on all three sides: COOL's own environment, `~/stock` and `~/stock4` |
| Launched | 2026-10-20 11:10:53 UTC (22:10:53 AEDT) |
| Ended | 2026-10-20 12:46:36 UTC (23:46:36 AEDT), terminated from the console (CloudTrail's `TerminateInstances` for `i-0b0de3b70ccc01b45`) |
| Hours | 1.6 (1 h 35 min 43 s) |
| Fees | None paid: the EC2 and Marketplace charges were covered by the account's free AWS credits |
| Code | `4582abcdbe5395f35c2990ea2ee1ef4879b4eb3d` (`dev`), clean; `scripts/vision_benchmark_networks/sydney.json` sha256 `bfc3c7f8…7eb4`, as its `NOTICE.md` row |
| Inputs | `vision_eval.py pin`: the manifest matches the bucket; coverage not re-run (nothing re-pinned) |

## The builds

| Side | `cv2` | numpy | Fingerprint (sha256 of `getBuildInformation()`) |
|---|---|---|---|
| COOL 3.1 | 5.1.0-dev, `/opt/cool/python_3.12/site-packages/cv2/…` | 2.5.1 | `12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4`, equal to [cool-buildinfo.txt](cool-buildinfo.txt) |
| stock 5 | 5.0.0 (`opencv-python-headless==5.0.0.93`) | 2.4.4 | `4460ffe25568d7f496997f0593f568f4d91ee4740c97f057b2986c237e8a386c` ([stock-buildinfo.txt](stock-buildinfo.txt)) |
| stock 4 | 4.14.0 (`opencv-python-headless==4.14.0.94`) | 2.4.4 | `ef331f816d3fad3bd68817cd7f4d8a8ba3e6d52dd691d046e9fc61967a3a1323` ([stock4-buildinfo.txt](stock4-buildinfo.txt)) |

COOL's build lines name `-O3 -mcpu=neoverse-v2 … -flto=auto` and `Custom HAL: YES (carotene (ver
0.0.1) KleidiCV (ver 0.7.0))`. The COOL `scenes` runs list its `libopencv_*.so.5.1.0` from
`/opt/cool/cpp_sdk/lib` as mapped into the process (`environment.libraries`); KleidiCV is linked into
them, not a library of its own.

## The results

COOL over stock OpenCV 5.0.0 (median stock / median COOL: above 1 is COOL faster), from the
`bench-compare` run files:

| Pair | Speed-up | 95 % interval | Parity |
|---|---:|---|---|
| **scenes, one core (the headline: the deployed worker's shape)**, compute stages | **1.028** | 0.964 to 1.037 | pass, no pixel differs |
| … `sydney-rail-2024-08-26`: analyse, of which graph; annotate | 1.027, 1.069; 1.007 | | |
| … `sydney-rail-2024-01-05`: analyse, of which graph; annotate | 1.038, 1.064; 1.015 | | |
| … `botany-bay-2024-01-05` (ships): analyse; annotate | 0.981; 1.017 | | |
| kernels, one core (synthetic) | 1.054 | 1.053 to 1.056 | pass |
| scenes, four threads | not measured: COOL aborted on both rail scenes (below) | | fail |
| stock 5.0.0 over stock 4.14.0, scenes, one core | 1.007 | 0.998 to 1.009 | pass |

Per kernel, one core: COOL is faster on `GaussianBlur` (1.21), `findContours` (1.16),
`distanceTransform` (1.12), `connectedComponentsWithStats` (1.09) and `morphologyEx` (1.08), and
slower on `adaptiveThreshold` (0.67: about 1.5 times as long) and `polylines` (0.77).

**What that says.** On this workload COOL 3.1 is about 3 % faster than stock OpenCV 5.0.0, and the
interval includes no change. The gains in the graph stage are cancelled by `adaptiveThreshold`, which
the built-up mask runs on every scene. `perf` (step 9, [perf-dso.txt](perf-dso.txt), COOL on
`sydney-rail-2024-08-26`, one core) puts about 11 % of the samples' own time in `libopencv_imgproc`
and under 1 % in the rest of OpenCV; the interpreter (33 %), zlib, decompressing the COG tiles
(20 %), and numpy (14 %) take most of it. However fast its kernels, an OpenCV build can move only that
share. (`dpkg-query`, 6 %, is the benchmark asking which package owns each mapped library, not the
worker.) OpenCV 5.0.0 against 4.14.0 is no change either.

**The controls.** From COOL's environment: `ok`, `not_cool`, `backend_mismatch`; from stock's with
the same pin: `not_cool`, `not_cool`, `backend_mismatch`. Both 3 of 3 as expected.

## The crash: COOL with more than one thread

`scenes --threads 4` under COOL measured the ships scene and failed both rail scenes with "isolated
run exited -6" (SIGABRT). Stock OpenCV 5.0.0 measured all three at four threads. Diagnosed by hand on
the instance:

1. Run in-process, the scene printed glibc's `double free or corruption (!prev)` before aborting, at
   two threads as at four. `dmesg` showed nothing of it.
2. Python's fault handler stopped in `analyse_rail_access` (`lambdas/vision/rail_analyse.py:368`) with
   no deeper Python frame: the corruption was found while freeing, so it happened earlier.
3. Bisected over the 16 `cv2` functions the rail path calls, at two threads, each group forced to one
   thread by a wrapper: none forced, aborts; all forced, completes; then the half, then each of its
   eight alone. **Only `connectedComponentsWithStats` forced to one thread makes the scene complete.**
4. Not reproduced alone: `connectedComponentsWithStats` on random images up to 6000 px square and
   3306 × 3327, at densities 0.05 and 0.5, and the scene's own four inputs (each 3306 × 3327 `uint8`,
   contiguous, connectivity 8; 2224, 449, 437 and 2224 labels), each in a fresh process at one and two
   threads, and all four ten times in one process at one, two and four threads with `MALLOC_CHECK_=3`:
   all completed, the counts equal across thread counts. The fault needs the run's state around the
   call, not the call alone. `distanceTransform` and `distanceTransformWithLabels` alone at 6000 px
   were clean too.
5. The workaround: the scene at two threads with only `connectedComponentsWithStats` held to one
   thread completed, and `compare` against stock's one-core run gave `sydney-rail-2024-08-26 parity:
   pass`. That run was a diagnostic (one repeat, a patched `cv2`, no labels), not committed.

So the one-core numbers above stand, and a COOL worker would have to run OpenCV on one thread, or
hold that one function to one. Reported to the vendor: [vendor-report.md](vendor-report.md).

## Not done

- Step 10, the container route: a separate subscription, not reconnoitred.
- Step 12, the agent session: not run, so the report does not claim COOL inside the agent's loop.
- No COOL worker is deployed. With no measurable speed-up on this workload and a multi-thread fault,
  the deployed worker stays on stock OpenCV 5 on Lambda arm64.
