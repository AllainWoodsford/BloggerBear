# Vision runsheet: the worker, rail topics and the evidence

The vision worker measures satellite imagery for the `satellite_vision` and `rail_access` topics.
It is **off by default**, in both environments. This runsheet turns it on in one environment,
creates and runs vision topics (the Rail Access Monitor's `rail_access` topics in detail), and reads
back what the pipeline measured and decided. It picks up from a working dev deployment
([deployment-runsheet.md](deployment-runsheet.md)).

Background, not repeated here: how it all fits ([architecture/blogger-vision.md](architecture/blogger-vision.md));
every setting ([configuration.md, Vision and rail access](configuration.md#vision-and-rail-access)); and,
for the example `rail-cities` topic, the full wording of its editorial goals and the reasons for
it, what each of its articles shows, the reviewer's checklist and dev's seeding steps
([RailVisionSeeding.md](RailVisionSeeding.md)). Production has its own short section
([production-runsheet.md, Vision](production-runsheet.md#vision-optional)), and so do
[two accounts](deployment-separate-accounts.md#vision-optional). The OpenCV competition's
by-hand sessions are the [COOL benchmark runsheet](cool-benchmark-runsheet.md) and the
[evaluation runsheet](evaluation-runsheet.md).

## Contents

- [Turning the worker on](#turning-the-worker-on)
  - [1. Re-apply the bootstrap by hand](#1-re-apply-the-bootstrap-by-hand)
  - [2. Set the `VISION_ENABLED` repository variable to `true`](#2-set-the-vision_enabled-repository-variable-to-true)
  - [3. Check it](#3-check-it)
- [Creating a vision topic](#creating-a-vision-topic)
- [`rail_access` topics](#rail_access-topics)
  - [Create the topic](#create-the-topic)
  - [What the first tick does](#what-the-first-tick-does)
  - [The first-deploy check: one tick per city](#the-first-deploy-check-one-tick-per-city)
  - [A large city's first network can take a minute](#a-large-citys-first-network-can-take-a-minute)
  - [How the cadence works](#how-the-cadence-works)
  - [When a run writes nothing](#when-a-run-writes-nothing)
  - [Where things land](#where-things-land)
  - [Reading the agent's trail](#reading-the-agents-trail)
  - [Adding a city](#adding-a-city)
  - [Changing `reach_m` or a polygon](#changing-reach_m-or-a-polygon)
  - [Moving from city topics to `rail-cities`](#moving-from-city-topics-to-rail-cities)
  - [After deploying the stops layer](#after-deploying-the-stops-layer)
- [Evidence: what the pipeline measured and decided](#evidence-what-the-pipeline-measured-and-decided)
  - [The queries](#the-queries)
  - [Reading a line](#reading-a-line)
  - [The first-deploy check of the evidence lines](#the-first-deploy-check-of-the-evidence-lines)
  - [The demo topic's two-tick procedure](#the-demo-topics-two-tick-procedure)
- [Turning it off](#turning-it-off)

## Turning the worker on

Nothing of the worker is created until you turn it on, and you turn it on per environment with a
GitHub variable, not with a tfvars edit. Its design is
[enhancements/vision.md](enhancements/archive/vision.md); every setting it uses is in
[configuration.md](configuration.md#vision-and-rail-access).

### 1. Re-apply the bootstrap by hand

Do this first: the bootstrap is never applied by CI. It adds two statements to the deploy roles:
Lambda functions and log groups named `<prefix>-*-vision-*` in `vision_region` (default
`us-west-2`, beside the Sentinel-2 imagery). Pass the same other variables as your
[first apply](deployment-runsheet.md#1-bootstrap-once-by-hand), and check the plan touches only the
roles' policies. If you change `vision_region`, pass the same value to the bootstrap and to the
environment. Until this is done, an apply with the worker turned on is refused.

### 2. Set the `VISION_ENABLED` repository variable to `true`

In GitHub → Settings → Secrets and variables → Actions → Variables
([configuration.md](configuration.md)), then run a deploy: merge a change under `infra/`,
`lambdas/` or `frontend/` to `dev` (the Terraform workflow's paths filter starts no apply for a
docs-only merge), or `workflow_dispatch` the Terraform workflow from the `dev` branch. Nothing in
`terraform.tfvars` changes (a `vision_enabled` there would beat the variable).

The apply creates, in `vision_region`: the worker (arm64, python3.12), its role (its own log group only), its log
group, and a small artifacts bucket its package is uploaded through. In the home region it
gives the research tick the worker's ARN (`VISION_WORKER_ARN`), and gives the right to invoke
that one function to the pipeline's shared execution role (`lambda_exec`), which thirteen
functions use (twelve when `reviewer_enabled` is false and the reviewer bear is not deployed):
the research tick, the daily cycle, the admin API and ten more. Both apply
workflows read the variable as a switch: `true`, in any letter case (GitHub Actions compares
strings ignoring case, so `TRUE` and `True` count too), turns the worker on, and any other
value, or unset, is read as off.

**This also turns the worker on in production at the next release:** `terraform-production-release.yml` runs in `environment: production` and reads
`vars.VISION_ENABLED`, which falls back to the repository's value. To keep production off until
the dev trial is done, set `VISION_ENABLED` to `false` on the `production` environment. A
value on the `production` environment wins over the repository's, for production alone.

### 3. Check it

The worker reads public data over HTTPS (Sentinel-2 from the `sentinel-cogs` bucket, nothing
else) and needs no key and no other access. It has no alarm of its own: observability's alarms
are in the home Region, and the worker is not; read its log group,
`/aws/lambda/<prefix>-<env>-vision-worker`, in `vision_region`. The ops assistant reads it there
(`log_review`, "any errors in the vision worker?"), and each topic's running state (`vision_status`,
"how is the vision pipeline doing?"), granted by the same apply: nothing to do by hand
([ops-assistant-vision.md](enhancements/archive/ops-assistant-vision.md)). After turning vision on, ask it
"is the satellite worker on?": it should say on, and in which Region.

## Creating a vision topic

**A `satellite_vision` topic** needs `adapter: "satellite_vision"` and `sites` in its
`adapter_config` (one to ten lon/lat polygons, each with an `id` and optionally a `name`), and
usually an `object_noun`; everything else has a default
(`common/adapters/satellite_vision.py` documents every key); a topic without
`editorial_goals` is drafted under the adapter's default goal (`ADAPTER_DEFAULTS` in
`common/editorial_resolver.py`, #298). With the default 60 s `time_budget_seconds`, a tick
asks the worker only while 45 s of the budget remain, so in about its first 15 s: only the
sites whose new scene is found by then are measured, how many depending on the search's and
the worker's times (often fewer than `max_sites_per_tick`); the rest rotate to later ticks and
keep their place.

**A `rail_access` topic** watches up to eight cities, each inside one Sentinel-2 tile, and writes
about one of them a day: [`rail_access` topics](#rail_access-topics), below, creates one topic over
the four starting cities from a file.

**Both get `force_manual_review`** whether you ask or not: every article waits for
`admin_cli approve`.

To start a new vision topic:

1. Trigger the first tick by hand: `topics trigger <id> --pipeline research_tick`. The first
   scene measured at or above the topic's coverage floor is its first observation, reported once.
2. Wait about two minutes, or until `topics findings <id>` shows the Finding. The CLI waits only
   30 s for the Finding, and a tick that measures a scene can run longer (up to 58 s of it is the
   worker, inside the function's 120 s). So "No new Finding after 30s" is expected here.
3. Then trigger the daily cycle (`topics trigger <id> --pipeline daily_cycle`).

A Finding, then a held article, is what success looks like. A tick that measures nothing (the
worker off, every site in `last_error`), or only a scene under the floor (mostly cloud), writes
no Finding.

## `rail_access` topics

The Rail Access Monitor is one topic that watches several cities and writes about one of them a
day, by a fixed weekly rota ([what it measures](architecture/blogger-vision.md#part-two-the-trains-explainer);
every key and default: [configuration.md](configuration.md#a-rail_access-topic); the design:
[dailysubject_hook.md](enhancements/archive/dailysubject_hook.md)). Turn the worker on first (above):
without it each tick records `last_error` on the city and writes nothing.

This section follows the example's rota, a city a day. Dev's `rail-cities` has since moved to a
city a week (`rotation_period` `"week"`), its Monday weekly reading written up from three angles in
turn: [RailVisionSeeding.md §7](RailVisionSeeding.md#7-a-city-a-week-written-up-from-three-sides).

### Create the topic

`scripts/rail_topics.example.json` holds one complete admin API topic body,
`rail-cities`: the four starting cities (Sydney, Melbourne, Singapore and Boston, each with its box
from [rail-access-monitor.md §2](enhancements/archive/rail-access-monitor.md#2-the-angle-rail-access-not-rail-yards)),
`reach_m` 1000, the 80 s `time_budget_seconds` cap (see
[A large city's first network can take a minute](#a-large-citys-first-network-can-take-a-minute)), the rota (`rotation`, read in `rotation_timezone`), an editorial goal, a daily run
every day and a findings window of 192 hours (`findings_window_hours`). `topics create` takes a
topic as flags, not as a file, so read each field out of it, from the repository root:

```bash
field() { python -c 'import json, sys; v = json.load(open("scripts/rail_topics.example.json"))["topics"][0][sys.argv[1]]; print(v if isinstance(v, str) else json.dumps(v))' "$1"; }
python scripts/admin_cli.py topics create --topic-id "$(field topic_id)" --name "$(field name)" \
    --adapter rail_access --config-json "$(field adapter_config)" \
    --editorial-goals-json "$(field editorial_goals)" \
    --goal-rotation-json "$(field editorial_goal_rotation)" \
    --daily-cadence "$(field daily_cadence)" --daily-timezone "$(field daily_timezone)" \
    --findings-window-hours "$(field findings_window_hours)"
python scripts/admin_cli.py topics get rail-cities
```

The answer shows `"force_manual_review": true`, which was not sent: the admin API forces it for
this adapter. The example's editorial goal names no city: it asks for the measured figures of "the
day's city", the capture date and the coverage, with reach to a stop as the same straight-line
proxy as reach to a station and minutes as that distance at 80 m a minute; it calls every
simulated link theoretical, justified by its numbers and by what its straight line crosses; and
it rules out ridership, journey time, cost, recommendations, causes and any complaint, political
or resource-allocation view. Its `editorial_goal_rotation` carries the four angles (access,
network, simulations, transit) taken in turn on a city's Findings
([RailVisionSeeding.md §3 and §7](RailVisionSeeding.md#3-the-editorial-goal)); each day's mandate
names the city (below). A rail topic created without a goal is drafted
under the adapter's own (`ADAPTER_DEFAULTS` in `lambdas/common/editorial_resolver.py`, #298), which
restates the summary prompt's limits (only what was measured, with the capture dates; no
ridership, journey time, cost, cause, forecast or recommendation). A topic's own goal wins over it.

### What the first tick does

Trigger it by hand rather than wait for the research heartbeat, which runs hourly by default
([How the cadence works](#how-the-cadence-works)). A manual tick ignores the research interval:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline research_tick
```

The tick does three things, in order:

1. It runs the scene search for the city whose turn it is. On a new topic that is Sydney, first
   in the config.
2. It fetches the city's OpenStreetMap network from the Overpass API, since none is stored. The
   fetch starts only if 30 s of the measuring budget remain. It gets the rest of the budget less
   10 s, both Overpass endpoints included: about 70 s at the example's 80 s budget. If it fails,
   the city's `last_error` is `osm` and the city waits for its next turn.
3. If 45 s of the budget remain, the worker measures the newest scene with that network. If the
   fetch was slow, the measurement is deferred to the city's next turn, which reads the stored
   network.

If the scene's coverage is at least 0.6, that is the topic's first observation: a Finding, its
summary written by the model from "First observation. Sydney: served share … %, … stations, … km²
beyond 1 km of a station (coverage … %)" (the area beyond reach printed to one decimal, such as
12.3 km²) and the planner notes, with the figure attached, `subjects` ["sydney"], and no triage
(there is no baseline to check against; a person reads every article anyway). Each other city's
first clear scene is its own first observation, reported once in the same way ("First
observation. Melbourne: …"), so each city's day has an article to start with; after that a city is
reported when it changes. A scene under the floor writes no Finding, and the city's next clear one
is its first observation.

All of that takes longer than the CLI's 30 s wait (`topics trigger` polls for a new Finding for
30 s, `_RESEARCH_TICK_TIMEOUT_SECONDS` in `scripts/admin_cli.py`; the tick has 120 s), so the CLI
printing "No new Finding after 30s. This can be a legitimate outcome …" does not mean it failed.
Wait about two minutes, or until `python scripts/admin_cli.py topics findings rail-cities` shows
the new Finding.

Then `topics trigger rail-cities --pipeline daily_cycle` drafts the article **if it is that city's
day** (on Sydney's days, Monday and Friday, for Sydney's first Finding; on any other day the run
says `no_findings` for the day's city, and Sydney's Finding waits for Sydney's day). The
`force_manual_review` hold keeps the article in the review inbox for `admin_cli approve`, whatever
the compliance review says. A daily cycle started before the Finding is written finds
nothing to draft (run it again); on a later day, with other Findings about the city in its window,
it drafts without the new one, which is then lost ([One Finding can still be missed](#how-the-cadence-works)).

### The first-deploy check: one tick per city

Trigger a research tick once per city, a few minutes apart, and once more for each city whose
first tick only fetched its network:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline research_tick
```

Each tick works on the city whose turn it is: one never attempted first, in the config's order
(Sydney, Melbourne, Singapore, Boston). A city whose first network took the time it had to measure
keeps its place and is measured on the next tick
([A large city's first network can take a minute](#a-large-citys-first-network-can-take-a-minute)).
So four to eight ticks cover the four cities.

Then check two things:

- The ops assistant's vision status (or the running state, [Where things land](#where-things-land))
  should show each city with a network fetched.
- `topics findings rail-cities` should show a first-observation Finding for each city that had a
  clear scene, each listing its city in `subjects`.

### A large city's first network can take a minute

Overpass answers a big city's rail query
slowly: Sydney's took 54 s from a workstation on 16 October (5,925 elements, 5.5 MB). So the first
fetch gets the tick's measuring time, not the 30 s a refresh of a stored network gets (#312): the
rest of the budget less 10 s, each Overpass hop reading for as long as that leaves and the query
asking the server for the same (`FIRST_FETCH_MARGIN_SECONDS` and `FIRST_FETCH_TIMEOUT_SECONDS` in
`lambdas/common/adapters/rail_access.py`). Such a fetch leaves too little of the budget to measure,
so **the first tick fetches and the next measures**: the first tick's `vision_site` line says
`deferred` for `time_budget`, and the ops assistant's vision status shows the network's
`fetched_at` from that tick; the city's next turn measures it against the stored network, which
then serves for `osm_ttl_days` (a refresh that fails keeps it). The fetch ends by its deadline even
if Overpass goes silent mid-reply: a reply still arriving then is cut, and the tick records a
timeout.

The example sets `time_budget_seconds` to the cap, 80, which gives the fetch about 70 s. A topic
created before it did, like dev's `trains-sydney`, does not. To fix such a topic:

1. Read its config with `topics get <id>`.
2. Add `"time_budget_seconds": 80` to the whole `adapter_config`.
3. Send all of it back with `topics update <id> --adapter-config-json '...'`. It replaces the
   config: see [Changing `reach_m` or a polygon](#changing-reach_m-or-a-polygon).

A failed first fetch is tried again on the city's next turn, with no 6-hour wait: nothing is
measured without a network.

If the status keeps saying `osm` with "OpenStreetMap timed out" at the 80 s budget (Overpass took
39 to 75 s to start answering for Sydney on 16 October, so a busy hour can miss), fetch the
networks out of band:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline network_prefetch
```

It runs the research tick's function in a mode that only fetches each city's network, in the
config's order, with up to 90 s per Overpass reply and 100 s in all, and stores each in its own
object, `vision/<topic>/<site>/network.json` in the content bucket: the same object the hourly tick
reads and writes, so the next tick that reaches the city measures with it. It writes no Finding, no
running state and no research time, so the CLI does not wait: each city's result is a
`network_prefetch` line in the research-tick log (`stored`, `failed` with the Overpass error, or
`deferred` when the time ran out before the next city): a slow city can leave too little of the
100 s for the next one. A second prefetch starts again at the first city (it skips no stored
network, however fresh), so it can defer the same cities again; seed those with ordinary research
ticks ([RailVisionSeeding.md §4](RailVisionSeeding.md#4-seeding-runsheet-dev)). When a city's
network goes stale (`osm_ttl_days`, 30 by default) and the tick's 30 s refresh keeps failing, the
old network is still measured against; run the prefetch again to replace it. If even the prefetch fails with a timeout, the query
is too big for Overpass to answer: shrink the polygon (it starts a new baseline, and a new network
is fetched for it).

### How the cadence works

Like every topic, `rail-cities` has two schedules. The research
heartbeat (`rate(1 hour)` by default) asks whether the topic is due (its `research_interval_hours`,
else the pipeline-wide interval) and, when it is, the adapter takes the cities in order (the one
never attempted first, else the one attempted longest ago) and each tick looks at the cities in that
order until it has measured one new scene (`max_sites_per_tick` 1 caps measured scenes, not
cities looked at). For each it searches Earth Search for the newest scene over the whole city in
the last 20 days. Sentinel-2 passes every few days, so most visits find a scene the city already
has: nothing is measured, no model is called, the tick may instead refresh a network past its
30-day TTL, and it goes on to the next city. A new scene is measured; only a material change (or
the first observation) makes a Finding, and the Finding lists the city it reports in `subjects`.
The tick never reads the rota: with an hourly heartbeat every city is looked at about every hour,
and at least one is measured when it has a new scene, whatever the day, so its Findings are
waiting when its day comes.

The daily cycle runs on `daily_cadence` in `daily_timezone`, every day after 09:00
Sydney time, at the minute its stagger slot gives it (`topics schedule` shows it), not at the
example's 09:05. At the start of each run it asks the rota whose day it is, on the
calendar of `rotation_timezone` (Sydney's, so "Tuesday" is Tuesday in Sydney): Sydney on Monday and
Friday, Melbourne on Tuesday and Saturday, Singapore on Wednesday and Sunday, Boston on Thursday.
The run writes about that city alone. It drafts the Findings that list the city in `subjects`,
made since the topic's last article **on that city** (`last_article_by_subject` on the topic item,
beside `last_article_at`). It reaches back at most `findings_window_hours`
(`lambdas/common/findings_window.py`; 24 hours when it is not set, at most 336). The day's mandate
in its prompts names the city.

When there is no new Finding about the day's city:

- A topic with an `editorial_goal_rotation`, like the example, revisits the city's current set:
  the Findings the city's last article on new Findings was drafted from, while they are unexpired.
  It drafts them under the next goal not yet used on them, and writes and stamps an article as
  usual (`revisit=True` in the log).
- Such a topic stops instead, before any model call (`no_findings`, naming the city), and stamps
  nothing, in three cases: the city has no current set yet (no article on it so far), every goal
  has been used on that set, or its Findings have expired.
- A topic without a goal rotation stops the same way at once, with `no_findings`.

Either way the run never borrows another city's Findings. A Finding that reports two cities (a
tick that measured two) is drafted on each one's day. The log says
`daily_cycle_handler: day_subject=<city> topic_id=rail-cities`, and the held article keeps the
city as its `subject`.

The window must cover the longest gap between two of a city's days, plus a day for a clock change
or a late run: 24 hours a day of it. The two-day cities are at most 4 days apart (120 hours would
do), but Boston's Thursday is 7 days from the next, so the example sets 192 hours, 8 days. Four
cities cannot all have two of the seven days. A list rota (`"rotation": ["sydney", "melbourne",
"singapore", "boston"]`) gives each one day in four and needs 120 hours, but no city has a fixed
weekday.

The window never reaches back past the city's own last article, so a run on new Findings never
drafts one that an earlier article on the city used. The daily cycle sees to that: it stamps the
city and the topic with the run's start time on every article it writes, published or held for
review (every rail article is held).

A revisit (above) drafts Findings again on purpose. It reads the city's current set back by its
oldest and newest `captured_at`, not by the window. It drafts the set once under each goal not yet
used on it, never twice under the same one (`goal_rotation_state` on the topic item,
`lambdas/common/goal_rotation.py`).

One Finding can still be missed. The research tick dates a Finding (`captured_at`) when its fetch
starts, at the start of the tick, and writes it at the tick's end. So a Finding whose tick is still
running when the daily cycle starts is not there for that run. Once that run has written an article
on its city, the Finding is dated before that city's next window starts. That predates PR K and is
a code follow-up ([vision.md §10](enhancements/archive/vision.md#10-still-open)).

Findings expire 14 days after they are made (`FINDING_TTL_DAYS` in
`lambdas/research_tick_handler.py`), which is why the window is capped at 336 hours. Daylight
saving does not matter: the rota reads Sydney's calendar, and the window starts at the city's last
article whatever the gap.

Re-Write from the review inbox rebuilds a rail article's sources from the Findings of the article's
own city in the same window. It looks back `findings_window_hours` from the article's creation
(`_findings_for` in `lambdas/common/rewrite.py`). If none are left (a city since removed), it falls
back to the whole window and says so in its log.

Rail Findings are few (the first observation, then material changes). But with the example's four
goals, each new set of a city's Findings is written up on up to four of that city's days: once
when it is new, then revisited under each goal not yet used on it. That goes on until the goals
run out or the Findings expire (14 days, so Boston, with one day a week, usually gets two). After
that the city's days draft nothing until research makes a new Finding about it.

### When a run writes nothing

A run that writes nothing stamps nothing. That covers three cases:

- **It found no Finding about the day's city.** This loses nothing: its window was empty, and the
  city's next run reaches back past it.
- **It had no usable angle.**
- **It ended in an error.**

An error the handler catches is quiet. The daily cycle returns `"status": "error"`, so Step
Functions sees a successful invoke and does not retry it, and nothing reaches the DLQ or an alarm.
The error is in the execution's output, not in the function's log (the handler's top-level guard
prints nothing, a code follow-up in [vision.md §10](enhancements/archive/vision.md#10-still-open)).
The sign is a city's day with a new Finding but no new item in the review inbox.

A broken rota (a slot that is not a site, a site with no day, a zone that is not one) is such an
error on every day. Every research tick fails on it too: the research-tick log shows
`unhandled exception` with the `ConfigError`, and the ops assistant's vision status shows it as
the topic's config error.

A timeout (the function's `timeout` is 300 s) or a crash is not caught. The state machine retries
it twice and then sends it to the DLQ (`admin_cli failed-executions list`), so it is not silent.

Because nothing is stamped, the city's next day still starts at its last article, but never more
than 192 hours back. After a run with no usable angle or an error:

- A two-day city still gets everything on its next day (its days are 3 and 4 days apart, so its
  last article is at most 7 days old).
- For Boston, the once-a-week city, or after two such days in a row, Findings older than 192 hours
  drop out: for Boston that is about six of the week's seven days, and for a two-day city after two
  missed days, two or three days.

After a run that had no usable angle or ended in an error, run the daily cycle again by hand the
same day, before midnight Sydney time: a run writes about the day's city, so the next day's run
would be about another city.

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline daily_cycle
```

It reaches back to the city's last article, up to 192 hours. Leave out `--force`: it ignores both
stamps and drafts the city's whole 192-hour window, so it would draft again the Findings an earlier
article on the city already used (it still keeps to the day's city).

### Where things land

The content bucket, `<prefix>-<env>-content`, holds:

- **The figures** as the worker drew them: the rail map `vision/<topic>/<site>/<scene>.png` (for
  example `vision/rail-cities/sydney/S2A_56HLH_20240105_0_L2A.png`) and, once the city's network
  carries its stops, the transit-reach map `vision/<topic>/<site>/<scene>-modes.png`.
- **Each city's network**, its only copy, `vision/<topic>/<site>/network.json`. The tick's fetches
  and `network_prefetch` both write it. It is kept outside `snapshots/`, whose objects the bucket
  expires after 21 days.
- **The running state**, `snapshots/<topic>/running-state.json`, rewritten every tick.
- **Each Finding's own snapshot**, `snapshots/<topic>/<captured_at>.json`.

The running state holds `first_observed_at` for the topic and, per city:

- the history (the newest entry with its station, flag, suggestion and desert lists);
- what it knows of the network without holding it (`network_key`, `network_fetched_at`, the
  polygon's `network_site_key` and `network_counts`);
- any `last_error`, `network_error` or `failed_scene`;
- the city's own `first_observed_at`.

When the article is published, each figure is copied to the site bucket, `<prefix>-<env>-site`, as
`articles/figures/<article>/<n>.png`, and the page shows it under the credits with its caption.

```bash
aws s3 cp s3://<prefix>-dev-content/snapshots/rail-cities/running-state.json - | python -m json.tool | less
aws s3 cp s3://<prefix>-dev-content/vision/rail-cities/sydney/network.json - | python -c 'import json, sys; n = json.load(sys.stdin); print(n["fetched_at"], n["counts"], n.get("bytes"))'
```

### Reading the agent's trail

From the second clear scene of a city on, a numeric change goes to
the triage agent before it can become a Finding. `topics findings rail-cities` prints the latest
Finding: its `summary` (the model wrote it from a diff that, for a change the agent judged real,
quoted the agent's reason as `the vision agent's note (not a measurement): "…"`), `figures`,
`planner_notes`, `subjects` and `raw_snapshot_s3_key`. That snapshot (and the running state) holds
the whole trail under `triage`, keyed by site id: `verdict`, `reason`, `tool_calls` (each tool's
`name`, its `input` and whether it was `ok`), `steps` (each tool's code-derived result), `stop`,
`model_id`, the tokens and `model_calls`; the newest history entry repeats the verdict and reason.
A change the agent judged an artefact writes no Finding, so its trail is in the site's decision
ring in the running state (the last 12 decisions) and in the logs (the `vision_triage` line, 90
days: [Evidence](#evidence-what-the-pipeline-measured-and-decided)). The review evidence, which the fresh-data
review checks the draft against, carries each city's measured numbers and planner notes, not the
trail: to see why a change was or was not reported, read `triage`.

### Adding a city

A new city is a new site in the topic's `sites` and a day for it in the rota. Every site needs a
day, and a rota day that names no site is refused.

1. Read the config with `topics get rail-cities`.
2. Add the site: a new `id` (at most 64 letters, digits, `_` or `-`), its name and its box.
3. Give it a day. Take one from a city that has two (for Friday, `"FRI": "perth"`), or change the
   rota to a list cycled one a day.
4. Send the whole config back with `topics update rail-cities --adapter-config-json '...'`. It
   replaces the config: see [Changing `reach_m` or a polygon](#changing-reach_m-or-a-polygon).
5. Keep `findings_window_hours` at least the longest gap between a city's days plus a day
   (`topics update rail-cities --findings-window-hours 192`). 192 hours covers any city that has
   at least one day a week.

A topic may hold up to eight cities (`MAX_RAIL_SITES`), measured one per tick in turn. Each city's
network is in its own object, so the running state holds only the histories (about 7 MB at eight
cities; `MAX_RAIL_SITES`'s comment in `lambdas/common/adapters/rail_access.py` has the
measurements).

The new city's box must sit inside one Sentinel-2 tile, because the scene search keeps only scenes
whose footprint holds the whole site. A tile is a 100 km square of the UTM grid and 9.8 km more to
the east and south (Sydney's 56HLH runs from easting 300,000 to 409,800 and northing 6,190,240 to
6,300,040), so a box whose corners fall in one 100 km square fits. Check it with
`lambdas/vision/geo.py` from the repository root (any Python 3.11 or later; `geo.py` needs only the
standard library), putting your box's west, south, east and north in place of Sydney's:

```bash
python -c 'import sys, math; sys.path.insert(0, "lambdas"); from vision import geo; w, s, e, n = 150.86, -34.16, 151.56, -33.58; z = int((w + e) / 2 + 180) // 6 + 1; c = [geo.lonlat_to_utm(x, y, z, s < 0) for x in (w, e) for y in (s, n)]; xs, ys = [p[0] for p in c], [p[1] for p in c]; print("zone", z, "S" if s < 0 else "N", "| 100 km squares", sorted({(int(x // 1e5), int(y // 1e5)) for x, y in c}), "| margin km", round(min(min(xs) % 1e5, 1e5 - max(xs) % 1e5, min(ys) % 1e5, 1e5 - max(ys) % 1e5) / 1e3, 1), "| window px", math.ceil(max(xs) / 20) - math.floor(min(xs) / 20) + 32, "x", math.ceil(max(ys) / 20) - math.floor(min(ys) / 20) + 32)'
```

For Sydney it prints `zone 56 S | 100 km squares [(3, 62)] | margin km 1.4 | window px 3327 x 3306`.
Three things to read: **one** square in the list (two means the box straddles a grid line: move
or shrink it); a margin of a kilometre or more from the square's nearest edge (the four boxes have
1.0 to 3.0); and a window of at most 3,584 px a side (the worker's `RAIL_MAX_WINDOW_PX`, 71.7 km
at 20 m with the 16 px pad), or the worker refuses the city as `window_too_large`. A scene refused
as `window_too_large`, `site_outside_scene` or `bad_request` is recorded in the running state as
`failed_scene` (`id`, `code`, `at`, `site_key`, `params`) and not sent again until a newer scene
appears, 6 hours pass, or the site's polygon or parameters change, so a corrected box is sent on
the next tick; `unreadable_scene` and any other failure are retried on the next tick. Some passes
cover only part of a tile; the search skips those and waits for one that holds the whole city.

### Changing `reach_m` or a polygon

`reach_m` is the walking reach of a station in a straight line, 200 to 5000 m. Keep 1000 (about a
12-minute walk) unless there is a reason: the served share and the deserts are only comparable
between cities at the same reach, and a larger reach describes stations people drive or ride a
bus to. Changing it, or a polygon, on a running topic starts a new baseline: the next scene
reports "baseline still building" rather than a change.

Either is changed with `topics update <id> --adapter-config-json '...'`. It replaces the whole
`adapter_config`, not only the keys it names, so:

1. Read the config with `topics get <id>`.
2. Edit it, keeping each city's `sites` entry with the same `id` (and polygon, unless that is what
   changes) and the `rotation`.
3. Send all of it back.

What goes wrong otherwise:

- Sent with only the changed key, the config loses `sites`, and every tick fails until it is whole
  again.
- Any other key left out falls back to its default. A lost `rotation` makes the topic write about
  every city every day.
- A city under a new `id` starts with no history.

### Moving from city topics to `rail-cities`

A topic made before the day-subject round, like dev's
`trains-sydney` (one city, no rota), keeps working after the deploy: it has no rota, so its daily
cycle drafts every Finding as before, and its first tick moves the network its running state
carried to the city's own object (`vision/trains-sydney/sydney/network.json`) without fetching it
again. To change to one rotating topic, start it fresh rather than migrate state
([dailysubject_hook.md §4.4](enhancements/archive/dailysubject_hook.md#44-migrating-the-four-deployed-topics)):
create `rail-cities` ([Create the topic](#create-the-topic)), run [the first-deploy check](#the-first-deploy-check-one-tick-per-city), and once each city has
had its first tick, pause the old topic, which stops its ticks and daily runs and keeps its
published articles under its own id:

```bash
python scripts/admin_cli.py topics pause trains-sydney
```

(`topics delete trains-sydney` removes it for good.) Each city in `rail-cities` starts with a first
observation again: the baseline needs one earlier clear scene (`min_baseline` 1), so each city is
back to reporting changes after its second clear scene, a few days. Pause any other city topics
the same way.

### After deploying the stops layer

The stops layer is every bus and tram stop in the network, the stop measures and
the transit-reach map. A network stored before it carries no `stops`, so the city's Findings keep
reporting `no_stops_layer`, with no stop measures and no second map, until the network is
refreshed at its TTL (30 days) or replaced. Once the deploy is green, run the prefetch once:

```bash
python scripts/admin_cli.py topics trigger rail-cities --pipeline network_prefetch
```

Each city's `network_prefetch` line carries the Overpass body's size as `bytes` beside its
`counts` (which gain `stops` and `stops_by_mode`). Read it against the client's 20 MB cap
(`MAX_RESPONSE_BYTES` in `lambdas/common/osm.py`): a big city's bus stops add a few MB to its rail
query (Sydney's rail reply alone was 5.5 MB), and a city near the cap is a polygon to shrink.

A `deferred` city keeps the network it had. Its research ticks measure against that network
(still `no_stops_layer`) and fetch it again only once it is past `osm_ttl_days`, because ordinary
ticks seed only a city that has no network yet
([A large city's first network can take a minute](#a-large-citys-first-network-can-take-a-minute)).
A second prefetch starts at the first city again, so it can defer the same city.

Then push the goals. Their text changed with the layer: every simulated link theoretical with what
it crosses, minutes as walking time at 80 m a minute, a constructive register, and the fourth
rotating goal, `transit`. Write them out from
[RailVisionSeeding.md §3 and §7](RailVisionSeeding.md#3-the-editorial-goal) and send both in one
update, which changes nothing else of the topic:

```bash
python scripts/admin_cli.py topics update rail-cities --goal-rotation-json "$(cat rail_goal_rotation.json)" --editorial-goals-json "$(cat goals.json)"
```

Until the rows are pushed, the adapter's own default mandate and drafting guidance already carry
the framing. The benchmark's committed networks (`scripts/vision_benchmark_networks/*.json`)
predate the layer too and keep working without it (their runs report `no_stops_layer`); refetch
one with `python scripts/vision_benchmark.py fetch-network --scene <name>` on a machine that can
reach Overpass ([scripts/README.md](../scripts/README.md)), which prints the file's counts and
`bytes`.

## Evidence: what the pipeline measured and decided

Every step the vision pipeline takes leaves one JSON line in the logs, and every decision an
entry in the site's decision ring in the running state ([the evidence
trail](enhancements/archive/vision-evidence.md)). The lines are read in the CloudWatch console (Logs →
Logs Insights, pick the Region, the log group and a window) with the fixed queries below, which
filter by field equality: a query on one topic id never matches another that it prefixes (a
`rail-sydney` beside `rail-sydney-demo`). The operator's assistant and Alexa+ read the same lines
with `vision_evidence` ([Asking the assistant](enhancements/archive/vision-evidence.md#asking-the-assistant)).

### The queries

- (a), (b), (c), (c2) and (f) run on `/aws/lambda/<prefix>-<env>-research-tick` in the home
  Region;
- (d) and (g) on `/aws/lambda/<prefix>-<env>-vision-worker` in `us-west-2` (the vision Region);
- (e) on `/aws/lambda/<prefix>-<env>-admin-api` in the home Region.

```
(a) A topic's decisions (one topic, by equality: rail-cities never matches rail-sydney-demo)
fields @timestamp, site_id, scene_id, code, moved, network_changed, verdict, reported, coverage, tick, run
| filter evidence = "bloggerbear/1" and event = "vision_decision" and topic_id = "rail-cities"
| sort @timestamp desc | limit 50

(b) What the agent did
fields @timestamp, topic_id, site_id, scene_id, verdict, stop, tools, look_again, steps_total, elapsed_ms, cost_usd
| filter evidence = "bloggerbear/1" and event = "vision_triage"
| sort @timestamp desc | limit 50

(c) One tick, end to end, with Lambda's own lines (paste its tick id)
fields @timestamp, event, run, site_id, outcome, code, stored, worker_request_ids, worker_request_id, @message
| filter tick = "<tick id>" or @requestId = "<tick id>"
| sort @timestamp asc

(c2) The agent's whole trail for that tick (read the JSON; do not name flattened step fields)
fields @timestamp, @message
| filter evidence = "bloggerbear/1" and event = "vision_triage" and tick = "<tick id>"

(d) The worker, by build and CPU (us-west-2; evaluation and benchmark calls left out)
filter evidence = "bloggerbear/1" and event = "vision_measure" and site_id not like /^(eval|bench)-/
| stats count(*) as runs, pct(total_ms, 50) as p50_ms, pct(total_ms, 95) as p95_ms,
        max(total_ms) as max_ms, max(rss_peak_mb) as peak_mb by backend, build, cpu, ok, code

(e) What people decided (the admin-api group)
fields @timestamp, event, topic_id, article_id, queue_id, decision, from_status, action, config_keys
| filter evidence = "bloggerbear/1" and event in ["moderation_decision", "topic_changed"]
| sort @timestamp desc

(f) Ticks by outcome, and the attempts that timed out (two queries on the research-tick group)
filter evidence = "bloggerbear/1" and event = "research_tick"
| stats count(*) by topic_id, outcome, stored

filter @type = "REPORT"
| stats count(*) as runs, sum(strcontains(@message, "Status: timeout")) as timeouts

(g) The worker's runs as Lambda saw them (us-west-2): the ones killed before they could log
filter @type = "REPORT"
| stats count(*) as runs, sum(strcontains(@message, "Status: timeout")) as timeouts,
        max(@maxMemoryUsed) as max_memory_bytes, pct(@duration, 95) as p95_ms
```

### Reading a line

`event` says the step, `tick` the research tick's Lambda request id and `run`
the attempt (a tick Lambda retried after a timeout keeps its request id; `run` is new each time).
A decision is **kept** when its tick's `research_tick` line says `stored: true`; a tick that
failed after deciding says `stored: false`, a tick that timed out has no `research_tick` line at
all, and in both cases the scene is measured and decided again on the next tick. A decision whose
tick's line carries `finding` equal to its `at` wrote a Finding. The worker's lines carry no topic
id: join them to a tick through `worker_request_id` (c), or read them by site id (d), which is why
the demo topic's site is `demo-sydney`, not `sydney`.

### The first-deploy check of the evidence lines

Once, on dev, right after the deploy that carries this:

1. Force a tick (`python scripts/admin_cli.py topics trigger rail-cities --pipeline
   research_tick`) and run (c) with its tick id (the `START RequestId:` line of the invocation,
   or the `tick` field of any evidence line in the window). Every line of the tick must come
   back with Lambda's own `START`, `END` and `REPORT` lines.
2. Take a `worker_request_id` from the `vision_site` line. In `us-west-2`, on the worker's group,
   run `filter @requestId = "<id>" or request_id = "<id>"`: the worker's `vision_measure` line
   and its `REPORT` line must both come back.
3. Check that (a) discovers its fields (the console offers `site_id`, `code`, `moved` in the
   fields pane), and that one line of each event parses. Paste one line into the parser, from
   the repository root:

   ```bash
   echo '<one evidence line, as the console shows it>' | python -c "import sys; sys.path.insert(0, 'lambdas'); from common import evidence; print(evidence.parse(sys.stdin.readline()))"
   ```

   The parser prints the line's fields as a dict, or `None` for a line it does not take. In the
   same window, confirm that a multi-line print (any `Traceback`) came through as several
   CloudWatch events, one per line, which is what the Text log format does and what the
   evidence writer's single-line ASCII relies on.
4. **The retry key.** Look in the research-tick group for two `START RequestId: <id>` lines with
   the same id (a tick Lambda retried after a timeout). If a retried attempt ever shows a new id,
   `run` is still the attempt key and nothing changes; record which it is here.

### The demo topic's two-tick procedure

A city's second clear scene can come days after the
deploy, and the agent runs only when the numbers move. The demo topic makes the first live
triage happen on a known day: thresholds of 0 send its second clear scene to the agent whatever
the numbers did. The report and the video disclose it as such, beside its thresholds, and its
articles are rejected in the inbox, never published.

1. **Pick two scenes.** List the Sydney tile's (56HLH) items of the last 30 days with each
   item's STAC cloud cover and its coverage over the Sydney polygon. Choose **A**, an older item
   with site coverage at least 0.95; **B**, the newest item with STAC cloud cover at most 40 that
   covers the site whole, with site coverage at least 0.6 (and B is not A); and a cloud cover
   **X** with `A.cloud <= X <` the cloud cover of every item newer than A that covers the site
   whole, so that the newest item at cloud cover X or less is A (the scene search sorts newest
   first and keeps the first that covers the site whole). If no such pair exists in the last 30
   days, use the evaluation's replay trace and say so.
2. **Create the topic** `rail-sydney-demo`, named "Rail access: Sydney (demo)" (so every spoken
   count says which topic it is), with the Sydney polygon but **site id `demo-sydney`** (so the
   worker's lines, which carry no topic id, are told apart from the city's),
   `served_share_threshold: 0`, `desert_relative_threshold: 0`, `min_baseline: 1`,
   `max_cloud_cover: X`, `lookback_days` large enough to reach A, and Sydney's cadence.
3. **Tick 1.** Fetch the demo topic's network first, then force a tick.
   - The demo topic has no network of its own yet. Each network is stored per topic and site
     (`vision/rail-sydney-demo/demo-sydney/network.json`), so `rail-cities`' Sydney network does
     not serve it, and Sydney's first fetch takes most of a tick's budget.
   - Fetch it out of band: `python scripts/admin_cli.py topics trigger rail-sydney-demo --pipeline
     network_prefetch`. Wait for its `network_prefetch rail-sydney-demo/demo-sydney: stored` line
     in the research-tick log, and run it again if the line says `failed`. Without the prefetch,
     the first tick only fetches the network: its `vision_site` line says `deferred` for
     `time_budget` (or `none` with `osm`, if Overpass ran out of time), and A is measured only on
     a later tick.
   - Force a tick (`topics trigger rail-sydney-demo --pipeline research_tick`). A is measured; it
     is the topic's first observation (a Finding, then a held article: reject it).
   - Before you change the config for tick 2, check that the tick's `vision_site` line says
     `measured` with A's `scene_id`. If B is measured first, it becomes the first observation and
     never goes to the agent.
4. **Tick 2.** `python scripts/admin_cli.py topics update rail-sydney-demo --adapter-config-json
   '<the same config with max_cloud_cover 40 and lookback_days 20>'` (`adapter_config` is a
   whole-block replace: send all of it), then force a tick. B is not in the site's history, so
   it is measured; with both thresholds at 0 the verdict is material and goes to the agent.
5. **Check** with (b) and (c), and with the assistant's `vision_evidence` once it is live.
6. **Afterwards**, once its articles are rejected and the trace is captured, pause the demo topic
   (`topics pause rail-sydney-demo`): every later clear scene would otherwise cost a worker run
   and a Bedrock triage, measuring Sydney's scenes a second time. Delete it (`topics delete`) only
   after the last export.

## Turning it off

Set `VISION_ENABLED` to `false` (any value other than `true` in any
letter case), or remove it (either is read as off; on the `production` environment, removing
it falls back to the repository's value, so set it to `false` there instead), and run a
deploy. The next apply removes the worker, its bucket, its log group and the invoke right;
topics keep their state and their history, and record `last_error` on each site until the
worker is back. Disable or delete the topics if you do not want those errors.
