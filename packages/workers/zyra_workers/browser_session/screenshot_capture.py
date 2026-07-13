from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .errors import BrowserArtifactError
from .models import browser_id, browser_now


class ScreenshotCapturePhase(StrEnum):
    PREPARE = "prepare"
    HIGHLIGHTS_REMOVED = "highlights_removed"
    CAPTURED = "captured"
    HIGHLIGHTS_RESTORED = "highlights_restored"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HighlightCapturePolicy:
    maximum_bytes: int = 25_000_000
    require_highlight_cleanup: bool = True
    require_restore: bool = True
    allow_formats: frozenset[str] = frozenset({"png", "jpeg", "webp"})

    def __post_init__(self) -> None:
        if self.maximum_bytes < 64:
            raise ValueError("screenshot byte limit is too small")
        if not self.allow_formats:
            raise ValueError("at least one screenshot format is required")


@dataclass(frozen=True, slots=True)
class HighlightMutationReceipt:
    token: str
    matched_elements: int
    hidden_overlays: int
    changed_attributes: int
    phase: ScreenshotCapturePhase
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "matched_elements": self.matched_elements,
            "hidden_overlays": self.hidden_overlays,
            "changed_attributes": self.changed_attributes,
            "phase": str(self.phase),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class HighlightFreeScreenshot:
    content: bytes
    image_format: str
    sha256: str
    capture_id: str
    target_id: str = ""
    cdp_session_id: str = ""
    captured_at: str = field(default_factory=browser_now)
    highlight_removed: bool = True
    highlight_restored: bool = True
    mutation: HighlightMutationReceipt | None = None
    restore_error: str = ""

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("screenshot content must not be empty")
        if not self.sha256.startswith("sha256:"):
            raise ValueError("screenshot receipt requires sha256")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-session.highlight-free-screenshot.v1",
            "capture_id": self.capture_id,
            "image_format": self.image_format,
            "size_bytes": len(self.content),
            "sha256": self.sha256,
            "target_id": self.target_id,
            "cdp_session_id": self.cdp_session_id,
            "captured_at": self.captured_at,
            "highlight_removed": self.highlight_removed,
            "highlight_restored": self.highlight_restored,
            "mutation": self.mutation.to_dict() if self.mutation else None,
            "restore_error": self.restore_error,
            "capture_owner": "M1-04A/04C",
            "evidence_owner": "M1-04D",
        }


class HighlightFreeScreenshotCapture:
    """Capture screenshot bytes while page highlights are temporarily hidden."""

    def __init__(self, *, policy: HighlightCapturePolicy | None = None) -> None:
        self.policy = policy or HighlightCapturePolicy()

    def capture(
        self,
        send: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        *,
        image_format: str,
        capture_beyond_viewport: bool,
        from_surface: bool = True,
        quality: int | None = None,
        target_id: str = "",
        cdp_session_id: str = "",
    ) -> HighlightFreeScreenshot:
        image_format = str(image_format).casefold()
        if image_format not in self.policy.allow_formats:
            raise BrowserArtifactError(
                f"unsupported screenshot format {image_format!r}",
                operation="Page.captureScreenshot",
            )
        token = browser_id("highlight")
        mutation: HighlightMutationReceipt | None = None
        restore_error = ""
        content = b""
        try:
            mutation = self._remove(send, token)
            if self.policy.require_highlight_cleanup and mutation.phase != ScreenshotCapturePhase.HIGHLIGHTS_REMOVED:
                raise BrowserArtifactError(
                    mutation.error or "page highlights could not be removed",
                    operation="Runtime.evaluate/remove_highlights",
                )
            params: dict[str, Any] = {
                "format": image_format,
                "captureBeyondViewport": bool(capture_beyond_viewport),
                "fromSurface": bool(from_surface),
            }
            if image_format in {"jpeg", "webp"}:
                if quality is None or not 1 <= int(quality) <= 100:
                    raise BrowserArtifactError(
                        "lossy screenshot quality must be between 1 and 100",
                        operation="Page.captureScreenshot",
                    )
                params["quality"] = int(quality)
            response = send("Page.captureScreenshot", params)
            if response.get("error"):
                raise BrowserArtifactError(
                    str(response["error"]),
                    operation="Page.captureScreenshot",
                )
            content = self._decode(response.get("data"))
            if len(content) > self.policy.maximum_bytes:
                raise BrowserArtifactError(
                    "screenshot exceeds productized byte limit",
                    operation="Page.captureScreenshot",
                )
        finally:
            if mutation is not None:
                try:
                    restored = self._restore(send, token)
                    if not restored and self.policy.require_restore:
                        raise BrowserArtifactError(
                            "page highlight state could not be restored",
                            operation="Runtime.evaluate/restore_highlights",
                        )
                except Exception as exc:
                    restore_error = f"{type(exc).__name__}: {exc}"
                    if self.policy.require_restore:
                        raise
        if not content:
            raise BrowserArtifactError(
                "CDP screenshot returned no image bytes",
                operation="Page.captureScreenshot",
            )
        return HighlightFreeScreenshot(
            content=content,
            image_format=image_format,
            sha256="sha256:" + hashlib.sha256(content).hexdigest(),
            capture_id=browser_id("screenshot"),
            target_id=target_id,
            cdp_session_id=cdp_session_id,
            highlight_removed=mutation is not None,
            highlight_restored=not bool(restore_error),
            mutation=mutation,
            restore_error=restore_error,
        )

    @staticmethod
    def _remove(
        send: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        token: str,
    ) -> HighlightMutationReceipt:
        response = send(
            "Runtime.evaluate",
            {
                "expression": _remove_expression(token),
                "awaitPromise": False,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        exception = response.get("exceptionDetails")
        if exception:
            return HighlightMutationReceipt(
                token=token,
                matched_elements=0,
                hidden_overlays=0,
                changed_attributes=0,
                phase=ScreenshotCapturePhase.FAILED,
                error=str(exception),
            )
        result = response.get("result")
        value = result.get("value") if isinstance(result, Mapping) else None
        if not isinstance(value, Mapping) or not bool(value.get("ok")):
            return HighlightMutationReceipt(
                token=token,
                matched_elements=0,
                hidden_overlays=0,
                changed_attributes=0,
                phase=ScreenshotCapturePhase.FAILED,
                error=str(value.get("error") if isinstance(value, Mapping) else "invalid cleanup result"),
            )
        return HighlightMutationReceipt(
            token=token,
            matched_elements=int(value.get("matched") or 0),
            hidden_overlays=int(value.get("hidden") or 0),
            changed_attributes=int(value.get("attributes") or 0),
            phase=ScreenshotCapturePhase.HIGHLIGHTS_REMOVED,
        )

    @staticmethod
    def _restore(
        send: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
        token: str,
    ) -> bool:
        response = send(
            "Runtime.evaluate",
            {
                "expression": _restore_expression(token),
                "awaitPromise": False,
                "returnByValue": True,
                "userGesture": False,
            },
        )
        if response.get("exceptionDetails"):
            return False
        result = response.get("result")
        value = result.get("value") if isinstance(result, Mapping) else None
        return bool(value.get("ok")) if isinstance(value, Mapping) else False

    @staticmethod
    def _decode(value: Any) -> bytes:
        encoded = str(value or "")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError, binascii.Error) as exc:
            raise BrowserArtifactError(
                "CDP screenshot returned invalid base64 data",
                operation="Page.captureScreenshot",
            ) from exc
        if not content:
            raise BrowserArtifactError(
                "CDP screenshot returned empty data",
                operation="Page.captureScreenshot",
            )
        return content


def _remove_expression(token: str) -> str:
    safe = json_string(token)
    return f"""
(() => {{
  const token = {safe};
  const key = '__zyraHighlightBackup';
  const selectors = [
    '[data-zyra-highlight]',
    '[data-browser-use-highlight]',
    '[data-highlight-index]',
    '.browser-use-highlight',
    '.zyra-browser-highlight',
    '[id^="playwright-highlight"]'
  ];
  const nodes = Array.from(document.querySelectorAll(selectors.join(',')));
  const backup = [];
  let hidden = 0;
  let attributes = 0;
  for (const element of nodes) {{
    const entry = {{
      element,
      style: element.getAttribute('style'),
      attrs: {{}}
    }};
    for (const name of ['data-zyra-highlight','data-browser-use-highlight','data-highlight-index']) {{
      if (element.hasAttribute(name)) {{
        entry.attrs[name] = element.getAttribute(name);
        element.removeAttribute(name);
        attributes += 1;
      }}
    }}
    element.style.setProperty('outline', 'none', 'important');
    element.style.setProperty('box-shadow', 'none', 'important');
    element.style.setProperty('background-image', 'none', 'important');
    if (element.classList.contains('browser-use-highlight') ||
        element.classList.contains('zyra-browser-highlight') ||
        (element.id || '').startsWith('playwright-highlight')) {{
      element.style.setProperty('visibility', 'hidden', 'important');
      hidden += 1;
    }}
    backup.push(entry);
  }}
  globalThis[key] = {{ token, backup }};
  return {{ ok: true, token, matched: nodes.length, hidden, attributes }};
}})()
""".strip()


def _restore_expression(token: str) -> str:
    safe = json_string(token)
    return f"""
(() => {{
  const token = {safe};
  const key = '__zyraHighlightBackup';
  const state = globalThis[key];
  if (!state || state.token !== token || !Array.isArray(state.backup)) {{
    return {{ ok: false, error: 'highlight backup missing or stale' }};
  }}
  let restored = 0;
  for (const entry of state.backup) {{
    const element = entry.element;
    if (!element || !element.isConnected) continue;
    if (entry.style === null) element.removeAttribute('style');
    else element.setAttribute('style', entry.style);
    for (const [name, value] of Object.entries(entry.attrs || {{}})) {{
      element.setAttribute(name, value);
    }}
    restored += 1;
  }}
  delete globalThis[key];
  return {{ ok: true, token, restored }};
}})()
""".strip()


def json_string(value: str) -> str:
    import json

    return json.dumps(str(value), ensure_ascii=False)
