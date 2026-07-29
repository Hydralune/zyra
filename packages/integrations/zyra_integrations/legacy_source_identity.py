from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class LegacySourceStatus(StrEnum):
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class LegacySourceIdentity:
    name: str
    version: str
    source_commit: str
    source_role: str
    current_runtime_owner: str
    license_id: str
    status: LegacySourceStatus = LegacySourceStatus.RETIRED
    availability: str = "not_applicable"
    filesystem_required: bool = False
    fallback_available: bool = False
    provenance_manifest: str = (
        "docs/reviews/evidence/P2-S02A-01/"
        "legacy-source-pool-retirement-manifest.json"
    )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "source_commit": self.source_commit,
            "source_role": self.source_role,
            "current_runtime_owner": self.current_runtime_owner,
            "license_id": self.license_id,
            "status": str(self.status),
            "availability": self.availability,
            "filesystem_required": self.filesystem_required,
            "fallback_available": self.fallback_available,
            "provenance_manifest": self.provenance_manifest,
        }


def claude_code_source_identity() -> LegacySourceIdentity:
    return LegacySourceIdentity(
        name="claude-code-best",
        version="1.0.0",
        source_commit="c57f5a29e88e9a814bea47abeb9a0a6f725dc102",
        source_role="primary_implementation",
        current_runtime_owner="packages/runtime/claude-runtime",
        license_id="USER-AUTHORIZED-PROJECT-REUSE",
    )


def browser_use_source_identity() -> LegacySourceIdentity:
    return LegacySourceIdentity(
        name="browser-use",
        version="0.13.3",
        source_commit="18484f23ac96bb955259a1c54530a7d265dfffdb",
        source_role="primary_implementation",
        current_runtime_owner="packages/workers/zyra_workers/browser_session",
        license_id="MIT",
    )


__all__ = [
    "LegacySourceIdentity",
    "LegacySourceStatus",
    "browser_use_source_identity",
    "claude_code_source_identity",
]
