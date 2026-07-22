from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, utc_now
from .integration_contracts import parse_timestamp, redact_mapping, stable_digest


class EvidenceKind(StrEnum):
    SCENARIO = "scenario"
    DISCONNECT = "disconnect"
    CANONICAL_EVENT = "canonical_event"
    STATE_CUSTODY = "state_custody"
    SOURCE_COVERAGE = "source_coverage"
    LINE_AUDIT = "line_audit"
    CLEANROOM = "cleanroom"
    EXECUTION_TIER = "execution_tier"
    PROVIDER_WIRE = "provider_wire"
    BENCHMARK = "benchmark"
    HANDOFF = "handoff"


class EvidenceOrigin(StrEnum):
    LIVE_RUNTIME = "live_runtime"
    LOCAL_AUDITOR = "local_auditor"
    INDEPENDENT_ENDPOINT = "independent_endpoint"
    GIT_OBJECT = "git_object"
    IMPORTED = "imported"
    FIXTURE = "fixture"
    UNKNOWN = "unknown"


class AdmissionStatus(StrEnum):
    ADMITTED = "admitted"
    QUARANTINED = "quarantined"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class EvidenceEnvelope:
    evidence_id: str
    kind: EvidenceKind
    origin: EvidenceOrigin
    produced_at: str
    producer: str
    run_id: str
    task_id: str
    baseline_commit: str
    target_commit: str
    content: Mapping[str, Any]
    content_digest: str
    previous_digest: str = ""
    signature_algorithm: str = "none"
    signature: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceEnvelope":
        content = dict(value.get("content") or {}) if isinstance(value.get("content"), Mapping) else {}
        return cls(
            evidence_id=str(value.get("evidence_id") or ""),
            kind=_enum(EvidenceKind, value.get("kind"), EvidenceKind.CANONICAL_EVENT),
            origin=_enum(EvidenceOrigin, value.get("origin"), EvidenceOrigin.UNKNOWN),
            produced_at=str(value.get("produced_at") or ""),
            producer=str(value.get("producer") or ""),
            run_id=str(value.get("run_id") or ""),
            task_id=str(value.get("task_id") or ""),
            baseline_commit=str(value.get("baseline_commit") or ""),
            target_commit=str(value.get("target_commit") or ""),
            content=content,
            content_digest=str(value.get("content_digest") or ""),
            previous_digest=str(value.get("previous_digest") or ""),
            signature_algorithm=str(value.get("signature_algorithm") or "none"),
            signature=str(value.get("signature") or ""),
            metadata=dict(value.get("metadata") or {}) if isinstance(value.get("metadata"), Mapping) else {},
        )

    def signing_payload(self) -> Mapping[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "origin": self.origin.value,
            "produced_at": self.produced_at,
            "producer": self.producer,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "baseline_commit": self.baseline_commit,
            "target_commit": self.target_commit,
            "content_digest": self.content_digest,
            "previous_digest": self.previous_digest,
        }

    def envelope_digest(self) -> str:
        return stable_digest(self.signing_payload())

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        value = {
            **self.signing_payload(),
            "signature_algorithm": self.signature_algorithm,
            "signature": self.signature,
            "metadata": redact_mapping(self.metadata),
            "envelope_digest": self.envelope_digest(),
        }
        if include_content:
            value["content"] = redact_mapping(self.content)
        return value


@dataclass(frozen=True, slots=True)
class AdmissionPolicy:
    required_kinds: tuple[EvidenceKind, ...] = tuple(EvidenceKind)
    allowed_origins: tuple[EvidenceOrigin, ...] = (
        EvidenceOrigin.LIVE_RUNTIME,
        EvidenceOrigin.LOCAL_AUDITOR,
        EvidenceOrigin.INDEPENDENT_ENDPOINT,
        EvidenceOrigin.GIT_OBJECT,
    )
    maximum_age_seconds: float = 86_400.0 * 7
    require_full_commits: bool = True
    require_chain_for_kinds: tuple[EvidenceKind, ...] = (
        EvidenceKind.CANONICAL_EVENT,
        EvidenceKind.DISCONNECT,
        EvidenceKind.BENCHMARK,
    )
    require_signature_for_origins: tuple[EvidenceOrigin, ...] = (
        EvidenceOrigin.INDEPENDENT_ENDPOINT,
    )
    maximum_content_bytes: int = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AdmissionReceipt:
    evidence_id: str
    kind: EvidenceKind
    origin: EvidenceOrigin
    status: AdmissionStatus
    envelope_digest: str
    content_digest: str
    reasons: tuple[str, ...]
    admitted_at: str
    run_id: str
    task_id: str
    target_commit: str

    @property
    def admitted(self) -> bool:
        return self.status is AdmissionStatus.ADMITTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind.value,
            "origin": self.origin.value,
            "status": self.status.value,
            "envelope_digest": self.envelope_digest,
            "content_digest": self.content_digest,
            "reasons": list(self.reasons),
            "admitted_at": self.admitted_at,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "target_commit": self.target_commit,
            "admitted": self.admitted,
        }


class EvidenceSigner:
    ALGORITHM = "hmac-sha256"

    @classmethod
    def sign(cls, envelope: EvidenceEnvelope, key: bytes) -> str:
        if len(key) < 32:
            raise ValueError("evidence signing key must contain at least 256 bits")
        payload = json.dumps(
            envelope.signing_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(key, payload, hashlib.sha256).hexdigest()

    @classmethod
    def verify(cls, envelope: EvidenceEnvelope, key: bytes) -> bool:
        if envelope.signature_algorithm != cls.ALGORITHM:
            return False
        expected = cls.sign(envelope, key)
        return hmac.compare_digest(expected, envelope.signature)


class EvidenceAdmissionController:
    def __init__(
        self,
        policy: AdmissionPolicy | None = None,
        *,
        verification_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        self.policy = policy or AdmissionPolicy()
        self.verification_keys = dict(verification_keys or {})

    def admit(
        self,
        envelopes: Sequence[EvidenceEnvelope | Mapping[str, Any]],
        *,
        expected_target_commit: str,
        final_completion: bool,
    ) -> tuple[list[AdmissionReceipt], GateResult]:
        values = [
            item if isinstance(item, EvidenceEnvelope) else EvidenceEnvelope.from_mapping(item)
            for item in envelopes
        ]
        receipts: list[AdmissionReceipt] = []
        seen_ids: set[str] = set()
        seen_digests: set[str] = set()
        previous_by_partition: dict[tuple[str, str, EvidenceKind], str] = {}
        for envelope in values:
            reasons = self._validate(
                envelope,
                expected_target_commit=expected_target_commit,
                seen_ids=seen_ids,
                seen_digests=seen_digests,
                previous_by_partition=previous_by_partition,
            )
            if reasons:
                rejected = any(
                    reason.startswith(("content_digest", "fixture", "duplicate", "signature", "chain_fork"))
                    for reason in reasons
                )
                status = AdmissionStatus.REJECTED if rejected else AdmissionStatus.QUARANTINED
            else:
                status = AdmissionStatus.ADMITTED
            receipt = AdmissionReceipt(
                evidence_id=envelope.evidence_id,
                kind=envelope.kind,
                origin=envelope.origin,
                status=status,
                envelope_digest=envelope.envelope_digest(),
                content_digest=envelope.content_digest,
                reasons=tuple(reasons),
                admitted_at=utc_now(),
                run_id=envelope.run_id,
                task_id=envelope.task_id,
                target_commit=envelope.target_commit,
            )
            receipts.append(receipt)
            if status is AdmissionStatus.ADMITTED:
                seen_ids.add(envelope.evidence_id)
                seen_digests.add(envelope.content_digest)
                partition = (envelope.run_id, envelope.task_id, envelope.kind)
                previous_by_partition[partition] = envelope.envelope_digest()
        gate = self._gate(receipts, final_completion=final_completion)
        return receipts, gate

    def _validate(
        self,
        envelope: EvidenceEnvelope,
        *,
        expected_target_commit: str,
        seen_ids: set[str],
        seen_digests: set[str],
        previous_by_partition: Mapping[tuple[str, str, EvidenceKind], str],
    ) -> list[str]:
        reasons: list[str] = []
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,180}", envelope.evidence_id):
            reasons.append("evidence_identity_invalid")
        if envelope.evidence_id in seen_ids:
            reasons.append("duplicate_evidence_identity")
        if envelope.content_digest in seen_digests:
            reasons.append("duplicate_content_digest")
        actual_digest = stable_digest(envelope.content)
        if envelope.content_digest != actual_digest:
            reasons.append("content_digest_mismatch")
        size = len(
            json.dumps(envelope.content, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
        )
        if size > self.policy.maximum_content_bytes:
            reasons.append("content_size_exceeds_policy")
        if envelope.origin not in self.policy.allowed_origins:
            reasons.append(f"origin_not_allowed:{envelope.origin.value}")
        if envelope.origin is EvidenceOrigin.FIXTURE or self._looks_like_fixture(envelope):
            reasons.append("fixture_evidence_rejected")
        if not envelope.producer:
            reasons.append("producer_missing")
        if envelope.kind in {
            EvidenceKind.SCENARIO,
            EvidenceKind.DISCONNECT,
            EvidenceKind.CANONICAL_EVENT,
            EvidenceKind.BENCHMARK,
        } and (not envelope.run_id or not envelope.task_id):
            reasons.append("runtime_partition_missing")
        if self.policy.require_full_commits:
            if not re.fullmatch(r"[0-9a-f]{40}", envelope.target_commit.lower()):
                reasons.append("target_commit_invalid")
            if envelope.baseline_commit and not re.fullmatch(r"[0-9a-f]{40}", envelope.baseline_commit.lower()):
                reasons.append("baseline_commit_invalid")
        if expected_target_commit and envelope.target_commit != expected_target_commit:
            reasons.append("target_commit_mismatch")
        produced = parse_timestamp(envelope.produced_at)
        now = parse_timestamp(utc_now())
        if produced is None or now is None:
            reasons.append("produced_timestamp_invalid")
        elif (now - produced).total_seconds() > self.policy.maximum_age_seconds:
            reasons.append("evidence_too_old")
        elif produced > now:
            reasons.append("evidence_timestamp_in_future")
        if envelope.kind in self.policy.require_chain_for_kinds:
            partition = (envelope.run_id, envelope.task_id, envelope.kind)
            expected_previous = previous_by_partition.get(partition, "")
            if expected_previous and envelope.previous_digest != expected_previous:
                reasons.append("chain_fork_or_gap")
            if not expected_previous and envelope.previous_digest:
                reasons.append("chain_unknown_parent")
        if envelope.origin in self.policy.require_signature_for_origins:
            key = self.verification_keys.get(envelope.producer)
            if key is None:
                reasons.append("signature_verification_key_missing")
            elif not EvidenceSigner.verify(envelope, key):
                reasons.append("signature_invalid")
        return reasons

    @staticmethod
    def _looks_like_fixture(envelope: EvidenceEnvelope) -> bool:
        metadata_text = json.dumps(envelope.metadata, ensure_ascii=False, default=str).lower()
        content_text = json.dumps(envelope.content, ensure_ascii=False, default=str).lower()
        tokens = ("fixture", "golden", "recorded_trace", "mock_provider", "fake_endpoint", "sample-only")
        return envelope.metadata.get("fixture") is True or any(
            token in metadata_text or token in content_text[:4096] for token in tokens
        )

    def _gate(self, receipts: Sequence[AdmissionReceipt], *, final_completion: bool) -> GateResult:
        result = GateResult(
            gate_id="m1-evidence-admission",
            status=GateStatus.NOT_RUN,
            summary="Cryptographic provenance and anti-fixture admission for M1 completion evidence.",
        )
        by_kind: dict[EvidenceKind, list[AdmissionReceipt]] = defaultdict(list)
        for receipt in receipts:
            by_kind[receipt.kind].append(receipt)
            if receipt.status is not AdmissionStatus.ADMITTED:
                severity = Severity.BLOCKER if final_completion or receipt.status is AdmissionStatus.REJECTED else Severity.WARNING
                result.add(
                    Finding(
                        code="evidence.not_admitted",
                        severity=severity,
                        summary="M1 evidence failed provenance/admission policy.",
                        detail=f"{receipt.evidence_id}: {', '.join(receipt.reasons)}",
                        location=receipt.evidence_id,
                    )
                )
            else:
                result.evidence.append(
                    EvidencePointer(
                        kind=f"admitted_{receipt.kind.value}",
                        location=receipt.evidence_id,
                        summary=f"{receipt.origin.value} evidence admitted",
                        revision=receipt.target_commit,
                        causation_id=receipt.run_id,
                        metadata={
                            "content_digest": receipt.content_digest,
                            "envelope_digest": receipt.envelope_digest,
                        },
                    )
                )
        if final_completion:
            for kind in self.policy.required_kinds:
                admitted = [item for item in by_kind.get(kind, ()) if item.admitted]
                if not admitted:
                    result.add(
                        Finding(
                            code="evidence.required_kind_missing",
                            severity=Severity.BLOCKER,
                            summary="Final M1 evidence lacks an admitted required evidence kind.",
                            detail=kind.value,
                        )
                    )
        status_counts = Counter(item.status.value for item in receipts)
        result.metrics.update(
            {
                "receipt_count": len(receipts),
                "status_counts": dict(status_counts),
                "kind_counts": dict(Counter(item.kind.value for item in receipts)),
                "origin_counts": dict(Counter(item.origin.value for item in receipts)),
                "admitted_kind_count": len(
                    {item.kind for item in receipts if item.status is AdmissionStatus.ADMITTED}
                ),
                "receipts": [item.to_dict() for item in receipts],
            }
        )
        return result.finish(default_partial=not final_completion)


class EvidenceEnvelopeFactory:
    def create(
        self,
        *,
        evidence_id: str,
        kind: EvidenceKind,
        origin: EvidenceOrigin,
        producer: str,
        content: Mapping[str, Any],
        baseline_commit: str,
        target_commit: str,
        run_id: str = "",
        task_id: str = "",
        previous_digest: str = "",
        metadata: Mapping[str, Any] | None = None,
        signing_key: bytes | None = None,
    ) -> EvidenceEnvelope:
        envelope = EvidenceEnvelope(
            evidence_id=evidence_id,
            kind=kind,
            origin=origin,
            produced_at=utc_now(),
            producer=producer,
            run_id=run_id,
            task_id=task_id,
            baseline_commit=baseline_commit,
            target_commit=target_commit,
            content=dict(content),
            content_digest=stable_digest(content),
            previous_digest=previous_digest,
            signature_algorithm=EvidenceSigner.ALGORITHM if signing_key else "none",
            signature="",
            metadata=dict(metadata or {}),
        )
        if not signing_key:
            return envelope
        signature = EvidenceSigner.sign(envelope, signing_key)
        return EvidenceEnvelope(
            evidence_id=envelope.evidence_id,
            kind=envelope.kind,
            origin=envelope.origin,
            produced_at=envelope.produced_at,
            producer=envelope.producer,
            run_id=envelope.run_id,
            task_id=envelope.task_id,
            baseline_commit=envelope.baseline_commit,
            target_commit=envelope.target_commit,
            content=envelope.content,
            content_digest=envelope.content_digest,
            previous_digest=envelope.previous_digest,
            signature_algorithm=envelope.signature_algorithm,
            signature=signature,
            metadata=envelope.metadata,
        )


class EvidenceVault:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def persist(
        self,
        envelope: EvidenceEnvelope,
        receipt: AdmissionReceipt,
    ) -> Path:
        if not receipt.admitted:
            raise ValueError("rejected/quarantined evidence cannot enter the admitted vault")
        if receipt.evidence_id != envelope.evidence_id:
            raise ValueError("admission receipt does not match evidence envelope")
        safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", envelope.evidence_id)
        target = (self.root / envelope.kind.value / f"{safe_id}.json").resolve()
        try:
            target.relative_to(self.root)
        except ValueError as error:
            raise ValueError("evidence vault path escapes root") from error
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "zyra.m1-admitted-evidence/v1",
            "envelope": envelope.to_dict(include_content=True),
            "admission": receipt.to_dict(),
        }
        payload["record_digest"] = stable_digest(payload)
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing.get("record_digest") == payload["record_digest"]:
                return target
            raise RuntimeError("immutable admitted evidence id already exists with different content")
        temporary = target.with_name(f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        temporary.replace(target)
        return target

    def verify(self) -> GateResult:
        result = GateResult(
            gate_id="m1-evidence-vault",
            status=GateStatus.NOT_RUN,
            summary="Immutable admitted-evidence vault integrity.",
        )
        count = 0
        by_kind: Counter[str] = Counter()
        for path in sorted(self.root.rglob("*.json")):
            count += 1
            relative = path.relative_to(self.root).as_posix()
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                result.add(
                    Finding(
                        code="evidence.vault_record_unreadable",
                        severity=Severity.BLOCKER,
                        summary="Admitted evidence vault contains an unreadable record.",
                        location=relative,
                        detail=str(error),
                    )
                )
                continue
            expected = str(value.get("record_digest") or "")
            content = dict(value)
            content.pop("record_digest", None)
            if stable_digest(content) != expected:
                result.add(
                    Finding(
                        code="evidence.vault_record_tampered",
                        severity=Severity.BLOCKER,
                        summary="Admitted evidence vault record digest is invalid.",
                        location=relative,
                    )
                )
            kind = str((value.get("envelope") or {}).get("kind") or "unknown")
            by_kind[kind] += 1
            result.evidence.append(
                EvidencePointer(
                    kind="admitted_evidence_record",
                    location=relative,
                    summary=kind,
                    metadata={"record_digest": expected},
                )
            )
        result.metrics.update({"record_count": count, "kind_counts": dict(by_kind)})
        return result.finish(default_partial=count == 0)


def _enum(kind: type[StrEnum], value: Any, default: Any) -> Any:
    try:
        return kind(str(value))
    except ValueError:
        return default
