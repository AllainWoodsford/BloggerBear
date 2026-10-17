# COOL reconnaissance, 16 October 2026

The answers to step 0 of the [COOL benchmark runsheet](../../../cool-benchmark-runsheet.md),
taken on a `c8g.large` in us-west-2, launched from the Marketplace subscription and reached through
SSM Session Manager. Nothing was benchmarked. By the runsheet's table this is **go, on the AMI**:
an AMI, and `cv2` 5.x on Python 3.12 once the vendor's environment is activated (below).

Everything below comes from the instance's own output, pasted from the session, except the rows
marked *(listing)*, which were read off the Marketplace pages.

## The product

| | |
|---|---|
| Listing | *Cloud Optimized OpenCV For AWS Graviton4*, AWS Marketplace |
| Version | 3.1, 15 July 2026, marked "latest, stable". The vendor's release notes say "built on OpenCV 5.0"; the build calls itself `5.1.0-dev` (below) |
| Product code | `aajkmdd4qo3r7yhqg61a7aah9`, as the instance's own metadata reports it (`product-codes`) |
| Delivery | An **AMI**. The same vendor also lists a Docker variant (Python 3.10, 3.11 and 3.12) and a Graviton5 listing *(listing)*. Neither was used |
| Region | Offered in us-west-2, beside `sentinel-cogs`, as `ami-08dacb72c289c8261`. The listing names one AMI per Region, 26 Regions in all |
| Instances | The vendor names the c8g, m8g and r8g families, and recommends `m8g.4xlarge` or larger. On 16 October the price table listed `c8g.large` at USD 0.01 an hour and `c8g.2xlarge` at USD 0.02 an hour. That is the software fee only; EC2 is billed on top. The listing offered a free trial *(listing)* |

## The instance

| | |
|---|---|
| Type | `c8g.large`: 2 vCPU, 1 thread per core |
| OS | Ubuntu 24.04.3 LTS, kernel `6.17.0-1019-aws` |
| CPU | `CPU part : 0xd4f`, Neoverse-V2. The firmware names it "AWS Graviton4 CPU @ 2.8GHz". Its flags include `sve`, `sve2`, `bf16` and `i8mm` |
| Pythons | `/usr/bin/python3` is 3.12.3; `python3.11` and `python3.12` are installed too |
| Docker | 29.1.3 |

## Where COOL's `cv2` lives

`/opt/cool` holds `cpp_sdk`, `python_3.10`, `python_3.11`, `python_3.12`, `samples` and `venvs`.
The vendor's venvs are `/opt/cool/venvs/python_3.10`, `python_3.11` and `python_3.12` (Python
3.10.19, 3.11.14 and 3.12.3). The `python_3.12` venv holds numpy 2.5.1 and pip 24.0, with
`include-system-site-packages = false`.

The `cv2` module is not inside any venv. The `python_3.12` venv's `activate` script ends with two
exports (the other two venvs' scripts were not read), and these are what let the venv's Python
import the module and load its libraries:

```sh
export PYTHONPATH="/opt/cool/python_3.12/site-packages/cv2/python-3.12:$PYTHONPATH"
export LD_LIBRARY_PATH="/opt/cool/cpp_sdk/lib:/opt/ffmpeg/lib:$LD_LIBRARY_PATH"
```

What each way of running Python found:

| Run as | Result |
|---|---|
| A venv's `bin/python`, not activated | `ModuleNotFoundError: No module named 'cv2'` |
| The system `python3` | It finds a `cv2`, but there is no numpy: "OpenCV bindings requires "numpy" package" |
| `python` after `source /opt/cool/venvs/python_3.12/bin/activate` | `cv2 5.1.0-dev`, from `/opt/cool/python_3.12/site-packages/cv2/python-3.12/cv2.cpython-312-aarch64-linux-gnu.so`, with numpy 2.5.1 from the venv |

The vendor's instructions also offer the system Python with the two paths exported by hand. That
route was not used.

## The build

[`cool-buildinfo.txt`](cool-buildinfo.txt) is `cv2.getBuildInformation()` from the activated
`python_3.12` environment, byte for byte. As OpenCV prints it, it begins and ends with a blank
line. `.gitattributes` exempts it from line-ending conversion, because the fingerprint is its hash.

- **The COOL fingerprint:**
  `12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4`. This is the SHA-256 of that
  text, which is how `lambdas/vision/build.py` computes `build_sha256`. It is what `--cool-sha`
  and `COOL_BUILD_SHA256` pin. `scripts/tests/test_cool_evidence.py` checks the file against it.
- **Version:** `5.1.0-dev`. The build was made on 2026-07-13 with GCC 13.3.0, using
  `-O3 -mcpu=neoverse-v2` and link-time optimisation (`-flto=auto`).
- **CPU features:**
  - baseline `SVE NEON FP16 NEON_DOTPROD NEON_FP16 NEON_BF16`;
  - dispatched `NEON_DOTPROD NEON_FP16 NEON_BF16`.
- **Libraries:**
  - custom HAL `YES (carotene (ver 0.0.1) KleidiCV (ver 0.7.0))`, so `build.py` reports
    `kleidicv` true;
  - LAPACK from Arm Performance Libraries 25.07.1;
  - DNN MLAS, SGEMM only, with NEON kernels.

  The parallel framework is pthreads.
- **Modules:** calib, core, dnn, features, flann, geometry, highgui, imgcodecs, imgproc,
  objdetect, photo, ptcloud, python3, stereo, stitching, ts, video and videoio. There are no
  contrib modules, so there is no `ximgproc`. The worker's thinning already uses core OpenCV only.
- **Python binding:** built against numpy 2.5.1.

## Against the stock rows

| | COOL (this AMI) | Stock, as deployed | Stock 4.x row |
|---|---|---|---|
| OpenCV | `5.1.0-dev` | 5.0, `opencv-python-headless==5.0.0.93` | 4.14, `opencv-python-headless==4.14.0.94` |
| numpy | 2.5.1 (the vendor's) | 2.4.4 | 2.4.4 |
| Python on the instance | 3.12.3 (the vendor's venv) | 3.12.3 (a venv on `/usr/bin/python3`); Lambda arm64 runs 3.12 | 3.12.3 |

All three run on Python 3.12, so the runsheet's "Python under 3.11" row does not apply. The
OpenCV versions differ, though: COOL 3.1 is a 5.1 development build, and stock is the 5.0.0
release. Every COOL claim is therefore scoped to "COOL 3.1 (OpenCV 5.1.0-dev)". A speed-up is COOL
3.1 measured against stock OpenCV 5.0.0, not the vendor's optimisations in isolation. The `compare`
parity check shows whether the two produce the same rail metrics.

## Notes for the Graviton session

- **Session Manager opens `sh`, not bash, in a folder you cannot write to.** `source` does not
  exist in `sh` (`sh: 8: source: not found`), and the shell starts as `ssm-user` in the agent's
  own folder (`/var/snap/amazon-ssm-agent/<revision>`): a script saved there needed `sudo`. Type
  `bash`, then `cd ~`.
- **The user-data script's output file (`/var/log/cool-recon.txt`) was never written.** Every step
  of the session is typed by hand.
- **COOL's interpreter:**
  - The vendor's activated `python_3.12` environment, wrapped as `~/cool/bin/python` so the
    runsheet's commands stay as written.
  - The benchmark's own packages (requests, networkx and boto3) go into `~/cool/extra`, not into
    the vendor's venv.
  - Never install opencv or numpy into either.

  Steps 4 and 6 of the runsheet give the commands.
- **The instance size is still open.** The runsheet's `c8g.xlarge` was not in the price table on
  16 October. If the launch page does not offer it, use `c8g.2xlarge`. The 4-thread runs pass
  `--threads 4`, so their rows mean the same on either size.
