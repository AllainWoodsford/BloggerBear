"""Every Terraform root tags what it creates (provider default_tags), so the account can be filtered
by Project/Environment/ManagedBy -- by a person, or by an agent looking for orphaned resources or
ones to import -- and the deploy role may tag API Gateway, whose tags live at a separate ARN."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(*parts: str) -> str:
    return (ROOT / "infra").joinpath(*parts).read_text(encoding="utf-8")


def _provider_blocks(text: str) -> list[str]:
    return re.findall(r'^provider "aws" \{\n(.*?)^\}', text, re.S | re.M)


@pytest.mark.parametrize("env", ["dev", "production"])
def test_every_provider_in_an_environment_tags_what_it_creates(env):
    text = _read("environments", env, "main.tf")

    blocks = _provider_blocks(text)
    assert len(blocks) == 2  # the default provider and the us_east_1 alias
    for block in blocks:
        assert "default_tags {\n    tags = local.default_tags\n  }" in block
    tags = re.search(r"default_tags = \{\n(.*?)\n  \}", text, re.S).group(1)
    assert 'ManagedBy     = "Terraform"' in tags
    assert 'Project       = "BloggerBear"' in tags
    assert f'Environment   = "{env}"' in tags
    assert f'TerraformRoot = "infra/environments/{env}"' in tags


def test_bootstrap_tags_what_it_creates_as_shared():
    (block,) = _provider_blocks(_read("bootstrap", "main.tf"))

    assert 'ManagedBy     = "Terraform"' in block
    assert 'Project       = "BloggerBear"' in block
    assert 'Environment   = "shared"' in block
    assert 'TerraformRoot = "infra/bootstrap"' in block


def test_tags_are_only_set_as_provider_defaults():
    """A resource-level tag with the same key as a default tag makes every plan show a change, so
    for now every `tags =` is a provider's default_tags. (A resource may add its own *other* keys
    later; then this test should allow them.)"""
    for path in (ROOT / "infra").rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(lines):
            if re.match(r"^\s+tags\s*=", line):
                assert lines[n - 1].strip() == "default_tags {", f"{path}:{n + 1}"


def test_the_deploy_role_may_tag_api_gateway():
    text = _read("bootstrap", "main.tf")
    statement = re.search(r'sid     = "ApiGateway"(.*?)\n  \}', text, re.S).group(1)

    assert '"arn:aws:apigateway:ap-southeast-2::/tags/*"' in statement
