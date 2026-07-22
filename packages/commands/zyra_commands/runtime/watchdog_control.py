from __future__ import annotations

import json
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zyra_core import TaskState
from zyra_scheduler.fault_runtime import FaultRuntimeApiService


@dataclass(frozen=True, slots=True)
class WatchdogCommandReceipt:
    command: str
    action: str
    task_id: str
    run_id: str
    ok: bool
    data: Mapping[str, Any]
    mutations: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.watchdog-control-command-receipt/v1",
            "command": self.command,
            "action": self.action,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "ok": self.ok,
            "data": dict(self.data),
            "mutations": list(self.mutations),
            "warnings": list(self.warnings),
            "canonical_fault_owner": "python.FaultStateStore",
            "recovery_plan_owner": "M1-S07C",
        }


class WatchdogControlCommandRuntime:
    """Durable command handler behind TypeScript-owned slash parsing.

    ``/inject`` and ``/watchdog`` enter the same application object used by
    HTTP and worker integrations. This module parses only the already-bounded
    action arguments produced by the canonical command layer; it does not
    become a shadow command registry, permission evaluator or recovery
    planner.
    """

    ACTIONS = frozenset(
        {
            "status",
            "tick",
            "bind",
            "observe",
            "runtime-event",
            "dispatch",
            "disable",
            "enable",
        }
    )

    def __init__(
        self,
        api: FaultRuntimeApiService,
        state: TaskState,
        *,
        actor_id: str,
    ) -> None:
        if not actor_id.strip():
            raise ValueError("watchdog control actor_id must not be empty")
        self.api = api
        self.state = state
        self.actor_id = actor_id

    def dispatch(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> WatchdogCommandReceipt:
        try:
            normalized = command.strip().lower()
            if normalized == "/inject":
                return self.inject(arguments, idempotency_key=idempotency_key)
            if normalized == "/watchdog":
                return self.watchdog(arguments)
            raise ValueError(f"unsupported watchdog control command: {command}")
        finally:
            self.api.release_idle_connection()

    def inject(
        self,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> WatchdogCommandReceipt:
        raw = str(arguments.get("raw") or "").strip()
        receipt = self.api.command_inject(
            raw,
            state=self.state,
            requested_by=self.actor_id,
            idempotency_key=idempotency_key,
        )
        signal = dict(receipt.get("signal") or {})
        projection = dict(receipt.get("projection") or {})
        handoff = dict(receipt.get("handoff") or {})
        mutations = [
            "FaultStateStore.injection",
            "FaultStateStore.signal",
        ]
        if projection.get("canonical_event_written"):
            mutations.append("canonical_event_store")
        if projection.get("memory_record_ids"):
            mutations.append("MemoryFabric")
        if projection.get("scheduler_changed"):
            mutations.append("BackendRegistryStore")
        if handoff:
            mutations.append("WatchdogRecoveryBridge.outbox")
        return WatchdogCommandReceipt(
            command="/inject",
            action=str(receipt.get("request", {}).get("kind") or "inject"),
            task_id=self.state.task_id,
            run_id=self.state.run_id,
            ok=bool(receipt.get("ok")),
            data={
                **dict(receipt),
                "signal_id": str(signal.get("signal_id") or ""),
                "event_id": str(projection.get("event_id") or ""),
                "handoff_id": str(handoff.get("handoff_id") or ""),
                "same_run": True,
            },
            mutations=tuple(mutations),
        )

    def watchdog(self, arguments: Mapping[str, Any]) -> WatchdogCommandReceipt:
        action, payload = self._action_payload(arguments)
        if action == "status":
            data = dict(self.api.runtime.snapshot(task_id=self.state.task_id))
            mutations: tuple[str, ...] = ()
        elif action == "tick":
            result = self.api.runtime.integration.tick(
                task_id=self.state.task_id,
                at_ms=(int(payload["at_ms"]) if payload.get("at_ms") is not None else None),
            )
            data = result.to_dict()
            mutations = self._mutations_from_tick(result.body)
        elif action == "bind":
            data = self.api.runtime.integration.bind_source(self.state, payload).to_dict()
            mutations = ("RuntimeSourceSessionManager",)
        elif action == "observe":
            data = self.api.runtime.integration.observe_source(self.state, payload).to_dict()
            mutations = self._projection_mutations()
        elif action == "runtime-event":
            if payload.get("event_type"):
                data = self.api.runtime.integration.observations.ingest(self.state, payload).to_dict()
            else:
                data = self.api.runtime.integration.ingest_typescript_event(self.state, payload).to_dict()
            mutations = self._projection_mutations()
        elif action == "dispatch":
            result = self.api.runtime.integration.dispatch_handoffs(
                self._identity(payload, "consumer_id"),
                task_id=self.state.task_id,
                limit=self._bounded_int(payload.get("limit"), default=20, minimum=1, maximum=100),
                now_ms=(int(payload["now_ms"]) if payload.get("now_ms") is not None else None),
            )
            data = result.to_dict()
            mutations = ("WatchdogRecoveryBridge.delivery",)
        elif action in {"disable", "enable"}:
            observer_id = self._identity(payload, "observer_id")
            if action == "disable":
                observer = self.api.runtime.watchdog.disable_observer(
                    observer_id,
                    reason=str(payload.get("reason") or "disabled through /watchdog"),
                )
            else:
                observer = self.api.runtime.watchdog.enable_observer(observer_id)
            data = {
                "schema": "zyra.watchdog-control-command-observer/v1",
                "observer": observer,
                "injection_observer_changed": False,
                "capture_path_removed": action == "disable",
                "injection_fallback_started": False,
            }
            mutations = ("WatchdogObserverRegistry",)
        else:
            raise ValueError(f"unsupported /watchdog action: {action}")
        return WatchdogCommandReceipt(
            command="/watchdog",
            action=action,
            task_id=self.state.task_id,
            run_id=self.state.run_id,
            ok=True,
            data=data,
            mutations=mutations,
        )

    def _action_payload(self, arguments: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        explicit = str(arguments.get("action") or "").strip().lower()
        raw = str(arguments.get("raw") or "").strip()
        tokens = self._tokens(raw)
        action = explicit or (tokens[0].lower() if tokens else "status")
        if action not in self.ACTIONS:
            raise ValueError(f"unknown /watchdog action: {action}")
        payload: dict[str, Any] = {}
        raw_payload = arguments.get("payload")
        if raw_payload is not None:
            if not isinstance(raw_payload, Mapping):
                raise ValueError("/watchdog payload must be an object")
            payload.update(dict(raw_payload))
        for key, value in arguments.items():
            if key not in {"raw", "argv", "action", "payload"}:
                payload[str(key)] = value
        for token in tokens[1:] if not explicit else tokens:
            if "=" not in token:
                if token.startswith("{"):
                    decoded = json.loads(token)
                    if not isinstance(decoded, Mapping):
                        raise ValueError("/watchdog JSON token must be an object")
                    payload.update(dict(decoded))
                    continue
                raise ValueError(f"/watchdog values require name=value syntax: {token}")
            key, raw_value = token.split("=", 1)
            normalized_key = key.strip().lower().replace("-", "_")
            if not normalized_key:
                raise ValueError("/watchdog argument key must not be empty")
            payload[normalized_key] = self._coerce(raw_value)
        payload.setdefault("run_id", self.state.run_id)
        payload.setdefault("task_id", self.state.task_id)
        payload.setdefault("actor_id", self.actor_id)
        return action, payload

    @staticmethod
    def _tokens(raw: str) -> list[str]:
        if not raw:
            return []
        try:
            return shlex.split(raw, posix=True)
        except ValueError as error:
            raise ValueError(f"invalid /watchdog syntax: {error}") from error

    @staticmethod
    def _coerce(value: str) -> Any:
        selected = value.strip()
        lowered = selected.lower()
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if lowered == "null":
            return None
        if selected.startswith("{") or selected.startswith("["):
            return json.loads(selected)
        try:
            return int(selected)
        except ValueError:
            return selected

    def _projection_mutations(self) -> tuple[str, ...]:
        snapshot = self.api.runtime.store.snapshot(task_id=self.state.task_id)
        signals = list(snapshot.get("signals") or [])
        if not signals:
            return ("FaultStateStore.observation",)
        latest = dict(signals[-1])
        receipt = self.api.runtime.store.projection_receipt(str(latest.get("signal_id") or ""))
        values = ["FaultStateStore.observation", "FaultStateStore.signal"]
        if receipt is not None:
            if receipt.canonical_event_written:
                values.append("canonical_event_store")
            if receipt.memory_record_ids:
                values.append("MemoryFabric")
            if receipt.scheduler_changed:
                values.append("BackendRegistryStore")
        return tuple(values)

    @staticmethod
    def _mutations_from_tick(body: Mapping[str, Any]) -> tuple[str, ...]:
        mutations = ["RuntimeSourceSessionManager"]
        if body.get("new_signal_ids"):
            mutations.extend(
                [
                    "FaultStateStore.signal",
                    "canonical_event_store",
                    "MemoryFabric",
                    "BackendRegistryStore",
                    "WatchdogRecoveryBridge.outbox",
                ]
            )
        return tuple(mutations)

    @staticmethod
    def _identity(payload: Mapping[str, Any], name: str) -> str:
        value = str(payload.get(name) or "").strip()
        if not value:
            raise ValueError(f"/watchdog requires {name}")
        return value

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        selected = default if value is None else int(value)
        if not minimum <= selected <= maximum:
            raise ValueError(f"integer must be in range {minimum}..{maximum}")
        return selected


def watchdog_control_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.watchdog-control-command-contract/v1",
        "parser_owner": "typescript command runtime",
        "durable_handler": "python.WatchdogControlCommandRuntime",
        "commands": {
            "/inject": ["same-run structured fault injection"],
            "/watchdog": sorted(WatchdogControlCommandRuntime.ACTIONS),
        },
        "canonical_fault_owner": "FaultStateStore",
        "recovery_plan_owner": "M1-S07C",
        "observer_disable_starts_injection_fallback": False,
        "free_text_identity_inference": False,
    }


__all__ = [
    "WatchdogCommandReceipt",
    "WatchdogControlCommandRuntime",
    "watchdog_control_contract",
]
