from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .errors import BrowserConfigurationError


def browser_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def browser_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:20]}"


def stable_digest(value: Mapping[str, Any] | list[Any] | tuple[Any, ...] | str) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return f"sha256:{hashlib.sha256(encoded.encode('utf-8')).hexdigest()}"


class BrowserSessionStatus(StrEnum):
    CREATED = "created"
    PREPARING = "preparing"
    STARTING = "starting"
    CONNECTING = "connecting"
    RUNNING = "running"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"
    QUARANTINED = "quarantined"


class BrowserTargetStatus(StrEnum):
    DISCOVERED = "discovered"
    ATTACHING = "attaching"
    ATTACHED = "attached"
    DETACHING = "detaching"
    DETACHED = "detached"
    CRASHED = "crashed"


class BrowserConnectionStatus(StrEnum):
    NEW = "new"
    OPENING = "opening"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    LOST = "lost"


class BrowserPermissionEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class BrowserArtifactKind(StrEnum):
    SCREENSHOT = "screenshot"
    DOWNLOAD = "download"
    DOM_STATE = "dom_state"
    TRACE = "trace"
    STORAGE_STATE = "storage_state"
    DIAGNOSTIC = "diagnostic"


@dataclass(frozen=True, slots=True)
class BrowserRuntimeConfig:
    state_root: Path
    runtime_root: Path
    artifact_root: Path
    request_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 10.0
    reconnect_delays: tuple[float, ...] = (1.0, 2.0, 4.0)
    max_reconnect_attempts: int = 3
    disabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "state_root", Path(self.state_root).expanduser().resolve())
        object.__setattr__(self, "runtime_root", Path(self.runtime_root).expanduser().resolve())
        object.__setattr__(self, "artifact_root", Path(self.artifact_root).expanduser().resolve())
        if self.request_timeout_seconds <= 0:
            raise BrowserConfigurationError("request timeout must be positive")
        if self.connect_timeout_seconds <= 0:
            raise BrowserConfigurationError("connect timeout must be positive")
        if self.max_reconnect_attempts < 0:
            raise BrowserConfigurationError("max reconnect attempts cannot be negative")
        if any(delay < 0 for delay in self.reconnect_delays):
            raise BrowserConfigurationError("reconnect delays cannot be negative")

    def prepare_roots(self) -> None:
        for root in (self.state_root, self.runtime_root, self.artifact_root):
            root.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_root": str(self.state_root),
            "runtime_root": str(self.runtime_root),
            "artifact_root": str(self.artifact_root),
            "request_timeout_seconds": self.request_timeout_seconds,
            "connect_timeout_seconds": self.connect_timeout_seconds,
            "reconnect_delays": list(self.reconnect_delays),
            "max_reconnect_attempts": self.max_reconnect_attempts,
            "disabled": self.disabled,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionCommand:
    run_id: str
    task_id: str
    worker_request_id: str
    canonical_session_id: str
    node_id: str = ""
    browser_session_id: str = ""
    workspace_root: Path = Path(".")
    artifact_root: Path = Path(".")
    executable_path: Path | None = None
    endpoint_url: str = ""
    headless: bool = True
    keep_alive: bool = False
    headers: Mapping[str, str] = field(default_factory=dict)
    proxy_url: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.worker_request_id:
            raise BrowserConfigurationError("run, task, and worker request identity are required")
        if not self.canonical_session_id:
            raise BrowserConfigurationError("canonical session identity is required")
        workspace = Path(self.workspace_root).expanduser().resolve()
        artifact = Path(self.artifact_root).expanduser().resolve()
        executable = Path(self.executable_path).expanduser() if self.executable_path else None
        object.__setattr__(self, "workspace_root", workspace)
        object.__setattr__(self, "artifact_root", artifact)
        object.__setattr__(self, "executable_path", executable)
        object.__setattr__(self, "headers", {str(k): str(v) for k, v in self.headers.items()})
        object.__setattr__(self, "constraints", dict(self.constraints))

    @property
    def identity_key(self) -> str:
        return stable_digest({
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_session_id": self.canonical_session_id,
        })

    @property
    def request_fingerprint(self) -> str:
        return stable_digest({
            "identity": self.identity_key,
            "worker_request_id": self.worker_request_id,
            "endpoint_url": self.endpoint_url,
            "headless": self.headless,
            "keep_alive": self.keep_alive,
            "workspace_root": str(self.workspace_root),
            "executable_path": str(self.executable_path or ""),
            "header_names": sorted(name.casefold() for name in self.headers),
        })

    def public_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "canonical_session_id": self.canonical_session_id,
            "node_id": self.node_id,
            "browser_session_id": self.browser_session_id,
            "workspace_root": str(self.workspace_root),
            "artifact_root": str(self.artifact_root),
            "executable_path": str(self.executable_path or ""),
            "endpoint_url": self.endpoint_url,
            "headless": self.headless,
            "keep_alive": self.keep_alive,
            "header_names": sorted(self.headers),
            "proxy_configured": bool(self.proxy_url),
            "request_fingerprint": self.request_fingerprint,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionRef:
    session_id: str
    run_id: str
    task_id: str
    canonical_session_id: str
    worker_request_id: str
    status: str
    revision: int
    profile_id: str
    active_target_id: str = ""
    process_id: int | None = None
    endpoint_url: str = ""
    keep_alive: bool = False
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        if not self.session_id or not self.run_id or not self.task_id:
            raise BrowserConfigurationError("session reference identity is incomplete")
        if self.revision < 0:
            raise BrowserConfigurationError("session revision cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def with_status(self, status: BrowserSessionStatus | str, *, revision: int | None = None) -> "BrowserSessionRef":
        return replace(
            self,
            status=str(status),
            revision=self.revision + 1 if revision is None else revision,
            updated_at=browser_now(),
        )

    def with_target(self, target_id: str) -> "BrowserSessionRef":
        return replace(self, active_target_id=target_id, revision=self.revision + 1, updated_at=browser_now())


@dataclass(frozen=True, slots=True)
class BrowserSessionDiagnostic:
    ok: bool
    session_id: str
    status: str
    process_alive: bool
    cdp_connected: bool
    target_count: int
    active_target_id: str
    event_bus_generation: int
    profile_healthy: bool
    issues: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "status": self.status,
            "process_alive": self.process_alive,
            "cdp_connected": self.cdp_connected,
            "target_count": self.target_count,
            "active_target_id": self.active_target_id,
            "event_bus_generation": self.event_bus_generation,
            "profile_healthy": self.profile_healthy,
            "issues": list(self.issues),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionStartResult:
    ok: bool
    created: bool
    reused: bool
    reconnected: bool
    session: BrowserSessionRef
    diagnostic: BrowserSessionDiagnostic
    events: tuple[Mapping[str, Any], ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "created": self.created,
            "reused": self.reused,
            "reconnected": self.reconnected,
            "session": self.session.to_dict(),
            "diagnostic": self.diagnostic.to_dict(),
            "events": [dict(event) for event in self.events],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BrowserSessionStopResult:
    ok: bool
    already_stopped: bool
    session: BrowserSessionRef
    events: tuple[Mapping[str, Any], ...] = ()
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "already_stopped": self.already_stopped,
            "session": self.session.to_dict(),
            "events": [dict(event) for event in self.events],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class BrowserTargetRef:
    target_id: str
    target_type: str
    url: str
    title: str = ""
    status: str = str(BrowserTargetStatus.DISCOVERED)
    session_ids: tuple[str, ...] = ()
    opener_id: str = ""
    attached_at: str = ""
    updated_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_page(self) -> bool:
        return self.target_type == "page"

    @property
    def attached(self) -> bool:
        return bool(self.session_ids) and self.status == str(BrowserTargetStatus.ATTACHED)


@dataclass(frozen=True, slots=True)
class BrowserCdpSessionRef:
    cdp_session_id: str
    target_id: str
    generation: int
    attached_at: str = field(default_factory=browser_now)
    detached_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserRequestReceipt:
    request_id: int
    method: str
    session_id: str
    generation: int
    started_at: str
    completed_at: str = ""
    ok: bool = False
    error: str = ""
    result_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserProfileRef:
    profile_id: str
    session_id: str
    root: Path
    cache_dir: Path
    downloads_dir: Path
    temp_dir: Path
    state_dir: Path
    healthy: bool = True
    generation: int = 1
    created_at: str = field(default_factory=browser_now)
    updated_at: str = field(default_factory=browser_now)
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "root": str(self.root),
            "cache_dir": str(self.cache_dir),
            "downloads_dir": str(self.downloads_dir),
            "temp_dir": str(self.temp_dir),
            "state_dir": str(self.state_dir),
            "healthy": self.healthy,
            "generation": self.generation,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "issues": list(self.issues),
        }


@dataclass(frozen=True, slots=True)
class BrowserLifecycleEvent:
    topic: str
    session_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    generation: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: browser_id("brevt"))
    cause_event_id: str = ""
    created_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic": self.topic,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "generation": self.generation,
            "payload": dict(self.payload),
            "event_id": self.event_id,
            "cause_event_id": self.cause_event_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserPermissionRequest:
    session_id: str
    action: str
    arguments: Mapping[str, Any]
    run_id: str
    task_id: str
    worker_request_id: str
    target_id: str = ""
    url: str = ""
    request_id: str = field(default_factory=lambda: browser_id("brperm"))

    @property
    def arguments_digest(self) -> str:
        return stable_digest(dict(self.arguments))

    def public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "action": self.action,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "target_id": self.target_id,
            "url": self.url,
            "request_id": self.request_id,
            "arguments_digest": self.arguments_digest,
        }


@dataclass(frozen=True, slots=True)
class BrowserPermissionDecision:
    request_id: str
    effect: BrowserPermissionEffect
    reason: str
    decision_id: str = field(default_factory=lambda: browser_id("brdecision"))
    grant_id: str = ""
    expires_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "effect": str(self.effect),
            "reason": self.reason,
            "decision_id": self.decision_id,
            "grant_id": self.grant_id,
            "expires_at": self.expires_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class BrowserArtifactReceipt:
    artifact_id: str
    session_id: str
    kind: BrowserArtifactKind
    uri: str
    size_bytes: int
    sha256: str
    media_type: str = "application/octet-stream"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=browser_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "session_id": self.session_id,
            "kind": str(self.kind),
            "uri": self.uri,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
        }
