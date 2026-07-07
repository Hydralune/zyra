from __future__ import annotations

import ast
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_models import to_jsonable


class CustodySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class CustodyCode(StrEnum):
    STATE_CLAIM_PRESENT = "STATE_CLAIM_PRESENT"
    STATE_CLAIM_MISSING = "STATE_CLAIM_MISSING"
    MUTATION_API_PRESENT = "MUTATION_API_PRESENT"
    MUTATION_API_MISSING = "MUTATION_API_MISSING"
    EVENT_BINDING_PRESENT = "EVENT_BINDING_PRESENT"
    EVENT_BINDING_MISSING = "EVENT_BINDING_MISSING"
    STORE_PATH_PRESENT = "STORE_PATH_PRESENT"
    STORE_PATH_MISSING = "STORE_PATH_MISSING"
    OWNER_MODULE_PRESENT = "OWNER_MODULE_PRESENT"
    OWNER_MODULE_MISSING = "OWNER_MODULE_MISSING"
    ROOT_SOURCE_DEPENDENCY_RISK = "ROOT_SOURCE_DEPENDENCY_RISK"
    CACHE_ONLY_STATE_RISK = "CACHE_ONLY_STATE_RISK"
    CUSTODY_MATRIX_READY = "CUSTODY_MATRIX_READY"


class CustodySurface(StrEnum):
    LEDGER = "ledger"
    EVENT_LOG = "event_log"
    SQLITE = "sqlite"
    ARTIFACT = "artifact"
    PERMISSION = "permission"
    WORKSPACE = "workspace"
    SNAPSHOT = "snapshot"
    MUTATION_JOURNAL = "mutation_journal"
    SOURCE_SCAN = "source_scan"
    LINE_COUNT = "line_count"


@dataclass(slots=True)
class CustodyClaim:
    state_name: str
    surface: CustodySurface
    owner_module: str
    store_path_function: str
    mutation_api: str
    event_binding: str
    test_binding: str
    recovery_path: str = ""
    cleanup_policy: str = ""
    notes: str = ""

    @property
    def complete(self) -> bool:
        return all([self.owner_module, self.store_path_function, self.mutation_api, self.event_binding, self.test_binding])

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["complete"] = self.complete
        return payload


@dataclass(slots=True)
class CustodyModuleSignal:
    module_path: str
    exists: bool
    functions: list[str] = field(default_factory=list)
    classes: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    constants: list[str] = field(default_factory=list)
    path_literals: list[str] = field(default_factory=list)
    event_literals: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CustodyFinding:
    code: CustodyCode
    severity: CustodySeverity
    message: str
    state_name: str = ""
    module_path: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.severity in {CustodySeverity.ERROR, CustodySeverity.BLOCKER}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class StateCustodyReport:
    ok: bool
    claims: list[CustodyClaim]
    module_signals: list[CustodyModuleSignal]
    findings: list[CustodyFinding]
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CustodySeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CustodySeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == CustodySeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def default_custody_claims() -> list[CustodyClaim]:
    return [
        CustodyClaim(
            state_name="internalization_ledger",
            surface=CustodySurface.LEDGER,
            owner_module="packages/integrations/zyra_integrations/ledger_store.py",
            store_path_function="project_ledger_path",
            mutation_api="LedgerWorkflow.advance / AtomicLedgerStore.save_atomic / POST /ledger/entries",
            event_binding="integration_ledger_update",
            test_binding="tests/integration/test_internalization_ledger_api.py",
            recovery_path="Atomic revision files under tmp/ledger-revisions",
            cleanup_policy="Project ledger is persistent; tests isolate with ZYRA_INTEGRATION_LEDGER.",
        ),
        CustodyClaim(
            state_name="ledger_mutation_journal",
            surface=CustodySurface.MUTATION_JOURNAL,
            owner_module="packages/integrations/zyra_integrations/ledger_persistence.py",
            store_path_function="AtomicLedgerStore.journal_path",
            mutation_api="AtomicLedgerStore.save_atomic",
            event_binding="integration_ledger_update",
            test_binding="tests/integration/test_internalization_ledger_api.py",
            recovery_path="LedgerRevision metadata plus JSONL journal",
            cleanup_policy="Journal stays in tmp and is excluded from line-count evidence.",
        ),
        CustodyClaim(
            state_name="event_log",
            surface=CustodySurface.EVENT_LOG,
            owner_module="apps/api/zyra_api/main.py",
            store_path_function="event_log_path",
            mutation_api="persist_events",
            event_binding="EventRecord appended to JSONL and SQLiteStore",
            test_binding="tests/integration/test_internalization_ledger_api.py",
            recovery_path="SQLite event table and tmp/events.jsonl",
            cleanup_policy="Tests isolate with ZYRA_EVENT_LOG.",
        ),
        CustodyClaim(
            state_name="sqlite_runtime_state",
            surface=CustodySurface.SQLITE,
            owner_module="apps/api/zyra_api/main.py",
            store_path_function="sqlite_path",
            mutation_api="SQLiteStore.initialize / append_events",
            event_binding="EventRecord",
            test_binding="tests/integration/test_internalization_ledger_api.py",
            recovery_path="SQLiteStore loads events/tasks/artifacts from configured path.",
            cleanup_policy="Tests isolate with ZYRA_SQLITE_PATH.",
        ),
        CustodyClaim(
            state_name="artifact_store",
            surface=CustodySurface.ARTIFACT,
            owner_module="apps/api/zyra_api/main.py",
            store_path_function="artifact_root_path",
            mutation_api="LocalArtifactStore",
            event_binding="artifact refs in task events",
            test_binding="tests/integration/test_task_graph_api.py",
            recovery_path="Artifact root configured by ZYRA_ARTIFACT_ROOT.",
            cleanup_policy="Tests isolate artifact root under temp directory.",
        ),
        CustodyClaim(
            state_name="permission_store",
            surface=CustodySurface.PERMISSION,
            owner_module="apps/api/zyra_api/main.py",
            store_path_function="permission_store_path",
            mutation_api="JsonPermissionStore",
            event_binding="permission request/control events",
            test_binding="tests/unit/test_runtime_permissions.py",
            recovery_path="JSON permission file configured by ZYRA_PERMISSION_STORE.",
            cleanup_policy="Tests isolate with temp permission store.",
        ),
        CustodyClaim(
            state_name="tool_workspace",
            surface=CustodySurface.WORKSPACE,
            owner_module="apps/api/zyra_api/main.py",
            store_path_function="tool_workspace_path",
            mutation_api="GraphExecutionContext",
            event_binding="tool_result_event",
            test_binding="tests/integration/test_task_graph_api.py",
            recovery_path="Workspace root configured by ZYRA_TOOL_WORKSPACE.",
            cleanup_policy="Workspace is temp-scoped for tests and ignored for source audit.",
        ),
        CustodyClaim(
            state_name="ledger_snapshots",
            surface=CustodySurface.SNAPSHOT,
            owner_module="packages/integrations/zyra_integrations/ledger_snapshots.py",
            store_path_function="save_snapshot_to_default_dir",
            mutation_api="build_snapshot / save_snapshot",
            event_binding="ledger snapshot payload",
            test_binding="tests/integration/test_internalization_ledger_gate_cli.py",
            recovery_path="Snapshot files under tmp/ledger-snapshots",
            cleanup_policy="Snapshots are generated artifacts and excluded from effective line count.",
        ),
        CustodyClaim(
            state_name="source_scan_evidence",
            surface=CustodySurface.SOURCE_SCAN,
            owner_module="packages/integrations/zyra_integrations/ledger_source_scan.py",
            store_path_function="build_source_scan_report",
            mutation_api="source-scan CLI/API read-only report",
            event_binding="audit/source scan findings",
            test_binding="tests/integration/test_internalization_ledger_gate_cli.py",
            recovery_path="Regenerated from checked-in ledger and source tree.",
            cleanup_policy="No persisted state; report must not depend on parent source repos in cleanroom mode.",
        ),
        CustodyClaim(
            state_name="line_count_evidence",
            surface=CustodySurface.LINE_COUNT,
            owner_module="packages/integrations/zyra_integrations/ledger_linecount.py",
            store_path_function="git diff --numstat",
            mutation_api="linecount/buckets CLI/API read-only report",
            event_binding="completion gate findings",
            test_binding="tests/integration/test_internalization_ledger_gate_cli.py",
            recovery_path="Regenerated from Git diff and path policy.",
            cleanup_policy="Generated reports are not counted as source.",
        ),
    ]


def build_state_custody_report(project_root: Path, *, claims: Iterable[CustodyClaim] | None = None) -> StateCustodyReport:
    custody_claims = list(claims or default_custody_claims())
    module_signals = [inspect_custody_module(project_root, claim.owner_module) for claim in custody_claims]
    signal_by_path = {signal.module_path: signal for signal in module_signals}
    findings: list[CustodyFinding] = []
    for claim in custody_claims:
        findings.extend(validate_claim(project_root, claim, signal_by_path.get(claim.owner_module)))
    findings.append(
        CustodyFinding(
            code=CustodyCode.CUSTODY_MATRIX_READY,
            severity=CustodySeverity.INFO,
            message="State custody matrix was built from explicit Zyra-owned claims.",
            metadata={"claim_count": len(custody_claims)},
        )
    )
    ok = not any(finding.blocking for finding in findings)
    return StateCustodyReport(
        ok=ok,
        claims=custody_claims,
        module_signals=module_signals,
        findings=findings,
        summary=custody_summary(custody_claims, module_signals, findings),
    )


def inspect_custody_module(project_root: Path, module_path: str) -> CustodyModuleSignal:
    path = project_root / module_path
    if not path.exists() or not path.is_file():
        return CustodyModuleSignal(module_path=module_path, exists=False, errors=["module path does not exist"])
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError as error:
        return CustodyModuleSignal(module_path=module_path, exists=True, errors=[str(error)])
    signal = CustodyModuleSignal(module_path=module_path, exists=True)
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        signal.errors.append(str(error))
        return signal
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            signal.functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            signal.classes.append(node.name)
        elif isinstance(node, ast.Import):
            signal.imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            signal.imports.append(node.module or "")
        elif isinstance(node, ast.Assign):
            signal.constants.extend(_assignment_names(node))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value
            if "/" in value or "\\" in value or value.endswith(".json") or value.endswith(".sqlite3"):
                signal.path_literals.append(value)
            if value.startswith("integration_") or value.endswith("_event") or value in {"system_notice", "task_created"}:
                signal.event_literals.append(value)
    signal.functions = sorted(set(signal.functions))
    signal.classes = sorted(set(signal.classes))
    signal.imports = sorted(set(item for item in signal.imports if item))
    signal.constants = sorted(set(signal.constants))
    signal.path_literals = sorted(set(signal.path_literals))[:200]
    signal.event_literals = sorted(set(signal.event_literals))
    return signal


def validate_claim(project_root: Path, claim: CustodyClaim, signal: CustodyModuleSignal | None) -> list[CustodyFinding]:
    findings: list[CustodyFinding] = []
    if not claim.complete:
        findings.append(
            CustodyFinding(
                code=CustodyCode.STATE_CLAIM_MISSING,
                severity=CustodySeverity.ERROR,
                message=f"State custody claim is incomplete: {claim.state_name}",
                state_name=claim.state_name,
                module_path=claim.owner_module,
                remediation="Populate owner module, store path, mutation API, event binding, and test binding.",
                metadata=claim.to_dict(),
            )
        )
    else:
        findings.append(info_finding(CustodyCode.STATE_CLAIM_PRESENT, claim, "State custody claim is complete."))
    if signal is None or not signal.exists:
        findings.append(
            CustodyFinding(
                code=CustodyCode.OWNER_MODULE_MISSING,
                severity=CustodySeverity.ERROR,
                message=f"Owner module is missing: {claim.owner_module}",
                state_name=claim.state_name,
                module_path=claim.owner_module,
                remediation="Move state custody implementation into checked-in Zyra source.",
            )
        )
        return findings
    findings.append(info_finding(CustodyCode.OWNER_MODULE_PRESENT, claim, "Owner module exists."))
    if claim.store_path_function and function_or_class_present(signal, claim.store_path_function):
        findings.append(info_finding(CustodyCode.STORE_PATH_PRESENT, claim, "Store path resolver is present."))
    elif claim.surface in {CustodySurface.SOURCE_SCAN, CustodySurface.LINE_COUNT}:
        findings.append(info_finding(CustodyCode.STORE_PATH_PRESENT, claim, "Read-only regenerated report has no persisted store path."))
    else:
        findings.append(
            CustodyFinding(
                code=CustodyCode.STORE_PATH_MISSING,
                severity=CustodySeverity.ERROR,
                message=f"Store path resolver was not found in {claim.owner_module}: {claim.store_path_function}",
                state_name=claim.state_name,
                module_path=claim.owner_module,
                remediation="Expose a stable resolver for this persisted state.",
                metadata=signal.to_dict(),
            )
        )
    if mutation_api_present(signal, claim.mutation_api):
        findings.append(info_finding(CustodyCode.MUTATION_API_PRESENT, claim, "Mutation API signal is present."))
    else:
        findings.append(
            CustodyFinding(
                code=CustodyCode.MUTATION_API_MISSING,
                severity=CustodySeverity.WARNING,
                message=f"Mutation API signal was not obvious in {claim.owner_module}: {claim.mutation_api}",
                state_name=claim.state_name,
                module_path=claim.owner_module,
                remediation="Add direct mutation API evidence or adjust claim.",
                metadata=signal.to_dict(),
            )
        )
    if event_binding_present(signal, claim.event_binding):
        findings.append(info_finding(CustodyCode.EVENT_BINDING_PRESENT, claim, "Event binding signal is present."))
    elif claim.surface in {CustodySurface.SOURCE_SCAN, CustodySurface.LINE_COUNT, CustodySurface.SNAPSHOT}:
        findings.append(info_finding(CustodyCode.EVENT_BINDING_PRESENT, claim, "Read-only/generated state is bound through audit/gate payloads."))
    else:
        findings.append(
            CustodyFinding(
                code=CustodyCode.EVENT_BINDING_MISSING,
                severity=CustodySeverity.WARNING,
                message=f"Event binding signal was not obvious in {claim.owner_module}: {claim.event_binding}",
                state_name=claim.state_name,
                module_path=claim.owner_module,
                remediation="Record or emit a concrete event for state mutation.",
                metadata=signal.to_dict(),
            )
        )
    findings.extend(path_risk_findings(claim, signal))
    return findings


def info_finding(code: CustodyCode, claim: CustodyClaim, message: str) -> CustodyFinding:
    return CustodyFinding(
        code=code,
        severity=CustodySeverity.INFO,
        message=message,
        state_name=claim.state_name,
        module_path=claim.owner_module,
        metadata={"surface": str(claim.surface)},
    )


def path_risk_findings(claim: CustodyClaim, signal: CustodyModuleSignal) -> list[CustodyFinding]:
    findings: list[CustodyFinding] = []
    forbidden_fragments = ["/claude-code-best", "\\claude-code-best", "/browser-use", "\\browser-use", "/OpenHands", "\\OpenHands"]
    for literal in signal.path_literals:
        if any(fragment in literal for fragment in forbidden_fragments):
            findings.append(
                CustodyFinding(
                    code=CustodyCode.ROOT_SOURCE_DEPENDENCY_RISK,
                    severity=CustodySeverity.BLOCKER,
                    message="State owner module contains a parent source repository path literal.",
                    state_name=claim.state_name,
                    module_path=claim.owner_module,
                    remediation="Move runtime dependency into zyra or use a checked-in productized runtime path.",
                    metadata={"literal": literal},
                )
            )
        if ".cache" in literal or "__pycache__" in literal:
            findings.append(
                CustodyFinding(
                    code=CustodyCode.CACHE_ONLY_STATE_RISK,
                    severity=CustodySeverity.WARNING,
                    message="State owner module references cache-like storage.",
                    state_name=claim.state_name,
                    module_path=claim.owner_module,
                    remediation="Do not rely on cache artifacts for unit completion evidence.",
                    metadata={"literal": literal},
                )
            )
    return findings


def function_or_class_present(signal: CustodyModuleSignal, expected: str) -> bool:
    candidates = tokenize_api_claim(expected)
    if not candidates:
        return False
    names = set(signal.functions) | set(signal.classes)
    return any(candidate in names for candidate in candidates)


def mutation_api_present(signal: CustodyModuleSignal, mutation_api: str) -> bool:
    candidates = tokenize_api_claim(mutation_api)
    names = set(signal.functions) | set(signal.classes)
    if any(candidate in names for candidate in candidates):
        return True
    lowered_constants = " ".join(signal.constants).lower()
    lowered_literals = " ".join(signal.path_literals + signal.event_literals).lower()
    return any(candidate.lower() in lowered_constants or candidate.lower() in lowered_literals for candidate in candidates)


def event_binding_present(signal: CustodyModuleSignal, event_binding: str) -> bool:
    if not event_binding:
        return False
    fragments = tokenize_api_claim(event_binding)
    literal_text = " ".join(signal.event_literals + signal.path_literals + signal.constants).lower()
    return any(fragment.lower() in literal_text for fragment in fragments if len(fragment) > 3)


def tokenize_api_claim(value: str) -> list[str]:
    tokens: list[str] = []
    current = ""
    for char in value:
        if char.isalnum() or char == "_":
            current += char
        else:
            if current:
                tokens.append(current)
            current = ""
    if current:
        tokens.append(current)
    ignore = {"and", "or", "to", "by", "in", "under", "with", "api", "json", "jsonl", "event", "events", "payload"}
    return [token for token in tokens if token.lower() not in ignore]


def custody_summary(
    claims: list[CustodyClaim],
    module_signals: list[CustodyModuleSignal],
    findings: list[CustodyFinding],
) -> dict[str, Any]:
    by_surface: dict[str, int] = {}
    by_code: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for claim in claims:
        by_surface[str(claim.surface)] = by_surface.get(str(claim.surface), 0) + 1
    for finding in findings:
        by_code[str(finding.code)] = by_code.get(str(finding.code), 0) + 1
        by_severity[str(finding.severity)] = by_severity.get(str(finding.severity), 0) + 1
    return {
        "claim_count": len(claims),
        "complete_claims": sum(1 for claim in claims if claim.complete),
        "module_count": len(module_signals),
        "missing_modules": sum(1 for signal in module_signals if not signal.exists),
        "by_surface": dict(sorted(by_surface.items())),
        "findings_by_code": dict(sorted(by_code.items())),
        "findings_by_severity": dict(sorted(by_severity.items())),
        "function_signal_count": sum(len(signal.functions) for signal in module_signals),
        "event_signal_count": sum(len(signal.event_literals) for signal in module_signals),
        "path_signal_count": sum(len(signal.path_literals) for signal in module_signals),
    }


def state_custody_payload(report: StateCustodyReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {CustodySeverity.ERROR, CustodySeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == CustodySeverity.WARNING
    ]
    return payload


def assert_state_custody(report: StateCustodyReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.state_name}: {finding.message}"
        for finding in report.findings
        if finding.blocking
    )
    raise AssertionError(f"State custody failed:\n{formatted}")


def _assignment_names(node: ast.Assign) -> list[str]:
    names: list[str] = []
    for target in node.targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
        elif isinstance(target, ast.Tuple):
            for item in target.elts:
                if isinstance(item, ast.Name):
                    names.append(item.id)
    return names
