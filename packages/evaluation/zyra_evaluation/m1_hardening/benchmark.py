from __future__ import annotations

import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from .autonomy import SealedAutonomyGate
from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity
from .entropy import LowEntropyGate
from .evidence_graph import CausalEvidenceGraphGate
from .integration_contracts import canonical_revision, stable_digest
from .progress import LongHorizonProgressLedger


class SemanticFamily(StrEnum):
    STATE = "state"
    ROUTE = "route"
    PLACEMENT = "placement"
    TOOL = "tool"
    VERIFICATION = "verification"
    PERMISSION = "permission"
    COMPACT_RESTORE = "compact_restore"
    FAULT_RECOVERY = "fault_recovery"
    ARTIFACT = "artifact"
    TOPOLOGY = "topology"
    MEMORY = "memory"


EXCLUDED_EVENT_TOKENS = (
    "heartbeat",
    "health",
    "log",
    "trace_replay",
    "projector",
    "projection_refresh",
    "ui_repaint",
    "no_op",
    "noop",
    "poll",
    "snapshot_read",
)


@dataclass(frozen=True, slots=True)
class CanonicalTransition:
    event_id: str
    run_id: str
    task_id: str
    event_type: str
    semantic_family: SemanticFamily
    semantic_key: str
    before_revision: int
    after_revision: int
    before_digest: str
    after_digest: str
    action_id: str
    causation_id: str
    parent_event_id: str
    sequence: int
    payload_digest: str
    effective: bool
    exclusion_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "event_type": self.event_type,
            "semantic_family": self.semantic_family.value,
            "semantic_key": self.semantic_key,
            "before_revision": self.before_revision,
            "after_revision": self.after_revision,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "action_id": self.action_id,
            "causation_id": self.causation_id,
            "parent_event_id": self.parent_event_id,
            "sequence": self.sequence,
            "payload_digest": self.payload_digest,
            "effective": self.effective,
            "exclusion_reason": self.exclusion_reason,
        }


@dataclass(frozen=True, slots=True)
class CanonicalAction:
    action_id: str
    run_id: str
    task_id: str
    risk: str
    policy_effect: str
    transition_ids: tuple[str, ...]
    semantic_families: tuple[str, ...]
    causation_ids: tuple[str, ...]
    effective: bool
    completed: bool
    human_intervention: bool
    manual_resume: bool
    state_edit: bool
    unresolved_approval: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "risk": self.risk,
            "policy_effect": self.policy_effect,
            "transition_ids": list(self.transition_ids),
            "semantic_families": list(self.semantic_families),
            "causation_ids": list(self.causation_ids),
            "effective": self.effective,
            "completed": self.completed,
            "human_intervention": self.human_intervention,
            "manual_resume": self.manual_resume,
            "state_edit": self.state_edit,
            "unresolved_approval": self.unresolved_approval,
        }


@dataclass(frozen=True, slots=True)
class BenchmarkThresholds:
    minimum_effective_actions: int = 1_000
    minimum_effective_transitions: int = 2_000
    minimum_semantic_families: int = 7
    minimum_requirement_changes: int = 1
    minimum_fault_recoveries: int = 1
    minimum_topology_mutations: int = 1
    maximum_human_interventions: int = 0
    maximum_manual_resumes: int = 0
    maximum_state_edits: int = 0
    maximum_unresolved_approvals: int = 0
    maximum_duplicate_ratio: float = 0.01
    maximum_excluded_ratio: float = 0.35


@dataclass(slots=True)
class BenchmarkSnapshot:
    run_id: str
    transitions: list[CanonicalTransition]
    actions: list[CanonicalAction]
    exclusions: Counter[str]
    duplicates: int
    revision_gaps: list[tuple[int, int, str]]
    requirement_changes: int
    fault_recoveries: int
    topology_mutations: int
    omp_effects: dict[str, bool]

    @property
    def effective_transitions(self) -> list[CanonicalTransition]:
        return [item for item in self.transitions if item.effective]

    @property
    def effective_actions(self) -> list[CanonicalAction]:
        return [item for item in self.actions if item.effective and item.completed]

    def to_dict(self, *, include_records: bool = False) -> dict[str, Any]:
        result = {
            "run_id": self.run_id,
            "transition_count": len(self.transitions),
            "effective_transition_count": len(self.effective_transitions),
            "action_count": len(self.actions),
            "effective_action_count": len(self.effective_actions),
            "exclusions": dict(sorted(self.exclusions.items())),
            "duplicates": self.duplicates,
            "revision_gaps": [list(item) for item in self.revision_gaps],
            "requirement_changes": self.requirement_changes,
            "fault_recoveries": self.fault_recoveries,
            "topology_mutations": self.topology_mutations,
            "omp_effects": dict(self.omp_effects),
            "semantic_family_counts": dict(
                Counter(item.semantic_family.value for item in self.effective_transitions)
            ),
        }
        if include_records:
            result["transitions"] = [item.to_dict() for item in self.transitions]
            result["actions"] = [item.to_dict() for item in self.actions]
        return result


class TransitionAdmission:
    _FAMILY_TOKENS: tuple[tuple[SemanticFamily, tuple[str, ...]], ...] = (
        (SemanticFamily.TOPOLOGY, ("topology", "node_added", "edge_added", "role_changed", "capability_changed")),
        (SemanticFamily.FAULT_RECOVERY, ("fault", "failure", "recover", "retry", "reroute", "resume")),
        (SemanticFamily.COMPACT_RESTORE, ("compact", "restore", "context_restored")),
        (SemanticFamily.PERMISSION, ("permission", "approval", "deny", "allow", "policy")),
        (SemanticFamily.PLACEMENT, ("placement", "lease", "worker_assigned", "dispatch")),
        (SemanticFamily.ROUTE, ("route", "fallback", "failover", "backend")),
        (SemanticFamily.VERIFICATION, ("verify", "test", "validation", "assert")),
        (SemanticFamily.ARTIFACT, ("artifact", "patch", "file_written", "diff")),
        (SemanticFamily.MEMORY, ("memory", "retrieval", "curator", "procedure", "index")),
        (SemanticFamily.TOOL, ("tool", "mcp", "skill", "subagent")),
        (SemanticFamily.STATE, ("state", "task", "requirement", "session", "control")),
    )

    def extract(self, event: Mapping[str, Any], index: int) -> CanonicalTransition:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        event_type = str(event.get("event_type") or event.get("type") or "").lower()
        event_id = str(event.get("event_id") or payload.get("event_id") or "")
        run_id = str(event.get("run_id") or payload.get("run_id") or "")
        task_id = str(event.get("task_id") or payload.get("task_id") or "")
        semantic_family = self._family(event_type, payload)
        semantic_key = str(
            payload.get("semantic_key")
            or payload.get("state_key")
            or payload.get("artifact_id")
            or payload.get("route_id")
            or payload.get("tool_call_id")
            or payload.get("decision_id")
            or event_type
        )
        before_revision = self._revision(payload.get("before_revision"), event.get("before_revision"), index)
        after_revision = self._revision(payload.get("after_revision"), event.get("after_revision"), before_revision)
        before = payload.get("before") if "before" in payload else event.get("before")
        after = payload.get("after") if "after" in payload else event.get("after")
        before_digest = str(payload.get("before_digest") or stable_digest(before))
        after_digest = str(payload.get("after_digest") or stable_digest(after))
        action_id = str(payload.get("action_id") or event.get("action_id") or "")
        causation_id = str(
            event.get("causation_id")
            or payload.get("causation_id")
            or payload.get("request_id")
            or payload.get("command_id")
            or ""
        )
        parent_event_id = str(event.get("parent_event_id") or payload.get("parent_event_id") or "")
        sequence = self._revision(event.get("sequence"), payload.get("sequence"), index)
        exclusion = self._exclusion(
            event_type=event_type,
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            action_id=action_id,
            causation_id=causation_id,
            before_revision=before_revision,
            after_revision=after_revision,
            before_digest=before_digest,
            after_digest=after_digest,
            payload=payload,
        )
        return CanonicalTransition(
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            event_type=event_type,
            semantic_family=semantic_family,
            semantic_key=semantic_key,
            before_revision=before_revision,
            after_revision=after_revision,
            before_digest=before_digest,
            after_digest=after_digest,
            action_id=action_id,
            causation_id=causation_id,
            parent_event_id=parent_event_id,
            sequence=sequence,
            payload_digest=stable_digest(payload),
            effective=not exclusion,
            exclusion_reason=exclusion,
        )

    def _exclusion(
        self,
        *,
        event_type: str,
        event_id: str,
        run_id: str,
        task_id: str,
        action_id: str,
        causation_id: str,
        before_revision: int,
        after_revision: int,
        before_digest: str,
        after_digest: str,
        payload: Mapping[str, Any],
    ) -> str:
        if any(token in event_type for token in EXCLUDED_EVENT_TOKENS):
            return "excluded_event_family"
        if payload.get("noop") is True or payload.get("semantic_mutation") is False:
            return "declared_noop"
        if not event_id:
            return "event_identity_missing"
        if not run_id or not task_id:
            return "partition_identity_missing"
        if not action_id:
            return "action_identity_missing"
        if not causation_id:
            return "causation_missing"
        if after_revision <= before_revision:
            return "revision_not_advanced"
        if before_digest == after_digest:
            return "semantic_value_unchanged"
        if payload.get("fixture") is True or payload.get("replayed") is True:
            return "fixture_or_replay"
        return ""

    def _family(self, event_type: str, payload: Mapping[str, Any]) -> SemanticFamily:
        declared = str(payload.get("semantic_family") or "").lower()
        for family in SemanticFamily:
            if declared == family.value:
                return family
        text = event_type + " " + json.dumps(payload, ensure_ascii=False, default=str).lower()
        for family, tokens in self._FAMILY_TOKENS:
            if any(token in text for token in tokens):
                return family
        return SemanticFamily.STATE

    @staticmethod
    def _revision(*values: Any) -> int:
        for value in values:
            if isinstance(value, bool):
                continue
            try:
                return int(value)
            except (TypeError, ValueError):
                continue
        return 0


class ActionAdmission:
    def group(
        self,
        transitions: Sequence[CanonicalTransition],
        events: Sequence[Mapping[str, Any]],
    ) -> list[CanonicalAction]:
        by_action: dict[str, list[CanonicalTransition]] = defaultdict(list)
        for transition in transitions:
            if transition.action_id:
                by_action[transition.action_id].append(transition)
        event_by_action: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            action_id = str(payload.get("action_id") or event.get("action_id") or "")
            if action_id:
                event_by_action[action_id].append(event)
        actions: list[CanonicalAction] = []
        for action_id, values in sorted(by_action.items()):
            source_events = event_by_action.get(action_id, ())
            text = " ".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).lower()
                for event in source_events
            )
            risks = [self._field(event, "risk") for event in source_events]
            effects = [self._field(event, "policy_effect", "effect", "decision") for event in source_events]
            risk = next((value for value in risks if value), "unknown")
            policy_effect = next((value for value in effects if value), "")
            effective_values = [item for item in values if item.effective]
            completed = self._completed(text, policy_effect, effective_values)
            actions.append(
                CanonicalAction(
                    action_id=action_id,
                    run_id=self._single(item.run_id for item in values),
                    task_id=self._single(item.task_id for item in values),
                    risk=risk,
                    policy_effect=policy_effect,
                    transition_ids=tuple(item.event_id for item in effective_values),
                    semantic_families=tuple(sorted({item.semantic_family.value for item in effective_values})),
                    causation_ids=tuple(sorted({item.causation_id for item in effective_values if item.causation_id})),
                    effective=bool(effective_values),
                    completed=completed,
                    human_intervention=self._truth(text, "human_intervention", "human approval", "user approved"),
                    manual_resume=self._truth(text, "manual_resume", "manual resume"),
                    state_edit=self._truth(text, "manual_state_edit", "state edited by user"),
                    unresolved_approval=self._truth(text, "approval_pending", "unresolved approval"),
                )
            )
        return actions

    @staticmethod
    def _field(event: Mapping[str, Any], *names: str) -> str:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        for name in names:
            value = event.get(name) if name in event else payload.get(name)
            if value is not None and str(value):
                return str(value).lower()
        return ""

    @staticmethod
    def _single(values: Iterable[str]) -> str:
        unique = {value for value in values if value}
        return next(iter(unique)) if len(unique) == 1 else ""

    @staticmethod
    def _completed(text: str, effect: str, transitions: Sequence[CanonicalTransition]) -> bool:
        if any(token in text for token in ("unresolved", "pending_human", "waiting_for_user")):
            return False
        if effect in {"deny", "denied", "ask"}:
            return any(item.semantic_family is SemanticFamily.FAULT_RECOVERY for item in transitions)
        return bool(transitions) and any(
            item.semantic_family in {SemanticFamily.ARTIFACT, SemanticFamily.VERIFICATION, SemanticFamily.STATE}
            for item in transitions
        )

    @staticmethod
    def _truth(text: str, *tokens: str) -> bool:
        return any(token in text for token in tokens)


class BenchmarkAnalyzer:
    def __init__(self) -> None:
        self.transitions = TransitionAdmission()
        self.actions = ActionAdmission()

    def analyze(self, events: Sequence[Mapping[str, Any]], *, run_id: str) -> BenchmarkSnapshot:
        selected = [
            event
            for event in events
            if str(event.get("run_id") or (event.get("payload") or {}).get("run_id") or "") == run_id
        ]
        transitions = [self.transitions.extract(event, index) for index, event in enumerate(selected)]
        exclusions = Counter(item.exclusion_reason for item in transitions if not item.effective)
        duplicates = self._duplicates(transitions)
        revision_gaps = self._revision_gaps(transitions)
        actions = self.actions.group(transitions, selected)
        effective = [item for item in transitions if item.effective]
        requirement_changes = sum(
            1
            for item in effective
            if "requirement" in item.event_type
            or (item.semantic_family is SemanticFamily.STATE and "requirement" in item.semantic_key.lower())
        )
        fault_recoveries = sum(item.semantic_family is SemanticFamily.FAULT_RECOVERY for item in effective)
        topology_mutations = sum(item.semantic_family is SemanticFamily.TOPOLOGY for item in effective)
        return BenchmarkSnapshot(
            run_id=run_id,
            transitions=transitions,
            actions=actions,
            exclusions=exclusions,
            duplicates=duplicates,
            revision_gaps=revision_gaps,
            requirement_changes=requirement_changes,
            fault_recoveries=fault_recoveries,
            topology_mutations=topology_mutations,
            omp_effects=self._omp_effects(selected),
        )

    @staticmethod
    def _duplicates(transitions: Sequence[CanonicalTransition]) -> int:
        seen: set[tuple[str, str, str, str]] = set()
        duplicates = 0
        for item in transitions:
            if not item.effective:
                continue
            key = (item.action_id, item.semantic_family.value, item.semantic_key, item.after_digest)
            if key in seen:
                duplicates += 1
            seen.add(key)
        return duplicates

    @staticmethod
    def _revision_gaps(transitions: Sequence[CanonicalTransition]) -> list[tuple[int, int, str]]:
        values = sorted(
            (item.before_revision, item.after_revision, item.event_id)
            for item in transitions
            if item.effective
        )
        gaps: list[tuple[int, int, str]] = []
        previous_after: int | None = None
        for before, after, event_id in values:
            if previous_after is not None and before > previous_after:
                gaps.append((previous_after, before, event_id))
            previous_after = max(previous_after or after, after)
        return gaps

    @staticmethod
    def _omp_effects(events: Sequence[Mapping[str, Any]]) -> dict[str, bool]:
        text = "\n".join(json.dumps(event, ensure_ascii=False, default=str).lower() for event in events)
        return {
            "tasktool_pal": any(token in text for token in ("tasktool", "task_tool", "pal_execution")),
            "mnemopi_memory": any(token in text for token in ("mnemopi", "procedure_memory", "curator_commit")),
            "provider_retry_fallback": "provider" in text and any(token in text for token in ("retry", "fallback", "failover")),
            "hashline_patch": any(token in text for token in ("hashline", "stale_patch", "patch_anchor")),
        }


class LongHorizonBenchmarkGate:
    def __init__(self, thresholds: BenchmarkThresholds | None = None) -> None:
        self.thresholds = thresholds or BenchmarkThresholds()
        self.analyzer = BenchmarkAnalyzer()

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        declared_policy: Mapping[str, Any] | None = None,
        final_completion: bool,
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-long-horizon-benchmark",
            status=GateStatus.NOT_RUN,
            summary="Same-run sealed-autonomy benchmark with effective action and transition admission.",
        )
        if not run_id:
            result.add(
                Finding(
                    code="benchmark.run_id_missing",
                    severity=Severity.BLOCKER,
                    summary="Long-horizon benchmark requires one canonical run id.",
                )
            )
            return result.finish()
        snapshot = self.analyzer.analyze(events, run_id=run_id)
        self._threshold_findings(snapshot, result, final_completion=final_completion)
        self._autonomy_findings(snapshot, result)
        self._quality_findings(snapshot, result, final_completion=final_completion)
        child_gates = self._child_gates(
            events,
            run_id=run_id,
            declared_policy=declared_policy or {},
            final_completion=final_completion,
        )
        for gate in child_gates:
            for finding in gate.findings:
                if finding.severity.failing:
                    result.add(
                        Finding(
                            code=f"benchmark.child.{finding.code}",
                            severity=finding.severity,
                            summary=finding.summary,
                            detail=finding.detail,
                            location=finding.location,
                            metadata=dict(finding.metadata),
                        )
                    )
            result.evidence.extend(gate.evidence)
        result.metrics.update(snapshot.to_dict(include_records=False))
        result.metrics.update(
            {
                "thresholds": {
                    "minimum_effective_actions": self.thresholds.minimum_effective_actions,
                    "minimum_effective_transitions": self.thresholds.minimum_effective_transitions,
                    "minimum_semantic_families": self.thresholds.minimum_semantic_families,
                    "maximum_duplicate_ratio": self.thresholds.maximum_duplicate_ratio,
                    "maximum_excluded_ratio": self.thresholds.maximum_excluded_ratio,
                },
                "child_status": {gate.gate_id: gate.status.value for gate in child_gates},
                "snapshot_digest": stable_digest(snapshot.to_dict(include_records=False)),
            }
        )
        result.evidence.append(
            EvidencePointer(
                kind="long_horizon_benchmark",
                location=run_id,
                summary=(
                    f"{len(snapshot.effective_actions)} effective actions / "
                    f"{len(snapshot.effective_transitions)} effective transitions"
                ),
                revision=str(max((item.after_revision for item in snapshot.transitions), default=0)),
                metadata={"snapshot_digest": result.metrics["snapshot_digest"]},
            )
        )
        return result.finish(default_partial=not final_completion)

    def _threshold_findings(
        self,
        snapshot: BenchmarkSnapshot,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        checks = (
            (
                len(snapshot.effective_actions),
                self.thresholds.minimum_effective_actions,
                "benchmark.effective_actions_below_threshold",
                "Long-horizon run has too few real effective actions.",
            ),
            (
                len(snapshot.effective_transitions),
                self.thresholds.minimum_effective_transitions,
                "benchmark.effective_transitions_below_threshold",
                "Long-horizon run has too few canonical state transitions.",
            ),
            (
                len({item.semantic_family for item in snapshot.effective_transitions}),
                self.thresholds.minimum_semantic_families,
                "benchmark.semantic_family_coverage_low",
                "Long-horizon progress is not diverse enough to prove a real task.",
            ),
            (
                snapshot.requirement_changes,
                self.thresholds.minimum_requirement_changes,
                "benchmark.requirement_change_missing",
                "Long-horizon run lacks a real requirement change.",
            ),
            (
                snapshot.fault_recoveries,
                self.thresholds.minimum_fault_recoveries,
                "benchmark.fault_recovery_missing",
                "Long-horizon run lacks fault/recovery progress.",
            ),
            (
                snapshot.topology_mutations,
                self.thresholds.minimum_topology_mutations,
                "benchmark.topology_mutation_missing",
                "Long-horizon run lacks runtime topology mutation.",
            ),
        )
        for observed, minimum, code, summary in checks:
            if observed < minimum:
                result.add(
                    Finding(
                        code=code,
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary=summary,
                        detail=f"required={minimum}; observed={observed}",
                    )
                )

    def _autonomy_findings(self, snapshot: BenchmarkSnapshot, result: GateResult) -> None:
        interventions = sum(item.human_intervention for item in snapshot.actions)
        manual_resumes = sum(item.manual_resume for item in snapshot.actions)
        state_edits = sum(item.state_edit for item in snapshot.actions)
        unresolved = sum(item.unresolved_approval for item in snapshot.actions)
        for observed, maximum, code, summary in (
            (interventions, self.thresholds.maximum_human_interventions, "benchmark.human_intervention", "Sealed benchmark used human intervention."),
            (manual_resumes, self.thresholds.maximum_manual_resumes, "benchmark.manual_resume", "Sealed benchmark used manual resume."),
            (state_edits, self.thresholds.maximum_state_edits, "benchmark.manual_state_edit", "Sealed benchmark used manual state editing."),
            (unresolved, self.thresholds.maximum_unresolved_approvals, "benchmark.unresolved_approval", "Sealed benchmark ended with unresolved approval."),
        ):
            if observed > maximum:
                result.add(
                    Finding(
                        code=code,
                        severity=Severity.BLOCKER,
                        summary=summary,
                        detail=f"maximum={maximum}; observed={observed}",
                    )
                )
        for action in snapshot.actions:
            risk = action.risk.lower()
            effect = action.policy_effect.lower()
            if risk == "low" and effect not in {"allow", "allowed", "execute", "executed"}:
                result.add(
                    Finding(
                        code="benchmark.low_risk_not_allowed",
                        severity=Severity.BLOCKER,
                        summary="Sealed policy did not deterministically allow a low-risk action.",
                        detail=action.action_id,
                    )
                )
            if risk in {"dangerous", "high", "unknown"} and effect not in {"deny", "denied", "reject", "rejected"}:
                result.add(
                    Finding(
                        code="benchmark.risky_action_not_denied",
                        severity=Severity.BLOCKER,
                        summary="Sealed policy did not deterministically deny a dangerous/unknown action.",
                        detail=action.action_id,
                    )
                )

    def _quality_findings(
        self,
        snapshot: BenchmarkSnapshot,
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        effective_count = max(1, len(snapshot.effective_transitions))
        duplicate_ratio = snapshot.duplicates / effective_count
        excluded_ratio = sum(snapshot.exclusions.values()) / max(1, len(snapshot.transitions))
        if duplicate_ratio > self.thresholds.maximum_duplicate_ratio:
            result.add(
                Finding(
                    code="benchmark.duplicate_ratio_high",
                    severity=Severity.BLOCKER,
                    summary="Too many semantically duplicate transitions were counted as progress.",
                    detail=f"maximum={self.thresholds.maximum_duplicate_ratio:.4f}; observed={duplicate_ratio:.4f}",
                )
            )
        if excluded_ratio > self.thresholds.maximum_excluded_ratio:
            result.add(
                Finding(
                    code="benchmark.excluded_ratio_high",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Run is dominated by non-effective events rather than real state progress.",
                    detail=f"maximum={self.thresholds.maximum_excluded_ratio:.4f}; observed={excluded_ratio:.4f}",
                )
            )
        if snapshot.revision_gaps:
            result.add(
                Finding(
                    code="benchmark.revision_lineage_gap",
                    severity=Severity.BLOCKER,
                    summary="Canonical transition revisions contain unexplained gaps.",
                    detail=repr(snapshot.revision_gaps[:20]),
                )
            )
        omp_count = sum(snapshot.omp_effects.values())
        if omp_count < 2:
            result.add(
                Finding(
                    code="benchmark.omp_effect_coverage_low",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Long-horizon run did not semantically exercise two selected OMP mechanisms.",
                    detail=json.dumps(snapshot.omp_effects, sort_keys=True),
                )
            )

    @staticmethod
    def _child_gates(
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str,
        declared_policy: Mapping[str, Any],
        final_completion: bool,
    ) -> tuple[GateResult, ...]:
        selected = [
            event
            for event in events
            if str(event.get("run_id") or (event.get("payload") or {}).get("run_id") or "") == run_id
        ]
        return (
            LongHorizonProgressLedger().evaluate(selected, run_id=run_id, final_completion=final_completion),
            CausalEvidenceGraphGate().evaluate(selected, final_completion=final_completion),
            SealedAutonomyGate().evaluate(
                selected,
                declared_policy=declared_policy,
                final_completion=final_completion,
            ),
            LowEntropyGate().evaluate(events=selected, final_completion=final_completion),
        )


def benchmark_summary(events: Sequence[Mapping[str, Any]], run_id: str) -> Mapping[str, Any]:
    snapshot = BenchmarkAnalyzer().analyze(events, run_id=run_id)
    effective = snapshot.effective_transitions
    action_sizes = [len(action.transition_ids) for action in snapshot.effective_actions]
    revisions = [item.after_revision for item in effective]
    return {
        **snapshot.to_dict(include_records=False),
        "action_transition_mean": statistics.fmean(action_sizes) if action_sizes else 0.0,
        "action_transition_p95": _percentile(action_sizes, 0.95),
        "revision_min": min(revisions, default=0),
        "revision_max": max(revisions, default=0),
        "evidence_digest": stable_digest([item.to_dict() for item in effective]),
    }


def _percentile(values: Sequence[int], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    position = max(0.0, min(1.0, quantile)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
