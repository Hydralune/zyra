from __future__ import annotations

import threading
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_runtime.provider_control_plane import (
    ProviderControlPlaneClient,
    ProviderControlPlanePortError,
)
from zyra_scheduler.backend_registry import (
    BackendControlAction,
    BackendControlRuntime,
    BackendDispatchJournal,
    BackendHealthSupervisor,
    BackendDefinition,
    BackendRegistry,
    BackendRegistryStore,
    backend_registry_path,
    ensure_default_backends,
)


@dataclass(frozen=True, slots=True)
class ProviderBackendApiResponse:
    status: HTTPStatus
    body: Mapping[str, Any]
    headers: Mapping[str, str]


_CLIENT_LOCK = threading.RLock()
_CLIENT: ProviderControlPlaneClient | None = None
_CLIENT_KEY: tuple[str, str] | None = None


def get_provider_control_client(
    *,
    project_root: str | Path,
    database_path: str | Path,
) -> ProviderControlPlaneClient:
    global _CLIENT, _CLIENT_KEY
    key = (
        str(Path(project_root).expanduser().resolve()),
        str(Path(database_path).expanduser().resolve()),
    )
    with _CLIENT_LOCK:
        if _CLIENT is None or _CLIENT_KEY != key:
            if _CLIENT is not None:
                _CLIENT.close()
            _CLIENT = ProviderControlPlaneClient(
                project_root=key[0],
                database_path=key[1],
            )
            _CLIENT_KEY = key
        return _CLIENT


def reset_provider_control_client() -> None:
    global _CLIENT, _CLIENT_KEY
    with _CLIENT_LOCK:
        client = _CLIENT
        _CLIENT = None
        _CLIENT_KEY = None
    if client is not None:
        client.close()


class ProviderBackendApi:
    def __init__(
        self,
        *,
        project_root: str | Path,
        artifact_root: str | Path,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        self.provider_database = (
            self.artifact_root
            / ".provider-control-plane"
            / "provider.sqlite3"
        )

    def handle_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any],
    ) -> ProviderBackendApiResponse | None:
        try:
            if parts and parts[0] == "providers":
                client = self._client()
                if list(parts) == ["providers", "health"]:
                    result = client.health()
                elif list(parts) == ["providers", "catalog"]:
                    result = client.catalog.snapshot()
                elif list(parts) == ["providers"]:
                    result = client.catalog.providers(
                        available_only=_truthy(query.get("available_only")),
                    )
                elif list(parts) == ["providers", "models"]:
                    result = client.catalog.models(
                        provider_id=str(query.get("provider_id") or ""),
                        available_only=_truthy(query.get("available_only")),
                    )
                elif list(parts) == ["providers", "integrations"]:
                    result = client.integrations.list()
                elif list(parts) == ["providers", "credentials"]:
                    result = client.credentials.list(
                        provider_id=str(query.get("provider_id") or ""),
                    )
                elif list(parts) == ["providers", "routes"]:
                    result = client.routing.list(
                        run_id=str(query.get("run_id") or ""),
                        task_id=str(query.get("task_id") or ""),
                    )
                elif list(parts) == ["providers", "events"]:
                    result = client.store.events(
                        run_id=str(query.get("run_id") or ""),
                        task_id=str(query.get("task_id") or ""),
                    )
                elif list(parts) == ["providers", "compat-v1"]:
                    result = client.compat_v1.snapshot()
                else:
                    return None
                return self._ok(result, state_owner="typescript.ProviderControlPlaneStore")

            if parts and parts[0] == "backends":
                with self._backend_registry() as registry:
                    if list(parts) == ["backends"]:
                        result = [item.to_dict() for item in registry.definitions()]
                    elif list(parts) == ["backends", "health"]:
                        result = registry.summary()
                    elif list(parts) == ["backends", "events"]:
                        result = [
                            item.to_dict()
                            for item in registry.store.events(
                                run_id=str(query.get("run_id") or ""),
                                task_id=str(query.get("task_id") or ""),
                            )
                        ]
                    elif list(parts) == ["backends", "dispatch-sessions"]:
                        result = [
                            item.to_dict()
                            for item in registry.store.dispatch_sessions(
                                run_id=str(query.get("run_id") or ""),
                                task_id=str(query.get("task_id") or ""),
                                active_only=_truthy(query.get("active_only")),
                            )
                        ]
                    elif list(parts) == ["backends", "recovery-inputs"]:
                        result = [
                            item.to_dict()
                            for item in registry.store.recovery_inputs(
                                run_id=str(query.get("run_id") or ""),
                                task_id=str(query.get("task_id") or ""),
                                unconsumed_only=_truthy(query.get("unconsumed_only")),
                            )
                        ]
                    elif list(parts) == ["backends", "control-requests"]:
                        result = [
                            item.to_dict()
                            for item in registry.store.control_requests(
                                run_id=str(query.get("run_id") or ""),
                                task_id=str(query.get("task_id") or ""),
                            )
                        ]
                    elif len(parts) == 4 and list(parts[:2]) == ["backends", "dispatch-sessions"] and parts[3] == "replay":
                        result = BackendDispatchJournal(registry.store).replay_plan(parts[2]).to_dict()
                    elif len(parts) == 4 and list(parts[:2]) == ["backends", "dispatch-sessions"] and parts[3] == "verify":
                        result = BackendDispatchJournal(registry.store).verify(parts[2])
                    else:
                        return None
                return self._ok(result, state_owner="python.BackendRegistryStore")
        except ProviderControlPlanePortError as error:
            return self._provider_error(error)
        except (TypeError, ValueError, RuntimeError, KeyError) as error:
            return self._error(
                HTTPStatus.CONFLICT,
                "provider_backend_operation_rejected",
                str(error),
            )
        return None

    def handle_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any],
    ) -> ProviderBackendApiResponse | None:
        try:
            if parts and parts[0] == "providers":
                client = self._client()
                process = client.process
                if list(parts) == ["providers"]:
                    result = process.request("catalog.provider.upsert", {
                        "provider": _mapping(payload.get("provider"), "provider"),
                        "expectedRevision": payload.get("expected_revision"),
                    })
                elif list(parts) == ["providers", "models"]:
                    result = process.request("catalog.model.upsert", {
                        "model": _mapping(payload.get("model"), "model"),
                        "expectedRevision": payload.get("expected_revision"),
                    })
                elif list(parts) == ["providers", "integrations"]:
                    result = process.request("integration.upsert", {
                        "integration": _mapping(payload.get("integration"), "integration"),
                        "expectedRevision": payload.get("expected_revision"),
                    })
                elif list(parts) == ["providers", "credentials"]:
                    result = process.request("credential.register", {
                        "credential": _mapping(payload.get("credential"), "credential"),
                    })
                elif list(parts) == ["providers", "routes"]:
                    result = process.request("route.acquire", {
                        "request": _mapping(payload.get("request"), "request"),
                        "previousRouteId": payload.get("previous_route_id"),
                    })
                elif list(parts) == ["providers", "dispatch"]:
                    result = process.request("dispatch", {
                        "request": _mapping(payload.get("request"), "request"),
                    })
                elif (
                    len(parts) == 4
                    and list(parts[:2]) == ["providers", "credentials"]
                    and parts[3] in {"revoke", "block"}
                ):
                    operation = f"credential.{parts[3]}"
                    result = process.request(operation, {
                        "credentialId": parts[2],
                        "expectedVersion": payload.get("expected_version"),
                        "reason": payload.get("reason"),
                    })
                else:
                    return None
                return self._ok(result, state_owner="typescript.ProviderControlPlaneStore")

            if list(parts) == ["backends"]:
                definition = BackendDefinition.from_dict(
                    _mapping(payload.get("backend"), "backend")
                )
                with self._backend_registry() as registry:
                    revision = registry.register(
                        definition,
                        expected_revision=(
                            int(payload["expected_revision"])
                            if payload.get("expected_revision") is not None
                            else None
                        ),
                    )
                return self._ok(
                    {"backend_id": definition.backend_id, "registry_revision": revision},
                    state_owner="python.BackendRegistryStore",
                )
            if list(parts) == ["backends", "probe"]:
                with self._backend_registry() as registry:
                    batch = BackendHealthSupervisor(registry).probe_all(
                        runtime_worker=str(payload.get("runtime_worker") or ""),
                    )
                return self._ok(
                    batch.to_dict(),
                    state_owner="python.BackendRegistryStore",
                )
            if list(parts) == ["backends", "control"]:
                with self._backend_registry() as registry:
                    receipt = BackendControlRuntime(registry.store).submit(
                        action=BackendControlAction(str(payload.get("action") or "")),
                        run_id=str(payload.get("run_id") or ""),
                        task_id=str(payload.get("task_id") or ""),
                        reason=str(payload.get("reason") or "backend control request"),
                        requested_by=str(payload.get("requested_by") or "backend-control-api"),
                        idempotency_key=str(payload.get("idempotency_key") or ""),
                        turn_id=(str(payload["turn_id"]) if payload.get("turn_id") else None),
                        dispatch_session_id=(
                            str(payload["dispatch_session_id"])
                            if payload.get("dispatch_session_id")
                            else None
                        ),
                        backend_id=(
                            str(payload["backend_id"])
                            if payload.get("backend_id")
                            else None
                        ),
                        metadata=_mapping(payload.get("metadata") or {}, "metadata"),
                    )
                return self._ok(
                    receipt.to_dict(),
                    state_owner="python.BackendRegistryStore",
                )
        except ProviderControlPlanePortError as error:
            return self._provider_error(error)
        except (TypeError, ValueError, RuntimeError, KeyError) as error:
            return self._error(
                HTTPStatus.CONFLICT,
                "provider_backend_operation_rejected",
                str(error),
            )
        return None

    def _client(self) -> ProviderControlPlaneClient:
        return get_provider_control_client(
            project_root=self.project_root,
            database_path=self.provider_database,
        )

    def _backend_registry(self) -> _BackendRegistryContext:
        return _BackendRegistryContext(backend_registry_path(self.artifact_root))

    @staticmethod
    def _ok(result: Any, *, state_owner: str) -> ProviderBackendApiResponse:
        return ProviderBackendApiResponse(
            status=HTTPStatus.OK,
            body={
                "ok": True,
                "schema": "zyra.provider-backend-api/v1",
                "state_owner": state_owner,
                "provider_backend_owner_separation": True,
                "result": result,
            },
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Pragma": "no-cache",
                "X-Zyra-State-Owner": state_owner,
            },
        )

    @staticmethod
    def _provider_error(error: ProviderControlPlanePortError) -> ProviderBackendApiResponse:
        status = (
            HTTPStatus.NOT_FOUND
            if "not_found" in error.code
            else HTTPStatus.FORBIDDEN
            if error.code in {"credential_blocked", "credential_revoked", "authorization_failed"}
            else HTTPStatus.CONFLICT
        )
        return ProviderBackendApi._error(status, error.code, str(error), detail=error.detail)

    @staticmethod
    def _error(
        status: HTTPStatus,
        code: str,
        message: str,
        *,
        detail: Mapping[str, Any] | None = None,
    ) -> ProviderBackendApiResponse:
        return ProviderBackendApiResponse(
            status=status,
            body={
                "ok": False,
                "schema": "zyra.provider-backend-api/v1",
                "error": code,
                "message": message,
                "detail": dict(detail or {}),
            },
            headers={"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"},
        )


class _BackendRegistryContext:
    def __init__(self, path: Path) -> None:
        self.store = BackendRegistryStore(path)
        self.registry = BackendRegistry(self.store)

    def __enter__(self) -> BackendRegistry:
        ensure_default_backends(self.registry)
        return self.registry

    def __exit__(self, *_: object) -> None:
        self.store.close()


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    return dict(value)


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
