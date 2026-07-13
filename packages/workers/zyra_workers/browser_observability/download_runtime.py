from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from .models import digest_value, new_observation_id, utc_now


class DownloadState(StrEnum):
    REQUESTED = "requested"
    RECEIVING = "receiving"
    COMPLETE = "complete"
    QUARANTINED = "quarantined"
    FAILED = "failed"
    PUBLISHED = "published"


class DownloadRisk(StrEnum):
    LOW = "low"
    UNKNOWN_TYPE = "unknown_type"
    EXECUTABLE = "executable"
    OVERSIZED = "oversized"
    PATH_TRAVERSAL = "path_traversal"
    DIGEST_MISMATCH = "digest_mismatch"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True, slots=True)
class DownloadPolicy:
    max_file_bytes: int = 100_000_000
    max_session_bytes: int = 500_000_000
    max_downloads: int = 100
    allowed_media_types: frozenset[str] = frozenset()
    denied_extensions: frozenset[str] = frozenset(
        {
            ".exe",
            ".dll",
            ".msi",
            ".bat",
            ".cmd",
            ".ps1",
            ".com",
            ".scr",
            ".jar",
        }
    )
    quarantine_unknown_types: bool = True
    require_digest: bool = True

    def __post_init__(self) -> None:
        if self.max_file_bytes < 1:
            raise ValueError("download max_file_bytes must be positive")
        if self.max_session_bytes < self.max_file_bytes:
            raise ValueError("download session budget must fit one file")
        if self.max_downloads < 1:
            raise ValueError("download count limit must be positive")
        object.__setattr__(
            self,
            "denied_extensions",
            frozenset(
                item.casefold()
                if item.startswith(".")
                else f".{item.casefold()}"
                for item in self.denied_extensions
            ),
        )


@dataclass(frozen=True, slots=True)
class DownloadItem:
    download_id: str
    browser_session_id: str
    suggested_name: str
    safe_name: str
    source_url: str
    state: DownloadState
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    received_bytes: int = 0
    expected_bytes: int | None = None
    media_type: str = "application/octet-stream"
    digest: str = ""
    staging_path: str = ""
    artifact_id: str = ""
    risks: tuple[DownloadRisk, ...] = ()
    error: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.received_bytes < 0:
            raise ValueError("received bytes must be non-negative")
        if self.expected_bytes is not None and self.expected_bytes < 0:
            raise ValueError("expected bytes must be non-negative")
        object.__setattr__(self, "risks", tuple(self.risks))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def terminal(self) -> bool:
        return self.state in {
            DownloadState.COMPLETE,
            DownloadState.QUARANTINED,
            DownloadState.FAILED,
            DownloadState.PUBLISHED,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "browser_session_id": self.browser_session_id,
            "suggested_name": self.suggested_name,
            "safe_name": self.safe_name,
            "source_url": self.source_url,
            "state": str(self.state),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "received_bytes": self.received_bytes,
            "expected_bytes": self.expected_bytes,
            "media_type": self.media_type,
            "digest": self.digest,
            "staging_path": self.staging_path,
            "artifact_id": self.artifact_id,
            "risks": [str(item) for item in self.risks],
            "error": self.error,
            "metadata": dict(self.metadata),
        }


class BrowserDownloadRuntime:
    """Grant-scoped download ledger with quarantine and readback verification."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: DownloadPolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.staging_root = self.root / "staging"
        self.quarantine_root = self.root / "quarantine"
        self.published_root = self.root / "published"
        for path in (
            self.staging_root,
            self.quarantine_root,
            self.published_root,
        ):
            path.mkdir(parents=True, exist_ok=True)
        self.policy = policy or DownloadPolicy()
        self._guard = threading.RLock()
        self._items: dict[str, DownloadItem] = {}

    def begin(
        self,
        *,
        browser_session_id: str,
        suggested_name: str,
        source_url: str,
        expected_bytes: int | None = None,
        media_type: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> DownloadItem:
        with self._guard:
            session_items = self.for_session(browser_session_id)
            if len(session_items) >= self.policy.max_downloads:
                raise RuntimeError("browser download count limit exceeded")
            safe_name, path_risk = safe_download_name(suggested_name)
            resolved_media = (
                media_type
                or mimetypes.guess_type(safe_name)[0]
                or "application/octet-stream"
            )
            risks: list[DownloadRisk] = []
            if path_risk:
                risks.append(DownloadRisk.PATH_TRAVERSAL)
            if Path(safe_name).suffix.casefold() in self.policy.denied_extensions:
                risks.append(DownloadRisk.EXECUTABLE)
            if (
                resolved_media == "application/octet-stream"
                and self.policy.quarantine_unknown_types
            ):
                risks.append(DownloadRisk.UNKNOWN_TYPE)
            if (
                expected_bytes is not None
                and expected_bytes > self.policy.max_file_bytes
            ):
                risks.append(DownloadRisk.OVERSIZED)
            download_id = new_observation_id("browser-download")
            staging = self.staging_root / browser_session_id / f"{download_id}-{safe_name}"
            staging.parent.mkdir(parents=True, exist_ok=True)
            item = DownloadItem(
                download_id=download_id,
                browser_session_id=browser_session_id,
                suggested_name=suggested_name,
                safe_name=safe_name,
                source_url=source_url,
                state=DownloadState.REQUESTED,
                expected_bytes=expected_bytes,
                media_type=resolved_media,
                staging_path=str(staging),
                risks=tuple(dict.fromkeys(risks)),
                metadata=dict(metadata or {}),
            )
            self._items[download_id] = item
            return item

    def receive(
        self,
        download_id: str,
        data: bytes,
        *,
        append: bool = True,
    ) -> DownloadItem:
        with self._guard:
            item = self.require(download_id)
            if item.terminal:
                raise RuntimeError("cannot write a terminal download")
            path = Path(item.staging_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if append else "wb"
            with path.open(mode) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            received = path.stat().st_size
            session_bytes = sum(
                candidate.received_bytes
                for candidate in self.for_session(item.browser_session_id)
                if candidate.download_id != item.download_id
            ) + received
            risks = list(item.risks)
            if received > self.policy.max_file_bytes:
                risks.append(DownloadRisk.OVERSIZED)
            if session_bytes > self.policy.max_session_bytes:
                risks.append(DownloadRisk.OVERSIZED)
            updated = replace(
                item,
                state=DownloadState.RECEIVING,
                received_bytes=received,
                updated_at=utc_now(),
                risks=tuple(dict.fromkeys(risks)),
            )
            self._items[download_id] = updated
            return updated

    def complete(
        self,
        download_id: str,
        *,
        expected_digest: str = "",
    ) -> DownloadItem:
        with self._guard:
            item = self.require(download_id)
            if item.terminal:
                return item
            path = Path(item.staging_path)
            if not path.exists():
                return self.fail(download_id, "download staging file is missing")
            digest = file_digest(path)
            risks = list(item.risks)
            if (
                item.expected_bytes is not None
                and path.stat().st_size != item.expected_bytes
            ):
                risks.append(DownloadRisk.INCOMPLETE)
            if expected_digest and digest != expected_digest:
                risks.append(DownloadRisk.DIGEST_MISMATCH)
            quarantine = bool(risks)
            target_root = self.quarantine_root if quarantine else self.published_root
            target = (
                target_root
                / item.browser_session_id
                / f"{item.download_id}-{item.safe_name}"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            updated = replace(
                item,
                state=(
                    DownloadState.QUARANTINED
                    if quarantine
                    else DownloadState.COMPLETE
                ),
                received_bytes=target.stat().st_size,
                digest=digest,
                staging_path=str(target),
                updated_at=utc_now(),
                risks=tuple(dict.fromkeys(risks)),
            )
            self._items[download_id] = updated
            return updated

    def publish(
        self,
        download_id: str,
        artifact_id: str,
    ) -> DownloadItem:
        with self._guard:
            item = self.require(download_id)
            if item.state != DownloadState.COMPLETE:
                raise RuntimeError("only a verified non-quarantined download can publish")
            updated = replace(
                item,
                state=DownloadState.PUBLISHED,
                artifact_id=artifact_id,
                updated_at=utc_now(),
            )
            self._items[download_id] = updated
            return updated

    def fail(
        self,
        download_id: str,
        error: str,
    ) -> DownloadItem:
        with self._guard:
            item = self.require(download_id)
            updated = replace(
                item,
                state=DownloadState.FAILED,
                error=str(error),
                updated_at=utc_now(),
            )
            self._items[download_id] = updated
            return updated

    def require(
        self,
        download_id: str,
    ) -> DownloadItem:
        item = self._items.get(download_id)
        if item is None:
            raise KeyError(download_id)
        return item

    def for_session(
        self,
        browser_session_id: str,
    ) -> tuple[DownloadItem, ...]:
        return tuple(
            item
            for item in self._items.values()
            if item.browser_session_id == browser_session_id
        )

    def projection(
        self,
        *,
        browser_session_id: str = "",
    ) -> dict[str, Any]:
        items = (
            self.for_session(browser_session_id)
            if browser_session_id
            else tuple(self._items.values())
        )
        return {
            "schema": "zyra.browser-observability.downloads.v1",
            "download_count": len(items),
            "active_count": sum(1 for item in items if not item.terminal),
            "completed_count": sum(
                1
                for item in items
                if item.state in {DownloadState.COMPLETE, DownloadState.PUBLISHED}
            ),
            "quarantined_count": sum(
                1
                for item in items
                if item.state == DownloadState.QUARANTINED
            ),
            "failed_count": sum(
                1
                for item in items
                if item.state == DownloadState.FAILED
            ),
            "received_bytes": sum(item.received_bytes for item in items),
            "items": [item.to_dict() for item in items],
        }


def safe_download_name(
    suggested: str,
) -> tuple[str, bool]:
    raw = str(suggested or "").strip()
    base = raw.replace("\\", "/").split("/")[-1]
    base = re.sub(r"[\x00-\x1f<>:\"|?*]", "_", base)
    base = re.sub(r"\s+", " ", base).strip(" .")
    if not base:
        base = "download.bin"
    if len(base) > 180:
        suffix = Path(base).suffix[:20]
        stem_budget = max(1, 180 - len(suffix))
        base = Path(base).stem[:stem_budget] + suffix
    risky = (
        raw != base
        or ".." in raw.replace("\\", "/").split("/")
        or raw.startswith(("/", "\\"))
    )
    return base, risky


def file_digest(
    path: str | Path,
) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()
