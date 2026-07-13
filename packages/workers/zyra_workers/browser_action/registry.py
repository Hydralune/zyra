from __future__ import annotations

import copy
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from zyra_runtime.tools import ToolRegistry, ToolSpec

from .catalog import default_action_definitions
from .models import ActionDefinition, PlanValidationIssue, digest_value
from .schema import ActionArgumentValidator, BrowserActionSchemaProjector, ValidatedArguments


class BrowserActionRegistryError(RuntimeError):
    """Raised when an action catalog violates Zyra's immutable registry rules."""


@dataclass(frozen=True, slots=True)
class RegistrySnapshot:
    version: int
    digest: str
    actions: tuple[str, ...]
    aliases: Mapping[str, str]
    schema_digests: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "aliases", MappingProxyType(dict(self.aliases)))
        object.__setattr__(self, "schema_digests", MappingProxyType(dict(self.schema_digests)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "digest": self.digest,
            "actions": list(self.actions),
            "aliases": dict(self.aliases),
            "schema_digests": dict(self.schema_digests),
        }


@dataclass(frozen=True, slots=True)
class ResolvedAction:
    requested_name: str
    canonical_name: str
    definition: ActionDefinition
    arguments: ValidatedArguments
    registry_digest: str

    @property
    def alias_used(self) -> bool:
        return self.requested_name != self.canonical_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_name": self.requested_name,
            "canonical_name": self.canonical_name,
            "alias_used": self.alias_used,
            "definition": self.definition.public_dict(),
            "arguments": self.arguments.to_dict(),
            "registry_digest": self.registry_digest,
        }


class BrowserActionRegistry:
    """Zyra-owned immutable browser action registry.

    The upstream browser-use registry informed these definitions, but runtime
    registration never scans an upstream checkout, vendor tree, editable
    package or plugin directory.  The private definitions and aliases are
    copied once, validated as a closed catalog and exposed only through
    detached immutable snapshots.
    """

    def __init__(
        self,
        definitions: Iterable[ActionDefinition],
        *,
        validator: ActionArgumentValidator | None = None,
        projector: BrowserActionSchemaProjector | None = None,
        version: int = 1,
    ) -> None:
        if version < 1:
            raise ValueError("browser action registry version must be positive")
        self._lock = threading.RLock()
        self._validator = validator or ActionArgumentValidator()
        self._projector = projector or BrowserActionSchemaProjector()
        self._version = version
        actions: dict[str, ActionDefinition] = {}
        aliases: dict[str, str] = {}
        for definition in definitions:
            if not isinstance(definition, ActionDefinition):
                raise TypeError("browser action registry accepts ActionDefinition values only")
            if definition.name in actions or definition.name in aliases:
                raise BrowserActionRegistryError(f"duplicate browser action name: {definition.name}")
            actions[definition.name] = copy.deepcopy(definition)
            for alias in definition.aliases:
                if alias == definition.name or alias in actions or alias in aliases:
                    raise BrowserActionRegistryError(f"duplicate browser action alias: {alias}")
                aliases[alias] = definition.name
        if not actions:
            raise BrowserActionRegistryError("browser action registry cannot be empty")
        self._actions = MappingProxyType(actions)
        self._aliases = MappingProxyType(aliases)
        self._snapshot = self._build_snapshot()

    @classmethod
    def default(cls) -> "BrowserActionRegistry":
        return cls(default_action_definitions())

    @property
    def digest(self) -> str:
        return self._snapshot.digest

    @property
    def version(self) -> int:
        return self._version

    def snapshot(self) -> RegistrySnapshot:
        return RegistrySnapshot(**self._snapshot.to_dict())

    def names(self, *, include_aliases: bool = False) -> tuple[str, ...]:
        names = tuple(self._actions)
        if include_aliases:
            return (*names, *tuple(self._aliases))
        return names

    def canonical_name(self, name: str) -> str | None:
        candidate = str(name).strip()
        if candidate in self._actions:
            return candidate
        return self._aliases.get(candidate)

    def get(self, name: str) -> ActionDefinition | None:
        canonical = self.canonical_name(name)
        if canonical is None:
            return None
        return copy.deepcopy(self._actions[canonical])

    def require(self, name: str) -> ActionDefinition:
        definition = self.get(name)
        if definition is None:
            raise BrowserActionRegistryError(f"unknown browser action: {name}")
        return definition

    def resolve(self, name: str, arguments: Mapping[str, Any] | None) -> ResolvedAction:
        requested = str(name).strip()
        canonical = self.canonical_name(requested)
        if canonical is None:
            raise BrowserActionRegistryError(f"unknown browser action: {requested}")
        definition = self._actions[canonical]
        validated = self._validator.validate(definition, arguments)
        return ResolvedAction(
            requested_name=requested,
            canonical_name=canonical,
            definition=copy.deepcopy(definition),
            arguments=validated,
            registry_digest=self.digest,
        )

    def validate_plan(self, actions: Sequence[Any]) -> tuple[PlanValidationIssue, ...]:
        issues: list[PlanValidationIssue] = []
        if isinstance(actions, (str, bytes, bytearray)) or not isinstance(actions, Sequence):
            return (
                PlanValidationIssue(
                    step_index=0,
                    action="",
                    reason="actions_must_be_a_sequence",
                    path="$.actions",
                ),
            )
        for step_index, item in enumerate(actions, start=1):
            if isinstance(item, Mapping):
                name = str(item.get("action") or item.get("name") or "").strip()
                arguments = item.get("arguments", item.get("args", {}))
            else:
                name = str(getattr(item, "action", "") or getattr(item, "name", "")).strip()
                arguments = getattr(item, "arguments", getattr(item, "args", {}))
            canonical = self.canonical_name(name)
            if canonical is None:
                issues.append(
                    PlanValidationIssue(
                        step_index=step_index,
                        action=name,
                        reason="unknown_action",
                        path=f"$.actions[{step_index - 1}].action",
                    )
                )
                continue
            validated = self._validator.validate(self._actions[canonical], arguments)
            for issue in validated.issues:
                if issue.blocking:
                    reason = issue.code
                    if issue.code == "missing_required_argument":
                        reason = "missing_required_argument:" + issue.path.rsplit(".", 1)[-1]
                    elif issue.code == "missing_element_target":
                        reason = "missing_required_target:index|id|selector"
                    issues.append(
                        PlanValidationIssue(
                            step_index=step_index,
                            action=canonical,
                            reason=reason,
                            path=f"$.actions[{step_index - 1}].arguments{issue.path.removeprefix('$')}",
                            details=issue.to_dict(),
                        )
                    )
        return tuple(issues)

    def public_actions(self) -> tuple[dict[str, Any], ...]:
        return tuple(copy.deepcopy(self._actions[name].public_dict()) for name in self._actions)

    def tool_specs(self) -> tuple[ToolSpec, ...]:
        return tuple(self._projector.project(self._actions[name]) for name in self._actions)

    def tool_registry(self) -> ToolRegistry:
        return ToolRegistry(list(self.tool_specs()))

    def source_summary(self) -> dict[str, Any]:
        repositories: dict[str, set[str]] = {}
        paths: dict[str, set[str]] = {}
        for definition in self._actions.values():
            for source in definition.sources:
                repositories.setdefault(source.repository, set()).add(definition.name)
                paths.setdefault(source.repository, set()).add(source.path)
        return {
            "runtime_authority": "zyra_workers.browser_action.registry.BrowserActionRegistry",
            "filesystem_source_scan": False,
            "external_runtime_required": False,
            "registry_digest": self.digest,
            "repositories": {name: sorted(actions) for name, actions in sorted(repositories.items())},
            "source_paths": {name: sorted(values) for name, values in sorted(paths.items())},
        }

    def assert_self_contained(self) -> None:
        for definition in self._actions.values():
            if not definition.sources:
                raise BrowserActionRegistryError(f"action {definition.name} has no source provenance")
            for source in definition.sources:
                path = source.path.replace("\\", "/")
                if path.startswith("../") or path.startswith("/") or ":/" in path:
                    raise BrowserActionRegistryError(
                        f"action {definition.name} contains runtime-like source path {source.path!r}"
                    )

    def _build_snapshot(self) -> RegistrySnapshot:
        schema_digests = {name: definition.identity for name, definition in self._actions.items()}
        payload = {
            "version": self._version,
            "actions": list(self._actions),
            "aliases": dict(self._aliases),
            "schema_digests": schema_digests,
        }
        return RegistrySnapshot(
            version=self._version,
            digest=digest_value(payload),
            actions=tuple(self._actions),
            aliases=dict(self._aliases),
            schema_digests=schema_digests,
        )


_DEFAULT_LOCK = threading.Lock()
_DEFAULT_REGISTRY: BrowserActionRegistry | None = None


def default_browser_action_registry() -> BrowserActionRegistry:
    global _DEFAULT_REGISTRY
    with _DEFAULT_LOCK:
        if _DEFAULT_REGISTRY is None:
            registry = BrowserActionRegistry.default()
            registry.assert_self_contained()
            _DEFAULT_REGISTRY = registry
        return _DEFAULT_REGISTRY
