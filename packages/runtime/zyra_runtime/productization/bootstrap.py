from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .configuration import ResolvedConfiguration, load_runtime_configuration
from .credentials import (
    CredentialPresenceReport,
    CredentialPresenceRuntime,
    EnvironmentSecretProvisioner,
)
from .lifecycle import (
    ErrorEnvelope,
    LifecyclePhase,
    LifecycleRecorder,
    ProcessIdentity,
    Severity,
    ShutdownReport,
)
from .migration_adapters import (
    ArtifactManifestMigrationAdapter,
    CallableOwnerMigrationAdapter,
    MigrationAdapter,
    SqliteOwnerMigrationAdapter,
)
from .migration_journal import MigrationJournal
from .migration_runtime import (
    MigrationPlan,
    MigrationReceipt,
    MigrationRecoveryReceipt,
    MigrationRuntime,
    MigrationRuntimeError,
)


class BootstrapError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        phase: LifecyclePhase,
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.phase = phase
        self.retryable = retryable
        self.details = dict(details or {})


@dataclass(frozen=True, slots=True)
class BootstrapReceipt:
    process: ProcessIdentity
    configuration_digest: str
    credentials: CredentialPresenceReport
    migration: MigrationReceipt | None
    recovery: MigrationRecoveryReceipt | None
    owner_readiness: Mapping[str, Any]
    ready_at_ns: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.product-bootstrap-receipt/v1",
            "process": self.process.to_dict(),
            "configuration_digest": self.configuration_digest,
            "credentials": self.credentials.to_dict(),
            "migration": (
                self.migration.to_dict() if self.migration is not None else None
            ),
            "recovery": (
                self.recovery.to_dict() if self.recovery is not None else None
            ),
            "owner_readiness": dict(self.owner_readiness),
            "ready_at_ns": self.ready_at_ns,
            "digest": self.digest,
            "demo_fallback": False,
            "source_store_fallback": False,
        }


class ProductBootstrapRuntime:
    def __init__(
        self,
        configuration: ResolvedConfiguration,
        *,
        adapters: Sequence[MigrationAdapter] | None = None,
        owner_probe: Callable[[], Mapping[str, Any]] | None = None,
        environ: Mapping[str, str] | None = None,
        process_id: str | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
        crash_hook: Callable[[str, str, str], Any] | None = None,
    ) -> None:
        self.configuration = configuration
        self._environment = dict(os.environ if environ is None else environ)
        self._clock_ns = clock_ns
        self.process = ProcessIdentity.create(
            configuration_digest=configuration.digest,
            process_id=process_id,
            clock_ns=clock_ns,
        )
        self.lifecycle = LifecycleRecorder(
            self.process,
            component="zyra.product-bootstrap",
            log_path=configuration.path("state.lifecycle_log"),
            clock_ns=clock_ns,
        )
        self.credential_runtime = CredentialPresenceRuntime(
            configuration,
            environ=self._environment,
            clock_ns=clock_ns,
        )
        self.credentials = EnvironmentSecretProvisioner(
            self.credential_runtime.requirements(),
            environ=self._environment,
            clock_ns=clock_ns,
        )
        self.journal = MigrationJournal(
            configuration.path("state.migration_journal"),
            clock_ns=clock_ns,
        )
        selected_adapters = tuple(
            adapters if adapters is not None else build_default_migration_adapters(configuration)
        )
        state_root = configuration.path("state.root")
        self.migrations = MigrationRuntime(
            self.journal,
            selected_adapters,
            backup_root=state_root / ".productization" / "backups",
            process_generation=(
                f"pid:{self.process.pid}:{self.process.generation_id}"
            ),
            configuration_digest=configuration.digest,
            target_version=int(configuration.require("migration.target_version")),
            lease_seconds=int(configuration.require("migration.lease_seconds")),
            lifecycle=self.lifecycle,
            crash_hook=crash_hook,
            clock_ns=clock_ns,
        )
        self._owner_probe = owner_probe
        self._lock = threading.RLock()
        self._receipt: BootstrapReceipt | None = None
        self._failure: ErrorEnvelope | None = None
        self._shutdown: ShutdownReport | None = None
        self._closed = False
        self.lifecycle.register_resource(
            "credential-provisioner",
            self.credentials,
            close=lambda value: value.close(),
        )
        self.lifecycle.register_resource(
            "migration-journal",
            self.journal,
            close=lambda value: value.close(),
            dependencies=("credential-provisioner",),
        )

    def start(self) -> BootstrapReceipt:
        with self._lock:
            if self._closed:
                raise BootstrapError(
                    "bootstrap_closed",
                    "product bootstrap is closed",
                    phase=self.lifecycle.phase,
                )
            if self._receipt is not None:
                return self._receipt
            try:
                self.lifecycle.transition(LifecyclePhase.CONFIGURING)
                credential_report = self.credential_runtime.inspect()
                credential_report.require_ready()
                self.credentials.provision_required()
                self.lifecycle.emit(
                    "configuration.validated",
                    attributes={
                        "configuration_digest": self.configuration.digest,
                        "profile": self.configuration.get("profile"),
                        "credential_bindings": len(credential_report.entries),
                    },
                )
                self.lifecycle.transition(LifecyclePhase.MIGRATING)
                recovery = self.migrations.recover_incomplete()
                plan = self.migrations.plan()
                migration_enabled = bool(
                    self.configuration.require("runtime.migration_enabled")
                )
                auto_apply = bool(
                    self.configuration.require("migration.auto_apply")
                )
                if plan.migration_required and not migration_enabled:
                    raise BootstrapError(
                        "bootstrap_migration_disabled",
                        "owner stores require migration but migration is disabled",
                        phase=LifecyclePhase.MIGRATING,
                        details={
                            "plan_digest": plan.digest,
                            "adapters": [
                                step.adapter_id
                                for step in plan.steps
                                if step.migration_required
                            ],
                        },
                    )
                if plan.migration_required and not auto_apply:
                    raise BootstrapError(
                        "bootstrap_migration_approval_required",
                        "owner stores require migration and auto_apply is disabled",
                        phase=LifecyclePhase.MIGRATING,
                        retryable=True,
                        details={"plan": plan.to_dict()},
                    )
                migration = self.migrations.execute(plan)
                self.lifecycle.transition(LifecyclePhase.STARTING)
                owner_readiness = self._probe_owners()
                if owner_readiness.get("ready") is not True:
                    raise BootstrapError(
                        "bootstrap_owner_readiness_failed",
                        "canonical runtime owners are not ready",
                        phase=LifecyclePhase.STARTING,
                        retryable=True,
                        details=owner_readiness,
                    )
                migration_readiness = self.migrations.readiness()
                if (
                    bool(
                        self.configuration.require(
                            "runtime.readiness_requires_migration"
                        )
                    )
                    and migration_readiness.get("ready") is not True
                ):
                    raise BootstrapError(
                        "bootstrap_migration_readiness_failed",
                        "migration runtime is not ready after commit",
                        phase=LifecyclePhase.STARTING,
                        retryable=True,
                        details=migration_readiness,
                    )
                self.lifecycle.transition(LifecyclePhase.READY)
                ready_at_ns = self._clock_ns()
                body = {
                    "process": self.process.to_dict(),
                    "configuration_digest": self.configuration.digest,
                    "credentials": credential_report.to_dict(),
                    "migration_digest": migration.digest,
                    "recovery": (
                        recovery.to_dict() if recovery is not None else None
                    ),
                    "owner_readiness": owner_readiness,
                    "ready_at_ns": ready_at_ns,
                }
                receipt = BootstrapReceipt(
                    process=self.process,
                    configuration_digest=self.configuration.digest,
                    credentials=credential_report,
                    migration=migration,
                    recovery=recovery,
                    owner_readiness=MappingProxyType(dict(owner_readiness)),
                    ready_at_ns=ready_at_ns,
                    digest=_digest(body),
                )
                self._receipt = receipt
                self.lifecycle.emit(
                    "bootstrap.ready",
                    attributes={
                        "receipt_digest": receipt.digest,
                        "configuration_digest": self.configuration.digest,
                        "migration_digest": migration.digest,
                    },
                )
                return receipt
            except BaseException as error:
                self._failure = self.lifecycle.record_error(
                    error,
                    event="bootstrap.failed",
                    code=(
                        error.code
                        if isinstance(error, (BootstrapError, MigrationRuntimeError))
                        else "bootstrap_unhandled_error"
                    ),
                    retryable=bool(getattr(error, "retryable", False)),
                )
                if self.lifecycle.phase is not LifecyclePhase.FAILED:
                    self.lifecycle.transition(
                        LifecyclePhase.FAILED,
                        attributes={"error_id": self._failure.error_id},
                    )
                raise

    def readiness(self) -> dict[str, Any]:
        with self._lock:
            lifecycle = self.lifecycle.readiness()
            credentials = self.credential_runtime.inspect()
            try:
                migration = self.migrations.readiness()
            except BaseException as error:
                migration = {
                    "schema": "zyra.migration-readiness/v1",
                    "ready": False,
                    "blockers": ["migration_runtime"],
                    "error": f"{type(error).__name__}: {str(error)[:1024]}",
                    "source_store_fallback": False,
                }
            owner = (
                dict(self._receipt.owner_readiness)
                if self._receipt is not None
                else {"ready": False, "blockers": ["bootstrap_not_started"]}
            )
            blockers: list[str] = []
            if lifecycle.get("ready") is not True:
                blockers.append("lifecycle")
            if not credentials.ready:
                blockers.extend(
                    f"credential:{item}" for item in credentials.blockers
                )
            if migration.get("ready") is not True:
                blockers.append("migration")
            if owner.get("ready") is not True:
                blockers.append("canonical_owners")
            if self._failure is not None:
                blockers.append("bootstrap_failure")
            return {
                "schema": "zyra.product-bootstrap-readiness/v1",
                "ready": not blockers,
                "blockers": sorted(set(blockers)),
                "process": self.process.to_dict(),
                "configuration": self.configuration.public_projection(),
                "credentials": credentials.to_dict(),
                "migration": migration,
                "canonical_owners": owner,
                "lifecycle": lifecycle,
                "receipt": (
                    self._receipt.to_dict()
                    if self._receipt is not None
                    else None
                ),
                "failure": (
                    self._failure.to_dict()
                    if self._failure is not None
                    else None
                ),
                "shutdown": (
                    self._shutdown.to_dict()
                    if self._shutdown is not None
                    else None
                ),
                "demo_fallback": False,
                "source_store_fallback": False,
            }

    def shutdown(self) -> ShutdownReport:
        with self._lock:
            if self._shutdown is not None:
                return self._shutdown
            timeout = float(
                self.configuration.require("runtime.shutdown_timeout_seconds")
            )
            report = self.lifecycle.shutdown(timeout_seconds=timeout)
            self._shutdown = report
            self._closed = True
            return report

    def _probe_owners(self) -> dict[str, Any]:
        if self._owner_probe is None:
            return {
                "schema": "zyra.bootstrap-owner-readiness/v1",
                "ready": True,
                "mode": "deferred_to_composition_root",
                "blockers": [],
            }
        try:
            result = dict(self._owner_probe())
        except BaseException as error:
            return {
                "schema": "zyra.bootstrap-owner-readiness/v1",
                "ready": False,
                "mode": "composition_root",
                "blockers": ["owner_probe_exception"],
                "error": f"{type(error).__name__}: {str(error)[:1024]}",
            }
        result.setdefault("schema", "zyra.bootstrap-owner-readiness/v1")
        result.setdefault("mode", "composition_root")
        result.setdefault("blockers", [])
        return result


def build_default_migration_adapters(
    configuration: ResolvedConfiguration,
) -> tuple[MigrationAdapter, ...]:
    session = SqliteOwnerMigrationAdapter(
        adapter_id="session-event",
        owner="typescript.ClaudeRuntimeCore/RuntimeEventSqliteStore",
        path=configuration.path("state.database"),
        target_version=1,
    )
    graph = SqliteOwnerMigrationAdapter(
        adapter_id="graph-checkpoint",
        owner="GraphStateCustody/GraphStateStore",
        path=configuration.path("state.graph"),
        target_version=1,
        dependencies=("session-event",),
    )
    artifact = ArtifactManifestMigrationAdapter(
        configuration.path("state.artifacts"),
        dependencies=("session-event",),
    )
    permission_path = configuration.path("state.permission")

    def permission_version() -> int:
        from zyra_runtime.permission.store import PERMISSION_STATE_SCHEMA

        try:
            value = json.loads(permission_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise BootstrapError(
                "permission_migration_probe_failed",
                "permission owner state cannot be parsed",
                phase=LifecyclePhase.MIGRATING,
                details={"error": type(error).__name__},
            ) from error
        if not isinstance(value, Mapping):
            raise BootstrapError(
                "permission_migration_root_invalid",
                "permission owner state root must be an object",
                phase=LifecyclePhase.MIGRATING,
            )
        if value.get("schema") == PERMISSION_STATE_SCHEMA:
            return int(value.get("schema_version") or 0)
        if "rules" in value or "requests" in value:
            return 0
        raise BootstrapError(
            "permission_migration_schema_unknown",
            "permission owner state schema is unknown",
            phase=LifecyclePhase.MIGRATING,
        )

    def migrate_permission(source: int, target: int) -> Mapping[str, Any]:
        if source != 0 or target != 1:
            raise BootstrapError(
                "permission_migration_step_unsupported",
                "permission owner migration step is unsupported",
                phase=LifecyclePhase.MIGRATING,
                details={"source": source, "target": target},
            )
        from zyra_runtime.permission import PermissionStateStore

        store = PermissionStateStore(permission_path)
        migrated = store.mutate(lambda _: None)
        return {
            "operation": "PermissionStateStore.mutate",
            "schema": migrated.get("schema"),
            "schema_version": migrated.get("schema_version"),
            "revision": migrated.get("revision"),
            "legacy_payload_digest": (
                migrated.get("metadata") or {}
            ).get("legacy_payload_digest", ""),
        }

    def verify_permission(target: int) -> Mapping[str, Any]:
        from zyra_runtime.permission import PermissionStateStore
        from zyra_runtime.permission.store import PERMISSION_STATE_SCHEMA

        state = PermissionStateStore(permission_path).read_state()
        return {
            "verified": (
                state.get("schema") == PERMISSION_STATE_SCHEMA
                and int(state.get("schema_version") or 0) == target
                and int(state.get("revision") or 0) >= 0
                and isinstance(state.get("global_rules"), list)
                and isinstance(state.get("requests"), Mapping)
                and isinstance(state.get("decisions"), list)
            ),
            "schema": state.get("schema"),
            "schema_version": state.get("schema_version"),
            "revision": state.get("revision"),
            "rule_count": len(state.get("global_rules") or []),
            "request_count": len(state.get("requests") or {}),
            "decision_count": len(state.get("decisions") or []),
            "state_digest": _digest(state),
        }

    permission = CallableOwnerMigrationAdapter(
        adapter_id="permission-state",
        owner="PermissionStateStore",
        path=permission_path,
        target_version=1,
        probe_version=permission_version,
        apply_migration=migrate_permission,
        verify_owner=verify_permission,
        dependencies=("artifact-state",),
    )
    return (session, graph, artifact, permission)


_BOOTSTRAP_LOCK = threading.RLock()
_BOOTSTRAP: ProductBootstrapRuntime | None = None
_BOOTSTRAP_KEY: tuple[str, str, tuple[tuple[str, str], ...]] | None = None


def get_product_bootstrap(
    project_root: Path | str,
    *,
    environ: Mapping[str, str] | None = None,
    config_path: Path | str | None = None,
    explicit_overrides: Mapping[str, Any] | None = None,
    owner_probe: Callable[[], Mapping[str, Any]] | None = None,
    auto_start: bool = False,
) -> ProductBootstrapRuntime:
    global _BOOTSTRAP, _BOOTSTRAP_KEY
    environment = dict(os.environ if environ is None else environ)
    configuration = load_runtime_configuration(
        project_root,
        environ=environment,
        config_path=config_path,
        explicit_overrides=explicit_overrides,
    )
    relevant_environment = tuple(
        sorted(
            (key, value)
            for key, value in environment.items()
            if key.startswith("ZYRA_")
        )
    )
    key = (
        str(configuration.project_root),
        configuration.digest,
        relevant_environment,
    )
    with _BOOTSTRAP_LOCK:
        if _BOOTSTRAP is None or _BOOTSTRAP_KEY != key:
            if _BOOTSTRAP is not None:
                _BOOTSTRAP.shutdown()
            _BOOTSTRAP = ProductBootstrapRuntime(
                configuration,
                owner_probe=owner_probe,
                environ=environment,
            )
            _BOOTSTRAP_KEY = key
        runtime = _BOOTSTRAP
    if auto_start:
        runtime.start()
    return runtime


def reset_product_bootstrap(*, shutdown: bool = True) -> None:
    global _BOOTSTRAP, _BOOTSTRAP_KEY
    with _BOOTSTRAP_LOCK:
        current = _BOOTSTRAP
        _BOOTSTRAP = None
        _BOOTSTRAP_KEY = None
    if shutdown and current is not None:
        current.shutdown()


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
