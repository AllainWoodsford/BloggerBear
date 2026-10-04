"""Stand-ins for the two things the ops agent's tests must never call: Bedrock and AWS.

- ScriptedModel is a Strands model that plays a script: on each call it either asks for tools or
  gives its answer. It records which tools it was offered on every call, which is how a test
  sees what the model could and could not have called.
- FakeTool is a Strands tool that returns a fixed `structuredContent`, shaped as the MCP server's
  tools return theirs, and records the arguments of every call that reached it.

Both go through the real `Agent`, its event loop and its hooks: only the two ends are fake.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from strands.models import Model
from strands.types._events import ToolResultEvent
from strands.types.tools import AgentTool


def finding(kind: str, target_id: str | None, command: str | None = None) -> dict:
    """A finding as ops_mcp/suggestions.finding builds one."""
    suggestion = (
        {"action": "Do the thing", "command": command, "what_it_does": "It does the thing."}
        if command
        else None
    )
    return {
        "kind": kind,
        "id": target_id,
        "noticed": f"{kind} noticed",
        "where": {},
        "suggestion": suggestion,
    }


class CutOff(str):
    """A reply that stopped at the token limit: the text so far."""


class ScriptedModel(Model):
    """Plays `script`, one entry per model call. An entry is the answer (a string), a reply that
    was cut off (a CutOff), the tools to ask for (a list of `(name, arguments)`), or an exception
    to raise; a callable entry is given the messages so far and returns one of those. When the
    script runs out, it answers `then`."""

    def __init__(self, script: list, then: str = "That is everything.") -> None:
        self.script = list(script)
        self.then = then
        self.offered: list[list[str]] = []  # per call: the tool names in the request
        self.system_prompts: list[str | None] = []
        self.requests: list[list] = []  # per call: the messages sent

    @property
    def calls(self) -> int:
        return len(self.offered)

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict:
        return {}

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):
        raise NotImplementedError
        yield  # pragma: no cover

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.offered.append([spec["name"] for spec in tool_specs or []])
        self.system_prompts.append(system_prompt)
        self.requests.append(json.loads(json.dumps(messages, default=str)))
        step = self.script.pop(0) if self.script else self.then
        if callable(step):
            step = step(messages)
        if isinstance(step, Exception):
            raise step

        yield {"messageStart": {"role": "assistant"}}
        if isinstance(step, str | CutOff):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": str(step)}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "max_tokens" if isinstance(step, CutOff) else "end_turn"}}
        else:
            for index, (name, arguments) in enumerate(step):
                tool_use = {"toolUseId": f"call-{self.calls}-{index}", "name": name}
                yield {"contentBlockStart": {"start": {"toolUse": tool_use}}}
                yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(arguments)}}}}
                yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {
            "metadata": {
                "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                "metrics": {"latencyMs": 1},
            }
        }


class FakeTool(AgentTool):
    """A tool named `name` whose result is `result(arguments)`: the `structuredContent`."""

    def __init__(self, name: str, result: Callable[[dict], dict] | dict) -> None:
        super().__init__()
        self._name = name
        self._result = result
        self.calls: list[dict] = []

    @property
    def tool_name(self) -> str:
        return self._name

    @property
    def tool_spec(self) -> dict:
        return {
            "name": self._name,
            "description": f"The {self._name} tool.",
            "inputSchema": {"json": {"type": "object", "properties": {"topic": {"type": "string"}}}},
        }

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):
        arguments = tool_use.get("input") or {}
        self.calls.append(arguments)
        structured = self._result(arguments) if callable(self._result) else self._result
        yield ToolResultEvent(
            {
                "toolUseId": tool_use["toolUseId"],
                "status": "success",
                "content": [{"text": json.dumps(structured)}],
                "structuredContent": structured,
            }
        )


def tool_results(messages: list) -> list[str]:
    """The text of every tool result the model has been sent so far."""
    return [
        part.get("text", "")
        for message in messages
        for block in message.get("content", [])
        if "toolResult" in block
        for part in block["toolResult"].get("content", [])
    ]
