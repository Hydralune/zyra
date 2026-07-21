from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import platform
import socketserver
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .protocol import (
    PROTOCOL_VERSION,
    EdgeFrame,
    EdgeMessageKind,
    EdgeProtocolError,
    read_frame,
    write_frame,
)


@dataclass(slots=True)
class EdgeJob:
    job_id: str
    task_id: str
    attempt_id: str
    lease_id: str
    fence_epoch: int
    fence_token_digest: str
    started_at: float = field(default_factory=time.monotonic)
    cancelled: threading.Event = field(default_factory=threading.Event)
    completed: bool = False


class EdgeWorkerState:
    def __init__(self, *, worker_id: str, secret: bytes, endpoint: str) -> None:
        self.worker_id = worker_id
        self.secret = secret
        self.endpoint = endpoint
        self.process_identity = f"pid-{os.getpid()}-{uuid4().hex[:12]}"
        self.started_at = time.monotonic()
        self.sequence = 0
        self.draining = False
        self.stopping = False
        self._jobs: dict[str, EdgeJob] = {}
        self._lock = threading.RLock()
        self._seen_nonces: set[str] = set()

    def accept_nonce(self, nonce: str) -> None:
        with self._lock:
            if nonce in self._seen_nonces:
                raise EdgeProtocolError("edge protocol nonce replay detected")
            self._seen_nonces.add(nonce)
            if len(self._seen_nonces) > 10000:
                self._seen_nonces = set(sorted(self._seen_nonces)[-5000:])

    def attest(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        manifest_digest = str(payload.get("manifest_digest") or "")
        challenge = str(payload.get("challenge_nonce") or "")
        protocol_version = int(payload.get("protocol_version") or 0)
        if len(manifest_digest) != 64 or not challenge:
            raise EdgeProtocolError("attestation request is incomplete")
        if protocol_version != PROTOCOL_VERSION:
            raise EdgeProtocolError("attestation protocol version mismatch")
        message = "\n".join(
            (
                self.worker_id,
                self.process_identity,
                manifest_digest,
                challenge,
                self.endpoint,
                str(protocol_version),
            )
        ).encode("utf-8")
        response = hmac.new(self.secret, message, hashlib.sha256).hexdigest()
        return {
            "process_identity": self.process_identity,
            "manifest_digest": manifest_digest,
            "challenge_nonce": challenge,
            "response_digest": response,
            "protocol_version": PROTOCOL_VERSION,
            "endpoint": self.endpoint,
            "pid": os.getpid(),
            "platform": platform.platform(),
        }

    def heartbeat(self) -> Mapping[str, Any]:
        with self._lock:
            self.sequence += 1
            jobs = tuple(job for job in self._jobs.values() if not job.completed)
        uptime_ms = int((time.monotonic() - self.started_at) * 1000)
        return {
            "sequence": self.sequence,
            "active_lease_ids": sorted({job.lease_id for job in jobs}),
            "active_attempt_ids": sorted({job.attempt_id for job in jobs}),
            "queue_depth": 0,
            "process_uptime_ms": uptime_ms,
            "load_average": _load_average(),
            "pid": os.getpid(),
            "draining": self.draining,
            "stopping": self.stopping,
        }

    def execute(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        if self.draining or self.stopping:
            raise EdgeProtocolError("edge worker is draining and rejects new execution")
        job_id = str(payload.get("job_id") or f"edge_job_{uuid4().hex}")
        task_id = str(payload.get("task_id") or "")
        attempt_id = str(payload.get("attempt_id") or "")
        lease_id = str(payload.get("lease_id") or "")
        fence_epoch = int(payload.get("fence_epoch") or 0)
        fence_token_digest = str(payload.get("fence_token_digest") or "")
        if not all((task_id, attempt_id, lease_id, fence_token_digest)) or fence_epoch < 1:
            raise EdgeProtocolError("edge execution is missing lease fence identity")
        with self._lock:
            existing = self._jobs.get(job_id)
            if existing is not None and not existing.completed:
                raise EdgeProtocolError("edge job id is already running")
            job = EdgeJob(
                job_id=job_id,
                task_id=task_id,
                attempt_id=attempt_id,
                lease_id=lease_id,
                fence_epoch=fence_epoch,
                fence_token_digest=fence_token_digest,
            )
            self._jobs[job_id] = job
        try:
            operation = str(payload.get("operation") or "hash_artifact")
            input_payload = payload.get("input")
            if not isinstance(input_payload, Mapping):
                input_payload = {}
            if operation == "wait":
                duration_ms = max(0, min(60000, int(input_payload.get("duration_ms") or 0)))
                deadline = time.monotonic() + duration_ms / 1000.0
                while time.monotonic() < deadline:
                    if job.cancelled.wait(timeout=min(0.05, max(0.0, deadline - time.monotonic()))):
                        return self._cancelled_result(job)
                content = json.dumps({"waited_ms": duration_ms}, sort_keys=True).encode("utf-8")
            elif operation == "echo_artifact":
                content = str(input_payload.get("content") or "").encode("utf-8")
            elif operation == "hash_artifact":
                raw = str(input_payload.get("content") or "").encode("utf-8")
                content = hashlib.sha256(raw).hexdigest().encode("ascii")
            elif operation == "json_transform":
                source = input_payload.get("value")
                content = json.dumps(
                    {"value": source, "digest": hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()},
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            else:
                raise EdgeProtocolError(f"edge operation is not allowed: {operation}")
            if job.cancelled.is_set():
                return self._cancelled_result(job)
            return {
                "job_id": job.job_id,
                "task_id": job.task_id,
                "attempt_id": job.attempt_id,
                "lease_id": job.lease_id,
                "fence_epoch": job.fence_epoch,
                "outcome": "succeeded",
                "artifact": {
                    "encoding": "base64",
                    "content": base64.b64encode(content).decode("ascii"),
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                },
            }
        finally:
            with self._lock:
                job.completed = True

    def cancel(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        job_id = str(payload.get("job_id") or "")
        lease_id = str(payload.get("lease_id") or "")
        changed: list[str] = []
        with self._lock:
            for job in self._jobs.values():
                if job.completed:
                    continue
                if (job_id and job.job_id == job_id) or (lease_id and job.lease_id == lease_id):
                    job.cancelled.set()
                    changed.append(job.job_id)
        return {"cancelled_job_ids": sorted(changed), "changed": bool(changed)}

    def _cancelled_result(self, job: EdgeJob) -> Mapping[str, Any]:
        return {
            "job_id": job.job_id,
            "task_id": job.task_id,
            "attempt_id": job.attempt_id,
            "lease_id": job.lease_id,
            "fence_epoch": job.fence_epoch,
            "outcome": "cancelled",
            "artifact": None,
        }


class EdgeRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        state: EdgeWorkerState = self.server.state  # type: ignore[attr-defined]
        try:
            frame = read_frame(self.rfile)
            frame.verify(state.secret)
            if frame.worker_id != state.worker_id:
                raise EdgeProtocolError("edge frame addresses another worker")
            state.accept_nonce(frame.nonce)
            payload = self._dispatch(state, frame)
            response = EdgeFrame(
                kind=EdgeMessageKind.RESPONSE,
                worker_id=state.worker_id,
                payload=payload,
                request_id=frame.request_id,
            ).sign(state.secret)
        except Exception as error:
            worker_id = getattr(locals().get("frame"), "worker_id", state.worker_id)
            request_id = getattr(locals().get("frame"), "request_id", f"edge_request_{uuid4().hex}")
            response = EdgeFrame(
                kind=EdgeMessageKind.ERROR,
                worker_id=worker_id,
                payload={"error_type": type(error).__name__, "message": str(error)},
                request_id=request_id,
            ).sign(state.secret)
        write_frame(self.wfile, response)
        if locals().get("frame") is not None and frame.kind is EdgeMessageKind.STOP:
            threading.Thread(target=state_server_shutdown, args=(self.server,), daemon=True).start()

    @staticmethod
    def _dispatch(state: EdgeWorkerState, frame: EdgeFrame) -> Mapping[str, Any]:
        if frame.kind is EdgeMessageKind.ATTEST:
            return state.attest(frame.payload)
        if frame.kind is EdgeMessageKind.HEARTBEAT:
            return state.heartbeat()
        if frame.kind is EdgeMessageKind.EXECUTE:
            return state.execute(frame.payload)
        if frame.kind is EdgeMessageKind.CANCEL:
            return state.cancel(frame.payload)
        if frame.kind is EdgeMessageKind.DRAIN:
            state.draining = True
            return {"draining": True}
        if frame.kind is EdgeMessageKind.WAKE:
            if not state.stopping:
                state.draining = False
            return {"draining": state.draining, "stopping": state.stopping}
        if frame.kind is EdgeMessageKind.STOP:
            state.stopping = True
            state.draining = True
            return {"stopping": True}
        raise EdgeProtocolError(f"edge request kind is unsupported: {frame.kind.value}")


class ThreadingEdgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], state: EdgeWorkerState | None = None) -> None:
        super().__init__(address, EdgeRequestHandler)
        self.state = state


def state_server_shutdown(server: socketserver.BaseServer) -> None:
    time.sleep(0.05)
    server.shutdown()


def run_server(*, host: str, port: int, worker_id: str, secret: bytes) -> int:
    with ThreadingEdgeServer((host, port)) as server:
        actual_host, actual_port = server.server_address[:2]
        endpoint = f"tcp://{actual_host}:{actual_port}"
        server.state = EdgeWorkerState(worker_id=worker_id, secret=secret, endpoint=endpoint)
        print(
            json.dumps(
                {
                    "ready": True,
                    "worker_id": worker_id,
                    "host": actual_host,
                    "port": actual_port,
                    "endpoint": endpoint,
                    "pid": os.getpid(),
                    "protocol_version": PROTOCOL_VERSION,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.05)
    return 0


def _load_average() -> float:
    try:
        return max(0.0, float(os.getloadavg()[0]))
    except (AttributeError, OSError):
        return 0.0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zyra independent edge worker protocol process")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--worker-id", required=True)
    args = parser.parse_args(argv)
    secret_hex = os.environ.get("ZYRA_EDGE_PROCESS_SECRET", "")
    if len(secret_hex) < 48:
        print(json.dumps({"ready": False, "error": "missing edge process secret"}), flush=True)
        return 2
    try:
        secret = bytes.fromhex(secret_hex)
    except ValueError:
        return 2
    return run_server(host=args.host, port=args.port, worker_id=args.worker_id, secret=secret)


if __name__ == "__main__":
    raise SystemExit(main())
