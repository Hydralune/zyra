from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    ContinuationMode,
    CorrelationRefs,
    FaultDisposition,
    FaultInjectionRequest,
    FaultKind,
    FaultSeverity,
    FaultSignal,
    InjectionKind,
    InjectionPhase,
    InjectionTransition,
    ObservationCategory,
    ObservationProvenance,
    ObserverDescriptor,
    ObserverLifecycle,
    ObserverMaturity,
    ObserverState,
    RecoveryHandoff,
    SignalOrigin,
    StructuredObservation,
)


def refs_from_dict(value: Mapping[str, Any]) -> CorrelationRefs:
    return CorrelationRefs(
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        session_id=str(value.get("session_id") or ""),
        node_id=str(value.get("node_id") or ""),
        attempt_id=str(value.get("attempt_id") or ""),
        tool_call_id=str(value.get("tool_call_id") or ""),
        tool_name=str(value.get("tool_name") or ""),
        worker_id=str(value.get("worker_id") or ""),
        backend_id=str(value.get("backend_id") or ""),
        provider_id=str(value.get("provider_id") or ""),
        workspace_id=str(value.get("workspace_id") or ""),
        browser_session_id=str(value.get("browser_session_id") or ""),
        mcp_server_id=str(value.get("mcp_server_id") or ""),
        subagent_task_id=str(value.get("subagent_task_id") or ""),
        observation_id=str(value.get("observation_id") or ""),
        source_state_revision=int(value.get("source_state_revision") or 0),
    )


def provenance_from_dict(value: Mapping[str, Any]) -> ObservationProvenance:
    return ObservationProvenance(
        observer_id=str(value.get("observer_id") or ""),
        source_repo=str(value.get("source_repo") or ""),
        source_revision=str(value.get("source_revision") or ""),
        observation_point=str(value.get("observation_point") or ""),
        maturity=ObserverMaturity(str(value.get("maturity") or ObserverMaturity.EXPERIMENTAL.value)),
        adapter_version=str(value.get("adapter_version") or "M1-S07B-01"),
        injection_id=str(value.get("injection_id") or ""),
    )


def observation_from_dict(value: Mapping[str, Any]) -> StructuredObservation:
    return StructuredObservation(
        category=ObservationCategory(str(value.get("category") or "")),
        code=str(value.get("code") or ""),
        refs=refs_from_dict(_as_mapping(value.get("refs"))),
        provenance=provenance_from_dict(_as_mapping(value.get("provenance"))),
        summary=str(value.get("summary") or ""),
        observed_at=str(value.get("observed_at") or ""),
        status=str(value.get("status") or ""),
        error_type=str(value.get("error_type") or ""),
        status_code=(int(value["status_code"]) if value.get("status_code") is not None else None),
        retryable_hint=(bool(value["retryable_hint"]) if value.get("retryable_hint") is not None else None),
        terminal_hint=(bool(value["terminal_hint"]) if value.get("terminal_hint") is not None else None),
        elapsed_ms=(int(value["elapsed_ms"]) if value.get("elapsed_ms") is not None else None),
        deadline_ms=(int(value["deadline_ms"]) if value.get("deadline_ms") is not None else None),
        details=_as_mapping(value.get("details")),
        evidence_event_ids=tuple(str(item) for item in value.get("evidence_event_ids") or ()),
    )


def signal_from_dict(value: Mapping[str, Any]) -> FaultSignal:
    return FaultSignal(
        signal_id=str(value.get("signal_id") or ""),
        kind=FaultKind(str(value.get("kind") or FaultKind.UNKNOWN.value)),
        severity=FaultSeverity(str(value.get("severity") or FaultSeverity.ERROR.value)),
        disposition=FaultDisposition(str(value.get("disposition") or FaultDisposition.RECORD.value)),
        origin=SignalOrigin(str(value.get("origin") or SignalOrigin.OBSERVER.value)),
        refs=refs_from_dict(_as_mapping(value.get("refs"))),
        provenance=provenance_from_dict(_as_mapping(value.get("provenance"))),
        summary=str(value.get("summary") or ""),
        retryable=bool(value.get("retryable")),
        terminal=bool(value.get("terminal")),
        classification_rule=str(value.get("classification_rule") or ""),
        observed_code=str(value.get("observed_code") or ""),
        created_at=str(value.get("created_at") or ""),
        evidence_event_ids=tuple(str(item) for item in value.get("evidence_event_ids") or ()),
        details=_as_mapping(value.get("details")),
    )


def descriptor_from_dict(value: Mapping[str, Any]) -> ObserverDescriptor:
    return ObserverDescriptor(
        observer_id=str(value.get("observer_id") or ""),
        display_name=str(value.get("display_name") or ""),
        maturity=ObserverMaturity(str(value.get("maturity") or ObserverMaturity.EXPERIMENTAL.value)),
        attach_owner=str(value.get("attach_owner") or ""),
        lifecycle_owner=str(value.get("lifecycle_owner") or ""),
        observation_point=str(value.get("observation_point") or ""),
        source_repo=str(value.get("source_repo") or ""),
        source_revision=str(value.get("source_revision") or ""),
        emitted_kinds=tuple(FaultKind(str(item)) for item in value.get("emitted_kinds") or ()),
        categories=tuple(ObservationCategory(str(item)) for item in value.get("categories") or ()),
        enabled_by_default=bool(value.get("enabled_by_default")),
        descriptor_revision=int(value.get("descriptor_revision") or 1),
        metadata=_as_mapping(value.get("metadata")),
    )


def observer_state_from_dict(value: Mapping[str, Any]) -> ObserverState:
    return ObserverState(
        descriptor=descriptor_from_dict(_as_mapping(value.get("descriptor"))),
        lifecycle=ObserverLifecycle(str(value.get("lifecycle") or ObserverLifecycle.REGISTERED.value)),
        revision=int(value.get("revision") or 0),
        attach_count=int(value.get("attach_count") or 0),
        start_count=int(value.get("start_count") or 0),
        stop_count=int(value.get("stop_count") or 0),
        observation_count=int(value.get("observation_count") or 0),
        emitted_count=int(value.get("emitted_count") or 0),
        last_observation_id=str(value.get("last_observation_id") or ""),
        last_error=str(value.get("last_error") or ""),
        updated_at=str(value.get("updated_at") or ""),
    )


def injection_request_from_dict(value: Mapping[str, Any]) -> FaultInjectionRequest:
    return FaultInjectionRequest(
        injection_id=str(value.get("injection_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        kind=InjectionKind(str(value.get("kind") or "")),
        target=refs_from_dict(_as_mapping(value.get("target"))),
        requested_by=str(value.get("requested_by") or ""),
        idempotency_key=str(value.get("idempotency_key") or ""),
        continuation=ContinuationMode(str(value.get("continuation") or ContinuationMode.AUTO.value)),
        parameters=_as_mapping(value.get("parameters")),
        requested_at=str(value.get("requested_at") or ""),
    )


def transition_from_dict(value: Mapping[str, Any]) -> InjectionTransition:
    return InjectionTransition(
        transition_id=str(value.get("transition_id") or ""),
        injection_id=str(value.get("injection_id") or ""),
        phase=InjectionPhase(str(value.get("phase") or "")),
        revision=int(value.get("revision") or 0),
        reason=str(value.get("reason") or ""),
        signal_id=str(value.get("signal_id") or ""),
        event_id=str(value.get("event_id") or ""),
        handoff_id=str(value.get("handoff_id") or ""),
        created_at=str(value.get("created_at") or ""),
        details=_as_mapping(value.get("details")),
    )


def handoff_from_dict(value: Mapping[str, Any]) -> RecoveryHandoff:
    return RecoveryHandoff(
        handoff_id=str(value.get("handoff_id") or ""),
        run_id=str(value.get("run_id") or ""),
        task_id=str(value.get("task_id") or ""),
        signal_id=str(value.get("signal_id") or ""),
        fault_kind=FaultKind(str(value.get("fault_kind") or "")),
        refs=refs_from_dict(_as_mapping(value.get("refs"))),
        requested_actions=tuple(str(item) for item in value.get("requested_actions") or ()),
        recoverable=bool(value.get("recoverable")),
        reason=str(value.get("reason") or ""),
        injection_id=str(value.get("injection_id") or ""),
        created_at=str(value.get("created_at") or ""),
        metadata=_as_mapping(value.get("metadata")),
    )


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}
