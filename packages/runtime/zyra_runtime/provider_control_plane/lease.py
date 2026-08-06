from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .client import ProviderControlPlaneClient
from .models import (
    CredentialRegistration,
    IntegrationDefinition,
    ModelCapabilities,
    ModelDefinition,
    ProviderControlPlanePortError,
    ProviderDefinition,
    ProviderProtocol,
    RouteConstraints,
    RouteRequest,
)


@dataclass(frozen=True, slots=True)
class ProviderRouteLeaseRef:
    route_id: str
    route_checksum: str
    catalog_revision: int
    credential_version: int
    credential_fingerprint: str
    transport_id: str
    run_id: str
    task_id: str
    session_id: str
    turn_id: str
    expires_at: int
    provider_id: str = field(default="", repr=False)
    model_id: str = field(default="", repr=False)
    credential_id: str = field(default="", repr=False)
    credential_environment_name: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        required = {
            "route_id": self.route_id,
            "route_checksum": self.route_checksum,
            "credential_fingerprint": self.credential_fingerprint,
            "transport_id": self.transport_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"provider route ref missing fields: {', '.join(missing)}")
        if self.catalog_revision <= 0:
            raise ValueError("provider catalog revision must be positive")
        if self.credential_version <= 0:
            raise ValueError("provider credential version must be positive")

    @classmethod
    def from_wire(cls, value: Mapping[str, Any]) -> ProviderRouteLeaseRef:
        return cls(
            route_id=str(value.get("routeId") or ""),
            route_checksum=str(value.get("checksum") or ""),
            catalog_revision=int(value.get("catalogRevision") or 0),
            credential_version=int(value.get("credentialVersion") or 0),
            credential_fingerprint=str(value.get("credentialFingerprint") or ""),
            transport_id=str(value.get("transportId") or ""),
            run_id=str(value.get("runId") or ""),
            task_id=str(value.get("taskId") or ""),
            session_id=str(value.get("sessionId") or ""),
            turn_id=str(value.get("turnId") or ""),
            expires_at=int(value.get("expiresAt") or 0),
            provider_id=str(value.get("providerId") or ""),
            model_id=str(value.get("modelId") or ""),
            credential_id=str(value.get("credentialId") or ""),
            credential_environment_name=str(
                value.get("credentialEnvironmentName") or ""
            ),
        )

    def safe_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "route_checksum": self.route_checksum,
            "catalog_revision": self.catalog_revision,
            "credential_version": self.credential_version,
            "credential_fingerprint": self.credential_fingerprint,
            "transport_id": self.transport_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "expires_at": self.expires_at,
            "provider_state_embedded": False,
            "credential_bytes_included": False,
        }

    def runtime_constraints(self, *, database_path: str | Path) -> dict[str, Any]:
        explicit_simulation = self.provider_id == "zyra-sim"
        if not explicit_simulation and not self.credential_environment_name:
            raise ValueError(
                "the pinned provider credential is not backed by a supported "
                "environment reference"
            )
        return {
            "provider_control_plane_required": not explicit_simulation,
            "provider_control_plane_explicit_simulation": explicit_simulation,
            "provider_control_plane_database_path": str(Path(database_path).resolve()),
            "provider_route_id": self.route_id,
            "provider_route_checksum": self.route_checksum,
            "provider_catalog_revision": self.catalog_revision,
            "provider_credential_version": self.credential_version,
            "provider_credential_fingerprint": self.credential_fingerprint,
            "provider_transport_id": self.transport_id,
            "provider_route_session_id": self.session_id,
            "provider_route_turn_id": self.turn_id,
            "provider_route_expires_at": self.expires_at,
            "provider_id": self.provider_id,
            "provider_model_id": self.model_id,
            "disable_legacy_provider_defaults": True,
            # The TypeScript model process receives exactly the environment
            # reference pinned by this immutable route.  Ambient provider keys
            # remain excluded, and physical tools execute in a separately
            # allowlisted environment that rejects secret-like names.
            "provider_credential_environment_name": self.credential_environment_name,
            "provider_credential_environment_scoped": not explicit_simulation,
            "disable_worker_api_key_environment": True,
        }


class ProviderRouteBindingError(RuntimeError):
    def __init__(self, code: str, message: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class ProviderRouteBindingRuntime:
    """Single-turn route preflight shared by CodeWorker and BrowserWorker."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        database_path: str | Path,
        allow_explicit_sim_bootstrap: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.database_path = Path(database_path).resolve()
        self.allow_explicit_sim_bootstrap = allow_explicit_sim_bootstrap

    def bind(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        turn_id: str,
        route_id: str | None = None,
        purpose: str = "general",
        preferred_provider_id: str | None = None,
        preferred_model_id: str | None = None,
        require_tools: bool = False,
        require_streaming: bool = True,
        metadata: Mapping[str, Any] | None = None,
    ) -> ProviderRouteLeaseRef:
        for name, value in {
            "run_id": run_id,
            "task_id": task_id,
            "session_id": session_id,
            "turn_id": turn_id,
        }.items():
            if not str(value).strip():
                raise ProviderRouteBindingError("provider_route_identity_missing", f"{name} is required")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with ProviderControlPlaneClient(
                project_root=self.project_root,
                database_path=self.database_path,
            ) as client:
                # Reconcile the configured live profiles on every bind.  This
                # is idempotent and also rotates an env-referenced credential
                # when the operator changed it between daemon lifecycles.
                client.install_configured_profiles()
                if route_id:
                    wire = client.routing.get(route_id)
                    ref = ProviderRouteLeaseRef.from_wire(wire)
                    self._assert_identity(
                        ref,
                        run_id=run_id,
                        task_id=task_id,
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                    return self._with_credential_environment(client, ref)
                existing = self._existing_turn_route(
                    client,
                    run_id=run_id,
                    task_id=task_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                if existing is not None:
                    return self._with_credential_environment(client, existing)
                if not client.catalog.models(available_only=True):
                    if not self.allow_explicit_sim_bootstrap:
                        raise ProviderRouteBindingError(
                            "provider_catalog_empty",
                            "no active provider model is registered",
                        )
                    self._ensure_explicit_sim_catalog(client)
                wire = client.routing.acquire(
                    RouteRequest(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        purpose=purpose,
                        preferred_provider_id=preferred_provider_id,
                        preferred_model_id=preferred_model_id,
                        route_hint=(
                            f"{preferred_provider_id}/{preferred_model_id}"
                            if preferred_provider_id and preferred_model_id
                            else None
                        ),
                        constraints=RouteConstraints(
                            require_tools=require_tools,
                            require_streaming=require_streaming,
                        ),
                        metadata={
                            "bindingOwner": "ProviderRouteBindingRuntime",
                            "sharedWorkerTurn": True,
                            **dict(metadata or {}),
                        },
                    )
                )
                ref = ProviderRouteLeaseRef.from_wire(wire)
                self._assert_identity(
                    ref,
                    run_id=run_id,
                    task_id=task_id,
                    session_id=session_id,
                    turn_id=turn_id,
                )
                return self._with_credential_environment(client, ref)
        except ProviderRouteBindingError:
            raise
        except ProviderControlPlanePortError as error:
            raise ProviderRouteBindingError(
                error.code,
                str(error),
                detail=error.detail,
            ) from error
        except (OSError, RuntimeError, ValueError, KeyError) as error:
            raise ProviderRouteBindingError(
                "provider_control_plane_unavailable",
                f"ProviderControlPlane route preflight failed: {type(error).__name__}: {error}",
            ) from error

    @staticmethod
    def _with_credential_environment(
        client: ProviderControlPlaneClient,
        ref: ProviderRouteLeaseRef,
    ) -> ProviderRouteLeaseRef:
        if ref.provider_id == "zyra-sim":
            return ref
        if not ref.credential_id:
            raise ProviderRouteBindingError(
                "provider_route_credential_missing",
                "provider route does not identify its pinned credential",
            )
        credential = client.credentials.get(ref.credential_id)
        secret_ref = str(credential.get("secretRef") or "")
        prefix = "env://"
        environment_name = secret_ref[len(prefix) :] if secret_ref.startswith(prefix) else ""
        if (
            not environment_name
            or len(environment_name) > 128
            or not environment_name[0].isalpha()
            or not environment_name.replace("_", "A").isalnum()
        ):
            raise ProviderRouteBindingError(
                "provider_credential_reference_unsupported",
                "the pinned credential must use a valid env:// reference for the "
                "isolated model runtime",
                detail={"credential_id": ref.credential_id},
            )
        return replace(ref, credential_environment_name=environment_name)

    @staticmethod
    def _existing_turn_route(
        client: ProviderControlPlaneClient,
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        turn_id: str,
    ) -> ProviderRouteLeaseRef | None:
        candidates: list[ProviderRouteLeaseRef] = []
        for value in client.routing.list(run_id=run_id, task_id=task_id):
            try:
                ref = ProviderRouteLeaseRef.from_wire(value)
            except (TypeError, ValueError):
                continue
            if ref.session_id == session_id and ref.turn_id == turn_id:
                candidates.append(ref)
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item.catalog_revision, item.route_id), reverse=True)
        # Multiple active routes for one turn indicate a pre-05D-02 race. Fail
        # closed unless they project exactly the same immutable route.
        identities = {
            (
                item.route_id,
                item.route_checksum,
                item.catalog_revision,
                item.credential_version,
                item.credential_fingerprint,
                item.transport_id,
            )
            for item in candidates
        }
        if len(identities) != 1:
            raise ProviderRouteBindingError(
                "provider_turn_route_conflict",
                "multiple provider route leases exist for the same worker turn",
                detail={"route_ids": sorted(item.route_id for item in candidates)},
            )
        return candidates[0]

    @staticmethod
    def _assert_identity(
        ref: ProviderRouteLeaseRef,
        *,
        run_id: str,
        task_id: str,
        session_id: str,
        turn_id: str,
    ) -> None:
        mismatches = {
            name: {"expected": expected, "actual": actual}
            for name, expected, actual in (
                ("run_id", run_id, ref.run_id),
                ("task_id", task_id, ref.task_id),
                ("session_id", session_id, ref.session_id),
                ("turn_id", turn_id, ref.turn_id),
            )
            if expected != actual
        }
        if mismatches:
            raise ProviderRouteBindingError(
                "provider_route_identity_mismatch",
                "provider route lease does not belong to this worker turn",
                detail=mismatches,
            )

    @staticmethod
    def _ensure_explicit_sim_catalog(client: ProviderControlPlaneClient) -> None:
        integrations = {item.get("integrationId") for item in client.integrations.list()}
        if "zyra-sim-anonymous" not in integrations:
            client.integrations.upsert(
                IntegrationDefinition(
                    integration_id="zyra-sim-anonymous",
                    display_name="Zyra Explicit Simulation",
                    kind="anonymous",
                    metadata={
                        "explicitSimulation": True,
                        "productionDispatch": False,
                        "owner": "ProviderControlPlane",
                    },
                )
            )
        providers = {item.get("providerId") for item in client.catalog.providers()}
        if "zyra-sim" not in providers:
            client.catalog.upsert_provider(
                ProviderDefinition(
                    provider_id="zyra-sim",
                    display_name="Zyra Explicit Simulation Provider",
                    integration_id="zyra-sim-anonymous",
                    status="active",
                    base_url="http://127.0.0.1:9",
                    protocol=ProviderProtocol.OPENAI_CHAT,
                    allowed_hosts=("127.0.0.1",),
                    tags=("explicit-sim",),
                    metadata={
                        "explicitSimulation": True,
                        "transportExpected": False,
                        "noDeterministicFallback": True,
                    },
                )
            )
        models = {
            (item.get("providerId"), item.get("modelId"))
            for item in client.catalog.models()
        }
        if ("zyra-sim", "zyra-local-code-model") not in models:
            client.catalog.upsert_model(
                ModelDefinition(
                    provider_id="zyra-sim",
                    model_id="zyra-local-code-model",
                    display_name="Zyra Explicit Simulation Model",
                    family="zyra-sim",
                    released_at=1,
                    capabilities=ModelCapabilities(
                        tools=True,
                        streaming=True,
                        reasoning=False,
                    ),
                    endpoint_path="/v1/chat/completions",
                    tags=("explicit-sim",),
                    metadata={
                        "explicitSimulation": True,
                        "emptyTurnOnly": True,
                    },
                )
            )
        credentials = {
            item.get("credentialId"): item for item in client.credentials.list(provider_id="zyra-sim")
        }
        if "zyra-sim-anonymous-credential" not in credentials:
            client.credentials.register(
                CredentialRegistration(
                    credential_id="zyra-sim-anonymous-credential",
                    integration_id="zyra-sim-anonymous",
                    provider_id="zyra-sim",
                    account_id="anonymous",
                    secret_ref="env://ZYRA_SIM_UNUSED",
                    fingerprint=(
                        "sha256:"
                        + hashlib.sha256(b"zyra-explicit-sim").hexdigest()[:16]
                    ),
                    metadata={
                        "explicitSimulation": True,
                        "secretMaterialRequired": False,
                    },
                )
            )


def provider_database_path(artifact_root: str | Path) -> Path:
    return (
        Path(artifact_root).expanduser().resolve()
        / ".provider-control-plane"
        / "provider.sqlite3"
    )


__all__ = [
    "ProviderRouteBindingError",
    "ProviderRouteBindingRuntime",
    "ProviderRouteLeaseRef",
    "provider_database_path",
]
