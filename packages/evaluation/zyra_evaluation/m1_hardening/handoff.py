from __future__ import annotations

import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import HANDOFF_SCHEMA, redact_mapping, stable_digest


@dataclass(frozen=True, slots=True)
class HandoffSurface:
    surface_id: str
    kind: str
    method: str
    path: str
    owner: str
    state_family: str
    event_types: tuple[str, ...]
    artifact_kinds: tuple[str, ...]
    control_commands: tuple[str, ...]
    scenario_ids: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    stable: bool
    limitations: tuple[str, ...] = ()

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not re.fullmatch(r"[a-z][a-z0-9-]{2,100}", self.surface_id):
            issues.append("invalid surface id")
        if self.kind not in {"api", "event", "artifact", "control", "stream", "worker"}:
            issues.append(f"unsupported surface kind: {self.kind}")
        if self.kind == "api":
            if self.method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                issues.append("API surface has invalid method")
            if not self.path.startswith("/"):
                issues.append("API surface has invalid path")
        if not self.owner:
            issues.append("surface has no canonical owner")
        if not self.state_family:
            issues.append("surface has no state family")
        if not self.evidence_refs:
            issues.append("surface has no executed evidence reference")
        if self.stable and self.limitations:
            issues.append("stable surface cannot retain unresolved limitations")
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface_id": self.surface_id,
            "kind": self.kind,
            "method": self.method,
            "path": self.path,
            "owner": self.owner,
            "state_family": self.state_family,
            "event_types": list(self.event_types),
            "artifact_kinds": list(self.artifact_kinds),
            "control_commands": list(self.control_commands),
            "scenario_ids": list(self.scenario_ids),
            "evidence_refs": list(self.evidence_refs),
            "stable": self.stable,
            "limitations": list(self.limitations),
        }


@dataclass(frozen=True, slots=True)
class SourceChainHandoff:
    chain_id: str
    source_repository: str
    source_paths: tuple[str, ...]
    source_role: str
    source_language: str
    target_paths: tuple[str, ...]
    target_language: str
    canonical_owner: str
    state_family: str
    main_path_surfaces: tuple[str, ...]
    test_paths: tuple[str, ...]
    disable_probe_ids: tuple[str, ...]
    residual_dependencies: tuple[str, ...]
    final_decision: str

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if not self.chain_id or not self.source_repository:
            issues.append("source chain identity is incomplete")
        if self.source_role in {"primary", "supplementary"}:
            if not self.target_paths:
                issues.append("active source chain has no target path")
            if not self.main_path_surfaces:
                issues.append("active source chain has no main-path surface")
            if not self.test_paths:
                issues.append("active source chain has no behavior test")
            if not self.disable_probe_ids:
                issues.append("active source chain has no disable probe")
            if not self.canonical_owner:
                issues.append("active source chain has no canonical owner")
        if self.source_role in {"conformance", "reference", "experimental", "deferred", "rejected", "excluded"}:
            if self.final_decision not in {"conformance_only", "reference_only", "experimental", "deferred", "rejected", "excluded_forward_only"}:
                issues.append("inactive source chain has inconsistent final decision")
        if self.source_repository.lower() == "openclaw" and self.final_decision != "excluded_forward_only":
            issues.append("OpenClaw must remain excluded_forward_only")
        return tuple(issues)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "source_repository": self.source_repository,
            "source_paths": list(self.source_paths),
            "source_role": self.source_role,
            "source_language": self.source_language,
            "target_paths": list(self.target_paths),
            "target_language": self.target_language,
            "canonical_owner": self.canonical_owner,
            "state_family": self.state_family,
            "main_path_surfaces": list(self.main_path_surfaces),
            "test_paths": list(self.test_paths),
            "disable_probe_ids": list(self.disable_probe_ids),
            "residual_dependencies": list(self.residual_dependencies),
            "final_decision": self.final_decision,
        }


@dataclass(slots=True)
class M2HandoffContract:
    baseline_commit: str
    target_commit: str
    created_at: str
    surfaces: list[HandoffSurface]
    source_chains: list[SourceChainHandoff]
    state_custody: list[Mapping[str, Any]]
    scenario_status: Mapping[str, str]
    gate_status: Mapping[str, str]
    residual_blockers: list[Mapping[str, Any]]
    cleanroom_digest: str
    report_id: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return (
            bool(self.target_commit)
            and bool(self.surfaces)
            and all(item.stable and not item.limitations for item in self.surfaces)
            and bool(self.source_chains)
            and not self.residual_blockers
            and bool(self.cleanroom_digest)
            and all(status == "passed" for status in self.scenario_status.values())
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": HANDOFF_SCHEMA,
            "baseline_commit": self.baseline_commit,
            "target_commit": self.target_commit,
            "created_at": self.created_at,
            "surfaces": [item.to_dict() for item in self.surfaces],
            "source_chains": [item.to_dict() for item in self.source_chains],
            "state_custody": [redact_mapping(item) for item in self.state_custody],
            "scenario_status": dict(sorted(self.scenario_status.items())),
            "gate_status": dict(sorted(self.gate_status.items())),
            "residual_blockers": [redact_mapping(item) for item in self.residual_blockers],
            "cleanroom_digest": self.cleanroom_digest,
            "report_id": self.report_id,
            "metadata": redact_mapping(self.metadata),
            "ready": self.ready,
        }
        if include_digest:
            value["content_digest"] = stable_digest(value)
        return value


class HandoffBuilder:
    def build(
        self,
        *,
        baseline_commit: str,
        target_commit: str,
        surfaces: Sequence[HandoffSurface],
        source_chains: Sequence[SourceChainHandoff],
        state_custody: Sequence[Mapping[str, Any]],
        scenario_status: Mapping[str, str],
        gates: Sequence[GateResult],
        cleanroom_digest: str,
        report_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> M2HandoffContract:
        blockers: list[Mapping[str, Any]] = []
        for gate in gates:
            for finding in gate.findings:
                if finding.severity is Severity.BLOCKER:
                    blockers.append(
                        {
                            "gate_id": gate.gate_id,
                            "code": finding.code,
                            "summary": finding.summary,
                            "detail": finding.detail,
                            "capability": finding.capability,
                        }
                    )
        return M2HandoffContract(
            baseline_commit=baseline_commit,
            target_commit=target_commit,
            created_at=utc_now(),
            surfaces=list(surfaces),
            source_chains=list(source_chains),
            state_custody=[dict(item) for item in state_custody],
            scenario_status=dict(scenario_status),
            gate_status={gate.gate_id: gate.status.value for gate in gates},
            residual_blockers=blockers,
            cleanroom_digest=cleanroom_digest,
            report_id=report_id,
            metadata=dict(metadata or {}),
        )


class HandoffGate:
    REQUIRED_SURFACE_KINDS = {"api", "event", "artifact", "control", "stream", "worker"}
    REQUIRED_STATE_FAMILIES = {
        "task_session",
        "runtime_event",
        "permission",
        "memory",
        "worker_lease",
        "provider_backend",
        "graph_topology",
        "recovery",
        "workspace",
        "skill_invocation",
        "code_index",
    }

    def evaluate(self, contract: M2HandoffContract, *, final_completion: bool) -> GateResult:
        result = GateResult(
            gate_id="m1-m2-handoff",
            status=GateStatus.NOT_RUN,
            summary="Stable M1 API/event/artifact/control/worker handoff contract for M2.",
        )
        self._validate_identity(contract, result)
        self._validate_surfaces(contract, result, final_completion=final_completion)
        self._validate_sources(contract, result)
        self._validate_custody(contract, result)
        self._validate_status(contract, result, final_completion=final_completion)
        result.metrics.update(
            {
                "ready": contract.ready,
                "surface_count": len(contract.surfaces),
                "source_chain_count": len(contract.source_chains),
                "state_family_count": len(
                    {str(item.get("state_family") or "") for item in contract.state_custody}
                ),
                "scenario_count": len(contract.scenario_status),
                "gate_count": len(contract.gate_status),
                "residual_blocker_count": len(contract.residual_blockers),
                "content_digest": contract.to_dict()["content_digest"],
                "surface_kind_counts": dict(Counter(item.kind for item in contract.surfaces)),
                "source_role_counts": dict(Counter(item.source_role for item in contract.source_chains)),
            }
        )
        result.evidence.append(
            EvidencePointer(
                kind="m2_handoff",
                location=contract.report_id,
                summary=f"M1 to M2 handoff: {'ready' if contract.ready else 'blocked'}",
                revision=contract.target_commit,
                metadata={"content_digest": contract.to_dict()["content_digest"]},
            )
        )
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _validate_identity(contract: M2HandoffContract, result: GateResult) -> None:
        for name, value in (
            ("baseline_commit", contract.baseline_commit),
            ("target_commit", contract.target_commit),
            ("report_id", contract.report_id),
            ("cleanroom_digest", contract.cleanroom_digest),
        ):
            if not value:
                result.add(
                    Finding(
                        code="handoff.identity_missing",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff lacks required immutable identity.",
                        detail=name,
                    )
                )
        if contract.baseline_commit == contract.target_commit:
            result.add(
                Finding(
                    code="handoff.commit_range_empty",
                    severity=Severity.BLOCKER,
                    summary="M2 handoff baseline and target commit are identical.",
                )
            )

    def _validate_surfaces(
        self,
        contract: M2HandoffContract,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        ids: set[str] = set()
        kinds: set[str] = set()
        for surface in contract.surfaces:
            if surface.surface_id in ids:
                result.add(
                    Finding(
                        code="handoff.surface_duplicate",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff contains duplicate surface identity.",
                        detail=surface.surface_id,
                    )
                )
            ids.add(surface.surface_id)
            kinds.add(surface.kind)
            for issue in surface.validate():
                result.add(
                    Finding(
                        code="handoff.surface_invalid",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff surface contract is invalid.",
                        detail=f"{surface.surface_id}: {issue}",
                    )
                )
            if final_completion and not surface.stable:
                result.add(
                    Finding(
                        code="handoff.surface_unstable",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff exposes an unstable M1 surface.",
                        detail=surface.surface_id,
                    )
                )
        if final_completion:
            for kind in sorted(self.REQUIRED_SURFACE_KINDS - kinds):
                result.add(
                    Finding(
                        code="handoff.surface_kind_missing",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff lacks a required product surface kind.",
                        detail=kind,
                    )
                )

    @staticmethod
    def _validate_sources(contract: M2HandoffContract, result: GateResult) -> None:
        ids: set[str] = set()
        omp_found = False
        for chain in contract.source_chains:
            if chain.chain_id in ids:
                result.add(
                    Finding(
                        code="handoff.source_chain_duplicate",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff contains duplicate source chain identity.",
                        detail=chain.chain_id,
                    )
                )
            ids.add(chain.chain_id)
            omp_found = omp_found or chain.source_repository == "oh-my-pi"
            for issue in chain.validate():
                result.add(
                    Finding(
                        code="handoff.source_chain_invalid",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff source-to-target chain is incomplete.",
                        detail=f"{chain.chain_id}: {issue}",
                    )
                )
            for dependency in chain.residual_dependencies:
                if dependency.startswith("../") or "agent-zoo" in dependency.lower():
                    result.add(
                        Finding(
                            code="handoff.external_dependency",
                            severity=Severity.BLOCKER,
                            summary="M2 handoff retains a sibling source-repository dependency.",
                            detail=f"{chain.chain_id}: {dependency}",
                        )
                    )
        if not omp_found:
            result.add(
                Finding(
                    code="handoff.omp_chain_missing",
                    severity=Severity.BLOCKER,
                    summary="M2 handoff omits final Oh My Pi source-chain decisions.",
                )
            )

    def _validate_custody(self, contract: M2HandoffContract, result: GateResult) -> None:
        by_family: dict[str, set[str]] = defaultdict(set)
        for entry in contract.state_custody:
            family = str(entry.get("state_family") or "")
            owner = str(entry.get("canonical_owner") or "")
            if family and owner:
                by_family[family].add(owner)
        for family in sorted(self.REQUIRED_STATE_FAMILIES - set(by_family)):
            result.add(
                Finding(
                    code="handoff.state_family_missing",
                    severity=Severity.BLOCKER,
                    summary="M2 handoff lacks canonical custody for a required state family.",
                    detail=family,
                )
            )
        for family, owners in sorted(by_family.items()):
            if len(owners) > 1:
                result.add(
                    Finding(
                        code="handoff.state_owner_ambiguous",
                        severity=Severity.BLOCKER,
                        summary="M2 handoff gives one state family multiple canonical owners.",
                        detail=f"{family}: {', '.join(sorted(owners))}",
                    )
                )

    @staticmethod
    def _validate_status(
        contract: M2HandoffContract,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        if final_completion:
            for scenario_id, status in contract.scenario_status.items():
                if status != "passed":
                    result.add(
                        Finding(
                            code="handoff.scenario_not_passed",
                            severity=Severity.BLOCKER,
                            summary="M2 handoff includes an incomplete M1 scenario.",
                            detail=f"{scenario_id}: {status}",
                        )
                    )
            for gate_id, status in contract.gate_status.items():
                if status not in {"passed"}:
                    result.add(
                        Finding(
                            code="handoff.gate_not_passed",
                            severity=Severity.BLOCKER,
                            summary="M2 handoff includes an incomplete M1 exit gate.",
                            detail=f"{gate_id}: {status}",
                        )
                    )
        for blocker in contract.residual_blockers:
            result.add(
                Finding(
                    code="handoff.residual_blocker",
                    severity=Severity.BLOCKER,
                    summary="M2 handoff cannot carry an unresolved M1 blocker.",
                    detail=f"{blocker.get('gate_id')}: {blocker.get('code')} — {blocker.get('summary')}",
                )
            )


class HandoffStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(self, contract: M2HandoffContract) -> Path:
        payload = contract.to_dict()
        name = f"m1-m2-handoff-{contract.target_commit[:12] or 'pending'}.json"
        target = (self.root / name).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("handoff target escapes artifact root") from error
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("content_digest") == payload.get("content_digest"):
                return target
            raise RuntimeError("handoff artifact already exists with different immutable content")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(target)
        return target

    def load(self, path: str | Path) -> Mapping[str, Any]:
        target = Path(path).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("handoff path escapes artifact root") from error
        value = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping) or value.get("schema") != HANDOFF_SCHEMA:
            raise ValueError("unsupported handoff artifact")
        expected = str(value.get("content_digest") or "")
        content = dict(value)
        content.pop("content_digest", None)
        if stable_digest(content) != expected:
            raise ValueError("handoff artifact digest mismatch")
        return value


def default_handoff_surfaces(evidence_refs: Sequence[str] = ()) -> tuple[HandoffSurface, ...]:
    refs = tuple(evidence_refs)
    return (
        HandoffSurface(
            surface_id="task-query-worker-api",
            kind="api",
            method="POST",
            path="/tasks/{task_id}/workers/code",
            owner="QueryEngine/CodeWorkerRuntime",
            state_family="task_session",
            event_types=("worker_started", "tool_started", "tool_completed"),
            artifact_kinds=("file", "markdown", "diff"),
            control_commands=(),
            scenario_ids=("m1-integration-query-session-context-tool",),
            evidence_refs=refs,
            stable=bool(refs),
        ),
        HandoffSurface(
            surface_id="runtime-event-stream-api",
            kind="stream",
            method="GET",
            path="/tasks/{task_id}/runtime-event-stream",
            owner="RuntimeEventSpine",
            state_family="runtime_event",
            event_types=("runtime_event_appended", "provider_attempt", "recovery_rerouted"),
            artifact_kinds=(),
            control_commands=(),
            scenario_ids=("m1-integration-api-stream-provider-failover",),
            evidence_refs=refs,
            stable=bool(refs),
        ),
        HandoffSurface(
            surface_id="permission-control-api",
            kind="api",
            method="POST",
            path="/permissions/requests/{request_id}/respond",
            owner="PermissionJournal/PermissionApprovalRuntime",
            state_family="permission",
            event_types=("permission_decision", "permission_approval"),
            artifact_kinds=(),
            control_commands=(),
            scenario_ids=("m1-integration-dangerous-tool-permission",),
            evidence_refs=refs,
            stable=bool(refs),
        ),
        HandoffSurface(
            surface_id="task-artifact-surface",
            kind="artifact",
            method="GET",
            path="/tasks/{task_id}/artifacts",
            owner="TaskArtifactStore",
            state_family="workspace",
            event_types=("artifact_written",),
            artifact_kinds=("file", "markdown", "diff", "trace"),
            control_commands=("/export",),
            scenario_ids=("m1-integration-query-session-context-tool",),
            evidence_refs=refs,
            stable=bool(refs),
        ),
        HandoffSurface(
            surface_id="task-control-command-surface",
            kind="control",
            method="POST",
            path="/tasks/{task_id}/commands",
            owner="ControlCommandRuntime",
            state_family="task_session",
            event_types=("control_command", "requirement_change", "failure_injected"),
            artifact_kinds=("trace",),
            control_commands=("/change", "/compact", "/retry", "/inject", "/export"),
            scenario_ids=(
                "m1-integration-dangerous-tool-permission",
                "m1-integration-subagent-worker-recovery",
            ),
            evidence_refs=refs,
            stable=bool(refs),
        ),
        HandoffSurface(
            surface_id="worker-route-recovery-surface",
            kind="worker",
            method="POST",
            path="/tasks/{task_id}/recovery/worker-handoff",
            owner="WorkerPoolStore/RecoveryApplication",
            state_family="worker_lease",
            event_types=("worker_lease_acquired", "worker_lost", "recovery_rerouted"),
            artifact_kinds=("checkpoint", "recovery_plan"),
            control_commands=("/retry",),
            scenario_ids=("m1-integration-subagent-worker-recovery",),
            evidence_refs=refs,
            stable=bool(refs),
        ),
    )
