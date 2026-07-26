from __future__ import annotations

import hashlib
import json
import secrets
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .lifecycle import LifecyclePhase, LifecycleRecorder, Severity
from .migration_adapters import (
    MigrationAdapter,
    MigrationAdapterError,
    MigrationMutation,
    MigrationProbe,
)
from .migration_journal import (
    MigrationBackupRecord,
    MigrationJournal,
    MigrationJournalError,
    MigrationLease,
    MigrationStepRecord,
    MigrationStepState,
    MigrationTransactionRecord,
    MigrationTransactionState,
)


class MigrationRuntimeError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        transaction_id: str = "",
        adapter_id: str = "",
        retryable: bool = False,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.transaction_id = transaction_id
        self.adapter_id = adapter_id
        self.retryable = retryable
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-runtime-error/v1",
            "code": self.code,
            "message": str(self),
            "transaction_id": self.transaction_id,
            "adapter_id": self.adapter_id,
            "retryable": self.retryable,
            "details": dict(self.details),
        }


@dataclass(frozen=True, slots=True)
class MigrationPlanStep:
    ordinal: int
    adapter_id: str
    owner: str
    source_version: int
    target_version: int
    dependencies: tuple[str, ...]
    probe: MigrationProbe
    preflight: Mapping[str, Any]

    @property
    def migration_required(self) -> bool:
        return self.probe.migration_required

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-plan-step/v1",
            "ordinal": self.ordinal,
            "adapter_id": self.adapter_id,
            "owner": self.owner,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "dependencies": list(self.dependencies),
            "migration_required": self.migration_required,
            "probe": self.probe.to_dict(),
            "preflight": dict(self.preflight),
        }


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    plan_id: str
    target_version: int
    configuration_digest: str
    created_at_ns: int
    steps: tuple[MigrationPlanStep, ...]
    digest: str

    @property
    def migration_required(self) -> bool:
        return any(step.migration_required for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-plan/v1",
            "plan_id": self.plan_id,
            "target_version": self.target_version,
            "configuration_digest": self.configuration_digest,
            "created_at_ns": self.created_at_ns,
            "migration_required": self.migration_required,
            "steps": [step.to_dict() for step in self.steps],
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class MigrationStepReceipt:
    adapter_id: str
    owner: str
    source_version: int
    target_version: int
    backups: tuple[MigrationBackupRecord, ...]
    mutation: MigrationMutation
    verification: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-step-receipt/v1",
            "adapter_id": self.adapter_id,
            "owner": self.owner,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "backups": [item.to_dict() for item in self.backups],
            "mutation": self.mutation.to_dict(),
            "verification": dict(self.verification),
        }


@dataclass(frozen=True, slots=True)
class MigrationReceipt:
    receipt_id: str
    transaction: MigrationTransactionRecord
    plan_digest: str
    configuration_digest: str
    process_generation: str
    steps: tuple[MigrationStepReceipt, ...]
    committed_at_ns: int
    digest: str

    @property
    def migrated(self) -> bool:
        return any(step.mutation.changed for step in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-receipt/v1",
            "receipt_id": self.receipt_id,
            "transaction": self.transaction.to_dict(),
            "plan_digest": self.plan_digest,
            "configuration_digest": self.configuration_digest,
            "process_generation": self.process_generation,
            "migrated": self.migrated,
            "steps": [item.to_dict() for item in self.steps],
            "committed_at_ns": self.committed_at_ns,
            "digest": self.digest,
            "source_store_fallback": False,
        }


@dataclass(frozen=True, slots=True)
class MigrationRecoveryReceipt:
    transaction_id: str
    original_state: str
    final_state: str
    restored_adapters: tuple[str, ...]
    restored_backups: tuple[str, ...]
    recovered_at_ns: int
    clean: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.migration-recovery-receipt/v1",
            "transaction_id": self.transaction_id,
            "original_state": self.original_state,
            "final_state": self.final_state,
            "restored_adapters": list(self.restored_adapters),
            "restored_backups": list(self.restored_backups),
            "recovered_at_ns": self.recovered_at_ns,
            "clean": self.clean,
            "source_store_fallback": False,
        }


class MigrationPlanBuilder:
    def __init__(
        self,
        adapters: Sequence[MigrationAdapter],
        *,
        target_version: int,
        configuration_digest: str,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if target_version < 1:
            raise ValueError("target_version must be positive")
        self.adapters = tuple(adapters)
        self.target_version = target_version
        self.configuration_digest = configuration_digest
        self._clock_ns = clock_ns

    def build(self) -> MigrationPlan:
        adapters = self._validate_adapters()
        ordered = self._topological_order(adapters)
        steps: list[MigrationPlanStep] = []
        for ordinal, adapter in enumerate(ordered):
            probe = adapter.probe()
            if probe.target_version != adapter.target_version:
                raise MigrationRuntimeError(
                    "migration_probe_target_mismatch",
                    "migration adapter returned an inconsistent target version",
                    adapter_id=adapter.adapter_id,
                    details={
                        "adapter_target": adapter.target_version,
                        "probe_target": probe.target_version,
                    },
                )
            preflight = dict(adapter.preflight(probe))
            if preflight.get("ready") is not True:
                raise MigrationRuntimeError(
                    "migration_preflight_rejected",
                    "migration adapter preflight did not report ready",
                    adapter_id=adapter.adapter_id,
                    details=preflight,
                )
            steps.append(
                MigrationPlanStep(
                    ordinal=ordinal,
                    adapter_id=adapter.adapter_id,
                    owner=adapter.owner,
                    source_version=probe.source_version,
                    target_version=probe.target_version,
                    dependencies=tuple(adapter.dependencies),
                    probe=probe,
                    preflight=MappingProxyType(preflight),
                )
            )
        created_at_ns = self._clock_ns()
        body = {
            "target_version": self.target_version,
            "configuration_digest": self.configuration_digest,
            "steps": [
                {
                    "ordinal": step.ordinal,
                    "adapter_id": step.adapter_id,
                    "owner": step.owner,
                    "source_version": step.source_version,
                    "target_version": step.target_version,
                    "dependencies": list(step.dependencies),
                    "probe": {
                        "path": step.probe.path,
                        "exists": step.probe.exists,
                        "mutable": step.probe.mutable,
                        "state_digest": step.probe.state_digest,
                    },
                    # Capacity observations are retained in the plan receipt,
                    # but only semantic readiness participates in freshness.
                    "preflight": {
                        "ready": step.preflight.get("ready"),
                        "operation": step.preflight.get("operation", ""),
                    },
                }
                for step in steps
            ],
        }
        digest = _digest(body)
        return MigrationPlan(
            plan_id="migration-plan-" + digest.removeprefix("sha256:")[:24],
            target_version=self.target_version,
            configuration_digest=self.configuration_digest,
            created_at_ns=created_at_ns,
            steps=tuple(steps),
            digest=digest,
        )

    def _validate_adapters(self) -> dict[str, MigrationAdapter]:
        if not self.adapters:
            raise MigrationRuntimeError(
                "migration_adapters_empty",
                "at least one migration adapter is required",
            )
        result: dict[str, MigrationAdapter] = {}
        owners: dict[str, str] = {}
        for adapter in self.adapters:
            if adapter.adapter_id in result:
                raise MigrationRuntimeError(
                    "migration_adapter_duplicate",
                    "migration adapter id is duplicated",
                    adapter_id=adapter.adapter_id,
                )
            if adapter.owner in owners:
                raise MigrationRuntimeError(
                    "migration_owner_duplicate",
                    "multiple migration adapters claim the same canonical owner",
                    adapter_id=adapter.adapter_id,
                    details={
                        "owner": adapter.owner,
                        "existing_adapter": owners[adapter.owner],
                    },
                )
            if adapter.target_version < 1:
                raise MigrationRuntimeError(
                    "migration_adapter_target_invalid",
                    "migration adapter target version must be positive",
                    adapter_id=adapter.adapter_id,
                )
            result[adapter.adapter_id] = adapter
            owners[adapter.owner] = adapter.adapter_id
        for adapter in self.adapters:
            missing = [
                dependency
                for dependency in adapter.dependencies
                if dependency not in result
            ]
            if missing:
                raise MigrationRuntimeError(
                    "migration_adapter_dependency_missing",
                    "migration adapter dependency is not registered",
                    adapter_id=adapter.adapter_id,
                    details={"missing": missing},
                )
            if adapter.adapter_id in adapter.dependencies:
                raise MigrationRuntimeError(
                    "migration_adapter_self_dependency",
                    "migration adapter cannot depend on itself",
                    adapter_id=adapter.adapter_id,
                )
        return result

    def _topological_order(
        self,
        adapters: Mapping[str, MigrationAdapter],
    ) -> tuple[MigrationAdapter, ...]:
        incoming: dict[str, int] = {
            adapter_id: 0 for adapter_id in adapters
        }
        children: dict[str, list[str]] = defaultdict(list)
        for adapter in adapters.values():
            for dependency in adapter.dependencies:
                incoming[adapter.adapter_id] += 1
                children[dependency].append(adapter.adapter_id)
        ready = deque(sorted(key for key, degree in incoming.items() if degree == 0))
        ordered: list[MigrationAdapter] = []
        while ready:
            adapter_id = ready.popleft()
            ordered.append(adapters[adapter_id])
            for child in sorted(children[adapter_id]):
                incoming[child] -= 1
                if incoming[child] == 0:
                    ready.append(child)
        if len(ordered) != len(adapters):
            cycle = sorted(key for key, degree in incoming.items() if degree > 0)
            raise MigrationRuntimeError(
                "migration_adapter_dependency_cycle",
                "migration adapter dependencies contain a cycle",
                details={"cycle_members": cycle},
            )
        return tuple(ordered)


class MigrationRuntime:
    def __init__(
        self,
        journal: MigrationJournal,
        adapters: Sequence[MigrationAdapter],
        *,
        backup_root: Path | str,
        process_generation: str,
        configuration_digest: str,
        target_version: int,
        lease_seconds: int = 120,
        lifecycle: LifecycleRecorder | None = None,
        crash_hook: Callable[[str, str, str], Any] | None = None,
        clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if not process_generation.strip():
            raise ValueError("process_generation is required")
        if not configuration_digest.startswith("sha256:"):
            raise ValueError("configuration_digest must use sha256")
        if not 5 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 5 and 3600")
        self.journal = journal
        self.adapters = tuple(adapters)
        self.backup_root = Path(backup_root).resolve()
        self.process_generation = process_generation
        self.configuration_digest = configuration_digest
        self.target_version = target_version
        self.lease_seconds = lease_seconds
        self.lifecycle = lifecycle
        self.crash_hook = crash_hook
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._adapter_map = {adapter.adapter_id: adapter for adapter in adapters}
        if len(self._adapter_map) != len(self.adapters):
            raise ValueError("migration adapter ids must be unique")

    def plan(self) -> MigrationPlan:
        return MigrationPlanBuilder(
            self.adapters,
            target_version=self.target_version,
            configuration_digest=self.configuration_digest,
            clock_ns=self._clock_ns,
        ).build()

    def recover_incomplete(self) -> MigrationRecoveryReceipt | None:
        with self._lock:
            active = self.journal.active_transaction()
            if active is None:
                return None
            lease = self.journal.acquire_lease(
                self.process_generation,
                ttl_seconds=self.lease_seconds,
            )
            try:
                try:
                    return self._recover_transaction(active, lease=lease)
                except Exception as error:
                    payload = self._error_payload(
                        error,
                        transaction_id=active.transaction_id,
                        adapter_id="",
                    )
                    self._mark_failure(
                        lease=lease,
                        transaction_id=active.transaction_id,
                        adapter_id="",
                        error=payload,
                    )
                    raise MigrationRuntimeError(
                        "migration_recovery_failed",
                        "restart recovery could not restore canonical owner state",
                        transaction_id=active.transaction_id,
                        retryable=False,
                        details={"cause": payload},
                    ) from error
            finally:
                self.journal.release_lease(lease)

    def execute(
        self,
        plan: MigrationPlan | None = None,
    ) -> MigrationReceipt:
        with self._lock:
            active = self.journal.active_transaction()
            if active is not None:
                raise MigrationRuntimeError(
                    "migration_recovery_required",
                    "an incomplete migration must be recovered before execution",
                    transaction_id=active.transaction_id,
                    retryable=True,
                    details={"state": active.state.value},
                )
            selected_plan = plan or self.plan()
            self._validate_plan(selected_plan)
            lease = self.journal.acquire_lease(
                self.process_generation,
                ttl_seconds=self.lease_seconds,
            )
            transaction_id = (
                "migration-"
                + selected_plan.digest.removeprefix("sha256:")[:18]
                + "-"
                + secrets.token_hex(4)
            )
            step_rows = tuple(
                {
                    "ordinal": step.ordinal,
                    "adapter_id": step.adapter_id,
                    "owner": step.owner,
                    "source_version": step.source_version,
                    "target_version": step.target_version,
                }
                for step in selected_plan.steps
            )
            transaction = self.journal.begin(
                lease=lease,
                transaction_id=transaction_id,
                configuration_digest=self.configuration_digest,
                process_generation=self.process_generation,
                plan_digest=selected_plan.digest,
                target_version=self.target_version,
                steps=step_rows,
            )
            receipts: list[MigrationStepReceipt] = []
            current_adapter = ""
            try:
                self._emit(
                    "migration.transaction.prepared",
                    attributes={
                        "transaction_id": transaction_id,
                        "plan_digest": selected_plan.digest,
                        "steps": len(selected_plan.steps),
                    },
                )
                self._crash("after_prepare", transaction_id, "")
                transaction = self.journal.transition_transaction(
                    lease=lease,
                    transaction_id=transaction_id,
                    target=MigrationTransactionState.APPLYING,
                )
                for step in selected_plan.steps:
                    current_adapter = step.adapter_id
                    adapter = self._adapter_map[step.adapter_id]
                    lease = self._renew_if_needed(lease)
                    backups = adapter.backup(
                        transaction_id=transaction_id,
                        backup_root=self.backup_root,
                    )
                    for backup in backups:
                        self.journal.add_backup(lease=lease, record=backup)
                    if backups:
                        self.journal.transition_step(
                            lease=lease,
                            transaction_id=transaction_id,
                            adapter_id=step.adapter_id,
                            target=MigrationStepState.BACKED_UP,
                        )
                    self._emit(
                        "migration.step.backed_up",
                        attributes={
                            "transaction_id": transaction_id,
                            "adapter_id": step.adapter_id,
                            "backup_count": len(backups),
                        },
                    )
                    self._crash("after_backup", transaction_id, step.adapter_id)
                    self.journal.transition_step(
                        lease=lease,
                        transaction_id=transaction_id,
                        adapter_id=step.adapter_id,
                        target=MigrationStepState.APPLYING,
                    )
                    mutation = adapter.apply(step.probe)
                    self.journal.transition_step(
                        lease=lease,
                        transaction_id=transaction_id,
                        adapter_id=step.adapter_id,
                        target=MigrationStepState.APPLIED,
                        mutation_digest=mutation.digest,
                    )
                    self._emit(
                        "migration.step.applied",
                        attributes={
                            "transaction_id": transaction_id,
                            "adapter_id": step.adapter_id,
                            "mutation_digest": mutation.digest,
                            "changed": mutation.changed,
                        },
                    )
                    self._crash("after_apply", transaction_id, step.adapter_id)
                    receipts.append(
                        MigrationStepReceipt(
                            adapter_id=step.adapter_id,
                            owner=step.owner,
                            source_version=step.source_version,
                            target_version=step.target_version,
                            backups=tuple(backups),
                            mutation=mutation,
                            verification=MappingProxyType({}),
                        )
                    )
                transaction = self.journal.transition_transaction(
                    lease=lease,
                    transaction_id=transaction_id,
                    target=MigrationTransactionState.VERIFYING,
                )
                verified: list[MigrationStepReceipt] = []
                for receipt in receipts:
                    current_adapter = receipt.adapter_id
                    adapter = self._adapter_map[receipt.adapter_id]
                    verification = dict(adapter.verify(receipt.mutation))
                    if verification.get("verified") is not True:
                        raise MigrationRuntimeError(
                            "migration_verification_rejected",
                            "migration adapter verification did not report success",
                            transaction_id=transaction_id,
                            adapter_id=receipt.adapter_id,
                            details=verification,
                        )
                    self.journal.transition_step(
                        lease=lease,
                        transaction_id=transaction_id,
                        adapter_id=receipt.adapter_id,
                        target=MigrationStepState.VERIFIED,
                        mutation_digest=receipt.mutation.digest,
                    )
                    verified.append(
                        MigrationStepReceipt(
                            adapter_id=receipt.adapter_id,
                            owner=receipt.owner,
                            source_version=receipt.source_version,
                            target_version=receipt.target_version,
                            backups=receipt.backups,
                            mutation=receipt.mutation,
                            verification=MappingProxyType(verification),
                        )
                    )
                    self._emit(
                        "migration.step.verified",
                        attributes={
                            "transaction_id": transaction_id,
                            "adapter_id": receipt.adapter_id,
                            "state_digest": verification.get("state_digest", ""),
                        },
                    )
                    self._crash("after_verify", transaction_id, receipt.adapter_id)
                transaction = self.journal.transition_transaction(
                    lease=lease,
                    transaction_id=transaction_id,
                    target=MigrationTransactionState.COMMITTED,
                )
                receipt = self._build_receipt(
                    transaction=transaction,
                    plan=selected_plan,
                    steps=tuple(verified),
                )
                self._emit(
                    "migration.transaction.committed",
                    attributes={
                        "transaction_id": transaction_id,
                        "receipt_digest": receipt.digest,
                        "migrated": receipt.migrated,
                    },
                )
                self._crash("after_commit", transaction_id, "")
                return receipt
            except Exception as error:
                current_transaction = self.journal.require_transaction(
                    transaction_id
                )
                if current_transaction.state is MigrationTransactionState.COMMITTED:
                    raise
                error_payload = self._error_payload(
                    error,
                    transaction_id=transaction_id,
                    adapter_id=current_adapter,
                )
                try:
                    self._mark_failure(
                        lease=lease,
                        transaction_id=transaction_id,
                        adapter_id=current_adapter,
                        error=error_payload,
                    )
                    recovery = self._rollback(
                        transaction_id,
                        lease=lease,
                        original_state=transaction.state.value,
                    )
                except BaseException as rollback_error:
                    raise MigrationRuntimeError(
                        "migration_and_rollback_failed",
                        "migration failed and rollback could not restore canonical owner state",
                        transaction_id=transaction_id,
                        adapter_id=current_adapter,
                        details={
                            "migration_error": error_payload,
                            "rollback_error": self._error_payload(
                                rollback_error,
                                transaction_id=transaction_id,
                                adapter_id=current_adapter,
                            ),
                        },
                    ) from rollback_error
                raise MigrationRuntimeError(
                    "migration_transaction_rolled_back",
                    "migration failed and was rolled back",
                    transaction_id=transaction_id,
                    adapter_id=current_adapter,
                    retryable=True,
                    details={
                        "cause": error_payload,
                        "recovery": recovery.to_dict(),
                    },
                ) from error
            finally:
                self.journal.release_lease(lease)

    def readiness(self) -> dict[str, Any]:
        integrity = self.journal.integrity_report()
        active = self.journal.active_transaction()
        latest = self.journal.latest_committed()
        probes: list[dict[str, Any]] = []
        blockers: list[str] = []
        for adapter in self.adapters:
            try:
                probe = adapter.probe()
                verification = adapter.verify(
                    MigrationMutation(
                        adapter_id=adapter.adapter_id,
                        owner=adapter.owner,
                        source_version=probe.source_version,
                        target_version=probe.target_version,
                        before_digest=probe.state_digest,
                        after_digest=probe.state_digest,
                        changed=False,
                        attributes={"operation": "readiness_probe"},
                    )
                )
                ready = verification.get("verified") is True
                probes.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "owner": adapter.owner,
                        "ready": ready,
                        "probe": probe.to_dict(),
                        "verification": dict(verification),
                    }
                )
                if not ready:
                    blockers.append(adapter.adapter_id)
            except BaseException as error:
                blockers.append(adapter.adapter_id)
                probes.append(
                    {
                        "adapter_id": adapter.adapter_id,
                        "owner": adapter.owner,
                        "ready": False,
                        "error": self._error_payload(
                            error,
                            transaction_id=(
                                active.transaction_id if active is not None else ""
                            ),
                            adapter_id=adapter.adapter_id,
                        ),
                    }
                )
        if active is not None:
            blockers.append("active_migration")
        if not integrity.get("healthy"):
            blockers.append("migration_journal")
        return {
            "schema": "zyra.migration-readiness/v1",
            "ready": not blockers,
            "blockers": sorted(set(blockers)),
            "active_transaction": (
                active.to_dict() if active is not None else None
            ),
            "latest_committed": (
                latest.to_dict() if latest is not None else None
            ),
            "integrity": integrity,
            "adapters": probes,
            "source_store_fallback": False,
        }

    def _recover_transaction(
        self,
        transaction: MigrationTransactionRecord,
        *,
        lease: MigrationLease,
    ) -> MigrationRecoveryReceipt:
        self._emit(
            "migration.recovery.started",
            severity=Severity.WARNING,
            attributes={
                "transaction_id": transaction.transaction_id,
                "state": transaction.state.value,
            },
        )
        if transaction.state is MigrationTransactionState.COMMITTED:
            raise MigrationRuntimeError(
                "migration_recovery_committed_forbidden",
                "committed migration must not be rolled back during restart recovery",
                transaction_id=transaction.transaction_id,
            )
        receipt = self._rollback(
            transaction.transaction_id,
            lease=lease,
            original_state=transaction.state.value,
        )
        self._emit(
            "migration.recovery.completed",
            attributes=receipt.to_dict(),
        )
        return receipt

    def _rollback(
        self,
        transaction_id: str,
        *,
        lease: MigrationLease,
        original_state: str,
    ) -> MigrationRecoveryReceipt:
        transaction = self.journal.require_transaction(transaction_id)
        if transaction.state is not MigrationTransactionState.ROLLING_BACK:
            if transaction.state is not MigrationTransactionState.FAILED:
                transaction = self.journal.transition_transaction(
                    lease=lease,
                    transaction_id=transaction_id,
                    target=MigrationTransactionState.ROLLING_BACK,
                )
            else:
                transaction = self.journal.transition_transaction(
                    lease=lease,
                    transaction_id=transaction_id,
                    target=MigrationTransactionState.ROLLING_BACK,
                )
        steps = list(self.journal.steps(transaction_id))
        restored_adapters: list[str] = []
        restored_backups: list[str] = []
        for step in reversed(steps):
            if step.state in {
                MigrationStepState.PENDING,
                MigrationStepState.ROLLED_BACK,
            }:
                continue
            adapter = self._adapter_map.get(step.adapter_id)
            if adapter is None:
                raise MigrationRuntimeError(
                    "migration_recovery_adapter_missing",
                    "migration recovery cannot find the original owner adapter",
                    transaction_id=transaction_id,
                    adapter_id=step.adapter_id,
                )
            lease = self._renew_if_needed(lease)
            if step.state is not MigrationStepState.ROLLING_BACK:
                self.journal.transition_step(
                    lease=lease,
                    transaction_id=transaction_id,
                    adapter_id=step.adapter_id,
                    target=MigrationStepState.ROLLING_BACK,
                )
            backups = self.journal.backups(transaction_id, step.adapter_id)
            restore = dict(adapter.restore(backups))
            if restore.get("restored") is not True:
                raise MigrationRuntimeError(
                    "migration_rollback_adapter_rejected",
                    "owner adapter did not confirm rollback",
                    transaction_id=transaction_id,
                    adapter_id=step.adapter_id,
                    details=restore,
                )
            self.journal.transition_step(
                lease=lease,
                transaction_id=transaction_id,
                adapter_id=step.adapter_id,
                target=MigrationStepState.ROLLED_BACK,
            )
            for backup in backups:
                self.journal.mark_backup_restored(
                    lease=lease,
                    transaction_id=transaction_id,
                    adapter_id=step.adapter_id,
                    backup_id=backup.backup_id,
                )
                restored_backups.append(backup.backup_id)
            restored_adapters.append(step.adapter_id)
            self._emit(
                "migration.step.rolled_back",
                severity=Severity.WARNING,
                attributes={
                    "transaction_id": transaction_id,
                    "adapter_id": step.adapter_id,
                    "backup_count": len(backups),
                },
            )
        transaction = self.journal.transition_transaction(
            lease=lease,
            transaction_id=transaction_id,
            target=MigrationTransactionState.ROLLED_BACK,
        )
        return MigrationRecoveryReceipt(
            transaction_id=transaction_id,
            original_state=original_state,
            final_state=transaction.state.value,
            restored_adapters=tuple(restored_adapters),
            restored_backups=tuple(restored_backups),
            recovered_at_ns=self._clock_ns(),
            clean=True,
        )

    def _mark_failure(
        self,
        *,
        lease: MigrationLease,
        transaction_id: str,
        adapter_id: str,
        error: Mapping[str, Any],
    ) -> None:
        if adapter_id:
            try:
                step = self.journal.require_step(transaction_id, adapter_id)
                if self.journal.can_transition_step(
                    step.state,
                    MigrationStepState.FAILED,
                ):
                    self.journal.transition_step(
                        lease=lease,
                        transaction_id=transaction_id,
                        adapter_id=adapter_id,
                        target=MigrationStepState.FAILED,
                        error=error,
                    )
            except MigrationJournalError:
                pass
        transaction = self.journal.require_transaction(transaction_id)
        if self.journal.can_transition_transaction(
            transaction.state,
            MigrationTransactionState.FAILED,
        ):
            self.journal.transition_transaction(
                lease=lease,
                transaction_id=transaction_id,
                target=MigrationTransactionState.FAILED,
                error=error,
            )
        self._emit(
            "migration.transaction.failed",
            severity=Severity.ERROR,
            attributes={
                "transaction_id": transaction_id,
                "adapter_id": adapter_id,
                "error": dict(error),
            },
        )

    def _renew_if_needed(self, lease: MigrationLease) -> MigrationLease:
        remaining = lease.expires_at_ns - self._clock_ns()
        threshold = max(1_000_000_000, self.lease_seconds * 250_000_000)
        if remaining > threshold:
            self.journal.assert_lease(lease)
            return lease
        return self.journal.renew_lease(
            lease,
            ttl_seconds=self.lease_seconds,
        )

    def _validate_plan(self, plan: MigrationPlan) -> None:
        if plan.configuration_digest != self.configuration_digest:
            raise MigrationRuntimeError(
                "migration_plan_configuration_mismatch",
                "migration plan was built for another configuration",
                details={
                    "expected": self.configuration_digest,
                    "actual": plan.configuration_digest,
                },
            )
        if plan.target_version != self.target_version:
            raise MigrationRuntimeError(
                "migration_plan_target_mismatch",
                "migration plan target does not match the runtime",
                details={
                    "expected": self.target_version,
                    "actual": plan.target_version,
                },
            )
        rebuilt = self.plan()
        if rebuilt.digest != plan.digest:
            raise MigrationRuntimeError(
                "migration_plan_stale",
                "owner state changed after migration planning",
                retryable=True,
                details={
                    "planned": plan.digest,
                    "actual": rebuilt.digest,
                },
            )

    def _build_receipt(
        self,
        *,
        transaction: MigrationTransactionRecord,
        plan: MigrationPlan,
        steps: tuple[MigrationStepReceipt, ...],
    ) -> MigrationReceipt:
        committed_at_ns = transaction.committed_at_ns or self._clock_ns()
        body = {
            "transaction_id": transaction.transaction_id,
            "plan_digest": plan.digest,
            "configuration_digest": self.configuration_digest,
            "process_generation": self.process_generation,
            "steps": [step.to_dict() for step in steps],
            "committed_at_ns": committed_at_ns,
        }
        digest = _digest(body)
        return MigrationReceipt(
            receipt_id="migration-receipt-" + digest.removeprefix("sha256:")[:24],
            transaction=transaction,
            plan_digest=plan.digest,
            configuration_digest=self.configuration_digest,
            process_generation=self.process_generation,
            steps=steps,
            committed_at_ns=committed_at_ns,
            digest=digest,
        )

    def _crash(
        self,
        phase: str,
        transaction_id: str,
        adapter_id: str,
    ) -> None:
        if self.crash_hook is not None:
            self.crash_hook(phase, transaction_id, adapter_id)

    def _emit(
        self,
        event: str,
        *,
        severity: Severity = Severity.INFO,
        attributes: Mapping[str, Any] | None = None,
    ) -> None:
        if self.lifecycle is not None:
            self.lifecycle.emit(
                event,
                severity=severity,
                attributes=attributes,
            )

    @staticmethod
    def _error_payload(
        error: BaseException,
        *,
        transaction_id: str,
        adapter_id: str,
    ) -> dict[str, Any]:
        if isinstance(error, MigrationRuntimeError):
            return error.to_dict()
        if isinstance(error, MigrationAdapterError):
            return {
                "schema": "zyra.migration-runtime-error/v1",
                "code": error.code,
                "message": str(error),
                "transaction_id": transaction_id,
                "adapter_id": error.adapter_id,
                "retryable": False,
                "details": dict(error.details),
            }
        if isinstance(error, MigrationJournalError):
            return {
                "schema": "zyra.migration-runtime-error/v1",
                "code": error.code,
                "message": str(error),
                "transaction_id": error.transaction_id or transaction_id,
                "adapter_id": error.adapter_id or adapter_id,
                "retryable": False,
                "details": dict(error.details),
            }
        return {
            "schema": "zyra.migration-runtime-error/v1",
            "code": "migration_unhandled_error",
            "message": f"{type(error).__name__}: {str(error)[:1024]}",
            "transaction_id": transaction_id,
            "adapter_id": adapter_id,
            "retryable": False,
            "details": {},
        }


def _digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
