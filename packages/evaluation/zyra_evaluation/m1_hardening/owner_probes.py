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
from .integration_scenarios import ScenarioExecutionContext
from .owner_matrix import REQUIRED_DISABLE_CAPABILITIES
from .scenario import HttpScenarioTransport, ScenarioTransport


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
    _environment_lock = threading.RLock()

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
    method: str
    path: str
    payload: Mapping[str, Any]
    success_statuses: tuple[int, ...]
    owner_paths: tuple[str, ...] = ()
    error_paths: tuple[str, ...] = ("error", "worker_result.error", "code")
    fallback_paths: tuple[str, ...] = (
        "fallback",
        "python_fallback",
        "worker_result.metadata.fallback",
    )


class HttpOwnerExercise:
    def __init__(self, transport: ScenarioTransport, spec: HttpExerciseSpec) -> None:
        self.transport = transport
        self.spec = spec

    def __call__(self) -> Mapping[str, Any]:
        if self.spec.method.upper() == "GET":
            status, response = self.transport.get(self.spec.path, self.spec.payload)
        else:
            status, response = self.transport.post(self.spec.path, self.spec.payload)
        error = self._first(response, self.spec.error_paths)
        fallback = self._first(response, self.spec.fallback_paths)
        owner = self._first(response, self.spec.owner_paths)
        ok = status in self.spec.success_statuses
        if error and status >= 400:
            ok = False
        return {
            "ok": ok,
            "status": status,
            "error": str(error or ""),
            "canonical_owner": str(owner or ""),
            "fallback": bool(fallback),
            "response_digest": stable_digest(response),
            "semantic": self._semantic_projection(response),
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
    def _semantic_projection(value: Mapping[str, Any]) -> Mapping[str, Any]:
        allowed = {
            "ok",
            "status",
            "state",
            "error",
            "code",
            "canonical_entrypoint",
            "python_fallback",
            "worker_result",
            "revision",
            "snapshotHash",
            "event_count",
            "route",
            "backend",
        }
        return {key: item for key, item in value.items() if key in allowed}


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
        contract = self.catalog.contract(requirement.probe_id)
        exercise = self._scenario_exercise(requirement, context)
        probe = EnvironmentOwnerDisconnectProbe(
            contract,
            exercise,
            reset_registry=self.reset_registry,
        )
        suite = DisableModuleProbe()
        suite.register(probe)
        gate = suite.evaluate(
            required_capabilities=(requirement.capability,),
            selected_probe_ids=(requirement.probe_id,),
            execute=True,
        )
        executions = gate.metrics.get("executions") or []
        if not executions:
            return {
                "probe_id": requirement.probe_id,
                "capability": requirement.capability,
                "status": "failed",
                "error_code": "probe_execution_missing",
                "expected_failure_observed": False,
                "fallback_masked": False,
            }
        receipt = dict(executions[0])
        if requirement.expected_effect is ExpectedEffect.MATERIAL_DIFFERENCE:
            difference = receipt.get("difference") if isinstance(receipt.get("difference"), Mapping) else {}
            receipt["material_difference"] = bool(difference.get("semantic_change"))
        return receipt

    @staticmethod
    def _scenario_exercise(
        requirement: DisconnectRequirement,
        context: ScenarioExecutionContext,
    ) -> Exercise:
        successful_steps = [step for step in context.steps if step.ok and step.method in {"GET", "POST"}]
        preferred = [
            step
            for step in successful_steps
            if requirement.capability.replace("-", "") in step.step_id.replace("-", "")
        ]
        step = (preferred or successful_steps)[-1] if successful_steps else None
        if step is None:
            raise DisableProbeBlocked(f"scenario has no real step to exercise {requirement.capability}")
        spec = HttpExerciseSpec(
            method=step.method,
            path=step.path,
            payload={},
            success_statuses=(200, 201, 202, 204, 409),
            owner_paths=(
                "canonical_entrypoint",
                "worker_result.metadata.canonical_runtime_owner",
                "metadata.canonical_owner",
            ),
        )
        return HttpOwnerExercise(context.transport, spec)


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
            expected_error_codes=("sandbox_gateway_disabled", "gateway_unavailable"),
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
            environment_flags=("ZYRA_GRAPH_STATE_STORE_DISABLED",),
            expected_error_codes=("graph_custody_disabled",),
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
