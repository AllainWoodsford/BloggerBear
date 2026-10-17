# The day-subject hook: one topic that rotates through the cities it is configured to watch

**Status:** S1 and S2 built (#320, #323), with S1's review leftovers in S1b (#324), S2's review
fixes in #327 and both reviews' nits in S3 · **Date:** 2026-10-15 · **Pick up after:** the
assistant's vision round (PR M, PR N) and before the competition report (Phase 3), or after the
deadline ·
**Builds on:** [rail-access-monitor.md](rail-access-monitor.md), [vision.md](vision.md) §5,
`common/editorial_goals.py` (the crypto feed's day goals), PR K (`findings_window_hours`, #297)

> **The ask.** "My problem with the vision topic is it's set up to target one city, so I would
> have to spawn 4 topics. Why can't I have one topic that rotates daily to write about the cities
> it's configured to grab, like crypto has market days or altcoin days?"

This document says what one rotating topic needs, why it was not built that way the first
time, and how to build it in two PRs without changing any other topic's behaviour. Every file
reference is at `dev` 7552942 (the vision integration branch, #285, merged); re-grep before
editing, since PR M and PR N move some of these lines.

---

## 1. Why it is four topics today

The crypto feed varies its subject day by day with a trick worth copying: **the day's goal is a
pure function of the date**. `editorial_goals.goal_for_date` seeds a random draw with the UTC
date, so the research tick (which fetches what the goal needs, hourly) and the daily cycle (which
writes the article) compute the same goal without any stored state, and the daily cycle keeps only
the Findings captured on that day (`daily_cycle_handler._select_goal_and_findings`, line 583).
Only the crypto adapter has goals: `resolve_goal_for_topic` returns `None` for every other adapter
(`editorial_goals.py:134`), so nothing else in the pipeline knows the idea.

Nothing in the rail work generalised that. Three things pushed it towards one topic per city:

1. **`MAX_RAIL_SITES = 3`** (`common/adapters/rail_access.py:167`). The research tick loads the
   topic's whole running state every hour (`research_tick_handler._load_running_state`), and each
   city's OpenStreetMap network is about 3 MB of JSON in that state (the module docstring, lines
   70-79, and `common/osm.py`'s caps). Four cities did not fit the 512 MB tick comfortably, so the
   cap stayed at three.
2. **"One city a day" was mapped onto per-topic schedules.** Each city topic has its own
   `daily_cadence` cron (`scripts/rail_topics.example.json`: Sydney Mon/Fri, Melbourne Tue/Sat,
   Singapore Wed/Sun, Boston Thu) and a 192-hour `findings_window_hours`.
3. **`last_article_at` is one stamp per topic.** The daily cycle never reads back past the
   topic's previous article (`_window_start`, line 227). One shared topic writing about Sydney on
   Monday would have cut off Melbourne's and Boston's Findings made before Monday; that is the
   bug PR K fixed for the rotation *between* topics, and it reappears *inside* one topic.

So four topics was the shape that needed no pipeline change. It works, it is deployed, and the
measurements, figures and planner notes are the same either way. What it costs the owner is four
topics to create and four to read, and a weaker story: "four topics" rather than "one topic, one
adapter, N subjects".

## 2. The design in one paragraph

A **day subject** is to a multi-city topic what the day goal is to crypto: a subject chosen from
the topic's own configuration as a function of the date, agreed on by the research tick and the
daily cycle without stored state. An adapter may expose it through one hook; a Finding records
which subjects it reports; the daily cycle keeps only today's subject's Findings, folds the
subject's name into the editorial mandate, and remembers the last article **per subject**. The
rail adapter uses it with a fixed weekly rota (not a random draw: the owner wants to know which
city is Tuesday's). The cap on cities per topic is lifted by keeping each city's network out of
the running state. Every topic without the hook behaves byte-for-byte as before, including crypto.

## 3. PR S1: the generic hook (pipeline, rule 5)

### 3.1 `Adapter.day_subject` (`common/adapters/base.py`)

```python
@dataclass(frozen=True)
class DaySubject:
    key: str        # stable id, e.g. a site id: "sydney"
    label: str      # how the subject is named in prompts and logs: "Sydney"
    mandate: str    # the day's editorial mandate, code-written, with the label folded in
    style: str      # the article style line, like editorial_goals.ARTICLE_STYLES

def day_subject(self, topic_config: dict, day: date) -> DaySubject | None:
    """Today's subject for a topic that rotates through several, or None (the default)."""
    return None

def subjects(self, new_state: dict) -> list[str]:
    """The subject keys this observation reports (default: none)."""
    return []
```

- `day_subject` is pure: config and date in, subject out. The tick and the daily cycle call it
  independently and must agree, exactly as `goal_for_date` is used today.
- `subjects` is the Finding-side half: the rail adapter returns the site ids its summary reports,
  the same list `figures` and `planner_notes` already follow (`_reported_scenes`,
  `rail_access.py:545`).
- `mandate` and `style` are **data from the adapter**, never branches in the daily cycle. For
  rail they are the templates' editorial goal with the city's name substituted (the text now in
  `editorial_resolver.ADAPTER_DEFAULTS["rail_access"]`, plus "for Sydney").

### 3.2 The Finding records its subjects (`research_tick_handler.py:320-345`, `dynamo.put_finding`)

- The tick calls `adapter.subjects(new_state)` beside `figures` and `planner_notes`, cleans the
  result (strings, `SITE_ID`-shaped, at most `MAX_SUBJECTS_PER_FINDING = 10`, de-duplicated) and
  passes it to `put_finding(..., subjects=...)`, stored only when non-empty, like `figures`. A
  hook that raises costs the subjects, never the Finding (the `_planner_notes` pattern, line 137).
- `list_recent_findings` is unchanged; the field rides along.

### 3.3 The daily cycle picks the day's subject (`daily_cycle_handler.py`)

Generalise `_select_goal_and_findings` (line 583) into `_select_subject_and_findings`:

1. Resolve the crypto goal as today (unchanged path, unchanged bytes).
2. Otherwise `subject = registry_adapter(topic).day_subject(topic["adapter_config"], today)`.
   If `None`, return `(None, findings)` as now.
3. Keep `[f for f in findings if subject.key in (f.get("subjects") or [])]`.
4. If nothing is left: return `no_findings` for this run **without** stamping `last_article_at`
   (the "a day with no new Finding stops before any model call" rule the runsheet already
   promises). Do not borrow crypto's "follow the newest Finding's day" fallback: a Monday article
   about Boston because Sydney had nothing is exactly what the owner does not want.
5. The prompts: `_ideate` (line 735) and `_draft_article` index `EDITORIAL_MANDATES[goal]` and
   `ARTICLE_STYLES[goal]` by enum. Give them one small seam: a `goal` argument that is either an
   `EditorialGoal` (looked up as now) or a `DaySubject` (its own `mandate` and `style`). A test
   asserts the crypto prompts are byte-identical before and after.
6. `resolve_editorial_goals(topic, daily_goal)` (`editorial_resolver.py:92`): a `DaySubject` is
   inside the adapter's domain, so the adapter default is kept (unlike `MARKET_NEWS`).
7. Log one line: `daily_cycle_handler: day_subject=sydney topic_id=...`, like the goal line (291).
8. The article stores `subject` (the key) beside `editorial_goal`, so Re-Write and the ops
   assistant can see what it was about.

### 3.4 The last article, per subject (`_window_start`, `dynamo.set_topic_last_article_at`)

The one real design change. Today: `floor = run_started - window; start = max(floor,
last_article_at)`. With subjects:

- The topic item gains `last_article_by_subject: {"<key>": "<ISO UTC>"}` (a map; DynamoDB stores
  it as `M`). `last_article_at` stays as the topic-wide stamp, so every existing display
  (`cli_guide`, `topics_overview`, `pipeline_health`) keeps working.
- `_window_start(topic, run_started, force, subject_key=None)`: when a subject is in play, the
  bound is `last_article_by_subject.get(key)`; absent means "never written about this subject",
  so the window alone bounds it. `force` ignores both stamps, as now.
- `_record_article_written(topic_id, run_started, subject_key=None)`: one `UpdateExpression`
  that sets `last_article_at` and, when a key is given, `last_article_by_subject.#k`. DynamoDB
  refuses to set a map and a key inside it in one expression, so it is two sequential writes (no
  transaction needed). The first is a conditional overwrite, `SET last_article_by_subject = :empty`
  only when the topic exists and the field is missing or not a map (`attribute_not_exists(...) OR
  NOT attribute_type(..., :map)`); a map already there fails the condition and is left alone, and a
  field that was not a map (a hand edit) is replaced and logged. The second sets the key and
  `last_article_at` together (as built: `dynamo.set_topic_last_article_at`, 3.10).
- **A known gap, not widened here:** `admin_api._update_topic` reads the topic and writes it
  back whole (`put_item`), so an edit that lands between the stamp and the read restores the old
  stamps (vision.md §10). With a map it is the same race on more fields. Fixing it is a
  conditional update in `_update_topic`; list it as a follow-up unless S1 wants to take it.
- `findings_window_hours` for a rotating topic must cover the rota: `24 × (longest gap in days)
  + 24`. For a 4-city weekly rota with two slots each it is 96 h or 120 h; for one slot each
  (Boston today), 192 h. The rotation test (3.9) asserts it.

### 3.5 Re-Write (`common/rewrite.py:255`)

`_findings_for(article, topic)` keeps only Findings whose `subjects` contain the article's
`subject` when the article has one; otherwise unchanged. The fresh-data review and the
compliance review then see the city's own research, not its neighbours'.

### 3.6 The research tick side (optional, small)

`vision_sites.order_sites` (line 310) rotates one site per tick by `last_attempt_at`. With N
sites and an hourly heartbeat every city is attempted within N hours, and Sentinel-2 passes every
few days, so Findings for each city accumulate inside the window regardless of the rota. S1 does
**not** need the tick to know the subject. If an owner wants today's city measured first (a fresh
scene on its own day), a later option is `order_sites(..., first=subject.key)` when the config
sets `measure_subject_first: true`. Leave it out of S1.

### 3.7 Configuration (`adapter_config`, validated in the adapter's `parse_config`)

```json
"rotation": {"MON": "sydney", "TUE": "melbourne", "WED": "singapore", "THU": "boston",
             "FRI": "sydney", "SAT": "melbourne", "SUN": "singapore"},
"rotation_timezone": "Australia/Sydney"
```

- Keys are weekday names; values are site ids in `sites`. A list form
  (`["sydney", "melbourne", ...]`, cycled by `(date - 1970-01-01).days mod N`) is allowed for
  rotas that are not weekly. Every site in `sites` must appear at least once (a city nobody ever
  writes about is a misconfiguration); a value that is not a site id is a `ConfigError`.
- The day is the **local calendar day** in `rotation_timezone` (default UTC), so "Tuesday" is
  Tuesday in Sydney. The daily cycle's `today` must be computed in that zone; the tick uses the
  same helper. (The topic's `daily_timezone` is the schedule's zone and may differ; do not
  conflate them, but default `rotation_timezone` to `daily_timezone` when unset.)
- The admin API does not validate `adapter_config` beyond "an object" (`admin_api_handler.py:249`)
  and the adapter's `parse_config` runs at tick time, recording a `ConfigError` on the topic's
  `last_error` as today. Leave that as it is; the CLI reference and `configuration.md` document
  the keys.

### 3.8 What must not change

- Any topic whose adapter returns `None` from `day_subject` (every adapter but rail after S2):
  the daily cycle's prompts, Findings, Articles, stamps and return values are byte-identical.
  The regression probe from the integration review (dev vs branch on github-trending,
  hacker_news, web_search and crypto, `PYTHONHASHSEED=0`) is the test.
- The crypto feed keeps `editorial_goals.py` as its implementation; the `DaySubject` seam is
  additive. Migrating crypto onto the hook is possible later and is not part of S1.
- `satellite_vision` is unchanged (one anchorage per topic is its normal shape; it may adopt the
  hook later with no pipeline change).

### 3.9 Tests (S1)

- `test_daily_cycle_handler.py`: a two-subject topic over a fortnight with the real
  `_window_start`, `_select_subject_and_findings` and `_record_article_written` (moto):
  every Finding is drafted exactly once, on its subject's day; a subject with no new Findings
  gives `no_findings` and stamps nothing; `force` keeps the subject filter but ignores the
  stamps; the rota is read in `rotation_timezone` across a clock change; Decimal values from
  DynamoDB; a Finding with no `subjects` field is never drafted by a rotating topic (and still
  is by a plain one).
- `test_rewrite.py`: an article with `subject` sees only its subject's Findings.
- `test_research_tick_handler.py`: `subjects` stored only when non-empty, capped, a raising hook
  loses the subjects and keeps the Finding.
- `test_editorial_goals.py` / prompts: crypto's ideation and draft prompts byte-identical.
- `test_admin_api_handler.py`: `last_article_by_subject` survives an unrelated update; GET
  returns it.
- `test_ops_mcp_*`: `topics_overview` and `cli_guide` ignore the new map (or show it, if PR M's
  overview already lists `last_article_at`; decide then).

### 3.10 S1 as built

S1 was built on the Phase 2 branch after #297 (PR K), #302 (paused topics), #306 (P1's evidence
lines) and #307 (PR N), so some of the references above moved. What differs from 3.1 to 3.9, and
why:

- **The hook takes the moment, not a date:** `day_subject(topic, at)`, where `at` is the daily
  run's start (aware, UTC) and `topic` is the whole Topic item, as `fetch_state` gets it. Which
  local day `at` falls on is the adapter's to work out in its own `rotation_timezone`. A `date`
  argument would have made the pipeline compute the day in a zone it must not know (rule 5). The
  item is needed for `daily_timezone`, the zone's default.
- **One window rule, three readers:** PR N moved `_window_start` into
  `common/findings_window.window_start`, which now takes `subject_key`. The daily cycle passes the
  day's subject. The assistant's `topic_findings` and `GET /topics/{topic_id}/findings` (with
  `admin_cli findings list --subject`) take an optional `subject`, with `next_article` only;
  given alone, it is refused (a fixed sentence, or a 400). Without one, both were unchanged in
  S1; since S1b they read a rotating topic for today's subject (below). The assistant never speaks
  the subject, because it is the operator's text; the result echoes it.
- **Selection:** `_select_subject_and_findings` dispatches. `_select_goal_and_findings`, the
  crypto path, is untouched, and its tests call it. The subject is asked once, at `run_started`,
  after the paused check, so a paused rotating topic stays paused and its rota is not read. It is
  never asked of a topic that has a crypto goal.
- **A broken rota writes nothing:** if the hook raises, or returns something that is not a usable
  `DaySubject` (`common/day_subject.day_subject_error`), the run returns an error before any model
  call. It never falls back to "every subject".
- **The research tally** on a rotating topic's article covers the Findings it was written from,
  not the whole window. Subjects' windows overlap, so a whole-window tally would count a Finding
  once per subject.
- **Stamps:** `dynamo.set_topic_last_article_at(topic_id, ts, subject_key=)` makes the map if it
  is missing, then sets the subject's entry and `last_article_at` together in a second write.
  DynamoDB refuses to set a map and a key inside it in one expression. A day that writes nothing
  stamps nothing.
- **The article** stores `subject`, and the daily result names it. There is no `editorial_goal`
  field on an article to put it beside (3.3.8). A topic that does not rotate stores and returns
  nothing new.
- **The rota (3.7)** is a generic library in `common/day_subject.py`: `parse_rotation` checks
  either the seven weekdays (all seven must be named) or a list cycled from 1970-01-01, that every
  subject has a day, and the zone. It raises ValueError, which the adapter wraps in its
  ConfigError. `rotation_subject` names the subject for the local day. An adapter imports the
  module at its top: the module imports the adapter registry, which imports every adapter, only
  inside `resolve_day_subject`, so the import is not a cycle.
- **Not changed:** no new evidence event or field (a Finding's subject count on the
  `research_tick` line is a follow-up), and `admin_api._update_topic`'s whole-item write (the 3.4
  race, now on one more field, is a follow-up). The 48-Finding cap applied to the window before
  the subject filter in S1; S1b moved it after (below).
- **The operator's view:** `topics_overview`'s single-topic view lists the last article per
  subject. The list view's columns are unchanged.

**S1b (the review's leftovers, after S1 merged mid-review):**

- **The next article, with no subject.** `findings_window.next_article_subject(topic, at)` is the
  one rule for which subject a run writes about: the daily cycle asks it at `run_started`, and
  `topic_findings` and `GET /topics/{topic_id}/findings` ask it at `now` when `next_article` comes
  without a subject. A rotating topic is then read for today's subject (its window, its Findings,
  the subject echoed); every other topic is read byte for byte as before. A hook that raises is
  the tool's error and the route's 500, never the topic-wide window, which no run of a rotating
  topic reads.
- **The cap after the filter.** A rotating topic's readers (the daily cycle, Re-Write, the tool,
  the route) read up to `findings_window.SUBJECT_READ_LIMIT` (4 x 48 = 192), keep the subject's
  Findings, then cap those at 48. Capped first, other subjects' newer Findings pushed a subject's
  older ones out, and its stamp then passed them for good. The daily cycle logs how many of the
  subject's own the cap left out. What 192 guarantees is every Finding of a one-slot rota of up to
  7 subjects at one Finding an hour (its 192 h window), within the read's page cap (5 DynamoDB
  pages of up to 1 MB, so Findings of up to about 26 KB each). A wider window (up to 336 h), a
  longer rota (a list rota may have 62 slots) or larger Findings can hold more than the read
  reaches; the daily cycle and Re-Write then log where the read stopped (the limit or the page cap)
  and how many Findings it held, and the tool and the route give their count as a floor
  (`count_is_at_least`). A topic that does not rotate makes the same Query with the same limit.
- **The hook's output.** `clean_subjects` reads any iterable of keys (a generator, a set in sorted
  order), looking at no more than 1,000 values (`MAX_SUBJECT_VALUES_EXAMINED`), so a hook that
  yields junk without end still returns; a set larger than that is refused before it is sorted. A
  string, a mapping or a number raises, so the research tick's log says the subjects were dropped.
  A `DaySubject`'s label, mandate and style reach the prompts as one plain line each
  (`figures.plain_text`); a mandate or style longer than the editorial goals' 1,000 characters, or
  a label longer than a site name's 80, stops the run, and raw text over four times its bound
  (`DAY_SUBJECT_RAW_TEXT_MULTIPLE`) is refused before it is cleaned.
- **The stamp map.** A `last_article_by_subject` that is not a map (a hand edit) is replaced by
  one holding the subject's stamp, and the log says so, rather than failing every run.

What S2 needs from this: `RailAccessAdapter.subjects` returns the reported site ids.
`day_subject` parses the rota with `parse_rotation(adapter_config, site_ids,
topic.get("daily_timezone"))` (also from `parse_config`, so a bad rota is the topic's
`last_error`). It returns `DaySubject(key=site_id, label=name, mandate=..., style=...)` for
`rotation_subject(rota, at)`. The rotating topic's `findings_window_hours` must cover the rota's
longest gap: 24 h a day, plus a day.

## 4. PR S2: the rail adapter rotates (after S1)

### 4.1 Lift `MAX_RAIL_SITES` by keeping networks out of the running state

- The network is the heavy part of a site's record (about 3 MB each; the scene history is tens
  of KB). Store it as its own object, `snapshots/<topic>/sites/<site>/network.json`, next to the
  figures the adapter already writes to the content bucket (`vision_sites.store_image`); add
  `store_blob` / `load_blob` beside it with the same bucket and error handling.
- The record keeps `network_fetched_at`, `network_key` and `network_stale`; `fetch_state` loads
  the one network it needs for the site it measures this tick, and refreshes it on quiet ticks
  exactly as now (`NETWORK_RETRY_HOURS`, `OSM_RESERVE_SECONDS` unchanged).
- The running state then holds histories only, so `MAX_RAIL_SITES` can rise to **8** (a week
  plus one): measure the state size and the tick's peak RSS at 8 sites with 6-entry histories
  and say the numbers in the comment, as every bound in this module does.
- `vision_status` (PR M) reads the running state with byte budgets; a smaller state only helps.
  It reports "network age" from `network_fetched_at`, which stays in the record. Check PR M's
  `status()` hook for any read of `record["network"]` and route it through the new key.
- The worker request still carries the network inline (the worker is stateless and has no S3
  access by design); nothing changes on that side.

### 4.2 `RailAccessAdapter.day_subject` and `subjects`

- `day_subject`: the rota of 3.7, read in `rotation_timezone`; `label` is the site's `name`
  (cleaned through `rail_notes`' name rule: no markup, one line); `mandate` is
  `ADAPTER_DEFAULTS["rail_access"]` with "for <label>" folded in; `style` as the templates'
  goal text today.
- `subjects`: `_reported_scenes` site ids. A multi-site tick that reports two cities yields a
  Finding with two subjects; each city's day drafts it (the summary names both, which is
  honest), so most ticks should keep `max_sites_per_tick: 1` as the default already is.

### 4.3 Templates, runsheet, docs

- `scripts/rail_topics.example.json` becomes **one** topic, `rail-cities`, with the four sites,
  the rota above, `daily_cadence` every day at 09:05 `Australia/Sydney`,
  `findings_window_hours` covering the rota (120 h for the two-slot rota above; the test checks
  the arithmetic), and one editorial goal that names no city (the subject's mandate adds it).
- `deployment-runsheet.md` "`rail_access` topics": one `topics create`; "How the cadence works"
  rewritten for the rota and the per-subject stamp; "Adding a city" is adding a site and a rota
  slot (`topics update --adapter-config-json` replaces the whole config: read, edit, send back,
  as the runsheet already says).
- `configuration.md`: the `rotation` and `rotation_timezone` rows; `MAX_RAIL_SITES` 8.
- `vision.md` §5 and §6, `blogger-vision.md` step 6, the submission's "Cities" paragraph and
  sketch line (`daily_cadence: one city a day` becomes `rotation: one city a day`), and
  `rail-access-monitor.md`'s status note.
- `test_rail_access_adapter.py`: the rotation test becomes a one-topic simulation (every
  weekday has a city, every Finding drafted once, the window covers the longest gap).

### 4.4 Migrating the four deployed topics

The simple way: create `rail-cities`, let its first tick per city be a first observation again
(the baseline needs `min_baseline` = 1 earlier scene, so each city is back to diffing after its
second clear scene, a few days), then disable the four city topics (`topics update --disabled`
or delete them). Their articles stay published under their own topic ids. The alternative, a
one-off script that copies each old topic's `sites[<city>]` record into the new running state,
keeps the baselines but is more code than it saves; only do it if a week of first observations
matters for the demo.

### 4.5 S2 as built

S2 was built on S1 (#320, with its review fixes, 7176098) and on the owner's network prefetch
(#315), U's first fetch (#312, #319), P1's evidence lines and decision ring (#316) and PR M's
`vision_status`, so some of 4.1 to 4.4 moved. What differs, and why:

- **The network's object is #315's, not a new one.** The prefetch already kept a site's network at
  `vision/<topic>/<site>/network.json` in the content bucket, outside `snapshots/` (which the
  bucket expires after 21 days), where 4.1's `snapshots/<topic>/sites/<site>/network.json` would
  have gone. That object is now the network's only home: the tick's first fetch and refresh write
  it through `vision_sites.store_network`, every visit reads it (`_load_network`, a plain load where
  `_adopt_prefetched` was), and the prefetch and the tick share one store, the last write being the
  network. `prefetch_networks`, its timeouts, results and log lines are unchanged. A missing object,
  one for another polygon, or one whose body is not a network is no network (fetched again, with a
  first fetch's time). An object the record names for this polygon that S3 fails to hand back (a
  read timeout, a 500, a body cut off) is not a missing one: the city waits for the next tick
  (`last_error` `network_read`) and the record keeps the network and any refresh failure, rather
  than spend most of the tick on Overpass for a network S3 still holds (the round's tidy-up). A network that cannot
  be written is a failed fetch (`network_error` code `store`), so the record never names one the
  object does not hold.
- **What the record keeps:** `NETWORK_FIELDS`, `network_key`, `network_fetched_at`,
  `network_site_key` and `network_counts`. `network_stale` stays where it always was, on each
  history entry, and so does `network_fetched_at`, which is all the decision ring's
  `network_changed` compares: no content hash was needed. The network a scene was measured with is
  held on the adapter for the tick only, for the triage agent's `look_again` and the source
  reference. `vision_status` reads the record's fields through the adapter's `status`, never the
  object, so PR M's byte budgets and IAM (`snapshots/*/running-state.json` only) are unchanged.
- **The transition:** a state written before S2 kept each network inline (`network` in the site's
  record). The first tick moves it to the object, unless the object already holds one at least as
  new for the site's polygon (a prefetch the tick had not adopted), and keeps it inline, serving
  from it, until the move has worked. The owner's `trains-sydney` (one city, no rota) keeps its
  network, fetches nothing for the move, and otherwise behaves as before.
- **`MAX_RAIL_SITES` is 8,** measured with the real research tick in a fresh process: 8 cities at
  `osm.py`'s caps (2.8 MB of network JSON each), 6-entry histories, 2,000 station rows in each
  newest scene, names at the 80-character cap and full decision rings make a 7.3 MB state, and a
  tick measuring one scene peaks at 107 MB (16.4 MB and 128 MB with 80 CJK characters a name; two
  scenes a tick 124 and 145 MB; all eight at once, which no budget allows, 220 and 247 MB). The
  first tick on a three-city state from before (11.3 MB) peaks at 134 MB and leaves 3.6 MB.
- **The hook (4.2):** `day_subject(topic, at)` parses the rota with S1's `parse_rotation` over the
  site ids, the zone defaulting to the topic's `daily_timezone`; `parse_config` runs the same check,
  so a broken rota is the topic's `last_error` at tick time and an error before any model call in
  the daily run (§5). The label is the site's name by `rail_notes`' name rule (now public as
  `rail_notes.clean_name`), else its id. The mandate is `ADAPTER_DEFAULTS["rail_access"]` with
  "for <city>" folded into its first sentence ("Report only what was measured for Sydney: ..."),
  and the style line is the old city templates' editorial goal ("a plain report of what the rail
  access measurements show for Sydney: ..."). `subjects` is `_reported_scenes`' site ids, and is
  kept whether or not the topic has a rota, so a topic that gains one later finds its recent
  Findings tagged.
- **Each city has its own first observation.** 4.4 counts on each city of a new topic starting
  with a first observation, but the first observation was the topic's: after it, every other
  city's first clear scene was only "baseline still building", which is never a Finding, so three
  of four cities' days would have had nothing to write until one of them changed. Each site's
  record now keeps `first_observed_at` too, set and carried forward the way the topic's is, and the
  first clear scene of a city the topic had not yet seen clearly is reported as that city's first
  observation (the ring's code `first_observation`). A record from before that is its state's only
  site is the city the topic observed, so it counts as observed when the topic was (the owner's
  trains-sydney stays as it was, even when cloudy scenes have trimmed every clear one from its
  history); one of several counts as observed if its history holds a clear scene. The shared
  `vision_sites` helpers, which `satellite_vision` uses, are unchanged.
- **The window is 192 hours, not 120 (4.3).** It must cover the longest gap between a city's days
  plus a day. The three two-day cities are at most 4 days apart (120 hours), but Boston's one
  Thursday is 7 days from the next, and four cities cannot all have two of seven days: 192 hours.
  The test checks the arithmetic and simulates both (120 hours loses Boston's Findings and only
  Boston's). A list rota of the four (one day in four each) would need 120.
- **The template (4.3)** is one topic, `rail-cities`: the four sites, the owner's rota read in
  Sydney, a run every day at 09:05 Sydney time (`cron(5 9 * * ? *)`), the 80 s budget cap for
  Sydney's first network, and a goal that names no city.
- **Migration (4.4)** is the restart: create `rail-cities`, one tick per city as the first-deploy
  check, then `topics pause` the old city topic (`trains-sydney` on dev); the runsheet's
  [`rail_access` topics](../../deployment-runsheet.md#rail_access-topics) has the steps. The
  benchmark's Sydney network fixture is now taken from the object, not the running state.
- **An import cycle, fixed once:** `common/day_subject.py` imported the adapter registry at its top,
  and the rail adapter imports the module for the rota; S1's review fixes (7176098) import the
  registry inside `resolve_day_subject`, which S2 needed too.

## 5. Edge cases (each with a test)

- A subject in the rota that is not in `sites`, or a site not in the rota: `ConfigError`.
- A Finding reporting two subjects: drafted on both days; neither day re-drafts it after its own
  stamp.
- The rota's day changes while the tick runs (23:59 → 00:00 local): the tick does not read the
  subject, so nothing to agree on; the daily cycle reads it once at `run_started`.
- `rotation_timezone` invalid: `ConfigError` (reuse `scheduler.validate_timezone`).
- An article `subject` the topic no longer has (a city removed): Re-Write filters to nothing and
  falls back to the unfiltered window with a printed line, never an empty prompt.
- A Finding older than the subject's stamp but newer than the topic's: not drafted (the subject
  stamp wins when present).
- A topic that adopts a rota after it has published: each subject's first window starts from
  `findings_window_hours`, not from the topic's last article (no stamp of its own means the window
  alone bounds it, 3.4), so Findings the last plain article already used are drafted again on each
  subject's first day. Kept by choice (7.3: state is not migrated): a topic that has published
  adopts a rota by being created anew (4.4, restart, not migrate), and `configuration.md` says so;
  converting it with `topics update` risks re-drafting those Findings
  (`test_findings_window.py::test_a_subject_never_written_about_is_bounded_by_the_window_alone`).
- `force`: ignores both stamps, keeps the subject filter.
- A rail topic whose stored config does not parse (hand-edited past `topics update`'s check):
  `day_subject` answers None before it parses anything when `adapter_config` sets neither
  `rotation` nor `rotation_timezone` (#327), so a rail topic without a rota reads as one with no
  subject: its daily run and its next-article read are what they always were, and the broken config
  is the tick's `last_error`. Only a rail topic that sets a rota (either key) and whose stored
  config does not parse cannot say whose day it is: the daily run stops with its `ConfigError`
  before any model call, and the next-article read (`topic_findings`, `findings list
  --next-article`) fails the same way, a tool error or a 500, rather than answer with a window the
  daily run would not read.

## 6. Order, size and where it sits

| PR | Scope | Size | Depends on |
|---|---|---|---|
| S1 | the hook, Finding subjects, per-subject stamps, daily cycle and Re-Write, config keys, tests, docs for the generic part | about a day of agent work with two review rounds (it touches the window logic PR K just changed, so the regression probe is mandatory) | PR M merged (`vision_status` reads the topic item; avoid parallel edits to `tools.py`) |
| S2 | per-site network objects, `MAX_RAIL_SITES` 8, the rail hook, one-topic templates, runsheet, migration note | about a day | S1 |

Both target the Phase 2 branch (`claude/vibrant-albattani-rjbsao-p2-integration`) or `dev`
once that has merged, under the same build-review-refute-fix loop as the rail PRs. If the
deadline (26 Oct) leaves no room, ship Phase 3 against the four-topic shape and build S1/S2
afterwards: nothing in the report depends on it except the "Cities" paragraph.

## 7. Decisions for the owner

1. **Fixed rota or random draw?** This spec assumes a fixed weekly rota (predictable). Crypto's
   seeded random draw is one line away if variety is wanted (`random.Random(f"{topic_id}:{day}")`).
2. **Two slots a week per city or one?** Two slots (the current templates') halves the window
   and doubles the articles; one slot (seven cities) is the other natural shape.
3. **Migrate state or restart?** §4.4 recommends restarting.
4. **Keep the four topics in production for the demo** until S2 lands, or wait for S2 before
   creating any rail topic? The first is safe: the topics are independent, and S2 does not
   change what they measure.

---

*Rule 5 check: the pipeline gains one adapter hook and one Finding field, both optional, and no
adapter name. The rota, its timezone, the mandate text and the per-city state live in the rail
adapter.*
