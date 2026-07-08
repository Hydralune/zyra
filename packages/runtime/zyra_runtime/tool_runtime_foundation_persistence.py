from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .tool_runtime_foundation import TOOL_LOOP_FOUNDATION_OWNER_UNIT, TOOL_LOOP_FOUNDATION_RUNTIME_ID
from .tool_runtime_foundation_audit import ToolFoundationAuditReport


class ToolFoundationArtifactKind(StrEnum):
    MATERIALIZATION = "materialization"
    RECEIPT_LOG = "receipt_log"
    CONTEXT_SNAPSHOT = "context_snapshot"
    REPLAY_REPORT = "replay_report"
    EXTERNALIZED_OUTPUT_INDEX = "externalized_output_index"


class ToolFoundationReplayStatus(StrEnum):
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"


class ToolFoundationReplayFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ToolFoundationReplaySurface(StrEnum):
    MATERIALIZATION = "materialization"
    RECEIPTS = "receipts"
    CONTEXT = "context"
    ARTIFACTS = "artifacts"
    SOURCE_LEDGER = "source_ledger"
    BUDGET = "budget"


@dataclass(frozen=True, slots=True)
class ToolFoundationPersistedArtifact:
    artifact: ArtifactRef
    kind: ToolFoundationArtifactKind
    record_count: int
    byte_size: int
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact": to_jsonable(self.artifact),
            "kind": str(self.kind),
            "record_count": self.record_count,
            "byte_size": self.byte_size,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationArtifactSet:
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    materialization_artifact: ToolFoundationPersistedArtifact | None = None
    receipt_log_artifact: ToolFoundationPersistedArtifact | None = None
    context_snapshot_artifact: ToolFoundationPersistedArtifact | None = None
    replay_report_artifact: ToolFoundationPersistedArtifact | None = None
    externalized_output_index_artifact: ToolFoundationPersistedArtifact | None = None

    @property
    def artifacts(self) -> tuple[ArtifactRef, ...]:
        refs: list[ArtifactRef] = []
        for item in [
            self.materialization_artifact,
            self.receipt_log_artifact,
            self.context_snapshot_artifact,
            self.replay_report_artifact,
            self.externalized_output_index_artifact,
        ]:
            if item is not None:
                refs.append(item.artifact)
        return tuple(refs)

    @property
    def persisted_count(self) -> int:
        return len(self.artifacts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "materialization_artifact": self.materialization_artifact.to_dict() if self.materialization_artifact else None,
            "receipt_log_artifact": self.receipt_log_artifact.to_dict() if self.receipt_log_artifact else None,
            "context_snapshot_artifact": self.context_snapshot_artifact.to_dict() if self.context_snapshot_artifact else None,
            "replay_report_artifact": self.replay_report_artifact.to_dict() if self.replay_report_artifact else None,
            "externalized_output_index_artifact": (
                self.externalized_output_index_artifact.to_dict() if self.externalized_output_index_artifact else None
            ),
            "persisted_count": self.persisted_count,
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_foundation_persistence_owner_unit": self.owner_unit,
            "tool_foundation_persistence_runtime_id": self.runtime_id,
            "tool_foundation_persisted_artifacts": str(self.persisted_count),
            "tool_foundation_materialization_artifact_id": _artifact_id(self.materialization_artifact),
            "tool_foundation_receipt_log_artifact_id": _artifact_id(self.receipt_log_artifact),
            "tool_foundation_context_snapshot_artifact_id": _artifact_id(self.context_snapshot_artifact),
            "tool_foundation_replay_report_artifact_id": _artifact_id(self.replay_report_artifact),
            "tool_foundation_externalized_output_index_artifact_id": _artifact_id(self.externalized_output_index_artifact),
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationExternalizedOutputRecord:
    tool_call_id: str
    tool_name: str
    artifact_id: str
    artifact_uri: str
    reason: str
    original_chars: int
    budget_chars: int
    turn_index: int
    step_index: int

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ToolFoundationReplayFinding:
    code: str
    severity: ToolFoundationReplayFindingSeverity
    surface: ToolFoundationReplaySurface
    message: str
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == ToolFoundationReplayFindingSeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ToolFoundationReplayReport:
    report_id: str
    status: ToolFoundationReplayStatus
    owner_unit: str
    runtime_id: str
    session_id: str
    worker_request_id: str
    materialization_id: str
    active_tool_count: int
    receipt_count: int
    context_turn_count: int
    externalized_output_count: int
    source_repo_count: int
    checked_at: str = field(default_factory=now_iso)
    findings: tuple[ToolFoundationReplayFinding, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status != ToolFoundationReplayStatus.FAILED and not any(item.blocking for item in self.findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "materialization_id": self.materialization_id,
            "active_tool_count": self.active_tool_count,
            "receipt_count": self.receipt_count,
            "context_turn_count": self.context_turn_count,
            "externalized_output_count": self.externalized_output_count,
            "source_repo_count": self.source_repo_count,
            "checked_at": self.checked_at,
            "findings": [finding.to_dict() for finding in self.findings],
        }

    def metadata(self) -> dict[str, str]:
        return {
            "tool_foundation_replay_report_id": self.report_id,
            "tool_foundation_replay_status": str(self.status),
            "tool_foundation_replay_ok": str(self.ok).lower(),
            "tool_foundation_replay_receipts": str(self.receipt_count),
            "tool_foundation_replay_context_turns": str(self.context_turn_count),
            "tool_foundation_replay_externalized_outputs": str(self.externalized_output_count),
            "tool_foundation_replay_findings": str(len(self.findings)),
        }


class ToolFoundationPersistenceRuntime:
    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        owner_unit: str = TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        runtime_id: str = TOOL_LOOP_FOUNDATION_RUNTIME_ID,
    ) -> None:
        self.artifact_store = artifact_store
        self.owner_unit = owner_unit
        self.runtime_id = runtime_id

    def persist_materialization(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        materialization: Mapping[str, Any],
    ) -> ToolFoundationPersistedArtifact:
        payload = {
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "kind": str(ToolFoundationArtifactKind.MATERIALIZATION),
            "materialization": to_jsonable(materialization),
            "created_at": now_iso(),
        }
        return self._write_json_artifact(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            payload=payload,
            title="Tool registry materialization",
            kind=ToolFoundationArtifactKind.MATERIALIZATION,
            record_count=len(materialization.get("entries") or []),
        )

    def persist_final_state(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        materialization: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
        audit_report: ToolFoundationAuditReport | Mapping[str, Any],
    ) -> ToolFoundationArtifactSet:
        receipt_artifact = self.persist_receipt_log(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            receipts=receipts,
        )
        context_artifact = self.persist_context_snapshot(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            context_snapshots=context_snapshots,
        )
        output_index = self.persist_externalized_output_index(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            receipts=receipts,
        )
        replay_report = self.build_replay_report(
            materialization=materialization,
            receipts=receipts,
            context_snapshots=context_snapshots,
            externalized_outputs=_externalized_output_records(receipts),
        )
        replay_artifact = self.persist_replay_report(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            report=replay_report,
            audit_report=audit_report,
        )
        materialization_artifact = self.persist_materialization(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            materialization=materialization,
        )
        return ToolFoundationArtifactSet(
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=session_id,
            worker_request_id=worker_request_id,
            materialization_artifact=materialization_artifact,
            receipt_log_artifact=receipt_artifact,
            context_snapshot_artifact=context_artifact,
            replay_report_artifact=replay_artifact,
            externalized_output_index_artifact=output_index,
        )

    def persist_receipt_log(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolFoundationPersistedArtifact:
        lines = []
        for index, receipt in enumerate(receipts, start=1):
            lines.append(
                json.dumps(
                    {
                        "owner_unit": self.owner_unit,
                        "runtime_id": self.runtime_id,
                        "session_id": session_id,
                        "worker_request_id": worker_request_id,
                        "receipt_index": index,
                        "receipt": to_jsonable(receipt),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
        content = "\n".join(lines)
        artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=content,
            title="Tool execution receipt log",
            kind=ArtifactKind.TRACE,
            extension=".jsonl",
            producer_node_id=node_id,
        )
        return ToolFoundationPersistedArtifact(
            artifact=artifact,
            kind=ToolFoundationArtifactKind.RECEIPT_LOG,
            record_count=len(receipts),
            byte_size=len(content.encode("utf-8")),
            metadata={"session_id": session_id, "worker_request_id": worker_request_id},
        )

    def persist_context_snapshot(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        context_snapshots: Sequence[Mapping[str, Any]],
    ) -> ToolFoundationPersistedArtifact:
        payload = {
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "kind": str(ToolFoundationArtifactKind.CONTEXT_SNAPSHOT),
            "session_id": session_id,
            "worker_request_id": worker_request_id,
            "turn_count": len(context_snapshots),
            "snapshots": to_jsonable(context_snapshots),
            "created_at": now_iso(),
        }
        return self._write_json_artifact(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            payload=payload,
            title="ToolUseContext snapshots",
            kind=ToolFoundationArtifactKind.CONTEXT_SNAPSHOT,
            record_count=len(context_snapshots),
        )

    def persist_externalized_output_index(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        session_id: str,
        worker_request_id: str,
        receipts: Sequence[Mapping[str, Any]],
    ) -> ToolFoundationPersistedArtifact:
        records = _externalized_output_records(receipts)
        payload = {
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "kind": str(ToolFoundationArtifactKind.EXTERNALIZED_OUTPUT_INDEX),
            "session_id": session_id,
            "worker_request_id": worker_request_id,
            "externalized_output_count": len(records),
            "records": [record.to_dict() for record in records],
            "created_at": now_iso(),
        }
        return self._write_json_artifact(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            payload=payload,
            title="Tool externalized output index",
            kind=ToolFoundationArtifactKind.EXTERNALIZED_OUTPUT_INDEX,
            record_count=len(records),
        )

    def persist_replay_report(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        report: ToolFoundationReplayReport,
        audit_report: ToolFoundationAuditReport | Mapping[str, Any],
    ) -> ToolFoundationPersistedArtifact:
        payload = {
            "owner_unit": self.owner_unit,
            "runtime_id": self.runtime_id,
            "kind": str(ToolFoundationArtifactKind.REPLAY_REPORT),
            "replay_report": report.to_dict(),
            "audit_report": audit_report.to_dict() if hasattr(audit_report, "to_dict") else to_jsonable(audit_report),
            "created_at": now_iso(),
        }
        return self._write_json_artifact(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            payload=payload,
            title="Tool foundation replay report",
            kind=ToolFoundationArtifactKind.REPLAY_REPORT,
            record_count=len(report.findings),
        )

    def build_replay_report(
        self,
        *,
        materialization: Mapping[str, Any],
        receipts: Sequence[Mapping[str, Any]],
        context_snapshots: Sequence[Mapping[str, Any]],
        externalized_outputs: Sequence[ToolFoundationExternalizedOutputRecord],
    ) -> ToolFoundationReplayReport:
        findings = [
            *self._materialization_findings(materialization),
            *self._receipt_findings(receipts),
            *self._context_findings(context_snapshots, receipts),
            *self._externalized_output_findings(externalized_outputs),
        ]
        status = _replay_status(findings)
        source_repos = {
            str(row.get("source_repo") or "")
            for row in materialization.get("source_ledger", [])
            if isinstance(row, Mapping) and row.get("source_repo")
        }
        return ToolFoundationReplayReport(
            report_id=new_id("toolreplay"),
            status=status,
            owner_unit=self.owner_unit,
            runtime_id=self.runtime_id,
            session_id=str(materialization.get("session_id") or ""),
            worker_request_id=str(materialization.get("worker_request_id") or ""),
            materialization_id=str(materialization.get("materialization_id") or ""),
            active_tool_count=len(materialization.get("active_tool_names") or []),
            receipt_count=len(receipts),
            context_turn_count=len(context_snapshots),
            externalized_output_count=len(externalized_outputs),
            source_repo_count=len(source_repos),
            findings=tuple(findings),
        )

    def replay_receipt_log(self, artifact: ArtifactRef) -> list[dict[str, Any]]:
        path = self.artifact_store.resolve_path(artifact)
        if not path.exists():
            raise FileNotFoundError(path)
        receipts: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            receipt = data.get("receipt") if isinstance(data, Mapping) else None
            if isinstance(receipt, Mapping):
                receipts.append(dict(receipt))
        return receipts

    def replay_json_artifact(self, artifact: ArtifactRef) -> dict[str, Any]:
        path = self.artifact_store.resolve_path(artifact)
        if not path.exists():
            raise FileNotFoundError(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, Mapping) else {"value": data}

    def event_for_artifacts(
        self,
        artifact_set: ToolFoundationArtifactSet,
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
                    "session_id": artifact_set.session_id,
                    "worker_request_id": artifact_set.worker_request_id,
                    "phase": "tool_foundation_persisted",
                    "artifact_set": artifact_set.to_dict(),
                }
            },
        )

    def _write_json_artifact(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
        payload: Mapping[str, Any],
        title: str,
        kind: ToolFoundationArtifactKind,
        record_count: int,
    ) -> ToolFoundationPersistedArtifact:
        content = json.dumps(to_jsonable(payload), ensure_ascii=False, indent=2, sort_keys=True)
        artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=content,
            title=title,
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=node_id,
        )
        return ToolFoundationPersistedArtifact(
            artifact=artifact,
            kind=kind,
            record_count=record_count,
            byte_size=len(content.encode("utf-8")),
            metadata={
                "owner_unit": self.owner_unit,
                "runtime_id": self.runtime_id,
            },
        )

    def _materialization_findings(self, materialization: Mapping[str, Any]) -> list[ToolFoundationReplayFinding]:
        findings: list[ToolFoundationReplayFinding] = []
        if not materialization.get("materialization_id"):
            findings.append(
                _finding(
                    "TOOL_REPLAY_MATERIALIZATION_ID_MISSING",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.MATERIALIZATION,
                    "Persisted tool materialization has no materialization_id.",
                )
            )
        if materialization.get("owner_unit") != self.owner_unit:
            findings.append(
                _finding(
                    "TOOL_REPLAY_OWNER_MISMATCH",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.MATERIALIZATION,
                    "Persisted materialization owner does not match the M1-02C foundation owner.",
                    expected_owner=self.owner_unit,
                    actual_owner=str(materialization.get("owner_unit") or ""),
                )
            )
        active_tools = materialization.get("active_tool_names")
        if not isinstance(active_tools, list) or not active_tools:
            findings.append(
                _finding(
                    "TOOL_REPLAY_ACTIVE_TOOLS_MISSING",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.MATERIALIZATION,
                    "Persisted materialization contains no active tool names.",
                )
            )
        repos = {
            str(row.get("source_repo") or "")
            for row in materialization.get("source_ledger", [])
            if isinstance(row, Mapping) and row.get("source_repo")
        }
        missing = {"claude-code-best", "opencode", "hermes-agent"}.difference(repos)
        if missing:
            findings.append(
                _finding(
                    "TOOL_REPLAY_SOURCE_REPO_MISSING",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.SOURCE_LEDGER,
                    "Persisted source ledger does not cover required source repos.",
                    missing_repos=",".join(sorted(missing)),
                )
            )
        return findings

    def _receipt_findings(self, receipts: Sequence[Mapping[str, Any]]) -> list[ToolFoundationReplayFinding]:
        findings: list[ToolFoundationReplayFinding] = []
        for index, receipt in enumerate(receipts, start=1):
            if not isinstance(receipt.get("request"), Mapping):
                findings.append(
                    _finding(
                        "TOOL_REPLAY_RECEIPT_REQUEST_MISSING",
                        ToolFoundationReplayFindingSeverity.BLOCKER,
                        ToolFoundationReplaySurface.RECEIPTS,
                        "A persisted execution receipt has no request block.",
                        receipt_index=str(index),
                    )
                )
            if not isinstance(receipt.get("bounded_result"), Mapping):
                findings.append(
                    _finding(
                        "TOOL_REPLAY_RECEIPT_RESULT_MISSING",
                        ToolFoundationReplayFindingSeverity.BLOCKER,
                        ToolFoundationReplaySurface.RECEIPTS,
                        "A persisted execution receipt has no bounded result block.",
                        receipt_index=str(index),
                    )
                )
            if not isinstance(receipt.get("budget_decision"), Mapping):
                findings.append(
                    _finding(
                        "TOOL_REPLAY_RECEIPT_BUDGET_MISSING",
                        ToolFoundationReplayFindingSeverity.BLOCKER,
                        ToolFoundationReplaySurface.BUDGET,
                        "A persisted execution receipt has no budget decision block.",
                        receipt_index=str(index),
                    )
                )
        return findings

    def _context_findings(
        self,
        context_snapshots: Sequence[Mapping[str, Any]],
        receipts: Sequence[Mapping[str, Any]],
    ) -> list[ToolFoundationReplayFinding]:
        findings: list[ToolFoundationReplayFinding] = []
        if receipts and not context_snapshots:
            findings.append(
                _finding(
                    "TOOL_REPLAY_CONTEXT_SNAPSHOT_MISSING",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.CONTEXT,
                    "Receipts exist but no ToolUseContext snapshots were persisted.",
                )
            )
            return findings
        modifier_count = sum(len(snapshot.get("modifier_log") or []) for snapshot in context_snapshots)
        if receipts and modifier_count < len(receipts):
            findings.append(
                _finding(
                    "TOOL_REPLAY_CONTEXT_MODIFIER_UNDERFLOW",
                    ToolFoundationReplayFindingSeverity.BLOCKER,
                    ToolFoundationReplaySurface.CONTEXT,
                    "Persisted ToolUseContext modifiers do not cover every receipt.",
                    receipt_count=str(len(receipts)),
                    modifier_count=str(modifier_count),
                )
            )
        return findings

    def _externalized_output_findings(
        self,
        records: Sequence[ToolFoundationExternalizedOutputRecord],
    ) -> list[ToolFoundationReplayFinding]:
        findings: list[ToolFoundationReplayFinding] = []
        for record in records:
            if not record.artifact_uri:
                findings.append(
                    _finding(
                        "TOOL_REPLAY_EXTERNALIZED_OUTPUT_URI_MISSING",
                        ToolFoundationReplayFindingSeverity.BLOCKER,
                        ToolFoundationReplaySurface.ARTIFACTS,
                        "An externalized tool output has no artifact URI.",
                        tool_call_id=record.tool_call_id,
                        artifact_id=record.artifact_id,
                    )
                )
                continue
            path = Path(record.artifact_uri)
            if not path.exists():
                findings.append(
                    _finding(
                        "TOOL_REPLAY_EXTERNALIZED_OUTPUT_FILE_MISSING",
                        ToolFoundationReplayFindingSeverity.BLOCKER,
                        ToolFoundationReplaySurface.ARTIFACTS,
                        "An externalized tool output artifact path does not exist.",
                        tool_call_id=record.tool_call_id,
                        artifact_id=record.artifact_id,
                    )
                )
        return findings


def tool_foundation_persistence_metadata(artifact_set: ToolFoundationArtifactSet | None) -> dict[str, str]:
    if artifact_set is None:
        return {
            "tool_foundation_persisted_artifacts": "0",
            "tool_foundation_persistence_owner_unit": TOOL_LOOP_FOUNDATION_OWNER_UNIT,
        }
    metadata = artifact_set.metadata()
    replay_report = artifact_set.replay_report_artifact
    metadata["tool_foundation_replay_artifact_written"] = str(replay_report is not None).lower()
    return metadata


def render_tool_foundation_persistence_markdown(artifact_set: ToolFoundationArtifactSet) -> str:
    rows = []
    for item in [
        artifact_set.materialization_artifact,
        artifact_set.receipt_log_artifact,
        artifact_set.context_snapshot_artifact,
        artifact_set.externalized_output_index_artifact,
        artifact_set.replay_report_artifact,
    ]:
        if item is None:
            continue
        rows.append(f"- `{item.kind}`: `{item.artifact.artifact_id}` ({item.record_count} record(s))")
    return "\n".join(
        [
            "## Tool Foundation Persistence",
            "",
            f"- owner_unit: `{artifact_set.owner_unit}`",
            f"- runtime_id: `{artifact_set.runtime_id}`",
            f"- session_id: `{artifact_set.session_id}`",
            f"- persisted_count: `{artifact_set.persisted_count}`",
            "",
            "### Artifacts",
            "",
            *(rows or ["- no artifacts persisted"]),
        ]
    )


def _externalized_output_records(receipts: Sequence[Mapping[str, Any]]) -> tuple[ToolFoundationExternalizedOutputRecord, ...]:
    records: list[ToolFoundationExternalizedOutputRecord] = []
    for receipt in receipts:
        request = receipt.get("request") if isinstance(receipt.get("request"), Mapping) else {}
        decision = receipt.get("budget_decision") if isinstance(receipt.get("budget_decision"), Mapping) else {}
        result = receipt.get("bounded_result") if isinstance(receipt.get("bounded_result"), Mapping) else {}
        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), list) else []
        artifact_id = str(decision.get("artifact_id") or "")
        if not artifact_id:
            continue
        artifact = _find_artifact(artifacts, artifact_id)
        records.append(
            ToolFoundationExternalizedOutputRecord(
                tool_call_id=str(request.get("tool_call_id") or result.get("tool_call_id") or ""),
                tool_name=str(request.get("tool_name") or ""),
                artifact_id=artifact_id,
                artifact_uri=str(artifact.get("uri") or ""),
                reason=str(decision.get("reason") or ""),
                original_chars=_safe_int(decision.get("original_chars")),
                budget_chars=_safe_int(decision.get("budget_chars")),
                turn_index=_safe_int(request.get("turn_index")),
                step_index=_safe_int(request.get("step_index")),
            )
        )
    return tuple(records)


def _find_artifact(artifacts: Sequence[Any], artifact_id: str) -> Mapping[str, Any]:
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            continue
        if str(artifact.get("artifact_id") or "") == artifact_id:
            return artifact
    return {}


def _replay_status(findings: Sequence[ToolFoundationReplayFinding]) -> ToolFoundationReplayStatus:
    if any(finding.blocking for finding in findings):
        return ToolFoundationReplayStatus.FAILED
    if any(finding.severity in {ToolFoundationReplayFindingSeverity.WARNING, ToolFoundationReplayFindingSeverity.ERROR} for finding in findings):
        return ToolFoundationReplayStatus.PARTIAL
    return ToolFoundationReplayStatus.READY


def _finding(
    code: str,
    severity: ToolFoundationReplayFindingSeverity,
    surface: ToolFoundationReplaySurface,
    message: str,
    **metadata: str,
) -> ToolFoundationReplayFinding:
    return ToolFoundationReplayFinding(
        code=code,
        severity=severity,
        surface=surface,
        message=message,
        metadata={str(key): str(value) for key, value in metadata.items()},
    )


def _artifact_id(item: ToolFoundationPersistedArtifact | None) -> str:
    return item.artifact.artifact_id if item is not None else ""


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
