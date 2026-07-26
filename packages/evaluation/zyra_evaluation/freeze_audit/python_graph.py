from __future__ import annotations

import ast
import re
import tokenize
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path, PurePosixPath
from typing import Any

from .model import (
    AuditSection,
    EdgeKind,
    EvidencePointer,
    GraphEdge,
    GraphNode,
    Language,
    NodeKind,
    RuleSwitches,
    Severity,
    content_digest,
    finding,
    relative_to,
    section,
)


WRITE_VERBS = frozenset(
    {
        "add",
        "append",
        "apply",
        "bind",
        "cancel",
        "claim",
        "close",
        "commit",
        "compare_and_swap",
        "create",
        "delete",
        "deny",
        "dispatch",
        "enqueue",
        "fail",
        "finalize",
        "grant",
        "insert",
        "invalidate",
        "mark",
        "merge",
        "mutate",
        "persist",
        "publish",
        "put",
        "record",
        "release",
        "remove",
        "renew",
        "replace",
        "resolve",
        "restore",
        "retry",
        "route",
        "save",
        "seal",
        "set",
        "snapshot",
        "start",
        "stop",
        "store",
        "transition",
        "update",
        "upsert",
        "verify",
        "write",
    }
)
READ_VERBS = frozenset(
    {
        "all",
        "by_id",
        "find",
        "get",
        "health",
        "inspect",
        "list",
        "load",
        "lookup",
        "peek",
        "project",
        "query",
        "read",
        "replay",
        "scan",
        "search",
        "status",
    }
)
EVENT_CALL_SUFFIXES = frozenset(
    {
        "append_event",
        "emit",
        "emit_event",
        "publish",
        "publish_event",
        "record_event",
        "send_event",
        "write_event",
    }
)
ROUTE_METHODS = frozenset({"get", "post", "put", "patch", "delete"})
NON_PRODUCTION_PARTS = frozenset(
    {
        "tests",
        "test",
        "fixtures",
        "mocks",
        "examples",
        "example",
        "demo",
        "demos",
        "docs",
        "benchmarks",
        "benchmark",
        "vendor",
        "vendor-runtimes",
        "source-pool",
        "runtime-sources",
    }
)
OWNER_CLAIM_PATTERN = re.compile(
    r"(?i)\b(?:canonical|authoritative|durable)\s+(?:state\s+)?owner\b"
)


@dataclass(frozen=True, slots=True)
class ImportFact:
    module: str
    name: str
    alias: str
    line: int
    level: int = 0
    dynamic: bool = False

    @property
    def local_name(self) -> str:
        return self.alias or self.name or self.module.rsplit(".", 1)[-1]

    def to_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "name": self.name,
            "alias": self.alias,
            "line": self.line,
            "level": self.level,
            "dynamic": self.dynamic,
            "local_name": self.local_name,
        }


@dataclass(frozen=True, slots=True)
class CallFact:
    scope: str
    name: str
    qualified_name: str
    line: int
    literal_arguments: tuple[Any, ...] = ()
    keywords: Mapping[str, Any] = field(default_factory=dict)

    @property
    def verb(self) -> str:
        return self.qualified_name.rsplit(".", 1)[-1].casefold()

    @property
    def write_like(self) -> bool:
        verb = self.verb
        return verb in WRITE_VERBS or any(
            verb.startswith(f"{prefix}_")
            for prefix in WRITE_VERBS
        )

    @property
    def read_like(self) -> bool:
        verb = self.verb
        return verb in READ_VERBS or any(
            verb.startswith(f"{prefix}_")
            for prefix in READ_VERBS
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "qualified_name": self.qualified_name,
            "line": self.line,
            "literal_arguments": list(self.literal_arguments),
            "keywords": dict(self.keywords),
            "verb": self.verb,
            "write_like": self.write_like,
            "read_like": self.read_like,
        }


@dataclass(frozen=True, slots=True)
class SymbolFact:
    name: str
    qualified_name: str
    kind: str
    line: int
    end_line: int
    parent: str = ""
    decorators: tuple[str, ...] = ()
    bases: tuple[str, ...] = ()
    executable_lines: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
            "parent": self.parent,
            "decorators": list(self.decorators),
            "bases": list(self.bases),
            "executable_lines": self.executable_lines,
        }


@dataclass(frozen=True, slots=True)
class AssignmentFact:
    scope: str
    target: str
    line: int
    persistent: bool
    value_kind: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "target": self.target,
            "line": self.line,
            "persistent": self.persistent,
            "value_kind": self.value_kind,
        }


@dataclass(frozen=True, slots=True)
class EventFact:
    scope: str
    event_name: str
    call: str
    line: int
    attributes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "event_name": self.event_name,
            "call": self.call,
            "line": self.line,
            "attributes": list(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class RouteFact:
    scope: str
    method: str
    route: str
    line: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "method": self.method,
            "route": self.route,
            "line": self.line,
        }


@dataclass(frozen=True, slots=True)
class PythonFileFacts:
    path: str
    module: str
    digest: str
    production: bool
    executable_lines: int
    symbols: tuple[SymbolFact, ...]
    imports: tuple[ImportFact, ...]
    calls: tuple[CallFact, ...]
    assignments: tuple[AssignmentFact, ...]
    events: tuple[EventFact, ...]
    routes: tuple[RouteFact, ...]
    string_literals: tuple[str, ...]
    owner_claim_lines: tuple[int, ...]
    parse_error: str = ""

    @property
    def symbol_names(self) -> frozenset[str]:
        return frozenset(
            value
            for item in self.symbols
            for value in (item.name, item.qualified_name)
        )

    @property
    def write_calls(self) -> tuple[CallFact, ...]:
        return tuple(item for item in self.calls if item.write_like)

    def contains_selector(self, selector: str) -> bool:
        if not selector:
            return True
        folded = selector.casefold()
        if any(folded in item.casefold() for item in self.string_literals):
            return True
        if any(folded in item.qualified_name.casefold() for item in self.calls):
            return True
        if any(folded in item.target.casefold() for item in self.assignments):
            return True
        return any(
            folded in value.casefold()
            for item in self.symbols
            for value in (item.name, item.qualified_name)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "digest": self.digest,
            "production": self.production,
            "executable_lines": self.executable_lines,
            "symbols": [item.to_dict() for item in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "assignments": [item.to_dict() for item in self.assignments],
            "events": [item.to_dict() for item in self.events],
            "routes": [item.to_dict() for item in self.routes],
            "string_literal_count": len(self.string_literals),
            "owner_claim_lines": list(self.owner_claim_lines),
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True, slots=True)
class PythonGraphResult:
    files: tuple[PythonFileFacts, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    section: AuditSection

    @property
    def by_path(self) -> Mapping[str, PythonFileFacts]:
        return {item.path: item for item in self.files}


class _Visitor(ast.NodeVisitor):
    def __init__(self, source: str) -> None:
        self.source = source
        self.lines = source.splitlines()
        self.scope: list[str] = []
        self.symbols: list[SymbolFact] = []
        self.imports: list[ImportFact] = []
        self.calls: list[CallFact] = []
        self.assignments: list[AssignmentFact] = []
        self.events: list[EventFact] = []
        self.routes: list[RouteFact] = []
        self.strings: list[str] = []

    @property
    def current_scope(self) -> str:
        return ".".join(self.scope)

    def visit_ClassDef(self, node: ast.ClassDef) -> Any:
        self._record_symbol(
            node,
            kind="class",
            decorators=tuple(self._name(item) for item in node.decorator_list),
            bases=tuple(self._name(item) for item in node.bases),
        )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        self._visit_function(node, "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        self._visit_function(node, "async_function")

    def _visit_function(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        kind: str,
    ) -> None:
        decorators = tuple(self._name(item) for item in node.decorator_list)
        self._record_symbol(node, kind=kind, decorators=decorators)
        scope = ".".join((*self.scope, node.name))
        for decorator in node.decorator_list:
            route = self._route_from_decorator(decorator)
            if route is not None:
                self.routes.append(
                    RouteFact(
                        scope=scope,
                        method=route[0],
                        route=route[1],
                        line=int(getattr(decorator, "lineno", node.lineno)),
                    )
                )
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_Import(self, node: ast.Import) -> Any:
        for alias in node.names:
            self.imports.append(
                ImportFact(
                    module=alias.name,
                    name="",
                    alias=alias.asname or "",
                    line=node.lineno,
                )
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> Any:
        module = node.module or ""
        for alias in node.names:
            self.imports.append(
                ImportFact(
                    module=module,
                    name=alias.name,
                    alias=alias.asname or "",
                    line=node.lineno,
                    level=node.level,
                )
            )

    def visit_Call(self, node: ast.Call) -> Any:
        qualified = self._name(node.func)
        name = qualified.rsplit(".", 1)[-1]
        literals = tuple(
            value
            for value in (self._literal(item) for item in node.args)
            if value is not _MISSING
        )
        keywords = {
            item.arg: value
            for item in node.keywords
            if item.arg
            for value in (self._literal(item.value),)
            if value is not _MISSING
        }
        self.calls.append(
            CallFact(
                scope=self.current_scope,
                name=name,
                qualified_name=qualified,
                line=node.lineno,
                literal_arguments=literals,
                keywords=keywords,
            )
        )
        if qualified in {
            "importlib.import_module",
            "__import__",
            "import_module",
        }:
            module = next(
                (item for item in literals if isinstance(item, str)),
                "",
            )
            self.imports.append(
                ImportFact(
                    module=module,
                    name="",
                    alias="",
                    line=node.lineno,
                    dynamic=True,
                )
            )
        event = self._event_from_call(qualified, literals, keywords)
        if event is not None:
            self.events.append(
                EventFact(
                    scope=self.current_scope,
                    event_name=event,
                    call=qualified,
                    line=node.lineno,
                    attributes=tuple(sorted(keywords)),
                )
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> Any:
        value_kind = type(node.value).__name__
        for target in node.targets:
            self._record_assignment(target, node.lineno, value_kind)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> Any:
        value_kind = type(node.value).__name__ if node.value is not None else "annotation"
        self._record_assignment(node.target, node.lineno, value_kind)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> Any:
        self._record_assignment(
            node.target,
            node.lineno,
            f"augmented_{type(node.op).__name__}",
        )
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> Any:
        self._record_assignment(node.target, node.lineno, "named_expression")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> Any:
        if isinstance(node.value, str):
            self.strings.append(node.value)

    def _record_symbol(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
        *,
        kind: str,
        decorators: tuple[str, ...] = (),
        bases: tuple[str, ...] = (),
    ) -> None:
        qualified = ".".join((*self.scope, node.name))
        self.symbols.append(
            SymbolFact(
                name=node.name,
                qualified_name=qualified,
                kind=kind,
                line=node.lineno,
                end_line=int(getattr(node, "end_lineno", node.lineno)),
                parent=self.current_scope,
                decorators=decorators,
                bases=bases,
                executable_lines=self._node_executable_lines(node),
            )
        )

    def _record_assignment(
        self,
        target: ast.AST,
        line: int,
        value_kind: str,
    ) -> None:
        selected = self._name(target)
        if not selected:
            return
        persistent = (
            selected.startswith(("self.", "cls."))
            or not self.scope
            or selected.rsplit(".", 1)[-1].casefold()
            in {
                "state",
                "status",
                "revision",
                "version",
                "epoch",
                "sequence",
                "checkpoint",
                "lease",
                "owner",
                "route",
                "permit",
            }
        )
        self.assignments.append(
            AssignmentFact(
                scope=self.current_scope,
                target=selected,
                line=line,
                persistent=persistent,
                value_kind=value_kind,
            )
        )

    def _route_from_decorator(self, node: ast.AST) -> tuple[str, str] | None:
        if not isinstance(node, ast.Call):
            return None
        qualified = self._name(node.func)
        method = qualified.rsplit(".", 1)[-1].casefold()
        if method not in ROUTE_METHODS:
            return None
        if not node.args:
            return None
        route = self._literal(node.args[0])
        if not isinstance(route, str) or not route.startswith("/"):
            return None
        return method.upper(), route

    @staticmethod
    def _event_from_call(
        qualified: str,
        literals: Sequence[Any],
        keywords: Mapping[str, Any],
    ) -> str | None:
        suffix = qualified.rsplit(".", 1)[-1].casefold()
        if suffix not in EVENT_CALL_SUFFIXES and not any(
            suffix.endswith(value) for value in EVENT_CALL_SUFFIXES
        ):
            return None
        for name in ("event_type", "event", "kind", "type", "name"):
            selected = keywords.get(name)
            if isinstance(selected, str) and selected:
                return selected
        return next(
            (item for item in literals if isinstance(item, str) and item),
            "<dynamic>",
        )

    @staticmethod
    def _name(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            prefix = _Visitor._name(node.value)
            return f"{prefix}.{node.attr}" if prefix else node.attr
        if isinstance(node, ast.Subscript):
            return _Visitor._name(node.value)
        if isinstance(node, ast.Call):
            return _Visitor._name(node.func)
        if isinstance(node, (ast.Tuple, ast.List)):
            return ",".join(_Visitor._name(item) for item in node.elts)
        return ""

    @staticmethod
    def _literal(node: ast.AST) -> Any:
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return _MISSING

    def _node_executable_lines(self, node: ast.AST) -> int:
        start = int(getattr(node, "lineno", 0))
        end = int(getattr(node, "end_lineno", start))
        if start <= 0 or end <= 0:
            return 0
        return executable_line_count(
            "\n".join(self.lines[start - 1 : end]),
            start_line=start,
        )


_MISSING = object()


class PythonGraphAnalyzer:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def analyze(self, paths: Iterable[str]) -> PythonGraphResult:
        findings = []
        evidence: list[EvidencePointer] = []
        facts: list[PythonFileFacts] = []
        for relative in sorted(
            {
                item.replace("\\", "/")
                for item in paths
                if Path(item).suffix.casefold() in {".py", ".pyi"}
            }
        ):
            selected = (self.root / relative).resolve(strict=False)
            try:
                selected.relative_to(self.root)
            except ValueError:
                continue
            if not selected.is_file():
                continue
            fact = self._analyze_file(selected, relative)
            facts.append(fact)
            if fact.parse_error:
                findings.append(
                    finding(
                        "python_graph_parse_error",
                        f"Python call graph cannot parse {relative}: {fact.parse_error}",
                        "reachability",
                        severity=Severity.BLOCKER,
                        path=relative,
                        owner_unit="M3-01B",
                        remediation=(
                            "Repair syntax or explicitly remove the file from the "
                            "production entry graph."
                        ),
                    )
                )
            evidence.append(
                EvidencePointer(
                    kind="python_source",
                    path=relative,
                    digest=fact.digest,
                    attributes={
                        "module": fact.module,
                        "symbols": len(fact.symbols),
                        "calls": len(fact.calls),
                        "events": len(fact.events),
                        "production": fact.production,
                    },
                )
            )
        nodes, edges = self._build_graph(facts)
        metrics = {
            "files": len(facts),
            "production_files": sum(item.production for item in facts),
            "parse_errors": sum(bool(item.parse_error) for item in facts),
            "symbols": sum(len(item.symbols) for item in facts),
            "imports": sum(len(item.imports) for item in facts),
            "calls": sum(len(item.calls) for item in facts),
            "write_calls": sum(len(item.write_calls) for item in facts),
            "assignments": sum(len(item.assignments) for item in facts),
            "events": sum(len(item.events) for item in facts),
            "routes": sum(len(item.routes) for item in facts),
            "owner_claims": sum(len(item.owner_claim_lines) for item in facts),
            "nodes": len(nodes),
            "edges": len(edges),
        }
        return PythonGraphResult(
            files=tuple(facts),
            nodes=nodes,
            edges=edges,
            section=section(
                "python_graph",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
            ),
        )

    def _analyze_file(self, path: Path, relative: str) -> PythonFileFacts:
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return PythonFileFacts(
                path=relative,
                module=module_name(relative),
                digest="",
                production=is_production_path(relative),
                executable_lines=0,
                symbols=(),
                imports=(),
                calls=(),
                assignments=(),
                events=(),
                routes=(),
                string_literals=(),
                owner_claim_lines=(),
                parse_error=f"{type(exc).__name__}: {exc}",
            )
        digest = content_digest(source)
        try:
            tree = ast.parse(source, filename=relative, type_comments=True)
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            return PythonFileFacts(
                path=relative,
                module=module_name(relative),
                digest=digest,
                production=is_production_path(relative),
                executable_lines=0,
                symbols=(),
                imports=(),
                calls=(),
                assignments=(),
                events=(),
                routes=(),
                string_literals=(),
                owner_claim_lines=tuple(
                    index
                    for index, line in enumerate(source.splitlines(), start=1)
                    if OWNER_CLAIM_PATTERN.search(line)
                ),
                parse_error=f"{type(exc).__name__}: {exc}",
            )
        visitor = _Visitor(source)
        visitor.visit(tree)
        return PythonFileFacts(
            path=relative,
            module=module_name(relative),
            digest=digest,
            production=is_production_path(relative),
            executable_lines=executable_line_count(source),
            symbols=tuple(visitor.symbols),
            imports=tuple(visitor.imports),
            calls=tuple(visitor.calls),
            assignments=tuple(visitor.assignments),
            events=tuple(visitor.events),
            routes=tuple(visitor.routes),
            string_literals=tuple(dict.fromkeys(visitor.strings)),
            owner_claim_lines=tuple(
                index
                for index, line in enumerate(source.splitlines(), start=1)
                if OWNER_CLAIM_PATTERN.search(line)
            ),
        )

    def _build_graph(
        self,
        facts: Sequence[PythonFileFacts],
    ) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        modules = {item.module: item for item in facts if item.module}
        symbols_by_name: dict[str, list[tuple[PythonFileFacts, SymbolFact]]] = (
            defaultdict(list)
        )
        for file_fact in facts:
            file_node = GraphNode(
                node_id=f"file:{file_fact.path}",
                kind=NodeKind.FILE,
                language=Language.PYTHON,
                path=file_fact.path,
                executable=bool(file_fact.executable_lines and not file_fact.parse_error),
                attributes={
                    "module": file_fact.module,
                    "production": file_fact.production,
                    "digest": file_fact.digest,
                },
            )
            nodes[file_node.node_id] = file_node
            module_id = f"module:{file_fact.module}"
            if file_fact.module:
                nodes[module_id] = GraphNode(
                    node_id=module_id,
                    kind=NodeKind.MODULE,
                    language=Language.PYTHON,
                    path=file_fact.path,
                    symbol=file_fact.module,
                    executable=file_node.executable,
                    attributes={"production": file_fact.production},
                )
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=module_id,
                        target=file_node.node_id,
                        kind=EdgeKind.CONTAINS,
                        path=file_fact.path,
                    ),
                )
            for symbol_fact in file_fact.symbols:
                node_id = f"symbol:{file_fact.path}#{symbol_fact.qualified_name}"
                nodes[node_id] = GraphNode(
                    node_id=node_id,
                    kind=NodeKind.SYMBOL,
                    language=Language.PYTHON,
                    path=file_fact.path,
                    symbol=symbol_fact.qualified_name,
                    executable=symbol_fact.executable_lines > 0,
                    attributes={
                        "kind": symbol_fact.kind,
                        "line": symbol_fact.line,
                        "end_line": symbol_fact.end_line,
                        "production": file_fact.production,
                    },
                )
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=file_node.node_id,
                        target=node_id,
                        kind=EdgeKind.CONTAINS,
                        path=file_fact.path,
                        line=symbol_fact.line,
                    ),
                )
                symbols_by_name[symbol_fact.name].append((file_fact, symbol_fact))
                symbols_by_name[symbol_fact.qualified_name].append(
                    (file_fact, symbol_fact)
                )
            for event in file_fact.events:
                event_id = f"event:{file_fact.path}#{event.event_name}@{event.line}"
                nodes[event_id] = GraphNode(
                    node_id=event_id,
                    kind=NodeKind.EVENT,
                    language=Language.PYTHON,
                    path=file_fact.path,
                    symbol=event.event_name,
                    attributes={
                        "scope": event.scope,
                        "call": event.call,
                        "line": event.line,
                        "production": file_fact.production,
                    },
                )
                source_id = self._scope_node_id(file_fact, event.scope, nodes)
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=source_id,
                        target=event_id,
                        kind=EdgeKind.EMITS,
                        path=file_fact.path,
                        line=event.line,
                    ),
                )
            for call in file_fact.write_calls:
                mutation_id = (
                    f"mutation:{file_fact.path}#{call.scope or '<module>'}@{call.line}"
                )
                nodes[mutation_id] = GraphNode(
                    node_id=mutation_id,
                    kind=NodeKind.MUTATION,
                    language=Language.PYTHON,
                    path=file_fact.path,
                    symbol=call.qualified_name,
                    attributes={
                        "scope": call.scope,
                        "line": call.line,
                        "production": file_fact.production,
                    },
                )
                source_id = self._scope_node_id(file_fact, call.scope, nodes)
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=source_id,
                        target=mutation_id,
                        kind=EdgeKind.MUTATES,
                        path=file_fact.path,
                        line=call.line,
                    ),
                )
        for file_fact in facts:
            source = f"module:{file_fact.module}" if file_fact.module else f"file:{file_fact.path}"
            alias_map: dict[str, str] = {}
            for imported in file_fact.imports:
                resolved = resolve_import_module(file_fact.module, imported)
                if imported.local_name:
                    alias_map[imported.local_name] = resolved
                target_module = longest_module_match(resolved, modules)
                if target_module:
                    self._add_edge(
                        edges,
                        GraphEdge(
                            source=source,
                            target=f"module:{target_module}",
                            kind=EdgeKind.IMPORTS,
                            path=file_fact.path,
                            line=imported.line,
                            attributes={
                                "requested": resolved,
                                "dynamic": imported.dynamic,
                            },
                        ),
                    )
            local_symbols = {
                item.name: f"symbol:{file_fact.path}#{item.qualified_name}"
                for item in file_fact.symbols
            }
            for call in file_fact.calls:
                caller = self._scope_node_id(file_fact, call.scope, nodes)
                target = local_symbols.get(call.name)
                if target is None:
                    root = call.qualified_name.split(".", 1)[0]
                    imported_module = alias_map.get(root, "")
                    if imported_module:
                        target_module = longest_module_match(imported_module, modules)
                        if target_module:
                            target = f"module:{target_module}"
                if target is None:
                    candidates = symbols_by_name.get(call.name, ())
                    if len(candidates) == 1:
                        target_fact, target_symbol = candidates[0]
                        target = (
                            f"symbol:{target_fact.path}#{target_symbol.qualified_name}"
                        )
                if target:
                    self._add_edge(
                        edges,
                        GraphEdge(
                            source=caller,
                            target=target,
                            kind=EdgeKind.CALLS,
                            path=file_fact.path,
                            line=call.line,
                            attributes={"call": call.qualified_name},
                        ),
                    )
        return (
            tuple(sorted(nodes.values(), key=lambda item: item.node_id)),
            tuple(
                sorted(
                    edges.values(),
                    key=lambda item: (
                        item.source,
                        item.target,
                        item.kind.value,
                        item.line,
                    ),
                )
            ),
        )

    @staticmethod
    def _scope_node_id(
        file_fact: PythonFileFacts,
        scope: str,
        nodes: Mapping[str, GraphNode],
    ) -> str:
        if scope:
            exact = f"symbol:{file_fact.path}#{scope}"
            if exact in nodes:
                return exact
            parts = scope.split(".")
            while parts:
                candidate = f"symbol:{file_fact.path}#{'.'.join(parts)}"
                if candidate in nodes:
                    return candidate
                parts.pop()
        module = f"module:{file_fact.module}"
        return module if module in nodes else f"file:{file_fact.path}"

    @staticmethod
    def _add_edge(
        selected: dict[str, GraphEdge],
        edge: GraphEdge,
    ) -> None:
        selected.setdefault(edge.fingerprint, edge)


def module_name(path: str) -> str:
    selected = PurePosixPath(path.replace("\\", "/"))
    if selected.suffix not in {".py", ".pyi"}:
        return ""
    parts = list(selected.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    for marker in (
        ("apps", "api"),
        ("packages", "evaluation"),
        ("packages", "integrations"),
        ("packages", "runtime"),
        ("packages", "memory"),
        ("packages", "scheduler"),
        ("packages", "orchestration"),
        ("packages", "skills"),
        ("packages", "workers"),
        ("packages", "workspace"),
        ("packages", "commands"),
        ("packages", "code_index"),
        ("packages", "core"),
        ("packages", "symbolic"),
    ):
        if tuple(parts[: len(marker)]) == marker:
            parts = parts[len(marker) :]
            break
    return ".".join(parts)


def resolve_import_module(current_module: str, fact: ImportFact) -> str:
    if not fact.level:
        base = fact.module
    else:
        current_parts = current_module.split(".")
        if current_parts:
            current_parts.pop()
        trim = max(0, fact.level - 1)
        if trim:
            current_parts = current_parts[:-trim] if trim <= len(current_parts) else []
        base = ".".join(
            [*current_parts, *([fact.module] if fact.module else [])]
        )
    if fact.name and fact.name != "*":
        return ".".join(item for item in (base, fact.name) if item)
    return base


def longest_module_match(
    requested: str,
    modules: Mapping[str, PythonFileFacts],
) -> str:
    selected = requested
    while selected:
        if selected in modules:
            return selected
        if "." not in selected:
            return ""
        selected = selected.rsplit(".", 1)[0]
    return ""


def is_production_path(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    parts = PurePosixPath(normalized).parts
    if any(part in NON_PRODUCTION_PARTS for part in parts):
        return False
    return bool(parts and parts[0] in {"apps", "packages", "scripts", "skills"})


def executable_line_count(source: str, *, start_line: int = 1) -> int:
    lines = source.splitlines()
    ignored: set[int] = set()
    try:
        for token in tokenize.generate_tokens(StringIO(source).readline):
            if token.type in {
                tokenize.COMMENT,
                tokenize.ENCODING,
                tokenize.ENDMARKER,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.NL,
                tokenize.NEWLINE,
            }:
                if token.type is tokenize.COMMENT:
                    ignored.update(
                        range(
                            token.start[0] + start_line - 1,
                            token.end[0] + start_line,
                        )
                    )
    except (tokenize.TokenError, IndentationError):
        pass
    count = 0
    for offset, raw in enumerate(lines):
        number = start_line + offset
        stripped = raw.strip()
        if not stripped or number in ignored:
            continue
        if stripped in {"pass", "..."}:
            continue
        if stripped.startswith(("'", '"')) and stripped.endswith(("'", '"')):
            continue
        count += 1
    return count
