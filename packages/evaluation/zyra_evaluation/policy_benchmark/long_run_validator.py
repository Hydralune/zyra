from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_orchestration.topology_policy.contracts import PhysicalDispatchReceipt


EVIDENCE_INDEX_SCHEMA = "zyra.phase2-sealed-evidence-index/v1"
VALIDATION_SCHEMA = "zyra.phase2-sealed-long-run-validation/v1"
TRANSITION_INDEX_SCHEMA = "zyra.phase2-canonical-transition-index/v1"
OWNER_SNAPSHOT_SCHEMA = "zyra.phase2-canonical-owner-snapshot/v1"
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


def _owner_receipt_blockers(
    event: Mapping[str, Any],
    *,
    owner_events: Mapping[str, Mapping[str, Any]],
    owner_memories: Mapping[str, Mapping[str, Any]],
    run_id: str,
    task_id: str,
) -> list[str]:
    receipt = _mapping(event.get("owner_receipt"))
    event_type = str(event.get("event_type") or "").casefold()
    if not receipt:
        return ["owner_receipt_missing"]
    unsigned = dict(receipt)
    claimed = str(unsigned.pop("receipt_digest", ""))
    blockers: list[str] = []
    if claimed != canonical_digest(unsigned):
        blockers.append("owner_receipt_digest")
    if receipt.get("run_id") != run_id or receipt.get("task_id") != task_id:
        blockers.append("owner_receipt_scope")
    event_id = str(event.get("event_id") or "")
    if receipt.get("event_id") != event_id:
        blockers.append("owner_receipt_event")
    source = dict(event)
    source.pop("owner_receipt", None)
    if receipt.get("source_event_digest") != canonical_digest(source):
        blockers.append("owner_receipt_source_digest")
    if event_type in {"tool_call", "artifact_committed"}:
        blockers.append("unsupported_pseudo_effect_schema")
    if event_type in {"analysis_unit_indexed", "analysis_unit_verified"}:
        unit = _mapping(_mutation(event).get("owner_receipt"))
        unit_unsigned = dict(unit)
        unit_claimed = str(unit_unsigned.pop("receipt_digest", ""))
        if (
            unit.get("schema") != "zyra.analysis-unit-owner-receipt/v1"
            or unit.get("owner") != "SQLiteStore.MemoryRecord"
            or unit.get("run_id") != run_id
            or unit.get("task_id") != task_id
            or not str(unit.get("source_locator") or "")
            or int(unit.get("byte_end") or 0)
            <= int(unit.get("byte_start") or 0)
            or unit.get("committed") is not True
            or unit.get("readback_verified") is not True
            or unit_claimed != canonical_digest(unit_unsigned)
        ):
            blockers.append("analysis_owner_receipt_invalid")
        memory_id = str(unit.get("memory_id") or "")
        persisted_memory = owner_memories.get(memory_id)
        if (
            receipt.get("schema")
            != "zyra.canonical-analysis-event-owner-receipt/v1"
            or receipt.get("owner") != "SQLiteStore.MemoryRecord"
            or receipt.get("memory_id") != memory_id
            or receipt.get("analysis_receipt_digest")
            != unit.get("receipt_digest")
            or persisted_memory is None
        ):
            blockers.append("analysis_event_owner_binding")
        elif (
            receipt.get("persisted_memory_digest")
            != canonical_digest(persisted_memory)
            or receipt.get("persisted_content_digest")
            != canonical_digest(_mapping(persisted_memory.get("content")))
            or receipt.get("persisted_content_digest")
            != unit.get("content_digest")
            or persisted_memory.get("run_id") != run_id
            or persisted_memory.get("task_id") != task_id
            or persisted_memory.get("source_id") != unit.get("work_unit_id")
        ):
            blockers.append("analysis_memory_snapshot_binding")
        else:
            content = _mapping(persisted_memory.get("content"))
            if any(
                content.get(field) != unit.get(field)
                for field in (
                    "work_unit_id",
                    "input_digest",
                    "output_digest",
                    "byte_start",
                    "byte_end",
                    "source_locator",
                )
            ):
                blockers.append("analysis_memory_content_binding")
    else:
        if (
            receipt.get("schema") != "zyra.canonical-event-owner-receipt/v1"
            or receipt.get("owner") != "SQLiteStore.EventRecord"
        ):
            blockers.append("owner_receipt_owner")
        persisted = owner_events.get(event_id)
        if persisted is None:
            blockers.append("owner_event_unresolved")
        else:
            if receipt.get("persisted_event_digest") != canonical_digest(persisted):
                blockers.append("owner_event_digest")
            if receipt.get("persisted_payload_digest") != canonical_digest(
                _mapping(persisted.get("payload"))
            ):
                blockers.append("owner_payload_digest")
            if receipt.get("persisted_event_type") != persisted.get("event_type"):
                blockers.append("owner_event_type")
            if (
                persisted.get("run_id") != run_id
                or persisted.get("task_id") != task_id
            ):
                blockers.append("owner_event_scope")
    return blockers


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
        owner_events: Iterable[Mapping[str, Any]] = (),
        owner_analysis_records: Iterable[Mapping[str, Any]] = (),
    ) -> TransitionValidation:
        selected = tuple(dict(item) for item in events)
        owner_event_map = {
            str(item.get("event_id") or ""): dict(item)
            for item in owner_events
            if str(item.get("event_id") or "")
        }
        owner_memory_map = {
            str(item.get("memory_id") or ""): dict(item)
            for item in owner_analysis_records
            if str(item.get("memory_id") or "")
        }
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
        seen_analysis_ranges: set[tuple[str, int, int]] = set()
        seen_analysis_intervals: dict[str, list[tuple[int, int]]] = {}
        indexed_analysis: dict[str, tuple[str, str]] = {}
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
            reasons.extend(
                _owner_receipt_blockers(
                    event,
                    owner_events=owner_event_map,
                    owner_memories=owner_memory_map,
                    run_id=run_id,
                    task_id=task_id,
                )
            )
            if event_type in {"analysis_unit_indexed", "analysis_unit_verified"}:
                unit = _mapping(mutation.get("owner_receipt"))
                unit_id = str(unit.get("work_unit_id") or "")
                range_key = (
                    str(unit.get("source_locator") or ""),
                    int(unit.get("byte_start") or 0),
                    int(unit.get("byte_end") or 0),
                )
                if event_type == "analysis_unit_indexed":
                    if unit_id in indexed_analysis:
                        reasons.append("analysis_unit_duplicate")
                    if range_key in seen_analysis_ranges:
                        reasons.append("analysis_source_range_duplicate")
                    if any(
                        range_key[1] < existing_end
                        and range_key[2] > existing_start
                        for existing_start, existing_end in seen_analysis_intervals.get(
                            range_key[0],
                            (),
                        )
                    ):
                        reasons.append("analysis_source_range_overlap")
                else:
                    indexed = indexed_analysis.get(unit_id)
                    if indexed is None:
                        reasons.append("analysis_unit_not_indexed")
                    elif (
                        str(mutation.get("caused_by") or "") != indexed[0]
                        or str(unit.get("receipt_digest") or "") != indexed[1]
                    ):
                        reasons.append("analysis_verification_causality")
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
                if event_type == "analysis_unit_indexed":
                    unit = _mapping(mutation.get("owner_receipt"))
                    unit_id = str(unit.get("work_unit_id") or "")
                    range_key = (
                        str(unit.get("source_locator") or ""),
                        int(unit.get("byte_start") or 0),
                        int(unit.get("byte_end") or 0),
                    )
                    indexed_analysis[unit_id] = (
                        event_id,
                        str(unit.get("receipt_digest") or ""),
                    )
                    seen_analysis_ranges.add(range_key)
                    seen_analysis_intervals.setdefault(range_key[0], []).append(
                        (range_key[1], range_key[2])
                    )
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
        owner_path = _member(
            root,
            run.get("canonical_owner_snapshot"),
            label=f"{run_key}.canonical_owner_snapshot",
        )
        if str(run.get("canonical_owner_snapshot_digest") or "") != file_digest(
            owner_path
        ):
            blockers.append("canonical_owner_snapshot_digest")
        owner_snapshot = load_json(owner_path)
        owner_unsigned = dict(owner_snapshot)
        owner_claimed = str(owner_unsigned.pop("snapshot_digest", ""))
        owner_events = tuple(
            dict(item)
            for item in _sequence(owner_snapshot.get("events"))
            if isinstance(item, Mapping)
        )
        owner_analysis_records = tuple(
            dict(item)
            for item in _sequence(owner_snapshot.get("analysis_records"))
            if isinstance(item, Mapping)
        )
        analysis_memory_ids = tuple(
            str(item.get("memory_id") or "")
            for item in owner_analysis_records
        )
        if (
            owner_snapshot.get("schema") != OWNER_SNAPSHOT_SCHEMA
            or owner_snapshot.get("run_id") != run_id
            or owner_snapshot.get("task_id") != task_id
            or owner_claimed != canonical_digest(owner_unsigned)
            or not owner_analysis_records
            or any(not item for item in analysis_memory_ids)
            or len(analysis_memory_ids) != len(set(analysis_memory_ids))
        ):
            blockers.append("canonical_owner_snapshot_invalid")
        transitions = IndependentTransitionValidator().validate(
            raw_events,
            run_id=run_id,
            task_id=task_id,
            owner_events=owner_events,
            owner_analysis_records=owner_analysis_records,
        )
        transition_index = transitions.index(run_id=run_id, task_id=task_id)
        if transitions.invalid:
            blockers.append("invalid_transition_candidates")
        if transitions.valid_count < minimum:
            blockers.append("valid_transition_minimum")
        if REQUIRED_EFFECTS - set(transitions.effect_counts):
            blockers.append("required_effect_coverage")
        mechanism_path = _member(
            root,
            run.get("mechanism_bundle"),
            label=f"{run_key}.mechanism_bundle",
        )
        if str(run.get("mechanism_bundle_digest") or "") != file_digest(
            mechanism_path
        ):
            blockers.append("mechanism_bundle_digest")
        mechanism = load_json(mechanism_path)
        mechanism_unsigned = dict(mechanism)
        mechanism_claimed = str(mechanism_unsigned.pop("bundle_digest", ""))
        if (
            mechanism.get("schema") != "zyra.phase2-sealed-mechanism-bundle/v2"
            or mechanism_claimed != canonical_digest(mechanism_unsigned)
        ):
            blockers.append("mechanism_bundle_integrity")
        control = _mapping(mechanism.get("production_control"))
        blockers.extend(
            self._production_control_blockers(
                control,
                raw_events=raw_events,
                owner_events=owner_events,
                run_id=run_id,
                task_id=task_id,
            )
        )
        physical_path = _member(
            root,
            run.get("physical_dispatch_bundle"),
            label=f"{run_key}.physical_dispatch_bundle",
        )
        if str(run.get("physical_dispatch_bundle_digest") or "") != file_digest(
            physical_path
        ):
            blockers.append("physical_dispatch_bundle_digest")
        physical = load_json(physical_path)
        blockers.extend(self._physical_bundle_blockers(physical, run_id, task_id))
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
        gate_physical = _mapping(gates.get("physical_dispatch"))
        gate_receipts = tuple(
            str(_mapping(item).get("receipt_digest") or "")
            for item in _sequence(gate_physical.get("lanes"))
        )
        physical_receipts = tuple(
            str(_mapping(item).get("digest") or "")
            for item in _sequence(physical.get("receipts"))
        )
        gate_models = tuple(
            str(_mapping(item).get("receipt_digest") or "")
            for item in _sequence(gate_physical.get("provider_models"))
        )
        physical_models = tuple(
            str(_mapping(_mapping(item).get("metadata")).get(
                "physical_dispatch_receipt_digest"
            ) or "")
            for item in _sequence(physical.get("providers"))
            if _mapping(item).get("provider_id") != "zyra-local"
        )
        if gate_receipts != physical_receipts or gate_models != physical_models:
            blockers.append("hard_gate_physical_binding")
        if _mapping(gates.get("production_control")).get(
            "receipt_digest"
        ) != control.get("receipt_digest"):
            blockers.append("hard_gate_production_control_binding")
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
        if verifier.get("inline_policy_receipt_digest") != control.get(
            "receipt_digest"
        ):
            blockers.append("final_verifier_policy_binding")
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
            "canonical_owner_snapshot_digest": file_digest(owner_path),
            "mechanism_bundle_digest": file_digest(mechanism_path),
            "physical_dispatch_bundle_digest": file_digest(physical_path),
            "hard_gate_bundle_digest": file_digest(gates_path),
            "final_artifact_digest": file_digest(artifact_path),
            "final_verifier_digest": file_digest(verifier_path),
            "blockers": sorted(set(blockers)),
        }

    @staticmethod
    def _physical_bundle_blockers(
        physical: Mapping[str, Any],
        run_id: str,
        task_id: str,
    ) -> list[str]:
        blockers: list[str] = []
        envelopes = tuple(
            _mapping(item) for item in _sequence(physical.get("receipt_envelopes"))
        )
        receipts = tuple(
            _mapping(item) for item in _sequence(physical.get("receipts"))
        )
        validations = tuple(
            _mapping(item) for item in _sequence(physical.get("validations"))
        )
        decisions = tuple(
            _mapping(item)
            for item in _sequence(physical.get("scheduler_decisions"))
        )
        leases = tuple(
            _mapping(item) for item in _sequence(physical.get("lease_receipts"))
        )
        if (
            physical.get("schema") != "zyra.phase2-sealed-physical-evidence/v1"
            or not len(envelopes) == len(receipts) == len(validations)
            == len(decisions) == len(leases) == 5
        ):
            return ["physical_bundle_shape"]
        locations: list[str] = []
        for index, (envelope, receipt, validation, decision, lease) in enumerate(
            zip(envelopes, receipts, validations, decisions, leases, strict=True)
        ):
            try:
                parsed = PhysicalDispatchReceipt.from_dict(envelope)
            except (TypeError, ValueError):
                blockers.append(f"physical_receipt_contract:{index}")
                continue
            payload = _mapping(envelope.get("payload"))
            flattened = {
                **payload,
                "digest": envelope.get("digest"),
                "contract_id": envelope.get("contract_id"),
                "created_at": envelope.get("created_at"),
            }
            if receipt != flattened or parsed.digest != receipt.get("digest"):
                blockers.append(f"physical_receipt_projection:{index}")
            identity = _mapping(receipt.get("physical_identity"))
            location = str(identity.get("location") or "")
            locations.append(location)
            acquisition = _mapping(lease.get("acquisition"))
            acquired_lease = _mapping(acquisition.get("lease"))
            attempt = _mapping(lease.get("attempt"))
            completion = _mapping(lease.get("completion"))
            call = _mapping(receipt.get("call_receipt"))
            if (
                validation.get("schema")
                != "zyra.physical-dispatch-validation/v1"
                or validation.get("receipt_digest") != receipt.get("digest")
                or validation.get("location") != location
                or validation.get("real_gate_closed") is not True
                or validation.get("blockers") != []
                or not all(
                    item is True
                    for item in _mapping(validation.get("checks")).values()
                )
                or decision.get("decision_id")
                != receipt.get("placement_decision_id")
                or decision.get("run_id") != run_id
                or decision.get("task_id") != task_id
                or acquired_lease.get("lease_id") != receipt.get("lease_id")
                or attempt.get("attempt_id")
                != receipt.get("physical_attempt_id")
                or attempt.get("lease_id") != receipt.get("lease_id")
                or completion.get("lease_id") != receipt.get("lease_id")
                or completion.get("attempt_id")
                != receipt.get("physical_attempt_id")
                or completion.get("outcome") != "succeeded"
                or completion.get("backend_receipt_ref") != call.get("ref_id")
                or lease.get("fence_token_persisted") is not False
            ):
                blockers.append(f"physical_custody_chain:{index}")
        if tuple(locations) != ("local", "edge", "cloud", "cloud", "cloud"):
            blockers.append("physical_location_order")
        providers = tuple(
            _mapping(item) for item in _sequence(physical.get("providers"))
        )
        external = tuple(
            item for item in providers if item.get("provider_id") != "zyra-local"
        )
        if tuple(
            (str(item.get("provider_id") or ""), str(item.get("model_id") or ""))
            for item in external
        ) != (
            ("zhipu", "glm-5.2"),
            ("deepseek", "deepseek-v4-flash"),
            ("kimi-platform", "kimi-k2.7-code"),
        ) or any(
            item.get("authenticated") is not True
            or not 200 <= int(item.get("response_status") or 0) < 300
            or not str(item.get("request_id") or "")
            or _mapping(item.get("metadata")).get("simulated") is not False
            or _mapping(item.get("metadata")).get("semantic_only") is not False
            for item in external
        ):
            blockers.append("physical_external_models")
        return blockers

    @staticmethod
    def _production_control_blockers(
        control: Mapping[str, Any],
        *,
        raw_events: Sequence[Mapping[str, Any]],
        owner_events: Sequence[Mapping[str, Any]],
        run_id: str,
        task_id: str,
    ) -> list[str]:
        blockers: list[str] = []
        unsigned = dict(control)
        claimed = str(unsigned.pop("receipt_digest", ""))
        if (
            control.get("schema") != "zyra.phase2-sealed-inline-policy/v1"
            or control.get("ready") is not True
            or control.get("policy_profile") != "phase2_strongest_v1"
            or control.get("run_id") != run_id
            or control.get("task_id") != task_id
            or control.get("consumed_before_domain_execution") is not True
            or claimed != canonical_digest(unsigned)
        ):
            blockers.append("production_control_integrity")
        checks = _mapping(control.get("checks"))
        required_checks = {
            "candidate_set",
            "resource_decision",
            "lease",
            "attempt",
            "physical_receipt",
            "real_execution",
        }
        if set(checks) != required_checks or not all(
            checks.get(name) is True for name in required_checks
        ):
            blockers.append("production_control_checks")
        event_ids = tuple(str(item) for item in _sequence(control.get("production_event_ids")))
        if (
            not event_ids
            or len(event_ids) != len(set(event_ids))
            or int(control.get("production_event_count") or 0) != len(event_ids)
        ):
            blockers.append("production_event_identity")
        owner_map = {
            str(item.get("event_id") or ""): dict(item)
            for item in owner_events
            if str(item.get("event_id") or "")
        }
        snapshot = [owner_map[item] for item in event_ids if item in owner_map]
        if (
            len(snapshot) != len(event_ids)
            or control.get("production_event_snapshot_digest")
            != canonical_digest(snapshot)
        ):
            blockers.append("production_owner_snapshot_binding")
        topology_event_id = str(control.get("topology_policy_event_id") or "")
        topology_event = owner_map.get(topology_event_id)
        policy = _mapping(
            _mapping((topology_event or {}).get("payload")).get("topology_policy")
        )
        placement = _mapping(policy.get("physical_placement"))
        candidate = _mapping(policy.get("operator_candidate_set"))
        permission = _mapping(policy.get("permission_receipt"))
        if (
            topology_event_id not in event_ids
            or policy.get("used_baseline") is not False
            or policy.get("committed") is not True
            or candidate.get("candidate_set_digest")
            != control.get("candidate_set_digest")
            or placement.get("resource_decision_id")
            != control.get("resource_decision_id")
            or placement.get("lease_id") != control.get("lease_id")
            or placement.get("attempt_id") != control.get("attempt_id")
            or permission.get("receipt_digest")
            != control.get("permission_receipt_digest")
        ):
            blockers.append("production_policy_event_binding")
        policy_events = [
            item
            for item in raw_events
            if str(item.get("event_type") or "").casefold()
            == "policy_control_committed"
            and _mapping(_mutation(item)).get("receipt_digest") == claimed
        ]
        if len(policy_events) != 1:
            blockers.append("production_domain_consumption_binding")
        return blockers

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
        model_receipts = tuple(
            _mapping(item) for item in _sequence(physical.get("provider_models"))
        )
        model_order = tuple(
            (
                str(item.get("provider_id") or ""),
                str(item.get("model_id") or ""),
            )
            for item in model_receipts
        )
        if model_order != (
            ("zhipu", "glm-5.2"),
            ("deepseek", "deepseek-v4-flash"),
            ("kimi-platform", "kimi-k2.7-code"),
        ) or any(
            item.get("authenticated") is not True
            or not 200 <= int(item.get("response_status") or 0) < 300
            or not str(item.get("request_id") or "")
            or not str(item.get("attempt_id") or "")
            or not str(item.get("receipt_digest") or "")
            or item.get("simulated") is not False
            or item.get("semantic_only") is not False
            for item in model_receipts
        ):
            blockers.append("live_external_model_coverage")
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
        production = _mapping(gates.get("production_control"))
        if (
            production.get("ready") is not True
            or production.get("policy_profile") != "phase2_strongest_v1"
            or production.get("consumed_before_domain_execution") is not True
            or int(production.get("production_event_count") or 0) < 1
            or not str(production.get("production_event_snapshot_digest") or "")
            or not str(production.get("receipt_digest") or "")
            or not all(
                item is True
                for item in _mapping(production.get("checks")).values()
            )
        ):
            blockers.append("production_control")
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
