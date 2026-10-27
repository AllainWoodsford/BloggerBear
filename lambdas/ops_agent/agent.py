"""The agent: a Strands `Agent` on Bedrock that answers the operator by calling the ops MCP server.

One question in, one short answer out, plus what the page shows next to it. The model is given a
goal and the server's tools and decides what to call (the design's section 2: it is not a script).
Everything that must be exact is code, in policy.py, and is put between the model and the tools
here:

- **which tools the model is given** is policy.offered: on a briefing the deep dives are not in
  the list passed to the `Agent`, so they are not in the request Bedrock receives;
- **every tool call passes policy.Ledger.admit first** (a `BeforeToolCallEvent` hook). A refused
  call never reaches the server: the model reads the refusal as that call's result;
- **the model's turns are capped** (Strands' own `limits`), so a model that never stops asking
  still ends, and the answer then comes from the tools' own `spoken` summaries;
- **findings and tables are copied from each tool's `structuredContent`** (an `AfterToolCallEvent`
  hook), never parsed out of what the model wrote. That includes the CLI guide's `how_to` cards
  (a command's help, or a command built by the server): a command the model writes in its answer
  is only words, and makes no card.

**The MCP calls are made by Strands' own client** (`strands.tools.mcp.MCPClient`), over
Streamable HTTP. It is given the server's URL and the caller's `Authorization` header, which it
sends on every request; with the pinned `mcp` release it speaks MCP 2026-07-28 to the server
(one `server/discover`, then standalone requests, no handshake and no session).
tests/test_ops_agent_mcp_wire.py holds both against the real server.

**Nothing is kept.** An `Agent`, its ledger and its MCP connection are made for one question and
dropped; the earlier turns arrive with the request (the browser tab holds the conversation).

Configuration, from the environment:

    OPS_AGENT_MODEL_ID  the Bedrock model or inference profile (an id or an ARN, as the pipeline's
                        BEDROCK_MODEL_ID is), called through Converse
    OPS_MCP_URL         the ops MCP server's endpoint, ending in /mcp
    AWS_REGION          set by Lambda: the region Bedrock is called in, as common/bedrock.py's
                        client is (the pipeline calls Bedrock in its own region)

This is the only module that imports `strands`.
"""

from __future__ import annotations

import os
from typing import Any

from botocore.config import Config
from strands import Agent
from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor
from strands.tools.mcp import MCPClient
from strands.types.exceptions import MaxTokensReachedException

from common import prompts, stats_tracking
from ops_agent import policy
from ops_mcp import redact

# The answer is spoken: about 120 words is some 200 tokens, and a turn that asks for two or three
# tools at once needs about as many. A reply cut off at this cap is not read out half-finished:
# the tools' own summaries are used (see _answer_text).
MAX_TOKENS = 600

# API Gateway gives up on a request after 29 seconds, so nothing here may wait long: one retry on
# a Bedrock error, and a read that does not hang. Two attempts in all: botocore reads
# `max_attempts` as the number of retries, so it is `total_max_attempts` that says two.
# One call's worst case is still more than 29 s: two attempts of 5 s connect and 20 s read, with up
# to 1 s of backoff between them, is 51 s. It is kept: the retry is for an error that comes back at
# once (a throttle, a 5xx), and the retry of a read that timed out is stopped by the function's own
# 29 s timeout (infra/modules/ops-assistant/agent.tf), the caller told it failed either way. Two
# whole attempts inside 29 s would need reads of 9 s, less than a MAX_TOKENS answer, not streamed,
# can take; and a question is several model calls and tool calls, which share the 29 s.
_BEDROCK_CONFIG = Config(
    connect_timeout=5,
    read_timeout=20,
    retries={"total_max_attempts": 2, "mode": "standard"},
    user_agent_extra="bloggerbear-ops-agent",
)
_MCP_STARTUP_TIMEOUT_SECONDS = 10

# The system prompt is ops_agent/system in common/prompts_registry.py: one rule per entry, each with
# the reason it is there. It is code-only (asset=False), and vanilla_prompt reads nothing at all, so
# the rules that keep this assistant read-only are never read from S3 and its role needs no access
# to the prompts bucket or hash table: the agent sends the registry's own text, every time.
SYSTEM_PROMPT_ID = "ops_agent/system"
SYSTEM_PROMPT = prompts.vanilla_prompt(SYSTEM_PROMPT_ID)

# Said when the model gave no usable answer and the tools had nothing to say either.
_NO_ANSWER = "I couldn't put an answer together. What I checked is on screen."


class AgentError(Exception):
    """The model or the MCP server failed. The handler answers 502 and says nothing more."""


def bearer_headers(authorization: str, extra: dict[str, str] | None = None) -> dict[str, str]:
    """The headers every MCP request carries: the caller's own `Authorization` value, as it
    arrived (the server's authorizer checks it again; this code never reads what is in it), and
    whatever the handler adds so the server can judge the request by the operator's address and
    not this function's (ops_mcp/access.py)."""
    return {**(extra or {}), "Authorization": authorization}


def mcp_client(url: str, authorization: str, extra_headers: dict[str, str] | None = None) -> MCPClient:
    """Strands' MCP client for the ops server, over Streamable HTTP, sending the caller's token.
    Used as a context manager: it connects on entry and disconnects on exit."""
    return MCPClient(
        url=url,
        headers=bearer_headers(authorization, extra_headers),
        startup_timeout=_MCP_STARTUP_TIMEOUT_SECONDS,
        application_name="bloggerbear-ops-agent",
    )


def list_tools(client: MCPClient) -> list:
    """Every tool the server lists, across pages. Nothing here names them: the server decides
    what exists, and policy.py decides which of them a turn is given."""
    tools: list = []
    token = None
    while True:
        page = client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = page.pagination_token
        if not token:
            return tools


def warm(authorization: str, extra_headers: dict[str, str] | None = None) -> int:
    """Wake the path a question takes, and do nothing else: open the MCP session with the caller's
    own token and list the tools. That starts the MCP server's Lambda if it was cold (this one is
    awake by being called). No model is called and no tool is run, so it costs no tokens and
    reads no data. Returns how many tools the server listed."""
    with mcp_client(os.environ["OPS_MCP_URL"], authorization, extra_headers) as client:
        return len(list_tools(client))


def dismiss(
    kind: str, finding_id: str, authorization: str, extra_headers: dict[str, str] | None = None
) -> dict:
    """The page's Dismiss button: call the MCP server's `dismiss` tool for one finding, with the
    caller's own token, and return what it answered. No model is involved: the operator pressed
    a button, so there is nothing to decide. The server checks the kind and the id itself and
    writes only the caller's own list (ops_mcp/memory.py)."""
    with mcp_client(os.environ["OPS_MCP_URL"], authorization, extra_headers) as client:
        result = client.call_tool_sync(
            tool_use_id="page-dismiss", name="dismiss", arguments={"kind": kind, "id": finding_id}
        )
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    if result.get("status") != "success" or not isinstance(structured, dict):
        raise AgentError("DismissFailed")
    return structured


def bedrock_model() -> BedrockModel:
    """The model, from the environment. Not streamed: the answer is sent whole, and Converse
    without streaming needs one IAM action (bedrock:InvokeModel) and not two."""
    return BedrockModel(
        model_id=os.environ["OPS_AGENT_MODEL_ID"],
        region_name=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION"),
        boto_client_config=_BEDROCK_CONFIG,
        max_tokens=MAX_TOKENS,
        streaming=False,
    )


def _messages(question: str, history: list[dict] | None) -> tuple[list[dict], list[dict]]:
    """The earlier turns as the model's messages, and the question as its prompt.

    Converse wants the roles to alternate and the first message to be the user's. The browser
    sends "the last few turns", which can start with an answer or hold two questions in a row
    (one whose answer failed), so neighbouring turns with the same role are put into one message
    and an answer with no question before it is dropped."""
    merged: list[dict] = []
    for turn in [*(history or []), {"role": "user", "text": question}]:
        if not merged and turn["role"] != "user":
            continue
        block = {"text": turn["text"]}
        if merged and merged[-1]["role"] == turn["role"]:
            merged[-1]["content"].append(block)
        else:
            merged.append({"role": turn["role"], "content": [block]})
    return merged[:-1], merged[-1]["content"]


def _hooks(ledger: policy.Ledger) -> list:
    """The two places the policy sits between the model and the tools."""

    def before(event: BeforeToolCallEvent) -> None:
        refusal = ledger.admit(event.tool_use.get("name", ""), event.tool_use.get("input"))
        if refusal:
            event.cancel_tool = refusal  # the tool is not run; the model reads this instead

    def after(event: AfterToolCallEvent) -> None:
        # A refused call has no tool behind it, and a failed one has nothing to collect.
        if event.selected_tool is None or event.result.get("status") != "success":
            return
        ledger.record(event.result.get("structuredContent"))

    return [before, after]


def _answer_text(result: Any, ledger: policy.Ledger) -> str:
    """What is spoken. The model's words when it finished an answer, unless they repeat text a
    tool marked as written outside this project (policy.echoes: `ledger.echoed` is then set);
    otherwise (it ran out of turns, was cut off, or echoed) the tools' own summaries, which code
    wrote and are safe to say. Either way it passes the PII sweep last (redact.sweep_answer): an
    e-mail, a whole address, a token or a key the model repeated from something it read is
    replaced before the operator hears it."""
    if result is not None and result.stop_reason == "end_turn":
        text = " ".join(
            block["text"].strip() for block in result.message.get("content", []) if block.get("text")
        ).strip()
        if text and policy.echoes(text, ledger):
            ledger.echoed = True
        elif text:
            return redact.sweep_answer(text)
    return redact.sweep_answer(" ".join(ledger.spoken) or _NO_ANSWER)


def run(question: str, history: list[dict] | None, tools: list, model: Any = None) -> dict:
    """Answer one question with these tools (Strands `AgentTool`s; in production, the MCP
    server's). Returns what the handler sends: `answer`, `tool_calls`, `findings`, `tables` and
    `turn`.

    The tools are filtered here, before the `Agent` exists, so on a briefing the model is never
    shown a deep dive."""
    turn = policy.turn_kind(history)
    by_name = {tool.tool_name: tool for tool in tools}
    ledger = policy.Ledger(turn, by_name)
    earlier, prompt = _messages(question, history)
    agent = Agent(
        model=model or bedrock_model(),
        messages=earlier,
        tools=[by_name[name] for name in policy.offered(by_name, turn)],
        system_prompt=SYSTEM_PROMPT,
        # The default handler prints the model's words as they arrive, which would put the answer
        # in the logs.
        callback_handler=None,
        hooks=_hooks(ledger),
        # One at a time: findings are then collected in the order the model asked for them, and
        # the budget is counted in that order too.
        tool_executor=SequentialToolExecutor(),
        # Bedrock's own client already retries once (_BEDROCK_CONFIG); Strands' default would
        # wait and retry for minutes, long after API Gateway has given up.
        retry_strategy=None,
    )
    try:
        result = agent(prompt, limits={"turns": policy.max_model_calls(turn)})
    except Exception as exc:  # noqa: BLE001 - whatever failed, the caller is told only "it failed"
        if not _cut_off(exc):
            raise AgentError(type(exc).__name__) from exc
        result = None
    finally:
        # What the run cost, whether it answered, was cut off or failed: tokens a failed run
        # spent were still billed. Recorded here, by code, from Strands' own count.
        _record_usage(agent)
    spoken = _answer_text(result, ledger)
    return {
        "answer": spoken,
        "tool_calls": ledger.tool_calls,
        "findings": ledger.findings,
        "tables": ledger.tables,
        "turn": turn,
        # For the handler's log line only (it is taken out before the answer is sent): the model's
        # answer repeated text a tool marked as written outside this project.
        "echo": ledger.echoed,
    }


def _record_usage(agent: Agent) -> None:
    """Tally this run's model calls, tokens and cost onto the week's Stats row (the "assistant"
    category, common/stats_tracking.py). Never raises: bookkeeping never costs the answer."""
    try:
        metrics = agent.event_loop_metrics
        usage = metrics.accumulated_usage or {}
        stats_tracking.record_assistant_run(
            os.environ.get("OPS_AGENT_MODEL_ID", ""),
            usage.get("inputTokens", 0),
            usage.get("outputTokens", 0),
            metrics.cycle_count,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"ops_agent: usage not recorded error={type(exc).__name__}")


def _cut_off(exc: Exception) -> bool:
    """True if the model's reply hit MAX_TOKENS: not a failure of the service, so the question is
    still answered, from the tools' summaries."""
    return isinstance(exc, MaxTokensReachedException) or isinstance(exc.__cause__, MaxTokensReachedException)


def answer(
    question: str,
    history: list[dict] | None,
    authorization: str,
    extra_headers: dict[str, str] | None = None,
) -> dict:
    """Answer one question against the ops MCP server, as the caller: connect with their token,
    list the server's tools, run the agent, disconnect."""
    try:
        with mcp_client(os.environ["OPS_MCP_URL"], authorization, extra_headers) as client:
            return run(question, history, list_tools(client))
    except AgentError:
        raise
    except Exception as exc:  # noqa: BLE001 - a server that is down, refuses the token, or times out
        raise AgentError(type(exc).__name__) from exc
