"""What this deployment's resources are called.

Every resource Terraform makes is named "<prefix>-<environment>-<resource>": "bloggerbear-dev-topics"
in the original deployment. The prefix is a setting (Terraform's var.unique_name_prefix, the
UNIQUE_NAME_PREFIX GitHub Actions variable), so that a second deployment can have names of its own:
S3 bucket names are unique across every AWS account.

Code that builds a resource's name, or recognises one, takes the prefix from here and never writes
it out: the per-topic schedules (common/scheduler.py) and everything the operator's assistant knows
by name (ops_mcp/architecture.py, account.py, firewall.py).

**Where it comes from.** The NAME_PREFIX environment variable, which Terraform sets on every
function, read once, when this module is first imported. An environment variable rather than an SSM
parameter: it costs nothing per invocation and needs no permission, where a parameter costs a call
on every cold start and an IAM statement on every role. Unset (a test, a script run by hand), it is
the original deployment's.

The prefix has no trailing hyphen; the names add it. It is not the project's name: the site's
title and the CloudWatch metric namespace stay "BloggerBear" whatever this is. (The "Project" tag
does follow it, in Terraform: "BloggerBear" for the default prefix, the prefix itself otherwise.
Code never writes that tag out either; ops_mcp/samples.py is handed it as OPS_DEFAULT_TAGS.)
"""

from __future__ import annotations

import os

NAME_PREFIX_ENV = "NAME_PREFIX"
DEFAULT_NAME_PREFIX = "bloggerbear"

# Terraform validates the value (lowercase letters, digits and hyphens, starting with a letter, not
# ending with a hyphen), so nothing is checked again here. Empty counts as unset.
NAME_PREFIX = os.environ.get(NAME_PREFIX_ENV) or DEFAULT_NAME_PREFIX


def environment_prefix(environment: str) -> str:
    """What every name in one environment starts with: "bloggerbear-dev-" for "dev"."""
    return f"{NAME_PREFIX}-{environment}-"
