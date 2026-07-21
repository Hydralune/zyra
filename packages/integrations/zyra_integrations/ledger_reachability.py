from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import textwrap
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable

from .ledger_audit import InternalizationLedgerAuditor
from .ledger_models import InternalizationLedgerEntry, to_jsonable
from .ledger_policy import CONNECTED_STATUSES, MATERIALIZED_LIFECYCLES, classify_path
from .ledger_store import InternalizationLedger


class ReachabilitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


class ReachabilityCode(StrEnum):
    API_ROUTE_DECLARED = "API_ROUTE_DECLARED"
    API_ROUTE_MISSING = "API_ROUTE_MISSING"
    CLI_COMMAND_DECLARED = "CLI_COMMAND_DECLARED"
    CLI_COMMAND_MISSING = "CLI_COMMAND_MISSING"
    EVENT_TYPE_DECLARED = "EVENT_TYPE_DECLARED"
    EVENT_TYPE_MISSING = "EVENT_TYPE_MISSING"
    TEST_COMMAND_DECLARED = "TEST_COMMAND_DECLARED"
    TEST_COMMAND_MISSING = "TEST_COMMAND_MISSING"
    RUNTIME_ENTRY_IMPORTABLE = "RUNTIME_ENTRY_IMPORTABLE"
    RUNTIME_ENTRY_MISSING = "RUNTIME_ENTRY_MISSING"
    RUNTIME_ENTRY_NOT_IMPORTABLE = "RUNTIME_ENTRY_NOT_IMPORTABLE"
    TARGET_PATH_MATERIALIZED = "TARGET_PATH_MATERIALIZED"
    TARGET_PATH_MISSING = "TARGET_PATH_MISSING"
    LEDGER_ENTRY_REACHABLE = "LEDGER_ENTRY_REACHABLE"
    LEDGER_ENTRY_UNREACHABLE = "LEDGER_ENTRY_UNREACHABLE"
    SURFACE_HAS_TEST = "SURFACE_HAS_TEST"
    SURFACE_LACKS_TEST = "SURFACE_LACKS_TEST"
    DISCONNECT_PROBE_READY = "DISCONNECT_PROBE_READY"
    DISCONNECT_PROBE_MISSING = "DISCONNECT_PROBE_MISSING"


@dataclass(slots=True)
class RouteProbe:
    route: str
    method: str
    declared_by: str
    implemented: bool
    handler: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class CliProbe:
    command: str
    implemented: bool
    parser_path: str
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class EventProbe:
    event_type: str
    implemented: bool
    producer: str = ""
    payload_key: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class RuntimeProbe:
    module: str
    function: str
    command: str
    importable: bool
    callable: bool
    health_check: str = ""
    error: str = ""
    evidence: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.module and self.function:
            return self.importable and self.callable
        if self.module:
            return self.importable
        if self.command:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TestProbe:
    path: str
    command: str
    exists: bool
    command_mentions_path: bool
    kind: str = ""
    expected_signal: str = ""

    @property
    def ok(self) -> bool:
        return self.exists and (not self.command or self.command_mentions_path)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class TargetProbe:
    target_path: str
    exists: bool
    effective: bool
    surface: str
    reason: str
    required_for_status: bool = False

    @property
    def ok(self) -> bool:
        return self.exists or not self.required_for_status

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class EntryReachability:
    ledger_id: str
    owner_unit: str
    source_repo: str
    source_path: str
    capability_name: str
    lifecycle: str
    main_path_status: str
    reachable: bool
    route_probes: list[RouteProbe] = field(default_factory=list)
    cli_probes: list[CliProbe] = field(default_factory=list)
    event_probes: list[EventProbe] = field(default_factory=list)
    runtime_probe: RuntimeProbe | None = None
    test_probes: list[TestProbe] = field(default_factory=list)
    target_probes: list[TargetProbe] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ReachabilityFinding:
    code: ReachabilityCode
    severity: ReachabilitySeverity
    message: str
    ledger_id: str = ""
    owner_unit: str = ""
    surface: str = ""
    remediation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(slots=True)
class ReachabilityReport:
    ok: bool
    project_root: str
    total_entries: int
    reachable_entries: int
    unreachable_entries: int
    connected_entries: int
    materialized_entries: int
    route_count: int
    cli_command_count: int
    event_type_count: int
    findings: list[ReachabilityFinding] = field(default_factory=list)
    entries: list[EntryReachability] = field(default_factory=list)
    routes: list[RouteProbe] = field(default_factory=list)
    cli_commands: list[CliProbe] = field(default_factory=list)
    event_types: list[EventProbe] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def blocker_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReachabilitySeverity.BLOCKER)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReachabilitySeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == ReachabilitySeverity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        payload = to_jsonable(self)
        payload["blocker_count"] = self.blocker_count
        payload["error_count"] = self.error_count
        payload["warning_count"] = self.warning_count
        return payload


def build_reachability_report(
    project_root: Path,
    ledger: InternalizationLedger,
    *,
    owner_unit: str = "",
    include_entries: bool = True,
    strict_audit: bool = True,
) -> ReachabilityReport:
    api_routes = discover_api_routes(project_root)
    cli_commands = discover_cli_commands(project_root)
    event_types = discover_event_producers(project_root)
    entries = ledger.by_owner_unit(owner_unit) if owner_unit else ledger.entries()
    reachability = [
        build_entry_reachability(
            project_root,
            entry,
            api_routes=api_routes,
            cli_commands=cli_commands,
            event_types=event_types,
        )
        for entry in entries
    ]
    findings: list[ReachabilityFinding] = []
    for item in reachability:
        findings.extend(_findings_for_entry(item))
    findings.extend(_surface_test_findings(reachability))
    audit = InternalizationLedgerAuditor(project_root, strict=strict_audit).audit(ledger)
    if not audit.ok:
        findings.append(
            ReachabilityFinding(
                code=ReachabilityCode.LEDGER_ENTRY_UNREACHABLE,
                severity=ReachabilitySeverity.ERROR,
                message="Ledger audit has errors; reachability evidence cannot be trusted.",
                remediation="Fix strict audit errors before using reachability as completion evidence.",
                metadata={"error_count": audit.error_count, "blocker_count": audit.blocker_count},
            )
        )
    reachable_entries = sum(1 for item in reachability if item.reachable)
    materialized_entries = sum(1 for entry in entries if entry.lifecycle in MATERIALIZED_LIFECYCLES)
    connected_entries = sum(1 for entry in entries if entry.main_path_status in CONNECTED_STATUSES)
    ok = not any(finding.severity in {ReachabilitySeverity.ERROR, ReachabilitySeverity.BLOCKER} for finding in findings)
    return ReachabilityReport(
        ok=ok,
        project_root=str(project_root),
        total_entries=len(entries),
        reachable_entries=reachable_entries,
        unreachable_entries=len(entries) - reachable_entries,
        connected_entries=connected_entries,
        materialized_entries=materialized_entries,
        route_count=len(api_routes),
        cli_command_count=len(cli_commands),
        event_type_count=len(event_types),
        findings=findings,
        entries=reachability if include_entries else [],
        routes=api_routes,
        cli_commands=cli_commands,
        event_types=event_types,
        summary={
            "by_owner_unit": _count_by(reachability, "owner_unit"),
            "by_source_repo": _count_by(reachability, "source_repo"),
            "unreachable_by_owner_unit": _count_by([item for item in reachability if not item.reachable], "owner_unit"),
            "route_methods": _route_method_counts(api_routes),
        },
    )


def build_entry_reachability(
    project_root: Path,
    entry: InternalizationLedgerEntry,
    *,
    api_routes: list[RouteProbe],
    cli_commands: list[CliProbe],
    event_types: list[EventProbe],
) -> EntryReachability:
    route_lookup = {_route_key(route.method, route.route): route for route in api_routes}
    cli_lookup = {command.command: command for command in cli_commands}
    event_lookup = {event.event_type: event for event in event_types}
    route_probes = [_route_probe_for_binding(binding, route_lookup) for binding in entry.main_path.api_routes]
    cli_probes = [_cli_probe_for_binding(binding, cli_lookup) for binding in entry.main_path.control_commands]
    event_probes = [_event_probe_for_binding(binding, event_lookup) for binding in entry.main_path.event_types]
    runtime_probe = build_runtime_probe(entry, project_root=project_root)
    test_probes = [build_test_probe(project_root, test_entry.path, test_entry.command, test_entry.kind, test_entry.expected_signal) for test_entry in entry.test_entries]
    target_probes = [build_target_probe(project_root, entry, target) for target in entry.target_paths]
    reasons: list[str] = []
    warnings: list[str] = []
    if route_probes and not any(route.implemented for route in route_probes):
        reasons.append("declared API routes are not implemented")
    if cli_probes and not any(command.implemented for command in cli_probes):
        reasons.append("declared control commands are not implemented")
    if event_probes and not any(event.implemented for event in event_probes):
        reasons.append("declared event types are not produced")
    if entry.runtime_entry.is_empty():
        if entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES:
            reasons.append("runtime entry is required but empty")
        else:
            warnings.append("runtime entry is empty for planned entry")
    elif runtime_probe and not runtime_probe.ok:
        reasons.append("runtime entry is not importable or callable")
    if entry.test_entries and not any(test.ok for test in test_probes):
        reasons.append("test entries do not point at existing tests")
    if entry.lifecycle in MATERIALIZED_LIFECYCLES and not any(target.exists for target in target_probes):
        reasons.append("materialized entry has no existing target")
    if entry.main_path_status in CONNECTED_STATUSES and entry.main_path.is_empty():
        reasons.append("connected status has no main-path binding")
    if not target_probes:
        reasons.append("entry has no target probes")
    reachable = not reasons and (
        any(target.exists for target in target_probes)
        or any(route.implemented for route in route_probes)
        or any(command.implemented for command in cli_probes)
        or any(event.implemented for event in event_probes)
        or (runtime_probe.ok if runtime_probe else False)
    )
    return EntryReachability(
        ledger_id=entry.ledger_id,
        owner_unit=entry.owner_unit,
        source_repo=entry.source_repo,
        source_path=entry.source_path,
        capability_name=entry.capability_name,
        lifecycle=str(entry.lifecycle),
        main_path_status=str(entry.main_path_status),
        reachable=reachable,
        route_probes=route_probes,
        cli_probes=cli_probes,
        event_probes=event_probes,
        runtime_probe=runtime_probe,
        test_probes=test_probes,
        target_probes=target_probes,
        reasons=sorted(set(reasons)),
        warnings=sorted(set(warnings)),
    )


def discover_api_routes(project_root: Path) -> list[RouteProbe]:
    main_path = project_root / "apps" / "api" / "zyra_api" / "main.py"
    if not main_path.exists():
        return []
    text = main_path.read_text(encoding="utf-8", errors="ignore")
    probes: list[RouteProbe] = []
    for method_name, method in [("do_GET", "GET"), ("do_POST", "POST")]:
        method_body = _function_source(text, method_name)
        for route in _literal_routes_from_source(method_body):
            probes.append(
                RouteProbe(
                    route=route,
                    method=method,
                    declared_by="apps/api/zyra_api/main.py",
                    implemented=True,
                    handler=method_name,
                    evidence=[f"{method_name}:{route}"],
                )
            )
        for helper in _route_helper_names(method_body):
            routes = _routes_for_helper(text, helper)
            for route in routes:
                probes.append(
                    RouteProbe(
                        route=route,
                        method=method,
                        declared_by="apps/api/zyra_api/main.py",
                        implemented=True,
                        handler=helper,
                        evidence=[f"{method_name}:{helper}", f"{helper}:{route}"],
                    )
                )
    for method, route in _route_manifest_from_source(text, "ZYRA_DYNAMIC_API_ROUTES"):
        probes.append(
            RouteProbe(
                route=route,
                method=method,
                declared_by="apps/api/zyra_api/main.py",
                implemented=True,
                handler="ZyraRequestHandler",
                evidence=[f"ZYRA_DYNAMIC_API_ROUTES:{method} {route}"],
            )
        )
    facade_path = project_root / "apps" / "api" / "zyra_api" / "mcp_api.py"
    facade_is_connected = (
        facade_path.exists()
        and "McpApiFacade" in text
        and ".handle_get(" in text
        and ".handle_post(" in text
    )
    if facade_is_connected:
        facade_text = facade_path.read_text(encoding="utf-8", errors="ignore")
        for method, route in _route_manifest_from_source(facade_text, "MCP_API_ROUTES"):
            probes.append(
                RouteProbe(
                    route=route,
                    method=method,
                    declared_by="apps/api/zyra_api/mcp_api.py",
                    implemented=True,
                    handler="McpApiFacade",
                    evidence=[
                        "main.py:McpApiFacade.handle_get/handle_post",
                        f"MCP_API_ROUTES:{method} {route}",
                    ],
                )
            )
    return _dedupe_routes(probes)


def discover_cli_commands(project_root: Path) -> list[CliProbe]:
    parser_path = project_root / "packages" / "integrations" / "zyra_integrations" / "ledger_cli.py"
    if not parser_path.exists():
        return []
    commands: list[str] = []
    try:
        from .ledger_cli import build_parser

        parser = build_parser()
        commands = list(_parser_subcommands(parser))
    except Exception:
        text = parser_path.read_text(encoding="utf-8", errors="ignore")
        commands = _subparser_names_from_text(text)
    probes: list[CliProbe] = []
    for command in sorted(set(commands)):
        probes.append(
            CliProbe(
                command=command,
                implemented=True,
                parser_path="packages/integrations/zyra_integrations/ledger_cli.py",
                evidence=[f"argparse:{command}"],
            )
        )
        probes.append(
            CliProbe(
                command=f"ledger:{command}",
                implemented=True,
                parser_path="scripts/zyra_integration_ledger.py",
                evidence=[f"argparse:{command}", f"alias:ledger:{command}"],
            )
        )
    api_path = project_root / "apps" / "api" / "zyra_api" / "main.py"
    if api_path.exists():
        api_text = api_path.read_text(encoding="utf-8", errors="ignore")
        command_source = _function_source(api_text, "_command_result_for_event")
        for command in _slash_commands_from_source(command_source):
            probes.append(
                CliProbe(
                    command=command,
                    implemented=True,
                    parser_path="apps/api/zyra_api/main.py",
                    evidence=[f"_command_result_for_event:{command}"],
                )
            )
    return probes


def discover_event_producers(project_root: Path) -> list[EventProbe]:
    probes: list[EventProbe] = []
    event_enum_values = _event_enum_values(project_root)
    for path in [
        project_root / "packages" / "integrations" / "zyra_integrations" / "ledger_events.py",
        project_root / "packages" / "integrations" / "zyra_integrations" / "source_extraction.py",
        project_root / "packages" / "runtime" / "zyra_runtime" / "scaffold.py",
        project_root / "packages" / "runtime" / "zyra_runtime" / "scaffold_lifecycle.py",
        project_root / "packages" / "workers" / "zyra_workers" / "scaffold_bridge_runtime.py",
        project_root / "packages" / "memory" / "zyra_memory" / "curator_store.py",
        project_root / "packages" / "memory" / "zyra_memory" / "curator_runtime.py",
        project_root / "packages" / "memory" / "zyra_memory" / "curator_commit.py",
        project_root / "packages" / "workers" / "zyra_workers" / "memory_curator_integration.py",
        project_root / "packages" / "integrations" / "zyra_integrations" / "mcp" / "events.py",
        project_root / "apps" / "api" / "zyra_api" / "main.py",
        project_root / "packages" / "runtime" / "claude-runtime" / "src" / "query-engine.ts",
    ]:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for payload_key in [
            "integration_ledger_audit",
            "integration_ledger_update",
            "integration_ledger_acceptance",
            "integration_ledger_boundary",
            "runtime_scaffold_health",
            "runtime_scaffold_lifecycle",
            "source_extraction_completed",
            "worker_bridge_probe",
        ]:
            if payload_key in text:
                probes.append(
                    EventProbe(
                        event_type=payload_key,
                        implemented=True,
                        producer=_relative(project_root, path),
                        payload_key=payload_key,
                        evidence=[payload_key],
                    )
                )
                probes.append(
                    EventProbe(
                        event_type="system_notice",
                        implemented=True,
                        producer=_relative(project_root, path),
                        payload_key=payload_key,
                        evidence=[payload_key],
                    )
                )
        if "EventType.SYSTEM_NOTICE" in text:
            probes.append(
                EventProbe(
                    event_type="system_notice",
                    implemented=True,
                    producer=_relative(project_root, path),
                    payload_key="EventType.SYSTEM_NOTICE",
                    evidence=["EventRecord"],
                )
            )
        for event_type in _typescript_emit_event_names(text):
            probes.append(
                EventProbe(
                    event_type=event_type,
                    implemented=True,
                    producer=_relative(project_root, path),
                    payload_key=event_type,
                    evidence=[f'emit("{event_type}")'],
                )
            )
        for event_type in _python_event_type_names(text):
            probes.append(
                EventProbe(
                    event_type=event_type,
                    implemented=True,
                    producer=_relative(project_root, path),
                    payload_key="event_type",
                    evidence=[f'event_type="{event_type}"'],
                )
            )
        for member_name, event_type in event_enum_values.items():
            if member_name not in text:
                continue
            probes.append(
                EventProbe(
                    event_type=event_type,
                    implemented=True,
                    producer=_relative(project_root, path),
                    payload_key=member_name,
                    evidence=[f"EventType.{member_name}"],
                )
            )
    return _dedupe_events(probes)


def build_runtime_probe(
    entry: InternalizationLedgerEntry,
    *,
    project_root: Path | None = None,
) -> RuntimeProbe:
    runtime = entry.runtime_entry
    if runtime.is_empty():
        return RuntimeProbe(
            module="",
            function="",
            command="",
            health_check="",
            importable=False,
            callable=False,
            error="runtime entry is empty",
        )
    if runtime.module:
        if project_root is not None and runtime.module.startswith("@"):
            workspace = _typescript_workspace_for_module(project_root, runtime.module)
            if workspace is not None:
                callable_export = _typescript_export_exists(workspace, runtime.function)
                return RuntimeProbe(
                    module=runtime.module,
                    function=runtime.function,
                    command=runtime.command,
                    health_check=runtime.health_check,
                    importable=True,
                    callable=callable_export,
                    error="" if callable_export or not runtime.function else "TypeScript export is not declared",
                    evidence=[
                        f"workspace:{_relative(project_root, workspace)}",
                        f"export:{runtime.function}" if runtime.function else "module-only",
                    ],
                )
        try:
            module = importlib.import_module(runtime.module)
            target = getattr(module, runtime.function) if runtime.function else None
            return RuntimeProbe(
                module=runtime.module,
                function=runtime.function,
                command=runtime.command,
                health_check=runtime.health_check,
                importable=True,
                callable=callable(target) if runtime.function else False,
                evidence=[f"import:{runtime.module}", f"getattr:{runtime.function}" if runtime.function else "module-only"],
            )
        except Exception as error:
            return RuntimeProbe(
                module=runtime.module,
                function=runtime.function,
                command=runtime.command,
                health_check=runtime.health_check,
                importable=False,
                callable=False,
                error=str(error),
            )
    return RuntimeProbe(
        module=runtime.module,
        function=runtime.function,
        command=runtime.command,
        health_check=runtime.health_check,
        importable=False,
        callable=False,
        evidence=["command-present"] if runtime.command else [],
    )


def build_test_probe(project_root: Path, path: str, command: str, kind: str, expected_signal: str) -> TestProbe:
    normalized = path.replace("\\", "/").strip()
    candidate = project_root / normalized
    exists = candidate.exists()
    command_mentions_path = not command or normalized.replace("/", ".").replace(".py", "") in command or normalized in command
    return TestProbe(
        path=normalized,
        command=command,
        exists=exists,
        command_mentions_path=command_mentions_path,
        kind=kind,
        expected_signal=expected_signal,
    )


def build_target_probe(project_root: Path, entry: InternalizationLedgerEntry, target_path: str) -> TargetProbe:
    classification = classify_path(target_path)
    exists = classification.is_project_relative and (project_root / classification.normalized_path).exists()
    required = entry.lifecycle in MATERIALIZED_LIFECYCLES or entry.main_path_status in CONNECTED_STATUSES
    return TargetProbe(
        target_path=target_path,
        exists=exists,
        effective=str(classification.verdict) == "effective",
        surface=str(classification.surface),
        reason=classification.reason,
        required_for_status=required,
    )


def assert_reachability(report: ReachabilityReport) -> None:
    if report.ok:
        return
    formatted = "\n".join(
        f"{finding.severity} {finding.code} {finding.ledger_id}: {finding.message}"
        for finding in report.findings
        if finding.severity in {ReachabilitySeverity.ERROR, ReachabilitySeverity.BLOCKER}
    )
    raise AssertionError(f"Ledger reachability failed:\n{formatted}")


def reachability_payload(report: ReachabilityReport) -> dict[str, Any]:
    payload = report.to_dict()
    payload["blocking_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity in {ReachabilitySeverity.ERROR, ReachabilitySeverity.BLOCKER}
    ]
    payload["warning_findings"] = [
        finding.to_dict()
        for finding in report.findings
        if finding.severity == ReachabilitySeverity.WARNING
    ]
    return payload


def _findings_for_entry(entry: EntryReachability) -> list[ReachabilityFinding]:
    findings: list[ReachabilityFinding] = []
    if entry.reachable:
        findings.append(
            ReachabilityFinding(
                code=ReachabilityCode.LEDGER_ENTRY_REACHABLE,
                severity=ReachabilitySeverity.INFO,
                message="Ledger entry has at least one reachable runtime, target, API, CLI, event, or test surface.",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
            )
        )
    elif entry.main_path_status in {str(status) for status in CONNECTED_STATUSES} or entry.lifecycle in {str(lifecycle) for lifecycle in MATERIALIZED_LIFECYCLES}:
        findings.append(
            ReachabilityFinding(
                code=ReachabilityCode.LEDGER_ENTRY_UNREACHABLE,
                severity=ReachabilitySeverity.ERROR,
                message=f"Entry is materialized/connected but not reachable: {', '.join(entry.reasons)}",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                remediation="Create the target/runtime/API/event/test binding or downgrade the ledger lifecycle.",
            )
        )
    for route in entry.route_probes:
        if not route.implemented:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.API_ROUTE_MISSING,
                    severity=ReachabilitySeverity.ERROR,
                    message=f"Declared API route is not implemented: {route.method} {route.route}",
                    ledger_id=entry.ledger_id,
                    owner_unit=entry.owner_unit,
                    surface=route.route,
                    remediation="Wire the route into apps/api/zyra_api/main.py or remove the declaration.",
                )
            )
    for command in entry.cli_probes:
        if not command.implemented:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.CLI_COMMAND_MISSING,
                    severity=ReachabilitySeverity.ERROR,
                    message=f"Declared CLI command is not implemented: {command.command}",
                    ledger_id=entry.ledger_id,
                    owner_unit=entry.owner_unit,
                    surface=command.command,
                    remediation="Add the command to ledger_cli.py or remove the declaration.",
                )
            )
    for event in entry.event_probes:
        if not event.implemented:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.EVENT_TYPE_MISSING,
                    severity=ReachabilitySeverity.ERROR,
                    message=f"Declared event type is not produced: {event.event_type}",
                    ledger_id=entry.ledger_id,
                    owner_unit=entry.owner_unit,
                    surface=event.event_type,
                    remediation="Add event producer code or downgrade the status.",
                )
            )
    if entry.runtime_probe and entry.runtime_probe.error and not entry.runtime_probe.ok:
        severity = ReachabilitySeverity.WARNING
        if entry.lifecycle in {str(lifecycle) for lifecycle in MATERIALIZED_LIFECYCLES}:
            severity = ReachabilitySeverity.ERROR
        findings.append(
            ReachabilityFinding(
                code=ReachabilityCode.RUNTIME_ENTRY_NOT_IMPORTABLE,
                severity=severity,
                message=f"Runtime entry is not importable: {entry.runtime_probe.error}",
                ledger_id=entry.ledger_id,
                owner_unit=entry.owner_unit,
                surface=entry.runtime_probe.module,
                remediation="Fix runtime_entry.module/function or mark the entry planned.",
            )
        )
    for target in entry.target_probes:
        if not target.ok:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.TARGET_PATH_MISSING,
                    severity=ReachabilitySeverity.ERROR,
                    message=f"Required target path does not exist: {target.target_path}",
                    ledger_id=entry.ledger_id,
                    owner_unit=entry.owner_unit,
                    surface=target.target_path,
                    remediation="Create the target path or downgrade the lifecycle/status.",
                )
            )
    return findings


def _surface_test_findings(entries: list[EntryReachability]) -> list[ReachabilityFinding]:
    findings: list[ReachabilityFinding] = []
    by_unit: dict[str, list[EntryReachability]] = {}
    for entry in entries:
        by_unit.setdefault(entry.owner_unit or "unassigned", []).append(entry)
    for owner_unit, unit_entries in sorted(by_unit.items()):
        if owner_unit == "unassigned":
            continue
        has_reachable = any(entry.reachable for entry in unit_entries)
        has_tests = any(any(test.ok for test in entry.test_probes) for entry in unit_entries)
        if has_reachable and has_tests:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.SURFACE_HAS_TEST,
                    severity=ReachabilitySeverity.INFO,
                    message=f"{owner_unit} has reachable entries with test coverage.",
                    owner_unit=owner_unit,
                )
            )
        elif has_reachable and not has_tests:
            findings.append(
                ReachabilityFinding(
                    code=ReachabilityCode.SURFACE_LACKS_TEST,
                    severity=ReachabilitySeverity.WARNING,
                    message=f"{owner_unit} has reachable entries but no reachable test entry.",
                    owner_unit=owner_unit,
                    remediation="Add test_entries that point at existing tests.",
                )
            )
    return findings


def _route_probe_for_binding(binding: str, route_lookup: dict[str, RouteProbe]) -> RouteProbe:
    method, route = _split_route_binding(binding)
    probe = route_lookup.get(_route_key(method, route))
    if probe:
        return probe
    return RouteProbe(route=route, method=method, declared_by="ledger", implemented=False)


def _cli_probe_for_binding(binding: str, cli_lookup: dict[str, CliProbe]) -> CliProbe:
    command = binding.strip()
    probe = cli_lookup.get(command)
    if probe:
        return probe
    normalized = command.lstrip("/")
    probe = cli_lookup.get(normalized)
    if probe:
        return probe
    if ":" in normalized:
        namespace, subcommand = normalized.split(":", 1)
        if namespace == "ledger":
            probe = cli_lookup.get(subcommand)
            if probe:
                return CliProbe(
                    command=command,
                    implemented=True,
                    parser_path=probe.parser_path,
                    evidence=[*probe.evidence, f"alias:{command}"],
                )
    return CliProbe(command=command, implemented=False, parser_path="packages/integrations/zyra_integrations/ledger_cli.py")


def _event_probe_for_binding(binding: str, event_lookup: dict[str, EventProbe]) -> EventProbe:
    event_type = binding.strip()
    probe = event_lookup.get(event_type)
    if probe:
        return probe
    return EventProbe(event_type=event_type, implemented=False)


def _split_route_binding(binding: str) -> tuple[str, str]:
    parts = binding.strip().split(maxsplit=1)
    if len(parts) == 2 and parts[0].upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        return parts[0].upper(), _normalize_route(parts[1])
    return "GET", _normalize_route(binding)


def _normalize_route(route: str) -> str:
    route = route.strip()
    if not route.startswith("/"):
        route = "/" + route
    return route.rstrip("/") or "/"


def _route_key(method: str, route: str) -> str:
    return f"{method.upper()} {_normalize_route(route)}"


def _function_source(source: str, name: str) -> str:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ""
    lines = source.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            start = node.lineno - 1
            end = getattr(node, "end_lineno", node.lineno)
            return textwrap.dedent("\n".join(lines[start:end]))
    return ""


def _literal_routes_from_source(source: str) -> list[str]:
    routes: list[str] = []
    for match in re_route_literals(source):
        routes.append(match)
    return routes


def re_route_literals(source: str) -> list[str]:
    routes: list[str] = []
    for prefix in ["parts ==", "parts in"]:
        if prefix not in source:
            continue
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return routes
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comparator in node.comparators:
                route = _route_from_list_literal(comparator)
                if route:
                    routes.append(route)
    return sorted(set(routes))


def _typescript_emit_event_names(source: str) -> list[str]:
    """Extract literal event names from the TypeScript runtime emit boundary."""

    return sorted(
        {
            match.group(1)
            for match in re.finditer(
                r"\bemit\s*\(\s*[\"']([A-Za-z0-9_.:-]+)[\"']",
                source,
            )
        }
    )


def _python_event_type_names(source: str) -> list[str]:
    """Extract literal values passed through a Python ``event_type`` field."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    values: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg == "event_type"
                    and isinstance(keyword.value, ast.Constant)
                    and isinstance(keyword.value.value, str)
                ):
                    values.add(keyword.value.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=False):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "event_type"
                    and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                ):
                    values.add(value.value)
    return sorted(values)


def _typescript_workspace_for_module(project_root: Path, module: str) -> Path | None:
    for manifest in project_root.glob("packages/**/package.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if str(payload.get("name") or "") == module:
            return manifest.parent
    return None


def _typescript_export_exists(workspace: Path, function: str) -> bool:
    if not function:
        return True
    declaration = re.compile(
        rf"\bexport\s+(?:default\s+)?(?:class|function|const|let|var)\s+{re.escape(function)}\b"
    )
    named_export = re.compile(rf"\bexport\s*\{{[^}}]*\b{re.escape(function)}\b", re.DOTALL)
    for path in workspace.glob("src/**/*.ts"):
        try:
            source = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if declaration.search(source) or named_export.search(source):
            return True
    return False


def _route_from_list_literal(node: ast.AST) -> str:
    if not isinstance(node, ast.List):
        return ""
    parts: list[str] = []
    for item in node.elts:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            parts.append(item.value)
        else:
            return ""
    return "/" + "/".join(parts)


def _route_helper_names(source: str) -> list[str]:
    names: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.startswith("_is_ledger"):
            names.append(node.func.id)
    return sorted(set(names))


def _routes_for_helper(source: str, helper: str) -> list[str]:
    helper_source = _function_source(source, helper)
    routes: list[str] = []
    try:
        tree = ast.parse(helper_source)
    except SyntaxError:
        return routes
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comparator in node.comparators:
                route = _route_from_list_literal(comparator)
                if route:
                    routes.append(route)
    return sorted(set(routes))


def _route_manifest_from_source(source: str, variable_name: str) -> list[tuple[str, str]]:
    """Read a literal ``(method, route)`` manifest without importing API code."""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in tree.body:
        target_name = ""
        value: ast.AST | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target_name = node.target.id
            value = node.value
        if target_name != variable_name or value is None:
            continue
        try:
            literal = ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError):
            return []
        routes: list[tuple[str, str]] = []
        if not isinstance(literal, (list, tuple)):
            return routes
        for item in literal:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            method, route = str(item[0]).upper(), str(item[1])
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"} and route.startswith("/"):
                routes.append((method, _normalize_route(route)))
        return routes
    return []


def _slash_commands_from_source(source: str) -> list[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    commands: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        value = node.value.strip()
        if value.startswith("/") and " " not in value and len(value) > 1:
            commands.add(value)
    return sorted(commands)


def _event_enum_values(project_root: Path) -> dict[str, str]:
    """Map statically declared EventType members to their wire values."""

    models_path = project_root / "packages" / "core" / "zyra_core" / "models.py"
    if not models_path.exists():
        return {}
    try:
        tree = ast.parse(models_path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:
        return {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "EventType":
            continue
        values: dict[str, str] = {}
        for statement in node.body:
            if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
                continue
            target = statement.targets[0]
            if not isinstance(target, ast.Name):
                continue
            if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                values[target.id] = statement.value.value
        return values
    return {}


def _dedupe_routes(routes: Iterable[RouteProbe]) -> list[RouteProbe]:
    result: dict[str, RouteProbe] = {}
    for route in routes:
        key = _route_key(route.method, route.route)
        existing = result.get(key)
        if existing is None:
            result[key] = route
        else:
            evidence = sorted(set(existing.evidence + route.evidence))
            result[key] = RouteProbe(
                route=existing.route,
                method=existing.method,
                declared_by=existing.declared_by,
                implemented=existing.implemented or route.implemented,
                handler=existing.handler or route.handler,
                evidence=evidence,
            )
    return [result[key] for key in sorted(result)]


def _dedupe_events(events: Iterable[EventProbe]) -> list[EventProbe]:
    result: dict[tuple[str, str], EventProbe] = {}
    for event in events:
        key = (event.event_type, event.payload_key)
        existing = result.get(key)
        if existing is None:
            result[key] = event
        else:
            result[key] = EventProbe(
                event_type=event.event_type,
                implemented=event.implemented or existing.implemented,
                producer=existing.producer or event.producer,
                payload_key=event.payload_key,
                evidence=sorted(set(existing.evidence + event.evidence)),
            )
    return [result[key] for key in sorted(result)]


def _parser_subcommands(parser: argparse.ArgumentParser) -> list[str]:
    commands: list[str] = []
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            commands.extend(str(command) for command in choices)
    return commands


def _subparser_names_from_text(text: str) -> list[str]:
    names: list[str] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return names
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "add_parser":
            continue
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            names.append(node.args[0].value)
    return names


def _count_by(entries: Iterable[EntryReachability], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for entry in entries:
        key = str(getattr(entry, field_name) or "unassigned")
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _route_method_counts(routes: Iterable[RouteProbe]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for route in routes:
        counts[route.method] = counts.get(route.method, 0) + 1
    return dict(sorted(counts.items()))


def _relative(project_root: Path, path: Path) -> str:
    try:
        return path.relative_to(project_root).as_posix()
    except ValueError:
        return path.as_posix()


def entry_has_disconnect_probe(entry: EntryReachability) -> bool:
    if entry.route_probes or entry.cli_probes or entry.event_probes:
        return True
    if entry.runtime_probe and entry.runtime_probe.ok:
        return True
    return any(test.ok for test in entry.test_probes)


def disconnect_probe_summary(report: ReachabilityReport) -> dict[str, Any]:
    entries = report.entries
    ready = [entry for entry in entries if entry_has_disconnect_probe(entry)]
    missing = [entry for entry in entries if not entry_has_disconnect_probe(entry)]
    return {
        "ready": len(ready),
        "missing": len(missing),
        "missing_entries": [entry.ledger_id for entry in missing[:100]],
        "ready_entries": [entry.ledger_id for entry in ready[:100]],
    }
