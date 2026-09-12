from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .canonical import canonical_json, canonicalize, digest, new_identity, utc_now
from .errors import conflict, invalid, not_found
from .models import (
    ExecutionProfile,
    FaultInjection,
    PreflightKind,
    PreflightPolicy,
    PreflightTarget,
    ScenarioConfiguration,
    ScenarioMode,
    ScenarioPhase,
    ScenarioRun,
    SealedPolicy,
)


# Bump whenever the fields held by the stored list projection change, so
# existing rows are re-derived instead of served in the old shape.
SUMMARY_VERSION = 2


ALLOWED_TRANSITIONS: dict[ScenarioPhase, set[ScenarioPhase]] = {
    ScenarioPhase.CREATED: {
        ScenarioPhase.ADMITTED,
        ScenarioPhase.CANCELLED,
        ScenarioPhase.FAILED,
        ScenarioPhase.ARCHIVED,
    },
    ScenarioPhase.ADMITTED: {
        ScenarioPhase.QUEUED,
        ScenarioPhase.CANCELLED,
        ScenarioPhase.FAILED,
        ScenarioPhase.ARCHIVED,
    },
    ScenarioPhase.QUEUED: {
        ScenarioPhase.RUNNING,
        ScenarioPhase.CANCELLING,
        ScenarioPhase.CANCELLED,
        ScenarioPhase.FAILED,
    },
    ScenarioPhase.RUNNING: {
        ScenarioPhase.QUEUED,
        ScenarioPhase.CANCELLING,
        ScenarioPhase.CANCELLED,
        ScenarioPhase.SUCCEEDED,
        ScenarioPhase.FAILED,
    },
    ScenarioPhase.CANCELLING: {
        ScenarioPhase.QUEUED,
        ScenarioPhase.CANCELLED,
        ScenarioPhase.SUCCEEDED,
        ScenarioPhase.FAILED,
    },
    ScenarioPhase.CANCELLED: {ScenarioPhase.ARCHIVED},
    ScenarioPhase.SUCCEEDED: {ScenarioPhase.ARCHIVED},
    ScenarioPhase.FAILED: {ScenarioPhase.QUEUED, ScenarioPhase.ARCHIVED},
    ScenarioPhase.ARCHIVED: set(),
}


class ScenarioRunStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterable[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def create(self, run: ScenarioRun) -> ScenarioRun:
        payload = self._encode(run)
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO scenario_runs (
                        scenario_run_id, scenario_id, definition_version,
                        input_digest, phase, revision, created_at, updated_at,
                        owner_run_id, task_id, cancel_requested, archived,
                        record_json, record_digest, summary_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.scenario_run_id,
                        run.configuration.scenario_id,
                        run.configuration.definition_version,
                        run.configuration.input_digest,
                        run.phase.value,
                        run.revision,
                        run.created_at,
                        run.updated_at,
                        run.owner_run_id,
                        run.task_id,
                        int(run.cancel_requested),
                        int(run.phase is ScenarioPhase.ARCHIVED),
                        payload,
                        digest(run.to_dict(include_input=True)),
                        self._encode_summary(run),
                    ),
                )
                self._append_transition(
                    connection,
                    run,
                    from_phase="",
                    reason="scenario.created",
                    detail={"configuration_digest": run.configuration.configuration_digest},
                )
                connection.commit()
            except sqlite3.IntegrityError as error:
                connection.rollback()
                raise conflict(
                    "scenario_run_conflict",
                    "Scenario run identity or active input already exists.",
                    phase="store",
                    detail={
                        "scenario_run_id": run.scenario_run_id,
                        "input_digest": run.configuration.input_digest,
                    },
                ) from error
        return run

    def load(self, scenario_run_id: str) -> ScenarioRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT record_json FROM scenario_runs WHERE scenario_run_id = ?",
                (scenario_run_id,),
            ).fetchone()
        return self._decode(row[0]) if row else None

    def require(self, scenario_run_id: str) -> ScenarioRun:
        run = self.load(scenario_run_id)
        if run is None:
            raise not_found(
                "scenario_run_not_found",
                "Scenario run does not exist.",
                phase="store",
            )
        return run

    def save(
        self,
        run: ScenarioRun,
        *,
        expected_revision: int,
        reason: str,
        detail: Mapping[str, Any] | None = None,
    ) -> ScenarioRun:
        payload = self._encode(run)
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT phase, revision FROM scenario_runs WHERE scenario_run_id = ?",
                (run.scenario_run_id,),
            ).fetchone()
            if current is None:
                connection.rollback()
                raise not_found(
                    "scenario_run_not_found",
                    "Scenario run does not exist.",
                    phase="store",
                )
            from_phase = ScenarioPhase(str(current[0]))
            current_revision = int(current[1])
            if current_revision != expected_revision:
                connection.rollback()
                raise conflict(
                    "scenario_revision_conflict",
                    "Scenario run changed since it was read.",
                    phase="store",
                    detail={
                        "expected_revision": expected_revision,
                        "actual_revision": current_revision,
                    },
                )
            if run.revision != expected_revision + 1:
                connection.rollback()
                raise conflict(
                    "scenario_revision_invalid",
                    "Scenario revision must advance exactly once per committed mutation.",
                    phase="store",
                )
            if run.phase != from_phase and run.phase not in ALLOWED_TRANSITIONS[from_phase]:
                connection.rollback()
                raise conflict(
                    "scenario_phase_transition_invalid",
                    "Scenario lifecycle transition is invalid.",
                    phase="store",
                    detail={"from": from_phase.value, "to": run.phase.value},
                )
            updated = connection.execute(
                """
                UPDATE scenario_runs
                SET phase = ?, revision = ?, updated_at = ?, owner_run_id = ?,
                    task_id = ?, cancel_requested = ?, archived = ?,
                    record_json = ?, record_digest = ?, summary_json = ?
                WHERE scenario_run_id = ? AND revision = ?
                """,
                (
                    run.phase.value,
                    run.revision,
                    run.updated_at,
                    run.owner_run_id,
                    run.task_id,
                    int(run.cancel_requested),
                    int(run.phase is ScenarioPhase.ARCHIVED),
                    payload,
                    digest(run.to_dict(include_input=True)),
                    self._encode_summary(run),
                    run.scenario_run_id,
                    expected_revision,
                ),
            ).rowcount
            if updated != 1:
                connection.rollback()
                raise conflict(
                    "scenario_revision_conflict",
                    "Scenario run mutation lost an optimistic concurrency race.",
                    phase="store",
                )
            self._append_transition(
                connection,
                run,
                from_phase=from_phase.value,
                reason=reason,
                detail=dict(detail or {}),
            )
            connection.commit()
        return run

    def mutate(
        self,
        scenario_run_id: str,
        mutation: Any,
        *,
        reason: str,
        retries: int = 8,
    ) -> ScenarioRun:
        last: Exception | None = None
        for _ in range(retries):
            current = self.require(scenario_run_id)
            next_run = mutation(current)
            if not isinstance(next_run, ScenarioRun):
                raise TypeError("scenario mutation must return ScenarioRun")
            try:
                return self.save(
                    next_run,
                    expected_revision=current.revision,
                    reason=reason,
                )
            except Exception as error:
                if getattr(error, "code", "") != "scenario_revision_conflict":
                    raise
                last = error
        if last:
            raise last
        raise RuntimeError("scenario mutation retry exhausted")

    def list(
        self,
        *,
        phases: Iterable[ScenarioPhase] = (),
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ScenarioRun, ...]:
        selected_phases = tuple(phase.value for phase in phases)
        clauses = []
        arguments: list[Any] = []
        if selected_phases:
            placeholders = ",".join("?" for _ in selected_phases)
            clauses.append(f"phase IN ({placeholders})")
            arguments.extend(selected_phases)
        if not include_archived:
            clauses.append("archived = 0")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        arguments.extend([max(1, min(10_000, limit)), max(0, offset)])
        with self._connection() as connection:
            # Read summary_json only.  Selecting record_json as well made SQLite
            # materialise every embedded evidence manifest even though the value
            # went unused: 484MB across 34 runs, ~6s per list call.  COALESCE
            # touches record_json only for a row whose summary is still NULL.
            rows = connection.execute(
                "SELECT COALESCE(summary_json, record_json) FROM scenario_runs"
                + where
                + " ORDER BY created_at DESC, scenario_run_id DESC LIMIT ? OFFSET ?",
                arguments,
            ).fetchall()
        return tuple(self._decode(row[0]) for row in rows)

    def input_seen(self, input_digest: str, *, excluding_run_id: str = "") -> bool:
        query = "SELECT 1 FROM scenario_runs WHERE input_digest = ?"
        arguments: list[Any] = [input_digest]
        if excluding_run_id:
            query += " AND scenario_run_id != ?"
            arguments.append(excluding_run_id)
        query += " LIMIT 1"
        with self._connection() as connection:
            return connection.execute(query, arguments).fetchone() is not None

    def transitions(self, scenario_run_id: str) -> tuple[dict[str, Any], ...]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT transition_id, sequence, from_phase, to_phase, reason,
                       revision, detail_json, created_at, transition_digest
                FROM scenario_transitions
                WHERE scenario_run_id = ?
                ORDER BY sequence
                """,
                (scenario_run_id,),
            ).fetchall()
        return tuple(
            {
                "transition_id": row[0],
                "scenario_run_id": scenario_run_id,
                "sequence": int(row[1]),
                "from_phase": row[2],
                "to_phase": row[3],
                "reason": row[4],
                "revision": int(row[5]),
                "detail": json.loads(row[6]),
                "created_at": row[7],
                "transition_digest": row[8],
            }
            for row in rows
        )

    def append_receipt(
        self,
        scenario_run_id: str,
        *,
        kind: str,
        receipt: Mapping[str, Any],
    ) -> dict[str, Any]:
        receipt_id = str(
            receipt.get("receipt_id")
            or receipt.get("manifest_id")
            or new_identity("receipt")
        )
        payload = canonical_json(receipt)
        checksum = digest(receipt)
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO scenario_receipts (
                    receipt_id, scenario_run_id, kind, receipt_json,
                    receipt_digest, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(receipt_id) DO UPDATE SET
                    receipt_json=excluded.receipt_json,
                    receipt_digest=excluded.receipt_digest
                """,
                (
                    receipt_id,
                    scenario_run_id,
                    kind,
                    payload,
                    checksum,
                    utc_now(),
                ),
            )
        return {"receipt_id": receipt_id, "receipt_digest": checksum}

    def receipts(
        self,
        scenario_run_id: str,
        *,
        kind: str = "",
        include_body: bool = True,
    ) -> tuple[dict[str, Any], ...]:
        # ``include_body`` False returns only the receipt envelope.  The
        # polling status endpoint must use it: some receipts (evidence manifest,
        # effective steps, metrics) reach tens of megabytes and would push the
        # status response past the client limit.  Full bodies are read from the
        # dedicated evidence endpoint.
        query = (
            "SELECT receipt_id, kind, receipt_json, receipt_digest, created_at "
            "FROM scenario_receipts WHERE scenario_run_id = ?"
        )
        arguments: list[Any] = [scenario_run_id]
        if kind:
            query += " AND kind = ?"
            arguments.append(kind)
        query += " ORDER BY created_at, receipt_id"
        with self._connection() as connection:
            rows = connection.execute(query, arguments).fetchall()
        return tuple(
            {
                "receipt_id": row[0],
                "kind": row[1],
                "receipt": json.loads(row[2]) if include_body else None,
                "receipt_digest": row[3],
                "created_at": row[4],
            }
            for row in rows
        )

    def reconcile_interrupted(self) -> tuple[ScenarioRun, ...]:
        recoverable = self.list(
            phases=(
                ScenarioPhase.QUEUED,
                ScenarioPhase.RUNNING,
                ScenarioPhase.CANCELLING,
            ),
            include_archived=False,
            limit=100_000,
        )
        output: list[ScenarioRun] = []
        for run in recoverable:
            if run.phase is ScenarioPhase.QUEUED:
                output.append(run)
                continue
            next_run = run.evolve(
                phase=ScenarioPhase.QUEUED,
                failure={
                    "code": "scenario_process_restarted",
                    "message": "Interrupted scenario was re-queued from durable state.",
                    "retryable": True,
                    "previous_phase": run.phase.value,
                },
                cancel_requested=False,
            )
            output.append(
                self.save(
                    next_run,
                    expected_revision=run.revision,
                    reason="scenario.reconciled_after_restart",
                )
            )
        return tuple(output)

    def summary(self) -> dict[str, Any]:
        with self._connection() as connection:
            phases = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT phase, COUNT(*) FROM scenario_runs GROUP BY phase"
                )
            }
            run_count = int(
                connection.execute("SELECT COUNT(*) FROM scenario_runs").fetchone()[0]
            )
            receipt_count = int(
                connection.execute("SELECT COUNT(*) FROM scenario_receipts").fetchone()[0]
            )
            transition_count = int(
                connection.execute("SELECT COUNT(*) FROM scenario_transitions").fetchone()[0]
            )
        result = {
            "schema": "zyra.scenario-store-summary/v1",
            "path": str(self.path),
            "run_count": run_count,
            "receipt_count": receipt_count,
            "transition_count": transition_count,
            "phases": phases,
        }
        result["summary_digest"] = digest(result)
        return result

    def _append_transition(
        self,
        connection: sqlite3.Connection,
        run: ScenarioRun,
        *,
        from_phase: str,
        reason: str,
        detail: Mapping[str, Any],
    ) -> None:
        sequence = int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM scenario_transitions "
                "WHERE scenario_run_id = ?",
                (run.scenario_run_id,),
            ).fetchone()[0]
        )
        created_at = utc_now()
        payload = {
            "scenario_run_id": run.scenario_run_id,
            "sequence": sequence,
            "from_phase": from_phase,
            "to_phase": run.phase.value,
            "reason": reason,
            "revision": run.revision,
            "detail": canonicalize(detail),
            "created_at": created_at,
        }
        connection.execute(
            """
            INSERT INTO scenario_transitions (
                transition_id, scenario_run_id, sequence, from_phase,
                to_phase, reason, revision, detail_json, created_at,
                transition_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_identity("transition"),
                run.scenario_run_id,
                sequence,
                from_phase,
                run.phase.value,
                reason,
                run.revision,
                canonical_json(detail),
                created_at,
                digest(payload),
            ),
        )

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS scenario_runs (
                    scenario_run_id TEXT PRIMARY KEY,
                    scenario_id TEXT NOT NULL,
                    definition_version TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    owner_run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    archived INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    record_digest TEXT NOT NULL,
                    summary_json TEXT
                );
                CREATE INDEX IF NOT EXISTS scenario_runs_created
                    ON scenario_runs(created_at DESC);
                CREATE INDEX IF NOT EXISTS scenario_runs_input
                    ON scenario_runs(input_digest, archived);
                CREATE INDEX IF NOT EXISTS scenario_runs_phase
                    ON scenario_runs(phase, updated_at);
                CREATE TABLE IF NOT EXISTS scenario_transitions (                    transition_id TEXT PRIMARY KEY,
                    scenario_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    from_phase TEXT NOT NULL,
                    to_phase TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    transition_digest TEXT NOT NULL,
                    UNIQUE(scenario_run_id, sequence),
                    FOREIGN KEY(scenario_run_id)
                        REFERENCES scenario_runs(scenario_run_id)
                        ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS scenario_receipts (
                    receipt_id TEXT PRIMARY KEY,
                    scenario_run_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    receipt_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(scenario_run_id)
                        REFERENCES scenario_runs(scenario_run_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS scenario_receipts_run_kind
                    ON scenario_receipts(scenario_run_id, kind, created_at);
                """
            )
            self._migrate_summary_column(connection)

    def _migrate_summary_column(self, connection: sqlite3.Connection) -> None:
        """Add ``summary_json`` to databases created before it existed.

        Databases written by an earlier version have only ``record_json``,
        which embeds the full evidence manifest (tens of megabytes per run).
        Listing such a database previously meant decoding every blob; the
        summary column lets a list read a few hundred bytes per run instead.
        Existing rows keep ``summary_json`` NULL and are backfilled the first
        time they are saved again.
        """

        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(scenario_runs)")
        }
        if "summary_json" not in columns:
            connection.execute(
                "ALTER TABLE scenario_runs ADD COLUMN summary_json TEXT"
            )
        self._backfill_summaries(connection)

    def _backfill_summaries(self, connection: sqlite3.Connection) -> None:
        """Derive ``summary_json`` for rows missing it or holding an old shape.

        Runs on open so a database written by an earlier build gets the fast
        list path immediately, and so a change to SUMMARY_VERSION re-derives
        rows instead of serving a stale projection.
        """

        rows = connection.execute(
            "SELECT scenario_run_id, record_json, summary_json FROM scenario_runs"
        ).fetchall()
        for scenario_run_id, record_json, summary_json in rows:
            if summary_json:
                try:
                    if (
                        json.loads(summary_json).get("summary_version")
                        == SUMMARY_VERSION
                    ):
                        continue
                except (TypeError, ValueError):
                    pass
            try:
                run = self._decode(record_json)
            except Exception:  # noqa: BLE001 - never block startup on one row.
                continue
            connection.execute(
                "UPDATE scenario_runs SET summary_json = ?"
                " WHERE scenario_run_id = ?",
                (self._encode_summary(run), str(scenario_run_id)),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _encode(self, run: ScenarioRun) -> str:
        return canonical_json(run.to_dict(include_input=True))

    def _encode_summary(self, run: ScenarioRun) -> str:
        """Encode the list projection of a run.

        A list page previously carried every run's full record -- dominated by
        the evidence manifest (~99.9% of a stored run; one measured at 15.4MB)
        plus the policy decisions and full configuration blocks the list never
        renders.  Listing used to read and parse 484MB of JSON across 34 runs.
        The summary keeps identity, phase and timing, and callers read the rest
        from the per-run status and evidence endpoints.

        The stored summary carries ``summary_version``: it is a derived
        projection, so changing which fields it holds has to invalidate the
        summaries already on disk.  Without that marker a shape change leaves
        stale rows behind and the client sees a projection that no longer
        matches what the current code expects.
        """

        payload = run.to_dict(include_evidence=False, projection="summary")
        payload["summary_version"] = SUMMARY_VERSION
        return canonical_json(payload)

    def _decode(self, payload: str) -> ScenarioRun:
        return run_from_dict(json.loads(payload))


def run_from_dict(raw: Mapping[str, Any]) -> ScenarioRun:
    configuration = configuration_from_dict(raw.get("configuration") or {})
    return ScenarioRun(
        scenario_run_id=str(raw["scenario_run_id"]),
        configuration=configuration,
        phase=ScenarioPhase(str(raw["phase"])),
        revision=int(raw.get("revision") or 0),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        started_at=str(raw.get("started_at") or ""),
        completed_at=str(raw.get("completed_at") or ""),
        owner_run_id=str(raw.get("owner_run_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        preflight_receipt=_mapping_or_none(raw.get("preflight_receipt")),
        policy_decisions=tuple(raw.get("policy_decisions") or ()),
        interventions=tuple(raw.get("interventions") or ()),
        evidence_manifest=_mapping_or_none(raw.get("evidence_manifest")),
        verification_receipt=_mapping_or_none(raw.get("verification_receipt")),
        failure=_mapping_or_none(raw.get("failure")),
        cancel_requested=bool(raw.get("cancel_requested", False)),
        archive_reason=str(raw.get("archive_reason") or ""),
    )


def configuration_from_dict(raw: Mapping[str, Any]) -> ScenarioConfiguration:
    # A summary projection omits the profile, fault schedule and policy block;
    # it is only ever decoded to render a list, never to run or mutate.  Fill
    # in inert placeholders so the dataclass still constructs.
    profile_raw = raw.get("profile") or {
        "profile_id": "",
        "provider_id": "",
        "model_id": "",
        "backend_id": "",
        "maximum_effective_steps": 0,
        "maximum_wall_time_ms": 0,
    }
    policy_raw = raw.get("policy") or {
        "policy_id": "",
        "version": "",
        "ask_disposition": "",
        "unknown_disposition": "",
        "maximum_denials": 0,
        "manual_mutation_disposition": "",
    }
    faults = tuple(
        FaultInjection(
            injection_id=str(item["injection_id"]),
            stage=str(item["stage"]),
            kind=str(item["kind"]),
            after_effective_step=int(item.get("after_effective_step") or 0),
            target=str(item.get("target") or ""),
            payload=dict(item.get("payload") or {}),
        )
        for item in raw.get("faults") or ()
    )
    preflight = tuple(
        PreflightTarget(
            kind=PreflightKind(str(item["kind"])),
            path=str(item["path"]),
            policy=PreflightPolicy(str(item["policy"])),
            ignored_names=tuple(item.get("ignored_names") or ()),
            required=bool(item.get("required", True)),
        )
        for item in raw.get("preflight_targets") or ()
    )
    profile = ExecutionProfile(
        profile_id=str(profile_raw["profile_id"]),
        provider_id=str(profile_raw["provider_id"]),
        model_id=str(profile_raw["model_id"]),
        backend_id=str(profile_raw["backend_id"]),
        worker_classes=tuple(profile_raw.get("worker_classes") or ()),
        maximum_effective_steps=int(profile_raw["maximum_effective_steps"]),
        maximum_wall_time_ms=int(profile_raw["maximum_wall_time_ms"]),
        metadata=dict(profile_raw.get("metadata") or {}),
    )
    policy = SealedPolicy(
        policy_id=str(policy_raw["policy_id"]),
        version=str(policy_raw["version"]),
        allow_actions=tuple(policy_raw.get("allow_actions") or ()),
        deny_actions=tuple(policy_raw.get("deny_actions") or ()),
        high_risk_actions=tuple(policy_raw.get("high_risk_actions") or ()),
        ask_disposition=str(policy_raw["ask_disposition"]),
        unknown_disposition=str(policy_raw["unknown_disposition"]),
        maximum_denials=int(policy_raw["maximum_denials"]),
        manual_mutation_disposition=str(policy_raw["manual_mutation_disposition"]),
        metadata=dict(policy_raw.get("metadata") or {}),
    )
    return ScenarioConfiguration(
        scenario_id=str(raw["scenario_id"]),
        definition_version=str(raw["definition_version"]),
        definition_digest=str(raw["definition_digest"]),
        mode=ScenarioMode(str(raw["mode"])),
        input_text=str(raw.get("input_text") or ""),
        input_digest=str(raw["input_digest"]),
        seed=int(raw.get("seed") or 0),
        profile=profile,
        faults=faults,
        preflight_targets=preflight,
        policy=policy,
        expected_policy_digest=str(raw.get("expected_policy_digest") or ""),
        requested_by=str(raw.get("requested_by") or ""),
        labels=dict(raw.get("labels") or {}),
        metadata=dict(raw.get("metadata") or {}),
    )


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None
