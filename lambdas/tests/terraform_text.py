"""Reading infra/ as text, for the tests that hold the Terraform to names.

Every resource name is "<prefix>-<env>-<resource>", and the prefix is a variable
(var.unique_name_prefix, default "bloggerbear"), so the files say
"${var.unique_name_prefix}-dev-topics" where the deployed table is bloggerbear-dev-topics. The
tests assert the deployed names: the ones the original deployment has, which must not change by a
character, because a renamed table or bucket is one Terraform destroys and makes again, empty.

`with_default_prefix` is what makes that honest rather than a text trick: it writes the default in
place of the variable, which is exactly what Terraform does when nothing sets it. That the default
is still "bloggerbear", in every root, and that no name skips the variable, is checked once, in
test_terraform_wiring.py (the name-prefix tests at its end).
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"

PREFIX_VARIABLE = "unique_name_prefix"
PREFIX_REFERENCE = "${var.unique_name_prefix}"
DEFAULT_PREFIX = "bloggerbear"


def with_default_prefix(text: str) -> str:
    """Terraform text as it reads with the prefix left at its default."""
    return text.replace(PREFIX_REFERENCE, DEFAULT_PREFIX)


def read_terraform(path: Path) -> str:
    """One infra/ file, with the default prefix written in."""
    return with_default_prefix(path.read_text(encoding="utf-8"))


def terraform_files() -> list[Path]:
    """Every .tf file in infra/ that is ours (no provider cache, no Lambda build output)."""
    return sorted(
        path
        for path in INFRA.rglob("*.tf")
        if ".terraform" not in path.parts and "lambda-build" not in path.parts
    )
