from __future__ import annotations

from typing import Any

from .transport import ProviderControlPlaneProcess


class ProviderControlPlaneStorePort:
    """Read-only process projection; it never opens or mutates canonical SQLite."""

    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def health(self) -> dict[str, Any]:
        return self.process.health()

    def events(self, *, run_id: str = "", task_id: str = "") -> list[dict[str, Any]]:
        payload = {key: value for key, value in {"runId": run_id, "taskId": task_id}.items() if value}
        values = self.process.request("events.list", payload)
        return [dict(item) for item in values or () if isinstance(item, dict)]

    def dispatch_attempts(self, dispatch_id: str) -> list[dict[str, Any]]:
        values = self.process.request("dispatch.attempts", {"dispatchId": dispatch_id})
        return [dict(item) for item in values or () if isinstance(item, dict)]
