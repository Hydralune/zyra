from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from .contracts import (
    AssertionSeverity,
    CaseExecutionBuffer,
    FailureKind,
    ObservationKind,
    canonical_json,
    stable_digest,
)


class ContentSurface(StrEnum):
    EVENT = "event"
    ARTIFACT = "artifact"
    API = "api"
    UI = "ui"
    SHARED_MEMORY = "shared_memory"
    WEB = "web"
    MCP = "mcp"
    BROWSER = "browser"


class TrustLevel(StrEnum):
    SYSTEM = "system"
    TRUSTED = "trusted"
    CONSTRAINED = "constrained"
    UNTRUSTED = "untrusted"


class AdmissionEffect(StrEnum):
    ACCEPT = "accept"
    REDACT = "redact"
    QUARANTINE = "quarantine"
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class ContentEnvelope:
    envelope_id: str
    surface: ContentSurface
    trust: TrustLevel
    source_id: str
    content: Any
    provenance_digest: str
    requested_capability: str = ""
    intended_effect: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def content_digest(self) -> str:
        return stable_digest(self.content)

    def to_dict(self, *, include_content: bool = False) -> dict[str, Any]:
        payload = {
            "envelope_id": self.envelope_id,
            "surface": self.surface.value,
            "trust": self.trust.value,
            "source_id": self.source_id,
            "content_digest": self.content_digest,
            "provenance_digest": self.provenance_digest,
            "requested_capability": self.requested_capability,
            "intended_effect": self.intended_effect,
            "metadata": dict(self.metadata),
        }
        if include_content:
            payload["content"] = self.content
        return payload


@dataclass(frozen=True, slots=True)
class ContentAdmission:
    envelope_id: str
    surface: ContentSurface
    effect: AdmissionEffect
    reason_code: str
    input_digest: str
    output_digest: str
    output: Any
    finding_codes: tuple[str, ...]
    redaction_count: int
    policy_digest: str
    owner_id: str

    @property
    def changed(self) -> bool:
        return self.input_digest != self.output_digest

    def to_dict(self, *, include_output: bool = False) -> dict[str, Any]:
        payload = {
            "envelope_id": self.envelope_id,
            "surface": self.surface.value,
            "effect": self.effect.value,
            "reason_code": self.reason_code,
            "input_digest": self.input_digest,
            "output_digest": self.output_digest,
            "changed": self.changed,
            "finding_codes": list(self.finding_codes),
            "redaction_count": self.redaction_count,
            "policy_digest": self.policy_digest,
            "owner_id": self.owner_id,
        }
        if include_output:
            payload["output"] = self.output
        return payload


@dataclass(frozen=True, slots=True)
class ContentFinding:
    code: str
    envelope_id: str
    surface: ContentSurface
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "envelope_id": self.envelope_id,
            "surface": self.surface.value,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ContentSecurityReport:
    envelopes: tuple[ContentEnvelope, ...]
    admissions: tuple[ContentAdmission, ...]
    findings: tuple[ContentFinding, ...]
    surface_counts: Mapping[str, int]
    effect_counts: Mapping[str, int]
    canary_digests: tuple[str, ...]
    digest: str

    @property
    def valid(self) -> bool:
        required = {
            ContentSurface.EVENT,
            ContentSurface.ARTIFACT,
            ContentSurface.API,
            ContentSurface.UI,
            ContentSurface.SHARED_MEMORY,
            ContentSurface.WEB,
            ContentSurface.MCP,
            ContentSurface.BROWSER,
        }
        observed = {item.surface for item in self.envelopes}
        return not self.findings and required.issubset(observed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.m3-content-security/v1",
            "valid": self.valid,
            "envelopes": [item.to_dict() for item in self.envelopes],
            "admissions": [item.to_dict() for item in self.admissions],
            "findings": [item.to_dict() for item in self.findings],
            "surface_counts": dict(sorted(self.surface_counts.items())),
            "effect_counts": dict(sorted(self.effect_counts.items())),
            "canary_digests": list(self.canary_digests),
            "digest": self.digest,
        }


class InjectionDetector:
    _RULE_MUTATION = re.compile(
        r"(?i)\b(?:ignore|override|replace|disable|rewrite|edit|change)\b"
        r"[\s\S]{0,80}\b(?:system|developer|permission|policy|rule|allowlist|denylist)\b"
    )
    _SECRET_REQUEST = re.compile(
        r"(?i)\b(?:print|reveal|exfiltrate|send|upload|return|show)\b"
        r"[\s\S]{0,80}\b(?:secret|token|password|credential|api[_ -]?key|environment)\b"
    )
    _TOOL_DIRECTIVE = re.compile(
        r"(?i)\b(?:run|execute|call|invoke|use)\b"
        r"[\s\S]{0,80}\b(?:shell|terminal|tool|mcp|browser|filesystem|git)\b"
    )
    _AUTHORITY_CLAIM = re.compile(
        r"(?i)\b(?:i am|this is|message from)\b"
        r"[\s\S]{0,40}\b(?:system|developer|administrator|owner|security team)\b"
    )
    _HIDDEN_DIRECTIVE = re.compile(
        r"(?i)(?:<!--[\s\S]*?(?:ignore|system|tool)[\s\S]*?-->|"
        r"<(?:script|style)[^>]*>[\s\S]*?</(?:script|style)>)"
    )

    def detect(self, value: Any) -> tuple[str, ...]:
        text = self._text(value)
        findings: list[str] = []
        for code, pattern in (
            ("rule_mutation", self._RULE_MUTATION),
            ("secret_exfiltration", self._SECRET_REQUEST),
            ("tool_directive", self._TOOL_DIRECTIVE),
            ("authority_claim", self._AUTHORITY_CLAIM),
            ("hidden_directive", self._HIDDEN_DIRECTIVE),
        ):
            if pattern.search(text):
                findings.append(code)
        return tuple(findings)

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return canonical_json(value)
        except (TypeError, ValueError):
            return str(value)


class ContentSecurityAuditor:
    _SECRET_PATTERNS = (
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
        re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
        re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{24,}\b"),
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password)"
            r"\s*[:=]\s*['\"]?[^\s,'\"]{8,}"
        ),
    )
    _OUTPUT_SURFACES = {
        ContentSurface.EVENT,
        ContentSurface.ARTIFACT,
        ContentSurface.API,
        ContentSurface.UI,
    }
    _UNTRUSTED_INPUT_SURFACES = {
        ContentSurface.WEB,
        ContentSurface.MCP,
        ContentSurface.BROWSER,
    }

    def __init__(self, *, canaries: Iterable[str]) -> None:
        self.canaries = tuple(
            sorted(
                {str(item) for item in canaries if len(str(item)) >= 4},
                key=len,
                reverse=True,
            )
        )
        if not self.canaries:
            raise ValueError("content security campaign requires secret canaries")
        self.injection_detector = InjectionDetector()

    def audit(
        self,
        envelopes: Sequence[ContentEnvelope],
        admissions: Sequence[ContentAdmission],
    ) -> ContentSecurityReport:
        admissions_by_id = {item.envelope_id: item for item in admissions}
        findings: list[ContentFinding] = []
        if len(admissions_by_id) != len(admissions):
            findings.append(
                ContentFinding(
                    code="duplicate_admission",
                    envelope_id="",
                    surface=ContentSurface.EVENT,
                    reason="multiple admissions use the same envelope identity",
                )
            )
        for envelope in envelopes:
            admission = admissions_by_id.get(envelope.envelope_id)
            if admission is None:
                findings.append(
                    self._finding(
                        "admission_missing",
                        envelope,
                        "content envelope has no admission receipt",
                    )
                )
                continue
            findings.extend(self._admission_findings(envelope, admission))
        orphan = set(admissions_by_id) - {item.envelope_id for item in envelopes}
        for envelope_id in sorted(orphan):
            admission = admissions_by_id[envelope_id]
            findings.append(
                ContentFinding(
                    code="orphan_admission",
                    envelope_id=envelope_id,
                    surface=admission.surface,
                    reason="admission has no input envelope",
                )
            )
        surface_counts = Counter(item.surface.value for item in envelopes)
        effect_counts = Counter(item.effect.value for item in admissions)
        material = {
            "envelopes": [item.to_dict() for item in envelopes],
            "admissions": [item.to_dict() for item in admissions],
            "findings": [item.to_dict() for item in findings],
            "canaries": [stable_digest("canary", item) for item in self.canaries],
        }
        return ContentSecurityReport(
            envelopes=tuple(envelopes),
            admissions=tuple(admissions),
            findings=tuple(findings),
            surface_counts=dict(surface_counts),
            effect_counts=dict(effect_counts),
            canary_digests=tuple(
                stable_digest("canary", item)
                for item in self.canaries
            ),
            digest=stable_digest(material),
        )

    def mutation_campaign(
        self,
        envelopes: Sequence[ContentEnvelope],
        admissions: Sequence[ContentAdmission],
    ) -> Mapping[str, ContentSecurityReport]:
        baseline = self.audit(envelopes, admissions)
        output_envelope = next(
            item for item in envelopes if item.surface in self._OUTPUT_SURFACES
        )
        output_admission = next(
            item for item in admissions if item.envelope_id == output_envelope.envelope_id
        )
        leak = replace(
            output_admission,
            output=f"leaked={self.canaries[0]}",
            output_digest=stable_digest(f"leaked={self.canaries[0]}"),
            effect=AdmissionEffect.ACCEPT,
            redaction_count=0,
            finding_codes=(),
        )
        memory_envelope = next(
            item for item in envelopes if item.surface is ContentSurface.SHARED_MEMORY
        )
        memory_admission = next(
            item for item in admissions if item.envelope_id == memory_envelope.envelope_id
        )
        memory_accept = replace(
            memory_admission,
            effect=AdmissionEffect.ACCEPT,
            output=memory_envelope.content,
            output_digest=memory_envelope.content_digest,
            reason_code="accepted",
        )
        injection_envelope = next(
            item
            for item in envelopes
            if item.surface in self._UNTRUSTED_INPUT_SURFACES
            and self.injection_detector.detect(item.content)
        )
        injection_admission = next(
            item for item in admissions if item.envelope_id == injection_envelope.envelope_id
        )
        injection_accept = replace(
            injection_admission,
            effect=AdmissionEffect.ACCEPT,
            output=injection_envelope.content,
            output_digest=injection_envelope.content_digest,
            reason_code="accepted",
        )
        wrong_owner = replace(
            output_admission,
            owner_id="ModelGateway",
        )
        missing_provenance = replace(
            injection_envelope,
            envelope_id=f"{injection_envelope.envelope_id}-provenance",
            provenance_digest="",
        )
        missing_provenance_admission = replace(
            injection_admission,
            envelope_id=missing_provenance.envelope_id,
        )
        return {
            "baseline": baseline,
            "surface_leak": self.audit(
                envelopes,
                tuple(leak if item is output_admission else item for item in admissions),
            ),
            "memory_secret_accepted": self.audit(
                envelopes,
                tuple(memory_accept if item is memory_admission else item for item in admissions),
            ),
            "injection_accepted": self.audit(
                envelopes,
                tuple(injection_accept if item is injection_admission else item for item in admissions),
            ),
            "wrong_owner": self.audit(
                envelopes,
                tuple(wrong_owner if item is output_admission else item for item in admissions),
            ),
            "missing_provenance": self.audit(
                (*envelopes, missing_provenance),
                (*admissions, missing_provenance_admission),
            ),
        }

    def _admission_findings(
        self,
        envelope: ContentEnvelope,
        admission: ContentAdmission,
    ) -> list[ContentFinding]:
        findings: list[ContentFinding] = []
        if admission.surface is not envelope.surface:
            findings.append(
                self._finding(
                    "surface_mismatch",
                    envelope,
                    "admission surface differs from input surface",
                )
            )
        if admission.input_digest != envelope.content_digest:
            findings.append(
                self._finding(
                    "input_digest_mismatch",
                    envelope,
                    "admission is not bound to input content",
                )
            )
        if not envelope.provenance_digest:
            findings.append(
                self._finding(
                    "provenance_missing",
                    envelope,
                    "content lacks provenance identity",
                )
            )
        if not admission.policy_digest:
            findings.append(
                self._finding(
                    "policy_digest_missing",
                    envelope,
                    "admission lacks policy digest",
                )
            )
        if admission.owner_id not in {
            "SecretRedactor",
            "SandboxGatewayRuntime",
            "MemoryWriteGuard",
            "BrowserSecurityPolicy",
            "McpCoordinatorRuntime",
            "EventProjectionRedactor",
        }:
            findings.append(
                self._finding(
                    "owner_invalid",
                    envelope,
                    f"unexpected security owner {admission.owner_id}",
                )
            )
        output_text = self._text(admission.output)
        canary_hits = [
            stable_digest("canary", item)
            for item in self.canaries
            if item in output_text
        ]
        secret_hits = [
            pattern.pattern
            for pattern in self._SECRET_PATTERNS
            if pattern.search(output_text)
        ]
        injection_hits = self.injection_detector.detect(envelope.content)
        if envelope.surface in self._OUTPUT_SURFACES:
            if canary_hits or secret_hits:
                findings.append(
                    self._finding(
                        "secret_leak",
                        envelope,
                        f"outbound surface contains secret material: {canary_hits}",
                    )
                )
            input_has_secret = bool(
                any(item in self._text(envelope.content) for item in self.canaries)
                or any(pattern.search(self._text(envelope.content)) for pattern in self._SECRET_PATTERNS)
            )
            if input_has_secret and admission.effect not in {
                AdmissionEffect.REDACT,
                AdmissionEffect.REJECT,
            }:
                findings.append(
                    self._finding(
                        "secret_not_redacted",
                        envelope,
                        "secret-bearing outbound content was not redacted/rejected",
                    )
                )
            if input_has_secret and admission.effect is AdmissionEffect.REDACT:
                if not admission.changed or admission.redaction_count < 1:
                    findings.append(
                        self._finding(
                            "redaction_ineffective",
                            envelope,
                            "redaction receipt reports no semantic change",
                        )
                    )
        if envelope.surface is ContentSurface.SHARED_MEMORY:
            input_has_secret = any(
                item in self._text(envelope.content)
                for item in self.canaries
            )
            if input_has_secret and admission.effect not in {
                AdmissionEffect.REJECT,
                AdmissionEffect.QUARANTINE,
            }:
                findings.append(
                    self._finding(
                        "secret_memory_write",
                        envelope,
                        "secret-bearing content entered shared memory",
                    )
                )
            if injection_hits and admission.effect not in {
                AdmissionEffect.REJECT,
                AdmissionEffect.QUARANTINE,
            }:
                findings.append(
                    self._finding(
                        "injection_memory_write",
                        envelope,
                        "instructional injection entered shared memory",
                    )
                )
        if (
            envelope.surface in self._UNTRUSTED_INPUT_SURFACES
            and envelope.trust is TrustLevel.UNTRUSTED
            and injection_hits
            and admission.effect not in {
                AdmissionEffect.REJECT,
                AdmissionEffect.QUARANTINE,
            }
        ):
            findings.append(
                self._finding(
                    "untrusted_injection_accepted",
                    envelope,
                    f"untrusted instruction was accepted: {list(injection_hits)}",
                )
            )
        if (
            envelope.trust is TrustLevel.UNTRUSTED
            and envelope.intended_effect.casefold()
            in {
                "permission_mutation",
                "policy_mutation",
                "system_prompt_mutation",
                "tool_execution",
            }
            and admission.effect not in {
                AdmissionEffect.REJECT,
                AdmissionEffect.QUARANTINE,
            }
        ):
            findings.append(
                self._finding(
                    "untrusted_effect_accepted",
                    envelope,
                    f"untrusted content gained {envelope.intended_effect}",
                )
            )
        return findings

    @staticmethod
    def _finding(
        code: str,
        envelope: ContentEnvelope,
        reason: str,
    ) -> ContentFinding:
        return ContentFinding(
            code=code,
            envelope_id=envelope.envelope_id,
            surface=envelope.surface,
            reason=reason,
        )

    @staticmethod
    def _text(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            return str(value)


def content_envelopes_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[ContentEnvelope, ...]:
    return tuple(
        ContentEnvelope(
            envelope_id=str(value.get("envelope_id") or ""),
            surface=ContentSurface(str(value.get("surface") or "")),
            trust=TrustLevel(str(value.get("trust") or "untrusted")),
            source_id=str(value.get("source_id") or ""),
            content=value.get("content"),
            provenance_digest=str(value.get("provenance_digest") or ""),
            requested_capability=str(value.get("requested_capability") or ""),
            intended_effect=str(value.get("intended_effect") or ""),
            metadata=(
                dict(value.get("metadata"))
                if isinstance(value.get("metadata"), Mapping)
                else {}
            ),
        )
        for value in values
    )


def content_admissions_from_mappings(
    values: Iterable[Mapping[str, Any]],
) -> tuple[ContentAdmission, ...]:
    return tuple(
        ContentAdmission(
            envelope_id=str(value.get("envelope_id") or ""),
            surface=ContentSurface(str(value.get("surface") or "")),
            effect=AdmissionEffect(str(value.get("effect") or "")),
            reason_code=str(value.get("reason_code") or ""),
            input_digest=str(value.get("input_digest") or ""),
            output_digest=str(value.get("output_digest") or ""),
            output=value.get("output"),
            finding_codes=tuple(str(item) for item in value.get("finding_codes") or ()),
            redaction_count=int(value.get("redaction_count") or 0),
            policy_digest=str(value.get("policy_digest") or ""),
            owner_id=str(value.get("owner_id") or ""),
        )
        for value in values
    )


def evaluate_content_security(
    envelopes: Sequence[ContentEnvelope],
    admissions: Sequence[ContentAdmission],
    *,
    canaries: Sequence[str],
) -> CaseExecutionBuffer:
    reports = ContentSecurityAuditor(canaries=canaries).mutation_campaign(
        envelopes,
        admissions,
    )
    buffer = CaseExecutionBuffer()
    baseline = reports["baseline"]
    base_observation = buffer.observe(
        "content-security.baseline",
        ObservationKind.SECURITY,
        "secret-redaction-and-untrusted-content",
        "verified" if baseline.valid else "invalid",
        attributes={
            "report_digest": baseline.digest,
            "surface_counts": baseline.surface_counts,
            "effect_counts": baseline.effect_counts,
            "canary_digests": list(baseline.canary_digests),
        },
    )
    buffer.assert_that(
        "content-security.baseline-valid",
        baseline.valid,
        "all secret and injection surfaces must enforce their admissions",
        evidence=(base_observation.observation_id,),
        failure_kind=FailureKind.SECURITY,
    )
    for name, report in reports.items():
        if name == "baseline":
            continue
        observation = buffer.observe(
            f"content-security.{name}",
            ObservationKind.MUTATION,
            f"content-security-negative:{name}",
            "mutation-rejected" if not report.valid else "mutation-accepted",
            attributes={
                "report_digest": report.digest,
                "finding_codes": [item.code for item in report.findings],
            },
        )
        buffer.assert_that(
            f"content-security.reject-{name}",
            not report.valid,
            f"{name} content-security mutation must be rejected",
            severity=AssertionSeverity.BLOCKER,
            evidence=(observation.observation_id,),
            failure_kind=FailureKind.SECURITY,
        )
    return buffer


__all__ = [
    "AdmissionEffect",
    "ContentAdmission",
    "ContentEnvelope",
    "ContentFinding",
    "ContentSecurityAuditor",
    "ContentSecurityReport",
    "ContentSurface",
    "InjectionDetector",
    "TrustLevel",
    "content_admissions_from_mappings",
    "content_envelopes_from_mappings",
    "evaluate_content_security",
]
