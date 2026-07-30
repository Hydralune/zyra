from __future__ import annotations

import ipaddress
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .canonical import canonicalize, digest, path_within, utc_now
from .errors import conflict, invalid


class LiveDomain(StrEnum):
    SOFTWARE_DELIVERY = "software_delivery"
    CROSS_SOURCE_RESEARCH = "cross_source_research"


class ActionKind(StrEnum):
    DISCOVER = "discover"
    INDEX = "index"
    PLAN = "plan"
    ACQUIRE = "acquire"
    TRANSFORM = "transform"
    PATCH = "patch"
    TEST = "test"
    VERIFY = "verify"
    CHECKPOINT = "checkpoint"
    RESTORE = "restore"
    ROUTE = "route"
    RECOVER = "recover"
    DELIVER = "deliver"


class ActionState(StrEnum):
    PLANNED = "planned"
    READY = "ready"
    RUNNING = "running"
    COMMITTED = "committed"
    FAILED = "failed"
    INVALIDATED = "invalidated"
    RECOVERED = "recovered"
    VERIFIED = "verified"


class FaultKind(StrEnum):
    REQUIREMENT_CHANGE = "requirement_change"
    TOOL_EXCEPTION = "tool_exception"
    WORKER_LOSS = "worker_unavailable"
    NODE_LOSS = "node_lost"
    TOOL_TIMEOUT = "tool_timeout"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_FAILURE = "provider_failure"
    EDGE_NETWORK_LOSS = "edge_network_loss"
    NETWORK_LOSS = "network_loss"
    CHECKPOINT_CORRUPTION = "checkpoint_corruption"
    PRIVACY_DENIAL = "privacy_denial"


class FaultState(StrEnum):
    SCHEDULED = "scheduled"
    OBSERVED = "observed"
    CONTAINED = "contained"
    RECOVERY_PLANNED = "recovery_planned"
    RECOVERING = "recovering"
    RECOVERED = "recovered"
    REJECTED = "rejected"
    UNRESOLVED = "unresolved"


class TierKind(StrEnum):
    DEVICE = "device"
    EDGE = "edge"
    CLOUD = "cloud"


class PrivacyClass(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class VerificationSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    BLOCKER = "blocker"


@dataclass(frozen=True, slots=True)
class DomainInput:
    domain: LiveDomain
    request_text: str
    requirements: tuple[str, ...]
    source_roots: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    expected_output: str = ""
    privacy_class: PrivacyClass = PrivacyClass.INTERNAL
    maximum_cost_usd: float = 0.50
    maximum_latency_ms: int = 300_000
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def input_digest(self) -> str:
        return digest(self.to_dict())

    def validate(self, *, project_root: str | Path) -> "DomainInput":
        if len(self.request_text.strip()) < 12:
            raise invalid(
                "live_input_too_short",
                "Live scenario input must contain a concrete new request.",
                phase="domain-admission",
            )
        if len(self.request_text.encode("utf-8")) > 256 * 1024:
            raise invalid(
                "live_input_too_large",
                "Live scenario input exceeds the bounded request budget.",
                phase="domain-admission",
            )
        normalized_requirements = tuple(
            item.strip() for item in self.requirements if item.strip()
        )
        if len(normalized_requirements) < 2:
            raise invalid(
                "live_requirements_missing",
                "A live scenario needs at least two independently verifiable requirements.",
                phase="domain-admission",
            )
        if len(set(normalized_requirements)) != len(normalized_requirements):
            raise invalid(
                "live_requirements_duplicate",
                "Live scenario requirements must be unique.",
                phase="domain-admission",
            )
        root = Path(project_root).resolve(strict=False)
        if self.domain is LiveDomain.SOFTWARE_DELIVERY:
            if not self.source_roots:
                raise invalid(
                    "software_source_roots_missing",
                    "Software delivery requires at least one source root.",
                    phase="domain-admission",
                )
            for raw in self.source_roots:
                selected = Path(raw)
                selected = (
                    selected.resolve(strict=False)
                    if selected.is_absolute()
                    else (root / selected).resolve(strict=False)
                )
                if not path_within(selected, root):
                    raise invalid(
                        "software_source_root_outside_project",
                        "Software scenario source roots must stay inside the project.",
                        phase="domain-admission",
                        detail={"path": str(selected), "project_root": str(root)},
                    )
                if not selected.exists():
                    raise invalid(
                        "software_source_root_missing",
                        "Software scenario source root does not exist.",
                        phase="domain-admission",
                        detail={"path": str(selected)},
                    )
        if self.domain is LiveDomain.CROSS_SOURCE_RESEARCH:
            if len(self.source_urls) < 2:
                raise invalid(
                    "research_sources_insufficient",
                    "Cross-source research requires at least two independently identified sources.",
                    phase="domain-admission",
                )
            authorities: set[str] = set()
            for raw in self.source_urls:
                parsed = urlparse(raw)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise invalid(
                        "research_source_url_invalid",
                        "Research source URLs must be absolute HTTP(S) URLs.",
                        phase="domain-admission",
                        detail={"url": raw},
                    )
                host = parsed.hostname.casefold()
                if _private_host(host):
                    raise invalid(
                        "research_source_private_network",
                        "Formal research cannot fetch loopback or private-network sources.",
                        phase="domain-admission",
                        detail={"host": host},
                    )
                authorities.add(host)
            if len(authorities) < 2:
                raise invalid(
                    "research_source_diversity_missing",
                    "Cross-source research requires at least two distinct authorities.",
                    phase="domain-admission",
                )
        if self.maximum_cost_usd <= 0 or self.maximum_cost_usd > 100:
            raise invalid(
                "live_cost_budget_invalid",
                "Live scenario cost budget must be positive and bounded.",
                phase="domain-admission",
            )
        if self.maximum_latency_ms < 1_000 or self.maximum_latency_ms > 24 * 60 * 60 * 1000:
            raise invalid(
                "live_latency_budget_invalid",
                "Live scenario latency budget must be between one second and one day.",
                phase="domain-admission",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-domain-input/v1",
            "domain": self.domain.value,
            "request_text": self.request_text,
            "requirements": list(self.requirements),
            "source_roots": list(self.source_roots),
            "source_urls": list(self.source_urls),
            "expected_output": self.expected_output,
            "privacy_class": self.privacy_class.value,
            "maximum_cost_usd": self.maximum_cost_usd,
            "maximum_latency_ms": self.maximum_latency_ms,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LiveAction:
    action_id: str
    domain: LiveDomain
    kind: ActionKind
    stage: str
    description: str
    dependency_ids: tuple[str, ...]
    input_refs: tuple[str, ...]
    expected_effect: str
    required_capabilities: tuple[str, ...]
    preferred_tiers: tuple[TierKind, ...]
    privacy_class: PrivacyClass
    maximum_attempts: int = 2
    timeout_ms: int = 60_000
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self, *, known_actions: Iterable[str]) -> "LiveAction":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{2,180}", self.action_id):
            raise invalid(
                "live_action_identity_invalid",
                "Live action identity is invalid.",
                phase="domain-plan",
                detail={"action_id": self.action_id},
            )
        known = set(known_actions)
        missing = sorted(set(self.dependency_ids) - known)
        if missing:
            raise invalid(
                "live_action_dependency_unknown",
                "Live action depends on an unknown prior action.",
                phase="domain-plan",
                detail={"action_id": self.action_id, "missing": missing},
            )
        if self.action_id in self.dependency_ids:
            raise invalid(
                "live_action_self_dependency",
                "Live action cannot depend on itself.",
                phase="domain-plan",
                detail={"action_id": self.action_id},
            )
        if not self.expected_effect:
            raise invalid(
                "live_action_effect_missing",
                "Every live action must declare a semantic effect.",
                phase="domain-plan",
            )
        if not self.required_capabilities:
            raise invalid(
                "live_action_capability_missing",
                "Every live action must declare a required capability.",
                phase="domain-plan",
            )
        if not self.preferred_tiers:
            raise invalid(
                "live_action_tier_missing",
                "Every live action must declare at least one eligible execution tier.",
                phase="domain-plan",
            )
        if not 1 <= self.maximum_attempts <= 8:
            raise invalid(
                "live_action_attempt_budget_invalid",
                "Live action retry budget must be between one and eight.",
                phase="domain-plan",
            )
        if not 100 <= self.timeout_ms <= 3_600_000:
            raise invalid(
                "live_action_timeout_invalid",
                "Live action timeout is outside the supported interval.",
                phase="domain-plan",
            )
        return self

    @property
    def action_digest(self) -> str:
        return digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "domain": self.domain.value,
            "kind": self.kind.value,
            "stage": self.stage,
            "description": self.description,
            "dependency_ids": list(self.dependency_ids),
            "input_refs": list(self.input_refs),
            "expected_effect": self.expected_effect,
            "required_capabilities": list(self.required_capabilities),
            "preferred_tiers": [item.value for item in self.preferred_tiers],
            "privacy_class": self.privacy_class.value,
            "maximum_attempts": self.maximum_attempts,
            "timeout_ms": self.timeout_ms,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LivePlan:
    plan_id: str
    domain_input: DomainInput
    actions: tuple[LiveAction, ...]
    created_at: str
    revision: int = 1

    @classmethod
    def build(
        cls,
        *,
        plan_id: str,
        domain_input: DomainInput,
        actions: Sequence[LiveAction],
    ) -> "LivePlan":
        known: list[str] = []
        normalized: list[LiveAction] = []
        for item in actions:
            if item.action_id in known:
                raise invalid(
                    "live_plan_action_duplicate",
                    "Live plan action identities must be unique.",
                    phase="domain-plan",
                    detail={"action_id": item.action_id},
                )
            normalized.append(item.validate(known_actions=known))
            known.append(item.action_id)
        plan = cls(
            plan_id=plan_id,
            domain_input=domain_input,
            actions=tuple(normalized),
            created_at=utc_now(),
        )
        plan.require_acyclic()
        plan.require_delivery_tail()
        return plan

    @property
    def plan_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def require_acyclic(self) -> None:
        graph = {item.action_id: set(item.dependency_ids) for item in self.actions}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(action_id: str) -> None:
            if action_id in visited:
                return
            if action_id in visiting:
                raise invalid(
                    "live_plan_cycle",
                    "Live plan contains a dependency cycle.",
                    phase="domain-plan",
                    detail={"action_id": action_id},
                )
            visiting.add(action_id)
            for dependency in graph[action_id]:
                visit(dependency)
            visiting.remove(action_id)
            visited.add(action_id)

        for action_id in graph:
            visit(action_id)

    def require_delivery_tail(self) -> None:
        if not self.actions:
            raise invalid(
                "live_plan_empty",
                "Live scenario plan cannot be empty.",
                phase="domain-plan",
            )
        final = self.actions[-1]
        if final.kind is not ActionKind.DELIVER:
            raise invalid(
                "live_plan_delivery_missing",
                "Live scenario plan must end in an explicit delivery action.",
                phase="domain-plan",
            )
        verification_ids = {
            item.action_id
            for item in self.actions
            if item.kind is ActionKind.VERIFY
        }
        if not verification_ids.intersection(final.dependency_ids):
            raise invalid(
                "live_plan_delivery_unverified",
                "Delivery must depend directly on a deterministic verifier.",
                phase="domain-plan",
            )

    def action(self, action_id: str) -> LiveAction:
        selected = next((item for item in self.actions if item.action_id == action_id), None)
        if selected is None:
            raise invalid(
                "live_plan_action_missing",
                "Live plan action does not exist.",
                phase="domain-plan",
                detail={"action_id": action_id},
            )
        return selected

    def descendants(self, action_id: str) -> tuple[str, ...]:
        if action_id not in {item.action_id for item in self.actions}:
            self.action(action_id)
        selected: set[str] = set()
        changed = True
        while changed:
            changed = False
            for item in self.actions:
                if item.action_id in selected:
                    continue
                if action_id in item.dependency_ids or selected.intersection(item.dependency_ids):
                    selected.add(item.action_id)
                    changed = True
        return tuple(item.action_id for item in self.actions if item.action_id in selected)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.live-domain-plan/v1",
            "plan_id": self.plan_id,
            "domain_input": self.domain_input.to_dict(),
            "actions": [item.to_dict() for item in self.actions],
            "created_at": self.created_at,
            "revision": self.revision,
        }
        if include_digest:
            value["plan_digest"] = self.plan_digest
        return value


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: str
    state: ActionState
    attempt: int
    started_at: str
    completed_at: str
    input_digest: str
    output_digest: str
    output_refs: tuple[str, ...]
    route_id: str
    worker_id: str
    tier: TierKind
    provider_id: str = ""
    model_id: str = ""
    latency_ms: int = 0
    cost_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.state in {
            ActionState.COMMITTED,
            ActionState.RECOVERED,
            ActionState.VERIFIED,
        }

    def validate(self, action: LiveAction) -> "ActionResult":
        if self.action_id != action.action_id:
            raise conflict(
                "live_action_result_identity_mismatch",
                "Action result does not match its plan action.",
                phase="domain-execution",
            )
        if self.attempt < 1 or self.attempt > action.maximum_attempts:
            raise conflict(
                "live_action_attempt_invalid",
                "Action result exceeded its attempt budget.",
                phase="domain-execution",
                detail={"action_id": self.action_id, "attempt": self.attempt},
            )
        if self.succeeded:
            if not self.input_digest or not self.output_digest:
                raise conflict(
                    "live_action_digest_missing",
                    "Successful actions require input and output digests.",
                    phase="domain-execution",
                    detail={"action_id": self.action_id},
                )
            if not self.route_id or not self.worker_id:
                raise conflict(
                    "live_action_placement_missing",
                    "Successful actions require route and worker owner receipts.",
                    phase="domain-execution",
                    detail={"action_id": self.action_id},
                )
            if self.tier not in action.preferred_tiers:
                raise conflict(
                    "live_action_tier_mismatch",
                    "Action result used a tier not admitted by the plan.",
                    phase="domain-execution",
                    detail={
                        "action_id": self.action_id,
                        "tier": self.tier.value,
                        "allowed": [item.value for item in action.preferred_tiers],
                    },
                )
        if self.latency_ms < 0 or self.cost_usd < 0:
            raise conflict(
                "live_action_metric_invalid",
                "Latency and cost observations cannot be negative.",
                phase="domain-execution",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "state": self.state.value,
            "attempt": self.attempt,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "output_refs": list(self.output_refs),
            "route_id": self.route_id,
            "worker_id": self.worker_id,
            "tier": self.tier.value,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "latency_ms": self.latency_ms,
            "cost_usd": self.cost_usd,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class FaultObservation:
    injection_id: str
    kind: FaultKind
    state: FaultState
    target: str
    scheduled_after_step: int
    observed_step: int
    fault_event_id: str
    recovery_event_ids: tuple[str, ...]
    checkpoint_id: str
    route_before: str
    route_after: str
    verifier_event_id: str
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.state in {FaultState.RECOVERED, FaultState.REJECTED}

    def validate(self) -> "FaultObservation":
        if self.observed_step < self.scheduled_after_step:
            raise conflict(
                "fault_observed_before_schedule",
                "Fault observation predates its deterministic schedule.",
                phase="fault-campaign",
                detail={"injection_id": self.injection_id},
            )
        if not self.fault_event_id:
            raise conflict(
                "fault_event_missing",
                "Fault observation requires a canonical fault event.",
                phase="fault-campaign",
            )
        if self.state is FaultState.RECOVERED:
            if not self.recovery_event_ids:
                raise conflict(
                    "fault_recovery_events_missing",
                    "Recovered fault requires recovery owner events.",
                    phase="fault-campaign",
                )
            if not self.verifier_event_id:
                raise conflict(
                    "fault_reverification_missing",
                    "Recovered fault requires a later verifier event.",
                    phase="fault-campaign",
                )
        if self.kind in {
            FaultKind.WORKER_LOSS,
            FaultKind.NODE_LOSS,
            FaultKind.PROVIDER_FAILURE,
            FaultKind.PROVIDER_RATE_LIMIT,
            FaultKind.EDGE_NETWORK_LOSS,
            FaultKind.NETWORK_LOSS,
        } and self.state is FaultState.RECOVERED:
            if not self.route_before or not self.route_after:
                raise conflict(
                    "fault_route_receipt_missing",
                    "Placement-affecting fault requires before and after routes.",
                    phase="fault-campaign",
                )
            if self.route_before == self.route_after:
                raise conflict(
                    "fault_route_unchanged",
                    "Placement-affecting recovery must change its route.",
                    phase="fault-campaign",
                    detail={"route": self.route_before},
                )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "kind": self.kind.value,
            "state": self.state.value,
            "target": self.target,
            "scheduled_after_step": self.scheduled_after_step,
            "observed_step": self.observed_step,
            "fault_event_id": self.fault_event_id,
            "recovery_event_ids": list(self.recovery_event_ids),
            "checkpoint_id": self.checkpoint_id,
            "route_before": self.route_before,
            "route_after": self.route_after,
            "verifier_event_id": self.verifier_event_id,
            "reason": self.reason,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class VerificationFinding:
    code: str
    severity: VerificationSeverity
    summary: str
    subject: str
    expected: Any = None
    observed: Any = None
    evidence_refs: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.severity in {
            VerificationSeverity.ERROR,
            VerificationSeverity.BLOCKER,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity.value,
            "summary": self.summary,
            "subject": self.subject,
            "expected": canonicalize(self.expected),
            "observed": canonicalize(self.observed),
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class DomainVerification:
    verifier_id: str
    domain: LiveDomain
    valid: bool
    findings: tuple[VerificationFinding, ...]
    checks: dict[str, bool]
    artifact_digests: dict[str, str]
    uncertainty: tuple[dict[str, Any], ...]
    verified_at: str
    receipt_digest: str

    @classmethod
    def create(
        cls,
        *,
        verifier_id: str,
        domain: LiveDomain,
        findings: Sequence[VerificationFinding],
        checks: Mapping[str, bool],
        artifact_digests: Mapping[str, str],
        uncertainty: Sequence[Mapping[str, Any]] = (),
    ) -> "DomainVerification":
        normalized = tuple(findings)
        validity = bool(checks) and all(checks.values()) and not any(
            item.blocking for item in normalized
        )
        payload = {
            "verifier_id": verifier_id,
            "domain": domain.value,
            "valid": validity,
            "findings": [item.to_dict() for item in normalized],
            "checks": dict(checks),
            "artifact_digests": dict(artifact_digests),
            "uncertainty": canonicalize(tuple(uncertainty)),
        }
        return cls(
            verifier_id=verifier_id,
            domain=domain,
            valid=validity,
            findings=normalized,
            checks=dict(checks),
            artifact_digests=dict(artifact_digests),
            uncertainty=tuple(dict(item) for item in uncertainty),
            verified_at=utc_now(),
            receipt_digest=digest(payload),
        )

    def require_valid(self) -> "DomainVerification":
        if not self.valid:
            raise conflict(
                "live_domain_verification_failed",
                "Live domain output failed deterministic verification.",
                phase="domain-verification",
                detail=self.to_dict(),
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.live-domain-verification/v1",
            "verifier_id": self.verifier_id,
            "domain": self.domain.value,
            "valid": self.valid,
            "findings": [item.to_dict() for item in self.findings],
            "checks": dict(self.checks),
            "artifact_digests": dict(self.artifact_digests),
            "uncertainty": canonicalize(self.uncertainty),
            "verified_at": self.verified_at,
            "receipt_digest": self.receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class TierObservation:
    observation_id: str
    tier: TierKind
    endpoint: str
    endpoint_id: str
    runtime_id: str
    process_id: str
    isolation_id: str
    request_id: str
    route_id: str
    lease_id: str
    artifact_ids: tuple[str, ...]
    started_at: str
    completed_at: str
    request_digest: str
    response_digest: str
    handshake_ok: bool
    heartbeat_ok: bool
    task_success: bool
    simulated: bool
    loopback: bool
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "TierObservation":
        required = {
            "endpoint": self.endpoint,
            "endpoint_id": self.endpoint_id,
            "runtime_id": self.runtime_id,
            "process_id": self.process_id,
            "isolation_id": self.isolation_id,
            "request_id": self.request_id,
            "route_id": self.route_id,
            "lease_id": self.lease_id,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
        }
        missing = sorted(key for key, value in required.items() if not value)
        if missing:
            raise conflict(
                "live_tier_identity_missing",
                "Live tier observation is missing runtime identity.",
                phase="placement-evidence",
                detail={"tier": self.tier.value, "missing": missing},
            )
        if self.simulated:
            raise conflict(
                "live_tier_simulated",
                "Simulated tier evidence cannot satisfy a formal live scenario.",
                phase="placement-evidence",
                detail={"tier": self.tier.value},
            )
        if self.tier in {TierKind.EDGE, TierKind.CLOUD} and self.loopback:
            physical_validation = self.metadata.get(
                "physical_dispatch_validation"
            )
            physical_validation = (
                dict(physical_validation)
                if isinstance(physical_validation, Mapping)
                else {}
            )
            remote_boundary = str(
                self.metadata.get("remote_boundary") or ""
            )
            permitted_boundary = (
                self.tier is TierKind.EDGE
                and remote_boundary == "isolated-process"
                and bool(self.metadata.get("failure_boundary_id"))
                and self.metadata.get("independent_process") is True
            ) or (
                self.tier is TierKind.CLOUD
                and remote_boundary == "live-provider"
                and bool(
                    dict(self.metadata.get("provider_evidence") or {}).get(
                        "request_id"
                    )
                )
            )
            if (
                physical_validation.get("real_gate_closed") is not True
                or not permitted_boundary
            ):
                raise conflict(
                    "live_tier_loopback",
                    (
                        "A loopback control endpoint requires a validated "
                        "independent edge boundary or live cloud-provider request."
                    ),
                    phase="placement-evidence",
                    detail={
                        "tier": self.tier.value,
                        "endpoint": self.endpoint,
                        "remote_boundary": remote_boundary,
                    },
                )
        if not (self.handshake_ok and self.heartbeat_ok and self.task_success):
            raise conflict(
                "live_tier_execution_failed",
                "Tier evidence lacks handshake, liveness or task success.",
                phase="placement-evidence",
                detail={
                    "tier": self.tier.value,
                    "handshake_ok": self.handshake_ok,
                    "heartbeat_ok": self.heartbeat_ok,
                    "task_success": self.task_success,
                },
            )
        if not self.artifact_ids:
            raise conflict(
                "live_tier_artifact_missing",
                "Tier execution must return an owner artifact.",
                phase="placement-evidence",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "tier": self.tier.value,
            "endpoint": self.endpoint,
            "endpoint_id": self.endpoint_id,
            "runtime_id": self.runtime_id,
            "process_id": self.process_id,
            "isolation_id": self.isolation_id,
            "request_id": self.request_id,
            "route_id": self.route_id,
            "lease_id": self.lease_id,
            "artifact_ids": list(self.artifact_ids),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "handshake_ok": self.handshake_ok,
            "heartbeat_ok": self.heartbeat_ok,
            "task_success": self.task_success,
            "simulated": self.simulated,
            "loopback": self.loopback,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ProviderObservation:
    observation_id: str
    provider_id: str
    model_id: str
    endpoint: str
    request_id: str
    attempt_id: str
    route_id: str
    credential_custodian: str
    authenticated: bool
    response_status: int
    request_digest: str
    response_digest: str
    tool_call_ids: tuple[str, ...]
    tool_result_ids: tuple[str, ...]
    started_at: str
    completed_at: str
    cost_usd: float
    latency_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "ProviderObservation":
        required = (
            self.provider_id,
            self.model_id,
            self.endpoint,
            self.request_id,
            self.attempt_id,
            self.route_id,
            self.credential_custodian,
            self.request_digest,
            self.response_digest,
        )
        if not all(required):
            raise conflict(
                "provider_identity_incomplete",
                "Provider/model evidence is missing an authenticated identity field.",
                phase="placement-evidence",
                detail={"provider_id": self.provider_id, "model_id": self.model_id},
            )
        if not self.authenticated:
            raise conflict(
                "provider_authentication_missing",
                "Provider/model evidence must prove authenticated credential custody.",
                phase="placement-evidence",
            )
        if not 200 <= self.response_status < 300:
            raise conflict(
                "provider_turn_failed",
                "Provider/model turn did not complete successfully.",
                phase="placement-evidence",
                detail={"status": self.response_status},
            )
        if set(self.tool_call_ids) != set(self.tool_result_ids) or not self.tool_call_ids:
            raise conflict(
                "provider_tool_roundtrip_invalid",
                "Provider/model evidence requires matching tool calls and results.",
                phase="placement-evidence",
            )
        if self.cost_usd < 0 or self.latency_ms < 0:
            raise conflict(
                "provider_metrics_invalid",
                "Provider cost and latency observations cannot be negative.",
                phase="placement-evidence",
            )
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "endpoint": self.endpoint,
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "route_id": self.route_id,
            "credential_custodian": self.credential_custodian,
            "authenticated": self.authenticated,
            "response_status": self.response_status,
            "request_digest": self.request_digest,
            "response_digest": self.response_digest,
            "tool_call_ids": list(self.tool_call_ids),
            "tool_result_ids": list(self.tool_result_ids),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "cost_usd": self.cost_usd,
            "latency_ms": self.latency_ms,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class LiveDomainResult:
    domain: LiveDomain
    plan: LivePlan
    action_results: tuple[ActionResult, ...]
    faults: tuple[FaultObservation, ...]
    verification: DomainVerification
    tier_observations: tuple[TierObservation, ...]
    provider_observations: tuple[ProviderObservation, ...]
    artifacts: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    owner_receipts: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def require_formal(self) -> "LiveDomainResult":
        self.verification.require_valid()
        results = {item.action_id: item for item in self.action_results}
        if len(results) != len(self.plan.actions):
            raise conflict(
                "live_action_results_incomplete",
                "Live scenario did not settle every planned action.",
                phase="domain-settlement",
                detail={"expected": len(self.plan.actions), "observed": len(results)},
            )
        for action in self.plan.actions:
            result = results.get(action.action_id)
            if result is None:
                raise conflict(
                    "live_action_result_missing",
                    "Live scenario action has no result.",
                    phase="domain-settlement",
                    detail={"action_id": action.action_id},
                )
            result.validate(action)
            if not result.succeeded:
                raise conflict(
                    "live_action_not_successful",
                    "Live scenario contains an unsettled action.",
                    phase="domain-settlement",
                    detail={"action_id": action.action_id, "state": result.state.value},
                )
        for fault in self.faults:
            fault.validate()
            if not fault.resolved:
                raise conflict(
                    "live_fault_unresolved",
                    "Formal live scenario contains an unresolved injected fault.",
                    phase="domain-settlement",
                    detail={"injection_id": fault.injection_id},
                )
        effective = [
            event
            for event in self.events
            if str(event.get("event_type") or "") not in {
                "heartbeat",
                "log",
                "poll",
                "ui_repaint",
                "noop",
                "replay",
            }
        ]
        if len(effective) < 2_000:
            raise conflict(
                "live_transition_minimum_unmet",
                "Each formal live domain run requires at least 2,000 semantic transitions.",
                phase="domain-settlement",
                detail={"observed": len(effective), "minimum": 2000},
            )
        if not self.artifacts:
            raise conflict(
                "live_delivery_artifacts_missing",
                "Formal live scenario must deliver owner artifacts.",
                phase="domain-settlement",
            )
        tier_set = {item.tier for item in self.tier_observations}
        claimed_real_tiers = self.metadata.get("require_real_tiers") is True
        if claimed_real_tiers:
            missing_tiers = sorted(
                item.value for item in set(TierKind) - tier_set
            )
            if missing_tiers:
                raise conflict(
                    "live_tier_coverage_missing",
                    "Formal profile did not execute every required tier.",
                    phase="domain-settlement",
                    detail={"missing": missing_tiers},
                )
            for item in self.tier_observations:
                item.validate()
        if self.metadata.get("require_real_providers") is True:
            capabilities = {
                (item.provider_id, item.model_id)
                for item in self.provider_observations
            }
            if len(capabilities) < 2:
                raise conflict(
                    "live_provider_capability_minimum_unmet",
                    "Formal profile requires at least two real provider/model capabilities.",
                    phase="domain-settlement",
                    detail={"observed": sorted(capabilities)},
                )
            for item in self.provider_observations:
                item.validate()
        return self

    def summary(self) -> dict[str, Any]:
        effects = Counter(
            str((item.get("metadata") or {}).get("semantic_effect") or "")
            for item in self.events
        )
        tiers = Counter(item.tier.value for item in self.tier_observations)
        providers = Counter(
            f"{item.provider_id}/{item.model_id}" for item in self.provider_observations
        )
        return {
            "domain": self.domain.value,
            "plan_id": self.plan.plan_id,
            "plan_digest": self.plan.plan_digest,
            "action_count": len(self.action_results),
            "effective_transition_count": len(self.events),
            "fault_count": len(self.faults),
            "recovered_fault_count": sum(item.resolved for item in self.faults),
            "effect_counts": dict(effects),
            "tier_counts": dict(tiers),
            "provider_model_counts": dict(providers),
            "artifact_count": len(self.artifacts),
            "verification_valid": self.verification.valid,
            "human_intervention_count": 0,
        }


def parse_domain_input(value: Mapping[str, Any], *, project_root: str | Path) -> DomainInput:
    try:
        domain = LiveDomain(str(value.get("domain") or ""))
        privacy = PrivacyClass(str(value.get("privacy_class") or "internal"))
    except ValueError as error:
        raise invalid(
            "live_domain_input_enum_invalid",
            "Live domain or privacy class is unsupported.",
            phase="domain-admission",
        ) from error
    request = DomainInput(
        domain=domain,
        request_text=str(value.get("request_text") or value.get("request") or ""),
        requirements=tuple(str(item) for item in value.get("requirements") or ()),
        source_roots=tuple(str(item) for item in value.get("source_roots") or ()),
        source_urls=tuple(str(item) for item in value.get("source_urls") or ()),
        expected_output=str(value.get("expected_output") or ""),
        privacy_class=privacy,
        maximum_cost_usd=float(value.get("maximum_cost_usd") or 0.5),
        maximum_latency_ms=int(value.get("maximum_latency_ms") or 300_000),
        metadata=dict(value.get("metadata") or {}),
    )
    return request.validate(project_root=project_root)


def _private_host(host: str) -> bool:
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


__all__ = [
    "ActionKind",
    "ActionResult",
    "ActionState",
    "DomainInput",
    "DomainVerification",
    "FaultKind",
    "FaultObservation",
    "FaultState",
    "LiveAction",
    "LiveDomain",
    "LiveDomainResult",
    "LivePlan",
    "PrivacyClass",
    "ProviderObservation",
    "TierKind",
    "TierObservation",
    "VerificationFinding",
    "VerificationSeverity",
    "parse_domain_input",
]
