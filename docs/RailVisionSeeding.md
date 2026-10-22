# Rail vision seeding: the `rail-cities` runsheet

How to seed the rotating rail topic: one `rail_access` topic that watches four cities and writes
about one of them at a time: one a day as first seeded, one a week since the switch in
[section 7](#7-a-city-a-week-written-up-from-three-sides), from four sides since the bus, tram and
ferry stops joined the measures. This covers the editorial goals it is created with, what each
article shows (its text and its maps), how to create and check it, and how it differs from the
"weekly city rotation" prompt template it was adapted from.

Background, not repeated here: [what the rail task measures](architecture/blogger-vision.md#part-two-the-trains-explainer),
every config key ([configuration.md, A `rail_access` topic](configuration.md#a-rail_access-topic)),
the rota design ([dailysubject_hook.md](enhancements/archive/dailysubject_hook.md)) and the cadence in
detail ([vision-runsheet.md, How the cadence works](vision-runsheet.md#how-the-cadence-works)).

## Contents

- [1. What gets seeded](#1-what-gets-seeded)
- [2. What an article is made of](#2-what-an-article-is-made-of)
  - [The text](#the-text)
  - [The figures](#the-figures)
- [3. The editorial goal](#3-the-editorial-goal)
  - [What was kept from the "weekly city rotation" template, and what was not](#what-was-kept-from-the-weekly-city-rotation-template-and-what-was-not)
- [4. Seeding runsheet (dev)](#4-seeding-runsheet-dev)
- [5. Reviewer checklist (before `approve`)](#5-reviewer-checklist-before-approve)
- [6. Adding a city later](#6-adding-a-city-later)
- [7. A city a week, written up from three sides](#7-a-city-a-week-written-up-from-three-sides)

## 1. What gets seeded

| Setting | Value | Why |
|---|---|---|
| `topic_id` | `rail-cities` | One topic for every city, not one topic per city |
| Sites | Sydney, Melbourne, Singapore, Boston (the boxes in `scripts/rail_topics.example.json`) | Each box sits inside one Sentinel-2 tile |
| `rotation` | Sydney Mon and Fri, Melbourne Tue and Sat, Singapore Wed and Sun, Boston Thu | The day-subject rota |
| `rotation_timezone` | `Australia/Sydney` | "Tuesday" is Tuesday in Sydney |
| Schedules | Set by the stagger, not by the example's `daily_cadence` | Each topic gets a slot 5 minutes apart (`topics schedule`). In dev on 2026-10-17, `rail-cities` took slot 5: research hourly at :25 past, the daily run at 09:50 Sydney time. The example's `cron(5 9 * * ? *)` is sent but rewritten |
| `findings_window_hours` | 192 | Boston's 7 days between Thursdays, plus a day |
| `reach_m` | 1000 | Straight-line walking reach of a station |
| `time_budget_seconds` | 80 | A big city's first OpenStreetMap network takes most of a minute |
| `editorial_goals` | Section 3 below (the example file carries the same text) | Analytical rather than a plain report, and still inside the adapter's limits |
| `editorial_goal_rotation` | Section 7's four goals, `access`, `network`, `simulations` and `transit` (the example file carries them too) | One Finding written up from four sides, one a day |
| `force_manual_review` | forced on by the admin API | Every rail article waits in the review inbox |

**Restart, don't migrate.** A rota goes on a new topic, never on one that has published
([configuration.md](configuration.md), the `rotation` row): each city's first window would draft
Findings the old topic's article already used. The single-city `trains-sydney` topic is **paused**,
not edited. Its Findings and articles stay, and it can be resumed or deleted later.

## 2. What an article is made of

Each day's article is drafted only from the day's city's Findings since that city's last article
(at most 48, newest first). A Finding is written only for a city's first clear scene, a material
change (a served-share move of 2 points or more, a desert-area move of 10% of its baseline median
or more (the median counted as at least 1 km², so the smallest material move is 0.1 km²), or a
changed station count) or, on a week rota, the city's weekly reading (section 7). A city with
nothing new gets no article that day (`no_findings`, no model call), unless a goal rotation still
has a goal to write its last Findings up under (section 7).

### The text

What the drafting model can cite, all of it in the Finding's summary:

| Data | Where it comes from | Use in the article |
|---|---|---|
| Served share (% of built-up area within `reach_m` of a station) | Worker: NDBI/NDVI built-up mask × station reach | The headline number |
| Built-up area beyond reach (km²) | Worker | The "transit desert" size. The total built-up area is measured and kept in the state, but the summary does not carry it, so an article cannot cite it |
| Station count | OpenStreetMap network | Context; a change is material on its own |
| With the network's `stops` layer: the share of built-up area within reach of any mapped stop or station beside the rail-only share, the area beyond all of them, the bus, tram and ferry stop counts, the built-up km² within stop reach per 100 stops, and how many of the largest rail deserts the stops mostly cover (half or more of the patch within stop reach). The overlap with station reach is measured and kept in the state, but the summary does not carry it | Worker: a distance transform over every mapped bus and tram stop and the ferry terminals, 400 m by default (`stop_reach_m`) | The transit angle. Reach is straight-line distance; minutes are that distance at 80 m a minute, never a journey time. Reported, never material; absent (not zero) until the city's network carries the layer |
| Scene date | Sentinel-2 STAC item | Always stated: it dates every figure |
| Coverage (the share of the site the scene could see: not cloud, cloud shadow, cirrus, saturation or no data in its SCL band; not the STAC item's cloud cover) | Worker: `vision/rail_analyse.py` `analyse_rail_access` | Always stated beside the scene's figures; a scene below `coverage_floor` is never material |
| Change against the baseline (median of earlier clear scenes) | `material_diff` | What changed, if anything |
| Flags: `single_point_of_failure`, `isolated_high_demand`, `ghost_line` | `vision/rail_analyse.py` `flag_stations`, phrased by `common/rail_notes.py` | One paragraph each, the station named in quotes; an isolated-high-demand flag also carries the station's feeder coverage and multimodal isolation weight (the rail weight discounted by the share of its hinterland the stops reach) |
| Simulations: `orbital_link`, `feeder_corridor`, each `theoretical`, with its justification (both stations' distance to a hub and the fall in mean hub distance; a feeder's two isolation weights and feeder coverage) and what its straight line crosses (`water_share`, `water_km`, `unmeasured_share`) | `rail_analyse.suggest`, phrased by `rail_notes.py` | Always theoretical: a what-if on the mapped graph and straight lines, never a route, an alignment or a recommendation; conditional words ("the simulation suggests…") |
| The triage agent's note | `common/vision_triage.py` | Quoted, as "not a measurement" |

The planner notes are capped at 4 flags and 3 simulations per scene, in a fixed order. The summary
prompt asks an article to end with at most three of them, so each article discusses the strongest
few, not all of them.

### The figures

Each measured scene gives two figures. The first is the **rail map**: the worker's annotated heat
map of the city (built-up density in warm colours, areas beyond reach outlined, OpenStreetMap lines
and stations, flagged stations in red rings, theoretical links dashed, in azure when a link's
straight line is mostly over water). The second is the **transit-reach map**
(`<scene>-modes.png`): the same scene in grey, with the built-up ground filled by what reaches it:
within 1 km of a rail station (about 12 min walk), beyond that but within 400 m of a mapped bus,
tram or ferry stop (5 min walk), and beyond both (outlined, so it reads without colour). The
minutes are the straight-line reach restated at a stated 80 m a minute, never a journey, travel
or waiting time. Both carry a fixed caption with the Copernicus and OpenStreetMap credits, are
stored with the Finding (`figures`) and copied to the article page when it is published. A city
whose stored network predates the stops layer has no transit-reach map (and no stop measures:
the quality flag `no_stops_layer`) until the network is refreshed at its TTL or prefetched
([vision-runsheet.md, rail_access topics](vision-runsheet.md#rail_access-topics)).

**What the article shows today:** `common/figures.py` `figures_for_findings` takes the figures of
the Findings drafted, newest first, each once, at most **3** (`MAX_FIGURES_PER_ARTICLE`). Because
the rail map carries the flags and simulations the text discusses and the transit-reach map what
the stops add, the newest scene's two maps of the day's city are the right figures; an article
drafted from two Findings shows three (the newest scene's two, then the older rail map).

**What to change in code, so an article shows only its own city's map:**

1. **Filter by subject.** A Finding that measured two cities in one tick carries both maps, and on
   one city's day the other city's map can come along. With `max_sites_per_tick` at 1 this is
   rare, but filter the figures to the day's subject (the key is
   `vision/<topic>/<site>/<scene>.png`) before the cap.
2. **One scene per city per article.** Two or three maps of the same city from different weeks
   look alike and add nothing. Keep the newest scene per site, which for a rota article means its
   two maps. Older scenes belong in the text as numbers ("down from X% on <date>").

Until that lands, the reviewer drops a redundant or wrong-city map in the review inbox before
approving (section 5).

## 3. The editorial goal

The admin API takes two fields, `primary_focus` and `exclusion_criteria`, each at most 1000
characters. There are no title templates and no other fields. The goal names no city: on each day
the mandate adds the day's city (`rail_access.subject_mandate`), and the adapter's own rules
(only what was measured, with dates; no ridership, cause, forecast or recommendation; every
simulated link theoretical; a constructive register) apply on top whatever the goal says.
Compliance holds any draft with a number found in none of the sources. Five things the goal says
in plain words, and section 7's four goals say again: reach is straight-line distance to the
nearest mapped station or bus, tram or ferry stop, and minutes are that distance at a stated 80 m a
minute, never a journey, travel or waiting time; every simulated link is theoretical, a what-if on
the mapped graph and straight lines, justified by its numbers and qualified by what its line
crosses, never a route, an alignment or a recommendation; the register is constructive, with no
complaint, blame or cost, political or resource-allocation view; the efficiency of the mapped stops
and the multimodal isolation weight are named as what they measure; and nothing is said that is
not in the findings.

```json
{
  "primary_focus": "Rail and transit access in the day's city, analysed from the findings alone. Title it plainly after what was measured; open with what the measures are: built-up density from Sentinel-2 imagery and straight-line reach over OpenStreetMap's stations and bus, tram and ferry stops; minutes are that distance at 80 m a minute, never a journey time. Then the numbers: the share of built-up area within reach of a station and, where given, of any stop, the area beyond, station and stop counts, the scene's date and coverage, and any change from earlier scenes. Then each rule-based flag the findings carry (single point of failure, isolated high demand, ghost line): the station, what it measures, its multimodal isolation weight where given. Then each simulated link, always theoretical: a what-if on the mapped graph and straight lines, justified by its numbers and what its line crosses, never a route or recommendation. Say what the measures show and what a link would change; point to each map once.",
  "exclusion_criteria": "No ridership, patronage, frequency, journey, travel or waiting time, cost, funding, population, income or other demographic claim: built-up density measures structures, not people, and minutes are walking time over a straight line. No recommendation to build, extend or close anything, no route or alignment, no forecast and no cause. No complaint, blame or criticism of transport agencies, governments, politicians or zoning, and no cost, funding, political or resource-allocation view: say what the measures show and what a theoretical link would change, never what anyone should have done. No absolute judgement that a place is badly served or unlivable. No comparison with a city the findings do not measure. Nothing from outside the findings (strikes, fares, rolling stock, timetables, plans). Name a flag, link or station only if the findings carry it; no number or place name that is not in the findings."
}
```

### What was kept from the "weekly city rotation" template, and what was not

| Template idea | Here | Why |
|---|---|---|
| One generic prompt, the city filled in per day | Kept | The day-subject mandate fills in the city; the goal names none |
| Three title templates | Folded into one plain title instruction | No title-template field exists, and titles like "Spatially Uncoupled" oversell a proxy |
| "Define data bounds early" | Kept: the opening of every article | Same as the adapter's own rule |
| Flags only from the `flags` array | Kept, stronger: no flag the findings don't carry | Ghost lines and single points of failure come only from `rail_notes.py` sentences |
| Conditional words for simulations | Kept, stronger: every link is theoretical, justified by its numbers and qualified by what its straight line crosses | `rail_notes.py` calls them theoretical simulations, never routes or recommendations, and says what each crosses |
| Network vulnerabilities, betweenness, isolation weight | Kept, as what each flag measures | The flags are built from betweenness, `d_hub_m` and `isolation_weight` |
| No political or agency blame, no absolute judgements, no demographics, nothing outside the data | All kept, in `exclusion_criteria`, as a constructive register: what the measures show and what a theoretical link would change, never a complaint or a cost, political or resource-allocation view | The adapter's own drafting guidance says the same, whatever the goal |
| Sprawl, car dependency, a 5-to-10-year outlook, economic mobility, spatial equity | Dropped | A forecast and a cause, which the adapter forbids and no measure supports |
| Commuter-flow bottlenecks, where bus loops or park-and-ride are "most critical" | Dropped | Ridership and a recommendation |
| Tunnels as engineering achievements | Dropped for now | The worker separates tunnel ways, but nothing about them reaches the Finding, so the drafter has nothing to cite. It returns if the summary ever reports tunnel share |
| `[Reviewer Context: …]` markers in the text | Dropped | Every rail article is already held; a marker left in by an approve without an edit would be published. The reviewer checklist (section 5) does that job |

## 4. Seeding runsheet (dev)

Use dev unless production is named. From the repository root, with AWS credentials for the
account and the caller's IP on the admin WAF allowlist. If `BLOGGERBEAR_ADMIN_API_URL` is stale,
look the id up with `aws apigateway get-rest-apis` (`bloggerbear-dev-admin-api`) and pass
`--api-url https://<id>.execute-api.ap-southeast-2.amazonaws.com/dev --region ap-southeast-2`
to every command below (shown here as `$A`).

**0. The code is deployed.** The day-subject round must be live in the environment: the latest
`terraform` run on `dev` is green, and `aws lambda list-functions` shows the `research-tick` and
`daily-cycle` functions modified after the merge. The vision worker is on (`VISION_ENABLED`).

**1. Write the goal file** (section 3) to `goals.json` outside the repository. The example's
`editorial_goal_rotation` (section 7's four goals, read out of the example below) goes in at
creation too.

**2. Create the topic** from the example's config, with this goal:

```bash
field() { python -c 'import json, sys; v = json.load(open("scripts/rail_topics.example.json"))["topics"][0][sys.argv[1]]; print(v if isinstance(v, str) else json.dumps(v))' "$1"; }
python scripts/admin_cli.py $A topics create --topic-id rail-cities --name "$(field name)" \
    --adapter rail_access --config-json "$(field adapter_config)" \
    --editorial-goals-json "$(cat goals.json)" \
    --goal-rotation-json "$(field editorial_goal_rotation)" \
    --daily-cadence "$(field daily_cadence)" --daily-timezone "$(field daily_timezone)" \
    --findings-window-hours "$(field findings_window_hours)"
python scripts/admin_cli.py $A topics get rail-cities
```

Check `force_manual_review: true` (forced; not sent), `rotation` and `rotation_timezone` as
sent, the four goals under `editorial_goal_rotation`, and `findings_window_hours: 192`.

**3. Pause the single-city topic:**

```bash
python scripts/admin_cli.py $A topics pause trains-sydney
```

**4. Seed every city: one research tick per city.** Each tick takes the next city never
attempted (Sydney, Melbourne, Singapore, Boston), so trigger a tick, wait about two minutes,
and repeat:

```bash
python scripts/admin_cli.py $A topics trigger rail-cities --pipeline research_tick
```

A city whose first network took the tick's whole budget keeps its place and is measured on the
next tick, so expect four to eight ticks. For a city that never gets past its network fetch,
fetch the network out of band, then tick again:

```bash
python scripts/admin_cli.py $A topics trigger rail-cities --pipeline network_prefetch
```

**Known limit of the prefetch (seen when this topic was seeded in dev, 2026-10-17):**
`prefetch_networks` fetches every site in config order and skips none, even one whose stored
network is fresh, so within its budget it reaches the same first cities on every run. Sydney and
Melbourne took the whole budget twice, and Singapore and Boston were `deferred` both times. Until
it skips fresh networks, seed the later cities with ordinary research ticks: a tick gives a city's
first network the rest of its 80 s budget less 10 s, which a mid-sized city fits.

**5. Check the seed.** Each city with a clear scene should have one first-observation Finding,
with its city in `subjects`, its figures (the rail map, and the transit-reach map once the city's
network carries its stops) and its planner notes:

```bash
python scripts/admin_cli.py $A findings list rail-cities --limit 10
python scripts/admin_cli.py $A findings list rail-cities --next-article
```

`--next-article` answers for today's city on Sydney's calendar. A city with no Finding: see
[vision-runsheet.md, rail_access topics](vision-runsheet.md#rail_access-topics) (cloud
cover, `last_error`, the network).

**6. Optional: the first article now** (otherwise it comes at the topic's staggered daily slot:
`topics schedule` shows it):

```bash
python scripts/admin_cli.py $A topics trigger rail-cities --pipeline daily_cycle
python scripts/admin_cli.py $A inbox
```

The log says `daily_cycle_handler: day_subject=<city> topic_id=rail-cities`, and the article waits
in the inbox with its city as `subject`.

From then on the hourly research heartbeat measures each city as new scenes arrive, whatever the
day, and the daily run writes about the day's city whenever it has a new Finding. With the goal
rotation set in step 2, the city's day with nothing new is not empty: the run revisits the
Findings that city's last new article was drafted from, under the next goal not yet used on them
(`revisit=True`). This goes on until all four goals have been used on them or they expire (14
days), and only then does the run stop with `no_findings`.

## 5. Reviewer checklist (before `approve`)

- [ ] The opening says what the measures are and what they are not (no ridership, frequency or
      travel time); any minutes are walking time at 80 m a minute over a straight line.
- [ ] Every number is in the Finding (`findings list rail-cities` summary), with the scene's date.
- [ ] Every flag and station named is in the Finding's planner notes; no "ghost line" or "single
      point of failure" the notes don't carry.
- [ ] Every simulation is called theoretical and says what it crosses (water, ground the scene
      could not see), with its numbers; conditional ("the simulation suggests"), never a route,
      an alignment or "should build".
- [ ] No cause, forecast, demographic, cost, funding, agency, political or resource-allocation
      claim; no complaint or blame; no "badly served". The text says what the measures show and
      what a theoretical link would change.
- [ ] The figures are the day's city's two maps, the newest (the rail map and, once the city's
      network carries its stops, the transit-reach map), and no other scene's. Drop a duplicate
      or a wrong-city map (section 2).
- [ ] The caption keeps the Copernicus and OpenStreetMap credits.

## 6. Adding a city later

Add a site to `adapter_config.sites` (one Sentinel-2 tile, at most 8 sites) and give it a day in
`rotation` (on section 7's week rota, a place in the list: the weeks are counted modulo the list's
length, so a longer list changes whose week it is from then on). Every site needs one. Read the
whole config with `topics get`, edit it, and send all of it back with
`topics update rail-cities --adapter-config-json '...'`: that flag replaces the whole config. Keep
every existing site's `id` and polygon, so its history and network still match. Re-check
`findings_window_hours` against the longest gap between one city's days, plus a day (on the week
rota, at least 24).

## 7. A city a week, written up from three sides

The rota above gives each city a day or two a week, and each city writes only when research finds
a material change, which is rare. This switch gives each city a **whole week**
(`rotation_period` `"week"`) and four editorial goals taken in turn (`editorial_goal_rotation`,
[configuration.md](configuration.md)); the fourth, `transit`, joined when the bus, tram and ferry
stops entered the measures, and the heading keeps its name so links to it hold:

| Day of the city's week | What runs | Goal |
|---|---|---|
| Monday, first research tick (about 00:25 Sydney) | The **weekly reading**: the city's newest clear scene, no change claimed, with its map and planner notes | |
| Monday 09:50 | The article from the weekly reading | `access`: served share, the area beyond reach, the map's heat and outlines |
| Tuesday 09:50 | A revisit of the same Finding | `network`: the rule-based flags, the stations they name |
| Wednesday 09:50 | A revisit of the same Finding | `simulations`: the theoretical links, each justified by its numbers and saying what its straight line crosses |
| Thursday 09:50 | A revisit of the same Finding | `transit`: reach by any mode, how efficiently the mapped stops cover built-up ground, how many of the largest rail deserts they mostly cover, walking minutes; the transit-reach map |
| Friday to Sunday | Nothing, unless research finds a material change (a new Finding starts the goals again) | |

The order of the list sets whose week it is (the weeks since Monday 1970-01-05, modulo four).
`["melbourne", "singapore", "boston", "sydney"]` gives Sydney the week of 19 October 2026, then
Melbourne (26 October), Singapore (2 November) and Boston (9 November). The week of 12 October
is Boston's, so the first research tick after the switch writes Boston's weekly reading and the
Sunday 18 October run writes it up.

`rail-cities` has not published, so its config is updated in place: nothing it wrote is drafted
twice. On a topic that has published, restart it instead (section 1).

**0. Both changes are deployed:** the goal rotation (#335) and the week rota are merged, and the
latest `terraform` run on `dev` is green, with `research-tick`, `daily-cycle` and `admin-api`
modified after it.

**1. The goals** (`rail_goal_rotation.json`, outside the repository; each `primary_focus` at
most 1000 characters, so count them: `python -c 'import json; [print(g["name"], len(g["primary_focus"])) for g in json.load(open("rail_goal_rotation.json"))]'`;
the topic's `editorial_goals.exclusion_criteria` apply to all four). Until a city's network
carries its stops layer, its Finding has no stop measures and no transit-reach map (`no_stops_layer`),
so a `transit` article drafted from it has only the rail-only share to report: prefetch the
networks first ([the runsheet](vision-runsheet.md#rail_access-topics)):

```json
[
  {"name": "access", "primary_focus": "Rail and transit access in the day's city through walking reach, from the findings alone. Title it plainly after what was measured. Open by saying what the measures are: built-up density from Sentinel-2 imagery and straight-line reach over OpenStreetMap's rail stations and mapped bus, tram and ferry stops, with minutes as that distance at 80 m a minute, never a journey time. Lead with the share of built-up area within reach of a station and the built-up area beyond it; where the findings give it, put the share within reach of any mapped stop or station beside the rail-only one, with the area beyond all of them. Then the station count, the scene's capture date and coverage, and any change against the city's earlier scenes. Point to the rail map once, saying what its warm colours and outlined areas show. Say what the measures show, never a complaint or a cost, political or resource-allocation view. Give the flags and simulations one sentence at most: they have their own articles."},
  {"name": "network", "primary_focus": "The structure of the day's city's mapped rail network, from the findings alone. Title it plainly after the flag it leads with. Open by saying the flags are rule-based readings of OpenStreetMap's topology and built-up density, not ridership or service. Take each rule-based flag the findings carry in turn (single point of failure, isolated high demand, ghost line): the station or stations it names, what the flag measures (shortest paths through the network, distance to a hub, activity along a chain), and the figure behind it. Where the findings give a station's multimodal isolation weight, put it beside the rail one and say what it is: the rail weight discounted by the share of the station's hinterland that mapped bus, tram and ferry stops reach. Give the served share and station count once, as context. Say what the measures show, never a complaint, blame or criticism of anyone. Point to the rail map once: flagged stations are ringed in red. Name only stations the findings name."},
  {"name": "simulations", "primary_focus": "The theoretical links for the day's city's mapped rail network, from the findings alone. Title it plainly after the link it leads with. Open by saying every link is theoretical: a rule-based what-if on OpenStreetMap's topology and straight-line distances, not a route, an alignment, a plan, a cost or a recommendation. Take each link the findings carry (orbital link, feeder corridor): the stations it joins, its length, its justification in the numbers (the stations' isolation weights and distances to a hub, the fall in hub distance, how many stations it brings closer) and what its straight line crosses (water, ground the scene could not see), always in conditional words ('in the model', 'the simulation suggests'). Say what each link would change, never a cost, political or resource-allocation view. Give the served share and any flag the link answers once, as context. Point to the rail map once: theoretical links are dashed. Name only stations the findings name."},
  {"name": "transit", "primary_focus": "Transit reach across modes in the day's city, from the findings alone. Title it plainly after what was measured. Open by saying the measures are straight-line reach to the nearest mapped bus, tram or ferry stop and rail station over OpenStreetMap, with minutes as that distance at 80 m a minute, never a journey time. Lead with the share of built-up area within walking reach of any mapped stop or station against the rail-only share, and the area beyond all of them. Then how efficiently the mapped stops cover built-up ground: the km² within reach per 100 stops and the overlap with station reach; how many of the largest rail deserts the stops mostly cover (half or more of a patch within stop reach); the stop counts by mode; the scene's date and coverage. Point to the transit-reach map once, saying what its three fills show. Constructive: what the measures show, never a complaint or a cost, political or resource-allocation view. No number or place name that is not in the findings."}
]
```

**2. The week rota.** `--adapter-config-json` replaces the whole config, so read it, change only
`rotation` and add `rotation_period`, and send all of it back (every site's `id` and polygon
unchanged, so its history and network still match):

```bash
python scripts/admin_cli.py $A topics get rail-cities > rail-cities.json
python -c 'import json; t = json.load(open("rail-cities.json")); c = t["adapter_config"]; c["rotation"] = ["melbourne", "singapore", "boston", "sydney"]; c["rotation_period"] = "week"; print(json.dumps(c))' > adapter_config.json
python scripts/admin_cli.py $A topics update rail-cities --adapter-config-json "$(cat adapter_config.json)" \
    --goal-rotation-json "$(cat rail_goal_rotation.json)"
python scripts/admin_cli.py $A topics get rail-cities
```

Check `rotation` is the list, `rotation_period` is `"week"`, the four goals are stored, and
`editorial_goals` and `findings_window_hours` (192) are unchanged.

**2b. Pushing new goal text to the running topic.** The live topic's goals are rows in
DynamoDB, not code: when the goals change (as they did when the stops joined the measures: the
theoretical framing, the walking minutes and the `transit` goal), write section 3's goal to
`goals.json` and this section's four to `rail_goal_rotation.json`, count them, and send both with
one update; it changes nothing else of the topic:

```bash
python scripts/admin_cli.py $A topics update rail-cities \
    --goal-rotation-json "$(cat rail_goal_rotation.json)" \
    --editorial-goals-json "$(cat goals.json)"
python scripts/admin_cli.py $A topics get rail-cities
```

`goal_rotation_state` is kept, so a city in the middle of its week carries on with the goals not
yet used on its Finding. Until the rows are pushed, the adapter's own default mandate and drafting
guidance already carry the framing, and a Finding written against a network without stops is
written up as before, apart from the wording.

**3. The weekly reading now:** `topics trigger rail-cities --pipeline research_tick`. The log's
`research_tick` line says `material_change`, and `findings list rail-cities --limit 1` shows a
Finding about this week's city, its `subjects` naming that city, with its figures (one map, or two
once the city's network carries its stops) and its planner notes. The tick's diff summary opens
"Weekly reading (no change claimed).", but the stored summary is the model's own wording, written
from that diff, and need not start that way.

**4. Optional: the first article now** (otherwise at the topic's daily slot):
`topics trigger rail-cities --pipeline daily_cycle`. The log says `day_subject=<this week's city>`
and `editorial_goal=access revisit=False` when the city has no `goal_rotation_state` entry yet or
the goal it used last was `transit` (or one no longer in the list). New Findings take the goal
after the one the city used last, so a city already written up under the day rota may start
further down the list (`goal_rotation_state.<city>.goal` shows the goal it used last). The article
waits in the inbox. Each later trigger revisits the same Finding (`revisit=True`) under the next
goal not yet used on it, in list order and wrapping round (from `access`: `network`, then
`simulations`, then `transit`). Once all four have been used on it, the next trigger writes
nothing.

**Watching it:** `topics get rail-cities` shows `goal_rotation_state.<city>` (the goal used last
and the goals used on the current Finding); the research state shows each city's
`reported_period`.
