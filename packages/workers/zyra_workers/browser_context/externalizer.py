from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, to_jsonable

from ..browser_state.artifact_serializer import HTMLSerializer
from ..browser_state.contracts import BrowserDomCapture, BrowserSelectorMapRevision
from ..browser_state.errors import BrowserStateExternalizationFailed
from ..browser_state.text import redact_attributes, sanitize_untrusted_text
from .models import BrowserArtifactExternalization


class BrowserStateArtifactExternalizer:
    """Adapter into the existing LocalArtifactStore; it owns no artifact state."""

    def __init__(self, artifact_store: Any, *, disabled: bool = False) -> None:
        self.artifact_store = artifact_store
        self.disabled = disabled
        self._externalizations = 0
        self._failures = 0
        self._bytes = 0

    def externalize(
        self,
        capture: BrowserDomCapture,
        *,
        selector_revision: BrowserSelectorMapRevision | None = None,
        include_html: bool = True,
    ) -> BrowserArtifactExternalization:
        if self.disabled:
            raise BrowserStateExternalizationFailed("browser state externalizer is disabled")
        raw_bundle = {
            "schema_version": 1,
            "capture": capture.public_dict(),
            "dom": _redact_nested(capture.raw_dom),
            "ax": _redact_nested(capture.raw_ax),
            "snapshot": _redact_nested(capture.raw_snapshot),
            "frames": [frame.to_dict() for frame in capture.frames],
            "selector_revision": selector_revision.to_dict() if selector_revision else None,
        }
        encoded = json.dumps(raw_bundle, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        raw_bytes = len(encoded.encode("utf-8"))
        artifacts: list[ArtifactRef] = []
        digests: dict[str, str] = {}
        try:
            state_artifact = self.artifact_store.write_text(
                run_id=capture.request.run_id,
                task_id=capture.request.task_id,
                content=encoded,
                title=f"Browser DOM state {capture.capture_id}",
                kind=ArtifactKind.STRUCTURED_DATA,
                extension=".json",
                producer_node_id=capture.request.node_id or None,
            )
            artifacts.append(state_artifact)
            digests[state_artifact.artifact_id] = _record_written_text(
                self.artifact_store, state_artifact, encoded
            )
            if include_html:
                html = HTMLSerializer(extract_links=True).serialize(capture.root)
                html_artifact = self.artifact_store.write_text(
                    run_id=capture.request.run_id,
                    task_id=capture.request.task_id,
                    content=html,
                    title=f"Browser enhanced DOM {capture.capture_id}",
                    kind=ArtifactKind.FILE,
                    extension=".html",
                    producer_node_id=capture.request.node_id or None,
                )
                artifacts.append(html_artifact)
                digests[html_artifact.artifact_id] = _record_written_text(
                    self.artifact_store, html_artifact, html
                )
            report = {
                "capture_id": capture.capture_id,
                "state_digest": capture.state_digest,
                "full_state_bytes": capture.metrics.full_state_bytes,
                "full_state_tokens": capture.metrics.full_state_tokens,
                "selector_count": capture.selector_count,
                "selector_revision_id": selector_revision.revision_id if selector_revision else "",
                "artifact_ids": [artifact.artifact_id for artifact in artifacts],
                "digests": digests,
                "authoritative_selector_owner": "BrowserSelectorMapStore",
                "artifact_owner": type(self.artifact_store).__name__,
            }
            report_text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
            report_artifact = self.artifact_store.write_text(
                run_id=capture.request.run_id,
                task_id=capture.request.task_id,
                content=report_text,
                title=f"Browser state disclosure report {capture.capture_id}",
                kind=ArtifactKind.REPORT,
                extension=".json",
                producer_node_id=capture.request.node_id or None,
            )
            artifacts.append(report_artifact)
            digests[report_artifact.artifact_id] = _record_written_text(
                self.artifact_store, report_artifact, report_text
            )
            verified_bytes = sum(_verify_artifact(self.artifact_store, item, digests[item.artifact_id]) for item in artifacts)
        except Exception as error:
            self._failures += 1
            raise BrowserStateExternalizationFailed(
                "browser state artifact externalization failed",
                details={
                    "capture_id": capture.capture_id,
                    "error": f"{type(error).__name__}: {error}",
                    "written_artifact_ids": [item.artifact_id for item in artifacts],
                },
            ) from error
        externalized_bytes = sum(_artifact_size(self.artifact_store, item) for item in artifacts)
        self._externalizations += 1
        self._bytes += externalized_bytes
        return BrowserArtifactExternalization(
            capture_id=capture.capture_id,
            artifacts=tuple(artifacts),
            raw_bytes=raw_bytes,
            externalized_bytes=externalized_bytes,
            verified_bytes=verified_bytes,
            artifact_ids=tuple(item.artifact_id for item in artifacts),
            digests=digests,
            complete=verified_bytes == externalized_bytes,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserStateArtifactExternalizer",
            "owner_unit": "M1-S04B-01",
            "artifact_owner": type(self.artifact_store).__name__,
            "disabled": self.disabled,
            "externalizations": self._externalizations,
            "failures": self._failures,
            "externalized_bytes": self._bytes,
        }


def _redact_nested(value: Any, *, parent_key: str = "", depth: int = 0) -> Any:
    if depth > 128:
        return "[DEPTH-LIMIT]"
    if isinstance(value, Mapping):
        source = {str(key): item for key, item in value.items()}
        if parent_key.casefold() in {"attributes", "attrs"}:
            redacted, _ = redact_attributes(source)
            return redacted
        result: dict[str, Any] = {}
        for key, item in source.items():
            lowered = key.casefold()
            if any(secret in lowered for secret in ("password", "secret", "token", "authorization", "cookie")):
                result[key] = "[REDACTED]"
            else:
                result[key] = _redact_nested(item, parent_key=key, depth=depth + 1)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_nested(item, parent_key=parent_key, depth=depth + 1) for item in value]
    if isinstance(value, str):
        clean, _ = sanitize_untrusted_text(value, limit=max(2000, min(50000, len(value))))
        return clean
    return value


def _verify_artifact(store: Any, artifact: ArtifactRef, expected_digest: str) -> int:
    path = store.resolve_path(artifact)
    payload = path.read_bytes()
    if _sha256(payload) != expected_digest:
        raise ValueError(f"artifact digest mismatch: {artifact.artifact_id}")
    return len(payload)


def _record_written_text(store: Any, artifact: ArtifactRef, expected_text: str) -> str:
    path = store.resolve_path(artifact)
    if path.read_text(encoding="utf-8") != expected_text:
        raise ValueError(f"artifact text mismatch: {artifact.artifact_id}")
    return _sha256(path.read_bytes())


def _artifact_size(store: Any, artifact: ArtifactRef) -> int:
    return store.resolve_path(artifact).stat().st_size


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
