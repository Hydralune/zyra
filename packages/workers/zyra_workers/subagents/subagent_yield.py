from __future__ import annotations

"""Model-visible terminal yield tool for logical child executions."""

import copy
from dataclasses import dataclass
from threading import RLock
from typing import Any, Mapping

from zyra_core import new_id
from zyra_runtime import (
    DynamicToolProvenance,
    ProvenancedDynamicHandler,
    ToolCall,
    ToolResult,
    ToolSpec,
)

from .typed_yield import TypedYield, YieldKind


SUBAGENT_YIELD_TOOL_NAME = "SubagentYield"


class SubagentYieldError(RuntimeError):
    pass


class SubagentYieldConflict(SubagentYieldError):
    pass


@dataclass(frozen=True, slots=True)
class SubagentYieldBinding:
    spec: ToolSpec
    handler: ProvenancedDynamicHandler


class SubagentYieldCollector:
    """Collect exactly one terminal protocol value from one child task."""

    def __init__(self, *, task_id: str, parent_task_id: str, execution_ref: str, attempt: int) -> None:
        self.task_id = str(task_id)
        self.parent_task_id = str(parent_task_id)
        self.execution_ref = str(execution_ref)
        self.attempt = max(1, int(attempt))
        self._lock = RLock()
        self._value: TypedYield | None = None

    @property
    def value(self) -> TypedYield | None:
        with self._lock:
            return copy.deepcopy(self._value)

    def handle(self, call: ToolCall) -> ToolResult:
        try:
            value = self.capture(call.arguments, causation_id=call.tool_call_id)
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=True,
                summary="Terminal subagent yield accepted.",
                output={
                    "accepted": True,
                    "yield_id": value.yield_id,
                    "task_id": value.task_id,
                    "sequence": value.sequence,
                    "kind": value.kind.value,
                },
                metadata={"owner_unit": "M1-S03D-02", "typed_yield": "true"},
            )
        except Exception as error:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="Terminal subagent yield rejected.",
                output={"accepted": False, "error_type": type(error).__name__},
                error=str(error),
                metadata={"owner_unit": "M1-S03D-02", "typed_yield": "true"},
            )

    def capture(self, raw: Mapping[str, Any], *, causation_id: str = "") -> TypedYield:
        if not isinstance(raw, Mapping):
            raise SubagentYieldError("SubagentYield input must be an object")
        kind = YieldKind(str(raw.get("kind") or YieldKind.TERMINAL.value))
        if kind not in {YieldKind.TERMINAL, YieldKind.FAILURE, YieldKind.CANCELLED}:
            raise SubagentYieldError("SubagentYield accepts only a terminal kind")
        summary = str(raw.get("summary") or "").strip()
        if not summary:
            raise SubagentYieldError("SubagentYield requires a non-empty summary")
        value = TypedYield(
            yield_id=str(raw.get("yield_id") or new_id("typedyield")),
            task_id=self.task_id,
            parent_task_id=self.parent_task_id,
            execution_ref=self.execution_ref,
            attempt=self.attempt,
            sequence=1,
            kind=kind,
            summary=summary,
            data=copy.deepcopy(raw.get("data")),
            artifact_refs=tuple(str(item) for item in raw.get("artifact_refs") or ()),
            evidence_refs=tuple(str(item) for item in raw.get("evidence_refs") or ()),
            warnings=tuple(str(item) for item in raw.get("warnings") or ()),
            usage=copy.deepcopy(dict(raw.get("usage") or {})),
            error_code=str(raw.get("error_code") or ""),
            retryable=bool(raw.get("retryable", False)),
            causation_id=causation_id,
            metadata={"source": "SubagentYield tool", "explicit": True},
        )
        with self._lock:
            if self._value is not None:
                if self._value.digest == value.digest:
                    return copy.deepcopy(self._value)
                raise SubagentYieldConflict("terminal subagent yield was already committed")
            self._value = value
            return copy.deepcopy(value)


def subagent_yield_tool_spec() -> ToolSpec:
    provenance = DynamicToolProvenance(
        tool_name=SUBAGENT_YIELD_TOOL_NAME,
        namespace="zyra-subagent-yield",
        version="M1-S03D-02",
        handler_kind="typed-yield",
        external_boundary=False,
        requires_exact_grant=False,
        source="zyra_workers.subagents.subagent_yield",
    )
    return ToolSpec(
        name=SUBAGENT_YIELD_TOOL_NAME,
        purpose="Commit the child's explicit typed terminal result to its parent fan-in contract.",
        source="oh-my-pi task typed yield, internalized by M1-S03D-02",
        input_schema={
            "type": "object",
            "required": ["summary", "data"],
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": ["terminal", "failure", "cancelled"]},
                "summary": {"type": "string", "minLength": 1},
                "data": {},
                "artifact_refs": {"type": "array", "items": {"type": "string"}},
                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                "warnings": {"type": "array", "items": {"type": "string"}},
                "usage": {"type": "object"},
                "error_code": {"type": "string"},
                "retryable": {"type": "boolean"},
            },
        },
        output_schema={
            "type": "object",
            "required": ["accepted", "yield_id", "task_id", "sequence", "kind"],
            "properties": {
                "accepted": {"type": "boolean"},
                "yield_id": {"type": "string"},
                "task_id": {"type": "string"},
                "sequence": {"type": "integer"},
                "kind": {"type": "string"},
            },
        },
        metadata={
            "access_mode": "control",
            "read_only": "false",
            "concurrency_safe": "false",
            "capabilities": "mutation,internal_protocol",
            "logical_child_protocol": "true",
            "requires_exact_grant": "false",
        },
        execution_provenance=provenance,
    )


def bind_subagent_yield_collector(spec: ToolSpec, collector: SubagentYieldCollector) -> SubagentYieldBinding:
    provenance = spec.execution_provenance
    if provenance is None or provenance.namespace != "zyra-subagent-yield":
        raise SubagentYieldError("SubagentYield spec has no canonical execution provenance")
    return SubagentYieldBinding(
        spec=spec,
        handler=ProvenancedDynamicHandler(provenance, collector.handle),
    )


__all__ = [
    "SUBAGENT_YIELD_TOOL_NAME",
    "SubagentYieldBinding",
    "SubagentYieldCollector",
    "SubagentYieldConflict",
    "SubagentYieldError",
    "bind_subagent_yield_collector",
    "subagent_yield_tool_spec",
]
