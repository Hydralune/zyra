from __future__ import annotations

import ast
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .canonical import digest, file_digest, now, safe_relative_path
from .errors import blocker, fail, require_no_blockers


ALGORITHM_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "algorithm_id": "dynamic-sparse-topology-routing",
        "title": "Dynamic sparse heterogeneous topology routing",
        "implementation_candidates": (
            "packages/symbolic/zyra_symbolic/topology.py",
            "packages/orchestration/zyra_orchestration/dynamic_topology.py",
            "packages/orchestration/zyra_orchestration/graph_custody.py",
        ),
        "test_terms": ("topology", "dynamic_graph"),
        "pseudocode": (
            "profile task requirements and current topology revision",
            "filter workers by capability, privacy, lease, and policy",
            "score capability, latency, cost, load, diversity, and failure risk",
            "select adaptive top-k workers above the admission threshold",
            "emit add/remove/replace node and edge mutations",
            "commit mutations with a graph-revision compare-and-swap",
        ),
        "time": "O(W log K + E_delta)",
        "space": "O(W + K + E_delta)",
        "communication": "O(K * M_ref), K << W",
    },
    {
        "algorithm_id": "low-entropy-structured-communication",
        "title": "Low-entropy structured evidence communication",
        "implementation_candidates": (
            "packages/runtime/zyra_runtime",
            "packages/orchestration/zyra_orchestration",
        ),
        "test_terms": ("low_entropy", "message_bus", "event"),
        "pseudocode": (
            "classify payload into conclusion, evidence, state delta, and request",
            "store oversized tool or artifact content once in the artifact store",
            "replace shared payload content with checksum-bound references",
            "route only to the adaptive top-k recipients",
            "enforce per-message and per-recipient budgets before publication",
            "record token, duplicate-fact, broadcast, and offload metrics",
        ),
        "time": "O(B + R log K)",
        "space": "O(U + K)",
        "communication": "O(K * S), instead of O(W * H)",
    },
    {
        "algorithm_id": "distributed-memory-compact-restore",
        "title": "Distributed memory compact, wakeup, and exact restore",
        "implementation_candidates": (
            "packages/memory/zyra_memory",
            "packages/runtime/claude-runtime/src",
        ),
        "test_terms": ("memory", "compact", "restore"),
        "pseudocode": (
            "atomically group tool calls with their corresponding results",
            "pin goal, constraints, unresolved work, and recent observations",
            "flush durable episodic, semantic, and skill candidates",
            "compact middle history into fact, decision, evidence, and debt records",
            "retrieve by lexical, semantic, temporal, and diversity signals",
            "restore the compact boundary and verify goal and evidence fidelity",
        ),
        "time": "O(N + Q log I + K log K)",
        "space": "O(I + K + C)",
        "communication": "O(C + K * R_ref)",
    },
    {
        "algorithm_id": "neuro-symbolic-action-admission",
        "title": "Neuro-symbolic action and completion admission",
        "implementation_candidates": (
            "packages/symbolic/zyra_symbolic",
            "packages/runtime/zyra_runtime/permission",
        ),
        "test_terms": ("symbolic", "permission", "constraint"),
        "pseudocode": (
            "accept an LLM proposal as an untrusted candidate",
            "validate schema, dependency, budget, permission, and data policy",
            "bind a worker lease, route receipt, and idempotency key",
            "execute only after deterministic admission succeeds",
            "append causal state and evidence mutations",
            "verify completion predicates or issue a bounded recovery/replan",
        ),
        "time": "O(C + D + P + A)",
        "space": "O(C + D + A)",
        "communication": "O(P_ref + A_ref)",
    },
    {
        "algorithm_id": "device-edge-cloud-placement",
        "title": "Device-edge-cloud placement and model split",
        "implementation_candidates": (
            "packages/scheduler",
            "packages/orchestration/zyra_orchestration/deployment",
        ),
        "test_terms": ("placement", "edge", "deployment", "provider"),
        "pseudocode": (
            "filter nodes by privacy, capability, model, and credential constraints",
            "reject stale generations, unhealthy nodes, and exhausted leases",
            "score latency, cost, load, locality, trust, and failure history",
            "split classification, extraction, planning, execution, and verification",
            "commit placement before dispatch and bind the dispatch receipt",
            "reroute on node, provider, network, or budget failure",
        ),
        "time": "O(W * (C + M) + W log K)",
        "space": "O(W + T)",
        "communication": "O(T + K * R_dispatch)",
    },
    {
        "algorithm_id": "fault-classification-exact-recovery",
        "title": "Fault classification, side-effect fencing, and exact recovery",
        "implementation_candidates": (
            "packages/orchestration/zyra_orchestration",
            "packages/scheduler",
        ),
        "test_terms": ("fault", "recovery", "checkpoint", "exact_resume"),
        "pseudocode": (
            "classify observed process, tool, network, provider, and state faults",
            "load checkpoint identity, lineage, pending writes, and topology revision",
            "discard uncommitted projection state while preserving committed facts",
            "consult the side-effect idempotency fence before retry",
            "select resume, retry, reroute, rebase, or replan deterministically",
            "commit the recovery plan and verify no duplicated external effect",
        ),
        "time": "O(L + P + W log W)",
        "space": "O(L + P + R)",
        "communication": "O(R_receipt + D_delta)",
    },
)


class AlgorithmMaterialBuilder:
    """Links pseudocode and complexity claims to executable symbols and tests."""

    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)

    def build(self) -> dict[str, Any]:
        algorithms = [self._build_algorithm(spec) for spec in ALGORITHM_CATALOG]
        material = {
            "schema": "zyra.first-stage-algorithm-material/v1",
            "algorithms": algorithms,
            "generated_at": now(),
        }
        verification = self.verify(material)
        material["verification"] = verification
        material["material_digest"] = digest(material)
        return material

    def verify(self, material: Mapping[str, Any]) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        algorithms = material.get("algorithms")
        if not isinstance(algorithms, Sequence):
            raise fail(
                "algorithm-material-list-missing",
                "Algorithm material does not contain an algorithm list.",
                phase="material",
            )
        identities: set[str] = set()
        for value in algorithms:
            if not isinstance(value, Mapping):
                findings.append(
                    blocker(
                        "algorithm-material-entry-invalid",
                        "Algorithm material entry is not a mapping.",
                    )
                )
                continue
            algorithm_id = str(value.get("algorithm_id") or "")
            if algorithm_id in identities:
                findings.append(
                    blocker(
                        "algorithm-material-id-duplicate",
                        "Algorithm material identifier is duplicated.",
                        algorithm_id=algorithm_id,
                    )
                )
            identities.add(algorithm_id)
            if len(value.get("pseudocode") or []) < 4:
                findings.append(
                    blocker(
                        "algorithm-pseudocode-incomplete",
                        "Algorithm pseudocode is too short to explain control flow.",
                        algorithm_id=algorithm_id,
                    )
                )
            complexity = value.get("complexity") or {}
            for key in ("time", "space", "communication"):
                if not str(complexity.get(key) or "").startswith("O("):
                    findings.append(
                        blocker(
                            "algorithm-complexity-missing",
                            "Algorithm complexity claim is missing.",
                            algorithm_id=algorithm_id,
                            dimension=key,
                        )
                    )
            if not value.get("implementation_anchors"):
                findings.append(
                    blocker(
                        "algorithm-implementation-anchor-missing",
                        "Algorithm has no executable implementation anchor.",
                        algorithm_id=algorithm_id,
                    )
                )
            if not value.get("behavior_tests"):
                findings.append(
                    blocker(
                        "algorithm-behavior-test-missing",
                        "Algorithm has no behavior test anchor.",
                        algorithm_id=algorithm_id,
                    )
                )
        require_no_blockers(
            findings,
            code="algorithm-material-invalid",
            message="Algorithm material is not implementation-linked.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.first-stage-algorithm-material-verification/v1",
            "valid": True,
            "algorithm_count": len(algorithms),
            "algorithm_ids": sorted(identities),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _build_algorithm(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        anchors = []
        for relative in spec["implementation_candidates"]:
            path = self.repository_root / relative
            if not path.exists():
                continue
            anchors.extend(self._implementation_anchors(path, limit=12))
        tests = self._tests(spec["test_terms"], limit=12)
        return {
            "algorithm_id": spec["algorithm_id"],
            "title": spec["title"],
            "pseudocode": [
                {"step": index, "operation": operation}
                for index, operation in enumerate(spec["pseudocode"], 1)
            ],
            "complexity": {
                "time": spec["time"],
                "space": spec["space"],
                "communication": spec["communication"],
                "symbols": self._complexity_symbols(
                    spec["time"],
                    spec["space"],
                    spec["communication"],
                ),
            },
            "implementation_anchors": anchors,
            "behavior_tests": tests,
        }

    def _implementation_anchors(
        self,
        path: Path,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        paths = (
            [path]
            if path.is_file()
            else sorted(
                item
                for item in path.rglob("*")
                if item.is_file() and item.suffix in {".py", ".ts", ".tsx", ".rs"}
            )
        )
        anchors = []
        for selected in paths:
            if len(anchors) >= limit:
                break
            symbols = self._symbols(selected)
            if not symbols:
                continue
            anchors.append(
                {
                    "path": selected.relative_to(self.repository_root).as_posix(),
                    "sha256": file_digest(selected),
                    "symbols": symbols[:16],
                    "language": self._language(selected),
                }
            )
        return anchors

    def _tests(self, terms: Sequence[str], *, limit: int) -> list[dict[str, Any]]:
        candidates = []
        for path in (self.repository_root / "tests").rglob("test_*"):
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            lowered = path.name.lower()
            score = sum(1 for term in terms if term.lower() in lowered)
            if score:
                candidates.append((score, path))
        candidates.sort(key=lambda item: (-item[0], item[1].as_posix()))
        return [
            {
                "path": path.relative_to(self.repository_root).as_posix(),
                "sha256": file_digest(path),
                "language": self._language(path),
            }
            for _score, path in candidates[:limit]
        ]

    @staticmethod
    def _symbols(path: Path) -> list[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        if path.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError:
                return []
            return [
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and not node.name.startswith("_")
            ]
        pattern = re.compile(
            r"(?:export\s+)?(?:async\s+)?(?:class|function|const|interface|enum|struct|fn)"
            r"\s+([A-Za-z_][A-Za-z0-9_]*)"
        )
        return pattern.findall(text)

    @staticmethod
    def _language(path: Path) -> str:
        return {
            ".py": "python",
            ".ts": "typescript",
            ".tsx": "tsx",
            ".rs": "rust",
        }.get(path.suffix.lower(), path.suffix.lstrip("."))

    @staticmethod
    def _complexity_symbols(*claims: str) -> list[str]:
        symbols: set[str] = set()
        for claim in claims:
            for value in re.findall(r"\b[A-Z][A-Za-z_]*\b", claim):
                if value != "O":
                    symbols.add(value)
        return sorted(symbols)
