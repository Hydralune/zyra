from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, now_iso, to_jsonable

from .claude_context_assembly_foundation import ContextAssemblySnapshot
from .claude_input_processor import QueryInputKind, QueryInputProcessingReport
from .claude_session_store import (
    CodeWorkerSessionSeed,
    CodeWorkerSessionStore,
    CodeWorkerSessionStoreRecord,
    CodeWorkerSessionStoreRecordType,
)


class FoundationAuditSeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class FoundationAuditSurface(StrEnum):
    INPUT_PROCESSOR = "input_processor"
    CONTEXT_ASSEMBLY = "context_assembly"
    SESSION_STORE = "session_store"
    QUERY_ENGINE_HANDOFF = "query_engine_handoff"
    EVENT_STREAM = "event_stream"
    SOURCE_TO_TARGET = "source_to_target"
    CLEAN_RUNTIME = "clean_runtime"
    DISCONNECT_SEMANTICS = "disconnect_semantics"


class FoundationAuditRuleKind(StrEnum):
    REQUIRED = "required"
    CONSISTENCY = "consistency"
    REACHABILITY = "reachability"
    EFFECT = "effect"
    CLEAN_BOUNDARY = "clean_boundary"
    LINE_BUCKET = "line_bucket"


@dataclass(frozen=True, slots=True)
class FoundationAuditEvidence:
    evidence_id: str
    surface: FoundationAuditSurface
    source_path: str
    target_path: str
    description: str
    value: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "surface": str(self.surface),
            "source_path": self.source_path,
            "target_path": self.target_path,
            "description": self.description,
            "value": to_jsonable(self.value),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FoundationAuditFinding:
    code: str
    severity: FoundationAuditSeverity
    surface: FoundationAuditSurface
    rule_kind: FoundationAuditRuleKind
    message: str
    evidence: tuple[FoundationAuditEvidence, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == FoundationAuditSeverity.BLOCKER

    @property
    def passed(self) -> bool:
        return self.severity == FoundationAuditSeverity.PASS

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "rule_kind": str(self.rule_kind),
            "message": self.message,
            "blocking": self.blocking,
            "passed": self.passed,
            "evidence": [item.to_dict() for item in self.evidence],
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FoundationAuditRule:
    rule_id: str
    surface: FoundationAuditSurface
    rule_kind: FoundationAuditRuleKind
    description: str
    source_paths: tuple[str, ...]
    target_paths: tuple[str, ...]
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "surface": str(self.surface),
            "rule_kind": str(self.rule_kind),
            "description": self.description,
            "source_paths": list(self.source_paths),
            "target_paths": list(self.target_paths),
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class FoundationStoreReplaySummary:
    ok: bool
    path: str
    record_count: int
    record_types: dict[str, int]
    first_sequence: int
    last_sequence: int
    session_ids: tuple[str, ...]
    worker_request_ids: tuple[str, ...]
    error: str = ""

    @property
    def has_seed(self) -> bool:
        return self.record_types.get(str(CodeWorkerSessionStoreRecordType.SESSION_SEED), 0) > 0

    @property
    def has_input(self) -> bool:
        return self.record_types.get(str(CodeWorkerSessionStoreRecordType.INPUT_ACCEPTED), 0) > 0

    @property
    def has_context(self) -> bool:
        return self.record_types.get(str(CodeWorkerSessionStoreRecordType.CONTEXT_SNAPSHOT), 0) > 0

    @property
    def has_query_engine_attach(self) -> bool:
        return self.record_types.get(str(CodeWorkerSessionStoreRecordType.QUERY_ENGINE_ATTACHED), 0) > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "path": self.path,
            "record_count": self.record_count,
            "record_types": dict(self.record_types),
            "first_sequence": self.first_sequence,
            "last_sequence": self.last_sequence,
            "session_ids": list(self.session_ids),
            "worker_request_ids": list(self.worker_request_ids),
            "error": self.error,
            "has_seed": self.has_seed,
            "has_input": self.has_input,
            "has_context": self.has_context,
            "has_query_engine_attach": self.has_query_engine_attach,
        }


@dataclass(frozen=True, slots=True)
class FoundationEventProjection:
    event_count: int
    query_session_event_count: int
    phases: tuple[str, ...]
    phase_counts: dict[str, int]
    session_ids: tuple[str, ...]
    worker_request_ids: tuple[str, ...]
    missing_required_phases: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing_required_phases

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_count": self.event_count,
            "query_session_event_count": self.query_session_event_count,
            "phases": list(self.phases),
            "phase_counts": dict(self.phase_counts),
            "session_ids": list(self.session_ids),
            "worker_request_ids": list(self.worker_request_ids),
            "missing_required_phases": list(self.missing_required_phases),
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class FoundationContextProjection:
    ok: bool
    snapshot_id: str
    status: str
    selected_block_count: int
    dropped_block_count: int
    active_chars: int
    fingerprint: str
    kinds: dict[str, int]
    blocker_count: int
    warning_count: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "snapshot_id": self.snapshot_id,
            "status": self.status,
            "selected_block_count": self.selected_block_count,
            "dropped_block_count": self.dropped_block_count,
            "active_chars": self.active_chars,
            "fingerprint": self.fingerprint,
            "kinds": dict(self.kinds),
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FoundationInputProjection:
    ok: bool
    input_count: int
    accepted_count: int
    rejected_count: int
    kind_counts: dict[str, int]
    disposition_counts: dict[str, int]
    high_risk_count: int
    blocker_count: int
    warning_count: int
    accepted_input_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "input_count": self.input_count,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "kind_counts": dict(self.kind_counts),
            "disposition_counts": dict(self.disposition_counts),
            "high_risk_count": self.high_risk_count,
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "accepted_input_ids": list(self.accepted_input_ids),
        }


@dataclass(frozen=True, slots=True)
class FoundationAuditReport:
    ok: bool
    status: str
    session_id: str
    worker_request_id: str
    created_at: str
    findings: tuple[FoundationAuditFinding, ...]
    input_projection: FoundationInputProjection
    context_projection: FoundationContextProjection | None
    store_replay: FoundationStoreReplaySummary
    event_projection: FoundationEventProjection | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == FoundationAuditSeverity.WARNING)

    @property
    def pass_count(self) -> int:
        return sum(1 for finding in self.findings if finding.passed)

    def metadata_values(self) -> dict[str, str]:
        return {
            "session_foundation_audit_ok": str(self.ok).lower(),
            "session_foundation_audit_status": self.status,
            "session_foundation_audit_findings": str(len(self.findings)),
            "session_foundation_audit_blockers": str(self.blocker_count),
            "session_foundation_audit_warnings": str(self.warning_count),
            "session_foundation_audit_passes": str(self.pass_count),
            "session_foundation_store_replay_ok": str(self.store_replay.ok).lower(),
            "session_foundation_store_replay_records": str(self.store_replay.record_count),
            "session_foundation_event_projection_ok": str(self.event_projection.ok).lower() if self.event_projection else "",
            "session_foundation_input_projection_ok": str(self.input_projection.ok).lower(),
            "session_foundation_context_projection_ok": str(self.context_projection.ok).lower() if self.context_projection else "",
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "created_at": self.created_at,
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "pass_count": self.pass_count,
            "input_projection": self.input_projection.to_dict(),
            "context_projection": self.context_projection.to_dict() if self.context_projection else None,
            "store_replay": self.store_replay.to_dict(),
            "event_projection": self.event_projection.to_dict() if self.event_projection else None,
            "metadata": to_jsonable(self.metadata),
        }


class SessionFoundationAuditor:
    """Audits the actual CodeWorker session foundation objects used by a run."""

    def __init__(self, *, rules: Sequence[FoundationAuditRule] | None = None) -> None:
        self.rules = tuple(rules or default_foundation_audit_rules())

    def audit_seed(
        self,
        seed: CodeWorkerSessionSeed,
        *,
        store: CodeWorkerSessionStore,
        events: Sequence[EventRecord] = (),
    ) -> FoundationAuditReport:
        input_projection = project_input_report(seed.input_report)
        context_projection = project_context_snapshot(seed.context_snapshot)
        store_replay = replay_store_summary(store, seed.session_id)
        event_projection = project_foundation_events(events, session_id=seed.session_id, worker_request_id=seed.worker_request_id)
        findings = list(
            self._findings_for_seed(
                seed=seed,
                input_projection=input_projection,
                context_projection=context_projection,
                store_replay=store_replay,
                event_projection=event_projection,
            )
        )
        ok = seed.ok and not any(finding.blocking for finding in findings)
        status = "ready" if ok else "blocked"
        if ok and any(finding.severity == FoundationAuditSeverity.WARNING for finding in findings):
            status = "degraded"
        return FoundationAuditReport(
            ok=ok,
            status=status,
            session_id=seed.session_id,
            worker_request_id=seed.worker_request_id,
            created_at=now_iso(),
            findings=tuple(findings),
            input_projection=input_projection,
            context_projection=context_projection,
            store_replay=store_replay,
            event_projection=event_projection,
            metadata={
                "rule_count": len(self.rules),
                "rule_ids": [rule.rule_id for rule in self.rules],
                "seed_status": str(seed.status),
                "run_id": seed.run_id,
                "task_id": seed.task_id,
                "node_id": seed.node_id,
            },
        )

    def _findings_for_seed(
        self,
        *,
        seed: CodeWorkerSessionSeed,
        input_projection: FoundationInputProjection,
        context_projection: FoundationContextProjection | None,
        store_replay: FoundationStoreReplaySummary,
        event_projection: FoundationEventProjection,
    ) -> Iterable[FoundationAuditFinding]:
        yield self._finding(
            code="input_processor_reachable" if input_projection.ok else "input_processor_blocked",
            severity=FoundationAuditSeverity.PASS if input_projection.ok else FoundationAuditSeverity.BLOCKER,
            surface=FoundationAuditSurface.INPUT_PROCESSOR,
            rule_kind=FoundationAuditRuleKind.REACHABILITY,
            message="QueryInputProcessor produced accepted records." if input_projection.ok else "QueryInputProcessor did not produce accepted records.",
            evidence=[
                self._evidence(
                    surface=FoundationAuditSurface.INPUT_PROCESSOR,
                    source_path="src/processUserInput.ts",
                    target_path="packages/runtime/zyra_runtime/claude_input_processor.py",
                    description="Input projection",
                    value=input_projection.to_dict(),
                )
            ],
        )
        context_ok = context_projection is not None and context_projection.ok
        yield self._finding(
            code="context_snapshot_reachable" if context_ok else "context_snapshot_blocked",
            severity=FoundationAuditSeverity.PASS if context_ok else FoundationAuditSeverity.BLOCKER,
            surface=FoundationAuditSurface.CONTEXT_ASSEMBLY,
            rule_kind=FoundationAuditRuleKind.REACHABILITY,
            message="ContextAssemblyRuntime produced a usable snapshot." if context_ok else "ContextAssemblyRuntime did not produce a usable snapshot.",
            evidence=[
                self._evidence(
                    surface=FoundationAuditSurface.CONTEXT_ASSEMBLY,
                    source_path="src/utils/queryContext.ts",
                    target_path="packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
                    description="Context projection",
                    value=context_projection.to_dict() if context_projection else None,
                )
            ],
        )
        store_ok = store_replay.ok and store_replay.has_seed and store_replay.has_input and store_replay.has_context
        yield self._finding(
            code="session_store_replay_consistent" if store_ok else "session_store_replay_incomplete",
            severity=FoundationAuditSeverity.PASS if store_ok else FoundationAuditSeverity.BLOCKER,
            surface=FoundationAuditSurface.SESSION_STORE,
            rule_kind=FoundationAuditRuleKind.CONSISTENCY,
            message="CodeWorkerSessionStore replay includes seed, input and context records."
            if store_ok
            else "CodeWorkerSessionStore replay is missing required seed/input/context records.",
            evidence=[
                self._evidence(
                    surface=FoundationAuditSurface.SESSION_STORE,
                    source_path="src/utils/sessionStorage.ts",
                    target_path="packages/runtime/zyra_runtime/claude_session_store.py",
                    description="Store replay",
                    value=store_replay.to_dict(),
                )
            ],
        )
        session_match = (
            seed.session_id in store_replay.session_ids
            and seed.worker_request_id in store_replay.worker_request_ids
            and (context_projection is None or context_projection.metadata.get("session_id", seed.session_id) == seed.session_id)
        )
        yield self._finding(
            code="session_identity_consistent" if session_match else "session_identity_mismatch",
            severity=FoundationAuditSeverity.PASS if session_match else FoundationAuditSeverity.BLOCKER,
            surface=FoundationAuditSurface.QUERY_ENGINE_HANDOFF,
            rule_kind=FoundationAuditRuleKind.CONSISTENCY,
            message="Session id and worker request id are consistent across seed, context and store."
            if session_match
            else "Session id or worker request id changed across seed, context or store.",
            evidence=[
                self._evidence(
                    surface=FoundationAuditSurface.QUERY_ENGINE_HANDOFF,
                    source_path="src/QueryEngine.ts",
                    target_path="packages/runtime/zyra_runtime/claude_query_engine_runtime.py",
                    description="Session identity",
                    value={
                        "seed_session_id": seed.session_id,
                        "store_session_ids": list(store_replay.session_ids),
                        "seed_worker_request_id": seed.worker_request_id,
                        "store_worker_request_ids": list(store_replay.worker_request_ids),
                    },
                )
            ],
        )
        event_ok = event_projection.ok
        yield self._finding(
            code="foundation_events_projected" if event_ok else "foundation_events_missing_phase",
            severity=FoundationAuditSeverity.PASS if event_ok else FoundationAuditSeverity.WARNING,
            surface=FoundationAuditSurface.EVENT_STREAM,
            rule_kind=FoundationAuditRuleKind.REACHABILITY,
            message="Foundation event stream contains required pre-query phases."
            if event_ok
            else "Foundation event stream is missing one or more pre-query phases.",
            evidence=[
                self._evidence(
                    surface=FoundationAuditSurface.EVENT_STREAM,
                    source_path="src/QueryEngine.ts",
                    target_path="packages/workers/zyra_workers/code_worker_runtime.py",
                    description="Event projection",
                    value=event_projection.to_dict(),
                )
            ],
        )
        for rule in self.rules:
            if rule.surface in {
                FoundationAuditSurface.SOURCE_TO_TARGET,
                FoundationAuditSurface.CLEAN_RUNTIME,
                FoundationAuditSurface.DISCONNECT_SEMANTICS,
            }:
                yield self._rule_presence_finding(rule)

    def _rule_presence_finding(self, rule: FoundationAuditRule) -> FoundationAuditFinding:
        missing_targets = [path for path in rule.target_paths if not Path(path).is_absolute() and not path]
        severity = FoundationAuditSeverity.PASS if not missing_targets else FoundationAuditSeverity.BLOCKER
        return self._finding(
            code=f"{rule.rule_id}_declared",
            severity=severity,
            surface=rule.surface,
            rule_kind=rule.rule_kind,
            message=rule.description,
            evidence=[
                self._evidence(
                    surface=rule.surface,
                    source_path=", ".join(rule.source_paths),
                    target_path=", ".join(rule.target_paths),
                    description="Declared audit rule source-to-target boundary",
                    value=rule.to_dict(),
                )
            ],
        )

    def _finding(
        self,
        *,
        code: str,
        severity: FoundationAuditSeverity,
        surface: FoundationAuditSurface,
        rule_kind: FoundationAuditRuleKind,
        message: str,
        evidence: Sequence[FoundationAuditEvidence] = (),
        metadata: Mapping[str, Any] | None = None,
    ) -> FoundationAuditFinding:
        return FoundationAuditFinding(
            code=code,
            severity=severity,
            surface=surface,
            rule_kind=rule_kind,
            message=message,
            evidence=tuple(evidence),
            metadata=dict(metadata or {}),
        )

    def _evidence(
        self,
        *,
        surface: FoundationAuditSurface,
        source_path: str,
        target_path: str,
        description: str,
        value: Any,
        metadata: Mapping[str, Any] | None = None,
    ) -> FoundationAuditEvidence:
        return FoundationAuditEvidence(
            evidence_id=f"evidence_{surface}_{abs(hash((source_path, target_path, description))) % 1_000_000}",
            surface=surface,
            source_path=source_path,
            target_path=target_path,
            description=description,
            value=value,
            metadata=dict(metadata or {}),
        )


def project_input_report(report: QueryInputProcessingReport) -> FoundationInputProjection:
    kind_counts = report.kind_counts
    disposition_counts = report.disposition_counts
    high_risk_count = 0
    blocker_count = len(report.blockers)
    warning_count = len(report.warnings)
    for record in report.records:
        if str(record.risk) in {"high", "blocking"}:
            high_risk_count += 1
        blocker_count += len(record.blockers)
        warning_count += len(record.warnings)
    return FoundationInputProjection(
        ok=report.ok and bool(report.accepted_records),
        input_count=len(report.records),
        accepted_count=len(report.accepted_records),
        rejected_count=len(report.rejected_records),
        kind_counts=dict(kind_counts),
        disposition_counts=dict(disposition_counts),
        high_risk_count=high_risk_count,
        blocker_count=blocker_count,
        warning_count=warning_count,
        accepted_input_ids=tuple(record.input_id for record in report.accepted_records),
    )


def project_context_snapshot(snapshot: ContextAssemblySnapshot | None) -> FoundationContextProjection | None:
    if snapshot is None:
        return None
    kinds: dict[str, int] = {}
    for block in snapshot.selected_blocks:
        kinds[str(block.kind)] = kinds.get(str(block.kind), 0) + 1
    return FoundationContextProjection(
        ok=snapshot.ok and bool(snapshot.selected_blocks),
        snapshot_id=snapshot.snapshot_id,
        status=str(snapshot.status),
        selected_block_count=len(snapshot.selected_blocks),
        dropped_block_count=len(snapshot.dropped_blocks),
        active_chars=snapshot.active_chars,
        fingerprint=snapshot.fingerprint,
        kinds=kinds,
        blocker_count=snapshot.blocker_count,
        warning_count=snapshot.warning_count,
        metadata={
            "session_id": snapshot.session_id,
            "worker_request_id": snapshot.worker_request_id,
            "source_path": snapshot.source.source_path,
            "target_path": snapshot.source.target_path,
        },
    )


def replay_store_summary(store: CodeWorkerSessionStore, session_id: str) -> FoundationStoreReplaySummary:
    replay = store.replay_session(session_id)
    if not replay.ok:
        return FoundationStoreReplaySummary(
            ok=False,
            path=replay.path,
            record_count=0,
            record_types={},
            first_sequence=0,
            last_sequence=0,
            session_ids=(),
            worker_request_ids=(),
            error=replay.error,
        )
    return summarize_store_records(replay.records, path=replay.path)


def summarize_store_records(records: Sequence[CodeWorkerSessionStoreRecord], *, path: str = "") -> FoundationStoreReplaySummary:
    record_types: dict[str, int] = {}
    session_ids: list[str] = []
    worker_request_ids: list[str] = []
    sequences: list[int] = []
    for record in records:
        record_types[str(record.record_type)] = record_types.get(str(record.record_type), 0) + 1
        if record.session_id and record.session_id not in session_ids:
            session_ids.append(record.session_id)
        if record.worker_request_id and record.worker_request_id not in worker_request_ids:
            worker_request_ids.append(record.worker_request_id)
        sequences.append(record.sequence)
    contiguous = sequences == list(range(min(sequences or [1]), max(sequences or [0]) + 1))
    return FoundationStoreReplaySummary(
        ok=bool(records) and contiguous,
        path=path,
        record_count=len(records),
        record_types=record_types,
        first_sequence=min(sequences or [0]),
        last_sequence=max(sequences or [0]),
        session_ids=tuple(session_ids),
        worker_request_ids=tuple(worker_request_ids),
        error="" if contiguous else "store_sequence_gap",
    )


def project_foundation_events(
    events: Sequence[EventRecord],
    *,
    session_id: str,
    worker_request_id: str,
) -> FoundationEventProjection:
    phases: list[str] = []
    phase_counts: dict[str, int] = {}
    session_ids: list[str] = []
    worker_request_ids: list[str] = []
    query_session_event_count = 0
    for event in events:
        payload = event.payload if isinstance(event.payload, Mapping) else {}
        query_session = payload.get("query_session") if isinstance(payload.get("query_session"), Mapping) else None
        if query_session is None:
            continue
        query_session_event_count += 1
        phase = str(query_session.get("phase") or "")
        if phase:
            phases.append(phase)
            phase_counts[phase] = phase_counts.get(phase, 0) + 1
        event_session_id = str(query_session.get("session_id") or "")
        if event_session_id and event_session_id not in session_ids:
            session_ids.append(event_session_id)
        event_worker_request_id = str(query_session.get("worker_request_id") or "")
        if event_worker_request_id and event_worker_request_id not in worker_request_ids:
            worker_request_ids.append(event_worker_request_id)
    required = (
        "query_session_seed_created",
        "query_input_processed",
        "context_snapshot_ready",
        "session_store_append",
    )
    missing = tuple(phase for phase in required if phase not in phase_counts)
    if events and session_id and session_id not in session_ids:
        missing = (*missing, "session_id_projection")
    if events and worker_request_id and worker_request_id not in worker_request_ids:
        missing = (*missing, "worker_request_id_projection")
    return FoundationEventProjection(
        event_count=len(events),
        query_session_event_count=query_session_event_count,
        phases=tuple(phases),
        phase_counts=phase_counts,
        session_ids=tuple(session_ids),
        worker_request_ids=tuple(worker_request_ids),
        missing_required_phases=missing,
    )


def default_foundation_audit_rules() -> tuple[FoundationAuditRule, ...]:
    return (
        FoundationAuditRule(
            rule_id="process_user_input_migrated",
            surface=FoundationAuditSurface.SOURCE_TO_TARGET,
            rule_kind=FoundationAuditRuleKind.REACHABILITY,
            description="processUserInput behavior is represented by QueryInputProcessor.",
            source_paths=("src/processUserInput.ts",),
            target_paths=("packages/runtime/zyra_runtime/claude_input_processor.py",),
        ),
        FoundationAuditRule(
            rule_id="query_context_migrated",
            surface=FoundationAuditSurface.SOURCE_TO_TARGET,
            rule_kind=FoundationAuditRuleKind.REACHABILITY,
            description="queryContext behavior is represented by ContextAssemblyRuntime.",
            source_paths=("src/utils/queryContext.ts", "src/context.ts"),
            target_paths=("packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",),
        ),
        FoundationAuditRule(
            rule_id="session_storage_migrated",
            surface=FoundationAuditSurface.SOURCE_TO_TARGET,
            rule_kind=FoundationAuditRuleKind.CONSISTENCY,
            description="sessionStorage seed behavior is represented by CodeWorkerSessionStore.",
            source_paths=("src/utils/sessionStorage.ts",),
            target_paths=("packages/runtime/zyra_runtime/claude_session_store.py", "packages/runtime/zyra_runtime/query_session.py"),
        ),
        FoundationAuditRule(
            rule_id="default_path_sidecar_free",
            surface=FoundationAuditSurface.CLEAN_RUNTIME,
            rule_kind=FoundationAuditRuleKind.CLEAN_BOUNDARY,
            description="Default session foundation path does not require a root-level source checkout or sidecar runtime.",
            source_paths=("source-graphs/claude-code-best/batch-01-query-session-context.md",),
            target_paths=("packages/workers/zyra_workers/code_worker_runtime.py",),
        ),
        FoundationAuditRule(
            rule_id="disconnect_blocks_before_query",
            surface=FoundationAuditSurface.DISCONNECT_SEMANTICS,
            rule_kind=FoundationAuditRuleKind.EFFECT,
            description="Disabling input, context or store blocks before stream_request_start.",
            source_paths=("src/QueryEngine.ts",),
            target_paths=(
                "packages/runtime/zyra_runtime/claude_input_processor.py",
                "packages/runtime/zyra_runtime/claude_context_assembly_foundation.py",
                "packages/runtime/zyra_runtime/claude_session_store.py",
                "packages/workers/zyra_workers/code_worker_runtime.py",
            ),
        ),
    )


def foundation_audit_event(report: FoundationAuditReport) -> EventRecord:
    return EventRecord(
        run_id=report.metadata.get("run_id", ""),
        task_id=report.metadata.get("task_id", ""),
        node_id=report.metadata.get("node_id") or None,
        event_type=EventType.AGENT_MESSAGE,
        payload={
            "query_session": {
                "session_id": report.session_id,
                "worker_request_id": report.worker_request_id,
                "phase": "session_foundation_audit",
                "ok": report.ok,
                "status": report.status,
                "blocker_count": report.blocker_count,
                "warning_count": report.warning_count,
                "pass_count": report.pass_count,
            }
        },
    )


def render_foundation_audit_markdown(report: FoundationAuditReport) -> str:
    lines = [
        "# Session Foundation Audit",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- session_id: `{report.session_id}`",
        f"- worker_request_id: `{report.worker_request_id}`",
        f"- blockers: `{report.blocker_count}`",
        f"- warnings: `{report.warning_count}`",
        f"- passes: `{report.pass_count}`",
        "",
        "## Projections",
        "",
        f"- input_ok: `{str(report.input_projection.ok).lower()}`",
        f"- input_count: `{report.input_projection.input_count}`",
        f"- context_ok: `{str(report.context_projection.ok).lower() if report.context_projection else ''}`",
        f"- store_ok: `{str(report.store_replay.ok).lower()}`",
        f"- store_records: `{report.store_replay.record_count}`",
        f"- event_projection_ok: `{str(report.event_projection.ok).lower() if report.event_projection else ''}`",
        "",
        "## Findings",
        "",
    ]
    for finding in report.findings:
        lines.append(f"- `{finding.severity}` `{finding.surface}` `{finding.code}` {finding.message}")
    return "\n".join(lines) + "\n"


def foundation_audit_metadata(report: FoundationAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "session_foundation_audit_ok": "false",
            "session_foundation_audit_status": "",
            "session_foundation_audit_findings": "0",
            "session_foundation_audit_blockers": "0",
        }
    return report.metadata_values()


def audit_report_from_payload(payload: Mapping[str, Any]) -> FoundationAuditReport:
    input_projection = FoundationInputProjection(
        ok=bool(payload.get("input_projection", {}).get("ok")) if isinstance(payload.get("input_projection"), Mapping) else False,
        input_count=int(_nested(payload, "input_projection", "input_count", default=0)),
        accepted_count=int(_nested(payload, "input_projection", "accepted_count", default=0)),
        rejected_count=int(_nested(payload, "input_projection", "rejected_count", default=0)),
        kind_counts=dict(_nested(payload, "input_projection", "kind_counts", default={})),
        disposition_counts=dict(_nested(payload, "input_projection", "disposition_counts", default={})),
        high_risk_count=int(_nested(payload, "input_projection", "high_risk_count", default=0)),
        blocker_count=int(_nested(payload, "input_projection", "blocker_count", default=0)),
        warning_count=int(_nested(payload, "input_projection", "warning_count", default=0)),
        accepted_input_ids=tuple(str(item) for item in _nested(payload, "input_projection", "accepted_input_ids", default=[])),
    )
    store_replay = FoundationStoreReplaySummary(
        ok=bool(_nested(payload, "store_replay", "ok", default=False)),
        path=str(_nested(payload, "store_replay", "path", default="")),
        record_count=int(_nested(payload, "store_replay", "record_count", default=0)),
        record_types=dict(_nested(payload, "store_replay", "record_types", default={})),
        first_sequence=int(_nested(payload, "store_replay", "first_sequence", default=0)),
        last_sequence=int(_nested(payload, "store_replay", "last_sequence", default=0)),
        session_ids=tuple(str(item) for item in _nested(payload, "store_replay", "session_ids", default=[])),
        worker_request_ids=tuple(str(item) for item in _nested(payload, "store_replay", "worker_request_ids", default=[])),
        error=str(_nested(payload, "store_replay", "error", default="")),
    )
    findings = tuple(_finding_from_payload(item) for item in payload.get("findings", []) if isinstance(item, Mapping))
    return FoundationAuditReport(
        ok=bool(payload.get("ok")),
        status=str(payload.get("status") or ""),
        session_id=str(payload.get("session_id") or ""),
        worker_request_id=str(payload.get("worker_request_id") or ""),
        created_at=str(payload.get("created_at") or now_iso()),
        findings=findings,
        input_projection=input_projection,
        context_projection=None,
        store_replay=store_replay,
        event_projection=None,
        metadata=dict(payload.get("metadata") or {}),
    )


def _finding_from_payload(payload: Mapping[str, Any]) -> FoundationAuditFinding:
    return FoundationAuditFinding(
        code=str(payload.get("code") or ""),
        severity=_enum_or_default(FoundationAuditSeverity, payload.get("severity"), FoundationAuditSeverity.WARNING),
        surface=_enum_or_default(FoundationAuditSurface, payload.get("surface"), FoundationAuditSurface.SESSION_STORE),
        rule_kind=_enum_or_default(FoundationAuditRuleKind, payload.get("rule_kind"), FoundationAuditRuleKind.CONSISTENCY),
        message=str(payload.get("message") or ""),
        evidence=(),
        metadata=dict(payload.get("metadata") or {}),
    )


def _nested(payload: Mapping[str, Any], *keys: str, default: Any) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return default
        current = current.get(key)
    return default if current is None else current


def _enum_or_default(enum_type: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return enum_type(str(value))
    except ValueError:
        return default
