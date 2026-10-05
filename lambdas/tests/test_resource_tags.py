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


# The Project tag's value: "BloggerBear" for the default name prefix (the tag the original
# deployment's resources have always carried, so none of them is re-tagged), and for any other
# deployment its own prefix, as given. The assistant's table access is conditioned on this tag, so
# a fork's tables are told apart from the original's by it.
PROJECT_TAG_RULE = 'var.unique_name_prefix == "bloggerbear" ? "BloggerBear" : var.unique_name_prefix'


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
    assert "Project       = local.project_tag" in tags
    assert f"\n  project_tag = {PROJECT_TAG_RULE}\n" in text
    assert f'Environment   = "{env}"' in tags
    assert f'TerraformRoot = "infra/environments/{env}"' in tags


def test_bootstrap_tags_what_it_creates_as_shared():
    (block,) = _provider_blocks(_read("bootstrap", "main.tf"))

    assert 'ManagedBy     = "Terraform"' in block
    assert f"Project       = {PROJECT_TAG_RULE}" in block
    assert 'Environment   = "shared"' in block
    assert 'TerraformRoot = "infra/bootstrap"' in block


def test_the_project_tag_is_bloggerbear_by_default_and_the_prefix_otherwise():
    """One rule, written the same way in all three roots, and nowhere a literal: with the default
    prefix the tag is byte for byte what it was, and nothing else in infra/ sets or compares a
    Project tag except through the root's default_tags."""
    for root in ("bootstrap", "environments/dev", "environments/production"):
        text = _read(*root.split("/"), "main.tf")
        assert text.count(PROJECT_TAG_RULE) == 1, root
        variables = _read(*root.split("/"), "variables.tf")
        block = variables.split('variable "unique_name_prefix" {')[1].split("\n}\n")[0]
        assert re.search(r'^  default\s*=\s*"bloggerbear"$', block, re.M), root
    for path in (ROOT / "infra").rglob("*.tf"):
        if ".terraform" in path.parts:
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        code = "\n".join(line for line in lines if not line.lstrip().startswith("#"))
        assert not re.search(r'Project\s*=\s*"', code), path
    # The module that conditions on the tag has no default of its own to fall back on.
    module = _read("modules", "ops-assistant", "variables.tf")
    tags = module.split('variable "default_tags" {')[1].split("\n}\n")[0]
    assert not re.search(r"^  default\s*=", tags, re.M)
    policy = _read("modules", "ops-assistant", "main.tf")
    assert 'variable = "aws:ResourceTag/Project"\n      values   = [var.default_tags["Project"]]' in policy


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

    # The region is the home region's variable (docs/deployment-runsheet.md), not a literal.
    assert '"arn:aws:apigateway:${var.aws_region}::/tags/*"' in statement
