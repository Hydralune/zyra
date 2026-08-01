from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from zyra_orchestration.topology_policy import (
    ContractHeader,
    FrozenDict,
    NeuroSymbolicEvidenceBundle,
    PolicyDecisionReceipt,
    PhysicalDispatchReceipt,
    StableArtifactRef,
    TopologyProposalArtifact,
)
from zyra_scheduler import PhysicalDispatchReceiptValidator

from .contracts import canonical_digest
from .metrics import (
    ADAPTIVE_DEPTH_RECEIPTS,
    COMMUNICATION_RECEIPTS,
    CONTINUITY_RECEIPTS,
    EARLY_EXIT_RECEIPTS,
    PHYSICAL_DISPATCH_RECEIPTS,
    POLICY_DECISIONS,
    POLICY_OUTCOMES,
    READINESS_REPORTS,
    RECEIPT_KINDS,
    SYMBOLIC_BUNDLES,
    TOPOLOGY_PROPOSALS,
    Phase2MetricError,
    ReceiptResolution,
)
from .report import Phase2MetricReport


_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMPLETION_GATE_SCHEMA = "zyra.production-adaptive-depth-completion-gate/v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_value(event: Any, name: str) -> Any:
    if isinstance(event, Mapping):
        return event.get(name)
    return getattr(event, name, None)


def _event_payload(event: Any) -> Mapping[str, Any]:
    return _mapping(_event_value(event, "payload"))


def _event_type(event: Any) -> str:
    value = _event_value(event, "event_type")
    return str(getattr(value, "value", value) or "")


def _owner_digest(value: Mapping[str, Any]) -> dict[str, Any]:
    selected = dict(value)
    supplied = str(selected.get("digest") or "")
    unsigned = dict(selected)
    unsigned.pop("digest", None)
    if not supplied or canonical_digest(unsigned) != supplied:
        raise Phase2MetricError(
            "metric_receipt_digest_missing",
            "runtime owner did not issue a valid communication receipt digest",
        )
    return selected


def _validated_readiness(
    report: Mapping[str, Any],
    expected_digest: str,
) -> dict[str, Any]:
    selected = dict(report)
    supplied = str(selected.get("report_digest") or "")
    unsigned = dict(selected)
    unsigned.pop("report_digest", None)
    if (
        not supplied
        or supplied != canonical_digest(unsigned)
        or supplied != expected_digest
    ):
        raise Phase2MetricError(
            "metric_readiness_digest_mismatch",
            "runtime readiness input is not bound to the routed report",
        )
    return selected


def _symbolic_bundle(
    *,
    proposal_document: Mapping[str, Any],
    decision_document: Mapping[str, Any],
    outcome_document: Mapping[str, Any],
    physical_document: Mapping[str, Any],
) -> dict[str, Any]:
    proposal = TopologyProposalArtifact.from_dict(proposal_document)
    decision = PolicyDecisionReceipt.from_dict(decision_document)
    physical_payload = _mapping(physical_document.get("payload"))
    commit = dict(decision.graph_commit)
    commit_present = bool(commit) and str(commit.get("status") or "") == "committed"
    failed_constraints = tuple(
        item for item in decision.constraint_results if not item.passed
    )
    unsafe_commit = commit_present and bool(failed_constraints)
    if unsafe_commit:
        raise Phase2MetricError(
            "metric_symbolic_unsafe_commit",
            "a failed symbolic constraint reached canonical commit",
        )
    result = (
        "rejected"
        if decision.disposition.value in {"reject", "conflict"}
        else "repaired"
        if decision.disposition.value in {"project", "rebase"}
        else "accepted"
    )
    proposal_ref = StableArtifactRef(
        ref_id=proposal.proposal_id,
        uri=f"event-contract://{proposal.header.source_event_id}/{proposal.proposal_id}",
        digest=proposal.digest,
    )
    verifier = _mapping(physical_payload.get("verifier_ref"))
    outcome_digest = str(
        outcome_document.get("digest") or canonical_digest(outcome_document)
    )
    header = ContractHeader(
        contract_id=f"runtime-symbolic:{decision.decision_id}",
        created_at=decision.header.created_at,
        source_event_id=decision.header.source_event_id,
        correlation_id=decision.header.correlation_id,
        causation_id=decision.header.contract_id,
        mechanism_id="phase2_symbolic_metric_projection",
        mechanism_version="phase2_strongest_v1",
        input_version=NeuroSymbolicEvidenceBundle.SCHEMA_VERSION,
        idempotency_key=f"runtime-symbolic:{decision.digest}",
        configuration_digest=decision.header.configuration_digest,
    )
    bundle = NeuroSymbolicEvidenceBundle(
        header=header,
        proposal_signal_mode="deterministic_only",
        proposal_ref=proposal_ref,
        model_observation_refs=(),
        constraint_results=decision.constraint_results,
        projected_delta_ref=decision.delta_id or "no-delta",
        commit_or_no_commit=FrozenDict(
            {
                "result": result,
                "decision_disposition": decision.disposition.value,
                "proposal_digest": proposal.digest,
                "decision_digest": decision.digest,
                "canonical_commit_present": commit_present,
                "canonical_commit": commit,
                "constraint_failure_codes": [
                    item.reason_code for item in failed_constraints
                ],
                "unsafe_commit": unsafe_commit,
                "projector_bypass_production_reachable": False,
                "outcome_digest": outcome_digest,
                "physical_dispatch_digest": str(
                    physical_document.get("digest") or ""
                ),
                "symbolic_owner_controls_commit": True,
            }
        ),
        permission_ref=str(physical_payload.get("permission_ref") or ""),
        lease_ref=str(physical_payload.get("lease_id") or ""),
        verification_ref=str(
            verifier.get("uri") or verifier.get("ref_id") or ""
        ),
    )
    return bundle.to_dict()


class CanonicalRuntimeReceiptResolver:
    """Resolve metrics exclusively from one completed production run.

    The adapter accepts runtime-owned event and state projections, validates
    their cross-contract bindings, and marks every absent source unavailable.
    It never manufactures a successful sample for a missing receipt family.
    """

    def __init__(
        self,
        *,
        run_id: str,
        task_id: str,
        events: Sequence[Any],
        canonical_events: Sequence[Any],
        communication_receipts: Sequence[Mapping[str, Any]],
        physical_dispatch_receipts: Sequence[Mapping[str, Any]],
        readiness_report: Mapping[str, Any],
        decision_records: Sequence[Any] = (),
        disconnected: Iterable[str] = (),
    ) -> None:
        self.run_id = str(run_id)
        self.task_id = str(task_id)
        self._disconnected = frozenset(str(item) for item in disconnected)
        unknown = self._disconnected - set(RECEIPT_KINDS)
        if unknown:
            raise Phase2MetricError(
                "metric_receipt_kind_unknown",
                f"unknown canonical receipt kinds: {sorted(unknown)}",
            )
        selected_events = tuple(
            event
            for event in events
            if str(_event_value(event, "run_id") or "") == self.run_id
            and str(_event_value(event, "task_id") or "") == self.task_id
        )
        canonical_selected_events = tuple(
            event
            for event in canonical_events
            if str(_event_value(event, "run_id") or "") == self.run_id
            and str(_event_value(event, "task_id") or "") == self.task_id
        )
        self._event_refs = tuple(
            sorted(
                {
                    str(_event_value(event, "event_id") or "")
                    for event in canonical_selected_events
                    if str(_event_value(event, "event_id") or "")
                }
            )
        )
        source_event_refs = {
            str(_event_value(event, "event_id") or "")
            for event in selected_events
            if str(_event_value(event, "event_id") or "")
        }
        if not source_event_refs or not source_event_refs.issubset(
            set(self._event_refs)
        ):
            raise Phase2MetricError(
                "metric_runtime_event_unadmitted",
                "runtime receipt events are not admitted by the canonical event spine",
            )
        admitted_contract_digests = {
            str(_event_payload(event).get("policy_contract_digest") or "")
            for event in canonical_selected_events
            if _event_payload(event).get("schema")
            == "zyra.policy-contract-event/v1"
        }
        topology_sets: list[
            tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any], str]
        ] = []
        gates: list[Mapping[str, Any]] = []
        for event in selected_events:
            payload = _event_payload(event)
            if payload.get("schema") == _COMPLETION_GATE_SCHEMA:
                gates.append(payload)
            if _event_type(event) != "topology_route":
                continue
            policy = _mapping(payload.get("topology_policy"))
            if policy.get("committed") is not True or policy.get("used_baseline") is True:
                continue
            topology = _mapping(policy.get("topology_result"))
            composition = _mapping(topology.get("composition"))
            proposal = _mapping(composition.get("proposal"))
            decision = _mapping(topology.get("decision_receipt"))
            outcome = _mapping(topology.get("outcome"))
            if proposal and decision and outcome:
                topology_sets.append(
                    (
                        proposal,
                        decision,
                        outcome,
                        str(policy.get("readiness_report_digest") or ""),
                    )
                )
        physical = tuple(dict(item) for item in physical_dispatch_receipts)
        for document in physical:
            try:
                receipt = PhysicalDispatchReceipt.from_dict(document)
                validation = PhysicalDispatchReceiptValidator().validate(
                    receipt
                )
            except Exception as error:
                raise Phase2MetricError(
                    "metric_receipt_contract_invalid",
                    f"physical dispatch contract admission failed: {error}",
                ) from error
            if not validation.real_gate_closed:
                raise Phase2MetricError(
                    "metric_physical_dispatch_gate_open",
                    "production physical dispatch failed its real gate: "
                    + ",".join(validation.blockers),
                )
        expected_readiness = topology_sets[-1][3] if topology_sets else ""
        readiness = (
            _validated_readiness(readiness_report, expected_readiness)
            if expected_readiness
            else {}
        )
        values: dict[str, tuple[Any, ...]] = {
            COMMUNICATION_RECEIPTS: tuple(
                _owner_digest(item) for item in communication_receipts
            ),
            TOPOLOGY_PROPOSALS: tuple(item[0] for item in topology_sets),
            POLICY_DECISIONS: tuple(item[1] for item in topology_sets),
            POLICY_OUTCOMES: tuple(item[2] for item in topology_sets),
            READINESS_REPORTS: (readiness,) if readiness else (),
            CONTINUITY_RECEIPTS: tuple(
                _mapping(item.get("continuity_receipt"))
                for item in gates
                if _mapping(item.get("continuity_receipt"))
            ),
            EARLY_EXIT_RECEIPTS: tuple(
                _mapping(item.get("early_exit_receipt"))
                for item in gates
                if _mapping(item.get("early_exit_receipt"))
            ),
            ADAPTIVE_DEPTH_RECEIPTS: tuple(
                _mapping(item.get("adaptive_depth_receipt"))
                for item in gates
                if _mapping(item.get("adaptive_depth_receipt"))
            ),
            PHYSICAL_DISPATCH_RECEIPTS: physical,
        }
        decision_ids = {
            str(
                (
                    item.get("decision_id")
                    if isinstance(item, Mapping)
                    else getattr(item, "decision_id", "")
                )
                or ""
            )
            for item in decision_records
        }
        admitted_usage_refs = set(self._event_refs)
        admitted_usage_refs.update(
            str(item.get("delivery_receipt_ref") or "")
            for item in communication_receipts
        )
        for continuity_document in values[CONTINUITY_RECEIPTS]:
            payload = _mapping(continuity_document.get("payload"))
            downstream = str(payload.get("downstream_decision_ref") or "")
            if not downstream or downstream not in decision_ids:
                raise Phase2MetricError(
                    "metric_continuity_decision_unresolved",
                    "continuity receipt does not resolve to a task decision",
                )
            facts = _mapping(payload.get("critical_fact_results"))
            for raw in facts.values():
                fact = _mapping(raw)
                usage_refs = {
                    str(item)
                    for item in fact.get("usage_event_refs") or ()
                    if str(item)
                }
                if fact.get("consumed") is True and (
                    not usage_refs
                    or not usage_refs.issubset(admitted_usage_refs)
                ):
                    raise Phase2MetricError(
                        "metric_continuity_usage_unresolved",
                        "continuity actual-use refs do not resolve in this run",
                    )
        if topology_sets and physical:
            proposal, decision, outcome, _ = topology_sets[-1]
            values[SYMBOLIC_BUNDLES] = (
                _symbolic_bundle(
                    proposal_document=proposal,
                    decision_document=decision,
                    outcome_document=outcome,
                    physical_document=physical[-1],
                ),
            )
        else:
            values[SYMBOLIC_BUNDLES] = ()
        owner_bound_contracts = (
            *values[TOPOLOGY_PROPOSALS],
            *values[POLICY_DECISIONS],
            *values[POLICY_OUTCOMES],
            *values[CONTINUITY_RECEIPTS],
            *values[PHYSICAL_DISPATCH_RECEIPTS],
        )
        missing_owner_digests = sorted(
            {
                str(document.get("digest") or "")
                for document in owner_bound_contracts
                if str(document.get("digest") or "")
            }
            - admitted_contract_digests
        )
        if missing_owner_digests:
            raise Phase2MetricError(
                "metric_contract_event_unadmitted",
                "runtime contracts are missing canonical artifact admission events",
            )
        self._values = values

    def canonical_transition_count(self) -> int:
        """Return the canonical event-spine evidence volume for this run."""

        return len(self._event_refs)

    def admission_context(self) -> Mapping[str, Any]:
        return {
            "schema_version": "zyra.phase2-runtime-source-admission/v1",
            "canonical_owner": (
                "RuntimeEventSpine+TaskState+ArtifactStore"
            ),
            "event_refs": list(self._event_refs),
            "readiness_report_digest": str(
                _mapping(
                    next(
                        iter(self._values.get(READINESS_REPORTS, ())),
                        {},
                    )
                ).get("report_digest")
                or ""
            ),
            "physical_dispatch_digests": [
                str(item.get("digest") or "")
                for item in self._values.get(
                    PHYSICAL_DISPATCH_RECEIPTS,
                    (),
                )
            ],
        }

    def resolve(self, kind: str) -> ReceiptResolution:
        if kind not in RECEIPT_KINDS:
            return ReceiptResolution(
                kind=kind,
                available=False,
                reason="unknown_receipt_kind",
            )
        if kind in self._disconnected:
            return ReceiptResolution(
                kind=kind,
                available=False,
                reason="receipt_resolver_disconnected",
            )
        receipts = self._values.get(kind, ())
        return ReceiptResolution(
            kind=kind,
            available=bool(receipts),
            receipts=receipts,
            reason="" if receipts else "canonical_runtime_receipt_missing",
        )


def write_phase2_metric_report(
    report: Phase2MetricReport,
    *,
    root: Path,
    report_id: str,
) -> Path:
    """Atomically publish a target-named report for the GET-only API."""

    if not _REPORT_ID.fullmatch(str(report_id or "")):
        raise ValueError("invalid Phase 2 metric report id")
    selected_root = root.resolve()
    selected_root.mkdir(parents=True, exist_ok=True)
    destination = (selected_root / f"{report_id}.json").resolve()
    if destination.parent != selected_root:
        raise ValueError("Phase 2 metric report path escapes its owner root")
    temporary = destination.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, destination)
    return destination


__all__ = [
    "CanonicalRuntimeReceiptResolver",
    "write_phase2_metric_report",
]
