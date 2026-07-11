from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .attachments import SkillAttachmentRuntime, SkillListingProjection
from .body_loader import SkillBodyResourceLoader
from .change_detector import SkillChangeDetector, SkillChangeReloadResult
from .compact_bridge import SkillCompactBridge
from .compact_bridge import compact_reference_from_dict
from .hooks import SkillHookRuntime
from .invocation import SkillInvocationRuntime
from .invocation_permission import (
    PreauthorizedBuiltinSkillPermission,
    SkillInvocationPermissionPort,
)
from .models import (
    InvokedSkillState,
    SkillInvocationPlan,
    SkillInvocationRequest,
    SkillInvocationStatus,
    SkillListingEntry,
    SkillSourceKind,
)
from .plugin_runtime import PluginRuntime
from .policy import SkillAllowedToolsPolicy
from .registry import SkillRegistry, SkillRegistryReloadResult
from .reload import SkillReloadCoordinator
from .resource_loader import SkillResourceLoader
from .revision_store import SkillRevisionStore
from .session_bridge import SkillSessionBridge
from .sources import FilesystemSkillSource
from .state import SkillInvocationStateStore
from .subagent_contract import DurableForkRequestQueue, SkillForkPort


@dataclass(frozen=True, slots=True)
class SkillRuntimeConfig:
    project_root: str
    workspace_root: str
    builtin_root: str
    managed_roots: tuple[str, ...] = ()
    user_roots: tuple[str, ...] = ()
    project_roots: tuple[str, ...] = ()
    add_dirs: tuple[str, ...] = ()
    plugin_roots: tuple[str, ...] = ()
    revision_state_path: str = ""
    include_user_skills: bool = False
    strict_sources: bool = True
    disabled: bool = False

    @classmethod
    def for_project(
        cls,
        project_root: str | Path,
        *,
        workspace_root: str | Path | None = None,
        include_user_skills: bool = False,
        add_dirs: Sequence[str | Path] = (),
        plugin_roots: Sequence[str | Path] = (),
        disabled: bool = False,
    ) -> "SkillRuntimeConfig":
        project = Path(project_root).resolve()
        workspace = Path(workspace_root or project).resolve()
        builtin = project / "skills" / "builtin"
        project_skills = project / ".zyra" / "skills"
        user_skills = Path.home() / ".zyra" / "skills"
        return cls(
            project_root=str(project),
            workspace_root=str(workspace),
            builtin_root=str(builtin),
            user_roots=(str(user_skills),) if include_user_skills and user_skills.exists() else (),
            project_roots=(str(project_skills),) if project_skills.exists() else (),
            add_dirs=tuple(str(Path(value).resolve()) for value in add_dirs),
            plugin_roots=tuple(str(Path(value).resolve()) for value in plugin_roots),
            revision_state_path=str(os.environ.get("ZYRA_SKILL_REVISION_STATE_PATH") or ""),
            include_user_skills=include_user_skills,
            disabled=disabled,
        )


class SkillRuntime:
    """03C facade used by API, workers, compact, and future 03D/06C ports."""

    def __init__(
        self,
        config: SkillRuntimeConfig,
        *,
        permission_port: SkillInvocationPermissionPort | None = None,
        fork_port: SkillForkPort | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        self.config = config
        if state_snapshot and isinstance(state_snapshot.get("state_snapshot"), Mapping):
            state_snapshot = state_snapshot["state_snapshot"]
        self.revision_store = SkillRevisionStore(
            state_path=Path(config.revision_state_path) if config.revision_state_path else None
        )
        if state_snapshot and isinstance(state_snapshot.get("revision_lifecycle"), dict):
            self.revision_store.restore_snapshot(state_snapshot["revision_lifecycle"])
        self.plugin_runtime = PluginRuntime(config.plugin_roots)
        if config.plugin_roots:
            self.plugin_runtime.refresh()
        self.registry = SkillRegistry(
            sources=self._build_sources(),
            revision_store=self.revision_store,
            disabled=config.disabled,
        )
        self.body_loader = SkillBodyResourceLoader(self.revision_store, disabled=config.disabled)
        self.resource_loader = SkillResourceLoader(self.revision_store)
        self.allowed_tools_policy = SkillAllowedToolsPolicy(disabled=config.disabled)
        self.state_store = SkillInvocationStateStore()
        if state_snapshot:
            self.state_store.restore(state_snapshot)
        self.hook_runtime = SkillHookRuntime()
        self.attachment_runtime = SkillAttachmentRuntime()
        self.fork_queue = fork_port or DurableForkRequestQueue()
        self.invocation_runtime = SkillInvocationRuntime(
            registry=self.registry,
            body_loader=self.body_loader,
            resource_loader=self.resource_loader,
            allowed_tools_policy=self.allowed_tools_policy,
            state_store=self.state_store,
            hook_runtime=self.hook_runtime,
            attachment_runtime=self.attachment_runtime,
            permission_port=permission_port or PreauthorizedBuiltinSkillPermission(),
            fork_port=self.fork_queue,
            disabled=config.disabled,
        )
        if state_snapshot and isinstance(state_snapshot.get("budget_state"), Mapping):
            self.invocation_runtime.budget_runtime.restore(state_snapshot["budget_state"])
        self.compact_bridge = SkillCompactBridge(
            registry=self.registry,
            body_loader=self.body_loader,
            allowed_tools_policy=self.allowed_tools_policy,
        )
        self.session_bridge = SkillSessionBridge(
            state_store=self.state_store,
            compact_bridge=self.compact_bridge,
        )
        self.reload_coordinator = SkillReloadCoordinator(
            registry=self.registry,
            invocation_runtime=self.invocation_runtime,
            hook_runtime=self.hook_runtime,
            allowed_tools_policy=self.allowed_tools_policy,
        )
        self.change_detector = SkillChangeDetector(
            (
                config.builtin_root,
                *config.managed_roots,
                *config.user_roots,
                *config.project_roots,
                *config.add_dirs,
                *config.plugin_roots,
            )
        )
        self._bootstrapped = False
        self._active_state_restored = False
        self._lock = RLock()

    def bootstrap(self) -> SkillRegistryReloadResult:
        with self._lock:
            if self._bootstrapped:
                snapshot = self.registry.snapshot()
                return SkillRegistryReloadResult(
                    applied=True,
                    previous_generation=snapshot.generation,
                    generation=snapshot.generation,
                    snapshot_id=snapshot.snapshot_id,
                    scans=(),
                    changed_refs=(),
                    removed_refs=(),
                    errors=(),
                )
            result = self.registry.reload(expected_generation=0)
            if not result.applied:
                from .errors import SkillReloadRejected

                raise SkillReloadRejected("initial skill registry load was rejected", detail=result.to_dict())
            self._restore_active_runtime_state()
            self._bootstrapped = True
            self.change_detector.capture()
            return result

    def _restore_active_runtime_state(self) -> None:
        if self._active_state_restored:
            return
        for state in self.state_store.all_states():
            if state.status.terminal or state.policy_snapshot is None:
                continue
            try:
                revision = self.registry.resolve(
                    state.version_ref.qualified_name,
                    requested_ref=state.version_ref,
                )
                policy = self.allowed_tools_policy.restore_snapshot(
                    state.policy_snapshot,
                    current_revision=revision,
                    parent_snapshot_ids=state.policy_snapshot.parent_snapshot_ids,
                )
                leases = self.hook_runtime.register(
                    invocation_id=state.invocation_id,
                    session_id=state.session_id,
                    version_ref=revision.version_ref,
                    specs=revision.metadata.hooks,
                )
                self.state_store.replace(
                    replace(
                        state,
                        version_ref=revision.version_ref,
                        policy_snapshot=policy,
                        hook_lease_ids=tuple(lease.lease_id for lease in leases),
                    ),
                    expected_revision=state.revision,
                )
            except Exception as error:  # noqa: BLE001 - restored authority fails closed.
                try:
                    self.state_store.transition(
                        state.invocation_id,
                        SkillInvocationStatus.REVOKED,
                        error_code=str(getattr(error, "code", "skill_restore_rejected")),
                        error_message=str(error),
                    )
                finally:
                    self.allowed_tools_policy.unbind(state.invocation_id)
                    self.hook_runtime.cleanup_invocation(
                        state.invocation_id,
                        reason="skill restore failed closed",
                    )
        self._active_state_restored = True

    def reload_if_changed(self, *, now: float | None = None) -> SkillChangeReloadResult:
        """Validate and atomically swap a stable filesystem change set."""

        self.bootstrap()
        changes = self.change_detector.poll(now=now)
        if not changes.reload_required:
            return SkillChangeReloadResult(changes=changes, reload=None)
        reload_result = self.reload_coordinator.reload()
        if reload_result.registry.applied:
            changes = self.change_detector.acknowledge(changes)
        return SkillChangeReloadResult(changes=changes, reload=reload_result)

    def list(
        self,
        *,
        workspace_paths: Sequence[str] = (),
        for_model: bool = False,
        for_user: bool = False,
    ) -> list[SkillListingEntry]:
        self.bootstrap()
        return list(
            self.registry.list(
                workspace_paths=workspace_paths,
                for_model=for_model,
                for_user=for_user,
            )
        )

    def listing_projection(
        self,
        *,
        session_id: str,
        agent_id: str,
        workspace_paths: Sequence[str] = (),
        context_window_tokens: int = 200_000,
    ) -> SkillListingProjection:
        entries = self.list(workspace_paths=workspace_paths, for_model=True)
        return self.attachment_runtime.listing(
            entries,
            session_id=session_id,
            agent_id=agent_id,
            generation=self.registry.generation,
            context_window_tokens=context_window_tokens,
        )

    def invoke(self, request: SkillInvocationRequest, **kwargs: Any) -> SkillInvocationPlan:
        self.bootstrap()
        return self.invocation_runtime.invoke(request, **kwargs)

    def state_snapshot(self) -> dict[str, Any]:
        return {
            **self.state_store.snapshot(),
            "budget_state": self.invocation_runtime.budget_runtime.snapshot(),
            "revision_lifecycle": self.revision_store.snapshot(),
            "fork_handoff": self.fork_queue.snapshot() if hasattr(self.fork_queue, "snapshot") else {},
            "body_in_checkpoint": False,
            "callbacks_in_checkpoint": False,
        }

    def outcome_projection(self, invocation_id: str) -> dict[str, Any]:
        state = self.state_store.get(invocation_id)
        return self.compact_bridge.memory_projection(state).to_dict()

    def restore_compact_references(self, references: Sequence[Mapping[str, Any]]) -> tuple[Any, ...]:
        self.bootstrap()
        restored: list[Any] = []
        for raw in references:
            reference = compact_reference_from_dict(dict(raw))
            restored.extend(
                self.compact_bridge.restore(
                    (reference,),
                    session_id=reference.session_id,
                    agent_id=reference.agent_id,
                )
            )
        return tuple(restored)

    def install_permission_hook(self, adapter: Any, *, permission_session_id: str = "") -> str:
        self.bootstrap()
        if permission_session_id:
            for state in self.state_store.all_states():
                if state.status.terminal or state.policy_snapshot is None:
                    continue
                revision = self.registry.resolve(
                    state.version_ref.qualified_name,
                    requested_ref=state.version_ref,
                )
                self.allowed_tools_policy.restore_snapshot(
                    replace(state.policy_snapshot, session_id=permission_session_id),
                    current_revision=revision,
                    parent_snapshot_ids=state.policy_snapshot.parent_snapshot_ids,
                )
        return self.allowed_tools_policy.register_permission_hook(adapter)

    def _build_sources(self) -> tuple[Any, ...]:
        config = self.config
        sources: list[Any] = []
        builtin_root = Path(config.builtin_root)
        if builtin_root.exists():
            sources.append(
                FilesystemSkillSource(
                    root=builtin_root,
                    source_kind=SkillSourceKind.BUILTIN,
                    source_id="builtin:zyra",
                    namespace="builtin",
                    strict=config.strict_sources,
                    origin_uri="zyra://builtin-skills",
                )
            )
        for index, root in enumerate(config.managed_roots):
            sources.append(
                FilesystemSkillSource(
                    root=Path(root),
                    source_kind=SkillSourceKind.MANAGED,
                    source_id=f"managed:{index}",
                    namespace="managed",
                    configured_order=index,
                    strict=config.strict_sources,
                )
            )
        for index, root in enumerate(config.user_roots):
            sources.append(
                FilesystemSkillSource(
                    root=Path(root),
                    source_kind=SkillSourceKind.USER,
                    source_id=f"user:{index}",
                    namespace="user",
                    configured_order=index,
                    strict=config.strict_sources,
                )
            )
        for index, root in enumerate(config.project_roots):
            sources.append(
                FilesystemSkillSource(
                    root=Path(root),
                    source_kind=SkillSourceKind.PROJECT,
                    source_id=f"project:{index}",
                    namespace="project",
                    configured_order=index,
                    project_distance=index,
                    strict=config.strict_sources,
                )
            )
        for index, root in enumerate(config.add_dirs):
            sources.append(
                FilesystemSkillSource(
                    root=Path(root),
                    source_kind=SkillSourceKind.ADD_DIR,
                    source_id=f"add-dir:{index}",
                    namespace=f"add-dir-{index}",
                    configured_order=index,
                    strict=config.strict_sources,
                )
            )
        sources.extend(self.plugin_runtime.skill_sources())
        return tuple(sources)


_DEFAULT_LOCK = RLock()
_DEFAULT_REGISTRY: SkillRegistry | None = None
_DEFAULT_RUNTIME: SkillRuntime | None = None


def project_root_from_package() -> Path:
    return Path(__file__).resolve().parents[3]


def default_skill_runtime(*, refresh: bool = False) -> SkillRuntime:
    global _DEFAULT_RUNTIME, _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if refresh or _DEFAULT_RUNTIME is None:
            project_root = project_root_from_package()
            config = SkillRuntimeConfig.for_project(
                project_root,
                workspace_root=os.environ.get("ZYRA_WORKSPACE_ROOT") or project_root,
                include_user_skills=False,
            )
            _DEFAULT_RUNTIME = SkillRuntime(config)
            _DEFAULT_RUNTIME.bootstrap()
            _DEFAULT_REGISTRY = _DEFAULT_RUNTIME.registry
        return _DEFAULT_RUNTIME


def default_skill_registry(*, refresh: bool = False) -> SkillRegistry:
    return default_skill_runtime(refresh=refresh).registry
