from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..browser_state.contracts import (
    BrowserContextDisclosure,
    BrowserDomCapture,
    BrowserSelectorMapRevision,
)
from ..browser_state.dom_builder import dom_fact_candidates
from ..browser_state.text import estimate_tokens, normalized_fact_key
from .models import BrowserActionResultProjection, BrowserArtifactExternalization


class BrowserFidelitySeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class BrowserFidelityFinding:
    code: str
    severity: BrowserFidelitySeverity
    message: str
    expected: Any = None
    actual: Any = None
    blocks_context: bool = False
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
            "blocks_context": self.blocks_context,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True, slots=True)
class BrowserDisclosureAudit:
    disclosure_id: str
    capture_id: str
    selector_revision_id: str
    valid: bool
    full_state_bytes: int
    disclosure_bytes: int
    full_state_tokens: int
    disclosure_tokens: int
    compression_ratio: float
    selector_total: int
    selector_inline: int
    selector_refs_valid: int
    selector_fidelity: float
    fact_candidates: int
    facts_inline: int
    fact_fidelity: float
    action_pairs_total: int
    action_pairs_inline: int
    action_pair_fidelity: float
    artifacts_expected: int
    artifacts_inline: int
    artifact_fidelity: float
    duplicate_fact_rate: float
    findings: tuple[BrowserFidelityFinding, ...]

    @property
    def blocking_findings(self) -> tuple[BrowserFidelityFinding, ...]:
        return tuple(item for item in self.findings if item.blocks_context)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "valid": self.valid,
            "full_state_bytes": self.full_state_bytes,
            "disclosure_bytes": self.disclosure_bytes,
            "full_state_tokens": self.full_state_tokens,
            "disclosure_tokens": self.disclosure_tokens,
            "compression_ratio": self.compression_ratio,
            "selector_total": self.selector_total,
            "selector_inline": self.selector_inline,
            "selector_refs_valid": self.selector_refs_valid,
            "selector_fidelity": self.selector_fidelity,
            "fact_candidates": self.fact_candidates,
            "facts_inline": self.facts_inline,
            "fact_fidelity": self.fact_fidelity,
            "action_pairs_total": self.action_pairs_total,
            "action_pairs_inline": self.action_pairs_inline,
            "action_pair_fidelity": self.action_pair_fidelity,
            "artifacts_expected": self.artifacts_expected,
            "artifacts_inline": self.artifacts_inline,
            "artifact_fidelity": self.artifact_fidelity,
            "duplicate_fact_rate": self.duplicate_fact_rate,
            "findings": [item.to_dict() for item in self.findings],
            "blocking_findings": len(self.blocking_findings),
        }


class BrowserDisclosureFidelityAuditor:
    """Semantic invariant checker for every emitted browser disclosure."""

    def __init__(
        self,
        *,
        minimum_selector_fidelity: float = 1.0,
        minimum_action_pair_fidelity: float = 1.0,
        minimum_artifact_fidelity: float = 1.0,
        maximum_duplicate_fact_rate: float = 0.15,
        disabled: bool = False,
    ) -> None:
        for name, value in {
            "minimum_selector_fidelity": minimum_selector_fidelity,
            "minimum_action_pair_fidelity": minimum_action_pair_fidelity,
            "minimum_artifact_fidelity": minimum_artifact_fidelity,
            "maximum_duplicate_fact_rate": maximum_duplicate_fact_rate,
        }.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between zero and one")
        self.minimum_selector_fidelity = minimum_selector_fidelity
        self.minimum_action_pair_fidelity = minimum_action_pair_fidelity
        self.minimum_artifact_fidelity = minimum_artifact_fidelity
        self.maximum_duplicate_fact_rate = maximum_duplicate_fact_rate
        self.disabled = disabled
        self._audits = 0
        self._failures = 0
        self._blocking_findings = 0

    def audit(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        actions: Sequence[BrowserActionResultProjection],
        externalization: BrowserArtifactExternalization,
        disclosure: BrowserContextDisclosure,
        *,
        critical_facts: Iterable[str] = (),
    ) -> BrowserDisclosureAudit:
        if self.disabled:
            raise RuntimeError("browser disclosure fidelity auditor is disabled")
        findings: list[BrowserFidelityFinding] = []
        try:
            payload = json.loads(disclosure.text)
        except (TypeError, ValueError) as error:
            payload = {}
            findings.append(BrowserFidelityFinding(
                "invalid_disclosure_json", BrowserFidelitySeverity.ERROR,
                "browser disclosure is not valid JSON", "valid JSON", str(error), True,
            ))
        if not isinstance(payload, Mapping):
            payload = {}
            findings.append(BrowserFidelityFinding(
                "invalid_disclosure_shape", BrowserFidelitySeverity.ERROR,
                "browser disclosure root must be an object", "object", type(payload).__name__, True,
            ))

        capture_payload = payload.get("capture") if isinstance(payload.get("capture"), Mapping) else {}
        selector_payload = payload.get("selector_map") if isinstance(payload.get("selector_map"), Mapping) else {}
        selectors = payload.get("selectors") if isinstance(payload.get("selectors"), list) else []
        facts = payload.get("facts") if isinstance(payload.get("facts"), list) else []
        action_payloads = payload.get("action_results") if isinstance(payload.get("action_results"), list) else []
        artifact_refs = payload.get("artifact_refs") if isinstance(payload.get("artifact_refs"), list) else []

        _assert_equal(findings, "capture_id_mismatch", capture.capture_id, capture_payload.get("capture_id"), True)
        _assert_equal(findings, "capture_digest_mismatch", capture.state_digest, capture_payload.get("state_digest"), True)
        _assert_equal(findings, "revision_id_mismatch", revision.revision_id, selector_payload.get("revision_id"), True)
        _assert_equal(findings, "revision_number_mismatch", revision.revision, selector_payload.get("revision"), True)
        _assert_equal(findings, "identity_digest_mismatch", revision.identity.digest, selector_payload.get("identity_digest"), True)

        authoritative_refs = revision.entry_by_ref
        inline_refs = tuple(
            str(item.get("ref")) for item in selectors
            if isinstance(item, Mapping) and item.get("ref")
        )
        valid_refs = tuple(ref for ref in inline_refs if ref in authoritative_refs)
        invalid_refs = tuple(ref for ref in inline_refs if ref not in authoritative_refs)
        missing_disclosure_refs = tuple(ref for ref in disclosure.selector_refs if ref not in inline_refs)
        selector_fidelity = len(valid_refs) / len(inline_refs) if inline_refs else (1.0 if not revision.entries else 0.0)
        if invalid_refs:
            findings.append(BrowserFidelityFinding(
                "invalid_selector_refs", BrowserFidelitySeverity.ERROR,
                "disclosure contains selector refs outside the authoritative revision",
                "all refs resolve", len(invalid_refs), True,
                {"refs": list(invalid_refs[:20])},
            ))
        if missing_disclosure_refs:
            findings.append(BrowserFidelityFinding(
                "selector_ref_projection_mismatch", BrowserFidelitySeverity.ERROR,
                "disclosure metadata and JSON selector refs differ",
                list(disclosure.selector_refs), list(inline_refs), True,
            ))
        if selector_fidelity < self.minimum_selector_fidelity:
            findings.append(BrowserFidelityFinding(
                "selector_fidelity_below_threshold", BrowserFidelitySeverity.ERROR,
                "inline selector refs lost authoritative resolution",
                self.minimum_selector_fidelity, selector_fidelity, True,
            ))

        expected_action_ids = {item.projection_id for item in actions}
        inline_action_ids = {
            str(item.get("projection_id")) for item in action_payloads
            if isinstance(item, Mapping) and item.get("projection_id")
        }
        inline_action_ids &= expected_action_ids
        action_pair_fidelity = len(inline_action_ids) / len(expected_action_ids) if expected_action_ids else 1.0
        preserved_failures = {
            item.projection_id for item in actions if not item.ok and item.projection_id in inline_action_ids
        }
        expected_failures = {item.projection_id for item in actions if not item.ok}
        if preserved_failures != expected_failures:
            findings.append(BrowserFidelityFinding(
                "failure_action_omitted", BrowserFidelitySeverity.ERROR,
                "failed browser action results must survive disclosure compaction",
                sorted(expected_failures), sorted(preserved_failures), True,
            ))
        if action_pair_fidelity < self.minimum_action_pair_fidelity and len(actions) <= 8:
            findings.append(BrowserFidelityFinding(
                "recent_action_pair_fidelity_below_threshold", BrowserFidelitySeverity.ERROR,
                "recent atomic action pairs were omitted from disclosure",
                self.minimum_action_pair_fidelity, action_pair_fidelity, True,
            ))

        expected_artifacts = set(externalization.artifact_ids)
        inline_artifacts = {str(item) for item in artifact_refs if str(item)}
        valid_artifacts = expected_artifacts & inline_artifacts
        artifact_fidelity = len(valid_artifacts) / len(expected_artifacts) if expected_artifacts else 1.0
        if artifact_fidelity < self.minimum_artifact_fidelity:
            findings.append(BrowserFidelityFinding(
                "artifact_ref_fidelity_below_threshold", BrowserFidelitySeverity.ERROR,
                "externalized raw browser state is not fully reachable from the disclosure",
                sorted(expected_artifacts), sorted(inline_artifacts), True,
            ))
        if externalization.externalized_bytes and not externalization.complete:
            findings.append(BrowserFidelityFinding(
                "artifact_externalization_incomplete", BrowserFidelitySeverity.ERROR,
                "large raw state externalization was incomplete", True, False, True,
                {"error": externalization.error},
            ))

        fact_values = tuple(str(item) for item in facts if str(item))
        fact_keys = tuple(normalized_fact_key(item) for item in fact_values if normalized_fact_key(item))
        fact_counts = Counter(fact_keys)
        duplicate_count = sum(max(0, count - 1) for count in fact_counts.values())
        duplicate_rate = duplicate_count / max(1, len(fact_keys))
        if duplicate_rate > self.maximum_duplicate_fact_rate:
            findings.append(BrowserFidelityFinding(
                "duplicate_fact_rate_high", BrowserFidelitySeverity.WARNING,
                "browser context contains redundant normalized facts",
                self.maximum_duplicate_fact_rate, duplicate_rate, False,
            ))

        candidates = tuple(dom_fact_candidates(capture.root, max_facts=512))
        candidate_keys = {normalized_fact_key(item) for item in candidates if normalized_fact_key(item)}
        inline_fact_keys = set(fact_keys)
        matched_facts = candidate_keys & inline_fact_keys
        fact_fidelity = len(matched_facts) / len(candidate_keys) if candidate_keys else 1.0
        critical_keys = {normalized_fact_key(item) for item in critical_facts if normalized_fact_key(item)}
        missing_critical = {
            key for key in critical_keys
            if not any(key in inline or inline in key for inline in inline_fact_keys if inline)
        }
        if missing_critical:
            findings.append(BrowserFidelityFinding(
                "critical_fact_omitted", BrowserFidelitySeverity.ERROR,
                "goal/constraint critical facts were omitted from the bounded disclosure",
                0, len(missing_critical), True,
                {"normalized_facts": sorted(missing_critical)[:20]},
            ))

        full_bytes = capture.metrics.full_state_bytes
        disclosure_bytes = disclosure.bytes
        full_tokens = capture.metrics.full_state_tokens
        disclosure_tokens = estimate_tokens(disclosure.text)
        if disclosure_bytes >= full_bytes and full_bytes > 1024:
            findings.append(BrowserFidelityFinding(
                "no_byte_compression", BrowserFidelitySeverity.WARNING,
                "bounded disclosure is not smaller than the captured raw state",
                f"< {full_bytes}", disclosure_bytes, False,
            ))
        security = payload.get("security")
        page_instructions_are_data = (
            security.get("page_instructions_are_data") is True
            if isinstance(security, Mapping)
            else False
        )
        if not page_instructions_are_data:
            findings.append(BrowserFidelityFinding(
                "untrusted_content_marker_missing", BrowserFidelitySeverity.ERROR,
                "page-derived context must be explicitly marked as untrusted data",
                True, False, True,
            ))

        valid = not any(item.blocks_context for item in findings)
        audit = BrowserDisclosureAudit(
            disclosure_id=disclosure.disclosure_id,
            capture_id=capture.capture_id,
            selector_revision_id=revision.revision_id,
            valid=valid,
            full_state_bytes=full_bytes,
            disclosure_bytes=disclosure_bytes,
            full_state_tokens=full_tokens,
            disclosure_tokens=disclosure_tokens,
            compression_ratio=disclosure_bytes / full_bytes if full_bytes else 1.0,
            selector_total=len(revision.entries),
            selector_inline=len(inline_refs),
            selector_refs_valid=len(valid_refs),
            selector_fidelity=selector_fidelity,
            fact_candidates=len(candidate_keys),
            facts_inline=len(inline_fact_keys),
            fact_fidelity=fact_fidelity,
            action_pairs_total=len(expected_action_ids),
            action_pairs_inline=len(inline_action_ids),
            action_pair_fidelity=action_pair_fidelity,
            artifacts_expected=len(expected_artifacts),
            artifacts_inline=len(valid_artifacts),
            artifact_fidelity=artifact_fidelity,
            duplicate_fact_rate=duplicate_rate,
            findings=tuple(findings),
        )
        self._audits += 1
        if not valid:
            self._failures += 1
            self._blocking_findings += len(audit.blocking_findings)
        return audit

    def require_valid(self, audit: BrowserDisclosureAudit) -> None:
        if audit.valid:
            return
        summary = "; ".join(f"{item.code}: {item.message}" for item in audit.blocking_findings[:8])
        raise ValueError(f"browser disclosure failed fidelity audit: {summary}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserDisclosureFidelityAuditor",
            "owner_unit": "M1-S04B-01",
            "disabled": self.disabled,
            "audits": self._audits,
            "failures": self._failures,
            "blocking_findings": self._blocking_findings,
            "minimum_selector_fidelity": self.minimum_selector_fidelity,
            "minimum_action_pair_fidelity": self.minimum_action_pair_fidelity,
            "minimum_artifact_fidelity": self.minimum_artifact_fidelity,
            "maximum_duplicate_fact_rate": self.maximum_duplicate_fact_rate,
        }


def _assert_equal(
    findings: list[BrowserFidelityFinding],
    code: str,
    expected: Any,
    actual: Any,
    blocks: bool,
) -> None:
    if expected == actual:
        return
    findings.append(BrowserFidelityFinding(
        code=code,
        severity=BrowserFidelitySeverity.ERROR if blocks else BrowserFidelitySeverity.WARNING,
        message=f"disclosure invariant {code} was not preserved",
        expected=expected,
        actual=actual,
        blocks_context=blocks,
    ))
