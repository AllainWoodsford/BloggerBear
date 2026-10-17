# Enhancement: Alexa+ — a voice that drives the Strands assistant

**Status:** built (PRs [#195](https://github.com/AllainWoodsford/BloggerBear/pull/195),
[#198](https://github.com/AllainWoodsford/BloggerBear/pull/198),
[#200](https://github.com/AllainWoodsford/BloggerBear/pull/200)–[#203](https://github.com/AllainWoodsford/BloggerBear/pull/203),
[#206](https://github.com/AllainWoodsford/BloggerBear/pull/206)); the Alexa+ bootstrap waits on toolkit access ·
**Date:** 2026-10-04 · **Entry:**
[Amazon Build, Ship, Shape](https://amazonappdev2026.devpost.com), Alexa+ track. **Deadline:**
Friday 23 October 2026, 12:00 pm PDT (Saturday 24 October, 6:00 am Sydney).

**Builds on:** [alexa-plus-operator-assistant-enhancement.md](alexa-plus-operator-assistant-enhancement.md)
(the operator's briefing: the ops MCP server, the Strands agent, memory, `ask.html`). That
document is still the design of the assistant itself. This one decides **how Alexa+ fits on top
of it**, replaces the externally written `AlexaMCP.md` proposal (reviewed in section 2), and plans
the work as a series of PRs.

## 1. The decision, in one paragraph

**Alexa is the voice; the Strands agent is the brain; the ops MCP server is the only way either
of them touches the pipeline.** On the `ask.html` page, the voice button is the Alexa+
experience, simulated in the browser as the rules allow: you speak, the Strands agent works out
what needs your attention by orchestrating the MCP tools, and the answer is spoken back with a
suggestion card on screen. For a **real** Alexa+ device, the same MCP server is registered as an
Alexa+ add-on with OAuth account linking through the same Cognito pool. Alexa+ cannot wait for the
agent (the toolkit's latency limit is 500 ms; a briefing takes 10 to 25 seconds), so Alexa
orchestrates the agent **asynchronously**: one fast tool starts a Strands briefing in the
background, another reads the latest one back. Everything stays serverless (API Gateway, Lambda,
DynamoDB, Cognito, Bedrock); the only non-Terraform step is a one-time `alexa-ai` bootstrap, run
by a person, per environment. Dev's Alexa sees only dev and never the firewall; production's sees
only production, plus the firewall deep dive.

## 2. Review of `AlexaMCP.md`

The proposal had the right direction (MCP over Streamable HTTP, Cognito as the authorization
server, a pre-computed answer for latency, the existing card rules) and these problems. Each was
checked against the toolkit's public pages (overview, quickstart, account linking), the MCP
`2025-11-25` authorization spec, RFC 9728 and this repository.

| # | `AlexaMCP.md` says | What is true | What this plan does |
|---|---|---|---|
| 1 | Transition to native Alexa+ "bypasses the custom Agent Lambda" | Alexa+'s own model would then call the raw tools. The Strands agent (the orchestration, the budget, the deep-dive gate, memory-led briefings) is what makes the entry "creative" in the judges' words; dropping it trades the entry's best part for the "basic MCP wrapper" the rules call obvious | Keep the agent. Alexa starts and reads Strands briefings (section 4.3) and may also call the fast read tools directly |
| 2 | "Maintains MCP 2026-07-28" | The toolkit supports **2025-11-25** (the `initialize` handshake). The server already answers both (`mcp` 2.1.1, stateless); the agent keeps 2026-07-28 | A contract test for the exact sequence Alexa sends: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, each as a standalone POST |
| 3 | 401 "without a `WWW-Authenticate` header" | The opposite. The MCP authorization spec and RFC 9728 require `WWW-Authenticate: Bearer resource_metadata="…"` on a 401: it is how a client finds the metadata | API Gateway's `UNAUTHORIZED` gateway response carries the header |
| 4 | PRM and AS metadata at `/.well-known/…` | On an `execute-api` URL the first path segment is the **stage**: `https://<id>.execute-api…/.well-known/…` asks for a stage called `.well-known` and gets a 403. The spec's fallback is the `resource_metadata` URL in the 401 header, which may be any URL | Serve both documents under the stage (`/<stage>/.well-known/oauth-protected-resource`, `/<stage>/.well-known/oauth-authorization-server`) and point the 401 header at the first; a custom domain later can add the root paths |
| 5 | "OAuth metadata with explicit `code_challenge_methods_supported`" from Cognito | Cognito publishes OIDC discovery only, and it does **not** list `code_challenge_methods_supported`, though it enforces S256. A spec-following client refuses an authorization server that doesn't advertise S256 | Our own RFC 8414 document (static JSON from an API Gateway mock integration: no Lambda, no cost) naming Cognito's endpoints and `["S256"]` |
| 6 | "< 500 ms by using pre-aggregated background cron data" | Right idea, wrong mechanism: a cron has no signed-in user to call the tools as (memory is per user), and a cold Lambda running the MCP SDK under the Web Adapter takes 1–3 s whatever it reads | Briefings are produced by the agent **as the user**, on demand and after every web briefing (section 4.3); the Alexa-facing tools are one DynamoDB read; production can keep one instance warm (section 4.5) |
| 7 | `ui://` identifiers "within `structuredContent`" | MCP Apps puts a `ui://` resource URI in the tool's `_meta`, served by `resources/read`; it is not a field of the result | Out of scope for the hackathon (as before). The cards stay on `ask.html` |
| 8 | A full `addon.json` "in the original configuration" | The manifest isn't in the proposal, and its schema is in the partner documentation we cannot read. Hand-writing it is guessing | `alexa-ai` generates it during the bootstrap; we commit a template with the values Terraform outputs (section 6) |
| 9 | Runbook: `configure`, `deploy`, simulator | Missing `alexa-ai configure-account-linking` (the client id, secret, endpoints), without which `deploy` fails; and the toolkit is **US-only and partner-gated** today | The bootstrap includes it; the simulated web experience is the entry, the real add-on is the stretch (section 7) |
| 10 | The document itself | Its first half is pasted twice; "Operational Engineering Team" authorship | Superseded by this file |

### Repository-specific findings from the review

- **The voice button does not work reliably** (reported by the operator). The page logic is
  correct with a working speech engine (checked in headless Chromium with a fake one), so the
  failures are in how it uses the browser's: section 4.1.
- **A briefing runs against API Gateway's 29-second ceiling.** Eight tool calls plus up to ten
  model turns on Haiku is often 15–25 s, and a cold MCP Lambda adds 2–3 s. A 504 reads on the page
  as "could not answer", which is easy to take for the voice failing. The async briefing (4.3)
  removes the ceiling for the slow path.
- **`allowlist` and Alexa can't both work.** Alexa calls the MCP server from Amazon's addresses.
  Under `assistant_access = allowlist` every Alexa call is refused (correctly: it fails closed).
  Documented, and the add-on's bootstrap checks the setting first.
- **Production has no assistant at all yet** (`module.ops_assistant` is only in dev), so "Alexa in
  production" needs that first (PR 6).
- **Commands on a dev card don't say which environment they're for.** The CLI talks to whatever
  `BLOGGERBEAR_ADMIN_API_URL` points at, so a dev card's article id could be pasted into a shell
  pointed at production. The cards get an environment label (PR 2).

## 3. Options considered

| | A. Native add-on only (`AlexaMCP.md`) | B. Web simulation only | **C. Both doors, one brain (chosen)** |
|---|---|---|---|
| What answers | Alexa+'s model over the raw tools | Strands agent | Strands agent; Alexa+ starts it and reads its briefings |
| Works from Australia, today | No: US-only, partner-gated | Yes | Yes (web); real Alexa+ when access arrives |
| Judges can test it | Only with a US Alexa+ account and the add-on enabled | Yes, with the judges' login | Yes |
| "Creative, not obvious" | Weakest: a wrapper for someone else's agent | Strong | Strongest: two front ends, async orchestration, memory |
| 500 ms limit | Every tool must meet it, cold or warm | n/a | Only two tiny tools need to |
| Serverless, ongoing | Yes | Yes | Yes |
| Extra work | OAuth metadata, latency | Voice fix | Both, plus the async briefing |

## 4. Design

```
 Alexa+ device (US)                    ask.html (any browser)
   │ "ask BloggerBear what needs         │ voice button → speech recognition
   │  my attention"                      │ Cognito sign-in (PKCE), token in memory
   │ OAuth 2.1 account link (Cognito)    │
   v                                     v
 POST /<stage>/mcp ─────────────┐     POST /<stage>/ask ── agent Lambda (Strands, Bedrock)
   start_briefing ──async──────────────────────────────────>│  as the user, with their token
   latest_briefing (1 GetItem)  │                            │  writes the briefing when done
   + the fast read tools        │     ops MCP Lambda <───────┘  MCP client, every tool call
                                └──> (Cognito authorizer, scope bloggerbear-ops/read)
                                         │
       DynamoDB (app tables, read) · OperatorSuggestions (memory) · OpsBriefings (NEW)
       CloudWatch alarms · S3 article bodies · WAF logs (production only, firewall_review)
```

### 4.1 The voice on `ask.html` (PR 2)

The button is the Alexa+ experience in the web simulation. What goes wrong today, and the fix:

| Symptom | Cause | Fix |
|---|---|---|
| Nothing is heard on a phone when the button is held | A long press opens the text-selection menu, which sends `pointercancel`; the page treats that as "released" and stops listening at once | Tap to talk (tap to start, tap to stop, or stop on silence) is the default; holding still works on a desktop. `touch-action: none`, `user-select: none`, `-webkit-touch-callout: none` on the button |
| Recognition fails with a vague message | `network` (Chromium builds with no Google speech service: Brave, many Linux Chromium builds), `audio-capture` (no microphone), `language-not-supported` are all reported as "failed" | A message per error, saying what to do |
| Recognition is unreliable on Safari | `lang` is set to `en` from `<html lang>`; Safari wants a full tag | The browser's own `en-*` tag (`navigator.language`), else `en-US` |
| No feedback while listening | `interimResults` is off and nothing shows what was heard | Interim words shown live; the final words go in the box before sending |
| The answer is sometimes not spoken | `speechSynthesis.cancel()` then `speak()` in the same tick drops the utterance in Chrome; the utterance can be garbage-collected mid-sentence; Chrome's network voices stop at about 15 s; iOS refuses `speak()` that wasn't first called in a tap | Cancel only when something is speaking, and speak on the next tick; keep a reference; speak sentence by sentence; "unlock" speech with an empty utterance on the first tap |
| Hard to tell which part is broken | No diagnosis | A "Test voice" button: microphone permission, recognition, and speech, each reported |

The page itself never says "Alexa" (a test holds that, for the trademark rule); the submission's
text and video describe it as simulating the Alexa+ experience. The voice prefers an `en-AU`, then `en-US` voice. Everything else about the page
(the token in memory only, `textContent` only, commands never spoken) is unchanged.

### 4.2 OAuth for Alexa+ account linking (PR 3)

All Terraform, all in `infra/modules/ops-assistant/`, behind one variable: an empty
`alexa_redirect_uris` (the default) creates none of it, so nothing changes for an environment
that isn't linked.

- **A separate Cognito app client for Alexa** (`aws_cognito_user_pool_client.alexa`): code grant,
  PKCE, scopes `openid` and `bloggerbear-ops/read`, callback URLs from `alexa_redirect_uris` (given
  by `alexa-ai configure-account-linking`), with a secret (Alexa's account linking sends one; the
  variable `alexa_client_secret` can turn it off). Its own client means it can be revoked alone,
  and its refresh token lasts 30 days (the page's lasts one) so the link doesn't break daily.
- **Protected Resource Metadata** (RFC 9728) at `GET /<stage>/.well-known/oauth-protected-resource`:
  `resource` = the MCP URL, `authorization_servers` = [our own issuer URL],
  `scopes_supported` = [`bloggerbear-ops/read`], `bearer_methods_supported` = [`header`].
- **Authorization server metadata** (RFC 8414) at `GET /<stage>/.well-known/oauth-authorization-server`:
  Cognito's authorize, token and revoke endpoints on the hosted domain, `code_challenge_methods_supported`
  = [`S256`], `grant_types_supported` = [`authorization_code`, `refresh_token`],
  `response_types_supported` = [`code`].
- Both are **mock integrations** in API Gateway: static JSON, no Lambda, no IAM, no cost.
- **`WWW-Authenticate`** on the `UNAUTHORIZED` gateway response, pointing at the PRM URL. API
  Gateway's Cognito authorizer answers a missing or bad token with that response, so every 401 the
  MCP route gives carries it.
- **Unknown, to settle at bootstrap:** whether Cognito ignores or rejects the `resource`
  parameter (RFC 8707) the toolkit sends. If it rejects it, the fix is a 20-line authorize
  redirector Lambda that drops it; nothing else changes.
- **What isn't built:** dynamic client registration (the toolkit uses static registration), and
  token audience binding beyond Cognito's scope and client checks.

### 4.3 The async briefing: Alexa orchestrates the Strands agent (PR 4)

- **A new table**, `bloggerbear-<env>-ops-briefings`: hash key `user_id` (the Cognito subject), one
  item per user: `status` (`running`, `ready`, `failed`), `started_at`, `finished_at`, `answer`
  (the agent's spoken words), `findings` (as the tools returned them), `tool_calls`, and a 2-day
  TTL.
- **Why a new table and not the memory table:** the memory table's rule is "kinds, ids, booleans
  and timestamps, never text", so hostile text can't persist into the agent's future context. A
  briefing *is* text. It goes to a table **the agent never reads** and the MCP server only hands to
  Alexa, which reads it aloud. It is overwritten by the next briefing and expires in two days.
- **Two new MCP tools**, the only ones Alexa needs:
  - `start_briefing()` (not read-only, not destructive): one conditional `UpdateItem` (refused if a
    briefing for this user started under 2 minutes ago and hasn't finished: the rate limit), then
    `lambda:Invoke` of the agent with `InvocationType=Event`, passing the caller's bearer token and
    their subject. Returns at once: "I've asked the assistant to look. Ask me for the briefing in
    about a minute."
  - `latest_briefing()` (read-only): one `GetItem`. Returns the briefing (its `spoken` is the
    answer, its `findings` the cards) with its age, or "still working", or "none yet".
- **The agent handler gains a second entry point**: an event with `source = "ops-briefing"`
  (it can only come from a direct Lambda invoke, which only the MCP role is allowed). It applies the
  `assistant_access` switch with no address (so `off` and `allowlist` refuse, `open` admits), runs
  the same briefing as `POST /ask`, with the passed token, and writes the result. Its timeout for
  this path is the Lambda's (raised to 90 s), not API Gateway's 29 s.
- **Every web briefing is written too**, so after a briefing on the page, "Alexa, what did the
  assistant find?" is instant.
- **The token in the async event:** a one-hour bearer token sits in Lambda's async queue
  (encrypted at rest by AWS) for the seconds before the run starts. Never logged. The alternative,
  a machine identity with its own Cognito client, would act as no user, so memory (whose
  suggestions are per user) would not work, and it would be a standing credential.
- **IAM:** the MCP role gains `lambda:InvokeFunction` on the agent's ARN only, and read/write on
  the briefings table only. The agent role gains `PutItem`/`UpdateItem` on the briefings table
  only. Both stay under the cross-environment Deny (isolation.tf).

### 4.4 Each environment's Alexa (PR 5, PR 6)

The operator's rule: **the dev Alexa must not be able to plan or reason about production
resources or the firewall; production's must be limited to production and the firewall.**

| | Dev's Alexa (and web assistant) | Production's |
|---|---|---|
| Cognito pool, app clients, Alexa add-on | dev's own; the add-on's endpoint is dev's MCP URL | production's own, MFA required |
| A token from the other environment | refused by API Gateway (another pool) | refused |
| Tables, bucket, logs, alarms, briefings | dev's (allow by name, deny by tag, filter in code) | production's |
| `firewall_review` | **not registered**, no IAM for any WAF log group or metric, and not named in its prompt | registered; reads production's regional WAF logs and the shared CloudFront WAF's |
| The AWS bill | not reported | reported (`account_wide_data`) |

- **`firewall_review` (PR 5)** is gated three ways, so one mistake isn't enough: the server
  registers it only when `OPS_ACCOUNT_WIDE_DATA` is true **and** `OPS_WAF_LOG_GROUPS` is set; the
  role has `logs:StartQuery` and `logs:GetQueryResults` only on the log groups named in
  `waf_log_group_arns` (empty in dev, so no statement at all); and the tool refuses any log group
  that isn't `aws-waf-logs-bloggerbear-<this env>-*` or the shared group. Fixed Logs Insights
  queries (blocks per rule, per action, top blocked paths with query strings cut off), counts and
  baselines only, no addresses or raw lines in its result. It stays a deep dive: never offered on a
  briefing turn.
- **Nothing in the agent's prompt names the other environment or the firewall**; the deep-dive
  instruction is only added when the tool exists (it is listed by the server, so dev's agent never
  hears of it).
- **The cards say which environment** a command is for (`ENVIRONMENT_NAME`), so a dev id is not
  pasted into a production shell.
- **Production (PR 6)**: `module.ops_assistant` in `infra/environments/production` with MFA
  `ON`, `account_wide_data = true`, the three WAF log groups, the briefings table, the page's
  settings in production's `config.js`, the microphone allowed on production's site, and Cognito's
  domain in its `connect-src`. It ships through the normal release (a `v*` tag).

### 4.5 Latency for Alexa (PR 4)

- `latest_briefing` and `start_briefing` do one DynamoDB call each (plus one async invoke): tens of
  milliseconds warm.
- **Cold starts are the problem, not the tools.** Opt-in `keep_warm` (production, and only once
  the add-on is linked): an EventBridge Scheduler rule invokes the MCP function every 5 minutes.
  The Web Adapter passes a non-HTTP event to `/events`, which the app answers with a 204 and
  nothing else. About 8,600 invocations a month: inside the free tier. Provisioned concurrency
  (about US$6 a month per instance at 512 MB) is the fallback if one warm instance is not enough.

### 4.6 The add-on package and the one-time bootstrap (PR 7)

`alexa/` holds an `addon-package/` template and a runbook. Ongoing, nothing runs but the existing
serverless stack; the bootstrap is once per environment, by a person, on their own machine:

1. `terraform output` for the environment: `ops_mcp_url`, `ops_alexa_prm_url`,
   `ops_hosted_ui_domain`, and (after step 3) the Alexa client's id and secret.
2. `npm i -g` the Alexa AI CLI; `alexa-ai configure` (the Amazon developer account; US marketplace).
3. `alexa-ai configure-account-linking`: authorization URL
   `https://<hosted domain>/oauth2/authorize`, token URL `https://<hosted domain>/oauth2/token`,
   client id and secret from Terraform, scope `bloggerbear-ops/read`. It prints Alexa's redirect
   URLs: put them in `ops_alexa_redirect_uris` in the environment's tfvars and apply.
4. Check `assistant_access` is `open` (`pipeline-config show`).
5. `alexa-ai deploy`, then test in the simulator: link the account (sign in with the environment's
   Cognito user; MFA in production), "ask BloggerBear to start a briefing", then "ask BloggerBear
   for the briefing".
6. Dev and production are **two add-ons** (two add-on ids), each pointing at its own environment.

### 4.7 What it costs, recorded and bounded

- **Every agent run is tallied** onto the week's Stats row under the `assistant` category
  (`common/stats_tracking.py`, `record_assistant_run`): its model calls, tokens and cost, from
  Strands' own count, whether the run answered, was cut off or failed. So it is on the public
  Stats page ("Operator assistant"), in the `spend` tool's AI spend, and per environment, the same
  week it is spent. The agent's role may `UpdateItem` the Stats table and nothing else of it
  (`costs.tf`); it prices from the built-in table, since it reads no Models table.
- **Each user gets 100 questions a UTC day** (`agent_daily_question_cap`; `ops_agent/quota.py`),
  briefings Alexa+ starts included, counted before the model is called. Past it the page gets a
  429 with plain words; a count that cannot be kept refuses (503), it never becomes a free question.
- **Everything else the assistant uses** (Lambda, API Gateway, DynamoDB, Cognito, Logs Insights,
  the keep-warm schedule) is on the account's bill, which the daily Cost Explorer poll already
  reads in full and groups as Infrastructure on the Stats page. That is account-wide (dev and
  production together) and a day behind; splitting it by environment needs cost-allocation tags,
  activated by hand in the Billing console.

## 5. Security notes

- **No new way to change the pipeline.** `start_briefing` changes only the briefings table and
  costs a Bedrock run (rate-limited per user, and by the agent's own budget). Nothing else Alexa can
  call writes anything but the assistant's own memory.
- **Linking an Alexa account is a sign-in.** It goes through Cognito's hosted page, with MFA in
  production. Unlinking (in the Alexa app) or `admin-user-global-sign-out` on the Cognito user
  revokes it; disabling the Alexa app client cuts off every linked device at once.
- **What Alexa says aloud** is the agent's answer, written under the existing rules (no ids, no
  commands, nothing under `untrusted`). A device in a room is heard by whoever is there: the
  production add-on should be enabled only on the operator's own device.
- **Text in the briefings table** came from a model that read hostile data. It is never given back
  to our agent; Alexa+'s model reads it as tool output, which the server's instructions already
  call data, not instructions.
- **The well-known documents are public**, like any OAuth metadata: endpoints and a scope name,
  nothing secret.

## 6. Plan of PRs

Each PR is small enough to review alone, has its own tests, and leaves dev deployable.

| PR | What | Depends on |
|---|---|---|
| 1 | This document, and friction entries: #195 | — |
| 2 | **Voice fix** on `ask.html` (4.1), the voice self-test, environment label on cards: #198 | — |
| 3 | **OAuth metadata for Alexa+** (4.2): Alexa app client, PRM and AS metadata as mock integrations, `WWW-Authenticate` on 401, outputs; Terraform tests; the 2025-11-25 `initialize` contract test: #200 | — |
| 4 | **Async briefing** (4.3, 4.5): the briefings table, `start_briefing` and `latest_briefing`, the agent's second entry point, the `/events` answer, `keep_warm`; IAM; tests: #202 | 3 (shares the module) |
| 5 | **`firewall_review`** (4.4), production only by three gates; tests that dev can't register or read it: #203 | 4 |
| 6 | **Production assistant** (4.4): the module in production, its page settings and headers: #206 | 5 |
| 7 | **Add-on package and bootstrap** (4.6): `alexa/`, the runbook, a script that prints the values from `terraform output`: #201 | 3 |
| 8 | **Docs**: README, PROGRESS, the runsheet, the operator-assistant doc's status, friction: this one | all |
| 9+ | Fixes: #205 (`TRENDING_URL`, not this feature's but it broke `dev` mid-series) | — |

### As built, where it differs from the above

- **The two briefing tools are listed only where the function has the table and the agent to
  start** (`briefings.configured`), and are never offered to the agent itself
  (`policy.CLIENT_ONLY_TOOLS`): a briefing that started a briefing would never stop spending.
- **A first question is recorded as a briefing only if it called a briefing tool** and returned
  no table: a "how do I" about the Admin CLI is not the latest briefing.
- **`keep_warm`'s role is named `…-ops-mcp-scheduler-invoke`**, not `…-keep-warm`: the deploy
  role may manage only roles that fit its name patterns (friction 10.23).
- **Production turns `keep_warm` on by itself** once `ops_alexa_redirect_uris` is set; dev never
  does.
- **The agent's timeout stays 29 seconds** on the async path too. A briefing that would need more
  is reported as "didn't finish" two minutes after it started, as on the page.
- **The firewall review waits up to 15 seconds** for Logs Insights and reports what finished,
  saying so when something didn't; with no baseline it claims no spike.
| 9+ | Fixes from reviewing 1–8 | — |

## 7. Risks and unknowns

- **Alexa+ developer access from Australia** (toolkit US-only, partner-gated): the web simulation
  is the entry either way; the add-on is shipped ready to link.
- **Cognito and the `resource` parameter** (4.2): settled at bootstrap; small fallback.
- **The toolkit's exact manifest schema**: generated by the CLI, not guessed.
- **One account for both environments**: as before, separation is policy and code, not an account
  wall (operator-assistant doc, section 6).

## Sources

- [Alexa+ MCP Toolkit overview](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-overview.html),
  [quickstart](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-quickstart.html),
  [account linking](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-account-linking.html),
  [local inspector](https://developer.amazon.com/docs/alexaplus/add-ons/mcp-toolkit-local-inspector.html)
  (read through search summaries: the pages were not reachable from the build environment).
- [MCP authorization, 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization),
  RFC 9728 (Protected Resource Metadata), RFC 8414 (AS metadata), RFC 7636 (PKCE), RFC 8707
  (resource indicators).
