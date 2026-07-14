from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import content_digest, stable_id
from .models import FileArtifactRequest, QuarantineRecord
from .state_store import GatewayStateStore


class QuarantineStore:
    """Stores rejected payloads outside workspace mounts with durable metadata."""

    def __init__(
        self,
        state_root: str | Path,
        state_store: GatewayStateStore,
    ) -> None:
        self.root = Path(state_root).resolve() / "sandbox-gateway-quarantine"
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_store = state_store
        self._lock = threading.RLock()

    def quarantine(
        self,
        request: FileArtifactRequest,
        *,
        reason_codes: Iterable[str],
        metadata: Mapping[str, Any] | None = None,
    ) -> QuarantineRecord:
        codes = tuple(sorted(set(str(item) for item in reason_codes)))
        quarantine_id = stable_id(
            "gateway-quarantine",
            request.session_id,
            request.request_id,
            request.content_digest,
            request.logical_path,
            codes,
        )
        record = QuarantineRecord(
            quarantine_id=quarantine_id,
            session_id=request.session_id,
            request_id=request.request_id,
            content_digest=request.content_digest,
            logical_path=request.logical_path,
            reason_codes=codes,
            metadata=dict(metadata or {}),
        )
        with self._lock:
            target = self._payload_path(record)
            if not target.exists():
                temporary = target.with_suffix(
                    f".{os.getpid()}.{threading.get_ident()}.tmp"
                )
                try:
                    with temporary.open("wb") as handle:
                        handle.write(request.content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            persisted = self.state_store.save_quarantine(record)
        return persisted

    def read(self, quarantine_id: str) -> bytes:
        record = self.state_store.require_quarantine(quarantine_id)
        payload = self._payload_path(record).read_bytes()
        if content_digest(payload) != record.content_digest:
            raise ValueError("quarantine payload digest mismatch")
        return payload

    def release(
        self,
        quarantine_id: str,
        *,
        reason: str,
        delete_payload: bool = False,
    ) -> QuarantineRecord:
        record = self.state_store.release_quarantine(
            quarantine_id,
            reason=reason,
        )
        if delete_payload:
            self._payload_path(record).unlink(missing_ok=True)
        return record

    def delete_expired(self, *, older_than_seconds: float) -> int:
        threshold = time.time() - older_than_seconds
        removed = 0
        for path in self.root.glob("*.payload"):
            if path.stat().st_mtime < threshold:
                path.unlink(missing_ok=True)
                removed += 1
        return removed

    def descriptor(self) -> Mapping[str, Any]:
        return {
            "owner": "QuarantineStore",
            "workspace_visible": False,
            "content_addressed": True,
            "release_requires_explicit_call": True,
            "payload_count": sum(1 for _ in self.root.glob("*.payload")),
        }

    def _payload_path(self, record: QuarantineRecord) -> Path:
        digest_value = record.content_digest.rsplit(":", 1)[-1]
        return self.root / f"{digest_value}.payload"
