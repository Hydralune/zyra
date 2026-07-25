from __future__ import annotations

import ast
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import digest, utc_now
from .errors import invalid


ROLE_VALUES = {
    "primary_implementation",
    "supplementary_implementation",
    "conformance_only",
    "reference_only",
    "experimental",
    "deferred",
    "rejected",
    "excluded_forward_only",
}

_PARENT_PATH_PREFIX = ".."
_FORBIDDEN_PARENT_REPOSITORIES = (
    "claude-code-best",
    "browser-use",
    "OpenHands",
    "opencode",
    "oh-my-pi",
    "openclaw",
)


@dataclass(frozen=True, slots=True)
class SourceRoleDisposition:
    source: str
    capability: str
    source_role: str
    source_language: str
    target_language: str
    migration_mode: str
    landing_status: str
    owner: str
    runtime_entry: str
    evidence_paths: tuple[str, ...]
    reason: str
    production_code_required: bool
    active: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "capability": self.capability,
            "source_role": self.source_role,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "migration_mode": self.migration_mode,
            "landing_status": self.landing_status,
            "owner": self.owner,
            "runtime_entry": self.runtime_entry,
            "evidence_paths": list(self.evidence_paths),
            "reason": self.reason,
            "production_code_required": self.production_code_required,
            "active": self.active,
        }


def m2_exit_dispositions() -> tuple[SourceRoleDisposition, ...]:
    return (
        SourceRoleDisposition(
            source="zyra",
            capability="experiment_matrix_metric_bundle_owner",
            source_role="primary_implementation",
            source_language="python/typescript/tsx",
            target_language="python/typescript/tsx",
            migration_mode="scenario_and_evidence_integration_only",
            landing_status="productized",
            owner=(
                "ExperimentMatrixRuntime/MetricAggregationRuntime/"
                "EvidenceBundleBuilder"
            ),
            runtime_entry="zyra_evaluation.experiment_runtime",
            evidence_paths=(
                "packages/evaluation/zyra_evaluation/experiment_runtime",
                "apps/web/src/features/experiments",
                "apps/api/zyra_api/experiment_api.py",
            ),
            reason=(
                "M2-S05-03 assigns a new Zyra-owned evaluation and evidence "
                "state domain; no upstream experiment runtime is selected."
            ),
            production_code_required=True,
            active=True,
        ),
        SourceRoleDisposition(
            source="opencode",
            capability=(
                "protocol_app_tui_terminal_review_diff_session_ui_categories"
            ),
            source_role="reference_only",
            source_language="typescript/tsx",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing M2 active UI owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "apps/web/src",
                "packages/integrations/zyra_integrations/data/internalization_ledger_seed.json",
            ),
            reason=(
                "M2-05 adds no parallel OpenCode runtime; earlier M2 units "
                "already own the selected protocol and workbench categories."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="claude-code-best",
            capability=(
                "query_permission_session_context_compact_mcp_skill_subagent_console"
            ),
            source_role="reference_only",
            source_language="typescript/tsx",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing M1 runtime and M2 control panel owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "packages/runtime",
                "apps/web/src/features/session",
                "apps/web/src/features/permissions",
                "apps/web/src/features/mcp",
                "apps/web/src/features/skills",
            ),
            reason=(
                "The mature runtime/control categories remain with earlier "
                "active owners; the exit runner only verifies their evidence."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="OpenHands",
            capability="conversation_event_artifact_diff_browser_terminal_patterns",
            source_role="reference_only",
            source_language="python/typescript/tsx",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing M2 workbench owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "apps/web/src/features/artifacts",
                "apps/web/src/features/diff-review",
                "apps/web/src/features/browser",
                "apps/web/src/features/terminal",
            ),
            reason=(
                "No second UI state owner is created; prior M2 components "
                "cover these categories."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="browser-use",
            capability="browser_history_close_restore_watchdog",
            source_role="reference_only",
            source_language="python/typescript",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing browser/watchdog owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "packages/browser_worker",
                "apps/web/src/features/browser",
                "packages/scheduler",
            ),
            reason=(
                "Browser-close independence and watchdog behavior are "
                "consumed as frozen evidence, not reimplemented."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="oh-my-pi",
            capability="agentloop_tasktool_mnemopi_provider_rpc_tui_native",
            source_role="reference_only",
            source_language="typescript/native",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing Zyra M1/M2 owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "packages/runtime",
                "packages/memory",
                "apps/web/src",
            ),
            reason=(
                "M2-05 uses OMP only in the forward role-aware audit; it does "
                "not start RPC, TUI, provider or native runtimes."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="Hermes",
            capability="memory_mcp_skill_terminal_trace_reference",
            source_role="reference_only",
            source_language="python/typescript",
            target_language="none",
            migration_mode="reference_only",
            landing_status="not_applicable",
            owner="existing M1/M2 memory and console owners",
            runtime_entry="M2 workbench source-role audit",
            evidence_paths=(
                "packages/memory",
                "apps/web/src/features/memory",
                "apps/web/src/features/trace",
            ),
            reason=(
                "No Hermes runtime is selected for the experiment/evidence domain."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="agent-framework",
            capability="workflow_checkpoint_agui_approval_history",
            source_role="conformance_only",
            source_language="python/csharp",
            target_language="none",
            migration_mode="conformance_only",
            landing_status="not_applicable",
            owner="existing Zyra checkpoint and UI state owners",
            runtime_entry="conformance receipt index",
            evidence_paths=("tests", "docs/reviews/evidence"),
            reason=(
                "Forward-only conformance role; no workflow or approval owner "
                "is migrated."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="agentscope",
            capability="worker_lifecycle_inbox_wakeup",
            source_role="conformance_only",
            source_language="python",
            target_language="none",
            migration_mode="conformance_only",
            landing_status="not_applicable",
            owner="existing Zyra worker-pool owners",
            runtime_entry="conformance receipt index",
            evidence_paths=("packages/scheduler", "tests"),
            reason=(
                "Forward conformance only; no AgentScope runtime or store is selected."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="langgraph",
            capability="checkpoint_identity_pending_committed_writes_exact_resume",
            source_role="conformance_only",
            source_language="python",
            target_language="none",
            migration_mode="conformance_only",
            landing_status="not_applicable",
            owner="GraphCommitRuntime/CheckpointRecoveryRuntime",
            runtime_entry="exact-resume conformance receipt index",
            evidence_paths=(
                "packages/orchestration",
                "packages/scheduler",
                "tests",
            ),
            reason=(
                "Only narrow exact-resume conformance remains; StateGraph, "
                "Pregel, channels, ToolNode, stream and server are not owners."
            ),
            production_code_required=False,
            active=False,
        ),
        SourceRoleDisposition(
            source="openclaw",
            capability="all_forward_roles",
            source_role="excluded_forward_only",
            source_language="none",
            target_language="none",
            migration_mode="excluded_forward_only",
            landing_status="not_applicable",
            owner="none",
            runtime_entry="none",
            evidence_paths=(),
            reason=(
                "OpenClaw remains excluded_forward_only from M1-S06A-01 onward."
            ),
            production_code_required=False,
            active=False,
        ),
    )


class SourceRoleExitAuditor:
    FORBIDDEN_RUNTIME_PATTERNS = tuple(
        f"{_PARENT_PATH_PREFIX}{separator}{repository}"
        for separator in ("/", "\\")
        for repository in _FORBIDDEN_PARENT_REPOSITORIES
    )
    FORBIDDEN_LANGGRAPH_IMPORTS = (
        "langgraph.graph",
        "langgraph.pregel",
        "langgraph.channels",
        "langgraph.prebuilt",
        "langgraph.store",
    )

    def __init__(
        self,
        *,
        project_root: str | Path,
        dispositions: Iterable[SourceRoleDisposition] | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.dispositions = tuple(dispositions or m2_exit_dispositions())

    def audit(self) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        role_counts: dict[str, int] = {}
        keys: set[tuple[str, str]] = set()
        for item in self.dispositions:
            role_counts[item.source_role] = role_counts.get(item.source_role, 0) + 1
            key = (item.source.casefold(), item.capability)
            if key in keys:
                findings.append(
                    {
                        "code": "source_role_duplicate",
                        "source": item.source,
                        "capability": item.capability,
                    }
                )
            keys.add(key)
            if item.source_role not in ROLE_VALUES:
                findings.append(
                    {
                        "code": "source_role_invalid",
                        "source": item.source,
                        "role": item.source_role,
                    }
                )
            if item.production_code_required and not item.active:
                findings.append(
                    {
                        "code": "production_role_inactive",
                        "source": item.source,
                        "capability": item.capability,
                    }
                )
            if item.source_role in {
                "conformance_only",
                "reference_only",
                "excluded_forward_only",
            } and item.production_code_required:
                findings.append(
                    {
                        "code": "inactive_role_has_code_quota",
                        "source": item.source,
                    }
                )
        openclaw = [
            item for item in self.dispositions if item.source.casefold() == "openclaw"
        ]
        if len(openclaw) != 1 or openclaw[0].source_role != "excluded_forward_only":
            findings.append({"code": "openclaw_boundary_invalid"})
        runtime_findings = self._scan_runtime_dependencies()
        findings.extend(runtime_findings)
        receipt = {
            "schema": "zyra.m2-exit-source-role-audit/v1",
            "valid": not findings,
            "openclaw": "excluded_forward_only",
            "rows": [item.to_dict() for item in self.dispositions],
            "role_counts": dict(sorted(role_counts.items())),
            "runtime_dependency_scan": {
                "valid": not runtime_findings,
                "findings": runtime_findings,
            },
            "findings": findings,
            "audited_at": utc_now(),
        }
        receipt["audit_digest"] = digest(receipt)
        return receipt

    def require_valid(self) -> dict[str, Any]:
        receipt = self.audit()
        if not receipt["valid"]:
            raise invalid(
                "experiment_source_role_audit_failed",
                "M2 exit source-role audit failed.",
                phase="source_audit",
                detail=receipt,
            )
        return receipt

    def _scan_runtime_dependencies(self) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        roots = [
            self.project_root / "packages",
            self.project_root / "apps",
            self.project_root / "scripts",
        ]
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*"):
                if not path.is_file() or path.suffix.casefold() not in {
                    ".py",
                    ".ts",
                    ".tsx",
                    ".js",
                    ".mjs",
                    ".cjs",
                    ".json",
                    ".toml",
                    ".yaml",
                    ".yml",
                }:
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if path.suffix.casefold() == ".py":
                    findings.extend(self._python_import_findings(path, text))
                elif path.suffix.casefold() in {".ts", ".tsx", ".js", ".mjs", ".cjs"}:
                    findings.extend(self._javascript_import_findings(path, text))
                elif path.name in {
                    "package.json",
                    "pyproject.toml",
                    "requirements.txt",
                }:
                    findings.extend(self._manifest_dependency_findings(path, text))
        return findings

    def _python_import_findings(
        self,
        path: Path,
        text: str,
    ) -> list[dict[str, Any]]:
        try:
            tree = ast.parse(text, filename=str(path))
        except SyntaxError:
            return []
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        return [
            {
                "code": "forbidden_langgraph_runtime_import",
                "path": str(path.relative_to(self.project_root)),
                "module": module,
            }
            for module in modules
            if any(
                module == forbidden or module.startswith(f"{forbidden}.")
                for forbidden in self.FORBIDDEN_LANGGRAPH_IMPORTS
            )
        ]

    def _javascript_import_findings(
        self,
        path: Path,
        text: str,
    ) -> list[dict[str, Any]]:
        specifiers = re.findall(
            r"""(?:from\s+|import\s*\(|require\s*\()\s*["']([^"']+)["']""",
            text,
        )
        findings: list[dict[str, Any]] = []
        for specifier in specifiers:
            normalized = specifier.replace("\\", "/")
            if any(
                normalized == pattern.replace("\\", "/")
                or normalized.startswith(pattern.replace("\\", "/") + "/")
                for pattern in self.FORBIDDEN_RUNTIME_PATTERNS
            ):
                findings.append(
                    {
                        "code": "parent_source_runtime_import",
                        "path": str(path.relative_to(self.project_root)),
                        "specifier": specifier,
                    }
                )
        return findings

    def _manifest_dependency_findings(
        self,
        path: Path,
        text: str,
    ) -> list[dict[str, Any]]:
        normalized = text.replace("\\", "/")
        findings: list[dict[str, Any]] = []
        for pattern in self.FORBIDDEN_RUNTIME_PATTERNS:
            selected = pattern.replace("\\", "/")
            for prefix in ("file:", "path:", "workspace:"):
                if f"{prefix}{selected}" in normalized:
                    findings.append(
                        {
                            "code": "parent_source_manifest_dependency",
                            "path": str(path.relative_to(self.project_root)),
                            "pattern": f"{prefix}{selected}",
                        }
                    )
        return findings
