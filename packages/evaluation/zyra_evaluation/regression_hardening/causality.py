from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


class FactKind(StrEnum):
    EVENT = "event"
    SPAN = "span"
    TOOL_CALL = "tool_call"
    ARTIFACT = "artifact"
    ROUTE = "route"
    MUTATION = "mutation"
    CHECKPOINT = "checkpoint"
    PERMISSION = "permission"


class LinkDisposition(StrEnum):
    VALID = "valid"
    ORPHAN_SOURCE = "orphan_source"
    ORPHAN_TARGET = "orphan_target"
    CROSS_RUN = "cross_run"
    LATE = "late"
    FUTURE_CAUSE = "future_cause"
    SELF_LINK = "self_link"
    TYPE_MISMATCH = "type_mismatch"
    NO_EFFECT = "no_effect"
    DUPLICATE = "duplicate"
    AMBIGUOUS = "ambiguous"


_EFFECT_KINDS = {
    FactKind.TOOL_CALL,
    FactKind.ARTIFACT,
    FactKind.ROUTE,
    FactKind.MUTATION,
    FactKind.CHECKPOINT,
    FactKind.PERMISSION,
}


@dataclass(frozen=True, slots=True)
class CausalFact:
    fact_id: str
    kind: FactKind
    run_id: str
    task_id: str
    sequence: int
    timestamp_ns: int
    correlation_id: str = ""
    causation_id: str = ""
    span_id: str = ""
    parent_span_id: str = ""
    subject_id: str = ""
    state_before_digest: str = ""
    state_after_digest: str = ""
    status: str = ""
    attributes: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.fact_id or not self.run_id:
            raise ValueError("causal fact requires fact_id and run_id")
        if self.sequence < 0 or self.timestamp_ns < 0:
            raise ValueError("causal fact sequence/timestamp cannot be negative")
        object.__setattr__(self, "attributes", dict(self.attributes or {}))

    @property
    def changed_state(self) -> bool:
        return bool(
            self.state_after_digest
            and self.state_after_digest != self.state_before_digest
        )

    @property
    def effectful(self) -> bool:
        if self.kind is FactKind.ARTIFACT:
            return bool(self.subject_id or self.attributes.get("content_digest"))
        if self.kind is FactKind.ROUTE:
            return bool(
                self.attributes.get("selected_worker")
                or self.attributes.get("selected_target")
                or self.subject_id
            )
        if self.kind is FactKind.TOOL_CALL:
            return self.status.casefold() in {
                "completed",
                "succeeded",
                "failed",
                "denied",
                "cancelled",
            }
        if self.kind is FactKind.PERMISSION:
            return self.status.casefold() in {"allow", "deny", "ask", "expired"}
        if self.kind in {FactKind.MUTATION, FactKind.CHECKPOINT}:
            return self.changed_state or bool(self.attributes.get("committed"))
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "kind": self.kind.value,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "sequence": self.sequence,
            "timestamp_ns": self.timestamp_ns,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "subject_id": self.subject_id,
            "state_before_digest": self.state_before_digest,
            "state_after_digest": self.state_after_digest,
            "status": self.status,
            "effectful": self.effectful,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class CausalLink:
    source_id: str
    target_id: str
    link_type: str
    disposition: LinkDisposition
    reason: str

    @property
    def valid(self) -> bool:
        return self.disposition is LinkDisposition.VALID

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "link_type": self.link_type,
            "disposition": self.disposition.value,
            "valid": self.valid,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class CausalityReport:
    facts: tuple[CausalFact, ...]
    links: tuple[CausalLink, ...]
    orphan_events: tuple[str, ...]
    orphan_effects: tuple[str, ...]
    duplicate_facts: tuple[str, ...]
    sequence_regressions: tuple[str, ...]
    invalid_links: tuple[CausalLink, ...]
    kind_counts: Mapping[str, int]
    digest: str

    @property
    def valid(self) -> bool:
        return not (
            self.orphan_events
            or self.orphan_effects
            or self.duplicate_facts
            or self.sequence_regressions
            or self.invalid_links
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-bidirectional-causality/v1",
            "valid": self.valid,
            "facts": [item.to_dict() for item in self.facts],
            "links": [item.to_dict() for item in self.links],
            "orphan_events": list(self.orphan_events),
            "orphan_effects": list(self.orphan_effects),
            "duplicate_facts": list(self.duplicate_facts),
            "sequence_regressions": list(self.sequence_regressions),
            "invalid_links": [item.to_dict() for item in self.invalid_links],
            "kind_counts": dict(sorted(self.kind_counts.items())),
            "digest": self.digest,
        }


class FactNormalizer:
    _KIND_ALIASES = {
        "agent_message": FactKind.EVENT,
        "system_notice": FactKind.EVENT,
        "event": FactKind.EVENT,
        "span": FactKind.SPAN,
        "tool": FactKind.TOOL_CALL,
        "tool_call": FactKind.TOOL_CALL,
        "tool_result": FactKind.TOOL_CALL,
        "artifact": FactKind.ARTIFACT,
        "topology_route": FactKind.ROUTE,
        "worker_route": FactKind.ROUTE,
        "route": FactKind.ROUTE,
        "state_mutation": FactKind.MUTATION,
        "mutation": FactKind.MUTATION,
        "checkpoint": FactKind.CHECKPOINT,
        "permission": FactKind.PERMISSION,
        "permission_decision": FactKind.PERMISSION,
    }

    def normalize_many(
        self,
        values: Iterable[Mapping[str, Any]],
    ) -> tuple[CausalFact, ...]:
        return tuple(
            self.normalize(value, index=index)
            for index, value in enumerate(values)
        )

    def normalize(
        self,
        value: Mapping[str, Any],
        *,
        index: int = 0,
    ) -> CausalFact:
        payload = value.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        attributes: dict[str, Any] = {**payload}
        nested = self._first_nested(attributes)
        if nested is not attributes:
            attributes = {**attributes, **nested}
        raw_kind = str(
            value.get("fact_kind")
            or value.get("event_type")
            or value.get("type")
            or attributes.get("kind")
            or attributes.get("event_type")
            or "event"
        ).casefold()
        kind = self._kind(raw_kind, attributes)
        fact_id = str(
            value.get("fact_id")
            or value.get("event_id")
            or attributes.get("fact_id")
            or attributes.get("event_id")
            or attributes.get("span_id")
            or attributes.get("tool_call_id")
            or attributes.get("artifact_id")
            or attributes.get("mutation_id")
            or attributes.get("checkpoint_id")
            or stable_digest(index, raw_kind, value)[:31].replace(":", "-")
        )
        sequence = self._integer(
            value.get("sequence")
            or value.get("seq")
            or attributes.get("sequence")
            or attributes.get("seq")
            or index + 1
        )
        timestamp_ns = self._integer(
            value.get("timestamp_ns")
            or attributes.get("timestamp_ns")
            or sequence
        )
        return CausalFact(
            fact_id=fact_id,
            kind=kind,
            run_id=str(value.get("run_id") or attributes.get("run_id") or ""),
            task_id=str(value.get("task_id") or attributes.get("task_id") or ""),
            sequence=sequence,
            timestamp_ns=timestamp_ns,
            correlation_id=str(
                value.get("correlation_id")
                or attributes.get("correlation_id")
                or attributes.get("request_id")
                or ""
            ),
            causation_id=str(
                value.get("causation_id")
                or attributes.get("causation_id")
                or attributes.get("parent_event_id")
                or ""
            ),
            span_id=str(
                value.get("span_id")
                or attributes.get("span_id")
                or ""
            ),
            parent_span_id=str(
                value.get("parent_span_id")
                or attributes.get("parent_span_id")
                or ""
            ),
            subject_id=str(
                attributes.get("tool_call_id")
                or attributes.get("artifact_id")
                or attributes.get("mutation_id")
                or attributes.get("checkpoint_id")
                or attributes.get("selected_worker")
                or attributes.get("subject_id")
                or ""
            ),
            state_before_digest=str(
                attributes.get("state_before_digest")
                or attributes.get("before_digest")
                or ""
            ),
            state_after_digest=str(
                attributes.get("state_after_digest")
                or attributes.get("after_digest")
                or ""
            ),
            status=str(
                value.get("status")
                or attributes.get("status")
                or attributes.get("effect")
                or ""
            ),
            attributes=attributes,
        )

    def _kind(self, raw: str, attributes: Mapping[str, Any]) -> FactKind:
        if raw in self._KIND_ALIASES:
            return self._KIND_ALIASES[raw]
        if "artifact" in raw or attributes.get("artifact_id"):
            return FactKind.ARTIFACT
        if "permission" in raw:
            return FactKind.PERMISSION
        if "route" in raw or attributes.get("selected_worker"):
            return FactKind.ROUTE
        if "mutation" in raw or attributes.get("mutation_id"):
            return FactKind.MUTATION
        if "checkpoint" in raw or attributes.get("checkpoint_id"):
            return FactKind.CHECKPOINT
        if "tool" in raw or attributes.get("tool_call_id"):
            return FactKind.TOOL_CALL
        if "span" in raw or attributes.get("span_id"):
            return FactKind.SPAN
        return FactKind.EVENT

    @staticmethod
    def _first_nested(attributes: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in (
            "event",
            "span",
            "tool_call",
            "tool_result",
            "artifact",
            "route",
            "mutation",
            "checkpoint",
            "permission_decision",
        ):
            value = attributes.get(key)
            if isinstance(value, Mapping):
                return value
        return attributes

    @staticmethod
    def _integer(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0


class BidirectionalCausalityAuditor:
    """Requires every effect to have a prior event and every causal event an effect."""

    def __init__(
        self,
        *,
        maximum_sequence_gap: int = 10_000,
        maximum_timestamp_gap_ns: int = 3_600_000_000_000,
    ) -> None:
        self.maximum_sequence_gap = int(maximum_sequence_gap)
        self.maximum_timestamp_gap_ns = int(maximum_timestamp_gap_ns)

    def audit(self, facts: Sequence[CausalFact]) -> CausalityReport:
        ordered = tuple(sorted(facts, key=lambda item: (item.run_id, item.sequence, item.fact_id)))
        by_id: dict[str, CausalFact] = {}
        duplicates: list[str] = []
        for fact in ordered:
            if fact.fact_id in by_id:
                duplicates.append(fact.fact_id)
            else:
                by_id[fact.fact_id] = fact
        sequence_regressions = self._sequence_regressions(facts)
        links: list[CausalLink] = []
        incoming: Counter[str] = Counter()
        outgoing: Counter[str] = Counter()
        seen_pairs: set[tuple[str, str, str]] = set()
        for target in ordered:
            references = self._references(target)
            for link_type, source_id in references:
                source = by_id.get(source_id)
                link = self._link(source, target, link_type)
                pair = (link.source_id, link.target_id, link.link_type)
                if pair in seen_pairs:
                    link = replace(
                        link,
                        disposition=LinkDisposition.DUPLICATE,
                        reason="duplicate causal edge",
                    )
                seen_pairs.add(pair)
                links.append(link)
                if link.valid:
                    incoming[target.fact_id] += 1
                    outgoing[source_id] += 1
        orphan_effects = tuple(
            fact.fact_id
            for fact in ordered
            if fact.kind in _EFFECT_KINDS and incoming[fact.fact_id] == 0
        )
        orphan_events = tuple(
            fact.fact_id
            for fact in ordered
            if fact.kind in {FactKind.EVENT, FactKind.SPAN}
            and self._expects_effect(fact)
            and outgoing[fact.fact_id] == 0
        )
        invalid = tuple(link for link in links if not link.valid)
        kind_counts = Counter(fact.kind.value for fact in ordered)
        material = {
            "facts": [item.to_dict() for item in ordered],
            "links": [item.to_dict() for item in links],
            "orphan_events": orphan_events,
            "orphan_effects": orphan_effects,
            "duplicates": duplicates,
            "sequence_regressions": sequence_regressions,
        }
        return CausalityReport(
            facts=ordered,
            links=tuple(links),
            orphan_events=orphan_events,
            orphan_effects=orphan_effects,
            duplicate_facts=tuple(sorted(set(duplicates))),
            sequence_regressions=sequence_regressions,
            invalid_links=invalid,
            kind_counts=dict(kind_counts),
            digest=stable_digest(material),
        )

    def audit_mappings(
        self,
        values: Iterable[Mapping[str, Any]],
    ) -> CausalityReport:
        return self.audit(FactNormalizer().normalize_many(values))

    def mutation_campaign(
        self,
        valid_facts: Sequence[CausalFact],
    ) -> dict[str, CausalityReport]:
        if len(valid_facts) < 2:
            raise ValueError("causality mutation campaign requires at least two facts")
        baseline = self.audit(valid_facts)
        source = next(
            (
                item
                for item in valid_facts
                if item.kind in {FactKind.EVENT, FactKind.SPAN}
            ),
            valid_facts[0],
        )
        effect = next(
            (item for item in valid_facts if item.kind in _EFFECT_KINDS),
            valid_facts[-1],
        )
        fake = replace(
            effect,
            fact_id=f"{effect.fact_id}-fake",
            causation_id="missing-fact-id",
        )
        late = replace(
            effect,
            fact_id=f"{effect.fact_id}-late",
            causation_id=source.fact_id,
            sequence=source.sequence + self.maximum_sequence_gap + 1,
            timestamp_ns=source.timestamp_ns + self.maximum_timestamp_gap_ns + 1,
        )
        cross = replace(
            effect,
            fact_id=f"{effect.fact_id}-cross",
            causation_id=source.fact_id,
            run_id=f"{effect.run_id}-other",
        )
        no_effect = replace(
            effect,
            fact_id=f"{effect.fact_id}-noop",
            causation_id=source.fact_id,
            status="observed",
            subject_id="",
            state_before_digest="same",
            state_after_digest="same",
            attributes={},
        )
        orphan_event = replace(
            source,
            fact_id=f"{source.fact_id}-orphan",
            attributes={**source.attributes, "expects_effect": True},
        )
        return {
            "baseline": baseline,
            "fake": self.audit((*valid_facts, fake)),
            "late": self.audit((*valid_facts, late)),
            "cross_run": self.audit((*valid_facts, cross)),
            "no_effect": self.audit((*valid_facts, no_effect)),
            "orphan_event": self.audit((*valid_facts, orphan_event)),
        }

    def _link(
        self,
        source: CausalFact | None,
        target: CausalFact,
        link_type: str,
    ) -> CausalLink:
        source_id = (
            target.causation_id
            if link_type == "causation"
            else target.parent_span_id
            if link_type == "parent_span"
            else target.correlation_id
        )
        if source is None:
            return CausalLink(
                source_id=source_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.ORPHAN_SOURCE,
                reason="referenced source fact does not exist",
            )
        if source.fact_id == target.fact_id:
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.SELF_LINK,
                reason="fact cannot cause itself",
            )
        if source.run_id != target.run_id:
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.CROSS_RUN,
                reason="source and target belong to different runs",
            )
        if target.sequence <= source.sequence:
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.FUTURE_CAUSE,
                reason="target sequence does not follow source sequence",
            )
        if (
            target.sequence - source.sequence > self.maximum_sequence_gap
            or target.timestamp_ns - source.timestamp_ns
            > self.maximum_timestamp_gap_ns
        ):
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.LATE,
                reason="causal edge exceeds bounded sequence/time window",
            )
        if target.kind in _EFFECT_KINDS and not target.effectful:
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.NO_EFFECT,
                reason="target is labeled as an effect but changes no owner state or outcome",
            )
        if link_type == "parent_span" and (
            source.kind is not FactKind.SPAN
            or target.kind is not FactKind.SPAN
        ):
            return CausalLink(
                source_id=source.fact_id,
                target_id=target.fact_id,
                link_type=link_type,
                disposition=LinkDisposition.TYPE_MISMATCH,
                reason="parent_span edge must connect span facts",
            )
        return CausalLink(
            source_id=source.fact_id,
            target_id=target.fact_id,
            link_type=link_type,
            disposition=LinkDisposition.VALID,
            reason="source precedes an effectful target in the same run",
        )

    @staticmethod
    def _references(fact: CausalFact) -> tuple[tuple[str, str], ...]:
        values: list[tuple[str, str]] = []
        if fact.causation_id:
            values.append(("causation", fact.causation_id))
        if fact.parent_span_id:
            values.append(("parent_span", fact.parent_span_id))
        if (
            fact.correlation_id
            and fact.correlation_id not in {fact.causation_id, fact.parent_span_id}
        ):
            values.append(("correlation", fact.correlation_id))
        return tuple(values)

    @staticmethod
    def _expects_effect(fact: CausalFact) -> bool:
        return bool(
            fact.attributes.get("expects_effect")
            or fact.attributes.get("command")
            or fact.attributes.get("requested_action")
            or fact.status.casefold() in {"started", "requested", "accepted"}
        )

    @staticmethod
    def _sequence_regressions(
        facts: Sequence[CausalFact],
    ) -> tuple[str, ...]:
        by_run: dict[str, list[CausalFact]] = defaultdict(list)
        for fact in facts:
            by_run[fact.run_id].append(fact)
        regressions: list[str] = []
        for run_id, values in by_run.items():
            previous = -1
            seen: set[int] = set()
            for fact in values:
                if fact.sequence in seen:
                    regressions.append(f"{run_id}:duplicate:{fact.sequence}")
                if fact.sequence < previous:
                    regressions.append(
                        f"{run_id}:regression:{previous}->{fact.sequence}"
                    )
                seen.add(fact.sequence)
                previous = fact.sequence
        return tuple(regressions)


def evaluate_causality_campaign(
    facts: Sequence[CausalFact] | Iterable[Mapping[str, Any]],
) -> CaseExecutionBuffer:
    values = tuple(facts)
    normalized: tuple[CausalFact, ...]
    if values and isinstance(values[0], CausalFact):
        normalized = tuple(values)  # type: ignore[arg-type]
    else:
        normalized = FactNormalizer().normalize_many(values)  # type: ignore[arg-type]
    auditor = BidirectionalCausalityAuditor()
    reports = auditor.mutation_campaign(normalized)
    buffer = CaseExecutionBuffer()
    baseline_observation = buffer.observe(
        "causality.baseline",
        ObservationKind.EVENT,
        "event-effect-bidirectional-graph",
        "accepted" if reports["baseline"].valid else "rejected",
        attributes={
            "digest": reports["baseline"].digest,
            "fact_count": len(reports["baseline"].facts),
            "link_count": len(reports["baseline"].links),
        },
    )
    buffer.assert_that(
        "causality.baseline-valid",
        reports["baseline"].valid,
        "valid event/effect graph must be accepted",
        evidence=(baseline_observation.observation_id,),
    )
    for name in ("fake", "late", "cross_run", "no_effect", "orphan_event"):
        report = reports[name]
        observation = buffer.observe(
            f"causality.{name}",
            ObservationKind.MUTATION,
            f"causality-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "digest": report.digest,
                "invalid_link_count": len(report.invalid_links),
                "orphan_event_count": len(report.orphan_events),
                "orphan_effect_count": len(report.orphan_effects),
            },
        )
        buffer.assert_that(
            f"causality.reject-{name}",
            not report.valid,
            f"{name} causality mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.CONTRACT,
        )
    return buffer


__all__ = [
    "BidirectionalCausalityAuditor",
    "CausalFact",
    "CausalLink",
    "CausalityReport",
    "FactKind",
    "FactNormalizer",
    "LinkDisposition",
    "evaluate_causality_campaign",
]
