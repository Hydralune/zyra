from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zyra_core import EventRecord, PlanNode, TaskState

from .models import FailureKind, FailureSignal, ResourceDecision


WATCHDOG_SOURCE_MODULES = {
    "browser-use": ["BrowserSession", "Controller action failures", "DOM state errors"],
    "claude-code-best": ["tool loop result classification", "permission runtime", "background task status"],
    "OpenHands": ["runtime event stream", "agent controller failure states"],
    "openclaw": ["fault injection and recovery concepts"],
}


class RuntimeWatchdog:
    def classify(
        self,
        state: TaskState,
        *,
        node: PlanNode | None = None,
        event: EventRecord | Mapping[str, Any] | None = None,
        worker_result: Any | None = None,
        error: BaseException | None = None,
        decision: ResourceDecision | None = None,
    ) -> FailureSignal:
        raw_parts: list[str] = []
        classification_parts: list[str] = []
        evidence_event_ids: list[str] = []
        if event is not None:
            event_dict = _event_dict(event)
            raw_parts.append(str(event_dict.get("event_type") or ""))
            raw_parts.append(str(event_dict.get("payload") or ""))
            classification_parts.extend(
                [
                    str(event_dict.get("event_type") or ""),
                    str(event_dict.get("payload") or ""),
                ]
            )
            event_id = str(event_dict.get("event_id") or "")
            if event_id:
                evidence_event_ids.append(event_id)
        if worker_result is not None:
            raw_parts.append(str(getattr(worker_result, "summary", "")))
            raw_parts.append(str(getattr(worker_result, "error", "")))
            raw_parts.append(str(getattr(worker_result, "metadata", "")))
            classification_parts.extend(
                [
                    str(getattr(worker_result, "summary", "")),
                    str(getattr(worker_result, "error", "")),
                ]
            )
        if error is not None:
            raw_parts.append(f"{type(error).__name__}: {error}")
            # An exception is the primary failure evidence.  Candidate lists,
            # permission check descriptions, and other node metadata are useful
            # in the audit trail but must not overwrite its classification.
            classification_parts = [f"{type(error).__name__}: {error}"]
        if node is not None:
            raw_parts.append(node.title)
            raw_parts.append(node.description)
            raw_parts.append(str(node.metadata))
            if not classification_parts:
                classification_parts.extend([node.title, node.description])
        raw = "\n".join(part for part in raw_parts if part).strip()
        lowered = "\n".join(
            part for part in classification_parts if part
        ).strip().lower()

        kind = FailureKind.UNKNOWN
        severity = "medium"
        retryable = True
        summary = "Runtime anomaly classified by watchdog."
        if "permission" in lowered and any(word in lowered for word in ["deny", "denied", "blocked", "ask"]):
            kind = FailureKind.PERMISSION_DENIED
            severity = "high"
            retryable = False
            summary = "Permission policy blocked a runtime operation."
        elif "timeout" in lowered or "timed out" in lowered or "stall" in lowered:
            kind = FailureKind.TOOL_TIMEOUT
            summary = "Tool or worker timeout detected."
        elif "browser" in lowered and any(word in lowered for word in ["crash", "failed", "outside workspace", "navigation"]):
            kind = FailureKind.BROWSER_CRASH
            summary = "Browser worker/session failure detected."
        elif "schema" in lowered or "validation" in lowered:
            kind = FailureKind.SCHEMA_ERROR if "schema" in lowered else FailureKind.VALIDATION_FAILED
            summary = "Structured output or verification failure detected."
        elif "model" in lowered or "quota" in lowered or "rate limit" in lowered:
            kind = FailureKind.MODEL_ERROR
            summary = "Model/backend failure detected."
        elif "node" in lowered and "fail" in lowered:
            kind = FailureKind.NODE_FAILED
            summary = "Plan node failure detected."
        elif any(word in lowered for word in ["unavailable", "connection refused", "not found"]):
            kind = FailureKind.WORKER_UNAVAILABLE
            summary = "Worker backend unavailable."

        # Structured ownership outranks free-text mentions.  In particular, an
        # unselected BrowserWorker alternative in route metadata is not the
        # worker that failed a CodeWorker execution.
        failed_worker = ""
        if decision is not None:
            failed_worker = decision.selected_worker
        if not failed_worker and node is not None and node.assigned_worker_id:
            failed_worker = str(node.assigned_worker_id)
        if not failed_worker:
            failed_worker = _worker_from_text(lowered)
        if kind == FailureKind.UNKNOWN and failed_worker:
            kind = FailureKind.NODE_FAILED
            summary = "Worker-linked failure detected."

        return FailureSignal(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=None if node is None else node.node_id,
            kind=kind,
            failed_worker=failed_worker,
            severity=severity,
            retryable=retryable,
            raw=raw,
            summary=summary,
            evidence_event_ids=evidence_event_ids,
            source_modules=WATCHDOG_SOURCE_MODULES,
            metadata={
                "node_status": "" if node is None else str(node.status),
                "decision_id": "" if decision is None else decision.decision_id,
            },
        )

    def scan_events(self, state: TaskState, events: Sequence[Mapping[str, Any]]) -> list[FailureSignal]:
        signals: list[FailureSignal] = []
        for event in events:
            event_type = str(event.get("event_type") or "")
            raw = str(event.get("payload") or "").lower()
            if event_type in {"failure_injected", "node_failed"} or any(
                word in raw for word in ["failed", "timeout", "permission denied", "crash"]
            ):
                signals.append(self.classify(state, event=event))
        return signals


def _event_dict(event: EventRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return {
            "event_id": event.event_id,
            "event_type": str(event.event_type),
            "payload": event.payload,
            "node_id": event.node_id,
        }
    return dict(event)


def _worker_from_text(text: str) -> str:
    if "browserworker" in text or "browser worker" in text or "browser" in text:
        return "BrowserWorker"
    if "codeworkerruntime" in text or "code worker" in text or "shell" in text:
        return "CodeWorkerRuntime"
    if "cloud" in text or "model" in text:
        return "cloud-planner-verifier"
    if "memory" in text or "compact" in text:
        return "local-memory-curator"
    return ""
