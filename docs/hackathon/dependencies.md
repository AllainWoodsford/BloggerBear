# Dependencies: what is pinned, where, and what still floats

An audit of every dependency the Rail Access Monitor builds, deploys or tests with, as at 4a3d1b8
(16 October). The short answer: every direct dependency is pinned to one version, Terraform's
providers are locked, every CI action is pinned by commit, and the judges' Python environment is
pinned whole by `constraints-py311.txt`. What still floats is the deployed Lambdas' transitive
Python packages, which are resolved at each build (below, "The gap").

## Python

Every non-comment line of every `requirements*.txt` is `name==version` or an include (`-r`), and
one package has one version across all of them: `lambdas/tests/test_requirements.py` fails
otherwise.

| File | Pins | Installed by |
|---|---|---|
| `lambdas/requirements.txt` | `requests==2.34.2`, `markdown==3.11`, `tzdata==2025.2` | the one package every pipeline Lambda shares (the research tick, the daily cycle, the APIs and the jobs): `infra/environments/dev/main.tf:288`, `infra/environments/production/main.tf:389`; the ops MCP server's package beside its own file; and CI |
| `lambdas/requirements-ops-mcp.txt` | `mcp==2.1.1` | the ops MCP server's package: `infra/modules/ops-assistant/main.tf:141-144` |
| `lambdas/requirements-ops-agent.txt` | `strands-agents==1.57.2`, and the MCP server's file | the ops agent's package: `infra/modules/ops-assistant/agent.tf:99-101` |
| `lambdas/requirements-vision.txt` | `opencv-python-headless==5.0.0.93`, `numpy==2.4.4`, `requests==2.34.2`, `networkx==3.4.2` | the vision worker's package, for arm64 and Python 3.12 (`--platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 --only-binary=:all:`): `infra/modules/vision-worker/main.tf:88-91`; the Graviton session's stock venv |
| `lambdas/requirements-vision-cool.txt` | `requests==2.34.2`, `awslambdaric==4.1.0`, `networkx==3.4.2`; never OpenCV or numpy, which the COOL image brings | the COOL worker image (`docker/vision-cool/Dockerfile`); the Graviton session's COOL venv takes its pins without `awslambdaric` |
| `lambdas/requirements-dev.txt` | the four files above (`-r`), and `boto3==1.34.162`, `moto==5.0.13`, `pytest==9.1.1`, `PyYAML==6.0.3`, `ruff==0.6.9` | CI's Python job (`.github/workflows/python-ci.yml`); a developer's machine |
| `scripts/requirements.txt` | `requests==2.34.2`, `boto3==1.34.162`, `rjsmin==1.2.5`, `rcssmin==1.2.2` | CI; the admin CLI and the operator's scripts; the site's minifier at deploy |
| `constraints-py311.txt` (repository root) | all 68 distributions CI's job installs, transitive ones included | the judges only: [REPRODUCE.md](REPRODUCE.md) tier 1a passes it with `-c`. Neither CI's install nor any Lambda's packaging reads it |

The Graviton session's third venv, for the "what OpenCV 5 changed" row, pins
`opencv-python-headless==4.14.0.94` beside `numpy==2.4.4`, `requests==2.34.2` and `networkx==3.4.2`
([the evidence README](evidence/README.md#the-4x-pin); the runsheet's
[COOL section](../deployment-runsheet.md#the-cool-backend-and-the-benchmark), step 7).

### The gap: transitive packages are resolved at each Lambda build

Each Lambda package is built by Terraform with `pip install --upgrade -r <file> -t <dir>`
(`infra/environments/dev/main.tf:288`, `infra/environments/production/main.tf:389`,
`infra/modules/vision-worker/main.tf:88-91`, `infra/modules/ops-assistant/main.tf:141-144`,
`infra/modules/ops-assistant/agent.tf:99-101`). The direct packages are pinned; what *they* depend
on (urllib3, certifi, idna and charset-normalizer under `requests`, for one) is whatever newest
version their own ranges allow on the day of the build. Two deploys a month apart can therefore
ship different transitive versions of the same pins. That is a deliberate trade, not an oversight:
a security fix in a transitive package reaches the next deploy without a commit, and Trivy scans
every requirements file on every pull request and before every apply
(`.github/workflows/security.yml`).

What the judges get is pinned anyway: `constraints-py311.txt` locks the whole environment the test
suite runs in, so tier 1a reproduces CI's environment exactly, whenever it is run.

**Not done, by choice: `lambdas/constraints-vision.txt`** for the worker's transitive packages. It
would mean taking them from `pip download` with the worker's platform flags, passing them with `-c`
in the worker's pip line, and updating `test_vision_terraform.py`'s exact pin of that line: a change
to a deployed package days before the deadline, for no change in what the worker measures. The
audit above and the judges' constraints file answer the rubric's "pinned dependencies"; the
worker's own constraints file is the owner's call after the deadline.

### Where `constraints-py311.txt` comes from

`python-ci.yml` prints `pip freeze --all` right after its install step, in every run (the step
only prints: nothing is written or uploaded). At the submission tag, that output, copied from the
tag's CI run, replaces the file's list. Until then the file was generated on 16 October from a
Python 3.11.17 venv built from the same three requirements files, whose 68 distributions are
exactly their dependency closure (pip, setuptools and wheel are not listed: that venv had none).
A fresh venv installed from it with all three files resolved those 68 and nothing else, on the
same day. `lambdas/tests/test_requirements.py` holds every direct pin equal to its line in the
file, so a requirements change that forgets it fails CI. The file is outside Trivy's
`--file-patterns` (`security.yml`) on purpose: it deploys nothing, and every package in it comes
from a requirements file Trivy does scan, or from one of their dependencies.

## Terraform

| What | Version | Where |
|---|---|---|
| The CLI | 1.16.2, downloaded and checked by every workflow that runs Terraform | `.github/workflows/terraform.yml:149`, `pr-checks.yml:33`, `terraform-production-release.yml:127`, `on-demand-scan.yml:47`, `destroy-dev.yml:71` |
| `hashicorp/aws` | 6.64.0 | the lock files: `infra/environments/dev/.terraform.lock.hcl`, `infra/environments/production/.terraform.lock.hcl`, `infra/bootstrap/.terraform.lock.hcl` |
| `hashicorp/archive` | 2.8.1 | the dev and production lock files |
| `hashicorp/random` | 3.9.1 | the dev and production lock files |

The lock files carry each provider's hashes, so `terraform init` refuses a provider binary that
differs from the one locked.

## CI

- **Every action is pinned by commit SHA**, never a moving tag: all 27 `uses:` of an external action
  across `.github/workflows/` name a 40-character commit.
- **The tools CI downloads are pinned too:** Trivy 0.74.0 (`security.yml:35`), Bandit 1.7.10
  (`security.yml:45`), Checkov 3.3.25 (`security.yml:120`), gitleaks 8.30.1 (`pr-checks.yml:96`),
  ruff 0.6.9 (`lambdas/requirements-dev.txt`), Terraform 1.16.2 (above).
- **The runner's Python** is `actions/setup-python` (pinned by SHA) with `python-version: '3.11'`:
  the newest 3.11 patch release on the day. The constraints file records the packages, and REPRODUCE
  asks for Python 3.11.

## The site

No third-party JavaScript: the site and the operator's ask page are plain HTML, CSS and JavaScript
from this repository, served from its own bucket; nothing is loaded from a CDN. The minifier's two
packages (`rjsmin`, `rcssmin`) are pinned above.

## COOL

The COOL product is not a package of this repository: it is OpenCV's Marketplace image or AMI, run
by hand on the Graviton instance. It is pinned at the session by its image digest
(`docker/vision-cool/Dockerfile` takes `COOL_IMAGE=<registry>/<repository>@sha256:<digest>`) or its
AMI id, and by its build fingerprint (the SHA-256 of `cv2.getBuildInformation()`). [R2: the digest
or AMI id and the fingerprint, from `evidence/cool/session.md` and `evidence/cool/recon.md`]

## Tools used only to write the submission

The SVG exports of the diagrams are made with Mermaid's command-line tool, pinned to an exact
version in [diagrams/README.md](diagrams/README.md). It is a writing tool run once, not a
dependency of anything that builds, deploys or tests.
