from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType
from zyra_runtime import LocalArtifactStore

from .models import (
    ArtifactLineage,
    ArtifactRole,
    ObservationScope,
    canonical_json,
    digest_value,
)


class BrowserArtifactPublicationError(RuntimeError):
    code = "browser_artifact_publication_error"


@dataclass(frozen=True, slots=True)
class ArtifactPublisherPolicy:
    max_trace_bytes: int = 25_000_000
    max_manifest_bytes: int = 5_000_000
    verify_readback: bool = True
    deduplicate: bool = True
    require_causal_event: bool = True

    def __post_init__(self) -> None:
        if self.max_trace_bytes < 1024:
            raise ValueError("trace artifact budget is too small")
        if self.max_manifest_bytes < 1024:
            raise ValueError("manifest artifact budget is too small")


@dataclass(frozen=True, slots=True)
class ArtifactPublication:
    artifact: ArtifactRef
    lineage: ArtifactLineage
    event: EventRecord
    idempotent: bool = False


class BrowserArtifactPublisher:
    """Publishes observability artifacts through the canonical artifact owner."""

    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        policy: ArtifactPublisherPolicy | None = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or ArtifactPublisherPolicy()
        self._by_digest: dict[str, ArtifactPublication] = {}

    def adopt(
        self,
        scope: ObservationScope,
        artifact: ArtifactRef,
        *,
        role: ArtifactRole,
        source_event_ids: Sequence[str],
        source_record_ids: Sequence[str] = (),
        parent_artifact_ids: Sequence[str] = (),
        quarantined: bool = False,
        metadata: Mapping[str, Any] | None = None,
    ) -> ArtifactPublication:
        if self.policy.require_causal_event and not source_event_ids:
            raise BrowserArtifactPublicationError(
                "artifact publication requires a causal event"
            )
        payload, media_type = self._read_artifact(artifact)
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        lineage = ArtifactLineage(
            scope=scope,
            artifact_id=artifact.artifact_id,
            role=role,
            uri=artifact.uri,
            sha256=digest,
            size_bytes=len(payload),
            source_event_ids=tuple(source_event_ids),
            source_record_ids=tuple(source_record_ids),
            parent_artifact_ids=tuple(parent_artifact_ids),
            media_type=media_type,
            quarantined=quarantined,
            metadata={
                **dict(metadata or {}),
                "canonical_artifact_kind": str(artifact.kind),
                "canonical_artifact_title": artifact.title,
                "producer_node_id": artifact.producer_node_id,
            },
        )
        existing = self._by_digest.get(digest)
        if existing and self.policy.deduplicate:
            return ArtifactPublication(
                artifact=existing.artifact,
                lineage=existing.lineage,
                event=existing.event,
                idempotent=True,
            )
        event = self._event(scope, artifact, lineage)
        publication = ArtifactPublication(artifact, lineage, event)
        self._by_digest[digest] = publication
        return publication

    def publish_trace(
        self,
        scope: ObservationScope,
        trace: Mapping[str, Any],
        *,
        source_event_ids: Sequence[str],
        source_record_ids: Sequence[str],
        title: str = "Browser execution trace",
    ) -> ArtifactPublication:
        payload = (canonical_json(dict(trace)) + "\n").encode("utf-8")
        if len(payload) > self.policy.max_trace_bytes:
            raise BrowserArtifactPublicationError("browser trace exceeds artifact budget")
        artifact = self._write_text(
            payload.decode("utf-8"),
            run_id=scope.run_id,
            task_id=scope.task_id,
            kind=ArtifactKind.TRACE,
            title=title,
            producer_node_id=scope.node_id or None,
            suffix=".json",
            metadata={
                "schema": "zyra.browser-observability.trace-artifact.v1",
                "browser_session_id": scope.browser_session_id,
                "worker_request_id": scope.worker_request_id,
                "owner_unit": "M1-S04D-01",
            },
        )
        return self.adopt(
            scope,
            artifact,
            role=ArtifactRole.TRACE,
            source_event_ids=source_event_ids,
            source_record_ids=source_record_ids,
            metadata={"trace_digest": digest_value(trace)},
        )

    def publish_history_manifest(
        self,
        scope: ObservationScope,
        manifest: Mapping[str, Any],
        *,
        source_event_ids: Sequence[str],
        source_record_ids: Sequence[str],
    ) -> ArtifactPublication:
        payload = (canonical_json(dict(manifest)) + "\n").encode("utf-8")
        if len(payload) > self.policy.max_manifest_bytes:
            raise BrowserArtifactPublicationError(
                "browser history manifest exceeds artifact budget"
            )
        artifact = self._write_text(
            payload.decode("utf-8"),
            run_id=scope.run_id,
            task_id=scope.task_id,
            kind=ArtifactKind.STRUCTURED_DATA,
            title="Browser durable history manifest",
            producer_node_id=scope.node_id or None,
            suffix=".json",
            metadata={
                "schema": "zyra.browser-observability.history-manifest.v1",
                "browser_session_id": scope.browser_session_id,
                "worker_request_id": scope.worker_request_id,
                "owner_unit": "M1-S04D-01",
            },
        )
        return self.adopt(
            scope,
            artifact,
            role=ArtifactRole.HISTORY_SEGMENT,
            source_event_ids=source_event_ids,
            source_record_ids=source_record_ids,
        )

    def publications(
        self,
        *,
        scope: ObservationScope | None = None,
        role: ArtifactRole | None = None,
    ) -> tuple[ArtifactPublication, ...]:
        output: list[ArtifactPublication] = []
        for item in self._by_digest.values():
            if scope is not None and item.lineage.scope != scope:
                continue
            if role is not None and item.lineage.role != role:
                continue
            output.append(item)
        return tuple(output)

    def projection(
        self,
        *,
        scope: ObservationScope | None = None,
    ) -> dict[str, Any]:
        values = self.publications(scope=scope)
        return {
            "schema": "zyra.browser-observability.artifacts.v1",
            "publication_count": len(values),
            "screenshot_count": sum(
                1 for item in values if item.lineage.role == ArtifactRole.SCREENSHOT
            ),
            "download_count": sum(
                1 for item in values if item.lineage.role == ArtifactRole.DOWNLOAD
            ),
            "trace_count": sum(
                1 for item in values if item.lineage.role == ArtifactRole.TRACE
            ),
            "history_manifest_count": sum(
                1
                for item in values
                if item.lineage.role == ArtifactRole.HISTORY_SEGMENT
            ),
            "artifacts": [
                {
                    "artifact": {
                        "artifact_id": item.artifact.artifact_id,
                        "kind": str(item.artifact.kind),
                        "uri": item.artifact.uri,
                        "title": item.artifact.title,
                    },
                    "lineage": item.lineage.to_dict(),
                    "event_id": item.event.event_id,
                    "idempotent": item.idempotent,
                }
                for item in values
            ],
        }

    def _read_artifact(
        self,
        artifact: ArtifactRef,
    ) -> tuple[bytes, str]:
        try:
            path = self.artifact_store.resolve_path(artifact)
        except (TypeError, AttributeError):
            path = self.artifact_store.resolve_path(artifact.artifact_id)
        if path is None or not path.is_file():
            raise BrowserArtifactPublicationError(
                f"canonical artifact {artifact.artifact_id!r} is missing"
            )
        payload = path.read_bytes()
        media_type = str(artifact.metadata.get("content_type") or "")
        if not media_type:
            media_type = _media_type(path)
        if self.policy.verify_readback:
            second = path.read_bytes()
            if hashlib.sha256(second).digest() != hashlib.sha256(payload).digest():
                raise BrowserArtifactPublicationError(
                    f"canonical artifact {artifact.artifact_id!r} changed during readback"
                )
        return payload, media_type

    def _write_text(
        self,
        content: str,
        *,
        run_id: str,
        task_id: str,
        kind: ArtifactKind,
        title: str,
        producer_node_id: str | None,
        suffix: str,
        metadata: Mapping[str, Any],
    ) -> ArtifactRef:
        artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=content,
            kind=kind,
            title=title,
            producer_node_id=producer_node_id,
        )
        artifact.metadata.update(
            {
                **dict(metadata),
                "requested_suffix": suffix,
            }
        )
        return artifact

    @staticmethod
    def _event(
        scope: ObservationScope,
        artifact: ArtifactRef,
        lineage: ArtifactLineage,
    ) -> EventRecord:
        return EventRecord(
            run_id=scope.run_id,
            task_id=scope.task_id,
            node_id=scope.node_id or None,
            event_type=EventType.ARTIFACT_WRITTEN,
            payload={
                "browser_artifact": {
                    "schema": "zyra.browser-observability.artifact-event.v1",
                    "artifact_id": artifact.artifact_id,
                    "role": str(lineage.role),
                    "lineage_receipt_id": lineage.receipt_id,
                    "sha256": lineage.sha256,
                    "size_bytes": lineage.size_bytes,
                    "source_event_ids": list(lineage.source_event_ids),
                    "source_record_ids": list(lineage.source_record_ids),
                    "quarantined": lineage.quarantined,
                }
            },
        )


def infer_artifact_role(
    artifact: ArtifactRef,
) -> ArtifactRole:
    kind = str(artifact.kind)
    title = artifact.title.casefold()
    metadata = {
        str(key).casefold(): str(value).casefold()
        for key, value in artifact.metadata.items()
    }
    if kind == str(ArtifactKind.SCREENSHOT) or "screenshot" in title:
        return ArtifactRole.SCREENSHOT
    if "download" in title or metadata.get("artifact_role") == "download":
        return ArtifactRole.DOWNLOAD
    if kind == str(ArtifactKind.TRACE) or "trace" in title:
        return ArtifactRole.TRACE
    if "dom" in title or metadata.get("artifact_role") == "dom_snapshot":
        return ArtifactRole.DOM_SNAPSHOT
    if "storage" in title:
        return ArtifactRole.STORAGE_STATE
    return ArtifactRole.DIAGNOSTIC


def _media_type(
    path: Path,
) -> str:
    suffix = path.suffix.casefold()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".json": "application/json",
        ".jsonl": "application/x-ndjson",
        ".html": "text/html",
        ".txt": "text/plain",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
