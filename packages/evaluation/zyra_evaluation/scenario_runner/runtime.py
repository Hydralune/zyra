from __future__ import annotations

import os
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Protocol

from .canonical import canonicalize, digest, new_identity, utc_now
from .effective_steps import EffectiveStepClassifier, require_effect_coverage
from .errors import ScenarioRunnerError, conflict, invalid, unavailable
from .evidence import EvidenceCollector
from .metrics import ScenarioMetricCollector
from .models import (
    OwnerExecutionResult,
    ScenarioConfiguration,
    ScenarioMode,
    ScenarioPhase,
    ScenarioRun,
)
from .preflight import CleanStateInspector, ensure_scratch_roots, environment_preflight_guards
from .registry import ScenarioRegistry, build_configuration
from .sealed_policy import SealedPolicyRuntime, require_safe_policy_decisions
from .source_audit import SourceRoleAuditor
from .store import ScenarioRunStore


class ScenarioExecutionPort(Protocol):
    def execute(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        goal: str,
        policy_decisions: tuple[dict[str, Any], ...],
        cancel_requested: Callable[[], bool],
    ) -> OwnerExecutionResult: ...


class CallbackScenarioExecutionPort:
    def __init__(self, callback: Callable[..., OwnerExecutionResult]) -> None:
        self._callback = callback

    def execute(
        self,
        *,
        scenario_run_id: str,
        configuration: ScenarioConfiguration,
        goal: str,
        policy_decisions: tuple[dict[str, Any], ...],
        cancel_requested: Callable[[], bool],
    ) -> OwnerExecutionResult:
        return self._callback(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            goal=goal,
            policy_decisions=policy_decisions,
            cancel_requested=cancel_requested,
        )


class UnboundScenarioExecutionPort:
    def execute(self, **_: Any) -> OwnerExecutionResult:
        raise unavailable(
            "scenario_execution_port_unbound",
            "Scenario runner is not bound to the canonical M1/M2 owners.",
            phase="execution",
        )


class ScenarioRunnerService:
    def __init__(
        self,
        *,
        project_root: str | Path,
        store: ScenarioRunStore,
        execution: ScenarioExecutionPort,
        artifact_root: str | Path,
        default_preflight_paths: Mapping[str, str],
        registry: ScenarioRegistry | None = None,
        maximum_workers: int = 2,
        auto_reconcile: bool = True,
    ) -> None:
        self.project_root = Path(project_root).resolve(strict=False)
        self.store = store
        self.registry = registry or ScenarioRegistry.defaults()
        self.execution = execution
        self.artifact_root = Path(artifact_root).resolve(strict=False)
        self.default_preflight_paths = {
            str(key): str(value) for key, value in default_preflight_paths.items()
        }
        self.source_auditor = SourceRoleAuditor(project_root=self.project_root)
        self.metrics = ScenarioMetricCollector()
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, min(32, maximum_workers)),
            thread_name_prefix="zyra-scenario",
        )
        self._futures: dict[str, Future[None]] = {}
        self._lock = threading.RLock()
        self._closed = False
        if auto_reconcile:
            self.reconcile()

    def create(self, request: Mapping[str, Any]) -> ScenarioRun:
        self._require_open()
        environment_preflight_guards()
        configuration = build_configuration(
            self.registry,
            request,
            project_root=str(self.project_root),
            default_preflight_paths=self.default_preflight_paths,
        )
        definition = self.registry.definition(
            configuration.scenario_id,
            configuration.definition_version,
        )
        if definition.definition_digest != configuration.definition_digest:
            raise conflict(
                "scenario_definition_digest_mismatch",
                "Scenario configuration does not match the registered definition.",
                phase="admission",
            )
        policy = SealedPolicyRuntime(configuration.policy)
        policy.verify_configuration(configuration)
        run = ScenarioRun.create(configuration)
        inspector = CleanStateInspector(input_seen=self.store.input_seen)
        if configuration.mode is ScenarioMode.SEALED:
            preflight = inspector.require_formal_admission(
                run.scenario_run_id,
                configuration,
            )
        else:
            preflight = inspector.inspect(run.scenario_run_id, configuration)
        source_audit = self.source_auditor.require_valid()
        self.store.create(run)
        admitted = run.evolve(
            phase=ScenarioPhase.ADMITTED,
            preflight_receipt=preflight.to_dict(),
            failure=None,
        )
        admitted = self.store.save(
            admitted,
            expected_revision=run.revision,
            reason="scenario.admitted",
            detail={
                "preflight_receipt_digest": preflight.receipt_digest,
                "source_audit_digest": source_audit["audit_digest"],
            },
        )
        self.store.append_receipt(
            admitted.scenario_run_id,
            kind="preflight",
            receipt=preflight.to_dict(),
        )
        self.store.append_receipt(
            admitted.scenario_run_id,
            kind="source_audit",
            receipt=source_audit,
        )
        return admitted

    def start(
        self,
        scenario_run_id: str,
        *,
        wait: bool = False,
        timeout: float | None = None,
    ) -> ScenarioRun:
        self._require_open()
        environment_preflight_guards()
        run = self.store.require(scenario_run_id)
        if run.phase is ScenarioPhase.SUCCEEDED:
            return run
        if run.phase not in {ScenarioPhase.ADMITTED, ScenarioPhase.FAILED}:
            raise conflict(
                "scenario_start_phase_invalid",
                "Scenario can start only after admission or an explicitly retryable failure.",
                phase="lifecycle",
                detail={"phase": run.phase.value},
            )
        policy = SealedPolicyRuntime(run.configuration.policy)
        policy.verify_configuration(run.configuration)
        inspector = CleanStateInspector(
            input_seen=lambda value: self.store.input_seen(
                value,
                excluding_run_id=run.scenario_run_id,
            )
        )
        if run.configuration.mode is ScenarioMode.SEALED:
            preflight = inspector.require_formal_admission(
                run.scenario_run_id,
                run.configuration,
            )
        else:
            preflight = inspector.inspect(run.scenario_run_id, run.configuration)
        ensure_scratch_roots(run.configuration.preflight_targets)
        queued = run.evolve(
            phase=ScenarioPhase.QUEUED,
            preflight_receipt=preflight.to_dict(),
            failure=None,
            cancel_requested=False,
            completed_at="",
        )
        queued = self.store.save(
            queued,
            expected_revision=run.revision,
            reason="scenario.queued",
            detail={"preflight_receipt_digest": preflight.receipt_digest},
        )
        self.store.append_receipt(
            queued.scenario_run_id,
            kind="preflight_start",
            receipt=preflight.to_dict(),
        )
        with self._lock:
            existing = self._futures.get(queued.scenario_run_id)
            if existing is None or existing.done():
                self._futures[queued.scenario_run_id] = self._executor.submit(
                    self._run,
                    queued.scenario_run_id,
                )
            future = self._futures[queued.scenario_run_id]
        if wait:
            future.result(timeout=timeout)
            return self.store.require(queued.scenario_run_id)
        return queued

    def status(self, scenario_run_id: str) -> dict[str, Any]:
        run = self.store.require(scenario_run_id)
        transitions = self.store.transitions(scenario_run_id)
        receipts = self.store.receipts(scenario_run_id)
        with self._lock:
            future = self._futures.get(scenario_run_id)
            worker_active = bool(future and not future.done())
        return {
            "schema": "zyra.scenario-status/v1",
            "run": run.to_dict(),
            "transitions": list(transitions),
            "receipts": list(receipts),
            "worker_active": worker_active,
            "browser_connection_required": False,
            "status_digest": digest(
                {
                    "run": run.to_dict(),
                    "transitions": transitions,
                    "receipts": receipts,
                    "worker_active": worker_active,
                }
            ),
        }

    def list(
        self,
        *,
        include_archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        runs = self.store.list(
            include_archived=include_archived,
            limit=limit,
            offset=offset,
        )
        return {
            "schema": "zyra.scenario-run-page/v1",
            "offset": max(0, offset),
            "limit": max(1, min(10_000, limit)),
            "runs": [item.to_dict() for item in runs],
            "store": self.store.summary(),
        }

    def cancel(
        self,
        scenario_run_id: str,
        *,
        reason: str,
        actor_id: str,
    ) -> ScenarioRun:
        self._require_open()
        selected_reason = str(reason or "").strip()
        if not selected_reason:
            raise invalid(
                "scenario_cancel_reason_missing",
                "Scenario cancellation requires a reason.",
                phase="lifecycle",
            )
        run = self.store.require(scenario_run_id)
        if run.terminal:
            return run
        if run.configuration.mode is ScenarioMode.SEALED:
            policy = SealedPolicyRuntime(run.configuration.policy)
            intervention = policy.record_operator_attempt(
                actor_id=actor_id,
                action="operator.cancel",
                reason=selected_reason,
                kind="manual_mutation",
            )
            failed = run.evolve(
                phase=ScenarioPhase.FAILED,
                interventions=(*run.interventions, intervention.to_dict()),
                failure={
                    "code": "scenario_sealed_operator_attempt",
                    "message": "Sealed scenario rejected an operator cancellation attempt.",
                    "retryable": False,
                },
                completed_at=utc_now(),
                cancel_requested=False,
            )
            return self.store.save(
                failed,
                expected_revision=run.revision,
                reason="scenario.sealed_operator_attempt_rejected",
            )
        next_phase = (
            ScenarioPhase.CANCELLING
            if run.phase is ScenarioPhase.RUNNING
            else ScenarioPhase.CANCELLED
        )
        cancelled = run.evolve(
            phase=next_phase,
            cancel_requested=True,
            completed_at=utc_now() if next_phase is ScenarioPhase.CANCELLED else "",
            failure={
                "code": "scenario_cancel_requested",
                "message": selected_reason,
                "actor_id": actor_id,
            },
        )
        return self.store.save(
            cancelled,
            expected_revision=run.revision,
            reason="scenario.cancel_requested",
        )

    def archive(
        self,
        scenario_run_id: str,
        *,
        reason: str,
    ) -> ScenarioRun:
        run = self.store.require(scenario_run_id)
        if not run.terminal:
            raise conflict(
                "scenario_archive_active",
                "Active scenario cannot be archived.",
                phase="lifecycle",
            )
        if run.phase is ScenarioPhase.ARCHIVED:
            return run
        archived = run.evolve(
            phase=ScenarioPhase.ARCHIVED,
            archive_reason=str(reason or "Archived from scenario control plane.").strip(),
        )
        return self.store.save(
            archived,
            expected_revision=run.revision,
            reason="scenario.archived",
        )

    def verify(self, scenario_run_id: str) -> dict[str, Any]:
        run = self.store.require(scenario_run_id)
        if not run.evidence_manifest:
            raise conflict(
                "scenario_evidence_missing",
                "Scenario has no evidence manifest to verify.",
                phase="evidence",
            )
        collector = EvidenceCollector(artifact_root=self.artifact_root)
        receipt = collector.verify(run.evidence_manifest, mode=run.configuration.mode)
        self.store.append_receipt(
            scenario_run_id,
            kind="evidence_reverification",
            receipt=receipt,
        )
        return receipt

    def reconcile(self) -> tuple[ScenarioRun, ...]:
        if self._closed:
            return ()
        reconciled = self.store.reconcile_interrupted()
        for run in reconciled:
            if run.phase is ScenarioPhase.QUEUED:
                with self._lock:
                    existing = self._futures.get(run.scenario_run_id)
                    if existing is None or existing.done():
                        self._futures[run.scenario_run_id] = self._executor.submit(
                            self._run,
                            run.scenario_run_id,
                        )
        return reconciled

    def close(self, *, wait: bool = False) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=wait, cancel_futures=False)

    def _run(self, scenario_run_id: str) -> None:
        try:
            queued = self.store.require(scenario_run_id)
            if queued.cancel_requested:
                cancelled = queued.evolve(
                    phase=ScenarioPhase.CANCELLED,
                    completed_at=utc_now(),
                )
                self.store.save(
                    cancelled,
                    expected_revision=queued.revision,
                    reason="scenario.cancelled_before_start",
                )
                return
            running = queued.evolve(
                phase=ScenarioPhase.RUNNING,
                started_at=utc_now(),
            )
            running = self.store.save(
                running,
                expected_revision=queued.revision,
                reason="scenario.running",
            )
            configuration = running.configuration
            definition = self.registry.definition(
                configuration.scenario_id,
                configuration.definition_version,
            )
            policy = SealedPolicyRuntime(configuration.policy)
            policy.verify_configuration(configuration)
            decisions = policy.evaluate_all(definition.planned_actions)
            require_safe_policy_decisions(
                decisions,
                required_actions=tuple(
                    str(item.get("action") or "")
                    for item in definition.planned_actions
                    if str(item.get("effect") or "ask").casefold() == "allow"
                ),
            )
            goal = definition.goal_template.format(
                input=configuration.input_text,
                seed=configuration.seed,
            )
            owner = self.execution.execute(
                scenario_run_id=scenario_run_id,
                configuration=configuration,
                goal=goal,
                policy_decisions=tuple(item.to_dict() for item in decisions),
                cancel_requested=lambda: self.store.require(
                    scenario_run_id
                ).cancel_requested,
            )
            current = self.store.require(scenario_run_id)
            if current.cancel_requested and current.configuration.mode is not ScenarioMode.SEALED:
                cancelled = current.evolve(
                    phase=ScenarioPhase.CANCELLED,
                    owner_run_id=owner.owner_run_id,
                    task_id=owner.task_id,
                    completed_at=utc_now(),
                )
                self.store.save(
                    cancelled,
                    expected_revision=current.revision,
                    reason="scenario.cancelled_after_owner_settlement",
                )
                return
            events = list(owner.events)
            events.extend(
                _receipt_event(
                    item,
                    owner_run_id=owner.owner_run_id,
                    task_id=owner.task_id,
                    previous_event_id=(
                        str(events[-1].get("event_id") or "") if events else ""
                    ),
                )
                for item in owner.owner_receipts
            )
            events.extend(
                _policy_event(
                    item.to_dict(),
                    owner_run_id=owner.owner_run_id,
                    task_id=owner.task_id,
                    previous_event_id=(
                        str(events[-1].get("event_id") or "") if events else ""
                    ),
                )
                for item in decisions
                if item.action == "permission.evaluate"
            )
            classifier = EffectiveStepClassifier(profile=configuration.profile)
            step_batch = classifier.classify_all(
                events,
                expected_run_id=owner.owner_run_id,
                expected_task_id=owner.task_id,
            )
            coverage = require_effect_coverage(
                step_batch,
                expected_effects=definition.expected_effects,
                minimum_steps=definition.minimum_effective_steps,
                maximum_steps=configuration.profile.maximum_effective_steps,
            )
            policy_receipt = policy.assert_formal_invariants()
            samples = self.metrics.collect(
                step_batch,
                scenario_run_id=scenario_run_id,
                owner_run_id=owner.owner_run_id,
                task_id=owner.task_id,
                started_at=owner.started_at,
                completed_at=owner.completed_at,
            )
            summary = self.metrics.summarize(samples)
            source_audit = self.source_auditor.require_valid()
            collector = EvidenceCollector(artifact_root=self.artifact_root)
            manifest = collector.collect(
                scenario_run_id=scenario_run_id,
                configuration=configuration,
                owner=owner,
                step_batch=step_batch,
                policy_receipt=policy_receipt,
                preflight_receipt=current.preflight_receipt or {},
                metric_samples=samples,
                metric_summary=summary,
                coverage_receipt=coverage,
                source_audit=source_audit,
            )
            verification = dict(manifest["verification_receipt"])
            self.store.append_receipt(
                scenario_run_id,
                kind="policy",
                receipt=policy_receipt,
            )
            self.store.append_receipt(
                scenario_run_id,
                kind="effective_steps",
                receipt=step_batch.to_dict(),
            )
            self.store.append_receipt(
                scenario_run_id,
                kind="metrics",
                receipt={
                    "receipt_id": new_identity("metrics"),
                    "summary": summary,
                    "raw_samples": [item.to_dict() for item in samples],
                },
            )
            self.store.append_receipt(
                scenario_run_id,
                kind="evidence_manifest",
                receipt=manifest,
            )
            completed = self.store.require(scenario_run_id)
            succeeded = completed.evolve(
                phase=ScenarioPhase.SUCCEEDED,
                owner_run_id=owner.owner_run_id,
                task_id=owner.task_id,
                completed_at=utc_now(),
                policy_decisions=tuple(item.to_dict() for item in decisions),
                interventions=tuple(item.to_dict() for item in policy.interventions),
                evidence_manifest=manifest,
                verification_receipt=verification,
                failure=None,
            )
            self.store.save(
                succeeded,
                expected_revision=completed.revision,
                reason="scenario.succeeded",
                detail={
                    "manifest_digest": manifest["manifest_digest"],
                    "effective_step_count": len(step_batch.admitted),
                },
            )
        except BaseException as error:
            self._record_failure(scenario_run_id, error)

    def _record_failure(self, scenario_run_id: str, error: BaseException) -> None:
        try:
            current = self.store.require(scenario_run_id)
            if current.phase in {
                ScenarioPhase.SUCCEEDED,
                ScenarioPhase.CANCELLED,
                ScenarioPhase.ARCHIVED,
            }:
                return
            if isinstance(error, ScenarioRunnerError):
                failure = error.response()
            else:
                failure = {
                    "schema": "zyra.scenario-error/v1",
                    "ok": False,
                    "error": "scenario_execution_failed",
                    "message": str(error),
                    "phase": "execution",
                    "retryable": False,
                    "fallback": False,
                }
            failed = current.evolve(
                phase=ScenarioPhase.FAILED,
                completed_at=utc_now(),
                failure=failure,
            )
            self.store.save(
                failed,
                expected_revision=current.revision,
                reason="scenario.failed",
                detail={"error": failure.get("error")},
            )
            self.store.append_receipt(
                scenario_run_id,
                kind="failure",
                receipt={
                    "receipt_id": new_identity("failure"),
                    **failure,
                    "recorded_at": utc_now(),
                },
            )
        except BaseException:
            return

    def _require_open(self) -> None:
        if self._closed:
            raise unavailable(
                "scenario_runner_closed",
                "Scenario runner is closed.",
                phase="lifecycle",
            )


def _receipt_event(
    receipt: Mapping[str, Any],
    *,
    owner_run_id: str,
    task_id: str,
    previous_event_id: str,
) -> dict[str, Any]:
    event_type = str(
        receipt.get("event_type")
        or receipt.get("kind")
        or receipt.get("receipt_kind")
        or "verification"
    ).strip().casefold()
    event_id = str(
        receipt.get("event_id")
        or receipt.get("receipt_id")
        or new_identity("event")
    )
    return {
        "event_id": event_id,
        "event_type": event_type,
        "run_id": owner_run_id,
        "task_id": task_id,
        "node_id": str(receipt.get("node_id") or "root"),
        "causation_id": str(receipt.get("causation_id") or previous_event_id),
        "created_at": str(receipt.get("created_at") or utc_now()),
        "payload": {
            "receipt": canonicalize(receipt),
            "semantic_effect": str(receipt.get("semantic_effect") or ""),
        },
        "metadata": {
            "stage": str(receipt.get("stage") or event_type.split("_", 1)[0]),
            "worker_id": str(receipt.get("worker_id") or ""),
            "provider_id": str(receipt.get("provider_id") or ""),
            "semantic_effect": str(receipt.get("semantic_effect") or ""),
        },
    }


def _policy_event(
    decision: Mapping[str, Any],
    *,
    owner_run_id: str,
    task_id: str,
    previous_event_id: str,
) -> dict[str, Any]:
    return {
        "event_id": str(decision["decision_id"]),
        "event_type": "permission_decision",
        "run_id": owner_run_id,
        "task_id": task_id,
        "node_id": "root",
        "causation_id": previous_event_id,
        "created_at": str(decision.get("created_at") or utc_now()),
        "payload": {
            "permission": canonicalize(decision),
            "semantic_effect": "permission",
        },
        "metadata": {
            "stage": "permission",
            "semantic_effect": "permission",
        },
    }
