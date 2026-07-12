from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .models import BrowserSessionCommand, BrowserSessionRef, browser_id, browser_now, stable_digest


@dataclass(frozen=True, slots=True)
class BrowserWorkerSessionBinding:
    binding_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    canonical_session_id: str
    browser_session_id: str
    request_fingerprint: str
    attached_at: str
    detached_at: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "canonical_session_id": self.canonical_session_id,
            "browser_session_id": self.browser_session_id,
            "request_fingerprint": self.request_fingerprint,
            "attached_at": self.attached_at,
            "detached_at": self.detached_at,
            "metadata": dict(self.metadata),
        }


class BrowserWorkerSessionBridge:
    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._bindings: dict[str, BrowserWorkerSessionBinding] = {}
        self._request_index: dict[str, str] = {}
        self._lock = threading.RLock()

    def attach(self, command: BrowserSessionCommand, session: BrowserSessionRef) -> BrowserWorkerSessionBinding:
        if self.disabled:
            raise RuntimeError("browser worker session bridge is disabled")
        if session.run_id != command.run_id or session.task_id != command.task_id:
            raise ValueError("browser session identity does not match worker command")
        if session.canonical_session_id != command.canonical_session_id:
            raise ValueError("canonical session identity does not match worker command")
        with self._lock:
            existing_id = self._request_index.get(command.worker_request_id)
            if existing_id:
                existing = self._bindings[existing_id]
                if existing.browser_session_id != session.session_id:
                    raise ValueError("worker request is already bound to another browser session")
                return existing
            binding = BrowserWorkerSessionBinding(
                binding_id=browser_id("brbinding"),
                run_id=command.run_id,
                task_id=command.task_id,
                worker_request_id=command.worker_request_id,
                canonical_session_id=command.canonical_session_id,
                browser_session_id=session.session_id,
                request_fingerprint=command.request_fingerprint,
                attached_at=browser_now(),
                metadata={
                    "node_id": command.node_id,
                    "workspace_root": str(command.workspace_root),
                    "profile_id": session.profile_id,
                },
            )
            self._bindings[binding.binding_id] = binding
            self._request_index[command.worker_request_id] = binding.binding_id
            return binding

    def detach(self, worker_request_id: str) -> BrowserWorkerSessionBinding | None:
        with self._lock:
            binding_id = self._request_index.pop(worker_request_id, "")
            binding = self._bindings.get(binding_id)
            if binding is None:
                return None
            detached = BrowserWorkerSessionBinding(
                **{**binding.to_dict(), "detached_at": browser_now()}
            )
            self._bindings[binding_id] = detached
            return detached

    def binding_for_request(self, worker_request_id: str) -> BrowserWorkerSessionBinding | None:
        with self._lock:
            binding_id = self._request_index.get(worker_request_id, "")
            return self._bindings.get(binding_id)

    def bindings_for_session(self, browser_session_id: str) -> tuple[BrowserWorkerSessionBinding, ...]:
        with self._lock:
            return tuple(item for item in self._bindings.values() if item.browser_session_id == browser_session_id)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            active = sum(1 for item in self._bindings.values() if not item.detached_at)
            return {
                "runtime_id": "zyra-browser-worker-session-bridge",
                "bindings": len(self._bindings),
                "active_bindings": active,
                "checksum": stable_digest([item.to_dict() for item in self._bindings.values()]),
            }
