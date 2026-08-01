from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from zyra_core import EventRecord, EventType

from ..graph_custody import (
    GraphCommitStatus,
    GraphConflictStrategy,
    GraphStateSnapshot,
)
from .arg import ARGTopologyRuntimeResult
from .composer import (
    TopologyCompositionResult,
    TopologyLayerSwitches,
    TopologyPolicyComposer,
)
from .condition import CARDTopologyRuntimeResult
from .contracts import (
    ContractHeader,
    ConstraintResult,
    FrozenDict,
    PolicyDecisionDisposition,
    PolicyInputSnapshot,
    PolicyOutcome,
    canonical_digest,
    freeze_json,
    thaw_json,
)
from .evidence import PolicyEvidencePublisher, PublishedPolicyEvidence
from .projector import PolicyProjectionResult, TopologyConstraintProjector
from .pruning import AgentPruneRuntimeResult
from .registry import (
    MechanismLifecycle,
    MechanismRegistration,
    MechanismRegistry,
    MechanismRunPin,
    PolicyRegistryError,
    ResolutionPurpose,
    ValidationManifest,
)


class PolicyRuntimeError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class DiagnosticMutationError(PolicyRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MechanismInvocation:
    run_id: str
    task_id: str
    mode: str
    pin: MechanismRunPin
    input_snapshot: Any
    baseline_reference: Any
    execution_allowed: bool
    timeout_seconds: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mode": self.mode,
            "pin": self.pin.to_dict(),
            "input_snapshot": thaw_json(self.input_snapshot),
            "baseline_reference": thaw_json(self.baseline_reference),
            "execution_allowed": self.execution_allowed,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class BaselineDecisionReference:
    profile_id: str
    input_snapshot_digest: str
    decision: Any
    decision_digest: str
    executed: bool
    side_effect_free: bool

    @classmethod
    def create(
        cls,
        *,
        profile_id: str,
        input_snapshot_digest: str,
        decision: Mapping[str, Any],
        executed: bool,
        side_effect_free: bool,
    ) -> BaselineDecisionReference:
        frozen = freeze_json(decision)
        return cls(
            profile_id=profile_id,
            input_snapshot_digest=input_snapshot_digest,
            decision=frozen,
            decision_digest=canonical_digest(thaw_json(frozen)),
            executed=executed,
            side_effect_free=side_effect_free,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "input_snapshot_digest": self.input_snapshot_digest,
            "decision": thaw_json(self.decision),
            "decision_digest": self.decision_digest,
            "executed": self.executed,
            "side_effect_free": self.side_effect_free,
        }


@dataclass(frozen=True, slots=True)
class DiagnosticReceipt:
    run_id: str
    task_id: str
    pin_digest: str
    input_snapshot_digest: str
    baseline_reference_digest: str
    proposal: Any
    proposal_digest: str
    no_effect_verified: bool
    owner_state_before_digest: str
    owner_state_after_digest: str
    executed: bool = False
    actual_outcome_recorded: bool = False
    outcome_statement: str = "not_executed_no_actual_outcome"

    @property
    def receipt_id(self) -> str:
        return "diagnostic_" + canonical_digest(self._payload())[:24]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def _payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "pin_digest": self.pin_digest,
            "input_snapshot_digest": self.input_snapshot_digest,
            "baseline_reference_digest": self.baseline_reference_digest,
            "proposal": thaw_json(self.proposal),
            "proposal_digest": self.proposal_digest,
            "no_effect_verified": self.no_effect_verified,
            "owner_state_before_digest": self.owner_state_before_digest,
            "owner_state_after_digest": self.owner_state_after_digest,
            "executed": self.executed,
            "actual_outcome_recorded": self.actual_outcome_recorded,
            "outcome_statement": self.outcome_statement,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-mechanism-diagnostic-receipt/v1",
            "receipt_id": self.receipt_id,
            **self._payload(),
        }


@dataclass(frozen=True, slots=True)
class MechanismExecutionReceipt:
    run_id: str
    task_id: str
    pin: MechanismRunPin
    input_snapshot_digest: str
    requested_mode: str
    actual_profile_id: str
    actual_version: str
    actual_decision_digest: str
    baseline_reference_digest: str
    diagnostic_receipt_digest: str
    degraded: bool
    degraded_reason: str
    fallback_executed: bool
    outcome_attribution: str
    actual_outcome_recorded: bool
    strongest_success_eligible: bool

    @property
    def receipt_id(self) -> str:
        return "mechanism_execution_" + canonical_digest(self._payload())[:24]

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def _payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "pin": self.pin.to_dict(),
            "input_snapshot_digest": self.input_snapshot_digest,
            "requested_mode": self.requested_mode,
            "actual_profile_id": self.actual_profile_id,
            "actual_version": self.actual_version,
            "actual_decision_digest": self.actual_decision_digest,
            "baseline_reference_digest": self.baseline_reference_digest,
            "diagnostic_receipt_digest": self.diagnostic_receipt_digest,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "fallback_executed": self.fallback_executed,
            "outcome_attribution": self.outcome_attribution,
            "actual_outcome_recorded": self.actual_outcome_recorded,
            "strongest_success_eligible": self.strongest_success_eligible,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.phase2-mechanism-execution-receipt/v1",
            "receipt_id": self.receipt_id,
            **self._payload(),
        }


@dataclass(frozen=True, slots=True)
class PolicyRuntimeResult:
    pin: MechanismRunPin
    actual_decision: Any
    baseline_reference: BaselineDecisionReference
    execution_receipt: MechanismExecutionReceipt
    diagnostic_receipt: DiagnosticReceipt | None
    events: tuple[EventRecord, ...]


@dataclass(frozen=True, slots=True)
class TopologyComposerRuntimeResult:
    mode: str
    trigger_kind: str
    composition: TopologyCompositionResult
    projection: PolicyProjectionResult | None
    outcome: PolicyOutcome | None
    published_evidence: tuple[PublishedPolicyEvidence, ...]
    scheduler_causal_refs: tuple[str, ...]
    memory_causal_refs: tuple[str, ...]
    recovery_causal_refs: tuple[str, ...]
    permission_result: str
    graph_revision_before: int
    graph_revision_after: int
    degraded: bool
    degraded_reason: str
    events: tuple[EventRecord, ...]

    @property
    def committed(self) -> bool:
        return (
            self.projection is not None
            and self.projection.commit is not None
            and self.projection.commit.receipt.status
            in {
                GraphCommitStatus.COMMITTED,
                GraphCommitStatus.REBASED,
                GraphCommitStatus.REPLAYED,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "trigger_kind": self.trigger_kind,
            "composition": self.composition.to_dict(),
            "decision_receipt": (
                self.projection.receipt.to_dict()
                if self.projection is not None
                else None
            ),
            "outcome": (
                self.outcome.to_dict()
                if self.outcome is not None
                else None
            ),
            "published_artifact_ids": [
                item.artifact.artifact_id
                for item in self.published_evidence
            ],
            "scheduler_causal_refs": list(self.scheduler_causal_refs),
            "memory_causal_refs": list(self.memory_causal_refs),
            "recovery_causal_refs": list(self.recovery_causal_refs),
            "permission_result": self.permission_result,
            "graph_revision_before": self.graph_revision_before,
            "graph_revision_after": self.graph_revision_after,
            "committed": self.committed,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "event_ids": [item.event_id for item in self.events],
        }


BaselineExecutor = Callable[[Any], Mapping[str, Any]]
MechanismExecutor = Callable[[MechanismInvocation], Mapping[str, Any]]
OwnerProbe = Callable[[], Mapping[str, Any]]
EventSink = Callable[[EventRecord], None]


class TopologyPolicyRuntime:
    """Pinned policy execution without owning run/session/config persistence."""

    def __init__(
        self,
        registry: MechanismRegistry | None,
        *,
        baseline_executor: BaselineExecutor,
        baseline_reference: BaselineExecutor | None = None,
        owner_probe: OwnerProbe | None = None,
        admit_event: EventSink | None = None,
        disconnected_reason: str = "",
    ) -> None:
        self.registry = registry
        self.baseline_executor = baseline_executor
        self.baseline_reference = baseline_reference
        self.owner_probe = owner_probe or (lambda: {})
        self.admit_event = admit_event or (lambda event: None)
        self.disconnected_reason = disconnected_reason

    @classmethod
    def from_repository(
        cls,
        repository_root: Path,
        *,
        baseline_executor: BaselineExecutor,
        baseline_reference: BaselineExecutor | None = None,
        owner_probe: OwnerProbe | None = None,
        admit_event: EventSink | None = None,
        config_path: Path | None = None,
    ) -> TopologyPolicyRuntime:
        try:
            registry = MechanismRegistry.load(
                repository_root,
                config_path=config_path,
            )
        except PolicyRegistryError as exc:
            return cls(
                None,
                baseline_executor=baseline_executor,
                baseline_reference=baseline_reference,
                owner_probe=owner_probe,
                admit_event=admit_event,
                disconnected_reason=exc.code,
            )
        return cls(
            registry,
            baseline_executor=baseline_executor,
            baseline_reference=baseline_reference,
            owner_probe=owner_probe,
            admit_event=admit_event,
        )

    def execute(
        self,
        *,
        run_id: str,
        task_id: str,
        input_snapshot: PolicyInputSnapshot | Mapping[str, Any],
        family: str = "topology_policy",
        purpose: ResolutionPurpose = ResolutionPurpose.NORMAL,
        version: str | None = None,
        validation_manifest: ValidationManifest | None = None,
        existing_pin: MechanismRunPin | None = None,
        mechanism_executor: MechanismExecutor | None = None,
        collect_baseline_reference: bool = True,
    ) -> PolicyRuntimeResult:
        frozen_input, input_digest = _freeze_input(input_snapshot)
        events: list[EventRecord] = []
        if self.registry is None:
            pin = _disconnected_baseline_pin(run_id)
            return self._baseline_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=pin,
                requested_mode=purpose.value,
                events=events,
                degraded_reason=(
                    "registry_disconnected:"
                    + (self.disconnected_reason or "unknown")
                ),
            )

        try:
            if existing_pin is not None:
                if existing_pin.run_id != run_id:
                    raise PolicyRegistryError(
                        "run-pin-owner-mismatch",
                        "A run cannot consume another run's mechanism pin.",
                    )
                record = self.registry.resolve_pinned(existing_pin)
                pin = existing_pin
            else:
                record = self.registry.resolve(
                    family,
                    purpose=purpose,
                    version=version,
                    validation_manifest=validation_manifest,
                )
                pin = self.registry.pin(
                    run_id,
                    family,
                    purpose=purpose,
                    version=version,
                    validation_manifest=validation_manifest,
                )
        except PolicyRegistryError as exc:
            pin = self.registry.pin_baseline(run_id)
            return self._baseline_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=pin,
                requested_mode=purpose.value,
                events=events,
                degraded_reason=f"registry_resolution:{exc.code}",
            )

        effective_lifecycle = (
            pin.lifecycle_at_pin
            if existing_pin is not None
            else record.lifecycle
        )
        if effective_lifecycle is MechanismLifecycle.BASELINE:
            return self._baseline_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=pin,
                requested_mode=purpose.value,
                events=events,
            )

        if effective_lifecycle is MechanismLifecycle.DIAGNOSTIC:
            return self._diagnostic_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=pin,
                record=record,
                mechanism_executor=mechanism_executor,
                events=events,
            )

        if mechanism_executor is None:
            return self._baseline_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=self.registry.pin_baseline(run_id),
                requested_mode=purpose.value,
                events=events,
                degraded_reason="mechanism_executor_missing",
                failed_record=record,
            )

        try:
            reference = (
                self._read_only_baseline_reference(
                    frozen_input,
                    input_digest=input_digest,
                )
                if collect_baseline_reference
                else _empty_baseline_reference(input_digest)
            )
            invocation = MechanismInvocation(
                run_id=run_id,
                task_id=task_id,
                mode=effective_lifecycle.value,
                pin=pin,
                input_snapshot=frozen_input,
                baseline_reference=freeze_json(reference.to_dict()),
                execution_allowed=True,
                timeout_seconds=record.timeout_seconds,
            )
            started = monotonic()
            actual = mechanism_executor(invocation)
            elapsed = monotonic() - started
            if elapsed > record.timeout_seconds:
                raise PolicyRuntimeError(
                    "mechanism_timeout",
                    "Mechanism execution exceeded its fixed timeout.",
                )
            frozen_actual = freeze_json(_mapping_result(actual))
        except Exception as exc:  # noqa: BLE001 - failure becomes explicit degraded evidence.
            reason = (
                exc.code
                if isinstance(exc, (PolicyRuntimeError, PolicyRegistryError))
                else f"mechanism_exception:{type(exc).__name__}"
            )
            return self._baseline_result(
                run_id=run_id,
                task_id=task_id,
                frozen_input=frozen_input,
                input_digest=input_digest,
                pin=self.registry.pin_baseline(run_id),
                requested_mode=purpose.value,
                events=events,
                degraded_reason=reason,
                failed_record=record,
            )

        actual_digest = canonical_digest(thaw_json(frozen_actual))
        strongest_success = (
            effective_lifecycle is MechanismLifecycle.DEFAULT
            and record.profile_id == "phase2_strongest_v1"
        )
        receipt = MechanismExecutionReceipt(
            run_id=run_id,
            task_id=task_id,
            pin=pin,
            input_snapshot_digest=input_digest,
            requested_mode=purpose.value,
            actual_profile_id=record.profile_id,
            actual_version=record.version,
            actual_decision_digest=actual_digest,
            baseline_reference_digest=canonical_digest(reference.to_dict()),
            diagnostic_receipt_digest="",
            degraded=False,
            degraded_reason="",
            fallback_executed=False,
            outcome_attribution=(
                "strongest_default"
                if strongest_success
                else "isolated_validation"
            ),
            actual_outcome_recorded=True,
            strongest_success_eligible=strongest_success,
        )
        return PolicyRuntimeResult(
            pin=pin,
            actual_decision=frozen_actual,
            baseline_reference=reference,
            execution_receipt=receipt,
            diagnostic_receipt=None,
            events=tuple(events),
        )

    def replay_receipt(
        self,
        receipt: MechanismExecutionReceipt,
    ) -> MechanismExecutionReceipt:
        if self.registry is None:
            raise PolicyRuntimeError(
                "registry_disconnected",
                "Historical receipt replay requires a compatible registry.",
            )
        self.registry.resolve_pinned(receipt.pin)
        return receipt

    def _diagnostic_result(
        self,
        *,
        run_id: str,
        task_id: str,
        frozen_input: Any,
        input_digest: str,
        pin: MechanismRunPin,
        record: MechanismRegistration,
        mechanism_executor: MechanismExecutor | None,
        events: list[EventRecord],
    ) -> PolicyRuntimeResult:
        baseline_actual = freeze_json(
            _mapping_result(self.baseline_executor(frozen_input))
        )
        baseline = BaselineDecisionReference.create(
            profile_id="phase1_deterministic_baseline",
            input_snapshot_digest=input_digest,
            decision=_mapping_result(thaw_json(baseline_actual)),
            executed=True,
            side_effect_free=False,
        )
        before = freeze_json(self.owner_probe())
        before_digest = canonical_digest(thaw_json(before))
        if mechanism_executor is None:
            return self._baseline_result_from_actual(
                run_id=run_id,
                task_id=task_id,
                pin=pin,
                requested_mode=ResolutionPurpose.DIAGNOSTIC.value,
                input_digest=input_digest,
                baseline_actual=baseline_actual,
                baseline=baseline,
                events=events,
                degraded_reason="diagnostic_executor_missing",
                failed_record=record,
            )
        failure_reason = ""
        try:
            invocation = MechanismInvocation(
                run_id=run_id,
                task_id=task_id,
                mode=MechanismLifecycle.DIAGNOSTIC.value,
                pin=pin,
                input_snapshot=frozen_input,
                baseline_reference=freeze_json(baseline.to_dict()),
                execution_allowed=False,
                timeout_seconds=record.timeout_seconds,
            )
            started = monotonic()
            proposal_value = mechanism_executor(invocation)
            elapsed = monotonic() - started
            if elapsed > record.timeout_seconds:
                raise PolicyRuntimeError(
                    "mechanism_timeout",
                    "Diagnostic execution exceeded its fixed timeout.",
                )
            proposal = freeze_json(_mapping_result(proposal_value))
        except Exception as exc:  # noqa: BLE001 - explicit degraded diagnostic.
            failure_reason = (
                exc.code
                if isinstance(exc, (PolicyRuntimeError, PolicyRegistryError))
                else f"mechanism_exception:{type(exc).__name__}"
            )
            proposal = freeze_json({})
        after = freeze_json(self.owner_probe())
        after_digest = canonical_digest(thaw_json(after))
        if before_digest != after_digest:
            event = self._degraded_event(
                run_id=run_id,
                task_id=task_id,
                failure_class="diagnostic_no_effect_guard",
                fallback_profile="phase1_deterministic_baseline",
                input_digest=input_digest,
                failed_record=record,
                owner_diff=_owner_diff(before, after),
            )
            self._emit(event, events)
            raise DiagnosticMutationError(
                "diagnostic-mutation-detected",
                "Diagnostic execution changed canonical owner state.",
            )
        if failure_reason:
            return self._baseline_result_from_actual(
                run_id=run_id,
                task_id=task_id,
                pin=pin,
                requested_mode=ResolutionPurpose.DIAGNOSTIC.value,
                input_digest=input_digest,
                baseline_actual=baseline_actual,
                baseline=baseline,
                events=events,
                degraded_reason=failure_reason,
                failed_record=record,
            )
        proposal_digest = canonical_digest(thaw_json(proposal))
        diagnostic = DiagnosticReceipt(
            run_id=run_id,
            task_id=task_id,
            pin_digest=pin.digest,
            input_snapshot_digest=input_digest,
            baseline_reference_digest=canonical_digest(baseline.to_dict()),
            proposal=proposal,
            proposal_digest=proposal_digest,
            no_effect_verified=True,
            owner_state_before_digest=before_digest,
            owner_state_after_digest=after_digest,
        )
        event = EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_id=f"event_policy_diagnostic_{diagnostic.digest[:24]}",
            event_type=EventType.SYSTEM_NOTICE,
            payload={
                "event_name": "phase2.mechanism_diagnostic",
                "diagnostic_receipt": diagnostic.to_dict(),
                "diagnostic_receipt_digest": diagnostic.digest,
                "executed": False,
                "actual_outcome_recorded": False,
            },
        )
        self._emit(event, events)
        receipt = MechanismExecutionReceipt(
            run_id=run_id,
            task_id=task_id,
            pin=pin,
            input_snapshot_digest=input_digest,
            requested_mode=ResolutionPurpose.DIAGNOSTIC.value,
            actual_profile_id="phase1_deterministic_baseline",
            actual_version=(
                self.registry.fallback_version
                if self.registry is not None
                else "phase1_deterministic_baseline"
            ),
            actual_decision_digest=canonical_digest(
                thaw_json(baseline_actual)
            ),
            baseline_reference_digest=canonical_digest(baseline.to_dict()),
            diagnostic_receipt_digest=diagnostic.digest,
            degraded=False,
            degraded_reason="",
            fallback_executed=False,
            outcome_attribution="baseline_with_read_only_diagnostic",
            actual_outcome_recorded=True,
            strongest_success_eligible=False,
        )
        return PolicyRuntimeResult(
            pin=pin,
            actual_decision=baseline_actual,
            baseline_reference=baseline,
            execution_receipt=receipt,
            diagnostic_receipt=diagnostic,
            events=tuple(events),
        )

    def _baseline_result(
        self,
        *,
        run_id: str,
        task_id: str,
        frozen_input: Any,
        input_digest: str,
        pin: MechanismRunPin,
        requested_mode: str,
        events: list[EventRecord],
        degraded_reason: str = "",
        failed_record: MechanismRegistration | None = None,
    ) -> PolicyRuntimeResult:
        actual = freeze_json(
            _mapping_result(self.baseline_executor(frozen_input))
        )
        baseline = BaselineDecisionReference.create(
            profile_id="phase1_deterministic_baseline",
            input_snapshot_digest=input_digest,
            decision=_mapping_result(thaw_json(actual)),
            executed=True,
            side_effect_free=False,
        )
        return self._baseline_result_from_actual(
            run_id=run_id,
            task_id=task_id,
            pin=pin,
            requested_mode=requested_mode,
            input_digest=input_digest,
            baseline_actual=actual,
            baseline=baseline,
            events=events,
            degraded_reason=degraded_reason,
            failed_record=failed_record,
        )

    def _baseline_result_from_actual(
        self,
        *,
        run_id: str,
        task_id: str,
        pin: MechanismRunPin,
        requested_mode: str,
        input_digest: str,
        baseline_actual: Any,
        baseline: BaselineDecisionReference,
        events: list[EventRecord],
        degraded_reason: str,
        failed_record: MechanismRegistration | None,
    ) -> PolicyRuntimeResult:
        if degraded_reason:
            event = self._degraded_event(
                run_id=run_id,
                task_id=task_id,
                failure_class=degraded_reason,
                fallback_profile="phase1_deterministic_baseline",
                input_digest=input_digest,
                failed_record=failed_record,
            )
            self._emit(event, events)
        receipt = MechanismExecutionReceipt(
            run_id=run_id,
            task_id=task_id,
            pin=pin,
            input_snapshot_digest=input_digest,
            requested_mode=requested_mode,
            actual_profile_id="phase1_deterministic_baseline",
            actual_version=(
                self.registry.fallback_version
                if self.registry is not None
                else "phase1_deterministic_baseline"
            ),
            actual_decision_digest=canonical_digest(
                thaw_json(baseline_actual)
            ),
            baseline_reference_digest=canonical_digest(baseline.to_dict()),
            diagnostic_receipt_digest="",
            degraded=bool(degraded_reason),
            degraded_reason=degraded_reason,
            fallback_executed=bool(degraded_reason),
            outcome_attribution=(
                "degraded_baseline" if degraded_reason else "baseline"
            ),
            actual_outcome_recorded=True,
            strongest_success_eligible=False,
        )
        return PolicyRuntimeResult(
            pin=pin,
            actual_decision=baseline_actual,
            baseline_reference=baseline,
            execution_receipt=receipt,
            diagnostic_receipt=None,
            events=tuple(events),
        )

    def _read_only_baseline_reference(
        self,
        frozen_input: Any,
        *,
        input_digest: str,
    ) -> BaselineDecisionReference:
        if self.baseline_reference is None:
            return _empty_baseline_reference(input_digest)
        before = freeze_json(self.owner_probe())
        value = self.baseline_reference(frozen_input)
        after = freeze_json(self.owner_probe())
        if canonical_digest(thaw_json(before)) != canonical_digest(
            thaw_json(after)
        ):
            raise PolicyRuntimeError(
                "baseline-reference-side-effect",
                "The frozen baseline reference changed owner state.",
            )
        return BaselineDecisionReference.create(
            profile_id="phase1_deterministic_baseline",
            input_snapshot_digest=input_digest,
            decision=_mapping_result(value),
            executed=False,
            side_effect_free=True,
        )

    def _degraded_event(
        self,
        *,
        run_id: str,
        task_id: str,
        failure_class: str,
        fallback_profile: str,
        input_digest: str,
        failed_record: MechanismRegistration | None,
        owner_diff: Mapping[str, Any] | None = None,
    ) -> EventRecord:
        payload = {
            "schema": "zyra.phase2-mechanism-degraded/v1",
            "event_name": "phase2.mechanism_degraded",
            "failure_class": failure_class,
            "source_mechanism": (
                ""
                if failed_record is None
                else f"{failed_record.family}/{failed_record.version}"
            ),
            "source_profile_id": (
                "" if failed_record is None else failed_record.profile_id
            ),
            "input_snapshot_digest": input_digest,
            "config_digest": (
                "" if failed_record is None else failed_record.config_digest
            ),
            "readiness_digest": (
                "" if failed_record is None else failed_record.readiness_digest
            ),
            "fallback_profile": fallback_profile,
            "silent_fallback": False,
            "strongest_success_eligible": False,
            "outcome_attribution": "degraded_baseline",
            "owner_diff": dict(owner_diff or {}),
        }
        digest = canonical_digest(payload)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            event_id=f"event_mechanism_degraded_{digest[:24]}",
            event_type=EventType.SYSTEM_NOTICE,
            payload=payload,
        )

    def _emit(
        self,
        event: EventRecord,
        events: list[EventRecord],
    ) -> None:
        self.admit_event(event)
        events.append(event)


class TopologyComposerRuntime:
    """Runs the unified proposal through the sole projector/custody path."""

    def __init__(
        self,
        *,
        composer: TopologyPolicyComposer,
        projector: TopologyConstraintProjector,
        evidence_publisher: PolicyEvidencePublisher | None = None,
        admit_event: EventSink | None = None,
    ) -> None:
        self.composer = composer
        self.projector = projector
        self.evidence_publisher = evidence_publisher
        self.admit_event = admit_event or (lambda event: None)
        self._committed_signatures: dict[
            str,
            list[tuple[str, str, str]],
        ] = {}

    def execute(
        self,
        *,
        mode: str,
        trigger_kind: str,
        policy_input: PolicyInputSnapshot,
        current_graph: GraphStateSnapshot,
        arg_result: ARGTopologyRuntimeResult,
        card_result: CARDTopologyRuntimeResult,
        pruning_result: AgentPruneRuntimeResult,
        switches: TopologyLayerSwitches | None = None,
        conflict_strategy: GraphConflictStrategy = GraphConflictStrategy.REPLAN,
        scheduler_causal_refs: tuple[str, ...] = (),
        recovery_causal_refs: tuple[str, ...] = (),
        verification_ref: str = "GraphStateCustody.validate_graph",
    ) -> TopologyComposerRuntimeResult:
        if mode not in {"validation", "default", "diagnostic"}:
            raise PolicyRuntimeError(
                "composer_mode_invalid",
                "topology composer runtime mode is unsupported",
            )
        events: list[EventRecord] = []
        composition = self.composer.compose(
            policy_input=policy_input,
            current_graph=current_graph,
            arg_result=arg_result,
            card_result=card_result,
            pruning_result=pruning_result,
            switches=switches,
        )
        self._emit_many(composition.events, events)
        memory_refs = tuple(item.ref_id for item in policy_input.memory_refs)
        if composition.degraded or composition.proposal is None:
            return TopologyComposerRuntimeResult(
                mode=mode,
                trigger_kind=trigger_kind,
                composition=composition,
                projection=None,
                outcome=None,
                published_evidence=(),
                scheduler_causal_refs=scheduler_causal_refs,
                memory_causal_refs=memory_refs,
                recovery_causal_refs=recovery_causal_refs,
                permission_result="not_evaluated_baseline_required",
                graph_revision_before=current_graph.revision,
                graph_revision_after=self.projector.custody.current(
                    current_graph.graph_id
                ).revision,
                degraded=True,
                degraded_reason=composition.degraded_reason,
                events=tuple(events),
            )
        if mode == "diagnostic":
            degraded = replace(
                composition,
                proposal=None,
                degraded=True,
                degraded_reason="diagnostic_canonical_mutation_forbidden",
            )
            event = self._degraded_event(
                policy_input=policy_input,
                trigger_kind=trigger_kind,
                reason=degraded.degraded_reason,
                graph_revision=current_graph.revision,
            )
            self._emit_many((event,), events)
            return TopologyComposerRuntimeResult(
                mode=mode,
                trigger_kind=trigger_kind,
                composition=degraded,
                projection=None,
                outcome=None,
                published_evidence=(),
                scheduler_causal_refs=scheduler_causal_refs,
                memory_causal_refs=memory_refs,
                recovery_causal_refs=recovery_causal_refs,
                permission_result="diagnostic_no_effect",
                graph_revision_before=current_graph.revision,
                graph_revision_after=current_graph.revision,
                degraded=False,
                degraded_reason="",
                events=tuple(events),
            )

        proposal = composition.proposal
        decision_id = "decision_topology_composer_" + proposal.digest[:24]
        projection = self.projector.execute(
            policy_input,
            proposal,
            decision_id=decision_id,
            strategy=conflict_strategy,
            execution_mode=mode,
            commit_guard=lambda before, after, delta: self._commit_guard(
                policy_input=policy_input,
                before_signature=before.signature,
                after_signature=after.signature,
                recovery_minimum_dwell_exempt=(
                    trigger_kind == "recovery"
                    and bool(recovery_causal_refs)
                ),
            ),
        )
        decision_event = EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_topology_decision_" + projection.receipt.digest[:24],
            event_type=EventType.CONSTRAINT_CHECK,
            payload={
                "schema": "zyra.topology-composer-decision/v1",
                "trigger_kind": trigger_kind,
                "proposal_id": proposal.proposal_id,
                "proposal_digest": proposal.digest,
                "decision_receipt": projection.receipt.to_dict(),
                "decision_receipt_digest": projection.receipt.digest,
                "graph_commit": dict(projection.receipt.graph_commit),
                "scheduler_causal_refs": list(scheduler_causal_refs),
                "memory_causal_refs": list(memory_refs),
                "recovery_causal_refs": list(recovery_causal_refs),
                "permission_constraint": next(
                    (
                        item.to_dict()
                        for item in projection.receipt.constraint_results
                        if item.constraint_id
                        == "permission_privacy_placement"
                    ),
                    {},
                ),
                "silent_fallback": False,
            },
        )
        self._emit_many((decision_event,), events)
        published: list[PublishedPolicyEvidence] = []
        if self.evidence_publisher is not None:
            published.append(
                self.evidence_publisher.publish(
                    proposal,
                    run_id=policy_input.run_id,
                    task_id=policy_input.task_id,
                )
            )
            published.append(
                self.evidence_publisher.publish(
                    projection.receipt,
                    run_id=policy_input.run_id,
                    task_id=policy_input.task_id,
                )
            )

        permission_result = self._permission_result(projection)
        after = self.projector.custody.current(current_graph.graph_id)
        outcome = self._outcome(
            policy_input=policy_input,
            trigger_kind=trigger_kind,
            proposal_ref=proposal.proposal_id,
            decision_ref=projection.receipt.decision_id,
            projection=projection,
            published=published,
            permission_result=permission_result,
            scheduler_causal_refs=scheduler_causal_refs,
            memory_causal_refs=memory_refs,
            recovery_causal_refs=recovery_causal_refs,
            verification_ref=verification_ref,
            graph_revision_before=current_graph.revision,
            graph_revision_after=after.revision,
        )
        if self.evidence_publisher is not None:
            published.append(
                self.evidence_publisher.publish(
                    outcome,
                    run_id=policy_input.run_id,
                    task_id=policy_input.task_id,
                )
            )
        accepted = projection.receipt.disposition in {
            PolicyDecisionDisposition.ACCEPT,
            PolicyDecisionDisposition.REBASE,
            PolicyDecisionDisposition.REPLAY,
        }
        degraded_reason = (
            ""
            if accepted
            else (
                projection.receipt.fallback_reason
                or projection.receipt.disposition.value
            )
        )
        if degraded_reason:
            self._emit_many(
                (
                    self._degraded_event(
                        policy_input=policy_input,
                        trigger_kind=trigger_kind,
                        reason=degraded_reason,
                        graph_revision=after.revision,
                    ),
                ),
                events,
            )
        if (
            projection.commit is not None
            and projection.commit.receipt.status
            in {
                GraphCommitStatus.COMMITTED,
                GraphCommitStatus.REBASED,
            }
        ):
            history = self._committed_signatures.setdefault(
                policy_input.run_id,
                [],
            )
            history.append(
                (
                    projection.commit.snapshot.created_at,
                    current_graph.signature,
                    projection.commit.snapshot.signature,
                )
            )
            del history[
                : max(
                    0,
                    len(history)
                    - self.composer.config.maximum_commits_per_window,
                )
            ]
        return TopologyComposerRuntimeResult(
            mode=mode,
            trigger_kind=trigger_kind,
            composition=composition,
            projection=projection,
            outcome=outcome,
            published_evidence=tuple(published),
            scheduler_causal_refs=scheduler_causal_refs,
            memory_causal_refs=memory_refs,
            recovery_causal_refs=recovery_causal_refs,
            permission_result=permission_result,
            graph_revision_before=current_graph.revision,
            graph_revision_after=after.revision,
            degraded=bool(degraded_reason),
            degraded_reason=degraded_reason,
            events=tuple(events),
        )

    @staticmethod
    def _permission_result(
        projection: PolicyProjectionResult,
    ) -> str:
        result = next(
            (
                item
                for item in projection.receipt.constraint_results
                if item.constraint_id == "permission_privacy_placement"
            ),
            None,
        )
        if result is None:
            return "permission_constraint_missing"
        return result.reason_code

    @staticmethod
    def _outcome(
        *,
        policy_input: PolicyInputSnapshot,
        trigger_kind: str,
        proposal_ref: str,
        decision_ref: str,
        projection: PolicyProjectionResult,
        published: list[PublishedPolicyEvidence],
        permission_result: str,
        scheduler_causal_refs: tuple[str, ...],
        memory_causal_refs: tuple[str, ...],
        recovery_causal_refs: tuple[str, ...],
        verification_ref: str,
        graph_revision_before: int,
        graph_revision_after: int,
    ) -> PolicyOutcome:
        commit = projection.receipt.graph_commit
        commit_ref = str(
            commit.get("commit_id")
            or commit.get("delta_id")
            or projection.receipt.delta_id
            or "no_commit"
        )
        accepted = projection.receipt.accepted
        flattened_refs = [
            policy_input.header.causation_id,
            policy_input.header.source_event_id,
            *scheduler_causal_refs,
            *memory_causal_refs,
            *recovery_causal_refs,
            verification_ref,
        ]
        for item in projection.receipt.constraint_results:
            flattened_refs.extend(item.evidence_refs)
        header = ContractHeader(
            contract_id="outcome_topology_" + projection.receipt.digest[:24],
            created_at=projection.receipt.header.created_at,
            source_event_id=policy_input.header.source_event_id,
            correlation_id=policy_input.header.correlation_id,
            causation_id=projection.receipt.decision_id,
            mechanism_id=projection.receipt.header.mechanism_id,
            mechanism_version=projection.receipt.header.mechanism_version,
            input_version=PolicyInputSnapshot.SCHEMA_VERSION,
            idempotency_key=(
                f"outcome:{projection.receipt.header.idempotency_key}"
            ),
            configuration_digest=(
                projection.receipt.header.configuration_digest
            ),
        )
        return PolicyOutcome(
            header=header,
            proposal_ref=proposal_ref,
            decision_ref=decision_ref,
            commit_ref=commit_ref,
            verifier_result=(
                "graph_commit_verified"
                if accepted
                else "no_commit_constraint_or_conflict"
            ),
            artifact_refs=tuple(item.artifact_ref for item in published),
            metrics=FrozenDict(
                {
                    "trigger_kind": trigger_kind,
                    "graph_revision_before": graph_revision_before,
                    "graph_revision_after": graph_revision_after,
                    "graph_mutated": (
                        graph_revision_after > graph_revision_before
                    ),
                    "operation_count": len(
                        projection.receipt.projected_operations
                    ),
                    "constraint_pass_count": sum(
                        item.passed
                        for item in projection.receipt.constraint_results
                    ),
                    "constraint_failure_count": sum(
                        not item.passed
                        for item in projection.receipt.constraint_results
                    ),
                    "fallback_profile": (
                        projection.receipt.fallback_profile
                    ),
                    "fallback_reason": (
                        projection.receipt.fallback_reason
                    ),
                    "verification_ref": verification_ref,
                }
            ),
            permission_result=permission_result,
            recovery_result=(
                "recovery_causation_bound"
                if recovery_causal_refs
                else "not_a_recovery_trigger"
            ),
            causal_refs=tuple(sorted(set(flattened_refs))),
        )

    def _commit_guard(
        self,
        *,
        policy_input: PolicyInputSnapshot,
        before_signature: str,
        after_signature: str,
        recovery_minimum_dwell_exempt: bool = False,
    ) -> ConstraintResult | None:
        observed_at = datetime.now(UTC)
        history = list(
            self._committed_signatures.get(policy_input.run_id, [])
        )
        try:
            snapshots = self.projector.custody.store.list_snapshots(
                policy_input.graph.graph_id
            )
        except Exception as exc:  # noqa: BLE001 - stability history is a hard gate.
            return ConstraintResult(
                constraint_id="composer_run_stability_history",
                passed=False,
                reason_code="composer_stability_history_unavailable",
                message=(
                    "durable graph history is required for run-level "
                    "stability enforcement"
                ),
                details=FrozenDict(
                    {"error_type": type(exc).__name__}
                ),
            )
        history.extend(
            (
                selected.created_at,
                previous.signature,
                selected.signature,
            )
            for previous, selected in zip(
                snapshots,
                snapshots[1:],
                strict=False,
            )
            if str(selected.metadata.get("last_branch_id") or "").startswith(
                "policy:"
            )
        )
        history = list(
            {
                (created_at, previous, selected): (
                    created_at,
                    previous,
                    selected,
                )
                for created_at, previous, selected in history
            }.values()
        )
        within_window = [
            item
            for item in history
            if 0
            <= (
                observed_at
                - datetime.fromisoformat(
                    item[0].replace("Z", "+00:00")
                ).astimezone(UTC)
            ).total_seconds()
            <= self.composer.config.churn_window_seconds
        ]
        within_window.sort(
            key=lambda item: datetime.fromisoformat(
                item[0].replace("Z", "+00:00")
            ).astimezone(UTC)
        )
        self._committed_signatures[policy_input.run_id] = within_window
        if len(within_window) >= (
            self.composer.config.maximum_commits_per_window
        ):
            return ConstraintResult(
                constraint_id="composer_run_churn_window",
                passed=False,
                reason_code="composer_churn_window_limit",
                message=(
                    "run-level topology commit count exceeded the "
                    "configured stability window"
                ),
                details=FrozenDict(
                    {
                        "commit_count": len(within_window),
                        "maximum_commits": (
                            self.composer.config.maximum_commits_per_window
                        ),
                        "window_seconds": (
                            self.composer.config.churn_window_seconds
                        ),
                    }
                ),
            )
        prior_signatures = {
            signature
            for _, previous, selected in within_window
            for signature in (previous, selected)
        }
        if (
            after_signature != before_signature
            and after_signature in prior_signatures
            and (
                not within_window
                or after_signature != within_window[-1][2]
            )
        ):
            return ConstraintResult(
                constraint_id="composer_run_oscillation",
                passed=False,
                reason_code="topology_oscillation_detected",
                message=(
                    "the projected topology would return to a recent "
                    "run-level signature"
                ),
                details=FrozenDict(
                    {
                        "before_signature": before_signature,
                        "after_signature": after_signature,
                        "window_seconds": (
                            self.composer.config.churn_window_seconds
                        ),
                    }
                ),
            )
        if within_window and not recovery_minimum_dwell_exempt:
            dwell_seconds = (
                observed_at
                - datetime.fromisoformat(
                    within_window[-1][0].replace("Z", "+00:00")
                ).astimezone(UTC)
            ).total_seconds()
            if dwell_seconds < self.composer.config.minimum_dwell_seconds:
                return ConstraintResult(
                    constraint_id="composer_run_minimum_dwell",
                    passed=False,
                    reason_code="composer_minimum_dwell_not_met",
                    message=(
                        "the durable run-level topology commit has not "
                        "satisfied the configured minimum dwell"
                    ),
                    details=FrozenDict(
                        {
                            "dwell_seconds": round(dwell_seconds, 6),
                            "minimum_dwell_seconds": (
                                self.composer.config.minimum_dwell_seconds
                            ),
                        }
                    ),
                )
        return ConstraintResult(
            constraint_id="composer_run_stability",
            passed=True,
            reason_code="run_stability_guard_passed",
            message="run-level churn and oscillation guards allow the commit",
            details=FrozenDict(
                {
                    "commit_count": len(within_window),
                    "before_signature": before_signature,
                    "after_signature": after_signature,
                    "recovery_minimum_dwell_exempt": (
                        recovery_minimum_dwell_exempt
                    ),
                }
            ),
        )

    @staticmethod
    def _degraded_event(
        *,
        policy_input: PolicyInputSnapshot,
        trigger_kind: str,
        reason: str,
        graph_revision: int,
    ) -> EventRecord:
        payload = {
            "schema": "zyra.topology-composer-runtime-degraded/v1",
            "trigger_kind": trigger_kind,
            "reason": reason,
            "fallback_profile": "phase1_deterministic_baseline",
            "silent_fallback": False,
            "strongest_success_eligible": False,
            "graph_revision": graph_revision,
        }
        return EventRecord(
            run_id=policy_input.run_id,
            task_id=policy_input.task_id,
            event_id="event_topology_runtime_degraded_"
            + canonical_digest(payload)[:24],
            event_type=EventType.RECOVERY_PLANNED,
            payload=payload,
        )

    def _emit_many(
        self,
        selected: tuple[EventRecord, ...],
        events: list[EventRecord],
    ) -> None:
        for event in selected:
            self.admit_event(event)
            events.append(event)


def _freeze_input(
    input_snapshot: PolicyInputSnapshot | Mapping[str, Any],
) -> tuple[Any, str]:
    if isinstance(input_snapshot, PolicyInputSnapshot):
        return freeze_json(input_snapshot.to_dict()), input_snapshot.digest
    frozen = freeze_json(_mapping_result(input_snapshot))
    return frozen, canonical_digest(thaw_json(frozen))


def _mapping_result(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PolicyRuntimeError(
            "mechanism-result-invalid",
            "Policy executors must return an object.",
        )
    return value


def _empty_baseline_reference(
    input_digest: str,
) -> BaselineDecisionReference:
    return BaselineDecisionReference.create(
        profile_id="phase1_deterministic_baseline",
        input_snapshot_digest=input_digest,
        decision={"available": False, "reason": "not_requested"},
        executed=False,
        side_effect_free=True,
    )


def _owner_diff(before: Any, after: Any) -> dict[str, Any]:
    left = thaw_json(before)
    right = thaw_json(after)
    if not isinstance(left, Mapping) or not isinstance(right, Mapping):
        return {"before": left, "after": right}
    keys = sorted(set(left) | set(right))
    return {
        key: {"before": left.get(key), "after": right.get(key)}
        for key in keys
        if left.get(key) != right.get(key)
    }


def _disconnected_baseline_pin(run_id: str) -> MechanismRunPin:
    schema_digest = canonical_digest(
        {"schema": "zyra.phase2-policy-runtime-fallback/v1"}
    )
    source_digest = canonical_digest(
        {"source": "phase1_deterministic_baseline"}
    )
    config_digest = canonical_digest(
        {"profile_id": "phase1_deterministic_baseline"}
    )
    readiness_digest = canonical_digest([])
    registration_digest = canonical_digest(
        {
            "family": "topology_policy",
            "version": "phase1_deterministic_baseline",
            "schema_digest": schema_digest,
            "source_digest": source_digest,
            "config_digest": config_digest,
        }
    )
    registry_digest = canonical_digest(
        {"state": "disconnected_explicit_baseline"}
    )
    return MechanismRunPin(
        run_id=run_id,
        family="topology_policy",
        version="phase1_deterministic_baseline",
        profile_id="phase1_deterministic_baseline",
        lifecycle_at_pin=MechanismLifecycle.BASELINE,
        schema_version="v1",
        schema_digest=schema_digest,
        source_digest=source_digest,
        config_digest=config_digest,
        readiness_digest=readiness_digest,
        registration_digest=registration_digest,
        registry_digest=registry_digest,
        registry_revision=0,
    )


__all__ = [
    "BaselineDecisionReference",
    "DiagnosticMutationError",
    "DiagnosticReceipt",
    "MechanismExecutionReceipt",
    "MechanismInvocation",
    "PolicyRuntimeError",
    "PolicyRuntimeResult",
    "TopologyPolicyRuntime",
    "TopologyComposerRuntime",
    "TopologyComposerRuntimeResult",
]
