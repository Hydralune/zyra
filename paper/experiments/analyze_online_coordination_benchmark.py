from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont


METHODS = (
    "single_agent",
    "static_chain",
    "static_star",
    "broadcast",
    "zyra_full",
)
METHOD_LABELS = {
    "single_agent": "单智能体",
    "static_chain": "静态链式",
    "static_star": "静态星型",
    "broadcast": "全量广播",
    "zyra_full": "ZYRA",
}
CONDITION_LABELS = {"no_fault": "无故障", "agent_loss": "成员响应丢失"}
COLORS = {
    "single_agent": "#A5A5A5",
    "static_chain": "#70AD47",
    "static_star": "#5B9BD5",
    "broadcast": "#ED7D31",
    "zyra_full": "#4472C4",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit and analyse the online coordination benchmark.")
    parser.add_argument("input", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--bootstrap", type=int, default=20_000)
    parser.add_argument("--seed", type=int, default=20260907)
    return parser.parse_args()


def sha256_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def percentile(values: list[float], q: float) -> float:
    if not values:
        return math.nan
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def wilson_interval(successes: int, total: int, z: float = 1.959963984540054) -> tuple[float, float]:
    if total <= 0:
        return math.nan, math.nan
    proportion = successes / total
    denominator = 1 + z * z / total
    centre = (proportion + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)


def bootstrap_mean_interval(values: list[float], iterations: int, seed: int) -> tuple[float, float]:
    if not values:
        return math.nan, math.nan
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [values[rng.randrange(len(values))] for _ in values]
        means.append(statistics.fmean(sample))
    return percentile(means, 0.025), percentile(means, 0.975)


def exact_mcnemar(first: list[bool], second: list[bool]) -> dict[str, Any]:
    if len(first) != len(second):
        raise ValueError("paired binary vectors differ in length")
    b = sum(left and not right for left, right in zip(first, second))
    c = sum(right and not left for left, right in zip(first, second))
    discordant = b + c
    if discordant == 0:
        p_value = 1.0
    else:
        tail = sum(math.comb(discordant, index) for index in range(0, min(b, c) + 1)) / (2**discordant)
        p_value = min(1.0, 2 * tail)
    return {"zyra_wins": b, "baseline_wins": c, "discordant": discordant, "p_value": p_value}


def paired_randomization(differences: list[float]) -> float:
    nonzero = [value for value in differences if abs(value) > 1e-12]
    if not nonzero:
        return 1.0
    observed = abs(statistics.fmean(nonzero))
    if len(nonzero) <= 20:
        extreme = 0
        total = 1 << len(nonzero)
        for mask in range(total):
            value = statistics.fmean(
                item if mask & (1 << index) else -item for index, item in enumerate(nonzero)
            )
            if abs(value) + 1e-12 >= observed:
                extreme += 1
        return extreme / total
    rng = random.Random(20260907)
    iterations = 100_000
    extreme = 0
    for _ in range(iterations):
        value = statistics.fmean(item if rng.random() < 0.5 else -item for item in nonzero)
        if abs(value) + 1e-12 >= observed:
            extreme += 1
    return (extreme + 1) / (iterations + 1)


def holm_adjust(rows: list[dict[str, Any]], key: str = "p_value") -> None:
    ordered = sorted(enumerate(rows), key=lambda item: item[1][key])
    running = 0.0
    count = len(rows)
    for rank, (index, row) in enumerate(ordered):
        adjusted = min(1.0, row[key] * (count - rank))
        running = max(running, adjusted)
        rows[index]["holm_adjusted_p"] = running


def aggregate(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for cell in cells:
        groups[(cell["condition"], cell["method"])].append(cell)
    rows = []
    for condition in ("no_fault", "agent_loss"):
        for method in METHODS:
            group = groups[(condition, method)]
            successes = sum(bool(item["success"]) for item in group)
            low, high = wilson_interval(successes, len(group))
            row = {
                "condition": condition,
                "method": method,
                "n": len(group),
                "successes": successes,
                "success_rate": successes / len(group),
                "success_ci95_low": low,
                "success_ci95_high": high,
            }
            for metric in (
                "fieldAccuracy",
                "totalTokens",
                "wallTimeMs",
                "communicationBytes",
                "communicationMessages",
                "modelCallCount",
                "staleFactAcceptanceCount",
            ):
                values = [float(item[metric]) for item in group]
                row[f"{metric}_mean"] = statistics.fmean(values)
                row[f"{metric}_median"] = statistics.median(values)
                row[f"{metric}_p25"] = percentile(values, 0.25)
                row[f"{metric}_p75"] = percentile(values, 0.75)
            rows.append(row)
    return rows


def paired_comparisons(cells: list[dict[str, Any]], bootstrap: int, seed: int) -> list[dict[str, Any]]:
    by_key = {
        (cell["condition"], cell["taskId"], int(cell["repetition"]), cell["method"]): cell
        for cell in cells
    }
    rows = []
    for condition in ("no_fault", "agent_loss"):
        for baseline in METHODS[:-1]:
            pairs = []
            for key, zyra in by_key.items():
                if key[0] != condition or key[3] != "zyra_full":
                    continue
                baseline_cell = by_key.get((condition, key[1], key[2], baseline))
                if baseline_cell is None:
                    raise ValueError(f"missing pair for {condition}/{key[1]}/{key[2]}/{baseline}")
                pairs.append((zyra, baseline_cell))
            pairs.sort(key=lambda pair: (pair[0]["taskId"], pair[0]["repetition"]))
            binary = exact_mcnemar(
                [bool(pair[0]["success"]) for pair in pairs],
                [bool(pair[1]["success"]) for pair in pairs],
            )
            row: dict[str, Any] = {
                "condition": condition,
                "baseline": baseline,
                "n_pairs": len(pairs),
                **binary,
            }
            for metric in ("fieldAccuracy", "totalTokens", "wallTimeMs", "communicationBytes"):
                differences = [float(zyra[metric]) - float(other[metric]) for zyra, other in pairs]
                low, high = bootstrap_mean_interval(
                    differences,
                    bootstrap,
                    seed + sum(ord(char) for char in f"{condition}:{baseline}:{metric}"),
                )
                row[f"{metric}_mean_delta"] = statistics.fmean(differences)
                row[f"{metric}_median_delta"] = statistics.median(differences)
                row[f"{metric}_delta_ci95_low"] = low
                row[f"{metric}_delta_ci95_high"] = high
                row[f"{metric}_randomization_p"] = paired_randomization(differences)
            rows.append(row)
    for condition in ("no_fault", "agent_loss"):
        holm_adjust([row for row in rows if row["condition"] == condition])
    return rows


def audit(payload: dict[str, Any], cells: list[dict[str, Any]]) -> dict[str, Any]:
    configuration = payload.get("configuration", {})
    cell_ids = [cell.get("cellId") for cell in cells]
    call_ids = [call.get("callId") for cell in cells for call in cell.get("calls", [])]
    http_statuses = [status for cell in cells for call in cell.get("calls", []) for status in call.get("httpStatuses", [])]
    checks = {
        "campaign_completed": payload.get("status") == "completed",
        "schema_current": payload.get("schema") == "zyra.online-coordination-campaign/v1",
        "no_historical_trace_replay": configuration.get("historicalTraceReplay") is False,
        "independent_provider_dispatch": configuration.get("independentProviderDispatchPerAgent") is True,
        "unique_cell_ids": len(cell_ids) == len(set(cell_ids)),
        "unique_call_ids": len(call_ids) == len(set(call_ids)),
        "all_cells_have_usage": all(int(cell.get("totalTokens", 0)) > 0 for cell in cells),
        "all_attempts_http_200": bool(http_statuses) and all(status == 200 for status in http_statuses),
        "all_methods_present": set(configuration.get("methods", [])) == set(METHODS),
        "all_conditions_present": set(configuration.get("conditions", [])) == {"no_fault", "agent_loss"},
        "balanced_cells": len(cells) == int(configuration.get("repetitions", 0)) * int(configuration.get("taskCount", 0)) * len(METHODS) * 2,
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "cell_count": len(cells),
        "call_count": len(call_ids),
        "http_attempt_count": len(http_statuses),
        "provider_ids": sorted({call.get("providerId") for cell in cells for call in cell.get("calls", [])}),
        "model_ids": sorted({call.get("modelId") for cell in cells for call in cell.get("calls", [])}),
    }


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf" if bold else r"C:\Windows\Fonts\simsun.ttc"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def draw_axes(draw: ImageDraw.ImageDraw, plot: tuple[int, int, int, int], y_max: float, ticks: int, formatter) -> None:
    left, top, right, bottom = plot
    draw.line((left, top, left, bottom), fill="#666666", width=2)
    draw.line((left, bottom, right, bottom), fill="#666666", width=2)
    tick_font = font(25)
    for index in range(ticks + 1):
        value = y_max * index / ticks
        y = bottom - (bottom - top) * index / ticks
        draw.line((left, y, right, y), fill="#E5E5E5", width=1)
        label = formatter(value)
        box = draw.textbbox((0, 0), label, font=tick_font)
        draw.text((left - 18 - (box[2] - box[0]), y - (box[3] - box[1]) / 2), label, fill="#555555", font=tick_font)


def draw_grouped_chart(rows: list[dict[str, Any]], output: Path, metric: str, title: str, y_max: float, formatter, note: str) -> None:
    image = Image.new("RGB", (1900, 1050), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 42), title, fill="#1F1F1F", font=font(46, bold=True))
    draw.text((80, 108), note, fill="#666666", font=font(27))
    plot = (170, 200, 1810, 850)
    draw_axes(draw, plot, y_max, 5, formatter)
    left, top, right, bottom = plot
    conditions = ("no_fault", "agent_loss")
    group_width = (right - left) / len(conditions)
    bar_gap = 18
    bar_width = 105
    label_font = font(26)
    value_font = font(24, bold=True)
    lookup = {(row["condition"], row["method"]): row for row in rows}
    for condition_index, condition in enumerate(conditions):
        centre = left + group_width * (condition_index + 0.5)
        total_width = len(METHODS) * bar_width + (len(METHODS) - 1) * bar_gap
        x_start = centre - total_width / 2
        for method_index, method in enumerate(METHODS):
            value = float(lookup[(condition, method)][metric])
            x0 = x_start + method_index * (bar_width + bar_gap)
            x1 = x0 + bar_width
            y0 = bottom - (bottom - top) * min(value, y_max) / y_max
            draw.rounded_rectangle((x0, y0, x1, bottom), radius=8, fill=COLORS[method])
            value_label = formatter(value)
            box = draw.textbbox((0, 0), value_label, font=value_font)
            draw.text(((x0 + x1 - (box[2] - box[0])) / 2, y0 - 38), value_label, fill="#333333", font=value_font)
        condition_label = CONDITION_LABELS[condition]
        box = draw.textbbox((0, 0), condition_label, font=font(31, bold=True))
        draw.text((centre - (box[2] - box[0]) / 2, bottom + 25), condition_label, fill="#333333", font=font(31, bold=True))
    legend_y = 955
    legend_x = 260
    for method in METHODS:
        draw.rounded_rectangle((legend_x, legend_y, legend_x + 34, legend_y + 28), radius=5, fill=COLORS[method])
        draw.text((legend_x + 45, legend_y - 4), METHOD_LABELS[method], fill="#333333", font=label_font)
        legend_x += 280
    image.save(output)


def draw_efficiency_chart(rows: list[dict[str, Any]], output: Path) -> None:
    no_fault = {row["method"]: row for row in rows if row["condition"] == "no_fault"}
    image = Image.new("RGB", (1900, 1050), "white")
    draw = ImageDraw.Draw(image)
    draw.text((80, 42), "在线先导实验的模型 Token 与智能体间通信量", fill="#1F1F1F", font=font(46, bold=True))
    draw.text((80, 108), "5 个任务 × 3 次重复；通信量不含用户输入，单智能体没有智能体间消息", fill="#666666", font=font(27))
    panels = [
        ((120, 220, 900, 850), "平均总 Token", "totalTokens_mean", max(row["totalTokens_mean"] for row in no_fault.values()) * 1.18, lambda value: f"{value/1000:.1f}k"),
        ((1030, 220, 1810, 850), "平均智能体间通信字节", "communicationBytes_mean", max(row["communicationBytes_mean"] for row in no_fault.values()) * 1.18, lambda value: f"{value/1000:.1f}k"),
    ]
    for plot, subtitle, metric, y_max, formatter in panels:
        left, top, right, bottom = plot
        draw.text((left, 170), subtitle, fill="#333333", font=font(31, bold=True))
        draw_axes(draw, plot, y_max, 5, formatter)
        available = right - left
        bar_width = 100
        gap = (available - len(METHODS) * bar_width) / (len(METHODS) + 1)
        for index, method in enumerate(METHODS):
            value = float(no_fault[method][metric])
            x0 = left + gap + index * (bar_width + gap)
            x1 = x0 + bar_width
            y0 = bottom - (bottom - top) * value / y_max
            draw.rounded_rectangle((x0, y0, x1, bottom), radius=8, fill=COLORS[method])
            label = formatter(value)
            box = draw.textbbox((0, 0), label, font=font(23, bold=True))
            draw.text(((x0 + x1 - (box[2] - box[0])) / 2, y0 - 34), label, fill="#333333", font=font(23, bold=True))
            method_label = METHOD_LABELS[method]
            box = draw.textbbox((0, 0), method_label, font=font(23))
            draw.text(((x0 + x1 - (box[2] - box[0])) / 2, bottom + 18), method_label, fill="#333333", font=font(23))
    image.save(output)


def write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    rows = list(rows)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(args.input.read_text(encoding="utf-8"))
    cells = list(payload.get("cells", []))
    audit_result = audit(payload, cells)
    if not audit_result["passed"]:
        raise SystemExit(f"evidence audit failed: {json.dumps(audit_result, ensure_ascii=False)}")
    aggregates = aggregate(cells)
    comparisons = paired_comparisons(cells, args.bootstrap, args.seed)
    result = {
        "schema": "zyra.online-coordination-analysis/v1",
        "campaign_id": payload["campaignId"],
        "source_path": str(args.input.resolve()),
        "source_sha256": sha256_file(args.input),
        "evidence_digest": payload.get("evidenceDigest"),
        "audit": audit_result,
        "design": {
            "task_count": payload["configuration"]["taskCount"],
            "repetitions": payload["configuration"]["repetitions"],
            "paired_units_per_condition": payload["configuration"]["taskCount"] * payload["configuration"]["repetitions"],
            "methods": list(METHODS),
            "conditions": ["no_fault", "agent_loss"],
            "model_provider": payload["configuration"]["modelProvider"],
            "model_id": payload["configuration"]["modelId"],
            "bootstrap_iterations": args.bootstrap,
            "statistical_unit_note": "Task-repetition is the paired execution unit. Repetitions of the same task are correlated; p-values are exploratory and the five unique tasks limit external validity.",
        },
        "aggregates": aggregates,
        "paired_comparisons": comparisons,
        "limitations": [
            "This is a controlled synthetic information-integration benchmark, not an industry deployment study.",
            "The five policies are native benchmark adapters; they are not the official AutoGen, GPTSwarm, AgentPrune or G-Designer implementations.",
            "Only one provider/model and five unique task templates were used.",
            "The agent-loss condition discards one response after a successful provider call; it measures coordination recovery rather than provider outage handling.",
            "The ZYRA adapter includes obligation routing, response-loss replacement and deterministic completion-gate repair, so the result supports the combined policy stack; component causality requires ablation experiments.",
        ],
    }
    (args.output_dir / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_csv(args.output_dir / "aggregate.csv", aggregates)
    write_csv(args.output_dir / "paired-comparisons.csv", comparisons)
    draw_grouped_chart(
        aggregates,
        args.output_dir / "success-rate.png",
        "success_rate",
        "在线先导实验的严格任务成功率",
        1.0,
        lambda value: f"{value * 100:.0f}%",
        "5 个任务 × 3 次重复；成功要求 12 个字段全部通过独立判分",
    )
    draw_grouped_chart(
        aggregates,
        args.output_dir / "field-accuracy.png",
        "fieldAccuracy_mean",
        "在线先导实验的字段级正确率",
        1.0,
        lambda value: f"{value * 100:.0f}%",
        "字段级指标用于解释接近成功的运行，不替代严格任务成功率",
    )
    draw_efficiency_chart(aggregates, args.output_dir / "efficiency.png")
    print(json.dumps({
        "status": "passed",
        "summary": str((args.output_dir / "summary.json").resolve()),
        "cell_count": audit_result["cell_count"],
        "call_count": audit_result["call_count"],
        "source_sha256": result["source_sha256"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
