from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID


class ToolSessionBridgeOrigin(StrEnum):
    REQUEST_MESSAGES = "request_messages"
    CONSTRAINT_SESSION_MESSAGES = "constraint_session_messages"
    CONSTRAINT_ASSISTANT_TOOL_USES = "constraint_assistant_tool_uses"
    CONSTRAINT_OPENCODE_PARTS = "constraint_opencode_parts"
    FALLBACK_STRUCTURED_PLAN = "fallback_structured_plan"
    EMPTY = "empty"


class ToolSessionBridgeFormat(StrEnum):
    CLAUDE_TOOL_USE_BLOCK = "claude_tool_use_block"
    OPENCODE_TOOL_CALL_PART = "opencode_tool_call_part"
    OPENAI_TOOL_CALL = "openai_tool_call"
    HERMES_PERMISSION_TOOL_USE = "hermes_permission_tool_use"
    STRUCTURED_TOOL_USE = "structured_tool_use"
    UNKNOWN = "unknown"


class ToolSessionBridgeFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolSessionBridgeSurface(StrEnum):
    REQUEST_MESSAGE = "request_message"
    CONSTRAINT = "constraint"
    TOOL_USE = "tool_use"
    FALLBACK = "fallback"
    EVENT = "event"


@dataclass(frozen=True, slots=True)
class ToolSessionBridgeFinding:
    code: str
    severity: ToolSessionBridgeFindingSeverity
    surface: ToolSessionBridgeSurface
    message: str
    origin: str = ""
    message_index: int = 0
    block_index: int = 0
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolSessionBridgeFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "origin": self.origin,
            "message_index": self.message_index,
            "block_index": self.block_index,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSessionToolUse:
    bridge_tool_use_id: str
    tool_name: str
    arguments: dict[str, Any]
    origin: ToolSessionBridgeOrigin
    source_format: ToolSessionBridgeFormat
    message_index: int
    block_index: int
    turn_index: int
    raw_role: str = "assistant"
    source_message_id: str = ""
    upstream_tool_use_id: str = ""
    prompt: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return bool(self.tool_name)

    @property
    def opencode_sourced(self) -> bool:
        return self.source_format == ToolSessionBridgeFormat.OPENCODE_TOOL_CALL_PART

    @property
    def hermes_sourced(self) -> bool:
        return self.source_format == ToolSessionBridgeFormat.HERMES_PERMISSION_TOOL_USE

    def to_tool_step(self) -> dict[str, Any]:
        stable_tool_call_id = self.upstream_tool_use_id or self.bridge_tool_use_id
        metadata = {
            "tool_session_bridge_tool_use_id": self.bridge_tool_use_id,
            "tool_session_bridge_origin": str(self.origin),
            "tool_session_bridge_format": str(self.source_format),
            "assistant_tool_use_id": stable_tool_call_id,
            "source_message_id": self.source_message_id,
            "message_index": str(self.message_index),
            "block_index": str(self.block_index),
            "turn_index": str(self.turn_index),
            **dict(self.metadata),
        }
        payload = {
            "tool_name": self.tool_name,
            "arguments": dict(self.arguments),
            "metadata": metadata,
        }
        payload["tool_call_id"] = stable_tool_call_id
        if self.prompt:
            payload["prompt"] = self.prompt
        return payload

    def to_dict(self) -> dict[str, Any]:
        return {
            "bridge_tool_use_id": self.bridge_tool_use_id,
            "tool_name": self.tool_name,
            "arguments": to_jsonable(self.arguments),
            "origin": str(self.origin),
            "source_format": str(self.source_format),
            "message_index": self.message_index,
            "block_index": self.block_index,
            "turn_index": self.turn_index,
            "raw_role": self.raw_role,
            "source_message_id": self.source_message_id,
            "upstream_tool_use_id": self.upstream_tool_use_id,
            "prompt": self.prompt,
            "valid": self.valid,
            "opencode_sourced": self.opencode_sourced,
            "hermes_sourced": self.hermes_sourced,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSessionBridgeTurn:
    turn_index: int
    origin: ToolSessionBridgeOrigin
    tool_uses: tuple[ToolSessionToolUse, ...]
    prompt: str = ""
    source_message_indices: tuple[int, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def valid_tool_uses(self) -> tuple[ToolSessionToolUse, ...]:
        return tuple(item for item in self.tool_uses if item.valid)

    def to_tool_turn(self) -> list[dict[str, Any]]:
        return [item.to_tool_step() for item in self.valid_tool_uses]

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "origin": str(self.origin),
            "prompt": self.prompt,
            "source_message_indices": list(self.source_message_indices),
            "tool_use_count": len(self.tool_uses),
            "valid_tool_use_count": len(self.valid_tool_uses),
            "tool_uses": [item.to_dict() for item in self.tool_uses],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolSessionBridgeReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    origin: ToolSessionBridgeOrigin
    turns: tuple[ToolSessionBridgeTurn, ...]
    fallback_turn_count: int
    fallback_used: bool
    findings: tuple[ToolSessionBridgeFinding, ...] = ()
    created_at: str = field(default_factory=now_iso)
    metadata_values: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    @property
    def tool_use_count(self) -> int:
        return sum(len(turn.tool_uses) for turn in self.turns)

    @property
    def valid_tool_use_count(self) -> int:
        return sum(len(turn.valid_tool_uses) for turn in self.turns)

    @property
    def generated_turn_count(self) -> int:
        return len([turn for turn in self.turns if turn.valid_tool_uses])

    @property
    def opencode_tool_use_count(self) -> int:
        return sum(1 for turn in self.turns for item in turn.tool_uses if item.opencode_sourced)

    @property
    def hermes_tool_use_count(self) -> int:
        return sum(1 for turn in self.turns for item in turn.tool_uses if item.hermes_sourced)

    @property
    def assistant_message_tool_use_count(self) -> int:
        return sum(
            1
            for turn in self.turns
            for item in turn.tool_uses
            if item.origin
            in {
                ToolSessionBridgeOrigin.REQUEST_MESSAGES,
                ToolSessionBridgeOrigin.CONSTRAINT_SESSION_MESSAGES,
            }
        )

    def to_tool_turns(self) -> list[list[dict[str, Any]]]:
        return [turn.to_tool_turn() for turn in self.turns if turn.to_tool_turn()]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_session_bridge.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "origin": str(self.origin),
            "ok": self.ok,
            "tool_use_count": self.tool_use_count,
            "valid_tool_use_count": self.valid_tool_use_count,
            "generated_turn_count": self.generated_turn_count,
            "fallback_turn_count": self.fallback_turn_count,
            "fallback_used": self.fallback_used,
            "opencode_tool_use_count": self.opencode_tool_use_count,
            "hermes_tool_use_count": self.hermes_tool_use_count,
            "assistant_message_tool_use_count": self.assistant_message_tool_use_count,
            "turns": [turn.to_dict() for turn in self.turns],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
            "metadata": dict(self.metadata_values),
        }

    def metadata(self) -> dict[str, str]:
        metadata = {
            "tool_session_bridge_report_id": self.report_id,
            "tool_session_bridge_owner_unit": self.owner_unit,
            "tool_session_bridge_runtime_id": self.runtime_id,
            "tool_session_bridge_ok": str(self.ok).lower(),
            "tool_session_bridge_origin": str(self.origin),
            "tool_session_bridge_tool_uses": str(self.tool_use_count),
            "tool_session_bridge_valid_tool_uses": str(self.valid_tool_use_count),
            "tool_session_bridge_turns": str(self.generated_turn_count),
            "tool_session_bridge_fallback_turns": str(self.fallback_turn_count),
            "tool_session_bridge_fallback_used": str(self.fallback_used).lower(),
            "tool_session_bridge_findings": str(len(self.findings)),
            "tool_session_bridge_assistant_message_tool_uses": str(self.assistant_message_tool_use_count),
            "tool_session_bridge_opencode_tool_uses": str(self.opencode_tool_use_count),
            "tool_session_bridge_hermes_tool_uses": str(self.hermes_tool_use_count),
        }
        metadata.update({str(k): str(v) for k, v in self.metadata_values.items()})
        return metadata


class ToolSessionBridgeRuntime:
    """Turns persisted assistant tool_use blocks into executable tool loop turns.

    The bridge is intentionally outside the planner: it models the handoff from
    session messages produced by 02B into the 02C ToolExecutionRuntime. It does
    not execute tools and it does not bypass validation, permission or budgets.
    """

    def __init__(
        self,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def build_report(
        self,
        *,
        session_id: str,
        worker_request_id: str,
        request_messages: Sequence[Any] = (),
        constraints: Mapping[str, Any] | None = None,
        fallback_turns: Sequence[Sequence[Mapping[str, Any]]] = (),
    ) -> ToolSessionBridgeReport:
        constraints = dict(constraints or {})
        fallback_count = len(list(fallback_turns or ()))
        findings: list[ToolSessionBridgeFinding] = []
        tool_uses: list[ToolSessionToolUse] = []
        tool_uses.extend(
            self._extract_from_messages(
                request_messages,
                origin=ToolSessionBridgeOrigin.REQUEST_MESSAGES,
                findings=findings,
            )
        )
        session_messages = constraints.get("session_messages")
        if isinstance(session_messages, Sequence) and not isinstance(session_messages, (str, bytes, bytearray)):
            tool_uses.extend(
                self._extract_from_messages(
                    session_messages,
                    origin=ToolSessionBridgeOrigin.CONSTRAINT_SESSION_MESSAGES,
                    findings=findings,
                )
            )
        assistant_tool_uses = constraints.get("assistant_tool_uses")
        if isinstance(assistant_tool_uses, Sequence) and not isinstance(assistant_tool_uses, (str, bytes, bytearray)):
            tool_uses.extend(
                self._extract_from_tool_use_list(
                    assistant_tool_uses,
                    origin=ToolSessionBridgeOrigin.CONSTRAINT_ASSISTANT_TOOL_USES,
                    findings=findings,
                )
            )
        opencode_parts = constraints.get("opencode_tool_parts")
        if isinstance(opencode_parts, Sequence) and not isinstance(opencode_parts, (str, bytes, bytearray)):
            pseudo_message = {"role": "assistant", "parts": list(opencode_parts), "metadata": {"source": "constraints.opencode_tool_parts"}}
            tool_uses.extend(
                self._extract_from_messages(
                    [pseudo_message],
                    origin=ToolSessionBridgeOrigin.CONSTRAINT_OPENCODE_PARTS,
                    findings=findings,
                )
            )

        tool_uses = _dedupe_tool_uses(tool_uses)
        if tool_uses:
            turns = tuple(_group_tool_uses_by_turn(tool_uses))
            origin = _dominant_origin(tool_uses)
            fallback_used = False
        elif fallback_count:
            turns = tuple(_turns_from_fallback(fallback_turns))
            origin = ToolSessionBridgeOrigin.FALLBACK_STRUCTURED_PLAN
            fallback_used = True
            findings.append(
                ToolSessionBridgeFinding(
                    code="FALLBACK_STRUCTURED_PLAN_USED",
                    severity=ToolSessionBridgeFindingSeverity.INFO,
                    surface=ToolSessionBridgeSurface.FALLBACK,
                    message="No assistant tool_use blocks were found; existing structured plan remains the QueryEngine source.",
                    metadata={"fallback_turn_count": str(fallback_count)},
                )
            )
        else:
            turns = ()
            origin = ToolSessionBridgeOrigin.EMPTY
            fallback_used = False
            findings.append(
                ToolSessionBridgeFinding(
                    code="NO_TOOL_USE_SOURCE",
                    severity=ToolSessionBridgeFindingSeverity.WARNING,
                    surface=ToolSessionBridgeSurface.TOOL_USE,
                    message="No assistant tool_use blocks or structured fallback turns were available.",
                )
            )

        return ToolSessionBridgeReport(
            report_id=new_id("toolsessionbridge"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            origin=origin,
            turns=turns,
            fallback_turn_count=fallback_count,
            fallback_used=fallback_used,
            findings=tuple(findings),
            metadata_values={
                "tool_session_bridge_source_path": "packages/runtime/zyra_runtime/tool_runtime_session_bridge.py",
                "tool_session_bridge_upstream_claude": "src/query.ts assistant tool_use stream",
                "tool_session_bridge_upstream_opencode": "packages/opencode/src/session/message.ts tool-call parts",
                "tool_session_bridge_upstream_hermes": "hermes-agent approval/tool handoff",
            },
        )

    def events_for_report(
        self,
        report: ToolSessionBridgeReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> list[EventRecord]:
        events = [
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "query_session": {
                        "session_id": report.session_id,
                        "worker_request_id": report.worker_request_id,
                        "phase": "tool_session_bridge",
                        "tool_session_bridge": report.to_dict(),
                    }
                },
            )
        ]
        for turn in report.turns:
            for tool_use in turn.tool_uses:
                events.append(
                    EventRecord(
                        run_id=run_id,
                        task_id=task_id,
                        node_id=node_id,
                        event_type=EventType.AGENT_MESSAGE,
                        payload={
                            "query_session": {
                                "session_id": report.session_id,
                                "worker_request_id": report.worker_request_id,
                                "phase": "assistant_tool_use_requested",
                                "turn_index": turn.turn_index,
                                "tool_session_bridge_tool_use": tool_use.to_dict(),
                            }
                        },
                    )
                )
        return events

    def _extract_from_messages(
        self,
        messages: Sequence[Any],
        *,
        origin: ToolSessionBridgeOrigin,
        findings: list[ToolSessionBridgeFinding],
    ) -> list[ToolSessionToolUse]:
        output: list[ToolSessionToolUse] = []
        for message_index, raw_message in enumerate(messages or (), start=1):
            message = _message_mapping(raw_message)
            role = _message_role(message)
            if role and role not in {"assistant", "agent", "model"}:
                continue
            message_id = _message_id(message)
            prompt = _message_prompt(message)
            blocks = list(_iter_tool_blocks(message))
            if not blocks:
                continue
            for block_index, block in enumerate(blocks, start=1):
                extracted = _tool_use_from_block(
                    block,
                    origin=origin,
                    message_index=message_index,
                    block_index=block_index,
                    role=role or "assistant",
                    message_id=message_id,
                    prompt=prompt,
                )
                if extracted is None:
                    findings.append(
                        ToolSessionBridgeFinding(
                            code="UNSUPPORTED_TOOL_USE_BLOCK",
                            severity=ToolSessionBridgeFindingSeverity.WARNING,
                            surface=ToolSessionBridgeSurface.REQUEST_MESSAGE,
                            message="A candidate assistant block did not contain a usable tool name.",
                            origin=str(origin),
                            message_index=message_index,
                            block_index=block_index,
                        )
                    )
                    continue
                output.append(extracted)
        return output

    def _extract_from_tool_use_list(
        self,
        items: Sequence[Any],
        *,
        origin: ToolSessionBridgeOrigin,
        findings: list[ToolSessionBridgeFinding],
    ) -> list[ToolSessionToolUse]:
        output: list[ToolSessionToolUse] = []
        for index, item in enumerate(items or (), start=1):
            block = _coerce_block(item)
            extracted = _tool_use_from_block(
                block,
                origin=origin,
                message_index=index,
                block_index=1,
                role="assistant",
                message_id=str(block.get("message_id") or ""),
                prompt=str(block.get("prompt") or ""),
            )
            if extracted is None:
                findings.append(
                    ToolSessionBridgeFinding(
                        code="INVALID_ASSISTANT_TOOL_USE",
                        severity=ToolSessionBridgeFindingSeverity.WARNING,
                        surface=ToolSessionBridgeSurface.CONSTRAINT,
                        message="constraints.assistant_tool_uses contained an entry without a usable tool name.",
                        origin=str(origin),
                        message_index=index,
                    )
                )
                continue
            output.append(extracted)
        return output


def tool_session_bridge_metadata(report: ToolSessionBridgeReport | None) -> dict[str, str]:
    return report.metadata() if report is not None else {
        "tool_session_bridge_ok": "false",
        "tool_session_bridge_tool_uses": "0",
        "tool_session_bridge_turns": "0",
    }


def _message_mapping(raw_message: Any) -> dict[str, Any]:
    if isinstance(raw_message, Mapping):
        return dict(raw_message)
    payload = to_jsonable(raw_message)
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"role": "assistant", "content": str(raw_message)}


def _message_role(message: Mapping[str, Any]) -> str:
    role = message.get("role") or message.get("sender_role") or message.get("author")
    if role is None and "assistant" in str(message.get("type") or "").lower():
        role = "assistant"
    return str(role or "").lower()


def _message_id(message: Mapping[str, Any]) -> str:
    return str(message.get("message_id") or message.get("id") or message.get("uuid") or "")


def _message_prompt(message: Mapping[str, Any]) -> str:
    for key in ("prompt", "text", "summary"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    content = message.get("content")
    if isinstance(content, str) and content.strip() and not _looks_like_json(content):
        return content.strip()
    return ""


def _iter_tool_blocks(message: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    for key in ("content", "parts", "blocks", "items"):
        value = message.get(key)
        yield from _blocks_from_value(value)
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, Sequence) and not isinstance(tool_calls, (str, bytes, bytearray)):
        for item in tool_calls:
            block = _coerce_block(item)
            block.setdefault("type", "openai_tool_call")
            yield block
    if _is_tool_block(message):
        yield message


def _blocks_from_value(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        if _is_tool_block(value):
            yield dict(value)
        for key in ("content", "parts", "blocks", "items", "tool_calls"):
            nested = value.get(key)
            if nested is not value:
                yield from _blocks_from_value(nested)
        return
    if isinstance(value, str):
        parsed = _parse_jsonish(value)
        if parsed is not None:
            yield from _blocks_from_value(parsed)
        return
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        for item in value:
            yield from _blocks_from_value(item)


def _is_tool_block(value: Mapping[str, Any]) -> bool:
    block_type = str(value.get("type") or value.get("kind") or "").lower().replace("_", "-")
    if block_type in {"tool-use", "tool-call", "tool-call-request", "openai-tool-call"}:
        return True
    if value.get("tool_use") is True or value.get("tool_call") is True:
        return True
    if value.get("function") is not None and (value.get("id") or value.get("tool_call_id")):
        return True
    return any(key in value for key in ("tool_name", "tool", "name")) and any(
        key in value for key in ("input", "arguments", "args", "parameters")
    )


def _tool_use_from_block(
    block: Mapping[str, Any],
    *,
    origin: ToolSessionBridgeOrigin,
    message_index: int,
    block_index: int,
    role: str,
    message_id: str,
    prompt: str,
) -> ToolSessionToolUse | None:
    payload = _coerce_block(block)
    function = payload.get("function") if isinstance(payload.get("function"), Mapping) else {}
    raw_type = str(payload.get("type") or payload.get("kind") or "").lower().replace("_", "-")
    source_format = _source_format(raw_type, payload)
    tool_name = str(
        payload.get("name")
        or payload.get("tool_name")
        or payload.get("tool")
        or function.get("name")
        or ""
    )
    if not tool_name:
        return None
    arguments = _arguments_from_block(payload, function)
    upstream_id = str(
        payload.get("id")
        or payload.get("tool_use_id")
        or payload.get("tool_call_id")
        or payload.get("call_id")
        or payload.get("callID")
        or payload.get("toolCallID")
        or ""
    )
    metadata = _metadata_from_block(payload)
    metadata.setdefault("raw_block_type", raw_type)
    metadata.setdefault("source_format", str(source_format))
    if source_format == ToolSessionBridgeFormat.OPENCODE_TOOL_CALL_PART:
        metadata.setdefault("opencode_session_part", "true")
    if source_format == ToolSessionBridgeFormat.HERMES_PERMISSION_TOOL_USE:
        metadata.setdefault("hermes_permission_handoff", "true")
    return ToolSessionToolUse(
        bridge_tool_use_id=new_id("toolusebridge"),
        tool_name=tool_name,
        arguments=arguments,
        origin=origin,
        source_format=source_format,
        message_index=message_index,
        block_index=block_index,
        turn_index=_positive_int(payload.get("turn_index") or payload.get("turn") or message_index, default=message_index),
        raw_role=role,
        source_message_id=message_id,
        upstream_tool_use_id=upstream_id,
        prompt=prompt or str(payload.get("prompt") or ""),
        metadata=metadata,
    )


def _source_format(raw_type: str, payload: Mapping[str, Any]) -> ToolSessionBridgeFormat:
    if raw_type in {"tool-use", "tool-use-block"}:
        if str(payload.get("source") or "").lower().startswith("hermes"):
            return ToolSessionBridgeFormat.HERMES_PERMISSION_TOOL_USE
        return ToolSessionBridgeFormat.CLAUDE_TOOL_USE_BLOCK
    if raw_type in {"tool-call", "tool-call-request"} or any(key in payload for key in ("callID", "toolCallID")):
        return ToolSessionBridgeFormat.OPENCODE_TOOL_CALL_PART
    if raw_type == "openai-tool-call" or payload.get("function") is not None:
        return ToolSessionBridgeFormat.OPENAI_TOOL_CALL
    if str(payload.get("source") or "").lower().startswith("hermes"):
        return ToolSessionBridgeFormat.HERMES_PERMISSION_TOOL_USE
    if any(key in payload for key in ("tool_name", "tool", "name")):
        return ToolSessionBridgeFormat.STRUCTURED_TOOL_USE
    return ToolSessionBridgeFormat.UNKNOWN


def _arguments_from_block(payload: Mapping[str, Any], function: Mapping[str, Any]) -> dict[str, Any]:
    for key in ("input", "arguments", "args", "parameters"):
        value = payload.get(key)
        parsed = _parse_arguments(value)
        if parsed is not None:
            return parsed
    parsed = _parse_arguments(function.get("arguments"))
    if parsed is not None:
        return parsed
    return {}


def _parse_arguments(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        parsed = _parse_jsonish(value)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return None


def _metadata_from_block(payload: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    raw_metadata = payload.get("metadata")
    if isinstance(raw_metadata, Mapping):
        metadata.update({str(key): str(value) for key, value in raw_metadata.items()})
    for key in ("source", "provider", "model", "session_id", "message_uuid", "part_id"):
        if payload.get(key) not in (None, ""):
            metadata[f"raw_{key}"] = str(payload[key])
    return metadata


def _coerce_block(item: Any) -> dict[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    payload = to_jsonable(item)
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"type": "tool_use", "name": "", "input": {}, "raw": str(item)}


def _parse_jsonish(value: str) -> Any | None:
    text = value.strip()
    if not text or text[0] not in "[{":
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _looks_like_json(value: str) -> bool:
    text = value.strip()
    return bool(text) and text[0] in "[{"


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _dedupe_tool_uses(tool_uses: Sequence[ToolSessionToolUse]) -> list[ToolSessionToolUse]:
    seen: set[tuple[str, str, str, str]] = set()
    output: list[ToolSessionToolUse] = []
    for item in tool_uses:
        key = (
            item.upstream_tool_use_id,
            item.tool_name,
            json.dumps(to_jsonable(item.arguments), ensure_ascii=False, sort_keys=True),
            str(item.origin),
        )
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def _group_tool_uses_by_turn(tool_uses: Sequence[ToolSessionToolUse]) -> list[ToolSessionBridgeTurn]:
    grouped: dict[int, list[ToolSessionToolUse]] = {}
    for item in tool_uses:
        grouped.setdefault(item.turn_index, []).append(item)
    turns: list[ToolSessionBridgeTurn] = []
    for new_index, source_turn in enumerate(sorted(grouped), start=1):
        items = tuple(sorted(grouped[source_turn], key=lambda item: (item.message_index, item.block_index, item.bridge_tool_use_id)))
        prompt = next((item.prompt for item in items if item.prompt), f"assistant tool_use turn {new_index}")
        origins = {item.origin for item in items}
        origin = next(iter(origins)) if len(origins) == 1 else ToolSessionBridgeOrigin.CONSTRAINT_SESSION_MESSAGES
        turns.append(
            ToolSessionBridgeTurn(
                turn_index=new_index,
                origin=origin,
                tool_uses=items,
                prompt=prompt,
                source_message_indices=tuple(sorted({item.message_index for item in items})),
                metadata={
                    "source_turn_index": str(source_turn),
                    "source_origin_count": str(len(origins)),
                    "tool_names": ",".join(item.tool_name for item in items),
                },
            )
        )
    return turns


def _turns_from_fallback(fallback_turns: Sequence[Sequence[Mapping[str, Any]]]) -> list[ToolSessionBridgeTurn]:
    turns: list[ToolSessionBridgeTurn] = []
    for turn_index, raw_turn in enumerate(fallback_turns or (), start=1):
        tool_uses: list[ToolSessionToolUse] = []
        for step_index, raw_step in enumerate(raw_turn or (), start=1):
            step = dict(raw_step or {})
            tool_name = str(step.get("tool_name") or step.get("tool") or step.get("name") or "")
            args = step.get("arguments") if isinstance(step.get("arguments"), Mapping) else {}
            metadata = step.get("metadata") if isinstance(step.get("metadata"), Mapping) else {}
            tool_uses.append(
                ToolSessionToolUse(
                    bridge_tool_use_id=new_id("toolusebridge"),
                    tool_name=tool_name,
                    arguments=dict(args),
                    origin=ToolSessionBridgeOrigin.FALLBACK_STRUCTURED_PLAN,
                    source_format=ToolSessionBridgeFormat.STRUCTURED_TOOL_USE,
                    message_index=turn_index,
                    block_index=step_index,
                    turn_index=turn_index,
                    upstream_tool_use_id=str(step.get("tool_call_id") or ""),
                    prompt=str(step.get("prompt") or ""),
                    metadata={str(k): str(v) for k, v in metadata.items()},
                )
            )
        turns.append(
            ToolSessionBridgeTurn(
                turn_index=turn_index,
                origin=ToolSessionBridgeOrigin.FALLBACK_STRUCTURED_PLAN,
                tool_uses=tuple(tool_uses),
                prompt=f"structured fallback tool turn {turn_index}",
                source_message_indices=(turn_index,),
                metadata={"fallback": "true"},
            )
        )
    return turns


def _dominant_origin(tool_uses: Sequence[ToolSessionToolUse]) -> ToolSessionBridgeOrigin:
    priority = [
        ToolSessionBridgeOrigin.REQUEST_MESSAGES,
        ToolSessionBridgeOrigin.CONSTRAINT_SESSION_MESSAGES,
        ToolSessionBridgeOrigin.CONSTRAINT_ASSISTANT_TOOL_USES,
        ToolSessionBridgeOrigin.CONSTRAINT_OPENCODE_PARTS,
    ]
    present = {item.origin for item in tool_uses}
    for origin in priority:
        if origin in present:
            return origin
    return ToolSessionBridgeOrigin.EMPTY
