"""The COOL reconnaissance's build information (docs/hackathon/evidence/cool/cool-buildinfo.txt) is
the fingerprint every COOL run is checked against: `--cool-sha` and `COOL_BUILD_SHA256` pin its
SHA-256, and the worker's `not_cool` guard compares each build with it. So the committed text must
hash to the pin that recon.md and the runsheet's commands name, byte for byte (a line-ending
conversion on a checkout would change it, which is what `.gitattributes` prevents), and the
worker's own `build_info()` must read it as the session will: the same hash, KleidiCV detected.
Nothing here touches the network.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lambdas"))

from vision import build

ROOT = Path(__file__).resolve().parents[2]
COOL = ROOT / "docs" / "hackathon" / "evidence" / "cool"
BUILDINFO = COOL / "cool-buildinfo.txt"
RUNSHEET = ROOT / "docs" / "cool-benchmark-runsheet.md"
# COOL 3.1 (OpenCV 5.1.0-dev) on the us-west-2 AMI, 16 October 2026.
PINNED = "12740978f31483f2352d9e8b367f4f6053c3bfb58618e2ff42d53443da2a8ec4"


def test_the_committed_build_information_hashes_to_the_pin():
    assert hashlib.sha256(BUILDINFO.read_bytes()).hexdigest() == PINNED


def test_recon_and_the_runsheet_name_the_same_pin():
    assert PINNED in (COOL / "recon.md").read_text(encoding="utf-8")
    assert f"SHA={PINNED}" in RUNSHEET.read_text(encoding="utf-8")


def test_the_file_is_kept_out_of_line_ending_conversion():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    assert "docs/hackathon/evidence/cool/*buildinfo.txt -text -whitespace" in attributes


def test_the_worker_reads_it_as_the_cool_build(monkeypatch):
    monkeypatch.setattr(build.cv2, "getBuildInformation", lambda: BUILDINFO.read_text(encoding="utf-8"))
    info = build.build_info()
    assert info["build_sha256"] == PINNED
    assert build.matches(info, PINNED)
    assert info["kleidicv"] is True
    assert info["cpu_baseline"] == "SVE NEON FP16 NEON_DOTPROD NEON_FP16 NEON_BF16"
