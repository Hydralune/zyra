from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .canonical import digest, identity, new_identity, path_within, utc_now
from .errors import conflict, invalid


ROLE_PRODUCTION = {
    "primary_implementation",
    "supplementary_implementation",
    "zyra_owned_primary",
    "existing_owner_integration",
}
ROLE_INACTIVE = {
    "conformance_only",
    "reference_only",
    "role_aware_audit_only",
    "experimental",
    "deferred",
    "rejected",
    "excluded_forward_only",
}


DEFAULT_SOURCE_AUDIT_ROWS: tuple[dict[str, Any], ...] = (
    {
        "source": "zyra",
        "capability": "scenario_runner_evidence_owner",
        "role": "zyra_owned_primary",
        "status": "active",
        "landing": [
            "packages/evaluation/zyra_evaluation/scenario_runner",
            "apps/api/zyra_api/scenario_api.py",
            "apps/web/src/features/scenarios",
            "scripts/run_first_stage_scenarios.py",
        ],
        "reason": "M2-S05-01 creates the canonical scenario/evidence owner.",
    },
    {
        "source": "zyra",
        "capability": "m1_canonical_runtime_owners",
        "role": "existing_owner_integration",
        "status": "active",
        "landing": [
            "apps/api/zyra_api/main.py",
            "packages/orchestration",
            "packages/scheduler",
            "packages/memory",
            "packages/runtime",
        ],
        "reason": "Task, event, scheduler, memory, permission and recovery custody remains unchanged.",
    },
    {
        "source": "opencode",
        "capability": "split_protocol_app_tui_web_desktop",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "apps/web/src/api",
            "apps/web/src/features/scenarios",
            "apps/web/src/features/session",
            "apps/web/src/features/terminal",
            "apps/web/src/features/diff-review",
        ],
        "reason": "Session event, permission/question, terminal/review/diff patterns are audited without another console owner.",
    },
    {
        "source": "claude-code-best",
        "capability": "backend_command_session_permission",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "packages/runtime/claude-runtime",
            "packages/commands",
            "apps/web/src/features/permissions",
            "apps/web/src/features/session",
        ],
        "reason": "Existing internalized runtime remains owner; this slice adds no source runtime.",
    },
    {
        "source": "claude-code-best",
        "capability": "cli_tui_repl_prompt_queue_local_jsx_dialog_ink",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "apps/web/src/features/scenarios",
            "apps/web/src/command",
            "apps/web/src/shell",
        ],
        "reason": "CLI/TUI interaction patterns are audited separately from backend runtime roles.",
    },
    {
        "source": "oh-my-pi",
        "capability": "agentloop_tasktool_pal_mnemopi",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "packages/runtime/claude-runtime",
            "packages/memory",
            "apps/web/src/features/subagents",
        ],
        "reason": "AgentLoop, TaskTool/PAL and Mnemopi categories are audited; their owners are not duplicated.",
    },
    {
        "source": "oh-my-pi",
        "capability": "provider_rpc_acp_hashline_roboomp",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "packages/runtime/provider-control-plane",
            "packages/runtime/claude-runtime/src/protocol",
            "apps/web/src/features/diff-review",
        ],
        "reason": "Provider wires, RPC/ACP, Hashline and RoboOmp are category audits only.",
    },
    {
        "source": "oh-my-pi",
        "capability": "tui_native_snapcompact",
        "role": "role_aware_audit_only",
        "status": "inactive",
        "landing": [
            "apps/web/src",
            "packages/runtime/claude-runtime/src/compact",
        ],
        "reason": "TUI/native/Snapcompact status is retained as audit-only for this slice.",
    },
    {
        "source": "agent-framework",
        "capability": "workflow_checkpoint_agui_approval_history",
        "role": "conformance_only",
        "status": "inactive",
        "landing": ["tests/scenarios", "tests/integration"],
        "reason": "Conformance challenges only; no workflow or store owner.",
    },
    {
        "source": "agentscope",
        "capability": "worker_lifecycle_inbox_wakeup",
        "role": "conformance_only",
        "status": "inactive",
        "landing": ["tests/scenarios", "tests/integration"],
        "reason": "Restart/lifecycle conformance only.",
    },
    {
        "source": "langgraph",
        "capability": "narrow_exact_resume_contract",
        "role": "conformance_only",
        "status": "inactive",
        "landing": ["tests/scenarios", "packages/scheduler/zyra_scheduler/recovery_runtime"],
        "reason": "Only narrow checkpoint identity and exact-resume semantics are eligible.",
    },
    {
        "source": "browser-use",
        "capability": "browser_close_restore_watchdog",
        "role": "reference_only",
        "status": "inactive",
        "landing": ["apps/web/src/features/scenarios", "tests/scenarios"],
        "reason": "Browser-close independence and recovery behavior are reference-only.",
    },
    {
        "source": "openclaw",
        "capability": "all_forward_roles",
        "role": "excluded_forward_only",
        "status": "inactive",
        "landing": [],
        "reason": "Forward exclusion is mandatory; no source path or runtime dependency is allowed.",
    },
)


class SourceRoleAuditor:
    def __init__(
        self,
        *,
        project_root: str | Path,
        rows: Iterable[Mapping[str, Any]] = DEFAULT_SOURCE_AUDIT_ROWS,
    ) -> None:
        self._project_root = Path(project_root).resolve(strict=False)
        self._rows = tuple(self._normalize(row) for row in rows)

    def audit(self) -> dict[str, Any]:
        findings: list[dict[str, Any]] = []
        role_counts: Counter[str] = Counter()
        status_counts: Counter[str] = Counter()
        capability_status: dict[str, str] = {}
        landing_owners: dict[str, list[str]] = defaultdict(list)
        for row in self._rows:
            role = row["role"]
            status = row["status"]
            role_counts[role] += 1
            status_counts[status] += 1
            capability_key = f"{row['source']}:{row['capability']}"
            capability_status[capability_key] = status
            if role in ROLE_PRODUCTION and status != "active":
                findings.append(
                    {
                        "code": "production_role_inactive",
                        "source": row["source"],
                        "capability": row["capability"],
                    }
                )
            if role in ROLE_INACTIVE and status == "active":
                findings.append(
                    {
                        "code": "nonproduction_role_active",
                        "source": row["source"],
                        "capability": row["capability"],
                    }
                )
            if row["source"] == "openclaw":
                if role != "excluded_forward_only" or row["landing"]:
                    findings.append({"code": "openclaw_forward_exclusion_broken"})
            for landing in row["landing"]:
                path = (self._project_root / landing).resolve(strict=False)
                if not path_within(path, self._project_root):
                    findings.append(
                        {
                            "code": "landing_outside_project",
                            "landing": landing,
                            "source": row["source"],
                        }
                    )
                    continue
                landing_owners[landing].append(capability_key)
                if role in ROLE_PRODUCTION and not path.exists():
                    findings.append(
                        {
                            "code": "active_landing_missing",
                            "landing": landing,
                            "source": row["source"],
                        }
                    )
        duplicate_primary: list[dict[str, Any]] = []
        by_capability: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self._rows:
            by_capability[row["capability"]].append(row)
        for capability, rows in by_capability.items():
            primary = [
                row
                for row in rows
                if row["role"] in {"primary_implementation", "zyra_owned_primary"}
                and row["status"] == "active"
            ]
            if len(primary) > 1:
                duplicate_primary.append(
                    {
                        "capability": capability,
                        "sources": [row["source"] for row in primary],
                    }
                )
        findings.extend(
            {"code": "duplicate_primary_owner", **item}
            for item in duplicate_primary
        )
        inactive_not_missing = [
            {
                "source": row["source"],
                "capability": row["capability"],
                "role": row["role"],
                "reason": row["reason"],
                "landing": list(row["landing"]),
            }
            for row in self._rows
            if row["status"] == "inactive"
        ]
        result = {
            "schema": "zyra.m2-source-role-audit/v1",
            "audit_id": new_identity("sourceaudit"),
            "valid": not findings,
            "project_root": str(self._project_root),
            "rows": [dict(row) for row in self._rows],
            "role_counts": dict(sorted(role_counts.items())),
            "status_counts": dict(sorted(status_counts.items())),
            "capability_status": dict(sorted(capability_status.items())),
            "landing_owners": {
                key: sorted(value) for key, value in sorted(landing_owners.items())
            },
            "inactive_not_missing": inactive_not_missing,
            "findings": findings,
            "openclaw": "excluded_forward_only",
            "audited_at": utc_now(),
        }
        result["audit_digest"] = digest(result)
        return result

    def require_valid(self) -> dict[str, Any]:
        report = self.audit()
        if not report["valid"]:
            raise conflict(
                "scenario_source_role_audit_failed",
                "M2 role and landing audit failed.",
                phase="source-audit",
                detail=report,
            )
        return report

    def _normalize(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        source = identity(raw.get("source"), "source audit repository")
        capability = identity(raw.get("capability"), "source audit capability")
        role = identity(raw.get("role"), "source audit role")
        status = str(raw.get("status") or "").strip().casefold()
        if role not in ROLE_PRODUCTION | ROLE_INACTIVE:
            raise invalid(
                "source_audit_role_invalid",
                "Source audit role is unsupported.",
                phase="source-audit",
                detail={"role": role},
            )
        if status not in {"active", "inactive"}:
            raise invalid(
                "source_audit_status_invalid",
                "Source audit status must be active or inactive.",
                phase="source-audit",
            )
        landing = raw.get("landing") or []
        if not isinstance(landing, (list, tuple)):
            raise invalid(
                "source_audit_landing_invalid",
                "Source audit landing must be an array.",
                phase="source-audit",
            )
        return {
            "source": source,
            "capability": capability,
            "role": role,
            "status": status,
            "landing": tuple(str(value).replace("\\", "/").strip("/") for value in landing),
            "reason": str(raw.get("reason") or "").strip(),
        }
