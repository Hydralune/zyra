from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import (
    CheckpointPhase,
    CheckpointReceipt,
    CheckpointWrite,
    InFlightMessage,
    PendingRequestInfo,
    PendingWriteState,
    RecoveryCheckpoint,
    RecoveryRefs,
    SideEffectFence,
    SideEffectState,
    recovery_id,
    stable_digest,
    utc_now,
)
from .delta_journal import BranchDeltaBuilder, DeterministicCommitRuntime
from .safe_codec import CheckpointCodecError, SafeCheckpointCodec
from .store import RecoveryPlanStore, RecoveryReplayConflict, RecoveryStoreConflict


class CheckpointRuntimeError(RuntimeError):
    pass


class CheckpointSignatureMismatch(CheckpointRuntimeError):
    pass


class CheckpointOwnerMismatch(CheckpointRuntimeError):
    pass


class CheckpointResumeRejected(CheckpointRuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CheckpointCommitRequest:
    refs: RecoveryRefs
    workflow_signature: str
    graph_signature: str
    topology_signature: str
    owner_refs: Mapping[str, Any]
    version_refs: Mapping[str, Any]
    committed_refs: tuple[Mapping[str, Any], ...] = ()
    committed_writes: tuple[CheckpointWrite, ...] = ()
    pending_writes: tuple[CheckpointWrite, ...] = ()
    in_flight_messages: tuple[InFlightMessage, ...] = ()
    pending_requests: tuple[PendingRequestInfo, ...] = ()
    completed_step_ids: tuple[str, ...] = ()
    processed_response_ids: tuple[str, ...] = ()
    side_effect_fence_keys: tuple[str, ...] = ()
    state_payload: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    expected_revision: int | None = None


@dataclass(frozen=True, slots=True)
class ResumeExpectations:
    run_id: str
    task_id: str
    session_id: str
    workflow_signature: str
    graph_signature: str
    topology_signature: str
    owner_refs: Mapping[str, Any] = field(default_factory=dict)
    version_refs: Mapping[str, Any] = field(default_factory=dict)
    allow_version_advancement: tuple[str, ...] = ()
    required_completed_step_ids: tuple[str, ...] = ()
    required_fence_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResumeWorkset:
    checkpoint: RecoveryCheckpoint
    executable_step_ids: tuple[str, ...]
    bypassed_step_ids: tuple[str, ...]
    deliverable_messages: tuple[InFlightMessage, ...]
    skipped_message_ids: tuple[str, ...]
    pending_requests: tuple[PendingRequestInfo, ...]
    skipped_response_ids: tuple[str, ...]
    preserved_fence_keys: tuple[str, ...]
    state_payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint.to_dict(),
            "executable_step_ids": list(self.executable_step_ids),
            "bypassed_step_ids": list(self.bypassed_step_ids),
            "deliverable_messages": [item.to_dict() for item in self.deliverable_messages],
            "skipped_message_ids": list(self.skipped_message_ids),
            "pending_requests": [item.to_dict() for item in self.pending_requests],
            "skipped_response_ids": list(self.skipped_response_ids),
            "preserved_fence_keys": list(self.preserved_fence_keys),
            "state_payload": copy.deepcopy(dict(self.state_payload)),
        }


@dataclass(frozen=True, slots=True)
class OwnerResumeReceipt:
    owner: str
    operation: str
    receipt_ref: str
    changed: bool
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "operation": self.operation,
            "receipt_ref": self.receipt_ref,
            "changed": self.changed,
            "payload": copy.deepcopy(dict(self.payload)),
        }


class ResumeOwnerPort(Protocol):
    owner: str

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class CallbackResumeOwner:
    def __init__(
        self,
        owner: str,
        callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    ) -> None:
        self.owner = owner
        self.callback = callback

    def resume(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        result = self.callback(copy.deepcopy(dict(request)))
        if not isinstance(result, Mapping):
            raise CheckpointResumeRejected(f"{self.owner} returned a non-mapping resume receipt")
        return copy.deepcopy(dict(result))


class CheckpointCommitRuntime:
    """Build and atomically commit versioned recovery checkpoints."""

    def __init__(self, store: RecoveryPlanStore, *, codec: SafeCheckpointCodec | None = None) -> None:
        self.store = store
        self.codec = codec or store.codec
        self.deltas = DeterministicCommitRuntime(store)

    def commit(self, request: CheckpointCommitRequest) -> tuple[RecoveryCheckpoint, CheckpointReceipt, bool]:
        self._validate_request(request)
        head = self.store.checkpoint_head(request.refs.task_id)
        expected_revision = request.expected_revision
        if expected_revision is None:
            expected_revision = head.commit_revision if head else 0
        if head is not None:
            self._validate_lineage_scope(head, request)
            parent_id = head.checkpoint_id
            ancestry = (*head.ancestry, head.checkpoint_id)
            iteration = head.iteration + 1
        else:
            if expected_revision != 0:
                raise RecoveryStoreConflict("initial checkpoint expected revision must be zero")
            parent_id = ""
            ancestry = ()
            iteration = 0
        committed_at = utc_now()
        checkpoint_id = self._checkpoint_id(request, parent_id, expected_revision + 1)
        checkpoint = RecoveryCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id=request.refs.run_id,
            task_id=request.refs.task_id,
            session_id=request.refs.session_id,
            workflow_signature=request.workflow_signature,
            graph_signature=request.graph_signature,
            topology_signature=request.topology_signature,
            owner_refs=copy.deepcopy(dict(request.owner_refs)),
            version_refs=copy.deepcopy(dict(request.version_refs)),
            committed_refs=tuple(copy.deepcopy(dict(item)) for item in request.committed_refs),
            phase=CheckpointPhase.COMMITTED,
            parent_checkpoint_id=parent_id,
            ancestry=ancestry,
            iteration=iteration,
            commit_revision=expected_revision + 1,
            pending_writes=tuple(request.pending_writes),
            committed_writes=tuple(request.committed_writes),
            in_flight_messages=tuple(request.in_flight_messages),
            pending_requests=tuple(request.pending_requests),
            completed_step_ids=tuple(request.completed_step_ids),
            processed_response_ids=tuple(request.processed_response_ids),
            side_effect_fence_keys=tuple(request.side_effect_fence_keys),
            state_payload=copy.deepcopy(dict(request.state_payload)),
            created_at=committed_at,
            committed_at=committed_at,
            metadata={
                **copy.deepcopy(dict(request.metadata)),
                "codec": "versioned-json-allowlist",
                "pickle_allowed": False,
                "atomic_store": "sqlite-begin-immediate",
                "pending_committed_separated": True,
                "source_semantics": "langgraph-narrow-conformance",
                "graph_runtime_migrated": False,
            },
        )
        self.codec.decode(self.codec.encode(checkpoint))
        receipt = CheckpointReceipt(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=checkpoint.run_id,
            task_id=checkpoint.task_id,
            commit_revision=checkpoint.commit_revision,
            phase=CheckpointPhase.COMMITTED,
            signature=checkpoint.signature,
            content_digest=checkpoint.content_digest,
            applied_write_ids=tuple(item.write_id for item in checkpoint.committed_writes),
            pending_write_ids=tuple(item.write_id for item in checkpoint.pending_writes),
            bypassed_step_ids=tuple(checkpoint.completed_step_ids),
            skipped_response_ids=tuple(checkpoint.processed_response_ids),
            fenced_effect_keys=tuple(checkpoint.side_effect_fence_keys),
            metadata={
                "parent_checkpoint_id": checkpoint.parent_checkpoint_id,
                "state_owner": "python.RecoveryPlanStore",
                "exact_resume_contract": True,
            },
        )
        return self.store.commit_checkpoint(
            checkpoint,
            expected_revision=expected_revision,
            receipt=receipt,
        )

    def promote_pending(
        self,
        checkpoint_id: str,
        *,
        write_ids: Sequence[str],
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[RecoveryCheckpoint, CheckpointReceipt]:
        checkpoint = self.store.checkpoint(checkpoint_id)
        if checkpoint is None:
            raise CheckpointRuntimeError(f"checkpoint not found: {checkpoint_id}")
        selected_ids = set(str(item) for item in write_ids)
        if not selected_ids:
            raise CheckpointRuntimeError("at least one pending write id is required")
        pending_map = {item.write_id: item for item in checkpoint.pending_writes}
        missing = selected_ids - set(pending_map)
        if missing:
            raise CheckpointRuntimeError(f"pending writes not found: {sorted(missing)}")
        promoted = tuple(
            CheckpointWrite(
                write_id=item.write_id,
                task_key=item.task_key,
                channel=item.channel,
                value=copy.deepcopy(item.value),
                state=PendingWriteState.COMMITTED,
                sequence=item.sequence,
                writer_id=item.writer_id,
                idempotency_key=item.idempotency_key,
                created_at=item.created_at,
                metadata={**dict(item.metadata), "promoted_from": checkpoint.checkpoint_id},
            )
            for item in checkpoint.pending_writes
            if item.write_id in selected_ids
        )
        remaining = tuple(item for item in checkpoint.pending_writes if item.write_id not in selected_ids)
        request = CheckpointCommitRequest(
            refs=RecoveryRefs(
                run_id=checkpoint.run_id,
                task_id=checkpoint.task_id,
                session_id=checkpoint.session_id,
                checkpoint_id=checkpoint.checkpoint_id,
            ),
            workflow_signature=checkpoint.workflow_signature,
            graph_signature=checkpoint.graph_signature,
            topology_signature=checkpoint.topology_signature,
            owner_refs=dict(checkpoint.owner_refs),
            version_refs={
                **dict(checkpoint.version_refs),
                "pending_write_promotion_parent": checkpoint.checkpoint_id,
            },
            committed_refs=tuple(checkpoint.committed_refs),
            committed_writes=(*checkpoint.committed_writes, *promoted),
            pending_writes=remaining,
            in_flight_messages=tuple(checkpoint.in_flight_messages),
            pending_requests=tuple(checkpoint.pending_requests),
            completed_step_ids=tuple(checkpoint.completed_step_ids),
            processed_response_ids=tuple(checkpoint.processed_response_ids),
            side_effect_fence_keys=tuple(checkpoint.side_effect_fence_keys),
            state_payload=dict(checkpoint.state_payload),
            metadata={**dict(checkpoint.metadata), **dict(metadata or {}), "promotion_write_ids": sorted(selected_ids)},
            expected_revision=checkpoint.commit_revision,
        )
        result, receipt, _ = self.commit(request)
        return result, receipt

    def branch(
        self,
        *,
        task_id: str,
        owner: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> BranchDeltaBuilder:
        checkpoint = self.store.checkpoint_head(task_id)
        if checkpoint is None:
            raise CheckpointRuntimeError(f"task has no committed checkpoint: {task_id}")
        return BranchDeltaBuilder(checkpoint, owner=owner, metadata=metadata)

    @staticmethod
    def _validate_request(request: CheckpointCommitRequest) -> None:
        if not request.refs.session_id:
            raise CheckpointRuntimeError("checkpoint commit requires session_id")
        for name in ("workflow_signature", "graph_signature", "topology_signature"):
            if not str(getattr(request, name) or "").strip():
                raise CheckpointRuntimeError(f"checkpoint commit requires {name}")
        if not request.owner_refs:
            raise CheckpointRuntimeError("checkpoint commit requires owner_refs")
        if not request.version_refs:
            raise CheckpointRuntimeError("checkpoint commit requires version_refs")
        pending_ids = {item.write_id for item in request.pending_writes}
        committed_ids = {item.write_id for item in request.committed_writes}
        if pending_ids & committed_ids:
            raise CheckpointRuntimeError("pending and committed checkpoint writes overlap")
        if any(item.state is not PendingWriteState.PENDING for item in request.pending_writes):
            raise CheckpointRuntimeError("pending write list contains a non-pending write")
        if any(item.state is not PendingWriteState.COMMITTED for item in request.committed_writes):
            raise CheckpointRuntimeError("committed write list contains a non-committed write")
        response_ids = set(request.processed_response_ids)
        if len(response_ids) != len(request.processed_response_ids):
            raise CheckpointRuntimeError("processed response ids must be unique")
        fence_keys = set(request.side_effect_fence_keys)
        if len(fence_keys) != len(request.side_effect_fence_keys):
            raise CheckpointRuntimeError("side-effect fence keys must be unique")

    @staticmethod
    def _validate_lineage_scope(head: RecoveryCheckpoint, request: CheckpointCommitRequest) -> None:
        if head.run_id != request.refs.run_id or head.task_id != request.refs.task_id:
            raise CheckpointRuntimeError("checkpoint head scope differs from commit request")
        if head.session_id != request.refs.session_id:
            raise CheckpointRuntimeError("checkpoint session identity cannot change inside a lineage")
        if head.workflow_signature != request.workflow_signature:
            raise CheckpointSignatureMismatch("workflow signature changed without replan")

    @staticmethod
    def _checkpoint_id(request: CheckpointCommitRequest, parent_id: str, revision: int) -> str:
        return "recoverycheckpoint:" + stable_digest({
            "run_id": request.refs.run_id,
            "task_id": request.refs.task_id,
            "session_id": request.refs.session_id,
            "parent": parent_id,
            "revision": revision,
            "workflow": request.workflow_signature,
            "graph": request.graph_signature,
            "topology": request.topology_signature,
            "committed_writes": [item.to_dict() for item in request.committed_writes],
            "pending_writes": [item.to_dict() for item in request.pending_writes],
            "state": copy.deepcopy(dict(request.state_payload)),
        })[:40]


class SideEffectFenceRuntime:
    def __init__(self, store: RecoveryPlanStore) -> None:
        self.store = store

    def reserve(
        self,
        *,
        run_id: str,
        task_id: str,
        operation: str,
        request: Mapping[str, Any],
        idempotency_key: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[SideEffectFence, bool]:
        request_digest = stable_digest(copy.deepcopy(dict(request)))
        fence_key = "effect:" + stable_digest({
            "run_id": run_id,
            "task_id": task_id,
            "operation": operation,
            "idempotency_key": idempotency_key,
        })[:48]
        fence = SideEffectFence(
            fence_key=fence_key,
            run_id=run_id,
            task_id=task_id,
            operation=operation,
            state=SideEffectState.RESERVED,
            request_digest=request_digest,
            metadata={**dict(metadata or {}), "idempotency_key": idempotency_key},
        )
        return self.store.reserve_side_effect(fence)

    def start(self, fence_key: str) -> SideEffectFence:
        current = self._require(fence_key)
        if current.state is SideEffectState.COMMITTED:
            return current
        if current.state is SideEffectState.STARTED:
            return current
        return self.store.transition_side_effect(
            fence_key,
            expected_revision=current.revision,
            state=SideEffectState.STARTED,
        )

    def commit(
        self,
        fence_key: str,
        *,
        response: Mapping[str, Any],
        receipt_ref: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> SideEffectFence:
        current = self._require(fence_key)
        response_digest = stable_digest(copy.deepcopy(dict(response)))
        if current.state is SideEffectState.COMMITTED:
            if current.response_digest != response_digest or current.receipt_ref != receipt_ref:
                raise RecoveryReplayConflict("committed side effect was replayed with a different response")
            return current
        if current.state is SideEffectState.RESERVED:
            current = self.start(fence_key)
        return self.store.transition_side_effect(
            fence_key,
            expected_revision=current.revision,
            state=SideEffectState.COMMITTED,
            response_digest=response_digest,
            receipt_ref=receipt_ref,
            metadata=metadata,
        )

    def fail(self, fence_key: str, *, error_code: str, retryable: bool) -> SideEffectFence:
        current = self._require(fence_key)
        if current.state is SideEffectState.COMMITTED:
            raise RecoveryReplayConflict("cannot fail an already committed side effect")
        if current.state is SideEffectState.RESERVED:
            current = self.start(fence_key)
        return self.store.transition_side_effect(
            fence_key,
            expected_revision=current.revision,
            state=SideEffectState.FAILED,
            metadata={"error_code": error_code, "retryable": retryable},
        )

    def execute_once(
        self,
        *,
        run_id: str,
        task_id: str,
        operation: str,
        request: Mapping[str, Any],
        idempotency_key: str,
        callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        receipt_field: str = "receipt_id",
    ) -> tuple[Mapping[str, Any], SideEffectFence, bool]:
        fence, created = self.reserve(
            run_id=run_id,
            task_id=task_id,
            operation=operation,
            request=request,
            idempotency_key=idempotency_key,
        )
        if not created and fence.state is SideEffectState.COMMITTED:
            return {
                "replayed": True,
                "receipt_ref": fence.receipt_ref,
                "response_digest": fence.response_digest,
            }, fence, False
        started = self.start(fence.fence_key)
        try:
            response = callback(copy.deepcopy(dict(request)))
            if not isinstance(response, Mapping):
                raise CheckpointRuntimeError(f"{operation} owner returned a non-mapping receipt")
            result = copy.deepcopy(dict(response))
            receipt_ref = str(result.get(receipt_field) or result.get("id") or recovery_id("ownerreceipt"))
            committed = self.commit(
                started.fence_key,
                response=result,
                receipt_ref=receipt_ref,
            )
            return result, committed, True
        except Exception as error:
            self.fail(started.fence_key, error_code=type(error).__name__, retryable=False)
            raise

    def _require(self, fence_key: str) -> SideEffectFence:
        current = self.store.side_effect_fence(fence_key)
        if current is None:
            raise CheckpointRuntimeError(f"side-effect fence not found: {fence_key}")
        return current


class CheckpointResumeBridge:
    """Validate exact-resume boundaries and call existing canonical owners."""

    def __init__(
        self,
        store: RecoveryPlanStore,
        *,
        session_owner: ResumeOwnerPort | None = None,
        compact_owner: ResumeOwnerPort | None = None,
        worker_owner: ResumeOwnerPort | None = None,
        graph_owner: ResumeOwnerPort | None = None,
    ) -> None:
        self.store = store
        self.session_owner = session_owner
        self.compact_owner = compact_owner
        self.worker_owner = worker_owner
        self.graph_owner = graph_owner
        self.effects = SideEffectFenceRuntime(store)

    def prepare(
        self,
        checkpoint_id: str,
        expectations: ResumeExpectations,
        *,
        candidate_step_ids: Sequence[str] = (),
    ) -> ResumeWorkset:
        checkpoint = self.store.checkpoint(checkpoint_id)
        if checkpoint is None:
            raise CheckpointResumeRejected(f"checkpoint does not exist: {checkpoint_id}")
        self._validate_expectations(checkpoint, expectations)
        completed = set(checkpoint.completed_step_ids)
        executable = tuple(str(item) for item in candidate_step_ids if str(item) not in completed)
        bypassed = tuple(str(item) for item in candidate_step_ids if str(item) in completed)
        deliverable: list[InFlightMessage] = []
        skipped_messages: list[str] = []
        checkpoint_processed_responses = set(checkpoint.processed_response_ids)
        for message in checkpoint.in_flight_messages:
            if message.processed or (
                message.correlation_id
                and (
                    message.correlation_id in checkpoint_processed_responses
                    or self.store.response_processed(
                        task_id=checkpoint.task_id,
                        response_id=message.correlation_id,
                    )
                )
            ):
                skipped_messages.append(message.message_id)
            else:
                deliverable.append(message)
        pending_requests = tuple(
            request for request in checkpoint.pending_requests
            if request.awaiting_response and not request.response_id
        )
        skipped_responses = tuple(sorted(set(checkpoint.processed_response_ids)))
        fence_keys = tuple(sorted(set(checkpoint.side_effect_fence_keys)))
        for key in expectations.required_fence_keys:
            fence = self.store.side_effect_fence(key)
            if fence is None:
                raise CheckpointResumeRejected(f"required side-effect fence is missing: {key}")
            if fence.state is SideEffectState.COMMITTED and key not in fence_keys:
                fence_keys = (*fence_keys, key)
        return ResumeWorkset(
            checkpoint=checkpoint,
            executable_step_ids=executable,
            bypassed_step_ids=bypassed,
            deliverable_messages=tuple(deliverable),
            skipped_message_ids=tuple(skipped_messages),
            pending_requests=pending_requests,
            skipped_response_ids=skipped_responses,
            preserved_fence_keys=tuple(sorted(set(fence_keys))),
            state_payload=copy.deepcopy(dict(checkpoint.state_payload)),
        )

    def resume(
        self,
        checkpoint_id: str,
        expectations: ResumeExpectations,
        *,
        candidate_step_ids: Sequence[str] = (),
        compact_first: bool = False,
        rebind_worker: bool = False,
        rebind_graph: bool = False,
        idempotency_key: str = "",
    ) -> tuple[CheckpointReceipt, tuple[OwnerResumeReceipt, ...]]:
        workset = self.prepare(checkpoint_id, expectations, candidate_step_ids=candidate_step_ids)
        checkpoint = workset.checkpoint
        resume_key = idempotency_key or f"resume:{checkpoint.checkpoint_id}:{checkpoint.commit_revision}"
        owner_receipts: list[OwnerResumeReceipt] = []
        base_request = {
            "schema": "zyra.checkpoint-owner-resume/v1",
            "checkpoint_id": checkpoint.checkpoint_id,
            "run_id": checkpoint.run_id,
            "task_id": checkpoint.task_id,
            "session_id": checkpoint.session_id,
            "commit_revision": checkpoint.commit_revision,
            "signature": checkpoint.signature,
            "workset": workset.to_dict(),
            "idempotency_key": resume_key,
        }
        ordered: list[tuple[str, ResumeOwnerPort | None, bool]] = [
            ("compact_restore", self.compact_owner, compact_first),
            ("worker_rebind", self.worker_owner, rebind_worker),
            ("graph_rebind", self.graph_owner, rebind_graph),
            ("session_resume", self.session_owner, True),
        ]
        for operation, owner, required in ordered:
            if not required:
                continue
            if owner is None:
                raise CheckpointResumeRejected(f"resume owner is unavailable: {operation}")
            response, fence, changed = self.effects.execute_once(
                run_id=checkpoint.run_id,
                task_id=checkpoint.task_id,
                operation=operation,
                request={**base_request, "operation": operation, "owner": owner.owner},
                idempotency_key=f"{resume_key}:{operation}",
                callback=owner.resume,
            )
            owner_receipts.append(OwnerResumeReceipt(
                owner=owner.owner,
                operation=operation,
                receipt_ref=fence.receipt_ref,
                changed=changed or bool(response.get("changed", False)),
                payload=response,
            ))
        resume_token = "resume:" + stable_digest({
            "checkpoint": checkpoint.checkpoint_id,
            "revision": checkpoint.commit_revision,
            "signature": checkpoint.signature,
            "owners": [
                {
                    "owner": item.owner,
                    "operation": item.operation,
                    "receipt_ref": item.receipt_ref,
                }
                for item in owner_receipts
            ],
            "bypassed": workset.bypassed_step_ids,
            "skipped_responses": workset.skipped_response_ids,
            "fences": workset.preserved_fence_keys,
        })[:48]
        receipt = CheckpointReceipt(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=checkpoint.run_id,
            task_id=checkpoint.task_id,
            commit_revision=checkpoint.commit_revision,
            phase=CheckpointPhase.RESUMED,
            signature=checkpoint.signature,
            content_digest=checkpoint.content_digest,
            applied_write_ids=tuple(item.write_id for item in checkpoint.committed_writes),
            pending_write_ids=tuple(item.write_id for item in checkpoint.pending_writes),
            bypassed_step_ids=workset.bypassed_step_ids,
            skipped_response_ids=workset.skipped_response_ids,
            fenced_effect_keys=workset.preserved_fence_keys,
            resume_token=resume_token,
            metadata={
                "owner_receipts": [item.to_dict() for item in owner_receipts],
                "deliverable_message_ids": [item.message_id for item in workset.deliverable_messages],
                "skipped_message_ids": list(workset.skipped_message_ids),
                "pending_request_ids": [item.request_id for item in workset.pending_requests],
                "completed_step_bypass": True,
                "processed_response_replay": False,
                "committed_side_effect_replay": False,
            },
        )
        saved, _ = self.store.append_checkpoint_receipt(receipt)
        return saved, tuple(owner_receipts)

    def _validate_expectations(
        self,
        checkpoint: RecoveryCheckpoint,
        expected: ResumeExpectations,
    ) -> None:
        for name in ("run_id", "task_id", "session_id"):
            if getattr(checkpoint, name) != getattr(expected, name):
                raise CheckpointOwnerMismatch(f"checkpoint {name} does not match resume request")
        for name in ("workflow_signature", "graph_signature", "topology_signature"):
            if getattr(checkpoint, name) != getattr(expected, name):
                raise CheckpointSignatureMismatch(f"checkpoint {name} mismatch")
        for owner, reference in expected.owner_refs.items():
            if checkpoint.owner_refs.get(owner) != reference:
                raise CheckpointOwnerMismatch(f"checkpoint owner ref changed: {owner}")
        allowed = set(expected.allow_version_advancement)
        for owner, version in expected.version_refs.items():
            checkpoint_version = checkpoint.version_refs.get(owner)
            if owner in allowed:
                if not self._version_at_least(version, checkpoint_version):
                    raise CheckpointOwnerMismatch(f"checkpoint version is newer than resume owner: {owner}")
            elif checkpoint_version != version:
                raise CheckpointOwnerMismatch(f"checkpoint version ref changed: {owner}")
        completed = set(checkpoint.completed_step_ids)
        missing = set(expected.required_completed_step_ids) - completed
        if missing:
            raise CheckpointResumeRejected(f"checkpoint lacks required completed steps: {sorted(missing)}")
        if checkpoint.phase not in {
            CheckpointPhase.COMMITTED,
            CheckpointPhase.INTERRUPTED,
            CheckpointPhase.RESUMED,
        }:
            raise CheckpointResumeRejected(f"checkpoint phase is not resumable: {checkpoint.phase.value}")

    @staticmethod
    def _version_at_least(current: Any, checkpoint: Any) -> bool:
        if isinstance(current, int) and isinstance(checkpoint, int):
            return current >= checkpoint
        return current == checkpoint


def checkpoint_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.checkpoint-runtime-contract/v1",
        "state_owner": "python.RecoveryPlanStore",
        "semantic_source": "LangGraph checkpoint exact-resume narrow conformance",
        "migrated_graph_runtime": False,
        "serialization": "versioned JSON allowlist",
        "atomic_commit": True,
        "pending_committed_separated": True,
        "branch_local_delta": True,
        "write_set_conflict_detection": True,
        "completed_step_bypass": True,
        "processed_response_replay": False,
        "committed_side_effect_replay": False,
        "unrestricted_pickle": False,
        "path_escape": False,
    }


__all__ = [
    "CallbackResumeOwner",
    "CheckpointCommitRequest",
    "CheckpointCommitRuntime",
    "CheckpointOwnerMismatch",
    "CheckpointResumeBridge",
    "CheckpointResumeRejected",
    "CheckpointRuntimeError",
    "CheckpointSignatureMismatch",
    "OwnerResumeReceipt",
    "ResumeExpectations",
    "ResumeOwnerPort",
    "ResumeWorkset",
    "SideEffectFenceRuntime",
    "checkpoint_runtime_contract",
]
