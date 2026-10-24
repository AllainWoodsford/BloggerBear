# Agentic Vision evidence

The evaluation's real-data runs for the Agentic Vision award, made on 20 October 2026 with the
deployed dev vision worker (stock OpenCV 5.0, arm64 Lambda in us-west-2) and Claude Haiku 4.5 on
Amazon Bedrock. They follow steps 2 to 7 of
[the evaluation's real-data runs](../../../evaluation-runsheet.md#the-evaluations-real-data-runs),
with `--out` pointed here. [RESULTS.md](RESULTS.md) indexes every run file
(`python scripts/vision_eval.py index --evidence docs/hackathon/evidence/agentic-vision`); the
[technical report](../../technical-report.md#appendix-b-agentic-vision) cites them.

## What the rubric asks, and where it is

| The rubric | Here |
|---|---|
| An agent workflow diagram | [agent-workflow.svg](../../diagrams/agent-workflow.svg) |
| A trace showing OpenCV 5 output changing a later decision, tool call or action | [replay/sydney-demo.md](replay/sydney-demo.md), and the counterfactual run file below |
| Task success, failure handling, observability and human control | the `agent` and `failures` run files below; the report's Appendix B |

## The runs

| Run | What it shows | Result |
|---|---|---|
| [`failures`](runs/2026-10-20-eval-failures-amd64-opencv.json) | The failure catalogue's real rows: cloud, swath no-data, smoke, a site off the scene, a window too large, an unreadable scene, a URL off the allowlist, the wrong build | 8 of 8 as expected |
| [`access-baseline`](runs/2026-10-20-eval-access-baseline-amd64-opencv.json) | E5: the production access method against two yardsticks | served share 0.3895 against 0.3901, IoU 0.991 |
| `replay … -bedrock` ×3 | Three Sydney sequences, tick by tick, with the deployed worker and the real model: the traces in [replay/](replay/) | 6 changes sent to the agent: 5 artefact, 1 real (the constructed new line) |
| `replay … -recorded` ×3 | The same three, re-run with no AWS account from the recordings, every measurement recomputed on x86 | all reproduced: every decision equal, every worker call within the parity tolerances |
| [`agent`](runs/2026-10-20-eval-agent-amd64-opencv-bedrock.json) | 13 labelled cases, five repeats each | balanced accuracy 0.95 (recall real 0.90, artefact 1.00; baselines 0.50); 15 of 15 failure runs failed closed; AUD 0.94 in all |
| [`counterfactual`](runs/2026-10-20-eval-counterfactual-amd64-opencv-bedrock.json) | Only OpenCV's output changed | gate level: 2 of 2 flipped; agent level: not shown (`look_again` called in 1 of 20 runs) |

## How to check them yourself

With no AWS account, from the repository root after
[REPRODUCE.md](../../REPRODUCE.md)'s tier 1a install, replay a recording:

```sh
python scripts/vision_eval.py replay --sequence sydney-demo --agent recorded --from docs/hackathon/evidence/agentic-vision/runs/2026-10-20-eval-replay-amd64-opencv-sydney-demo-bedrock.json --cache ~/cogs --out /tmp
```

It reads the pinned scenes from the public `sentinel-cogs` bucket (about 54 MB a scene, once),
recomputes every OpenCV measurement, replays the model's recorded answers, and exits 0 when every
decision and measurement matches.

## Caveats

- Made on a Windows x86 laptop, so the run files' CPU is `amd64`; the worker calls ran on the
  deployed arm64 worker (Graviton3), whose CPU each worker row names.
- `failures` ran with `--no-collect`: one named test module imports `resource`, which Windows lacks,
  so that the catalogue's tests are collected is held by CI on Linux.
- The agent rarely asks OpenCV to re-measure (`look_again`), so the agent-level counterfactual
  measures nothing; the gate-level one is the causal evidence.
- No live triage yet: the demo topic's two-tick procedure (O3) and the evidence export (O9) are the
  owner's.
