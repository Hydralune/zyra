from __future__ import annotations

import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .backends import BackendSession
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
from .redaction import SecretRedactor


_CONTAINER_REF = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class DockerCliSandboxConnector:
    """Execute gateway commands in one pre-existing Docker container.

    The connector deliberately does not create, stop, or remove the container.
    Its lifecycle remains owned by the external benchmark harness.  Zyra only
    verifies the binding and attaches command processes through ``docker exec``.
    """

    backend_id = "zyra.docker-sandbox.v1"

    def __init__(
        self,
        *,
        container: str,
        workdir: str,
        docker_executable: str | Path | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        container_ref = str(container).strip()
        if not _CONTAINER_REF.fullmatch(container_ref):
            raise ValueError("Docker container reference is invalid")
        normalized_workdir = str(workdir).strip().replace("\\", "/")
        if (
            not normalized_workdir.startswith("/")
            or "\x00" in normalized_workdir
            or any(part == ".." for part in normalized_workdir.split("/"))
        ):
            raise ValueError("Docker workdir must be an absolute container path")
        executable = str(docker_executable or shutil.which("docker") or "docker")
        self.container = container_ref
        self.workdir = posixpath.normpath(normalized_workdir)
        self.docker_executable = executable
        self.redactor = redactor or SecretRedactor()
        self.tree = ProcessTreeController()
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.RLock()

    @property
    def container_ref_digest(self) -> str:
        return digest({"container_ref": self.container})

    def prepare(self, record: GatewaySessionRecord) -> Mapping[str, Any]:
        if record.backend_id != self.backend_id:
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_PROTOCOL,
                "Docker connector received a session for another backend",
                operation="docker_connector_prepare",
            )
        try:
            check = subprocess.run(
                [
                    self.docker_executable,
                    "inspect",
                    "--format={{.State.Running}}",
                    self.container,
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=30.0,
                shell=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "Docker benchmark container could not be inspected",
                operation="docker_connector_prepare",
                retryable=True,
                metadata={"error_type": type(error).__name__},
            ) from error
        running = check.returncode == 0 and check.stdout.strip().lower() == b"true"
        if not running:
            report = self.redactor.redact_bytes(
                check.stderr,
                source="docker_inspect_stderr",
            )
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "Docker benchmark container is not running",
                operation="docker_connector_prepare",
                retryable=True,
                metadata={
                    "container_ref_digest": self.container_ref_digest,
                    "docker_return_code": check.returncode,
                    "stderr": bytes(report.value).decode("utf-8", errors="replace")[:500],
                },
            )
        return {
            "connector": "docker-cli",
            "container_ref_digest": self.container_ref_digest,
            "container_workdir": self.workdir,
            "container_running_verified": True,
            "container_lifecycle_owner": "external-harness",
        }

    def execute(
        self,
        session: BackendSession,
        envelope: GatewayCommandEnvelope,
        cancellation: CancellationToken,
        *,
        on_chunk: Callable[[StreamChunk], None] | None = None,
    ) -> ProcessResult:
        started = time.time()
        if cancellation.cancelled:
            return self._cancelled_result(envelope, started, cancellation.reason)
        collector = OutputBudgetCollector(envelope.budget, on_chunk=on_chunk)
        process: subprocess.Popen[bytes] | None = None
        try:
            process = subprocess.Popen(
                self.command_argv(envelope),
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
                metadata={
                    "error_type": type(error).__name__,
                    "container_ref_digest": self.container_ref_digest,
                },
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
                elif state.exceeded:
                    termination = ProcessTermination.OUTPUT_LIMIT
                    cancellation_reason = "process output budget exceeded"
                elif time.monotonic() >= deadline:
                    termination = ProcessTermination.TIMED_OUT
                    cancellation_reason = "process deadline exceeded"
                if termination is not ProcessTermination.EXITED:
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
            if (
                termination is ProcessTermination.EXITED
                and (
                    raw_output.stdout_truncated
                    or raw_output.stderr_truncated
                    or raw_output.combined_truncated
                )
            ):
                termination = ProcessTermination.OUTPUT_LIMIT
                cancellation_reason = "process output budget exceeded"
            output, findings = self._redact_output(raw_output)
            if process.poll() is None:
                termination = ProcessTermination.TREE_LEAK
                tree_result = self.tree.terminate(
                    process,
                    grace_seconds=envelope.budget.cancel_grace_seconds,
                    reason="final Docker CLI process cleanup",
                ).to_dict()
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
                    "connector": "docker-cli",
                    "container_ref_digest": self.container_ref_digest,
                    "container_workdir": self.resolve_cwd(envelope.cwd),
                    "tree_termination": dict(tree_result),
                    "redaction_findings": [item.to_dict() for item in findings],
                    "shell": False,
                    "container_lifecycle_owner": "external-harness",
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

    def command_argv(self, envelope: GatewayCommandEnvelope) -> list[str]:
        command = [
            self.docker_executable,
            "exec",
            "--workdir",
            self.resolve_cwd(envelope.cwd),
        ]
        for key, value in sorted(envelope.environment.items()):
            command.extend(("--env", f"{key}={value}"))
        command.extend((self.container, envelope.executable, *envelope.argv))
        return command

    def resolve_cwd(self, logical_path: str) -> str:
        normalized = canonical_logical_path(logical_path, allow_root=True)
        if normalized == ".":
            return self.workdir
        target = posixpath.normpath(posixpath.join(self.workdir, normalized))
        if target != self.workdir and not target.startswith(f"{self.workdir}/"):
            raise SandboxGatewayError(
                GatewayErrorCode.PATH_ESCAPE,
                "Docker command cwd escaped the benchmark workdir",
                operation="docker_connector_cwd",
            )
        return target

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
                "cannot release Docker connector while commands remain active",
                operation="docker_connector_cleanup",
                metadata={"active_command_ids": active},
            )

    def cancel(self, command_id: str, reason: str) -> bool:
        with self._lock:
            process = self._processes.get(command_id)
        if process is None:
            return False
        return self.tree.terminate(
            process,
            grace_seconds=1.0,
            reason=reason,
        ).stopped

    def _redact_output(self, output: ProcessOutput) -> tuple[ProcessOutput, tuple[Any, ...]]:
        reports = (
            self.redactor.redact_bytes(output.stdout, source="stdout"),
            self.redactor.redact_bytes(output.stderr, source="stderr"),
            self.redactor.redact_bytes(output.stdout_overflow, source="stdout_overflow"),
            self.redactor.redact_bytes(output.stderr_overflow, source="stderr_overflow"),
        )
        return (
            ProcessOutput(
                stdout=bytes(reports[0].value),
                stderr=bytes(reports[1].value),
                stdout_overflow=bytes(reports[2].value),
                stderr_overflow=bytes(reports[3].value),
                stdout_truncated=output.stdout_truncated,
                stderr_truncated=output.stderr_truncated,
                combined_truncated=output.combined_truncated,
            ),
            tuple(item for report in reports for item in report.findings),
        )

    def _cancelled_result(
        self,
        envelope: GatewayCommandEnvelope,
        started: float,
        reason: str,
    ) -> ProcessResult:
        return ProcessResult(
            command_id=envelope.command_id,
            termination=ProcessTermination.CANCELLED,
            return_code=None,
            started_at=started,
            finished_at=time.time(),
            output=ProcessOutput(),
            backend_id=self.backend_id,
            cancellation_reason=reason,
            error_code=GatewayErrorCode.PROCESS_CANCELLED.value,
            metadata={"container_ref_digest": self.container_ref_digest},
        )

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
