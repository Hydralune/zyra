from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from zyra_core import to_jsonable

from ..browser_state.contracts import BrowserDisclosureBudget, BrowserSelectorMapRevision, digest_json
from ..browser_state.errors import BrowserActionProjectionFailed
from ..browser_state.text import estimate_tokens, sanitize_untrusted_text
from .models import (
    BrowserActionResultProjection,
    BrowserMessageKind,
    BrowserMessagePart,
    BrowserMessageRole,
    message_id,
    stable_projection_id,
)


class BrowserActionResultProjector:
    """Normalize 04A action receipts into bounded atomic tool pairs."""

    def __init__(self, *, disabled: bool = False) -> None:
        self.disabled = disabled
        self._projected = 0
        self._failed = 0
        self._externalized = 0

    def project_many(
        self,
        receipts: Iterable[Any],
        *,
        budget: BrowserDisclosureBudget,
        selector_revision: BrowserSelectorMapRevision,
        capture_id: str,
        artifact_ids: Sequence[str] = (),
    ) -> tuple[BrowserActionResultProjection, ...]:
        values = tuple(receipts)
        if self.disabled and values:
            raise BrowserActionProjectionFailed("browser action result projector is disabled")
        result: list[BrowserActionResultProjection] = []
        seen: set[str] = set()
        for receipt in values:
            projected = self.project(
                receipt,
                budget=budget,
                selector_revision=selector_revision,
                capture_id=capture_id,
                artifact_ids=artifact_ids,
            )
            if projected.projection_id in seen:
                continue
            seen.add(projected.projection_id)
            result.append(projected)
        result.sort(key=lambda item: (item.step_index, item.receipt_id))
        return tuple(result)

    def project(
        self,
        receipt: Any,
        *,
        budget: BrowserDisclosureBudget,
        selector_revision: BrowserSelectorMapRevision,
        capture_id: str,
        artifact_ids: Sequence[str] = (),
    ) -> BrowserActionResultProjection:
        if self.disabled:
            raise BrowserActionProjectionFailed("browser action result projector is disabled")
        raw = _mapping(_to_dict(receipt))
        receipt_id = str(raw.get("receipt_id") or "")
        request_id = str(raw.get("request_id") or "")
        request_fingerprint = str(raw.get("request_fingerprint") or "")
        browser_session_id = str(raw.get("browser_session_id") or selector_revision.identity.browser_session_id)
        if not receipt_id or not request_id or not request_fingerprint:
            self._failed += 1
            raise BrowserActionProjectionFailed(
                "browser action receipt identity is incomplete",
                details={
                    "receipt_id": receipt_id,
                    "request_id": request_id,
                    "request_fingerprint": request_fingerprint,
                },
            )
        action = str(raw.get("action") or "unknown")
        step_index = _int(raw.get("step_index"))
        status = str(raw.get("status") or "failed")
        ok = bool(raw.get("ok")) or status == "succeeded"
        error_code = str(raw.get("error_code") or ("" if ok else "browser_action_failed"))
        error_message, error_signals = sanitize_untrusted_text(raw.get("error_message") or "", limit=1200)
        output = _mapping(raw.get("output"))
        encoded = json.dumps(to_jsonable(output), ensure_ascii=False, sort_keys=True, default=str)
        original_bytes = len(encoded.encode("utf-8"))
        max_chars = max(128, int(budget.max_action_result_tokens * 3.6))
        preview, preview_signals = sanitize_untrusted_text(encoded, limit=max_chars)
        projected_bytes = len(preview.encode("utf-8"))
        handoff_ids = _artifact_ids(raw.get("artifact_handoffs"))
        all_artifacts = tuple(dict.fromkeys((*artifact_ids, *handoff_ids)))
        externalized = original_bytes > projected_bytes
        if externalized:
            self._externalized += 1
        summary = _summary(action, ok, output, error_code, error_message)
        selector_refs = _selector_refs(output, selector_revision)
        projection_id = stable_projection_id(receipt_id, request_fingerprint)
        call_content = json.dumps(
            {
                "tool_call_id": request_id,
                "tool_name": f"browser.{action}",
                "step_index": step_index,
                "selector_revision_id": selector_revision.revision_id,
                "selector_refs": list(selector_refs),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        result_content = json.dumps(
            {
                "tool_call_id": request_id,
                "ok": ok,
                "status": status,
                "summary": summary,
                "error_code": error_code,
                "error_message": error_message,
                "output_preview": preview,
                "artifact_ids": list(all_artifacts),
                "original_output_bytes": original_bytes,
                "projected_output_bytes": projected_bytes,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        call = BrowserMessagePart(
            message_id=message_id("call", request_id),
            role=BrowserMessageRole.ASSISTANT,
            kind=BrowserMessageKind.ACTION_CALL,
            content=call_content,
            source_id=request_id,
            causation_id=receipt_id,
            selector_refs=selector_refs,
            trust="zyra_runtime",
            priority=850,
            token_estimate=estimate_tokens(call_content),
            metadata={"projection_id": projection_id, "request_fingerprint": request_fingerprint},
        )
        result = BrowserMessagePart(
            message_id=message_id("result", receipt_id),
            role=BrowserMessageRole.TOOL,
            kind=BrowserMessageKind.ACTION_RESULT,
            content=result_content,
            source_id=receipt_id,
            causation_id=request_id,
            artifact_ids=all_artifacts,
            selector_refs=selector_refs,
            trust="external_untrusted",
            priority=900 if not ok else 800,
            token_estimate=estimate_tokens(result_content),
            metadata={"projection_id": projection_id, "error": error_code, "ok": ok},
        )
        self._projected += 1
        return BrowserActionResultProjection(
            projection_id=projection_id,
            receipt_id=receipt_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            browser_session_id=browser_session_id,
            action=action,
            step_index=step_index,
            ok=ok,
            status=status,
            summary=summary,
            error_code=error_code,
            error_message=error_message,
            output_preview=preview,
            original_output_bytes=original_bytes,
            projected_output_bytes=projected_bytes,
            artifact_ids=all_artifacts,
            tool_call_message=call,
            tool_result_message=result,
            selector_revision_id=selector_revision.revision_id,
            selector_refs=selector_refs,
            capture_id=capture_id,
            budget_decision={
                "max_action_result_tokens": budget.max_action_result_tokens,
                "original_tokens": estimate_tokens(encoded),
                "projected_tokens": estimate_tokens(preview),
                "externalized": externalized,
                "artifact_refs_preserved": bool(all_artifacts),
            },
            metadata={
                "source": "browser-use ActionResult + OMP tool-pair normalization",
                "prompt_injection_signals": list((*error_signals, *preview_signals)),
                "pair_atomic": True,
            },
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserActionResultProjector",
            "owner_unit": "M1-S04B-01",
            "disabled": self.disabled,
            "projected": self._projected,
            "failed": self._failed,
            "externalized": self._externalized,
        }


def _summary(action: str, ok: bool, output: Mapping[str, Any], error_code: str, error_message: str) -> str:
    if not ok:
        return f"browser.{action} failed: {error_code or error_message or 'unknown error'}"
    for key in ("summary", "message", "title", "url"):
        if output.get(key):
            value, _ = sanitize_untrusted_text(output[key], limit=300)
            return f"browser.{action} succeeded: {value}"
    return f"browser.{action} succeeded"


def _selector_refs(output: Mapping[str, Any], revision: BrowserSelectorMapRevision) -> tuple[str, ...]:
    requested: list[Any] = []
    for key in ("selector_ref", "selector_refs", "element_ref", "index", "backend_node_id"):
        value = output.get(key)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            requested.extend(value)
        elif value not in (None, ""):
            requested.append(value)
    result: list[str] = []
    for value in requested:
        text = str(value)
        for entry in revision.entries:
            if text in {
                entry.ref.opaque_ref,
                str(entry.ref.selector_index),
                str(entry.backend_node_id),
            }:
                result.append(entry.ref.opaque_ref)
                break
    return tuple(dict.fromkeys(result))


def _artifact_ids(value: Any) -> tuple[str, ...]:
    result: list[str] = []
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            raw = _mapping(_to_dict(item))
            artifact_id = str(raw.get("artifact_id") or raw.get("handoff_id") or "")
            if artifact_id:
                result.append(artifact_id)
    return tuple(dict.fromkeys(result))


def _to_dict(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        if isinstance(result, Mapping):
            return result
    data = to_jsonable(value)
    return data if isinstance(data, Mapping) else {}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
