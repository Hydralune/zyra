from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import threading
from dataclasses import dataclass, replace
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

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
    BackendKind,
    BackendLocation,
    RemoteBackendControlClient,
    RemoteBackendControlError,
    backend_registry_path,
    ensure_default_backends,
)


class TerminalRegistrationError(ValueError):
    def __init__(self, code: str, message: str, *, status: HTTPStatus = HTTPStatus.UNPROCESSABLE_ENTITY) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


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
        provider_database: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.artifact_root = Path(artifact_root).expanduser().resolve()
        selected_database = (
            provider_database
            if provider_database is not None
            else self.artifact_root
            / ".provider-control-plane"
            / "provider.sqlite3"
        )
        self.provider_database = Path(selected_database).expanduser().resolve()

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
                        result = [item.to_public_dict() for item in registry.definitions()]
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
                if payload.get("expected_revision") is None:
                    raise TerminalRegistrationError(
                        "terminal_registration_revision_required",
                        "Terminal backend registration requires expected_revision.",
                    )
                expected_revision = int(payload["expected_revision"])
                registration = _mapping(
                    payload.get("terminal_registration"),
                    "terminal_registration",
                )
                with self._backend_registry() as registry:
                    definition = _validate_terminal_registration(
                        registry,
                        definition,
                        registration,
                    )
                    revision = registry.register(
                        definition,
                        expected_revision=expected_revision,
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
        except TerminalRegistrationError as error:
            return self._error(error.status, error.code, str(error))
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


def _validate_terminal_registration(
    registry: BackendRegistry,
    definition: BackendDefinition,
    registration: Mapping[str, Any],
) -> BackendDefinition:
    if definition.kind is not BackendKind.EDGE_HTTP or definition.location is not BackendLocation.LOCAL:
        raise TerminalRegistrationError(
            "terminal_registration_scope_rejected",
            "Terminal registration is restricted to edge_http transport at local location.",
            status=HTTPStatus.FORBIDDEN,
        )
    if definition.command or definition.docker_image:
        raise TerminalRegistrationError(
            "terminal_registration_executable_rejected",
            "Terminal registration may not define a process command or Docker image.",
            status=HTTPStatus.FORBIDDEN,
        )
    generation = str(registration.get("generation") or "").strip()
    owner_id = str(registration.get("owner_id") or "").strip()
    token = str(registration.get("capability_token") or "")
    if not generation or len(generation.encode("utf-8")) > 256:
        raise TerminalRegistrationError("terminal_generation_invalid", "Terminal generation is required.")
    if not owner_id or len(owner_id.encode("utf-8")) > 256:
        raise TerminalRegistrationError("terminal_owner_invalid", "Terminal owner identity is required.")
    if len(token) < 32 or len(token.encode("utf-8")) > 512:
        raise TerminalRegistrationError(
            "terminal_capability_invalid",
            "Terminal capability token must contain at least 32 characters.",
        )

    endpoint = str(definition.endpoint or "")
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise TerminalRegistrationError(
            "terminal_endpoint_invalid",
            "Terminal endpoint must be an absolute loopback URL with an explicit random port.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise TerminalRegistrationError(
            "terminal_endpoint_credentials_rejected",
            "Terminal endpoint may not contain userinfo, query, or fragment data.",
        )
    if not _loopback_host(parsed.hostname):
        raise TerminalRegistrationError(
            "terminal_endpoint_not_loopback",
            "Terminal endpoint must bind to loopback.",
            status=HTTPStatus.FORBIDDEN,
        )
    path_segments = [part for part in parsed.path.split("/") if part]
    if (
        len(path_segments) != 2
        or path_segments[0] != "capability"
        or not secrets.compare_digest(path_segments[-1], token)
    ):
        raise TerminalRegistrationError(
            "terminal_capability_mismatch",
            "Terminal endpoint capability path does not match registration proof.",
            status=HTTPStatus.FORBIDDEN,
        )
    expected_health = f"{endpoint.rstrip('/')}/health"
    if definition.health_endpoint and definition.health_endpoint != expected_health:
        raise TerminalRegistrationError(
            "terminal_health_endpoint_mismatch",
            "Terminal health endpoint must remain under the capability URL.",
        )

    metadata_text = json.dumps(dict(definition.metadata), sort_keys=True, default=str)
    if token in metadata_text:
        raise TerminalRegistrationError(
            "terminal_capability_metadata_leak",
            "Terminal capability token may not be persisted in metadata.",
            status=HTTPStatus.FORBIDDEN,
        )
    existing = next(
        (item for item in registry.definitions() if item.backend_id == definition.backend_id),
        None,
    )
    capability_digest = f"sha256:{hashlib.sha256(token.encode('utf-8')).hexdigest()}"
    if not definition.enabled and existing is None:
        raise TerminalRegistrationError(
            "terminal_registration_disabled",
            "A terminal backend must be registered enabled before it can be disabled.",
        )
    if existing is not None and existing.metadata.get("terminal_owner_id") != owner_id:
        raise TerminalRegistrationError(
            "terminal_owner_conflict",
            "Terminal backend owner identity does not match the existing definition.",
            status=HTTPStatus.CONFLICT,
        )
    if (
        existing is not None
        and existing.metadata.get("terminal_generation") != generation
    ):
        raise TerminalRegistrationError(
            "terminal_generation_conflict",
            "Terminal generation does not match the existing definition.",
            status=HTTPStatus.CONFLICT,
        )
    if existing is not None and not secrets.compare_digest(
        str(existing.metadata.get("terminal_capability_digest") or ""),
        capability_digest,
    ):
        raise TerminalRegistrationError(
            "terminal_capability_owner_mismatch",
            "Terminal capability proof does not match the existing definition.",
            status=HTTPStatus.FORBIDDEN,
        )
    if existing is not None and (
        definition.endpoint != existing.endpoint
        or definition.runtime_worker != existing.runtime_worker
        or definition.kind is not existing.kind
        or definition.location is not existing.location
    ):
        raise TerminalRegistrationError(
            "terminal_definition_identity_conflict",
            "Terminal transport identity cannot change within a process generation.",
            status=HTTPStatus.CONFLICT,
        )

    if not definition.enabled:
        health = registry.health(existing.backend_id)
        if health.current_leases:
            raise TerminalRegistrationError(
                "terminal_disable_active_leases",
                "Terminal backend must drain active leases before disable.",
                status=HTTPStatus.CONFLICT,
            )
        return replace(existing, enabled=False)

    normalized_definition = replace(
        definition,
        health_endpoint=expected_health,
        metadata={
            **dict(definition.metadata),
            "allowed_hosts": (parsed.hostname,),
            "terminal_registration": True,
            "terminal_generation": generation,
            "terminal_owner_id": owner_id,
            "terminal_capability_digest": capability_digest,
        },
    )

    try:
        health = RemoteBackendControlClient(normalized_definition).health()
    except (RemoteBackendControlError, OSError, ValueError) as error:
        raise TerminalRegistrationError(
            "terminal_listener_not_ready",
            "Terminal listener health could not be attested.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        ) from error
    if not health.ok or not health.accepting:
        raise TerminalRegistrationError(
            "terminal_listener_not_ready",
            "Terminal listener health is not ready for registration.",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
        )
    if health.generation != generation or health.runtime_worker != definition.runtime_worker:
        raise TerminalRegistrationError(
            "terminal_listener_identity_mismatch",
            "Terminal listener generation or runtime worker does not match registration proof.",
            status=HTTPStatus.CONFLICT,
        )
    if not set(definition.capabilities).issubset(health.capabilities):
        raise TerminalRegistrationError(
            "terminal_listener_capability_mismatch",
            "Terminal listener health omitted registered capabilities.",
            status=HTTPStatus.CONFLICT,
        )

    return normalized_definition


def _loopback_host(value: str) -> bool:
    if value.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False
