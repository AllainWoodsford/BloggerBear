"""The `agent`, `counterfactual` and `stability` subcommands' arguments (2.6.1-2.6.3, 2.6.6, E3).

Apart from vision_eval_agent so that vision_eval.py can build its whole parser without importing
the replay's driver (it imports the adapter, and so boto3): `pin` and `coverage` then run in a venv
with only the worker's requirements. vision_eval.py imports vision_eval_agent only to run one of
these three; vision_eval_agent takes its defaults from here.
"""

from __future__ import annotations

from pathlib import Path

import vision_runs

COMMANDS = ("agent", "counterfactual", "stability")
DEFAULT_REPEATS = 5
# Fixture A for the Sydney cases: the file the runsheet's fixture step captures (and the benchmark's
# scenes file names).
DEFAULT_NETWORK = Path(__file__).resolve().parent / "vision_benchmark_networks" / "sydney.json"
# E3's scene set in the manifest, in time order.
YEAR_SET = "year-2024"
# The synthetic year: this many clear synthetic scenes, ten days apart (CI's E3).
SYNTHETIC_YEAR_SCENES = 6

HELPS = {
    "agent": "2.6.1-2.6.3: the agent's cases, scored per class against two baselines; a run file",
    "counterfactual": "2.6.6: OpenCV's output changed and nothing else, at the agent and at the gate",
    "stability": "E3: the production rule over the year set, one network; false alarms and spreads",
}


def add_parsers(sub, manifest_default: Path) -> None:
    """The three subcommands' arguments, on vision_eval.py's subparsers."""
    for name in COMMANDS:
        p = sub.add_parser(name, help=HELPS[name])
        p.add_argument("--manifest", type=Path, default=manifest_default)
        p.add_argument("--synthetic", action="store_true",
                       help="the synthetic city, in this process, no network (what CI runs)")  # fmt: skip
        p.add_argument("--network", type=Path,
                       help=f"fixture A (default {DEFAULT_NETWORK.name}, or the synthetic one)")  # fmt: skip
        p.add_argument("--worker", metavar="ARN|in-process",
                       help="the deployed worker's ARN, or in-process (with --cache)")  # fmt: skip
        p.add_argument("--cache", type=vision_runs.cache_dir, nargs="?", default=None, metavar="DIR",
                       const=vision_runs.default_cache_dir(),
                       help="answer range reads from DIR (~ expanded), filled on first read")  # fmt: skip
        p.add_argument("--out", type=Path, help="write the run file here: a .json file, or a directory")
        p.add_argument("--tag", help="a suffix for the run id")
        if name in ("agent", "counterfactual"):
            p.add_argument("--case", action="append", metavar="NAME", help="only this case (repeatable)")
            p.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
            p.add_argument("--agent", choices=("bedrock", "scripted"), default="scripted",
                           help="the live model (with --model-id), or each case's script")  # fmt: skip
            p.add_argument("--model-id", help="the Bedrock model id the rail topic uses")
            p.add_argument("--region", help="Bedrock's Region, as replay takes it (default: the SDK's own)")
        if name == "counterfactual":
            p.add_argument("--arms", default="a,b", help="a (the agent), b (the gate), or both: a,b")
        if name == "stability":
            p.add_argument("--set", default=YEAR_SET, help="the manifest's scene set, in time order")
            p.add_argument("--scenes", type=int, default=SYNTHETIC_YEAR_SCENES,
                           help="with --synthetic: how many synthetic scenes")  # fmt: skip
