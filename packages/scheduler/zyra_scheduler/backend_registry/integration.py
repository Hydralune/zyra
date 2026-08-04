from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from zyra_core import EventRecord, EventType

from .defaults import backend_registry_path, ensure_default_backends
from .models import BackendControlEvent, BackendLocation, BackendSelectionRequest
from .registry import BackendRegistry
from .runtime import BackendDispatchOutcome, BackendDispatchRuntime
from .store import BackendRegistryStore


T = TypeVar("T")


def dispatch_worker_callable(
    *,
    run_id: str,
    task_id: str,
    node_id: str | None,
    runtime_worker: str,
    preferred_backend_id: str | None,
    workspace_root: str | Path,
    artifact_root: str | Path,
    provider_route_id: str | None,
    provider_route_checksum: str,
    provider_catalog_revision: int,
    provider_credential_version: int,
    provider_credential_fingerprint: str,
    provider_transport_id: str,
    m0_execution_ref: str,
    turn_id: str,
    operation: Callable[[Any], T],
    idempotency_key: str,
    required_capabilities: tuple[str, ...] = (),
    allowed_locations: tuple[BackendLocation, ...] = (),
    excluded_backend_ids: tuple[str, ...] = (),
    exclude_terminal_backends: bool = False,
    interruptible: bool = False,
) -> BackendDispatchOutcome[T]:
    store = BackendRegistryStore(backend_registry_path(artifact_root))
    try:
        registry = BackendRegistry(store)
        ensure_default_backends(registry)
        excluded = set(excluded_backend_ids)
        if exclude_terminal_backends:
            excluded.update(registry.terminal_backend_ids())
        request = BackendSelectionRequest(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            runtime_worker=runtime_worker,
            preferred_backend_id=preferred_backend_id,
            required_capabilities=required_capabilities,
            allowed_locations=allowed_locations,
            excluded_backend_ids=tuple(sorted(excluded)),
            workspace_root=str(Path(workspace_root).expanduser().resolve()),
            artifact_root=str(Path(artifact_root).expanduser().resolve()),
            provider_route_id=provider_route_id,
            provider_route_checksum=provider_route_checksum,
            provider_catalog_revision=provider_catalog_revision,
            provider_credential_version=provider_credential_version,
            provider_credential_fingerprint=provider_credential_fingerprint,
            provider_transport_id=provider_transport_id,
            m0_execution_ref=m0_execution_ref,
            turn_id=turn_id,
            metadata={
                "source": "zyra_orchestration.task_graph",
                "sealed_terminal_exclusion": exclude_terminal_backends,
                "excluded_backend_ids": tuple(sorted(excluded)),
            },
        )
        return BackendDispatchRuntime(registry).dispatch_callable(
            request,
            operation,
            idempotency_key=idempotency_key,
            interruptible=interruptible,
        )
    finally:
        store.close()


def event_record_from_backend(event: BackendControlEvent) -> EventRecord:
    return EventRecord(
        run_id=event.run_id,
        task_id=event.task_id,
        node_id=event.node_id,
        event_type=EventType.SYSTEM_NOTICE,
        payload={
            "backend_control": event.to_dict(),
            "backend_event_type": event.event_type,
            "backend_id": event.backend_id,
            "backend_lease_id": event.lease_id,
            "backend_dispatch_envelope_id": event.envelope_id,
            "provider_route_id": event.provider_route_id,
            "causation_id": event.causation_id,
            "correlation_id": event.correlation_id,
        },
    )
