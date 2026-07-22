from __future__ import annotations

import copy
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .checkpoint_runtime import (
    CheckpointCommitRequest,
    CheckpointCommitRuntime,
    CheckpointResumeBridge,
    CheckpointResumeRejected,
    ResumeExpectations,
    ResumeWorkset,
    SideEffectFenceRuntime,
)
from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import (
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
from .delta_journal import BranchDeltaBuilder, DeltaCommitResult, DeterministicCommitRuntime
from .store import RecoveryPlanStore, RecoveryReplayConflict, RecoveryStoreConflict


class ExactRecoveryError(RuntimeError):
    pass


class ExactRecoveryScopeError(ExactRecoveryError):
    pass


class ExactRecoveryStateError(ExactRecoveryError):
    pass


class ExactRecoveryPhase(StrEnum):
    PREPARED = "prepared"
    EFFECT_STARTED = "effect_started"
    EFFECT_COMMITTED = "effect_committed"
    CRASHED = "crashed"
    RESUMED = "resumed"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ExactRecoveryAttempt:
    attempt_id: str
    run_id: str
    task_id: str
    session_id: str
    step_id: str
    operation: str
    idempotency_key: str
    checkpoint_id: str
    checkpoint_revision: int
    fence_key: str
    request_digest: str
    workflow_signature: str
    graph_signature: str
    topology_signature: str
    owner_refs: Mapping[str, Any]
    version_refs: Mapping[str, Any]
    phase: ExactRecoveryPhase = ExactRecoveryPhase.PREPARED
    response_id: str = ""
    effect_receipt_ref: str = ""
    created_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return stable_digest({
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "step_id": self.step_id,
            "operation": self.operation,
            "idempotency_key": self.idempotency_key,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_revision": self.checkpoint_revision,
            "fence_key": self.fence_key,
            "request_digest": self.request_digest,
            "workflow_signature": self.workflow_signature,
            "graph_signature": self.graph_signature,
            "topology_signature": self.topology_signature,
            "owner_refs": dict(self.owner_refs),
            "version_refs": dict(self.version_refs),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.exact-recovery-attempt/v1",
            "attempt_id": self.attempt_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "step_id": self.step_id,
            "operation": self.operation,
            "idempotency_key": self.idempotency_key,
            "checkpoint_id": self.checkpoint_id,
            "checkpoint_revision": self.checkpoint_revision,
            "fence_key": self.fence_key,
            "request_digest": self.request_digest,
            "workflow_signature": self.workflow_signature,
            "graph_signature": self.graph_signature,
            "topology_signature": self.topology_signature,
            "owner_refs": copy.deepcopy(dict(self.owner_refs)),
            "version_refs": copy.deepcopy(dict(self.version_refs)),
            "phase": self.phase.value,
            "response_id": self.response_id,
            "effect_receipt_ref": self.effect_receipt_ref,
            "created_at": self.created_at,
            "metadata": copy.deepcopy(dict(self.metadata)),
            "digest": self.digest,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExactRecoveryAttempt":
        attempt = cls(
            attempt_id=str(value["attempt_id"]),
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            session_id=str(value["session_id"]),
            step_id=str(value["step_id"]),
            operation=str(value["operation"]),
            idempotency_key=str(value["idempotency_key"]),
            checkpoint_id=str(value["checkpoint_id"]),
            checkpoint_revision=int(value["checkpoint_revision"]),
            fence_key=str(value["fence_key"]),
            request_digest=str(value["request_digest"]),
            workflow_signature=str(value["workflow_signature"]),
            graph_signature=str(value["graph_signature"]),
            topology_signature=str(value["topology_signature"]),
            owner_refs=copy.deepcopy(dict(value.get("owner_refs") or {})),
            version_refs=copy.deepcopy(dict(value.get("version_refs") or {})),
            phase=ExactRecoveryPhase(str(value.get("phase") or ExactRecoveryPhase.PREPARED.value)),
            response_id=str(value.get("response_id") or ""),
            effect_receipt_ref=str(value.get("effect_receipt_ref") or ""),
            created_at=str(value.get("created_at") or utc_now()),
            metadata=copy.deepcopy(dict(value.get("metadata") or {})),
        )
        if value.get("digest") and str(value["digest"]) != attempt.digest:
            raise ExactRecoveryStateError("exact recovery attempt digest mismatch")
        return attempt


@dataclass(frozen=True, slots=True)
class ExactEffectResult:
    attempt: ExactRecoveryAttempt
    response: Mapping[str, Any]
    fence: SideEffectFence
    executed: bool
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "response": copy.deepcopy(dict(self.response)),
            "fence": self.fence.to_dict(),
            "executed": self.executed,
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class ExactResumeResult:
    attempt: ExactRecoveryAttempt
    workset: ResumeWorkset
    receipt: CheckpointReceipt
    owner_receipts: tuple[Mapping[str, Any], ...]
    effect_step_bypassed: bool
    response_replayed: bool
    topology_revision_preserved: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.exact-recovery-resume-result/v1",
            "attempt": self.attempt.to_dict(),
            "workset": self.workset.to_dict(),
            "receipt": self.receipt.to_dict(),
            "owner_receipts": [copy.deepcopy(dict(item)) for item in self.owner_receipts],
            "effect_step_bypassed": self.effect_step_bypassed,
            "response_replayed": self.response_replayed,
            "topology_revision_preserved": self.topology_revision_preserved,
        }


@dataclass(frozen=True, slots=True)
class ExactCompletionResult:
    attempt: ExactRecoveryAttempt
    checkpoint: RecoveryCheckpoint
    checkpoint_receipt: CheckpointReceipt
    response_marked: bool
    promoted_write_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "checkpoint_receipt": self.checkpoint_receipt.to_dict(),
            "response_marked": self.response_marked,
            "promoted_write_ids": list(self.promoted_write_ids),
        }


class ExactRecoveryRuntime:
    ATTEMPT_METADATA_KEY = "exact_recovery_attempt"

    def __init__(
        self,
        store: RecoveryPlanStore,
        commits: CheckpointCommitRuntime,
        resume_bridge: CheckpointResumeBridge,
        *,
        components: RecoveryComponentControl | None = None,
    ) -> None:
        self.store = store
        self.commits = commits
        self.resume_bridge = resume_bridge
        self.effects = SideEffectFenceRuntime(store)
        self.deltas = DeterministicCommitRuntime(store)
        self.components = components or RecoveryComponentControl()
        self._lock = threading.RLock()

    def prepare(
        self,
        *,
        refs: RecoveryRefs,
        step_id: str,
        operation: str,
        request: Mapping[str, Any],
        idempotency_key: str,
        workflow_signature: str,
        graph_signature: str,
        topology_signature: str,
        owner_refs: Mapping[str, Any],
        version_refs: Mapping[str, Any],
        state_payload: Mapping[str, Any] | None = None,
        committed_refs: Sequence[Mapping[str, Any]] = (),
        committed_writes: Sequence[CheckpointWrite | Mapping[str, Any]] = (),
        pending_writes: Sequence[CheckpointWrite | Mapping[str, Any]] = (),
        in_flight_messages: Sequence[InFlightMessage | Mapping[str, Any]] = (),
        pending_requests: Sequence[PendingRequestInfo | Mapping[str, Any]] = (),
        completed_step_ids: Sequence[str] = (),
        processed_response_ids: Sequence[str] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> ExactRecoveryAttempt:
        self._require_components("prepare exact side-effect recovery")
        if not refs.session_id:
            raise ExactRecoveryScopeError("exact recovery requires session_id")
        normalized_step = self._identity("step id", step_id)
        normalized_operation = self._identity("operation", operation)
        normalized_key = self._identity("idempotency key", idempotency_key)
        request_value = copy.deepcopy(dict(request))
        request_digest = stable_digest(request_value)
        fence, _ = self.effects.reserve(
            run_id=refs.run_id,
            task_id=refs.task_id,
            operation=normalized_operation,
            request=request_value,
            idempotency_key=normalized_key,
            metadata={"step_id": normalized_step, "session_id": refs.session_id},
        )
        head = self.store.checkpoint_head(refs.task_id)
        if head is not None:
            self._assert_head_scope(head, refs)
        attempt_id = "exactattempt:" + stable_digest({
            "run_id": refs.run_id,
            "task_id": refs.task_id,
            "session_id": refs.session_id,
            "step_id": normalized_step,
            "operation": normalized_operation,
            "idempotency_key": normalized_key,
            "request_digest": request_digest,
            "fence_key": fence.fence_key,
        })[:40]
        provisional = ExactRecoveryAttempt(
            attempt_id=attempt_id,
            run_id=refs.run_id,
            task_id=refs.task_id,
            session_id=refs.session_id,
            step_id=normalized_step,
            operation=normalized_operation,
            idempotency_key=normalized_key,
            checkpoint_id=head.checkpoint_id if head else "pending",
            checkpoint_revision=head.commit_revision if head else 0,
            fence_key=fence.fence_key,
            request_digest=request_digest,
            workflow_signature=self._identity("workflow signature", workflow_signature),
            graph_signature=self._identity("graph signature", graph_signature),
            topology_signature=self._identity("topology signature", topology_signature),
            owner_refs=copy.deepcopy(dict(owner_refs)),
            version_refs=copy.deepcopy(dict(version_refs)),
            metadata={**copy.deepcopy(dict(metadata or {})), "request": request_value},
        )
        pending = tuple(self._write(item, pending=True) for item in pending_writes)
        committed = tuple(self._write(item, pending=False) for item in committed_writes)
        messages = tuple(self._message(item) for item in in_flight_messages)
        requests = tuple(self._request(item) for item in pending_requests)
        request_metadata = {
            **copy.deepcopy(dict(metadata or {})),
            self.ATTEMPT_METADATA_KEY: provisional.to_dict(),
            "effect_step_id": normalized_step,
            "effect_fence_key": fence.fence_key,
            "effect_request_digest": request_digest,
            "crash_boundary": "before_observable_effect",
            "exact_resume_required": True,
        }
        checkpoint, _, _ = self.commits.commit(CheckpointCommitRequest(
            refs=refs,
            workflow_signature=provisional.workflow_signature,
            graph_signature=provisional.graph_signature,
            topology_signature=provisional.topology_signature,
            owner_refs=copy.deepcopy(dict(owner_refs)),
            version_refs=copy.deepcopy(dict(version_refs)),
            state_payload=copy.deepcopy(dict(state_payload or {})),
            committed_refs=tuple(copy.deepcopy(dict(item)) for item in committed_refs),
            completed_step_ids=tuple(dict.fromkeys(str(item) for item in completed_step_ids)),
            committed_writes=committed,
            pending_writes=pending,
            in_flight_messages=messages,
            pending_requests=requests,
            processed_response_ids=tuple(dict.fromkeys(str(item) for item in processed_response_ids)),
            side_effect_fence_keys=tuple(dict.fromkeys((
                *(head.side_effect_fence_keys if head else ()),
                fence.fence_key,
            ))),
            expected_revision=head.commit_revision if head else 0,
            metadata=request_metadata,
        ))
        attempt = self._replace_attempt(
            provisional,
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_revision=checkpoint.commit_revision,
        )
        return attempt

    def execute_effect(
        self,
        attempt: ExactRecoveryAttempt,
        callback: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        *,
        receipt_field: str = "receipt_id",
        response_id_field: str = "response_id",
    ) -> ExactEffectResult:
        self._require_components("execute exactly-once side effect")
        checkpoint = self._checkpoint(attempt)
        self._assert_attempt(checkpoint, attempt)
        request = copy.deepcopy(dict(attempt.metadata.get("request") or {}))
        if stable_digest(request) != attempt.request_digest:
            raise ExactRecoveryStateError("side-effect request digest changed after checkpoint")
        response, fence, executed = self.effects.execute_once(
            run_id=attempt.run_id,
            task_id=attempt.task_id,
            operation=attempt.operation,
            request=request,
            idempotency_key=attempt.idempotency_key,
            callback=callback,
            receipt_field=receipt_field,
        )
        response_id = str(response.get(response_id_field) or "")
        phase = ExactRecoveryPhase.EFFECT_COMMITTED if fence.state is SideEffectState.COMMITTED else ExactRecoveryPhase.EFFECT_STARTED
        updated = self._replace_attempt(
            attempt,
            phase=phase,
            response_id=response_id,
            effect_receipt_ref=fence.receipt_ref,
        )
        return ExactEffectResult(
            attempt=updated,
            response=copy.deepcopy(dict(response)),
            fence=fence,
            executed=executed,
            replayed=not executed,
        )

    def mark_crashed(self, attempt: ExactRecoveryAttempt, *, reason: str) -> ExactRecoveryAttempt:
        checkpoint = self._checkpoint(attempt)
        self._assert_attempt(checkpoint, attempt)
        fence = self.store.side_effect_fence(attempt.fence_key)
        if fence is None:
            raise ExactRecoveryStateError("side-effect fence disappeared before crash recording")
        return self._replace_attempt(
            attempt,
            phase=ExactRecoveryPhase.CRASHED,
            metadata={
                **dict(attempt.metadata),
                "crash_reason": str(reason)[:1000],
                "fence_state_at_crash": fence.state.value,
                "crashed_at": utc_now(),
            },
        )

    def resume(
        self,
        attempt: ExactRecoveryAttempt,
        *,
        candidate_step_ids: Sequence[str],
        compact_first: bool = False,
        rebind_worker: bool = False,
        rebind_graph: bool = False,
        allow_version_advancement: Sequence[str] = (),
    ) -> ExactResumeResult:
        self._require_components("resume exact recovery")
        checkpoint = self._checkpoint(attempt)
        self._assert_attempt(checkpoint, attempt)
        fence = self.store.side_effect_fence(attempt.fence_key)
        if fence is None:
            raise CheckpointResumeRejected("exact resume fence is missing")
        candidates = tuple(dict.fromkeys(self._identity("candidate step id", item) for item in candidate_step_ids))
        response_processed = bool(attempt.response_id and self.store.response_processed(
            task_id=attempt.task_id,
            response_id=attempt.response_id,
        ))
        effect_committed = fence.state is SideEffectState.COMMITTED
        safe_candidates = tuple(item for item in candidates if not (effect_committed and item == attempt.step_id))
        expectations = ResumeExpectations(
            run_id=attempt.run_id,
            task_id=attempt.task_id,
            session_id=attempt.session_id,
            workflow_signature=attempt.workflow_signature,
            graph_signature=attempt.graph_signature,
            topology_signature=attempt.topology_signature,
            owner_refs=copy.deepcopy(dict(attempt.owner_refs)),
            version_refs=copy.deepcopy(dict(attempt.version_refs)),
            allow_version_advancement=tuple(allow_version_advancement),
            required_completed_step_ids=(),
            required_fence_keys=(attempt.fence_key,),
        )
        workset = self.resume_bridge.prepare(
            checkpoint.checkpoint_id,
            expectations,
            candidate_step_ids=safe_candidates,
        )
        receipt, owners = self.resume_bridge.resume(
            checkpoint.checkpoint_id,
            expectations,
            candidate_step_ids=safe_candidates,
            compact_first=compact_first,
            rebind_worker=rebind_worker,
            rebind_graph=rebind_graph,
            idempotency_key=f"exact-resume:{attempt.attempt_id}:{checkpoint.commit_revision}",
        )
        bypassed = tuple(dict.fromkeys((
            *receipt.bypassed_step_ids,
            *((attempt.step_id,) if effect_committed else ()),
        )))
        if bypassed != receipt.bypassed_step_ids:
            receipt = CheckpointReceipt.from_dict({**receipt.to_dict(), "bypassed_step_ids": list(bypassed)})
        resumed = self._replace_attempt(attempt, phase=ExactRecoveryPhase.RESUMED)
        topology_revision = self._topology_revision(checkpoint)
        expected_revision = self._topology_revision_from_refs(attempt.owner_refs, attempt.version_refs)
        return ExactResumeResult(
            attempt=resumed,
            workset=workset,
            receipt=receipt,
            owner_receipts=tuple(item.to_dict() for item in owners),
            effect_step_bypassed=effect_committed and attempt.step_id in bypassed,
            response_replayed=response_processed,
            topology_revision_preserved=(expected_revision is None or topology_revision == expected_revision),
        )

    def complete(
        self,
        attempt: ExactRecoveryAttempt,
        *,
        response_id: str = "",
        promote_write_ids: Sequence[str] = (),
        state_updates: Mapping[str, Any] | None = None,
        committed_refs: Sequence[Mapping[str, Any]] = (),
    ) -> ExactCompletionResult:
        self._require_components("complete exact recovery")
        checkpoint = self._checkpoint(attempt)
        self._assert_attempt(checkpoint, attempt)
        fence = self.store.side_effect_fence(attempt.fence_key)
        if fence is None or fence.state is not SideEffectState.COMMITTED:
            raise ExactRecoveryStateError("cannot complete exact recovery before the side effect is committed")
        selected_response = self._identity("response id", response_id or attempt.response_id, optional=True)
        marked = False
        if selected_response:
            marked = self.store.mark_response_processed(
                task_id=attempt.task_id,
                response_id=selected_response,
                checkpoint_id=checkpoint.checkpoint_id,
                response_digest=fence.response_digest,
                metadata={
                    "receipt_ref": fence.receipt_ref,
                    "exact_recovery_attempt_id": attempt.attempt_id,
                },
            )
        promoted_ids = tuple(dict.fromkeys(self._identity("write id", item) for item in promote_write_ids))
        current = checkpoint
        if promoted_ids:
            current, _ = self.commits.promote_pending(
                checkpoint.checkpoint_id,
                write_ids=promoted_ids,
                metadata={"exact_recovery_attempt_id": attempt.attempt_id},
            )
        remaining_pending = tuple(item for item in current.pending_writes if item.write_id not in promoted_ids)
        committed_writes = tuple(current.committed_writes)
        state_payload = copy.deepcopy(dict(current.state_payload))
        state_payload.update(copy.deepcopy(dict(state_updates or {})))
        completed_steps = tuple(dict.fromkeys((*current.completed_step_ids, attempt.step_id)))
        processed = tuple(dict.fromkeys((*current.processed_response_ids, *((selected_response,) if selected_response else ()))))
        completed_attempt = self._replace_attempt(attempt, phase=ExactRecoveryPhase.COMPLETED, response_id=selected_response)
        metadata = {
            **copy.deepcopy(dict(current.metadata)),
            self.ATTEMPT_METADATA_KEY: completed_attempt.to_dict(),
            "crash_boundary": "after_exact_completion_commit",
            "effect_replayed": False,
            "response_replayed": False,
        }
        final, receipt, _ = self.commits.commit(CheckpointCommitRequest(
            refs=RecoveryRefs(
                run_id=current.run_id,
                task_id=current.task_id,
                session_id=current.session_id,
                checkpoint_id=current.checkpoint_id,
            ),
            workflow_signature=current.workflow_signature,
            graph_signature=current.graph_signature,
            topology_signature=current.topology_signature,
            owner_refs=copy.deepcopy(dict(current.owner_refs)),
            version_refs=copy.deepcopy(dict(current.version_refs)),
            state_payload=state_payload,
            committed_refs=tuple((*current.committed_refs, *(copy.deepcopy(dict(item)) for item in committed_refs))),
            completed_step_ids=completed_steps,
            committed_writes=committed_writes,
            pending_writes=remaining_pending,
            in_flight_messages=tuple(
                item for item in current.in_flight_messages
                if not (selected_response and item.correlation_id == selected_response)
            ),
            pending_requests=tuple(
                item for item in current.pending_requests
                if not (selected_response and item.response_id == selected_response)
            ),
            processed_response_ids=processed,
            side_effect_fence_keys=tuple(dict.fromkeys((*current.side_effect_fence_keys, attempt.fence_key))),
            expected_revision=current.commit_revision,
            metadata=metadata,
        ))
        return ExactCompletionResult(
            attempt=completed_attempt,
            checkpoint=final,
            checkpoint_receipt=receipt,
            response_marked=marked,
            promoted_write_ids=promoted_ids,
        )

    def rehydrate(self, checkpoint_id: str) -> ExactRecoveryAttempt:
        checkpoint = self.store.checkpoint(checkpoint_id)
        if checkpoint is None:
            raise ExactRecoveryStateError(f"checkpoint not found: {checkpoint_id}")
        raw = checkpoint.metadata.get(self.ATTEMPT_METADATA_KEY)
        if not isinstance(raw, Mapping):
            raise ExactRecoveryStateError("checkpoint does not contain exact recovery attempt metadata")
        attempt = ExactRecoveryAttempt.from_dict(raw)
        if attempt.checkpoint_id in {"pending", checkpoint.checkpoint_id}:
            if attempt.checkpoint_id == "pending":
                attempt = ExactRecoveryAttempt.from_dict({
                    **attempt.to_dict(),
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "checkpoint_revision": checkpoint.commit_revision,
                })
        self._assert_attempt(checkpoint, attempt)
        return attempt

    def recovery_workset(
        self,
        attempt: ExactRecoveryAttempt,
        candidate_step_ids: Sequence[str],
    ) -> dict[str, Any]:
        checkpoint = self._checkpoint(attempt)
        fence = self.store.side_effect_fence(attempt.fence_key)
        if fence is None:
            raise ExactRecoveryStateError("exact recovery fence is missing")
        committed = fence.state is SideEffectState.COMMITTED
        pending_steps = [
            self._identity("candidate step id", item)
            for item in candidate_step_ids
            if not (committed and str(item) == attempt.step_id)
            and str(item) not in checkpoint.completed_step_ids
        ]
        bypassed = [item for item in candidate_step_ids if item not in pending_steps]
        deliverable = [
            item.to_dict()
            for item in checkpoint.in_flight_messages
            if not item.processed
            and not (item.correlation_id and self.store.response_processed(task_id=attempt.task_id, response_id=item.correlation_id))
        ]
        requests = [
            item.to_dict() for item in checkpoint.pending_requests
            if item.awaiting_response and not item.response_id
        ]
        return {
            "schema": "zyra.exact-recovery-workset/v1",
            "attempt_id": attempt.attempt_id,
            "checkpoint_id": checkpoint.checkpoint_id,
            "fence_state": fence.state.value,
            "executable_step_ids": pending_steps,
            "bypassed_step_ids": bypassed,
            "deliverable_messages": deliverable,
            "pending_requests": requests,
            "committed_effect_replay": False,
            "processed_response_replay": False,
        }

    def commit_branch_batch(
        self,
        branch_ids: Sequence[str],
        *,
        conflict_strategy: str = "replan",
    ) -> tuple[DeltaCommitResult, ...]:
        # Branch conflict handling is deliberately delegated to the one branch
        # recovery owner.  Keeping a second implementation here previously made
        # exact resume and branch merge disagree about rejection and rebase.
        from .branch_recovery_runtime import BranchRecoveryRuntime, BranchResolutionStrategy

        strategy = BranchResolutionStrategy(str(conflict_strategy).casefold())
        ordered_ids = tuple(sorted(dict.fromkeys(self._identity("branch id", item) for item in branch_ids)))
        deltas = []
        for branch_id in ordered_ids:
            delta = self.store.branch_delta(branch_id)
            if delta is None:
                raise ExactRecoveryStateError(f"branch delta not found: {branch_id}")
            deltas.append(delta)
        batch = BranchRecoveryRuntime(self.store, components=self.components).commit_batch(
            deltas,
            strategy=strategy,
        )
        results: list[DeltaCommitResult] = []
        for resolution in batch.resolutions:
            if not resolution.committed:
                if resolution.replan_required:
                    raise RecoveryStoreConflict("branch conflict requires graph replan")
                raise RecoveryStoreConflict("branch conflict was rejected")
            effective = self.store.branch_delta(resolution.effective_branch_id)
            checkpoint = self.store.checkpoint(resolution.checkpoint_id)
            if effective is None or checkpoint is None:
                raise ExactRecoveryStateError("branch batch committed without durable delta/checkpoint")
            receipt = next(
                (
                    item
                    for item in self.store.checkpoint_receipts(checkpoint.checkpoint_id)
                    if item.receipt_id == resolution.receipt_id
                ),
                None,
            )
            if receipt is None:
                raise ExactRecoveryStateError("branch batch committed without durable receipt")
            resulting_values: dict[str, Any] = {}
            for key in effective.write_set:
                try:
                    resulting_values[key] = copy.deepcopy(
                        BranchDeltaBuilder._read_path(checkpoint.state_payload, BranchDeltaBuilder._path(key))
                    )
                except KeyError:
                    resulting_values[key] = None
            results.append(DeltaCommitResult(
                delta=effective,
                checkpoint=checkpoint,
                receipt=receipt,
                conflicts=(),
                resulting_values=resulting_values,
            ))
        return tuple(results)

    def _require_components(self, operation: str) -> None:
        self.components.require(RecoveryComponent.PLAN_STORE, operation=operation)
        self.components.require(RecoveryComponent.CHECKPOINT_RESTORER, operation=operation)
        self.components.require(RecoveryComponent.SIDE_EFFECT_FENCE, operation=operation)

    def _checkpoint(self, attempt: ExactRecoveryAttempt) -> RecoveryCheckpoint:
        checkpoint = self.store.checkpoint(attempt.checkpoint_id)
        if checkpoint is None:
            raise ExactRecoveryStateError(f"exact recovery checkpoint not found: {attempt.checkpoint_id}")
        return checkpoint

    def _assert_attempt(self, checkpoint: RecoveryCheckpoint, attempt: ExactRecoveryAttempt) -> None:
        if checkpoint.run_id != attempt.run_id or checkpoint.task_id != attempt.task_id:
            raise ExactRecoveryScopeError("exact recovery attempt crosses checkpoint custody")
        if checkpoint.session_id != attempt.session_id:
            raise ExactRecoveryScopeError("exact recovery session identity changed")
        if checkpoint.workflow_signature != attempt.workflow_signature:
            raise ExactRecoveryStateError("exact recovery workflow signature changed")
        if checkpoint.graph_signature != attempt.graph_signature:
            raise ExactRecoveryStateError("exact recovery graph signature changed")
        if checkpoint.topology_signature != attempt.topology_signature:
            raise ExactRecoveryStateError("exact recovery topology signature changed")
        if attempt.fence_key not in checkpoint.side_effect_fence_keys:
            raise ExactRecoveryStateError("checkpoint does not preserve the attempt side-effect fence")
        for owner, reference in attempt.owner_refs.items():
            if checkpoint.owner_refs.get(owner) != reference:
                raise ExactRecoveryStateError(f"exact recovery owner ref changed: {owner}")

    @staticmethod
    def _assert_head_scope(head: RecoveryCheckpoint, refs: RecoveryRefs) -> None:
        if head.run_id != refs.run_id or head.task_id != refs.task_id or head.session_id != refs.session_id:
            raise ExactRecoveryScopeError("checkpoint head belongs to another run/task/session")

    @staticmethod
    def _write(value: CheckpointWrite | Mapping[str, Any], *, pending: bool) -> CheckpointWrite:
        if isinstance(value, CheckpointWrite):
            expected = PendingWriteState.PENDING if pending else PendingWriteState.COMMITTED
            if value.state is not expected:
                raise ExactRecoveryStateError("checkpoint write state does not match its collection")
            return value
        raw = copy.deepcopy(dict(value))
        raw["state"] = PendingWriteState.PENDING.value if pending else PendingWriteState.COMMITTED.value
        return CheckpointWrite.from_dict(raw)

    @staticmethod
    def _message(value: InFlightMessage | Mapping[str, Any]) -> InFlightMessage:
        return value if isinstance(value, InFlightMessage) else InFlightMessage.from_dict(value)

    @staticmethod
    def _request(value: PendingRequestInfo | Mapping[str, Any]) -> PendingRequestInfo:
        return value if isinstance(value, PendingRequestInfo) else PendingRequestInfo.from_dict(value)

    @staticmethod
    def _replace_attempt(attempt: ExactRecoveryAttempt, **changes: Any) -> ExactRecoveryAttempt:
        value = attempt.to_dict()
        value.pop("schema", None)
        value.pop("digest", None)
        value.update(changes)
        if isinstance(value.get("phase"), ExactRecoveryPhase):
            value["phase"] = value["phase"].value
        return ExactRecoveryAttempt.from_dict(value)

    @staticmethod
    def _identity(name: str, value: Any, *, optional: bool = False) -> str:
        candidate = str(value or "").strip()
        if optional and not candidate:
            return ""
        if not candidate:
            raise ExactRecoveryStateError(f"{name} is required")
        if len(candidate) > 512 or any(character.isspace() for character in candidate):
            raise ExactRecoveryStateError(f"{name} is invalid")
        return candidate

    @staticmethod
    def _topology_revision(checkpoint: RecoveryCheckpoint) -> int | None:
        for source in (checkpoint.version_refs, checkpoint.owner_refs, checkpoint.state_payload):
            for key in ("topology_revision", "graph_revision", "revision"):
                value = source.get(key) if isinstance(source, Mapping) else None
                if isinstance(value, int):
                    return value
        return None

    @staticmethod
    def _topology_revision_from_refs(owner_refs: Mapping[str, Any], version_refs: Mapping[str, Any]) -> int | None:
        for source in (version_refs, owner_refs):
            for key in ("topology_revision", "graph_revision", "revision"):
                value = source.get(key)
                if isinstance(value, int):
                    return value
            graph = source.get("graph")
            if isinstance(graph, Mapping):
                value = graph.get("revision")
                if isinstance(value, int):
                    return value
        return None


def exact_recovery_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.exact-recovery-runtime-contract/v1",
        "state_owner": "RecoveryPlanStore",
        "checkpoint_bridge": "CheckpointResumeBridge",
        "side_effect_fence": "SideEffectFenceRuntime",
        "committed_effect_replayed": False,
        "processed_response_replayed": False,
        "pending_request_restored": True,
        "in_flight_message_restored": True,
        "topology_signature_validated": True,
        "branch_commit_order": "stable branch id",
        "langgraph_runtime_dependency": False,
    }


__all__ = [
    "ExactCompletionResult",
    "ExactEffectResult",
    "ExactRecoveryAttempt",
    "ExactRecoveryError",
    "ExactRecoveryPhase",
    "ExactRecoveryRuntime",
    "ExactRecoveryScopeError",
    "ExactRecoveryStateError",
    "ExactResumeResult",
    "exact_recovery_runtime_contract",
]
