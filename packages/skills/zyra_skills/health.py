from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import utc_now


class SkillHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SkillHealthCheck:
    name: str
    status: SkillHealthStatus
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "message": self.message,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SkillRuntimeHealth:
    status: SkillHealthStatus
    checks: tuple[SkillHealthCheck, ...]
    registry_generation: int
    active_skill_count: int
    active_invocation_count: int
    active_hook_count: int
    active_policy_count: int
    created_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.status is SkillHealthStatus.HEALTHY

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "ok": self.ok,
            "checks": [check.to_dict() for check in self.checks],
            "registry_generation": self.registry_generation,
            "active_skill_count": self.active_skill_count,
            "active_invocation_count": self.active_invocation_count,
            "active_hook_count": self.active_hook_count,
            "active_policy_count": self.active_policy_count,
            "created_at": self.created_at,
        }


class SkillRuntimeHealthProbe:
    def probe(self, runtime: Any, *, session_id: str = "") -> SkillRuntimeHealth:
        checks: list[SkillHealthCheck] = []
        try:
            runtime.bootstrap()
            snapshot = runtime.registry.snapshot()
            checks.append(
                SkillHealthCheck(
                    name="registry",
                    status=SkillHealthStatus.HEALTHY,
                    message="atomic registry snapshot is available",
                    evidence={
                        "generation": snapshot.generation,
                        "snapshot_id": snapshot.snapshot_id,
                        "active_count": len(snapshot.active_by_qualified_name),
                    },
                )
            )
        except Exception as error:  # noqa: BLE001 - health must report a blocked owner.
            snapshot = None
            checks.append(
                SkillHealthCheck(
                    name="registry",
                    status=SkillHealthStatus.BLOCKED,
                    message="skill registry is unavailable",
                    evidence={"error": type(error).__name__, "code": getattr(error, "code", "")},
                )
            )
        builtin_root = Path(runtime.config.builtin_root)
        checks.append(
            SkillHealthCheck(
                name="builtin_assets",
                status=(SkillHealthStatus.HEALTHY if builtin_root.is_dir() else SkillHealthStatus.BLOCKED),
                message=("builtin skill assets are available" if builtin_root.is_dir() else "builtin skill asset root is missing"),
                evidence={"root": str(builtin_root), "inside_project": _inside(builtin_root, Path(runtime.config.project_root))},
            )
        )
        components = {
            "body_loader": runtime.body_loader,
            "allowed_tools_policy": runtime.allowed_tools_policy,
            "invocation_runtime": runtime.invocation_runtime,
        }
        for name, component in components.items():
            disabled = bool(getattr(component, "disabled", False))
            checks.append(
                SkillHealthCheck(
                    name=name,
                    status=SkillHealthStatus.BLOCKED if disabled else SkillHealthStatus.HEALTHY,
                    message=f"{name} is {'disabled' if disabled else 'enabled'}",
                    evidence={"disable_to_fail": True},
                )
            )
        source_status = snapshot.source_status if snapshot is not None else {}
        rejected = {
            source_id: item
            for source_id, item in source_status.items()
            if str(item.get("state") or "") == "reload_rejected"
        }
        checks.append(
            SkillHealthCheck(
                name="source_scans",
                status=SkillHealthStatus.DEGRADED if rejected else SkillHealthStatus.HEALTHY,
                message=("one or more source scans are rejected" if rejected else "configured source scans are active"),
                evidence={"rejected": rejected, "source_count": len(source_status)},
            )
        )
        active_states = runtime.state_store.active_for_session(session_id) if session_id else ()
        active_hooks = runtime.hook_runtime.active_for_session(session_id) if session_id else ()
        active_policies = runtime.allowed_tools_policy.active_snapshots(session_id) if session_id else ()
        if len(active_hooks) > sum(len(state.hook_lease_ids) for state in active_states):
            checks.append(
                SkillHealthCheck(
                    name="hook_leases",
                    status=SkillHealthStatus.BLOCKED,
                    message="active hook leases are not owned by active invocation state",
                    evidence={"active_hooks": len(active_hooks), "active_invocations": len(active_states)},
                )
            )
        else:
            checks.append(
                SkillHealthCheck(
                    name="hook_leases",
                    status=SkillHealthStatus.HEALTHY,
                    message="hook leases are bounded by active invocation state",
                    evidence={"active_hooks": len(active_hooks)},
                )
            )
        severity = max(
            (check.status for check in checks),
            key=lambda value: {
                SkillHealthStatus.HEALTHY: 0,
                SkillHealthStatus.DEGRADED: 1,
                SkillHealthStatus.BLOCKED: 2,
            }[value],
        )
        return SkillRuntimeHealth(
            status=severity,
            checks=tuple(checks),
            registry_generation=snapshot.generation if snapshot is not None else 0,
            active_skill_count=len(snapshot.active_by_qualified_name) if snapshot is not None else 0,
            active_invocation_count=len(active_states),
            active_hook_count=len(active_hooks),
            active_policy_count=len(active_policies),
        )


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False
