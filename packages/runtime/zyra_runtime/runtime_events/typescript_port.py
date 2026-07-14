"""Fail-closed process port to the TypeScript-owned canonical event spine."""

from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time
from typing import Any, Mapping
from uuid import uuid4

from .models import (
    JsonValue,
    RpcErrorPayload,
    RuntimeEventContractError,
    RuntimeEventProcessError,
    coerce_json,
    redact_sensitive_fields,
    require_mapping,
)


@dataclass(frozen=True, slots=True)
class TypeScriptPortConfig:
    database_path: Path
    artifact_root: Path
    script_path: Path
    node_binary: str = "node"
    startup_timeout_seconds: float = 15.0
    request_timeout_seconds: float = 30.0
    shutdown_timeout_seconds: float = 5.0
    max_response_bytes: int = 16 * 1024 * 1024
    diagnostic_lines: int = 64

    def normalized(self) -> "TypeScriptPortConfig":
        database = self.database_path.expanduser().resolve()
        artifacts = self.artifact_root.expanduser().resolve()
        script = self.script_path.expanduser().resolve()
        if not script.is_file():
            raise RuntimeEventProcessError(
                f"runtime event spine entrypoint does not exist: {script}",
                code="runtime_event_entrypoint_missing",
            )
        if self.startup_timeout_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise RuntimeEventContractError("process timeouts must be positive")
        if self.max_response_bytes < 8192:
            raise RuntimeEventContractError("max_response_bytes must be at least 8192")
        return TypeScriptPortConfig(
            database_path=database,
            artifact_root=artifacts,
            script_path=script,
            node_binary=self.node_binary,
            startup_timeout_seconds=self.startup_timeout_seconds,
            request_timeout_seconds=self.request_timeout_seconds,
            shutdown_timeout_seconds=self.shutdown_timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            diagnostic_lines=self.diagnostic_lines,
        )


@dataclass(frozen=True, slots=True)
class PortDiagnostics:
    pid: int | None
    running: bool
    generation: int
    requests: int
    failures: int
    started_at_monotonic: float | None
    last_stderr: tuple[str, ...]

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "pid": self.pid,
            "running": self.running,
            "generation": self.generation,
            "requests": self.requests,
            "failures": self.failures,
            "startedAtMonotonic": self.started_at_monotonic,
            "lastStderr": list(self.last_stderr),
        }


@dataclass(slots=True)
class _PendingResponse:
    request_id: str
    payload: Mapping[str, Any] | None = None
    transport_error: BaseException | None = None


class TypeScriptRuntimeEventPort:
    """Own one persistent Node process and serialize requests over JSON lines.

    The port never emulates canonical append behavior in Python.  If Node or the
    TypeScript module is unavailable, calls fail and the caller must surface the
    failure instead of silently falling back to the legacy SQLite writer.
    """

    def __init__(self, config: TypeScriptPortConfig) -> None:
        self.config = config.normalized()
        self._lifecycle_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._pending: dict[str, queue.Queue[_PendingResponse]] = {}
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._generation = 0
        self._requests = 0
        self._failures = 0
        self._started_at: float | None = None
        self._closed = False
        self._stderr: deque[str] = deque(maxlen=config.diagnostic_lines)
        atexit.register(self.close)

    @classmethod
    def for_workspace(
        cls,
        *,
        database_path: str | os.PathLike[str],
        artifact_root: str | os.PathLike[str],
        workspace_root: str | os.PathLike[str] | None = None,
        node_binary: str | None = None,
        request_timeout_seconds: float = 30.0,
    ) -> "TypeScriptRuntimeEventPort":
        if workspace_root is None:
            # runtime_events -> zyra_runtime -> runtime -> packages -> zyra
            root = Path(__file__).resolve().parents[4]
        else:
            root = Path(workspace_root).expanduser().resolve()
        script = root / "packages" / "runtime" / "runtime-event-spine" / "src" / "stdio-server.ts"
        binary = node_binary or os.environ.get("ZYRA_NODE_BINARY", "node")
        return cls(
            TypeScriptPortConfig(
                database_path=Path(database_path),
                artifact_root=Path(artifact_root),
                script_path=script,
                node_binary=binary,
                request_timeout_seconds=request_timeout_seconds,
            )
        )

    @property
    def database_path(self) -> Path:
        return self.config.database_path

    @property
    def artifact_root(self) -> Path:
        return self.config.artifact_root

    def diagnostics(self) -> PortDiagnostics:
        with self._lifecycle_lock:
            process = self._process
            running = process is not None and process.poll() is None
            return PortDiagnostics(
                pid=process.pid if running else None,
                running=running,
                generation=self._generation,
                requests=self._requests,
                failures=self._failures,
                started_at_monotonic=self._started_at,
                last_stderr=tuple(self._stderr),
            )

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise RuntimeEventProcessError(
                    "runtime event port is closed",
                    code="runtime_event_port_closed",
                )
            if self._process is not None and self._process.poll() is None:
                return
            self._assert_node_available()
            self.config.database_path.parent.mkdir(parents=True, exist_ok=True)
            self.config.artifact_root.mkdir(parents=True, exist_ok=True)
            command = [
                self.config.node_binary,
                "--experimental-strip-types",
                str(self.config.script_path),
                "--db",
                str(self.config.database_path),
                "--artifact-root",
                str(self.config.artifact_root),
            ]
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(self.config.script_path.parents[4]),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=creation_flags,
                )
            except OSError as error:
                self._failures += 1
                raise RuntimeEventProcessError(
                    f"failed to start TypeScript runtime event spine: {error}",
                    code="runtime_event_process_start_failed",
                    details={"command": command, "errorType": type(error).__name__},
                ) from error
            self._process = process
            self._generation += 1
            generation = self._generation
            self._started_at = time.monotonic()
            self._reader_thread = threading.Thread(
                target=self._read_stdout,
                args=(process, generation),
                name=f"zyra-runtime-event-reader-{generation}",
                daemon=True,
            )
            self._stderr_thread = threading.Thread(
                target=self._read_stderr,
                args=(process, generation),
                name=f"zyra-runtime-event-stderr-{generation}",
                daemon=True,
            )
            self._reader_thread.start()
            self._stderr_thread.start()
        try:
            response = self.call("ping", {}, timeout=self.config.startup_timeout_seconds, _already_started=True)
        except BaseException:
            self._terminate_process(process)
            raise
        if not isinstance(response, Mapping) or response.get("ok") is not True:
            self._terminate_process(process)
            raise RuntimeEventProcessError(
                "TypeScript runtime event spine failed its startup handshake",
                code="runtime_event_handshake_failed",
                details={"response": redact_sensitive_fields(response)},
            )

    def call(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
        _already_started: bool = False,
    ) -> JsonValue:
        method = method.strip()
        if not method:
            raise RuntimeEventContractError("rpc method must not be empty")
        if not _already_started:
            self.start()
        with self._lifecycle_lock:
            process = self._process
            if process is None or process.poll() is not None:
                self._failures += 1
                raise self._process_ended_error(process, method)
        request_id = uuid4().hex
        inbox: queue.Queue[_PendingResponse] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = inbox
        request = {
            "id": request_id,
            "method": method,
            "params": coerce_json(dict(params or {})),
        }
        encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        try:
            with self._write_lock:
                stdin = process.stdin
                if stdin is None or stdin.closed:
                    raise BrokenPipeError("runtime event process stdin is closed")
                stdin.write(encoded)
                stdin.write("\n")
                stdin.flush()
            self._requests += 1
            wait_timeout = timeout if timeout is not None else self.config.request_timeout_seconds
            try:
                pending = inbox.get(timeout=wait_timeout)
            except queue.Empty as error:
                self._failures += 1
                raise RuntimeEventProcessError(
                    f"runtime event request timed out: {method}",
                    code="runtime_event_rpc_timeout",
                    details={
                        "method": method,
                        "timeoutSeconds": wait_timeout,
                        "process": self.diagnostics().to_jsonable(),
                    },
                ) from error
            if pending.transport_error is not None:
                self._failures += 1
                raise RuntimeEventProcessError(
                    f"runtime event transport failed during {method}: {pending.transport_error}",
                    code="runtime_event_transport_failed",
                    details={"method": method, "process": self.diagnostics().to_jsonable()},
                ) from pending.transport_error
            response = require_mapping(pending.payload, "rpc response")
            if response.get("ok") is not True:
                self._failures += 1
                rpc_error = RpcErrorPayload.from_json(response.get("error", {}))
                raise RuntimeEventProcessError(
                    rpc_error.message,
                    code=rpc_error.code,
                    details={**rpc_error.details, "retryable": rpc_error.retryable, "method": method},
                )
            return coerce_json(response.get("result"))
        except (BrokenPipeError, OSError) as error:
            self._failures += 1
            raise RuntimeEventProcessError(
                f"failed to send runtime event request {method}: {error}",
                code="runtime_event_transport_write_failed",
                details={"method": method, "process": self.diagnostics().to_jsonable()},
            ) from error
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            process = self._process
        if process is not None and process.poll() is None:
            try:
                self.call(
                    "shutdown",
                    {},
                    timeout=self.config.shutdown_timeout_seconds,
                    _already_started=True,
                )
            except RuntimeError:
                pass
            self._terminate_process(process)
        with self._lifecycle_lock:
            self._process = None

    def _assert_node_available(self) -> None:
        binary = self.config.node_binary
        if Path(binary).is_file():
            return
        if shutil.which(binary) is None:
            raise RuntimeEventProcessError(
                f"Node.js executable is unavailable: {binary}",
                code="runtime_event_node_unavailable",
            )

    def _read_stdout(self, process: subprocess.Popen[str], generation: int) -> None:
        stdout = process.stdout
        if stdout is None:
            self._fail_pending(BrokenPipeError("runtime event process stdout is unavailable"))
            return
        try:
            for line in stdout:
                if generation != self._generation:
                    return
                if len(line.encode("utf-8", errors="replace")) > self.config.max_response_bytes:
                    self._fail_pending(RuntimeEventContractError("runtime event response exceeded byte budget"))
                    self._terminate_process(process)
                    return
                raw = line.strip()
                if not raw:
                    continue
                try:
                    parsed = json.loads(raw)
                    response = require_mapping(parsed, "rpc response")
                    request_id = require_string_compat(response.get("id"), "rpc response id")
                except BaseException as error:
                    self._stderr.append(f"invalid stdout frame: {type(error).__name__}: {error}")
                    self._fail_pending(error)
                    self._terminate_process(process)
                    return
                with self._pending_lock:
                    inbox = self._pending.get(request_id)
                if inbox is not None:
                    try:
                        inbox.put_nowait(_PendingResponse(request_id=request_id, payload=response))
                    except queue.Full:
                        self._stderr.append(f"duplicate response for request {request_id}")
        except BaseException as error:
            self._stderr.append(f"stdout reader failed: {type(error).__name__}: {error}")
            self._fail_pending(error)
        finally:
            if generation == self._generation and process.poll() is not None:
                self._fail_pending(self._process_ended_error(process, "read"))

    def _read_stderr(self, process: subprocess.Popen[str], generation: int) -> None:
        stderr = process.stderr
        if stderr is None:
            return
        try:
            for line in stderr:
                if generation != self._generation:
                    return
                normalized = line.rstrip()
                if normalized:
                    self._stderr.append(normalized[:4096])
        except BaseException as error:
            self._stderr.append(f"stderr reader failed: {type(error).__name__}: {error}")

    def _fail_pending(self, error: BaseException) -> None:
        with self._pending_lock:
            pending = tuple(self._pending.values())
        for inbox in pending:
            try:
                inbox.put_nowait(_PendingResponse(request_id="", transport_error=error))
            except queue.Full:
                pass

    def _terminate_process(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=self.config.shutdown_timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=self.config.shutdown_timeout_seconds)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                try:
                    stream.close()
                except OSError:
                    pass

    def _process_ended_error(
        self,
        process: subprocess.Popen[str] | None,
        method: str,
    ) -> RuntimeEventProcessError:
        return RuntimeEventProcessError(
            f"TypeScript runtime event spine exited while serving {method}",
            code="runtime_event_process_exited",
            details={
                "method": method,
                "returnCode": process.poll() if process is not None else None,
                "stderr": list(self._stderr),
            },
        )


def require_string_compat(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeEventContractError(f"{field_name} must be a non-empty string")
    return value

