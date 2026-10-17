"""The evaluation's numbers, as pure functions: the agent's scores (the specification's 2.6.3), the
counterfactuals' counts (2.6.6) and the stability run's rates and spreads (E3).

Nothing here reads a file, a clock or the network, so every number a run file's `summary` holds can
be re-made from its `results` by calling these again, and the tests pin each rule on small inputs.

**A run** of the agent is one case at one repeat: {"case", "expected", "verdict", "stop", ...}.
Its **label** is its verdict when the loop answered well-formed (`stop` "answered"), else
`failed_closed`: a run that ended on a Bedrock error, the deadline, the turn limit or a malformed
answer is an artefact by design, but it is not a judgment, so it scores for neither class and the
confusion matrix gives it a column of its own. A run with no Bedrock access is therefore counted
(`failed_closed`) and lowers both recalls; it is never dropped.

**Balanced accuracy** is the mean of the per-class recalls (each a share of that class's runs), so
the class mix cannot flatter it: the two baselines, numeric-only (every candidate that passed the
gate reported, as with `triage: false`) and always-artefact, each score 0.5 whatever the mix. A
run holding one class has no balanced accuracy (not measured), only that class's recall.
Every share carries its **Wilson 95 % interval** (`wilson`), which stays inside [0, 1] and is not
degenerate at 0 or n, as the normal approximation is on the small n these runs have.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter

VERDICTS = ("real", "artefact")
FAILED_CLOSED = "failed_closed"
LABELS = (*VERDICTS, FAILED_CLOSED)
# The two-sided 95 % normal quantile.
Z95 = 1.959963984540054
# How the loop stops when it answered with a verdict it could parse (vision_triage.STOPS).
ANSWERED = "answered"
# The decision codes that compared a scene with a baseline (vision_decisions.CODES): the ones the
# false-alarm rate is over. The first observation, a scene below the floor and a baseline still
# building are not decisions about a change.
COMPARED_CODES = ("within_range", "reported", "artefact")
ROUND = 4


def _round(value):
    return None if value is None else round(value, ROUND)


def wilson(k: int, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """The Wilson score interval for `k` successes in `n` trials, rounded to 4 places; (None, None)
    when n is 0 (no trial, no interval)."""
    if n <= 0:
        return None, None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return round(max(0.0, centre - half), ROUND), round(min(1.0, centre + half), ROUND)


def share(k: int, n: int) -> float | None:
    return None if n <= 0 else round(k / n, ROUND)


def label(run: dict) -> str:
    """A run's scored label: its verdict when the loop answered, else FAILED_CLOSED."""
    if run.get("stop") == ANSWERED and run.get("verdict") in VERDICTS:
        return run["verdict"]
    return FAILED_CLOSED


# --- the agent's class scores -------------------------------------------------------------------


def confusion(runs: list[dict]) -> dict:
    """{expected class: {label: runs}} over `runs` whose `expected` is a class: the expected class
    by the agent's verdict, with failed-closed runs in their own column."""
    matrix = {expected: dict.fromkeys(LABELS, 0) for expected in VERDICTS}
    for run in runs:
        if run.get("expected") in VERDICTS:
            matrix[run["expected"]][label(run)] += 1
    return matrix


def class_scores(matrix: dict) -> dict:
    """Per-class recall with its Wilson interval, and the balanced accuracy: the mean of the two
    recalls, None unless both classes have runs (over one class it would be that class's recall,
    and the baselines would read 0 and 1, not 0.5)."""
    out = {}
    recalls = []
    for cls in VERDICTS:
        row = matrix.get(cls) or {}
        n = sum(row.values())
        k = row.get(cls, 0)
        recall = share(k, n)
        low, high = wilson(k, n)
        out[f"recall_{cls}"] = recall
        out[f"recall_{cls}_wilson"] = [low, high]
        out[f"n_{cls}"] = n
        if recall is not None:
            recalls.append(k / n)
    out["balanced_accuracy"] = _round(sum(recalls) / len(recalls)) if len(recalls) == len(VERDICTS) else None
    out["failed_closed"] = sum((matrix.get(cls) or {}).get(FAILED_CLOSED, 0) for cls in VERDICTS)
    return out


def baseline_runs(runs: list[dict], kind: str) -> list[dict]:
    """`runs` as a baseline would have answered them: `numeric_only` reports every candidate (real),
    `always_artefact` none; each answers well-formed, so neither fails closed."""
    verdict = {"numeric_only": "real", "always_artefact": "artefact"}[kind]
    return [{**run, "verdict": verdict, "stop": ANSWERED} for run in runs]


def stability(labels_by_case: dict[str, list[str]]) -> dict:
    """Per case, the share of its repeats that agree with its majority label (ties: the first
    label in LABELS order among the most frequent), and the mean over cases."""
    per_case = {}
    for case, labels in labels_by_case.items():
        if not labels:
            continue
        counts = Counter(labels)
        top = max(counts.values())
        majority = next(lab for lab in LABELS if counts.get(lab) == top)
        per_case[case] = {"majority": majority, "agree": share(top, len(labels)), "repeats": len(labels)}
    mean = _round(statistics.fmean(row["agree"] for row in per_case.values())) if per_case else None
    return {"per_case": per_case, "mean": mean}


def fail_closed(runs: list[dict]) -> dict:
    """The failure cases with a required end (`expected_stop`): how many runs ended as required
    (an artefact, with that stop) out of how many."""
    required = [run for run in runs if run.get("expected_stop")]
    ok = sum(1 for run in required
             if run.get("verdict") == "artefact" and run.get("stop") == run["expected_stop"])  # fmt: skip
    return {"as_required": ok, "runs": len(required), "all": ok == len(required) if required else None}


def percentile(values: list[float], q: float) -> float | None:
    """The `q`-th percentile (0-100) by linear interpolation between ranks (numpy's default)."""
    data = sorted(v for v in values if v is not None)
    if not data:
        return None
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * q / 100.0
    lo = math.floor(rank)
    hi = min(lo + 1, len(data) - 1)
    return data[lo] + (data[hi] - data[lo]) * (rank - lo)


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.fmean(values) if values else None


def tool_use(runs: list[dict]) -> dict:
    """The mean number of tool calls a run made, and the shares of runs that called `look_again`
    and `web_context` at least once (from each run's `tools`, the names in call order)."""
    n = len(runs)
    return {
        "tool_calls_mean": _round(_mean([len(run.get("tools") or []) for run in runs])),
        "look_again_share": share(sum(1 for r in runs if "look_again" in (r.get("tools") or [])), n),
        "web_context_share": share(sum(1 for r in runs if "web_context" in (r.get("tools") or [])), n),
    }


def costs(runs: list[dict]) -> dict:
    """Latency (p50, p95 of `elapsed_ms`), tokens and cost per run (USD and AUD, over the runs
    whose cost is known; `cost_known` says how many)."""
    priced = [r for r in runs if r.get("cost_usd") is not None]
    p50 = percentile([r.get("elapsed_ms") for r in runs], 50)
    p95 = percentile([r.get("elapsed_ms") for r in runs], 95)
    return {
        "latency_p50_ms": None if p50 is None else round(p50),
        "latency_p95_ms": None if p95 is None else round(p95),
        "tokens_mean": _round(_mean([(r.get("input_tokens") or 0) + (r.get("output_tokens") or 0)
                                     for r in runs])),  # fmt: skip
        "cost_usd_mean": _round(_mean([r["cost_usd"] for r in priced])),
        "cost_aud_mean": _round(_mean([r["cost_aud"] for r in priced if r.get("cost_aud") is not None])),
        "cost_usd_total": _round(sum(r["cost_usd"] for r in priced)) if priced else None,
        "cost_aud_total": _round(sum(r["cost_aud"] for r in priced if r.get("cost_aud") is not None))
        if priced else None,
        "cost_known": len(priced),
    }


def by_driven(runs: list[dict]) -> dict:
    """Per `driven_by` (imagery, network), the share of its runs whose label is the expected class,
    with its Wilson interval and n."""
    out = {}
    for driven in ("imagery", "network"):
        group = [r for r in runs if r.get("driven_by") == driven and r.get("expected") in VERDICTS]
        k = sum(1 for r in group if label(r) == r["expected"])
        n = len(group)
        out[driven] = {"recall": share(k, n), "wilson": list(wilson(k, n)), "runs": n}
    return out


def agent_scores(task_runs: list[dict], failure_runs: list[dict]) -> dict:
    """Everything 2.6.3 reports, from the task cases' runs (those that passed the gate) and the
    failure cases' runs: the agent's and both baselines' confusion and class scores, stability,
    the fail-closed count, tool use, latency, tokens and cost, and the recall by input."""
    agent = confusion(task_runs)
    labels = {}
    for run in task_runs + failure_runs:
        labels.setdefault(run["case"], []).append(label(run))
    out = {
        "agent": {"confusion": agent, **class_scores(agent)},
        "baselines": {},
        "stability": stability(labels),
        "fail_closed": fail_closed(failure_runs),
        "tool_use": tool_use(task_runs + failure_runs),
        "costs": costs(task_runs + failure_runs),
        "by_driven": by_driven(task_runs),
    }
    for kind in ("numeric_only", "always_artefact"):
        matrix = confusion(baseline_runs(task_runs, kind))
        out["baselines"][kind] = {"confusion": matrix, **class_scores(matrix)}
    return out


def agent_summary(scores: dict, cases: int, gate_closed: int, runs: int, not_measured: int = 0) -> dict:
    """The run file's flat `summary` (fixed codes to numbers) from `agent_scores`; `not_measured`
    counts the runs whose newest scene the worker did not measure (in `runs`, scored nowhere)."""
    agent, tools, money = scores["agent"], scores["tool_use"], scores["costs"]
    summary = {
        "cases": cases,
        "gate_closed": gate_closed,
        "runs": runs,
        "not_measured_runs": not_measured,
        "recall_real": agent["recall_real"],
        "recall_artefact": agent["recall_artefact"],
        "balanced_accuracy": agent["balanced_accuracy"],
        "failed_closed_task_runs": agent["failed_closed"],
        "numeric_only_balanced_accuracy": scores["baselines"]["numeric_only"]["balanced_accuracy"],
        "always_artefact_balanced_accuracy": scores["baselines"]["always_artefact"]["balanced_accuracy"],
        "stability": scores["stability"]["mean"],
        "fail_closed": scores["fail_closed"]["as_required"],
        "fail_closed_runs": scores["fail_closed"]["runs"],
        **tools,
        **{key: money[key] for key in ("latency_p50_ms", "latency_p95_ms", "tokens_mean", "cost_usd_mean",
                                       "cost_aud_mean", "cost_usd_total", "cost_aud_total")},  # fmt: skip
        "recall_imagery_driven": scores["by_driven"]["imagery"]["recall"],
        "recall_network_driven": scores["by_driven"]["network"]["recall"],
    }
    for cls in VERDICTS:
        low, high = agent[f"recall_{cls}_wilson"]
        summary[f"recall_{cls}_wilson_low"], summary[f"recall_{cls}_wilson_high"] = low, high
    return summary


# --- the counterfactuals ------------------------------------------------------------------------


def agent_counterfactual(arms: dict[str, list[dict]]) -> dict:
    """Arm (a): `arms` holds {"measured": runs, "baseline": runs}, the same repeats of one case
    with only `look_again`'s numbers differing. Repeat k of one arm is paired with repeat k of the
    other; a pair counts when both called `look_again`, and flips when their labels differ.
    Returns per arm how many repeats called it and their verdicts (of the repeats that reached the
    agent: one whose scene was not measured has no verdict), and the pairs and flips."""
    measured, baseline = arms.get("measured") or [], arms.get("baseline") or []
    called = {name: [r for r in runs if "look_again" in (r.get("tools") or [])]
              for name, runs in (("measured", measured), ("baseline", baseline))}  # fmt: skip
    pairs = flips = 0
    for a, b in zip(measured, baseline, strict=False):
        if "look_again" in (a.get("tools") or []) and "look_again" in (b.get("tools") or []):
            pairs += 1
            flips += label(a) != label(b)
    return {
        "repeats": {"measured": len(measured), "baseline": len(baseline)},
        "called_look_again": {name: len(runs) for name, runs in called.items()},
        "labels": {name: dict(Counter(label(r) for r in runs if r.get("agent_called", True)))
                   for name, runs in (("measured", measured), ("baseline", baseline))},  # fmt: skip
        "pairs": pairs,
        "flips": flips,
        "flip_rate": share(flips, pairs),
    }


def gate_counterfactual(above: dict, below: dict) -> dict:
    """Arm (b): the same scene measured with coverage just above and just below the floor. Each
    side is {"coverage", "code", "agent_called", "finding", "at_target"}; the pair is `measured`
    when both sides were measured at their target coverage (a side the worker did not measure says
    nothing about the gate), and flips when the agent was called on one side and not the other."""
    flipped = bool(above.get("agent_called")) != bool(below.get("agent_called"))
    measured = bool(above.get("at_target") and below.get("at_target"))
    return {"above": above, "below": below, "measured": measured, "flipped": flipped,
            "finding_flipped": bool(above.get("finding")) != bool(below.get("finding"))}  # fmt: skip


def counterfactual_summary(agent_rows: dict[str, dict], gate_rows: dict[str, dict]) -> dict:
    """The run file's `summary`: arm (a)'s pairs, how many repeats (both arms) called `look_again`,
    and the flips; arm (b)'s pairs measured either side of the floor, and their flips."""
    measured = [row for row in gate_rows.values() if row.get("measured")]
    return {
        "agent_pairs": sum(row["pairs"] for row in agent_rows.values()),
        "agent_called_look_again": sum(sum(row["called_look_again"].values()) for row in agent_rows.values()),
        "agent_flips": sum(row["flips"] for row in agent_rows.values()),
        "gate_pairs": len(measured),
        "gate_flips": sum(1 for row in measured if row["flipped"]),
    }


# --- E3: stability over the year -----------------------------------------------------------------


def false_alarms(rows: list[dict]) -> dict:
    """E3 (a). `rows` are the year set's decisions in time order, each {"scene", "captured_at",
    "code", "material"}: on an unchanged network every material verdict is a false alarm. The rate
    is over the decisions that compared a scene with a baseline (COMPARED_CODES), with its Wilson
    95 % interval, overall and by month (`captured_at`'s YYYY-MM)."""
    compared = [row for row in rows if row.get("code") in COMPARED_CODES]
    k = sum(1 for row in compared if row.get("material"))
    low, high = wilson(k, len(compared))
    months: dict[str, list[dict]] = {}
    for row in compared:
        months.setdefault(str(row.get("captured_at") or "")[:7], []).append(row)
    by_month = {}
    for month in sorted(months):
        group = months[month]
        m = sum(1 for row in group if row.get("material"))
        by_month[month] = {"decisions": len(group), "false_material": m, "rate": share(m, len(group)),
                           "wilson": list(wilson(m, len(group)))}  # fmt: skip
    return {
        "decisions": len(compared),
        "false_material": k,
        "false_material_rate": share(k, len(compared)),
        "wilson_low": low,
        "wilson_high": high,
        "by_month": by_month,
        "codes": dict(Counter(row.get("code") for row in rows)),
    }


def spread(values: list[float]) -> float | None:
    """max - min of the values that are numbers; None when there are none."""
    data = [v for v in values if isinstance(v, int | float)]
    return _round(max(data) - min(data)) if data else None


def relative_spread(values: list[float]) -> float | None:
    """(max - min) / median, None when there is no value or the median is 0."""
    data = [v for v in values if isinstance(v, int | float)]
    if not data:
        return None
    median = statistics.median(data)
    return None if median == 0 else _round((max(data) - min(data)) / median)


def jaccard(a, b) -> float:
    """|a ∩ b| / |a ∪ b| of two collections as sets; 1.0 when both are empty (nothing changed)."""
    a, b = {_hashable(x) for x in a or []}, {_hashable(x) for x in b or []}
    union = a | b
    return 1.0 if not union else round(len(a & b) / len(union), ROUND)


def _hashable(x):
    return tuple(_hashable(v) for v in x) if isinstance(x, list | tuple) else x


def iou_from_counts(intersection: int, union: int) -> float | None:
    """A mask IoU from its pixel counts; None when both masks are empty."""
    return None if union <= 0 else round(intersection / union, ROUND)


def stability_spread(rows: list[dict]) -> dict:
    """E3 (b). `rows` are the year set's measurements in time order, each {"scene", "served_share",
    "desert_km2", "built_up_km2", "hubs", "flags", "iou_previous", "iou_first"} (the IoUs of the
    built-up mask, from the worker's layers; None where not measured). The spreads, the smallest
    mask IoU (with the previous scene and with the first), and the smallest Jaccard of hubs and of
    flags between consecutive scenes."""
    measured = [row for row in rows if row.get("served_share") is not None]
    pairs = list(zip(measured, measured[1:], strict=False))
    hubs = [jaccard(a.get("hubs"), b.get("hubs")) for a, b in pairs]
    flags = [jaccard(a.get("flags"), b.get("flags")) for a, b in pairs]
    ious = [row.get("iou_previous") for row in measured[1:] if row.get("iou_previous") is not None]
    firsts = [row.get("iou_first") for row in measured[1:] if row.get("iou_first") is not None]
    return {
        "scenes": len(measured),
        "served_share_spread": spread([row["served_share"] for row in measured]),
        "desert_rel_spread": relative_spread([row.get("desert_km2") for row in measured]),
        "built_up_rel_spread": relative_spread([row.get("built_up_km2") for row in measured]),
        "mask_iou_min": min(ious) if ious else None,
        "mask_iou_first_min": min(firsts) if firsts else None,
        "hub_jaccard_min": min(hubs) if hubs else None,
        "flag_jaccard_min": min(flags) if flags else None,
        "hub_jaccard": hubs,
        "flag_jaccard": flags,
    }


def stability_summary(alarms: dict, spreads: dict) -> dict:
    """The run file's `summary` for E3: the table's eleven codes."""
    return {
        "decisions": alarms["decisions"],
        "false_material": alarms["false_material"],
        "false_material_rate": alarms["false_material_rate"],
        "wilson_low": alarms["wilson_low"],
        "wilson_high": alarms["wilson_high"],
        "served_share_spread": spreads["served_share_spread"],
        "desert_rel_spread": spreads["desert_rel_spread"],
        "built_up_rel_spread": spreads["built_up_rel_spread"],
        "mask_iou_min": spreads["mask_iou_min"],
        "hub_jaccard_min": spreads["hub_jaccard_min"],
        "flag_jaccard_min": spreads["flag_jaccard_min"],
    }
