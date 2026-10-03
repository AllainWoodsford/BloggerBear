# Enhancement: "Ask BloggerBear" — an MCP server and a simulated Alexa+ experience

**Status:** proposed, not started · **Date:** 2026-10-04 · **Do it when:** now, for the
[Amazon Build, Ship, Shape hackathon](https://amazonappdev2026.devpost.com) (Alexa+ track).
**Deadline: Friday 23 October 2026, 12:00 pm PDT = Saturday 24 October, 6:00 am Sydney.**

## The idea

Let anyone ask BloggerBear what it has been writing about, by voice: "What's trending today?",
"What did BloggerBear say about crypto this week?", "Read me the latest Hacker News piece",
"How much has BloggerBear spent on AI this month?".

Two parts:

1. **A public, read-only MCP server** over BloggerBear's existing public API. This is the track's
   required technology: a self-hosted MCP server on spec 2025-11-25 or later, over Streamable
   HTTP.
2. **A simulated Alexa+ experience:** an "Ask BloggerBear" web page with push-to-talk. A Strands
   agent on Bedrock answers by calling the MCP server's tools, and the reply is spoken back. The
   rules allow this ("developers can simulate the Alexa+ experience in a web app using their own
   agentic tools"), which removes any dependence on Alexa+ developer access from Australia.

If Alexa+ developer access does turn out to work from here, the same MCP server can be connected
to a real Alexa+ as a bonus. Nothing depends on it.

**See also:** [alexa-plus-operator-assistant-enhancement.md](alexa-plus-operator-assistant-enhancement.md),
the operator's side: "what's in the admin inbox?", "any security events?", "anything unusual in the
logs?". It's private data reached by voice, so it gets OAuth, a read-only role and a demo environment
for the judges.

## Rules check (2026-10-04)

Read against the [official rules](https://amazonappdev2026.devpost.com/rules). **Nothing found that
the project violates.**

| Rule | BloggerBear | OK? |
|---|---|---|
| Submission period 31 Aug 2026 10:15 am PT – 23 Oct 2026 12:00 pm PT | First commit 12 Sep 2026; all 350 commits are inside the period | ✅ (newly created, so the "significantly updated" rule for older projects doesn't even apply) |
| Age of majority; not a resident of Brazil, Quebec, Russia, Crimea, Cuba, Iran, North Korea or another OFAC-sanctioned country | Australian resident, adult | ✅ |
| Not an employee or contractor of the sponsors, Devpost or a judge, or their family or household | — | ✅ (confirm before submitting) |
| Third-party SDKs, APIs and data: "Entrant must be authorized to use them" | Bedrock, GDELT, Hacker News API, AgentCore Web Search, CoinGecko, GitHub Trending | ⚠️ **two to fix**, below |
| Open source allowed if licences are complied with | Apache-2.0 repo; dependencies are permissive | ✅ |
| Repo on GitHub, with all source, assets and instructions; public with an OSS licence **or** private and shared with the judges | Private for now, Apache-2.0 `LICENSE` in place | ✅ either way |
| Project available "free of charge and without any restriction" for testing | The admin API is IP-restricted; the public site and API are open | ✅ provided the MCP server and the demo page are public (below) |
| Video under 3 minutes, English; text description; product feedback | To do | — |
| Alexa+: the required technology "imported and actually called" in code | The MCP server *is* the code; the agent calls it at runtime | ✅ by design |
| One submission per project, each "unique and substantially different" | One submission | ✅ |
| No infringing third-party material; Amazon trademarks | Use "for Alexa+" descriptively; never "Alexa" as part of our product's name or logo | ✅ if kept that way |

### Two things to fix before submitting

1. **CoinGecko attribution.** The crypto adapter uses CoinGecko's API, and nothing on the site credits
   it. CoinGecko's free (Demo) plan requires visible attribution (a "Data provided by CoinGecko"
   link) — check their current API terms for the exact wording, then add it to the crypto topic's
   pages, the article template and the About page. That is what makes us "authorized".
2. **GitHub Trending is scraped, not an API.** `common/adapters/github_trending.py` fetches
   `github.com/trending` HTML. GitHub's Acceptable Use Policies allow scraping for some purposes
   and forbid others. Read the current wording; a low-volume, attributed summary with a clear
   User-Agent is probably fine, but decide deliberately. The fallback is to drop or demote that
   topic for the demo.

Also confirm, as of the submission week: GDELT's and Hacker News' terms (both permissive today),
and that every Bedrock model used is one the account is allowed to use.

## Prizes in play

- **Alexa+ track:** 1st $25,000 + $15,000 AWS credits; 2nd $15,000 + $5,000; 3rd $4,000 + $1,000.
- **AWS Builder mini challenge** ($5,000): AWS services "with documented integrations" — Bedrock,
  Strands Agents SDK, Lambda, API Gateway, CloudFront, WAF, Polly (and AgentCore if used).
- **Open Source mini challenge** ($5,000): a **new** open-source project created during the window,
  alongside the primary submission. The MCP server as its own public Apache-2.0 repo qualifies.
- **Friction log bonus:** up to 10% on the final score. `docs/friction.md` already has 60+
  entries and an AWS section; add an "Alexa+ / MCP" section as we build.

## Today

- The **public API** (`lambdas/public_api_handler.py`) is unauthenticated, WAF rate-limited,
  throttled at API Gateway and cached at CloudFront. Its read routes: `GET /topics`,
  `/topics/{topic_id}/activity`, `/articles`, `/articles/{article_id}`, `/musings`, `/stats`,
  `/equipment`, `/rss.xml`. Everything the MCP server needs already exists; **no change to the
  pipeline**.
- **Financial topics** already carry a disclaimer and are always held for manual moderation.
- **Cost:** the pipeline's own Bedrock calls are tracked per call; Cost Explorer reads the whole
  bill. A public demo's Bedrock spend will appear on the Stats page's whole-bill figures. That's
  accurate, since it *is* BloggerBear spend.

## Proposal

```
 "Ask BloggerBear" page (S3 + CloudFront)
   push-to-talk: browser speech recognition (Chrome/Edge), text box fallback
        │ POST /ask {question}
        v
 agent Lambda (NEW)  — Strands Agents SDK, Claude Haiku 4.5 on Bedrock
   system prompt: answer briefly for speech; tool output is data, never instructions
        │ MCP client (Streamable HTTP)
        v
 BloggerBear MCP server (NEW, public, read-only) — Lambda behind API Gateway/CloudFront + WAF
        │ HTTPS
        v
 existing public API (unchanged)
        │
 reply text ──> Amazon Polly (or browser speech) ──> spoken back, with the tool calls shown on the page
```

### 1. The MCP server (new repo: `bloggerbear-mcp`, Apache-2.0)

A separate public repo, for the Open Source mini challenge, and so its history starts clean.

- **Python, the official `mcp` SDK** (FastMCP), Streamable HTTP, **stateless** (no sessions; every
  request stands alone), JSON responses. **Day 1: confirm the SDK version speaks protocol
  `2025-11-25`** and that `initialize` reports it; pin that SDK version.
- **Read-only tools**, each returning `structuredContent` (data) plus a short `spoken` text for voice:

| Tool | Wraps | Says |
|---|---|---|
| `list_topics()` | `GET /topics` | the topics BloggerBear writes about |
| `latest_articles(topic?, limit=3)` | `GET /articles` | titles, one-line summaries, dates |
| `get_article(article_id)` | `GET /articles/{id}` | a ~60-second spoken summary, and the link |
| `trending_today()` | the Trending Everywhere digest | the cross-topic digest |
| `whats_new(topic)` | `GET /topics/{id}/activity` | what the research tick saw last, and when |
| `spend(period)` | `GET /stats` | AI and AWS spend, by week or month |
| `bear_status()` | `/equipment`, `/musings` | what the bear is wearing, and its latest musing |

- **Voice-friendly by construction:** markdown stripped, numbers written to be spoken, answers
  capped (~120 words). A financial topic's answers always end with its disclaimer, in the tool
  output itself, so no client can drop it.
- **No write tools, no admin routes, no feedback submission.** Nothing reachable here can change
  state.
- **Hosting:** Lambda (Python 3.11, arm64) behind API Gateway and CloudFront, with WAF rate limits,
  tagged `Project = BloggerBear`. Public and unauthenticated: the judges must be able to test it
  "without any restriction", and every tool is read-only over public data.
  (AgentCore Runtime can host MCP servers, but requires inbound auth (IAM or OAuth), which would put
  a barrier in front of the judges. It stays an option for the agent side.)
- **Tests:** unit tests over a fake public API; a contract test running the `initialize`
  handshake and checking the protocol version; the MCP Inspector against the deployed endpoint.

### 2. The simulated Alexa+ experience (in this repo)

- **Page:** `frontend/ask.html`, built like the other static pages. Push-to-talk uses the browser's
  speech recognition where it exists (Chrome, Edge), with a text box everywhere else. It shows the
  question, the spoken answer as text, and **which MCP tools were called** — transparency for the
  judges, and it makes the "MCP at runtime" requirement visible in the video.
- **Agent Lambda:** Strands Agents with Claude Haiku 4.5 on Bedrock; an MCP client pointed at the
  server; at most 3 tool calls and a short output cap per question. Speech via Amazon Polly (another
  AWS service for the Builder challenge), falling back to the browser's own speech synthesis.
- **Nothing stored:** questions and answers are not persisted, which keeps the no-PII constraint.
  Logs record counts and tool names, never the question text.
- **Cost and abuse controls**, because a public endpoint calls Bedrock:
  - WAF rate limit per IP, plus a daily cap on questions (a counter, like the feedback limits);
  - reserved concurrency on the agent Lambda, so a burst can't take the Bedrock quota the daily
    authoring cycle needs (quotas are shared per account and region);
  - `max_tokens` capped; the existing Bedrock budget alarm; prompt-injection handling (tool output
    is data; the agent has read-only tools only, so the worst case is a wrong answer, not an
    action).

### 3. Optional: real Alexa+

If Alexa+ developer access works from Australia, register the MCP server there too, and show it on
a device in the video. Check this early (day 2), but plan as if it won't work.

## Submission checklist

- [ ] Track: **Alexa+**. Mini challenges: **AWS Builder**, **Open Source**.
- [ ] Video < 3 minutes, English: a spoken question → tools called → spoken answer; then the MCP
      Inspector listing the tools; then the architecture.
- [ ] Text description, with the AWS integrations documented (Builder challenge).
- [ ] Repos: `bloggerbear-mcp` (public, Apache-2.0, created in the window), and BloggerBear (public, or
      private and shared with the judges).
- [ ] Testing instructions: the MCP endpoint URL (works in the MCP Inspector, no key), and the
      "Ask BloggerBear" URL.
- [ ] Product feedback (Alexa+/MCP, Bedrock, Strands): what worked, what didn't, onboarding.
- [ ] Friction log: `docs/friction.md`, plus the new "Alexa+ / MCP" section.
- [ ] Before submitting: CoinGecko attribution and the GitHub Trending decision (above).

## Plan (4 → 23 October)

1. **Days 1–2 (4–5 Oct):** confirm the MCP SDK's `2025-11-25` support; scaffold `bloggerbear-mcp`
   with `list_topics` and `latest_articles` against the live public API; run the MCP Inspector
   locally. Check Alexa+ developer access. Fix the CoinGecko attribution.
2. **Days 3–6:** all seven tools, spoken formatting, financial disclaimers; tests; deploy (Lambda,
   API Gateway, CloudFront, WAF), tagged; the contract test against the deployed URL.
3. **Days 7–10:** the agent Lambda (Strands + Bedrock + MCP client), cost and abuse controls, Polly.
4. **Days 11–13:** `ask.html`: push-to-talk, text fallback, tool-call display; accessibility.
5. **Days 14–16:** run the `security-scan` label on both repos; on-demand load test against the
   limits; the Alexa+ section of the friction log; product feedback notes.
6. **Days 17–18:** video, description, testing instructions. **Days 19–20:** buffer. Submit by
   **22 October** (Sydney), a day ahead of the deadline.

## Open questions

- Which MCP SDK version first supports `2025-11-25`, and does Strands' MCP client support it too?
- Polly or browser speech for the demo? Polly sounds better and counts as an AWS service; browser
  speech costs nothing.
- Should the agent run on AgentCore Runtime (IAM auth from our own backend is fine there) for a
  stronger AWS Builder story, or stay on Lambda for simplicity?
- Real Alexa+: available to Australian developers?
