from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import digest_value, stable_id
from .network_policy import canonicalize_url
from .secret_policy import SecretRedactor


class ClipboardGuardError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        self.code = code
        self.details = dict(details or {})
        super().__init__(message)


class ClipboardAccess(StrEnum):
    READ = "read"
    WRITE = "write"


@dataclass(frozen=True, slots=True)
class ClipboardReceipt:
    receipt_id: str
    action_id: str
    origin: str
    access: ClipboardAccess
    browser_context_id: str
    maximum_chars: int
    content_digest: str = ""

    def __post_init__(self) -> None:
        if not self.action_id or not self.origin or not self.browser_context_id:
            raise ValueError("browser clipboard receipt identity is incomplete")
        if self.maximum_chars < 1:
            raise ValueError("browser clipboard maximum_chars must be positive")

    @property
    def binding_digest(self) -> str:
        return digest_value(self.public_dict())

    def public_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "action_id": self.action_id,
            "origin": self.origin,
            "access": str(self.access),
            "browser_context_id": self.browser_context_id,
            "maximum_chars": self.maximum_chars,
            "content_digest": self.content_digest,
        }


class ClipboardPort(Protocol):
    def grant(self, *, origin: str, browser_context_id: str, read: bool, write: bool) -> None: ...

    def read_text(self, *, origin: str, browser_context_id: str) -> str: ...

    def write_text(self, value: str, *, origin: str, browser_context_id: str) -> None: ...

    def revoke(self, *, origin: str, browser_context_id: str) -> None: ...


@dataclass(slots=True)
class RecordingClipboardPort:
    value: str = ""
    operations: list[dict[str, Any]] = field(default_factory=list)

    def grant(self, *, origin: str, browser_context_id: str, read: bool, write: bool) -> None:
        self.operations.append(
            {
                "operation": "grant",
                "origin": origin,
                "browser_context_id": browser_context_id,
                "read": read,
                "write": write,
            }
        )

    def read_text(self, *, origin: str, browser_context_id: str) -> str:
        self.operations.append({"operation": "read", "origin": origin, "browser_context_id": browser_context_id})
        return self.value

    def write_text(self, value: str, *, origin: str, browser_context_id: str) -> None:
        self.operations.append(
            {
                "operation": "write",
                "origin": origin,
                "browser_context_id": browser_context_id,
                "value_digest": digest_value(value),
            }
        )
        self.value = value

    def revoke(self, *, origin: str, browser_context_id: str) -> None:
        self.operations.append({"operation": "revoke", "origin": origin, "browser_context_id": browser_context_id})


class BrowserClipboardGuard:
    """Exact-origin, one-use clipboard permission lease.

    Browser contexts must start without clipboard permissions.  The guard
    grants only the approved access immediately before the clipboard call and
    revokes it in ``finally``.  It never exposes a permanent browser context
    permission and never treats evaluate/send_keys as clipboard authorization.
    """

    def __init__(self, port: ClipboardPort, *, maximum_chars: int = 100_000, disabled: bool = False) -> None:
        if maximum_chars < 1:
            raise ValueError("browser clipboard character limit must be positive")
        self.port = port
        self.maximum_chars = maximum_chars
        self.disabled = disabled
        self._consumed: set[str] = set()
        self._lock = threading.Lock()

    def preflight(
        self,
        *,
        action_id: str,
        target_url: str,
        browser_context_id: str,
        access: ClipboardAccess,
        value: str = "",
    ) -> ClipboardReceipt:
        if self.disabled:
            raise ClipboardGuardError("clipboard_guard_disabled", "browser clipboard guard is disabled")
        origin = canonicalize_url(target_url).origin
        if access == ClipboardAccess.WRITE and len(value) > self.maximum_chars:
            raise ClipboardGuardError("clipboard_value_too_large", "clipboard write exceeds character limit")
        content_digest = digest_value(value) if access == ClipboardAccess.WRITE else ""
        receipt_id = stable_id(
            "brclipboard",
            action_id,
            origin,
            access,
            browser_context_id,
            self.maximum_chars,
            content_digest,
        )
        return ClipboardReceipt(
            receipt_id=receipt_id,
            action_id=action_id,
            origin=origin,
            access=access,
            browser_context_id=browser_context_id,
            maximum_chars=self.maximum_chars,
            content_digest=content_digest,
        )

    def read(self, receipt: ClipboardReceipt, *, target_url: str) -> str:
        self._consume(receipt, target_url=target_url, expected_access=ClipboardAccess.READ)
        self.port.grant(
            origin=receipt.origin,
            browser_context_id=receipt.browser_context_id,
            read=True,
            write=False,
        )
        try:
            value = self.port.read_text(origin=receipt.origin, browser_context_id=receipt.browser_context_id)
            if len(value) > receipt.maximum_chars:
                raise ClipboardGuardError("clipboard_result_too_large", "clipboard read exceeds character limit")
            return value
        finally:
            self.port.revoke(origin=receipt.origin, browser_context_id=receipt.browser_context_id)

    def write(self, receipt: ClipboardReceipt, value: str, *, target_url: str) -> None:
        self._consume(receipt, target_url=target_url, expected_access=ClipboardAccess.WRITE)
        if digest_value(value) != receipt.content_digest:
            raise ClipboardGuardError("clipboard_content_changed", "clipboard write content changed after approval")
        if len(value) > receipt.maximum_chars:
            raise ClipboardGuardError("clipboard_value_too_large", "clipboard write exceeds character limit")
        self.port.grant(
            origin=receipt.origin,
            browser_context_id=receipt.browser_context_id,
            read=False,
            write=True,
        )
        try:
            self.port.write_text(value, origin=receipt.origin, browser_context_id=receipt.browser_context_id)
        finally:
            self.port.revoke(origin=receipt.origin, browser_context_id=receipt.browser_context_id)

    def _consume(
        self,
        receipt: ClipboardReceipt,
        *,
        target_url: str,
        expected_access: ClipboardAccess,
    ) -> None:
        if self.disabled:
            raise ClipboardGuardError("clipboard_guard_disabled", "browser clipboard guard is disabled")
        if receipt.access != expected_access:
            raise ClipboardGuardError("clipboard_access_mismatch", "clipboard receipt does not authorize this operation")
        current_origin = canonicalize_url(target_url).origin
        if current_origin != receipt.origin:
            raise ClipboardGuardError(
                "clipboard_origin_changed",
                "clipboard origin changed after approval",
                details={"approved_origin": receipt.origin, "current_origin": current_origin},
            )
        with self._lock:
            if receipt.receipt_id in self._consumed:
                raise ClipboardGuardError("clipboard_receipt_replayed", "clipboard receipt can be consumed only once")
            self._consumed.add(receipt.receipt_id)
