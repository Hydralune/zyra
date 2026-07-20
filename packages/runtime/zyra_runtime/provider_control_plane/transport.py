from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from .models import ProviderControlPlanePortError


RPC_PROTOCOL = "zyra.provider-control-plane.rpc/v1"


class _LineReader:
    def __init__(self, stream: Any) -> None:
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread = threading.Thread(target=self._read, args=(stream,), daemon=True)
        self._thread.start()

    def _read(self, stream: Any) -> None:
        try:
            for line in stream:
                self._queue.put(str(line).rstrip("\r\n"))
        finally:
            self._queue.put(None)

    def get(self, timeout: float) -> str | None:
        try:
            return self._queue.get(timeout=max(0.01, timeout))
        except queue.Empty as error:
            raise ProviderControlPlanePortError(
                "provider_rpc_timeout",
                "provider control-plane process did not respond before the deadline",
            ) from error


class ProviderControlPlaneProcess:
    """Long-lived JSON-lines port; TypeScript remains the only logical state owner."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        database_path: str | Path,
        request_timeout_seconds: float = 180.0,
        node_executable: str = "node",
    ) -> None:
        self.project_root = Path(project_root).expanduser().resolve()
        self.database_path = Path(database_path).expanduser().resolve()
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.node_executable = node_executable
        self._process: subprocess.Popen[str] | None = None
        self._reader: _LineReader | None = None
        self._lock = threading.RLock()
        self._request_index = 0

    def request(self, operation: str, payload: Mapping[str, Any] | None = None) -> Any:
        with self._lock:
            process = self._ensure_process()
            self._request_index += 1
            request_id = f"provider_rpc_{self._request_index}_{uuid.uuid4().hex}"
            frame = {
                "protocol": RPC_PROTOCOL,
                "requestId": request_id,
                "operation": str(operation),
                "payload": dict(payload or {}),
            }
            _reject_inline_secrets(frame)
            if process.stdin is None or process.poll() is not None:
                raise self._process_error(process, "provider RPC process is not writable")
            try:
                process.stdin.write(json.dumps(frame, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError) as error:
                raise self._process_error(process, "provider RPC process disconnected") from error
            assert self._reader is not None
            line = self._reader.get(self.request_timeout_seconds)
            if line is None:
                raise self._process_error(process, "provider RPC process closed stdout")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as error:
                self.close()
                raise ProviderControlPlanePortError(
                    "provider_rpc_protocol_error",
                    "provider RPC process emitted non-JSON output",
                ) from error
            if not isinstance(response, dict):
                raise ProviderControlPlanePortError(
                    "provider_rpc_protocol_error",
                    "provider RPC response must be an object",
                )
            if response.get("protocol") != RPC_PROTOCOL or response.get("requestId") != request_id:
                raise ProviderControlPlanePortError(
                    "provider_rpc_protocol_error",
                    "provider RPC response identity mismatch",
                    detail={"response": response},
                )
            if response.get("ok") is not True:
                failure = response.get("error") if isinstance(response.get("error"), dict) else {}
                raise ProviderControlPlanePortError(
                    str(failure.get("code") or "provider_rpc_failed"),
                    str(failure.get("message") or "provider RPC operation failed"),
                    detail=(failure.get("detail") if isinstance(failure.get("detail"), dict) else {}),
                )
            return response.get("result")

    def health(self) -> dict[str, Any]:
        value = self.request("health")
        return dict(value) if isinstance(value, dict) else {}

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            self._reader = None
            if process is None:
                return
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)

    def __enter__(self) -> ProviderControlPlaneProcess:
        self._ensure_process()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _ensure_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        entrypoint = (
            self.project_root
            / "packages"
            / "runtime"
            / "provider-control-plane"
            / "src"
            / "stdio-server.ts"
        )
        if not entrypoint.is_file():
            raise ProviderControlPlanePortError(
                "provider_rpc_entrypoint_missing",
                f"provider control-plane entrypoint not found: {entrypoint}",
            )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        environment = os.environ.copy()
        environment["NO_COLOR"] = "1"
        process = subprocess.Popen(
            [
                self.node_executable,
                "--experimental-strip-types",
                str(entrypoint),
                str(self.database_path),
            ],
            cwd=str(self.project_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=environment,
            creationflags=(subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0),
        )
        if process.stdout is None:
            process.kill()
            raise ProviderControlPlanePortError(
                "provider_rpc_process_failed",
                "provider RPC process has no stdout stream",
            )
        self._process = process
        self._reader = _LineReader(process.stdout)
        return process

    @staticmethod
    def _process_error(process: subprocess.Popen[str], fallback: str) -> ProviderControlPlanePortError:
        stderr = ""
        if process.poll() is not None and process.stderr is not None:
            try:
                stderr = process.stderr.read().strip()
            except OSError:
                stderr = ""
        return ProviderControlPlanePortError(
            "provider_rpc_process_failed",
            stderr[-2_000:] or fallback,
            detail={"returncode": process.poll(), "observed_at": time.time()},
        )


def _reject_inline_secrets(value: Mapping[str, Any]) -> None:
    encoded = json.dumps(value, ensure_ascii=False).lower()
    forbidden = (
        '"apikey":',
        '"api_key":',
        '"secret":',
        '"accesstoken":',
        '"access_token":',
        '"refreshtoken":',
        '"refresh_token":',
    )
    if any(name in encoded for name in forbidden):
        raise ProviderControlPlanePortError(
            "provider_inline_secret_rejected",
            "provider RPC accepts secret references, never inline credential bytes",
        )
