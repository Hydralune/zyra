from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditSection,
    Disposition,
    Evidence,
    EvidenceKind,
    Finding,
    RuleSwitches,
    Severity,
    content_digest,
    finding,
    section,
)
from .repository import RepositoryFile, RepositoryInventory


JS_SUFFIXES = frozenset({".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"})
FORBIDDEN_LANGGRAPH_PACKAGES = (
    "@langchain/langgraph",
    "@langchain/langgraph-sdk",
    "langgraph",
    "langgraph-sdk",
)
FORBIDDEN_LANGGRAPH_SEGMENTS = (
    "/pregel",
    "/graph",
    "/channels",
    "/prebuilt",
    "/store",
    "/stream",
    "/sdk",
    "/server",
)
PROCESS_CALLEES = frozenset(
    {
        "spawn",
        "spawnSync",
        "exec",
        "execSync",
        "execFile",
        "execFileSync",
        "fork",
        "Bun.spawn",
        "Bun.spawnSync",
        "Deno.Command",
        "execa",
        "execaCommand",
        "$",
    }
)
SHELL_CALLEES = frozenset({"exec", "execSync", "execaCommand", "$"})
PROCESS_IMPORTS = frozenset(
    {
        "child_process",
        "node:child_process",
        "execa",
    }
)
PROCESS_OBJECTS = frozenset(
    {
        "bun",
        "deno",
        "child_process",
        "childprocess",
        "cp",
    }
)
NETWORK_CALLEES = frozenset(
    {
        "fetch",
        "axios.get",
        "axios.post",
        "got",
        "got.get",
        "request",
        "https.get",
        "http.get",
        "Bun.write",
    }
)
PORT_CALLEES = frozenset(
    {
        "serve",
        "Bun.serve",
        "Deno.serve",
        "createServer",
        "server.listen",
        "httpServer.listen",
        "httpsServer.listen",
    }
)
INSTALLERS = frozenset(
    {
        "npm",
        "npx",
        "bun",
        "bunx",
        "pnpm",
        "pnpx",
        "yarn",
        "pip",
        "pip3",
        "uv",
        "cargo",
    }
)
PARENT_SOURCE_PATTERN = re.compile(
    r"(?i)(?:^|[/\\])\.\.[/\\](claude-code-best|browser-use|OpenHands|opencode|"
    r"agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)(?:[/\\]|$)"
)
ABSOLUTE_SOURCE_PATTERN = re.compile(
    r"(?i)[A-Z]:[/\\][^\r\n'\"`]*(claude-code-best|browser-use|OpenHands|"
    r"opencode|agentscope|agent-framework|hermes-agent|langgraph|oh-my-pi|openclaw)"
)
URL_PATTERN = re.compile(r"^https?://", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Token:
    kind: str
    value: str
    line: int
    column: int
    interpolated: bool = False


@dataclass(frozen=True, slots=True)
class JavaScriptImport:
    specifier: str
    line: int
    style: str
    dynamic: bool
    literal: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "specifier": self.specifier,
            "line": self.line,
            "style": self.style,
            "dynamic": self.dynamic,
            "literal": self.literal,
        }


@dataclass(frozen=True, slots=True)
class JavaScriptCall:
    callee: str
    line: int
    arguments: tuple[str, ...]
    literal_arguments: tuple[str, ...]
    object_keys: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "callee": self.callee,
            "line": self.line,
            "arguments": list(self.arguments),
            "literal_arguments": list(self.literal_arguments),
            "object_keys": list(self.object_keys),
        }


@dataclass(frozen=True, slots=True)
class JavaScriptFileAnalysis:
    path: str
    imports: tuple[JavaScriptImport, ...]
    calls: tuple[JavaScriptCall, ...]
    strings: tuple[tuple[int, str, bool], ...]
    tokens: int
    parse_error: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "imports": [item.to_dict() for item in self.imports],
            "calls": [item.to_dict() for item in self.calls],
            "strings": len(self.strings),
            "tokens": self.tokens,
            "parse_error": self.parse_error,
            "digest": self.digest,
        }


class JavaScriptLexer:
    def __init__(self, source: str) -> None:
        self.source = source
        self.length = len(source)
        self.index = 0
        self.line = 1
        self.column = 1

    def tokens(self) -> Iterator[Token]:
        previous: Token | None = None
        before_previous: Token | None = None
        while self.index < self.length:
            character = self.source[self.index]
            if character.isspace():
                self._consume_space()
                continue
            if self._starts("//"):
                self._consume_line_comment()
                continue
            if self._starts("/*"):
                self._consume_block_comment()
                continue
            if character in {"'", '"'}:
                token = self._consume_string(character)
            elif character == "`":
                token = self._consume_template()
            elif character == "/" and self._regex_allowed(
                previous, before_previous
            ):
                token = self._consume_regex()
            elif _identifier_start(character):
                token = self._consume_identifier()
            elif character.isdigit():
                token = self._consume_number()
            else:
                line, column = self.line, self.column
                operator = self._consume_operator()
                token = Token("punctuation", operator, line, column)
            before_previous, previous = previous, token
            yield token

    def _starts(self, value: str) -> bool:
        return self.source.startswith(value, self.index)

    def _advance(self, count: int = 1) -> str:
        consumed = self.source[self.index : self.index + count]
        self.index += count
        for character in consumed:
            if character == "\n":
                self.line += 1
                self.column = 1
            else:
                self.column += 1
        return consumed

    def _consume_space(self) -> None:
        while self.index < self.length and self.source[self.index].isspace():
            self._advance()

    def _consume_line_comment(self) -> None:
        self._advance(2)
        while self.index < self.length and self.source[self.index] not in "\r\n":
            self._advance()

    def _consume_block_comment(self) -> None:
        self._advance(2)
        while self.index < self.length:
            if self._starts("*/"):
                self._advance(2)
                return
            self._advance()

    def _consume_string(self, quote: str) -> Token:
        line, column = self.line, self.column
        self._advance()
        buffer: list[str] = []
        while self.index < self.length:
            character = self.source[self.index]
            if character == "\\":
                self._advance()
                if self.index < self.length:
                    buffer.append(self._decode_escape(self._advance()))
                continue
            if character == quote:
                self._advance()
                return Token("string", "".join(buffer), line, column)
            if character in "\r\n":
                return Token("error", "unterminated string", line, column)
            buffer.append(self._advance())
        return Token("error", "unterminated string", line, column)

    def _consume_template(self) -> Token:
        line, column = self.line, self.column
        self._advance()
        buffer: list[str] = []
        interpolated = False
        brace_depth = 0
        while self.index < self.length:
            character = self.source[self.index]
            if character == "\\":
                self._advance()
                if self.index < self.length:
                    buffer.append(self._decode_escape(self._advance()))
                continue
            if self._starts("${"):
                interpolated = True
                brace_depth += 1
                buffer.append("${")
                self._advance(2)
                continue
            if (
                brace_depth
                and character == "/"
                and self._regex_character_context()
            ):
                nested = self._consume_regex()
                if nested.kind == "error":
                    return nested
                continue
            if brace_depth and character in {"'", '"'}:
                nested = self._consume_string(character)
                buffer.append(nested.value)
                continue
            if brace_depth and character == "`":
                nested = self._consume_template()
                buffer.append(nested.value)
                interpolated = interpolated or nested.interpolated
                continue
            if brace_depth and self._starts("//"):
                self._consume_line_comment()
                continue
            if brace_depth and self._starts("/*"):
                self._consume_block_comment()
                continue
            if character == "{" and brace_depth:
                brace_depth += 1
                buffer.append(self._advance())
                continue
            if character == "}" and brace_depth:
                brace_depth -= 1
                buffer.append(self._advance())
                continue
            if character == "`" and brace_depth == 0:
                self._advance()
                return Token(
                    "template",
                    "".join(buffer),
                    line,
                    column,
                    interpolated=interpolated,
                )
            buffer.append(self._advance())
        return Token("error", "unterminated template", line, column)

    def _regex_allowed(
        self,
        previous: Token | None,
        before_previous: Token | None,
    ) -> bool:
        if previous is None:
            return True
        if previous.kind == "punctuation":
            if previous.value == "!":
                return (
                    before_previous is None
                    or (
                        before_previous.kind == "punctuation"
                        and before_previous.value
                        in {"(", "[", "{", ",", ";", ":", "=", "=>"}
                    )
                )
            return previous.value in {
                "(",
                "[",
                "{",
                ",",
                ";",
                ":",
                "=",
                "==",
                "===",
                "!=",
                "!==",
                "&&",
                "||",
                "??",
                "?",
                "=>",
            }
        return previous.kind == "identifier" and previous.value in {
            "return",
            "case",
            "throw",
            "yield",
            "await",
            "typeof",
            "instanceof",
            "in",
            "of",
        }

    def _regex_character_context(self) -> bool:
        cursor = self.index - 1
        while cursor >= 0 and self.source[cursor].isspace():
            cursor -= 1
        if cursor < 0:
            return True
        previous = self.source[cursor]
        if previous != "!":
            return previous in "([{,;:=?"
        cursor -= 1
        while cursor >= 0 and self.source[cursor].isspace():
            cursor -= 1
        return cursor < 0 or self.source[cursor] in "([{,;:="

    def _consume_regex(self) -> Token:
        line, column = self.line, self.column
        self._advance()
        in_character_class = False
        while self.index < self.length:
            character = self.source[self.index]
            if character == "\\":
                self._advance()
                if self.index < self.length:
                    self._advance()
                continue
            if character in "\r\n":
                return Token("error", "unterminated regular expression", line, column)
            if character == "[":
                in_character_class = True
                self._advance()
                continue
            if character == "]" and in_character_class:
                in_character_class = False
                self._advance()
                continue
            if character == "/" and not in_character_class:
                self._advance()
                while (
                    self.index < self.length
                    and self.source[self.index].isalpha()
                ):
                    self._advance()
                return Token("regex", "", line, column)
            self._advance()
        return Token("error", "unterminated regular expression", line, column)

    def _decode_escape(self, escaped: str) -> str:
        return {
            "n": "\n",
            "r": "\r",
            "t": "\t",
            "b": "\b",
            "f": "\f",
            "v": "\v",
            "0": "\0",
        }.get(escaped, escaped)

    def _consume_identifier(self) -> Token:
        line, column = self.line, self.column
        buffer: list[str] = []
        while self.index < self.length and _identifier_part(self.source[self.index]):
            buffer.append(self._advance())
        return Token("identifier", "".join(buffer), line, column)

    def _consume_number(self) -> Token:
        line, column = self.line, self.column
        buffer: list[str] = []
        while self.index < self.length:
            character = self.source[self.index]
            if character.isalnum() or character in "._":
                buffer.append(self._advance())
            else:
                break
        return Token("number", "".join(buffer), line, column)

    def _consume_operator(self) -> str:
        for size in (4, 3, 2):
            candidate = self.source[self.index : self.index + size]
            if candidate in {
                ">>>=",
                "===",
                "!==",
                ">>>",
                "**=",
                "&&=",
                "||=",
                "??=",
                "=>",
                "==",
                "!=",
                "<=",
                ">=",
                "&&",
                "||",
                "??",
                "?.",
                "++",
                "--",
                "**",
                "<<",
                ">>",
                "+=",
                "-=",
                "*=",
                "/=",
                "%=",
                "&=",
                "|=",
                "^=",
                "...",
            }:
                return self._advance(size)
        return self._advance()


class JavaScriptParser:
    def __init__(self, tokens: Sequence[Token]) -> None:
        self.tokens = tokens

    def imports(self) -> tuple[JavaScriptImport, ...]:
        result: list[JavaScriptImport] = []
        index = 0
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.kind != "identifier":
                index += 1
                continue
            if token.value == "import":
                imported, consumed = self._parse_import(index)
                if imported is not None:
                    result.append(imported)
                index = max(index + 1, consumed)
                continue
            if token.value == "require" and self._value(index + 1) == "(":
                literal = self._token(index + 2)
                result.append(
                    JavaScriptImport(
                        specifier=literal.value if literal and literal.kind == "string" else "",
                        line=token.line,
                        style="require",
                        dynamic=False,
                        literal=bool(literal and literal.kind == "string"),
                    )
                )
            index += 1
        return tuple(result)

    def _parse_import(
        self, index: int
    ) -> tuple[JavaScriptImport | None, int]:
        start = self.tokens[index]
        next_token = self._token(index + 1)
        if next_token is None:
            return None, index + 1
        if next_token.value == "(":
            literal = self._token(index + 2)
            return (
                JavaScriptImport(
                    specifier=literal.value if literal and literal.kind == "string" else "",
                    line=start.line,
                    style="dynamic_import",
                    dynamic=True,
                    literal=bool(
                        literal
                        and literal.kind in {"string", "template"}
                        and not literal.interpolated
                    ),
                ),
                index + 3,
            )
        if next_token.kind == "string":
            return (
                JavaScriptImport(
                    specifier=next_token.value,
                    line=start.line,
                    style="side_effect",
                    dynamic=False,
                    literal=True,
                ),
                index + 2,
            )
        cursor = index + 1
        while cursor < len(self.tokens) and cursor < index + 80:
            current = self.tokens[cursor]
            if current.kind == "identifier" and current.value == "from":
                literal = self._token(cursor + 1)
                return (
                    JavaScriptImport(
                        specifier=(
                            literal.value if literal and literal.kind == "string" else ""
                        ),
                        line=start.line,
                        style="static_import",
                        dynamic=False,
                        literal=bool(literal and literal.kind == "string"),
                    ),
                    cursor + 2,
                )
            if current.value == ";":
                break
            cursor += 1
        return None, cursor

    def calls(self) -> tuple[JavaScriptCall, ...]:
        result: list[JavaScriptCall] = []
        for index, token in enumerate(self.tokens):
            if token.kind != "identifier" and token.value != "$":
                continue
            callee, open_index = self._callee_at(index)
            if not callee or self._value(open_index) != "(":
                continue
            arguments, literals, object_keys, _ = self._parse_arguments(open_index)
            result.append(
                JavaScriptCall(
                    callee=callee,
                    line=token.line,
                    arguments=arguments,
                    literal_arguments=literals,
                    object_keys=object_keys,
                )
            )
        return tuple(result)

    def _callee_at(self, index: int) -> tuple[str, int]:
        parts = [self.tokens[index].value]
        cursor = index + 1
        while (
            self._value(cursor) in {".", "?."}
            and (next_token := self._token(cursor + 1)) is not None
            and next_token.kind == "identifier"
        ):
            parts.append(next_token.value)
            cursor += 2
        if self._value(cursor) == "?." and self._value(cursor + 1) == "(":
            cursor += 1
        return ".".join(parts), cursor

    def _parse_arguments(
        self, open_index: int
    ) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], int]:
        depth = 0
        cursor = open_index
        current: list[Token] = []
        arguments: list[str] = []
        literals: list[str] = []
        object_keys: set[str] = set()
        while cursor < len(self.tokens):
            token = self.tokens[cursor]
            if token.value in {"(", "[", "{"}:
                depth += 1
                if depth > 1:
                    current.append(token)
            elif token.value in {")", "]", "}"}:
                depth -= 1
                if depth == 0:
                    if current:
                        arguments.append(_render_tokens(current))
                        literals.extend(_literal_values(current))
                    return (
                        tuple(arguments),
                        tuple(literals),
                        tuple(sorted(object_keys)),
                        cursor + 1,
                    )
                current.append(token)
            elif token.value == "," and depth == 1:
                arguments.append(_render_tokens(current))
                literals.extend(_literal_values(current))
                current = []
            else:
                current.append(token)
                if (
                    token.kind in {"identifier", "string"}
                    and self._value(cursor + 1) == ":"
                    and depth >= 2
                ):
                    object_keys.add(token.value)
            cursor += 1
        if current:
            arguments.append(_render_tokens(current))
            literals.extend(_literal_values(current))
        return tuple(arguments), tuple(literals), tuple(sorted(object_keys)), cursor

    def _token(self, index: int) -> Token | None:
        return self.tokens[index] if 0 <= index < len(self.tokens) else None

    def _value(self, index: int) -> str:
        token = self._token(index)
        return token.value if token else ""


class JavaScriptAnalyzer:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()

    def analyze(
        self, inventory: RepositoryInventory
    ) -> tuple[tuple[JavaScriptFileAnalysis, ...], AuditSection]:
        analyses: list[JavaScriptFileAnalysis] = []
        findings: list[Finding] = []
        evidence: list[Evidence] = []
        candidates = [
            item
            for item in inventory.files
            if item.kind == "source"
            and item.suffix in JS_SUFFIXES
            and not item.path.startswith(("vendor/", "vendor-runtimes/"))
        ]
        for record in candidates:
            analysis = self._analyze_file(record)
            analyses.append(analysis)
            if analysis.parse_error:
                findings.append(
                    finding(
                        "javascript_source_lex_failed",
                        f"JavaScript/TypeScript source cannot be tokenized: {analysis.parse_error}",
                        "dependencies",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        disposition=Disposition.DECLARE,
                        remediation="Repair the lexical error or exclude non-runtime output.",
                        default_path_impact="Dependency and process coverage is incomplete.",
                    )
                )
                continue
            if self.switches.dependencies:
                findings.extend(self._dependency_findings(analysis))
            if self.switches.processes:
                findings.extend(self._process_findings(analysis))
            if self.switches.langgraph:
                findings.extend(self._langgraph_findings(analysis))
            if self.switches.source_specific:
                findings.extend(self._source_specific_findings(analysis))
            evidence.append(
                Evidence(
                    kind=EvidenceKind.SOURCE,
                    path=analysis.path,
                    excerpt_digest=analysis.digest,
                    attributes={
                        "imports": len(analysis.imports),
                        "calls": len(analysis.calls),
                    },
                )
            )
        import_roots = Counter(
            _package_root(item.specifier)
            for analysis in analyses
            for item in analysis.imports
            if item.specifier and not item.specifier.startswith((".", "/"))
        )
        call_counts = Counter(
            call.callee for analysis in analyses for call in analysis.calls
        )
        return tuple(analyses), section(
            "javascript_source",
            metrics={
                "files": len(analyses),
                "parsed": sum(not item.parse_error for item in analyses),
                "lex_errors": sum(bool(item.parse_error) for item in analyses),
                "tokens": sum(item.tokens for item in analyses),
                "imports": sum(len(item.imports) for item in analyses),
                "calls": sum(len(item.calls) for item in analyses),
                "top_import_roots": dict(import_roots.most_common(30)),
                "process_call_count": sum(
                    is_process_call(analysis, call)
                    for analysis in analyses
                    for call in analysis.calls
                ),
            },
            findings=findings,
            evidence=evidence,
        )

    def _analyze_file(self, record: RepositoryFile) -> JavaScriptFileAnalysis:
        try:
            source = (self.project_root / record.path).read_text(encoding="utf-8")
            tokens = tuple(JavaScriptLexer(source).tokens())
        except (OSError, UnicodeDecodeError) as exc:
            return JavaScriptFileAnalysis(
                path=record.path,
                imports=(),
                calls=(),
                strings=(),
                tokens=0,
                parse_error=str(exc),
                digest=record.digest,
            )
        errors = [token for token in tokens if token.kind == "error"]
        parser = JavaScriptParser(tokens)
        return JavaScriptFileAnalysis(
            path=record.path,
            imports=parser.imports(),
            calls=parser.calls(),
            strings=tuple(
                (token.line, token.value, token.interpolated)
                for token in tokens
                if token.kind in {"string", "template"}
            ),
            tokens=len(tokens),
            parse_error=(
                f"{errors[0].value} at {errors[0].line}:{errors[0].column}"
                if errors
                else ""
            ),
            digest=record.digest,
        )

    def _dependency_findings(
        self, analysis: JavaScriptFileAnalysis
    ) -> list[Finding]:
        findings: list[Finding] = []
        runtime_scope = _runtime_dependency_scope(analysis.path)
        for imported in analysis.imports:
            if imported.dynamic and not imported.literal:
                findings.append(
                    finding(
                        "javascript_dynamic_import_nonliteral",
                        "Dynamic JavaScript import uses a non-literal specifier.",
                        "dependencies",
                        severity=(
                            Severity.BLOCKER if runtime_scope else Severity.WARNING
                        ),
                        path=analysis.path,
                        line=imported.line,
                        disposition=(
                            Disposition.BLOCK_RELEASE
                            if runtime_scope
                            else Disposition.TRACK
                        ),
                        remediation=(
                            "Use a declared allowlisted module registry."
                            if runtime_scope
                            else "Keep the test/audit loader outside product runtime custody."
                        ),
                        default_path_impact=(
                            "Runtime dependency cannot be frozen."
                            if runtime_scope
                            else "Non-runtime loader cannot own task execution."
                        ),
                        attributes={"runtime_scope": runtime_scope},
                    )
                )
            normalized = imported.specifier.replace("\\", "/")
            parent = PARENT_SOURCE_PATTERN.search(normalized)
            if parent:
                findings.append(
                    finding(
                        "javascript_parent_source_import",
                        "JavaScript/TypeScript imports a sibling source repository.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        source_repo=parent.group(1),
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.BLOCK_RELEASE,
                        remediation="Migrate owned code or use a declared package dependency.",
                        default_path_impact="Clean submission depends on workspace layout.",
                        attributes={"specifier": imported.specifier},
                    )
                )
            if _package_root(normalized).casefold() == "openclaw":
                findings.append(
                    finding(
                        "openclaw_javascript_import",
                        "Runtime imports the forward-excluded OpenClaw package.",
                        "dependencies",
                        severity=Severity.BLOCKER,
                        source_repo="openclaw",
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.REMOVE,
                        remediation="Remove the import and preserve the selected Zyra owner.",
                        default_path_impact="Excluded source is runtime reachable.",
                    )
                )
        for line, value, interpolated in analysis.strings:
            parent = PARENT_SOURCE_PATTERN.search(value.replace("\\", "/"))
            absolute = ABSOLUTE_SOURCE_PATTERN.search(value)
            if parent or absolute:
                findings.append(
                    finding(
                        "javascript_source_path_literal",
                        "JavaScript/TypeScript embeds a source-repository filesystem path.",
                        "dependencies",
                        severity=(
                            Severity.BLOCKER if runtime_scope else Severity.WARNING
                        ),
                        source_repo=(parent.group(1) if parent else absolute.group(1)),
                        path=analysis.path,
                        line=line,
                        disposition=(
                            Disposition.BLOCK_RELEASE
                            if runtime_scope
                            else Disposition.TRACK
                        ),
                        remediation=(
                            "Use project-local paths or declared dependencies."
                            if runtime_scope
                            else "Keep negative fixtures and provenance paths outside product runtime."
                        ),
                        default_path_impact=(
                            "Clean-machine package is not self-contained."
                            if runtime_scope
                            else "Test/audit provenance is non-runtime."
                        ),
                        attributes={
                            "interpolated": interpolated,
                            "literal_digest": content_digest(value.encode()),
                            "runtime_scope": runtime_scope,
                        },
                    )
                )
        return findings

    def _process_findings(
        self, analysis: JavaScriptFileAnalysis
    ) -> list[Finding]:
        findings: list[Finding] = []
        runtime_scope = _runtime_dependency_scope(analysis.path)
        for call in analysis.calls:
            tail = call.callee.rsplit(".", 1)[-1]
            if not is_process_call(analysis, call):
                continue
            command = call.literal_arguments[0] if call.literal_arguments else ""
            tokens = tuple(re.split(r"\s+", command.strip())) if command else ()
            executable = Path(tokens[0]).name.casefold() if tokens else ""
            shell = (
                call.callee in SHELL_CALLEES
                or tail in SHELL_CALLEES
                or "shell" in call.object_keys
            )
            if shell:
                findings.append(
                    finding(
                        "javascript_shell_process_call",
                        "JavaScript/TypeScript starts a shell-mediated process.",
                        "processes",
                        severity=(
                            Severity.BLOCKER if runtime_scope else Severity.WARNING
                        ),
                        path=analysis.path,
                        line=call.line,
                        disposition=(
                            Disposition.EXTERNALIZE
                            if runtime_scope
                            else Disposition.TRACK
                        ),
                        remediation=(
                            "Use an argv-based declared process profile."
                            if runtime_scope
                            else "Keep shell-based audit tooling outside product runtime."
                        ),
                        default_path_impact=(
                            "Shell expansion bypasses process/dependency custody."
                            if runtime_scope
                            else "Repository audit tooling cannot own runtime decisions."
                        ),
                        attributes={
                            "callee": call.callee,
                            "runtime_scope": runtime_scope,
                        },
                    )
                )
            elif not command:
                findings.append(
                    finding(
                        "javascript_process_command_nonliteral",
                        "Process executable cannot be resolved statically.",
                        "processes",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.DECLARE,
                        remediation="Bind executable identity to a declared process profile.",
                        default_path_impact="Release process graph is incomplete.",
                        attributes={"callee": call.callee},
                    )
                )
            if executable in INSTALLERS:
                install_command = tokens[1].casefold() if len(tokens) > 1 else ""
                benign_bun_runtime = (
                    executable == "bun"
                    and install_command not in {"add", "install", "x"}
                )
                if not benign_bun_runtime:
                    findings.append(
                        finding(
                            "javascript_runtime_installer_call",
                            f"Runtime invokes package installer {executable!r}.",
                            "processes",
                            severity=Severity.BLOCKER,
                            path=analysis.path,
                            line=call.line,
                            disposition=Disposition.REMOVE,
                            remediation="Resolve and lock dependencies before runtime.",
                            default_path_impact="Runtime can download undeclared executable code.",
                            attributes={
                                "installer": executable,
                                "command": install_command,
                            },
                        )
                    )
        for call in analysis.calls:
            tail = call.callee.rsplit(".", 1)[-1]
            if call.callee not in NETWORK_CALLEES and tail not in NETWORK_CALLEES:
                continue
            for value in call.literal_arguments:
                if URL_PATTERN.match(value) and _download_like(value):
                    findings.append(
                        finding(
                            "javascript_undeclared_download",
                            "Runtime contains a direct code/archive download.",
                            "processes",
                            severity=Severity.BLOCKER,
                            path=analysis.path,
                            line=call.line,
                            disposition=Disposition.EXTERNALIZE,
                            remediation="Move download to checksum-bound build acquisition.",
                            default_path_impact="Runtime can acquire mutable undeclared code.",
                            attributes={"url_digest": content_digest(value.encode())},
                        )
                    )
        return findings

    def _langgraph_findings(
        self, analysis: JavaScriptFileAnalysis
    ) -> list[Finding]:
        findings: list[Finding] = []
        for imported in analysis.imports:
            specifier = imported.specifier.casefold()
            root = _package_root(specifier)
            if root in FORBIDDEN_LANGGRAPH_PACKAGES and (
                any(segment in specifier for segment in FORBIDDEN_LANGGRAPH_SEGMENTS)
                or specifier in FORBIDDEN_LANGGRAPH_PACKAGES
            ):
                findings.append(
                    finding(
                        "langgraph_javascript_runtime_import",
                        f"Default source imports broad LangGraph package {imported.specifier!r}.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=analysis.path,
                        line=imported.line,
                        disposition=Disposition.REMOVE,
                        remediation="Keep dynamic graph, CodeWorker, stores and streams Zyra-owned.",
                        default_path_impact="Broad LangGraph runtime could replace canonical owners.",
                    )
                )
        forbidden_calls = {
            "StateGraph",
            "MessageGraph",
            "Pregel",
            "ToolNode",
            "createReactAgent",
            "create_react_agent",
            "RemoteGraph",
            "StreamController",
        }
        for call in analysis.calls:
            if call.callee.rsplit(".", 1)[-1] in forbidden_calls:
                findings.append(
                    finding(
                        "langgraph_javascript_runtime_call",
                        f"Source calls forbidden broad LangGraph symbol {call.callee!r}.",
                        "langgraph",
                        severity=Severity.BLOCKER,
                        source_repo="langgraph",
                        path=analysis.path,
                        line=call.line,
                        disposition=Disposition.REMOVE,
                        remediation="Use the Zyra-owned runtime boundary.",
                        default_path_impact="Reason/tool loop or graph custody may be duplicated.",
                    )
                )
        return findings

    def _source_specific_findings(
        self, analysis: JavaScriptFileAnalysis
    ) -> list[Finding]:
        findings: list[Finding] = []
        lower_path = analysis.path.casefold()
        for line, value, _ in analysis.strings:
            lowered = value.casefold().replace("\\", "/")
            if (
                any(fragment in lowered for fragment in (".sqlite", ".sqlite3", ".db"))
                and any(fragment in lower_path for fragment in ("provider", "memory", "omp", "job"))
            ):
                findings.append(
                    finding(
                        "javascript_sqlite_state_path",
                        "JavaScript/TypeScript declares a local SQLite/database state path.",
                        "source_specific",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        line=line,
                        disposition=Disposition.DECLARE,
                        remediation="Declare state owner, profile path, restore and package policy.",
                        default_path_impact="Hidden local state can break clean-run evidence.",
                    )
                )
            if any(
                fragment in lowered
                for fragment in ("process.env.home", "userprofile", "~/.")
            ):
                findings.append(
                    finding(
                        "javascript_home_state_path",
                        "JavaScript/TypeScript refers to user-home state.",
                        "source_specific",
                        severity=Severity.ERROR,
                        path=analysis.path,
                        line=line,
                        disposition=Disposition.EXTERNALIZE,
                        remediation="Route state through an explicit Zyra deployment profile.",
                        default_path_impact="Clean-machine and privacy boundaries are ambiguous.",
                    )
                )
        for call in analysis.calls:
            if is_listener_call(call):
                if call.literal_arguments and any(
                    value.isdigit() for value in call.literal_arguments
                ):
                    findings.append(
                        finding(
                            "javascript_literal_port_listener",
                            "JavaScript/TypeScript starts a listener on a literal port.",
                            "source_specific",
                            severity=Severity.WARNING,
                            path=analysis.path,
                            line=call.line,
                            disposition=Disposition.DECLARE,
                            remediation="Bind the port to a declared deployment/process profile.",
                            default_path_impact="Port conflicts and package health are profile-dependent.",
                            attributes={"ports": list(call.literal_arguments)},
                        )
                    )
        return findings


def _identifier_start(character: str) -> bool:
    return character.isalpha() or character in {"_", "$"} or ord(character) >= 128


def _identifier_part(character: str) -> bool:
    return _identifier_start(character) or character.isdigit()


def _render_tokens(tokens: Sequence[Token]) -> str:
    if not tokens:
        return ""
    parts: list[str] = []
    previous: Token | None = None
    for token in tokens:
        value = (
            repr(token.value)
            if token.kind in {"string", "template"}
            else token.value
        )
        if (
            previous is not None
            and previous.kind in {"identifier", "number", "string", "template"}
            and token.kind in {"identifier", "number", "string", "template"}
        ):
            parts.append(" ")
        parts.append(value)
        previous = token
    return "".join(parts)


def _literal_values(tokens: Sequence[Token]) -> tuple[str, ...]:
    return tuple(
        token.value
        for token in tokens
        if token.kind in {"string", "template"} and not token.interpolated
    )


def _package_root(specifier: str) -> str:
    normalized = specifier.replace("\\", "/")
    if normalized.startswith("@"):
        parts = normalized.split("/")
        return "/".join(parts[:2]) if len(parts) >= 2 else normalized
    return normalized.split("/", 1)[0]


def is_process_call(
    analysis: JavaScriptFileAnalysis,
    call: JavaScriptCall,
) -> bool:
    """Return true only for calls bound to a recognized process runtime.

    A method named ``exec`` is common on regular expressions, database handles,
    command models and test doubles. Treating every ``object.exec()`` as a child
    process produces a release-blocking false positive and pollutes the process
    custody queue. Bare imported functions and well-known runtime objects remain
    auditable.
    """

    callee = call.callee
    tail = callee.rsplit(".", 1)[-1]
    if tail not in PROCESS_CALLEES and callee not in PROCESS_CALLEES:
        return False
    if callee == "$":
        return True
    if "." in callee:
        owner = callee.split(".", 1)[0].casefold()
        return owner in PROCESS_OBJECTS
    imported_process_runtime = any(
        imported.specifier.casefold() in PROCESS_IMPORTS
        for imported in analysis.imports
    )
    return imported_process_runtime


def is_listener_call(call: JavaScriptCall) -> bool:
    if call.callee in PORT_CALLEES:
        return True
    lowered = call.callee.casefold()
    if not lowered.endswith(".listen"):
        return False
    owner = lowered.rsplit(".", 1)[0].rsplit(".", 1)[-1]
    return owner in {
        "server",
        "httpserver",
        "httpsserver",
        "socketserver",
        "listener",
    }


def _runtime_dependency_scope(path: str) -> bool:
    normalized = path.replace("\\", "/").casefold()
    if normalized.startswith(("tests/", "docs/", "scripts/")):
        return False
    if "/test/" in normalized or "/tests/" in normalized:
        return False
    if normalized.endswith((".test.ts", ".test.tsx", ".test.js", ".test.jsx")):
        return False
    return normalized.startswith(("apps/", "packages/"))


def _download_like(url: str) -> bool:
    lowered = url.casefold().split("?", 1)[0]
    return any(
        lowered.endswith(suffix)
        for suffix in (
            ".zip",
            ".tar",
            ".tar.gz",
            ".tgz",
            ".exe",
            ".dll",
            ".so",
            ".dylib",
            ".node",
            ".wasm",
            ".jsonl",
        )
    ) or any(
        fragment in lowered
        for fragment in (
            "github.com/releases/download",
            "raw.githubusercontent.com",
            "/install.sh",
            "/install.ps1",
        )
    )
