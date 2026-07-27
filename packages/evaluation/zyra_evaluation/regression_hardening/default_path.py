from __future__ import annotations

import copy
import re
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    stable_digest,
)


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized or "unknown"


class EntryKind(StrEnum):
    CLI = "cli"
    API = "api"
    WEB = "web"
    WORKER = "worker"


class ProbePhase(StrEnum):
    CREATED = "created"
    STARTED = "started"
    OWNER_REACHED = "owner_reached"
    EFFECT_COMMITTED = "effect_committed"
    COMPLETED = "completed"
    FAILED = "failed"
    DISABLED = "disabled"
    MUTATED = "mutated"


@dataclass(frozen=True, slots=True)
class PathStep:
    step_id: str
    entry_kind: EntryKind
    phase: ProbePhase
    owner_id: str
    sequence: int
    run_id: str
    causation_id: str = ""
    state_before_digest: str = ""
    state_after_digest: str = ""
    status: str = ""
    attributes: Mapping[str, Any] = field(default_factory=dict)

    @property
    def mutated_state(self) -> bool:
        return bool(
            self.state_after_digest
            and self.state_after_digest != self.state_before_digest
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "entry_kind": self.entry_kind.value,
            "phase": self.phase.value,
            "owner_id": self.owner_id,
            "sequence": self.sequence,
            "run_id": self.run_id,
            "causation_id": self.causation_id,
            "state_before_digest": self.state_before_digest,
            "state_after_digest": self.state_after_digest,
            "mutated_state": self.mutated_state,
            "status": self.status,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True, slots=True)
class DisableOutcome:
    entry_kind: EntryKind
    owner_id: str
    baseline_succeeded: bool
    disabled_succeeded: bool
    explicit_error_code: str
    fallback_owner_id: str = ""
    state_changed_while_disabled: bool = False

    @property
    def valid(self) -> bool:
        return (
            self.baseline_succeeded
            and not self.disabled_succeeded
            and bool(self.explicit_error_code)
            and not self.fallback_owner_id
            and not self.state_changed_while_disabled
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_kind": self.entry_kind.value,
            "owner_id": self.owner_id,
            "baseline_succeeded": self.baseline_succeeded,
            "disabled_succeeded": self.disabled_succeeded,
            "explicit_error_code": self.explicit_error_code,
            "fallback_owner_id": self.fallback_owner_id,
            "state_changed_while_disabled": self.state_changed_while_disabled,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class MutationOutcome:
    mutation_id: str
    entry_kind: EntryKind
    mutation_kind: str
    baseline_digest: str
    candidate_digest: str
    expected_change: str
    observed_change: str
    admitted: bool
    reason: str

    @property
    def valid(self) -> bool:
        changed = self.baseline_digest != self.candidate_digest
        if self.expected_change == "reject":
            return changed and not self.admitted
        if self.expected_change == "behavior":
            return changed and self.admitted and bool(self.observed_change)
        if self.expected_change == "none":
            return not changed
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "mutation_id": self.mutation_id,
            "entry_kind": self.entry_kind.value,
            "mutation_kind": self.mutation_kind,
            "baseline_digest": self.baseline_digest,
            "candidate_digest": self.candidate_digest,
            "expected_change": self.expected_change,
            "observed_change": self.observed_change,
            "admitted": self.admitted,
            "reason": self.reason,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class EntryTrace:
    entry_kind: EntryKind
    entry_id: str
    run_id: str
    generated_input_digest: str
    steps: tuple[PathStep, ...]
    selected_owner_ids: tuple[str, ...]
    completed: bool
    error_code: str = ""
    output_digest: str = ""

    @property
    def reached_owner(self) -> bool:
        return bool(self.selected_owner_ids) and any(
            item.phase is ProbePhase.OWNER_REACHED
            and item.owner_id in self.selected_owner_ids
            for item in self.steps
        )

    @property
    def committed_effect(self) -> bool:
        return any(
            item.phase is ProbePhase.EFFECT_COMMITTED
            and (item.mutated_state or item.attributes.get("effect_digest"))
            for item in self.steps
        )

    @property
    def valid(self) -> bool:
        if not self.completed or self.error_code:
            return False
        if not self.reached_owner or not self.committed_effect:
            return False
        sequences = [item.sequence for item in self.steps]
        if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
            return False
        return all(item.run_id == self.run_id for item in self.steps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_kind": self.entry_kind.value,
            "entry_id": self.entry_id,
            "run_id": self.run_id,
            "generated_input_digest": self.generated_input_digest,
            "steps": [item.to_dict() for item in self.steps],
            "selected_owner_ids": list(self.selected_owner_ids),
            "completed": self.completed,
            "error_code": self.error_code,
            "output_digest": self.output_digest,
            "reached_owner": self.reached_owner,
            "committed_effect": self.committed_effect,
            "valid": self.valid,
        }


@dataclass(frozen=True, slots=True)
class DefaultPathReport:
    traces: tuple[EntryTrace, ...]
    disable_outcomes: tuple[DisableOutcome, ...]
    mutation_outcomes: tuple[MutationOutcome, ...]
    missing_entries: tuple[str, ...]
    duplicate_entries: tuple[str, ...]
    invalid_traces: tuple[str, ...]
    invalid_disables: tuple[str, ...]
    invalid_mutations: tuple[str, ...]
    owner_counts: Mapping[str, int]
    digest: str

    @property
    def valid(self) -> bool:
        return not (
            self.missing_entries
            or self.duplicate_entries
            or self.invalid_traces
            or self.invalid_disables
            or self.invalid_mutations
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-default-path-reachability/v1",
            "valid": self.valid,
            "traces": [item.to_dict() for item in self.traces],
            "disable_outcomes": [item.to_dict() for item in self.disable_outcomes],
            "mutation_outcomes": [item.to_dict() for item in self.mutation_outcomes],
            "missing_entries": list(self.missing_entries),
            "duplicate_entries": list(self.duplicate_entries),
            "invalid_traces": list(self.invalid_traces),
            "invalid_disables": list(self.invalid_disables),
            "invalid_mutations": list(self.invalid_mutations),
            "owner_counts": dict(sorted(self.owner_counts.items())),
            "digest": self.digest,
        }


class EntrypointProbe(Protocol):
    entry_kind: EntryKind
    entry_id: str

    def execute(self, generated_input: Mapping[str, Any]) -> EntryTrace:
        ...

    def disable_owner(
        self,
        owner_id: str,
        generated_input: Mapping[str, Any],
    ) -> DisableOutcome:
        ...

    def mutate(
        self,
        mutation_id: str,
        generated_input: Mapping[str, Any],
    ) -> MutationOutcome:
        ...


class DefaultPathAuditor:
    def __init__(
        self,
        *,
        required_entries: Sequence[EntryKind] = tuple(EntryKind),
    ) -> None:
        self.required_entries = tuple(required_entries)

    def audit(
        self,
        traces: Sequence[EntryTrace],
        disable_outcomes: Sequence[DisableOutcome],
        mutation_outcomes: Sequence[MutationOutcome],
    ) -> DefaultPathReport:
        kinds = Counter(item.entry_kind.value for item in traces)
        missing = tuple(
            item.value
            for item in self.required_entries
            if kinds[item.value] == 0
        )
        duplicate = tuple(
            kind
            for kind, count in sorted(kinds.items())
            if count > 1
        )
        invalid_traces = tuple(
            item.entry_id
            for item in traces
            if not item.valid
        )
        invalid_disables = tuple(
            f"{item.entry_kind.value}:{item.owner_id}"
            for item in disable_outcomes
            if not item.valid
        )
        invalid_mutations = tuple(
            item.mutation_id
            for item in mutation_outcomes
            if not item.valid
        )
        owner_counts = Counter(
            owner
            for trace in traces
            for owner in trace.selected_owner_ids
        )
        disable_coverage = {
            (item.entry_kind, item.owner_id)
            for item in disable_outcomes
        }
        for trace in traces:
            for owner in trace.selected_owner_ids:
                if (trace.entry_kind, owner) not in disable_coverage:
                    invalid_disables += (
                        f"{trace.entry_kind.value}:{owner}:missing",
                    )
        mutation_coverage = {item.entry_kind for item in mutation_outcomes}
        for kind in self.required_entries:
            if kind not in mutation_coverage:
                invalid_mutations += (f"{kind.value}:missing",)
        material = {
            "traces": [item.to_dict() for item in traces],
            "disable": [item.to_dict() for item in disable_outcomes],
            "mutation": [item.to_dict() for item in mutation_outcomes],
            "missing": missing,
            "duplicate": duplicate,
            "invalid_traces": invalid_traces,
            "invalid_disables": invalid_disables,
            "invalid_mutations": invalid_mutations,
        }
        return DefaultPathReport(
            traces=tuple(traces),
            disable_outcomes=tuple(disable_outcomes),
            mutation_outcomes=tuple(mutation_outcomes),
            missing_entries=missing,
            duplicate_entries=duplicate,
            invalid_traces=invalid_traces,
            invalid_disables=tuple(sorted(set(invalid_disables))),
            invalid_mutations=tuple(sorted(set(invalid_mutations))),
            owner_counts=dict(owner_counts),
            digest=stable_digest(material),
        )


class CallbackEntrypointProbe:
    """Adapts a real entrypoint callback without supplying fallback behavior."""

    def __init__(
        self,
        entry_kind: EntryKind,
        entry_id: str,
        executor: Callable[[Mapping[str, Any], str, str], Mapping[str, Any]],
        *,
        selected_owner_ids: Sequence[str],
    ) -> None:
        self.entry_kind = entry_kind
        self.entry_id = entry_id
        self.executor = executor
        self.selected_owner_ids = tuple(selected_owner_ids)
        if not self.selected_owner_ids:
            raise ValueError("callback entrypoint probe requires selected owners")

    def execute(self, generated_input: Mapping[str, Any]) -> EntryTrace:
        result = self.executor(generated_input, "baseline", "")
        return self._trace(result, generated_input)

    def disable_owner(
        self,
        owner_id: str,
        generated_input: Mapping[str, Any],
    ) -> DisableOutcome:
        if owner_id not in self.selected_owner_ids:
            raise ValueError(f"{owner_id} is not selected by {self.entry_id}")
        baseline = self.executor(generated_input, "baseline", "")
        disabled = self.executor(generated_input, "disable", owner_id)
        return DisableOutcome(
            entry_kind=self.entry_kind,
            owner_id=owner_id,
            baseline_succeeded=bool(baseline.get("completed")),
            disabled_succeeded=bool(disabled.get("completed")),
            explicit_error_code=str(disabled.get("error_code") or ""),
            fallback_owner_id=str(disabled.get("fallback_owner_id") or ""),
            state_changed_while_disabled=bool(
                disabled.get("state_changed")
                or disabled.get("committed_effect")
            ),
        )

    def mutate(
        self,
        mutation_id: str,
        generated_input: Mapping[str, Any],
    ) -> MutationOutcome:
        baseline = self.executor(generated_input, "baseline", "")
        candidate = self.executor(generated_input, "mutation", mutation_id)
        baseline_digest = str(
            baseline.get("output_digest")
            or stable_digest(baseline)
        )
        candidate_digest = str(
            candidate.get("output_digest")
            or stable_digest(candidate)
        )
        expected = str(
            candidate.get("expected_change")
            or generated_input.get("expected_change")
            or "reject"
        )
        return MutationOutcome(
            mutation_id=mutation_id,
            entry_kind=self.entry_kind,
            mutation_kind=str(candidate.get("mutation_kind") or mutation_id),
            baseline_digest=baseline_digest,
            candidate_digest=candidate_digest,
            expected_change=expected,
            observed_change=str(candidate.get("observed_change") or ""),
            admitted=bool(candidate.get("completed") or candidate.get("admitted")),
            reason=str(candidate.get("error_code") or candidate.get("reason") or ""),
        )

    def _trace(
        self,
        result: Mapping[str, Any],
        generated_input: Mapping[str, Any],
    ) -> EntryTrace:
        raw_steps = result.get("steps")
        steps: list[PathStep] = []
        if isinstance(raw_steps, Sequence) and not isinstance(raw_steps, (str, bytes)):
            for index, raw in enumerate(raw_steps):
                if not isinstance(raw, Mapping):
                    continue
                steps.append(
                    PathStep(
                        step_id=str(raw.get("step_id") or f"{self.entry_id}-{index + 1}"),
                        entry_kind=self.entry_kind,
                        phase=ProbePhase(str(raw.get("phase") or "started")),
                        owner_id=str(raw.get("owner_id") or ""),
                        sequence=int(raw.get("sequence") or index + 1),
                        run_id=str(raw.get("run_id") or result.get("run_id") or ""),
                        causation_id=str(raw.get("causation_id") or ""),
                        state_before_digest=str(raw.get("state_before_digest") or ""),
                        state_after_digest=str(raw.get("state_after_digest") or ""),
                        status=str(raw.get("status") or ""),
                        attributes=(
                            dict(raw.get("attributes"))
                            if isinstance(raw.get("attributes"), Mapping)
                            else {}
                        ),
                    )
                )
        return EntryTrace(
            entry_kind=self.entry_kind,
            entry_id=self.entry_id,
            run_id=str(result.get("run_id") or ""),
            generated_input_digest=stable_digest(generated_input),
            steps=tuple(steps),
            selected_owner_ids=self.selected_owner_ids,
            completed=bool(result.get("completed")),
            error_code=str(result.get("error_code") or ""),
            output_digest=str(result.get("output_digest") or stable_digest(result)),
        )


class DefaultPathCampaign:
    def __init__(self, probes: Iterable[EntrypointProbe]) -> None:
        self.probes = tuple(probes)
        kinds = [item.entry_kind for item in self.probes]
        if len(set(kinds)) != len(kinds):
            raise ValueError("default-path campaign requires one probe per entry kind")

    def execute(
        self,
        generated_inputs: Mapping[EntryKind | str, Mapping[str, Any]],
    ) -> tuple[DefaultPathReport, CaseExecutionBuffer]:
        traces: list[EntryTrace] = []
        disables: list[DisableOutcome] = []
        mutations: list[MutationOutcome] = []
        buffer = CaseExecutionBuffer()
        for probe in self.probes:
            value = generated_inputs.get(probe.entry_kind)
            if value is None:
                value = generated_inputs.get(probe.entry_kind.value, {})
            started = time.monotonic()
            trace = probe.execute(value)
            traces.append(trace)
            observation = buffer.observe(
                f"default-path.{probe.entry_kind.value}",
                ObservationKind.ENTRYPOINT,
                probe.entry_id,
                "completed" if trace.valid else "failed",
                started=started,
                run_id=trace.run_id,
                attributes={
                    "trace_digest": stable_digest(trace.to_dict()),
                    "selected_owner_ids": list(trace.selected_owner_ids),
                    "committed_effect": trace.committed_effect,
                },
            )
            buffer.assert_that(
                f"default-path.{probe.entry_kind.value}-valid",
                trace.valid,
                f"{probe.entry_kind.value} entry must reach selected owner and commit an effect",
                evidence=(observation.observation_id,),
            )
            for owner_id in trace.selected_owner_ids:
                outcome = probe.disable_owner(owner_id, value)
                disables.append(outcome)
                owner_slug = _slug(owner_id)
                disabled_observation = buffer.observe(
                    f"default-path.{probe.entry_kind.value}-disable-{owner_slug}",
                    ObservationKind.OWNER,
                    owner_id,
                    "owner-disabled" if outcome.valid else "disable-invalid",
                    attributes=outcome.to_dict(),
                )
                buffer.assert_that(
                    f"default-path.{probe.entry_kind.value}-disable-{owner_slug}-valid",
                    outcome.valid,
                    f"disabling {owner_id} must fail explicitly without fallback",
                    evidence=(disabled_observation.observation_id,),
                    failure_kind=FailureKind.CONTRACT,
                )
            mutation_ids = value.get("mutation_ids")
            if not isinstance(mutation_ids, Sequence) or isinstance(mutation_ids, (str, bytes)):
                mutation_ids = ("forged-owner-receipt",)
            for raw_mutation_id in mutation_ids:
                mutation_id = str(raw_mutation_id)
                mutation_slug = _slug(mutation_id)
                outcome = probe.mutate(mutation_id, value)
                mutations.append(outcome)
                mutation_observation = buffer.observe(
                    f"default-path.{probe.entry_kind.value}-mutation-{mutation_slug}",
                    ObservationKind.MUTATION,
                    f"{probe.entry_id}:{mutation_id}",
                    "mutation-rejected" if not outcome.admitted else "mutation-admitted",
                    attributes=outcome.to_dict(),
                )
                buffer.assert_that(
                    f"default-path.{probe.entry_kind.value}-mutation-{mutation_slug}-valid",
                    outcome.valid,
                    f"{mutation_id} must have the declared semantic effect",
                    evidence=(mutation_observation.observation_id,),
                    failure_kind=FailureKind.CONTRACT,
                )
        report = DefaultPathAuditor().audit(traces, disables, mutations)
        summary = buffer.observe(
            "default-path.summary",
            ObservationKind.MUTATION,
            "cli-api-web-worker-default-path",
            "mutation-verified" if report.valid else "mutation-invalid",
            attributes={
                "report_digest": report.digest,
                "trace_count": len(report.traces),
                "disable_count": len(report.disable_outcomes),
                "mutation_count": len(report.mutation_outcomes),
            },
        )
        buffer.assert_that(
            "default-path.report-valid",
            report.valid,
            "all default entries, owner disables and mutations must pass",
            severity=AssertionSeverity.BLOCKER,
            evidence=(summary.observation_id,),
        )
        return report, buffer


def synthetic_trace_from_owner_events(
    *,
    entry_kind: EntryKind,
    entry_id: str,
    run_id: str,
    selected_owner_ids: Sequence[str],
    events: Sequence[Mapping[str, Any]],
    generated_input: Mapping[str, Any],
    completed: bool,
    error_code: str = "",
) -> EntryTrace:
    """Normalize real owner events; it does not invent a missing mutation."""

    steps: list[PathStep] = []
    prior_id = ""
    for index, event in enumerate(events):
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            payload = {}
        text = f"{event.get('event_type', '')} {payload}".casefold()
        owner_id = str(
            payload.get("state_owner")
            or payload.get("owner_id")
            or payload.get("selected_worker")
            or ""
        )
        if index == 0:
            phase = ProbePhase.STARTED
        elif owner_id and owner_id in selected_owner_ids:
            phase = ProbePhase.OWNER_REACHED
        elif (
            payload.get("state_after_digest")
            or payload.get("artifact_id")
            or payload.get("mutation_id")
            or payload.get("selected_worker")
            or "completed" in text
        ):
            phase = ProbePhase.EFFECT_COMMITTED
        else:
            phase = ProbePhase.STARTED
        step_id = str(
            event.get("event_id")
            or payload.get("event_id")
            or f"{entry_id}-event-{index + 1}"
        )
        steps.append(
            PathStep(
                step_id=step_id,
                entry_kind=entry_kind,
                phase=phase,
                owner_id=owner_id,
                sequence=int(event.get("sequence") or event.get("seq") or index + 1),
                run_id=str(event.get("run_id") or run_id),
                causation_id=str(event.get("causation_id") or prior_id),
                state_before_digest=str(payload.get("state_before_digest") or ""),
                state_after_digest=str(payload.get("state_after_digest") or ""),
                status=str(payload.get("status") or event.get("status") or ""),
                attributes={
                    "event_type": str(event.get("event_type") or ""),
                    "effect_digest": str(
                        payload.get("effect_digest")
                        or payload.get("artifact_id")
                        or payload.get("mutation_id")
                        or payload.get("selected_worker")
                        or ""
                    ),
                },
            )
        )
        prior_id = step_id
    return EntryTrace(
        entry_kind=entry_kind,
        entry_id=entry_id,
        run_id=run_id,
        generated_input_digest=stable_digest(generated_input),
        steps=tuple(steps),
        selected_owner_ids=tuple(selected_owner_ids),
        completed=completed,
        error_code=error_code,
        output_digest=stable_digest(events),
    )


__all__ = [
    "CallbackEntrypointProbe",
    "DefaultPathAuditor",
    "DefaultPathCampaign",
    "DefaultPathReport",
    "DisableOutcome",
    "EntryKind",
    "EntryTrace",
    "EntrypointProbe",
    "MutationOutcome",
    "PathStep",
    "ProbePhase",
    "synthetic_trace_from_owner_events",
]
