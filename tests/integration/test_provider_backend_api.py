from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class ProviderBackendApiTests(unittest.TestCase):
    def test_api_reaches_separate_provider_and_backend_state_owners(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")

            from apps.api.zyra_api.main import (
                ZyraRequestHandler,
                reset_browser_runtime,
                reset_control_runtime,
                reset_mcp_runtime,
                reset_subagent_runtime,
                reset_workspace_manager,
            )

            server = ThreadingHTTPServer(("127.0.0.1", 0), ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                provider_health = _get(base_url, "/providers/health")
                self.assertEqual(
                    provider_health["state_owner"],
                    "typescript.ProviderControlPlaneStore",
                )
                self.assertFalse(provider_health["result"]["backendFallbackOwned"])

                _post(
                    base_url,
                    "/providers/integrations",
                    {
                        "integration": {
                            "integrationId": "api-anonymous",
                            "displayName": "API Anonymous",
                            "kind": "anonymous",
                            "envNames": [],
                            "headerName": None,
                            "authorizationScheme": None,
                            "supportsRefresh": False,
                            "metadata": {},
                        }
                    },
                )
                _post(
                    base_url,
                    "/providers",
                    {
                        "provider": {
                            "providerId": "api-loopback",
                            "displayName": "API Loopback",
                            "integrationId": "api-anonymous",
                            "status": "active",
                            "baseUrl": "http://127.0.0.1:9",
                            "protocol": "openai_chat",
                            "defaultHeaders": {},
                            "requestDefaults": {},
                            "allowedHosts": ["127.0.0.1"],
                            "tags": ["api-test"],
                            "metadata": {},
                        }
                    },
                )
                _post(
                    base_url,
                    "/providers/models",
                    {
                        "model": {
                            "providerId": "api-loopback",
                            "modelId": "api-model",
                            "displayName": "API Model",
                            "family": "api",
                            "status": "active",
                            "enabled": True,
                            "releasedAt": 1,
                            "contextWindow": 16_000,
                            "maximumOutputTokens": 2_000,
                            "capabilities": {
                                "input": ["text"],
                                "output": ["text"],
                                "tools": True,
                                "streaming": True,
                                "reasoning": False,
                                "structuredOutput": True,
                            },
                            "pricing": [],
                            "endpointPath": "/v1/chat/completions",
                            "protocol": None,
                            "requestDefaults": {},
                            "tags": [],
                            "metadata": {},
                        }
                    },
                )
                _post(
                    base_url,
                    "/providers/credentials",
                    {
                        "credential": {
                            "credentialId": "api-credential",
                            "integrationId": "api-anonymous",
                            "providerId": "api-loopback",
                            "accountId": "anonymous",
                            "secretRef": "env://ANONYMOUS_UNUSED",
                            "fingerprint": "sha256:0000000000000000",
                            "priority": 0,
                            "allowedModels": [],
                            "scopes": [],
                            "expiresAt": None,
                            "refreshAfter": None,
                            "metadata": {},
                        }
                    },
                )
                route = _post(
                    base_url,
                    "/providers/routes",
                    {
                        "request": {
                            "runId": "api-run",
                            "taskId": "api-task",
                            "nodeId": "api-node",
                            "sessionId": "api-session",
                            "turnId": "api-turn",
                            "purpose": "general",
                            "preferredProviderId": "api-loopback",
                            "preferredModelId": "api-model",
                            "routeHint": "api-loopback/api-model",
                            "constraints": {
                                "providerIds": [],
                                "modelIds": [],
                                "requiredInput": ["text"],
                                "requiredOutput": ["text"],
                                "requireTools": False,
                                "requireStreaming": True,
                                "minimumContextWindow": 0,
                                "maximumInputPricePerMillion": None,
                                "maximumOutputPricePerMillion": None,
                                "excludedCredentialIds": [],
                                "requiredScopes": [],
                            },
                            "metadata": {},
                        }
                    },
                )
                self.assertTrue(route["result"]["routeId"].startswith("provider_route_"))
                self.assertNotIn("apiKey", route["result"])
                self.assertNotIn("secret", route["result"])

                backends = _get(base_url, "/backends/health")
                self.assertEqual(backends["state_owner"], "python.BackendRegistryStore")
                self.assertFalse(backends["result"]["provider_state_owned"])
                self.assertGreaterEqual(backends["result"]["counts"]["backend_definitions"], 4)

                created = _post(
                    base_url,
                    "/tasks",
                    {"goal": "Run backend-dispatch API integration.", "auto_run": True},
                )
                backend_events = [
                    event
                    for event in created["events"]
                    if event.get("payload", {}).get("backend_event_type")
                ]
                self.assertTrue(
                    any(
                        event["payload"]["backend_event_type"]
                        == "backend.dispatch.started"
                        for event in backend_events
                    )
                )
                self.assertTrue(
                    any(
                        event["payload"]["backend_event_type"]
                        == "backend.dispatch.completed"
                        for event in backend_events
                    )
                )
                execute_node = next(
                    item
                    for item in created["task"]["plan_nodes"].values()
                    if item.get("metadata", {}).get("stage") == "execute"
                )
                dispatch = execute_node["metadata"]["backend_dispatch"]
                self.assertEqual(dispatch["state_owner"], "python.BackendRegistryStore")
                self.assertFalse(dispatch["provider_state_owned"])
                self.assertEqual(dispatch["attempts"][0]["outcome"], "succeeded")

                session = dispatch["dispatch_session"]
                run_id = created["task"]["run_id"]
                task_id = created["task"]["task_id"]
                sessions = _get(
                    base_url,
                    f"/backends/dispatch-sessions?run_id={run_id}&task_id={task_id}",
                )
                self.assertTrue(
                    any(
                        item["session_id"] == session["session_id"]
                        and item["phase"] == "succeeded"
                        for item in sessions["result"]
                    )
                )
                replay = _get(
                    base_url,
                    f"/backends/dispatch-sessions/{session['session_id']}/replay",
                )
                self.assertEqual(
                    replay["result"]["route_refs"]["provider_before"],
                    dispatch["provider_route_ref"]["route_id"],
                )
                self.assertEqual(
                    replay["result"]["route_refs"]["provider_after"],
                    dispatch["provider_route_ref"]["route_id"],
                )
                verification = _get(
                    base_url,
                    f"/backends/dispatch-sessions/{session['session_id']}/verify",
                )
                self.assertTrue(verification["result"]["valid"])
                self.assertFalse(verification["result"]["provider_route_changed"])

                control = _post(
                    base_url,
                    "/backends/control",
                    {
                        "action": "cancel",
                        "run_id": run_id,
                        "task_id": task_id,
                        "dispatch_session_id": session["session_id"],
                        "reason": "API control persistence verification",
                        "requested_by": "provider-backend-api-test",
                        "idempotency_key": f"api-control:{session['session_id']}",
                    },
                )
                self.assertFalse(control["result"]["effective"])
                control_requests = _get(
                    base_url,
                    f"/backends/control-requests?run_id={run_id}&task_id={task_id}",
                )
                self.assertTrue(
                    any(
                        item["idempotency_key"] == f"api-control:{session['session_id']}"
                        for item in control_requests["result"]
                    )
                )

                probe = _post(
                    base_url,
                    "/backends/probe",
                    {"runtime_worker": "CodeWorkerRuntime"},
                )
                self.assertGreaterEqual(len(probe["result"]["samples"]), 1)
                self.assertTrue(
                    all(item["backend_id"] for item in probe["result"]["samples"])
                )

                compat = _get(base_url, "/providers/compat-v1")
                self.assertFalse(compat["result"]["writable"])
                self.assertIsNone(compat["result"]["defaultModel"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                reset_browser_runtime()
                reset_control_runtime()
                reset_mcp_runtime()
                reset_subagent_runtime()
                reset_workspace_manager()


HTTP_TIMEOUT_SECONDS = 30


def _get(base_url: str, path: str) -> dict[str, Any]:
    with urllib.request.urlopen(
        f"{base_url}{path}",
        timeout=HTTP_TIMEOUT_SECONDS,
    ) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
