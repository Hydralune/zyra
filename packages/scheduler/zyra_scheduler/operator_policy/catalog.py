from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_orchestration.topology_policy.contracts import FrozenDict, canonical_digest

from ..models import WorkerHealth, WorkerManifest
from ..pool import WorkerPool


OPERATOR_CATALOG_SCHEMA = "zyra.operator-catalog/v1"


class OperatorCatalogError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OperatorType(StrEnum):
    SKILL = "skill"
    AGENT = "agent"
    WORKER = "worker"
    TOOL = "tool"
    MODEL = "model"


def _token(value: Any) -> str:
    return str(value or "").strip()


def _tokens(values: Any) -> tuple[str, ...]:
    if values is None:
        selected: Iterable[Any] = ()
    elif isinstance(values, str):
        selected = (values,)
    elif isinstance(values, Mapping):
        selected = values.keys()
    else:
        selected = values
    return tuple(
        sorted(
            {
                _token(item).lower().replace(" ", "_")
                for item in selected
                if _token(item)
            }
        )
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _value(source: Any, *names: str, default: Any = None) -> Any:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return default
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return default


def _operator_metadata(source: Any) -> Mapping[str, Any]:
    metadata = _mapping(_value(source, "metadata", default={}))
    nested = metadata.get("operator")
    if isinstance(nested, Mapping):
        return {**metadata, **nested}
    extensions = _mapping(_value(source, "extensions", default={}))
    nested = extensions.get("operator")
    if isinstance(nested, Mapping):
        return {**metadata, **extensions, **nested}
    return {**metadata, **extensions}


def _privacy_classes(value: Any, *, fallback: Sequence[str]) -> tuple[str, ...]:
    rendered = _token(value).lower()
    if not rendered:
        return _tokens(fallback)
    if rendered in {"sensitive_ok", "private_ok", "any"}:
        return ("internal", "masked", "project", "public", "sensitive")
    if rendered in {"public_only", "public"}:
        return ("public",)
    if rendered in {"public_or_masked", "masked"}:
        return ("masked", "public")
    if rendered in {"internal_or_project", "project_cloud"}:
        return ("internal", "project")
    return _tokens(value)


@dataclass(frozen=True, slots=True)
class OperatorProfile:
    operator_id: str
    operator_type: OperatorType
    version: str
    display_name: str
    description: str
    capabilities: tuple[str, ...]
    input_contract: tuple[str, ...]
    output_contract: tuple[str, ...]
    required_permissions: tuple[str, ...]
    allowed_locations: tuple[str, ...]
    allowed_privacy_classes: tuple[str, ...]
    estimated_tokens: int
    estimated_cost_usd: float
    estimated_latency_ms: int
    health_status: str
    available_capacity: int
    verifier_contracts: tuple[str, ...]
    minimum_evidence_contract: tuple[str, ...]
    cold_start: bool
    confidence: float
    outcome_count: int
    source_registry: str
    source_registry_version: str
    source_ref: str
    enabled: bool = True
    revoked: bool = False
    metadata: FrozenDict = field(default_factory=FrozenDict)

    def __post_init__(self) -> None:
        if not _token(self.operator_id):
            raise OperatorCatalogError(
                "operator_id_missing",
                "operator profiles require a stable operator id",
            )
        if not _token(self.version):
            raise OperatorCatalogError(
                "operator_version_missing",
                f"operator {self.operator_id} requires a version",
            )
        for name in (
            "capabilities",
            "input_contract",
            "output_contract",
            "required_permissions",
            "allowed_locations",
            "allowed_privacy_classes",
            "verifier_contracts",
            "minimum_evidence_contract",
        ):
            object.__setattr__(self, name, _tokens(getattr(self, name)))
        object.__setattr__(self, "estimated_tokens", max(0, int(self.estimated_tokens)))
        object.__setattr__(
            self,
            "estimated_cost_usd",
            max(0.0, round(float(self.estimated_cost_usd), 8)),
        )
        object.__setattr__(
            self,
            "estimated_latency_ms",
            max(0, int(self.estimated_latency_ms)),
        )
        object.__setattr__(
            self,
            "available_capacity",
            max(0, int(self.available_capacity)),
        )
        object.__setattr__(self, "outcome_count", max(0, int(self.outcome_count)))
        object.__setattr__(
            self,
            "confidence",
            round(min(1.0, max(0.0, float(self.confidence))), 8),
        )
        object.__setattr__(self, "metadata", FrozenDict(self.metadata))

    @property
    def reference(self) -> str:
        return f"{self.operator_id}@{self.version}"

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "operator_type": self.operator_type.value,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "input_contract": list(self.input_contract),
            "output_contract": list(self.output_contract),
            "required_permissions": list(self.required_permissions),
            "allowed_locations": list(self.allowed_locations),
            "allowed_privacy_classes": list(self.allowed_privacy_classes),
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "estimated_latency_ms": self.estimated_latency_ms,
            "health_status": self.health_status,
            "available_capacity": self.available_capacity,
            "verifier_contracts": list(self.verifier_contracts),
            "minimum_evidence_contract": list(self.minimum_evidence_contract),
            "cold_start": self.cold_start,
            "confidence": self.confidence,
            "outcome_count": self.outcome_count,
            "source_registry": self.source_registry,
            "source_registry_version": self.source_registry_version,
            "source_ref": self.source_ref,
            "enabled": self.enabled,
            "revoked": self.revoked,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class OperatorCatalog:
    entries: tuple[OperatorProfile, ...]
    source_versions: FrozenDict
    generation: int
    built_at: str
    schema_version: str = OPERATOR_CATALOG_SCHEMA

    def __post_init__(self) -> None:
        if self.schema_version != OPERATOR_CATALOG_SCHEMA:
            raise OperatorCatalogError(
                "operator_catalog_schema_invalid",
                f"unsupported catalog schema: {self.schema_version}",
            )
        ordered = tuple(
            sorted(self.entries, key=lambda item: (item.operator_id, item.version))
        )
        identifiers = [item.operator_id for item in ordered]
        if len(identifiers) != len(set(identifiers)):
            raise OperatorCatalogError(
                "operator_catalog_duplicate_id",
                "operator catalog contains duplicate stable operator ids",
            )
        object.__setattr__(self, "entries", ordered)
        object.__setattr__(self, "source_versions", FrozenDict(self.source_versions))
        object.__setattr__(self, "generation", max(0, int(self.generation)))

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "generation": self.generation,
                "source_versions": dict(self.source_versions),
                "entries": [item.to_dict() for item in self.entries],
            }
        )

    @property
    def catalog_version(self) -> str:
        return f"operator-catalog-v1-g{self.generation}-{self.digest[:16]}"

    def get(self, operator_id: str) -> OperatorProfile | None:
        return next(
            (item for item in self.entries if item.operator_id == operator_id),
            None,
        )

    def validate_references(
        self,
        *,
        catalog_version: str,
        catalog_digest: str,
        references: Sequence[tuple[str, str]],
    ) -> None:
        if catalog_version != self.catalog_version or catalog_digest != self.digest:
            raise OperatorCatalogError(
                "operator_catalog_drift",
                "operator proposal belongs to a different catalog version",
            )
        for operator_id, version in references:
            profile = self.get(operator_id)
            if (
                profile is None
                or profile.version != version
                or not profile.enabled
                or profile.revoked
            ):
                raise OperatorCatalogError(
                    "operator_reference_revoked",
                    f"operator proposal reference is no longer executable: {operator_id}@{version}",
                )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "catalog_version": self.catalog_version,
            "catalog_digest": self.digest,
            "generation": self.generation,
            "built_at": self.built_at,
            "source_versions": dict(self.source_versions),
            "entries": [item.to_dict() for item in self.entries],
        }


class OperatorCatalogBuilder:
    """Build a new immutable catalog from current Zyra registry projections."""

    def __init__(
        self,
        *,
        cold_start_confidence: float = 0.55,
        established_confidence: float = 1.0,
        cold_start_outcome_threshold: int = 3,
    ) -> None:
        self.cold_start_confidence = min(
            1.0, max(0.0, float(cold_start_confidence))
        )
        self.established_confidence = min(
            1.0, max(0.0, float(established_confidence))
        )
        self.cold_start_outcome_threshold = max(
            1, int(cold_start_outcome_threshold)
        )

    def build(
        self,
        *,
        skill_snapshot: Any | None = None,
        skill_entries: Sequence[Any] = (),
        agent_definitions: Sequence[Any] = (),
        worker_pool: WorkerPool | None = None,
        worker_manifests: Sequence[Any] = (),
        physical_worker_manifests: Sequence[Any] = (),
        worker_health: Sequence[WorkerHealth | Mapping[str, Any]] = (),
        tool_registry: Any | None = None,
        tool_specs: Sequence[Any] = (),
        model_definitions: Sequence[Any] = (),
        built_at: str = "1970-01-01T00:00:00Z",
    ) -> OperatorCatalog:
        health_by_worker = {
            _token(_value(item, "worker_id")): item for item in worker_health
        }
        profiles: list[OperatorProfile] = []
        source_versions: dict[str, str] = {}

        if skill_snapshot is not None:
            generation = int(_value(skill_snapshot, "generation", default=0) or 0)
            snapshot_id = _token(
                _value(skill_snapshot, "snapshot_id", default=f"generation-{generation}")
            )
            source_versions["skill_registry"] = f"{generation}:{snapshot_id}"
            active = _mapping(
                _value(skill_snapshot, "active_by_qualified_name", default={})
            )
            revisions = _mapping(
                _value(skill_snapshot, "revisions_by_ref", default={})
            )
            for qualified_name, ref in sorted(active.items()):
                revision = revisions.get(ref)
                if revision is not None:
                    profiles.append(
                        self._skill_profile(
                            revision,
                            qualified_name=_token(qualified_name),
                            registry_version=f"{generation}:{snapshot_id}",
                        )
                    )

        if skill_entries:
            digest = canonical_digest(
                [_public_mapping(item) for item in skill_entries]
            )
            source_versions["skill_listing"] = digest
            for item in skill_entries:
                profiles.append(
                    self._skill_listing_profile(
                        item,
                        registry_version=digest,
                    )
                )

        if agent_definitions:
            digest = canonical_digest(
                [_public_mapping(item) for item in agent_definitions]
            )
            source_versions["agent_registry"] = digest
            profiles.extend(
                self._agent_profile(item, registry_version=digest)
                for item in agent_definitions
            )

        selected_workers = [
            *(worker_pool.manifests() if worker_pool is not None else ()),
            *worker_manifests,
        ]
        if selected_workers:
            digest = canonical_digest(
                [_public_mapping(item) for item in selected_workers]
            )
            source_versions["worker_pool"] = digest
            profiles.extend(
                self._worker_profile(
                    item,
                    registry_version=digest,
                    health=health_by_worker.get(
                        _token(_value(item, "worker_id"))
                    ),
                )
                for item in selected_workers
            )

        if physical_worker_manifests:
            digest = canonical_digest(
                [_public_mapping(item) for item in physical_worker_manifests]
            )
            source_versions["physical_worker_registry"] = digest
            profiles.extend(
                self._physical_worker_profile(
                    item,
                    registry_version=digest,
                    health=health_by_worker.get(
                        _token(_value(item, "worker_id"))
                    ),
                )
                for item in physical_worker_manifests
            )

        selected_tools = [
            *(tool_registry.list() if tool_registry is not None else ()),
            *tool_specs,
        ]
        if selected_tools:
            digest = canonical_digest(
                [_public_mapping(item) for item in selected_tools]
            )
            source_versions["tool_registry"] = digest
            profiles.extend(
                self._tool_profile(item, registry_version=digest)
                for item in selected_tools
            )

        if model_definitions:
            digest = canonical_digest(
                [_public_mapping(item) for item in model_definitions]
            )
            source_versions["model_registry"] = digest
            profiles.extend(
                self._model_profile(item, registry_version=digest)
                for item in model_definitions
            )

        active_profiles = tuple(
            item for item in profiles if item.enabled and not item.revoked
        )
        return OperatorCatalog(
            entries=active_profiles,
            source_versions=FrozenDict(source_versions),
            generation=max(
                [int(_value(skill_snapshot, "generation", default=0) or 0)]
                + [len(active_profiles)]
            ),
            built_at=built_at,
        )

    def _confidence(self, outcome_count: int) -> tuple[bool, float]:
        cold_start = outcome_count < self.cold_start_outcome_threshold
        return (
            cold_start,
            self.cold_start_confidence
            if cold_start
            else self.established_confidence,
        )

    def _skill_profile(
        self,
        revision: Any,
        *,
        qualified_name: str,
        registry_version: str,
    ) -> OperatorProfile:
        metadata = _value(revision, "metadata")
        version_ref = _value(revision, "version_ref")
        operator = _operator_metadata(metadata)
        outcome_count = int(operator.get("outcome_count") or 0)
        cold_start, confidence = self._confidence(outcome_count)
        allowed_tools = _value(metadata, "allowed_tools", default=()) or ()
        permissions = operator.get("required_permissions") or (
            "skill.invoke",
            *(
                f"tool:{_token(_value(item, 'canonical_name', default=item))}"
                for item in allowed_tools
            ),
        )
        capabilities = (
            operator.get("capabilities")
            or _value(metadata, "extensions", default={}).get("capabilities")
            or (_value(metadata, "name", default=qualified_name),)
        )
        return OperatorProfile(
            operator_id=f"skill:{qualified_name}",
            operator_type=OperatorType.SKILL,
            version=_token(
                _value(
                    version_ref,
                    "content_digest",
                    default=_value(metadata, "declared_version", default="0"),
                )
            ),
            display_name=_token(
                _value(metadata, "display_name", default=qualified_name)
            )
            or qualified_name,
            description=_token(_value(metadata, "description", default=qualified_name)),
            capabilities=_tokens(capabilities),
            input_contract=_tokens(
                operator.get("input_contract") or ("instruction", "skill_context")
            ),
            output_contract=_tokens(
                operator.get("output_contract") or ("skill_result", "artifact_refs")
            ),
            required_permissions=_tokens(permissions),
            allowed_locations=_tokens(
                operator.get("allowed_locations") or ("local", "edge")
            ),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("internal", "masked", "project", "public", "sensitive"),
            ),
            estimated_tokens=int(
                operator.get("estimated_tokens")
                or _value(
                    _value(metadata, "context_budget"),
                    "listing_tokens",
                    default=256,
                )
            ),
            estimated_cost_usd=float(operator.get("estimated_cost_usd") or 0),
            estimated_latency_ms=int(operator.get("estimated_latency_ms") or 25),
            health_status=_token(operator.get("health_status") or "healthy"),
            available_capacity=int(operator.get("available_capacity") or 1),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("skill_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("skill_invocation_receipt", "artifact_or_result")
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="SkillRegistrySnapshot",
            source_registry_version=registry_version,
            source_ref=_token(_value(version_ref, "immutable_ref", default=qualified_name)),
            enabled=True,
            revoked=False,
            metadata=FrozenDict(operator),
        )

    def _skill_listing_profile(
        self,
        item: Any,
        *,
        registry_version: str,
    ) -> OperatorProfile:
        operator = _operator_metadata(item)
        outcome_count = int(operator.get("outcome_count") or 0)
        cold_start, confidence = self._confidence(outcome_count)
        qualified_name = _token(
            _value(item, "qualified_name", "name", default="unknown-skill")
        )
        return OperatorProfile(
            operator_id=f"skill:{qualified_name}",
            operator_type=OperatorType.SKILL,
            version=_token(
                _value(item, "content_digest", "declared_version", default="0")
            ),
            display_name=qualified_name,
            description=_token(_value(item, "description", default=qualified_name)),
            capabilities=_tokens(
                operator.get("capabilities")
                or (_value(item, "name", default=qualified_name),)
            ),
            input_contract=_tokens(
                operator.get("input_contract") or ("instruction", "skill_context")
            ),
            output_contract=_tokens(
                operator.get("output_contract") or ("skill_result", "artifact_refs")
            ),
            required_permissions=_tokens(
                operator.get("required_permissions") or ("skill.invoke",)
            ),
            allowed_locations=_tokens(
                operator.get("allowed_locations") or ("local", "edge")
            ),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("internal", "masked", "project", "public", "sensitive"),
            ),
            estimated_tokens=int(operator.get("estimated_tokens") or 256),
            estimated_cost_usd=float(operator.get("estimated_cost_usd") or 0),
            estimated_latency_ms=int(operator.get("estimated_latency_ms") or 25),
            health_status=_token(operator.get("health_status") or "healthy"),
            available_capacity=int(operator.get("available_capacity") or 1),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("skill_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("skill_invocation_receipt",)
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="SkillRegistryListing",
            source_registry_version=registry_version,
            source_ref=qualified_name,
            enabled=str(_value(item, "lifecycle", default="active")) == "active",
            revoked=str(_value(item, "lifecycle", default="active"))
            in {"revoked", "invalid", "superseded"},
            metadata=FrozenDict(operator),
        )

    def _agent_profile(
        self,
        item: Any,
        *,
        registry_version: str,
    ) -> OperatorProfile:
        operator = _operator_metadata(item)
        outcome_count = int(operator.get("outcome_count") or 0)
        cold_start, confidence = self._confidence(outcome_count)
        agent_type = _token(_value(item, "agent_type", "name"))
        budget = _value(item, "budget")
        permission_mode = _token(_value(item, "permission_mode", default="default"))
        return OperatorProfile(
            operator_id=f"agent:{agent_type}",
            operator_type=OperatorType.AGENT,
            version=_token(_value(item, "version", default="1")),
            display_name=agent_type,
            description=_token(_value(item, "description", default=agent_type)),
            capabilities=_tokens(
                _value(item, "capabilities", default=())
                or operator.get("capabilities")
                or (agent_type,)
            ),
            input_contract=_tokens(
                operator.get("input_contract") or ("task", "bounded_context")
            ),
            output_contract=_tokens(
                operator.get("output_contract")
                or ("subagent_result", "artifact_refs", "usage")
            ),
            required_permissions=_tokens(
                operator.get("required_permissions")
                or ("agent.spawn", f"permission_mode:{permission_mode}")
            ),
            allowed_locations=_tokens(
                operator.get("allowed_locations") or ("local", "edge")
            ),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("internal", "masked", "project", "public", "sensitive"),
            ),
            estimated_tokens=int(
                operator.get("estimated_tokens")
                or _value(budget, "max_input_tokens", default=4096)
            ),
            estimated_cost_usd=float(operator.get("estimated_cost_usd") or 0),
            estimated_latency_ms=int(
                operator.get("estimated_latency_ms")
                or min(30_000, int(_value(budget, "max_wall_time_ms", default=1000)))
            ),
            health_status=_token(operator.get("health_status") or "healthy"),
            available_capacity=int(
                operator.get("available_capacity")
                or _value(budget, "max_children", default=1)
            ),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("subagent_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("subagent_dispatch_receipt", "subagent_result")
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="AgentDefinitionRegistry",
            source_registry_version=registry_version,
            source_ref=_token(_value(item, "definition_id", default=agent_type)),
            enabled=bool(_value(item, "enabled", default=True)),
            revoked=bool(operator.get("revoked", False)),
            metadata=FrozenDict(operator),
        )

    def _worker_profile(
        self,
        item: WorkerManifest | Any,
        *,
        registry_version: str,
        health: Any | None,
    ) -> OperatorProfile:
        operator = _operator_metadata(item)
        worker_id = _token(_value(item, "worker_id"))
        outcome_count = int(
            operator.get("outcome_count")
            or _value(health, "recent_successes", default=0)
            or 0
        )
        cold_start, confidence = self._confidence(outcome_count)
        health_status = _token(
            _value(health, "status", default=operator.get("health_status") or "unknown")
        )
        maximum = int(_value(item, "max_concurrency", default=1) or 1)
        current = int(
            _value(health, "current_load", default=_value(item, "current_load", default=0))
            or 0
        )
        return OperatorProfile(
            operator_id=f"worker:{worker_id}",
            operator_type=OperatorType.WORKER,
            version=_token(
                operator.get("manifest_version")
                or operator.get("version")
                or "1"
            ),
            display_name=_token(_value(item, "display_name", default=worker_id)),
            description=_token(
                operator.get("description")
                or f"Registered worker {_value(item, 'runtime_worker', default=worker_id)}"
            ),
            capabilities=_tokens(
                (
                    *_value(item, "capabilities", default=()),
                    *_value(item, "tools", default=()),
                )
            ),
            input_contract=_tokens(
                operator.get("input_contract") or ("task", "worker_envelope")
            ),
            output_contract=_tokens(
                operator.get("output_contract")
                or ("worker_result", "artifact_refs", "usage")
            ),
            required_permissions=_tokens(
                operator.get("required_permissions") or ("worker.dispatch",)
            ),
            allowed_locations=_tokens((_value(item, "location", default="local"),)),
            allowed_privacy_classes=_privacy_classes(
                _value(item, "privacy_level", default=""),
                fallback=("internal", "project", "public"),
            ),
            estimated_tokens=int(operator.get("estimated_tokens") or 512),
            estimated_cost_usd=float(
                operator.get("estimated_cost_usd")
                or float(_value(item, "cost_per_1k_tokens", default=0) or 0)
                * 0.512
            ),
            estimated_latency_ms=int(
                operator.get("estimated_latency_ms")
                or _value(item, "latency_ms", default=50)
            ),
            health_status=health_status,
            available_capacity=max(0, maximum - current),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("worker_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("worker_dispatch_receipt", "worker_result")
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="WorkerPool",
            source_registry_version=registry_version,
            source_ref=worker_id,
            enabled=bool(_value(item, "enabled", default=True)),
            revoked=bool(operator.get("revoked", False)),
            metadata=FrozenDict(operator),
        )

    def _physical_worker_profile(
        self,
        item: Any,
        *,
        registry_version: str,
        health: Any | None,
    ) -> OperatorProfile:
        worker_id = _token(_value(item, "worker_id"))
        operator = _operator_metadata(item)
        outcome_count = int(
            operator.get("outcome_count")
            or _value(health, "recent_successes", default=0)
            or 0
        )
        cold_start, confidence = self._confidence(outcome_count)
        capacity = _value(item, "resource_capacity")
        return OperatorProfile(
            operator_id=f"worker:{worker_id}",
            operator_type=OperatorType.WORKER,
            version=str(_value(item, "manifest_revision", default=1)),
            display_name=worker_id,
            description=_token(
                operator.get("description")
                or f"Physical worker capability manifest for {worker_id}"
            ),
            capabilities=_tokens(
                (
                    *_value(item, "capabilities", default=()),
                    *_value(item, "tool_ids", default=()),
                )
            ),
            input_contract=_tokens(
                operator.get("input_contract") or ("task", "lease_candidate")
            ),
            output_contract=_tokens(
                operator.get("output_contract") or ("physical_worker_result",)
            ),
            required_permissions=_tokens(
                operator.get("required_permissions") or ("worker.dispatch",)
            ),
            allowed_locations=_tokens((_value(item, "location", default="local"),)),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("internal", "masked", "project", "public", "sensitive"),
            ),
            estimated_tokens=int(operator.get("estimated_tokens") or 512),
            estimated_cost_usd=float(operator.get("estimated_cost_usd") or 0),
            estimated_latency_ms=int(operator.get("estimated_latency_ms") or 50),
            health_status=_token(
                _value(health, "status", default="unknown")
            ),
            available_capacity=max(
                0,
                int(
                    operator.get("available_capacity")
                    or _value(capacity, "process_slots", default=1)
                    or 1
                ),
            ),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("worker_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("worker_lease_candidate_receipt",)
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="WorkerPoolFoundationRuntime",
            source_registry_version=registry_version,
            source_ref=_token(_value(item, "digest", default=worker_id)),
            enabled=True,
            revoked=bool(operator.get("revoked", False)),
            metadata=FrozenDict(operator),
        )

    def _tool_profile(
        self,
        item: Any,
        *,
        registry_version: str,
    ) -> OperatorProfile:
        operator = _operator_metadata(item)
        name = _token(_value(item, "name"))
        outcome_count = int(operator.get("outcome_count") or 0)
        cold_start, confidence = self._confidence(outcome_count)
        input_schema = _mapping(_value(item, "input_schema", default={}))
        output_schema = _mapping(_value(item, "output_schema", default={}))
        return OperatorProfile(
            operator_id=f"tool:{name}",
            operator_type=OperatorType.TOOL,
            version=_token(operator.get("version") or "1"),
            display_name=name,
            description=_token(_value(item, "purpose", default=name)),
            capabilities=_tokens(
                operator.get("capabilities") or (name, _value(item, "source", default=""))
            ),
            input_contract=_tokens(
                operator.get("input_contract")
                or tuple(input_schema.get("required") or ())
                or ("arguments",)
            ),
            output_contract=_tokens(
                operator.get("output_contract")
                or tuple(_mapping(output_schema.get("properties")).keys())
                or ("tool_result",)
            ),
            required_permissions=_tokens(
                operator.get("required_permissions")
                or operator.get("permission_action")
                or (f"tool:{name}",)
            ),
            allowed_locations=_tokens(
                operator.get("allowed_locations") or ("local",)
            ),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("internal", "masked", "project", "public", "sensitive"),
            ),
            estimated_tokens=int(operator.get("estimated_tokens") or 128),
            estimated_cost_usd=float(operator.get("estimated_cost_usd") or 0),
            estimated_latency_ms=int(operator.get("estimated_latency_ms") or 25),
            health_status=_token(operator.get("health_status") or "healthy"),
            available_capacity=int(operator.get("available_capacity") or 1),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("tool_result_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("tool_call_receipt", "tool_result")
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="ToolRegistry",
            source_registry_version=registry_version,
            source_ref=f"{_value(item, 'source', default='tool')}:{name}",
            enabled=bool(operator.get("enabled", True)),
            revoked=bool(operator.get("revoked", False)),
            metadata=FrozenDict(operator),
        )

    def _model_profile(
        self,
        item: Any,
        *,
        registry_version: str,
    ) -> OperatorProfile:
        operator = _operator_metadata(item)
        provider_id = _token(_value(item, "provider_id", "providerId"))
        model_id = _token(_value(item, "model_id", "modelId"))
        capabilities = _value(item, "capabilities", default={})
        outcome_count = int(operator.get("outcome_count") or 0)
        cold_start, confidence = self._confidence(outcome_count)
        status = _token(_value(item, "status", default="active")).lower()
        pricing = _value(item, "pricing", default=()) or ()
        estimated_cost = float(operator.get("estimated_cost_usd") or 0)
        if not estimated_cost:
            estimated_cost = _price_estimate(pricing)
        capability_tokens = list(operator.get("capabilities") or ())
        if isinstance(capabilities, Mapping):
            capability_tokens.extend(_tokens(capabilities.get("input")))
            capability_tokens.extend(_tokens(capabilities.get("output")))
            capability_tokens.extend(
                key for key, enabled in capabilities.items() if enabled is True
            )
        else:
            capability_tokens.extend(_tokens(_value(capabilities, "input", default=())))
            capability_tokens.extend(_tokens(_value(capabilities, "output", default=())))
            for name in ("tools", "streaming", "reasoning", "structured_output"):
                if bool(_value(capabilities, name, default=False)):
                    capability_tokens.append(name)
        return OperatorProfile(
            operator_id=f"model:{provider_id}/{model_id}",
            operator_type=OperatorType.MODEL,
            version=_token(
                operator.get("version")
                or _value(item, "released_at", "releasedAt", default="1")
                or "1"
            ),
            display_name=_token(
                _value(item, "display_name", "displayName", default=model_id)
            ),
            description=_token(
                operator.get("description")
                or f"{provider_id} model {_value(item, 'family', default=model_id)}"
            ),
            capabilities=_tokens(capability_tokens or (model_id,)),
            input_contract=_tokens(
                operator.get("input_contract")
                or _value(capabilities, "input", default=("text",))
            ),
            output_contract=_tokens(
                operator.get("output_contract")
                or _value(capabilities, "output", default=("text",))
            ),
            required_permissions=_tokens(
                operator.get("required_permissions") or ("model.invoke",)
            ),
            allowed_locations=_tokens(
                operator.get("allowed_locations") or ("cloud",)
            ),
            allowed_privacy_classes=_privacy_classes(
                operator.get("allowed_privacy_classes"),
                fallback=("masked", "public"),
            ),
            estimated_tokens=int(operator.get("estimated_tokens") or 1024),
            estimated_cost_usd=estimated_cost,
            estimated_latency_ms=int(operator.get("estimated_latency_ms") or 250),
            health_status=(
                "healthy" if status in {"active", "available", "healthy"} else status
            ),
            available_capacity=int(operator.get("available_capacity") or 1),
            verifier_contracts=_tokens(
                operator.get("verifier_contracts") or ("provider_response_verifier",)
            ),
            minimum_evidence_contract=_tokens(
                operator.get("minimum_evidence_contract")
                or ("provider_request_receipt", "provider_usage")
            ),
            cold_start=cold_start,
            confidence=confidence,
            outcome_count=outcome_count,
            source_registry="ProviderControlPlane",
            source_registry_version=registry_version,
            source_ref=f"{provider_id}/{model_id}",
            enabled=bool(_value(item, "enabled", default=True)),
            revoked=status in {"revoked", "disabled", "retired"},
            metadata=FrozenDict(operator),
        )


def _price_estimate(pricing: Sequence[Any]) -> float:
    values: list[float] = []
    for item in pricing:
        if not isinstance(item, Mapping):
            continue
        for key in (
            "input_cost_per_1k",
            "cost_per_1k_tokens",
            "price",
            "input",
        ):
            try:
                if item.get(key) is not None:
                    values.append(float(item[key]) / 1000.0)
            except (TypeError, ValueError):
                continue
    return round(min(values), 8) if values else 0.0


def _public_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return dict(item)
    to_wire = getattr(item, "to_wire", None)
    if callable(to_wire):
        return dict(to_wire())
    to_dict = getattr(item, "to_dict", None)
    if callable(to_dict):
        try:
            value = to_dict(include_prompt=False)
        except TypeError:
            value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    fields = getattr(item, "__dataclass_fields__", {})
    return {
        name: _value(item, name)
        for name in fields
        if name not in {"system_prompt", "execution_provenance"}
    }
