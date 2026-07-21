from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from .models import (
    BranchGraphDelta,
    GraphCommitReceipt,
    GraphCommitStatus,
    GraphConflictStrategy,
    GraphStateSnapshot,
    canonical_json,
    digest,
    now_iso,
)


class GraphStoreConflict(RuntimeError):
    pass


class GraphStateStore:
    """Durable immutable snapshot/delta store with atomic head advancement."""

    SCHEMA_VERSION = 1

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;
                PRAGMA synchronous=FULL;
                PRAGMA busy_timeout=5000;

                CREATE TABLE IF NOT EXISTS graph_store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_heads (
                    graph_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS graph_snapshots (
                    graph_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    parent_revision INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(graph_id, revision),
                    UNIQUE(graph_id, signature)
                );

                CREATE TABLE IF NOT EXISTS graph_deltas (
                    delta_id TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    branch_id TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    base_signature TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    read_set_json TEXT NOT NULL,
                    write_set_json TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(graph_id, branch_id, idempotency_key)
                );

                CREATE INDEX IF NOT EXISTS idx_graph_delta_base
                    ON graph_deltas(graph_id, base_revision, created_at);

                CREATE TABLE IF NOT EXISTS graph_commits (
                    commit_id TEXT PRIMARY KEY,
                    graph_id TEXT NOT NULL,
                    delta_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    base_revision INTEGER NOT NULL,
                    committed_revision INTEGER NOT NULL,
                    snapshot_signature TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(graph_id, delta_id)
                );

                CREATE INDEX IF NOT EXISTS idx_graph_commit_revision
                    ON graph_commits(graph_id, committed_revision);

                CREATE TABLE IF NOT EXISTS graph_key_writes (
                    graph_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    delta_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    PRIMARY KEY(graph_id, revision, key)
                );

                CREATE INDEX IF NOT EXISTS idx_graph_key_write_lookup
                    ON graph_key_writes(graph_id, key, revision);

                CREATE TABLE IF NOT EXISTS graph_journal (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    graph_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    operation TEXT NOT NULL,
                    delta_id TEXT NOT NULL,
                    commit_id TEXT NOT NULL,
                    causation_id TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_graph_journal_graph
                    ON graph_journal(graph_id, sequence);
                """
            )
            connection.execute(
                """
                INSERT INTO graph_store_meta(key, value, updated_at)
                VALUES('schema_version', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                (str(self.SCHEMA_VERSION), now_iso()),
            )

    def create_graph(self, snapshot: GraphStateSnapshot) -> GraphStateSnapshot:
        if snapshot.revision != 0:
            raise ValueError("new graph must begin at revision zero")
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT signature FROM graph_heads WHERE graph_id=?",
                (snapshot.graph_id,),
            ).fetchone()
            if row is not None:
                current = self.current(snapshot.graph_id, connection=connection)
                if current.signature == snapshot.signature:
                    return current
                raise GraphStoreConflict("graph id already has a different revision-zero snapshot")
            self._insert_snapshot(connection, snapshot)
            connection.execute(
                """
                INSERT INTO graph_heads(graph_id, run_id, revision, signature, commit_id, updated_at)
                VALUES(?,?,?,?,?,?)
                """,
                (
                    snapshot.graph_id,
                    snapshot.run_id,
                    snapshot.revision,
                    snapshot.signature,
                    snapshot.commit_id,
                    now_iso(),
                ),
            )
            self._journal(
                connection,
                graph_id=snapshot.graph_id,
                run_id=snapshot.run_id,
                revision=0,
                operation="graph_created",
                payload={"signature": snapshot.signature},
            )
        return snapshot

    def save_delta(
        self,
        delta: BranchGraphDelta,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> BranchGraphDelta:
        if connection is None:
            with self.transaction() as current:
                return self.save_delta(delta, connection=current)
        row = connection.execute(
            """
            SELECT payload_json, content_digest FROM graph_deltas
            WHERE graph_id=? AND branch_id=? AND idempotency_key=?
            """,
            (delta.graph_id, delta.branch_id, delta.idempotency_key),
        ).fetchone()
        if row is not None:
            if str(row["content_digest"]) != delta.content_digest:
                raise GraphStoreConflict("graph delta idempotency key maps to different content")
            return BranchGraphDelta.from_dict(self._decode(row["payload_json"]))
        connection.execute(
            """
            INSERT INTO graph_deltas(
                delta_id, graph_id, run_id, branch_id, base_revision, base_signature,
                content_digest, read_set_json, write_set_json, causation_id,
                correlation_id, idempotency_key, actor_id, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                delta.delta_id,
                delta.graph_id,
                delta.run_id,
                delta.branch_id,
                delta.base_revision,
                delta.base_signature,
                delta.content_digest,
                canonical_json(delta.read_set),
                canonical_json(delta.write_set),
                delta.causation_id,
                delta.correlation_id,
                delta.idempotency_key,
                delta.actor_id,
                canonical_json(delta),
                delta.created_at,
            ),
        )
        self._journal(
            connection,
            graph_id=delta.graph_id,
            run_id=delta.run_id,
            revision=delta.base_revision,
            operation="graph_delta_staged",
            delta_id=delta.delta_id,
            causation_id=delta.causation_id,
            correlation_id=delta.correlation_id,
            payload={
                "branch_id": delta.branch_id,
                "read_set": list(delta.read_set),
                "write_set": list(delta.write_set),
                "content_digest": delta.content_digest,
            },
        )
        return delta

    def commit(
        self,
        *,
        expected_head_revision: int,
        snapshot: GraphStateSnapshot,
        delta: BranchGraphDelta,
        receipt: GraphCommitReceipt,
    ) -> GraphCommitReceipt:
        if not receipt.committed:
            return self.save_receipt(receipt)
        if snapshot.revision != expected_head_revision + 1:
            raise ValueError("committed snapshot must advance the graph head by exactly one revision")
        if receipt.committed_revision != snapshot.revision:
            raise ValueError("commit receipt revision differs from snapshot")
        with self.transaction() as connection:
            replay = self.commit_for_delta(delta.graph_id, delta.delta_id, connection=connection)
            if replay is not None:
                return replay
            head = self.current(delta.graph_id, connection=connection)
            if head.revision != expected_head_revision:
                raise GraphStoreConflict(
                    f"graph head advanced from {expected_head_revision} to {head.revision} before commit"
                )
            self.save_delta(delta, connection=connection)
            self._insert_snapshot(connection, snapshot)
            cursor = connection.execute(
                """
                UPDATE graph_heads SET
                    revision=?, signature=?, commit_id=?, updated_at=?
                WHERE graph_id=? AND revision=? AND signature=?
                """,
                (
                    snapshot.revision,
                    snapshot.signature,
                    receipt.commit_id,
                    now_iso(),
                    snapshot.graph_id,
                    expected_head_revision,
                    head.signature,
                ),
            )
            if cursor.rowcount != 1:
                raise GraphStoreConflict("graph head compare-and-swap failed")
            self._insert_receipt(connection, receipt)
            for key in delta.write_set:
                connection.execute(
                    """
                    INSERT INTO graph_key_writes(graph_id, revision, key, delta_id, commit_id)
                    VALUES(?,?,?,?,?)
                    """,
                    (delta.graph_id, snapshot.revision, key, delta.delta_id, receipt.commit_id),
                )
            self._journal(
                connection,
                graph_id=delta.graph_id,
                run_id=delta.run_id,
                revision=snapshot.revision,
                operation="graph_delta_committed",
                delta_id=delta.delta_id,
                commit_id=receipt.commit_id,
                causation_id=delta.causation_id,
                correlation_id=delta.correlation_id,
                payload={
                    "status": receipt.status.value,
                    "strategy": receipt.strategy.value,
                    "base_revision": delta.base_revision,
                    "revision": snapshot.revision,
                    "signature": snapshot.signature,
                    "write_set": list(delta.write_set),
                },
            )
        return receipt

    def save_receipt(self, receipt: GraphCommitReceipt) -> GraphCommitReceipt:
        with self.transaction() as connection:
            replay = self.commit_for_delta(receipt.graph_id, receipt.delta_id, connection=connection)
            if replay is not None:
                return replay
            self._insert_receipt(connection, receipt)
            current = self.current(receipt.graph_id, connection=connection)
            self._journal(
                connection,
                graph_id=receipt.graph_id,
                run_id=current.run_id,
                revision=current.revision,
                operation="graph_delta_conflicted",
                delta_id=receipt.delta_id,
                commit_id=receipt.commit_id,
                payload={
                    "status": receipt.status.value,
                    "strategy": receipt.strategy.value,
                    "conflicts": [item.to_dict() for item in receipt.conflicts],
                },
            )
        return receipt

    def current(
        self,
        graph_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> GraphStateSnapshot:
        if connection is None:
            with self._connect() as current:
                return self.current(graph_id, connection=current)
        row = connection.execute(
            """
            SELECT s.payload_json
            FROM graph_heads AS h
            JOIN graph_snapshots AS s
              ON s.graph_id=h.graph_id AND s.revision=h.revision
            WHERE h.graph_id=?
            """,
            (graph_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"graph not found: {graph_id}")
        return GraphStateSnapshot.from_dict(self._decode(row["payload_json"]))

    def snapshot(self, graph_id: str, revision: int) -> GraphStateSnapshot | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM graph_snapshots WHERE graph_id=? AND revision=?",
                (graph_id, int(revision)),
            ).fetchone()
        return None if row is None else GraphStateSnapshot.from_dict(self._decode(row["payload_json"]))

    def list_snapshots(self, graph_id: str) -> tuple[GraphStateSnapshot, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM graph_snapshots
                WHERE graph_id=? ORDER BY revision
                """,
                (graph_id,),
            ).fetchall()
        return tuple(GraphStateSnapshot.from_dict(self._decode(row["payload_json"])) for row in rows)

    def delta(self, delta_id: str) -> BranchGraphDelta | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM graph_deltas WHERE delta_id=?",
                (delta_id,),
            ).fetchone()
        return None if row is None else BranchGraphDelta.from_dict(self._decode(row["payload_json"]))

    def deltas_after(self, graph_id: str, revision: int) -> tuple[BranchGraphDelta, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT d.payload_json
                FROM graph_commits AS c
                JOIN graph_deltas AS d ON d.delta_id=c.delta_id
                WHERE c.graph_id=? AND c.committed_revision>? AND c.status IN (?,?,?)
                ORDER BY c.committed_revision
                """,
                (
                    graph_id,
                    int(revision),
                    GraphCommitStatus.COMMITTED.value,
                    GraphCommitStatus.REBASED.value,
                    GraphCommitStatus.REPLAYED.value,
                ),
            ).fetchall()
        return tuple(BranchGraphDelta.from_dict(self._decode(row["payload_json"])) for row in rows)

    def writes_after(self, graph_id: str, revision: int) -> Mapping[str, tuple[str, ...]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT key, delta_id FROM graph_key_writes
                WHERE graph_id=? AND revision>? ORDER BY revision, key
                """,
                (graph_id, int(revision)),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(str(row["key"]), []).append(str(row["delta_id"]))
        return {key: tuple(values) for key, values in result.items()}

    def commit_for_delta(
        self,
        graph_id: str,
        delta_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> GraphCommitReceipt | None:
        if connection is None:
            with self._connect() as current:
                return self.commit_for_delta(graph_id, delta_id, connection=current)
        row = connection.execute(
            "SELECT payload_json FROM graph_commits WHERE graph_id=? AND delta_id=?",
            (graph_id, delta_id),
        ).fetchone()
        return None if row is None else GraphCommitReceipt.from_dict(self._decode(row["payload_json"]))

    def journal(
        self,
        graph_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 1000,
    ) -> tuple[Mapping[str, Any], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM graph_journal
                WHERE graph_id=? AND sequence>?
                ORDER BY sequence LIMIT ?
                """,
                (graph_id, max(0, int(after_sequence)), max(1, min(10000, int(limit)))),
            ).fetchall()
        return tuple(
            {
                "sequence": int(row["sequence"]),
                "event_id": str(row["event_id"]),
                "graph_id": str(row["graph_id"]),
                "run_id": str(row["run_id"]),
                "revision": int(row["revision"]),
                "operation": str(row["operation"]),
                "delta_id": str(row["delta_id"]),
                "commit_id": str(row["commit_id"]),
                "causation_id": str(row["causation_id"]),
                "correlation_id": str(row["correlation_id"]),
                "payload": self._decode(row["payload_json"]),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        )

    def integrity_report(self, graph_id: str = "") -> Mapping[str, Any]:
        with self._connect() as connection:
            check = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            params: tuple[Any, ...] = (graph_id,) if graph_id else ()
            where = " WHERE graph_id=?" if graph_id else ""
            heads = connection.execute(f"SELECT * FROM graph_heads{where}", params).fetchall()  # noqa: S608
            issues: list[str] = []
            for head in heads:
                snapshot = connection.execute(
                    "SELECT signature, commit_id FROM graph_snapshots WHERE graph_id=? AND revision=?",
                    (head["graph_id"], head["revision"]),
                ).fetchone()
                if snapshot is None:
                    issues.append(f"head has no snapshot: {head['graph_id']}@{head['revision']}")
                elif snapshot["signature"] != head["signature"]:
                    issues.append(f"head signature mismatch: {head['graph_id']}")
            orphan_writes = connection.execute(
                """
                SELECT w.graph_id, w.revision, w.key FROM graph_key_writes AS w
                LEFT JOIN graph_commits AS c ON c.commit_id=w.commit_id
                WHERE c.commit_id IS NULL
                """
            ).fetchall()
            issues.extend(
                f"orphan write: {row['graph_id']}@{row['revision']}:{row['key']}"
                for row in orphan_writes
            )
            counts = {
                table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])  # noqa: S608
                for table in (
                    "graph_heads",
                    "graph_snapshots",
                    "graph_deltas",
                    "graph_commits",
                    "graph_key_writes",
                    "graph_journal",
                )
            }
        return {
            "ok": check == "ok" and not issues,
            "sqlite_integrity": check,
            "issues": issues,
            "counts": counts,
            "store_path": str(self.path),
        }

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
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

    def _insert_snapshot(self, connection: sqlite3.Connection, snapshot: GraphStateSnapshot) -> None:
        connection.execute(
            """
            INSERT INTO graph_snapshots(
                graph_id, run_id, revision, parent_revision, signature, commit_id,
                payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                snapshot.graph_id,
                snapshot.run_id,
                snapshot.revision,
                snapshot.parent_revision,
                snapshot.signature,
                snapshot.commit_id,
                canonical_json(snapshot),
                snapshot.created_at,
            ),
        )

    def _insert_receipt(self, connection: sqlite3.Connection, receipt: GraphCommitReceipt) -> None:
        connection.execute(
            """
            INSERT INTO graph_commits(
                commit_id, graph_id, delta_id, status, strategy, base_revision,
                committed_revision, snapshot_signature, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                receipt.commit_id,
                receipt.graph_id,
                receipt.delta_id,
                receipt.status.value,
                receipt.strategy.value,
                receipt.base_revision,
                receipt.committed_revision,
                receipt.snapshot_signature,
                canonical_json(receipt),
                receipt.created_at,
            ),
        )

    def _journal(
        self,
        connection: sqlite3.Connection,
        *,
        graph_id: str,
        run_id: str,
        revision: int,
        operation: str,
        payload: Mapping[str, Any],
        delta_id: str = "",
        commit_id: str = "",
        causation_id: str = "",
        correlation_id: str = "",
    ) -> None:
        event_id = f"graph_event_{digest((graph_id, revision, operation, delta_id, commit_id, payload))[:24]}"
        connection.execute(
            """
            INSERT INTO graph_journal(
                event_id, graph_id, run_id, revision, operation, delta_id, commit_id,
                causation_id, correlation_id, payload_json, created_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                graph_id,
                run_id,
                revision,
                operation,
                delta_id,
                commit_id,
                causation_id,
                correlation_id,
                canonical_json(payload),
                now_iso(),
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        decoded = json.loads(value)
        if not isinstance(decoded, dict):
            raise GraphStoreConflict("stored graph payload is not an object")
        return decoded
