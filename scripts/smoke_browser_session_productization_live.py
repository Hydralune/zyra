from __future__ import annotations

import json
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for package_path in sorted((ROOT / "packages").iterdir()):
    if package_path.is_dir() and str(package_path) not in sys.path:
        sys.path.insert(0, str(package_path))

from zyra_core import create_task_state, to_jsonable  # noqa: E402
from zyra_integrations.browser_use import ChromeProcessController  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_runtime.permission.custody import PermissionSessionCustodyBinding, PermissionSessionCustodyStore  # noqa: E402
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_workers import BrowserRuntimeConfig, BrowserRuntimeRegistry, BrowserWorkerRuntime, find_browser_executable  # noqa: E402
from zyra_workers.browser_action import default_browser_action_registry as default_productized_action_registry  # noqa: E402


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    executable = find_browser_executable()
    if executable is None:
        print(json.dumps({
            "ok": False,
            "blocking": True,
            "error": "browser_executable_not_found",
            "message": "The mandatory productized local-Chrome lane cannot be skipped.",
        }, sort_keys=True))
        return 2
    with tempfile.TemporaryDirectory(
        prefix="zyra-browser-productized-live-",
        ignore_cleanup_errors=True,
    ) as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        page = workspace / "live.html"
        page.write_text("<html><head><title>Zyra Productized Live</title></head><body>live lane</body></html>", encoding="utf-8")
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(workspace)))
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        page_url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
        registry = BrowserRuntimeRegistry()
        runtime_config = BrowserRuntimeConfig(
            root / "artifacts" / ".browser-session" / "state",
            root / "tmp" / "browser-session-runtime",
            root / "artifacts",
            request_timeout_seconds=30.0,
            connect_timeout_seconds=30.0,
        )
        worker = BrowserWorkerRuntime(
            project_root=root,
            workspace_root=workspace,
            artifact_root=root / "artifacts",
            permission_state_path=root / "permission-state.json",
            browser_runtime_registry=registry,
        )
        runtime = registry.get_or_create(runtime_config)
        state = create_task_state("Run the mandatory productized local Chrome lane.")
        permission_session_id = "productized-live"
        permission_store = PermissionStateStore(worker.permission_state_path)
        permission_custody = PermissionSessionCustodyStore(permission_store).claim(
            PermissionSessionCustodyBinding(
                session_id=permission_session_id,
                run_id=state.run_id,
                task_id=state.task_id,
                workspace_root=str(worker.workspace_root),
            )
        )
        productized_actions = set(default_productized_action_registry().names())
        productized_actions.update(str(action["action"]) for action in worker.action_registry.describe()["actions"])
        for action_name in sorted(productized_actions):
            permission_store.add_session_rule(
                permission_session_id,
                PermissionRuleRecord(
                    effect=PermissionEffect.ALLOW,
                    source=PermissionRuleSource.SESSION,
                    scope=PermissionScope(PermissionScopeKind.SESSION, session_id=permission_session_id),
                    namespace_pattern="browser",
                    tool_pattern=action_name,
                    reason="bounded productized live smoke authorization",
                ),
            )
        run = worker.run(WorkerRequest(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=state.root_node_id,
            worker_name="BrowserWorker",
            constraints={
                "browser_executable": str(executable),
                "browser_transport": "websocket",
                "browser_allow_unsafe_sandbox_bypass": True,
                "browser_allow_loopback": True,
                "browser_allow_literal_ip": True,
                "canonical_session_id": "productized-live",
                "keep_alive": True,
                "permission_mode": "interactive",
                "permission_headless": False,
                "permission_session_id": permission_session_id,
                "permission_session_custody_token": permission_custody.token,
                "allowed_schemes": ["file"],
                "browser_plan": [
                    {"action": "navigate", "arguments": {"url": page_url}},
                    {"action": "list_targets", "arguments": {}},
                ],
            },
        ))
        result_session_id = str(run.worker_result.metadata.get("browser_session_id") or "")
        cleanup_session_id = result_session_id
        if not cleanup_session_id:
            matching_sessions = [
                session
                for session in runtime.list_sessions()
                if session.run_id == state.run_id
                and session.task_id == state.task_id
                and session.canonical_session_id == "productized-live"
            ]
            if matching_sessions:
                cleanup_session_id = matching_sessions[-1].session_id
        diagnostic = None
        diagnostic_error = ""
        if cleanup_session_id:
            try:
                diagnostic = runtime.diagnose(cleanup_session_id)
            except Exception as error:  # noqa: BLE001 - diagnostics must not hide the primary result.
                diagnostic_error = f"{type(error).__name__}: {error}"
        cleanup_worker = worker
        owner_loss: dict[str, object] = {
            "attempted": False,
            "ok": False,
            "error": "",
        }
        original_pid = 0
        custody_marker: Path | None = None
        recovered_controller: ChromeProcessController | None = None
        if result_session_id:
            try:
                original_session = runtime.get_session(result_session_id)
                original_pid = int(original_session.process_id or 0)
                profile = runtime._runtime.profile_store.get(original_session.profile_id)  # type: ignore[attr-defined]
                custody_marker = profile.root / "process" / "chrome-custody.json" if profile is not None else None
                recovered_registry = BrowserRuntimeRegistry()
                recovered_worker = BrowserWorkerRuntime(
                    project_root=root,
                    workspace_root=workspace,
                    artifact_root=root / "artifacts",
                    permission_state_path=root / "permission-state.json",
                    browser_runtime_registry=recovered_registry,
                )
                recovered_runtime = recovered_registry.get_or_create(runtime_config)
                recovered_controller = recovered_runtime.process_controller
                resumed = recovered_worker.run(WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="BrowserWorker",
                    constraints={
                        "browser_lifecycle_command": "resume",
                        "browser_session_id": result_session_id,
                        "canonical_session_id": "productized-live",
                    },
                ))
                resumed_session_id = str(resumed.worker_result.metadata.get("browser_session_id") or "")
                owner_loss = {
                    "attempted": True,
                    "ok": bool(
                        resumed.worker_result.ok
                        and resumed_session_id == result_session_id
                        and original_pid > 0
                        and custody_marker is not None
                        and custody_marker.is_file()
                        and recovered_controller.owns(original_pid)
                    ),
                    "error": str(resumed.worker_result.error or ""),
                    "original_pid": original_pid,
                    "resumed_session_id": resumed_session_id,
                    "same_session": resumed_session_id == result_session_id,
                    "custody_marker_before_cancel": bool(custody_marker and custody_marker.is_file()),
                    "controller_owns_pid_after_resume": bool(
                        original_pid and recovered_controller.owns(original_pid)
                    ),
                    "resume": to_jsonable(resumed.worker_result),
                }
                if owner_loss["ok"]:
                    cleanup_worker = recovered_worker
            except Exception as error:  # noqa: BLE001 - owner loss is mandatory evidence, not a crash.
                owner_loss = {
                    "attempted": True,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                    "original_pid": original_pid,
                }
        stopped = None
        cleanup_error = ""
        if cleanup_session_id:
            try:
                stopped = cleanup_worker.run(WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="BrowserWorker",
                    constraints={
                        "browser_lifecycle_command": "cancel",
                        "browser_session_id": cleanup_session_id,
                        "canonical_session_id": "productized-live",
                        "reason": "live_smoke_complete",
                    },
                ))
            except Exception as error:  # noqa: BLE001 - smoke must preserve the primary result.
                cleanup_error = f"{type(error).__name__}: {error}"
        artifacts = [to_jsonable(item) for item in run.worker_result.artifacts]
        cleanup_ok = bool(stopped is not None and stopped.worker_result.ok)
        if owner_loss.get("attempted"):
            process_alive_after_cancel = bool(
                original_pid and recovered_controller is not None and recovered_controller.is_alive(original_pid)
            )
            marker_exists_after_cancel = bool(custody_marker and custody_marker.exists())
            owner_loss["process_alive_after_cancel"] = process_alive_after_cancel
            owner_loss["custody_marker_after_cancel"] = marker_exists_after_cancel
            owner_loss["pid_terminated"] = bool(original_pid and not process_alive_after_cancel)
            owner_loss["marker_removed"] = not marker_exists_after_cancel
            owner_loss["ok"] = bool(
                owner_loss.get("ok")
                and cleanup_ok
                and original_pid
                and not process_alive_after_cancel
                and not marker_exists_after_cancel
            )
        live_ok = bool(
            run.worker_result.ok
            and result_session_id
            and cleanup_ok
            and owner_loss.get("ok")
            and len(artifacts) >= 2
        )
        payload = {
            "ok": live_ok,
            "blocking": not live_ok,
            "executable": str(executable),
            "transport": "websocket",
            "permission_mode": "interactive",
            "unsafe_sandbox_bypass_explicit": True,
            "session_id": result_session_id,
            "generation": run.worker_result.metadata.get("browser_logical_lease_generation"),
            "receipt_count": run.worker_result.metadata.get("browser_receipt_count"),
            "artifacts": artifacts,
            "result": to_jsonable(run.worker_result),
            "events": [to_jsonable(item) for item in run.event_records],
            "diagnostic": to_jsonable(diagnostic) if diagnostic is not None else None,
            "diagnostic_error": diagnostic_error,
            "owner_loss": owner_loss,
            "cleanup": {
                "attempted": bool(cleanup_session_id),
                "session_id": cleanup_session_id,
                "ok": cleanup_ok,
                "error": cleanup_error,
                "result": to_jsonable(stopped.worker_result) if stopped is not None else None,
            },
        }
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if live_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
