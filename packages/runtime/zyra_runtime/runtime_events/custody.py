"""Runtime custody and dependency-boundary audit for the event spine."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Iterable, Mapping

from .models import JsonValue


FORBIDDEN_SOURCE_SEGMENTS = {
    "vendor",
    "vendor-runtimes",
    "source-pool",
    "runtime-sources",
    "claude-code-best",
    "OpenHands",
    "opencode",
    "oh-my-pi",
}


@dataclass(frozen=True, slots=True)
class CustodyFinding:
    code: str
    severity: str
    message: str
    path: str | None = None
    owner: str | None = None

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "path": self.path,
            "owner": self.owner,
        }


@dataclass(frozen=True, slots=True)
class CustodyReport:
    passed: bool
    canonical_store_owner: str
    canonical_database: str
    projection_owner: str
    delivery_owner: str
    findings: tuple[CustodyFinding, ...]
    tables: tuple[str, ...]
    required_tables: tuple[str, ...]
    missing_tables: tuple[str, ...]

    def to_jsonable(self) -> dict[str, JsonValue]:
        return {
            "passed": self.passed,
            "canonicalStoreOwner": self.canonical_store_owner,
            "canonicalDatabase": self.canonical_database,
            "projectionOwner": self.projection_owner,
            "deliveryOwner": self.delivery_owner,
            "findings": [finding.to_jsonable() for finding in self.findings],
            "tables": list(self.tables),
            "requiredTables": list(self.required_tables),
            "missingTables": list(self.missing_tables),
        }


class RuntimeEventCustodyAudit:
    REQUIRED_TABLES = (
        "runtime_events",
        "runtime_event_aggregates",
        "runtime_event_global_sequence",
        "runtime_event_routes",
        "runtime_projector_cursors",
        "runtime_event_subscriptions",
        "runtime_event_deliveries",
        "runtime_event_dead_letters",
        "runtime_event_metrics",
    )

    def __init__(self, *, workspace_root: str | Path, database_path: str | Path) -> None:
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.database_path = Path(database_path).expanduser().resolve()

    def inspect(self, *, runtime_paths: Iterable[str | Path] = ()) -> CustodyReport:
        findings: list[CustodyFinding] = []
        self._inspect_database_location(findings)
        self._inspect_runtime_paths(runtime_paths, findings)
        tables = self._read_tables(findings)
        missing = tuple(table for table in self.REQUIRED_TABLES if table not in tables)
        for table in missing:
            findings.append(
                CustodyFinding(
                    code="runtime_event_table_missing",
                    severity="error",
                    message=f"canonical runtime event table is missing: {table}",
                    path=str(self.database_path),
                    owner="runtime-event-spine",
                )
            )
        findings.extend(self._inspect_schema_owners(tables))
        passed = not any(finding.severity == "error" for finding in findings)
        return CustodyReport(
            passed=passed,
            canonical_store_owner="packages/runtime/runtime-event-spine/src/sqlite-store.ts",
            canonical_database=str(self.database_path),
            projection_owner="packages/runtime/runtime-event-spine/src/projector.ts",
            delivery_owner="packages/runtime/runtime-event-spine/src/message-bus.ts",
            findings=tuple(findings),
            tables=tuple(sorted(tables)),
            required_tables=self.REQUIRED_TABLES,
            missing_tables=missing,
        )

    def _inspect_database_location(self, findings: list[CustodyFinding]) -> None:
        try:
            self.database_path.relative_to(self.workspace_root)
        except ValueError:
            findings.append(
                CustodyFinding(
                    code="runtime_event_database_outside_workspace",
                    severity="error",
                    message="canonical runtime event database is outside the Zyra workspace",
                    path=str(self.database_path),
                    owner="runtime-event-spine",
                )
            )
        lowered = {part.lower() for part in self.database_path.parts}
        forbidden = sorted(lowered.intersection({item.lower() for item in FORBIDDEN_SOURCE_SEGMENTS}))
        if forbidden:
            findings.append(
                CustodyFinding(
                    code="runtime_event_database_source_dependency",
                    severity="error",
                    message=f"canonical database path crosses forbidden source boundary: {', '.join(forbidden)}",
                    path=str(self.database_path),
                    owner="runtime-event-spine",
                )
            )

    def _inspect_runtime_paths(
        self,
        runtime_paths: Iterable[str | Path],
        findings: list[CustodyFinding],
    ) -> None:
        for raw_path in runtime_paths:
            path = Path(raw_path).expanduser().resolve()
            lowered = {part.lower() for part in path.parts}
            forbidden = sorted(lowered.intersection({item.lower() for item in FORBIDDEN_SOURCE_SEGMENTS}))
            if forbidden:
                findings.append(
                    CustodyFinding(
                        code="runtime_event_forbidden_runtime_path",
                        severity="error",
                        message=f"runtime path depends on source/vendor boundary: {', '.join(forbidden)}",
                        path=str(path),
                    )
                )
            try:
                path.relative_to(self.workspace_root)
            except ValueError:
                findings.append(
                    CustodyFinding(
                        code="runtime_event_external_runtime_path",
                        severity="error",
                        message="runtime event path is outside the Zyra workspace",
                        path=str(path),
                    )
                )

    def _read_tables(self, findings: list[CustodyFinding]) -> set[str]:
        if not self.database_path.exists():
            findings.append(
                CustodyFinding(
                    code="runtime_event_database_missing",
                    severity="error",
                    message="canonical runtime event database does not exist",
                    path=str(self.database_path),
                )
            )
            return set()
        try:
            connection = sqlite3.connect(f"file:{self.database_path}?mode=ro", uri=True)
            try:
                rows = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                ).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as error:
            findings.append(
                CustodyFinding(
                    code="runtime_event_database_unreadable",
                    severity="error",
                    message=f"canonical runtime event database cannot be inspected: {error}",
                    path=str(self.database_path),
                )
            )
            return set()
        return {str(row[0]) for row in rows}

    def _inspect_schema_owners(self, tables: set[str]) -> list[CustodyFinding]:
        findings: list[CustodyFinding] = []
        suspicious = sorted(
            table
            for table in tables
            if table.startswith("runtime_event") and table not in self.REQUIRED_TABLES
            and table not in {"runtime_event_artifact_refs"}
        )
        for table in suspicious:
            findings.append(
                CustodyFinding(
                    code="runtime_event_parallel_owner_candidate",
                    severity="warning",
                    message=f"table requires custody review because it may duplicate canonical state: {table}",
                    path=str(self.database_path),
                )
            )
        return findings


def assert_runtime_event_custody(report: CustodyReport) -> None:
    if report.passed:
        return
    errors = [finding.message for finding in report.findings if finding.severity == "error"]
    raise RuntimeError("runtime event custody audit failed: " + "; ".join(errors))
