from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import CommandBudget, GatewayCommandEnvelope, ProcessTermination
from .process_budget import OutputBudgetCollector
from .process_tree import ProcessTreeController
from .redaction import SecretRedactor
from .integration_models import canonical_value, content_digest, stable_identifier
from .integration_policy import GatewayPolicyRuntime


@dataclass(frozen=True, slots=True)
class HostProcessReceipt:
    receipt_id: str
    command_id: str
    command_digest: str
    policy_digest: str
    termination: ProcessTermination
    return_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    started_at: float
    finished_at: float
    process_id: int | None = None
    failure_code: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.termination == ProcessTermination.EXITED and self.return_code == 0

    def safe_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        value = {
            "receipt_id": self.receipt_id,
            "command_id": self.command_id,
            "command_digest": self.command_digest,
            "policy_digest": self.policy_digest,
            "termination": self.termination.value,
            "return_code": self.return_code,
            "stdout_digest": content_digest(self.stdout),
            "stderr_digest": content_digest(self.stderr),
            "stdout_bytes": len(self.stdout),
            "stderr_bytes": len(self.stderr),
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "process_id": self.process_id,
            "failure_code": self.failure_code,
            "metadata": canonical_value(self.metadata),
        }
        if include_output:
            value["stdout"] = self.stdout.decode("utf-8", errors="replace")
            value["stderr"] = self.stderr.decode("utf-8", errors="replace")
        return value


class GatewayHostProcessRuntime:
    """Structured process port for Zyra-owned control executables.

    This is not a second sandbox lifecycle.  It is the bounded host-control
    port used only for productized Zyra executables that cannot be copied into
    a task workspace, such as the TypeScript CodeWorker stdio entrypoint.
    """

    def __init__(
        self,
        policy_runtime: GatewayPolicyRuntime,
        *,
        redactor: SecretRedactor | None = None,
        allowed_roots: Sequence[str | Path] = (),
    ) -> None:
        self.policy_runtime = policy_runtime
        self.redactor = redactor or SecretRedactor()
        self.allowed_roots = tuple(Path(item).resolve() for item in allowed_roots)
        self.process_tree = ProcessTreeController()
        self._lock = threading.RLock()
        self._active: dict[str, subprocess.Popen[bytes]] = {}

    def run(
        self,
        *,
        executable: str,
        argv: Sequence[str],
        cwd: str | Path,
        timeout_seconds: float = 30.0,
        stdout_limit_bytes: int = 2 * 1024 * 1024,
        stderr_limit_bytes: int = 2 * 1024 * 1024,
        environment: Mapping[str, str] | None = None,
        operation_name: str = "zyra-control",
    ) -> HostProcessReceipt:
        root = Path(cwd).resolve()
        self._assert_root(root)
        command_id = stable_identifier(
            "gateway-host-command",
            str(root),
            executable,
            tuple(argv),
            time.time_ns(),
        )
        envelope = GatewayCommandEnvelope(
            command_id=command_id,
            session_id=stable_identifier("gateway-host-session", str(root), operation_name),
            run_id=stable_identifier("gateway-host-run", str(root), operation_name),
            task_id=stable_identifier("gateway-host-task", str(root), operation_name),
            worker_id="CodeWorkerControl",
            executable=str(executable),
            argv=tuple(str(item) for item in argv),
            cwd=".",
            environment=self.policy_runtime.assert_environment(environment or {}),
            budget=CommandBudget(
                timeout_seconds=max(0.1, min(300.0, float(timeout_seconds))),
                stdout_limit_bytes=max(1024, stdout_limit_bytes),
                stderr_limit_bytes=max(1024, stderr_limit_bytes),
                combined_output_limit_bytes=max(2048, stdout_limit_bytes + stderr_limit_bytes),
                max_processes=8,
            ),
            tool_use_id=command_id,
            metadata={"host_control": True, "operation_name": operation_name},
        )
        policy = self.policy_runtime.evaluate_command(envelope)
        if policy.hard_denied:
            raise RuntimeError(f"host control command denied: {policy.reason}")
        started = time.time()
        process: subprocess.Popen[bytes] | None = None
        termination = ProcessTermination.FAILED_TO_START
        return_code: int | None = None
        failure_code = ""
        try:
            creationflags = 0
            start_new_session = os.name != "nt"
            if os.name == "nt":
                creationflags = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            process = subprocess.Popen(
                [str(executable), *(str(item) for item in argv)],
                cwd=root,
                env={**_safe_base_environment(), **dict(environment or {})},
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=start_new_session,
                creationflags=creationflags,
            )
            with self._lock:
                self._active[command_id] = process
            try:
                stdout, stderr = process.communicate(timeout=envelope.budget.timeout_seconds)
                return_code = process.returncode
                termination = ProcessTermination.EXITED
            except subprocess.TimeoutExpired:
                self.process_tree.terminate(
                    process,
                    grace_seconds=envelope.budget.cancel_grace_seconds,
                )
                stdout, stderr = process.communicate()
                return_code = process.returncode
                termination = ProcessTermination.TIMED_OUT
                failure_code = "host_process_timeout"
        except OSError as error:
            stdout = b""
            stderr = str(error).encode("utf-8", errors="replace")
            failure_code = "host_process_start_failed"
        finally:
            with self._lock:
                self._active.pop(command_id, None)
        stdout, stdout_truncated = _bounded(stdout, envelope.budget.stdout_limit_bytes)
        stderr, stderr_truncated = _bounded(stderr, envelope.budget.stderr_limit_bytes)
        stdout, stderr, findings = self._redact(stdout, stderr)
        finished = time.time()
        return HostProcessReceipt(
            receipt_id=stable_identifier(
                "gateway-host-receipt",
                command_id,
                return_code,
                content_digest(stdout),
                content_digest(stderr),
            ),
            command_id=command_id,
            command_digest=content_digest(envelope.permission_material()),
            policy_digest=policy.policy_digest,
            termination=termination,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            started_at=started,
            finished_at=finished,
            process_id=process.pid if process is not None else None,
            failure_code=failure_code,
            metadata={
                "shell": False,
                "structured_argv": True,
                "process_tree_controlled": True,
                "redaction_findings": len(findings),
                "operation_name": operation_name,
            },
        )

    def cancel(self, command_id: str, *, reason: str = "control cancellation") -> bool:
        with self._lock:
            process = self._active.get(command_id)
        if process is None:
            return False
        self.process_tree.terminate(process, grace_seconds=3.0)
        return True

    def active_commands(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._active)

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime": "GatewayHostProcessRuntime",
            "allowed_root_digests": [content_digest(str(item)) for item in self.allowed_roots],
            "active_commands": len(self.active_commands()),
            "shell": False,
            "structured_argv": True,
            "bounded_output": True,
            "process_tree_controlled": True,
        }

    def _assert_root(self, root: Path) -> None:
        if not self.allowed_roots:
            raise RuntimeError("host process runtime has no allowed root")
        for allowed in self.allowed_roots:
            try:
                root.relative_to(allowed)
                return
            except ValueError:
                continue
        raise RuntimeError("host process working directory is outside allowed roots")

    def _redact(self, stdout: bytes, stderr: bytes) -> tuple[bytes, bytes, tuple[Any, ...]]:
        from .redaction import redact_terminal_output

        return redact_terminal_output(stdout, stderr)


def _bounded(value: bytes, limit: int) -> tuple[bytes, bool]:
    if len(value) <= limit:
        return value, False
    return value[:limit], True


def _safe_base_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_ALL",
        "TERM",
        "NO_COLOR",
        "CI",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


__all__ = [
    "GatewayHostProcessRuntime",
    "HostProcessReceipt",
]
