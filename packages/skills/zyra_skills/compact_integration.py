from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .attachments import estimate_tokens
from .compact_bridge import RestoredSkillContext, SkillCompactReference, compact_reference_from_dict
from .digests import digest_object
from .integration_errors import (
    SkillRestoreBudgetExceeded,
    SkillRestorePolicyRejected,
    SkillRestoreReferenceRejected,
    SkillSessionOwnershipError,
)
from .models import SkillInvocationStatus, utc_now


class SkillCompactItemKind(StrEnum):
    INLINE_CONTEXT = "inline_context"
    FORK_HANDOFF = "fork_handoff"
    TERMINAL_OUTCOME = "terminal_outcome"
    REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class SkillCompactRestoreItem:
    kind: SkillCompactItemKind
    invocation_id: str
    skill_ref: str
    status: str
    token_estimate: int
    body: str = ""
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    outcome_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    fork_request_ref: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind is not SkillCompactItemKind.INLINE_CONTEXT and self.body:
            raise ValueError("only inline compact restore items may contain body")
        if self.kind is SkillCompactItemKind.INLINE_CONTEXT and not self.policy_snapshot:
            raise ValueError("inline compact restore item requires policy snapshot")

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        value = {
            "kind": str(self.kind),
            "invocation_id": self.invocation_id,
            "skill_ref": self.skill_ref,
            "status": self.status,
            "token_estimate": self.token_estimate,
            "policy_snapshot": copy.deepcopy(self.policy_snapshot),
            "outcome_refs": list(self.outcome_refs),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
            "fork_request_ref": self.fork_request_ref,
            "metadata": copy.deepcopy(self.metadata),
            "body_in_checkpoint": False,
        }
        if include_body:
            value["body"] = self.body
        return value


@dataclass(frozen=True, slots=True)
class SkillCompactRestorePlan:
    session_id: str
    agent_id: str
    items: tuple[SkillCompactRestoreItem, ...]
    requested_count: int
    restored_inline_count: int
    fork_handoff_count: int
    terminal_outcome_count: int
    rejected_count: int
    consumed_tokens: int
    token_budget: int
    plan_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self, *, include_body: bool = False) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "items": [item.to_dict(include_body=include_body) for item in self.items],
            "requested_count": self.requested_count,
            "restored_inline_count": self.restored_inline_count,
            "fork_handoff_count": self.fork_handoff_count,
            "terminal_outcome_count": self.terminal_outcome_count,
            "rejected_count": self.rejected_count,
            "consumed_tokens": self.consumed_tokens,
            "token_budget": self.token_budget,
            "plan_digest": self.plan_digest,
            "created_at": self.created_at,
            "fork_body_restored": False,
            "terminal_body_restored": False,
        }


class SkillCompactIntegrationRuntime:
    """Status-aware compact restore split for 02D and 06C handoff."""

    def __init__(
        self,
        *,
        runtime: Any,
        total_token_budget: int = 25_000,
        per_skill_token_budget: int = 5_000,
    ) -> None:
        self.runtime = runtime
        self.total_token_budget = total_token_budget
        self.per_skill_token_budget = per_skill_token_budget

    def plan(
        self,
        references: Sequence[Mapping[str, Any] | SkillCompactReference],
        *,
        session_id: str,
        agent_id: str,
        fork_handoffs: Mapping[str, Mapping[str, Any]] | None = None,
        outcome_projections: Mapping[str, Mapping[str, Any]] | None = None,
        strict: bool = True,
    ) -> SkillCompactRestorePlan:
        fork_handoffs = dict(fork_handoffs or {})
        outcome_projections = dict(outcome_projections or {})
        remaining = self.total_token_budget
        items: list[SkillCompactRestoreItem] = []
        rejected = 0
        for raw in references:
            reference = raw if isinstance(raw, SkillCompactReference) else compact_reference_from_dict(dict(raw))
            if reference.session_id != session_id or reference.agent_id != agent_id:
                if strict:
                    raise SkillSessionOwnershipError("skill compact reference crossed session/agent scope")
                rejected += 1
                continue
            try:
                item = self._restore_one(
                    reference,
                    session_id=session_id,
                    agent_id=agent_id,
                    remaining_tokens=remaining,
                    fork_handoff=fork_handoffs.get(reference.invocation_id),
                    outcome_projection=outcome_projections.get(reference.invocation_id),
                )
            except Exception:
                if strict:
                    raise
                rejected += 1
                continue
            items.append(item)
            remaining -= item.token_estimate
            if remaining < 0:
                raise SkillRestoreBudgetExceeded("skill compact restore exceeded total budget")
        payload = {
            "session_id": session_id,
            "agent_id": agent_id,
            "items": [item.to_dict(include_body=True) for item in items],
            "requested_count": len(references),
            "rejected_count": rejected,
            "token_budget": self.total_token_budget,
        }
        return SkillCompactRestorePlan(
            session_id=session_id,
            agent_id=agent_id,
            items=tuple(items),
            requested_count=len(references),
            restored_inline_count=sum(item.kind is SkillCompactItemKind.INLINE_CONTEXT for item in items),
            fork_handoff_count=sum(item.kind is SkillCompactItemKind.FORK_HANDOFF for item in items),
            terminal_outcome_count=sum(item.kind is SkillCompactItemKind.TERMINAL_OUTCOME for item in items),
            rejected_count=rejected,
            consumed_tokens=self.total_token_budget - remaining,
            token_budget=self.total_token_budget,
            plan_digest=digest_object(payload),
        )

    def _restore_one(
        self,
        reference: SkillCompactReference,
        *,
        session_id: str,
        agent_id: str,
        remaining_tokens: int,
        fork_handoff: Mapping[str, Any] | None,
        outcome_projection: Mapping[str, Any] | None,
    ) -> SkillCompactRestoreItem:
        status = reference.status
        if status is SkillInvocationStatus.FORK_PENDING:
            if fork_handoff is None:
                raise SkillRestoreReferenceRejected("forked skill compact restore requires 03D handoff ref")
            return SkillCompactRestoreItem(
                kind=SkillCompactItemKind.FORK_HANDOFF,
                invocation_id=reference.invocation_id,
                skill_ref=reference.version_ref.immutable_ref,
                status=str(status),
                token_estimate=max(1, estimate_tokens(str(fork_handoff))),
                fork_request_ref=str(
                    fork_handoff.get("execution_ref")
                    or fork_handoff.get("fork_request_id")
                    or ""
                ),
                metadata={"handoff": copy.deepcopy(dict(fork_handoff)), "parent_body_disclosed": False},
            )
        if status.terminal:
            projection = dict(outcome_projection or {})
            if not projection:
                projection = {
                    "invocation_id": reference.invocation_id,
                    "version_ref": reference.version_ref.to_dict(),
                    "outcome_refs": list(reference.outcome_refs),
                    "evidence_refs": list(reference.evidence_refs),
                    "artifact_refs": list(reference.artifact_refs),
                    "status": str(status),
                }
            return SkillCompactRestoreItem(
                kind=SkillCompactItemKind.TERMINAL_OUTCOME,
                invocation_id=reference.invocation_id,
                skill_ref=reference.version_ref.immutable_ref,
                status=str(status),
                token_estimate=max(1, estimate_tokens(str(projection))),
                outcome_refs=reference.outcome_refs,
                evidence_refs=reference.evidence_refs,
                artifact_refs=reference.artifact_refs,
                metadata={"outcome_projection": projection, "body_disclosed": False},
            )
        if status is not SkillInvocationStatus.INLINE_ACTIVE:
            raise SkillRestoreReferenceRejected(
                "skill compact reference status is not restorable",
                detail={"status": str(status)},
            )
        budget = min(remaining_tokens, self.per_skill_token_budget)
        if budget <= 0:
            raise SkillRestoreBudgetExceeded("skill compact restore has no remaining token budget")
        restored = self.runtime.compact_bridge.restore(
            (reference,),
            session_id=session_id,
            agent_id=agent_id,
        )
        if len(restored) != 1:
            raise SkillRestoreReferenceRejected("exact skill compact restore returned no context")
        item: RestoredSkillContext = restored[0]
        tokens = min(budget, max(1, item.body.token_estimate))
        if item.restored_policy.policy_digest != reference.policy_snapshot.policy_digest:
            raise SkillRestorePolicyRejected("restored skill policy digest diverged")
        return SkillCompactRestoreItem(
            kind=SkillCompactItemKind.INLINE_CONTEXT,
            invocation_id=reference.invocation_id,
            skill_ref=reference.version_ref.immutable_ref,
            status=str(status),
            token_estimate=tokens,
            body=item.body.text,
            policy_snapshot=item.restored_policy.to_dict(),
            metadata={
                "source_kind": item.reference.source_kind,
                "trust_tier": item.reference.trust_tier,
                "permission_authority": False,
            },
        )
