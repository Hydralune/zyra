from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Mapping, Protocol, Sequence

from .atomic_update import AtomicSkillPackageUpdater, SkillUpdateReceipt
from .digests import digest_file, digest_object
from .errors import SkillReloadRejected, SkillRollbackRejected
from .integration_errors import (
    SkillPluginRollbackError,
    SkillPluginSupplyChainRejected,
    SkillPluginUpdateDenied,
    SkillPluginUpdatePending,
)
from .models import SkillSourceKind, utc_now
from .path_security import canonical_root, iter_skill_directories


class SkillUpdateAction(StrEnum):
    INSTALL = "install"
    UPDATE = "update"
    ROLLBACK = "rollback"
    DISABLE = "disable"
    ENABLE = "enable"
    REVOKE = "revoke"


class SkillUpdateStatus(StrEnum):
    REQUESTED = "requested"
    VALIDATED = "validated"
    PERMISSION_PENDING = "permission_pending"
    PERMISSION_DENIED = "permission_denied"
    STAGED = "staged"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            SkillUpdateStatus.PERMISSION_DENIED,
            SkillUpdateStatus.COMMITTED,
            SkillUpdateStatus.ROLLED_BACK,
            SkillUpdateStatus.FAILED,
        }


@dataclass(frozen=True, slots=True)
class SkillUpdateRequest:
    update_id: str
    action: SkillUpdateAction
    run_id: str
    task_id: str
    session_id: str
    source_root: str
    target_root: str
    source_kind: SkillSourceKind
    source_id: str
    namespace: str
    expected_base_digest: str = ""
    plugin_id: str = ""
    receipt_id: str = ""
    reason: str = ""
    interactive: bool = True
    headless: bool = False
    requested_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = {
            "update_id": self.update_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
        }
        missing = sorted(name for name, value in required.items() if not str(value).strip())
        if missing:
            raise ValueError(f"skill update request is incomplete: {missing}")
        if self.action in {SkillUpdateAction.INSTALL, SkillUpdateAction.UPDATE}:
            if not self.source_root or not self.target_root:
                raise ValueError("skill install/update requires source_root and target_root")
        if self.action is SkillUpdateAction.ROLLBACK and not self.receipt_id:
            raise ValueError("skill rollback requires receipt_id")

    def permission_arguments(self) -> dict[str, Any]:
        return {
            "update_id": self.update_id,
            "action": str(self.action),
            "source_kind": str(self.source_kind),
            "source_id": self.source_id,
            "namespace": self.namespace,
            "expected_base_digest": self.expected_base_digest,
            "plugin_id": self.plugin_id,
            "receipt_id": self.receipt_id,
            "target_root_digest": digest_object({"target_root": self.target_root}),
        }

    def to_dict(self, *, include_paths: bool = False) -> dict[str, Any]:
        value = {
            "update_id": self.update_id,
            "action": str(self.action),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "source_kind": str(self.source_kind),
            "source_id": self.source_id,
            "namespace": self.namespace,
            "expected_base_digest": self.expected_base_digest,
            "plugin_id": self.plugin_id,
            "receipt_id": self.receipt_id,
            "reason": self.reason,
            "interactive": self.interactive,
            "headless": self.headless,
            "requested_at": self.requested_at,
            "permission_arguments": self.permission_arguments(),
        }
        if include_paths:
            value["source_root"] = self.source_root
            value["target_root"] = self.target_root
        return value


@dataclass(frozen=True, slots=True)
class SkillUpdateFinding:
    code: str
    severity: str
    message: str
    path: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {"error", "blocker"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "detail": copy.deepcopy(self.detail),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class SkillUpdatePreflight:
    request_digest: str
    source_digest: str
    base_digest: str
    skill_count: int
    total_bytes: int
    findings: tuple[SkillUpdateFinding, ...]
    checked_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_digest": self.request_digest,
            "source_digest": self.source_digest,
            "base_digest": self.base_digest,
            "skill_count": self.skill_count,
            "total_bytes": self.total_bytes,
            "findings": [item.to_dict() for item in self.findings],
            "ok": self.ok,
            "checked_at": self.checked_at,
        }


@dataclass(frozen=True, slots=True)
class SkillUpdatePermissionDecision:
    decision: str
    request_id: str
    reason: str
    events: tuple[Any, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"

    @property
    def pending(self) -> bool:
        return self.decision == "ask"


class SkillUpdatePermissionPort(Protocol):
    def guard_update(
        self,
        request: SkillUpdateRequest,
        preflight: SkillUpdatePreflight,
    ) -> SkillUpdatePermissionDecision: ...


class DenySkillUpdatePermission:
    def guard_update(
        self,
        request: SkillUpdateRequest,
        preflight: SkillUpdatePreflight,
    ) -> SkillUpdatePermissionDecision:
        return SkillUpdatePermissionDecision(
            decision="deny",
            request_id=request.update_id,
            reason="skill update permission port is not configured",
        )


@dataclass(frozen=True, slots=True)
class SkillUpdateState:
    request: SkillUpdateRequest
    status: SkillUpdateStatus
    preflight: SkillUpdatePreflight | None = None
    permission_decision: SkillUpdatePermissionDecision | None = None
    receipt: SkillUpdateReceipt | None = None
    registry_reload: dict[str, Any] = field(default_factory=dict)
    plugin_reload: dict[str, Any] = field(default_factory=dict)
    invalidated_invocations: tuple[str, ...] = ()
    error_code: str = ""
    error_message: str = ""
    revision: int = 0
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(include_paths=False),
            "status": str(self.status),
            "preflight": self.preflight.to_dict() if self.preflight else None,
            "permission_decision": {
                "decision": self.permission_decision.decision,
                "request_id": self.permission_decision.request_id,
                "reason": self.permission_decision.reason,
            }
            if self.permission_decision
            else None,
            "receipt": self.receipt.to_dict() if self.receipt else None,
            "registry_reload": copy.deepcopy(self.registry_reload),
            "plugin_reload": copy.deepcopy(self.plugin_reload),
            "invalidated_invocations": list(self.invalidated_invocations),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class SkillUpdatePreflightRuntime:
    def __init__(
        self,
        *,
        max_files: int = 2_000,
        max_total_bytes: int = 100_000_000,
        max_single_file_bytes: int = 10_000_000,
    ) -> None:
        self.max_files = max_files
        self.max_total_bytes = max_total_bytes
        self.max_single_file_bytes = max_single_file_bytes

    def inspect(self, request: SkillUpdateRequest) -> SkillUpdatePreflight:
        source = canonical_root(request.source_root)
        target = Path(request.target_root).resolve()
        findings: list[SkillUpdateFinding] = []
        skill_dirs = iter_skill_directories(source)
        if not skill_dirs:
            findings.append(
                SkillUpdateFinding(
                    code="skill_update_empty",
                    severity="blocker",
                    message="skill update bundle contains no SKILL.md directories",
                    path=str(source),
                )
            )
        file_count = 0
        total_bytes = 0
        file_records: list[dict[str, Any]] = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                findings.append(
                    SkillUpdateFinding(
                        code="skill_update_symlink",
                        severity="blocker",
                        message="skill update bundle contains a symlink",
                        path=str(path),
                    )
                )
                continue
            if not path.is_file():
                continue
            file_count += 1
            size = path.stat().st_size
            total_bytes += size
            if size > self.max_single_file_bytes:
                findings.append(
                    SkillUpdateFinding(
                        code="skill_update_file_too_large",
                        severity="blocker",
                        message="skill update file exceeds size limit",
                        path=str(path),
                        detail={"size": size, "maximum": self.max_single_file_bytes},
                    )
                )
            suffix = path.suffix.lower()
            if suffix in {".exe", ".dll", ".so", ".dylib", ".ps1", ".bat", ".cmd"}:
                findings.append(
                    SkillUpdateFinding(
                        code="skill_update_executable_payload",
                        severity="blocker",
                        message="skill update contains an executable payload",
                        path=str(path),
                    )
                )
            relative = path.relative_to(source).as_posix()
            file_records.append(
                {"path": relative, "size": size, "digest": f"sha256:{digest_file(path)}"}
            )
        if file_count > self.max_files:
            findings.append(
                SkillUpdateFinding(
                    code="skill_update_file_limit",
                    severity="blocker",
                    message="skill update contains too many files",
                    detail={"count": file_count, "maximum": self.max_files},
                )
            )
        if total_bytes > self.max_total_bytes:
            findings.append(
                SkillUpdateFinding(
                    code="skill_update_size_limit",
                    severity="blocker",
                    message="skill update exceeds total size limit",
                    detail={"size": total_bytes, "maximum": self.max_total_bytes},
                )
            )
        source_digest = digest_object(file_records)
        base_digest = _directory_digest(target) if target.exists() else ""
        if request.expected_base_digest and request.expected_base_digest != base_digest:
            findings.append(
                SkillUpdateFinding(
                    code="skill_update_base_changed",
                    severity="blocker",
                    message="skill update target changed after it was read",
                    path=str(target),
                    detail={"expected": request.expected_base_digest, "actual": base_digest},
                )
            )
        request_digest = digest_object(
            {
                "request": request.to_dict(include_paths=False),
                "source_digest": source_digest,
                "base_digest": base_digest,
            }
        )
        return SkillUpdatePreflight(
            request_digest=request_digest,
            source_digest=source_digest,
            base_digest=base_digest,
            skill_count=len(skill_dirs),
            total_bytes=total_bytes,
            findings=tuple(findings),
        )


class SkillUpdateRuntime:
    """Permissioned local bundle update/rollback control path."""

    def __init__(
        self,
        *,
        updater: AtomicSkillPackageUpdater,
        permission_port: SkillUpdatePermissionPort | None = None,
        skill_runtime: Any | None = None,
        plugin_runtime: Any | None = None,
        preflight_runtime: SkillUpdatePreflightRuntime | None = None,
        runtime_rebuilder: Callable[[SkillUpdateRequest], Any] | None = None,
    ) -> None:
        self.updater = updater
        self.permission_port = permission_port or DenySkillUpdatePermission()
        self.skill_runtime = skill_runtime
        self.plugin_runtime = plugin_runtime
        self.preflight_runtime = preflight_runtime or SkillUpdatePreflightRuntime()
        self.runtime_rebuilder = runtime_rebuilder
        self._lock = RLock()
        self._states: dict[str, SkillUpdateState] = {}

    def request(self, request: SkillUpdateRequest) -> SkillUpdateState:
        with self._lock:
            existing = self._states.get(request.update_id)
            if existing is not None:
                if existing.request != request:
                    raise SkillReloadRejected("skill update id was reused with different input")
                return existing
            state = SkillUpdateState(request=request, status=SkillUpdateStatus.REQUESTED)
            self._states[request.update_id] = state
        if request.action is SkillUpdateAction.ROLLBACK:
            return self._rollback(state)
        if request.action not in {SkillUpdateAction.INSTALL, SkillUpdateAction.UPDATE}:
            return self._control_action(state)
        return self._install_or_update(state)

    def resume(self, update_id: str) -> SkillUpdateState:
        with self._lock:
            state = self._states.get(update_id)
        if state is None:
            raise SkillReloadRejected("skill update request was not found")
        if state.status is not SkillUpdateStatus.PERMISSION_PENDING:
            return state
        if state.request.action is SkillUpdateAction.ROLLBACK:
            return self._rollback(state)
        if state.request.action not in {
            SkillUpdateAction.INSTALL,
            SkillUpdateAction.UPDATE,
        }:
            return self._control_action(state)
        # Re-run preflight after an approval delay.  The source/target may have
        # changed while the request was pending and old findings are not an
        # execution capability.
        return self._install_or_update(state, reuse_preflight=False)

    def state(self, update_id: str) -> SkillUpdateState:
        with self._lock:
            state = self._states.get(update_id)
        if state is None:
            raise SkillReloadRejected("skill update request was not found")
        return state

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = {key: value.to_dict() for key, value in self._states.items()}
            pending = {
                key: value.request.to_dict(include_paths=True)
                for key, value in self._states.items()
                if value.status is SkillUpdateStatus.PERMISSION_PENDING
            }
        payload = {
            "owner": "M1-03C SkillUpdateRuntime",
            "states": values,
            "pending_requests": pending,
        }
        payload["snapshot_digest"] = digest_object(payload)
        return payload

    def restore_pending(self, snapshot: Mapping[str, Any] | None) -> None:
        if not snapshot:
            return
        raw = dict(snapshot)
        supplied = str(raw.pop("snapshot_digest", ""))
        if supplied and supplied != digest_object(raw):
            raise SkillReloadRejected("skill update runtime snapshot digest mismatch")
        pending = raw.get("pending_requests")
        if not isinstance(pending, Mapping):
            return
        restored: dict[str, SkillUpdateState] = {}
        public_states = raw.get("states") if isinstance(raw.get("states"), Mapping) else {}
        for update_id, value in pending.items():
            if not isinstance(value, Mapping):
                raise SkillReloadRejected("pending skill update request is invalid")
            request = SkillUpdateRequest(
                update_id=str(value.get("update_id") or update_id),
                action=SkillUpdateAction(str(value.get("action") or "")),
                run_id=str(value.get("run_id") or ""),
                task_id=str(value.get("task_id") or ""),
                session_id=str(value.get("session_id") or ""),
                source_root=str(value.get("source_root") or ""),
                target_root=str(value.get("target_root") or ""),
                source_kind=SkillSourceKind(str(value.get("source_kind") or "project")),
                source_id=str(value.get("source_id") or ""),
                namespace=str(value.get("namespace") or ""),
                expected_base_digest=str(value.get("expected_base_digest") or ""),
                plugin_id=str(value.get("plugin_id") or ""),
                receipt_id=str(value.get("receipt_id") or ""),
                reason=str(value.get("reason") or ""),
                interactive=bool(value.get("interactive", True)),
                headless=bool(value.get("headless", False)),
                requested_at=str(value.get("requested_at") or utc_now()),
            )
            public = public_states.get(update_id) if isinstance(public_states, Mapping) else None
            revision = int(public.get("revision") or 0) if isinstance(public, Mapping) else 0
            restored[str(update_id)] = SkillUpdateState(
                request=request,
                status=SkillUpdateStatus.PERMISSION_PENDING,
                revision=revision,
            )
        with self._lock:
            for update_id, state in restored.items():
                self._states.setdefault(update_id, state)

    def _install_or_update(
        self,
        state: SkillUpdateState,
        *,
        reuse_preflight: bool = False,
    ) -> SkillUpdateState:
        request = state.request
        installed_receipt: SkillUpdateReceipt | None = None
        try:
            preflight = state.preflight if reuse_preflight else self.preflight_runtime.inspect(request)
            if preflight is None or not preflight.ok:
                raise SkillPluginSupplyChainRejected(
                    "skill update preflight rejected the bundle",
                    detail={
                        "findings": [item.to_dict() for item in (preflight.findings if preflight else ())]
                    },
                )
            state = self._replace(
                state,
                status=SkillUpdateStatus.VALIDATED,
                preflight=preflight,
            )
            decision = self.permission_port.guard_update(request, preflight)
            if decision.pending:
                state = self._replace(
                    state,
                    status=SkillUpdateStatus.PERMISSION_PENDING,
                    permission_decision=decision,
                )
                raise SkillPluginUpdatePending(
                    "skill update requires M1-03A approval",
                    detail={"update_id": request.update_id, "request_id": decision.request_id},
                )
            if not decision.allowed:
                state = self._replace(
                    state,
                    status=SkillUpdateStatus.PERMISSION_DENIED,
                    permission_decision=decision,
                    error_code="skill_plugin_update_denied",
                    error_message=decision.reason,
                )
                raise SkillPluginUpdateDenied(decision.reason)
            receipt = self.updater.install(
                source_root=request.source_root,
                target_root=request.target_root,
                source_kind=request.source_kind,
                source_id=request.source_id,
                namespace=request.namespace,
            )
            installed_receipt = receipt
            state = self._replace(
                state,
                status=SkillUpdateStatus.STAGED,
                permission_decision=decision,
                receipt=receipt,
            )
            registry_reload, plugin_reload, invalidated = self._reload_dependents(
                reason=f"skill update {request.update_id} committed",
                request=request,
            )
            return self._replace(
                state,
                status=SkillUpdateStatus.COMMITTED,
                registry_reload=registry_reload,
                plugin_reload=plugin_reload,
                invalidated_invocations=invalidated,
            )
        except (SkillPluginUpdatePending, SkillPluginUpdateDenied):
            raise
        except Exception as error:
            rollback_error = ""
            if installed_receipt is not None:
                try:
                    self.updater.rollback(installed_receipt.update_id)
                    if self.runtime_rebuilder is not None:
                        self.skill_runtime = self.runtime_rebuilder(request)
                except Exception as rollback_failure:  # noqa: BLE001 - retain both failure causes.
                    rollback_error = f"; rollback failed: {rollback_failure}"
            self._replace(
                state,
                status=SkillUpdateStatus.FAILED,
                error_code=str(getattr(error, "code", "skill_update_failed")),
                error_message=f"{error}{rollback_error}",
            )
            raise

    def _rollback(self, state: SkillUpdateState) -> SkillUpdateState:
        request = state.request
        preflight = SkillUpdatePreflight(
            request_digest=digest_object(request.permission_arguments()),
            source_digest="",
            base_digest="",
            skill_count=0,
            total_bytes=0,
            findings=(),
        )
        decision = self.permission_port.guard_update(request, preflight)
        if decision.pending:
            self._replace(
                state,
                status=SkillUpdateStatus.PERMISSION_PENDING,
                preflight=preflight,
                permission_decision=decision,
            )
            raise SkillPluginUpdatePending("skill rollback requires M1-03A approval")
        if not decision.allowed:
            self._replace(
                state,
                status=SkillUpdateStatus.PERMISSION_DENIED,
                preflight=preflight,
                permission_decision=decision,
            )
            raise SkillPluginUpdateDenied(decision.reason)
        try:
            receipt = self.updater.rollback(request.receipt_id)
            registry_reload, plugin_reload, invalidated = self._reload_dependents(
                reason=f"skill update {request.receipt_id} rolled back",
                request=request,
            )
            return self._replace(
                state,
                status=SkillUpdateStatus.ROLLED_BACK,
                preflight=preflight,
                permission_decision=decision,
                receipt=receipt,
                registry_reload=registry_reload,
                plugin_reload=plugin_reload,
                invalidated_invocations=invalidated,
            )
        except Exception as error:
            self._replace(
                state,
                status=SkillUpdateStatus.FAILED,
                error_code=str(getattr(error, "code", "skill_rollback_failed")),
                error_message=str(error),
            )
            raise SkillPluginRollbackError(str(error)) from error

    def _control_action(self, state: SkillUpdateState) -> SkillUpdateState:
        request = state.request
        preflight = SkillUpdatePreflight(
            request_digest=digest_object(request.permission_arguments()),
            source_digest="",
            base_digest="",
            skill_count=0,
            total_bytes=0,
            findings=(),
        )
        decision = self.permission_port.guard_update(request, preflight)
        if decision.pending:
            self._replace(
                state,
                status=SkillUpdateStatus.PERMISSION_PENDING,
                preflight=preflight,
                permission_decision=decision,
            )
            raise SkillPluginUpdatePending("skill control action requires M1-03A approval")
        if not decision.allowed:
            self._replace(
                state,
                status=SkillUpdateStatus.PERMISSION_DENIED,
                preflight=preflight,
                permission_decision=decision,
            )
            raise SkillPluginUpdateDenied(decision.reason)
        if self.plugin_runtime is not None and request.plugin_id:
            if request.action in {SkillUpdateAction.DISABLE, SkillUpdateAction.REVOKE}:
                self.plugin_runtime.disable(request.plugin_id, reason=request.reason or str(request.action))
            elif request.action is SkillUpdateAction.ENABLE:
                self.plugin_runtime.enable(request.plugin_id)
        registry_reload, plugin_reload, invalidated = self._reload_dependents(
            reason=f"skill control action {request.action}",
            request=request,
        )
        return self._replace(
            state,
            status=SkillUpdateStatus.COMMITTED,
            preflight=preflight,
            permission_decision=decision,
            registry_reload=registry_reload,
            plugin_reload=plugin_reload,
            invalidated_invocations=invalidated,
        )

    def _reload_dependents(
        self,
        *,
        reason: str,
        request: SkillUpdateRequest,
    ) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...]]:
        if self.runtime_rebuilder is not None:
            self.skill_runtime = self.runtime_rebuilder(request)
        plugin_reload: dict[str, Any] = {}
        if self.plugin_runtime is not None:
            result = self.plugin_runtime.refresh()
            plugin_reload = result.to_dict() if hasattr(result, "to_dict") else {}
            if hasattr(result, "applied") and not result.applied:
                raise SkillReloadRejected("plugin capability reload rejected after package update")
        registry_reload: dict[str, Any] = {}
        invalidated: list[str] = list(
            getattr(self.skill_runtime, "_skill_update_invalidated", ())
            if self.skill_runtime is not None
            else ()
        )
        if self.skill_runtime is not None:
            before = {
                state.invocation_id: state.version_ref.immutable_ref
                for state in self.skill_runtime.state_store.all_states()
                if not state.status.terminal
            }
            result = self.skill_runtime.reload_coordinator.reload()
            registry_reload = result.to_dict()
            if not result.registry.applied:
                raise SkillReloadRejected("skill registry reload rejected after package update")
            after_refs = set(result.registry.changed_refs) | set(result.registry.removed_refs)
            for invocation_id, immutable_ref in before.items():
                if immutable_ref in after_refs:
                    invalidated.append(invocation_id)
        return registry_reload, plugin_reload, tuple(sorted(invalidated))

    def _replace(self, state: SkillUpdateState, **changes: Any) -> SkillUpdateState:
        updated = replace(
            state,
            **changes,
            revision=state.revision + 1,
            updated_at=utc_now(),
        )
        with self._lock:
            current = self._states.get(state.request.update_id)
            if current is not None and current.revision != state.revision:
                raise SkillReloadRejected("skill update state changed concurrently")
            self._states[state.request.update_id] = updated
        return updated


def _directory_digest(root: Path) -> str:
    if not root.exists():
        return ""
    records: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            records.append({"path": path.relative_to(root).as_posix(), "symlink": True})
        elif path.is_file():
            records.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "size": path.stat().st_size,
                    "digest": f"sha256:{digest_file(path)}",
                }
            )
    return digest_object(records)
