# Enhancement (parked): "Ask BloggerBear" — the reader's side of the assistant

**Status:** **parked** (2026-10-04) — not planned, and not part of the hackathon entry. Kept for
reference in case a reader-facing assistant is wanted later.
**The entry is:** [alexa-plus-operator-assistant-enhancement.md](alexa-plus-operator-assistant-enhancement.md)
— the operator's briefing. It holds the rules check, the prizes, the submission checklist and the
plan. Read this document as a sketch of what a public version would add to it, written before the
operator design settled; check it against that document before building anything from it.

## The idea

Let anyone ask BloggerBear what it has been writing about, by voice: "What's trending today?",
"What did BloggerBear say about crypto this week?", "Read me the latest Hacker News piece", "How much
has BloggerBear spent on AI this month?".

It reuses the primary entry's page and agent. What it adds is a set of public, read-only tools over
the existing public API, and a mode of `ask.html` that needs no sign-in.

## Why it's parked, not the entry

This was the first proposal. The rules' judging criteria name it as the obvious idea for the track:
"single-turn Q&A bot, basic MCP wrapper around an existing API." By itself it would score poorly on
Quality of the Idea, one of four equally weighted criteria.

As an addition to the operator's briefing it still earns its place:

- **Potential Impact** asks whether the project "could realistically serve an audience beyond the
  hackathon". This gives the same assistant a second audience: readers, not only the operator.
- **A judge can try it without signing in**, before reaching for the judges' login.
- **It's cheap once the primary exists:** thin tools over routes that already exist, the same agent,
  the same page. About two days.

If it isn't built, nothing in the primary entry changes.

## Today

- The **public API** (`lambdas/public_api_handler.py`) is unauthenticated, WAF rate-limited,
  throttled at API Gateway and cached at CloudFront. Its read routes: `GET /topics`,
  `/topics/{topic_id}/activity`, `/articles`, `/articles/{article_id}`, `/musings`, `/stats`,
  `/equipment`, `/rss.xml`. Everything these tools need already exists; **no change to the
  pipeline**.
- **Financial topics** already carry a disclaimer and are always held for manual moderation.
- **Cost:** a public demo's Bedrock spend will appear on the Stats page's whole-bill figures. That's
  accurate, since it *is* BloggerBear spend.

## Proposal

```
 ask.html, reader mode (no sign-in)
        │ POST /ask-public {question, the last few turns}
        v
 the same agent code, a second Lambda with the public tools only
        │ MCP client (Streamable HTTP)
        v
 public MCP endpoint (NEW, read-only, no auth) — Lambda behind API Gateway + WAF
        │ HTTPS
        v
 existing public API (unchanged)
```

### 1. The public tools

A second, unauthenticated MCP endpoint, built from the same code as the ops server but with its own
Lambda, its own route and **its own IAM role with no access to any table or log group**: it only
calls the public API over HTTPS. The ops tools aren't registered on it, so the public side can't
reach them even by mistake.

Start with four tools; the last two are extras.

| Tool | Wraps | Says |
|---|---|---|
| `latest_articles(topic?, limit=3)` | `GET /articles` | titles, one-line summaries, dates |
| `get_article(article_id)` | `GET /articles/{id}` | a ~60-second spoken summary, and the link |
| `trending_today()` | the Trending Everywhere digest | the cross-topic digest |
| `spend(period)` | `GET /stats` | AI and AWS spend, by week or month |
| `list_topics()` | `GET /topics` | the topics BloggerBear writes about |
| `whats_new(topic)` | `GET /topics/{id}/activity` | what the research tick saw last, and when |

- **Voice-friendly by construction:** markdown stripped, numbers written to be spoken, answers
  capped (~120 words). A financial topic's answers always end with its disclaimer, in the tool
  output itself, so no client can drop it.
- **No write tools, no admin routes, no feedback submission, no memory.** Nothing reachable here can
  change state, and nothing about the visitor is kept.

### 2. Reader mode on the page

- `ask.html` opens in reader mode; "Operator" is the sign-in. Same push-to-talk, text box fallback,
  tool-call display and spoken reply.
- **A separate agent Lambda** for the public side, with the public tools only, at most 3 tool calls
  a question. A visitor's question never runs with a role, or a tool list, that includes the ops
  tools.
- **Nothing stored:** questions and answers are not persisted, which keeps the no-PII constraint.
  Logs record counts and tool names, never the question text.
- **Cost and abuse controls**, because a public endpoint calls Bedrock:
  - a WAF rate limit per IP, plus a daily cap on questions overall (a counter, like the feedback
    limits);
  - reserved concurrency on this Lambda, separate from the operator's, so public traffic can take
    neither the operator's capacity nor the Bedrock quota the daily authoring cycle needs;
  - `max_tokens` capped; the existing Bedrock budget alarm; tool output treated as data (the tools
    are read-only, so the worst case is a wrong answer, not an action).

## Plan

Two days of work. The primary plan now puts production on day 14 and the log and firewall reviews on
day 15, so before the deadline there is room for this only if those finish early or the buffer day
is free. Otherwise it follows the hackathon:

1. The four tools, spoken formatting and the financial disclaimer; unit tests over a fake public
   API; the public endpoint, with WAF and the role that reaches nothing.
2. Reader mode on the page, the second agent Lambda and its limits; a line in the video and in the
   testing instructions.

If only one day is left: build the tools and the endpoint, and show them in the MCP Inspector. Skip
reader mode.

## Tests to write

- Each tool over a fake public API: shape, caps, markdown stripped.
- A financial topic's answer always ends with the disclaimer.
- The public endpoint lists the public tools only (wiring test), and its role has no DynamoDB, logs
  or CloudWatch actions.
- The daily cap stops further questions and says so plainly.

## Open questions

- Is a daily cap across all visitors enough, or is a per-IP cap needed as well as the WAF limit?
- Deployed to dev only at first, or to production too? The operator's side goes to production as a
  pilot; a public endpoint that calls Bedrock deserves its own decision.

(Decided, 2026-10-04: BloggerBear itself goes open source and is the Open Source mini challenge's
entry, so these tools stay in this repo; there is no separate `bloggerbear-mcp` repo.)
