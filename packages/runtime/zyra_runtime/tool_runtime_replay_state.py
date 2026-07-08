from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable

from .tool_runtime_execution_timeline import ToolExecutionTimelineReport
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_result_context import ToolResultContextReport


class ToolReplayNodeKind(StrEnum):
    MATERIALIZATION = "materialization"
    PLAN = "plan"
    BATCH = "batch"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    CONTEXT_MUTATION = "context_mutation"
    ARTIFACT = "artifact"
    SESSION_APPEND = "session_append"


class ToolReplayTransitionKind(StrEnum):
    MATERIALIZE_TO_PLAN = "materialize_to_plan"
    PLAN_TO_BATCH = "plan_to_batch"
    BATCH_TO_CALL = "batch_to_call"
    CALL_TO_RESULT = "call_to_result"
    RESULT_TO_CONTEXT = "result_to_context"
    RESULT_TO_ARTIFACT = "result_to_artifact"
    RESULT_TO_SESSION = "result_to_session"


class ToolReplayStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    EMPTY = "empty"
    BLOCKED = "blocked"


class ToolReplaySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolReplaySurface(StrEnum):
    MATERIALIZATION = "materialization"
    SETTLEMENT = "settlement"
    RECEIPT = "receipt"
    EVENT_LOG = "event_log"
    TIMELINE = "timeline"
    RESULT_CONTEXT = "result_context"


@dataclass(frozen=True, slots=True)
class ToolReplayNode:
    node_id: str
    kind: ToolReplayNodeKind
    label: str
    tool_call_id: str = ""
    tool_name: str = ""
    ok: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "kind": str(self.kind),
            "label": self.label,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "ok": self.ok,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolReplayTransition:
    transition_id: str
    kind: ToolReplayTransitionKind
    from_node_id: str
    to_node_id: str
    tool_call_id: str = ""
    required: bool = True
    satisfied: bool = True
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.required and not self.satisfied

    def to_dict(self) -> dict[str, Any]:
        return {
            "transition_id": self.transition_id,
            "kind": str(self.kind),
            "from_node_id": self.from_node_id,
            "to_node_id": self.to_node_id,
            "tool_call_id": self.tool_call_id,
            "required": self.required,
            "satisfied": self.satisfied,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolReplayFinding:
    code: str
    severity: ToolReplaySeverity
    surface: ToolReplaySurface
    message: str
    tool_call_id: str = ""
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolReplaySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "tool_call_id": self.tool_call_id,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolReplayStateReport:
    report_id: str
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    nodes: tuple[ToolReplayNode, ...]
    transitions: tuple[ToolReplayTransition, ...]
    findings: tuple[ToolReplayFinding, ...]
    created_at: str = field(default_factory=now_iso)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings) and not any(transition.blocking for transition in self.transitions)

    @property
    def status(self) -> ToolReplayStatus:
        if any(finding.blocking for finding in self.findings) or any(transition.blocking for transition in self.transitions):
            return ToolReplayStatus.BLOCKED
        if not self.nodes:
            return ToolReplayStatus.EMPTY
        if self.findings:
            return ToolReplayStatus.DEGRADED
        return ToolReplayStatus.READY

    @property
    def tool_call_count(self) -> int:
        return len({node.tool_call_id for node in self.nodes if node.tool_call_id})

    @property
    def blocking_transition_count(self) -> int:
        return sum(1 for transition in self.transitions if transition.blocking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.tool_replay_state.v1",
            "report_id": self.report_id,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "status": str(self.status),
            "node_count": len(self.nodes),
            "transition_count": len(self.transitions),
            "tool_call_count": self.tool_call_count,
            "blocking_transition_count": self.blocking_transition_count,
            "nodes": [node.to_dict() for node in self.nodes],
            "transitions": [transition.to_dict() for transition in self.transitions],
            "findings": [finding.to_dict() for finding in self.findings],
            "created_at": self.created_at,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_replay_state_report_id": self.report_id,
            "tool_replay_state_owner_unit": self.owner_unit,
            "tool_replay_state_runtime_id": self.runtime_id,
            "tool_replay_state_ok": str(self.ok).lower(),
            "tool_replay_state_status": str(self.status),
            "tool_replay_state_nodes": str(len(self.nodes)),
            "tool_replay_state_transitions": str(len(self.transitions)),
            "tool_replay_state_tool_calls": str(self.tool_call_count),
            "tool_replay_state_blocking_transitions": str(self.blocking_transition_count),
            "tool_replay_state_findings": str(len(self.findings)),
        }


class ToolReplayStateRuntime:
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
        materialization: Mapping[str, Any],
        settlement_reports: Sequence[Any],
        receipts: Sequence[Mapping[str, Any]],
        event_records: Sequence[EventRecord],
        timeline_report: ToolExecutionTimelineReport | None,
        result_context_report: ToolResultContextReport | None,
    ) -> ToolReplayStateReport:
        nodes: list[ToolReplayNode] = []
        nodes.append(self._materialization_node(materialization))
        nodes.extend(self._settlement_nodes(settlement_reports))
        nodes.extend(self._receipt_nodes(receipts))
        nodes.extend(self._event_nodes(event_records))
        nodes.extend(self._timeline_nodes(timeline_report))
        nodes.extend(self._result_context_nodes(result_context_report))
        nodes = _dedupe_nodes(nodes)
        transitions = self._transitions(nodes, receipts, result_context_report)
        findings = self._findings(nodes, transitions, receipts, materialization)
        return ToolReplayStateReport(
            report_id=new_id("toolreplay"),
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            nodes=tuple(nodes),
            transitions=tuple(transitions),
            findings=tuple(findings),
        )

    def event_for_report(
        self,
        report: ToolReplayStateReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "phase": "tool_replay_state",
                    "tool_replay_state": report.to_dict(),
                }
            },
        )

    def _materialization_node(self, materialization: Mapping[str, Any]) -> ToolReplayNode:
        return ToolReplayNode(
            node_id="materialization",
            kind=ToolReplayNodeKind.MATERIALIZATION,
            label=str(materialization.get("materialization_id") or "materialization"),
            ok=bool(materialization.get("active_tool_names")),
            metadata={"active_tool_count": str(len(materialization.get("active_tool_names") or []))},
        )

    def _settlement_nodes(self, settlement_reports: Sequence[Any]) -> list[ToolReplayNode]:
        nodes: list[ToolReplayNode] = []
        for index, report in enumerate(settlement_reports, start=1):
            payload = report.to_dict() if hasattr(report, "to_dict") else report
            if not isinstance(payload, Mapping):
                continue
            report_id = str(payload.get("report_id") or f"settlement-{index}")
            nodes.append(
                ToolReplayNode(
                    node_id=f"plan:{report_id}",
                    kind=ToolReplayNodeKind.PLAN,
                    label=report_id,
                    ok=payload.get("ok") is not False,
                    metadata={"turn_index": str(payload.get("turn_index") or index), "status": str(payload.get("status") or "")},
                )
            )
            batches = payload.get("batches") if isinstance(payload.get("batches"), Sequence) else ()
            for batch in batches:
                if not isinstance(batch, Mapping):
                    continue
                batch_id = str(batch.get("batch_id") or batch.get("batch_index") or new_id("batch"))
                nodes.append(
                    ToolReplayNode(
                        node_id=f"batch:{batch_id}",
                        kind=ToolReplayNodeKind.BATCH,
                        label=batch_id,
                        ok=batch.get("ok") is not False,
                        metadata={"plan_id": report_id},
                    )
                )
        return nodes

    def _receipt_nodes(self, receipts: Sequence[Mapping[str, Any]]) -> list[ToolReplayNode]:
        nodes: list[ToolReplayNode] = []
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
            tool_name = str(request.get("tool_name") or "")
            nodes.append(
                ToolReplayNode(
                    node_id=f"call:{tool_call_id}",
                    kind=ToolReplayNodeKind.TOOL_CALL,
                    label=tool_name or tool_call_id,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    ok=True,
                    metadata={"step_index": str(request.get("step_index") or "")},
                )
            )
            nodes.append(
                ToolReplayNode(
                    node_id=f"result:{tool_call_id}",
                    kind=ToolReplayNodeKind.TOOL_RESULT,
                    label=f"{tool_name}:{tool_call_id}",
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    ok=result.get("ok") is True,
                    metadata={"error": str(result.get("error") or "")},
                )
            )
            for artifact_id in _artifact_ids(result):
                nodes.append(
                    ToolReplayNode(
                        node_id=f"artifact:{artifact_id}",
                        kind=ToolReplayNodeKind.ARTIFACT,
                        label=artifact_id,
                        tool_call_id=tool_call_id,
                        tool_name=tool_name,
                    )
                )
        return nodes

    def _event_nodes(self, event_records: Sequence[EventRecord]) -> list[ToolReplayNode]:
        nodes: list[ToolReplayNode] = []
        for event in event_records:
            payload = event.payload if isinstance(event.payload, Mapping) else {}
            query = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else {}
            phase = str(query.get("phase") or "")
            if phase == "tool_context_modifier_applied":
                tool_call_id = str(query.get("tool_call_id") or "")
                nodes.append(
                    ToolReplayNode(
                        node_id=f"context:{event.event_id}",
                        kind=ToolReplayNodeKind.CONTEXT_MUTATION,
                        label=phase,
                        tool_call_id=tool_call_id,
                        tool_name=str(query.get("tool_name") or ""),
                        metadata={"event_id": event.event_id},
                    )
                )
            elif phase == "tool_result_session_appended":
                message = query.get("tool_result_message") if isinstance(query.get("tool_result_message"), Mapping) else {}
                tool_call_id = str(message.get("tool_call_id") or "")
                nodes.append(
                    ToolReplayNode(
                        node_id=f"session_append:{event.event_id}",
                        kind=ToolReplayNodeKind.SESSION_APPEND,
                        label=phase,
                        tool_call_id=tool_call_id,
                        tool_name=str(message.get("tool_name") or ""),
                        metadata={"event_id": event.event_id},
                    )
                )
        return nodes

    def _timeline_nodes(self, timeline_report: ToolExecutionTimelineReport | None) -> list[ToolReplayNode]:
        if timeline_report is None:
            return []
        return [
            ToolReplayNode(
                node_id=f"timeline:{timeline_report.report_id}",
                kind=ToolReplayNodeKind.PLAN,
                label="execution_timeline",
                ok=timeline_report.ok,
                metadata={"tool_call_count": str(timeline_report.tool_call_count)},
            )
        ]

    def _result_context_nodes(self, result_context_report: ToolResultContextReport | None) -> list[ToolReplayNode]:
        if result_context_report is None:
            return []
        nodes: list[ToolReplayNode] = []
        for projection in result_context_report.projections:
            nodes.append(
                ToolReplayNode(
                    node_id=f"result_context:{projection.tool_call_id}",
                    kind=ToolReplayNodeKind.SESSION_APPEND,
                    label=projection.projection_id,
                    tool_call_id=projection.tool_call_id,
                    tool_name=projection.tool_name,
                    ok=projection.visible_to_next_turn,
                    metadata={"projection_id": projection.projection_id},
                )
            )
        return nodes

    def _transitions(
        self,
        nodes: Sequence[ToolReplayNode],
        receipts: Sequence[Mapping[str, Any]],
        result_context_report: ToolResultContextReport | None,
    ) -> list[ToolReplayTransition]:
        index = {node.node_id: node for node in nodes}
        transitions: list[ToolReplayTransition] = []
        plan_nodes = [node for node in nodes if node.kind == ToolReplayNodeKind.PLAN]
        for plan in plan_nodes:
            transitions.append(_transition(index.get("materialization"), plan, ToolReplayTransitionKind.MATERIALIZE_TO_PLAN))
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
            result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
            tool_call_id = str(request.get("tool_call_id") or result.get("tool_call_id") or "")
            call = index.get(f"call:{tool_call_id}")
            result_node = index.get(f"result:{tool_call_id}")
            transitions.append(_transition(plan_nodes[0] if plan_nodes else index.get("materialization"), call, ToolReplayTransitionKind.BATCH_TO_CALL, tool_call_id=tool_call_id))
            transitions.append(_transition(call, result_node, ToolReplayTransitionKind.CALL_TO_RESULT, tool_call_id=tool_call_id))
            context_nodes = [node for node in nodes if node.kind == ToolReplayNodeKind.CONTEXT_MUTATION and node.tool_call_id == tool_call_id]
            append_nodes = [node for node in nodes if node.kind == ToolReplayNodeKind.SESSION_APPEND and node.tool_call_id == tool_call_id]
            artifact_nodes = [node for node in nodes if node.kind == ToolReplayNodeKind.ARTIFACT and node.tool_call_id == tool_call_id]
            for context in context_nodes:
                transitions.append(_transition(result_node, context, ToolReplayTransitionKind.RESULT_TO_CONTEXT, tool_call_id=tool_call_id))
            for append in append_nodes:
                transitions.append(_transition(result_node, append, ToolReplayTransitionKind.RESULT_TO_SESSION, tool_call_id=tool_call_id))
            for artifact in artifact_nodes:
                transitions.append(_transition(result_node, artifact, ToolReplayTransitionKind.RESULT_TO_ARTIFACT, tool_call_id=tool_call_id, required=False))
        if result_context_report is not None:
            for projection in result_context_report.projections:
                result_node = index.get(f"result:{projection.tool_call_id}")
                projection_node = index.get(f"result_context:{projection.tool_call_id}")
                transitions.append(_transition(result_node, projection_node, ToolReplayTransitionKind.RESULT_TO_SESSION, tool_call_id=projection.tool_call_id))
        return transitions

    def _findings(
        self,
        nodes: Sequence[ToolReplayNode],
        transitions: Sequence[ToolReplayTransition],
        receipts: Sequence[Mapping[str, Any]],
        materialization: Mapping[str, Any],
    ) -> list[ToolReplayFinding]:
        findings: list[ToolReplayFinding] = []
        if not materialization.get("active_tool_names"):
            findings.append(ToolReplayFinding("TOOL_REPLAY_MATERIALIZATION_EMPTY", ToolReplaySeverity.BLOCKER, ToolReplaySurface.MATERIALIZATION, "Tool replay cannot start without materialized active tools."))
        if receipts and not any(node.kind == ToolReplayNodeKind.TOOL_RESULT for node in nodes):
            findings.append(ToolReplayFinding("TOOL_REPLAY_RESULT_NODE_MISSING", ToolReplaySeverity.BLOCKER, ToolReplaySurface.RECEIPT, "Receipts exist but replay result nodes are missing."))
        for transition in transitions:
            if transition.blocking:
                findings.append(
                    ToolReplayFinding(
                        code="TOOL_REPLAY_TRANSITION_MISSING",
                        severity=ToolReplaySeverity.BLOCKER,
                        surface=ToolReplaySurface.EVENT_LOG,
                        message=f"Replay transition is not satisfied: {transition.kind}.",
                        tool_call_id=transition.tool_call_id,
                        metadata={"transition_id": transition.transition_id},
                    )
                )
        return findings


def tool_replay_state_metadata(report: ToolReplayStateReport | None) -> dict[str, str]:
    if report is None:
        return {"tool_replay_state_ok": "false", "tool_replay_state_nodes": "0"}
    return report.metadata()


def assert_tool_replay_state_ready(report: ToolReplayStateReport) -> None:
    if report.ok:
        return
    blockers = ", ".join(finding.code for finding in report.findings if finding.blocking)
    raise AssertionError(f"tool replay state blocked: {blockers or 'unknown'}")


def render_tool_replay_state_markdown(report: ToolReplayStateReport) -> str:
    lines = [
        "## Tool Replay State",
        "",
        f"- status: `{report.status}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- nodes: `{len(report.nodes)}`",
        f"- transitions: `{len(report.transitions)}`",
        f"- tool_calls: `{report.tool_call_count}`",
        "",
        "### Findings",
        "",
    ]
    if report.findings:
        lines.extend(f"- `{finding.code}` [{finding.severity}]: {finding.message}" for finding in report.findings)
    else:
        lines.append("- no findings")
    return "\n".join(lines)


def _dedupe_nodes(nodes: Sequence[ToolReplayNode]) -> list[ToolReplayNode]:
    seen: set[str] = set()
    output: list[ToolReplayNode] = []
    for node in nodes:
        if node.node_id in seen:
            continue
        seen.add(node.node_id)
        output.append(node)
    return output


def _artifact_ids(result: Mapping[str, Any]) -> list[str]:
    ids: list[str] = []
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), Sequence) else ()
    for artifact in artifacts:
        if isinstance(artifact, Mapping):
            artifact_id = str(artifact.get("artifact_id") or "")
        else:
            artifact_id = str(getattr(artifact, "artifact_id", "") or "")
        if artifact_id and artifact_id not in ids:
            ids.append(artifact_id)
    output = result.get("output") if isinstance(result.get("output"), Mapping) else {}
    artifact_id = str(output.get("full_output_artifact_id") or "")
    if artifact_id and artifact_id not in ids:
        ids.append(artifact_id)
    return ids


def _transition(
    before: ToolReplayNode | None,
    after: ToolReplayNode | None,
    kind: ToolReplayTransitionKind,
    *,
    tool_call_id: str = "",
    required: bool = True,
) -> ToolReplayTransition:
    return ToolReplayTransition(
        transition_id=new_id("toolreplaytrans"),
        kind=kind,
        from_node_id=before.node_id if before else "",
        to_node_id=after.node_id if after else "",
        tool_call_id=tool_call_id,
        required=required,
        satisfied=before is not None and after is not None,
    )
