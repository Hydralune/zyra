from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .contracts import (
    CorrelationRefs,
    FaultKind,
    ObservationCategory,
    ObservationProvenance,
    ObserverDescriptor,
    ObserverMaturity,
    StructuredObservation,
    runtime_id,
    utc_now,
)
from .observer_registry import CallbackObserver, ObserverSubmission, WatchdogObserverRegistry


_CATEGORY = {item.value: item for item in ObservationCategory}


class RuntimeEventObservationAdapter:
    """Admits TypeScript watchdog events into the canonical Python journal.

    The TypeScript side is a real observation source, but the Python
    FaultStateStore remains the single durable signal owner.  All correlation
    fields are copied from the structured event; missing required identities
    are rejected rather than recovered from summaries or error text.
    """

    descriptor = ObserverDescriptor(
        observer_id="typescript-runtime-ingress",
        display_name="TypeScript CodeWorker watchdog event ingress",
        maturity=ObserverMaturity.ACTIVE_REAL,
        attach_owner="python.RuntimeWatchdog.ingest_runtime_event",
        lifecycle_owner="python.WatchdogObserverRegistry",
        observation_point="typescript.runtime.event.tool_failure_signal",
        source_repo="oh-my-pi",
        source_revision="c6b83c-source-audit",
        emitted_kinds=(
            FaultKind.TOOL_TIMEOUT,
            FaultKind.WORKER_UNAVAILABLE,
            FaultKind.PERMISSION_DENIED,
            FaultKind.MODEL_FAILURE,
            FaultKind.MODEL_RATE_LIMIT,
            FaultKind.MODEL_QUOTA_EXHAUSTED,
            FaultKind.MCP_DISCONNECTED,
            FaultKind.PROCESS_EXITED,
        ),
        categories=(
            ObservationCategory.TOOL,
            ObservationCategory.WORKER,
            ObservationCategory.PERMISSION,
            ObservationCategory.PROVIDER,
            ObservationCategory.MCP,
            ObservationCategory.PROCESS,
        ),
        enabled_by_default=True,
        metadata={
            "transport": "RuntimeEvent.phase=tool_failure_signal",
            "canonical_signal_owner": "python.FaultStateStore",
            "typescript_state_owner": "typescript.RuntimeWatchdogObserver",
            "free_text_identity_inference": False,
        },
    )

    def __init__(self, registry: WatchdogObserverRegistry) -> None:
        self.registry = registry
        self.callback = CallbackObserver(self.descriptor)
        self.registry.register(self.callback)
        self._accepted_source_ids: dict[str, str] = {}

    def ingest(self, event: Mapping[str, Any]) -> ObserverSubmission | None:
        if str(event.get("phase") or "") != "tool_failure_signal":
            return None
        observation = self.to_observation(event)
        prior = self.registry.store.observation(observation.observation_id)
        if prior is not None:
            last = self.registry.snapshot().get("last_submissions", {}).get(self.descriptor.observer_id)
            return None if last is None else None
        before = tuple(self.registry.store.signals(task_id=observation.refs.task_id, limit=10))
        if not self.callback.observe(observation):
            raise RuntimeError("TypeScript runtime event observer is not running")
        source_signal_id = str(event.get("signal_id") or "")
        if source_signal_id:
            candidates = self.registry.store.signals(task_id=observation.refs.task_id, limit=50)
            selected = next(
                (item for item in candidates if item.refs.observation_id == observation.observation_id),
                None,
            )
            if selected is not None:
                self._accepted_source_ids[source_signal_id] = selected.signal_id
        snapshot = self.registry.snapshot()
        payload = snapshot.get("last_submissions", {}).get(self.descriptor.observer_id)
        # The registry intentionally owns the concrete ObserverSubmission. The
        # mapping snapshot is sufficient to prove the callback reached it; API
        # callers retrieve the canonical signal through FaultStateStore.
        _ = before, payload
        return None

    def to_observation(self, event: Mapping[str, Any]) -> StructuredObservation:
        value = event.get("observation")
        if not isinstance(value, Mapping):
            raise ValueError("TypeScript watchdog event lacks structured observation")
        refs_value = value.get("refs")
        if not isinstance(refs_value, Mapping):
            refs_value = event.get("refs")
        if not isinstance(refs_value, Mapping):
            raise ValueError("TypeScript watchdog observation lacks structured refs")
        category_value = str(value.get("category") or "")
        category = _CATEGORY.get(category_value)
        if category is None or category is ObservationCategory.REQUIREMENT_CHANGE:
            raise ValueError("unsupported TypeScript watchdog observation category")
        run_id = str(refs_value.get("run_id") or event.get("run_id") or "")
        task_id = str(refs_value.get("task_id") or event.get("task_id") or "")
        observation_id = str(value.get("observation_id") or refs_value.get("observation_id") or runtime_id("ts-ingress-observation"))
        if not run_id or not task_id:
            raise ValueError("TypeScript watchdog event lacks run_id or task_id")
        refs = CorrelationRefs(
            run_id=run_id,
            task_id=task_id,
            observation_id=observation_id,
            session_id=str(refs_value.get("session_id") or ""),
            node_id=str(refs_value.get("node_id") or ""),
            attempt_id=str(refs_value.get("attempt_id") or ""),
            tool_call_id=str(refs_value.get("tool_call_id") or ""),
            tool_name=str(refs_value.get("tool_name") or ""),
            worker_id=str(refs_value.get("worker_id") or ""),
            backend_id=str(refs_value.get("backend_id") or ""),
            provider_id=str(refs_value.get("provider_id") or ""),
            workspace_id=str(refs_value.get("workspace_id") or ""),
            browser_session_id=str(refs_value.get("browser_session_id") or ""),
            mcp_server_id=str(refs_value.get("mcp_server_id") or ""),
            subagent_task_id=str(refs_value.get("subagent_task_id") or ""),
            source_state_revision=int(refs_value.get("source_state_revision") or event.get("sequence") or 0),
        )
        provenance_value = event.get("provenance")
        if not isinstance(provenance_value, Mapping):
            provenance_value = {}
        source_repo = str(provenance_value.get("source_repo") or "oh-my-pi")
        if source_repo != "oh-my-pi":
            raise ValueError("TypeScript supplementary watchdog source must declare oh-my-pi provenance")
        return StructuredObservation(
            category=category,
            code=str(value.get("code") or event.get("observed_code") or ""),
            refs=refs,
            provenance=ObservationProvenance(
                observer_id=self.descriptor.observer_id,
                source_repo=self.descriptor.source_repo,
                source_revision=str(provenance_value.get("source_revision") or self.descriptor.source_revision),
                observation_point=self.descriptor.observation_point,
                maturity=ObserverMaturity.ACTIVE_REAL,
            ),
            summary=str(value.get("summary") or "TypeScript runtime emitted a structured fault observation."),
            observed_at=str(value.get("observed_at") or event.get("created_at") or "") or utc_now(),
            status=str(value.get("status") or "failed"),
            error_type=str(value.get("error_type") or "RuntimeFault"),
            status_code=(int(value["status_code"]) if value.get("status_code") is not None else None),
            retryable_hint=(bool(value["retryable_hint"]) if value.get("retryable_hint") is not None else None),
            terminal_hint=(bool(value["terminal_hint"]) if value.get("terminal_hint") is not None else None),
            elapsed_ms=(int(value["elapsed_ms"]) if value.get("elapsed_ms") is not None else None),
            deadline_ms=(int(value["deadline_ms"]) if value.get("deadline_ms") is not None else None),
            details={
                **dict(value.get("details") or {}),
                "source_signal_id": str(event.get("signal_id") or ""),
                "source_runtime_id": str(event.get("runtime_id") or ""),
                "source_sequence": int(event.get("sequence") or 0),
                "critical_ref_source": "structured_refs_only",
            },
        )

    def canonical_signal_id(self, source_signal_id: str) -> str:
        return self._accepted_source_ids.get(source_signal_id, "")

    def snapshot(self) -> Mapping[str, Any]:
        return {
            "schema": "zyra.typescript-watchdog-ingress/v1",
            "descriptor": self.descriptor.to_dict(),
            "runtime": dict(self.callback.snapshot()),
            "source_to_canonical_signal_ids": dict(sorted(self._accepted_source_ids.items())),
            "critical_ref_source": "structured_refs_only",
        }


__all__ = ["RuntimeEventObservationAdapter"]
