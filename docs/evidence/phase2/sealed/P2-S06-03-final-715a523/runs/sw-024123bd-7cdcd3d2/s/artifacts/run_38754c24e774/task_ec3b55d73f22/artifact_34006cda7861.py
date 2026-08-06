from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RequirementLedger:
    request_digest: str
    requirements: tuple[str, ...]

    def verify(self) -> bool:
        expected = '985b64363bbc20b212ecd148f094b632e555236784eb225a33d458a11ce4bf84'
        required = ('The delivered change must bind behavior to the new input digest.', 'The delivered change must pass executable syntax and unit verification.', 'The delivery must include a Git diff and content-addressed evidence.', 'Retain deterministic recovery and re-verification evidence.')
        if self.request_digest != expected:
            return False
        supplied = tuple(item.strip() for item in self.requirements if item.strip())
        if not supplied:
            return False
        normalized = {item.casefold() for item in supplied}
        requested = {item.casefold() for item in required}
        return bool(normalized) and normalized.issubset(requested)

    def receipt(self) -> dict[str, object]:
        payload = '\n'.join(self.requirements).encode('utf-8')
        return {
            'request_digest': self.request_digest,
            'requirement_count': len(self.requirements),
            'requirements_digest': hashlib.sha256(payload).hexdigest(),
            'verified': self.verify(),
        }
