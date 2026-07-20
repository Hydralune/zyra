from __future__ import annotations

import fnmatch
import json
import re
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from .models import (
    CallEdge,
    CodeFile,
    CodeSymbol,
    ContentMatch,
    ContentSearchMode,
    ContentSearchQuery,
    ContentSearchResult,
    SourceLocation,
    SymbolCapability,
    SymbolKind,
    SymbolProvenance,
    SymbolQuery,
    SymbolQueryResult,
    SymbolReference,
    WorkspaceIdentity,
)


class CodeIndexStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 10_000) -> None:
        self.path = Path(path)
        self.busy_timeout_ms = max(1, int(busy_timeout_ms))
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized and self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction(immediate=True, initialize=False) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS code_index_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    owner_epoch INTEGER NOT NULL,
                    binding_revision INTEGER NOT NULL,
                    workspace_revision TEXT NOT NULL,
                    desired_generation INTEGER NOT NULL,
                    active_generation INTEGER NOT NULL,
                    content_digest TEXT NOT NULL,
                    file_count INTEGER NOT NULL,
                    symbol_count INTEGER NOT NULL,
                    reference_count INTEGER NOT NULL,
                    call_edge_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS code_index_files (
                    file_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    logical_path TEXT NOT NULL,
                    language TEXT NOT NULL,
                    suffix TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    line_count INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    UNIQUE(workspace_id, generation, logical_path)
                );

                CREATE INDEX IF NOT EXISTS idx_code_files_workspace_generation
                    ON code_index_files(workspace_id, generation, logical_path);
                CREATE INDEX IF NOT EXISTS idx_code_files_language
                    ON code_index_files(workspace_id, generation, language, suffix);

                CREATE VIRTUAL TABLE IF NOT EXISTS code_index_fts USING fts5(
                    logical_path,
                    content,
                    tokenize='unicode61 remove_diacritics 2',
                    prefix='2 3 4'
                );

                CREATE TABLE IF NOT EXISTS code_index_symbols (
                    symbol_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    qualified_name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    language TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    column_start INTEGER NOT NULL,
                    column_end INTEGER NOT NULL,
                    signature TEXT NOT NULL,
                    documentation TEXT NOT NULL,
                    container_name TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_code_symbols_name
                    ON code_index_symbols(workspace_id, generation, name, qualified_name);
                CREATE INDEX IF NOT EXISTS idx_code_symbols_path
                    ON code_index_symbols(workspace_id, generation, logical_path, line_start);

                CREATE TABLE IF NOT EXISTS code_index_references (
                    reference_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    symbol_name TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    column_start INTEGER NOT NULL,
                    column_end INTEGER NOT NULL,
                    reference_kind TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    resolved_symbol_id TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    metadata_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_code_references_name
                    ON code_index_references(workspace_id, generation, symbol_name, logical_path);
                CREATE INDEX IF NOT EXISTS idx_code_references_resolved
                    ON code_index_references(workspace_id, generation, resolved_symbol_id);

                CREATE TABLE IF NOT EXISTS code_index_calls (
                    edge_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    caller_symbol_id TEXT NOT NULL,
                    callee_name TEXT NOT NULL,
                    resolved_callee_symbol_id TEXT NOT NULL,
                    logical_path TEXT NOT NULL,
                    line_start INTEGER NOT NULL,
                    line_end INTEGER NOT NULL,
                    column_start INTEGER NOT NULL,
                    column_end INTEGER NOT NULL,
                    provenance TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_code_calls_caller
                    ON code_index_calls(workspace_id, generation, caller_symbol_id);
                CREATE INDEX IF NOT EXISTS idx_code_calls_callee
                    ON code_index_calls(workspace_id, generation, callee_name, resolved_callee_symbol_id);

                CREATE TABLE IF NOT EXISTS code_index_invalidations (
                    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    workspace_id TEXT NOT NULL,
                    workspace_revision TEXT NOT NULL,
                    transaction_id TEXT NOT NULL,
                    changed_paths_json TEXT NOT NULL,
                    deleted_paths_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    applied_generation INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(workspace_id, transaction_id)
                );
                """
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
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(
        self,
        *,
        immediate: bool = False,
        initialize: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        if initialize:
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

    def next_generation(self, identity: WorkspaceIdentity) -> int:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT desired_generation FROM code_index_workspaces WHERE workspace_id = ?",
                (identity.workspace_id,),
            ).fetchone()
        return int(row["desired_generation"] if row else 0) + 1

    def publish(
        self,
        identity: WorkspaceIdentity,
        generation: int,
        files: Sequence[tuple[CodeFile, str]],
        symbols: Sequence[CodeSymbol],
        references: Sequence[SymbolReference],
        calls: Sequence[CallEdge],
        *,
        content_digest: str,
    ) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        if any(file.workspace_id != identity.workspace_id for file, _ in files):
            raise ValueError("file batch contains another workspace")
        now = time.time()
        with self.transaction(immediate=True) as connection:
            old_files = connection.execute(
                "SELECT rowid FROM code_index_files WHERE workspace_id = ?",
                (identity.workspace_id,),
            ).fetchall()
            connection.executemany(
                "DELETE FROM code_index_fts WHERE rowid = ?",
                [(int(row["rowid"]),) for row in old_files],
            )
            for table in (
                "code_index_calls",
                "code_index_references",
                "code_index_symbols",
                "code_index_files",
            ):
                connection.execute(f"DELETE FROM {table} WHERE workspace_id = ?", (identity.workspace_id,))
            for file, content in sorted(files, key=lambda item: item[0].logical_path):
                cursor = connection.execute(
                    """
                    INSERT INTO code_index_files(
                        file_id, workspace_id, generation, logical_path, language,
                        suffix, size_bytes, mtime_ns, content_hash, source_revision,
                        line_count, content, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        file.file_id,
                        file.workspace_id,
                        generation,
                        file.logical_path,
                        file.language,
                        file.suffix,
                        file.size_bytes,
                        file.mtime_ns,
                        file.content_hash,
                        file.source_revision,
                        file.line_count,
                        content,
                        json.dumps(dict(file.metadata), ensure_ascii=False, sort_keys=True, default=str),
                    ),
                )
                connection.execute(
                    "INSERT INTO code_index_fts(rowid, logical_path, content) VALUES (?, ?, ?)",
                    (int(cursor.lastrowid), file.logical_path, content),
                )
            connection.executemany(
                """
                INSERT INTO code_index_symbols(
                    symbol_id, workspace_id, generation, name, qualified_name,
                    kind, language, logical_path, line_start, line_end,
                    column_start, column_end, signature, documentation,
                    container_name, file_hash, source_revision, provenance,
                    metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._symbol_values(item, generation) for item in symbols],
            )
            connection.executemany(
                """
                INSERT INTO code_index_references(
                    reference_id, workspace_id, generation, symbol_name,
                    logical_path, line_start, line_end, column_start, column_end,
                    reference_kind, file_hash, source_revision,
                    resolved_symbol_id, provenance, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._reference_values(item, generation) for item in references],
            )
            connection.executemany(
                """
                INSERT INTO code_index_calls(
                    edge_id, workspace_id, generation, caller_symbol_id,
                    callee_name, resolved_callee_symbol_id, logical_path,
                    line_start, line_end, column_start, column_end, provenance
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [self._call_values(identity.workspace_id, item, generation) for item in calls],
            )
            connection.execute(
                """
                INSERT INTO code_index_workspaces(
                    workspace_id, task_id, run_id, session_id, owner_epoch,
                    binding_revision, workspace_revision, desired_generation,
                    active_generation, content_digest, file_count, symbol_count,
                    reference_count, call_edge_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    session_id=excluded.session_id,
                    owner_epoch=excluded.owner_epoch,
                    binding_revision=excluded.binding_revision,
                    workspace_revision=excluded.workspace_revision,
                    desired_generation=excluded.desired_generation,
                    active_generation=excluded.active_generation,
                    content_digest=excluded.content_digest,
                    file_count=excluded.file_count,
                    symbol_count=excluded.symbol_count,
                    reference_count=excluded.reference_count,
                    call_edge_count=excluded.call_edge_count,
                    updated_at=excluded.updated_at
                """,
                (
                    identity.workspace_id,
                    identity.task_id,
                    identity.run_id,
                    identity.session_id,
                    identity.owner_epoch,
                    identity.binding_revision,
                    identity.revision,
                    generation,
                    generation,
                    content_digest,
                    len(files),
                    len(symbols),
                    len(references),
                    len(calls),
                    now,
                ),
            )

    def workspace_state(self, workspace_id: str) -> Mapping[str, Any]:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM code_index_workspaces WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
        return {} if row is None else dict(row)

    def files(self, workspace_id: str) -> tuple[CodeFile, ...]:
        state = self.workspace_state(workspace_id)
        if not state:
            return ()
        generation = int(state["active_generation"])
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM code_index_files
                WHERE workspace_id = ? AND generation = ?
                ORDER BY logical_path ASC
                """,
                (workspace_id, generation),
            ).fetchall()
        return tuple(self._row_to_file(row) for row in rows)

    def file_content(self, workspace_id: str, logical_path: str) -> tuple[CodeFile, str] | None:
        state = self.workspace_state(workspace_id)
        if not state:
            return None
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM code_index_files
                WHERE workspace_id = ? AND generation = ? AND logical_path = ?
                """,
                (workspace_id, state["active_generation"], logical_path),
            ).fetchone()
        return None if row is None else (self._row_to_file(row), str(row["content"]))

    def search_content(self, identity: WorkspaceIdentity, query: ContentSearchQuery) -> ContentSearchResult:
        request = query.validated()
        state = self.workspace_state(identity.workspace_id)
        generation = int(state.get("active_generation") or 0)
        if not generation:
            return ContentSearchResult(
                query=request,
                matches=(),
                candidate_files=0,
                scanned_files=0,
                scanned_bytes=0,
                elapsed_ms=0.0,
                truncated=False,
                next_offset=0,
                generation=0,
                workspace_revision=identity.revision,
                warnings=("code_index_not_ready",),
            )
        started = time.monotonic()
        where = ["workspace_id = ?", "generation = ?"]
        params: list[Any] = [identity.workspace_id, generation]
        if request.languages:
            placeholders = ",".join("?" for _ in request.languages)
            where.append(f"language IN ({placeholders})")
            params.extend(request.languages)
        if request.suffixes:
            placeholders = ",".join("?" for _ in request.suffixes)
            where.append(f"suffix IN ({placeholders})")
            params.extend(item.casefold() for item in request.suffixes)
        params.append(request.budget.maximum_candidate_files)
        with self.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM code_index_files
                WHERE {' AND '.join(where)}
                ORDER BY logical_path ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        filtered = [row for row in rows if self._path_filter(str(row["logical_path"]), request)]
        matcher = self._compile_matcher(request)
        matches: list[ContentMatch] = []
        scanned_files = 0
        scanned_bytes = 0
        output_chars = 0
        truncated = False
        skipped = 0
        warnings: list[str] = []
        for row in filtered:
            if (time.monotonic() - started) * 1000.0 > request.budget.deadline_ms:
                warnings.append("search_deadline_reached")
                truncated = True
                break
            content = str(row["content"])
            encoded_size = len(content.encode("utf-8"))
            if scanned_bytes + encoded_size > request.budget.maximum_scanned_bytes:
                warnings.append("search_byte_budget_reached")
                truncated = True
                break
            scanned_files += 1
            scanned_bytes += encoded_size
            file_matches = self._matches_in_file(row, content, matcher, request)
            for match in file_matches:
                if skipped < request.budget.offset:
                    skipped += 1
                    continue
                projected = len(match.preview) + len(match.matched_text)
                if output_chars + projected > request.budget.maximum_output_chars:
                    truncated = True
                    warnings.append("search_output_budget_reached")
                    break
                matches.append(match)
                output_chars += projected
                effective_limit = request.budget.head_limit or request.budget.maximum_results
                effective_limit = min(effective_limit, request.budget.maximum_results)
                if len(matches) >= effective_limit:
                    truncated = True
                    break
            if truncated and (
                len(matches) >= min(
                    request.budget.head_limit or request.budget.maximum_results,
                    request.budget.maximum_results,
                )
                or output_chars >= request.budget.maximum_output_chars
            ):
                break
        return ContentSearchResult(
            query=request,
            matches=tuple(matches),
            candidate_files=len(filtered),
            scanned_files=scanned_files,
            scanned_bytes=scanned_bytes,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            truncated=truncated,
            next_offset=request.budget.offset + skipped + len(matches) if truncated else 0,
            generation=generation,
            workspace_revision=str(state.get("workspace_revision") or identity.revision),
            warnings=tuple(warnings),
        )

    def query_symbols(self, identity: WorkspaceIdentity, query: SymbolQuery) -> SymbolQueryResult:
        request = query.validated()
        state = self.workspace_state(identity.workspace_id)
        generation = int(state.get("active_generation") or 0)
        revision = str(state.get("workspace_revision") or identity.revision)
        if not generation:
            return SymbolQueryResult(
                query=request,
                degraded=True,
                degradation_reason="code_index_not_ready",
                generation=0,
                workspace_revision=revision,
            )
        symbols: tuple[CodeSymbol, ...] = ()
        references: tuple[SymbolReference, ...] = ()
        calls: tuple[CallEdge, ...] = ()
        hover: Mapping[str, Any] = {}
        provenance = SymbolProvenance.PYTHON_AST
        degraded = False
        reason = ""
        with self.connection() as connection:
            if request.capability is SymbolCapability.DOCUMENT_SYMBOL:
                rows = connection.execute(
                    """
                    SELECT * FROM code_index_symbols
                    WHERE workspace_id = ? AND generation = ? AND logical_path = ?
                    ORDER BY line_start, column_start, qualified_name LIMIT ?
                    """,
                    (identity.workspace_id, generation, request.logical_path, request.limit),
                ).fetchall()
                symbols = tuple(self._row_to_symbol(row) for row in rows)
            elif request.capability in {SymbolCapability.WORKSPACE_SYMBOL, SymbolCapability.DEFINITION}:
                if request.capability is SymbolCapability.WORKSPACE_SYMBOL:
                    predicate = "(name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')"
                    value = f"%{self._escape_like(request.name)}%"
                else:
                    predicate = "(name = ? OR qualified_name = ?)"
                    value = request.name
                rows = connection.execute(
                    f"""
                    SELECT * FROM code_index_symbols
                    WHERE workspace_id = ? AND generation = ? AND {predicate}
                    ORDER BY CASE WHEN name = ? THEN 0 ELSE 1 END, qualified_name, logical_path
                    LIMIT ?
                    """,
                    (identity.workspace_id, generation, value, value, request.name, request.limit),
                ).fetchall()
                symbols = tuple(self._row_to_symbol(row) for row in rows)
            elif request.capability is SymbolCapability.REFERENCES:
                rows = connection.execute(
                    """
                    SELECT * FROM code_index_references
                    WHERE workspace_id = ? AND generation = ?
                        AND (symbol_name = ? OR resolved_symbol_id = ?)
                    ORDER BY logical_path, line_start, column_start LIMIT ?
                    """,
                    (identity.workspace_id, generation, request.name, request.symbol_id, request.limit),
                ).fetchall()
                references = tuple(self._row_to_reference(row) for row in rows)
            elif request.capability is SymbolCapability.IMPLEMENTATION:
                rows = connection.execute(
                    """
                    SELECT * FROM code_index_symbols
                    WHERE workspace_id = ? AND generation = ? AND name = ?
                        AND kind IN ('class', 'method', 'function')
                    ORDER BY logical_path, line_start LIMIT ?
                    """,
                    (identity.workspace_id, generation, request.name, request.limit),
                ).fetchall()
                symbols = tuple(self._row_to_symbol(row) for row in rows)
                degraded = any(item.provenance is SymbolProvenance.STRUCTURAL_FALLBACK for item in symbols)
                reason = "fallback implementations are name-based, not type-checked" if degraded else ""
            elif request.capability is SymbolCapability.HOVER:
                row = connection.execute(
                    """
                    SELECT * FROM code_index_symbols
                    WHERE workspace_id = ? AND generation = ? AND logical_path = ?
                        AND line_start <= ? AND line_end >= ?
                    ORDER BY (line_end-line_start) ASC, column_start DESC LIMIT 1
                    """,
                    (identity.workspace_id, generation, request.logical_path, request.line, request.line),
                ).fetchone()
                if row is not None:
                    symbol = self._row_to_symbol(row)
                    symbols = (symbol,)
                    hover = {
                        "name": symbol.qualified_name,
                        "signature": symbol.signature,
                        "documentation": symbol.documentation,
                        "location": symbol.location.to_dict(),
                        "precision": "ast" if symbol.provenance is SymbolProvenance.PYTHON_AST else "structural",
                    }
                    degraded = symbol.provenance is SymbolProvenance.STRUCTURAL_FALLBACK
                    reason = "LSP unavailable; hover is derived from parser output" if degraded else ""
            elif request.capability is SymbolCapability.CALL_HIERARCHY:
                rows = connection.execute(
                    """
                    SELECT * FROM code_index_calls
                    WHERE workspace_id = ? AND generation = ?
                        AND (caller_symbol_id = ? OR resolved_callee_symbol_id = ? OR callee_name = ?)
                    ORDER BY logical_path, line_start LIMIT ?
                    """,
                    (identity.workspace_id, generation, request.symbol_id, request.symbol_id, request.name, request.limit),
                ).fetchall()
                calls = tuple(self._row_to_call(row) for row in rows)
                degraded = any(item.provenance is SymbolProvenance.STRUCTURAL_FALLBACK for item in calls)
                reason = "LSP unavailable; call hierarchy is parser-derived and may be incomplete" if degraded else ""
        observed = [item.provenance for item in (*symbols, *references, *calls)]
        if observed and any(item is SymbolProvenance.STRUCTURAL_FALLBACK for item in observed):
            provenance = SymbolProvenance.STRUCTURAL_FALLBACK
        return SymbolQueryResult(
            query=request,
            symbols=symbols,
            references=references,
            call_edges=calls,
            hover=hover,
            provenance=provenance,
            degraded=degraded,
            degradation_reason=reason,
            generation=generation,
            workspace_revision=revision,
        )

    def record_invalidation(
        self,
        identity: WorkspaceIdentity,
        *,
        transaction_id: str,
        changed_paths: Sequence[str],
        deleted_paths: Sequence[str] = (),
    ) -> bool:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO code_index_invalidations(
                    workspace_id, workspace_revision, transaction_id,
                    changed_paths_json, deleted_paths_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.workspace_id,
                    identity.revision,
                    transaction_id,
                    json.dumps(sorted(set(changed_paths))),
                    json.dumps(sorted(set(deleted_paths))),
                    time.time(),
                ),
            )
            return int(connection.execute("SELECT changes() AS count").fetchone()["count"]) == 1

    def mark_invalidations_applied(self, workspace_id: str, generation: int) -> int:
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """
                UPDATE code_index_invalidations SET applied_generation = ?
                WHERE workspace_id = ? AND applied_generation = 0
                """,
                (generation, workspace_id),
            )
            return int(connection.execute("SELECT changes() AS count").fetchone()["count"])

    def pending_invalidations(self, workspace_id: str) -> tuple[Mapping[str, Any], ...]:
        self.initialize()
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM code_index_invalidations
                WHERE workspace_id = ? AND applied_generation = 0
                ORDER BY invalidation_id ASC
                """,
                (workspace_id,),
            ).fetchall()
        return tuple(
            {
                **dict(row),
                "changed_paths": json.loads(str(row["changed_paths_json"])),
                "deleted_paths": json.loads(str(row["deleted_paths_json"])),
            }
            for row in rows
        )

    def has_invalidation(self, workspace_id: str, transaction_id: str) -> bool:
        self.initialize()
        with self.connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM code_index_invalidations
                WHERE workspace_id = ? AND transaction_id = ?
                """,
                (workspace_id, transaction_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _compile_matcher(query: ContentSearchQuery) -> re.Pattern[str]:
        flags = 0 if query.case_sensitive else re.IGNORECASE
        if query.multiline:
            flags |= re.MULTILINE | re.DOTALL
        if query.mode is ContentSearchMode.LITERAL:
            pattern = re.escape(query.pattern)
        else:
            CodeIndexStore._validate_regex(query.pattern)
            pattern = query.pattern
        if query.whole_word:
            pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
        try:
            return re.compile(pattern, flags)
        except re.error as error:
            raise ValueError(f"invalid regex: {error}") from error

    @staticmethod
    def _validate_regex(pattern: str) -> None:
        dangerous = (
            re.compile(r"\([^)]*[+*][^)]*\)[+*]"),
            re.compile(r"\.\*\.\*"),
            re.compile(r"\{\d{5,}(?:,\d*)?\}"),
            re.compile(r"\\[1-9]"),
        )
        if any(item.search(pattern) for item in dangerous):
            raise ValueError("regex rejected by bounded-complexity policy")

    @staticmethod
    def _path_filter(path: str, query: ContentSearchQuery) -> bool:
        pure = PurePosixPath(path)
        if query.paths and path not in query.paths:
            return False
        if query.file_globs and not any(
            pure.match(pattern) or fnmatch.fnmatchcase(path, pattern)
            for pattern in query.file_globs
        ):
            return False
        return True

    @staticmethod
    def _matches_in_file(
        row: sqlite3.Row,
        content: str,
        matcher: re.Pattern[str],
        query: ContentSearchQuery,
    ) -> list[ContentMatch]:
        matches: list[ContentMatch] = []
        lines = content.splitlines(keepends=True)
        offsets: list[int] = [0]
        for line in lines:
            offsets.append(offsets[-1] + len(line))

        def locate(offset: int) -> tuple[int, int]:
            import bisect

            line_index = max(0, bisect.bisect_right(offsets, offset) - 1)
            return line_index + 1, offset - offsets[line_index]

        for found in matcher.finditer(content):
            line_start, column_start = locate(found.start())
            line_end, column_end = locate(max(found.start(), found.end() - 1))
            context_start = max(0, line_start - 1 - query.budget.context_lines)
            context_end = min(len(lines), line_end + query.budget.context_lines)
            preview = "".join(lines[context_start:context_end])
            preview = preview[: query.budget.maximum_match_chars]
            matched = found.group(0)[: query.budget.maximum_match_chars]
            matches.append(
                ContentMatch(
                    workspace_id=str(row["workspace_id"]),
                    logical_path=str(row["logical_path"]),
                    line_start=line_start,
                    line_end=line_end,
                    column_start=column_start,
                    column_end=column_end + (1 if found.end() > found.start() else 0),
                    preview=preview,
                    matched_text=matched,
                    file_hash=str(row["content_hash"]),
                    source_revision=str(row["source_revision"]),
                    generation=int(row["generation"]),
                    score=1.0,
                    metadata={"context_start_line": context_start + 1, "context_end_line": context_end},
                )
            )
        return matches

    @staticmethod
    def _symbol_values(item: CodeSymbol, generation: int) -> tuple[Any, ...]:
        return (
            item.symbol_id,
            item.workspace_id,
            generation,
            item.name,
            item.qualified_name,
            item.kind.value,
            item.language,
            item.location.logical_path,
            item.location.line_start,
            item.location.line_end,
            item.location.column_start,
            item.location.column_end,
            item.signature,
            item.documentation,
            item.container_name,
            item.file_hash,
            item.source_revision,
            item.provenance.value,
            json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True, default=str),
        )

    @staticmethod
    def _reference_values(item: SymbolReference, generation: int) -> tuple[Any, ...]:
        return (
            item.reference_id,
            item.workspace_id,
            generation,
            item.symbol_name,
            item.location.logical_path,
            item.location.line_start,
            item.location.line_end,
            item.location.column_start,
            item.location.column_end,
            item.reference_kind,
            item.file_hash,
            item.source_revision,
            item.resolved_symbol_id,
            item.provenance.value,
            json.dumps(dict(item.metadata), ensure_ascii=False, sort_keys=True, default=str),
        )

    @staticmethod
    def _call_values(workspace_id: str, item: CallEdge, generation: int) -> tuple[Any, ...]:
        return (
            item.edge_id,
            workspace_id,
            generation,
            item.caller_symbol_id,
            item.callee_name,
            item.resolved_callee_symbol_id,
            item.location.logical_path,
            item.location.line_start,
            item.location.line_end,
            item.location.column_start,
            item.location.column_end,
            item.provenance.value,
        )

    @staticmethod
    def _row_to_file(row: sqlite3.Row) -> CodeFile:
        return CodeFile(
            workspace_id=str(row["workspace_id"]),
            logical_path=str(row["logical_path"]),
            language=str(row["language"]),
            suffix=str(row["suffix"]),
            size_bytes=int(row["size_bytes"]),
            mtime_ns=int(row["mtime_ns"]),
            content_hash=str(row["content_hash"]),
            source_revision=str(row["source_revision"]),
            generation=int(row["generation"]),
            line_count=int(row["line_count"]),
            metadata=json.loads(str(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_symbol(row: sqlite3.Row) -> CodeSymbol:
        return CodeSymbol(
            symbol_id=str(row["symbol_id"]),
            workspace_id=str(row["workspace_id"]),
            name=str(row["name"]),
            qualified_name=str(row["qualified_name"]),
            kind=SymbolKind(str(row["kind"])),
            language=str(row["language"]),
            location=CodeIndexStore._row_location(row),
            signature=str(row["signature"]),
            documentation=str(row["documentation"]),
            container_name=str(row["container_name"]),
            file_hash=str(row["file_hash"]),
            source_revision=str(row["source_revision"]),
            generation=int(row["generation"]),
            provenance=SymbolProvenance(str(row["provenance"])),
            metadata=json.loads(str(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_reference(row: sqlite3.Row) -> SymbolReference:
        return SymbolReference(
            reference_id=str(row["reference_id"]),
            workspace_id=str(row["workspace_id"]),
            symbol_name=str(row["symbol_name"]),
            location=CodeIndexStore._row_location(row),
            reference_kind=str(row["reference_kind"]),
            file_hash=str(row["file_hash"]),
            source_revision=str(row["source_revision"]),
            generation=int(row["generation"]),
            resolved_symbol_id=str(row["resolved_symbol_id"]),
            provenance=SymbolProvenance(str(row["provenance"])),
            metadata=json.loads(str(row["metadata_json"])),
        )

    @staticmethod
    def _row_to_call(row: sqlite3.Row) -> CallEdge:
        return CallEdge(
            caller_symbol_id=str(row["caller_symbol_id"]),
            callee_name=str(row["callee_name"]),
            location=CodeIndexStore._row_location(row),
            resolved_callee_symbol_id=str(row["resolved_callee_symbol_id"]),
            provenance=SymbolProvenance(str(row["provenance"])),
        )

    @staticmethod
    def _row_location(row: sqlite3.Row) -> SourceLocation:
        return SourceLocation(
            logical_path=str(row["logical_path"]),
            line_start=int(row["line_start"]),
            line_end=int(row["line_end"]),
            column_start=int(row["column_start"]),
            column_end=int(row["column_end"]),
        )

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
