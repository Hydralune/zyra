from __future__ import annotations

"""MCP result normalization and artifact custody.

The external server is allowed to return heterogeneous content, but it is not
allowed to decide what enters a model context, event log, or transcript.  This
module is the MCP-specific front half of result shaping.  The generic 02C tool
budget remains the aggregate/per-turn owner after this runtime has converted
binary and large structured values into Zyra ``ArtifactRef`` objects.
"""

import json
import mimetypes
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, now_iso, to_jsonable
from zyra_runtime import LocalArtifactStore, ToolResult

from .models import (
    JsonValue,
    McpContent,
    McpContentKind,
    McpModelError,
    canonical_json,
    redact_value,
    stable_digest,
)


class McpOutputError(RuntimeError):
    """Raised when an MCP response cannot be safely normalized."""


class McpOutputRuntimeDisabled(McpOutputError):
    pass


@dataclass(frozen=True, slots=True)
class McpOutputPolicy:
    max_inline_text_chars: int = 12_000
    max_inline_structured_chars: int = 8_000
    max_total_inline_chars: int = 20_000
    max_binary_bytes: int = 16 * 1024 * 1024
    max_content_blocks: int = 256
    preview_chars: int = 2_000
    externalize_all_binary: bool = True
    externalize_large_text: bool = True
    externalize_large_structured: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_inline_text_chars",
            "max_inline_structured_chars",
            "max_total_inline_chars",
            "max_binary_bytes",
            "max_content_blocks",
            "preview_chars",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.preview_chars > self.max_inline_text_chars:
            object.__setattr__(self, "preview_chars", self.max_inline_text_chars)


@dataclass(frozen=True, slots=True)
class McpContentProjection:
    index: int
    kind: McpContentKind
    inline: JsonValue
    size_bytes: int
    digest: str
    artifact_id: str = ""
    truncated: bool = False
    mime_type: str = ""
    source_uri: str = ""
    security_flags: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "index": self.index,
            "kind": str(self.kind),
            "inline": self.inline,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "artifact_id": self.artifact_id,
            "truncated": self.truncated,
            "mime_type": self.mime_type,
            "source_uri": self.source_uri,
            "security_flags": list(self.security_flags),
        }


@dataclass(frozen=True, slots=True)
class McpOutputReceipt:
    receipt_id: str
    server_id: str
    tool_name: str
    tool_call_id: str
    result: ToolResult
    projections: tuple[McpContentProjection, ...]
    artifacts: tuple[ArtifactRef, ...]
    is_error: bool
    original_chars: int
    inline_chars: int
    externalized_bytes: int
    structured_digest: str
    warnings: tuple[str, ...] = ()
    created_at: str = field(default_factory=now_iso)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "receipt_id": self.receipt_id,
            "server_id": self.server_id,
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "ok": self.result.ok,
            "is_error": self.is_error,
            "projections": [item.to_dict() for item in self.projections],
            "artifact_ids": [item.artifact_id for item in self.artifacts],
            "original_chars": self.original_chars,
            "inline_chars": self.inline_chars,
            "externalized_bytes": self.externalized_bytes,
            "structured_digest": self.structured_digest,
            "warnings": list(self.warnings),
            "created_at": self.created_at,
        }


PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore_prior_instructions", re.compile(r"\bignore\s+(all\s+)?(previous|prior)\s+instructions\b", re.I)),
    ("system_prompt_claim", re.compile(r"\b(system|developer)\s+(prompt|message)\b", re.I)),
    ("secret_exfiltration_request", re.compile(r"\b(api[_ -]?key|access[_ -]?token|password|credential)s?\b.{0,80}\b(send|upload|post|exfiltrate)\b", re.I | re.S)),
    ("permission_bypass_request", re.compile(r"\b(bypass|disable|ignore)\b.{0,60}\b(permission|approval|sandbox|policy)\b", re.I | re.S)),
)


class McpOutputBudgetRuntime:
    """Convert untrusted MCP result blocks into bounded Zyra tool results."""

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        policy: McpOutputPolicy | None = None,
        disabled: bool = False,
    ) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or McpOutputPolicy()
        self.disabled = disabled

    def normalize_tool_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        server_id: str,
        tool_name: str,
        tool_call_id: str,
        raw_result: Mapping[str, Any],
    ) -> McpOutputReceipt:
        if self.disabled:
            raise McpOutputRuntimeDisabled("McpOutputBudgetRuntime is disabled")
        if not isinstance(raw_result, Mapping):
            raise McpOutputError("MCP tool result must be an object")

        raw_blocks = raw_result.get("content") or []
        if isinstance(raw_blocks, Mapping):
            raw_blocks = [raw_blocks]
        if not isinstance(raw_blocks, Sequence) or isinstance(raw_blocks, (str, bytes, bytearray)):
            raise McpOutputError("MCP tool result content must be an array")
        if len(raw_blocks) > self.policy.max_content_blocks:
            raise McpOutputError(
                f"MCP tool result contains {len(raw_blocks)} blocks; maximum is {self.policy.max_content_blocks}"
            )

        blocks: list[McpContent] = []
        warnings: list[str] = []
        for index, raw in enumerate(raw_blocks):
            if not isinstance(raw, Mapping):
                raise McpOutputError(f"MCP content block {index} is not an object")
            try:
                blocks.append(McpContent.from_wire(raw))
            except (McpModelError, TypeError, ValueError) as error:
                raise McpOutputError(f"invalid MCP content block {index}: {error}") from error

        structured = raw_result.get("structuredContent")
        if structured is not None:
            blocks.append(
                McpContent(
                    McpContentKind.STRUCTURED,
                    structured=to_jsonable(structured),
                )
            )

        projections: list[McpContentProjection] = []
        artifacts: list[ArtifactRef] = []
        inline_chars = 0
        original_chars = 0
        externalized_bytes = 0
        output_blocks: list[JsonValue] = []

        for index, block in enumerate(blocks):
            projection, artifact = self._project_block(
                block,
                index=index,
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                server_id=server_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
            )
            projections.append(projection)
            original_chars += _content_chars(block)
            inline_chars += len(canonical_json(projection.inline)) if projection.inline is not None else 0
            if artifact is not None:
                artifacts.append(artifact)
                externalized_bytes += block.size_bytes
            output_blocks.append(projection.inline)
            warnings.extend(projection.security_flags)

        if inline_chars > self.policy.max_total_inline_chars:
            summary_payload = {
                "blocks": output_blocks,
                "server_id": server_id,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
            }
            artifact = self.artifact_store.write_text(
                run_id=run_id,
                task_id=task_id,
                content=json.dumps(summary_payload, ensure_ascii=False, indent=2, sort_keys=True),
                title=f"MCP inline overflow {server_id}/{tool_name}",
                kind=ArtifactKind.STRUCTURED_DATA,
                extension=".json",
                producer_node_id=node_id,
            )
            artifacts.append(artifact)
            output_blocks = [
                {
                    "type": "artifact_ref",
                    "artifact_id": artifact.artifact_id,
                    "reason": "mcp_total_inline_budget_exceeded",
                    "preview": canonical_json(output_blocks)[: self.policy.preview_chars],
                    "original_chars": inline_chars,
                }
            ]
            inline_chars = len(canonical_json(output_blocks))
            warnings.append("mcp_total_inline_budget_exceeded")

        is_error = bool(raw_result.get("isError"))
        summary = self._summary(server_id, tool_name, blocks, is_error, artifacts)
        safe_output = {
            "mcp": {
                "server_id": server_id,
                "tool_name": tool_name,
                "content": output_blocks,
                "structured_content_present": structured is not None,
                "artifact_ids": [item.artifact_id for item in artifacts],
                "warnings": sorted(set(warnings)),
                "untrusted_external_content": True,
            }
        }
        result = ToolResult(
            tool_call_id=tool_call_id,
            ok=not is_error,
            summary=summary,
            output=safe_output,
            artifacts=list(artifacts),
            error="mcp_tool_error" if is_error else None,
            metadata={
                "tool_namespace": "mcp",
                "server_id": server_id,
                "mcp_tool_name": tool_name,
                "mcp_content_blocks": str(len(blocks)),
                "mcp_artifact_count": str(len(artifacts)),
                "mcp_original_chars": str(original_chars),
                "mcp_inline_chars": str(inline_chars),
                "mcp_externalized_bytes": str(externalized_bytes),
                "mcp_untrusted_external_content": "true",
                "mcp_output_runtime": "McpOutputBudgetRuntime",
                "mcp_output_already_externalized": str(bool(artifacts)).lower(),
            },
        )
        receipt = McpOutputReceipt(
            receipt_id=new_id("mcpoutput"),
            server_id=server_id,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=result,
            projections=tuple(projections),
            artifacts=tuple(artifacts),
            is_error=is_error,
            original_chars=original_chars,
            inline_chars=inline_chars,
            externalized_bytes=externalized_bytes,
            structured_digest=stable_digest(to_jsonable(structured)) if structured is not None else "",
            warnings=tuple(sorted(set(warnings))),
        )
        return receipt

    def normalize_resource_result(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        server_id: str,
        uri: str,
        raw_result: Mapping[str, Any],
    ) -> McpOutputReceipt:
        contents = raw_result.get("contents")
        if not isinstance(contents, list):
            raise McpOutputError("resources/read result requires a contents array")
        blocks: list[dict[str, Any]] = []
        for item in contents:
            if not isinstance(item, Mapping):
                raise McpOutputError("resource content must be an object")
            resource = {
                "uri": item.get("uri") or uri,
                "mimeType": item.get("mimeType") or "application/octet-stream",
            }
            if isinstance(item.get("blob"), str):
                resource["blob"] = item["blob"]
            elif isinstance(item.get("text"), str):
                resource["text"] = item["text"]
            else:
                raise McpOutputError("resource content requires text or blob")
            blocks.append({"type": "resource", "resource": resource})
        return self.normalize_tool_result(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            server_id=server_id,
            tool_name="resources/read",
            tool_call_id=new_id("mcpresource"),
            raw_result={"content": blocks, "isError": False},
        )

    def event_for_receipt(
        self,
        receipt: McpOutputReceipt,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        cause_event_id: str = "",
        connection_generation: int = 0,
        capability_generation: int = 0,
    ) -> EventRecord:
        event_type = getattr(EventType, "MCP_TOOL_RESULT", EventType.SYSTEM_NOTICE)
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=event_type,
            payload={
                "mcp_runtime": {
                    "schema": "zyra.mcp-output-event.v1",
                    "runtime_id": "McpOutputBudgetRuntime",
                    "server_id": receipt.server_id,
                    "tool_name": receipt.tool_name,
                    "tool_call_id": receipt.tool_call_id,
                    "cause_event_id": cause_event_id,
                    "connection_generation": connection_generation,
                    "capability_generation": capability_generation,
                    "output": receipt.safe_dict(),
                }
            },
        )

    def _project_block(
        self,
        block: McpContent,
        *,
        index: int,
        run_id: str,
        task_id: str,
        node_id: str | None,
        server_id: str,
        tool_name: str,
        tool_call_id: str,
    ) -> tuple[McpContentProjection, ArtifactRef | None]:
        flags = tuple(_security_flags(block))
        artifact: ArtifactRef | None = None
        inline: JsonValue
        truncated = False

        if block.data:
            if len(block.data) > self.policy.max_binary_bytes:
                raise McpOutputError(
                    f"binary MCP block exceeds {self.policy.max_binary_bytes} byte custody limit"
                )
            extension = _extension_for_mime(block.mime_type)
            artifact = self.artifact_store.write_bytes(
                run_id=run_id,
                task_id=task_id,
                content=block.data,
                title=f"MCP {server_id}/{tool_name} block {index}",
                kind=_artifact_kind_for_mime(block.mime_type),
                extension=extension,
                producer_node_id=node_id,
                metadata={
                    "source": "mcp",
                    "server_id": server_id,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "content_index": index,
                    "mime_type": block.mime_type,
                    "digest": block.digest,
                    "source_uri": block.uri,
                    "untrusted_external_content": True,
                },
            )
            inline = {
                "type": "artifact_ref",
                "kind": str(block.kind),
                "artifact_id": artifact.artifact_id,
                "mime_type": block.mime_type,
                "size_bytes": block.size_bytes,
                "digest": block.digest,
                "source_uri": block.uri,
            }
        elif block.kind is McpContentKind.STRUCTURED:
            encoded = canonical_json(block.structured)
            if self.policy.externalize_large_structured and len(encoded) > self.policy.max_inline_structured_chars:
                artifact = self.artifact_store.write_text(
                    run_id=run_id,
                    task_id=task_id,
                    content=json.dumps(block.structured, ensure_ascii=False, indent=2, sort_keys=True),
                    title=f"MCP structured result {server_id}/{tool_name} block {index}",
                    kind=ArtifactKind.STRUCTURED_DATA,
                    extension=".json",
                    producer_node_id=node_id,
                )
                inline = {
                    "type": "artifact_ref",
                    "kind": "structured",
                    "artifact_id": artifact.artifact_id,
                    "preview": encoded[: self.policy.preview_chars],
                    "original_chars": len(encoded),
                    "digest": block.digest,
                }
                truncated = True
            else:
                inline = {"type": "structured", "value": block.structured, "digest": block.digest}
        elif block.text:
            if self.policy.externalize_large_text and len(block.text) > self.policy.max_inline_text_chars:
                artifact = self.artifact_store.write_text(
                    run_id=run_id,
                    task_id=task_id,
                    content=block.text,
                    title=f"MCP text result {server_id}/{tool_name} block {index}",
                    kind=ArtifactKind.TEXT,
                    extension=_extension_for_mime(block.mime_type, default=".txt"),
                    producer_node_id=node_id,
                )
                inline = {
                    "type": "artifact_ref",
                    "kind": str(block.kind),
                    "artifact_id": artifact.artifact_id,
                    "preview": block.text[: self.policy.preview_chars],
                    "original_chars": len(block.text),
                    "digest": block.digest,
                    "source_uri": block.uri,
                }
                truncated = True
            else:
                inline = {
                    "type": str(block.kind),
                    "text": block.text,
                    "uri": block.uri,
                    "mime_type": block.mime_type,
                    "digest": block.digest,
                }
        elif block.kind is McpContentKind.RESOURCE_LINK:
            inline = {
                "type": "resource_link",
                "uri": block.uri,
                "name": block.name,
                "mime_type": block.mime_type,
            }
        else:
            inline = block.safe_dict()

        if flags and isinstance(inline, dict):
            inline["security_flags"] = list(flags)
            inline["untrusted_external_content"] = True
        return (
            McpContentProjection(
                index=index,
                kind=block.kind,
                inline=redact_value(inline),
                size_bytes=block.size_bytes,
                digest=block.digest,
                artifact_id=artifact.artifact_id if artifact else "",
                truncated=truncated,
                mime_type=block.mime_type,
                source_uri=block.uri,
                security_flags=flags,
            ),
            artifact,
        )

    @staticmethod
    def _summary(
        server_id: str,
        tool_name: str,
        blocks: Sequence[McpContent],
        is_error: bool,
        artifacts: Sequence[ArtifactRef],
    ) -> str:
        state = "failed" if is_error else "completed"
        return (
            f"MCP tool {server_id}/{tool_name} {state}: "
            f"{len(blocks)} content block(s), {len(artifacts)} artifact(s)."
        )


def _content_chars(content: McpContent) -> int:
    if content.text:
        return len(content.text)
    if content.structured is not None:
        return len(canonical_json(content.structured))
    if content.data:
        return len(content.data)
    return len(content.uri) + len(content.name)


def _security_flags(content: McpContent) -> Iterable[str]:
    text = content.text
    if content.structured is not None:
        text += "\n" + canonical_json(content.structured)
    for name, pattern in PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            yield name


def _extension_for_mime(mime_type: str, *, default: str = ".bin") -> str:
    normalized = (mime_type or "").split(";", 1)[0].strip().lower()
    safe = {
        "application/json": ".json",
        "application/pdf": ".pdf",
        "application/xml": ".xml",
        "audio/mpeg": ".mp3",
        "audio/wav": ".wav",
        "image/gif": ".gif",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "text/csv": ".csv",
        "text/html": ".html",
        "text/markdown": ".md",
        "text/plain": ".txt",
    }
    if normalized in safe:
        return safe[normalized]
    guessed = mimetypes.guess_extension(normalized) if normalized else None
    if guessed and re.fullmatch(r"\.[A-Za-z0-9]{1,10}", guessed):
        return guessed
    return default


def _artifact_kind_for_mime(mime_type: str) -> ArtifactKind:
    normalized = (mime_type or "").lower()
    if normalized.startswith("image/"):
        return ArtifactKind.SCREENSHOT
    if normalized in {"application/json", "text/csv"}:
        return ArtifactKind.STRUCTURED_DATA
    if normalized.startswith("text/"):
        return ArtifactKind.TEXT
    return ArtifactKind.FILE


__all__ = [
    "McpContentProjection",
    "McpOutputBudgetRuntime",
    "McpOutputError",
    "McpOutputPolicy",
    "McpOutputReceipt",
    "McpOutputRuntimeDisabled",
    "PROMPT_INJECTION_PATTERNS",
]
