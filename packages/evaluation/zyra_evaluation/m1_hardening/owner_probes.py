from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .disable import DisableModuleProbe, DisableProbeBlocked, ProbeHandle
from .integration_contracts import DisconnectRequirement, ExpectedEffect, stable_digest, utc_now
from .integration_scenarios import (
    IntegrationScenarioExecutor,
    ScenarioExecutionContext,
)
from .owner_matrix import REQUIRED_DISABLE_CAPABILITIES
from .scenario import HttpScenarioTransport, ScenarioTransport, ScenarioTransportError


Exercise = Callable[[], Mapping[str, Any]]
Reset = Callable[[], None]


@dataclass(frozen=True, slots=True)
class OwnerDisableContract:
    probe_id: str
    capability: str
    environment_flags: tuple[str, ...]
    expected_error_codes: tuple[str, ...]
    allow_success_with_difference: bool = False
    dependencies: tuple[str, ...] = ()
    timeout_seconds: float = 45.0
    reset_components: tuple[str, ...] = ()
    process_scope: str = "api-process"

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        if self.capability not in REQUIRED_DISABLE_CAPABILITIES:
            issues.append(f"capability is outside the M1 final matrix: {self.capability}")
        if self.probe_id != f"disable-{self.capability}":
            if not (self.capability == "graph-custody" and self.probe_id == "disable-graph-state-store"):
                issues.append("probe id does not match canonical capability")
        if not self.environment_flags:
            issues.append("no production disable flag is bound")
        if any(not flag.startswith("ZYRA_") for flag in self.environment_flags):
            issues.append("disable flags must be explicitly Zyra scoped")
        if not self.expected_error_codes and not self.allow_success_with_difference:
            issues.append("probe has neither expected error nor material-difference contract")
        if self.timeout_seconds <= 0:
            issues.append("probe timeout must be positive")
        return tuple(issues)


@dataclass(slots=True)
class EnvironmentSnapshot:
    values: dict[str, str | None]
    captured_at: str
    process_id: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "flags": sorted(self.values),
            "present_flags": sorted(key for key, value in self.values.items() if value is not None),
            "captured_at": self.captured_at,
            "process_id": self.process_id,
            "value_digest": stable_digest({key: value is not None for key, value in self.values.items()}),
        }


class RuntimeResetRegistry:
    def __init__(self) -> None:
        self._resets: dict[str, Reset] = {}
        self._lock = threading.RLock()

    def register(self, component: str, reset: Reset) -> None:
        name = component.strip()
        if not name:
            raise ValueError("reset component name is required")
        if not callable(reset):
            raise TypeError("reset callback must be callable")
        with self._lock:
            if name in self._resets:
                raise ValueError(f"duplicate runtime reset: {name}")
            self._resets[name] = reset

    def reset(self, components: Sequence[str]) -> dict[str, Any]:
        receipts: list[dict[str, Any]] = []
        with self._lock:
            for component in components:
                callback = self._resets.get(component)
                if callback is None:
                    receipts.append({"component": component, "ok": False, "error": "reset_not_registered"})
                    continue
                started = time.monotonic()
                try:
                    callback()
                except Exception as error:
                    receipts.append(
                        {
                            "component": component,
                            "ok": False,
                            "error": type(error).__name__,
                            "message": str(error),
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
                else:
                    receipts.append(
                        {
                            "component": component,
                            "ok": True,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        }
                    )
        return {
            "ok": all(item["ok"] for item in receipts),
            "components": receipts,
        }

    def registered_components(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._resets))


class EnvironmentOwnerDisconnectProbe:
    # DisableProbeRunner enforces timeouts by invoking capture/disable/restore in
    # separate worker threads.  RLock ownership is thread-affine and therefore
    # cannot safely span those stages.  A plain Lock still serializes process
    # environment mutation and may be released by the restore stage.
    _environment_lock = threading.Lock()

    def __init__(
        self,
        contract: OwnerDisableContract,
        exercise: Exercise,
        *,
        reset_registry: RuntimeResetRegistry | None = None,
    ) -> None:
        issues = contract.validate()
        if issues:
            raise ValueError("invalid owner disable contract: " + "; ".join(issues))
        self.contract = contract
        self.exercise_fn = exercise
        self.reset_registry = reset_registry or RuntimeResetRegistry()
        self.probe_id = contract.probe_id
        self.capability = contract.capability
        self.dependencies = contract.dependencies
        self.timeout_seconds = contract.timeout_seconds
        self.expected_error_codes = contract.expected_error_codes
        self.allow_success_with_difference = contract.allow_success_with_difference
        self._lock_acquired = False

    def capture(self) -> Mapping[str, Any]:
        if not self._environment_lock.acquire(timeout=max(1.0, self.timeout_seconds)):
            raise DisableProbeBlocked("could not acquire process environment probe lock")
        self._lock_acquired = True
        snapshot = EnvironmentSnapshot(
            values={flag: os.environ.get(flag) for flag in self.contract.environment_flags},
            captured_at=utc_now(),
            process_id=os.getpid(),
        )
        return {
            "environment": snapshot.to_dict(),
            "raw_values": dict(snapshot.values),
            "registered_resets": list(self.reset_registry.registered_components()),
            "process_scope": self.contract.process_scope,
        }

    def exercise(self) -> Mapping[str, Any]:
        value = self.exercise_fn()
        if not isinstance(value, Mapping):
            raise TypeError(f"owner exercise returned {type(value).__name__}, expected mapping")
        normalized = dict(value)
        normalized.setdefault("canonical_owner", self.capability)
        normalized.setdefault("fallback", False)
        return normalized

    def disable(self) -> Mapping[str, Any]:
        if not self._lock_acquired:
            raise RuntimeError("environment probe was not captured before disable")
        for flag in self.contract.environment_flags:
            os.environ[flag] = "1"
        reset = self.reset_registry.reset(self.contract.reset_components)
        if self.contract.reset_components and not reset["ok"]:
            raise RuntimeError("runtime reset failed after disable: " + json.dumps(reset, sort_keys=True))
        return {
            "ok": True,
            "probe_id": self.probe_id,
            "capability": self.capability,
            "flags": list(self.contract.environment_flags),
            "reset": reset,
            "fallback_enabled": False,
            "disabled_at": utc_now(),
        }

    def restore(self, captured: Mapping[str, Any]) -> Mapping[str, Any]:
        try:
            raw = captured.get("raw_values") if isinstance(captured.get("raw_values"), Mapping) else {}
            for flag in self.contract.environment_flags:
                previous = raw.get(flag)
                if previous is None:
                    os.environ.pop(flag, None)
                else:
                    os.environ[flag] = str(previous)
            reset = self.reset_registry.reset(self.contract.reset_components)
            if self.contract.reset_components and not reset["ok"]:
                raise RuntimeError("runtime reset failed after restore: " + json.dumps(reset, sort_keys=True))
            return {
                "ok": True,
                "probe_id": self.probe_id,
                "restored_flags": list(self.contract.environment_flags),
                "reset": reset,
                "restored_at": utc_now(),
            }
        finally:
            if self._lock_acquired:
                self._lock_acquired = False
                self._environment_lock.release()


@dataclass(frozen=True, slots=True)
class HttpExerciseSpec:
    probe_id: str
    method: str
    path: str
    payload: Mapping[str, Any]
    headers: Mapping[str, str]
    success_statuses: tuple[int, ...]
    owner_paths: tuple[str, ...] = ()
    error_paths: tuple[str, ...] = ("error", "worker_result.error", "code")
    fallback_paths: tuple[str, ...] = (
        "fallback",
        "python_fallback",
        "worker_result.metadata.fallback",
    )
    failure_error_codes: tuple[str, ...] = ()


class HttpOwnerExercise:
    def __init__(self, transport: ScenarioTransport, spec: HttpExerciseSpec) -> None:
        self.transport = transport
        self.spec = spec
        self._call_index = 0

    def __call__(self) -> Mapping[str, Any]:
        self._call_index += 1
        payload = dict(self.spec.payload)
        if self.spec.method.upper() != "GET":
            unique = f"{self.spec.probe_id}-{self._call_index}"
            if self.spec.path.endswith("/workers/code"):
                payload.pop("session_custody_token", None)
                payload["session_id"] = f"m1-owner-probe:{unique}"
            if "idempotency_key" in payload:
                payload["idempotency_key"] = unique
            if "request_id" in payload:
                payload["request_id"] = unique
        if self.spec.method.upper() == "GET":
            status, response = self.transport.get(
                self.spec.path,
                payload,
                headers=self.spec.headers,
            )
        else:
            status, response = self.transport.post(
                self.spec.path,
                payload,
                headers=self.spec.headers,
            )
        return self.project(response, status=status, spec=self.spec)

    @classmethod
    def project(
        cls,
        response: Mapping[str, Any],
        *,
        status: int,
        spec: HttpExerciseSpec,
    ) -> Mapping[str, Any]:
        error = cls._first(response, spec.error_paths)
        if isinstance(error, Mapping):
            error = error.get("code") or error.get("kind") or error.get("message")
        fallback = cls._first(response, spec.fallback_paths)
        owner = cls._first(response, spec.owner_paths)
        ok = status in spec.success_statuses and str(error or "") not in spec.failure_error_codes
        return {
            "ok": ok,
            "status": status,
            "error": str(error or ""),
            "error_detail": str(response.get("message") or ""),
            "canonical_owner": str(owner or ""),
            "fallback": cls._as_bool(fallback),
            "response_digest": stable_digest(response),
            "semantic": (
                cls._fanout_semantic_projection(response)
                if spec.path.endswith("/subagents/fanout")
                else cls._semantic_projection(response)
            ),
        }

    @staticmethod
    def _first(value: Mapping[str, Any], paths: Sequence[str]) -> Any:
        for path in paths:
            current: Any = value
            found = True
            for token in path.split("."):
                if isinstance(current, Mapping) and token in current:
                    current = current[token]
                else:
                    found = False
                    break
            if found:
                return current
        return None

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    @staticmethod
    def _semantic_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
        allowed = {
            "ok",
            "status",
            "state",
            "error",
            "code",
            "canonical_entrypoint",
            "python_fallback",
            "revision",
            "snapshotHash",
            "event_count",
            "route",
            "backend",
        }
        projected = {key: item for key, item in value.items() if key in allowed}
        worker_result = value.get("worker_result")
        if isinstance(worker_result, Mapping):
            # Worker results can contain complete event streams and artifacts.
            # Preserve only bounded semantic leaves so probe evidence remains
            # reviewable and task/session identifiers cannot manufacture a
            # material-difference pass.
            projected["worker_result"] = {
                "signals": HttpOwnerExercise._semantic_signals(worker_result),
            }
        return projected

    @staticmethod
    def _fanout_semantic_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
        """Project stable fanout facts, excluding asynchronous child status."""

        workers = value.get("physical_workers")
        physical: list[dict[str, Any]] = []
        if isinstance(workers, Sequence) and not isinstance(
            workers, (str, bytes, bytearray)
        ):
            for item in workers:
                if not isinstance(item, Mapping):
                    continue
                gateway = item.get("edge_gateway_receipt")
                physical.append(
                    {
                        "location": str(item.get("worker_location") or ""),
                        "gateway_accepted": (
                            bool(gateway.get("accepted"))
                            if isinstance(gateway, Mapping)
                            else None
                        ),
                        "artifact_returned": (
                            bool(gateway.get("artifact_refs"))
                            if isinstance(gateway, Mapping)
                            else None
                        ),
                    }
                )
        return {
            "ok": bool(value.get("ok")),
            "error": str(value.get("error") or ""),
            "canonical_owner": str(
                value.get("canonical_owner")
                or value.get("canonical_entrypoint")
                or ""
            ),
            "physical_workers": sorted(
                physical,
                key=lambda item: (
                    item["location"],
                    str(item["gateway_accepted"]),
                    str(item["artifact_returned"]),
                ),
            ),
        }

    @staticmethod
    def _semantic_signals(value: Mapping[str, Any]) -> list[dict[str, Any]]:
        meaningful = {
            "action",
            "canonical_owner",
            "canonical_runtime_owner",
            "code",
            "decision",
            "error",
            "error_code",
            "fallback",
            "kind",
            "phase",
            "reason",
            "state",
            "status",
            "stopped_reason",
        }
        volatile = {
            "binding_id",
            "event_id",
            "lease_id",
            "request_id",
            "run_id",
            "session_id",
            "span_id",
            "task_id",
            "trace_id",
            "turn_id",
            "worker_id",
        }
        signals: list[dict[str, Any]] = []

        def visit(item: Any, path: str, depth: int) -> None:
            if depth > 8 or len(signals) >= 96:
                return
            if isinstance(item, Mapping):
                for key, child in item.items():
                    token = str(key)
                    lowered = token.lower()
                    child_path = f"{path}.{token}" if path else token
                    if lowered in volatile:
                        continue
                    if lowered in meaningful and isinstance(child, (str, int, float, bool, type(None))):
                        signals.append({"path": child_path, "value": child})
                    elif isinstance(child, (Mapping, list, tuple)):
                        visit(child, child_path, depth + 1)
            elif isinstance(item, (list, tuple)):
                for index, child in enumerate(item[:64]):
                    visit(child, f"{path}[{index}]", depth + 1)

        visit(value, "", 0)
        return signals


class ScenarioPrefixOwnerExercise:
    """Recreate scenario preconditions for every probe phase on a fresh task."""

    def __init__(
        self,
        context: ScenarioExecutionContext,
        requirement: DisconnectRequirement,
        spec: HttpExerciseSpec,
    ) -> None:
        self.context = context
        self.requirement = requirement
        self.spec = spec

    def __call__(self) -> Mapping[str, Any]:
        fresh = ScenarioExecutionContext(
            definition=self.context.definition,
            transport=self.context.transport,
            options=self.context.options,
        )
        fresh.bindings.update(
            {
                "actor_id": self.context.options.actor_id,
                "scenario_id": self.context.definition.scenario_id,
                "scenario_kind": self.context.definition.kind.value,
            }
        )
        executor = IntegrationScenarioExecutor(self.context.transport)
        result: dict[str, Any] | None = None
        try:
            for request in self.context.definition.requests:
                executor._execute_request(fresh, request)
                step = fresh.steps[-1]
                if request.step_id == self.requirement.exercise_step_id:
                    result = dict(
                        HttpOwnerExercise.project(
                            step.response,
                            status=step.status,
                            spec=self.spec,
                        )
                    )
                    break
                if step.error and not request.optional:
                    result = dict(
                        HttpOwnerExercise.project(
                            step.response,
                            status=step.status,
                            spec=HttpExerciseSpec(
                                probe_id=self.spec.probe_id,
                                method=step.method,
                                path=step.path,
                                payload={},
                                headers={},
                                success_statuses=self.spec.success_statuses,
                                owner_paths=self.spec.owner_paths,
                                error_paths=self.spec.error_paths,
                                fallback_paths=self.spec.fallback_paths,
                                failure_error_codes=self.spec.failure_error_codes,
                            ),
                        )
                    )
                    break
            if result is None:
                raise DisableProbeBlocked(
                    f"scenario prefix did not reach {self.requirement.exercise_step_id}"
                )
            return result
        finally:
            task_id = str(fresh.bindings.get("task_id") or "")
            if task_id:
                # POST /tasks reserves a physical worker lease even when the
                # task is intentionally left pending.  Probe prefixes are
                # disposable, so release their lease through the production
                # cancellation path or a complete owner matrix exhausts the
                # worker pool and produces order-dependent false failures.
                try:
                    target_response = fresh.steps[-1].response if fresh.steps else {}
                    physical_workers = target_response.get("physical_workers")
                    if isinstance(physical_workers, Sequence) and not isinstance(
                        physical_workers, (str, bytes, bytearray)
                    ):
                        for item in physical_workers:
                            if not isinstance(item, Mapping):
                                continue
                            self.context.transport.post(
                                f"/tasks/{task_id}/worker-pool-control",
                                {
                                    "kind": "cancel",
                                    "reason": "M1 owner disconnect child probe completed",
                                    "lease_id": str(item.get("lease_id") or ""),
                                    "binding_id": str(
                                        item.get("integration_binding_id")
                                        or item.get("binding_id")
                                        or ""
                                    ),
                                    "worker_id": str(item.get("worker_id") or ""),
                                    "idempotency_key": (
                                        f"{self.spec.probe_id}:{task_id}:"
                                        f"{item.get('lease_id') or item.get('binding_id') or 'child'}:cleanup"
                                    ),
                                },
                                headers={},
                            )
                    self.context.transport.post(
                        f"/tasks/{task_id}/worker-pool-cancel",
                        {
                            "reason": "M1 owner disconnect probe prefix completed",
                            "idempotency_key": f"{self.spec.probe_id}:{task_id}:cleanup",
                        },
                        headers={},
                    )
                except (ScenarioTransportError, OSError, TimeoutError):
                    # Cleanup failure must not replace the owner behavior.  A
                    # later probe will expose lease exhaustion fail-closed.
                    pass


class OwnerProbeCatalog:
    def __init__(self, contracts: Sequence[OwnerDisableContract] | None = None) -> None:
        values = tuple(contracts or default_owner_disable_contracts())
        self._contracts = {item.probe_id: item for item in values}
        if len(self._contracts) != len(values):
            raise ValueError("duplicate owner disable contract")

    def contracts(self) -> tuple[OwnerDisableContract, ...]:
        return tuple(self._contracts.values())

    def probe_ids(self) -> tuple[str, ...]:
        return tuple(self._contracts)

    def capabilities(self) -> tuple[str, ...]:
        return tuple(sorted({item.capability for item in self._contracts.values()}))

    def contract(self, probe_id: str) -> OwnerDisableContract:
        try:
            return self._contracts[probe_id]
        except KeyError as error:
            raise KeyError(f"unknown owner disable probe: {probe_id}") from error

    def validate(self) -> tuple[str, ...]:
        issues: list[str] = []
        for contract in self._contracts.values():
            issues.extend(f"{contract.probe_id}: {issue}" for issue in contract.validate())
            for dependency in contract.dependencies:
                if dependency not in self._contracts:
                    issues.append(f"{contract.probe_id}: missing dependency {dependency}")
        missing = set(REQUIRED_DISABLE_CAPABILITIES) - set(self.capabilities())
        issues.extend(f"missing required capability: {capability}" for capability in sorted(missing))
        return tuple(issues)

    def build_suite(
        self,
        exercises: Mapping[str, Exercise],
        *,
        reset_registry: RuntimeResetRegistry | None = None,
        include_probe_ids: Sequence[str] = (),
        existing: Sequence[ProbeHandle] = (),
    ) -> DisableModuleProbe:
        selected = set(include_probe_ids) if include_probe_ids else set(self._contracts)
        unknown = selected - set(self._contracts)
        if unknown:
            raise ValueError("unknown owner probes: " + ", ".join(sorted(unknown)))
        suite = DisableModuleProbe()
        registered: set[str] = set()
        for probe in existing:
            suite.register(probe)
            registered.add(probe.probe_id)
        for probe_id in self._topological_order(selected):
            if probe_id in registered:
                continue
            contract = self._contracts[probe_id]
            exercise = exercises.get(contract.capability)
            if exercise is None:
                continue
            suite.register(
                EnvironmentOwnerDisconnectProbe(
                    contract,
                    exercise,
                    reset_registry=reset_registry,
                )
            )
        return suite

    def _topological_order(self, selected: set[str]) -> tuple[str, ...]:
        temporary: set[str] = set()
        permanent: set[str] = set()
        order: list[str] = []

        def visit(probe_id: str) -> None:
            if probe_id in permanent:
                return
            if probe_id in temporary:
                raise ValueError(f"owner probe dependency cycle at {probe_id}")
            temporary.add(probe_id)
            for dependency in self._contracts[probe_id].dependencies:
                if dependency in selected:
                    visit(dependency)
            temporary.remove(probe_id)
            permanent.add(probe_id)
            order.append(probe_id)

        for probe_id in sorted(selected):
            visit(probe_id)
        return tuple(order)


class ScenarioDisconnectCoordinator:
    def __init__(
        self,
        catalog: OwnerProbeCatalog,
        *,
        reset_registry: RuntimeResetRegistry | None = None,
    ) -> None:
        self.catalog = catalog
        self.reset_registry = reset_registry or RuntimeResetRegistry()

    def execute(
        self,
        requirement: DisconnectRequirement,
        context: ScenarioExecutionContext,
    ) -> Mapping[str, Any]:
        if requirement.capability == "edge-worker":
            step = next(
                (
                    item
                    for item in context.steps
                    if item.step_id == requirement.exercise_step_id and item.ok
                ),
                None,
            )
            workers = (
                step.response.get("physical_workers")
                if step is not None and isinstance(step.response, Mapping)
                else ()
            )
            locations = (
                sorted(
                    {
                        str(item.get("worker_location") or "")
                        for item in workers
                        if isinstance(item, Mapping)
                    }
                    - {""}
                )
                if isinstance(workers, Sequence)
                and not isinstance(workers, (str, bytes, bytearray))
                else []
            )
            if "edge" not in locations:
                return {
                    "probe_id": requirement.probe_id,
                    "capability": requirement.capability,
                    "status": "blocked",
                    "error_code": "edge_live_dispatch_unavailable",
                    "error_message": (
                        "scenario exercise did not dispatch through an isolated edge worker; "
                        f"observed locations={locations or ['unreported']}"
                    ),
                    "expected_failure_observed": False,
                    "fallback_masked": False,
                    "observed_worker_locations": locations,
                    "limitation": "real isolated edge dispatch remains an M1 exit blocker",
                }
        before_isolation = self._reset_probe_worker_pool()
        if not before_isolation["ok"]:
            return {
                "probe_id": requirement.probe_id,
                "capability": requirement.capability,
                "status": "failed",
                "error_code": "probe_isolation_reset_failed",
                "error_message": "worker-pool reset failed before owner disconnect",
                "expected_failure_observed": False,
                "fallback_masked": False,
                "isolation": {"before": before_isolation},
            }
        contract = self.catalog.contract(requirement.probe_id)
        exercise = self._scenario_exercise(requirement, context)
        probe = EnvironmentOwnerDisconnectProbe(
            contract,
            exercise,
            reset_registry=self.reset_registry,
        )
        suite = DisableModuleProbe()
        suite.register(probe)
        after_isolation: Mapping[str, Any] = {}
        try:
            gate = suite.evaluate(
                required_capabilities=(requirement.capability,),
                selected_probe_ids=(requirement.probe_id,),
                execute=True,
            )
        finally:
            after_isolation = self._reset_probe_worker_pool()
        executions = gate.metrics.get("executions") or []
        if not executions:
            return {
                "probe_id": requirement.probe_id,
                "capability": requirement.capability,
                "status": "failed",
                "error_code": "probe_execution_missing",
                "expected_failure_observed": False,
                "fallback_masked": False,
                "isolation": {
                    "before": before_isolation,
                    "after": after_isolation,
                },
            }
        receipt = dict(executions[0])
        receipt["isolation"] = {
            "before": before_isolation,
            "after": after_isolation,
        }
        if not after_isolation["ok"]:
            receipt.update(
                {
                    "status": "failed",
                    "error_code": "probe_isolation_cleanup_failed",
                    "error_message": "worker-pool reset failed after owner disconnect",
                    "expected_failure_observed": False,
                }
            )
        if requirement.expected_effect is ExpectedEffect.MATERIAL_DIFFERENCE:
            difference = receipt.get("difference") if isinstance(receipt.get("difference"), Mapping) else {}
            receipt["material_difference"] = bool(difference.get("semantic_change"))
        return receipt

    def _reset_probe_worker_pool(self) -> Mapping[str, Any]:
        if "worker-pool" not in self.reset_registry.registered_components():
            return {
                "ok": True,
                "skipped": True,
                "reason": "worker-pool reset is not registered in this process",
                "components": [],
            }
        receipt = self.reset_registry.reset(("worker-pool",))
        return {
            **receipt,
            "skipped": False,
        }

    @staticmethod
    def _scenario_exercise(
        requirement: DisconnectRequirement,
        context: ScenarioExecutionContext,
    ) -> Exercise:
        request = next(
            (
                item
                for item in context.definition.requests
                if item.step_id == requirement.exercise_step_id
            ),
            None,
        )
        step = next(
            (
                item
                for item in context.steps
                if item.step_id == requirement.exercise_step_id and item.ok
            ),
            None,
        )
        if request is None:
            raise DisableProbeBlocked(
                f"scenario does not define exercise step {requirement.exercise_step_id}"
            )
        if step is None:
            raise DisableProbeBlocked(
                f"scenario did not successfully execute {requirement.exercise_step_id} "
                f"for {requirement.capability}"
            )
        spec = HttpExerciseSpec(
            probe_id=requirement.probe_id,
            method=request.method,
            path=step.path,
            payload=request.payload,
            headers=request.headers,
            success_statuses=request.expected_statuses,
            owner_paths=(
                "canonical_entrypoint",
                "state_owner",
                "worker_result.metadata.canonical_runtime_owner",
                "metadata.canonical_owner",
            ),
            failure_error_codes=tuple(requirement.expected_errors),
        )
        # Read-only owner surfaces are safe to replay against the task or
        # process that the scenario already proved reachable.  Recreating the
        # entire scenario prefix while the owner is disabled can fail in an
        # unrelated prerequisite (for example task-event persistence), which
        # masks the exact owner-disconnect behavior under review.
        if request.method.upper() == "GET":
            return HttpOwnerExercise(context.transport, spec)
        return ScenarioPrefixOwnerExercise(context, requirement, spec)


def default_owner_disable_contracts() -> tuple[OwnerDisableContract, ...]:
    return (
        OwnerDisableContract(
            probe_id="disable-query-session",
            capability="query-session",
            environment_flags=("ZYRA_DISABLE_E04_QUERY_SOURCE_RUNTIME",),
            expected_error_codes=("e04_query_source_runtime_disabled", "query_session_disabled"),
            reset_components=("codeworker",),
        ),
        OwnerDisableContract(
            probe_id="disable-tool-loop",
            capability="tool-loop",
            environment_flags=("ZYRA_DISABLE_E04_TOOL_SOURCE_RUNTIME",),
            expected_error_codes=("e04_tool_source_runtime_disabled", "tool_loop_foundation_disabled"),
            dependencies=("disable-query-session",),
            reset_components=("codeworker",),
        ),
        OwnerDisableContract(
            probe_id="disable-mcp-runtime",
            capability="mcp-runtime",
            environment_flags=("ZYRA_DISABLE_E04_MCP_SOURCE_RUNTIME",),
            expected_error_codes=("mcp_source_runtime_disabled", "mcp_runtime_disabled"),
            dependencies=("disable-permission-runtime",),
            reset_components=("mcp",),
        ),
        OwnerDisableContract(
            probe_id="disable-permission-runtime",
            capability="permission-runtime",
            environment_flags=("ZYRA_DISABLE_E04_PERMISSION_SOURCE_RUNTIME",),
            expected_error_codes=("permission_source_runtime_disabled", "permission_runtime_disabled"),
            dependencies=("disable-tool-loop",),
            reset_components=("mcp", "codeworker"),
        ),
        OwnerDisableContract(
            probe_id="disable-workspace-runtime",
            capability="workspace-runtime",
            environment_flags=("ZYRA_WORKSPACE_INTEGRATION_DISABLED",),
            expected_error_codes=("workspace_backend_disabled", "workspace_runtime_disabled"),
            dependencies=("disable-permission-runtime",),
            reset_components=("workspace",),
        ),
        OwnerDisableContract(
            probe_id="disable-sandbox-gateway",
            capability="sandbox-gateway",
            environment_flags=("ZYRA_SANDBOX_GATEWAY_DISABLED",),
            expected_error_codes=("sandbox_gateway_disabled",),
            allow_success_with_difference=True,
            dependencies=("disable-workspace-runtime",),
            reset_components=("sandbox-gateway",),
        ),
        OwnerDisableContract(
            probe_id="disable-runtime-event-spine",
            capability="runtime-event-spine",
            environment_flags=("ZYRA_RUNTIME_EVENT_SPINE_DISABLED",),
            expected_error_codes=("runtime_event_spine_disabled", "event_owner_disabled"),
            reset_components=("runtime-event-spine",),
        ),
        OwnerDisableContract(
            probe_id="disable-provider-control-plane",
            capability="provider-control-plane",
            environment_flags=("ZYRA_PROVIDER_CONTROL_PLANE_DISABLED",),
            expected_error_codes=("provider_control_plane_disabled", "provider_owner_disabled"),
            dependencies=("disable-runtime-event-spine",),
            reset_components=("provider-control-plane", "codeworker"),
        ),
        OwnerDisableContract(
            probe_id="disable-memory-retrieval",
            capability="memory-retrieval",
            environment_flags=("ZYRA_RETRIEVAL_INDEX_DISABLED",),
            expected_error_codes=("retrieval_index_disabled",),
            allow_success_with_difference=True,
            dependencies=("disable-runtime-event-spine",),
            reset_components=("memory",),
        ),
        OwnerDisableContract(
            probe_id="disable-code-index",
            capability="code-index",
            environment_flags=("ZYRA_CODE_INDEX_DISABLED",),
            expected_error_codes=("code_index_disabled",),
            allow_success_with_difference=True,
            dependencies=("disable-workspace-runtime",),
            reset_components=("code-index",),
        ),
        OwnerDisableContract(
            probe_id="disable-memory-curator",
            capability="memory-curator",
            environment_flags=("ZYRA_MEMORY_CURATOR_DISABLED",),
            expected_error_codes=("memory_curator_disabled", "curator_runtime_disabled"),
            dependencies=("disable-memory-retrieval",),
            reset_components=("memory",),
        ),
        OwnerDisableContract(
            probe_id="disable-skill-memory-restore",
            capability="skill-memory-restore",
            environment_flags=(
                "ZYRA_DISABLE_SKILL_MEMORY_RUNTIME",
                "ZYRA_DISABLE_COMPACT_RESTORE_MEMORY_BRIDGE",
            ),
            expected_error_codes=("skill_memory_runtime_disabled", "compact_restore_memory_bridge_disabled"),
            allow_success_with_difference=True,
            dependencies=("disable-memory-curator",),
            reset_components=("codeworker", "skill-memory"),
        ),
        OwnerDisableContract(
            probe_id="disable-physical-worker",
            capability="physical-worker",
            environment_flags=("ZYRA_WORKER_POOL_INTEGRATION_DISABLED",),
            expected_error_codes=("worker_pool_integration_disabled", "physical_worker_disabled"),
            dependencies=("disable-runtime-event-spine",),
            reset_components=("worker-pool",),
        ),
        OwnerDisableContract(
            probe_id="disable-edge-worker",
            capability="edge-worker",
            environment_flags=("ZYRA_EDGE_POOL_DISABLED",),
            expected_error_codes=("edge_connector_disabled", "edge_worker_disabled"),
            dependencies=("disable-physical-worker",),
            reset_components=("worker-pool",),
        ),
        OwnerDisableContract(
            probe_id="disable-watchdog",
            capability="watchdog",
            environment_flags=("ZYRA_WATCHDOG_RUNTIME_DISABLED",),
            expected_error_codes=("watchdog_runtime_disabled",),
            allow_success_with_difference=True,
            dependencies=("disable-physical-worker", "disable-runtime-event-spine"),
            reset_components=("fault-runtime",),
        ),
        OwnerDisableContract(
            probe_id="disable-checkpoint-recovery",
            capability="checkpoint-recovery",
            environment_flags=("ZYRA_DISABLE_RECOVERY_RUNTIME",),
            expected_error_codes=("recovery_runtime_disabled", "checkpoint_recovery_disabled"),
            dependencies=("disable-watchdog", "disable-graph-state-store"),
            reset_components=("recovery",),
        ),
        OwnerDisableContract(
            probe_id="disable-layered-route",
            capability="layered-route",
            environment_flags=("ZYRA_LAYERED_SCHEDULER_DISABLED",),
            expected_error_codes=("layered_scheduler_disabled",),
            allow_success_with_difference=True,
            dependencies=("disable-physical-worker", "disable-provider-control-plane"),
            reset_components=("scheduler",),
        ),
        OwnerDisableContract(
            probe_id="disable-graph-state-store",
            capability="graph-custody",
            environment_flags=("ZYRA_DYNAMIC_GRAPH_COMMIT_DISABLED",),
            expected_error_codes=("dynamic_graph_commit_disabled", "graph_custody_disabled"),
            dependencies=("disable-runtime-event-spine",),
            reset_components=("graph-custody",),
        ),
    )


def write_probe_receipt(path: str | Path, receipt: Mapping[str, Any]) -> Path:
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": "zyra.m1-owner-disable-receipt/v1",
        "written_at": utc_now(),
        "receipt": dict(receipt),
    }
    payload["content_digest"] = stable_digest(payload)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target
