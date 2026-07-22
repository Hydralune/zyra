from __future__ import annotations

import contextlib
import copy
import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .contracts import (
    BranchDelta,
    CheckpointPhase,
    CheckpointReceipt,
    CheckpointWrite,
    LayeredRouteDecision,
    PendingWriteState,
    RecoveryActionReceipt,
    RecoveryCheckpoint,
    RecoveryOutcome,
    RecoveryPlan,
    RecoveryPlanStatus,
    RecoverySignal,
    RoutingMemoryRecord,
    SideEffectFence,
    SideEffectState,
    canonical_json,
    recovery_id,
    stable_digest,
    utc_now,
)
from .safe_codec import SafeCheckpointCodec


class RecoveryStoreError(RuntimeError):
    pass


class RecoveryStoreConflict(RecoveryStoreError):
    pass


class RecoveryLeaseError(RecoveryStoreError):
    pass


class RecoveryReplayConflict(RecoveryStoreError):
    pass


class _ClosingConnection(sqlite3.Connection):
    """Make ``with connection`` own the connection lifetime as well as commit."""

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


class RecoveryPlanStore:
    """Canonical durable owner for 07C recovery planning state.

    The database contains recovery-owned facts only. Worker leases, backend
    routes, permission requests, sessions, graph snapshots, MemoryFabric and
    runtime events remain foreign owner references. Every multi-row mutation is
    committed under `BEGIN IMMEDIATE`; compare-and-swap revisions prevent two
    recovery executors from applying the same plan or checkpoint branch.
    """

    def __init__(self, path: str | Path, *, checkpoint_codec: SafeCheckpointCodec | None = None) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.codec = checkpoint_codec or SafeCheckpointCodec()
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    PRAGMA journal_mode=WAL;
                    PRAGMA foreign_keys=ON;
                    PRAGMA busy_timeout=5000;

                    CREATE TABLE IF NOT EXISTS recovery_signals (
                        signal_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        source TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        observed_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(run_id, task_id, fingerprint)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_signals_task
                        ON recovery_signals(task_id, observed_at, signal_id);

                    CREATE TABLE IF NOT EXISTS recovery_plans (
                        plan_id TEXT PRIMARY KEY,
                        signal_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        idempotency_key TEXT NOT NULL,
                        selected_action TEXT NOT NULL,
                        claimed_by TEXT NOT NULL DEFAULT '',
                        claim_expires_at TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(run_id, task_id, idempotency_key),
                        FOREIGN KEY(signal_id) REFERENCES recovery_signals(signal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_plans_task
                        ON recovery_plans(task_id, updated_at, plan_id);
                    CREATE INDEX IF NOT EXISTS idx_recovery_plans_status
                        ON recovery_plans(status, claim_expires_at, updated_at);

                    CREATE TABLE IF NOT EXISTS recovery_action_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        status TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        changed_execution INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        UNIQUE(plan_id, request_digest),
                        FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_receipts_plan
                        ON recovery_action_receipts(plan_id, created_at, receipt_id);

                    CREATE TABLE IF NOT EXISTS recovery_outcomes (
                        outcome_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        signal_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        success INTEGER NOT NULL,
                        kind TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id),
                        FOREIGN KEY(signal_id) REFERENCES recovery_signals(signal_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_outcomes_task
                        ON recovery_outcomes(task_id, created_at, outcome_id);

                    CREATE TABLE IF NOT EXISTS recovery_checkpoints (
                        checkpoint_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        parent_checkpoint_id TEXT NOT NULL DEFAULT '',
                        phase TEXT NOT NULL,
                        commit_revision INTEGER NOT NULL,
                        signature TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        committed_at TEXT NOT NULL DEFAULT '',
                        payload_json BLOB NOT NULL,
                        UNIQUE(task_id, commit_revision)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_task
                        ON recovery_checkpoints(task_id, commit_revision DESC);

                    CREATE TABLE IF NOT EXISTS recovery_checkpoint_heads (
                        task_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        commit_revision INTEGER NOT NULL,
                        signature TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        FOREIGN KEY(checkpoint_id) REFERENCES recovery_checkpoints(checkpoint_id)
                    );

                    CREATE TABLE IF NOT EXISTS recovery_checkpoint_writes (
                        write_id TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        task_key TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        state TEXT NOT NULL,
                        sequence INTEGER NOT NULL,
                        value_digest TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        PRIMARY KEY(checkpoint_id, write_id),
                        UNIQUE(checkpoint_id, task_key, channel, sequence),
                        FOREIGN KEY(checkpoint_id) REFERENCES recovery_checkpoints(checkpoint_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_writes_task
                        ON recovery_checkpoint_writes(task_id, state, task_key, channel, sequence);

                    CREATE TABLE IF NOT EXISTS recovery_checkpoint_receipts (
                        receipt_id TEXT PRIMARY KEY,
                        checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        phase TEXT NOT NULL,
                        commit_revision INTEGER NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(checkpoint_id) REFERENCES recovery_checkpoints(checkpoint_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_checkpoint_receipts_checkpoint
                        ON recovery_checkpoint_receipts(checkpoint_id, created_at, receipt_id);

                    CREATE TABLE IF NOT EXISTS recovery_side_effect_fences (
                        fence_key TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        state TEXT NOT NULL,
                        request_digest TEXT NOT NULL,
                        response_digest TEXT NOT NULL DEFAULT '',
                        receipt_ref TEXT NOT NULL DEFAULT '',
                        revision INTEGER NOT NULL,
                        updated_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_fences_task
                        ON recovery_side_effect_fences(task_id, operation, state);

                    CREATE TABLE IF NOT EXISTS recovery_processed_responses (
                        task_id TEXT NOT NULL,
                        response_id TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        response_digest TEXT NOT NULL,
                        processed_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        PRIMARY KEY(task_id, response_id)
                    );

                    CREATE TABLE IF NOT EXISTS recovery_route_decisions (
                        route_decision_id TEXT PRIMARY KEY,
                        plan_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        action TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(plan_id) REFERENCES recovery_plans(plan_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_routes_task
                        ON recovery_route_decisions(task_id, created_at, route_decision_id);

                    CREATE TABLE IF NOT EXISTS recovery_feedback (
                        record_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        signal_kind TEXT NOT NULL,
                        action TEXT NOT NULL,
                        success INTEGER NOT NULL,
                        worker_id TEXT NOT NULL DEFAULT '',
                        backend_id TEXT NOT NULL DEFAULT '',
                        provider_id TEXT NOT NULL DEFAULT '',
                        model_id TEXT NOT NULL DEFAULT '',
                        score_delta REAL NOT NULL,
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_feedback_task
                        ON recovery_feedback(task_id, created_at, record_id);
                    CREATE INDEX IF NOT EXISTS idx_recovery_feedback_route
                        ON recovery_feedback(worker_id, backend_id, provider_id, model_id, created_at);

                    CREATE TABLE IF NOT EXISTS recovery_branch_deltas (
                        branch_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        checkpoint_id TEXT NOT NULL,
                        base_revision INTEGER NOT NULL,
                        owner TEXT NOT NULL,
                        status TEXT NOT NULL,
                        digest TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        committed_revision INTEGER,
                        payload_json TEXT NOT NULL,
                        FOREIGN KEY(checkpoint_id) REFERENCES recovery_checkpoints(checkpoint_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_deltas_task
                        ON recovery_branch_deltas(task_id, base_revision, status, branch_id);

                    CREATE TABLE IF NOT EXISTS recovery_delta_writes (
                        task_id TEXT NOT NULL,
                        commit_revision INTEGER NOT NULL,
                        branch_id TEXT NOT NULL,
                        key TEXT NOT NULL,
                        value_digest TEXT NOT NULL,
                        committed_at TEXT NOT NULL,
                        PRIMARY KEY(task_id, commit_revision, branch_id, key),
                        FOREIGN KEY(branch_id) REFERENCES recovery_branch_deltas(branch_id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_delta_writes_key
                        ON recovery_delta_writes(task_id, key, commit_revision);

                    CREATE TABLE IF NOT EXISTS recovery_journal (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        entity_kind TEXT NOT NULL,
                        entity_id TEXT NOT NULL,
                        operation TEXT NOT NULL,
                        revision INTEGER NOT NULL,
                        causation_id TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_recovery_journal_task
                        ON recovery_journal(task_id, sequence);
                    """
                )
            self._initialized = True

    def put_signal(self, signal: RecoverySignal) -> tuple[RecoverySignal, bool]:
        self.initialize()
        payload = signal.to_dict()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_signals WHERE run_id = ? AND task_id = ? AND fingerprint = ?",
                (signal.refs.run_id, signal.refs.task_id, signal.fingerprint),
            ).fetchone()
            if existing is not None:
                return RecoverySignal.from_dict(self._loads(existing["payload_json"])), False
            connection.execute(
                """
                INSERT INTO recovery_signals
                    (signal_id, run_id, task_id, fingerprint, source, kind, observed_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id, signal.refs.run_id, signal.refs.task_id, signal.fingerprint,
                    signal.source.value, signal.kind.value, signal.observed_at, canonical_json(payload),
                ),
            )
            self._journal(connection, signal.refs.run_id, signal.refs.task_id, "signal", signal.signal_id, "admitted", 1, signal.causation_id, payload)
        return signal, True

    def signal(self, signal_id: str) -> RecoverySignal | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_signals WHERE signal_id = ?",
                (signal_id,),
            ).fetchone()
        return RecoverySignal.from_dict(self._loads(row["payload_json"])) if row else None

    def signals(self, *, task_id: str = "", limit: int = 500) -> tuple[RecoverySignal, ...]:
        self.initialize()
        with self._connect() as connection:
            if task_id:
                rows = connection.execute(
                    "SELECT payload_json FROM recovery_signals WHERE task_id = ? ORDER BY observed_at, signal_id LIMIT ?",
                    (task_id, self._bounded_limit(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT payload_json FROM recovery_signals ORDER BY observed_at, signal_id LIMIT ?",
                    (self._bounded_limit(limit),),
                ).fetchall()
        return tuple(RecoverySignal.from_dict(self._loads(row["payload_json"])) for row in rows)

    def put_plan(self, plan: RecoveryPlan) -> tuple[RecoveryPlan, bool]:
        self.initialize()
        if self.signal(plan.signal.signal_id) is None:
            self.put_signal(plan.signal)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_plans WHERE run_id = ? AND task_id = ? AND idempotency_key = ?",
                (plan.signal.refs.run_id, plan.signal.refs.task_id, plan.idempotency_key),
            ).fetchone()
            if existing is not None:
                return RecoveryPlan.from_dict(self._loads(existing["payload_json"])), False
            payload = plan.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_plans
                    (plan_id, signal_id, run_id, task_id, status, revision, idempotency_key,
                     selected_action, claimed_by, claim_expires_at, created_at, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id, plan.signal.signal_id, plan.signal.refs.run_id, plan.signal.refs.task_id,
                    plan.status.value, plan.revision, plan.idempotency_key, plan.decision.selected.action.value,
                    plan.claimed_by, plan.claim_expires_at, plan.created_at, plan.updated_at,
                    canonical_json(payload),
                ),
            )
            self._journal(connection, plan.signal.refs.run_id, plan.signal.refs.task_id, "plan", plan.plan_id, "created", plan.revision, plan.signal.signal_id, payload)
        return plan, True

    def plan(self, plan_id: str) -> RecoveryPlan | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return RecoveryPlan.from_dict(self._loads(row["payload_json"])) if row else None

    def plan_for_idempotency(self, *, run_id: str, task_id: str, idempotency_key: str) -> RecoveryPlan | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_plans WHERE run_id = ? AND task_id = ? AND idempotency_key = ?",
                (run_id, task_id, idempotency_key),
            ).fetchone()
        return RecoveryPlan.from_dict(self._loads(row["payload_json"])) if row else None

    def plans(
        self,
        *,
        task_id: str = "",
        statuses: Sequence[RecoveryPlanStatus] = (),
        limit: int = 500,
    ) -> tuple[RecoveryPlan, ...]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            parameters.extend(item.value for item in statuses)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(self._bounded_limit(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM recovery_plans {where} ORDER BY created_at, plan_id LIMIT ?",  # noqa: S608 - placeholders built above.
                parameters,
            ).fetchall()
        return tuple(RecoveryPlan.from_dict(self._loads(row["payload_json"])) for row in rows)

    def update_plan(
        self,
        plan: RecoveryPlan,
        *,
        expected_revision: int,
        operation: str,
        causation_id: str = "",
    ) -> RecoveryPlan:
        self.initialize()
        if plan.revision != expected_revision + 1:
            raise RecoveryStoreConflict("updated plan revision must advance exactly once")
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT revision, payload_json FROM recovery_plans WHERE plan_id = ?",
                (plan.plan_id,),
            ).fetchone()
            if current is None:
                raise RecoveryStoreConflict(f"recovery plan does not exist: {plan.plan_id}")
            if int(current["revision"]) != expected_revision:
                raise RecoveryStoreConflict(
                    f"recovery plan revision conflict: expected {expected_revision}, current {current['revision']}"
                )
            previous = RecoveryPlan.from_dict(self._loads(current["payload_json"]))
            self._validate_plan_transition(previous, plan)
            payload = plan.to_dict()
            cursor = connection.execute(
                """
                UPDATE recovery_plans
                SET status = ?, revision = ?, claimed_by = ?, claim_expires_at = ?,
                    updated_at = ?, payload_json = ?
                WHERE plan_id = ? AND revision = ?
                """,
                (
                    plan.status.value, plan.revision, plan.claimed_by, plan.claim_expires_at,
                    plan.updated_at, canonical_json(payload), plan.plan_id, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RecoveryStoreConflict("recovery plan compare-and-swap failed")
            self._journal(connection, plan.signal.refs.run_id, plan.signal.refs.task_id, "plan", plan.plan_id, operation, plan.revision, causation_id, payload)
        return plan

    def claim_plan(
        self,
        plan_id: str,
        *,
        owner: str,
        lease_seconds: float = 60.0,
        now: datetime | None = None,
    ) -> RecoveryPlan:
        moment = (now or datetime.now(UTC)).astimezone(UTC)
        expires = moment + timedelta(seconds=max(1.0, float(lease_seconds)))
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise RecoveryLeaseError(f"recovery plan not found: {plan_id}")
            current = RecoveryPlan.from_dict(self._loads(row["payload_json"]))
            if current.status.terminal:
                raise RecoveryLeaseError(f"recovery plan is terminal: {current.status.value}")
            if current.claimed_by and current.claimed_by != owner and self._future(current.claim_expires_at, moment):
                raise RecoveryLeaseError(f"recovery plan is claimed by {current.claimed_by}")
            claimed = current.evolve(
                status=RecoveryPlanStatus.CLAIMED,
                claimed_by=owner,
                claim_expires_at=expires.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
            payload = claimed.to_dict()
            cursor = connection.execute(
                """
                UPDATE recovery_plans
                SET status = ?, revision = ?, claimed_by = ?, claim_expires_at = ?, updated_at = ?, payload_json = ?
                WHERE plan_id = ? AND revision = ?
                """,
                (
                    claimed.status.value, claimed.revision, claimed.claimed_by, claimed.claim_expires_at,
                    claimed.updated_at, canonical_json(payload), claimed.plan_id, current.revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RecoveryLeaseError("recovery plan claim lost compare-and-swap")
            self._journal(connection, claimed.signal.refs.run_id, claimed.signal.refs.task_id, "plan", claimed.plan_id, "claimed", claimed.revision, owner, payload)
        return claimed

    def release_plan(self, plan_id: str, *, owner: str, reason: str) -> RecoveryPlan:
        current = self.plan(plan_id)
        if current is None:
            raise RecoveryLeaseError(f"recovery plan not found: {plan_id}")
        if current.claimed_by != owner:
            raise RecoveryLeaseError(f"recovery plan claim is not owned by {owner}")
        released = current.evolve(
            status=RecoveryPlanStatus.PLANNED,
            claimed_by="",
            claim_expires_at="",
            provenance={**dict(current.provenance), "last_release_reason": reason},
        )
        return self.update_plan(released, expected_revision=current.revision, operation="released", causation_id=owner)

    def append_action_receipt(self, receipt: RecoveryActionReceipt) -> tuple[RecoveryActionReceipt, bool]:
        self.initialize()
        plan = self.plan(receipt.plan_id)
        if plan is None:
            raise RecoveryStoreConflict(f"receipt plan does not exist: {receipt.plan_id}")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_action_receipts WHERE plan_id = ? AND request_digest = ?",
                (receipt.plan_id, receipt.request_digest),
            ).fetchone()
            if existing is not None:
                saved = RecoveryActionReceipt.from_dict(self._loads(existing["payload_json"]))
                if saved.action is not receipt.action:
                    raise RecoveryReplayConflict("request digest was reused for another recovery action")
                return saved, False
            payload = receipt.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_action_receipts
                    (receipt_id, plan_id, action, status, request_digest, changed_execution, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id, receipt.plan_id, receipt.action.value, receipt.status.value,
                    receipt.request_digest, int(receipt.changed_execution), receipt.created_at,
                    canonical_json(payload),
                ),
            )
            self._journal(connection, plan.signal.refs.run_id, plan.signal.refs.task_id, "action_receipt", receipt.receipt_id, receipt.status.value, plan.revision, plan.plan_id, payload)
        return receipt, True

    def action_receipt(self, *, plan_id: str, request_digest: str) -> RecoveryActionReceipt | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_action_receipts WHERE plan_id = ? AND request_digest = ?",
                (plan_id, request_digest),
            ).fetchone()
        return RecoveryActionReceipt.from_dict(self._loads(row["payload_json"])) if row else None

    def action_receipts(self, *, plan_id: str) -> tuple[RecoveryActionReceipt, ...]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM recovery_action_receipts WHERE plan_id = ? ORDER BY created_at, receipt_id",
                (plan_id,),
            ).fetchall()
        return tuple(RecoveryActionReceipt.from_dict(self._loads(row["payload_json"])) for row in rows)

    def append_outcome(self, outcome: RecoveryOutcome) -> tuple[RecoveryOutcome, bool]:
        self.initialize()
        plan = self.plan(outcome.plan_id)
        if plan is None or plan.signal.signal_id != outcome.signal_id:
            raise RecoveryStoreConflict("outcome does not match a durable recovery plan")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_outcomes WHERE outcome_id = ?",
                (outcome.outcome_id,),
            ).fetchone()
            if existing:
                return RecoveryOutcome.from_dict(self._loads(existing["payload_json"])), False
            payload = outcome.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_outcomes
                    (outcome_id, plan_id, signal_id, task_id, success, kind, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome.outcome_id, outcome.plan_id, outcome.signal_id, plan.signal.refs.task_id,
                    int(outcome.success), outcome.kind.value, outcome.created_at, canonical_json(payload),
                ),
            )
            self._journal(connection, plan.signal.refs.run_id, plan.signal.refs.task_id, "outcome", outcome.outcome_id, outcome.kind.value, plan.revision, plan.plan_id, payload)
        return outcome, True

    def outcomes(self, *, task_id: str = "", plan_id: str = "", limit: int = 500) -> tuple[RecoveryOutcome, ...]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if plan_id:
            clauses.append("plan_id = ?")
            parameters.append(plan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(self._bounded_limit(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM recovery_outcomes {where} ORDER BY created_at, outcome_id LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        return tuple(RecoveryOutcome.from_dict(self._loads(row["payload_json"])) for row in rows)

    def commit_checkpoint(
        self,
        checkpoint: RecoveryCheckpoint,
        *,
        expected_revision: int,
        receipt: CheckpointReceipt,
    ) -> tuple[RecoveryCheckpoint, CheckpointReceipt, bool]:
        self.initialize()
        if checkpoint.phase is not CheckpointPhase.COMMITTED:
            raise RecoveryStoreConflict("canonical checkpoint must be in committed phase")
        if checkpoint.commit_revision != expected_revision + 1:
            raise RecoveryStoreConflict("checkpoint commit revision must advance exactly once")
        if receipt.checkpoint_id != checkpoint.checkpoint_id or receipt.commit_revision != checkpoint.commit_revision:
            raise RecoveryStoreConflict("checkpoint receipt does not match checkpoint")
        payload = self.codec.encode(checkpoint)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_checkpoints WHERE checkpoint_id = ?",
                (checkpoint.checkpoint_id,),
            ).fetchone()
            if existing is not None:
                saved = self.codec.decode(existing["payload_json"])
                saved_receipt = self._checkpoint_receipt_connection(connection, checkpoint.checkpoint_id)
                if saved.content_digest != checkpoint.content_digest:
                    raise RecoveryReplayConflict("checkpoint id was reused with different content")
                if saved_receipt is None:
                    raise RecoveryStoreConflict("checkpoint exists without receipt")
                return saved, saved_receipt, False
            head = connection.execute(
                "SELECT checkpoint_id, commit_revision FROM recovery_checkpoint_heads WHERE task_id = ?",
                (checkpoint.task_id,),
            ).fetchone()
            current_revision = int(head["commit_revision"]) if head else 0
            if current_revision != expected_revision:
                raise RecoveryStoreConflict(
                    f"checkpoint head conflict: expected {expected_revision}, current {current_revision}"
                )
            if head and checkpoint.parent_checkpoint_id != str(head["checkpoint_id"]):
                raise RecoveryStoreConflict("checkpoint parent does not match current task head")
            if not head and checkpoint.parent_checkpoint_id:
                raise RecoveryStoreConflict("initial checkpoint cannot have an unknown parent")
            connection.execute(
                """
                INSERT INTO recovery_checkpoints
                    (checkpoint_id, run_id, task_id, session_id, parent_checkpoint_id, phase,
                     commit_revision, signature, content_digest, created_at, committed_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id, checkpoint.run_id, checkpoint.task_id, checkpoint.session_id,
                    checkpoint.parent_checkpoint_id, checkpoint.phase.value, checkpoint.commit_revision,
                    checkpoint.signature, checkpoint.content_digest, checkpoint.created_at,
                    checkpoint.committed_at, payload,
                ),
            )
            for write in (*checkpoint.committed_writes, *checkpoint.pending_writes):
                connection.execute(
                    """
                    INSERT INTO recovery_checkpoint_writes
                        (write_id, checkpoint_id, task_id, task_key, channel, state, sequence, value_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        write.write_id, checkpoint.checkpoint_id, checkpoint.task_id, write.task_key,
                        write.channel, write.state.value, write.sequence, write.value_digest,
                        canonical_json(write.to_dict()),
                    ),
                )
            connection.execute(
                """
                INSERT INTO recovery_checkpoint_heads
                    (task_id, run_id, checkpoint_id, commit_revision, signature, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    run_id = excluded.run_id,
                    checkpoint_id = excluded.checkpoint_id,
                    commit_revision = excluded.commit_revision,
                    signature = excluded.signature,
                    updated_at = excluded.updated_at
                """,
                (
                    checkpoint.task_id, checkpoint.run_id, checkpoint.checkpoint_id,
                    checkpoint.commit_revision, checkpoint.signature, checkpoint.committed_at or utc_now(),
                ),
            )
            self._insert_checkpoint_receipt(connection, receipt)
            self._journal(connection, checkpoint.run_id, checkpoint.task_id, "checkpoint", checkpoint.checkpoint_id, "committed", checkpoint.commit_revision, checkpoint.parent_checkpoint_id, checkpoint.to_dict())
        return checkpoint, receipt, True

    def checkpoint(self, checkpoint_id: str) -> RecoveryCheckpoint | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()
        return self.codec.decode(row["payload_json"]) if row else None

    def checkpoint_head(self, task_id: str) -> RecoveryCheckpoint | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT c.payload_json
                FROM recovery_checkpoint_heads h
                JOIN recovery_checkpoints c ON c.checkpoint_id = h.checkpoint_id
                WHERE h.task_id = ?
                """,
                (task_id,),
            ).fetchone()
        return self.codec.decode(row["payload_json"]) if row else None

    def checkpoints(self, *, task_id: str, limit: int = 500) -> tuple[RecoveryCheckpoint, ...]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM recovery_checkpoints WHERE task_id = ? ORDER BY commit_revision LIMIT ?",
                (task_id, self._bounded_limit(limit)),
            ).fetchall()
        return tuple(self.codec.decode(row["payload_json"]) for row in rows)

    def append_checkpoint_receipt(self, receipt: CheckpointReceipt) -> tuple[CheckpointReceipt, bool]:
        self.initialize()
        if self.checkpoint(receipt.checkpoint_id) is None:
            raise RecoveryStoreConflict("checkpoint receipt references unknown checkpoint")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_checkpoint_receipts WHERE receipt_id = ?",
                (receipt.receipt_id,),
            ).fetchone()
            if existing:
                saved = CheckpointReceipt.from_dict(self._loads(existing["payload_json"]))
                if saved.to_dict() != receipt.to_dict():
                    raise RecoveryReplayConflict("checkpoint receipt id was reused")
                return saved, False
            self._insert_checkpoint_receipt(connection, receipt)
            checkpoint = self.checkpoint(receipt.checkpoint_id)
            assert checkpoint is not None
            self._journal(connection, checkpoint.run_id, checkpoint.task_id, "checkpoint_receipt", receipt.receipt_id, receipt.phase.value, receipt.commit_revision, receipt.checkpoint_id, receipt.to_dict())
        return receipt, True

    def checkpoint_receipts(self, checkpoint_id: str) -> tuple[CheckpointReceipt, ...]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM recovery_checkpoint_receipts WHERE checkpoint_id = ? ORDER BY created_at, receipt_id",
                (checkpoint_id,),
            ).fetchall()
        return tuple(CheckpointReceipt.from_dict(self._loads(row["payload_json"])) for row in rows)

    def reserve_side_effect(self, fence: SideEffectFence) -> tuple[SideEffectFence, bool]:
        self.initialize()
        if fence.state is not SideEffectState.RESERVED:
            raise RecoveryStoreConflict("new side-effect fence must begin reserved")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_side_effect_fences WHERE fence_key = ?",
                (fence.fence_key,),
            ).fetchone()
            if existing:
                saved = SideEffectFence.from_dict(self._loads(existing["payload_json"]))
                if saved.request_digest != fence.request_digest or saved.operation != fence.operation:
                    raise RecoveryReplayConflict("side-effect fence key was reused for another request")
                return saved, False
            payload = fence.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_side_effect_fences
                    (fence_key, run_id, task_id, operation, state, request_digest, response_digest,
                     receipt_ref, revision, updated_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fence.fence_key, fence.run_id, fence.task_id, fence.operation, fence.state.value,
                    fence.request_digest, fence.response_digest, fence.receipt_ref, fence.revision,
                    fence.reserved_at, canonical_json(payload),
                ),
            )
            self._journal(connection, fence.run_id, fence.task_id, "side_effect_fence", fence.fence_key, "reserved", fence.revision, fence.operation, payload)
        return fence, True

    def side_effect_fence(self, fence_key: str) -> SideEffectFence | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_side_effect_fences WHERE fence_key = ?",
                (fence_key,),
            ).fetchone()
        return SideEffectFence.from_dict(self._loads(row["payload_json"])) if row else None

    def transition_side_effect(
        self,
        fence_key: str,
        *,
        expected_revision: int,
        state: SideEffectState,
        response_digest: str = "",
        receipt_ref: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> SideEffectFence:
        self.initialize()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_side_effect_fences WHERE fence_key = ?",
                (fence_key,),
            ).fetchone()
            if row is None:
                raise RecoveryStoreConflict(f"side-effect fence not found: {fence_key}")
            current = SideEffectFence.from_dict(self._loads(row["payload_json"]))
            if current.revision != expected_revision:
                raise RecoveryStoreConflict("side-effect fence revision conflict")
            self._validate_fence_transition(current.state, state)
            updated = SideEffectFence(
                fence_key=current.fence_key,
                run_id=current.run_id,
                task_id=current.task_id,
                operation=current.operation,
                state=state,
                request_digest=current.request_digest,
                response_digest=response_digest or current.response_digest,
                receipt_ref=receipt_ref or current.receipt_ref,
                revision=current.revision + 1,
                reserved_at=current.reserved_at,
                committed_at=utc_now() if state is SideEffectState.COMMITTED else current.committed_at,
                metadata={**dict(current.metadata), **dict(metadata or {})},
            )
            payload = updated.to_dict()
            cursor = connection.execute(
                """
                UPDATE recovery_side_effect_fences
                SET state = ?, response_digest = ?, receipt_ref = ?, revision = ?, updated_at = ?, payload_json = ?
                WHERE fence_key = ? AND revision = ?
                """,
                (
                    updated.state.value, updated.response_digest, updated.receipt_ref, updated.revision,
                    updated.committed_at or utc_now(), canonical_json(payload), fence_key, expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise RecoveryStoreConflict("side-effect fence compare-and-swap failed")
            self._journal(connection, updated.run_id, updated.task_id, "side_effect_fence", updated.fence_key, state.value, updated.revision, updated.receipt_ref, payload)
        return updated

    def mark_response_processed(
        self,
        *,
        task_id: str,
        response_id: str,
        checkpoint_id: str,
        response_digest: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        self.initialize()
        checkpoint = self.checkpoint(checkpoint_id)
        if checkpoint is None or checkpoint.task_id != task_id:
            raise RecoveryStoreConflict("processed response checkpoint scope mismatch")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT response_digest, checkpoint_id FROM recovery_processed_responses WHERE task_id = ? AND response_id = ?",
                (task_id, response_id),
            ).fetchone()
            if existing:
                if str(existing["response_digest"]) != response_digest:
                    raise RecoveryReplayConflict("response id was reused with different content")
                return False
            connection.execute(
                """
                INSERT INTO recovery_processed_responses
                    (task_id, response_id, checkpoint_id, response_digest, processed_at, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (task_id, response_id, checkpoint_id, response_digest, utc_now(), canonical_json(dict(metadata or {}))),
            )
            self._journal(connection, checkpoint.run_id, task_id, "processed_response", response_id, "committed", checkpoint.commit_revision, checkpoint_id, dict(metadata or {}))
        return True

    def response_processed(self, *, task_id: str, response_id: str) -> bool:
        self.initialize()
        with self._connect() as connection:
            return connection.execute(
                "SELECT 1 FROM recovery_processed_responses WHERE task_id = ? AND response_id = ?",
                (task_id, response_id),
            ).fetchone() is not None

    def append_route_decision(self, decision: LayeredRouteDecision) -> tuple[LayeredRouteDecision, bool]:
        self.initialize()
        plan = self.plan(decision.plan_id)
        if plan is None or plan.signal.refs.task_id != decision.task_id:
            raise RecoveryStoreConflict("route decision does not match recovery plan")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_route_decisions WHERE route_decision_id = ?",
                (decision.route_decision_id,),
            ).fetchone()
            if existing:
                saved = LayeredRouteDecision.from_dict(self._loads(existing["payload_json"]))
                if saved.to_dict() != decision.to_dict():
                    raise RecoveryReplayConflict("route decision id was reused")
                return saved, False
            payload = decision.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_route_decisions
                    (route_decision_id, plan_id, task_id, action, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.route_decision_id, decision.plan_id, decision.task_id,
                    decision.action.value, decision.created_at, canonical_json(payload),
                ),
            )
            self._journal(connection, decision.run_id, decision.task_id, "route_decision", decision.route_decision_id, "applied" if any(item.applied for item in decision.changes) else "recorded", plan.revision, decision.plan_id, payload)
        return decision, True

    def route_decisions(self, *, task_id: str = "", plan_id: str = "", limit: int = 500) -> tuple[LayeredRouteDecision, ...]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        if task_id:
            clauses.append("task_id = ?")
            parameters.append(task_id)
        if plan_id:
            clauses.append("plan_id = ?")
            parameters.append(plan_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(self._bounded_limit(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM recovery_route_decisions {where} ORDER BY created_at, route_decision_id LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        return tuple(LayeredRouteDecision.from_dict(self._loads(row["payload_json"])) for row in rows)

    def append_feedback(self, record: RoutingMemoryRecord) -> tuple[RoutingMemoryRecord, bool]:
        self.initialize()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json FROM recovery_feedback WHERE record_id = ?",
                (record.record_id,),
            ).fetchone()
            if existing:
                saved = RoutingMemoryRecord.from_dict(self._loads(existing["payload_json"]))
                if saved.to_dict() != record.to_dict():
                    raise RecoveryReplayConflict("routing memory record id was reused")
                return saved, False
            payload = record.to_dict()
            connection.execute(
                """
                INSERT INTO recovery_feedback
                    (record_id, run_id, task_id, signal_kind, action, success, worker_id,
                     backend_id, provider_id, model_id, score_delta, created_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.record_id, record.run_id, record.task_id, record.signal_kind.value,
                    record.action.value, int(record.success), record.worker_id, record.backend_id,
                    record.provider_id, record.model_id, record.score_delta, record.created_at,
                    canonical_json(payload),
                ),
            )
            self._journal(connection, record.run_id, record.task_id, "routing_memory", record.record_id, "feedback", 1, record.evidence_refs[0] if record.evidence_refs else "", payload)
        return record, True

    def feedback(
        self,
        *,
        task_id: str = "",
        worker_id: str = "",
        backend_id: str = "",
        provider_id: str = "",
        model_id: str = "",
        limit: int = 1000,
    ) -> tuple[RoutingMemoryRecord, ...]:
        self.initialize()
        clauses: list[str] = []
        parameters: list[Any] = []
        for name, value in (
            ("task_id", task_id), ("worker_id", worker_id), ("backend_id", backend_id),
            ("provider_id", provider_id), ("model_id", model_id),
        ):
            if value:
                clauses.append(f"{name} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(self._bounded_limit(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM recovery_feedback {where} ORDER BY created_at, record_id LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        return tuple(RoutingMemoryRecord.from_dict(self._loads(row["payload_json"])) for row in rows)

    def put_branch_delta(self, delta: BranchDelta) -> tuple[BranchDelta, bool]:
        self.initialize()
        checkpoint = self.checkpoint(delta.checkpoint_id)
        if checkpoint is None or checkpoint.task_id != delta.task_id:
            raise RecoveryStoreConflict("branch delta checkpoint scope mismatch")
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT payload_json, digest FROM recovery_branch_deltas WHERE branch_id = ?",
                (delta.branch_id,),
            ).fetchone()
            if existing:
                if str(existing["digest"]) != delta.digest:
                    raise RecoveryReplayConflict("branch id was reused with a different delta")
                return BranchDelta.from_dict(self._loads(existing["payload_json"])), False
            connection.execute(
                """
                INSERT INTO recovery_branch_deltas
                    (branch_id, run_id, task_id, checkpoint_id, base_revision, owner,
                     status, digest, created_at, committed_revision, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, NULL, ?)
                """,
                (
                    delta.branch_id, delta.run_id, delta.task_id, delta.checkpoint_id,
                    delta.base_revision, delta.owner, delta.digest, delta.created_at,
                    canonical_json(delta.to_dict()),
                ),
            )
            self._journal(connection, delta.run_id, delta.task_id, "branch_delta", delta.branch_id, "pending", delta.base_revision, delta.checkpoint_id, delta.to_dict())
        return delta, True

    def branch_delta(self, branch_id: str) -> BranchDelta | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM recovery_branch_deltas WHERE branch_id = ?",
                (branch_id,),
            ).fetchone()
        return BranchDelta.from_dict(self._loads(row["payload_json"])) if row else None

    def branch_deltas(
        self,
        *,
        task_id: str,
        after_revision: int = -1,
        status: str = "",
    ) -> tuple[BranchDelta, ...]:
        self.initialize()
        clauses = ["task_id = ?", "base_revision > ?"]
        parameters: list[Any] = [task_id, after_revision]
        if status:
            clauses.append("status = ?")
            parameters.append(status)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT payload_json FROM recovery_branch_deltas WHERE {' AND '.join(clauses)} ORDER BY base_revision, branch_id",  # noqa: S608
                parameters,
            ).fetchall()
        return tuple(BranchDelta.from_dict(self._loads(row["payload_json"])) for row in rows)

    def committed_delta_writes(self, *, task_id: str, after_revision: int) -> tuple[dict[str, Any], ...]:
        self.initialize()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT task_id, commit_revision, branch_id, key, value_digest, committed_at
                FROM recovery_delta_writes
                WHERE task_id = ? AND commit_revision > ?
                ORDER BY commit_revision, branch_id, key
                """,
                (task_id, after_revision),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def commit_branch_delta(
        self,
        delta: BranchDelta,
        *,
        expected_head_revision: int,
        new_checkpoint: RecoveryCheckpoint,
        checkpoint_receipt: CheckpointReceipt,
        resulting_values: Mapping[str, Any],
    ) -> tuple[RecoveryCheckpoint, CheckpointReceipt]:
        """Atomically publish a branch delta and its successor checkpoint."""

        self.initialize()
        if new_checkpoint.commit_revision != expected_head_revision + 1:
            raise RecoveryStoreConflict("branch commit must advance checkpoint head once")
        if delta.base_revision > expected_head_revision:
            raise RecoveryStoreConflict("branch base revision is ahead of checkpoint head")
        payload = self.codec.encode(new_checkpoint)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, digest FROM recovery_branch_deltas WHERE branch_id = ?",
                (delta.branch_id,),
            ).fetchone()
            if row is None:
                raise RecoveryStoreConflict("branch delta must be persisted before commit")
            if str(row["digest"]) != delta.digest:
                raise RecoveryReplayConflict("persisted branch delta digest changed")
            if str(row["status"]) == "committed":
                existing = self.checkpoint(new_checkpoint.checkpoint_id)
                if existing is None:
                    raise RecoveryStoreConflict("committed branch is missing checkpoint")
                receipts = self.checkpoint_receipts(new_checkpoint.checkpoint_id)
                if not receipts:
                    raise RecoveryStoreConflict("committed branch is missing checkpoint receipt")
                return existing, receipts[0]
            if str(row["status"]) != "pending":
                raise RecoveryStoreConflict(f"branch delta cannot commit from {row['status']}")
            head = connection.execute(
                "SELECT checkpoint_id, commit_revision FROM recovery_checkpoint_heads WHERE task_id = ?",
                (delta.task_id,),
            ).fetchone()
            if head is None or int(head["commit_revision"]) != expected_head_revision:
                raise RecoveryStoreConflict("branch commit checkpoint head changed")
            if new_checkpoint.parent_checkpoint_id != str(head["checkpoint_id"]):
                raise RecoveryStoreConflict("branch successor parent is not current checkpoint head")
            connection.execute(
                """
                INSERT INTO recovery_checkpoints
                    (checkpoint_id, run_id, task_id, session_id, parent_checkpoint_id, phase,
                     commit_revision, signature, content_digest, created_at, committed_at, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_checkpoint.checkpoint_id, new_checkpoint.run_id, new_checkpoint.task_id,
                    new_checkpoint.session_id, new_checkpoint.parent_checkpoint_id,
                    new_checkpoint.phase.value, new_checkpoint.commit_revision, new_checkpoint.signature,
                    new_checkpoint.content_digest, new_checkpoint.created_at,
                    new_checkpoint.committed_at, payload,
                ),
            )
            for write in (*new_checkpoint.committed_writes, *new_checkpoint.pending_writes):
                connection.execute(
                    """
                    INSERT INTO recovery_checkpoint_writes
                        (write_id, checkpoint_id, task_id, task_key, channel, state, sequence, value_digest, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        write.write_id, new_checkpoint.checkpoint_id, new_checkpoint.task_id,
                        write.task_key, write.channel, write.state.value, write.sequence,
                        write.value_digest, canonical_json(write.to_dict()),
                    ),
                )
            connection.execute(
                """
                UPDATE recovery_checkpoint_heads
                SET run_id = ?, checkpoint_id = ?, commit_revision = ?, signature = ?, updated_at = ?
                WHERE task_id = ? AND commit_revision = ?
                """,
                (
                    new_checkpoint.run_id, new_checkpoint.checkpoint_id, new_checkpoint.commit_revision,
                    new_checkpoint.signature, new_checkpoint.committed_at or utc_now(),
                    new_checkpoint.task_id, expected_head_revision,
                ),
            )
            self._insert_checkpoint_receipt(connection, checkpoint_receipt)
            committed_at = new_checkpoint.committed_at or utc_now()
            for key in sorted(delta.write_set):
                connection.execute(
                    """
                    INSERT INTO recovery_delta_writes
                        (task_id, commit_revision, branch_id, key, value_digest, committed_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        delta.task_id, new_checkpoint.commit_revision, delta.branch_id, key,
                        stable_digest(resulting_values.get(key)), committed_at,
                    ),
                )
            connection.execute(
                "UPDATE recovery_branch_deltas SET status = 'committed', committed_revision = ? WHERE branch_id = ?",
                (new_checkpoint.commit_revision, delta.branch_id),
            )
            self._journal(connection, delta.run_id, delta.task_id, "branch_delta", delta.branch_id, "committed", new_checkpoint.commit_revision, new_checkpoint.checkpoint_id, {"delta": delta.to_dict(), "resulting_values": copy.deepcopy(dict(resulting_values))})
            self._journal(connection, new_checkpoint.run_id, new_checkpoint.task_id, "checkpoint", new_checkpoint.checkpoint_id, "committed_from_delta", new_checkpoint.commit_revision, delta.branch_id, new_checkpoint.to_dict())
        return new_checkpoint, checkpoint_receipt

    def reject_branch_delta(self, branch_id: str, *, reason: str) -> None:
        self.initialize()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT run_id, task_id, base_revision, status FROM recovery_branch_deltas WHERE branch_id = ?",
                (branch_id,),
            ).fetchone()
            if row is None:
                raise RecoveryStoreConflict(f"branch delta not found: {branch_id}")
            if str(row["status"]) == "committed":
                raise RecoveryStoreConflict("committed branch delta cannot be rejected")
            connection.execute(
                "UPDATE recovery_branch_deltas SET status = 'rejected' WHERE branch_id = ?",
                (branch_id,),
            )
            self._journal(connection, str(row["run_id"]), str(row["task_id"]), "branch_delta", branch_id, "rejected", int(row["base_revision"]), "", {"reason": reason})

    def journal(
        self,
        *,
        task_id: str = "",
        after_sequence: int = 0,
        limit: int = 2000,
    ) -> tuple[dict[str, Any], ...]:
        self.initialize()
        with self._connect() as connection:
            if task_id:
                rows = connection.execute(
                    """
                    SELECT * FROM recovery_journal
                    WHERE task_id = ? AND sequence > ?
                    ORDER BY sequence LIMIT ?
                    """,
                    (task_id, after_sequence, self._bounded_limit(limit, maximum=10_000)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM recovery_journal WHERE sequence > ? ORDER BY sequence LIMIT ?",
                    (after_sequence, self._bounded_limit(limit, maximum=10_000)),
                ).fetchall()
        return tuple(self._journal_row(row) for row in rows)

    def task_snapshot(self, task_id: str) -> dict[str, Any]:
        head = self.checkpoint_head(task_id)
        plans = self.plans(task_id=task_id)
        return {
            "schema": "zyra.recovery-store-snapshot/v1",
            "state_owner": "python.RecoveryPlanStore",
            "task_id": task_id,
            "signals": [item.to_dict() for item in self.signals(task_id=task_id)],
            "plans": [item.to_dict() for item in plans],
            "action_receipts": [
                receipt.to_dict()
                for plan in plans
                for receipt in self.action_receipts(plan_id=plan.plan_id)
            ],
            "outcomes": [item.to_dict() for item in self.outcomes(task_id=task_id)],
            "checkpoint_head": head.to_dict() if head else None,
            "checkpoints": [item.to_dict() for item in self.checkpoints(task_id=task_id)],
            "route_decisions": [item.to_dict() for item in self.route_decisions(task_id=task_id)],
            "routing_memory": [item.to_dict() for item in self.feedback(task_id=task_id)],
            "journal": list(self.journal(task_id=task_id)),
        }

    def integrity_report(self, *, task_id: str = "") -> dict[str, Any]:
        self.initialize()
        errors: list[str] = []
        checked = 0
        task_ids: list[str]
        with self._connect() as connection:
            if task_id:
                task_ids = [task_id]
            else:
                task_ids = [str(row["task_id"]) for row in connection.execute("SELECT task_id FROM recovery_checkpoint_heads ORDER BY task_id").fetchall()]
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            for row in foreign:
                errors.append(f"foreign_key:{tuple(row)}")
        for current_task in task_ids:
            checkpoints = self.checkpoints(task_id=current_task)
            previous: RecoveryCheckpoint | None = None
            for checkpoint in checkpoints:
                checked += 1
                try:
                    self.codec.decode(self.codec.encode(checkpoint))
                except Exception as error:  # noqa: BLE001 - report all corruption.
                    errors.append(f"checkpoint:{checkpoint.checkpoint_id}:{error}")
                if previous is not None:
                    if checkpoint.parent_checkpoint_id != previous.checkpoint_id:
                        errors.append(f"lineage:{checkpoint.checkpoint_id}:parent")
                    if checkpoint.commit_revision != previous.commit_revision + 1:
                        errors.append(f"lineage:{checkpoint.checkpoint_id}:revision")
                previous = checkpoint
            head = self.checkpoint_head(current_task)
            if checkpoints and (head is None or head.checkpoint_id != checkpoints[-1].checkpoint_id):
                errors.append(f"head:{current_task}:mismatch")
            for plan in self.plans(task_id=current_task):
                checked += 1
                if self.signal(plan.signal.signal_id) is None:
                    errors.append(f"plan:{plan.plan_id}:missing_signal")
                for receipt in self.action_receipts(plan_id=plan.plan_id):
                    checked += 1
                    if receipt.plan_id != plan.plan_id:
                        errors.append(f"receipt:{receipt.receipt_id}:scope")
        return {
            "schema": "zyra.recovery-store-integrity/v1",
            "ok": not errors,
            "database": str(self.path),
            "task_id": task_id,
            "checked_records": checked,
            "errors": errors,
            "state_owner": "python.RecoveryPlanStore",
        }

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _loads(value: str | bytes | bytearray | memoryview) -> dict[str, Any]:
        if isinstance(value, (bytes, bytearray, memoryview)):
            value = bytes(value).decode("utf-8")
        result = json.loads(str(value))
        if not isinstance(result, dict):
            raise RecoveryStoreError("stored recovery payload is not an object")
        return result

    @staticmethod
    def _bounded_limit(value: int, *, maximum: int = 5000) -> int:
        return max(1, min(maximum, int(value)))

    @staticmethod
    def _future(value: str, now: datetime) -> bool:
        if not value:
            return False
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return parsed.astimezone(UTC) > now

    @staticmethod
    def _validate_plan_transition(previous: RecoveryPlan, current: RecoveryPlan) -> None:
        if previous.plan_id != current.plan_id or previous.signal.signal_id != current.signal.signal_id:
            raise RecoveryStoreConflict("recovery plan identity changed")
        if previous.status.terminal and current.status is not previous.status:
            raise RecoveryStoreConflict("terminal recovery plan cannot transition")
        if previous.status is current.status:
            return
        allowed = {
            RecoveryPlanStatus.PLANNED: {RecoveryPlanStatus.CLAIMED, RecoveryPlanStatus.CANCELLED, RecoveryPlanStatus.SUPERSEDED},
            RecoveryPlanStatus.CLAIMED: {RecoveryPlanStatus.PLANNED, RecoveryPlanStatus.APPLYING, RecoveryPlanStatus.CANCELLED},
            RecoveryPlanStatus.APPLYING: {
                RecoveryPlanStatus.WAITING_PERMISSION, RecoveryPlanStatus.WAITING_AUTH,
                RecoveryPlanStatus.WAITING_BACKOFF, RecoveryPlanStatus.APPLIED,
                RecoveryPlanStatus.FAILED, RecoveryPlanStatus.CANCELLED,
            },
            RecoveryPlanStatus.WAITING_PERMISSION: {RecoveryPlanStatus.PLANNED, RecoveryPlanStatus.APPLYING, RecoveryPlanStatus.FAILED, RecoveryPlanStatus.CANCELLED},
            RecoveryPlanStatus.WAITING_AUTH: {RecoveryPlanStatus.PLANNED, RecoveryPlanStatus.APPLYING, RecoveryPlanStatus.FAILED, RecoveryPlanStatus.CANCELLED},
            RecoveryPlanStatus.WAITING_BACKOFF: {RecoveryPlanStatus.PLANNED, RecoveryPlanStatus.APPLYING, RecoveryPlanStatus.FAILED, RecoveryPlanStatus.CANCELLED},
            RecoveryPlanStatus.APPLIED: {RecoveryPlanStatus.SUCCEEDED, RecoveryPlanStatus.FAILED, RecoveryPlanStatus.PLANNED},
            RecoveryPlanStatus.SUCCEEDED: {RecoveryPlanStatus.SUCCEEDED},
            RecoveryPlanStatus.FAILED: {RecoveryPlanStatus.FAILED},
            RecoveryPlanStatus.CANCELLED: {RecoveryPlanStatus.CANCELLED},
            RecoveryPlanStatus.SUPERSEDED: {RecoveryPlanStatus.SUPERSEDED},
        }
        if current.status not in allowed[previous.status]:
            raise RecoveryStoreConflict(
                f"invalid recovery plan transition {previous.status.value} -> {current.status.value}"
            )

    @staticmethod
    def _validate_fence_transition(previous: SideEffectState, current: SideEffectState) -> None:
        allowed = {
            SideEffectState.RESERVED: {SideEffectState.STARTED, SideEffectState.CANCELLED},
            SideEffectState.STARTED: {SideEffectState.COMMITTED, SideEffectState.FAILED, SideEffectState.CANCELLED},
            SideEffectState.FAILED: {SideEffectState.STARTED, SideEffectState.CANCELLED},
            SideEffectState.COMMITTED: {SideEffectState.COMMITTED},
            SideEffectState.CANCELLED: {SideEffectState.CANCELLED},
        }
        if current not in allowed[previous]:
            raise RecoveryStoreConflict(
                f"invalid side-effect transition {previous.value} -> {current.value}"
            )

    def _insert_checkpoint_receipt(self, connection: sqlite3.Connection, receipt: CheckpointReceipt) -> None:
        connection.execute(
            """
            INSERT INTO recovery_checkpoint_receipts
                (receipt_id, checkpoint_id, task_id, phase, commit_revision, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                receipt.receipt_id, receipt.checkpoint_id, receipt.task_id, receipt.phase.value,
                receipt.commit_revision, receipt.created_at, canonical_json(receipt.to_dict()),
            ),
        )

    def _checkpoint_receipt_connection(
        self,
        connection: sqlite3.Connection,
        checkpoint_id: str,
    ) -> CheckpointReceipt | None:
        row = connection.execute(
            "SELECT payload_json FROM recovery_checkpoint_receipts WHERE checkpoint_id = ? ORDER BY created_at, receipt_id LIMIT 1",
            (checkpoint_id,),
        ).fetchone()
        return CheckpointReceipt.from_dict(self._loads(row["payload_json"])) if row else None

    def _journal(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        entity_kind: str,
        entity_id: str,
        operation: str,
        revision: int,
        causation_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO recovery_journal
                (run_id, task_id, entity_kind, entity_id, operation, revision,
                 causation_id, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, task_id, entity_kind, entity_id, operation, revision,
                causation_id, utc_now(), canonical_json(copy.deepcopy(dict(payload))),
            ),
        )

    def _journal_row(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "sequence": int(row["sequence"]),
            "run_id": str(row["run_id"]),
            "task_id": str(row["task_id"]),
            "entity_kind": str(row["entity_kind"]),
            "entity_id": str(row["entity_id"]),
            "operation": str(row["operation"]),
            "revision": int(row["revision"]),
            "causation_id": str(row["causation_id"]),
            "created_at": str(row["created_at"]),
            "payload": self._loads(row["payload_json"]),
        }


__all__ = [
    "RecoveryLeaseError",
    "RecoveryPlanStore",
    "RecoveryReplayConflict",
    "RecoveryStoreConflict",
    "RecoveryStoreError",
]
