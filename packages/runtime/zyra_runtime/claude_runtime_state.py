from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping

from zyra_core import ArtifactRef, new_id, now_iso, to_jsonable


class ClaudeRuntimeStateScope(StrEnum):
    SESSION = "session"
    TURN = "turn"
    TOOL = "tool"
    CONTEXT = "context"
    ARTIFACT = "artifact"
    CONTROL = "control"
    PERMISSION = "permission"
    WORKER = "worker"


class ClaudeRuntimeMutationKind(StrEnum):
    CREATED = "created"
    UPDATED = "updated"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPACTED = "compacted"
    EXTERNALIZED = "externalized"
    SNAPSHOTTED = "snapshotted"
    RESTORED = "restored"
    BLOCKED = "blocked"
    EMITTED = "emitted"


class ClaudeRuntimeCausalityStatus(StrEnum):
    OK = "ok"
    ORPHAN = "orphan"
    MISSING_PARENT = "missing_parent"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeStateMutation:
    mutation_id: str
    scope: ClaudeRuntimeStateScope
    kind: ClaudeRuntimeMutationKind
    subject_id: str
    summary: str
    parent_id: str = ""
    event_phase: str = ""
    artifact_ids: list[str] = field(default_factory=list)
    tool_call_id: str = ""
    turn_id: str = ""
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeCausalityEdge:
    edge_id: str
    parent_id: str
    child_id: str
    relation: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeCausalityFinding:
    status: ClaudeRuntimeCausalityStatus
    subject_id: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status in {ClaudeRuntimeCausalityStatus.ORPHAN, ClaudeRuntimeCausalityStatus.MISSING_PARENT}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeRuntimeStateReport:
    runtime_id: str
    session_id: str
    mutation_count: int
    edge_count: int
    artifact_count: int
    tool_mutation_count: int
    context_mutation_count: int
    control_mutation_count: int
    findings: list[ClaudeRuntimeCausalityFinding]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not any(finding.blocking for finding in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.runtime_state_report.v1",
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "ok": self.ok,
            "mutation_count": self.mutation_count,
            "edge_count": self.edge_count,
            "artifact_count": self.artifact_count,
            "tool_mutation_count": self.tool_mutation_count,
            "context_mutation_count": self.context_mutation_count,
            "control_mutation_count": self.control_mutation_count,
            "findings": [item.to_dict() for item in self.findings],
            "metadata": to_jsonable(self.metadata),
        }


class ClaudeRuntimeStateLedger:
    """Captures state custody and causality for the productized QueryEngine path."""

    def __init__(self, *, runtime_id: str, runtime_source: str, session_id: str = "") -> None:
        self.runtime_id = runtime_id
        self.runtime_source = runtime_source
        self.session_id = session_id
        self._mutations: list[ClaudeRuntimeStateMutation] = []
        self._edges: list[ClaudeRuntimeCausalityEdge] = []

    @property
    def mutations(self) -> list[ClaudeRuntimeStateMutation]:
        return list(self._mutations)

    @property
    def edges(self) -> list[ClaudeRuntimeCausalityEdge]:
        return list(self._edges)

    def record(
        self,
        *,
        scope: ClaudeRuntimeStateScope,
        kind: ClaudeRuntimeMutationKind,
        subject_id: str,
        summary: str,
        parent_id: str = "",
        event_phase: str = "",
        artifact_ids: Iterable[str] = (),
        tool_call_id: str = "",
        turn_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> ClaudeRuntimeStateMutation:
        mutation = ClaudeRuntimeStateMutation(
            mutation_id=new_id("mutation"),
            scope=scope,
            kind=kind,
            subject_id=subject_id,
            summary=summary,
            parent_id=parent_id,
            event_phase=event_phase,
            artifact_ids=[str(item) for item in artifact_ids],
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            metadata={
                "runtime_id": self.runtime_id,
                "runtime_source": self.runtime_source,
                **dict(metadata or {}),
            },
        )
        self._mutations.append(mutation)
        if parent_id:
            self._edges.append(
                ClaudeRuntimeCausalityEdge(
                    edge_id=new_id("edge"),
                    parent_id=parent_id,
                    child_id=mutation.mutation_id,
                    relation="caused",
                    metadata={"subject_id": subject_id, "scope": str(scope)},
                )
            )
        return mutation

    def record_session_started(self, session_id: str, *, worker_request_id: str) -> ClaudeRuntimeStateMutation:
        self.session_id = session_id
        return self.record(
            scope=ClaudeRuntimeStateScope.SESSION,
            kind=ClaudeRuntimeMutationKind.CREATED,
            subject_id=session_id,
            summary="query session started",
            event_phase="session_started",
            metadata={"worker_request_id": worker_request_id},
        )

    def record_turn(self, turn_id: str, *, turn_index: int, completed: bool = False, ok: bool = True) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.TURN,
            kind=ClaudeRuntimeMutationKind.COMPLETED if completed else ClaudeRuntimeMutationKind.CREATED,
            subject_id=turn_id,
            summary="query turn completed" if completed else "query turn started",
            parent_id=self._latest_session_mutation_id(),
            event_phase="turn_completed" if completed else "turn_started",
            turn_id=turn_id,
            metadata={"turn_index": turn_index, "ok": ok},
        )

    def record_tool_result(
        self,
        *,
        tool_call_id: str,
        tool_name: str,
        ok: bool,
        artifact_ids: Iterable[str] = (),
        turn_id: str = "",
        error: str | None = None,
    ) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.TOOL,
            kind=ClaudeRuntimeMutationKind.COMPLETED if ok else ClaudeRuntimeMutationKind.FAILED,
            subject_id=tool_call_id,
            summary=f"tool {tool_name} {'completed' if ok else 'failed'}",
            parent_id=self._latest_turn_mutation_id(turn_id),
            event_phase="tool_call_completed",
            artifact_ids=artifact_ids,
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            metadata={"tool_name": tool_name, "error": error},
        )

    def record_context_compaction(self, *, artifact: ArtifactRef, before_chars: int, after_chars: int) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.CONTEXT,
            kind=ClaudeRuntimeMutationKind.COMPACTED,
            subject_id=artifact.artifact_id,
            summary="context window compacted",
            parent_id=self._latest_session_mutation_id(),
            event_phase="context_compacted",
            artifact_ids=[artifact.artifact_id],
            metadata={"before_chars": before_chars, "after_chars": after_chars, "uri": artifact.uri},
        )

    def record_session_artifacts(self, *, snapshot_artifact: ArtifactRef, transcript_artifact: ArtifactRef, resume_artifact: ArtifactRef | None = None) -> ClaudeRuntimeStateMutation:
        artifact_ids = [snapshot_artifact.artifact_id, transcript_artifact.artifact_id]
        if resume_artifact is not None:
            artifact_ids.append(resume_artifact.artifact_id)
        return self.record(
            scope=ClaudeRuntimeStateScope.SESSION,
            kind=ClaudeRuntimeMutationKind.SNAPSHOTTED,
            subject_id=self.session_id or snapshot_artifact.artifact_id,
            summary="query session snapshot and transcript written",
            parent_id=self._latest_session_mutation_id(),
            event_phase="query_session_snapshot",
            artifact_ids=artifact_ids,
            metadata={
                "snapshot_artifact_id": snapshot_artifact.artifact_id,
                "transcript_artifact_id": transcript_artifact.artifact_id,
                "resume_artifact_id": resume_artifact.artifact_id if resume_artifact else "",
            },
        )

    def record_control_result(self, *, command_id: str, command_name: str, ok: bool, artifact: ArtifactRef | None = None) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.CONTROL,
            kind=ClaudeRuntimeMutationKind.COMPLETED if ok else ClaudeRuntimeMutationKind.FAILED,
            subject_id=command_id,
            summary=f"control command {command_name} {'completed' if ok else 'failed'}",
            parent_id=self._latest_session_mutation_id(),
            event_phase="control_command",
            artifact_ids=[artifact.artifact_id] if artifact else [],
            metadata={"command_name": command_name},
        )

    def record_permission_decision(
        self,
        *,
        subject_id: str,
        allowed: bool,
        operation: str,
        reason: str,
        tool_call_id: str = "",
        turn_id: str = "",
    ) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.PERMISSION,
            kind=ClaudeRuntimeMutationKind.COMPLETED if allowed else ClaudeRuntimeMutationKind.BLOCKED,
            subject_id=subject_id,
            summary=f"permission {operation} {'allowed' if allowed else 'blocked'}",
            parent_id=self._latest_turn_mutation_id(turn_id),
            event_phase="tool_permission_decision",
            tool_call_id=tool_call_id,
            turn_id=turn_id,
            metadata={"operation": operation, "reason": reason, "allowed": allowed},
        )

    def record_worker_result(
        self,
        *,
        request_id: str,
        ok: bool,
        artifact_ids: Iterable[str] = (),
        error: str | None = None,
    ) -> ClaudeRuntimeStateMutation:
        return self.record(
            scope=ClaudeRuntimeStateScope.WORKER,
            kind=ClaudeRuntimeMutationKind.COMPLETED if ok else ClaudeRuntimeMutationKind.FAILED,
            subject_id=request_id,
            summary="worker result completed" if ok else "worker result failed",
            parent_id=self._latest_session_mutation_id(),
            event_phase="worker_result",
            artifact_ids=artifact_ids,
            metadata={"error": error},
        )

    def artifact_custody(self) -> dict[str, dict[str, Any]]:
        custody: dict[str, dict[str, Any]] = {}
        for mutation in self._mutations:
            for artifact_id in mutation.artifact_ids:
                custody[artifact_id] = {
                    "artifact_id": artifact_id,
                    "owner_scope": str(mutation.scope),
                    "owner_mutation_id": mutation.mutation_id,
                    "subject_id": mutation.subject_id,
                    "event_phase": mutation.event_phase,
                    "created_at": mutation.created_at,
                }
        return custody

    def state_custody_map(self) -> dict[str, str]:
        custody: dict[str, str] = {}
        for mutation in self._mutations:
            key = f"{mutation.scope}:{mutation.subject_id}"
            custody[key] = mutation.mutation_id
        return custody

    def report(self) -> ClaudeRuntimeStateReport:
        findings = self._causality_findings()
        artifacts = {
            artifact_id
            for mutation in self._mutations
            for artifact_id in mutation.artifact_ids
            if artifact_id
        }
        return ClaudeRuntimeStateReport(
            runtime_id=self.runtime_id,
            session_id=self.session_id,
            mutation_count=len(self._mutations),
            edge_count=len(self._edges),
            artifact_count=len(artifacts),
            tool_mutation_count=sum(1 for item in self._mutations if item.scope == ClaudeRuntimeStateScope.TOOL),
            context_mutation_count=sum(1 for item in self._mutations if item.scope == ClaudeRuntimeStateScope.CONTEXT),
            control_mutation_count=sum(1 for item in self._mutations if item.scope == ClaudeRuntimeStateScope.CONTROL),
            findings=findings,
            metadata={"runtime_source": self.runtime_source},
        )

    def metadata(self) -> dict[str, str]:
        report = self.report()
        return {
            "runtime_state_ok": str(report.ok).lower(),
            "runtime_state_mutations": str(report.mutation_count),
            "runtime_state_edges": str(report.edge_count),
            "runtime_state_artifacts": str(report.artifact_count),
            "runtime_state_tool_mutations": str(report.tool_mutation_count),
            "runtime_state_context_mutations": str(report.context_mutation_count),
            "runtime_state_control_mutations": str(report.control_mutation_count),
            "runtime_state_findings": str(len(report.findings)),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.runtime_state_ledger.v1",
            "runtime_id": self.runtime_id,
            "runtime_source": self.runtime_source,
            "session_id": self.session_id,
            "mutations": [item.to_dict() for item in self._mutations],
            "edges": [item.to_dict() for item in self._edges],
            "artifact_custody": self.artifact_custody(),
            "state_custody": self.state_custody_map(),
            "report": self.report().to_dict(),
        }

    def _causality_findings(self) -> list[ClaudeRuntimeCausalityFinding]:
        findings: list[ClaudeRuntimeCausalityFinding] = []
        mutation_ids = {mutation.mutation_id for mutation in self._mutations}
        subject_counts: dict[str, int] = {}
        for mutation in self._mutations:
            subject_counts[mutation.subject_id] = subject_counts.get(mutation.subject_id, 0) + 1
            if mutation.parent_id and not any(edge.parent_id == mutation.parent_id for edge in self._edges):
                findings.append(
                    ClaudeRuntimeCausalityFinding(
                        status=ClaudeRuntimeCausalityStatus.MISSING_PARENT,
                        subject_id=mutation.subject_id,
                        message=f"mutation {mutation.mutation_id} references missing parent edge",
                    )
                )
        for edge in self._edges:
            if edge.child_id not in mutation_ids:
                findings.append(
                    ClaudeRuntimeCausalityFinding(
                        status=ClaudeRuntimeCausalityStatus.ORPHAN,
                        subject_id=edge.child_id,
                        message=f"edge {edge.edge_id} references missing child mutation",
                    )
                )
        for subject_id, count in subject_counts.items():
            if subject_id and count > 8:
                findings.append(
                    ClaudeRuntimeCausalityFinding(
                        status=ClaudeRuntimeCausalityStatus.DUPLICATE,
                        subject_id=subject_id,
                        message=f"subject has {count} mutations",
                        metadata={"count": count},
                    )
                )
        return findings

    def _latest_session_mutation_id(self) -> str:
        for mutation in reversed(self._mutations):
            if mutation.scope == ClaudeRuntimeStateScope.SESSION:
                return mutation.mutation_id
        return ""

    def _latest_turn_mutation_id(self, turn_id: str = "") -> str:
        for mutation in reversed(self._mutations):
            if mutation.scope == ClaudeRuntimeStateScope.TURN and (not turn_id or mutation.turn_id == turn_id):
                return mutation.mutation_id
        return self._latest_session_mutation_id()


def runtime_state_metadata_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, str]:
    report = snapshot.get("report") if isinstance(snapshot.get("report"), Mapping) else {}
    return {
        "runtime_state_ok": str(report.get("ok") is True).lower(),
        "runtime_state_mutations": str(report.get("mutation_count") or 0),
        "runtime_state_edges": str(report.get("edge_count") or 0),
        "runtime_state_artifacts": str(report.get("artifact_count") or 0),
    }


def runtime_state_custody_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, str]:
    custody = snapshot.get("state_custody")
    if isinstance(custody, Mapping):
        return {str(key): str(value) for key, value in custody.items()}
    return {}


def runtime_artifact_custody_from_snapshot(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    custody = snapshot.get("artifact_custody")
    if not isinstance(custody, Mapping):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for artifact_id, payload in custody.items():
        if isinstance(payload, Mapping):
            normalized[str(artifact_id)] = dict(payload)
    return normalized


def merge_runtime_state_metadata(*metadata_maps: Mapping[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    numeric_keys = {
        "runtime_state_mutations",
        "runtime_state_edges",
        "runtime_state_artifacts",
        "runtime_state_tool_mutations",
        "runtime_state_context_mutations",
        "runtime_state_control_mutations",
        "runtime_state_findings",
    }
    for metadata in metadata_maps:
        for key, value in metadata.items():
            if key in numeric_keys:
                try:
                    merged[key] = str(int(merged.get(key, "0")) + int(value))
                except ValueError:
                    merged[key] = str(value)
            elif key == "runtime_state_ok":
                merged[key] = str(merged.get(key, "true") == "true" and value == "true").lower()
            else:
                merged[key] = str(value)
    return merged
