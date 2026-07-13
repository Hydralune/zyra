from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from zyra_core import EventRecord, EventType

from ..browser_state.contracts import BrowserContextDisclosure, BrowserDomCapture, state_id
from ..browser_state.text import normalize_page_text
from .models import (
    BrowserActionResultProjection,
    BrowserMemoryCandidate,
    BrowserMemoryCandidateKind,
)


class BrowserMemorySignalBridge:
    """Emit candidate signals; MemoryFabric/06B/06C remain commit owners."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._candidates = 0
        self._events = 0

    def candidates(
        self,
        capture: BrowserDomCapture,
        disclosure: BrowserContextDisclosure,
        action_results: Sequence[BrowserActionResultProjection],
        *,
        source_event_id: str = "",
    ) -> tuple[BrowserMemoryCandidate, ...]:
        if self.disabled:
            return ()
        result: list[BrowserMemoryCandidate] = []
        artifact_ids = disclosure.artifact_ids
        for fact in disclosure.facts[:16]:
            value = normalize_page_text(fact, limit=500)
            if len(value) < 8:
                continue
            result.append(BrowserMemoryCandidate(
                candidate_id=state_id("brmemcand"),
                kind=BrowserMemoryCandidateKind.FACT,
                summary=value,
                source_event_id=source_event_id,
                source_action_receipt_id="",
                source_dom_capture_id=capture.capture_id,
                artifact_ids=artifact_ids,
                confidence=min(0.95, 0.55 + disclosure.metrics.critical_fact_fidelity * 0.4),
                retention_hint="episodic_or_semantic_after_verification",
                evidence={
                    "selector_revision_id": disclosure.selector_revision_id,
                    "disclosure_id": disclosure.disclosure_id,
                    "candidate_only": True,
                },
            ))
        for action in action_results:
            if not action.ok:
                result.append(BrowserMemoryCandidate(
                    candidate_id=state_id("brmemcand"),
                    kind=BrowserMemoryCandidateKind.FAILURE_PATTERN,
                    summary=normalize_page_text(
                        f"browser.{action.action} failed with {action.error_code}: {action.error_message}",
                        limit=600,
                    ),
                    source_event_id=source_event_id,
                    source_action_receipt_id=action.receipt_id,
                    source_dom_capture_id=capture.capture_id,
                    artifact_ids=tuple(dict.fromkeys((*artifact_ids, *action.artifact_ids))),
                    confidence=0.85,
                    retention_hint="failure_pattern_after_recurrence_check",
                    evidence={
                        "request_fingerprint": action.request_fingerprint,
                        "pair_fingerprint": action.pair_fingerprint,
                        "status": action.status,
                    },
                ))
            elif action.action in {"navigate", "evaluate_js", "focus_target", "take_screenshot"}:
                result.append(BrowserMemoryCandidate(
                    candidate_id=state_id("brmemcand"),
                    kind=BrowserMemoryCandidateKind.PROCEDURE_CANDIDATE,
                    summary=normalize_page_text(action.summary, limit=500),
                    source_event_id=source_event_id,
                    source_action_receipt_id=action.receipt_id,
                    source_dom_capture_id=capture.capture_id,
                    artifact_ids=tuple(dict.fromkeys((*artifact_ids, *action.artifact_ids))),
                    confidence=0.62,
                    retention_hint="skill_candidate_only_after_06c_validation",
                    evidence={
                        "action": action.action,
                        "selector_refs": list(action.selector_refs),
                        "candidate_only": True,
                    },
                ))
        deduped: dict[tuple[str, str, str], BrowserMemoryCandidate] = {}
        for candidate in result:
            key = (str(candidate.kind), candidate.summary.casefold(), candidate.source_action_receipt_id)
            deduped.setdefault(key, candidate)
        values = tuple(deduped.values())
        self._candidates += len(values)
        return values

    def events(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        worker_request_id: str,
        candidates: Sequence[BrowserMemoryCandidate],
        cause_event_id: str = "",
    ) -> tuple[EventRecord, ...]:
        result: list[EventRecord] = []
        for candidate in candidates:
            result.append(EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id or None,
                event_type=EventType.AGENT_MESSAGE,
                payload={
                    "browser_memory_candidate": candidate.to_dict(),
                    "worker_request_id": worker_request_id,
                    "cause_event_id": cause_event_id,
                    "canonical_memory_owner": "MemoryFabric/M1-06B-M1-06C",
                    "committed": False,
                },
            ))
        self._events += len(result)
        return tuple(result)

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserMemorySignalBridge",
            "owner_unit": "M1-S04B-01",
            "canonical_memory_owner": "MemoryFabric/M1-06B-M1-06C",
            "disabled": self.disabled,
            "candidates": self._candidates,
            "events": self._events,
            "direct_memory_writes": 0,
        }
