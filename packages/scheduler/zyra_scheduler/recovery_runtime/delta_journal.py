from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import (
    BranchDelta,
    CheckpointPhase,
    CheckpointReceipt,
    CheckpointWrite,
    ConflictKind,
    DeltaConflict,
    DeltaEntry,
    DeltaOperation,
    PendingWriteState,
    RecoveryCheckpoint,
    recovery_id,
    stable_digest,
    utc_now,
)
from .store import RecoveryPlanStore, RecoveryStoreConflict


class DeltaJournalError(RuntimeError):
    pass


class DeltaConflictError(DeltaJournalError):
    def __init__(self, conflicts: Sequence[DeltaConflict]) -> None:
        self.conflicts = tuple(conflicts)
        super().__init__(
            "branch delta conflicts: "
            + "; ".join(f"{item.kind.value}:{item.key}" for item in self.conflicts)
        )


class DeltaOperationError(DeltaJournalError):
    pass


@dataclass(frozen=True, slots=True)
class DeltaCommitResult:
    delta: BranchDelta
    checkpoint: RecoveryCheckpoint
    receipt: CheckpointReceipt
    conflicts: tuple[DeltaConflict, ...]
    resulting_values: Mapping[str, Any]

    @property
    def committed(self) -> bool:
        return not self.conflicts and self.checkpoint.phase is CheckpointPhase.COMMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "committed": self.committed,
            "delta": self.delta.to_dict(),
            "checkpoint": self.checkpoint.to_dict(),
            "receipt": self.receipt.to_dict(),
            "conflicts": [item.to_dict() for item in self.conflicts],
            "resulting_values": copy.deepcopy(dict(self.resulting_values)),
        }


class BranchDeltaBuilder:
    """Copy-on-write builder for a recovery checkpoint branch."""

    def __init__(
        self,
        checkpoint: RecoveryCheckpoint,
        *,
        owner: str,
        branch_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if checkpoint.phase is not CheckpointPhase.COMMITTED:
            raise DeltaJournalError("branch base must be a committed checkpoint")
        self.checkpoint = checkpoint
        self.owner = owner
        self.branch_id = branch_id or recovery_id("recoverybranch")
        self.metadata = copy.deepcopy(dict(metadata or {}))
        self._base = copy.deepcopy(dict(checkpoint.state_payload))
        self._working = copy.deepcopy(self._base)
        self._reads: set[str] = set()
        self._entries: list[DeltaEntry] = []
        self._sequence = 0

    def read(self, key: str, default: Any = None) -> Any:
        path = self._path(key)
        self._reads.add(key)
        try:
            value = self._read_path(self._working, path)
        except KeyError:
            value = default
        return copy.deepcopy(value)

    def contains(self, key: str) -> bool:
        try:
            self._read_path(self._working, self._path(key))
        except (KeyError, IndexError, TypeError):
            return False
        return True

    def set(self, key: str, value: Any, *, expected_digest: str = "", metadata: Mapping[str, Any] | None = None) -> "BranchDeltaBuilder":
        self._record(key, DeltaOperation.SET, value, expected_digest=expected_digest, metadata=metadata)
        self._set_path(self._working, self._path(key), copy.deepcopy(value))
        return self

    def delete(self, key: str, *, expected_digest: str = "", metadata: Mapping[str, Any] | None = None) -> "BranchDeltaBuilder":
        self._record(key, DeltaOperation.DELETE, None, expected_digest=expected_digest, metadata=metadata)
        self._delete_path(self._working, self._path(key), missing_ok=False)
        return self

    def append_unique(
        self,
        key: str,
        value: Any,
        *,
        expected_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "BranchDeltaBuilder":
        current = self.read(key, [])
        if not isinstance(current, list):
            raise DeltaOperationError(f"append_unique target is not a list: {key}")
        candidate = copy.deepcopy(value)
        candidate_digest = stable_digest(candidate)
        if all(stable_digest(item) != candidate_digest for item in current):
            current.append(candidate)
        self._record(key, DeltaOperation.APPEND_UNIQUE, value, expected_digest=expected_digest, metadata=metadata)
        self._set_path(self._working, self._path(key), current)
        return self

    def increment(
        self,
        key: str,
        amount: int | float = 1,
        *,
        expected_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "BranchDeltaBuilder":
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise DeltaOperationError("increment amount must be numeric")
        current = self.read(key, 0)
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise DeltaOperationError(f"increment target is not numeric: {key}")
        self._record(key, DeltaOperation.INCREMENT, amount, expected_digest=expected_digest, metadata=metadata)
        self._set_path(self._working, self._path(key), current + amount)
        return self

    def merge_mapping(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        expected_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "BranchDeltaBuilder":
        current = self.read(key, {})
        if not isinstance(current, Mapping):
            raise DeltaOperationError(f"merge_mapping target is not a mapping: {key}")
        merged = self._deep_merge(dict(current), dict(value))
        self._record(key, DeltaOperation.MERGE_MAPPING, value, expected_digest=expected_digest, metadata=metadata)
        self._set_path(self._working, self._path(key), merged)
        return self

    def working_snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._working)

    def build(self) -> BranchDelta:
        write_set = tuple(sorted({entry.key for entry in self._entries}))
        return BranchDelta(
            branch_id=self.branch_id,
            run_id=self.checkpoint.run_id,
            task_id=self.checkpoint.task_id,
            checkpoint_id=self.checkpoint.checkpoint_id,
            base_revision=self.checkpoint.commit_revision,
            owner=self.owner,
            read_set=tuple(sorted(self._reads)),
            write_set=write_set,
            entries=tuple(self._entries),
            metadata={
                **self.metadata,
                "base_checkpoint_digest": self.checkpoint.content_digest,
                "copy_on_write": True,
                "shared_mutable_aliases": False,
            },
        )

    def _record(
        self,
        key: str,
        operation: DeltaOperation,
        value: Any,
        *,
        expected_digest: str,
        metadata: Mapping[str, Any] | None,
    ) -> None:
        self._path(key)
        if expected_digest:
            current = self.read(key, None)
            if stable_digest(current) != expected_digest:
                raise DeltaOperationError(f"expected digest mismatch before {operation.value}: {key}")
        self._sequence += 1
        self._entries.append(DeltaEntry(
            entry_id=f"deltaentry:{stable_digest({'branch': self.branch_id, 'sequence': self._sequence, 'key': key, 'operation': operation.value, 'value': value})[:40]}",
            branch_id=self.branch_id,
            key=key,
            operation=operation,
            value=copy.deepcopy(value),
            sequence=self._sequence,
            expected_digest=expected_digest,
            metadata=dict(metadata or {}),
        ))

    @staticmethod
    def _path(key: str) -> tuple[str, ...]:
        value = str(key or "").strip()
        if not value:
            raise DeltaOperationError("delta key is required")
        if value.startswith("/"):
            parts = tuple(part.replace("~1", "/").replace("~0", "~") for part in value.split("/")[1:])
        else:
            parts = tuple(value.split("."))
        if not parts or any(not part or part in {".", ".."} for part in parts):
            raise DeltaOperationError(f"invalid delta key: {key!r}")
        if any("/" in part or "\\" in part for part in parts):
            raise DeltaOperationError(f"invalid delta key segment: {key!r}")
        return parts

    @classmethod
    def _read_path(cls, root: Mapping[str, Any], path: Sequence[str]) -> Any:
        current: Any = root
        for part in path:
            if not isinstance(current, Mapping) or part not in current:
                raise KeyError(".".join(path))
            current = current[part]
        return current

    @classmethod
    def _set_path(cls, root: dict[str, Any], path: Sequence[str], value: Any) -> None:
        current = root
        for part in path[:-1]:
            child = current.get(part)
            if child is None:
                child = {}
                current[part] = child
            if not isinstance(child, dict):
                raise DeltaOperationError(f"delta path traverses non-mapping at {part}")
            current = child
        current[path[-1]] = copy.deepcopy(value)

    @classmethod
    def _delete_path(cls, root: dict[str, Any], path: Sequence[str], *, missing_ok: bool) -> None:
        current = root
        for part in path[:-1]:
            child = current.get(part)
            if not isinstance(child, dict):
                if missing_ok:
                    return
                raise DeltaOperationError(f"delta delete path is missing at {part}")
            current = child
        if path[-1] not in current and not missing_ok:
            raise DeltaOperationError(f"delta delete target does not exist: {'.'.join(path)}")
        current.pop(path[-1], None)

    @classmethod
    def _deep_merge(cls, left: dict[str, Any], right: Mapping[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(left)
        for key in sorted(right):
            incoming = right[key]
            current = result.get(key)
            if isinstance(current, Mapping) and isinstance(incoming, Mapping):
                result[key] = cls._deep_merge(dict(current), incoming)
            else:
                result[key] = copy.deepcopy(incoming)
        return result


class WriteSetConflictDetector:
    def __init__(self, store: RecoveryPlanStore) -> None:
        self.store = store

    def detect(self, delta: BranchDelta, *, current: RecoveryCheckpoint | None = None) -> tuple[DeltaConflict, ...]:
        head = current or self.store.checkpoint_head(delta.task_id)
        if head is None:
            return (
                DeltaConflict(
                    kind=ConflictKind.BASE_REVISION,
                    key="checkpoint_head",
                    branch_id=delta.branch_id,
                    conflicting_branch_id="",
                    base_revision=delta.base_revision,
                    current_revision=-1,
                    reason="task has no committed checkpoint head",
                ),
            )
        conflicts: list[DeltaConflict] = []
        base = self.store.checkpoint(delta.checkpoint_id)
        if base is None or base.task_id != delta.task_id:
            conflicts.append(DeltaConflict(
                kind=ConflictKind.BASE_REVISION,
                key="checkpoint_id",
                branch_id=delta.branch_id,
                conflicting_branch_id="",
                base_revision=delta.base_revision,
                current_revision=head.commit_revision,
                reason="branch base checkpoint is missing or belongs to another task",
            ))
            return tuple(conflicts)
        if base.commit_revision != delta.base_revision:
            conflicts.append(DeltaConflict(
                kind=ConflictKind.BASE_REVISION,
                key="commit_revision",
                branch_id=delta.branch_id,
                conflicting_branch_id="",
                base_revision=delta.base_revision,
                current_revision=head.commit_revision,
                reason="branch base revision does not match base checkpoint",
            ))
        if base.run_id != delta.run_id or head.run_id != delta.run_id:
            conflicts.append(DeltaConflict(
                kind=ConflictKind.OWNER_MISMATCH,
                key="run_id",
                branch_id=delta.branch_id,
                conflicting_branch_id="",
                base_revision=delta.base_revision,
                current_revision=head.commit_revision,
                reason="branch run identity differs from checkpoint lineage",
            ))
        writes = self.store.committed_delta_writes(
            task_id=delta.task_id,
            after_revision=delta.base_revision,
        )
        for write in writes:
            key = str(write["key"])
            conflicting_branch = str(write["branch_id"])
            if key in delta.write_set:
                conflicts.append(DeltaConflict(
                    kind=ConflictKind.WRITE_WRITE,
                    key=key,
                    branch_id=delta.branch_id,
                    conflicting_branch_id=conflicting_branch,
                    base_revision=delta.base_revision,
                    current_revision=int(write["commit_revision"]),
                    reason="another committed branch wrote the same key",
                ))
            elif key in delta.read_set:
                conflicts.append(DeltaConflict(
                    kind=ConflictKind.READ_WRITE,
                    key=key,
                    branch_id=delta.branch_id,
                    conflicting_branch_id=conflicting_branch,
                    base_revision=delta.base_revision,
                    current_revision=int(write["commit_revision"]),
                    reason="a value read by this branch changed after its base revision",
                ))
        return self._dedupe(conflicts)

    @staticmethod
    def _dedupe(conflicts: Sequence[DeltaConflict]) -> tuple[DeltaConflict, ...]:
        unique: dict[tuple[str, str, str, int], DeltaConflict] = {}
        for item in conflicts:
            key = (item.kind.value, item.key, item.conflicting_branch_id, item.current_revision)
            unique[key] = item
        return tuple(sorted(
            unique.values(),
            key=lambda item: (item.current_revision, item.kind.value, item.key, item.conflicting_branch_id),
        ))


class DeterministicCommitRuntime:
    """Merge conflict-free branch state and publish one atomic checkpoint."""

    def __init__(self, store: RecoveryPlanStore) -> None:
        self.store = store
        self.conflicts = WriteSetConflictDetector(store)

    def persist(self, delta: BranchDelta) -> tuple[BranchDelta, bool]:
        return self.store.put_branch_delta(delta)

    def commit(self, delta: BranchDelta) -> DeltaCommitResult:
        persisted, _ = self.store.put_branch_delta(delta)
        head = self.store.checkpoint_head(delta.task_id)
        conflicts = self.conflicts.detect(persisted, current=head)
        if conflicts:
            self.store.reject_branch_delta(persisted.branch_id, reason="; ".join(item.reason for item in conflicts))
            raise DeltaConflictError(conflicts)
        if head is None:
            raise DeltaJournalError("cannot commit a branch without checkpoint head")
        resulting = self.apply(head.state_payload, persisted.entries)
        committed_writes = self._checkpoint_writes(persisted)
        checkpoint_id = "recoverycheckpoint:" + stable_digest({
            "parent": head.checkpoint_id,
            "parent_revision": head.commit_revision,
            "delta": persisted.digest,
            "state": resulting,
        })[:40]
        existing = self.store.checkpoint(checkpoint_id)
        if existing is not None:
            receipts = self.store.checkpoint_receipts(checkpoint_id)
            if not receipts:
                raise DeltaJournalError("deterministic checkpoint exists without receipt")
            return DeltaCommitResult(
                delta=persisted,
                checkpoint=existing,
                receipt=receipts[0],
                conflicts=(),
                resulting_values=resulting,
            )
        committed_at = utc_now()
        checkpoint = RecoveryCheckpoint(
            checkpoint_id=checkpoint_id,
            run_id=head.run_id,
            task_id=head.task_id,
            session_id=head.session_id,
            workflow_signature=head.workflow_signature,
            graph_signature=head.graph_signature,
            topology_signature=head.topology_signature,
            owner_refs=dict(head.owner_refs),
            version_refs={
                **dict(head.version_refs),
                "recovery_commit_revision": head.commit_revision + 1,
                "branch_id": persisted.branch_id,
            },
            committed_refs=tuple(head.committed_refs),
            phase=CheckpointPhase.COMMITTED,
            parent_checkpoint_id=head.checkpoint_id,
            ancestry=(*head.ancestry, head.checkpoint_id),
            iteration=head.iteration + 1,
            commit_revision=head.commit_revision + 1,
            pending_writes=tuple(head.pending_writes),
            committed_writes=(*head.committed_writes, *committed_writes),
            in_flight_messages=tuple(head.in_flight_messages),
            pending_requests=tuple(head.pending_requests),
            completed_step_ids=tuple(head.completed_step_ids),
            processed_response_ids=tuple(head.processed_response_ids),
            side_effect_fence_keys=tuple(head.side_effect_fence_keys),
            state_payload=resulting,
            created_at=committed_at,
            committed_at=committed_at,
            metadata={
                **dict(head.metadata),
                "commit_source": "branch_local_delta",
                "branch_id": persisted.branch_id,
                "branch_digest": persisted.digest,
                "deterministic_commit": True,
                "pending_in_canonical_state": False,
            },
        )
        receipt = CheckpointReceipt(
            checkpoint_id=checkpoint.checkpoint_id,
            run_id=checkpoint.run_id,
            task_id=checkpoint.task_id,
            commit_revision=checkpoint.commit_revision,
            phase=CheckpointPhase.COMMITTED,
            signature=checkpoint.signature,
            content_digest=checkpoint.content_digest,
            applied_write_ids=tuple(write.write_id for write in committed_writes),
            pending_write_ids=tuple(write.write_id for write in checkpoint.pending_writes),
            fenced_effect_keys=tuple(checkpoint.side_effect_fence_keys),
            metadata={
                "branch_id": persisted.branch_id,
                "branch_digest": persisted.digest,
                "state_owner": "python.RecoveryPlanStore",
            },
        )
        saved, saved_receipt = self.store.commit_branch_delta(
            persisted,
            expected_head_revision=head.commit_revision,
            new_checkpoint=checkpoint,
            checkpoint_receipt=receipt,
            resulting_values={key: self._safe_read(resulting, BranchDeltaBuilder._path(key)) for key in persisted.write_set},
        )
        return DeltaCommitResult(
            delta=persisted,
            checkpoint=saved,
            receipt=saved_receipt,
            conflicts=(),
            resulting_values=resulting,
        )

    @classmethod
    def apply(cls, state: Mapping[str, Any], entries: Sequence[DeltaEntry]) -> dict[str, Any]:
        result = copy.deepcopy(dict(state))
        for entry in sorted(entries, key=lambda item: (item.sequence, item.key, item.entry_id)):
            path = BranchDeltaBuilder._path(entry.key)
            current = cls._safe_read(result, path)
            if entry.expected_digest and stable_digest(current) != entry.expected_digest:
                raise DeltaOperationError(f"delta expected digest changed during commit: {entry.key}")
            if entry.operation is DeltaOperation.SET:
                BranchDeltaBuilder._set_path(result, path, entry.value)
            elif entry.operation is DeltaOperation.DELETE:
                BranchDeltaBuilder._delete_path(result, path, missing_ok=False)
            elif entry.operation is DeltaOperation.APPEND_UNIQUE:
                target = [] if current is None else copy.deepcopy(current)
                if not isinstance(target, list):
                    raise DeltaOperationError(f"append_unique target is not a list: {entry.key}")
                digest = stable_digest(entry.value)
                if all(stable_digest(item) != digest for item in target):
                    target.append(copy.deepcopy(entry.value))
                BranchDeltaBuilder._set_path(result, path, target)
            elif entry.operation is DeltaOperation.INCREMENT:
                target = 0 if current is None else current
                if isinstance(target, bool) or not isinstance(target, (int, float)):
                    raise DeltaOperationError(f"increment target is not numeric: {entry.key}")
                BranchDeltaBuilder._set_path(result, path, target + entry.value)
            elif entry.operation is DeltaOperation.MERGE_MAPPING:
                target = {} if current is None else current
                if not isinstance(target, Mapping) or not isinstance(entry.value, Mapping):
                    raise DeltaOperationError(f"merge_mapping target/value mismatch: {entry.key}")
                BranchDeltaBuilder._set_path(
                    result,
                    path,
                    BranchDeltaBuilder._deep_merge(dict(target), entry.value),
                )
            else:
                raise DeltaOperationError(f"unsupported delta operation: {entry.operation}")
        return copy.deepcopy(result)

    @staticmethod
    def _checkpoint_writes(delta: BranchDelta) -> tuple[CheckpointWrite, ...]:
        writes: list[CheckpointWrite] = []
        for entry in delta.entries:
            writes.append(CheckpointWrite(
                write_id=f"checkpointwrite:{entry.digest[:40]}",
                task_key=entry.key,
                channel="recovery_delta",
                value={
                    "operation": entry.operation.value,
                    "value": copy.deepcopy(entry.value),
                    "entry_digest": entry.digest,
                },
                state=PendingWriteState.COMMITTED,
                sequence=entry.sequence,
                writer_id=delta.owner,
                idempotency_key=f"{delta.branch_id}:{entry.sequence}",
                metadata={"branch_id": delta.branch_id, "entry_id": entry.entry_id},
            ))
        return tuple(writes)

    @staticmethod
    def _safe_read(root: Mapping[str, Any], path: Sequence[str]) -> Any:
        try:
            return copy.deepcopy(BranchDeltaBuilder._read_path(root, path))
        except KeyError:
            return None


__all__ = [
    "BranchDeltaBuilder",
    "DeltaCommitResult",
    "DeltaConflictError",
    "DeltaJournalError",
    "DeltaOperationError",
    "DeterministicCommitRuntime",
    "WriteSetConflictDetector",
]
