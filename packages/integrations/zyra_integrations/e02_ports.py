from __future__ import annotations

"""Physical JSONL transport to the TypeScript E02 capability owner.

This module deliberately contains no permission, MCP, skill, plugin, or command
policy.  It starts the locked TypeScript runtime, correlates protocol frames,
and returns already-decided projections or receipts to Python API callers.
"""

import json
import os
import queue
import shutil
import subprocess
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Any, Mapping, Sequence


E02_API_PROTOCOL_VERSION = "zyra.e02-api-port/v1"


class TypeScriptE02PortError(RuntimeError):
    def __init__(self, code: str, message: str, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = dict(detail or {})


class _LineReader:
    def __init__(self, stream: Any) -> None:
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def get(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=max(0.01, timeout))
        except queue.Empty as error:
            raise TypeScriptE02PortError(
                "e02_api_port_timeout",
                "TypeScript E02 API port did not return a protocol frame before the deadline.",
            ) from error

    def _read(self, stream: Any) -> None:
        try:
            for line in iter(stream.readline, ""):
                self._lines.put(line)
        finally:
            self._lines.put(None)


class TypeScriptE02ApiPort:
    """Thread-safe process transport; every logical decision remains TypeScript-owned."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        workspace_root: str | Path,
        state_path: str | Path,
        artifact_root: str | Path,
        timeout_seconds: float = 45.0,
        permission_mode: str = "default",
        sealed_autonomous: bool = False,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.workspace_root = Path(workspace_root).resolve()
        self.state_path = Path(state_path).resolve()
        self.artifact_root = Path(artifact_root).resolve()
        self.timeout_seconds = min(180.0, max(1.0, float(timeout_seconds)))
        self.permission_mode = str(permission_mode or "default")
        self.sealed_autonomous = bool(sealed_autonomous)
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._reader: _LineReader | None = None
        self._stderr_lines: deque[str] = deque(maxlen=40)
        self._stderr_thread: threading.Thread | None = None

    def request(
        self,
        operation: str,
        payload: Mapping[str, Any] | None = None,
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        if not str(operation or "").strip():
            raise TypeScriptE02PortError("e02_api_operation_missing", "E02 API operation is required.")
        with self._lock:
            self._ensure_started()
            return self._exchange(
                {
                    "type": "request",
                    "request_id": f"e02-{uuid.uuid4().hex}",
                    "operation": str(operation),
                    "payload": _json_value(dict(payload or {})),
                },
                timeout_seconds=timeout_seconds,
            )

    def health(self) -> dict[str, Any]:
        return _object(self.request("health"))

    def snapshot(self, domains: Sequence[str] = ()) -> dict[str, Any]:
        return _object(self.request("snapshot", {"domains": list(domains)}))

    def tools(self, *, namespace: str = "") -> dict[str, Any]:
        return _object(self.request("tools", {"namespace": namespace}))

    def skills(self, *, query: str = "") -> dict[str, Any]:
        return _object(self.request("skills", {"query": query}))

    def plugins(self) -> dict[str, Any]:
        return _object(self.request("plugins"))

    def commands(self) -> dict[str, Any]:
        return _object(self.request("commands"))

    def permission_get(
        self,
        *,
        view: str = "summary",
        request_id: str = "",
        status: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        return _object(
            self.request(
                "permission.get",
                {
                    "view": str(view),
                    "request_id": str(request_id),
                    "status": str(status),
                    "limit": min(1_000, max(1, int(limit))),
                },
            )
        )

    def permission_enforce(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Forward an exact physical-call identity to the TypeScript owner."""

        return _object(self.request("permission.enforce", dict(payload)))

    def permission_claim(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Consume a TypeScript-issued approval permit for one exact call."""

        return _object(self.request("permission.claim", dict(payload)))

    def permission_respond(
        self,
        request_id: str,
        effect: str,
        *,
        responder: str,
        response_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _object(
            self.request(
                "permission.respond",
                {
                    "request_id": str(request_id),
                    "effect": str(effect),
                    "responder": str(responder),
                    "response_id": str(response_id),
                    "metadata": dict(metadata or {}),
                },
            )
        )

    def permission_cancel(self, request_id: str, *, reason: str = "") -> dict[str, Any]:
        return _object(
            self.request(
                "permission.cancel",
                {"request_id": str(request_id), "reason": str(reason)},
            )
        )

    def permission_expire(self) -> dict[str, Any]:
        return _object(self.request("permission.expire"))

    def permission_policy(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return _object(self.request("permission.policy", dict(payload)))

    def mcp_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _object(
            self.request(
                "mcp.http.get",
                {"parts": list(parts), "query": dict(query or {})},
            )
        )

    def mcp_post(
        self,
        parts: Sequence[str],
        body: Mapping[str, Any] | None = None,
        *,
        actor_id: str = "",
    ) -> dict[str, Any]:
        payload = dict(body or {})
        if actor_id and "actor_id" not in payload:
            payload["actor_id"] = actor_id
        return _object(
            self.request(
                "mcp.http.post",
                {"parts": list(parts), "body": payload},
            )
        )

    def execute(
        self,
        tool_name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        identity: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "tool_name": str(tool_name),
            "arguments": dict(arguments or {}),
            **dict(identity or {}),
        }
        return _object(self.request("execute", payload))

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._reader = None
            if process is None:
                return
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=10.0)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5.0)

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        self._process = None
        self._reader = None
        bun = self._bun_executable()
        entrypoint = self.project_root / "apps" / "code-worker" / "src" / "main.ts"
        if not entrypoint.is_file():
            raise TypeScriptE02PortError(
                "e02_api_entrypoint_missing",
                f"TypeScript E02 API entrypoint is missing: {entrypoint}",
            )
        environment = dict(os.environ)
        environment.pop("NODE_PATH", None)
        environment["ZYRA_TYPESCRIPT_RUNTIME_OWNER"] = "canonical"
        environment["ZYRA_TYPESCRIPT_RUNTIME_PROTOCOL"] = E02_API_PROTOCOL_VERSION
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = subprocess.Popen(
            [bun, str(entrypoint), "--e02-api"],
            cwd=self.project_root,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise TypeScriptE02PortError("e02_api_pipe_missing", "TypeScript E02 API pipes were not created.")
        self._process = process
        self._reader = _LineReader(process.stdout)
        self._stderr_lines.clear()
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr,
            args=(process.stderr,),
            daemon=True,
        )
        self._stderr_thread.start()
        try:
            self._exchange(
                {
                    "type": "initialize",
                    "request_id": f"e02-init-{uuid.uuid4().hex}",
                    "workspace_root": str(self.workspace_root),
                    "state_path": str(self.state_path),
                    "artifact_root": str(self.artifact_root),
                    "permission_mode": self.permission_mode,
                    "sealed_autonomous": self.sealed_autonomous,
                    "runtime_constraints": {
                        "workspaceRoot": str(self.workspace_root),
                        "projectRoot": str(self.project_root),
                    },
                }
            )
        except Exception:
            self.close()
            raise

    def _exchange(
        self,
        frame: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Any:
        process = self._process
        reader = self._reader
        if process is None or process.stdin is None or reader is None:
            raise TypeScriptE02PortError("e02_api_not_started", "TypeScript E02 API process is not started.")
        if process.poll() is not None:
            raise self._process_exit_error(process)
        encoded = json.dumps(_json_value(dict(frame)), ensure_ascii=False, separators=(",", ":"))
        try:
            process.stdin.write(encoded + "\n")
            process.stdin.flush()
        except OSError as error:
            raise self._process_exit_error(process) from error
        line = reader.get(timeout_seconds or self.timeout_seconds)
        if line is None:
            raise self._process_exit_error(process)
        try:
            response = json.loads(line)
        except json.JSONDecodeError as error:
            raise TypeScriptE02PortError(
                "e02_api_frame_invalid",
                "TypeScript E02 API returned a non-JSON protocol frame.",
                {"frame_prefix": line[:160]},
            ) from error
        if not isinstance(response, dict):
            raise TypeScriptE02PortError("e02_api_frame_invalid", "TypeScript E02 API frame is not an object.")
        if response.get("protocol") != E02_API_PROTOCOL_VERSION:
            raise TypeScriptE02PortError("e02_api_protocol_mismatch", "TypeScript E02 API protocol mismatch.")
        if response.get("request_id") != frame.get("request_id"):
            raise TypeScriptE02PortError("e02_api_correlation_mismatch", "TypeScript E02 API response correlation mismatch.")
        if response.get("ok") is not True:
            error = _object(response.get("error"))
            raise TypeScriptE02PortError(
                str(error.get("code") or "e02_api_request_failed"),
                str(error.get("message") or "TypeScript E02 API request failed."),
                _object(error.get("detail")),
            )
        return response.get("payload")

    def _bun_executable(self) -> str:
        configured = str(os.environ.get("ZYRA_BUN_EXECUTABLE") or "").strip()
        local = self.project_root / "node_modules" / "bun" / "bin" / (
            "bun.exe" if os.name == "nt" else "bun"
        )
        candidate = configured or shutil.which("bun") or (str(local) if local.is_file() else "")
        if not candidate or not Path(candidate).is_file():
            raise TypeScriptE02PortError(
                "e02_api_bun_unavailable",
                "Bun 1.2.15 is required for the canonical TypeScript E02 API port.",
            )
        version = subprocess.run(
            [candidate, "--version"],
            cwd=self.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        if version.returncode != 0 or version.stdout.strip() != "1.2.15":
            raise TypeScriptE02PortError(
                "e02_api_bun_version_mismatch",
                f"Expected Bun 1.2.15, observed {version.stdout.strip() or 'unavailable'}.",
            )
        return str(Path(candidate).resolve())

    def _drain_stderr(self, stream: Any) -> None:
        for line in iter(stream.readline, ""):
            self._stderr_lines.append(line.rstrip())

    def _process_exit_error(self, process: subprocess.Popen[str]) -> TypeScriptE02PortError:
        return TypeScriptE02PortError(
            "e02_api_process_exited",
            "TypeScript E02 API process exited before completing the request.",
            {
                "returncode": process.poll(),
                "stderr_tail": list(self._stderr_lines),
                "python_fallback_attempted": False,
            },
        )


def _json_value(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _object(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "E02_API_PROTOCOL_VERSION",
    "TypeScriptE02ApiPort",
    "TypeScriptE02PortError",
]
