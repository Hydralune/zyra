from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .digests import digest_object
from .integration_errors import (
    SkillMcpProjectionInvalid,
    SkillPluginSupplyChainRejected,
    SkillSessionOwnershipError,
)
from .models import SkillSourceKind, utc_now
from .path_security import assert_within, canonical_root
from .runtime import SkillRuntime, SkillRuntimeConfig
from .sources.base import SkillSource
from .sources.plugin import PluginManifest


def _truthy(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True, slots=True)
class SkillSourceRoot:
    source_kind: SkillSourceKind
    source_id: str
    root: str
    namespace: str
    configured_order: int = 0
    project_distance: int = 0
    trusted_configuration: bool = False
    read_only: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("skill source root requires source_id")
        if not self.namespace.strip():
            raise ValueError("skill source root requires namespace")
        if not self.root.strip():
            raise ValueError("skill source root requires root")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": str(self.source_kind),
            "source_id": self.source_id,
            "root": self.root,
            "namespace": self.namespace,
            "configured_order": self.configured_order,
            "project_distance": self.project_distance,
            "trusted_configuration": self.trusted_configuration,
            "read_only": self.read_only,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class SkillSourceCompositionSnapshot:
    composition_id: str
    product_root: str
    workspace_root: str
    builtin_root: str
    managed_roots: tuple[SkillSourceRoot, ...]
    user_roots: tuple[SkillSourceRoot, ...]
    project_roots: tuple[SkillSourceRoot, ...]
    add_dirs: tuple[SkillSourceRoot, ...]
    plugin_roots: tuple[SkillSourceRoot, ...]
    external_source_ids: tuple[str, ...]
    include_user_skills: bool
    disabled: bool
    source_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "composition_id": self.composition_id,
            "product_root": self.product_root,
            "workspace_root": self.workspace_root,
            "builtin_root": self.builtin_root,
            "managed_roots": [item.to_dict() for item in self.managed_roots],
            "user_roots": [item.to_dict() for item in self.user_roots],
            "project_roots": [item.to_dict() for item in self.project_roots],
            "add_dirs": [item.to_dict() for item in self.add_dirs],
            "plugin_roots": [item.to_dict() for item in self.plugin_roots],
            "external_source_ids": list(self.external_source_ids),
            "include_user_skills": self.include_user_skills,
            "disabled": self.disabled,
            "source_digest": self.source_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SkillRuntimeComposition:
    config: SkillRuntimeConfig
    snapshot: SkillSourceCompositionSnapshot

    def create_runtime(
        self,
        *,
        permission_port: Any | None = None,
        fork_port: Any | None = None,
        state_snapshot: Mapping[str, Any] | None = None,
    ) -> SkillRuntime:
        return SkillRuntime(
            self.config,
            permission_port=permission_port,
            fork_port=fork_port,
            state_snapshot=state_snapshot,
        )


class SkillRuntimeCompositionFactory:
    """Single production composition root for API, worker and compact paths.

    Request payloads never become filesystem roots.  Every non-builtin root is
    selected from deployment configuration or derived from the task workspace.
    External sources are already validated typed projections, normally emitted
    by the 03B MCP adapter.
    """

    def __init__(
        self,
        *,
        product_root: str | Path,
        managed_roots: Sequence[str | Path] = (),
        plugin_cache_roots: Sequence[str | Path] = (),
        trusted_add_dirs: Sequence[str | Path] = (),
        include_user_skills: bool = False,
        allow_workspace_plugins: bool = True,
        allow_workspace_project_skills: bool = True,
    ) -> None:
        self.product_root = canonical_root(product_root)
        self.managed_roots = tuple(Path(item).resolve() for item in managed_roots)
        self.plugin_cache_roots = tuple(Path(item).resolve() for item in plugin_cache_roots)
        self.trusted_add_dirs = tuple(Path(item).resolve() for item in trusted_add_dirs)
        self.include_user_skills = include_user_skills
        self.allow_workspace_plugins = allow_workspace_plugins
        self.allow_workspace_project_skills = allow_workspace_project_skills
        self._lock = RLock()
        self._external_sources: dict[str, SkillSource] = {}

    @classmethod
    def from_environment(cls, product_root: str | Path) -> "SkillRuntimeCompositionFactory":
        def roots(name: str) -> tuple[Path, ...]:
            value = os.environ.get(name, "")
            return tuple(
                Path(item).expanduser().resolve()
                for item in value.split(os.pathsep)
                if item.strip()
            )

        return cls(
            product_root=product_root,
            managed_roots=roots("ZYRA_MANAGED_SKILL_ROOTS"),
            plugin_cache_roots=roots("ZYRA_PLUGIN_CACHE_ROOTS"),
            trusted_add_dirs=roots("ZYRA_TRUSTED_SKILL_ADD_DIRS"),
            include_user_skills=_truthy(os.environ.get("ZYRA_INCLUDE_USER_SKILLS")),
            allow_workspace_plugins=_truthy(
                os.environ.get("ZYRA_ALLOW_WORKSPACE_PLUGINS"),
                default=True,
            ),
            allow_workspace_project_skills=_truthy(
                os.environ.get("ZYRA_ALLOW_WORKSPACE_PROJECT_SKILLS"),
                default=True,
            ),
        )

    def register_external_source(self, source: SkillSource) -> None:
        source_id = str(getattr(source, "source_id", "")).strip()
        source_kind = getattr(source, "source_kind", None)
        if not source_id:
            raise SkillMcpProjectionInvalid("external skill source has no stable source_id")
        if source_kind is not SkillSourceKind.MCP:
            raise SkillMcpProjectionInvalid(
                "only typed MCP projections may enter external source composition",
                detail={"source_id": source_id, "source_kind": str(source_kind)},
            )
        with self._lock:
            existing = self._external_sources.get(source_id)
            if existing is not None and existing is not source:
                raise SkillMcpProjectionInvalid(
                    "external skill source identity collision",
                    detail={"source_id": source_id},
                )
            self._external_sources[source_id] = source

    def remove_external_source(self, source_id: str) -> bool:
        with self._lock:
            return self._external_sources.pop(str(source_id), None) is not None

    def external_sources(self) -> tuple[SkillSource, ...]:
        with self._lock:
            return tuple(self._external_sources[key] for key in sorted(self._external_sources))

    def compose(
        self,
        *,
        workspace_root: str | Path,
        disabled: bool = False,
        include_user_skills: bool | None = None,
    ) -> SkillRuntimeComposition:
        workspace = canonical_root(workspace_root)
        builtin_root = self.product_root / "skills" / "builtin"
        managed = self._managed_source_roots()
        user = self._user_source_roots(
            self.include_user_skills if include_user_skills is None else include_user_skills
        )
        project = self._project_source_roots(workspace)
        add_dirs = self._add_dir_source_roots()
        plugins = self._plugin_source_roots(workspace)
        external = self.external_sources()
        payload = {
            "product_root": str(self.product_root),
            "workspace_root": str(workspace),
            "builtin_root": str(builtin_root),
            "managed": [item.to_dict() for item in managed],
            "user": [item.to_dict() for item in user],
            "project": [item.to_dict() for item in project],
            "add_dirs": [item.to_dict() for item in add_dirs],
            "plugins": [item.to_dict() for item in plugins],
            "external_source_ids": [str(getattr(item, "source_id")) for item in external],
            "disabled": disabled,
        }
        source_digest = digest_object(payload)
        composition_id = source_digest[:32]
        snapshot = SkillSourceCompositionSnapshot(
            composition_id=composition_id,
            product_root=str(self.product_root),
            workspace_root=str(workspace),
            builtin_root=str(builtin_root),
            managed_roots=managed,
            user_roots=user,
            project_roots=project,
            add_dirs=add_dirs,
            plugin_roots=plugins,
            external_source_ids=tuple(str(getattr(item, "source_id")) for item in external),
            include_user_skills=bool(user),
            disabled=disabled,
            source_digest=source_digest,
        )
        config = SkillRuntimeConfig(
            project_root=str(self.product_root),
            workspace_root=str(workspace),
            builtin_root=str(builtin_root),
            managed_roots=tuple(item.root for item in managed),
            user_roots=tuple(item.root for item in user),
            project_roots=tuple(item.root for item in project),
            add_dirs=tuple(item.root for item in add_dirs),
            plugin_roots=tuple(item.root for item in plugins),
            revision_state_path=str(os.environ.get("ZYRA_SKILL_REVISION_STATE_PATH") or ""),
            include_user_skills=bool(user),
            strict_sources=True,
            disabled=disabled,
            external_sources=external,
            composition_id=composition_id,
        )
        return SkillRuntimeComposition(config=config, snapshot=snapshot)

    def _managed_source_roots(self) -> tuple[SkillSourceRoot, ...]:
        values: list[SkillSourceRoot] = []
        for index, root in enumerate(self.managed_roots):
            if not root.exists():
                continue
            values.append(
                SkillSourceRoot(
                    source_kind=SkillSourceKind.MANAGED,
                    source_id=f"managed:{index}",
                    root=str(canonical_root(root)),
                    namespace="managed",
                    configured_order=index,
                    trusted_configuration=True,
                    metadata={"configured_by": "deployment"},
                )
            )
        return tuple(values)

    def _user_source_roots(self, enabled: bool) -> tuple[SkillSourceRoot, ...]:
        if not enabled:
            return ()
        root = Path.home() / ".zyra" / "skills"
        if not root.exists():
            return ()
        return (
            SkillSourceRoot(
                source_kind=SkillSourceKind.USER,
                source_id="user:0",
                root=str(canonical_root(root)),
                namespace="user",
                configured_order=0,
                trusted_configuration=False,
                metadata={"configured_by": "deployment_allow_user"},
            ),
        )

    def _project_source_roots(self, workspace: Path) -> tuple[SkillSourceRoot, ...]:
        if not self.allow_workspace_project_skills:
            return ()
        roots: list[SkillSourceRoot] = []
        current = workspace
        distance = 0
        stop = workspace.anchor
        while True:
            candidate = current / ".zyra" / "skills"
            if candidate.exists():
                assert_within(candidate, current, label="project skill root")
                roots.append(
                    SkillSourceRoot(
                        source_kind=SkillSourceKind.PROJECT,
                        source_id=f"project:{distance}",
                        root=str(canonical_root(candidate)),
                        namespace="project",
                        configured_order=distance,
                        project_distance=distance,
                        trusted_configuration=False,
                        metadata={"workspace_derived": True},
                    )
                )
            if str(current) == stop or current.parent == current:
                break
            current = current.parent
            distance += 1
            if distance > 64:
                break
        return tuple(roots)

    def _add_dir_source_roots(self) -> tuple[SkillSourceRoot, ...]:
        values: list[SkillSourceRoot] = []
        for index, root in enumerate(self.trusted_add_dirs):
            if not root.exists():
                continue
            values.append(
                SkillSourceRoot(
                    source_kind=SkillSourceKind.ADD_DIR,
                    source_id=f"add-dir:{index}",
                    root=str(canonical_root(root)),
                    namespace=f"add-dir-{index}",
                    configured_order=index,
                    trusted_configuration=True,
                    metadata={"configured_by": "deployment"},
                )
            )
        return tuple(values)

    def _plugin_source_roots(self, workspace: Path) -> tuple[SkillSourceRoot, ...]:
        disabled_plugins: set[str] = set()
        control_state = workspace / ".zyra" / "skill-control" / "plugin-state.json"
        if control_state.is_file():
            try:
                raw_state = json.loads(control_state.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SkillPluginSupplyChainRejected(
                    "workspace plugin control state is invalid"
                ) from error
            raw_disabled = raw_state.get("disabled") if isinstance(raw_state, Mapping) else None
            if not isinstance(raw_disabled, Mapping):
                raise SkillPluginSupplyChainRejected(
                    "workspace plugin disabled state is invalid"
                )
            disabled_plugins = {str(item) for item in raw_disabled}
        candidates: list[tuple[Path, bool, str]] = [
            (root, True, "deployment_cache") for root in self.plugin_cache_roots
        ]
        workspace_plugins = workspace / ".zyra" / "plugins"
        if self.allow_workspace_plugins and workspace_plugins.exists():
            for root in sorted(workspace_plugins.iterdir()):
                if root.is_dir():
                    candidates.append((root, False, "workspace_cache"))
        values: list[SkillSourceRoot] = []
        seen: set[str] = set()
        for index, (root, trusted, configured_by) in enumerate(candidates):
            if not root.exists():
                continue
            resolved = canonical_root(root)
            key = os.path.normcase(str(resolved))
            if key in seen:
                continue
            seen.add(key)
            manifest = resolved / ".zyra-plugin" / "plugin.json"
            if not manifest.exists() and configured_by == "deployment_cache":
                raise SkillPluginSupplyChainRejected(
                    "deployment plugin cache entry has no manifest",
                    detail={"root": str(resolved)},
                )
            plugin_id = PluginManifest.load(resolved).plugin_id
            if plugin_id in disabled_plugins:
                continue
            values.append(
                SkillSourceRoot(
                    source_kind=SkillSourceKind.PLUGIN,
                    source_id=f"plugin-root:{index}",
                    root=str(resolved),
                    namespace=f"plugin-root-{index}",
                    configured_order=index,
                    trusted_configuration=trusted,
                    metadata={"configured_by": configured_by, "cache_only": True},
                )
            )
        return tuple(values)


_DEFAULT_FACTORY_LOCK = RLock()
_DEFAULT_FACTORIES: dict[str, SkillRuntimeCompositionFactory] = {}


def default_skill_composition_factory(
    product_root: str | Path,
    *,
    refresh: bool = False,
) -> SkillRuntimeCompositionFactory:
    key = os.path.normcase(str(Path(product_root).resolve()))
    with _DEFAULT_FACTORY_LOCK:
        if refresh or key not in _DEFAULT_FACTORIES:
            _DEFAULT_FACTORIES[key] = SkillRuntimeCompositionFactory.from_environment(product_root)
        return _DEFAULT_FACTORIES[key]


def compose_skill_runtime(
    *,
    product_root: str | Path,
    workspace_root: str | Path,
    permission_port: Any | None = None,
    fork_port: Any | None = None,
    state_snapshot: Mapping[str, Any] | None = None,
    disabled: bool = False,
    external_sources: Iterable[SkillSource] = (),
) -> tuple[SkillRuntime, SkillSourceCompositionSnapshot]:
    # Worker/API composition is request scoped.  In particular, MCP sources
    # carry live 03B capability identity and must never leak through the
    # product-root singleton into another session or workspace.
    factory = SkillRuntimeCompositionFactory.from_environment(product_root)
    for source in external_sources:
        factory.register_external_source(source)
    composition = factory.compose(workspace_root=workspace_root, disabled=disabled)
    runtime = composition.create_runtime(
        permission_port=permission_port,
        fork_port=fork_port,
        state_snapshot=state_snapshot,
    )
    return runtime, composition.snapshot
