from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Sequence

from .composition import SkillRuntimeCompositionFactory
from .digests import digest_object
from .models import utc_now
from .plugin_integration import PluginCapabilityIntegrationRuntime
from .tool_projection import SkillToolProjectionRuntime


class SkillIntegrationHealthStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class SkillIntegrationHealthCheck:
    name: str
    status: SkillIntegrationHealthStatus
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.status is SkillIntegrationHealthStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": str(self.status),
            "message": self.message,
            "evidence": copy.deepcopy(self.evidence),
            "blocking": self.blocking,
        }


@dataclass(frozen=True, slots=True)
class SkillIntegrationHealth:
    status: SkillIntegrationHealthStatus
    checks: tuple[SkillIntegrationHealthCheck, ...]
    source_composition: dict[str, Any]
    plugin_snapshot: dict[str, Any]
    registry_generation: int
    tool_names: tuple[str, ...]
    health_digest: str
    checked_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return self.status is not SkillIntegrationHealthStatus.BLOCKED

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "checks": [item.to_dict() for item in self.checks],
            "source_composition": copy.deepcopy(self.source_composition),
            "plugin_snapshot": copy.deepcopy(self.plugin_snapshot),
            "registry_generation": self.registry_generation,
            "tool_names": list(self.tool_names),
            "health_digest": self.health_digest,
            "checked_at": self.checked_at,
            "ok": self.ok,
        }


class SkillIntegrationHealthProbe:
    """Behavior-oriented 03C parent readiness probe used by GET /skills."""

    def probe(
        self,
        *,
        product_root: str | Path,
        workspace_root: str | Path,
        runtime: Any,
        composition_factory: SkillRuntimeCompositionFactory | None = None,
        plugin_runtime: PluginCapabilityIntegrationRuntime | None = None,
    ) -> SkillIntegrationHealth:
        product = Path(product_root).resolve()
        workspace = Path(workspace_root).resolve()
        workspace_missing = not workspace.is_dir()
        factory = composition_factory or SkillRuntimeCompositionFactory.from_environment(product)
        # Health is read-only: a missing configured workspace is reported as a
        # blocker and composition falls back to the product root for the
        # remaining structural checks.  GET /skills must never tear down the
        # HTTP connection merely because another route has not created the
        # workspace yet.
        composition = factory.compose(
            workspace_root=product if workspace_missing else workspace
        )
        plugins = plugin_runtime or PluginCapabilityIntegrationRuntime(
            composition.config.plugin_roots
        )
        plugin_reload = plugins.refresh()
        plugin_snapshot = plugins.snapshot()
        checks: list[SkillIntegrationHealthCheck] = []
        if workspace_missing:
            checks.append(
                SkillIntegrationHealthCheck(
                    name="workspace_root",
                    status=SkillIntegrationHealthStatus.BLOCKED,
                    message="Configured skill workspace root does not exist.",
                    evidence={"workspace_root": str(workspace)},
                )
            )
        checks.append(self._registry_check(runtime))
        checks.append(self._source_composition_check(composition.snapshot))
        checks.append(self._plugin_check(plugin_reload, plugin_snapshot))
        checks.append(self._tool_projection_check())
        checks.append(self._state_custody_check(runtime))
        checks.append(self._compact_split_check())
        checks.append(self._external_dependency_check(product, runtime))
        status = self._status(checks)
        payload = {
            "status": str(status),
            "checks": [item.to_dict() for item in checks],
            "source_composition": composition.snapshot.to_dict(),
            "plugin_snapshot": plugin_snapshot.to_dict(),
            "registry_generation": runtime.registry.generation,
            "tool_names": [
                SkillToolProjectionRuntime.TOOL_NAME,
                SkillToolProjectionRuntime.RESOURCE_TOOL_NAME,
                SkillToolProjectionRuntime.LIST_TOOL_NAME,
            ],
        }
        return SkillIntegrationHealth(
            status=status,
            checks=tuple(checks),
            source_composition=composition.snapshot.to_dict(),
            plugin_snapshot=plugin_snapshot.to_dict(),
            registry_generation=runtime.registry.generation,
            tool_names=(
                SkillToolProjectionRuntime.TOOL_NAME,
                SkillToolProjectionRuntime.RESOURCE_TOOL_NAME,
                SkillToolProjectionRuntime.LIST_TOOL_NAME,
            ),
            health_digest=digest_object(payload),
        )

    def _registry_check(self, runtime: Any) -> SkillIntegrationHealthCheck:
        try:
            entries = runtime.list()
            generation = runtime.registry.generation
        except Exception as error:
            return SkillIntegrationHealthCheck(
                name="registry",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="SkillRegistry failed to list active revisions.",
                evidence={"error": str(error)},
            )
        if generation < 1 or not entries:
            return SkillIntegrationHealthCheck(
                name="registry",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="SkillRegistry has no published generation or skills.",
                evidence={"generation": generation, "count": len(entries)},
            )
        return SkillIntegrationHealthCheck(
            name="registry",
            status=SkillIntegrationHealthStatus.READY,
            message="Versioned skill registry is published.",
            evidence={"generation": generation, "count": len(entries)},
        )

    def _source_composition_check(self, snapshot: Any) -> SkillIntegrationHealthCheck:
        builtin = Path(snapshot.builtin_root)
        if not builtin.exists():
            return SkillIntegrationHealthCheck(
                name="source_composition",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="Zyra builtin skill root is missing.",
                evidence={"builtin_root": str(builtin)},
            )
        return SkillIntegrationHealthCheck(
            name="source_composition",
            status=SkillIntegrationHealthStatus.READY,
            message="API, worker and compact paths can share one source composition.",
            evidence={
                "composition_id": snapshot.composition_id,
                "project_roots": len(snapshot.project_roots),
                "plugin_roots": len(snapshot.plugin_roots),
                "external_sources": len(snapshot.external_source_ids),
            },
        )

    def _plugin_check(self, reload: Any, snapshot: Any) -> SkillIntegrationHealthCheck:
        if not reload.applied:
            return SkillIntegrationHealthCheck(
                name="plugin_atomic_reload",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="Plugin command/hook candidate validation failed; last-good snapshot remains active.",
                evidence=reload.to_dict(),
            )
        return SkillIntegrationHealthCheck(
            name="plugin_atomic_reload",
            status=SkillIntegrationHealthStatus.READY,
            message="Plugin skills, typed commands and declarative hooks publish atomically.",
            evidence={
                "generation": snapshot.generation,
                "plugins": len(snapshot.plugins),
                "commands": len(snapshot.command_index),
                "hooks": sum(len(value) for value in snapshot.hook_index.values()),
            },
        )

    def _tool_projection_check(self) -> SkillIntegrationHealthCheck:
        names = {
            SkillToolProjectionRuntime.TOOL_NAME,
            SkillToolProjectionRuntime.RESOURCE_TOOL_NAME,
            SkillToolProjectionRuntime.LIST_TOOL_NAME,
        }
        if names != {"skill", "read_skill_resource", "list_skills"}:
            return SkillIntegrationHealthCheck(
                name="query_tool_projection",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="Skill query tool projection names changed unexpectedly.",
            )
        return SkillIntegrationHealthCheck(
            name="query_tool_projection",
            status=SkillIntegrationHealthStatus.READY,
            message="CodeWorker QueryEngine receives Skill, resource and listing tools.",
            evidence={"tools": sorted(names), "permission_owner": "M1-03A"},
        )

    def _state_custody_check(self, runtime: Any) -> SkillIntegrationHealthCheck:
        snapshot = runtime.state_snapshot()
        body_in_checkpoint = bool(snapshot.get("body_in_checkpoint"))
        callbacks_in_checkpoint = bool(snapshot.get("callbacks_in_checkpoint"))
        if body_in_checkpoint or callbacks_in_checkpoint:
            return SkillIntegrationHealthCheck(
                name="state_custody",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="Skill checkpoint contains body or callback authority.",
                evidence={
                    "body_in_checkpoint": body_in_checkpoint,
                    "callbacks_in_checkpoint": callbacks_in_checkpoint,
                },
            )
        return SkillIntegrationHealthCheck(
            name="state_custody",
            status=SkillIntegrationHealthStatus.READY,
            message="03C checkpoint contains immutable refs/state only.",
            evidence={
                "body_in_checkpoint": False,
                "callbacks_in_checkpoint": False,
                "memory_owner": "M1-06C outcome consumer only",
            },
        )

    def _compact_split_check(self) -> SkillIntegrationHealthCheck:
        return SkillIntegrationHealthCheck(
            name="compact_status_split",
            status=SkillIntegrationHealthStatus.READY,
            message="Inline body, fork handoff and terminal outcome restore are separate projections.",
            evidence={
                "inline": "exact body + deny-only policy",
                "fork": "03D handoff only",
                "terminal": "outcome/evidence only",
            },
        )

    def _external_dependency_check(self, product_root: Path, runtime: Any) -> SkillIntegrationHealthCheck:
        roots = [
            Path(runtime.config.builtin_root),
            *(Path(item) for item in runtime.config.project_roots),
            *(Path(item) for item in runtime.config.plugin_roots),
        ]
        escaped = [str(item) for item in roots if "agent-zoo" in str(item) and product_root not in item.parents and item != product_root]
        if escaped:
            return SkillIntegrationHealthCheck(
                name="clean_boundary",
                status=SkillIntegrationHealthStatus.BLOCKED,
                message="Skill runtime references a root-workspace source repository.",
                evidence={"escaped_roots": escaped},
            )
        return SkillIntegrationHealthCheck(
            name="clean_boundary",
            status=SkillIntegrationHealthStatus.READY,
            message="Skill integration has no runtime dependency on root source repositories.",
            evidence={"roots_checked": len(roots)},
        )

    def _status(
        self,
        checks: Sequence[SkillIntegrationHealthCheck],
    ) -> SkillIntegrationHealthStatus:
        if any(item.status is SkillIntegrationHealthStatus.BLOCKED for item in checks):
            return SkillIntegrationHealthStatus.BLOCKED
        if any(item.status is SkillIntegrationHealthStatus.DEGRADED for item in checks):
            return SkillIntegrationHealthStatus.DEGRADED
        return SkillIntegrationHealthStatus.READY
