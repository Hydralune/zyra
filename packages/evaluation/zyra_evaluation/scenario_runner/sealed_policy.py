from __future__ import annotations

import hmac
import os
from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .canonical import digest, identity, new_identity, require_digest, utc_now
from .errors import conflict, invalid, unavailable
from .models import (
    InterventionRecord,
    PolicyDecision,
    ScenarioConfiguration,
    ScenarioMode,
    SealedPolicy,
)


class SealedPolicyRuntime:
    def __init__(self, policy: SealedPolicy) -> None:
        self._policy = policy
        self._decisions: list[PolicyDecision] = []
        self._interventions: list[InterventionRecord] = []
        self._denials = 0

    @property
    def policy(self) -> SealedPolicy:
        return self._policy

    @property
    def decisions(self) -> tuple[PolicyDecision, ...]:
        return tuple(self._decisions)

    @property
    def interventions(self) -> tuple[InterventionRecord, ...]:
        return tuple(self._interventions)

    @property
    def human_intervention_count(self) -> int:
        return sum(1 for item in self._interventions if item.counted_as_human)

    @property
    def operator_intervention_attempt_count(self) -> int:
        return len(self._interventions)

    def verify_configuration(self, configuration: ScenarioConfiguration) -> str:
        if configuration.mode is not ScenarioMode.SEALED:
            return self._policy.policy_digest
        if _disabled("ZYRA_SCENARIO_SEALED_POLICY_DISABLED"):
            raise unavailable(
                "scenario_sealed_policy_disabled",
                "Formal scenario start requires the sealed policy runtime.",
                phase="sealed-policy",
            )
        expected = require_digest(
            configuration.expected_policy_digest,
            "expected sealed policy digest",
        )
        actual = self._policy.policy_digest
        if not hmac.compare_digest(expected, actual):
            raise conflict(
                "scenario_policy_digest_mismatch",
                "Formal scenario policy digest does not match the registered policy.",
                phase="sealed-policy",
                detail={"expected": expected, "actual": actual},
            )
        if self._policy.ask_disposition != "deny_and_replan":
            raise conflict(
                "scenario_policy_allows_human_wait",
                "Formal scenario policy must deny and replan ask decisions.",
                phase="sealed-policy",
            )
        if self._policy.unknown_disposition != "deny_and_replan":
            raise conflict(
                "scenario_policy_allows_unknown",
                "Formal scenario policy must fail closed for unknown actions.",
                phase="sealed-policy",
            )
        return actual

    def evaluate(
        self,
        action: Mapping[str, Any],
        *,
        sequence: int | None = None,
    ) -> PolicyDecision:
        action_id = identity(
            action.get("action_id") or new_identity("action"),
            "sealed action id",
        )
        name = identity(action.get("action"), "sealed action")
        requested = str(action.get("effect") or "ask").strip().casefold()
        if requested not in {"allow", "deny", "ask"}:
            raise invalid(
                "scenario_policy_effect_invalid",
                "Sealed action effect must be allow, deny, or ask.",
                phase="sealed-policy",
            )
        high_risk = name in self._policy.high_risk_actions
        explicitly_denied = name in self._policy.deny_actions
        explicitly_allowed = name in self._policy.allow_actions
        unknown = not explicitly_denied and not explicitly_allowed
        final_effect = "allow"
        reason_code = "sealed.allowlist"
        reason = "Action matches the sealed deterministic allowlist."
        recovery_action = ""
        if high_risk:
            final_effect = "deny"
            reason_code = "sealed.high_risk"
            reason = "High-risk actions are deterministically denied."
            recovery_action = "replan"
        elif explicitly_denied or requested == "deny":
            final_effect = "deny"
            reason_code = "sealed.denylist"
            reason = "Action matches the sealed denylist."
            recovery_action = "replan"
        elif requested == "ask":
            final_effect = "deny"
            reason_code = "sealed.ask_converted"
            reason = "Ask was converted to deterministic deny and replan."
            recovery_action = "replan"
        elif unknown:
            final_effect = "deny"
            reason_code = "sealed.unknown"
            reason = "Unknown action was deterministically denied."
            recovery_action = "replan"
        elif not explicitly_allowed:
            final_effect = "deny"
            reason_code = "sealed.not_allowed"
            reason = "Action is not in the sealed allowlist."
            recovery_action = "replan"
        if final_effect == "deny":
            self._denials += 1
            if self._denials > self._policy.maximum_denials:
                recovery_action = "abort"
                reason_code = "sealed.denial_budget_exhausted"
                reason = "Sealed denial budget was exhausted; abort is deterministic."
        decision = PolicyDecision(
            decision_id=new_identity("policy"),
            action_id=action_id,
            action=name,
            requested_effect=requested,
            final_effect=final_effect,
            reason_code=reason_code,
            reason=reason,
            recovery_action=recovery_action,
            policy_digest=self._policy.policy_digest,
            sequence=sequence if sequence is not None else len(self._decisions) + 1,
            created_at=utc_now(),
            metadata={
                "high_risk": high_risk,
                "unknown": unknown,
                "human_wait": False,
                "denial_count": self._denials,
                "original_metadata": dict(action.get("metadata") or {}),
            },
        )
        self._decisions.append(decision)
        return decision

    def evaluate_all(
        self,
        actions: Iterable[Mapping[str, Any]],
    ) -> tuple[PolicyDecision, ...]:
        output: list[PolicyDecision] = []
        for sequence, action in enumerate(actions, start=1):
            output.append(self.evaluate(action, sequence=sequence))
        return tuple(output)

    def record_operator_attempt(
        self,
        *,
        actor_id: str,
        action: str,
        reason: str,
        kind: str = "manual_mutation",
        metadata: Mapping[str, Any] | None = None,
    ) -> InterventionRecord:
        record = InterventionRecord(
            intervention_id=new_identity("intervention"),
            kind=identity(kind, "intervention kind"),
            actor_id=identity(actor_id, "intervention actor"),
            action=identity(action, "intervention action"),
            counted_as_human=False,
            rejected=True,
            reason=str(reason or "Sealed scenario rejected operator mutation.").strip(),
            created_at=utc_now(),
            metadata={
                **dict(metadata or {}),
                "sealed": True,
                "human_intervention_count": 0,
                "operator_attempt_counted": True,
            },
        )
        self._interventions.append(record)
        return record

    def assert_formal_invariants(self) -> dict[str, Any]:
        violations: list[str] = []
        if self.human_intervention_count != 0:
            violations.append("human_intervention_count_nonzero")
        if any(item.requested_effect == "ask" and item.final_effect != "deny" for item in self._decisions):
            violations.append("ask_not_denied")
        if any(item.metadata.get("human_wait") is True for item in self._decisions):
            violations.append("human_wait_present")
        if any(
            item.final_effect == "allow"
            and item.action not in self._policy.allow_actions
            for item in self._decisions
        ):
            violations.append("unknown_action_allowed")
        if self._interventions:
            violations.append("operator_intervention_attempt_present")
        receipt = {
            "schema": "zyra.sealed-policy-verification/v1",
            "policy_digest": self._policy.policy_digest,
            "decision_count": len(self._decisions),
            "denial_count": self._denials,
            "human_intervention_count": self.human_intervention_count,
            "operator_intervention_attempt_count": self.operator_intervention_attempt_count,
            "violations": violations,
            "valid": not violations,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        if violations:
            raise conflict(
                "scenario_sealed_invariant_failed",
                "Formal sealed scenario invariant failed.",
                phase="sealed-policy",
                detail=receipt,
            )
        return receipt

    def snapshot(self) -> dict[str, Any]:
        value = {
            "schema": "zyra.sealed-policy-runtime/v1",
            "policy": self._policy.to_dict(),
            "decisions": [item.to_dict() for item in self._decisions],
            "interventions": [item.to_dict() for item in self._interventions],
            "human_intervention_count": self.human_intervention_count,
            "operator_intervention_attempt_count": self.operator_intervention_attempt_count,
        }
        value["snapshot_digest"] = digest(value)
        return value


def require_safe_policy_decisions(
    decisions: Iterable[PolicyDecision],
    *,
    required_actions: Iterable[str],
) -> None:
    indexed = {item.action: item for item in decisions}
    missing = [action for action in required_actions if action not in indexed]
    denied_required = [
        action
        for action in required_actions
        if action in indexed and indexed[action].final_effect != "allow"
    ]
    if missing or denied_required:
        raise conflict(
            "scenario_required_action_not_admitted",
            "Sealed policy did not admit every required canonical-owner action.",
            phase="sealed-policy",
            detail={"missing": missing, "denied": denied_required},
        )


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}
