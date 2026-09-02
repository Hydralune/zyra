from __future__ import annotations

import hashlib
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


def _request(
    base_url: str,
    path: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any], dict[str, str]]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={
            **({"Content-Type": "application/json"} if body is not None else {}),
            **dict(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return (
                response.status,
                json.loads(response.read()),
                dict(response.headers.items()),
            )
    except HTTPError as error:
        return error.code, json.loads(error.read()), dict(error.headers.items())


def _console_proof(
    request: dict[str, Any],
    *,
    response_id: str,
    effect: str,
    decision_scope: str = "once",
) -> dict[str, Any]:
    challenge = request["response_challenge"]
    material = {
        "version": challenge["version"],
        "nonce": challenge["nonce"],
        "canonical_owner": challenge["canonical_owner"],
        "envelope_id": request["envelope_id"],
        "request_id": request["request_id"],
        "response_id": response_id,
        "effect": effect,
        **(
            {"decision_scope": decision_scope}
            if challenge["version"] == "zyra.permission-response/v2"
            else {}
        ),
        "run_id": request["run_id"],
        "task_id": request["task_id"],
        "session_id": request["session_id"],
        "session_revision": request["session_revision"],
        "worker_request_id": request["worker_request_id"],
        "tool_call_id": request["tool_call_id"],
        "request_fingerprint": request["request_fingerprint"],
        "arguments_digest": request["arguments_digest"],
        "policy_revision": request["policy_revision"],
        "mode_revision": request["mode_revision"],
        "expires_at": request["expires_at"],
    }
    canonical = json.dumps(
        material,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return {
        **material,
        "proof": hashlib.sha256(canonical).hexdigest(),
    }


class PermissionConsoleApiTests(unittest.TestCase):
    def test_custody_handoff_is_fenced_to_the_exact_previous_binding(self) -> None:
        from zyra_runtime.permission import (
            PermissionSessionCustodyBinding,
            PermissionSessionCustodyScopeMismatch,
            PermissionSessionCustodyStore,
            PermissionStateStore,
        )

        with tempfile.TemporaryDirectory(
            prefix="zyra-permission-handoff-fence-",
        ) as directory:
            root = Path(directory).resolve()
            custody = PermissionSessionCustodyStore(
                PermissionStateStore(root / "permission-state.json")
            )
            first = PermissionSessionCustodyBinding(
                session_id="session_handoff_fence",
                run_id="run_first",
                task_id="task_first",
                workspace_root=str(root),
            )
            second = PermissionSessionCustodyBinding(
                session_id=first.session_id,
                run_id="run_second",
                task_id="task_second",
                workspace_root=str(root),
            )
            opened = custody.claim(first)

            with self.assertRaises(PermissionSessionCustodyScopeMismatch):
                custody.claim(
                    second,
                    presented_token=opened.token,
                    allow_binding_handoff=True,
                    expected_handoff_fingerprint="sha256:" + "0" * 64,
                )

            handed_off = custody.claim(
                second,
                presented_token=opened.token,
                allow_binding_handoff=True,
                expected_handoff_fingerprint=first.fingerprint,
            )
            self.assertEqual(handed_off.binding, second)
            self.assertEqual(handed_off.epoch, opened.epoch + 1)

    def test_terminal_task_hands_permission_custody_to_next_conversation_turn(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zyra-permission-continuation-api-",
        ) as directory:
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
            conversation_id = "session_product_continuation"
            first = api.create_task_state(user_goal="first turn")
            first.metadata["query_session_id"] = conversation_id
            first.status = api.PlanNodeStatus.COMPLETED
            api.get_store().save_checkpoint(first)
            second = api.create_task_state(user_goal="second turn")
            second.metadata["query_session_id"] = conversation_id
            api.get_store().save_checkpoint(second)

            server = ThreadingHTTPServer(("127.0.0.1", 0), api.ZyraRequestHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                first_status, opened, _ = _request(
                    base_url,
                    "/permissions/sessions/open",
                    method="POST",
                    payload={
                        "session_id": conversation_id,
                        "run_id": first.run_id,
                        "task_id": first.task_id,
                        "external_session_exists": False,
                    },
                )
                self.assertEqual(first_status, 201, opened)
                token = opened["session"]["bearer_token"]
                auth = {"Authorization": f"Bearer {token}"}

                next_status, handed_off, _ = _request(
                    base_url,
                    "/permissions/sessions/open",
                    method="POST",
                    payload={
                        "session_id": conversation_id,
                        "run_id": second.run_id,
                        "task_id": second.task_id,
                        "external_session_exists": False,
                    },
                    headers=auth,
                )
                self.assertEqual(next_status, 200, handed_off)
                self.assertTrue(handed_off["session"]["verified"])
                self.assertEqual(
                    handed_off["session"]["task_id"],
                    second.task_id,
                )

                second_query = urlencode(
                    {
                        "session_id": conversation_id,
                        "run_id": second.run_id,
                        "task_id": second.task_id,
                    }
                )
                query_status, visible, _ = _request(
                    base_url,
                    f"/permissions/requests?{second_query}",
                    method="GET",
                    headers=auth,
                )
                self.assertEqual(query_status, 200, visible)

                first_query = urlencode(
                    {
                        "session_id": conversation_id,
                        "run_id": first.run_id,
                        "task_id": first.task_id,
                    }
                )
                stale_status, stale, _ = _request(
                    base_url,
                    f"/permissions/requests?{first_query}",
                    method="GET",
                    headers=auth,
                )
                self.assertEqual(stale_status, 401, stale)
                self.assertEqual(stale["error"], "session_custody_scope_mismatch")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                api.reset_mcp_runtime()
                api.reset_experiment_api(wait=True)
                api.reset_scenario_runner_api(wait=True)

    def test_http_console_proof_is_forwarded_to_typescript_exact_resume(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zyra-permission-console-api-",
        ) as directory:
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
            state = api.create_task_state(
                user_goal="Exercise permission console proof forwarding.",
            )
            api.get_store().save_checkpoint(state)
            session_id = f"permission-console:{state.task_id}"
            custody = api.get_permission_api_facade(
                task_id=state.task_id,
                session_id=session_id,
            ).open_session(
                session_id=session_id,
                run_id=state.run_id,
                task_id=state.task_id,
            )
            bearer = custody.body["session"]["bearer_token"]
            auth = {"Authorization": f"Bearer {bearer}"}
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                api.ZyraRequestHandler,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                command_status, denied, _ = _request(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    method="POST",
                    payload={
                        "text": "/e02-reload",
                        "actor_id": "permission-console-test",
                        "tool_call_id": "permission-console-call-1",
                    },
                )
                self.assertEqual(command_status, 403, denied)
                query = urlencode(
                    {
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "status": "delivered",
                    }
                )
                pending_status, pending, pending_headers = _request(
                    base_url,
                    f"/permissions/requests?{query}",
                    method="GET",
                    headers=auth,
                )
                self.assertEqual(pending_status, 200, pending)
                self.assertEqual(
                    pending_headers["X-Zyra-Permission-State-Owner"],
                    "typescript.PermissionCoordinator",
                )
                requests = pending["requests"]["items"]
                self.assertEqual(len(requests), 1)
                approval = requests[0]
                self.assertNotIn("arguments", approval)
                self.assertFalse(approval["final_arguments_projected"])
                self.assertEqual(
                    approval["response_challenge"]["canonical_owner"],
                    "typescript.PermissionCoordinator",
                )
                response_id = "permission-console-response-1"
                proof = _console_proof(
                    approval,
                    response_id=response_id,
                    effect="allow",
                    decision_scope="session",
                )
                resolved_status, resolved, resolved_headers = _request(
                    base_url,
                    (
                        f"/permissions/requests/"
                        f"{approval['request_id']}/resolve"
                    ),
                    method="POST",
                    payload={
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "effect": "allow",
                        "decision_scope": "session",
                        "response_id": response_id,
                        "idempotency_key": response_id,
                        "console_response": proof,
                        "display_responder": "permission-console-test",
                    },
                    headers=auth,
                )
                self.assertEqual(resolved_status, 200, resolved)
                receipt = resolved["receipt"]
                self.assertTrue(receipt["accepted"])
                self.assertTrue(receipt["response_proof_verified"])
                self.assertEqual(receipt["decision_scope"], "session")
                self.assertTrue(receipt["scope_rule"]["installed"])
                self.assertEqual(receipt["response_proof_digest"], proof["proof"])
                self.assertEqual(
                    receipt["response_challenge_digest"],
                    approval["response_challenge"]["challenge_digest"],
                )
                self.assertEqual(
                    receipt["canonical_owner"],
                    "typescript.PermissionCoordinator",
                )
                self.assertTrue(receipt["permit_id"])
                self.assertEqual(
                    resolved_headers["Cache-Control"],
                    "no-store, max-age=0",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                api.reset_mcp_runtime()
                api.reset_experiment_api(wait=True)
                api.reset_scenario_runner_api(wait=True)

    def test_http_console_rejects_tampered_proof_without_settling_request(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="zyra-permission-console-tamper-",
        ) as directory:
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
            state = api.create_task_state(
                user_goal="Reject a forged permission console proof.",
            )
            api.get_store().save_checkpoint(state)
            session_id = f"permission-console:{state.task_id}"
            custody = api.get_permission_api_facade(
                task_id=state.task_id,
                session_id=session_id,
            ).open_session(
                session_id=session_id,
                run_id=state.run_id,
                task_id=state.task_id,
            )
            bearer = custody.body["session"]["bearer_token"]
            auth = {"Authorization": f"Bearer {bearer}"}
            server = ThreadingHTTPServer(
                ("127.0.0.1", 0),
                api.ZyraRequestHandler,
            )
            thread = threading.Thread(
                target=server.serve_forever,
                daemon=True,
            )
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                _request(
                    base_url,
                    f"/tasks/{state.task_id}/commands",
                    method="POST",
                    payload={
                        "text": "/e02-reload",
                        "actor_id": "permission-console-test",
                        "tool_call_id": "permission-console-call-forged",
                    },
                )
                query = urlencode(
                    {
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "status": "delivered",
                    }
                )
                _, pending, _ = _request(
                    base_url,
                    f"/permissions/requests?{query}",
                    method="GET",
                    headers=auth,
                )
                approval = pending["requests"]["items"][0]
                response_id = "permission-console-response-forged"
                proof = _console_proof(
                    approval,
                    response_id=response_id,
                    effect="deny",
                )
                proof["mode_revision"] += 1
                material = dict(proof)
                material.pop("proof")
                proof["proof"] = hashlib.sha256(
                    json.dumps(
                        material,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                status, rejected, _ = _request(
                    base_url,
                    (
                        f"/permissions/requests/"
                        f"{approval['request_id']}/resolve"
                    ),
                    method="POST",
                    payload={
                        "session_id": session_id,
                        "run_id": state.run_id,
                        "task_id": state.task_id,
                        "effect": "deny",
                        "response_id": response_id,
                        "idempotency_key": response_id,
                        "console_response": proof,
                        "display_responder": "permission-console-test",
                    },
                    headers=auth,
                )
                self.assertEqual(status, 403, rejected)
                self.assertEqual(
                    rejected["error"],
                    "permission_response_mode_revision_mismatch",
                )
                _, after, _ = _request(
                    base_url,
                    f"/permissions/requests?{query}",
                    method="GET",
                    headers=auth,
                )
                visible = after["requests"]["items"][0]
                self.assertEqual(visible["status"], "delivered")
                self.assertFalse(visible["response_accepted"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
                api.reset_mcp_runtime()
                api.reset_experiment_api(wait=True)
                api.reset_scenario_runner_api(wait=True)


if __name__ == "__main__":
    unittest.main()
