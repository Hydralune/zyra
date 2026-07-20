from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import WorkspaceIdentity, stable_digest


class CodeIndexWorkerProcessError(RuntimeError):
    def __init__(self, code: str, message: str, *, returncode: int | None = None, stderr: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class CodeIndexProcessReceipt:
    operation: str
    worker_id: str
    workspace_id: str
    source_revision: str
    command_digest: str
    pid: int
    started_at: float
    completed_at: float
    returncode: int
    outcomes: tuple[Mapping[str, Any], ...] = ()
    sweep: Mapping[str, Any] | None = None
    stderr_tail: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "worker_id": self.worker_id,
            "workspace_id": self.workspace_id,
            "source_revision": self.source_revision,
            "command_digest": self.command_digest,
            "pid": self.pid,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": max(0.0, (self.completed_at - self.started_at) * 1000.0),
            "returncode": self.returncode,
            "ok": self.ok,
            "outcomes": [dict(value) for value in self.outcomes],
            "sweep": dict(self.sweep or {}),
            "stderr_tail": self.stderr_tail,
            "runtime": "zyra_code_index.worker_cli",
            "external_source_process": False,
            "physical_workspace_root_persisted": False,
        }


class CodeIndexWorkerProcessSupervisor:
    """Launch the package worker and return a terminal, auditable receipt."""

    def __init__(
        self,
        *,
        identity: WorkspaceIdentity,
        index_db: str | Path,
        workspace_state_root: str | Path,
        workspace_data_root: str | Path,
        project_root: str | Path | None = None,
        python_executable: str | Path | None = None,
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 5.0,
        timeout_seconds: float = 180.0,
        extra_python_paths: Sequence[str | Path] = (),
    ) -> None:
        self.identity = identity
        self.index_db = Path(index_db).resolve()
        self.workspace_state_root = Path(workspace_state_root).resolve()
        self.workspace_data_root = Path(workspace_data_root).resolve()
        self.project_root = Path(project_root or Path.cwd()).resolve()
        self.python_executable = str(Path(python_executable or sys.executable).resolve())
        self.lease_ttl_seconds = max(0.1, float(lease_ttl_seconds))
        self.heartbeat_interval_seconds = max(0.05, min(float(heartbeat_interval_seconds), self.lease_ttl_seconds / 2))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.extra_python_paths = tuple(Path(value).resolve() for value in extra_python_paths)

    def drain(self, *, maximum_jobs: int = 64) -> CodeIndexProcessReceipt:
        if maximum_jobs < 1 or maximum_jobs > 10_000:
            raise ValueError("maximum_jobs must be between 1 and 10000")
        return self._execute("drain", ("--drain", str(maximum_jobs)))

    def process_one(self) -> CodeIndexProcessReceipt:
        return self._execute("process_one", ("--once",))

    def sweep_once(self) -> CodeIndexProcessReceipt:
        return self._execute("sweep_once", ("--sweep-once",))

    def status(self) -> CodeIndexProcessReceipt:
        return self._execute("status", ("--status",))

    def _execute(self, operation: str, arguments: Sequence[str]) -> CodeIndexProcessReceipt:
        worker_id = f"code-index:{operation}:{stable_digest(time.time_ns(), os.getpid())[:12]}"
        command = [
            self.python_executable,
            "-m",
            "zyra_code_index.worker_cli",
            "--index-db",
            str(self.index_db),
            "--workspace-state-root",
            str(self.workspace_state_root),
            "--workspace-data-root",
            str(self.workspace_data_root),
            "--workspace-id",
            self.identity.workspace_id,
            "--source-revision",
            self.identity.revision,
            "--backend-id",
            self.identity.backend_id or "local-default",
            "--worker-id",
            worker_id,
            "--lease-ttl",
            str(self.lease_ttl_seconds),
            "--heartbeat-interval",
            str(self.heartbeat_interval_seconds),
            *arguments,
        ]
        started = time.time()
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=self._environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise CodeIndexWorkerProcessError("code_index_worker_start_failed", str(error)) from error
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            process.kill()
            stdout, stderr = process.communicate(timeout=10.0)
            raise CodeIndexWorkerProcessError(
                "code_index_worker_timeout",
                f"code index worker exceeded {self.timeout_seconds:.1f}s",
                returncode=process.returncode,
                stderr=self._tail(stderr),
            ) from error
        completed = time.time()
        parsed = self._parse(stdout, operation)
        outcomes: tuple[Mapping[str, Any], ...] = ()
        sweep: Mapping[str, Any] | None = None
        if operation == "drain":
            if not isinstance(parsed, list):
                raise CodeIndexWorkerProcessError("code_index_worker_protocol", "drain did not return an array")
            outcomes = tuple(dict(value) for value in parsed if isinstance(value, Mapping))
        elif operation == "sweep_once":
            if not isinstance(parsed, Mapping):
                raise CodeIndexWorkerProcessError("code_index_worker_protocol", "sweeper did not return an object")
            sweep = dict(parsed)
        else:
            if not isinstance(parsed, Mapping):
                raise CodeIndexWorkerProcessError("code_index_worker_protocol", f"{operation} did not return an object")
            outcomes = (dict(parsed),)
        receipt = CodeIndexProcessReceipt(
            operation=operation,
            worker_id=worker_id,
            workspace_id=self.identity.workspace_id,
            source_revision=self.identity.revision,
            command_digest=stable_digest("zyra_code_index.worker_cli", self.index_db.name, operation, self.identity.revision),
            pid=process.pid,
            started_at=started,
            completed_at=completed,
            returncode=int(process.returncode or 0),
            outcomes=outcomes,
            sweep=sweep,
            stderr_tail=self._tail(stderr),
        )
        if process.returncode != 0:
            raise CodeIndexWorkerProcessError(
                "code_index_worker_failed",
                f"code index worker failed with exit code {process.returncode}",
                returncode=process.returncode,
                stderr=receipt.stderr_tail,
            )
        return receipt

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        candidates = [
            self.project_root / "packages" / "code_index",
            self.project_root / "packages" / "workspace",
            self.project_root / "packages" / "core",
            *self.extra_python_paths,
        ]
        candidates.extend(Path(value) for value in environment.get("PYTHONPATH", "").split(os.pathsep) if value)
        ordered: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            resolved = str(candidate.resolve())
            key = resolved.casefold() if os.name == "nt" else resolved
            if key not in seen:
                seen.add(key)
                ordered.append(resolved)
        environment["PYTHONPATH"] = os.pathsep.join(ordered)
        return environment

    @staticmethod
    def _parse(stdout: str, operation: str) -> Any:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise CodeIndexWorkerProcessError("code_index_worker_protocol", f"{operation} returned no JSON")
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as error:
            raise CodeIndexWorkerProcessError("code_index_worker_protocol", f"{operation} returned invalid JSON") from error

    @staticmethod
    def _tail(value: str, limit: int = 4_000) -> str:
        return value[-limit:] if len(value) > limit else value


__all__ = ["CodeIndexProcessReceipt", "CodeIndexWorkerProcessError", "CodeIndexWorkerProcessSupervisor"]
