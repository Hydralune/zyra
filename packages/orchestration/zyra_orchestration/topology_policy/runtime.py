from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Any

from zyra_core import EventRecord, EventType

from .contracts import (
    FrozenDict,
    PolicyInputSnapshot,
    canonical_digest,
    freeze_json,
    thaw_json,
)
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
]
