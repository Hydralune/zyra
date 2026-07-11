from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Sequence

from .digests import digest_object
from .models import (
    SkillInvocationRequest,
    SkillRevision,
    SkillSourceKind,
    SkillTrustTier,
    utc_now,
)


class SkillCommandSafety(StrEnum):
    SAFE_CONTEXT_EXPANSION = "safe_context_expansion"
    PERMISSION_REQUIRED = "permission_required"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class SkillAdmissionFinding:
    code: str
    message: str
    blocking: bool
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "blocking": self.blocking,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class SkillAdmissionVerdict:
    safety: SkillCommandSafety
    skill_ref: str
    request_digest: str
    findings: tuple[SkillAdmissionFinding, ...]
    downstream_tools_authorized: bool = False
    evaluated_at: str = field(default_factory=utc_now)

    @property
    def bootstrap_allow(self) -> bool:
        return self.safety is SkillCommandSafety.SAFE_CONTEXT_EXPANSION

    @property
    def valid(self) -> bool:
        return self.safety is not SkillCommandSafety.INVALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "safety": str(self.safety),
            "skill_ref": self.skill_ref,
            "request_digest": self.request_digest,
            "findings": [finding.to_dict() for finding in self.findings],
            "bootstrap_allow": self.bootstrap_allow,
            "valid": self.valid,
            "downstream_tools_authorized": self.downstream_tools_authorized,
            "evaluated_at": self.evaluated_at,
        }


class SkillCommandSafetyClassifier:
    """Classify the narrow command property that may receive bootstrap allow.

    A safe classification authorizes only immutable prompt-context expansion.
    It never authorizes a tool named by ``allowed-tools``.  Non-product sources
    are valid requests but must proceed through the normal 03A ask/deny rules.
    """

    def __init__(
        self,
        *,
        max_argument_depth: int = 8,
        max_argument_items: int = 512,
        max_argument_bytes: int = 64_000,
    ) -> None:
        if max_argument_depth < 1 or max_argument_items < 1 or max_argument_bytes < 128:
            raise ValueError("skill admission limits must be positive")
        self.max_argument_depth = max_argument_depth
        self.max_argument_items = max_argument_items
        self.max_argument_bytes = max_argument_bytes

    def classify(
        self,
        request: SkillInvocationRequest,
        revision: SkillRevision,
        *,
        requested_resources: Sequence[str] = (),
        active_ref: str = "",
    ) -> SkillAdmissionVerdict:
        findings: list[SkillAdmissionFinding] = []
        request_payload = {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "session_id": request.session_id,
            "agent_id": request.agent_id,
            "skill_name": request.skill_name,
            "arguments": request.arguments,
            "resources": list(requested_resources),
            "skill_depth": request.skill_depth,
            "interactive": request.interactive,
            "headless": request.headless,
            "skill_ref": revision.version_ref.immutable_ref,
        }
        request_digest = digest_object(request_payload)
        argument_error = self._validate_arguments(request.arguments)
        if argument_error:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_ARGUMENT_SHAPE_INVALID",
                    message=argument_error,
                    blocking=True,
                )
            )
        declared_resources = set(revision.metadata.resources)
        undeclared = sorted(set(requested_resources) - declared_resources)
        if undeclared:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_RESOURCE_NOT_DECLARED",
                    message="requested skill resources are not in the immutable revision",
                    blocking=True,
                    evidence={"resources": undeclared},
                )
            )
        if request.requested_version_ref is not None:
            if request.requested_version_ref.immutable_ref != revision.version_ref.immutable_ref:
                findings.append(
                    SkillAdmissionFinding(
                        code="SKILL_REQUESTED_REVISION_MISMATCH",
                        message="requested immutable skill revision does not match resolution",
                        blocking=True,
                    )
                )
        if active_ref and active_ref != revision.version_ref.immutable_ref:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_REVISION_NOT_ACTIVE",
                    message="resolved revision is not the current active registry revision",
                    blocking=True,
                    evidence={"active_ref": active_ref},
                )
            )
        if not revision.metadata.user_invocable and request.interactive:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_NOT_USER_INVOCABLE",
                    message="interactive command cannot invoke a model-only skill",
                    blocking=True,
                )
            )
        if request.headless and not revision.metadata.model_invocable:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_NOT_MODEL_INVOCABLE",
                    message="sealed/headless execution cannot invoke this skill",
                    blocking=True,
                )
            )
        if request.skill_depth > revision.metadata.invocation.max_skill_depth:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_DEPTH_EXCEEDED",
                    message="skill invocation depth exceeds the revision policy",
                    blocking=True,
                    evidence={
                        "requested": request.skill_depth,
                        "limit": revision.metadata.invocation.max_skill_depth,
                    },
                )
            )
        if any(finding.blocking for finding in findings):
            return SkillAdmissionVerdict(
                safety=SkillCommandSafety.INVALID,
                skill_ref=revision.version_ref.immutable_ref,
                request_digest=request_digest,
                findings=tuple(findings),
            )

        product_owned = (
            revision.provenance.source_kind is SkillSourceKind.BUILTIN
            and revision.provenance.trust_tier is SkillTrustTier.PRODUCT
        )
        if not product_owned:
            findings.append(
                SkillAdmissionFinding(
                    code="SKILL_03A_DECISION_REQUIRED",
                    message="non-product skill context expansion requires an explicit 03A decision",
                    blocking=False,
                    evidence={
                        "source_kind": str(revision.provenance.source_kind),
                        "trust_tier": str(revision.provenance.trust_tier),
                    },
                )
            )
            return SkillAdmissionVerdict(
                safety=SkillCommandSafety.PERMISSION_REQUIRED,
                skill_ref=revision.version_ref.immutable_ref,
                request_digest=request_digest,
                findings=tuple(findings),
            )

        findings.append(
            SkillAdmissionFinding(
                code="SKILL_SAFE_CONTEXT_ONLY",
                message="immutable product skill may expand prompt context; downstream tools remain under 03A",
                blocking=False,
                evidence={
                    "policy_digest": revision.version_ref.policy_digest,
                    "provenance_digest": revision.version_ref.provenance_digest,
                    "allowed_tools_semantics": "deny-only ceiling",
                },
            )
        )
        return SkillAdmissionVerdict(
            safety=SkillCommandSafety.SAFE_CONTEXT_EXPANSION,
            skill_ref=revision.version_ref.immutable_ref,
            request_digest=request_digest,
            findings=tuple(findings),
        )

    def _validate_arguments(self, arguments: Mapping[str, Any]) -> str:
        item_count = 0
        byte_count = 0
        stack: list[tuple[Any, int]] = [(arguments, 0)]
        while stack:
            value, depth = stack.pop()
            if depth > self.max_argument_depth:
                return "skill arguments exceed the maximum nesting depth"
            item_count += 1
            if item_count > self.max_argument_items:
                return "skill arguments exceed the maximum item count"
            if value is None or isinstance(value, (bool, int, float)):
                byte_count += len(str(value))
            elif isinstance(value, str):
                byte_count += len(value.encode("utf-8"))
            elif isinstance(value, Mapping):
                for key, child in value.items():
                    if not isinstance(key, str):
                        return "skill argument object keys must be strings"
                    byte_count += len(key.encode("utf-8"))
                    stack.append((child, depth + 1))
            elif isinstance(value, (list, tuple)):
                stack.extend((child, depth + 1) for child in value)
            else:
                return f"skill arguments contain unsupported value type: {type(value).__name__}"
            if byte_count > self.max_argument_bytes:
                return "skill arguments exceed the maximum encoded size"
        return ""
