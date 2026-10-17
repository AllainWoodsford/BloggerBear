# COOL benchmark runsheet

The Graviton session for the competition's COOL award, done by hand. On one Graviton4 instance in
us-west-2 it runs the vision worker's own code with COOL (the vendor's Graviton4 build of OpenCV)
and with stock OpenCV 5, on the same pinned Sydney scenes and the same network, checks that both
give the same rail metrics, and commits the run files that prove which build ran and how fast. The
report's COOL numbers come only from those files.

- **When:** steps 1 to 11 on Monday 19 October; the optional agent session (step 12) on
  Wednesday 21.
- **Before it:** the Sydney network fixture is committed (step 5 says how), and the reconnaissance
  is done (step 0).
- **Where it came from:** this was the section "The COOL backend and the benchmark" of the
  [deployment runsheet](deployment-runsheet.md#the-cool-backend-and-the-benchmark), which now
  points here.

## The session

COOL (OpenCV's Graviton-tuned OpenCV 5, "Cloud Optimized OpenCV for AWS Graviton4" on AWS
Marketplace) is what the competition's COOL award scores, and it is measured against the stock
worker (the [deployment runsheet's Vision section](deployment-runsheet.md#vision-optional)) on
one Graviton4 instance, in one session. Nothing here is in Terraform; these are by hand, and every
command below is what the session runs, in this order. The evidence it leaves
behind is listed at the end; the run files it writes are the only source of the COOL numbers in
the report ([the evidence README](hackathon/evidence/README.md) has the schema). The script's own
options are in [scripts/README.md](../scripts/README.md#timing-the-vision-worker-vision_benchmarkpy).

**Venvs, not the system pip.** Ubuntu 24.04 refuses a system-wide `pip install` (PEP 668,
"externally-managed-environment"). Every install below goes into a venv or a folder of its own:
never `sudo pip`, never `--break-system-packages`. Never put `opencv-python-headless` or `numpy`
into COOL's environment, which is the vendor's venv plus `~/cool/extra`. It must see the
product's own `cv2` and the numpy that `cv2` was built against. A stock install there would shadow
them, and the handler would answer `not_cool`.

**Bash, in your home folder, by hand.** SSM Session Manager opens `sh` (dash) as `ssm-user`, in
the agent's own folder (`/var/snap/amazon-ssm-agent/<revision>`), which `ssm-user` cannot write
to; and `source` does not exist in `sh`. So type `bash`, then `cd ~`, first. Every command below
is bash, and steps 3 and 5 write to the folder you are in. On 16 October the user-data script's
output file (`/var/log/cool-recon.txt`) was never written, so type the steps by hand; do not
script them into the launch.

0. **Reconnaissance: done on 16 October, in
   [recon.md](hackathon/evidence/cool/recon.md).** It found:
   - an AMI, version 3.1, in us-west-2;
   - `cv2` 5.1.0-dev in the vendor's Python 3.12 environment, with numpy 2.5.1;
   - a Neoverse V2 (Graviton4) CPU.

   That is the first row of the table below: **go**. Steps 4 and 6 follow the vendor's layout. To
   reconnoitre another version, record the same answers:
   - the Marketplace product id, version and price, and its **delivery type**: an AMI or a
     container image;
   - whether it is offered in **us-west-2**, beside `sentinel-cogs`, and with `c8g` instances;
   - on the smallest instance it allows: `python3 -V`;
     `python3 -c 'import cv2; print(cv2.__version__, cv2.__file__)'`; `lscpu`;
     `grep -m1 'CPU part' /proc/cpuinfo`. On this vendor's AMI a plain `python3` finds a `cv2` it
     cannot import (no numpy beside it): ask inside the vendor's environment instead,
     `bash -c 'source /opt/cool/venvs/python_3.12/bin/activate && python -c "import cv2, numpy; print(cv2.__version__, cv2.__file__, numpy.__version__)"'`.

   Then decide:

   | Found | Then |
   |---|---|
   | an AMI; `cv2` imports and is 5.x; Python 3.11 or newer | go: the steps below as written |
   | `cv2` imports, but is not 5.x | record it; every claim is scoped to "COOL <version>"; add a stock venv of the same major version, so the comparison is like for like |
   | a container image, not an AMI | launch a `c8g` with Docker; build `docker/vision-cool/Dockerfile` from the image by digest once step 5 has the network fixture in the checkout (its 5.x gate applies), and run the COOL commands inside it, as the paragraph below the table says |
   | no Python binding (C++ only) | the container route if one exists; otherwise the COOL proof (C3) is "not shown" and the report says why; the COOL configuration (C1) and the stock rows still stand |
   | Python under 3.11 | the stock venvs need a 3.11+ interpreter (numpy 2.4.4 requires it): install one for them (step 3) and record that the backends ran on different Pythons |
   | not in us-west-2 | the nearest Region with `c8g`; fill `--cache` once there (the reads then cross Regions only once) |
   | no trial, or more than about USD 5 an hour | decide before launching |

   **The container route.** The image copies in the handler, the benchmark with its run-file
   module, its scenes file, the evaluation's manifest and `scripts/vision_benchmark_networks/`, so
   build it from the checkout after step 5, naming the commit (the image has no git history):

   ```sh
   docker build --platform linux/arm64 -f docker/vision-cool/Dockerfile --build-arg COOL_IMAGE=<registry>/<repository>@sha256:<digest> --build-arg GIT_SHA=$(git rev-parse HEAD) -t bloggerbear-vision-cool .
   docker run --rm --network host -v "$PWD/docs/hackathon/evidence/runs:/var/task/runs" -v "$HOME/cogs:/cogs" --entrypoint python3 bloggerbear-vision-cool bench/vision_benchmark.py kernels --backend cool --cool-sha $SHA --out runs
   ```

   Each COOL command of step 8 runs that way: `docker run ... bench/vision_benchmark.py` in
   place of `~/cool/bin/python scripts/vision_benchmark.py`, `--out runs` for `--out $E` and
   `--cache /cogs` for `$C` (the two mounts put the run files and the cache on the host, beside the
   stock runs, which use the host's venvs as written), and `--network host` so `--imds` reaches the
   metadata service in one hop.
1. **Subscribe** on AWS Marketplace to *Cloud Optimized OpenCV for AWS Graviton4*: done on
   16 October, the AMI listing (not *[Docker]*, not Graviton5). Unsubscribe at the end unless the
   subscription is kept.
2. **Launch** a `c8g.xlarge` (4 vCPU, 8 GiB: enough for the 1.32 GB worst case) in us-west-2,
   from the subscription, so that the instance carries the product code:
   - version 3.1;
   - `ami-08dacb72c289c8261` in us-west-2;
   - product code `aajkmdd4qo3r7yhqg61a7aah9`.

   On 16 October the listing's price table showed only `c8g.large` and `c8g.2xlarge`. If the
   launch page does not offer `c8g.xlarge`, take `c8g.2xlarge` (8 vCPU, 16 GiB). Step 8's
   4-thread runs pass `--threads 4`, so they mean the same on either size. Require IMDSv2.
   Connect through SSM Session Manager, with no inbound ports, and type `bash`, then `cd ~`. The instance role
   has `AmazonSSMManagedInstanceCore` only (step 12 adds one statement for its own hour).
3. **The host.** On the instance:

   ```sh
   lscpu > ~/lscpu.txt
   grep -m1 'CPU part' /proc/cpuinfo      # expect 0xd4f: Neoverse V2, Graviton4
   python3 -V && uname -r
   sudo apt-get update && sudo apt-get install -y python3-venv git gh   # step 7 makes venvs, step 5 clones, step 11 pushes; a no-op when they are there
   ```

   On the COOL AMI, on 16 October, that printed `0xd4f`, Python 3.12.3 and `6.17.0-1019-aws`.

   An Ubuntu server image ships `python3` without its `venv` package (`python3 -m venv` then fails
   with "ensurepip is not available"), hence the install. If `python3 -V` is under 3.11, the stock
   venvs need a 3.11+ interpreter from the distribution's own archive: on Ubuntu 22.04 (Python
   3.10) that is `sudo apt-get install -y python3.11 python3.11-venv` (22.04 has no `python3.12`);
   use `python3.11` in place of `python3` in step 7, and record it in `recon.md`.
4. **COOL's interpreter and its build.** The product's `cv2` is found only inside the vendor's
   activated environment: its `activate` script puts `cv2` on `PYTHONPATH` and the vendor's
   libraries on `LD_LIBRARY_PATH` (recon.md). A plain `python3` finds a `cv2` it cannot import
   (there is no numpy beside it), and the venv's `bin/python` without activation finds none. So `~/cool/bin/python` is a small wrapper that activates
   the environment and runs the venv's Python. Every COOL command below runs through it, under
   `sudo` too, because it sets the paths itself:

   ```sh
   mkdir -p ~/cool/bin
   cat > ~/cool/bin/python <<'EOF'
   #!/bin/bash
   # COOL's interpreter: the vendor's python_3.12 environment (its cv2, its libraries, its numpy),
   # plus step 6's packages from the extra folder beside this one.
   source /opt/cool/venvs/python_3.12/bin/activate
   export PYTHONPATH="$(dirname "$(dirname "$(readlink -f "$0")")")/extra:$PYTHONPATH"
   exec python "$@"
   EOF
   chmod +x ~/cool/bin/python
   ~/cool/bin/python -c 'import cv2, sys; sys.stdout.write(cv2.getBuildInformation())' > ~/cool-buildinfo.txt
   sha256sum ~/cool-buildinfo.txt
   ~/cool/bin/python -c 'import cv2, numpy; print(cv2.__version__, cv2.__file__, numpy.__version__)'
   ```

   The sha256 is the COOL fingerprint: it must equal `build_info()["build_sha256"]`, which is the
   hash of exactly that text (`lambdas/vision/build.py`), and it is what `COOL_BUILD_SHA256` is
   pinned to. Expect `kleidicv` True. For COOL 3.1 it is
   `12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4`, with
   `5.1.0-dev /opt/cool/python_3.12/site-packages/cv2/python-3.12/cv2.cpython-312-aarch64-linux-gnu.so 2.5.1`.
   The text is committed as
   [cool-buildinfo.txt](hackathon/evidence/cool/cool-buildinfo.txt): after step 5,
   `cmp ~/cool-buildinfo.txt docs/hackathon/evidence/cool/cool-buildinfo.txt` prints nothing.

   A different hash means a different build. In that case:
   - say so in `recon.md`;
   - commit the new text over the old;
   - update the pin in `scripts/tests/test_cool_evidence.py`;
   - use the new hash as step 8's `SHA`.
5. **The code and the inputs.** Clone the repository at the commit to be benchmarked and record
   it: `git rev-parse HEAD` (the submission tag does not exist yet; the evidence index says how an
   earlier commit is still cited). The repository is public:
   `git clone https://github.com/AllainWoodsford/BloggerBear.git && cd BloggerBear && git checkout <commit>`,
   and every later command runs from that folder. Not a `git archive`: a copy without git history
   has no commit to record, and every run file would name none (`code.git_sha` null).

   The run files come back by a pushed branch (step 11), so give the clone a way to push now:

   ```sh
   gh auth login                     # GitHub.com, HTTPS, authenticate Git: yes; then the one-time code in your browser
   git config user.name '<your GitHub login>'
   git config user.email '<id>+<login>@users.noreply.github.com'   # your noreply address (GitHub's email settings): no real address in the commits
   git switch -c cool-session-$(date -u +%Y-%m-%d)
   ```

   Two inputs must be in that checkout before the runs:

   - **The Sydney network fixture**, `scripts/vision_benchmark_networks/sydney.json`: the network
     the deployed worker was sent, taken from Sydney's network object after the first Sydney tick
     (each city's network is its own object, `vision/<topic>/<site>/network.json`, not part of the
     running state: [`rail_access` topics](deployment-runsheet.md#rail_access-topics), "Where things land"), on your own
     machine. For the example topic the object is `vision/rail-cities/sydney/network.json`; for
     dev's `trains-sydney`, `vision/trains-sydney/sydney/network.json`, once a tick after the
     day-subject round has moved it there:

     ```sh
     aws s3 cp s3://<prefix>-dev-content/vision/rail-cities/sydney/network.json - | jq '{ways, stations, pois, fetched_at, source, endpoint, query_url, counts}' > scripts/vision_benchmark_networks/sydney.json
     ```

     ```powershell
     aws s3 cp s3://<prefix>-dev-content/vision/rail-cities/sydney/network.json "$env:TEMP\network.json"
     jq -a '{ways, stations, pois, fetched_at, source, endpoint, query_url, counts}' "$env:TEMP\network.json" | Set-Content -Encoding ascii scripts\vision_benchmark_networks\sydney.json
     ```

     The PowerShell lines differ on purpose: Windows PowerShell re-encodes whatever passes between
     two programs through its own pipe, which mangles any station name outside ASCII, so the
     network is downloaded to a file first and `jq -a` writes every such character as a `\u`
     escape; and
     `Set-Content -Encoding ascii` writes the lines as they are, with no byte-order mark (the
     benchmark reads a file with one too).

     The alternative is `python scripts/vision_benchmark.py fetch-network --scene sydney-rail-2024-08-26`
     (PowerShell: `py -3.11 scripts\vision_benchmark.py fetch-network --scene sydney-rail-2024-08-26`)
     on any machine that reaches Overpass (the network is then whatever OpenStreetMap holds that
     day). Fill the row in `scripts/vision_benchmark_networks/NOTICE.md` from the file's
     `fetched_at`, `endpoint` and `query_url` and its sha256 (`sha256sum`; PowerShell:
     `Get-FileHash scripts\vision_benchmark_networks\sydney.json`), `git add` it and run
     `python scripts/pii_denylist_check.py --staged` (PowerShell:
     `py -3.11 scripts\pii_denylist_check.py --staged`) before committing it: Sydney's suburb and
     station names could match the denylist; if they do, decide whether to allow the term or keep
     the file out of git, in which case the run files still cite its sha256. Without the file the
     two rail scenes report `error: network_file missing` and only the ship scene is timed.
   - **The pinned scenes.** `scripts/vision_eval_manifest.json` was pinned on 16 October (every
     asset's ETag and size, from the build container). Confirm nothing moved, on your own machine
     (on the instance, after step 7: `~/stock/bin/python scripts/vision_eval.py pin`; the instance
     has no `python` and no `requests` until then):

     ```sh
     python scripts/vision_eval.py pin        # prints "the manifest matches the bucket", or what changed
     ```

     ```powershell
     py -3.11 scripts\vision_eval.py pin
     ```

     A changed ETag is said and exits 1: every benchmark run then refuses that scene ("input
     changed") until the manifest is re-pinned with `--write`, which is a new input, not the one
     earlier runs read. `coverage` was run on 16 October too (its run file is in
     `docs/hackathon/evidence/runs/`); it need not be re-run for the session, and when it is (a
     re-pinned scene, or a scene added), it runs anywhere with internet, about 60 range requests
     and 0.7 MB a scene:

     ```sh
     python scripts/vision_eval.py coverage --write --out docs/hackathon/evidence/runs
     ```

     ```powershell
     py -3.11 scripts\vision_eval.py coverage --write --out docs\hackathon\evidence\runs
     ```
6. **COOL's packages**, in a folder of their own beside the vendor's environment, never inside
   it. The vendor's venv stays as shipped. Step 4's wrapper puts
   `~/cool/extra` on `PYTHONPATH`:

   ```sh
   /opt/cool/venvs/python_3.12/bin/python -m pip install --target ~/cool/extra requests==2.34.2 networkx==3.4.2 boto3==1.34.162
   ~/cool/bin/python -c 'import cv2, numpy, requests, networkx, boto3; print(cv2.__file__, numpy.__file__)'   # both under /opt/cool
   ~/cool/bin/python -c 'import cv2, hashlib; print(hashlib.sha256(cv2.getBuildInformation().encode()).hexdigest())'   # must equal step 4
   ```

   These are `lambdas/requirements-vision-cool.txt`'s pins without `awslambdaric`, plus boto3
   for step 12. None of them needs numpy. **Never install opencv or numpy here**: the vendor's
   `cv2` was built against its own numpy 2.5.1.
7. **The stock venvs**: OpenCV 5 as the deployed worker runs it, and the newest OpenCV 4 that has
   an aarch64 wheel for this Python and numpy 2, so the report can say what OpenCV 5 changed for
   this workload:

   ```sh
   python3 -m venv ~/stock && ~/stock/bin/pip install -r lambdas/requirements-vision.txt
   python3 -m venv ~/stock4 && ~/stock4/bin/pip install "opencv-python-headless==4.14.0.94" numpy==2.4.4 requests==2.34.2 networkx==3.4.2
   for v in stock stock4; do ~/$v/bin/python -c 'import cv2, sys; sys.stdout.write(cv2.getBuildInformation())' > docs/hackathon/evidence/cool/$v-buildinfo.txt; done
   sha256sum docs/hackathon/evidence/cool/stock-buildinfo.txt docs/hackathon/evidence/cool/stock4-buildinfo.txt
   ```

   `opencv-python-headless==4.14.0.94` is the 4.x pin: PyPI's newest 4.x release (its wheels
   uploaded on 28 and 29 July 2026), with `cp37-abi3` aarch64 wheels (manylinux_2_17 and 2_28);
   it was installed beside `numpy==2.4.4` on 16 October and imports as `cv2` 4.14.0, and the
   benchmark's `kernels` ran on it (the evidence README records the check). Use step 3's
   `python3.11` in place of `python3` here if step 3 said so. On the COOL AMI, `python3` is
   3.12.3, the deployed worker's minor version, and the same Python as COOL's environment. Stock
   aarch64 builds can include KleidiCV too, which is why the proof is the fingerprint and the
   mapped libraries, not the flag.

   The versions are not the same on both sides:
   - COOL 3.1 is OpenCV 5.1.0-dev on numpy 2.5.1;
   - stock is OpenCV 5.0.0 on numpy 2.4.4.

   So the report scopes every COOL claim to "COOL 3.1 (OpenCV 5.1.0-dev)", and gives a speed-up
   as that build against stock 5.0.0, not as the vendor's optimisations alone (recon.md).
8. **The runs.** Interleave the backends, so a slow minute on the instance does not land on one
   side. Set the names once:

   ```sh
   E=docs/hackathon/evidence/runs
   CPU=neoverse-v2-graviton4               # the cpu slug in every run id: check it against the first file written
   SHA=12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4   # step 4's sha256: COOL 3.1
   L="--imds --label cool_product=aajkmdd4qo3r7yhqg61a7aah9 --label cool_version=3.1 --label cool_delivery=ami"
   C="--cache $HOME/cogs"
   ```

   Each run writes `$E/<UTC date>-bench-<mode>-$CPU-<backend>-<tag>.json` (`--out $E` with
   `--tag`), and `scenes` writes each scene's figure beside it. The date is the run's own start, so
   a session that crosses midnight UTC writes two dates; the `compare` lines find each file by the
   rest of its name (`newest`: the latest of that mode, backend and tag). `--imds` records the
   instance type, AMI, Region, architecture and the Marketplace product codes, nothing else of the
   identity document (the account id never reaches a file). `taskset -c 0` pins a run to one core, the deployed worker's
   shape; the affinity is recorded, so the one-core rows say so themselves (`--pin-cpu 0` does the
   same from inside the script). `--isolate` runs each scene in a fresh interpreter, so its peak
   memory is its own.

   ```sh
   for r in 1 2; do
     taskset -c 0 ~/stock/bin/python scripts/vision_benchmark.py kernels --threads 1 $L --out $E --tag 1t-r$r
     taskset -c 0 ~/cool/bin/python  scripts/vision_benchmark.py kernels --threads 1 --backend cool --cool-sha $SHA $L --out $E --tag 1t-r$r
     ~/stock/bin/python scripts/vision_benchmark.py kernels --threads 4 $L --out $E --tag 4t-r$r
     ~/cool/bin/python  scripts/vision_benchmark.py kernels --threads 4 --backend cool --cool-sha $SHA $L --out $E --tag 4t-r$r
   done
   ~/stock/bin/python scripts/vision_benchmark.py scenes --repeats 1 $C --out /tmp/warm.json                      # fills the cache: the timed runs read no network
   taskset -c 0 ~/stock/bin/python scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C $L --out $E --tag 1t
   taskset -c 0 ~/cool/bin/python  scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C --backend cool --cool-sha $SHA $L --out $E --tag 1t
   ~/stock/bin/python scripts/vision_benchmark.py scenes --threads 4 --repeats 5 --isolate $C $L --out $E --tag 4t
   ~/cool/bin/python  scripts/vision_benchmark.py scenes --threads 4 --repeats 5 --isolate $C --backend cool --cool-sha $SHA $L --out $E --tag 4t
   taskset -c 0 ~/stock4/bin/python scripts/vision_benchmark.py kernels --threads 1 $L --out $E --tag 4x-1t
   taskset -c 0 ~/stock4/bin/python scripts/vision_benchmark.py scenes --threads 1 --repeats 5 --isolate $C $L --out $E --tag 4x-1t
   newest() { ls "$E"/*-bench-$1-$CPU-$2.json | tail -n 1; }
   for pair in "scenes opencv-1t cool-1t" "scenes opencv-4t cool-4t" "kernels opencv-1t-r1 cool-1t-r1" "scenes opencv-4x-1t opencv-1t"; do
     set -- $pair
     ~/stock/bin/python scripts/vision_benchmark.py compare "$(newest $1 $2)" "$(newest $1 $3)" --out $E --tag "$1-$2-vs-$3"
   done
   ~/cool/bin/python  scripts/vision_benchmark.py controls --cool-sha $SHA --backend cool $C $L --out $E
   ~/stock/bin/python scripts/vision_benchmark.py controls --cool-sha $SHA $C $L --out $E
   ```

   What to expect: a `kernels --backend cool` run on any build but the pinned one stops with
   `not_cool` before timing anything; the `scenes --backend cool` rows are `ok` only from the COOL
   venv (the handler's own guard answers `not_cool` elsewhere, and that row is what gets
   recorded). `compare` prints the speed-up of the second run over the first (median A / median
   B) with its bootstrap interval, the headline on the compute stages (`analyse` and `annotate`;
   the read is reported apart, and `graph` is inside `analyse`), each stage's share, and `parity:
   pass` when the rail metrics agree; it exits 1 when they do not, and refuses two runs from
   different machines or inputs. The **headline speed-up is the pinned one-core scenes pair on the
   compute stages**: it matches the deployed worker's one vCPU. The 4-thread pair shows what more
   cores add; the `4x-1t` pair says what OpenCV 5 changed against 4.14. `controls` from the COOL
   venv must show `ok`, `not_cool`, `backend_mismatch` (`as_expected` 3); from the stock venv
   `not_cool`, `not_cool`, `backend_mismatch` (also 3). Every `scenes` and `controls` run file
   lists the OpenCV shared objects mapped into the process (`environment.libraries`, with each
   file's sha256 and package) and the build lines that tell a vendor build apart.
9. **Optional: where the time went.**

   ```sh
   sudo apt-get install -y linux-tools-common "linux-tools-$(uname -r)"    # perf, for this kernel
   sudo perf record -g -o perf.data -- ~/cool/bin/python scripts/vision_benchmark.py scenes --repeats 1 --threads 1 $C --backend cool --cool-sha $SHA --only sydney-rail-2024-08-26
   sudo perf report -i perf.data --sort dso --stdio > docs/hackathon/evidence/cool/perf-dso.txt
   ```

   `sudo` resets the environment, but step 4's wrapper sets the vendor's paths itself, so nothing
   is passed on by hand. `perf.data` is root's (mode 0600), so the report runs under `sudo` too.

   The share of samples in `libkleidicv*` and `libopencv*`/`cv2*.so` is direct evidence that the
   vendor's code executed. Commit `perf-dso.txt`, not `perf.data`.
10. **Optional, the container route** (when the product is an AMI): build
    `docker/vision-cool/Dockerfile` with the COOL image by digest and print `build_info()` inside
    it. The image's fingerprint may differ from the AMI's; record both, and say which one ran.
    **Not on Monday 19:** the image is the separate *[Docker]* listing, a subscription of its own;
    pulling it needs ECR permissions the SSM-only instance role does not have; and its layout is
    not reconnoitred. The Dockerfile assumes a `cv2` that a plain `python3` imports, which this
    vendor's AMI does not ship, so check the image first
    (`docker run --rm --entrypoint sh <image@digest> -c 'env | grep -E "PYTHONPATH|LD_LIBRARY_PATH|VIRTUAL_ENV"; python3 -c "import cv2, numpy; print(cv2.__version__, cv2.__file__)"'`)
    and change the Dockerfile to match before building.
11. **Push, then stop.** The files exist only on the instance until they are pushed, so push
    before anything is terminated:

    ```sh
    cp ~/lscpu.txt docs/hackathon/evidence/cool/
    git add "$E" docs/hackathon/evidence/cool/
    git commit -m "COOL: the Graviton session's run files"
    git push -u origin HEAD
    ```

    Check the branch on GitHub (the run files, the figures, `stock-buildinfo.txt`,
    `stock4-buildinfo.txt`, `lscpu.txt`, and `perf-dso.txt` if made), then terminate the instance
    (or stop it, if step 12 follows within two days), and unsubscribe unless the subscription is
    kept. On your own machine, on that branch, record the instance hours and the software fee in
    `docs/hackathon/evidence/cool/session.md`, run `python scripts/vision_eval.py index`, and update
    the tracker's C1, C2, C3 and S7 rows and O5; then open the pull request, whose checks run the
    PII and secret scans (the instance has no `.pii-denylist`). `recon.md` and
    `cool-buildinfo.txt` are committed from the 16 October reconnaissance; step 4 says when to
    replace them. Nothing in `$E` holds an account id, an instance id or an IP: `--imds` keeps five
    fields, and the run files say which.

    **What the session must leave behind** (the proof that COOL executed the claimed core workload,
    all committed):

    - [ ] the `scenes --backend cool` run files, with every scene `ok` and `environment.opencv.build_sha256`
      equal to step 4's `sha256sum ~/cool-buildinfo.txt` (the committed `cool-buildinfo.txt`), taken
      from the product's own `cv2`;
    - [ ] in those run files, `environment.libraries`: the `cv2` extension and the OpenCV and
      KleidiCV shared objects by path, size, sha256 and owning package or AMI path, and
      `environment.build_lines`;
    - [ ] optionally `perf-dso.txt` (step 9);
    - [ ] the CPU is Neoverse V2 (`lscpu.txt`, and `environment.cpu` in every run);
    - [ ] `environment.host.marketplace_product_codes` names the COOL product;
    - [ ] the two `controls` run files: `not_cool` under stock with the same pin, `not_cool` with
      no pin, `backend_mismatch` for an `opencv` request;
    - [ ] the `compare` run files with `parity: pass` for the stock-COOL pairs, on the same scene
      and network;
    - [ ] the speed-up on the compute stages, per stage, with its interval, from the same files;
    - [ ] optionally the agent session's replay run (step 12).

    **The claim's scope**, as the report words it: "the same worker handler, run on Graviton4 with
    COOL, on real Sydney scenes and its real OpenStreetMap network, and (if step 12 ran) inside the
    agent's loop; the deployed worker runs stock OpenCV 5 on Lambda arm64 (CPU named in its
    logs)". Not "COOL is deployed": a deployed COOL worker (Terraform for `VISION_COOL_WORKER_ARN`
    and a container Lambda or ECS) is out of scope until after the deadline, and whether COOL runs
    on Lambda's CPUs is the open question in [vision.md §10](enhancements/vision.md#10-still-open).
12. **Optional: the agent session (Wednesday 21, about an hour, once the evaluation's `replay`
    and `stability` exist).** On the same kind of instance, with the cache from step 8:
    - give the instance role, for this hour only, one statement: `bedrock:InvokeModel` on the one
      inference profile the topics use and its foundation models, and remove it afterwards;
    - `~/cool/bin/python scripts/vision_eval.py replay --sequence sydney-demo --agent bedrock --model-id $M --region ap-southeast-2 --worker in-process --backend cool --cool-sha $SHA $C --out $E --tag cool`
      (`M` as in [the evaluation's runs](deployment-runsheet.md#the-evaluations-real-data-runs), step 4):
      COOL then executes the agent's `look_again` re-measurements inside the agentic loop, in
      us-west-2, with production's deadline;
    - `~/stock/bin/python scripts/vision_eval.py stability --set year-2024 $C --out $E`: the year of
      clear Sydney scenes through the production rule, in-Region.
