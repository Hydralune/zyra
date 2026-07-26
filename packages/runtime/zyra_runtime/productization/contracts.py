from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


OWNER_RECEIPT_SCHEMA = "zyra.runtime-owner-receipt/v1"
CAUSAL_RECEIPT_SCHEMA = "zyra.runtime-causal-receipt/v1"
ABSORPTION_RECEIPT_SCHEMA = "zyra.m3-runtime-absorption-receipt/v1"
M3_QUEUE_SCHEMA = "zyra.m3-freeze-audit-work-queue/v1"
M3_SOURCE_SLICE = "M3-S01A-02"
M3_OWNER_UNIT = "M3-01B"

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,255}$")
DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
WORK_ID_PATTERN = re.compile(r"^m3_work_[0-9a-f]{20}$")
FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{24}$")


class ProductizationContractError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": str(self),
            "details": canonicalize(self.details),
        }


class RuntimeDomain(StrEnum):
    SESSION_EVENT_PROJECTION = "session_event_projection"
    PERMISSION = "permission"
    MEMORY_COMPACT = "memory_compact"
    SCHEDULER_RECOVERY = "scheduler_recovery"
    ARTIFACT = "artifact"
    WORKER_ROUTE = "worker_route"
    GRAPH_CHECKPOINT = "graph_checkpoint"
    PROVIDER_CREDENTIAL_FAILOVER = "provider_credential_failover"
    MCP_PLUGIN_REGISTRY = "mcp_plugin_registry"
    GATEWAY_LEASE_BUSY = "gateway_lease_busy"
    TERMINAL_BROWSER_SESSION = "terminal_browser_session"


REQUIRED_RUNTIME_DOMAINS = tuple(RuntimeDomain)


class OwnerRole(StrEnum):
    OWNER = "owner"
    STORE = "store"
    WRITER = "writer"
    CHECKPOINT = "checkpoint"
    RECOVERY = "recovery"
    PROJECTION = "projection"
    CACHE = "cache"
    ADAPTER = "adapter"
    GUARD = "guard"

    @property
    def may_claim_canonical_state(self) -> bool:
        return self in {
            OwnerRole.OWNER,
            OwnerRole.STORE,
            OwnerRole.WRITER,
            OwnerRole.CHECKPOINT,
            OwnerRole.RECOVERY,
        }

    @property
    def subordinate_only(self) -> bool:
        return self in {
            OwnerRole.PROJECTION,
            OwnerRole.CACHE,
            OwnerRole.ADAPTER,
            OwnerRole.GUARD,
        }


class OwnerAvailability(StrEnum):
    UNKNOWN = "unknown"
    READY = "ready"
    DEGRADED = "degraded"
    DISABLED = "disabled"
    LOST = "lost"
    FAILED = "failed"

    @property
    def admits_writes(self) -> bool:
        return self is OwnerAvailability.READY


class RuntimeSurface(StrEnum):
    API = "api"
    CLI = "cli"
    WEB = "web"
    WORKER = "worker"


class WorkAction(StrEnum):
    RESOLVE_OWNER = "resolve_owner"
    REWIRE_DEFAULT = "rewire_default"
    REPAIR_CAUSALITY = "repair_causality"
    DISPOSE_SOURCE_RISK = "dispose_source_risk"


class WorkPriority(StrEnum):
    P0 = "p0"
    P1 = "p1"
    P3 = "p3"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    EXTERNALIZED = "externalized"
    RETAINED_PROVEN = "retained_proven"
    NON_RUNTIME = "non_runtime"
    OPEN = "open"
    REJECTED = "rejected"

    @property
    def closes_blocker(self) -> bool:
        return self in {
            ResolutionStatus.RESOLVED,
            ResolutionStatus.EXTERNALIZED,
            ResolutionStatus.RETAINED_PROVEN,
            ResolutionStatus.NON_RUNTIME,
        }


class CausalEffectKind(StrEnum):
    STATE_MUTATION = "state_mutation"
    PERMISSION = "permission"
    COMPACT = "compact"
    RECOVERY = "recovery"
    ARTIFACT = "artifact"
    PLACEMENT = "placement"
    ROUTE = "route"
    TOOL = "tool"


class EventSemanticClass(StrEnum):
    CANONICAL_EFFECT = "canonical_effect"
    DERIVED_PROJECTION = "derived_projection"
    DIAGNOSTIC = "diagnostic"
    HEARTBEAT = "heartbeat"
    REPLAY = "replay"
    UI_ONLY = "ui_only"
    UNKNOWN = "unknown"

    @property
    def counts_as_effect(self) -> bool:
        return self is EventSemanticClass.CANONICAL_EFFECT


@dataclass(frozen=True, slots=True)
class CodeReference:
    path: str
    symbol: str
    language: str
    role: OwnerRole

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", normalize_repo_path(self.path))
        object.__setattr__(self, "symbol", require_token(self.symbol, "symbol"))
        language = require_token(self.language.casefold(), "language")
        if language not in {"python", "typescript", "tsx"}:
            raise ProductizationContractError(
                "unsupported_language",
                f"unsupported code-reference language: {language}",
            )
        object.__setattr__(self, "language", language)

    @property
    def identity(self) -> str:
        return f"{self.language}:{self.path}#{self.symbol}:{self.role.value}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "language": self.language,
            "role": self.role.value,
        }


@dataclass(frozen=True, slots=True)
class OwnerBinding:
    domain: RuntimeDomain
    owner: CodeReference
    store: CodeReference
    writers: tuple[CodeReference, ...]
    checkpoint: CodeReference
    recovery: CodeReference
    projections: tuple[CodeReference, ...] = ()
    caches: tuple[CodeReference, ...] = ()
    fallback_refs: tuple[CodeReference, ...] = ()
    default_entries: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.owner.role is not OwnerRole.OWNER:
            raise ProductizationContractError(
                "owner_role_invalid",
                "canonical owner reference must use role=owner",
                details={"domain": self.domain.value, "role": self.owner.role.value},
            )
        if self.store.role not in {OwnerRole.STORE, OwnerRole.OWNER}:
            raise ProductizationContractError(
                "store_role_invalid",
                "durable store reference must use role=store or role=owner",
                details={"domain": self.domain.value, "role": self.store.role.value},
            )
        if not self.writers:
            raise ProductizationContractError(
                "writer_missing",
                f"{self.domain.value} has no canonical writer",
            )
        for writer in self.writers:
            if writer.role is not OwnerRole.WRITER:
                raise ProductizationContractError(
                    "writer_role_invalid",
                    f"{writer.identity} is not a writer reference",
                )
        if self.checkpoint.role not in {
            OwnerRole.CHECKPOINT,
            OwnerRole.STORE,
            OwnerRole.OWNER,
        }:
            raise ProductizationContractError(
                "checkpoint_role_invalid",
                f"{self.checkpoint.identity} is not a checkpoint boundary",
            )
        if self.recovery.role not in {
            OwnerRole.RECOVERY,
            OwnerRole.OWNER,
            OwnerRole.WRITER,
        }:
            raise ProductizationContractError(
                "recovery_role_invalid",
                f"{self.recovery.identity} is not a recovery boundary",
            )
        for projection in self.projections:
            if projection.role is not OwnerRole.PROJECTION:
                raise ProductizationContractError(
                    "projection_role_invalid",
                    f"{projection.identity} cannot be registered as a projection",
                )
        for cache in self.caches:
            if cache.role is not OwnerRole.CACHE:
                raise ProductizationContractError(
                    "cache_role_invalid",
                    f"{cache.identity} cannot be registered as a cache",
                )
        canonical = {
            self.owner.identity,
            self.store.identity,
            *(item.identity for item in self.writers),
        }
        subordinate = {
            *(item.identity for item in self.projections),
            *(item.identity for item in self.caches),
        }
        overlap = canonical & subordinate
        if overlap:
            raise ProductizationContractError(
                "subordinate_owner_overlap",
                "projection/cache reference overlaps a canonical state role",
                details={"references": sorted(overlap)},
            )
        normalized_entries = tuple(
            dict.fromkeys(require_token(value, "default_entry") for value in self.default_entries)
        )
        if not normalized_entries:
            raise ProductizationContractError(
                "default_entry_missing",
                f"{self.domain.value} has no product default entry",
            )
        object.__setattr__(self, "default_entries", normalized_entries)

    @property
    def owner_token(self) -> str:
        return digest_payload(
            {
                "domain": self.domain.value,
                "owner": self.owner.to_dict(),
                "store": self.store.to_dict(),
            }
        )

    def all_references(self) -> tuple[CodeReference, ...]:
        return (
            self.owner,
            self.store,
            *self.writers,
            self.checkpoint,
            self.recovery,
            *self.projections,
            *self.caches,
            *self.fallback_refs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain.value,
            "owner": self.owner.to_dict(),
            "store": self.store.to_dict(),
            "writers": [item.to_dict() for item in self.writers],
            "checkpoint": self.checkpoint.to_dict(),
            "recovery": self.recovery.to_dict(),
            "projections": [item.to_dict() for item in self.projections],
            "caches": [item.to_dict() for item in self.caches],
            "fallback_refs": [item.to_dict() for item in self.fallback_refs],
            "default_entries": list(self.default_entries),
            "owner_token": self.owner_token,
        }


@dataclass(frozen=True, slots=True)
class OwnerProbeResult:
    domain: RuntimeDomain
    availability: OwnerAvailability
    owner_token: str
    generation: int
    checked_at_ns: int
    latency_ns: int
    write_path_ready: bool
    checkpoint_ready: bool
    recovery_ready: bool
    fallback_active: bool = False
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_digest(self.owner_token, "owner_token")
        if self.generation < 1:
            raise ProductizationContractError(
                "owner_generation_invalid",
                "owner generation must be positive",
            )
        if self.checked_at_ns < 0 or self.latency_ns < 0:
            raise ProductizationContractError(
                "probe_clock_invalid",
                "probe timestamps cannot be negative",
            )
        if self.fallback_active and self.availability is OwnerAvailability.READY:
            raise ProductizationContractError(
                "fallback_reported_ready",
                "a fallback-owned domain cannot report ready",
                details={"domain": self.domain.value},
            )

    @property
    def ready(self) -> bool:
        return (
            self.availability is OwnerAvailability.READY
            and self.write_path_ready
            and self.checkpoint_ready
            and self.recovery_ready
            and not self.fallback_active
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain.value,
            "availability": self.availability.value,
            "owner_token": self.owner_token,
            "generation": self.generation,
            "checked_at_ns": self.checked_at_ns,
            "latency_ns": self.latency_ns,
            "write_path_ready": self.write_path_ready,
            "checkpoint_ready": self.checkpoint_ready,
            "recovery_ready": self.recovery_ready,
            "fallback_active": self.fallback_active,
            "reason": self.reason,
            "details": canonicalize(self.details),
            "ready": self.ready,
        }


@dataclass(frozen=True, slots=True)
class OwnerLease:
    lease_id: str
    domain: RuntimeDomain
    owner_token: str
    generation: int
    operation: str
    acquired_at_ns: int
    expires_at_ns: int
    correlation_id: str

    def __post_init__(self) -> None:
        require_token(self.lease_id, "lease_id")
        require_digest(self.owner_token, "owner_token")
        require_token(self.operation, "operation")
        require_token(self.correlation_id, "correlation_id")
        if self.generation < 1:
            raise ProductizationContractError(
                "lease_generation_invalid",
                "lease generation must be positive",
            )
        if self.expires_at_ns <= self.acquired_at_ns:
            raise ProductizationContractError(
                "lease_expiry_invalid",
                "owner lease expiry must follow acquisition",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "domain": self.domain.value,
            "owner_token": self.owner_token,
            "generation": self.generation,
            "operation": self.operation,
            "acquired_at_ns": self.acquired_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "correlation_id": self.correlation_id,
        }


@dataclass(frozen=True, slots=True)
class MutationIdentity:
    run_id: str
    task_id: str
    mutation_id: str
    revision: int
    correlation_id: str
    causation_id: str
    span_id: str = ""
    call_id: str = ""
    owner_generation: int = 1

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "task_id",
            "mutation_id",
            "correlation_id",
            "causation_id",
        ):
            require_token(getattr(self, name), name)
        for name in ("span_id", "call_id"):
            value = getattr(self, name)
            if value:
                require_token(value, name)
        if self.revision < 1 or self.owner_generation < 1:
            raise ProductizationContractError(
                "mutation_revision_invalid",
                "mutation revision and owner generation must be positive",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "mutation_id": self.mutation_id,
            "revision": self.revision,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "span_id": self.span_id,
            "call_id": self.call_id,
            "owner_generation": self.owner_generation,
        }


@dataclass(frozen=True, slots=True)
class MutationReceipt:
    receipt_id: str
    domain: RuntimeDomain
    owner_token: str
    identity: MutationIdentity
    effect_kind: CausalEffectKind
    effect_ref: str
    effect_digest: str
    committed_at_ns: int
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_token(self.receipt_id, "receipt_id")
        require_digest(self.owner_token, "owner_token")
        require_token(self.effect_ref, "effect_ref")
        require_digest(self.effect_digest, "effect_digest")
        if self.committed_at_ns < 1:
            raise ProductizationContractError(
                "mutation_commit_clock_invalid",
                "mutation receipt requires a positive commit clock",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CAUSAL_RECEIPT_SCHEMA,
            "receipt_id": self.receipt_id,
            "domain": self.domain.value,
            "owner_token": self.owner_token,
            "identity": self.identity.to_dict(),
            "effect_kind": self.effect_kind.value,
            "effect_ref": self.effect_ref,
            "effect_digest": self.effect_digest,
            "committed_at_ns": self.committed_at_ns,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CausalEventContract:
    link_id: str
    domain: RuntimeDomain
    event_name: str
    effect_kind: CausalEffectKind
    required_attributes: tuple[str, ...]
    aliases: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        require_token(self.link_id, "link_id")
        require_token(self.event_name, "event_name")
        attributes = tuple(
            dict.fromkeys(require_token(item, "required_attribute") for item in self.required_attributes)
        )
        if not attributes:
            raise ProductizationContractError(
                "causal_attributes_missing",
                f"{self.link_id} has no identity/effect attributes",
            )
        object.__setattr__(self, "required_attributes", attributes)
        normalized_aliases: dict[str, tuple[str, ...]] = {}
        for key, values in self.aliases.items():
            canonical = require_token(key, "attribute_alias_key")
            if canonical not in attributes:
                raise ProductizationContractError(
                    "attribute_alias_unknown",
                    f"alias key {canonical} is not a required attribute",
                )
            normalized_aliases[canonical] = tuple(
                dict.fromkeys(require_token(value, "attribute_alias") for value in values)
            )
        object.__setattr__(self, "aliases", normalized_aliases)

    def accepted_names(self, attribute: str) -> tuple[str, ...]:
        return (attribute, *self.aliases.get(attribute, ()))

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "domain": self.domain.value,
            "event_name": self.event_name,
            "effect_kind": self.effect_kind.value,
            "required_attributes": list(self.required_attributes),
            "aliases": {key: list(value) for key, value in sorted(self.aliases.items())},
        }


@dataclass(frozen=True, slots=True)
class CanonicalEventEnvelope:
    event_id: str
    event_name: str
    domain: RuntimeDomain
    attributes: Mapping[str, Any]
    emitted_at_ns: int
    semantic_class: EventSemanticClass
    mutation_receipt_id: str = ""

    def __post_init__(self) -> None:
        require_token(self.event_id, "event_id")
        require_token(self.event_name, "event_name")
        if self.emitted_at_ns < 1:
            raise ProductizationContractError(
                "event_clock_invalid",
                "canonical event requires a positive emission clock",
            )
        if self.mutation_receipt_id:
            require_token(self.mutation_receipt_id, "mutation_receipt_id")

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_name": self.event_name,
            "domain": self.domain.value,
            "attributes": canonicalize(self.attributes),
            "emitted_at_ns": self.emitted_at_ns,
            "semantic_class": self.semantic_class.value,
            "mutation_receipt_id": self.mutation_receipt_id,
        }


@dataclass(frozen=True, slots=True)
class CausalVerification:
    link_id: str
    valid: bool
    event_id: str
    mutation_receipt_id: str
    missing_attributes: tuple[str, ...]
    mismatches: tuple[str, ...]
    reason: str
    digest: str

    def __post_init__(self) -> None:
        require_token(self.link_id, "link_id")
        if self.event_id:
            require_token(self.event_id, "event_id")
        if self.mutation_receipt_id:
            require_token(self.mutation_receipt_id, "mutation_receipt_id")
        require_digest(self.digest, "digest")
        if self.valid and (self.missing_attributes or self.mismatches):
            raise ProductizationContractError(
                "causal_verification_inconsistent",
                "valid causal verification cannot retain missing attributes or mismatches",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "link_id": self.link_id,
            "valid": self.valid,
            "event_id": self.event_id,
            "mutation_receipt_id": self.mutation_receipt_id,
            "missing_attributes": list(self.missing_attributes),
            "mismatches": list(self.mismatches),
            "reason": self.reason,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class M3WorkItem:
    work_id: str
    action: WorkAction
    blocking: bool
    priority: WorkPriority
    owner_unit: str
    finding_codes: tuple[str, ...]
    finding_fingerprints: tuple[str, ...]
    paths: tuple[str, ...]
    domains: tuple[RuntimeDomain, ...]
    requirements: tuple[str, ...]
    remediation: str
    default_path_impact: str

    def __post_init__(self) -> None:
        if not WORK_ID_PATTERN.fullmatch(self.work_id):
            raise ProductizationContractError(
                "work_id_invalid",
                f"invalid M3 work id: {self.work_id}",
            )
        if self.owner_unit != M3_OWNER_UNIT:
            raise ProductizationContractError(
                "work_owner_invalid",
                f"{self.work_id} belongs to {self.owner_unit}, not {M3_OWNER_UNIT}",
            )
        if not self.finding_codes or not self.finding_fingerprints:
            raise ProductizationContractError(
                "work_finding_identity_missing",
                f"{self.work_id} has no finding identity",
            )
        for code in self.finding_codes:
            require_token(code, "finding_code")
        for fingerprint in self.finding_fingerprints:
            if not FINGERPRINT_PATTERN.fullmatch(fingerprint):
                raise ProductizationContractError(
                    "finding_fingerprint_invalid",
                    f"{self.work_id} contains invalid fingerprint {fingerprint}",
                )
        for path in self.paths:
            normalize_repo_path(path)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> M3WorkItem:
        try:
            domains = tuple(RuntimeDomain(str(value)) for value in payload.get("domains", ()))
            return cls(
                work_id=str(payload["work_id"]),
                action=WorkAction(str(payload["action"])),
                blocking=bool(payload.get("blocking", False)),
                priority=WorkPriority(str(payload["priority"])),
                owner_unit=str(payload["owner_unit"]),
                finding_codes=tuple(str(value) for value in payload.get("finding_codes", ())),
                finding_fingerprints=tuple(
                    str(value) for value in payload.get("finding_fingerprints", ())
                ),
                paths=tuple(str(value) for value in payload.get("paths", ())),
                domains=domains,
                requirements=tuple(
                    str(value) for value in payload.get("requirements", ())
                ),
                remediation=str(payload.get("remediation", "")),
                default_path_impact=str(payload.get("default_path_impact", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ProductizationContractError(
                "work_item_invalid",
                f"invalid M3 work item: {error}",
            ) from error

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "action": self.action.value,
            "blocking": self.blocking,
            "priority": self.priority.value,
            "owner_unit": self.owner_unit,
            "finding_codes": list(self.finding_codes),
            "finding_fingerprints": list(self.finding_fingerprints),
            "paths": list(self.paths),
            "domains": [item.value for item in self.domains],
            "requirements": list(self.requirements),
            "remediation": self.remediation,
            "default_path_impact": self.default_path_impact,
        }


@dataclass(frozen=True, slots=True)
class M3WorkQueue:
    revision: str
    source_slice: str
    owner_unit: str
    receipt_digest: str
    digest: str
    items: tuple[M3WorkItem, ...]

    def __post_init__(self) -> None:
        require_git_revision(self.revision)
        if self.source_slice != M3_SOURCE_SLICE:
            raise ProductizationContractError(
                "queue_source_slice_invalid",
                f"unexpected queue source slice: {self.source_slice}",
            )
        if self.owner_unit != M3_OWNER_UNIT:
            raise ProductizationContractError(
                "queue_owner_invalid",
                f"unexpected queue owner: {self.owner_unit}",
            )
        require_digest(self.receipt_digest, "receipt_digest")
        require_digest(self.digest, "digest")
        if len(self.items) != 119:
            raise ProductizationContractError(
                "queue_population_invalid",
                f"M3-01B queue must contain 119 items, found {len(self.items)}",
            )
        identities = [item.work_id for item in self.items]
        if len(identities) != len(set(identities)):
            raise ProductizationContractError(
                "queue_work_id_duplicate",
                "M3-01B queue contains duplicate work ids",
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> M3WorkQueue:
        if str(payload.get("schema", "")) != M3_QUEUE_SCHEMA:
            raise ProductizationContractError(
                "queue_schema_invalid",
                f"expected {M3_QUEUE_SCHEMA}",
            )
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ProductizationContractError(
                "queue_items_invalid",
                "M3 work queue items must be a list",
            )
        queue = cls(
            revision=str(payload.get("revision", "")),
            source_slice=str(payload.get("source_slice", "")),
            owner_unit=str(payload.get("owner_unit", "")),
            receipt_digest=str(payload.get("receipt_digest", "")),
            digest=str(payload.get("digest", "")),
            items=tuple(M3WorkItem.from_dict(item) for item in raw_items),
        )
        calculated = digest_payload(
            {
                "schema": M3_QUEUE_SCHEMA,
                "source_slice": queue.source_slice,
                "owner_unit": queue.owner_unit,
                "revision": queue.revision,
                "receipt_digest": queue.receipt_digest,
                "items": [item.to_dict() for item in queue.items],
            }
        )
        if calculated != queue.digest:
            raise ProductizationContractError(
                "queue_digest_mismatch",
                "M3-01B queue digest does not match its contents",
                details={"expected": queue.digest, "actual": calculated},
            )
        return queue

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": M3_QUEUE_SCHEMA,
            "source_slice": self.source_slice,
            "owner_unit": self.owner_unit,
            "revision": self.revision,
            "receipt_digest": self.receipt_digest,
            "digest": self.digest,
            "items": [item.to_dict() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class WorkResolution:
    work_id: str
    status: ResolutionStatus
    evidence_codes: tuple[str, ...]
    evidence_paths: tuple[str, ...]
    owner_receipt_ids: tuple[str, ...] = ()
    causal_verification_ids: tuple[str, ...] = ()
    reason: str = ""
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not WORK_ID_PATTERN.fullmatch(self.work_id):
            raise ProductizationContractError(
                "resolution_work_id_invalid",
                f"invalid work resolution id: {self.work_id}",
            )
        if not self.evidence_codes:
            raise ProductizationContractError(
                "resolution_evidence_missing",
                f"{self.work_id} has no evidence code",
            )
        for code in self.evidence_codes:
            require_token(code, "resolution_evidence_code")
        for path in self.evidence_paths:
            normalize_repo_path(path)
        for receipt_id in (*self.owner_receipt_ids, *self.causal_verification_ids):
            require_token(receipt_id, "resolution_receipt_id")

    @property
    def resolved(self) -> bool:
        return self.status.closes_blocker

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_id": self.work_id,
            "status": self.status.value,
            "resolved": self.resolved,
            "evidence_codes": list(self.evidence_codes),
            "evidence_paths": list(self.evidence_paths),
            "owner_receipt_ids": list(self.owner_receipt_ids),
            "causal_verification_ids": list(self.causal_verification_ids),
            "reason": self.reason,
            "details": canonicalize(self.details),
        }


@dataclass(frozen=True, slots=True)
class RuntimeReadiness:
    revision: str
    generated_at_ns: int
    owner_results: tuple[OwnerProbeResult, ...]
    unresolved_work_ids: tuple[str, ...]
    source_boundary_ready: bool
    causal_boundary_ready: bool
    module_enabled: bool
    digest: str

    def __post_init__(self) -> None:
        require_git_revision(self.revision)
        require_digest(self.digest, "readiness_digest")
        if self.generated_at_ns < 1:
            raise ProductizationContractError(
                "readiness_clock_invalid",
                "runtime readiness needs a positive generation clock",
            )
        domains = [item.domain for item in self.owner_results]
        if len(domains) != len(set(domains)):
            raise ProductizationContractError(
                "readiness_owner_duplicate",
                "runtime readiness contains duplicate domains",
            )
        for work_id in self.unresolved_work_ids:
            if not WORK_ID_PATTERN.fullmatch(work_id):
                raise ProductizationContractError(
                    "readiness_work_id_invalid",
                    f"invalid unresolved work id: {work_id}",
                )

    @property
    def ready(self) -> bool:
        return (
            self.module_enabled
            and self.source_boundary_ready
            and self.causal_boundary_ready
            and not self.unresolved_work_ids
            and len(self.owner_results) == len(REQUIRED_RUNTIME_DOMAINS)
            and all(item.ready for item in self.owner_results)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": OWNER_RECEIPT_SCHEMA,
            "revision": self.revision,
            "generated_at_ns": self.generated_at_ns,
            "owners": [item.to_dict() for item in self.owner_results],
            "unresolved_work_ids": list(self.unresolved_work_ids),
            "source_boundary_ready": self.source_boundary_ready,
            "causal_boundary_ready": self.causal_boundary_ready,
            "module_enabled": self.module_enabled,
            "ready": self.ready,
            "digest": self.digest,
        }


def canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ProductizationContractError(
                "non_finite_number",
                "canonical payload cannot contain NaN or infinity",
            )
        return value
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, Mapping):
        return {
            str(key): canonicalize(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [canonicalize(item) for item in value]
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return canonicalize(to_dict())
    raise ProductizationContractError(
        "non_canonical_value",
        f"unsupported canonical payload value: {type(value).__name__}",
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonicalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def digest_payload(value: Any) -> str:
    encoded = canonical_json(value).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def stable_id(prefix: str, *parts: Any) -> str:
    token = require_token(prefix, "id_prefix")
    payload = digest_payload([token, *parts]).removeprefix("sha256:")
    return f"{token}_{payload[:24]}"


def require_token(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not TOKEN_PATTERN.fullmatch(normalized):
        raise ProductizationContractError(
            "token_invalid",
            f"{name} is not a valid bounded token",
            details={"name": name, "value": normalized[:256]},
        )
    return normalized


def require_digest(value: str, name: str) -> str:
    normalized = str(value).strip()
    if not DIGEST_PATTERN.fullmatch(normalized):
        raise ProductizationContractError(
            "digest_invalid",
            f"{name} is not a sha256 digest",
        )
    return normalized


def require_git_revision(value: str) -> str:
    normalized = str(value).strip().casefold()
    if not re.fullmatch(r"[0-9a-f]{40}", normalized):
        raise ProductizationContractError(
            "git_revision_invalid",
            "revision must be a full 40-character Git object id",
        )
    return normalized


def normalize_repo_path(value: str) -> str:
    raw = str(value).strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if (
        not raw
        or path.is_absolute()
        or raw.startswith(("../", "./", "/"))
        or ".." in path.parts
        or ":" in path.parts[0]
    ):
        raise ProductizationContractError(
            "repository_path_invalid",
            f"path must be relative to the Zyra repository: {value}",
        )
    return path.as_posix()


def unique_tuple(values: Iterable[str], *, field_name: str) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = require_token(value, field_name)
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return tuple(result)


def mapping_strings(value: Mapping[str, Any], names: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        candidate = value.get(name)
        if candidate is None:
            continue
        normalized = str(candidate).strip()
        if normalized:
            result[name] = normalized
    return result
