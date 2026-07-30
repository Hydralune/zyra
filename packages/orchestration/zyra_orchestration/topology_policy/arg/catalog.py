from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass
from enum import StrEnum
from typing import Any

from ..contracts import EnvironmentSnapshot, FrozenDict, canonical_digest, thaw_json


CATALOG_SCHEMA_VERSION = "zyra.arg-role-catalog/v1"
_TOKEN = re.compile(r"[^A-Za-z0-9_.:/@+-]+")


class ARGCatalogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _primitive(value: Any) -> Any:
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, FrozenDict):
        return thaw_json(value)
    if is_dataclass(value):
        return {item.name: _primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_primitive(item) for item in value]
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if is_dataclass(value):
        return {item.name: getattr(value, item.name) for item in fields(value)}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        selected = to_dict()
        return selected if isinstance(selected, Mapping) else {}
    to_wire = getattr(value, "to_wire", None)
    if callable(to_wire):
        selected = to_wire()
        return selected if isinstance(selected, Mapping) else {}
    return {}


def _values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        values: Iterable[Any] = (value,)
    elif isinstance(value, Mapping):
        values = value.keys()
    else:
        values = value
    return tuple(
        sorted(
            {
                str(item).strip()
                for item in values
                if str(item).strip()
            }
        )
    )


def _identifier(value: Any, *, fallback: str = "") -> str:
    text = str(value or fallback).strip().lower().replace(" ", "_")
    text = _TOKEN.sub("_", text).strip("_.")
    if not text:
        raise ARGCatalogError("catalog_identifier_missing", "catalog identifier is missing")
    return text[:192]


def _bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "disabled", "unavailable"}
    return bool(value)


def _digest_declared_manifest(value: Any, selected: Mapping[str, Any]) -> str:
    digest = str(getattr(value, "digest", "") or selected.get("digest") or "")
    if digest:
        return digest
    return canonical_digest(_primitive(selected))


@dataclass(frozen=True, slots=True)
class ARGCapabilityBinding:
    binding_id: str
    source_kind: str
    source_id: str
    manifest_digest: str
    worker_id: str
    capabilities: tuple[str, ...]
    tool_ids: tuple[str, ...] = ()
    skill_ids: tuple[str, ...] = ()
    model_ids: tuple[str, ...] = ()
    required_permissions: tuple[str, ...] = ("graph.write",)
    allowed_placements: tuple[str, ...] = ()
    healthy: bool = True
    enabled: bool = True
    capacity_available: float = 0.0
    semantic_terms: tuple[str, ...] = ()
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        for name in ("binding_id", "source_kind", "source_id", "manifest_digest", "worker_id"):
            if not str(getattr(self, name) or "").strip():
                raise ARGCatalogError(
                    "catalog_binding_invalid",
                    f"capability binding {name} is required",
                )
        if len(self.manifest_digest) != 64:
            raise ARGCatalogError(
                "catalog_manifest_digest_invalid",
                "capability binding requires a SHA-256 manifest digest",
            )
        capabilities = _values(self.capabilities)
        if not capabilities:
            raise ARGCatalogError(
                "catalog_capability_missing",
                "a role binding without a real capability is rejected",
            )
        permissions = _values(self.required_permissions)
        if not permissions:
            raise ARGCatalogError(
                "catalog_permission_missing",
                "a role binding without an explicit permission is rejected",
            )
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "tool_ids", _values(self.tool_ids))
        object.__setattr__(self, "skill_ids", _values(self.skill_ids))
        object.__setattr__(self, "model_ids", _values(self.model_ids))
        object.__setattr__(self, "required_permissions", permissions)
        object.__setattr__(self, "allowed_placements", _values(self.allowed_placements))
        object.__setattr__(self, "semantic_terms", _values(self.semantic_terms))
        object.__setattr__(self, "capacity_available", max(0.0, float(self.capacity_available)))
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "manifest_digest": self.manifest_digest,
            "worker_id": self.worker_id,
            "capabilities": list(self.capabilities),
            "tool_ids": list(self.tool_ids),
            "skill_ids": list(self.skill_ids),
            "model_ids": list(self.model_ids),
            "required_permissions": list(self.required_permissions),
            "allowed_placements": list(self.allowed_placements),
            "healthy": self.healthy,
            "enabled": self.enabled,
            "capacity_available": self.capacity_available,
            "semantic_terms": list(self.semantic_terms),
            "metadata": thaw_json(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ARGRoleProfile:
    role_id: str
    display_name: str
    capabilities: tuple[str, ...]
    bindings: tuple[ARGCapabilityBinding, ...]
    semantic_terms: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        role_id = _identifier(self.role_id)
        if not self.bindings:
            raise ARGCatalogError(
                "catalog_role_unbound",
                f"role {role_id} has no capability manifest binding",
            )
        capabilities = _values(
            (*self.capabilities, *(item for binding in self.bindings for item in binding.capabilities))
        )
        if not capabilities:
            raise ARGCatalogError(
                "catalog_role_capability_missing",
                f"role {role_id} has no capabilities",
            )
        object.__setattr__(self, "role_id", role_id)
        object.__setattr__(self, "display_name", str(self.display_name or role_id))
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(
            self,
            "bindings",
            tuple(sorted(self.bindings, key=lambda item: item.binding_id)),
        )
        object.__setattr__(
            self,
            "semantic_terms",
            _values(
                (
                    *self.semantic_terms,
                    role_id,
                    self.display_name,
                    *capabilities,
                    *(item for binding in self.bindings for item in binding.semantic_terms),
                )
            ),
        )

    @property
    def manifest_digests(self) -> tuple[str, ...]:
        return tuple(sorted({item.manifest_digest for item in self.bindings}))

    def eligible_bindings(
        self,
        *,
        allowed_permissions: Iterable[str],
        allowed_placements: Iterable[str],
    ) -> tuple[ARGCapabilityBinding, ...]:
        permissions = set(allowed_permissions)
        placements = set(allowed_placements)
        return tuple(
            item
            for item in self.bindings
            if item.enabled
            and item.healthy
            and set(item.required_permissions).issubset(permissions)
            and (
                not item.allowed_placements
                or bool(set(item.allowed_placements).intersection(placements))
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "role_id": self.role_id,
            "display_name": self.display_name,
            "capabilities": list(self.capabilities),
            "bindings": [item.to_dict() for item in self.bindings],
            "semantic_terms": list(self.semantic_terms),
            "manifest_digests": list(self.manifest_digests),
        }


@dataclass(frozen=True, slots=True)
class ARGRoleCatalog:
    profiles: tuple[ARGRoleProfile, ...]
    source_versions: FrozenDict
    rejected_entries: tuple[FrozenDict, ...] = ()
    schema_version: str = CATALOG_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CATALOG_SCHEMA_VERSION:
            raise ARGCatalogError(
                "catalog_schema_unsupported",
                f"unsupported ARG role catalog schema: {self.schema_version}",
            )
        profiles = tuple(sorted(self.profiles, key=lambda item: item.role_id))
        role_ids = [item.role_id for item in profiles]
        if len(role_ids) != len(set(role_ids)):
            raise ARGCatalogError("catalog_role_duplicate", "duplicate ARG role id")
        if not profiles:
            raise ARGCatalogError(
                "catalog_empty",
                "ARG requires at least one capability-bound role",
            )
        object.__setattr__(self, "profiles", profiles)
        object.__setattr__(self, "source_versions", FrozenDict(self.source_versions))
        object.__setattr__(
            self,
            "rejected_entries",
            tuple(sorted((FrozenDict(item) for item in self.rejected_entries), key=canonical_digest)),
        )

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "profiles": [item.to_dict() for item in self.profiles],
                "source_versions": thaw_json(self.source_versions),
                "rejected_entries": [thaw_json(item) for item in self.rejected_entries],
            }
        )

    @property
    def catalog_version(self) -> str:
        return f"arg-role-catalog-v1:{self.digest[:20]}"

    def profile(self, role_id: str) -> ARGRoleProfile:
        selected = _identifier(role_id)
        for profile in self.profiles:
            if profile.role_id == selected:
                return profile
        raise ARGCatalogError(
            "catalog_unknown_role",
            f"unknown or capability-free role rejected: {role_id}",
        )

    def eligible_profiles(
        self,
        *,
        allowed_permissions: Iterable[str],
        allowed_placements: Iterable[str],
    ) -> tuple[ARGRoleProfile, ...]:
        return tuple(
            profile
            for profile in self.profiles
            if profile.eligible_bindings(
                allowed_permissions=allowed_permissions,
                allowed_placements=allowed_placements,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "digest": self.digest,
            "profiles": [item.to_dict() for item in self.profiles],
            "source_versions": thaw_json(self.source_versions),
            "rejected_entries": [thaw_json(item) for item in self.rejected_entries],
        }


class ARGRoleCatalogBuilder:
    """Builds role profiles from canonical worker/tool/skill/model projections."""

    def build(
        self,
        *,
        worker_manifests: Iterable[Any],
        environment: EnvironmentSnapshot,
        skill_registry: Any | None = None,
        tool_registry: Iterable[Any] = (),
        model_registry: Iterable[Any] = (),
        source_versions: Mapping[str, Any] | None = None,
    ) -> ARGRoleCatalog:
        worker_manifests = tuple(worker_manifests)
        tool_registry = tuple(tool_registry)
        model_registry = tuple(model_registry)
        tools = self._tool_index(tool_registry)
        models = self._model_index(model_registry)
        skills = self._skill_index(skill_registry)
        observations = environment.observation_by_resource
        grouped: dict[str, list[ARGCapabilityBinding]] = defaultdict(list)
        role_terms: dict[str, set[str]] = defaultdict(set)
        display_names: dict[str, str] = {}
        rejected: list[FrozenDict] = []

        for raw_manifest in worker_manifests:
            manifest = _mapping(raw_manifest)
            worker_id = str(
                manifest.get("worker_id")
                or manifest.get("workerId")
                or manifest.get("id")
                or ""
            )
            worker_kind = str(
                manifest.get("worker_kind")
                or manifest.get("runtime_worker")
                or manifest.get("workerKind")
                or ""
            )
            constraints = _mapping(manifest.get("constraints"))
            labels = _mapping(manifest.get("labels"))
            metadata = _mapping(manifest.get("metadata"))
            capabilities = _values(
                manifest.get("capabilities")
                or manifest.get("capability_ids")
            )
            if not worker_id or not worker_kind or not capabilities:
                rejected.append(
                    FrozenDict(
                        {
                            "source_kind": "worker",
                            "source_id": worker_id or "unknown",
                            "reason": "missing_worker_identity_or_capability",
                        }
                    )
                )
                continue
            role_id = _identifier(
                labels.get("arg_role")
                or labels.get("role")
                or metadata.get("arg_role")
                or f"role.{worker_kind}"
            )
            display_names[role_id] = str(
                labels.get("arg_role_display_name")
                or manifest.get("display_name")
                or worker_kind
            )
            tool_ids = _values(
                manifest.get("tool_ids")
                or manifest.get("tools")
            )
            model_ids = _values(
                manifest.get("model_ids")
                or manifest.get("models")
                or constraints.get("model_ids")
            )
            matching_skills = tuple(
                item
                for item in skills.values()
                if item["preferred_runtime"] in {
                    worker_id,
                    worker_kind,
                    str(manifest.get("runtime_worker") or ""),
                }
            )
            skill_ids = _values(item["skill_id"] for item in matching_skills)
            skill_tools = _values(
                item
                for skill in matching_skills
                for item in skill["tool_ids"]
            )
            tool_ids = _values((*tool_ids, *skill_tools))
            semantic_terms = {
                *capabilities,
                worker_kind,
                role_id,
                *(item for skill in matching_skills for item in skill["semantic_terms"]),
            }
            for tool_id in tool_ids:
                selected_tool = tools.get(tool_id)
                if selected_tool:
                    semantic_terms.update(selected_tool["semantic_terms"])
                    capabilities = _values(
                        (*capabilities, *selected_tool["capabilities"])
                    )
            for model_id in model_ids:
                selected_model = models.get(model_id)
                if selected_model:
                    semantic_terms.update(selected_model["semantic_terms"])
                    capabilities = _values(
                        (*capabilities, *selected_model["capabilities"])
                    )

            required_permissions = _values(
                constraints.get("required_permissions")
                or metadata.get("required_permissions")
                or ("graph.write",)
            )
            location_value = getattr(manifest.get("location"), "value", manifest.get("location"))
            placements = _values(
                constraints.get("allowed_placements")
                or manifest.get("allowed_placements")
                or (location_value or "local",)
            )
            observation = observations.get(worker_id)
            enabled = _bool(manifest.get("enabled"), default=True)
            healthy = bool(
                observation is not None
                and observation.available
                and observation.healthy
                and not observation.missing_fields
            )
            capacity = (
                observation.capacity_available
                if observation is not None
                else 0.0
            )
            digest = _digest_declared_manifest(raw_manifest, manifest)
            if len(digest) != 64:
                rejected.append(
                    FrozenDict(
                        {
                            "source_kind": "worker",
                            "source_id": worker_id,
                            "reason": "invalid_manifest_digest",
                        }
                    )
                )
                continue
            binding = ARGCapabilityBinding(
                binding_id=f"worker:{worker_id}:{digest[:16]}",
                source_kind="worker",
                source_id=worker_id,
                manifest_digest=digest,
                worker_id=worker_id,
                capabilities=capabilities,
                tool_ids=tool_ids,
                skill_ids=skill_ids,
                model_ids=model_ids,
                required_permissions=required_permissions,
                allowed_placements=placements,
                healthy=healthy,
                enabled=enabled,
                capacity_available=capacity,
                semantic_terms=tuple(semantic_terms),
                metadata=FrozenDict(
                    {
                        "worker_kind": worker_kind,
                        "manifest_revision": manifest.get("manifest_revision")
                        or manifest.get("revision")
                        or 1,
                        "location": str(location_value or ""),
                        "observation_id": (
                            observation.observation_id if observation is not None else ""
                        ),
                        "fresh_until": (
                            observation.fresh_until if observation is not None else ""
                        ),
                        "catalog_sources": ["worker", "tool", "skill", "model"],
                    }
                ),
            )
            grouped[role_id].append(binding)
            role_terms[role_id].update(semantic_terms)

        profiles = tuple(
            ARGRoleProfile(
                role_id=role_id,
                display_name=display_names[role_id],
                capabilities=_values(
                    item
                    for binding in bindings
                    for item in binding.capabilities
                ),
                bindings=tuple(bindings),
                semantic_terms=tuple(role_terms[role_id]),
            )
            for role_id, bindings in sorted(grouped.items())
        )
        versions = {
            "worker_registry": canonical_digest(
                sorted(
                    (
                        {
                            "worker_id": str(
                                _mapping(item).get("worker_id")
                                or _mapping(item).get("workerId")
                                or _mapping(item).get("id")
                                or ""
                            ),
                            "manifest_digest": _digest_declared_manifest(
                                item, _mapping(item)
                            ),
                        }
                        for item in worker_manifests
                    ),
                    key=lambda item: (
                        item["worker_id"],
                        item["manifest_digest"],
                    ),
                )
            ),
            "tool_registry": canonical_digest(
                [_primitive(_mapping(item)) for item in tool_registry]
            ),
            "skill_registry": canonical_digest(
                [_primitive(item) for item in skills.values()]
            ),
            "model_registry": canonical_digest(
                [_primitive(_mapping(item)) for item in model_registry]
            ),
            **dict(source_versions or {}),
        }
        return ARGRoleCatalog(
            profiles=profiles,
            source_versions=FrozenDict(versions),
            rejected_entries=tuple(rejected),
        )

    @staticmethod
    def _tool_index(values: Iterable[Any]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for value in values:
            selected = _mapping(value)
            tool_id = str(
                selected.get("tool_id")
                or selected.get("canonical_name")
                or selected.get("name")
                or ""
            )
            if not tool_id or not _bool(selected.get("enabled"), default=True):
                continue
            output[tool_id] = {
                "capabilities": _values(selected.get("capabilities")),
                "semantic_terms": _values(
                    (
                        tool_id,
                        selected.get("description") or "",
                        *(_values(selected.get("capabilities"))),
                    )
                ),
            }
        return output

    @staticmethod
    def _model_index(values: Iterable[Any]) -> dict[str, dict[str, Any]]:
        output: dict[str, dict[str, Any]] = {}
        for value in values:
            selected = _mapping(value)
            provider_id = str(selected.get("provider_id") or selected.get("providerId") or "")
            model_id = str(selected.get("model_id") or selected.get("modelId") or "")
            if not model_id or not _bool(selected.get("enabled"), default=True):
                continue
            status = str(selected.get("status") or "active").lower()
            if status not in {"active", "available", "healthy", "ready"}:
                continue
            canonical_id = f"{provider_id}/{model_id}" if provider_id else model_id
            capabilities_value = _mapping(selected.get("capabilities"))
            capabilities = list(_values(capabilities_value.get("input")))
            capabilities.extend(_values(capabilities_value.get("output")))
            for flag in ("tools", "streaming", "reasoning", "structured_output", "structuredOutput"):
                if capabilities_value.get(flag) is True:
                    capabilities.append(f"model_{flag.lower()}")
            output[model_id] = output[canonical_id] = {
                "capabilities": _values(capabilities),
                "semantic_terms": _values(
                    (
                        canonical_id,
                        selected.get("display_name")
                        or selected.get("displayName")
                        or "",
                        selected.get("family") or "",
                        *(_values(selected.get("tags"))),
                    )
                ),
            }
        return output

    @staticmethod
    def _skill_index(value: Any | None) -> dict[str, dict[str, Any]]:
        if value is None:
            return {}
        snapshot = _mapping(value)
        revisions = (
            getattr(value, "revisions_by_ref", None)
            or snapshot.get("revisions_by_ref")
            or snapshot.get("revisions")
            or {}
        )
        active = (
            getattr(value, "active_by_qualified_name", None)
            or snapshot.get("active_by_qualified_name")
            or {}
        )
        output: dict[str, dict[str, Any]] = {}
        for qualified_name, reference in sorted(dict(active).items()):
            revision = _mapping(dict(revisions).get(reference))
            metadata = _mapping(revision.get("metadata"))
            if not metadata:
                metadata = _mapping(getattr(dict(revisions).get(reference), "metadata", None))
            allowed_tools = metadata.get("allowed_tools")
            tool_ids = []
            for tool in allowed_tools or ():
                selected_tool = _mapping(tool)
                canonical_name = getattr(tool, "canonical_name", "")
                tool_id = str(
                    canonical_name
                    or selected_tool.get("canonical_name")
                    or selected_tool.get("name")
                    or ""
                )
                if tool_id:
                    tool_ids.append(tool_id)
            invocation = _mapping(metadata.get("invocation"))
            preferred_runtime = str(
                metadata.get("preferred_runtime")
                or invocation.get("agent")
                or "CodeWorkerRuntime"
            )
            output[str(qualified_name)] = {
                "skill_id": str(qualified_name),
                "preferred_runtime": preferred_runtime,
                "tool_ids": _values(tool_ids),
                "semantic_terms": _values(
                    (
                        qualified_name,
                        metadata.get("name") or "",
                        metadata.get("description") or "",
                        metadata.get("when_to_use") or "",
                    )
                ),
            }
        return output
