# The operator's assistant reads the logs

## Why

The assistant could tell the operator *where* to look in the logs ([the architecture
expert](ops-assistant-architecture-expert.md)), but not what was there. The operator wanted to ask:

> Are there any errors in the logs? Check for API failures or Lambda failures, and find the root
> cause.

> I'm investigating 400s on the API between 1 and 3 this morning.

and get an answer like:

> Yes, I saw 100 failures. Most were research-tick hitting its time limit: that needs a settings
> change. How to check it yourself is on screen. I've written the findings to the suggestions table.
> Should I watch research-tick and tell you next time if it's still happening?

and then, at the next "what needs my attention?":

> You asked me to watch research-tick. I flagged that it hit its time limit, 2 days ago, and it's
> still happening (or: it has calmed down).

It had to stay read-only, keep to its own environment, never leak personal data, and not be steered
by what the logs say.

## What it is

Delivered as a stack of PRs (#218, #220, #221, #225, #226, #227, #228; docs in this one).

| Tool | Reads | Returns |
|---|---|---|
| `log_review(function?, topic?, hours=24, start?, end?)` | this environment's Lambda logs: four fixed Logs Insights queries over every group at once (error lines, counts per function, REPORT numbers, a 7-day baseline) | per function: error lines, each one's **root cause** (`log_review.CAUSES`) with its **fix type** (code, settings, transient, data, permissions, packaging), whether it is more than usual, runs, duration and memory against the limit; example lines (redacted, `untrusted`); "check it yourself" cards with the same queries |
| `api_errors(api?, status?, hours=24, start?, end?)` | the APIs' access logs (public, admin, assistant): three fixed queries | errors by status and **who answered** (firewall, throttle, sign-in, API Gateway, the Lambda), each with its root cause and fix type; error rate, first and peak hour; a Lambda failure points at `log_review` for the same window |
| `firewall_review(hours)` (production only) | the WAF logs, as before | plus the most-blocked client addresses, **masked**: `123.XXX.XXX.34`, said as "an address ending in .34" |
| `follow_up()` | the suggestions table, then each logged finding's log again since it was last mentioned | still happening (with the count then and now: getting worse, easing off) or calmed down (forgotten) |
| `watch(kind, id)` / `watch_list()` | `function` (any name for it), `table` (new: candidate ideas, findings), and the kinds before | a watched function's errors in the last day and what the suggestions table says was wrong with it; a watched table's on-time writes |
| `cli_help(commands, options?)` | the CLI reference | each command's help **and under it a suggested exact command**, filled in from what the operator said ("seed a topic called Watering vegetables" → `topics create --topic-id watering-vegetables --name 'Watering vegetables'`), `<placeholders>` for the rest |

A topic deep dive (`log_review(topic=...)`) reads the topic's `research-tick` and `daily-cycle` logs
narrowed to lines naming the topic or written by its adapter (`crypto_feed:`, `github_trending:`,
...). For crypto that is where "coins dropped from the pool for having no usable history" shows up,
classified as `source_data`.

## The rules, and where each is held

**Read-only, except the assistant's own list.** The MCP server's role may `logs:StartQuery`,
`logs:ListTagsForResource`, `GetQueryResults` and `StopQuery`, nothing else on logs (no
`GetLogEvents`, no `FilterLogEvents`: those return raw lines without a fixed query). The one write
is still the suggestions table (`memory.tf`). A logged finding is written there as its kind, the
function, two counts and a fix type, checked by `memory._write`'s allowlist: there is nowhere for a
log line to go.

**By environment, project and ManagedBy tag** (`infra/modules/ops-assistant/logs.tf`,
`lambdas/ops_mcp/logs.py`):

| | reads |
|---|---|
| dev's assistant | `/aws/lambda/<prefix>-dev-*`, `/aws/apigateway/<prefix>-dev-*`, tagged `Environment = dev` |
| production's | production's, and `<prefix>-shared-*` tagged `Environment = shared` |

Held three ways, any one enough: IAM allows those name patterns only with the project's
`ManagedBy` and `Project` tags and a readable `Environment`; the Deny in `isolation.tf` refuses any
other `Environment`; and the code checks the name and lists the group's own tags before every query.
Dev never reads production's or shared; production never reads dev's.

**Fixed queries.** The model never writes a query. The only values put into one are a topic id that
passes `ID_PATTERN`, an adapter prefix from a fixed list, and an HTTP status that is a whole number
from 100 to 599. Windows are clamped: at most a week long, at most 30 days back.

**Root cause in code.** `log_review.CAUSES` and `api_errors.cause()` decide it from fixed patterns
and from API Gateway's `errorType`. The model only says it.

**No personal data out** (`lambdas/ops_mcp/redact.py`). Every log-derived string goes through
`redact.scrub`: e-mails, bearer tokens and JWTs, AWS keys, `key=value` secrets, account ids, card
numbers (Luhn-checked), phone numbers and long secrets are replaced; addresses are masked to their
first and last part. The agent's spoken answer passes `redact.sweep_answer` last, so whatever the
model repeated is swept too.

**Not steered by the logs.** A log line that reads like instructions to a model ("ignore previous
instructions", a fake tool call, `system:`) is **withheld whole** and replaced with a marker; the
answer says that lines were withheld, which is itself a sign of probing. Example lines are under
`untrusted`, and the prompt (and the server's instructions, for Alexa+) says log lines are data.
Commands still come only from the catalogue or the CLI reference, built in code.

**Time-bounded.** One request stops at 30 seconds. A tool reading logs waits at most 15 seconds for
Logs Insights and stops what has not finished; `follow_up` and `watch_list` read at most two logs per
call, eight seconds each, and leave the rest open for next time, never taking an unread log for calm.

## How the agent uses it

The prompt (`ops_agent/agent.py`) and, for Alexa+, the server's instructions say, in order: how many
errors, the main root cause, code fix or settings change or time; how to check it yourself is on
screen; the findings are written to the suggestions table; then offer to watch, and call `watch` only
on a yes. On a topic that failed or is late, offer a deep dive into its logs. "What needs my
attention?" starts with `follow_up` and `watch_list`. "How can I check this myself?" is `investigate`,
the runsheet. Every Admin CLI command on screen carries a warning to double-check it before running.

## Assumptions to check after deploy

- CloudWatch Logs evaluates `aws:ResourceTag` for `StartQuery` and `ListTagsForResource` on a log
  group. If it does not in this account, the tools say AWS refused and read nothing (they fail
  closed); the deployment runsheet says what to look at.
- A log group a Lambda created for itself before Terraform did carries no tags, so it is not
  readable until Terraform manages it.

## Second round: forgiving names, success rates, commands for anything

After the first test run, the operator asked for:

> "Finance and Crypto": did you mean Crypto & Investing? If so, here's what I found, but feel free
> to interrupt me.

and for success rates as well as errors, a list of the Lambdas and what they do, a suggested command
for any Admin CLI question, a mock-up for something like setting up a topic, and a talk button that
does not let go while it is still held. It was delivered as #232, #234, #235 and #238, with docs in
this PR.

| | What changed | Where |
|---|---|---|
| **Forgiving topic names** (#232) | Every tool that takes a topic takes its id, its name, or the operator's own words. An exact match is used as it is. A single close match is used and said first ("I took that to mean Crypto & Investing; tell me if you meant another topic."). Two close matches get "did you mean A or B?". Scoring is mostly how many of the operator's words the topic covers, with synonyms (finance ~ investing ~ markets, crypto ~ bitcoin, AI ~ artificial intelligence), plus how alike the spellings are. Words with characters no topic name has (`;`, `|`, `$`, quotes) are refused, never matched, so "crypto; topics delete crypto" goes nowhere. Function and table names in `architecture` and `watch` are forgiving the same way. | `ops_mcp/topic_match.py`, `architecture.forgiving` |
| **Success rates** (#234) | `log_review` runs a fifth fixed query that counts failed invocations (an `[ERROR]`, a timeout, a crash), once per request. Each function gets **Runs** and **Succeeded**, and the answer says "Of 120 runs, 97.5% succeeded; research-tick did worst, at 90%". A function with no errors still gets its row. `api_errors` gives each API's success share, and only says "all succeeded" when no status filter was asked. | `log_review.py`, `api_errors.py` |
| **The Lambdas and what they do** (#234) | `architecture(kind="function")` lists each function with what it is for and when it runs. The agent uses it for "list the Lambda functions". | `architecture.py` |
| **A suggested command for any CLI question** (#235) | Every help card from `cli_help` and every step of a `cli_guides` guide has a drafted command under it. The values the operator gave are filled in, and the rest show as `<placeholders>`. Each draft is headed by a ⚠️ warning to double-check every value before running it. | `cli_guide.py`, `suggestions.DOUBLE_CHECK_WARNING` |
| **Mock-ups for new users** (#235) | A new `topic-setup` guide maps the operator's words to options. Editorial goals and a focus go into `--editorial-goals-json` (`primary_focus`, `exclusion_criteria`). Keywords to look for go into `--config-json` `title_keywords`, and keywords to ignore go into the exclusions. A fallback model comes from `models list`. Its example is a full `topics create` for "Watering vegetables". The agent never makes up an id, a model id or a number the operator did not give: it leaves a placeholder. | `cli_guide.GUIDES["topic-setup"]`, `ops_agent/agent.py` |
| **Push to talk** (#238) | The talk button captures the pointer on press, so a mouse drifting off it while held does not end the question. Once a hold has heard words, a session the browser cuts short (`aborted`, a network blip, an end with no sign of sound) is listened through. A refusal, the release, the 60-second ceiling, or a run of sessions that hear nothing still ends it. | `frontend/ask.js` |

The rules above still hold. Matching is done in code, before any value reaches a query: a matched
topic is a real topic id, and an unmatched word is never put into a query. Commands still come only
from the CLI reference, built in code. The prompt and the server's instructions (for Alexa+) say
the same things.
