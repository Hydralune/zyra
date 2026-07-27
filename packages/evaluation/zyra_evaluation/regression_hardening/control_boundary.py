from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


class ControlDomain(StrEnum):
    PERMISSION = "permission"
    SCHEDULER = "scheduler"
    RECOVERY = "recovery"
    COMPACT = "compact"


class ControlEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"
    ROUTE = "route"
    REPLAN = "replan"
    RETRY = "retry"
    ABORT = "abort"
    COMPACT = "compact"
    RESTORE = "restore"
    NOOP = "noop"


@dataclass(frozen=True, slots=True)
class LlmSuggestion:
    suggestion_id: str
    run_id: str
    domain: ControlDomain
    requested_effect: ControlEffect
    subject_id: str
    content: str
    model_id: str
    sequence: int
    trusted: bool = False

    @property
    def digest(self) -> str:
        return stable_digest(
            self.suggestion_id,
            self.run_id,
            self.domain.value,
            self.requested_effect.value,
            self.subject_id,
            self.content,
            self.model_id,
            self.sequence,
            self.trusted,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "run_id": self.run_id,
            "domain": self.domain.value,
            "requested_effect": self.requested_effect.value,
            "subject_id": self.subject_id,
            "content_digest": stable_digest(self.content),
            "model_id": self.model_id,
            "sequence": self.sequence,
            "trusted": self.trusted,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class ControlReceipt:
    receipt_id: str
    run_id: str
    domain: ControlDomain
    effect: ControlEffect
    subject_id: str
    sequence: int
    owner_id: str
    rule_id: str
    input_digest: str
    policy_digest: str
    state_before_digest: str
    state_after_digest: str
    suggestion_digest: str = ""
    deterministic: bool = True
    committed: bool = True
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def state_changed(self) -> bool:
        return bool(
            self.state_after_digest
            and self.state_after_digest != self.state_before_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "domain": self.domain.value,
            "effect": self.effect.value,
            "subject_id": self.subject_id,
            "sequence": self.sequence,
            "owner_id": self.owner_id,
            "rule_id": self.rule_id,
            "input_digest": self.input_digest,
            "policy_digest": self.policy_digest,
            "state_before_digest": self.state_before_digest,
            "state_after_digest": self.state_after_digest,
            "state_changed": self.state_changed,
            "suggestion_digest": self.suggestion_digest,
            "deterministic": self.deterministic,
            "committed": self.committed,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class BoundaryFinding:
    code: str
    domain: ControlDomain
    subject_id: str
    receipt_id: str
    suggestion_id: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "domain": self.domain.value,
            "subject_id": self.subject_id,
            "receipt_id": self.receipt_id,
            "suggestion_id": self.suggestion_id,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ControlBoundaryReport:
    suggestions: tuple[LlmSuggestion, ...]
    receipts: tuple[ControlReceipt, ...]
    findings: tuple[BoundaryFinding, ...]
    domain_counts: Mapping[str, int]
    owner_counts: Mapping[str, int]
    linked_suggestions: tuple[str, ...]
    digest: str

    @property
    def valid(self) -> bool:
        required = set(ControlDomain)
        observed = {item.domain for item in self.receipts}
        return not self.findings and required.issubset(observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-llm-control-boundary/v1",
            "valid": self.valid,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "receipts": [item.to_dict() for item in self.receipts],
            "findings": [item.to_dict() for item in self.findings],
            "domain_counts": dict(sorted(self.domain_counts.items())),
            "owner_counts": dict(sorted(self.owner_counts.items())),
            "linked_suggestions": list(self.linked_suggestions),
            "digest": self.digest,
        }


class ControlBoundaryAuditor:
    """Proves suggestions are data and Zyra receipts are enforcing facts."""

    _OWNERS = {
        ControlDomain.PERMISSION: {
            "ToolPermissionRuntime",
            "PermissionRuntimeService",
            "SealedAutonomousPolicy",
        },
        ControlDomain.SCHEDULER: {
            "ResourceSchedulerRuntime",
            "WorkerLifecycleRuntime",
            "TopologyRouter",
        },
        ControlDomain.RECOVERY: {
            "RecoveryPlannerRuntime",
            "FaultRuntime",
            "CheckpointRecoveryRuntime",
        },
        ControlDomain.COMPACT: {
            "MessageManagerRuntime",
            "MemoryFabric",
            "CodeWorkerCompactRuntime",
        },
    }

    _EFFECTS = {
        ControlDomain.PERMISSION: {
            ControlEffect.ALLOW,
            ControlEffect.DENY,
            ControlEffect.ASK,
        },
        ControlDomain.SCHEDULER: {
            ControlEffect.ROUTE,
            ControlEffect.DENY,
            ControlEffect.NOOP,
        },
        ControlDomain.RECOVERY: {
            ControlEffect.REPLAN,
            ControlEffect.RETRY,
            ControlEffect.ABORT,
            ControlEffect.RESTORE,
        },
        ControlDomain.COMPACT: {
            ControlEffect.COMPACT,
            ControlEffect.RESTORE,
            ControlEffect.NOOP,
        },
    }

    def audit(
        self,
        suggestions: Sequence[LlmSuggestion],
        receipts: Sequence[ControlReceipt],
    ) -> ControlBoundaryReport:
        findings: list[BoundaryFinding] = []
        suggestions_by_digest = {item.digest: item for item in suggestions}
        receipts_by_identity: set[tuple[str, str, int]] = set()
        linked: set[str] = set()
        for receipt in receipts:
            suggestion = suggestions_by_digest.get(receipt.suggestion_digest)
            finding = self._validate_receipt(receipt, suggestion)
            findings.extend(finding)
            identity = (receipt.run_id, receipt.receipt_id, receipt.sequence)
            if identity in receipts_by_identity:
                findings.append(
                    self._finding(
                        "duplicate_receipt",
                        receipt,
                        suggestion,
                        "control receipt identity is duplicated",
                    )
                )
            receipts_by_identity.add(identity)
            if suggestion:
                linked.add(suggestion.suggestion_id)
        for suggestion in suggestions:
            if suggestion.suggestion_id not in linked:
                continue
            matching = [
                receipt
                for receipt in receipts
                if receipt.suggestion_digest == suggestion.digest
            ]
            if len(matching) > 1 and len(
                {
                    (
                        receipt.domain,
                        receipt.subject_id,
                        receipt.state_after_digest,
                    )
                    for receipt in matching
                }
            ) > 1:
                findings.append(
                    BoundaryFinding(
                        code="suggestion_multiple_effects",
                        domain=suggestion.domain,
                        subject_id=suggestion.subject_id,
                        receipt_id="",
                        suggestion_id=suggestion.suggestion_id,
                        reason="one model suggestion produced conflicting control effects",
                    )
                )
        domain_counts = Counter(item.domain.value for item in receipts)
        owner_counts = Counter(item.owner_id for item in receipts)
        material = {
            "suggestions": [item.to_dict() for item in suggestions],
            "receipts": [item.to_dict() for item in receipts],
            "findings": [item.to_dict() for item in findings],
        }
        return ControlBoundaryReport(
            suggestions=tuple(suggestions),
            receipts=tuple(receipts),
            findings=tuple(findings),
            domain_counts=dict(domain_counts),
            owner_counts=dict(owner_counts),
            linked_suggestions=tuple(sorted(linked)),
            digest=stable_digest(material),
        )

    def mutation_campaign(
        self,
        suggestions: Sequence[LlmSuggestion],
        receipts: Sequence[ControlReceipt],
    ) -> Mapping[str, ControlBoundaryReport]:
        if not suggestions or len(receipts) < 4:
            raise ValueError(
                "control-boundary campaign needs suggestions and all four receipts"
            )
        first = receipts[0]
        forged_owner = replace(first, receipt_id=f"{first.receipt_id}-owner", owner_id="LLM")
        nondeterministic = replace(
            first,
            receipt_id=f"{first.receipt_id}-nondeterministic",
            deterministic=False,
        )
        missing_rule = replace(
            first,
            receipt_id=f"{first.receipt_id}-rule",
            rule_id="",
            policy_digest="",
        )
        cross_run = replace(
            first,
            receipt_id=f"{first.receipt_id}-cross",
            run_id=f"{first.run_id}-other",
        )
        direct_suggestion = replace(
            first,
            receipt_id=f"{first.receipt_id}-direct",
            input_digest=suggestions[0].digest,
            suggestion_digest=suggestions[0].digest,
            rule_id="model_output",
            owner_id="ModelGateway",
        )
        return {
            "baseline": self.audit(suggestions, receipts),
            "forged_owner": self.audit(suggestions, (*receipts, forged_owner)),
            "nondeterministic": self.audit(suggestions, (*receipts, nondeterministic)),
            "missing_rule": self.audit(suggestions, (*receipts, missing_rule)),
            "cross_run": self.audit(suggestions, (*receipts, cross_run)),
            "direct_suggestion": self.audit(suggestions, (*receipts, direct_suggestion)),
        }

    def _validate_receipt(
        self,
        receipt: ControlReceipt,
        suggestion: LlmSuggestion | None,
    ) -> list[BoundaryFinding]:
        findings: list[BoundaryFinding] = []
        if receipt.owner_id not in self._OWNERS[receipt.domain]:
            findings.append(
                self._finding(
                    "owner_invalid",
                    receipt,
                    suggestion,
                    f"{receipt.owner_id} is not an admitted owner for {receipt.domain.value}",
                )
            )
        if receipt.effect not in self._EFFECTS[receipt.domain]:
            findings.append(
                self._finding(
                    "effect_invalid",
                    receipt,
                    suggestion,
                    f"{receipt.effect.value} is invalid for {receipt.domain.value}",
                )
            )
        if not receipt.deterministic:
            findings.append(
                self._finding(
                    "nondeterministic_receipt",
                    receipt,
                    suggestion,
                    "control receipt is not deterministic",
                )
            )
        if not receipt.committed:
            findings.append(
                self._finding(
                    "uncommitted_effect",
                    receipt,
                    suggestion,
                    "uncommitted receipt cannot prove a control effect",
                )
            )
        for field_name, value in (
            ("rule_id", receipt.rule_id),
            ("input_digest", receipt.input_digest),
            ("policy_digest", receipt.policy_digest),
            ("state_after_digest", receipt.state_after_digest),
        ):
            if not value:
                findings.append(
                    self._finding(
                        f"{field_name}_missing",
                        receipt,
                        suggestion,
                        f"control receipt is missing {field_name}",
                    )
                )
        if suggestion:
            if suggestion.run_id != receipt.run_id:
                findings.append(
                    self._finding(
                        "cross_run_suggestion",
                        receipt,
                        suggestion,
                        "suggestion and control receipt belong to different runs",
                    )
                )
            if suggestion.domain != receipt.domain:
                findings.append(
                    self._finding(
                        "domain_mismatch",
                        receipt,
                        suggestion,
                        "suggestion domain differs from receipt domain",
                    )
                )
            if suggestion.subject_id != receipt.subject_id:
                findings.append(
                    self._finding(
                        "subject_mismatch",
                        receipt,
                        suggestion,
                        "suggestion subject differs from receipt subject",
                    )
                )
            if receipt.sequence <= suggestion.sequence:
                findings.append(
                    self._finding(
                        "sequence_invalid",
                        receipt,
                        suggestion,
                        "control receipt must follow the model suggestion",
                    )
                )
            if receipt.input_digest == suggestion.digest:
                findings.append(
                    self._finding(
                        "model_output_used_as_rule_input",
                        receipt,
                        suggestion,
                        "raw model output digest cannot replace normalized policy input",
                    )
                )
            if receipt.rule_id.casefold() in {
                "llm",
                "model",
                "model_output",
                "suggestion",
            }:
                findings.append(
                    self._finding(
                        "model_rule_owner",
                        receipt,
                        suggestion,
                        "model output cannot be the enforcing rule",
                    )
                )
        return findings

    @staticmethod
    def _finding(
        code: str,
        receipt: ControlReceipt,
        suggestion: LlmSuggestion | None,
        reason: str,
    ) -> BoundaryFinding:
        return BoundaryFinding(
            code=code,
            domain=receipt.domain,
            subject_id=receipt.subject_id,
            receipt_id=receipt.receipt_id,
            suggestion_id=suggestion.suggestion_id if suggestion else "",
            reason=reason,
        )


def control_receipts_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[ControlReceipt, ...]:
    receipts: list[ControlReceipt] = []
    for index, value in enumerate(values):
        receipts.append(
            ControlReceipt(
                receipt_id=str(value.get("receipt_id") or f"receipt-{index + 1}"),
                run_id=str(value.get("run_id") or ""),
                domain=ControlDomain(str(value.get("domain") or "")),
                effect=ControlEffect(str(value.get("effect") or "")),
                subject_id=str(value.get("subject_id") or ""),
                sequence=int(value.get("sequence") or index + 1),
                owner_id=str(value.get("owner_id") or ""),
                rule_id=str(value.get("rule_id") or ""),
                input_digest=str(value.get("input_digest") or ""),
                policy_digest=str(value.get("policy_digest") or ""),
                state_before_digest=str(value.get("state_before_digest") or ""),
                state_after_digest=str(value.get("state_after_digest") or ""),
                suggestion_digest=str(value.get("suggestion_digest") or ""),
                deterministic=bool(value.get("deterministic", True)),
                committed=bool(value.get("committed", True)),
                attributes=(
                    dict(value.get("attributes"))
                    if isinstance(value.get("attributes"), Mapping)
                    else {}
                ),
            )
        )
    return tuple(receipts)


def evaluate_control_boundary(
    suggestions: Sequence[LlmSuggestion],
    receipts: Sequence[ControlReceipt],
) -> CaseExecutionBuffer:
    reports = ControlBoundaryAuditor().mutation_campaign(suggestions, receipts)
    buffer = CaseExecutionBuffer()
    baseline = reports["baseline"]
    base_observation = buffer.observe(
        "control-boundary.baseline",
        ObservationKind.CONTROL,
        "permission-scheduler-recovery-compact",
        "deterministic" if baseline.valid else "invalid",
        attributes={
            "report_digest": baseline.digest,
            "domain_counts": baseline.domain_counts,
            "owner_counts": baseline.owner_counts,
        },
    )
    buffer.assert_that(
        "control-boundary.baseline-valid",
        baseline.valid,
        "all four control domains must have deterministic Zyra-owned receipts",
        evidence=(base_observation.observation_id,),
    )
    for name, report in reports.items():
        if name == "baseline":
            continue
        observation = buffer.observe(
            f"control-boundary.{name}",
            ObservationKind.MUTATION,
            f"llm-boundary-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "report_digest": report.digest,
                "finding_codes": [item.code for item in report.findings],
            },
        )
        buffer.assert_that(
            f"control-boundary.reject-{name}",
            not report.valid,
            f"{name} control-boundary mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    return buffer


__all__ = [
    "BoundaryFinding",
    "ControlBoundaryAuditor",
    "ControlBoundaryReport",
    "ControlDomain",
    "ControlEffect",
    "ControlReceipt",
    "LlmSuggestion",
    "control_receipts_from_mappings",
    "evaluate_control_boundary",
]
