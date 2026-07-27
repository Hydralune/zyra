from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .canonical import (
    bounded_integer,
    bounded_text,
    digest,
    identity,
    invalid,
    mapping,
    require_digest,
    sequence,
    utc_now,
)


SEMANTIC_EFFECTS = frozenset(
    {
        "state-mutation",
        "topology",
        "route",
        "placement",
        "tool",
        "verification",
        "permission",
        "memory",
        "compact",
        "restore",
        "checkpoint",
        "fault",
        "recovery",
        "artifact",
        "patch",
        "requirement-change",
        "delivery",
    }
)
EXCLUDED_EVENT_TYPES = frozenset(
    {
        "heartbeat",
        "log",
        "debug-log",
        "token-chunk",
        "stream-chunk",
        "poll",
        "polling",
        "ack",
        "acknowledgement",
        "ui-repaint",
        "replay-marker",
        "keepalive",
        "progress-noop",
    }
)
TERMINAL_EVENT_TYPES = frozenset(
    {
        "task-completed",
        "delivery-completed",
        "artifact-published",
        "verification-completed",
    }
)
MUTATION_KEYS = frozenset(
    {
        "before",
        "after",
        "state",
        "operation",
        "added-nodes",
        "removed-nodes",
        "added-edges",
        "removed-edges",
        "route-id",
        "placement-id",
        "tool-call-id",
        "tool-result-id",
        "verification-id",
        "decision",
        "memory-id",
        "checkpoint-id",
        "fault-id",
        "recovery-id",
        "artifact-id",
        "artifact-digest",
        "patch-digest",
        "requirement-id",
        "output-digest",
    }
)


@dataclass(frozen=True, slots=True)
class ClassifiedStep:
    event_id: str
    sequence: int
    event_type: str
    effect: str
    effective: bool
    exclusion_reason: str
    causal_parent_ids: tuple[str, ...]
    payload_digest: str
    mutation_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "effect": self.effect,
            "effective": self.effective,
            "exclusion_reason": self.exclusion_reason,
            "causal_parent_ids": list(self.causal_parent_ids),
            "payload_digest": self.payload_digest,
            "mutation_digest": self.mutation_digest,
        }


class SemanticStepVerifier:
    def classify(self, events: Iterable[Mapping[str, Any]]) -> tuple[ClassifiedStep, ...]:
        selected = tuple(events)
        if not selected:
            raise invalid(
                "benchmark_events_empty",
                "Live benchmark receipt has no canonical events.",
                phase="semantic-steps",
            )
        classified: list[ClassifiedStep] = []
        for index, raw in enumerate(selected, start=1):
            classified.append(self._classify_one(raw, expected_sequence=index))
        return tuple(classified)

    def verify(
        self,
        events: Iterable[Mapping[str, Any]],
        *,
        declared_effective_event_ids: Iterable[str],
        minimum_effective_steps: int,
        run_id: str,
    ) -> dict[str, Any]:
        selected_run_id = identity(run_id, "run id")
        minimum = bounded_integer(
            minimum_effective_steps,
            "minimum effective steps",
            minimum=1,
            maximum=10_000_000,
        )
        raw_events = tuple(events)
        steps = self.classify(raw_events)
        declared = tuple(
            identity(item, "declared effective event id")
            for item in declared_effective_event_ids
        )
        findings: list[dict[str, Any]] = []
        event_ids = [item.event_id for item in steps]
        duplicates = sorted(
            item for item, count in Counter(event_ids).items() if count > 1
        )
        if duplicates:
            findings.append(
                {"code": "duplicate-event-id", "event_ids": duplicates[:100]}
            )
        sequences = [item.sequence for item in steps]
        if sequences != list(range(1, len(steps) + 1)):
            findings.append(
                {
                    "code": "sequence-gap-or-reorder",
                    "first_sequences": sequences[:100],
                }
            )
        by_id = {item.event_id: item for item in steps}
        declared_counts = Counter(declared)
        declared_duplicates = sorted(
            item for item, count in declared_counts.items() if count > 1
        )
        if declared_duplicates:
            findings.append(
                {
                    "code": "effective-event-declared-twice",
                    "event_ids": declared_duplicates[:100],
                }
            )
        unknown_declared = sorted(set(declared) - set(by_id))
        if unknown_declared:
            findings.append(
                {
                    "code": "effective-event-unknown",
                    "event_ids": unknown_declared[:100],
                }
            )
        inflated = sorted(
            item
            for item in declared
            if item in by_id and not by_id[item].effective
        )
        if inflated:
            findings.append(
                {
                    "code": "effective-step-inflation",
                    "event_ids": inflated[:100],
                    "reasons": {
                        item: by_id[item].exclusion_reason
                        for item in inflated[:100]
                    },
                }
            )
        omitted = sorted(
            item.event_id
            for item in steps
            if item.effective and item.event_id not in set(declared)
        )
        if omitted:
            findings.append(
                {
                    "code": "effective-step-undercounted",
                    "event_ids": omitted[:100],
                }
            )
        findings.extend(self._causal_findings(steps))
        findings.extend(self._content_findings(raw_events, steps, selected_run_id))
        effective = tuple(item for item in steps if item.effective)
        if len(effective) < minimum:
            findings.append(
                {
                    "code": "effective-step-count-insufficient",
                    "minimum": minimum,
                    "observed": len(effective),
                }
            )
        if not any(item.event_type in TERMINAL_EVENT_TYPES for item in effective):
            findings.append({"code": "terminal-semantic-event-missing"})
        effect_counts = Counter(item.effect for item in effective)
        required_effects = {"state-mutation", "verification", "artifact"}
        missing_effects = sorted(required_effects - set(effect_counts))
        if missing_effects:
            findings.append(
                {"code": "required-effect-missing", "effects": missing_effects}
            )
        duplicate_semantics = self._duplicate_semantic_findings(effective)
        findings.extend(duplicate_semantics)
        if findings:
            raise invalid(
                "benchmark_semantic_steps_invalid",
                "Canonical event stream failed semantic-step admission.",
                phase="semantic-steps",
                detail={
                    "run_id": selected_run_id,
                    "raw_event_count": len(steps),
                    "effective_event_count": len(effective),
                    "findings": findings,
                },
            )
        receipt = {
            "schema": "zyra.live-benchmark-semantic-step-verification/v1",
            "valid": True,
            "run_id": selected_run_id,
            "raw_event_count": len(steps),
            "effective_step_count": len(effective),
            "excluded_event_count": len(steps) - len(effective),
            "effect_counts": dict(sorted(effect_counts.items())),
            "event_stream_digest": digest([item.to_dict() for item in steps]),
            "effective_event_digest": digest([item.event_id for item in effective]),
            "first_event_id": steps[0].event_id,
            "last_event_id": steps[-1].event_id,
            "verified_at": utc_now(),
        }
        receipt["receipt_digest"] = digest(receipt)
        return receipt

    def _classify_one(
        self,
        raw: Mapping[str, Any],
        *,
        expected_sequence: int,
    ) -> ClassifiedStep:
        event_id = identity(raw.get("event_id"), "event id")
        event_type = normalize_event_type(raw.get("event_type"))
        effect = normalize_effect(raw.get("effect"))
        observed_sequence = bounded_integer(
            raw.get("sequence"),
            "event sequence",
            minimum=1,
            maximum=100_000_000,
        )
        payload = dict(mapping(raw.get("payload") or {}, "event payload"))
        mutation = dict(mapping(raw.get("mutation") or {}, "event mutation"))
        parents_value = raw.get("causal_parent_ids") or []
        parents = tuple(
            identity(item, "causal parent id")
            for item in sequence(parents_value, "causal parent ids")
        )
        exclusion = exclusion_reason(
            event_type=event_type,
            effect=effect,
            payload=payload,
            mutation=mutation,
        )
        if observed_sequence != expected_sequence:
            exclusion = exclusion or "sequence-gap-or-reorder"
        return ClassifiedStep(
            event_id=event_id,
            sequence=observed_sequence,
            event_type=event_type,
            effect=effect,
            effective=not exclusion,
            exclusion_reason=exclusion,
            causal_parent_ids=parents,
            payload_digest=digest(payload),
            mutation_digest=digest(mutation),
        )

    def _causal_findings(
        self,
        steps: Sequence[ClassifiedStep],
    ) -> list[dict[str, Any]]:
        by_id = {item.event_id: item for item in steps}
        findings: list[dict[str, Any]] = []
        for item in steps:
            if item.sequence > 1 and item.effective and not item.causal_parent_ids:
                findings.append(
                    {
                        "code": "effective-event-orphaned",
                        "event_id": item.event_id,
                    }
                )
            if item.event_id in item.causal_parent_ids:
                findings.append(
                    {
                        "code": "causal-self-cycle",
                        "event_id": item.event_id,
                    }
                )
            for parent_id in item.causal_parent_ids:
                parent = by_id.get(parent_id)
                if parent is None:
                    findings.append(
                        {
                            "code": "causal-parent-missing",
                            "event_id": item.event_id,
                            "parent_id": parent_id,
                        }
                    )
                elif parent.sequence >= item.sequence:
                    findings.append(
                        {
                            "code": "causal-parent-not-prior",
                            "event_id": item.event_id,
                            "parent_id": parent_id,
                        }
                    )
        findings.extend(detect_cycles(steps))
        return findings

    def _content_findings(
        self,
        raw_events: Sequence[Mapping[str, Any]],
        steps: Sequence[ClassifiedStep],
        run_id: str,
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        for raw, step in zip(raw_events, steps, strict=True):
            observed_run = str(raw.get("run_id") or "")
            if observed_run != run_id:
                findings.append(
                    {
                        "code": "event-run-mismatch",
                        "event_id": step.event_id,
                        "expected": run_id,
                        "observed": observed_run,
                    }
                )
            if raw.get("replay") is True:
                findings.append(
                    {"code": "event-replay-marked", "event_id": step.event_id}
                )
            if raw.get("synthetic") is True or raw.get("fixture") is True:
                findings.append(
                    {
                        "code": "event-non-live-marked",
                        "event_id": step.event_id,
                    }
                )
            declared_digest = raw.get("event_digest")
            if declared_digest is not None:
                projection = dict(raw)
                projection.pop("event_digest", None)
                expected_digest = digest(projection)
                try:
                    observed_digest = require_digest(
                        declared_digest,
                        "event digest",
                    )
                except ValueError:
                    observed_digest = ""
                if observed_digest != expected_digest:
                    findings.append(
                        {
                            "code": "event-digest-mismatch",
                            "event_id": step.event_id,
                        }
                    )
        return findings

    def _duplicate_semantic_findings(
        self,
        steps: Sequence[ClassifiedStep],
    ) -> list[dict[str, Any]]:
        groups: dict[tuple[str, str, str], list[str]] = defaultdict(list)
        for item in steps:
            key = (item.event_type, item.effect, item.mutation_digest)
            groups[key].append(item.event_id)
        return [
            {
                "code": "semantic-transition-duplicated",
                "event_type": key[0],
                "effect": key[1],
                "event_ids": values[:100],
            }
            for key, values in sorted(groups.items())
            if len(values) > 1
            and key[0] not in {"verification-observed", "tool-completed"}
        ]


def normalize_event_type(value: Any) -> str:
    return (
        bounded_text(value, "event type", maximum_bytes=128)
        .lower()
        .replace("_", "-")
    )


def normalize_effect(value: Any) -> str:
    return (
        bounded_text(value, "event effect", maximum_bytes=128)
        .lower()
        .replace("_", "-")
    )


def exclusion_reason(
    *,
    event_type: str,
    effect: str,
    payload: Mapping[str, Any],
    mutation: Mapping[str, Any],
) -> str:
    if event_type in EXCLUDED_EVENT_TYPES:
        return "excluded-event-type"
    if effect not in SEMANTIC_EFFECTS:
        return "non-semantic-effect"
    if payload.get("noop") is True or mutation.get("noop") is True:
        return "explicit-noop"
    if payload.get("changed") is False or mutation.get("changed") is False:
        return "unchanged"
    if payload.get("replay") is True or mutation.get("replay") is True:
        return "replay"
    if not mutation and effect not in {
        "tool",
        "verification",
        "permission",
        "fault",
        "recovery",
    }:
        return "mutation-empty"
    normalized_keys = {str(key).lower().replace("_", "-") for key in mutation}
    if mutation and not (
        normalized_keys.intersection(MUTATION_KEYS)
        or any(key.endswith("-digest") for key in normalized_keys)
        or any(key.endswith("-id") for key in normalized_keys)
    ):
        return "mutation-not-semantic"
    before = mutation.get("before")
    after = mutation.get("after")
    if "before" in mutation and "after" in mutation and digest(before) == digest(after):
        return "before-after-identical"
    return ""


def detect_cycles(steps: Sequence[ClassifiedStep]) -> list[dict[str, Any]]:
    graph = {item.event_id: item.causal_parent_ids for item in steps}
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[list[str]] = []

    def visit(node: str, path: list[str]) -> None:
        if node in visited:
            return
        if node in visiting:
            try:
                start = path.index(node)
            except ValueError:
                start = 0
            cycles.append(path[start:] + [node])
            return
        visiting.add(node)
        path.append(node)
        for parent in graph.get(node, ()):
            if parent in graph:
                visit(parent, path)
        path.pop()
        visiting.remove(node)
        visited.add(node)

    for event_id in graph:
        visit(event_id, [])
    return [
        {"code": "causal-cycle", "event_ids": cycle[:100]}
        for cycle in cycles
    ]
