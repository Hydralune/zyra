from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from zyra_core import EventRecord, EventType

from ..browser_state.contracts import (
    BrowserDisclosureBudget,
    BrowserDomCapture,
    BrowserSelectorMapRevision,
    state_id,
)
from ..browser_state.errors import BrowserMessageManagerDisabled
from ..browser_state.semantic_sections import BrowserSemanticOutlineBuilder
from ..browser_state.state_delta import BrowserDomDelta
from ..browser_state.text import estimate_tokens
from .action_result import BrowserActionResultProjector
from .action_envelope import BrowserActionEnvelopeNormalizer, BrowserActionOutputExternalizer
from .ablation import BrowserCompressionAblationRuntime
from .compressor import BrowserStateCompressor
from .context_port import BrowserNextContextPort
from .causal_runtime import BrowserTurnCausalRuntime
from .externalizer import BrowserStateArtifactExternalizer
from .fidelity import BrowserDisclosureFidelityAuditor
from .history import BrowserHistoryNormalizer, BrowserHistoryPolicy
from .memory_bridge import BrowserMemorySignalBridge
from .models import (
    BrowserMessageKind,
    BrowserMessagePart,
    BrowserMessageRole,
    BrowserMessageTurn,
    BrowserProjectionStatus,
    message_id,
)


class BrowserMessageManagerRuntime:
    """Deterministic per-turn browser state/message projection.

    No MessageManagerState is persisted here. Canonical history is the event
    log and 02D context window; read-once identity is the disclosure source id.
    """

    def __init__(
        self,
        *,
        externalizer: BrowserStateArtifactExternalizer,
        action_projector: BrowserActionResultProjector | None = None,
        compressor: BrowserStateCompressor | None = None,
        next_context: BrowserNextContextPort | None = None,
        memory_bridge: BrowserMemorySignalBridge | None = None,
        outline_builder: BrowserSemanticOutlineBuilder | None = None,
        fidelity_auditor: BrowserDisclosureFidelityAuditor | None = None,
        history_normalizer: BrowserHistoryNormalizer | None = None,
        action_envelope_normalizer: BrowserActionEnvelopeNormalizer | None = None,
        ablation_runtime: BrowserCompressionAblationRuntime | None = None,
        causal_runtime: BrowserTurnCausalRuntime | None = None,
        disabled: bool = False,
    ) -> None:
        self.externalizer = externalizer
        self.action_projector = action_projector or BrowserActionResultProjector()
        self.compressor = compressor or BrowserStateCompressor()
        self.next_context = next_context or BrowserNextContextPort()
        self.memory_bridge = memory_bridge or BrowserMemorySignalBridge()
        self.outline_builder = outline_builder or BrowserSemanticOutlineBuilder()
        self.fidelity_auditor = fidelity_auditor or BrowserDisclosureFidelityAuditor()
        self.history_normalizer = history_normalizer or BrowserHistoryNormalizer()
        self.action_envelope_normalizer = action_envelope_normalizer or BrowserActionEnvelopeNormalizer(
            externalizer=BrowserActionOutputExternalizer(externalizer.artifact_store),
        )
        self.ablation_runtime = ablation_runtime or BrowserCompressionAblationRuntime(
            externalizer.artifact_store,
        )
        self.causal_runtime = causal_runtime or BrowserTurnCausalRuntime(externalizer.artifact_store)
        self.disabled = disabled
        self._turns = 0
        self._blocked = 0
        self._degraded = 0

    def build_turn(
        self,
        capture: BrowserDomCapture,
        selector_revision: BrowserSelectorMapRevision,
        action_receipts: Sequence[Any],
        *,
        budget: BrowserDisclosureBudget,
        goal: str = "",
        constraints: Sequence[str] = (),
        previous_facts: Sequence[str] = (),
        context_window: Any | None = None,
        source_event_id: str = "",
        dom_delta: BrowserDomDelta | None = None,
        selector_probe_audit: Mapping[str, Any] | None = None,
    ) -> BrowserMessageTurn:
        if self.disabled:
            raise BrowserMessageManagerDisabled("browser message manager is disabled")
        externalization = self.externalizer.externalize(
            capture,
            selector_revision=selector_revision,
        )
        action_batch = self.action_envelope_normalizer.normalize_many(
            action_receipts,
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            node_id=capture.request.node_id,
            browser_session_id=capture.request.browser_session_id,
            worker_request_id=capture.request.worker_request_id,
            budget=budget,
        )
        action_artifacts = tuple(item.artifact for item in action_batch.artifacts)
        actions = self.action_projector.project_many(
            action_batch.receipts,
            budget=budget,
            selector_revision=selector_revision,
            capture_id=capture.capture_id,
            artifact_ids=externalization.artifact_ids,
        )
        semantic_outline = self.outline_builder.build(
            capture.root,
            goal=goal,
            constraints=constraints,
        )
        disclosure = self.compressor.compress(
            capture,
            selector_revision,
            actions,
            externalization,
            budget=budget,
            goal=goal,
            constraints=constraints,
            previous_facts=previous_facts,
            semantic_outline=semantic_outline,
            dom_delta=dom_delta,
        )
        fidelity_audit = self.fidelity_auditor.audit(
            capture,
            selector_revision,
            actions,
            externalization,
            disclosure,
        )
        self.fidelity_auditor.require_valid(fidelity_audit)
        ablation_report = None
        if self.ablation_runtime.enabled(capture.request.constraints):
            ablation_report = self.ablation_runtime.run(
                capture,
                selector_revision,
                disclosure,
                constraints=capture.request.constraints,
            )
        context_receipt = self.next_context.append_disclosure(
            disclosure,
            context_window=context_window,
            session_id=capture.request.canonical_session_id,
            worker_request_id=capture.request.worker_request_id,
            max_chars=budget.max_context_bytes,
        )
        state_message = BrowserMessagePart(
            message_id=message_id("state", disclosure.disclosure_id),
            role=BrowserMessageRole.USER,
            kind=BrowserMessageKind.STATE,
            content=disclosure.text,
            source_id=disclosure.disclosure_id,
            causation_id=capture.capture_id,
            artifact_ids=disclosure.artifact_ids,
            selector_refs=disclosure.selector_refs,
            trust=disclosure.trust,
            read_once=True,
            priority=820,
            token_estimate=estimate_tokens(disclosure.text),
            metadata={
                "selector_revision_id": selector_revision.revision_id,
                "context_receipt_id": context_receipt.receipt_id,
            },
        )
        messages: list[BrowserMessagePart] = [state_message]
        for action in actions:
            messages.extend((action.tool_call_message, action.tool_result_message))
        history_projection = self.history_normalizer.project(
            messages,
            policy=BrowserHistoryPolicy(
                max_tokens=budget.max_context_tokens + budget.max_action_result_tokens * budget.preserve_recent_actions,
                max_messages=max(8, 2 + budget.preserve_recent_actions * 2),
                preserve_recent_pairs=budget.preserve_recent_actions,
                preserve_failures=max(8, budget.preserve_recent_actions),
                preserve_state_messages=1,
                max_message_tokens=max(budget.max_context_tokens, budget.max_action_result_tokens),
                summary_tokens=min(900, budget.max_context_tokens),
            ),
        )
        messages = list(history_projection.messages)
        candidates = self.memory_bridge.candidates(
            capture,
            disclosure,
            actions,
            source_event_id=source_event_id,
        )
        turn_artifacts = tuple((
            *externalization.artifacts,
            *action_artifacts,
            *((ablation_report.bitmap_artifact,) if ablation_report and ablation_report.bitmap_artifact else ()),
            *((ablation_report.report_artifact,) if ablation_report and ablation_report.report_artifact else ()),
        ))
        causal_bundle = self.causal_runtime.build(
            capture=capture,
            revision=selector_revision,
            artifacts=turn_artifacts,
            disclosure=disclosure.to_dict(),
            actions=[item.to_dict() for item in actions],
            context_receipt=context_receipt.to_dict(),
            source_event_id=source_event_id,
            fidelity_audit=fidelity_audit.to_dict(),
            history_projection=history_projection.to_dict(),
            semantic_outline=semantic_outline.to_dict(section_limit=12),
            dom_delta=dom_delta.to_dict(change_limit=16) if dom_delta else {},
            action_envelope=action_batch.to_dict(),
            ablation=ablation_report.to_dict() if ablation_report else {},
            selector_probe_audit=dict(selector_probe_audit or {}),
        )
        events = list(causal_bundle.events)
        events.extend(self.memory_bridge.events(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            node_id=capture.request.node_id,
            worker_request_id=capture.request.worker_request_id,
            candidates=candidates,
            cause_event_id=events[-1].event_id if events else source_event_id,
        ))
        status = BrowserProjectionStatus.READY
        findings = list(capture.warnings)
        findings.extend(action_batch.findings)
        if ablation_report and not ablation_report.structured_wins_tokens:
            findings.append("structured_ablation_lane_did_not_reduce_tokens")
        if selector_probe_audit and int(selector_probe_audit.get("failed") or 0):
            findings.append("live_selector_probe_degraded")
        findings.extend(item.code for item in causal_bundle.audit.findings)
        if findings or not externalization.complete:
            status = BrowserProjectionStatus.DEGRADED
            self._degraded += 1
        self._turns += 1
        return BrowserMessageTurn(
            turn_id=state_id("brmsgturn"),
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            worker_request_id=capture.request.worker_request_id,
            browser_session_id=capture.request.browser_session_id,
            capture_id=capture.capture_id,
            selector_revision_id=selector_revision.revision_id,
            disclosure=disclosure,
            action_results=actions,
            messages=tuple(messages),
            artifacts=turn_artifacts,
            memory_candidates=candidates,
            next_context=context_receipt,
            events=tuple(events),
            metrics=disclosure.metrics,
            status=status,
            findings=tuple(findings),
            ablation=ablation_report.to_dict() if ablation_report else {},
        )

    def _events(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        disclosure: Any,
        actions: Sequence[Any],
        context_receipt: Mapping[str, Any],
        *,
        source_event_id: str,
        fidelity_audit: Mapping[str, Any],
        history_projection: Mapping[str, Any],
        semantic_outline: Mapping[str, Any],
        dom_delta: Mapping[str, Any],
        action_envelope: Mapping[str, Any],
        ablation: Mapping[str, Any],
        selector_probe_audit: Mapping[str, Any],
    ) -> tuple[EventRecord, ...]:
        capture_event = EventRecord(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            node_id=capture.request.node_id or None,
            event_type=EventType.BROWSER_SESSION_LIFECYCLE,
            payload={
                "browser_dom_state": capture.public_dict(),
                "selector_revision": {
                    "revision_id": revision.revision_id,
                    "revision": revision.revision,
                    "entry_count": len(revision.entries),
                    "identity": revision.identity.to_dict(),
                    "stale": revision.stale,
                },
                "cause_event_id": source_event_id,
            },
        )
        disclosure_event = EventRecord(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            node_id=capture.request.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_context_disclosure": disclosure.to_dict(),
                "browser_low_entropy_metrics": disclosure.metrics.to_dict(),
                "browser_action_result_projections": [item.to_dict() for item in actions],
                "browser_next_context": dict(context_receipt),
                "browser_disclosure_fidelity": dict(fidelity_audit),
                "browser_history_projection": dict(history_projection),
                "browser_semantic_outline": dict(semantic_outline),
                "browser_dom_delta": dict(dom_delta),
                "browser_action_envelope": dict(action_envelope),
                "browser_context_ablation": dict(ablation),
                "browser_live_selector_probe": dict(selector_probe_audit),
                "cause_event_id": capture_event.event_id,
            },
        )
        return capture_event, disclosure_event

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserMessageManagerRuntime",
            "owner_unit": "M1-S04B-01",
            "canonical_history_owner": "event log + M1-02D context",
            "disabled": self.disabled,
            "turns": self._turns,
            "blocked": self._blocked,
            "degraded": self._degraded,
            "action_projector": self.action_projector.snapshot(),
            "compressor": self.compressor.snapshot(),
            "next_context": self.next_context.snapshot(),
            "memory_bridge": self.memory_bridge.snapshot(),
            "externalizer": self.externalizer.snapshot(),
            "fidelity_auditor": self.fidelity_auditor.snapshot(),
            "history_normalizer": self.history_normalizer.snapshot(),
            "action_envelope_normalizer": self.action_envelope_normalizer.snapshot(),
            "ablation_runtime": self.ablation_runtime.snapshot(),
            "causal_runtime": self.causal_runtime.snapshot(),
        }
