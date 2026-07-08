from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import new_id, now_iso, to_jsonable


class QueryInputKind(StrEnum):
    TEXT = "text"
    SLASH_COMMAND = "slash_command"
    BASH = "bash"
    STRUCTURED_TURN = "structured_turn"
    SYSTEM = "system"
    EMPTY = "empty"


class QueryInputDisposition(StrEnum):
    ACCEPT = "accept"
    ROUTE_CONTROL_COMMAND = "route_control_command"
    ROUTE_SHELL_TOOL = "route_shell_tool"
    ROUTE_STRUCTURED_PLAN = "route_structured_plan"
    REJECT = "reject"


class QueryInputRisk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKING = "blocking"


class SlashCommandNamespace(StrEnum):
    SESSION = "session"
    MEMORY = "memory"
    PERMISSION = "permission"
    RUNTIME = "runtime"
    TASK = "task"
    UNKNOWN = "unknown"


class BashInputMode(StrEnum):
    INLINE = "inline"
    LOGIN_SHELL = "login_shell"
    MULTILINE = "multiline"
    BACKGROUND = "background"


class QuerySourceKind(StrEnum):
    CLAUDE_QUERY_ENGINE = "claude_query_engine"
    CLAUDE_QUERY_CONTEXT = "claude_query_context"
    CLAUDE_PROCESS_USER_INPUT = "claude_process_user_input"
    CLAUDE_SESSION_STORAGE = "claude_session_storage"
    ZYRA_WORKER_REQUEST = "zyra_worker_request"
    ZYRA_CONTROL_COMMAND = "zyra_control_command"


@dataclass(frozen=True, slots=True)
class QuerySourceMetadata:
    source_repo: str
    source_path: str
    target_path: str
    source_kind: QuerySourceKind
    owner_unit: str = "M1-02B"
    source_graph_batch: str = "batch-01-query-session-context"
    source_graph_document: str = "source-graphs/claude-code-best/batch-01-query-session-context.md"
    decision: str = "zyra_module_migrated"
    runtime_owner: str = "zyra-claude-productized"
    upstream_signals: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_repo": self.source_repo,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "source_kind": str(self.source_kind),
            "owner_unit": self.owner_unit,
            "source_graph_batch": self.source_graph_batch,
            "source_graph_document": self.source_graph_document,
            "decision": self.decision,
            "runtime_owner": self.runtime_owner,
            "upstream_signals": list(self.upstream_signals),
            "notes": list(self.notes),
        }

    def metadata(self, prefix: str) -> dict[str, str]:
        return {
            f"{prefix}_source_repo": self.source_repo,
            f"{prefix}_source_path": self.source_path,
            f"{prefix}_target_path": self.target_path,
            f"{prefix}_source_kind": str(self.source_kind),
            f"{prefix}_owner_unit": self.owner_unit,
            f"{prefix}_decision": self.decision,
        }


@dataclass(frozen=True, slots=True)
class QueryInputSpan:
    start: int
    end: int
    label: str
    text: str
    redacted: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def length(self) -> int:
        return max(0, self.end - self.start)

    def to_dict(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "label": self.label,
            "text": self.text,
            "redacted": self.redacted,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SlashCommandSpec:
    name: str
    namespace: SlashCommandNamespace
    routes_to_control_command: bool
    clears_context: bool = False
    requires_session: bool = True
    affects_context_snapshot: bool = False
    allowed_in_worker: bool = True
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "namespace": str(self.namespace),
            "routes_to_control_command": self.routes_to_control_command,
            "clears_context": self.clears_context,
            "requires_session": self.requires_session,
            "affects_context_snapshot": self.affects_context_snapshot,
            "allowed_in_worker": self.allowed_in_worker,
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class BashInputSpec:
    command: str
    mode: BashInputMode
    requires_approval: bool
    read_only_hint: bool
    background: bool = False
    working_directory: str = ""
    timeout_seconds: int = 30
    tokens: tuple[str, ...] = ()
    risk: QueryInputRisk = QueryInputRisk.MEDIUM
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "mode": str(self.mode),
            "requires_approval": self.requires_approval,
            "read_only_hint": self.read_only_hint,
            "background": self.background,
            "working_directory": self.working_directory,
            "timeout_seconds": self.timeout_seconds,
            "tokens": list(self.tokens),
            "risk": str(self.risk),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class QueryInputAttachment:
    attachment_id: str
    kind: str
    path: str = ""
    artifact_id: str = ""
    content_type: str = ""
    chars: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "attachment_id": self.attachment_id,
            "kind": self.kind,
            "path": self.path,
            "artifact_id": self.artifact_id,
            "content_type": self.content_type,
            "chars": self.chars,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QueryInputRecord:
    input_id: str
    sequence: int
    kind: QueryInputKind
    disposition: QueryInputDisposition
    raw_text: str
    normalized_text: str
    created_at: str
    source: QuerySourceMetadata
    role: str = "user"
    command_name: str = ""
    command_arguments: str = ""
    slash_command: SlashCommandSpec | None = None
    bash: BashInputSpec | None = None
    spans: tuple[QueryInputSpan, ...] = ()
    attachments: tuple[QueryInputAttachment, ...] = ()
    risk: QueryInputRisk = QueryInputRisk.LOW
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted(self) -> bool:
        return self.disposition != QueryInputDisposition.REJECT and not self.blockers

    @property
    def chars(self) -> int:
        return len(self.normalized_text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "sequence": self.sequence,
            "kind": str(self.kind),
            "disposition": str(self.disposition),
            "raw_text": self.raw_text,
            "normalized_text": self.normalized_text,
            "created_at": self.created_at,
            "source": self.source.to_dict(),
            "role": self.role,
            "command_name": self.command_name,
            "command_arguments": self.command_arguments,
            "slash_command": self.slash_command.to_dict() if self.slash_command else None,
            "bash": self.bash.to_dict() if self.bash else None,
            "spans": [span.to_dict() for span in self.spans],
            "attachments": [attachment.to_dict() for attachment in self.attachments],
            "risk": str(self.risk),
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "accepted": self.accepted,
            "chars": self.chars,
            "metadata": to_jsonable(self.metadata),
        }

    def to_context_message(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.normalized_text,
            "metadata": {
                "input_id": self.input_id,
                "kind": str(self.kind),
                "disposition": str(self.disposition),
                "command_name": self.command_name,
                "risk": str(self.risk),
                **self.source.metadata("input_source"),
            },
        }


@dataclass(frozen=True, slots=True)
class QueryInputProcessingReport:
    ok: bool
    records: tuple[QueryInputRecord, ...]
    source_metadata: QuerySourceMetadata
    created_at: str = field(default_factory=now_iso)
    warnings: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def accepted_records(self) -> tuple[QueryInputRecord, ...]:
        return tuple(record for record in self.records if record.accepted)

    @property
    def rejected_records(self) -> tuple[QueryInputRecord, ...]:
        return tuple(record for record in self.records if not record.accepted)

    @property
    def kind_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[str(record.kind)] = counts.get(str(record.kind), 0) + 1
        return counts

    @property
    def disposition_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.records:
            counts[str(record.disposition)] = counts.get(str(record.disposition), 0) + 1
        return counts

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_input_processor_ok": str(self.ok).lower(),
            "query_input_count": str(len(self.records)),
            "query_input_accepted_count": str(len(self.accepted_records)),
            "query_input_rejected_count": str(len(self.rejected_records)),
            "query_input_text_count": str(self.kind_counts.get(str(QueryInputKind.TEXT), 0)),
            "query_input_slash_count": str(self.kind_counts.get(str(QueryInputKind.SLASH_COMMAND), 0)),
            "query_input_bash_count": str(self.kind_counts.get(str(QueryInputKind.BASH), 0)),
            "query_input_structured_turn_count": str(self.kind_counts.get(str(QueryInputKind.STRUCTURED_TURN), 0)),
            "query_input_blocker_count": str(len(self.blockers)),
            "query_input_warning_count": str(len(self.warnings)),
            **self.source_metadata.metadata("query_input_processor"),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "records": [record.to_dict() for record in self.records],
            "source_metadata": self.source_metadata.to_dict(),
            "created_at": self.created_at,
            "warnings": list(self.warnings),
            "blockers": list(self.blockers),
            "kind_counts": self.kind_counts,
            "disposition_counts": self.disposition_counts,
            "metadata": to_jsonable(self.metadata),
        }


class QueryInputProcessor:
    """Classifies worker/user input before a QueryEngine turn is accepted.

    Claude Code routes text, slash commands, shell-prefixed input and structured
    turns before entering the model/tool loop. Zyra keeps that boundary as a
    deterministic processor so context assembly, transcript storage and later
    permission runtimes all observe the same accepted input record.
    """

    def __init__(
        self,
        *,
        source_metadata: QuerySourceMetadata | None = None,
        slash_commands: Mapping[str, SlashCommandSpec] | None = None,
        max_input_chars: int = 64000,
    ) -> None:
        self.source_metadata = source_metadata or default_query_input_source()
        self.slash_commands = dict(slash_commands or default_slash_command_specs())
        self.max_input_chars = max(1, max_input_chars)

    def process_worker_request(self, request: Any, *, disabled: bool = False) -> QueryInputProcessingReport:
        if disabled:
            return QueryInputProcessingReport(
                ok=False,
                records=(),
                source_metadata=self.source_metadata,
                blockers=("query_input_processor_disabled",),
            )
        raw_inputs = list(self._extract_request_inputs(request))
        records: list[QueryInputRecord] = []
        warnings: list[str] = []
        blockers: list[str] = []
        for sequence, item in enumerate(raw_inputs, start=1):
            record = self.process_item(item, sequence=sequence)
            records.append(record)
            warnings.extend(record.warnings)
            blockers.extend(record.blockers)
        if not records:
            records.append(
                self.process_item(
                    {
                        "text": "",
                        "role": "user",
                        "metadata": {"source": "empty_worker_request"},
                    },
                    sequence=1,
                )
            )
        ok = not blockers and any(record.accepted for record in records)
        return QueryInputProcessingReport(
            ok=ok,
            records=tuple(records),
            source_metadata=self.source_metadata,
            warnings=tuple(dict.fromkeys(warnings)),
            blockers=tuple(dict.fromkeys(blockers)),
            metadata={
                "request_id": str(getattr(request, "request_id", "")),
                "worker_name": str(getattr(request, "worker_name", "")),
            },
        )

    def process_item(self, item: Mapping[str, Any] | Any, *, sequence: int) -> QueryInputRecord:
        payload = item if isinstance(item, Mapping) else {"text": str(item), "role": "user"}
        raw_text = _coerce_text(payload.get("text") or payload.get("content") or payload.get("raw") or "")
        role = str(payload.get("role") or "user")
        metadata = dict(_as_mapping(payload.get("metadata")))
        attachments = tuple(_attachments_from_payload(payload))
        normalized, spans, redaction_warnings = normalize_query_text(raw_text, max_chars=self.max_input_chars)
        warnings = list(redaction_warnings)
        blockers: list[str] = []
        kind = classify_query_input(normalized, payload)
        risk = QueryInputRisk.LOW
        disposition = QueryInputDisposition.ACCEPT
        command_name = ""
        command_arguments = ""
        slash_spec: SlashCommandSpec | None = None
        bash_spec: BashInputSpec | None = None
        if len(raw_text) > self.max_input_chars:
            warnings.append("input_truncated_to_max_chars")
        if kind == QueryInputKind.EMPTY:
            disposition = QueryInputDisposition.REJECT
            blockers.append("empty_input")
        elif kind == QueryInputKind.SLASH_COMMAND:
            command_name, command_arguments = split_slash_command(normalized)
            slash_spec = self.slash_commands.get(command_name) or unknown_slash_command_spec(command_name)
            risk = QueryInputRisk.MEDIUM if slash_spec.namespace == SlashCommandNamespace.UNKNOWN else QueryInputRisk.LOW
            if not slash_spec.allowed_in_worker:
                disposition = QueryInputDisposition.REJECT
                blockers.append("slash_command_not_allowed_in_worker")
            elif slash_spec.routes_to_control_command:
                disposition = QueryInputDisposition.ROUTE_CONTROL_COMMAND
            else:
                disposition = QueryInputDisposition.ACCEPT
        elif kind == QueryInputKind.BASH:
            bash_spec = parse_bash_input(normalized, payload)
            command_name = "shell"
            command_arguments = bash_spec.command
            risk = bash_spec.risk
            disposition = QueryInputDisposition.ROUTE_SHELL_TOOL
            if bash_spec.risk == QueryInputRisk.BLOCKING:
                blockers.append("bash_input_blocked_by_static_risk")
                disposition = QueryInputDisposition.REJECT
        elif kind == QueryInputKind.STRUCTURED_TURN:
            disposition = QueryInputDisposition.ROUTE_STRUCTURED_PLAN
            command_name = "query_turns"
        return QueryInputRecord(
            input_id=new_id("qin"),
            sequence=sequence,
            kind=kind,
            disposition=disposition,
            raw_text=raw_text,
            normalized_text=normalized,
            created_at=now_iso(),
            source=self.source_metadata,
            role=role,
            command_name=command_name,
            command_arguments=command_arguments,
            slash_command=slash_spec,
            bash=bash_spec,
            spans=tuple(spans),
            attachments=attachments,
            risk=risk,
            warnings=tuple(dict.fromkeys(warnings)),
            blockers=tuple(dict.fromkeys(blockers)),
            metadata=metadata,
        )

    def _extract_request_inputs(self, request: Any) -> Iterable[Mapping[str, Any]]:
        constraints = _as_mapping(getattr(request, "constraints", {}))
        explicit_inputs = constraints.get("query_inputs")
        if isinstance(explicit_inputs, Sequence) and not isinstance(explicit_inputs, (str, bytes)):
            for item in explicit_inputs:
                if isinstance(item, Mapping):
                    yield item
                else:
                    yield {"text": str(item), "role": "user", "metadata": {"source": "constraints.query_inputs"}}
        session_messages = constraints.get("session_messages")
        if isinstance(session_messages, Sequence) and not isinstance(session_messages, (str, bytes)):
            for index, item in enumerate(session_messages, start=1):
                if isinstance(item, Mapping):
                    payload = dict(item)
                else:
                    payload = {"content": item, "role": "assistant"}
                payload.setdefault("metadata", {})
                if isinstance(payload["metadata"], dict):
                    payload["metadata"].setdefault("source", f"constraints.session_messages[{index}]")
                if payload.get("text") in (None, "") and payload.get("content") not in (None, ""):
                    payload["text"] = _coerce_text(payload.get("content"))
                yield payload
        assistant_tool_uses = constraints.get("assistant_tool_uses")
        if isinstance(assistant_tool_uses, Sequence) and not isinstance(assistant_tool_uses, (str, bytes)):
            yield {
                "text": _coerce_text(list(assistant_tool_uses)),
                "role": "assistant",
                "metadata": {
                    "source": "constraints.assistant_tool_uses",
                    "assistant_tool_use_count": len(assistant_tool_uses),
                },
                "kind": str(QueryInputKind.STRUCTURED_TURN),
            }
        for key in ("raw_input", "input", "prompt", "text", "bash_command", "slash_command"):
            if constraints.get(key) not in (None, ""):
                yield {
                    "text": str(constraints[key]),
                    "role": "user",
                    "metadata": {"source": f"constraints.{key}"},
                }
        messages = getattr(request, "messages", ())
        for index, message in enumerate(messages or ()):
            if isinstance(message, Mapping):
                payload = dict(message)
            else:
                payload = to_jsonable(message)
                if not isinstance(payload, Mapping):
                    payload = {"text": str(message), "role": "user"}
            payload.setdefault("metadata", {})
            if isinstance(payload["metadata"], dict):
                payload["metadata"].setdefault("source", f"request.messages[{index}]")
            yield payload
        query_turns = constraints.get("query_turns")
        if isinstance(query_turns, Sequence) and not isinstance(query_turns, (str, bytes)):
            for index, turn in enumerate(query_turns, start=1):
                prompt = _prompt_from_structured_turn(turn, index=index)
                yield {
                    "text": prompt,
                    "role": "user",
                    "metadata": {
                        "source": "constraints.query_turns",
                        "turn_index": index,
                        "structured_turn": to_jsonable(turn),
                    },
                    "kind": str(QueryInputKind.STRUCTURED_TURN),
                }
        tool_plan = constraints.get("tool_plan")
        if isinstance(tool_plan, Sequence) and not isinstance(tool_plan, (str, bytes)):
            prompt = _prompt_from_structured_turn(tool_plan, index=1)
            yield {
                "text": prompt,
                "role": "user",
                "metadata": {
                    "source": "constraints.tool_plan",
                    "turn_index": 1,
                    "structured_turn": to_jsonable(tool_plan),
                },
                "kind": str(QueryInputKind.STRUCTURED_TURN),
            }


def default_query_input_source() -> QuerySourceMetadata:
    return QuerySourceMetadata(
        source_repo="claude-code-best",
        source_path="src/processUserInput.ts",
        target_path="packages/runtime/zyra_runtime/claude_input_processor.py",
        source_kind=QuerySourceKind.CLAUDE_PROCESS_USER_INPUT,
        upstream_signals=(
            "processUserInput",
            "slash command routing",
            "bash command detection",
            "command-scoped allowed tools",
            "pre-submit hooks",
        ),
        notes=(
            "Input classification runs before QueryEngine session creation.",
            "Zyra stores the accepted record in append-only session metadata.",
        ),
    )


def default_slash_command_specs() -> dict[str, SlashCommandSpec]:
    specs = [
        SlashCommandSpec("/clear", SlashCommandNamespace.SESSION, True, clears_context=True, affects_context_snapshot=True),
        SlashCommandSpec("/compact", SlashCommandNamespace.MEMORY, True, affects_context_snapshot=True),
        SlashCommandSpec("/resume", SlashCommandNamespace.SESSION, True),
        SlashCommandSpec("/rewind", SlashCommandNamespace.SESSION, True, affects_context_snapshot=True),
        SlashCommandSpec("/memory", SlashCommandNamespace.MEMORY, True, affects_context_snapshot=True),
        SlashCommandSpec("/permissions", SlashCommandNamespace.PERMISSION, True),
        SlashCommandSpec("/cost", SlashCommandNamespace.RUNTIME, True),
        SlashCommandSpec("/doctor", SlashCommandNamespace.RUNTIME, True),
        SlashCommandSpec("/context", SlashCommandNamespace.RUNTIME, True, affects_context_snapshot=True),
        SlashCommandSpec("/tasks", SlashCommandNamespace.TASK, True),
        SlashCommandSpec("/bashes", SlashCommandNamespace.RUNTIME, True),
        SlashCommandSpec("/export", SlashCommandNamespace.SESSION, True),
        SlashCommandSpec("/verify", SlashCommandNamespace.TASK, True),
        SlashCommandSpec("/eval", SlashCommandNamespace.TASK, True),
        SlashCommandSpec("/change", SlashCommandNamespace.TASK, True),
        SlashCommandSpec("/inject", SlashCommandNamespace.TASK, True),
    ]
    return {spec.name: spec for spec in specs}


def unknown_slash_command_spec(name: str) -> SlashCommandSpec:
    return SlashCommandSpec(
        name=name if name.startswith("/") else f"/{name}",
        namespace=SlashCommandNamespace.UNKNOWN,
        routes_to_control_command=False,
        allowed_in_worker=False,
        description="Unknown slash command rejected before QueryEngine dispatch.",
    )


def classify_query_input(text: str, payload: Mapping[str, Any] | None = None) -> QueryInputKind:
    payload = payload or {}
    explicit_kind = payload.get("kind")
    if explicit_kind:
        try:
            return QueryInputKind(str(explicit_kind))
        except ValueError:
            pass
    if not text.strip():
        return QueryInputKind.EMPTY
    stripped = text.lstrip()
    if stripped.startswith("/"):
        return QueryInputKind.SLASH_COMMAND
    if stripped.startswith("!") or payload.get("bash") is True or payload.get("shell") is True:
        return QueryInputKind.BASH
    if _looks_like_bash_assignment_or_pipeline(stripped):
        return QueryInputKind.BASH
    if payload.get("structured_turn") is not None:
        return QueryInputKind.STRUCTURED_TURN
    role = str(payload.get("role") or "")
    if role == "system":
        return QueryInputKind.SYSTEM
    return QueryInputKind.TEXT


def normalize_query_text(text: str, *, max_chars: int) -> tuple[str, list[QueryInputSpan], list[str]]:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = normalized.strip("\ufeff")
    warnings: list[str] = []
    spans: list[QueryInputSpan] = []
    if len(normalized) > max_chars:
        normalized = normalized[:max_chars]
        warnings.append("input_truncated_to_max_chars")
    for pattern, label in _SECRET_PATTERNS:
        for match in pattern.finditer(normalized):
            spans.append(
                QueryInputSpan(
                    start=match.start(),
                    end=match.end(),
                    label=label,
                    text=match.group(0)[:12] + "...",
                    redacted=True,
                )
            )
    if spans:
        warnings.append("possible_secret_span_detected")
    return normalized, spans, warnings


def split_slash_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    if not stripped or stripped == "/":
        return "/", ""
    parts = stripped.split(maxsplit=1)
    command = parts[0]
    arguments = parts[1] if len(parts) > 1 else ""
    return command, arguments


def parse_bash_input(text: str, payload: Mapping[str, Any] | None = None) -> BashInputSpec:
    payload = payload or {}
    stripped = text.strip()
    mode = BashInputMode.INLINE
    if stripped.startswith("!"):
        stripped = stripped[1:].lstrip()
    if "\n" in stripped:
        mode = BashInputMode.MULTILINE
    if payload.get("login_shell") is True:
        mode = BashInputMode.LOGIN_SHELL
    if stripped.endswith("&") or payload.get("background") is True:
        mode = BashInputMode.BACKGROUND
    tokens = _safe_shlex_split(stripped)
    read_only_hint = _bash_tokens_read_only(tokens)
    reasons: list[str] = []
    risk = QueryInputRisk.LOW if read_only_hint else QueryInputRisk.MEDIUM
    if _contains_destructive_shell_token(tokens):
        risk = QueryInputRisk.HIGH
        reasons.append("destructive_shell_token")
    if _contains_forbidden_shell_pattern(stripped):
        risk = QueryInputRisk.BLOCKING
        reasons.append("forbidden_shell_pattern")
    requires_approval = not read_only_hint or risk in {QueryInputRisk.HIGH, QueryInputRisk.BLOCKING}
    return BashInputSpec(
        command=stripped,
        mode=mode,
        requires_approval=requires_approval,
        read_only_hint=read_only_hint,
        background=mode == BashInputMode.BACKGROUND,
        working_directory=str(payload.get("cwd") or payload.get("working_directory") or ""),
        timeout_seconds=_bounded_int(payload.get("timeout_seconds"), default=30, minimum=1, maximum=600),
        tokens=tuple(tokens),
        risk=risk,
        reasons=tuple(reasons),
    )


def input_records_to_messages(records: Sequence[QueryInputRecord]) -> list[dict[str, Any]]:
    return [record.to_context_message() for record in records if record.accepted]


def input_records_to_metadata(records: Sequence[QueryInputRecord]) -> dict[str, str]:
    counts: dict[str, int] = {}
    dispositions: dict[str, int] = {}
    for record in records:
        counts[str(record.kind)] = counts.get(str(record.kind), 0) + 1
        dispositions[str(record.disposition)] = dispositions.get(str(record.disposition), 0) + 1
    return {
        "query_input_count": str(len(records)),
        "query_input_text_count": str(counts.get(str(QueryInputKind.TEXT), 0)),
        "query_input_slash_count": str(counts.get(str(QueryInputKind.SLASH_COMMAND), 0)),
        "query_input_bash_count": str(counts.get(str(QueryInputKind.BASH), 0)),
        "query_input_structured_turn_count": str(counts.get(str(QueryInputKind.STRUCTURED_TURN), 0)),
        "query_input_control_route_count": str(dispositions.get(str(QueryInputDisposition.ROUTE_CONTROL_COMMAND), 0)),
        "query_input_shell_route_count": str(dispositions.get(str(QueryInputDisposition.ROUTE_SHELL_TOOL), 0)),
        "query_input_rejected_count": str(dispositions.get(str(QueryInputDisposition.REJECT), 0)),
    }


def render_query_input_report_markdown(report: QueryInputProcessingReport) -> str:
    lines = [
        "# Query Input Processing",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- input_count: `{len(report.records)}`",
        f"- accepted_count: `{len(report.accepted_records)}`",
        f"- rejected_count: `{len(report.rejected_records)}`",
        f"- source: `{report.source_metadata.source_path}`",
        f"- target: `{report.source_metadata.target_path}`",
        "",
        "## Inputs",
        "",
    ]
    for record in report.records:
        lines.extend(
            [
                f"- `{record.sequence}` `{record.kind}` `{record.disposition}` `{record.risk}`",
                f"  - id: `{record.input_id}`",
                f"  - command: `{record.command_name}`",
                f"  - chars: `{record.chars}`",
            ]
        )
        if record.warnings:
            lines.append("  - warnings: `" + ", ".join(record.warnings) + "`")
        if record.blockers:
            lines.append("  - blockers: `" + ", ".join(record.blockers) + "`")
    return "\n".join(lines) + "\n"


def _prompt_from_structured_turn(turn: Any, *, index: int) -> str:
    if isinstance(turn, Sequence) and not isinstance(turn, (str, bytes)):
        prompts = []
        tools = []
        for step in turn:
            if isinstance(step, Mapping):
                prompt = step.get("prompt") or step.get("user_message")
                if prompt:
                    prompts.append(str(prompt))
                tools.append(str(step.get("tool_name") or step.get("tool") or "unknown_tool"))
        if prompts:
            return "\n".join(prompts)
        return f"Structured CodeWorker turn {index}: execute {', '.join(tools) or 'no tools'}."
    return f"Structured CodeWorker turn {index}: {json.dumps(to_jsonable(turn), ensure_ascii=False, sort_keys=True)}"


def _attachments_from_payload(payload: Mapping[str, Any]) -> Iterable[QueryInputAttachment]:
    attachments = payload.get("attachments")
    if not isinstance(attachments, Sequence) or isinstance(attachments, (str, bytes)):
        return []
    parsed: list[QueryInputAttachment] = []
    for item in attachments:
        if not isinstance(item, Mapping):
            continue
        path = str(item.get("path") or "")
        artifact_id = str(item.get("artifact_id") or "")
        parsed.append(
            QueryInputAttachment(
                attachment_id=str(item.get("attachment_id") or new_id("qattach")),
                kind=str(item.get("kind") or ("artifact" if artifact_id else "file")),
                path=path,
                artifact_id=artifact_id,
                content_type=str(item.get("content_type") or ""),
                chars=_bounded_int(item.get("chars"), default=0, minimum=0, maximum=10_000_000),
                metadata=dict(_as_mapping(item.get("metadata"))),
            )
        )
    return parsed


def _coerce_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return "\n".join(_coerce_text(item) for item in value)
    if isinstance(value, Mapping):
        return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
    return str(value)


def _looks_like_bash_assignment_or_pipeline(text: str) -> bool:
    if "\n" in text and any(line.strip().startswith(("cd ", "git ", "python ", "npm ", "pytest ")) for line in text.splitlines()):
        return True
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=.*\s+[\w./-]+", text):
        return True
    if any(token in text for token in (" | ", " && ", " || ", " > ", " 2>", "1>")):
        first = text.split()[0] if text.split() else ""
        return first in _SHELL_COMMAND_HINTS or Path(first).suffix in {".ps1", ".bat", ".cmd", ".sh"}
    first = text.split()[0] if text.split() else ""
    return first in _SHELL_COMMAND_HINTS and len(text.split()) > 1


def _safe_shlex_split(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=False)
    except ValueError:
        return command.split()


def _bash_tokens_read_only(tokens: Sequence[str]) -> bool:
    if not tokens:
        return False
    first = tokens[0].lower()
    first = first.removeprefix("!")
    return first in _READ_ONLY_SHELL_COMMANDS


def _contains_destructive_shell_token(tokens: Sequence[str]) -> bool:
    lowered = {token.lower() for token in tokens}
    destructive = {"rm", "del", "erase", "rmdir", "remove-item", "git", "reset", "clean", "checkout"}
    if lowered & {"rm", "del", "erase", "rmdir", "remove-item"}:
        return True
    if "git" in lowered and ({"reset", "clean", "checkout"} & lowered):
        return True
    return bool(destructive & lowered and ("-rf" in lowered or "/s" in lowered))


def _contains_forbidden_shell_pattern(command: str) -> bool:
    lowered = command.lower()
    forbidden = [
        "rm -rf /",
        "format ",
        "mkfs",
        "diskpart",
        "shutdown ",
        "restart-computer",
        "stop-computer",
    ]
    return any(pattern in lowered for pattern in forbidden)


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


_SECRET_PATTERNS = (
    (re.compile(r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*['\"]?[^'\"\s]+"), "possible_secret_assignment"),
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "possible_api_key"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._-]{20,}"), "possible_bearer_token"),
)

_READ_ONLY_SHELL_COMMANDS = {
    "cat",
    "cd",
    "dir",
    "echo",
    "find",
    "git",
    "grep",
    "ls",
    "pwd",
    "rg",
    "select-string",
    "type",
    "where",
    "whoami",
    "get-childitem",
    "get-content",
    "get-location",
    "get-process",
}

_SHELL_COMMAND_HINTS = _READ_ONLY_SHELL_COMMANDS | {
    "python",
    "python3",
    "node",
    "npm",
    "pnpm",
    "yarn",
    "pytest",
    "uv",
    "pip",
    "pwsh",
    "powershell",
    "cmd",
    "bash",
    "sh",
    "mkdir",
    "cp",
    "copy",
    "mv",
    "move",
}
