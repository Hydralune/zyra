from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .canonical import canonical_logical_path, digest
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    GatewayCommandEnvelope,
    GatewaySessionRecord,
    ProcessOutput,
    ProcessResult,
    ProcessTermination,
)
from .process_budget import (
    CancellationToken,
    OutputBudgetCollector,
    ProcessStreamPump,
    StreamChunk,
)
from .process_tree import ProcessTreeController
from .redaction import SecretRedactor, redact_terminal_output


@dataclass(frozen=True, slots=True)
class BackendSession:
    session_id: str
    backend_id: str
    execution_root: Path
    generation: int
    prepared_at: float
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "backend_id": self.backend_id,
            "generation": self.generation,
            "prepared_at": self.prepared_at,
            "execution_root_digest": digest({"path": str(self.execution_root)}),
            "metadata": dict(self.metadata),
        }


class SandboxBackend(Protocol):
    backend_id: str

    def prepare(self, record: GatewaySessionRecord) -> BackendSession:
        ...

    def execute(
        self,
        session: BackendSession,
        envelope: GatewayCommandEnvelope,
        cancellation: CancellationToken,
        *,
        on_chunk: Callable[[StreamChunk], None] | None = None,
    ) -> ProcessResult:
        ...

    def cleanup(self, session: BackendSession) -> None:
        ...

    def cancel(self, command_id: str, reason: str) -> bool:
        ...


class LocalProcessSandboxBackend:
    """Credential-free local isolation root; workspace deltas are imported later."""

    backend_id = "zyra.local-process-sandbox.v1"

    def __init__(
        self,
        state_root: str | Path,
        *,
        base_environment: Mapping[str, str] | None = None,
        redactor: SecretRedactor | None = None,
        preserve_failed_roots: bool = True,
    ) -> None:
        self.state_root = Path(state_root).resolve()
        self.session_root = self.state_root / "sandbox-sessions"
        self.session_root.mkdir(parents=True, exist_ok=True)
        self.base_environment = dict(base_environment or self._default_environment())
        self.redactor = redactor or SecretRedactor()
        self.preserve_failed_roots = bool(preserve_failed_roots)
        self.tree = ProcessTreeController()
        self._sessions: dict[str, BackendSession] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    def prepare(self, record: GatewaySessionRecord) -> BackendSession:
        with self._lock:
            existing = self._sessions.get(record.session_id)
            if existing is not None and existing.execution_root.exists():
                return existing
            root = Path(
                tempfile.mkdtemp(
                    prefix=f"{record.session_id[:24]}-",
                    dir=self.session_root,
                )
            ).resolve()
            self._assert_under_session_root(root)
            session = BackendSession(
                session_id=record.session_id,
                backend_id=self.backend_id,
                execution_root=root,
                generation=record.generation,
                prepared_at=time.time(),
                metadata={
                    "workspace_direct_write": False,
                    "credential_inheritance": False,
                    "cleanup_owner": self.backend_id,
                },
            )
            self._sessions[record.session_id] = session
            return session

    def execute(
        self,
        session: BackendSession,
        envelope: GatewayCommandEnvelope,
        cancellation: CancellationToken,
        *,
        on_chunk: Callable[[StreamChunk], None] | None = None,
    ) -> ProcessResult:
        self._validate_session(session, envelope)
        cwd = self._resolve_cwd(session, envelope.cwd)
        environment = dict(self.base_environment)
        environment.update(envelope.environment)
        started = time.time()
        process: subprocess.Popen[bytes] | None = None
        collector = OutputBudgetCollector(envelope.budget, on_chunk=on_chunk)
        try:
            process = subprocess.Popen(
                [envelope.executable, *envelope.argv],
                cwd=cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                creationflags=self.tree.creation_flags(),
                start_new_session=self.tree.start_new_session(),
            )
        except Exception as error:
            return ProcessResult(
                command_id=envelope.command_id,
                termination=ProcessTermination.FAILED_TO_START,
                return_code=None,
                started_at=started,
                finished_at=time.time(),
                output=ProcessOutput(stderr=str(error).encode("utf-8")),
                backend_id=self.backend_id,
                error_code=GatewayErrorCode.PROCESS_START_FAILED.value,
                metadata={"error_type": type(error).__name__},
            )
        with self._lock:
            self._processes[envelope.command_id] = process
        assert process.stdout is not None
        assert process.stderr is not None
        pump = ProcessStreamPump(process.stdout, process.stderr, collector)
        pump.start()
        deadline = time.monotonic() + envelope.budget.timeout_seconds
        termination = ProcessTermination.EXITED
        cancellation_reason = ""
        tree_result: Mapping[str, Any] = {}
        try:
            while process.poll() is None:
                state = pump.drain(timeout_seconds=0.025)
                if cancellation.cancelled:
                    termination = ProcessTermination.CANCELLED
                    cancellation_reason = cancellation.reason
                    tree_result = self.tree.terminate(
                        process,
                        grace_seconds=envelope.budget.cancel_grace_seconds,
                        reason=cancellation_reason,
                    ).to_dict()
                    break
                if state.exceeded:
                    termination = ProcessTermination.OUTPUT_LIMIT
                    cancellation_reason = "process output budget exceeded"
                    tree_result = self.tree.terminate(
                        process,
                        grace_seconds=envelope.budget.cancel_grace_seconds,
                        reason=cancellation_reason,
                    ).to_dict()
                    break
                if time.monotonic() >= deadline:
                    termination = ProcessTermination.TIMED_OUT
                    cancellation_reason = "process deadline exceeded"
                    tree_result = self.tree.terminate(
                        process,
                        grace_seconds=envelope.budget.cancel_grace_seconds,
                        reason=cancellation_reason,
                    ).to_dict()
                    break
                time.sleep(0.01)
            raw_output = pump.finish(
                timeout_seconds=max(1.0, envelope.budget.cancel_grace_seconds)
            )
            stdout, stderr, findings = redact_terminal_output(
                raw_output.stdout,
                raw_output.stderr,
            )
            output = ProcessOutput(
                stdout=stdout,
                stderr=stderr,
                stdout_truncated=raw_output.stdout_truncated,
                stderr_truncated=raw_output.stderr_truncated,
                combined_truncated=raw_output.combined_truncated,
            )
            if process.poll() is None:
                termination = ProcessTermination.TREE_LEAK
                final_tree = self.tree.terminate(
                    process,
                    grace_seconds=envelope.budget.cancel_grace_seconds,
                    reason="final process tree cleanup",
                )
                tree_result = final_tree.to_dict()
            return ProcessResult(
                command_id=envelope.command_id,
                termination=termination,
                return_code=process.poll(),
                started_at=started,
                finished_at=time.time(),
                output=output,
                process_id=process.pid,
                process_group_id=self.tree.process_group_id(process),
                backend_id=self.backend_id,
                cancellation_reason=cancellation_reason,
                error_code=self._error_code(termination),
                metadata={
                    "tree_termination": dict(tree_result),
                    "redaction_findings": [item.to_dict() for item in findings],
                    "shell": False,
                    "execution_root_digest": digest({"path": str(session.execution_root)}),
                },
            )
        finally:
            with self._lock:
                self._processes.pop(envelope.command_id, None)
            try:
                process.stdout.close()
                process.stderr.close()
            except OSError:
                pass

    def cancel(self, command_id: str, reason: str) -> bool:
        with self._lock:
            process = self._processes.get(command_id)
        if process is None:
            return False
        result = self.tree.terminate(process, grace_seconds=1.0, reason=reason)
        return result.stopped

    def cleanup(self, session: BackendSession) -> None:
        with self._lock:
            active = [
                command_id
                for command_id, process in self._processes.items()
                if process.poll() is None
            ]
            if active:
                raise SandboxGatewayError(
                    GatewayErrorCode.PROCESS_TREE_LEAK,
                    "cannot clean sandbox while commands remain active",
                    operation="backend_cleanup",
                    metadata={"active_command_ids": active},
                )
            self._assert_under_session_root(session.execution_root)
            shutil.rmtree(session.execution_root, ignore_errors=False)
            self._sessions.pop(session.session_id, None)

    def get(self, session_id: str) -> BackendSession:
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None or not session.execution_root.exists():
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "backend session is not prepared in this process",
                operation="backend_get",
                retryable=True,
            )
        return session

    def recover(self, record: GatewaySessionRecord) -> BackendSession:
        candidates = sorted(
            self.session_root.glob(f"{record.session_id[:24]}-*"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for candidate in candidates:
            try:
                self._assert_under_session_root(candidate.resolve())
            except SandboxGatewayError:
                continue
            session = BackendSession(
                session_id=record.session_id,
                backend_id=self.backend_id,
                execution_root=candidate.resolve(),
                generation=record.generation,
                prepared_at=candidate.stat().st_ctime,
                metadata={"recovered": True},
            )
            with self._lock:
                self._sessions[record.session_id] = session
            return session
        return self.prepare(record)

    def descriptor(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "backend_id": self.backend_id,
                "sessions": sorted(self._sessions),
                "active_commands": sorted(self._processes),
                "shell": False,
                "workspace_direct_write": False,
                "credential_inheritance": False,
                "process_tree_control": type(self.tree).__name__,
            }

    def _validate_session(
        self,
        session: BackendSession,
        envelope: GatewayCommandEnvelope,
    ) -> None:
        if session.session_id != envelope.session_id:
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_PROTOCOL,
                "command session does not match backend session",
                operation="backend_execute",
            )
        self._assert_under_session_root(session.execution_root)
        if not session.execution_root.is_dir():
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "backend execution root is absent",
                operation="backend_execute",
            )

    def _resolve_cwd(self, session: BackendSession, logical_path: str) -> Path:
        normalized = canonical_logical_path(logical_path, allow_root=True)
        target = (
            session.execution_root
            if normalized == "."
            else session.execution_root.joinpath(*normalized.split("/"))
        ).resolve()
        self._assert_under(session.execution_root, target)
        if not target.exists():
            target.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() or not target.is_dir():
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_INVALID,
                "command cwd must be a real directory beneath the sandbox root",
                operation="backend_execute",
            )
        return target

    def _assert_under_session_root(self, path: Path) -> None:
        self._assert_under(self.session_root, path)

    @staticmethod
    def _assert_under(root: Path, path: Path) -> None:
        try:
            path.relative_to(root.resolve())
        except ValueError as error:
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_ESCAPE,
                "backend path escaped the sandbox root",
                operation="backend_path",
            ) from error

    @staticmethod
    def _default_environment() -> dict[str, str]:
        allowed = {
            "COMSPEC",
            "LANG",
            "LC_ALL",
            "NO_COLOR",
            "PATH",
            "PATHEXT",
            "PYTHONIOENCODING",
            "SYSTEMROOT",
            "TEMP",
            "TERM",
            "TMP",
            "TZ",
            "WINDIR",
        }
        return {
            key: value
            for key, value in os.environ.items()
            if key.upper() in allowed
        }

    @staticmethod
    def _error_code(termination: ProcessTermination) -> str:
        return {
            ProcessTermination.EXITED: "",
            ProcessTermination.FAILED_TO_START: GatewayErrorCode.PROCESS_START_FAILED.value,
            ProcessTermination.TIMED_OUT: GatewayErrorCode.PROCESS_TIMEOUT.value,
            ProcessTermination.CANCELLED: GatewayErrorCode.PROCESS_CANCELLED.value,
            ProcessTermination.OUTPUT_LIMIT: GatewayErrorCode.PROCESS_OUTPUT_LIMIT.value,
            ProcessTermination.KILLED: GatewayErrorCode.PROCESS_CANCELLED.value,
            ProcessTermination.TREE_LEAK: GatewayErrorCode.PROCESS_TREE_LEAK.value,
        }[termination]
