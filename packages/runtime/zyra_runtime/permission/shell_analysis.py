from __future__ import annotations

"""Structured shell evidence for the Zyra permission boundary.

The permission evaluator must not infer shell safety from a handful of string
prefixes.  This module performs a deliberately non-executing parse of Bash and
PowerShell command text.  It records compound commands, pipelines,
redirections, substitutions, nested interpreters, filesystem targets, and
network egress as immutable evidence.  The parser is conservative: it never
expands variables, invokes a shell, imports a plugin, or treats a successful
parse as execution authority.

This is not intended to be a fully compatible shell implementation.  Its
security contract is narrower and easier to audit:

* retain source spans and quote boundaries;
* recognize control operators before deriving command segments;
* recursively inspect explicit ``-c``/``-Command`` nested shell payloads;
* fail toward review when syntax cannot be classified;
* resolve path evidence against the request workspace without touching files;
* never include secret values in evidence or descriptors.
"""

from bisect import bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
import json
import ntpath
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

from zyra_core import to_jsonable


SHELL_ANALYZER_ID = "zyra-shell-structure-analyzer"
SHELL_ANALYZER_VERSION = "1"


class ShellDialect(StrEnum):
    BASH = "bash"
    POWERSHELL = "powershell"
    UNKNOWN = "unknown"


class ShellTokenKind(StrEnum):
    WORD = "word"
    OPERATOR = "operator"
    REDIRECTION = "redirection"
    SUBSTITUTION = "substitution"
    NEWLINE = "newline"
    COMMENT = "comment"


class ShellEvidenceSeverity(StrEnum):
    INFO = "info"
    REVIEW = "review"
    DENY = "deny"


class ShellEvidenceCategory(StrEnum):
    SYNTAX = "syntax"
    COMPOUND = "compound"
    PIPELINE = "pipeline"
    REDIRECTION = "redirection"
    SUBSTITUTION = "substitution"
    INTERPRETER = "interpreter"
    FILESYSTEM = "filesystem"
    NETWORK = "network"
    SECRET = "secret"
    PROCESS = "process"
    DESTRUCTIVE = "destructive"


_SEVERITY_RANK = {
    ShellEvidenceSeverity.INFO: 0,
    ShellEvidenceSeverity.REVIEW: 1,
    ShellEvidenceSeverity.DENY: 2,
}


@dataclass(frozen=True, slots=True)
class SourceSpan:
    start: int
    end: int
    line: int
    column: int

    def to_dict(self) -> dict[str, int]:
        return {
            "start": self.start,
            "end": self.end,
            "line": self.line,
            "column": self.column,
        }


@dataclass(frozen=True, slots=True)
class ShellToken:
    kind: ShellTokenKind
    value: str
    span: SourceSpan
    raw: str = ""
    quoted: bool = False
    quote_style: str = ""

    def to_dict(self) -> dict[str, Any]:
        structural_value = (
            self.value
            if self.kind in {
                ShellTokenKind.OPERATOR,
                ShellTokenKind.REDIRECTION,
                ShellTokenKind.NEWLINE,
            }
            else ""
        )
        return {
            "kind": str(self.kind),
            "value": structural_value,
            "value_digest": _digest_text(self.value),
            "value_chars": len(self.value),
            "span": self.span.to_dict(),
            "raw_digest": _digest_text(self.raw or self.value),
            "quoted": self.quoted,
            "quote_style": self.quote_style,
        }


@dataclass(frozen=True, slots=True)
class ShellRedirection:
    operator: str
    target: str
    source_fd: str = ""
    target_fd: str = ""
    append: bool = False
    reads: bool = False
    writes: bool = False
    span: SourceSpan | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator": self.operator,
            "target_present": bool(self.target),
            "target_digest": _digest_text(self.target),
            "source_fd": self.source_fd,
            "target_fd": self.target_fd,
            "append": self.append,
            "reads": self.reads,
            "writes": self.writes,
            "span": self.span.to_dict() if self.span else None,
        }


@dataclass(frozen=True, slots=True)
class ShellCommandSegment:
    index: int
    executable: str
    argv: tuple[str, ...]
    assignments: tuple[str, ...] = ()
    redirections: tuple[ShellRedirection, ...] = ()
    substitutions: tuple[str, ...] = ()
    invocation_operator: str = ""
    connector_before: str = ""
    connector_after: str = ""
    background: bool = False
    source_span: SourceSpan | None = None
    raw_digest: str = ""

    @property
    def normalized_executable(self) -> str:
        return _normalize_executable(self.executable)

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "executable": self.normalized_executable,
            "executable_digest": _digest_text(self.executable),
            "normalized_executable": self.normalized_executable,
            "argument_count": max(0, len(self.argv) - 1),
            "argument_digests": [_digest_text(item) for item in self.argv[1:]],
            "assignments": list(self.assignments),
            "redirections": [item.to_dict() for item in self.redirections],
            "substitution_count": len(self.substitutions),
            "substitution_digests": [_digest_text(item) for item in self.substitutions],
            "invocation_operator": self.invocation_operator,
            "connector_before": self.connector_before,
            "connector_after": self.connector_after,
            "background": self.background,
            "source_span": self.source_span.to_dict() if self.source_span else None,
            "raw_digest": self.raw_digest,
        }


@dataclass(frozen=True, slots=True)
class ShellPipeline:
    index: int
    command_indexes: tuple[int, ...]
    operators: tuple[str, ...] = ()
    connector_before: str = ""
    connector_after: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "command_indexes": list(self.command_indexes),
            "operators": list(self.operators),
            "connector_before": self.connector_before,
            "connector_after": self.connector_after,
        }


@dataclass(frozen=True, slots=True)
class ShellRiskEvidence:
    code: str
    severity: ShellEvidenceSeverity
    category: ShellEvidenceCategory
    message: str
    segment_index: int | None = None
    span: SourceSpan | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "category": str(self.category),
            "message": self.message,
            "segment_index": self.segment_index,
            "span": self.span.to_dict() if self.span else None,
            "metadata": _safe_metadata(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ShellAnalysisResult:
    dialect: ShellDialect
    command_digest: str
    token_digest: str
    tokens: tuple[ShellToken, ...]
    commands: tuple[ShellCommandSegment, ...]
    pipelines: tuple[ShellPipeline, ...]
    evidence: tuple[ShellRiskEvidence, ...]
    parse_errors: tuple[str, ...] = ()
    nested: tuple["ShellAnalysisResult", ...] = ()
    analyzer_id: str = SHELL_ANALYZER_ID
    analyzer_version: str = SHELL_ANALYZER_VERSION

    @property
    def maximum_severity(self) -> ShellEvidenceSeverity:
        return max(
            (item.severity for item in self.all_evidence()),
            key=lambda value: _SEVERITY_RANK[value],
            default=ShellEvidenceSeverity.INFO,
        )

    @property
    def hard_denied(self) -> bool:
        return self.maximum_severity is ShellEvidenceSeverity.DENY

    @property
    def requires_approval(self) -> bool:
        return self.maximum_severity in {
            ShellEvidenceSeverity.REVIEW,
            ShellEvidenceSeverity.DENY,
        }

    @property
    def safe_read_only(self) -> bool:
        if self.parse_errors or self.requires_approval or not self.commands:
            return False
        return all(
            command.normalized_executable in _LOCAL_READ_COMMANDS
            and not any(redirection.writes for redirection in command.redirections)
            for command in self.commands
        )

    @property
    def evidence_digest(self) -> str:
        return _stable_digest([item.to_dict() for item in self.all_evidence()])

    def evidence_codes(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.code for item in self.all_evidence()))

    def all_evidence(self) -> tuple[ShellRiskEvidence, ...]:
        nested = tuple(item for result in self.nested for item in result.all_evidence())
        return (*self.evidence, *nested)

    def policy_metadata(self) -> dict[str, Any]:
        return {
            "analyzer_id": self.analyzer_id,
            "analyzer_version": self.analyzer_version,
            "dialect": str(self.dialect),
            "command_digest": self.command_digest,
            "token_digest": self.token_digest,
            "evidence_digest": self.evidence_digest,
            "evidence_codes": list(self.evidence_codes()),
            "maximum_severity": str(self.maximum_severity),
            "hard_denied": self.hard_denied,
            "requires_approval": self.requires_approval,
            "safe_read_only": self.safe_read_only,
            "command_count": len(self.commands),
            "pipeline_count": len(self.pipelines),
            "nested_analysis_count": len(self.nested),
            "parse_error_count": len(self.parse_errors),
        }

    def to_dict(
        self,
        *,
        include_tokens: bool = True,
        include_commands: bool = True,
        include_nested: bool = True,
    ) -> dict[str, Any]:
        return {
            **self.policy_metadata(),
            "tokens": [item.to_dict() for item in self.tokens] if include_tokens else [],
            "commands": [item.to_dict() for item in self.commands] if include_commands else [],
            "pipelines": [item.to_dict() for item in self.pipelines],
            "evidence": [item.to_dict() for item in self.evidence],
            "parse_errors": list(self.parse_errors),
            "nested": [
                item.to_dict(
                    include_tokens=include_tokens,
                    include_commands=include_commands,
                    include_nested=True,
                )
                for item in self.nested
            ]
            if include_nested
            else [],
        }


@dataclass(frozen=True, slots=True)
class SecretEgressEvidence:
    secret_key_names: tuple[str, ...]
    secret_reference_count: int
    external_destination_count: int
    destination_domains: tuple[str, ...]
    tool_is_remote: bool
    egress_detected: bool
    secret_detected: bool
    hard_denied: bool
    evidence_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "secret_key_names": list(self.secret_key_names),
            "secret_reference_count": self.secret_reference_count,
            "external_destination_count": self.external_destination_count,
            "destination_domains": list(self.destination_domains),
            "tool_is_remote": self.tool_is_remote,
            "egress_detected": self.egress_detected,
            "secret_detected": self.secret_detected,
            "hard_denied": self.hard_denied,
            "evidence_digest": self.evidence_digest,
        }


@dataclass(frozen=True, slots=True)
class ShellAnalyzerConfig:
    max_nested_depth: int = 3
    max_command_chars: int = 64_000
    fail_parse_as_review: bool = True
    deny_encoded_powershell: bool = True
    deny_workspace_escape: bool = True
    deny_secret_egress: bool = True

    def __post_init__(self) -> None:
        if self.max_nested_depth < 0 or self.max_nested_depth > 8:
            raise ValueError("max_nested_depth must be between zero and eight")
        if self.max_command_chars < 1:
            raise ValueError("max_command_chars must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_nested_depth": self.max_nested_depth,
            "max_command_chars": self.max_command_chars,
            "fail_parse_as_review": self.fail_parse_as_review,
            "deny_encoded_powershell": self.deny_encoded_powershell,
            "deny_workspace_escape": self.deny_workspace_escape,
            "deny_secret_egress": self.deny_secret_egress,
        }


class ShellCommandAnalyzer:
    """Parse command text into deterministic, non-authoritative risk evidence."""

    def __init__(self, config: ShellAnalyzerConfig | None = None) -> None:
        self.config = config or ShellAnalyzerConfig()

    @property
    def config_digest(self) -> str:
        return _stable_digest(self.config.to_dict())

    def descriptor(self) -> dict[str, Any]:
        return {
            "analyzer_id": SHELL_ANALYZER_ID,
            "version": SHELL_ANALYZER_VERSION,
            "config_digest": self.config_digest,
            "executes_commands": False,
            "dynamic_imports": False,
            "authoritative_allow": False,
            "supported_dialects": [str(ShellDialect.BASH), str(ShellDialect.POWERSHELL)],
            "evidence_categories": [str(item) for item in ShellEvidenceCategory],
        }

    def analyze_arguments(
        self,
        arguments: Mapping[str, Any],
        *,
        tool_name: str = "",
        workspace_root: str | Path | None = None,
        dialect: ShellDialect | str | None = None,
    ) -> ShellAnalysisResult:
        command = _first_command(arguments)
        selected = dialect or infer_shell_dialect(tool_name, command, arguments)
        return self.analyze(
            command,
            dialect=selected,
            workspace_root=workspace_root,
            argument_context=arguments,
        )

    def analyze(
        self,
        command: str,
        *,
        dialect: ShellDialect | str | None = None,
        workspace_root: str | Path | None = None,
        argument_context: Mapping[str, Any] | None = None,
        _depth: int = 0,
    ) -> ShellAnalysisResult:
        text = str(command or "")
        selected = _dialect(dialect or infer_shell_dialect("", text, argument_context or {}))
        command_digest = _digest_text(text)
        if len(text) > self.config.max_command_chars:
            evidence = ShellRiskEvidence(
                code="syntax.command_too_large",
                severity=ShellEvidenceSeverity.DENY,
                category=ShellEvidenceCategory.SYNTAX,
                message="shell command exceeds the bounded parser input size",
                metadata={"command_chars": len(text), "limit": self.config.max_command_chars},
            )
            return ShellAnalysisResult(
                dialect=selected,
                command_digest=command_digest,
                token_digest=_stable_digest([]),
                tokens=(),
                commands=(),
                pipelines=(),
                evidence=(evidence,),
                parse_errors=("command_too_large",),
            )

        scanner = _ShellScanner(text, selected)
        tokens, scan_errors = scanner.scan()
        commands, pipelines, parse_errors = _parse_commands(tokens, selected, text)
        errors = (*scan_errors, *parse_errors)
        evidence: list[ShellRiskEvidence] = []
        nested: list[ShellAnalysisResult] = []

        if not text.strip():
            evidence.append(
                ShellRiskEvidence(
                    code="syntax.empty_command",
                    severity=ShellEvidenceSeverity.REVIEW,
                    category=ShellEvidenceCategory.SYNTAX,
                    message="empty shell command cannot be authorized",
                )
            )
        if errors and self.config.fail_parse_as_review:
            evidence.append(
                ShellRiskEvidence(
                    code="syntax.parse_uncertain",
                    severity=ShellEvidenceSeverity.REVIEW,
                    category=ShellEvidenceCategory.SYNTAX,
                    message="shell syntax was not fully classified",
                    metadata={"error_codes": sorted(set(errors)), "error_count": len(errors)},
                )
            )

        evidence.extend(_control_flow_evidence(tokens, commands, pipelines))
        secret_context = inspect_secret_egress(
            argument_context or {},
            tool_name="shell",
            server_name="",
            command=text,
        )
        for segment in commands:
            segment_evidence, nested_payloads = self._segment_evidence(
                segment,
                dialect=selected,
                workspace_root=workspace_root,
                secret_context=secret_context,
            )
            evidence.extend(segment_evidence)
            if _depth >= self.config.max_nested_depth:
                if nested_payloads:
                    evidence.append(
                        ShellRiskEvidence(
                            code="interpreter.nesting_limit",
                            severity=ShellEvidenceSeverity.DENY,
                            category=ShellEvidenceCategory.INTERPRETER,
                            message="nested interpreter depth exceeds the bounded analysis limit",
                            segment_index=segment.index,
                            metadata={"max_nested_depth": self.config.max_nested_depth},
                        )
                    )
                continue
            for nested_dialect, payload in nested_payloads:
                nested_result = self.analyze(
                    payload,
                    dialect=nested_dialect,
                    workspace_root=workspace_root,
                    argument_context=argument_context,
                    _depth=_depth + 1,
                )
                nested.append(nested_result)
                evidence.append(
                    ShellRiskEvidence(
                        code="interpreter.nested_shell",
                        severity=ShellEvidenceSeverity.REVIEW,
                        category=ShellEvidenceCategory.INTERPRETER,
                        message="command launches a nested shell payload",
                        segment_index=segment.index,
                        metadata={
                            "nested_dialect": str(nested_dialect),
                            "nested_command_digest": nested_result.command_digest,
                            "nested_evidence_digest": nested_result.evidence_digest,
                        },
                    )
                )

        evidence = _deduplicate_evidence(evidence)
        return ShellAnalysisResult(
            dialect=selected,
            command_digest=command_digest,
            token_digest=_stable_digest([item.to_dict() for item in tokens]),
            tokens=tuple(tokens),
            commands=tuple(commands),
            pipelines=tuple(pipelines),
            evidence=tuple(evidence),
            parse_errors=tuple(errors),
            nested=tuple(nested),
        )

    def _segment_evidence(
        self,
        segment: ShellCommandSegment,
        *,
        dialect: ShellDialect,
        workspace_root: str | Path | None,
        secret_context: SecretEgressEvidence,
    ) -> tuple[list[ShellRiskEvidence], list[tuple[ShellDialect, str]]]:
        evidence: list[ShellRiskEvidence] = []
        nested_payloads: list[tuple[ShellDialect, str]] = []
        executable = segment.normalized_executable
        argv = list(segment.argv)

        for substitution in segment.substitutions:
            evidence.append(
                ShellRiskEvidence(
                    code="substitution.command",
                    severity=ShellEvidenceSeverity.REVIEW,
                    category=ShellEvidenceCategory.SUBSTITUTION,
                    message="command substitution executes a nested expression before the outer command",
                    segment_index=segment.index,
                    metadata={"substitution_digest": _digest_text(substitution)},
                )
            )

        for redirection in segment.redirections:
            evidence.extend(
                _redirection_evidence(
                    redirection,
                    segment_index=segment.index,
                    workspace_root=workspace_root,
                    deny_workspace_escape=self.config.deny_workspace_escape,
                )
            )

        if executable in _DYNAMIC_EVAL_COMMANDS:
            evidence.append(
                ShellRiskEvidence(
                    code="interpreter.dynamic_eval",
                    severity=ShellEvidenceSeverity.DENY,
                    category=ShellEvidenceCategory.INTERPRETER,
                    message="dynamic evaluation bypasses static command boundaries",
                    segment_index=segment.index,
                    metadata={"executable": executable},
                )
            )

        if executable in _PROCESS_LAUNCHERS:
            evidence.append(
                ShellRiskEvidence(
                    code="process.dynamic_launch",
                    severity=ShellEvidenceSeverity.REVIEW,
                    category=ShellEvidenceCategory.PROCESS,
                    message="command dynamically launches another process",
                    segment_index=segment.index,
                    metadata={"executable": executable},
                )
            )

        nested_dialect = _NESTED_SHELLS.get(executable)
        if nested_dialect is not None:
            payload = _nested_shell_payload(argv, nested_dialect)
            if payload.encoded:
                evidence.append(
                    ShellRiskEvidence(
                        code="interpreter.encoded_command",
                        severity=(
                            ShellEvidenceSeverity.DENY
                            if self.config.deny_encoded_powershell
                            else ShellEvidenceSeverity.REVIEW
                        ),
                        category=ShellEvidenceCategory.INTERPRETER,
                        message="encoded interpreter payload cannot be inspected as submitted",
                        segment_index=segment.index,
                        metadata={"interpreter": executable},
                    )
                )
            elif payload.value:
                nested_payloads.append((nested_dialect, payload.value))
            else:
                evidence.append(
                    ShellRiskEvidence(
                        code="interpreter.nested_process",
                        severity=ShellEvidenceSeverity.REVIEW,
                        category=ShellEvidenceCategory.INTERPRETER,
                        message="shell interpreter launch requires an exact execution scope",
                        segment_index=segment.index,
                        metadata={"interpreter": executable},
                    )
                )

        if executable in _GENERAL_INTERPRETERS:
            evidence.append(
                ShellRiskEvidence(
                    code="interpreter.general_code",
                    severity=ShellEvidenceSeverity.REVIEW,
                    category=ShellEvidenceCategory.INTERPRETER,
                    message="general-purpose interpreter can create effects beyond the shell token surface",
                    segment_index=segment.index,
                    metadata={"interpreter": executable},
                )
            )

        network = _network_evidence(segment)
        evidence.extend(network)
        if network and secret_context.secret_detected and self.config.deny_secret_egress:
            evidence.append(
                ShellRiskEvidence(
                    code="secret.external_egress",
                    severity=ShellEvidenceSeverity.DENY,
                    category=ShellEvidenceCategory.SECRET,
                    message="secret-bearing data cannot cross an external command boundary",
                    segment_index=segment.index,
                    metadata={
                        "secret_key_names": list(secret_context.secret_key_names),
                        "secret_reference_count": secret_context.secret_reference_count,
                        "destination_domains": list(secret_context.destination_domains),
                    },
                )
            )

        evidence.extend(_destructive_evidence(segment, dialect))
        evidence.extend(_path_argument_evidence(segment, workspace_root, self.config.deny_workspace_escape))
        return evidence, nested_payloads


@dataclass(frozen=True, slots=True)
class _NestedPayload:
    value: str = ""
    encoded: bool = False


class _ShellScanner:
    def __init__(self, source: str, dialect: ShellDialect) -> None:
        self.source = source
        self.dialect = dialect
        self.length = len(source)
        self.index = 0
        self.newlines = [-1, *[index for index, char in enumerate(source) if char == "\n"]]
        self.errors: list[str] = []

    def scan(self) -> tuple[list[ShellToken], tuple[str, ...]]:
        tokens: list[ShellToken] = []
        while self.index < self.length:
            char = self.source[self.index]
            if char in " \t\r":
                self.index += 1
                continue
            if char == "\n":
                start = self.index
                self.index += 1
                tokens.append(self._token(ShellTokenKind.NEWLINE, "\n", start, self.index))
                continue
            if char == "#" and self._comment_boundary():
                tokens.append(self._read_comment())
                continue
            redirection = self._match_redirection()
            if redirection is not None:
                value, start, end = redirection
                tokens.append(self._token(ShellTokenKind.REDIRECTION, value, start, end))
                continue
            operator = self._match_operator()
            if operator is not None:
                value, start, end = operator
                tokens.append(self._token(ShellTokenKind.OPERATOR, value, start, end))
                continue
            tokens.extend(self._read_word())
        return tokens, tuple(self.errors)

    def _read_word(self) -> list[ShellToken]:
        output: list[ShellToken] = []
        start = self.index
        parts: list[str] = []
        quoted = False
        quote_styles: list[str] = []

        def flush_word(end: int) -> None:
            nonlocal start, parts, quoted, quote_styles
            if not parts:
                start = end
                return
            value = "".join(parts)
            output.append(
                self._token(
                    ShellTokenKind.WORD,
                    value,
                    start,
                    end,
                    quoted=quoted,
                    quote_style="+".join(dict.fromkeys(quote_styles)),
                )
            )
            parts = []
            quoted = False
            quote_styles = []
            start = end

        while self.index < self.length:
            char = self.source[self.index]
            if char in " \t\r\n":
                break
            if char == "#" and not parts and self._comment_boundary():
                break
            if self._would_start_operator_or_redirection():
                break
            if char in {"'", '"'}:
                quote_start = self.index
                content, style, closed = self._read_quoted(char)
                parts.append(content)
                quoted = True
                quote_styles.append(style)
                if not closed:
                    self.errors.append(f"unclosed_{style}_quote")
                if self.index == quote_start:
                    self.index += 1
                continue
            if self.dialect is ShellDialect.BASH and char == "`":
                flush_word(self.index)
                substitution_start = self.index
                value, closed = self._read_backtick_substitution()
                output.append(
                    self._token(
                        ShellTokenKind.SUBSTITUTION,
                        value,
                        substitution_start,
                        self.index,
                        quoted=True,
                        quote_style="backtick",
                    )
                )
                if not closed:
                    self.errors.append("unclosed_backtick_substitution")
                start = self.index
                continue
            if char == "$" and self.index + 1 < self.length and self.source[self.index + 1] == "(":
                flush_word(self.index)
                substitution_start = self.index
                value, closed = self._read_parenthesized_substitution()
                output.append(
                    self._token(
                        ShellTokenKind.SUBSTITUTION,
                        value,
                        substitution_start,
                        self.index,
                        quoted=False,
                        quote_style="subexpression" if self.dialect is ShellDialect.POWERSHELL else "command",
                    )
                )
                if not closed:
                    self.errors.append("unclosed_command_substitution")
                start = self.index
                continue
            if char == "\\" and self.dialect is ShellDialect.BASH:
                if self.index + 1 >= self.length:
                    self.errors.append("dangling_escape")
                    self.index += 1
                    break
                parts.append(self.source[self.index + 1])
                self.index += 2
                continue
            if char == "`" and self.dialect is ShellDialect.POWERSHELL:
                if self.index + 1 >= self.length:
                    self.errors.append("dangling_powershell_escape")
                    self.index += 1
                    break
                parts.append(self.source[self.index + 1])
                self.index += 2
                continue
            parts.append(char)
            self.index += 1
        flush_word(self.index)
        if not output and self.index < self.length:
            # Unknown punctuation must make progress and remain visible.
            unknown_start = self.index
            self.index += 1
            output.append(self._token(ShellTokenKind.WORD, self.source[unknown_start], unknown_start, self.index))
        return output

    def _read_quoted(self, quote: str) -> tuple[str, str, bool]:
        style = "single" if quote == "'" else "double"
        self.index += 1
        parts: list[str] = []
        while self.index < self.length:
            char = self.source[self.index]
            if char == quote:
                if self.dialect is ShellDialect.POWERSHELL and quote == "'" and self.index + 1 < self.length and self.source[self.index + 1] == "'":
                    parts.append("'")
                    self.index += 2
                    continue
                self.index += 1
                return "".join(parts), style, True
            if self.dialect is ShellDialect.BASH and quote == '"' and char == "\\" and self.index + 1 < self.length:
                parts.append(self.source[self.index + 1])
                self.index += 2
                continue
            if self.dialect is ShellDialect.POWERSHELL and char == "`" and self.index + 1 < self.length:
                parts.append(self.source[self.index + 1])
                self.index += 2
                continue
            parts.append(char)
            self.index += 1
        return "".join(parts), style, False

    def _read_parenthesized_substitution(self) -> tuple[str, bool]:
        self.index += 2
        start = self.index
        depth = 1
        quote = ""
        while self.index < self.length:
            char = self.source[self.index]
            if quote:
                if char == quote:
                    quote = ""
                elif char in {"\\", "`"} and self.index + 1 < self.length:
                    self.index += 1
                self.index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                self.index += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    value = self.source[start:self.index]
                    self.index += 1
                    return value, True
            self.index += 1
        return self.source[start:self.index], False

    def _read_backtick_substitution(self) -> tuple[str, bool]:
        self.index += 1
        start = self.index
        while self.index < self.length:
            char = self.source[self.index]
            if char == "`":
                value = self.source[start:self.index]
                self.index += 1
                return value, True
            if char == "\\" and self.index + 1 < self.length:
                self.index += 2
                continue
            self.index += 1
        return self.source[start:self.index], False

    def _read_comment(self) -> ShellToken:
        start = self.index
        while self.index < self.length and self.source[self.index] != "\n":
            self.index += 1
        return self._token(ShellTokenKind.COMMENT, self.source[start + 1:self.index], start, self.index)

    def _match_redirection(self) -> tuple[str, int, int] | None:
        pattern = _POWERSHELL_REDIRECTION if self.dialect is ShellDialect.POWERSHELL else _BASH_REDIRECTION
        match = pattern.match(self.source, self.index)
        if match is None:
            return None
        value = match.group(0)
        start = self.index
        self.index = match.end()
        return value, start, self.index

    def _match_operator(self) -> tuple[str, int, int] | None:
        operators = _POWERSHELL_OPERATORS if self.dialect is ShellDialect.POWERSHELL else _BASH_OPERATORS
        for operator in operators:
            if self.source.startswith(operator, self.index):
                start = self.index
                self.index += len(operator)
                return operator, start, self.index
        return None

    def _would_start_operator_or_redirection(self) -> bool:
        saved = self.index
        redirection = self._match_redirection()
        self.index = saved
        if redirection is not None:
            return True
        operator = self._match_operator()
        self.index = saved
        return operator is not None

    def _comment_boundary(self) -> bool:
        return self.index == 0 or self.source[self.index - 1].isspace() or self.source[self.index - 1] in ";|&(){}"

    def _token(
        self,
        kind: ShellTokenKind,
        value: str,
        start: int,
        end: int,
        *,
        quoted: bool = False,
        quote_style: str = "",
    ) -> ShellToken:
        line_index = bisect_right(self.newlines, start) - 1
        line_start = self.newlines[max(0, line_index)]
        span = SourceSpan(start=start, end=end, line=line_index + 1, column=start - line_start)
        return ShellToken(
            kind=kind,
            value=value,
            span=span,
            raw=self.source[start:end],
            quoted=quoted,
            quote_style=quote_style,
        )


_BASH_REDIRECTION = re.compile(r"(?:\d+|&)?(?:<<<|<<|>>|<>|>&|<&|&>|>|<)(?:&\d+|-)?")
_POWERSHELL_REDIRECTION = re.compile(r"(?:\*|\d+)?(?:>>|>|<)(?:&\d+)?")
_BASH_OPERATORS = (";;&", "|&", "&&", "||", ";;", ";&", "(", ")", "{", "}", ";", "|", "&")
_POWERSHELL_OPERATORS = ("&&", "||", "@(", "@{", "(", ")", "{", "}", ";", "|", "&")
_COMMAND_BREAKS = {";", "&&", "||", "&", "\n"}
_PIPE_OPERATORS = {"|", "|&"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)


def _parse_commands(
    tokens: Sequence[ShellToken],
    dialect: ShellDialect,
    source: str,
) -> tuple[list[ShellCommandSegment], list[ShellPipeline], tuple[str, ...]]:
    commands: list[ShellCommandSegment] = []
    pipelines: list[ShellPipeline] = []
    errors: list[str] = []
    current: list[ShellToken] = []
    current_pipeline: list[int] = []
    pipeline_operators: list[str] = []
    connector_before = ""
    group_depth = 0

    def finish_command(connector_after: str = "") -> None:
        nonlocal current, connector_before
        significant = [item for item in current if item.kind is not ShellTokenKind.COMMENT]
        if not significant:
            current = []
            return
        command = _build_segment(
            len(commands),
            significant,
            connector_before=connector_before,
            connector_after=connector_after,
            dialect=dialect,
            source=source,
        )
        commands.append(command)
        current_pipeline.append(command.index)
        current = []
        connector_before = connector_after

    def finish_pipeline(connector_after: str = "") -> None:
        nonlocal current_pipeline, pipeline_operators
        if not current_pipeline:
            return
        first = commands[current_pipeline[0]]
        pipelines.append(
            ShellPipeline(
                index=len(pipelines),
                command_indexes=tuple(current_pipeline),
                operators=tuple(pipeline_operators),
                connector_before=first.connector_before,
                connector_after=connector_after,
            )
        )
        current_pipeline = []
        pipeline_operators = []

    for token in tokens:
        value = "\n" if token.kind is ShellTokenKind.NEWLINE else token.value
        if token.kind is ShellTokenKind.OPERATOR and value in {"(", "{", "@(", "@{"}:
            group_depth += 1
            current.append(token)
            continue
        if token.kind is ShellTokenKind.OPERATOR and value in {")", "}"}:
            group_depth -= 1
            if group_depth < 0:
                errors.append("unmatched_group_close")
                group_depth = 0
            current.append(token)
            continue
        if group_depth == 0 and value in _PIPE_OPERATORS:
            finish_command(value)
            if not current_pipeline:
                errors.append("pipeline_without_left_command")
            pipeline_operators.append(value)
            connector_before = value
            continue
        if group_depth == 0 and value in _COMMAND_BREAKS:
            if dialect is ShellDialect.POWERSHELL and value == "&" and not current:
                current.append(token)
                continue
            finish_command(value)
            finish_pipeline(value)
            connector_before = value
            continue
        current.append(token)
    finish_command("")
    finish_pipeline("")
    if group_depth:
        errors.append("unclosed_command_group")
    return commands, pipelines, tuple(errors)


def _build_segment(
    index: int,
    tokens: Sequence[ShellToken],
    *,
    connector_before: str,
    connector_after: str,
    dialect: ShellDialect,
    source: str,
) -> ShellCommandSegment:
    argv: list[str] = []
    assignments: list[str] = []
    redirections: list[ShellRedirection] = []
    substitutions: list[str] = []
    invocation_operator = ""
    index_value = 0
    while index_value < len(tokens):
        token = tokens[index_value]
        if token.kind is ShellTokenKind.REDIRECTION:
            target = ""
            if index_value + 1 < len(tokens) and tokens[index_value + 1].kind in {
                ShellTokenKind.WORD,
                ShellTokenKind.SUBSTITUTION,
            }:
                target = tokens[index_value + 1].value
                index_value += 1
            redirections.append(_redirection(token, target))
        elif token.kind is ShellTokenKind.SUBSTITUTION:
            substitutions.append(token.value)
            argv.append(f"$({_digest_text(token.value)[:12]})")
        elif token.kind is ShellTokenKind.WORD:
            substitutions.extend(_embedded_substitutions(token.raw, dialect))
            if not argv and _ASSIGNMENT.match(token.value):
                assignments.append(token.value.split("=", 1)[0])
            else:
                argv.append(token.value)
        elif token.kind is ShellTokenKind.OPERATOR:
            if dialect is ShellDialect.POWERSHELL and token.value in {"&", "."} and not argv:
                invocation_operator = token.value
            else:
                argv.append(token.value)
        index_value += 1
    executable = argv[0] if argv else ""
    start = tokens[0].span.start if tokens else 0
    end = tokens[-1].span.end if tokens else start
    span = SourceSpan(start, end, tokens[0].span.line if tokens else 1, tokens[0].span.column if tokens else 1)
    background = connector_after == "&" and dialect is ShellDialect.BASH
    return ShellCommandSegment(
        index=index,
        executable=executable,
        argv=tuple(argv),
        assignments=tuple(assignments),
        redirections=tuple(redirections),
        substitutions=tuple(substitutions),
        invocation_operator=invocation_operator,
        connector_before=connector_before,
        connector_after=connector_after,
        background=background,
        source_span=span,
        raw_digest=_digest_text(source[start:end]),
    )


def _redirection(token: ShellToken, target: str) -> ShellRedirection:
    operator = token.value
    source_match = re.match(r"^(\d+|\*)", operator)
    source_fd = source_match.group(1) if source_match else ""
    target_match = re.search(r"&([0-9]+|-)$", operator)
    target_fd = target_match.group(1) if target_match else ""
    reads = "<" in operator and ">" not in operator
    writes = ">" in operator or operator.startswith("&>") or "<>" in operator
    return ShellRedirection(
        operator=operator,
        target=target,
        source_fd=source_fd,
        target_fd=target_fd,
        append=">>" in operator,
        reads=reads,
        writes=writes,
        span=token.span,
    )


def _embedded_substitutions(raw: str, dialect: ShellDialect) -> list[str]:
    """Extract substitutions that appeared inside a quoted/compound word."""

    output: list[str] = []
    index = 0
    while index < len(raw):
        if raw.startswith("$(", index):
            start = index + 2
            depth = 1
            cursor = start
            quote = ""
            while cursor < len(raw):
                char = raw[cursor]
                if quote:
                    if char == quote:
                        quote = ""
                    elif char in {"\\", "`"} and cursor + 1 < len(raw):
                        cursor += 1
                    cursor += 1
                    continue
                if char in {"'", '"'}:
                    quote = char
                elif char == "(":
                    depth += 1
                elif char == ")":
                    depth -= 1
                    if depth == 0:
                        output.append(raw[start:cursor])
                        index = cursor + 1
                        break
                cursor += 1
            else:
                index = len(raw)
            continue
        if dialect is ShellDialect.BASH and raw[index] == "`":
            cursor = index + 1
            while cursor < len(raw):
                if raw[cursor] == "`":
                    output.append(raw[index + 1:cursor])
                    index = cursor + 1
                    break
                if raw[cursor] == "\\" and cursor + 1 < len(raw):
                    cursor += 1
                cursor += 1
            else:
                index = len(raw)
            continue
        index += 1
    return output


def _control_flow_evidence(
    tokens: Sequence[ShellToken],
    commands: Sequence[ShellCommandSegment],
    pipelines: Sequence[ShellPipeline],
) -> list[ShellRiskEvidence]:
    evidence: list[ShellRiskEvidence] = []
    compound = [
        item.value
        for item in tokens
        if item.kind in {ShellTokenKind.OPERATOR, ShellTokenKind.NEWLINE}
        and (item.value in _COMMAND_BREAKS or item.kind is ShellTokenKind.NEWLINE)
    ]
    if len(commands) > 1 and compound:
        evidence.append(
            ShellRiskEvidence(
                code="compound.multiple_commands",
                severity=ShellEvidenceSeverity.REVIEW,
                category=ShellEvidenceCategory.COMPOUND,
                message="compound shell input contains multiple command segments",
                metadata={"operators": sorted(set(compound)), "command_count": len(commands)},
            )
        )
    if any(len(item.command_indexes) > 1 for item in pipelines):
        evidence.append(
            ShellRiskEvidence(
                code="pipeline.multiple_commands",
                severity=ShellEvidenceSeverity.REVIEW,
                category=ShellEvidenceCategory.PIPELINE,
                message="pipeline connects output between multiple commands",
                metadata={"pipeline_count": len(pipelines)},
            )
        )
    if any(item.background for item in commands):
        evidence.append(
            ShellRiskEvidence(
                code="process.background",
                severity=ShellEvidenceSeverity.REVIEW,
                category=ShellEvidenceCategory.PROCESS,
                message="background execution can outlive the permission transaction",
            )
        )
    return evidence


def _redirection_evidence(
    redirection: ShellRedirection,
    *,
    segment_index: int,
    workspace_root: str | Path | None,
    deny_workspace_escape: bool,
) -> list[ShellRiskEvidence]:
    evidence: list[ShellRiskEvidence] = []
    if not redirection.target and not redirection.target_fd:
        evidence.append(
            ShellRiskEvidence(
                code="redirection.missing_target",
                severity=ShellEvidenceSeverity.REVIEW,
                category=ShellEvidenceCategory.REDIRECTION,
                message="redirection target is missing or dynamically generated",
                segment_index=segment_index,
                span=redirection.span,
            )
        )
        return evidence
    if redirection.writes:
        evidence.append(
            ShellRiskEvidence(
                code="redirection.write",
                severity=ShellEvidenceSeverity.REVIEW,
                category=ShellEvidenceCategory.REDIRECTION,
                message="shell redirection writes command output",
                segment_index=segment_index,
                span=redirection.span,
                metadata={"operator": redirection.operator, "append": redirection.append},
            )
        )
    elif redirection.reads:
        evidence.append(
            ShellRiskEvidence(
                code="redirection.read",
                severity=ShellEvidenceSeverity.INFO,
                category=ShellEvidenceCategory.REDIRECTION,
                message="shell input is read from a filesystem target",
                segment_index=segment_index,
                span=redirection.span,
            )
        )
    if redirection.target and not redirection.target_fd:
        path = inspect_path_scope(redirection.target, workspace_root)
        if path["unc"]:
            evidence.append(
                ShellRiskEvidence(
                    code="filesystem.unc_target",
                    severity=ShellEvidenceSeverity.DENY,
                    category=ShellEvidenceCategory.FILESYSTEM,
                    message="UNC/device redirection target crosses a credential-bearing boundary",
                    segment_index=segment_index,
                    metadata=path,
                )
            )
        elif path["outside_workspace"]:
            evidence.append(
                ShellRiskEvidence(
                    code="filesystem.path_escape",
                    severity=(ShellEvidenceSeverity.DENY if deny_workspace_escape else ShellEvidenceSeverity.REVIEW),
                    category=ShellEvidenceCategory.FILESYSTEM,
                    message="redirection target resolves outside the authorized workspace",
                    segment_index=segment_index,
                    metadata=path,
                )
            )
    return evidence


def _network_evidence(segment: ShellCommandSegment) -> list[ShellRiskEvidence]:
    executable = segment.normalized_executable
    argv_lower = [item.casefold() for item in segment.argv[1:]]
    domains = sorted(_domains_from_values(segment.argv))
    egress = executable in _NETWORK_COMMANDS or bool(domains)
    if executable == "git" and not any(item in {"push", "fetch", "pull", "clone", "ls-remote"} for item in argv_lower):
        egress = bool(domains)
    if not egress:
        return []
    upload = (
        executable in _UPLOAD_COMMANDS
        or any(item in {"push", "publish", "upload", "post", "put", "--upload-file", "-t"} for item in argv_lower)
        or any(item.startswith("--data") or item.startswith("--form") for item in argv_lower)
    )
    return [
        ShellRiskEvidence(
            code="network.external_egress" if upload else "network.external_access",
            severity=ShellEvidenceSeverity.REVIEW,
            category=ShellEvidenceCategory.NETWORK,
            message="command communicates with an external destination",
            segment_index=segment.index,
            metadata={
                "executable": executable,
                "destination_domains": domains,
                "upload_capable": upload,
            },
        )
    ]


def _destructive_evidence(segment: ShellCommandSegment, dialect: ShellDialect) -> list[ShellRiskEvidence]:
    executable = segment.normalized_executable
    args = [item.casefold() for item in segment.argv[1:]]
    destructive = False
    reason = ""
    if executable in {"rm", "rmdir", "del", "erase", "remove-item"}:
        destructive = any(item in {"-rf", "-fr", "-r", "-recurse", "/s", "/q"} for item in args)
        reason = "recursive filesystem deletion"
    elif executable == "git":
        destructive = (
            ("reset" in args and "--hard" in args)
            or ("clean" in args and any("f" in item.lstrip("-") for item in args if item.startswith("-")))
            or ("push" in args and any(item in {"--force", "-f"} for item in args))
        )
        reason = "destructive Git history or workspace operation"
    elif executable in {"format", "mkfs", "diskpart", "shutdown", "reboot"}:
        destructive = True
        reason = "irreversible system command"
    if not destructive:
        return []
    return [
        ShellRiskEvidence(
            code="destructive.irreversible_command",
            severity=ShellEvidenceSeverity.DENY,
            category=ShellEvidenceCategory.DESTRUCTIVE,
            message=reason,
            segment_index=segment.index,
            metadata={"executable": executable, "dialect": str(dialect)},
        )
    ]


def _path_argument_evidence(
    segment: ShellCommandSegment,
    workspace_root: str | Path | None,
    deny_workspace_escape: bool,
) -> list[ShellRiskEvidence]:
    evidence: list[ShellRiskEvidence] = []
    candidates: list[tuple[str, str]] = []
    expect_path = False
    executable = _normalize_executable(segment.executable)
    if executable in {"cmd", "cmd.exe"} and any(
        value.casefold() in {"/c", "/k"} for value in segment.argv[1:]
    ):
        # Everything after /c or /k belongs to the recursively analyzed CMD
        # payload.  Reinterpreting its slash switches in the outer segment
        # creates false POSIX path escapes (for example ``dir /b``).
        return evidence
    for value in segment.argv[1:]:
        normalized = value.casefold()
        if _is_windows_slash_option(value, executable=executable):
            continue
        if expect_path:
            candidates.append(("path_option", value))
            expect_path = False
            continue
        if normalized in _PATH_OPTIONS:
            expect_path = True
            continue
        if any(normalized.startswith(f"{option}=") for option in _PATH_OPTIONS if option.startswith("--")):
            candidates.append(("path_option", value.split("=", 1)[1]))
        elif _looks_like_path(value):
            candidates.append(("argument", value))
    for source, value in candidates:
        inspected = inspect_path_scope(value, workspace_root)
        if inspected["unc"]:
            evidence.append(
                ShellRiskEvidence(
                    code="filesystem.unc_target",
                    severity=ShellEvidenceSeverity.DENY,
                    category=ShellEvidenceCategory.FILESYSTEM,
                    message="UNC/device path can disclose credentials or cross workspace custody",
                    segment_index=segment.index,
                    metadata={**inspected, "source": source},
                )
            )
        elif inspected["outside_workspace"]:
            evidence.append(
                ShellRiskEvidence(
                    code="filesystem.path_escape",
                    severity=(ShellEvidenceSeverity.DENY if deny_workspace_escape else ShellEvidenceSeverity.REVIEW),
                    category=ShellEvidenceCategory.FILESYSTEM,
                    message="shell argument resolves outside the authorized workspace",
                    segment_index=segment.index,
                    metadata={**inspected, "source": source},
                )
            )
    return evidence


def inspect_path_scope(value: str, workspace_root: str | Path | None) -> dict[str, Any]:
    raw = str(value or "").strip()
    expanded = raw.strip("'\"")
    unc = bool(re.match(r"^(?:\\\\|//|\\\\[?.]\\)", expanded))
    dynamic = any(marker in expanded for marker in ("$", "`", "$(", "${", "%"))
    if not expanded or _looks_like_url(expanded) or expanded in {"-", "/dev/null", "NUL", "nul"}:
        return {
            "path_digest": _digest_text(expanded),
            "workspace_bound": bool(workspace_root),
            "outside_workspace": False,
            "unc": unc,
            "dynamic": dynamic,
        }
    if dynamic:
        return {
            "path_digest": _digest_text(expanded),
            "workspace_bound": bool(workspace_root),
            "outside_workspace": False,
            "unc": unc,
            "dynamic": True,
        }
    if workspace_root is None:
        return {
            "path_digest": _digest_text(expanded),
            "workspace_bound": False,
            "outside_workspace": False,
            "unc": unc,
            "dynamic": False,
        }
    root = Path(workspace_root).resolve()
    windows_absolute = bool(ntpath.splitdrive(expanded)[0])
    candidate = Path(expanded)
    try:
        resolved = candidate.resolve() if candidate.is_absolute() or windows_absolute else (root / candidate).resolve()
        resolved.relative_to(root)
        outside = False
    except (OSError, RuntimeError, ValueError):
        outside = True
        resolved = candidate
    return {
        "path_digest": _digest_text(str(resolved)),
        "workspace_digest": _digest_text(str(root)),
        "workspace_bound": True,
        "outside_workspace": outside,
        "unc": unc,
        "dynamic": False,
    }


def inspect_secret_egress(
    arguments: Mapping[str, Any],
    *,
    tool_name: str,
    server_name: str = "",
    command: str = "",
) -> SecretEgressEvidence:
    secret_keys: set[str] = set()
    string_values: list[str] = []
    _collect_argument_signals(arguments, secret_keys=secret_keys, strings=string_values)
    if command:
        string_values.append(command)
    secret_references = sum(len(_SECRET_REFERENCE.findall(value)) for value in string_values)
    domains = sorted(_domains_from_values(string_values))
    normalized_tool = _normalize_executable(tool_name)
    remote = bool(server_name) or normalized_tool in _REMOTE_TOOL_NAMES
    command_egress = any(
        re.search(rf"(?:^|[;&|\s]){re.escape(name)}(?:$|\s)", command, flags=re.IGNORECASE)
        for name in _NETWORK_COMMANDS
    )
    explicit_external = any(
        key.casefold() in {"external_egress", "external_destination", "upload", "publish"}
        and bool(value)
        for key, value in arguments.items()
    )
    egress = remote or bool(domains) or command_egress or explicit_external
    secret = bool(secret_keys or secret_references or arguments.get("contains_secrets") is True)
    payload = {
        "secret_key_names": sorted(secret_keys),
        "secret_reference_count": secret_references,
        "external_destination_count": len(domains),
        "destination_domains": domains,
        "tool_is_remote": remote,
        "egress_detected": egress,
        "secret_detected": secret,
    }
    return SecretEgressEvidence(
        secret_key_names=tuple(payload["secret_key_names"]),
        secret_reference_count=secret_references,
        external_destination_count=len(domains),
        destination_domains=tuple(domains),
        tool_is_remote=remote,
        egress_detected=egress,
        secret_detected=secret,
        hard_denied=bool(egress and secret),
        evidence_digest=_stable_digest(payload),
    )


def infer_shell_dialect(
    tool_name: str,
    command: str,
    arguments: Mapping[str, Any] | None = None,
) -> ShellDialect:
    selected = str((arguments or {}).get("shell") or (arguments or {}).get("dialect") or "").casefold()
    name = _normalize_executable(tool_name)
    prefix = str(command or "").lstrip().casefold()
    if selected in {"powershell", "pwsh", "ps1"} or name in {"powershell", "pwsh"}:
        return ShellDialect.POWERSHELL
    if selected in {"bash", "sh", "zsh", "posix"} or name in {"bash", "sh", "zsh"}:
        return ShellDialect.BASH
    if re.match(r"^(?:powershell|pwsh)(?:\.exe)?(?:\s|$)", prefix):
        return ShellDialect.POWERSHELL
    if any(marker in command for marker in ("$env:", "Invoke-", "Get-", "Set-", "Remove-Item", "Start-Process")):
        return ShellDialect.POWERSHELL
    return ShellDialect.BASH


def is_shell_tool(tool_name: str, arguments: Mapping[str, Any] | None = None) -> bool:
    name = _normalize_executable(tool_name)
    if name in _SHELL_TOOL_NAMES:
        return True
    values = arguments or {}
    return bool(_first_command(values)) and name in {"execute", "terminal", "command"}


def _nested_shell_payload(argv: Sequence[str], dialect: ShellDialect) -> _NestedPayload:
    executable = _normalize_executable(argv[0] if argv else "")
    flags = {"-c", "--command"}
    encoded_flags = {"-encodedcommand", "-enc", "-e"}
    if dialect is ShellDialect.POWERSHELL:
        flags.update({"-command", "-commandwithargs"})
    for index, value in enumerate(argv[1:], start=1):
        normalized = value.casefold()
        if normalized in encoded_flags:
            return _NestedPayload(encoded=True)
        if normalized in flags and index + 1 < len(argv):
            if executable in {"powershell", "pwsh"}:
                return _NestedPayload(value=" ".join(argv[index + 1 :]))
            return _NestedPayload(value=argv[index + 1])
        if normalized.startswith("-command:"):
            return _NestedPayload(value=value.split(":", 1)[1])
    if executable in {"cmd", "cmd.exe"}:
        for index, value in enumerate(argv[1:], start=1):
            if value.casefold() in {"/c", "/k"} and index + 1 < len(argv):
                return _NestedPayload(value=" ".join(argv[index + 1 :]))
    return _NestedPayload()


def _collect_argument_signals(
    value: Any,
    *,
    secret_keys: set[str],
    strings: list[str],
    key: str = "",
) -> None:
    if key and any(fragment in key.casefold() for fragment in _SECRET_KEY_FRAGMENTS):
        secret_keys.add(key)
        # Do not traverse or retain a secret value.
        return
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _collect_argument_signals(child, secret_keys=secret_keys, strings=strings, key=str(child_key))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for child in value:
            _collect_argument_signals(child, secret_keys=secret_keys, strings=strings, key=key)
    elif isinstance(value, str):
        strings.append(value)


def _domains_from_values(values: Iterable[str]) -> set[str]:
    domains: set[str] = set()
    for raw in values:
        for match in _URL_PATTERN.finditer(str(raw)):
            parsed = urlparse(match.group(0))
            if parsed.hostname:
                domains.add(parsed.hostname.casefold().rstrip("."))
        remote_match = re.match(r"^(?:[^@\s]+@)?([A-Za-z0-9.-]+):[^/\\]", str(raw))
        if remote_match and "." in remote_match.group(1):
            domains.add(remote_match.group(1).casefold().rstrip("."))
    return domains


def _first_command(arguments: Mapping[str, Any]) -> str:
    for key in ("command", "cmd", "script", "code", "shell_command"):
        value = arguments.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _normalize_executable(value: str) -> str:
    text = str(value or "").strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return text[:-4] if text.endswith(".exe") else text


def _dialect(value: ShellDialect | str) -> ShellDialect:
    if isinstance(value, ShellDialect):
        return value
    normalized = str(value).casefold()
    if normalized in {"powershell", "pwsh", "ps1"}:
        return ShellDialect.POWERSHELL
    if normalized in {"bash", "sh", "zsh", "posix"}:
        return ShellDialect.BASH
    return ShellDialect.UNKNOWN


def _looks_like_url(value: str) -> bool:
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value))


def _looks_like_path(value: str) -> bool:
    text = str(value or "")
    if not text or _looks_like_url(text) or text.startswith("-"):
        return False
    return (
        text.startswith((".", "~", "/", "\\"))
        or bool(ntpath.splitdrive(text)[0])
        or "/" in text
        or "\\" in text
    )


def _is_windows_slash_option(value: str, *, executable: str) -> bool:
    """Distinguish command switches such as ``cmd.exe /c`` from POSIX paths."""

    if executable not in _WINDOWS_SLASH_OPTION_COMMANDS:
        return False
    if executable not in {"cmd", "cmd.exe"}:
        return bool(
            re.fullmatch(
                r"/[A-Za-z?][A-Za-z0-9?]*(?::[^/\\]+)?",
                str(value or "").strip(),
            )
        )
    return bool(
        re.fullmatch(
            r"/(?:c|k|d|s|q|a|u|e(?::(?:on|off))?|f(?::(?:on|off))?|v(?::(?:on|off))?)",
            str(value or "").strip(),
            flags=re.IGNORECASE,
        )
    )


def _deduplicate_evidence(values: Iterable[ShellRiskEvidence]) -> list[ShellRiskEvidence]:
    output: list[ShellRiskEvidence] = []
    seen: set[tuple[str, int | None, str]] = set()
    for item in values:
        key = (item.code, item.segment_index, _stable_digest(item.metadata))
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return sorted(
        output,
        key=lambda item: (
            -_SEVERITY_RANK[item.severity],
            item.segment_index if item.segment_index is not None else -1,
            item.code,
        ),
    )


def _safe_metadata(value: Mapping[str, Any]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold()
        if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS) and normalized not in {
            "secret_key_names",
            "secret_reference_count",
        }:
            output[str(key)] = "[REDACTED]"
        elif isinstance(item, Mapping):
            output[str(key)] = _safe_metadata(item)
        elif isinstance(item, (list, tuple)):
            output[str(key)] = [
                _safe_metadata(child) if isinstance(child, Mapping) else to_jsonable(child)
                for child in item
            ]
        else:
            output[str(key)] = to_jsonable(item)
    return output


def _stable_digest(value: Any) -> str:
    encoded = json.dumps(
        to_jsonable(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _digest_text(value: str) -> str:
    return f"sha256:{sha256(str(value).encode('utf-8')).hexdigest()}"


_SHELL_TOOL_NAMES = {
    "bash",
    "sh",
    "zsh",
    "shell",
    "shell_command",
    "powershell",
    "pwsh",
    "terminal",
    "exec",
}
_NESTED_SHELLS = {
    "bash": ShellDialect.BASH,
    "sh": ShellDialect.BASH,
    "zsh": ShellDialect.BASH,
    "dash": ShellDialect.BASH,
    "cmd": ShellDialect.POWERSHELL,
    "powershell": ShellDialect.POWERSHELL,
    "pwsh": ShellDialect.POWERSHELL,
}
_GENERAL_INTERPRETERS = {"python", "python3", "node", "deno", "bun", "ruby", "perl", "php"}
_DYNAMIC_EVAL_COMMANDS = {"eval", "source", ".", "invoke-expression", "iex", "add-type"}
_PROCESS_LAUNCHERS = {"start-process", "nohup", "setsid", "xargs", "parallel", "invoke-command"}
_WINDOWS_SLASH_OPTION_COMMANDS = {
    "attrib",
    "cmd",
    "cmd.exe",
    "copy",
    "del",
    "dir",
    "erase",
    "findstr",
    "icacls",
    "move",
    "robocopy",
    "takeown",
    "taskkill",
    "type",
    "xcopy",
}
_NETWORK_COMMANDS = {
    "curl",
    "wget",
    "invoke-webrequest",
    "invoke-restmethod",
    "iwr",
    "irm",
    "ssh",
    "scp",
    "sftp",
    "ftp",
    "nc",
    "netcat",
    "telnet",
    "git",
    "rsync",
    "azcopy",
    "rclone",
}
_UPLOAD_COMMANDS = {"scp", "sftp", "ftp", "rsync", "azcopy", "rclone"}
_REMOTE_TOOL_NAMES = {
    "http",
    "http_request",
    "network",
    "web_request",
    "browser",
    "browser_action",
    "send_email",
    "send_message",
    "publish",
    "upload",
    "mcp",
    "mcp_tool",
}
_LOCAL_READ_COMMANDS = {
    "cat",
    "head",
    "tail",
    "grep",
    "rg",
    "find",
    "ls",
    "dir",
    "pwd",
    "get-content",
    "select-string",
    "get-childitem",
    "get-item",
    "test-path",
}
_PATH_OPTIONS = {
    "-o",
    "--output",
    "--output-document",
    "--upload-file",
    "--config",
    "--data-binary",
    "-path",
    "-literalpath",
    "-filepath",
    "-destination",
    "-outfile",
    "-workingdirectory",
}
_SECRET_KEY_FRAGMENTS = {
    "secret",
    "token",
    "password",
    "credential",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "private_key",
}
_SECRET_REFERENCE = re.compile(
    r"(?:\$(?:env:)?(?:[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY)[A-Za-z0-9_]*)|"
    r"%(?:[A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|CREDENTIAL|API_KEY)[A-Za-z0-9_]*)%|"
    r"(?:authorization|x-api-key)\s*:\s*\S+)",
    flags=re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"(?:https?|ftp|ssh|s3)://[^\s'\"<>]+", flags=re.IGNORECASE)


__all__ = [
    "SHELL_ANALYZER_ID",
    "SHELL_ANALYZER_VERSION",
    "SecretEgressEvidence",
    "ShellAnalysisResult",
    "ShellAnalyzerConfig",
    "ShellCommandAnalyzer",
    "ShellCommandSegment",
    "ShellDialect",
    "ShellEvidenceCategory",
    "ShellEvidenceSeverity",
    "ShellPipeline",
    "ShellRedirection",
    "ShellRiskEvidence",
    "ShellToken",
    "ShellTokenKind",
    "SourceSpan",
    "infer_shell_dialect",
    "inspect_path_scope",
    "inspect_secret_egress",
    "is_shell_tool",
]
