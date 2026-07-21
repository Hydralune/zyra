from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_digest(*values: Any) -> str:
    digest = hashlib.sha256()
    for value in values:
        digest.update(canonical_json(value).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class CodeIndexError(RuntimeError):
    pass


class PathPolicyError(CodeIndexError):
    pass


class SearchBudgetError(CodeIndexError):
    pass


class StaleWorkspaceError(CodeIndexError):
    pass


class SymbolCapability(StrEnum):
    DOCUMENT_SYMBOL = "document_symbol"
    WORKSPACE_SYMBOL = "workspace_symbol"
    DEFINITION = "definition"
    REFERENCES = "references"
    IMPLEMENTATION = "implementation"
    HOVER = "hover"
    CALL_HIERARCHY = "call_hierarchy"


class SymbolProvenance(StrEnum):
    PYTHON_AST = "python_ast"
    STRUCTURAL_FALLBACK = "structural_fallback"
    LSP = "lsp"


class SymbolKind(StrEnum):
    MODULE = "module"
    NAMESPACE = "namespace"
    CLASS = "class"
    INTERFACE = "interface"
    FUNCTION = "function"
    METHOD = "method"
    PROPERTY = "property"
    VARIABLE = "variable"
    CONSTANT = "constant"
    IMPORT = "import"
    UNKNOWN = "unknown"


class FileDisposition(StrEnum):
    INDEXED = "indexed"
    IGNORED = "ignored"
    BINARY = "binary"
    TOO_LARGE = "too_large"
    OUTSIDE_WORKSPACE = "outside_workspace"
    SYMLINK = "symlink"
    UNREADABLE = "unreadable"
    GENERATED = "generated"
    VENDOR = "vendor"


class ContentSearchMode(StrEnum):
    LITERAL = "literal"
    REGEX = "regex"


@dataclass(frozen=True, slots=True)
class WorkspaceIdentity:
    workspace_id: str
    task_id: str
    run_id: str
    session_id: str
    root: str
    owner_epoch: int
    binding_revision: int
    lease_id: str = ""
    backend_id: str = ""

    @property
    def revision(self) -> str:
        return (
            f"workspace:{self.workspace_id}:{self.owner_epoch}:"
            f"{self.binding_revision}:{stable_digest(self.lease_id, self.backend_id)}"
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "owner_epoch": self.owner_epoch,
            "binding_revision": self.binding_revision,
            "lease_id": self.lease_id,
            "backend_id": self.backend_id,
            "root_redacted": True,
            "revision": self.revision,
        }


@dataclass(frozen=True, slots=True)
class DiscoveryBudget:
    maximum_files: int = 20_000
    maximum_directories: int = 10_000
    maximum_depth: int = 64
    maximum_total_bytes: int = 512 * 1024 * 1024
    maximum_file_bytes: int = 4 * 1024 * 1024
    maximum_path_chars: int = 1024
    deadline_ms: int = 30_000

    def validated(self) -> "DiscoveryBudget":
        values = (
            self.maximum_files,
            self.maximum_directories,
            self.maximum_depth,
            self.maximum_total_bytes,
            self.maximum_file_bytes,
            self.maximum_path_chars,
            self.deadline_ms,
        )
        if any(value <= 0 for value in values):
            raise SearchBudgetError("all discovery budgets must be positive")
        if self.maximum_file_bytes > self.maximum_total_bytes:
            raise SearchBudgetError("maximum_file_bytes cannot exceed maximum_total_bytes")
        return self


@dataclass(frozen=True, slots=True)
class SearchBudget:
    maximum_results: int = 200
    maximum_candidate_files: int = 10_000
    maximum_scanned_bytes: int = 128 * 1024 * 1024
    maximum_match_chars: int = 4_000
    maximum_output_chars: int = 100_000
    maximum_line_chars: int = 20_000
    maximum_regex_chars: int = 1_000
    context_lines: int = 2
    head_limit: int = 0
    offset: int = 0
    deadline_ms: int = 10_000

    def validated(self) -> "SearchBudget":
        if self.maximum_results < 0 or self.maximum_results > 100_000:
            raise SearchBudgetError("maximum_results must be between 0 and 100000")
        if self.maximum_candidate_files <= 0 or self.maximum_scanned_bytes <= 0:
            raise SearchBudgetError("candidate and byte budgets must be positive")
        if self.maximum_match_chars < 0 or self.maximum_output_chars < 0:
            raise SearchBudgetError("output budgets must be non-negative")
        if self.maximum_line_chars <= 0 or self.maximum_regex_chars <= 0:
            raise SearchBudgetError("line and regex budgets must be positive")
        if self.context_lines < 0 or self.context_lines > 100:
            raise SearchBudgetError("context_lines must be between 0 and 100")
        if self.head_limit < 0 or self.offset < 0:
            raise SearchBudgetError("head_limit and offset must be non-negative")
        if self.deadline_ms <= 0:
            raise SearchBudgetError("deadline_ms must be positive")
        return self


@dataclass(frozen=True, slots=True)
class FileDiscoveryQuery:
    globs: tuple[str, ...] = ("**/*",)
    include_suffixes: tuple[str, ...] = ()
    exclude_globs: tuple[str, ...] = ()
    include_hidden: bool = False
    include_generated: bool = False
    include_vendor: bool = False
    cursor: str = ""
    page_size: int = 500
    budget: DiscoveryBudget = field(default_factory=DiscoveryBudget)

    def validated(self) -> "FileDiscoveryQuery":
        self.budget.validated()
        if self.page_size <= 0 or self.page_size > 10_000:
            raise SearchBudgetError("page_size must be between 1 and 10000")
        if not self.globs:
            raise ValueError("at least one discovery glob is required")
        return self


@dataclass(frozen=True, slots=True)
class CodeFile:
    workspace_id: str
    logical_path: str
    language: str
    suffix: str
    size_bytes: int
    mtime_ns: int
    content_hash: str
    source_revision: str
    generation: int
    line_count: int
    disposition: FileDisposition = FileDisposition.INDEXED
    generated: bool = False
    vendor: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def file_id(self) -> str:
        return f"codefile_{stable_digest(self.workspace_id, self.logical_path)[:32]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "workspace_id": self.workspace_id,
            "logical_path": self.logical_path,
            "language": self.language,
            "suffix": self.suffix,
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "content_hash": self.content_hash,
            "source_revision": self.source_revision,
            "generation": self.generation,
            "line_count": self.line_count,
            "disposition": self.disposition.value,
            "generated": self.generated,
            "vendor": self.vendor,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class DiscoveryPage:
    files: tuple[CodeFile, ...]
    next_cursor: str = ""
    scanned_files: int = 0
    scanned_directories: int = 0
    scanned_bytes: int = 0
    ignored_count: int = 0
    truncated: bool = False
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "files": [item.to_dict() for item in self.files],
            "next_cursor": self.next_cursor,
            "scanned_files": self.scanned_files,
            "scanned_directories": self.scanned_directories,
            "scanned_bytes": self.scanned_bytes,
            "ignored_count": self.ignored_count,
            "truncated": self.truncated,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ContentSearchQuery:
    pattern: str
    mode: ContentSearchMode = ContentSearchMode.LITERAL
    case_sensitive: bool = False
    multiline: bool = False
    whole_word: bool = False
    file_globs: tuple[str, ...] = ()
    languages: tuple[str, ...] = ()
    suffixes: tuple[str, ...] = ()
    paths: tuple[str, ...] = ()
    budget: SearchBudget = field(default_factory=SearchBudget)

    def validated(self) -> "ContentSearchQuery":
        self.budget.validated()
        if not self.pattern:
            raise ValueError("search pattern cannot be empty")
        if self.mode is ContentSearchMode.REGEX and len(self.pattern) > self.budget.maximum_regex_chars:
            raise SearchBudgetError("regex exceeds maximum_regex_chars")
        return self


@dataclass(frozen=True, slots=True)
class ContentMatch:
    workspace_id: str
    logical_path: str
    line_start: int
    line_end: int
    column_start: int
    column_end: int
    preview: str
    matched_text: str
    file_hash: str
    source_revision: str
    generation: int
    score: float
    match_id: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.match_id:
            object.__setattr__(
                self,
                "match_id",
                f"codematch_{stable_digest(self.logical_path, self.line_start, self.column_start, self.matched_text)[:32]}",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "match_id": self.match_id,
            "workspace_id": self.workspace_id,
            "logical_path": self.logical_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "column_start": self.column_start,
            "column_end": self.column_end,
            "preview": self.preview,
            "matched_text": self.matched_text,
            "file_hash": self.file_hash,
            "source_revision": self.source_revision,
            "generation": self.generation,
            "score": self.score,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContentSearchResult:
    query: ContentSearchQuery
    matches: tuple[ContentMatch, ...]
    candidate_files: int
    scanned_files: int
    scanned_bytes: int
    elapsed_ms: float
    truncated: bool
    next_offset: int
    generation: int
    workspace_revision: str
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": {
                "pattern": self.query.pattern,
                "mode": self.query.mode.value,
                "case_sensitive": self.query.case_sensitive,
                "multiline": self.query.multiline,
                "whole_word": self.query.whole_word,
                "file_globs": list(self.query.file_globs),
                "languages": list(self.query.languages),
                "suffixes": list(self.query.suffixes),
                "paths": list(self.query.paths),
            },
            "matches": [item.to_dict() for item in self.matches],
            "candidate_files": self.candidate_files,
            "scanned_files": self.scanned_files,
            "scanned_bytes": self.scanned_bytes,
            "elapsed_ms": self.elapsed_ms,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
            "generation": self.generation,
            "workspace_revision": self.workspace_revision,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class SourceLocation:
    logical_path: str
    line_start: int
    line_end: int
    column_start: int = 0
    column_end: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_path": self.logical_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "column_start": self.column_start,
            "column_end": self.column_end,
        }


@dataclass(frozen=True, slots=True)
class CodeSymbol:
    symbol_id: str
    workspace_id: str
    name: str
    qualified_name: str
    kind: SymbolKind
    language: str
    location: SourceLocation
    signature: str = ""
    documentation: str = ""
    container_name: str = ""
    file_hash: str = ""
    source_revision: str = ""
    generation: int = 0
    provenance: SymbolProvenance = SymbolProvenance.STRUCTURAL_FALLBACK
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol_id": self.symbol_id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind.value,
            "language": self.language,
            "location": self.location.to_dict(),
            "signature": self.signature,
            "documentation": self.documentation,
            "container_name": self.container_name,
            "file_hash": self.file_hash,
            "source_revision": self.source_revision,
            "generation": self.generation,
            "provenance": self.provenance.value,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SymbolReference:
    reference_id: str
    workspace_id: str
    symbol_name: str
    location: SourceLocation
    reference_kind: str
    file_hash: str
    source_revision: str
    generation: int
    resolved_symbol_id: str = ""
    provenance: SymbolProvenance = SymbolProvenance.STRUCTURAL_FALLBACK
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference_id": self.reference_id,
            "workspace_id": self.workspace_id,
            "symbol_name": self.symbol_name,
            "location": self.location.to_dict(),
            "reference_kind": self.reference_kind,
            "file_hash": self.file_hash,
            "source_revision": self.source_revision,
            "generation": self.generation,
            "resolved_symbol_id": self.resolved_symbol_id,
            "provenance": self.provenance.value,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CallEdge:
    caller_symbol_id: str
    callee_name: str
    location: SourceLocation
    resolved_callee_symbol_id: str = ""
    provenance: SymbolProvenance = SymbolProvenance.STRUCTURAL_FALLBACK

    @property
    def edge_id(self) -> str:
        return f"calledge_{stable_digest(self.caller_symbol_id, self.callee_name, self.location.to_dict())[:32]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "edge_id": self.edge_id,
            "caller_symbol_id": self.caller_symbol_id,
            "callee_name": self.callee_name,
            "location": self.location.to_dict(),
            "resolved_callee_symbol_id": self.resolved_callee_symbol_id,
            "provenance": self.provenance.value,
        }


@dataclass(frozen=True, slots=True)
class SymbolQuery:
    capability: SymbolCapability
    name: str = ""
    logical_path: str = ""
    line: int = 0
    column: int = 0
    symbol_id: str = ""
    limit: int = 100

    def validated(self) -> "SymbolQuery":
        if self.limit <= 0 or self.limit > 10_000:
            raise SearchBudgetError("symbol query limit must be between 1 and 10000")
        if self.capability in {
            SymbolCapability.WORKSPACE_SYMBOL,
            SymbolCapability.DEFINITION,
            SymbolCapability.REFERENCES,
            SymbolCapability.IMPLEMENTATION,
        } and not (self.name or self.symbol_id):
            raise ValueError("symbol name or id is required")
        if self.capability is SymbolCapability.DOCUMENT_SYMBOL and not self.logical_path:
            raise ValueError("document_symbol requires logical_path")
        return self


@dataclass(frozen=True, slots=True)
class SymbolQueryResult:
    query: SymbolQuery
    symbols: tuple[CodeSymbol, ...] = ()
    references: tuple[SymbolReference, ...] = ()
    call_edges: tuple[CallEdge, ...] = ()
    hover: Mapping[str, Any] = field(default_factory=dict)
    provenance: SymbolProvenance = SymbolProvenance.STRUCTURAL_FALLBACK
    degraded: bool = False
    degradation_reason: str = ""
    generation: int = 0
    workspace_revision: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability": self.query.capability.value,
            "symbols": [item.to_dict() for item in self.symbols],
            "references": [item.to_dict() for item in self.references],
            "call_edges": [item.to_dict() for item in self.call_edges],
            "hover": dict(self.hover),
            "provenance": self.provenance.value,
            "degraded": self.degraded,
            "degradation_reason": self.degradation_reason,
            "generation": self.generation,
            "workspace_revision": self.workspace_revision,
        }


@dataclass(frozen=True, slots=True)
class CodeIndexBuildResult:
    workspace_id: str
    generation: int
    workspace_revision: str
    file_count: int
    symbol_count: int
    reference_count: int
    call_edge_count: int
    ignored_count: int
    content_digest: str
    changed_paths: tuple[str, ...] = ()
    deleted_paths: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "generation": self.generation,
            "workspace_revision": self.workspace_revision,
            "file_count": self.file_count,
            "symbol_count": self.symbol_count,
            "reference_count": self.reference_count,
            "call_edge_count": self.call_edge_count,
            "ignored_count": self.ignored_count,
            "content_digest": self.content_digest,
            "changed_paths": list(self.changed_paths),
            "deleted_paths": list(self.deleted_paths),
            "warnings": list(self.warnings),
        }
