from __future__ import annotations

import base64
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .defaults import ensure_default_backends
from .models import (
    BackendDispatchError,
    BackendFailureKind,
    BackendKind,
    BackendLocation,
    BackendRecoveryIntent,
    BackendSelectionRequest,
)
from .registry import BackendRegistry
from .router import WorkerDispatchRouter
from .store import BackendRegistryStore


TERMINAL_ACTION_CAPABILITIES: Mapping[str, str] = {
    "file_read": "code-change",
    "file_write": "code-change",
    "file_edit": "code-change",
    "file_delete": "code-change",
    "web_search": "code-change",
    "shell": "shell",
    "artifact_write": "artifact",
}


@dataclass(frozen=True, slots=True)
class BackendActionRoute:
    route_id: str
    route_checksum: str
    catalog_revision: int
    credential_version: int
    credential_fingerprint: str
    transport_id: str
    turn_id: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "BackendActionRoute":
        return cls(
            route_id=str(value.get("route_id") or ""),
            route_checksum=str(value.get("route_checksum") or value.get("checksum") or ""),
            catalog_revision=int(value.get("catalog_revision") or 0),
            credential_version=int(value.get("credential_version") or 0),
            credential_fingerprint=str(value.get("credential_fingerprint") or ""),
            transport_id=str(value.get("transport_id") or ""),
            turn_id=str(value.get("turn_id") or ""),
        )

    @property
    def valid(self) -> bool:
        return bool(
            self.route_id
            and self.route_checksum
            and self.catalog_revision > 0
            and self.credential_version > 0
            and self.credential_fingerprint
            and self.transport_id
            and self.turn_id
        )


class BackendRegistryActionDispatchPort:
    """Delegate one permission-approved physical tool action.

    The port owns neither permission nor placement. It accepts only a safe
    permission consumption receipt, restricts payload dispatch to registered
    terminal definitions, and leaves selection, lease, attestation, transport,
    cancellation, retry, and result materialization to ``WorkerDispatchRouter``.
    """

    def __init__(
        self,
        *,
        registry_path: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        route_resolver: Callable[[], Mapping[str, Any]],
        terminal_dispatch_enabled: Callable[[], bool] | None = None,
        runtime_worker: str = "CodeWorkerRuntime",
        excluded_backend_ids: Sequence[str] = (),
    ) -> None:
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.route_resolver = route_resolver
        self.terminal_dispatch_enabled = terminal_dispatch_enabled or (lambda: True)
        self.runtime_worker = str(runtime_worker)
        self.excluded_backend_ids = tuple(sorted({str(item) for item in excluded_backend_ids}))

    def handles(self, tool_name: str) -> bool:
        return str(tool_name) in TERMINAL_ACTION_CAPABILITIES

    def available(self, tool_name: str) -> bool:
        return str(tool_name) in self.available_actions()

    def available_actions(self) -> tuple[str, ...]:
        if not self.terminal_dispatch_enabled() or not self._route().valid:
            return ()
        store = BackendRegistryStore(self.registry_path)
        try:
            registry = BackendRegistry(store)
            terminal_ids = set(registry.terminal_backend_ids())
            capabilities = {
                capability
                for definition in registry.definitions(runtime_worker=self.runtime_worker)
                if definition.backend_id in terminal_ids
                and definition.enabled
                and definition.runtime_worker == self.runtime_worker
                and definition.kind is BackendKind.EDGE_HTTP
                and definition.location is BackendLocation.LOCAL
                and definition.backend_id not in self.excluded_backend_ids
                for capability in definition.capabilities
            }
            return tuple(
                tool_name
                for tool_name, capability in TERMINAL_ACTION_CAPABILITIES.items()
                if capability in capabilities
            )
        finally:
            store.close()

    def dispatch_action(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        tool_name: str,
        tool_call_id: str,
        arguments: Mapping[str, Any],
        metadata: Mapping[str, Any],
        permission_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        capability = TERMINAL_ACTION_CAPABILITIES.get(str(tool_name))
        if capability is None:
            raise ValueError(f"terminal action is not supported: {tool_name}")
        if not self.terminal_dispatch_enabled():
            raise ValueError("terminal action dispatch is excluded by execution mode")
        receipt_id = str(permission_receipt.get("receipt_id") or "")
        if not receipt_id or permission_receipt.get("allowed") is not True:
            raise ValueError("terminal action requires an allowed permission receipt")
        route = self._route()
        if not route.valid:
            raise ValueError("terminal action requires a complete provider route projection")

        store = BackendRegistryStore(self.registry_path)
        try:
            registry = BackendRegistry(store)
            ensure_default_backends(registry)
            terminal_ids = set(registry.terminal_backend_ids())
            definitions = registry.definitions(runtime_worker=self.runtime_worker)
            eligible_terminal_ids = {
                definition.backend_id
                for definition in definitions
                if definition.backend_id in terminal_ids
                and definition.enabled
                and definition.kind is BackendKind.EDGE_HTTP
                and definition.location is BackendLocation.LOCAL
                and capability in definition.capabilities
                and definition.backend_id not in self.excluded_backend_ids
            }
            if not eligible_terminal_ids:
                raise BackendDispatchError(
                    BackendFailureKind.BACKEND_UNAVAILABLE,
                    f"no terminal backend can execute {tool_name}",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                )
            excluded = {
                definition.backend_id
                for definition in definitions
                if definition.backend_id not in eligible_terminal_ids
            }
            excluded.update(self.excluded_backend_ids)
            request = BackendSelectionRequest(
                run_id=str(run_id),
                task_id=str(task_id),
                node_id=(str(node_id) if node_id else None),
                runtime_worker=self.runtime_worker,
                preferred_backend_id=None,
                required_capabilities=(capability,),
                allowed_locations=(BackendLocation.LOCAL,),
                excluded_backend_ids=tuple(sorted(excluded)),
                workspace_root=str(self.workspace_root),
                artifact_root=str(self.artifact_root),
                provider_route_id=route.route_id,
                provider_route_checksum=route.route_checksum,
                provider_catalog_revision=route.catalog_revision,
                provider_credential_version=route.credential_version,
                provider_credential_fingerprint=route.credential_fingerprint,
                provider_transport_id=route.transport_id,
                m0_execution_ref=f"tool_call:{tool_call_id}",
                turn_id=route.turn_id,
                metadata={
                    "source": "zyra_runtime.ToolExecutor",
                    "physical_action": True,
                    "tool_name": str(tool_name),
                    "permission_receipt_id": receipt_id,
                    "payload_only": True,
                },
            )
            payload = {
                "schema": "zyra.terminal-action/v1",
                "tool_name": str(tool_name),
                "tool_call_id": str(tool_call_id),
                "arguments": _json_value(arguments),
                "metadata": _safe_metadata(metadata),
                "permission": _safe_permission_receipt(permission_receipt),
            }
            outcome = WorkerDispatchRouter(registry).dispatch_payload(
                request,
                operation_name=f"tool.{tool_name}",
                payload=payload,
                idempotency_key=f"terminal-action:{tool_call_id}",
            )
            tool_result = dict(outcome.value)
            if tool_result.get("schema") != "zyra.terminal-action-result/v1":
                raise BackendDispatchError(
                    BackendFailureKind.BACKEND_PROTOCOL,
                    "terminal action result schema is invalid",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                    backend_id=outcome.final_lease.backend_id,
                    lease_id=outcome.final_lease.lease_id,
                )
            if str(tool_result.get("tool_call_id") or "") != str(tool_call_id):
                raise BackendDispatchError(
                    BackendFailureKind.BACKEND_PROTOCOL,
                    "terminal action result tool-call identity changed",
                    retryable=False,
                    recovery_intent=BackendRecoveryIntent.NONE,
                    backend_id=outcome.final_lease.backend_id,
                    lease_id=outcome.final_lease.lease_id,
                )
            dispatch_receipt = {
                "schema": "zyra.backend-action-dispatch-receipt/v1",
                "state_owner": "python.BackendRegistryStore",
                "permission_owner": "typescript.PermissionCoordinator",
                "permission_receipt_id": receipt_id,
                "dispatch_session_id": outcome.session.session_id,
                "backend_lease_id": outcome.final_lease.lease_id,
                "backend_id": outcome.final_lease.backend_id,
                "backend_kind": outcome.final_envelope.backend_kind.value,
                "backend_location": outcome.final_envelope.backend_location.value,
                "envelope_id": outcome.final_envelope.envelope_id,
                "operation": f"tool.{tool_name}",
                "attempt_ids": [item.attempt_id for item in outcome.attempts],
                "transport_receipt_ids": [
                    response.transport_receipt_id
                    for response in outcome.transport_responses
                ],
                "backend_changed": outcome.backend_changed,
                "workspace_root_projected": False,
                "artifact_root_projected": False,
            }
            return {
                "schema": "zyra.backend-action-dispatch-result/v1",
                "tool_result": tool_result,
                "dispatch_receipt": dispatch_receipt,
            }
        finally:
            store.close()

    def _route(self) -> BackendActionRoute:
        value = self.route_resolver()
        if not isinstance(value, Mapping):
            value = {}
        return BackendActionRoute.from_mapping(value)


def _safe_permission_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "receipt_id",
        "binding_id",
        "tool_call_id",
        "command_digest",
        "grant_digest",
        "allowed",
        "reason",
        "authority_type",
        "consumed_at",
    }
    return {str(key): _json_value(item) for key, item in value.items() if key in allowed}


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "correlation_id",
        "execution_owner",
        "gateway_original_tool",
        "namespace",
        "provenance_ref",
        "server_id",
        "server_name",
        "session_id",
        "tool_namespace",
        "worker_request_id",
    }
    return {
        str(key): _json_value(item)
        for key, item in value.items()
        if str(key) in allowed
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, bytes | bytearray):
        return {
            "encoding": "base64",
            "data": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    raise TypeError(f"terminal action payload is not JSON serializable: {type(value).__name__}")


__all__ = [
    "BackendActionRoute",
    "BackendRegistryActionDispatchPort",
    "TERMINAL_ACTION_CAPABILITIES",
]
