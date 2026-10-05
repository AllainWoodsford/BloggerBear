# The operator's assistant as an architecture expert

## Why

Asked "Can you check what's in logs like any 400 error?", the assistant answered:

> I don't have access to logs or the ability to search for specific errors like 400s. [...] you'd
> need to check CloudWatch logs or your monitoring system directly.

The first half is true and stays true: the assistant cannot read logs, on purpose (its role is read-only
on named tables, and logs hold what visitors and attackers sent). The second half is the problem.
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

`table_sample(name)` uses the same resolver to read the newest row of a table and say whether the
table looks healthy. For example: is a new candidate idea being written at about each topic's daily
cadence? See that PR for the access model (default tags in SSM Parameter Store, tag-conditioned
IAM, environment isolation) and the security-events rules (the attacker-written `untrusted` field
is never read, and PII is redacted).

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
