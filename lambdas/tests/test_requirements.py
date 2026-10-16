"""Every Python dependency the project names is pinned to one version, and the judges' constraints
file agrees with every pin.

- Every non-comment line of every `requirements*.txt` (under `lambdas/` and `scripts/`) is either an
  include (`-r other.txt`) or `name==version`: no range, no unpinned name, no URL. A floating
  direct dependency would let a Lambda's package or CI's environment change without a commit.
- `constraints-py311.txt` (the repository root) pins the whole environment CI installs, transitive
  packages included, for docs/hackathon/REPRODUCE.md's tier 1a. Every direct pin must appear in it
  at the same version, so the judges' environment never contradicts a requirements file. The one
  exception is a package only the COOL worker image installs (COOL_IMAGE_ONLY), which neither CI nor
  tier 1a installs and so cannot be in CI's freeze.

Names are compared as pip compares them (PEP 503: case-insensitive, runs of `-`, `_` and `.` alike),
since `pip freeze` prints `Markdown` where `lambdas/requirements.txt` says `markdown`. Nothing here
touches the network or imports what it reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONSTRAINTS = ROOT / "constraints-py311.txt"
# The files CI's python job installs (.github/workflows/python-ci.yml), and so the ones whose whole
# closure is in the constraints file; every other requirements file must be listed in
# NOT_INSTALLED_BY_CI with its reason.
CI_INSTALLS = (
    ROOT / "lambdas" / "requirements.txt",
    ROOT / "lambdas" / "requirements-dev.txt",
    ROOT / "scripts" / "requirements.txt",
)
NOT_INSTALLED_BY_CI = {
    # The COOL worker image's own additions (docker/vision-cool/Dockerfile); its requests and
    # networkx pins equal requirements-vision.txt's (test_vision_terraform.py holds that).
    "lambdas/requirements-vision-cool.txt",
}
# In the COOL image only: the Lambda runtime interface client for a container-image Lambda.
COOL_IMAGE_ONLY = frozenset({"awslambdaric"})

_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[A-Za-z0-9][A-Za-z0-9.+!-]*)$")
_INCLUDE = re.compile(r"^-r\s+(?P<file>\S+)$")


def canonical(name: str) -> str:
    """A distribution name as pip compares it (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def requirement_files() -> list[Path]:
    return sorted(
        path for folder in (ROOT / "lambdas", ROOT / "scripts") for path in folder.glob("requirements*.txt")
    )


def meaningful_lines(path: Path) -> list[tuple[int, str]]:
    """(line number, text) of every line that is neither blank nor a comment, a trailing comment
    removed."""
    lines = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        text = raw.split("#", 1)[0].strip()
        if text:
            lines.append((number, text))
    return lines


def pins(path: Path) -> dict[str, tuple[str, str]]:
    """{canonical name: (name as written, version)} for the file's own `name==version` lines."""
    found = {}
    for _number, text in meaningful_lines(path):
        match = _PIN.match(text)
        if match:
            found[canonical(match["name"])] = (match["name"], match["version"])
    return found


def includes(path: Path) -> list[Path]:
    return [
        (path.parent / match["file"]).resolve()
        for _number, text in meaningful_lines(path)
        if (match := _INCLUDE.match(text))
    ]


def closure(paths) -> set[Path]:
    """The files `pip install -r` reads for `paths`, following every `-r` include."""
    seen: set[Path] = set()
    stack = [Path(p).resolve() for p in paths]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        stack.extend(includes(path))
    return seen


def test_there_are_requirement_files_to_check():
    names = {path.relative_to(ROOT).as_posix() for path in requirement_files()}
    assert {"lambdas/requirements.txt", "lambdas/requirements-dev.txt", "scripts/requirements.txt"} <= names


def test_every_requirement_is_pinned():
    loose = [
        f"{path.relative_to(ROOT).as_posix()}:{number}: {text}"
        for path in requirement_files()
        for number, text in meaningful_lines(path)
        if not (_PIN.match(text) or _INCLUDE.match(text))
    ]
    assert not loose, f"every requirement must be name==version (or -r another file): {loose}"


def test_every_include_names_a_requirements_file_that_exists():
    for path in requirement_files():
        for included in includes(path):
            assert included.exists(), f"{path.relative_to(ROOT)} includes a missing file: {included}"
            assert included.name.startswith("requirements"), f"{path.relative_to(ROOT)} includes {included}"


def test_a_package_is_pinned_to_one_version_across_every_file():
    seen: dict[str, tuple[str, str]] = {}
    clashes = []
    for path in requirement_files():
        for key, (name, version) in pins(path).items():
            where = path.relative_to(ROOT).as_posix()
            if key in seen and seen[key][1] != version:
                clashes.append(f"{name}: {seen[key][0]} pins {seen[key][1]}, {where} pins {version}")
            seen.setdefault(key, (where, version))
    assert not clashes, clashes


def test_every_requirements_file_is_installed_by_ci_or_says_why_not():
    installed = {path.relative_to(ROOT).as_posix() for path in closure(CI_INSTALLS)}
    every = {path.relative_to(ROOT).as_posix() for path in requirement_files()}
    assert every - installed == NOT_INSTALLED_BY_CI, (
        "a requirements file CI does not install must be named in NOT_INSTALLED_BY_CI with its reason: "
        f"{sorted(every - installed)}"
    )


def test_the_constraints_file_is_all_pins():
    loose = [f"{number}: {text}" for number, text in meaningful_lines(CONSTRAINTS) if not _PIN.match(text)]
    assert not loose, f"constraints-py311.txt holds only name==version lines: {loose}"
    names = [canonical(_PIN.match(text)["name"]) for _number, text in meaningful_lines(CONSTRAINTS)]
    assert len(names) == len(set(names)), "a package is listed twice in constraints-py311.txt"


def test_every_direct_pin_is_in_the_constraints_file_at_the_same_version():
    constrained = pins(CONSTRAINTS)
    wrong = []
    for path in requirement_files():
        for key, (name, version) in pins(path).items():
            if key in COOL_IMAGE_ONLY:
                continue
            got = constrained.get(key)
            if got is None or got[1] != version:
                where = path.relative_to(ROOT).as_posix()
                wrong.append(f"{where}: {name}=={version}, constraints: {got[1] if got else 'missing'}")
    assert not wrong, (
        "constraints-py311.txt disagrees with a requirements file; regenerate it from CI's "
        f"`pip freeze --all` (python-ci.yml): {wrong}"
    )


def test_the_cool_image_only_packages_are_pinned_only_where_the_cool_image_installs():
    cool = pins(ROOT / "lambdas" / "requirements-vision-cool.txt")
    assert COOL_IMAGE_ONLY <= set(cool)
    for path in requirement_files():
        if path.name != "requirements-vision-cool.txt":
            where = path.relative_to(ROOT)
            assert not COOL_IMAGE_ONLY & set(pins(path)), f"{where} pins a COOL-image-only package"


@pytest.mark.parametrize(
    ("line", "pinned"),
    [
        ("requests==2.34.2", True),
        ("opencv-python-headless==5.0.0.93", True),
        ("python-dateutil==2.9.0.post0", True),
        ("requests>=2.34", False),
        ("requests", False),
        ("requests==2.*", False),
        ("requests @ https://example.com/requests.whl", False),
        ("requests==2.34.2; python_version < '3.12'", False),
    ],
)
def test_the_pin_rule(line, pinned):
    assert bool(_PIN.match(line)) is pinned


def test_names_compare_as_pip_compares_them():
    assert canonical("Markdown") == canonical("markdown")
    assert canonical("typing_extensions") == canonical("typing-extensions")
    assert canonical("zope.interface") == canonical("zope-interface")


def test_a_trailing_comment_is_not_part_of_the_pin(tmp_path):
    path = tmp_path / "requirements-x.txt"
    body = "# a comment\n\nrequests==2.34.2  # the range reads\n-r requirements.txt\n"
    path.write_text(body, encoding="utf-8")
    assert meaningful_lines(path) == [(3, "requests==2.34.2"), (4, "-r requirements.txt")]
    assert pins(path) == {"requests": ("requests", "2.34.2")}
