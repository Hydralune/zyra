from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .protocol import EdgeFrame, EdgeMessageKind, EdgeProtocolError, PROTOCOL_VERSION, request


class EdgeProcessError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class EdgeProcessEndpoint:
    worker_id: str
    host: str
    port: int
    endpoint: str
    pid: int
    protocol_version: int
    process_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "host": self.host,
            "port": self.port,
            "endpoint": self.endpoint,
            "pid": self.pid,
            "protocol_version": self.protocol_version,
            "process_identity": self.process_identity,
        }


@dataclass(frozen=True, slots=True)
class EdgeAttestationResponse:
    process_identity: str
    manifest_digest: str
    challenge_nonce: str
    response_digest: str
    protocol_version: int
    endpoint: str
    pid: int
    platform: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EdgeAttestationResponse":
        return cls(
            process_identity=str(value.get("process_identity") or ""),
            manifest_digest=str(value.get("manifest_digest") or ""),
            challenge_nonce=str(value.get("challenge_nonce") or ""),
            response_digest=str(value.get("response_digest") or ""),
            protocol_version=int(value.get("protocol_version") or 0),
            endpoint=str(value.get("endpoint") or ""),
            pid=int(value.get("pid") or 0),
            platform=str(value.get("platform") or ""),
        )


@dataclass(frozen=True, slots=True)
class EdgeExecutionResult:
    job_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    fence_epoch: int
    outcome: str
    artifact: Mapping[str, Any] | None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EdgeExecutionResult":
        artifact = value.get("artifact")
        return cls(
            job_id=str(value.get("job_id") or ""),
            task_id=str(value.get("task_id") or ""),
            attempt_id=str(value.get("attempt_id") or ""),
            lease_id=str(value.get("lease_id") or ""),
            fence_epoch=int(value.get("fence_epoch") or 0),
            outcome=str(value.get("outcome") or "failed"),
            artifact=dict(artifact) if isinstance(artifact, Mapping) else None,
        )


class EdgeWorkerProcessConnector:
    """Owns an independent Zyra edge process and its authenticated TCP endpoint."""

    def __init__(
        self,
        *,
        worker_id: str,
        secret: bytes,
        python_executable: str | Path | None = None,
        package_roots: Sequence[str | Path] = (),
        startup_timeout_seconds: float = 10.0,
        request_timeout_seconds: float = 10.0,
        enabled: bool = True,
    ) -> None:
        if len(secret) < 24:
            raise ValueError("edge connector secret must contain at least 24 bytes")
        self.worker_id = worker_id
        self.secret = bytes(secret)
        self.python_executable = str(python_executable or sys.executable)
        self.package_roots = tuple(str(Path(item).resolve()) for item in package_roots)
        self.startup_timeout_seconds = float(startup_timeout_seconds)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.enabled = bool(enabled)
        self._process: subprocess.Popen[str] | None = None
        self._endpoint: EdgeProcessEndpoint | None = None
        self._stderr_lines: list[str] = []
        self._lock = threading.RLock()

    @property
    def endpoint(self) -> EdgeProcessEndpoint | None:
        return self._endpoint

    @property
    def running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None and self._endpoint is not None

    def start(self) -> EdgeProcessEndpoint:
        if not self.enabled:
            raise EdgeProcessError("edge connector is disabled; no local fallback is permitted")
        with self._lock:
            if self.running:
                return self._endpoint  # type: ignore[return-value]
            environment = os.environ.copy()
            environment["ZYRA_EDGE_PROCESS_SECRET"] = self.secret.hex()
            paths = [*self.package_roots]
            current_path = environment.get("PYTHONPATH", "")
            if current_path:
                paths.append(current_path)
            if paths:
                environment["PYTHONPATH"] = os.pathsep.join(paths)
            command = [
                self.python_executable,
                "-m",
                "zyra_workers.edge_pool.server",
                "--host",
                "127.0.0.1",
                "--port",
                "0",
                "--worker-id",
                self.worker_id,
            ]
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
                creationflags=_hidden_window_creation_flags(),
            )
            output_queue: queue.Queue[str | None] = queue.Queue()
            threading.Thread(
                target=_read_first_line,
                args=(self._process.stdout, output_queue),
                daemon=True,
            ).start()
            threading.Thread(
                target=self._collect_stderr,
                args=(self._process.stderr,),
                daemon=True,
            ).start()
            try:
                line = output_queue.get(timeout=max(0.1, self.startup_timeout_seconds))
            except queue.Empty as error:
                self._terminate_process()
                raise EdgeProcessError("edge worker did not publish a ready endpoint before timeout") from error
            if not line:
                code = self._process.poll()
                detail = "\n".join(self._stderr_lines[-10:])
                self._terminate_process()
                raise EdgeProcessError(f"edge worker exited before readiness (code={code}): {detail}")
            try:
                ready = json.loads(line)
            except json.JSONDecodeError as error:
                self._terminate_process()
                raise EdgeProcessError("edge worker readiness record is not JSON") from error
            if not isinstance(ready, Mapping) or not ready.get("ready"):
                self._terminate_process()
                raise EdgeProcessError(str(getattr(ready, "get", lambda *_: "readiness rejected")("error")))
            endpoint = EdgeProcessEndpoint(
                worker_id=str(ready.get("worker_id") or ""),
                host=str(ready.get("host") or "127.0.0.1"),
                port=int(ready.get("port") or 0),
                endpoint=str(ready.get("endpoint") or ""),
                pid=int(ready.get("pid") or 0),
                protocol_version=int(ready.get("protocol_version") or 0),
            )
            if endpoint.worker_id != self.worker_id or endpoint.port <= 0:
                self._terminate_process()
                raise EdgeProcessError("edge worker readiness identity is invalid")
            if endpoint.protocol_version != PROTOCOL_VERSION:
                self._terminate_process()
                raise EdgeProcessError("edge worker protocol version is incompatible")
            self._endpoint = endpoint
            return endpoint

    def attest(self, *, manifest_digest: str, challenge_nonce: str) -> EdgeAttestationResponse:
        response = self._request(
            EdgeMessageKind.ATTEST,
            {
                "manifest_digest": manifest_digest,
                "challenge_nonce": challenge_nonce,
                "protocol_version": PROTOCOL_VERSION,
            },
        )
        attestation = EdgeAttestationResponse.from_mapping(response.payload)
        endpoint = self._require_endpoint()
        if attestation.endpoint != endpoint.endpoint or attestation.pid != endpoint.pid:
            raise EdgeProcessError("edge attestation identity differs from ready endpoint")
        self._endpoint = EdgeProcessEndpoint(
            **{**endpoint.to_dict(), "process_identity": attestation.process_identity}
        )
        return attestation

    def heartbeat(self) -> Mapping[str, Any]:
        return dict(self._request(EdgeMessageKind.HEARTBEAT, {}).payload)

    def execute(
        self,
        *,
        job_id: str,
        task_id: str,
        attempt_id: str,
        lease_id: str,
        fence_epoch: int,
        fence_token_digest: str,
        operation: str,
        input_payload: Mapping[str, Any],
        timeout_seconds: float | None = None,
    ) -> EdgeExecutionResult:
        response = self._request(
            EdgeMessageKind.EXECUTE,
            {
                "job_id": job_id,
                "task_id": task_id,
                "attempt_id": attempt_id,
                "lease_id": lease_id,
                "fence_epoch": int(fence_epoch),
                "fence_token_digest": fence_token_digest,
                "operation": operation,
                "input": dict(input_payload),
            },
            timeout_seconds=timeout_seconds,
        )
        result = EdgeExecutionResult.from_mapping(response.payload)
        if result.lease_id != lease_id or result.attempt_id != attempt_id or result.fence_epoch != fence_epoch:
            raise EdgeProcessError("edge execution response failed lease fence correlation")
        return result

    def cancel(self, *, job_id: str = "", lease_id: str = "", reason: str = "") -> bool:
        response = self._request(
            EdgeMessageKind.CANCEL,
            {"job_id": job_id, "lease_id": lease_id, "reason": reason},
        )
        return bool(response.payload.get("changed"))

    def drain(self) -> bool:
        return bool(self._request(EdgeMessageKind.DRAIN, {}).payload.get("draining"))

    def wake(self) -> bool:
        response = self._request(EdgeMessageKind.WAKE, {})
        return not bool(response.payload.get("draining"))

    def stop(self, *, graceful: bool = True) -> None:
        with self._lock:
            if self._process is None:
                return
            if graceful and self.running:
                try:
                    self._request(EdgeMessageKind.STOP, {}, timeout_seconds=2.0)
                except Exception:
                    pass
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._terminate_process()
            finally:
                self._process = None
                self._endpoint = None

    def kill(self) -> None:
        with self._lock:
            self._terminate_process()
            self._process = None
            self._endpoint = None

    def _request(
        self,
        kind: EdgeMessageKind,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> EdgeFrame:
        if not self.enabled:
            raise EdgeProcessError("edge connector is disabled; edge execution has no fallback")
        endpoint = self._require_endpoint()
        process = self._process
        if process is None or process.poll() is not None:
            raise EdgeProcessError("edge process is not running")
        try:
            return request(
                host=endpoint.host,
                port=endpoint.port,
                frame=EdgeFrame(kind=kind, worker_id=self.worker_id, payload=dict(payload)),
                secret=self.secret,
                timeout_seconds=float(timeout_seconds or self.request_timeout_seconds),
            )
        except (OSError, EdgeProtocolError) as error:
            raise EdgeProcessError(f"edge protocol request failed: {error}") from error

    def _require_endpoint(self) -> EdgeProcessEndpoint:
        if self._endpoint is None:
            raise EdgeProcessError("edge process connector has not been started")
        return self._endpoint

    def _collect_stderr(self, stream: Any) -> None:
        if stream is None:
            return
        for line in stream:
            self._stderr_lines.append(line.rstrip())
            if len(self._stderr_lines) > 200:
                del self._stderr_lines[:100]

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2.0)

    def __enter__(self) -> "EdgeWorkerProcessConnector":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.stop()


def _read_first_line(stream: Any, output: queue.Queue[str | None]) -> None:
    if stream is None:
        output.put(None)
        return
    output.put(stream.readline())


def _hidden_window_creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0
