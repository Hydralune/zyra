from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Sequence

from .curator_integration_models import (
    CuratorConsumer,
    CuratorFailureContract,
    CuratorOutcome,
    CuratorOutcomeKind,
    CuratorOutcomeState,
    FailureDisposition,
    FailureSeverity,
    consumers_for_outcome,
    failure_from_decision,
    outcome_kind_for_candidate,
)
from .curator_models import (
    CandidateKind,
    CommitDisposition,
    CuratorRunResult,
    DecisionStatus,
    MemoryCandidate,
    MemoryCommitReceipt,
    MemoryDecision,
    OutboxKind,
    OutboxState,
    stable_digest,
)
from .curator_store import CuratorCandidateStore


class CuratorOutcomeProjectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CuratorProjectionDraft:
    result: CuratorRunResult
    outcomes: tuple[CuratorOutcome, ...]
    failures: tuple[CuratorFailureContract, ...]
    candidate_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    index_message_ids: tuple[str, ...]
    event_message_ids: tuple[str, ...]
    missing_candidate_ids: tuple[str, ...] = ()
    missing_decision_ids: tuple[str, ...] = ()
    missing_receipt_candidate_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorProjectionDraft:
        if self.result.status != "succeeded":
            raise CuratorOutcomeProjectionError(
                "only a succeeded curator result can produce a published projection"
            )
        if self.missing_candidate_ids:
            raise CuratorOutcomeProjectionError(
                f"curator projection is missing candidates: {self.missing_candidate_ids}"
            )
        if self.missing_decision_ids:
            raise CuratorOutcomeProjectionError(
                f"curator projection is missing decisions: {self.missing_decision_ids}"
            )
        outcome_ids = [item.outcome_id for item in self.outcomes]
        if len(set(outcome_ids)) != len(outcome_ids):
            raise CuratorOutcomeProjectionError("projection contains duplicate outcome IDs")
        if any(item.task_id != self.result.task_id for item in self.outcomes):
            raise CuratorOutcomeProjectionError("projection contains a foreign-task outcome")
        if any(item.run_id != self.result.run_id for item in self.outcomes):
            raise CuratorOutcomeProjectionError("projection contains a foreign-run outcome")
        for outcome in self.outcomes:
            outcome.validated()
            if outcome.canonical_memory_changed and outcome.decision_id not in self.decision_ids:
                raise CuratorOutcomeProjectionError(
                    "canonical outcome does not reference a run decision"
                )
            if outcome.canonical_memory_changed and outcome.commit_receipt_id not in self.receipt_ids:
                raise CuratorOutcomeProjectionError(
                    "canonical outcome does not reference a run commit receipt"
                )
        for failure in self.failures:
            failure.validated()
            if failure.canonical_memory_changed:
                raise CuratorOutcomeProjectionError(
                    "failure projection cannot claim canonical memory change"
                )
        return self

    @property
    def canonical_outcomes(self) -> tuple[CuratorOutcome, ...]:
        return tuple(item for item in self.outcomes if item.canonical_memory_changed)

    @property
    def published_index_outcomes(self) -> tuple[CuratorOutcome, ...]:
        return tuple(item for item in self.outcomes if item.index_published)

    @property
    def rejected_outcomes(self) -> tuple[CuratorOutcome, ...]:
        return tuple(
            item
            for item in self.outcomes
            if item.kind
            in {
                CuratorOutcomeKind.REJECTED,
                CuratorOutcomeKind.DISCARDED,
                CuratorOutcomeKind.NOOP,
                CuratorOutcomeKind.MERGE_REQUIRED,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "result": self.result.to_dict(),
            "outcomes": [item.to_dict() for item in self.outcomes],
            "failures": [item.to_dict() for item in self.failures],
            "candidate_ids": list(self.candidate_ids),
            "decision_ids": list(self.decision_ids),
            "receipt_ids": list(self.receipt_ids),
            "index_message_ids": list(self.index_message_ids),
            "event_message_ids": list(self.event_message_ids),
            "missing_candidate_ids": list(self.missing_candidate_ids),
            "missing_decision_ids": list(self.missing_decision_ids),
            "missing_receipt_candidate_ids": list(self.missing_receipt_candidate_ids),
            "warnings": list(self.warnings),
            "canonical_outcome_count": len(self.canonical_outcomes),
            "published_index_outcome_count": len(self.published_index_outcomes),
            "rejected_outcome_count": len(self.rejected_outcomes),
            "diagnostics": dict(self.diagnostics),
        }


class CuratorOutcomeProjector:
    """Project candidate/decision/commit/index phases from durable 06B-01 state.

    The projector never infers a commit from a model response or candidate
    state.  A canonical outcome requires a persisted deterministic decision,
    persisted commit receipt and a memory identity/revision.  Index publication
    is a separate phase backed by the delivered index outbox message.
    """

    def __init__(self, store: CuratorCandidateStore) -> None:
        self.store = store

    def project(self, result: CuratorRunResult) -> CuratorProjectionDraft:
        if result.status != "succeeded":
            raise CuratorOutcomeProjectionError(
                f"curator job is not successful: {result.status}"
            )
        candidates, missing_candidates = self._load_candidates(result.candidate_ids)
        decisions, missing_decisions = self._load_decisions(result.decision_ids)
        decisions_by_candidate = self._decisions_by_candidate(decisions)
        receipts_by_candidate = self._receipts_by_candidate(result, candidates)
        outbox = self.store.outbox_messages(task_id=result.task_id, limit=100_000)
        outbox_by_candidate: dict[str, list[Any]] = {}
        for message in outbox:
            outbox_by_candidate.setdefault(message.aggregate_id, []).append(message)
        outcomes: list[CuratorOutcome] = []
        failures: list[CuratorFailureContract] = []
        warnings: list[str] = []
        index_message_ids: list[str] = []
        event_message_ids: list[str] = []
        missing_receipts: list[str] = []
        for candidate in candidates:
            candidate_outcome = self._candidate_outcome(result, candidate)
            outcomes.append(candidate_outcome)
            decision = decisions_by_candidate.get(candidate.candidate_id)
            receipt = receipts_by_candidate.get(candidate.candidate_id)
            if decision is None:
                failures.append(
                    self._missing_decision_failure(result=result, candidate=candidate)
                )
                continue
            decision_outcome = self._decision_outcome(
                result=result,
                candidate=candidate,
                decision=decision,
            )
            outcomes.append(decision_outcome)
            validation_failure = failure_from_decision(
                result=result,
                candidate=candidate,
                decision=decision,
                outcome_id=decision_outcome.outcome_id,
            )
            if validation_failure is not None:
                failures.append(validation_failure)
            if decision.accepted and receipt is None:
                missing_receipts.append(candidate.candidate_id)
                failures.append(
                    self._missing_receipt_failure(
                        result=result,
                        candidate=candidate,
                        decision=decision,
                        outcome_id=decision_outcome.outcome_id,
                    )
                )
                continue
            if receipt is None:
                continue
            final_kind = outcome_kind_for_candidate(candidate, decision, receipt)
            candidate_outbox = tuple(outbox_by_candidate.get(candidate.candidate_id, ()))
            event_messages = tuple(
                message
                for message in candidate_outbox
                if message.kind is OutboxKind.EVENT
            )
            index_messages = tuple(
                message
                for message in candidate_outbox
                if message.kind is OutboxKind.INDEX_SYNC
            )
            event_message_ids.extend(message.message_id for message in event_messages)
            index_message_ids.extend(message.message_id for message in index_messages)
            event_published = bool(event_messages) and all(
                message.state is OutboxState.DELIVERED for message in event_messages
            )
            index_published = bool(index_messages) and all(
                message.state is OutboxState.DELIVERED for message in index_messages
            )
            final_outcome = self._final_outcome(
                result=result,
                candidate=candidate,
                decision=decision,
                receipt=receipt,
                kind=final_kind,
                event_messages=event_messages,
                index_messages=index_messages,
                event_published=event_published,
                index_published=index_published,
            )
            outcomes.append(final_outcome)
            if final_outcome.canonical_memory_changed and not event_published:
                warnings.append(
                    f"canonical_outcome_event_not_published:{candidate.candidate_id}"
                )
            if final_outcome.canonical_memory_changed and index_published:
                outcomes.append(
                    self._index_outcome(
                        result=result,
                        candidate=candidate,
                        decision=decision,
                        receipt=receipt,
                        final_outcome=final_outcome,
                        index_messages=index_messages,
                    )
                )
            elif final_outcome.canonical_memory_changed:
                warnings.append(
                    f"canonical_outcome_index_not_published:{candidate.candidate_id}"
                )
        draft = CuratorProjectionDraft(
            result=result,
            outcomes=tuple(outcomes),
            failures=tuple(failures),
            candidate_ids=tuple(candidate.candidate_id for candidate in candidates),
            decision_ids=tuple(decision.decision_id for decision in decisions),
            receipt_ids=tuple(
                receipt.receipt_id
                for receipt in receipts_by_candidate.values()
            ),
            index_message_ids=tuple(dict.fromkeys(index_message_ids)),
            event_message_ids=tuple(dict.fromkeys(event_message_ids)),
            missing_candidate_ids=missing_candidates,
            missing_decision_ids=missing_decisions,
            missing_receipt_candidate_ids=tuple(missing_receipts),
            warnings=tuple(dict.fromkeys(warnings)),
            diagnostics={
                "projector": "CuratorOutcomeProjector/1",
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "candidate_owner": "CuratorCandidateStore",
                "model_can_write": False,
                "candidate_phase_count": sum(
                    1 for item in outcomes if item.kind is CuratorOutcomeKind.CANDIDATE
                ),
                "decision_phase_count": sum(
                    1
                    for item in outcomes
                    if item.kind
                    in {
                        CuratorOutcomeKind.ACCEPTED,
                        CuratorOutcomeKind.REJECTED,
                        CuratorOutcomeKind.MERGE_REQUIRED,
                        CuratorOutcomeKind.SUPERSEDED,
                        CuratorOutcomeKind.NOOP,
                        CuratorOutcomeKind.DISCARDED,
                    }
                ),
                "commit_phase_count": sum(
                    1 for item in outcomes if item.canonical_memory_changed
                ),
                "index_phase_count": sum(
                    1 for item in outcomes if item.kind is CuratorOutcomeKind.INDEX_PUBLISHED
                ),
            },
        )
        return draft.validated()

    def _load_candidates(
        self,
        candidate_ids: Sequence[str],
    ) -> tuple[tuple[MemoryCandidate, ...], tuple[str, ...]]:
        values: list[MemoryCandidate] = []
        missing: list[str] = []
        for candidate_id in candidate_ids:
            candidate = self.store.candidate(candidate_id)
            if candidate is None:
                missing.append(candidate_id)
            else:
                values.append(candidate)
        return tuple(values), tuple(missing)

    def _load_decisions(
        self,
        decision_ids: Sequence[str],
    ) -> tuple[tuple[MemoryDecision, ...], tuple[str, ...]]:
        values: list[MemoryDecision] = []
        missing: list[str] = []
        for decision_id in decision_ids:
            decision = self.store.decision(decision_id)
            if decision is None:
                missing.append(decision_id)
            else:
                values.append(decision)
        return tuple(values), tuple(missing)

    @staticmethod
    def _decisions_by_candidate(
        decisions: Sequence[MemoryDecision],
    ) -> Mapping[str, MemoryDecision]:
        output: dict[str, MemoryDecision] = {}
        for decision in decisions:
            existing = output.get(decision.candidate_id)
            if existing is not None and existing.decision_id != decision.decision_id:
                raise CuratorOutcomeProjectionError(
                    f"run contains multiple decisions for candidate {decision.candidate_id}"
                )
            output[decision.candidate_id] = decision
        return output

    def _receipts_by_candidate(
        self,
        result: CuratorRunResult,
        candidates: Sequence[MemoryCandidate],
    ) -> Mapping[str, MemoryCommitReceipt]:
        output = {receipt.candidate_id: receipt for receipt in result.receipts}
        for candidate in candidates:
            stored = self.store.receipt_for_candidate(candidate.candidate_id)
            if stored is None:
                continue
            existing = output.get(candidate.candidate_id)
            if existing is not None and existing.receipt_id != stored.receipt_id:
                raise CuratorOutcomeProjectionError(
                    f"result receipt differs from stored receipt for {candidate.candidate_id}"
                )
            output[candidate.candidate_id] = stored
        return output

    def _candidate_outcome(
        self,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
    ) -> CuratorOutcome:
        return CuratorOutcome.build(
            result=result,
            candidate=candidate,
            kind=CuratorOutcomeKind.CANDIDATE,
            payload={
                "candidate": candidate.to_dict(),
                "phase": "candidate",
                "trusted": False,
                "requires_deterministic_validation": True,
                "can_write_canonical_memory": False,
                "source_proposer": candidate.proposer,
            },
            target_consumers=(CuratorConsumer.AUDIT,),
            metadata={
                "phase": "candidate",
                "canonical_memory_changed": False,
            },
        )

    def _decision_outcome(
        self,
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> CuratorOutcome:
        kind = self._decision_kind(candidate, decision)
        consumers = consumers_for_outcome(kind, canonical_memory_changed=False)
        return CuratorOutcome.build(
            result=result,
            candidate=candidate,
            decision=decision,
            kind=kind,
            payload={
                "decision": decision.to_dict(),
                "phase": "decision",
                "accepted": decision.accepted,
                "retryable": decision.retryable,
                "issue_codes": [issue.code.value for issue in decision.issues],
                "deterministic_validator": True,
                "can_write_canonical_memory": False,
            },
            target_consumers=consumers,
            metadata={
                "phase": "decision",
                "policy_digest": decision.policy_digest,
                "canonical_memory_changed": False,
            },
        )

    def _final_outcome(
        self,
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        receipt: MemoryCommitReceipt,
        kind: CuratorOutcomeKind,
        event_messages: Sequence[Any],
        index_messages: Sequence[Any],
        event_published: bool,
        index_published: bool,
    ) -> CuratorOutcome:
        canonical = receipt.disposition in {
            CommitDisposition.COMMITTED,
            CommitDisposition.ALREADY_COMMITTED,
        }
        consumers = consumers_for_outcome(
            kind,
            canonical_memory_changed=canonical,
        )
        return CuratorOutcome.build(
            result=result,
            candidate=candidate,
            decision=decision,
            receipt=receipt,
            kind=kind,
            payload={
                "receipt": receipt.to_dict(),
                "phase": "commit",
                "event_outbox": [message.to_dict() for message in event_messages],
                "index_outbox": [message.to_dict() for message in index_messages],
                "event_published": event_published,
                "index_published": index_published,
                "transactional_commit": canonical,
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "derived_index_owner": "MemoryIndexRuntime",
            },
            target_consumers=consumers,
            index_published=False,
            metadata={
                "phase": "commit",
                "event_published": event_published,
                "index_publication_observed": index_published,
                "receipt_disposition": receipt.disposition.value,
            },
        )

    def _index_outcome(
        self,
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        receipt: MemoryCommitReceipt,
        final_outcome: CuratorOutcome,
        index_messages: Sequence[Any],
    ) -> CuratorOutcome:
        return CuratorOutcome.build(
            result=result,
            candidate=candidate,
            decision=decision,
            receipt=receipt,
            kind=CuratorOutcomeKind.INDEX_PUBLISHED,
            payload={
                "phase": "index_published",
                "source_outcome_id": final_outcome.outcome_id,
                "memory_id": receipt.memory_id,
                "memory_revision": receipt.memory_revision,
                "index_message_ids": [message.message_id for message in index_messages],
                "index_messages": [message.to_dict() for message in index_messages],
                "derived_index_owner": "MemoryIndexRuntime",
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "index_is_canonical": False,
            },
            target_consumers=(CuratorConsumer.AUDIT,),
            index_published=True,
            canonical_memory_changed=False,
            metadata={
                "phase": "index_published",
                "source_outcome_id": final_outcome.outcome_id,
            },
        )

    @staticmethod
    def _decision_kind(
        candidate: MemoryCandidate,
        decision: MemoryDecision,
    ) -> CuratorOutcomeKind:
        if decision.status is DecisionStatus.ACCEPT:
            return CuratorOutcomeKind.ACCEPTED
        if decision.status is DecisionStatus.REJECT:
            return CuratorOutcomeKind.REJECTED
        if decision.status is DecisionStatus.MERGE_REQUIRED:
            return CuratorOutcomeKind.MERGE_REQUIRED
        if decision.status is DecisionStatus.SUPERSEDE:
            return CuratorOutcomeKind.SUPERSEDED
        if decision.status is DecisionStatus.DISCARD:
            return CuratorOutcomeKind.DISCARDED
        if decision.status is DecisionStatus.NOOP:
            return CuratorOutcomeKind.NOOP
        raise CuratorOutcomeProjectionError(
            f"unsupported decision status: {decision.status.value}"
        )

    @staticmethod
    def _missing_decision_failure(
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
    ) -> CuratorFailureContract:
        return CuratorFailureContract.build(
            run_id=result.run_id,
            task_id=result.task_id,
            curator_job_id=result.job_id,
            phase="deterministic_validation",
            code="decision_missing",
            message="candidate has no persisted deterministic decision",
            severity=FailureSeverity.CRITICAL,
            disposition=FailureDisposition.QUARANTINE,
            retryable=False,
            attempt=0,
            causation_id=candidate.candidate_id,
            candidate_id=candidate.candidate_id,
            evidence_ids=candidate.evidence_ids,
            consumer_hints=(
                CuratorConsumer.FAULT_OBSERVER,
                CuratorConsumer.RECOVERY_PLANNER,
            ),
            details={
                "validator_required": True,
                "canonical_memory_changed": False,
            },
        )

    @staticmethod
    def _missing_receipt_failure(
        *,
        result: CuratorRunResult,
        candidate: MemoryCandidate,
        decision: MemoryDecision,
        outcome_id: str,
    ) -> CuratorFailureContract:
        return CuratorFailureContract.build(
            run_id=result.run_id,
            task_id=result.task_id,
            curator_job_id=result.job_id,
            phase="transactional_commit",
            code="commit_receipt_missing",
            message="accepted candidate has no persisted commit receipt",
            severity=FailureSeverity.CRITICAL,
            disposition=FailureDisposition.RETRY,
            retryable=True,
            attempt=0,
            causation_id=decision.decision_id,
            candidate_id=candidate.candidate_id,
            decision_id=decision.decision_id,
            outcome_id=outcome_id,
            evidence_ids=candidate.evidence_ids,
            consumer_hints=(
                CuratorConsumer.FAULT_OBSERVER,
                CuratorConsumer.RECOVERY_PLANNER,
            ),
            details={
                "committer_required": True,
                "canonical_memory_changed": False,
            },
        )


@dataclass(frozen=True, slots=True)
class CuratorProjectionAudit:
    ok: bool
    task_id: str
    job_id: str
    findings: tuple[str, ...]
    phase_counts: Mapping[str, int]
    candidate_coverage: Mapping[str, tuple[str, ...]]
    canonical_memory_ids: tuple[str, ...]
    index_published_memory_ids: tuple[str, ...]
    deterministic_decision_ids: tuple[str, ...]
    receipt_ids: tuple[str, ...]
    evidence_bundle_ids: tuple[str, ...]
    consumer_counts: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "job_id": self.job_id,
            "findings": list(self.findings),
            "phase_counts": dict(self.phase_counts),
            "candidate_coverage": {
                key: list(values) for key, values in self.candidate_coverage.items()
            },
            "canonical_memory_ids": list(self.canonical_memory_ids),
            "index_published_memory_ids": list(self.index_published_memory_ids),
            "deterministic_decision_ids": list(self.deterministic_decision_ids),
            "receipt_ids": list(self.receipt_ids),
            "evidence_bundle_ids": list(self.evidence_bundle_ids),
            "consumer_counts": dict(self.consumer_counts),
            "canonical_memory_owner": "SQLiteStore.memory_records",
            "model_can_write": False,
        }


class CuratorProjectionAuditor:
    """Check semantic phase coverage and reject report-only success claims."""

    def audit(self, draft: CuratorProjectionDraft) -> CuratorProjectionAudit:
        findings: list[str] = []
        by_candidate: dict[str, list[CuratorOutcome]] = {}
        consumer_counts: dict[str, int] = {}
        phase_counts: dict[str, int] = {}
        for outcome in draft.outcomes:
            by_candidate.setdefault(outcome.candidate_id, []).append(outcome)
            phase_counts[outcome.kind.value] = phase_counts.get(outcome.kind.value, 0) + 1
            for consumer in outcome.target_consumers:
                consumer_counts[consumer.value] = consumer_counts.get(consumer.value, 0) + 1
        for candidate_id in draft.candidate_ids:
            values = by_candidate.get(candidate_id, [])
            kinds = {item.kind for item in values}
            if CuratorOutcomeKind.CANDIDATE not in kinds:
                findings.append(f"candidate_phase_missing:{candidate_id}")
            if not kinds.intersection(
                {
                    CuratorOutcomeKind.ACCEPTED,
                    CuratorOutcomeKind.REJECTED,
                    CuratorOutcomeKind.MERGE_REQUIRED,
                    CuratorOutcomeKind.SUPERSEDED,
                    CuratorOutcomeKind.DISCARDED,
                    CuratorOutcomeKind.NOOP,
                }
            ):
                findings.append(f"decision_phase_missing:{candidate_id}")
            canonical = [item for item in values if item.canonical_memory_changed]
            for outcome in canonical:
                if not outcome.deterministic_validation:
                    findings.append(f"canonical_without_validation:{outcome.outcome_id}")
                if not outcome.commit_receipt_id:
                    findings.append(f"canonical_without_receipt:{outcome.outcome_id}")
                if not outcome.memory_id or outcome.memory_revision <= 0:
                    findings.append(f"canonical_without_memory_identity:{outcome.outcome_id}")
            index_values = [
                item for item in values if item.kind is CuratorOutcomeKind.INDEX_PUBLISHED
            ]
            for index_outcome in index_values:
                if not index_outcome.index_published:
                    findings.append(
                        f"index_outcome_without_publication:{index_outcome.outcome_id}"
                    )
                if not canonical:
                    findings.append(
                        f"index_publication_without_commit:{index_outcome.outcome_id}"
                    )
        for failure in draft.failures:
            if failure.canonical_memory_changed:
                findings.append(f"failure_claims_memory_change:{failure.failure_id}")
        canonical_memory_ids = tuple(
            dict.fromkeys(
                item.memory_id
                for item in draft.outcomes
                if item.canonical_memory_changed and item.memory_id
            )
        )
        index_memory_ids = tuple(
            dict.fromkeys(
                item.memory_id
                for item in draft.outcomes
                if item.index_published and item.memory_id
            )
        )
        if set(index_memory_ids) - set(canonical_memory_ids):
            findings.append("index_publication_exceeds_canonical_commits")
        coverage = {
            candidate_id: tuple(item.kind.value for item in values)
            for candidate_id, values in sorted(by_candidate.items())
        }
        return CuratorProjectionAudit(
            ok=not findings,
            task_id=draft.result.task_id,
            job_id=draft.result.job_id,
            findings=tuple(findings),
            phase_counts=dict(sorted(phase_counts.items())),
            candidate_coverage=coverage,
            canonical_memory_ids=canonical_memory_ids,
            index_published_memory_ids=index_memory_ids,
            deterministic_decision_ids=tuple(draft.decision_ids),
            receipt_ids=tuple(draft.receipt_ids),
            evidence_bundle_ids=tuple(
                dict.fromkeys(item.evidence_bundle_id for item in draft.outcomes)
            ),
            consumer_counts=dict(sorted(consumer_counts.items())),
        )


__all__ = [
    "CuratorOutcomeProjectionError",
    "CuratorOutcomeProjector",
    "CuratorProjectionAudit",
    "CuratorProjectionAuditor",
    "CuratorProjectionDraft",
]
