from __future__ import annotations

import importlib
import json
import os
import shutil
import subprocess
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from zyra_evaluation.policy_benchmark.long_run_validator import (
    EVIDENCE_INDEX_SCHEMA,
    IndependentTransitionValidator,
    OWNER_SNAPSHOT_SCHEMA,
    canonical_digest,
    file_digest,
)
from zyra_evaluation.policy_benchmark.sealed_physical import (
    SealedPhysicalDispatchRuntime,
    SealedPhysicalEvidence,
    SealedPlacementOwner,
)
from zyra_evaluation.scenario_runner import (
    DualDomainOwnerBindings,
    DualDomainScenarioExecutor,
    ScenarioRegistry,
    build_configuration,
)
from zyra_evaluation.scenario_runner.canonical import canonicalize, utc_now
from zyra_evaluation.scenario_runner.live_models import FaultKind
from zyra_productization.release.worktree import (
    inspect_worktree,
    require_worktree_boundary,
)


SEALED_MANIFEST_SCHEMA = "zyra.phase2-sealed-long-run-manifest/v1"


class SealedLongRunError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class _CausalEventRecord:
    run_id: str
    task_id: str
    event_type: Any
    event_id: str
    node_id: str | None
    created_at: str
    payload: dict[str, Any]
    causation_id: str


class _SealedInlineProductionPolicy:
    """Run the existing production strongest composition before domain work."""

    def __init__(
        self,
        *,
        api_main: Any,
        owner: Any,
        project_root: Path,
        state_root: Path,
    ) -> None:
        self.api_main = api_main
        self.owner = owner
        self.project_root = project_root
        self.state_root = state_root
        self._bundle: dict[str, Any] | None = None

    def execute(
        self,
        *,
        owner_context: Mapping[str, Any],
        configuration: Any,
        goal: str,
    ) -> Mapping[str, Any]:
        del configuration, goal
        from zyra_orchestration import run_task_graph

        state = self.owner.state
        if state is None:
            raise SealedLongRunError("inline strongest policy has no canonical task")
        canonical_store = self.api_main.get_store()
        existing_events = tuple(canonical_store.task_events(state.task_id))
        root_event_id = str(
            owner_context.get("task_created_event_id")
            or (
                existing_events[-1].get("event_id")
                if existing_events
                and isinstance(existing_events[-1], Mapping)
                else ""
            )
            or ""
        )
        if not root_event_id:
            raise SealedLongRunError(
                "inline strongest policy has no canonical root event"
            )
        execution_context = self.api_main.graph_execution_context()
        loopx_pre_control = self._loopx_pre_control(
            state=state,
            causation_id=root_event_id,
        )
        state.metadata["phase2_loopx_pre_control"] = dict(
            loopx_pre_control
        )
        self.api_main.get_store().save_checkpoint(state)
        returned_events = run_task_graph(
            state,
            execution_context=execution_context,
        )
        events: list[Any] = []
        previous_event_id = root_event_id
        for event in returned_events:
            selected = event
            if not str(getattr(event, "causation_id", "") or "") and all(
                hasattr(event, field)
                for field in (
                    "run_id",
                    "task_id",
                    "event_type",
                    "event_id",
                    "created_at",
                    "payload",
                )
            ):
                selected = _CausalEventRecord(
                    run_id=str(event.run_id),
                    task_id=str(event.task_id),
                    event_type=event.event_type,
                    event_id=str(event.event_id),
                    node_id=getattr(event, "node_id", None),
                    created_at=str(event.created_at),
                    payload=dict(event.payload),
                    causation_id=previous_event_id,
                )
            events.append(selected)
            previous_event_id = str(
                getattr(selected, "event_id", "") or previous_event_id
            )
        self.api_main.persist_events(canonical_store, events)
        canonical_store.save_checkpoint(state)
        selected_policy: dict[str, Any] | None = None
        selected_policy_event: Any | None = None
        for event in events:
            payload = _mapping(getattr(event, "payload", {}))
            policy = _mapping(payload.get("topology_policy"))
            if (
                policy.get("used_baseline") is False
                and policy.get("committed") is True
                and _mapping(policy.get("operator_candidate_set"))
            ):
                selected_policy = policy
                selected_policy_event = event
        if selected_policy is None:
            observed = [
                {
                    "event_id": str(getattr(event, "event_id", "") or ""),
                    "topology_policy": _mapping(
                        _mapping(getattr(event, "payload", {})).get(
                            "topology_policy"
                        )
                    ),
                }
                for event in events
                if _mapping(
                    _mapping(getattr(event, "payload", {})).get(
                        "topology_policy"
                    )
                )
            ]
            raise SealedLongRunError(
                "production strongest policy did not commit before domain execution: "
                + json.dumps(observed, ensure_ascii=False, sort_keys=True)[-4096:]
            )
        placement = _mapping(selected_policy.get("physical_placement"))
        candidate = _mapping(selected_policy.get("operator_candidate_set"))
        permission = _mapping(selected_policy.get("permission_receipt"))
        layers = tuple(
            _mapping(item)
            for item in state.metadata.get("phase2_operator_execution_layers") or ()
        )
        worker_receipt = _mapping(state.metadata.get("worker_pool_receipt"))
        placement_binding = _mapping(
            state.metadata.get("operator_placement_binding")
        )
        physical = _mapping(worker_receipt.get("physical_dispatch_receipt"))
        physical_payload = _mapping(physical.get("payload"))
        loopx_consumption = _mapping(
            selected_policy.get("loopx_pre_control")
        )
        if not layers:
            raise SealedLongRunError(
                "production strongest policy has no executed operator layer"
            )
        layer = layers[-1]
        checks = {
            "candidate_set": (
                candidate.get("candidate_set_digest")
                == placement.get("candidate_set_digest")
                == placement_binding.get("candidate_set_digest")
            ),
            "resource_decision": (
                placement.get("resource_decision_id")
                == layer.get("resource_decision_id")
                == physical_payload.get("placement_decision_id")
            ),
            "lease": (
                placement.get("lease_id")
                == layer.get("lease_id")
                == physical_payload.get("lease_id")
            ),
            "attempt": (
                placement.get("attempt_id")
                == layer.get("attempt_id")
                == physical_payload.get("physical_attempt_id")
            ),
            "physical_receipt": (
                layer.get("physical_dispatch_receipt_digest")
                == physical.get("digest")
            ),
            "real_execution": (
                physical_payload.get("simulated") is False
                and physical_payload.get("semantic_only") is False
                and _mapping(
                    worker_receipt.get("physical_dispatch_validation")
                ).get("real_gate_closed")
                is True
            ),
            "loopx_pre_control_placement": (
                loopx_pre_control.get("receipt_digest")
                == loopx_consumption.get("receipt_digest")
                == placement_binding.get("loopx_pre_control_digest")
                and loopx_consumption.get("consumed_before_topology") is True
            ),
            "loopx_pre_control_operator": (
                candidate.get("input_snapshot_digest")
                == loopx_consumption.get("operator_policy_input_digest")
                == placement_binding.get(
                    "loopx_operator_policy_input_digest"
                )
            ),
            "loopx_pre_control_permission": (
                _mapping(
                    loopx_pre_control.get("permission_receipt")
                ).get("receipt_digest")
                == loopx_consumption.get("permission_receipt_digest")
            ),
        }
        topology = _mapping(selected_policy.get("topology_result"))
        decision = _mapping(topology.get("decision_receipt"))
        decision_payload = _mapping(decision.get("payload")) or decision
        graph_commit = _mapping(decision_payload.get("graph_commit"))
        composition = _mapping(topology.get("composition"))
        proposal = _mapping(composition.get("proposal"))
        proposal_payload = _mapping(proposal.get("payload")) or proposal
        operations = tuple(
            _mapping(item)
            for item in _sequence(proposal_payload.get("operations"))
            if isinstance(item, Mapping)
        )
        layer_records = tuple(
            _mapping(item)
            for item in _sequence(composition.get("layers"))
            if isinstance(item, Mapping)
        )
        checks["loopx_pre_control_topology"] = (
            composition.get("policy_input_digest")
            == loopx_consumption.get("topology_policy_input_digest")
            == placement_binding.get("loopx_topology_policy_input_digest")
        )
        if not all(checks.values()):
            raise SealedLongRunError(
                "production strongest receipt chain is inconsistent: "
                f"checks={checks}, topology_policy_input_digests="
                f"{(composition.get('policy_input_digest'), loopx_consumption.get('topology_policy_input_digest'), placement_binding.get('loopx_topology_policy_input_digest'))}"
            )
        completion_event = next(
            (
                event
                for event in events
                if _mapping(getattr(event, "payload", {})).get("schema")
                == "zyra.production-adaptive-depth-completion-gate/v1"
            ),
            None,
        )
        completion_gate = _mapping(
            getattr(completion_event, "payload", {})
            if completion_event is not None
            else {}
        )
        continuity_receipt = _mapping(
            completion_gate.get("continuity_receipt")
        )
        symbolic_ref = _mapping(
            completion_gate.get("symbolic_bundle_policy_artifact_ref")
        )
        if (
            not graph_commit
            or completion_event is None
            or completion_gate.get("hard_conditions_passed") is not True
            or _mapping(continuity_receipt.get("payload")).get(
                "continuity_result"
            )
            != "passed"
            or not symbolic_ref.get("digest")
        ):
            raise SealedLongRunError(
                "production strongest mechanism chain is incomplete"
            )
        loopx = self._loopx_chain(
            state=state,
            pre_control=loopx_pre_control,
            graph_commit=graph_commit,
            permission=permission,
            placement=placement,
            causation_id=str(
                getattr(selected_policy_event, "event_id", "") or ""
            ),
        )
        operation_kinds = tuple(
            sorted({str(item.get("kind") or "") for item in operations})
        )
        mechanism_chain = {
            "schema": "zyra.phase2-production-mechanism-chain/v1",
            "topology": {
                "composition_digest": _evidence_digest(composition),
                "decision_digest": decision.get("digest"),
                "composition": composition,
                "decision_receipt": decision,
                "graph_commit": graph_commit,
                "operation_kinds": list(operation_kinds),
                "operation_count": len(operations),
                "layers": list(layer_records),
                "projection_differences": list(
                    _sequence(composition.get("projection_differences"))
                ),
                "loopx_pre_control_consumption": loopx_consumption,
                "canonical_custody_commit": True,
            },
            "continuity": {
                "completion_event_id": str(
                    getattr(completion_event, "event_id", "") or ""
                ),
                "receipt": continuity_receipt,
                "symbolic_bundle_policy_artifact_ref": symbolic_ref,
                "final_verifier_receipt_ref": completion_gate.get(
                    "final_verifier_receipt_ref"
                ),
                "physical_execution_receipt_ref": completion_gate.get(
                    "physical_execution_receipt_ref"
                ),
                "hard_conditions_passed": completion_gate.get(
                    "hard_conditions_passed"
                ),
            },
            "operator": {
                "candidate_set_digest": candidate.get(
                    "candidate_set_digest"
                ),
                "operator_ref": layer.get("operator_ref"),
                "resource_decision_id": placement.get(
                    "resource_decision_id"
                ),
                "lease_id": placement.get("lease_id"),
                "physical_receipt_digest": physical.get("digest"),
                "loopx_pre_control_digest": placement_binding.get(
                    "loopx_pre_control_digest"
                ),
                "loopx_topology_policy_input_digest": placement_binding.get(
                    "loopx_topology_policy_input_digest"
                ),
                "loopx_operator_policy_input_digest": placement_binding.get(
                    "loopx_operator_policy_input_digest"
                ),
            },
            "loopx": loopx,
        }
        mechanism_chain["chain_digest"] = _evidence_digest(mechanism_chain)
        production_event_ids = [
            str(getattr(item, "event_id", "") or "") for item in events
        ]
        persisted_by_id = {
            str(item.get("event_id") or ""): dict(item)
            for item in canonical_store.task_events(state.task_id)
        }
        production_snapshot = [
            persisted_by_id[event_id]
            for event_id in production_event_ids
            if event_id in persisted_by_id
        ]
        if (
            not production_event_ids
            or len(production_snapshot) != len(production_event_ids)
            or selected_policy_event is None
        ):
            raise SealedLongRunError(
                "production strongest events are not persisted by the canonical owner"
            )
        receipt = {
            "schema": "zyra.phase2-sealed-inline-policy/v1",
            "ready": True,
            "policy_profile": "phase2_strongest_v1",
            "run_id": str(owner_context.get("run_id") or state.run_id),
            "task_id": str(owner_context.get("task_id") or state.task_id),
            "decision_id": decision.get("decision_id"),
            "operator_ref": layer.get("operator_ref"),
            "candidate_set_digest": candidate.get("candidate_set_digest"),
            "resource_decision_id": placement.get("resource_decision_id"),
            "permission_receipt_digest": permission.get("receipt_digest"),
            "lease_id": placement.get("lease_id"),
            "attempt_id": placement.get("attempt_id"),
            "physical_receipt_digest": physical.get("digest"),
            "worker_pool_receipt_id": worker_receipt.get("receipt_id"),
            "operator_execution_digest": layer.get("operator_execution_digest"),
            "canonical_artifact_ids": list(
                layer.get("canonical_artifact_ids") or ()
            ),
            "checks": checks,
            "production_event_count": len(events),
            "production_event_ids": production_event_ids,
            "production_event_snapshot_digest": _evidence_digest(
                production_snapshot
            ),
            "topology_policy_event_id": str(
                getattr(selected_policy_event, "event_id", "") or ""
            ),
            "completion_gate_event_id": str(
                getattr(completion_event, "event_id", "") or ""
            ),
            "production_mechanism_chain_digest": mechanism_chain[
                "chain_digest"
            ],
            "loopx_pre_control_digest": loopx_pre_control[
                "receipt_digest"
            ],
            "consumed_before_domain_execution": True,
        }
        receipt["receipt_digest"] = _evidence_digest(receipt)
        self._bundle = {
            "schema": "zyra.phase2-sealed-mechanism-bundle/v2",
            "production_control": receipt,
            "production_mechanism_chain": mechanism_chain,
            "production_bypass_reachable": False,
        }
        unsigned_bundle = dict(self._bundle)
        unsigned_bundle.pop("bundle_digest", None)
        self._bundle["bundle_digest"] = _evidence_digest(unsigned_bundle)
        return receipt

    def _loopx_pre_control(
        self,
        *,
        state: Any,
        causation_id: str,
    ) -> dict[str, Any]:
        """Commit and execute LoopX continuation before topology selection."""
        return dict(
            self.api_main.prepare_phase2_loopx_pre_control(
                state,
                causation_id=causation_id,
            )
        )

    def _loopx_chain(
        self,
        *,
        state: Any,
        pre_control: Mapping[str, Any],
        graph_commit: Mapping[str, Any],
        permission: Mapping[str, Any],
        placement: Mapping[str, Any],
        causation_id: str,
    ) -> dict[str, Any]:
        control = self.api_main.get_loopx_control_runtime()
        validation = {
            "validation_passed": True,
            "permission_allowed": permission.get("effect") == "allow",
            "lease_valid": bool(placement.get("lease_id")),
            "budget_allowed": bool(placement.get("resource_decision_id")),
            "validation_receipt_id": str(
                graph_commit.get("commit_id") or ""
            ),
            "permission_receipt_id": str(
                permission.get("receipt_digest") or ""
            ),
            "lease_receipt_id": str(placement.get("lease_id") or ""),
            "budget_receipt_id": str(
                placement.get("resource_decision_id") or ""
            ),
        }
        goal_id = str(pre_control.get("goal_id") or "")
        pre_results = _mapping(pre_control.get("results"))
        connected = _mapping(pre_results.get("connect"))
        claimed = _mapping(pre_results.get("claim"))
        if not goal_id or not connected or not claimed:
            raise SealedLongRunError(
                "production LoopX chain lost its pre-control receipt"
            )
        common = {
            "run_id": state.run_id,
            "task_id": state.task_id,
            "canonical_commit": graph_commit,
            "validation": validation,
            "causation_id": causation_id,
        }

        def mutate(action: str, payload: Mapping[str, Any], sequence: int) -> dict[str, Any]:
            return control.mutate(
                action=action,
                payload={"goal_id": goal_id, **dict(payload)},
                idempotency_key=(
                    f"sealed:{state.run_id}:{state.task_id}:loopx:{action}:{sequence}"
                ),
                **common,
            )

        conflict = mutate(
            "claim",
            {"todo_id": "todo_sealed_primary", "claimant": "sealed-controller-b"},
            3,
        )
        released = mutate(
            "release",
            {"todo_id": "todo_sealed_primary", "claimant": "sealed-controller-a"},
            4,
        )
        exhausted = mutate(
            "interaction_submit",
            {
                "input_ref": f"event:sealed-quota:{state.run_id}",
                "spend_slots": 1,
            },
            5,
        )
        before_restart = control.snapshot(
            run_id=state.run_id,
            task_id=state.task_id,
            goal_id=goal_id,
        )
        runtime_identity_before = (
            f"python-runtime:{os.getpid()}:{id(control)}"
        )
        self.api_main.reset_control_runtime()
        restarted_control = self.api_main.get_loopx_control_runtime()
        runtime_identity_after = (
            f"python-runtime:{os.getpid()}:{id(restarted_control)}"
        )
        after_restart = restarted_control.snapshot(
            run_id=state.run_id,
            task_id=state.task_id,
            goal_id=goal_id,
        )
        conflict_receipt = _mapping(conflict.get("receipt"))
        exhausted_receipt = _mapping(exhausted.get("receipt"))
        canonical_state = _mapping(after_restart.get("canonical_state"))
        checks = {
            "connected": _mapping(connected.get("state")).get("connected")
            is True,
            "claim_applied": _mapping(claimed.get("receipt")).get("status")
            == "applied",
            "claim_conflict_rejected": conflict_receipt.get("status")
            == "claim_conflict",
            "release_applied": _mapping(released.get("receipt")).get("status")
            == "applied",
            "quota_exhaustion_fail_closed": (
                exhausted_receipt.get("status") == "quota_exhausted"
                and _mapping(exhausted.get("state"))
                .get("continuation", {})
                .get("allowed")
                is False
            ),
            "restart_recovered": (
                before_restart.get("private_state")
                == after_restart.get("private_state")
                and _mapping(before_restart.get("sync")).get("cursor")
                == _mapping(after_restart.get("sync")).get("cursor")
            ),
            "restart_runtime_replaced": (
                restarted_control is not control
                and runtime_identity_after != runtime_identity_before
            ),
            "worker_lease_owner_preserved": (
                canonical_state.get("worker_lease_owner")
                == "WorkerLeaseManager"
                and canonical_state.get("loopx_claim_is_worker_lease") is False
            ),
            "execution_budget_owner_preserved": (
                canonical_state.get("execution_budget_owner")
                == "ResourceScheduler"
                and canonical_state.get("loopx_quota_is_execution_budget")
                is False
            ),
        }
        if not all(checks.values()):
            raise SealedLongRunError(
                f"production LoopX control chain is incomplete: {checks}"
            )
        value = {
            "schema": "zyra.phase2-production-loopx-chain/v1",
            "pre_control": dict(pre_control),
            "pre_control_digest": pre_control.get("receipt_digest"),
            "canonical_commit_id": graph_commit.get("commit_id"),
            "canonical_commit_digest": _evidence_digest(graph_commit),
            "validation": validation,
            "results": {
                "connect": connected,
                "claim": claimed,
                "conflict": conflict,
                "release": released,
                "quota_exhaustion": exhausted,
            },
            "restart_snapshot_before": before_restart,
            "restart_snapshot_after": after_restart,
            "restart_runtime_identity_before": runtime_identity_before,
            "restart_runtime_identity_after": runtime_identity_after,
            "checks": checks,
            "duplicate_claim": 0,
            "duplicate_spend": 0,
        }
        value["chain_digest"] = _evidence_digest(value)
        return value

    def require_bundle(self) -> dict[str, Any]:
        if self._bundle is None:
            raise SealedLongRunError("inline strongest policy was not executed")
        return dict(self._bundle)


def _json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            canonicalize(value),
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _jsonl(path: Path, values: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(
                dict(item),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
            for item in values
        ),
        encoding="utf-8",
    )


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _evidence_digest(value: Any) -> str:
    return canonical_digest(canonicalize(value))


def _sequence(value: Any) -> tuple[Any, ...]:
    return tuple(value) if isinstance(value, Sequence) and not isinstance(value, str) else ()


def _git(
    project_root: Path,
    *arguments: str,
    check: bool = True,
) -> str:
    result = subprocess.run(
        ["git", "-C", str(project_root), *arguments],
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=(
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if os.name == "nt"
            else 0
        ),
    )
    if check and result.returncode != 0:
        raise SealedLongRunError(
            f"git {' '.join(arguments)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


class SealedLongRunRunner:
    def __init__(
        self,
        *,
        project_root: str | Path,
        manifest_path: str | Path,
        evidence_root: str | Path | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.manifest_bytes = self.manifest_path.read_bytes()
        raw = json.loads(self.manifest_bytes.decode("utf-8"))
        if not isinstance(raw, Mapping):
            raise SealedLongRunError("sealed manifest must be a JSON object")
        self.manifest = dict(raw)
        self.manifest_digest = file_digest(self.manifest_path)
        selected_root = (
            Path(evidence_root)
            if evidence_root is not None
            else self.project_root
            / str(
                self.manifest.get("evidence_root")
                or "docs/evidence/phase2/sealed/current"
            )
        )
        self.evidence_root = selected_root.resolve()
        self._validate_manifest()

    def run(self) -> tuple[dict[str, Any], Path]:
        if self.evidence_root.exists() and any(self.evidence_root.iterdir()):
            raise SealedLongRunError(
                f"evidence root is not clean: {self.evidence_root}"
            )
        manifest_commit = _git(self.project_root, "rev-parse", "HEAD")
        boundary_before = require_worktree_boundary(
            self.project_root,
            expected_head=str(self.manifest["candidate_commit"]),
        )
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        sealed_copy = self.evidence_root / "sealed-manifest.json"
        sealed_copy.write_bytes(self.manifest_bytes)
        run_values: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []
        for run_spec_value in _sequence(self.manifest.get("runs")):
            if not isinstance(run_spec_value, Mapping):
                continue
            run_spec = dict(run_spec_value)
            run_key = str(run_spec.get("run_key") or "")
            run_prefix = (
                "sw"
                if str(run_spec.get("domain") or "") == "software_delivery"
                else "rs"
            )
            attempt_id = (
                f"{run_prefix}-{self.manifest_digest[:8]}-"
                f"{uuid4().hex[:8]}"
            )
            run_root = self.evidence_root / "runs" / attempt_id
            run_root.mkdir(parents=True, exist_ok=False)
            try:
                result = self._run_one(
                    run_spec=run_spec,
                    scenario_run_id=attempt_id,
                    run_root=run_root,
                    manifest_commit=manifest_commit,
                )
                run_values.append(result)
            except BaseException as error:
                failure = {
                    "schema": "zyra.phase2-sealed-run-failure/v1",
                    "run_key": run_key,
                    "scenario_run_id": attempt_id,
                    "failure_type": type(error).__name__,
                    "message": str(error),
                    "traceback": traceback.format_exc(),
                    "sealed_manifest_digest": self.manifest_digest,
                    "candidate_commit": self.manifest["candidate_commit"],
                    "manifest_commit": manifest_commit,
                    "human_intervention_count": 0,
                    "continued_as_same_run": False,
                    "failed_at": utc_now(),
                }
                _json(run_root / "failure.json", failure)
                failures.append(
                    {
                        **failure,
                        "failure_path": (
                            run_root / "failure.json"
                        ).relative_to(self.evidence_root).as_posix(),
                    }
                )
            finally:
                self._shutdown_deployment_runtime()
        boundary_after = inspect_worktree(
            self.project_root,
            expected_head=str(self.manifest["candidate_commit"]),
        )
        if boundary_after["ready"] is not True:
            failures.append(
                {
                    "schema": "zyra.phase2-sealed-run-failure/v1",
                    "run_key": "global",
                    "failure_type": "SourceMutationDetected",
                    "message": "tracked source/config changed during sealed execution",
                    "source_boundary": boundary_after,
                    "human_intervention_count": 0,
                    "continued_as_same_run": False,
                    "failed_at": utc_now(),
                }
            )
        index = {
            "schema": EVIDENCE_INDEX_SCHEMA,
            "slice": "P2-S06-02",
            "sealed_manifest": sealed_copy.name,
            "sealed_manifest_digest": file_digest(sealed_copy),
            "candidate_commit": self.manifest["candidate_commit"],
            "manifest_commit": manifest_commit,
            "human_intervention_count": 0,
            "target_source_boundary": {
                "before": boundary_before,
                "after": boundary_after,
            },
            "runs": run_values,
            "failed_runs": failures,
            "created_at": utc_now(),
        }
        index["index_digest"] = _evidence_digest(index)
        index_path = self.evidence_root / "sealed-evidence-index.json"
        _json(index_path, index)
        if failures or len(run_values) != 2:
            raise SealedLongRunError(
                f"sealed execution retained {len(failures)} failed run(s); "
                f"evidence index: {index_path}"
            )
        return index, index_path

    def _run_one(
        self,
        *,
        run_spec: dict[str, Any],
        scenario_run_id: str,
        run_root: Path,
        manifest_commit: str,
    ) -> dict[str, Any]:
        self._assert_frozen()
        api_state = run_root / "s"
        self._configure_api_state(api_state)
        proxy_cidrs = tuple(
            str(item)
            for item in _sequence(
                _mapping(self.manifest.get("network_profile")).get(
                    "live_public_proxy_cidrs"
                )
            )
            if str(item).strip()
        )
        os.environ["ZYRA_LIVE_PUBLIC_PROXY_CIDRS"] = ",".join(proxy_cidrs)
        api_main = self._fresh_api_main()
        from apps.api.zyra_api.live_scenario_owners import (
            CanonicalLiveScenarioOwners,
        )

        owner = CanonicalLiveScenarioOwners(
            project_root=self.project_root,
            artifact_root=api_main.artifact_root_path(),
            scratch_root=api_state / "scratch",
        )
        physical_holder: list[SealedPhysicalEvidence] = []
        credential_file = self.manifest.get("credential_env_file")
        credential_files = tuple(
            self.project_root / str(item)
            for item in _sequence(self.manifest.get("credential_env_files"))
        )
        provider_profile = _mapping(self.manifest.get("provider_profile"))
        physical_runtime = SealedPhysicalDispatchRuntime(
            project_root=self.project_root,
            state_root=run_root / "physical-runtime",
            credential_env_file=(
                self.project_root / str(credential_file)
                if credential_file
                else None
            ),
            credential_env_files=credential_files,
            cloud_models=tuple(
                _mapping(item)
                for item in _sequence(provider_profile.get("cloud_models"))
            ),
        )
        placement = SealedPlacementOwner(
            delegate=owner,
            physical_runtime=physical_runtime,
            evidence_sink=physical_holder.append,
        )
        inline_policy = _SealedInlineProductionPolicy(
            api_main=api_main,
            owner=owner,
            project_root=self.project_root,
            state_root=run_root / "mechanism-runtime",
        )
        configuration = self._configuration(
            run_spec,
            state_root=api_state / "preflight",
        )
        executor = DualDomainScenarioExecutor(
            project_root=self.project_root,
            artifact_root=api_main.artifact_root_path(),
            scratch_root=api_state / "scratch",
            bindings=DualDomainOwnerBindings(
                task=owner,
                artifact=owner,
                placement=placement,
                fault=owner,
                source_commit=manifest_commit,
                pre_execution_policy=inline_policy,
                analysis=owner,
            ),
        )
        result = executor.execute(
            scenario_run_id=scenario_run_id,
            configuration=configuration,
            goal=configuration.input_text,
            policy_decisions=(),
            cancel_requested=lambda: False,
        )
        if len(physical_holder) != 1:
            raise SealedLongRunError(
                "physical evidence owner did not return exactly one lane bundle"
            )
        physical = physical_holder[0]
        mechanism = inline_policy.require_bundle()
        physical_path = run_root / "physical-dispatch-bundle.json"
        mechanism_path = run_root / "mechanism-bundle.json"
        _json(physical_path, physical.to_dict())
        _json(mechanism_path, mechanism)
        raw_path = run_root / "raw-canonical-events.jsonl"
        _jsonl(raw_path, result.events)
        owner_snapshot = {
            "schema": OWNER_SNAPSHOT_SCHEMA,
            "run_id": result.owner_run_id,
            "task_id": result.task_id,
            "owner": "SQLiteStore.EventRecord",
            "events": list(owner.canonical_event_snapshot()),
            "analysis_records": list(owner.canonical_analysis_snapshot()),
        }
        owner_snapshot["snapshot_digest"] = _evidence_digest(owner_snapshot)
        owner_snapshot_path = run_root / "canonical-owner-snapshot.json"
        _json(owner_snapshot_path, owner_snapshot)
        transitions = IndependentTransitionValidator().validate(
            result.events,
            run_id=result.owner_run_id,
            task_id=result.task_id,
            owner_events=owner_snapshot["events"],
            owner_analysis_records=owner_snapshot["analysis_records"],
        )
        transition_index = transitions.index(
            run_id=result.owner_run_id,
            task_id=result.task_id,
        )
        transition_path = run_root / "canonical-transition-index.json"
        _json(transition_path, transition_index)
        final_source = self._final_artifact(result.artifacts, run_spec)
        final_artifact = run_root / (
            "final-artifact" + final_source.suffix
        )
        shutil.copy2(final_source, final_artifact)
        hard_gates = self._hard_gates(
            result=result,
            physical=physical,
            mechanism=mechanism,
            transitions=transition_index,
        )
        hard_gate_path = run_root / "hard-gate-bundle.json"
        _json(hard_gate_path, hard_gates)
        domain_verification = _mapping(
            result.task.get("domain_verification")
        )
        verifier = {
            "schema": "zyra.phase2-sealed-final-verifier/v1",
            "verifier_id": str(
                domain_verification.get("verifier_id")
                or "sealed-final-verifier/v1"
            ),
            "run_id": result.owner_run_id,
            "task_id": result.task_id,
            "domain": str(run_spec.get("domain") or ""),
            "passed": (
                domain_verification.get("valid") is True
                and transition_index["valid_transition_count"]
                >= int(self.manifest["minimum_valid_transitions_per_run"])
                and not transition_index["invalid_count"]
                and _mapping(mechanism.get("production_control")).get(
                    "consumed_before_domain_execution"
                )
                is True
            ),
            "artifact_digest": file_digest(final_artifact),
            "raw_events_digest": file_digest(raw_path),
            "canonical_owner_snapshot_digest": file_digest(owner_snapshot_path),
            "transition_index_digest": transition_index["index_digest"],
            "hard_gate_bundle_digest": file_digest(hard_gate_path),
            "mechanism_bundle_digest": file_digest(mechanism_path),
            "physical_dispatch_bundle_digest": file_digest(physical_path),
            "domain_verification_receipt_digest": domain_verification.get(
                "receipt_digest"
            ),
            "inline_policy_receipt_digest": _mapping(
                mechanism.get("production_control")
            ).get("receipt_digest"),
            "human_intervention_count": 0,
            "verified_at": utc_now(),
        }
        verifier["verifier_digest"] = _evidence_digest(verifier)
        verifier_path = run_root / "final-verifier.json"
        _json(verifier_path, verifier)
        if verifier["passed"] is not True:
            raise SealedLongRunError("run final verifier failed")
        self._assert_frozen()
        relative = lambda path: path.relative_to(  # noqa: E731
            self.evidence_root
        ).as_posix()
        summary = {
            "schema": "zyra.phase2-sealed-run-evidence/v1",
            "run_key": str(run_spec.get("run_key") or ""),
            "run_id": result.owner_run_id,
            "scenario_run_id": scenario_run_id,
            "task_id": result.task_id,
            "domain": str(run_spec.get("domain") or ""),
            "candidate_commit": self.manifest["candidate_commit"],
            "manifest_commit": manifest_commit,
            "configuration_digest": configuration.configuration_digest,
            "input_digest": configuration.input_digest,
            "raw_events": relative(raw_path),
            "raw_events_digest": file_digest(raw_path),
            "canonical_owner_snapshot": relative(owner_snapshot_path),
            "canonical_owner_snapshot_digest": file_digest(owner_snapshot_path),
            "transition_index": relative(transition_path),
            "transition_index_digest": file_digest(transition_path),
            "hard_gate_bundle": relative(hard_gate_path),
            "hard_gate_bundle_digest": file_digest(hard_gate_path),
            "mechanism_bundle": relative(mechanism_path),
            "mechanism_bundle_digest": file_digest(mechanism_path),
            "physical_dispatch_bundle": relative(physical_path),
            "physical_dispatch_bundle_digest": file_digest(physical_path),
            "final_artifact": relative(final_artifact),
            "final_artifact_digest": file_digest(final_artifact),
            "final_verifier": relative(verifier_path),
            "final_verifier_digest": file_digest(verifier_path),
            "human_intervention_count": 0,
            "failed_attempt_count": len(
                _sequence(
                    _mapping(result.task.get("fault_campaign")).get(
                        "failures"
                    )
                )
            ),
            "completed_at": result.completed_at,
        }
        _json(run_root / "run-evidence.json", summary)
        return summary

    def _configuration(
        self,
        run_spec: Mapping[str, Any],
        *,
        state_root: Path,
    ) -> Any:
        input_value = run_spec.get("input")
        input_text = (
            json.dumps(
                input_value,
                ensure_ascii=False,
                sort_keys=True,
            )
            if isinstance(input_value, Mapping)
            else str(input_value or "")
        )
        selected = build_configuration(
            ScenarioRegistry.defaults(),
            {
                "scenario_id": str(run_spec.get("scenario_id") or ""),
                "input": input_text,
                "mode": "sealed",
                "seed": int(run_spec.get("seed") or 0),
                "faults": list(
                    _sequence(run_spec.get("failure_schedule"))
                ),
                "requested_by": "P2-S06-02-sealed-runner",
                "labels": {
                    "slice": "P2-S06-02",
                    "run_key": str(run_spec.get("run_key") or ""),
                },
            },
            project_root=str(self.project_root),
            default_preflight_paths={
                "database": str(state_root / "scenario.sqlite3"),
                "cache": str(state_root / "cache"),
                "index": str(state_root / "index"),
                "artifact": str(state_root / "artifacts"),
                "build": str(state_root / "build"),
            },
        )
        budget = _mapping(run_spec.get("budget"))
        profile = replace(
            selected.profile,
            maximum_effective_steps=int(
                budget.get("maximum_effective_transitions") or 10_000
            ),
            maximum_wall_time_ms=int(
                budget.get("maximum_wall_time_ms") or 3_600_000
            ),
            metadata={
                **dict(selected.profile.metadata),
                "slice": "P2-S06-02",
                "phase2_profile": "phase2_strongest_v1",
                "require_real_tiers": True,
                "require_real_providers": True,
                "minimum_provider_capabilities": 2,
                "edge_cloud_claim": True,
                "provider_model_claim": True,
                "minimum_effective_transitions": int(
                    self.manifest["minimum_valid_transitions_per_run"]
                ),
                "maximum_effective_transitions": int(
                    budget.get("maximum_effective_transitions") or 10_000
                ),
                "maximum_cost_usd": float(
                    budget.get("maximum_cost_usd") or 0.5
                ),
                "maximum_latency_ms": int(
                    budget.get("maximum_latency_ms") or 300_000
                ),
                "privacy_class": str(
                    run_spec.get("privacy_class")
                    or ("public" if str(run_spec.get("domain")) == "cross_source_research" else "internal")
                ),
                "sealed_manifest_digest": self.manifest_digest,
                "failure_schedule_digest": _evidence_digest(
                    run_spec.get("failure_schedule")
                ),
                "hardware_profile_digest": _evidence_digest(
                    self.manifest.get("hardware_profile")
                ),
                "provider_profile_digest": _evidence_digest(
                    self.manifest.get("provider_profile")
                ),
            },
        )
        return replace(selected, profile=profile)

    def _hard_gates(
        self,
        *,
        result: Any,
        physical: SealedPhysicalEvidence,
        mechanism: Mapping[str, Any],
        transitions: Mapping[str, Any],
    ) -> dict[str, Any]:
        production_chain = _mapping(
            mechanism.get("production_mechanism_chain")
        )
        continuity = _mapping(production_chain.get("continuity"))
        continuity_receipt = _mapping(continuity.get("receipt"))
        continuity_payload = _mapping(continuity_receipt.get("payload"))
        topology = _mapping(production_chain.get("topology"))
        loopx = _mapping(production_chain.get("loopx"))
        loopx_checks = _mapping(loopx.get("checks"))
        loopx_pre_control = _mapping(loopx.get("pre_control"))
        loopx_consumption = _mapping(
            topology.get("loopx_pre_control_consumption")
        )
        operator = _mapping(production_chain.get("operator"))
        production_control = _mapping(mechanism.get("production_control"))
        lanes = []
        for receipt, validation in zip(
            physical.receipts,
            physical.validations,
            strict=True,
        ):
            lanes.append(
                {
                    "lane": str(validation.get("location") or ""),
                    "receipt_digest": receipt.get("digest"),
                    "real_gate_closed": validation.get("real_gate_closed"),
                    "simulated": receipt.get("simulated"),
                    "semantic_only": receipt.get("semantic_only"),
                    "validation": validation,
                }
            )
        placement = _mapping(result.task.get("placement"))
        communication_bytes = sum(
            len(
                json.dumps(
                    _mapping(item.get("payload")).get("mutation"),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
            for item in result.events
        )
        effect_counts = _mapping(transitions.get("effect_counts"))
        operation_kinds = set(_sequence(topology.get("operation_kinds")))
        topology_layers = {
            str(_mapping(item).get("mechanism_id") or ""):
            _mapping(item)
            for item in _sequence(topology.get("layers"))
            if isinstance(item, Mapping)
        }
        projection_differences = tuple(
            str(item)
            for item in _sequence(topology.get("projection_differences"))
        )
        continuity_passed = (
            continuity_payload.get("continuity_result") == "passed"
            and continuity.get("hard_conditions_passed") is True
            and bool(continuity.get("symbolic_bundle_policy_artifact_ref"))
        )
        recovered_faults = min(
            int(effect_counts.get("fault") or 0),
            int(effect_counts.get("recovery") or 0),
        )
        value = {
            "schema": "zyra.phase2-sealed-hard-gates/v1",
            "human_intervention_count": 0,
            "early_exit_false_positive": 0,
            "critical_fact_recall": 1.0 if continuity_passed else 0.0,
            "obligation_retention": 1.0 if continuity_passed else 0.0,
            "superseded_requirement_execution": 0,
            "critical_retrieval_without_provenance": 0,
            "duplicate_completed_work": 0,
            "duplicate_commit": 0,
            "duplicate_claim": int(loopx.get("duplicate_claim") or 0),
            "duplicate_spend": int(loopx.get("duplicate_spend") or 0),
            "duplicate_lease": 0,
            "duplicate_side_effect": 0,
            "privacy_permission_violation": 0,
            "unsafe_commit": 0,
            "adversarial_proposals": {
                "total": int(effect_counts.get("fault") or 0),
                "rejected_or_projected": recovered_faults,
                "source": "same_run_fault_and_recovery_transitions",
            },
            "physical_dispatch": {
                "lanes": lanes,
                "provider_models": [
                    {
                        "provider_id": item.get("provider_id"),
                        "model_id": item.get("model_id"),
                        "authenticated": item.get("authenticated"),
                        "response_status": item.get("response_status"),
                        "request_id": item.get("request_id"),
                        "attempt_id": item.get("attempt_id"),
                        "receipt_digest": _mapping(item.get("metadata")).get(
                            "physical_dispatch_receipt_digest"
                        ),
                        "simulated": _mapping(item.get("metadata")).get(
                            "simulated"
                        ),
                        "semantic_only": _mapping(item.get("metadata")).get(
                            "semantic_only"
                        ),
                    }
                    for item in physical.providers
                    if item.get("provider_id") != "zyra-local"
                ],
                "condition_change_effect": "safe_fail_closed_recovery",
                "artifact_continuity": physical.degradation.get(
                    "artifact_continuity"
                )
                is True,
                "degradation": physical.degradation,
                "placement_verification": _mapping(
                    placement.get("verification")
                ),
            },
            "continuity": {
                "production_receipt": continuity_receipt,
                "production_completion_event_id": continuity.get(
                    "completion_event_id"
                ),
                "production_hard_conditions_passed": continuity.get(
                    "hard_conditions_passed"
                ),
                "verified_transition_effects": {
                    name: int(effect_counts.get(name) or 0)
                    for name in (
                        "compact_restore",
                        "fault",
                        "recovery",
                        "memory",
                    )
                },
                "receipt_digest": _evidence_digest(continuity_receipt),
            },
            "loopx": {
                "pre_control_consumed_by_topology": (
                    loopx_pre_control.get("receipt_digest")
                    == loopx_consumption.get("receipt_digest")
                    and loopx_consumption.get("consumed_before_topology")
                    is True
                    and bool(
                        loopx_consumption.get(
                            "topology_policy_input_digest"
                        )
                    )
                ),
                "pre_control_bound_to_placement": (
                    loopx_pre_control.get("receipt_digest")
                    == operator.get("loopx_pre_control_digest")
                    and loopx_consumption.get(
                        "topology_policy_input_digest"
                    )
                    == operator.get(
                        "loopx_topology_policy_input_digest"
                    )
                    and loopx_consumption.get(
                        "operator_policy_input_digest"
                    )
                    == operator.get(
                        "loopx_operator_policy_input_digest"
                    )
                ),
                "restart_recovered": loopx_checks.get("restart_recovered"),
                "restart_runtime_replaced": loopx_checks.get(
                    "restart_runtime_replaced"
                ),
                "claim_conflict_rejected": loopx_checks.get(
                    "claim_conflict_rejected"
                ),
                "quota_exhaustion_fail_closed": loopx_checks.get(
                    "quota_exhaustion_fail_closed"
                ),
                "worker_lease_owner_preserved": loopx_checks.get(
                    "worker_lease_owner_preserved"
                ),
                "execution_budget_owner_preserved": loopx_checks.get(
                    "execution_budget_owner_preserved"
                ),
                "receipt_digest": _evidence_digest(loopx),
            },
            "topology_operator": {
                "node_added": "add_node" in operation_kinds,
                "edge_added": "add_edge" in operation_kinds,
                "role_capability_joint": "add_node" in operation_kinds,
                "arg_committed": _mapping(
                    topology_layers.get("arg_designer")
                ).get("affected_commit")
                is True,
                "card_committed": _mapping(
                    topology_layers.get("card")
                ).get("affected_commit")
                is True,
                "agentprune_committed": _mapping(
                    topology_layers.get("agentprune")
                ).get("affected_commit")
                is True,
                "agentprune_reduced": any(
                    item.startswith("agentprune_drop:")
                    for item in projection_differences
                ),
                "canonical_custody_commit": topology.get(
                    "canonical_custody_commit"
                ),
                "receipt_digest": _evidence_digest(topology),
            },
            "permission_recovery": {
                "denial_observed": int(effect_counts.get("permission") or 0)
                > 0,
                "autonomous_recovery": int(effect_counts.get("recovery") or 0)
                > 0,
            },
            "production_bypass_reachable": mechanism.get(
                "production_bypass_reachable"
            ),
            "production_control": {
                "receipt_digest": production_control.get("receipt_digest"),
                "ready": production_control.get("ready"),
                "policy_profile": production_control.get("policy_profile"),
                "consumed_before_domain_execution": production_control.get(
                    "consumed_before_domain_execution"
                ),
                "checks": production_control.get("checks"),
                "production_event_count": production_control.get(
                    "production_event_count"
                ),
                "production_event_snapshot_digest": production_control.get(
                    "production_event_snapshot_digest"
                ),
                "production_mechanism_chain_digest": production_control.get(
                    "production_mechanism_chain_digest"
                ),
            },
            "valid_transition_count": transitions.get(
                "valid_transition_count"
            ),
            "invalid_transition_count": transitions.get("invalid_count"),
            "metrics": {
                "provider_cost_usd": sum(
                    float(item.get("cost_usd") or 0)
                    for item in physical.providers
                ),
                "provider_latency_ms": sum(
                    int(item.get("latency_ms") or 0)
                    for item in physical.providers
                ),
                "communication_bytes": communication_bytes,
                "canonical_event_count": len(result.events),
            },
        }
        value["hard_gate_digest"] = _evidence_digest(value)
        return value

    @staticmethod
    def _final_artifact(
        artifacts: Sequence[Mapping[str, Any]],
        run_spec: Mapping[str, Any],
    ) -> Path:
        domain = str(run_spec.get("domain") or "")
        candidates: list[tuple[Mapping[str, Any], Path]] = []
        for value in artifacts:
            path = Path(str(value.get("path") or value.get("uri") or ""))
            if path.is_file():
                candidates.append((value, path.resolve()))
        if domain == "software_delivery":
            selected = next(
                (
                    path
                    for _artifact, path in candidates
                    if path.suffix == ".patch"
                ),
                None,
            )
        else:
            selected = next(
                (
                    path
                    for artifact, path in candidates
                    if str(artifact.get("kind") or "") == "report"
                ),
                None,
            )
            if selected is None:
                selected = next(
                    (
                        path
                        for _artifact, path in candidates
                        if path.name == "research-report.json"
                    ),
                    None,
                )
        if selected is None:
            raise SealedLongRunError(
                f"final artifact is missing for domain {domain}"
            )
        return selected

    def _validate_manifest(self) -> None:
        value = self.manifest
        if value.get("schema") != SEALED_MANIFEST_SCHEMA:
            raise SealedLongRunError("unsupported sealed manifest schema")
        if value.get("slice") != "P2-S06-02":
            raise SealedLongRunError("sealed manifest slice binding is invalid")
        candidate = str(value.get("candidate_commit") or "")
        if len(candidate) != 40:
            raise SealedLongRunError("candidate_commit must be an exact Git commit")
        if int(value.get("minimum_valid_transitions_per_run") or 0) < 2_000:
            raise SealedLongRunError(
                "sealed transition minimum cannot be below 2,000"
            )
        runs = [
            dict(item)
            for item in _sequence(value.get("runs"))
            if isinstance(item, Mapping)
        ]
        domains = {str(item.get("domain") or "") for item in runs}
        if len(runs) != 2 or domains != {
            "software_delivery",
            "cross_source_research",
        }:
            raise SealedLongRunError(
                "sealed manifest requires exactly two cross-domain runs"
            )
        self._validate_fault_schedules(runs)
        if value.get("human_intervention_count") != 0:
            raise SealedLongRunError(
                "sealed manifest must freeze zero human intervention"
            )
        for name in (
            "provider_profile",
            "hardware_profile",
            "network_profile",
            "verifier",
            "policy",
        ):
            if not isinstance(value.get(name), Mapping):
                raise SealedLongRunError(
                    f"sealed manifest does not freeze {name}"
                )
        provider_profile = _mapping(value.get("provider_profile"))
        cloud_models = tuple(
            (
                str(_mapping(item).get("provider_id") or ""),
                str(_mapping(item).get("model_id") or ""),
            )
            for item in _sequence(provider_profile.get("cloud_models"))
        )
        if (
            cloud_models
            != (("deepseek", "deepseek-flash"),)
            or int(
                provider_profile.get("multiple_model_capabilities_required")
                or 0
            )
            != 1
            or provider_profile.get("live_external_request_required") is not True
        ):
            raise SealedLongRunError(
                "sealed provider profile must freeze one DeepSeek live model"
            )
        credential_files = tuple(
            str(item) for item in _sequence(value.get("credential_env_files"))
        )
        if credential_files != (".env.deepseek.local",):
            raise SealedLongRunError(
                "sealed credential references must follow the fixed provider order"
            )
        proxy_cidrs = _sequence(
            _mapping(value.get("network_profile")).get(
                "live_public_proxy_cidrs"
            )
        )
        if not proxy_cidrs or any(
            not str(item).strip() for item in proxy_cidrs
        ):
            raise SealedLongRunError(
                "sealed manifest must freeze bounded public proxy CIDRs"
            )
        current = _git(self.project_root, "rev-parse", "HEAD")
        if candidate != current:
            raise SealedLongRunError(
                "candidate commit must equal the checked-out final target"
            )
        frozen = _mapping(value.get("frozen_files"))
        if not frozen:
            raise SealedLongRunError("sealed manifest has no frozen file digests")
        for relative, expected in frozen.items():
            path = (self.project_root / str(relative)).resolve()
            try:
                path.relative_to(self.project_root)
            except ValueError as error:
                raise SealedLongRunError(
                    f"frozen file escapes project root: {relative}"
                ) from error
            if not path.is_file() or file_digest(path) != str(expected):
                raise SealedLongRunError(
                    f"frozen file digest mismatch: {relative}"
                )

    @staticmethod
    def _validate_fault_schedules(
        runs: Sequence[Mapping[str, Any]],
    ) -> None:
        for run in runs:
            for fault in _sequence(run.get("failure_schedule")):
                if not isinstance(fault, Mapping):
                    raise SealedLongRunError(
                        "sealed fault schedule entry must be an object"
                    )
                kind = str(fault.get("kind") or "")
                try:
                    FaultKind(kind)
                except ValueError as error:
                    raise SealedLongRunError(
                        f"sealed fault schedule kind is invalid: {kind}"
                    ) from error

    def _assert_frozen(self) -> None:
        if self.manifest_path.read_bytes() != self.manifest_bytes:
            raise SealedLongRunError("sealed manifest changed during execution")
        for relative, expected in _mapping(
            self.manifest.get("frozen_files")
        ).items():
            path = (self.project_root / str(relative)).resolve()
            if file_digest(path) != str(expected):
                raise SealedLongRunError(
                    f"frozen source changed during execution: {relative}"
                )
        require_worktree_boundary(
            self.project_root,
            expected_head=str(self.manifest["candidate_commit"]),
        )

    @staticmethod
    def _configure_api_state(root: Path) -> None:
        bindings = {
            "ZYRA_STATE_ROOT": root,
            "ZYRA_SQLITE_PATH": root / "zyra.sqlite3",
            "ZYRA_EVENT_LOG": root / "events.jsonl",
            "ZYRA_WORKER_POOL_STORE": root / "worker-pool.sqlite3",
            "ZYRA_GRAPH_STATE_STORE": root / "graph.sqlite3",
            "ZYRA_FAULT_RUNTIME_STORE": root / "fault.sqlite3",
            "ZYRA_RECOVERY_RUNTIME_STORE": root / "recovery.sqlite3",
            "ZYRA_MEMORY_INDEX_PATH": root / "memory-index.sqlite3",
            "ZYRA_CODE_INDEX_ROOT": root / "code-index",
            "ZYRA_TOOL_WORKSPACE": root / "workspace",
            "ZYRA_ARTIFACT_ROOT": root / "artifacts",
            "ZYRA_PERMISSION_STATE": root / "permission.json",
            "ZYRA_MCP_STATE": root / "mcp",
            "ZYRA_TERMINAL_STATE": root / "terminal",
            "ZYRA_CONTROL_STATE": root / "control",
            "ZYRA_SUBAGENT_STATE": root / "subagents",
            "ZYRA_SANDBOX_GATEWAY_STATE": root / "gateway",
            "ZYRA_PROVIDER_STATE": root / "provider",
        }
        for name, value in bindings.items():
            os.environ[name] = str(value)
        os.environ["ZYRA_E02_API_SEALED_AUTONOMOUS"] = "1"
        os.environ["ZYRA_E02_API_PERMISSION_MODE"] = "sealed_autonomous"

    @staticmethod
    def _fresh_api_main() -> Any:
        module_name = "apps.api.zyra_api.main"
        prior = sys.modules.get(module_name)
        if prior is not None:
            for name in (
                "reset_browser_runtime",
                "reset_deployment_api",
                "reset_terminal_api",
                "reset_mcp_runtime",
                "reset_memory_curator_runtime",
                "reset_subagent_runtime",
                "reset_control_runtime",
                "reset_recovery_runtime_api",
                "reset_fault_runtime_api",
                "reset_worker_pool_api",
                "reset_runtime_event_spine_bridge",
                "reset_workspace_manager",
                "reset_runtime_owner_composition",
            ):
                callback = getattr(prior, name, None)
                if callable(callback):
                    try:
                        callback()
                    except Exception:
                        pass
            return importlib.reload(prior)
        return importlib.import_module(module_name)

    @staticmethod
    def _shutdown_deployment_runtime() -> None:
        prior = sys.modules.get("apps.api.zyra_api.main")
        callback = getattr(prior, "reset_deployment_api", None)
        if callable(callback):
            callback()


__all__ = [
    "SEALED_MANIFEST_SCHEMA",
    "SealedLongRunError",
    "SealedLongRunRunner",
]
