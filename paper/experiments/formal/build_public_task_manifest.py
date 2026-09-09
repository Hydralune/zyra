"""Freeze a broad, deterministic public benchmark sample without exposing answers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


ROOT = Path(__file__).resolve().parents[3]
SEED = "zyra-formal-v2-20260907"
OUTPUT = ROOT / "paper/experiments/tasksets/public-task-manifest-v2.json"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def file_digest(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def rank(identifier: str) -> str:
    return sha256_bytes(f"{SEED}:{identifier}".encode("utf-8"))


def record_digest(record: dict[str, Any]) -> str:
    return sha256_bytes(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8"))


def select_swe() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = ROOT / ".tmp/datasets/raw/swe-bench-verified/test.parquet"
    rows = pq.read_table(path).to_pylist()
    by_repo: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_repo[row["repo"]].append(row)
    # Preserve repository breadth, but do not let a simple hash draw collapse the
    # sample into easy fixes.  The published sample has only three >4-hour items,
    # so the pre-registered tiers use the stable, sufficiently populated bands.
    quotas = {"<15 min fix": 3, "15 min - 1 hour": 6, "1-4 hours": 3}
    repos = sorted(by_repo)
    candidates: dict[str, dict[str, dict[str, Any]]] = {}
    for repo in repos:
        candidates[repo] = {}
        for difficulty in quotas:
            tier_rows = [row for row in by_repo[repo] if row.get("difficulty") == difficulty]
            if tier_rows:
                candidates[repo][difficulty] = min(tier_rows, key=lambda item: rank(item["instance_id"]))

    # A tiny dynamic program selects one task per repository while meeting the
    # pre-registered tier quotas.  The cost is deterministic and is not model data.
    states: dict[tuple[int, int, int], tuple[int, list[dict[str, Any]]]] = {(0, 0, 0): (0, [])}
    tiers = tuple(quotas)
    for repo in repos:
        next_states: dict[tuple[int, int, int], tuple[int, list[dict[str, Any]]]] = {}
        for counts, (cost, chosen) in states.items():
            for tier_index, tier in enumerate(tiers):
                row = candidates[repo].get(tier)
                if row is None or counts[tier_index] >= quotas[tier]:
                    continue
                updated = list(counts)
                updated[tier_index] += 1
                key = tuple(updated)
                candidate_cost = cost + int(rank(row["instance_id"])[:16], 16)
                previous = next_states.get(key)
                if previous is None or candidate_cost < previous[0]:
                    next_states[key] = (candidate_cost, chosen + [row])
        states = next_states
    target = tuple(quotas[tier] for tier in tiers)
    if target not in states:
        raise RuntimeError("SWE difficulty quotas cannot be satisfied with one task per repository")
    selected = []
    for row in states[target][1]:
        repo = row["repo"]
        selected.append(
            {
                "task_id": row["instance_id"],
                "repo": repo,
                "difficulty": row.get("difficulty"),
                "record_digest": record_digest(row),
            }
        )
    if len(selected) != 12:
        raise RuntimeError(f"Expected one task from each of 12 repos, got {len(selected)}")
    return selected, {
        "source": "princeton-nlp/SWE-bench_Verified",
        "source_rows": len(rows),
        "source_sha256": file_digest(path),
        "selection": "one deterministic hash-ranked task per repository; difficulty quotas: 3 <15 min, 6 15 min-1 hour, 3 1-4 hours",
    }


def select_tau() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = ROOT / ".tmp/datasets/tau2-bench/data/tau2/domains"
    selected = []
    source_hashes = {}
    source_counts = {}
    for domain in ("airline", "retail", "telecom"):
        path = root / domain / "tasks.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        source_hashes[domain] = file_digest(path)
        source_counts[domain] = len(rows)
        chosen = sorted(rows, key=lambda item: rank(f"tau:{domain}:{item['id']}"))[:6]
        selected.extend(
            {
                "task_id": str(row["id"]),
                "domain": domain,
                "record_digest": record_digest(row),
            }
            for row in chosen
        )
    return selected, {
        "source": "sierra-research/tau2-bench@1.0.1",
        "source_rows": source_counts,
        "source_sha256": source_hashes,
        "selection": "six deterministic hash-ranked tasks per domain",
    }


def select_marble() -> tuple[list[dict[str, Any]], dict[str, Any]]:
    root = ROOT / ".tmp/datasets/multiagentbench-marble/multiagentbench"
    # The released Minecraft file contains one duplicated record repeated under
    # 100 IDs, so it is excluded rather than being counted as independent work.
    allocation = {"coding": 4, "database": 4, "research": 2, "bargaining": 2}
    selected = []
    source_hashes = {}
    source_counts = {}
    for scenario, count in allocation.items():
        path = root / scenario / f"{scenario}_main.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        source_hashes[scenario] = file_digest(path)
        source_counts[scenario] = len(rows)
        keyed = []
        for index, row in enumerate(rows):
            task_id = str(row.get("task_id", index + 1))
            keyed.append((task_id, row))
        # Some scenario JSONL files contain duplicated records under different
        # IDs.  IDs alone are not an independence guarantee, so retain only the
        # first deterministically ranked instance of each full-record digest.
        chosen = []
        seen_digests: set[str] = set()
        for task_id, row in sorted(keyed, key=lambda item: rank(f"marble:{scenario}:{item[0]}")):
            digest = record_digest(row)
            if digest in seen_digests:
                continue
            seen_digests.add(digest)
            chosen.append((task_id, row))
            if len(chosen) == count:
                break
        if len(chosen) != count:
            raise RuntimeError(f"Not enough unique {scenario} records for requested allocation")
        selected.extend(
            {
                "task_id": task_id,
                "scenario": scenario,
                "record_digest": record_digest(row),
            }
            for task_id, row in chosen
        )
    return selected, {
        "source": "ulab-uiuc/MARBLE MultiAgentBench",
        "source_rows": source_counts,
        "source_sha256": source_hashes,
        "selection": allocation,
    }


def main() -> None:
    swe, swe_meta = select_swe()
    tau, tau_meta = select_tau()
    marble, marble_meta = select_marble()
    all_ids = [f"swe:{x['task_id']}" for x in swe]
    all_ids += [f"tau:{x['domain']}:{x['task_id']}" for x in tau]
    all_ids += [f"marble:{x['scenario']}:{x['task_id']}" for x in marble]
    duplicates = [item for item, count in Counter(all_ids).items() if count > 1]
    if duplicates:
        raise RuntimeError(f"Duplicate selected task IDs: {duplicates}")

    manifest = {
        "schema": "zyra.public-task-manifest/v2",
        "seed": SEED,
        "status": "frozen-before-model-results",
        "answer_fields_included": False,
        "selected_task_count": len(all_ids),
        "gaia": {
            "status": "pending-official-access",
            "planned_count": 12,
            "selection": "four validation tasks per official difficulty level",
        },
        "swe_bench_verified": {"metadata": swe_meta, "tasks": swe},
        "tau2_bench": {"metadata": tau_meta, "tasks": tau},
        "multiagentbench": {"metadata": marble_meta, "tasks": marble},
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "selected_without_gaia": len(all_ids)}))


if __name__ == "__main__":
    main()
