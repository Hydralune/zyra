from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import threading
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from zyra_core import EventRecord, to_jsonable
from zyra_integrations.e02_ports import TypeScriptE02ApiPort
from zyra_runtime import WorkerRequest

from .browser_worker import BrowserWorkerRun, BrowserWorkerRuntime


_BROWSER_ACTION_ALIASES = {
    "navigate": "open_url",
    "open": "open_url",
    "goto": "open_url",
    "extract": "extract_text",
    "read_page": "extract_text",
    "state": "snapshot_state",
    "read_state": "snapshot_state",
}


class BrowserWorkerActionDispatchPort:
    """Delegate an approved CodeWorker browser call to BrowserWorkerRuntime.

    The CodeWorker permission receipt authorizes the bounded delegation itself.
    BrowserWorker still performs its own TypeScript-owned, one-use permission
    checks for every expanded browser action and retains the normal browser and
    SandboxGateway policy boundaries.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        artifact_root: str | Path,
        state_root: str | Path,
        workspace_edit_port: Any,
        runtime: BrowserWorkerRuntime | None = None,
        permission_port: TypeScriptE02ApiPort | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.state_root = Path(state_root).resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._event_records: list[EventRecord] = []
        self._closed = False
        self._owns_permission_port = permission_port is None and runtime is None
        self._permission_port = permission_port
        if runtime is None:
            if self._permission_port is None:
                self._permission_port = TypeScriptE02ApiPort(
                    project_root=self.project_root,
                    workspace_root=self.workspace_root,
                    state_path=self.state_root / "typescript-browser-permission.json",
                    artifact_root=self.artifact_root,
                    permission_mode="auto",
                    sealed_autonomous=False,
                )
            runtime = BrowserWorkerRuntime(
                project_root=self.project_root,
                workspace_root=self.workspace_root,
                artifact_root=self.artifact_root,
                permission_state_path=self.state_root / "browser-permission-custody.json",
                workspace_edit_port=workspace_edit_port,
                workspace_gateway_required=True,
                sandbox_gateway_services={
                    "workspace_gateway_required": True,
                    "sandbox_gateway_required": True,
                    # The physical CodeWorker can start a task-local static
                    # server. BrowserWorker may reach that loopback endpoint,
                    # but public and private non-loopback targets remain closed.
                    "sandbox_gateway_allow_loopback_network": True,
                    # Gateway scheme admission uses this flag before address
                    # classification. The separate public-network flag stays
                    # closed by the explicit deployment allowlist and the
                    # dispatch input check, so HTTP remains loopback-only.
                    "sandbox_gateway_allow_public_http": True,
                    "sandbox_gateway_allow_public_https": False,
                    "sandbox_gateway_allowed_hosts": (
                        "127.0.0.1",
                        "localhost",
                        "::1",
                    ),
                },
                e02_permission_port=self._permission_port,
            )
        self.runtime = runtime

    def handles(self, tool_name: str) -> bool:
        return str(tool_name) == "browser"

    def available(self, tool_name: str) -> bool:
        return self.handles(tool_name) and not self._closed

    def available_actions(self) -> tuple[str, ...]:
        return ("browser",) if not self._closed else ()

    def dispatch_action(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        tool_name: str,
        tool_call_id: str,
        arguments: Mapping[str, Any],
        metadata: Mapping[str, Any],
        permission_receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        del metadata
        if not self.available(tool_name):
            raise RuntimeError("BrowserWorker action dispatch is unavailable")
        receipt_id = str(permission_receipt.get("receipt_id") or "")
        receipt_tool_call_id = str(permission_receipt.get("tool_call_id") or "")
        if (
            permission_receipt.get("allowed") is not True
            or not receipt_id
            or (receipt_tool_call_id and receipt_tool_call_id != str(tool_call_id))
        ):
            raise RuntimeError("BrowserWorker dispatch requires the exact approved browser receipt")

        plan, url = _browser_plan(arguments)
        request_id = _stable_id("browser-request", run_id, task_id, tool_call_id)
        browser_session_id = _stable_id("browser-session", run_id, task_id, tool_call_id)
        host = (urlparse(url).hostname or "").casefold()
        requested_domains = arguments.get("allowed_domains")
        allowed_domains = (
            [str(item) for item in requested_domains if str(item)]
            if isinstance(requested_domains, (list, tuple))
            else []
        )
        if host and host not in allowed_domains:
            allowed_domains.append(host)
        request = WorkerRequest(
            run_id=str(run_id),
            task_id=str(task_id),
            node_id=str(node_id),
            worker_name="BrowserWorker",
            request_id=request_id,
            constraints={
                "browser_backend": "zyra-browser-productized",
                "browser_plan": plan,
                "canonical_session_id": browser_session_id,
                "permission_session_id": _stable_id(
                    "browser-permission", run_id, task_id, tool_call_id
                ),
                # The trusted dispatch adapter selects the TypeScript E02 port
                # in auto mode. Caller-supplied bypass/auto flags are neither
                # accepted nor projected into this request.
                "permission_mode": "default",
                "browser_allow_loopback": True,
                # _browser_plan has already rejected every non-loopback IP.
                "browser_allow_literal_ip": True,
                "browser_allowed_domains": allowed_domains,
                "allowed_schemes": ["http", "https"],
                "headless": True,
                "keep_alive": False,
                # The Windows headless Chrome sandbox cannot initialize in the
                # managed physical lane. This authority is safe only because
                # this adapter also enforces an ephemeral task profile and a
                # loopback-only navigation target before BrowserWorker starts.
                "browser_allow_unsafe_sandbox_bypass": os.name == "nt",
            },
        )
        run: BrowserWorkerRun = self.runtime.run(request)
        with self._lock:
            self._event_records.extend(run.event_records)

        worker_result = to_jsonable(run.worker_result)
        dispatch_session_id = _stable_id(
            "browser-dispatch", run_id, task_id, tool_call_id, receipt_id
        )
        return {
            "schema": "zyra.backend-action-dispatch-result/v1",
            "tool_result": {
                "schema": "zyra.terminal-action-result/v1",
                "tool_call_id": str(tool_call_id),
                "ok": run.worker_result.ok,
                "summary": run.worker_result.summary,
                "output": {
                    "execution_location": "BrowserWorkerRuntime",
                    "browser_worker_result": worker_result,
                    "browser_context_projection": to_jsonable(
                        run.browser_context_projection
                    ),
                },
                "error": run.worker_result.error,
                "metadata": {
                    **dict(run.worker_result.metadata),
                    "delegated_runtime": "BrowserWorkerRuntime",
                    "browser_worker_request_id": request_id,
                },
            },
            "dispatch_receipt": {
                "schema": "zyra.backend-action-dispatch-receipt/v1",
                "state_owner": "python.BrowserWorkerActionDispatchPort",
                "permission_owner": "typescript.PermissionCoordinator",
                "permission_receipt_id": receipt_id,
                "dispatch_session_id": dispatch_session_id,
                "backend_lease_id": browser_session_id,
                "backend_id": "physical-browser-worker",
                "backend_kind": "local_process",
                "backend_location": "local",
                "envelope_id": request_id,
                "operation": "tool.browser",
                "attempt_ids": [request_id],
                "transport_receipt_ids": [],
                "backend_changed": True,
                "workspace_root_projected": False,
                "artifact_root_projected": False,
            },
        }

    def event_records(self) -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(self._event_records)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        if self._owns_permission_port and self._permission_port is not None:
            self._permission_port.close()


def _browser_plan(arguments: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    url = str(arguments.get("url") or "").strip()
    if not url:
        raise ValueError(
            "BrowserWorker delegation requires an HTTP(S) URL; start the task-local "
            "static server and pass its URL instead of inline HTML"
        )
    scheme = urlparse(url).scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("BrowserWorker delegation accepts only HTTP(S) target URLs")
    host = (urlparse(url).hostname or "").strip("[]").casefold()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host == "localhost" or host.endswith(".localhost")
    if not loopback:
        raise ValueError(
            "Physical BrowserWorker delegation is restricted to the task-local "
            "loopback static server"
        )
    requested = str(arguments.get("action") or "snapshot_state").strip().casefold()
    action = _BROWSER_ACTION_ALIASES.get(requested, requested)
    if action not in {"open_url", "snapshot_state", "extract_text"}:
        raise ValueError(f"Unsupported delegated BrowserWorker action: {requested}")

    plan: list[dict[str, Any]] = [
        {"action": "open_url", "arguments": {"url": url}},
    ]
    if action == "extract_text":
        plan.extend(
            [
                {"action": "extract_text", "arguments": {"max_chars": 100_000}},
                {"action": "snapshot_state", "arguments": {}},
            ]
        )
    else:
        plan.extend(
            [
                {"action": "snapshot_state", "arguments": {}},
                {"action": "extract_text", "arguments": {"max_chars": 100_000}},
            ]
        )
    return plan, url


def _stable_id(prefix: str, *parts: Any) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str)
    return f"{prefix}_{hashlib.sha256(encoded.encode('utf-8')).hexdigest()[:32]}"


__all__ = ["BrowserWorkerActionDispatchPort"]
