from __future__ import annotations

import threading
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping

from .canonical import content_digest, digest, provenance_digest, stable_id
from .constants import POLICY_CONTROL_PATH_NAMES, UNTRUSTED_PROVENANCE_KINDS
from .errors import GatewayErrorCode, SandboxGatewayError
from .models import ArtifactProvenance, ProvenanceKind, TrustLevel


@dataclass(frozen=True, slots=True)
class ProvenancePolicyDecision:
    allowed: bool
    quarantine: bool
    reason: str
    reason_codes: tuple[str, ...]
    provenance_id: str
    logical_path: str = ""
    policy_digest: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "quarantine": self.quarantine,
            "reason": self.reason,
            "reason_codes": list(self.reason_codes),
            "provenance_id": self.provenance_id,
            "logical_path": self.logical_path,
            "policy_digest": self.policy_digest,
            "metadata": dict(self.metadata),
        }


class ProvenanceRegistry:
    """Zyra-owned lineage graph; it stores no artifact payloads."""

    def __init__(self) -> None:
        self._records: dict[str, ArtifactProvenance] = {}
        self._children: dict[str, set[str]] = {}
        self._lock = threading.RLock()

    def register(self, provenance: ArtifactProvenance) -> ArtifactProvenance:
        with self._lock:
            existing = self._records.get(provenance.provenance_id)
            if existing is not None:
                if existing.to_dict() != provenance.to_dict():
                    raise SandboxGatewayError(
                        GatewayErrorCode.STATE_CONFLICT,
                        "provenance identity collision",
                        operation="register_provenance",
                    )
                return existing
            for parent in provenance.parent_refs:
                if parent not in self._records:
                    raise SandboxGatewayError(
                        GatewayErrorCode.INVALID_REQUEST,
                        f"unknown provenance parent: {parent}",
                        operation="register_provenance",
                    )
            self._records[provenance.provenance_id] = provenance
            for parent in provenance.parent_refs:
                self._children.setdefault(parent, set()).add(provenance.provenance_id)
            return provenance

    def require(self, provenance_id: str) -> ArtifactProvenance:
        with self._lock:
            try:
                return self._records[provenance_id]
            except KeyError as error:
                raise SandboxGatewayError(
                    GatewayErrorCode.INVALID_REQUEST,
                    f"unknown provenance: {provenance_id}",
                    operation="require_provenance",
                ) from error

    def descendants(self, provenance_id: str) -> tuple[ArtifactProvenance, ...]:
        with self._lock:
            self.require(provenance_id)
            pending = list(self._children.get(provenance_id, ()))
            seen: set[str] = set()
            result: list[ArtifactProvenance] = []
            while pending:
                child = pending.pop()
                if child in seen:
                    continue
                seen.add(child)
                result.append(self._records[child])
                pending.extend(self._children.get(child, ()))
            return tuple(sorted(result, key=lambda item: item.provenance_id))

    def lineage(self, provenance_id: str) -> tuple[ArtifactProvenance, ...]:
        with self._lock:
            current = self.require(provenance_id)
            pending = list(current.parent_refs)
            seen: set[str] = set()
            result: list[ArtifactProvenance] = [current]
            while pending:
                parent = pending.pop()
                if parent in seen:
                    continue
                seen.add(parent)
                record = self.require(parent)
                result.append(record)
                pending.extend(record.parent_refs)
            return tuple(result)

    def derive(
        self,
        *,
        kind: ProvenanceKind | str,
        source_id: str,
        parent_refs: Iterable[str],
        content: bytes | str,
        trust: TrustLevel | str | None = None,
        untrusted_instructions: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactProvenance:
        parents = tuple(parent_refs)
        parent_records = tuple(self.require(item) for item in parents)
        inherited = trust or self._inherited_trust(parent_records)
        record = ArtifactProvenance.build(
            kind=kind,
            trust=inherited,
            source_id=source_id,
            parent_refs=parents,
            content_digest_value=content_digest(content),
            untrusted_instructions=(
                untrusted_instructions
                or any(item.untrusted_instructions for item in parent_records)
            ),
            metadata=metadata,
        )
        return self.register(record)

    def mark_quarantined(
        self,
        provenance_id: str,
        *,
        reason_codes: Iterable[str],
    ) -> ArtifactProvenance:
        with self._lock:
            record = self.require(provenance_id)
            updated = replace(
                record,
                trust=TrustLevel.QUARANTINED,
                metadata={
                    **dict(record.metadata),
                    "quarantine_reason_codes": sorted(set(reason_codes)),
                },
            )
            self._records[provenance_id] = updated
            return updated

    def export(self) -> dict[str, Any]:
        with self._lock:
            records = [
                item.to_dict()
                for item in sorted(self._records.values(), key=lambda record: record.provenance_id)
            ]
            return {
                "records": records,
                "graph_digest": provenance_digest({"records": records}),
            }

    @staticmethod
    def _inherited_trust(parents: Iterable[ArtifactProvenance]) -> TrustLevel:
        levels = {item.trust for item in parents}
        if TrustLevel.QUARANTINED in levels:
            return TrustLevel.QUARANTINED
        if TrustLevel.UNTRUSTED in levels:
            return TrustLevel.UNTRUSTED
        if TrustLevel.CONSTRAINED in levels:
            return TrustLevel.CONSTRAINED
        return TrustLevel.TRUSTED


class ProvenancePolicy:
    def __init__(
        self,
        *,
        control_names: Iterable[str] = POLICY_CONTROL_PATH_NAMES,
        executable_from_untrusted: bool = False,
        archive_from_untrusted: bool = False,
    ) -> None:
        self.control_names = frozenset(str(item).casefold() for item in control_names)
        self.executable_from_untrusted = bool(executable_from_untrusted)
        self.archive_from_untrusted = bool(archive_from_untrusted)
        self.policy_digest = digest(
            {
                "control_names": sorted(self.control_names),
                "executable_from_untrusted": self.executable_from_untrusted,
                "archive_from_untrusted": self.archive_from_untrusted,
            }
        )

    def inspect_write(
        self,
        provenance: ArtifactProvenance,
        logical_path: str,
        *,
        executable: bool = False,
        archive: bool = False,
        claims_policy_authority: bool = False,
    ) -> ProvenancePolicyDecision:
        reasons: list[str] = []
        lowered_parts = tuple(part.casefold() for part in logical_path.replace("\\", "/").split("/"))
        untrusted = provenance.untrusted or provenance.kind.value in UNTRUSTED_PROVENANCE_KINDS
        control_target = any(part in self.control_names for part in lowered_parts)
        if untrusted and control_target:
            reasons.append("untrusted_control_write")
        if untrusted and claims_policy_authority:
            reasons.append("untrusted_policy_claim")
        if untrusted and executable and not self.executable_from_untrusted:
            reasons.append("untrusted_executable")
        if untrusted and archive and not self.archive_from_untrusted:
            reasons.append("untrusted_archive")
        if provenance.trust is TrustLevel.QUARANTINED:
            reasons.append("provenance_quarantined")
        allowed = not reasons
        return ProvenancePolicyDecision(
            allowed=allowed,
            quarantine=bool(reasons),
            reason=(
                "provenance accepted by gateway policy"
                if allowed
                else "untrusted provenance cannot alter control state or introduce active content"
            ),
            reason_codes=tuple(sorted(set(reasons))),
            provenance_id=provenance.provenance_id,
            logical_path=logical_path,
            policy_digest=self.policy_digest,
            metadata={
                "trust": provenance.trust.value,
                "kind": provenance.kind.value,
                "untrusted_instructions": provenance.untrusted_instructions,
            },
        )

    def require_write(
        self,
        provenance: ArtifactProvenance,
        logical_path: str,
        **kwargs: Any,
    ) -> ProvenancePolicyDecision:
        decision = self.inspect_write(provenance, logical_path, **kwargs)
        if not decision.allowed:
            code = (
                GatewayErrorCode.UNTRUSTED_CONTROL_WRITE
                if "untrusted_control_write" in decision.reason_codes
                else GatewayErrorCode.QUARANTINED
            )
            raise SandboxGatewayError(
                code,
                decision.reason,
                operation="provenance_write",
                recovery=("keep the content in quarantine", "request an explicit trusted transformation"),
                metadata=decision.to_dict(),
            )
        return decision


def provenance_from_external(
    *,
    kind: ProvenanceKind | str,
    source_id: str,
    source_uri: str = "",
    content: bytes | str = b"",
    metadata: Mapping[str, Any] | None = None,
) -> ArtifactProvenance:
    normalized_kind = kind if isinstance(kind, ProvenanceKind) else ProvenanceKind(str(kind))
    trust = (
        TrustLevel.UNTRUSTED
        if normalized_kind.value in UNTRUSTED_PROVENANCE_KINDS
        else TrustLevel.CONSTRAINED
    )
    return ArtifactProvenance.build(
        kind=normalized_kind,
        trust=trust,
        source_id=source_id,
        source_uri_digest=digest({"uri": source_uri}) if source_uri else "",
        content_digest_value=content_digest(content),
        untrusted_instructions=normalized_kind in {
            ProvenanceKind.WEB,
            ProvenanceKind.BROWSER,
            ProvenanceKind.MCP,
            ProvenanceKind.REMOTE_TOOL,
        },
        metadata=metadata,
    )
