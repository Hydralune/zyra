from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[2]
for package_path in [
    ROOT / "packages" / "core",
    ROOT / "packages" / "runtime",
    ROOT / "packages" / "integrations",
    ROOT / "packages" / "workers",
]:
    if str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))


from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_runtime.permission.action_gate import (  # noqa: E402
    BrowserActionPermissionGate,
    BrowserActionPermissionInput,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionRequestRecord,
    PermissionResolutionResponse,
)
from zyra_runtime.permission.request_queue import PermissionRequestQueue  # noqa: E402
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_workers.browser_worker import BrowserWorkerRuntime  # noqa: E402


class _CountingBrowserWorkerRuntime(BrowserWorkerRuntime):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.load_url_count = 0

    def _load_url(self, url: str, request: WorkerRequest) -> str:
        self.load_url_count += 1
        return "<html><head><title>Permission Fixture</title></head><body>permission evidence</body></html>"


def _browser_events(run: object) -> list[object]:
    return [
        event
        for event in run.event_records
        if isinstance(event.payload.get("browser_result"), dict)
    ]


def _permission_kinds(run: object) -> list[str]:
    return [
        str(event.payload.get("query_session", {}).get("permission_runtime", {}).get("kind") or "")
        for event in run.event_records
        if event.payload.get("query_session", {}).get("permission_runtime")
    ]


def _pending_record(run: object) -> PermissionRequestRecord:
    for event in _browser_events(run):
        pending = event.payload["browser_result"]["output"].get("pending_request")
        if isinstance(pending, dict):
            return PermissionRequestRecord.from_dict(pending)
    raise AssertionError("browser run did not expose a pending permission request")


def _approve_exact(state_path: Path, record: PermissionRequestRecord) -> None:
    outcome = PermissionRequestQueue(
        PermissionStateStore(state_path),
        record.session_id,
    ).resolve(
        PermissionResolutionResponse(
            request_id=record.request_id,
            session_id=record.session_id,
            tool_use_id=record.tool_use_id,
            tool_identity=record.tool_identity,
            arguments_digest=record.arguments_digest,
            request_fingerprint=record.request_fingerprint,
            scope=record.scope,
            effect=PermissionEffect.ALLOW,
            actor_id="browser-permission-test-authority",
            expected_revision=record.revision,
            channel="test",
            reason="approve exact BrowserWorker retry",
            idempotency_key=f"browser-test-approval:{record.request_id}",
        )
    )
    if not outcome.accepted:
        raise AssertionError(f"permission resolution failed: {outcome.code}: {outcome.reason}")


class BrowserWorkerPermissionGateTests(unittest.TestCase):
    def _runtime(
        self,
        root: Path,
        *,
        permission_state_path: Path | None = None,
    ) -> _CountingBrowserWorkerRuntime:
        workspace = root / "workspace"
        workspace.mkdir(exist_ok=True)
        return _CountingBrowserWorkerRuntime(
            project_root=ROOT,
            workspace_root=workspace,
            artifact_root=root / "artifacts",
            permission_state_path=permission_state_path,
        )

    @staticmethod
    def _request(
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        url: str,
        constraints: dict[str, Any] | None = None,
    ) -> WorkerRequest:
        return WorkerRequest(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            worker_name="BrowserWorker",
            constraints={
                "browser_plan": [{"action": "open_url", "arguments": {"url": url}}],
                **(constraints or {}),
            },
        )

    def test_local_file_read_is_allowed_in_sealed_mode_and_consumes_one_grant(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            page = runtime.workspace_root / "local.html"
            page.write_text("<html><body>local</body></html>", encoding="utf-8")
            state = create_task_state("Read a local browser file under sealed policy.")

            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=page.resolve().as_uri(),
                    constraints={"permission_mode": "sealed", "allowed_schemes": ["file"]},
                )
            )

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 1)
            self.assertEqual(run.worker_result.metadata["browser_permission_action_execution_count"], "1")
            browser_event = _browser_events(run)[0]
            self.assertEqual(
                browser_event.payload["browser_action"]["permission_execution_grant_consumed"],
                "true",
            )
            self.assertIn("permission_execution_grant_consumed", _permission_kinds(run))

    def test_network_ask_and_sealed_deny_both_stop_before_browser_callback(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Attempt a network browser action.")
            base = dict(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url="https://example.test/permission",
            )

            ask_run = runtime.run(self._request(**base))
            self.assertFalse(ask_run.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(ask_run.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(_browser_events(ask_run)[0].payload["browser_result"]["error"], "permission_required")
            self.assertIn("browser_action_permission_blocked", _permission_kinds(ask_run))
            self.assertIn("recovery_input", _permission_kinds(ask_run))

            sealed_root = root / "sealed"
            sealed_root.mkdir()
            sealed_runtime = self._runtime(sealed_root)
            sealed_run = sealed_runtime.run(
                self._request(**base, constraints={"permission_mode": "sealed"})
            )
            self.assertFalse(sealed_run.worker_result.ok)
            self.assertEqual(sealed_runtime.load_url_count, 0)
            self.assertEqual(sealed_run.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(_browser_events(sealed_run)[0].payload["browser_result"]["error"], "permission_denied")
            self.assertIn("recovery_input", _permission_kinds(sealed_run))
            self.assertTrue(
                all(
                    event.payload.get("query_session", {})
                    .get("permission_runtime", {})
                    .get("payload", {})
                    .get("human_intervention_count", 0)
                    == 0
                    for event in sealed_run.event_records
                )
            )

    def test_live_network_ask_does_not_start_browser_process_before_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            if not runtime.browser_use_health.importable:
                self.skipTest("browser-use runtime is unavailable")
            state = create_task_state("Block live browser startup before network approval.")
            request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url="https://example.test/live-permission",
                constraints={"browser_backend": "browser-use-live"},
            )

            with (
                patch(
                    "zyra_workers.browser_worker.find_browser_executable",
                    return_value=Path(sys.executable),
                ),
                patch(
                    "browser_use.browser.session.BrowserSession.start",
                    new_callable=AsyncMock,
                ) as start,
            ):
                run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(start.await_count, 0)
            self.assertEqual(run.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(_browser_events(run)[0].payload["browser_result"]["error"], "permission_required")

    def test_exact_approved_retry_runs_once_while_mismatch_and_replay_stay_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Approve one exact browser action.")
            original_url = "https://example.test/exact"
            first = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=original_url,
                )
            )
            self.assertEqual(runtime.load_url_count, 0)
            pending = _pending_record(first)
            token = first.permission_session_custody_token
            self.assertTrue(token)
            _approve_exact(runtime.permission_state_path, pending)
            authority = {
                "permission_session_id": pending.session_id,
                "permission_session_custody_token": token,
            }

            mismatched = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/different",
                    constraints=authority,
                )
            )
            self.assertFalse(mismatched.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(_browser_events(mismatched)[0].payload["browser_result"]["error"], "permission_required")

            approved = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=original_url,
                    constraints=authority,
                )
            )
            self.assertTrue(approved.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 1)
            self.assertEqual(approved.worker_result.metadata["browser_permission_action_execution_count"], "1")

            replay = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=original_url,
                    constraints=authority,
                )
            )
            self.assertFalse(replay.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 1)
            self.assertEqual(replay.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(_browser_events(replay)[0].payload["browser_result"]["error"], "permission_required")

    def test_static_click_without_prior_page_state_is_blocked_before_any_load(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            page = runtime.workspace_root / "click-without-state.html"
            page.write_text(
                '<html><body><a href="https://remote.example/side-effect">remote</a></body></html>',
                encoding="utf-8",
            )
            state = create_task_state("A click target must be resolved before permission.")
            request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url=page.resolve().as_uri(),
                constraints={
                    "permission_mode": "sealed",
                    "allowed_schemes": ["file", "https"],
                    "browser_plan": [
                        {
                            "action": "click_element",
                            "arguments": {"url": page.resolve().as_uri(), "index": 0},
                        }
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(
                _browser_events(run)[0].payload["browser_result"]["error"],
                "browser_click_state_required",
            )
            self.assertEqual(
                run.worker_result.metadata["browser_permission_action_execution_count"],
                "0",
            )

    def test_static_click_approval_binds_resolved_target_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            page = runtime.workspace_root / "target-drift.html"
            approved_url = "https://approved.example/original"
            evil_url = "https://evil.example/changed-after-approval"
            page.write_text(
                f'<html><body><a href="{approved_url}">target</a></body></html>',
                encoding="utf-8",
            )
            remote_urls: list[str] = []

            def load(url: str, request: WorkerRequest) -> str:
                runtime.load_url_count += 1
                if url.startswith("file:"):
                    return page.read_text(encoding="utf-8")
                remote_urls.append(url)
                return "<html><body>remote</body></html>"

            state = create_task_state("A browser approval cannot drift to another href.")
            plan = [
                {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                {"action": "click_element", "arguments": {"index": 0}},
            ]
            first_request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url=page.resolve().as_uri(),
                constraints={
                    "browser_plan": plan,
                    "allowed_schemes": ["file", "https"],
                },
            )
            with patch.object(runtime, "_load_url", side_effect=load):
                first = runtime.run(first_request)
            pending = _pending_record(first)
            token = first.permission_session_custody_token
            _approve_exact(runtime.permission_state_path, pending)
            self.assertEqual(remote_urls, [])

            page.write_text(
                f'<html><body><a href="{evil_url}">target</a></body></html>',
                encoding="utf-8",
            )
            with patch.object(runtime, "_load_url", side_effect=load):
                drifted = runtime.run(
                    self._request(
                        run_id=state.run_id,
                        task_id=state.task_id,
                        node_id=state.root_node_id,
                        url=page.resolve().as_uri(),
                        constraints={
                            "browser_plan": plan,
                            "allowed_schemes": ["file", "https"],
                            "permission_session_id": pending.session_id,
                            "permission_session_custody_token": token,
                        },
                    )
                )

            self.assertFalse(drifted.worker_result.ok)
            self.assertEqual(remote_urls, [])
            click_event = _browser_events(drifted)[-1]
            self.assertEqual(
                click_event.payload["browser_result"]["error"],
                "permission_required",
            )

    def test_custody_capability_in_action_or_nested_metadata_is_never_executed_or_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("A custody bearer is authentication data only.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/get-custody",
                )
            )
            token = initial.permission_session_custody_token
            session_id = initial.worker_result.metadata["permission_runtime_session_id"]
            page = runtime.workspace_root / "capability-echo.html"
            page.write_text("<html><body>local</body></html>", encoding="utf-8")
            request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url=page.resolve().as_uri(),
                constraints={
                    "permission_session_id": session_id,
                    "permission_session_custody_token": token,
                    "operator_note": token,
                    "nested": {"note": token, token: "secret-key"},
                    "allowed_schemes": ["file"],
                    "browser_plan": [
                        {"action": "open_url", "arguments": {"url": page.resolve().as_uri()}},
                        {
                            "action": "input_text",
                            "arguments": {"index": 0, "text": token},
                        },
                    ],
                },
            )

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(run.worker_result.error, "permission_custody_capability_echo")
            projected = json.dumps(
                {
                    "result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(event) for event in run.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_agent_task_custody_echo_is_blocked_before_backend_or_agent_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Never treat a custody capability as a browser agent task.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/agent-custody",
                )
            )
            token = initial.permission_session_custody_token
            session_id = initial.worker_result.metadata["permission_runtime_session_id"]
            load_count_before_echo = runtime.load_url_count
            permission_state_before_echo = runtime.permission_state_path.read_bytes()

            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/must-not-run",
                    constraints={
                        "permission_session_id": session_id,
                        "permission_session_custody_token": token,
                        "browser_backend": "browser-use-agent",
                        "agent_task": token,
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_custody_capability_echo")
            self.assertEqual(runtime.load_url_count, load_count_before_echo)
            self.assertEqual(runtime.permission_state_path.read_bytes(), permission_state_before_echo)
            self.assertEqual(run.worker_result.metadata["browser_backend"], "blocked_before_selection")
            self.assertFalse(any("browser_agent_result" in event.payload for event in run.event_records))
            projected = json.dumps(
                {
                    "run": repr(run),
                    "result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(event) for event in run.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_agent_task_custody_echo_at_mapping_item_513_fails_closed_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("A large constraint map cannot hide an agent task capability echo.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/large-agent-custody",
                )
            )
            token = initial.permission_session_custody_token
            constraints: dict[str, Any] = {
                "permission_session_id": initial.worker_result.metadata[
                    "permission_runtime_session_id"
                ],
                "permission_session_custody_token": token,
                "browser_backend": "browser-use-agent",
            }
            constraints.update(
                {f"safe_padding_{index:04d}": "safe" for index in range(508)}
            )
            constraints["agent_task"] = token
            request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url="https://example.test/must-not-run",
                constraints=constraints,
            )
            self.assertEqual(list(request.constraints).index("agent_task") + 1, 513)
            permission_state_before_echo = runtime.permission_state_path.read_bytes()

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_custody_capability_echo")
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(runtime.permission_state_path.read_bytes(), permission_state_before_echo)
            self.assertFalse(any("browser_agent_result" in event.payload for event in run.event_records))
            projected = json.dumps(
                {
                    "run": repr(run),
                    "result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(event) for event in run.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            self.assertNotIn(token.lower(), projected.lower())
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_browser_backend_custody_echo_at_mapping_item_513_fails_closed_without_leak(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("A large constraint map cannot hide a backend capability echo.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/large-backend-custody",
                )
            )
            token = initial.permission_session_custody_token
            constraints: dict[str, Any] = {
                "permission_session_id": initial.worker_result.metadata[
                    "permission_runtime_session_id"
                ],
                "permission_session_custody_token": token,
            }
            constraints.update(
                {f"safe_padding_{index:04d}": "safe" for index in range(509)}
            )
            constraints["browser_backend"] = token
            request = self._request(
                run_id=state.run_id,
                task_id=state.task_id,
                node_id=state.root_node_id,
                url="https://example.test/must-not-run",
                constraints=constraints,
            )
            self.assertEqual(list(request.constraints).index("browser_backend") + 1, 513)
            permission_state_before_echo = runtime.permission_state_path.read_bytes()

            run = runtime.run(request)

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_custody_capability_echo")
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(runtime.permission_state_path.read_bytes(), permission_state_before_echo)
            self.assertEqual(run.worker_result.metadata["browser_backend"], "blocked_before_selection")
            projected = json.dumps(
                {
                    "run": repr(run),
                    "result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(event) for event in run.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            self.assertNotIn(token.lower(), projected.lower())
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_static_permission_mode_custody_echo_is_blocked_before_mode_or_action_processing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Never treat a custody capability as a permission mode.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/mode-custody",
                )
            )
            token = initial.permission_session_custody_token
            session_id = initial.worker_result.metadata["permission_runtime_session_id"]
            page = runtime.workspace_root / "mode-capability-echo.html"
            page.write_text("<html><body>must not load</body></html>", encoding="utf-8")
            load_count_before_echo = runtime.load_url_count
            permission_state_before_echo = runtime.permission_state_path.read_bytes()

            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=page.resolve().as_uri(),
                    constraints={
                        "permission_session_id": session_id,
                        "permission_session_custody_token": token,
                        "permission_mode": token,
                        "allowed_schemes": ["file"],
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(run.worker_result.error, "permission_custody_capability_echo")
            self.assertEqual(runtime.load_url_count, load_count_before_echo)
            self.assertEqual(runtime.permission_state_path.read_bytes(), permission_state_before_echo)
            self.assertEqual(
                run.worker_result.metadata["browser_permission_action_execution_count"],
                "0",
            )
            projected = json.dumps(
                {
                    "run": repr(run),
                    "result": to_jsonable(run.worker_result),
                    "events": [to_jsonable(event) for event in run.event_records],
                },
                default=str,
                sort_keys=True,
            )
            self.assertNotIn(token, projected)
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_custody_mismatch_and_disabled_gate_emit_recovery_with_zero_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Reject invalid browser permission authority.")
            initial = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/custody",
                )
            )
            session_id = initial.worker_result.metadata["permission_runtime_session_id"]

            wrong_token = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/custody",
                    constraints={
                        "permission_session_id": session_id,
                        "permission_session_custody_token": "not-the-session-token",
                    },
                )
            )
            self.assertFalse(wrong_token.worker_result.ok)
            self.assertEqual(wrong_token.worker_result.error, "browser_action_permission_unavailable")
            self.assertEqual(wrong_token.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(runtime.load_url_count, 0)
            self.assertIn("browser_action_permission_unavailable", _permission_kinds(wrong_token))
            self.assertIn("recovery_input", _permission_kinds(wrong_token))

            disabled_root = root / "disabled"
            disabled_root.mkdir()
            disabled_runtime = self._runtime(disabled_root)
            disabled = disabled_runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/disabled",
                    constraints={"disable_browser_action_permission_gate": True},
                )
            )
            self.assertFalse(disabled.worker_result.ok)
            self.assertEqual(disabled.worker_result.error, "browser_action_permission_unavailable")
            self.assertEqual(disabled.worker_result.metadata["browser_permission_action_execution_count"], "0")
            self.assertEqual(disabled_runtime.load_url_count, 0)
            self.assertIn("recovery_input", _permission_kinds(disabled))

    def test_caller_bypass_and_auto_flags_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Ignore caller supplied browser bypass flags.")
            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/no-bypass",
                    constraints={
                        "permission_mode": "bypass",
                        "permission_bypass_available": True,
                        "permission_auto_available": True,
                    },
                )
            )

            self.assertFalse(run.worker_result.ok)
            self.assertEqual(runtime.load_url_count, 0)
            self.assertEqual(_browser_events(run)[0].payload["browser_result"]["error"], "permission_required")
            self.assertEqual(run.worker_result.metadata["browser_permission_effective_mode"], "default")
            self.assertEqual(run.worker_result.metadata["browser_permission_caller_bypass_ignored"], "true")
            self.assertEqual(run.worker_result.metadata["browser_permission_caller_auto_ignored"], "true")

    def test_persisted_sealed_mode_overrides_request_default_and_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Persist a sticky sealed browser permission mode.")
            first = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/persisted-sealed",
                    constraints={"permission_session_id": "persisted-browser-session"},
                )
            )
            token = first.permission_session_custody_token
            self.assertTrue(token)

            def persist_sealed(owner_state: dict[str, Any]) -> None:
                integration = owner_state.setdefault("metadata", {}).setdefault(
                    "permission_integration",
                    {
                        "schema": "zyra.permission-integration-state.v1",
                        "owner_unit": "M1-S03A-02",
                        "retry_descriptors": {},
                        "session_modes": {},
                        "event_links": {},
                        "metadata": {"legacy_store_is_authority": False},
                    },
                )
                integration.setdefault("session_modes", {})["persisted-browser-session"] = {
                    "session_id": "persisted-browser-session",
                    "mode": "sealed",
                    "revision": 1,
                }

            PermissionStateStore(runtime.permission_state_path).mutate(persist_sealed)
            authority = {
                "permission_session_id": "persisted-browser-session",
                "permission_session_custody_token": token,
            }
            request_modes = (
                {**authority, "permission_mode": "default"},
                {
                    **authority,
                    "permission_mode": "bypass",
                    "permission_bypass_available": True,
                    "permission_auto_available": True,
                },
            )

            for constraints in request_modes:
                with self.subTest(requested=constraints["permission_mode"]):
                    run = runtime.run(
                        self._request(
                            run_id=state.run_id,
                            task_id=state.task_id,
                            node_id=state.root_node_id,
                            url="https://example.test/persisted-sealed",
                            constraints=constraints,
                        )
                    )
                    self.assertFalse(run.worker_result.ok)
                    self.assertEqual(_browser_events(run)[0].payload["browser_result"]["error"], "permission_denied")
                    self.assertEqual(run.worker_result.metadata["browser_permission_persisted_mode"], "sealed")
                    self.assertEqual(run.worker_result.metadata["browser_permission_effective_mode"], "sealed")
                    self.assertEqual(run.worker_result.metadata["browser_permission_mode_source"], "state_owner")
                    self.assertEqual(run.worker_result.metadata["browser_permission_action_execution_count"], "0")
                    self.assertIn("recovery_input", _permission_kinds(run))
            self.assertEqual(runtime.load_url_count, 0)

    def test_first_custody_token_is_repr_hidden_and_never_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            state = create_task_state("Keep the browser custody capability out of telemetry.")
            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url="https://example.test/token",
                )
            )
            token = run.permission_session_custody_token

            self.assertTrue(token)
            self.assertNotIn(token, repr(run))
            self.assertNotIn(token, json.dumps(to_jsonable(run.worker_result), default=str))
            self.assertNotIn(token, json.dumps([to_jsonable(event) for event in run.event_records], default=str))
            for path in (root / "artifacts").rglob("*"):
                if path.is_file():
                    self.assertNotIn(token.encode("utf-8"), path.read_bytes(), str(path))

    def test_grant_identity_mismatch_and_replay_are_rejected_at_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            runtime = self._runtime(root)
            page = runtime.workspace_root / "exact.html"
            page.write_text("exact", encoding="utf-8")
            request = WorkerRequest(
                run_id="run-grant",
                task_id="task-grant",
                node_id="node-grant",
                worker_name="BrowserWorker",
            )
            gate = BrowserActionPermissionGate.for_worker_request(
                request,
                workspace_root=runtime.workspace_root,
                state_path=runtime.permission_state_path,
            )
            original = BrowserActionPermissionInput(
                step_index=1,
                action="open_url",
                normalized_action="open_url",
                backend="static",
                arguments={"url": page.resolve().as_uri()},
                target_url=page.resolve().as_uri(),
            )
            decision = gate.guard(original)
            self.assertTrue(decision.allowed)
            mismatch = BrowserActionPermissionInput(
                step_index=1,
                action="extract_text",
                normalized_action="extract_text",
                backend="static",
                arguments={},
                current_url=page.resolve().as_uri(),
                target_url=page.resolve().as_uri(),
            )

            rejected = gate.consume(decision, mismatch)
            self.assertFalse(rejected.accepted)
            self.assertIn(
                "browser_action_execution_grant_rejected",
                [
                    event.payload.get("query_session", {}).get("permission_runtime", {}).get("kind")
                    for event in rejected.events
                ],
            )
            self.assertIn(
                "recovery_input",
                [
                    event.payload.get("query_session", {}).get("permission_runtime", {}).get("kind")
                    for event in rejected.events
                ],
            )
            accepted = gate.consume(decision, original)
            self.assertTrue(accepted.accepted)
            replay = gate.consume(decision, original)
            self.assertFalse(replay.accepted)

    def test_permission_state_path_is_deployment_owned_and_request_override_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            deployment_state = root / "deployment" / "permission-state.json"
            runtime = self._runtime(root, permission_state_path=deployment_state)
            page = runtime.workspace_root / "local.html"
            page.write_text("local", encoding="utf-8")
            state = create_task_state("Use the deployment permission state owner.")
            attacker_path = root / "request-owned-state.json"

            run = runtime.run(
                self._request(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    url=page.resolve().as_uri(),
                    constraints={
                        "permission_state_path": str(attacker_path),
                        "allowed_schemes": ["file"],
                    },
                )
            )

            self.assertTrue(run.worker_result.ok)
            self.assertEqual(runtime.permission_state_path, deployment_state.resolve())
            self.assertTrue(deployment_state.exists())
            self.assertFalse(attacker_path.exists())


if __name__ == "__main__":
    unittest.main()
