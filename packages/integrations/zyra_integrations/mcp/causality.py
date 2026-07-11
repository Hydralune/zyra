from __future__ import annotations

"""Causal identities and validation for MCP runtime operations.

The module is deliberately storage-neutral.  It builds immutable causal
records around existing ``EventRecord`` values and validates that MCP config,
connection, capability, permission, tool, resource, prompt, elicitation, task,
auth, instruction, and control events remain attributable to one run/task
partition.  It does not persist events or create another session/event store.
"""

import threading
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from zyra_core import EventRecord, EventType, new_id, now_iso

from .models import JsonValue, redact_value, stable_digest, to_json_value


class McpCausalityError(RuntimeError):
    pass


class McpCausalityValidationError(McpCausalityError):
    def __init__(self, report: "McpCausalityReport") -> None:
        super().__init__("MCP causal graph validation failed")
        self.report = report


class McpOperationKind(StrEnum):
    BOOTSTRAP = "bootstrap"
    CONFIGURE = "configure"
    APPROVE = "approve"
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    RECONNECT = "reconnect"
    REFRESH = "refresh"
    AUTHENTICATE = "authenticate"
    AUTH_REFRESH = "auth_refresh"
    AUTH_REVOKE = "auth_revoke"
    PROJECT_TOOLS = "project_tools"
    PROJECT_RESOURCES = "project_resources"
    PROJECT_PROMPTS = "project_prompts"
    TOOL_PERMISSION = "tool_permission"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    RESOURCE_LIST = "resource_list"
    RESOURCE_READ = "resource_read"
    PROMPT_INVOKE = "prompt_invoke"
    ELICITATION_CREATE = "elicitation_create"
    ELICITATION_RESOLVE = "elicitation_resolve"
    SAMPLING_REQUEST = "sampling_request"
    TASK_CREATE = "task_create"
    TASK_POLL = "task_poll"
    TASK_RESULT = "task_result"
    TASK_CANCEL = "task_cancel"
    INSTRUCTIONS_UPDATE = "instructions_update"
    COMPACT_RESTORE = "compact_restore"
    CONTROL = "control"
    CHECKPOINT = "checkpoint"
    RECOVERY = "recovery"


class McpCausalNodeKind(StrEnum):
    OPERATION = "operation"
    SPAN = "span"
    EVENT = "event"
    PERMISSION = "permission"
    TOOL_CALL = "tool_call"
    ARTIFACT = "artifact"
    CHECKPOINT = "checkpoint"
    TASK = "task"
    ELICITATION = "elicitation"


class McpCausalEdgeKind(StrEnum):
    PARENT = "parent"
    CAUSES = "causes"
    FOLLOWS = "follows"
    AUTHORIZES = "authorizes"
    EXECUTES = "executes"
    PRODUCES = "produces"
    RESTORES = "restores"
    REFRESHES = "refreshes"
    CANCELS = "cancels"
    RETRIES = "retries"
    PROJECTS = "projects"
    REFERENCES = "references"


class McpSpanStatus(StrEnum):
    OPEN = "open"
    OK = "ok"
    FAILED = "failed"
    CANCELLED = "cancelled"


class McpCausalitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class McpCausalityCode(StrEnum):
    PARTITION_MISMATCH = "partition_mismatch"
    IDENTITY_MISSING = "identity_missing"
    DUPLICATE_NODE = "duplicate_node"
    DUPLICATE_EVENT = "duplicate_event"
    EDGE_ENDPOINT_MISSING = "edge_endpoint_missing"
    SELF_EDGE = "self_edge"
    CYCLE = "cycle"
    ORPHAN_EVENT = "orphan_event"
    ORPHAN_SPAN = "orphan_span"
    OPEN_SPAN = "open_span"
    SEQUENCE_REGRESSION = "sequence_regression"
    PARENT_MISMATCH = "parent_mismatch"
    TOOL_PERMISSION_MISSING = "tool_permission_missing"
    TOOL_RESULT_MISSING_CALL = "tool_result_missing_call"
    TASK_TERMINAL_MISSING_CREATE = "task_terminal_missing_create"
    ELICITATION_RESOLUTION_MISSING_REQUEST = "elicitation_resolution_missing_request"
    RESTORE_MISSING_INSTRUCTIONS = "restore_missing_instructions"
    EVENT_PAYLOAD_INVALID = "event_payload_invalid"


@dataclass(frozen=True, slots=True)
class McpOperationContext:
    run_id: str
    task_id: str
    session_id: str
    worker_request_id: str
    operation: McpOperationKind
    operation_id: str = field(default_factory=lambda: new_id("mcpop"))
    node_id: str | None = None
    server_id: str = ""
    tool_call_id: str = ""
    request_id: str = ""
    parent_operation_id: str = ""
    cause_event_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    attempt: int = 1
    sequence: int = 0
    idempotency_key: str = ""
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        required = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "operation_id": self.operation_id,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError("MCP operation context missing: " + ", ".join(missing))
        if not isinstance(self.operation, McpOperationKind):
            object.__setattr__(self, "operation", McpOperationKind(str(self.operation)))
        if self.attempt <= 0:
            raise ValueError("attempt must be positive")
        if self.sequence < 0:
            raise ValueError("sequence cannot be negative")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def partition(self) -> tuple[str, str]:
        return self.run_id, self.task_id

    @property
    def identity_digest(self) -> str:
        return stable_digest(
            {
                "run_id": self.run_id,
                "task_id": self.task_id,
                "session_id": self.session_id,
                "worker_request_id": self.worker_request_id,
                "operation_id": self.operation_id,
                "operation": str(self.operation),
                "server_id": self.server_id,
                "tool_call_id": self.tool_call_id,
                "request_id": self.request_id,
                "attempt": self.attempt,
            }
        )

    def child(
        self,
        operation: McpOperationKind | str,
        *,
        operation_id: str = "",
        server_id: str | None = None,
        tool_call_id: str | None = None,
        request_id: str | None = None,
        span_id: str = "",
        sequence: int | None = None,
        metadata: Mapping[str, JsonValue] | None = None,
    ) -> "McpOperationContext":
        return McpOperationContext(
            run_id=self.run_id,
            task_id=self.task_id,
            session_id=self.session_id,
            worker_request_id=self.worker_request_id,
            operation=McpOperationKind(str(operation)),
            operation_id=operation_id or new_id("mcpop"),
            node_id=self.node_id,
            server_id=self.server_id if server_id is None else server_id,
            tool_call_id=self.tool_call_id if tool_call_id is None else tool_call_id,
            request_id=self.request_id if request_id is None else request_id,
            parent_operation_id=self.operation_id,
            cause_event_id=self.cause_event_id,
            span_id=span_id,
            parent_span_id=self.span_id,
            attempt=1,
            sequence=self.sequence + 1 if sequence is None else sequence,
            idempotency_key=self.idempotency_key,
            metadata={**dict(self.metadata), **dict(metadata or {})},
        )

    def retry(self, *, cause_event_id: str = "") -> "McpOperationContext":
        return replace(
            self,
            operation_id=new_id("mcpop"),
            parent_operation_id=self.operation_id,
            cause_event_id=cause_event_id or self.cause_event_id,
            attempt=self.attempt + 1,
            sequence=self.sequence + 1,
            created_at=now_iso(),
        )

    def with_span(self, span_id: str, *, parent_span_id: str = "") -> "McpOperationContext":
        if not span_id:
            raise ValueError("span_id is required")
        return replace(
            self,
            span_id=span_id,
            parent_span_id=parent_span_id or self.span_id,
        )

    def event_payload(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-operation-context.v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "node_id": self.node_id,
            "operation": str(self.operation),
            "operation_id": self.operation_id,
            "parent_operation_id": self.parent_operation_id,
            "cause_event_id": self.cause_event_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "server_id": self.server_id,
            "tool_call_id": self.tool_call_id,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "sequence": self.sequence,
            "idempotency_key_digest": stable_digest(self.idempotency_key) if self.idempotency_key else "",
            "metadata": redact_value(self.metadata),
            "identity_digest": self.identity_digest,
            "created_at": self.created_at,
        }

    safe_dict = event_payload


@dataclass(frozen=True, slots=True)
class McpCausalSpan:
    span_id: str
    context: McpOperationContext
    name: str
    status: McpSpanStatus = McpSpanStatus.OPEN
    started_at: str = field(default_factory=now_iso)
    completed_at: str = ""
    error_code: str = ""
    error_message: str = ""
    attributes: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.span_id or not self.name:
            raise ValueError("span_id and name are required")
        if not isinstance(self.status, McpSpanStatus):
            object.__setattr__(self, "status", McpSpanStatus(str(self.status)))
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    @property
    def terminal(self) -> bool:
        return self.status is not McpSpanStatus.OPEN

    def finish(
        self,
        status: McpSpanStatus | str = McpSpanStatus.OK,
        *,
        error_code: str = "",
        error_message: str = "",
        attributes: Mapping[str, JsonValue] | None = None,
    ) -> "McpCausalSpan":
        if self.terminal:
            raise McpCausalityError("causal span is already terminal")
        selected = McpSpanStatus(str(status))
        if selected is McpSpanStatus.OPEN:
            raise ValueError("finish status cannot be open")
        return replace(
            self,
            status=selected,
            completed_at=now_iso(),
            error_code=error_code,
            error_message=error_message[:1000],
            attributes={**dict(self.attributes), **dict(attributes or {})},
        )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "status": str(self.status),
            "terminal": self.terminal,
            "context": self.context.safe_dict(),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "attributes": redact_value(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class McpCausalNode:
    node_id: str
    kind: McpCausalNodeKind
    run_id: str
    task_id: str
    operation_id: str = ""
    span_id: str = ""
    event_id: str = ""
    sequence: int = 0
    payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.node_id or not self.run_id or not self.task_id:
            raise ValueError("causal node identity is incomplete")
        if not isinstance(self.kind, McpCausalNodeKind):
            object.__setattr__(self, "kind", McpCausalNodeKind(str(self.kind)))
        if self.sequence < 0:
            raise ValueError("node sequence cannot be negative")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    @property
    def partition(self) -> tuple[str, str]:
        return self.run_id, self.task_id

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "operation_id": self.operation_id,
            "span_id": self.span_id,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "payload": redact_value(self.payload),
        }


@dataclass(frozen=True, slots=True)
class McpCausalEdge:
    source_id: str
    target_id: str
    kind: McpCausalEdgeKind
    reason: str
    metadata: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id or not self.target_id:
            raise ValueError("causal edge endpoints are required")
        if not isinstance(self.kind, McpCausalEdgeKind):
            object.__setattr__(self, "kind", McpCausalEdgeKind(str(self.kind)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    @property
    def identity(self) -> str:
        return stable_digest(
            {
                "source": self.source_id,
                "target": self.target_id,
                "kind": str(self.kind),
                "reason": self.reason,
            }
        )

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "kind": str(self.kind),
            "reason": self.reason,
            "metadata": redact_value(self.metadata),
            "identity": self.identity,
        }


@dataclass(frozen=True, slots=True)
class McpCausalityFinding:
    code: McpCausalityCode
    severity: McpCausalitySeverity
    message: str
    node_ids: tuple[str, ...] = ()
    event_ids: tuple[str, ...] = ()
    evidence: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_ids", tuple(self.node_ids))
        object.__setattr__(self, "event_ids", tuple(self.event_ids))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))

    @property
    def blocking(self) -> bool:
        return self.severity is McpCausalitySeverity.ERROR

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "message": self.message,
            "node_ids": list(self.node_ids),
            "event_ids": list(self.event_ids),
            "evidence": redact_value(self.evidence),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class McpCausalityReport:
    run_id: str
    task_id: str
    findings: tuple[McpCausalityFinding, ...]
    node_count: int
    edge_count: int
    event_count: int
    graph_digest: str
    assessed_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(item.blocking for item in self.findings)

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-causality-report.v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "ok": self.ok,
            "findings": [item.safe_dict() for item in self.findings],
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "event_count": self.event_count,
            "graph_digest": self.graph_digest,
            "assessed_at": self.assessed_at,
        }


class McpCausalGraph:
    """Thread-safe in-memory construction object, never a canonical store."""

    def __init__(self, run_id: str, task_id: str) -> None:
        if not run_id or not task_id:
            raise ValueError("run_id and task_id are required")
        self.run_id = run_id
        self.task_id = task_id
        self._nodes: dict[str, McpCausalNode] = {}
        self._edges: dict[str, McpCausalEdge] = {}
        self._events: dict[str, EventRecord] = {}
        self._spans: dict[str, McpCausalSpan] = {}
        self._lock = threading.RLock()

    def add_context(self, context: McpOperationContext) -> McpCausalNode:
        self._require_partition(context.run_id, context.task_id)
        node = McpCausalNode(
            node_id="operation:" + context.operation_id,
            kind=McpCausalNodeKind.OPERATION,
            run_id=context.run_id,
            task_id=context.task_id,
            operation_id=context.operation_id,
            span_id=context.span_id,
            sequence=context.sequence,
            payload=context.safe_dict(),
        )
        self.add_node(node)
        if context.parent_operation_id:
            self.add_edge(
                McpCausalEdge(
                    "operation:" + context.parent_operation_id,
                    node.node_id,
                    McpCausalEdgeKind.PARENT,
                    "operation parent",
                ),
                allow_missing_source=True,
            )
        return node

    def add_span(self, span: McpCausalSpan) -> McpCausalNode:
        self._require_partition(span.context.run_id, span.context.task_id)
        node = McpCausalNode(
            node_id="span:" + span.span_id,
            kind=McpCausalNodeKind.SPAN,
            run_id=span.context.run_id,
            task_id=span.context.task_id,
            operation_id=span.context.operation_id,
            span_id=span.span_id,
            sequence=span.context.sequence,
            payload=span.safe_dict(),
        )
        with self._lock:
            self._spans[span.span_id] = span
        self.add_node(node, replace_existing=True)
        operation_id = "operation:" + span.context.operation_id
        if operation_id in self._nodes:
            self.add_edge(McpCausalEdge(operation_id, node.node_id, McpCausalEdgeKind.CAUSES, "operation opened span"))
        if span.context.parent_span_id:
            self.add_edge(McpCausalEdge("span:" + span.context.parent_span_id, node.node_id, McpCausalEdgeKind.PARENT, "span parent"), allow_missing_source=True)
        return node

    def add_event(self, event: EventRecord, context: McpOperationContext | None = None) -> McpCausalNode:
        self._require_partition(event.run_id, event.task_id)
        event_id = str(getattr(event, "event_id", "") or "")
        if not event_id:
            raise McpCausalityError("EventRecord requires event_id")
        payload = to_json_value(event.payload)
        if not isinstance(payload, Mapping):
            raise McpCausalityError("EventRecord payload must be a mapping")
        operation_payload = _operation_payload(payload)
        operation_id = context.operation_id if context else str(operation_payload.get("operation_id") or "")
        span_id = context.span_id if context else str(operation_payload.get("span_id") or "")
        sequence = context.sequence if context else _safe_int(operation_payload.get("sequence"), 0)
        node = McpCausalNode(
            node_id="event:" + event_id,
            kind=McpCausalNodeKind.EVENT,
            run_id=event.run_id,
            task_id=event.task_id,
            operation_id=operation_id,
            span_id=span_id,
            event_id=event_id,
            sequence=sequence,
            payload=payload,
        )
        with self._lock:
            if event_id in self._events:
                raise McpCausalityError(f"duplicate MCP event id: {event_id}")
            self._events[event_id] = event
        self.add_node(node)
        if operation_id:
            self.add_edge(McpCausalEdge("operation:" + operation_id, node.node_id, McpCausalEdgeKind.PRODUCES, "operation emitted event"), allow_missing_source=True)
        if span_id:
            self.add_edge(McpCausalEdge("span:" + span_id, node.node_id, McpCausalEdgeKind.PRODUCES, "span emitted event"), allow_missing_source=True)
        cause_event_id = context.cause_event_id if context else str(operation_payload.get("cause_event_id") or "")
        if cause_event_id:
            self.add_edge(McpCausalEdge("event:" + cause_event_id, node.node_id, McpCausalEdgeKind.CAUSES, "event cause"), allow_missing_source=True)
        return node

    def add_node(self, node: McpCausalNode, *, replace_existing: bool = False) -> None:
        self._require_partition(node.run_id, node.task_id)
        with self._lock:
            if node.node_id in self._nodes and not replace_existing:
                raise McpCausalityError(f"duplicate causal node: {node.node_id}")
            self._nodes[node.node_id] = node

    def add_edge(self, edge: McpCausalEdge, *, allow_missing_source: bool = False, allow_missing_target: bool = False) -> None:
        if edge.source_id == edge.target_id:
            raise McpCausalityError("causal self-edge is forbidden")
        with self._lock:
            if not allow_missing_source and edge.source_id not in self._nodes:
                raise McpCausalityError(f"causal edge source is missing: {edge.source_id}")
            if not allow_missing_target and edge.target_id not in self._nodes:
                raise McpCausalityError(f"causal edge target is missing: {edge.target_id}")
            self._edges.setdefault(edge.identity, edge)

    def link_permission(self, permission_event_id: str, tool_call_id: str) -> None:
        self.add_edge(McpCausalEdge("event:" + permission_event_id, "tool_call:" + tool_call_id, McpCausalEdgeKind.AUTHORIZES, "exact permission authorizes tool call"), allow_missing_source=True, allow_missing_target=True)

    def link_tool_result(self, tool_call_id: str, result_event_id: str) -> None:
        self.add_edge(McpCausalEdge("tool_call:" + tool_call_id, "event:" + result_event_id, McpCausalEdgeKind.PRODUCES, "tool call produced result"), allow_missing_source=True, allow_missing_target=True)

    def nodes(self) -> tuple[McpCausalNode, ...]:
        with self._lock:
            return tuple(self._nodes[key] for key in sorted(self._nodes))

    def edges(self) -> tuple[McpCausalEdge, ...]:
        with self._lock:
            return tuple(self._edges[key] for key in sorted(self._edges))

    def events(self) -> tuple[EventRecord, ...]:
        with self._lock:
            return tuple(self._events[key] for key in sorted(self._events))

    def spans(self) -> tuple[McpCausalSpan, ...]:
        with self._lock:
            return tuple(self._spans[key] for key in sorted(self._spans))

    def safe_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": "zyra.mcp-causal-graph.v1",
            "run_id": self.run_id,
            "task_id": self.task_id,
            "nodes": [item.safe_dict() for item in self.nodes()],
            "edges": [item.safe_dict() for item in self.edges()],
            "spans": [item.safe_dict() for item in self.spans()],
            "event_ids": [str(getattr(item, "event_id", "")) for item in self.events()],
            "canonical_store": False,
        }

    @property
    def digest(self) -> str:
        return stable_digest(self.safe_dict())

    def _require_partition(self, run_id: str, task_id: str) -> None:
        if (run_id, task_id) != (self.run_id, self.task_id):
            raise McpCausalityError("causal graph partition mismatch")


class McpCausalityValidator:
    def validate(self, graph: McpCausalGraph, *, raise_on_error: bool = False) -> McpCausalityReport:
        nodes = {item.node_id: item for item in graph.nodes()}
        edges = graph.edges()
        findings: list[McpCausalityFinding] = []
        findings.extend(self._edge_findings(nodes, edges))
        findings.extend(self._cycle_findings(nodes, edges))
        findings.extend(self._span_findings(graph.spans(), nodes))
        findings.extend(self._sequence_findings(nodes, edges))
        findings.extend(self._semantic_findings(nodes, edges))
        report = McpCausalityReport(graph.run_id, graph.task_id, tuple(findings), len(nodes), len(edges), len(graph.events()), graph.digest)
        if raise_on_error and not report.ok:
            raise McpCausalityValidationError(report)
        return report

    def validate_event_chain(self, events: Sequence[EventRecord], *, raise_on_error: bool = False) -> McpCausalityReport:
        if not events:
            report = McpCausalityReport("", "", (), 0, 0, 0, stable_digest([]))
            return report
        graph = McpCausalGraph(events[0].run_id, events[0].task_id)
        for event in events:
            try:
                graph.add_event(event)
            except McpCausalityError as error:
                node = McpCausalNode(new_id("invalid"), McpCausalNodeKind.EVENT, graph.run_id, graph.task_id, payload={"error": str(error)})
                graph.add_node(node)
        return self.validate(graph, raise_on_error=raise_on_error)

    def _edge_findings(self, nodes: Mapping[str, McpCausalNode], edges: Sequence[McpCausalEdge]) -> list[McpCausalityFinding]:
        findings: list[McpCausalityFinding] = []
        for edge in edges:
            missing = tuple(item for item in (edge.source_id, edge.target_id) if item not in nodes)
            if missing:
                findings.append(McpCausalityFinding(McpCausalityCode.EDGE_ENDPOINT_MISSING, McpCausalitySeverity.ERROR, "causal edge endpoint is absent", missing, evidence={"edge": edge.safe_dict()}))
                continue
            if nodes[edge.source_id].partition != nodes[edge.target_id].partition:
                findings.append(McpCausalityFinding(McpCausalityCode.PARTITION_MISMATCH, McpCausalitySeverity.ERROR, "causal edge crosses run/task partition", (edge.source_id, edge.target_id)))
        return findings

    def _cycle_findings(self, nodes: Mapping[str, McpCausalNode], edges: Sequence[McpCausalEdge]) -> list[McpCausalityFinding]:
        adjacency: dict[str, set[str]] = defaultdict(set)
        indegree = {key: 0 for key in nodes}
        for edge in edges:
            if edge.source_id not in nodes or edge.target_id not in nodes:
                continue
            if edge.target_id not in adjacency[edge.source_id]:
                adjacency[edge.source_id].add(edge.target_id)
                indegree[edge.target_id] += 1
        queue = deque(sorted(key for key, value in indegree.items() if value == 0))
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for target in sorted(adjacency[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    queue.append(target)
        if visited == len(nodes):
            return []
        cycle_nodes = tuple(sorted(key for key, value in indegree.items() if value > 0))
        return [McpCausalityFinding(McpCausalityCode.CYCLE, McpCausalitySeverity.ERROR, "MCP causal graph contains a cycle", cycle_nodes)]

    def _span_findings(self, spans: Sequence[McpCausalSpan], nodes: Mapping[str, McpCausalNode]) -> list[McpCausalityFinding]:
        findings: list[McpCausalityFinding] = []
        for span in spans:
            if not span.terminal:
                findings.append(McpCausalityFinding(McpCausalityCode.OPEN_SPAN, McpCausalitySeverity.ERROR, "MCP span remains open", ("span:" + span.span_id,)))
            if span.context.parent_span_id and "span:" + span.context.parent_span_id not in nodes:
                findings.append(McpCausalityFinding(McpCausalityCode.ORPHAN_SPAN, McpCausalitySeverity.ERROR, "MCP parent span is missing", ("span:" + span.span_id, "span:" + span.context.parent_span_id)))
        return findings

    def _sequence_findings(self, nodes: Mapping[str, McpCausalNode], edges: Sequence[McpCausalEdge]) -> list[McpCausalityFinding]:
        findings: list[McpCausalityFinding] = []
        for edge in edges:
            source = nodes.get(edge.source_id)
            target = nodes.get(edge.target_id)
            if source is None or target is None:
                continue
            if edge.kind in {McpCausalEdgeKind.FOLLOWS, McpCausalEdgeKind.CAUSES, McpCausalEdgeKind.PARENT} and source.sequence > target.sequence:
                findings.append(McpCausalityFinding(McpCausalityCode.SEQUENCE_REGRESSION, McpCausalitySeverity.ERROR, "causal edge regresses operation sequence", (source.node_id, target.node_id), evidence={"source_sequence": source.sequence, "target_sequence": target.sequence}))
        return findings

    def _semantic_findings(self, nodes: Mapping[str, McpCausalNode], edges: Sequence[McpCausalEdge]) -> list[McpCausalityFinding]:
        incoming: dict[str, list[McpCausalEdge]] = defaultdict(list)
        for edge in edges:
            incoming[edge.target_id].append(edge)
        findings: list[McpCausalityFinding] = []
        for node in nodes.values():
            operation = str(_operation_payload(node.payload).get("operation") or "")
            if operation == str(McpOperationKind.TOOL_CALL):
                authorized = any(edge.kind is McpCausalEdgeKind.AUTHORIZES for edge in incoming[node.node_id])
                if not authorized:
                    findings.append(McpCausalityFinding(McpCausalityCode.TOOL_PERMISSION_MISSING, McpCausalitySeverity.ERROR, "MCP tool call has no exact permission cause", (node.node_id,)))
            if operation == str(McpOperationKind.TOOL_RESULT):
                produced = any(edge.kind is McpCausalEdgeKind.PRODUCES for edge in incoming[node.node_id])
                if not produced:
                    findings.append(McpCausalityFinding(McpCausalityCode.TOOL_RESULT_MISSING_CALL, McpCausalitySeverity.ERROR, "MCP tool result has no producing call", (node.node_id,)))
            if operation in {str(McpOperationKind.TASK_RESULT), str(McpOperationKind.TASK_CANCEL)}:
                caused = any(edge.kind in {McpCausalEdgeKind.CAUSES, McpCausalEdgeKind.CANCELS, McpCausalEdgeKind.PRODUCES} for edge in incoming[node.node_id])
                if not caused:
                    findings.append(McpCausalityFinding(McpCausalityCode.TASK_TERMINAL_MISSING_CREATE, McpCausalitySeverity.ERROR, "terminal MCP task has no create/poll cause", (node.node_id,)))
            if operation == str(McpOperationKind.ELICITATION_RESOLVE):
                caused = any(edge.kind is McpCausalEdgeKind.CAUSES for edge in incoming[node.node_id])
                if not caused:
                    findings.append(McpCausalityFinding(McpCausalityCode.ELICITATION_RESOLUTION_MISSING_REQUEST, McpCausalitySeverity.ERROR, "elicitation resolution has no request cause", (node.node_id,)))
            if operation == str(McpOperationKind.COMPACT_RESTORE):
                restored = any(edge.kind is McpCausalEdgeKind.RESTORES for edge in incoming[node.node_id])
                if not restored:
                    findings.append(McpCausalityFinding(McpCausalityCode.RESTORE_MISSING_INSTRUCTIONS, McpCausalitySeverity.ERROR, "compact restore has no MCP instructions cause", (node.node_id,)))
        return findings


def event_from_operation(context: McpOperationContext, event_type: EventType, payload: Mapping[str, Any], *, node_id: str | None = None) -> EventRecord:
    selected = to_json_value(payload)
    if not isinstance(selected, Mapping):
        raise ValueError("event payload must be a mapping")
    return EventRecord(run_id=context.run_id, task_id=context.task_id, node_id=node_id or context.node_id, event_type=event_type, payload={**dict(selected), "mcp_causality": context.event_payload()})


def context_from_event(event: EventRecord) -> McpOperationContext | None:
    payload = event.payload if isinstance(event.payload, Mapping) else {}
    raw = payload.get("mcp_causality")
    if not isinstance(raw, Mapping):
        raw = _operation_payload(payload)
    if not isinstance(raw, Mapping) or not raw.get("operation_id"):
        return None
    try:
        return McpOperationContext(run_id=event.run_id, task_id=event.task_id, session_id=str(raw.get("session_id") or ""), worker_request_id=str(raw.get("worker_request_id") or ""), operation=McpOperationKind(str(raw.get("operation") or McpOperationKind.CONTROL)), operation_id=str(raw.get("operation_id") or ""), node_id=event.node_id, server_id=str(raw.get("server_id") or ""), tool_call_id=str(raw.get("tool_call_id") or ""), request_id=str(raw.get("request_id") or ""), parent_operation_id=str(raw.get("parent_operation_id") or ""), cause_event_id=str(raw.get("cause_event_id") or ""), span_id=str(raw.get("span_id") or ""), parent_span_id=str(raw.get("parent_span_id") or ""), attempt=_safe_int(raw.get("attempt"), 1), sequence=_safe_int(raw.get("sequence"), 0), metadata=raw.get("metadata") if isinstance(raw.get("metadata"), Mapping) else {})
    except (TypeError, ValueError):
        return None


def _operation_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("mcp_causality", "mcp_operation", "operation_context"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    runtime = payload.get("mcp_runtime")
    if isinstance(runtime, Mapping):
        for key in ("causality", "operation", "context"):
            value = runtime.get(key)
            if isinstance(value, Mapping):
                return value
    return {}


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = [
    "McpCausalEdge",
    "McpCausalEdgeKind",
    "McpCausalGraph",
    "McpCausalNode",
    "McpCausalNodeKind",
    "McpCausalSpan",
    "McpCausalityCode",
    "McpCausalityError",
    "McpCausalityFinding",
    "McpCausalityReport",
    "McpCausalitySeverity",
    "McpCausalityValidationError",
    "McpCausalityValidator",
    "McpOperationContext",
    "McpOperationKind",
    "McpSpanStatus",
    "context_from_event",
    "event_from_operation",
]
