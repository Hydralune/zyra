from __future__ import annotations

import copy
import fnmatch
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .digests import arguments_digest, digest_object
from .errors import PluginManifestError
from .integration_errors import (
    SkillCommandArgumentError,
    SkillPluginConflict,
    SkillPluginDisabled,
    SkillPluginNotFound,
    SkillPluginReloadRejected,
    SkillPluginSecretRejected,
    SkillPluginSupplyChainRejected,
)
from .models import (
    SkillHookEvent,
    SkillHookLifetime,
    SkillHookSpec,
    SkillInvocationRequest,
    SkillInvocationStatus,
    utc_now,
)
from .runtime import SkillRuntime
from .sources.plugin import (
    PluginCommandDeclaration,
    PluginHookDeclaration,
    PluginManifest,
    substitute_plugin_content,
)


class PluginCommandSpecPort(Protocol):
    def __call__(
        self,
        *,
        name: str,
        purpose: str,
        source: str,
        event_hint: str,
        aliases: tuple[str, ...],
        requires_task: bool,
        metadata: dict[str, str],
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class PluginCapabilityIdentity:
    plugin_id: str
    plugin_version: str
    manifest_digest: str
    capability_kind: str
    capability_name: str
    capability_digest: str
    configured_order: int

    @property
    def immutable_ref(self) -> str:
        return (
            f"plugin-capability://{self.plugin_id}/{self.capability_kind}/"
            f"{self.capability_name}@sha256:{self.capability_digest}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "plugin_version": self.plugin_version,
            "manifest_digest": self.manifest_digest,
            "capability_kind": self.capability_kind,
            "capability_name": self.capability_name,
            "capability_digest": self.capability_digest,
            "configured_order": self.configured_order,
            "immutable_ref": self.immutable_ref,
        }


@dataclass(frozen=True, slots=True)
class PluginCommandCapability:
    identity: PluginCapabilityIdentity
    command_name: str
    qualified_command_name: str
    target_skill: str
    description: str
    aliases: tuple[str, ...]
    default_arguments: dict[str, Any]
    user_invocable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "command_name": self.command_name,
            "qualified_command_name": self.qualified_command_name,
            "target_skill": self.target_skill,
            "description": self.description,
            "aliases": list(self.aliases),
            "default_arguments": copy.deepcopy(self.default_arguments),
            "user_invocable": self.user_invocable,
        }


@dataclass(frozen=True, slots=True)
class PluginHookCapability:
    identity: PluginCapabilityIdentity
    declaration: PluginHookDeclaration
    hook_spec: SkillHookSpec

    def matches(self, skill_name: str) -> bool:
        return fnmatch.fnmatchcase(skill_name, self.declaration.matcher)

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": self.identity.to_dict(),
            "declaration": asdict(self.declaration),
            "hook_spec": self.hook_spec.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PluginIntegratedCapability:
    plugin_id: str
    version: str
    root: str
    manifest_digest: str
    commands: tuple[PluginCommandCapability, ...]
    hooks: tuple[PluginHookCapability, ...]
    skill_namespaces: tuple[str, ...]
    cache_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "version": self.version,
            "root": self.root,
            "manifest_digest": self.manifest_digest,
            "commands": [item.to_dict() for item in self.commands],
            "hooks": [item.to_dict() for item in self.hooks],
            "skill_namespaces": list(self.skill_namespaces),
            "cache_only": self.cache_only,
        }


@dataclass(frozen=True, slots=True)
class PluginIntegrationSnapshot:
    generation: int
    plugins: dict[str, PluginIntegratedCapability]
    disabled: dict[str, str]
    command_index: dict[str, PluginCommandCapability]
    hook_index: dict[str, tuple[PluginHookCapability, ...]]
    snapshot_digest: str
    errors: tuple[dict[str, Any], ...] = ()
    created_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "plugins": {key: value.to_dict() for key, value in self.plugins.items()},
            "disabled": dict(self.disabled),
            "command_index": {key: value.to_dict() for key, value in self.command_index.items()},
            "hook_index": {
                key: [item.to_dict() for item in value] for key, value in self.hook_index.items()
            },
            "snapshot_digest": self.snapshot_digest,
            "errors": [copy.deepcopy(item) for item in self.errors],
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class PluginReloadReceipt:
    applied: bool
    previous_generation: int
    generation: int
    previous_digest: str
    snapshot_digest: str
    added_plugins: tuple[str, ...]
    removed_plugins: tuple[str, ...]
    changed_plugins: tuple[str, ...]
    pruned_hook_refs: tuple[str, ...]
    errors: tuple[dict[str, Any], ...]
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "previous_generation": self.previous_generation,
            "generation": self.generation,
            "previous_digest": self.previous_digest,
            "snapshot_digest": self.snapshot_digest,
            "added_plugins": list(self.added_plugins),
            "removed_plugins": list(self.removed_plugins),
            "changed_plugins": list(self.changed_plugins),
            "pruned_hook_refs": list(self.pruned_hook_refs),
            "errors": [copy.deepcopy(item) for item in self.errors],
            "created_at": self.created_at,
        }


class PluginCapabilityIntegrationRuntime:
    """Atomic cache-only plugin command/skill/hook integration.

    No Python/Node module is imported and no executable command is loaded from
    a plugin.  Commands are typed aliases to exact SkillRuntime invocations;
    hooks are converted to the two product-owned declarative actions already
    enforced by SkillHookRuntime.
    """

    def __init__(self, roots: Iterable[str | Path] = ()) -> None:
        self._lock = RLock()
        self._roots = tuple(Path(item).resolve() for item in roots)
        self._disabled: dict[str, str] = {}
        self._snapshot = PluginIntegrationSnapshot(
            generation=0,
            plugins={},
            disabled={},
            command_index={},
            hook_index={},
            snapshot_digest=digest_object({"generation": 0, "plugins": {}}),
        )

    def configure_roots(self, roots: Iterable[str | Path]) -> None:
        selected = tuple(Path(item).resolve() for item in roots)
        if len(set(map(str, selected))) != len(selected):
            raise SkillPluginConflict("duplicate plugin cache root")
        with self._lock:
            self._roots = selected

    def snapshot(self) -> PluginIntegrationSnapshot:
        with self._lock:
            return self._snapshot

    def restore_disabled(self, values: Mapping[str, str]) -> None:
        restored = {
            str(plugin_id): str(reason or "disabled")
            for plugin_id, reason in values.items()
            if str(plugin_id).strip()
        }
        with self._lock:
            self._disabled = restored

    def disable(self, plugin_id: str, *, reason: str) -> PluginReloadReceipt:
        plugin_id = str(plugin_id).strip()
        if not plugin_id:
            raise ValueError("plugin_id is required")
        with self._lock:
            if plugin_id not in self._snapshot.plugins and not any(
                PluginManifest.load(root).plugin_id == plugin_id for root in self._roots if root.exists()
            ):
                raise SkillPluginNotFound("plugin was not found", detail={"plugin_id": plugin_id})
            self._disabled[plugin_id] = reason or "disabled"
        return self.refresh()

    def enable(self, plugin_id: str) -> PluginReloadReceipt:
        with self._lock:
            if self._disabled.pop(str(plugin_id), None) is None:
                raise SkillPluginNotFound(
                    "disabled plugin was not found",
                    detail={"plugin_id": plugin_id},
                )
        return self.refresh()

    def refresh(self) -> PluginReloadReceipt:
        previous = self.snapshot()
        try:
            candidate = self._build_candidate(previous.generation + 1)
        except Exception as error:  # noqa: BLE001 - last-good snapshot is preserved.
            detail = {
                "code": str(getattr(error, "code", "plugin_reload_rejected")),
                "message": str(error),
                "detail": dict(getattr(error, "detail", {}) or {}),
            }
            return PluginReloadReceipt(
                applied=False,
                previous_generation=previous.generation,
                generation=previous.generation,
                previous_digest=previous.snapshot_digest,
                snapshot_digest=previous.snapshot_digest,
                added_plugins=(),
                removed_plugins=(),
                changed_plugins=(),
                pruned_hook_refs=(),
                errors=(detail,),
            )
        with self._lock:
            if self._snapshot.snapshot_digest != previous.snapshot_digest:
                return self.refresh()
            self._snapshot = candidate
        previous_ids = set(previous.plugins)
        current_ids = set(candidate.plugins)
        changed = tuple(
            sorted(
                plugin_id
                for plugin_id in previous_ids & current_ids
                if previous.plugins[plugin_id].manifest_digest
                != candidate.plugins[plugin_id].manifest_digest
            )
        )
        survivors = {
            item.identity.immutable_ref
            for values in candidate.hook_index.values()
            for item in values
        }
        removed_hook_refs = tuple(
            sorted(
                item.identity.immutable_ref
                for values in previous.hook_index.values()
                for item in values
                if item.identity.immutable_ref not in survivors
            )
        )
        return PluginReloadReceipt(
            applied=True,
            previous_generation=previous.generation,
            generation=candidate.generation,
            previous_digest=previous.snapshot_digest,
            snapshot_digest=candidate.snapshot_digest,
            added_plugins=tuple(sorted(current_ids - previous_ids)),
            removed_plugins=tuple(sorted(previous_ids - current_ids)),
            changed_plugins=changed,
            pruned_hook_refs=removed_hook_refs,
            errors=(),
        )

    def command_specs(self) -> tuple[Any, ...]:
        try:
            from zyra_commands import CommandSpec
        except ImportError as error:  # pragma: no cover - packaging gate.
            raise SkillPluginReloadRejected("zyra_commands is unavailable") from error
        values = []
        for name, command in sorted(self.snapshot().command_index.items()):
            values.append(
                CommandSpec(
                    name=f"/{name}",
                    purpose=command.description or f"Invoke plugin skill {command.target_skill}.",
                    source="M1-03C PluginCapabilityIntegrationRuntime",
                    event_hint="skill_invoked",
                    aliases=tuple(f"/{item}" for item in command.aliases),
                    requires_task=True,
                    metadata={
                        "category": "plugin_skill",
                        "runtime_status": "stateful",
                        "plugin_id": command.identity.plugin_id,
                        "plugin_version": command.identity.plugin_version,
                        "plugin_capability_ref": command.identity.immutable_ref,
                        "target_skill": command.target_skill,
                    },
                )
            )
        return tuple(values)

    def resolve_command(self, command_name: str) -> PluginCommandCapability:
        normalized = str(command_name).strip().removeprefix("/")
        snapshot = self.snapshot()
        value = snapshot.command_index.get(normalized)
        if value is None:
            raise SkillPluginNotFound(
                "plugin command was not found",
                detail={"command": command_name},
            )
        if value.identity.plugin_id in snapshot.disabled:
            raise SkillPluginDisabled(
                "plugin command belongs to a disabled plugin",
                detail={"plugin_id": value.identity.plugin_id},
            )
        return value

    def invoke_command(
        self,
        command_name: str,
        *,
        runtime: SkillRuntime,
        run_id: str,
        task_id: str,
        session_id: str,
        agent_id: str,
        raw_argument_text: str = "",
        arguments: Mapping[str, Any] | None = None,
        permission_port: Any | None = None,
        parent_tool_use_id: str = "",
        invocation_id: str = "",
    ) -> Any:
        command = self.resolve_command(command_name)
        merged_arguments = copy.deepcopy(command.default_arguments)
        merged_arguments.update(dict(arguments or {}))
        if raw_argument_text:
            merged_arguments.setdefault("raw", raw_argument_text)
        request = SkillInvocationRequest(
            run_id=run_id,
            task_id=task_id,
            session_id=session_id,
            agent_id=agent_id,
            skill_name=command.target_skill,
            arguments=merged_arguments,
            parent_tool_use_id=parent_tool_use_id or invocation_id or command.identity.immutable_ref,
            idempotency_key=digest_object(
                {
                    "command_ref": command.identity.immutable_ref,
                    "arguments": merged_arguments,
                    "session_id": session_id,
                    "invocation_id": invocation_id,
                }
            ),
            interactive=True,
            headless=False,
            invocation_id=invocation_id or digest_object(
                {
                    "command_ref": command.identity.immutable_ref,
                    "session_id": session_id,
                    "time": utc_now(),
                }
            )[:28],
        )
        if permission_port is not None:
            runtime.invocation_runtime.permission_port = permission_port
        plan = runtime.invoke(
            request,
            require_user_invocable=True,
            require_model_invocable=False,
        )
        return plan

    def plugin_hooks_for_skill(self, skill_name: str) -> tuple[PluginHookCapability, ...]:
        values: list[PluginHookCapability] = []
        snapshot = self.snapshot()
        for hooks in snapshot.hook_index.values():
            values.extend(item for item in hooks if item.matches(skill_name))
        return tuple(values)

    def register_plugin_hooks(
        self,
        *,
        runtime: SkillRuntime,
        invocation_id: str,
        session_id: str,
        version_ref: Any,
        skill_name: str,
    ) -> tuple[Any, ...]:
        hooks = self.plugin_hooks_for_skill(skill_name)
        return runtime.hook_runtime.register(
            invocation_id=invocation_id,
            session_id=session_id,
            version_ref=version_ref,
            specs=tuple(item.hook_spec for item in hooks),
        )

    def substitute_model_content(
        self,
        plugin_id: str,
        content: str,
        *,
        plugin_data_root: str | Path,
        user_config: Mapping[str, str],
    ) -> str:
        plugin = self.snapshot().plugins.get(plugin_id)
        if plugin is None:
            raise SkillPluginNotFound("plugin was not found", detail={"plugin_id": plugin_id})
        manifest = PluginManifest.load(plugin.root)
        try:
            return substitute_plugin_content(
                content,
                plugin_root=plugin.root,
                plugin_data_root=plugin_data_root,
                manifest=manifest,
                user_config=user_config,
                sink="model",
            )
        except Exception as error:
            raise SkillPluginSecretRejected(str(error), detail=getattr(error, "detail", {})) from error

    def _build_candidate(self, generation: int) -> PluginIntegrationSnapshot:
        plugins: dict[str, PluginIntegratedCapability] = {}
        command_index: dict[str, PluginCommandCapability] = {}
        hook_index: dict[str, list[PluginHookCapability]] = {}
        seen_roots: set[str] = set()
        for order, root in enumerate(self._roots):
            if not root.exists() or not root.is_dir():
                raise SkillPluginSupplyChainRejected(
                    "plugin cache root is missing",
                    detail={"root": str(root)},
                )
            resolved = root.resolve()
            root_key = str(resolved).casefold()
            if root_key in seen_roots:
                raise SkillPluginConflict("plugin cache root is duplicated", detail={"root": str(root)})
            seen_roots.add(root_key)
            manifest = PluginManifest.load(resolved)
            if not manifest.enabled or manifest.plugin_id in self._disabled:
                continue
            if manifest.plugin_id in plugins:
                raise SkillPluginConflict(
                    "duplicate plugin id",
                    detail={"plugin_id": manifest.plugin_id},
                )
            manifest_digest = manifest.digest()
            commands = tuple(
                self._command_capability(manifest, declaration, manifest_digest, order)
                for declaration in manifest.commands
            )
            hooks = tuple(
                self._hook_capability(manifest, declaration, manifest_digest, order, index)
                for index, declaration in enumerate(manifest.hooks)
            )
            for command in commands:
                keys = (command.qualified_command_name, *command.aliases)
                for key in keys:
                    if key in command_index:
                        raise SkillPluginConflict(
                            "plugin command or alias collision",
                            detail={"command": key},
                        )
                    command_index[key] = command
            for hook in hooks:
                hook_index.setdefault(hook.declaration.event, []).append(hook)
            plugins[manifest.plugin_id] = PluginIntegratedCapability(
                plugin_id=manifest.plugin_id,
                version=manifest.version,
                root=str(resolved),
                manifest_digest=manifest_digest,
                commands=commands,
                hooks=hooks,
                skill_namespaces=tuple(
                    f"plugin:{manifest.plugin_id}:{item}" for item in manifest.skills_paths
                ),
            )
        payload = {
            "generation": generation,
            "plugins": {key: value.to_dict() for key, value in plugins.items()},
            "disabled": self._disabled,
            "command_index": {key: value.to_dict() for key, value in command_index.items()},
            "hook_index": {
                key: [item.to_dict() for item in values] for key, values in hook_index.items()
            },
        }
        return PluginIntegrationSnapshot(
            generation=generation,
            plugins=plugins,
            disabled=dict(self._disabled),
            command_index=command_index,
            hook_index={key: tuple(values) for key, values in hook_index.items()},
            snapshot_digest=digest_object(payload),
        )

    def _command_capability(
        self,
        manifest: PluginManifest,
        declaration: PluginCommandDeclaration,
        manifest_digest: str,
        order: int,
    ) -> PluginCommandCapability:
        qualified_name = f"{manifest.plugin_id}:{declaration.name}"
        digest = digest_object(
            {
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.version,
                "manifest_digest": manifest_digest,
                "declaration": asdict(declaration),
            }
        )
        identity = PluginCapabilityIdentity(
            plugin_id=manifest.plugin_id,
            plugin_version=manifest.version,
            manifest_digest=manifest_digest,
            capability_kind="command",
            capability_name=qualified_name,
            capability_digest=digest,
            configured_order=order,
        )
        aliases = tuple(f"{manifest.plugin_id}:{item}" for item in declaration.aliases)
        return PluginCommandCapability(
            identity=identity,
            command_name=declaration.name,
            qualified_command_name=qualified_name,
            target_skill=f"plugin:{manifest.plugin_id}:{declaration.skill}",
            description=declaration.description,
            aliases=aliases,
            default_arguments=copy.deepcopy(declaration.default_arguments),
            user_invocable=declaration.user_invocable,
        )

    def _hook_capability(
        self,
        manifest: PluginManifest,
        declaration: PluginHookDeclaration,
        manifest_digest: str,
        order: int,
        index: int,
    ) -> PluginHookCapability:
        name = f"{manifest.plugin_id}:{declaration.event}:{index}"
        digest = digest_object(
            {
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.version,
                "manifest_digest": manifest_digest,
                "declaration": asdict(declaration),
            }
        )
        identity = PluginCapabilityIdentity(
            plugin_id=manifest.plugin_id,
            plugin_version=manifest.version,
            manifest_digest=manifest_digest,
            capability_kind="hook",
            capability_name=name,
            capability_digest=digest,
            configured_order=order,
        )
        event = SkillHookEvent(declaration.event)
        lifetime = SkillHookLifetime.ONCE if declaration.once else SkillHookLifetime.INVOCATION
        spec = SkillHookSpec(
            hook_id=identity.immutable_ref,
            event=event,
            action=declaration.action,
            lifetime=lifetime,
            parameters={
                **copy.deepcopy(declaration.payload),
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.version,
                "plugin_capability_ref": identity.immutable_ref,
                "matcher": declaration.matcher,
            },
        )
        return PluginHookCapability(identity=identity, declaration=declaration, hook_spec=spec)
