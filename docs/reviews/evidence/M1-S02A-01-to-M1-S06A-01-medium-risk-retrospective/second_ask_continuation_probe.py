from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from typing import Any


# The probe is stored with the review evidence but is executed with the exact-target
# cleanroom as its working directory. Put that checkout first so no code is imported
# from the review worktree.
TARGET_ROOT = Path.cwd().resolve()
sys.path.insert(0, str(TARGET_ROOT))

from tests.integration.test_api_control_commands import (  # noqa: E402
    _fresh_api_handler,
    _get,
    _post,
    _post_with_status,
)


def _increment_command(path: str) -> str:
    program = (
        "from pathlib import Path; "
        f"p=Path({path!r}); "
        "n=int(p.read_text(encoding='utf-8')) if p.exists() else 0; "
        "p.write_text(str(n+1), encoding='utf-8')"
    )
    return subprocess.list2cmdline([sys.executable, "-c", program])


def _pending(
    base_url: str,
    *,
    session_id: str,
    run_id: str,
    task_id: str,
    headers: dict[str, str],
) -> list[dict[str, Any]]:
    return _get(
        base_url,
        (
            "/permissions/requests"
            f"?session_id={session_id}&run_id={run_id}"
            f"&task_id={task_id}&pending_only=true"
        ),
        headers=headers,
    )["requests"]["items"]


def _request_items(
    base_url: str,
    *,
    session_id: str,
    run_id: str,
    task_id: str,
    headers: dict[str, str],
    pending_only: bool,
) -> list[dict[str, Any]]:
    suffix = "&pending_only=true" if pending_only else ""
    return _get(
        base_url,
        (
            "/permissions/requests"
            f"?session_id={session_id}&run_id={run_id}"
            f"&task_id={task_id}{suffix}"
        ),
        headers=headers,
    )["requests"]["items"]


def _response_diagnostics(body: dict[str, Any]) -> dict[str, Any]:
    worker = dict(body.get("worker_result") or {})
    session = dict(body.get("permission_session") or {})
    for key in list(session):
        if "token" in key or "bearer" in key:
            session[key] = "<redacted>" if session[key] else session[key]
    return {
        "top_level_error": body.get("error"),
        "worker_error": worker.get("error"),
        "worker_summary": worker.get("summary"),
        "worker_metadata": worker.get("metadata"),
        "permission_session": session,
    }


def _resolve(
    base_url: str,
    request: dict[str, Any],
    *,
    session_id: str,
    run_id: str,
    task_id: str,
    headers: dict[str, str],
    suffix: str,
) -> None:
    _post(
        base_url,
        f"/permissions/requests/{request['request_id']}/resolve",
        {
            "session_id": session_id,
            "run_id": run_id,
            "task_id": task_id,
            "effect": "allow",
            "idempotency_key": f"medium-risk-second-ask-{suffix}",
        },
        headers=headers,
    )


def main() -> int:
    audit_tmp = TARGET_ROOT / ".audit-tmp"
    audit_tmp.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=audit_tmp) as tmpdir:
        root = Path(tmpdir)
        os.environ["ZYRA_SQLITE_PATH"] = str(root / "api.sqlite3")
        os.environ["ZYRA_EVENT_LOG"] = str(root / "events.jsonl")
        os.environ["ZYRA_TOOL_WORKSPACE"] = str(root / "workspace")
        os.environ["ZYRA_ARTIFACT_ROOT"] = str(root / "artifacts")
        os.environ["ZYRA_PERMISSION_STORE"] = str(root / "legacy-permissions.json")
        os.environ["ZYRA_PERMISSION_STATE"] = str(root / "permission-state.json")

        server = ThreadingHTTPServer(("127.0.0.1", 0), _fresh_api_handler())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_address[1]}"
        report: dict[str, Any] = {
            "schema": "zyra.medium-risk.second-ask-probe/v1",
            "target_root": str(TARGET_ROOT),
            "attempts": [],
        }
        try:
            created = _post(
                base_url,
                "/tasks",
                {"goal": "Probe two sequential permission-gated effects.", "auto_run": False},
            )
            task = created["task"]
            session_id = "medium-risk-second-ask-session"
            plan = [
                {
                    "tool_name": "shell",
                    "tool_call_id": "medium-risk-shell-1",
                    "arguments": {"command": _increment_command("first-count.txt")},
                },
                {
                    "tool_name": "shell",
                    "tool_call_id": "medium-risk-shell-2",
                    "arguments": {"command": _increment_command("second-count.txt")},
                },
            ]

            def run_attempt(token: str | None) -> tuple[int, dict[str, Any]]:
                constraints: dict[str, Any] = {
                    "session_id": session_id,
                    "tool_plan": plan,
                }
                if token:
                    constraints["session_custody_token"] = token
                return _post_with_status(
                    base_url,
                    f"/tasks/{task['task_id']}/workers/code",
                    {"constraints": constraints},
                )

            status, body = run_attempt(None)
            report["attempts"].append(
                {
                    "ordinal": 1,
                    "status": status,
                    "error": body.get("worker_result", {}).get("error"),
                    "diagnostics": _response_diagnostics(body),
                }
            )
            session = body["permission_session"]
            token = session.get("session_custody_token") or session.get("bearer_token")
            if not token:
                raise RuntimeError("first suspension did not return a custody token")
            headers = {"Authorization": f"Bearer {token}"}
            identity = {
                "session_id": session_id,
                "run_id": task["run_id"],
                "task_id": task["task_id"],
            }

            pending_1 = _pending(base_url, headers=headers, **identity)
            report["pending_after_attempt_1"] = [item["request_id"] for item in pending_1]
            report["all_requests_after_attempt_1"] = _request_items(
                base_url, headers=headers, pending_only=False, **identity
            )
            if len(pending_1) != 1:
                report["unexpected_pending_count_after_attempt_1"] = len(pending_1)
            for index, request in enumerate(pending_1, start=1):
                _resolve(base_url, request, headers=headers, suffix=f"first-{index}", **identity)

            status, body = run_attempt(token)
            report["attempts"].append(
                {
                    "ordinal": 2,
                    "status": status,
                    "error": body.get("worker_result", {}).get("error"),
                    "diagnostics": _response_diagnostics(body),
                }
            )
            pending_2 = _pending(base_url, headers=headers, **identity)
            report["pending_after_attempt_2"] = [item["request_id"] for item in pending_2]
            for index, request in enumerate(pending_2, start=1):
                _resolve(base_url, request, headers=headers, suffix=f"second-{index}", **identity)

            if pending_2:
                status, body = run_attempt(token)
                report["attempts"].append(
                    {
                        "ordinal": 3,
                        "status": status,
                        "error": body.get("worker_result", {}).get("error"),
                        "diagnostics": _response_diagnostics(body),
                    }
                )
            report["final_worker_ok"] = bool(body.get("worker_result", {}).get("ok"))

            workspace_id = task["metadata"]["workspace_ref"]["workspace_id"]
            for name in ("first-count.txt", "second-count.txt"):
                try:
                    item = _get(
                        base_url,
                        f"/workspaces/{workspace_id}/files?path={name}&read=true&encoding=utf-8",
                    )
                    report.setdefault("effect_counts", {})[name] = item.get("content")
                except Exception as error:  # noqa: BLE001 - record fail-closed outcome.
                    report.setdefault("effect_counts", {})[name] = f"unavailable:{type(error).__name__}"

            report["legacy_permission_store_exists"] = Path(
                os.environ["ZYRA_PERMISSION_STORE"]
            ).exists()
            report["verdict"] = (
                "continuous_two_ask_resume_exactly_once"
                if [item["status"] for item in report["attempts"]] == [409, 409, 201]
                and report.get("effect_counts")
                == {"first-count.txt": "1", "second-count.txt": "1"}
                else "continuous_two_ask_resume_not_proven"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if report["verdict"] == "continuous_two_ask_resume_exactly_once" else 2
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
