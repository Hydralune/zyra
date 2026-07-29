from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from zyra_core import TaskState, now_iso, to_jsonable

from .contracts import (
    ContractHeader,
    FrozenDict,
    MemoryContinuityReceipt,
    PolicyBudget,
    PolicyContractError,
    PolicyInputSnapshot,
    StableArtifactRef,
    canonical_digest,
    thaw_json,
)


class MemoryContinuityError(PolicyContractError):
    """Continuity evidence is insufficient to construct a policy input."""

    def __init__(self, message: str, *, action: str, reason_codes: Sequence[str]) -> None:
        super().__init__(message)
        self.action = action
        self.reason_codes = tuple(reason_codes)


class ContinuityTransitionKind(StrEnum):
    COMPACT_RESTORE = "compact_restore"
    ROLE_HANDOFF = "role_handoff"
    WORKER_HANDOFF = "worker_handoff"
    NODE_REPLACEMENT = "node_replacement"
    CHECKPOINT_RESTART = "checkpoint_restart"
    PROCESS_RESTART = "process_restart"
    REQUIREMENT_REVISION = "requirement_revision"


class CanonicalMemoryFabricPort(Protocol):
    """The only memory read used by the continuity verifier."""

    def canonical_task_records(self, task_id: str) -> Sequence[Any]: ...


@dataclass(frozen=True, slots=True)
class ContinuityTransition:
    transition_id: str
    kind: ContinuityTransitionKind
    owner_receipt_ref: str
    source_scope: str
    target_scope: str
    checkpoint_ref: str = ""
    compact_ref: str = ""
    source_process_id: str = ""
    target_process_id: str = ""
    acknowledged: bool = False
    acknowledged_requirement_revision: str = ""
    acknowledged_obligation_digest: str = ""
    acknowledged_fact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", ContinuityTransitionKind(self.kind))
        for name in ("transition_id", "owner_receipt_ref", "source_scope", "target_scope"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(
            self,
            "acknowledged_fact_ids",
            tuple(sorted({str(item) for item in self.acknowledged_fact_ids if str(item)})),
        )


@dataclass(frozen=True, slots=True)
class ContinuitySnapshot:
    run_id: str
    task_id: str
    requirement_revision: str
    obligation_ids: tuple[str, ...]
    obligation_digest: str
    critical_fact_refs: tuple[StableArtifactRef, ...]
    fact_versions: FrozenDict
    fact_confidence: FrozenDict
    fact_status: FrozenDict
    fact_provenance: FrozenDict
    completed_artifact_ids: tuple[str, ...]
    pending_side_effect_ids: tuple[str, ...]
    rejected_memory_refs: tuple[StableArtifactRef, ...]
    captured_at: str
    snapshot_digest: str = ""

    def __post_init__(self) -> None:
        for name in ("run_id", "task_id", "requirement_revision", "captured_at"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        object.__setattr__(
            self,
            "obligation_ids",
            tuple(sorted({str(item) for item in self.obligation_ids if str(item)})),
        )
        expected_obligation_digest = canonical_digest(self.obligation_ids)
        if self.obligation_digest and self.obligation_digest != expected_obligation_digest:
            raise ValueError("obligation digest does not match obligation ids")
        object.__setattr__(self, "obligation_digest", expected_obligation_digest)
        object.__setattr__(
            self,
            "critical_fact_refs",
            tuple(sorted(self.critical_fact_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(self, "fact_versions", FrozenDict(self.fact_versions))
        object.__setattr__(self, "fact_confidence", FrozenDict(self.fact_confidence))
        object.__setattr__(self, "fact_status", FrozenDict(self.fact_status))
        object.__setattr__(self, "fact_provenance", FrozenDict(self.fact_provenance))
        object.__setattr__(
            self,
            "completed_artifact_ids",
            tuple(sorted({str(item) for item in self.completed_artifact_ids if str(item)})),
        )
        object.__setattr__(
            self,
            "pending_side_effect_ids",
            tuple(sorted({str(item) for item in self.pending_side_effect_ids if str(item)})),
        )
        object.__setattr__(
            self,
            "rejected_memory_refs",
            tuple(sorted(self.rejected_memory_refs, key=lambda item: item.ref_id)),
        )
        digest = canonical_digest(
            {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "requirement_revision": self.requirement_revision,
                "obligation_digest": self.obligation_digest,
                "critical_fact_refs": [item.to_dict() for item in self.critical_fact_refs],
                "fact_versions": thaw_json(self.fact_versions),
                "fact_confidence": thaw_json(self.fact_confidence),
                "fact_status": thaw_json(self.fact_status),
                "fact_provenance": thaw_json(self.fact_provenance),
                "completed_artifact_ids": self.completed_artifact_ids,
                "pending_side_effect_ids": self.pending_side_effect_ids,
                "rejected_memory_refs": [
                    item.to_dict() for item in self.rejected_memory_refs
                ],
            }
        )
        if self.snapshot_digest and self.snapshot_digest != digest:
            raise ValueError("continuity snapshot digest mismatch")
        object.__setattr__(self, "snapshot_digest", digest)


@dataclass(frozen=True, slots=True)
class ContinuityGateResult:
    gate_id: str
    transition: ContinuityTransition
    before: ContinuitySnapshot
    after: ContinuitySnapshot
    passed: bool
    recovery_action: str
    reason_codes: tuple[str, ...]
    accepted_fact_refs: tuple[StableArtifactRef, ...]
    rejected_memory_refs: tuple[StableArtifactRef, ...]
    critical_fact_results: FrozenDict
    obligation_results: FrozenDict
    verifier_enabled: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "reason_codes",
            tuple(sorted({str(item) for item in self.reason_codes if str(item)})),
        )
        object.__setattr__(
            self,
            "accepted_fact_refs",
            tuple(sorted(self.accepted_fact_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(
            self,
            "rejected_memory_refs",
            tuple(sorted(self.rejected_memory_refs, key=lambda item: item.ref_id)),
        )
        object.__setattr__(self, "critical_fact_results", FrozenDict(self.critical_fact_results))
        object.__setattr__(self, "obligation_results", FrozenDict(self.obligation_results))
        if self.passed and self.reason_codes:
            raise ValueError("passing continuity gate cannot contain failure reasons")
        if not self.passed and self.recovery_action not in {"recover", "replan"}:
            raise ValueError("failed continuity gate must select recover or replan")


@dataclass(frozen=True, slots=True)
class DownstreamMemoryUsage:
    decision_ref: str
    requirement_revision: str
    critical_fact_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    source_event_refs: tuple[str, ...]
    tool_call_ref: str = ""
    produced_artifact_ids: tuple[str, ...] = ()
    worker_id: str = ""
    node_id: str = ""

    def __post_init__(self) -> None:
        for name in ("decision_ref", "requirement_revision"):
            if not str(getattr(self, name) or "").strip():
                raise ValueError(f"{name} is required")
        for name in (
            "critical_fact_ids",
            "obligation_ids",
            "source_event_refs",
            "produced_artifact_ids",
        ):
            object.__setattr__(
                self,
                name,
                tuple(sorted({str(item) for item in getattr(self, name) if str(item)})),
            )


@dataclass(frozen=True, slots=True)
class ContinuityFinalResult:
    receipt: MemoryContinuityReceipt
    passed: bool
    recovery_action: str
    reason_codes: tuple[str, ...]


class MemoryContinuityVerifier:
    """Fail-closed continuity gate over MemoryFabric-owned canonical facts.

    The verifier owns no memory rows, index, checkpoint, task, graph, lease, or
    outcome state. Its snapshots contain stable record references and digests,
    never copied fact bodies.
    """

    _ACTIVE_STATUSES = frozenset({"", "active", "current", "verified"})
    _REJECTED_STATUSES = frozenset(
        {"stale", "superseded", "conflicting", "poisoned"}
    )

    def __init__(
        self,
        memory_fabric: CanonicalMemoryFabricPort,
        *,
        enabled: bool = True,
        test_mode: bool = False,
    ) -> None:
        if not enabled and not test_mode:
            raise ValueError("continuity disable switch is test-only")
        self.memory_fabric = memory_fabric
        self.enabled = bool(enabled)
        self.test_mode = bool(test_mode)

    def capture(
        self,
        *,
        run_id: str,
        task_id: str,
        requirement_revision: str,
        obligation_ids: Iterable[str],
        critical_fact_ids: Iterable[str],
        completed_artifact_ids: Iterable[str] = (),
        pending_side_effect_ids: Iterable[str] = (),
        captured_at: str | None = None,
    ) -> ContinuitySnapshot:
        records = tuple(self.memory_fabric.canonical_task_records(task_id))
        by_id = {str(getattr(item, "memory_id", "")): item for item in records}
        requested = tuple(sorted({str(item) for item in critical_fact_ids if str(item)}))
        refs: list[StableArtifactRef] = []
        rejected: list[StableArtifactRef] = []
        versions: dict[str, Any] = {}
        confidence: dict[str, Any] = {}
        statuses: dict[str, Any] = {}
        provenance: dict[str, Any] = {}
        for memory_id in requested:
            record = by_id.get(memory_id)
            if record is None:
                statuses[memory_id] = "missing"
                continue
            reference = self._memory_ref(record)
            status, resolution = self._record_status(record, by_id)
            statuses[memory_id] = status
            versions[memory_id] = self._record_version(record)
            confidence[memory_id] = float(getattr(record, "score", 0.0))
            provenance[memory_id] = self._record_provenance(record)
            if status in self._ACTIVE_STATUSES:
                refs.append(reference)
            else:
                rejected.append(reference)
                if resolution is not None:
                    resolved_ref = self._memory_ref(resolution)
                    refs.append(resolved_ref)
                    resolved_id = resolved_ref.ref_id
                    statuses[resolved_id] = "resolved"
                    versions[resolved_id] = self._record_version(resolution)
                    confidence[resolved_id] = float(getattr(resolution, "score", 0.0))
                    provenance[resolved_id] = self._record_provenance(resolution)
        return ContinuitySnapshot(
            run_id=run_id,
            task_id=task_id,
            requirement_revision=requirement_revision,
            obligation_ids=tuple(obligation_ids),
            obligation_digest="",
            critical_fact_refs=tuple(refs),
            fact_versions=FrozenDict(versions),
            fact_confidence=FrozenDict(confidence),
            fact_status=FrozenDict(statuses),
            fact_provenance=FrozenDict(provenance),
            completed_artifact_ids=tuple(completed_artifact_ids),
            pending_side_effect_ids=tuple(pending_side_effect_ids),
            rejected_memory_refs=tuple(rejected),
            captured_at=captured_at or now_iso(),
        )

    def verify_before_policy(
        self,
        before: ContinuitySnapshot,
        *,
        transition: ContinuityTransition,
        current_requirement_revision: str,
        current_obligation_ids: Iterable[str],
        critical_fact_ids: Iterable[str],
        completed_artifact_ids: Iterable[str] = (),
        pending_side_effect_ids: Iterable[str] = (),
        captured_at: str | None = None,
    ) -> ContinuityGateResult:
        after = self.capture(
            run_id=before.run_id,
            task_id=before.task_id,
            requirement_revision=current_requirement_revision,
            obligation_ids=current_obligation_ids,
            critical_fact_ids=critical_fact_ids,
            completed_artifact_ids=completed_artifact_ids,
            pending_side_effect_ids=pending_side_effect_ids,
            captured_at=captured_at,
        )
        reasons: list[str] = []
        if not self.enabled:
            reasons.append("continuity_verifier_disabled")
        reasons.extend(self._validate_transition(before, after, transition))
        before_facts = {item.ref_id: item for item in before.critical_fact_refs}
        after_facts = {item.ref_id: item for item in after.critical_fact_refs}
        missing_facts = sorted(set(before_facts) - set(after_facts))
        requested_facts = tuple(sorted({str(item) for item in critical_fact_ids if str(item)}))
        missing_requested = sorted(
            fact_id
            for fact_id in requested_facts
            if fact_id not in after_facts
            and str(after.fact_status.get(fact_id) or "") not in {"resolved"}
        )
        if missing_facts or missing_requested:
            reasons.append("critical_fact_missing")
        if after.rejected_memory_refs:
            reasons.append("unsafe_memory_rejected")
        current_obligations = set(after.obligation_ids)
        superseded_obligations: list[str] = []
        if (
            transition.kind is ContinuityTransitionKind.REQUIREMENT_REVISION
            and before.requirement_revision != after.requirement_revision
        ):
            superseded_obligations = sorted(
                set(before.obligation_ids) - current_obligations
            )
            missing_obligations = []
        else:
            missing_obligations = sorted(
                set(before.obligation_ids) - current_obligations
            )
        if missing_obligations:
            reasons.append("unresolved_obligation_missing")
        if after.requirement_revision != current_requirement_revision:
            reasons.append("stale_requirement_revision")
        if not self._fact_provenance_complete(
            after.critical_fact_refs,
            after.fact_provenance,
        ):
            reasons.append("critical_fact_provenance_missing")
        fact_results = {
            fact_id: {
                "present_before": fact_id in before_facts,
                "present_after": fact_id in after_facts,
                "version_before": before.fact_versions.get(fact_id),
                "version_after": after.fact_versions.get(fact_id),
                "confidence": after.fact_confidence.get(fact_id),
                "status": after.fact_status.get(fact_id, "missing"),
                "provenance": after.fact_provenance.get(fact_id, {}),
                "provenance_ref": (
                    after_facts[fact_id].to_dict() if fact_id in after_facts else {}
                ),
            }
            for fact_id in sorted(set(requested_facts) | set(before_facts) | set(after_facts))
        }
        obligation_results = {
            "before_digest": before.obligation_digest,
            "after_digest": after.obligation_digest,
            "retained": not missing_obligations,
            "missing": missing_obligations,
            "superseded": superseded_obligations,
            "transition_kind": transition.kind.value,
            "transition_receipt_ref": transition.owner_receipt_ref,
            "handoff_source": transition.source_scope,
            "handoff_target": transition.target_scope,
            "acknowledged": transition.acknowledged,
            "pending_side_effect_ids": list(after.pending_side_effect_ids),
        }
        reason_codes = tuple(sorted(set(reasons)))
        passed = not reason_codes
        return ContinuityGateResult(
            gate_id=(
                "continuity_gate_"
                + canonical_digest(
                    (
                        transition.transition_id,
                        before.snapshot_digest,
                        after.snapshot_digest,
                    )
                )[:24]
            ),
            transition=transition,
            before=before,
            after=after,
            passed=passed,
            recovery_action="" if passed else self._recovery_action(reason_codes),
            reason_codes=reason_codes,
            accepted_fact_refs=after.critical_fact_refs,
            rejected_memory_refs=after.rejected_memory_refs,
            critical_fact_results=FrozenDict(fact_results),
            obligation_results=FrozenDict(obligation_results),
            verifier_enabled=self.enabled,
        )

    def build_policy_input(
        self,
        gate: ContinuityGateResult,
        *,
        task: TaskState,
        graph: Any,
        environment: Any,
        header: ContractHeader,
        budget: PolicyBudget,
        readiness_refs: Iterable[Any],
        registry_versions: Mapping[str, Any],
        phase: str = "phase2",
        registered_roles: Iterable[str] = (),
        registered_capabilities: Iterable[str] = (),
        allowed_permissions: Iterable[str] = (),
        allowed_placements: Iterable[str] = (),
        privacy_class: str = "internal",
        last_topology_change_at: str = "",
    ) -> PolicyInputSnapshot:
        if not gate.passed:
            raise MemoryContinuityError(
                "continuity verification failed before policy input construction",
                action=gate.recovery_action,
                reason_codes=gate.reason_codes,
            )
        if gate.after.task_id != task.task_id or gate.after.run_id != task.run_id:
            raise MemoryContinuityError(
                "continuity gate belongs to a different run or task",
                action="replan",
                reason_codes=("continuity_scope_mismatch",),
            )
        if header.causation_id != gate.gate_id:
            raise MemoryContinuityError(
                "policy input header is not caused by the continuity gate",
                action="replan",
                reason_codes=("continuity_causation_missing",),
            )
        from .snapshot import PolicyInputSnapshotBuilder

        snapshot = PolicyInputSnapshotBuilder.build(
            task=task,
            graph=graph,
            environment=environment,
            header=header,
            budget=budget,
            readiness_refs=readiness_refs,
            registry_versions=registry_versions,
            memory_refs=gate.accepted_fact_refs,
            phase=phase,
            requirement_revision=gate.after.requirement_revision,
            registered_roles=registered_roles,
            registered_capabilities=registered_capabilities,
            allowed_permissions=allowed_permissions,
            allowed_placements=allowed_placements,
            privacy_class=privacy_class,
            last_topology_change_at=last_topology_change_at,
            continuity_gate=gate,
        )
        if set(snapshot.unresolved_obligations) != set(gate.after.obligation_ids):
            raise MemoryContinuityError(
                "canonical task obligations differ from the continuity gate",
                action="replan",
                reason_codes=("continuity_obligation_scope_mismatch",),
            )
        return snapshot

    def finalize_after_decision(
        self,
        gate: ContinuityGateResult,
        usage: DownstreamMemoryUsage,
        *,
        header: ContractHeader,
    ) -> ContinuityFinalResult:
        reasons = list(gate.reason_codes)
        accepted_ids = {item.ref_id for item in gate.accepted_fact_refs}
        missing_usage = sorted(accepted_ids - set(usage.critical_fact_ids))
        if missing_usage:
            reasons.append("critical_fact_not_consumed")
        missing_obligation_usage = sorted(
            set(gate.after.obligation_ids) - set(usage.obligation_ids)
        )
        if missing_obligation_usage:
            reasons.append("obligation_not_consumed")
        stale_requirement_execution = (
            usage.requirement_revision != gate.after.requirement_revision
        )
        if stale_requirement_execution:
            reasons.append("stale_requirement_execution")
        duplicate_work = sorted(
            set(usage.produced_artifact_ids)
            & set(gate.before.completed_artifact_ids)
        )
        if duplicate_work:
            reasons.append("duplicate_completed_artifact")
        if not usage.source_event_refs:
            reasons.append("downstream_usage_event_missing")
        if not usage.tool_call_ref and not usage.decision_ref:
            reasons.append("downstream_decision_or_tool_missing")
        critical_results = thaw_json(gate.critical_fact_results)
        for fact_id, result in critical_results.items():
            result["consumed"] = fact_id in usage.critical_fact_ids
            result["decision_ref"] = usage.decision_ref
            result["tool_call_ref"] = usage.tool_call_ref
            result["usage_event_refs"] = list(usage.source_event_refs)
        obligation_results = thaw_json(gate.obligation_results)
        obligation_results.update(
            {
                "consumed_ids": list(usage.obligation_ids),
                "missing_consumption": missing_obligation_usage,
                "duplicate_work_artifact_ids": duplicate_work,
                "stale_requirement_execution": stale_requirement_execution,
                "downstream_decision_ref": usage.decision_ref,
                "downstream_tool_call_ref": usage.tool_call_ref,
                "downstream_event_refs": list(usage.source_event_refs),
                "worker_id": usage.worker_id,
                "node_id": usage.node_id,
            }
        )
        reason_codes = tuple(sorted(set(reasons)))
        passed = not reason_codes
        receipt = MemoryContinuityReceipt(
            header=header,
            before_digest=gate.before.snapshot_digest,
            after_digest=gate.after.snapshot_digest,
            requirement_revision=gate.after.requirement_revision,
            critical_fact_results=FrozenDict(critical_results),
            obligation_results=FrozenDict(obligation_results),
            provenance_refs=gate.accepted_fact_refs,
            rejected_memory_refs=gate.rejected_memory_refs,
            downstream_decision_ref=usage.decision_ref,
            continuity_result=("passed" if passed else "failed:" + ",".join(reason_codes)),
        )
        return ContinuityFinalResult(
            receipt=receipt,
            passed=passed,
            recovery_action="" if passed else self._recovery_action(reason_codes),
            reason_codes=reason_codes,
        )

    @classmethod
    def _record_status(
        cls,
        record: Any,
        by_id: Mapping[str, Any],
    ) -> tuple[str, Any | None]:
        metadata = getattr(record, "metadata", {}) or {}
        status = str(metadata.get("continuity_status") or "active").lower()
        content_digest = str(metadata.get("content_digest") or "")
        if content_digest:
            observed = canonical_digest(getattr(record, "content", {}))
            if observed != content_digest:
                status = "poisoned"
        conflicts = metadata.get("conflicts_with") or ()
        if conflicts and status in cls._ACTIVE_STATUSES:
            status = "conflicting"
        if status not in cls._ACTIVE_STATUSES | cls._REJECTED_STATUSES:
            status = "poisoned"
        if status == "conflicting":
            resolution_id = str(metadata.get("resolved_by") or "")
            resolution = by_id.get(resolution_id)
            if resolution is not None:
                resolved_status = str(
                    (getattr(resolution, "metadata", {}) or {}).get(
                        "continuity_status",
                        "active",
                    )
                ).lower()
                if resolved_status in cls._ACTIVE_STATUSES:
                    return status, resolution
        return status, None

    @staticmethod
    def _memory_ref(record: Any) -> StableArtifactRef:
        rendered = to_jsonable(record)
        return StableArtifactRef(
            ref_id=str(getattr(record, "memory_id", "")),
            uri=(
                "memoryfabric://"
                f"{getattr(record, 'run_id', '')}/"
                f"{getattr(record, 'task_id', '')}/"
                f"{getattr(record, 'memory_id', '')}"
            ),
            digest=canonical_digest(rendered),
            media_type="application/vnd.zyra.memory-record+json",
        )

    @staticmethod
    def _record_version(record: Any) -> str:
        metadata = getattr(record, "metadata", {}) or {}
        return str(
            metadata.get("continuity_version")
            or getattr(record, "updated_at", "")
            or getattr(record, "created_at", "")
        )

    @staticmethod
    def _record_provenance(record: Any) -> dict[str, Any]:
        return {
            "source_type": str(getattr(record, "source_type", "")),
            "source_id": str(getattr(record, "source_id", "")),
            "artifact_ids": sorted(
                str(item) for item in getattr(record, "artifact_ids", ()) if str(item)
            ),
            "evidence_ids": sorted(
                str(item) for item in getattr(record, "evidence_ids", ()) if str(item)
            ),
            "node_id": str(getattr(record, "node_id", "") or ""),
        }

    @staticmethod
    def _fact_provenance_complete(
        refs: Sequence[StableArtifactRef],
        provenance: Mapping[str, Any],
    ) -> bool:
        return all(
            item.ref_id
            and item.uri.startswith("memoryfabric://")
            and len(item.digest) == 64
            and bool(
                isinstance(provenance.get(item.ref_id), Mapping)
                and provenance[item.ref_id].get("source_type")
                and provenance[item.ref_id].get("source_id")
            )
            for item in refs
        )

    @staticmethod
    def _validate_transition(
        before: ContinuitySnapshot,
        after: ContinuitySnapshot,
        transition: ContinuityTransition,
    ) -> list[str]:
        reasons: list[str] = []
        if before.run_id != after.run_id or before.task_id != after.task_id:
            reasons.append("continuity_scope_mismatch")
        if transition.kind is ContinuityTransitionKind.COMPACT_RESTORE:
            if not transition.compact_ref or not transition.checkpoint_ref:
                reasons.append("compact_restore_receipt_missing")
        if transition.kind in {
            ContinuityTransitionKind.ROLE_HANDOFF,
            ContinuityTransitionKind.WORKER_HANDOFF,
            ContinuityTransitionKind.NODE_REPLACEMENT,
        }:
            if transition.source_scope == transition.target_scope:
                reasons.append("handoff_target_not_changed")
            if not transition.acknowledged:
                reasons.append("handoff_not_acknowledged")
            if transition.acknowledged_requirement_revision != after.requirement_revision:
                reasons.append("handoff_requirement_ack_mismatch")
            if transition.acknowledged_obligation_digest != after.obligation_digest:
                reasons.append("handoff_obligation_ack_mismatch")
            after_fact_ids = {item.ref_id for item in after.critical_fact_refs}
            if set(transition.acknowledged_fact_ids) != after_fact_ids:
                reasons.append("handoff_fact_ack_mismatch")
        if transition.kind in {
            ContinuityTransitionKind.CHECKPOINT_RESTART,
            ContinuityTransitionKind.PROCESS_RESTART,
        } and not transition.checkpoint_ref:
            reasons.append("restart_checkpoint_missing")
        if transition.kind is ContinuityTransitionKind.PROCESS_RESTART:
            if (
                not transition.source_process_id
                or not transition.target_process_id
                or transition.source_process_id == transition.target_process_id
            ):
                reasons.append("process_restart_identity_unchanged")
        if transition.kind is ContinuityTransitionKind.REQUIREMENT_REVISION:
            if before.requirement_revision == after.requirement_revision:
                reasons.append("requirement_revision_unchanged")
        return reasons

    @staticmethod
    def _recovery_action(reason_codes: Sequence[str]) -> str:
        recoverable = {
            "continuity_verifier_disabled",
            "compact_restore_receipt_missing",
            "restart_checkpoint_missing",
            "process_restart_identity_unchanged",
            "critical_fact_missing",
            "unresolved_obligation_missing",
            "critical_fact_not_consumed",
            "obligation_not_consumed",
            "downstream_usage_event_missing",
        }
        return "recover" if any(item in recoverable for item in reason_codes) else "replan"


__all__ = [
    "CanonicalMemoryFabricPort",
    "ContinuityFinalResult",
    "ContinuityGateResult",
    "ContinuitySnapshot",
    "ContinuityTransition",
    "ContinuityTransitionKind",
    "DownstreamMemoryUsage",
    "MemoryContinuityError",
    "MemoryContinuityVerifier",
]
