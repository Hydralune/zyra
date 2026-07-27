from __future__ import annotations

import concurrent.futures
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


class ApprovalEffect(StrEnum):
    PENDING = "pending"
    ALLOW = "allow"
    DENY = "deny"
    EXPIRED = "expired"
    STALE = "stale"
    REJECTED = "rejected"


class ApprovalPhase(StrEnum):
    CREATED = "created"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CONSUMED = "consumed"
    TIMED_OUT = "timed_out"
    RESTORED = "restored"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalChallenge:
    approval_id: str
    session_id: str
    run_id: str
    request_id: str
    tool_call_id: str
    action_digest: str
    policy_digest: str
    identity_digest: str
    nonce: str
    idempotency_key: str
    created_at_ns: int
    expires_at_ns: int
    generation: int
    sealed: bool
    risk: str

    @property
    def digest(self) -> str:
        return stable_digest(
            self.approval_id,
            self.session_id,
            self.run_id,
            self.request_id,
            self.tool_call_id,
            self.action_digest,
            self.policy_digest,
            self.identity_digest,
            self.nonce,
            self.idempotency_key,
            self.created_at_ns,
            self.expires_at_ns,
            self.generation,
            self.sealed,
            self.risk,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "request_id": self.request_id,
            "tool_call_id": self.tool_call_id,
            "action_digest": self.action_digest,
            "policy_digest": self.policy_digest,
            "identity_digest": self.identity_digest,
            "nonce_digest": stable_digest("nonce", self.nonce),
            "idempotency_key": self.idempotency_key,
            "created_at_ns": self.created_at_ns,
            "expires_at_ns": self.expires_at_ns,
            "generation": self.generation,
            "sealed": self.sealed,
            "risk": self.risk,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class ApprovalObservation:
    observation_id: str
    approval_id: str
    session_id: str
    run_id: str
    phase: ApprovalPhase
    effect: ApprovalEffect
    challenge_digest: str
    action_digest: str
    policy_digest: str
    identity_digest: str
    nonce: str
    idempotency_key: str
    generation: int
    sequence: int
    observed_at_ns: int
    consumer_id: str = ""
    error_code: str = ""
    terminal: bool = False
    committed: bool = False
    restored_from_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "approval_id": self.approval_id,
            "session_id": self.session_id,
            "run_id": self.run_id,
            "phase": self.phase.value,
            "effect": self.effect.value,
            "challenge_digest": self.challenge_digest,
            "action_digest": self.action_digest,
            "policy_digest": self.policy_digest,
            "identity_digest": self.identity_digest,
            "nonce_digest": stable_digest("nonce", self.nonce),
            "idempotency_key": self.idempotency_key,
            "generation": self.generation,
            "sequence": self.sequence,
            "observed_at_ns": self.observed_at_ns,
            "consumer_id": self.consumer_id,
            "error_code": self.error_code,
            "terminal": self.terminal,
            "committed": self.committed,
            "restored_from_digest": self.restored_from_digest,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ApprovalFinding:
    code: str
    approval_id: str
    observation_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "approval_id": self.approval_id,
            "observation_id": self.observation_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ApprovalSecurityReport:
    challenges: tuple[ApprovalChallenge, ...]
    observations: tuple[ApprovalObservation, ...]
    findings: tuple[ApprovalFinding, ...]
    phase_counts: Mapping[str, int]
    effect_counts: Mapping[str, int]
    terminal_counts: Mapping[str, int]
    sealed_pending: tuple[str, ...]
    digest: str

    @property
    def valid(self) -> bool:
        required_phases = {
            ApprovalPhase.CREATED,
            ApprovalPhase.CLAIMED,
            ApprovalPhase.RESOLVED,
            ApprovalPhase.CONSUMED,
            ApprovalPhase.TIMED_OUT,
            ApprovalPhase.RESTORED,
            ApprovalPhase.REJECTED,
        }
        observed = {item.phase for item in self.observations}
        return (
            not self.findings
            and not self.sealed_pending
            and required_phases.issubset(observed)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-approval-security/v1",
            "valid": self.valid,
            "challenges": [item.to_dict() for item in self.challenges],
            "observations": [item.to_dict() for item in self.observations],
            "findings": [item.to_dict() for item in self.findings],
            "phase_counts": dict(sorted(self.phase_counts.items())),
            "effect_counts": dict(sorted(self.effect_counts.items())),
            "terminal_counts": dict(sorted(self.terminal_counts.items())),
            "sealed_pending": list(self.sealed_pending),
            "digest": self.digest,
        }


class ApprovalSubject(Protocol):
    def resolve(
        self,
        challenge: ApprovalChallenge,
        *,
        effect: ApprovalEffect,
        session_id: str,
        nonce: str,
        idempotency_key: str,
    ) -> Mapping[str, Any]:
        ...

    def snapshot(self) -> Mapping[str, Any]:
        ...

    def restore(self, snapshot: Mapping[str, Any]) -> None:
        ...


@dataclass(frozen=True, slots=True)
class ConcurrencyProbeReceipt:
    challenge_digest: str
    effects: tuple[str, ...]
    terminal_successes: int
    duplicate_successes: int
    errors: tuple[str, ...]
    duration_seconds: float
    snapshot_digest: str
    restored_snapshot_digest: str

    @property
    def valid(self) -> bool:
        return (
            self.terminal_successes == 1
            and self.duplicate_successes == 0
            and not self.errors
            and self.snapshot_digest == self.restored_snapshot_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "challenge_digest": self.challenge_digest,
            "effects": list(self.effects),
            "terminal_successes": self.terminal_successes,
            "duplicate_successes": self.duplicate_successes,
            "errors": list(self.errors),
            "duration_seconds": round(self.duration_seconds, 6),
            "snapshot_digest": self.snapshot_digest,
            "restored_snapshot_digest": self.restored_snapshot_digest,
            "valid": self.valid,
        }


class ApprovalConcurrencyProbe:
    """Drives a real approval subject through race and crash/restore inputs."""

    def __init__(self, *, maximum_workers: int = 8, timeout_seconds: float = 5.0) -> None:
        self.maximum_workers = int(maximum_workers)
        self.timeout_seconds = float(timeout_seconds)
        if self.maximum_workers < 2:
            raise ValueError("approval race probe requires at least two workers")
        if self.timeout_seconds <= 0:
            raise ValueError("approval race timeout must be positive")

    def execute(
        self,
        subject: ApprovalSubject,
        challenge: ApprovalChallenge,
    ) -> ConcurrencyProbeReceipt:
        started = time.monotonic()
        gate = threading.Barrier(self.maximum_workers)

        def compete(index: int) -> Mapping[str, Any]:
            gate.wait(timeout=self.timeout_seconds)
            return subject.resolve(
                challenge,
                effect=(
                    ApprovalEffect.ALLOW
                    if index % 2 == 0
                    else ApprovalEffect.DENY
                ),
                session_id=challenge.session_id,
                nonce=challenge.nonce,
                idempotency_key=f"{challenge.idempotency_key}:race:{index}",
            )

        results: list[Mapping[str, Any]] = []
        errors: list[str] = []
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=self.maximum_workers,
            thread_name_prefix="approval-race",
        ) as pool:
            futures = [pool.submit(compete, index) for index in range(self.maximum_workers)]
            for future in futures:
                try:
                    results.append(future.result(timeout=self.timeout_seconds))
                except BaseException as error:
                    errors.append(f"{type(error).__name__}:{error}")
        terminal = [
            item
            for item in results
            if bool(item.get("terminal")) and bool(item.get("committed"))
        ]
        duplicate_successes = max(0, len(terminal) - 1)
        snapshot = dict(subject.snapshot())
        snapshot_digest = stable_digest(snapshot)
        subject.restore(snapshot)
        restored_snapshot_digest = stable_digest(subject.snapshot())
        return ConcurrencyProbeReceipt(
            challenge_digest=challenge.digest,
            effects=tuple(
                sorted(str(item.get("effect") or "") for item in results)
            ),
            terminal_successes=len(terminal),
            duplicate_successes=duplicate_successes,
            errors=tuple(errors),
            duration_seconds=time.monotonic() - started,
            snapshot_digest=snapshot_digest,
            restored_snapshot_digest=restored_snapshot_digest,
        )


class ApprovalSecurityAuditor:
    def audit(
        self,
        challenges: Sequence[ApprovalChallenge],
        observations: Sequence[ApprovalObservation],
    ) -> ApprovalSecurityReport:
        findings: list[ApprovalFinding] = []
        challenges_by_id = {item.approval_id: item for item in challenges}
        if len(challenges_by_id) != len(challenges):
            findings.append(
                ApprovalFinding(
                    code="duplicate_challenge",
                    approval_id="",
                    observation_id="",
                    reason="approval challenge identity is duplicated",
                )
            )
        by_approval: dict[str, list[ApprovalObservation]] = defaultdict(list)
        observation_ids: set[str] = set()
        for observation in observations:
            by_approval[observation.approval_id].append(observation)
            if observation.observation_id in observation_ids:
                findings.append(
                    self._finding(
                        "duplicate_observation",
                        observation,
                        "approval observation identity is duplicated",
                    )
                )
            observation_ids.add(observation.observation_id)
            challenge = challenges_by_id.get(observation.approval_id)
            if challenge is None:
                findings.append(
                    self._finding(
                        "orphan_observation",
                        observation,
                        "approval observation has no challenge",
                    )
                )
            else:
                findings.extend(self._observation_findings(challenge, observation))
        terminal_counts: dict[str, int] = {}
        sealed_pending: list[str] = []
        for challenge in challenges:
            values = by_approval.get(challenge.approval_id, [])
            findings.extend(self._lifecycle_findings(challenge, values))
            terminal = [
                item
                for item in values
                if item.terminal and item.committed
            ]
            terminal_counts[challenge.approval_id] = len(terminal)
            if challenge.sealed and not terminal:
                sealed_pending.append(challenge.approval_id)
        phase_counts = Counter(item.phase.value for item in observations)
        effect_counts = Counter(item.effect.value for item in observations)
        material = {
            "challenges": [item.to_dict() for item in challenges],
            "observations": [item.to_dict() for item in observations],
            "findings": [item.to_dict() for item in findings],
            "terminal_counts": terminal_counts,
            "sealed_pending": sealed_pending,
        }
        return ApprovalSecurityReport(
            challenges=tuple(challenges),
            observations=tuple(observations),
            findings=tuple(findings),
            phase_counts=dict(phase_counts),
            effect_counts=dict(effect_counts),
            terminal_counts=terminal_counts,
            sealed_pending=tuple(sealed_pending),
            digest=stable_digest(material),
        )

    def mutation_campaign(
        self,
        challenges: Sequence[ApprovalChallenge],
        observations: Sequence[ApprovalObservation],
    ) -> Mapping[str, ApprovalSecurityReport]:
        baseline = self.audit(challenges, observations)
        resolved = next(
            item for item in observations if item.phase is ApprovalPhase.RESOLVED
        )
        challenge = next(
            item for item in challenges if item.approval_id == resolved.approval_id
        )
        forged_identity = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-identity",
            identity_digest=f"{resolved.identity_digest}-forged",
        )
        forged_action = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-action",
            action_digest=f"{resolved.action_digest}-forged",
        )
        forged_policy = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-policy",
            policy_digest=f"{resolved.policy_digest}-forged",
        )
        replay_nonce = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-nonce",
            nonce=f"{resolved.nonce}-forged",
        )
        cross_session = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-session",
            session_id=f"{resolved.session_id}-other",
        )
        stale_generation = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-generation",
            generation=max(0, resolved.generation - 1),
        )
        late_allow = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-late",
            observed_at_ns=challenge.expires_at_ns + 1,
            effect=ApprovalEffect.ALLOW,
            terminal=True,
            committed=True,
        )
        duplicate_terminal = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-duplicate",
            sequence=resolved.sequence + 100,
            effect=(
                ApprovalEffect.DENY
                if resolved.effect is ApprovalEffect.ALLOW
                else ApprovalEffect.ALLOW
            ),
            terminal=True,
            committed=True,
        )
        no_idempotency = replace(
            resolved,
            observation_id=f"{resolved.observation_id}-idempotency",
            idempotency_key="",
        )
        no_restore_digest = replace(
            next(
                item for item in observations if item.phase is ApprovalPhase.RESTORED
            ),
            observation_id="approval-restore-missing-digest",
            restored_from_digest="",
        )
        return {
            "baseline": baseline,
            "forged_identity": self.audit(challenges, (*observations, forged_identity)),
            "forged_action": self.audit(challenges, (*observations, forged_action)),
            "forged_policy": self.audit(challenges, (*observations, forged_policy)),
            "forged_nonce": self.audit(challenges, (*observations, replay_nonce)),
            "cross_session": self.audit(challenges, (*observations, cross_session)),
            "stale_generation": self.audit(challenges, (*observations, stale_generation)),
            "late_allow": self.audit(challenges, (*observations, late_allow)),
            "duplicate_terminal": self.audit(challenges, (*observations, duplicate_terminal)),
            "missing_idempotency": self.audit(challenges, (*observations, no_idempotency)),
            "restore_without_digest": self.audit(
                challenges,
                (*observations, no_restore_digest),
            ),
        }

    @staticmethod
    def _observation_findings(
        challenge: ApprovalChallenge,
        observation: ApprovalObservation,
    ) -> list[ApprovalFinding]:
        findings: list[ApprovalFinding] = []

        def add(code: str, reason: str) -> None:
            findings.append(ApprovalSecurityAuditor._finding(code, observation, reason))

        if observation.challenge_digest != challenge.digest:
            add("challenge_digest_mismatch", "observation is not bound to challenge")
        if observation.session_id != challenge.session_id:
            add("cross_session", "observation belongs to another session")
        if observation.run_id != challenge.run_id:
            add("cross_run", "observation belongs to another run")
        if observation.action_digest != challenge.action_digest:
            add("action_digest_mismatch", "approved action digest changed")
        if observation.policy_digest != challenge.policy_digest:
            add("policy_digest_mismatch", "approval policy digest changed")
        if observation.identity_digest != challenge.identity_digest:
            add("identity_digest_mismatch", "approver/subject identity digest changed")
        if observation.nonce != challenge.nonce:
            add("nonce_mismatch", "approval nonce does not match challenge")
        if observation.idempotency_key == "":
            add("idempotency_missing", "approval observation lacks idempotency key")
        if observation.generation != challenge.generation:
            add("stale_generation", "approval generation is stale or from the future")
        if observation.observed_at_ns < challenge.created_at_ns:
            add("observation_before_creation", "observation predates challenge")
        if (
            observation.observed_at_ns > challenge.expires_at_ns
            and observation.effect in {ApprovalEffect.ALLOW, ApprovalEffect.DENY}
        ):
            add("late_resolution", "allow/deny arrived after challenge expiry")
        if observation.terminal and not observation.committed:
            add("terminal_uncommitted", "terminal approval observation is uncommitted")
        if observation.phase in {
            ApprovalPhase.REJECTED,
            ApprovalPhase.TIMED_OUT,
        }:
            if not observation.error_code:
                add("error_code_missing", "negative terminal lacks stable error code")
        if observation.phase is ApprovalPhase.RESTORED:
            if not observation.restored_from_digest:
                add("restore_digest_missing", "restored approval lacks source snapshot digest")
        return findings

    @staticmethod
    def _lifecycle_findings(
        challenge: ApprovalChallenge,
        values: Sequence[ApprovalObservation],
    ) -> list[ApprovalFinding]:
        findings: list[ApprovalFinding] = []
        if not values:
            return [
                ApprovalFinding(
                    code="challenge_unobserved",
                    approval_id=challenge.approval_id,
                    observation_id="",
                    reason="challenge has no lifecycle observations",
                )
            ]
        ordered = sorted(values, key=lambda item: (item.sequence, item.observed_at_ns))
        sequences = [item.sequence for item in ordered]
        if len(sequences) != len(set(sequences)):
            findings.append(
                ApprovalFinding(
                    code="duplicate_sequence",
                    approval_id=challenge.approval_id,
                    observation_id="",
                    reason="approval lifecycle sequence is duplicated",
                )
            )
        terminals = [
            item
            for item in ordered
            if item.terminal and item.committed
        ]
        if len(terminals) > 1:
            findings.append(
                ApprovalFinding(
                    code="multiple_terminal_effects",
                    approval_id=challenge.approval_id,
                    observation_id="",
                    reason=f"approval has {len(terminals)} committed terminal effects",
                )
            )
        phases = [item.phase for item in ordered]
        rank = {
            ApprovalPhase.CREATED: 1,
            ApprovalPhase.CLAIMED: 2,
            ApprovalPhase.RESOLVED: 3,
            ApprovalPhase.CONSUMED: 4,
            ApprovalPhase.TIMED_OUT: 3,
            ApprovalPhase.REJECTED: 3,
            ApprovalPhase.RESTORED: 2,
        }
        numeric = [rank[item] for item in phases]
        if numeric != sorted(numeric):
            findings.append(
                ApprovalFinding(
                    code="phase_regression",
                    approval_id=challenge.approval_id,
                    observation_id="",
                    reason=f"approval phases regress: {[item.value for item in phases]}",
                )
            )
        if challenge.sealed:
            pending = [
                item
                for item in ordered
                if item.effect is ApprovalEffect.PENDING
            ]
            terminal = [
                item
                for item in ordered
                if item.terminal and item.committed
            ]
            if pending and not terminal:
                findings.append(
                    ApprovalFinding(
                        code="sealed_pending_hang",
                        approval_id=challenge.approval_id,
                        observation_id=pending[-1].observation_id,
                        reason="sealed policy left approval pending without terminal deny/expiry",
                    )
                )
        return findings

    @staticmethod
    def _finding(
        code: str,
        observation: ApprovalObservation,
        reason: str,
    ) -> ApprovalFinding:
        return ApprovalFinding(
            code=code,
            approval_id=observation.approval_id,
            observation_id=observation.observation_id,
            reason=reason,
        )


def approval_challenges_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[ApprovalChallenge, ...]:
    return tuple(
        ApprovalChallenge(
            approval_id=str(value.get("approval_id") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            request_id=str(value.get("request_id") or ""),
            tool_call_id=str(value.get("tool_call_id") or ""),
            action_digest=str(value.get("action_digest") or ""),
            policy_digest=str(value.get("policy_digest") or ""),
            identity_digest=str(value.get("identity_digest") or ""),
            nonce=str(value.get("nonce") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            created_at_ns=int(value.get("created_at_ns") or 0),
            expires_at_ns=int(value.get("expires_at_ns") or 0),
            generation=int(value.get("generation") or 0),
            sealed=bool(value.get("sealed")),
            risk=str(value.get("risk") or ""),
        )
        for value in values
    )


def approval_observations_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[ApprovalObservation, ...]:
    return tuple(
        ApprovalObservation(
            observation_id=str(value.get("observation_id") or ""),
            approval_id=str(value.get("approval_id") or ""),
            session_id=str(value.get("session_id") or ""),
            run_id=str(value.get("run_id") or ""),
            phase=ApprovalPhase(str(value.get("phase") or "")),
            effect=ApprovalEffect(str(value.get("effect") or "")),
            challenge_digest=str(value.get("challenge_digest") or ""),
            action_digest=str(value.get("action_digest") or ""),
            policy_digest=str(value.get("policy_digest") or ""),
            identity_digest=str(value.get("identity_digest") or ""),
            nonce=str(value.get("nonce") or ""),
            idempotency_key=str(value.get("idempotency_key") or ""),
            generation=int(value.get("generation") or 0),
            sequence=int(value.get("sequence") or 0),
            observed_at_ns=int(value.get("observed_at_ns") or 0),
            consumer_id=str(value.get("consumer_id") or ""),
            error_code=str(value.get("error_code") or ""),
            terminal=bool(value.get("terminal")),
            committed=bool(value.get("committed")),
            restored_from_digest=str(value.get("restored_from_digest") or ""),
            metadata=(
                dict(value.get("metadata"))
                if isinstance(value.get("metadata"), Mapping)
                else {}
            ),
        )
        for value in values
    )


def evaluate_approval_security(
    challenges: Sequence[ApprovalChallenge],
    observations: Sequence[ApprovalObservation],
    *,
    concurrency_receipt: ConcurrencyProbeReceipt | None = None,
) -> CaseExecutionBuffer:
    reports = ApprovalSecurityAuditor().mutation_campaign(
        challenges,
        observations,
    )
    buffer = CaseExecutionBuffer()
    baseline = reports["baseline"]
    base_observation = buffer.observe(
        "approval-security.baseline",
        ObservationKind.APPROVAL,
        "approval-identity-race-restore-sealed",
        "verified" if baseline.valid else "invalid",
        attributes={
            "report_digest": baseline.digest,
            "phase_counts": baseline.phase_counts,
            "effect_counts": baseline.effect_counts,
            "terminal_counts": baseline.terminal_counts,
        },
    )
    buffer.assert_that(
        "approval-security.baseline-valid",
        baseline.valid,
        "approval lifecycle must be identity-bound, terminal and restorable",
        evidence=(base_observation.observation_id,),
        failure_kind=FailureKind.SECURITY,
    )
    if concurrency_receipt is not None:
        race_observation = buffer.observe(
            "approval-security.race",
            ObservationKind.APPROVAL,
            "approval-concurrency-and-crash-restore",
            "verified" if concurrency_receipt.valid else "invalid",
            attributes=concurrency_receipt.to_dict(),
        )
        buffer.assert_that(
            "approval-security.race-valid",
            concurrency_receipt.valid,
            "concurrent resolutions must have one terminal effect and exact restore",
            evidence=(race_observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    for name, report in reports.items():
        if name == "baseline":
            continue
        observation = buffer.observe(
            f"approval-security.{name}",
            ObservationKind.MUTATION,
            f"approval-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "report_digest": report.digest,
                "finding_codes": [item.code for item in report.findings],
            },
        )
        buffer.assert_that(
            f"approval-security.reject-{name}",
            not report.valid,
            f"{name} approval mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    return buffer


__all__ = [
    "ApprovalChallenge",
    "ApprovalConcurrencyProbe",
    "ApprovalEffect",
    "ApprovalFinding",
    "ApprovalObservation",
    "ApprovalPhase",
    "ApprovalSecurityAuditor",
    "ApprovalSecurityReport",
    "ApprovalSubject",
    "ConcurrencyProbeReceipt",
    "approval_challenges_from_mappings",
    "approval_observations_from_mappings",
    "evaluate_approval_security",
]
