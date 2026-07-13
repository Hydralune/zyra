from __future__ import annotations

"""Causal event construction and validation for browser message turns."""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactRef, EventRecord, EventType, to_jsonable

from ..browser_state.contracts import BrowserDomCapture, BrowserSelectorMapRevision, digest_json, state_id


class BrowserCausalFindingSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrowserArtifactEvidence:
    artifact_id: str
    artifact_kind: str
    title: str
    uri: str
    size_bytes: int
    digest: str
    capture_id: str
    selector_revision_id: str
    action_receipt_ids: tuple[str, ...]
    producer_event_id: str
    verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_kind": self.artifact_kind,
            "title": self.title,
            "uri": self.uri,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "action_receipt_ids": list(self.action_receipt_ids),
            "producer_event_id": self.producer_event_id,
            "verified": self.verified,
        }


@dataclass(frozen=True, slots=True)
class BrowserCausalFinding:
    code: str
    severity: BrowserCausalFindingSeverity
    message: str
    blocks_turn: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "blocks_turn": self.blocks_turn,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BrowserTurnCausalAudit:
    audit_id: str
    capture_id: str
    selector_revision_id: str
    source_event_id: str
    capture_event_id: str
    artifact_event_ids: tuple[str, ...]
    disclosure_event_id: str
    action_receipt_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    memory_candidate_ids: tuple[str, ...]
    event_ids: tuple[str, ...]
    findings: tuple[BrowserCausalFinding, ...]

    @property
    def blocking_findings(self) -> tuple[BrowserCausalFinding, ...]:
        return tuple(item for item in self.findings if item.blocks_turn)

    @property
    def valid(self) -> bool:
        return not self.blocking_findings

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-turn-causal-audit.v1",
            "audit_id": self.audit_id,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "source_event_id": self.source_event_id,
            "capture_event_id": self.capture_event_id,
            "artifact_event_ids": list(self.artifact_event_ids),
            "disclosure_event_id": self.disclosure_event_id,
            "action_receipt_ids": list(self.action_receipt_ids),
            "artifact_ids": list(self.artifact_ids),
            "memory_candidate_ids": list(self.memory_candidate_ids),
            "event_ids": list(self.event_ids),
            "valid": self.valid,
            "findings": [item.to_dict() for item in self.findings],
            "blocking_findings": len(self.blocking_findings),
        }


@dataclass(frozen=True, slots=True)
class BrowserCausalEventBundle:
    capture_event: EventRecord
    artifact_events: tuple[EventRecord, ...]
    disclosure_event: EventRecord
    artifact_evidence: tuple[BrowserArtifactEvidence, ...]
    audit: BrowserTurnCausalAudit

    @property
    def events(self) -> tuple[EventRecord, ...]:
        return (self.capture_event, *self.artifact_events, self.disclosure_event)

    def to_dict(self) -> dict[str, Any]:
        return {
            "capture_event": to_jsonable(self.capture_event),
            "artifact_events": [to_jsonable(item) for item in self.artifact_events],
            "disclosure_event": to_jsonable(self.disclosure_event),
            "artifact_evidence": [item.to_dict() for item in self.artifact_evidence],
            "audit": self.audit.to_dict(),
        }


class BrowserTurnCausalRuntime:
    """Create capture→artifact→disclosure events and verify every edge."""

    def __init__(self, artifact_store: Any, *, disabled: bool = False) -> None:
        self.artifact_store = artifact_store
        self.disabled = disabled
        self._bundles = 0
        self._artifacts = 0
        self._failures = 0

    def build(
        self,
        *,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        artifacts: Sequence[ArtifactRef],
        disclosure: Mapping[str, Any],
        actions: Sequence[Mapping[str, Any]],
        context_receipt: Mapping[str, Any],
        fidelity_audit: Mapping[str, Any],
        history_projection: Mapping[str, Any],
        semantic_outline: Mapping[str, Any],
        dom_delta: Mapping[str, Any],
        action_envelope: Mapping[str, Any],
        ablation: Mapping[str, Any],
        selector_probe_audit: Mapping[str, Any],
        source_event_id: str,
    ) -> BrowserCausalEventBundle:
        if self.disabled:
            raise RuntimeError("browser turn causal runtime is disabled")
        action_receipt_ids = tuple(
            dict.fromkeys(str(item.get("receipt_id") or "") for item in actions if item.get("receipt_id"))
        )
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
                "action_receipt_ids": list(action_receipt_ids),
            },
        )
        artifact_events: list[EventRecord] = []
        evidence: list[BrowserArtifactEvidence] = []
        for artifact in artifacts:
            item = self._artifact_evidence(
                artifact,
                capture=capture,
                revision=revision,
                action_receipt_ids=action_receipt_ids,
                producer_event_id=capture_event.event_id,
            )
            evidence.append(item)
            artifact_events.append(EventRecord(
                run_id=capture.request.run_id,
                task_id=capture.request.task_id,
                node_id=capture.request.node_id or None,
                event_type=EventType.ARTIFACT_WRITTEN,
                payload={
                    "browser_artifact": item.to_dict(),
                    "cause_event_id": capture_event.event_id,
                    "cause_event_ids": [capture_event.event_id, *([source_event_id] if source_event_id else [])],
                },
            ))
        artifact_event_ids = tuple(item.event_id for item in artifact_events)
        disclosure_event = EventRecord(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            node_id=capture.request.node_id or None,
            event_type=EventType.AGENT_MESSAGE,
            payload={
                "browser_context_disclosure": dict(disclosure),
                "browser_low_entropy_metrics": dict(_mapping(disclosure.get("metrics"))),
                "browser_action_result_projections": [dict(item) for item in actions],
                "browser_next_context": dict(context_receipt),
                "browser_disclosure_fidelity": dict(fidelity_audit),
                "browser_history_projection": dict(history_projection),
                "browser_semantic_outline": dict(semantic_outline),
                "browser_dom_delta": dict(dom_delta),
                "browser_action_envelope": dict(action_envelope),
                "browser_context_ablation": dict(ablation),
                "browser_live_selector_probe": dict(selector_probe_audit),
                "browser_artifact_evidence": [item.to_dict() for item in evidence],
                "cause_event_id": capture_event.event_id,
                "cause_event_ids": [capture_event.event_id, *artifact_event_ids],
                "action_receipt_ids": list(action_receipt_ids),
            },
        )
        audit = self.audit(
            capture=capture,
            revision=revision,
            source_event_id=source_event_id,
            capture_event=capture_event,
            artifact_events=artifact_events,
            disclosure_event=disclosure_event,
            artifact_evidence=evidence,
            action_receipt_ids=action_receipt_ids,
        )
        if not audit.valid:
            self._failures += 1
            raise RuntimeError(
                "browser causal event bundle failed validation: "
                + "; ".join(item.code for item in audit.blocking_findings)
            )
        self._bundles += 1
        self._artifacts += len(evidence)
        return BrowserCausalEventBundle(
            capture_event=capture_event,
            artifact_events=tuple(artifact_events),
            disclosure_event=disclosure_event,
            artifact_evidence=tuple(evidence),
            audit=audit,
        )

    def audit(
        self,
        *,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        source_event_id: str,
        capture_event: EventRecord,
        artifact_events: Sequence[EventRecord],
        disclosure_event: EventRecord,
        artifact_evidence: Sequence[BrowserArtifactEvidence],
        action_receipt_ids: Sequence[str],
    ) -> BrowserTurnCausalAudit:
        findings: list[BrowserCausalFinding] = []
        if capture_event.run_id != capture.request.run_id or capture_event.task_id != capture.request.task_id:
            findings.append(BrowserCausalFinding(
                "capture_event_scope_mismatch",
                BrowserCausalFindingSeverity.ERROR,
                "capture event left the browser turn run/task scope",
                True,
            ))
        capture_payload = _mapping(capture_event.payload.get("browser_dom_state"))
        if str(capture_payload.get("capture_id") or "") != capture.capture_id:
            findings.append(BrowserCausalFinding(
                "capture_event_identity_mismatch",
                BrowserCausalFindingSeverity.ERROR,
                "capture event no longer points to the captured DOM state",
                True,
            ))
        selector_payload = _mapping(capture_event.payload.get("selector_revision"))
        if str(selector_payload.get("revision_id") or "") != revision.revision_id:
            findings.append(BrowserCausalFinding(
                "selector_event_identity_mismatch",
                BrowserCausalFindingSeverity.ERROR,
                "capture event no longer points to the committed selector revision",
                True,
            ))
        artifact_event_ids = {item.event_id for item in artifact_events}
        artifact_ids = {item.artifact_id for item in artifact_evidence}
        if len(artifact_event_ids) != len(artifact_events):
            findings.append(BrowserCausalFinding(
                "duplicate_artifact_event_id",
                BrowserCausalFindingSeverity.ERROR,
                "artifact events must have unique identities",
                True,
            ))
        if len(artifact_ids) != len(artifact_evidence):
            findings.append(BrowserCausalFinding(
                "duplicate_artifact_evidence",
                BrowserCausalFindingSeverity.ERROR,
                "each browser artifact must have exactly one evidence event",
                True,
            ))
        for event, item in zip(artifact_events, artifact_evidence, strict=True):
            if event.event_type != EventType.ARTIFACT_WRITTEN:
                findings.append(BrowserCausalFinding(
                    "artifact_event_type_invalid",
                    BrowserCausalFindingSeverity.ERROR,
                    "browser artifact evidence must use ARTIFACT_WRITTEN",
                    True,
                    {"event_id": event.event_id},
                ))
            if str(event.payload.get("cause_event_id") or "") != capture_event.event_id:
                findings.append(BrowserCausalFinding(
                    "artifact_cause_missing",
                    BrowserCausalFindingSeverity.ERROR,
                    "browser artifact event must be caused by the capture event",
                    True,
                    {"artifact_id": item.artifact_id},
                ))
            if not item.verified:
                findings.append(BrowserCausalFinding(
                    "artifact_roundtrip_unverified",
                    BrowserCausalFindingSeverity.ERROR,
                    "browser artifact bytes were not verified after writing",
                    True,
                    {"artifact_id": item.artifact_id},
                ))
        causes = set(_strings(disclosure_event.payload.get("cause_event_ids")))
        expected_causes = {capture_event.event_id, *artifact_event_ids}
        missing_causes = expected_causes - causes
        if missing_causes:
            findings.append(BrowserCausalFinding(
                "disclosure_cause_incomplete",
                BrowserCausalFindingSeverity.ERROR,
                "disclosure event omitted capture or artifact causes",
                True,
                {"missing_event_ids": sorted(missing_causes)},
            ))
        disclosure = _mapping(disclosure_event.payload.get("browser_context_disclosure"))
        disclosure_artifacts = set(_strings(disclosure.get("artifact_ids")))
        # Ablation/action artifacts may be turn artifacts without being part of
        # the deterministic structured disclosure. State artifacts must remain
        # reachable and are a blocking invariant.
        state_artifacts = {
            item.artifact_id for item in artifact_evidence
            if "Browser DOM state" in item.title or "Browser enhanced DOM" in item.title
            or "Browser state disclosure report" in item.title
        }
        missing_artifacts = state_artifacts - disclosure_artifacts
        if missing_artifacts:
            findings.append(BrowserCausalFinding(
                "disclosure_state_artifact_unreachable",
                BrowserCausalFindingSeverity.ERROR,
                "structured disclosure lost an authoritative state artifact reference",
                True,
                {"artifact_ids": sorted(missing_artifacts)},
            ))
        return BrowserTurnCausalAudit(
            audit_id=state_id("brcausalaudit"),
            capture_id=capture.capture_id,
            selector_revision_id=revision.revision_id,
            source_event_id=source_event_id,
            capture_event_id=capture_event.event_id,
            artifact_event_ids=tuple(item.event_id for item in artifact_events),
            disclosure_event_id=disclosure_event.event_id,
            action_receipt_ids=tuple(action_receipt_ids),
            artifact_ids=tuple(item.artifact_id for item in artifact_evidence),
            memory_candidate_ids=(),
            event_ids=(capture_event.event_id, *(item.event_id for item in artifact_events), disclosure_event.event_id),
            findings=tuple(findings),
        )

    def _artifact_evidence(
        self,
        artifact: ArtifactRef,
        *,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        action_receipt_ids: Sequence[str],
        producer_event_id: str,
    ) -> BrowserArtifactEvidence:
        try:
            path = self.artifact_store.resolve_path(artifact)
            payload = path.read_bytes()
            digest = "sha256:" + hashlib.sha256(payload).hexdigest()
            size = len(payload)
            verified = size == path.stat().st_size
        except Exception:
            digest = ""
            size = 0
            verified = False
        return BrowserArtifactEvidence(
            artifact_id=artifact.artifact_id,
            artifact_kind=str(artifact.kind),
            title=artifact.title,
            uri=artifact.uri,
            size_bytes=size,
            digest=digest,
            capture_id=capture.capture_id,
            selector_revision_id=revision.revision_id,
            action_receipt_ids=tuple(action_receipt_ids),
            producer_event_id=producer_event_id,
            verified=verified,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserTurnCausalRuntime",
            "owner_unit": "M1-S04B-02",
            "event_owner": "canonical event log",
            "artifact_owner": type(self.artifact_store).__name__,
            "disabled": self.disabled,
            "bundles": self._bundles,
            "artifacts": self._artifacts,
            "failures": self._failures,
        }


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(dict.fromkeys(str(item) for item in value if str(item)))
    return ()
