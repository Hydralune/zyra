from __future__ import annotations

from typing import Any

from .models import RouteRequest
from .transport import ProviderControlPlaneProcess


class ProviderRoutingPort:
    def __init__(self, process: ProviderControlPlaneProcess) -> None:
        self.process = process

    def acquire(
        self,
        request: RouteRequest,
        *,
        previous_route_id: str | None = None,
    ) -> dict[str, Any]:
        return dict(self.process.request("route.acquire", {
            "request": request.to_wire(),
            "previousRouteId": previous_route_id,
        }) or {})

    def get(self, route_id: str) -> dict[str, Any]:
        return dict(self.process.request("route.get", {"routeId": route_id}) or {})

    def list(self, *, run_id: str = "", task_id: str = "") -> list[dict[str, Any]]:
        payload = {key: value for key, value in {"runId": run_id, "taskId": task_id}.items() if value}
        values = self.process.request("route.list", payload)
        return [dict(item) for item in values or () if isinstance(item, dict)]
