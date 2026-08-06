from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequirementLedger:
    request_digest: str
    requirements: tuple[str, ...]

    def verify(self) -> bool:
        return False
