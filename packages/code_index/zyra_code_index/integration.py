from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping, Sequence

from zyra_core import EventRecord, EventType

from .invalidation import PatchIndexReceipt, WorkspacePatchIndexBridge, WorkspaceTransactionResolver
from .job_models import (
    CodeIndexBuildJob,
    CodeIndexJobOperation,
    CodeIndexJobState,
    CodeIndexWorkerOutcome,
)
from .jobs import CodeIndexBuildQueue
from .models import StaleWorkspaceError, stable_digest
from .runtime import CodeIndexRuntime, TestSelectionResult
from .worker import CodeIndexWorkerRuntime


CODE_QUERY_CONTRACT = "zyra.code-index-query.v2"
CODE_SELECTION_CONTRACT = "zyra.code-index-selection.v2"
CODE_RECOVERY_REFERENCE_CONTRACT = "zyra.code-recovery-index-reference.v1"


class CodeIndexConsumer(StrEnum):
    CODE_WORKER_CONTEXT = "code_worker_context"
    PATCH_PLANNER = "patch_planner"
    TEST_SELECTOR = "test_selector"
    RECOVERY = "recovery"
    API = "api"
    EVALUATION = "evaluation"


class CodeIndexIntegrationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class CodeIndexQuery:
    task_id: str
    text: str
    consumer: CodeIndexConsumer
    run_id: str = ""
    session_id: str = ""
    worker_request_id: str = ""
    changed_paths: tuple[str, ...] = ()
    file_globs: tuple[str, ...] = ()
    maximum_terms: int = 12
    maximum_results: int = 24
    maximum_files: int = 16
    maximum_tests: int = 100
    maximum_chars: int = 20_000
    request_id: str = ""
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> "CodeIndexQuery":
        from dataclasses import replace

        task_id = self.task_id.strip()
        text = self.text.strip()
        if not task_id or not text:
            raise ValueError("code index query requires task_id and text")
        budgets = {
            "maximum_terms": (self.maximum_terms, 1, 64),
            "maximum_results": (self.maximum_results, 0, 1_000),
            "maximum_files": (self.maximum_files, 0, 1_000),
            "maximum_tests": (self.maximum_tests, 0, 10_000),
            "maximum_chars": (self.maximum_chars, 0, 1_000_000),
        }
        for name, (value, minimum, maximum) in budgets.items():
            if value < minimum or value > maximum:
                raise ValueError(f"{name} must be between {minimum} and {maximum}")

        def normalized(values: Sequence[Any]) -> tuple[str, ...]:
            return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))

        return replace(
            self,
            task_id=task_id,
            text=text,
            run_id=self.run_id.strip(),
            session_id=self.session_id.strip(),
            worker_request_id=self.worker_request_id.strip(),
            changed_paths=normalized(self.changed_paths),
            file_globs=normalized(self.file_globs),
            request_id=self.request_id.strip(),
            causation_id=self.causation_id.strip(),
            metadata=dict(self.metadata),
        )

    @property
    def query_id(self) -> str:
        value = self.validated()
        return "codequery_" + stable_digest(
            CODE_QUERY_CONTRACT,
            value.task_id,
            value.text,
            value.consumer.value,
            value.changed_paths,
            value.file_globs,
            value.maximum_terms,
            value.maximum_results,
            value.maximum_files,
            value.maximum_tests,
            value.maximum_chars,
            value.request_id,
        )[:32]

    @property
    def fingerprint(self) -> str:
        return stable_digest(self.to_dict(include_text=False))

    def to_dict(self, *, include_text: bool = True) -> dict[str, Any]:
        value = self.validated()
        body: dict[str, Any] = {
            "schema": CODE_QUERY_CONTRACT,
            "query_id": value.query_id,
            "task_id": value.task_id,
            "consumer": value.consumer.value,
            "run_id": value.run_id,
            "session_id": value.session_id,
            "worker_request_id": value.worker_request_id,
            "changed_paths": list(value.changed_paths),
            "file_globs": list(value.file_globs),
            "budget": {
                "maximum_terms": value.maximum_terms,
                "maximum_results": value.maximum_results,
                "maximum_files": value.maximum_files,
                "maximum_tests": value.maximum_tests,
                "maximum_chars": value.maximum_chars,
            },
            "request_id": value.request_id,
            "causation_id": value.causation_id,
            "metadata": dict(value.metadata),
        }
        if include_text:
            body["text"] = value.text
        else:
            body["text_digest"] = stable_digest(value.text)
        return body


@dataclass(frozen=True, slots=True)
class CodeSourceRef:
    logical_path: str
    line_start: int
    line_end: int
    file_hash: str
    source_revision: str
    generation: int
    matched_terms: tuple[str, ...]
    score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "file_hash": self.file_hash,
            "source_revision": self.source_revision,
            "generation": self.generation,
            "matched_terms": list(self.matched_terms),
            "score": self.score,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexSelection:
    request: CodeIndexQuery
    workspace_id: str
    workspace_revision: str
    generation: int
    search_terms: tuple[str, ...]
    source_refs: tuple[CodeSourceRef, ...]
    excerpts: tuple[str, ...]
    selected_files: tuple[str, ...]
    selected_tests: tuple[str, ...]
    test_reasons: Mapping[str, tuple[str, ...]]
    total_chars: int
    truncated: bool
    result_digest: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CODE_SELECTION_CONTRACT,
            "query": self.request.to_dict(include_text=False),
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "generation": self.generation,
            "search_terms": list(self.search_terms),
            "source_refs": [item.to_dict() for item in self.source_refs],
            "excerpts": list(self.excerpts),
            "selected_files": list(self.selected_files),
            "selected_tests": list(self.selected_tests),
            "test_reasons": {key: list(value) for key, value in self.test_reasons.items()},
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "result_digest": self.result_digest,
            "warnings": list(self.warnings),
            "retrieval_scope": "task_workspace",
            "code_index_source": True,
            "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
        }

    def recovery_reference(self) -> Mapping[str, Any]:
        return {
            "schema": CODE_RECOVERY_REFERENCE_CONTRACT,
            "task_id": self.request.task_id,
            "query_id": self.request.query_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "generation": self.generation,
            "source_refs": [item.to_dict() for item in self.source_refs],
            "selected_files": list(self.selected_files),
            "selected_tests": list(self.selected_tests),
            "result_digest": self.result_digest,
            "index_dump_embedded": False,
            "workspace_content_embedded": False,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexAdmissionResult:
    job: CodeIndexBuildJob
    disposition: str
    patch_receipt: PatchIndexReceipt | None = None
    worker_outcomes: tuple[CodeIndexWorkerOutcome, ...] = ()
    published: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "job": self.job.to_dict(),
            "disposition": self.disposition,
            "patch_receipt": self.patch_receipt.to_dict() if self.patch_receipt else None,
            "worker_outcomes": [outcome.to_dict() for outcome in self.worker_outcomes],
            "published": self.published,
            "canonical_owner": "WorkspaceManagerRuntime+WorkspaceIntegrationStore",
            "derived_owner": "CodeIndexRuntime",
        }


class CodeIndexQueryJournal:
    """Derived query/delivery receipts stored in the code-index database."""

    def __init__(self, runtime: CodeIndexRuntime) -> None:
        self.runtime = runtime
        self.initialize()

    def initialize(self) -> None:
        with self.runtime.store.transaction(immediate=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS code_index_query_receipts (
                    query_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    consumer TEXT NOT NULL,
                    worker_request_id TEXT NOT NULL,
                    query_fingerprint TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    workspace_revision TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    source_ref_count INTEGER NOT NULL,
                    selected_file_count INTEGER NOT NULL,
                    selected_test_count INTEGER NOT NULL,
                    result_digest TEXT NOT NULL,
                    budget_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_code_query_task_consumer
                    ON code_index_query_receipts(task_id, consumer, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_code_query_worker
                    ON code_index_query_receipts(worker_request_id)
                    WHERE worker_request_id <> '';

                CREATE TABLE IF NOT EXISTS code_index_context_deliveries (
                    worker_request_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    query_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    terminal_event_ids_json TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )

    def record_query(self, selection: CodeIndexSelection) -> Mapping[str, Any]:
        request = selection.request
        now = time.time()
        budget = request.to_dict(include_text=False)["budget"]
        with self.runtime.store.transaction(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO code_index_query_receipts(
                    query_id, task_id, consumer, worker_request_id,
                    query_fingerprint, workspace_id, workspace_revision,
                    generation, source_ref_count, selected_file_count,
                    selected_test_count, result_digest, budget_json,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id) DO UPDATE SET
                    worker_request_id = excluded.worker_request_id,
                    workspace_revision = excluded.workspace_revision,
                    generation = excluded.generation,
                    source_ref_count = excluded.source_ref_count,
                    selected_file_count = excluded.selected_file_count,
                    selected_test_count = excluded.selected_test_count,
                    result_digest = excluded.result_digest,
                    budget_json = excluded.budget_json,
                    metadata_json = excluded.metadata_json
                """,
                (
                    request.query_id,
                    request.task_id,
                    request.consumer.value,
                    request.worker_request_id,
                    request.fingerprint,
                    selection.workspace_id,
                    selection.workspace_revision,
                    selection.generation,
                    len(selection.source_refs),
                    len(selection.selected_files),
                    len(selection.selected_tests),
                    selection.result_digest,
                    self._json(budget),
                    self._json(
                        {
                            "request_id": request.request_id,
                            "causation_id": request.causation_id,
                            "search_terms": selection.search_terms,
                            "warnings": selection.warnings,
                        }
                    ),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM code_index_query_receipts WHERE query_id = ?",
                (request.query_id,),
            ).fetchone()
        return {**dict(row), "raw_query_persisted": False}

    def claim_delivery(
        self,
        selection: CodeIndexSelection,
        *,
        message_id: str,
    ) -> Mapping[str, Any]:
        request = selection.request
        if not request.worker_request_id:
            raise ValueError("code context delivery requires worker_request_id")
        now = time.time()
        with self.runtime.store.transaction(immediate=True) as connection:
            existing = connection.execute(
                """
                SELECT * FROM code_index_context_deliveries
                WHERE worker_request_id = ?
                """,
                (request.worker_request_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["query_id"]) != request.query_id
                    or str(existing["result_digest"]) != selection.result_digest
                ):
                    raise RuntimeError("worker request already claimed another code-index selection")
                if str(existing["state"]) == "released":
                    connection.execute(
                        """
                        UPDATE code_index_context_deliveries
                        SET state = 'claimed', terminal_event_ids_json = '[]',
                            reason = '', message_id = ?, updated_at = ?
                        WHERE worker_request_id = ?
                        """,
                        (message_id, now, request.worker_request_id),
                    )
                    existing = connection.execute(
                        "SELECT * FROM code_index_context_deliveries WHERE worker_request_id = ?",
                        (request.worker_request_id,),
                    ).fetchone()
                return dict(existing)
            connection.execute(
                """
                INSERT INTO code_index_context_deliveries(
                    worker_request_id, task_id, query_id, state, result_digest,
                    message_id, terminal_event_ids_json, reason, created_at, updated_at
                ) VALUES (?, ?, ?, 'claimed', ?, ?, '[]', '', ?, ?)
                """,
                (
                    request.worker_request_id,
                    request.task_id,
                    request.query_id,
                    selection.result_digest,
                    message_id,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM code_index_context_deliveries WHERE worker_request_id = ?",
                (request.worker_request_id,),
            ).fetchone()
        return dict(row)

    def delivery(self, worker_request_id: str) -> Mapping[str, Any] | None:
        """Return the durable delivery receipt for compensation and recovery."""

        with self.runtime.store.connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM code_index_context_deliveries
                WHERE worker_request_id = ?
                """,
                (worker_request_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def finish_delivery(
        self,
        worker_request_id: str,
        *,
        committed: bool,
        terminal_event_ids: Sequence[str],
        reason: str,
    ) -> Mapping[str, Any]:
        state = "committed" if committed else "released"
        with self.runtime.store.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM code_index_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
            if row is None:
                raise KeyError(worker_request_id)
            if str(row["state"]) == "committed" and not committed:
                raise RuntimeError("committed code-index delivery cannot be released")
            connection.execute(
                """
                UPDATE code_index_context_deliveries
                SET state = ?, terminal_event_ids_json = ?, reason = ?, updated_at = ?
                WHERE worker_request_id = ?
                """,
                (
                    state,
                    self._json(tuple(dict.fromkeys(str(value) for value in terminal_event_ids if str(value)))),
                    reason,
                    time.time(),
                    worker_request_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM code_index_context_deliveries WHERE worker_request_id = ?",
                (worker_request_id,),
            ).fetchone()
        return dict(updated)

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class CodeSearchPlanner:
    """Turn a task request into bounded content/file/test selections."""

    STOP_WORDS = frozenset(
        {
            "about",
            "after",
            "again",
            "also",
            "before",
            "build",
            "change",
            "code",
            "complete",
            "create",
            "from",
            "have",
            "implement",
            "into",
            "make",
            "need",
            "please",
            "should",
            "task",
            "that",
            "their",
            "then",
            "there",
            "these",
            "this",
            "update",
            "with",
        }
    )

    def __init__(self, runtime: CodeIndexRuntime) -> None:
        self.runtime = runtime

    def select(self, query: CodeIndexQuery) -> CodeIndexSelection:
        request = query.validated()
        self.runtime._require_current_revision()
        terms = self.search_terms(request.text, maximum=request.maximum_terms)
        refs_by_location: dict[tuple[str, int, int], dict[str, Any]] = {}
        excerpts_by_location: dict[tuple[str, int, int], str] = {}
        warnings: list[str] = []
        truncated = False
        per_term_results = max(1, request.maximum_results // max(1, len(terms)))
        per_term_chars = max(256, request.maximum_chars // max(1, len(terms)))
        for term_index, term in enumerate(terms):
            remaining_results = max(0, request.maximum_results - len(refs_by_location))
            remaining_chars = max(0, request.maximum_chars - sum(len(value) for value in excerpts_by_location.values()))
            if not remaining_results or not remaining_chars:
                truncated = True
                break
            context = self.runtime.context(
                term,
                maximum_chars=min(per_term_chars, remaining_chars),
                maximum_results=min(per_term_results, remaining_results),
                file_globs=request.file_globs,
            )
            truncated = truncated or context.truncated
            for raw, excerpt in zip(context.source_refs, context.excerpts, strict=True):
                key = (
                    str(raw["logical_path"]),
                    int(raw["line_start"]),
                    int(raw["line_end"]),
                )
                score = 1.0 / (1.0 + term_index)
                existing = refs_by_location.get(key)
                if existing is None:
                    refs_by_location[key] = {
                        **dict(raw),
                        "matched_terms": [term],
                        "score": score,
                    }
                    excerpts_by_location[key] = excerpt
                else:
                    existing["matched_terms"] = list(
                        dict.fromkeys((*existing["matched_terms"], term))
                    )
                    existing["score"] = float(existing["score"]) + score
        ordered_raw = sorted(
            refs_by_location.items(),
            key=lambda item: (
                -float(item[1]["score"]),
                item[0][0],
                item[0][1],
                item[0][2],
            ),
        )
        source_refs = tuple(
            CodeSourceRef(
                logical_path=key[0],
                line_start=key[1],
                line_end=key[2],
                file_hash=str(raw["file_hash"]),
                source_revision=str(raw["source_revision"]),
                generation=int(raw["generation"]),
                matched_terms=tuple(raw["matched_terms"]),
                score=round(float(raw["score"]), 8),
            )
            for key, raw in ordered_raw
        )
        selected_files = tuple(
            dict.fromkeys(ref.logical_path for ref in source_refs)
        )[: request.maximum_files]
        test_inputs = request.changed_paths or selected_files
        tests = (
            self.runtime.select_tests(test_inputs, limit=request.maximum_tests)
            if test_inputs and request.maximum_tests
            else TestSelectionResult(
                changed_paths=tuple(test_inputs),
                selected_tests=(),
                reasons={},
                generation=(source_refs[0].generation if source_refs else self._generation()),
                workspace_revision=self.runtime.source.identity.revision,
            )
        )
        excerpts = tuple(excerpts_by_location[key] for key, _ in ordered_raw)
        total_chars = sum(len(value) for value in excerpts)
        if not terms:
            warnings.append("no_searchable_query_terms")
        if not source_refs:
            warnings.append("code_index_no_matches")
        result_digest = stable_digest(
            request.fingerprint,
            [ref.to_dict() for ref in source_refs],
            selected_files,
            tests.selected_tests,
            tests.reasons,
        )
        return CodeIndexSelection(
            request=request,
            workspace_id=self.runtime.source.identity.workspace_id,
            workspace_revision=self.runtime.source.identity.revision,
            generation=(source_refs[0].generation if source_refs else self._generation()),
            search_terms=terms,
            source_refs=source_refs,
            excerpts=excerpts,
            selected_files=selected_files,
            selected_tests=tests.selected_tests,
            test_reasons=tests.reasons,
            total_chars=total_chars,
            truncated=truncated or len(tuple(dict.fromkeys(ref.logical_path for ref in source_refs))) > len(selected_files),
            result_digest=result_digest,
            warnings=tuple(warnings),
        )

    def search_terms(self, text: str, *, maximum: int) -> tuple[str, ...]:
        candidates: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for position, raw in enumerate(
            re.findall(r"[A-Za-z_][A-Za-z0-9_.:/-]{2,}|[\u4e00-\u9fff]{2,}", text)
        ):
            value = raw.strip("./:-").casefold()
            if not value or value in seen or value in self.STOP_WORDS:
                continue
            seen.add(value)
            identifier_bonus = 4 if any(character in raw for character in "_./:") else 0
            case_bonus = 2 if any(character.isupper() for character in raw[1:]) else 0
            length_bonus = min(len(value), 24)
            candidates.append((-(identifier_bonus + case_bonus + length_bonus), position, value))
        candidates.sort()
        return tuple(value for _, _, value in candidates[: max(1, maximum)])

    def _generation(self) -> int:
        state = self.runtime.store.workspace_state(self.runtime.source.identity.workspace_id)
        return int(state.get("active_generation") or 0)


class CodeIndexIntegrationRuntime:
    """Patch/event admission, durable worker and worker-selection coordinator."""

    def __init__(
        self,
        runtime: CodeIndexRuntime,
        *,
        transaction_resolver: WorkspaceTransactionResolver | None = None,
        worker: CodeIndexWorkerRuntime | None = None,
        process_worker: Callable[[str, int], Sequence[Mapping[str, Any]]] | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        run_id: str = "",
        task_id: str = "",
        enabled: bool = True,
        worker_enabled: bool = True,
        sweeper_enabled: bool = True,
        allow_inline_worker_for_tests: bool = False,
    ) -> None:
        self.runtime = runtime
        self.queue = CodeIndexBuildQueue(runtime.store)
        self.transaction_resolver = transaction_resolver
        self.patch_bridge = (
            WorkspacePatchIndexBridge(
                transaction_resolver,
                lambda workspace_id: self._runtime_for_workspace(workspace_id),
            )
            if transaction_resolver is not None
            else None
        )
        self.worker = worker or CodeIndexWorkerRuntime(
            worker_id="code-index-inline-test",
            queue=self.queue,
            runtime_factory=lambda job: self._runtime_for_job(job),
        )
        self.process_worker = process_worker
        self.event_sink = event_sink
        self.run_id = run_id or runtime.source.identity.run_id
        self.task_id = task_id or runtime.source.identity.task_id
        self.enabled = bool(enabled)
        self.worker_enabled = bool(worker_enabled)
        self.sweeper_enabled = bool(sweeper_enabled)
        self.allow_inline_worker_for_tests = bool(allow_inline_worker_for_tests)
        self.journal = CodeIndexQueryJournal(runtime)
        self.planner = CodeSearchPlanner(runtime)

    def admit_initial(
        self,
        *,
        process: bool = True,
        causation_id: str = "",
    ) -> CodeIndexAdmissionResult:
        self._require_enabled()
        identity = self.runtime.source.identity
        job = self.queue.enqueue(
            identity,
            operation=CodeIndexJobOperation.REBUILD,
            payload={
                "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
                "reason": "initial_or_revision_rebuild",
            },
            idempotency_key=f"initial:{identity.workspace_id}:{identity.revision}",
            causation_id=causation_id,
        )
        outcomes = self._process(job.workspace_id) if process and job.state is CodeIndexJobState.QUEUED else ()
        current = self.queue.require(job.job_id)
        published = self._published(current)
        if process and not published:
            raise CodeIndexIntegrationError(
                "code_index_publication_not_observed",
                "code index worker returned without publishing the admitted workspace revision",
            )
        self._emit(
            "initial_published" if published else "initial_queued",
            {
                "job": current.to_dict(),
                "worker_outcomes": [dict(value) for value in outcomes],
            },
        )
        return CodeIndexAdmissionResult(
            job=current,
            disposition="published" if published else "queued",
            worker_outcomes=tuple(self._outcome(value) for value in outcomes),
            published=published,
        )

    def admit_patch_transaction(
        self,
        transaction_id: str,
        *,
        process: bool = True,
        causation_id: str = "",
    ) -> CodeIndexAdmissionResult:
        self._require_enabled()
        if self.patch_bridge is None:
            raise CodeIndexIntegrationError(
                "workspace_transaction_resolver_missing",
                "patch admission requires WorkspaceIntegrationStore",
            )
        patch = self.patch_bridge.apply_transaction(transaction_id, rebuild=False)
        if patch.disposition == "ignored":
            raise CodeIndexIntegrationError(
                "workspace_patch_not_committed",
                patch.reason or "workspace patch transaction is not committed",
            )
        identity = self.runtime.source.identity
        if patch.workspace_id != identity.workspace_id:
            raise StaleWorkspaceError("patch transaction belongs to another workspace")
        job = self.queue.enqueue(
            identity,
            operation=CodeIndexJobOperation.PATCH,
            transaction_id=patch.transaction_id,
            changed_paths=patch.changed_paths,
            deleted_paths=patch.deleted_paths,
            payload={
                "patch_disposition": patch.disposition,
                "canonical_transaction_resolved": True,
                "workspace_revision": identity.revision,
            },
            idempotency_key=f"patch:{identity.workspace_id}:{patch.transaction_id}",
            causation_id=causation_id or patch.transaction_id,
        )
        outcomes = self._process(job.workspace_id) if process and job.state is CodeIndexJobState.QUEUED else ()
        current = self.queue.require(job.job_id)
        published = self._published(current)
        if process and not published:
            raise CodeIndexIntegrationError(
                "code_index_patch_not_published",
                "durable code-index worker did not publish the committed patch revision",
            )
        self._emit(
            "patch_published" if published else "patch_queued",
            {
                "transaction_id": patch.transaction_id,
                "changed_paths": list(patch.changed_paths),
                "deleted_paths": list(patch.deleted_paths),
                "job": current.to_dict(),
                "worker_outcomes": [dict(value) for value in outcomes],
            },
        )
        return CodeIndexAdmissionResult(
            job=current,
            disposition="published" if published else "queued",
            patch_receipt=patch,
            worker_outcomes=tuple(self._outcome(value) for value in outcomes),
            published=published,
        )

    def select(self, query: CodeIndexQuery) -> CodeIndexSelection:
        self._require_enabled()
        value = query.validated()
        if value.task_id != self.task_id:
            raise CodeIndexIntegrationError(
                "code_index_task_mismatch",
                "code-index selection request belongs to another task",
            )
        fence = self.queue.fence_state(self.runtime.source.identity.workspace_id)
        state = self.runtime.store.workspace_state(self.runtime.source.identity.workspace_id)
        if (
            not state
            or str(state.get("workspace_revision") or "") != self.runtime.source.identity.revision
            or int(state.get("active_generation") or 0) != int(fence.get("published_generation") or 0)
            or str(fence.get("published_revision") or "") != self.runtime.source.identity.revision
        ):
            raise CodeIndexIntegrationError(
                "code_index_revision_not_current",
                "code index selection cannot use a stale or unproven workspace generation",
            )
        selection = self.planner.select(value)
        self.journal.record_query(selection)
        self._emit(
            "query_snapshot",
            {
                "query_id": value.query_id,
                "consumer": value.consumer.value,
                "query_fingerprint": value.fingerprint,
                "workspace_id": selection.workspace_id,
                "workspace_revision": selection.workspace_revision,
                "generation": selection.generation,
                "source_refs": [ref.to_dict() for ref in selection.source_refs],
                "selected_files": list(selection.selected_files),
                "selected_tests": list(selection.selected_tests),
                "result_digest": selection.result_digest,
                "budget": value.to_dict(include_text=False)["budget"],
            },
        )
        return selection

    def sweep_and_recover(self, *, maximum_jobs: int = 64) -> Mapping[str, Any]:
        self._require_enabled()
        if not self.sweeper_enabled:
            return {
                "sweeper_enabled": False,
                "stale_lease_recovered": False,
                "fallback": False,
            }
        sweep = self.queue.sweep_expired()
        outcomes: Sequence[Mapping[str, Any]] = ()
        if sweep.count:
            outcomes = self._process(self.runtime.source.identity.workspace_id, maximum_jobs)
        result = {
            "sweep": sweep.to_dict(),
            "worker_outcomes": [dict(value) for value in outcomes],
        }
        self._emit("sweeper_completed", result)
        return result

    def disable_probe(self, query: CodeIndexQuery) -> Mapping[str, Any]:
        enabled: CodeIndexSelection | None = None
        try:
            enabled = self.select(query)
        except (CodeIndexIntegrationError, StaleWorkspaceError):
            enabled = None
        return {
            "enabled": {
                "selected_files": list(enabled.selected_files) if enabled else [],
                "selected_tests": list(enabled.selected_tests) if enabled else [],
                "context_chars": enabled.total_chars if enabled else 0,
            },
            "disabled": {
                "selected_files": [],
                "selected_tests": [],
                "context_chars": 0,
                "legacy_grep_fallback": False,
                "workspace_scan_fallback": False,
            },
            "behavior_changed": bool(
                enabled
                and (enabled.selected_files or enabled.selected_tests or enabled.total_chars)
            ),
        }

    def status(self) -> Mapping[str, Any]:
        workspace_id = self.runtime.source.identity.workspace_id
        return {
            "enabled": self.enabled,
            "worker_enabled": self.worker_enabled,
            "sweeper_enabled": self.sweeper_enabled,
            "runtime": self.runtime.status(),
            "generation_fence": dict(self.queue.fence_state(workspace_id)),
            "job_counts": {
                state.value: len(self.queue.list(workspace_id=workspace_id, states=(state,)))
                for state in CodeIndexJobState
            },
            "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
            "derived_owner": "CodeIndexRuntime",
            "external_source_dependency": False,
            "fallback": False,
        }

    def _process(
        self,
        workspace_id: str,
        maximum_jobs: int = 64,
    ) -> Sequence[Mapping[str, Any]]:
        if not self.worker_enabled:
            raise CodeIndexIntegrationError(
                "code_index_worker_disabled",
                "code-index job is queued but the worker is disabled",
            )
        if self.process_worker is not None:
            outcomes = tuple(dict(value) for value in self.process_worker(workspace_id, maximum_jobs))
        elif self.allow_inline_worker_for_tests:
            outcomes = tuple(
                outcome.to_dict()
                for outcome in self.worker.drain(
                    maximum_jobs=maximum_jobs,
                    workspace_id=workspace_id,
                )
            )
        else:
            raise CodeIndexIntegrationError(
                "code_index_worker_process_missing",
                "production code-index integration requires the worker process port",
            )
        failed = [value for value in outcomes if str(value.get("status")) not in {"ready", "idle", "fenced"}]
        if failed:
            raise CodeIndexIntegrationError(
                "code_index_worker_failed",
                f"code-index worker returned {len(failed)} failed outcomes",
            )
        return outcomes

    def _published(self, job: CodeIndexBuildJob) -> bool:
        fence = self.queue.fence_state(job.workspace_id)
        state = self.runtime.store.workspace_state(job.workspace_id)
        return (
            job.state is CodeIndexJobState.READY
            and int(fence.get("published_generation") or 0) == job.generation
            and str(fence.get("published_revision") or "") == job.source_revision
            and int(state.get("active_generation") or 0) == job.generation
            and str(state.get("workspace_revision") or "") == job.source_revision
        )

    def _runtime_for_workspace(self, workspace_id: str) -> CodeIndexRuntime:
        if self.runtime.source.identity.workspace_id != workspace_id:
            raise StaleWorkspaceError("integration runtime is bound to another workspace")
        return self.runtime

    def _runtime_for_job(self, job: CodeIndexBuildJob) -> CodeIndexRuntime:
        runtime = self._runtime_for_workspace(job.workspace_id)
        if runtime.source.identity.revision != job.source_revision:
            raise StaleWorkspaceError("code-index job source revision is stale")
        return runtime

    def _emit(self, phase: str, payload: Mapping[str, Any]) -> None:
        if self.event_sink is None or not self.run_id or not self.task_id:
            return
        self.event_sink(
            EventRecord(
                run_id=self.run_id,
                task_id=self.task_id,
                event_type=EventType.SYSTEM_NOTICE,
                payload={
                    "code_index": {
                        "schema": "zyra.code-index-event.v1",
                        "phase": phase,
                        "state_owner": "CodeIndexRuntime",
                        "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
                        **dict(payload),
                    }
                },
            )
        )

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise CodeIndexIntegrationError(
                "code_index_disabled",
                "CodeIndexRuntime is disabled and no workspace-scan fallback is allowed",
            )

    @staticmethod
    def _outcome(value: Mapping[str, Any]) -> CodeIndexWorkerOutcome:
        return CodeIndexWorkerOutcome(
            status=str(value.get("status") or "unknown"),
            worker_id=str(value.get("worker_id") or ""),
            workspace_id=str(value.get("workspace_id") or ""),
            job_id=str(value.get("job_id") or ""),
            generation=int(value.get("generation") or 0),
            source_revision=str(value.get("source_revision") or ""),
            content_digest=str(value.get("content_digest") or ""),
            file_count=int(value.get("file_count") or 0),
            symbol_count=int(value.get("symbol_count") or 0),
            reference_count=int(value.get("reference_count") or 0),
            call_edge_count=int(value.get("call_edge_count") or 0),
            error_code=str(value.get("error_code") or ""),
            error_message=str(value.get("error_message") or ""),
            fenced=bool(value.get("fenced")),
        )


__all__ = [
    "CODE_QUERY_CONTRACT",
    "CODE_RECOVERY_REFERENCE_CONTRACT",
    "CODE_SELECTION_CONTRACT",
    "CodeIndexAdmissionResult",
    "CodeIndexConsumer",
    "CodeIndexIntegrationError",
    "CodeIndexIntegrationRuntime",
    "CodeIndexQuery",
    "CodeIndexQueryJournal",
    "CodeIndexSelection",
    "CodeSearchPlanner",
    "CodeSourceRef",
]
