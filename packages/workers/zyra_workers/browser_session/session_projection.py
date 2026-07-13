from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .integration_models import integration_digest, public_mapping
from .models import BrowserSessionRef, browser_now
from .resume_runtime import BrowserResumeCapsulePort, redact_url
from .runtime import BrowserRuntime
from .session_lease import BrowserSessionLeaseStore


PROJECTION_SCHEMA = "zyra.browser-session-projection.v1"


@dataclass(frozen=True, slots=True)
class BrowserTargetProjection:
    target_id: str
    target_type: str
    title: str
    url: str
    status: str
    active: bool
    attached: bool
    cdp_session_count: int
    opener_id: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "target_type": self.target_type,
            "title": self.title,
            "url": redact_url(self.url),
            "status": self.status,
            "active": self.active,
            "attached": self.attached,
            "cdp_session_count": self.cdp_session_count,
            "opener_id": self.opener_id,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserCdpProjection:
    present: bool
    status: str
    generation: int
    transport: str
    pending_requests: int
    completed_requests: int
    timed_out_requests: int
    failed_requests: int
    received_events: int
    late_messages: int
    last_message_at: str
    last_error_type: str = ""

    @property
    def live(self) -> bool:
        return self.present and self.status == "open" and self.transport not in {"", "MemoryCdpTransport"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "present": self.present,
            "live": self.live,
            "status": self.status,
            "generation": self.generation,
            "transport": self.transport,
            "pending_requests": self.pending_requests,
            "completed_requests": self.completed_requests,
            "timed_out_requests": self.timed_out_requests,
            "failed_requests": self.failed_requests,
            "received_events": self.received_events,
            "late_messages": self.late_messages,
            "last_message_at": self.last_message_at,
            "last_error_type": self.last_error_type,
        }


@dataclass(frozen=True, slots=True)
class BrowserProfileProjection:
    profile_id: str
    healthy: bool
    generation: int
    cache_ref: str
    downloads_ref: str
    state_ref: str
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "healthy": self.healthy,
            "generation": self.generation,
            "cache_ref": self.cache_ref,
            "downloads_ref": self.downloads_ref,
            "state_ref": self.state_ref,
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class BrowserArtifactProjection:
    artifact_id: str
    handoff_id: str
    action_request_id: str
    kind: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "handoff_id": self.handoff_id,
            "action_request_id": self.action_request_id,
            "kind": self.kind,
            "uri": self.uri,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserControlProjection:
    control_id: str
    action: str
    status: str
    event_id: str
    cause_event_id: str
    generation: int
    created_at: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_id": self.control_id,
            "action": self.action,
            "status": self.status,
            "event_id": self.event_id,
            "cause_event_id": self.cause_event_id,
            "generation": self.generation,
            "created_at": self.created_at,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class BrowserActionProjection:
    receipt_id: str
    request_id: str
    step_index: int
    action: str
    status: str
    ok: bool
    artifact_ids: tuple[str, ...]
    permission_effect: str
    error_code: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "step_index": self.step_index,
            "action": self.action,
            "status": self.status,
            "ok": self.ok,
            "artifact_ids": list(self.artifact_ids),
            "permission_effect": self.permission_effect,
            "error_code": self.error_code,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionProjection:
    session_id: str
    run_id: str
    task_id: str
    canonical_session_id: str
    worker_request_id: str
    status: str
    revision: int
    endpoint: str
    process_id: int | None
    keep_alive: bool
    active_target_id: str
    profile: BrowserProfileProjection | None
    cdp: BrowserCdpProjection
    targets: tuple[BrowserTargetProjection, ...]
    artifacts: tuple[BrowserArtifactProjection, ...]
    actions: tuple[BrowserActionProjection, ...]
    controls: tuple[BrowserControlProjection, ...]
    logical_lease: Mapping[str, Any]
    resume: Mapping[str, Any]
    event_bus: Mapping[str, Any]
    custody: Mapping[str, str]
    created_at: str
    updated_at: str
    projected_at: str = field(default_factory=browser_now)
    schema: str = PROJECTION_SCHEMA

    @property
    def live(self) -> bool:
        return self.status == "running" and self.cdp.live and bool(self.active_target_id)

    @property
    def resumable(self) -> bool:
        return bool(self.resume.get("capsule_present")) and not bool(self.resume.get("state_lost"))

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_session_id": self.canonical_session_id,
            "worker_request_id": self.worker_request_id,
            "status": self.status,
            "revision": self.revision,
            "endpoint": redact_url(self.endpoint),
            "process_id": self.process_id,
            "keep_alive": self.keep_alive,
            "active_target_id": self.active_target_id,
            "live": self.live,
            "resumable": self.resumable,
            "profile": self.profile.to_dict() if self.profile else None,
            "cdp": self.cdp.to_dict(),
            "targets": [item.to_dict() for item in self.targets],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "actions": [item.to_dict() for item in self.actions],
            "controls": [item.to_dict() for item in self.controls],
            "logical_lease": safe_projection_value(self.logical_lease),
            "resume": safe_projection_value(self.resume),
            "event_bus": safe_projection_value(self.event_bus),
            "custody": dict(self.custody),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "projected_at": self.projected_at,
        }
        value["checksum"] = integration_digest(value)
        return value


@dataclass(frozen=True, slots=True)
class BrowserRuntimeProjection:
    runtime_id: str
    runtime_key: str
    sessions: tuple[BrowserSessionProjection, ...]
    state: Mapping[str, Any]
    registry: Mapping[str, Any]
    custody: Mapping[str, Any]
    generated_at: str = field(default_factory=browser_now)
    schema: str = PROJECTION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        value = {
            "schema": self.schema,
            "runtime_id": self.runtime_id,
            "runtime_key": self.runtime_key,
            "sessions": [item.to_dict() for item in self.sessions],
            "state": safe_projection_value(self.state),
            "registry": safe_projection_value(self.registry),
            "custody": safe_projection_value(self.custody),
            "generated_at": self.generated_at,
        }
        value["checksum"] = integration_digest(value)
        return value


class BrowserSessionProjectionRuntime:
    def __init__(
        self,
        browser_runtime: BrowserRuntime,
        *,
        runtime_key: str,
        registry_snapshot: Mapping[str, Any] | None = None,
        lease_store: BrowserSessionLeaseStore | None = None,
        disabled: bool = False,
    ) -> None:
        self.browser_runtime = browser_runtime
        self.runtime_key = runtime_key
        self.registry_snapshot = dict(registry_snapshot or {})
        self.lease_store = lease_store or BrowserSessionLeaseStore(
            Path(browser_runtime.config.state_root) / "integration"
        )
        self.disabled = disabled
        self.session_runtime = getattr(browser_runtime, "_runtime", None)
        if self.session_runtime is None:
            raise TypeError("BrowserRuntime has no BrowserSessionRuntime")

    def project(self, *, session_id: str = "") -> BrowserRuntimeProjection | BrowserSessionProjection:
        if self.disabled:
            raise RuntimeError("browser session projection runtime is disabled")
        sessions = self.browser_runtime.list_sessions()
        if session_id:
            session = next((item for item in sessions if item.session_id == session_id), None)
            if session is None:
                raise KeyError(session_id)
            return self.project_session(session)
        projected = tuple(self.project_session(item) for item in sessions)
        return BrowserRuntimeProjection(
            runtime_id="zyra-browser-session-projection",
            runtime_key=self.runtime_key,
            sessions=projected,
            state=self._state_snapshot(),
            registry=self.registry_snapshot,
            custody=self._custody_map(),
        )

    def project_session(self, session: BrowserSessionRef) -> BrowserSessionProjection:
        cdp = self._cdp_projection(session.session_id)
        targets = self._target_projections(session.session_id)
        profile = self._profile_projection(session)
        artifacts = self._artifact_projections(session.session_id)
        actions = self._action_projections(session.session_id)
        controls = self._control_projections(session.session_id)
        lease = self.lease_store.get_lease(session.session_id)
        capsule = BrowserResumeCapsulePort(self.browser_runtime.state_store).get(
            session.session_id,
            session_revision=session.revision,
        )
        event_bus = self._event_bus_projection(session.session_id)
        resume = {
            "capsule_present": capsule is not None,
            "capsule_id": capsule.capsule_id if capsule else "",
            "captured_at": capsule.captured_at if capsule else "",
            "can_restart": capsule.can_restart if capsule else False,
            "requires_secret_reentry": capsule.has_unrecoverable_secrets if capsule else False,
            "state_lost": session.status == "failed" and capsule is None,
        }
        custody = {
            "session_state_owner": "JsonBrowserStateStore",
            "runtime_handle_owner": "BrowserRuntimeRegistry",
            "profile_owner": "BrowserProfileStore",
            "action_receipt_owner": "BrowserSessionLeaseStore",
            "permission_owner": "ToolPermissionRuntime/PermissionStateStore",
            "artifact_owner": "LocalArtifactStore+BrowserArtifactHandoff",
            "physical_lease_owner": "M1-07A-deferred",
        }
        return BrowserSessionProjection(
            session_id=session.session_id,
            run_id=session.run_id,
            task_id=session.task_id,
            canonical_session_id=session.canonical_session_id,
            worker_request_id=session.worker_request_id,
            status=session.status,
            revision=session.revision,
            endpoint=redact_url(session.endpoint_url),
            process_id=session.process_id,
            keep_alive=session.keep_alive,
            active_target_id=session.active_target_id,
            profile=profile,
            cdp=cdp,
            targets=targets,
            artifacts=artifacts,
            actions=actions,
            controls=controls,
            logical_lease=lease.to_dict() if lease else {},
            resume=resume,
            event_bus=event_bus,
            custody=custody,
            created_at=session.created_at,
            updated_at=session.updated_at,
        )

    def _cdp_projection(self, session_id: str) -> BrowserCdpProjection:
        runtime = getattr(self.session_runtime, "_cdp", {}).get(session_id)
        if runtime is None:
            return BrowserCdpProjection(False, "missing", 0, "", 0, 0, 0, 0, 0, 0, "")
        snapshot = runtime.snapshot()
        transport = getattr(runtime, "_transport", None)
        error_type = ""
        if snapshot.last_error:
            error_type = str(snapshot.last_error).split(":", 1)[0][:120]
        return BrowserCdpProjection(
            present=True,
            status=str(snapshot.status),
            generation=snapshot.generation,
            transport=type(transport).__name__ if transport is not None else "",
            pending_requests=snapshot.pending_requests,
            completed_requests=snapshot.completed_requests,
            timed_out_requests=snapshot.timed_out_requests,
            failed_requests=snapshot.failed_requests,
            received_events=snapshot.received_events,
            late_messages=snapshot.late_messages,
            last_message_at=snapshot.last_message_at,
            last_error_type=error_type,
        )

    def _target_projections(self, session_id: str) -> tuple[BrowserTargetProjection, ...]:
        runtime = getattr(self.session_runtime, "_targets", {}).get(session_id)
        if runtime is None:
            return ()
        snapshot = runtime.snapshot()
        values = [
            BrowserTargetProjection(
                target_id=item.target_id,
                target_type=item.target_type,
                title=item.title[:500],
                url=redact_url(item.url),
                status=item.status,
                active=item.target_id == snapshot.active_target_id,
                attached=item.attached,
                cdp_session_count=len(item.session_ids),
                opener_id=item.opener_id,
                updated_at=item.updated_at,
            )
            for item in snapshot.targets
        ]
        return tuple(sorted(values, key=lambda item: (not item.active, item.target_type, item.target_id)))

    def _profile_projection(self, session: BrowserSessionRef) -> BrowserProfileProjection | None:
        if not session.profile_id:
            return None
        profile = self.session_runtime.profile_store.get(session.profile_id)
        if profile is None:
            return BrowserProfileProjection(session.profile_id, False, 0, "", "", "", ("profile reference cannot be resolved",))
        health = self.session_runtime.profile_store.health(profile)
        return BrowserProfileProjection(
            profile_id=profile.profile_id,
            healthy=health.healthy,
            generation=profile.generation,
            cache_ref=path_ref(profile.cache_dir, profile.root),
            downloads_ref=path_ref(profile.downloads_dir, profile.root),
            state_ref=path_ref(profile.state_dir, profile.root),
            issues=tuple(health.issues),
        )

    def _artifact_projections(self, session_id: str) -> tuple[BrowserArtifactProjection, ...]:
        values: list[BrowserArtifactProjection] = []
        for item in self.lease_store.list_handoffs(session_id, limit=1000):
            values.append(BrowserArtifactProjection(
                artifact_id=str(item.get("artifact_id") or ""),
                handoff_id=str(item.get("handoff_id") or ""),
                action_request_id=str(item.get("action_request_id") or ""),
                kind=str(item.get("kind") or "file"),
                uri=project_artifact_uri(str(item.get("uri") or "")),
                sha256=str(item.get("sha256") or ""),
                size_bytes=int(item.get("size_bytes") or 0),
                media_type=str(item.get("media_type") or "application/octet-stream"),
                created_at=str(item.get("created_at") or ""),
            ))
        return tuple(values)

    def _action_projections(self, session_id: str) -> tuple[BrowserActionProjection, ...]:
        values: list[BrowserActionProjection] = []
        for receipt in self.lease_store.list_actions(session_id, limit=1000):
            values.append(BrowserActionProjection(
                receipt_id=receipt.receipt_id,
                request_id=receipt.request_id,
                step_index=receipt.step_index,
                action=str(receipt.action),
                status=str(receipt.status),
                ok=receipt.ok,
                artifact_ids=tuple(item.artifact_id for item in receipt.artifact_handoffs),
                permission_effect=str(receipt.permission_metadata.get("permission_effect") or ""),
                error_code=receipt.error_code,
                completed_at=receipt.completed_at,
            ))
        return tuple(values)

    def _control_projections(self, session_id: str) -> tuple[BrowserControlProjection, ...]:
        durable: list[BrowserControlProjection] = []
        for receipt in self.lease_store.list_controls(session_id, limit=1000):
            durable.append(BrowserControlProjection(
                control_id=str(receipt.get("receipt_id") or receipt.get("request_id") or ""),
                action=str(receipt.get("action") or "lifecycle"),
                status=str(receipt.get("status") or "unknown"),
                event_id=str((receipt.get("event_record") or {}).get("event_id") if isinstance(receipt.get("event_record"), Mapping) else ""),
                cause_event_id="",
                generation=int(receipt.get("generation_after") or receipt.get("generation_before") or 0),
                created_at=str(receipt.get("completed_at") or receipt.get("started_at") or ""),
                reason=str(receipt.get("error_code") or receipt.get("error_message") or ""),
            ))
        state_store = self.browser_runtime.state_store
        if not hasattr(state_store, "list_events"):
            return tuple(durable)
        try:
            events = state_store.list_events(session_id, limit=1000)
        except Exception:
            return tuple(durable)
        controls: list[BrowserControlProjection] = []
        control_topics = {
            "browser.session.stopped": ("stop", "completed"),
            "browser.session.reconnecting": ("reconnect", "running"),
            "browser.session.reconnected": ("reconnect", "completed"),
            "browser.session.resume_completed": ("resume", "completed"),
            "browser.session.live_rebound": ("resume", "completed"),
            "browser.session.failed": ("lifecycle", "failed"),
        }
        for event in events:
            topic = str(event.get("topic") or "")
            selected = control_topics.get(topic)
            if selected is None:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            failure = payload.get("failure") if isinstance(payload.get("failure"), Mapping) else {}
            controls.append(BrowserControlProjection(
                control_id=str(event.get("event_id") or integration_digest(event)[:32]),
                action=selected[0],
                status=selected[1],
                event_id=str(event.get("event_id") or ""),
                cause_event_id=str(event.get("cause_event_id") or ""),
                generation=int(event.get("generation") or 0),
                created_at=str(event.get("created_at") or ""),
                reason=str(payload.get("reason") or failure.get("code") or ""),
            ))
        unique = {item.control_id or item.event_id: item for item in (*durable, *controls)}
        return tuple(unique.values())

    def _event_bus_projection(self, session_id: str) -> dict[str, Any]:
        bus = getattr(self.session_runtime, "_event_buses", {}).get(session_id)
        if bus is None:
            return {"present": False, "state": "missing", "generation": 0}
        snapshot = bus.snapshot()
        value = snapshot.to_dict() if hasattr(snapshot, "to_dict") else {
            "state": str(snapshot.state),
            "generation": snapshot.generation,
        }
        return {"present": True, **safe_projection_value(value)}

    def _state_snapshot(self) -> dict[str, Any]:
        store = self.browser_runtime.state_store
        if not hasattr(store, "snapshot"):
            return {}
        snapshot = store.snapshot()
        return snapshot.to_dict() if hasattr(snapshot, "to_dict") else safe_projection_value(snapshot)

    def _custody_map(self) -> dict[str, Any]:
        return {
            "session": {"owner": "JsonBrowserStateStore", "durable": True},
            "process_cdp_target_event_bus": {"owner": "BrowserRuntimeRegistry", "durable": False, "recoverable_via": "BrowserResumeCapsule"},
            "profile_runtime_dirs": {"owner": "BrowserProfileStore", "durable": True},
            "action_receipts_handoffs": {"owner": "BrowserSessionLeaseStore", "durable": True},
            "permission": {"owner": "PermissionStateStore", "durable": True},
            "physical_worker_lease": {"owner": "M1-07A", "status": "deferred"},
        }


def path_ref(path: Path, root: Path) -> str:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return "invalid://outside-profile-root"
    return "profile://" + relative.as_posix()


def project_artifact_uri(value: str) -> str:
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute():
        return "artifact://" + path.name
    return "artifact://" + path.as_posix().lstrip("/")


def safe_projection_value(value: Any, *, key: str = "") -> Any:
    normalized = key.casefold().replace("-", "_")
    if any(token in normalized for token in ("authorization", "cookie", "password", "secret", "token", "api_key", "headers")):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): safe_projection_value(item, key=str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe_projection_value(item, key=key) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        if normalized.endswith("url") or normalized in {"endpoint", "proxy"}:
            return redact_url(value)
        return value[:4000]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return safe_projection_value(value.to_dict(), key=key)
    return str(value)[:1000]
