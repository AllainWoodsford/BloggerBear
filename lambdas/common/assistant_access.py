"""The `assistant_access` setting: who may reach the operator's assistant at all.

One value on the config table's `pipeline` row, changed with `pipeline-config set
--assistant-access` and read on every request to the assistant (ops_mcp/access.py):

    open       any signed-in caller, from anywhere (the default, and what no setting means)
    allowlist  only from the operator's addresses
    off        every request is refused

This module is only the values and their validation, so the admin API can check what it
stores without importing the assistant's own code.
"""

from __future__ import annotations

ACCESS_OPEN = "open"
ACCESS_ALLOWLIST = "allowlist"
ACCESS_OFF = "off"
ASSISTANT_ACCESS_VALUES = (ACCESS_OPEN, ACCESS_ALLOWLIST, ACCESS_OFF)
DEFAULT_ASSISTANT_ACCESS = ACCESS_OPEN


def assistant_access_error(value) -> str | None:
    """A message if `value` isn't a valid stored `assistant_access`, else None. None (unset)
    is valid and means the default."""
    if value is None or value in ASSISTANT_ACCESS_VALUES:
        return None
    return (
        f"must be one of {', '.join(ASSISTANT_ACCESS_VALUES)}, or null for the default "
        f"({DEFAULT_ASSISTANT_ACCESS})"
    )


def effective_assistant_access(pipeline_config: dict | None) -> str:
    """What the stored setting does, for showing the operator: the value itself, `open` when
    nothing is set, and `off` for a value that isn't one of the three. Unlike the review mode,
    a value that can't be understood does not fall back to the default: the default here is
    the most permissive one, so the assistant refuses every request until it is corrected."""
    value = (pipeline_config or {}).get("assistant_access")
    if value is None:
        return DEFAULT_ASSISTANT_ACCESS
    if isinstance(value, str) and value in ASSISTANT_ACCESS_VALUES:
        return value
    return ACCESS_OFF
