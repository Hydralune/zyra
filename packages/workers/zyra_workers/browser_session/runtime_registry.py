from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .artifact_event_bridge import BrowserArtifactPort, BrowserCanonicalEventPort
from .errors import BrowserRuntimeDisabled, BrowserStateConflict
from .models import BrowserRuntimeConfig, browser_now
from .permission_bridge import BrowserPermissionPort
from .runtime import BrowserRuntime
from .store import BrowserStatePort
from .resume_runtime import (
    BrowserResumeCapsule,
    BrowserResumeResult,
    BrowserSessionResumeRuntime,
)
from .diagnostics_runtime import BrowserDiagnosticsReport, BrowserDiagnosticsRuntime
from .session_projection import (
    BrowserRuntimeProjection,
    BrowserSessionProjection,
    BrowserSessionProjectionRuntime,
)
from .session_lease import BrowserSessionLeaseStore


class BrowserRuntimeRegistryStatus(StrEnum):
    ACTIVE = "active"
    DRAINING = "draining"
    CLOSED = "closed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class BrowserRuntimeRegistryKey:
    state_root: str
    runtime_root: str
    artifact_root: str

    @classmethod
    def from_config(cls, config: BrowserRuntimeConfig) -> "BrowserRuntimeRegistryKey":
        return cls(
            state_root=str(Path(config.state_root).expanduser().resolve()),
            runtime_root=str(Path(config.runtime_root).expanduser().resolve()),
            artifact_root=str(Path(config.artifact_root).expanduser().resolve()),
        )

    @property
    def value(self) -> str:
        return "|".join((self.state_root, self.runtime_root, self.artifact_root))

    def to_dict(self) -> dict[str, str]:
        return {
            "state_root": self.state_root,
            "runtime_root": self.runtime_root,
            "artifact_root": self.artifact_root,
            "value": self.value,
        }


@dataclass(slots=True)
class BrowserRuntimeRegistryEntry:
    key: BrowserRuntimeRegistryKey
    runtime: BrowserRuntime
    dependency_identity: tuple[int, int, int, int, int]
    lease_store: BrowserSessionLeaseStore | None = None
    application: Any | None = None
    control_runtime: Any | None = None
    resume_runtime: Any | None = None
    canonical_ports: Any | None = None
    integration_audit: Any | None = None
    status: BrowserRuntimeRegistryStatus = BrowserRuntimeRegistryStatus.ACTIVE
    created_at: str = field(default_factory=browser_now)
    last_accessed_at: str = field(default_factory=browser_now)
    access_count: int = 1

    def touch(self) -> None:
        if self.status != BrowserRuntimeRegistryStatus.ACTIVE:
            raise BrowserRuntimeDisabled(f"browser runtime registry entry is {self.status}")
        self.last_accessed_at = browser_now()
        self.access_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "runtime_key": integration_key_digest(self.key),
            "status": str(self.status),
            "created_at": self.created_at,
            "last_accessed_at": self.last_accessed_at,
            "access_count": self.access_count,
            "runtime_metadata": self.runtime.metadata(),
            "application": self.application.snapshot() if self.application is not None else {},
            "control": self.control_runtime.transactions.snapshot() if self.control_runtime is not None else {},
            "integration_audit": self.integration_audit.inspect().to_dict() if self.integration_audit is not None else {},
        }


def integration_key_digest(key: BrowserRuntimeRegistryKey) -> str:
    import hashlib

    return "browser-runtime:" + hashlib.sha256(key.value.encode("utf-8")).hexdigest()[:24]


class BrowserRuntimeRegistry:
    """Process-level owner for productized BrowserRuntime instances.

    API requests must reuse this registry rather than constructing BrowserRuntime
    per request. Durable BrowserSessionRef data remains in BrowserStatePort;
    sockets, event buses, CDP readers, task generations and process handles live
    in the registry-owned runtime until explicit removal/shutdown.
    """

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._entries: dict[str, BrowserRuntimeRegistryEntry] = {}
        self._lock = threading.RLock()
        self._creation_count = 0
        self._reuse_count = 0
        self._removal_count = 0
        self._stale_eviction_count = 0
        self._stale_runtimes: list[dict[str, Any]] = []
        self._last_error = ""

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserRuntimeDisabled("browser runtime registry is disabled")

    @staticmethod
    def _stale_reason(entry: BrowserRuntimeRegistryEntry) -> str:
        missing = [
            name
            for name, value in (
                ("state_root", entry.key.state_root),
                ("runtime_root", entry.key.runtime_root),
                ("artifact_root", entry.key.artifact_root),
            )
            if not Path(value).is_dir()
        ]
        return "removed_runtime_roots:" + ",".join(missing) if missing else ""

    def _evict_stale_locked(self, entry: BrowserRuntimeRegistryEntry, reason: str) -> dict[str, Any]:
        entry.status = BrowserRuntimeRegistryStatus.STALE
        if self._entries.get(entry.key.value) is entry:
            self._entries.pop(entry.key.value, None)
            self._removal_count += 1
            self._stale_eviction_count += 1
        record = {
            "runtime_key": integration_key_digest(entry.key),
            "status": str(BrowserRuntimeRegistryStatus.STALE),
            "reason": reason or "runtime_root_unavailable",
            "detected_at": browser_now(),
        }
        self._stale_runtimes.append(record)
        del self._stale_runtimes[:-32]
        return record

    @staticmethod
    def _stale_diagnostics(record: dict[str, Any]) -> dict[str, Any]:
        finding = {
            "check_id": "browser_runtime_registry_entry_stale",
            "severity": "blocking",
            "message": "registered browser runtime roots were removed",
            "details": dict(record),
        }
        return {
            "ok": False,
            "ready": False,
            "stale": True,
            "error_code": "browser_runtime_registry_entry_stale",
            "blocking_findings": [finding],
            "integration_audit": {
                "ok": False,
                "ready": False,
                "blocking_check_ids": [finding["check_id"]],
                "blocking_findings": [finding],
            },
        }

    @staticmethod
    def _dependency_identity(
        state_store: BrowserStatePort | None,
        permission_port: BrowserPermissionPort | None,
        artifact_port: BrowserArtifactPort | None,
        event_port: BrowserCanonicalEventPort | None,
        process_controller: Any | None,
    ) -> tuple[int, int, int, int, int]:
        return tuple(id(value) if value is not None else 0 for value in (
            state_store,
            permission_port,
            artifact_port,
            event_port,
            process_controller,
        ))  # type: ignore[return-value]

    def get_or_create(
        self,
        config: BrowserRuntimeConfig,
        *,
        state_store: BrowserStatePort | None = None,
        permission_port: BrowserPermissionPort | None = None,
        artifact_port: BrowserArtifactPort | None = None,
        event_port: BrowserCanonicalEventPort | None = None,
        process_controller: Any | None = None,
    ) -> BrowserRuntime:
        self._ensure_available()
        key = BrowserRuntimeRegistryKey.from_config(config)
        dependencies = self._dependency_identity(
            state_store,
            permission_port,
            artifact_port,
            event_port,
            process_controller,
        )
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is not None:
                stale_reason = self._stale_reason(entry)
                if stale_reason:
                    self._evict_stale_locked(entry, stale_reason)
                    entry = None
            if entry is not None:
                if entry.status != BrowserRuntimeRegistryStatus.ACTIVE:
                    raise BrowserRuntimeDisabled(f"browser runtime registry entry is {entry.status}")
                if entry.dependency_identity != dependencies:
                    raise BrowserStateConflict(
                        "browser runtime registry key is already owned by different injected ports",
                        details={"registry_key": key.value},
                    )
                entry.touch()
                self._reuse_count += 1
                return entry.runtime
            try:
                runtime = BrowserRuntime(
                    config,
                    state_store=state_store,
                    permission_port=permission_port,
                    artifact_port=artifact_port,
                    event_port=event_port,
                    process_controller=process_controller,
                )
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
                raise
            entry = BrowserRuntimeRegistryEntry(
                key=key,
                runtime=runtime,
                dependency_identity=dependencies,
            )
            self._attach_integration_owners(entry)
            self._entries[key.value] = entry
            self._creation_count += 1
            self._last_error = ""
            return runtime

    @staticmethod
    def _attach_integration_owners(entry: BrowserRuntimeRegistryEntry) -> None:
        from zyra_runtime import LocalArtifactStore

        from .application import BrowserSessionApplication
        from .canonical_ports import BrowserCanonicalPorts
        from .control_runtime import BrowserSessionControlRuntime
        from .integration_audit import BrowserSessionIntegrationAudit

        runtime = entry.runtime
        lease_store = BrowserSessionLeaseStore(Path(runtime.config.state_root) / "integration")
        canonical_ports = BrowserCanonicalPorts(
            artifact_projector=lambda artifact: None,
            event_projector=lambda event: None,
            strict=True,
            metadata={
                "mode": "validate-and-return",
                "canonical_fact_owner": "API/SQLiteStore",
                "retains_refs": False,
            },
        )
        application = BrowserSessionApplication(
            runtime,
            LocalArtifactStore(runtime.config.artifact_root),
            receipt_store=lease_store,
            canonical_ports=canonical_ports,
        )
        control = BrowserSessionControlRuntime(
            runtime,
            lease_store=lease_store,
            transaction_runtime=application.lifecycle_transactions,
            canonical_ports=canonical_ports,
        )
        audit = BrowserSessionIntegrationAudit(
            runtime,
            application=application,
            control_runtime=control,
            canonical_ports=canonical_ports,
        )
        application.audit_runtime = audit
        setattr(runtime, "_browser_control_runtime", control)
        setattr(runtime, "_browser_canonical_ports", canonical_ports)
        setattr(runtime, "_browser_integration_audit", audit)
        setattr(runtime, "_browser_registry_entry", entry)
        entry.lease_store = lease_store
        entry.application = application
        entry.control_runtime = control
        entry.resume_runtime = application.resume_runtime
        entry.canonical_ports = canonical_ports
        entry.integration_audit = audit

    def get(self, config: BrowserRuntimeConfig) -> BrowserRuntime | None:
        self._ensure_available()
        key = BrowserRuntimeRegistryKey.from_config(config)
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                return None
            stale_reason = self._stale_reason(entry)
            if stale_reason:
                self._evict_stale_locked(entry, stale_reason)
                return None
            entry.touch()
            self._reuse_count += 1
            return entry.runtime

    def get_by_roots(
        self,
        *,
        state_root: str | Path,
        runtime_root: str | Path,
        artifact_root: str | Path,
    ) -> BrowserRuntime | None:
        self._ensure_available()
        key = BrowserRuntimeRegistryKey(
            state_root=str(Path(state_root).expanduser().resolve()),
            runtime_root=str(Path(runtime_root).expanduser().resolve()),
            artifact_root=str(Path(artifact_root).expanduser().resolve()),
        )
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                return None
            stale_reason = self._stale_reason(entry)
            if stale_reason:
                self._evict_stale_locked(entry, stale_reason)
                return None
            entry.touch()
            self._reuse_count += 1
            return entry.runtime

    def require(self, config: BrowserRuntimeConfig) -> BrowserRuntime:
        runtime = self.get(config)
        if runtime is None:
            key = BrowserRuntimeRegistryKey.from_config(config)
            raise KeyError(f"browser runtime is not registered: {key.value}")
        return runtime

    def application(self, config: BrowserRuntimeConfig) -> Any:
        entry = self._require_entry(config)
        return entry.application

    def control(self, config: BrowserRuntimeConfig) -> Any:
        entry = self._require_entry(config)
        return entry.control_runtime

    def _require_entry(self, config: BrowserRuntimeConfig) -> BrowserRuntimeRegistryEntry:
        key = BrowserRuntimeRegistryKey.from_config(config)
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                raise KeyError(f"browser runtime is not registered: {key.value}")
            stale_reason = self._stale_reason(entry)
            if stale_reason:
                self._evict_stale_locked(entry, stale_reason)
                raise BrowserRuntimeDisabled("browser runtime registry entry is stale")
            entry.touch()
            return entry

    def capture_resume_capsule(
        self,
        config: BrowserRuntimeConfig,
        session_id: str,
    ) -> BrowserResumeCapsule:
        """Persist a redacted capsule in the registered runtime state store."""

        entry = self._require_entry(config)
        return entry.resume_runtime.capture(session_id)

    def resume(
        self,
        config: BrowserRuntimeConfig,
        session_id: str,
        *,
        expected_run_id: str = "",
        expected_task_id: str = "",
        worker_request_id: str = "",
    ) -> BrowserResumeResult:
        """Rebind/reconnect one registered session without creating a runtime."""

        entry = self._require_entry(config)
        return entry.resume_runtime.resume(
            session_id,
            expected_run_id=expected_run_id,
            expected_task_id=expected_task_id,
            worker_request_id=worker_request_id,
        )

    def diagnostics(
        self,
        config: BrowserRuntimeConfig,
        *,
        session_id: str = "",
        action_runtime: Any | None = None,
        lease_store: BrowserSessionLeaseStore | None = None,
    ) -> dict[str, Any]:
        """Aggregate blocking health findings for one registered runtime."""

        key = BrowserRuntimeRegistryKey.from_config(config)
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is None:
                raise KeyError(f"browser runtime is not registered: {key.value}")
            stale_reason = self._stale_reason(entry)
            if stale_reason:
                record = self._evict_stale_locked(entry, stale_reason)
                return self._stale_diagnostics(record)
            entry.touch()
        runtime = entry.runtime
        selected_actions = getattr(action_runtime, "actions", action_runtime) if action_runtime is not None else entry.application.actions
        diagnostics = BrowserDiagnosticsRuntime(
            runtime,
            registry_snapshot=self.snapshot,
            action_runtime=selected_actions,
            lease_store=lease_store or entry.lease_store,
        )
        try:
            report = diagnostics.diagnose(session_id=session_id)
            value = report.to_dict()
            audit = entry.integration_audit.inspect()
        except FileNotFoundError:
            with self._lock:
                record = self._evict_stale_locked(entry, self._stale_reason(entry) or "runtime_root_disappeared")
            return self._stale_diagnostics(record)
        value["integration_audit"] = audit.to_dict()
        value["ok"] = bool(value.get("ok")) and not audit.blocking_findings
        return value

    def projection(
        self,
        config: BrowserRuntimeConfig,
        *,
        session_id: str = "",
        lease_store: BrowserSessionLeaseStore | None = None,
    ) -> dict[str, Any]:
        """Build a secret-free API/M2 projection from one registered runtime."""

        entry = self._require_entry(config)
        runtime = entry.runtime
        key = entry.key
        projection = BrowserSessionProjectionRuntime(
            runtime,
            runtime_key=integration_key_digest(key),
            registry_snapshot=self.snapshot(),
            lease_store=lease_store or entry.lease_store,
        )
        return projection.project(session_id=session_id).to_dict()

    def list_entries(self) -> tuple[BrowserRuntimeRegistryEntry, ...]:
        with self._lock:
            for entry in tuple(self._entries.values()):
                stale_reason = self._stale_reason(entry)
                if stale_reason:
                    self._evict_stale_locked(entry, stale_reason)
            return tuple(sorted(self._entries.values(), key=lambda item: item.key.value))

    def remove(
        self,
        config: BrowserRuntimeConfig,
        *,
        stop: bool = False,
        force: bool = False,
    ) -> BrowserRuntime | None:
        key = BrowserRuntimeRegistryKey.from_config(config)
        with self._lock:
            entry = self._entries.get(key.value)
            if entry is not None:
                if entry.status != BrowserRuntimeRegistryStatus.ACTIVE:
                    raise BrowserRuntimeDisabled(f"browser runtime registry entry is {entry.status}")
                entry.status = BrowserRuntimeRegistryStatus.DRAINING
        if entry is None:
            return None
        if not stop and any(session.status != "stopped" for session in entry.runtime.list_sessions()):
            with self._lock:
                entry.status = BrowserRuntimeRegistryStatus.ACTIVE
            raise BrowserRuntimeDisabled("cannot remove a registry owner while browser sessions are active")
        if stop:
            try:
                self._stop_entry(entry, force=force, reason="runtime_registry_remove")
            except Exception as error:
                self._last_error = f"{type(error).__name__}: {error}"
                raise
        with self._lock:
            current = self._entries.get(key.value)
            if current is entry:
                entry.status = BrowserRuntimeRegistryStatus.CLOSED
                self._entries.pop(key.value, None)
                self._removal_count += 1
        return entry.runtime

    def stop_all(self, *, force: bool = False) -> dict[str, tuple[Any, ...]]:
        self._ensure_available()
        with self._lock:
            entries = tuple(item for item in self._entries.values() if item.status == BrowserRuntimeRegistryStatus.ACTIVE)
            for entry in entries:
                entry.status = BrowserRuntimeRegistryStatus.DRAINING
        results: dict[str, tuple[Any, ...]] = {}
        errors: list[str] = []
        for entry in entries:
            try:
                results[entry.key.value] = self._stop_entry(
                    entry,
                    force=force,
                    reason="runtime_registry_stop_all",
                )
            except Exception as error:
                errors.append(f"{entry.key.value}: {type(error).__name__}: {error}")
        if errors:
            with self._lock:
                for entry in entries:
                    if entry.status == BrowserRuntimeRegistryStatus.DRAINING:
                        entry.status = BrowserRuntimeRegistryStatus.ACTIVE
            self._last_error = "; ".join(errors)
            raise RuntimeError(f"one or more browser runtimes failed to stop: {self._last_error}")
        self._last_error = ""
        with self._lock:
            for entry in entries:
                entry.status = BrowserRuntimeRegistryStatus.CLOSED
        return results

    def clear(
        self,
        *,
        stop: bool = False,
        force: bool = False,
    ) -> tuple[BrowserRuntimeRegistryEntry, ...]:
        self._ensure_available()
        with self._lock:
            entries = tuple(self._entries.values())
            for entry in entries:
                if entry.status == BrowserRuntimeRegistryStatus.ACTIVE:
                    entry.status = BrowserRuntimeRegistryStatus.DRAINING
        if not stop and any(
            session.status != "stopped"
            for entry in entries
            for session in entry.runtime.list_sessions()
        ):
            with self._lock:
                for entry in entries:
                    if entry.status == BrowserRuntimeRegistryStatus.DRAINING:
                        entry.status = BrowserRuntimeRegistryStatus.ACTIVE
            raise BrowserRuntimeDisabled("cannot clear registry owners while browser sessions are active")
        if stop:
            errors: list[str] = []
            for entry in entries:
                try:
                    self._stop_entry(entry, force=force, reason="runtime_registry_clear")
                except Exception as error:
                    errors.append(f"{integration_key_digest(entry.key)}: {type(error).__name__}: {error}")
            if errors:
                with self._lock:
                    for entry in entries:
                        if entry.status == BrowserRuntimeRegistryStatus.DRAINING:
                            entry.status = BrowserRuntimeRegistryStatus.ACTIVE
                raise RuntimeError("one or more browser runtimes failed to stop: " + "; ".join(errors))
        with self._lock:
            for entry in entries:
                entry.status = BrowserRuntimeRegistryStatus.CLOSED
                if self._entries.get(entry.key.value) is entry:
                    self._entries.pop(entry.key.value, None)
            self._removal_count += len(entries)
        return entries

    def shutdown(self, *, force: bool = False) -> tuple[BrowserRuntimeRegistryEntry, ...]:
        """Stop every owned runtime and remove it from the process registry."""

        # Registry ownership must never be discarded while a keep-alive Chrome
        # process survives. Shutdown therefore always performs physical cleanup.
        return self.clear(stop=True, force=True)

    @staticmethod
    def _stop_entry(
        entry: BrowserRuntimeRegistryEntry,
        *,
        force: bool,
        reason: str,
    ) -> tuple[Any, ...]:
        application_lock = getattr(entry.application, "_lock", None)
        if application_lock is None:
            return entry.runtime.stop_all(force=force, reason=reason)
        with application_lock:
            return entry.runtime.stop_all(force=force, reason=reason)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            runtimes: list[dict[str, Any]] = []
            for entry in tuple(self._entries.values()):
                stale_reason = self._stale_reason(entry)
                if stale_reason:
                    self._evict_stale_locked(entry, stale_reason)
                    continue
                try:
                    runtimes.append(entry.to_dict())
                except FileNotFoundError:
                    self._evict_stale_locked(entry, self._stale_reason(entry) or "runtime_root_disappeared")
            entries = tuple(self._entries.values())
            return {
                "runtime_id": "zyra-browser-runtime-registry",
                "owner_unit": "M1-S04A-02",
                "disabled": self.disabled,
                "entries": len(entries),
                "creations": self._creation_count,
                "reuses": self._reuse_count,
                "removals": self._removal_count,
                "stale_evictions": self._stale_eviction_count,
                "stale_runtimes": [dict(item) for item in self._stale_runtimes],
                "last_error": self._last_error,
                "runtimes": runtimes,
            }


_DEFAULT_REGISTRY = BrowserRuntimeRegistry()


def default_browser_runtime_registry() -> BrowserRuntimeRegistry:
    return _DEFAULT_REGISTRY


def reset_default_browser_runtime_registry(*, stop: bool = False, force: bool = False) -> tuple[BrowserRuntimeRegistryEntry, ...]:
    return _DEFAULT_REGISTRY.clear(stop=stop, force=force)
