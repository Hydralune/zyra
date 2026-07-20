from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

from .lsp_adapter import LspAdapter, UnavailableLspAdapter
from .models import (
    CodeIndexBuildResult,
    ContentSearchQuery,
    ContentSearchResult,
    FileDiscoveryQuery,
    SearchBudget,
    StaleWorkspaceError,
    SymbolCapability,
    SymbolQuery,
    SymbolQueryResult,
    WorkspaceIdentity,
    stable_digest,
)
from .store import CodeIndexStore
from .symbols import analyze_symbols
from .workspace_source import BoundWorkspaceSource, FileDiscoveryRuntime


@dataclass(frozen=True, slots=True)
class CodeContextSelection:
    query: str
    workspace_id: str
    generation: int
    workspace_revision: str
    source_refs: tuple[Mapping[str, Any], ...]
    excerpts: tuple[str, ...]
    total_chars: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "workspace_id": self.workspace_id,
            "generation": self.generation,
            "workspace_revision": self.workspace_revision,
            "source_refs": [dict(item) for item in self.source_refs],
            "excerpts": list(self.excerpts),
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "retrieval_scope": "task_workspace",
            "code_index_source": True,
            "query_digest": stable_digest(self.query),
        }


@dataclass(frozen=True, slots=True)
class TestSelectionResult:
    changed_paths: tuple[str, ...]
    selected_tests: tuple[str, ...]
    reasons: Mapping[str, tuple[str, ...]]
    generation: int
    workspace_revision: str
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "changed_paths": list(self.changed_paths),
            "selected_tests": list(self.selected_tests),
            "reasons": {key: list(value) for key, value in self.reasons.items()},
            "generation": self.generation,
            "workspace_revision": self.workspace_revision,
            "truncated": self.truncated,
        }


class CodeIndexRuntime:
    """Workspace-bound file/content/symbol derived index.

    Construction requires a BoundWorkspaceSource.  Production callers create
    it through WorkspaceManagerRuntime and a live access handle; the explicit
    test constructor is labelled and never used by the API path.
    """

    def __init__(
        self,
        source: BoundWorkspaceSource,
        *,
        index_path: str | Path,
        lsp_adapter: LspAdapter | None = None,
    ) -> None:
        self.source = source
        self.store = CodeIndexStore(index_path)
        self.discovery = FileDiscoveryRuntime(source)
        self.lsp_adapter = lsp_adapter or UnavailableLspAdapter()

    @classmethod
    def from_workspace_manager(
        cls,
        manager: Any,
        handle: Any,
        *,
        index_path: str | Path,
    ) -> "CodeIndexRuntime":
        return cls(BoundWorkspaceSource.from_manager(manager, handle), index_path=index_path)

    def rebuild(
        self,
        *,
        query: FileDiscoveryQuery | None = None,
        changed_paths: Sequence[str] = (),
        deleted_paths: Sequence[str] = (),
    ) -> CodeIndexBuildResult:
        generation = self.store.next_generation(self.source.identity)
        request = query or FileDiscoveryQuery(page_size=2_000)
        files_with_content: list[tuple[Any, str]] = []
        symbols: list[Any] = []
        references: list[Any] = []
        calls: list[Any] = []
        ignored = 0
        warnings: list[str] = []
        cursor = ""
        seen_paths: set[str] = set()
        while True:
            page = self.discovery.discover(replace(request, cursor=cursor), generation=generation)
            ignored += page.ignored_count
            warnings.extend(page.warnings)
            for file in page.files:
                if file.logical_path in seen_paths:
                    continue
                seen_paths.add(file.logical_path)
                text = self.source.read_text(
                    file.logical_path,
                    maximum_bytes=request.budget.maximum_file_bytes,
                )
                files_with_content.append((file, text))
                analysis = analyze_symbols(file, text)
                symbols.extend(analysis.symbols)
                references.extend(analysis.references)
                calls.extend(analysis.calls)
                warnings.extend(analysis.diagnostics)
            cursor = page.next_cursor
            if not cursor:
                break
            if len(seen_paths) >= request.budget.maximum_files:
                warnings.append("build_file_budget_reached")
                break
        digest = stable_digest(
            self.source.identity.revision,
            [(file.logical_path, file.content_hash, file.source_revision) for file, _ in files_with_content],
            [(symbol.symbol_id, symbol.source_revision) for symbol in symbols],
        )
        self.store.publish(
            self.source.identity,
            generation,
            files_with_content,
            symbols,
            references,
            calls,
            content_digest=digest,
        )
        self.store.mark_invalidations_applied(self.source.identity.workspace_id, generation)
        return CodeIndexBuildResult(
            workspace_id=self.source.identity.workspace_id,
            generation=generation,
            workspace_revision=self.source.identity.revision,
            file_count=len(files_with_content),
            symbol_count=len(symbols),
            reference_count=len(references),
            call_edge_count=len(calls),
            ignored_count=ignored,
            content_digest=digest,
            changed_paths=tuple(sorted(set(changed_paths))),
            deleted_paths=tuple(sorted(set(deleted_paths))),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def ensure_current(self) -> CodeIndexBuildResult | None:
        state = self.store.workspace_state(self.source.identity.workspace_id)
        if state and str(state.get("workspace_revision") or "") == self.source.identity.revision:
            return None
        return self.rebuild()

    def invalidate_patch(
        self,
        *,
        transaction_id: str,
        changed_paths: Sequence[str],
        deleted_paths: Sequence[str] = (),
        rebuild: bool = True,
    ) -> CodeIndexBuildResult | None:
        normalized_changed = tuple(
            sorted({self.source.policy.normalize_logical(path) for path in changed_paths})
        )
        normalized_deleted = tuple(
            sorted({self.source.policy.normalize_logical(path) for path in deleted_paths})
        )
        inserted = self.store.record_invalidation(
            self.source.identity,
            transaction_id=transaction_id,
            changed_paths=normalized_changed,
            deleted_paths=normalized_deleted,
        )
        if not inserted:
            return None
        if rebuild:
            return self.rebuild(
                changed_paths=normalized_changed,
                deleted_paths=normalized_deleted,
            )
        return None

    def reconcile_invalidations(self) -> CodeIndexBuildResult | None:
        pending = self.store.pending_invalidations(self.source.identity.workspace_id)
        if not pending:
            return None
        changed: set[str] = set()
        deleted: set[str] = set()
        for item in pending:
            changed.update(str(path) for path in item["changed_paths"])
            deleted.update(str(path) for path in item["deleted_paths"])
        return self.rebuild(changed_paths=tuple(changed), deleted_paths=tuple(deleted))

    def search(self, query: ContentSearchQuery) -> ContentSearchResult:
        self._require_current_revision()
        return self.store.search_content(self.source.identity, query)

    def symbols(self, query: SymbolQuery) -> SymbolQueryResult:
        self._require_current_revision()
        status = self.lsp_adapter.status()
        if status.available and query.capability in status.capabilities:
            lsp_result = self.lsp_adapter.query(self.source.identity, query)
            state = self.store.workspace_state(self.source.identity.workspace_id)
            if not lsp_result.degraded:
                return replace(
                    lsp_result,
                    generation=int(state.get("active_generation") or 0),
                )
            fallback = self.store.query_symbols(self.source.identity, query)
            return replace(
                fallback,
                degraded=True,
                degradation_reason=(
                    f"lsp_degraded:{lsp_result.degradation_reason};"
                    f"fallback:{fallback.provenance.value}"
                ),
            )
        return self.store.query_symbols(self.source.identity, query)

    def context(
        self,
        query: str,
        *,
        maximum_chars: int = 16_000,
        maximum_results: int = 20,
        file_globs: Sequence[str] = (),
    ) -> CodeContextSelection:
        if maximum_chars < 0 or maximum_results < 0:
            raise ValueError("context budgets must be non-negative")
        result = self.search(
            ContentSearchQuery(
                pattern=query,
                file_globs=tuple(file_globs),
                budget=SearchBudget(
                    maximum_results=maximum_results,
                    maximum_output_chars=maximum_chars,
                    maximum_match_chars=min(4_000, maximum_chars),
                    head_limit=maximum_results,
                ),
            )
        )
        excerpts: list[str] = []
        refs: list[Mapping[str, Any]] = []
        chars = 0
        truncated = result.truncated
        for match in result.matches:
            header = f"[{match.logical_path}:{match.line_start}]"
            excerpt = f"{header}\n{match.preview}"
            if chars + len(excerpt) > maximum_chars:
                truncated = True
                break
            excerpts.append(excerpt)
            refs.append(
                {
                    "logical_path": match.logical_path,
                    "line_start": match.line_start,
                    "line_end": match.line_end,
                    "file_hash": match.file_hash,
                    "source_revision": match.source_revision,
                    "generation": match.generation,
                }
            )
            chars += len(excerpt)
        return CodeContextSelection(
            query=query,
            workspace_id=self.source.identity.workspace_id,
            generation=result.generation,
            workspace_revision=result.workspace_revision,
            source_refs=tuple(refs),
            excerpts=tuple(excerpts),
            total_chars=chars,
            truncated=truncated,
        )

    def select_tests(
        self,
        changed_paths: Sequence[str],
        *,
        limit: int = 100,
    ) -> TestSelectionResult:
        self._require_current_revision()
        candidates = [
            file.logical_path
            for file in self.store.files(self.source.identity.workspace_id)
            if self._looks_like_test(file.logical_path)
        ]
        reasons: dict[str, set[str]] = {}
        for raw_path in changed_paths:
            path = self.source.policy.normalize_logical(raw_path)
            stem = Path(path).stem.casefold()
            module = stem.removeprefix("test_").removesuffix("_test")
            definition = self.symbols(
                SymbolQuery(
                    capability=SymbolCapability.DOCUMENT_SYMBOL,
                    logical_path=path,
                    limit=1_000,
                )
            )
            names = {symbol.name.casefold() for symbol in definition.symbols}
            for test_path in candidates:
                test_stem = Path(test_path).stem.casefold()
                selected_reasons: list[str] = []
                if module and module in test_stem:
                    selected_reasons.append(f"filename_affinity:{path}")
                if path.rsplit("/", 1)[0] and test_path.startswith(path.rsplit("/", 1)[0]):
                    selected_reasons.append(f"same_directory:{path}")
                if names:
                    content = self.store.file_content(self.source.identity.workspace_id, test_path)
                    if content and any(name in content[1].casefold() for name in names):
                        selected_reasons.append(f"symbol_reference:{path}")
                if selected_reasons:
                    reasons.setdefault(test_path, set()).update(selected_reasons)
        ordered = sorted(reasons)
        truncated = len(ordered) > max(0, int(limit))
        selected = ordered[: max(0, int(limit))]
        state = self.store.workspace_state(self.source.identity.workspace_id)
        return TestSelectionResult(
            changed_paths=tuple(sorted(set(changed_paths))),
            selected_tests=tuple(selected),
            reasons={path: tuple(sorted(reasons[path])) for path in selected},
            generation=int(state.get("active_generation") or 0),
            workspace_revision=str(state.get("workspace_revision") or self.source.identity.revision),
            truncated=truncated,
        )

    def status(self) -> Mapping[str, Any]:
        state = dict(self.store.workspace_state(self.source.identity.workspace_id))
        lsp_status = self.lsp_adapter.status().to_dict()
        lsp_status["fallback"] = "python_ast_or_bounded_structural_parser"
        return {
            "workspace": self.source.identity.to_public_dict(),
            "index": state,
            "pending_invalidations": len(
                self.store.pending_invalidations(self.source.identity.workspace_id)
            ),
            "canonical_owner": "WorkspaceManagerRuntime+WorkspaceFileRevision",
            "derived_state": True,
            "rebuildable": True,
            "lsp": lsp_status,
        }

    def _require_current_revision(self) -> None:
        state = self.store.workspace_state(self.source.identity.workspace_id)
        if not state:
            raise StaleWorkspaceError("code index is not built")
        if str(state.get("workspace_revision") or "") != self.source.identity.revision:
            raise StaleWorkspaceError("code index belongs to a stale workspace binding revision")

    @staticmethod
    def _looks_like_test(path: str) -> bool:
        lowered = path.casefold()
        name = Path(path).name.casefold()
        return (
            "/tests/" in f"/{lowered}/"
            or "/test/" in f"/{lowered}/"
            or name.startswith("test_")
            or ".test." in name
            or ".spec." in name
            or name.endswith("_test.py")
        )
