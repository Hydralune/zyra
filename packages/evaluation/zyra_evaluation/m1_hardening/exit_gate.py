from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import stable_digest
from .integration_scenarios import M1IntegrationScenarioSuite
from .owner_matrix import REQUIRED_DISABLE_CAPABILITIES


class ExitDecision(StrEnum):
    READY_FOR_M2 = "ready_for_m2"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class LineEvidence:
    slice_id: str
    baseline_commit: str
    target_commit: str
    raw_additions: int
    production_raw: int
    effective_production: int
    tests: int
    docs: int
    generated: int
    data: int
    vendor_like: int
    adapter_only: int
    mock_fixture: int
    minimum_required: int
    auditor_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LineEvidence":
        return cls(
            slice_id=str(value.get("slice_id") or ""),
            baseline_commit=str(value.get("baseline_commit") or ""),
            target_commit=str(value.get("target_commit") or ""),
            raw_additions=_int(value.get("raw_additions")),
            production_raw=_int(value.get("production_raw")),
            effective_production=_int(value.get("effective_production")),
            tests=_int(value.get("tests")),
            docs=_int(value.get("docs")),
            generated=_int(value.get("generated")),
            data=_int(value.get("data")),
            vendor_like=_int(value.get("vendor_like")),
            adapter_only=_int(value.get("adapter_only")),
            mock_fixture=_int(value.get("mock_fixture")),
            minimum_required=_int(value.get("minimum_required")),
            auditor_digest=str(value.get("auditor_digest") or value.get("content_digest") or ""),
        )

    @property
    def passes(self) -> bool:
        return (
            self.effective_production >= self.minimum_required
            and self.effective_production <= self.production_raw
            and self.production_raw <= self.raw_additions
            and bool(re.fullmatch(r"[0-9a-f]{64}", self.auditor_digest.lower()))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slice_id": self.slice_id,
            "baseline_commit": self.baseline_commit,
            "target_commit": self.target_commit,
            "raw_additions": self.raw_additions,
            "production_raw": self.production_raw,
            "effective_production": self.effective_production,
            "tests": self.tests,
            "docs": self.docs,
            "generated": self.generated,
            "data": self.data,
            "vendor_like": self.vendor_like,
            "adapter_only": self.adapter_only,
            "mock_fixture": self.mock_fixture,
            "minimum_required": self.minimum_required,
            "auditor_digest": self.auditor_digest,
            "passes": self.passes,
        }


@dataclass(frozen=True, slots=True)
class GateAttestation:
    gate_id: str
    status: GateStatus
    blocker_count: int
    error_count: int
    evidence_count: int
    metrics_digest: str
    evidence_refs: tuple[str, ...]
    limitations: tuple[str, ...]

    @classmethod
    def from_gate(cls, gate: GateResult) -> "GateAttestation":
        return cls(
            gate_id=gate.gate_id,
            status=gate.status,
            blocker_count=gate.blocker_count,
            error_count=gate.error_count,
            evidence_count=len(gate.evidence),
            metrics_digest=stable_digest(gate.metrics),
            evidence_refs=tuple(item.location for item in gate.evidence if item.location),
            limitations=tuple(gate.limitations),
        )

    @property
    def passed(self) -> bool:
        return (
            self.status is GateStatus.PASSED
            and self.blocker_count == 0
            and self.error_count == 0
            and self.evidence_count > 0
            and not self.limitations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status.value,
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "evidence_count": self.evidence_count,
            "metrics_digest": self.metrics_digest,
            "evidence_refs": list(self.evidence_refs),
            "limitations": list(self.limitations),
            "passed": self.passed,
        }


@dataclass(slots=True)
class M1ExitBundle:
    baseline_commit: str
    implementation_commit: str
    evidence_commit: str
    created_at: str
    line_evidence: list[LineEvidence]
    gates: list[GateAttestation]
    scenario_status: Mapping[str, str]
    executed_disable_capabilities: tuple[str, ...]
    cleanroom_commit: str
    cleanroom_digest: str
    state_custody_digest: str
    source_coverage_digest: str
    handoff_digest: str
    unresolved_requirements: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def parent_effective_lines(self) -> int:
        return sum(item.effective_production for item in self.line_evidence)

    @property
    def decision(self) -> ExitDecision:
        if self.unresolved_requirements:
            return ExitDecision.BLOCKED
        if not self.implementation_commit or not self.cleanroom_digest or not self.handoff_digest:
            return ExitDecision.INCOMPLETE
        if not self.line_evidence or not all(item.passes for item in self.line_evidence):
            return ExitDecision.BLOCKED
        if not self.gates or not all(item.passed for item in self.gates):
            return ExitDecision.BLOCKED
        if any(status != GateStatus.PASSED.value for status in self.scenario_status.values()):
            return ExitDecision.BLOCKED
        if set(REQUIRED_DISABLE_CAPABILITIES) - set(self.executed_disable_capabilities):
            return ExitDecision.BLOCKED
        return ExitDecision.READY_FOR_M2

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.m1-exit-bundle/v1",
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "evidence_commit": self.evidence_commit,
            "created_at": self.created_at,
            "line_evidence": [item.to_dict() for item in self.line_evidence],
            "parent_effective_lines": self.parent_effective_lines,
            "gates": [item.to_dict() for item in self.gates],
            "scenario_status": dict(sorted(self.scenario_status.items())),
            "executed_disable_capabilities": list(self.executed_disable_capabilities),
            "cleanroom_commit": self.cleanroom_commit,
            "cleanroom_digest": self.cleanroom_digest,
            "state_custody_digest": self.state_custody_digest,
            "source_coverage_digest": self.source_coverage_digest,
            "handoff_digest": self.handoff_digest,
            "unresolved_requirements": list(self.unresolved_requirements),
            "metadata": dict(self.metadata),
            "decision": self.decision.value,
        }
        if include_digest:
            value["content_digest"] = stable_digest(value)
        return value


class ExitPolicy:
    REQUIRED_GATES = {
        "source-to-target-coverage",
        "m1-internalization",
        "m1-state-custody",
        "m1-owner-matrix",
        "langgraph-boundary",
        "m1-integration-scenarios",
        "m1-cross-scenario-consistency",
        "m1-topology-adversarial-integration",
        "disable-module-probe",
        "m1-long-horizon-benchmark",
        "m1-evidence-admission",
        "causal-evidence",
        "low-entropy",
        "sealed-autonomy",
        "m1-live-execution-tiers",
        "m1-live-provider-wires",
        "patch-git",
        "deny-policy",
        "secrets-prompt-injection",
        "code-index",
        "m1-cleanroom",
        "m1-m2-handoff",
        "effective-line-audit",
    }
    REQUIRED_SCENARIOS = set(M1IntegrationScenarioSuite().scenario_ids())
    REQUIRED_SLICE_LINES = {
        "M1-S08-01": 9_000,
        "M1-S08-02": 7_000,
    }
    PARENT_MINIMUM_LINES = 16_000

    def evaluate(self, bundle: M1ExitBundle) -> GateResult:
        result = GateResult(
            gate_id="m1-exit",
            status=GateStatus.NOT_RUN,
            summary="M1 parent completion and M2 release decision.",
        )
        self._commit_findings(bundle, result)
        self._line_findings(bundle, result)
        self._gate_findings(bundle, result)
        self._scenario_findings(bundle, result)
        self._disable_findings(bundle, result)
        self._evidence_findings(bundle, result)
        for requirement in bundle.unresolved_requirements:
            result.add(
                Finding(
                    code="exit.requirement_unresolved",
                    severity=Severity.BLOCKER,
                    summary="M1 exit retains an unresolved competition or engineering requirement.",
                    detail=requirement,
                )
            )
        result.metrics.update(
            {
                "decision": bundle.decision.value,
                "parent_effective_lines": bundle.parent_effective_lines,
                "line_slice_count": len(bundle.line_evidence),
                "gate_count": len(bundle.gates),
                "passed_gate_count": sum(item.passed for item in bundle.gates),
                "scenario_count": len(bundle.scenario_status),
                "passed_scenario_count": sum(
                    status == GateStatus.PASSED.value for status in bundle.scenario_status.values()
                ),
                "executed_disable_capability_count": len(bundle.executed_disable_capabilities),
                "unresolved_requirement_count": len(bundle.unresolved_requirements),
                "bundle_digest": bundle.to_dict()["content_digest"],
            }
        )
        result.evidence.append(
            EvidencePointer(
                kind="m1_exit_bundle",
                location=bundle.implementation_commit or "pending",
                summary=f"M1 exit decision: {bundle.decision.value}",
                revision=bundle.evidence_commit or bundle.implementation_commit,
                metadata={"bundle_digest": bundle.to_dict()["content_digest"]},
            )
        )
        return result.finish()

    @staticmethod
    def _commit_findings(bundle: M1ExitBundle, result: GateResult) -> None:
        commits = {
            "baseline_commit": bundle.baseline_commit,
            "implementation_commit": bundle.implementation_commit,
            "cleanroom_commit": bundle.cleanroom_commit,
        }
        for name, value in commits.items():
            if not re.fullmatch(r"[0-9a-f]{40}", value.lower()):
                result.add(
                    Finding(
                        code="exit.commit_invalid",
                        severity=Severity.BLOCKER,
                        summary="M1 exit bundle lacks a full immutable commit identity.",
                        detail=name,
                    )
                )
        if bundle.implementation_commit and bundle.cleanroom_commit != bundle.implementation_commit:
            result.add(
                Finding(
                    code="exit.cleanroom_commit_mismatch",
                    severity=Severity.BLOCKER,
                    summary="Cleanroom did not verify the exact M1 implementation commit.",
                    detail=(
                        f"implementation={bundle.implementation_commit}; cleanroom={bundle.cleanroom_commit}"
                    ),
                )
            )
        if bundle.evidence_commit and not re.fullmatch(r"[0-9a-f]{40}", bundle.evidence_commit.lower()):
            result.add(
                Finding(
                    code="exit.evidence_commit_invalid",
                    severity=Severity.BLOCKER,
                    summary="M1 evidence commit identity is malformed.",
                )
            )

    def _line_findings(self, bundle: M1ExitBundle, result: GateResult) -> None:
        by_slice = {item.slice_id: item for item in bundle.line_evidence}
        for slice_id, minimum in self.REQUIRED_SLICE_LINES.items():
            evidence = by_slice.get(slice_id)
            if evidence is None:
                result.add(
                    Finding(
                        code="exit.slice_line_evidence_missing",
                        severity=Severity.BLOCKER,
                        summary="M1 parent lacks effective-line evidence for a required slice.",
                        detail=slice_id,
                    )
                )
                continue
            if evidence.minimum_required != minimum:
                result.add(
                    Finding(
                        code="exit.slice_line_floor_changed",
                        severity=Severity.BLOCKER,
                        summary="Slice line evidence changed the frozen minimum.",
                        detail=f"{slice_id}: expected={minimum}; observed={evidence.minimum_required}",
                    )
                )
            if not evidence.passes:
                result.add(
                    Finding(
                        code="exit.slice_line_gate_failed",
                        severity=Severity.BLOCKER,
                        summary="Slice does not meet conservative effective production-line evidence.",
                        detail=(
                            f"{slice_id}: effective={evidence.effective_production}; "
                            f"minimum={evidence.minimum_required}"
                        ),
                    )
                )
        if bundle.parent_effective_lines < self.PARENT_MINIMUM_LINES:
            result.add(
                Finding(
                    code="exit.parent_line_floor_failed",
                    severity=Severity.BLOCKER,
                    summary="M1-08 parent does not meet its cumulative effective-line floor.",
                    detail=(
                        f"required={self.PARENT_MINIMUM_LINES}; observed={bundle.parent_effective_lines}"
                    ),
                )
            )

    def _gate_findings(self, bundle: M1ExitBundle, result: GateResult) -> None:
        by_id = {item.gate_id: item for item in bundle.gates}
        duplicates = Counter(item.gate_id for item in bundle.gates)
        for gate_id, count in duplicates.items():
            if count > 1:
                result.add(
                    Finding(
                        code="exit.gate_duplicate",
                        severity=Severity.BLOCKER,
                        summary="M1 exit bundle contains duplicate gate identity.",
                        detail=gate_id,
                    )
                )
        for gate_id in sorted(self.REQUIRED_GATES - set(by_id)):
            result.add(
                Finding(
                    code="exit.gate_missing",
                    severity=Severity.BLOCKER,
                    summary="M1 exit bundle omits a mandatory gate.",
                    detail=gate_id,
                )
            )
        for gate_id in sorted(self.REQUIRED_GATES & set(by_id)):
            attestation = by_id[gate_id]
            if not attestation.passed:
                result.add(
                    Finding(
                        code="exit.gate_not_passed",
                        severity=Severity.BLOCKER,
                        summary="Mandatory M1 gate is not fully passed with evidence.",
                        detail=(
                            f"{gate_id}: status={attestation.status.value}; "
                            f"blockers={attestation.blocker_count}; errors={attestation.error_count}; "
                            f"evidence={attestation.evidence_count}"
                        ),
                    )
                )

    def _scenario_findings(self, bundle: M1ExitBundle, result: GateResult) -> None:
        observed = set(bundle.scenario_status)
        for scenario_id in sorted(self.REQUIRED_SCENARIOS - observed):
            result.add(
                Finding(
                    code="exit.scenario_missing",
                    severity=Severity.BLOCKER,
                    summary="M1 exit omits one of the six required integration scenarios.",
                    detail=scenario_id,
                )
            )
        for scenario_id in sorted(self.REQUIRED_SCENARIOS & observed):
            status = bundle.scenario_status[scenario_id]
            if status != GateStatus.PASSED.value:
                result.add(
                    Finding(
                        code="exit.scenario_not_passed",
                        severity=Severity.BLOCKER,
                        summary="Required M1 integration scenario is not passed.",
                        detail=f"{scenario_id}: {status}",
                    )
                )

    @staticmethod
    def _disable_findings(bundle: M1ExitBundle, result: GateResult) -> None:
        observed = set(bundle.executed_disable_capabilities)
        for capability in sorted(set(REQUIRED_DISABLE_CAPABILITIES) - observed):
            result.add(
                Finding(
                    code="exit.disable_capability_missing",
                    severity=Severity.BLOCKER,
                    summary="M1 exit lacks executed disconnect evidence for a canonical owner.",
                    capability=capability,
                )
            )
        duplicates = Counter(bundle.executed_disable_capabilities)
        for capability, count in duplicates.items():
            if count > 1:
                result.add(
                    Finding(
                        code="exit.disable_capability_duplicate",
                        severity=Severity.ERROR,
                        summary="M1 exit lists duplicate owner-disconnect evidence.",
                        capability=capability,
                        detail=f"count={count}",
                    )
                )

    @staticmethod
    def _evidence_findings(bundle: M1ExitBundle, result: GateResult) -> None:
        for name, value in (
            ("cleanroom_digest", bundle.cleanroom_digest),
            ("state_custody_digest", bundle.state_custody_digest),
            ("source_coverage_digest", bundle.source_coverage_digest),
            ("handoff_digest", bundle.handoff_digest),
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", value.lower()):
                result.add(
                    Finding(
                        code="exit.evidence_digest_invalid",
                        severity=Severity.BLOCKER,
                        summary="M1 exit evidence digest is missing or malformed.",
                        detail=name,
                    )
                )


class ExitBundleBuilder:
    def build(
        self,
        *,
        baseline_commit: str,
        implementation_commit: str,
        evidence_commit: str,
        line_evidence: Sequence[LineEvidence | Mapping[str, Any]],
        gates: Sequence[GateResult],
        scenario_status: Mapping[str, str],
        executed_disable_capabilities: Sequence[str],
        cleanroom_commit: str,
        cleanroom_digest: str,
        state_custody_digest: str,
        source_coverage_digest: str,
        handoff_digest: str,
        unresolved_requirements: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> M1ExitBundle:
        lines = [
            item if isinstance(item, LineEvidence) else LineEvidence.from_mapping(item)
            for item in line_evidence
        ]
        return M1ExitBundle(
            baseline_commit=baseline_commit,
            implementation_commit=implementation_commit,
            evidence_commit=evidence_commit,
            created_at=utc_now(),
            line_evidence=lines,
            gates=[GateAttestation.from_gate(gate) for gate in gates],
            scenario_status=dict(scenario_status),
            executed_disable_capabilities=tuple(executed_disable_capabilities),
            cleanroom_commit=cleanroom_commit,
            cleanroom_digest=cleanroom_digest,
            state_custody_digest=state_custody_digest,
            source_coverage_digest=source_coverage_digest,
            handoff_digest=handoff_digest,
            unresolved_requirements=tuple(unresolved_requirements),
            metadata=dict(metadata or {}),
        )


def exit_summary(bundle: M1ExitBundle, gate: GateResult) -> str:
    lines = [
        "# M1 exit decision",
        "",
        f"- decision: `{bundle.decision.value}`",
        f"- implementation commit: `{bundle.implementation_commit or 'pending'}`",
        f"- effective production lines: `{bundle.parent_effective_lines}`",
        f"- passed gates: `{sum(item.passed for item in bundle.gates)}/{len(bundle.gates)}`",
        f"- passed scenarios: `{sum(status == 'passed' for status in bundle.scenario_status.values())}/{len(bundle.scenario_status)}`",
        f"- executed disable capabilities: `{len(bundle.executed_disable_capabilities)}/{len(REQUIRED_DISABLE_CAPABILITIES)}`",
        f"- blockers: `{gate.blocker_count}`",
        "",
    ]
    blockers = [finding for finding in gate.findings if finding.severity is Severity.BLOCKER]
    if blockers:
        lines.append("## Blockers")
        lines.append("")
        for finding in blockers:
            detail = f" — {finding.detail}" if finding.detail else ""
            lines.append(f"- `{finding.code}`: {finding.summary}{detail}")
    return "\n".join(lines).rstrip() + "\n"


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
