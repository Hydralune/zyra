from __future__ import annotations

import importlib
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
from urllib.error import HTTPError
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fresh_api() -> Any:
    module_name = "apps.api.zyra_api.main"
    existing = sys.modules.get(module_name)
    if existing is not None:
        existing.reset_mcp_runtime()
        return importlib.reload(existing)
    return importlib.import_module(module_name)


def _post(
    base_url: str,
    path: str,
    payload: dict[str, Any],
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **dict(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read()), dict(response.headers.items())
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())


def _get(
    base_url: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(f"{base_url}{path}", headers=dict(headers or {}), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read()), dict(response.headers.items())
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())


class E02TypeScriptApiCutoverTests(unittest.TestCase):
    def test_task_commands_enter_typescript_and_skill_mutation_reaches_real_owner(self) -> None:
        with tempfile.TemporaryDirectory(prefix="zyra-e02-api-cutover-") as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
            os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
            os.environ["ZYRA_TOOL_WORKSPACE"] = str(workspace)
            os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
            os.environ["ZYRA_MCP_STATE"] = str(root / "e02-state.json")
            os.environ["ZYRA_E02_API_PERMISSION_MODE"] = "default"

            api = _fresh_api()
            state = api.create_task_state(user_goal="Exercise the E02 TypeScript command route.")
            api.get_store().save_checkpoint(state)
            sealed_state = api.create_task_state(
                user_goal="Reject canonical sealed E02 mutations."
            )
            sealed_state.metadata.update(
                {
                    "sealed": True,
                    "sealed_autonomous": True,
                    "competition_mode": "sealed_autonomous",
                }
            )
            api.get_store().save_checkpoint(sealed_state)
            session_id = "e02-cutover-session"
            custody = api.get_permission_api_facade(
                task_id=state.task_id,
                session_id=session_id,
            ).open_session(
                session_id=session_id,
                run_id=state.run_id,
                task_id=state.task_id,
            )
            bearer = custody.body["session"]["bearer_token"]
            auth_headers = {"Authorization": f"Bearer {bearer}"}

            server = ThreadingHTTPServer(("127.0.0.1", 0), api.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                status, command, headers = _post(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    {"text": "/mcp tools", "actor_id": "cutover-test"},
                )
                self.assertEqual(status, 201, command)
                self.assertEqual(headers.get("Cache-Control"), "no-store, max-age=0")
                self.assertEqual(command["command"]["canonical_owner"], "typescript.CommandCoordinator")
                result = command["command_result"]
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["permission"]["effect"], "allow")
                self.assertEqual(result["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(result["python_parser_fallback"])
                self.assertFalse(result["python_dispatch_fallback"])
                self.assertEqual(result["receipt"]["owner"], "typescript-command")
                self.assertEqual(result["receipt"]["commit"]["domain"], "command")

                second_status, second, _ = _post(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    {"text": "/tools", "actor_id": "cutover-test"},
                )
                self.assertEqual(second_status, 201, second)
                self.assertEqual(second["command_result"]["status"], "completed")
                self.assertEqual(second["command_result"]["receipt"]["owner"], "typescript-command")

                sealed_status, sealed, _ = _post(
                    base_url,
                    f"/tasks/{sealed_state.task_id}/commands",
                    {
                        "text": "/mcp disable unavailable-server",
                        "actor_id": "cutover-test",
                    },
                )
                self.assertEqual(sealed_status, 409, sealed)
                self.assertEqual(sealed["error"], "sealed_mcp_mutation_denied")
                sealed_text = json.dumps(sealed, sort_keys=True)
                self.assertIn('"owner_effect_started": false', sealed_text, sealed)
                self.assertIn('"human_intervention_count": 0', sealed_text, sealed)

                skills_status, skills_projection, _ = _get(base_url, "/skills")
                self.assertEqual(skills_status, 200, skills_projection)
                registry = skills_projection["registry"]
                selected_skill = registry["revisions"][-1]["skills"][0]
                skill_command = " ".join(
                    (
                        "/skills update",
                        f'--skill "{selected_skill["name"]}"',
                        f'--expected-hash "sha256:{selected_skill["bodyDigest"]}"',
                        f'--expected-revision {registry["revision"]}',
                        '--dependency-digest "api-cutover-dependency"',
                        '--supply-digest "api-cutover-supply"',
                        '--approval-id "api-cutover-approval"',
                        '--nonce "api-cutover-nonce"',
                        '--idempotency-key "api-cutover-skill-update"',
                    )
                )
                denied_status, denied, denied_headers = _post(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    {"text": skill_command, "actor_id": "cutover-test"},
                )
                self.assertEqual(denied_status, 403, denied)
                self.assertIn("permission", denied["error"])
                self.assertEqual(denied["canonical_entrypoint"], "E02CapabilityCoordinator.execute")
                self.assertFalse(denied["python_parser_fallback"])
                self.assertFalse(denied["python_dispatch_fallback"])
                self.assertEqual(denied_headers.get("Cache-Control"), "no-store, max-age=0")

                query = urlencode(
                    {
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "status": "delivered",
                    }
                )
                pending_status, pending, pending_headers = _get(
                    base_url,
                    f"/permissions/requests?{query}",
                    headers=auth_headers,
                )
                self.assertEqual(pending_status, 200, pending)
                self.assertEqual(pending["state_owner"], "typescript.PermissionCoordinator")
                self.assertFalse(pending["python_decision_fallback"])
                requests = pending["requests"]["items"]
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0]["tool_call_id"], denied["tool_call_id"])
                self.assertFalse(requests[0]["final_arguments_projected"])
                self.assertEqual(pending_headers.get("X-Zyra-Permission-State-Owner"), "typescript.PermissionCoordinator")

                resolved_status, resolved, _ = _post(
                    base_url,
                    f"/permissions/requests/{requests[0]['request_id']}/resolve",
                    {
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "effect": "allow",
                        "idempotency_key": "e02-cutover-approval",
                    },
                    headers=auth_headers,
                )
                self.assertEqual(resolved_status, 200, resolved)
                decision_receipt = resolved["receipt"]
                self.assertEqual(decision_receipt["canonical_owner"], "typescript.PermissionCoordinator")
                self.assertFalse(decision_receipt["python_decision_fallback"])
                self.assertTrue(decision_receipt["permit_id"])

                retry_status, retry, _ = _post(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    {
                        "text": skill_command,
                        "actor_id": "cutover-test",
                        "tool_call_id": denied["tool_call_id"],
                        "permit_id": decision_receipt["permit_id"],
                    },
                )
                self.assertEqual(retry_status, 201, retry)
                self.assertEqual(retry["command_result"]["status"], "completed")
                self.assertEqual(
                    retry["command_result"]["permission"]["reasonCode"],
                    "outer_e02_permit_consumed",
                )
                update = retry["command_result"]["data"]
                self.assertEqual(update["canonical_owner"], "typescript.SkillCoordinator")
                self.assertEqual(update["action"], "update")
                self.assertTrue(update["receipt_id"].startswith("skill-reload-receipt-"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                api.reset_mcp_runtime()


if __name__ == "__main__":
    unittest.main()
