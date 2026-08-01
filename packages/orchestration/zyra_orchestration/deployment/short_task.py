from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from .errors import SemanticProbeFailed
from .http_client import ProductHttpClient
from .models import digest, new_id, now_iso
from .state_store import DeploymentStateStore


class DeploymentExercise(Protocol):
    def __call__(self, *, task_id: str, run_id: str) -> Mapping[str, Any]: ...


class ShortTaskVerifier:
    def __init__(
        self,
        *,
        api: ProductHttpClient,
        store: DeploymentStateStore,
        deployment_exercise: DeploymentExercise,
    ) -> None:
        self.api = api
        self.store = store
        self.deployment_exercise = deployment_exercise

    def run(
        self,
        *,
        goal: str = "",
        sealed: bool = True,
    ) -> dict[str, Any]:
        started_at = now_iso()
        started = time.monotonic()
        task_request_id = new_id("semantic_task")
        selected_goal = goal or (
            "Produce a concise deployment readiness artifact, preserve the "
            "result in memory, and verify it through the default task graph."
        )
        response = self.api.post(
            "/tasks",
            {
                "goal": selected_goal,
                "auto_run": True,
                "sealed": sealed,
                "sealed_autonomous": sealed,
                "competition_mode": "sealed_autonomous" if sealed else "interactive",
                "session_id": f"deployment-health:{task_request_id}",
                "idempotency_key": task_request_id,
            },
            accepted_statuses=(201,),
            idempotency_key=task_request_id,
            timeout_seconds=90.0,
        )
        task = response.get("task")
        events = response.get("events")
        if not isinstance(task, Mapping) or not isinstance(events, list):
            raise SemanticProbeFailed(
                "semantic_short_task_response_invalid",
                "canonical API task response lacks task or events",
                operation="short_task",
            )
        task_id = str(task.get("task_id") or "")
        run_id = str(task.get("run_id") or "")
        if not task_id or not run_id:
            raise SemanticProbeFailed(
                "semantic_short_task_identity_missing",
                "canonical API task response identity is incomplete",
                operation="short_task",
            )
        assertions: list[dict[str, Any]] = []
        self._assert(
            assertions,
            "task_completed",
            str(task.get("status") or "") == "completed",
            {"status": str(task.get("status") or "")},
        )
        self._assert(
            assertions,
            "sealed_mode",
            (
                not sealed
                or (
                    (task.get("metadata") or {}).get("sealed_autonomous") is True
                    and (task.get("metadata") or {}).get("competition_mode")
                    == "sealed_autonomous"
                )
            ),
            {
                "sealed_autonomous": (task.get("metadata") or {}).get(
                    "sealed_autonomous"
                ),
                "competition_mode": (task.get("metadata") or {}).get(
                    "competition_mode"
                ),
            },
        )
        normalized_events = [
            dict(item) for item in events if isinstance(item, Mapping)
        ]
        event_ids = [str(item.get("event_id") or "") for item in normalized_events]
        event_types = Counter(
            str(item.get("event_type") or "") for item in normalized_events
        )
        self._assert(
            assertions,
            "event_identity_unique",
            bool(event_ids)
            and all(event_ids)
            and len(event_ids) == len(set(event_ids)),
            {
                "event_count": len(event_ids),
                "unique_event_count": len(set(event_ids)),
            },
        )
        semantic_types = {
            "task_created",
            "node_created",
            "node_updated",
            "artifact_written",
            "resource_decision",
            "constraint_check",
            "topology_route",
            "worker_health",
            "evaluation",
        }
        semantic_event_count = sum(
            count
            for event_type, count in event_types.items()
            if event_type in semantic_types
        )
        self._assert(
            assertions,
            "semantic_events_present",
            semantic_event_count >= 3,
            {
                "semantic_event_count": semantic_event_count,
                "event_types": dict(event_types),
            },
        )
        projections = self._collect_projections(task_id)
        self._validate_projections(
            assertions,
            task=task,
            events=normalized_events,
            projections=projections,
        )
        deployment = dict(
            self.deployment_exercise(task_id=task_id, run_id=run_id)
        )
        self._validate_deployment(assertions, deployment)
        failed = [
            item["assertion_id"]
            for item in assertions
            if item["accepted"] is not True
        ]
        completed_at = now_iso()
        receipt = {
            "schema": "zyra.semantic-short-task/v1",
            "task_id": task_id,
            "run_id": run_id,
            "goal_digest": digest(selected_goal),
            "sealed": sealed,
            "human_intervention_count": 0,
            "task_status": str(task.get("status") or ""),
            "event_count": len(normalized_events),
            "semantic_event_count": semantic_event_count,
            "event_type_counts": dict(event_types),
            "artifact_count": len(task.get("artifacts") or ()),
            "assertions": assertions,
            "failed_assertions": failed,
            "accepted": not failed,
            "projections": projections,
            "deployment": deployment,
            "started_at": started_at,
            "completed_at": completed_at,
            "latency_ms": round((time.monotonic() - started) * 1000),
            "fresh_input": True,
            "fixture": False,
            "replay": False,
            "fallback": False,
        }
        receipt["receipt_digest"] = digest(receipt)
        self.store.append_event(
            "deployment.short_task_verified",
            {
                "task_id": task_id,
                "run_id": run_id,
                "accepted": not failed,
                "failed_assertions": failed,
                "event_count": len(normalized_events),
                "artifact_count": len(task.get("artifacts") or ()),
                "receipt_digest": receipt["receipt_digest"],
            },
            task_id=task_id,
            run_id=run_id,
            correlation_id=task_request_id,
        )
        if failed:
            raise SemanticProbeFailed(
                "semantic_short_task_failed",
                "short canonical task failed semantic assertions",
                operation="short_task",
                details={
                    "task_id": task_id,
                    "failed_assertions": failed,
                    "receipt": receipt,
                },
            )
        return receipt

    def _collect_projections(self, task_id: str) -> dict[str, Any]:
        routes = {
            "task": f"/tasks/{task_id}",
            "events": f"/tasks/{task_id}/events",
            "artifacts": f"/tasks/{task_id}/artifacts",
            "memory": f"/tasks/{task_id}/memory",
            "compactions": f"/tasks/{task_id}/compactions",
            "scheduler": f"/tasks/{task_id}/scheduler",
            "codeworker_session": f"/tasks/{task_id}/workers/code/session",
            "codeworker_tool_trace": f"/tasks/{task_id}/workers/code/tool-trace",
            "codeworker_compact": f"/tasks/{task_id}/workers/code/compact-state",
            "browser_observability": f"/tasks/{task_id}/browser-observability",
            "event_capabilities": f"/tasks/{task_id}/event-ingress/capabilities",
            "event_snapshot": f"/tasks/{task_id}/event-ingress/snapshot",
            "recovery": f"/tasks/{task_id}/recovery",
            "runtime_readiness": "/runtime/readiness",
        }
        projections: dict[str, Any] = {}
        for name, path in routes.items():
            try:
                projections[name] = {
                    "status": "observed",
                    "body": self.api.get(path),
                }
            except BaseException as error:
                projections[name] = {
                    "status": "unavailable",
                    "error": f"{type(error).__name__}: {error}",
                }
        return projections

    def _validate_projections(
        self,
        assertions: list[dict[str, Any]],
        *,
        task: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        projections: Mapping[str, Any],
    ) -> None:
        artifact_body = self._body(projections, "artifacts")
        artifacts = artifact_body.get("artifacts")
        task_artifacts = task.get("artifacts")
        artifact_count = (
            len(artifacts)
            if isinstance(artifacts, list)
            else len(task_artifacts)
            if isinstance(task_artifacts, list)
            else 0
        )
        self._assert(
            assertions,
            "artifact_store_effect",
            artifact_count > 0,
            {"artifact_count": artifact_count},
        )
        memory = self._body(projections, "memory")
        memory_count = sum(
            len(value)
            for value in memory.values()
            if isinstance(value, list)
        )
        self._assert(
            assertions,
            "memory_projection_observed",
            projections.get("memory", {}).get("status") == "observed"
            and bool(memory),
            {"memory_collection_count": memory_count},
        )
        scheduler = self._body(projections, "scheduler")
        self._assert(
            assertions,
            "scheduler_projection_observed",
            projections.get("scheduler", {}).get("status") == "observed"
            and bool(scheduler),
            {
                "keys": sorted(scheduler)[:50],
                "projection_digest": digest(scheduler),
            },
        )
        metadata = task.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        physical_receipts = metadata.get("physical_dispatch_receipts")
        physical_receipts = (
            physical_receipts if isinstance(physical_receipts, list) else []
        )
        physical_receipt = (
            physical_receipts[-1]
            if physical_receipts
            and isinstance(physical_receipts[-1], Mapping)
            else {}
        )
        physical_payload = physical_receipt.get("payload")
        physical_payload = (
            physical_payload if isinstance(physical_payload, Mapping) else {}
        )
        input_signals = physical_payload.get("input_signals")
        input_signals = (
            input_signals if isinstance(input_signals, Mapping) else {}
        )
        placement = metadata.get("operator_placement_binding")
        placement = placement if isinstance(placement, Mapping) else {}
        worker_receipt = metadata.get("worker_pool_receipt")
        worker_receipt = (
            worker_receipt if isinstance(worker_receipt, Mapping) else {}
        )
        self._assert(
            assertions,
            "physical_operator_route_contract",
            (
                physical_receipt.get("schema_version")
                == "zyra.physical-dispatch-receipt/v2"
                and bool(physical_receipt.get("digest"))
                and worker_receipt.get("outcome") == "succeeded"
                and physical_payload.get("simulated") is False
                and physical_payload.get("semantic_only") is False
                and input_signals.get("workload_operation")
                == "phase2-operator-execution"
                and input_signals.get("domain_effect_performed") is True
                and input_signals.get("output_contract_fulfilled") is True
                and physical_payload.get("lease_id")
                == placement.get("lease_id")
                and physical_payload.get("physical_attempt_id")
                == placement.get("attempt_id")
            ),
            {
                "physical_receipt_digest": physical_receipt.get("digest"),
                "outcome": worker_receipt.get("outcome"),
                "lease_id": physical_payload.get("lease_id"),
                "physical_attempt_id": physical_payload.get(
                    "physical_attempt_id"
                ),
                "operator_ref": input_signals.get("operator_ref"),
                "legacy_codeworker_projection_status": projections.get(
                    "codeworker_session", {}
                ).get("status"),
            },
        )
        event_capabilities = self._body(projections, "event_capabilities")
        event_snapshot = self._body(projections, "event_snapshot")
        self._assert(
            assertions,
            "event_reconnect_contract",
            (
                projections.get("event_capabilities", {}).get("status")
                == "observed"
                and projections.get("event_snapshot", {}).get("status")
                == "observed"
                and bool(event_capabilities)
                and bool(event_snapshot)
            ),
            {
                "capabilities_digest": digest(event_capabilities),
                "snapshot_digest": digest(event_snapshot),
            },
        )
        compactions = self._body(projections, "compactions")
        self._assert(
            assertions,
            "checkpoint_or_compact_projection",
            (
                projections.get("codeworker_compact", {}).get("status")
                == "observed"
                or (
                    projections.get("compactions", {}).get("status")
                    == "observed"
                    and isinstance(compactions.get("compactions"), list)
                )
            ),
            {
                "compaction_count": len(compactions.get("compactions") or ()),
                "codeworker_compact_status": projections.get(
                    "codeworker_compact", {}
                ).get("status"),
            },
        )
        browser = self._body(projections, "browser_observability")
        self._assert(
            assertions,
            "browser_worker_projection",
            projections.get("browser_observability", {}).get("status")
            == "observed"
            and "browser_observability" in browser,
            {"projection_digest": digest(browser)},
        )
        recovery = self._body(projections, "recovery")
        runtime_readiness = self._body(projections, "runtime_readiness")
        canonical = runtime_readiness.get("canonical_owners")
        domains = (
            canonical.get("domains")
            if isinstance(canonical, Mapping)
            else ()
        )
        recovery_owner = next(
            (
                item
                for item in domains or ()
                if isinstance(item, Mapping)
                and item.get("domain") == "scheduler_recovery"
            ),
            {},
        )
        route_observed = (
            projections.get("recovery", {}).get("status") == "observed"
            and bool(recovery)
        )
        owner_observed = (
            projections.get("runtime_readiness", {}).get("status") == "observed"
            and isinstance(recovery_owner, Mapping)
            and recovery_owner.get("ready") is True
            and recovery_owner.get("fallback_active") is not True
        )
        self._assert(
            assertions,
            "recovery_projection_observed",
            route_observed or owner_observed,
            {
                "route_observed": route_observed,
                "owner_observed": owner_observed,
                "projection_digest": digest(recovery),
                "owner_digest": digest(recovery_owner),
            },
        )

    def _validate_deployment(
        self,
        assertions: list[dict[str, Any]],
        deployment: Mapping[str, Any],
    ) -> None:
        routes = deployment.get("routes")
        failures = deployment.get("failure_recovery")
        self._assert(
            assertions,
            "three_profile_execution",
            isinstance(routes, list)
            and {
                str(item.get("profile") or "")
                for item in routes
                if isinstance(item, Mapping)
            }
            == {"device", "edge", "cloud"},
            {"routes": routes if isinstance(routes, list) else []},
        )
        self._assert(
            assertions,
            "placement_changes_execution",
            deployment.get("placement_binding_verified") is True,
            {
                "placement_binding_verified": deployment.get(
                    "placement_binding_verified"
                )
            },
        )
        self._assert(
            assertions,
            "failure_migration_and_checkpoint",
            isinstance(failures, Mapping)
            and failures.get("migrated") is True
            and failures.get("checkpoint_handoff_verified") is True,
            dict(failures) if isinstance(failures, Mapping) else {},
        )
        self._assert(
            assertions,
            "profile_process_isolation",
            deployment.get("independent_processes") is True,
            {
                "independent_processes": deployment.get(
                    "independent_processes"
                ),
                "process_ids": deployment.get("process_ids"),
            },
        )

    @staticmethod
    def _body(
        projections: Mapping[str, Any],
        key: str,
    ) -> dict[str, Any]:
        item = projections.get(key)
        if not isinstance(item, Mapping):
            return {}
        body = item.get("body")
        return dict(body) if isinstance(body, Mapping) else {}

    @staticmethod
    def _assert(
        target: list[dict[str, Any]],
        assertion_id: str,
        accepted: bool,
        observation: Mapping[str, Any],
    ) -> None:
        target.append(
            {
                "assertion_id": assertion_id,
                "accepted": bool(accepted),
                "observation": dict(observation),
                "asserted_at": now_iso(),
            }
        )


__all__ = [
    "DeploymentExercise",
    "ShortTaskVerifier",
]
