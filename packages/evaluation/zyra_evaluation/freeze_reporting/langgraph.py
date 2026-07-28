from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .canonical import digest, now, require_mapping, require_sequence
from .errors import blocker, require_no_blockers
from .sources import behavior_tests, existing_targets


ACTIVE_MECHANISMS = {
    "checkpoint-identity-lineage": "exact_resume",
    "pending-committed-writes": "exact_resume",
    "side-effect-fence": "exact_resume",
    "exact-resume": "exact_resume",
}

INACTIVE_MECHANISMS = (
    "stategraph-pregel",
    "generic-channel-reducer",
    "toolnode-stream-store",
    "sdk-server-deploy",
)

ZYRA_OWNED_COUNTERPARTS = {
    "open-world-dynamic-topology": "dynamic_topology",
    "immutable-branch-local-delta": "dynamic_topology",
    "explicit-read-write-conflict": "dynamic_topology",
    "deterministic-commit": "dynamic_topology",
    "cohesive-codeworker-reasoning-loop": "code_worker_loop",
}


class LangGraphCorrectionMatrixBuilder:
    """Makes the 2026-07-13 LangGraph boundary executable and auditable."""

    def __init__(self, repository_root: str | Path) -> None:
        self.repository_root = Path(repository_root).resolve(strict=True)

    def build(self) -> dict[str, Any]:
        active = [
            self._active_row(mechanism, capability)
            for mechanism, capability in ACTIVE_MECHANISMS.items()
        ]
        inactive = [
            {
                "mechanism": mechanism,
                "role": "inactive",
                "production_owner": "",
                "target_paths": [],
                "test_paths": [],
                "decision": (
                    "No production owner or migration quota after the "
                    "2026-07-13 forward correction."
                ),
            }
            for mechanism in INACTIVE_MECHANISMS
        ]
        zyra_owned = [
            self._zyra_row(mechanism, capability)
            for mechanism, capability in ZYRA_OWNED_COUNTERPARTS.items()
        ]
        matrix = {
            "schema": "zyra.langgraph-forward-correction-matrix/v1",
            "effective_from": "2026-07-13",
            "scope": (
                "narrow checkpoint/exact-resume semantics only; no generic graph "
                "runtime, reducer, channel, ToolNode, Store, SDK, or deploy owner"
            ),
            "active_langgraph_semantics": active,
            "inactive_langgraph_subsystems": inactive,
            "zyra_owned_counterparts": zyra_owned,
            "anti_patterns": [
                "compile-time-only node and edge selection is not dynamic topology",
                "in-place mutation of shared dict/list/object is forbidden",
                "parallel completion order cannot decide reducer output",
                "pending writes cannot be exposed as committed writes",
                "external effects require an idempotency fence",
                "CodeWorker reason-tool-observe-revise cannot be split into graph micro-nodes",
            ],
            "generated_at": now(),
        }
        matrix["verification"] = self.verify(matrix)
        matrix["matrix_digest"] = digest(matrix)
        return matrix

    def verify(self, matrix: Mapping[str, Any]) -> dict[str, Any]:
        selected = require_mapping(matrix, "LangGraph correction matrix")
        findings: list[dict[str, Any]] = []
        active = [
            require_mapping(item, "active LangGraph row")
            for item in require_sequence(
                selected.get("active_langgraph_semantics"),
                "active LangGraph rows",
            )
        ]
        inactive = [
            require_mapping(item, "inactive LangGraph row")
            for item in require_sequence(
                selected.get("inactive_langgraph_subsystems"),
                "inactive LangGraph rows",
            )
        ]
        zyra_owned = [
            require_mapping(item, "Zyra-owned correction row")
            for item in require_sequence(
                selected.get("zyra_owned_counterparts"),
                "Zyra-owned correction rows",
            )
        ]
        if {str(item.get("mechanism")) for item in active} != set(
            ACTIVE_MECHANISMS
        ):
            findings.append(
                blocker(
                    "langgraph-active-set-mismatch",
                    "Active LangGraph semantics exceed or omit the protected scope.",
                )
            )
        for row in active:
            if row.get("role") != "primary_semantic_source":
                findings.append(
                    blocker(
                        "langgraph-active-role-invalid",
                        "Active LangGraph row has the wrong role.",
                        mechanism=row.get("mechanism"),
                    )
                )
            findings.extend(self._path_findings(row, active=True))
        if {str(item.get("mechanism")) for item in inactive} != set(
            INACTIVE_MECHANISMS
        ):
            findings.append(
                blocker(
                    "langgraph-inactive-set-mismatch",
                    "Inactive LangGraph subsystem set is incomplete.",
                )
            )
        for row in inactive:
            if (
                row.get("role") != "inactive"
                or row.get("production_owner")
                or row.get("target_paths")
                or row.get("test_paths")
            ):
                findings.append(
                    blocker(
                        "langgraph-inactive-obligation-forbidden",
                        "Inactive LangGraph subsystem acquired a production obligation.",
                        mechanism=row.get("mechanism"),
                    )
                )
        for row in zyra_owned:
            if row.get("role") != "zyra_owned":
                findings.append(
                    blocker(
                        "langgraph-zyra-owner-missing",
                        "Corrected graph mechanism must remain Zyra-owned.",
                        mechanism=row.get("mechanism"),
                    )
                )
            findings.extend(self._path_findings(row, active=True))
        if len(selected.get("anti_patterns") or []) < 6:
            findings.append(
                blocker(
                    "langgraph-antipattern-matrix-incomplete",
                    "Correction matrix omits a protected anti-pattern.",
                )
            )
        require_no_blockers(
            findings,
            code="langgraph-correction-invalid",
            message="LangGraph forward correction matrix is not enforceable.",
            phase="material",
        )
        receipt = {
            "schema": "zyra.langgraph-forward-correction-verification/v1",
            "valid": True,
            "active_semantic_count": len(active),
            "inactive_subsystem_count": len(inactive),
            "zyra_owned_count": len(zyra_owned),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _active_row(self, mechanism: str, capability: str) -> dict[str, Any]:
        return {
            "mechanism": mechanism,
            "role": "primary_semantic_source",
            "production_owner": "GraphCommitRuntime / CheckpointRecoveryRuntime",
            "target_paths": existing_targets(self.repository_root, capability),
            "test_paths": behavior_tests(self.repository_root, capability),
            "decision": (
                "LangGraph supplies narrow semantic conformance; Zyra owns the "
                "production implementation, persistence, events, and recovery."
            ),
        }

    def _zyra_row(self, mechanism: str, capability: str) -> dict[str, Any]:
        owner = (
            "CodeWorkerRuntime"
            if capability == "code_worker_loop"
            else "Zyra DynamicTopology / GraphCommitRuntime"
        )
        return {
            "mechanism": mechanism,
            "role": "zyra_owned",
            "production_owner": owner,
            "target_paths": existing_targets(self.repository_root, capability),
            "test_paths": behavior_tests(self.repository_root, capability),
            "decision": (
                "This mechanism is implemented and tested inside Zyra; LangGraph "
                "does not acquire production custody."
            ),
        }

    def _path_findings(
        self,
        row: Mapping[str, Any],
        *,
        active: bool,
    ) -> list[dict[str, Any]]:
        findings = []
        for field in ("target_paths", "test_paths"):
            paths = [str(item) for item in row.get(field) or []]
            if active and not paths:
                findings.append(
                    blocker(
                        "langgraph-correction-path-missing",
                        "Active correction row lacks a code or behavior-test anchor.",
                        mechanism=row.get("mechanism"),
                        field=field,
                    )
                )
            for path in paths:
                if not (self.repository_root / path).exists():
                    findings.append(
                        blocker(
                            "langgraph-correction-path-unresolved",
                            "Correction matrix path does not resolve.",
                            mechanism=row.get("mechanism"),
                            path=path,
                        )
                    )
        return findings
