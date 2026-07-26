from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from zyra_integrations.source_custody.javascript_analyzer import (
    JavaScriptLexer,
    JavaScriptParser,
    Token,
)

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
    section,
)
from .python_graph import NON_PRODUCTION_PARTS, WRITE_VERBS


SCRIPT_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
DECLARATION_KEYWORDS = frozenset(
    {
        "class",
        "function",
        "interface",
        "type",
        "enum",
        "namespace",
    }
)
VARIABLE_KEYWORDS = frozenset({"const", "let", "var"})
MODIFIERS = frozenset(
    {
        "export",
        "default",
        "declare",
        "abstract",
        "async",
        "public",
        "private",
        "protected",
        "readonly",
        "static",
        "override",
    }
)
ENTRY_CALLS = frozenset(
    {
        "createRoot",
        "hydrateRoot",
        "render",
        "listen",
        "serve",
        "start",
        "main",
        "run",
        "bootstrap",
        "register",
    }
)
EVENT_SUFFIXES = frozenset(
    {
        "append",
        "appendEvent",
        "dispatch",
        "emit",
        "emitEvent",
        "publish",
        "publishEvent",
        "record",
        "recordEvent",
        "send",
        "writeEvent",
    }
)
OWNER_CLAIM_PATTERN = re.compile(
    r"(?i)\b(?:canonical|authoritative|durable)\s+(?:state\s+)?owner\b"
)


@dataclass(frozen=True, slots=True)
class ScriptImportFact:
    specifier: str
    line: int
    dynamic: bool
    local_names: tuple[str, ...]
    type_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "specifier": self.specifier,
            "line": self.line,
            "dynamic": self.dynamic,
            "local_names": list(self.local_names),
            "type_only": self.type_only,
        }


@dataclass(frozen=True, slots=True)
class ScriptSymbolFact:
    name: str
    qualified_name: str
    kind: str
    line: int
    end_line: int
    parent: str = ""
    exported: bool = False
    default_export: bool = False
    executable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "qualified_name": self.qualified_name,
            "kind": self.kind,
            "line": self.line,
            "end_line": self.end_line,
            "parent": self.parent,
            "exported": self.exported,
            "default_export": self.default_export,
            "executable": self.executable,
        }


@dataclass(frozen=True, slots=True)
class ScriptCallFact:
    scope: str
    callee: str
    line: int
    arguments: tuple[str, ...]
    literals: tuple[str, ...]
    object_keys: tuple[str, ...]

    @property
    def verb(self) -> str:
        return self.callee.rsplit(".", 1)[-1]

    @property
    def write_like(self) -> bool:
        folded = self.verb.casefold()
        return folded in WRITE_VERBS or any(
            folded.startswith(f"{verb}_")
            or folded.startswith(verb)
            and len(folded) > len(verb)
            and folded[len(verb)].isupper()
            for verb in WRITE_VERBS
        )

    @property
    def entry_like(self) -> bool:
        return self.verb in ENTRY_CALLS

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "callee": self.callee,
            "line": self.line,
            "arguments": list(self.arguments),
            "literals": list(self.literals),
            "object_keys": list(self.object_keys),
            "write_like": self.write_like,
            "entry_like": self.entry_like,
        }


@dataclass(frozen=True, slots=True)
class ScriptAssignmentFact:
    scope: str
    target: str
    line: int
    persistent: bool
    operator: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "target": self.target,
            "line": self.line,
            "persistent": self.persistent,
            "operator": self.operator,
        }


@dataclass(frozen=True, slots=True)
class ScriptEventFact:
    scope: str
    event_name: str
    callee: str
    line: int
    attributes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "event_name": self.event_name,
            "callee": self.callee,
            "line": self.line,
            "attributes": list(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class ScriptFileFacts:
    path: str
    module: str
    language: Language
    digest: str
    production: bool
    executable_lines: int
    symbols: tuple[ScriptSymbolFact, ...]
    imports: tuple[ScriptImportFact, ...]
    calls: tuple[ScriptCallFact, ...]
    assignments: tuple[ScriptAssignmentFact, ...]
    events: tuple[ScriptEventFact, ...]
    strings: tuple[str, ...]
    tokens: int
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
    def write_calls(self) -> tuple[ScriptCallFact, ...]:
        return tuple(item for item in self.calls if item.write_like)

    def contains_selector(self, selector: str) -> bool:
        if not selector:
            return True
        folded = selector.casefold()
        return (
            any(folded in item.casefold() for item in self.strings)
            or any(folded in item.callee.casefold() for item in self.calls)
            or any(folded in item.target.casefold() for item in self.assignments)
            or any(
                folded in value.casefold()
                for item in self.symbols
                for value in (item.name, item.qualified_name)
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "module": self.module,
            "language": self.language.value,
            "digest": self.digest,
            "production": self.production,
            "executable_lines": self.executable_lines,
            "symbols": [item.to_dict() for item in self.symbols],
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "assignments": [item.to_dict() for item in self.assignments],
            "events": [item.to_dict() for item in self.events],
            "string_count": len(self.strings),
            "tokens": self.tokens,
            "owner_claim_lines": list(self.owner_claim_lines),
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True, slots=True)
class ScriptGraphResult:
    files: tuple[ScriptFileFacts, ...]
    nodes: tuple[GraphNode, ...]
    edges: tuple[GraphEdge, ...]
    section: AuditSection

    @property
    def by_path(self) -> Mapping[str, ScriptFileFacts]:
        return {item.path: item for item in self.files}


class ScriptGraphAnalyzer:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def analyze(self, paths: Iterable[str]) -> ScriptGraphResult:
        files: list[ScriptFileFacts] = []
        findings = []
        evidence: list[EvidencePointer] = []
        selected_paths = sorted(
            {
                item.replace("\\", "/")
                for item in paths
                if Path(item).suffix.casefold() in SCRIPT_SUFFIXES
            }
        )
        for relative in selected_paths:
            path = (self.root / relative).resolve(strict=False)
            try:
                path.relative_to(self.root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            fact = self._analyze_file(path, relative)
            files.append(fact)
            if fact.parse_error:
                findings.append(
                    finding(
                        "script_graph_parse_error",
                        (
                            "TypeScript/JavaScript call graph cannot tokenize "
                            f"{relative}: {fact.parse_error}"
                        ),
                        "reachability",
                        severity=Severity.BLOCKER,
                        path=relative,
                        owner_unit="M3-01B",
                        remediation=(
                            "Repair lexical syntax or remove the file from the "
                            "production entry graph."
                        ),
                    )
                )
            evidence.append(
                EvidencePointer(
                    kind="script_source",
                    path=relative,
                    digest=fact.digest,
                    attributes={
                        "module": fact.module,
                        "language": fact.language.value,
                        "symbols": len(fact.symbols),
                        "calls": len(fact.calls),
                        "events": len(fact.events),
                        "production": fact.production,
                    },
                )
            )
        nodes, edges = self._build_graph(files)
        metrics = {
            "files": len(files),
            "production_files": sum(item.production for item in files),
            "parse_errors": sum(bool(item.parse_error) for item in files),
            "symbols": sum(len(item.symbols) for item in files),
            "imports": sum(len(item.imports) for item in files),
            "calls": sum(len(item.calls) for item in files),
            "write_calls": sum(len(item.write_calls) for item in files),
            "assignments": sum(len(item.assignments) for item in files),
            "events": sum(len(item.events) for item in files),
            "entry_calls": sum(
                item.entry_like for file_fact in files for item in file_fact.calls
            ),
            "owner_claims": sum(len(item.owner_claim_lines) for item in files),
            "nodes": len(nodes),
            "edges": len(edges),
        }
        return ScriptGraphResult(
            files=tuple(files),
            nodes=nodes,
            edges=edges,
            section=section(
                "script_graph",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
            ),
        )

    def _analyze_file(self, path: Path, relative: str) -> ScriptFileFacts:
        language = language_for_path(relative)
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            return ScriptFileFacts(
                path=relative,
                module=script_module_name(relative),
                language=language,
                digest="",
                production=is_production_script(relative),
                executable_lines=0,
                symbols=(),
                imports=(),
                calls=(),
                assignments=(),
                events=(),
                strings=(),
                tokens=0,
                owner_claim_lines=(),
                parse_error=f"{type(exc).__name__}: {exc}",
            )
        digest = content_digest(source)
        try:
            tokens = tuple(JavaScriptLexer(source).tokens())
            parser = JavaScriptParser(tokens)
            parsed_imports = parser.imports()
            parsed_calls = parser.calls()
        except (ValueError, IndexError, RecursionError, MemoryError) as exc:
            return ScriptFileFacts(
                path=relative,
                module=script_module_name(relative),
                language=language,
                digest=digest,
                production=is_production_script(relative),
                executable_lines=0,
                symbols=(),
                imports=(),
                calls=(),
                assignments=(),
                events=(),
                strings=(),
                tokens=0,
                owner_claim_lines=(),
                parse_error=f"{type(exc).__name__}: {exc}",
            )
        symbols = discover_symbols(tokens)
        scopes = scope_by_line(symbols)
        imports = tuple(
            ScriptImportFact(
                specifier=item.specifier,
                line=item.line,
                dynamic=item.dynamic,
                local_names=import_local_names(tokens, item.line, item.specifier),
                type_only=import_type_only(tokens, item.line),
            )
            for item in parsed_imports
        )
        calls = tuple(
            ScriptCallFact(
                scope=scope_at(scopes, item.line),
                callee=item.callee,
                line=item.line,
                arguments=item.arguments,
                literals=item.literal_arguments,
                object_keys=item.object_keys,
            )
            for item in parsed_calls
        )
        assignments = discover_assignments(tokens, scopes)
        events = tuple(
            event
            for item in calls
            for event in (event_from_call(item),)
            if event is not None
        )
        strings = tuple(
            dict.fromkeys(
                token.value
                for token in tokens
                if token.kind in {"string", "template"}
                and not token.interpolated
            )
        )
        return ScriptFileFacts(
            path=relative,
            module=script_module_name(relative),
            language=language,
            digest=digest,
            production=is_production_script(relative),
            executable_lines=script_executable_lines(source),
            symbols=symbols,
            imports=imports,
            calls=calls,
            assignments=assignments,
            events=events,
            strings=strings,
            tokens=len(tokens),
            owner_claim_lines=tuple(
                index
                for index, line in enumerate(source.splitlines(), start=1)
                if OWNER_CLAIM_PATTERN.search(line)
            ),
        )

    def _build_graph(
        self,
        files: Sequence[ScriptFileFacts],
    ) -> tuple[tuple[GraphNode, ...], tuple[GraphEdge, ...]]:
        nodes: dict[str, GraphNode] = {}
        edges: dict[str, GraphEdge] = {}
        by_module = {item.module: item for item in files}
        by_path = {item.path: item for item in files}
        symbol_candidates: dict[str, list[tuple[ScriptFileFacts, ScriptSymbolFact]]] = (
            defaultdict(list)
        )
        for file_fact in files:
            file_id = f"file:{file_fact.path}"
            module_id = f"module:{file_fact.module}"
            nodes[file_id] = GraphNode(
                node_id=file_id,
                kind=NodeKind.FILE,
                language=file_fact.language,
                path=file_fact.path,
                executable=bool(file_fact.executable_lines and not file_fact.parse_error),
                attributes={
                    "production": file_fact.production,
                    "digest": file_fact.digest,
                },
            )
            nodes[module_id] = GraphNode(
                node_id=module_id,
                kind=NodeKind.MODULE,
                language=file_fact.language,
                path=file_fact.path,
                symbol=file_fact.module,
                executable=nodes[file_id].executable,
                attributes={"production": file_fact.production},
            )
            self._add_edge(
                edges,
                GraphEdge(
                    source=module_id,
                    target=file_id,
                    kind=EdgeKind.CONTAINS,
                    path=file_fact.path,
                ),
            )
            for symbol_fact in file_fact.symbols:
                symbol_id = (
                    f"symbol:{file_fact.path}#{symbol_fact.qualified_name}"
                )
                nodes[symbol_id] = GraphNode(
                    node_id=symbol_id,
                    kind=NodeKind.SYMBOL,
                    language=file_fact.language,
                    path=file_fact.path,
                    symbol=symbol_fact.qualified_name,
                    executable=symbol_fact.executable,
                    attributes={
                        "kind": symbol_fact.kind,
                        "line": symbol_fact.line,
                        "end_line": symbol_fact.end_line,
                        "exported": symbol_fact.exported,
                        "production": file_fact.production,
                    },
                )
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=file_id,
                        target=symbol_id,
                        kind=EdgeKind.CONTAINS,
                        path=file_fact.path,
                        line=symbol_fact.line,
                    ),
                )
                symbol_candidates[symbol_fact.name].append(
                    (file_fact, symbol_fact)
                )
                symbol_candidates[symbol_fact.qualified_name].append(
                    (file_fact, symbol_fact)
                )
            for event in file_fact.events:
                event_id = (
                    f"event:{file_fact.path}#{event.event_name}@{event.line}"
                )
                nodes[event_id] = GraphNode(
                    node_id=event_id,
                    kind=NodeKind.EVENT,
                    language=file_fact.language,
                    path=file_fact.path,
                    symbol=event.event_name,
                    attributes={
                        "scope": event.scope,
                        "callee": event.callee,
                        "line": event.line,
                        "production": file_fact.production,
                    },
                )
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=self._scope_node(file_fact, event.scope, nodes),
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
                    language=file_fact.language,
                    path=file_fact.path,
                    symbol=call.callee,
                    attributes={
                        "scope": call.scope,
                        "line": call.line,
                        "production": file_fact.production,
                    },
                )
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=self._scope_node(file_fact, call.scope, nodes),
                        target=mutation_id,
                        kind=EdgeKind.MUTATES,
                        path=file_fact.path,
                        line=call.line,
                    ),
                )
        for file_fact in files:
            module_id = f"module:{file_fact.module}"
            alias_map: dict[str, ScriptFileFacts] = {}
            for imported in file_fact.imports:
                target = resolve_script_import(
                    file_fact.path,
                    imported.specifier,
                    by_path,
                    by_module,
                )
                if target is None:
                    continue
                target_id = f"module:{target.module}"
                self._add_edge(
                    edges,
                    GraphEdge(
                        source=module_id,
                        target=target_id,
                        kind=EdgeKind.IMPORTS,
                        path=file_fact.path,
                        line=imported.line,
                        attributes={
                            "specifier": imported.specifier,
                            "dynamic": imported.dynamic,
                            "type_only": imported.type_only,
                        },
                    ),
                )
                for local_name in imported.local_names:
                    alias_map[local_name] = target
            local_symbols = {
                item.name: f"symbol:{file_fact.path}#{item.qualified_name}"
                for item in file_fact.symbols
            }
            for call in file_fact.calls:
                caller = self._scope_node(file_fact, call.scope, nodes)
                name = call.callee.rsplit(".", 1)[-1]
                target_id = local_symbols.get(name)
                root = call.callee.split(".", 1)[0]
                if target_id is None and root in alias_map:
                    target_file = alias_map[root]
                    target_id = f"module:{target_file.module}"
                if target_id is None:
                    candidates = symbol_candidates.get(name, ())
                    if len(candidates) == 1:
                        target_file, target_symbol = candidates[0]
                        target_id = (
                            f"symbol:{target_file.path}#"
                            f"{target_symbol.qualified_name}"
                        )
                if target_id:
                    self._add_edge(
                        edges,
                        GraphEdge(
                            source=caller,
                            target=target_id,
                            kind=EdgeKind.CALLS,
                            path=file_fact.path,
                            line=call.line,
                            attributes={"call": call.callee},
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
    def _scope_node(
        file_fact: ScriptFileFacts,
        scope: str,
        nodes: Mapping[str, GraphNode],
    ) -> str:
        if scope:
            parts = scope.split(".")
            while parts:
                candidate = (
                    f"symbol:{file_fact.path}#{'.'.join(parts)}"
                )
                if candidate in nodes:
                    return candidate
                parts.pop()
        return f"module:{file_fact.module}"

    @staticmethod
    def _add_edge(
        selected: dict[str, GraphEdge],
        edge: GraphEdge,
    ) -> None:
        selected.setdefault(edge.fingerprint, edge)


def discover_symbols(tokens: Sequence[Token]) -> tuple[ScriptSymbolFact, ...]:
    symbols: list[ScriptSymbolFact] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.kind != "identifier":
            index += 1
            continue
        cursor = index
        modifiers: list[str] = []
        while cursor < len(tokens) and tokens[cursor].value in MODIFIERS:
            modifiers.append(tokens[cursor].value)
            cursor += 1
        if cursor >= len(tokens):
            break
        current = tokens[cursor]
        if current.value in DECLARATION_KEYWORDS:
            name_token = next_identifier(tokens, cursor + 1)
            if name_token is not None:
                end_line = declaration_end_line(tokens, cursor, name_token.line)
                executable = current.value not in {"interface", "type"}
                symbols.append(
                    ScriptSymbolFact(
                        name=name_token.value,
                        qualified_name=name_token.value,
                        kind=current.value,
                        line=token.line,
                        end_line=end_line,
                        exported="export" in modifiers,
                        default_export="default" in modifiers,
                        executable=executable,
                    )
                )
                index = cursor + 2
                continue
        if current.value in VARIABLE_KEYWORDS:
            name_token = next_identifier(tokens, cursor + 1)
            if name_token is not None:
                assign_index = token_index_at(tokens, name_token)
                arrow = find_value(tokens, "=>", assign_index + 1, limit=80)
                class_expr = find_value(tokens, "class", assign_index + 1, limit=12)
                function_expr = find_value(
                    tokens, "function", assign_index + 1, limit=12
                )
                if arrow >= 0 or class_expr >= 0 or function_expr >= 0:
                    start = (
                        min(
                            item
                            for item in (arrow, class_expr, function_expr)
                            if item >= 0
                        )
                    )
                    end_line = declaration_end_line(
                        tokens,
                        start,
                        name_token.line,
                    )
                    symbols.append(
                        ScriptSymbolFact(
                            name=name_token.value,
                            qualified_name=name_token.value,
                            kind=(
                                "arrow"
                                if arrow >= 0 and arrow == start
                                else "class_expression"
                                if class_expr >= 0 and class_expr == start
                                else "function_expression"
                            ),
                            line=token.line,
                            end_line=end_line,
                            exported="export" in modifiers,
                            default_export="default" in modifiers,
                            executable=True,
                        )
                    )
        index += 1
    class_symbols = [item for item in symbols if item.kind in {"class", "class_expression"}]
    for class_symbol in class_symbols:
        class_start = next(
            (
                index
                for index, token in enumerate(tokens)
                if token.line >= class_symbol.line
                and token.value == class_symbol.name
            ),
            -1,
        )
        if class_start < 0:
            continue
        body_start = find_value(tokens, "{", class_start, limit=40)
        if body_start < 0:
            continue
        body_end = matching_delimiter(tokens, body_start, "{", "}")
        if body_end < 0:
            continue
        cursor = body_start + 1
        depth = 1
        while cursor < body_end:
            value = tokens[cursor].value
            if value == "{":
                depth += 1
            elif value == "}":
                depth -= 1
            if (
                depth == 1
                and tokens[cursor].kind == "identifier"
                and value not in MODIFIERS
                and value not in {"constructor", "get", "set"}
                and cursor + 1 < body_end
            ):
                open_index = cursor + 1
                while (
                    open_index < body_end
                    and tokens[open_index].value in {"?", "<"}
                ):
                    open_index += 1
                if tokens[open_index].value == "(":
                    close_paren = matching_delimiter(tokens, open_index, "(", ")")
                    open_body = find_value(
                        tokens,
                        "{",
                        close_paren + 1,
                        limit=20,
                    )
                    if open_body >= 0 and open_body < body_end:
                        close_body = matching_delimiter(tokens, open_body, "{", "}")
                        if close_body > open_body:
                            symbols.append(
                                ScriptSymbolFact(
                                    name=value,
                                    qualified_name=f"{class_symbol.name}.{value}",
                                    kind="method",
                                    line=tokens[cursor].line,
                                    end_line=tokens[close_body].line,
                                    parent=class_symbol.name,
                                    executable=True,
                                )
                            )
                            cursor = close_body
            cursor += 1
    unique: dict[tuple[str, int, str], ScriptSymbolFact] = {}
    for item in symbols:
        unique.setdefault((item.qualified_name, item.line, item.kind), item)
    return tuple(
        sorted(
            unique.values(),
            key=lambda item: (item.line, item.qualified_name, item.kind),
        )
    )


def discover_assignments(
    tokens: Sequence[Token],
    scopes: Sequence[tuple[int, int, str]],
) -> tuple[ScriptAssignmentFact, ...]:
    results: list[ScriptAssignmentFact] = []
    assignment_operators = {"=", "+=", "-=", "*=", "/=", "??=", "||=", "&&="}
    for index, token in enumerate(tokens):
        if token.value not in assignment_operators or index == 0:
            continue
        target_tokens: list[Token] = []
        cursor = index - 1
        while cursor >= 0 and len(target_tokens) < 16:
            current = tokens[cursor]
            if current.value in {";", "{", "}", "(", ")", ",", "=>"}:
                break
            if current.value in VARIABLE_KEYWORDS:
                break
            target_tokens.append(current)
            cursor -= 1
        target = render_target(tuple(reversed(target_tokens)))
        if not target:
            continue
        folded = target.casefold()
        persistent = (
            folded.startswith(("this.", "state.", "store.", "session."))
            or any(
                value in folded
                for value in (
                    "revision",
                    "version",
                    "epoch",
                    "sequence",
                    "checkpoint",
                    "lease",
                    "route",
                    "permit",
                    "status",
                    "owner",
                )
            )
        )
        results.append(
            ScriptAssignmentFact(
                scope=scope_at(scopes, token.line),
                target=target,
                line=token.line,
                persistent=persistent,
                operator=token.value,
            )
        )
    return tuple(results)


def event_from_call(call: ScriptCallFact) -> ScriptEventFact | None:
    verb = call.verb
    if verb not in EVENT_SUFFIXES and not any(
        verb.casefold().endswith(item.casefold()) for item in EVENT_SUFFIXES
    ):
        return None
    event_name = next((item for item in call.literals if item), "<dynamic>")
    return ScriptEventFact(
        scope=call.scope,
        event_name=event_name,
        callee=call.callee,
        line=call.line,
        attributes=tuple(sorted(call.object_keys)),
    )


def import_local_names(
    tokens: Sequence[Token],
    line: int,
    specifier: str,
) -> tuple[str, ...]:
    start = next(
        (
            index
            for index, token in enumerate(tokens)
            if token.line == line and token.value in {"import", "require"}
        ),
        -1,
    )
    if start < 0:
        return ()
    names: list[str] = []
    cursor = start + 1
    while cursor < len(tokens) and cursor < start + 100:
        token = tokens[cursor]
        if token.value in {"from", ";"} or (
            token.kind == "string" and token.value == specifier
        ):
            break
        if token.kind == "identifier" and token.value not in {
            "type",
            "as",
            "import",
            "require",
        }:
            if cursor > start + 1 and tokens[cursor - 1].value == "as":
                if names:
                    names[-1] = token.value
                else:
                    names.append(token.value)
            else:
                names.append(token.value)
        cursor += 1
    if tokens[start].value == "require" and start >= 2:
        previous = tokens[start - 1]
        if previous.value == "=" and tokens[start - 2].kind == "identifier":
            names.append(tokens[start - 2].value)
    return tuple(dict.fromkeys(names))


def import_type_only(tokens: Sequence[Token], line: int) -> bool:
    candidates = [token.value for token in tokens if token.line == line][:12]
    try:
        start = candidates.index("import")
    except ValueError:
        return False
    return start + 1 < len(candidates) and candidates[start + 1] == "type"


def scope_by_line(
    symbols: Sequence[ScriptSymbolFact],
) -> tuple[tuple[int, int, str], ...]:
    return tuple(
        sorted(
            (
                item.line,
                max(item.line, item.end_line),
                item.qualified_name,
            )
            for item in symbols
            if item.executable
        )
    )


def scope_at(scopes: Sequence[tuple[int, int, str]], line: int) -> str:
    candidates = [
        (end - start, name)
        for start, end, name in scopes
        if start <= line <= end
    ]
    return min(candidates, default=(0, ""))[1]


def resolve_script_import(
    current_path: str,
    specifier: str,
    by_path: Mapping[str, ScriptFileFacts],
    by_module: Mapping[str, ScriptFileFacts],
) -> ScriptFileFacts | None:
    if not specifier:
        return None
    if specifier.startswith("."):
        base = PurePosixPath(current_path).parent
        raw = (base / specifier).as_posix()
        parts: list[str] = []
        for part in PurePosixPath(raw).parts:
            if part == ".":
                continue
            if part == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(part)
        normalized = "/".join(parts)
        candidates = [normalized]
        candidates.extend(f"{normalized}{suffix}" for suffix in SCRIPT_SUFFIXES)
        candidates.extend(
            f"{normalized}/index{suffix}" for suffix in SCRIPT_SUFFIXES
        )
        for candidate in candidates:
            if candidate in by_path:
                return by_path[candidate]
        return None
    cleaned = specifier
    for prefix in ("@zyra/", "zyra/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    suffix = cleaned.replace("-", "_").replace("/", ".")
    exact = by_module.get(suffix)
    if exact is not None:
        return exact
    matches = [
        item
        for module, item in by_module.items()
        if module.endswith(f".{suffix}") or module.endswith(suffix)
    ]
    return matches[0] if len(matches) == 1 else None


def script_module_name(path: str) -> str:
    selected = PurePosixPath(path.replace("\\", "/"))
    parts = list(selected.with_suffix("").parts)
    if parts and parts[-1] == "index":
        parts.pop()
    for marker in (
        ("apps", "web", "src"),
        ("packages", "runtime"),
        ("packages", "memory"),
        ("packages", "integrations"),
        ("packages", "commands"),
    ):
        if tuple(parts[: len(marker)]) == marker:
            parts = parts[len(marker) :]
            break
    return ".".join(parts)


def language_for_path(path: str) -> Language:
    return {
        ".ts": Language.TYPESCRIPT,
        ".tsx": Language.TSX,
        ".js": Language.JAVASCRIPT,
        ".jsx": Language.JSX,
        ".mjs": Language.JAVASCRIPT,
        ".cjs": Language.JAVASCRIPT,
    }[Path(path).suffix.casefold()]


def is_production_script(path: str) -> bool:
    parts = PurePosixPath(path.replace("\\", "/").casefold()).parts
    if any(part in NON_PRODUCTION_PARTS for part in parts):
        return False
    return bool(parts and parts[0] in {"apps", "packages", "scripts", "skills"})


def script_executable_lines(source: str) -> int:
    count = 0
    block_comment = False
    declaration_depth = 0
    for raw in source.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if block_comment:
            if "*/" in stripped:
                block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                block_comment = True
            continue
        if stripped.startswith(("//", "*")):
            continue
        if declaration_depth:
            declaration_depth += stripped.count("{") - stripped.count("}")
            if declaration_depth <= 0:
                declaration_depth = 0
            continue
        if re.match(r"^(?:export\s+)?(?:declare\s+)?(?:interface|type)\b", stripped):
            declaration_depth = stripped.count("{") - stripped.count("}")
            if declaration_depth <= 0 and not stripped.endswith(";"):
                declaration_depth = 1
            continue
        if re.match(r"^(?:import|export\s+\{[^}]*\}\s+from)\b", stripped):
            continue
        if re.fullmatch(r"[{}();,]+", stripped):
            continue
        count += 1
    return count


def next_identifier(
    tokens: Sequence[Token],
    start: int,
) -> Token | None:
    for token in tokens[start : start + 20]:
        if token.kind == "identifier" and token.value not in MODIFIERS:
            return token
        if token.value in {";", "{", "}"}:
            break
    return None


def token_index_at(tokens: Sequence[Token], target: Token) -> int:
    for index, token in enumerate(tokens):
        if token is target:
            return index
    return -1


def find_value(
    tokens: Sequence[Token],
    value: str,
    start: int,
    *,
    limit: int,
) -> int:
    stop = min(len(tokens), start + limit)
    for index in range(max(0, start), stop):
        if tokens[index].value == value:
            return index
        if tokens[index].value == ";":
            return -1
    return -1


def declaration_end_line(
    tokens: Sequence[Token],
    start: int,
    default_line: int,
) -> int:
    body_start = find_value(tokens, "{", start, limit=160)
    if body_start >= 0:
        body_end = matching_delimiter(tokens, body_start, "{", "}")
        if body_end >= 0:
            return tokens[body_end].line
    for token in tokens[start : start + 160]:
        if token.value == ";":
            return token.line
    return default_line


def matching_delimiter(
    tokens: Sequence[Token],
    start: int,
    opening: str,
    closing: str,
) -> int:
    if start < 0 or start >= len(tokens) or tokens[start].value != opening:
        return -1
    depth = 0
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if value == opening:
            depth += 1
        elif value == closing:
            depth -= 1
            if depth == 0:
                return index
    return -1


def render_target(tokens: Sequence[Token]) -> str:
    if not tokens:
        return ""
    values: list[str] = []
    for token in tokens:
        if token.kind == "identifier" or token.value in {".", "?.", "[", "]"}:
            values.append(token.value)
    return "".join(values).strip(".")
