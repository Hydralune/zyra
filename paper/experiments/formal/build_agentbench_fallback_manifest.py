"""Freeze a small, answer-free official AgentBench v0.2 fallback set.

Used only when GAIA remains inaccessible before the first benchmark result.
The manifest intentionally stores record hashes and selectors, never prompts,
examples, SQL, or evaluator answers.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / ".tmp/datasets/agentbench-v0.2/data"
OUTPUT = ROOT / "paper/experiments/tasksets/agentbench-fallback-task-manifest-v1.json"
SEED = "zyra-agentbench-fallback-v1-20260907"


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def rank(identifier: str) -> str:
    return hashlib.sha256(f"{SEED}:{identifier}".encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_os() -> tuple[list[dict[str, str]], dict[str, str]]:
    # Four distinct official OS scenario groups: logs, shell/system, files,
    # and networking/permissions.  A deterministic hash chooses within each.
    groups = ("1", "3", "5", "7")
    selected = []
    files: dict[str, str] = {}
    for group in groups:
        candidates = []
        for path in sorted((DATA / "os_interaction/data" / group).glob("*.json")):
            rows = json.loads(path.read_text(encoding="utf-8"))
            files[str(path.relative_to(DATA))] = file_digest(path)
            for index, row in enumerate(rows):
                identifier = f"os-{group}-{path.stem}-{index}"
                candidates.append((identifier, row))
        identifier, row = min(candidates, key=lambda item: rank(item[0]))
        selected.append({"task_id": identifier, "scenario_group": group, "record_digest": digest(row)})
    return selected, files


def select_db() -> tuple[list[dict[str, str]], str]:
    path = DATA / "dbbench/standard.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    by_type: dict[str, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for index, row in enumerate(rows):
        category = str(row.get("type", "uncategorized"))
        by_type[category].append((f"db-{index}", row))
    # Prefer breadth if the release exposes at least four task types.
    choices = []
    for category in sorted(by_type, key=lambda value: rank(f"db-type:{value}"))[:4]:
        identifier, row = min(by_type[category], key=lambda item: rank(item[0]))
        choices.append({"task_id": identifier, "type": category, "record_digest": digest(row)})
    if len(choices) != 4:
        raise RuntimeError("Expected at least four AgentBench DB task types")
    return choices, file_digest(path)


def select_alfworld() -> tuple[list[dict[str, str]], str]:
    path = DATA / "alfworld/standard.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    preferred = ("pick_and_place", "pick_clean_then_place", "pick_heat_then_place", "look_at_obj")
    selected = []
    for category in preferred:
        rows = records.get(category, [])
        if not rows:
            raise RuntimeError(f"Expected ALFWorld category {category}")
        index, row = min(enumerate(rows), key=lambda item: rank(f"alf-{category}-{item[0]}"))
        selected.append({"task_id": f"alf-{category}-{index}", "category": category, "record_digest": digest(row)})
    return selected, file_digest(path)


def main() -> None:
    os_tasks, os_files = select_os()
    db_tasks, db_hash = select_db()
    alf_tasks, alf_hash = select_alfworld()
    records = [*os_tasks, *db_tasks, *alf_tasks]
    if len({item["record_digest"] for item in records}) != len(records):
        raise RuntimeError("Fallback selection contains duplicate task records")
    manifest = {
        "schema": "zyra.agentbench-fallback-task-manifest/v1",
        "status": "frozen-before-model-results",
        "source": {"repository": "THUDM/AgentBench", "tag": "v0.2", "commit": "ed013ff9887b0c3d7864c56ae54d41eba54a99d8"},
        "seed": SEED,
        "answer_fields_included": False,
        "selected_task_count": len(records),
        "selection_reason": "GAIA official access unavailable before results; fixed resource-bounded fallback",
        "os_interaction": {"files_sha256": os_files, "tasks": os_tasks},
        "dbbench": {"source_sha256": db_hash, "tasks": db_tasks},
        "alfworld": {"source_sha256": alf_hash, "tasks": alf_tasks},
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT), "selected": len(records)}))


if __name__ == "__main__":
    main()
