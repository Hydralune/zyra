from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .contracts import Finding, GateResult, GateStatus, Severity


_LOW_RISK_EFFECTS = {"allow", "allowed", "auto_allow", "permitted"}
_DENY_EFFECTS = {"deny", "denied", "reject", "rejected", "blocked"}
_ASK_EFFECTS = {"ask", "pending", "approval_required", "suspended"}
_TERMINAL_RECOVERY = {"replanned", "rerouted", "recovered", "aborted", "safely_failed", "cancelled"}
_HIGH_RISK_LEVELS = {"high", "critical", "unknown", "destructive", "open_world"}
_LOW_RISK_LEVELS = {"low", "read_only", "readonly", "safe", "deterministic_read"}


@dataclass(frozen=True, slots=True)
class AutonomyObservation:
    event_id: str
    event_type: str
    effect: str
    risk: str
    tool_name: str
    request_id: str
    causation_id: str
    recovery_state: str
    human_actor: bool
    manual_resume: bool
    state_edit: bool
    unresolved: bool
    metadata: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "effect": self.effect,
            "risk": self.risk,
            "tool_name": self.tool_name,
            "request_id": self.request_id,
            "causation_id": self.causation_id,
            "recovery_state": self.recovery_state,
            "human_actor": self.human_actor,
            "manual_resume": self.manual_resume,
            "state_edit": self.state_edit,
            "unresolved": self.unresolved,
            "metadata": dict(self.metadata),
        }


class AutonomyEventExtractor:
    def extract(self, events: Sequence[Mapping[str, Any]]) -> tuple[AutonomyObservation, ...]:
        observations: list[AutonomyObservation] = []
        for index, event in enumerate(events):
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
            decision = self._decision(payload, event)
            if not decision and not self._autonomy_event(event_type, payload, metadata):
                continue
            request_id = str(
                decision.get("request_id")
                or decision.get("permission_request_id")
                or payload.get("request_id")
                or metadata.get("request_id")
                or ""
            )
            effect = str(
                decision.get("effect")
                or decision.get("decision")
                or decision.get("status")
                or payload.get("effect")
                or ""
            ).lower()
            risk = str(
                decision.get("risk")
                or decision.get("risk_level")
                or payload.get("risk")
                or metadata.get("risk")
                or ""
            ).lower()
            recovery_state = str(
                decision.get("recovery_state")
                or payload.get("recovery_state")
                or payload.get("recovery_disposition")
                or metadata.get("recovery_state")
                or ""
            ).lower()
            actor = str(
                decision.get("actor")
                or decision.get("actor_id")
                or payload.get("actor_id")
                or event.get("actor_id")
                or ""
            ).lower()
            human = self._truthy(
                decision.get("human_approval")
                or payload.get("human_approval")
                or metadata.get("human_intervention")
            ) or actor in {"user", "human", "operator", "api-operator", "approver"}
            manual_resume = self._truthy(
                decision.get("manual_resume")
                or payload.get("manual_resume")
                or metadata.get("manual_resume")
            ) or event_type in {"manual_resume", "session_manually_resumed"}
            state_edit = self._truthy(
                decision.get("manual_state_edit")
                or payload.get("manual_state_edit")
                or metadata.get("manual_state_edit")
            ) or event_type in {"manual_state_edit", "state_override"}
            unresolved = effect in _ASK_EFFECTS or self._truthy(
                decision.get("unresolved")
                or payload.get("unresolved_approval")
                or metadata.get("unresolved_approval")
            )
            observations.append(
                AutonomyObservation(
                    event_id=str(event.get("event_id") or f"autonomy-{index}"),
                    event_type=event_type,
                    effect=effect,
                    risk=risk,
                    tool_name=str(
                        decision.get("tool_name")
                        or payload.get("tool_name")
                        or payload.get("capability")
                        or ""
                    ),
                    request_id=request_id,
                    causation_id=str(
                        decision.get("causation_id")
                        or payload.get("causation_id")
                        or metadata.get("causation_id")
                        or ""
                    ),
                    recovery_state=recovery_state,
                    human_actor=human,
                    manual_resume=manual_resume,
                    state_edit=state_edit,
                    unresolved=unresolved,
                    metadata={**dict(metadata), **dict(decision.get("metadata") or {})},
                )
            )
        return tuple(observations)

    @staticmethod
    def _decision(payload: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("permission_decision", "decision", "permission", "policy_decision"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return value
            value = event.get(key)
            if isinstance(value, Mapping):
                return value
        return {}

    @staticmethod
    def _autonomy_event(
        event_type: str,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> bool:
        tokens = ("permission", "approval", "recovery", "replan", "manual", "policy", "tool")
        return any(token in event_type for token in tokens) or any(
            key in payload or key in metadata
            for key in ("human_intervention", "manual_resume", "manual_state_edit", "unresolved_approval")
        )

    @staticmethod
    def _truthy(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on", "human", "manual"}


class SealedAutonomyGate:
    def __init__(self) -> None:
        self.extractor = AutonomyEventExtractor()

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        declared_policy: Mapping[str, Any] | None = None,
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="sealed-autonomy",
            status=GateStatus.NOT_RUN,
            summary="Sealed low-risk allow, high-risk deny/recovery and zero-human audit.",
        )
        policy = dict(declared_policy or {})
        observations = self.extractor.extract(events)
        result.findings.extend(self._policy_findings(policy))
        result.findings.extend(self._observation_findings(observations))
        result.findings.extend(self._recovery_chain_findings(observations))
        human_count = sum(1 for item in observations if item.human_actor)
        manual_resume_count = sum(1 for item in observations if item.manual_resume)
        state_edit_count = sum(1 for item in observations if item.state_edit)
        unresolved_count = sum(1 for item in observations if item.unresolved)
        low_risk_allows = sum(1 for item in observations if item.risk in _LOW_RISK_LEVELS and item.effect in _LOW_RISK_EFFECTS)
        high_risk_denies = sum(1 for item in observations if item.risk in _HIGH_RISK_LEVELS and item.effect in _DENY_EFFECTS)
        if human_count:
            result.add(
                Finding(
                    code="autonomy.human_intervention",
                    severity=Severity.BLOCKER,
                    summary="Sealed benchmark includes human approval/intervention.",
                    detail=f"count={human_count}",
                )
            )
        if manual_resume_count:
            result.add(
                Finding(
                    code="autonomy.manual_resume",
                    severity=Severity.BLOCKER,
                    summary="Sealed benchmark contains a manual resume.",
                    detail=f"count={manual_resume_count}",
                )
            )
        if state_edit_count:
            result.add(
                Finding(
                    code="autonomy.manual_state_edit",
                    severity=Severity.BLOCKER,
                    summary="Sealed benchmark contains a manual state edit.",
                    detail=f"count={state_edit_count}",
                )
            )
        if unresolved_count:
            result.add(
                Finding(
                    code="autonomy.unresolved_approval",
                    severity=Severity.BLOCKER,
                    summary="Sealed benchmark ended with unresolved approval state.",
                    detail=f"count={unresolved_count}",
                )
            )
        if final_completion and not observations:
            result.add(
                Finding(
                    code="autonomy.no_policy_evidence",
                    severity=Severity.BLOCKER,
                    summary="No real sealed policy decisions were observed.",
                )
            )
        elif not observations:
            result.add(
                Finding(
                    code="autonomy.no_policy_evidence",
                    severity=Severity.WARNING,
                    summary="Foundation gate is executable but no sealed decision trace was supplied.",
                )
            )
            result.limitations.append("A formal sealed live scenario remains open for M1 integration/milestone exit.")
        result.metrics.update(
            {
                "observation_count": len(observations),
                "human_approval_count": human_count,
                "manual_resume_count": manual_resume_count,
                "manual_state_edit_count": state_edit_count,
                "unresolved_approval_count": unresolved_count,
                "low_risk_allow_count": low_risk_allows,
                "high_risk_deny_count": high_risk_denies,
                "effect_counts": dict(Counter(item.effect for item in observations)),
                "risk_counts": dict(Counter(item.risk for item in observations)),
                "observations": [item.to_dict() for item in observations],
                "declared_policy": policy,
            }
        )
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _policy_findings(policy: Mapping[str, Any]) -> list[Finding]:
        if not policy:
            return []
        findings: list[Finding] = []
        mode = str(policy.get("mode") or policy.get("permission_mode") or "").lower()
        if mode not in {"sealed", "sealed_autonomous"}:
            findings.append(
                Finding(
                    code="autonomy.policy_not_sealed",
                    severity=Severity.BLOCKER,
                    summary="Formal benchmark policy is not sealed autonomous.",
                    detail=mode,
                )
            )
        if policy.get("interactive") is True:
            findings.append(
                Finding(
                    code="autonomy.policy_interactive",
                    severity=Severity.BLOCKER,
                    summary="Sealed benchmark policy must be non-interactive.",
                )
            )
        ask_handling = str(policy.get("ask_handling") or policy.get("ask_effect") or "").lower()
        if ask_handling and ask_handling not in {"deny_recover", "deny_then_recover", "deny", "replan"}:
            findings.append(
                Finding(
                    code="autonomy.ask_policy_invalid",
                    severity=Severity.BLOCKER,
                    summary="ASK must deterministically deny and enter recovery/replan.",
                    detail=ask_handling,
                )
            )
        return findings

    @staticmethod
    def _observation_findings(observations: Sequence[AutonomyObservation]) -> list[Finding]:
        findings: list[Finding] = []
        for item in observations:
            if item.risk in _HIGH_RISK_LEVELS and item.effect in _LOW_RISK_EFFECTS:
                findings.append(
                    Finding(
                        code="autonomy.high_risk_allowed",
                        severity=Severity.BLOCKER,
                        summary="Sealed policy allowed a high-risk or unknown action.",
                        detail=f"tool={item.tool_name}; risk={item.risk}; effect={item.effect}",
                        location=item.event_id,
                    )
                )
            if item.risk in _LOW_RISK_LEVELS and item.effect in _ASK_EFFECTS:
                findings.append(
                    Finding(
                        code="autonomy.low_risk_friction",
                        severity=Severity.ERROR,
                        summary="Deterministic low-risk action caused an approval interruption.",
                        detail=f"tool={item.tool_name}; effect={item.effect}",
                        location=item.event_id,
                    )
                )
            if item.effect in _ASK_EFFECTS:
                findings.append(
                    Finding(
                        code="autonomy.ask_not_terminally_resolved",
                        severity=Severity.BLOCKER,
                        summary="ASK remained in the sealed trace instead of deterministic deny/recovery.",
                        location=item.event_id,
                    )
                )
        return findings

    @staticmethod
    def _recovery_chain_findings(observations: Sequence[AutonomyObservation]) -> list[Finding]:
        by_request: dict[str, list[AutonomyObservation]] = defaultdict(list)
        for item in observations:
            if item.request_id:
                by_request[item.request_id].append(item)
        findings: list[Finding] = []
        for request_id, items in by_request.items():
            denied = [item for item in items if item.effect in _DENY_EFFECTS and item.risk in _HIGH_RISK_LEVELS]
            if not denied:
                continue
            recovered = any(item.recovery_state in _TERMINAL_RECOVERY for item in items)
            recovered = recovered or any(
                any(token in item.event_type for token in ("recovery", "replan", "reroute", "abort", "cancel"))
                for item in items
            )
            if not recovered:
                findings.append(
                    Finding(
                        code="autonomy.denial_without_recovery",
                        severity=Severity.BLOCKER,
                        summary="High-risk denial has no correlated recovery/replan terminal state.",
                        detail=request_id,
                    )
                )
        return findings
