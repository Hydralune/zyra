from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef

from .integration_models import BrowserArtifactHandoff, public_mapping
from .models import BrowserArtifactKind, browser_id, browser_now


class BrowserArtifactPipelineError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


class BrowserArtifactPayloadKind(StrEnum):
    SCREENSHOT = "screenshot"
    DOWNLOAD = "download"
    TRACE = "trace"


class BrowserArtifactVerificationStatus(StrEnum):
    VERIFIED = "verified"
    DUPLICATE = "duplicate"
    QUARANTINED = "quarantined"
    FAILED = "failed"


class BrowserArtifactStorePort(Protocol):
    root: Path

    def write_bytes(
        self,
        *,
        run_id: str,
        task_id: str,
        content: bytes,
        title: str,
        kind: ArtifactKind,
        extension: str,
        producer_node_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef: ...

    def resolve_path(self, artifact: ArtifactRef) -> Path: ...


@dataclass(frozen=True, slots=True)
class BrowserArtifactPipelinePolicy:
    max_screenshot_bytes: int = 64 * 1024 * 1024
    max_download_bytes: int = 256 * 1024 * 1024
    max_trace_bytes: int = 32 * 1024 * 1024
    max_download_files: int = 256
    verify_readback: bool = True
    quarantine_invalid: bool = True
    deduplicate: bool = True
    quarantine_dir_name: str = ".browser-quarantine"
    incomplete_suffixes: tuple[str, ...] = (".crdownload", ".part", ".partial", ".tmp")

    def __post_init__(self) -> None:
        for name, value in (
            ("max_screenshot_bytes", self.max_screenshot_bytes),
            ("max_download_bytes", self.max_download_bytes),
            ("max_trace_bytes", self.max_trace_bytes),
            ("max_download_files", self.max_download_files),
        ):
            if int(value) <= 0:
                raise ValueError(f"{name} must be positive")
        if not self.quarantine_dir_name or "/" in self.quarantine_dir_name or "\\" in self.quarantine_dir_name:
            raise ValueError("artifact quarantine directory must be a simple name")

    def limit_for(self, kind: BrowserArtifactPayloadKind) -> int:
        if kind == BrowserArtifactPayloadKind.SCREENSHOT:
            return self.max_screenshot_bytes
        if kind == BrowserArtifactPayloadKind.TRACE:
            return self.max_trace_bytes
        return self.max_download_bytes


@dataclass(frozen=True, slots=True)
class BrowserArtifactPayload:
    kind: BrowserArtifactPayloadKind
    run_id: str
    task_id: str
    browser_session_id: str
    action_request_id: str
    content: bytes
    title: str
    extension: str
    media_type: str
    core_kind: ArtifactKind
    browser_kind: BrowserArtifactKind
    node_id: str = ""
    source_path: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.run_id or not self.task_id or not self.browser_session_id or not self.action_request_id:
            raise ValueError("artifact payload identity is incomplete")
        if not isinstance(self.content, bytes):
            raise TypeError("artifact payload content must be bytes")
        extension = self.extension if self.extension.startswith(".") else f".{self.extension}"
        if not re.fullmatch(r"\.[A-Za-z0-9][A-Za-z0-9._-]{0,31}", extension):
            raise ValueError("artifact extension is invalid")
        object.__setattr__(self, "extension", extension.casefold())
        object.__setattr__(self, "media_type", self.media_type.strip().casefold())
        if self.source_path is not None:
            object.__setattr__(self, "source_path", self.source_path.expanduser().resolve())

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.content).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    def public_metadata(self) -> dict[str, Any]:
        return {
            **redact_artifact_metadata(self.metadata),
            "browser_session_id": self.browser_session_id,
            "browser_action_request_id": self.action_request_id,
            "browser_artifact_payload_kind": str(self.kind),
            "media_type": self.media_type,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            **({"source_name": self.source_path.name} if self.source_path else {}),
        }


@dataclass(frozen=True, slots=True)
class BrowserArtifactVerification:
    status: BrowserArtifactVerificationStatus
    artifact_id: str
    expected_sha256: str
    actual_sha256: str
    expected_size: int
    actual_size: int
    path: str
    contained: bool
    media_type: str
    issues: tuple[str, ...] = ()
    quarantine_path: str = ""
    verified_at: str = field(default_factory=browser_now)

    @property
    def ok(self) -> bool:
        return self.status in {
            BrowserArtifactVerificationStatus.VERIFIED,
            BrowserArtifactVerificationStatus.DUPLICATE,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "ok": self.ok,
            "artifact_id": self.artifact_id,
            "expected_sha256": self.expected_sha256,
            "actual_sha256": self.actual_sha256,
            "expected_size": self.expected_size,
            "actual_size": self.actual_size,
            "path": self.path,
            "contained": self.contained,
            "media_type": self.media_type,
            "issues": list(self.issues),
            "quarantine_path": self.quarantine_path,
            "verified_at": self.verified_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserArtifactPipelineResult:
    artifact: ArtifactRef
    handoff: BrowserArtifactHandoff
    verification: BrowserArtifactVerification
    duplicate: bool = False
    duplicate_of: str = ""

    @property
    def ok(self) -> bool:
        return self.verification.ok

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact.artifact_id,
            "handoff": self.handoff.to_dict(),
            "verification": self.verification.to_dict(),
            "duplicate": self.duplicate,
            "duplicate_of": self.duplicate_of,
        }


class BrowserArtifactPipeline:
    """Validates and hands browser bytes to an existing artifact owner."""

    def __init__(
        self,
        artifact_store: BrowserArtifactStorePort | None,
        *,
        artifact_bridge: Any = None,
        handoff_store: Any = None,
        policy: BrowserArtifactPipelinePolicy | None = None,
        disabled: bool = False,
    ) -> None:
        if artifact_store is None and artifact_bridge is None:
            raise ValueError("artifact pipeline requires LocalArtifactStore or the existing artifact bridge")
        self.artifact_store = artifact_store
        self.artifact_bridge = artifact_bridge
        self.handoff_store = handoff_store
        self.policy = policy or BrowserArtifactPipelinePolicy()
        self.disabled = disabled
        self._lock = threading.RLock()
        self._digest_index: dict[tuple[str, str, str, str], BrowserArtifactPipelineResult] = {}
        self._committed = 0
        self._duplicates = 0
        self._verified = 0
        self._quarantined = 0
        self._failures = 0
        self._last_error = ""

    def commit_bytes(self, payload: BrowserArtifactPayload) -> BrowserArtifactPipelineResult:
        self._ensure_available()
        self._validate_payload(payload)
        dedupe_key = (payload.run_id, payload.task_id, payload.browser_session_id, f"{payload.kind}:{payload.sha256}")
        if self.policy.deduplicate:
            duplicate = self._find_duplicate(payload, dedupe_key)
            if duplicate is not None:
                with self._lock:
                    self._duplicates += 1
                return duplicate
        try:
            artifact = self._write(payload)
            verification = self.verify(artifact, payload)
            if not verification.ok:
                raise BrowserArtifactPipelineError(
                    "browser_artifact_verification_failed",
                    "browser artifact failed read-back verification",
                    details=verification.to_dict(),
                )
            handoff = BrowserArtifactHandoff(
                browser_session_id=payload.browser_session_id,
                action_request_id=payload.action_request_id,
                artifact_id=artifact.artifact_id,
                kind=str(payload.core_kind),
                uri=artifact.uri,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                media_type=payload.media_type,
                run_id=payload.run_id,
                task_id=payload.task_id,
                metadata=payload.public_metadata(),
            )
            if self.handoff_store is not None:
                handoff = self.handoff_store.record_handoff(handoff)
            result = BrowserArtifactPipelineResult(
                artifact=artifact,
                handoff=handoff,
                verification=verification,
            )
            with self._lock:
                self._digest_index[dedupe_key] = result
                self._committed += 1
                self._verified += 1
                self._last_error = ""
            return result
        except Exception as error:
            with self._lock:
                self._failures += 1
                self._last_error = f"{type(error).__name__}: {error}"
            raise

    def commit_screenshot(
        self,
        *,
        run_id: str,
        task_id: str,
        browser_session_id: str,
        action_request_id: str,
        content: bytes,
        image_format: str,
        title: str,
        node_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactPipelineResult:
        normalized = image_format.casefold().replace("jpg", "jpeg")
        if normalized not in {"png", "jpeg", "webp"}:
            raise BrowserArtifactPipelineError("unsupported_screenshot_format", "screenshot format must be png, jpeg or webp")
        extension = ".jpg" if normalized == "jpeg" else f".{normalized}"
        media_type = "image/jpeg" if normalized == "jpeg" else f"image/{normalized}"
        return self.commit_bytes(BrowserArtifactPayload(
            kind=BrowserArtifactPayloadKind.SCREENSHOT,
            run_id=run_id,
            task_id=task_id,
            browser_session_id=browser_session_id,
            action_request_id=action_request_id,
            content=content,
            title=title,
            extension=extension,
            media_type=media_type,
            core_kind=ArtifactKind.SCREENSHOT,
            browser_kind=BrowserArtifactKind.SCREENSHOT,
            node_id=node_id,
            metadata={**dict(metadata or {}), "format": normalized},
        ))

    def commit_trace(
        self,
        *,
        run_id: str,
        task_id: str,
        browser_session_id: str,
        action_request_id: str,
        trace: Mapping[str, Any],
        title: str,
        node_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactPipelineResult:
        public_trace = redact_artifact_metadata(trace)
        content = json.dumps(public_trace, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
        return self.commit_bytes(BrowserArtifactPayload(
            kind=BrowserArtifactPayloadKind.TRACE,
            run_id=run_id,
            task_id=task_id,
            browser_session_id=browser_session_id,
            action_request_id=action_request_id,
            content=content,
            title=title,
            extension=".json",
            media_type="application/json",
            core_kind=ArtifactKind.TRACE,
            browser_kind=BrowserArtifactKind.TRACE,
            node_id=node_id,
            metadata={"trace_schema": str(trace.get("schema") or ""), **dict(metadata or {})},
        ))

    def ingest_download(
        self,
        *,
        source_path: str | Path,
        downloads_root: str | Path,
        run_id: str,
        task_id: str,
        browser_session_id: str,
        action_request_id: str,
        node_id: str = "",
        title: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactPipelineResult:
        root = Path(downloads_root).expanduser().resolve()
        source = Path(source_path).expanduser().resolve()
        self._require_contained(source, root, "download source")
        if not source.is_file() or source.is_symlink():
            raise BrowserArtifactPipelineError("invalid_download_source", "download source must be a regular non-symlink file")
        if source.suffix.casefold() in self.policy.incomplete_suffixes:
            raise BrowserArtifactPipelineError("download_incomplete", f"download is still incomplete: {source.name}")
        size = source.stat().st_size
        if size <= 0 or size > self.policy.max_download_bytes:
            raise BrowserArtifactPipelineError(
                "download_size_invalid",
                "download is empty or exceeds the byte limit",
                details={"size_bytes": size, "limit": self.policy.max_download_bytes},
            )
        content = self._read_bounded(source, self.policy.max_download_bytes)
        extension = source.suffix.casefold() or ".bin"
        media_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        return self.commit_bytes(BrowserArtifactPayload(
            kind=BrowserArtifactPayloadKind.DOWNLOAD,
            run_id=run_id,
            task_id=task_id,
            browser_session_id=browser_session_id,
            action_request_id=action_request_id,
            content=content,
            title=title or f"Browser download: {source.name}",
            extension=extension,
            media_type=media_type,
            core_kind=ArtifactKind.FILE,
            browser_kind=BrowserArtifactKind.DOWNLOAD,
            node_id=node_id,
            source_path=source,
            metadata={"source_name": source.name, **dict(metadata or {})},
        ))

    def verify(self, artifact: ArtifactRef, payload: BrowserArtifactPayload) -> BrowserArtifactVerification:
        if self.artifact_store is None or not hasattr(self.artifact_store, "resolve_path"):
            return BrowserArtifactVerification(
                status=BrowserArtifactVerificationStatus.VERIFIED,
                artifact_id=artifact.artifact_id,
                expected_sha256=payload.sha256,
                actual_sha256=str(artifact.metadata.get("sha256") or payload.sha256),
                expected_size=payload.size_bytes,
                actual_size=int(artifact.metadata.get("size_bytes") or payload.size_bytes),
                path=artifact.uri,
                contained=True,
                media_type=payload.media_type,
            )
        issues: list[str] = []
        path = Path(artifact.uri).expanduser().resolve()
        root = Path(self.artifact_store.root).expanduser().resolve()
        contained = self._is_contained(path, root)
        if not contained:
            issues.append("artifact path escaped LocalArtifactStore root")
        if not path.is_file():
            issues.append("artifact file is missing")
            content = b""
        else:
            content = self._read_bounded(path, payload.size_bytes + 1)
        actual_sha = "sha256:" + hashlib.sha256(content).hexdigest()
        if len(content) != payload.size_bytes:
            issues.append("artifact read-back size mismatch")
        if actual_sha != payload.sha256:
            issues.append("artifact read-back digest mismatch")
        if not self._media_signature_valid(payload.kind, payload.media_type, content):
            issues.append("artifact content signature does not match media type")
        if not issues:
            return BrowserArtifactVerification(
                status=BrowserArtifactVerificationStatus.VERIFIED,
                artifact_id=artifact.artifact_id,
                expected_sha256=payload.sha256,
                actual_sha256=actual_sha,
                expected_size=payload.size_bytes,
                actual_size=len(content),
                path=str(path),
                contained=True,
                media_type=payload.media_type,
            )
        quarantine_path = self.quarantine(artifact, reason="; ".join(issues)) if self.policy.quarantine_invalid else ""
        return BrowserArtifactVerification(
            status=(BrowserArtifactVerificationStatus.QUARANTINED if quarantine_path else BrowserArtifactVerificationStatus.FAILED),
            artifact_id=artifact.artifact_id,
            expected_sha256=payload.sha256,
            actual_sha256=actual_sha,
            expected_size=payload.size_bytes,
            actual_size=len(content),
            path=str(path),
            contained=contained,
            media_type=payload.media_type,
            issues=tuple(issues),
            quarantine_path=quarantine_path,
        )

    def quarantine(self, artifact: ArtifactRef, *, reason: str) -> str:
        if self.artifact_store is None:
            return ""
        root = Path(self.artifact_store.root).expanduser().resolve()
        source = Path(artifact.uri).expanduser().resolve()
        if not self._is_contained(source, root) or not source.exists():
            return ""
        quarantine_root = (root / self.policy.quarantine_dir_name).resolve()
        self._require_contained(quarantine_root, root, "quarantine root")
        quarantine_root.mkdir(parents=True, exist_ok=True)
        target = quarantine_root / f"{artifact.artifact_id}-{int(time.time() * 1000)}{source.suffix}"
        target = target.resolve()
        self._require_contained(target, quarantine_root, "quarantine target")
        shutil.move(str(source), str(target))
        manifest = target.with_suffix(target.suffix + ".json")
        manifest.write_text(json.dumps({
            "artifact_id": artifact.artifact_id,
            "original_uri": artifact.uri,
            "reason": reason,
            "quarantined_at": browser_now(),
        }, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        with self._lock:
            self._quarantined += 1
        return str(target)

    def _find_duplicate(
        self,
        payload: BrowserArtifactPayload,
        key: tuple[str, str, str, str],
    ) -> BrowserArtifactPipelineResult | None:
        with self._lock:
            cached = self._digest_index.get(key)
        if cached is not None and self._artifact_still_valid(cached.artifact, payload):
            verification = BrowserArtifactVerification(
                status=BrowserArtifactVerificationStatus.DUPLICATE,
                artifact_id=cached.artifact.artifact_id,
                expected_sha256=payload.sha256,
                actual_sha256=payload.sha256,
                expected_size=payload.size_bytes,
                actual_size=payload.size_bytes,
                path=cached.artifact.uri,
                contained=True,
                media_type=payload.media_type,
            )
            handoff = BrowserArtifactHandoff(
                browser_session_id=payload.browser_session_id,
                action_request_id=payload.action_request_id,
                artifact_id=cached.artifact.artifact_id,
                kind=str(payload.core_kind),
                uri=cached.artifact.uri,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                media_type=payload.media_type,
                run_id=payload.run_id,
                task_id=payload.task_id,
                metadata={**payload.public_metadata(), "duplicate_ref": True},
            )
            if self.handoff_store is not None:
                handoff = self.handoff_store.record_handoff(handoff)
            return BrowserArtifactPipelineResult(
                artifact=cached.artifact,
                handoff=handoff,
                verification=verification,
                duplicate=True,
                duplicate_of=cached.artifact.artifact_id,
            )
        if self.handoff_store is None:
            return None
        for value in reversed(self.handoff_store.list_handoffs(payload.browser_session_id, limit=500)):
            if str(value.get("sha256") or "") != payload.sha256:
                continue
            if str(value.get("kind") or "") != str(payload.core_kind):
                continue
            artifact = ArtifactRef(
                artifact_id=str(value.get("artifact_id") or ""),
                kind=payload.core_kind,
                uri=str(value.get("uri") or ""),
                title=payload.title,
                producer_node_id=payload.node_id or None,
                metadata={**payload.public_metadata(), "duplicate_ref": True},
            )
            if not artifact.artifact_id or not self._artifact_still_valid(artifact, payload):
                continue
            handoff = BrowserArtifactHandoff(
                browser_session_id=payload.browser_session_id,
                action_request_id=payload.action_request_id,
                artifact_id=artifact.artifact_id,
                kind=str(payload.core_kind),
                uri=artifact.uri,
                sha256=payload.sha256,
                size_bytes=payload.size_bytes,
                media_type=payload.media_type,
                run_id=payload.run_id,
                task_id=payload.task_id,
                metadata={**payload.public_metadata(), "duplicate_ref": True},
            )
            if self.handoff_store is not None:
                handoff = self.handoff_store.record_handoff(handoff)
            verification = BrowserArtifactVerification(
                status=BrowserArtifactVerificationStatus.DUPLICATE,
                artifact_id=artifact.artifact_id,
                expected_sha256=payload.sha256,
                actual_sha256=payload.sha256,
                expected_size=payload.size_bytes,
                actual_size=payload.size_bytes,
                path=artifact.uri,
                contained=True,
                media_type=payload.media_type,
            )
            return BrowserArtifactPipelineResult(artifact, handoff, verification, True, artifact.artifact_id)
        return None

    def _artifact_still_valid(self, artifact: ArtifactRef, payload: BrowserArtifactPayload) -> bool:
        if self.artifact_store is None:
            return bool(artifact.uri)
        try:
            path = Path(artifact.uri).expanduser().resolve()
            root = Path(self.artifact_store.root).expanduser().resolve()
            if not self._is_contained(path, root) or not path.is_file() or path.stat().st_size != payload.size_bytes:
                return False
            return "sha256:" + self._hash_file(path, payload.size_bytes + 1) == payload.sha256
        except (OSError, ValueError):
            return False

    def _write(self, payload: BrowserArtifactPayload) -> ArtifactRef:
        metadata = payload.public_metadata()
        if self.artifact_store is not None:
            artifact = self.artifact_store.write_bytes(
                run_id=payload.run_id,
                task_id=payload.task_id,
                content=payload.content,
                title=payload.title,
                kind=payload.core_kind,
                extension=payload.extension,
                producer_node_id=payload.node_id or None,
                metadata=metadata,
            )
            path = Path(artifact.uri).expanduser().resolve()
            root = Path(self.artifact_store.root).expanduser().resolve()
            self._require_contained(path, root, "artifact target")
            return artifact
        receipt = self.artifact_bridge.write_bytes(
            payload.browser_session_id,
            payload.browser_kind,
            payload.content,
            name=f"{payload.action_request_id}{payload.extension}",
            media_type=payload.media_type,
            metadata=metadata,
        )
        return ArtifactRef(
            artifact_id=receipt.artifact_id,
            kind=payload.core_kind,
            uri=receipt.uri,
            title=payload.title,
            producer_node_id=payload.node_id or None,
            metadata={**metadata, "sha256": receipt.sha256, "size_bytes": receipt.size_bytes},
        )

    def _validate_payload(self, payload: BrowserArtifactPayload) -> None:
        limit = self.policy.limit_for(payload.kind)
        if payload.size_bytes <= 0:
            raise BrowserArtifactPipelineError("browser_artifact_empty", "browser artifact content is empty")
        if payload.size_bytes > limit:
            raise BrowserArtifactPipelineError(
                "browser_artifact_too_large",
                "browser artifact exceeds its configured byte limit",
                details={"kind": str(payload.kind), "size_bytes": payload.size_bytes, "limit": limit},
            )
        if not payload.media_type or "/" not in payload.media_type:
            raise BrowserArtifactPipelineError("browser_artifact_media_invalid", "browser artifact media type is invalid")
        if not self._media_signature_valid(payload.kind, payload.media_type, payload.content):
            raise BrowserArtifactPipelineError(
                "browser_artifact_signature_invalid",
                "browser artifact bytes do not match the declared payload kind",
            )

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserArtifactPipelineError("browser_artifact_pipeline_disabled", "browser artifact pipeline is disabled")

    @staticmethod
    def _media_signature_valid(kind: BrowserArtifactPayloadKind, media_type: str, content: bytes) -> bool:
        if not content:
            return False
        if kind == BrowserArtifactPayloadKind.TRACE:
            try:
                return isinstance(json.loads(content.decode("utf-8")), Mapping)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
        if kind == BrowserArtifactPayloadKind.DOWNLOAD:
            return True
        if media_type == "image/png":
            return content.startswith(b"\x89PNG\r\n\x1a\n")
        if media_type == "image/jpeg":
            return content.startswith(b"\xff\xd8") and content.endswith(b"\xff\xd9")
        if media_type == "image/webp":
            return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
        return False

    @staticmethod
    def _read_bounded(path: Path, limit: int) -> bytes:
        data = bytearray()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(min(1024 * 1024, limit + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > limit:
                    raise BrowserArtifactPipelineError("artifact_read_limit_exceeded", "artifact changed beyond its byte limit")
        return bytes(data)

    @staticmethod
    def _hash_file(path: Path, limit: int) -> str:
        digest = hashlib.sha256()
        total = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    return ""
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _is_contained(path: Path, root: Path) -> bool:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            return False

    @classmethod
    def _require_contained(cls, path: Path, root: Path, label: str) -> None:
        if not cls._is_contained(path, root):
            raise BrowserArtifactPipelineError(
                "browser_artifact_path_escape",
                f"{label} escapes its owned root",
                details={"path": str(path), "root": str(root)},
            )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "runtime_id": "zyra-browser-artifact-pipeline",
                "owner_unit": "M1-S04A-02",
                "disabled": self.disabled,
                "committed": self._committed,
                "duplicates": self._duplicates,
                "verified": self._verified,
                "quarantined": self._quarantined,
                "failures": self._failures,
                "digest_index_entries": len(self._digest_index),
                "last_error": self._last_error,
                "canonical_store_owner": False,
            }


SECRET_KEYS = frozenset({
    "authorization", "proxy_authorization", "cookie", "set_cookie", "password",
    "passwd", "secret", "token", "api_key", "apikey", "access_key", "private_key",
})


def redact_artifact_metadata(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if depth > 12:
        return "[DEPTH_LIMIT]"
    normalized = key.casefold().replace("-", "_")
    if normalized and any(token in normalized for token in SECRET_KEYS):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(item_key): redact_artifact_metadata(item, key=str(item_key), depth=depth + 1) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_artifact_metadata(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, bytes):
        return {"bytes": len(value), "sha256": "sha256:" + hashlib.sha256(value).hexdigest()}
    if isinstance(value, str):
        if re.search(r"(?i)(bearer\s+[A-Za-z0-9._~+/-]{8,}|basic\s+[A-Za-z0-9+/=]{8,})", value):
            return "[REDACTED]"
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)
