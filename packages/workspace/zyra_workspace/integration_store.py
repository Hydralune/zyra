from __future__ import annotations

"""Durable operation journal for workspace integration.

This journal is a subordinate component of ``WorkspaceManagerRuntime``.  It
does not own the canonical task-to-workspace binding; ``WorkspaceBindingStore``
continues to do that.  The journal makes multi-step operations restartable and
queryable without serializing process-private filesystem locations.
"""

import copy
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Generic, Iterable, Mapping, TypeVar

from .atomic import KeyedLockPool, atomic_write_json, read_json_object
from .errors import WorkspaceError, WorkspaceErrorCode, WorkspaceStoreError
from .integration_models import (
    ArtifactPublication,
    IsolationRecord,
    LocalWorkspaceEndpoint,
    MergeConflict,
    WorkerWorkspaceReceipt,
    WorkspaceAccessEnvelope,
    WorkspaceRebindRecord,
    WorkspaceRecoveryInput,
    WorkspaceReferenceMigration,
    WorkspaceTransactionRecord,
    WorkspaceTreeManifest,
    ensure_path_free_projection,
)
from .models import stable_digest, utc_now


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class IntegrationStoreSnapshot:
    revision: int
    transaction_count: int
    manifest_count: int
    isolation_count: int
    conflict_count: int
    rebind_count: int
    recovery_input_count: int
    receipt_count: int
    publication_count: int
    endpoint_count: int
    reference_migration_count: int
    envelope_audit_count: int
    committed_at: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "transaction_count": self.transaction_count,
            "manifest_count": self.manifest_count,
            "isolation_count": self.isolation_count,
            "conflict_count": self.conflict_count,
            "rebind_count": self.rebind_count,
            "recovery_input_count": self.recovery_input_count,
            "receipt_count": self.receipt_count,
            "publication_count": self.publication_count,
            "endpoint_count": self.endpoint_count,
            "reference_migration_count": self.reference_migration_count,
            "envelope_audit_count": self.envelope_audit_count,
            "committed_at": self.committed_at,
            "digest": self.digest,
        }


@dataclass
class _MutableIntegrationState:
    revision: int = 0
    transactions: dict[str, dict[str, Any]] = field(default_factory=dict)
    manifests: dict[str, dict[str, Any]] = field(default_factory=dict)
    isolations: dict[str, dict[str, Any]] = field(default_factory=dict)
    conflicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rebinds: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    receipts: dict[str, dict[str, Any]] = field(default_factory=dict)
    publications: dict[str, dict[str, Any]] = field(default_factory=dict)
    endpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    reference_migrations: dict[str, dict[str, Any]] = field(default_factory=dict)
    envelope_audits: dict[str, dict[str, Any]] = field(default_factory=dict)
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    workspace_indexes: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    committed_at: str = field(default_factory=utc_now)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "_MutableIntegrationState":
        def objects(name: str) -> dict[str, dict[str, Any]]:
            raw = value.get(name) or {}
            if not isinstance(raw, Mapping):
                raise WorkspaceStoreError(
                    WorkspaceErrorCode.STORE_CORRUPT,
                    "Workspace integration journal collection is not an object.",
                    operation="load_workspace_integration_journal",
                    metadata={"collection": name},
                )
            result: dict[str, dict[str, Any]] = {}
            for key, item in raw.items():
                if not isinstance(item, Mapping):
                    raise WorkspaceStoreError(
                        WorkspaceErrorCode.STORE_CORRUPT,
                        "Workspace integration journal record is not an object.",
                        operation="load_workspace_integration_journal",
                        metadata={"collection": name, "record_id": str(key)},
                    )
                result[str(key)] = dict(item)
            return result

        indexes_raw = value.get("workspace_indexes") or {}
        if not isinstance(indexes_raw, Mapping):
            raise WorkspaceStoreError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace integration indexes are not an object.",
                operation="load_workspace_integration_journal",
            )
        indexes: dict[str, dict[str, list[str]]] = {}
        for workspace_id, collections in indexes_raw.items():
            if not isinstance(collections, Mapping):
                continue
            indexes[str(workspace_id)] = {
                str(name): [str(item) for item in values or ()]
                for name, values in collections.items()
                if isinstance(values, list)
            }
        return cls(
            revision=max(0, int(value.get("revision") or 0)),
            transactions=objects("transactions"),
            manifests=objects("manifests"),
            isolations=objects("isolations"),
            conflicts=objects("conflicts"),
            rebinds=objects("rebinds"),
            recovery_inputs=objects("recovery_inputs"),
            receipts=objects("receipts"),
            publications=objects("publications"),
            endpoints=objects("endpoints"),
            reference_migrations=objects("reference_migrations"),
            envelope_audits=objects("envelope_audits"),
            idempotency=objects("idempotency"),
            workspace_indexes=indexes,
            committed_at=str(value.get("committed_at") or utc_now()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "state_owner": "WorkspaceManagerRuntime/WorkspaceIntegrationStore",
            "revision": self.revision,
            "transactions": copy.deepcopy(self.transactions),
            "manifests": copy.deepcopy(self.manifests),
            "isolations": copy.deepcopy(self.isolations),
            "conflicts": copy.deepcopy(self.conflicts),
            "rebinds": copy.deepcopy(self.rebinds),
            "recovery_inputs": copy.deepcopy(self.recovery_inputs),
            "receipts": copy.deepcopy(self.receipts),
            "publications": copy.deepcopy(self.publications),
            "endpoints": copy.deepcopy(self.endpoints),
            "reference_migrations": copy.deepcopy(self.reference_migrations),
            "envelope_audits": copy.deepcopy(self.envelope_audits),
            "idempotency": copy.deepcopy(self.idempotency),
            "workspace_indexes": copy.deepcopy(self.workspace_indexes),
            "committed_at": self.committed_at,
        }

    def index(self, workspace_id: str, collection: str, record_id: str) -> None:
        collections = self.workspace_indexes.setdefault(workspace_id, {})
        identifiers = collections.setdefault(collection, [])
        if record_id not in identifiers:
            identifiers.append(record_id)

    def unindex(self, workspace_id: str, collection: str, record_id: str) -> None:
        collections = self.workspace_indexes.get(workspace_id)
        if not collections:
            return
        identifiers = collections.get(collection)
        if identifiers and record_id in identifiers:
            identifiers.remove(record_id)


class WorkspaceIntegrationStore:
    """Atomic JSON journal for composite workspace operations.

    The store intentionally serializes every mutation through one filesystem
    transaction.  Workspace-level locks live above it in the manager/runtime;
    this global transaction lock protects durable revision ordering and avoids
    partially updated indexes after a process failure.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        disabled: bool = False,
        max_receipts_per_workspace: int = 4096,
        max_envelope_audits_per_workspace: int = 2048,
    ) -> None:
        self.root = Path(root).resolve()
        self.path = self.root / "integration-journal.json"
        self.disabled = bool(disabled)
        self.max_receipts_per_workspace = max(128, int(max_receipts_per_workspace))
        self.max_envelope_audits_per_workspace = max(128, int(max_envelope_audits_per_workspace))
        self._guard = threading.RLock()
        self.workspace_locks = KeyedLockPool()
        if not self.disabled:
            self.root.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                atomic_write_json(self.path, _MutableIntegrationState().to_dict())

    def snapshot(self) -> IntegrationStoreSnapshot:
        state = self._load()
        payload = state.to_dict()
        return IntegrationStoreSnapshot(
            revision=state.revision,
            transaction_count=len(state.transactions),
            manifest_count=len(state.manifests),
            isolation_count=len(state.isolations),
            conflict_count=len(state.conflicts),
            rebind_count=len(state.rebinds),
            recovery_input_count=len(state.recovery_inputs),
            receipt_count=len(state.receipts),
            publication_count=len(state.publications),
            endpoint_count=len(state.endpoints),
            reference_migration_count=len(state.reference_migrations),
            envelope_audit_count=len(state.envelope_audits),
            committed_at=state.committed_at,
            digest=stable_digest(payload),
        )

    def health(self) -> dict[str, Any]:
        if self.disabled:
            return {
                "ok": False,
                "disabled": True,
                "state_owner": "WorkspaceManagerRuntime/WorkspaceIntegrationStore",
                "fallback": None,
            }
        try:
            snapshot = self.snapshot()
            return {
                "ok": True,
                "disabled": False,
                "state_owner": "WorkspaceManagerRuntime/WorkspaceIntegrationStore",
                "revision": snapshot.revision,
                "transaction_count": snapshot.transaction_count,
                "isolation_count": snapshot.isolation_count,
                "rebind_count": snapshot.rebind_count,
                "recovery_input_count": snapshot.recovery_input_count,
                "physical_paths_persisted": False,
            }
        except WorkspaceError as error:
            return {
                "ok": False,
                "disabled": False,
                "state_owner": "WorkspaceManagerRuntime/WorkspaceIntegrationStore",
                "error_code": error.code.value,
                "physical_paths_persisted": False,
            }

    # Transactions -----------------------------------------------------

    def create_transaction(self, record: WorkspaceTransactionRecord) -> WorkspaceTransactionRecord:
        def mutation(state: _MutableIntegrationState) -> WorkspaceTransactionRecord:
            existing = state.transactions.get(record.transaction_id)
            if existing is not None:
                prior = WorkspaceTransactionRecord.from_dict(existing)
                if prior.plan_digest != record.plan_digest:
                    raise self._idempotency_conflict(
                        workspace_id=record.workspace_id,
                        operation="create_workspace_transaction",
                        key=record.transaction_id,
                        expected=prior.plan_digest,
                        actual=record.plan_digest,
                    )
                return prior
            state.transactions[record.transaction_id] = record.to_dict()
            state.index(record.workspace_id, "transactions", record.transaction_id)
            return record

        return self._transaction(mutation)

    def get_transaction(self, transaction_id: str) -> WorkspaceTransactionRecord | None:
        value = self._load().transactions.get(str(transaction_id))
        return WorkspaceTransactionRecord.from_dict(value) if value is not None else None

    def require_transaction(self, transaction_id: str) -> WorkspaceTransactionRecord:
        record = self.get_transaction(transaction_id)
        if record is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace transaction was not found.",
                operation="get_workspace_transaction",
                metadata={"transaction_id": str(transaction_id)},
            )
        return record

    def update_transaction(
        self,
        record: WorkspaceTransactionRecord,
        *,
        expected_revision: int,
    ) -> WorkspaceTransactionRecord:
        def mutation(state: _MutableIntegrationState) -> WorkspaceTransactionRecord:
            current_value = state.transactions.get(record.transaction_id)
            if current_value is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace transaction was not found.",
                    workspace_id=record.workspace_id,
                    operation="update_workspace_transaction",
                )
            current = WorkspaceTransactionRecord.from_dict(current_value)
            if current.revision != int(expected_revision):
                raise self._revision_conflict(
                    workspace_id=record.workspace_id,
                    operation="update_workspace_transaction",
                    expected=expected_revision,
                    actual=current.revision,
                )
            if record.revision <= current.revision:
                raise self._revision_conflict(
                    workspace_id=record.workspace_id,
                    operation="update_workspace_transaction",
                    expected=f"> {current.revision}",
                    actual=record.revision,
                )
            state.transactions[record.transaction_id] = record.to_dict()
            return record

        return self._transaction(mutation)

    def list_transactions(
        self,
        workspace_id: str,
        *,
        include_terminal: bool = True,
    ) -> tuple[WorkspaceTransactionRecord, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "transactions",
            state.transactions,
            WorkspaceTransactionRecord.from_dict,
        )
        if not include_terminal:
            records = tuple(item for item in records if not item.terminal)
        return tuple(sorted(records, key=lambda item: (item.started_at, item.transaction_id)))

    # Manifests --------------------------------------------------------

    def put_manifest(self, manifest: WorkspaceTreeManifest) -> WorkspaceTreeManifest:
        def mutation(state: _MutableIntegrationState) -> WorkspaceTreeManifest:
            existing = state.manifests.get(manifest.manifest_id)
            if existing is not None:
                prior = WorkspaceTreeManifest.from_dict(existing)
                if prior.to_dict() != manifest.to_dict():
                    raise self._idempotency_conflict(
                        workspace_id=manifest.workspace_id,
                        operation="put_workspace_manifest",
                        key=manifest.manifest_id,
                        expected=stable_digest(prior.to_dict()),
                        actual=stable_digest(manifest.to_dict()),
                    )
                return prior
            state.manifests[manifest.manifest_id] = manifest.to_dict()
            state.index(manifest.workspace_id, "manifests", manifest.manifest_id)
            return manifest

        return self._transaction(mutation)

    def get_manifest(self, manifest_id: str) -> WorkspaceTreeManifest | None:
        value = self._load().manifests.get(str(manifest_id))
        return WorkspaceTreeManifest.from_dict(value) if value is not None else None

    def require_manifest(self, manifest_id: str) -> WorkspaceTreeManifest:
        manifest = self.get_manifest(manifest_id)
        if manifest is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace tree manifest was not found.",
                operation="get_workspace_manifest",
                metadata={"manifest_id": str(manifest_id)},
            )
        return manifest

    def list_manifests(self, workspace_id: str) -> tuple[WorkspaceTreeManifest, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "manifests",
            state.manifests,
            WorkspaceTreeManifest.from_dict,
        )
        return tuple(sorted(records, key=lambda item: (item.created_at, item.manifest_id)))

    # Isolations and conflicts ----------------------------------------

    def create_isolation(self, record: IsolationRecord) -> IsolationRecord:
        def mutation(state: _MutableIntegrationState) -> IsolationRecord:
            existing = state.isolations.get(record.isolation_id)
            if existing is not None:
                prior = IsolationRecord.from_dict(existing)
                if record.idempotency_key and prior.idempotency_key == record.idempotency_key:
                    return prior
                raise self._idempotency_conflict(
                    workspace_id=record.workspace_id,
                    operation="create_workspace_isolation",
                    key=record.isolation_id,
                    expected=prior.idempotency_key,
                    actual=record.idempotency_key,
                )
            state.isolations[record.isolation_id] = record.to_dict()
            state.index(record.workspace_id, "isolations", record.isolation_id)
            return record

        return self._transaction(mutation)

    def get_isolation(self, isolation_id: str) -> IsolationRecord | None:
        value = self._load().isolations.get(str(isolation_id))
        return IsolationRecord.from_dict(value) if value is not None else None

    def require_isolation(self, isolation_id: str) -> IsolationRecord:
        record = self.get_isolation(isolation_id)
        if record is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace isolation was not found.",
                operation="get_workspace_isolation",
                metadata={"isolation_id": str(isolation_id)},
            )
        return record

    def update_isolation(self, record: IsolationRecord, *, expected_revision: int) -> IsolationRecord:
        def mutation(state: _MutableIntegrationState) -> IsolationRecord:
            current_value = state.isolations.get(record.isolation_id)
            if current_value is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace isolation was not found.",
                    workspace_id=record.workspace_id,
                    operation="update_workspace_isolation",
                )
            current = IsolationRecord.from_dict(current_value)
            if current.revision != int(expected_revision) or record.revision <= current.revision:
                raise self._revision_conflict(
                    workspace_id=record.workspace_id,
                    operation="update_workspace_isolation",
                    expected=expected_revision,
                    actual=current.revision,
                )
            state.isolations[record.isolation_id] = record.to_dict()
            return record

        return self._transaction(mutation)

    def list_isolations(
        self,
        workspace_id: str,
        *,
        include_terminal: bool = True,
    ) -> tuple[IsolationRecord, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "isolations",
            state.isolations,
            IsolationRecord.from_dict,
        )
        if not include_terminal:
            records = tuple(item for item in records if not item.terminal)
        return tuple(sorted(records, key=lambda item: (item.created_at, item.isolation_id)))

    def put_conflicts(self, conflicts: Iterable[MergeConflict]) -> tuple[MergeConflict, ...]:
        selected = tuple(conflicts)
        if not selected:
            return ()

        def mutation(state: _MutableIntegrationState) -> tuple[MergeConflict, ...]:
            result: list[MergeConflict] = []
            for conflict in selected:
                existing = state.conflicts.get(conflict.conflict_id)
                if existing is not None:
                    prior = MergeConflict.from_dict(existing)
                    if prior.to_dict() != conflict.to_dict():
                        raise self._idempotency_conflict(
                            workspace_id=conflict.workspace_id,
                            operation="put_workspace_merge_conflict",
                            key=conflict.conflict_id,
                            expected=stable_digest(prior.to_dict()),
                            actual=stable_digest(conflict.to_dict()),
                        )
                    result.append(prior)
                    continue
                state.conflicts[conflict.conflict_id] = conflict.to_dict()
                state.index(conflict.workspace_id, "conflicts", conflict.conflict_id)
                result.append(conflict)
            return tuple(result)

        return self._transaction(mutation)

    def get_conflict(self, conflict_id: str) -> MergeConflict | None:
        value = self._load().conflicts.get(str(conflict_id))
        return MergeConflict.from_dict(value) if value is not None else None

    def list_conflicts(
        self,
        workspace_id: str,
        *,
        isolation_id: str = "",
        unresolved_only: bool = False,
    ) -> tuple[MergeConflict, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "conflicts",
            state.conflicts,
            MergeConflict.from_dict,
        )
        if isolation_id:
            records = tuple(item for item in records if item.isolation_id == isolation_id)
        if unresolved_only:
            records = tuple(item for item in records if not item.resolved)
        return tuple(sorted(records, key=lambda item: (item.created_at, item.logical_path, item.conflict_id)))

    def resolve_conflict(
        self,
        conflict_id: str,
        *,
        resolution: str,
    ) -> MergeConflict:
        resolution_text = str(resolution or "").strip()
        if not resolution_text:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Conflict resolution text is required.",
                operation="resolve_workspace_merge_conflict",
            )

        def mutation(state: _MutableIntegrationState) -> MergeConflict:
            value = state.conflicts.get(str(conflict_id))
            if value is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace merge conflict was not found.",
                    operation="resolve_workspace_merge_conflict",
                    metadata={"conflict_id": str(conflict_id)},
                )
            current = MergeConflict.from_dict(value)
            updated = replace(current, resolved=True, resolution=resolution_text)
            state.conflicts[current.conflict_id] = updated.to_dict()
            return updated

        return self._transaction(mutation)

    # Rebind -----------------------------------------------------------

    def create_rebind(self, record: WorkspaceRebindRecord) -> WorkspaceRebindRecord:
        def mutation(state: _MutableIntegrationState) -> WorkspaceRebindRecord:
            existing = state.rebinds.get(record.rebind_id)
            if existing is not None:
                prior = WorkspaceRebindRecord.from_dict(existing)
                if record.idempotency_key and prior.idempotency_key == record.idempotency_key:
                    return prior
                raise self._idempotency_conflict(
                    workspace_id=record.workspace_id,
                    operation="create_workspace_rebind",
                    key=record.rebind_id,
                    expected=prior.idempotency_key,
                    actual=record.idempotency_key,
                )
            state.rebinds[record.rebind_id] = record.to_dict()
            state.index(record.workspace_id, "rebinds", record.rebind_id)
            return record

        return self._transaction(mutation)

    def get_rebind(self, rebind_id: str) -> WorkspaceRebindRecord | None:
        value = self._load().rebinds.get(str(rebind_id))
        return WorkspaceRebindRecord.from_dict(value) if value is not None else None

    def require_rebind(self, rebind_id: str) -> WorkspaceRebindRecord:
        record = self.get_rebind(rebind_id)
        if record is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Workspace rebind was not found.",
                operation="get_workspace_rebind",
                metadata={"rebind_id": str(rebind_id)},
            )
        return record

    def update_rebind(self, record: WorkspaceRebindRecord, *, expected_revision: int) -> WorkspaceRebindRecord:
        def mutation(state: _MutableIntegrationState) -> WorkspaceRebindRecord:
            current_value = state.rebinds.get(record.rebind_id)
            if current_value is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace rebind was not found.",
                    workspace_id=record.workspace_id,
                    operation="update_workspace_rebind",
                )
            current = WorkspaceRebindRecord.from_dict(current_value)
            if current.revision != int(expected_revision) or record.revision <= current.revision:
                raise self._revision_conflict(
                    workspace_id=record.workspace_id,
                    operation="update_workspace_rebind",
                    expected=expected_revision,
                    actual=current.revision,
                )
            state.rebinds[record.rebind_id] = record.to_dict()
            return record

        return self._transaction(mutation)

    def list_rebinds(
        self,
        workspace_id: str,
        *,
        include_terminal: bool = True,
    ) -> tuple[WorkspaceRebindRecord, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "rebinds",
            state.rebinds,
            WorkspaceRebindRecord.from_dict,
        )
        if not include_terminal:
            records = tuple(item for item in records if not item.terminal)
        return tuple(sorted(records, key=lambda item: (item.started_at, item.rebind_id)))

    # Recovery inputs --------------------------------------------------

    def put_recovery_input(self, recovery: WorkspaceRecoveryInput) -> WorkspaceRecoveryInput:
        def mutation(state: _MutableIntegrationState) -> WorkspaceRecoveryInput:
            existing = state.recovery_inputs.get(recovery.recovery_input_id)
            if existing is not None:
                prior = WorkspaceRecoveryInput.from_dict(existing)
                if prior.to_dict() != recovery.to_dict():
                    raise self._idempotency_conflict(
                        workspace_id=recovery.workspace_id,
                        operation="put_workspace_recovery_input",
                        key=recovery.recovery_input_id,
                        expected=stable_digest(prior.to_dict()),
                        actual=stable_digest(recovery.to_dict()),
                    )
                return prior
            state.recovery_inputs[recovery.recovery_input_id] = recovery.to_dict()
            state.index(recovery.workspace_id, "recovery_inputs", recovery.recovery_input_id)
            return recovery

        return self._transaction(mutation)

    def get_recovery_input(self, recovery_input_id: str) -> WorkspaceRecoveryInput | None:
        value = self._load().recovery_inputs.get(str(recovery_input_id))
        return WorkspaceRecoveryInput.from_dict(value) if value is not None else None

    def list_recovery_inputs(self, workspace_id: str) -> tuple[WorkspaceRecoveryInput, ...]:
        state = self._load()
        records = self._indexed_records(
            state,
            workspace_id,
            "recovery_inputs",
            state.recovery_inputs,
            WorkspaceRecoveryInput.from_dict,
        )
        return tuple(sorted(records, key=lambda item: (item.created_at, item.recovery_input_id)))

    # Receipts and publications ---------------------------------------

    def append_receipt(self, receipt: WorkerWorkspaceReceipt) -> WorkerWorkspaceReceipt:
        ensure_path_free_projection(receipt.to_dict())

        def mutation(state: _MutableIntegrationState) -> WorkerWorkspaceReceipt:
            existing = state.receipts.get(receipt.receipt_id)
            if existing is not None:
                if existing != receipt.to_dict():
                    raise self._idempotency_conflict(
                        workspace_id=receipt.workspace_id,
                        operation="append_worker_workspace_receipt",
                        key=receipt.receipt_id,
                        expected=stable_digest(existing),
                        actual=stable_digest(receipt.to_dict()),
                    )
                return receipt
            state.receipts[receipt.receipt_id] = receipt.to_dict()
            state.index(receipt.workspace_id, "receipts", receipt.receipt_id)
            self._prune_indexed_collection(
                state,
                receipt.workspace_id,
                "receipts",
                state.receipts,
                self.max_receipts_per_workspace,
            )
            return receipt

        return self._transaction(mutation)

    def list_receipts(
        self,
        workspace_id: str,
        *,
        transaction_id: str = "",
        isolation_id: str = "",
        rebind_id: str = "",
    ) -> tuple[dict[str, Any], ...]:
        state = self._load()
        identifiers = state.workspace_indexes.get(workspace_id, {}).get("receipts", [])
        values = [copy.deepcopy(state.receipts[item]) for item in identifiers if item in state.receipts]
        if transaction_id:
            values = [item for item in values if str(item.get("transaction_id") or "") == transaction_id]
        if isolation_id:
            values = [item for item in values if str(item.get("isolation_id") or "") == isolation_id]
        if rebind_id:
            values = [item for item in values if str(item.get("rebind_id") or "") == rebind_id]
        for value in values:
            ensure_path_free_projection(value)
        return tuple(values)

    def put_publication(self, publication: ArtifactPublication) -> ArtifactPublication:
        ensure_path_free_projection(publication.to_dict())

        def mutation(state: _MutableIntegrationState) -> ArtifactPublication:
            existing = state.publications.get(publication.publication_id)
            if existing is not None:
                if existing != publication.to_dict():
                    raise self._idempotency_conflict(
                        workspace_id=publication.workspace_id,
                        operation="put_workspace_artifact_publication",
                        key=publication.publication_id,
                        expected=stable_digest(existing),
                        actual=stable_digest(publication.to_dict()),
                    )
                return publication
            state.publications[publication.publication_id] = publication.to_dict()
            state.index(publication.workspace_id, "publications", publication.publication_id)
            return publication

        return self._transaction(mutation)

    def list_publications(self, workspace_id: str) -> tuple[dict[str, Any], ...]:
        state = self._load()
        identifiers = state.workspace_indexes.get(workspace_id, {}).get("publications", [])
        values = tuple(copy.deepcopy(state.publications[item]) for item in identifiers if item in state.publications)
        for value in values:
            ensure_path_free_projection(value)
        return values

    # Endpoints and reference migration -------------------------------

    def put_endpoint(self, endpoint: LocalWorkspaceEndpoint) -> LocalWorkspaceEndpoint:
        def mutation(state: _MutableIntegrationState) -> LocalWorkspaceEndpoint:
            existing = state.endpoints.get(endpoint.endpoint_id)
            if existing is not None:
                prior = LocalWorkspaceEndpoint.from_dict(existing)
                if prior.to_private_dict() != endpoint.to_private_dict():
                    raise self._idempotency_conflict(
                        workspace_id="",
                        operation="put_local_workspace_endpoint",
                        key=endpoint.endpoint_id,
                        expected=stable_digest(prior.to_private_dict()),
                        actual=stable_digest(endpoint.to_private_dict()),
                    )
                return prior
            state.endpoints[endpoint.endpoint_id] = endpoint.to_private_dict()
            return endpoint

        return self._transaction(mutation)

    def get_endpoint(self, endpoint_id: str) -> LocalWorkspaceEndpoint | None:
        value = self._load().endpoints.get(str(endpoint_id))
        return LocalWorkspaceEndpoint.from_dict(value) if value is not None else None

    def require_endpoint(self, endpoint_id: str) -> LocalWorkspaceEndpoint:
        endpoint = self.get_endpoint(endpoint_id)
        if endpoint is None:
            raise WorkspaceError(
                WorkspaceErrorCode.NOT_FOUND,
                "Local workspace endpoint was not found.",
                operation="get_local_workspace_endpoint",
                metadata={"endpoint_id": str(endpoint_id)},
            )
        if not endpoint.enabled:
            raise WorkspaceError(
                WorkspaceErrorCode.BACKEND_UNAVAILABLE,
                "Local workspace endpoint is disabled.",
                operation="get_local_workspace_endpoint",
                metadata={"endpoint_id": endpoint.endpoint_id},
            )
        return endpoint

    def list_endpoints(self, *, include_disabled: bool = False) -> tuple[LocalWorkspaceEndpoint, ...]:
        state = self._load()
        records = tuple(LocalWorkspaceEndpoint.from_dict(item) for item in state.endpoints.values())
        if not include_disabled:
            records = tuple(item for item in records if item.enabled)
        return tuple(sorted(records, key=lambda item: item.endpoint_id))

    def put_reference_migration(
        self,
        migration: WorkspaceReferenceMigration,
    ) -> WorkspaceReferenceMigration:
        ensure_path_free_projection(migration.to_dict())

        def mutation(state: _MutableIntegrationState) -> WorkspaceReferenceMigration:
            existing = state.reference_migrations.get(migration.migration_id)
            if existing is not None:
                if existing != migration.to_dict():
                    raise self._idempotency_conflict(
                        workspace_id=migration.workspace_id,
                        operation="put_workspace_reference_migration",
                        key=migration.migration_id,
                        expected=stable_digest(existing),
                        actual=stable_digest(migration.to_dict()),
                    )
                return migration
            state.reference_migrations[migration.migration_id] = migration.to_dict()
            state.index(migration.workspace_id, "reference_migrations", migration.migration_id)
            return migration

        return self._transaction(mutation)

    def list_reference_migrations(self, workspace_id: str) -> tuple[dict[str, Any], ...]:
        state = self._load()
        identifiers = state.workspace_indexes.get(workspace_id, {}).get("reference_migrations", [])
        values = tuple(
            copy.deepcopy(state.reference_migrations[item])
            for item in identifiers
            if item in state.reference_migrations
        )
        for value in values:
            ensure_path_free_projection(value)
        return values

    # Envelope audit ---------------------------------------------------

    def audit_envelope(
        self,
        envelope: WorkspaceAccessEnvelope,
        *,
        action: str,
        ok: bool,
        reason_code: str = "",
    ) -> dict[str, Any]:
        action_text = str(action or "").strip()
        if not action_text:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Workspace envelope audit action is required.",
                operation="audit_workspace_envelope",
            )
        audit_id = stable_digest(
            {
                "envelope_id": envelope.envelope_id,
                "action": action_text,
                "owner_epoch": envelope.owner_epoch,
                "nonce": envelope.nonce,
                "ok": bool(ok),
                "reason_code": str(reason_code or ""),
            }
        )
        value = {
            "audit_id": audit_id,
            "envelope_id": envelope.envelope_id,
            "workspace_id": envelope.workspace_id,
            "task_id": envelope.task_id,
            "audience": envelope.audience,
            "owner_epoch": envelope.owner_epoch,
            "binding_revision": envelope.binding_revision,
            "capability_revision": envelope.capability_revision,
            "lease_id": envelope.lease_id,
            "action": action_text,
            "ok": bool(ok),
            "reason_code": str(reason_code or ""),
            "created_at": utc_now(),
            "physical_location_redacted": True,
        }
        ensure_path_free_projection(value)

        def mutation(state: _MutableIntegrationState) -> dict[str, Any]:
            existing = state.envelope_audits.get(audit_id)
            if existing is not None:
                return copy.deepcopy(existing)
            state.envelope_audits[audit_id] = copy.deepcopy(value)
            state.index(envelope.workspace_id, "envelope_audits", audit_id)
            self._prune_indexed_collection(
                state,
                envelope.workspace_id,
                "envelope_audits",
                state.envelope_audits,
                self.max_envelope_audits_per_workspace,
            )
            return copy.deepcopy(value)

        return self._transaction(mutation)

    def list_envelope_audits(self, workspace_id: str) -> tuple[dict[str, Any], ...]:
        state = self._load()
        identifiers = state.workspace_indexes.get(workspace_id, {}).get("envelope_audits", [])
        values = tuple(copy.deepcopy(state.envelope_audits[item]) for item in identifiers if item in state.envelope_audits)
        for value in values:
            ensure_path_free_projection(value)
        return values

    # Idempotency ------------------------------------------------------

    def claim_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        fingerprint: str,
        workspace_id: str,
        result_ref: str = "",
    ) -> tuple[dict[str, Any], bool]:
        namespace_text = str(namespace or "").strip()
        key_text = str(key or "").strip()
        fingerprint_text = str(fingerprint or "").strip()
        if not namespace_text or not key_text or not fingerprint_text:
            raise WorkspaceError(
                WorkspaceErrorCode.INVALID_ARGUMENT,
                "Idempotency namespace, key, and fingerprint are required.",
                workspace_id=workspace_id,
                operation="claim_workspace_idempotency",
            )
        composite_key = f"{namespace_text}:{key_text}"

        def mutation(state: _MutableIntegrationState) -> tuple[dict[str, Any], bool]:
            existing = state.idempotency.get(composite_key)
            if existing is not None:
                if str(existing.get("fingerprint") or "") != fingerprint_text:
                    raise self._idempotency_conflict(
                        workspace_id=workspace_id,
                        operation="claim_workspace_idempotency",
                        key=composite_key,
                        expected=existing.get("fingerprint"),
                        actual=fingerprint_text,
                    )
                return copy.deepcopy(existing), False
            value = {
                "namespace": namespace_text,
                "key": key_text,
                "fingerprint": fingerprint_text,
                "workspace_id": workspace_id,
                "result_ref": str(result_ref or ""),
                "claimed_at": utc_now(),
                "completed_at": "",
            }
            state.idempotency[composite_key] = value
            return copy.deepcopy(value), True

        return self._transaction(mutation)

    def complete_idempotency(
        self,
        *,
        namespace: str,
        key: str,
        fingerprint: str,
        result_ref: str,
    ) -> dict[str, Any]:
        composite_key = f"{str(namespace or '').strip()}:{str(key or '').strip()}"

        def mutation(state: _MutableIntegrationState) -> dict[str, Any]:
            existing = state.idempotency.get(composite_key)
            if existing is None:
                raise WorkspaceError(
                    WorkspaceErrorCode.NOT_FOUND,
                    "Workspace idempotency claim was not found.",
                    operation="complete_workspace_idempotency",
                    metadata={"key": composite_key},
                )
            if str(existing.get("fingerprint") or "") != str(fingerprint or ""):
                raise self._idempotency_conflict(
                    workspace_id=str(existing.get("workspace_id") or ""),
                    operation="complete_workspace_idempotency",
                    key=composite_key,
                    expected=existing.get("fingerprint"),
                    actual=str(fingerprint or ""),
                )
            updated = {
                **existing,
                "result_ref": str(result_ref or ""),
                "completed_at": utc_now(),
            }
            state.idempotency[composite_key] = updated
            return copy.deepcopy(updated)

        return self._transaction(mutation)

    def idempotency_record(self, namespace: str, key: str) -> dict[str, Any] | None:
        composite_key = f"{str(namespace or '').strip()}:{str(key or '').strip()}"
        value = self._load().idempotency.get(composite_key)
        return copy.deepcopy(value) if value is not None else None

    # Recovery queries -------------------------------------------------

    def interrupted_operations(self) -> dict[str, tuple[str, ...]]:
        state = self._load()
        return {
            "transactions": tuple(
                item.transaction_id
                for item in (WorkspaceTransactionRecord.from_dict(value) for value in state.transactions.values())
                if not item.terminal
            ),
            "isolations": tuple(
                item.isolation_id
                for item in (IsolationRecord.from_dict(value) for value in state.isolations.values())
                if not item.terminal
            ),
            "rebinds": tuple(
                item.rebind_id
                for item in (WorkspaceRebindRecord.from_dict(value) for value in state.rebinds.values())
                if not item.terminal
            ),
        }

    def workspace_summary(self, workspace_id: str) -> dict[str, Any]:
        state = self._load()
        indexes = state.workspace_indexes.get(str(workspace_id), {})
        value = {
            "workspace_id": str(workspace_id),
            "transactions": len(indexes.get("transactions", [])),
            "manifests": len(indexes.get("manifests", [])),
            "isolations": len(indexes.get("isolations", [])),
            "conflicts": len(indexes.get("conflicts", [])),
            "rebinds": len(indexes.get("rebinds", [])),
            "recovery_inputs": len(indexes.get("recovery_inputs", [])),
            "receipts": len(indexes.get("receipts", [])),
            "publications": len(indexes.get("publications", [])),
            "reference_migrations": len(indexes.get("reference_migrations", [])),
            "envelope_audits": len(indexes.get("envelope_audits", [])),
            "journal_revision": state.revision,
            "physical_location_redacted": True,
        }
        ensure_path_free_projection(value)
        return value

    # Internal ---------------------------------------------------------

    def _load(self) -> _MutableIntegrationState:
        self._ensure_enabled()
        with self._guard:
            return self._load_locked()

    def _load_locked(self) -> _MutableIntegrationState:
        try:
            value = read_json_object(self.path)
        except FileNotFoundError as error:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.STORE_CORRUPT,
                "Workspace integration journal is missing.",
                operation="load_workspace_integration_journal",
                path=str(self.path),
            ) from error
        return _MutableIntegrationState.from_dict(value)

    def _transaction(self, mutation: Callable[[_MutableIntegrationState], T]) -> T:
        self._ensure_enabled()
        with self._guard:
            state = self._load_locked()
            result = mutation(state)
            state.revision += 1
            state.committed_at = utc_now()
            atomic_write_json(self.path, state.to_dict())
            return result

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise WorkspaceStoreError(
                WorkspaceErrorCode.DISABLED,
                "Workspace integration journal is disabled; composite mutations fail closed.",
                operation="workspace_integration_store",
            )

    @staticmethod
    def _indexed_records(
        state: _MutableIntegrationState,
        workspace_id: str,
        collection: str,
        values: Mapping[str, Mapping[str, Any]],
        decoder: Callable[[Mapping[str, Any]], T],
    ) -> tuple[T, ...]:
        identifiers = state.workspace_indexes.get(str(workspace_id), {}).get(collection, [])
        return tuple(decoder(values[item]) for item in identifiers if item in values)

    @staticmethod
    def _prune_indexed_collection(
        state: _MutableIntegrationState,
        workspace_id: str,
        collection: str,
        values: dict[str, dict[str, Any]],
        limit: int,
    ) -> None:
        identifiers = state.workspace_indexes.get(workspace_id, {}).get(collection, [])
        while len(identifiers) > limit:
            removed = identifiers.pop(0)
            values.pop(removed, None)

    @staticmethod
    def _revision_conflict(
        *,
        workspace_id: str,
        operation: str,
        expected: Any,
        actual: Any,
    ) -> WorkspaceStoreError:
        return WorkspaceStoreError(
            WorkspaceErrorCode.STORE_REVISION_CONFLICT,
            "Workspace integration journal revision changed concurrently.",
            retryable=True,
            workspace_id=workspace_id,
            operation=operation,
            expected=expected,
            actual=actual,
        )

    @staticmethod
    def _idempotency_conflict(
        *,
        workspace_id: str,
        operation: str,
        key: str,
        expected: Any,
        actual: Any,
    ) -> WorkspaceStoreError:
        return WorkspaceStoreError(
            WorkspaceErrorCode.IDEMPOTENCY_CONFLICT,
            "Workspace integration idempotency key was reused with a different request.",
            workspace_id=workspace_id,
            operation=operation,
            expected=expected,
            actual=actual,
            metadata={"idempotency_key": key},
        )


__all__ = [
    "IntegrationStoreSnapshot",
    "WorkspaceIntegrationStore",
]
