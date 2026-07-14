from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .artifact_port import GatewayFileArtifactPort
from .backends import BackendSession, SandboxBackend
from .canonical import random_nonce, token_digest
from .command_policy import StructuredCommandPolicy
from .errors import GatewayErrorCode, SandboxGatewayError
from .event_port import GatewayEventPort
from .isolation import IsolationDelta, IsolationWorkspace
from .lifecycle import SandboxLifecycle
from .models import (
    CommandReceipt,
    GatewayCommandEnvelope,
    GatewayEventKind,
    GatewayLifecycleState,
    GatewaySessionRecord,
    PatchReceipt,
    ProcessResult,
)
from .patch_port import GatewayPatchPort
from .permission_relay import GatewayPermissionRelay, PermissionTicket
from .process_budget import CancellationToken, ProcessBudgetRegistry, StreamChunk
from .receipts import ReceiptLedger
from .session_queue import SessionActorQueue
from .source_custody import assert_source_custody
from .state_store import GatewayStateStore


@dataclass(frozen=True, slots=True)
class SandboxGatewayConfig:
    state_root: Path
    interactive: bool = True
    sealed: bool = False
    enabled: bool = True
    actor_queue_timeout_seconds: float = 30.0
    ready_timeout_seconds: float = 30.0
    auto_prepare: bool = True
    commit_successful_outputs: bool = True
    preserve_failed_patch_root: Path | None = None

    def __post_init__(self) -> None:
        if self.interactive and self.sealed:
            raise ValueError("interactive and sealed modes are mutually exclusive")
        if self.actor_queue_timeout_seconds <= 0 or self.ready_timeout_seconds <= 0:
            raise ValueError("gateway timeouts must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "state_root_digest": token_digest(str(self.state_root.resolve())),
            "interactive": self.interactive,
            "sealed": self.sealed,
            "enabled": self.enabled,
            "actor_queue_timeout_seconds": self.actor_queue_timeout_seconds,
            "ready_timeout_seconds": self.ready_timeout_seconds,
            "auto_prepare": self.auto_prepare,
            "commit_successful_outputs": self.commit_successful_outputs,
            "preserve_failed_patch": self.preserve_failed_patch_root is not None,
        }


@dataclass(frozen=True, slots=True)
class SandboxCommandExecution:
    record: GatewaySessionRecord
    envelope: GatewayCommandEnvelope
    policy: Mapping[str, Any]
    permission_binding_id: str
    permission_consumption_id: str
    result: ProcessResult
    receipt: CommandReceipt
    isolation_delta: IsolationDelta | None = None
    patch_receipt: PatchReceipt | None = None
    event_ids: tuple[str, ...] = ()
    recovery_required: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result.ok and (
            self.patch_receipt is None or self.patch_receipt.ok
        )

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        return {
            "record": self.record.to_dict(),
            "envelope": self.envelope.to_dict(),
            "policy": dict(self.policy),
            "permission_binding_id": self.permission_binding_id,
            "permission_consumption_id": self.permission_consumption_id,
            "result": self.result.to_dict(include_output=include_output),
            "receipt": self.receipt.to_dict(include_output=include_output),
            "isolation_delta": (
                self.isolation_delta.to_dict() if self.isolation_delta else None
            ),
            "patch_receipt": (
                self.patch_receipt.to_dict() if self.patch_receipt else None
            ),
            "event_ids": list(self.event_ids),
            "recovery_required": self.recovery_required,
            "metadata": dict(self.metadata),
            "ok": self.ok,
        }


class SandboxGatewayRuntime:
    """Single Zyra-owned orchestration point for sandbox command and file transfer."""

    def __init__(
        self,
        config: SandboxGatewayConfig,
        *,
        state_store: GatewayStateStore,
        lifecycle: SandboxLifecycle,
        backend: SandboxBackend,
        command_policy: StructuredCommandPolicy,
        permission_relay: GatewayPermissionRelay,
        event_port: GatewayEventPort,
        receipt_ledger: ReceiptLedger,
        artifact_port: GatewayFileArtifactPort | None = None,
        patch_port: GatewayPatchPort | None = None,
        actor_queue: SessionActorQueue | None = None,
        budget_registry: ProcessBudgetRegistry | None = None,
    ) -> None:
        self.config = config
        self.state_store = state_store
        self.lifecycle = lifecycle
        self.backend = backend
        self.command_policy = command_policy
        self.permission_relay = permission_relay
        self.event_port = event_port
        self.receipt_ledger = receipt_ledger
        self.artifact_port = artifact_port
        self.patch_port = patch_port
        self.actor_queue = actor_queue or SessionActorQueue()
        self.budget_registry = budget_registry or ProcessBudgetRegistry()
        self._backend_sessions: dict[str, BackendSession] = {}
        self._backend_lock = threading.RLock()
        assert_source_custody()

    def create_session(
        self,
        *,
        session_id: str,
        run_id: str,
        task_id: str,
        workspace_id: str,
        worker_id: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GatewaySessionRecord:
        self._require_enabled()
        record = self.lifecycle.create(
            session_id=session_id,
            run_id=run_id,
            task_id=task_id,
            workspace_id=workspace_id,
            worker_id=worker_id,
            backend_id=self.backend.backend_id,
            metadata={
                **dict(metadata or {}),
                "gateway_owner": "SandboxGatewayRuntime",
                "permission_owner": "ToolPermissionRuntime",
                "workspace_owner": "WorkspaceManagerRuntime",
            },
        )
        event = self.event_port.emit(
            record,
            GatewayEventKind.SESSION_CREATED,
            {
                "state": record.state.value,
                "backend_id": record.backend_id,
                "workspace_id": record.workspace_id,
            },
        )
        if self.config.auto_prepare and record.state is GatewayLifecycleState.CREATED:
            record = self.prepare_session(record.session_id)
        return record

    def prepare_session(self, session_id: str) -> GatewaySessionRecord:
        self._require_enabled()
        with self.actor_queue.enter(
            session_id,
            timeout_seconds=self.config.actor_queue_timeout_seconds,
        ):
            record = self.state_store.require_session(session_id)
            if record.state is GatewayLifecycleState.READY:
                return record
            if record.state is not GatewayLifecycleState.PREPARING:
                record = self.lifecycle.prepare(session_id)
            try:
                backend_session = self.backend.prepare(record)
            except Exception as error:
                failed, _ = self.lifecycle.transition(
                    session_id,
                    GatewayLifecycleState.FAILED,
                    reason="backend preparation failed",
                    failure_code=GatewayErrorCode.BACKEND_UNAVAILABLE.value,
                    failure_reason=type(error).__name__,
                )
                self.event_port.emit(
                    failed,
                    GatewayEventKind.SESSION_STATE_CHANGED,
                    {
                        "state": failed.state.value,
                        "failure_code": failed.failure_code,
                    },
                )
                raise
            with self._backend_lock:
                self._backend_sessions[session_id] = backend_session
            ready = self.lifecycle.ready(session_id)
            self.event_port.emit(
                ready,
                GatewayEventKind.SESSION_STATE_CHANGED,
                {
                    "state": ready.state.value,
                    "backend": backend_session.to_public_dict(),
                },
            )
            return ready

    def execute(
        self,
        envelope: GatewayCommandEnvelope,
        *,
        input_paths: Iterable[str] = (),
        commit_outputs: bool | None = None,
    ) -> SandboxCommandExecution:
        self._require_enabled()
        staged_paths = tuple(input_paths)
        if envelope.session_id not in {
            record.session_id for record in self.state_store.list_sessions()
        }:
            raise SandboxGatewayError(
                GatewayErrorCode.SESSION_NOT_FOUND,
                "command references an unknown sandbox session",
                operation="gateway_execute",
            )
        with self.actor_queue.enter(
            envelope.session_id,
            timeout_seconds=self.config.actor_queue_timeout_seconds,
        ):
            record = self.lifecycle.wait_ready(
                envelope.session_id,
                timeout_seconds=self.config.ready_timeout_seconds,
            )
            self._assert_envelope_session(record, envelope)
            backend_session = self._backend_session(record)
            proposed = self.event_port.emit(
                record,
                GatewayEventKind.COMMAND_PROPOSED,
                {
                    "command_id": envelope.command_id,
                    "command_digest": envelope.identity_digest,
                    "executable": envelope.executable,
                    "argv_count": len(envelope.argv),
                    "workspace_id": envelope.workspace_id,
                    "owner_epoch": envelope.owner_epoch,
                },
                causation_id=envelope.causation_id,
                correlation_id=envelope.correlation_id,
            )
            policy = self.command_policy.evaluate(
                envelope,
                sealed=self.config.sealed,
            )
            policy_event = self.event_port.emit(
                record,
                GatewayEventKind.COMMAND_POLICY,
                policy.to_dict(),
                causation_id=proposed.event_id,
                correlation_id=envelope.correlation_id,
            )
            ticket = self._issue_permission(record, envelope, policy, policy_event.event_id)
            fence_token = random_nonce()
            busy, lease = self.lifecycle.begin_command(
                record.session_id,
                envelope.command_id,
                owner_id=record.worker_id,
                fence_token=fence_token,
            )
            consumed = None
            cancellation: CancellationToken | None = None
            isolation: IsolationWorkspace | None = None
            baseline = None
            event_ids = [proposed.event_id, policy_event.event_id]
            try:
                consumed = self.permission_relay.consume(ticket, envelope)
                permission_event = self.event_port.emit(
                    busy,
                    GatewayEventKind.COMMAND_PERMISSION,
                    {
                        "binding_id": consumed.binding_id,
                        "consumption_id": consumed.consumption_id,
                        "effect": consumed.effect.value,
                        "request_fingerprint": consumed.request_fingerprint,
                    },
                    causation_id=policy_event.event_id,
                    correlation_id=envelope.correlation_id,
                )
                event_ids.append(permission_event.event_id)
                isolation = IsolationWorkspace(backend_session)
                if staged_paths:
                    if self.artifact_port is None:
                        raise SandboxGatewayError(
                            GatewayErrorCode.WORKSPACE_GATEWAY_UNAVAILABLE,
                            "input staging requires GatewayFileArtifactPort",
                            operation="gateway_execute",
                        )
                    baseline = isolation.stage(self.artifact_port, staged_paths)
                else:
                    baseline = isolation.capture()
                    isolation._baseline = baseline  # noqa: SLF001 - same runtime custody.
                started = self.event_port.emit(
                    busy,
                    GatewayEventKind.COMMAND_STARTED,
                    {
                        "command_id": envelope.command_id,
                        "lease_id": lease.lease_id,
                        "backend_id": backend_session.backend_id,
                        "isolation_baseline": baseline.manifest_digest,
                    },
                    causation_id=permission_event.event_id,
                    correlation_id=envelope.correlation_id,
                )
                event_ids.append(started.event_id)
                cancellation = self.budget_registry.register(envelope.command_id)
                chunks: list[StreamChunk] = []

                def on_chunk(chunk: StreamChunk) -> None:
                    chunks.append(chunk)

                result = self.backend.execute(
                    backend_session,
                    envelope,
                    cancellation,
                    on_chunk=on_chunk,
                )
                for chunk in chunks:
                    output_event = self.event_port.emit(
                        busy,
                        GatewayEventKind.COMMAND_OUTPUT,
                        {
                            "command_id": envelope.command_id,
                            "stream": chunk.stream,
                            "sequence": chunk.sequence,
                            "bytes": len(chunk.content),
                            "content": chunk.content.decode("utf-8", errors="replace"),
                        },
                        causation_id=started.event_id,
                        correlation_id=envelope.correlation_id,
                    )
                    event_ids.append(output_event.event_id)
                delta, patch_receipt = self._collect_and_commit(
                    record,
                    envelope,
                    isolation,
                    baseline,
                    result,
                    commit_outputs=(
                        self.config.commit_successful_outputs
                        if commit_outputs is None
                        else bool(commit_outputs)
                    ),
                )
                final_record = self.lifecycle.finish_command(
                    record.session_id,
                    lease.lease_id,
                    owner_id=record.worker_id,
                    fence_token=fence_token,
                    failed=not result.ok,
                    failure_code=result.error_code,
                    failure_reason=result.cancellation_reason,
                )
                finished = self.event_port.emit(
                    final_record,
                    GatewayEventKind.COMMAND_FINISHED,
                    {
                        "command_id": envelope.command_id,
                        "result": result.to_dict(),
                        "patch_receipt": (
                            patch_receipt.to_dict() if patch_receipt else None
                        ),
                        "isolation_delta": delta.to_dict() if delta else None,
                    },
                    causation_id=started.event_id,
                    correlation_id=envelope.correlation_id,
                )
                event_ids.append(finished.event_id)
                receipt = self.receipt_ledger.command(
                    envelope,
                    policy,
                    consumed,
                    result,
                    patch_receipt=patch_receipt,
                    event_refs=event_ids,
                    owner_epoch_after=(
                        patch_receipt.owner_epoch_after
                        if patch_receipt
                        else envelope.owner_epoch
                    ),
                    metadata={
                        "backend_id": backend_session.backend_id,
                        "isolation_delta": delta.to_dict() if delta else None,
                    },
                )
                return SandboxCommandExecution(
                    record=final_record,
                    envelope=envelope,
                    policy=policy.to_dict(),
                    permission_binding_id=consumed.binding_id,
                    permission_consumption_id=consumed.consumption_id,
                    result=result,
                    receipt=receipt,
                    isolation_delta=delta,
                    patch_receipt=patch_receipt,
                    event_ids=tuple(event_ids),
                    recovery_required=not result.ok,
                    metadata={
                        "backend_id": backend_session.backend_id,
                        "gateway_owner": "SandboxGatewayRuntime",
                    },
                )
            except Exception as error:
                self._finish_failed_lease(
                    record,
                    lease.lease_id,
                    fence_token,
                    error,
                )
                rejected_record = self.state_store.require_session(record.session_id)
                rejected = self.event_port.emit(
                    rejected_record,
                    GatewayEventKind.COMMAND_REJECTED,
                    {
                        "command_id": envelope.command_id,
                        "error_code": str(getattr(error, "code", GatewayErrorCode.INTERNAL.value)),
                        "error_type": type(error).__name__,
                    },
                    causation_id=policy_event.event_id,
                    correlation_id=envelope.correlation_id,
                )
                self.event_port.emit_recovery(
                    rejected_record,
                    reason="sandbox command failed before a complete receipt was committed",
                    error_code=str(getattr(error, "code", GatewayErrorCode.INTERNAL.value)),
                    alternatives=(
                        "replan with a structured read-only command",
                        "restore permission or workspace owner state",
                        "inspect the preserved isolation patch",
                    ),
                    causation_id=rejected.event_id,
                )
                raise
            finally:
                if cancellation is not None:
                    self.budget_registry.release(envelope.command_id)

    def cancel(
        self,
        session_id: str,
        command_id: str,
        *,
        reason: str,
    ) -> bool:
        self._require_enabled()
        record = self.state_store.require_session(session_id)
        if record.active_command_id != command_id:
            return False
        token_cancelled = self.budget_registry.cancel(command_id, reason)
        backend_cancelled = self.backend.cancel(command_id, reason)
        if token_cancelled or backend_cancelled:
            self.event_port.emit(
                record,
                GatewayEventKind.PROCESS_CANCELLED,
                {
                    "command_id": command_id,
                    "reason": reason,
                    "token_cancelled": token_cancelled,
                    "backend_cancelled": backend_cancelled,
                },
            )
        return token_cancelled or backend_cancelled

    def close_session(self, session_id: str) -> GatewaySessionRecord:
        self._require_enabled()
        with self.actor_queue.enter(
            session_id,
            timeout_seconds=self.config.actor_queue_timeout_seconds,
        ):
            record = self.state_store.require_session(session_id)
            if record.active_command_id:
                self.cancel(
                    session_id,
                    record.active_command_id,
                    reason="session close requested",
                )
            backend_session = self._backend_sessions.get(session_id)
            if backend_session is not None:
                self.backend.cleanup(backend_session)
                with self._backend_lock:
                    self._backend_sessions.pop(session_id, None)
            closed = self.lifecycle.close(session_id)
            self.event_port.emit(
                closed,
                GatewayEventKind.SESSION_CLOSED,
                {"state": closed.state.value},
            )
            return closed

    def recover_on_startup(self) -> tuple[GatewaySessionRecord, ...]:
        self._require_enabled()
        recovered = self.lifecycle.recover_interrupted()
        results: list[GatewaySessionRecord] = []
        for record in recovered:
            if record.state is GatewayLifecycleState.PREPARING:
                try:
                    backend_session = (
                        self.backend.recover(record)
                        if hasattr(self.backend, "recover")
                        else self.backend.prepare(record)
                    )
                except Exception:
                    results.append(record)
                    continue
                with self._backend_lock:
                    self._backend_sessions[record.session_id] = backend_session
                ready = self.lifecycle.ready(record.session_id)
                self.event_port.emit(
                    ready,
                    GatewayEventKind.SESSION_RECOVERED,
                    {
                        "state": ready.state.value,
                        "backend": backend_session.to_public_dict(),
                    },
                )
                results.append(ready)
            else:
                results.append(record)
        return tuple(results)

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "runtime_id": "SandboxGatewayRuntime",
            "config": self.config.to_dict(),
            "backend": (
                self.backend.descriptor()
                if hasattr(self.backend, "descriptor")
                else {"backend_id": self.backend.backend_id}
            ),
            "command_policy": self.command_policy.descriptor(),
            "artifact_port": (
                self.artifact_port.descriptor() if self.artifact_port else None
            ),
            "patch_port": self.patch_port.descriptor() if self.patch_port else None,
            "state_custody": self.state_store.custody_descriptor(),
            "actor_queue": self.actor_queue.descriptor(),
            "process_budgets": self.budget_registry.descriptor(),
            "canonical_gateway_count": 1,
            "vendor_runtime_required": False,
        }

    def _issue_permission(
        self,
        record: GatewaySessionRecord,
        envelope: GatewayCommandEnvelope,
        policy: Any,
        causation_id: str,
    ) -> PermissionTicket:
        try:
            return self.permission_relay.issue(
                envelope,
                policy,
                interactive=self.config.interactive,
                sealed=self.config.sealed,
            )
        except SandboxGatewayError as error:
            self.event_port.emit(
                record,
                GatewayEventKind.COMMAND_REJECTED,
                {
                    "command_id": envelope.command_id,
                    "error": error.to_dict(),
                },
                causation_id=causation_id,
                correlation_id=envelope.correlation_id,
            )
            raise

    def _collect_and_commit(
        self,
        record: GatewaySessionRecord,
        envelope: GatewayCommandEnvelope,
        isolation: IsolationWorkspace,
        baseline: Any,
        result: ProcessResult,
        *,
        commit_outputs: bool,
    ) -> tuple[IsolationDelta | None, PatchReceipt | None]:
        after = isolation.capture()
        delta, patch_set = isolation.build_patch(
            session_id=record.session_id,
            base_workspace_id=envelope.workspace_id,
            base_owner_epoch=envelope.owner_epoch,
            idempotency_key=(
                envelope.idempotency_key or f"command-output:{envelope.command_id}"
            ),
            command_id=envelope.command_id,
            before=baseline,
            after=after,
            reason="sandbox command output delta",
            provenance_refs=(
                (envelope.provenance_ref,)
                if envelope.provenance_ref
                else ()
            ),
        )
        if not delta.changed:
            return delta, None
        if not result.ok or not commit_outputs:
            if self.config.preserve_failed_patch_root is not None:
                isolation.preserve_failure_patch(
                    destination=self.config.preserve_failed_patch_root,
                    delta=delta,
                    patch_set=patch_set,
                )
            return delta, None
        if self.patch_port is None:
            raise SandboxGatewayError(
                GatewayErrorCode.WORKSPACE_GATEWAY_UNAVAILABLE,
                "sandbox produced workspace changes but GatewayPatchPort is unavailable",
                operation="collect_outputs",
            )
        prepared = self.patch_port.prepare(patch_set)
        prepared_event = self.event_port.emit(
            record,
            GatewayEventKind.PATCH_PREPARED,
            {
                "command_id": envelope.command_id,
                "patch_set_id": patch_set.patch_set_id,
                "mutation_count": len(patch_set.mutations),
                "content_bytes": patch_set.content_bytes,
                "base_owner_epoch": patch_set.base_owner_epoch,
            },
            causation_id=envelope.command_id,
            correlation_id=envelope.correlation_id,
        )
        patch_receipt = self.patch_port.commit(prepared)
        self.event_port.emit(
            record,
            GatewayEventKind.PATCH_COMMITTED,
            {
                "command_id": envelope.command_id,
                "patch_set_id": patch_set.patch_set_id,
                "receipt": patch_receipt.to_dict(),
            },
            causation_id=prepared_event.event_id,
            correlation_id=envelope.correlation_id,
        )
        self.receipt_ledger.patch(
            patch_receipt,
            idempotency_key=f"patch:{patch_set.idempotency_key}",
        )
        return delta, patch_receipt

    def _backend_session(self, record: GatewaySessionRecord) -> BackendSession:
        with self._backend_lock:
            session = self._backend_sessions.get(record.session_id)
        if session is not None:
            return session
        if hasattr(self.backend, "get"):
            try:
                session = self.backend.get(record.session_id)
            except Exception:
                session = None
        if session is None:
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "sandbox backend session is not available in this process",
                operation="gateway_execute",
                retryable=True,
            )
        with self._backend_lock:
            self._backend_sessions[record.session_id] = session
        return session

    def _finish_failed_lease(
        self,
        record: GatewaySessionRecord,
        lease_id: str,
        fence_token: str,
        error: Exception,
    ) -> None:
        try:
            self.lifecycle.finish_command(
                record.session_id,
                lease_id,
                owner_id=record.worker_id,
                fence_token=fence_token,
                failed=True,
                failure_code=str(
                    getattr(error, "code", GatewayErrorCode.INTERNAL.value)
                ),
                failure_reason=type(error).__name__,
            )
        except Exception:
            pass

    @staticmethod
    def _assert_envelope_session(
        record: GatewaySessionRecord,
        envelope: GatewayCommandEnvelope,
    ) -> None:
        if (
            record.run_id != envelope.run_id
            or record.task_id != envelope.task_id
            or record.worker_id != envelope.worker_id
            or (
                envelope.workspace_id
                and record.workspace_id != envelope.workspace_id
            )
        ):
            raise SandboxGatewayError(
                GatewayErrorCode.INVALID_REQUEST,
                "command envelope does not match sandbox session custody",
                operation="gateway_execute",
            )

    def _require_enabled(self) -> None:
        if not self.config.enabled:
            raise SandboxGatewayError(
                GatewayErrorCode.BACKEND_UNAVAILABLE,
                "SandboxGatewayRuntime is disabled; direct execution fallback is forbidden",
                operation="sandbox_gateway",
            )
