from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .contracts import FaultKind, FaultSignal, SignalOrigin, runtime_id, utc_now
from .source_session import SourceKind
from .state_store import FaultStateStore


class ContainmentPhase(StrEnum):
    UNBOUND = "unbound"
    READY = "ready"
    APPLYING = "applying"
    APPLIED = "applied"
    FAILED = "failed"
    RELEASED = "released"


ContainmentHandler = Callable[[FaultSignal, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(slots=True)
class ContainmentTarget:
    source_kind: SourceKind
    source_id: str
    run_id: str
    task_id: str
    generation: int
    handlers: dict[str, ContainmentHandler]
    phase: ContainmentPhase = ContainmentPhase.READY
    revision: int = 1
    registered_at: str = field(default_factory=utc_now)
    released_at: str = ""
    release_reason: str = ""
    applied_signal_ids: list[str] = field(default_factory=list)
    failure_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source_kind.value}:{self.source_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "generation": self.generation,
            "actions": sorted(self.handlers),
            "phase": self.phase.value,
            "revision": self.revision,
            "registered_at": self.registered_at,
            "released_at": self.released_at,
            "release_reason": self.release_reason,
            "applied_signal_ids": list(self.applied_signal_ids[-100:]),
            "failure_count": self.failure_count,
            "metadata": dict(self.metadata),
        }


@dataclass(slots=True)
class ContainmentEffectState:
    signal_id: str
    run_id: str
    task_id: str
    fault_kind: FaultKind
    source_kind: SourceKind
    source_id: str
    generation: int
    action: str
    phase: ContainmentPhase
    attempt_count: int = 0
    result: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""
    revision: int = 1
    updated_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "fault_kind": self.fault_kind.value,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "generation": self.generation,
            "action": self.action,
            "phase": self.phase.value,
            "attempt_count": self.attempt_count,
            "result": dict(self.result),
            "last_error": self.last_error,
            "revision": self.revision,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class ContainmentReceipt:
    signal_id: str
    run_id: str
    task_id: str
    fault_kind: FaultKind
    source_kind: SourceKind
    source_id: str
    action: str
    phase: ContainmentPhase
    applied: bool
    changed: bool
    result: Mapping[str, Any] = field(default_factory=dict)
    error: str = ""
    receipt_id: str = field(default_factory=lambda: runtime_id("fault-containment"))
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.fault-containment-receipt/v1",
            "receipt_id": self.receipt_id,
            "signal_id": self.signal_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "fault_kind": self.fault_kind.value,
            "source_kind": self.source_kind.value,
            "source_id": self.source_id,
            "action": self.action,
            "phase": self.phase.value,
            "applied": self.applied,
            "changed": self.changed,
            "result": dict(self.result),
            "error": self.error,
            "created_at": self.created_at,
        }


_CONTAINMENT_ACTIONS: dict[FaultKind, tuple[SourceKind, str]] = {
    FaultKind.TOOL_TIMEOUT: (SourceKind.TOOL, "abort_tool_call"),
    FaultKind.WORKER_UNAVAILABLE: (SourceKind.WORKER, "revoke_worker_lease"),
    FaultKind.PROCESS_EXITED: (SourceKind.WORKER, "revoke_worker_lease"),
    FaultKind.BROWSER_CRASH: (SourceKind.BROWSER, "fence_browser_session"),
    FaultKind.BROWSER_DISCONNECT: (SourceKind.BROWSER, "fence_browser_transport"),
    FaultKind.MODEL_FAILURE: (SourceKind.PROVIDER, "abort_provider_stream"),
    FaultKind.MODEL_RATE_LIMIT: (SourceKind.PROVIDER, "pause_provider_attempts"),
    FaultKind.MODEL_QUOTA_EXHAUSTED: (SourceKind.PROVIDER, "quarantine_provider_credential"),
    FaultKind.MCP_DISCONNECTED: (SourceKind.MCP, "fence_pending_mcp_requests"),
    FaultKind.WORKSPACE_CORRUPT: (SourceKind.WORKSPACE, "fence_workspace_writes"),
    FaultKind.PERMISSION_DENIED: (SourceKind.TOOL, "fence_denied_action"),
    FaultKind.SCHEMA_FAILURE: (SourceKind.TOOL, "reject_invalid_payload"),
    FaultKind.SUBAGENT_FAILED: (SourceKind.WORKER, "fence_subagent_result"),
}


class ActiveFaultContainmentRuntime:
    """Applies immediate, deterministic containment for real observations.

    Containment is deliberately narrower than recovery: it aborts or fences
    the failing execution surface before a 07C consumer chooses retry,
    reroute, restore or terminal handling. Injected signals never execute
    these native callbacks, preserving fault injection as a same-run state
    mutation rather than an uncontrolled external-resource mutation.
    """

    _METADATA_KEY = "M1-S07B-02.active-containment"

    def __init__(self, store: FaultStateStore) -> None:
        self.store = store
        self._guard = threading.RLock()
        self._targets: dict[str, ContainmentTarget] = {}
        self._effects: dict[str, ContainmentEffectState] = {}
        self._receipts: list[ContainmentReceipt] = []
        self._metadata_revision = 0
        self._applied_count = 0
        self._failed_count = 0
        self._unbound_count = 0
        self._injection_skipped_count = 0
        self._restore()

    def register(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        *,
        run_id: str,
        task_id: str,
        generation: int,
        handlers: Mapping[str, ContainmentHandler],
        metadata: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        kind = SourceKind(source_kind)
        if not source_id.strip() or not run_id.strip() or not task_id.strip():
            raise ValueError("containment target requires source, run and task identity")
        if generation < 0:
            raise ValueError("containment target generation must be non-negative")
        normalized = {
            str(action).strip(): handler
            for action, handler in handlers.items()
            if str(action).strip() and callable(handler)
        }
        if not normalized:
            raise ValueError("containment target requires at least one callable action")
        key = self._key(kind, source_id)
        with self._guard:
            current = self._targets.get(key)
            if current is not None:
                if generation < current.generation:
                    raise RuntimeError("stale containment generation cannot replace target")
                if generation == current.generation:
                    if current.run_id != run_id or current.task_id != task_id:
                        raise RuntimeError("containment generation cannot move across run/task scope")
                    current.handlers.update(normalized)
                    current.phase = ContainmentPhase.READY
                    current.revision += 1
                    current.metadata.update(dict(metadata or {}))
                    self._persist_locked()
                    return {**current.to_dict(), "changed": True, "reattached": True}
                current.phase = ContainmentPhase.RELEASED
                current.released_at = utc_now()
                current.release_reason = "replaced by newer generation"
                current.revision += 1
            target = ContainmentTarget(
                source_kind=kind,
                source_id=source_id,
                run_id=run_id,
                task_id=task_id,
                generation=generation,
                handlers=normalized,
                metadata={
                    **dict(metadata or {}),
                    "native_callback_owner": "registered Zyra runtime surface",
                    "recovery_plan_owner": "M1-S07C",
                },
            )
            self._targets[key] = target
            self._persist_locked()
            return {**target.to_dict(), "changed": True, "reattached": current is not None}

    def release(
        self,
        source_kind: SourceKind | str,
        source_id: str,
        *,
        generation: int,
        reason: str,
    ) -> Mapping[str, Any]:
        kind = SourceKind(source_kind)
        if not reason.strip():
            raise ValueError("containment release requires reason")
        key = self._key(kind, source_id)
        with self._guard:
            target = self._targets.get(key)
            if target is None:
                raise KeyError(key)
            if target.generation != generation:
                raise RuntimeError("stale containment generation")
            if target.phase is ContainmentPhase.RELEASED:
                return {**target.to_dict(), "changed": False}
            target.phase = ContainmentPhase.RELEASED
            target.released_at = utc_now()
            target.release_reason = reason
            target.handlers.clear()
            target.revision += 1
            self._persist_locked()
            return {**target.to_dict(), "changed": True}

    def contain(self, signal: FaultSignal) -> ContainmentReceipt | None:
        selected = _CONTAINMENT_ACTIONS.get(signal.kind)
        if selected is None:
            return None
        source_kind, action = selected
        source_id = self._source_id(signal, source_kind)
        if signal.origin is SignalOrigin.INJECTION:
            with self._guard:
                self._injection_skipped_count += 1
            return self._receipt_for_skip(signal, source_kind, source_id, action)
        with self._guard:
            prior = self._effects.get(signal.signal_id)
            if prior is not None and prior.phase is ContainmentPhase.APPLIED:
                receipt = self._receipt(prior, changed=False)
                self._remember_receipt_locked(receipt)
                return receipt
            target = self._targets.get(self._key(source_kind, source_id)) if source_id else None
            if target is None or target.phase is not ContainmentPhase.READY:
                effect = prior or ContainmentEffectState(
                    signal_id=signal.signal_id,
                    run_id=signal.refs.run_id,
                    task_id=signal.refs.task_id,
                    fault_kind=signal.kind,
                    source_kind=source_kind,
                    source_id=source_id,
                    generation=int(signal.refs.source_state_revision),
                    action=action,
                    phase=ContainmentPhase.UNBOUND,
                )
                effect.phase = ContainmentPhase.UNBOUND
                effect.attempt_count += 1
                effect.last_error = "no active native containment target"
                effect.revision += 1
                effect.updated_at = utc_now()
                self._effects[signal.signal_id] = effect
                self._unbound_count += 1
                receipt = self._receipt(effect, changed=prior is None)
                self._remember_receipt_locked(receipt)
                self._persist_locked()
                return receipt
            if target.run_id != signal.refs.run_id or target.task_id != signal.refs.task_id:
                raise RuntimeError("containment target is outside signal run/task scope")
            observation_details = signal.details.get("observation_details")
            observed_generation = (
                observation_details.get("generation")
                if isinstance(observation_details, Mapping)
                else signal.details.get("generation")
            )
            if observed_generation is not None and target.generation != int(observed_generation):
                raise RuntimeError("containment target generation differs from fault source generation")
            handler = target.handlers.get(action)
            if handler is None:
                effect = prior or ContainmentEffectState(
                    signal_id=signal.signal_id,
                    run_id=signal.refs.run_id,
                    task_id=signal.refs.task_id,
                    fault_kind=signal.kind,
                    source_kind=source_kind,
                    source_id=source_id,
                    generation=target.generation,
                    action=action,
                    phase=ContainmentPhase.UNBOUND,
                )
                effect.phase = ContainmentPhase.UNBOUND
                effect.attempt_count += 1
                effect.last_error = f"target does not implement {action}"
                effect.revision += 1
                effect.updated_at = utc_now()
                self._effects[signal.signal_id] = effect
                self._unbound_count += 1
                receipt = self._receipt(effect, changed=prior is None)
                self._remember_receipt_locked(receipt)
                self._persist_locked()
                return receipt
            effect = prior or ContainmentEffectState(
                signal_id=signal.signal_id,
                run_id=signal.refs.run_id,
                task_id=signal.refs.task_id,
                fault_kind=signal.kind,
                source_kind=source_kind,
                source_id=source_id,
                generation=target.generation,
                action=action,
                phase=ContainmentPhase.READY,
            )
            effect.phase = ContainmentPhase.APPLYING
            effect.attempt_count += 1
            effect.last_error = ""
            effect.revision += 1
            effect.updated_at = utc_now()
            self._effects[signal.signal_id] = effect
            target.phase = ContainmentPhase.APPLYING
            target.revision += 1
            self._persist_locked()
        context = {
            "schema": "zyra.fault-containment-context/v1",
            "signal_id": signal.signal_id,
            "fault_kind": signal.kind.value,
            "action": action,
            "source_kind": source_kind.value,
            "source_id": source_id,
            "generation": target.generation,
            "run_id": signal.refs.run_id,
            "task_id": signal.refs.task_id,
            "session_id": signal.refs.session_id,
            "attempt_id": signal.refs.attempt_id,
            "tool_call_id": signal.refs.tool_call_id,
            "backend_id": signal.refs.backend_id,
            "provider_id": signal.refs.provider_id,
            "mcp_server_id": signal.refs.mcp_server_id,
            "observed_code": signal.observed_code,
            "recovery_plan_selected": False,
        }
        try:
            raw = handler(signal, context)
            result = dict(raw)
            applied = bool(result.get("applied"))
            if not applied:
                raise RuntimeError(str(result.get("error") or "containment callback did not apply effect"))
            self._validate_result(result, signal, action)
        except Exception as error:
            with self._guard:
                effect.phase = ContainmentPhase.FAILED
                effect.last_error = f"{type(error).__name__}: {error}"
                effect.revision += 1
                effect.updated_at = utc_now()
                target.phase = ContainmentPhase.READY
                target.failure_count += 1
                target.revision += 1
                self._failed_count += 1
                receipt = self._receipt(effect, changed=True)
                self._remember_receipt_locked(receipt)
                self._persist_locked()
                return receipt
        with self._guard:
            effect.phase = ContainmentPhase.APPLIED
            effect.result = result
            effect.last_error = ""
            effect.revision += 1
            effect.updated_at = utc_now()
            target.phase = ContainmentPhase.READY
            target.applied_signal_ids.append(signal.signal_id)
            del target.applied_signal_ids[:-100]
            target.revision += 1
            self._applied_count += 1
            receipt = self._receipt(effect, changed=True)
            self._remember_receipt_locked(receipt)
            self._persist_locked()
            return receipt

    def retry(self, signal_id: str) -> ContainmentReceipt:
        signal = self.store.signal(signal_id)
        if signal is None:
            raise KeyError(signal_id)
        with self._guard:
            effect = self._effects.get(signal_id)
            if effect is None:
                raise KeyError(signal_id)
            if effect.phase not in {ContainmentPhase.FAILED, ContainmentPhase.UNBOUND}:
                raise RuntimeError(f"containment cannot retry from {effect.phase.value}")
        receipt = self.contain(signal)
        if receipt is None:
            raise RuntimeError("fault kind has no immediate containment action")
        return receipt

    def reconcile_new_for_task(self, task_id: str, *, limit: int = 500) -> tuple[ContainmentReceipt, ...]:
        with self._guard:
            known = set(self._effects)
        receipts: list[ContainmentReceipt] = []
        for signal in reversed(self.store.signals(task_id=task_id, limit=limit)):
            if signal.signal_id in known:
                continue
            receipt = self.contain(signal)
            if receipt is not None:
                receipts.append(receipt)
        return tuple(receipts)

    def snapshot(self, *, task_id: str = "") -> Mapping[str, Any]:
        with self._guard:
            targets = {
                key: value.to_dict()
                for key, value in sorted(self._targets.items())
                if not task_id or value.task_id == task_id
            }
            effects = {
                key: value.to_dict()
                for key, value in sorted(self._effects.items())
                if not task_id or value.task_id == task_id
            }
            receipts = [
                item.to_dict()
                for item in self._receipts[-200:]
                if not task_id or item.task_id == task_id
            ]
        return {
            "schema": "zyra.active-fault-containment-runtime/v1",
            "task_id": task_id,
            "targets": targets,
            "effects": effects,
            "recent_receipts": receipts,
            "counts": {
                "applied": self._applied_count,
                "failed": self._failed_count,
                "unbound": self._unbound_count,
                "injection_skipped": self._injection_skipped_count,
            },
            "metadata_revision": self._metadata_revision,
            "observer_signals_execute_native_effects": True,
            "injection_signals_execute_native_effects": False,
            "selects_retry_or_route": False,
            "recovery_plan_owner": "M1-S07C",
        }

    @staticmethod
    def _source_id(signal: FaultSignal, source_kind: SourceKind) -> str:
        if source_kind is SourceKind.TOOL:
            return signal.refs.tool_call_id
        if source_kind is SourceKind.WORKER:
            return signal.refs.worker_id or signal.refs.subagent_task_id
        if source_kind is SourceKind.BROWSER:
            return signal.refs.browser_session_id
        if source_kind is SourceKind.PROVIDER:
            return signal.refs.provider_id or signal.refs.backend_id
        if source_kind is SourceKind.MCP:
            return signal.refs.mcp_server_id
        if source_kind is SourceKind.WORKSPACE:
            return signal.refs.workspace_id
        return ""

    @staticmethod
    def _validate_result(result: Mapping[str, Any], signal: FaultSignal, action: str) -> None:
        result_signal = str(result.get("signal_id") or signal.signal_id)
        result_action = str(result.get("action") or action)
        if result_signal != signal.signal_id:
            raise RuntimeError("containment callback receipt signal_id mismatch")
        if result_action != action:
            raise RuntimeError("containment callback receipt action mismatch")
        if bool(result.get("recovery_plan_selected")):
            raise RuntimeError("containment callback must not select a recovery plan")

    def _receipt_for_skip(
        self,
        signal: FaultSignal,
        source_kind: SourceKind,
        source_id: str,
        action: str,
    ) -> ContainmentReceipt:
        return ContainmentReceipt(
            signal_id=signal.signal_id,
            run_id=signal.refs.run_id,
            task_id=signal.refs.task_id,
            fault_kind=signal.kind,
            source_kind=source_kind,
            source_id=source_id,
            action=action,
            phase=ContainmentPhase.RELEASED,
            applied=False,
            changed=False,
            result={"skipped": "injection_origin", "external_resource_mutation": False},
        )

    @staticmethod
    def _receipt(effect: ContainmentEffectState, *, changed: bool) -> ContainmentReceipt:
        return ContainmentReceipt(
            signal_id=effect.signal_id,
            run_id=effect.run_id,
            task_id=effect.task_id,
            fault_kind=effect.fault_kind,
            source_kind=effect.source_kind,
            source_id=effect.source_id,
            action=effect.action,
            phase=effect.phase,
            applied=effect.phase is ContainmentPhase.APPLIED,
            changed=changed,
            result=effect.result,
            error=effect.last_error,
        )

    def _remember_receipt_locked(self, receipt: ContainmentReceipt) -> None:
        self._receipts.append(receipt)
        del self._receipts[:-500]

    def _persist_locked(self) -> None:
        payload = {
            "schema": "zyra.active-fault-containment-runtime/v1",
            "targets": {
                key: {
                    **value.to_dict(),
                    "actions": sorted(value.handlers),
                    "handlers_restorable": False,
                }
                for key, value in sorted(self._targets.items())
            },
            "effects": {
                key: value.to_dict()
                for key, value in sorted(self._effects.items())
            },
            "counts": {
                "applied": self._applied_count,
                "failed": self._failed_count,
                "unbound": self._unbound_count,
                "injection_skipped": self._injection_skipped_count,
            },
            "updated_at": utc_now(),
            "owns_recovery_plan": False,
        }
        self._metadata_revision = self.store.put_metadata(
            self._METADATA_KEY,
            payload,
            expected_revision=self._metadata_revision,
        )

    def _restore(self) -> None:
        saved = self.store.metadata(self._METADATA_KEY)
        if saved is None:
            return
        payload, revision = saved
        effects: dict[str, ContainmentEffectState] = {}
        for signal_id, raw in dict(payload.get("effects") or {}).items():
            value = dict(raw or {})
            try:
                phase = ContainmentPhase(str(value.get("phase") or "unbound"))
                if phase is ContainmentPhase.APPLYING:
                    phase = ContainmentPhase.FAILED
                effects[str(signal_id)] = ContainmentEffectState(
                    signal_id=str(value.get("signal_id") or signal_id),
                    run_id=str(value.get("run_id") or ""),
                    task_id=str(value.get("task_id") or ""),
                    fault_kind=FaultKind(str(value.get("fault_kind") or "unknown")),
                    source_kind=SourceKind(str(value.get("source_kind") or "tool")),
                    source_id=str(value.get("source_id") or ""),
                    generation=int(value.get("generation") or 0),
                    action=str(value.get("action") or ""),
                    phase=phase,
                    attempt_count=int(value.get("attempt_count") or 0),
                    result=dict(value.get("result") or {}),
                    last_error=(
                        "runtime restarted while containment callback was active"
                        if str(value.get("phase") or "") == "applying"
                        else str(value.get("last_error") or "")
                    ),
                    revision=int(value.get("revision") or 0) + 1,
                    updated_at=utc_now(),
                )
            except (TypeError, ValueError):
                continue
        counts = dict(payload.get("counts") or {})
        self._effects = effects
        self._applied_count = int(counts.get("applied") or 0)
        self._failed_count = int(counts.get("failed") or 0)
        self._unbound_count = int(counts.get("unbound") or 0)
        self._injection_skipped_count = int(counts.get("injection_skipped") or 0)
        self._metadata_revision = revision

    @staticmethod
    def _key(source_kind: SourceKind, source_id: str) -> str:
        return f"{source_kind.value}:{source_id}"


def containment_runtime_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.active-fault-containment-contract/v1",
        "owner": "ActiveFaultContainmentRuntime",
        "observer_signals_execute_native_effects": True,
        "injection_signals_execute_native_effects": False,
        "idempotent_by_signal": True,
        "generation_fenced": True,
        "restart_requires_callback_reattach": True,
        "selects_retry_or_route": False,
        "recovery_plan_owner": "M1-S07C",
        "actions": {
            kind.value: {"source_kind": source.value, "action": action}
            for kind, (source, action) in _CONTAINMENT_ACTIONS.items()
        },
    }


__all__ = [
    "ActiveFaultContainmentRuntime",
    "ContainmentEffectState",
    "ContainmentHandler",
    "ContainmentPhase",
    "ContainmentReceipt",
    "ContainmentTarget",
    "containment_runtime_contract",
]
