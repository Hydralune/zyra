from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .errors import BrowserArtifactError
from .models import BrowserArtifactKind, BrowserArtifactReceipt, BrowserLifecycleEvent, browser_id, browser_now
from .store import BrowserStatePort


class BrowserArtifactPort(Protocol):
    def write_bytes(
        self,
        session_id: str,
        kind: BrowserArtifactKind,
        content: bytes,
        *,
        name: str,
        media_type: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactReceipt: ...


class BrowserCanonicalEventPort(Protocol):
    def append(self, event: BrowserLifecycleEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class ArtifactPolicy:
    max_bytes: int = 128 * 1024 * 1024
    allowed_extensions: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".webp", ".json", ".txt", ".pdf", ".zip")
    redact_text: bool = True
    fsync: bool = True


class BrowserArtifactEventBridge:
    def __init__(
        self,
        artifact_root: str | Path,
        *,
        state_store: BrowserStatePort | None = None,
        canonical_event_port: BrowserCanonicalEventPort | None = None,
        policy: ArtifactPolicy | None = None,
        disabled: bool = False,
    ) -> None:
        self.root = Path(artifact_root).expanduser().resolve()
        self.state_store = state_store
        self.canonical_event_port = canonical_event_port
        self.policy = policy or ArtifactPolicy()
        self.disabled = disabled
        self._lock = threading.RLock()
        self._receipts: dict[str, BrowserArtifactReceipt] = {}
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserArtifactError("browser artifact bridge is disabled")

    def write_bytes(
        self,
        session_id: str,
        kind: BrowserArtifactKind,
        content: bytes,
        *,
        name: str,
        media_type: str = "application/octet-stream",
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactReceipt:
        self._ensure_available()
        if not session_id:
            raise BrowserArtifactError("artifact session id is required")
        if len(content) > self.policy.max_bytes:
            raise BrowserArtifactError("browser artifact exceeds configured size limit")
        safe_name = self._safe_name(name, kind)
        session_root = (self.root / "browser" / session_id).resolve()
        session_root.mkdir(parents=True, exist_ok=True)
        target = (session_root / safe_name).resolve()
        try:
            target.relative_to(session_root)
        except ValueError as error:
            raise BrowserArtifactError("artifact path escaped session root") from error
        artifact_id = browser_id("brartifact")
        temp = target.with_name(f".{target.name}.{artifact_id}.tmp")
        with self._lock:
            with temp.open("wb") as stream:
                stream.write(content)
                stream.flush()
                if self.policy.fsync:
                    os.fsync(stream.fileno())
            os.replace(temp, target)
            digest = f"sha256:{hashlib.sha256(content).hexdigest()}"
            receipt = BrowserArtifactReceipt(
                artifact_id=artifact_id,
                session_id=session_id,
                kind=kind,
                uri=str(target),
                size_bytes=len(content),
                sha256=digest,
                media_type=media_type,
                metadata=self._public_metadata(metadata or {}),
            )
            self._receipts[artifact_id] = receipt
            if self.state_store is not None and hasattr(self.state_store, "put_artifact"):
                self.state_store.put_artifact(artifact_id, receipt.to_dict())
            return receipt

    def write_json(
        self,
        session_id: str,
        kind: BrowserArtifactKind,
        value: Mapping[str, Any] | list[Any],
        *,
        name: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactReceipt:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
        return self.write_bytes(
            session_id,
            kind,
            encoded,
            name=name,
            media_type="application/json",
            metadata=metadata,
        )

    def write_text(
        self,
        session_id: str,
        kind: BrowserArtifactKind,
        text: str,
        *,
        name: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> BrowserArtifactReceipt:
        value = self._redact_text(text) if self.policy.redact_text else text
        return self.write_bytes(
            session_id,
            kind,
            value.encode("utf-8"),
            name=name,
            media_type="text/plain; charset=utf-8",
            metadata=metadata,
        )

    def append(self, event: BrowserLifecycleEvent) -> None:
        self._ensure_available()
        public = BrowserLifecycleEvent(
            topic=event.topic,
            session_id=event.session_id,
            run_id=event.run_id,
            task_id=event.task_id,
            worker_request_id=event.worker_request_id,
            generation=event.generation,
            payload=self._public_metadata(event.payload),
            event_id=event.event_id,
            cause_event_id=event.cause_event_id,
            created_at=event.created_at,
        )
        if self.state_store is not None:
            self.state_store.append_event(public.session_id, public.to_dict())
        if self.canonical_event_port is not None:
            self.canonical_event_port.append(public)

    def get(self, artifact_id: str) -> BrowserArtifactReceipt | None:
        with self._lock:
            return self._receipts.get(artifact_id)

    def list_session(self, session_id: str) -> tuple[BrowserArtifactReceipt, ...]:
        with self._lock:
            values = tuple(item for item in self._receipts.values() if item.session_id == session_id)
        return tuple(sorted(values, key=lambda item: (item.created_at, item.artifact_id)))

    def verify(self, receipt: BrowserArtifactReceipt) -> bool:
        path = Path(receipt.uri)
        if not path.exists() or not path.is_file():
            return False
        if path.stat().st_size != receipt.size_bytes:
            return False
        digest = f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
        return digest == receipt.sha256

    def _safe_name(self, name: str, kind: BrowserArtifactKind) -> str:
        normalized = name.replace("\\", "/").split("/")[-1].replace("\x00", "")
        normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip(".-")
        if not normalized:
            normalized = f"{kind}-{browser_id('file')}"
        suffix = Path(normalized).suffix.casefold()
        if suffix and suffix not in self.policy.allowed_extensions:
            normalized = f"{normalized}.bin"
        return normalized[:180]

    def _public_metadata(self, value: Mapping[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            if any(token in normalized for token in ("authorization", "cookie", "secret", "token", "password", "api_key")):
                result[str(key)] = "[REDACTED]"
            elif isinstance(item, Mapping):
                result[str(key)] = self._public_metadata(item)
            elif isinstance(item, (str, int, float, bool)) or item is None:
                result[str(key)] = item
            elif isinstance(item, (list, tuple)):
                result[str(key)] = [self._public_metadata(v) if isinstance(v, Mapping) else v for v in item]
            else:
                result[str(key)] = str(item)
        return result

    @staticmethod
    def _redact_text(text: str) -> str:
        patterns = (
            re.compile(r"(?i)(authorization\s*[:=]\s*)([^\s,;]+)"),
            re.compile(r"(?i)(api[_-]?key\s*[:=]\s*)([^\s,;]+)"),
            re.compile(r"(?i)(password\s*[:=]\s*)([^\s,;]+)"),
            re.compile(r"(?i)(cookie\s*[:=]\s*)([^\n]+)"),
        )
        result = text
        for pattern in patterns:
            result = pattern.sub(r"\1[REDACTED]", result)
        return result
