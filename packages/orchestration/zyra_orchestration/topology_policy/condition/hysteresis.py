from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..contracts import thaw_json


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CARDEdgeHysteresisState:
    edge_id: str
    active: bool
    last_changed_at: str
    last_score: float
    pending_action: str = ""
    confirmations: int = 0

    def __post_init__(self) -> None:
        if not self.edge_id:
            raise ValueError("CARD hysteresis state requires edge_id")
        _time(self.last_changed_at)
        if self.pending_action not in {"", "add", "drop"}:
            raise ValueError("CARD pending hysteresis action must be add or drop")
        object.__setattr__(self, "confirmations", max(0, int(self.confirmations)))
        object.__setattr__(self, "last_score", round(float(self.last_score), 8))

    def to_dict(self) -> dict[str, Any]:
        return thaw_json(self)


@dataclass(frozen=True, slots=True)
class CARDHysteresisDecision:
    requested_action: str
    applied_action: str
    switch_allowed: bool
    reason_code: str
    dwell_seconds: float
    switch_cost: float
    prior_state: CARDEdgeHysteresisState
    next_state: CARDEdgeHysteresisState

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_action": self.requested_action,
            "applied_action": self.applied_action,
            "switch_allowed": self.switch_allowed,
            "reason_code": self.reason_code,
            "dwell_seconds": round(self.dwell_seconds, 8),
            "switch_cost": round(self.switch_cost, 8),
            "prior_state": self.prior_state.to_dict(),
            "next_state": self.next_state.to_dict(),
        }


class CARDHysteresisController:
    """Pure edge-switch gate; callers own persistence of the returned state."""

    @staticmethod
    def evaluate(
        *,
        edge_id: str,
        requested_action: str,
        score: float,
        observed_at: str,
        active_before: bool,
        last_topology_change_at: str,
        state: CARDEdgeHysteresisState | None,
        hard_constraint: bool,
        minimum_dwell_seconds: int,
        switch_confirmations: int,
        reweight_epsilon: float,
        switch_cost: float,
    ) -> CARDHysteresisDecision:
        if requested_action not in {"add", "drop", "reweight", "hold", "reject"}:
            raise ValueError("unsupported CARD hysteresis action")
        prior = state or CARDEdgeHysteresisState(
            edge_id=edge_id,
            active=active_before,
            last_changed_at=last_topology_change_at,
            last_score=0.0,
        )
        if prior.edge_id != edge_id:
            raise ValueError("CARD hysteresis state belongs to another edge")
        observed = _time(observed_at)
        dwell = max(0.0, (observed - _time(prior.last_changed_at)).total_seconds())
        rounded_score = round(float(score), 8)

        if requested_action in {"hold", "reject"}:
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=prior.active,
                last_changed_at=prior.last_changed_at,
                last_score=rounded_score,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action=requested_action,
                switch_allowed=False,
                reason_code=(
                    "residual_rejected"
                    if requested_action == "reject"
                    else "within_hysteresis_band"
                ),
                dwell_seconds=dwell,
                switch_cost=0.0,
                prior_state=prior,
                next_state=next_state,
            )

        if requested_action == "reweight":
            if abs(rounded_score - prior.last_score) < max(0.0, reweight_epsilon):
                applied = "hold"
                reason = "reweight_below_epsilon"
            else:
                applied = "reweight"
                reason = "directional_residual_changed"
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=prior.active,
                last_changed_at=prior.last_changed_at,
                last_score=rounded_score,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action=applied,
                switch_allowed=False,
                reason_code=reason,
                dwell_seconds=dwell,
                switch_cost=0.0,
                prior_state=prior,
                next_state=next_state,
            )

        desired_active = requested_action == "add"
        if prior.active == desired_active:
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=prior.active,
                last_changed_at=prior.last_changed_at,
                last_score=rounded_score,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action="reweight" if prior.active else "hold",
                switch_allowed=False,
                reason_code="edge_already_in_requested_state",
                dwell_seconds=dwell,
                switch_cost=0.0,
                prior_state=prior,
                next_state=next_state,
            )

        if hard_constraint:
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=desired_active,
                last_changed_at=observed_at,
                last_score=rounded_score,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action=requested_action,
                switch_allowed=True,
                reason_code="hard_constraint_bypasses_hysteresis",
                dwell_seconds=dwell,
                switch_cost=round(max(0.0, switch_cost), 8),
                prior_state=prior,
                next_state=next_state,
            )

        if dwell < max(0, minimum_dwell_seconds):
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=prior.active,
                last_changed_at=prior.last_changed_at,
                last_score=rounded_score,
                pending_action=requested_action,
                confirmations=1,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action="hold",
                switch_allowed=False,
                reason_code="minimum_dwell_not_met",
                dwell_seconds=dwell,
                switch_cost=round(max(0.0, switch_cost), 8),
                prior_state=prior,
                next_state=next_state,
            )

        confirmations = (
            prior.confirmations + 1
            if prior.pending_action == requested_action
            else 1
        )
        required = max(1, int(switch_confirmations))
        if confirmations < required:
            next_state = CARDEdgeHysteresisState(
                edge_id=edge_id,
                active=prior.active,
                last_changed_at=prior.last_changed_at,
                last_score=rounded_score,
                pending_action=requested_action,
                confirmations=confirmations,
            )
            return CARDHysteresisDecision(
                requested_action=requested_action,
                applied_action="hold",
                switch_allowed=False,
                reason_code="switch_confirmation_pending",
                dwell_seconds=dwell,
                switch_cost=round(max(0.0, switch_cost), 8),
                prior_state=prior,
                next_state=next_state,
            )

        next_state = CARDEdgeHysteresisState(
            edge_id=edge_id,
            active=desired_active,
            last_changed_at=observed_at,
            last_score=rounded_score,
        )
        return CARDHysteresisDecision(
            requested_action=requested_action,
            applied_action=requested_action,
            switch_allowed=True,
            reason_code="hysteresis_confirmed",
            dwell_seconds=dwell,
            switch_cost=round(max(0.0, switch_cost), 8),
            prior_state=prior,
            next_state=next_state,
        )
