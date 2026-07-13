from __future__ import annotations

import json
import sys
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
for package in ("core", "runtime", "integrations", "workers", "skills"):
    path = PROJECT_ROOT / "packages" / package
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from zyra_core import create_task_state  # noqa: E402
from zyra_runtime import WorkerRequest  # noqa: E402
from zyra_runtime.permission.custody import (  # noqa: E402
    PermissionSessionCustodyBinding,
    PermissionSessionCustodyStore,
)
from zyra_runtime.permission.models import (  # noqa: E402
    PermissionEffect,
    PermissionRuleRecord,
    PermissionRuleSource,
    PermissionScope,
    PermissionScopeKind,
)
from zyra_runtime.permission.store import PermissionStateStore  # noqa: E402
from zyra_workers import BrowserWorkerRuntime, find_browser_executable, inspect_browser_use_runtime  # noqa: E402


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


def authorize(runtime: BrowserWorkerRuntime, *, run_id: str, task_id: str, session_id: str) -> dict[str, str]:
    store = PermissionStateStore(runtime.permission_state_path)
    custody = PermissionSessionCustodyStore(store).claim(
        PermissionSessionCustodyBinding(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_root=str(runtime.workspace_root),
        )
    )
    for action in runtime.action_registry.describe()["actions"]:
        store.add_session_rule(
            session_id,
            PermissionRuleRecord(
                effect=PermissionEffect.ALLOW,
                source=PermissionRuleSource.SESSION,
                scope=PermissionScope(PermissionScopeKind.SESSION, session_id=session_id),
                namespace_pattern="browser",
                tool_pattern=str(action["action"]),
                reason="bounded 04C-01 live foundation smoke authorization",
            ),
        )
    return {
        "permission_session_id": session_id,
        "permission_session_custody_token": custody.token,
    }


def main() -> int:
    if find_browser_executable() is None:
        print("browser_action_live_smoke=skipped")
        print("reason=no_chrome_or_edge")
        return 0
    health = inspect_browser_use_runtime(PROJECT_ROOT)
    if not health.importable:
        print("browser_action_live_smoke=skipped")
        print(f"reason=browser_use_unavailable:{health.error_type}:{health.error}")
        return 0
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        served = root / "served"
        workspace = root / "workspace"
        served.mkdir()
        workspace.mkdir()
        page = served / "index.html"
        page.write_text(
            "<html><head><title>04C action foundation</title></head><body>"
            "<input id='value' aria-label='value'>"
            "<button id='commit' onclick=\"document.body.dataset.result=document.querySelector('#value').value\">Commit</button>"
            "<p>bounded live action permission smoke</p></body></html>",
            encoding="utf-8",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHandler, directory=str(served)))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            state = create_task_state("Run bounded real browser action permission foundation smoke.")
            runtime = BrowserWorkerRuntime(
                project_root=PROJECT_ROOT,
                workspace_root=workspace,
                artifact_root=root / "artifacts",
            )
            authority = authorize(
                runtime,
                run_id=state.run_id,
                task_id=state.task_id,
                session_id="browser-action-foundation-live",
            )
            url = f"http://127.0.0.1:{server.server_address[1]}/{page.name}"
            run = runtime.run(
                WorkerRequest(
                    run_id=state.run_id,
                    task_id=state.task_id,
                    node_id=state.root_node_id,
                    worker_name="BrowserWorker",
                    constraints={
                        "browser_backend": "browser-use-live",
                        "browser_plan": [
                            {"action": "open_url", "arguments": {"url": url}},
                            {"action": "input_text", "arguments": {"id": "value", "text": "foundation-live"}},
                            {"action": "click_element", "arguments": {"id": "commit"}},
                            {"action": "search_page", "arguments": {"pattern": "bounded live action"}},
                        ],
                        "live_timeout_seconds": 60,
                        "live_navigation_timeout_seconds": 20,
                        "live_action_timeout_seconds": 20,
                        **authority,
                    },
                )
            )
            browser_events = [event for event in run.event_records if isinstance(event.payload.get("browser_result"), dict)]
            assert run.worker_result.ok, run.worker_result.error
            assert run.worker_result.metadata["action_registry_source"] == "zyra-browser-action-foundation"
            assert len(browser_events) == 4
            assert all(
                event.payload["browser_action"].get("permission_execution_grant_consumed") == "true"
                for event in browser_events
            )
            assert browser_events[-1].payload["browser_result"]["output"]["match_count"] >= 1
            print("browser_action_live_smoke=passed")
            print("browser_backend=browser-use-live")
            print("action_registry_source=zyra-browser-action-foundation")
            print(f"action_event_count={len(browser_events)}")
            print("permission_grants_consumed=4")
            print("source_runtime_dependency=false")
            print("summary=" + json.dumps(run.worker_result.summary, ensure_ascii=False))
            return 0
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
