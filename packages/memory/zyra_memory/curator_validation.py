from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .curator_evidence import EvidenceResolver, SecretRedactor
from .curator_models import (
    CandidateKind,
    CandidateRelationKind,
    CandidateState,
    DecisionCode,
    DecisionIssue,
    DecisionStatus,
    EvidenceDocument,
    EvidenceKind,
    MemoryCandidate,
    MemoryDecision,
    MemoryScope,
    TrustTier,
    canonical_json,
    stable_digest,
    unique_strings,
)
from .curator_store import CuratorCandidateStore
from .models import MemoryLayer, MemoryRecord


ALLOWED_LAYERS = {item.value for item in MemoryLayer}


FORBIDDEN_RULE_KEYS = {
    "allowed_tools",
    "required_tools",
    "forbidden",
    "permission",
    "permissions",
    "policy",
    "policies",
    "rules",
    "state_transition_rules",
    "trust",
    "trust_tier",
    "validator",
    "system_prompt",
    "developer_message",
    "retention_policy",
    "scope_policy",
    "canonical_owner",
}


PROMPT_MUTATION_PATTERN = re.compile(
    r"(?i)(?:ignore|override|replace|disable|bypass)\s+(?:the\s+)?"
    r"(?:system|developer|permission|policy|validator|guard|rule)"
)


NEGATION_PATTERN = re.compile(
    r"(?i)\b(?:not|never|no longer|false|disabled|forbidden|failed|invalid|removed)\b"
)


class ValidationPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ScopeRule:
    scope: MemoryScope
    allowed_layers: tuple[str, ...]
    minimum_confidence: float
    minimum_verified_refs: int
    minimum_total_refs: int
    allowed_trust: tuple[TrustTier, ...]
    maximum_ttl_seconds: int | None
    model_proposals_allowed: bool

    def validated(self) -> ScopeRule:
        if not self.allowed_layers:
            raise ValidationPolicyError("scope rule must allow at least one memory layer")
        if any(layer not in ALLOWED_LAYERS for layer in self.allowed_layers):
            raise ValidationPolicyError("scope rule includes an unknown memory layer")
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ValidationPolicyError("scope minimum confidence is invalid")
        if self.minimum_verified_refs < 0 or self.minimum_total_refs <= 0:
            raise ValidationPolicyError("scope evidence cardinality is invalid")
        if not self.allowed_trust:
            raise ValidationPolicyError("scope rule must allow at least one trust tier")
        if self.maximum_ttl_seconds is not None and self.maximum_ttl_seconds <= 0:
            raise ValidationPolicyError("scope maximum ttl must be positive")
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.value,
            "allowed_layers": list(self.allowed_layers),
            "minimum_confidence": self.minimum_confidence,
            "minimum_verified_refs": self.minimum_verified_refs,
            "minimum_total_refs": self.minimum_total_refs,
            "allowed_trust": [item.value for item in self.allowed_trust],
            "maximum_ttl_seconds": self.maximum_ttl_seconds,
            "model_proposals_allowed": self.model_proposals_allowed,
        }


def default_scope_rules() -> tuple[ScopeRule, ...]:
    verified = (TrustTier.SYSTEM, TrustTier.VERIFIED_RUNTIME, TrustTier.USER, TrustTier.TOOL)
    return (
        ScopeRule(
            scope=MemoryScope.TASK,
            allowed_layers=("working", "episodic", "semantic", "skill"),
            minimum_confidence=0.55,
            minimum_verified_refs=0,
            minimum_total_refs=1,
            allowed_trust=verified + (TrustTier.EXTERNAL, TrustTier.MODEL_PROPOSAL, TrustTier.UNKNOWN),
            maximum_ttl_seconds=90 * 24 * 60 * 60,
            model_proposals_allowed=True,
        ).validated(),
        ScopeRule(
            scope=MemoryScope.RUN,
            allowed_layers=("episodic", "semantic", "skill"),
            minimum_confidence=0.65,
            minimum_verified_refs=1,
            minimum_total_refs=1,
            allowed_trust=verified,
            maximum_ttl_seconds=180 * 24 * 60 * 60,
            model_proposals_allowed=True,
        ).validated(),
        ScopeRule(
            scope=MemoryScope.PROJECT,
            allowed_layers=("episodic", "semantic", "skill"),
            minimum_confidence=0.72,
            minimum_verified_refs=1,
            minimum_total_refs=2,
            allowed_trust=verified,
            maximum_ttl_seconds=365 * 24 * 60 * 60,
            model_proposals_allowed=True,
        ).validated(),
        ScopeRule(
            scope=MemoryScope.GLOBAL,
            allowed_layers=("semantic", "skill"),
            minimum_confidence=0.9,
            minimum_verified_refs=2,
            minimum_total_refs=3,
            allowed_trust=(TrustTier.SYSTEM, TrustTier.VERIFIED_RUNTIME, TrustTier.USER),
            maximum_ttl_seconds=365 * 24 * 60 * 60,
            model_proposals_allowed=False,
        ).validated(),
    )


@dataclass(frozen=True, slots=True)
class CuratorValidationPolicy:
    policy_id: str = "zyra-curator-policy-v1"
    scope_rules: tuple[ScopeRule, ...] = field(default_factory=default_scope_rules)
    reject_secret_evidence: bool = True
    reject_rule_mutation: bool = True
    reject_unknown_provenance_above_task: bool = True
    maximum_summary_chars: int = 2000
    maximum_content_chars: int = 32_000
    maximum_keyword_count: int = 64
    maximum_artifact_count: int = 128
    maximum_evidence_count: int = 1000
    default_task_ttl_seconds: int = 30 * 24 * 60 * 60
    default_run_ttl_seconds: int = 90 * 24 * 60 * 60
    default_project_ttl_seconds: int = 365 * 24 * 60 * 60
    require_explicit_global_ttl: bool = True
    preserve_failure_patterns: bool = True
    preserve_skill_candidates: bool = True
    duplicate_summary_threshold: float = 0.9

    def validated(self) -> CuratorValidationPolicy:
        if not self.policy_id.strip():
            raise ValidationPolicyError("policy_id is required")
        by_scope = {rule.scope: rule.validated() for rule in self.scope_rules}
        if set(by_scope) != set(MemoryScope):
            raise ValidationPolicyError("policy must define every memory scope")
        if self.maximum_summary_chars < 64 or self.maximum_content_chars < 256:
            raise ValidationPolicyError("policy content bounds are too small")
        if self.maximum_keyword_count <= 0 or self.maximum_artifact_count <= 0:
            raise ValidationPolicyError("policy collection bounds must be positive")
        if self.maximum_evidence_count <= 0:
            raise ValidationPolicyError("policy evidence bound must be positive")
        for value in (
            self.default_task_ttl_seconds,
            self.default_run_ttl_seconds,
            self.default_project_ttl_seconds,
        ):
            if value <= 0:
                raise ValidationPolicyError("default ttl values must be positive")
        if not 0.0 <= self.duplicate_summary_threshold <= 1.0:
            raise ValidationPolicyError("duplicate summary threshold is invalid")
        return self

    @property
    def digest(self) -> str:
        return stable_digest(self.to_dict())

    def rule(self, scope: MemoryScope) -> ScopeRule:
        for rule in self.scope_rules:
            if rule.scope is scope:
                return rule
        raise ValidationPolicyError(f"scope rule missing: {scope.value}")

    def default_ttl(self, scope: MemoryScope) -> int | None:
        if scope is MemoryScope.TASK:
            return self.default_task_ttl_seconds
        if scope is MemoryScope.RUN:
            return self.default_run_ttl_seconds
        if scope is MemoryScope.PROJECT:
            return self.default_project_ttl_seconds
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "scope_rules": [rule.to_dict() for rule in self.scope_rules],
            "reject_secret_evidence": self.reject_secret_evidence,
            "reject_rule_mutation": self.reject_rule_mutation,
            "reject_unknown_provenance_above_task": self.reject_unknown_provenance_above_task,
            "maximum_summary_chars": self.maximum_summary_chars,
            "maximum_content_chars": self.maximum_content_chars,
            "maximum_keyword_count": self.maximum_keyword_count,
            "maximum_artifact_count": self.maximum_artifact_count,
            "maximum_evidence_count": self.maximum_evidence_count,
            "default_task_ttl_seconds": self.default_task_ttl_seconds,
            "default_run_ttl_seconds": self.default_run_ttl_seconds,
            "default_project_ttl_seconds": self.default_project_ttl_seconds,
            "require_explicit_global_ttl": self.require_explicit_global_ttl,
            "preserve_failure_patterns": self.preserve_failure_patterns,
            "preserve_skill_candidates": self.preserve_skill_candidates,
            "duplicate_summary_threshold": self.duplicate_summary_threshold,
        }


@dataclass(frozen=True, slots=True)
class ValidationContext:
    candidate: MemoryCandidate
    documents: tuple[EvidenceDocument, ...]
    canonical_records: tuple[MemoryRecord, ...]
    relations: tuple[Any, ...]
    current_revision: int
    policy: CuratorValidationPolicy

    @property
    def trust_counts(self) -> Mapping[str, int]:
        return dict(sorted(Counter(item.ref.trust.value for item in self.documents).items()))

    @property
    def verified_count(self) -> int:
        verified = {TrustTier.SYSTEM, TrustTier.VERIFIED_RUNTIME, TrustTier.USER, TrustTier.TOOL}
        return sum(1 for item in self.documents if item.ref.trust in verified)

    @property
    def secret_fingerprint_count(self) -> int:
        return sum(len(item.secret_fingerprints) for item in self.documents)


@dataclass(frozen=True, slots=True)
class DuplicateFinding:
    memory_id: str
    reason: str
    score: float
    exact: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "reason": self.reason,
            "score": self.score,
            "exact": self.exact,
        }


@dataclass(frozen=True, slots=True)
class ContradictionFinding:
    memory_id: str
    subject: str
    conflicting_keys: tuple[str, ...]
    existing_values: Mapping[str, str]
    proposed_values: Mapping[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "memory_id": self.memory_id,
            "subject": self.subject,
            "conflicting_keys": list(self.conflicting_keys),
            "existing_values": dict(self.existing_values),
            "proposed_values": dict(self.proposed_values),
        }


class MemoryDuplicateDetector:
    def find(
        self,
        candidate: MemoryCandidate,
        records: Sequence[MemoryRecord],
        *,
        threshold: float,
    ) -> DuplicateFinding | None:
        for record in records:
            metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
            if str(metadata.get("curator_candidate_digest") or "") == candidate.semantic_digest:
                return DuplicateFinding(
                    memory_id=record.memory_id,
                    reason="canonical record has the same candidate digest",
                    score=1.0,
                    exact=True,
                )
            if str(metadata.get("curator_subject") or "") != candidate.subject:
                continue
            content_digest = stable_digest(record.content)
            if content_digest == stable_digest(candidate.content):
                return DuplicateFinding(
                    memory_id=record.memory_id,
                    reason="same subject and content digest",
                    score=1.0,
                    exact=True,
                )
            similarity = self._token_similarity(record.summary, candidate.summary)
            if similarity >= threshold:
                return DuplicateFinding(
                    memory_id=record.memory_id,
                    reason="same subject and near-identical summary",
                    score=similarity,
                    exact=False,
                )
        return None

    @staticmethod
    def _token_similarity(left: str, right: str) -> float:
        left_tokens = MemoryDuplicateDetector._tokens(left)
        right_tokens = MemoryDuplicateDetector._tokens(right)
        if not left_tokens or not right_tokens:
            return 0.0
        intersection = len(left_tokens & right_tokens)
        union = len(left_tokens | right_tokens)
        return intersection / max(1, union)

    @staticmethod
    def _tokens(value: str) -> set[str]:
        return {
            token.casefold()
            for token in re.findall(r"[A-Za-z0-9_./:-]{2,}", str(value))
            if len(token) > 2
        }


class MemoryContradictionDetector:
    def find(
        self,
        candidate: MemoryCandidate,
        records: Sequence[MemoryRecord],
    ) -> ContradictionFinding | None:
        proposed = self._facts(candidate.content, candidate.summary)
        if not proposed:
            return None
        for record in records:
            metadata = record.metadata if isinstance(record.metadata, Mapping) else {}
            if str(metadata.get("curator_subject") or "") != candidate.subject:
                continue
            existing = self._facts(record.content, record.summary)
            common = sorted(set(existing).intersection(proposed))
            conflicting = tuple(key for key in common if existing[key] != proposed[key])
            if conflicting:
                return ContradictionFinding(
                    memory_id=record.memory_id,
                    subject=candidate.subject,
                    conflicting_keys=conflicting,
                    existing_values={key: existing[key] for key in conflicting},
                    proposed_values={key: proposed[key] for key in conflicting},
                )
            if self._opposed_summary(record.summary, candidate.summary):
                return ContradictionFinding(
                    memory_id=record.memory_id,
                    subject=candidate.subject,
                    conflicting_keys=("summary_polarity",),
                    existing_values={"summary_polarity": self._polarity(record.summary)},
                    proposed_values={"summary_polarity": self._polarity(candidate.summary)},
                )
        return None

    @staticmethod
    def _facts(content: Mapping[str, Any], summary: str) -> dict[str, str]:
        output: dict[str, str] = {}
        for key in (
            "value",
            "status",
            "outcome",
            "decision",
            "requirement",
            "fact",
            "signature",
            "enabled",
            "available",
        ):
            value = content.get(key)
            if isinstance(value, (str, bool, int, float)):
                output[key] = str(value).strip().casefold()
        if summary:
            output["summary_polarity"] = MemoryContradictionDetector._polarity(summary)
        return output

    @staticmethod
    def _polarity(value: str) -> str:
        return "negative" if NEGATION_PATTERN.search(value) else "positive"

    @staticmethod
    def _opposed_summary(left: str, right: str) -> bool:
        left_tokens = MemoryDuplicateDetector._tokens(left)
        right_tokens = MemoryDuplicateDetector._tokens(right)
        if not left_tokens or not right_tokens:
            return False
        overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
        return overlap >= 0.65 and MemoryContradictionDetector._polarity(left) != MemoryContradictionDetector._polarity(right)


class MemoryDecisionValidator:
    """Deterministic admission gate; the only authority before canonical commit."""

    def __init__(
        self,
        *,
        candidate_store: CuratorCandidateStore,
        canonical_store: Any,
        policy: CuratorValidationPolicy | None = None,
        evidence_resolver: EvidenceResolver | None = None,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self.candidate_store = candidate_store
        self.canonical_store = canonical_store
        self.policy = (policy or CuratorValidationPolicy()).validated()
        self.evidence_resolver = evidence_resolver or EvidenceResolver(candidate_store)
        self.redactor = redactor or SecretRedactor()
        self.duplicates = MemoryDuplicateDetector()
        self.contradictions = MemoryContradictionDetector()

    def validate(self, candidate: MemoryCandidate) -> MemoryDecision:
        value = candidate.validated()
        stored = self.candidate_store.require_candidate(value.candidate_id)
        if stored.semantic_digest != value.semantic_digest:
            return self._decision(
                value,
                status=DecisionStatus.REJECT,
                issues=(self._issue(DecisionCode.SCHEMA_INVALID, "stored candidate digest changed"),),
            )
        for prior in reversed(self.candidate_store.candidate_decisions(value.candidate_id)):
            if (
                prior.candidate_digest == value.semantic_digest
                and prior.evidence_digest == value.evidence_digest
                and prior.policy_digest == self.policy.digest
            ):
                # A committed memory can change duplicate/revision observations. Reusing the
                # immutable decision for identical inputs is therefore required for exact crash
                # resume; recomputing here would turn a successful decision into a duplicate.
                return prior
        bundle, documents, evidence_issues = self.evidence_resolver.resolve_candidate(
            candidate_id=value.candidate_id
        )
        if bundle is None or evidence_issues:
            issues = tuple(self._evidence_issue(item) for item in evidence_issues or ("bundle_missing",))
            decision = self._decision(value, status=DecisionStatus.REJECT, issues=issues)
            return self._persist(decision)
        canonical_records = tuple(self.canonical_store.task_memory_records(value.task_id))
        relations = self.candidate_store.relations(value.candidate_id)
        current_revision = (
            self.candidate_store.memory_revision(value.expected_memory_id)
            if value.expected_memory_id
            else 0
        )
        context = ValidationContext(
            candidate=value,
            documents=documents,
            canonical_records=canonical_records,
            relations=relations,
            current_revision=current_revision,
            policy=self.policy,
        )
        issues: list[DecisionIssue] = []
        issues.extend(self._schema_checks(context))
        issues.extend(self._scope_checks(context))
        issues.extend(self._evidence_checks(context))
        issues.extend(self._provenance_checks(context))
        issues.extend(self._secret_checks(context))
        issues.extend(self._mutation_checks(context))
        issues.extend(self._ttl_checks(context))
        issues.extend(self._revision_checks(context))
        relation_status, relation_issue, relation_id = self._relation_check(context)
        if relation_issue is not None:
            issues.append(relation_issue)
        duplicate = self.duplicates.find(
            value,
            canonical_records,
            threshold=self.policy.duplicate_summary_threshold,
        )
        if duplicate is not None:
            issues.append(
                self._issue(
                    DecisionCode.DUPLICATE,
                    duplicate.reason,
                    metadata=duplicate.to_dict(),
                )
            )
        contradiction = self.contradictions.find(value, canonical_records)
        if contradiction is not None:
            issues.append(
                self._issue(
                    DecisionCode.CONTRADICTION,
                    "candidate contradicts canonical memory and requires explicit merge",
                    metadata=contradiction.to_dict(),
                )
            )
        sanitized_summary, sanitized_content = self._sanitize_candidate(value)
        status = self._status(
            value,
            issues,
            relation_status=relation_status,
            duplicate=duplicate,
            contradiction=contradiction,
        )
        target_memory_id = value.expected_memory_id
        target_revision = value.expected_revision
        if duplicate is not None:
            target_memory_id = duplicate.memory_id
            target_revision = self.candidate_store.memory_revision(duplicate.memory_id)
        if contradiction is not None:
            target_memory_id = contradiction.memory_id
            target_revision = self.candidate_store.memory_revision(contradiction.memory_id)
        decision = MemoryDecision.build(
            candidate=value,
            status=status,
            policy_digest=self.policy.digest,
            validated_summary=sanitized_summary,
            validated_content=sanitized_content,
            issues=tuple(issues),
            target_memory_id=target_memory_id,
            target_revision=target_revision,
            relation_id=relation_id,
            metadata={
                "policy_id": self.policy.policy_id,
                "trust_counts": context.trust_counts,
                "verified_evidence_count": context.verified_count,
                "secret_fingerprint_count": context.secret_fingerprint_count,
                "evidence_count": len(context.documents),
                "canonical_record_count": len(context.canonical_records),
                "current_revision": context.current_revision,
                "duplicate": duplicate.to_dict() if duplicate else None,
                "contradiction": contradiction.to_dict() if contradiction else None,
            },
        )
        return self._persist(decision)

    def validate_many(self, candidates: Sequence[MemoryCandidate]) -> tuple[MemoryDecision, ...]:
        return tuple(self.validate(candidate) for candidate in candidates)

    def _schema_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        issues: list[DecisionIssue] = []
        if candidate.proposed_layer not in ALLOWED_LAYERS:
            issues.append(
                self._issue(
                    DecisionCode.SCHEMA_INVALID,
                    "candidate proposed_layer is unknown",
                    field="proposed_layer",
                )
            )
        if not candidate.summary.strip() and candidate.kind is not CandidateKind.DISCARD:
            issues.append(
                self._issue(
                    DecisionCode.EMPTY_CONTENT,
                    "candidate summary is empty",
                    field="summary",
                )
            )
        if not candidate.content and candidate.kind not in {CandidateKind.DISCARD, CandidateKind.COMPRESS}:
            issues.append(
                self._issue(
                    DecisionCode.EMPTY_CONTENT,
                    "candidate content is empty",
                    field="content",
                )
            )
        if len(candidate.summary) > context.policy.maximum_summary_chars:
            issues.append(
                self._issue(
                    DecisionCode.SCHEMA_INVALID,
                    "candidate summary exceeds admission bound",
                    field="summary",
                    metadata={"maximum": context.policy.maximum_summary_chars},
                )
            )
        content_chars = len(canonical_json(candidate.content))
        if content_chars > context.policy.maximum_content_chars:
            issues.append(
                self._issue(
                    DecisionCode.SCHEMA_INVALID,
                    "candidate content exceeds admission bound",
                    field="content",
                    metadata={
                        "maximum": context.policy.maximum_content_chars,
                        "actual": content_chars,
                    },
                )
            )
        if len(candidate.evidence_ids) > context.policy.maximum_evidence_count:
            issues.append(
                self._issue(
                    DecisionCode.SCHEMA_INVALID,
                    "candidate evidence count exceeds admission bound",
                    field="evidence_ids",
                )
            )
        if len(candidate.artifact_ids) > context.policy.maximum_artifact_count:
            issues.append(
                self._issue(
                    DecisionCode.SCHEMA_INVALID,
                    "candidate artifact count exceeds admission bound",
                    field="artifact_ids",
                )
            )
        return issues

    def _scope_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        rule = context.policy.rule(candidate.scope)
        issues: list[DecisionIssue] = []
        if candidate.proposed_layer not in rule.allowed_layers:
            issues.append(
                self._issue(
                    DecisionCode.SCOPE_INVALID,
                    "candidate layer is not allowed in proposed scope",
                    field="scope",
                    metadata={
                        "scope": candidate.scope.value,
                        "layer": candidate.proposed_layer,
                        "allowed_layers": list(rule.allowed_layers),
                    },
                )
            )
        if candidate.confidence < rule.minimum_confidence:
            issues.append(
                self._issue(
                    DecisionCode.LOW_CONFIDENCE,
                    "candidate confidence is below scope threshold",
                    field="confidence",
                    metadata={
                        "actual": candidate.confidence,
                        "minimum": rule.minimum_confidence,
                    },
                )
            )
        if candidate.model_assisted and not rule.model_proposals_allowed:
            issues.append(
                self._issue(
                    DecisionCode.TRUST_INSUFFICIENT,
                    "model-assisted proposal is forbidden for this scope",
                    field="scope",
                )
            )
        return issues

    def _evidence_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        rule = context.policy.rule(candidate.scope)
        issues: list[DecisionIssue] = []
        if len(context.documents) < rule.minimum_total_refs:
            issues.append(
                self._issue(
                    DecisionCode.EVIDENCE_MISSING,
                    "candidate does not have enough evidence refs for its scope",
                    field="evidence_ids",
                    metadata={
                        "actual": len(context.documents),
                        "minimum": rule.minimum_total_refs,
                    },
                )
            )
        if context.verified_count < rule.minimum_verified_refs:
            issues.append(
                self._issue(
                    DecisionCode.TRUST_INSUFFICIENT,
                    "candidate does not have enough verified evidence refs",
                    field="evidence_ids",
                    metadata={
                        "actual": context.verified_count,
                        "minimum": rule.minimum_verified_refs,
                    },
                )
            )
        for document in context.documents:
            if document.ref.run_id != candidate.run_id or document.ref.task_id != candidate.task_id:
                issues.append(
                    self._issue(
                        DecisionCode.EVIDENCE_FORGED,
                        "evidence belongs to another run or task",
                        evidence_id=document.ref.evidence_id,
                    )
                )
            if not candidate.evidence_range.contains(document.ref.sequence):
                issues.append(
                    self._issue(
                        DecisionCode.EVIDENCE_RANGE_INVALID,
                        "evidence falls outside candidate range",
                        evidence_id=document.ref.evidence_id,
                    )
                )
            if stable_digest(document.normalized) != document.ref.content_digest:
                issues.append(
                    self._issue(
                        DecisionCode.EVIDENCE_FORGED,
                        "evidence content digest does not match",
                        evidence_id=document.ref.evidence_id,
                    )
                )
        return issues

    def _provenance_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        rule = context.policy.rule(candidate.scope)
        issues: list[DecisionIssue] = []
        for document in context.documents:
            if document.ref.trust not in rule.allowed_trust:
                issues.append(
                    self._issue(
                        DecisionCode.TRUST_INSUFFICIENT,
                        "evidence trust tier is not allowed for candidate scope",
                        evidence_id=document.ref.evidence_id,
                        metadata={
                            "trust": document.ref.trust.value,
                            "scope": candidate.scope.value,
                        },
                    )
                )
            if (
                context.policy.reject_unknown_provenance_above_task
                and candidate.scope is not MemoryScope.TASK
                and document.ref.trust in {TrustTier.UNKNOWN, TrustTier.MODEL_PROPOSAL}
            ):
                issues.append(
                    self._issue(
                        DecisionCode.PROVENANCE_INVALID,
                        "unknown/model-only provenance cannot be promoted above task scope",
                        evidence_id=document.ref.evidence_id,
                    )
                )
            if not document.ref.source_revision.strip() or len(document.ref.content_digest) != 64:
                issues.append(
                    self._issue(
                        DecisionCode.PROVENANCE_INVALID,
                        "evidence source revision or content digest is missing",
                        evidence_id=document.ref.evidence_id,
                    )
                )
        return issues

    def _secret_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        if not context.policy.reject_secret_evidence:
            return []
        issues: list[DecisionIssue] = []
        for document in context.documents:
            if document.secret_fingerprints or document.redacted:
                issues.append(
                    self._issue(
                        DecisionCode.SECRET_DETECTED,
                        "candidate evidence contained a secret and cannot be committed",
                        evidence_id=document.ref.evidence_id,
                        metadata={
                            "secret_fingerprint_count": len(document.secret_fingerprints),
                            "redacted": document.redacted,
                        },
                    )
                )
        candidate_redaction = self.redactor.redact_value(
            {"summary": context.candidate.summary, "content": context.candidate.content}
        )
        if candidate_redaction.redacted:
            issues.append(
                self._issue(
                    DecisionCode.SECRET_DETECTED,
                    "candidate proposal contained a secret and cannot be committed",
                    field="content",
                    metadata={
                        "secret_fingerprint_count": len(candidate_redaction.fingerprints),
                    },
                )
            )
        return issues

    def _mutation_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        if not context.policy.reject_rule_mutation:
            return []
        issues: list[DecisionIssue] = []
        paths = self._forbidden_paths(context.candidate.content)
        if paths:
            issues.append(
                self._issue(
                    DecisionCode.RULE_MUTATION_FORBIDDEN,
                    "candidate attempts to mutate protected runtime rules",
                    field="content",
                    metadata={"paths": list(paths)},
                )
            )
        combined = f"{context.candidate.summary}\n{canonical_json(context.candidate.content)}"
        if PROMPT_MUTATION_PATTERN.search(combined):
            issues.append(
                self._issue(
                    DecisionCode.RULE_MUTATION_FORBIDDEN,
                    "candidate text contains a protected-rule override instruction",
                    field="summary",
                )
            )
        if context.candidate.model_assisted:
            for document in context.documents:
                if document.ref.trust is TrustTier.MODEL_PROPOSAL:
                    issues.append(
                        self._issue(
                            DecisionCode.TRUST_INSUFFICIENT,
                            "model proposal cannot bootstrap trust from model-only evidence",
                            evidence_id=document.ref.evidence_id,
                        )
                    )
        return issues

    def _ttl_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        rule = context.policy.rule(candidate.scope)
        ttl = candidate.ttl_seconds
        issues: list[DecisionIssue] = []
        if candidate.scope is MemoryScope.GLOBAL and context.policy.require_explicit_global_ttl and ttl is None:
            issues.append(
                self._issue(
                    DecisionCode.TTL_INVALID,
                    "global candidate requires an explicit ttl",
                    field="ttl_seconds",
                )
            )
        if ttl is not None and rule.maximum_ttl_seconds is not None and ttl > rule.maximum_ttl_seconds:
            issues.append(
                self._issue(
                    DecisionCode.TTL_INVALID,
                    "candidate ttl exceeds scope maximum",
                    field="ttl_seconds",
                    metadata={"actual": ttl, "maximum": rule.maximum_ttl_seconds},
                )
            )
        if candidate.kind is CandidateKind.FAILURE_PATTERN and not context.policy.preserve_failure_patterns:
            issues.append(
                self._issue(
                    DecisionCode.RETENTION_DENIED,
                    "failure-pattern retention is disabled",
                    field="kind",
                )
            )
        if candidate.kind is CandidateKind.SKILL_CANDIDATE and not context.policy.preserve_skill_candidates:
            issues.append(
                self._issue(
                    DecisionCode.RETENTION_DENIED,
                    "skill-candidate retention is disabled",
                    field="kind",
                )
            )
        return issues

    def _revision_checks(self, context: ValidationContext) -> list[DecisionIssue]:
        candidate = context.candidate
        if not candidate.expected_memory_id:
            if candidate.expected_revision not in {None, 0}:
                return [
                    self._issue(
                        DecisionCode.REVISION_CONFLICT,
                        "expected revision was supplied without a target memory id",
                        field="expected_revision",
                    )
                ]
            return []
        if candidate.expected_revision is None:
            return [
                self._issue(
                    DecisionCode.REVISION_CONFLICT,
                    "target memory update requires expected revision",
                    field="expected_revision",
                )
            ]
        if context.current_revision != candidate.expected_revision:
            return [
                self._issue(
                    DecisionCode.REVISION_CONFLICT,
                    "target memory revision changed",
                    field="expected_revision",
                    retryable=True,
                    metadata={
                        "expected": candidate.expected_revision,
                        "actual": context.current_revision,
                        "memory_id": candidate.expected_memory_id,
                    },
                )
            ]
        if not any(record.memory_id == candidate.expected_memory_id for record in context.canonical_records):
            return [
                self._issue(
                    DecisionCode.REVISION_CONFLICT,
                    "target memory does not exist in candidate task scope",
                    field="expected_memory_id",
                )
            ]
        return []

    def _relation_check(
        self,
        context: ValidationContext,
    ) -> tuple[DecisionStatus | None, DecisionIssue | None, str]:
        for relation in context.relations:
            if relation.source_candidate_id != context.candidate.candidate_id:
                continue
            if relation.kind is CandidateRelationKind.DUPLICATE_OF:
                return (
                    DecisionStatus.NOOP,
                    self._issue(
                        DecisionCode.DUPLICATE,
                        "candidate was consolidated into an existing candidate",
                        metadata={"relation_id": relation.relation_id},
                    ),
                    relation.relation_id,
                )
            if relation.kind is CandidateRelationKind.CONTRADICTS:
                return (
                    DecisionStatus.MERGE_REQUIRED,
                    self._issue(
                        DecisionCode.CONTRADICTION,
                        "candidate relation records a contradiction",
                        metadata={"relation_id": relation.relation_id},
                    ),
                    relation.relation_id,
                )
            if relation.kind is CandidateRelationKind.SUPERSEDES:
                return DecisionStatus.SUPERSEDE, None, relation.relation_id
        return None, None, ""

    def _sanitize_candidate(self, candidate: MemoryCandidate) -> tuple[str, Mapping[str, Any]]:
        summary_result = self.redactor.redact_value(candidate.summary)
        content_result = self.redactor.redact_value(candidate.content)
        summary = str(summary_result.value).strip()[: self.policy.maximum_summary_chars]
        content = content_result.value if isinstance(content_result.value, Mapping) else {"value": content_result.value}
        raw = canonical_json(content)
        if len(raw) > self.policy.maximum_content_chars:
            content = {
                "content_digest": stable_digest(content),
                "preview": raw[: self.policy.maximum_content_chars],
                "truncated": True,
                "original_chars": len(raw),
            }
        ttl = candidate.ttl_seconds
        if ttl is None:
            ttl = self.policy.default_ttl(candidate.scope)
        expires_at = ""
        if ttl is not None:
            candidate_time = datetime.fromisoformat(
                candidate.created_at.replace("Z", "+00:00")
            )
            if candidate_time.tzinfo is None:
                candidate_time = candidate_time.replace(tzinfo=UTC)
            expires_at = (candidate_time.astimezone(UTC) + timedelta(seconds=ttl)).isoformat(
                timespec="seconds"
            ).replace("+00:00", "Z")
        return summary, {
            **dict(content),
            "curator": {
                "candidate_kind": candidate.kind.value,
                "scope": candidate.scope.value,
                "subject": candidate.subject,
                "ttl_seconds": ttl,
                "expires_at": expires_at,
                "evidence_digest": candidate.evidence_digest,
                "extractor_version": candidate.extractor_version,
            },
        }

    @staticmethod
    def _status(
        candidate: MemoryCandidate,
        issues: Sequence[DecisionIssue],
        *,
        relation_status: DecisionStatus | None,
        duplicate: DuplicateFinding | None,
        contradiction: ContradictionFinding | None,
    ) -> DecisionStatus:
        if relation_status is DecisionStatus.MERGE_REQUIRED or contradiction is not None:
            return DecisionStatus.MERGE_REQUIRED
        if relation_status is DecisionStatus.NOOP or duplicate is not None:
            return DecisionStatus.NOOP
        if any(issue.code is DecisionCode.REVISION_CONFLICT for issue in issues):
            return DecisionStatus.MERGE_REQUIRED
        blocking = [issue for issue in issues if issue.code is not DecisionCode.OK]
        if blocking:
            return DecisionStatus.REJECT
        if candidate.kind is CandidateKind.DISCARD:
            return DecisionStatus.DISCARD
        if relation_status is DecisionStatus.SUPERSEDE:
            return DecisionStatus.SUPERSEDE
        return DecisionStatus.ACCEPT

    def _decision(
        self,
        candidate: MemoryCandidate,
        *,
        status: DecisionStatus,
        issues: Sequence[DecisionIssue],
    ) -> MemoryDecision:
        summary, content = self._sanitize_candidate(candidate)
        return MemoryDecision.build(
            candidate=candidate,
            status=status,
            policy_digest=self.policy.digest,
            validated_summary=summary,
            validated_content=content,
            issues=issues,
            metadata={"policy_id": self.policy.policy_id},
        )

    def _persist(self, decision: MemoryDecision) -> MemoryDecision:
        stored, _created = self.candidate_store.save_decision(decision)
        target_state = CandidateState.ACCEPTED if stored.accepted else CandidateState.REJECTED
        candidate = self.candidate_store.require_candidate(stored.candidate_id)
        if candidate.state is CandidateState.PROPOSED:
            self.candidate_store.update_candidate_state(
                candidate.candidate_id,
                expected=(CandidateState.PROPOSED,),
                target=target_state,
                metadata={"decision_id": stored.decision_id, "decision_status": stored.status.value},
            )
        return stored

    @staticmethod
    def _evidence_issue(value: str) -> DecisionIssue:
        evidence_id = value.split(":", 1)[1] if ":" in value else ""
        if value.startswith("evidence_missing") or value == "bundle_missing":
            code = DecisionCode.EVIDENCE_MISSING
        elif "range" in value:
            code = DecisionCode.EVIDENCE_RANGE_INVALID
        else:
            code = DecisionCode.EVIDENCE_FORGED
        return DecisionIssue(
            code=code,
            message=value,
            evidence_id=evidence_id,
            retryable=value.startswith("evidence_missing"),
        )

    @staticmethod
    def _issue(
        code: DecisionCode,
        message: str,
        *,
        field: str = "",
        evidence_id: str = "",
        retryable: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> DecisionIssue:
        return DecisionIssue(
            code=code,
            message=message,
            field=field,
            evidence_id=evidence_id,
            retryable=retryable,
            metadata=dict(metadata or {}),
        )

    @classmethod
    def _forbidden_paths(cls, value: Any, *, prefix: str = "$") -> tuple[str, ...]:
        paths: list[str] = []
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = str(raw_key)
                path = f"{prefix}.{key}"
                if key.casefold() in FORBIDDEN_RULE_KEYS:
                    paths.append(path)
                paths.extend(cls._forbidden_paths(item, prefix=path))
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, item in enumerate(value):
                paths.extend(cls._forbidden_paths(item, prefix=f"{prefix}[{index}]"))
        return unique_strings(paths)


__all__ = [
    "ContradictionFinding",
    "CuratorValidationPolicy",
    "DuplicateFinding",
    "MemoryContradictionDetector",
    "MemoryDecisionValidator",
    "MemoryDuplicateDetector",
    "ScopeRule",
    "ValidationContext",
    "ValidationPolicyError",
    "default_scope_rules",
]
