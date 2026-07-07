from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_events import event_record_from_mutation
from .ledger_models import InternalizationLedgerEntry, LedgerLifecycle, MainPathStatus, MigrationStrategy, RuntimeEntry, TestEntry, to_jsonable
from .ledger_persistence import AtomicLedgerStore, validate_entry_for_persistence
from .ledger_store import InternalizationLedger, LedgerMutation
from .ledger_workflow import LedgerAdvanceRequest, LedgerWorkflow


class MutationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class MutationCode(StrEnum):
    INVALID_ENTRY_BLOCKED = "INVALID_ENTRY_BLOCKED"
    INVALID_ENTRY_NOT_BLOCKED = "INVALID_ENTRY_NOT_BLOCKED"
    ADVANCE_MUTATION_CREATED = "ADVANCE_MUTATION_CREATED"
    ADVANCE_MUTATION_MISSING = "ADVANCE_MUTATION_MISSING"
    REVISION_WRITTEN = "REVISION_WRITTEN"
    REVISION_MISSING = "REVISION_MISSING"
    JOURNAL_WRITTEN = "JOURNAL_WRITTEN"
    JOURNAL_MISSING = "JOURNAL_MISSING"
    EVENT_PAYLOAD_CREATED = "EVENT_PAYLOAD_CREATED"
    EVENT_PAYLOAD_MISSING = "EVENT_PAYLOAD_MISSING"
    POLICY_REJECTED_BAD_TRANSITION = "POLICY_REJECTED_BAD_TRANSITION"
    POLICY_ALLOWED_GOOD_TRANSITION = "POLICY_ALLOWED_GOOD_TRANSITION"
    STORED_LEDGER_ROUNDTRIP = "STORED_LEDGER_ROUNDTRIP"
    STORED_LEDGER_MISMATCH = "STORED_LEDGER_MISMATCH"
    MUTATION_CAUSAL_CHAIN_READY = "MUTATION_CAUSAL_CHAIN_READY"


@dataclass(slots=True)
class MutationProbe:
    name: str
    description: str
    enabled_signal: str
    disabled_signal: str
    passed: bool
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class MutationFinding:
    code: MutationCode
    severity: MutationSeverity
    message: str
    probe: str = ""
    ledger_id: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {MutationSeverity.ERROR, MutationSeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class MutationConsistencyReport:
    ok: bool
    probes: list[MutationProbe]
    findings: list[MutationFinding]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MutationSeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MutationSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == MutationSeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def build_mutation_consistency_report(project_root: Path) -> MutationConsistencyReport:
    probes: list[MutationProbe] = []
    findings: list[MutationFinding] = []
    probe_functions = [
        probe_invalid_entry_blocked,
        probe_advance_creates_mutation,
        probe_atomic_store_writes_revision,
        probe_event_payload_from_mutation,
        probe_bad_transition_rejected,
        probe_store_roundtrip,
    ]
    for probe_function in probe_functions:
        try:
            probe, probe_findings = probe_function(project_root)
        except Exception as error:
            name = getattr(probe_function, "__name__", "unknown")
            probe = MutationProbe(
                name=name,
                description="Mutation consistency probe raised an exception.",
                enabled_signal="probe executes",
                disabled_signal="probe crashes",
                passed=False,
                error=str(error),
            )
            probe_findings = [
                MutationFinding(
                    code=MutationCode.MUTATION_CAUSAL_CHAIN_READY,
                    severity=MutationSeverity.ERROR,
                    message=f"Mutation probe raised exception: {error}",
                    probe=name,
                    remediation="Fix probe or underlying mutation behavior.",
                )
            ]
        probes.append(probe)
        findings.extend(probe_findings)
    findings.append(
        MutationFinding(
            code=MutationCode.MUTATION_CAUSAL_CHAIN_READY,
            severity=MutationSeverity.INFO if all(probe.passed for probe in probes) else MutationSeverity.WARNING,
            message="Mutation causal-chain probes completed.",
            metadata={"passed": sum(1 for probe in probes if probe.passed), "total": len(probes)},
        )
    )
    ok = not any(finding.blocking for finding in findings)
    return MutationConsistencyReport(ok=ok, probes=probes, findings=findings, summary=mutation_summary(probes, findings))


def probe_invalid_entry_blocked(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    ledger = InternalizationLedger([valid_probe_entry()])
    invalid = InternalizationLedgerEntry(
        ledger_id="invalid",
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="invalid",
        capability_summary="invalid probe entry",
        target_bindings=[],
        migration_strategy="adapter",
        main_path_status="api_connected",
        lifecycle="active",
    )
    validation = validate_entry_for_persistence(project_root, ledger, invalid, strict=False)
    passed = not validation.ok and ledger.get("invalid") is None
    finding = MutationFinding(
        code=MutationCode.INVALID_ENTRY_BLOCKED if passed else MutationCode.INVALID_ENTRY_NOT_BLOCKED,
        severity=MutationSeverity.INFO if passed else MutationSeverity.BLOCKER,
        message="Invalid entry was rejected before upsert." if passed else "Invalid entry would pass persistence validation.",
        probe="invalid_entry_blocked",
        ledger_id="invalid",
        remediation="Call validate_entry_for_persistence before ledger.upsert.",
        metadata=validation.to_dict(),
    )
    return (
        MutationProbe(
            name="invalid_entry_blocked",
            description="Persistence validation blocks invalid active/API-connected entries.",
            enabled_signal="validation ok=false and ledger remains unchanged",
            disabled_signal="bad entry reaches ledger store",
            passed=passed,
            evidence=validation.to_dict(),
        ),
        [finding],
    )


def probe_advance_creates_mutation(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    entry = valid_probe_entry()
    entry.lifecycle = LedgerLifecycle.PLANNED
    entry.main_path_status = MainPathStatus.PLANNED
    ledger = InternalizationLedger([entry])
    request = LedgerAdvanceRequest(
        ledger_id=entry.ledger_id,
        lifecycle=LedgerLifecycle.IN_PROGRESS,
        reason="mutation consistency probe",
        actor="mutation-probe",
        write_event=False,
    )
    result = LedgerWorkflow(project_root, ledger, strict_audit=False).advance(request)
    passed = result.mutation is not None and ledger.get(entry.ledger_id).lifecycle == LedgerLifecycle.IN_PROGRESS and result.message == "advanced"
    finding = MutationFinding(
        code=MutationCode.ADVANCE_MUTATION_CREATED if passed else MutationCode.ADVANCE_MUTATION_MISSING,
        severity=MutationSeverity.INFO if passed else MutationSeverity.ERROR,
        message="LedgerWorkflow.advance created mutation and changed ledger state." if passed else "LedgerWorkflow.advance did not create expected mutation.",
        probe="advance_creates_mutation",
        ledger_id=entry.ledger_id,
        remediation="Ensure workflow creates PersistedMutation only after state mutation.",
        metadata=result.to_dict(),
    )
    return (
        MutationProbe(
            name="advance_creates_mutation",
            description="Workflow transition mutates entry and returns mutation metadata.",
            enabled_signal="result.ok with mutation and changed lifecycle",
            disabled_signal="transition only logs acknowledgement",
            passed=passed,
            evidence=result.to_dict(),
        ),
        [finding],
    )


def probe_atomic_store_writes_revision(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = AtomicLedgerStore(root)
        ledger = InternalizationLedger([valid_probe_entry()])
        mutation = LedgerMutation(
            action="update",
            ledger_id=ledger.entries()[0].ledger_id,
            before={},
            after=ledger.entries()[0].to_dict(),
        )
        revision = store.save_atomic(ledger, actor="mutation-probe", mutations=[mutation])
        loaded = store.load()
        journal_exists = store.journal_path.exists()
        revision_exists = Path(revision.path).exists()
    passed = revision_exists and journal_exists and loaded.get(ledger.entries()[0].ledger_id) is not None
    findings = [
        MutationFinding(
            code=MutationCode.REVISION_WRITTEN if revision_exists else MutationCode.REVISION_MISSING,
            severity=MutationSeverity.INFO if revision_exists else MutationSeverity.ERROR,
            message="Atomic revision file was written." if revision_exists else "Atomic revision file missing.",
            probe="atomic_store_writes_revision",
            ledger_id=ledger.entries()[0].ledger_id,
            remediation="Persist revision metadata alongside ledger writes.",
            metadata=revision.to_dict(),
        ),
        MutationFinding(
            code=MutationCode.JOURNAL_WRITTEN if journal_exists else MutationCode.JOURNAL_MISSING,
            severity=MutationSeverity.INFO if journal_exists else MutationSeverity.ERROR,
            message="Mutation journal was written." if journal_exists else "Mutation journal missing.",
            probe="atomic_store_writes_revision",
            ledger_id=ledger.entries()[0].ledger_id,
            remediation="Write mutation journal for auditability.",
            metadata={"journal_path": str(store.journal_path)},
        ),
    ]
    return (
        MutationProbe(
            name="atomic_store_writes_revision",
            description="AtomicLedgerStore writes ledger, revision metadata, and mutation journal.",
            enabled_signal="ledger reloads and revision/journal files exist",
            disabled_signal="adapter returns success without durable metadata",
            passed=passed,
            evidence={"revision": revision.to_dict(), "journal_exists": journal_exists, "revision_exists": revision_exists},
        ),
        findings,
    )


def probe_event_payload_from_mutation(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    entry = valid_probe_entry()
    mutation = LedgerMutation(
        action="update",
        ledger_id=entry.ledger_id,
        before={"lifecycle": "planned"},
        after={"lifecycle": "in_progress"},
    )
    event = event_record_from_mutation(mutation, trigger="mutation_probe")
    payload = to_jsonable(event)
    event_payload = payload.get("payload", {}).get("integration_ledger_update", {})
    passed = event_payload.get("ledger_id") == entry.ledger_id and payload.get("event_type") == "system_notice"
    finding = MutationFinding(
        code=MutationCode.EVENT_PAYLOAD_CREATED if passed else MutationCode.EVENT_PAYLOAD_MISSING,
        severity=MutationSeverity.INFO if passed else MutationSeverity.ERROR,
        message="Mutation event payload records ledger id and mutation metadata." if passed else "Mutation event payload is missing required metadata.",
        probe="event_payload_from_mutation",
        ledger_id=entry.ledger_id,
        remediation="Write integration_ledger_update event for mutation visibility.",
        metadata=payload,
    )
    return (
        MutationProbe(
            name="event_payload_from_mutation",
            description="PersistedMutation produces event-log payload.",
            enabled_signal="integration_ledger_update appears in event payload",
            disabled_signal="mutation hidden from event stream",
            passed=passed,
            evidence=payload,
        ),
        [finding],
    )


def probe_bad_transition_rejected(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    entry = valid_probe_entry()
    entry.lifecycle = LedgerLifecycle.PLANNED
    entry.main_path_status = MainPathStatus.PLANNED
    ledger = InternalizationLedger([entry])
    request = LedgerAdvanceRequest(
        ledger_id=entry.ledger_id,
        lifecycle=LedgerLifecycle.PRODUCTIZED,
        reason="bad transition probe",
        actor="mutation-probe",
        write_event=False,
    )
    result = LedgerWorkflow(project_root, ledger).advance(request)
    passed = not result.ok and ledger.get(entry.ledger_id).lifecycle == LedgerLifecycle.PLANNED
    finding = MutationFinding(
        code=MutationCode.POLICY_REJECTED_BAD_TRANSITION if passed else MutationCode.INVALID_ENTRY_NOT_BLOCKED,
        severity=MutationSeverity.INFO if passed else MutationSeverity.ERROR,
        message="Workflow policy rejected unsafe transition." if passed else "Unsafe transition was not rejected.",
        probe="bad_transition_rejected",
        ledger_id=entry.ledger_id,
        remediation="Enforce lifecycle policy before state mutation.",
        metadata=result.to_dict(),
    )
    return (
        MutationProbe(
            name="bad_transition_rejected",
            description="Workflow policy rejects direct planned-to-productized transitions.",
            enabled_signal="result.ok=false and lifecycle unchanged",
            disabled_signal="policy bypass allows invalid lifecycle",
            passed=passed,
            evidence=result.to_dict(),
        ),
        [finding],
    )


def probe_store_roundtrip(project_root: Path) -> tuple[MutationProbe, list[MutationFinding]]:
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "ledger.json"
        ledger = InternalizationLedger([valid_probe_entry()])
        ledger.save(path)
        loaded = InternalizationLedger.load(path)
    entry = ledger.entries()[0]
    loaded_entry = loaded.get(entry.ledger_id)
    passed = loaded_entry is not None and loaded_entry.to_dict() == entry.to_dict()
    finding = MutationFinding(
        code=MutationCode.STORED_LEDGER_ROUNDTRIP if passed else MutationCode.STORED_LEDGER_MISMATCH,
        severity=MutationSeverity.INFO if passed else MutationSeverity.ERROR,
        message="Ledger store roundtrip preserved entry payload." if passed else "Ledger store roundtrip changed entry payload.",
        probe="store_roundtrip",
        ledger_id=entry.ledger_id,
        remediation="Keep store serialization deterministic.",
    )
    return (
        MutationProbe(
            name="store_roundtrip",
            description="InternalizationLedger.save/load roundtrips entry JSON.",
            enabled_signal="loaded entry equals original entry",
            disabled_signal="store changes schema or loses fields",
            passed=passed,
            evidence={"ledger_id": entry.ledger_id},
        ),
        [finding],
    )


def valid_probe_entry() -> InternalizationLedgerEntry:
    return InternalizationLedgerEntry.new(
        source_repo="claude-code-best",
        source_path="src/QueryEngine.ts",
        capability_name="mutation consistency probe",
        capability_summary="valid probe entry for mutation consistency audit",
        target_paths=["packages/integrations/zyra_integrations/ledger_models.py"],
        migration_strategy=MigrationStrategy.ADAPTER,
        main_path_status=MainPathStatus.API_CONNECTED,
        lifecycle=LedgerLifecycle.ACTIVE,
        owner_unit="M1-01A",
        milestone="M1",
        runtime_entry=RuntimeEntry(module="zyra_integrations.ledger_models", function="InternalizationLedgerEntry"),
        test_entries=[TestEntry(path="tests/unit/test_internalization_ledger_control_plane.py", command="python -m unittest")],
    )


def mutation_summary(probes: list[MutationProbe], findings: list[MutationFinding]) -> dict[str, Any]:
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for finding in findings:
        by_code[str(finding.code)] = by_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
    return {
        "probe_count": len(probes),
        "passing_probes": sum(1 for probe in probes if probe.passed),
        "failing_probes": sum(1 for probe in probes if not probe.passed),
        "findings_by_code": dict(sorted(by_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
    }


def mutation_consistency_payload(report: MutationConsistencyReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {MutationSeverity.ERROR, MutationSeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == MutationSeverity.WARNING
    ]
    return payload


def assert_mutation_consistency(report: MutationConsistencyReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.probe}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"Ledger mutation consistency failed:\n{formatted}")
