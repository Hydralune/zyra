from __future__ import annotations

"""OMP-inspired browser action-result coercion and artifact custody.

The 04A action runtime has a strong receipt type, but recovery, hooks, legacy
imports, and external browser-use callbacks can still yield partial or
malformed values.  This module closes that boundary before the existing
``BrowserActionResultProjector`` creates atomic assistant/tool message pairs.
Every input produces exactly one normalized result.  Oversized original output
is stored in the existing artifact owner and is causally linked to the receipt;
the inline preview remains bounded.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, now_iso, to_jsonable

from ..browser_state.contracts import BrowserDisclosureBudget, digest_json
from ..browser_state.errors import BrowserActionProjectionFailed
from ..browser_state.text import sanitize_untrusted_text


class BrowserActionEnvelopeStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    PARTIAL = "partial"
    SKIPPED = "skipped"
    ABORTED = "aborted"
    MALFORMED = "malformed"
    CONFLICT = "conflict"


@dataclass(frozen=True, slots=True)
class BrowserActionOutputArtifact:
    artifact: ArtifactRef
    receipt_id: str
    request_id: str
    action: str
    original_bytes: int
    digest: str
    verified: bool
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-action-output-artifact.v1",
            "artifact": to_jsonable(self.artifact),
            "artifact_id": self.artifact.artifact_id,
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "action": self.action,
            "original_bytes": self.original_bytes,
            "digest": self.digest,
            "verified": self.verified,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class NormalizedBrowserActionReceipt:
    receipt_id: str
    request_id: str
    request_fingerprint: str
    browser_session_id: str
    worker_request_id: str
    action: str
    step_index: int
    ok: bool
    status: BrowserActionEnvelopeStatus
    output: Mapping[str, Any]
    error_code: str = ""
    error_message: str = ""
    artifact_handoffs: tuple[Mapping[str, Any], ...] = ()
    partial_sequence: int = 0
    partial_final: bool = True
    normalization_findings: tuple[str, ...] = ()
    raw_shape: str = "mapping"
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        if not self.receipt_id or not self.request_id or not self.request_fingerprint:
            raise BrowserActionProjectionFailed("normalized browser action identity is incomplete")
        if self.status in {
            BrowserActionEnvelopeStatus.FAILED,
            BrowserActionEnvelopeStatus.PARTIAL,
            BrowserActionEnvelopeStatus.SKIPPED,
            BrowserActionEnvelopeStatus.ABORTED,
            BrowserActionEnvelopeStatus.MALFORMED,
            BrowserActionEnvelopeStatus.CONFLICT,
        } and self.ok:
            raise BrowserActionProjectionFailed("non-final browser action envelope cannot be successful")
        object.__setattr__(self, "output", dict(self.output))

    @property
    def payload_digest(self) -> str:
        return digest_json({
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "action": self.action,
            "step_index": self.step_index,
            "ok": self.ok,
            "status": str(self.status),
            "output": self.output,
            "error_code": self.error_code,
            "error_message": self.error_message,
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "request_id": self.request_id,
            "request_fingerprint": self.request_fingerprint,
            "browser_session_id": self.browser_session_id,
            "worker_request_id": self.worker_request_id,
            "action": self.action,
            "step_index": self.step_index,
            "ok": self.ok,
            "status": str(self.status),
            "output": to_jsonable(dict(self.output)),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "artifact_handoffs": [to_jsonable(dict(item)) for item in self.artifact_handoffs],
            "partial_sequence": self.partial_sequence,
            "partial_final": self.partial_final,
            "normalization_findings": list(self.normalization_findings),
            "raw_shape": self.raw_shape,
            "payload_digest": self.payload_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserActionEnvelopeBatch:
    receipts: tuple[NormalizedBrowserActionReceipt, ...]
    artifacts: tuple[BrowserActionOutputArtifact, ...]
    input_count: int
    malformed_count: int
    partial_count: int
    conflict_count: int
    externalized_count: int
    original_output_bytes: int
    inline_output_bytes: int
    findings: tuple[str, ...]

    @property
    def pair_complete(self) -> bool:
        return len(self.receipts) == self.input_count and all(
            item.receipt_id and item.request_id for item in self.receipts
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipts": [item.to_dict() for item in self.receipts],
            "artifacts": [item.to_dict() for item in self.artifacts],
            "input_count": self.input_count,
            "output_count": len(self.receipts),
            "malformed_count": self.malformed_count,
            "partial_count": self.partial_count,
            "conflict_count": self.conflict_count,
            "externalized_count": self.externalized_count,
            "original_output_bytes": self.original_output_bytes,
            "inline_output_bytes": self.inline_output_bytes,
            "pair_complete": self.pair_complete,
            "findings": list(self.findings),
        }


class BrowserActionOutputExternalizer:
    """Write and verify original action output using LocalArtifactStore."""

    def __init__(self, artifact_store: Any, *, disabled: bool = False) -> None:
        self.artifact_store = artifact_store
        self.disabled = disabled
        self._writes = 0
        self._bytes = 0
        self._failures = 0

    def externalize(
        self,
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        receipt_id: str,
        request_id: str,
        request_fingerprint: str,
        action: str,
        output: Any,
        status: str,
        error_code: str,
        error_message: str,
    ) -> BrowserActionOutputArtifact:
        if self.disabled:
            raise BrowserActionProjectionFailed("browser action output externalizer is disabled")
        payload = {
            "schema": "zyra.browser-action-output.v1",
            "receipt_id": receipt_id,
            "request_id": request_id,
            "request_fingerprint": request_fingerprint,
            "action": action,
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "output": to_jsonable(output),
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        encoded = text.encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        try:
            # Write bytes rather than text so Windows newline translation cannot
            # change the content-addressed payload after its digest is computed.
            artifact = self.artifact_store.write_bytes(
                run_id=run_id,
                task_id=task_id,
                content=encoded,
                title=f"Browser action output {receipt_id}",
                kind=ArtifactKind.STRUCTURED_DATA,
                extension=".json",
                producer_node_id=node_id or None,
                metadata={
                    "content_type": "application/json; charset=utf-8",
                    "sha256": digest,
                    "schema": "zyra.browser-action-output.v1",
                },
            )
            path = self.artifact_store.resolve_path(artifact)
            restored = path.read_bytes()
            actual = "sha256:" + hashlib.sha256(restored).hexdigest()
            if actual != digest or restored != encoded:
                raise ValueError("browser action output artifact failed round-trip verification")
        except Exception as error:
            self._failures += 1
            raise BrowserActionProjectionFailed(
                "browser action output artifact externalization failed",
                details={
                    "receipt_id": receipt_id,
                    "request_id": request_id,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        self._writes += 1
        self._bytes += len(encoded)
        return BrowserActionOutputArtifact(
            artifact=artifact,
            receipt_id=receipt_id,
            request_id=request_id,
            action=action,
            original_bytes=len(encoded),
            digest=digest,
            verified=True,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserActionOutputExternalizer",
            "owner_unit": "M1-S04B-02",
            "artifact_owner": type(self.artifact_store).__name__,
            "disabled": self.disabled,
            "writes": self._writes,
            "bytes": self._bytes,
            "failures": self._failures,
        }


class BrowserActionEnvelopeNormalizer:
    """Coerce arbitrary action results into one stable receipt per call."""

    def __init__(
        self,
        *,
        externalizer: BrowserActionOutputExternalizer,
        disabled: bool = False,
    ) -> None:
        self.externalizer = externalizer
        self.disabled = disabled
        self._batches = 0
        self._normalized = 0
        self._malformed = 0
        self._partial = 0
        self._conflicts = 0

    def normalize_many(
        self,
        receipts: Iterable[Any],
        *,
        run_id: str,
        task_id: str,
        node_id: str,
        browser_session_id: str,
        worker_request_id: str,
        budget: BrowserDisclosureBudget,
    ) -> BrowserActionEnvelopeBatch:
        values = tuple(receipts)
        if self.disabled and values:
            raise BrowserActionProjectionFailed("browser action envelope normalizer is disabled")
        normalized: list[NormalizedBrowserActionReceipt] = []
        artifacts: list[BrowserActionOutputArtifact] = []
        findings: list[str] = []
        original_bytes = 0
        inline_bytes = 0
        identities: dict[str, str] = {}
        malformed_count = partial_count = conflict_count = 0

        for index, value in enumerate(values):
            item, raw_output, item_findings = self._normalize_one(
                value,
                index=index,
                browser_session_id=browser_session_id,
                worker_request_id=worker_request_id,
            )
            encoded = json.dumps(to_jsonable(raw_output), ensure_ascii=False, sort_keys=True, default=str)
            output_bytes = len(encoded.encode("utf-8"))
            original_bytes += output_bytes
            previous = identities.get(item.request_id)
            if previous is not None and previous != item.request_fingerprint:
                conflict_count += 1
                item_findings = (*item_findings, "duplicate_request_identity_conflict")
                item = NormalizedBrowserActionReceipt(
                    receipt_id=item.receipt_id,
                    request_id=item.request_id,
                    request_fingerprint=item.request_fingerprint,
                    browser_session_id=item.browser_session_id,
                    worker_request_id=item.worker_request_id,
                    action=item.action,
                    step_index=item.step_index,
                    ok=False,
                    status=BrowserActionEnvelopeStatus.CONFLICT,
                    output={"conflicting_request_fingerprint": item.request_fingerprint},
                    error_code="browser_action_request_identity_conflict",
                    error_message="duplicate browser request id carried different arguments",
                    artifact_handoffs=item.artifact_handoffs,
                    partial_sequence=item.partial_sequence,
                    partial_final=False,
                    normalization_findings=item_findings,
                    raw_shape=item.raw_shape,
                )
            identities[item.request_id] = item.request_fingerprint

            must_externalize = output_bytes > max(1, int(budget.raw_externalize_bytes))
            must_externalize = must_externalize or "unsupported_output_shape" in item_findings
            if must_externalize:
                artifact = self.externalizer.externalize(
                    run_id=run_id,
                    task_id=task_id,
                    node_id=node_id,
                    receipt_id=item.receipt_id,
                    request_id=item.request_id,
                    request_fingerprint=item.request_fingerprint,
                    action=item.action,
                    output=raw_output,
                    status=str(item.status),
                    error_code=item.error_code,
                    error_message=item.error_message,
                )
                artifacts.append(artifact)
                handoff = {
                    "artifact_id": artifact.artifact.artifact_id,
                    "digest": artifact.digest,
                    "original_bytes": output_bytes,
                    "receipt_id": item.receipt_id,
                    "request_id": item.request_id,
                    "purpose": "browser_action_original_output",
                    "verified": artifact.verified,
                }
                bounded_output = self._bounded_output(raw_output, max_chars=max(256, budget.max_action_result_tokens * 3))
                item = NormalizedBrowserActionReceipt(
                    receipt_id=item.receipt_id,
                    request_id=item.request_id,
                    request_fingerprint=item.request_fingerprint,
                    browser_session_id=item.browser_session_id,
                    worker_request_id=item.worker_request_id,
                    action=item.action,
                    step_index=item.step_index,
                    ok=item.ok,
                    status=item.status,
                    output={
                        **bounded_output,
                        "_zyra_externalized": True,
                        "_zyra_original_output_artifact_id": artifact.artifact.artifact_id,
                        "_zyra_original_output_digest": artifact.digest,
                        "_zyra_original_output_bytes": output_bytes,
                    },
                    error_code=item.error_code,
                    error_message=item.error_message,
                    artifact_handoffs=(*item.artifact_handoffs, handoff),
                    partial_sequence=item.partial_sequence,
                    partial_final=item.partial_final,
                    normalization_findings=(*item.normalization_findings, "original_output_externalized"),
                    raw_shape=item.raw_shape,
                )
            inline = json.dumps(to_jsonable(item.output), ensure_ascii=False, sort_keys=True, default=str)
            inline_bytes += len(inline.encode("utf-8"))
            malformed_count += item.status == BrowserActionEnvelopeStatus.MALFORMED
            partial_count += item.status == BrowserActionEnvelopeStatus.PARTIAL
            findings.extend(f"{item.receipt_id}:{finding}" for finding in item.normalization_findings)
            normalized.append(item)

        self._batches += 1
        self._normalized += len(normalized)
        self._malformed += malformed_count
        self._partial += partial_count
        self._conflicts += conflict_count
        return BrowserActionEnvelopeBatch(
            receipts=tuple(normalized),
            artifacts=tuple(artifacts),
            input_count=len(values),
            malformed_count=malformed_count,
            partial_count=partial_count,
            conflict_count=conflict_count,
            externalized_count=len(artifacts),
            original_output_bytes=original_bytes,
            inline_output_bytes=inline_bytes,
            findings=tuple(findings),
        )

    def _normalize_one(
        self,
        value: Any,
        *,
        index: int,
        browser_session_id: str,
        worker_request_id: str,
    ) -> tuple[NormalizedBrowserActionReceipt, Any, tuple[str, ...]]:
        raw, raw_shape, findings = _coerce_mapping(value)
        stable_raw = to_jsonable(raw if raw else value)
        value_digest = digest_json(stable_raw)
        request_id = str(
            raw.get("request_id")
            or raw.get("tool_call_id")
            or raw.get("call_id")
            or _stable_id("browser_request", worker_request_id, str(index), value_digest)
        )
        request_fingerprint = str(
            raw.get("request_fingerprint")
            or raw.get("arguments_digest")
            or digest_json({
                "request_id": request_id,
                "action": raw.get("action") or raw.get("tool_name"),
                "arguments": raw.get("arguments") or raw.get("input") or {},
            })
        )
        receipt_id = str(
            raw.get("receipt_id")
            or raw.get("result_id")
            or _stable_id("browser_receipt", worker_request_id, request_id, value_digest)
        )
        action = str(raw.get("action") or raw.get("tool_name") or "unknown")
        if action.startswith("browser."):
            action = action.split(".", 1)[1]
        action = action or "unknown"
        step_index = _integer(raw.get("step_index"), default=index)
        session_id = str(raw.get("browser_session_id") or browser_session_id)
        partial_sequence = _integer(raw.get("partial_sequence") or raw.get("sequence"), default=0)
        partial_flag = bool(raw.get("partial") or raw.get("is_partial") or raw.get("streaming"))
        final_flag = bool(raw.get("final", not partial_flag))
        skipped = bool(raw.get("skipped"))
        aborted = bool(raw.get("aborted") or raw.get("cancelled"))
        status_text = str(raw.get("status") or "").lower()
        error_value = raw.get("error")
        error_code = str(raw.get("error_code") or "")
        error_message_raw = raw.get("error_message") or raw.get("message") or ""
        if isinstance(error_value, Mapping):
            error_code = error_code or str(error_value.get("code") or "")
            error_message_raw = error_message_raw or error_value.get("message") or error_value
        elif error_value not in (None, False, ""):
            error_message_raw = error_message_raw or error_value
        error_message, signals = sanitize_untrusted_text(error_message_raw, limit=2000)
        findings = (*findings, *(f"error_{signal}" for signal in signals))
        raw_output = _extract_output(raw)
        output, output_findings = _coerce_output(raw_output)
        findings = (*findings, *output_findings)

        if raw_shape not in {"mapping", "typed_object", "dataclass"}:
            status = BrowserActionEnvelopeStatus.MALFORMED
            ok = False
            error_code = error_code or "browser_action_result_malformed"
            error_message = error_message or f"browser action result used unsupported {raw_shape} shape"
        elif aborted:
            status = BrowserActionEnvelopeStatus.ABORTED
            ok = False
            error_code = error_code or "browser_action_aborted"
            error_message = error_message or "browser action was aborted before a final result"
        elif skipped:
            status = BrowserActionEnvelopeStatus.SKIPPED
            ok = False
            error_code = error_code or "browser_action_skipped"
            error_message = error_message or "browser action was skipped"
        elif partial_flag or not final_flag or status_text in {"partial", "streaming", "in_progress"}:
            status = BrowserActionEnvelopeStatus.PARTIAL
            ok = False
            error_code = error_code or "browser_action_partial_result"
            error_message = error_message or "browser action did not produce a final result"
        else:
            explicit_ok = raw.get("ok")
            inferred_ok = status_text in {"ok", "success", "succeeded", "complete", "completed"}
            inferred_failure = status_text in {"error", "failed", "failure"} or bool(error_code or error_message)
            ok = bool(explicit_ok) if explicit_ok is not None else (inferred_ok and not inferred_failure)
            if explicit_ok is None and not status_text and not error_code and not error_message:
                ok = True
                findings = (*findings, "status_inferred_from_result_presence")
            status = BrowserActionEnvelopeStatus.SUCCEEDED if ok else BrowserActionEnvelopeStatus.FAILED
            if not ok:
                error_code = error_code or "browser_action_failed"
                error_message = error_message or "browser action returned a failed result without details"

        return NormalizedBrowserActionReceipt(
            receipt_id=receipt_id,
            request_id=request_id,
            request_fingerprint=request_fingerprint,
            browser_session_id=session_id,
            worker_request_id=str(raw.get("worker_request_id") or worker_request_id),
            action=action,
            step_index=step_index,
            ok=ok,
            status=status,
            output=output,
            error_code=error_code,
            error_message=error_message,
            artifact_handoffs=tuple(
                dict(item) for item in _sequence(raw.get("artifact_handoffs")) if isinstance(item, Mapping)
            ),
            partial_sequence=partial_sequence,
            partial_final=final_flag and not partial_flag,
            normalization_findings=tuple(dict.fromkeys(findings)),
            raw_shape=raw_shape,
        ), raw_output, tuple(dict.fromkeys(findings))

    def _bounded_output(self, value: Any, *, max_chars: int) -> dict[str, Any]:
        encoded = json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, default=str)
        preview, signals = sanitize_untrusted_text(encoded, limit=max_chars)
        return {
            "preview": preview,
            "preview_truncated": len(preview) < len(encoded),
            "prompt_injection_signals": list(signals),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserActionEnvelopeNormalizer",
            "owner_unit": "M1-S04B-02",
            "disabled": self.disabled,
            "batches": self._batches,
            "normalized": self._normalized,
            "malformed": self._malformed,
            "partial": self._partial,
            "conflicts": self._conflicts,
            "externalizer": self.externalizer.snapshot(),
        }


def _coerce_mapping(value: Any) -> tuple[Mapping[str, Any], str, tuple[str, ...]]:
    if isinstance(value, Mapping):
        return dict(value), "mapping", ()
    method = getattr(value, "to_dict", None)
    if callable(method):
        result = method()
        if isinstance(result, Mapping):
            return dict(result), "typed_object", ()
    converted = to_jsonable(value)
    if isinstance(converted, Mapping):
        return dict(converted), "dataclass", ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return {"content": list(value)}, "sequence", ("unsupported_output_shape",)
    if value is None:
        return {}, "null", ("unsupported_output_shape", "missing_result")
    return {"content": str(value)}, type(value).__name__, ("unsupported_output_shape",)


def _extract_output(raw: Mapping[str, Any]) -> Any:
    for key in ("output", "result", "content", "data", "details"):
        if key in raw:
            return raw[key]
    return {}


def _coerce_output(value: Any) -> tuple[Mapping[str, Any], tuple[str, ...]]:
    if isinstance(value, Mapping):
        return dict(value), ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        text_parts: list[str] = []
        supported: list[Mapping[str, Any]] = []
        ignored = 0
        for item in value:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, Mapping):
                item_type = str(item.get("type") or "text")
                if item_type in {"text", "output_text", "input_text"}:
                    text_parts.append(str(item.get("text") or item.get("content") or ""))
                    supported.append(dict(item))
                elif item_type in {"image", "image_url", "artifact", "file"}:
                    supported.append(dict(item))
                else:
                    ignored += 1
            else:
                ignored += 1
        result: dict[str, Any] = {"content_blocks": supported}
        if text_parts:
            result["text"] = "\n".join(text_parts)
        if ignored:
            result["ignored_content_blocks"] = ignored
        return result, (("unsupported_content_blocks_removed",) if ignored else ())
    if value is None:
        return {}, ("missing_output_normalized",)
    if isinstance(value, bytes):
        return {"text": value.decode("utf-8", errors="replace")}, ("binary_output_decoded",)
    return {"text": str(value)}, ("scalar_output_normalized",)


def _stable_id(prefix: str, *parts: str) -> str:
    encoded = ":".join(parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:24]}"


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()
