from __future__ import annotations

import hashlib
import struct
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import digest_value, utc_now


class ScreenshotFormat(StrEnum):
    PNG = "png"
    JPEG = "jpeg"
    WEBP = "webp"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ScreenshotPolicy:
    max_bytes: int = 25_000_000
    min_width: int = 1
    min_height: int = 1
    max_width: int = 16_384
    max_height: int = 16_384
    deduplicate: bool = True
    require_after_terminal_action: bool = True

    def __post_init__(self) -> None:
        if self.max_bytes < 64:
            raise ValueError("screenshot byte limit is too small")
        if self.min_width < 1 or self.min_height < 1:
            raise ValueError("screenshot minimum dimensions must be positive")
        if self.max_width < self.min_width or self.max_height < self.min_height:
            raise ValueError("screenshot dimension policy is invalid")


@dataclass(frozen=True, slots=True)
class ScreenshotEvidence:
    artifact_id: str
    format: ScreenshotFormat
    width: int
    height: int
    size_bytes: int
    digest: str
    captured_at: str = field(default_factory=utc_now)
    source_event_id: str = ""
    action_id: str = ""
    duplicate_of: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "format": str(self.format),
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "digest": self.digest,
            "captured_at": self.captured_at,
            "source_event_id": self.source_event_id,
            "action_id": self.action_id,
            "duplicate_of": self.duplicate_of,
            "metadata": dict(self.metadata),
        }


class BrowserScreenshotRuntime:
    def __init__(
        self,
        *,
        policy: ScreenshotPolicy | None = None,
    ) -> None:
        self.policy = policy or ScreenshotPolicy()
        self._by_digest: dict[str, ScreenshotEvidence] = {}
        self._by_action: dict[str, ScreenshotEvidence] = {}

    def inspect_bytes(
        self,
        data: bytes,
        *,
        artifact_id: str,
        source_event_id: str = "",
        action_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> ScreenshotEvidence:
        if len(data) > self.policy.max_bytes:
            raise ValueError("screenshot exceeds byte limit")
        format_value, width, height = image_dimensions(data)
        self._validate_dimensions(width, height)
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        duplicate = self._by_digest.get(digest)
        evidence = ScreenshotEvidence(
            artifact_id=artifact_id,
            format=format_value,
            width=width,
            height=height,
            size_bytes=len(data),
            digest=digest,
            source_event_id=source_event_id,
            action_id=action_id,
            duplicate_of=duplicate.artifact_id if duplicate else "",
            metadata=dict(metadata or {}),
        )
        if not duplicate or not self.policy.deduplicate:
            self._by_digest[digest] = evidence
        if action_id:
            self._by_action[action_id] = evidence
        return evidence

    def inspect_file(
        self,
        path: str | Path,
        **kwargs: Any,
    ) -> ScreenshotEvidence:
        candidate = Path(path)
        if not candidate.is_file():
            raise FileNotFoundError(candidate)
        return self.inspect_bytes(candidate.read_bytes(), **kwargs)

    def require_for_action(
        self,
        action_id: str,
    ) -> ScreenshotEvidence:
        value = self._by_action.get(action_id)
        if value is None:
            raise RuntimeError(
                f"terminal browser action {action_id!r} has no screenshot evidence"
            )
        return value

    def projection(self) -> dict[str, Any]:
        values = tuple(self._by_digest.values())
        return {
            "schema": "zyra.browser-observability.screenshots.v1",
            "screenshot_count": len(values),
            "action_coverage": len(self._by_action),
            "screenshots": [item.to_dict() for item in values],
        }

    def _validate_dimensions(
        self,
        width: int,
        height: int,
    ) -> None:
        if width < self.policy.min_width or height < self.policy.min_height:
            raise ValueError("screenshot dimensions are below the minimum")
        if width > self.policy.max_width or height > self.policy.max_height:
            raise ValueError("screenshot dimensions exceed the maximum")


def image_dimensions(
    data: bytes,
) -> tuple[ScreenshotFormat, int, int]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return ScreenshotFormat.PNG, width, height
    if data.startswith(b"\xff\xd8"):
        width, height = _jpeg_dimensions(data)
        return ScreenshotFormat.JPEG, width, height
    if (
        data.startswith(b"RIFF")
        and len(data) >= 30
        and data[8:12] == b"WEBP"
    ):
        width, height = _webp_dimensions(data)
        return ScreenshotFormat.WEBP, width, height
    raise ValueError("unsupported or malformed screenshot format")


def _jpeg_dimensions(
    data: bytes,
) -> tuple[int, int]:
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            break
        length = int.from_bytes(data[index:index + 2], "big")
        if length < 2 or index + length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += length
    raise ValueError("JPEG dimensions could not be decoded")


def _webp_dimensions(
    data: bytes,
) -> tuple[int, int]:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    raise ValueError("WEBP dimensions could not be decoded")
