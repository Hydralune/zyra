from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from .models import (
    CallEdge,
    CodeSymbol,
    PathPolicyError,
    SourceLocation,
    SymbolCapability,
    SymbolKind,
    SymbolProvenance,
    SymbolQuery,
    SymbolQueryResult,
    SymbolReference,
    WorkspaceIdentity,
    stable_digest,
)
from .workspace_source import BoundWorkspaceSource


class LanguageServerClient(Protocol):
    @property
    def server_id(self) -> str:
        ...

    def request(self, method: str, params: Mapping[str, Any]) -> Any:
        ...

    def notify(self, method: str, params: Mapping[str, Any]) -> None:
        ...


@dataclass(frozen=True, slots=True)
class LspAdapterStatus:
    configured: bool
    available: bool
    server_id: str = ""
    reason: str = ""
    capabilities: tuple[SymbolCapability, ...] = ()
    auto_install: bool = False
    auto_start_process: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "available": self.available,
            "server_id": self.server_id,
            "reason": self.reason,
            "capabilities": [item.value for item in self.capabilities],
            "auto_install": self.auto_install,
            "auto_start_process": self.auto_start_process,
        }


class LspAdapter:
    def query(self, identity: WorkspaceIdentity, query: SymbolQuery) -> SymbolQueryResult:
        raise NotImplementedError

    def file_changed(self, logical_path: str, content: str, *, version: int) -> None:
        raise NotImplementedError

    def file_deleted(self, logical_path: str) -> None:
        raise NotImplementedError

    def status(self) -> LspAdapterStatus:
        raise NotImplementedError


class UnavailableLspAdapter(LspAdapter):
    def __init__(self, reason: str = "no language server client explicitly configured") -> None:
        self.reason = reason

    def query(self, identity: WorkspaceIdentity, query: SymbolQuery) -> SymbolQueryResult:
        return SymbolQueryResult(
            query=query.validated(),
            provenance=SymbolProvenance.STRUCTURAL_FALLBACK,
            degraded=True,
            degradation_reason=self.reason,
            workspace_revision=identity.revision,
        )

    def file_changed(self, logical_path: str, content: str, *, version: int) -> None:
        del logical_path, content, version

    def file_deleted(self, logical_path: str) -> None:
        del logical_path

    def status(self) -> LspAdapterStatus:
        return LspAdapterStatus(configured=False, available=False, reason=self.reason)


class InjectedLspAdapter(LspAdapter):
    """Typed adapter for an already-created, explicitly injected LSP client.

    It never discovers binaries, installs servers, opens ports, or starts a
    subprocess.  Those actions belong to a later capability-gated runtime.  The
    adapter converts physical URIs back through WorkspacePathPolicy before any
    result is admitted to the derived code index.
    """

    METHOD_BY_CAPABILITY: Mapping[SymbolCapability, str] = {
        SymbolCapability.DOCUMENT_SYMBOL: "textDocument/documentSymbol",
        SymbolCapability.WORKSPACE_SYMBOL: "workspace/symbol",
        SymbolCapability.DEFINITION: "textDocument/definition",
        SymbolCapability.REFERENCES: "textDocument/references",
        SymbolCapability.IMPLEMENTATION: "textDocument/implementation",
        SymbolCapability.HOVER: "textDocument/hover",
        SymbolCapability.CALL_HIERARCHY: "callHierarchy/outgoingCalls",
    }

    def __init__(
        self,
        source: BoundWorkspaceSource,
        client: LanguageServerClient,
        *,
        capabilities: Sequence[SymbolCapability],
    ) -> None:
        self.source = source
        self.client = client
        self.capabilities = tuple(sorted(set(capabilities), key=str))

    def query(self, identity: WorkspaceIdentity, query: SymbolQuery) -> SymbolQueryResult:
        request = query.validated()
        if identity.workspace_id != self.source.identity.workspace_id:
            raise ValueError("LSP query belongs to another workspace")
        if identity.revision != self.source.identity.revision:
            raise ValueError("LSP query workspace revision is stale")
        if request.capability not in self.capabilities:
            return SymbolQueryResult(
                query=request,
                provenance=SymbolProvenance.LSP,
                degraded=True,
                degradation_reason=f"language server does not declare {request.capability.value}",
                workspace_revision=identity.revision,
            )
        method = self.METHOD_BY_CAPABILITY[request.capability]
        params = self._params(request)
        try:
            response = self.client.request(method, params)
        except Exception as error:  # noqa: BLE001 - explicit degradation, not silent fallback.
            return SymbolQueryResult(
                query=request,
                provenance=SymbolProvenance.LSP,
                degraded=True,
                degradation_reason=f"LSP request failed: {type(error).__name__}: {error}",
                workspace_revision=identity.revision,
            )
        return self._convert(request, response, identity)

    def file_changed(self, logical_path: str, content: str, *, version: int) -> None:
        uri = self.source.policy.resolve(logical_path).as_uri()
        self.client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": int(version)},
                "contentChanges": [{"text": content}],
            },
        )
        self.client.notify("textDocument/didSave", {"textDocument": {"uri": uri}})

    def file_deleted(self, logical_path: str) -> None:
        uri = self.source.policy.resolve(logical_path, require_exists=False).as_uri()
        self.client.notify(
            "workspace/didChangeWatchedFiles",
            {"changes": [{"uri": uri, "type": 3}]},
        )

    def status(self) -> LspAdapterStatus:
        return LspAdapterStatus(
            configured=True,
            available=True,
            server_id=self.client.server_id,
            capabilities=self.capabilities,
            auto_install=False,
            auto_start_process=False,
        )

    def _params(self, query: SymbolQuery) -> Mapping[str, Any]:
        if query.capability is SymbolCapability.WORKSPACE_SYMBOL:
            return {"query": query.name}
        if query.capability is SymbolCapability.CALL_HIERARCHY:
            return {"item": {"name": query.name, "data": {"symbol_id": query.symbol_id}}}
        logical = query.logical_path
        uri = self.source.policy.resolve(logical).as_uri() if logical else ""
        params: dict[str, Any] = {
            "textDocument": {"uri": uri},
            "position": {"line": max(0, query.line - 1), "character": max(0, query.column)},
        }
        if query.capability is SymbolCapability.REFERENCES:
            params["context"] = {"includeDeclaration": True}
        return params

    def _convert(
        self,
        query: SymbolQuery,
        response: Any,
        identity: WorkspaceIdentity,
    ) -> SymbolQueryResult:
        items = response if isinstance(response, list) else ([response] if isinstance(response, Mapping) else [])
        symbols: list[CodeSymbol] = []
        references: list[SymbolReference] = []
        calls: list[CallEdge] = []
        hover: dict[str, Any] = {}
        warnings: list[str] = []
        for raw in items[: query.limit]:
            if not isinstance(raw, Mapping):
                continue
            try:
                if query.capability is SymbolCapability.HOVER:
                    hover = self._hover(raw)
                elif query.capability is SymbolCapability.REFERENCES:
                    references.append(self._reference(raw, identity))
                elif query.capability is SymbolCapability.CALL_HIERARCHY:
                    calls.append(self._call(raw))
                else:
                    symbols.append(self._symbol(raw, identity))
            except (KeyError, TypeError, ValueError, OSError, PathPolicyError) as error:
                warnings.append(f"invalid_lsp_result:{type(error).__name__}")
        return SymbolQueryResult(
            query=query,
            symbols=tuple(symbols),
            references=tuple(references),
            call_edges=tuple(calls),
            hover=hover,
            provenance=SymbolProvenance.LSP,
            degraded=bool(warnings),
            degradation_reason=",".join(sorted(set(warnings))),
            workspace_revision=identity.revision,
        )

    def _symbol(self, raw: Mapping[str, Any], identity: WorkspaceIdentity) -> CodeSymbol:
        name = str(raw.get("name") or "")
        location = self._location(raw.get("location") or raw)
        qualified = str(raw.get("detail") or raw.get("containerName") or name)
        kind = self._kind(raw.get("kind"))
        return CodeSymbol(
            symbol_id=f"symbol_{stable_digest(identity.workspace_id, qualified, kind.value, location.to_dict())[:32]}",
            workspace_id=identity.workspace_id,
            name=name,
            qualified_name=qualified,
            kind=kind,
            language="lsp",
            location=location,
            signature=str(raw.get("detail") or ""),
            container_name=str(raw.get("containerName") or ""),
            source_revision=identity.revision,
            provenance=SymbolProvenance.LSP,
            metadata={"server_id": self.client.server_id},
        )

    def _reference(self, raw: Mapping[str, Any], identity: WorkspaceIdentity) -> SymbolReference:
        location = self._location(raw)
        name = str(raw.get("name") or raw.get("symbol") or "")
        return SymbolReference(
            reference_id=f"reference_{stable_digest(identity.workspace_id, name, location.to_dict())[:32]}",
            workspace_id=identity.workspace_id,
            symbol_name=name,
            location=location,
            reference_kind="lsp_reference",
            file_hash="",
            source_revision=identity.revision,
            generation=0,
            provenance=SymbolProvenance.LSP,
            metadata={"server_id": self.client.server_id},
        )

    def _call(self, raw: Mapping[str, Any]) -> CallEdge:
        target = raw.get("to") if isinstance(raw.get("to"), Mapping) else raw
        name = str(target.get("name") or "")
        location = self._location(target)
        caller = str(raw.get("caller_symbol_id") or raw.get("from", {}).get("data", {}).get("symbol_id") or "")
        return CallEdge(
            caller_symbol_id=caller,
            callee_name=name,
            location=location,
            provenance=SymbolProvenance.LSP,
        )

    def _location(self, raw: Mapping[str, Any]) -> SourceLocation:
        uri = str(raw.get("uri") or raw.get("targetUri") or "")
        if not uri:
            uri = str(raw.get("location", {}).get("uri") or "")
        if not uri.startswith("file:"):
            raise ValueError("LSP result does not contain a local file URI")
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        physical = Path(unquote(parsed.path))
        if os.name == "nt" and physical.as_posix().startswith("/") and re.match(r"/[A-Za-z]:", physical.as_posix()):
            physical = Path(physical.as_posix()[1:])
        logical = self.source.policy.logical_from_physical(physical)
        range_value = raw.get("range") or raw.get("targetSelectionRange") or raw.get("location", {}).get("range") or {}
        start = range_value.get("start") if isinstance(range_value, Mapping) else {}
        end = range_value.get("end") if isinstance(range_value, Mapping) else {}
        return SourceLocation(
            logical_path=logical,
            line_start=int(start.get("line", 0)) + 1,
            line_end=int(end.get("line", start.get("line", 0))) + 1,
            column_start=int(start.get("character", 0)),
            column_end=int(end.get("character", 0)),
        )

    @staticmethod
    def _hover(raw: Mapping[str, Any]) -> dict[str, Any]:
        contents = raw.get("contents")
        if isinstance(contents, str):
            text = contents
        elif isinstance(contents, Mapping):
            text = str(contents.get("value") or contents.get("language") or "")
        elif isinstance(contents, list):
            text = "\n".join(
                str(item.get("value") if isinstance(item, Mapping) else item)
                for item in contents
            )
        else:
            text = ""
        return {"contents": text[:20_000], "provider": "lsp"}

    @staticmethod
    def _kind(value: Any) -> SymbolKind:
        mapping = {
            3: SymbolKind.NAMESPACE,
            5: SymbolKind.CLASS,
            6: SymbolKind.METHOD,
            7: SymbolKind.PROPERTY,
            12: SymbolKind.FUNCTION,
            13: SymbolKind.VARIABLE,
            14: SymbolKind.CONSTANT,
            11: SymbolKind.INTERFACE,
        }
        return mapping.get(int(value or 0), SymbolKind.UNKNOWN)
