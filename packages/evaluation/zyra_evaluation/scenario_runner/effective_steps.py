from __future__ import annotations

import os
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import canonicalize, digest, identity, new_identity, utc_now
from .errors import conflict, invalid, unavailable
from .models import (
    EffectiveStep,
    ExecutionProfile,
    StepDisposition,
    StepEffect,
)


EXCLUDED_EVENT_TYPES = {
    "heartbeat",
    "worker_heartbeat",
    "worker_health",
    "log",
    "debug_log",
    "token",
    "token_usage",
    "stream_token",
    "ui_repaint",
    "ui_render",
    "ui_selection",
    "cursor",
    "cursor_advanced",
    "poll",
    "poll_completed",
    "sse_heartbeat",
    "transport_opened",
    "transport_closed",
    "transport_retry",
    "receipt_replayed",
    "replay",
    "noop",
    "no_op",
    "system_notice",
}


EFFECT_BY_EVENT_TYPE: dict[str, StepEffect] = {
    "task_created": StepEffect.STATE_MUTATION,
    "task_updated": StepEffect.STATE_MUTATION,
    "task_cancelled": StepEffect.STATE_MUTATION,
    "node_created": StepEffect.TOPOLOGY,
    "node_updated": StepEffect.STATE_MUTATION,
    "node_failed": StepEffect.FAULT,
    "topology_mutation": StepEffect.TOPOLOGY,
    "topology_route": StepEffect.ROUTE,
    "resource_decision": StepEffect.PLACEMENT,
    "worker_acquired": StepEffect.PLACEMENT,
    "worker_dispatched": StepEffect.PLACEMENT,
    "worker_released": StepEffect.PLACEMENT,
    "backend_route": StepEffect.ROUTE,
    "provider_route": StepEffect.ROUTE,
    "tool_call": StepEffect.TOOL,
    "tool_result": StepEffect.TOOL,
    "mcp_tool_result": StepEffect.TOOL,
    "browser_cdp_request": StepEffect.TOOL,
    "terminal_control": StepEffect.TOOL,
    "terminal_output_committed": StepEffect.TOOL,
    "constraint_check": StepEffect.VERIFICATION,
    "evaluation": StepEffect.VERIFICATION,
    "verification": StepEffect.VERIFICATION,
    "evidence_verified": StepEffect.VERIFICATION,
    "permission_decision": StepEffect.PERMISSION,
    "permission_request_created": StepEffect.PERMISSION,
    "permission_grant_consumed": StepEffect.PERMISSION,
    "permission_denied": StepEffect.PERMISSION,
    "compact_committed": StepEffect.COMPACT_RESTORE,
    "compact_restored": StepEffect.COMPACT_RESTORE,
    "checkpoint_restored": StepEffect.COMPACT_RESTORE,
    "failure_injected": StepEffect.FAULT,
    "fault_observed": StepEffect.FAULT,
    "fault_contained": StepEffect.FAULT,
    "recovery_planned": StepEffect.RECOVERY,
    "recovery_applied": StepEffect.RECOVERY,
    "recovery_resumed": StepEffect.RECOVERY,
    "requirement_change": StepEffect.RECOVERY,
    "artifact_written": StepEffect.ARTIFACT,
    "artifact_committed": StepEffect.ARTIFACT,
    "artifact_verified": StepEffect.ARTIFACT,
    "delivery_committed": StepEffect.DELIVERY,
    "subagent_completed": StepEffect.DELIVERY,
    "memory_curator_scheduled": StepEffect.MEMORY,
    "memory_curator_candidate": StepEffect.MEMORY,
    "memory_curator_accepted": StepEffect.MEMORY,
    "memory_curator_committed": StepEffect.MEMORY,
    "memory_curator_index_published": StepEffect.MEMORY,
    "memory_curator_recovered": StepEffect.MEMORY,
    "memory_retrieved": StepEffect.MEMORY,
    "procedure_mined": StepEffect.MEMORY,
}


EFFECT_HINTS: tuple[tuple[str, StepEffect], ...] = (
    ("permission", StepEffect.PERMISSION),
    ("recover", StepEffect.RECOVERY),
    ("fault", StepEffect.FAULT),
    ("failure", StepEffect.FAULT),
    ("route", StepEffect.ROUTE),
    ("dispatch", StepEffect.PLACEMENT),
    ("worker", StepEffect.PLACEMENT),
    ("memory", StepEffect.MEMORY),
    ("curator", StepEffect.MEMORY),
    ("artifact", StepEffect.ARTIFACT),
    ("tool", StepEffect.TOOL),
    ("browser_action", StepEffect.TOOL),
    ("terminal", StepEffect.TOOL),
    ("verify", StepEffect.VERIFICATION),
    ("constraint", StepEffect.VERIFICATION),
    ("checkpoint", StepEffect.COMPACT_RESTORE),
    ("compact", StepEffect.COMPACT_RESTORE),
    ("restore", StepEffect.COMPACT_RESTORE),
    ("node", StepEffect.STATE_MUTATION),
    ("task", StepEffect.STATE_MUTATION),
    ("topology", StepEffect.TOPOLOGY),
    ("delivery", StepEffect.DELIVERY),
    ("message_committed", StepEffect.DELIVERY),
)


SEMANTIC_PAYLOAD_KEYS = {
    "after",
    "artifact",
    "artifact_id",
    "before",
    "binding",
    "checkpoint",
    "decision",
    "delta",
    "effect",
    "fault",
    "memory",
    "mutation",
    "node",
    "permission",
    "plan",
    "receipt",
    "recovery",
    "result",
    "route",
    "state",
    "tool_result",
    "worker_result",
}


@dataclass(frozen=True, slots=True)
class StepBatch:
    steps: tuple[EffectiveStep, ...]
    admitted: tuple[EffectiveStep, ...]
    excluded: tuple[EffectiveStep, ...]
    invalid: tuple[EffectiveStep, ...]
    duplicates: tuple[EffectiveStep, ...]
    effect_counts: dict[str, int]
    reason_counts: dict[str, int]
    causal_roots: tuple[str, ...]
    causal_leaves: tuple[str, ...]
    batch_digest: str

    @property
    def formal_valid(self) -> bool:
        return not self.invalid and bool(self.admitted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.effective-step-batch/v1",
            "steps": [item.to_dict() for item in self.steps],
            "admitted_step_ids": [item.step_id for item in self.admitted],
            "excluded_step_ids": [item.step_id for item in self.excluded],
            "invalid_step_ids": [item.step_id for item in self.invalid],
            "duplicate_step_ids": [item.step_id for item in self.duplicates],
            "effect_counts": dict(self.effect_counts),
            "reason_counts": dict(self.reason_counts),
            "causal_roots": list(self.causal_roots),
            "causal_leaves": list(self.causal_leaves),
            "formal_valid": self.formal_valid,
            "batch_digest": self.batch_digest,
        }


class EffectiveStepClassifier:
    def __init__(
        self,
        *,
        profile: ExecutionProfile,
        strict_causation: bool = True,
        disabled: bool | None = None,
    ) -> None:
        self._profile = profile
        self._strict_causation = strict_causation
        self._disabled = (
            disabled
            if disabled is not None
            else _disabled("ZYRA_SCENARIO_EFFECTIVE_STEP_DISABLED")
        )
        self._seen_event_ids: set[str] = set()
        self._seen_semantic_digests: set[str] = set()
        self._last_by_scope: dict[tuple[str, str, str], str] = {}
        self._known_event_ids: set[str] = set()
        self._step_id_by_event_id: dict[str, str] = {}

    def classify_all(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        expected_run_id: str = "",
        expected_task_id: str = "",
    ) -> StepBatch:
        if self._disabled:
            raise unavailable(
                "scenario_effective_step_classifier_disabled",
                "Formal evidence requires the effective-step classifier.",
                phase="effective-step",
            )
        output: list[EffectiveStep] = []
        for sequence, event in enumerate(events, start=1):
            output.append(
                self.classify(
                    event,
                    sequence=sequence,
                    expected_run_id=expected_run_id,
                    expected_task_id=expected_task_id,
                )
            )
        admitted = tuple(
            item for item in output if item.disposition is StepDisposition.ADMITTED
        )
        excluded = tuple(
            item for item in output if item.disposition is StepDisposition.EXCLUDED
        )
        invalid_steps = tuple(
            item for item in output if item.disposition is StepDisposition.INVALID
        )
        duplicates = tuple(
            item for item in output if item.disposition is StepDisposition.DUPLICATE
        )
        referenced = {
            parent
            for item in admitted
            for parent in item.causal_parent_ids
            if parent
        }
        roots = tuple(
            item.step_id
            for item in admitted
            if not item.causal_parent_ids
        )
        leaves = tuple(
            item.step_id for item in admitted if item.step_id not in referenced
        )
        effect_counts = Counter(item.effect.value for item in admitted)
        reason_counts = Counter(item.reason_code for item in output)
        payload = {
            "steps": [item.to_dict() for item in output],
            "effect_counts": dict(effect_counts),
            "reason_counts": dict(reason_counts),
            "roots": roots,
            "leaves": leaves,
        }
        return StepBatch(
            steps=tuple(output),
            admitted=admitted,
            excluded=excluded,
            invalid=invalid_steps,
            duplicates=duplicates,
            effect_counts=dict(effect_counts),
            reason_counts=dict(reason_counts),
            causal_roots=roots,
            causal_leaves=leaves,
            batch_digest=digest(payload),
        )

    def classify(
        self,
        event: Mapping[str, Any],
        *,
        sequence: int,
        expected_run_id: str = "",
        expected_task_id: str = "",
    ) -> EffectiveStep:
        raw_type = str(
            event.get("event_type")
            or event.get("type")
            or event.get("kind")
            or ""
        ).strip().casefold()
        event_id = str(
            event.get("event_id")
            or event.get("receipt_id")
            or event.get("decision_id")
            or ""
        ).strip()
        run_id = str(event.get("run_id") or "").strip()
        task_id = str(event.get("task_id") or "").strip()
        payload = event.get("payload")
        payload = dict(payload) if isinstance(payload, Mapping) else {}
        metadata = event.get("metadata")
        metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
        stage = str(
            event.get("stage")
            or metadata.get("stage")
            or payload.get("stage")
            or _stage_for_type(raw_type)
        ).strip().casefold()
        worker_id = str(
            event.get("worker_id")
            or metadata.get("worker_id")
            or payload.get("worker_id")
            or payload.get("assigned_worker_id")
            or ""
        ).strip()
        provider_id = str(
            event.get("provider_id")
            or metadata.get("provider_id")
            or payload.get("provider_id")
            or self._profile.provider_id
        ).strip()
        created_at = str(
            event.get("created_at")
            or event.get("timestamp")
            or utc_now()
        )
        base = {
            "event_id": event_id,
            "event_type": raw_type,
            "run_id": run_id,
            "task_id": task_id,
            "stage": stage,
            "worker_id": worker_id,
            "profile_id": self._profile.profile_id,
            "provider_id": provider_id,
            "sequence": sequence,
            "created_at": created_at,
        }
        invalid_reasons: list[str] = []
        if not event_id:
            invalid_reasons.append("event_id_missing")
        if not raw_type:
            invalid_reasons.append("event_type_missing")
        if expected_run_id and run_id != expected_run_id:
            invalid_reasons.append("run_scope_mismatch")
        if expected_task_id and task_id != expected_task_id:
            invalid_reasons.append("task_scope_mismatch")
        if not run_id:
            invalid_reasons.append("run_id_missing")
        if not task_id:
            invalid_reasons.append("task_id_missing")
        if event_id and event_id in self._seen_event_ids:
            return self._step(
                base,
                effect=StepEffect.NONE,
                disposition=StepDisposition.DUPLICATE,
                reason_code="duplicate.event_id",
                parents=(),
                semantic_digest=digest({"event_id": event_id}),
                metadata={"invalid_reasons": invalid_reasons},
            )
        if event_id:
            self._seen_event_ids.add(event_id)
            self._known_event_ids.add(event_id)
        if invalid_reasons:
            return self._step(
                base,
                effect=StepEffect.NONE,
                disposition=StepDisposition.INVALID,
                reason_code="invalid." + invalid_reasons[0],
                parents=(),
                semantic_digest=digest(canonicalize(event)),
                metadata={"invalid_reasons": invalid_reasons},
            )
        if _excluded(raw_type, payload, metadata):
            return self._step(
                base,
                effect=StepEffect.NONE,
                disposition=StepDisposition.EXCLUDED,
                reason_code=f"excluded.{_exclusion_reason(raw_type, payload, metadata)}",
                parents=(),
                semantic_digest=digest(
                    {
                        "event_type": raw_type,
                        "run_id": run_id,
                        "task_id": task_id,
                        "sequence": sequence,
                    }
                ),
                metadata={"payload_digest": digest(payload)},
            )
        effect = _effect(raw_type, payload, metadata)
        if effect is StepEffect.NONE:
            return self._step(
                base,
                effect=effect,
                disposition=StepDisposition.EXCLUDED,
                reason_code="excluded.no_semantic_effect",
                parents=(),
                semantic_digest=digest(canonicalize(event)),
                metadata={"payload_digest": digest(payload)},
            )
        mutation = _semantic_payload(raw_type, payload, metadata)
        semantic_digest = digest(
            {
                "event_type": raw_type,
                "effect": effect.value,
                "run_id": run_id,
                "task_id": task_id,
                "stage": stage,
                "worker_id": worker_id,
                "mutation": mutation,
            }
        )
        if semantic_digest in self._seen_semantic_digests:
            return self._step(
                base,
                effect=effect,
                disposition=StepDisposition.DUPLICATE,
                reason_code="duplicate.semantic_effect",
                parents=(),
                semantic_digest=semantic_digest,
                metadata={"mutation": mutation},
            )
        explicit_parents = _causal_parent_ids(event, payload, metadata)
        valid_explicit = tuple(
            self._step_id_by_event_id[parent]
            for parent in explicit_parents
            if parent in self._step_id_by_event_id
        )
        scope = (run_id, task_id, stage)
        inferred = self._last_by_scope.get(scope)
        parents = valid_explicit
        causal_basis = "explicit"
        if not parents and raw_type != "task_created":
            broader = self._last_by_scope.get((run_id, task_id, "*"))
            selected = inferred or broader
            if selected:
                parents = (selected,)
                causal_basis = "canonical_sequence"
        if (
            self._strict_causation
            and raw_type != "task_created"
            and not parents
        ):
            return self._step(
                base,
                effect=effect,
                disposition=StepDisposition.INVALID,
                reason_code="invalid.causation_missing",
                parents=(),
                semantic_digest=semantic_digest,
                metadata={
                    "mutation": mutation,
                    "explicit_parent_ids": list(explicit_parents),
                },
            )
        step = self._step(
            base,
            effect=effect,
            disposition=StepDisposition.ADMITTED,
            reason_code=f"admitted.{effect.value}",
            parents=parents,
            semantic_digest=semantic_digest,
            metadata={
                "mutation": mutation,
                "payload_digest": digest(payload),
                "causal_basis": causal_basis if parents else "root",
                "canonical_event_id": event_id,
            },
        )
        self._seen_semantic_digests.add(semantic_digest)
        self._last_by_scope[scope] = step.step_id
        self._last_by_scope[(run_id, task_id, "*")] = step.step_id
        self._known_event_ids.add(step.step_id)
        self._step_id_by_event_id[event_id] = step.step_id
        return step

    def _step(
        self,
        base: Mapping[str, Any],
        *,
        effect: StepEffect,
        disposition: StepDisposition,
        reason_code: str,
        parents: tuple[str, ...],
        semantic_digest: str,
        metadata: dict[str, Any],
    ) -> EffectiveStep:
        return EffectiveStep(
            step_id=new_identity("step"),
            event_id=str(base["event_id"]),
            event_type=str(base["event_type"]),
            effect=effect,
            disposition=disposition,
            reason_code=reason_code,
            run_id=str(base["run_id"]),
            task_id=str(base["task_id"]),
            stage=str(base["stage"]),
            worker_id=str(base["worker_id"]),
            profile_id=str(base["profile_id"]),
            provider_id=str(base["provider_id"]),
            sequence=int(base["sequence"]),
            causal_parent_ids=parents,
            semantic_digest=semantic_digest,
            created_at=str(base["created_at"]),
            metadata=metadata,
        )


def require_effect_coverage(
    batch: StepBatch,
    *,
    expected_effects: Iterable[StepEffect],
    minimum_steps: int,
    maximum_steps: int,
) -> dict[str, Any]:
    if batch.invalid:
        raise conflict(
            "scenario_effective_step_invalid",
            "Formal evidence contains invalid effective-step candidates.",
            phase="effective-step",
            detail={
                "invalid_step_ids": [item.step_id for item in batch.invalid],
                "reason_counts": batch.reason_counts,
            },
        )
    count = len(batch.admitted)
    if count < minimum_steps:
        raise conflict(
            "scenario_effective_step_minimum_unmet",
            "Formal evidence did not reach its semantic effective-step minimum.",
            phase="effective-step",
            detail={"observed": count, "minimum": minimum_steps},
        )
    if count > maximum_steps:
        raise conflict(
            "scenario_effective_step_budget_exceeded",
            "Formal evidence exceeded its configured effective-step budget.",
            phase="effective-step",
            detail={"observed": count, "maximum": maximum_steps},
        )
    observed = {item.effect for item in batch.admitted}
    missing = sorted(
        effect.value for effect in set(expected_effects) - observed
    )
    if missing:
        raise conflict(
            "scenario_effect_coverage_missing",
            "Formal evidence is missing required semantic effects.",
            phase="effective-step",
            detail={
                "missing": missing,
                "observed": sorted(effect.value for effect in observed),
            },
        )
    receipt = {
        "schema": "zyra.effective-step-coverage/v1",
        "valid": True,
        "effective_step_count": count,
        "effect_counts": batch.effect_counts,
        "excluded_count": len(batch.excluded),
        "duplicate_count": len(batch.duplicates),
        "invalid_count": 0,
        "batch_digest": batch.batch_digest,
    }
    receipt["receipt_digest"] = digest(receipt)
    return receipt


def _effect(
    event_type: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> StepEffect:
    declared = str(
        metadata.get("semantic_effect")
        or payload.get("semantic_effect")
        or ""
    ).strip().casefold()
    if declared:
        try:
            return StepEffect(declared)
        except ValueError:
            return StepEffect.NONE
    if event_type in EFFECT_BY_EVENT_TYPE:
        return EFFECT_BY_EVENT_TYPE[event_type]
    for hint, effect in EFFECT_HINTS:
        if hint in event_type:
            return effect
    return StepEffect.NONE


def _excluded(
    event_type: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> bool:
    if event_type in EXCLUDED_EVENT_TYPES:
        return True
    if any(
        marker in event_type
        for marker in ("heartbeat", "repaint", "cursor", "token", "poll", "noop")
    ):
        return True
    if payload.get("no_op") is True or metadata.get("no_op") is True:
        return True
    if payload.get("replay") is True or metadata.get("replay") is True:
        return True
    if payload.get("semantic_mutation") is False:
        return True
    return False


def _exclusion_reason(
    event_type: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> str:
    if payload.get("replay") is True or metadata.get("replay") is True:
        return "replay"
    if payload.get("no_op") is True or metadata.get("no_op") is True:
        return "noop"
    for marker in ("heartbeat", "repaint", "cursor", "token", "poll", "log"):
        if marker in event_type:
            return marker
    return "non_semantic"


def _semantic_payload(
    event_type: str,
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    selected = {
        key: canonicalize(value)
        for key, value in payload.items()
        if key in SEMANTIC_PAYLOAD_KEYS
        or key.endswith("_id")
        or key.endswith("_ids")
        or key.endswith("_revision")
    }
    if not selected:
        selected = {
            "event_type": event_type,
            "summary": str(
                payload.get("summary")
                or payload.get("message")
                or metadata.get("summary")
                or ""
            )[:4_096],
        }
    return selected


def _causal_parent_ids(
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> tuple[str, ...]:
    values: list[str] = []
    for key in (
        "causation_id",
        "parent_event_id",
        "source_event_id",
        "trigger_event_id",
        "request_event_id",
    ):
        value = event.get(key) or payload.get(key) or metadata.get(key)
        if value:
            values.append(str(value))
    for key in ("causation_ids", "parent_event_ids", "source_event_ids"):
        raw = event.get(key) or payload.get(key) or metadata.get(key)
        if isinstance(raw, (list, tuple)):
            values.extend(str(value) for value in raw if value)
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        selected = value.strip()
        if not selected or selected in seen:
            continue
        seen.add(selected)
        output.append(selected)
    return tuple(output)


def _stage_for_type(event_type: str) -> str:
    for marker in (
        "permission",
        "recovery",
        "fault",
        "memory",
        "artifact",
        "scheduler",
        "route",
        "worker",
        "tool",
        "task",
    ):
        if marker in event_type:
            return "scheduler" if marker == "route" else marker
    return "runtime"


def _disabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}
