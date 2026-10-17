"""Which OpenCV build did the work.

The COOL award needs *verified* COOL integration, and the benchmark needs to know which build each
number came from. Every worker result carries this record. It can't tell COOL from stock OpenCV
by looking (stock aarch64 builds can include KleidiCV too), so the check is against a pinned
fingerprint: when the COOL image is pinned, the SHA-256 of its `cv2.getBuildInformation()` is
recorded beside it, and a `cool` request answered by any other build fails rather than counting.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re

import cv2

from common.vision_contract import cpu_identity


def build_info() -> dict:
    """`opencv_version`, `build_sha256` (of the full build information), `arch` (the machine),
    `kleidicv` (whether the build says it uses KleidiCV), `cpu_baseline` / `cpu_dispatch` as the
    build reports them, and the CPU the build ran on: `cpu` (common/vision_contract.cpu_identity:
    the x86 model name or the Arm core and Graviton generation, which answers which CPU Lambda's
    arm64 gives the worker from its first invoke) and `cpu_count` (the cores this process may
    use). The fingerprint stays the hash of the build information alone: the same build on
    another CPU is the same build."""
    text = cv2.getBuildInformation()
    return {
        "opencv_version": cv2.__version__,
        "build_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "arch": platform.machine(),
        "kleidicv": bool(re.search(r"kleidicv[^\n]*\b(YES|ON|enabled|\d+\.\d+)", text, re.IGNORECASE)),
        "cpu_baseline": _field(text, "Baseline"),
        "cpu_dispatch": _field(text, "Dispatched code generation"),
        "cpu": cpu_identity(),
        "cpu_count": _cpu_count(),
    }


def _cpu_count() -> int | None:
    """The cores this process may run on: the affinity mask where there is one (Linux), else the
    machine's count (Windows, macOS), else None."""
    try:
        return len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return os.cpu_count()


def matches(info: dict, expected_sha256: str | None) -> bool:
    """True only when a fingerprint is pinned and this build has it. No pin is never a match: an
    unconfigured COOL backend must not pass for COOL."""
    return bool(expected_sha256) and info.get("build_sha256") == expected_sha256.strip().lower()


def _field(text: str, label: str) -> str | None:
    match = re.search(rf"^\s*{re.escape(label)}:\s*(.+)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None
