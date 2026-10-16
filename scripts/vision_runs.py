"""One run file per run, and the pinned inputs every real-data run reads.

The evaluation (`vision_eval.py`) and the benchmark (`vision_benchmark.py`) write the same record,
`bloggerbear.vision-eval/1`, so one index can cover both and a judge reads every number from a
file with the code, the machine, the inputs and the build that made it. This module is what both
import: the schema and the run id rule, the code identity (git), where the run happened, the
digests, the portable peak-memory reading, and the inputs' side: the asset `HEAD` check (an input
that changed since it was pinned refuses the run) and the range cache (`--cache DIR`), so every
repeat and both backends read byte-identical bytes.

It runs on Windows too: nothing here needs `resource` or `sched_setaffinity` to import.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "bloggerbear.vision-eval/1"
# The kinds that exist now; the evaluation's later subcommands add theirs here.
KINDS = ("bench-kernels", "bench-scenes", "bench-worker", "bench-compare", "bench-controls", "eval-coverage")
RUNS_DIR = ROOT / "docs" / "hackathon" / "evidence" / "runs"
# The only host a real-data run reads from (AWS Open Data's Sentinel-2 COGs, us-west-2).
BUCKET_HOST = "sentinel-cogs.s3.us-west-2.amazonaws.com"
BUCKET_PREFIX = f"https://{BUCKET_HOST}/"
# A HEAD for an asset's ETag and size, before any byte of it is read.
HEAD_TIMEOUT_SECONDS = 10.0
# The cache's index (what each stored range is, by key) inside the cache directory.
CACHE_INDEX = "index.json"


class InputChanged(Exception):
    """An asset's ETag differs from the one pinned: the run is refused before anything is read."""


class WrongHost(ValueError):
    """A URL at any host but the bucket: refused before any request."""


# --- run files -----------------------------------------------------------------------------------


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def cpu_slug(cpu: str) -> str:
    """The CPU's name as a file-name token: `Neoverse V2 (Graviton4)` is `neoverse-v2-graviton4`,
    an Intel model name loses its (R)/(TM) marks and its `@`; at most 40 characters."""
    text = re.sub(r"\((r|tm)\)", " ", cpu.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40].rstrip("-") or "unknown"


def make_run_id(kind: str, cpu: str, backend: str, tag: str | None = None, date: str | None = None) -> str:
    """`<UTC date>-<kind>-<cpu slug>-<backend>`, plus `-<tag>` when the run is one of several of
    that kind on one day (the runsheet's `1t`, `4t-r1`)."""
    if kind not in KINDS:
        raise ValueError(f"unknown run kind {kind!r}")
    parts = [date or utc_now()[:10], kind, cpu_slug(cpu), re.sub(r"[^a-z0-9-]+", "-", backend.lower())]
    if tag:
        parts.append(re.sub(r"[^A-Za-z0-9.-]+", "-", tag).strip("-"))
    return "-".join(parts)


def code_identity(root: Path = ROOT, run: Callable = subprocess.run) -> dict:
    """`git_sha` and `dirty` (uncommitted changes under the repository) from `git`; both null when
    the directory is not a git checkout or git is not installed."""
    try:
        sha = run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10)
        status = run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return {"git_sha": None, "dirty": None}
    if sha.returncode != 0 or status.returncode != 0:
        return {"git_sha": None, "dirty": None}
    return {"git_sha": sha.stdout.strip(), "dirty": bool(status.stdout.strip())}


def where(
    host: dict | None = None,
    environ: dict | None = None,
    system: str | None = None,
    release: str | None = None,
    dmi_vendor: str | None = None,
) -> str:
    """Where the run happened: `ci`, `windows`, `macos`, `wsl`, `ec2` (IMDS answered, or the DMI
    vendor says Amazon EC2) or `linux`."""
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_ACTIONS") or environ.get("CI"):
        return "ci"
    system = system or platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    release = release if release is not None else platform.release()
    if "microsoft" in release.lower():
        return "wsl"
    if host:
        return "ec2"
    if dmi_vendor is None:
        try:
            dmi_vendor = Path("/sys/devices/virtual/dmi/id/sys_vendor").read_text(encoding="utf-8")
        except OSError:
            dmi_vendor = ""
    return "ec2" if "amazon ec2" in dmi_vendor.lower() else "linux"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical(value):
    """`value` with every float rounded to 6 places (ints, strings, lists and dicts as they are),
    so two machines that agree to the metrics' own precision hash alike."""
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, dict):
        return {str(k): canonical(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [canonical(v) for v in value]
    return value


def digest(value) -> str:
    """The sha256 of `value`'s canonical JSON: sorted keys, floats rounded to 6 places."""
    text = json.dumps(canonical(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return sha256_bytes(text.encode("utf-8"))


def peak_rss_mb(children: bool = False) -> float | None:
    """This process's peak resident set in MB (or its finished children's, `children`), from
    `resource.getrusage`: `ru_maxrss` is kilobytes on Linux and bytes on macOS, so the unit is
    chosen by platform. None on Windows, which has no `resource` module: the peak is not measured
    there, and the run file says so by holding null."""
    try:
        import resource
    except ImportError:  # Windows
        return None
    who = resource.RUSAGE_CHILDREN if children else resource.RUSAGE_SELF
    return rss_to_mb(resource.getrusage(who).ru_maxrss)


def rss_to_mb(maxrss: int | float, system: str | None = None) -> float:
    """`ru_maxrss` in MB: kilobytes on Linux, bytes on macOS (the BSD lineage)."""
    kb = maxrss / 1024.0 if (system or sys.platform) == "darwin" else float(maxrss)
    return round(kb / 1024.0, 1)


def affinity() -> list[int] | None:
    """The CPUs this process may run on (`taskset` shows here), where the OS reports it."""
    try:
        return sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # Windows, macOS
        return None


def environment(cpu: str, opencv: dict, threads: int | None = None, **extra) -> dict:
    """The run's `environment`: the CPU and machine, the interpreter and its libraries' versions,
    the OpenCV build record with `cv2`'s path, the cores, the affinity, the threads OpenCV uses,
    `where`, and whatever `extra` the caller measured (`host`, `cool`, `libraries`)."""
    import numpy

    try:
        import networkx

        networkx_version = networkx.__version__
    except ImportError:
        networkx_version = None
    try:
        import cv2

        cv2_path, cv2_threads = cv2.__file__, cv2.getNumThreads()
    except ImportError:
        cv2_path, cv2_threads = None, None
    env = {
        "cpu": cpu,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "opencv": {**opencv, "cv2_path": cv2_path},
        "cpu_count": os.cpu_count(),
        "affinity": affinity(),
        "threads": threads if threads is not None else cv2_threads,
        "numpy": numpy.__version__,
        "networkx": networkx_version,
        "where": where(host=extra.get("host")),
        "host": None,
        "cool": None,
        "libraries": None,
    }
    env.update(extra)
    return env


def new_run(
    kind: str,
    cpu: str,
    backend: str,
    environment_: dict,
    *,
    worker: dict | None = None,
    inputs: dict | None = None,
    params: dict | None = None,
    tag: str | None = None,
    mode: str | None = None,
) -> dict:
    """A run record with its `started_at`; `finish` fills the rest."""
    started = utc_now()
    return {
        "schema": SCHEMA,
        "run_id": make_run_id(kind, cpu, backend, tag, started[:10]),
        "kind": kind,
        "mode": mode or kind.split("-", 1)[1],
        "backend": backend,
        "started_at": started,
        "finished_at": None,
        "code": code_identity(),
        "environment": environment_,
        "worker": worker or {"kind": "in-process", "region": None, "code_sha256": None},
        "inputs": {"manifest_sha256": None, "cache": False, "scenes": [], "networks": [], **(inputs or {})},
        "params": params or {},
        "results": {},
        "agent": None,
        "checks": [],
        "summary": {},
    }


def finish(run: dict, results, checks: list[dict] | None = None, summary: dict | None = None) -> dict:
    run["results"] = results
    run["checks"] = list(checks or [])
    run["summary"] = dict(summary or {})
    run["finished_at"] = utc_now()
    return run


def out_path(run: dict, out: Path) -> Path:
    """Where `run` goes for `--out`: `<out>/<run_id>.json` when `out` is a directory (one that
    exists, or any path not ending in `.json`), else the file itself."""
    out = Path(out)
    if out.is_dir() or out.suffix.lower() != ".json":
        out = out / f"{run['run_id']}.json"
    return out


def write(run: dict, out: Path) -> Path:
    """`run` as JSON at `out_path(run, out)`; the directory is made."""
    path = out_path(run, out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(run, indent=2) + "\n", encoding="utf-8")
    return path


def load(path: Path) -> dict:
    """A run file read back, refused (ValueError naming the file) when it is not one."""
    path = Path(path)
    try:
        run = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: not a run file ({exc})") from exc
    if not isinstance(run, dict) or run.get("schema") != SCHEMA:
        raise ValueError(f"{path}: not a {SCHEMA} run file")
    if run.get("kind") not in KINDS:
        raise ValueError(f"{path}: unknown run kind {run.get('kind')!r}")
    for key in ("run_id", "environment", "inputs", "results"):
        if key not in run:
            raise ValueError(f"{path}: run file without {key!r}")
    return run


def worker_code_sha256(lambdas: Path = ROOT / "lambdas") -> str | None:
    """The in-process worker's code identity: the sha256 over the files the worker package ships
    (the handler, `vision/*.py`, `common/vision_contract.py`), in path order."""
    files = [lambdas / "vision_worker_handler.py", lambdas / "common" / "vision_contract.py"]
    files += sorted((lambdas / "vision").glob("*.py"))
    digest_ = hashlib.sha256()
    try:
        for file in files:
            digest_.update(file.relative_to(lambdas).as_posix().encode("utf-8"))
            digest_.update(file.read_bytes())
    except OSError:
        return None
    return digest_.hexdigest()


# --- pinned inputs ---------------------------------------------------------------------------------


def check_host(url: str) -> str:
    """`url` when it is under the bucket; WrongHost otherwise, before any request is made."""
    if not isinstance(url, str) or not url.startswith(BUCKET_PREFIX):
        raise WrongHost(f"refused: not under {BUCKET_PREFIX}: {url!r}")
    return url


def default_head(url: str, timeout: float = HEAD_TIMEOUT_SECONDS) -> dict:
    """One HEAD of a bucket URL: `etag` (unquoted) and `bytes` (Content-Length), with `timeout`
    for the connection and the reply alike."""
    import requests

    response = requests.head(check_host(url), timeout=(timeout, timeout), allow_redirects=False)
    if response.status_code != 200:
        raise InputChanged(f"HEAD {url}: HTTP {response.status_code}")
    return head_fields(response.headers)


def head_fields(headers) -> dict:
    etag = (headers.get("ETag") or "").strip().strip('"')
    length = headers.get("Content-Length")
    return {"etag": etag or None, "bytes": int(length) if length is not None else None}


def head_assets(urls: list[str], head: Callable | None = None) -> dict[str, dict]:
    """`{url: {etag, bytes}}` for every URL, through `head` (`default_head` when not given)."""
    head = head or default_head
    out = {}
    for url in dict.fromkeys(urls):
        out[url] = head(check_host(url))
    return out


def check_pinned(heads: dict[str, dict], pinned: dict[str, str]) -> None:
    """InputChanged, naming the asset, when a HEAD's ETag differs from the manifest's pin."""
    for url, expected in pinned.items():
        got = (heads.get(url) or {}).get("etag")
        if expected and url in heads and got != expected:
            raise InputChanged(f"input changed: {url} (pinned ETag {expected}, now {got})")


def pinned_etags(manifest: dict | None) -> dict[str, str]:
    """`{url: etag}` for every asset the manifest pins (an empty ETag pins nothing)."""
    out = {}
    for scene in (manifest or {}).get("scenes") or []:
        for asset in (scene.get("assets") or {}).values():
            if isinstance(asset, dict) and asset.get("url") and asset.get("etag"):
                out[asset["url"]] = asset["etag"]
    return out


def default_cache_dir() -> Path:
    """`~/.cache/bloggerbear-cogs`, or `%LOCALAPPDATA%\\bloggerbear-cogs` on Windows: outside the
    repository, never committed."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "bloggerbear-cogs"
    return Path.home() / ".cache" / "bloggerbear-cogs"


class CachedFetch:
    """A `fetch(url, start, end)` that answers each range from `directory`, filling it on the first
    read through `fetch`. The key is the sha256 of the URL, its ETag and the range, so a changed
    object never answers from the old bytes; `index.json` maps keys to `{url, etag, start, end,
    bytes}`. An index entry for the same URL and range under another ETag is refused: its file is
    deleted and the range re-fetched. ETags come from `etags` (the run's HEADs) or one `head` call
    per URL not in it."""

    def __init__(self, directory: Path, etags: dict[str, str] | None = None, fetch=None, head=None):
        self.directory = Path(directory)
        self.etags = dict(etags or {})
        self.fetch = fetch
        self.head = head or default_head
        self.hits = self.misses = self.refused = 0
        self.directory.mkdir(parents=True, exist_ok=True)
        self.index = self._read_index()

    def _read_index(self) -> dict:
        path = self.directory / CACHE_INDEX
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_index(self) -> None:
        path = self.directory / CACHE_INDEX
        path.write_text(json.dumps(self.index, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    def index_sha256(self) -> str | None:
        path = self.directory / CACHE_INDEX
        return sha256_file(path) if path.is_file() else None

    def etag_for(self, url: str) -> str:
        if url not in self.etags:
            self.etags[url] = self.head(check_host(url))["etag"] or ""
        return self.etags[url]

    @staticmethod
    def key(url: str, etag: str, start: int, end: int) -> str:
        return sha256_bytes(f"{url}\n{etag}\n{start}-{end}".encode())

    def _path(self, key: str) -> Path:
        return self.directory / key[:2] / key

    def __call__(self, url: str, start: int, end: int) -> bytes:
        etag = self.etag_for(url)
        key = self.key(url, etag, start, end)
        entry = self.index.get(key)
        path = self._path(key)
        if entry and entry.get("etag") == etag and path.is_file():
            self.hits += 1
            return path.read_bytes()
        # A stale entry: the same URL and range, another ETag. Refused, never served.
        for old_key, old in list(self.index.items()):
            same_range = old.get("url") == url and old.get("start") == start and old.get("end") == end
            if same_range and old_key != key:
                self._path(old_key).unlink(missing_ok=True)
                del self.index[old_key]
                self.refused += 1
        if self.fetch is None:
            raise RuntimeError("the cache has no fetch to fill from")
        data = self.fetch(url, start, end)
        self.misses += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.index[key] = {"url": url, "etag": etag, "start": start, "end": end, "bytes": len(data)}
        self._write_index()
        return data
