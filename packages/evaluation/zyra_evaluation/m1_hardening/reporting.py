from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contracts import GateResult, GateStatus, HardeningReport, Severity


@dataclass(frozen=True, slots=True)
class GatePolicy:
    gate_id: str
    required_for_foundation: bool = True
    required_for_final: bool = False
    allow_partial_foundation: bool = False
    allow_partial_final: bool = False
    warning_budget: int = 0
    owner: str = "evaluation"
    rationale: str = ""


@dataclass(frozen=True, slots=True)
class GateDisposition:
    gate_id: str
    status: str
    required: bool
    accepted: bool
    blocker_count: int
    error_count: int
    warning_count: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "status": self.status,
            "required": self.required,
            "accepted": self.accepted,
            "blocker_count": self.blocker_count,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "reason": self.reason,
        }


@dataclass(slots=True)
class ReportDisposition:
    accepted: bool
    final_completion: bool
    gates: list[GateDisposition] = field(default_factory=list)
    missing_required_gates: list[str] = field(default_factory=list)
    unknown_gates: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "final_completion": self.final_completion,
            "missing_required_gates": list(self.missing_required_gates),
            "unknown_gates": list(self.unknown_gates),
            "reasons": list(self.reasons),
            "gates": [item.to_dict() for item in self.gates],
        }


class GatePolicyEngine:
    def __init__(self, policies: Iterable[GatePolicy]) -> None:
        self._policies: dict[str, GatePolicy] = {}
        for policy in policies:
            if not policy.gate_id.strip():
                raise ValueError("gate policy id must not be empty")
            if policy.gate_id in self._policies:
                raise ValueError(f"duplicate gate policy: {policy.gate_id}")
            if policy.warning_budget < 0:
                raise ValueError(f"negative warning budget: {policy.gate_id}")
            self._policies[policy.gate_id] = policy

    def evaluate(self, report: HardeningReport, *, final_completion: bool = False) -> ReportDisposition:
        by_id = {gate.gate_id: gate for gate in report.gates}
        duplicate_ids = self._duplicates(gate.gate_id for gate in report.gates)
        required = {
            gate_id
            for gate_id, policy in self._policies.items()
            if (policy.required_for_final if final_completion else policy.required_for_foundation)
        }
        disposition = ReportDisposition(
            accepted=True,
            final_completion=final_completion,
            missing_required_gates=sorted(required - set(by_id)),
            unknown_gates=sorted(set(by_id) - set(self._policies)),
        )
        if duplicate_ids:
            disposition.accepted = False
            disposition.reasons.append("duplicate gate ids: " + ", ".join(duplicate_ids))
        if disposition.missing_required_gates:
            disposition.accepted = False
            disposition.reasons.append("missing required gates: " + ", ".join(disposition.missing_required_gates))
        for gate in report.gates:
            policy = self._policies.get(gate.gate_id)
            if policy is None:
                disposition.gates.append(
                    GateDisposition(
                        gate_id=gate.gate_id,
                        status=gate.status.value,
                        required=False,
                        accepted=False,
                        blocker_count=gate.blocker_count,
                        error_count=gate.error_count,
                        warning_count=gate.warning_count,
                        reason="gate has no adjudicated policy",
                    )
                )
                disposition.accepted = False
                continue
            gate_required = policy.required_for_final if final_completion else policy.required_for_foundation
            allow_partial = policy.allow_partial_final if final_completion else policy.allow_partial_foundation
            accepted, reason = self._accept_gate(gate, policy, required=gate_required, allow_partial=allow_partial)
            disposition.gates.append(
                GateDisposition(
                    gate_id=gate.gate_id,
                    status=gate.status.value,
                    required=gate_required,
                    accepted=accepted,
                    blocker_count=gate.blocker_count,
                    error_count=gate.error_count,
                    warning_count=gate.warning_count,
                    reason=reason,
                )
            )
            if gate_required and not accepted:
                disposition.accepted = False
                disposition.reasons.append(f"{gate.gate_id}: {reason}")
        if not disposition.reasons and disposition.accepted:
            disposition.reasons.append("all required gates satisfy the selected policy")
        return disposition

    @staticmethod
    def _accept_gate(
        gate: GateResult,
        policy: GatePolicy,
        *,
        required: bool,
        allow_partial: bool,
    ) -> tuple[bool, str]:
        if gate.blocker_count:
            return False, f"{gate.blocker_count} blocker finding(s)"
        if gate.error_count:
            return False, f"{gate.error_count} error finding(s)"
        if gate.status is GateStatus.PASSED:
            if gate.warning_count > policy.warning_budget:
                return False, f"warning budget exceeded ({gate.warning_count}>{policy.warning_budget})"
            return True, "passed"
        if gate.status is GateStatus.PARTIAL and allow_partial:
            if gate.warning_count > policy.warning_budget:
                return False, f"partial gate warning budget exceeded ({gate.warning_count}>{policy.warning_budget})"
            return True, "partial explicitly accepted for this phase"
        if not required and gate.status in {GateStatus.NOT_RUN, GateStatus.PARTIAL}:
            return True, "optional gate is not required in this phase"
        return False, f"status {gate.status.value} is not accepted"

    @staticmethod
    def _duplicates(values: Iterable[str]) -> list[str]:
        counts = Counter(values)
        return sorted(value for value, count in counts.items() if count > 1)


class EvidenceLinkAuditor:
    def evaluate(self, report: HardeningReport) -> dict[str, Any]:
        gate_ids = {gate.gate_id for gate in report.gates}
        locations: Counter[str] = Counter()
        kinds: Counter[str] = Counter()
        empty_locations: list[dict[str, str]] = []
        duplicate_locations: list[dict[str, Any]] = []
        finding_without_evidence: list[dict[str, str]] = []
        for gate in report.gates:
            for pointer in gate.evidence:
                kinds[pointer.kind] += 1
                if pointer.location.strip():
                    locations[pointer.location] += 1
                else:
                    empty_locations.append({"gate_id": gate.gate_id, "kind": pointer.kind})
            material = [item for item in gate.findings if item.severity in {Severity.BLOCKER, Severity.ERROR}]
            if material and not gate.evidence:
                finding_without_evidence.append(
                    {
                        "gate_id": gate.gate_id,
                        "finding_count": str(len(material)),
                    }
                )
        for location, count in sorted(locations.items()):
            if count > 1:
                duplicate_locations.append({"location": location, "reference_count": count})
        scenario_gate = next((gate for gate in report.gates if gate.gate_id.startswith("scenario:")), None)
        scenario_linked = bool(scenario_gate and (scenario_gate.evidence or scenario_gate.metrics))
        custody_gate = report.gate("m1-state-custody")
        custody_linked = bool(custody_gate and custody_gate.metrics.get("entries"))
        return {
            "gate_count": len(gate_ids),
            "evidence_count": sum(kinds.values()),
            "evidence_kinds": dict(sorted(kinds.items())),
            "unique_locations": len(locations),
            "empty_locations": empty_locations,
            "duplicate_locations": duplicate_locations,
            "material_findings_without_evidence": finding_without_evidence,
            "scenario_linked": scenario_linked,
            "custody_linked": custody_linked,
            "complete": not empty_locations and not finding_without_evidence and scenario_linked and custody_linked,
        }


class ReportNormalizer:
    _VOLATILE_KEYS = {
        "started_at",
        "completed_at",
        "generated_at",
        "duration_ms",
        "elapsed_ms",
        "wall_clock_ms",
    }

    def normalize(self, value: Any, *, retain_timestamps: bool = False) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key in sorted(str(item) for item in value):
                if not retain_timestamps and key in self._VOLATILE_KEYS:
                    continue
                result[key] = self.normalize(value[key], retain_timestamps=retain_timestamps)
            return result
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            normalized = [self.normalize(item, retain_timestamps=retain_timestamps) for item in value]
            if self._order_insensitive(normalized):
                return sorted(normalized, key=self._stable_sort_key)
            return normalized
        if isinstance(value, float):
            if math.isnan(value) or math.isinf(value):
                return str(value)
            return round(value, 9)
        return value

    def digest(self, value: Any, *, retain_timestamps: bool = False) -> str:
        normalized = self.normalize(value, retain_timestamps=retain_timestamps)
        encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _order_insensitive(values: Sequence[Any]) -> bool:
        if not values:
            return False
        if all(isinstance(item, Mapping) and "gate_id" in item for item in values):
            return True
        if all(isinstance(item, Mapping) and "code" in item for item in values):
            return True
        if all(isinstance(item, Mapping) and "capability_id" in item for item in values):
            return True
        return False

    @staticmethod
    def _stable_sort_key(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass(frozen=True, slots=True)
class MetricChange:
    gate_id: str
    metric_path: str
    before: Any
    after: Any
    direction: str
    material: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate_id": self.gate_id,
            "metric_path": self.metric_path,
            "before": self.before,
            "after": self.after,
            "direction": self.direction,
            "material": self.material,
        }


class ReportComparator:
    def compare(self, before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
        before_gates = self._gates(before)
        after_gates = self._gates(after)
        added = sorted(set(after_gates) - set(before_gates))
        removed = sorted(set(before_gates) - set(after_gates))
        status_changes: list[dict[str, str]] = []
        finding_changes: list[dict[str, Any]] = []
        metric_changes: list[MetricChange] = []
        for gate_id in sorted(set(before_gates) & set(after_gates)):
            old = before_gates[gate_id]
            new = after_gates[gate_id]
            old_status = str(old.get("status") or "")
            new_status = str(new.get("status") or "")
            if old_status != new_status:
                status_changes.append(
                    {
                        "gate_id": gate_id,
                        "before": old_status,
                        "after": new_status,
                        "direction": self._status_direction(old_status, new_status),
                    }
                )
            old_findings = self._finding_keys(old)
            new_findings = self._finding_keys(new)
            if old_findings != new_findings:
                finding_changes.append(
                    {
                        "gate_id": gate_id,
                        "added": sorted(new_findings - old_findings),
                        "resolved": sorted(old_findings - new_findings),
                    }
                )
            metric_changes.extend(
                self._metric_changes(
                    gate_id,
                    old.get("metrics") if isinstance(old.get("metrics"), Mapping) else {},
                    new.get("metrics") if isinstance(new.get("metrics"), Mapping) else {},
                )
            )
        regressions = [item for item in status_changes if item["direction"] == "regression"]
        regressions.extend(
            {
                "gate_id": item["gate_id"],
                "direction": "regression",
                "added_findings": item["added"],
            }
            for item in finding_changes
            if item["added"]
        )
        return {
            "before_report_id": before.get("report_id", ""),
            "after_report_id": after.get("report_id", ""),
            "added_gates": added,
            "removed_gates": removed,
            "status_changes": status_changes,
            "finding_changes": finding_changes,
            "metric_changes": [item.to_dict() for item in metric_changes],
            "regressions": regressions,
            "regression_count": len(regressions) + len(removed),
            "compatible": not regressions and not removed,
        }

    @staticmethod
    def _gates(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
        gates = report.get("gates")
        if not isinstance(gates, Sequence):
            return {}
        result: dict[str, Mapping[str, Any]] = {}
        for value in gates:
            if not isinstance(value, Mapping):
                continue
            gate_id = str(value.get("gate_id") or "")
            if gate_id:
                result[gate_id] = value
        return result

    @staticmethod
    def _finding_keys(gate: Mapping[str, Any]) -> set[str]:
        values = gate.get("findings")
        if not isinstance(values, Sequence):
            return set()
        result: set[str] = set()
        for item in values:
            if not isinstance(item, Mapping):
                continue
            severity = str(item.get("severity") or "")
            code = str(item.get("code") or "")
            capability = str(item.get("capability") or "")
            if code:
                result.add(f"{severity}:{code}:{capability}")
        return result

    def _metric_changes(
        self,
        gate_id: str,
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> list[MetricChange]:
        old = self._flatten_numeric(before)
        new = self._flatten_numeric(after)
        changes: list[MetricChange] = []
        for path in sorted(set(old) & set(new)):
            if old[path] == new[path]:
                continue
            delta = new[path] - old[path]
            scale = max(abs(old[path]), 1.0)
            changes.append(
                MetricChange(
                    gate_id=gate_id,
                    metric_path=path,
                    before=old[path],
                    after=new[path],
                    direction="increase" if delta > 0 else "decrease",
                    material=abs(delta) / scale >= 0.05,
                )
            )
        return changes

    def _flatten_numeric(self, value: Mapping[str, Any], prefix: str = "") -> dict[str, float]:
        result: dict[str, float] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(item, bool):
                result[path] = float(item)
            elif isinstance(item, (int, float)):
                result[path] = float(item)
            elif isinstance(item, Mapping):
                result.update(self._flatten_numeric(item, path))
        return result

    @staticmethod
    def _status_direction(before: str, after: str) -> str:
        rank = {
            "passed": 5,
            "partial": 4,
            "not_run": 3,
            "skipped": 3,
            "failed": 2,
            "blocked": 1,
        }
        old = rank.get(before, 0)
        new = rank.get(after, 0)
        if new > old:
            return "improvement"
        if new < old:
            return "regression"
        return "changed"


class HardeningMarkdownRenderer:
    def render(
        self,
        report: HardeningReport,
        *,
        disposition: ReportDisposition | None = None,
        evidence_audit: Mapping[str, Any] | None = None,
    ) -> str:
        lines = [
            f"# M1 Hardening Report `{report.report_id}`",
            "",
            f"- Generated: `{report.generated_at}`",
            f"- Baseline: `{report.baseline_commit}`",
            f"- Scenario: `{report.scenario_id or 'static-audit'}`",
            f"- Task: `{report.task_id or 'none'}`",
            f"- Result: `{'accepted' if disposition and disposition.accepted else 'not accepted'}`",
            f"- Blockers: `{report.blockers}`",
            f"- Failed gates: `{report.failures}`",
            "",
            "## Gate summary",
            "",
            "| Gate | Status | Blockers | Errors | Warnings | Summary |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
        for gate in report.gates:
            lines.append(
                "| "
                + " | ".join(
                    (
                        self._escape(gate.gate_id),
                        gate.status.value,
                        str(gate.blocker_count),
                        str(gate.error_count),
                        str(gate.warning_count),
                        self._escape(gate.summary),
                    )
                )
                + " |"
            )
        if disposition:
            lines.extend(self._render_disposition(disposition))
        lines.extend(self._render_findings(report.gates))
        lines.extend(self._render_limitations(report.gates))
        lines.extend(self._render_evidence(report.gates))
        lines.extend(self._render_metrics(report.gates))
        if evidence_audit:
            lines.extend(
                [
                    "",
                    "## Evidence-link audit",
                    "",
                    "```json",
                    json.dumps(evidence_audit, indent=2, ensure_ascii=False, sort_keys=True),
                    "```",
                ]
            )
        lines.extend(
            [
                "",
                "## Phase boundary",
                "",
                "This report evaluates the M1-S08-01 foundation contract. It does not by itself close the M1-08 parent unit, real edge/cloud dispatch, the two-live-provider requirement, or the sealed 2,000-transition final gate.",
                "",
            ]
        )
        return "\n".join(lines)

    def _render_disposition(self, disposition: ReportDisposition) -> list[str]:
        lines = [
            "",
            "## Policy disposition",
            "",
            "| Gate | Required | Accepted | Reason |",
            "| --- | --- | --- | --- |",
        ]
        for item in disposition.gates:
            lines.append(
                f"| {self._escape(item.gate_id)} | {str(item.required).lower()} | "
                f"{str(item.accepted).lower()} | {self._escape(item.reason)} |"
            )
        if disposition.missing_required_gates:
            lines.extend(["", "Missing required gates: " + ", ".join(disposition.missing_required_gates)])
        if disposition.unknown_gates:
            lines.extend(["", "Unknown gates: " + ", ".join(disposition.unknown_gates)])
        return lines

    def _render_findings(self, gates: Sequence[GateResult]) -> list[str]:
        rows: list[str] = []
        for gate in gates:
            for finding in gate.findings:
                rows.append(
                    "| "
                    + " | ".join(
                        (
                            self._escape(gate.gate_id),
                            finding.severity.value,
                            self._escape(finding.code),
                            self._escape(finding.capability),
                            self._escape(finding.summary),
                            self._escape(finding.location),
                        )
                    )
                    + " |"
                )
        lines = [
            "",
            "## Findings",
            "",
            "| Gate | Severity | Code | Capability | Summary | Location |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        lines.extend(rows or ["| - | - | - | - | No findings | - |"])
        return lines

    def _render_limitations(self, gates: Sequence[GateResult]) -> list[str]:
        rows = [
            f"- `{gate.gate_id}`: {limitation}"
            for gate in gates
            for limitation in gate.limitations
        ]
        return ["", "## Limitations", "", *(rows or ["- None declared."])]

    def _render_evidence(self, gates: Sequence[GateResult]) -> list[str]:
        rows = [
            f"| {self._escape(gate.gate_id)} | {self._escape(pointer.kind)} | "
            f"{self._escape(pointer.location)} | {self._escape(pointer.summary)} |"
            for gate in gates
            for pointer in gate.evidence
        ]
        return [
            "",
            "## Evidence pointers",
            "",
            "| Gate | Kind | Location | Summary |",
            "| --- | --- | --- | --- |",
            *(rows or ["| - | - | - | No evidence pointers |"]),
        ]

    def _render_metrics(self, gates: Sequence[GateResult]) -> list[str]:
        lines = ["", "## Machine metrics", ""]
        for gate in gates:
            lines.extend(
                [
                    f"### `{gate.gate_id}`",
                    "",
                    "```json",
                    json.dumps(gate.metrics, indent=2, ensure_ascii=False, sort_keys=True),
                    "```",
                    "",
                ]
            )
        return lines

    @staticmethod
    def _escape(value: Any) -> str:
        text = str(value or "").replace("\n", " ").replace("\r", " ")
        text = re.sub(r"\s+", " ", text).strip()
        return text.replace("|", "\\|")


def default_gate_policies() -> tuple[GatePolicy, ...]:
    return (
        GatePolicy("source-to-target-coverage", warning_budget=64, owner="source-custody"),
        GatePolicy("m1-internalization", warning_budget=64, owner="cleanroom"),
        GatePolicy("m1-state-custody", warning_budget=64, owner="state-custody"),
        GatePolicy("langgraph-boundary", warning_budget=4, owner="graph-custody"),
        GatePolicy("scenario:m1-foundation-query-permission-control", warning_budget=4, owner="evaluation"),
        GatePolicy("long-horizon-progress", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="evaluation"),
        GatePolicy("causal-evidence", allow_partial_foundation=True, required_for_final=True, warning_budget=16, owner="event-store"),
        GatePolicy("low-entropy", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="message-runtime"),
        GatePolicy("sealed-autonomy", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="permission-runtime"),
        GatePolicy("execution-tiers", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="backend-registry"),
        GatePolicy("provider-control-plane", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="provider-runtime"),
        GatePolicy("patch-git", warning_budget=8, owner="workspace-runtime"),
        GatePolicy("deny-policy", warning_budget=8, owner="permission-runtime"),
        GatePolicy("secrets-prompt-injection", warning_budget=8, owner="security-runtime"),
        GatePolicy("code-index", warning_budget=8, owner="code-index"),
        GatePolicy("disable-module-probe", allow_partial_foundation=True, required_for_final=True, warning_budget=8, owner="evaluation"),
        GatePolicy("effective-line-audit", warning_budget=8, owner="evaluation"),
    )
