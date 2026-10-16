# The Devpost checklist

What the owner fills in on the OpenCV AI Competition 2026's Devpost form (owner step O7), each field
with where its text or file comes from, and the checks to run before pressing submit. Submit on
Sunday 25 October; Monday 26 is the buffer, and the form closes at **11:45 pm PDT on Monday 26
October** (06:45 UTC, and 5:45 pm AEDT, on Tuesday 27 October).

**First, re-read the rules by hand.** The rubric's headings were read through search results from
the build container, never from the competition's pages themselves. Read the Devpost rules page once
before submitting: the required fields, the video's limit, the awards' opt-ins, and the judging
dates (they decide how long the endpoint must stay up, owner step O11).

## The repository: decide first (owner step O8, by Monday 19 October)

A private repository's download needs a GitHub login with access, so a judge who is not signed in
cannot open it. One of the two:

- [ ] **Public.** Follow [the public repository runsheet](../todo/public-repo-runsheet.md): the
      rulesets, secret scanning, and approval for workflows from forks. The form gets the
      repository's URL and the tag.
- [ ] **Private.** At submission, either attach the tag's archive to the form, if it takes files:

      ```bash
      git archive --format=zip -o bloggerbear-opencv-2026.zip opencv-2026-submission
      ```

      or publish a separate public snapshot repository holding only the tag's tree as one commit
      (which also keeps the old pull requests private). The form gets that archive or that
      snapshot's URL, and the report's front page says which.

Then, on Sunday 25 October, after the report's last numbers are in:

- [ ] Tag the commit and push the tag, and **never move it after submitting**:

      ```bash
      git tag -a opencv-2026-submission -m "OpenCV AI Competition 2026 submission" <sha>
      git push origin opencv-2026-submission
      ```

- [ ] **Do not publish a GitHub Release.** `terraform-production-release.yml` runs on *any*
      published release and applies production at the release's tag, gated only by the
      `production` environment's review: a "submission" release would queue a production apply of
      `dev`'s code. A draft release fires nothing.

## The form's fields

| Field | Source |
|---|---|
| [ ] Title | "Rail Access Monitor: an autonomous publisher that watches a city's rail network from space" ([opencv-2026-submission.md](opencv-2026-submission.md), Title) |
| [ ] The pitch | [opencv-2026-submission.md](opencv-2026-submission.md), One-line pitch |
| [ ] The description | [opencv-2026-submission.md](opencv-2026-submission.md): the summary and the awards' paragraphs, with a link to the report for everything else |
| [ ] The code | the repository's URL and the tag `opencv-2026-submission`, or the archive, or the snapshot repository (above) |
| [ ] The video | the unlisted video's link, at most 5:00 ([video-script.md](video-script.md)); its duration checked below |
| [ ] The working endpoint | the approved rail article's URL on dev's CloudFront domain; or the screen-share booking ([video-script.md, The live demonstration](video-script.md#the-live-demonstration)) |
| [ ] The technical report | the PDF printed from the Markdown page on GitHub at the tag, `docs/hackathon/technical-report.pdf`, and the link to [technical-report.md](technical-report.md) |
| [ ] The diagrams | [diagrams/architecture.svg](diagrams/architecture.svg) and [diagrams/agent-workflow.svg](diagrams/agent-workflow.svg), with their Mermaid sources beside them |
| [ ] The team | each member's name and role (the report's front page) |
| [ ] The awards | opt in to **Best Use of COOL** (the report's [Appendix A](technical-report.md#appendix-a-best-use-of-cool)) and **Agentic Vision** ([Appendix B](technical-report.md#appendix-b-agentic-vision)) |
| [ ] Built with | OpenCV 5, AWS Lambda, AWS Graviton, Amazon Bedrock, Sentinel-2, OpenStreetMap |
| [ ] Licences | the code: Apache License 2.0; the data: Copernicus Sentinel data, the Registry of Open Data on AWS, OpenStreetMap under the ODbL, GDELT, and ESA WorldCover under CC BY 4.0 if the built-up check ran (the report's §8) |
| [ ] Screenshots | the rail figure of the endpoint's article; the trace, step by step; the results slide |
| [ ] How to reproduce | [REPRODUCE.md](REPRODUCE.md) |

## The pre-submit check

Run it at the tag, offline, after the report's last numbers are in and the video is uploaded:

```bash
python scripts/submission_check.py --video-seconds <the video's length in seconds>
```

```powershell
py -3.11 scripts\submission_check.py --video-seconds <the video's length in seconds>
```

It fails, and says why, when:

- a placeholder, `[R2: ...]` or any other bracketed text that is not a link, is left in the report,
  the reproduction guide, the dependency audit, the video script or the Devpost text;
- a repository-relative link in those documents does not resolve (external links are listed for
  you to open by hand: it fetches nothing);
- the rubric tracker has a `missing` row other than those still to come after the check (O7, the
  form itself; O10, the optional production release; O11, keeping the site up through judging) or
  others you accept with `--accept <ID>`, each of which it lists;
- a run file under `docs/hackathon/evidence/runs/` was measured on code that differs from the tag's
  in the vision paths (`lambdas/vision`, `lambdas/vision_worker_handler.py`,
  `lambdas/common/vision_*.py`, `lambdas/common/adapters/rail_access.py`), unless
  `docs/hackathon/evidence/RESULTS.md` lists that run as "measured on older code" with its reason;
- the video's duration, entered by hand, is over 300 seconds, or not entered.

`--ref <commit>` checks against a commit other than the tag (before the tag exists, `--ref HEAD`).
Exit 0 means every check passed; 1 means at least one failed, each failure named.

## After submitting

- [ ] The Devpost page shows every field, the video plays logged out, and the endpoint's URL opens
      logged out.
- [ ] Freeze merges to `dev`, or allow docs-only ones, until the results (owner step O11):
      `terraform.yml` applies dev on every push that touches `infra/`, `lambdas/` or `frontend/`.
- [ ] Run the evidence export again by Thursday 29 October, before 16 October's Findings expire
      (owner step O9).
