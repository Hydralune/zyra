from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .errors import BrowserConnectionLost, BrowserSessionBusy, BrowserSessionNotFound, classify_browser_error
from .integration_models import integration_digest, public_mapping
from .models import (
    BrowserLifecycleEvent,
    BrowserSessionCommand,
    BrowserSessionRef,
    BrowserSessionStartResult,
    BrowserSessionStatus,
    browser_id,
    browser_now,
)
from .runtime import BrowserRuntime


CAPSULE_SCHEMA = "zyra.browser-session-resume-capsule.v1"
CAPSULE_KEY_PREFIX = "browser-resume-capsule:"


class BrowserResumeDisposition(StrEnum):
    LIVE_REBOUND = "live_rebound"
    RECONNECTED = "reconnected"
    RESTARTED = "restarted"
    STOPPED = "stopped"
    STATE_LOST = "state_lost"
    NOT_FOUND = "not_found"
    IDENTITY_CONFLICT = "identity_conflict"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BrowserResumeCapsule:
    browser_session_id: str
    run_id: str
    task_id: str
    canonical_session_id: str
    worker_request_id: str
    workspace_root: str
    artifact_root: str
    endpoint_url: str
    connection_mode: str
    last_known_endpoint: str
    executable_path: str
    headless: bool
    keep_alive: bool
    profile_id: str
    header_names: tuple[str, ...] = ()
    headers_digest: str = ""
    proxy_url: str = ""
    proxy_requires_secret: bool = False
    constraints: Mapping[str, Any] = field(default_factory=dict)
    session_revision: int = 0
    captured_at: str = field(default_factory=browser_now)
    capsule_id: str = field(default_factory=lambda: browser_id("brresume"))
    schema: str = CAPSULE_SCHEMA

    def __post_init__(self) -> None:
        if self.schema != CAPSULE_SCHEMA:
            raise ValueError("browser resume capsule schema mismatch")
        if not all((self.browser_session_id, self.run_id, self.task_id, self.canonical_session_id)):
            raise ValueError("browser resume capsule identity is incomplete")
        if self.session_revision < 0:
            raise ValueError("browser resume capsule revision cannot be negative")
        object.__setattr__(self, "endpoint_url", redact_url(self.endpoint_url))
        object.__setattr__(self, "last_known_endpoint", redact_url(self.last_known_endpoint))
        object.__setattr__(self, "proxy_url", redact_url(self.proxy_url))
        object.__setattr__(self, "header_names", tuple(sorted(set(str(item) for item in self.header_names if str(item)))))
        object.__setattr__(self, "constraints", public_mapping(self.constraints))

    @property
    def fingerprint(self) -> str:
        return integration_digest(self._fingerprint_payload())

    def _fingerprint_payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "browser_session_id": self.browser_session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_session_id": self.canonical_session_id,
            "workspace_root": self.workspace_root,
            "artifact_root": self.artifact_root,
            "endpoint_url": self.endpoint_url,
            "connection_mode": self.connection_mode,
            "last_known_endpoint": self.last_known_endpoint,
            "executable_path": self.executable_path,
            "headless": self.headless,
            "keep_alive": self.keep_alive,
            "profile_id": self.profile_id,
            "header_names": list(self.header_names),
            "headers_digest": self.headers_digest,
            "proxy_url": self.proxy_url,
            "proxy_requires_secret": self.proxy_requires_secret,
            "constraints": dict(self.constraints),
            "session_revision": self.session_revision,
        }

    @property
    def has_unrecoverable_secrets(self) -> bool:
        return bool(self.header_names) or self.proxy_requires_secret

    @property
    def can_restart(self) -> bool:
        if self.has_unrecoverable_secrets:
            return False
        if self.endpoint_url:
            return True
        return bool(self.executable_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "capsule_id": self.capsule_id,
            "browser_session_id": self.browser_session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_session_id": self.canonical_session_id,
            "worker_request_id": self.worker_request_id,
            "workspace_root": self.workspace_root,
            "artifact_root": self.artifact_root,
            "endpoint_url": self.endpoint_url,
            "connection_mode": self.connection_mode,
            "last_known_endpoint": self.last_known_endpoint,
            "executable_path": self.executable_path,
            "headless": self.headless,
            "keep_alive": self.keep_alive,
            "profile_id": self.profile_id,
            "header_names": list(self.header_names),
            "headers_digest": self.headers_digest,
            "proxy_url": self.proxy_url,
            "proxy_requires_secret": self.proxy_requires_secret,
            "constraints": dict(self.constraints),
            "session_revision": self.session_revision,
            "captured_at": self.captured_at,
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "BrowserResumeCapsule":
        return cls(
            schema=str(value.get("schema") or ""),
            capsule_id=str(value.get("capsule_id") or browser_id("brresume")),
            browser_session_id=str(value.get("browser_session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            canonical_session_id=str(value.get("canonical_session_id") or ""),
            worker_request_id=str(value.get("worker_request_id") or ""),
            workspace_root=str(value.get("workspace_root") or ""),
            artifact_root=str(value.get("artifact_root") or ""),
            endpoint_url=str(value.get("endpoint_url") or ""),
            connection_mode=str(value.get("connection_mode") or ("local_launch" if value.get("executable_path") else "remote_attach")),
            last_known_endpoint=str(value.get("last_known_endpoint") or value.get("endpoint_url") or ""),
            executable_path=str(value.get("executable_path") or ""),
            headless=bool(value.get("headless", True)),
            keep_alive=bool(value.get("keep_alive", False)),
            profile_id=str(value.get("profile_id") or ""),
            header_names=tuple(str(item) for item in value.get("header_names", ())),
            headers_digest=str(value.get("headers_digest") or ""),
            proxy_url=str(value.get("proxy_url") or ""),
            proxy_requires_secret=bool(value.get("proxy_requires_secret", False)),
            constraints=dict(value.get("constraints") or {}),
            session_revision=int(value.get("session_revision") or 0),
            captured_at=str(value.get("captured_at") or browser_now()),
        )


@dataclass(frozen=True, slots=True)
class BrowserResumeResult:
    ok: bool
    disposition: BrowserResumeDisposition
    session: BrowserSessionRef | None
    capsule: BrowserResumeCapsule | None
    diagnostic: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()
    error_code: str = ""
    error_message: str = ""
    recovery_input: "BrowserStateLossRecoveryInput | None" = None
    resumed_at: str = field(default_factory=browser_now)

    @property
    def error(self) -> str:
        """Compatibility alias for callers that consume the common result API."""
        return self.error_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "disposition": str(self.disposition),
            "session": self.session.to_dict() if self.session else None,
            "capsule": self.capsule.to_dict() if self.capsule else None,
            "diagnostic": public_mapping(self.diagnostic),
            "events": [dict(item) for item in self.events],
            "error_code": self.error_code,
            "error_message": self.error_message,
            "recovery_input": self.recovery_input.to_dict() if self.recovery_input else None,
            "resumed_at": self.resumed_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserStateLossRecoveryInput:
    browser_session_id: str
    run_id: str
    task_id: str
    reason: str
    required_inputs: tuple[str, ...]
    retryable: bool
    previous_profile_id: str = ""
    executable_path: str = ""
    remote_endpoint: str = ""
    secret_material_required: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "browser_session_id": self.browser_session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "reason": self.reason,
            "required_inputs": list(self.required_inputs),
            "retryable": self.retryable,
            "previous_profile_id": self.previous_profile_id,
            "executable_path": self.executable_path,
            "remote_endpoint": redact_url(self.remote_endpoint),
            "secret_material_required": self.secret_material_required,
        }


class BrowserResumeCapsulePort:
    """Stores resume capsules inside the existing BrowserStatePort.

    JsonBrowserStateStore already owns generic lease and idempotent request
    records. This adapter uses those namespaces and never creates another file
    or independent state owner.
    """

    def __init__(self, state_store: Any) -> None:
        self.state_store = state_store

    @staticmethod
    def _key(session_id: str) -> str:
        return CAPSULE_KEY_PREFIX + session_id

    @staticmethod
    def _request_fingerprint(session_id: str) -> str:
        return integration_digest({"kind": CAPSULE_SCHEMA, "browser_session_id": session_id})

    def put(self, capsule: BrowserResumeCapsule) -> None:
        key = self._key(capsule.browser_session_id)
        value = {"kind": CAPSULE_SCHEMA, "resume_capsule": capsule.to_dict()}
        if hasattr(self.state_store, "put_lease"):
            self.state_store.put_lease(key, value)
            return
        if hasattr(self.state_store, "record_request"):
            existing = self.get(capsule.browser_session_id)
            if existing is not None and existing.fingerprint != capsule.fingerprint:
                # request records are immutable by design. A revision-specific
                # key preserves every capsule while the latest pointer remains
                # derivable from the persisted BrowserSessionRef.
                key = f"{key}:revision:{capsule.session_revision}"
            self.state_store.record_request(key, self._request_fingerprint(capsule.browser_session_id), value)
            return
        raise TypeError("BrowserStatePort cannot persist a resume capsule")

    def get(self, session_id: str, *, session_revision: int | None = None) -> BrowserResumeCapsule | None:
        keys = [self._key(session_id)]
        if session_revision is not None:
            keys.insert(0, f"{self._key(session_id)}:revision:{session_revision}")
        for key in keys:
            value: Any = None
            if hasattr(self.state_store, "get_lease"):
                value = self.state_store.get_lease(key)
            elif hasattr(self.state_store, "request_result"):
                value = self.state_store.request_result(key, self._request_fingerprint(session_id))
            if isinstance(value, Mapping):
                raw = value.get("resume_capsule")
                if isinstance(raw, Mapping):
                    return BrowserResumeCapsule.from_dict(raw)
        return None


class BrowserSessionResumeRuntime:
    def __init__(self, browser_runtime: BrowserRuntime, *, disabled: bool = False) -> None:
        self.browser_runtime = browser_runtime
        self.disabled = disabled
        self.session_runtime = getattr(browser_runtime, "_runtime", None)
        if self.session_runtime is None:
            raise TypeError("BrowserRuntime has no BrowserSessionRuntime")
        self.state_store = browser_runtime.state_store
        self.capsules = BrowserResumeCapsulePort(self.state_store)

    def capture(self, session_id: str) -> BrowserResumeCapsule:
        self._ensure_available()
        session = self.browser_runtime.get_session(session_id)
        command = getattr(self.session_runtime, "_commands", {}).get(session_id)
        policy = getattr(self.session_runtime, "_connection_policies", {}).get(session_id)
        capsule = self._capsule_from(session, command, policy)
        self.capsules.put(capsule)
        return capsule

    def resume(
        self,
        session_id: str,
        *,
        expected_run_id: str = "",
        expected_task_id: str = "",
        worker_request_id: str = "",
    ) -> BrowserResumeResult:
        self._ensure_available()
        try:
            session = self.browser_runtime.get_session(session_id)
        except BrowserSessionNotFound as error:
            return BrowserResumeResult(
                ok=False,
                disposition=BrowserResumeDisposition.NOT_FOUND,
                session=None,
                capsule=None,
                error_code=error.error_code,
                error_message=str(error),
            )
        identity_error = self._identity_error(session, expected_run_id, expected_task_id)
        if identity_error:
            return BrowserResumeResult(
                ok=False,
                disposition=BrowserResumeDisposition.IDENTITY_CONFLICT,
                session=session,
                capsule=None,
                error_code="browser_resume_identity_conflict",
                error_message=identity_error,
            )
        command = getattr(self.session_runtime, "_commands", {}).get(session_id)
        policy = getattr(self.session_runtime, "_connection_policies", {}).get(session_id)
        capsule = self.capsules.get(session_id, session_revision=session.revision)
        if command is not None:
            capsule = self._capsule_from(session, command, policy)
            self.capsules.put(capsule)
        if session.status == str(BrowserSessionStatus.STOPPED) and not session.keep_alive:
            return BrowserResumeResult(
                ok=False,
                disposition=BrowserResumeDisposition.STOPPED,
                session=session,
                capsule=capsule,
                error_code="browser_session_stopped",
                error_message="stopped non-keep-alive browser session cannot be resumed implicitly",
            )
        if self._components_live(session_id):
            if command is None and capsule is not None:
                command = self._command_from_capsule(capsule, worker_request_id=worker_request_id)
                getattr(self.session_runtime, "_commands", {})[session_id] = command
            if command is not None:
                self.session_runtime.worker_bridge.attach(command, session)
            event = self._emit(command, session, "browser.session.live_rebound", {"capsule_id": capsule.capsule_id if capsule else ""})
            return BrowserResumeResult(
                ok=True,
                disposition=BrowserResumeDisposition.LIVE_REBOUND,
                session=session,
                capsule=capsule,
                diagnostic=self.browser_runtime.diagnose(session_id).to_dict(),
                events=(event,) if event else (),
            )
        if capsule is None:
            return self._state_lost(session, None, "browser resume capsule is unavailable")
        explicit_memory_transport = (
            capsule.connection_mode == "memory_transport"
            or str(capsule.constraints.get("browser_transport") or "").lower() == "memory"
        )
        memory_reconstructable = explicit_memory_transport and bool(capsule.endpoint_url)
        if capsule.has_unrecoverable_secrets and not explicit_memory_transport:
            return self._state_lost(
                session,
                capsule,
                "browser connection used secret headers/proxy credentials that were intentionally not persisted",
            )
        if not capsule.can_restart and not memory_reconstructable:
            return self._state_lost(session, capsule, "browser resume capsule has no endpoint or executable")
        profile = self.session_runtime.profile_store.get(session.profile_id) if session.profile_id else None
        if session.profile_id and profile is None and not explicit_memory_transport:
            return self._state_lost(session, capsule, "browser profile state is missing")
        command = self._command_from_capsule(capsule, worker_request_id=worker_request_id)
        try:
            current = self.browser_runtime.get_session(session_id)
            if current.status not in {str(BrowserSessionStatus.STOPPED), str(BrowserSessionStatus.FAILED)}:
                failed = replace(
                    current,
                    status=str(BrowserSessionStatus.FAILED),
                    revision=current.revision + 1,
                    updated_at=browser_now(),
                )
                self.state_store.update_session(failed, expected_revision=current.revision)
            started = self.browser_runtime.ensure_started(command)
            refreshed_capsule = self._capsule_from(started.session, command, getattr(self.session_runtime, "_connection_policies", {}).get(session_id))
            self.capsules.put(refreshed_capsule)
            disposition = BrowserResumeDisposition.RECONNECTED if capsule.endpoint_url else BrowserResumeDisposition.RESTARTED
            event = self._emit(command, started.session, "browser.session.resume_completed", {"disposition": str(disposition)})
            return BrowserResumeResult(
                ok=started.ok,
                disposition=disposition if started.ok else BrowserResumeDisposition.FAILED,
                session=started.session,
                capsule=refreshed_capsule,
                diagnostic=started.diagnostic.to_dict(),
                events=tuple((*started.events, *((event,) if event else ()))),
                error_code="" if started.ok else started.error,
                error_message="" if started.ok else "browser runtime did not reach running state",
            )
        except Exception as error:
            failure = classify_browser_error(error, operation="resume", session_id=session_id)
            return BrowserResumeResult(
                ok=False,
                disposition=BrowserResumeDisposition.FAILED,
                session=self.state_store.get_session(session_id),
                capsule=capsule,
                error_code=failure.code,
                error_message=failure.message,
                diagnostic={"retryable": failure.retryable, "failure_kind": str(failure.kind)},
            )

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserConnectionLost("browser resume runtime is disabled")

    @staticmethod
    def _identity_error(session: BrowserSessionRef, run_id: str, task_id: str) -> str:
        if run_id and session.run_id != run_id:
            return "browser session run identity does not match resume request"
        if task_id and session.task_id != task_id:
            return "browser session task identity does not match resume request"
        return ""

    def _components_live(self, session_id: str) -> bool:
        cdp = getattr(self.session_runtime, "_cdp", {}).get(session_id)
        target = getattr(self.session_runtime, "_targets", {}).get(session_id)
        event_bus = getattr(self.session_runtime, "_event_buses", {}).get(session_id)
        if cdp is None or target is None or event_bus is None:
            return False
        try:
            cdp_open = str(cdp.snapshot().status) == "open"
            target_valid = bool(target.ensure_valid_focus().target_id)
            bus_running = str(event_bus.snapshot().state) == "running"
        except Exception:
            return False
        return cdp_open and target_valid and bus_running

    def _capsule_from(self, session: BrowserSessionRef, command: BrowserSessionCommand | None, policy: Any) -> BrowserResumeCapsule:
        if command is None:
            return BrowserResumeCapsule(
                browser_session_id=session.session_id,
                run_id=session.run_id,
                task_id=session.task_id,
                canonical_session_id=session.canonical_session_id,
                worker_request_id=session.worker_request_id,
                workspace_root=str(self.browser_runtime.config.runtime_root),
                artifact_root=str(self.browser_runtime.config.artifact_root),
                endpoint_url=session.endpoint_url,
                connection_mode="remote_attach",
                last_known_endpoint=session.endpoint_url,
                executable_path="",
                headless=True,
                keep_alive=session.keep_alive,
                profile_id=session.profile_id,
                session_revision=session.revision,
            )
        header_names = tuple(str(key) for key in command.headers)
        headers_digest = integration_digest(dict(command.headers)) if command.headers else ""
        proxy_url = str(command.proxy_url or "")
        transport_mode = str(command.constraints.get("browser_transport") or "").lower()
        connection_mode = (
            "memory_transport"
            if transport_mode == "memory"
            else ("remote_attach" if command.endpoint_url else "local_launch")
        )
        policy_launch = getattr(policy, "launch", None)
        resolved_executable = command.executable_path or getattr(policy_launch, "executable", None)
        return BrowserResumeCapsule(
            browser_session_id=session.session_id,
            run_id=session.run_id,
            task_id=session.task_id,
            canonical_session_id=session.canonical_session_id,
            worker_request_id=command.worker_request_id,
            workspace_root=str(command.workspace_root),
            artifact_root=str(command.artifact_root),
            endpoint_url=str(command.endpoint_url or ""),
            connection_mode=connection_mode,
            last_known_endpoint=session.endpoint_url,
            executable_path=str(resolved_executable or ""),
            headless=command.headless,
            keep_alive=command.keep_alive,
            profile_id=session.profile_id,
            header_names=header_names,
            headers_digest=headers_digest,
            proxy_url=proxy_url,
            proxy_requires_secret=url_has_credentials(proxy_url),
            constraints=redacted_resume_constraints(command.constraints),
            session_revision=session.revision,
        )

    @staticmethod
    def _command_from_capsule(capsule: BrowserResumeCapsule, *, worker_request_id: str) -> BrowserSessionCommand:
        return BrowserSessionCommand(
            run_id=capsule.run_id,
            task_id=capsule.task_id,
            worker_request_id=worker_request_id or capsule.worker_request_id,
            canonical_session_id=capsule.canonical_session_id,
            browser_session_id=capsule.browser_session_id,
            workspace_root=Path(capsule.workspace_root),
            artifact_root=Path(capsule.artifact_root),
            executable_path=Path(capsule.executable_path) if capsule.executable_path else None,
            endpoint_url=(
                capsule.endpoint_url
                if capsule.connection_mode in {"remote_attach", "memory_transport"}
                else ""
            ),
            headless=capsule.headless,
            keep_alive=capsule.keep_alive,
            headers={},
            proxy_url=capsule.proxy_url,
            constraints={
                **dict(capsule.constraints),
                **({"browser_transport": "memory"} if capsule.connection_mode == "memory_transport" else {}),
            },
        )

    def _state_lost(self, session: BrowserSessionRef, capsule: BrowserResumeCapsule | None, reason: str) -> BrowserResumeResult:
        current = self.state_store.get_session(session.session_id)
        if current is not None and current.status != str(BrowserSessionStatus.FAILED):
            try:
                failed = replace(current, status=str(BrowserSessionStatus.FAILED), revision=current.revision + 1, updated_at=browser_now())
                current = self.state_store.update_session(failed, expected_revision=current.revision)
            except Exception:
                current = self.state_store.get_session(session.session_id)
        required: list[str] = []
        if capsule is None:
            required.append("new_browser_session_command")
        else:
            if capsule.has_unrecoverable_secrets:
                required.append("connection_secret_material")
            if capsule.connection_mode == "remote_attach" and not capsule.endpoint_url:
                required.append("remote_endpoint_url")
            if capsule.connection_mode == "local_launch" and not capsule.executable_path:
                required.append("browser_executable_path")
            if session.profile_id and self.session_runtime.profile_store.get(session.profile_id) is None:
                required.append("new_isolated_profile")
        recovery = BrowserStateLossRecoveryInput(
            browser_session_id=session.session_id,
            run_id=session.run_id,
            task_id=session.task_id,
            reason=reason,
            required_inputs=tuple(dict.fromkeys(required)),
            retryable=bool(capsule and (capsule.can_restart or capsule.has_unrecoverable_secrets)),
            previous_profile_id=session.profile_id,
            executable_path=capsule.executable_path if capsule else "",
            remote_endpoint=capsule.endpoint_url if capsule else "",
            secret_material_required=bool(capsule and capsule.has_unrecoverable_secrets),
        )
        return BrowserResumeResult(
            ok=False,
            disposition=BrowserResumeDisposition.STATE_LOST,
            session=current,
            capsule=capsule,
            error_code="browser_session_state_lost",
            error_message=reason,
            recovery_input=recovery,
            diagnostic={
                "requires_new_session": True,
                "secret_material_was_not_persisted": bool(capsule and capsule.has_unrecoverable_secrets),
            },
        )

    def _emit(
        self,
        command: BrowserSessionCommand | None,
        session: BrowserSessionRef,
        topic: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if command is None:
            return None
        event = BrowserLifecycleEvent(
            topic=topic,
            session_id=session.session_id,
            run_id=session.run_id,
            task_id=session.task_id,
            worker_request_id=command.worker_request_id,
            generation=session.revision,
            payload=public_mapping(payload),
        )
        bridge = getattr(self.session_runtime, "artifact_bridge", None)
        if bridge is not None and hasattr(bridge, "append"):
            bridge.append(event)
        return event.to_dict()


def redact_url(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return "[INVALID_URL]"
    host = parsed.hostname or ""
    if port:
        host = f"{host}:{port}"
    query = ""
    if parsed.query:
        query = "[REDACTED]"
    return urlunsplit((parsed.scheme, host, parsed.path, query, ""))


def url_has_credentials(value: str) -> bool:
    if not value:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return True
    return parsed.username is not None or parsed.password is not None


def redacted_resume_constraints(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "headless",
        "keep_alive",
        "browser_backend",
        "continue_on_error",
        "allowed_hosts",
        "browser_session_lease_ttl_seconds",
        "verify_tls",
        "browser_debug_port",
    }
    selected = {str(key): item for key, item in value.items() if str(key) in allowed}
    return public_mapping(selected)
