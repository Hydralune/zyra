from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


PRODUCT_PRESENTATION_SCHEMA = "zyra.product-presentation/v1"
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{6,}")
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|authorization)"
    r"\s*[:=]\s*[^\s,;]+"
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[A-Z]:\\|\\\\)[^\r\n\t<>|\"]+")
_OUTPUT_STREAM = re.compile(r"(?:^|[._/])(stdout|stderr)(?:$|[._/])", re.IGNORECASE)
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _identity(value: Any) -> str:
    selected = str(value or "").strip()
    return selected if _IDENTITY.fullmatch(selected) else ""


def _text(value: Any, maximum: int = 500) -> str:
    selected = str(value or "").strip()
    if not selected:
        return ""
    selected = " ".join(selected.split())
    selected = _BEARER.sub("Bearer [REDACTED]", selected)
    selected = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", selected)
    selected = _WINDOWS_PATH.sub("<path>", selected)
    return selected[:maximum]


def _content(value: Any, maximum: int = 2_048) -> str:
    selected = str(value or "")
    if not selected:
        return ""
    selected = _ANSI.sub("", selected)
    selected = _CONTROL.sub("�", selected)
    selected = _BEARER.sub("Bearer [REDACTED]", selected)
    selected = _ASSIGNMENT.sub(lambda match: f"{match.group(1)}=[REDACTED]", selected)
    selected = _WINDOWS_PATH.sub("<path>", selected)
    return selected[:maximum]


def _base(*, kind: str, phase: str, identity: str, label: str) -> dict[str, Any]:
    return {
        "schema": PRODUCT_PRESENTATION_SCHEMA,
        "kind": kind,
        "phase": phase,
        "identity": identity,
        "label": label,
        "severity": "info",
    }


def _failure_fields(
    result: dict[str, Any],
    inline: Mapping[str, Any],
    *,
    impact: str = "local",
) -> None:
    """Attach only explicit, bounded failure semantics to a product frame."""

    result["impact"] = impact
    code = (
        _identity(inline.get("error_code"))
        or _identity(inline.get("reason_code"))
        or _identity(inline.get("status"))
    )
    if code:
        result["code"] = code
    if inline.get("retryable") is True:
        result["retryable"] = True
    recovery = _text(inline.get("recovery"), 2_048)
    if recovery:
        result["recovery"] = recovery


def _tool_output_artifacts(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    output: list[dict[str, Any]] = []
    for value in values:
        item = _mapping(value)
        artifact_id = _identity(item.get("artifactId"))
        metadata = _mapping(item.get("metadata"))
        source_path = str(metadata.get("source_path") or "")[:2_048]
        match = _OUTPUT_STREAM.search(source_path)
        if not artifact_id or match is None:
            continue
        size_bytes = item.get("sizeBytes")
        descriptor: dict[str, Any] = {
            "artifactId": artifact_id,
            "stream": match.group(1).lower(),
            "title": _text(item.get("title"), 256) or f"Tool {match.group(1).lower()}",
            "mediaType": _text(item.get("mediaType"), 128) or "application/octet-stream",
        }
        if isinstance(size_bytes, int) and 0 <= size_bytes <= 1_000_000_000_000:
            descriptor["sizeBytes"] = size_bytes
        output.append(descriptor)
        if len(output) >= 16:
            break
    return output


def project_product_presentation(canonical: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project a strict, secret-free user surface from a canonical runtime event.

    Unknown event types and generic summaries are intentionally rejected. This
    keeps newly added runtime internals out of the product UI until their schema
    has an explicit admission rule.
    """

    event_type = _text(canonical.get("eventType"), 128)
    event_id = _identity(canonical.get("eventId"))
    inline = _mapping(canonical.get("inline"))
    identity = _mapping(canonical.get("identity"))

    if event_type in {
        "runtime.text.started",
        "runtime.text.delta",
        "runtime.text.ended",
    }:
        phase = {
            "runtime.text.started": "started",
            "runtime.text.delta": "delta",
            "runtime.text.ended": "completed",
        }[event_type]
        stream_id = _identity(inline.get("stream_id"))
        message_id = _identity(inline.get("assistant_message_id")) or stream_id
        if not stream_id or not message_id:
            return None
        result = _base(
            kind="assistant",
            phase=phase,
            identity=message_id,
            label="Assistant",
        )
        result["streamId"] = stream_id
        presentation_text = _content(inline.get("presentation_text"))
        if presentation_text:
            result["text"] = presentation_text
        return result

    if event_type in {
        "runtime.backend.dispatch.requested",
        "runtime.backend.dispatch.accepted",
        "runtime.backend.dispatch.failed",
        "runtime.backend.failover",
    }:
        worker_id = (
            _identity(identity.get("workerId"))
            or _identity(inline.get("worker_id"))
            or _identity(_mapping(canonical.get("sender")).get("id"))
        )
        dispatch_id = _identity(inline.get("dispatch_id"))
        if not worker_id and not dispatch_id:
            return None
        attempt_id = _identity(inline.get("attempt_id"))
        phase = {
            "runtime.backend.dispatch.requested": "dispatched",
            "runtime.backend.dispatch.accepted": "running",
            "runtime.backend.dispatch.failed": "failed",
            "runtime.backend.failover": "replaced",
        }[event_type]
        result = _base(
            kind="worker",
            phase=phase,
            identity=attempt_id or dispatch_id or worker_id,
            label=worker_id or "Execution worker",
        )
        backend_id = _identity(inline.get("backend_id"))
        if backend_id:
            result["summary"] = (
                f"switched to backend {backend_id}"
                if event_type == "runtime.backend.failover"
                else f"backend {backend_id}"
            )
        if event_type == "runtime.backend.dispatch.failed":
            result["severity"] = "warning"
            _failure_fields(result, inline)
        elif event_type == "runtime.backend.failover":
            result["severity"] = "warning"
            result["impact"] = "local"
        return result

    if event_type == "runtime.node.failed":
        worker_id = (
            _identity(identity.get("workerId"))
            or _identity(identity.get("nodeId"))
            or _identity(inline.get("worker_id"))
            or _identity(inline.get("node_id"))
            or _identity(_mapping(canonical.get("sender")).get("id"))
            or event_id
        )
        if not worker_id:
            return None
        result = _base(
            kind="worker",
            phase="failed",
            identity=worker_id,
            label=worker_id,
        )
        result["severity"] = "warning"
        result["summary"] = (
            _text(inline.get("presentation_summary"))
            or _text(inline.get("reason"))
            or "worker attempt failed; recovery may replace it"
        )
        _failure_fields(result, inline)
        return result

    if event_type.startswith("runtime.subagent."):
        phase = {
            "runtime.subagent.created": "created",
            "runtime.subagent.dispatched": "dispatched",
            "runtime.subagent.progress": "running",
            "runtime.subagent.completed": "completed",
            "runtime.subagent.failed": "failed",
            "runtime.subagent.cancelled": "cancelled",
        }.get(event_type)
        subagent_id = _identity(inline.get("subagent_id")) or _identity(identity.get("subagentId"))
        if not phase or not subagent_id:
            return None
        result = _base(kind="worker", phase=phase, identity=subagent_id, label="协作代理")
        summary = _text(inline.get("presentation_summary")) or _text(inline.get("status"))
        if summary:
            result["summary"] = summary
        if phase in {"failed", "cancelled"}:
            result["severity"] = "warning"
            _failure_fields(result, inline)
        return result

    if event_type.startswith("runtime.tool."):
        phase = {
            "runtime.tool.called": "started",
            "runtime.tool.progress": "progress",
            "runtime.tool.succeeded": "completed",
            "runtime.tool.failed": "failed",
            "runtime.tool.cancelled": "cancelled",
        }.get(event_type)
        tool_call_id = _identity(inline.get("tool_call_id")) or _identity(identity.get("toolCallId"))
        if not phase or not tool_call_id:
            return None
        tool_name = _identity(inline.get("tool_name")) or "tool"
        result = _base(kind="tool", phase=phase, identity=tool_call_id, label=tool_name)
        summary = _text(
            inline.get("presentation_summary")
            or inline.get("result_summary")
            or inline.get("status_label")
        )
        if summary:
            result["summary"] = summary
        if phase in {"failed", "cancelled"}:
            result["severity"] = "error" if phase == "failed" else "warning"
            _failure_fields(result, inline)
        duration_ms = inline.get("duration_ms")
        if isinstance(duration_ms, int) and 0 <= duration_ms <= 86_400_000:
            result["durationMs"] = duration_ms
        artifact_refs = canonical.get("artifactRefs", [])
        result["artifactIds"] = [
            artifact_id
            for artifact_id in (
                _identity(_mapping(item).get("artifactId"))
                for item in artifact_refs
                if isinstance(item, Mapping)
            )
            if artifact_id
        ][:32]
        result["outputArtifacts"] = _tool_output_artifacts(artifact_refs)
        return result

    if event_type in {
        "runtime.recovery.requested",
        "runtime.recovery.planned",
        "runtime.recovery.completed",
    }:
        recovery_id = _identity(inline.get("recovery_id")) or event_id
        if not recovery_id:
            return None
        completed = event_type == "runtime.recovery.completed"
        requested = event_type == "runtime.recovery.requested"
        result = _base(
            kind="activity",
            phase="completed" if completed else "started" if requested else "updated",
            identity=f"recovery:{recovery_id}",
            label="Local failure recovery",
        )
        result["category"] = "recovery"
        result["impact"] = "local"
        result["severity"] = "info" if completed else "warning"
        strategy = _identity(inline.get("strategy"))
        result["summary"] = (
            f"recovery completed ({strategy})"
            if completed and strategy
            else "recovery completed"
            if completed
            else f"recovery requested ({strategy})"
            if requested and strategy
            else "recovery requested"
            if requested
            else f"recovery planned ({strategy})"
            if strategy
            else "recovery planned"
        )
        return result

    if event_type != "runtime.agent.message":
        return None

    schema = _text(inline.get("schema"), 128)
    if schema == "zyra.task-execution-started/v1":
        task_id = _identity(identity.get("taskId")) or event_id
        result = _base(
            kind="activity",
            phase="started",
            identity=f"task-execution:{task_id}",
            label="Task execution",
        )
        result["category"] = "execution"
        started_at = _text(inline.get("started_at"), 64)
        if started_at:
            result["startedAt"] = started_at
        return result

    if schema == "zyra.task-execution-error/v1":
        code = _identity(inline.get("error")) or "task_execution_error"
        message = _text(inline.get("message")) or "Task execution could not continue."
        result = _base(
            kind="issue",
            phase="failed",
            identity=f"task-execution-error:{event_id or code}",
            label=message,
        )
        result.update({"severity": "error", "code": code, "retryable": inline.get("retryable") is True})
        result["impact"] = "task"
        return result

    worker_operation = _text(inline.get("worker_pool_operation"), 128)
    if worker_operation in {"attempt_leased", "attempt_lost"}:
        worker_id = _identity(inline.get("worker_id"))
        attempt_id = _identity(inline.get("aggregate_id"))
        worker_identity = attempt_id or worker_id
        if not worker_identity:
            return None
        phase = "running" if worker_operation == "attempt_leased" else "replaced"
        result = _base(kind="worker", phase=phase, identity=worker_identity, label=worker_id or "Execution worker")
        if worker_operation == "attempt_lost":
            result["severity"] = "warning"
            result["summary"] = "execution ownership changed"
        return result

    # Generic runtime.agent.message is compatibility noise unless a schema
    # above has explicitly admitted it. Its summary is never user content.
    return None
