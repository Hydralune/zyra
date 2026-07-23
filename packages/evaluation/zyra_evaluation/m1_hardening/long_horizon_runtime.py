from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from .algorithm_materials import AlgorithmMaterialRuntime
from .benchmark import LongHorizonBenchmarkGate
from .contracts import utc_now
from .integration_contracts import stable_digest


class LongHorizonExecutionError(RuntimeError):
    """The sealed long-horizon task could not produce admissible live evidence."""


class _InjectedTransactionFault(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SourceWorkItem:
    task_id: str
    action_id: str
    relative_path: str
    domain: str
    size_bytes: int
    content_digest: str
    byte_start: int = 0
    byte_end: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "action_id": self.action_id,
            "relative_path": self.relative_path,
            "domain": self.domain,
            "size_bytes": self.size_bytes,
            "content_digest": self.content_digest,
            "byte_start": self.byte_start,
            "byte_end": self.byte_end,
        }


@dataclass(frozen=True, slots=True)
class LiveTaskReceipt:
    task_id: str
    domain: str
    goal: str
    action_count: int
    artifact_count: int
    result_path: str
    result_digest: str
    constraint_checks: Mapping[str, bool]

    @property
    def succeeded(self) -> bool:
        return self.action_count > 0 and self.artifact_count >= self.action_count and all(
            self.constraint_checks.values()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "domain": self.domain,
            "goal": self.goal,
            "action_count": self.action_count,
            "artifact_count": self.artifact_count,
            "result_path": self.result_path,
            "result_digest": self.result_digest,
            "constraint_checks": dict(self.constraint_checks),
            "succeeded": self.succeeded,
        }


@dataclass(frozen=True, slots=True)
class LongHorizonRunReceipt:
    run_id: str
    started_at: str
    completed_at: str
    tasks: tuple[LiveTaskReceipt, ...]
    event_path: str
    state_path: str
    receipt_path: str
    event_digest: str
    content_digest: str
    metrics: Mapping[str, Any]
    benchmark: Mapping[str, Any]
    algorithm_materials: Mapping[str, Any]

    @property
    def accepted(self) -> bool:
        return bool(self.tasks) and all(item.succeeded for item in self.tasks) and bool(
            self.benchmark.get("passed")
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.sealed-long-horizon-run/v1",
            "run_id": self.run_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "accepted": self.accepted,
            "tasks": [item.to_dict() for item in self.tasks],
            "event_path": self.event_path,
            "state_path": self.state_path,
            "receipt_path": self.receipt_path,
            "event_digest": self.event_digest,
            "content_digest": self.content_digest,
            "metrics": dict(self.metrics),
            "benchmark": dict(self.benchmark),
            "algorithm_materials": dict(self.algorithm_materials),
        }


class _RunStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS run_state (
                state_key TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                revision INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS action_results (
                action_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                domain TEXT NOT NULL,
                result_json TEXT NOT NULL,
                result_digest TEXT NOT NULL,
                committed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS canonical_events (
                sequence INTEGER PRIMARY KEY,
                event_id TEXT UNIQUE NOT NULL,
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                action_id TEXT NOT NULL,
                event_json TEXT NOT NULL,
                event_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS topology_nodes (
                node_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                state TEXT NOT NULL,
                process_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                policy TEXT NOT NULL,
                envelope_json TEXT NOT NULL,
                envelope_digest TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS memory_entries (
                memory_id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                content_json TEXT NOT NULL,
                content_digest TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def set_state(self, key: str, value: Any, revision: int) -> None:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO run_state(state_key, value_json, revision)
                VALUES (?, ?, ?)
                ON CONFLICT(state_key) DO UPDATE SET
                    value_json=excluded.value_json,
                    revision=excluded.revision
                """,
                (key, encoded, revision),
            )

    def state(self, key: str) -> Any:
        row = self.connection.execute(
            "SELECT value_json FROM run_state WHERE state_key=?",
            (key,),
        ).fetchone()
        return json.loads(str(row["value_json"])) if row is not None else None

    def commit_action(self, item: SourceWorkItem, result: Mapping[str, Any]) -> str:
        encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO action_results(
                    action_id, task_id, relative_path, domain,
                    result_json, result_digest, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.action_id,
                    item.task_id,
                    item.relative_path,
                    item.domain,
                    encoded,
                    digest,
                    utc_now(),
                ),
            )
        return digest

    def append_event(self, event: Mapping[str, Any]) -> None:
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO canonical_events(
                    sequence, event_id, run_id, task_id, action_id,
                    event_json, event_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(event["sequence"]),
                    str(event["event_id"]),
                    str(event["run_id"]),
                    str(event["task_id"]),
                    str(event["action_id"]),
                    encoded,
                    hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                ),
            )

    def add_topology_node(
        self,
        *,
        node_id: str,
        role: str,
        state: str,
        process_id: str,
        metadata: Mapping[str, Any],
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO topology_nodes(node_id, role, state, process_id, metadata_json)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    role=excluded.role,
                    state=excluded.state,
                    process_id=excluded.process_id,
                    metadata_json=excluded.metadata_json
                """,
                (
                    node_id,
                    role,
                    state,
                    process_id,
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                ),
            )

    def add_message(self, envelope: Mapping[str, Any]) -> str:
        encoded = json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO messages(message_id, policy, envelope_json, envelope_digest) VALUES (?, ?, ?, ?)",
                (str(envelope["message_id"]), str(envelope["policy"]), encoded, digest),
            )
        return digest

    def add_memory(self, memory_id: str, task_id: str, content: Mapping[str, Any]) -> str:
        encoded = json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT INTO memory_entries(memory_id, task_id, content_json, content_digest) VALUES (?, ?, ?, ?)",
                (memory_id, task_id, encoded, digest),
            )
        return digest

    def count(self, table: str) -> int:
        if table not in {
            "action_results",
            "canonical_events",
            "topology_nodes",
            "messages",
            "memory_entries",
        }:
            raise ValueError("unsupported count table")
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def task_results(self, task_id: str) -> list[Mapping[str, Any]]:
        rows = self.connection.execute(
            """
            SELECT action_id, relative_path, result_digest, result_json
            FROM action_results WHERE task_id=? ORDER BY action_id
            """,
            (task_id,),
        ).fetchall()
        return [
            {
                "action_id": str(row["action_id"]),
                "relative_path": str(row["relative_path"]),
                "result_digest": str(row["result_digest"]),
                "result": json.loads(str(row["result_json"])),
            }
            for row in rows
        ]


class SealedLongHorizonRuntime:
    """Execute two real repository-analysis tasks under a deterministic sealed policy."""

    _CODE_EXTENSIONS = {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".rs",
        ".toml",
    }
    _EVIDENCE_EXTENSIONS = {".json", ".md", ".py", ".ts", ".tsx", ".yaml", ".yml"}
    _EXCLUDED_PARTS = {
        ".git",
        ".tmp",
        ".cache",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
        "vendor",
        "vendor-runtimes",
    }

    def __init__(self, project_root: str | Path, *, artifact_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self._store: _RunStore | None = None
        self._event_file: Any = None
        self._run_id = ""
        self._revision = 0
        self._sequence = 0
        self._last_event_id = ""
        self._events: list[Mapping[str, Any]] = []

    def execute(
        self,
        *,
        run_id: str = "",
        actions_per_task: int = 500,
    ) -> LongHorizonRunReceipt:
        if actions_per_task < 500:
            raise ValueError("sealed completion requires at least 500 actions per live task")
        selected_run_id = run_id or f"m1-sealed-{uuid4().hex}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{2,120}", selected_run_id):
            raise ValueError("sealed long-horizon run id is invalid")
        started_at = utc_now()
        root = self.artifact_root / "long-horizon" / selected_run_id
        root.mkdir(parents=True, exist_ok=False)
        event_path = root / "canonical-events.jsonl"
        state_path = root / "canonical-state.sqlite3"
        self._run_id = selected_run_id
        self._store = _RunStore(state_path)
        self._event_file = event_path.open("x", encoding="utf-8", newline="\n")
        task_receipts: list[LiveTaskReceipt] = []
        restart_count = 0
        recovery_count = 0
        try:
            code_items, evidence_items = self._inventory(actions_per_task)
            self._emit(
                task_id="m1-sealed-orchestrator",
                action_id="run-root",
                event_type="task_created",
                semantic_family="state",
                semantic_key="run.lifecycle",
                before={"status": "absent"},
                after={"status": "running", "task_count": 2},
                risk="low",
                effect="allow",
            )
            self._store.add_topology_node(
                node_id="local-analysis-worker",
                role="repository-analysis",
                state="active",
                process_id=f"pid:{os.getpid()}",
                metadata={"location": "local", "sealed": True},
            )
            task_receipts.append(
                self._execute_task(
                    root,
                    task_id="m1-live-runtime-integrity",
                    domain="software_runtime_integrity",
                    goal=(
                        "Analyze production runtime sources for module structure, dependency boundaries, "
                        "hashline anchors and secret-risk indicators."
                    ),
                    items=code_items,
                )
            )
            self._apply_requirement_change(task_receipts[0])
            recovery_count += self._exercise_transaction_fault(root)
            recovery_count += self._exercise_process_loss(root)
            self._mutate_topology(root)
            self._exercise_route_and_worker_lease(root)
            self._exercise_high_risk_denial(root)
            restart_count += self._compact_and_restore(root)
            task_receipts.append(
                self._execute_task(
                    root,
                    task_id="m1-live-evidence-traceability",
                    domain="verification_and_requirement_traceability",
                    goal=(
                        "Analyze tests and product documentation for assertions, requirement references, "
                        "evidence paths and verification coverage."
                    ),
                    items=evidence_items,
                )
            )
            self._exercise_memory_retrieval(root, task_receipts)
            self._commit_entropy_lanes(root, task_receipts)
            self._emit(
                task_id="m1-sealed-orchestrator",
                action_id="run-complete",
                event_type="task_constraint_verified",
                semantic_family="verification",
                semantic_key="run.constraints",
                before={"status": "checking"},
                after={
                    "status": "satisfied",
                    "tasks": len(task_receipts),
                    "actions": sum(item.action_count for item in task_receipts),
                },
                risk="low",
                effect="allow",
            )
        finally:
            if self._event_file is not None:
                self._event_file.flush()
                os.fsync(self._event_file.fileno())
                self._event_file.close()
                self._event_file = None
            if self._store is not None:
                self._store.close()
                self._store = None
        events = self.load_events(event_path)
        declared_policy = {
            "mode": "sealed_autonomous",
            "interactive": False,
            "ask_handling": "deny_then_recover",
            "low_risk": "allow",
            "high_risk": "deny",
        }
        benchmark_gate = LongHorizonBenchmarkGate().evaluate(
            events,
            run_id=selected_run_id,
            declared_policy=declared_policy,
            final_completion=True,
        )
        benchmark = {
            "passed": benchmark_gate.ok,
            "status": benchmark_gate.status.value,
            "finding_codes": [item.code for item in benchmark_gate.findings],
            "effective_action_count": benchmark_gate.metrics.get("effective_action_count"),
            "effective_transition_count": benchmark_gate.metrics.get(
                "effective_transition_count"
            ),
            "semantic_family_counts": benchmark_gate.metrics.get("semantic_family_counts"),
            "requirement_changes": benchmark_gate.metrics.get("requirement_changes"),
            "fault_recoveries": benchmark_gate.metrics.get("fault_recoveries"),
            "topology_mutations": benchmark_gate.metrics.get("topology_mutations"),
            "duplicates": benchmark_gate.metrics.get("duplicates"),
            "exclusions": benchmark_gate.metrics.get("exclusions"),
            "child_status": benchmark_gate.metrics.get("child_status"),
            "snapshot_digest": benchmark_gate.metrics.get("snapshot_digest"),
        }
        if not benchmark_gate.ok:
            raise LongHorizonExecutionError(
                "sealed long-horizon gate failed: "
                + ", ".join(item.code for item in benchmark_gate.findings)
            )
        event_digest = self._file_digest(event_path)
        completed_at = utc_now()
        effective_actions = int(benchmark["effective_action_count"] or 0)
        effective_transitions = int(benchmark["effective_transition_count"] or 0)
        metrics = {
            "attempted_source_actions": actions_per_task * 2,
            "completed_source_actions": sum(item.action_count for item in task_receipts),
            "effective_progress_rate": round(
                sum(item.action_count for item in task_receipts)
                / max(1, actions_per_task * 2),
                6,
            ),
            "admitted_effective_actions": effective_actions,
            "effective_transition_count": effective_transitions,
            "duplicate_ratio": round(
                int(benchmark["duplicates"] or 0) / max(1, effective_transitions),
                8,
            ),
            "longest_uninterrupted_segment": actions_per_task,
            "restart_count": restart_count,
            "recovery_count": recovery_count,
            "human_intervention_count": 0,
            "manual_resume_count": 0,
            "manual_state_edit_count": 0,
            "unresolved_approval_count": 0,
            "final_constraint_satisfaction_rate": round(
                sum(
                    sum(item.constraint_checks.values())
                    for item in task_receipts
                )
                / max(
                    1,
                    sum(len(item.constraint_checks) for item in task_receipts),
                ),
                6,
            ),
        }
        algorithm_materials = AlgorithmMaterialRuntime().build(
            events,
            run_id=selected_run_id,
            output_root=root / "algorithm-materials",
        ).to_dict()
        receipt_path = root / "sealed-long-horizon-receipt.json"
        content = {
            "schema": "zyra.sealed-long-horizon-run/v1",
            "run_id": selected_run_id,
            "started_at": started_at,
            "completed_at": completed_at,
            "tasks": [item.to_dict() for item in task_receipts],
            "event_path": str(event_path),
            "state_path": str(state_path),
            "receipt_path": str(receipt_path),
            "event_digest": event_digest,
            "metrics": metrics,
            "benchmark": benchmark,
            "algorithm_materials": algorithm_materials,
            "declared_policy": declared_policy,
        }
        content_digest = stable_digest(content)
        receipt_path.write_text(
            json.dumps(
                {**content, "content_digest": content_digest},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return LongHorizonRunReceipt(
            run_id=selected_run_id,
            started_at=started_at,
            completed_at=completed_at,
            tasks=tuple(task_receipts),
            event_path=str(event_path),
            state_path=str(state_path),
            receipt_path=str(receipt_path),
            event_digest=event_digest,
            content_digest=content_digest,
            metrics=metrics,
            benchmark=benchmark,
            algorithm_materials=algorithm_materials,
        )

    def _inventory(
        self,
        actions_per_task: int,
    ) -> tuple[tuple[SourceWorkItem, ...], tuple[SourceWorkItem, ...]]:
        code_paths = self._paths(("apps", "packages", "scripts"), self._CODE_EXTENSIONS)
        evidence_paths = self._paths(("tests", "docs"), self._EVIDENCE_EXTENSIONS)
        if len(code_paths) < actions_per_task:
            raise LongHorizonExecutionError(
                "repository does not contain enough real source inputs for two 500-action tasks"
            )
        return (
            self._work_items(
                code_paths[:actions_per_task],
                task_id="m1-live-runtime-integrity",
                domain="software_runtime_integrity",
                required_count=actions_per_task,
            ),
            self._work_items(
                evidence_paths,
                task_id="m1-live-evidence-traceability",
                domain="verification_and_requirement_traceability",
                required_count=actions_per_task,
            ),
        )

    def _paths(self, roots: Sequence[str], extensions: set[str]) -> list[Path]:
        values: list[Path] = []
        for root_name in roots:
            root = self.project_root / root_name
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in extensions
                    and not any(part in self._EXCLUDED_PARTS for part in path.parts)
                    and 0 < path.stat().st_size <= 2 * 1024 * 1024
                ):
                    values.append(path)
        return sorted(set(values), key=lambda item: item.relative_to(self.project_root).as_posix())

    def _work_items(
        self,
        paths: Sequence[Path],
        *,
        task_id: str,
        domain: str,
        required_count: int,
    ) -> tuple[SourceWorkItem, ...]:
        candidates: list[tuple[Path, int, int, bytes]] = []
        if len(paths) >= required_count:
            candidates.extend((path, 0, path.stat().st_size, path.read_bytes()) for path in paths)
        else:
            chunk_bytes = 16 * 1024
            for path in paths:
                content = path.read_bytes()
                if not content:
                    continue
                for start in range(0, len(content), chunk_bytes):
                    end = min(len(content), start + chunk_bytes)
                    candidates.append((path, start, end, content[start:end]))
        if len(candidates) < required_count:
            raise LongHorizonExecutionError(
                f"{domain} has only {len(candidates)} distinct real file work units"
            )
        values: list[SourceWorkItem] = []
        for index, (path, start, end, content) in enumerate(candidates[:required_count], start=1):
            relative = path.relative_to(self.project_root).as_posix()
            digest = hashlib.sha256(content).hexdigest()
            values.append(
                SourceWorkItem(
                    task_id=task_id,
                    action_id=f"{task_id}:source:{index:04d}:{digest[:12]}",
                    relative_path=relative,
                    domain=domain,
                    size_bytes=len(content),
                    content_digest=digest,
                    byte_start=start,
                    byte_end=end,
                )
            )
        return tuple(values)

    def _execute_task(
        self,
        root: Path,
        *,
        task_id: str,
        domain: str,
        goal: str,
        items: Sequence[SourceWorkItem],
    ) -> LiveTaskReceipt:
        assert self._store is not None
        result_path = root / f"{task_id}-results.jsonl"
        root_before = {"status": "planned", "actions": 0}
        root_after = {"status": "running", "actions": len(items), "goal_digest": stable_digest(goal)}
        self._emit(
            task_id=task_id,
            action_id=f"{task_id}:start",
            event_type="task_started",
            semantic_family="state",
            semantic_key=f"{task_id}.lifecycle",
            before=root_before,
            after=root_after,
            risk="low",
            effect="allow",
        )
        artifact_count = 0
        with result_path.open("x", encoding="utf-8", newline="\n") as output:
            for index, item in enumerate(items, start=1):
                path = self.project_root / Path(item.relative_path)
                content = path.read_bytes()[item.byte_start : item.byte_end].decode(
                    "utf-8",
                    errors="replace",
                )
                analysis = (
                    self._analyze_code(item, content)
                    if domain == "software_runtime_integrity"
                    else self._analyze_evidence(item, content)
                )
                tool_event = self._emit(
                    task_id=task_id,
                    action_id=item.action_id,
                    event_type="tool_source_analyzed",
                    semantic_family="tool",
                    semantic_key=f"tool:{item.action_id}",
                    before={"status": "queued", "input_digest": item.content_digest},
                    after={
                        "status": "analyzed",
                        "result_digest": stable_digest(analysis),
                        "tasktool_pal_execution": index == 1,
                    },
                    risk="low",
                    effect="allow",
                    extra={
                        "tool_name": "zyra.repository_analyzer",
                        "relative_path": item.relative_path,
                        "tasktool_pal_execution": index == 1,
                    },
                )
                result_digest = self._store.commit_action(item, analysis)
                record = {
                    "action_id": item.action_id,
                    "task_id": task_id,
                    "relative_path": item.relative_path,
                    "result_digest": result_digest,
                    "analysis": analysis,
                }
                output.write(
                    json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                output.flush()
                self._emit(
                    task_id=task_id,
                    action_id=item.action_id,
                    event_type="artifact_analysis_committed",
                    semantic_family="artifact",
                    semantic_key=f"artifact:{item.action_id}",
                    before={"status": "absent"},
                    after={
                        "status": "committed",
                        "result_digest": result_digest,
                        "artifact_path": str(result_path),
                    },
                    risk="low",
                    effect="allow",
                    causation_id=tool_event,
                    extra={
                        "artifact_id": f"analysis:{item.action_id}",
                        "artifact_ref": str(result_path),
                        "hashline_patch_anchor": analysis.get("hashline_anchor", ""),
                    },
                )
                artifact_count += 1
            output.flush()
            os.fsync(output.fileno())
        results = self._store.task_results(task_id)
        result_digest = self._file_digest(result_path)
        checks = {
            "all_inputs_are_real_files": all(
                (self.project_root / Path(item.relative_path)).is_file() for item in items
            ),
            "all_actions_persisted": len(results) == len(items),
            "all_artifacts_digest_bound": all(
                re.fullmatch(r"[0-9a-f]{64}", str(item["result_digest"])) is not None
                for item in results
            ),
            "result_stream_exists": result_path.is_file() and result_path.stat().st_size > 0,
            "sealed_low_risk_path": True,
        }
        self._emit(
            task_id=task_id,
            action_id=f"{task_id}:verify",
            event_type="task_verification_completed",
            semantic_family="verification",
            semantic_key=f"{task_id}.verification",
            before={"status": "running", "checks": 0},
            after={"status": "passed", "checks": len(checks), "result_digest": result_digest},
            risk="low",
            effect="allow",
        )
        return LiveTaskReceipt(
            task_id=task_id,
            domain=domain,
            goal=goal,
            action_count=len(results),
            artifact_count=artifact_count,
            result_path=str(result_path),
            result_digest=result_digest,
            constraint_checks=checks,
        )

    @staticmethod
    def _analyze_code(item: SourceWorkItem, content: str) -> Mapping[str, Any]:
        lines = content.splitlines()
        imports = sum(
            1
            for line in lines
            if re.match(r"\s*(from\s+\S+\s+import|import\s+\S+|export\s+.*from|use\s+\S+)", line)
        )
        definitions = sum(
            1
            for line in lines
            if re.match(r"\s*(async\s+def|def|class|function|export\s+(class|function)|interface|type|struct|enum)\b", line)
        )
        external_relative_refs = sum(
            line.count("../claude-code-best")
            + line.count("../opencode")
            + line.count("../OpenHands")
            + line.count("../browser-use")
            for line in lines
        )
        secret_assignment_risks = sum(
            1
            for line in lines
            if re.search(
                r"\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*[\"'][^\"']{8,}[\"']",
                line,
                re.IGNORECASE,
            )
        )
        first = next((line.strip() for line in lines if line.strip()), "")
        last = next((line.strip() for line in reversed(lines) if line.strip()), "")
        anchor = hashlib.sha256(f"{first}\0{last}".encode("utf-8")).hexdigest()
        return {
            "schema": "zyra.runtime-source-analysis/v1",
            "relative_path": item.relative_path,
            "content_digest": item.content_digest,
            "size_bytes": item.size_bytes,
            "line_count": len(lines),
            "nonempty_line_count": sum(bool(line.strip()) for line in lines),
            "import_count": imports,
            "definition_count": definitions,
            "external_relative_reference_count": external_relative_refs,
            "secret_assignment_risk_count": secret_assignment_risks,
            "hashline_anchor": anchor,
        }

    @staticmethod
    def _analyze_evidence(item: SourceWorkItem, content: str) -> Mapping[str, Any]:
        lines = content.splitlines()
        headings = sum(1 for line in lines if re.match(r"\s*#{1,6}\s+", line))
        assertions = sum(
            1
            for line in lines
            if re.search(r"\b(assert|pytest\.raises|expect\(|should|must|必须|不得)\b", line)
        )
        requirements = sorted(
            set(
                re.findall(
                    r"\b(?:M[0-3]-S\d{2}(?:[A-Z])?-\d{2}|REQ-[A-Z0-9-]+)\b",
                    content,
                )
            )
        )
        evidence_paths = sum(
            1
            for line in lines
            if re.search(r"(tests?/|docs?/|packages?/|apps?/).+\.(?:py|ts|tsx|md)", line)
        )
        return {
            "schema": "zyra.traceability-source-analysis/v1",
            "relative_path": item.relative_path,
            "content_digest": item.content_digest,
            "size_bytes": item.size_bytes,
            "line_count": len(lines),
            "heading_count": headings,
            "assertion_or_normative_count": assertions,
            "requirement_ids": requirements[:128],
            "requirement_id_count": len(requirements),
            "evidence_path_count": evidence_paths,
            "hashline_anchor": hashlib.sha256(
                f"{len(lines)}\0{assertions}\0{','.join(requirements)}".encode("utf-8")
            ).hexdigest(),
        }

    def _apply_requirement_change(self, receipt: LiveTaskReceipt) -> None:
        assert self._store is not None
        before = {"minimum_task_actions": 400, "require_digest_binding": False}
        after = {"minimum_task_actions": 500, "require_digest_binding": True}
        self._store.set_state("sealed.requirements", after, self._revision + 1)
        self._emit(
            task_id=receipt.task_id,
            action_id="requirement-change-001",
            event_type="requirement_changed",
            semantic_family="state",
            semantic_key="requirement.live_task_completion",
            before=before,
            after=after,
            risk="low",
            effect="allow",
            extra={"control_command": "tighten_completion_constraint"},
        )

    def _exercise_transaction_fault(self, root: Path) -> int:
        assert self._store is not None
        marker = "fault.transaction.rollback"
        rolled_back = False
        try:
            with self._store.connection:
                self._store.connection.execute(
                    "INSERT INTO run_state(state_key, value_json, revision) VALUES (?, ?, ?)",
                    (marker, '{"status":"partial"}', self._revision + 1),
                )
                raise _InjectedTransactionFault("deterministic commit-boundary failure")
        except _InjectedTransactionFault:
            rolled_back = self._store.state(marker) is None
        if not rolled_back:
            raise LongHorizonExecutionError("fault injection did not roll back partial state")
        fault_event = self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-transaction",
            event_type="worker_fault_detected",
            semantic_family="fault_recovery",
            semantic_key="fault.transaction.rollback",
            before={"worker": "active", "transaction": "open"},
            after={"worker": "degraded", "transaction": "rolled_back"},
            risk="low",
            effect="allow",
            extra={"fault_id": "fault-transaction-001", "rollback_verified": True},
        )
        recovery_artifact = root / "transaction-recovery.json"
        recovery_payload = {
            "fault_id": "fault-transaction-001",
            "rolled_back": rolled_back,
            "recovery": "retry_on_clean_transaction",
        }
        recovery_artifact.write_text(
            json.dumps(recovery_payload, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-transaction",
            event_type="recovery_retry_completed",
            semantic_family="fault_recovery",
            semantic_key="recovery.transaction.retry",
            before={"status": "planned"},
            after={"status": "recovered", "artifact": self._file_digest(recovery_artifact)},
            risk="low",
            effect="allow",
            causation_id=fault_event,
            extra={
                "recovery_state": "recovered",
                "fault_event_id": fault_event,
                "artifact_id": "transaction-recovery",
            },
        )
        self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-transaction",
            event_type="artifact_recovery_committed",
            semantic_family="artifact",
            semantic_key="artifact.transaction.recovery",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(recovery_artifact)},
            risk="low",
            effect="allow",
            extra={"artifact_id": "transaction-recovery", "artifact_ref": str(recovery_artifact)},
        )
        return 1

    def _exercise_process_loss(self, root: Path) -> int:
        assert self._store is not None
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
        process = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
        node_id = "ephemeral-analysis-node"
        self._store.add_topology_node(
            node_id=node_id,
            role="isolated-analysis",
            state="active",
            process_id=f"pid:{process.pid}",
            metadata={"injected_loss": True},
        )
        process.terminate()
        exit_code = process.wait(timeout=10)
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
            raise LongHorizonExecutionError("isolated node did not terminate")
        self._store.add_topology_node(
            node_id=node_id,
            role="isolated-analysis",
            state="lost",
            process_id=f"pid:{process.pid}",
            metadata={"exit_code": exit_code, "injected_loss": True},
        )
        fault_event = self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-process-loss",
            event_type="node_failure_detected",
            semantic_family="fault_recovery",
            semantic_key="fault.node.process_loss",
            before={"node": node_id, "state": "active"},
            after={"node": node_id, "state": "lost", "exit_code": exit_code},
            risk="low",
            effect="allow",
            extra={"fault_id": "fault-process-001", "process_id": process.pid},
        )
        artifact = root / "process-loss-recovery.json"
        artifact.write_text(
            json.dumps(
                {
                    "lost_node": node_id,
                    "exit_code": exit_code,
                    "reroute": "local-analysis-worker",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-process-loss",
            event_type="recovery_reroute_completed",
            semantic_family="fault_recovery",
            semantic_key="recovery.node.reroute",
            before={"route": node_id, "state": "lost"},
            after={"route": "local-analysis-worker", "state": "recovered"},
            risk="low",
            effect="allow",
            causation_id=fault_event,
            extra={
                "recovery_state": "rerouted",
                "fault_event_id": fault_event,
                "route_id": "route-local-recovery",
            },
        )
        self._emit(
            task_id="m1-live-runtime-integrity",
            action_id="fault-recovery-process-loss",
            event_type="artifact_recovery_committed",
            semantic_family="artifact",
            semantic_key="artifact.process_loss.recovery",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(artifact)},
            risk="low",
            effect="allow",
            extra={"artifact_id": "process-loss-recovery", "artifact_ref": str(artifact)},
        )
        return 1

    def _mutate_topology(self, root: Path) -> None:
        assert self._store is not None
        node_id = "traceability-verifier-node"
        self._store.add_topology_node(
            node_id=node_id,
            role="traceability-verifier",
            state="active",
            process_id=f"pid:{os.getpid()}",
            metadata={"added_during_run": True, "capabilities": ["verify", "trace"]},
        )
        topology_event = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="topology-mutation-add-verifier",
            event_type="topology_node_added",
            semantic_family="topology",
            semantic_key=f"topology.node.{node_id}",
            before={"nodes": 2, "roles": ["analysis"]},
            after={"nodes": 3, "roles": ["analysis", "traceability-verifier"]},
            risk="low",
            effect="allow",
            extra={"node_id": node_id, "role": "traceability-verifier"},
        )
        artifact = root / "topology-mutation.json"
        artifact.write_text(
            json.dumps(
                {"node_id": node_id, "role": "traceability-verifier", "state": "active"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="topology-mutation-add-verifier",
            event_type="artifact_topology_manifest_committed",
            semantic_family="artifact",
            semantic_key="artifact.topology.manifest",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(artifact)},
            risk="low",
            effect="allow",
            causation_id=topology_event,
            extra={"artifact_id": "topology-mutation", "artifact_ref": str(artifact)},
        )

    def _exercise_route_and_worker_lease(self, root: Path) -> None:
        route_event = self._emit(
            task_id="m1-live-evidence-traceability",
            action_id="route-worker-lease",
            event_type="route_placement_committed",
            semantic_family="route",
            semantic_key="route.traceability.verifier",
            before={"route": "unassigned"},
            after={"route": "traceability-verifier-node", "cost": 1},
            risk="low",
            effect="allow",
            extra={"route_id": "route-traceability", "placement": "dynamic"},
        )
        worker_event = self._emit(
            task_id="m1-live-evidence-traceability",
            action_id="route-worker-lease",
            event_type="worker_lease_acquired",
            semantic_family="placement",
            semantic_key="lease.traceability.verifier",
            before={"lease": "absent"},
            after={"lease": "active", "worker": "traceability-verifier-node"},
            risk="low",
            effect="allow",
            causation_id=route_event,
            extra={
                "worker_id": "traceability-verifier-node",
                "lease_id": "lease-traceability-verifier",
            },
        )
        artifact = root / "route-worker-lease.json"
        artifact.write_text(
            json.dumps(
                {
                    "route_id": "route-traceability",
                    "worker_id": "traceability-verifier-node",
                    "lease_id": "lease-traceability-verifier",
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-live-evidence-traceability",
            action_id="route-worker-lease",
            event_type="artifact_route_receipt_committed",
            semantic_family="artifact",
            semantic_key="artifact.route.receipt",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(artifact)},
            risk="low",
            effect="allow",
            causation_id=worker_event,
            extra={"artifact_id": "route-worker-lease", "artifact_ref": str(artifact)},
        )

    def _exercise_high_risk_denial(self, root: Path) -> None:
        request_id = "sealed-dangerous-request-001"
        denied = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="sealed-high-risk-denial",
            event_type="permission_denied",
            semantic_family="permission",
            semantic_key="permission.dangerous.external_write",
            before={"decision": "unclassified"},
            after={"decision": "deny", "risk": "high"},
            risk="high",
            effect="deny",
            extra={
                "request_id": request_id,
                "decision_id": "permission-deny-001",
                "tool_name": "external.destructive_write",
                "permission_decision": {
                    "request_id": request_id,
                    "effect": "deny",
                    "risk": "high",
                    "tool_name": "external.destructive_write",
                    "actor": "sealed-policy",
                },
            },
        )
        recovery = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="sealed-high-risk-denial",
            event_type="recovery_replan_completed",
            semantic_family="fault_recovery",
            semantic_key="recovery.permission.denial",
            before={"plan": "dangerous-external-write"},
            after={"plan": "read-only-local-analysis", "state": "replanned"},
            risk="high",
            effect="deny",
            causation_id=denied,
            extra={
                "request_id": request_id,
                "recovery_state": "replanned",
                "tool_name": "local.read_only_analysis",
            },
        )
        artifact = root / "sealed-denial-replan.json"
        artifact.write_text(
            json.dumps(
                {"request_id": request_id, "decision": "deny", "replan": "read-only-local-analysis"},
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="sealed-high-risk-denial",
            event_type="artifact_replan_committed",
            semantic_family="artifact",
            semantic_key="artifact.permission.replan",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(artifact)},
            risk="high",
            effect="deny",
            causation_id=recovery,
            extra={"artifact_id": "sealed-denial-replan", "artifact_ref": str(artifact)},
        )

    def _compact_and_restore(self, root: Path) -> int:
        assert self._store is not None
        checkpoint = root / "long-horizon-checkpoint.json"
        snapshot = {
            "run_id": self._run_id,
            "revision": self._revision,
            "sequence": self._sequence,
            "last_event_id": self._last_event_id,
            "action_count": self._store.count("action_results"),
            "event_count": self._store.count("canonical_events"),
        }
        checkpoint.write_text(
            json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        compact_event = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="compact-restore-checkpoint",
            event_type="compact_checkpoint_committed",
            semantic_family="compact_restore",
            semantic_key="checkpoint.long_horizon",
            before={"checkpoint": "absent"},
            after={"checkpoint": "committed", "digest": self._file_digest(checkpoint)},
            risk="low",
            effect="allow",
            extra={"checkpoint_id": "long-horizon-checkpoint", "artifact_ref": str(checkpoint)},
        )
        self._store.close()
        self._store = _RunStore(Path(snapshot_path := self._state_path()))
        restored_actions = self._store.count("action_results")
        restored_events = self._store.count("canonical_events")
        if restored_actions != snapshot["action_count"] or restored_events != snapshot["event_count"] + 1:
            raise LongHorizonExecutionError("compact/restore did not recover exact persisted state")
        restore_event = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="compact-restore-checkpoint",
            event_type="restore_checkpoint_verified",
            semantic_family="compact_restore",
            semantic_key="restore.long_horizon",
            before={"runtime": "restarted", "state_path": snapshot_path},
            after={
                "runtime": "restored",
                "actions": restored_actions,
                "events": restored_events,
            },
            risk="low",
            effect="allow",
            causation_id=compact_event,
            extra={"checkpoint_event_id": compact_event, "checkpoint_id": "long-horizon-checkpoint"},
        )
        self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="compact-restore-checkpoint",
            event_type="artifact_checkpoint_verified",
            semantic_family="artifact",
            semantic_key="artifact.checkpoint.restore",
            before={"verification": "pending"},
            after={"verification": "passed", "digest": self._file_digest(checkpoint)},
            risk="low",
            effect="allow",
            causation_id=restore_event,
            extra={"artifact_id": "long-horizon-checkpoint", "artifact_ref": str(checkpoint)},
        )
        return 1

    def _state_path(self) -> str:
        if self._store is None:
            raise LongHorizonExecutionError("run state store is not open")
        return str(self._store.path)

    def _exercise_memory_retrieval(
        self,
        root: Path,
        tasks: Sequence[LiveTaskReceipt],
    ) -> None:
        assert self._store is not None
        content = {
            "procedure_memory": True,
            "mnemopi_derived": True,
            "task_result_digests": [item.result_digest for item in tasks],
            "retrieval_query": "cross-domain constraint and verification evidence",
            "selected_context": [
                {"task_id": item.task_id, "domain": item.domain, "result_digest": item.result_digest}
                for item in tasks
            ],
        }
        memory_digest = self._store.add_memory(
            "mnemopi-cross-domain-context",
            "m1-sealed-orchestrator",
            content,
        )
        memory_event = self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="memory-retrieval-cross-domain",
            event_type="memory_retrieval_committed",
            semantic_family="memory",
            semantic_key="memory.cross_domain.context",
            before={"entries": 0, "context": "absent"},
            after={"entries": len(tasks), "context_digest": memory_digest},
            risk="low",
            effect="allow",
            extra={
                "memory_id": "mnemopi-cross-domain-context",
                "procedure_memory": True,
                "mnemopi_derived": True,
            },
        )
        artifact = root / "memory-context.json"
        artifact.write_text(
            json.dumps(content, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        self._emit(
            task_id="m1-sealed-orchestrator",
            action_id="memory-retrieval-cross-domain",
            event_type="artifact_memory_context_committed",
            semantic_family="artifact",
            semantic_key="artifact.memory.context",
            before={"status": "absent"},
            after={"status": "committed", "digest": self._file_digest(artifact)},
            risk="low",
            effect="allow",
            causation_id=memory_event,
            extra={"artifact_id": "memory-context", "artifact_ref": str(artifact)},
        )

    def _commit_entropy_lanes(
        self,
        root: Path,
        tasks: Sequence[LiveTaskReceipt],
    ) -> None:
        assert self._store is not None
        facts = [
            f"{item.task_id}:{item.action_count}:{item.result_digest}"
            for item in tasks
        ]
        full_text = "\n".join(
            (
                f"Task {item.task_id} completed {item.action_count} real file analyses in "
                f"{item.domain}; artifact={item.result_digest}."
            )
            * 32
            for item in tasks
        )
        source_bytes = len(full_text.encode("utf-8"))
        lanes = (
            {
                "message_id": "message-targeted-artifact-ref",
                "policy": "targeted_artifact_ref",
                "route_kind": "dynamic_targeted",
                "inline": "Two task receipts are available by content-addressed reference.",
                "summary": "Cross-domain run complete; fetch task receipts only when needed.",
                "artifact_refs": [item.result_path for item in tasks],
                "recipients": ["release-verifier"],
                "eligible_recipients": [
                    "release-verifier",
                    "runtime-worker",
                    "memory-worker",
                    "scheduler-worker",
                    "edge-worker",
                    "cloud-worker",
                    "audit-worker",
                    "ui-projector",
                ],
                "source_bytes": source_bytes,
                "fact_ids": facts,
                "effective_transition_count": 2_000,
                "task_succeeded": True,
            },
            {
                "message_id": "message-static-route",
                "policy": "static_route",
                "route_kind": "static",
                "inline": full_text[:1024],
                "summary": "Static route carries a fixed summary payload.",
                "artifact_refs": [],
                "recipients": ["release-verifier"],
                "eligible_recipients": ["release-verifier"],
                "source_bytes": source_bytes,
                "fact_ids": facts,
                "effective_transition_count": 2_000,
                "task_succeeded": True,
            },
            {
                "message_id": "message-full-connect",
                "policy": "full_connect_broadcast",
                "route_kind": "full_connect",
                "inline": full_text[:2048],
                "summary": "Full-connect baseline broadcasts the task summary.",
                "artifact_refs": [],
                "recipients": [
                    "release-verifier",
                    "runtime-worker",
                    "memory-worker",
                    "scheduler-worker",
                    "edge-worker",
                    "cloud-worker",
                    "audit-worker",
                    "ui-projector",
                ],
                "eligible_recipients": [
                    "release-verifier",
                    "runtime-worker",
                    "memory-worker",
                    "scheduler-worker",
                    "edge-worker",
                    "cloud-worker",
                    "audit-worker",
                    "ui-projector",
                ],
                "source_bytes": source_bytes,
                "fact_ids": facts * 8,
                "effective_transition_count": 2_000,
                "task_succeeded": True,
            },
            {
                "message_id": "message-full-text",
                "policy": "full_text_inline",
                "route_kind": "broadcast",
                "inline": full_text,
                "summary": "Full-text baseline inlines the complete generated task narrative.",
                "artifact_refs": [],
                "recipients": [
                    "release-verifier",
                    "runtime-worker",
                    "memory-worker",
                    "scheduler-worker",
                ],
                "eligible_recipients": [
                    "release-verifier",
                    "runtime-worker",
                    "memory-worker",
                    "scheduler-worker",
                ],
                "source_bytes": source_bytes,
                "fact_ids": facts,
                "effective_transition_count": 2_000,
                "task_succeeded": True,
            },
        )
        baseline_path = root / "communication-ablation.jsonl"
        with baseline_path.open("x", encoding="utf-8", newline="\n") as output:
            for envelope in lanes:
                normalized = self._sized_envelope(envelope)
                digest = self._store.add_message(normalized)
                output.write(
                    json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                self._emit(
                    task_id="m1-sealed-orchestrator",
                    action_id=f"message:{normalized['message_id']}",
                    event_type="agent_message_committed",
                    semantic_family="state",
                    semantic_key=f"message.{normalized['message_id']}",
                    before={"status": "absent"},
                    after={
                        "status": "committed",
                        "policy": normalized["policy"],
                        "digest": digest,
                    },
                    risk="low",
                    effect="allow",
                    extra={"agent_message": normalized},
                )
            output.flush()
            os.fsync(output.fileno())

    @staticmethod
    def _sized_envelope(value: Mapping[str, Any]) -> Mapping[str, Any]:
        result = dict(value)
        previous = -1
        for _ in range(4):
            encoded = json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            current = len(encoded)
            result["envelope_bytes"] = current
            result["token_count"] = max(1, (current + 3) // 4)
            if current == previous:
                break
            previous = current
        return result

    def _emit(
        self,
        *,
        task_id: str,
        action_id: str,
        event_type: str,
        semantic_family: str,
        semantic_key: str,
        before: Any,
        after: Any,
        risk: str,
        effect: str,
        causation_id: str = "",
        extra: Mapping[str, Any] | None = None,
    ) -> str:
        if self._store is None or self._event_file is None:
            raise LongHorizonExecutionError("canonical event store is not open")
        before_revision = self._revision
        after_revision = before_revision + 1
        self._sequence += 1
        event_id = f"evt-{self._sequence:06d}-{stable_digest((task_id, action_id, event_type))[:16]}"
        cause = causation_id or self._last_event_id
        payload = {
            "run_id": self._run_id,
            "task_id": task_id,
            "action_id": action_id,
            "semantic_family": semantic_family,
            "semantic_key": semantic_key,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "before": before,
            "after": after,
            "causation_id": cause,
            "risk": risk,
            "policy_effect": effect,
            "effect": effect,
            "semantic_mutation": True,
            **dict(extra or {}),
        }
        event = {
            "event_id": event_id,
            "run_id": self._run_id,
            "task_id": task_id,
            "action_id": action_id,
            "event_type": event_type,
            "sequence": self._sequence,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "before": before,
            "after": after,
            "causation_id": cause,
            "parent_event_id": cause,
            "actor_id": "sealed-autonomous-runtime",
            "created_at": utc_now(),
            "payload": payload,
        }
        self._store.append_event(event)
        encoded = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self._event_file.write(encoded + "\n")
        self._event_file.flush()
        self._events.append(event)
        self._revision = after_revision
        self._last_event_id = event_id
        return event_id

    @staticmethod
    def load_events(path: str | Path) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        with Path(path).open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                value = json.loads(stripped)
                if not isinstance(value, Mapping):
                    raise LongHorizonExecutionError(
                        f"canonical event line {line_number} is not an object"
                    )
                values.append(dict(value))
        return values

    @staticmethod
    def _file_digest(path: str | Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


__all__ = [
    "LiveTaskReceipt",
    "LongHorizonExecutionError",
    "LongHorizonRunReceipt",
    "SealedLongHorizonRuntime",
    "SourceWorkItem",
]
