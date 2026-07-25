from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .canonical import canonicalize, digest, new_identity, pretty_json, utc_now
from .codec import experiment_run, raw_metric_sample
from .errors import conflict, invalid, not_found
from .models import ExperimentPhase, ExperimentRun, RawMetricSample


SCHEMA_VERSION = 1


class ExperimentStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS experiment_runs (
                    experiment_id TEXT PRIMARY KEY,
                    phase TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    envelope_digest TEXT NOT NULL,
                    source_evidence_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_experiment_runs_phase_updated
                    ON experiment_runs(phase, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_experiment_runs_source
                    ON experiment_runs(source_evidence_digest, created_at DESC);

                CREATE TABLE IF NOT EXISTS experiment_transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    transition_id TEXT NOT NULL UNIQUE,
                    experiment_id TEXT NOT NULL,
                    from_phase TEXT NOT NULL,
                    to_phase TEXT NOT NULL,
                    from_revision INTEGER NOT NULL,
                    to_revision INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    detail_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiment_runs(experiment_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_experiment_transitions_run
                    ON experiment_transitions(experiment_id, sequence);

                CREATE TABLE IF NOT EXISTS experiment_receipts (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    receipt_id TEXT NOT NULL UNIQUE,
                    experiment_id TEXT NOT NULL,
                    cell_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiment_runs(experiment_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_experiment_receipts_run_kind
                    ON experiment_receipts(experiment_id, kind, sequence);
                CREATE INDEX IF NOT EXISTS idx_experiment_receipts_cell
                    ON experiment_receipts(experiment_id, cell_id, sequence);

                CREATE TABLE IF NOT EXISTS experiment_samples (
                    sample_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    cell_id TEXT NOT NULL,
                    variant_id TEXT NOT NULL,
                    repetition INTEGER NOT NULL,
                    metric TEXT NOT NULL,
                    unit TEXT NOT NULL,
                    status TEXT NOT NULL,
                    value REAL,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_digest TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiment_runs(experiment_id)
                        ON DELETE CASCADE,
                    UNIQUE(experiment_id, cell_id, metric)
                );
                CREATE INDEX IF NOT EXISTS idx_experiment_samples_metric
                    ON experiment_samples(
                        experiment_id,
                        metric,
                        variant_id,
                        repetition
                    );
                CREATE INDEX IF NOT EXISTS idx_experiment_samples_cell
                    ON experiment_samples(experiment_id, cell_id, sequence);

                CREATE TABLE IF NOT EXISTS experiment_sources (
                    experiment_id TEXT PRIMARY KEY,
                    archive_id TEXT NOT NULL,
                    archive_digest TEXT NOT NULL,
                    member_digests_json TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    summary_json TEXT NOT NULL,
                    summary_digest TEXT NOT NULL,
                    admitted_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiment_runs(experiment_id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS experiment_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL,
                    manifest_digest TEXT NOT NULL,
                    bundle_path TEXT NOT NULL,
                    bundle_sha256 TEXT NOT NULL,
                    bundle_size INTEGER NOT NULL,
                    manifest_json TEXT NOT NULL,
                    verification_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(experiment_id)
                        REFERENCES experiment_runs(experiment_id)
                        ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_experiment_bundles_run
                    ON experiment_bundles(experiment_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS experiment_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO experiment_metadata(key, value)
                VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def create(self, run: ExperimentRun) -> ExperimentRun:
        payload = run.to_dict()
        serialized = pretty_json(payload)
        payload_digest = digest(payload)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO experiment_runs(
                        experiment_id,
                        phase,
                        revision,
                        envelope_digest,
                        source_evidence_digest,
                        created_at,
                        updated_at,
                        payload_json,
                        payload_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.experiment_id,
                        run.phase.value,
                        run.revision,
                        run.envelope.envelope_digest,
                        run.envelope.source_evidence_digest,
                        run.created_at,
                        run.updated_at,
                        serialized,
                        payload_digest,
                    ),
                )
                self._append_transition(
                    connection,
                    run.experiment_id,
                    from_phase="",
                    to_phase=run.phase.value,
                    from_revision=-1,
                    to_revision=run.revision,
                    reason="experiment.created",
                    detail={
                        "envelope_digest": run.envelope.envelope_digest,
                        "cell_count": len(run.cells),
                    },
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return run

    def save(
        self,
        run: ExperimentRun,
        *,
        expected_revision: int,
        reason: str,
        detail: Mapping[str, Any] | None = None,
    ) -> ExperimentRun:
        payload = run.to_dict()
        serialized = pretty_json(payload)
        payload_digest = digest(payload)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT phase, revision
                    FROM experiment_runs
                    WHERE experiment_id=?
                    """,
                    (run.experiment_id,),
                ).fetchone()
                if row is None:
                    raise not_found(
                        "experiment_not_found",
                        "Experiment run does not exist.",
                        detail={"experiment_id": run.experiment_id},
                    )
                actual_revision = int(row["revision"])
                if actual_revision != expected_revision:
                    raise conflict(
                        "experiment_revision_conflict",
                        "Experiment state changed concurrently.",
                        retryable=True,
                        detail={
                            "experiment_id": run.experiment_id,
                            "expected_revision": expected_revision,
                            "actual_revision": actual_revision,
                        },
                    )
                if run.revision != expected_revision + 1:
                    raise invalid(
                        "experiment_revision_invalid",
                        "Experiment revision must advance by one.",
                        phase="persistence",
                        detail={
                            "expected_revision": expected_revision + 1,
                            "actual_revision": run.revision,
                        },
                    )
                changed = connection.execute(
                    """
                    UPDATE experiment_runs
                    SET phase=?,
                        revision=?,
                        updated_at=?,
                        payload_json=?,
                        payload_digest=?
                    WHERE experiment_id=? AND revision=?
                    """,
                    (
                        run.phase.value,
                        run.revision,
                        run.updated_at,
                        serialized,
                        payload_digest,
                        run.experiment_id,
                        expected_revision,
                    ),
                ).rowcount
                if changed != 1:
                    raise conflict(
                        "experiment_revision_conflict",
                        "Experiment state changed concurrently.",
                        retryable=True,
                    )
                self._append_transition(
                    connection,
                    run.experiment_id,
                    from_phase=str(row["phase"]),
                    to_phase=run.phase.value,
                    from_revision=expected_revision,
                    to_revision=run.revision,
                    reason=reason,
                    detail=detail or {},
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return run

    def require(self, experiment_id: str) -> ExperimentRun:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json, payload_digest
                FROM experiment_runs
                WHERE experiment_id=?
                """,
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise not_found(
                "experiment_not_found",
                "Experiment run does not exist.",
                detail={"experiment_id": experiment_id},
            )
        payload = json.loads(str(row["payload_json"]))
        if digest(payload) != str(row["payload_digest"]):
            raise invalid(
                "experiment_store_payload_corrupt",
                "Persisted experiment payload digest does not match.",
                phase="persistence",
                detail={"experiment_id": experiment_id},
            )
        return experiment_run(payload)

    def list(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ExperimentRun, ...]:
        selected_limit = max(1, min(10_000, int(limit)))
        selected_offset = max(0, int(offset))
        query = """
            SELECT payload_json, payload_digest
            FROM experiment_runs
        """
        parameters: list[Any] = []
        if not include_archived:
            query += " WHERE phase <> ?"
            parameters.append(ExperimentPhase.ARCHIVED.value)
        query += " ORDER BY created_at DESC, experiment_id DESC LIMIT ? OFFSET ?"
        parameters.extend([selected_limit, selected_offset])
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        output: list[ExperimentRun] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if digest(payload) != str(row["payload_digest"]):
                raise invalid(
                    "experiment_store_payload_corrupt",
                    "Persisted experiment list contains a corrupt payload.",
                    phase="persistence",
                )
            output.append(experiment_run(payload))
        return tuple(output)

    def append_receipt(
        self,
        experiment_id: str,
        *,
        kind: str,
        payload: Mapping[str, Any],
        cell_id: str = "",
    ) -> dict[str, Any]:
        selected = canonicalize(payload)
        receipt_id = str(
            selected.get("receipt_id")
            or new_identity("receipt")
        )
        selected = {
            **selected,
            "receipt_id": receipt_id,
            "experiment_id": experiment_id,
        }
        serialized = pretty_json(selected)
        selected_digest = digest(selected)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO experiment_receipts(
                    receipt_id,
                    experiment_id,
                    cell_id,
                    kind,
                    payload_json,
                    payload_digest,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt_id,
                    experiment_id,
                    cell_id,
                    kind,
                    serialized,
                    selected_digest,
                    utc_now(),
                ),
            )
        return selected

    def receipts(
        self,
        experiment_id: str,
        *,
        kind: str = "",
        cell_id: str = "",
    ) -> tuple[dict[str, Any], ...]:
        clauses = ["experiment_id=?"]
        parameters: list[Any] = [experiment_id]
        if kind:
            clauses.append("kind=?")
            parameters.append(kind)
        if cell_id:
            clauses.append("cell_id=?")
            parameters.append(cell_id)
        query = (
            "SELECT payload_json, payload_digest FROM experiment_receipts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence"
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if digest(payload) != str(row["payload_digest"]):
                raise invalid(
                    "experiment_receipt_corrupt",
                    "Persisted experiment receipt digest does not match.",
                    phase="persistence",
                )
            output.append(payload)
        return tuple(output)

    def append_samples(
        self,
        experiment_id: str,
        samples: Iterable[RawMetricSample],
    ) -> tuple[RawMetricSample, ...]:
        selected = tuple(samples)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for sample in selected:
                    if sample.experiment_id != experiment_id:
                        raise invalid(
                            "experiment_sample_binding_invalid",
                            "Metric sample belongs to another experiment.",
                            phase="persistence",
                            detail={"sample_id": sample.sample_id},
                        )
                    payload = sample.to_dict()
                    connection.execute(
                        """
                        INSERT INTO experiment_samples(
                            sample_id,
                            experiment_id,
                            cell_id,
                            variant_id,
                            repetition,
                            metric,
                            unit,
                            status,
                            value,
                            sequence,
                            payload_json,
                            payload_digest,
                            observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            sample.sample_id,
                            sample.experiment_id,
                            sample.cell_id,
                            sample.variant_id,
                            sample.repetition,
                            sample.metric,
                            sample.unit,
                            sample.status.value,
                            sample.value,
                            sample.sequence,
                            pretty_json(payload),
                            digest(payload),
                            sample.observed_at,
                        ),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return selected

    def samples(
        self,
        experiment_id: str,
        *,
        metric: str = "",
        variant_id: str = "",
        cell_id: str = "",
        status: str = "",
        limit: int = 1_000_000,
        offset: int = 0,
    ) -> tuple[RawMetricSample, ...]:
        clauses = ["experiment_id=?"]
        parameters: list[Any] = [experiment_id]
        for field, value in (
            ("metric", metric),
            ("variant_id", variant_id),
            ("cell_id", cell_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{field}=?")
                parameters.append(value)
        parameters.extend([max(1, min(10_000_000, limit)), max(0, offset)])
        query = (
            "SELECT payload_json, payload_digest FROM experiment_samples WHERE "
            + " AND ".join(clauses)
            + " ORDER BY variant_id, repetition, sequence, sample_id LIMIT ? OFFSET ?"
        )
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        output: list[RawMetricSample] = []
        for row in rows:
            payload = json.loads(str(row["payload_json"]))
            if digest(payload) != str(row["payload_digest"]):
                raise invalid(
                    "experiment_sample_corrupt",
                    "Persisted metric sample digest does not match.",
                    phase="persistence",
                )
            output.append(raw_metric_sample(payload))
        return tuple(output)

    def record_source(
        self,
        experiment_id: str,
        *,
        archive_id: str,
        archive_digest: str,
        member_digests: Mapping[str, str],
        source_path: str,
        summary: Mapping[str, Any],
    ) -> None:
        selected = canonicalize(summary)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO experiment_sources(
                    experiment_id,
                    archive_id,
                    archive_digest,
                    member_digests_json,
                    source_path,
                    summary_json,
                    summary_digest,
                    admitted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(experiment_id) DO UPDATE SET
                    archive_id=excluded.archive_id,
                    archive_digest=excluded.archive_digest,
                    member_digests_json=excluded.member_digests_json,
                    source_path=excluded.source_path,
                    summary_json=excluded.summary_json,
                    summary_digest=excluded.summary_digest,
                    admitted_at=excluded.admitted_at
                """,
                (
                    experiment_id,
                    archive_id,
                    archive_digest,
                    pretty_json(member_digests),
                    source_path,
                    pretty_json(selected),
                    digest(selected),
                    utc_now(),
                ),
            )

    def source(self, experiment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM experiment_sources
                WHERE experiment_id=?
                """,
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise not_found(
                "experiment_source_not_found",
                "Experiment has no admitted source evidence.",
                detail={"experiment_id": experiment_id},
            )
        summary = json.loads(str(row["summary_json"]))
        if digest(summary) != str(row["summary_digest"]):
            raise invalid(
                "experiment_source_summary_corrupt",
                "Persisted source evidence summary digest does not match.",
                phase="persistence",
            )
        return {
            "archive_id": str(row["archive_id"]),
            "archive_digest": str(row["archive_digest"]),
            "member_digests": json.loads(str(row["member_digests_json"])),
            "source_path": str(row["source_path"]),
            "summary": summary,
            "admitted_at": str(row["admitted_at"]),
        }

    def record_bundle(
        self,
        experiment_id: str,
        *,
        bundle_id: str,
        manifest_digest: str,
        bundle_path: str,
        bundle_sha256: str,
        bundle_size: int,
        manifest: Mapping[str, Any],
        verification: Mapping[str, Any],
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO experiment_bundles(
                    bundle_id,
                    experiment_id,
                    manifest_digest,
                    bundle_path,
                    bundle_sha256,
                    bundle_size,
                    manifest_json,
                    verification_json,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    experiment_id,
                    manifest_digest,
                    bundle_path,
                    bundle_sha256,
                    bundle_size,
                    pretty_json(manifest),
                    pretty_json(verification),
                    utc_now(),
                ),
            )

    def bundles(self, experiment_id: str) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM experiment_bundles
                WHERE experiment_id=?
                ORDER BY created_at DESC, bundle_id DESC
                """,
                (experiment_id,),
            ).fetchall()
        return tuple(
            {
                "bundle_id": str(row["bundle_id"]),
                "manifest_digest": str(row["manifest_digest"]),
                "bundle_path": str(row["bundle_path"]),
                "bundle_sha256": str(row["bundle_sha256"]),
                "bundle_size": int(row["bundle_size"]),
                "manifest": json.loads(str(row["manifest_json"])),
                "verification": json.loads(str(row["verification_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        )

    def transitions(self, experiment_id: str) -> tuple[dict[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM experiment_transitions
                WHERE experiment_id=?
                ORDER BY sequence
                """,
                (experiment_id,),
            ).fetchall()
        output: list[dict[str, Any]] = []
        for row in rows:
            detail = json.loads(str(row["detail_json"]))
            if digest(detail) != str(row["detail_digest"]):
                raise invalid(
                    "experiment_transition_corrupt",
                    "Persisted experiment transition digest does not match.",
                    phase="persistence",
                )
            output.append(
                {
                    "sequence": int(row["sequence"]),
                    "transition_id": str(row["transition_id"]),
                    "experiment_id": str(row["experiment_id"]),
                    "from_phase": str(row["from_phase"]),
                    "to_phase": str(row["to_phase"]),
                    "from_revision": int(row["from_revision"]),
                    "to_revision": int(row["to_revision"]),
                    "reason": str(row["reason"]),
                    "detail": detail,
                    "detail_digest": str(row["detail_digest"]),
                    "created_at": str(row["created_at"]),
                }
            )
        return tuple(output)

    def integrity(self) -> dict[str, Any]:
        with self._connect() as connection:
            result = connection.execute("PRAGMA integrity_check").fetchone()
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) AS count FROM {table}"
                    ).fetchone()["count"]
                )
                for table in (
                    "experiment_runs",
                    "experiment_transitions",
                    "experiment_receipts",
                    "experiment_samples",
                    "experiment_sources",
                    "experiment_bundles",
                )
            }
        receipt = {
            "schema": "zyra.experiment-store-integrity/v1",
            "valid": bool(result and str(result[0]).casefold() == "ok"),
            "schema_version": SCHEMA_VERSION,
            "counts": counts,
            "checked_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _append_transition(
        self,
        connection: sqlite3.Connection,
        experiment_id: str,
        *,
        from_phase: str,
        to_phase: str,
        from_revision: int,
        to_revision: int,
        reason: str,
        detail: Mapping[str, Any],
    ) -> None:
        selected = canonicalize(detail)
        connection.execute(
            """
            INSERT INTO experiment_transitions(
                transition_id,
                experiment_id,
                from_phase,
                to_phase,
                from_revision,
                to_revision,
                reason,
                detail_json,
                detail_digest,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_identity("transition"),
                experiment_id,
                from_phase,
                to_phase,
                from_revision,
                to_revision,
                reason,
                pretty_json(selected),
                digest(selected),
                utc_now(),
            ),
        )
