from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .claude_runtime_contracts import ClaudeCleanRuntimeProbe, ClaudeRuntimeContractBundle, clean_runtime_probe


class CleanRuntimeCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    SKIP = "skip"


class CleanRuntimeDependencyKind(StrEnum):
    ROOT_SOURCE_REPO = "root_source_repo"
    VENDOR_RUNTIME = "vendor_runtime"
    NODE_SIDECAR = "node_sidecar"
    EDITABLE_INSTALL = "editable_install"
    ENV_PATH = "env_path"
    RELATIVE_PARENT = "relative_parent"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class CleanRuntimeCheck:
    check_id: str
    status: CleanRuntimeCheckStatus
    summary: str
    path: str = ""
    dependency_kind: CleanRuntimeDependencyKind = CleanRuntimeDependencyKind.UNKNOWN
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {CleanRuntimeCheckStatus.PASS, CleanRuntimeCheckStatus.SKIP}

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class CleanRuntimeDefaultPath:
    entrypoint: str
    runtime_id: str
    contract_source: str
    sidecar_used: bool
    source_repo_required: bool
    default_path_exercised: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.default_path_exercised and not self.sidecar_used and not self.source_repo_required

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class CleanRuntimeDisconnectEvidence:
    target: str
    disconnected: bool
    failed_as_expected: bool
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.disconnected and self.failed_as_expected

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class CleanRuntimeAuditReport:
    project_root: str
    source_workspace_root: str
    runtime_id: str
    contract_source: str
    default_path: CleanRuntimeDefaultPath
    probe: ClaudeCleanRuntimeProbe
    checks: list[CleanRuntimeCheck]
    disconnect_evidence: list[CleanRuntimeDisconnectEvidence] = field(default_factory=list)
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking_checks(self) -> list[CleanRuntimeCheck]:
        return [check for check in self.checks if check.status == CleanRuntimeCheckStatus.FAIL]

    @property
    def warnings(self) -> list[CleanRuntimeCheck]:
        return [check for check in self.checks if check.status == CleanRuntimeCheckStatus.WARN]

    @property
    def ok(self) -> bool:
        return self.default_path.ok and self.probe.ok and not self.blocking_checks and all(item.ok for item in self.disconnect_evidence)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "project_root": self.project_root,
            "source_workspace_root": self.source_workspace_root,
            "runtime_id": self.runtime_id,
            "contract_source": self.contract_source,
            "default_path": self.default_path.to_dict(),
            "probe": self.probe.to_dict(),
            "checks": [check.to_dict() for check in self.checks],
            "disconnect_evidence": [item.to_dict() for item in self.disconnect_evidence],
            "created_at": self.created_at,
            "metadata": to_jsonable(self.metadata),
            "blocking_count": len(self.blocking_checks),
            "warning_count": len(self.warnings),
        }


class ClaudeCleanRuntimeAuditor:
    """Audits whether the productized runtime can run without root source repos."""

    def __init__(
        self,
        *,
        project_root: str | Path,
        source_workspace_root: str | Path,
        contracts: ClaudeRuntimeContractBundle,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.source_workspace_root = Path(source_workspace_root).resolve()
        self.contracts = contracts

    def audit(
        self,
        *,
        sidecar_used: bool,
        default_path_exercised: bool,
        source_pool_only_primary_sources: int = 0,
        runtime_metadata: Mapping[str, Any] | None = None,
        disconnect_evidence: Sequence[CleanRuntimeDisconnectEvidence] = (),
    ) -> CleanRuntimeAuditReport:
        default_path = CleanRuntimeDefaultPath(
            entrypoint="packages/workers/zyra_workers/code_worker_runtime.py",
            runtime_id=self.contracts.runtime_id,
            contract_source=self.contracts.contract_source,
            sidecar_used=sidecar_used,
            source_repo_required=False,
            default_path_exercised=default_path_exercised,
            metadata=dict(runtime_metadata or {}),
        )
        probe = clean_runtime_probe(
            project_root=self.project_root,
            source_workspace_root=self.source_workspace_root,
            sidecar_used=sidecar_used,
            default_path_exercised=default_path_exercised,
            source_pool_only_primary_sources=source_pool_only_primary_sources,
            extra_metadata={str(k): str(v) for k, v in dict(runtime_metadata or {}).items()},
        )
        checks = [
            self._check_project_root(),
            self._check_source_workspace_is_clean(),
            self._check_no_parent_source_dependency(),
            self._check_contracts_sidecar_free(),
            self._check_default_path(default_path),
            *self._scan_runtime_files_for_parent_refs(),
            *self._scan_environment_path_refs(),
        ]
        return CleanRuntimeAuditReport(
            project_root=str(self.project_root),
            source_workspace_root=str(self.source_workspace_root),
            runtime_id=self.contracts.runtime_id,
            contract_source=self.contracts.contract_source,
            default_path=default_path,
            probe=probe,
            checks=checks,
            disconnect_evidence=list(disconnect_evidence),
            metadata={
                "runtime_metadata": dict(runtime_metadata or {}),
                "source_pool_only_primary_sources": source_pool_only_primary_sources,
            },
        )

    def write_report_artifact(
        self,
        report: CleanRuntimeAuditReport,
        *,
        artifact_store: LocalArtifactStore,
        run_id: str,
        task_id: str,
        producer_node_id: str | None = None,
    ) -> ArtifactRef:
        return artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            title="Claude clean runtime audit",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
        )

    def _check_project_root(self) -> CleanRuntimeCheck:
        ok = self.project_root.exists() and (self.project_root / "packages").exists()
        return CleanRuntimeCheck(
            check_id="project_root",
            status=CleanRuntimeCheckStatus.PASS if ok else CleanRuntimeCheckStatus.FAIL,
            summary="project root contains Zyra packages" if ok else "project root is missing Zyra packages",
            path=str(self.project_root),
        )

    def _check_source_workspace_is_clean(self) -> CleanRuntimeCheck:
        source_repos = [
            self.source_workspace_root / "claude-code-best",
            self.source_workspace_root / "browser-use",
            self.source_workspace_root / "OpenHands",
        ]
        present = [str(path) for path in source_repos if path.exists()]
        return CleanRuntimeCheck(
            check_id="source_workspace_clean",
            status=CleanRuntimeCheckStatus.FAIL if present else CleanRuntimeCheckStatus.PASS,
            summary="source workspace has no root source repositories" if not present else "source workspace still contains root source repositories",
            path=str(self.source_workspace_root),
            dependency_kind=CleanRuntimeDependencyKind.ROOT_SOURCE_REPO,
            metadata={"present": present},
        )

    def _check_no_parent_source_dependency(self) -> CleanRuntimeCheck:
        parent = self.project_root.parent
        parent_sources = [parent / "claude-code-best", parent / "browser-use", parent / "OpenHands"]
        present = [str(path) for path in parent_sources if path.exists()]
        status = CleanRuntimeCheckStatus.WARN if present else CleanRuntimeCheckStatus.PASS
        return CleanRuntimeCheck(
            check_id="parent_source_repo_present",
            status=status,
            summary="parent source repos exist but are not required by clean runtime" if present else "no parent source repos detected",
            path=str(parent),
            dependency_kind=CleanRuntimeDependencyKind.RELATIVE_PARENT,
            metadata={"present": present, "blocking": False},
        )

    def _check_contracts_sidecar_free(self) -> CleanRuntimeCheck:
        clean = self.contracts.default_path.get("cleanRuntime") if isinstance(self.contracts.default_path, Mapping) else None
        if not isinstance(clean, Mapping):
            clean = self.contracts.inventory.get("cleanRuntime") if isinstance(self.contracts.inventory, Mapping) else None
        if not isinstance(clean, Mapping):
            clean = self.contracts.health.get("cleanRuntime") if isinstance(self.contracts.health, Mapping) else {}
        ok = (
            clean.get("requiresRootSourceRepo") is False
            and clean.get("requiresNodeSidecar") is False
            and clean.get("requiresVendorRuntime") is False
            and self.contracts.clean_runtime_safe
        )
        return CleanRuntimeCheck(
            check_id="contract_sidecar_free",
            status=CleanRuntimeCheckStatus.PASS if ok else CleanRuntimeCheckStatus.FAIL,
            summary="productized runtime contracts are sidecar-free" if ok else "productized runtime contracts still require sidecar/source/vendor",
            dependency_kind=CleanRuntimeDependencyKind.NODE_SIDECAR,
            metadata={"clean_runtime": to_jsonable(clean)},
        )

    def _check_default_path(self, default_path: CleanRuntimeDefaultPath) -> CleanRuntimeCheck:
        return CleanRuntimeCheck(
            check_id="default_path_exercised",
            status=CleanRuntimeCheckStatus.PASS if default_path.ok else CleanRuntimeCheckStatus.FAIL,
            summary="default CodeWorker path used productized runtime" if default_path.ok else "default CodeWorker path did not prove productized runtime",
            path=default_path.entrypoint,
            dependency_kind=CleanRuntimeDependencyKind.NODE_SIDECAR if default_path.sidecar_used else CleanRuntimeDependencyKind.UNKNOWN,
            metadata=default_path.to_dict(),
        )

    def _scan_runtime_files_for_parent_refs(self) -> list[CleanRuntimeCheck]:
        files = [
            self.project_root / "packages" / "workers" / "zyra_workers" / "code_worker_runtime.py",
            self.project_root / "packages" / "workers" / "zyra_workers" / "typescript_claude_runtime.py",
            self.project_root / "packages" / "runtime" / "claude-runtime" / "src" / "query-engine.ts",
            self.project_root / "packages" / "runtime" / "claude-runtime" / "src" / "stdio.ts",
            self.project_root / "packages" / "runtime" / "zyra_runtime" / "claude_query_engine_runtime.py",
            self.project_root / "packages" / "runtime" / "zyra_runtime" / "claude_runtime_contracts.py",
            self.project_root / "scripts" / "verify_claude_productization_foundation.py",
        ]
        checks: list[CleanRuntimeCheck] = []
        claude_repo = "claude-code-best"
        forbidden = [
            "../" + claude_repo,
            "..\\" + claude_repo,
            f'ROOT.parent / "{claude_repo}"',
            "vendor-runtimes/claude-code-runtime/productized",
            "vendor/claude-code-best",
        ]
        for path in files:
            if not path.exists():
                checks.append(
                    CleanRuntimeCheck(
                        check_id=f"runtime_file_missing:{path.name}",
                        status=CleanRuntimeCheckStatus.FAIL,
                        summary="runtime file is missing",
                        path=str(path),
                    )
                )
                continue
            content = path.read_text(encoding="utf-8", errors="ignore")
            matches = [pattern for pattern in forbidden if pattern in content]
            checks.append(
                CleanRuntimeCheck(
                    check_id=f"runtime_file_parent_refs:{path.name}",
                    status=CleanRuntimeCheckStatus.FAIL if matches else CleanRuntimeCheckStatus.PASS,
                    summary="runtime file has no forbidden parent/source-pool refs" if not matches else "runtime file contains forbidden parent/source-pool refs",
                    path=str(path),
                    dependency_kind=CleanRuntimeDependencyKind.RELATIVE_PARENT,
                    metadata={"matches": matches},
                )
            )
        return checks

    def _scan_environment_path_refs(self) -> list[CleanRuntimeCheck]:
        path_values = {
            key: value
            for key, value in os.environ.items()
            if key.upper() in {"PYTHONPATH", "NODE_PATH", "PATH"} and isinstance(value, str)
        }
        findings: list[str] = []
        for value in path_values.values():
            lowered = value.lower()
            if "claude-code-best" in lowered or "vendor-runtimes" in lowered:
                findings.append(value)
        status = CleanRuntimeCheckStatus.WARN if findings else CleanRuntimeCheckStatus.PASS
        return [
            CleanRuntimeCheck(
                check_id="environment_source_paths",
                status=status,
                summary="environment does not expose source repo paths" if not findings else "environment mentions source repo paths",
                dependency_kind=CleanRuntimeDependencyKind.ENV_PATH,
                metadata={"findings": findings, "checked_keys": sorted(path_values.keys())},
            )
        ]


def disconnect_evidence_from_worker_result(
    *,
    target: str,
    ok: bool,
    error: str | None,
    expected_error: str,
    metadata: Mapping[str, Any] | None = None,
) -> CleanRuntimeDisconnectEvidence:
    return CleanRuntimeDisconnectEvidence(
        target=target,
        disconnected=True,
        failed_as_expected=(not ok and error == expected_error),
        error=str(error or ""),
        metadata={"expected_error": expected_error, **dict(metadata or {})},
    )


def clean_runtime_metadata(report: CleanRuntimeAuditReport | None) -> dict[str, str]:
    if report is None:
        return {
            "clean_runtime_ok": "false",
            "clean_runtime_blocking_count": "",
            "clean_runtime_warning_count": "",
        }
    return {
        "clean_runtime_ok": str(report.ok).lower(),
        "clean_runtime_blocking_count": str(len(report.blocking_checks)),
        "clean_runtime_warning_count": str(len(report.warnings)),
        "clean_runtime_default_path_ok": str(report.default_path.ok).lower(),
        "clean_runtime_probe_ok": str(report.probe.ok).lower(),
        "clean_runtime_disconnect_checks": str(len(report.disconnect_evidence)),
    }


def clean_runtime_markdown(report: CleanRuntimeAuditReport) -> str:
    lines = [
        "# Claude Clean Runtime Audit",
        "",
        f"- ok: `{str(report.ok).lower()}`",
        f"- runtime_id: `{report.runtime_id}`",
        f"- contract_source: `{report.contract_source}`",
        f"- default_path_ok: `{str(report.default_path.ok).lower()}`",
        f"- sidecar_used: `{str(report.default_path.sidecar_used).lower()}`",
        f"- source_repo_required: `{str(report.default_path.source_repo_required).lower()}`",
        f"- blocking_count: `{len(report.blocking_checks)}`",
        f"- warning_count: `{len(report.warnings)}`",
        "",
        "## Checks",
        "",
    ]
    for check in report.checks:
        lines.append(f"- `{check.status}` {check.check_id}: {check.summary}")
    if report.disconnect_evidence:
        lines.extend(["", "## Disconnect Evidence", ""])
        for item in report.disconnect_evidence:
            lines.append(f"- `{str(item.ok).lower()}` {item.target}: error=`{item.error}`")
    return "\n".join(lines)


def assert_clean_runtime_report(report: CleanRuntimeAuditReport) -> None:
    if report.ok:
        return
    details = [f"{check.check_id}:{check.summary}" for check in report.blocking_checks]
    for item in report.disconnect_evidence:
        if not item.ok:
            details.append(f"disconnect:{item.target}:{item.error}")
    raise AssertionError("clean runtime audit failed: " + "; ".join(details))
