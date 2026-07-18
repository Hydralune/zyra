from __future__ import annotations

import mimetypes
import time
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence, TYPE_CHECKING

from ..executor import ToolCall, ToolResult
from .artifact_port import FileArtifactRequest
from .canonical import content_digest as gateway_content_digest
from .models import (
    CommandBudget,
    GatewayCommandEnvelope,
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
        "file_read",
        "file_write",
        "file_edit",
        "file_delete",
        "artifact_write",
    }
)


@dataclass(frozen=True, slots=True)
class GatewayToolRoutingDecision:
    handled: bool
    tool_name: str
    operation: GatewayAction | None
    requires_workspace_port: bool
    reason: str


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

    def handles(self, tool_name: str) -> bool:
        selected = str(tool_name)
        if selected == "file_read" and self.bundle.workspace_edit_port is None:
            return False
        return selected in _HANDLED_TOOLS

    def route(self, tool_name: str) -> GatewayToolRoutingDecision:
        selected = str(tool_name)
        action = {
            "shell": GatewayAction.COMMAND,
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
            requires_workspace_port=selected != "shell",
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
            identity = self._identity(call)
            if call.tool_name == "shell":
                return self._shell(
                    call,
                    identity,
                    permission_grant=permission_grant,
                    permission_authority=permission_authority,
                    permission_execution_context=permission_execution_context,
                )
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
                metadata={
                    "sandbox_gateway_failure_signal_id": signal.signal_id,
                    "sandbox_gateway_failure_signal_digest": signal.signal_digest,
                },
            )
        return self._error(call, "sandbox_gateway_tool_not_implemented", call.tool_name)

    def cancel(self, session_id: str, command_id: str, *, reason: str) -> bool:
        if self._host_runtime.cancel(command_id, reason=reason):
            return True
        return self.bundle.runtime.cancel(session_id, command_id, reason=reason)

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
        executable, argv, environment, cwd = self.bundle.policy_runtime.command_from_arguments(
            call.arguments
        )
        timeout = _bounded_float(
            call.arguments.get("timeout_seconds"),
            default=120.0,
            minimum=0.1,
            maximum=3600.0,
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
            network_profile=str(call.arguments.get("network_profile") or "offline"),
            provenance_ref=str(call.metadata.get("provenance_ref") or ""),
            idempotency_key=str(call.arguments.get("idempotency_key") or call.tool_call_id),
            causation_id=call.tool_call_id,
            correlation_id=str(call.metadata.get("correlation_id") or ""),
            metadata={
                "gateway_surface": GatewaySurface.CODE_WORKER.value,
                "structured_argv": True,
                "legacy_command_normalized": bool(call.arguments.get("command")),
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
                permission_grant=permission_grant,
                permission_authority=permission_authority,
                permission_execution_context=permission_execution_context,
            )
        self._ensure_session(identity)
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
        stdout = process.output.stdout.decode("utf-8", errors="replace")
        stderr = process.output.stderr.decode("utf-8", errors="replace")
        succeeded = process.termination == ProcessTermination.EXITED and process.return_code == 0
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
                    "stdout_digest": gateway_content_digest(process.output.stdout),
                    "stderr_digest": gateway_content_digest(process.output.stderr),
                }
            ),
            permission_consumption_id=execution.receipt.permission_consumption_id,
            command_receipt_id=execution.receipt.receipt_id,
            patch_receipt_id=execution.receipt.patch_receipt_id,
            artifact_refs=execution.receipt.artifact_refs,
            event_refs=execution.event_ids,
            owner_epoch_before=execution.receipt.owner_epoch_before,
            owner_epoch_after=execution.receipt.owner_epoch_after,
            backend_generation=execution.record.generation,
            started_at=process.started_at,
            finished_at=process.finished_at,
            failure_code=process.error_code if not succeeded else "",
            metadata={
                "termination": process.termination.value,
                "return_code": process.return_code,
                "stdout_truncated": process.output.stdout_truncated,
                "stderr_truncated": process.output.stderr_truncated,
                "combined_truncated": process.output.combined_truncated,
                "recovery_required": execution.recovery_required,
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
        metadata.update(
            {
                "return_code": str(process.return_code),
                "termination": process.termination.value,
                "output_bounded": "true",
                "process_tree_controlled": "true",
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
                "artifact_refs": list(execution.receipt.artifact_refs),
                "gateway_receipt": integration_receipt.safe_dict(),
            },
            error=None if succeeded else process.error_code or "sandbox_command_failed",
            metadata=metadata,
        )

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
        succeeded = host.termination == ProcessTermination.EXITED and host.return_code == 0
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
        logical_path = self.bundle.policy_runtime.assert_path(
            str(call.arguments.get("path") or call.arguments.get("name") or "")
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
            mount_kind=str(call.arguments.get("mount_kind") or "task"),
            executable_allowed=False,
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
            },
        )
        self.bundle.receipt_journal.append(receipt, idempotency_key=request.idempotency_key)
        ok = file_receipt.committed and not file_receipt.quarantined
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
                "content_digest": file_receipt.content_digest,
                "content_bytes": file_receipt.content_bytes,
                "transaction_id": file_receipt.transaction_id,
                "artifact_ref": file_receipt.artifact_ref,
                "quarantine_id": file_receipt.quarantine_id,
                "gateway_receipt": receipt.safe_dict(),
            },
            error=None if ok else "artifact_quarantined" if file_receipt.quarantined else "workspace_commit_failed",
            metadata=self.bundle.event_projector.tool_metadata(receipt),
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
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=bool(result.ok),
            summary=f"Deleted {logical_path} through SandboxGateway" if result.ok else f"Delete failed for {logical_path}",
            output={"path": logical_path, "gateway_receipt": receipt.safe_dict()},
            error=None if result.ok else str(result.error or "workspace_delete_failed"),
            metadata=self.bundle.event_projector.tool_metadata(receipt),
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
        return tuple(self.bundle.policy_runtime.assert_path(str(item)) for item in value)

    @staticmethod
    def _error(
        call: ToolCall,
        code: str,
        reason: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary=f"{call.tool_name} was blocked by SandboxGateway",
            error=code,
            metadata={
                "sandbox_gateway_routed": "true",
                "sandbox_gateway_failure": "true",
                "sandbox_gateway_reason": str(reason),
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


__all__ = [
    "GatewayToolExecutionRouter",
    "GatewayToolRoutingDecision",
]
