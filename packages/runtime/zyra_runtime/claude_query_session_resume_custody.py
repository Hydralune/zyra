from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import EventRecord, EventType, new_id, now_iso, to_jsonable


class QuerySessionCustodyStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


class QuerySessionCustodySeverity(StrEnum):
    PASS = "pass"
    INFO = "info"
    WARNING = "warning"
    BLOCKER = "blocker"


class QuerySessionCustodySurface(StrEnum):
    STORE = "store"
    SEED = "seed"
    INPUT = "input"
    CONTEXT = "context"
    QUERY_ENTRY = "query_entry"
    CONTROL = "control"
    CHECKPOINT = "checkpoint"
    RESUME = "resume"
    ENGINE_ATTACH = "engine_attach"


@dataclass(frozen=True, slots=True)
class QuerySessionCustodyRecord:
    record_type: str
    sequence: int
    session_id: str
    worker_request_id: str
    ok: bool
    fingerprint: str = ""
    payload_keys: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_type": self.record_type,
            "sequence": self.sequence,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "ok": self.ok,
            "fingerprint": self.fingerprint,
            "payload_keys": list(self.payload_keys),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionCustodyRequirement:
    requirement_id: str
    record_type: str
    surface: QuerySessionCustodySurface
    required: bool
    min_count: int = 1
    must_precede: tuple[str, ...] = ()
    must_follow: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "record_type": self.record_type,
            "surface": str(self.surface),
            "required": self.required,
            "min_count": self.min_count,
            "must_precede": list(self.must_precede),
            "must_follow": list(self.must_follow),
            "description": self.description,
        }


@dataclass(frozen=True, slots=True)
class QuerySessionCustodyFinding:
    code: str
    severity: QuerySessionCustodySeverity
    surface: QuerySessionCustodySurface
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity == QuerySessionCustodySeverity.BLOCKER

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "surface": str(self.surface),
            "message": self.message,
            "blocking": self.blocking,
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class QuerySessionResumeCustodyReport:
    report_id: str
    status: QuerySessionCustodyStatus
    session_id: str
    worker_request_id: str
    records: tuple[QuerySessionCustodyRecord, ...]
    requirements: tuple[QuerySessionCustodyRequirement, ...]
    findings: tuple[QuerySessionCustodyFinding, ...]
    resume_requested: bool
    checkpoint_requested: bool
    engine_attached: bool
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {QuerySessionCustodyStatus.READY, QuerySessionCustodyStatus.DEGRADED}

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.blocking)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == QuerySessionCustodySeverity.WARNING)

    @property
    def first_blocker_code(self) -> str:
        for finding in self.findings:
            if finding.blocking:
                return finding.code
        return ""

    @property
    def record_types(self) -> tuple[str, ...]:
        return tuple(record.record_type for record in self.records)

    def metadata_values(self) -> dict[str, str]:
        return {
            "query_custody_report_id": self.report_id,
            "query_custody_ok": str(self.ok).lower(),
            "query_custody_status": str(self.status),
            "query_custody_record_count": str(len(self.records)),
            "query_custody_blockers": str(self.blocker_count),
            "query_custody_warnings": str(self.warning_count),
            "query_custody_first_blocker": self.first_blocker_code,
            "query_custody_resume_requested": str(self.resume_requested).lower(),
            "query_custody_checkpoint_requested": str(self.checkpoint_requested).lower(),
            "query_custody_engine_attached": str(self.engine_attached).lower(),
            "query_custody_has_query_entry_packet": str("query_entry_packet" in self.record_types).lower(),
            "query_custody_has_query_control_state": str("query_control_state" in self.record_types).lower(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "report_id": self.report_id,
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "records": [record.to_dict() for record in self.records],
            "requirements": [requirement.to_dict() for requirement in self.requirements],
            "findings": [finding.to_dict() for finding in self.findings],
            "blocker_count": self.blocker_count,
            "warning_count": self.warning_count,
            "first_blocker_code": self.first_blocker_code,
            "resume_requested": self.resume_requested,
            "checkpoint_requested": self.checkpoint_requested,
            "engine_attached": self.engine_attached,
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
        }


class QuerySessionResumeCustodyRuntime:
    """Audits that query lifecycle state is held by Zyra session records."""

    def build_report(
        self,
        *,
        session_replay: Any,
        packet: Mapping[str, Any],
        integration_report: Mapping[str, Any],
        after_engine: bool,
    ) -> QuerySessionResumeCustodyReport:
        raw_records = tuple(getattr(session_replay, "records", ()) or ())
        records = tuple(project_custody_record(record) for record in raw_records)
        session_id = str(getattr(session_replay, "session_id", "") or packet.get("session_id") or "")
        worker_request_id = str(
            getattr(session_replay, "worker_request_id", "") or packet.get("worker_request_id") or ""
        )
        resume = _as_mapping(packet.get("resume"))
        checkpoint = _as_mapping(packet.get("checkpoint"))
        requirements = tuple(
            default_query_session_custody_requirements(
                resume_requested=resume.get("requested") is True,
                checkpoint_requested=str(checkpoint.get("status") or "") not in {"", "not_requested"},
                after_engine=after_engine,
            )
        )
        findings = [
            *validate_custody_requirements(records, requirements),
            *validate_custody_order(records),
            *validate_resume_custody(records, packet),
            *validate_checkpoint_custody(records, packet),
            *validate_integration_custody(records, integration_report),
        ]
        status = custody_status_from_findings(findings)
        return QuerySessionResumeCustodyReport(
            report_id=new_id("qcust"),
            status=status,
            session_id=session_id,
            worker_request_id=worker_request_id,
            records=records,
            requirements=requirements,
            findings=tuple(findings),
            resume_requested=resume.get("requested") is True,
            checkpoint_requested=str(checkpoint.get("status") or "") not in {"", "not_requested"},
            engine_attached=any(record.record_type == "query_engine_attached" for record in records),
            metadata={
                "source_path": "src/QueryEngine.ts",
                "target_path": "packages/runtime/zyra_runtime/claude_query_session_resume_custody.py",
                "owner_unit": "M1-02B",
            },
        )

    def event_for_report(
        self,
        report: QuerySessionResumeCustodyReport,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> EventRecord:
        return EventRecord(
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "query_session": {
                    "phase": "query_resume_custody",
                    "session_id": report.session_id,
                    "worker_request_id": report.worker_request_id,
                    "ok": report.ok,
                    "status": str(report.status),
                    "record_count": len(report.records),
                    "resume_requested": report.resume_requested,
                    "checkpoint_requested": report.checkpoint_requested,
                    "engine_attached": report.engine_attached,
                    "blocker_count": report.blocker_count,
                    "first_blocker_code": report.first_blocker_code,
                }
            },
        )


def project_custody_record(record: Any) -> QuerySessionCustodyRecord:
    payload = _as_mapping(getattr(record, "payload", {}))
    metadata = _as_mapping(getattr(record, "metadata", {}))
    record_type = str(getattr(record, "record_type", "") or "")
    if "." in record_type:
        record_type = record_type.rsplit(".", 1)[-1]
    fingerprint = str(metadata.get("fingerprint") or payload.get("fingerprint") or "")
    context_payload = _as_mapping(payload.get("context"))
    if not fingerprint:
        fingerprint = str(context_payload.get("fingerprint") or "")
    return QuerySessionCustodyRecord(
        record_type=record_type,
        sequence=int(getattr(record, "sequence", 0) or 0),
        session_id=str(getattr(record, "session_id", "") or ""),
        worker_request_id=str(getattr(record, "worker_request_id", "") or ""),
        ok=metadata.get("ok") is not False,
        fingerprint=fingerprint,
        payload_keys=tuple(sorted(str(key) for key in payload.keys())),
        metadata=dict(metadata),
    )


def default_query_session_custody_requirements(
    *,
    resume_requested: bool,
    checkpoint_requested: bool,
    after_engine: bool,
) -> Iterable[QuerySessionCustodyRequirement]:
    yield QuerySessionCustodyRequirement(
        requirement_id="session_seed_owned",
        record_type="session_seed",
        surface=QuerySessionCustodySurface.SEED,
        required=True,
        must_precede=("input_accepted", "context_snapshot", "query_entry_packet"),
        description="CodeWorkerSessionStore must own the initial session seed.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="input_owned",
        record_type="input_accepted",
        surface=QuerySessionCustodySurface.INPUT,
        required=True,
        must_follow=("session_seed",),
        must_precede=("context_snapshot", "query_entry_packet"),
        description="Accepted input records must be persisted before query entry.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="context_owned",
        record_type="context_snapshot",
        surface=QuerySessionCustodySurface.CONTEXT,
        required=True,
        must_follow=("session_seed", "input_accepted"),
        must_precede=("query_control_state", "query_entry_packet"),
        description="ContextAssemblyRuntime snapshot must be persisted before query entry.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="control_owned",
        record_type="query_control_state",
        surface=QuerySessionCustodySurface.CONTROL,
        required=True,
        must_follow=("context_snapshot",),
        must_precede=("query_entry_packet",),
        description="Interrupt/cancel/resume/checkpoint state must be persisted before query entry.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="entry_packet_owned",
        record_type="query_entry_packet",
        surface=QuerySessionCustodySurface.QUERY_ENTRY,
        required=True,
        must_follow=("query_control_state", "context_snapshot"),
        description="QueryEntryPacket must be persisted as the 02B to 02C handoff source.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="checkpoint_owned",
        record_type="query_checkpoint",
        surface=QuerySessionCustodySurface.CHECKPOINT,
        required=checkpoint_requested,
        must_follow=("query_control_state",),
        must_precede=("query_entry_packet",),
        description="Requested query checkpoints must be persisted in session store.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="resume_restored",
        record_type="query_control_state",
        surface=QuerySessionCustodySurface.RESUME,
        required=resume_requested,
        description="Resume requests must be reflected in query control state.",
    )
    yield QuerySessionCustodyRequirement(
        requirement_id="engine_attached",
        record_type="query_engine_attached",
        surface=QuerySessionCustodySurface.ENGINE_ATTACH,
        required=after_engine,
        must_follow=("query_entry_packet",),
        description="After QueryEngine runs, store must link the Zyra seed to QuerySession resume token.",
    )


def validate_custody_requirements(
    records: Sequence[QuerySessionCustodyRecord],
    requirements: Sequence[QuerySessionCustodyRequirement],
) -> Iterable[QuerySessionCustodyFinding]:
    counts: dict[str, int] = {}
    for record in records:
        counts[record.record_type] = counts.get(record.record_type, 0) + 1
    for requirement in requirements:
        count = counts.get(requirement.record_type, 0)
        if requirement.required and count < requirement.min_count:
            yield QuerySessionCustodyFinding(
                code=f"{requirement.requirement_id}_missing",
                severity=QuerySessionCustodySeverity.BLOCKER,
                surface=requirement.surface,
                message=f"Missing required session custody record: {requirement.record_type}.",
                metadata={**requirement.to_dict(), "observed_count": count},
            )
        elif count:
            yield QuerySessionCustodyFinding(
                code=f"{requirement.requirement_id}_ready",
                severity=QuerySessionCustodySeverity.PASS,
                surface=requirement.surface,
                message=f"Session custody record is present: {requirement.record_type}.",
                metadata={"observed_count": count},
            )


def validate_custody_order(records: Sequence[QuerySessionCustodyRecord]) -> Iterable[QuerySessionCustodyFinding]:
    first = first_record_sequences(records)
    for left, right in (
        ("session_seed", "input_accepted"),
        ("input_accepted", "context_snapshot"),
        ("context_snapshot", "query_control_state"),
        ("query_control_state", "query_entry_packet"),
        ("query_entry_packet", "query_engine_attached"),
    ):
        if left in first and right in first and first[left] > first[right]:
            yield QuerySessionCustodyFinding(
                code=f"{left}_after_{right}",
                severity=QuerySessionCustodySeverity.BLOCKER,
                surface=QuerySessionCustodySurface.STORE,
                message=f"Session custody order is invalid: {left} appears after {right}.",
                metadata={"left_sequence": first[left], "right_sequence": first[right]},
            )


def validate_resume_custody(
    records: Sequence[QuerySessionCustodyRecord],
    packet: Mapping[str, Any],
) -> Iterable[QuerySessionCustodyFinding]:
    resume = _as_mapping(packet.get("resume"))
    if resume.get("requested") is not True:
        return
    if not resume.get("parent_uuid") and not resume.get("resume_token"):
        yield QuerySessionCustodyFinding(
            code="resume_identity_not_restored",
            severity=QuerySessionCustodySeverity.BLOCKER,
            surface=QuerySessionCustodySurface.RESUME,
            message="Resume was requested but parent_uuid/resume_token were not restored.",
            metadata=dict(resume),
        )
    if not any(record.record_type == "query_control_state" for record in records):
        yield QuerySessionCustodyFinding(
            code="resume_control_state_not_persisted",
            severity=QuerySessionCustodySeverity.BLOCKER,
            surface=QuerySessionCustodySurface.RESUME,
            message="Resume was requested but query_control_state was not persisted.",
        )


def validate_checkpoint_custody(
    records: Sequence[QuerySessionCustodyRecord],
    packet: Mapping[str, Any],
) -> Iterable[QuerySessionCustodyFinding]:
    checkpoint = _as_mapping(packet.get("checkpoint"))
    status = str(checkpoint.get("status") or "")
    if status in {"", "not_requested"}:
        return
    if not any(record.record_type == "query_checkpoint" for record in records):
        yield QuerySessionCustodyFinding(
            code="checkpoint_record_not_persisted",
            severity=QuerySessionCustodySeverity.BLOCKER,
            surface=QuerySessionCustodySurface.CHECKPOINT,
            message="Checkpoint was requested but query_checkpoint record is missing.",
            metadata=dict(checkpoint),
        )
    if checkpoint.get("ok") is False:
        yield QuerySessionCustodyFinding(
            code="checkpoint_not_ok",
            severity=QuerySessionCustodySeverity.BLOCKER,
            surface=QuerySessionCustodySurface.CHECKPOINT,
            message="Checkpoint record exists but checkpoint runtime reported failure.",
            metadata=dict(checkpoint),
        )


def validate_integration_custody(
    records: Sequence[QuerySessionCustodyRecord],
    integration_report: Mapping[str, Any],
) -> Iterable[QuerySessionCustodyFinding]:
    if integration_report.get("ok") is True and not any(record.record_type == "query_entry_packet" for record in records):
        yield QuerySessionCustodyFinding(
            code="integration_ready_without_packet_record",
            severity=QuerySessionCustodySeverity.BLOCKER,
            surface=QuerySessionCustodySurface.QUERY_ENTRY,
            message="Integration report is ready but session store lacks query_entry_packet.",
            metadata=dict(integration_report),
        )
    elif integration_report.get("ok") is True:
        yield QuerySessionCustodyFinding(
            code="integration_packet_custody_ready",
            severity=QuerySessionCustodySeverity.PASS,
            surface=QuerySessionCustodySurface.QUERY_ENTRY,
            message="Integration report and session store packet custody agree.",
        )


def first_record_sequences(records: Sequence[QuerySessionCustodyRecord]) -> dict[str, int]:
    indices: dict[str, int] = {}
    for record in records:
        indices.setdefault(record.record_type, record.sequence)
    return indices


def custody_status_from_findings(
    findings: Sequence[QuerySessionCustodyFinding],
) -> QuerySessionCustodyStatus:
    if any(finding.blocking for finding in findings):
        return QuerySessionCustodyStatus.BLOCKED
    if any(finding.severity == QuerySessionCustodySeverity.WARNING for finding in findings):
        return QuerySessionCustodyStatus.DEGRADED
    return QuerySessionCustodyStatus.READY


def query_resume_custody_metadata(report: QuerySessionResumeCustodyReport | None) -> dict[str, str]:
    if report is None:
        return {
            "query_custody_ok": "false",
            "query_custody_status": "missing",
            "query_custody_blockers": "1",
        }
    return report.metadata_values()


def render_query_resume_custody_markdown(report: QuerySessionResumeCustodyReport) -> str:
    lines = [
        "## Query Session Resume Custody",
        "",
        f"- report_id: `{report.report_id}`",
        f"- ok: `{str(report.ok).lower()}`",
        f"- status: `{report.status}`",
        f"- record_count: `{len(report.records)}`",
        f"- resume_requested: `{str(report.resume_requested).lower()}`",
        f"- checkpoint_requested: `{str(report.checkpoint_requested).lower()}`",
        f"- engine_attached: `{str(report.engine_attached).lower()}`",
        f"- blocker_count: `{report.blocker_count}`",
        "",
        "### Records",
        "",
    ]
    lines.extend(
        f"- `{record.sequence}` `{record.record_type}` ok=`{str(record.ok).lower()}` fingerprint=`{record.fingerprint}`"
        for record in report.records
    )
    lines.extend(["", "### Findings", ""])
    lines.extend(
        f"- `{finding.severity}` `{finding.surface}` `{finding.code}`: {finding.message}"
        for finding in report.findings
    )
    return "\n".join(lines)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
