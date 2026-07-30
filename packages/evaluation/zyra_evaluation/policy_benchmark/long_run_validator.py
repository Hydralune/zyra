from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EVIDENCE_INDEX_SCHEMA = "zyra.phase2-sealed-evidence-index/v1"
VALIDATION_SCHEMA = "zyra.phase2-sealed-long-run-validation/v1"
TRANSITION_INDEX_SCHEMA = "zyra.phase2-canonical-transition-index/v1"
EXCLUDED_EVENT_TYPES = frozenset(
    {
        "heartbeat",
        "worker_heartbeat",
        "worker_health",
        "log",
        "debug_log",
        "replay",
        "receipt_replayed",
        "noop",
        "no_op",
        "ui_repaint",
        "ui_render",
        "poll",
        "poll_completed",
        "sse_heartbeat",
    }
)
VALID_EFFECTS = frozenset(
    {
        "state_mutation",
        "route",
        "placement",
        "lease",
        "tool",
        "provider",
        "verification",
        "permission",
        "compact_restore",
        "fault",
        "recovery",
        "artifact",
        "obligation",
        "delivery",
        "topology",
        "memory",
    }
)
REQUIRED_EFFECTS = frozenset(
    {
        "state_mutation",
        "route",
        "placement",
        "tool",
        "verification",
        "permission",
        "compact_restore",
        "fault",
        "recovery",
        "artifact",
        "delivery",
        "topology",
        "memory",
    }
)
REQUIRED_LANES = frozenset({"local", "edge", "cloud"})
REQUIRED_DISABLE_GATES = frozenset(
    {
        "memory_continuity",
        "symbolic_projector",
        "loopx",
        "dynamic_topology",
        "operator_selection",
        "physical_dispatch",
    }
)


class LongRunValidationError(RuntimeError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise LongRunValidationError(f"cannot read JSON evidence: {path}") from error
    if not isinstance(value, dict):
        raise LongRunValidationError(f"JSON evidence must be an object: {path}")
    return value


def load_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    output: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise LongRunValidationError(f"cannot read JSONL evidence: {path}") from error
    for line_number, raw in enumerate(lines, start=1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise LongRunValidationError(
                f"invalid JSONL at {path}:{line_number}"
            ) from error
        if not isinstance(value, dict):
            raise LongRunValidationError(
                f"JSONL member must be an object at {path}:{line_number}"
            )
        output.append(value)
    return tuple(output)


def _member(root: Path, value: Any, *, label: str) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise LongRunValidationError(f"missing evidence member: {label}")
    selected = (root / raw).resolve()
    try:
        selected.relative_to(root.resolve())
    except ValueError as error:
        raise LongRunValidationError(
            f"evidence member escapes bundle root: {label}"
        ) from error
    if not selected.is_file():
        raise LongRunValidationError(f"evidence member is unavailable: {label}")
    return selected


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _declared_effect(event: Mapping[str, Any]) -> str:
    payload = _mapping(event.get("payload"))
    metadata = _mapping(event.get("metadata"))
    return str(
        metadata.get("semantic_effect")
        or payload.get("semantic_effect")
        or event.get("semantic_effect")
        or ""
    ).strip().casefold()


def _mutation(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _mapping(event.get("payload"))
    for key in (
        "mutation",
        "delta",
        "state",
        "route",
        "receipt",
        "result",
        "artifact",
        "permission",
        "recovery",
        "memory",
    ):
        value = payload.get(key)
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


@dataclass(frozen=True, slots=True)
class TransitionValidation:
    transitions: tuple[dict[str, Any], ...]
    excluded: tuple[dict[str, Any], ...]
    invalid: tuple[dict[str, Any], ...]
    effect_counts: dict[str, int]
    initial_state_digest: str
    final_state_digest: str

    @property
    def valid_count(self) -> int:
        return len(self.transitions)

    def index(self, *, run_id: str, task_id: str) -> dict[str, Any]:
        body = {
            "schema": TRANSITION_INDEX_SCHEMA,
            "run_id": run_id,
            "task_id": task_id,
            "initial_state_digest": self.initial_state_digest,
            "final_state_digest": self.final_state_digest,
            "valid_transition_count": self.valid_count,
            "excluded_count": len(self.excluded),
            "invalid_count": len(self.invalid),
            "effect_counts": dict(self.effect_counts),
            "transitions": list(self.transitions),
            "excluded": list(self.excluded),
            "invalid": list(self.invalid),
        }
        body["index_digest"] = canonical_digest(body)
        return body


class IndependentTransitionValidator:
    """Reconstruct effective transitions from raw canonical events.

    The validator never consumes the runner's claimed count.  It rejects
    duplicate/no-op/replay evidence and independently constructs a hash-linked
    before/after state projection for every admitted canonical event.
    """

    def validate(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        run_id: str,
        task_id: str,
    ) -> TransitionValidation:
        selected = tuple(dict(item) for item in events)
        initial = canonical_digest(
            {
                "schema": "zyra.phase2-validation-state/v1",
                "run_id": run_id,
                "task_id": task_id,
                "revision": 0,
            }
        )
        previous_state = initial
        previous_event_id = ""
        seen_event_ids: set[str] = set()
        seen_semantics: set[str] = set()
        transitions: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        invalid: list[dict[str, Any]] = []
        effects: Counter[str] = Counter()
        for observed_sequence, event in enumerate(selected, start=1):
            event_id = str(event.get("event_id") or "").strip()
            event_type = str(event.get("event_type") or "").strip().casefold()
            event_run_id = str(event.get("run_id") or "").strip()
            event_task_id = str(event.get("task_id") or "").strip()
            declared_sequence = int(event.get("sequence") or 0)
            causation_id = str(
                event.get("causation_id")
                or _mapping(event.get("payload")).get("causation_id")
                or ""
            ).strip()
            effect = _declared_effect(event)
            mutation = _mutation(event)
            reasons: list[str] = []
            if not event_id:
                reasons.append("event_id_missing")
            if event_id in seen_event_ids:
                reasons.append("duplicate_event_id")
            if not event_type:
                reasons.append("event_type_missing")
            if event_run_id != run_id:
                reasons.append("run_scope_mismatch")
            if event_task_id != task_id:
                reasons.append("task_scope_mismatch")
            if declared_sequence != observed_sequence:
                reasons.append("sequence_mismatch")
            if observed_sequence > 1 and causation_id != previous_event_id:
                reasons.append("causation_mismatch")
            if event_type in EXCLUDED_EVENT_TYPES:
                excluded.append(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "reason": "excluded_event_type",
                    }
                )
                if event_id:
                    seen_event_ids.add(event_id)
                    previous_event_id = event_id
                continue
            if effect not in VALID_EFFECTS:
                reasons.append("semantic_effect_invalid")
            if not mutation:
                reasons.append("canonical_mutation_missing")
            semantic = canonical_digest(
                {
                    "event_type": event_type,
                    "effect": effect,
                    "stage": str(
                        event.get("stage")
                        or _mapping(event.get("metadata")).get("stage")
                        or ""
                    ),
                    "mutation": mutation,
                    "run_id": run_id,
                    "task_id": task_id,
                }
            )
            if semantic in seen_semantics:
                reasons.append("duplicate_semantic_effect")
            if reasons:
                invalid.append(
                    {
                        "event_id": event_id,
                        "event_type": event_type,
                        "reason_codes": sorted(set(reasons)),
                    }
                )
            else:
                after_state = canonical_digest(
                    {
                        "before": previous_state,
                        "canonical_event_digest": canonical_digest(event),
                        "semantic_digest": semantic,
                        "revision": len(transitions) + 1,
                    }
                )
                transition = {
                    "transition_id": (
                        f"transition-{len(transitions) + 1:06d}-{semantic[:16]}"
                    ),
                    "canonical_event_id": event_id,
                    "canonical_event_type": event_type,
                    "canonical_event_digest": canonical_digest(event),
                    "effect": effect,
                    "semantic_digest": semantic,
                    "state_before_digest": previous_state,
                    "state_after_digest": after_state,
                    "causation_id": causation_id,
                    "sequence": len(transitions) + 1,
                }
                transitions.append(transition)
                effects[effect] += 1
                previous_state = after_state
                seen_semantics.add(semantic)
            if event_id:
                seen_event_ids.add(event_id)
                previous_event_id = event_id
        return TransitionValidation(
            transitions=tuple(transitions),
            excluded=tuple(excluded),
            invalid=tuple(invalid),
            effect_counts=dict(effects),
            initial_state_digest=initial,
            final_state_digest=previous_state,
        )


class SealedLongRunValidator:
    def validate(self, evidence_index: str | Path) -> dict[str, Any]:
        index_path = Path(evidence_index).resolve()
        root = index_path.parent
        index = load_json(index_path)
        blockers: list[str] = []
        if index.get("schema") != EVIDENCE_INDEX_SCHEMA:
            blockers.append("evidence_index_schema")
        manifest_path = _member(
            root,
            index.get("sealed_manifest"),
            label="sealed_manifest",
        )
        manifest = load_json(manifest_path)
        manifest_digest = file_digest(manifest_path)
        if str(index.get("sealed_manifest_digest") or "") != manifest_digest:
            blockers.append("sealed_manifest_digest")
        candidate_commit = str(manifest.get("candidate_commit") or "")
        if len(candidate_commit) != 40:
            blockers.append("candidate_commit")
        minimum = int(manifest.get("minimum_valid_transitions_per_run") or 0)
        if minimum < 2_000:
            blockers.append("minimum_valid_transitions")
        manifest_runs = {
            str(item.get("run_key") or ""): dict(item)
            for item in _sequence(manifest.get("runs"))
            if isinstance(item, Mapping)
        }
        run_reports: list[dict[str, Any]] = []
        domains: set[str] = set()
        for value in _sequence(index.get("runs")):
            if not isinstance(value, Mapping):
                blockers.append("run_member_invalid")
                continue
            run = dict(value)
            report = self._validate_run(
                root=root,
                run=run,
                manifest_run=manifest_runs.get(str(run.get("run_key") or ""), {}),
                minimum=minimum,
                candidate_commit=candidate_commit,
            )
            run_reports.append(report)
            domains.add(str(report.get("domain") or ""))
            blockers.extend(
                f"{report.get('run_key')}:{item}"
                for item in report.get("blockers", ())
            )
        if len(run_reports) != 2:
            blockers.append("run_count")
        if domains != {"software_delivery", "cross_source_research"}:
            blockers.append("cross_domain_coverage")
        report = {
            "schema": VALIDATION_SCHEMA,
            "valid": not blockers,
            "evidence_index": index_path.name,
            "evidence_index_digest": file_digest(index_path),
            "sealed_manifest": manifest_path.name,
            "sealed_manifest_digest": manifest_digest,
            "candidate_commit": candidate_commit,
            "run_count": len(run_reports),
            "minimum_valid_transitions_per_run": minimum,
            "runs": run_reports,
            "blockers": sorted(set(blockers)),
        }
        report["validation_digest"] = canonical_digest(report)
        return report

    def _validate_run(
        self,
        *,
        root: Path,
        run: dict[str, Any],
        manifest_run: dict[str, Any],
        minimum: int,
        candidate_commit: str,
    ) -> dict[str, Any]:
        blockers: list[str] = []
        run_key = str(run.get("run_key") or "")
        run_id = str(run.get("run_id") or "")
        task_id = str(run.get("task_id") or "")
        domain = str(run.get("domain") or "")
        if not manifest_run:
            blockers.append("manifest_run_binding")
        if domain != str(manifest_run.get("domain") or ""):
            blockers.append("manifest_domain_binding")
        if str(run.get("candidate_commit") or "") != candidate_commit:
            blockers.append("candidate_commit_binding")
        raw_path = _member(root, run.get("raw_events"), label=f"{run_key}.raw_events")
        if str(run.get("raw_events_digest") or "") != file_digest(raw_path):
            blockers.append("raw_events_digest")
        raw_events = load_jsonl(raw_path)
        transitions = IndependentTransitionValidator().validate(
            raw_events,
            run_id=run_id,
            task_id=task_id,
        )
        transition_index = transitions.index(run_id=run_id, task_id=task_id)
        if transitions.invalid:
            blockers.append("invalid_transition_candidates")
        if transitions.valid_count < minimum:
            blockers.append("valid_transition_minimum")
        if REQUIRED_EFFECTS - set(transitions.effect_counts):
            blockers.append("required_effect_coverage")
        claimed_path = _member(
            root,
            run.get("transition_index"),
            label=f"{run_key}.transition_index",
        )
        claimed = load_json(claimed_path)
        if claimed != transition_index:
            blockers.append("transition_index_recompute_mismatch")
        gates_path = _member(
            root,
            run.get("hard_gate_bundle"),
            label=f"{run_key}.hard_gate_bundle",
        )
        if str(run.get("hard_gate_bundle_digest") or "") != file_digest(gates_path):
            blockers.append("hard_gate_bundle_digest")
        gates = load_json(gates_path)
        blockers.extend(self._hard_gate_blockers(gates))
        artifact_path = _member(
            root,
            run.get("final_artifact"),
            label=f"{run_key}.final_artifact",
        )
        verifier_path = _member(
            root,
            run.get("final_verifier"),
            label=f"{run_key}.final_verifier",
        )
        verifier = load_json(verifier_path)
        if verifier.get("passed") is not True:
            blockers.append("final_verifier")
        if str(verifier.get("artifact_digest") or "") != file_digest(artifact_path):
            blockers.append("final_artifact_binding")
        if str(verifier.get("run_id") or "") != run_id:
            blockers.append("final_verifier_run_binding")
        return {
            "run_key": run_key,
            "run_id": run_id,
            "task_id": task_id,
            "domain": domain,
            "valid": not blockers,
            "valid_transition_count": transitions.valid_count,
            "excluded_transition_count": len(transitions.excluded),
            "invalid_transition_count": len(transitions.invalid),
            "effect_counts": transitions.effect_counts,
            "initial_state_digest": transitions.initial_state_digest,
            "final_state_digest": transitions.final_state_digest,
            "transition_index_digest": transition_index["index_digest"],
            "hard_gate_bundle_digest": file_digest(gates_path),
            "final_artifact_digest": file_digest(artifact_path),
            "final_verifier_digest": file_digest(verifier_path),
            "blockers": sorted(set(blockers)),
        }

    @staticmethod
    def _hard_gate_blockers(gates: Mapping[str, Any]) -> list[str]:
        blockers: list[str] = []
        if gates.get("schema") != "zyra.phase2-sealed-hard-gates/v1":
            blockers.append("hard_gate_schema")
        exact_zero = (
            "human_intervention_count",
            "early_exit_false_positive",
            "superseded_requirement_execution",
            "critical_retrieval_without_provenance",
            "duplicate_completed_work",
            "duplicate_commit",
            "duplicate_claim",
            "duplicate_spend",
            "duplicate_lease",
            "duplicate_side_effect",
            "privacy_permission_violation",
            "unsafe_commit",
        )
        for name in exact_zero:
            if int(gates.get(name) or 0) != 0:
                blockers.append(name)
        exact_one = (
            "critical_fact_recall",
            "obligation_retention",
        )
        for name in exact_one:
            if float(gates.get(name) or 0) != 1.0:
                blockers.append(name)
        adversarial = _mapping(gates.get("adversarial_proposals"))
        total = int(adversarial.get("total") or 0)
        settled = int(adversarial.get("rejected_or_projected") or 0)
        if total < 1 or settled != total:
            blockers.append("adversarial_reject_project_rate")
        physical = _mapping(gates.get("physical_dispatch"))
        lanes = {
            str(item.get("lane") or "")
            for item in _sequence(physical.get("lanes"))
            if isinstance(item, Mapping)
            and item.get("real_gate_closed") is True
            and item.get("simulated") is False
            and item.get("semantic_only") is False
            and item.get("receipt_digest")
        }
        if lanes != REQUIRED_LANES:
            blockers.append("physical_lane_coverage")
        if physical.get("condition_change_effect") not in {
            "placement_changed",
            "safe_fail_closed_recovery",
        }:
            blockers.append("physical_condition_change")
        if physical.get("artifact_continuity") is not True:
            blockers.append("physical_artifact_continuity")
        continuity = _mapping(gates.get("continuity"))
        required_transitions = {
            "compact_restore",
            "process_restart",
            "handoff",
            "requirement_revision",
        }
        if set(_sequence(continuity.get("verified_transitions"))) != required_transitions:
            blockers.append("continuity_transition_coverage")
        if continuity.get("poisoned_rejected") is not True:
            blockers.append("poisoned_memory_rejection")
        if continuity.get("stale_rejected") is not True:
            blockers.append("stale_memory_rejection")
        if continuity.get("conflicting_rejected") is not True:
            blockers.append("conflicting_memory_rejection")
        loopx = _mapping(gates.get("loopx"))
        for name in (
            "restart_recovered",
            "claim_conflict_rejected",
            "quota_exhaustion_fail_closed",
            "worker_lease_owner_preserved",
            "execution_budget_owner_preserved",
        ):
            if loopx.get(name) is not True:
                blockers.append(f"loopx_{name}")
        topology = _mapping(gates.get("topology_operator"))
        for name in (
            "role_added",
            "role_removed",
            "operator_added",
            "operator_removed",
            "canonical_custody_commit",
        ):
            if topology.get(name) is not True:
                blockers.append(f"topology_{name}")
        permission = _mapping(gates.get("permission_recovery"))
        if permission.get("denial_observed") is not True:
            blockers.append("permission_denial")
        if permission.get("autonomous_recovery") is not True:
            blockers.append("permission_autonomous_recovery")
        disable = _mapping(gates.get("disable_evidence"))
        if set(disable) != REQUIRED_DISABLE_GATES:
            blockers.append("disable_evidence_coverage")
        for name, value in disable.items():
            item = _mapping(value)
            if item.get("disabled_changed_outcome") is not True:
                blockers.append(f"disable_evidence_{name}")
        if gates.get("production_bypass_reachable") is not False:
            blockers.append("production_bypass")
        return blockers


def write_validation(
    evidence_index: str | Path,
    *,
    output: str | Path | None = None,
) -> dict[str, Any]:
    report = SealedLongRunValidator().validate(evidence_index)
    target = (
        Path(output).resolve()
        if output is not None
        else Path(evidence_index).resolve().with_name("independent-validation.json")
    )
    target.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independently validate P2-S06-02 sealed long-run evidence."
    )
    parser.add_argument("--evidence", required=True, help="sealed evidence index JSON")
    parser.add_argument("--output", help="validation report path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = write_validation(arguments.evidence, output=arguments.output)
    except LongRunValidationError as error:
        print(json.dumps({"valid": False, "error": str(error)}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
