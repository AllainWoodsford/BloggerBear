# Article research

How a topic's data source becomes a published article: where the adapters, the third-party API
keys, the Lambdas, the findings and the candidate ideas each come in, how they end up as an
article in S3, and where the AI agents fit.

The operator's assistant explains the same thing, with this environment's resource names on
screen. Ask it "how is an article researched?" (it calls `architecture` with `feature`
`article-research`). The rules this follows are in the [project plan](../project-plan.md#2-hard-constraints):
research is diff-first, nothing is published without a review, a financial topic's article is
never published when it is drafted, and new sources are added as adapters.

Resource names below use `<prefix>` (your `unique_name_prefix`) and `<env>` (`dev` or
`production`).

```mermaid
flowchart LR
    admin(["Operator<br/>admin CLI"])
    subgraph setup["1-3. Topic, schedules, keys"]
        topics[("topics table")]
        sched["EventBridge Scheduler<br/>two schedules per topic"]
        ssm["SSM Parameter Store<br/>API keys (SecureString)"]
    end
    subgraph research["4-5. Research tick (heartbeat)"]
        tick["Lambda research-tick"]
        adapter["Adapter<br/>github_trending, hacker_news,<br/>crypto_feed, web_search,<br/>satellite_vision, rail_access"]
        src[["Third-party sources<br/>GitHub, HN, CoinGecko,<br/>GDELT, AgentCore,<br/>Sentinel-2, OpenStreetMap"]]
        snap[("S3 content bucket<br/>snapshots/")]
        findings[("findings table<br/>14-day TTL")]
    end
    subgraph daily["6-9. Daily cycle (once a day)"]
        sfn["Step Functions<br/>retries, then DLQ"]
        cycle["Lambda daily-cycle"]
        ideas[("candidate-ideas table<br/>7-day TTL")]
        models[("models, model-config,<br/>prompt-refinements")]
        review["Fresh-data review<br/>+ compliance review"]
    end
    subgraph out["9. The article"]
        body[("S3 content bucket<br/>articles/&lt;id&gt;.md")]
        articles[("articles table")]
        site[("S3 site bucket<br/>articles/&lt;id&gt;.html")]
        queue[("moderation-queue")]
    end
    bedrock["Amazon Bedrock<br/>Converse API"]

    admin --> topics
    admin --> sched
    sched --> tick
    tick --> adapter --> src
    adapter -. "key at cold start" .-> ssm
    tick -- "diff vs last Finding's snapshot" --> snap
    tick -- "only if something is new" --> bedrock
    tick --> findings
    sched --> sfn --> cycle
    cycle --> findings
    cycle -- "3 angles" --> bedrock
    cycle --> ideas
    cycle --> models
    cycle -- "draft" --> bedrock
    cycle --> review
    review -- "publish" --> body & articles & site
    review -- "hold" --> body & articles & queue
```

## The steps

| # | Step | What happens | Where in the code / infra |
|---|---|---|---|
| 1 | **Topic and adapter** | A topic is a row of settings created with the admin CLI: which adapter reads its source and with which `adapter_config` ([the keys](../configuration.md#adapter-settings)), how often to research, its editorial goals, its model ([which model a call uses](../models.md#which-model-a-call-uses)) and review mode. The adapter is the only code that knows anything about the source. | `topics` table; `lambdas/common/adapters/` (`base.py` is the contract, `registry.py` maps names to classes) |
| 2 | **Schedules** | Creating a topic creates two EventBridge Scheduler schedules: a research heartbeat (`research_cadence`, every hour by default), which invokes the research tick, and the daily cycle at its time and timezone, which starts the daily cycle's state machine. The heartbeat is not the research interval: each tick decides for itself whether the topic is due (step 4). Each topic has a slot in the stagger (`stagger_slot`), so the heartbeat runs at the slot's minute past the hour (slot 1 at :05, slot 2 at :10) and the daily cycle after every topic's heartbeat, one slot apart; the schedules as written are on the row, and `topics schedule` lists them. A paused topic's schedules are disabled. Topics are runtime data, not Terraform. | `lambdas/common/scheduler.py` and `common/stagger.py`, called by `admin_api_handler.py`; `<prefix>-<env>-<topic_id>-research-tick` / `-daily-cycle` |
| 3 | **Third-party sources and their keys** | API keys are SecureStrings in SSM Parameter Store. Terraform sets an environment variable that holds each parameter's *name*, and the Lambda reads the value at cold start. Keys are never in code, tables or logs. Both keys are optional. News search uses GDELT, which needs no key, and falls back to Bedrock AgentCore web search, which the Lambda's IAM role authorises. The vision topics' sources need no key either: Sentinel-2 scenes through the Earth Search STAC API and the public `sentinel-cogs` bucket (`satellite_vision`, `rail_access`), and OpenStreetMap's rail lines and stations through the Overpass API (`rail_access`); their measuring is done by the vision worker ([vision](blogger-vision.md)). | `COINGECKO_API_KEY_PARAMETER` (`crypto_feed.py`), `GITHUB_API_TOKEN_PARAMETER` (`github_trending.py`), `lambdas/common/web_search.py`, `lambdas/common/vision_sites.py`, `lambdas/common/osm.py`; [how to store a key](../deployment-runsheet.md#api-keys-for-the-data-sources) |
| 4 | **Research tick: diff first** | A paused topic (`topics pause`) stops here: nothing is fetched until it is resumed. So does a topic that is not due (`not_due`): a tick does work only once `research_interval_hours` has passed since the topic was last checked (the topic's own, else the pipeline-wide one, else 1 hour, less 5 minutes for scheduler jitter). A manual `topics trigger --pipeline research_tick` always skips that check. Otherwise the adapter fetches the source, and a fetch that answers stamps the topic's `last_research_at`; one that fails stamps nothing, so the next heartbeat tries again. `material_diff` then compares what came back with the snapshot of the topic's last Finding, not with the last fetch. "New" means new to the topic: every stored snapshot carries the items already reported (`_seen`, from the adapter's `item_keys`, each kept 7 days, at most 2,000), so an item that drops out of a feed and comes back is not reported again. If nothing is new, the tick returns `no_change`: no model is called, and no snapshot or Finding is stored (an adapter with `keeps_running_state`, which only the vision adapters set, still saves its running state). | `lambdas/research_tick_handler.py` (`_not_due`, `_merge_seen`); `lambdas/common/research_schedule.py`; `Adapter.known_keys` in `common/adapters/base.py` |
| 5 | **Findings** | Bedrock summarises only what is new. The prompt carries the topic's editorial goal (its own `editorial_goals.primary_focus`, else its adapter's default, else a global one, with its `exclusion_criteria` on top) and a relevance rule keyed to the topic's name; an adapter may supply its own prompt instead (`build_summary_prompt`, as the crypto feed does). Only once the summary is back is the raw snapshot stored, so a summary call that fails stores nothing, and the next due tick reports the same change again. The Finding then holds the summary, where its snapshot is (`raw_snapshot_s3_key`), its source references, the summary call's model and tokens (`research_call`, counted into the article it later feeds), and, when the adapter gives any, its `figures`, `planner_notes` and `subjects`. To look at a topic's Findings: ask the assistant ("what did research find for X?", `topic_findings`), run `python scripts/admin_cli.py findings list <topic_id>` (with `--next-article` for what the next article reads), or use the `topic-findings` Claude Code skill. | `lambdas/research_tick_handler.py` (`_build_prompt`); `common/editorial_resolver.py`, `common/relevance.py`; `<prefix>-<env>-content` bucket, `snapshots/<topic_id>/<captured_at>.json` (expires after 21 days); `<prefix>-<env>-findings` (expires after 14 days); `GET /topics/{topic_id}/findings` |
| 6 | **Candidate ideas** | Once a day, the schedule starts the daily cycle's state machine, which runs the daily-cycle Lambda (for retries, see [below](#runs-that-write-nothing-and-runs-that-fail)). The cycle reads the topic's Findings since its last article (`last_article_at`), but never from further back than its findings window (`findings_window_hours`, 24 hours by default, at most 336): at most 48 Findings, newest first, and at most 40,000 characters of their summaries, the oldest dropped first. A manual `topics trigger --pipeline daily_cycle --force` ignores the last article and reads the whole window. With nothing in the window, the run stops with `no_findings` before any model call. A crypto topic writes under the day's editorial goal (one of four, drawn from the UTC date, or pinned with `adapter_config.editorial_goal`) and keeps only the Findings captured that UTC day; with none from today, it follows the newest Finding's day and that day's goal. A topic whose adapter rotates through several subjects (one city a day or a week; `Adapter.day_subject`, `common/day_subject.py`) writes about the day's subject only: it reads from that subject's last article, keeps the findings that report it (`subjects` on the Finding), and stops before any model call if there are none. A topic with a goal rotation (`editorial_goal_rotation`) takes its goals in turn and does not stop when nothing is new: it revisits its last set of Findings under a goal not yet used on them, and stops with `no_findings` only when there is no set to revisit, every goal has had it, or its Findings have expired ([the setting](../configuration.md#dynamodb-settings-changed-with-no-deploy)). Bedrock is then asked for three angles, under the same editorial goal and relevance rule as step 5, and is shown the topic's last five published titles so a story already told is not told again. The angles are stored as candidate ideas and the first is selected. A reply with no usable angle in it (a refusal, or no text at all) ends the run with `no_usable_angles`, and the topic's model may move on ([rotation](../models.md#rotation-trying-several-models)). | `lambdas/daily_cycle_handler.py` (`_run_daily_cycle`, `_select_goal_and_findings`, `_pick_rotation_goal`, `_ideate`); `common/findings_window.py`, `common/editorial_goals.py`, `common/goal_rotation.py`; `<prefix>-<env>-daily-cycle` state machine; `<prefix>-<env>-candidate-ideas` (expires after 7 days) |
| 7 | **Drafting** | Bedrock drafts the article from the selected angle and the Findings' summaries, with the topic's model from the model registry ([which model](../models.md#which-model-a-call-uses)). The draft folds in the topic's editorial goal and a relevance boundary, the day's article style for a crypto or rotating topic, the topic's approved prompt refinements (the bear's gear, `equipment.py`), an excerpt of a top-voted past article, financial guidance where it applies, and what the topic's adapter says its articles must always do (`figure_guidance` and `drafting_guidance`: the crypto feed's approximate figures and its closing "At a glance" table and "Key takeaways"). The title is a separate call, made after the body, from the angle alone: it never sees the article. It must be one line of at most 20 words, not a refusal or a request for clarification; it is retried once, and if it still fails the article is held (step 8). A draft cut off at its token limit, after one retry with more room, is held too. | `lambdas/daily_cycle_handler.py` (`_draft_article`, `_draft_title`); `lambdas/common/model_routing.py`, `equipment.py`, `compliance.py`; `models`, `model-config`, `prompt-refinements` tables |
| 8 | **Reviews** | The fresh-data review runs first: the adapter re-reads its source (`review_evidence`), and a model is asked which claims in the title and body that fresh data shows to be stale, contradicted or unsupported. `off` skips it. In `shadow` mode, the default, the result is only recorded. In `enforce` mode, minor problems get one revision pass that plain code checks (no new figure or link, about the same length, the same number of headings), and a major problem, a revision that fails those checks, or a review that could not run holds the article, unless `review_on_unavailable` is `note` ([the modes](../../scripts/README.md#usage)). Next, a financial topic's draft gets its standing "not financial advice" disclaimer, appended in code. Then the compliance review: the model only nominates items (PII, a fabricated claim, investment advice), each with a quote from the draft, and code decides which of them hold the article; code also holds any figure over 20, other than a year, that is within 3% of no figure in the Findings' summaries. A financial topic makes no compliance call at all: its article is always held. So is an article with a hold reason from earlier (a truncated draft, a title that is not one, the fresh-data review), and every article of a topic with `force_manual_review` (the vision topics), whose compliance review still runs and is recorded. | `lambdas/common/fresh_review.py` (`run_review`, `enforcement_action`, `run_revision`), `lambdas/common/compliance.py` (`review_draft`, `invented_figures`); `daily_cycle_handler.py` (`_run_fresh_review`, `_enforce_review`) |
| 9 | **The article in S3** | The body is written to the content bucket; when an enforced review's revision replaced it, the original is kept beside it. The article's row records its lineage, tokens and cost, its review, the gear it was written with, its source credit and its figures, and the week's totals go to the Stats tables. A published article also gets a static page in the site bucket and a musing; the page says it was checked against current data only when the review was enforced (`common/fact_check.py`). A held article waits in the moderation queue with its reasons, for a person (`admin_cli approve`) or for the reviewer bear: once it has waited 48 hours he may read it and approve it himself, and a financial article he approves is flagged for a person to check. A topic with `force_manual_review` always waits for a person, as does one he may not approve for (`reviewer_may_approve`, `may_approve`; [the reviewer bear](../configuration.md#the-reviewer-bear-professor-grizzlewick)). Published or held, the topic's `last_article_at` (and, for a rotating topic, the day's subject's stamp) is set to the run's start, so the next window begins there, and the Stats page's Articles figures are rebuilt. | `daily_cycle_handler.py` (`_publish_or_moderate`, `_record_article_written`); `articles/<id>.md` (and `articles/<id>.original.md`) in the content bucket; `articles` table; `articles/<id>.html` in the site bucket (`common/static_pages.py`); `common/musings.py`; `moderation-queue`; `common/reviewer.py`; `stats-current` |

Every Bedrock call goes through `lambdas/common/bedrock.py`, which records tokens and cost. That is
how each article's lineage, and the public Stats page, know what an article cost. Which model a
call uses, and its fallback, is in [models](../models.md).

## Runs that write nothing, and runs that fail

A research tick returns `paused`, `not_due`, `no_change` or `material_change`; only the last writes
a Finding. A daily cycle that writes no article returns `paused`, `no_findings` or
`no_usable_angles`: it stores no article and stamps no `last_article_at`, so the next run still
reads from the last article, within its findings window.

Neither handler raises: each catches its own errors and returns `"status": "error"` with the
reason. The research tick's schedule invokes it directly, so a failed tick is tried again at a
later heartbeat. The daily cycle's state machine retries a failed invocation twice (after 30
seconds, then 60) and then sends it to the pipeline dead-letter queue, which
`admin_cli failed-executions list` shows. Because the handler catches its own errors, only a
timeout (the Lambda has 300 seconds) or a crash is retried and reaches the queue. To Step Functions
a caught error is a successful invocation, and it shows only in the execution's output. A manual
`topics trigger --pipeline daily_cycle` invokes the Lambda directly, without the state machine, so
a failure there never reaches the queue and leaves no FailedExecutions record.

## Where the AI agents are

None of the model calls above is an agent. Each one is a single Bedrock Converse request made by a
Lambda: a summary, three angles, a draft, a title, the reviews and a revision. The pipeline
decides what happens next in code, not by asking a model, which is why rules like "diff first"
and "a financial article is never published when it is drafted" cannot be talked around.

There are two agents. The **operator's assistant** is a Strands agent (`<prefix>-<env>-ops-agent`)
that calls the MCP server's read-only tools (`<prefix>-<env>-ops-mcp`). Those tools read the same
topics, findings, candidate ideas and articles described here. They report what needs attention
and suggest admin CLI commands, but change nothing. The **vision triage agent** runs inside a
vision topic's research tick (step 4), only after a measured change has passed its thresholds: it
looks at the worker's figure, may re-measure or check the news with a few bounded tools, and
answers "real" or "artefact"; anything unsure is an artefact, and no Finding is written. Its page
is [vision](blogger-vision.md).

## Adding a source

A new source is a new adapter (`fetch_state`, `material_diff`, `source_refs`, and the `sources` it
credits), registered in `registry.py`. If its source is made of items, give it `item_keys` too, so
the seen-set (step 4) knows what has been reported; without them, `material_diff` has only the
last Finding's snapshot to compare against. Each source it credits must also be in the "Data
sources" list on `frontend/about.html`, with the same sentence and link:
`lambdas/tests/test_attribution.py` fails for an adapter with no `sources`, and for a source the
About page does not name. If it needs a key, the key goes in a new SSM parameter whose
name Terraform passes to the function. Nothing in steps 4 to 9 changes. See "Adding an adapter" in
[docs/project-plan.md](../project-plan.md) §6.
