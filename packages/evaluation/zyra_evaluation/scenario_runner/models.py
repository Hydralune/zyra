from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any

from .canonical import canonicalize, digest, new_identity, utc_now


def _slot_dict(value: Any) -> dict[str, Any]:
    return {item.name: getattr(value, item.name) for item in fields(value)}


class ScenarioMode(StrEnum):
    SEALED = "sealed"
    INTERACTIVE = "interactive"
    REVIEW_REPLAY = "review_replay"


class ScenarioPhase(StrEnum):
    CREATED = "created"
    ADMITTED = "admitted"
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    ARCHIVED = "archived"


TERMINAL_PHASES = {
    ScenarioPhase.CANCELLED,
    ScenarioPhase.SUCCEEDED,
    ScenarioPhase.FAILED,
    ScenarioPhase.ARCHIVED,
}


class PreflightKind(StrEnum):
    DATABASE = "database"
    CACHE = "cache"
    INDEX = "index"
    ARTIFACT = "artifact"
    BUILD = "build"


class PreflightPolicy(StrEnum):
    ABSENT_OR_EMPTY = "absent_or_empty"
    EMPTY_DIRECTORY = "empty_directory"
    ABSENT_FILE = "absent_file"
    SQLITE_NO_USER_ROWS = "sqlite_no_user_rows"


class StepEffect(StrEnum):
    STATE_MUTATION = "state_mutation"
    ROUTE = "route"
    PLACEMENT = "placement"
    TOOL = "tool"
    VERIFICATION = "verification"
    PERMISSION = "permission"
    COMPACT_RESTORE = "compact_restore"
    FAULT = "fault"
    RECOVERY = "recovery"
    ARTIFACT = "artifact"
    DELIVERY = "delivery"
    TOPOLOGY = "topology"
    MEMORY = "memory"
    NONE = "none"


class StepDisposition(StrEnum):
    ADMITTED = "admitted"
    EXCLUDED = "excluded"
    INVALID = "invalid"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class PreflightTarget:
    kind: PreflightKind
    path: str
    policy: PreflightPolicy
    ignored_names: tuple[str, ...] = ()
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "path": self.path,
            "policy": self.policy.value,
            "ignored_names": list(self.ignored_names),
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class FaultInjection:
    injection_id: str
    stage: str
    kind: str
    after_effective_step: int
    target: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "injection_id": self.injection_id,
            "stage": self.stage,
            "kind": self.kind,
            "after_effective_step": self.after_effective_step,
            "target": self.target,
            "payload": canonicalize(self.payload),
        }


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    profile_id: str
    provider_id: str
    model_id: str
    backend_id: str
    worker_classes: tuple[str, ...]
    maximum_effective_steps: int
    maximum_wall_time_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "backend_id": self.backend_id,
            "worker_classes": list(self.worker_classes),
            "maximum_effective_steps": self.maximum_effective_steps,
            "maximum_wall_time_ms": self.maximum_wall_time_ms,
            "metadata": canonicalize(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ScenarioDefinition:
    scenario_id: str
    version: str
    title: str
    domain: str
    goal_template: str
    required_owner_stages: tuple[str, ...]
    planned_actions: tuple[dict[str, Any], ...]
    default_faults: tuple[FaultInjection, ...]
    expected_effects: tuple[StepEffect, ...]
    minimum_effective_steps: int
    source_roles: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def definition_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.scenario-definition/v1",
            "scenario_id": self.scenario_id,
            "version": self.version,
            "title": self.title,
            "domain": self.domain,
            "goal_template": self.goal_template,
            "required_owner_stages": list(self.required_owner_stages),
            "planned_actions": canonicalize(self.planned_actions),
            "default_faults": [item.to_dict() for item in self.default_faults],
            "expected_effects": [item.value for item in self.expected_effects],
            "minimum_effective_steps": self.minimum_effective_steps,
            "source_roles": list(self.source_roles),
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["definition_digest"] = self.definition_digest
        return value


@dataclass(frozen=True, slots=True)
class SealedPolicy:
    policy_id: str
    version: str
    allow_actions: tuple[str, ...]
    deny_actions: tuple[str, ...]
    high_risk_actions: tuple[str, ...]
    ask_disposition: str
    unknown_disposition: str
    maximum_denials: int
    manual_mutation_disposition: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def policy_digest(self) -> str:
        return digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.sealed-scenario-policy/v1",
            "policy_id": self.policy_id,
            "version": self.version,
            "allow_actions": list(self.allow_actions),
            "deny_actions": list(self.deny_actions),
            "high_risk_actions": list(self.high_risk_actions),
            "ask_disposition": self.ask_disposition,
            "unknown_disposition": self.unknown_disposition,
            "maximum_denials": self.maximum_denials,
            "manual_mutation_disposition": self.manual_mutation_disposition,
            "metadata": canonicalize(self.metadata),
        }
        if include_digest:
            value["policy_digest"] = self.policy_digest
        return value


@dataclass(frozen=True, slots=True)
class ScenarioConfiguration:
    scenario_id: str
    definition_version: str
    definition_digest: str
    mode: ScenarioMode
    input_text: str
    input_digest: str
    seed: int
    profile: ExecutionProfile
    faults: tuple[FaultInjection, ...]
    preflight_targets: tuple[PreflightTarget, ...]
    policy: SealedPolicy
    expected_policy_digest: str
    requested_by: str
    labels: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def configuration_digest(self) -> str:
        return digest(self.to_dict(include_digest=False, include_input=False))

    def summary_dict(self) -> dict[str, Any]:
        """The configuration subset a run list actually renders.

        The workbench list shows the scenario id; mode and seed identify a run
        at a glance, and the digests let a client detect a definition change.
        The profile, fault schedule, preflight targets and policy block are
        detail-view data -- carrying them on every row is what made a 34-run
        page 860KB.

        ``input_digest`` is kept because the client's configuration validator
        requires it; dropping it made every list response fail validation and
        the console fall back to a reconnecting state.
        """

        return {
            "schema": "zyra.scenario-configuration/v1",
            "scenario_id": self.scenario_id,
            "definition_version": self.definition_version,
            "definition_digest": self.definition_digest,
            "mode": self.mode.value,
            "input_digest": self.input_digest,
            "seed": self.seed,
            "labels": dict(self.labels),
        }

    def to_dict(
        self,
        *,
        include_digest: bool = True,
        include_input: bool = False,
    ) -> dict[str, Any]:
        value = {
            "schema": "zyra.scenario-configuration/v1",
            "scenario_id": self.scenario_id,
            "definition_version": self.definition_version,
            "definition_digest": self.definition_digest,
            "mode": self.mode.value,
            "input_digest": self.input_digest,
            "seed": self.seed,
            "profile": self.profile.to_dict(),
            "faults": [item.to_dict() for item in self.faults],
            "preflight_targets": [item.to_dict() for item in self.preflight_targets],
            "policy": self.policy.to_dict(),
            "expected_policy_digest": self.expected_policy_digest,
            "requested_by": self.requested_by,
            "labels": dict(self.labels),
            "metadata": canonicalize(self.metadata),
        }
        if include_input:
            value["input_text"] = self.input_text
        if include_digest:
            value["configuration_digest"] = self.configuration_digest
        return value


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    check_id: str
    kind: str
    path: str
    policy: str
    clean: bool
    observed_entries: int
    digest: str
    reason: str
    checked_at: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(_slot_dict(self))


@dataclass(frozen=True, slots=True)
class PreflightReceipt:
    receipt_id: str
    scenario_run_id: str
    clean: bool
    new_input: bool
    input_digest: str
    checks: tuple[PreflightCheck, ...]
    checked_at: str
    receipt_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.scenario-preflight-receipt/v1",
            "receipt_id": self.receipt_id,
            "scenario_run_id": self.scenario_run_id,
            "clean": self.clean,
            "new_input": self.new_input,
            "input_digest": self.input_digest,
            "checks": [item.to_dict() for item in self.checks],
            "checked_at": self.checked_at,
            "receipt_digest": self.receipt_digest,
        }


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision_id: str
    action_id: str
    action: str
    requested_effect: str
    final_effect: str
    reason_code: str
    reason: str
    recovery_action: str
    policy_digest: str
    sequence: int
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(_slot_dict(self))


@dataclass(frozen=True, slots=True)
class InterventionRecord:
    intervention_id: str
    kind: str
    actor_id: str
    action: str
    counted_as_human: bool
    rejected: bool
    reason: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return canonicalize(_slot_dict(self))


@dataclass(frozen=True, slots=True)
class EffectiveStep:
    step_id: str
    event_id: str
    event_type: str
    effect: StepEffect
    disposition: StepDisposition
    reason_code: str
    run_id: str
    task_id: str
    stage: str
    worker_id: str
    profile_id: str
    provider_id: str
    sequence: int
    causal_parent_ids: tuple[str, ...]
    semantic_digest: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_slot_dict(self)),
            "effect": self.effect.value,
            "disposition": self.disposition.value,
            "causal_parent_ids": list(self.causal_parent_ids),
        }


@dataclass(frozen=True, slots=True)
class MetricSample:
    sample_id: str
    metric: str
    value: float
    unit: str
    dimensions: dict[str, str]
    source_step_ids: tuple[str, ...]
    observed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **canonicalize(_slot_dict(self)),
            "source_step_ids": list(self.source_step_ids),
        }


@dataclass(frozen=True, slots=True)
class OwnerExecutionResult:
    owner_run_id: str
    task_id: str
    task: dict[str, Any]
    events: tuple[dict[str, Any], ...]
    artifacts: tuple[dict[str, Any], ...]
    owner_receipts: tuple[dict[str, Any], ...]
    started_at: str
    completed_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "task": canonicalize(self.task),
            "events": canonicalize(self.events),
            "artifacts": canonicalize(self.artifacts),
            "owner_receipts": canonicalize(self.owner_receipts),
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }


@dataclass(frozen=True, slots=True)
class ScenarioRun:
    scenario_run_id: str
    configuration: ScenarioConfiguration
    phase: ScenarioPhase
    revision: int
    created_at: str
    updated_at: str
    started_at: str = ""
    completed_at: str = ""
    owner_run_id: str = ""
    task_id: str = ""
    preflight_receipt: dict[str, Any] | None = None
    policy_decisions: tuple[dict[str, Any], ...] = ()
    interventions: tuple[dict[str, Any], ...] = ()
    evidence_manifest: dict[str, Any] | None = None
    verification_receipt: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    cancel_requested: bool = False
    archive_reason: str = ""

    @classmethod
    def create(cls, configuration: ScenarioConfiguration) -> "ScenarioRun":
        now = utc_now()
        return cls(
            scenario_run_id=new_identity("scenario"),
            configuration=configuration,
            phase=ScenarioPhase.CREATED,
            revision=0,
            created_at=now,
            updated_at=now,
        )

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def evolve(self, **changes: Any) -> "ScenarioRun":
        return replace(
            self,
            **changes,
            revision=self.revision + 1,
            updated_at=utc_now(),
        )

    def to_dict(
        self,
        *,
        include_input: bool = False,
        include_evidence: bool = True,
        projection: str = "detail",
    ) -> dict[str, Any]:
        """Project the run.

        ``projection`` selects how much of the record to emit:

        ``detail``
            The whole record.  Used for a single run.

        ``summary``
            Only what a run list renders.  A page of 34 runs used to send every
            run's ``policy_decisions`` (54% of the page), the full
            ``configuration`` including the fault schedule and policy block
            (28%), and the preflight receipt (13%) -- none of which the list
            shows.  That was 860KB and ~14 seconds per poll.

        ``include_evidence`` must be False for list projections: the evidence
        manifest embeds every canonical event and artifact and can exceed tens
        of megabytes, so carrying it on a page of runs overflows the client's
        response-size limit and no run ever becomes visible.  Callers that need
        the manifest read it from the per-run evidence endpoint.
        """

        if projection == "summary":
            return self._summary_dict()
        if projection != "detail":
            raise ValueError(f"unknown scenario run projection: {projection!r}")

        value = {
            "schema": "zyra.scenario-run/v1",
            "scenario_run_id": self.scenario_run_id,
            "configuration": self.configuration.to_dict(include_input=include_input),
            "phase": self.phase.value,
            "terminal": self.terminal,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "preflight_receipt": canonicalize(self.preflight_receipt),
            "policy_decisions": canonicalize(self.policy_decisions),
            "interventions": canonicalize(self.interventions),
            "human_intervention_count": sum(
                1 for item in self.interventions if item.get("counted_as_human") is True
            ),
            "operator_intervention_attempt_count": len(self.interventions),
            "evidence_manifest": (
                canonicalize(self.evidence_manifest) if include_evidence else None
            ),
            "verification_receipt": canonicalize(self.verification_receipt),
            "failure": canonicalize(self.failure),
            "cancel_requested": self.cancel_requested,
            "archive_reason": self.archive_reason,
        }
        return value

    def _summary_dict(self) -> dict[str, Any]:
        """The run fields a list view renders, plus what it needs to display.

        Deliberately excludes ``policy_decisions``, the full ``configuration``
        and ``preflight_receipt``: together those were 95% of a list page and
        the list renders none of them.  ``configuration`` keeps only the parts
        the list reads (scenario id, mode, seed) plus the digests a client uses
        to detect that the underlying definition changed.
        """

        return {
            "schema": "zyra.scenario-run/v1",
            "scenario_run_id": self.scenario_run_id,
            "configuration": self.configuration.summary_dict(),
            "phase": self.phase.value,
            "terminal": self.terminal,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "owner_run_id": self.owner_run_id,
            "task_id": self.task_id,
            "interventions": canonicalize(self.interventions),
            "human_intervention_count": sum(
                1 for item in self.interventions if item.get("counted_as_human") is True
            ),
            "operator_intervention_attempt_count": len(self.interventions),
            "evidence_manifest": None,
            "failure": canonicalize(self.failure),
            "cancel_requested": self.cancel_requested,
            "archive_reason": self.archive_reason,
        }
