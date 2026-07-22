from __future__ import annotations

import ast
import copy
import itertools
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_orchestration.graph_custody import (
    GraphConflictStrategy,
    GraphDeltaBuilder,
    GraphEdge,
    GraphNode,
    GraphStateCustody,
    GraphStateStore,
)

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now


_FORBIDDEN_IMPORT_PREFIXES = (
    "langgraph.graph",
    "langgraph.pregel",
    "langgraph.channels",
    "langgraph.prebuilt",
    "langgraph_sdk",
)
_FORBIDDEN_SYMBOLS = {
    "StateGraph",
    "Pregel",
    "ToolNode",
    "create_react_agent",
    "MessageGraph",
    "CompiledStateGraph",
    "RemoteGraph",
}
_FORBIDDEN_TEXT_PATTERNS = {
    "stategraph": re.compile(r"\bStateGraph\s*\("),
    "pregel": re.compile(r"\bPregel\s*\("),
    "toolnode": re.compile(r"\bToolNode\s*\("),
    "create_react_agent": re.compile(r"\bcreate_react_agent\s*\("),
    "remote_graph": re.compile(r"\bRemoteGraph\s*\("),
}


@dataclass(frozen=True, slots=True)
class BoundaryProbeReceipt:
    probe_id: str
    ok: bool
    started_at: str
    completed_at: str
    assertions: Mapping[str, bool]
    evidence: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "ok": self.ok,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "assertions": dict(self.assertions),
            "evidence": dict(self.evidence),
            "error": self.error,
        }


class ForbiddenLangGraphScanner:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()

    def scan(self, roots: Sequence[str] = ("apps", "packages")) -> tuple[list[Finding], dict[str, Any]]:
        findings: list[Finding] = []
        scanned = 0
        imports: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        for path in self._files(roots):
            scanned += 1
            if path.suffix in {".py", ".pyi"}:
                py_imports, py_symbols = self._python(path)
                imports.extend(py_imports)
                symbols.extend(py_symbols)
            else:
                script_imports, script_symbols = self._script(path)
                imports.extend(script_imports)
                symbols.extend(script_symbols)
        for item in imports:
            module = str(item["module"])
            if module == "langgraph" or module.startswith(_FORBIDDEN_IMPORT_PREFIXES):
                findings.append(
                    Finding(
                        code="langgraph.forbidden_import",
                        severity=Severity.BLOCKER,
                        summary="Default production source imports a forbidden LangGraph runtime surface.",
                        detail=module,
                        location=f"{item['path']}:{item['line']}",
                    )
                )
        for item in symbols:
            findings.append(
                Finding(
                    code="langgraph.forbidden_symbol",
                    severity=Severity.BLOCKER,
                    summary="Default production source constructs a forbidden graph/prebuilt runtime.",
                    detail=str(item["symbol"]),
                    location=f"{item['path']}:{item['line']}",
                )
            )
        return findings, {
            "scanned_file_count": scanned,
            "import_reference_count": len(imports),
            "forbidden_symbol_reference_count": len(symbols),
            "imports": imports,
            "symbols": symbols,
        }

    def _files(self, roots: Sequence[str]) -> Iterable[Path]:
        ignored = {"node_modules", "dist", "build", "target", "__pycache__", ".tmp", "vendor", "vendor-runtimes"}
        for raw in roots:
            root = (self.root / raw).resolve()
            if not root.exists() or not root.is_relative_to(self.root):
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix not in {".py", ".pyi", ".ts", ".tsx", ".js", ".jsx"}:
                    continue
                relative = path.relative_to(self.root)
                if any(part in ignored for part in relative.parts):
                    continue
                yield path

    def _python(self, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (OSError, UnicodeDecodeError, SyntaxError):
            return [], []
        imports: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    aliases[alias.asname or alias.name.split(".")[0]] = alias.name
                    imports.append(self._reference(path, node.lineno, alias.name, "module"))
            elif isinstance(node, ast.ImportFrom):
                module = ("." * node.level) + (node.module or "")
                imports.append(self._reference(path, node.lineno, module, "module"))
                for alias in node.names:
                    aliases[alias.asname or alias.name] = f"{module}.{alias.name}".strip(".")
            elif isinstance(node, ast.Call):
                called = self._python_call_name(node.func, aliases)
                leaf = called.rsplit(".", 1)[-1]
                if leaf in _FORBIDDEN_SYMBOLS:
                    symbols.append(self._reference(path, node.lineno, called, "symbol"))
        return imports, symbols

    def _script(self, path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return [], []
        imports: list[dict[str, Any]] = []
        symbols: list[dict[str, Any]] = []
        import_pattern = re.compile(
            r"(?:from\s+|import\s*(?:\(|)\s*|require\s*\(\s*)['\"]([^'\"]+)['\"]"
        )
        for match in import_pattern.finditer(text):
            imports.append(self._reference(path, text.count("\n", 0, match.start()) + 1, match.group(1), "module"))
        for name, pattern in _FORBIDDEN_TEXT_PATTERNS.items():
            for match in pattern.finditer(text):
                symbols.append(self._reference(path, text.count("\n", 0, match.start()) + 1, name, "symbol"))
        return imports, symbols

    def _reference(self, path: Path, line: int, value: str, key: str) -> dict[str, Any]:
        return {"path": path.relative_to(self.root).as_posix(), "line": line, key: value}

    @staticmethod
    def _python_call_name(node: ast.AST, aliases: Mapping[str, str]) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            parent = ForbiddenLangGraphScanner._python_call_name(node.value, aliases)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""


class GraphBoundaryProbeSuite:
    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.resolve()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def run(self) -> tuple[BoundaryProbeReceipt, ...]:
        probes: tuple[tuple[str, Callable[[], Mapping[str, Any]]], ...] = (
            ("runtime-topology-mutation", self._runtime_topology_mutation),
            ("nested-alias-isolation", self._nested_alias_isolation),
            ("deterministic-permutation", self._deterministic_permutation),
            ("explicit-write-conflict", self._explicit_write_conflict),
            ("pending-committed-separation", self._pending_committed_separation),
            ("side-effect-idempotency-fence", self._side_effect_idempotency_fence),
        )
        receipts: list[BoundaryProbeReceipt] = []
        for probe_id, probe in probes:
            started = utc_now()
            try:
                evidence = dict(probe())
                assertions = {
                    key: bool(value)
                    for key, value in dict(evidence.pop("assertions", {})).items()
                }
                ok = bool(assertions) and all(assertions.values())
                receipts.append(
                    BoundaryProbeReceipt(
                        probe_id=probe_id,
                        ok=ok,
                        started_at=started,
                        completed_at=utc_now(),
                        assertions=assertions,
                        evidence=evidence,
                        error="" if ok else "one or more boundary assertions failed",
                    )
                )
            except Exception as error:
                receipts.append(
                    BoundaryProbeReceipt(
                        probe_id=probe_id,
                        ok=False,
                        started_at=started,
                        completed_at=utc_now(),
                        assertions={},
                        error=f"{type(error).__name__}: {error}",
                    )
                )
        return tuple(receipts)

    def _custody(self, name: str) -> GraphStateCustody:
        path = self.artifact_root / f"{name}.sqlite3"
        if path.exists():
            path.unlink()
        store = GraphStateStore(path)
        store.initialize()
        return GraphStateCustody(store)

    @staticmethod
    def _node(
        node_id: str,
        *,
        role: str = "worker",
        capabilities: Sequence[str] = ("agent_task",),
        metadata: Mapping[str, Any] | None = None,
    ) -> GraphNode:
        return GraphNode(
            node_id=node_id,
            role=role,
            capabilities=tuple(capabilities),
            metadata=dict(metadata or {}),
        )

    def _runtime_topology_mutation(self) -> Mapping[str, Any]:
        custody = self._custody("runtime-topology")
        graph_id = "graph-runtime-topology"
        initial = custody.create(graph_id_value=graph_id, run_id="run-runtime-topology")
        compiled_set = {"planner", "executor"}
        dynamic_id = "worker-discovered-after-start"
        builder = GraphDeltaBuilder(
            initial,
            branch_id="dynamic-discovery",
            actor_id="boundary-probe",
            causation_id="environment:new-capability",
            idempotency_key="dynamic-discovery-1",
        )
        builder.add_node(self._node(dynamic_id, role="specialist", capabilities=("new_environment_capability",)))
        committed = custody.commit(builder.build())
        current = custody.current(graph_id)
        return {
            "assertions": {
                "created_after_runtime_start": dynamic_id in current.node_map,
                "outside_precompiled_set": dynamic_id not in compiled_set,
                "canonical_revision_advanced": current.revision == initial.revision + 1,
                "commit_visible": committed.receipt.committed,
                "causation_preserved": committed.receipt.metadata.get("causation_id") == "environment:new-capability",
            },
            "compiled_set": sorted(compiled_set),
            "dynamic_node": current.node_map[dynamic_id].to_dict(),
            "receipt": committed.receipt.to_dict(),
        }

    def _nested_alias_isolation(self) -> Mapping[str, Any]:
        custody = self._custody("alias-isolation")
        graph_id = "graph-alias-isolation"
        source_metadata = {"nested": {"tags": ["one"], "policy": {"mode": "sealed"}}}
        initial = custody.create(graph_id_value=graph_id, run_id="run-alias-isolation")
        node = self._node("alias-node", metadata=source_metadata)
        builder = GraphDeltaBuilder(
            initial,
            branch_id="alias",
            actor_id="boundary-probe",
            causation_id="alias-cause",
            idempotency_key="alias-1",
        ).add_node(node)
        committed = custody.commit(builder.build())
        source_metadata["nested"]["tags"].append("source-mutated")
        source_metadata["nested"]["policy"]["mode"] = "bypass"
        exposed = committed.snapshot.node_map["alias-node"].metadata
        exposed_copy = copy.deepcopy(exposed)
        exposed_copy["nested"]["tags"].append("copy-mutated")
        reloaded = custody.current(graph_id).node_map["alias-node"].metadata
        return {
            "assertions": {
                "source_alias_detached": reloaded["nested"]["tags"] == ["one"],
                "nested_mapping_detached": reloaded["nested"]["policy"]["mode"] == "sealed",
                "consumer_copy_detached": "copy-mutated" not in reloaded["nested"]["tags"],
                "snapshot_signature_stable": custody.current(graph_id).signature == committed.snapshot.signature,
            },
            "source_metadata": source_metadata,
            "canonical_metadata": reloaded,
        }

    def _deterministic_permutation(self) -> Mapping[str, Any]:
        outcomes: list[dict[str, Any]] = []
        mutations = (
            ("branch-a", self._node("node-a", role="researcher", capabilities=("search",))),
            ("branch-b", self._node("node-b", role="verifier", capabilities=("verify",))),
            ("branch-c", self._node("node-c", role="worker", capabilities=("execute",))),
        )
        for order_index, order in enumerate(itertools.permutations(mutations), start=1):
            custody = self._custody(f"deterministic-{order_index}")
            graph_id = f"graph-deterministic-{order_index}"
            base = custody.create(graph_id_value=graph_id, run_id=f"run-deterministic-{order_index}")
            deltas = []
            for branch, node in mutations:
                deltas.append(
                    GraphDeltaBuilder(
                        base,
                        branch_id=branch,
                        actor_id="boundary-probe",
                        causation_id=f"cause:{branch}",
                        idempotency_key=f"idempotency:{branch}",
                    ).add_node(node).build()
                )
            by_branch = {delta.branch_id: delta for delta in deltas}
            receipts = []
            for branch, _node in order:
                receipts.append(custody.commit(by_branch[branch], strategy=GraphConflictStrategy.REBASE).receipt)
            current = custody.current(graph_id)
            outcomes.append(
                {
                    "order": [branch for branch, _node in order],
                    "semantic": self._semantic_graph(current),
                    "statuses": [receipt.status.value for receipt in receipts],
                }
            )
        first = outcomes[0]["semantic"]
        return {
            "assertions": {
                "all_permutations_equal": all(item["semantic"] == first for item in outcomes),
                "all_nodes_committed": len(first["nodes"]) == 3,
                "no_last_writer_loss": all(len(item["semantic"]["nodes"]) == 3 for item in outcomes),
                "stale_disjoint_rebased": all(
                    all(status in {"committed", "rebased", "replayed"} for status in item["statuses"])
                    for item in outcomes
                ),
            },
            "permutation_count": len(outcomes),
            "outcomes": outcomes,
        }

    def _explicit_write_conflict(self) -> Mapping[str, Any]:
        custody = self._custody("write-conflict")
        graph_id = "graph-write-conflict"
        empty = custody.create(graph_id_value=graph_id, run_id="run-write-conflict")
        seed = GraphDeltaBuilder(
            empty,
            branch_id="seed",
            actor_id="boundary-probe",
            causation_id="seed",
            idempotency_key="seed",
        ).add_node(self._node("contended", role="worker"))
        custody.commit(seed.build())
        base = custody.current(graph_id)
        left = GraphDeltaBuilder(
            base,
            branch_id="left",
            actor_id="left-worker",
            causation_id="left-cause",
            idempotency_key="left",
        ).set_role("contended", "researcher", expected_revision=1).build()
        right = GraphDeltaBuilder(
            base,
            branch_id="right",
            actor_id="right-worker",
            causation_id="right-cause",
            idempotency_key="right",
        ).set_role("contended", "verifier", expected_revision=1).build()
        first = custody.commit(left)
        conflict = custody.commit(right, strategy=GraphConflictStrategy.REPLAN)
        current = custody.current(graph_id)
        return {
            "assertions": {
                "first_committed": first.receipt.committed,
                "conflict_not_committed": not conflict.receipt.committed,
                "replan_explicit": conflict.receipt.status.value == "replan_required",
                "conflict_described": bool(conflict.receipt.conflicts),
                "no_last_writer_wins": current.node_map["contended"].role == "researcher",
            },
            "first": first.receipt.to_dict(),
            "conflict": conflict.receipt.to_dict(),
            "current": current.to_dict(),
        }

    def _pending_committed_separation(self) -> Mapping[str, Any]:
        custody = self._custody("pending-committed")
        graph_id = "graph-pending-committed"
        base = custody.create(graph_id_value=graph_id, run_id="run-pending-committed")
        delta = GraphDeltaBuilder(
            base,
            branch_id="pending",
            actor_id="boundary-probe",
            causation_id="pending-cause",
            idempotency_key="pending",
        ).add_node(self._node("pending-node")).build()
        stored = custody.store.save_delta(delta)
        before_commit = custody.current(graph_id)
        committed = custody.commit(stored)
        after_commit = custody.current(graph_id)
        journal = custody.store.journal(graph_id)
        return {
            "assertions": {
                "pending_not_visible": "pending-node" not in before_commit.node_map,
                "head_not_advanced_by_pending": before_commit.revision == base.revision,
                "committed_visible": "pending-node" in after_commit.node_map,
                "head_advanced_once": after_commit.revision == base.revision + 1,
                "journal_records_commit": any(item.get("delta_id") == delta.delta_id for item in journal),
            },
            "delta_id": delta.delta_id,
            "before_revision": before_commit.revision,
            "after_revision": after_commit.revision,
            "receipt": committed.receipt.to_dict(),
            "journal": journal,
        }

    def _side_effect_idempotency_fence(self) -> Mapping[str, Any]:
        custody = self._custody("idempotency-fence")
        graph_id = "graph-idempotency-fence"
        base = custody.create(graph_id_value=graph_id, run_id="run-idempotency-fence")
        delta = GraphDeltaBuilder(
            base,
            branch_id="effect",
            actor_id="boundary-probe",
            causation_id="external-effect:receipt-1",
            idempotency_key="external-effect:receipt-1",
            metadata={"side_effect_receipt": "receipt-1"},
        ).add_node(self._node("effect-node", metadata={"side_effect_receipt": "receipt-1"})).build()
        first = custody.commit(delta)
        replay = custody.commit(delta)
        current = custody.current(graph_id)
        persisted_receipt = custody.store.commit_for_delta(graph_id, delta.delta_id)
        return {
            "assertions": {
                "first_committed": first.receipt.committed,
                "replay_recognized": replay.receipt.status.value == "replayed",
                "revision_advanced_once": current.revision == 1,
                "single_commit_receipt": persisted_receipt is not None
                and persisted_receipt.commit_id == first.receipt.commit_id,
                "effect_receipt_preserved": current.node_map["effect-node"].metadata.get("side_effect_receipt") == "receipt-1",
            },
            "first": first.receipt.to_dict(),
            "replay": replay.receipt.to_dict(),
            "current_revision": current.revision,
        }

    @staticmethod
    def _semantic_graph(snapshot: Any) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "node_id": node.node_id,
                    "role": node.role,
                    "capabilities": list(node.capabilities),
                    "dependencies": list(node.dependencies),
                    "state": node.state.value,
                    "metadata": dict(node.metadata),
                }
                for node in snapshot.nodes
            ],
            "edges": [
                {
                    "edge_id": edge.edge_id,
                    "source_node_id": edge.source_node_id,
                    "target_node_id": edge.target_node_id,
                    "relation": edge.relation,
                }
                for edge in snapshot.edges
            ],
        }


class CodeWorkerCohesionProbe:
    def __init__(self, project_root: Path) -> None:
        self.root = project_root.resolve()

    def run(self) -> BoundaryProbeReceipt:
        started = utc_now()
        runtime_root = self.root / "packages" / "runtime" / "claude-runtime" / "src"
        files = [path for path in runtime_root.rglob("*.ts") if path.is_file()]
        texts: dict[str, str] = {}
        for path in files:
            try:
                texts[path.relative_to(self.root).as_posix()] = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
        combined = "\n".join(texts.values())
        forbidden = {
            symbol: [path for path, text in texts.items() if re.search(rf"\b{re.escape(symbol)}\b", text)]
            for symbol in _FORBIDDEN_SYMBOLS
        }
        forbidden = {key: value for key, value in forbidden.items() if value}
        loop_markers = {
            "reason_or_query": bool(re.search(r"\b(query|reason|model)\b", combined, re.IGNORECASE)),
            "tool": bool(re.search(r"\btool(?:Use|Call|Execution|Result)?\b", combined)),
            "observe_or_result": bool(re.search(r"\b(observe|observation|toolResult|tool_result)\b", combined)),
            "revise_or_continue": bool(re.search(r"\b(revise|continue|nextTurn|queryLoop)\b", combined)),
            "permission": "permission" in combined.lower(),
            "compact": "compact" in combined.lower(),
            "session": "session" in combined.lower(),
        }
        assertions = {
            "runtime_source_present": bool(texts),
            "no_graph_micro_nodes": not forbidden,
            "reason_tool_observe_revise_markers": all(loop_markers.values()),
        }
        return BoundaryProbeReceipt(
            probe_id="codeworker-loop-cohesion",
            ok=all(assertions.values()),
            started_at=started,
            completed_at=utc_now(),
            assertions=assertions,
            evidence={
                "runtime_file_count": len(texts),
                "loop_markers": loop_markers,
                "forbidden_symbols": forbidden,
            },
            error="" if all(assertions.values()) else "CodeWorker cohesion assertions failed",
        )


class LangGraphBoundaryGate:
    def __init__(self, project_root: str | Path, *, artifact_root: str | Path | None = None) -> None:
        self.root = Path(project_root).resolve()
        self.artifact_root = Path(artifact_root).resolve() if artifact_root else self.root / ".tmp" / "m1-hardening" / "langgraph"

    def evaluate(self, *, run_dynamic_probes: bool = True) -> GateResult:
        result = GateResult(
            gate_id="langgraph-boundary",
            status=GateStatus.NOT_RUN,
            summary="Negative default-path dependency and Zyra graph custody adversarial probes.",
        )
        findings, scan_metrics = ForbiddenLangGraphScanner(self.root).scan()
        result.findings.extend(findings)
        receipts: list[BoundaryProbeReceipt] = []
        if run_dynamic_probes:
            receipts.extend(GraphBoundaryProbeSuite(self.artifact_root).run())
            receipts.append(CodeWorkerCohesionProbe(self.root).run())
            for receipt in receipts:
                if not receipt.ok:
                    result.add(
                        Finding(
                            code=f"langgraph.probe_failed.{receipt.probe_id}",
                            severity=Severity.BLOCKER,
                            summary="Required LangGraph boundary/adversarial probe failed.",
                            detail=receipt.error,
                            location=receipt.probe_id,
                            metadata={"assertions": dict(receipt.assertions)},
                        )
                    )
                result.evidence.append(
                    EvidencePointer(
                        kind="langgraph_boundary_probe",
                        location=receipt.probe_id,
                        summary="passed" if receipt.ok else "failed",
                        metadata={"assertions": dict(receipt.assertions)},
                    )
                )
        else:
            result.limitations.append("Dynamic topology/alias/determinism/conflict/pending-write probes were not run.")
        result.metrics.update(
            {
                "static_scan": scan_metrics,
                "dynamic_probe_count": len(receipts),
                "dynamic_probe_passed": sum(1 for receipt in receipts if receipt.ok),
                "probes": [receipt.to_dict() for receipt in receipts],
            }
        )
        return result.finish(default_partial=not run_dynamic_probes)
