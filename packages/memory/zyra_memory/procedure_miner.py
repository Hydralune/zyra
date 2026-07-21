from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from zyra_core import now_iso

from .curator_integration_models import (
    CuratorConsumer,
    CuratorOutcome,
    CuratorOutcomeKind,
    CuratorOutcomeState,
)
from .models import MemoryLayer, MemoryRecord
from .procedure_models import (
    PROCEDURE_MINING_PROTOCOL,
    ProcedureApplicability,
    ProcedureConsumer,
    ProcedureContractError,
    ProcedureEvidenceKind,
    ProcedureEvidenceRef,
    ProcedureMiningDisposition,
    ProcedureMiningError,
    ProcedureMiningReceipt,
    ProcedureProvenance,
    ProcedureSignal,
    ProcedureSignalKind,
    ProcedureStep,
    ProcedureStepState,
    ProcedureValidationStatus,
    ReusableProcedure,
    mapping,
    mapping_sequence,
    stable_digest,
    stable_id,
    unique_strings,
)
from .procedure_store import ReusableProcedureStore


@dataclass(frozen=True, slots=True)
class ProcedureMinerPolicy:
    minimum_tool_steps: int = 1
    maximum_tool_steps: int = 64
    minimum_confidence: float = 0.65
    validation_confidence: float = 0.8
    require_artifact_for_validation: bool = False
    require_skill_version_for_validation: bool = True
    require_trusted_runtime_event: bool = True
    require_successful_tool_results: bool = True
    allow_failure_recovery_steps: bool = True
    maximum_goal_patterns: int = 16
    maximum_summary_characters: int = 8_000

    def validated(self) -> ProcedureMinerPolicy:
        if self.minimum_tool_steps < 1:
            raise ProcedureContractError(
                "procedure_policy_minimum_steps",
                "minimum tool steps must be positive",
            )
        if self.maximum_tool_steps < self.minimum_tool_steps:
            raise ProcedureContractError(
                "procedure_policy_maximum_steps",
                "maximum tool steps must not be lower than minimum tool steps",
            )
        if not 0.0 <= self.minimum_confidence <= 1.0:
            raise ProcedureContractError(
                "procedure_policy_minimum_confidence",
                "minimum confidence must be between zero and one",
            )
        if not self.minimum_confidence <= self.validation_confidence <= 1.0:
            raise ProcedureContractError(
                "procedure_policy_validation_confidence",
                "validation confidence must be at least minimum confidence",
            )
        return self


@dataclass(frozen=True, slots=True)
class ProcedureMiningContext:
    outcome: CuratorOutcome
    memory: MemoryRecord
    events: tuple[Mapping[str, Any], ...]
    evidence: tuple[ProcedureEvidenceRef, ...]
    skill_versions: tuple[Mapping[str, Any], ...]
    session_ids: tuple[str, ...]
    tool_call_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    memory_event_ids: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROCEDURE_MINING_PROTOCOL,
            "outcome_id": self.outcome.outcome_id,
            "memory_id": self.memory.memory_id,
            "event_count": len(self.events),
            "evidence_count": len(self.evidence),
            "skill_version_count": len(self.skill_versions),
            "session_ids": list(self.session_ids),
            "tool_call_ids": list(self.tool_call_ids),
            "artifact_ids": list(self.artifact_ids),
            "memory_event_ids": list(self.memory_event_ids),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class ProcedureMiningResult:
    outcome_id: str
    disposition: ProcedureMiningDisposition
    receipt: ProcedureMiningReceipt
    procedure: ReusableProcedure | None
    signal: ProcedureSignal | None
    context: ProcedureMiningContext | None
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.disposition in {
            ProcedureMiningDisposition.CREATED,
            ProcedureMiningDisposition.UPDATED,
            ProcedureMiningDisposition.REPLAYED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROCEDURE_MINING_PROTOCOL,
            "ok": self.ok,
            "outcome_id": self.outcome_id,
            "disposition": self.disposition.value,
            "receipt": self.receipt.to_dict(),
            "procedure": self.procedure.to_dict() if self.procedure else None,
            "signal": self.signal.to_dict() if self.signal else None,
            "context": self.context.to_dict() if self.context else None,
            "warnings": list(self.warnings),
            "source_owner": "06B CuratorIntegrationStore",
            "procedure_owner": "ReusableProcedureStore",
            "skill_version_owner": "03C SkillCoordinator",
            "static_document_activated": False,
            "model_summary_activated": False,
        }


class ReusableProcedureMiner:
    """Mine deterministic procedures from published 06B skill outcomes only.

    Hermes' procedure-memory idea is retained, but inputs and activation are
    replaced with Zyra's curator outcome, canonical memory and runtime-event
    provenance.  The miner never reads SKILL.md, never invokes an LLM, and
    never changes the 03C skill catalog or allowed-tool policy.
    """

    def __init__(
        self,
        *,
        canonical_store: Any,
        procedure_store: ReusableProcedureStore,
        policy: ProcedureMinerPolicy | None = None,
        event_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self.canonical_store = canonical_store
        self.procedure_store = procedure_store
        self.policy = (policy or ProcedureMinerPolicy()).validated()
        self.event_sink = event_sink

    def mine(self, outcome: CuratorOutcome) -> ProcedureMiningResult:
        if _disabled():
            raise ProcedureMiningError(
                "procedure_miner_disabled",
                "06C reusable procedure miner is disabled",
            )
        try:
            self._validate_outcome(outcome)
            existing = self.procedure_store.by_outcome(outcome.outcome_id)
            if existing is not None:
                receipt = ProcedureMiningReceipt.build(
                    outcome_id=outcome.outcome_id,
                    task_id=outcome.task_id,
                    disposition=ProcedureMiningDisposition.REPLAYED,
                    procedure_id=existing.procedure_id,
                    reason="curator_outcome_already_mined",
                    evidence_digest=outcome.evidence_digest,
                    state_before=existing.state.value,
                    state_after=existing.state.value,
                    metadata={
                        "procedure_digest": existing.procedure_digest,
                        "idempotent": True,
                    },
                )
                self.procedure_store.put_receipt(receipt)
                return ProcedureMiningResult(
                    outcome_id=outcome.outcome_id,
                    disposition=ProcedureMiningDisposition.REPLAYED,
                    receipt=receipt,
                    procedure=existing,
                    signal=None,
                    context=None,
                )
            context = self._build_context(outcome)
            steps = self._derive_steps(context)
            if len(steps) < self.policy.minimum_tool_steps:
                return self._reject(
                    outcome,
                    "insufficient_successful_tool_steps",
                    context=context,
                    details={"step_count": len(steps)},
                )
            applicability = self._derive_applicability(context, steps)
            confidence, factors = self._score(context, steps)
            if confidence < self.policy.minimum_confidence:
                return self._reject(
                    outcome,
                    "procedure_confidence_below_minimum",
                    context=context,
                    details={"confidence": confidence, "factors": factors},
                )
            validation = self._validation_status(context, steps, confidence)
            procedure = ReusableProcedure.build(
                name=self._name(context, steps),
                summary=self._summary(context, steps),
                state=validation,
                steps=steps,
                applicability=applicability,
                provenance=self._provenance(context),
                evidence=context.evidence,
                consumers=(
                    ProcedureConsumer.AUDIT,
                    ProcedureConsumer.CONTEXT,
                    ProcedureConsumer.ROUTING,
                    ProcedureConsumer.RECOVERY,
                ),
                confidence=confidence,
                success_count=1,
                failure_count=0,
                validated_at=(now_iso() if validation is ProcedureValidationStatus.VALIDATED else ""),
                metadata={
                    "source": "hermes-procedure-memory-adapted",
                    "source_outcome_kind": outcome.kind.value,
                    "source_outcome_state": outcome.state.value,
                    "deterministic_validation": outcome.deterministic_validation,
                    "canonical_memory_changed": outcome.canonical_memory_changed,
                    "confidence_factors": factors,
                    "warnings": list(context.warnings),
                    "model_generated_procedure": False,
                    "static_skill_document_source": False,
                    "fixture_source": False,
                    "procedure_is_skill_invocation_cache": False,
                    "03c_resolution_required_before_skill_reuse": True,
                },
            )
            stored, created = self.procedure_store.put(procedure)
            disposition = (
                ProcedureMiningDisposition.CREATED
                if created
                else ProcedureMiningDisposition.REPLAYED
            )
            receipt = ProcedureMiningReceipt.build(
                outcome_id=outcome.outcome_id,
                task_id=outcome.task_id,
                disposition=disposition,
                procedure_id=stored.procedure_id,
                reason=(
                    "validated_successful_trajectory_mined"
                    if stored.state is ProcedureValidationStatus.VALIDATED
                    else "successful_trajectory_candidate_mined"
                ),
                evidence_digest=outcome.evidence_digest,
                state_before="",
                state_after=stored.state.value,
                metadata={
                    "procedure_digest": stored.procedure_digest,
                    "confidence": stored.confidence,
                    "step_count": len(stored.steps),
                    "created": created,
                },
            )
            self.procedure_store.put_receipt(receipt)
            signal = self._signal(outcome, stored)
            self.procedure_store.put_signal(signal)
            self._emit(signal)
            return ProcedureMiningResult(
                outcome_id=outcome.outcome_id,
                disposition=disposition,
                receipt=receipt,
                procedure=stored,
                signal=signal,
                context=context,
                warnings=context.warnings,
            )
        except ProcedureContractError as error:
            return self._reject(
                outcome,
                error.code,
                details={"message": str(error), **error.details},
            )

    def mine_many(
        self,
        outcomes: Sequence[CuratorOutcome],
    ) -> tuple[ProcedureMiningResult, ...]:
        ordered = sorted(
            outcomes,
            key=lambda item: (
                item.created_at,
                item.curator_job_id,
                item.outcome_id,
            ),
        )
        results: list[ProcedureMiningResult] = []
        seen: set[str] = set()
        for outcome in ordered:
            if outcome.outcome_id in seen:
                continue
            seen.add(outcome.outcome_id)
            results.append(self.mine(outcome))
        return tuple(results)

    def _validate_outcome(self, outcome: CuratorOutcome) -> None:
        outcome.validated()
        if outcome.kind is not CuratorOutcomeKind.SKILL_CANDIDATE:
            raise ProcedureContractError(
                "procedure_outcome_kind_rejected",
                "procedure miner only accepts 06B skill_candidate outcomes",
            )
        if outcome.state is not CuratorOutcomeState.PUBLISHED:
            raise ProcedureContractError(
                "procedure_outcome_not_published",
                "procedure miner only accepts published 06B outcomes",
            )
        if not outcome.deterministic_validation:
            raise ProcedureContractError(
                "procedure_outcome_not_deterministic",
                "procedure miner requires deterministic curator validation",
            )
        if not outcome.canonical_memory_changed:
            raise ProcedureContractError(
                "procedure_outcome_not_canonical",
                "procedure miner requires a committed canonical memory outcome",
            )
        if CuratorConsumer.SKILL_MEMORY not in outcome.target_consumers:
            raise ProcedureContractError(
                "procedure_outcome_consumer_missing",
                "procedure miner requires the 06B skill-memory consumer projection",
            )
        if not outcome.memory_id or outcome.memory_revision < 1:
            raise ProcedureContractError(
                "procedure_outcome_memory_provenance",
                "procedure miner requires canonical memory id and revision",
            )
        if not outcome.decision_id or not outcome.commit_receipt_id:
            raise ProcedureContractError(
                "procedure_outcome_commit_provenance",
                "procedure miner requires curator decision and commit receipt ids",
            )
        if outcome.model_assisted and not outcome.deterministic_validation:
            raise ProcedureContractError(
                "procedure_model_activation_forbidden",
                "model-assisted output cannot activate a procedure without deterministic validation",
            )

    def _build_context(self, outcome: CuratorOutcome) -> ProcedureMiningContext:
        memories = tuple(self.canonical_store.task_memory_records(outcome.task_id))
        memory = next(
            (item for item in memories if item.memory_id == outcome.memory_id),
            None,
        )
        if memory is None:
            raise ProcedureContractError(
                "procedure_canonical_memory_missing",
                f"canonical memory {outcome.memory_id} is missing",
            )
        if memory.layer is not MemoryLayer.SKILL:
            raise ProcedureContractError(
                "procedure_canonical_memory_layer",
                "procedure miner requires canonical skill-layer memory",
            )
        if memory.task_id != outcome.task_id or memory.run_id != outcome.run_id:
            raise ProcedureContractError(
                "procedure_memory_identity_mismatch",
                "canonical memory does not match curator outcome run/task",
            )
        events = tuple(
            dict(item)
            for item in self.canonical_store.task_events(outcome.task_id)
        )
        relevant = self._relevant_events(outcome, memory, events)
        skill_versions = self._skill_versions(outcome, memory, relevant)
        evidence = self._evidence(outcome, memory, relevant, skill_versions)
        tool_call_ids = unique_strings(
            item.tool_call_id
            for item in evidence
            if item.tool_call_id
        )
        artifact_ids = unique_strings(
            [*memory.artifact_ids, *(
                item.artifact_id for item in evidence if item.artifact_id
            )]
        )
        session_ids = unique_strings(
            _session_id(event) for event in relevant if _session_id(event)
        )
        memory_event_ids = unique_strings(
            [
                outcome.commit_receipt_id,
                *memory.evidence_ids,
                *(
                    item.event_id for item in evidence
                    if item.event_id and item.kind in {
                        ProcedureEvidenceKind.MEMORY,
                        ProcedureEvidenceKind.EVENT,
                    }
                ),
            ]
        )
        warnings: list[str] = []
        if not session_ids:
            warnings.append("runtime_session_id_not_present")
        if not artifact_ids:
            warnings.append("trajectory_has_no_artifact")
        if not skill_versions:
            warnings.append("03c_skill_version_provenance_not_present")
        return ProcedureMiningContext(
            outcome=outcome,
            memory=memory,
            events=relevant,
            evidence=evidence,
            skill_versions=skill_versions,
            session_ids=session_ids,
            tool_call_ids=tool_call_ids,
            artifact_ids=artifact_ids,
            memory_event_ids=memory_event_ids,
            warnings=tuple(warnings),
        )

    def _relevant_events(
        self,
        outcome: CuratorOutcome,
        memory: MemoryRecord,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        evidence_ids = set(memory.evidence_ids)
        artifact_ids = set(memory.artifact_ids)
        selected: list[Mapping[str, Any]] = []
        for event in events:
            payload = mapping(event.get("payload"))
            event_id = str(event.get("event_id") or "")
            event_type = str(event.get("event_type") or "")
            flattened = _flatten(payload)
            ids = set(_strings_at_keys(payload, {
                "tool_call_id",
                "artifact_id",
                "event_id",
                "invocation_id",
                "memory_id",
                "candidate_id",
                "decision_id",
            }))
            causal = bool(
                event_id in evidence_ids
                or ids & evidence_ids
                or ids & artifact_ids
                or outcome.candidate_id in ids
                or outcome.decision_id in ids
                or outcome.memory_id in ids
                or outcome.commit_receipt_id in ids
            )
            semantic = any(
                token in event_type.casefold()
                for token in (
                    "tool",
                    "artifact",
                    "skill",
                    "verification",
                    "completed",
                    "memory",
                )
            )
            summary_match = any(
                token and token.casefold() in flattened.casefold()
                for token in (memory.source_id, memory.summary[:80], outcome.subject[:80])
            )
            if causal or (semantic and summary_match):
                selected.append(dict(event))
        return tuple(
            sorted(
                selected,
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    str(item.get("event_id") or ""),
                ),
            )
        )

    def _skill_versions(
        self,
        outcome: CuratorOutcome,
        memory: MemoryRecord,
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], ...]:
        candidates: list[Mapping[str, Any]] = []
        for source in (
            memory.content,
            memory.metadata,
            outcome.payload,
            outcome.metadata,
        ):
            candidates.extend(_version_candidates(source))
        for event in events:
            candidates.extend(_version_candidates(mapping(event.get("payload"))))
        output: list[Mapping[str, Any]] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = _normalize_skill_version(candidate)
            if normalized is None:
                continue
            identity = stable_digest(normalized)
            if identity in seen:
                continue
            seen.add(identity)
            output.append(normalized)
        return tuple(output)

    def _evidence(
        self,
        outcome: CuratorOutcome,
        memory: MemoryRecord,
        events: Sequence[Mapping[str, Any]],
        skill_versions: Sequence[Mapping[str, Any]],
    ) -> tuple[ProcedureEvidenceRef, ...]:
        evidence: list[ProcedureEvidenceRef] = []
        sequence = 0
        for event in events:
            sequence += 1
            payload = mapping(event.get("payload"))
            event_id = str(event.get("event_id") or stable_id("event", event))
            event_type = str(event.get("event_type") or "event")
            tool_call_id = _first_at_keys(payload, {"tool_call_id", "toolCallId"})
            tool_name = _first_at_keys(payload, {"tool_name", "toolName", "name"})
            artifact_id = _first_at_keys(payload, {"artifact_id", "artifactId"})
            kind = (
                ProcedureEvidenceKind.TOOL_CALL
                if tool_call_id
                else ProcedureEvidenceKind.ARTIFACT
                if artifact_id
                else ProcedureEvidenceKind.VERIFICATION
                if "verif" in event_type.casefold()
                else ProcedureEvidenceKind.EVENT
            )
            evidence.append(
                ProcedureEvidenceRef(
                    evidence_id=stable_id(
                        "procedure-evidence",
                        outcome.outcome_id,
                        event_id,
                        kind.value,
                    ),
                    kind=kind,
                    source_id=event_id,
                    source_digest=stable_digest(event),
                    sequence=sequence,
                    occurred_at=str(event.get("created_at") or outcome.created_at),
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                    artifact_id=artifact_id,
                    event_id=event_id,
                    trusted_runtime=True,
                    canonical=True,
                    metadata={
                        "event_type": event_type,
                        "result_ok": _event_success(payload),
                        "runtime_event": True,
                    },
                ).validated()
            )
        sequence += 1
        evidence.append(
            ProcedureEvidenceRef(
                evidence_id=stable_id(
                    "procedure-evidence",
                    outcome.outcome_id,
                    memory.memory_id,
                    "memory",
                ),
                kind=ProcedureEvidenceKind.MEMORY,
                source_id=memory.memory_id,
                source_digest=stable_digest(
                    {
                        "memory_id": memory.memory_id,
                        "summary": memory.summary,
                        "content": memory.content,
                        "artifact_ids": memory.artifact_ids,
                        "evidence_ids": memory.evidence_ids,
                    }
                ),
                sequence=sequence,
                occurred_at=memory.updated_at,
                memory_id=memory.memory_id,
                trusted_runtime=True,
                canonical=True,
                metadata={
                    "memory_layer": str(memory.layer),
                    "memory_source_type": memory.source_type,
                    "memory_revision": outcome.memory_revision,
                },
            ).validated()
        )
        for version in skill_versions:
            sequence += 1
            evidence.append(
                ProcedureEvidenceRef(
                    evidence_id=stable_id(
                        "procedure-evidence",
                        outcome.outcome_id,
                        "skill-version",
                        version,
                    ),
                    kind=ProcedureEvidenceKind.SKILL_VERSION,
                    source_id=str(version["skill_id"]),
                    source_digest=str(version["descriptor_digest"]),
                    sequence=sequence,
                    occurred_at=outcome.created_at,
                    trusted_runtime=True,
                    canonical=False,
                    metadata=dict(version),
                ).validated()
            )
        existing_artifacts = {
            item.artifact_id for item in evidence if item.artifact_id
        }
        for artifact_id in memory.artifact_ids:
            if artifact_id in existing_artifacts:
                continue
            sequence += 1
            evidence.append(
                ProcedureEvidenceRef(
                    evidence_id=stable_id(
                        "procedure-evidence",
                        outcome.outcome_id,
                        "artifact",
                        artifact_id,
                    ),
                    kind=ProcedureEvidenceKind.ARTIFACT,
                    source_id=artifact_id,
                    source_digest=stable_digest(
                        {"artifact_id": artifact_id, "memory_id": memory.memory_id}
                    ),
                    sequence=sequence,
                    occurred_at=memory.updated_at,
                    artifact_id=artifact_id,
                    trusted_runtime=True,
                    canonical=True,
                    metadata={"source": "canonical_memory_artifact_ref"},
                ).validated()
            )
        return tuple(evidence)

    def _derive_steps(
        self,
        context: ProcedureMiningContext,
    ) -> tuple[ProcedureStep, ...]:
        tool_evidence = [
            item
            for item in context.evidence
            if item.kind is ProcedureEvidenceKind.TOOL_CALL
            and item.tool_call_id
        ]
        steps: list[ProcedureStep] = []
        for evidence in tool_evidence[: self.policy.maximum_tool_steps]:
            success = evidence.metadata.get("result_ok") is not False
            if self.policy.require_successful_tool_results and not success:
                if not self.policy.allow_failure_recovery_steps:
                    continue
            event = next(
                (
                    item for item in context.events
                    if str(item.get("event_id") or "") == evidence.event_id
                ),
                {},
            )
            payload = mapping(event.get("payload"))
            action = _action(payload, evidence.tool_name, evidence.tool_call_id)
            effect = _expected_effect(payload, success)
            artifact_kinds = unique_strings(
                _strings_at_keys(payload, {"artifact_kind", "kind"})
            )
            failure_routes = unique_strings(
                _strings_at_keys(payload, {"route", "failure_route", "recovery_route"})
            )
            input_shape = _first_at_keys(
                payload,
                {"arguments_digest", "input_digest", "request_digest"},
            )
            if input_shape and not re.fullmatch(r"(?:sha256:)?[0-9a-fA-F]{64}", input_shape):
                input_shape = stable_digest(_first_mapping_at_keys(payload, {"arguments", "input", "request"}))
            step = ProcedureStep(
                step_id=stable_id(
                    "procedure-step",
                    context.outcome.outcome_id,
                    len(steps) + 1,
                    evidence.tool_call_id,
                    action,
                ),
                ordinal=len(steps) + 1,
                action=action,
                tool_name=evidence.tool_name,
                input_shape_digest=input_shape.removeprefix("sha256:") if input_shape else "",
                expected_effect=effect,
                success_evidence_ids=(evidence.evidence_id,),
                artifact_kinds=artifact_kinds,
                retryable=not success,
                failure_routes=failure_routes,
                state=(
                    ProcedureStepState.VERIFIED
                    if success and evidence.trusted_runtime
                    else ProcedureStepState.FAILED
                    if not success
                    else ProcedureStepState.OBSERVED
                ),
                metadata={
                    "tool_call_id": evidence.tool_call_id,
                    "event_id": evidence.event_id,
                    "source_sequence": evidence.sequence,
                    "successful": success,
                },
            ).validated()
            steps.append(step)
        return tuple(steps)

    def _derive_applicability(
        self,
        context: ProcedureMiningContext,
        steps: Sequence[ProcedureStep],
    ) -> ProcedureApplicability:
        sources = (
            context.memory.content,
            context.memory.metadata,
            context.outcome.payload,
            context.outcome.metadata,
        )
        languages = unique_strings(
            value
            for source in sources
            for value in _strings_at_keys(source, {"language", "languages"})
        )
        workspace_kinds = unique_strings(
            value
            for source in sources
            for value in _strings_at_keys(
                source,
                {"workspace_kind", "workspace_type", "repository_kind"},
            )
        )
        goal_values = unique_strings(
            [
                context.outcome.subject,
                *(
                    value
                    for source in sources
                    for value in _strings_at_keys(
                        source,
                        {"goal", "objective", "task", "intent"},
                    )
                ),
            ]
        )
        goal_patterns = tuple(
            _goal_pattern(value)
            for value in goal_values[: self.policy.maximum_goal_patterns]
            if value
        )
        required_tools = unique_strings(
            step.tool_name for step in steps if step.tool_name
        )
        forbidden_tools = unique_strings(
            value
            for source in sources
            for value in _strings_at_keys(
                source,
                {"denied_tools", "forbidden_tools", "disallowed_tools"},
            )
        )
        artifact_kinds = unique_strings(
            kind for step in steps for kind in step.artifact_kinds
        )
        provider_capabilities = unique_strings(
            value
            for source in sources
            for value in _strings_at_keys(
                source,
                {"provider_capability", "provider_capabilities"},
            )
        )
        constraints: list[Mapping[str, Any]] = []
        for version in context.skill_versions:
            constraints.append(
                {
                    "condition_id": stable_id(
                        "procedure-condition",
                        context.outcome.outcome_id,
                        version["skill_id"],
                        version["descriptor_digest"],
                    ),
                    "kind": "custom",
                    "operator": "equals",
                    "key": "skill.descriptor_digest",
                    "value": version["descriptor_digest"],
                    "required": True,
                    "source_evidence_ids": [
                        item.evidence_id
                        for item in context.evidence
                        if item.kind is ProcedureEvidenceKind.SKILL_VERSION
                        and item.source_id == version["skill_id"]
                    ],
                }
            )
        return ProcedureApplicability(
            languages=languages,
            workspace_kinds=workspace_kinds,
            goal_patterns=goal_patterns,
            required_tools=required_tools,
            forbidden_tools=forbidden_tools,
            required_artifact_kinds=artifact_kinds,
            provider_capabilities=provider_capabilities,
            minimum_trust="verified",
            constraints=tuple(constraints),
        ).validated()

    def _score(
        self,
        context: ProcedureMiningContext,
        steps: Sequence[ProcedureStep],
    ) -> tuple[float, Mapping[str, float]]:
        verified_steps = sum(
            1 for step in steps if step.state is ProcedureStepState.VERIFIED
        )
        trusted_evidence = sum(1 for item in context.evidence if item.trusted_runtime)
        factors = {
            "curator_deterministic": 1.0 if context.outcome.deterministic_validation else 0.0,
            "canonical_memory": 1.0 if context.outcome.canonical_memory_changed else 0.0,
            "tool_step_coverage": min(1.0, verified_steps / max(1, len(steps))),
            "trusted_evidence": min(1.0, trusted_evidence / max(1, len(context.evidence))),
            "skill_version_provenance": 1.0 if context.skill_versions else 0.0,
            "artifact_provenance": 1.0 if context.artifact_ids else 0.5,
            "session_provenance": 1.0 if context.session_ids else 0.5,
            "memory_event_provenance": 1.0 if context.memory_event_ids else 0.0,
        }
        weights = {
            "curator_deterministic": 0.18,
            "canonical_memory": 0.18,
            "tool_step_coverage": 0.2,
            "trusted_evidence": 0.14,
            "skill_version_provenance": 0.12,
            "artifact_provenance": 0.06,
            "session_provenance": 0.06,
            "memory_event_provenance": 0.06,
        }
        confidence = sum(factors[key] * weight for key, weight in weights.items())
        return round(min(max(confidence, 0.0), 1.0), 6), factors

    def _validation_status(
        self,
        context: ProcedureMiningContext,
        steps: Sequence[ProcedureStep],
        confidence: float,
    ) -> ProcedureValidationStatus:
        if confidence < self.policy.validation_confidence:
            return ProcedureValidationStatus.CANDIDATE
        if self.policy.require_skill_version_for_validation and not context.skill_versions:
            return ProcedureValidationStatus.CANDIDATE
        if self.policy.require_artifact_for_validation and not context.artifact_ids:
            return ProcedureValidationStatus.CANDIDATE
        if self.policy.require_trusted_runtime_event and not any(
            item.trusted_runtime and item.kind is ProcedureEvidenceKind.TOOL_CALL
            for item in context.evidence
        ):
            return ProcedureValidationStatus.CANDIDATE
        if not all(step.state is ProcedureStepState.VERIFIED for step in steps):
            return ProcedureValidationStatus.CANDIDATE
        return ProcedureValidationStatus.VALIDATED

    def _provenance(self, context: ProcedureMiningContext) -> ProcedureProvenance:
        outcome = context.outcome
        return ProcedureProvenance(
            curator_outcome_id=outcome.outcome_id,
            curator_job_id=outcome.curator_job_id,
            curator_decision_id=outcome.decision_id,
            evidence_bundle_id=outcome.evidence_bundle_id,
            evidence_digest=outcome.evidence_digest,
            memory_id=outcome.memory_id,
            memory_revision=outcome.memory_revision,
            run_id=outcome.run_id,
            task_id=outcome.task_id,
            session_ids=context.session_ids,
            tool_call_ids=context.tool_call_ids,
            artifact_ids=context.artifact_ids,
            skill_versions=context.skill_versions,
            memory_event_ids=context.memory_event_ids,
        ).validated()

    def _name(
        self,
        context: ProcedureMiningContext,
        steps: Sequence[ProcedureStep],
    ) -> str:
        skill_name = (
            str(context.skill_versions[0].get("skill_name") or "").strip()
            if context.skill_versions
            else ""
        )
        if skill_name:
            return f"Validated procedure from {skill_name}"[:256]
        subject = re.sub(r"\s+", " ", context.outcome.subject).strip()
        if subject:
            return f"Procedure: {subject}"[:256]
        tools = ", ".join(
            unique_strings(step.tool_name for step in steps if step.tool_name)
        )
        return f"Successful tool procedure ({tools or 'runtime trajectory'})"[:256]

    def _summary(
        self,
        context: ProcedureMiningContext,
        steps: Sequence[ProcedureStep],
    ) -> str:
        parts = [context.outcome.summary.strip(), context.memory.summary.strip()]
        tool_flow = " -> ".join(
            step.tool_name or step.action for step in steps
        )
        if tool_flow:
            parts.append(f"Observed successful flow: {tool_flow}.")
        if context.artifact_ids:
            parts.append(
                "Produced artifacts: " + ", ".join(context.artifact_ids[:16]) + "."
            )
        parts.append(
            "Reuse remains conditional and any referenced skill must be resolved again by 03C."
        )
        return " ".join(dict.fromkeys(part for part in parts if part))[
            : self.policy.maximum_summary_characters
        ]

    def _signal(
        self,
        outcome: CuratorOutcome,
        procedure: ReusableProcedure,
    ) -> ProcedureSignal:
        kind = (
            ProcedureSignalKind.VALIDATED
            if procedure.state is ProcedureValidationStatus.VALIDATED
            else ProcedureSignalKind.MINED
        )
        return ProcedureSignal.build(
            kind=kind,
            run_id=outcome.run_id,
            task_id=outcome.task_id,
            procedure_id=procedure.procedure_id,
            outcome_id=outcome.outcome_id,
            causation_id=outcome.decision_id,
            payload={
                "procedure_id": procedure.procedure_id,
                "state": procedure.state.value,
                "confidence": procedure.confidence,
                "step_count": len(procedure.steps),
                "tool_call_ids": list(procedure.provenance.tool_call_ids),
                "artifact_ids": list(procedure.provenance.artifact_ids),
                "skill_versions": [dict(item) for item in procedure.provenance.skill_versions],
                "consumers": [consumer.value for consumer in procedure.consumers],
                "routing_visible": procedure.routable,
                "recovery_visible": procedure.recoverable,
                "03c_resolution_required": True,
            },
        )

    def _reject(
        self,
        outcome: CuratorOutcome,
        reason: str,
        *,
        context: ProcedureMiningContext | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> ProcedureMiningResult:
        receipt = ProcedureMiningReceipt.build(
            outcome_id=outcome.outcome_id or stable_id("invalid-outcome", outcome.to_dict()),
            task_id=outcome.task_id or "unknown-task",
            disposition=ProcedureMiningDisposition.REJECTED,
            procedure_id="",
            reason=reason,
            evidence_digest=(
                outcome.evidence_digest
                if re.fullmatch(r"[0-9a-f]{64}", outcome.evidence_digest or "")
                else stable_digest(outcome.to_dict())
            ),
            state_before="",
            state_after=ProcedureValidationStatus.REJECTED.value,
            metadata={
                **dict(details or {}),
                "canonical_memory_changed": outcome.canonical_memory_changed,
                "deterministic_validation": outcome.deterministic_validation,
                "static_document_activated": False,
                "model_summary_activated": False,
            },
        )
        self.procedure_store.put_receipt(receipt)
        return ProcedureMiningResult(
            outcome_id=outcome.outcome_id,
            disposition=ProcedureMiningDisposition.REJECTED,
            receipt=receipt,
            procedure=None,
            signal=None,
            context=context,
            warnings=(reason,),
        )

    def _emit(self, signal: ProcedureSignal) -> None:
        if self.event_sink is None:
            return
        self.event_sink(
            {
                "event_type": f"memory.{signal.kind.value}",
                "run_id": signal.run_id,
                "task_id": signal.task_id,
                "aggregate_id": signal.procedure_id,
                "causation_id": signal.causation_id,
                "correlation_id": signal.outcome_id,
                "payload": dict(signal.payload),
                "payload_digest": signal.payload_digest,
                "canonical_owner": "ReusableProcedureStore",
            }
        )


def _disabled() -> bool:
    import os

    return os.environ.get("ZYRA_DISABLE_REUSABLE_PROCEDURE_MINER", "").strip() == "1"


def _flatten(value: object) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_flatten(item)}" for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return " ".join(_flatten(item) for item in value)
    return str(value or "")


def _walk(value: object) -> Iterable[tuple[str, object]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield str(key), item
            yield from _walk(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _walk(item)


def _strings_at_keys(value: object, keys: set[str]) -> tuple[str, ...]:
    output: list[str] = []
    folded = {key.casefold() for key in keys}
    for key, item in _walk(value):
        if key.casefold() not in folded:
            continue
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            output.extend(str(value).strip() for value in item if str(value).strip())
        elif str(item or "").strip():
            output.append(str(item).strip())
    return unique_strings(output)


def _first_at_keys(value: object, keys: set[str]) -> str:
    values = _strings_at_keys(value, keys)
    return values[0] if values else ""


def _first_mapping_at_keys(
    value: object,
    keys: set[str],
) -> Mapping[str, Any]:
    folded = {key.casefold() for key in keys}
    for key, item in _walk(value):
        if key.casefold() in folded and isinstance(item, Mapping):
            return dict(item)
    return {}


def _session_id(event: Mapping[str, Any]) -> str:
    payload = mapping(event.get("payload"))
    return _first_at_keys(payload, {"session_id", "sessionId"})


def _event_success(payload: Mapping[str, Any]) -> bool | None:
    result = _first_mapping_at_keys(payload, {"tool_result", "result", "outcome"})
    for source in (result, payload):
        if "ok" in source:
            return bool(source["ok"])
        status = str(source.get("status") or "").casefold()
        if status in {"completed", "succeeded", "success", "ok", "passed"}:
            return True
        if status in {"failed", "error", "denied", "cancelled"}:
            return False
    return None


def _version_candidates(value: object) -> tuple[Mapping[str, Any], ...]:
    output: list[Mapping[str, Any]] = []
    if isinstance(value, Mapping):
        if (
            ("skill_id" in value or "skillId" in value)
            and ("descriptor_digest" in value or "descriptorDigest" in value)
        ):
            output.append(dict(value))
        for item in value.values():
            output.extend(_version_candidates(item))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            output.extend(_version_candidates(item))
    return tuple(output)


def _normalize_skill_version(
    value: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    skill_id = str(value.get("skill_id") or value.get("skillId") or "").strip()
    skill_name = str(value.get("skill_name") or value.get("skillName") or "").strip()
    descriptor_digest = str(
        value.get("descriptor_digest") or value.get("descriptorDigest") or ""
    ).removeprefix("sha256:").lower()
    body_digest = str(
        value.get("body_digest") or value.get("bodyDigest") or ""
    ).removeprefix("sha256:").lower()
    revision = int(value.get("registry_revision") or value.get("registryRevision") or 0)
    if (
        not skill_id
        or not skill_name
        or revision < 1
        or not re.fullmatch(r"[0-9a-f]{64}", descriptor_digest)
        or not re.fullmatch(r"[0-9a-f]{64}", body_digest)
    ):
        return None
    resources = mapping(
        value.get("resource_digests") or value.get("resourceDigests")
    )
    resource_digests = {
        str(path): str(digest_value).removeprefix("sha256:").lower()
        for path, digest_value in resources.items()
        if re.fullmatch(
            r"[0-9a-f]{64}",
            str(digest_value).removeprefix("sha256:").lower(),
        )
    }
    return {
        "skill_id": skill_id,
        "skill_name": skill_name,
        "registry_revision": revision,
        "descriptor_digest": descriptor_digest,
        "body_digest": body_digest,
        "resource_digests": resource_digests,
        "source_revision": str(
            value.get("source_revision") or value.get("sourceRevision") or revision
        ),
    }


def _action(
    payload: Mapping[str, Any],
    tool_name: str,
    tool_call_id: str,
) -> str:
    summary = _first_at_keys(payload, {"summary", "action", "operation", "description"})
    if summary:
        return re.sub(r"\s+", " ", summary).strip()[:2_000]
    return f"Execute {tool_name or 'tool'} call {tool_call_id}"


def _expected_effect(payload: Mapping[str, Any], success: bool) -> str:
    result = _first_at_keys(
        payload,
        {"effect", "result_summary", "output_summary", "summary", "outcome"},
    )
    if result:
        return re.sub(r"\s+", " ", result).strip()[:2_000]
    return "Observed successful runtime state mutation" if success else "Observed recoverable failure"


def _goal_pattern(value: str) -> str:
    tokens = [
        token for token in re.findall(r"[\w.-]+", value.casefold())
        if len(token) > 2
    ][:12]
    if not tokens:
        return r".*"
    return r"(?i).*" + r".*".join(re.escape(token) for token in tokens) + r".*"


__all__ = [
    "ProcedureMinerPolicy",
    "ProcedureMiningContext",
    "ProcedureMiningResult",
    "ReusableProcedureMiner",
]
