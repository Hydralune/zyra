from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for package_path in [ROOT / "packages" / "core", ROOT / "packages" / "runtime"]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_runtime.provider_control_plane import (
    ProviderRouteBindingError,
    ProviderRouteBindingRuntime,
)


def _route(route_id: str, previous_route_id: str | None = None) -> dict[str, Any]:
    return {
        "routeId": route_id,
        "checksum": f"sha256:{route_id}",
        "catalogRevision": 1,
        "credentialVersion": 1,
        "credentialFingerprint": "sha256:credential",
        "transportId": "openai_chat",
        "runId": "run-1",
        "taskId": "task-1",
        "sessionId": "session-1",
        "turnId": "turn-1",
        "expiresAt": 1,
        "providerId": "provider-1",
        "modelId": "model-1",
        "credentialId": "credential-1",
        "previousRouteId": previous_route_id,
    }


def _client(routes: list[dict[str, Any]]) -> Any:
    return SimpleNamespace(
        routing=SimpleNamespace(list=lambda **_kwargs: list(routes)),
    )


class ProviderRouteBindingRenewalTests(unittest.TestCase):
    def test_selects_leaf_of_linear_expiry_renewal_chain(self) -> None:
        selected = ProviderRouteBindingRuntime._existing_turn_route(
            _client([
                _route("route-1"),
                _route("route-2", "route-1"),
                _route("route-3", "route-2"),
            ]),
            run_id="run-1",
            task_id="task-1",
            session_id="session-1",
            turn_id="turn-1",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.route_id, "route-3")
        self.assertEqual(selected.previous_route_id, "route-2")

    def test_fails_closed_for_forked_renewal_chain(self) -> None:
        with self.assertRaises(ProviderRouteBindingError) as raised:
            ProviderRouteBindingRuntime._existing_turn_route(
                _client([
                    _route("route-1"),
                    _route("route-2", "route-1"),
                    _route("route-3", "route-1"),
                ]),
                run_id="run-1",
                task_id="task-1",
                session_id="session-1",
                turn_id="turn-1",
            )

        self.assertEqual(raised.exception.code, "provider_turn_route_conflict")
        self.assertEqual(
            raised.exception.detail["route_ids"],
            ["route-1", "route-2", "route-3"],
        )


if __name__ == "__main__":
    unittest.main()
