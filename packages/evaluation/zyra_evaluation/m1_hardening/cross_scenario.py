from __future__ import annotations

import json
import random
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity
from .integration_contracts import ScenarioEvidence, ScenarioKind, event_identity, stable_digest
from .integration_scenarios import M1IntegrationScenarioSuite


@dataclass(frozen=True, slots=True)
class EventProjection:
    event_id: str
    run_id: str
    task_id: str
    event_type: str
    sequence: int
    revision: int
    causation_id: str
    parent_event_id: str
    action_id: str
    tool_call_id: str
    side_effect_id: str
    state_family: str
    semantic_digest: str
    payload: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "EventProjection":
        payload = value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
        event_id, run_id, task_id = event_identity(value)
        return cls(
            event_id=event_id,
            run_id=run_id,
            task_id=task_id,
            event_type=str(value.get("event_type") or value.get("type") or "").lower(),
            sequence=_int(value.get("sequence"), _int(payload.get("sequence"), index)),
            revision=_int(payload.get("after_revision"), _int(payload.get("revision"), 0)),
            causation_id=str(value.get("causation_id") or payload.get("causation_id") or payload.get("request_id") or ""),
            parent_event_id=str(value.get("parent_event_id") or payload.get("parent_event_id") or ""),
            action_id=str(value.get("action_id") or payload.get("action_id") or ""),
            tool_call_id=str(payload.get("tool_call_id") or payload.get("tool_use_id") or ""),
            side_effect_id=str(payload.get("side_effect_id") or payload.get("idempotency_key") or ""),
            state_family=str(payload.get("state_family") or payload.get("semantic_family") or ""),
            semantic_digest=stable_digest(payload.get("after") if "after" in payload else payload),
            payload=dict(payload),
        )


@dataclass(slots=True)
class CausalPartition:
    run_id: str
    task_id: str
    events: list[EventProjection]
    by_id: dict[str, EventProjection] = field(init=False)
    children: dict[str, list[str]] = field(init=False)

    def __post_init__(self) -> None:
        self.by_id = {item.event_id: item for item in self.events if item.event_id}
        self.children = defaultdict(list)
        for event in self.events:
            parent = event.parent_event_id or event.causation_id
            if parent in self.by_id:
                self.children[parent].append(event.event_id)

    def reachable(self, start: str, predicate: Any, *, maximum_depth: int = 30) -> bool:
        if start not in self.by_id:
            return False
        queue: deque[tuple[str, int]] = deque([(start, 0)])
        visited: set[str] = set()
        while queue:
            current, depth = queue.popleft()
            if current in visited or depth > maximum_depth:
                continue
            visited.add(current)
            event = self.by_id[current]
            if current != start and predicate(event):
                return True
            for child in self.children.get(current, ()):
                queue.append((child, depth + 1))
        return False

    def roots(self) -> tuple[EventProjection, ...]:
        return tuple(
            event
            for event in self.events
            if not event.parent_event_id and event.causation_id not in self.by_id
        )


class CrossScenarioConsistencyGate:
    def __init__(self) -> None:
        self.required_ids = set(M1IntegrationScenarioSuite().scenario_ids())

    def evaluate(
        self,
        scenarios: Sequence[ScenarioEvidence],
        *,
        final_completion: bool,
        supporting_gates: Sequence[GateResult] = (),
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-cross-scenario-consistency",
            status=GateStatus.NOT_RUN,
            summary="Cross-scenario identity, causality, side-effect and semantic-effect consistency.",
        )
        self._identity_findings(scenarios, result, final_completion=final_completion)
        projections = self._projections(scenarios)
        partitions = self._partitions(projections)
        self._event_identity_findings(projections, result)
        self._partition_findings(partitions, result)
        self._side_effect_findings(projections, result)
        self._scenario_semantic_findings(scenarios, projections, result, final_completion=final_completion)
        self._cross_cutting_findings(
            scenarios,
            supporting_gates,
            result,
            final_completion=final_completion,
        )
        result.metrics.update(
            {
                "scenario_count": len(scenarios),
                "event_count": len(projections),
                "partition_count": len(partitions),
                "unique_event_count": len({item.event_id for item in projections if item.event_id}),
                "unique_action_count": len({item.action_id for item in projections if item.action_id}),
                "unique_side_effect_count": len({item.side_effect_id for item in projections if item.side_effect_id}),
                "scenario_digests": {item.scenario_id: item.digest() for item in scenarios},
                "event_type_counts": dict(Counter(item.event_type for item in projections)),
                "state_family_counts": dict(Counter(item.state_family for item in projections if item.state_family)),
            }
        )
        for scenario in scenarios:
            result.evidence.append(
                EvidencePointer(
                    kind="cross_scenario_partition",
                    location=scenario.scenario_id,
                    summary=f"task={scenario.task_id}; run={scenario.run_id}; events={len(scenario.events)}",
                    revision=str(scenario.final_revision or ""),
                    causation_id=scenario.run_id,
                    metadata={"scenario_digest": scenario.digest()},
                )
            )
        return result.finish(default_partial=not final_completion and len(scenarios) < len(self.required_ids))

    def _identity_findings(
        self,
        scenarios: Sequence[ScenarioEvidence],
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        ids = [item.scenario_id for item in scenarios]
        duplicates = Counter(ids)
        for scenario_id, count in duplicates.items():
            if count > 1:
                result.add(
                    Finding(
                        code="cross.scenario_duplicate",
                        severity=Severity.BLOCKER,
                        summary="Integration suite contains duplicate scenario evidence.",
                        detail=f"{scenario_id}: count={count}",
                    )
                )
        if final_completion:
            for scenario_id in sorted(self.required_ids - set(ids)):
                result.add(
                    Finding(
                        code="cross.scenario_missing",
                        severity=Severity.BLOCKER,
                        summary="Cross-scenario audit lacks one required scenario.",
                        detail=scenario_id,
                    )
                )
        task_ids = [item.task_id for item in scenarios if item.task_id]
        run_ids = [item.run_id for item in scenarios if item.run_id]
        if len(task_ids) != len(set(task_ids)):
            result.add(
                Finding(
                    code="cross.task_reused",
                    severity=Severity.BLOCKER,
                    summary="Independent integration scenarios reused a canonical task identity.",
                )
            )
        if len(run_ids) != len(set(run_ids)):
            result.add(
                Finding(
                    code="cross.run_reused",
                    severity=Severity.BLOCKER,
                    summary="Independent integration scenarios reused a canonical run identity.",
                )
            )
        for scenario in scenarios:
            if scenario.revision_advance <= 0:
                result.add(
                    Finding(
                        code="cross.scenario_no_revision",
                        severity=Severity.BLOCKER,
                        summary="Scenario produced no canonical revision advance.",
                        detail=scenario.scenario_id,
                    )
                )
            if not scenario.steps or not scenario.events:
                result.add(
                    Finding(
                        code="cross.scenario_not_dynamic",
                        severity=Severity.BLOCKER,
                        summary="Scenario has no real API steps or canonical events.",
                        detail=scenario.scenario_id,
                    )
                )

    @staticmethod
    def _projections(scenarios: Sequence[ScenarioEvidence]) -> list[EventProjection]:
        values: list[EventProjection] = []
        for scenario in scenarios:
            for index, event in enumerate(scenario.events):
                projection = EventProjection.from_mapping(event, index)
                values.append(projection)
        return values

    @staticmethod
    def _partitions(projections: Sequence[EventProjection]) -> list[CausalPartition]:
        grouped: dict[tuple[str, str], list[EventProjection]] = defaultdict(list)
        for event in projections:
            grouped[(event.run_id, event.task_id)].append(event)
        return [
            CausalPartition(run_id=run_id, task_id=task_id, events=sorted(events, key=lambda item: item.sequence))
            for (run_id, task_id), events in sorted(grouped.items())
        ]

    @staticmethod
    def _event_identity_findings(projections: Sequence[EventProjection], result: GateResult) -> None:
        ids: Counter[str] = Counter(item.event_id for item in projections if item.event_id)
        for event_id, count in ids.items():
            if count > 1:
                result.add(
                    Finding(
                        code="cross.event_id_duplicate",
                        severity=Severity.BLOCKER,
                        summary="Canonical event identity appears more than once across scenarios.",
                        detail=f"{event_id}: count={count}",
                    )
                )
        for event in projections:
            if not event.event_id or not event.run_id or not event.task_id:
                result.add(
                    Finding(
                        code="cross.event_partition_missing",
                        severity=Severity.BLOCKER,
                        summary="Scenario event lacks canonical event/run/task identity.",
                        detail=event.event_type,
                    )
                )

    @staticmethod
    def _partition_findings(partitions: Sequence[CausalPartition], result: GateResult) -> None:
        for partition in partitions:
            sequences = [item.sequence for item in partition.events]
            if sequences != sorted(sequences):
                result.add(
                    Finding(
                        code="cross.sequence_out_of_order",
                        severity=Severity.BLOCKER,
                        summary="Canonical event partition is not sequence ordered.",
                        detail=f"{partition.run_id}/{partition.task_id}",
                    )
                )
            if len(set(sequences)) != len(sequences):
                result.add(
                    Finding(
                        code="cross.sequence_duplicate",
                        severity=Severity.BLOCKER,
                        summary="Canonical event partition contains duplicate sequence values.",
                        detail=f"{partition.run_id}/{partition.task_id}",
                    )
                )
            revisions = [item.revision for item in partition.events if item.revision > 0]
            if revisions and revisions != sorted(revisions):
                result.add(
                    Finding(
                        code="cross.revision_regressed",
                        severity=Severity.BLOCKER,
                        summary="Canonical state revision regressed within one task/run partition.",
                        detail=f"{partition.run_id}/{partition.task_id}",
                    )
                )
            for event in partition.events:
                parent = event.parent_event_id
                causal_event = (
                    event.causation_id
                    if event.causation_id.startswith(("event_", "event-", "evt_", "evt-"))
                    else ""
                )
                missing = parent or causal_event
                if (
                    missing
                    and missing not in partition.by_id
                    and not event.payload.get("external_causation")
                ):
                    result.add(
                        Finding(
                            code="cross.causation_orphan",
                            severity=Severity.ERROR,
                            summary="Scenario event references a missing causal parent.",
                            detail=f"{event.event_id} -> {missing}",
                        )
                    )

    @staticmethod
    def _side_effect_findings(projections: Sequence[EventProjection], result: GateResult) -> None:
        by_effect: dict[str, list[EventProjection]] = defaultdict(list)
        for event in projections:
            if event.side_effect_id:
                by_effect[event.side_effect_id].append(event)
        for side_effect_id, events in by_effect.items():
            committed = [
                event
                for event in events
                if any(token in event.event_type for token in ("commit", "completed", "written", "settled"))
                or event.payload.get("committed") is True
            ]
            semantic = {event.semantic_digest for event in committed}
            if len(committed) > 1 and len(semantic) > 1:
                result.add(
                    Finding(
                        code="cross.side_effect_recommitted",
                        severity=Severity.BLOCKER,
                        summary="One idempotency identity committed different side effects.",
                        detail=side_effect_id,
                    )
                )
            if len(committed) > 1:
                result.add(
                    Finding(
                        code="cross.side_effect_duplicate_commit",
                        severity=Severity.BLOCKER,
                        summary="One external side effect was committed more than once.",
                        detail=f"{side_effect_id}: count={len(committed)}",
                    )
                )
        tool_starts: dict[str, list[EventProjection]] = defaultdict(list)
        tool_settles: dict[str, list[EventProjection]] = defaultdict(list)
        for event in projections:
            if not event.tool_call_id:
                continue
            if any(token in event.event_type for token in ("started", "requested", "dispatched")):
                tool_starts[event.tool_call_id].append(event)
            if any(token in event.event_type for token in ("completed", "failed", "denied", "settled")):
                tool_settles[event.tool_call_id].append(event)
        for call_id in sorted(set(tool_starts) | set(tool_settles)):
            if len(tool_starts.get(call_id, ())) != 1:
                result.add(
                    Finding(
                        code="cross.tool_start_cardinality",
                        severity=Severity.BLOCKER,
                        summary="Physical tool call must have exactly one start identity.",
                        detail=f"{call_id}: {len(tool_starts.get(call_id, ()))}",
                    )
                )
            if len(tool_settles.get(call_id, ())) != 1:
                result.add(
                    Finding(
                        code="cross.tool_settlement_cardinality",
                        severity=Severity.BLOCKER,
                        summary="Physical tool call must settle exactly once.",
                        detail=f"{call_id}: {len(tool_settles.get(call_id, ()))}",
                    )
                )

    def _scenario_semantic_findings(
        self,
        scenarios: Sequence[ScenarioEvidence],
        projections: Sequence[EventProjection],
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        by_run: dict[str, list[EventProjection]] = defaultdict(list)
        for event in projections:
            by_run[event.run_id].append(event)
        for scenario in scenarios:
            events = by_run.get(scenario.run_id, ())
            text = self._scenario_semantic_text(scenario, events)
            checks = self._checks_for_kind(scenario.kind)
            for check_id, tokens in checks:
                if not all(token in text for token in tokens):
                    result.add(
                        Finding(
                            code="cross.scenario_semantic_chain_missing",
                            severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                            summary="Scenario lacks a required cross-module semantic event chain.",
                            detail=f"{scenario.scenario_id}/{check_id}: {' + '.join(tokens)}",
                        )
                    )
            disconnect_probes = {
                str(item.get("probe_id") or "") for item in scenario.disconnect_evidence
            }
            if len(disconnect_probes) != len(scenario.disconnect_evidence):
                result.add(
                    Finding(
                        code="cross.disconnect_duplicate",
                        severity=Severity.BLOCKER,
                        summary="Scenario contains duplicate disconnect receipts.",
                        detail=scenario.scenario_id,
                    )
                )

    @staticmethod
    def _scenario_semantic_text(
        scenario: ScenarioEvidence,
        events: Sequence[EventProjection],
    ) -> str:
        values = [
            event.event_type
            + " "
            + json.dumps(event.payload, ensure_ascii=False, default=str).lower()
            for event in events
        ]
        values.extend(
            f"step {step.step_id} {step.path}"
            for step in scenario.steps
            if step.ok
        )
        values.extend(
            "assertion "
            + assertion.assertion_id
            + " "
            + str(assertion.observed).lower()
            + " "
            + assertion.source_location.lower()
            for assertion in scenario.assertions
            if assertion.passed
        )
        return "\n".join(values).lower()

    @staticmethod
    def _checks_for_kind(kind: ScenarioKind) -> tuple[tuple[str, tuple[str, ...]], ...]:
        return {
            ScenarioKind.QUERY_TOOL: (
                ("query-to-tool", ("query", "tool")),
                ("tool-to-artifact", ("tool", "artifact")),
            ),
            ScenarioKind.PERMISSION: (
                ("permission-to-tool", ("permission", "tool")),
                ("denial-to-recovery", ("deny", "recover")),
            ),
            ScenarioKind.MCP: (
                ("mcp-auth", ("mcp", "auth")),
                ("mcp-elicitation", ("mcp", "elicit")),
            ),
            ScenarioKind.SKILL_MEMORY: (
                ("skill-to-memory", ("skill", "memory")),
                ("compact-to-restore", ("compact", "restore")),
            ),
            ScenarioKind.SUBAGENT_RECOVERY: (
                ("subagent-to-lease", ("subagent", "lease")),
                ("failure-to-reroute", ("failure", "reroute")),
            ),
            ScenarioKind.STREAM_FAILOVER: (
                ("stream-to-retry", ("stream", "retry")),
                ("retry-to-failover", ("retry", "failover")),
            ),
        }[kind]

    @staticmethod
    def _cross_cutting_findings(
        scenarios: Sequence[ScenarioEvidence],
        supporting_gates: Sequence[GateResult],
        result: GateResult,
        *,
        final_completion: bool,
    ) -> None:
        gates = {gate.gate_id: gate for gate in supporting_gates}
        required_gates = {
            "patch-git": "safe-patch and dirty-worktree protection",
            "deny-policy": "progressive-friction denial recovery",
            "secrets-prompt-injection": "secret and prompt-injection protection",
            "code-index": "code-index context/patch/test/recovery effects",
        }
        support_status: dict[str, str] = {}
        for gate_id, capability in required_gates.items():
            gate = gates.get(gate_id)
            support_status[gate_id] = gate.status.value if gate is not None else "missing"
            if gate is None or not gate.ok or not gate.evidence:
                result.add(
                    Finding(
                        code="cross.cross_cutting_effect_missing",
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary="Integration evidence lacks a passed, traceable cross-cutting owner gate.",
                        detail=(
                            f"{gate_id}/{capability}: "
                            f"status={support_status[gate_id]}; "
                            f"evidence={len(gate.evidence) if gate is not None else 0}"
                        ),
                    )
                )
        code_index_receipts = [
            receipt
            for scenario in scenarios
            for receipt in scenario.disconnect_evidence
            if str(receipt.get("probe_id") or "") == "disable-code-index"
        ]
        material_code_index = any(
            str(receipt.get("status") or "").lower() == "passed"
            and receipt.get("expected_failure_observed") is True
            and receipt.get("material_difference") is True
            and receipt.get("fallback_masked") is not True
            and isinstance(receipt.get("difference"), Mapping)
            and receipt["difference"].get("semantic_change") is True
            for receipt in code_index_receipts
        )
        if not material_code_index:
            result.add(
                Finding(
                    code="cross.code_index_disable_difference_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Code-index evidence does not prove an enabled/disabled semantic difference.",
                )
            )
        result.metrics["cross_cutting_support_status"] = support_status
        result.metrics["code_index_disconnect_receipt_count"] = len(code_index_receipts)
        result.metrics["code_index_material_difference"] = material_code_index


class TopologyAdversarialGate:
    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        final_completion: bool,
        permutation_count: int = 32,
        random_seed: int = 7331,
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-topology-adversarial-integration",
            status=GateStatus.NOT_RUN,
            summary="Runtime topology mutation, branch isolation and deterministic replay integration.",
        )
        projections = [EventProjection.from_mapping(event, index) for index, event in enumerate(events)]
        topology = [
            item
            for item in projections
            if "topology" in item.event_type
            or any(token in item.event_type for token in ("node_added", "edge_added", "role_changed", "capability_changed"))
        ]
        external = [
            item
            for item in topology
            if item.payload.get("outside_precompiled_set") is True
            or item.payload.get("runtime_created") is True
        ]
        if not external:
            result.add(
                Finding(
                    code="topology.no_external_runtime_mutation",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="No runtime-created node/edge/role/capability outside a precompiled set was observed.",
                )
            )
        alias_findings = self._alias_findings(topology)
        result.findings.extend(alias_findings)
        deterministic, digests = self._permutation_replay(topology, permutation_count, random_seed)
        if not deterministic:
            result.add(
                Finding(
                    code="topology.permutation_nondeterministic",
                    severity=Severity.BLOCKER,
                    summary="Topology replay depends on parallel completion order.",
                    detail=f"distinct_digests={len(digests)}",
                )
            )
        pending = [item for item in projections if "pending" in item.event_type or item.payload.get("commit_state") == "pending"]
        committed = [item for item in projections if "committed" in item.event_type or item.payload.get("commit_state") == "committed"]
        if final_completion and (not pending or not committed):
            result.add(
                Finding(
                    code="topology.pending_committed_separation_missing",
                    severity=Severity.BLOCKER,
                    summary="Integration evidence does not separate pending and committed writes.",
                )
            )
        result.metrics.update(
            {
                "topology_event_count": len(topology),
                "external_mutation_count": len(external),
                "pending_count": len(pending),
                "committed_count": len(committed),
                "permutation_count": permutation_count,
                "permutation_digest_count": len(digests),
                "deterministic": deterministic,
            }
        )
        if topology:
            result.evidence.append(
                EvidencePointer(
                    kind="topology_event_chain",
                    location=f"{topology[0].run_id}/{topology[0].task_id}",
                    summary=(
                        f"topology_events={len(topology)}; "
                        f"external_mutations={len(external)}; permutations={permutation_count}"
                    ),
                    revision=str(max((item.revision for item in topology), default=0)),
                    causation_id=topology[0].causation_id,
                    metadata={
                        "deterministic": deterministic,
                        "permutation_digest_count": len(digests),
                    },
                )
            )
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _alias_findings(events: Sequence[EventProjection]) -> list[Finding]:
        findings: list[Finding] = []
        object_owners: dict[str, set[str]] = defaultdict(set)
        for event in events:
            branch = str(event.payload.get("branch_id") or "")
            object_ids = event.payload.get("mutable_object_ids") or ()
            for object_id in object_ids:
                if branch and str(object_id):
                    object_owners[str(object_id)].add(branch)
        for object_id, branches in object_owners.items():
            if len(branches) > 1:
                findings.append(
                    Finding(
                        code="topology.branch_alias_pollution",
                        severity=Severity.BLOCKER,
                        summary="Parallel branches share a mutable nested object identity.",
                        detail=f"{object_id}: {', '.join(sorted(branches))}",
                    )
                )
        return findings

    @staticmethod
    def _permutation_replay(
        events: Sequence[EventProjection],
        count: int,
        seed: int,
    ) -> tuple[bool, set[str]]:
        if not events:
            return False, set()
        grouped: dict[int, list[EventProjection]] = defaultdict(list)
        for event in events:
            grouped[event.sequence].append(event)
        rng = random.Random(seed)
        digests: set[str] = set()
        for _ in range(max(1, count)):
            state: dict[str, str] = {}
            for sequence in sorted(grouped):
                batch = list(grouped[sequence])
                rng.shuffle(batch)
                proposals: dict[str, list[str]] = defaultdict(list)
                for event in batch:
                    key = str(event.payload.get("semantic_key") or event.payload.get("node_id") or event.event_id)
                    proposals[key].append(event.semantic_digest)
                for key, values in sorted(proposals.items()):
                    unique = sorted(set(values))
                    state[key] = stable_digest(unique) if len(unique) > 1 else unique[0]
            digests.add(stable_digest(state))
        return len(digests) == 1, digests


def _int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
