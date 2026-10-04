# Friction log

**Started:** 2026-10-03 · **Covers:** the build from Phase 0 (2026-09-12) to today, PRs #1–#165,
plus the hackathon-planning and public-repo session of 2026-10-03, and the Alexa+ planning session
of 2026-10-04 (through #174).

What was harder than it should have been, why, and what we changed. It is for learning, not blame:
a lot of these were found by the process working (a real invocation, a real deploy, a review), just
later than ideal. It doubles as the friction log the
[Amazon Build, Ship, Shape](https://amazonappdev2026.devpost.com) hackathon gives a bonus for, so
AWS platform friction gets its own section.

**Adding an entry:** what happened · why · what fixed it · the lesson · refs (PR, commit).

## Themes at a glance

| Theme | Entries | The pattern |
|---|---|---|
| AWS platform | 11 | errors that don't say what's wrong; limits found only at runtime |
| Terraform and CI | 9 | `validate` passes, `plan` or `apply` fails; scans that cover less than they look |
| Tests vs reality | 6 | the test environment isn't the deployed one |
| Frontend | 4 | the hash router, and a performance fix that broke styling |
| The model in the pipeline | 6 | model output trusted without checking it |
| Agentic coding | 8 | confident output that wasn't checked against reality |
| GitHub and the repo | 11 | free private repos can't protect anything |
| Multi-account and OIDC | 2 | role-chaining trust is easy to get subtly wrong |
| Planning the hackathons | 4 | too many deadlines; ideas the data can't support |
| Alexa+ and MCP | 14 | the rules and the spec say less, or something else, than a first reading |

---

## 1. AWS platform

**1.1 `AssumeRoleWithWebIdentity` fails with no detail.** apply-dev failed with "Not authorized to
perform sts:AssumeRoleWithWebIdentity" and nothing else. Only CloudTrail's error event showed the
real subject claim GitHub sent, `repo:AllainWoodsford@39211453/BloggerBear@1367318003:...`: GitHub
had turned on immutable subject claims (owner and repo IDs appended), so the trust policy's literal
`repo:AllainWoodsford/BloggerBear:...` never matched. **Fix:** wildcard both segments
(`OWNER@*/REPO@*`). **Lesson:** when OIDC fails, read the CloudTrail event first; the claim GitHub
sends is the source of truth, not the docs. (#28)

**1.2 IAM permissions discovered one AccessDenied at a time.** The deploy role listed exact actions.
Each apply or destroy reached further into the resource graph and failed on the next read/list call
the Terraform provider makes internally (`iam:ListRolePolicies`, then `ListAttachedRolePolicies`,
then `ListInstanceProfilesForRole`, `dynamodb:DescribeContinuousBackups`, `sqs:ListQueueTags`...).
Three PRs fixed one gap each before a full inventory widened each statement to a service wildcard on
the same resource ARN pattern; the ARN scoping is what bounds the blast radius anyway. Later gaps
still came: event source mappings (#46, #144), the us-east-1 WAF log group (#103), REST API needs
(#36). **Lesson:** after the second failure of the same class, stop patching and inventory.
(#29–#32, #36, #46, #103, #144)

**1.3 Some IAM actions can't be resource-scoped at all.** `iam:ListInstanceProfilesForRole`,
`logs:DescribeLogGroups`, `cloudwatch:ListTagsForResource` and `states:ValidateStateMachineDefinition`
still failed under `service:*` on the right ARN: they only accept `Resource: "*"`. Nothing in the
error says so. **Fix:** a `NotResourceScopable` statement. (#34, #37)

**1.4 Bedrock model access only fails at invocation.** The default model was set to Sonnet 5, which
wasn't enabled for the account. `plan`, `validate` and the tests all passed; the first real call
returned AccessDenied. **Fix:** switch to the Haiku 4.5 AU inference profile, verified with a real
`converse` call first, and the variable's description now says to do that. (#42)

**1.5 API Gateway HTTP APIs can't have a WAF.** Both APIs were built as HTTP APIs, then migrated to
REST APIs because WAF only attaches to REST APIs. **Lesson:** check feature support (WAF, caching,
usage plans) before choosing the API type. (#35)

**1.6 The CloudFront WAF and its certificate must be in us-east-1.** The project's single-region rule
(ap-southeast-2) needed two documented exceptions, a second provider and a cross-region log group
the deploy role couldn't create at first. (#103)

**1.7 CloudFront doesn't compress by default.** Every asset was served uncompressed despite
`Accept-Encoding: gzip, br`: `compress` defaults to `false`. One line, found only by checking the live
response headers. (#119)

**1.8 Lambda creates its own log group if it runs first.** After a destroy, the rebuild failed with
`ResourceAlreadyExistsException`: the public API got a request and Lambda created
`/aws/lambda/...-public-api` (with no retention) nine seconds before Terraform did. **Fix:** every
function `depends_on` its log group, which also orders the destroy. (#145)

**1.9 Scheduler failures are silent.** Two schedules' role couldn't invoke their Lambdas. They had
**never run** — about 76 failed attempts a day in production — and nothing alarmed. Found because
the Stats page's historic totals were all zero. **Lesson:** alarm on Scheduler's failed-invocation
metric, not just on Lambda errors. (#145)

**1.10 Cost Explorer: manual, us-east-1 only, a day behind, account-wide.** It can't be enabled from
Terraform or the CLI, its endpoint is us-east-1 only, and its data lags about 24 hours. It reports the
whole account, so a second project in the same account would show up on BloggerBear's public Stats
page. On-demand Bedrock calls carry no tags, so tag filtering doesn't separate them. (2026-10-03
session; `lambdas/common/cost_explorer.py`)

**1.11 AWS Open Data sits in US regions.** For the OpenCV entry: Himawari is in us-east-1 and
Sentinel-2 in us-west-2; BloggerBear is in ap-southeast-2. Cross-region reads mean latency and
possibly transfer cost. (2026-10-03 session)

## 2. Terraform and CI

**2.1 `validate` passes, `plan` fails.** The edge dashboard used `cond ? [] : concat(...)`: an empty
tuple and a 17-element one are different types, so every plan failed with "Inconsistent conditional
result types". `validate` checks types without real values, so it passed. The dev applies for #158
and #159 both failed before anyone noticed. **Fix:** the `flatten([for ...])` idiom, plus a
`terraform test` with a mocked provider that runs on every PR. (#160)

**2.2 A tfvars value silently beat the variable's default.** `bedrock_model_id = ""` was left in both
environments' tfvars, so raising the default in `variables.tf` did nothing and every Lambda got an
empty model ID. Reading `variables.tf` made it look configured. **Lesson:** check with
`terraform console`, not by reading the file. (#40)

**2.3 A local build artifact assumed a persistent machine.** The Lambda package build only re-ran
when its inputs' hash changed. CI runners are new VMs every run, so the "unchanged" build directory
didn't exist: "could not archive missing directory". A `depends_on` on the data source wasn't enough
to defer its read either. **Fix:** always rebuild, and make the archive reference the build
resource's ID. (#41)

**2.4 The packaging script didn't run on Windows.** `local-exec` assumed a POSIX shell, then
`python3` turned out to be the Windows Store stub. **Fix:** fall back to `py -3`. (#48, #49)

**2.5 A PR-time `terraform plan` can't work without credentials.** By design, PRs get no AWS
credentials, but the config reads `aws_caller_identity` and the provider validates credentials on
init. It had failed on every PR since Phase 0. **Decision:** PRs run `validate` and `terraform
test` only. (ef1afee, #156)

**2.6 Path filters skipped a deploy.** `terraform.yml` triggered only on `infra/**`, so #146 (Lambda
and frontend only) never deployed. **Fix:** the trigger covers everything the apply deploys. (#151)

**2.7 A broken gatekeeper workflow failed on every push.** `dev-gatekeeper.yml` had invalid YAML (a
duplicate `with:`), ran `npm ci` in a Python/Terraform project, and used the wrong action inputs.
Repaired in #14 and retired in #151, since its Claude review step had no credentials and did nothing.
The copy left on `master` was a **0-byte file** that failed on every `master` push until #165.

**2.8 Scanners that looked thorough, and covered far less.** Reading the logs (2026-10-04) showed:
Trivy's dependency scan saw only `lambdas/requirements.txt` (it was pointed at `lambdas/`, and
it only recognises files named exactly `requirements.txt`, so the dev requirements and
`scripts/requirements.txt` were never checked); its secret scan skipped `frontend/`, `infra/`,
`scripts/`, `docs/` and `.github/`; and its summary table lists only files with findings, so a
clean secret scan looked like it had scanned one file. Only HIGH and CRITICAL were ever reported,
so five MEDIUM CVEs (`requests` x2, `markdown`, `pytest`) went unseen. Bandit never scanned
`scripts/`, and 3,908 test `assert`s buried its figures. Trufflehog's action was SHA-pinned but ran
`version: latest`, pulling a moving Docker image; Trivy came from apt, also unpinned. **Fix:**
whole-repo scans, the dev requirements included, MEDIUM reported without failing, pinned and
hash-verified binaries, the three packages bumped, and an on-demand workflow that runs everything
over the whole history at every severity. **Lesson:** a green check says nothing about what it
looked at; read what it scanned at least once.

**2.9 A dashboard only production creates failed production's apply.** The v0.2.6 release stopped on
`PutDashboard` with 84 validation errors ("metrics/0 Should be array"). One widget built its
`metrics` with `flatten()`, which goes all the way down: six metric lists became 42 loose values.
`validate` and `plan` both passed, because Terraform sees the dashboard body as a string and only
CloudWatch checks its shape, at apply. And dev never creates that dashboard (it costs US$3 a month),
so production was its first apply. **Fix:** `concat(...)` for one level, and a `terraform test`
assertion on the shape CloudWatch insists on. **Lesson:** a resource one environment skips is
untested until the release; give it a test that doesn't need the environment. A `Release` tag on
every resource was considered and dropped: dashboards can't carry tags, and it would have put a
change on every resource in every plan. (#174)

## 3. Tests vs reality

**3.1 The tests passed, but the deployed Lambdas couldn't import `requests`.** The zip never included
third-party dependencies. pytest runs where `requirements.txt` is installed, so nothing could catch
it, and **no adapter-based pipeline could ever have produced a Finding**. Found by the first real
`aws lambda invoke`. (#38)

**3.2 Hardcoded dates in fixtures.** The digest's "recent" fixture was a fixed date that aged out of
the 48-hour window, and the suite broke by itself. A second fixture (`test_without_a_pin...`) had
the same problem against the 24-hour news window, noted in #127. **Lesson:** fixtures relative to
`now()`, or a frozen clock. (599c174, #127)

**3.3 A behaviour change made an old test flaky.** #122 let the bear wear no armour; one test still
relied on the real random module, so it failed about half the time. (#123)

**3.4 DynamoDB numbers broke the admin API.** DynamoDB returns `Decimal`, which `json.dumps` can't
write, so `GET /topics` answered 500 as soon as any topic stored a number. Mocks and fixtures used
plain ints. (#104)

**3.5 Cost lineage under-reported.** The article summary showed the authoring cost only, not
authoring plus research. (#125)

**3.6 117 local failures that weren't ours.** On the Windows machine the Lambda suite fails 117
tests with or without a change: Python on Windows has no time zone database, and `tzdata`, pinned in
`lambdas/requirements.txt`, wasn't installed, so everything touching the feedback limits'
`Australia/Sydney` day fails. Telling a regression from that noise took a second full run on a clean
tree and a comparison of the two lists. **Lesson:** install the pinned requirements before trusting a
local run, and compare failures against a baseline, not against zero. (2026-10-04)

## 4. Frontend

**4.1 The Lighthouse fix that unstyled the site.** To fix render-blocking CSS and layout shift (CLS
0.384), #125 switched stylesheets to `preload` plus a JS swap. The swap raced: `preload-styles.js`
attached its `load` listener after cached stylesheets had already loaded, so `rel` never flipped and
pages rendered **unstyled** (remembered as "the JavaScript stopped loading"; the cause was the CSS
swap). **Fix:** back to plain blocking `<link rel="stylesheet">` (about 440 ms, but both files
together are about 5 KB gzipped), keeping the shim for already-published static pages. **Lesson:**
a performance score isn't worth a correctness risk at this size; test cached and uncached loads.
(#125, #127)

**4.2 The hash router ate fragment links, twice.** The SPA routes on the whole `location.hash`, so
`#top` (back-to-top) and later `#total-stats-heading` (Stats Quick Links) both routed to "Page not
found". The second was the same bug as the first, in new code. **Lesson:** one shared helper for
in-page links. (#26, #119)

**4.3 The legal pages lived inside `app.js`.** Terms and Privacy were rendered client-side (#23):
they needed JavaScript, had no real URLs, weighed about 12 KB in every page load and were harder to
link to by section. Moved to static `terms.html` and `privacy.html`, with redirects from the old
routes (#143). **Lesson:** content that doesn't change belongs in static HTML, not the SPA.

**4.4 A blanket CSS rule leaked.** `main h2`, meant for one homepage heading, shrank the legal pages'
headings. Scoped to a class. (#26)

## 5. The model in the pipeline

**5.1 Silent truncation at 1,024 tokens.** Drafts and research summaries used the default
`max_tokens=1024`. One article's body stopped mid-word ("...deep institutional liqu"). Bedrock said
`stopReason: max_tokens`; nothing read it. **Fix:** larger limits, one retry, and a still-truncated
draft is held for moderation. (#78)

**5.2 A refusal became a title.** Ideation refused one day; the parser treated the refusal's
sentences as angles, picked the first, and the published title was the model's confused reply ("I'd
be happy to help you write an article title, but I'm not sure..."). **Fix:** a plausibility check on
angles and titles, a retry, and a hold for moderation when it still fails. (#117)

**5.3 The same story two days running.** Ideation only saw today's findings, so a repo trending for
days looked new every day. **Fix:** the ideation prompt gets the topic's last five titles under
"Already covered recently". (#117)

**5.4 A musing with a mood and no words.** A musing went out on 4 October as "BloggerBear was
feeling proud", a link, and nothing else. The model had answered with an empty string, and the
article and feedback musings stored whatever came back; the loot and rejection musings already fell
back to plain text. **Fix:** the same fallback for all four. **Lesson:** an empty reply is not an
exception, so nothing catches it; check for it wherever a reply is published. (2026-10-04)

**5.5 An article published inside a code fence.** An article published on 2 October with no person
involved has `**"..."**` around its title and its whole body inside a ` ```markdown ` fence, so the
page shows it as a scrolling block of code. The model wrapped its reply; nothing stripped or
rejected the wrapper. Found by a person reading the site, two days later. **Not fixed yet, on
purpose:** it is the demo case for the operator assistant's content checks. The pipeline guard that
would have held it is still to write. **Lesson:** 5.1, 5.2 and now this are the same failure: the
reply's shape was assumed. (2026-10-04)

**5.6 Fixing an article took it off the site first.** `articles rewrite` unpublished a published
article before the rewrite had even started, so a rewrite that failed left a good article down and
waiting in the inbox. For a one-sentence fault (5.4) that is a poor trade. **Fix:** the article
stays up until the rewrite has been written, guarded and reviewed, and only then comes down;
`--force` keeps the old order for an article that must come down now. A review of the change found
three ways a still-published article could land in the inbox, where rejecting it would have left its
page up; each got a fix and a test. **Lesson:** "take it down, then fix it" is only the safe order
when being down is cheaper than being wrong. (2026-10-04)

## 6. Agentic coding

Most of this project was built with AI coding agents (Claude Code, plus Copilot early on). These are
the places that went wrong, and what each says about working this way.

**6.1 Confident CI that never ran.** The gatekeeper workflow (2.7) looked thorough — "adversarial
security review" — but was invalid YAML with wrong inputs, and later had no credentials. Green-looking
automation needs one check that it actually did something.

**6.2 Docs that overclaimed.** PROGRESS.md claimed an automated CI regression check for a contrast
fix; none existed (corrected in 74af538). The README said production deploys need a reviewer's
approval; the environment had no reviewer, because private repos can't have one on the free plan
(found 2026-10-03). Since #164, the README describes a branch ruleset that only exists once the repo
is public and the runsheet has been applied. **Lesson:** a doc claiming a control exists should be
checked against the setting itself.

**6.3 Whack-a-mole instead of stepping back.** The IAM gaps (1.2) were fixed one PR at a time,
across three PRs, before anyone inventoried. An agent fixes exactly the error in front of it unless
it's told to look for the pattern.

**6.4 "Verified" meant `validate`.** Several fixes (#38, #40, #42) were each "verified" by
`terraform validate` and the tests, and each failed on the first real invocation. The later PRs
learned this and verified against reality: a real `converse` call, `terraform console`, a real
invoke.

**6.5 A spec with made-up facts.** The hackathon spec for this session (itself AI-assisted) named a
nonexistent NVIDIA model ("Llama-3-Nevis"), gave a wrong deadline (11:59 instead of 11:45 pm PDT),
and said videos were "not permitted" for Qloo (they're just not required). Checking the four rules
pages fixed all three.

**6.6 Small errors from me (Claude) in this session.** I first suggested GOES imagery for an
Australian topic (it covers the Americas; Himawari covers Australia). I said member account IDs were
in the current docs (they were only in git history). I wrote a PR body claiming the bootstrap domain
"matches what was already applied", without checking, then softened it. Each was caught by
re-checking.

**6.7 PR housekeeping slips.** #81 duplicated #70 from the wrong branch (closed), and #118 reused a
branch name with another PR's title. Minor, but it makes history harder to read.

**6.8 The permission system and the agent.** Changing GitHub repo settings through the API was
blocked by the agent's auto-mode safety check (2026-10-03), so the settings went into a script and
then the runsheet for a person to run. That was the right outcome for outward-facing changes, but it
means some steps can't be fully hands-off.

## 7. GitHub and the repo (2026-10-03)

**7.1 Free private repos can't protect anything.** Branch protection, rulesets and required
reviewers on environments all return "Upgrade to GitHub Pro or make this repository public". The
README's "approval-gated production" had no setting behind it. **Plan:** apply them the moment the
repo goes public (docs/todo/public-repo-runsheet.md).

**7.2 A hand-added rule on `production`.** The environment also allowed the `dev` branch, so any
workflow on `dev` could have taken the production deploy role. Removed; only `v*` tags now.

**7.3 Curly quotes in `user.email`.** The global git config held
`“****”` (a TAFE NSW address), smart quotes included, probably pasted in. Invalid, so
GitHub couldn't link 175 commits to the account. **Fix:** the no-reply address, and GitHub's "block
command line pushes that expose my email".

**7.4 Git 2.19 is too old for `git filter-repo`** (2.22 or later). The rewrite used `filter-branch`
on a separate bare clone, so the working copy and its uncommitted changes were never at risk.

**7.5 A history rewrite doesn't reach PR refs.** GitHub keeps every PR's original commits read-only
(`refs/pull/*`, 159 PRs); only GitHub Support can purge them. Decided to accept that.

**7.6 145 stale local branches** still pointed at the old history; pushing any would have brought it
back. All deleted after checking each one's tip was on GitHub (or redundant).

**7.7 A 0-byte workflow on `master`** failed on every push since 2026-09-13 (2.7). Removed in #165.

**7.8 The Claude issue worker would have run for anyone.** On a public repo, anyone can open an
issue, and the issue body became the job's prompt, with write access and the owner's token. Now
limited to the repo owner (#163).

**7.9 A personal email in a variable default.** The budget alert email was about to be committed as
a default in `infra/bootstrap/variables.tf`. Moved to the gitignored local `terraform.tfvars`
(#163).

**7.10 PRs and issues limited to collaborators; Discussions instead.** PRs and issues are where
untrusted text enters the automation: the Claude worker reads issue bodies, and fork PRs run
workflows. Limiting both to collaborators closes that surface, on top of the owner-only check in
#163, and matches "PRs aren't expected". Issues stay on for the owner's own tracking, including
`@claude` tasks. Everyone else uses Discussions, which trigger no workflows; security reports still
go only through private vulnerability reporting. PRs use GitHub's permanent `collaborators_only`
setting. **To check after going public:** how issues are restricted. If it's interaction limits,
they expire after six months at most.

**7.11 The address we'd just removed went back in, in this log.** Entry 7.3 was first written with
the full TAFE address, on the same day 175 commits were rewritten to remove it, and merged in #166
(Claude wrote it; nobody caught it in review). Nothing failed, because no check looked for personal
data: Trufflehog and Trivy look for credentials, and an email address isn't one. The operator
first decided to leave it (the address is unused, and the repo carries their name anyway), then had
it replaced with `****` in 7.3; it remains in #166's commit in the history. What changed is the
checks (#167):

- **Gitleaks** in `pr-checks.yml`: its default secret rules plus email addresses (allowlisting
  reserved and no-reply domains) and AWS account IDs. Run against #166's commit, it fails on
  exactly that line.
- **`scripts/pii_denylist_check.py`**: exact personal strings, read from the `PII_DENYLIST` secret
  in CI or a gitignored `.pii-denylist` locally, so the list is never written anywhere public.
  Findings name the file, line and entry number, never the entry.
- **A pre-commit hook** (`.githooks/pre-commit`) running both, because a PR check runs after the
  push, and on a public repo the push alone publishes the content.

**Lesson:** a value you're protecting needs a check that runs before content leaves the machine,
not a reviewer remembering. Writing a sensitive value into the doc about protecting it is easy
when the doc's job is to describe exactly that value.

## 8. Multi-account and OIDC

**8.1 Role chaining on another project (AiSandbox).** The setup there: GitHub OIDC → a role in an
identity account → `sts:AssumeRole` into `OrganizationAccountAccessRole` in each environment's member
account. The assume-role steps kept failing. Things worth checking next time:

- The subject claim GitHub actually sends (1.1). Immutable subjects bit this project too.
- `OrganizationAccountAccessRole` trusts the **management** account by default. If the identity
  account isn't the management account, the member role's trust policy must name the identity role
  explicitly.
- Both sides must allow it: the identity role needs `sts:AssumeRole` on the target ARN, **and** the
  target's trust policy must allow the identity role.
- Chained role sessions are capped at one hour, and `aws-actions/configure-aws-credentials` needs
  `role-chaining: true` for the second hop.
- CloudTrail in **both** accounts shows which hop failed and why.

**8.2 Why BloggerBear got away with one account.** It's serverless, so dev and production share no
running servers and don't affect each other much. Separation comes from naming (`bloggerbear-dev-*`
and `bloggerbear-production-*`), separate S3 buckets, separate Terraform state per environment, a
deploy role per environment (dev trusts the `dev` branch; production trusts the `production`
environment) and resource-scoped IAM. The costs of one account: Cost Explorer can't tell the
environments apart (1.10), and Bedrock and Lambda quotas are shared per account and region. A
public hackathon demo in the same account could throttle the daily cycle.

## 9. Planning the hackathons (2026-10-03)

**9.1 Four deadlines in four weeks** (Oct 23, 26, 30, 30) for a solo builder. The plan narrows to two
or three entries.

**9.2 The first OpenCV topic was too risky.** A bushfire watch would publish safety information
a day late, with false reassurance as its worst failure. Switched to counting ships at shipping choke
points, written up as observations (docs/risks/opencv-bushfire-watch-01.md,
docs/enhancements/supply-chain-tracker-enhancement.md).

**9.3 Satellite resolution vs the idea.** "Count shipping containers" isn't possible at Sentinel-2's
10 m resolution (a container is 2.4 m wide); ships are.

**9.4 OpenCV 5 on Graviton is unconfirmed.** Whether an `opencv-python-headless` 5.x wheel exists for
Linux aarch64 is still the first thing to settle.

## 10. Alexa+ and MCP (2026-10-04)

Planning the Alexa+ entry, before any of it is built. The design these led to is
[docs/enhancements/alexa-plus-operator-assistant-enhancement.md](enhancements/alexa-plus-operator-assistant-enhancement.md).

**10.1 Our first idea was the rules' own example of an obvious one.** The plan was a public MCP
server wrapping the public API, answering one question at a time. The judging criteria name exactly
that for Alexa+: "single-turn Q&A bot, basic MCP wrapper around an existing API", against "agentic
workflow that orchestrates across services autonomously, context-aware add-on that maintains state
across sessions". The first rules check had read the eligibility rules and not the criteria.
**Changed:** the entry is now an operator's assistant that works out what needs attention across
services and remembers what it suggested; the public wrapper is parked. **Lesson:** read the judging
criteria before the design, not after.

**10.2 The minimum spec version and the linked one work differently.** The rules require MCP
`2025-11-25` or later; the hackathon's resources link to `2026-07-28`, which drops the `initialize`
handshake, sessions and the GET stream. That suits a Lambda, but a client that only speaks
`2025-11-25` can't talk to a `2026-07-28`-only server, and neither the spec pages nor the MCP Apps
page says which Python SDK release speaks which version (the MCP Apps page names TypeScript packages
only). **Decided:** `2026-07-28` first; the first day's work is finding out what the Python SDK and
Strands' MCP client each speak. **Feedback:** a table of SDK release against protocol version, next
to the spec, would have answered this in a minute.

**10.3 "Streamable HTTP" was read as "must stream".** A draft transport decision treated Streamable
HTTP as Server-Sent Events and planned API Gateway response streaming for it. The spec lets a server
answer every request with one JSON object, which is all read-only tools need, and which an ordinary
API Gateway and Lambda integration already does. **Lesson:** the transport's name describes what it
can do, not what a server must do.

**10.4 A layer ARN for the wrong region.** The same draft carried the Lambda Web Adapter's layer ARN
for us-east-1; the stack is in ap-southeast-2 and layers are regional, so it would have failed at
apply. Caught in review, before any apply. **Lesson:** an ARN copied from an example carries the
example's region.

**10.5 Every Lambda shares one role.** All the functions use one execution role with write and
delete on every app table. That was fine while every function was ours and did pipeline work; a
"read-only" assistant on that role would not be read-only. **Decided:** the MCP server gets its own
role, scoped to what its tools read, with one table it may write.

**10.6 The admin API's protection can't be reused.** Admin requests are IAM-signed and behind a WAF
IP allowlist that fails closed; the console is a local CLI so no browser holds credentials. A voice
page can't hold IAM keys, and a judge's network isn't on the allowlist, so the assistant is a second
way in, with its own sign-in (Cognito, MFA in production). The MCP authorization spec's own flow is
left out of the hackathon build: a page we control doesn't need it, and it was the largest unknown
in the schedule. Whether Alexa+ developer access works from Australia is still unknown, so the entry
simulates the experience in a web page, which the rules allow.

**10.7 A switch in a table can't be a WAF rule.** The operator wanted to be able to lock the
assistant to known addresses later, from a configuration table, without a deploy. WAF rules are
Terraform's; a table row can't change one. **Decided:** the check is in the Lambdas, read from the
existing config row on every request, and a failed read refuses the request.

**10.8 Managed memory stores the wrong thing for this job.** AgentCore Memory keeps conversation
turns and facts a model extracts from them. "Did I fix what you suggested?" needs an exact answer,
and text extracted from tool output is a way for hostile text in a draft or a log line to be
remembered. **Decided:** the assistant's memory is a DynamoDB table of what it suggested (kinds, ids
and timestamps, no text), re-checked in code each session; AgentCore Memory is optional, and only
for the operator's own words. Unconfirmed: whether it is available in ap-southeast-2.

**10.9 A suggestion is a command someone will run.** The assistant shows the operator the
`admin_cli` command that would fix what it found. If the model wrote that command, text in an
article could put `topics delete` on the screen. **Decided:** commands come from a fixed catalogue in
code, with ids read from the tables; the model only chooses which findings to talk about, and
nothing in the catalogue deletes.

**10.10 The agent SDK caps the MCP SDK.** The newest `mcp` is 2.3.0, but `strands-agents` 1.57.2
requires `mcp<2.2`, so installing both fails to resolve. **Decided:** pin `mcp==2.1.1`, which
Strands accepts and which already speaks `2026-07-28`. **Feedback:** the cap is easy to miss until
the two are installed together; it would help to see it stated next to Strands' MCP instructions.

**10.11 Every `mcp` 1.x example fails to import on 2.x.** FastMCP was renamed `MCPServer` and
moved. The SDK handles it well: the old import raises an error that names the new one and links
the migration guide. Most examples found by searching are still 1.x.

**10.12 The first request to the server was a 421.** The SDK checks the `Host` header against a
list, as well as `Origin`, and a test client's host isn't `localhost`. Nothing in the spec's
transport page mentions a Host check; it is the SDK's own protection against DNS rebinding. Behind
API Gateway the host is the API's domain, so an unset list would have refused every request in
the first deploy. **Fix:** the lists come from the environment, an empty one refuses everything
on purpose, and a contract test holds both. **Lesson:** run the real SDK in a test before
writing any Terraform for it.

**10.13 A new requirements file is invisible to the vulnerability scan.** Same as 2.8: Trivy only
reads files named `requirements.txt`, plus the patterns it is given, and the MCP server's
dependencies live in `requirements-ops-mcp.txt` so the pipeline's shared zip doesn't carry them.
**Fix:** the pattern in both scan workflows now names it. The file pins only `mcp` itself, so what
`mcp` pulls in is still not scanned; a lock file for that Lambda's build is the proper fix.

**10.14 The `allowlist` setting refused the assistant's own agent.** The access check was written
for the MCP server and tested there, then repeated in the agent's handler and tested there. Each
passed. Together, the agent admitted the operator and then called the server from Lambda's address,
which is on nobody's list, so every question under `allowlist` ended in a 502. It failed closed,
and it was noticed while writing the design doc, not by a test: no test ran the two checks in one
request with a setting other than `open` or `off`. **Fix:** the agent sends the server a key only
the two functions hold (Terraform makes it) and the address of the operator it has admitted; a
request with the right key is judged by that address. The key admits nobody by itself, and a wire
test now runs agent and server together under `allowlist`. **Lesson:** a rule enforced in two
places needs one test that goes through both, with every value of the setting.

---

## Patterns worth keeping

1. **Real invocation beats static checks.** `validate`, mocks and green tests missed the worst bugs
   (missing dependencies, model access, tfvars override, the type error). Each fix that stuck was
   checked against the real thing.
2. **Read the error's source, not the error.** CloudTrail for STS, Bedrock's `stopReason`, the live
   response headers for compression.
3. **Second failure of a kind → stop and inventory.**
4. **Alarm on "never ran", not just "failed".** Silent schedules went unnoticed for days.
5. **Docs describe settings, so check the settings.**
6. **Prefer boring.** Blocking CSS, static legal pages, one account: each simpler option worked
   better than the clever one it replaced.
