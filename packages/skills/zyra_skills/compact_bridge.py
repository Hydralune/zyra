from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Sequence

from .body_loader import SkillBodyResourceLoader
from .errors import SkillCompactRestoreError, SkillRevoked
from .models import (
    InvokedSkillState,
    SkillBody,
    SkillInvocationStatus,
    SkillOutcomeProjection,
    SkillPolicySnapshot,
    SkillVersionRef,
    utc_now,
)
from .policy import SkillAllowedToolsPolicy
from .registry import SkillRegistry


@dataclass(frozen=True, slots=True)
class SkillCompactReference:
    invocation_id: str
    session_id: str
    agent_id: str
    version_ref: SkillVersionRef
    policy_snapshot: SkillPolicySnapshot
    source_kind: str = ""
    trust_tier: str = ""
    invocation_status: str = ""
    outcome_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    invoked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation_id": self.invocation_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "version_ref": self.version_ref.to_dict(),
            "policy_snapshot": self.policy_snapshot.to_dict(),
            "source_kind": self.source_kind,
            "trust_tier": self.trust_tier,
            "invocation_status": self.invocation_status,
            "outcome_refs": list(self.outcome_refs),
            "evidence_refs": list(self.evidence_refs),
            "artifact_refs": list(self.artifact_refs),
            "invoked_at": self.invoked_at,
        }


@dataclass(frozen=True, slots=True)
class RestoredSkillContext:
    reference: SkillCompactReference
    body: SkillBody
    restored_policy: SkillPolicySnapshot
    restored_at: str = field(default_factory=utc_now)

    def to_dict(self, *, include_body: bool = True) -> dict[str, Any]:
        return {
            "reference": self.reference.to_dict(),
            "body": self.body.to_dict(include_text=include_body),
            "restored_policy": self.restored_policy.to_dict(),
            "restored_at": self.restored_at,
        }


class SkillCompactBridge:
    """02D read-only exact-revision bridge; 06C consumes projections only."""

    def __init__(
        self,
        *,
        registry: SkillRegistry,
        body_loader: SkillBodyResourceLoader,
        allowed_tools_policy: SkillAllowedToolsPolicy,
        per_skill_token_budget: int = 5_000,
        total_token_budget: int = 25_000,
    ) -> None:
        self.registry = registry
        self.body_loader = body_loader
        self.allowed_tools_policy = allowed_tools_policy
        self.per_skill_token_budget = per_skill_token_budget
        self.total_token_budget = total_token_budget

    def references(
        self,
        states: Iterable[InvokedSkillState],
        *,
        session_id: str,
        agent_id: str,
    ) -> tuple[SkillCompactReference, ...]:
        selected = [
            state
            for state in states
            if state.session_id == session_id
            and state.agent_id == agent_id
            and state.policy_snapshot is not None
            and state.status
            in {
                SkillInvocationStatus.INLINE_ACTIVE,
                SkillInvocationStatus.FORK_PENDING,
                SkillInvocationStatus.COMPLETED,
            }
        ]
        selected.sort(key=lambda item: item.updated_at, reverse=True)
        references: list[SkillCompactReference] = []
        for state in selected:
            revision = self.registry.resolve(
                state.version_ref.qualified_name,
                requested_ref=state.version_ref,
            )
            references.append(SkillCompactReference(
                invocation_id=state.invocation_id,
                session_id=state.session_id,
                agent_id=state.agent_id,
                version_ref=state.version_ref,
                policy_snapshot=state.policy_snapshot,
                source_kind=str(revision.provenance.source_kind),
                trust_tier=str(revision.provenance.trust_tier),
                invocation_status=str(state.status),
                outcome_refs=state.outcome_refs,
                evidence_refs=state.evidence_refs,
                artifact_refs=state.artifact_refs,
                invoked_at=state.requested_at,
            ))
        return tuple(references)

    def restore(
        self,
        references: Sequence[SkillCompactReference],
        *,
        session_id: str,
        agent_id: str,
        parent_policy_snapshot_ids: Sequence[str] = (),
    ) -> tuple[RestoredSkillContext, ...]:
        remaining = self.total_token_budget
        restored: list[RestoredSkillContext] = []
        for reference in references:
            if reference.session_id != session_id or reference.agent_id != agent_id:
                raise SkillCompactRestoreError("skill compact reference scope mismatch")
            try:
                revision = self.registry.resolve(
                    reference.version_ref.qualified_name,
                    requested_ref=reference.version_ref,
                )
            except SkillRevoked:
                raise
            except Exception as error:  # noqa: BLE001 - wrap exact restore failures.
                raise SkillCompactRestoreError(
                    "unable to resolve immutable skill revision during compact restore",
                    detail={"ref": reference.version_ref.immutable_ref, "error": str(error)},
                ) from error
            if revision.version_ref.content_digest != reference.version_ref.content_digest:
                raise SkillCompactRestoreError("skill digest mismatch during compact restore")
            # Trust is derived from the resolved immutable revision. Serialized
            # compact input is not allowed to self-assert a product trust tier.
            canonical_reference = replace(
                reference,
                source_kind=str(revision.provenance.source_kind),
                trust_tier=str(revision.provenance.trust_tier),
            )
            budget = min(
                remaining,
                self.per_skill_token_budget,
                revision.metadata.context_budget.restore_tokens,
            )
            if budget <= 0:
                break
            body = self.body_loader.restore_excerpt(reference.version_ref, token_budget=budget)
            policy = self.allowed_tools_policy.restore_snapshot(
                reference.policy_snapshot,
                current_revision=revision,
                parent_snapshot_ids=parent_policy_snapshot_ids,
                activate=reference.invocation_status in {
                    str(SkillInvocationStatus.INLINE_ACTIVE),
                    str(SkillInvocationStatus.FORK_PENDING),
                },
            )
            restored.append(
                RestoredSkillContext(
                    reference=canonical_reference,
                    body=body,
                    restored_policy=policy,
                )
            )
            remaining -= body.token_estimate
        return tuple(restored)

    def memory_projection(self, state: InvokedSkillState) -> SkillOutcomeProjection:
        if state.policy_snapshot is None:
            raise SkillCompactRestoreError("skill outcome projection requires a policy snapshot")
        try:
            status = self.registry.revision_store.status(state.version_ref)
            stale = status.revocation_epoch != state.version_ref.revocation_epoch or str(status.lifecycle) in {"revoked", "superseded"}
        except Exception:
            stale = True
        return SkillOutcomeProjection(
            invocation_id=state.invocation_id,
            session_id=state.session_id,
            agent_id=state.agent_id,
            version_ref=state.version_ref,
            policy_snapshot_digest=state.policy_snapshot.policy_digest,
            revocation_epoch=state.version_ref.revocation_epoch,
            status=state.status,
            outcome_refs=state.outcome_refs,
            evidence_refs=state.evidence_refs,
            artifact_refs=state.artifact_refs,
            stale=stale,
            completed_at=state.completed_at,
        )


def compact_reference_from_dict(value: dict[str, Any]) -> SkillCompactReference:
    version_raw = value.get("version_ref")
    policy_raw = value.get("policy_snapshot")
    if not isinstance(version_raw, dict) or not isinstance(policy_raw, dict):
        raise SkillCompactRestoreError("structured skill compact reference requires version_ref and policy_snapshot")
    tools_raw = policy_raw.get("effective_tools")
    effective_tools = None
    if tools_raw is not None:
        if not isinstance(tools_raw, list):
            raise SkillCompactRestoreError("policy effective_tools must be a list or null")
        from .models import ToolSelector

        effective_tools = tuple(ToolSelector.parse(item) for item in tools_raw)
    version_ref = SkillVersionRef.from_dict(version_raw)
    policy = SkillPolicySnapshot(
        snapshot_id=str(policy_raw.get("snapshot_id") or ""),
        invocation_id=str(policy_raw.get("invocation_id") or value.get("invocation_id") or ""),
        session_id=str(policy_raw.get("session_id") or value.get("session_id") or ""),
        version_ref=SkillVersionRef.from_dict(dict(policy_raw.get("version_ref") or version_raw)),
        effective_tools=effective_tools,
        parent_snapshot_ids=tuple(str(item) for item in policy_raw.get("parent_snapshot_ids") or ()),
        policy_digest=str(policy_raw.get("policy_digest") or ""),
        created_at=str(policy_raw.get("created_at") or ""),
    )
    return SkillCompactReference(
        invocation_id=str(value.get("invocation_id") or ""),
        session_id=str(value.get("session_id") or ""),
        agent_id=str(value.get("agent_id") or ""),
        version_ref=version_ref,
        policy_snapshot=policy,
        source_kind=str(value.get("source_kind") or ""),
        trust_tier=str(value.get("trust_tier") or ""),
        invocation_status=str(value.get("invocation_status") or ""),
        outcome_refs=tuple(str(item) for item in value.get("outcome_refs") or ()),
        evidence_refs=tuple(str(item) for item in value.get("evidence_refs") or ()),
        artifact_refs=tuple(str(item) for item in value.get("artifact_refs") or ()),
        invoked_at=str(value.get("invoked_at") or ""),
    )
