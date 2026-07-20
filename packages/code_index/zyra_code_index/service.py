from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .models import (
    ContentSearchMode,
    ContentSearchQuery,
    DiscoveryBudget,
    FileDiscoveryQuery,
    SearchBudget,
    SymbolCapability,
    SymbolQuery,
    stable_digest,
)
from .runtime import CodeIndexRuntime


class CodeIndexServiceError(RuntimeError):
    code = "code_index_service_error"
    status = 500

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": self.code,
            "message": str(self),
            "details": self.details,
            "fallback": False,
        }


class CodeIndexDisabledError(CodeIndexServiceError):
    code = "code_index_disabled"
    status = 503


class CodeIndexRequestError(CodeIndexServiceError):
    code = "code_index_request_invalid"
    status = 400


class CodeIndexOperation(StrEnum):
    STATUS = "status"
    REBUILD = "rebuild"
    SEARCH = "search"
    SYMBOLS = "symbols"
    CONTEXT = "context"
    SELECT_TESTS = "select_tests"
    INVALIDATE = "invalidate"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class CodeIndexServiceLimits:
    maximum_pattern_chars: int = 8_000
    maximum_globs: int = 256
    maximum_paths: int = 1_000
    maximum_languages: int = 128
    maximum_changed_paths: int = 5_000
    maximum_results: int = 2_000
    maximum_output_chars: int = 2_000_000
    maximum_context_chars: int = 250_000
    maximum_context_results: int = 1_000
    maximum_symbol_results: int = 10_000
    maximum_discovery_files: int = 100_000
    maximum_discovery_bytes: int = 2 * 1024 * 1024 * 1024
    maximum_file_bytes: int = 16 * 1024 * 1024
    maximum_deadline_ms: int = 120_000

    def validate(self) -> "CodeIndexServiceLimits":
        values = (
            self.maximum_pattern_chars,
            self.maximum_globs,
            self.maximum_paths,
            self.maximum_languages,
            self.maximum_changed_paths,
            self.maximum_results,
            self.maximum_output_chars,
            self.maximum_context_chars,
            self.maximum_context_results,
            self.maximum_symbol_results,
            self.maximum_discovery_files,
            self.maximum_discovery_bytes,
            self.maximum_file_bytes,
            self.maximum_deadline_ms,
        )
        if any(value <= 0 for value in values):
            raise ValueError("all CodeIndex service limits must be positive")
        if self.maximum_file_bytes > self.maximum_discovery_bytes:
            raise ValueError("maximum_file_bytes cannot exceed maximum_discovery_bytes")
        return self


@dataclass(frozen=True, slots=True)
class CodeIndexReceipt:
    request_id: str
    operation: CodeIndexOperation
    task_id: str
    workspace_id: str
    workspace_revision: str
    index_generation: int
    started_at_ns: int
    finished_at_ns: int
    rebuilt: bool = False
    result_count: int = 0
    truncated: bool = False
    warnings: tuple[str, ...] = ()
    causation_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def elapsed_ms(self) -> float:
        return max(0.0, (self.finished_at_ns - self.started_at_ns) / 1_000_000.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "operation": self.operation.value,
            "task_id": self.task_id,
            "workspace_id": self.workspace_id,
            "workspace_revision": self.workspace_revision,
            "index_generation": self.index_generation,
            "elapsed_ms": self.elapsed_ms,
            "rebuilt": self.rebuilt,
            "result_count": self.result_count,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
            "causation_id": self.causation_id,
            "metadata": dict(self.metadata),
            "canonical_workspace_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
            "index_state": "derived_rebuildable",
        }


@dataclass(frozen=True, slots=True)
class CodeIndexServiceResponse:
    status: int
    body: Mapping[str, Any]
    receipt: CodeIndexReceipt

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": 200 <= self.status < 300,
            "data": dict(self.body),
            "receipt": self.receipt.to_dict(),
        }


class CodeIndexRuntimeRegistry:
    """Constructs workspace-bound runtimes without persisting physical roots.

    Cache identity includes the public workspace revision.  A binding epoch or
    lease change therefore creates a fresh runtime over the same rebuildable DB;
    CodeIndexRuntime still refuses to query a stale published generation.
    """

    def __init__(
        self,
        manager: Any,
        *,
        index_root: str | Path,
        worker_id: str = "CodeIndexApiService",
    ) -> None:
        self.manager = manager
        self.index_root = Path(index_root)
        self.worker_id = str(worker_id).strip() or "CodeIndexApiService"
        self._lock = threading.RLock()
        self._runtimes: dict[tuple[str, str], CodeIndexRuntime] = {}

    def runtime_for_task(self, task_id: str) -> CodeIndexRuntime:
        normalized_task_id = _required_text(task_id, "task_id", maximum=512)
        access = self.manager.acquire_for_worker(
            task_id=normalized_task_id,
            session_id="",
            worker_id=self.worker_id,
        )
        workspace_id = _required_text(access.workspace_id, "workspace_id", maximum=512)
        public = access.to_public_dict()
        revision = str(public.get("revision") or "")
        if not revision:
            revision = stable_digest(
                workspace_id,
                public.get("owner_epoch"),
                public.get("binding_revision"),
                public.get("lease_id"),
            )
        key = (workspace_id, revision)
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None:
                return existing
            self.index_root.mkdir(parents=True, exist_ok=True)
            database = self.index_root / f"workspace-{stable_digest(workspace_id)[:32]}.sqlite3"
            runtime = CodeIndexRuntime.from_workspace_manager(
                self.manager,
                access,
                index_path=database,
            )
            self._runtimes[key] = runtime
            self._remove_stale_workspace_entries(workspace_id, keep=key)
            return runtime

    def clear_task(self, task_id: str) -> int:
        normalized_task_id = _required_text(task_id, "task_id", maximum=512)
        with self._lock:
            keys = [
                key
                for key, runtime in self._runtimes.items()
                if runtime.source.identity.task_id == normalized_task_id
            ]
            for key in keys:
                self._runtimes.pop(key, None)
            return len(keys)

    def cached_status(self) -> Mapping[str, Any]:
        with self._lock:
            entries = [
                {
                    "workspace_id": workspace_id,
                    "workspace_revision": revision,
                    "task_id": runtime.source.identity.task_id,
                }
                for (workspace_id, revision), runtime in self._runtimes.items()
            ]
        return {
            "cached_runtime_count": len(entries),
            "entries": entries,
            "index_root": "redacted",
        }

    def _remove_stale_workspace_entries(
        self,
        workspace_id: str,
        *,
        keep: tuple[str, str],
    ) -> None:
        stale = [key for key in self._runtimes if key[0] == workspace_id and key != keep]
        for key in stale:
            self._runtimes.pop(key, None)


class CodeIndexApiService:
    def __init__(
        self,
        registry: CodeIndexRuntimeRegistry,
        *,
        enabled: bool = True,
        limits: CodeIndexServiceLimits | None = None,
    ) -> None:
        self.registry = registry
        self.enabled = bool(enabled)
        self.limits = (limits or CodeIndexServiceLimits()).validate()

    def status(self, task_id: str, *, request_id: str = "") -> CodeIndexServiceResponse:
        return self.execute(
            task_id,
            CodeIndexOperation.STATUS,
            {},
            request_id=request_id,
        )

    def execute(
        self,
        task_id: str,
        operation: CodeIndexOperation | str,
        payload: Mapping[str, Any] | None,
        *,
        request_id: str = "",
        causation_id: str = "",
    ) -> CodeIndexServiceResponse:
        if not self.enabled:
            raise CodeIndexDisabledError(
                "CodeIndex is disabled; no filesystem scan fallback is permitted",
                details={"legacy_recursive_scan": False, "grep_subprocess": False},
            )
        normalized_task_id = _required_text(task_id, "task_id", maximum=512)
        try:
            selected = operation if isinstance(operation, CodeIndexOperation) else CodeIndexOperation(str(operation))
        except ValueError as error:
            raise CodeIndexRequestError(
                f"unsupported CodeIndex operation: {operation!r}",
                details={"operations": [item.value for item in CodeIndexOperation]},
            ) from error
        body = _mapping(payload, "payload")
        started = time.time_ns()
        runtime = self.registry.runtime_for_task(normalized_task_id)
        rebuilt = False
        warnings: tuple[str, ...] = ()
        result_count = 0
        truncated = False

        if selected is CodeIndexOperation.STATUS:
            result: Mapping[str, Any] = runtime.status()
        elif selected is CodeIndexOperation.REBUILD:
            build = runtime.rebuild(query=self._discovery_query(body))
            result = build.to_dict()
            rebuilt = True
            warnings = build.warnings
            result_count = build.file_count
        elif selected is CodeIndexOperation.SEARCH:
            rebuilt = runtime.ensure_current() is not None
            search = runtime.search(self._content_query(body))
            result = search.to_dict()
            warnings = search.warnings
            result_count = len(search.matches)
            truncated = search.truncated
        elif selected is CodeIndexOperation.SYMBOLS:
            rebuilt = runtime.ensure_current() is not None
            symbols = runtime.symbols(self._symbol_query(body))
            result = symbols.to_dict()
            result_count = len(symbols.symbols) + len(symbols.references) + len(symbols.call_edges)
            warnings = (symbols.degradation_reason,) if symbols.degraded and symbols.degradation_reason else ()
        elif selected is CodeIndexOperation.CONTEXT:
            rebuilt = runtime.ensure_current() is not None
            context = runtime.context(
                _required_text(body.get("query"), "query", maximum=self.limits.maximum_pattern_chars),
                maximum_chars=_bounded_int(
                    body.get("maximum_chars"),
                    default=16_000,
                    minimum=0,
                    maximum=self.limits.maximum_context_chars,
                    name="maximum_chars",
                ),
                maximum_results=_bounded_int(
                    body.get("maximum_results"),
                    default=20,
                    minimum=0,
                    maximum=self.limits.maximum_context_results,
                    name="maximum_results",
                ),
                file_globs=_string_tuple(
                    body.get("file_globs"),
                    "file_globs",
                    maximum_items=self.limits.maximum_globs,
                ),
            )
            result = context.to_dict()
            result_count = len(context.source_refs)
            truncated = context.truncated
        elif selected is CodeIndexOperation.SELECT_TESTS:
            rebuilt = runtime.ensure_current() is not None
            changed_paths = _string_tuple(
                body.get("changed_paths"),
                "changed_paths",
                maximum_items=self.limits.maximum_changed_paths,
                required=True,
            )
            tests = runtime.select_tests(
                changed_paths,
                limit=_bounded_int(
                    body.get("limit"),
                    default=100,
                    minimum=0,
                    maximum=self.limits.maximum_results,
                    name="limit",
                ),
            )
            result = tests.to_dict()
            result_count = len(tests.selected_tests)
            truncated = tests.truncated
        elif selected is CodeIndexOperation.INVALIDATE:
            transaction_id = _required_text(
                body.get("transaction_id"),
                "transaction_id",
                maximum=1_024,
            )
            changed_paths = _string_tuple(
                body.get("changed_paths"),
                "changed_paths",
                maximum_items=self.limits.maximum_changed_paths,
            )
            deleted_paths = _string_tuple(
                body.get("deleted_paths"),
                "deleted_paths",
                maximum_items=self.limits.maximum_changed_paths,
            )
            build = runtime.invalidate_patch(
                transaction_id=transaction_id,
                changed_paths=changed_paths,
                deleted_paths=deleted_paths,
                rebuild=_boolean(body.get("rebuild"), default=True, name="rebuild"),
            )
            rebuilt = build is not None
            result = {
                "accepted": True,
                "idempotent_replay": build is None
                and runtime.store.has_invalidation(
                    runtime.source.identity.workspace_id,
                    transaction_id,
                ),
                "build": build.to_dict() if build is not None else None,
            }
            if build is not None:
                warnings = build.warnings
                result_count = build.file_count
        else:
            build = runtime.reconcile_invalidations()
            rebuilt = build is not None
            result = {
                "reconciled": build is not None,
                "build": build.to_dict() if build is not None else None,
                "pending_invalidations": len(
                    runtime.store.pending_invalidations(runtime.source.identity.workspace_id)
                ),
            }
            if build is not None:
                warnings = build.warnings
                result_count = build.file_count

        status = runtime.status()
        state = status.get("index") if isinstance(status.get("index"), Mapping) else {}
        generation = int(state.get("active_generation") or 0)
        finished = time.time_ns()
        receipt = CodeIndexReceipt(
            request_id=_request_id(request_id, normalized_task_id, selected, started),
            operation=selected,
            task_id=normalized_task_id,
            workspace_id=runtime.source.identity.workspace_id,
            workspace_revision=runtime.source.identity.revision,
            index_generation=generation,
            started_at_ns=started,
            finished_at_ns=finished,
            rebuilt=rebuilt,
            result_count=result_count,
            truncated=truncated,
            warnings=tuple(dict.fromkeys(item for item in warnings if item)),
            causation_id=str(causation_id or ""),
            metadata={
                "filesystem_source": "BoundWorkspaceSource",
                "physical_root_exposed": False,
                "subprocess_search": False,
                "network_required": False,
                "lsp_auto_install": False,
            },
        )
        return CodeIndexServiceResponse(status=200, body=result, receipt=receipt)

    def _content_query(self, payload: Mapping[str, Any]) -> ContentSearchQuery:
        pattern = _required_text(
            payload.get("pattern"),
            "pattern",
            maximum=self.limits.maximum_pattern_chars,
        )
        try:
            mode = ContentSearchMode(str(payload.get("mode") or ContentSearchMode.LITERAL.value))
        except ValueError as error:
            raise CodeIndexRequestError(
                "mode must be literal or regex",
                details={"mode": payload.get("mode")},
            ) from error
        budget_payload = _mapping(payload.get("budget"), "budget")
        budget = SearchBudget(
            maximum_results=_bounded_int(
                budget_payload.get("maximum_results"),
                default=200,
                minimum=0,
                maximum=self.limits.maximum_results,
                name="maximum_results",
            ),
            maximum_candidate_files=_bounded_int(
                budget_payload.get("maximum_candidate_files"),
                default=10_000,
                minimum=1,
                maximum=self.limits.maximum_discovery_files,
                name="maximum_candidate_files",
            ),
            maximum_scanned_bytes=_bounded_int(
                budget_payload.get("maximum_scanned_bytes"),
                default=128 * 1024 * 1024,
                minimum=1,
                maximum=self.limits.maximum_discovery_bytes,
                name="maximum_scanned_bytes",
            ),
            maximum_match_chars=_bounded_int(
                budget_payload.get("maximum_match_chars"),
                default=4_000,
                minimum=0,
                maximum=self.limits.maximum_output_chars,
                name="maximum_match_chars",
            ),
            maximum_output_chars=_bounded_int(
                budget_payload.get("maximum_output_chars"),
                default=100_000,
                minimum=0,
                maximum=self.limits.maximum_output_chars,
                name="maximum_output_chars",
            ),
            maximum_line_chars=_bounded_int(
                budget_payload.get("maximum_line_chars"),
                default=20_000,
                minimum=1,
                maximum=self.limits.maximum_output_chars,
                name="maximum_line_chars",
            ),
            maximum_regex_chars=min(self.limits.maximum_pattern_chars, 8_000),
            context_lines=_bounded_int(
                budget_payload.get("context_lines"),
                default=2,
                minimum=0,
                maximum=100,
                name="context_lines",
            ),
            head_limit=_bounded_int(
                budget_payload.get("head_limit"),
                default=0,
                minimum=0,
                maximum=self.limits.maximum_results,
                name="head_limit",
            ),
            offset=_bounded_int(
                budget_payload.get("offset"),
                default=0,
                minimum=0,
                maximum=10_000_000,
                name="offset",
            ),
            deadline_ms=_bounded_int(
                budget_payload.get("deadline_ms"),
                default=10_000,
                minimum=1,
                maximum=self.limits.maximum_deadline_ms,
                name="deadline_ms",
            ),
        )
        return ContentSearchQuery(
            pattern=pattern,
            mode=mode,
            case_sensitive=_boolean(payload.get("case_sensitive"), default=False, name="case_sensitive"),
            multiline=_boolean(payload.get("multiline"), default=False, name="multiline"),
            whole_word=_boolean(payload.get("whole_word"), default=False, name="whole_word"),
            file_globs=_string_tuple(payload.get("file_globs"), "file_globs", self.limits.maximum_globs),
            languages=_string_tuple(payload.get("languages"), "languages", self.limits.maximum_languages),
            suffixes=_string_tuple(payload.get("suffixes"), "suffixes", self.limits.maximum_languages),
            paths=_string_tuple(payload.get("paths"), "paths", self.limits.maximum_paths),
            budget=budget,
        ).validated()

    def _symbol_query(self, payload: Mapping[str, Any]) -> SymbolQuery:
        raw_capability = str(payload.get("capability") or SymbolCapability.WORKSPACE_SYMBOL.value)
        try:
            capability = SymbolCapability(raw_capability)
        except ValueError as error:
            raise CodeIndexRequestError(
                f"unsupported symbol capability: {raw_capability!r}",
                details={"capabilities": [item.value for item in SymbolCapability]},
            ) from error
        return SymbolQuery(
            capability=capability,
            name=_optional_text(payload.get("name"), "name", maximum=self.limits.maximum_pattern_chars),
            logical_path=_optional_text(payload.get("logical_path"), "logical_path", maximum=32_768),
            line=_bounded_int(payload.get("line"), default=0, minimum=0, maximum=100_000_000, name="line"),
            column=_bounded_int(payload.get("column"), default=0, minimum=0, maximum=10_000_000, name="column"),
            symbol_id=_optional_text(payload.get("symbol_id"), "symbol_id", maximum=2_048),
            limit=_bounded_int(
                payload.get("limit"),
                default=100,
                minimum=1,
                maximum=self.limits.maximum_symbol_results,
                name="limit",
            ),
        ).validated()

    def _discovery_query(self, payload: Mapping[str, Any]) -> FileDiscoveryQuery:
        budget_payload = _mapping(payload.get("budget"), "budget")
        budget = DiscoveryBudget(
            maximum_files=_bounded_int(
                budget_payload.get("maximum_files"),
                default=20_000,
                minimum=1,
                maximum=self.limits.maximum_discovery_files,
                name="maximum_files",
            ),
            maximum_directories=_bounded_int(
                budget_payload.get("maximum_directories"),
                default=10_000,
                minimum=1,
                maximum=self.limits.maximum_discovery_files,
                name="maximum_directories",
            ),
            maximum_depth=_bounded_int(
                budget_payload.get("maximum_depth"),
                default=64,
                minimum=1,
                maximum=1_024,
                name="maximum_depth",
            ),
            maximum_total_bytes=_bounded_int(
                budget_payload.get("maximum_total_bytes"),
                default=512 * 1024 * 1024,
                minimum=1,
                maximum=self.limits.maximum_discovery_bytes,
                name="maximum_total_bytes",
            ),
            maximum_file_bytes=_bounded_int(
                budget_payload.get("maximum_file_bytes"),
                default=4 * 1024 * 1024,
                minimum=1,
                maximum=self.limits.maximum_file_bytes,
                name="maximum_file_bytes",
            ),
            maximum_path_chars=_bounded_int(
                budget_payload.get("maximum_path_chars"),
                default=1_024,
                minimum=1,
                maximum=32_768,
                name="maximum_path_chars",
            ),
            deadline_ms=_bounded_int(
                budget_payload.get("deadline_ms"),
                default=30_000,
                minimum=1,
                maximum=self.limits.maximum_deadline_ms,
                name="deadline_ms",
            ),
        )
        return FileDiscoveryQuery(
            globs=_string_tuple(
                payload.get("globs", ["**/*"]),
                "globs",
                self.limits.maximum_globs,
                required=True,
            ),
            include_suffixes=_string_tuple(
                payload.get("include_suffixes"),
                "include_suffixes",
                self.limits.maximum_languages,
            ),
            exclude_globs=_string_tuple(
                payload.get("exclude_globs"),
                "exclude_globs",
                self.limits.maximum_globs,
            ),
            include_hidden=_boolean(payload.get("include_hidden"), default=False, name="include_hidden"),
            include_generated=_boolean(
                payload.get("include_generated"),
                default=False,
                name="include_generated",
            ),
            include_vendor=_boolean(payload.get("include_vendor"), default=False, name="include_vendor"),
            cursor=_optional_text(payload.get("cursor"), "cursor", maximum=32_768),
            page_size=_bounded_int(
                payload.get("page_size"),
                default=500,
                minimum=1,
                maximum=10_000,
                name="page_size",
            ),
            budget=budget,
        ).validated()


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CodeIndexRequestError(f"{name} must be an object")
    return value


def _required_text(value: Any, name: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise CodeIndexRequestError(f"{name} must be a string")
    normalized = value.strip()
    if not normalized:
        raise CodeIndexRequestError(f"{name} is required")
    if len(normalized) > maximum:
        raise CodeIndexRequestError(f"{name} exceeds {maximum} characters")
    if "\x00" in normalized:
        raise CodeIndexRequestError(f"{name} contains NUL")
    return normalized


def _optional_text(value: Any, name: str, *, maximum: int) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str):
        raise CodeIndexRequestError(f"{name} must be a string")
    if len(value) > maximum:
        raise CodeIndexRequestError(f"{name} exceeds {maximum} characters")
    if "\x00" in value:
        raise CodeIndexRequestError(f"{name} contains NUL")
    return value


def _string_tuple(
    value: Any,
    name: str,
    maximum_items: int,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if value is None:
        items: Sequence[Any] = ()
    elif isinstance(value, str):
        items = (value,)
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        items = value
    else:
        raise CodeIndexRequestError(f"{name} must be a string or array of strings")
    if len(items) > maximum_items:
        raise CodeIndexRequestError(f"{name} exceeds {maximum_items} items")
    normalized: list[str] = []
    for index, item in enumerate(items):
        normalized.append(_required_text(item, f"{name}[{index}]", maximum=32_768))
    result = tuple(dict.fromkeys(normalized))
    if required and not result:
        raise CodeIndexRequestError(f"{name} requires at least one item")
    return result


def _bounded_int(
    value: Any,
    *,
    default: int,
    minimum: int,
    maximum: int,
    name: str,
) -> int:
    if value is None:
        result = default
    elif isinstance(value, bool):
        raise CodeIndexRequestError(f"{name} must be an integer")
    else:
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise CodeIndexRequestError(f"{name} must be an integer") from error
    if result < minimum or result > maximum:
        raise CodeIndexRequestError(f"{name} must be between {minimum} and {maximum}")
    return result


def _boolean(value: Any, *, default: bool, name: str) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    raise CodeIndexRequestError(f"{name} must be a boolean")


def _request_id(
    supplied: str,
    task_id: str,
    operation: CodeIndexOperation,
    started_at_ns: int,
) -> str:
    if supplied:
        return _required_text(supplied, "request_id", maximum=1_024)
    return f"codeidx_{stable_digest(task_id, operation.value, started_at_ns)[:32]}"
