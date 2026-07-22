from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Mapping

from .retrieval_models import (
    IndexCursor,
    IndexDocument,
    IndexHealth,
    IndexJobState,
    IndexLease,
    IndexPublication,
    IndexSourceKind,
    PublicationFencedError,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    stable_digest,
)
from .retrieval_query import fts_match_expression


SCHEMA_VERSION = 2


class SQLiteRetrievalIndex:
    """Durable derived index and generation-scoped publication store.

    Canonical memory, events, artifacts, and workspace file revisions remain in
    their existing owners.  Every table in this database can be deleted and
    rebuilt.  Publication is a single SQLite transaction that validates the
    worker lease, desired generation, and fencing token before it swaps the
    live scope.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        busy_timeout_ms: int = 10_000,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self.clock = clock or time.time
        self._initialized = False
        self._initialize_guard = threading.Lock()

    def initialize(self) -> None:
        if self._initialized and self.path.exists():
            return
        with self._initialize_guard:
            if self._initialized and self.path.exists():
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS retrieval_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS retrieval_scopes (
                        scope_key TEXT PRIMARY KEY,
                        desired_generation INTEGER NOT NULL DEFAULT 0,
                        published_generation INTEGER NOT NULL DEFAULT 0,
                        source_revision TEXT NOT NULL DEFAULT '',
                        published_revision TEXT NOT NULL DEFAULT '',
                        publication_digest TEXT NOT NULL DEFAULT '',
                        document_count INTEGER NOT NULL DEFAULT 0,
                        updated_at REAL NOT NULL,
                        published_at REAL
                    );

                    CREATE TABLE IF NOT EXISTS retrieval_staging_documents (
                        staging_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        document_id TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        layer TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        skill_name TEXT NOT NULL,
                        failure_kind TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL,
                        keywords_json TEXT NOT NULL,
                        keyword_text TEXT NOT NULL,
                        event_at TEXT NOT NULL,
                        importance REAL NOT NULL,
                        source_revision TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        content_digest TEXT NOT NULL,
                        UNIQUE(job_id, document_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_staging_job
                        ON retrieval_staging_documents(job_id, generation, staging_id);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_staging_scope
                        ON retrieval_staging_documents(scope_key, generation, document_id);

                    CREATE TABLE IF NOT EXISTS retrieval_documents (
                        row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        scope_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        document_id TEXT NOT NULL,
                        source_kind TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        task_id TEXT NOT NULL,
                        session_id TEXT NOT NULL,
                        layer TEXT NOT NULL,
                        node_id TEXT NOT NULL,
                        workspace_id TEXT NOT NULL,
                        skill_name TEXT NOT NULL,
                        failure_kind TEXT NOT NULL,
                        artifact_ids_json TEXT NOT NULL,
                        keywords_json TEXT NOT NULL,
                        keyword_text TEXT NOT NULL,
                        event_at TEXT NOT NULL,
                        importance REAL NOT NULL,
                        source_revision TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        deleted INTEGER NOT NULL DEFAULT 0,
                        content_digest TEXT NOT NULL,
                        UNIQUE(scope_key, document_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_documents_scope_generation
                        ON retrieval_documents(scope_key, generation, document_id);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_documents_task
                        ON retrieval_documents(task_id, run_id, session_id);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_documents_source
                        ON retrieval_documents(source_kind, source_id);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_documents_layer
                        ON retrieval_documents(layer, skill_name, failure_kind);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_documents_time
                        ON retrieval_documents(event_at);

                    CREATE VIRTUAL TABLE IF NOT EXISTS retrieval_fts USING fts5(
                        title,
                        body,
                        keywords,
                        tokenize='unicode61 remove_diacritics 2',
                        prefix='2 3 4'
                    );

                    CREATE TABLE IF NOT EXISTS retrieval_artifact_links (
                        scope_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        document_id TEXT NOT NULL,
                        artifact_id TEXT NOT NULL,
                        PRIMARY KEY(scope_key, document_id, artifact_id)
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_artifact_links_artifact
                        ON retrieval_artifact_links(artifact_id, scope_key, generation);

                    CREATE TABLE IF NOT EXISTS retrieval_cursors (
                        source_kind TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        scope_key TEXT NOT NULL,
                        source_revision TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        updated_at REAL NOT NULL,
                        PRIMARY KEY(source_kind, source_id, scope_key)
                    );

                    CREATE TABLE IF NOT EXISTS retrieval_publications (
                        publication_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL UNIQUE,
                        scope_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        previous_generation INTEGER NOT NULL,
                        source_revision TEXT NOT NULL,
                        content_digest TEXT NOT NULL,
                        document_count INTEGER NOT NULL,
                        published_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_publications_scope
                        ON retrieval_publications(scope_key, generation DESC);

                    CREATE TABLE IF NOT EXISTS retrieval_query_receipts (
                        query_id TEXT PRIMARY KEY,
                        scope_key TEXT NOT NULL,
                        generation INTEGER NOT NULL,
                        query_digest TEXT NOT NULL,
                        filters_digest TEXT NOT NULL,
                        candidate_count INTEGER NOT NULL,
                        returned_count INTEGER NOT NULL,
                        filtered_count INTEGER NOT NULL,
                        truncated INTEGER NOT NULL,
                        fts_used INTEGER NOT NULL,
                        vector_status TEXT NOT NULL,
                        vector_reason TEXT NOT NULL,
                        elapsed_ms REAL NOT NULL,
                        warnings_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_query_receipts_scope
                        ON retrieval_query_receipts(scope_key, generation, created_at DESC);

                    CREATE TABLE IF NOT EXISTS retrieval_audit_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL,
                        job_id TEXT NOT NULL DEFAULT '',
                        scope_key TEXT NOT NULL DEFAULT '',
                        generation INTEGER NOT NULL DEFAULT 0,
                        lease_epoch INTEGER NOT NULL DEFAULT 0,
                        worker_id TEXT NOT NULL DEFAULT '',
                        source_revision TEXT NOT NULL DEFAULT '',
                        causation_id TEXT NOT NULL DEFAULT '',
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_retrieval_audit_job
                        ON retrieval_audit_events(job_id, sequence);
                    CREATE INDEX IF NOT EXISTS idx_retrieval_audit_scope
                        ON retrieval_audit_events(scope_key, generation, sequence);
                    """
                )
                connection.execute(
                    "INSERT OR REPLACE INTO retrieval_meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO retrieval_meta(key, value) VALUES('canonical_owner', ?)",
                    ("derived_only:MemoryRecordStore+EventStore+ArtifactStore+WorkspaceFileRevision",),
                )
            self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def fts_available(self) -> bool:
        self.initialize()
        try:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='retrieval_fts'"
                ).fetchone()
            return row is not None and "fts5" in str(row["sql"]).casefold()
        except sqlite3.Error:
            return False

    def ensure_scope(self, scope_key: str) -> None:
        if not scope_key.strip():
            raise ValueError("scope_key is required")
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_scopes(scope_key, updated_at)
                VALUES (?, ?)
                ON CONFLICT(scope_key) DO NOTHING
                """,
                (scope_key, now),
            )

    def scope_state(self, scope_key: str) -> dict[str, Any]:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM retrieval_scopes WHERE scope_key = ?",
                (scope_key,),
            ).fetchone()
        if row is None:
            return {
                "scope_key": scope_key,
                "desired_generation": 0,
                "published_generation": 0,
                "source_revision": "",
                "published_revision": "",
                "publication_digest": "",
                "document_count": 0,
            }
        return dict(row)

    def stage_documents(self, lease: IndexLease, documents: Sequence[IndexDocument]) -> int:
        """Replace this job's private staging rows after a live fence check."""
        rows = [item.with_generation(lease.generation) for item in documents]
        identities = [item.document_id for item in rows]
        if len(identities) != len(set(identities)):
            raise ValueError("a staging generation cannot contain duplicate document_id values")
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            self._validate_active_lease(connection, lease, allowed_states=(IndexJobState.BUILDING,))
            connection.execute(
                "DELETE FROM retrieval_staging_documents WHERE job_id = ?",
                (lease.job_id,),
            )
            connection.executemany(
                """
                INSERT INTO retrieval_staging_documents (
                    job_id, scope_key, generation, document_id, source_kind,
                    source_id, title, body, run_id, task_id, session_id, layer,
                    node_id, workspace_id, skill_name, failure_kind,
                    artifact_ids_json, keywords_json, keyword_text, event_at,
                    importance, source_revision, metadata_json, deleted,
                    content_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._document_values(lease.job_id, item) for item in rows],
            )
            self._audit(
                connection,
                event_type="index.staged",
                lease=lease,
                payload={
                    "document_count": len(rows),
                    "content_digest": stable_digest([item.content_digest for item in rows]),
                },
                created_at=now,
            )
        return len(rows)

    def stage_document_batch(
        self,
        lease: IndexLease,
        documents: Sequence[IndexDocument],
        *,
        replace: bool = False,
    ) -> int:
        rows = [item.with_generation(lease.generation) for item in documents]
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            self._validate_active_lease(connection, lease, allowed_states=(IndexJobState.BUILDING,))
            if replace:
                connection.execute(
                    "DELETE FROM retrieval_staging_documents WHERE job_id = ?",
                    (lease.job_id,),
                )
            connection.executemany(
                """
                INSERT INTO retrieval_staging_documents (
                    job_id, scope_key, generation, document_id, source_kind,
                    source_id, title, body, run_id, task_id, session_id, layer,
                    node_id, workspace_id, skill_name, failure_kind,
                    artifact_ids_json, keywords_json, keyword_text, event_at,
                    importance, source_revision, metadata_json, deleted,
                    content_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, document_id) DO UPDATE SET
                    source_kind=excluded.source_kind,
                    source_id=excluded.source_id,
                    title=excluded.title,
                    body=excluded.body,
                    run_id=excluded.run_id,
                    task_id=excluded.task_id,
                    session_id=excluded.session_id,
                    layer=excluded.layer,
                    node_id=excluded.node_id,
                    workspace_id=excluded.workspace_id,
                    skill_name=excluded.skill_name,
                    failure_kind=excluded.failure_kind,
                    artifact_ids_json=excluded.artifact_ids_json,
                    keywords_json=excluded.keywords_json,
                    keyword_text=excluded.keyword_text,
                    event_at=excluded.event_at,
                    importance=excluded.importance,
                    source_revision=excluded.source_revision,
                    metadata_json=excluded.metadata_json,
                    deleted=excluded.deleted,
                    content_digest=excluded.content_digest
                """,
                [self._document_values(lease.job_id, item) for item in rows],
            )
            self._audit(
                connection,
                event_type="index.stage_batch",
                lease=lease,
                payload={"batch_count": len(rows), "replace": replace},
                created_at=now,
            )
        return len(rows)

    def staged_count(self, job_id: str) -> int:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM retrieval_staging_documents WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def staged_documents(self, job_id: str) -> tuple[IndexDocument, ...]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM retrieval_staging_documents
                WHERE job_id = ?
                ORDER BY document_id ASC
                """,
                (job_id,),
            ).fetchall()
        return tuple(self._row_to_document(row) for row in rows)

    def publish(self, lease: IndexLease) -> IndexPublication:
        """Fence-check and atomically replace the live scope with staged rows."""
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            job = self._validate_active_lease(
                connection,
                lease,
                allowed_states=(IndexJobState.PUBLISHING,),
            )
            scope = connection.execute(
                "SELECT * FROM retrieval_scopes WHERE scope_key = ?",
                (lease.scope_key,),
            ).fetchone()
            if scope is None:
                raise PublicationFencedError("index scope vanished before publication")
            if int(scope["desired_generation"]) != lease.generation:
                raise PublicationFencedError(
                    "publication generation is not the scope's current desired generation"
                )
            previous_generation = int(scope["published_generation"])
            staged = connection.execute(
                """
                SELECT * FROM retrieval_staging_documents
                WHERE job_id = ? AND scope_key = ? AND generation = ?
                ORDER BY document_id ASC
                """,
                (lease.job_id, lease.scope_key, lease.generation),
            ).fetchall()
            digests = [str(row["content_digest"]) for row in staged if not bool(row["deleted"])]
            publication_digest = stable_digest(lease.scope_key, lease.generation, digests)

            old_rows = connection.execute(
                "SELECT row_id FROM retrieval_documents WHERE scope_key = ?",
                (lease.scope_key,),
            ).fetchall()
            if old_rows:
                connection.executemany(
                    "DELETE FROM retrieval_fts WHERE rowid = ?",
                    [(int(row["row_id"]),) for row in old_rows],
                )
            connection.execute(
                "DELETE FROM retrieval_artifact_links WHERE scope_key = ?",
                (lease.scope_key,),
            )
            connection.execute(
                "DELETE FROM retrieval_documents WHERE scope_key = ?",
                (lease.scope_key,),
            )

            live_count = 0
            for row in staged:
                if bool(row["deleted"]):
                    continue
                cursor = connection.execute(
                    """
                    INSERT INTO retrieval_documents (
                        scope_key, generation, document_id, source_kind,
                        source_id, title, body, run_id, task_id, session_id,
                        layer, node_id, workspace_id, skill_name, failure_kind,
                        artifact_ids_json, keywords_json, keyword_text, event_at,
                        importance, source_revision, metadata_json, deleted,
                        content_digest
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["scope_key"], row["generation"], row["document_id"],
                        row["source_kind"], row["source_id"], row["title"], row["body"],
                        row["run_id"], row["task_id"], row["session_id"], row["layer"],
                        row["node_id"], row["workspace_id"], row["skill_name"],
                        row["failure_kind"], row["artifact_ids_json"], row["keywords_json"],
                        row["keyword_text"], row["event_at"], row["importance"],
                        row["source_revision"], row["metadata_json"], row["deleted"],
                        row["content_digest"],
                    ),
                )
                row_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO retrieval_fts(rowid, title, body, keywords) VALUES (?, ?, ?, ?)",
                    (row_id, row["title"], row["body"], row["keyword_text"]),
                )
                artifact_ids = json.loads(str(row["artifact_ids_json"]))
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO retrieval_artifact_links(
                        scope_key, generation, document_id, artifact_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (lease.scope_key, lease.generation, row["document_id"], str(artifact_id))
                        for artifact_id in artifact_ids
                        if str(artifact_id)
                    ],
                )
                live_count += 1

            source_revision = str(job["source_revision"])
            connection.execute(
                """
                UPDATE retrieval_scopes
                SET published_generation = ?, published_revision = ?,
                    publication_digest = ?, document_count = ?,
                    updated_at = ?, published_at = ?
                WHERE scope_key = ? AND desired_generation = ?
                """,
                (
                    lease.generation,
                    source_revision,
                    publication_digest,
                    live_count,
                    now,
                    now,
                    lease.scope_key,
                    lease.generation,
                ),
            )
            connection.execute(
                """
                INSERT INTO retrieval_publications(
                    job_id, scope_key, generation, previous_generation,
                    source_revision, content_digest, document_count, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lease.job_id,
                    lease.scope_key,
                    lease.generation,
                    previous_generation,
                    source_revision,
                    publication_digest,
                    live_count,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE index_jobs
                SET state = ?, published_at = ?, updated_at = ?,
                    lease_owner = '', lease_token = '', lease_expires_at = NULL
                WHERE job_id = ? AND state = ? AND generation = ?
                    AND lease_epoch = ? AND lease_token = ?
                """,
                (
                    IndexJobState.READY.value,
                    now,
                    now,
                    lease.job_id,
                    IndexJobState.PUBLISHING.value,
                    lease.generation,
                    lease.epoch,
                    lease.token,
                ),
            )
            if connection.execute("SELECT changes() AS count").fetchone()["count"] != 1:
                raise PublicationFencedError("lease changed during publication")
            connection.execute(
                "DELETE FROM retrieval_staging_documents WHERE job_id = ?",
                (lease.job_id,),
            )
            self._audit(
                connection,
                event_type="index.ready",
                lease=lease,
                source_revision=source_revision,
                payload={
                    "previous_generation": previous_generation,
                    "document_count": live_count,
                    "publication_digest": publication_digest,
                },
                created_at=now,
            )
        return IndexPublication(
            job_id=lease.job_id,
            scope_key=lease.scope_key,
            generation=lease.generation,
            document_count=live_count,
            content_digest=publication_digest,
            published_at=self._timestamp_iso(now),
            previous_generation=previous_generation,
            source_revision=source_revision,
        )

    def search_fts(self, query: RetrievalQuery, *, scope_key: str = "") -> tuple[RetrievalHit, ...]:
        enriched = query.validated()
        expression = fts_match_expression(enriched.text, match_all=False, prefix=False)
        if not expression:
            return ()
        where: list[str] = ["retrieval_fts MATCH ?"]
        params: list[Any] = [expression]
        filters = enriched.filters
        if scope_key:
            where.append("d.scope_key = ?")
            params.append(scope_key)
        self._append_filter(where, params, "d.run_id", filters.run_ids)
        self._append_filter(where, params, "d.task_id", filters.task_ids)
        self._append_filter(where, params, "d.session_id", filters.session_ids)
        self._append_filter(where, params, "d.source_kind", filters.source_types)
        self._append_filter(where, params, "d.source_id", filters.source_ids)
        self._append_filter(where, params, "d.skill_name", filters.skill_names)
        self._append_filter(where, params, "d.failure_kind", filters.failure_kinds)
        self._append_filter(where, params, "d.layer", tuple(str(item) for item in filters.layers))
        self._append_filter(where, params, "d.node_id", filters.node_ids)
        self._append_filter(where, params, "d.workspace_id", filters.workspace_ids)
        if not filters.include_deleted:
            where.append("d.deleted = 0")
        if filters.temporal.start_at:
            where.append("d.event_at >= ?")
            params.append(filters.temporal.start_at)
        if filters.temporal.end_at:
            where.append("d.event_at < ?")
            params.append(filters.temporal.end_at)
        if filters.artifact_ids:
            placeholders = ",".join("?" for _ in filters.artifact_ids)
            where.append(
                "EXISTS (SELECT 1 FROM retrieval_artifact_links a "
                "WHERE a.scope_key = d.scope_key AND a.document_id = d.document_id "
                f"AND a.artifact_id IN ({placeholders}))"
            )
            params.extend(filters.artifact_ids)
        params.append(enriched.budget.candidate_limit)
        sql = f"""
            SELECT d.*, bm25(retrieval_fts, 2.0, 1.0, 1.4) AS lexical_rank,
                   snippet(retrieval_fts, 1, '[', ']', ' … ', 32) AS match_snippet
            FROM retrieval_fts
            JOIN retrieval_documents d ON d.row_id = retrieval_fts.rowid
            WHERE {' AND '.join(where)}
            ORDER BY lexical_rank ASC, d.document_id ASC
            LIMIT ?
        """
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(sql, params).fetchall()
        return tuple(self._row_to_hit(row) for row in rows)

    def list_scope_documents(self, scope_key: str) -> tuple[RetrievalHit, ...]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT d.*, 0.0 AS lexical_rank, '' AS match_snippet
                FROM retrieval_documents d
                WHERE d.scope_key = ? AND d.deleted = 0
                ORDER BY d.document_id ASC
                """,
                (scope_key,),
            ).fetchall()
        return tuple(self._row_to_hit(row) for row in rows)

    def put_cursor(self, cursor: IndexCursor) -> None:
        now = self.clock()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_cursors(
                    source_kind, source_id, scope_key, source_revision,
                    content_digest, generation, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_kind, source_id, scope_key) DO UPDATE SET
                    source_revision=excluded.source_revision,
                    content_digest=excluded.content_digest,
                    generation=excluded.generation,
                    updated_at=excluded.updated_at
                """,
                (
                    cursor.source_kind.value,
                    cursor.source_id,
                    cursor.scope_key,
                    cursor.source_revision,
                    cursor.content_digest,
                    cursor.generation,
                    now,
                ),
            )

    def get_cursor(
        self,
        source_kind: IndexSourceKind,
        source_id: str,
        scope_key: str,
    ) -> IndexCursor | None:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM retrieval_cursors
                WHERE source_kind = ? AND source_id = ? AND scope_key = ?
                """,
                (source_kind.value, source_id, scope_key),
            ).fetchone()
        if row is None:
            return None
        return IndexCursor(
            source_kind=IndexSourceKind(str(row["source_kind"])),
            source_id=str(row["source_id"]),
            source_revision=str(row["source_revision"]),
            content_digest=str(row["content_digest"]),
            scope_key=str(row["scope_key"]),
            generation=int(row["generation"]),
            updated_at=self._timestamp_iso(float(row["updated_at"])),
        )

    def audit_events(self, *, job_id: str = "", scope_key: str = "") -> tuple[dict[str, Any], ...]:
        self.initialize()
        where: list[str] = []
        params: list[Any] = []
        if job_id:
            where.append("job_id = ?")
            params.append(job_id)
        if scope_key:
            where.append("scope_key = ?")
            params.append(scope_key)
        condition = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM retrieval_audit_events {condition} ORDER BY sequence ASC",
                params,
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "payload": json.loads(str(row["payload_json"])),
                "created_at_iso": self._timestamp_iso(float(row["created_at"])),
            }
            for row in rows
        )

    def record_query(self, result: RetrievalResult) -> None:
        """Persist a privacy-minimized receipt for one actual retrieval.

        Raw query text and returned document content are deliberately omitted.
        The receipt is derived observability state and is removed by the same
        clean-rebuild operation as documents and worker audit events.
        """

        self.initialize()
        diagnostics = result.diagnostics
        if not diagnostics.index_scope:
            raise ValueError("retrieval diagnostics require an index scope")
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO retrieval_query_receipts (
                    query_id, scope_key, generation, query_digest, filters_digest,
                    candidate_count, returned_count, filtered_count, truncated,
                    fts_used, vector_status, vector_reason, elapsed_ms,
                    warnings_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    scope_key = excluded.scope_key,
                    generation = excluded.generation,
                    query_digest = excluded.query_digest,
                    filters_digest = excluded.filters_digest,
                    candidate_count = excluded.candidate_count,
                    returned_count = excluded.returned_count,
                    filtered_count = excluded.filtered_count,
                    truncated = excluded.truncated,
                    fts_used = excluded.fts_used,
                    vector_status = excluded.vector_status,
                    vector_reason = excluded.vector_reason,
                    elapsed_ms = excluded.elapsed_ms,
                    warnings_json = excluded.warnings_json,
                    created_at = excluded.created_at
                """,
                (
                    diagnostics.query_id,
                    diagnostics.index_scope,
                    diagnostics.index_generation,
                    stable_digest(result.query.text),
                    stable_digest(result.query.filters.to_dict()),
                    diagnostics.candidate_count,
                    diagnostics.returned_count,
                    diagnostics.filtered_count,
                    int(diagnostics.truncated),
                    int(diagnostics.fts_used),
                    diagnostics.vector_status.value,
                    diagnostics.vector_reason,
                    diagnostics.elapsed_ms,
                    json.dumps(list(diagnostics.warnings), ensure_ascii=False, sort_keys=True),
                    self.clock(),
                ),
            )

    def query_receipts(
        self,
        *,
        scope_key: str = "",
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        self.initialize()
        bounded_limit = max(0, min(int(limit), 10_000))
        where = "WHERE scope_key = ?" if scope_key else ""
        params: list[Any] = [scope_key] if scope_key else []
        params.append(bounded_limit)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT query_id, scope_key, generation, query_digest, filters_digest,
                       candidate_count, returned_count, filtered_count, truncated,
                       fts_used, vector_status, vector_reason, elapsed_ms,
                       warnings_json, created_at
                FROM retrieval_query_receipts
                {where}
                ORDER BY created_at DESC, query_id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "truncated": bool(row["truncated"]),
                "fts_used": bool(row["fts_used"]),
                "warnings": json.loads(str(row["warnings_json"])),
                "created_at_iso": self._timestamp_iso(float(row["created_at"])),
                "raw_query_persisted": False,
                "result_content_persisted": False,
            }
            for row in rows
        )

    def health(self) -> IndexHealth:
        self.initialize()
        states = {state.value: 0 for state in IndexJobState}
        with self.connection() as connection:
            if self._table_exists(connection, "index_jobs"):
                for row in connection.execute(
                    "SELECT state, COUNT(*) AS count FROM index_jobs GROUP BY state"
                ).fetchall():
                    states[str(row["state"])] = int(row["count"])
                lease_row = connection.execute(
                    "SELECT MIN(lease_expires_at) AS oldest FROM index_jobs WHERE lease_expires_at IS NOT NULL"
                ).fetchone()
                oldest = self._timestamp_iso(float(lease_row["oldest"])) if lease_row and lease_row["oldest"] else ""
            else:
                oldest = ""
            live = int(connection.execute("SELECT COUNT(*) AS count FROM retrieval_documents").fetchone()["count"])
            staged = int(
                connection.execute("SELECT COUNT(*) AS count FROM retrieval_staging_documents").fetchone()["count"]
            )
        warnings: list[str] = []
        if not self.fts_available():
            warnings.append("fts5_unavailable")
        return IndexHealth(
            database_path=str(self.path),
            fts_available=self.fts_available(),
            queued=states[IndexJobState.QUEUED.value],
            leased=states[IndexJobState.LEASED.value],
            building=states[IndexJobState.BUILDING.value],
            publishing=states[IndexJobState.PUBLISHING.value],
            ready=states[IndexJobState.READY.value],
            failed=states[IndexJobState.FAILED.value],
            stale=states[IndexJobState.STALE.value],
            live_documents=live,
            staged_documents=staged,
            oldest_lease_expiry=oldest,
            warnings=tuple(warnings),
        )

    def drop_derived_state(self) -> None:
        """Remove all rebuildable data while leaving schema and canonical stores untouched."""
        with self.transaction(immediate=True) as connection:
            connection.execute("DELETE FROM retrieval_fts")
            for table in (
                "retrieval_documents",
                "retrieval_artifact_links",
                "retrieval_staging_documents",
                "retrieval_cursors",
                "retrieval_publications",
                "retrieval_query_receipts",
                "retrieval_audit_events",
                "retrieval_scopes",
            ):
                connection.execute(f"DELETE FROM {table}")
            if self._table_exists(connection, "index_jobs"):
                connection.execute("DELETE FROM index_jobs")

    def remove_database_files(self) -> None:
        """Test/maintenance helper for a known index path, never for canonical state."""
        resolved = self.path.resolve()
        if resolved.is_dir() or resolved.name in {"", ".", ".."}:
            raise ValueError("refusing to remove a broad or directory index path")
        for suffix in ("", "-wal", "-shm"):
            target = Path(f"{resolved}{suffix}")
            try:
                target.unlink()
            except FileNotFoundError:
                pass
        self._initialized = False

    def database_identity(self) -> Mapping[str, Any]:
        self.initialize()
        stat = self.path.stat()
        return {
            "path": str(self.path.resolve()),
            "size_bytes": stat.st_size,
            "schema_version": SCHEMA_VERSION,
            "fts5": self.fts_available(),
            "pid": os.getpid(),
            "canonical": False,
            "rebuildable": True,
        }

    @staticmethod
    def _append_filter(where: list[str], params: list[Any], column: str, values: Sequence[str]) -> None:
        if not values:
            return
        placeholders = ",".join("?" for _ in values)
        where.append(f"{column} IN ({placeholders})")
        params.extend(values)

    @staticmethod
    def _document_values(job_id: str, item: IndexDocument) -> tuple[Any, ...]:
        return (
            job_id,
            item.scope_key,
            item.generation,
            item.document_id,
            item.source_kind.value,
            item.source_id,
            item.title,
            item.body,
            item.run_id,
            item.task_id,
            item.session_id,
            item.layer,
            item.node_id,
            item.workspace_id,
            item.skill_name,
            item.failure_kind,
            json.dumps(list(item.artifact_ids), ensure_ascii=False, sort_keys=True),
            json.dumps(list(item.keywords), ensure_ascii=False, sort_keys=True),
            item.keyword_text,
            item.event_at,
            item.importance,
            item.source_revision,
            json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True, default=str),
            int(item.deleted),
            item.content_digest,
        )

    @staticmethod
    def _row_to_document(row: sqlite3.Row) -> IndexDocument:
        return IndexDocument(
            document_id=str(row["document_id"]),
            scope_key=str(row["scope_key"]),
            generation=int(row["generation"]),
            source_kind=IndexSourceKind(str(row["source_kind"])),
            source_id=str(row["source_id"]),
            title=str(row["title"]),
            body=str(row["body"]),
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            layer=str(row["layer"]),
            node_id=str(row["node_id"]),
            workspace_id=str(row["workspace_id"]),
            skill_name=str(row["skill_name"]),
            failure_kind=str(row["failure_kind"]),
            artifact_ids=tuple(str(item) for item in json.loads(str(row["artifact_ids_json"]))),
            keywords=tuple(str(item) for item in json.loads(str(row["keywords_json"]))),
            event_at=str(row["event_at"]),
            importance=float(row["importance"]),
            source_revision=str(row["source_revision"]),
            metadata=json.loads(str(row["metadata_json"])),
            deleted=bool(row["deleted"]),
        )

    @staticmethod
    def _row_to_hit(row: sqlite3.Row) -> RetrievalHit:
        raw_rank = float(row["lexical_rank"])
        lexical_score = max(1e-12, -raw_rank) if raw_rank < 0 else 1.0 / (1.0 + raw_rank)
        metadata = json.loads(str(row["metadata_json"]))
        snippet = str(row["match_snippet"] or "")
        if snippet:
            metadata = {**metadata, "match_snippet": snippet, "fts_rank": raw_rank}
        return RetrievalHit(
            document_id=str(row["document_id"]),
            source_kind=IndexSourceKind(str(row["source_kind"])),
            source_id=str(row["source_id"]),
            title=str(row["title"]),
            content=str(row["body"]),
            score=lexical_score,
            run_id=str(row["run_id"]),
            task_id=str(row["task_id"]),
            session_id=str(row["session_id"]),
            layer=str(row["layer"]),
            node_id=str(row["node_id"]),
            workspace_id=str(row["workspace_id"]),
            skill_name=str(row["skill_name"]),
            failure_kind=str(row["failure_kind"]),
            artifact_ids=tuple(str(item) for item in json.loads(str(row["artifact_ids_json"]))),
            keywords=tuple(str(item) for item in json.loads(str(row["keywords_json"]))),
            event_at=str(row["event_at"]),
            importance=float(row["importance"]),
            generation=int(row["generation"]),
            content_digest=str(row["content_digest"]),
            metadata=metadata,
        )

    def _validate_active_lease(
        self,
        connection: sqlite3.Connection,
        lease: IndexLease,
        *,
        allowed_states: tuple[IndexJobState, ...],
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM index_jobs WHERE job_id = ?",
            (lease.job_id,),
        ).fetchone()
        if row is None:
            raise PublicationFencedError("index job no longer exists")
        if str(row["scope_key"]) != lease.scope_key:
            raise PublicationFencedError("lease scope does not match job")
        if int(row["generation"]) != lease.generation:
            raise PublicationFencedError("lease generation does not match job")
        if int(row["lease_epoch"]) != lease.epoch:
            raise PublicationFencedError("lease epoch is stale")
        if str(row["lease_token"]) != lease.token:
            raise PublicationFencedError("lease token is stale")
        if str(row["lease_owner"]) != lease.worker_id:
            raise PublicationFencedError("lease owner is stale")
        if IndexJobState(str(row["state"])) not in allowed_states:
            raise PublicationFencedError("job is not in a publishable state")
        expires = row["lease_expires_at"]
        if expires is None or float(expires) <= self.clock():
            raise PublicationFencedError("lease expired before derived-state mutation")
        return row

    def _audit(
        self,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        lease: IndexLease | None = None,
        job_id: str = "",
        scope_key: str = "",
        generation: int = 0,
        lease_epoch: int = 0,
        worker_id: str = "",
        source_revision: str = "",
        causation_id: str = "",
        payload: Mapping[str, Any] | None = None,
        created_at: float | None = None,
    ) -> None:
        if lease is not None:
            job_id = lease.job_id
            scope_key = lease.scope_key
            generation = lease.generation
            lease_epoch = lease.epoch
            worker_id = lease.worker_id
        connection.execute(
            """
            INSERT INTO retrieval_audit_events(
                event_type, job_id, scope_key, generation, lease_epoch,
                worker_id, source_revision, causation_id, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_type,
                job_id,
                scope_key,
                generation,
                lease_epoch,
                worker_id,
                source_revision,
                causation_id,
                json.dumps(dict(payload or {}), ensure_ascii=False, sort_keys=True, default=str),
                self.clock() if created_at is None else created_at,
            ),
        )

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None

    @staticmethod
    def _timestamp_iso(value: float) -> str:
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value, tz=UTC).isoformat()
