# Architecture by feature

The [README's architecture](../../README.md#architecture) describes BloggerBear by layer: the edge,
the APIs, compute, AI, data, and so on. A layer tells you what something is made of. It does not
tell you how one thing the project does actually works, because that thing crosses several layers.

The pages here describe the project by **feature** instead. A feature is a small, logical grouping
of the parts that together do one job, in the order the work flows through them. Each page has the
same shape: why the feature exists, a diagram, the steps, and where each step lives in the code.

Two of the pages have a matching entry in the operator's assistant: ask it about the feature, and
it walks through the same steps with this environment's resource names on screen (`architecture`
with `feature`, in `lambdas/ops_mcp/architecture.py`): `architecture(feature="article-research")`
and `architecture(feature="vision-pipeline")`. The other five say at the top which of the
assistant's named parts to ask about instead. Where vision is on, the assistant also reads the
vision worker's log in `vision_region` and each vision topic's running state
([ops-assistant-vision.md](../enhancements/archive/ops-assistant-vision.md)).

| Feature | What it covers |
|---|---|
| [Article research](article-research.md) | How a topic's data source becomes a published article: adapters, third-party API keys, the research tick, findings, candidate ideas, Bedrock drafting, the reviews, and the article in S3. Also where the AI agents fit. |
| [Vision: satellite imagery and the Rail Access Monitor](blogger-vision.md) | How a topic that watches satellite imagery becomes an article with a map on it: Sentinel-2, OpenStreetMap and GDELT, the diff-first adapter, the stateless OpenCV worker in us-west-2, the bounded triage agent, the person who approves, and the figure on the page. Then the rail concepts in plain words: built-up density, walking reach, transit deserts, the station graph, hubs, the flags and the suggestions, and what they do not mean. In the assistant: `architecture(feature="vision-pipeline")`. |
| [The operator's assistant: the ops agent and its MCP server](ops-mcp-agent.md) | How a question becomes a spoken answer with the fixes on screen: the ask page and its sign-in, the one API, the Strands agent on Bedrock, the read-only MCP server and its tools, what they read and what comes back. What it can and cannot do, how read-only is held in IAM, and what it costs. |
| [Alexa+: the assistant by voice](alexa-plus.md) | How the same assistant is reached through Alexa+: the add-on, OAuth discovery and account linking, every call's path, the briefing a voice cannot wait for, the firewall review in production, what is said aloud and what is not, and the limits. |
| [The admin CLI and the admin API](admin-cli.md) | How a decision the operator types becomes a change in the pipeline: the local script and its SigV4 signing, the allowlist and IAM in front of the API, the one Lambda behind it, what each group of commands writes (topics and their schedules, decisions on articles, models and settings), and how the CLI and the assistant divide the work. |
| [The site, the bear and reader feedback](site-and-feedback.md) | How a reader's visit and vote become the bear's mood, gear and musings: the public site and its API, a vote's path through the honeypot, the token, the comment screen and the limits, the weekly reflection that turns feedback into gear, wear and repair, the Musings page, and Professor Grizzlewick's reviews. |
| [Stats and costs](stats-and-costs.md) | How every model call and request becomes a number on the Stats page: lineage on each article, the tracked categories, the weekly rollover, the daily Cost Explorer poll, and what to do about a missed week. |

## Adding a feature

A future one might be "prompts as assets": where prompts live, how reader feedback refines them,
and how a refinement is approved and worn.

1. Write a page here in the same shape as the existing ones: why it exists, a diagram, the steps,
   and where each step lives in the code.
2. Add a `Feature` to `FEATURES` in `lambdas/ops_mcp/architecture.py`, with the same steps. Name
   catalogue resources as `kind:key` (for example `table:findings`). Anything that is not one of
   our named resources, such as Bedrock or an adapter, goes in plain words.
   `lambdas/tests/test_ops_mcp_architecture.py` fails if a `kind:key` is not in the catalogue.
3. Add its key to the `feature` `Literal` on the `architecture` tool in `lambdas/ops_mcp/server.py`
   (a test holds the `Literal` equal to `FEATURE_KEYS`), and to the agent's prompt
   (`lambdas/ops_agent/agent.py`) if the question that should reach it is not obvious.
   Choose its `said` and `aliases` with `resolve_feature` in mind: an exact name wins, and
   otherwise every alias found as whole words in the question is scored and the **longest** wins
   (a tie goes to the earlier feature). So "the vision research pipeline" goes to the vision
   pipeline, though it holds article research's "research pipeline". "the" is dropped from the
   question before matching, so no alias may hold it.
4. Add a row to the table above.

## Keeping the assistant's catalogue up to date

The assistant's `architecture` tool answers from `CATALOGUE` in `lambdas/ops_mcp/architecture.py`:
every named resource, its layer and what it is for. When a resource is added, renamed or removed
under `infra/`, `lambdas/tests/test_ops_mcp_architecture.py` fails and names what is missing or
stale. The fix is the catalogue entry, and, where the change gives the operator somewhere new to
look (a log group, a metric, a console page), a step in the matching runsheet in
`lambdas/ops_mcp/runsheets.py`, which `investigate` reads.
