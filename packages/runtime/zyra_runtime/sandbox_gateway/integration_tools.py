from __future__ import annotations

import mimetypes
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING

from ..executor import ToolCall, ToolResult
from .artifact_port import FileArtifactRequest
from .canonical import content_digest as gateway_content_digest
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import (
    CommandBudget,
    GatewayCommandEnvelope,
    GatewayEventKind,
    GatewayLifecycleState,
    OperationKind,
    ProcessTermination,
    ProvenanceKind,
    TrustLevel,
)
from .provenance import ArtifactProvenance
from .integration_host import GatewayHostProcessRuntime
from .integration_models import (
    FailureClass,
    GatewayAction,
    GatewayExecutionReceipt,
    GatewayOutcome,
    GatewaySurface,
    RecoveryAction,
    TrustDisposition,
    WorkerGatewayIdentity,
    content_digest,
    invocation_for,
    stable_identifier,
)

if TYPE_CHECKING:
    from .integration_factory import GatewayRuntimeBundle


_HANDLED_TOOLS = frozenset(
    {
        "shell",
        "shell_wait",
        "file_read",
        "file_write",
        "file_edit",
        "file_delete",
        "artifact_write",
    }
)

# A sandboxed command can spend several seconds crossing the host/container
# boundary before the child process even starts.  Ten seconds caused ordinary
# inspections and quick test commands to be reported as background work in
# real Docker-backed tasks, forcing an otherwise unnecessary provider round
# just to poll a command that completed moments later.  Keep the window
# bounded and overridable, but cover that normal isolation overhead by default.
_DEFAULT_FOREGROUND_WAIT_SECONDS = 30.0
_MIN_RETURN_CODE = -(2**31)
_MAX_RETURN_CODE = 2**32 - 1
_MAX_ACCEPTED_RETURN_CODES = 32


def _foreground_wait_seconds(arguments: Mapping[str, Any]) -> float:
    if bool(arguments.get("background", False)):
        return 0.0
    return _bounded_float(
        arguments.get("foreground_wait_seconds"),
        default=_DEFAULT_FOREGROUND_WAIT_SECONDS,
        minimum=0.0,
        maximum=60.0,
    )


def _accepted_return_codes(arguments: Mapping[str, Any]) -> tuple[int, ...]:
    raw = arguments.get("accepted_return_codes")
    if raw is None:
        return (0,)
    if not isinstance(raw, (list, tuple)):
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            "accepted_return_codes must be a JSON array of unique integers",
            operation="shell",
        )
    if not 1 <= len(raw) <= _MAX_ACCEPTED_RETURN_CODES:
        raise SandboxGatewayError(
            GatewayErrorCode.INVALID_REQUEST,
            "accepted_return_codes must contain between 1 and 32 integers",
            operation="shell",
        )
    accepted: list[int] = []
    for value in raw:
        if type(value) is not int or not _MIN_RETURN_CODE <= value <= _MAX_RETURN_CODE:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                "accepted_return_codes entries must be bounded integers",
                operation="shell",
            )
        if value in accepted:
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                "accepted_return_codes entries must be unique",
                operation="shell",
            )
        accepted.append(value)
    return tuple(accepted)


@dataclass(frozen=True, slots=True)
class GatewayToolRoutingDecision:
    handled: bool
    tool_name: str
    operation: GatewayAction | None
    requires_workspace_port: bool
    reason: str


@dataclass(frozen=True, slots=True)
class _CommandJob:
    job_id: str
    command_id: str
    session_id: str
    parent_session_id: str
    run_id: str
    task_id: str
    submitted_at: float
    cancellation: threading.Event
    future: Future[ToolResult]


def _filesystem_safe_session_id(value: str) -> str:
    selected = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in str(value)
    ).strip("._")
    return selected[:160] or "gateway-session"


class GatewayToolExecutionRouter:
    """The managed CodeWorker side-effect boundary.

    ToolPermissionRuntime remains the authority.  WorkspaceEditPort remains
    the transaction owner.  This router binds both to SandboxGatewayRuntime
    and converts only immutable receipts back into ToolResult metadata.
    """

    def __init__(self, bundle: "GatewayRuntimeBundle") -> None:
        self.bundle = bundle
        self._host_runtime = GatewayHostProcessRuntime(
            bundle.policy_runtime,
            allowed_roots=(bundle.workspace_root,),
        )
        self._jobs_lock = threading.RLock()
        self._jobs: dict[str, _CommandJob] = {}
        self._session_cleanup_lock = threading.RLock()
        self._pending_session_cleanup: set[str] = set()
        self._job_pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="zyra-sandbox-command",
        )

    def handles(self, tool_name: str) -> bool:
        selected = str(tool_name)
        if selected == "file_read" and self.bundle.workspace_edit_port is None:
            return False
        return selected in _HANDLED_TOOLS

    def route(self, tool_name: str) -> GatewayToolRoutingDecision:
        selected = str(tool_name)
        action = {
            "shell": GatewayAction.COMMAND,
            "shell_wait": GatewayAction.COMMAND,
            "file_read": GatewayAction.FILE_READ,
            "file_write": GatewayAction.FILE_WRITE,
            "file_edit": GatewayAction.FILE_EDIT,
            "file_delete": GatewayAction.FILE_DELETE,
            "artifact_write": GatewayAction.ARTIFACT_PUBLISH,
        }.get(selected)
        return GatewayToolRoutingDecision(
            handled=action is not None,
            tool_name=selected,
            operation=action,
            requires_workspace_port=selected not in {"shell", "shell_wait"},
            reason=(
                "tool is owned by the managed sandbox gateway"
                if action is not None
                else "tool is outside the sandbox gateway surface"
            ),
        )

    def execute(
        self,
        call: ToolCall,
        *,
        permission_grant: Any | None,
        permission_authority: Any | None,
        permission_execution_context: Any | None,
    ) -> ToolResult:
        decision = self.route(call.tool_name)
        if not decision.handled:
            return self._error(call, "sandbox_gateway_tool_not_owned", decision.reason)
        if decision.requires_workspace_port and self.bundle.workspace_edit_port is None:
            return self._error(
                call,
                "workspace_gateway_unavailable",
                "managed sandbox execution requires WorkspaceEditPort",
            )
        try:
            # Every handled operation, including read-only WorkspaceEditPort
            # routes, is owned by SandboxGateway.  Check owner availability
            # before selecting the concrete execution port so a disabled
            # runtime cannot be masked by a direct file adapter.
            self.bundle.runtime.assert_enabled()
            identity = self._identity(call)
            if call.tool_name not in {"shell", "shell_wait"}:
                # WorkspaceEditPort operations commit through the same
                # session-scoped artifact/state custody as shell execution.
                # Ensure that custody exists before read/write/edit/delete;
                # otherwise a clean product state rejects the first ordinary
                # file effect with session_not_found.
                self._ensure_session(identity)
            if call.tool_name == "shell":
                return self._shell(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                )
            if call.tool_name == "shell_wait":
                return self._shell_wait(call, identity)
            if call.tool_name == "file_read":
                return self._file_read(call, identity)
            if call.tool_name == "file_write":
                return self._file_write(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                    artifact=False,
                )
            if call.tool_name == "artifact_write":
                return self._file_write(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                    artifact=True,
                )
            if call.tool_name == "file_edit":
                return self._file_edit(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                )
            if call.tool_name == "file_delete":
                return self._file_delete(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                )
        except Exception as error:  # noqa: BLE001 - gateway errors must be traceable tool results.
            code = str(getattr(getattr(error, "code", None), "value", "") or type(error).__name__)
            signal = self.bundle.signal_emitter.emit(
                self._identity(call),
                invocation_id=stable_identifier("gateway-invocation", call.tool_call_id, call.tool_name),
                failure_class=_failure_class(error),
                code=code,
                reason=str(error) or type(error).__name__,
                retryable=_retryable(error),
                recovery_actions=_recovery_actions(error),
                causation_id=call.tool_call_id,
                metadata={"tool_name": call.tool_name},
            )
            return self._error(
                call,
                code,
                str(error) or type(error).__name__,
                recovery=tuple(
                    str(item)
                    for item in getattr(getattr(error, "detail", None), "recovery", ())
                    if str(item).strip()
                ),
                metadata={
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                    "sandbox_gateway_failure_signal_digest": signal.signal_digest,
                },
            )
        return self._error(call, "sandbox_gateway_tool_not_implemented", call.tool_name)

    def cancel(self, session_id: str, command_id: str, *, reason: str) -> bool:
        if self._host_runtime.cancel(command_id, reason=reason):
            return True
        if session_id not in self.bundle.state_store.snapshot().get("sessions", {}):
            return False
        return self.bundle.runtime.cancel(session_id, command_id, reason=reason)

    def cancel_all(self, *, reason: str) -> int:
        """Cancel every unfinished command owned by this query router."""

        with self._jobs_lock:
            jobs = tuple(job for job in self._jobs.values() if not job.future.done())
        for job in jobs:
            job.cancellation.set()
            job.future.cancel()
        deadline = time.monotonic() + 1.0
        pending = list(jobs)
        while pending and time.monotonic() < deadline:
            next_pending: list[_CommandJob] = []
            for job in pending:
                if job.future.done():
                    continue
                if not self.cancel(job.session_id, job.command_id, reason=reason):
                    next_pending.append(job)
            pending = next_pending
            if pending:
                time.sleep(0.02)
        return len(jobs)

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime": "GatewayToolExecutionRouter",
            "handled_tools": sorted(_HANDLED_TOOLS),
            "bundle": self.bundle.descriptor(),
            "raw_subprocess_fallback": False,
            "raw_filesystem_mutation_fallback": False,
        }

    def _shell(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult:
        accepted_return_codes = _accepted_return_codes(call.arguments)
        executable, argv, _environment, _cwd = self.bundle.policy_runtime.command_from_arguments(
            call.arguments
        )
        parent_session_id = identity.session_id
        command_identity = replace(
            identity,
            session_id=_filesystem_safe_session_id(
                stable_identifier(
                    "gateway-command-session",
                    parent_session_id,
                    call.tool_call_id,
                )
            ),
        )
        command_id = stable_identifier(
            "gateway-command",
            command_identity.binding_digest,
            call.tool_call_id,
            executable,
            argv,
        )
        job_id = stable_identifier(
            "gateway-command-job",
            parent_session_id,
            command_id,
        )
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                cancellation = threading.Event()
                future = self._job_pool.submit(
                    self._shell_sync_and_close,
                    call,
                    command_identity,
                    cancellation_event=cancellation,
                    accepted_return_codes=accepted_return_codes,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                )
                job = _CommandJob(
                    job_id=job_id,
                    command_id=command_id,
                    session_id=command_identity.session_id,
                    parent_session_id=parent_session_id,
                    run_id=identity.run_id,
                    task_id=identity.task_id,
                    submitted_at=time.time(),
                    cancellation=cancellation,
                    future=future,
                )
                self._jobs[job_id] = job
                self._prune_jobs_locked()
        foreground_wait = _foreground_wait_seconds(call.arguments)
        try:
            result = job.future.result(timeout=foreground_wait)
        except FutureTimeoutError:
            return self._running_job_result(call, job, after_sequence=0)
        return self._completed_job_result(call, job, result)

    def _shell_wait(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
    ) -> ToolResult:
        job_id = str(call.arguments.get("job_id") or "").strip()
        with self._jobs_lock:
            job = self._jobs.get(job_id)
        if job is None:
            return self._error(call, "shell_job_not_found", "unknown or expired shell job_id")
        if (job.parent_session_id, job.run_id, job.task_id) != (
            identity.session_id,
            identity.run_id,
            identity.task_id,
        ):
            return self._error(call, "shell_job_scope_mismatch", "shell job belongs to another task")
        wait_seconds = _bounded_float(
            call.arguments.get("timeout_seconds"),
            default=10.0,
            minimum=0.0,
            maximum=60.0,
        )
        after_sequence = _bounded_int(
            call.arguments.get("after_sequence"),
            default=0,
            minimum=0,
            maximum=2_147_483_647,
        )
        try:
            result = job.future.result(timeout=wait_seconds)
        except FutureTimeoutError:
            return self._running_job_result(
                call,
                job,
                after_sequence=after_sequence,
            )
        return self._completed_job_result(call, job, result)

    def _running_job_result(
        self,
        call: ToolCall,
        job: _CommandJob,
        *,
        after_sequence: int,
    ) -> ToolResult:
        chunks, last_sequence = self._job_output(job, after_sequence=after_sequence)
        now = time.time()
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary="Sandbox command is still running in the background.",
            output={
                "job_id": job.job_id,
                "command_id": job.command_id,
                "status": "running",
                "heartbeat_at": now,
                "elapsed_seconds": max(0.0, now - job.submitted_at),
                "output_chunks": chunks,
                "last_sequence": last_sequence,
            },
            metadata={
                "sandbox_command_background": "true",
                "sandbox_command_status": "running",
                "sandbox_command_job_id": job.job_id,
                "sandbox_command_id": job.command_id,
                "physical_effect_pending": "true",
            },
        )

    def _completed_job_result(
        self,
        call: ToolCall,
        job: _CommandJob,
        result: ToolResult,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=result.ok,
            summary=result.summary,
            output={
                **dict(result.output),
                "job_id": job.job_id,
                "command_id": job.command_id,
                "status": "completed",
            },
            artifacts=list(result.artifacts),
            error=result.error,
            metadata={
                **dict(result.metadata),
                "sandbox_command_background": "false",
                "sandbox_command_status": "completed",
                "sandbox_command_job_id": job.job_id,
                "sandbox_command_id": job.command_id,
                "originating_tool_call_id": result.tool_call_id,
            },
        )

    def _job_output(
        self,
        job: _CommandJob,
        *,
        after_sequence: int,
    ) -> tuple[list[dict[str, Any]], int]:
        chunks: list[dict[str, Any]] = []
        last_sequence = after_sequence
        for event in self.bundle.event_port.list(
            job.session_id,
            after_sequence=after_sequence,
        ):
            last_sequence = max(last_sequence, int(event.sequence))
            if event.kind != GatewayEventKind.COMMAND_OUTPUT:
                continue
            payload = dict(event.payload)
            if str(payload.get("command_id") or "") != job.command_id:
                continue
            chunks.append(
                {
                    "sequence": event.sequence,
                    "stream": str(payload.get("stream") or ""),
                    "bytes": int(payload.get("bytes") or 0),
                    "content": str(payload.get("content") or ""),
                }
            )
        return chunks, last_sequence

    def _prune_jobs_locked(self) -> None:
        if len(self._jobs) <= 256:
            return
        completed = sorted(
            (job for job in self._jobs.values() if job.future.done()),
            key=lambda item: item.submitted_at,
        )
        for job in completed[: max(0, len(self._jobs) - 256)]:
            self._jobs.pop(job.job_id, None)

    def _queue_command_session_cleanup(self, session_id: str) -> None:
        """Close completed sessions once the shared backend has no live process."""

        with self._session_cleanup_lock:
            self._pending_session_cleanup.add(session_id)
            known_sessions = self.bundle.state_store.snapshot().get("sessions", {})
            for pending_session_id in tuple(sorted(self._pending_session_cleanup)):
                if pending_session_id not in known_sessions:
                    self._pending_session_cleanup.discard(pending_session_id)
                    continue
                try:
                    self.bundle.runtime.close_session(pending_session_id)
                except SandboxGatewayError as error:
                    if error.code is GatewayErrorCode.PROCESS_TREE_LEAK:
                        continue
                    raise
                self._pending_session_cleanup.discard(pending_session_id)

    def _shell_sync_and_close(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        **kwargs: Any,
    ) -> ToolResult:
        try:
            return self._shell_sync(call, identity, **kwargs)
        finally:
            self._queue_command_session_cleanup(identity.session_id)

    def _shell_sync(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        cancellation_event: threading.Event,
        accepted_return_codes: Sequence[int],
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult:
        if cancellation_event.is_set():
            return self._error(call, "parent_cancelled", "command cancelled before execution")
        executable, argv, environment, cwd = self.bundle.policy_runtime.command_from_arguments(
            call.arguments
        )
        timeout = _bounded_float(
            call.arguments.get("timeout_seconds"),
            default=self.bundle.policy_runtime.config.default_command_timeout_seconds,
            minimum=0.1,
            maximum=self.bundle.policy_runtime.config.maximum_command_timeout_seconds,
        )
        stdout_limit = _bounded_int(
            call.arguments.get("stdout_limit_bytes"),
            default=2 * 1024 * 1024,
            minimum=1024,
            maximum=64 * 1024 * 1024,
        )
        stderr_limit = _bounded_int(
            call.arguments.get("stderr_limit_bytes"),
            default=2 * 1024 * 1024,
            minimum=1024,
            maximum=64 * 1024 * 1024,
        )
        command_id = stable_identifier(
            "gateway-command",
            identity.binding_digest,
            call.tool_call_id,
            executable,
            argv,
        )
        envelope = GatewayCommandEnvelope(
            command_id=command_id,
            session_id=identity.session_id,
            run_id=identity.run_id,
            task_id=identity.task_id,
            worker_id=identity.worker_id,
            executable=executable,
            argv=argv,
            cwd=cwd,
            environment=environment,
            budget=CommandBudget(
                timeout_seconds=timeout,
                cancel_grace_seconds=3.0,
                stdout_limit_bytes=stdout_limit,
                stderr_limit_bytes=stderr_limit,
                combined_output_limit_bytes=min(
                    stdout_limit + stderr_limit,
                    96 * 1024 * 1024,
                ),
                max_processes=_bounded_int(
                    call.arguments.get("max_processes"),
                    default=32,
                    minimum=1,
                    maximum=256,
                ),
            ),
            operation=OperationKind.COMMAND,
            tool_use_id=call.tool_call_id,
            workspace_id=identity.workspace_id,
            owner_epoch=identity.owner_epoch,
            fence_digest=_workspace_fence_digest(self.bundle.workspace_edit_port),
            network_profile=str(
                call.arguments.get("network_profile")
                or self.bundle.policy_runtime.config.default_command_network_profile
            ),
            provenance_ref=str(call.metadata.get("provenance_ref") or ""),
            idempotency_key=str(call.arguments.get("idempotency_key") or call.tool_call_id),
            causation_id=call.tool_call_id,
            correlation_id=str(call.metadata.get("correlation_id") or ""),
            metadata={
                "gateway_surface": GatewaySurface.CODE_WORKER.value,
                "structured_argv": True,
                "legacy_command_normalized": bool(call.arguments.get("command")),
                "progressive_delivery_driving_shell": _metadata_flag(
                    call.metadata.get("progressive_delivery_driving_shell")
                ),
                "progressive_verification_driving": _metadata_flag(
                    call.metadata.get("progressive_verification_driving")
                ),
                "progressive_verification_scope": str(
                    call.metadata.get("progressive_verification_scope") or ""
                ),
                "accepted_return_codes": list(accepted_return_codes),
            },
        )
        policy = self.bundle.policy_runtime.evaluate_command(envelope)
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.CODE_WORKER,
            action=GatewayAction.COMMAND,
            tool_call_id=call.tool_call_id,
            logical_name=executable,
            arguments={
                "executable": executable,
                "argv": argv,
                "cwd": cwd,
                "environment_digest": content_digest(environment),
                "accepted_return_codes": list(accepted_return_codes),
            },
            policy_digest=policy.policy_digest,
            idempotency_key=envelope.idempotency_key,
            causation_id=call.tool_call_id,
            correlation_id=envelope.correlation_id,
            metadata={"command_id": command_id},
        )
        if policy.hard_denied:
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=invocation.invocation_id,
                failure_class=FailureClass.POLICY,
                code="gateway_command_denied",
                reason=policy.reason,
                retryable=False,
                recovery_actions=(RecoveryAction.REDUCE_SCOPE, RecoveryAction.REPLAN),
                causation_id=call.tool_call_id,
                metadata={"policy_digest": policy.policy_digest},
            )
            return self._error(
                call,
                "gateway_command_denied",
                policy.reason,
                metadata={"sandbox_gateway_failure_signal_id": signal.signal_id},
            )
        if cancellation_event.is_set():
            return self._error(call, "parent_cancelled", "command cancelled before dispatch")
        remote = self._dispatch_backend_action_after_permission(
            call,
            identity,
            policy=policy,
            permission_grant=permission_grant,
            permission_authority=permission_authority,
            permission_execution_context=permission_execution_context,
        )
        if remote is not None:
            return remote
        if cancellation_event.is_set():
            return self._error(call, "parent_cancelled", "command cancelled before process start")
        if self.bundle.workspace_edit_port is None and not self.bundle.required:
            return self._execute_host_compatibility(
                call,
                identity,
                invocation=invocation,
                envelope=envelope,
                policy=policy,
                executable=executable,
                argv=argv,
                environment=environment,
                cwd=cwd,
                timeout=timeout,
                stdout_limit=stdout_limit,
                stderr_limit=stderr_limit,
                accepted_return_codes=accepted_return_codes,
                permission_grant=permission_grant,
                permission_authority=permission_authority,
                permission_execution_context=permission_execution_context,
            )
        self._ensure_session(identity)
        if cancellation_event.is_set():
            return self._error(call, "parent_cancelled", "command cancelled before sandbox start")
        command_digest = str(envelope.identity_digest)
        with self.bundle.permission_bridge.activate(
            call=call,
            grant=permission_grant,
            authority=permission_authority,
            execution_context=permission_execution_context,
            policy=policy,
            command_digest=command_digest,
            metadata={"invocation_id": invocation.invocation_id},
        ):
            execution = self.bundle.runtime.execute(
                envelope,
                input_paths=self._input_paths(call.arguments),
                commit_outputs=True,
            )
        process = execution.result
        mutation_policy_guard = self.bundle.task_mutation_policy_guard
        if mutation_policy_guard is not None:
            mutation_policy_guard.observe_command(
                executable=envelope.executable,
                argv=envelope.argv,
                metadata=envelope.metadata,
                termination=process.termination.value,
                return_code=process.return_code,
                command_id=envelope.command_id,
            )
        stdout = process.output.stdout.decode("utf-8", errors="replace")
        stderr = process.output.stderr.decode("utf-8", errors="replace")
        spill_receipt = self._spill_command_output(call, identity, process.output)
        artifact_refs = tuple(
            dict.fromkeys(
                (
                    *execution.receipt.artifact_refs,
                    *((spill_receipt.artifact_ref,) if spill_receipt and spill_receipt.artifact_ref else ()),
                )
            )
        )
        return_code_accepted = (
            process.termination == ProcessTermination.EXITED
            and process.return_code in accepted_return_codes
        )
        succeeded = return_code_accepted
        outcome = GatewayOutcome.COMMITTED if succeeded else GatewayOutcome.FAILED
        integration_receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier(
                "gateway-execution",
                execution.receipt.receipt_id,
                invocation.binding_digest,
            ),
            invocation=invocation,
            outcome=outcome,
            result_digest=content_digest(
                {
                    "termination": process.termination.value,
                    "return_code": process.return_code,
                    "accepted_return_codes": list(accepted_return_codes),
                    "return_code_accepted": return_code_accepted,
                    "stdout_digest": gateway_content_digest(process.output.stdout),
                    "stderr_digest": gateway_content_digest(process.output.stderr),
                    "workspace_state_before_digest": process.metadata.get(
                        "workspace_state_before_digest", ""
                    ),
                    "workspace_state_after_digest": process.metadata.get(
                        "workspace_state_after_digest", ""
                    ),
                }
            ),
            permission_consumption_id=execution.receipt.permission_consumption_id,
            command_receipt_id=execution.receipt.receipt_id,
            patch_receipt_id=execution.receipt.patch_receipt_id,
            artifact_refs=artifact_refs,
            event_refs=execution.event_ids,
            owner_epoch_before=execution.receipt.owner_epoch_before,
            owner_epoch_after=(
                spill_receipt.owner_epoch_after
                if spill_receipt is not None and spill_receipt.committed
                else execution.receipt.owner_epoch_after
            ),
            backend_generation=execution.record.generation,
            started_at=process.started_at,
            finished_at=process.finished_at,
            failure_code=process.error_code if not succeeded else "",
            metadata={
                "termination": process.termination.value,
                "return_code": process.return_code,
                "accepted_return_codes": list(accepted_return_codes),
                "return_code_accepted": return_code_accepted,
                "stdout_truncated": process.output.stdout_truncated,
                "stderr_truncated": process.output.stderr_truncated,
                "combined_truncated": process.output.combined_truncated,
                "recovery_required": execution.recovery_required,
                "output_spilled": spill_receipt is not None,
                "output_spill_receipt_id": spill_receipt.receipt_id if spill_receipt else "",
                "workspace_mutation_committed": _metadata_flag(
                    process.metadata.get("workspace_mutation_committed")
                ),
                "workspace_state_mode": str(
                    process.metadata.get("workspace_state_mode") or ""
                ),
                "workspace_state_before_digest": str(
                    process.metadata.get("workspace_state_before_digest") or ""
                ),
                "workspace_state_after_digest": str(
                    process.metadata.get("workspace_state_after_digest") or ""
                ),
            },
        )
        self.bundle.receipt_journal.append(
            integration_receipt,
            idempotency_key=envelope.idempotency_key,
        )
        signals = ()
        if not succeeded:
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=invocation.invocation_id,
                failure_class=_process_failure_class(process.termination),
                code=process.error_code or f"process_{process.termination.value}",
                reason=process.cancellation_reason or stderr or "sandbox command failed",
                retryable=process.termination
                in {ProcessTermination.TIMED_OUT, ProcessTermination.FAILED_TO_START},
                recovery_actions=(RecoveryAction.RETRY, RecoveryAction.REPLAN),
                backend_signal=process.termination.value,
                artifact_refs=execution.receipt.artifact_refs,
                causation_id=call.tool_call_id,
            )
            signals = (signal,)
        metadata = self.bundle.event_projector.tool_metadata(integration_receipt, signals)
        process_tree_controlled = (
            process.metadata.get("container_process_tree_controlled") is True
            if process.metadata.get("connector") == "docker-cli"
            else True
        )
        settled_timeout = (
            process.termination is ProcessTermination.TIMED_OUT
            and process_tree_controlled
        )
        settled_start_failure = (
            process.termination is ProcessTermination.FAILED_TO_START
            and process.return_code is None
            and process_tree_controlled
            and not _metadata_flag(
                process.metadata.get("workspace_mutation_committed")
            )
            and not (
                execution.patch_receipt is not None
                and execution.patch_receipt.committed
            )
        )
        metadata.update(
            {
                "return_code": str(process.return_code),
                "accepted_return_codes": ",".join(
                    str(value) for value in accepted_return_codes
                ),
                "return_code_accepted": str(return_code_accepted).lower(),
                "termination": process.termination.value,
                "output_bounded": "true",
                "process_tree_controlled": str(process_tree_controlled).lower(),
                "workspace_mutation_committed": str(
                    succeeded
                    and (
                        (
                            execution.patch_receipt is not None
                            and execution.patch_receipt.committed
                        )
                        or _metadata_flag(
                            process.metadata.get("workspace_mutation_committed")
                        )
                    )
                ).lower(),
                # A gateway timeout has a synchronously committed process and
                # workspace receipt.  The child session remains quarantined,
                # while the model may inspect the committed workspace through
                # a new isolated command session and replan from the observed
                # timeout instead of losing the entire agent run.
                "command_timeout_settled": str(settled_timeout).lower(),
                # A failed-to-start process has no child to fence.  Only expose
                # model recovery when the gateway also proves that neither the
                # process nor the workspace patch path committed a mutation.
                "command_start_failure_settled": str(
                    settled_start_failure
                ).lower(),
                "model_recovery_allowed": str(
                    settled_timeout or settled_start_failure
                ).lower(),
            }
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=succeeded,
            summary="Sandbox command completed" if succeeded else "Sandbox command failed",
            output={
                "stdout": stdout,
                "stderr": stderr,
                "return_code": process.return_code,
                "termination": process.termination.value,
                "accepted_return_codes": list(accepted_return_codes),
                "return_code_accepted": return_code_accepted,
                "artifact_refs": list(artifact_refs),
                "gateway_receipt": integration_receipt.safe_dict(),
                **(
                    {"recovery_hint": _process_recovery_hint(process.termination)}
                    if process.termination is ProcessTermination.FAILED_TO_START
                    else {}
                ),
            },
            error=None if succeeded else process.error_code or "sandbox_command_failed",
            metadata=metadata,
        )

    def _spill_command_output(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        output: Any,
    ) -> FileArtifactReceipt | None:
        if not (
            output.stdout_truncated
            or output.stderr_truncated
            or output.combined_truncated
        ):
            return None
        artifact_port = self._require_artifact_port()
        stdout = bytes(output.stdout) + bytes(output.stdout_overflow)
        stderr = bytes(output.stderr) + bytes(output.stderr_overflow)
        content = b"[stdout]\n" + stdout + b"\n[stderr]\n" + stderr
        access = artifact_port.workspace_edit_port.current_access()
        current_workspace_id = str(getattr(access, "workspace_id", "") or "")
        current_owner_epoch = int(getattr(access, "owner_epoch", 0) or 0)
        manager = getattr(artifact_port.workspace_edit_port, "manager", None)
        store = getattr(manager, "store", None)
        require_binding = getattr(store, "require_binding", None)
        if callable(require_binding):
            binding = require_binding(current_workspace_id)
            current_owner_epoch = int(getattr(binding, "owner_epoch", 0) or current_owner_epoch)
        provenance = self._internal_provenance(
            identity,
            call,
            content,
            kind=ProvenanceKind.GENERATED,
        )
        request = FileArtifactRequest.build(
            session_id=identity.session_id,
            logical_path=f"command-output/{call.tool_call_id}.log",
            content=content,
            content_type="text/plain",
            provenance=provenance,
            operation=OperationKind.ARTIFACT_EXPORT,
            expected_workspace_id=current_workspace_id,
            expected_owner_epoch=current_owner_epoch,
            idempotency_key=stable_identifier("command-output-spill", call.tool_call_id),
            causation_id=call.tool_call_id,
            metadata={"gateway_output_spill": True},
        )
        return artifact_port.commit(request)

    def _execute_host_compatibility(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        invocation: Any,
        envelope: GatewayCommandEnvelope,
        policy: Any,
        executable: str,
        argv: Sequence[str],
        environment: Mapping[str, str],
        cwd: str,
        timeout: float,
        stdout_limit: int,
        stderr_limit: int,
        accepted_return_codes: Sequence[int],
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult:
        permission = self.bundle.permission_bridge.consume_non_command(
            call=call,
            grant=permission_grant,
            authority=permission_authority,
            execution_context=permission_execution_context,
            policy=policy,
        )
        if not permission.allowed:
            return self._error(
                call,
                "permission_grant_required",
                permission.reason,
                metadata={"permission_consumption_id": permission.receipt_id},
            )
        host_cwd = self.bundle.workspace_root
        if cwd not in {"", "."}:
            host_cwd = self.bundle.workspace_root.joinpath(*PurePosixPath(cwd).parts).resolve()
            host_cwd.relative_to(self.bundle.workspace_root)
        host = self._host_runtime.run(
            executable=executable,
            argv=argv,
            cwd=host_cwd,
            timeout_seconds=timeout,
            stdout_limit_bytes=stdout_limit,
            stderr_limit_bytes=stderr_limit,
            environment=environment,
            operation_name="code-worker-host-compatibility",
        )
        return_code_accepted = (
            host.termination == ProcessTermination.EXITED
            and host.return_code in accepted_return_codes
        )
        succeeded = return_code_accepted
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier(
                "gateway-execution",
                host.receipt_id,
                invocation.binding_digest,
            ),
            invocation=invocation,
            outcome=GatewayOutcome.COMMITTED if succeeded else GatewayOutcome.FAILED,
            result_digest=content_digest(
                {
                    "termination": host.termination.value,
                    "return_code": host.return_code,
                    "accepted_return_codes": list(accepted_return_codes),
                    "return_code_accepted": return_code_accepted,
                    "stdout_digest": gateway_content_digest(host.stdout),
                    "stderr_digest": gateway_content_digest(host.stderr),
                }
            ),
            permission_consumption_id=permission.receipt_id,
            command_receipt_id=host.receipt_id,
            patch_receipt_id="",
            artifact_refs=(),
            event_refs=(),
            owner_epoch_before=identity.owner_epoch,
            owner_epoch_after=identity.owner_epoch,
            backend_generation=identity.generation,
            started_at=host.started_at,
            finished_at=host.finished_at,
            failure_code=host.failure_code if not succeeded else "",
            metadata={
                "termination": host.termination.value,
                "return_code": host.return_code,
                "accepted_return_codes": list(accepted_return_codes),
                "return_code_accepted": return_code_accepted,
                "stdout_truncated": host.stdout_truncated,
                "stderr_truncated": host.stderr_truncated,
                "host_compatibility": True,
                "main_path": False,
                "shell": False,
                "process_tree_controlled": True,
            },
        )
        self.bundle.receipt_journal.append(receipt, idempotency_key=envelope.idempotency_key)
        signals = ()
        if not succeeded:
            signal = self.bundle.signal_emitter.emit(
                identity,
                invocation_id=invocation.invocation_id,
                failure_class=_process_failure_class(host.termination),
                code=host.failure_code or f"process_{host.termination.value}",
                reason=host.stderr.decode("utf-8", errors="replace") or "host command failed",
                retryable=host.termination
                in {
                    ProcessTermination.TIMED_OUT,
                    ProcessTermination.FAILED_TO_START,
                    ProcessTermination.TREE_LEAK,
                },
                recovery_actions=(RecoveryAction.RETRY, RecoveryAction.REPLAN),
                backend_signal=host.termination.value,
                causation_id=call.tool_call_id,
            )
            signals = (signal,)
        metadata = self.bundle.event_projector.tool_metadata(receipt, signals)
        metadata.update(
            {
                "return_code": str(host.return_code),
                "accepted_return_codes": ",".join(
                    str(value) for value in accepted_return_codes
                ),
                "return_code_accepted": str(return_code_accepted).lower(),
                "termination": host.termination.value,
                "output_bounded": "true",
                "process_tree_controlled": "true",
                "sandbox_gateway_host_compatibility": "true",
                "sandbox_gateway_main_path": "false",
            }
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=succeeded,
            summary="Gateway host command completed" if succeeded else "Gateway host command failed",
            output={
                "stdout": host.stdout.decode("utf-8", errors="replace"),
                "stderr": host.stderr.decode("utf-8", errors="replace"),
                "return_code": host.return_code,
                "termination": host.termination.value,
                "accepted_return_codes": list(accepted_return_codes),
                "return_code_accepted": return_code_accepted,
                "gateway_receipt": receipt.safe_dict(),
            },
            error=None if succeeded else host.failure_code or "gateway_host_command_failed",
            metadata=metadata,
        )

    def _file_read(self, call: ToolCall, identity: WorkerGatewayIdentity) -> ToolResult:
        artifact_port = self._require_artifact_port()
        logical_path = self.bundle.policy_runtime.assert_path(str(call.arguments.get("path") or ""))
        content = artifact_port.read(logical_path)
        encoding = str(call.arguments.get("encoding") or "utf-8")
        try:
            text = content.decode(encoding)
            binary = False
        except UnicodeDecodeError:
            text = ""
            binary = True
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.CODE_WORKER,
            action=GatewayAction.FILE_READ,
            tool_call_id=call.tool_call_id,
            logical_name=logical_path,
            arguments={"path": logical_path, "encoding": encoding},
            policy_digest=self.bundle.policy_runtime.policy_digest,
            causation_id=call.tool_call_id,
        )
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier(
                "gateway-execution",
                invocation.binding_digest,
                gateway_content_digest(content),
            ),
            invocation=invocation,
            outcome=GatewayOutcome.ALLOWED,
            result_digest=content_digest(
                {"content_digest": gateway_content_digest(content), "content_bytes": len(content)}
            ),
            owner_epoch_before=identity.owner_epoch,
            owner_epoch_after=identity.owner_epoch,
            metadata={"read_only": True, "binary": binary},
        )
        self.bundle.receipt_journal.append(receipt)
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Read {logical_path} through SandboxGateway",
            output={
                "path": logical_path,
                "content": text if not binary else "",
                "content_digest": gateway_content_digest(content),
                "content_bytes": len(content),
                "binary": binary,
                "gateway_receipt": receipt.safe_dict(),
            },
            metadata=self.bundle.event_projector.tool_metadata(receipt),
        )

    def _file_write(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
        artifact: bool,
        authorization_call: ToolCall | None = None,
    ) -> ToolResult:
        artifact_port = self._require_artifact_port()
        raw_logical_path = str(call.arguments.get("path") or call.arguments.get("name") or "")
        if artifact and not raw_logical_path:
            requested_extension = PurePosixPath(
                str(call.arguments.get("extension") or "")
            ).suffix
            if not requested_extension:
                requested_extension = {
                    "markdown": ".md",
                    "structured_data": ".json",
                    "trace": ".json",
                }.get(str(call.arguments.get("kind") or ""), ".txt")
            raw_logical_path = (
                "tool-artifacts/"
                + _filesystem_safe_session_id(
                    stable_identifier(
                        "tool-artifact",
                        call.tool_call_id,
                        str(call.arguments.get("title") or "Tool artifact"),
                    )
                )
                + requested_extension
            )
        logical_path = self.bundle.policy_runtime.assert_path(
            raw_logical_path
        )
        content = _content_bytes(call.arguments)
        provenance = self._internal_provenance(
            identity,
            call,
            content,
            kind=ProvenanceKind.GENERATED,
        )
        operation = OperationKind.ARTIFACT_EXPORT if artifact else OperationKind.FILE_WRITE
        request = FileArtifactRequest(
            request_id=stable_identifier("gateway-file-request", call.tool_call_id, logical_path),
            session_id=identity.session_id,
            logical_path=logical_path,
            content=content,
            content_type=str(
                call.arguments.get("content_type")
                or mimetypes.guess_type(logical_path)[0]
                or "text/plain"
            ),
            provenance=provenance,
            operation=operation,
            expected_digest=str(call.arguments.get("expected_digest") or ""),
            expected_previous_digest=str(call.arguments.get("expected_previous_digest") or ""),
            expected_workspace_id=identity.workspace_id,
            expected_owner_epoch=identity.owner_epoch,
            mount_kind=str(call.arguments.get("mount_kind") or "task"),
            # A generated script in the fenced task mount is source code at
            # this boundary. Artifact export and later process execution keep
            # their own executable-content controls.
            executable_allowed=not artifact,
            archive_expansion_allowed=False,
            idempotency_key=str(call.arguments.get("idempotency_key") or call.tool_call_id),
            causation_id=call.tool_call_id,
            metadata={"gateway_surface": GatewaySurface.CODE_WORKER.value},
        )
        policy = self.bundle.policy_runtime.evaluate_file(request)
        permission = self.bundle.permission_bridge.consume_non_command(
            call=authorization_call or call,
            grant=permission_grant,
            authority=permission_authority,
            execution_context=permission_execution_context,
            policy=policy,
        )
        if not permission.allowed:
            signal = self.bundle.signal_emitter.permission_blocked(
                identity,
                invocation_id=stable_identifier("gateway-invocation", call.tool_call_id, logical_path),
                reason=permission.reason,
                sealed=self.bundle.sealed,
                causation_id=call.tool_call_id,
            )
            return self._error(
                call,
                "permission_denied" if self.bundle.sealed else "permission_grant_required",
                permission.reason,
                metadata={
                    "sandbox_gateway_permission_receipt_id": permission.receipt_id,
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                },
            )
        remote = self._dispatch_authorized_backend_action(
            call,
            identity,
            permission.safe_dict(),
        )
        if remote is not None:
            return remote
        file_receipt = artifact_port.commit(request)
        outcome = (
            GatewayOutcome.QUARANTINED
            if file_receipt.quarantined
            else GatewayOutcome.COMMITTED
            if file_receipt.committed
            else GatewayOutcome.FAILED
        )
        action = GatewayAction.ARTIFACT_PUBLISH if artifact else GatewayAction.FILE_WRITE
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.ARTIFACT_PIPELINE,
            action=action,
            tool_call_id=call.tool_call_id,
            logical_name=logical_path,
            arguments={
                "path": logical_path,
                "content_digest": gateway_content_digest(content),
                "content_type": request.content_type,
            },
            provenance_ref=provenance.provenance_id,
            policy_digest=policy.policy_digest,
            idempotency_key=request.idempotency_key,
            causation_id=call.tool_call_id,
        )
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier("gateway-execution", file_receipt.receipt_id, invocation.binding_digest),
            invocation=invocation,
            outcome=outcome,
            result_digest=content_digest(file_receipt.__dict__ if hasattr(file_receipt, "__dict__") else str(file_receipt)),
            permission_consumption_id=permission.receipt_id,
            patch_receipt_id=file_receipt.transaction_id,
            artifact_refs=(file_receipt.artifact_ref,) if file_receipt.artifact_ref else (),
            event_refs=file_receipt.event_refs,
            owner_epoch_before=file_receipt.owner_epoch_before,
            owner_epoch_after=file_receipt.owner_epoch_after,
            failure_code="" if file_receipt.committed else file_receipt.reason,
            metadata={
                "quarantined": file_receipt.quarantined,
                "quarantine_id": file_receipt.quarantine_id,
                "content_bytes": file_receipt.content_bytes,
                "workspace_logical_path": logical_path,
                "workspace_path_disposition": str(
                    file_receipt.metadata.get("workspace_path_disposition") or ""
                ),
                "workspace_path_created": bool(
                    file_receipt.metadata.get("workspace_path_created") is True
                ),
                "workspace_path_existed_before": bool(
                    file_receipt.metadata.get("workspace_path_existed_before") is True
                ),
            },
        )
        self.bundle.receipt_journal.append(receipt, idempotency_key=request.idempotency_key)
        ok = file_receipt.committed and not file_receipt.quarantined
        metadata = self.bundle.event_projector.tool_metadata(receipt)
        metadata["workspace_mutation_committed"] = str(ok).lower()
        metadata["workspace_logical_path"] = logical_path
        metadata["workspace_path_disposition"] = str(
            file_receipt.metadata.get("workspace_path_disposition") or ""
        )
        metadata["workspace_path_created"] = str(
            file_receipt.metadata.get("workspace_path_created") is True
        ).lower()
        metadata["workspace_path_existed_before"] = str(
            file_receipt.metadata.get("workspace_path_existed_before") is True
        ).lower()
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=ok,
            summary=(
                f"Committed {logical_path} through SandboxGateway"
                if ok
                else f"Quarantined {logical_path}"
                if file_receipt.quarantined
                else f"Rejected {logical_path}"
            ),
            output={
                "path": logical_path,
                "artifact_id": file_receipt.artifact_ref.removeprefix("artifact://"),
                "content_digest": file_receipt.content_digest,
                "content_bytes": file_receipt.content_bytes,
                "transaction_id": file_receipt.transaction_id,
                "artifact_ref": file_receipt.artifact_ref,
                "quarantine_id": file_receipt.quarantine_id,
                "workspace_path_disposition": metadata["workspace_path_disposition"],
                "workspace_path_created": metadata["workspace_path_created"],
                "workspace_path_existed_before": metadata["workspace_path_existed_before"],
                "gateway_receipt": receipt.safe_dict(),
            },
            artifacts=list(file_receipt.artifact_records),
            error=None if ok else "artifact_quarantined" if file_receipt.quarantined else "workspace_commit_failed",
            metadata=metadata,
        )

    def _file_edit(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult:
        artifact_port = self._require_artifact_port()
        logical_path = self.bundle.policy_runtime.assert_path(str(call.arguments.get("path") or ""))
        before = artifact_port.read(logical_path)
        encoding = str(call.arguments.get("encoding") or "utf-8")
        text = before.decode(encoding)
        old = str(call.arguments.get("old") or "")
        new = str(call.arguments.get("new") or "")
        replace_all = bool(call.arguments.get("replace_all", False))
        if not old:
            return self._error(call, "edit_old_text_missing", "file_edit requires non-empty old text")
        occurrences = text.count(old)
        if occurrences == 0:
            return self._error(call, "edit_old_text_not_found", "old text was not found")
        if occurrences > 1 and not replace_all:
            return self._error(call, "edit_old_text_ambiguous", "old text occurs more than once")
        updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        arguments = dict(call.arguments)
        arguments.update(
            {
                "content": updated,
                "content_type": mimetypes.guess_type(logical_path)[0] or "text/plain",
                "expected_previous_digest": gateway_content_digest(before),
            }
        )
        synthetic = ToolCall(
            run_id=call.run_id,
            task_id=call.task_id,
            tool_name="file_write",
            arguments=arguments,
            tool_call_id=call.tool_call_id,
            node_id=call.node_id,
            created_at=call.created_at,
            metadata={**call.metadata, "gateway_original_tool": "file_edit"},
        )
        result = self._file_write(
            synthetic,
            identity,
            permission_grant=permission_grant,
            permission_authority=permission_authority,
            permission_execution_context=permission_execution_context,
            artifact=False,
            authorization_call=call,
        )
        result.output["edit_occurrences"] = occurrences
        result.output["before_digest"] = gateway_content_digest(before)
        return result

    def _file_delete(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult:
        logical_path = self.bundle.policy_runtime.assert_path(str(call.arguments.get("path") or ""))
        policy = self.bundle.policy_runtime.evaluate_mcp_call(
            server_id="zyra-workspace",
            tool_name="file-delete",
            arguments={"path": logical_path},
            external_boundary=True,
        )
        permission = self.bundle.permission_bridge.consume_non_command(
            call=call,
            grant=permission_grant,
            authority=permission_authority,
            execution_context=permission_execution_context,
            policy=policy,
        )
        if not permission.allowed:
            return self._error(call, "permission_grant_required", permission.reason)
        remote = self._dispatch_authorized_backend_action(
            call,
            identity,
            permission.safe_dict(),
        )
        if remote is not None:
            return remote
        result = self.bundle.workspace_edit_port.delete_file(
            logical_path,
            idempotency_key=str(call.arguments.get("idempotency_key") or call.tool_call_id),
            causation_id=call.tool_call_id,
        )
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.ARTIFACT_PIPELINE,
            action=GatewayAction.FILE_DELETE,
            tool_call_id=call.tool_call_id,
            logical_name=logical_path,
            arguments={"path": logical_path},
            policy_digest=policy.policy_digest,
            causation_id=call.tool_call_id,
        )
        receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier("gateway-execution", invocation.binding_digest, "delete"),
            invocation=invocation,
            outcome=GatewayOutcome.COMMITTED if result.ok else GatewayOutcome.FAILED,
            result_digest=content_digest(result.to_public_dict()),
            permission_consumption_id=permission.receipt_id,
            patch_receipt_id=result.transaction.transaction_id,
            owner_epoch_before=result.transaction.owner_epoch_before,
            owner_epoch_after=result.transaction.owner_epoch_after,
            failure_code="" if result.ok else str(result.error or "workspace_delete_failed"),
        )
        self.bundle.receipt_journal.append(receipt)
        metadata = self.bundle.event_projector.tool_metadata(receipt)
        metadata["workspace_mutation_committed"] = str(bool(result.ok)).lower()
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=bool(result.ok),
            summary=f"Deleted {logical_path} through SandboxGateway" if result.ok else f"Delete failed for {logical_path}",
            output={"path": logical_path, "gateway_receipt": receipt.safe_dict()},
            error=None if result.ok else str(result.error or "workspace_delete_failed"),
            metadata=metadata,
        )

    def _dispatch_backend_action_after_permission(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        *,
        policy: Any,
        permission_grant: Any,
        permission_authority: Any,
        permission_execution_context: Any,
    ) -> ToolResult | None:
        if not self._backend_action_available(call.tool_name):
            return None
        permission = self.bundle.permission_bridge.consume_non_command(
            call=call,
            grant=permission_grant,
            authority=permission_authority,
            execution_context=permission_execution_context,
            policy=policy,
        )
        if not permission.allowed:
            signal = self.bundle.signal_emitter.permission_blocked(
                identity,
                invocation_id=stable_identifier(
                    "gateway-invocation",
                    call.tool_call_id,
                    call.tool_name,
                ),
                reason=permission.reason,
                sealed=self.bundle.sealed,
                causation_id=call.tool_call_id,
            )
            return self._error(
                call,
                "permission_denied" if self.bundle.sealed else "permission_grant_required",
                permission.reason,
                metadata={
                    "sandbox_gateway_permission_receipt_id": permission.receipt_id,
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                },
            )
        return self._dispatch_authorized_backend_action(
            call,
            identity,
            permission.safe_dict(),
        )

    def _dispatch_authorized_backend_action(
        self,
        call: ToolCall,
        identity: WorkerGatewayIdentity,
        permission_receipt: Mapping[str, Any],
    ) -> ToolResult | None:
        if not self._backend_action_available(call.tool_name):
            return None
        port = self.bundle.backend_action_dispatch_port
        projection = port.dispatch_action(
            run_id=identity.run_id,
            task_id=identity.task_id,
            node_id=identity.node_id,
            tool_name=call.tool_name,
            tool_call_id=call.tool_call_id,
            arguments=call.arguments,
            metadata=call.metadata,
            permission_receipt=permission_receipt,
        )
        if not isinstance(projection, Mapping) or projection.get("schema") != (
            "zyra.backend-action-dispatch-result/v1"
        ):
            raise RuntimeError("backend action dispatch result schema is invalid")
        raw_result = projection.get("tool_result")
        receipt = projection.get("dispatch_receipt")
        if not isinstance(raw_result, Mapping) or not isinstance(receipt, Mapping):
            raise RuntimeError("backend action dispatch result is incomplete")
        raw_output = raw_result.get("output") or {}
        raw_metadata = raw_result.get("metadata") or {}
        if not isinstance(raw_output, Mapping) or not isinstance(raw_metadata, Mapping):
            raise RuntimeError("terminal action result output or metadata is invalid")
        routed_ok = raw_result.get("ok") is True
        route = self.route(call.tool_name)
        if route.operation is None:
            raise RuntimeError("terminal action has no sandbox gateway operation")
        invocation = invocation_for(
            identity,
            surface=GatewaySurface.CODE_WORKER,
            action=route.operation,
            tool_call_id=call.tool_call_id,
            logical_name=call.tool_name,
            arguments={
                "tool_name": call.tool_name,
                "arguments_digest": content_digest(call.arguments),
            },
            policy_digest=self.bundle.policy_runtime.policy_digest,
            idempotency_key=f"terminal-action:{call.tool_call_id}",
            causation_id=call.tool_call_id,
        )
        gateway_receipt = GatewayExecutionReceipt(
            receipt_id=stable_identifier(
                "gateway-execution",
                str(receipt.get("dispatch_session_id") or ""),
                str(receipt.get("backend_lease_id") or ""),
                invocation.binding_digest,
            ),
            invocation=invocation,
            outcome=GatewayOutcome.COMMITTED if routed_ok else GatewayOutcome.FAILED,
            result_digest=content_digest(raw_result),
            permission_consumption_id=str(permission_receipt.get("receipt_id") or ""),
            owner_epoch_before=identity.owner_epoch,
            owner_epoch_after=identity.owner_epoch,
            backend_generation=identity.generation,
            failure_code=(str(raw_result.get("error") or "") if not routed_ok else ""),
            metadata={
                "backend_action_dispatch": True,
                "backend_id": str(receipt.get("backend_id") or ""),
                "backend_kind": str(receipt.get("backend_kind") or ""),
                "backend_location": str(receipt.get("backend_location") or ""),
                "backend_lease_id": str(receipt.get("backend_lease_id") or ""),
                "envelope_id": str(receipt.get("envelope_id") or ""),
            },
        )
        self.bundle.receipt_journal.append(
            gateway_receipt,
            idempotency_key=f"terminal-action:{call.tool_call_id}",
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=routed_ok,
            summary=str(raw_result.get("summary") or "Terminal action completed"),
            output={
                **dict(raw_output),
                "backend_action_dispatch_receipt": dict(receipt),
                "gateway_receipt": gateway_receipt.safe_dict(),
            },
            error=(str(raw_result.get("error")) if raw_result.get("error") else None),
            metadata={
                **{str(key): str(value) for key, value in raw_metadata.items()},
                **self.bundle.event_projector.tool_metadata(gateway_receipt),
                "sandbox_gateway_routed": "true",
                "backend_action_dispatch_routed": "true",
                "backend_action_dispatch_receipt_schema": str(receipt.get("schema") or ""),
                "backend_action_dispatch_session_id": str(
                    receipt.get("dispatch_session_id") or ""
                ),
                "backend_action_dispatch_lease_id": str(
                    receipt.get("backend_lease_id") or ""
                ),
                "backend_action_dispatch_envelope_id": str(
                    receipt.get("envelope_id") or ""
                ),
                "sandbox_gateway_permission_receipt_id": str(
                    permission_receipt.get("receipt_id") or ""
                ),
                "permission_execution_grant_consumed": "true",
            },
        )

    def _backend_action_available(self, tool_name: str) -> bool:
        port = self.bundle.backend_action_dispatch_port
        if port is None:
            return False
        handles = getattr(port, "handles", None)
        available = getattr(port, "available", None)
        dispatch = getattr(port, "dispatch_action", None)
        return bool(
            callable(handles)
            and callable(available)
            and callable(dispatch)
            and handles(tool_name)
            and available(tool_name)
        )

    def _ensure_session(self, identity: WorkerGatewayIdentity) -> Any:
        try:
            record = self.bundle.state_store.require_session(identity.session_id)
        except Exception:  # noqa: BLE001 - absence is the create path.
            record = self.bundle.runtime.create_session(
                session_id=identity.session_id,
                run_id=identity.run_id,
                task_id=identity.task_id,
                workspace_id=identity.workspace_id,
                worker_id=identity.worker_id,
                metadata={
                    "identity_binding_digest": identity.binding_digest,
                    "workspace_root_digest": content_digest(str(self.bundle.workspace_root)),
                    "canonical_gateway_owner": "SandboxGatewayRuntime",
                },
            )
        if record.state == GatewayLifecycleState.CREATED:
            record = self.bundle.runtime.prepare_session(identity.session_id)
        if record.state not in {GatewayLifecycleState.READY, GatewayLifecycleState.BUSY}:
            raise RuntimeError(f"sandbox session is not executable: {record.state.value}")
        return record

    def _identity(self, call: ToolCall) -> WorkerGatewayIdentity:
        access = _current_access(self.bundle.workspace_edit_port)
        workspace_id = str(getattr(access, "workspace_id", "") or "")
        owner_epoch = int(getattr(access, "owner_epoch", 0) or 0)
        requested_session = str(call.metadata.get("session_id") or "")
        session_id = _filesystem_safe_session_id(
            requested_session
            or stable_identifier(
                "gateway-session",
                call.run_id,
                call.task_id,
                self.bundle.worker_id,
                workspace_id,
            )
        )
        return WorkerGatewayIdentity(
            run_id=call.run_id,
            task_id=call.task_id,
            node_id=str(call.node_id or ""),
            worker_id=self.bundle.worker_id,
            session_id=session_id,
            request_id=str(call.metadata.get("worker_request_id") or ""),
            workspace_id=workspace_id,
            owner_epoch=owner_epoch,
            backend_id="local-process",
            metadata={"tool_call_id": call.tool_call_id},
        )

    def _internal_provenance(
        self,
        identity: WorkerGatewayIdentity,
        call: ToolCall,
        content: bytes,
        *,
        kind: ProvenanceKind,
    ) -> ArtifactProvenance:
        provenance = self.bundle.provenance_registry.derive(
            kind=kind,
            source_id=identity.worker_id,
            parent_refs=tuple(
                item
                for item in (str(call.metadata.get("provenance_ref") or ""),)
                if item
            ),
            content=content,
            trust=TrustLevel.TRUSTED,
            untrusted_instructions=False,
            metadata={
                "run_id": identity.run_id,
                "task_id": identity.task_id,
                "tool_call_id": call.tool_call_id,
            },
        )
        return provenance

    def _require_artifact_port(self) -> Any:
        if self.bundle.artifact_port is None:
            raise RuntimeError("sandbox_gateway_artifact_port_unavailable")
        return self.bundle.artifact_port

    def _input_paths(self, arguments: Mapping[str, Any]) -> tuple[str, ...]:
        value = arguments.get("input_paths") or ()
        if isinstance(value, str):
            value = (value,)
        if not isinstance(value, Sequence):
            raise ValueError("input_paths must be a sequence")
        explicit = tuple(
            self.bundle.policy_runtime.assert_path(str(item)) for item in value
        )
        if not self.bundle.stage_workspace_snapshot:
            return explicit

        # File tools and local shell commands must observe one task workspace.
        # Only logical names are enumerated here; IsolationWorkspace transfers
        # every byte through GatewayFileArtifactPort and commits the resulting
        # delta through GatewayPatchPort. Runtime/cache trees are intentionally
        # excluded from the task snapshot.
        excluded_directories = {
            ".git",
            ".runtime",
            ".venv",
            "venv",
            "node_modules",
            "__pycache__",
            "dist",
            "coverage",
        }
        root = self.bundle.workspace_root.resolve()
        discovered: list[str] = []
        for current, directories, filenames in os.walk(root, followlinks=False):
            current_path = Path(current)
            directories[:] = sorted(
                name
                for name in directories
                if name not in excluded_directories
                and not current_path.joinpath(name).is_symlink()
            )
            for filename in sorted(filenames):
                path = current_path / filename
                if path.is_symlink() or not path.is_file():
                    continue
                logical = path.relative_to(root).as_posix()
                discovered.append(self.bundle.policy_runtime.assert_path(logical))
                if len(discovered) > 20_000:
                    raise SandboxGatewayError(
                        GatewayErrorCode.PATCH_REJECTED,
                        "managed workspace snapshot exceeds the sandbox staging file limit",
                        operation="gateway_stage_workspace_snapshot",
                    )
        return tuple(dict.fromkeys((*explicit, *discovered)))

    @staticmethod
    def _error(
        call: ToolCall,
        code: str,
        reason: str,
        *,
        recovery: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        public_reason = str(reason or code or "sandbox gateway rejected the tool call")[:2_000]
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary=f"{call.tool_name} was blocked by SandboxGateway",
            # ModelIteration deliberately projects summary/output/error but not
            # host metadata into the next provider observation.  Keeping the
            # actionable, bounded reason only in metadata made an autonomous
            # worker see a bare ``ValueError`` and repeat an identical rejected
            # command until its long-horizon deadline expired.
            output={
                "code": str(code),
                "reason": public_reason,
                "recovery_hint": " ".join(str(item) for item in recovery if str(item).strip())
                or "Change the tool arguments before retrying; do not repeat an identical rejected call.",
            },
            error=code,
            metadata={
                "sandbox_gateway_routed": "true",
                "sandbox_gateway_failure": "true",
                "sandbox_gateway_reason": public_reason,
                **{str(key): str(value) for key, value in dict(metadata or {}).items()},
            },
        )


def _current_access(port: Any) -> Any:
    current = getattr(port, "current_access", None)
    return current() if callable(current) else None


def _workspace_fence_digest(port: Any) -> str:
    access = _current_access(port)
    token = str(getattr(access, "fence_token", "") or "")
    return content_digest(token) if token else ""


def _content_bytes(arguments: Mapping[str, Any]) -> bytes:
    value = arguments.get("content", b"")
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    encoding = str(arguments.get("encoding") or "utf-8")
    return str(value).encode(encoding)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        selected = int(value)
    except (TypeError, ValueError):
        selected = default
    return max(minimum, min(maximum, selected))


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        selected = float(value)
    except (TypeError, ValueError):
        selected = default
    return max(minimum, min(maximum, selected))


def _failure_class(error: Exception) -> FailureClass:
    name = type(error).__name__.casefold()
    message = str(error).casefold()
    if "permission" in name or "permission" in message:
        return FailureClass.PERMISSION
    if "workspace" in name or "workspace" in message or "path" in message:
        return FailureClass.WORKSPACE
    if "timeout" in name or "timeout" in message:
        return FailureClass.TIMEOUT
    if "cancel" in name or "cancel" in message:
        return FailureClass.CANCEL
    if "policy" in name or "denied" in message:
        return FailureClass.POLICY
    if "provenance" in name or "quarantine" in message:
        return FailureClass.PROVENANCE
    return FailureClass.BACKEND


def _metadata_flag(value: Any) -> bool:
    return value is True or str(value or "").strip().lower() == "true"


def _retryable(error: Exception) -> bool:
    selected = _failure_class(error)
    return selected in {FailureClass.BACKEND, FailureClass.TIMEOUT, FailureClass.WORKSPACE}


def _recovery_actions(error: Exception) -> tuple[RecoveryAction, ...]:
    selected = _failure_class(error)
    if selected == FailureClass.PERMISSION:
        return (RecoveryAction.REQUEST_PERMISSION, RecoveryAction.REDUCE_SCOPE)
    if selected == FailureClass.WORKSPACE:
        return (RecoveryAction.REBIND_WORKSPACE, RecoveryAction.REPLAN)
    if selected == FailureClass.BACKEND:
        return (RecoveryAction.REPLACE_BACKEND, RecoveryAction.RETRY)
    if selected == FailureClass.TIMEOUT:
        return (RecoveryAction.REDUCE_SCOPE, RecoveryAction.RETRY)
    return (RecoveryAction.REPLAN,)


def _process_failure_class(termination: ProcessTermination) -> FailureClass:
    if termination == ProcessTermination.TIMED_OUT:
        return FailureClass.TIMEOUT
    if termination == ProcessTermination.CANCELLED:
        return FailureClass.CANCEL
    return FailureClass.BACKEND


def _process_recovery_hint(termination: ProcessTermination) -> str:
    if termination is not ProcessTermination.FAILED_TO_START:
        return ""
    if os.name == "nt":
        return (
            "The executable did not start. Change the structured executable/argv "
            "before retrying. Windows aliases such as pwd and ls are not standalone "
            "executables; use pwsh.exe with arguments such as -NoProfile, "
            "-NonInteractive, -Command, Get-Location (or Get-ChildItem -Force), "
            "or choose another executable available on PATH."
        )
    return (
        "The executable did not start. Change the structured executable/argv "
        "before retrying and choose an executable available on PATH."
    )


__all__ = [
    "GatewayToolExecutionRouter",
    "GatewayToolRoutingDecision",
]
