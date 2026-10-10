# Architecture by feature

The [README's architecture](../../README.md#architecture) describes BloggerBear by layer: the edge,
the APIs, compute, AI, data, and so on. A layer tells you what something is made of. It does not
tell you how one thing the project does actually works, because that thing crosses several layers.

The pages here describe the project by **feature** instead. A feature is a small, logical grouping
of the parts that together do one job, in the order the work flows through them. Each page has a
matching entry in the operator's assistant: ask it about the feature, and it walks through the same
steps with this environment's resource names on screen (`architecture` with `feature`, in
`lambdas/ops_mcp/architecture.py`).

| Feature | What it covers |
|---|---|
| [Article research](article-research.md) | How a topic's data source becomes a published article: adapters, third-party API keys, the research tick, findings, candidate ideas, Bedrock drafting, the reviews, and the article in S3. Also where the AI agents fit. |
| [Vision: satellite imagery and the Rail Access Monitor](blogger-vision.md) | How a topic that watches satellite imagery becomes an article with a map on it: Sentinel-2, OpenStreetMap and GDELT, the diff-first adapter, the stateless OpenCV worker in us-west-2, the bounded triage agent, the person who approves, and the figure on the page. Then the rail concepts in plain words: built-up density, walking reach, transit deserts, the station graph, hubs, the flags and the suggestions, and what they do not mean. No assistant entry yet. |

## Adding a feature

A future one might be "prompts as assets": where prompts live, how reader feedback refines them,
and how a refinement is approved and worn.

1. Write a page here in the same shape as the existing one: why it exists, a diagram, the steps,
   and where each step lives in the code.
2. Add a `Feature` to `FEATURES` in `lambdas/ops_mcp/architecture.py`, with the same steps. Name
   catalogue resources as `kind:key` (for example `table:findings`). Anything that is not one of
   our named resources, such as Bedrock or an adapter, goes in plain words.
   `tests/test_ops_mcp_architecture.py` fails if a `kind:key` is not in the catalogue.
3. Add its key to the `feature` `Literal` on the `architecture` tool in `lambdas/ops_mcp/server.py`,
   and to the agent's prompt (`lambdas/ops_agent/agent.py`) if the question that should reach it is
   not obvious.
4. Add a row to the table above.
