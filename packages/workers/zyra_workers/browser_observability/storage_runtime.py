from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import canonical_json, digest_value, utc_now


_SECRET_KEY = re.compile(
    r"(token|secret|password|passwd|authorization|cookie|session|credential|api[_-]?key)",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class StoragePolicy:
    max_cookies: int = 5_000
    max_origins: int = 1_000
    max_value_chars: int = 100_000
    redact_secret_values: bool = True
    persist_session_cookies: bool = False
    fsync: bool = True

    def __post_init__(self) -> None:
        if self.max_cookies < 0:
            raise ValueError("max_cookies must be non-negative")
        if self.max_origins < 0:
            raise ValueError("max_origins must be non-negative")
        if self.max_value_chars < 64:
            raise ValueError("max_value_chars is too small")


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    browser_session_id: str
    revision: int
    captured_at: str
    cookies: tuple[Mapping[str, Any], ...]
    origins: tuple[Mapping[str, Any], ...]
    digest: str
    redaction_count: int = 0
    source_event_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.storage-state.v1",
            "browser_session_id": self.browser_session_id,
            "revision": self.revision,
            "captured_at": self.captured_at,
            "cookies": [dict(item) for item in self.cookies],
            "origins": [dict(item) for item in self.origins],
            "digest": self.digest,
            "redaction_count": self.redaction_count,
            "source_event_ids": list(self.source_event_ids),
        }


@dataclass(frozen=True, slots=True)
class StorageDelta:
    previous_digest: str
    current_digest: str
    added_cookie_keys: tuple[str, ...]
    removed_cookie_keys: tuple[str, ...]
    changed_cookie_keys: tuple[str, ...]
    added_origins: tuple[str, ...]
    removed_origins: tuple[str, ...]
    changed_origins: tuple[str, ...]

    @property
    def dirty(self) -> bool:
        return any(
            (
                self.added_cookie_keys,
                self.removed_cookie_keys,
                self.changed_cookie_keys,
                self.added_origins,
                self.removed_origins,
                self.changed_origins,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "previous_digest": self.previous_digest,
            "current_digest": self.current_digest,
            "added_cookie_keys": list(self.added_cookie_keys),
            "removed_cookie_keys": list(self.removed_cookie_keys),
            "changed_cookie_keys": list(self.changed_cookie_keys),
            "added_origins": list(self.added_origins),
            "removed_origins": list(self.removed_origins),
            "changed_origins": list(self.changed_origins),
            "dirty": self.dirty,
        }


class BrowserStorageStateRuntime:
    """Bounded, redacted storage-state persistence owned by observability."""

    def __init__(
        self,
        root: str | Path,
        *,
        policy: StoragePolicy | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.policy = policy or StoragePolicy()
        self._guard = threading.RLock()
        self._snapshots: dict[str, StorageSnapshot] = {}

    def capture(
        self,
        browser_session_id: str,
        raw_state: Mapping[str, Any],
        *,
        source_event_ids: Sequence[str] = (),
    ) -> tuple[StorageSnapshot, StorageDelta]:
        with self._guard:
            previous = self._snapshots.get(browser_session_id)
            cookies, cookie_redactions = self._cookies(raw_state.get("cookies"))
            origins, origin_redactions = self._origins(raw_state.get("origins"))
            revision = 1 if previous is None else previous.revision + 1
            content = {
                "cookies": cookies,
                "origins": origins,
            }
            snapshot = StorageSnapshot(
                browser_session_id=browser_session_id,
                revision=revision,
                captured_at=utc_now(),
                cookies=tuple(cookies),
                origins=tuple(origins),
                digest=digest_value(content),
                redaction_count=cookie_redactions + origin_redactions,
                source_event_ids=tuple(source_event_ids),
            )
            delta = compare_storage(previous, snapshot)
            self._snapshots[browser_session_id] = snapshot
            return snapshot, delta

    def persist(
        self,
        snapshot: StorageSnapshot,
    ) -> Path:
        with self._guard:
            current = self._snapshots.get(snapshot.browser_session_id)
            if current is not None and current.revision > snapshot.revision:
                raise RuntimeError("refusing to persist a stale storage snapshot")
            path = self.root / snapshot.browser_session_id / "storage-state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            payload = (canonical_json(snapshot.to_dict()) + "\n").encode("utf-8")
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                if self.policy.fsync:
                    os.fsync(handle.fileno())
            os.replace(temporary, path)
            readback = json.loads(path.read_text(encoding="utf-8"))
            if str(readback.get("digest") or "") != snapshot.digest:
                raise RuntimeError("storage state readback digest mismatch")
            return path

    def latest(
        self,
        browser_session_id: str,
    ) -> StorageSnapshot | None:
        return self._snapshots.get(browser_session_id)

    def projection(
        self,
        browser_session_id: str,
    ) -> dict[str, Any]:
        snapshot = self.latest(browser_session_id)
        if snapshot is None:
            return {
                "schema": "zyra.browser-observability.storage-state.v1",
                "browser_session_id": browser_session_id,
                "available": False,
            }
        return {
            "available": True,
            **snapshot.to_dict(),
            "cookie_count": len(snapshot.cookies),
            "origin_count": len(snapshot.origins),
        }

    def _cookies(
        self,
        value: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        raw = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
        if len(raw) > self.policy.max_cookies:
            raise ValueError("storage state cookie limit exceeded")
        output: list[dict[str, Any]] = []
        redactions = 0
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            cookie = {
                str(key): self._bounded(raw_value)
                for key, raw_value in item.items()
            }
            if not self.policy.persist_session_cookies and cookie.get("session"):
                continue
            if self.policy.redact_secret_values and "value" in cookie:
                cookie["value"] = self._redacted(cookie["value"])
                redactions += 1
            output.append(cookie)
        output.sort(
            key=lambda item: (
                str(item.get("domain") or ""),
                str(item.get("path") or ""),
                str(item.get("name") or ""),
            )
        )
        return output, redactions

    def _origins(
        self,
        value: Any,
    ) -> tuple[list[dict[str, Any]], int]:
        raw = value if isinstance(value, Sequence) and not isinstance(value, str | bytes) else ()
        if len(raw) > self.policy.max_origins:
            raise ValueError("storage state origin limit exceeded")
        output: list[dict[str, Any]] = []
        redactions = 0
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            origin = str(item.get("origin") or "")
            local_storage = item.get("localStorage")
            entries: list[dict[str, str]] = []
            if isinstance(local_storage, Sequence) and not isinstance(local_storage, str | bytes):
                for entry in local_storage:
                    if not isinstance(entry, Mapping):
                        continue
                    name = str(entry.get("name") or "")
                    raw_value = self._bounded(entry.get("value"))
                    if self.policy.redact_secret_values and _SECRET_KEY.search(name):
                        raw_value = self._redacted(raw_value)
                        redactions += 1
                    entries.append({"name": name, "value": str(raw_value)})
            entries.sort(key=lambda entry: entry["name"])
            output.append({"origin": origin, "localStorage": entries})
        output.sort(key=lambda item: item["origin"])
        return output, redactions

    def _bounded(
        self,
        value: Any,
    ) -> Any:
        if not isinstance(value, str):
            return value
        if len(value) <= self.policy.max_value_chars:
            return value
        return value[: self.policy.max_value_chars] + "...[truncated]"

    @staticmethod
    def _redacted(
        value: Any,
    ) -> str:
        digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
        return f"[REDACTED sha256:{digest}]"


def compare_storage(
    previous: StorageSnapshot | None,
    current: StorageSnapshot,
) -> StorageDelta:
    previous_cookies = (
        {_cookie_key(item): digest_value(item) for item in previous.cookies}
        if previous
        else {}
    )
    current_cookies = {
        _cookie_key(item): digest_value(item)
        for item in current.cookies
    }
    previous_origins = (
        {str(item.get("origin") or ""): digest_value(item) for item in previous.origins}
        if previous
        else {}
    )
    current_origins = {
        str(item.get("origin") or ""): digest_value(item)
        for item in current.origins
    }
    return StorageDelta(
        previous_digest=previous.digest if previous else "",
        current_digest=current.digest,
        added_cookie_keys=tuple(sorted(current_cookies.keys() - previous_cookies.keys())),
        removed_cookie_keys=tuple(sorted(previous_cookies.keys() - current_cookies.keys())),
        changed_cookie_keys=tuple(
            sorted(
                key
                for key in current_cookies.keys() & previous_cookies.keys()
                if current_cookies[key] != previous_cookies[key]
            )
        ),
        added_origins=tuple(sorted(current_origins.keys() - previous_origins.keys())),
        removed_origins=tuple(sorted(previous_origins.keys() - current_origins.keys())),
        changed_origins=tuple(
            sorted(
                key
                for key in current_origins.keys() & previous_origins.keys()
                if current_origins[key] != previous_origins[key]
            )
        ),
    )


def _cookie_key(
    item: Mapping[str, Any],
) -> str:
    return "|".join(
        (
            str(item.get("domain") or ""),
            str(item.get("path") or ""),
            str(item.get("name") or ""),
        )
    )
