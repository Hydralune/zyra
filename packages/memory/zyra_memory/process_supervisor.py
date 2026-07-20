from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .retrieval_models import stable_digest


class IndexWorkerProcessError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        returncode: int | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class IndexWorkerProcessReceipt:
    operation: str
    worker_id: str
    scope_key: str
    command_digest: str
    pid: int
    started_at: float
    completed_at: float
    returncode: int
    outcomes: tuple[Mapping[str, Any], ...] = ()
    sweep: Mapping[str, Any] = field(default_factory=dict)
    stderr_tail: str = ""

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, (self.completed_at - self.started_at) * 1000.0)

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "worker_id": self.worker_id,
            "scope_key": self.scope_key,
            "command_digest": self.command_digest,
            "pid": self.pid,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "elapsed_ms": self.elapsed_ms,
            "returncode": self.returncode,
            "ok": self.ok,
            "outcomes": [dict(item) for item in self.outcomes],
            "sweep": dict(self.sweep),
            "stderr_tail": self.stderr_tail,
            "runtime": "zyra_memory.index_worker_cli",
            "external_source_process": False,
        }


class MemoryIndexWorkerProcessSupervisor:
    """Supervise the productized durable index-worker process.

    The API/coordinator may enqueue work, but document loading, lease
    heartbeat, staging and fenced publication execute in the package-owned
    worker entrypoint.  ``subprocess.run`` is deliberately not hidden behind a
    best-effort background thread: callers receive a terminal receipt and can
    fail closed when the worker cannot prove publication.
    """

    def __init__(
        self,
        *,
        canonical_db: str | Path,
        index_db: str | Path,
        project_root: str | Path | None = None,
        python_executable: str | Path | None = None,
        worker_id_prefix: str = "memory-index-worker",
        lease_ttl_seconds: float = 30.0,
        heartbeat_interval_seconds: float = 5.0,
        timeout_seconds: float = 120.0,
        extra_python_paths: Sequence[str | Path] = (),
    ) -> None:
        self.canonical_db = Path(canonical_db).expanduser().resolve()
        self.index_db = Path(index_db).expanduser().resolve()
        if self.canonical_db == self.index_db:
            raise ValueError("canonical and derived index databases must differ")
        self.project_root = Path(project_root or Path.cwd()).expanduser().resolve()
        self.python_executable = str(Path(python_executable or sys.executable).resolve())
        self.worker_id_prefix = worker_id_prefix.strip() or "memory-index-worker"
        self.lease_ttl_seconds = max(0.1, float(lease_ttl_seconds))
        self.heartbeat_interval_seconds = max(
            0.05,
            min(float(heartbeat_interval_seconds), self.lease_ttl_seconds / 2),
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.extra_python_paths = tuple(Path(value).resolve() for value in extra_python_paths)

    def process_one(self, *, scope_key: str = "") -> IndexWorkerProcessReceipt:
        worker_id = self._worker_id("once", scope_key)
        return self._execute(
            operation="process_one",
            worker_id=worker_id,
            scope_key=scope_key,
            arguments=("--once",),
        )

    def drain(
        self,
        *,
        maximum_jobs: int = 64,
        scope_key: str = "",
    ) -> IndexWorkerProcessReceipt:
        if maximum_jobs < 1 or maximum_jobs > 10_000:
            raise ValueError("maximum_jobs must be between 1 and 10000")
        worker_id = self._worker_id("drain", scope_key)
        return self._execute(
            operation="drain",
            worker_id=worker_id,
            scope_key=scope_key,
            arguments=("--drain", str(maximum_jobs)),
        )

    def sweep_once(self) -> IndexWorkerProcessReceipt:
        worker_id = self._worker_id("sweep", "")
        return self._execute(
            operation="sweep_once",
            worker_id=worker_id,
            scope_key="",
            arguments=("--sweep-once",),
        )

    def recover_and_drain(
        self,
        *,
        maximum_jobs: int = 64,
        scope_key: str = "",
    ) -> tuple[IndexWorkerProcessReceipt, IndexWorkerProcessReceipt]:
        sweep = self.sweep_once()
        drain = self.drain(maximum_jobs=maximum_jobs, scope_key=scope_key)
        return sweep, drain

    def status(self) -> IndexWorkerProcessReceipt:
        worker_id = self._worker_id("status", "")
        return self._execute(
            operation="status",
            worker_id=worker_id,
            scope_key="",
            arguments=("--status",),
        )

    def _execute(
        self,
        *,
        operation: str,
        worker_id: str,
        scope_key: str,
        arguments: Sequence[str],
    ) -> IndexWorkerProcessReceipt:
        if not self.canonical_db.exists():
            raise IndexWorkerProcessError(
                "canonical_store_missing",
                f"canonical memory store does not exist: {self.canonical_db}",
            )
        command = [
            self.python_executable,
            "-m",
            "zyra_memory.index_worker_cli",
            "--canonical-db",
            str(self.canonical_db),
            "--index-db",
            str(self.index_db),
            "--worker-id",
            worker_id,
            "--lease-ttl",
            str(self.lease_ttl_seconds),
            "--heartbeat-interval",
            str(self.heartbeat_interval_seconds),
            *arguments,
        ]
        if scope_key:
            command.extend(("--scope", scope_key))
        environment = self._environment()
        started_at = time.time()
        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as error:
            raise IndexWorkerProcessError(
                "index_worker_start_failed",
                f"failed to start Zyra memory index worker: {error}",
            ) from error
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            process.kill()
            stdout, stderr = process.communicate(timeout=10.0)
            raise IndexWorkerProcessError(
                "index_worker_timeout",
                f"Zyra memory index worker exceeded {self.timeout_seconds:.1f}s",
                returncode=process.returncode,
                stderr=self._tail(stderr),
            ) from error
        completed_at = time.time()
        parsed = self._parse_output(stdout, operation=operation)
        outcomes: tuple[Mapping[str, Any], ...] = ()
        sweep: Mapping[str, Any] = {}
        if operation == "drain":
            if not isinstance(parsed, list):
                raise IndexWorkerProcessError(
                    "index_worker_protocol_error",
                    "drain worker did not return an outcome array",
                    returncode=process.returncode,
                    stderr=self._tail(stderr),
                )
            outcomes = tuple(dict(item) for item in parsed if isinstance(item, Mapping))
        elif operation in {"process_one", "status"}:
            if not isinstance(parsed, Mapping):
                raise IndexWorkerProcessError(
                    "index_worker_protocol_error",
                    f"{operation} worker did not return an object",
                    returncode=process.returncode,
                    stderr=self._tail(stderr),
                )
            outcomes = (dict(parsed),)
        elif operation == "sweep_once":
            if not isinstance(parsed, Mapping):
                raise IndexWorkerProcessError(
                    "index_worker_protocol_error",
                    "sweeper did not return an object",
                    returncode=process.returncode,
                    stderr=self._tail(stderr),
                )
            sweep = dict(parsed)
        receipt = IndexWorkerProcessReceipt(
            operation=operation,
            worker_id=worker_id,
            scope_key=scope_key,
            command_digest=stable_digest(
                "zyra_memory.index_worker_cli",
                self.canonical_db.name,
                self.index_db.name,
                operation,
                scope_key,
            ),
            pid=process.pid,
            started_at=started_at,
            completed_at=completed_at,
            returncode=int(process.returncode or 0),
            outcomes=outcomes,
            sweep=sweep,
            stderr_tail=self._tail(stderr),
        )
        if process.returncode != 0:
            raise IndexWorkerProcessError(
                "index_worker_failed",
                f"Zyra memory index worker failed with exit code {process.returncode}",
                returncode=process.returncode,
                stderr=receipt.stderr_tail,
            )
        return receipt

    def _environment(self) -> dict[str, str]:
        environment = dict(os.environ)
        candidates = [
            self.project_root / "packages" / "memory",
            self.project_root / "packages" / "core",
            *self.extra_python_paths,
        ]
        existing = environment.get("PYTHONPATH", "")
        if existing:
            candidates.extend(Path(value) for value in existing.split(os.pathsep) if value)
        ordered: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            resolved = str(Path(candidate).resolve())
            key = resolved.casefold() if os.name == "nt" else resolved
            if key in seen:
                continue
            seen.add(key)
            ordered.append(resolved)
        environment["PYTHONPATH"] = os.pathsep.join(ordered)
        return environment

    def _worker_id(self, operation: str, scope_key: str) -> str:
        suffix = stable_digest(operation, scope_key, time.time_ns(), os.getpid())[:12]
        return f"{self.worker_id_prefix}:{operation}:{suffix}"

    @staticmethod
    def _parse_output(stdout: str, *, operation: str) -> Any:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise IndexWorkerProcessError(
                "index_worker_protocol_error",
                f"{operation} worker returned no JSON output",
            )
        try:
            return json.loads(lines[-1])
        except json.JSONDecodeError as error:
            raise IndexWorkerProcessError(
                "index_worker_protocol_error",
                f"{operation} worker returned invalid JSON",
            ) from error

    @staticmethod
    def _tail(value: str, limit: int = 4_000) -> str:
        return value[-limit:] if len(value) > limit else value


__all__ = [
    "IndexWorkerProcessError",
    "IndexWorkerProcessReceipt",
    "MemoryIndexWorkerProcessSupervisor",
]
