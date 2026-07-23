from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .integration_contracts import stable_digest


class AlgorithmMaterialError(RuntimeError):
    """Live events could not produce reviewable algorithm and causal material."""


@dataclass(frozen=True, slots=True)
class AlgorithmMaterialReceipt:
    run_id: str
    source_event_count: int
    selected_node_count: int
    selected_edge_count: int
    graph_path: str
    mermaid_path: str
    analysis_path: str
    manifest_path: str
    content_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.algorithm-material-receipt/v1",
            "run_id": self.run_id,
            "source_event_count": self.source_event_count,
            "selected_node_count": self.selected_node_count,
            "selected_edge_count": self.selected_edge_count,
            "graph_path": self.graph_path,
            "mermaid_path": self.mermaid_path,
            "analysis_path": self.analysis_path,
            "manifest_path": self.manifest_path,
            "content_digest": self.content_digest,
        }


class AlgorithmMaterialRuntime:
    """Derive bounded causal visualization and complexity material from live events."""

    _LANDMARK_TYPES = {
        "task_created",
        "task_started",
        "task_verification_completed",
        "task_constraint_verified",
        "requirement_changed",
        "worker_fault_detected",
        "node_failure_detected",
        "recovery_retry_completed",
        "recovery_reroute_completed",
        "recovery_replan_completed",
        "topology_node_added",
        "route_placement_committed",
        "worker_lease_acquired",
        "permission_denied",
        "compact_checkpoint_committed",
        "restore_checkpoint_verified",
        "memory_retrieval_committed",
        "agent_message_committed",
    }

    def build(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        output_root: str | Path,
    ) -> AlgorithmMaterialReceipt:
        values = tuple(
            sorted(
                (dict(item) for item in events if str(item.get("run_id") or "") == run_id),
                key=lambda item: int(item.get("sequence") or 0),
            )
        )
        if len(values) < 2_000:
            raise AlgorithmMaterialError(
                "algorithm material requires at least 2,000 canonical live transitions"
            )
        selected = self._landmarks(values)
        event_ids = {str(item.get("event_id") or "") for item in selected}
        edges = self._edges(selected, event_ids)
        self._validate(selected, edges)
        root = Path(output_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        graph_path = root / "causal-trace.json"
        mermaid_path = root / "causal-trace.mmd"
        analysis_path = root / "algorithm-pseudocode-and-complexity.md"
        manifest_path = root / "algorithm-material-manifest.json"
        graph = {
            "schema": "zyra.causal-trace-material/v1",
            "run_id": run_id,
            "source_event_count": len(values),
            "selection_policy": {
                "kind": "semantic_landmarks",
                "included_event_types": sorted(self._LANDMARK_TYPES),
                "routine_tool_artifact_pairs_are_summarized": True,
            },
            "nodes": [self._node(item) for item in selected],
            "edges": edges,
            "source_stream_digest": stable_digest(values),
        }
        graph_path.write_text(
            json.dumps(graph, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        mermaid_path.write_text(
            self._mermaid(selected, edges),
            encoding="utf-8",
            newline="\n",
        )
        analysis_path.write_text(
            self._analysis(
                run_id=run_id,
                source_event_count=len(values),
                node_count=len(selected),
                edge_count=len(edges),
            ),
            encoding="utf-8",
            newline="\n",
        )
        manifest = {
            "schema": "zyra.algorithm-material-manifest/v1",
            "run_id": run_id,
            "source_event_count": len(values),
            "selected_node_count": len(selected),
            "selected_edge_count": len(edges),
            "artifacts": {
                "causal_graph": self._artifact(graph_path, "application/json"),
                "mermaid": self._artifact(mermaid_path, "text/vnd.mermaid"),
                "algorithm_analysis": self._artifact(analysis_path, "text/markdown"),
            },
        }
        content_digest = stable_digest(manifest)
        manifest_path.write_text(
            json.dumps(
                {**manifest, "content_digest": content_digest},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        return AlgorithmMaterialReceipt(
            run_id=run_id,
            source_event_count=len(values),
            selected_node_count=len(selected),
            selected_edge_count=len(edges),
            graph_path=str(graph_path),
            mermaid_path=str(mermaid_path),
            analysis_path=str(analysis_path),
            manifest_path=str(manifest_path),
            content_digest=content_digest,
        )

    def _landmarks(
        self,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        selected = [
            item
            for item in events
            if str(item.get("event_type") or "") in self._LANDMARK_TYPES
        ]
        by_task: dict[str, list[Mapping[str, Any]]] = {}
        for item in events:
            by_task.setdefault(str(item.get("task_id") or ""), []).append(item)
        selected_ids = {str(item.get("event_id") or "") for item in selected}
        for task_events in by_task.values():
            for item in (task_events[0], task_events[-1]):
                event_id = str(item.get("event_id") or "")
                if event_id and event_id not in selected_ids:
                    selected.append(item)
                    selected_ids.add(event_id)
        return tuple(sorted(selected, key=lambda item: int(item.get("sequence") or 0)))

    @staticmethod
    def _edges(
        selected: Sequence[Mapping[str, Any]],
        selected_ids: set[str],
    ) -> tuple[dict[str, Any], ...]:
        edges: list[dict[str, Any]] = []
        previous_by_task: dict[str, str] = {}
        seen: set[tuple[str, str, str]] = set()
        for item in selected:
            target = str(item.get("event_id") or "")
            task_id = str(item.get("task_id") or "")
            cause = str(item.get("causation_id") or "")
            if cause in selected_ids and cause != target:
                edge = (cause, target, "causes")
            else:
                previous = previous_by_task.get(task_id, "")
                edge = (previous, target, "compressed_sequence")
            if edge[0] and edge not in seen:
                edges.append(
                    {
                        "source_event_id": edge[0],
                        "target_event_id": edge[1],
                        "relation": edge[2],
                    }
                )
                seen.add(edge)
            previous_by_task[task_id] = target
        return tuple(edges)

    @classmethod
    def _validate(
        cls,
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> None:
        event_types = {str(item.get("event_type") or "") for item in nodes}
        required = {
            "requirement_changed",
            "topology_node_added",
            "permission_denied",
            "compact_checkpoint_committed",
            "restore_checkpoint_verified",
            "memory_retrieval_committed",
            "task_constraint_verified",
        }
        missing = sorted(required - event_types)
        if missing:
            raise AlgorithmMaterialError(
                "causal material lacks required semantic landmarks: " + ", ".join(missing)
            )
        if not {"worker_fault_detected", "node_failure_detected"} & event_types:
            raise AlgorithmMaterialError("causal material lacks a real failure landmark")
        if not {
            "recovery_retry_completed",
            "recovery_reroute_completed",
            "recovery_replan_completed",
        } <= event_types:
            raise AlgorithmMaterialError("causal material lacks recovery strategy landmarks")
        task_ids = {
            str(item.get("task_id") or "")
            for item in nodes
            if str(item.get("task_id") or "").startswith("m1-live-")
        }
        if len(task_ids) < 2:
            raise AlgorithmMaterialError("causal material does not cover two live task domains")
        if not any(item.get("relation") == "causes" for item in edges):
            raise AlgorithmMaterialError("causal material lacks explicit causation edges")

    @staticmethod
    def _node(event: Mapping[str, Any]) -> Mapping[str, Any]:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        return {
            "event_id": str(event.get("event_id") or ""),
            "sequence": int(event.get("sequence") or 0),
            "task_id": str(event.get("task_id") or ""),
            "action_id": str(event.get("action_id") or ""),
            "event_type": str(event.get("event_type") or ""),
            "semantic_family": str(payload.get("semantic_family") or ""),
            "causation_id": str(event.get("causation_id") or ""),
            "before_revision": int(event.get("before_revision") or 0),
            "after_revision": int(event.get("after_revision") or 0),
            "payload_digest": stable_digest(payload),
        }

    @staticmethod
    def _mermaid(
        nodes: Sequence[Mapping[str, Any]],
        edges: Sequence[Mapping[str, Any]],
    ) -> str:
        aliases = {
            str(item.get("event_id") or ""): f"n{index:03d}"
            for index, item in enumerate(nodes, start=1)
        }
        lines = [
            "flowchart LR",
            "  %% Generated from canonical live events; routine pairs are summarized.",
        ]
        for item in nodes:
            event_id = str(item.get("event_id") or "")
            label = (
                f"{int(item.get('sequence') or 0)} "
                f"{str(item.get('event_type') or '')}\\n"
                f"{str(item.get('task_id') or '')}"
            )
            label = re.sub(r"[^A-Za-z0-9_.:\\\\ -]", "_", label)
            lines.append(f'  {aliases[event_id]}["{label}"]')
        for edge in edges:
            source = aliases[str(edge["source_event_id"])]
            target = aliases[str(edge["target_event_id"])]
            relation = str(edge["relation"])
            connector = "-->" if relation == "causes" else "-.->"
            lines.append(f"  {source} {connector}|{relation}| {target}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _analysis(
        *,
        run_id: str,
        source_event_count: int,
        node_count: int,
        edge_count: int,
    ) -> str:
        return f"""# Zyra M1 core algorithms: pseudocode and complexity

Run: `{run_id}`. The material is derived from `{source_event_count}` canonical
transitions; the bounded causal view contains `{node_count}` semantic landmarks
and `{edge_count}` edges.

## 1. Immutable canonical transition commit

Implementation: `long_horizon_runtime.py::_emit`,
`evidence_graph.py::CausalEvidenceGraph`, and the canonical SQLite event owner.

```text
COMMIT_TRANSITION(event):
  require event.after_revision = event.before_revision + 1
  require event.causation_id references an earlier committed event
  payload_digest = SHA256(canonical_json(event.payload))
  atomically append (sequence, identities, revisions, payload_digest)
  advance committed revision only after append succeeds
```

For `T` transitions, append work is `O(T)` total and `O(1)` per indexed append;
the durable event stream occupies `O(T)` space. Causal validation and graph
construction are `O(V + E)` for `V` event nodes and `E` causal edges.

## 2. Capability- and location-fenced worker placement

Implementation: `worker_pool/scheduler_bridge.py`,
`worker_pool/admission.py`, and `worker_pool/leases.py`.

```text
PLACE(requirement, workers):
  candidates = workers matching capability, tool, location and resource fences
  reject stale heartbeat, unhealthy route, exhausted session/run/worker limits
  rank accepted candidates deterministically
  atomically acquire lease with generation, epoch and fence token
  return selected route plus immutable admission receipt
```

With `W` observed workers and indexed active-lease counts, filtering/ranking is
`O(W log W)` worst case (`O(W)` when selection uses a single-pass minimum);
candidate evidence is `O(W)`, while lease commit is `O(1)` database work.

## 3. Fault fencing and deterministic recovery

Implementation: `fault_runtime`, `recovery_runtime`, worker control, and the
long-horizon transaction/process-loss exercises.

```text
RECOVER(fault):
  persist fault observation and capture owner checkpoint
  fence old attempt before any retry or reroute
  choose retry, reroute, replan or exact restore from typed evidence
  execute one idempotent recovery action
  verify artifact/state effect, then commit continuation and routing feedback
```

For `R` bounded recovery candidates, deterministic choice is `O(R log R)` with
ranking and `O(R)` space. Fence/receipt verification is `O(1)` per attempt.
Replay over `T` recovery events is `O(T)`.

## 4. Bounded causal visualization

```text
BUILD_VIEW(events):
  scan events once and retain semantic landmarks plus each task boundary
  connect an explicit retained cause when available
  otherwise connect the preceding retained event in the same task
  reject views missing requirement, topology, fault, recovery, permission,
         compact/restore, memory, two-task and terminal-verification landmarks
```

Selection is `O(T)`. Sorting is `O(V log V)` (the input stream is normally
already ordered), edge construction is `O(V)`, and output space is `O(V + E)`.
The bounded view does not replace the full canonical stream; its manifest binds
all generated artifacts back to the source stream digest.
"""

    @staticmethod
    def _artifact(path: Path, media_type: str) -> Mapping[str, Any]:
        return {
            "path": str(path),
            "media_type": media_type,
            "size_bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }


__all__ = [
    "AlgorithmMaterialError",
    "AlgorithmMaterialReceipt",
    "AlgorithmMaterialRuntime",
]
