# The video, and the live demonstration

The submission's video: **at most 5:00, uploaded unlisted**, showing the team, the application
working, its architecture and its principal results. It is **recorded as clips as things happen,
and edited once**, never filmed in one take: the live triage is filmed on its day (owner step O3),
the Graviton terminal during the COOL session (O5), the Alexa+ answer once it has been timed on
dev; the edit is on Saturday 24 October (O6). Its link goes into the
[technical report](technical-report.md)'s front page and the Devpost form
([devpost-checklist.md](devpost-checklist.md)).

The narration below is also the transcript: with captions on, it is what a viewer reads. Numbers
the evaluation has not produced yet are marked `[R2: ...]`; the edit uses the run files' values.

## The shot list

| Time | Scene | Shows | Rubric |
|---|---|---|---|
| 0:00-0:20 | The team | The owner on camera, name and role; anyone else on the team | S6: the team |
| 0:20-0:45 | The problem and the users | A Sentinel-2 view of Sydney, then OpenStreetMap's rail lines over it; who reads the articles and who runs the pipeline; SDG 11.2.1 as the yardstick, and how the measure differs from it | S1 |
| 0:45-1:20 | Architecture | [diagrams/architecture.svg](diagrams/architecture.svg), zoomed to each part as it is named: the two Regions, the worker beside the imagery, the agent, the person, the assistant, COOL benchmarked apart; then [diagrams/agent-workflow.svg](diagrams/agent-workflow.svg), the gates and the agent's loop | S4, A1 |
| 1:20-2:00 | It working | A forced tick of `rail-cities` (the clip sped up), naming the day's city of the rota; CloudWatch Logs Insights query (c) on that tick, zoomed on its `vision_site` and `vision_decision` rows; a cut to us-west-2 for the worker's `vision_measure` row (build, CPU, timings); the figure | S5, S6 |
| 2:00-2:50 | OpenCV's output changes a decision | The trace (the assistant's step-by-step view of one decision on its page, or the replay's Markdown), zoomed: the coverage and the gate, "moved, from the imagery, so the agent was asked", `look_again` with what OpenCV re-measured beside the same scene at the topic's settings, the verdict, the action; then one line with the counterfactual result | A2 |
| 2:50-3:00 | A failure, handled | The cloudy scene of 5 January 2024: its scene-classification breakdown and its `low_coverage` flag; no model call, no Finding | S7, A3 |
| 3:00-3:25 | The person | The review inbox, `approve`, the published page with the map, its caption and the credits | A3, S5 |
| 3:25-4:10 | Results | **One slide, three numbers:** E3's false-alarm rate with its interval; the agent's balanced accuracy against the two baselines; COOL's compute speed-up on one core with its interval, and "parity: pass" | S7, C1-C3, A3 |
| 4:10-4:40 | Ask it | Alexa+: "ask BloggerBear what the vision agent did for rail access this week", and its spoken answer | T1, A3 (observability) |
| 4:40-5:00 | Limits and responsible use | Proxies, not people; a person approves everything; what is not measured; the links | S1 (§7, §8) |

## The narration

**0:00 The team.** [R2: each person's name and role, in their own words, under 20 seconds]

**0:20 The problem.** "Where a city builds and where its trains reach drift apart slowly. The
numbers that would show it are in open data nobody reads every week: Sentinel-2 passes over every
city every few days, and OpenStreetMap maps every line and station. The Rail Access Monitor reads
them for four cities and writes an article when something moved. Its yardstick is the UN's
indicator of convenient access to public transport, with two differences we say every time: built-up
area, not people; a straight line, not the walking route."

**0:45 Architecture.** "Everything with state lives in Sydney's AWS Region. The vision worker,
OpenCV 5 on an arm64 Lambda, runs in Oregon, beside the imagery: it range-reads only the tiles under
a city and sends back a few kilobytes and a map. One topic rotates through four cities, one a day.
The research tick looks at a city each hour, measures any new scene, compares it with that city's
own baseline, and calls the triage agent only when a number moved. The agent can re-measure the scene with OpenCV, look at
earlier scenes, and read the news; anything unsure is an artefact. A person approves every article.
COOL, OpenCV's Graviton build, is benchmarked against stock OpenCV on Graviton4, not deployed."

**1:20 It working.** "Here a tick measures [R2: the city of the filmed tick]. These are its
evidence lines: the site's turn, then the decision, joined by the worker's request id to the
worker's own line in Oregon, with the OpenCV build, the CPU, and how long each stage took. And here
is the map it drew: built-up density in warm colours, areas beyond a kilometre of a station
outlined, the network by how many paths run through it."

**2:00 A decision.** "This is one decision, step by step. The scene was [R2: the scene's coverage]
clear. The served share moved past the topic's threshold, from the imagery, so the agent was asked.
It re-measured the scene with OpenCV at other settings: [R2: the look_again row's numbers, beside
the same scene's numbers at the topic's settings]. It judged the change [R2: the verdict], and
[R2: the action]. With only OpenCV's re-measurement changed, the verdict changed in [R2: k of n]
runs."

**2:50 A failure.** "This January scene was mostly cloud: only 15 per cent of the city could be
seen. It is flagged, kept, and never compared: no model call, no Finding."

**3:00 The person.** "Nothing publishes without a person. The article waits in the inbox; approving
it publishes the page, with the map and the Copernicus and OpenStreetMap credits."

**3:25 Results.** "On a year of clear scenes of an unchanged network, [R2: E3's false-alarm count
and rate, with its interval]. On cases built to be real and cases built to be artefacts, the agent's
balanced accuracy is [R2: the number], against [R2: the two baselines' numbers]. On Graviton4, COOL
ran the same handler on the same Sydney scene [R2: the speed-up, with its interval] on the compute
stages, with the same results."

**4:10 Ask it.** "And the operator can ask." (Then the Alexa+ question and its spoken answer, as recorded.)

**4:40 Limits.** "These are proxies, not people: built-up area from reflectance, and straight-line
reach. A person approves every article, and every article says what was not measured. The code,
the report and every number's source are linked below."

## Recording notes

- **Record at 1080p.** Crop or zoom every Logs Insights view and both diagrams so their text reads
  at 1080p. The diagrams are SVG, so zooming in the edit loses nothing; no separate crops are
  needed.
- **Captions on**; this narration is the transcript, committed here.
- **The Logs Insights tick is the committed one.** Run query (c) on the same tick id the committed
  trace shows, so the footage and the files agree
  ([the runsheet's evidence queries](../deployment-runsheet.md#evidence-what-the-pipeline-measured-and-decided)).
- **Time Alexa+'s answer on dev before filming.** `vision_evidence` holds itself to 12 seconds; if the
  spoken answer comes near Alexa's limit, film the page's answer and keep the voice question to
  counts. If the assistant is not answering on dev, film query (a) in Logs Insights instead.
- **Ask for the topic by its name.** The rotating topic's name is "Rail access", so "rail access"
  matches it exactly; "rail Sydney" is closer to the demo topic, "Rail access: Sydney (demo)", and
  the assistant would take that one. Ask about the demo topic as "the Sydney demo".
- **Disclose the demo topic** whenever its trace is on screen: its thresholds are 0, so the agent
  runs whatever the numbers did.

## The redaction checklist

Tick every line before uploading.

- [ ] No AWS account id anywhere: the console's header, any ARN, any log group's ARN, the `@log`
      column of Logs Insights (hide it, or show only `@timestamp` and the fields).
- [ ] No sign-in e-mail address on `ask.html` or in the Cognito console.
- [ ] No admin API host name, and no CloudFront distribution id beyond the public site's address.
- [ ] No account or device name in the Alexa app.
- [ ] No running state and no agent note on screen beyond the trace's "(unverified)" cell.
- [ ] No terminal prompt that shows a user or machine name; no `~/.aws` file, environment variable
      or credential on screen.
- [ ] The video is unlisted, at most 5:00, and its duration is entered in
      `python scripts/submission_check.py --video-seconds <s>`.

## The live demonstration

**Primary: a working URL.** The approved rail article's page on dev's CloudFront domain, which is
public (`infra/environments/dev/main.tf:475`), or on bloggerbear.com only if the owner also makes a
production release (O10, recommended against). It shows the map, its caption and the credits. The
report and the Devpost text say the article is a city's first observation, measured but not triaged
(a first observation has no baseline to compare), and point at the trace for the agent. The
repository at the tag and this video accompany it. After the tag, the endpoint is kept up through
judging (O11): merges to `dev` frozen or docs-only, the topic running, the demo topic slowed down,
and the article URL opened weekly, logged out.

**Secondary: a live screen-share,** if the organisers offer one, booked through Devpost. The
assistant's page sits behind Cognito, so judges cannot use it themselves; the screen-share is the
video's scenes, live:

1. "How is the vision pipeline doing?" (`vision_status`): which cities were measured, and why the
   others were not.
2. "Walk me through the latest scene for rail access" (`vision_evidence`, `scene="latest"`): the
   trace, step by step.
3. Logs Insights query (c) on that tick.
4. The article page.
5. One question by voice to Alexa+: "what did the vision agent do for rail access this week?"

**The fallback,** when no live triage happened before the deadline: the replay trace of the
`sydney-demo` sequence, labelled as a replay ([the report's Appendix B](technical-report.md#appendix-b-agentic-vision)).
