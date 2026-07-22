from __future__ import annotations

import copy
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import BranchDelta, DeltaConflict, DeltaEntry, DeltaOperation, RecoveryCheckpoint, stable_digest, utc_now
from .delta_journal import (
    BranchDeltaBuilder,
    DeltaCommitResult,
    DeltaConflictError,
    DeltaOperationError,
    DeterministicCommitRuntime,
    WriteSetConflictDetector,
)
from .store import RecoveryPlanStore


class BranchRecoveryError(RuntimeError):
    pass


class BranchAliasRejected(BranchRecoveryError):
    pass


class BranchResolutionStrategy(StrEnum):
    REPLAN = "replan"
    SERIALIZE = "serialize"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class BranchAdmission:
    branch_id: str
    run_id: str
    task_id: str
    base_checkpoint_id: str
    base_revision: int
    owner: str
    read_set: tuple[str, ...]
    write_set: tuple[str, ...]
    delta_digest: str
    snapshot_digest: str
    admitted_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-branch-admission/v1",
            "branch_id": self.branch_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "base_checkpoint_id": self.base_checkpoint_id,
            "base_revision": self.base_revision,
            "owner": self.owner,
            "read_set": list(self.read_set),
            "write_set": list(self.write_set),
            "delta_digest": self.delta_digest,
            "snapshot_digest": self.snapshot_digest,
            "admitted_at": self.admitted_at,
        }


@dataclass(frozen=True, slots=True)
class BranchResolution:
    original_branch_id: str
    effective_branch_id: str
    strategy: BranchResolutionStrategy
    conflicts: tuple[DeltaConflict, ...]
    committed: bool
    checkpoint_id: str
    commit_revision: int
    receipt_id: str
    replan_required: bool
    serialized_after_revision: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_branch_id": self.original_branch_id,
            "effective_branch_id": self.effective_branch_id,
            "strategy": self.strategy.value,
            "conflicts": [item.to_dict() for item in self.conflicts],
            "committed": self.committed,
            "checkpoint_id": self.checkpoint_id,
            "commit_revision": self.commit_revision,
            "receipt_id": self.receipt_id,
            "replan_required": self.replan_required,
            "serialized_after_revision": self.serialized_after_revision,
            "metadata": copy.deepcopy(dict(self.metadata)),
        }


@dataclass(frozen=True, slots=True)
class BranchBatchResult:
    batch_id: str
    task_id: str
    input_order: tuple[str, ...]
    deterministic_order: tuple[str, ...]
    admissions: tuple[BranchAdmission, ...]
    resolutions: tuple[BranchResolution, ...]
    initial_checkpoint_id: str
    final_checkpoint_id: str
    final_revision: int
    state_digest: str
    completed_at: str = field(default_factory=utc_now)

    @property
    def committed_branch_ids(self) -> tuple[str, ...]:
        return tuple(item.original_branch_id for item in self.resolutions if item.committed)

    @property
    def replan_branch_ids(self) -> tuple[str, ...]:
        return tuple(item.original_branch_id for item in self.resolutions if item.replan_required)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-branch-batch-result/v1",
            "batch_id": self.batch_id,
            "task_id": self.task_id,
            "input_order": list(self.input_order),
            "deterministic_order": list(self.deterministic_order),
            "admissions": [item.to_dict() for item in self.admissions],
            "resolutions": [item.to_dict() for item in self.resolutions],
            "initial_checkpoint_id": self.initial_checkpoint_id,
            "final_checkpoint_id": self.final_checkpoint_id,
            "final_revision": self.final_revision,
            "state_digest": self.state_digest,
            "committed_branch_ids": list(self.committed_branch_ids),
            "replan_branch_ids": list(self.replan_branch_ids),
            "completed_at": self.completed_at,
        }


class BranchRecoveryRuntime:
    def __init__(
        self,
        store: RecoveryPlanStore,
        *,
        components: RecoveryComponentControl | None = None,
    ) -> None:
        self.store = store
        self.commits = DeterministicCommitRuntime(store)
        self.conflicts = WriteSetConflictDetector(store)
        self.components = components or RecoveryComponentControl()
        self._lock = threading.RLock()

    def admit(self, delta: BranchDelta) -> BranchAdmission:
        self.components.require(RecoveryComponent.CHECKPOINT_RESTORER, operation="admit branch-local recovery delta")
        base = self.store.checkpoint(delta.checkpoint_id)
        if base is None:
            raise BranchRecoveryError(f"branch base checkpoint not found: {delta.checkpoint_id}")
        self._validate_scope(base, delta)
        self._validate_aliases(base, delta)
        persisted, _ = self.commits.persist(delta)
        if persisted.digest != delta.digest:
            raise BranchAliasRejected("persisted branch delta changed digest")
        return BranchAdmission(
            branch_id=delta.branch_id,
            run_id=delta.run_id,
            task_id=delta.task_id,
            base_checkpoint_id=delta.checkpoint_id,
            base_revision=delta.base_revision,
            owner=delta.owner,
            read_set=delta.read_set,
            write_set=delta.write_set,
            delta_digest=delta.digest,
            snapshot_digest=stable_digest(copy.deepcopy(dict(base.state_payload))),
        )

    def detect(self, branch_id: str) -> tuple[DeltaConflict, ...]:
        delta = self.store.branch_delta(branch_id)
        if delta is None:
            raise BranchRecoveryError(f"branch delta not found: {branch_id}")
        return self.conflicts.detect(delta)

    def commit_batch(
        self,
        deltas: Sequence[BranchDelta],
        *,
        strategy: BranchResolutionStrategy | str = BranchResolutionStrategy.REPLAN,
    ) -> BranchBatchResult:
        selected_strategy = BranchResolutionStrategy(str(strategy))
        if not deltas:
            raise BranchRecoveryError("branch recovery batch is empty")
        task_ids = {item.task_id for item in deltas}
        run_ids = {item.run_id for item in deltas}
        if len(task_ids) != 1 or len(run_ids) != 1:
            raise BranchRecoveryError("branch recovery batch cannot cross run/task custody")
        input_order = tuple(item.branch_id for item in deltas)
        by_id: dict[str, BranchDelta] = {}
        for delta in deltas:
            existing = by_id.get(delta.branch_id)
            if existing is not None and existing.digest != delta.digest:
                raise BranchAliasRejected("branch identity is reused by different delta content")
            by_id[delta.branch_id] = delta
        ordered = tuple(by_id[key] for key in sorted(by_id))
        task_id = ordered[0].task_id
        initial = self.store.checkpoint_head(task_id)
        if initial is None:
            raise BranchRecoveryError("branch recovery requires a checkpoint head")
        admissions = tuple(self.admit(delta) for delta in ordered)
        resolutions: list[BranchResolution] = []
        with self._lock:
            for delta in ordered:
                head_before = self.store.checkpoint_head(task_id)
                if head_before is None:
                    raise BranchRecoveryError("checkpoint head disappeared during branch commit")
                conflicts = self.conflicts.detect(delta, current=head_before)
                effective = delta
                serialized_revision = 0
                if conflicts:
                    if selected_strategy is BranchResolutionStrategy.REPLAN:
                        self.store.reject_branch_delta(delta.branch_id, reason=self._conflict_reason(conflicts))
                        resolutions.append(self._resolution(
                            delta,
                            effective,
                            selected_strategy,
                            conflicts,
                            result=None,
                            replan=True,
                            serialized_revision=0,
                        ))
                        continue
                    if selected_strategy is BranchResolutionStrategy.REJECT:
                        self.store.reject_branch_delta(delta.branch_id, reason=self._conflict_reason(conflicts))
                        resolutions.append(self._resolution(
                            delta,
                            effective,
                            selected_strategy,
                            conflicts,
                            result=None,
                            replan=False,
                            serialized_revision=0,
                        ))
                        continue
                    effective = self.rebase(delta, head_before)
                    serialized_revision = head_before.commit_revision
                    remaining = self.conflicts.detect(effective, current=head_before)
                    if remaining:
                        self.store.reject_branch_delta(effective.branch_id, reason=self._conflict_reason(remaining))
                        resolutions.append(self._resolution(
                            delta,
                            effective,
                            selected_strategy,
                            (*conflicts, *remaining),
                            result=None,
                            replan=True,
                            serialized_revision=serialized_revision,
                        ))
                        continue
                try:
                    result = self.commits.commit(effective)
                except DeltaConflictError as error:
                    resolutions.append(self._resolution(
                        delta,
                        effective,
                        selected_strategy,
                        (*conflicts, *error.conflicts),
                        result=None,
                        replan=selected_strategy is not BranchResolutionStrategy.REJECT,
                        serialized_revision=serialized_revision,
                    ))
                    continue
                resolutions.append(self._resolution(
                    delta,
                    effective,
                    selected_strategy,
                    conflicts,
                    result=result,
                    replan=False,
                    serialized_revision=serialized_revision,
                ))
        final = self.store.checkpoint_head(task_id)
        if final is None:
            raise BranchRecoveryError("branch recovery final checkpoint is missing")
        batch_id = "recoverybranchbatch:" + stable_digest({
            "task_id": task_id,
            "input": list(input_order),
            "ordered": [item.branch_id for item in ordered],
            "strategy": selected_strategy.value,
            "resolutions": [item.to_dict() for item in resolutions],
        })[:40]
        return BranchBatchResult(
            batch_id=batch_id,
            task_id=task_id,
            input_order=input_order,
            deterministic_order=tuple(item.branch_id for item in ordered),
            admissions=admissions,
            resolutions=tuple(resolutions),
            initial_checkpoint_id=initial.checkpoint_id,
            final_checkpoint_id=final.checkpoint_id,
            final_revision=final.commit_revision,
            state_digest=stable_digest(copy.deepcopy(dict(final.state_payload))),
        )

    def rebase(self, delta: BranchDelta, head: RecoveryCheckpoint | None = None) -> BranchDelta:
        current = head or self.store.checkpoint_head(delta.task_id)
        if current is None:
            raise BranchRecoveryError("cannot rebase without a checkpoint head")
        if current.run_id != delta.run_id or current.task_id != delta.task_id:
            raise BranchRecoveryError("branch rebase crosses run/task custody")
        builder = BranchDeltaBuilder(
            current,
            owner=delta.owner,
            branch_id=f"{delta.branch_id}:rebase:{current.commit_revision}",
            metadata={
                **copy.deepcopy(dict(delta.metadata)),
                "rebased_from_branch_id": delta.branch_id,
                "rebased_from_checkpoint_id": delta.checkpoint_id,
                "rebased_from_revision": delta.base_revision,
                "serialized_after_revision": current.commit_revision,
                "conflict_strategy": "serialize",
            },
        )
        for key in delta.read_set:
            builder.read(key, None)
        for entry in sorted(delta.entries, key=lambda item: (item.sequence, item.key, item.entry_id)):
            self._replay_entry(builder, entry)
        rebased = builder.build()
        self._validate_aliases(current, rebased)
        self.commits.persist(rebased)
        return rebased

    def permutation_key(self, deltas: Sequence[BranchDelta]) -> str:
        normalized = [
            {
                "branch_id": item.branch_id,
                "digest": item.digest,
                "base_revision": item.base_revision,
                "read_set": list(item.read_set),
                "write_set": list(item.write_set),
            }
            for item in sorted(deltas, key=lambda item: item.branch_id)
        ]
        return stable_digest(normalized)

    def alias_audit(self, deltas: Sequence[BranchDelta]) -> dict[str, Any]:
        digests_before = {item.branch_id: item.digest for item in deltas}
        snapshots = {item.branch_id: copy.deepcopy(item.to_dict()) for item in deltas}
        mutable_ids: dict[int, list[str]] = {}
        for delta in deltas:
            # Keep every inspected object alive for the whole audit.  Walking a
            # short-lived to_dict() result lets CPython reuse object ids and can
            # falsely report aliases between otherwise isolated branches.
            self._walk_mutable_ids(snapshots[delta.branch_id], delta.branch_id, mutable_ids)
        cross_branch_aliases = {
            str(identity): branches
            for identity, branches in mutable_ids.items()
            if len(set(branches)) > 1
        }
        digests_after = {item.branch_id: item.digest for item in deltas}
        content_changed = [branch_id for branch_id in digests_before if digests_before[branch_id] != digests_after[branch_id]]
        snapshot_changed = [
            branch_id for branch_id, snapshot in snapshots.items()
            if stable_digest(snapshot) != stable_digest(next(item.to_dict() for item in deltas if item.branch_id == branch_id))
        ]
        return {
            "schema": "zyra.recovery-branch-alias-audit/v1",
            "branch_ids": sorted(digests_before),
            "cross_branch_mutable_aliases": cross_branch_aliases,
            "content_changed_during_audit": content_changed,
            "snapshot_changed_during_audit": snapshot_changed,
            "copy_on_write": not cross_branch_aliases and not content_changed and not snapshot_changed,
        }

    @staticmethod
    def _replay_entry(builder: BranchDeltaBuilder, entry: DeltaEntry) -> None:
        metadata = {**copy.deepcopy(dict(entry.metadata)), "rebased_from_entry_id": entry.entry_id}
        if entry.operation is DeltaOperation.SET:
            builder.set(entry.key, copy.deepcopy(entry.value), metadata=metadata)
        elif entry.operation is DeltaOperation.DELETE:
            builder.delete(entry.key, metadata=metadata)
        elif entry.operation is DeltaOperation.APPEND_UNIQUE:
            builder.append_unique(entry.key, copy.deepcopy(entry.value), metadata=metadata)
        elif entry.operation is DeltaOperation.INCREMENT:
            builder.increment(entry.key, entry.value, metadata=metadata)
        elif entry.operation is DeltaOperation.MERGE_MAPPING:
            if not isinstance(entry.value, Mapping):
                raise DeltaOperationError("merge_mapping rebase value is not a mapping")
            builder.merge_mapping(entry.key, copy.deepcopy(dict(entry.value)), metadata=metadata)
        else:
            raise DeltaOperationError(f"unsupported branch rebase operation: {entry.operation.value}")

    @staticmethod
    def _validate_scope(base: RecoveryCheckpoint, delta: BranchDelta) -> None:
        if base.run_id != delta.run_id or base.task_id != delta.task_id:
            raise BranchRecoveryError("branch delta crosses checkpoint run/task custody")
        if base.commit_revision != delta.base_revision:
            raise BranchRecoveryError("branch delta base revision differs from checkpoint")
        if base.checkpoint_id != delta.checkpoint_id:
            raise BranchRecoveryError("branch delta base checkpoint identity differs")

    @staticmethod
    def _validate_aliases(base: RecoveryCheckpoint, delta: BranchDelta) -> None:
        before = stable_digest(copy.deepcopy(dict(base.state_payload)))
        delta_snapshot = copy.deepcopy(delta.to_dict())
        working = DeterministicCommitRuntime.apply(base.state_payload, delta.entries)
        if stable_digest(copy.deepcopy(dict(base.state_payload))) != before:
            raise BranchAliasRejected("branch application mutated the canonical checkpoint snapshot")
        if stable_digest(delta.to_dict()) != stable_digest(delta_snapshot):
            raise BranchAliasRejected("branch application mutated the immutable delta")
        for key in delta.write_set:
            path = BranchDeltaBuilder._path(key)
            try:
                base_value = BranchDeltaBuilder._read_path(base.state_payload, path)
                working_value = BranchDeltaBuilder._read_path(working, path)
            except KeyError:
                continue
            if isinstance(base_value, (dict, list)) and base_value is working_value:
                raise BranchAliasRejected(f"branch write shares mutable alias with canonical state: {key}")

    @staticmethod
    def _resolution(
        original: BranchDelta,
        effective: BranchDelta,
        strategy: BranchResolutionStrategy,
        conflicts: Sequence[DeltaConflict],
        *,
        result: DeltaCommitResult | None,
        replan: bool,
        serialized_revision: int,
    ) -> BranchResolution:
        return BranchResolution(
            original_branch_id=original.branch_id,
            effective_branch_id=effective.branch_id,
            strategy=strategy,
            conflicts=tuple(conflicts),
            committed=bool(result and result.committed),
            checkpoint_id=result.checkpoint.checkpoint_id if result else "",
            commit_revision=result.checkpoint.commit_revision if result else 0,
            receipt_id=result.receipt.receipt_id if result else "",
            replan_required=replan,
            serialized_after_revision=serialized_revision,
            metadata={
                "original_digest": original.digest,
                "effective_digest": effective.digest,
                "pending_state_published": False,
            },
        )

    @staticmethod
    def _conflict_reason(conflicts: Sequence[DeltaConflict]) -> str:
        return "; ".join(f"{item.kind.value}:{item.key}:{item.reason}" for item in conflicts)

    @classmethod
    def _walk_mutable_ids(cls, value: Any, branch_id: str, result: dict[int, list[str]]) -> None:
        if isinstance(value, dict):
            result.setdefault(id(value), []).append(branch_id)
            for child in value.values():
                cls._walk_mutable_ids(child, branch_id, result)
        elif isinstance(value, list):
            result.setdefault(id(value), []).append(branch_id)
            for child in value:
                cls._walk_mutable_ids(child, branch_id, result)


def branch_recovery_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-branch-runtime-contract/v1",
        "canonical_state": "immutable checkpoint snapshot",
        "branch_state": "copy-on-write delta",
        "commit_order": "lexical stable branch id independent of completion order",
        "conflict_strategies": [item.value for item in BranchResolutionStrategy],
        "conflict_default": "explicit graph replan",
        "shared_mutable_aliases": False,
        "pending_delta_in_canonical_state": False,
    }


__all__ = [
    "BranchAdmission",
    "BranchAliasRejected",
    "BranchBatchResult",
    "BranchRecoveryError",
    "BranchRecoveryRuntime",
    "BranchResolution",
    "BranchResolutionStrategy",
    "branch_recovery_runtime_contract",
]
