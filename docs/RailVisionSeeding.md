# Rail vision seeding: the `rail-cities` runsheet

How to seed the rotating rail topic: one `rail_access` topic that watches four cities and writes
about one of them a day. This covers the editorial goal it is created with, what each article
shows (its text and its maps), how to create and check it, and how it differs from the
"weekly city rotation" prompt template it was adapted from.

Background, not repeated here: [what the rail task measures](architecture/blogger-vision.md#part-two-the-trains-explainer),
every config key ([configuration.md, A `rail_access` topic](configuration.md#a-rail_access-topic)),
the rota design ([dailysubject_hook.md](enhancements/dailysubject_hook.md)) and the cadence in
detail ([deployment-runsheet.md, rail_access topics](deployment-runsheet.md#rail_access-topics)).

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
| `editorial_goals` | Section 3 below, **not** the example file's | Analytical rather than a plain report, and still inside the adapter's limits |
| `force_manual_review` | forced on by the admin API | Every rail article waits in the review inbox |

**Restart, don't migrate.** A rota goes on a new topic, never on one that has published
([configuration.md](configuration.md), the `rotation` row): each city's first window would draft
Findings the old topic's article already used. The single-city `trains-sydney` topic is **paused**,
not edited. Its Findings and articles stay, and it can be resumed or deleted later.

## 2. What an article is made of

Each day's article is drafted only from the day's city's Findings since that city's last article
(at most 48, newest first). A Finding is written only for a city's first clear scene or a material
change (a served-share move of 2 points or more, a desert-area move of 10% (at least 1 km²) or more,
or a changed station count), so a city with nothing new gets no article that day (`no_findings`, no
model call).

### The text

What the drafting model can cite, all of it in the Finding's summary:

| Data | Where it comes from | Use in the article |
|---|---|---|
| Served share (% of built-up area within `reach_m` of a station) | Worker: NDBI/NDVI built-up mask × station reach | The headline number |
| Built-up area beyond reach (km²), built-up area (km²) | Worker | The "transit desert" size |
| Station count | OpenStreetMap network | Context; a change is material on its own |
| Scene date, coverage | Sentinel-2 STAC item | Always stated: it dates every figure |
| Change against the baseline (median of earlier clear scenes) | `material_diff` | What changed, if anything |
| Flags: `single_point_of_failure`, `isolated_high_demand`, `ghost_line` | `vision/rail_analyse.py` `flag_stations`, phrased by `common/rail_notes.py` | One paragraph each, the station named in quotes |
| Simulations: `orbital_link`, `feeder_corridor` | `rail_analyse.suggest`, phrased by `rail_notes.py` | Conditional: "the simulation suggests…" |
| The triage agent's note | `common/vision_triage.py` | Quoted, as "not a measurement" |

The planner notes are capped at 4 flags and 3 simulations per scene, in a fixed order. The summary
prompt asks an article to end with at most three of them, so each article discusses the strongest
few, not all of them.

### The figures

Each measured scene gives one figure: the worker's annotated heat map of the city (built-up density
in warm colours, areas beyond reach outlined, OpenStreetMap lines and stations, flagged stations in
red rings, simulated links dashed), with a fixed caption carrying the Copernicus and OpenStreetMap
credits. It is stored with the Finding (`figures`) and copied to the article page when it is
published.

**What the article shows today:** `common/figures.py` `figures_for_findings` takes the figures of
the Findings drafted, newest first, each once, at most **3** (`MAX_FIGURES_PER_ARTICLE`). Because
the map carries the flags and simulations the text discusses, the newest map of the day's city is
the right figure.

**What to change in code, so an article shows only its own city's map:**

1. **Filter by subject.** A Finding that measured two cities in one tick carries both maps, and on
   one city's day the other city's map can come along. With `max_sites_per_tick` at 1 this is
   rare, but filter the figures to the day's subject (the key is
   `vision/<topic>/<site>/<scene>.png`) before the cap.
2. **One map per city per article.** Two or three maps of the same city from different weeks look
   alike and add nothing. Keep the newest per site, which for a rota article means one figure.
   Older scenes belong in the text as numbers ("down from X% on <date>").

Until that lands, the reviewer drops a redundant or wrong-city map in the review inbox before
approving (section 5).

## 3. The editorial goal

The admin API takes two fields, `primary_focus` and `exclusion_criteria`, each at most 1000
characters. There are no title templates and no other fields. The goal names no city: on each day
the mandate adds the day's city (`rail_access.subject_mandate`), and the adapter's own rules
(only what was measured, with dates; no ridership, cause, forecast or recommendation) apply on top
whatever the goal says. Compliance holds any draft with a number found in none of the sources.

```json
{
  "primary_focus": "An analytical report on rail access in the day's city, from the findings alone. Title it plainly after what was measured (for example 'The walking-reach gap: rail access in <city>'). Open by saying what the measures are: built-up density from Sentinel-2 imagery and straight-line walking reach over OpenStreetMap's rail network, not ridership, frequency or travel time. Then the headline measures: the share of built-up area within reach of a station, the built-up area beyond it, the station count, the scene's date and coverage, and any change against the city's earlier scenes. Then each rule-based flag the findings carry (single point of failure, isolated high demand, ghost line): which station, and what the flag measures. Then the simulated links (orbital link, feeder corridor) and the change in distance to a hub each shows, in conditional words ('the simulation suggests'). Point to the map once, saying what its colours, outlines and lines show.",
  "exclusion_criteria": "No ridership, patronage, frequency, journey time, cost, funding, population, income or other demographic claim: built-up density measures structures, not people. No recommendation to build, extend or close anything, no forecast and no cause. No criticism of transport agencies, governments, politicians or zoning. No absolute judgement that a place is badly served or unlivable. No comparison with a city the findings do not measure. Nothing from outside the findings (strikes, fares, rolling stock, timetables, plans). Name a flag, vulnerability or station only if the findings carry it; no number or place name that is not in the findings."
}
```

### What was kept from the "weekly city rotation" template, and what was not

| Template idea | Here | Why |
|---|---|---|
| One generic prompt, the city filled in per day | Kept | The day-subject mandate fills in the city; the goal names none |
| Three title templates | Folded into one plain title instruction | No title-template field exists, and titles like "Spatially Uncoupled" oversell a proxy |
| "Define data bounds early" | Kept: the opening of every article | Same as the adapter's own rule |
| Flags only from the `flags` array | Kept, stronger: no flag the findings don't carry | Ghost lines and single points of failure come only from `rail_notes.py` sentences |
| Conditional words for simulations | Kept | `rail_notes.py` already calls them simulations |
| Network vulnerabilities, betweenness, isolation weight | Kept, as what each flag measures | The flags are built from betweenness, `d_hub_m` and `isolation_weight` |
| No political or agency blame, no absolute judgements, no demographics, nothing outside the data | All kept, in `exclusion_criteria` | |
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

**1. Write the goal file** (section 3) to `goals.json` outside the repository.

**2. Create the topic** from the example's config, with this goal:

```bash
field() { python -c 'import json, sys; v = json.load(open("scripts/rail_topics.example.json"))["topics"][0][sys.argv[1]]; print(v if isinstance(v, str) else json.dumps(v))' "$1"; }
python scripts/admin_cli.py $A topics create --topic-id rail-cities --name "$(field name)" \
    --adapter rail_access --config-json "$(field adapter_config)" \
    --editorial-goals-json "$(cat goals.json)" \
    --daily-cadence "$(field daily_cadence)" --daily-timezone "$(field daily_timezone)" \
    --findings-window-hours "$(field findings_window_hours)"
python scripts/admin_cli.py $A topics get rail-cities
```

Check `force_manual_review: true` (forced; not sent), `rotation` and `rotation_timezone` as
sent, and `findings_window_hours: 192`.

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
with its city in `subjects`, one figure and its planner notes:

```bash
python scripts/admin_cli.py $A findings list rail-cities --limit 10
python scripts/admin_cli.py $A findings list rail-cities --next-article
```

`--next-article` answers for today's city on Sydney's calendar. A city with no Finding: see
[deployment-runsheet.md, rail_access topics](deployment-runsheet.md#rail_access-topics) (cloud
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
day, and the daily run writes about the day's city whenever it has a new Finding.

## 5. Reviewer checklist (before `approve`)

- [ ] The opening says what the measures are and what they are not (no ridership, frequency or
      travel time).
- [ ] Every number is in the Finding (`findings list rail-cities` summary), with the scene's date.
- [ ] Every flag and station named is in the Finding's planner notes; no "ghost line" or "single
      point of failure" the notes don't carry.
- [ ] Simulations are conditional ("the simulation suggests"), never "should build".
- [ ] No cause, forecast, demographic, cost, agency or political claim; no "badly served".
- [ ] The figure is the day's city's newest map, and only one. Drop a duplicate or a
      wrong-city map (section 2).
- [ ] The caption keeps the Copernicus and OpenStreetMap credits.

## 6. Adding a city later

Add a site to `adapter_config.sites` (one Sentinel-2 tile, at most 8 sites) and give it a day in
`rotation`. Every site needs one. Read the whole config with `topics get`, edit it, and send all of
it back with `topics update rail-cities --adapter-config-json '...'`: that flag replaces the whole
config. Keep every existing site's `id` and polygon, so its history and network still match.
Re-check `findings_window_hours` against the longest gap between one city's days, plus a day.

## 7. A city a week, written up from three sides

The rota above gives each city a day or two a week, and each city writes only when research finds
a material change, which is rare. This switch gives each city a **whole week**
(`rotation_period` `"week"`) and three editorial goals taken in turn (`editorial_goal_rotation`,
[configuration.md](configuration.md)):

| Day of the city's week | What runs | Goal |
|---|---|---|
| Monday, first research tick (about 00:25 Sydney) | The **weekly reading**: the city's newest clear scene, no change claimed, with its map and planner notes | |
| Monday 09:50 | The article from the weekly reading | `access`: served share, the area beyond reach, the map's heat and outlines |
| Tuesday 09:50 | A revisit of the same Finding | `network`: the rule-based flags, the stations they name |
| Wednesday 09:50 | A revisit of the same Finding | `simulations`: the simulated links, always as a what-if |
| Thursday to Sunday | Nothing, unless research finds a material change (a new Finding starts the goals again) | |

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
most 1000 characters; the topic's `editorial_goals.exclusion_criteria` apply to all three):

```json
[
  {"name": "access", "primary_focus": "Rail access in the day's city through walking reach, from the findings alone. Title it plainly after what was measured. Open by saying what the measures are: built-up density from Sentinel-2 imagery and straight-line walking reach over OpenStreetMap's rail network, not ridership, frequency or travel time. Lead with the share of built-up area within reach of a station and the built-up area beyond it, then the station count, the scene's capture date and coverage, and any change against the city's earlier scenes. Point to the map once, saying what its warm colours and outlined areas show. Give the flags and simulations one sentence at most: they have their own articles."},
  {"name": "network", "primary_focus": "The structure of the day's city's mapped rail network, from the findings alone. Title it plainly after the flag it leads with. Open by saying the flags are rule-based readings of OpenStreetMap's topology and built-up density, not ridership or service. Take each rule-based flag the findings carry in turn (single point of failure, isolated high demand, ghost line): the station or stations it names, what the flag measures (shortest paths through the network, distance to a hub, activity along a chain), and the figure behind it. Give the served share and station count once, as context. Point to the map once: flagged stations are ringed in red. Name only stations the findings name."},
  {"name": "simulations", "primary_focus": "The simulated links for the day's city's mapped rail network, from the findings alone. Title it plainly after the simulation it leads with. Open by saying a simulation is a rule-based what-if on OpenStreetMap's topology and straight-line distances, not a plan, a cost or a recommendation. Take each simulated link the findings carry (orbital link, feeder corridor): the stations it joins, its length and the change in distance to a hub it shows, always in conditional words ('the simulation suggests', 'in the model'). Give the served share and any flag the link answers once, as context. Point to the map once: simulated links are dashed. Name only stations the findings name."}
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

Check `rotation` is the list, `rotation_period` is `"week"`, the three goals are stored, and
`editorial_goals` and `findings_window_hours` (192) are unchanged.

**3. The weekly reading now:** `topics trigger rail-cities --pipeline research_tick`. The log's
`research_tick` line says `material_change`, and `findings list rail-cities --limit 1` shows a
Finding that starts "Weekly reading" about this week's city, with one figure and its planner notes.

**4. Optional: the first article now** (otherwise at the topic's daily slot):
`topics trigger rail-cities --pipeline daily_cycle`. The log says `day_subject=<this week's city>`
and `editorial_goal=access revisit=False`; the article waits in the inbox. Triggered again, it
revisits under `network` (`revisit=True`); a third time, `simulations`; a fourth writes nothing.

**Watching it:** `topics get rail-cities` shows `goal_rotation_state.<city>` (the goal used last
and the goals used on the current Finding); the research state shows each city's
`reported_period`.
