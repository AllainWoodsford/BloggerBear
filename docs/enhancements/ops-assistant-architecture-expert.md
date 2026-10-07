# The operator's assistant as an architecture expert

## Why

Asked "Can you check what's in logs like any 400 error?", the assistant answered:

> I don't have access to logs or the ability to search for specific errors like 400s. [...] you'd
> need to check CloudWatch logs or your monitoring system directly.

The first half was true when this was written: the assistant could not read logs (its role was
read-only on named tables, and logs hold what visitors and attackers sent). It reads them now, with
fixed queries, by tag, and a PII sweep: [the log reader](ops-assistant-log-reader.md). The runsheets
below stay, for "how can I check this myself?". The second half was the problem.
Someone who knows the system would say *which* log group, *which* dashboard, which of AWS's own
console pages, and what query to run. This enhancement gives the assistant that knowledge.

## What it adds

Two delivery steps, each its own PR.

### PR 1: knowing the architecture (`architecture`, `investigate`)

- **`lambdas/ops_mcp/architecture.py`** is a catalogue of every AWS resource the project deploys:
  17 DynamoDB tables, 13 Lambdas, 3 REST APIs, the daily-cycle state machine, the dead-letter
  queue, the alerts topic, 3 dashboards, the WAF and access log groups, the schedules, the buckets
  and the web ACLs. Each entry has its purpose, its keys, indexes and TTL, who writes and reads it,
  and its log groups, dashboards and alarms. Names are templates (`bloggerbear-{env}-...`) filled in
  with the assistant's own `ENVIRONMENT_NAME`.
- **`architecture(name?, kind?)`** describes a resource. The operator can paste it however they have
  it: `bloggerbear-prod-candidate-ideas`, `bloggerbear-production-candidate-ideas`, an ARN, a log
  group path, `CandidateIdeas`, `CANDIDATE_IDEAS_TABLE` or "candidate ideas".
- **`lambdas/ops_mcp/runsheets.py`** / **`investigate(symptom?, status?, api?)`** returns a runsheet
  per symptom:

  | id | for |
  |---|---|
  | `api-errors` | 4XX/5XX (400, 403, 429, 500, ...) on the public, admin or assistant API |
  | `pipeline-failed` | a daily run that failed, or published nothing |
  | `research-late` | research not running on time |
  | `lambda-errors` | a Lambda erroring, timing out, slow or throttled |
  | `security` | blocked requests and the firewall |
  | `feedback` | reader feedback rejected, screening used up |
  | `costs` | spend higher than expected |
  | `assistant` | the assistant itself: sign-in, 401/403/429 |

  Each runsheet starts with what the assistant can check itself (`alarms`, `pipeline_health`, ...).
  Then come the places to look, in order: the project's dashboards, AWS's own generated views (the
  API Gateway stage dashboard, the Lambda Monitor tab, CloudWatch's automatic Lambda dashboard,
  WAF's Traffic overview, Step Functions executions), and the log groups. Every Logs Insights
  query appears as a copyable card. For example, the `api-errors` runsheet with `status=400`
  includes:

  ```
  fields @timestamp, status, httpMethod, resourcePath, errorType, wafStatus, integrationLatency
  | filter status = 400
  | sort @timestamp desc
  | limit 50
  ```

  This is against `/aws/apigateway/bloggerbear-<env>-public-api-access`. `errorType` says who
  answered: the Lambda, the firewall (`WAF_FILTERED`) or throttling (`THROTTLED`).
- The agent's system prompt now says: never stop at "I can't"; call `investigate` and say the
  runsheet is on screen. For "what is this table/log group/...", call `architecture` with the name
  exactly as given.

### PR 2: reading a sample row (`table_sample`)

`table_sample(name, topic?, rows=1)` (`lambdas/ops_mcp/samples.py`) uses the same resolver to read
the newest row (up to 3) of any of the project's tables in this environment and put it on screen.
For findings and candidate ideas it also checks each topic. Findings are on time if the newest is
within two research intervals; candidate ideas, within a day and two hours. "Are candidate ideas
working?" gets a real answer: "Hacker News has no recent candidate idea: that looks like something
isn't running."

How it reads each kind of table:

| Table | Read as |
|---|---|
| findings, candidate-ideas, prompt-refinements | a Query per topic, newest first, Limit 1 |
| articles, moderation-queue, security-events | a Query per status on the status index, newest first, Limit 1 |
| anything else | a Scan capped at 300 items, newest by its time field |

#### What it may read: the tags

The rule is the project's default tags, not a list of tables. A table is readable only if it carries
`ManagedBy = Terraform` and `Project = BloggerBear` (exactly as the providers' `default_tags` put
them), and an `Environment` the assistant may read:

| Assistant | May read Environment |
|---|---|
| dev | `dev` only. Never `production`, never `shared` |
| production | `production` and `shared` |

The rule is enforced twice, from the same values:

1. **IAM.** In `infra/modules/ops-assistant/main.tf`, `SampleTaggedTables` allows Query, Scan and
   ListTagsOfResource on `bloggerbear-*` tables, with three `StringEquals` tag conditions. The Deny in
   `isolation.tf` refuses any other Environment: for dev, anything not `dev`; for production,
   anything neither `production` nor `shared`.
2. **Code.** Before any row is read, `samples.py` lists the table's tags and refuses on any
   difference from `OPS_DEFAULT_TAGS` (the module's `var.default_tags`, passed as JSON). The readable
   list is the intersection of what the module told the function (`OPS_READABLE_ENVIRONMENTS`) and
   the rule (only production reads `shared`), so neither can widen the other.

Both come from the root's own provider `default_tags`, which every resource the root creates
really carries. A test holds the root, the module and the code to the same values and the same
rule. There is no copy in SSM Parameter Store or DynamoDB. An earlier draft kept one, written by
bootstrap, but a second source added nothing the build-time test doesn't already guarantee. It
would have cost a bootstrap re-apply, an extra permission and an extra failure mode.

DynamoDB's tag-based access control must be on for the account and region (DynamoDB console >
Settings). Where it is off, tag conditions see no tags and the statement grants nothing. It fails
closed, and `table_sample` says AWS refused.

#### Never read, whatever the tags

`bloggerbear-<env>-ops-briefings` holds what the agent wrote after reading untrusted text.
`briefings.tf` keeps it out of the agent's reach so that text is never put back in front of it.
`table_sample` refuses it by name (`NEVER_SAMPLED`), before any tag check.

#### Security events: the payload is never read, PII is never shown

- SecurityEvents' `untrusted` field holds what a blocked client sent (the path and the matched text,
  in other words an attack payload). `client_hash` identifies a client. The read uses a
  `ProjectionExpression` naming only `SECURITY_EVENT_FIELDS`, so DynamoDB never returns either
  field. `FORBIDDEN_FIELDS` removes them again if they somehow arrive, and the page shows them as
  "withheld: never read".
- Every value from every table passes through `redact`:
  - fields that identify a person (`user_id`, `client_ip`, `email`, ...) are replaced whole;
  - e-mail and IP addresses inside text become `[email]` and `[ip]`;
  - text is cut to 300 characters and cleaned of control characters.
- Row values go under `untrusted`. The spoken answer only says how old the newest row is and whether
  writes look on time. A row's contents are never read aloud.

**Deploying it:** nothing extra. Dev deploys as usual.

### Layers and features

Asked with nothing named ("how does the project work?"), `architecture` names nine **layers**
(edge, presentation, API gateways, identity, orchestration, compute, AI, data, observability) and
asks which one; `layer` gives one. A layer answers "what is this made of?".

A **feature** (`feature`) answers "how does this one thing work?": a small, logical grouping of the
parts that do one job, told as steps in the order the work flows, across the layers. The first is
`article-research`: topic and adapter, schedules, third-party keys in SSM, the diff-first research
tick, findings, candidate ideas, drafting, the reviews and the article in S3, then where the agents
are. Each step's parts are catalogue resources (`kind:key`, which the tests hold to the catalogue)
or plain words for what is not ours. The written version is
[docs/architecture/article-research.md](../architecture/article-research.md);
[docs/architecture/README.md](../architecture/README.md) says how to add the next one (for
example "prompts as assets").

## The environment rule

Every answer is about the assistant's own environment, whatever name was pasted:

| Pasted (assistant in dev) | Answer about | `rewritten` | `data_allowed` |
|---|---|---|---|
| `bloggerbear-dev-candidate-ideas` | `bloggerbear-dev-candidate-ideas` | false | true |
| `bloggerbear-prod-candidate-ideas` (or `-production-`) | `bloggerbear-dev-candidate-ideas` | true, and the answer says so | true |
| `bloggerbear-staging-candidate-ideas` | `bloggerbear-dev-candidate-ideas` (its purpose) | false | **false**: no data is read for a name that is neither environment's |
| `candidate ideas`, `CandidateIdeas` | `bloggerbear-dev-candidate-ideas` | false | true |

## Why a catalogue in code (and not a table, a parameter or Terraform at run time)

- **Fast and free.** The catalogue is a module constant, and its lookup index is built once at
  import. An answer reads no table, no parameter and no AWS API. It costs nothing and still works
  when the rest of the account is having a bad day, which is when it is most needed.
- **Cannot drift.** The Lambda package has no `infra/`, so nothing parses Terraform at run time.
  `lambdas/tests/test_ops_mcp_architecture.py` reads `infra/` instead, and CI fails when any of
  these is added, renamed or changed in Terraform without the catalogue changing too: a table
  (with its keys, indexes and TTL), a Lambda, a dashboard, an alarm, an API, a log group or a fixed
  schedule. The same applies to the list of tables the assistant may read, and to where the
  assistant and the edge dashboard are deployed. This is the same approach as
  `cli_reference.json` for the Admin CLI.
- **Ours, and safe to speak.** Every word in the catalogue and the runsheets is ours. The
  operator's input is only matched against it and never repeated into `spoken`. The one value a
  caller can put into a query is an HTTP status, held to a whole number from 100 to 599. No query
  selects a visitor's address.

## Keeping it up to date

When you add or change a resource in `infra/`, the drift test names what is missing. Add or edit
its entry in `CATALOGUE` (`lambdas/ops_mcp/architecture.py`). If it is something people
investigate, add a step to a runsheet in `lambdas/ops_mcp/runsheets.py`, or add a runsheet.
