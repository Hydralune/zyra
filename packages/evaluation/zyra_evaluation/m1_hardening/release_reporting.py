from __future__ import annotations

import html
import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .contracts import Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import stable_digest
from .integration_service import IntegrationOutcome


@dataclass(frozen=True, slots=True)
class ReleaseMetric:
    metric_id: str
    value: float
    unit: str
    threshold: float | None
    comparison: str
    source_gate: str

    @property
    def passes(self) -> bool | None:
        if self.threshold is None:
            return None
        if self.comparison == "at_least":
            return self.value >= self.threshold
        if self.comparison == "at_most":
            return self.value <= self.threshold
        if self.comparison == "equals":
            return self.value == self.threshold
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "metric_id": self.metric_id,
            "value": self.value,
            "unit": self.unit,
            "threshold": self.threshold,
            "comparison": self.comparison,
            "source_gate": self.source_gate,
            "passes": self.passes,
        }


@dataclass(frozen=True, slots=True)
class ReleaseBlocker:
    blocker_id: str
    gate_id: str
    code: str
    summary: str
    detail: str
    capability: str
    location: str
    remediation_owner: str
    evidence_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocker_id": self.blocker_id,
            "gate_id": self.gate_id,
            "code": self.code,
            "summary": self.summary,
            "detail": self.detail,
            "capability": self.capability,
            "location": self.location,
            "remediation_owner": self.remediation_owner,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(slots=True)
class ReleaseReport:
    report_id: str
    generated_at: str
    baseline_commit: str
    implementation_commit: str
    accepted: bool
    decision: str
    gate_status: Mapping[str, str]
    scenario_status: Mapping[str, str]
    metrics: list[ReleaseMetric]
    blockers: list[ReleaseBlocker]
    warnings: list[Mapping[str, Any]]
    artifacts: list[str]
    limitations: list[str]
    state_custody: Mapping[str, str]
    source_roles: Mapping[str, str]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.m1-release-report/v1",
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "baseline_commit": self.baseline_commit,
            "implementation_commit": self.implementation_commit,
            "accepted": self.accepted,
            "decision": self.decision,
            "gate_status": dict(sorted(self.gate_status.items())),
            "scenario_status": dict(sorted(self.scenario_status.items())),
            "metrics": [item.to_dict() for item in self.metrics],
            "blockers": [item.to_dict() for item in self.blockers],
            "warnings": [dict(item) for item in self.warnings],
            "artifacts": list(self.artifacts),
            "limitations": list(self.limitations),
            "state_custody": dict(sorted(self.state_custody.items())),
            "source_roles": dict(sorted(self.source_roles.items())),
            "metadata": dict(self.metadata),
        }
        if include_digest:
            value["content_digest"] = stable_digest(value)
        return value


class ReleaseReportBuilder:
    def build(self, outcome: IntegrationOutcome) -> ReleaseReport:
        gates = {gate.gate_id: gate for gate in outcome.gates}
        exit_gate = gates.get("m1-exit")
        decision = str((exit_gate.metrics if exit_gate else {}).get("decision") or "incomplete")
        scenario_gate = gates.get("m1-integration-scenarios")
        scenario_status = dict((scenario_gate.metrics if scenario_gate else {}).get("scenario_status") or {})
        blockers = self._blockers(outcome.gates)
        warnings = self._warnings(outcome.gates)
        custody = self._state_custody(outcome)
        source_roles = self._source_roles(outcome)
        return ReleaseReport(
            report_id=f"release-{outcome.run_id}",
            generated_at=utc_now(),
            baseline_commit=outcome.baseline_commit,
            implementation_commit=outcome.implementation_commit,
            accepted=outcome.accepted and decision == "ready_for_m2",
            decision=decision,
            gate_status={gate.gate_id: gate.status.value for gate in outcome.gates},
            scenario_status=scenario_status,
            metrics=self._metrics(gates),
            blockers=blockers,
            warnings=warnings,
            artifacts=list(outcome.artifact_paths),
            limitations=list(outcome.limitations),
            state_custody=custody,
            source_roles=source_roles,
            metadata={
                "integration_outcome_digest": outcome.to_dict()["content_digest"],
                "base_audit_count": len(outcome.base_audits),
                "scenario_evidence_count": len(outcome.scenario_evidence),
            },
        )

    @staticmethod
    def _blockers(gates: Sequence[GateResult]) -> list[ReleaseBlocker]:
        values: list[ReleaseBlocker] = []
        seen: set[str] = set()
        for gate in gates:
            refs = tuple(item.location for item in gate.evidence if item.location)
            for finding in gate.findings:
                if finding.severity is not Severity.BLOCKER:
                    continue
                identity = stable_digest(
                    {
                        "gate": gate.gate_id,
                        "code": finding.code,
                        "detail": finding.detail,
                        "capability": finding.capability,
                    }
                )[:20]
                if identity in seen:
                    continue
                seen.add(identity)
                values.append(
                    ReleaseBlocker(
                        blocker_id=f"blocker-{identity}",
                        gate_id=gate.gate_id,
                        code=finding.code,
                        summary=finding.summary,
                        detail=finding.detail,
                        capability=finding.capability,
                        location=finding.location,
                        remediation_owner=ReleaseReportBuilder._remediation_owner(gate.gate_id, finding),
                        evidence_refs=refs,
                    )
                )
        return sorted(values, key=lambda item: (item.gate_id, item.code, item.blocker_id))

    @staticmethod
    def _warnings(gates: Sequence[GateResult]) -> list[Mapping[str, Any]]:
        values: list[Mapping[str, Any]] = []
        for gate in gates:
            for finding in gate.findings:
                if finding.severity is Severity.WARNING:
                    values.append(
                        {
                            "gate_id": gate.gate_id,
                            "code": finding.code,
                            "summary": finding.summary,
                            "detail": finding.detail,
                            "capability": finding.capability,
                            "location": finding.location,
                        }
                    )
        return values

    @staticmethod
    def _metrics(gates: Mapping[str, GateResult]) -> list[ReleaseMetric]:
        specs = (
            ("effective_actions", "m1-long-horizon-benchmark", "effective_action_count", 1_000, "at_least", "actions"),
            ("effective_transitions", "m1-long-horizon-benchmark", "effective_transition_count", 2_000, "at_least", "transitions"),
            ("integration_scenarios", "m1-integration-scenarios", "passed_count", 6, "at_least", "scenarios"),
            ("owner_disable_capabilities", "m1-owner-matrix", "executed_probe_count", 17, "at_least", "capabilities"),
            ("real_execution_tiers", "m1-live-execution-tiers", "tier_counts", 3, "at_least", "tiers"),
            ("provider_wires", "m1-live-provider-wires", "observation_count", 2, "at_least", "wires"),
            ("provider_models", "m1-live-provider-wires", "models", 2, "at_least", "models"),
            ("parent_effective_lines", "m1-exit", "parent_effective_lines", 16_000, "at_least", "lines"),
            ("human_interventions", "m1-long-horizon-benchmark", "human_intervention_count", 0, "at_most", "actions"),
        )
        values: list[ReleaseMetric] = []
        for metric_id, gate_id, key, threshold, comparison, unit in specs:
            gate = gates.get(gate_id)
            raw: Any = (gate.metrics if gate else {}).get(key)
            if isinstance(raw, Mapping):
                value = float(len(raw))
            elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
                value = float(len(raw))
            else:
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = 0.0
            values.append(
                ReleaseMetric(
                    metric_id=metric_id,
                    value=value,
                    unit=unit,
                    threshold=float(threshold),
                    comparison=comparison,
                    source_gate=gate_id,
                )
            )
        return values

    @staticmethod
    def _state_custody(outcome: IntegrationOutcome) -> dict[str, str]:
        custody: dict[str, str] = {}
        for audit in outcome.base_audits:
            gate = audit.report.gate("m1-state-custody")
            if gate is None:
                continue
            for entry in gate.metrics.get("entries") or ():
                if not isinstance(entry, Mapping):
                    continue
                family = str(entry.get("state_family") or "")
                owner = str(entry.get("canonical_owner") or "")
                if family and owner:
                    custody.setdefault(family, owner)
        return custody

    @staticmethod
    def _source_roles(outcome: IntegrationOutcome) -> dict[str, str]:
        roles: dict[str, str] = {}
        if outcome.handoff:
            for chain in outcome.handoff.source_chains:
                roles[chain.chain_id] = chain.source_role + ":" + chain.final_decision
        return roles

    @staticmethod
    def _remediation_owner(gate_id: str, finding: Finding) -> str:
        if finding.capability:
            return finding.capability
        prefixes = (
            ("scenario", "M1IntegrationScenarioSuite"),
            ("provider", "ProviderControlPlane"),
            ("live", "WorkerPool/ProviderControlPlane"),
            ("cleanroom", "M1 packaging"),
            ("handoff", "M1 hardening integration"),
            ("benchmark", "M1 evaluation"),
            ("langgraph", "GraphStateCustody"),
            ("permission", "PermissionRuntime"),
            ("memory", "MemoryRuntime"),
        )
        for prefix, owner in prefixes:
            if gate_id.startswith(prefix) or finding.code.startswith(prefix):
                return owner
        return gate_id


class ReleaseMarkdownRenderer:
    def render(self, report: ReleaseReport) -> str:
        lines = [
            "# M1 main-path hardening release report",
            "",
            f"Generated: `{report.generated_at}`",
            "",
            f"Decision: **{self._escape(report.decision)}**",
            "",
            f"Implementation commit: `{self._escape(report.implementation_commit or 'pending')}`",
            "",
            "## Gate summary",
            "",
            "| Gate | Status |",
            "| --- | --- |",
        ]
        for gate_id, status in sorted(report.gate_status.items()):
            lines.append(f"| `{self._escape(gate_id)}` | `{self._escape(status)}` |")
        lines.extend(
            [
                "",
                "## Scenario summary",
                "",
                "| Scenario | Status |",
                "| --- | --- |",
            ]
        )
        for scenario_id, status in sorted(report.scenario_status.items()):
            lines.append(f"| `{self._escape(scenario_id)}` | `{self._escape(status)}` |")
        lines.extend(
            [
                "",
                "## Quantitative gates",
                "",
                "| Metric | Observed | Requirement | Result |",
                "| --- | ---: | --- | --- |",
            ]
        )
        for metric in report.metrics:
            requirement = (
                "n/a"
                if metric.threshold is None
                else f"{metric.comparison} {metric.threshold:g} {metric.unit}"
            )
            passed = "n/a" if metric.passes is None else "pass" if metric.passes else "fail"
            lines.append(
                f"| `{self._escape(metric.metric_id)}` | {metric.value:g} {self._escape(metric.unit)} "
                f"| {self._escape(requirement)} | **{passed}** |"
            )
        lines.extend(["", "## Canonical state custody", ""])
        if report.state_custody:
            lines.extend(["| State family | Canonical owner |", "| --- | --- |"])
            for family, owner in sorted(report.state_custody.items()):
                lines.append(f"| `{self._escape(family)}` | `{self._escape(owner)}` |")
        else:
            lines.append("No executed custody evidence was admitted.")
        lines.extend(["", "## Source-role decisions", ""])
        if report.source_roles:
            for chain, role in sorted(report.source_roles.items()):
                lines.append(f"- `{self._escape(chain)}`: `{self._escape(role)}`")
        else:
            lines.append("No source-chain handoff was produced.")
        lines.extend(["", "## Blockers", ""])
        if report.blockers:
            for blocker in report.blockers:
                detail = f" — {self._escape(blocker.detail)}" if blocker.detail else ""
                lines.append(
                    f"- `{self._escape(blocker.gate_id)}/{self._escape(blocker.code)}`: "
                    f"{self._escape(blocker.summary)}{detail} "
                    f"(owner: `{self._escape(blocker.remediation_owner)}`)"
                )
        else:
            lines.append("No blockers.")
        if report.limitations:
            lines.extend(["", "## Limitations", ""])
            lines.extend(f"- {self._escape(item)}" for item in report.limitations)
        lines.extend(["", f"Report digest: `{report.to_dict()['content_digest']}`", ""])
        return "\n".join(lines)

    @staticmethod
    def _escape(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")


@dataclass(frozen=True, slots=True)
class ReleaseChange:
    category: str
    key: str
    before: Any
    after: Any
    regression: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "key": self.key,
            "before": self.before,
            "after": self.after,
            "regression": self.regression,
        }


class ReleaseReportComparator:
    STATUS_ORDER = {"not_run": 0, "partial": 1, "blocked": 1, "passed": 2}

    def compare(self, before: ReleaseReport, after: ReleaseReport) -> list[ReleaseChange]:
        changes: list[ReleaseChange] = []
        keys = set(before.gate_status) | set(after.gate_status)
        for key in sorted(keys):
            left = before.gate_status.get(key, "missing")
            right = after.gate_status.get(key, "missing")
            if left != right:
                regression = self.STATUS_ORDER.get(right, -1) < self.STATUS_ORDER.get(left, -1)
                changes.append(ReleaseChange("gate", key, left, right, regression))
        keys = set(before.scenario_status) | set(after.scenario_status)
        for key in sorted(keys):
            left = before.scenario_status.get(key, "missing")
            right = after.scenario_status.get(key, "missing")
            if left != right:
                regression = self.STATUS_ORDER.get(right, -1) < self.STATUS_ORDER.get(left, -1)
                changes.append(ReleaseChange("scenario", key, left, right, regression))
        before_metrics = {item.metric_id: item for item in before.metrics}
        after_metrics = {item.metric_id: item for item in after.metrics}
        for key in sorted(set(before_metrics) | set(after_metrics)):
            left = before_metrics.get(key)
            right = after_metrics.get(key)
            left_value = left.value if left else None
            right_value = right.value if right else None
            if left_value == right_value:
                continue
            regression = False
            metric = right or left
            if metric and left_value is not None and right_value is not None:
                if metric.comparison == "at_least":
                    regression = right_value < left_value
                elif metric.comparison == "at_most":
                    regression = right_value > left_value
            changes.append(ReleaseChange("metric", key, left_value, right_value, regression))
        if len(after.blockers) != len(before.blockers):
            changes.append(
                ReleaseChange(
                    "blockers",
                    "count",
                    len(before.blockers),
                    len(after.blockers),
                    len(after.blockers) > len(before.blockers),
                )
            )
        return changes


class ReleaseReportStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(self, report: ReleaseReport) -> Mapping[str, str]:
        payload = report.to_dict()
        json_path = self.root / f"{report.report_id}.json"
        markdown_path = self.root / f"{report.report_id}.md"
        self._atomic(json_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        self._atomic(markdown_path, ReleaseMarkdownRenderer().render(report))
        return {
            "json": str(json_path),
            "markdown": str(markdown_path),
            "content_digest": str(payload["content_digest"]),
        }

    @staticmethod
    def _atomic(path: Path, content: str) -> None:
        if path.exists():
            existing = path.read_text(encoding="utf-8")
            if existing == content:
                return
            raise RuntimeError(f"immutable release report already exists: {path.name}")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
