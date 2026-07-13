from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType
from zyra_runtime import LocalArtifactStore

from ...browser_session.errors import BrowserArtifactError
from ..models import (
    HealthStatus,
    ObservationScope,
    Severity,
    SignalKind,
    TraceSpan,
    TraceSpanKind,
    TraceStatus,
    WatchdogName,
    WatchdogSignal,
    digest_value,
    new_observation_id,
    utc_now,
)
from ..screenshot_runtime import BrowserScreenshotRuntime, ScreenshotEvidence
from .contracts import (
    BrowserIntegrationOutput,
    EvidenceSource,
    EvidenceTerminalState,
    RuntimeEvidenceEnvelope,
)


class ScreenshotEvidenceError(BrowserArtifactError):
    code = "browser_screenshot_evidence_error"


@dataclass(frozen=True, slots=True)
class ScreenshotEvidencePolicy:
    require_highlight_removed: bool = True
    require_highlight_restored: bool = True
    require_action_correlation: bool = True
    fail_if_terminal_action_missing: bool = True


@dataclass(frozen=True, slots=True)
class ScreenshotPublicationReceipt:
    scope: ObservationScope
    artifact_id: str
    screenshot: ScreenshotEvidence
    action_id: str
    source_event_ids: tuple[str, ...]
    highlight_removed: bool
    highlight_restored: bool
    capture_id: str
    receipt_id: str = field(
        default_factory=lambda: new_observation_id("browser-screenshot-publication")
    )
    tool_call_id: str = ""
    action_receipt_id: str = ""
    target_id: str = ""
    selector_revision_id: str = ""
    created_at: str = field(default_factory=utc_now)

    @property
    def digest(self) -> str:
        return digest_value(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-observability.screenshot-publication.v1",
            "receipt_id": self.receipt_id,
            "scope": self.scope.to_dict(),
            "artifact_id": self.artifact_id,
            "screenshot": self.screenshot.to_dict(),
            "action_id": self.action_id,
            "tool_call_id": self.tool_call_id,
            "action_receipt_id": self.action_receipt_id,
            "source_event_ids": list(self.source_event_ids),
            "highlight_removed": self.highlight_removed,
            "highlight_restored": self.highlight_restored,
            "capture_id": self.capture_id,
            "target_id": self.target_id,
            "selector_revision_id": self.selector_revision_id,
            "created_at": self.created_at,
            "artifact_owner": "LocalArtifactStore",
            "capture_owner": "M1-04A/04C",
            "evidence_owner": "M1-04D",
        }
        if include_digest:
            value["digest"] = digest_value(value)
        return value

    def runtime_evidence(self) -> RuntimeEvidenceEnvelope:
        return RuntimeEvidenceEnvelope(
            scope=self.scope,
            source=EvidenceSource.BROWSER_SCREENSHOT,
            event_type="screenshot_published",
            payload=self.to_dict(),
            evidence_id=self.receipt_id,
            source_event_id=self.source_event_ids[-1] if self.source_event_ids else "",
            correlation_event_ids=self.source_event_ids,
            action_id=self.action_id,
            tool_call_id=self.tool_call_id,
            action_receipt_id=self.action_receipt_id,
            terminal_state=EvidenceTerminalState.COMPLETED,
            artifact_ids=(self.artifact_id,),
            metadata={
                "capture_id": self.capture_id,
                "highlight_removed": self.highlight_removed,
                "highlight_restored": self.highlight_restored,
            },
        )


class BrowserScreenshotEvidenceRuntime:
    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        screenshot_runtime: BrowserScreenshotRuntime | None = None,
        policy: ScreenshotEvidencePolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.runtime = screenshot_runtime or BrowserScreenshotRuntime()
        self.policy = policy or ScreenshotEvidencePolicy()
        self._receipts: list[ScreenshotPublicationReceipt] = []
        self._by_artifact: dict[str, ScreenshotPublicationReceipt] = {}

    def inspect(
        self,
        scope: ObservationScope,
        artifacts: Sequence[ArtifactRef],
        *,
        action_run: Any,
        application_events: Sequence[EventRecord],
        action_terminal: bool,
    ) -> BrowserIntegrationOutput:
        screenshot_artifacts = tuple(
            item
            for item in artifacts
            if item.kind == ArtifactKind.SCREENSHOT
            or "screenshot" in item.title.casefold()
        )
        correlation = _action_correlation(action_run, application_events)
        new_receipts: list[ScreenshotPublicationReceipt] = []
        for artifact in screenshot_artifacts:
            if artifact.artifact_id in self._by_artifact:
                continue
            metadata = dict(artifact.metadata)
            action_id = str(
                metadata.get("browser_action_id")
                or metadata.get("action_id")
                or correlation["action_id"]
                or ""
            )
            if self.policy.require_action_correlation and not action_id:
                raise ScreenshotEvidenceError(
                    "screenshot artifact lacks browser action correlation",
                    session_id=scope.browser_session_id,
                    operation="screenshot_evidence",
                )
            removed = _boolean(metadata.get("highlight_removed"))
            restored = _boolean(metadata.get("highlight_restored"))
            if self.policy.require_highlight_removed and not removed:
                raise ScreenshotEvidenceError(
                    "screenshot was captured without highlight removal",
                    session_id=scope.browser_session_id,
                    operation="screenshot_evidence",
                )
            if self.policy.require_highlight_restored and not restored:
                raise ScreenshotEvidenceError(
                    "screenshot capture left page highlight state mutated",
                    session_id=scope.browser_session_id,
                    operation="screenshot_evidence",
                )
            path = self.artifact_store.resolve_path(artifact)
            screenshot = self.runtime.inspect_file(
                path,
                artifact_id=artifact.artifact_id,
                source_event_id=correlation["source_event_ids"][-1]
                if correlation["source_event_ids"]
                else "",
                action_id=action_id,
                metadata={
                    "target_id": metadata.get("target_id"),
                    "highlight_removed": removed,
                    "highlight_restored": restored,
                    "capture_id": metadata.get("screenshot_capture_id"),
                },
            )
            receipt = ScreenshotPublicationReceipt(
                scope=scope,
                artifact_id=artifact.artifact_id,
                screenshot=screenshot,
                action_id=action_id,
                tool_call_id=str(correlation["tool_call_id"]),
                action_receipt_id=str(correlation["action_receipt_id"]),
                source_event_ids=tuple(correlation["source_event_ids"]),
                highlight_removed=removed,
                highlight_restored=restored,
                capture_id=str(metadata.get("screenshot_capture_id") or ""),
                target_id=str(metadata.get("target_id") or ""),
                selector_revision_id=str(
                    metadata.get("browser_selector_revision_id")
                    or metadata.get("selector_revision_id")
                    or ""
                ),
            )
            self._by_artifact[artifact.artifact_id] = receipt
            self._receipts.append(receipt)
            new_receipts.append(receipt)
        signals: list[WatchdogSignal] = []
        if (
            action_terminal
            and self.policy.fail_if_terminal_action_missing
            and correlation["screenshot_expected"]
            and not screenshot_artifacts
        ):
            signals.append(
                WatchdogSignal(
                    scope=scope,
                    watchdog=WatchdogName.SCREENSHOT,
                    kind=SignalKind.SCREENSHOT_MISSING,
                    status=HealthStatus.DEGRADED,
                    severity=Severity.WARNING,
                    summary="Terminal screenshot action produced no screenshot artifact.",
                    sequence=1,
                    retryable=True,
                    terminal=False,
                    evidence_event_ids=tuple(correlation["source_event_ids"]),
                    metadata={
                        "action_id": correlation["action_id"],
                        "tool_call_id": correlation["tool_call_id"],
                    },
                )
            )
        spans = tuple(self._span(item) for item in new_receipts)
        events = tuple(self._event(item) for item in new_receipts)
        return BrowserIntegrationOutput(
            evidence=tuple(item.runtime_evidence() for item in new_receipts),
            signals=tuple(signals),
            spans=spans,
            events=events,
            projection=self.projection(scope=scope),
        )

    def projection(self, *, scope: ObservationScope | None = None) -> dict[str, Any]:
        values = tuple(
            item for item in self._receipts if scope is None or item.scope == scope
        )
        return {
            "schema": "zyra.browser-observability.screenshot-evidence.v1",
            "count": len(values),
            "highlight_removed": sum(1 for item in values if item.highlight_removed),
            "highlight_restored": sum(1 for item in values if item.highlight_restored),
            "action_coverage": len({item.action_id for item in values}),
            "screenshots": [item.to_dict() for item in values[-100:]],
        }

    @staticmethod
    def _span(receipt: ScreenshotPublicationReceipt) -> TraceSpan:
        return TraceSpan(
            scope=receipt.scope,
            kind=TraceSpanKind.ARTIFACT,
            name="browser_screenshot_publish",
            status=TraceStatus.OK,
            trace_id=receipt.scope.key,
            tool_call_id=receipt.tool_call_id,
            input_digest=digest_value(
                {
                    "action_id": receipt.action_id,
                    "capture_id": receipt.capture_id,
                }
            ),
            output_digest=receipt.screenshot.digest,
            event_ids=receipt.source_event_ids,
            artifact_ids=(receipt.artifact_id,),
            attributes={
                "highlight_removed": receipt.highlight_removed,
                "highlight_restored": receipt.highlight_restored,
                "width": receipt.screenshot.width,
                "height": receipt.screenshot.height,
                "target_id": receipt.target_id,
            },
        )

    @staticmethod
    def _event(receipt: ScreenshotPublicationReceipt) -> EventRecord:
        return EventRecord(
            run_id=receipt.scope.run_id,
            task_id=receipt.scope.task_id,
            node_id=receipt.scope.node_id or None,
            event_type=EventType.BROWSER_RUNTIME_DIAGNOSTIC,
            payload={
                "browser_screenshot_evidence": receipt.to_dict(),
                "recovery_planner_owner": "M1-07C",
                "is_recovery_plan": False,
            },
        )


def _action_correlation(
    action_run: Any,
    events: Sequence[EventRecord],
) -> dict[str, Any]:
    receipts = tuple(
        getattr(action_run, "receipts", getattr(action_run, "action_receipts", ()))
    ) if action_run is not None else ()
    value = _mapping(receipts[-1]) if receipts else {}
    action_name = str(
        value.get("action") or value.get("tool_name") or value.get("name") or ""
    ).casefold()
    return {
        "action_id": str(value.get("action_id") or value.get("tool_call_id") or ""),
        "tool_call_id": str(value.get("tool_call_id") or value.get("action_id") or ""),
        "action_receipt_id": str(value.get("receipt_id") or ""),
        "source_event_ids": tuple(item.event_id for item in events),
        "screenshot_expected": action_name in {"screenshot", "capture_screenshot"},
    }


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "to_dict"):
        projected = value.to_dict()
        return dict(projected) if isinstance(projected, Mapping) else {}
    return {}


def _boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").casefold() in {"1", "true", "yes", "on"}
