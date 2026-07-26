from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .model import (
    AuditCatalog,
    AuditSection,
    Disposition,
    EffectKind,
    EventMutationSpec,
    EvidencePointer,
    Finding,
    RuleSwitches,
    Severity,
    SourceRef,
    deduplicate_findings,
    finding,
    section,
)
from .python_graph import CallFact, EventFact, PythonFileFacts, PythonGraphResult
from .reachability import CodeGraph, PathResult
from .script_graph import (
    ScriptCallFact,
    ScriptEventFact,
    ScriptFileFacts,
    ScriptGraphResult,
)


MUTATION_ATTRIBUTE_PATTERN = re.compile(
    r"(?i)\b(?:revision|version|epoch|sequence|state|status|route|placement|"
    r"lease|permit|checkpoint|artifact|metric|digest|mutation|effect|"
    r"causation|correlation|span|tool_call|worker|provider)\b"
)
NON_CAUSAL_EVENT_PATTERN = re.compile(
    r"(?i)\b(?:heartbeat|log|debug|ui_repaint|render|replay|health|noop|no_op)\b"
)


@dataclass(frozen=True, slots=True)
class CausalityObservation:
    domain: str
    link_id: str
    event_name: str
    effect_kind: str
    producer_exists: bool
    event_found: bool
    event_line: int
    event_scope: str
    event_attributes: tuple[str, ...]
    required_attributes_found: tuple[str, ...]
    required_attributes_missing: tuple[str, ...]
    mutation_exists: bool
    mutation_signal: bool
    mutation_line: int
    mutation_scope: str
    producer_to_mutation: PathResult
    artifact_exists: bool
    metric_exists: bool
    verifier_exists: bool
    semantic_effect: bool

    @property
    def valid(self) -> bool:
        return (
            self.producer_exists
            and self.event_found
            and not self.required_attributes_missing
            and self.mutation_exists
            and self.mutation_signal
            and self.producer_to_mutation.reachable
            and self.artifact_exists
            and self.metric_exists
            and self.verifier_exists
            and self.semantic_effect
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "link_id": self.link_id,
            "event_name": self.event_name,
            "effect_kind": self.effect_kind,
            "producer_exists": self.producer_exists,
            "event_found": self.event_found,
            "event_line": self.event_line,
            "event_scope": self.event_scope,
            "event_attributes": list(self.event_attributes),
            "required_attributes_found": list(self.required_attributes_found),
            "required_attributes_missing": list(self.required_attributes_missing),
            "mutation_exists": self.mutation_exists,
            "mutation_signal": self.mutation_signal,
            "mutation_line": self.mutation_line,
            "mutation_scope": self.mutation_scope,
            "producer_to_mutation": self.producer_to_mutation.to_dict(),
            "artifact_exists": self.artifact_exists,
            "metric_exists": self.metric_exists,
            "verifier_exists": self.verifier_exists,
            "semantic_effect": self.semantic_effect,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class CausalityResult:
    observations: tuple[CausalityObservation, ...]
    section: AuditSection

    @property
    def valid_link_ids(self) -> frozenset[str]:
        return frozenset(item.link_id for item in self.observations if item.valid)


class CausalityAuditor:
    def __init__(
        self,
        project_root: str | Path,
        *,
        switches: RuleSwitches | None = None,
    ) -> None:
        self.root = Path(project_root).resolve(strict=False)
        self.switches = switches or RuleSwitches()
        self._text_cache: dict[str, str] = {}

    def audit(
        self,
        catalog: AuditCatalog,
        graph: CodeGraph,
        python: PythonGraphResult,
        script: ScriptGraphResult,
    ) -> CausalityResult:
        if not self.switches.causality:
            return CausalityResult(
                observations=(),
                section=section(
                    "event_mutation_causality",
                    metrics={
                        "rule_enabled": False,
                        "declared_links": sum(
                            len(item.events) for item in catalog.owners
                        ),
                    },
                ),
            )
        observations: list[CausalityObservation] = []
        findings: list[Finding] = []
        evidence: list[EvidencePointer] = []
        for owner_contract in catalog.owners:
            for link in owner_contract.events:
                observation = self._observe(
                    owner_contract.domain,
                    link,
                    graph,
                    python.by_path,
                    script.by_path,
                )
                observations.append(observation)
                findings.extend(self._findings(observation, link))
                evidence.append(
                    EvidencePointer(
                        kind="event_mutation_link",
                        path=link.producer.path,
                        line=observation.event_line,
                        symbol=link.event_name,
                        attributes={
                            "domain": owner_contract.domain,
                            "link_id": link.link_id,
                            "effect_kind": link.effect_kind.value,
                            "mutation": link.mutation.key,
                            "valid": observation.valid,
                        },
                    )
                )
        findings.extend(self._duplicate_event_authority(catalog))
        findings.extend(
            self._unbound_semantic_events(
                catalog,
                python,
                script,
            )
        )
        metrics = {
            "rule_enabled": True,
            "declared_links": len(observations),
            "valid_links": sum(item.valid for item in observations),
            "invalid_links": sum(not item.valid for item in observations),
            "event_found": sum(item.event_found for item in observations),
            "mutation_signal": sum(item.mutation_signal for item in observations),
            "semantic_effect": sum(item.semantic_effect for item in observations),
            "effect_kinds": dict(
                sorted(Counter(item.effect_kind for item in observations).items())
            ),
            "domains_with_valid_link": len(
                {item.domain for item in observations if item.valid}
            ),
        }
        return CausalityResult(
            observations=tuple(observations),
            section=section(
                "event_mutation_causality",
                metrics=metrics,
                findings=findings,
                evidence=evidence,
                records=(item.to_dict() for item in observations),
            ),
        )

    def _observe(
        self,
        domain: str,
        link: EventMutationSpec,
        graph: CodeGraph,
        python_by_path: Mapping[str, PythonFileFacts],
        script_by_path: Mapping[str, ScriptFileFacts],
    ) -> CausalityObservation:
        producer = graph.resolve_ref(link.producer)
        mutation = graph.resolve_ref(link.mutation)
        event_line, event_scope, event_attributes = self._event_fact(
            link,
            python_by_path.get(link.producer.path),
            script_by_path.get(link.producer.path),
        )
        event_found = bool(event_line) or self._event_text_found(link)
        if event_found and not event_line:
            event_line = self._line_for(link.producer.path, link.event_name)
        mutation_line, mutation_scope, mutation_signal = self._mutation_fact(
            link,
            python_by_path.get(link.mutation.path),
            script_by_path.get(link.mutation.path),
        )
        if not mutation_signal:
            mutation_signal = self._mutation_text_found(link.mutation)
        required_found = tuple(
            item
            for item in link.required_attributes
            if self._attribute_found(item, link, event_attributes)
        )
        required_missing = tuple(
            item for item in link.required_attributes if item not in required_found
        )
        path = graph.shortest_path(
            producer.node_id,
            mutation.node_id,
            max_depth=64,
        )
        if (
            not path.reachable
            and producer.node_id
            and mutation.node_id
            and self._declared_causal_edge(link)
        ):
            graph.add_edge(
                self._causal_edge(link, producer.node_id, mutation.node_id)
            )
            path = graph.shortest_path(
                producer.node_id,
                mutation.node_id,
                max_depth=64,
            )
        artifact_exists = self._optional_ref_valid(graph, link.artifact)
        metric_exists = self._optional_ref_valid(graph, link.metric)
        verifier_exists = self._optional_ref_valid(graph, link.verifier)
        semantic_effect = self._semantic_effect(
            link,
            mutation_signal,
            artifact_exists,
            metric_exists,
        )
        return CausalityObservation(
            domain=domain,
            link_id=link.link_id,
            event_name=link.event_name,
            effect_kind=link.effect_kind.value,
            producer_exists=producer.valid,
            event_found=event_found,
            event_line=event_line,
            event_scope=event_scope,
            event_attributes=event_attributes,
            required_attributes_found=required_found,
            required_attributes_missing=required_missing,
            mutation_exists=mutation.valid,
            mutation_signal=mutation_signal,
            mutation_line=mutation_line,
            mutation_scope=mutation_scope,
            producer_to_mutation=path,
            artifact_exists=artifact_exists,
            metric_exists=metric_exists,
            verifier_exists=verifier_exists,
            semantic_effect=semantic_effect,
        )

    def _event_fact(
        self,
        link: EventMutationSpec,
        python_fact: PythonFileFacts | None,
        script_fact: ScriptFileFacts | None,
    ) -> tuple[int, str, tuple[str, ...]]:
        expected = link.event_name.casefold()
        if python_fact is not None:
            candidates = [
                item
                for item in python_fact.events
                if self._event_matches(expected, item.event_name)
                and self._scope_matches(link.producer.symbol, item.scope)
            ]
            if candidates:
                selected = sorted(candidates, key=lambda item: item.line)[0]
                return selected.line, selected.scope, selected.attributes
        if script_fact is not None:
            candidates = [
                item
                for item in script_fact.events
                if self._event_matches(expected, item.event_name)
                and self._scope_matches(link.producer.symbol, item.scope)
            ]
            if candidates:
                selected = sorted(candidates, key=lambda item: item.line)[0]
                return selected.line, selected.scope, selected.attributes
        return 0, "", ()

    def _mutation_fact(
        self,
        link: EventMutationSpec,
        python_fact: PythonFileFacts | None,
        script_fact: ScriptFileFacts | None,
    ) -> tuple[int, str, bool]:
        expected = link.mutation.symbol.rsplit(".", 1)[-1].casefold()
        if python_fact is not None:
            candidates = [
                item
                for item in python_fact.calls
                if item.write_like
                and (
                    not expected
                    or expected in item.qualified_name.casefold()
                    or expected in item.scope.casefold()
                )
            ]
            if candidates:
                selected = sorted(candidates, key=lambda item: item.line)[0]
                return selected.line, selected.scope, True
            assignments = [
                item
                for item in python_fact.assignments
                if item.persistent
                and (
                    not expected
                    or expected in item.scope.casefold()
                    or expected in item.target.casefold()
                )
            ]
            if assignments:
                selected = sorted(assignments, key=lambda item: item.line)[0]
                return selected.line, selected.scope, True
        if script_fact is not None:
            candidates = [
                item
                for item in script_fact.calls
                if item.write_like
                and (
                    not expected
                    or expected in item.callee.casefold()
                    or expected in item.scope.casefold()
                )
            ]
            if candidates:
                selected = sorted(candidates, key=lambda item: item.line)[0]
                return selected.line, selected.scope, True
            assignments = [
                item
                for item in script_fact.assignments
                if item.persistent
                and (
                    not expected
                    or expected in item.scope.casefold()
                    or expected in item.target.casefold()
                )
            ]
            if assignments:
                selected = sorted(assignments, key=lambda item: item.line)[0]
                return selected.line, selected.scope, True
        return 0, "", False

    def _event_text_found(self, link: EventMutationSpec) -> bool:
        text = self._read(link.producer.path)
        if not text:
            return False
        return (
            link.event_name in text
            and (
                not link.producer.symbol
                or link.producer.symbol.rsplit(".", 1)[-1] in text
            )
        )

    def _mutation_text_found(self, reference: SourceRef) -> bool:
        text = self._read(reference.path)
        if not text:
            return False
        symbol = reference.symbol.rsplit(".", 1)[-1]
        if symbol and symbol not in text:
            return False
        if reference.selector and reference.selector not in text:
            return False
        return bool(
            re.search(
                r"(?i)\b(?:commit|save|store|persist|write|append|insert|"
                r"update|upsert|replace|delete|mutate|transition|set)\b",
                text,
            )
        )

    def _attribute_found(
        self,
        attribute: str,
        link: EventMutationSpec,
        observed_attributes: Sequence[str],
    ) -> bool:
        if attribute in observed_attributes:
            return True
        text = self._read(link.producer.path)
        if not text:
            return False
        return re.search(rf"\b{re.escape(attribute)}\b", text) is not None

    def _declared_causal_edge(self, link: EventMutationSpec) -> bool:
        producer_text = self._read(link.producer.path)
        mutation_text = self._read(link.mutation.path)
        if not producer_text or not mutation_text:
            return False
        target_candidates = {
            link.mutation.symbol,
            link.mutation.symbol.rsplit(".", 1)[-1],
            Path(link.mutation.path).stem,
            link.mutation.path.replace("\\", "/"),
        }
        if link.producer.path == link.mutation.path:
            return (
                link.event_name in producer_text
                and any(
                    candidate and candidate in producer_text
                    for candidate in target_candidates
                )
            )
        return any(
            candidate and candidate in producer_text
            for candidate in target_candidates
        )

    @staticmethod
    def _causal_edge(
        link: EventMutationSpec,
        source: str,
        target: str,
    ):
        from .model import EdgeKind, GraphEdge

        return GraphEdge(
            source=source,
            target=target,
            kind=EdgeKind.DECLARED,
            path=link.producer.path,
            verified=True,
            attributes={
                "link_id": link.link_id,
                "event_name": link.event_name,
                "causal": True,
            },
        )

    @staticmethod
    def _optional_ref_valid(
        graph: CodeGraph,
        reference: SourceRef | None,
    ) -> bool:
        return reference is None or graph.resolve_ref(reference).valid

    @staticmethod
    def _semantic_effect(
        link: EventMutationSpec,
        mutation_signal: bool,
        artifact_exists: bool,
        metric_exists: bool,
    ) -> bool:
        if not mutation_signal:
            return False
        if NON_CAUSAL_EVENT_PATTERN.search(link.event_name):
            return False
        if link.effect_kind is EffectKind.ARTIFACT:
            return artifact_exists
        if link.effect_kind is EffectKind.METRIC:
            return metric_exists
        return True

    def _findings(
        self,
        observation: CausalityObservation,
        link: EventMutationSpec,
    ) -> tuple[Finding, ...]:
        findings: list[Finding] = []
        common = {
            "domain": observation.domain,
            "path": link.producer.path,
            "owner_unit": "M3-01B",
            "disposition": Disposition.BLOCK_RELEASE,
            "default_path_impact": (
                f"Event {link.event_name} cannot prove a real "
                f"{link.effect_kind.value} effect."
            ),
        }
        if not observation.producer_exists:
            findings.append(
                finding(
                    "causal_event_producer_missing",
                    f"Event producer is missing: {link.producer.key}.",
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation="Point the link at the real canonical producer.",
                    **common,
                )
            )
        elif not observation.event_found:
            findings.append(
                finding(
                    "causal_event_not_emitted",
                    (
                        f"Producer does not emit the declared event "
                        f"{link.event_name!r}."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Emit a typed canonical event/receipt after the effect or "
                        "remove the unsupported evidence claim."
                    ),
                    **common,
                )
            )
        if observation.required_attributes_missing:
            findings.append(
                finding(
                    "causal_event_identity_attributes_missing",
                    (
                        f"Event {link.event_name!r} lacks required causal identity "
                        "attributes."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Bind exact run/task/span/call/mutation/revision identities "
                        "required by the contract."
                    ),
                    attributes={
                        "missing": list(observation.required_attributes_missing)
                    },
                    **common,
                )
            )
        if not observation.mutation_exists:
            findings.append(
                finding(
                    "causal_mutation_target_missing",
                    f"Mutation target is missing: {link.mutation.key}.",
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation="Point the link at a real canonical state write.",
                    **common,
                )
            )
        elif not observation.mutation_signal:
            findings.append(
                finding(
                    "event_without_semantic_mutation",
                    (
                        f"Event {link.event_name!r} exists but its target has no "
                        "observable state mutation."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Connect the event to a transaction, route, tool, permission, "
                        "compact/restore, fault/recovery, artifact, or metric effect."
                    ),
                    **common,
                )
            )
        if not observation.producer_to_mutation.reachable:
            findings.append(
                finding(
                    "event_mutation_path_unreachable",
                    (
                        f"Event producer cannot reach mutation target for "
                        f"{link.link_id}."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Add an executable call/import edge; hand-written evidence "
                        "mapping is insufficient."
                    ),
                    attributes={
                        "reason": observation.producer_to_mutation.reason
                    },
                    **common,
                )
            )
        if not observation.artifact_exists:
            findings.append(
                finding(
                    "causal_artifact_target_missing",
                    f"Artifact evidence target is missing for {link.link_id}.",
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation="Bind the event to a real artifact owner/output.",
                    **common,
                )
            )
        if not observation.metric_exists:
            findings.append(
                finding(
                    "causal_metric_target_missing",
                    f"Metric evidence target is missing for {link.link_id}.",
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation="Bind the event to raw metric custody and aggregation.",
                    **common,
                )
            )
        if not observation.verifier_exists:
            findings.append(
                finding(
                    "causal_verifier_missing",
                    f"Behavior verifier is missing for {link.link_id}.",
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation="Add a real behavior verifier for the claimed effect.",
                    **common,
                )
            )
        if not observation.semantic_effect:
            findings.append(
                finding(
                    "fake_causation_non_effect_event",
                    (
                        f"Declared event {link.event_name!r} is a heartbeat/log/"
                        "replay/UI/no-op or lacks its required output."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    remediation=(
                        "Use a canonical mutation/effect event; do not count "
                        "observability-only activity as semantic work."
                    ),
                    **common,
                )
            )
        return deduplicate_findings(findings)

    def _duplicate_event_authority(
        self,
        catalog: AuditCatalog,
    ) -> tuple[Finding, ...]:
        by_event: dict[str, list[tuple[str, EventMutationSpec]]] = defaultdict(list)
        for owner_contract in catalog.owners:
            for event in owner_contract.events:
                by_event[event.event_name].append((owner_contract.domain, event))
        findings: list[Finding] = []
        for event_name, bindings in sorted(by_event.items()):
            mutation_keys = {item.mutation.key for _, item in bindings}
            domains = {domain for domain, _ in bindings}
            if len(mutation_keys) <= 1:
                continue
            findings.append(
                finding(
                    "event_name_has_ambiguous_mutation_owner",
                    (
                        f"One canonical event name maps to multiple mutation owners: "
                        f"{event_name}."
                    ),
                    "causality",
                    severity=Severity.BLOCKER,
                    domain=",".join(sorted(domains)),
                    owner_unit="M3-01B",
                    disposition=Disposition.BLOCK_RELEASE,
                    default_path_impact=(
                        "Replay and trace cannot deterministically identify the effect."
                    ),
                    remediation=(
                        "Use distinct typed event identities or one shared canonical "
                        "mutation owner."
                    ),
                    attributes={"mutations": sorted(mutation_keys)},
                )
            )
        return deduplicate_findings(findings)

    def _unbound_semantic_events(
        self,
        catalog: AuditCatalog,
        python: PythonGraphResult,
        script: ScriptGraphResult,
    ) -> tuple[Finding, ...]:
        declared = {
            item.event_name.casefold()
            for owner_contract in catalog.owners
            for item in owner_contract.events
        }
        candidates: list[tuple[str, int, str]] = []
        for file_fact in python.files:
            if not file_fact.production:
                continue
            for event in file_fact.events:
                if (
                    event.event_name != "<dynamic>"
                    and event.event_name.casefold() not in declared
                    and MUTATION_ATTRIBUTE_PATTERN.search(event.event_name)
                ):
                    candidates.append(
                        (file_fact.path, event.line, event.event_name)
                    )
        for file_fact in script.files:
            if not file_fact.production:
                continue
            for event in file_fact.events:
                if (
                    event.event_name != "<dynamic>"
                    and event.event_name.casefold() not in declared
                    and MUTATION_ATTRIBUTE_PATTERN.search(event.event_name)
                ):
                    candidates.append(
                        (file_fact.path, event.line, event.event_name)
                    )
        return tuple(
            finding(
                "semantic_event_not_in_causality_catalog",
                f"Mutation-shaped production event is not causality-bound: {name}.",
                "causality",
                severity=Severity.WARNING,
                path=path,
                line=line,
                owner_unit="M3-01B",
                disposition=Disposition.TRACK,
                remediation=(
                    "Classify the event as canonical mutation evidence or document "
                    "why it is derived/non-semantic."
                ),
            )
            for path, line, name in sorted(candidates)[:500]
        )

    @staticmethod
    def _event_matches(expected: str, actual: str) -> bool:
        selected = actual.casefold()
        return selected == expected or expected in selected or selected in expected

    @staticmethod
    def _scope_matches(expected: str, actual: str) -> bool:
        if not expected:
            return True
        token = expected.rsplit(".", 1)[-1].casefold()
        return not actual or token in actual.casefold()

    def _read(self, relative: str) -> str:
        normalized = relative.replace("\\", "/")
        if normalized in self._text_cache:
            return self._text_cache[normalized]
        path = (self.root / normalized).resolve(strict=False)
        try:
            path.relative_to(self.root)
            text = path.read_text(encoding="utf-8")
        except (ValueError, OSError, UnicodeDecodeError):
            text = ""
        self._text_cache[normalized] = text
        return text

    def _line_for(self, relative: str, token: str) -> int:
        return next(
            (
                index
                for index, line in enumerate(
                    self._read(relative).splitlines(),
                    start=1,
                )
                if token in line
            ),
            0,
        )
