from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .models import (
    CallEdge,
    CodeFile,
    CodeSymbol,
    SourceLocation,
    SymbolKind,
    SymbolProvenance,
    SymbolReference,
    stable_digest,
)


IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]*\b")
CALL_PATTERN = re.compile(r"\b([A-Za-z_$][A-Za-z0-9_$.]*)\s*\(")


@dataclass(frozen=True, slots=True)
class SymbolAnalysis:
    symbols: tuple[CodeSymbol, ...]
    references: tuple[SymbolReference, ...]
    calls: tuple[CallEdge, ...]
    diagnostics: tuple[str, ...] = ()


def _location(
    logical_path: str,
    node: ast.AST,
    *,
    default_end: int | None = None,
) -> SourceLocation:
    line = int(getattr(node, "lineno", 1))
    end_line = int(getattr(node, "end_lineno", default_end or line))
    column = int(getattr(node, "col_offset", 0))
    end_column = int(getattr(node, "end_col_offset", column))
    return SourceLocation(
        logical_path=logical_path,
        line_start=line,
        line_end=max(line, end_line),
        column_start=column,
        column_end=max(column, end_column),
    )


def _symbol_id(file: CodeFile, qualified_name: str, kind: SymbolKind, location: SourceLocation) -> str:
    return f"symbol_{stable_digest(file.workspace_id, file.logical_path, qualified_name, kind.value, location.to_dict())[:32]}"


def _reference_id(file: CodeFile, name: str, location: SourceLocation, kind: str) -> str:
    return f"reference_{stable_digest(file.workspace_id, file.logical_path, name, location.to_dict(), kind)[:32]}"


class PythonSymbolAnalyzer(ast.NodeVisitor):
    def __init__(self, file: CodeFile, text: str) -> None:
        self.file = file
        self.text = text
        self.lines = text.splitlines()
        self.symbols: list[CodeSymbol] = []
        self.references: list[SymbolReference] = []
        self.calls: list[CallEdge] = []
        self.scope: list[str] = []
        self.symbol_stack: list[str] = []
        self.definitions: dict[str, list[str]] = {}

    def analyze(self) -> SymbolAnalysis:
        try:
            tree = ast.parse(self.text, filename=self.file.logical_path, type_comments=True)
        except (SyntaxError, ValueError) as error:
            return SymbolAnalysis((), (), (), (f"python_parse_failed:{type(error).__name__}:{error}",))
        module_location = SourceLocation(
            logical_path=self.file.logical_path,
            line_start=1,
            line_end=max(1, self.file.line_count),
        )
        module_name = self.file.logical_path.replace("/", ".").rsplit(".", 1)[0]
        module = CodeSymbol(
            symbol_id=_symbol_id(self.file, module_name, SymbolKind.MODULE, module_location),
            workspace_id=self.file.workspace_id,
            name=module_name.rsplit(".", 1)[-1],
            qualified_name=module_name,
            kind=SymbolKind.MODULE,
            language="python",
            location=module_location,
            file_hash=self.file.content_hash,
            source_revision=self.file.source_revision,
            generation=self.file.generation,
            provenance=SymbolProvenance.PYTHON_AST,
        )
        self.symbols.append(module)
        self.symbol_stack.append(module.symbol_id)
        self.visit(tree)
        self.symbol_stack.pop()
        by_name: dict[str, list[CodeSymbol]] = {}
        for symbol in self.symbols:
            by_name.setdefault(symbol.name, []).append(symbol)
        references = [
            SymbolReference(
                reference_id=item.reference_id,
                workspace_id=item.workspace_id,
                symbol_name=item.symbol_name,
                location=item.location,
                reference_kind=item.reference_kind,
                file_hash=item.file_hash,
                source_revision=item.source_revision,
                generation=item.generation,
                resolved_symbol_id=(
                    sorted(by_name[item.symbol_name], key=lambda symbol: symbol.qualified_name)[0].symbol_id
                    if item.symbol_name in by_name
                    else ""
                ),
                provenance=item.provenance,
                metadata=item.metadata,
            )
            for item in self.references
        ]
        calls = [
            CallEdge(
                caller_symbol_id=item.caller_symbol_id,
                callee_name=item.callee_name,
                location=item.location,
                resolved_callee_symbol_id=(
                    sorted(by_name[item.callee_name.rsplit(".", 1)[-1]], key=lambda symbol: symbol.qualified_name)[0].symbol_id
                    if item.callee_name.rsplit(".", 1)[-1] in by_name
                    else ""
                ),
                provenance=item.provenance,
            )
            for item in self.calls
        ]
        return SymbolAnalysis(
            symbols=tuple(sorted(self.symbols, key=lambda item: (item.location.line_start, item.qualified_name))),
            references=tuple(sorted(references, key=lambda item: (item.location.line_start, item.location.column_start, item.symbol_name))),
            calls=tuple(sorted(calls, key=lambda item: (item.location.line_start, item.callee_name))),
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self._visit_definition(node, node.name, SymbolKind.CLASS, self._class_signature(node))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        kind = SymbolKind.METHOD if self.scope and self._scope_is_class() else SymbolKind.FUNCTION
        self._visit_definition(node, node.name, kind, self._function_signature(node))

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        kind = SymbolKind.METHOD if self.scope and self._scope_is_class() else SymbolKind.FUNCTION
        self._visit_definition(node, node.name, kind, f"async {self._function_signature(node)}")

    def visit_Name(self, node: ast.Name) -> Any:
        kind = "write" if isinstance(node.ctx, (ast.Store, ast.Del)) else "read"
        self._add_reference(node.id, _location(self.file.logical_path, node), kind)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        full = self._attribute_name(node)
        if full:
            self._add_reference(full, _location(self.file.logical_path, node), "attribute")
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            name = alias.asname or alias.name.rsplit(".", 1)[-1]
            self._add_import_symbol(name, alias.name, node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            qualified = f"{module}.{alias.name}" if module else alias.name
            self._add_import_symbol(name, qualified, node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        for target in node.targets:
            if isinstance(target, ast.Name) and self._is_module_or_class_scope():
                kind = SymbolKind.CONSTANT if target.id.isupper() else SymbolKind.VARIABLE
                self._add_variable_symbol(target.id, target, kind)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        if isinstance(node.target, ast.Name) and self._is_module_or_class_scope():
            kind = SymbolKind.CONSTANT if node.target.id.isupper() else SymbolKind.VARIABLE
            self._add_variable_symbol(node.target.id, node, kind)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        callee = self._expression_name(node.func)
        if callee and self.symbol_stack:
            self.calls.append(
                CallEdge(
                    caller_symbol_id=self.symbol_stack[-1],
                    callee_name=callee,
                    location=_location(self.file.logical_path, node.func),
                    provenance=SymbolProvenance.PYTHON_AST,
                )
            )
        self.generic_visit(node)

    def _visit_definition(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        name: str,
        kind: SymbolKind,
        signature: str,
    ) -> None:
        qualified = ".".join((*self.scope, name)) if self.scope else name
        location = _location(self.file.logical_path, node)
        symbol = CodeSymbol(
            symbol_id=_symbol_id(self.file, qualified, kind, location),
            workspace_id=self.file.workspace_id,
            name=name,
            qualified_name=qualified,
            kind=kind,
            language="python",
            location=location,
            signature=signature,
            documentation=ast.get_docstring(node, clean=True) or "",
            container_name=".".join(self.scope),
            file_hash=self.file.content_hash,
            source_revision=self.file.source_revision,
            generation=self.file.generation,
            provenance=SymbolProvenance.PYTHON_AST,
            metadata={
                "decorators": [self._expression_name(item) for item in node.decorator_list],
            },
        )
        self.symbols.append(symbol)
        self.definitions.setdefault(name, []).append(symbol.symbol_id)
        self.scope.append(name)
        self.symbol_stack.append(symbol.symbol_id)
        for child in node.body:
            self.visit(child)
        self.symbol_stack.pop()
        self.scope.pop()

    def _add_reference(self, name: str, location: SourceLocation, kind: str) -> None:
        simple = name.rsplit(".", 1)[-1]
        self.references.append(
            SymbolReference(
                reference_id=_reference_id(self.file, name, location, kind),
                workspace_id=self.file.workspace_id,
                symbol_name=simple,
                location=location,
                reference_kind=kind,
                file_hash=self.file.content_hash,
                source_revision=self.file.source_revision,
                generation=self.file.generation,
                provenance=SymbolProvenance.PYTHON_AST,
                metadata={"expression": name},
            )
        )

    def _add_import_symbol(self, name: str, qualified: str, node: ast.AST) -> None:
        location = _location(self.file.logical_path, node)
        self.symbols.append(
            CodeSymbol(
                symbol_id=_symbol_id(self.file, qualified, SymbolKind.IMPORT, location),
                workspace_id=self.file.workspace_id,
                name=name,
                qualified_name=qualified,
                kind=SymbolKind.IMPORT,
                language="python",
                location=location,
                signature=f"import {qualified}",
                container_name=".".join(self.scope),
                file_hash=self.file.content_hash,
                source_revision=self.file.source_revision,
                generation=self.file.generation,
                provenance=SymbolProvenance.PYTHON_AST,
            )
        )

    def _add_variable_symbol(self, name: str, node: ast.AST, kind: SymbolKind) -> None:
        qualified = ".".join((*self.scope, name)) if self.scope else name
        location = _location(self.file.logical_path, node)
        self.symbols.append(
            CodeSymbol(
                symbol_id=_symbol_id(self.file, qualified, kind, location),
                workspace_id=self.file.workspace_id,
                name=name,
                qualified_name=qualified,
                kind=kind,
                language="python",
                location=location,
                container_name=".".join(self.scope),
                file_hash=self.file.content_hash,
                source_revision=self.file.source_revision,
                generation=self.file.generation,
                provenance=SymbolProvenance.PYTHON_AST,
            )
        )

    def _function_signature(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        try:
            args = ast.unparse(node.args)
            returns = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
            return f"def {node.name}({args}){returns}"
        except Exception:  # noqa: BLE001
            return f"def {node.name}(...)"

    @staticmethod
    def _class_signature(node: ast.ClassDef) -> str:
        try:
            bases = ", ".join(ast.unparse(item) for item in node.bases)
        except Exception:  # noqa: BLE001
            bases = ""
        return f"class {node.name}({bases})" if bases else f"class {node.name}"

    def _scope_is_class(self) -> bool:
        if not self.scope:
            return False
        name = self.scope[-1]
        return any(symbol.name == name and symbol.kind is SymbolKind.CLASS for symbol in self.symbols)

    def _is_module_or_class_scope(self) -> bool:
        return not self.scope or self._scope_is_class()

    @classmethod
    def _attribute_name(cls, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = cls._attribute_name(node.value)
            return f"{base}.{node.attr}" if base else node.attr
        return ""

    @classmethod
    def _expression_name(cls, node: ast.AST) -> str:
        if isinstance(node, (ast.Name, ast.Attribute)):
            return cls._attribute_name(node)
        try:
            return ast.unparse(node)[:200]
        except Exception:  # noqa: BLE001
            return ""


STRUCTURAL_DEFINITIONS: Mapping[str, tuple[tuple[SymbolKind, re.Pattern[str]], ...]] = {
    "typescript": (
        (SymbolKind.CLASS, re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")),
        (SymbolKind.INTERFACE, re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_$][\w$]*)")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(")),
    ),
    "javascript": (
        (SymbolKind.CLASS, re.compile(r"^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(")),
    ),
    "go": (
        (SymbolKind.CLASS, re.compile(r"^\s*type\s+([A-Za-z_][\w]*)\s+struct\b")),
        (SymbolKind.INTERFACE, re.compile(r"^\s*type\s+([A-Za-z_][\w]*)\s+interface\b")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)\s*\(")),
    ),
    "rust": (
        (SymbolKind.CLASS, re.compile(r"^\s*(?:pub\s+)?struct\s+([A-Za-z_][\w]*)")),
        (SymbolKind.INTERFACE, re.compile(r"^\s*(?:pub\s+)?trait\s+([A-Za-z_][\w]*)")),
        (SymbolKind.FUNCTION, re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+([A-Za-z_][\w]*)\s*\(")),
    ),
}


def analyze_structural(file: CodeFile, text: str) -> SymbolAnalysis:
    language = file.language.replace("react", "")
    if language.startswith("typescript"):
        language = "typescript"
    if language.startswith("javascript"):
        language = "javascript"
    patterns = STRUCTURAL_DEFINITIONS.get(language, ())
    symbols: list[CodeSymbol] = []
    references: list[SymbolReference] = []
    calls: list[CallEdge] = []
    symbol_by_line: list[tuple[int, CodeSymbol]] = []
    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        for kind, pattern in patterns:
            match = pattern.search(line)
            if not match:
                continue
            name = match.group(1)
            location = SourceLocation(
                logical_path=file.logical_path,
                line_start=line_number,
                line_end=line_number,
                column_start=match.start(1),
                column_end=match.end(1),
            )
            symbol = CodeSymbol(
                symbol_id=_symbol_id(file, name, kind, location),
                workspace_id=file.workspace_id,
                name=name,
                qualified_name=name,
                kind=kind,
                language=file.language,
                location=location,
                signature=line.strip()[:500],
                file_hash=file.content_hash,
                source_revision=file.source_revision,
                generation=file.generation,
                provenance=SymbolProvenance.STRUCTURAL_FALLBACK,
                metadata={"parser": "bounded_structural_regex", "semantic_precision": "degraded"},
            )
            symbols.append(symbol)
            symbol_by_line.append((line_number, symbol))
            break
    definitions = {symbol.name: symbol for symbol in symbols}
    for line_number, line in enumerate(lines, start=1):
        caller = _nearest_symbol(symbol_by_line, line_number)
        for match in IDENTIFIER_PATTERN.finditer(line):
            name = match.group(0)
            location = SourceLocation(
                logical_path=file.logical_path,
                line_start=line_number,
                line_end=line_number,
                column_start=match.start(),
                column_end=match.end(),
            )
            references.append(
                SymbolReference(
                    reference_id=_reference_id(file, name, location, "text_identifier"),
                    workspace_id=file.workspace_id,
                    symbol_name=name,
                    location=location,
                    reference_kind="text_identifier",
                    file_hash=file.content_hash,
                    source_revision=file.source_revision,
                    generation=file.generation,
                    resolved_symbol_id=definitions[name].symbol_id if name in definitions else "",
                    provenance=SymbolProvenance.STRUCTURAL_FALLBACK,
                    metadata={"semantic_precision": "degraded"},
                )
            )
        if caller:
            for match in CALL_PATTERN.finditer(line):
                callee = match.group(1)
                simple = callee.rsplit(".", 1)[-1]
                location = SourceLocation(
                    logical_path=file.logical_path,
                    line_start=line_number,
                    line_end=line_number,
                    column_start=match.start(1),
                    column_end=match.end(1),
                )
                calls.append(
                    CallEdge(
                        caller_symbol_id=caller.symbol_id,
                        callee_name=callee,
                        location=location,
                        resolved_callee_symbol_id=definitions[simple].symbol_id if simple in definitions else "",
                        provenance=SymbolProvenance.STRUCTURAL_FALLBACK,
                    )
                )
    return SymbolAnalysis(
        symbols=tuple(symbols),
        references=tuple(references),
        calls=tuple(calls),
        diagnostics=("lsp_unavailable:bounded_structural_fallback",),
    )


def analyze_symbols(file: CodeFile, text: str) -> SymbolAnalysis:
    if file.language == "python":
        analysis = PythonSymbolAnalyzer(file, text).analyze()
        if analysis.symbols:
            return analysis
    return analyze_structural(file, text)


def _nearest_symbol(symbols: Sequence[tuple[int, CodeSymbol]], line: int) -> CodeSymbol | None:
    candidate: CodeSymbol | None = None
    for start, symbol in symbols:
        if start > line:
            break
        candidate = symbol
    return candidate
