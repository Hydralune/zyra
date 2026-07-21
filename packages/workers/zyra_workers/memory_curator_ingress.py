from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from zyra_core import now_iso
from zyra_memory.curator_integration_models import (
    CuratorInputBatch,
    CuratorInputSource,
    RuntimeTraceKind,
    RuntimeTraceRef,
)
from zyra_memory.curator_integration_store import CuratorIntegrationStore
from zyra_memory.curator_models import canonical_json, stable_digest, unique_strings
from zyra_runtime import RuntimeEventQuery, RuntimeEventSpineBridge


class CuratorRuntimeEventIngressError(RuntimeError):
    pass


class CuratorRuntimeEventGapError(CuratorRuntimeEventIngressError):
    pass


class CuratorRuntimeEventDigestError(CuratorRuntimeEventIngressError):
    pass


class CanonicalTaskStore(Protocol):
    def task_events(self, task_id: str) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class CuratorIngressSnapshot:
    batch: CuratorInputBatch
    extraction_events: tuple[Mapping[str, Any], ...]
    runtime_event_ids: tuple[str, ...]
    legacy_event_ids: tuple[str, ...]
    excluded_event_ids: tuple[str, ...]
    source_mode: str
    direct_spine: bool
    fallback_used: bool
    diagnostics: Mapping[str, Any] = field(default_factory=dict)

    def validated(self) -> CuratorIngressSnapshot:
        self.batch.validated()
        if len(self.extraction_events) != len(self.runtime_event_ids):
            raise CuratorRuntimeEventIngressError(
                "each extraction event must retain one canonical runtime event id"
            )
        if len(set(self.runtime_event_ids)) != len(self.runtime_event_ids):
            raise CuratorRuntimeEventIngressError(
                "canonical runtime event identities must be unique"
            )
        if self.direct_spine and self.fallback_used:
            raise CuratorRuntimeEventIngressError(
                "direct spine snapshot cannot also claim legacy fallback"
            )
        if not self.direct_spine and not self.fallback_used:
            raise CuratorRuntimeEventIngressError(
                "non-spine snapshot must disclose fallback use"
            )
        return self

    @property
    def input_watermark(self) -> int:
        return len(self.extraction_events)

    def to_dict(self, *, include_events: bool = False) -> dict[str, Any]:
        output = {
            "batch": self.batch.to_dict(),
            "runtime_event_ids": list(self.runtime_event_ids),
            "legacy_event_ids": list(self.legacy_event_ids),
            "excluded_event_ids": list(self.excluded_event_ids),
            "source_mode": self.source_mode,
            "direct_spine": self.direct_spine,
            "fallback_used": self.fallback_used,
            "input_watermark": self.input_watermark,
            "diagnostics": dict(self.diagnostics),
        }
        if include_events:
            output["extraction_events"] = [dict(item) for item in self.extraction_events]
        return output


class RuntimeEventCuratorIngress:
    """Read task evidence from the canonical 05C runtime-event spine.

    The adapter preserves the spine event ID, global sequence, causation,
    correlation, artifact refs and content digest while projecting the shape
    already consumed by ``EventArtifactTraceExtractor``.  It does not rebuild
    session state and it never writes memory.  The legacy SQLite event list is
    inspected only to prove that the compatibility projection agrees with the
    canonical source or, when explicitly configured, to provide a degraded
    read-only fallback.
    """

    OPERATIONAL_TYPES = {
        "memory_curator_scheduled",
        "memory_curator_committed",
        "memory_curator_rejected",
        "memory_curator_recovered",
        "memory_curator_candidate",
        "memory_curator_accepted",
        "memory_curator_index_published",
    }

    TOOL_TYPES = {
        "tool_called",
        "tool_succeeded",
        "tool_failed",
        "mcp_tool_result",
        "command_succeeded",
        "command_failed",
    }

    FAILURE_TYPES = {
        "tool_failed",
        "turn_failed",
        "subagent_failed",
        "command_failed",
        "recovery_requested",
        "worker_unhealthy",
        "fault_injected",
    }

    RECOVERY_TYPES = {
        "recovery_requested",
        "recovery_started",
        "recovery_completed",
        "recovery_planned",
        "compact_restore",
    }

    COMPACT_TYPES = {
        "compact_started",
        "compact_completed",
        "compact_restore",
        "context_compacted",
        "context_restored",
    }

    SESSION_TYPES = {
        "query_admitted",
        "turn_started",
        "turn_completed",
        "turn_failed",
        "session_started",
        "session_ended",
        "subagent_started",
        "subagent_completed",
        "subagent_failed",
    }

    CONTROL_TYPES = {
        "control_requested",
        "control_accepted",
        "control_rejected",
        "control_completed",
        "permission_requested",
        "permission_resolved",
    }

    def __init__(
        self,
        *,
        bridge: RuntimeEventSpineBridge,
        canonical_store: CanonicalTaskStore,
        integration_store: CuratorIntegrationStore,
        page_size: int = 500,
        maximum_events: int = 100_000,
        allow_legacy_fallback: bool = False,
        verify_content_digests: bool = True,
    ) -> None:
        self.bridge = bridge
        self.canonical_store = canonical_store
        self.integration_store = integration_store
        self.page_size = max(1, min(int(page_size), 1000))
        self.maximum_events = max(self.page_size, min(int(maximum_events), 1_000_000))
        self.allow_legacy_fallback = bool(allow_legacy_fallback)
        self.verify_content_digests = bool(verify_content_digests)

    def current_watermark(self, *, run_id: str, task_id: str) -> int:
        snapshot = self.preview(run_id=run_id, task_id=task_id)
        return snapshot.input_watermark

    def preview(self, *, run_id: str, task_id: str) -> CuratorIngressSnapshot:
        return self._read(
            run_id=run_id,
            task_id=task_id,
            requested_watermark=None,
            record=False,
        )

    def prepare(
        self,
        *,
        run_id: str,
        task_id: str,
        input_watermark: int,
    ) -> CuratorIngressSnapshot:
        return self._read(
            run_id=run_id,
            task_id=task_id,
            requested_watermark=max(0, int(input_watermark)),
            record=True,
        )

    def replay_batch(self, batch_id: str) -> CuratorIngressSnapshot:
        batch = self.integration_store.input_batch(batch_id)
        if batch is None:
            raise KeyError(batch_id)
        events: list[Mapping[str, Any]] = []
        runtime_ids: list[str] = []
        for ref in batch.refs:
            if ref.source is not CuratorInputSource.RUNTIME_EVENT_SPINE:
                continue
            event = self._find_runtime_event(
                task_id=batch.task_id,
                event_id=ref.source_id,
                expected_digest=ref.content_digest,
            )
            events.append(self._to_extraction_event(event, run_id=batch.run_id, task_id=batch.task_id))
            runtime_ids.append(ref.source_id)
        if len(events) != batch.runtime_event_count:
            raise CuratorRuntimeEventGapError(
                "runtime event replay no longer covers the persisted input batch"
            )
        legacy_ids = self._legacy_event_ids(batch.task_id)
        return CuratorIngressSnapshot(
            batch=batch,
            extraction_events=tuple(events),
            runtime_event_ids=tuple(runtime_ids),
            legacy_event_ids=legacy_ids,
            excluded_event_ids=(),
            source_mode="runtime_event_spine_replay",
            direct_spine=True,
            fallback_used=False,
            diagnostics={
                "replayed": True,
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "input_owner": "RuntimeEventSpineBridge",
            },
        ).validated()

    def _read(
        self,
        *,
        run_id: str,
        task_id: str,
        requested_watermark: int | None,
        record: bool,
    ) -> CuratorIngressSnapshot:
        if not str(run_id).strip() or not str(task_id).strip():
            raise CuratorRuntimeEventIngressError("runtime ingress requires run/task identity")
        legacy_events = tuple(self.canonical_store.task_events(task_id))
        legacy_ids = self._legacy_event_ids_from_events(legacy_events)
        try:
            envelopes, high_watermark, pages = self._query_all(task_id)
        except BaseException as error:
            if not self.allow_legacy_fallback:
                raise CuratorRuntimeEventIngressError(
                    f"canonical runtime event spine is unavailable: {type(error).__name__}: {error}"
                ) from error
            return self._legacy_snapshot(
                run_id=run_id,
                task_id=task_id,
                events=legacy_events,
                requested_watermark=requested_watermark,
                cause=error,
                record=record,
            )
        admissible: list[Any] = []
        excluded: list[str] = []
        for envelope in envelopes:
            if envelope.event_type in self.OPERATIONAL_TYPES:
                excluded.append(envelope.event_id)
                continue
            identity = self._identity(envelope)
            event_run_id = str(identity.get("runId") or identity.get("run_id") or run_id)
            event_task_id = str(identity.get("taskId") or identity.get("task_id") or task_id)
            if event_task_id != task_id:
                excluded.append(envelope.event_id)
                continue
            if event_run_id and event_run_id != run_id:
                excluded.append(envelope.event_id)
                continue
            if self.verify_content_digests:
                self._verify_envelope_digest(envelope)
            admissible.append(envelope)
        runtime_ids_all = {item.event_id for item in admissible}
        legacy_admissible = tuple(
            event
            for event in legacy_events
            if self._legacy_event_type(event) not in self.OPERATIONAL_TYPES
        )
        compatibility_only = tuple(
            event
            for event in legacy_admissible
            if str(event.get("event_id") or "") not in runtime_ids_all
        )
        combined: list[tuple[str, Any]] = [
            ("runtime", item) for item in admissible
        ]
        combined.extend(("legacy", item) for item in compatibility_only)
        high_count = len(combined)
        selected_count = high_count if requested_watermark is None else requested_watermark
        if selected_count > high_count:
            raise CuratorRuntimeEventGapError(
                f"scheduled runtime event watermark {selected_count} exceeds available {high_count}"
            )
        selected = tuple(combined[:selected_count])
        extraction_events = tuple(
            self._to_extraction_event(item, run_id=run_id, task_id=task_id)
            if source == "runtime"
            else dict(item)
            for source, item in selected
        )
        refs = tuple(
            self._trace_ref(item, run_id=run_id, task_id=task_id)
            if source == "runtime"
            else self._legacy_trace_ref(
                item,
                run_id=run_id,
                task_id=task_id,
                sequence=index,
            )
            for index, (source, item) in enumerate(selected)
        )
        source_cursor = self.integration_store.input_cursor(
            task_id,
            CuratorInputSource.RUNTIME_EVENT_SPINE,
        )
        source_watermark = int(source_cursor["watermark"])
        if source_watermark > selected_count:
            source_watermark = selected_count
        incremental_refs = refs[source_watermark:selected_count]
        batch = CuratorInputBatch.build(
            run_id=run_id,
            task_id=task_id,
            refs=incremental_refs,
            source_watermark=source_watermark,
            next_watermark=selected_count,
            high_watermark=high_count,
            has_more=selected_count < high_count,
            legacy_event_count=len(legacy_events),
            runtime_event_count=sum(
                1
                for ref in incremental_refs
                if ref.source is CuratorInputSource.RUNTIME_EVENT_SPINE
            ),
            artifact_count=len(
                {
                    artifact_id
                    for ref in incremental_refs
                    for artifact_id in ref.artifact_ids
                }
            ),
            duplicate_source_ids=self._duplicates(
                item.event_id
                if source == "runtime"
                else str(item.get("event_id") or "")
                for source, item in selected
            ),
            warnings=self._projection_warnings(
                runtime_ids=tuple(
                    item.event_id for source, item in selected if source == "runtime"
                ),
                legacy_ids=legacy_ids,
            ),
            metadata={
                "input_owner": "RuntimeEventSpineBridge",
                "compatibility_projection_owner": type(self.canonical_store).__name__,
                "direct_spine": True,
                "legacy_fallback_used": False,
                "page_count": pages,
                "global_high_watermark": high_watermark,
                "scheduled_input_watermark": selected_count,
                "incremental_start": source_watermark,
                "incremental_count": len(incremental_refs),
                "compatibility_only_event_count": len(compatibility_only),
                "canonical_memory_owner": "SQLiteStore.memory_records",
                "model_can_write": False,
            },
        )
        inserted = False
        if record:
            batch, inserted = self.integration_store.record_input_batch(
                batch,
                expected_cursor_version=int(source_cursor["version"]),
            )
        return CuratorIngressSnapshot(
            batch=batch,
            extraction_events=extraction_events,
            runtime_event_ids=tuple(
                item.event_id
                if source == "runtime"
                else str(item.get("event_id") or f"legacy:{index}")
                for index, (source, item) in enumerate(selected)
            ),
            legacy_event_ids=legacy_ids,
            excluded_event_ids=tuple(excluded),
            source_mode="runtime_event_spine",
            direct_spine=True,
            fallback_used=False,
            diagnostics={
                "page_count": pages,
                "queried_event_count": len(envelopes),
                "admissible_event_count": high_count,
                "canonical_runtime_event_count": len(admissible),
                "compatibility_only_event_count": len(compatibility_only),
                "selected_event_count": selected_count,
                "incremental_ref_count": len(incremental_refs),
                "excluded_event_count": len(excluded),
                "global_high_watermark": high_watermark,
                "batch_recorded": record,
                "batch_inserted": inserted,
                "digest_verification": self.verify_content_digests,
                "canonical_input_owner": "RuntimeEventSpineBridge",
                "legacy_projection_used_for_extraction": bool(compatibility_only),
            },
        ).validated()

    def _query_all(self, task_id: str) -> tuple[tuple[Any, ...], int, int]:
        events: list[Any] = []
        cursor = 0
        high_watermark = 0
        page_count = 0
        seen_ids: set[str] = set()
        while True:
            page = self.bridge.query(
                RuntimeEventQuery(
                    task_id=task_id,
                    after_sequence=cursor,
                    limit=self.page_size,
                )
            )
            page_count += 1
            if page.next_sequence < cursor:
                raise CuratorRuntimeEventGapError("runtime event query cursor regressed")
            for event in page.events:
                if event.event_id in seen_ids:
                    continue
                if event.global_sequence <= cursor:
                    raise CuratorRuntimeEventGapError(
                        "runtime event page returned a sequence behind its cursor"
                    )
                seen_ids.add(event.event_id)
                events.append(event)
                if len(events) > self.maximum_events:
                    raise CuratorRuntimeEventIngressError(
                        f"runtime event input exceeds maximum {self.maximum_events}"
                    )
            high_watermark = max(high_watermark, int(page.high_watermark))
            if not page.has_more:
                break
            if page.next_sequence <= cursor:
                raise CuratorRuntimeEventGapError(
                    "runtime event query reported more pages without advancing"
                )
            cursor = page.next_sequence
        events.sort(key=lambda item: (item.global_sequence, item.event_id))
        return tuple(events), high_watermark, page_count

    def _legacy_snapshot(
        self,
        *,
        run_id: str,
        task_id: str,
        events: Sequence[Mapping[str, Any]],
        requested_watermark: int | None,
        cause: BaseException,
        record: bool,
    ) -> CuratorIngressSnapshot:
        operational = [
            event
            for event in events
            if self._legacy_event_type(event) not in self.OPERATIONAL_TYPES
        ]
        high_count = len(operational)
        selected_count = high_count if requested_watermark is None else requested_watermark
        if selected_count > high_count:
            raise CuratorRuntimeEventGapError(
                f"legacy fallback watermark {selected_count} exceeds available {high_count}"
            )
        selected = tuple(operational[:selected_count])
        source_cursor = self.integration_store.input_cursor(
            task_id,
            CuratorInputSource.LEGACY_EVENT_PROJECTION,
        )
        start = min(int(source_cursor["watermark"]), selected_count)
        refs = tuple(
            self._legacy_trace_ref(event, run_id=run_id, task_id=task_id, sequence=index)
            for index, event in enumerate(selected[start:], start=start)
        )
        batch = CuratorInputBatch.build(
            run_id=run_id,
            task_id=task_id,
            refs=refs,
            source_watermark=start,
            next_watermark=selected_count,
            high_watermark=high_count,
            has_more=selected_count < high_count,
            legacy_event_count=len(refs),
            runtime_event_count=0,
            artifact_count=len(
                {
                    artifact_id
                    for ref in refs
                    for artifact_id in ref.artifact_ids
                }
            ),
            warnings=("runtime_event_spine_unavailable", "legacy_projection_fallback"),
            metadata={
                "input_owner": type(self.canonical_store).__name__,
                "direct_spine": False,
                "legacy_fallback_used": True,
                "fallback_error": type(cause).__name__,
                "fallback_message": str(cause)[:500],
                "canonical_memory_owner": "SQLiteStore.memory_records",
            },
        )
        inserted = False
        if record:
            batch, inserted = self.integration_store.record_input_batch(
                batch,
                cursor_source=CuratorInputSource.LEGACY_EVENT_PROJECTION,
                expected_cursor_version=int(source_cursor["version"]),
            )
        ids = self._legacy_event_ids_from_events(selected)
        return CuratorIngressSnapshot(
            batch=batch,
            extraction_events=selected,
            runtime_event_ids=ids,
            legacy_event_ids=ids,
            excluded_event_ids=(),
            source_mode="legacy_event_projection_fallback",
            direct_spine=False,
            fallback_used=True,
            diagnostics={
                "fallback_error": type(cause).__name__,
                "fallback_message": str(cause)[:500],
                "selected_event_count": selected_count,
                "incremental_ref_count": len(refs),
                "batch_recorded": record,
                "batch_inserted": inserted,
            },
        ).validated()

    def _trace_ref(self, envelope: Any, *, run_id: str, task_id: str) -> RuntimeTraceRef:
        identity = self._identity(envelope)
        artifacts = tuple(ref.artifact_id for ref in envelope.artifact_refs)
        return RuntimeTraceRef.build(
            run_id=run_id,
            task_id=task_id,
            source=CuratorInputSource.RUNTIME_EVENT_SPINE,
            kind=self._kind(envelope.event_type, envelope.payload),
            source_id=envelope.event_id,
            source_sequence=envelope.global_sequence,
            content_digest=envelope.content_digest,
            occurred_at=envelope.occurred_at,
            event_type=envelope.event_type,
            aggregate_id=envelope.aggregate_id,
            causation_id=envelope.causation_id or "",
            correlation_id=envelope.correlation_id,
            producer=envelope.producer,
            summary=envelope.summary,
            artifact_ids=artifacts,
            trusted_runtime=envelope.trust in {"internal", "system", "verified_runtime"},
            canonical=True,
            metadata={
                "aggregate_type": envelope.aggregate_type,
                "aggregate_sequence": envelope.aggregate_sequence,
                "global_sequence": envelope.global_sequence,
                "subject": envelope.subject,
                "intent": envelope.intent,
                "schema_version": envelope.schema_version,
                "envelope_bytes": envelope.envelope_bytes,
                "identity": identity,
                "source_contract": "RuntimeEventEnvelope",
            },
        )

    def _legacy_trace_ref(
        self,
        event: Mapping[str, Any],
        *,
        run_id: str,
        task_id: str,
        sequence: int,
    ) -> RuntimeTraceRef:
        event_id = str(event.get("event_id") or f"legacy:{task_id}:{sequence}")
        event_type = self._legacy_event_type(event)
        payload = self._payload(event)
        digest = stable_digest(
            {
                "event_id": event_id,
                "event_type": event_type,
                "run_id": run_id,
                "task_id": task_id,
                "payload": payload,
            }
        )
        return RuntimeTraceRef.build(
            run_id=run_id,
            task_id=task_id,
            source=CuratorInputSource.LEGACY_EVENT_PROJECTION,
            kind=self._kind(event_type, payload),
            source_id=event_id,
            source_sequence=sequence,
            content_digest=digest,
            occurred_at=str(event.get("created_at") or now_iso()),
            event_type=event_type,
            causation_id=str(payload.get("causation_id") or ""),
            correlation_id=str(payload.get("correlation_id") or ""),
            producer=str(payload.get("producer") or payload.get("worker") or ""),
            summary=str(payload.get("summary") or ""),
            artifact_ids=self._artifact_ids(payload),
            trusted_runtime=True,
            canonical=False,
            metadata={
                "source_contract": "EventRecord compatibility projection",
                "fallback_only": True,
            },
        )

    def _to_extraction_event(
        self,
        envelope: Any,
        *,
        run_id: str,
        task_id: str,
    ) -> Mapping[str, Any]:
        identity = self._identity(envelope)
        payload = dict(envelope.payload)
        artifact_ids = tuple(ref.artifact_id for ref in envelope.artifact_refs)
        existing_artifacts = self._artifact_ids(payload)
        if artifact_ids or existing_artifacts:
            payload["artifact_ids"] = list(unique_strings((*existing_artifacts, *artifact_ids)))
        payload["runtime_event"] = {
            "event_id": envelope.event_id,
            "event_type": envelope.event_type,
            "aggregate_type": envelope.aggregate_type,
            "aggregate_id": envelope.aggregate_id,
            "aggregate_sequence": envelope.aggregate_sequence,
            "global_sequence": envelope.global_sequence,
            "causation_id": envelope.causation_id,
            "correlation_id": envelope.correlation_id,
            "producer": envelope.producer,
            "subject": envelope.subject,
            "trust": envelope.trust,
            "intent": envelope.intent,
            "content_digest": envelope.content_digest,
            "schema_version": envelope.schema_version,
        }
        payload["trust"] = (
            "verified_runtime"
            if envelope.trust in {"internal", "system", "verified_runtime"}
            else envelope.trust
        )
        payload.setdefault("producer", envelope.producer)
        payload.setdefault("source", "runtime_event_spine")
        payload.setdefault("causation_id", envelope.causation_id or "")
        payload.setdefault("correlation_id", envelope.correlation_id)
        payload.setdefault("summary", envelope.summary)
        node_id = str(
            identity.get("nodeId")
            or identity.get("node_id")
            or payload.get("node_id")
            or ""
        )
        return {
            "event_id": envelope.event_id,
            "run_id": run_id,
            "task_id": task_id,
            "node_id": node_id,
            "event_type": envelope.event_type,
            "created_at": envelope.occurred_at,
            "payload": payload,
            "canonical_runtime_event": True,
            "global_sequence": envelope.global_sequence,
            "content_digest": envelope.content_digest,
        }

    def _find_runtime_event(
        self,
        *,
        task_id: str,
        event_id: str,
        expected_digest: str,
    ) -> Any:
        envelopes, _high, _pages = self._query_all(task_id)
        for event in envelopes:
            if event.event_id != event_id:
                continue
            if event.content_digest.removeprefix("sha256:") != expected_digest.removeprefix("sha256:"):
                raise CuratorRuntimeEventDigestError(
                    f"runtime event {event_id} digest changed during replay"
                )
            return event
        raise CuratorRuntimeEventGapError(f"runtime event disappeared during replay: {event_id}")

    @staticmethod
    def _identity(envelope: Any) -> Mapping[str, Any]:
        canonical = envelope.canonical
        if isinstance(canonical, Mapping):
            value = canonical.get("identity")
            if isinstance(value, Mapping):
                return dict(value)
        payload = envelope.payload
        value = payload.get("identity") if isinstance(payload, Mapping) else None
        return dict(value) if isinstance(value, Mapping) else {}

    def _verify_envelope_digest(self, envelope: Any) -> None:
        content_digest = str(envelope.content_digest).removeprefix("sha256:")
        if len(content_digest) != 64:
            raise CuratorRuntimeEventDigestError(
                f"runtime event {envelope.event_id} has an invalid content digest"
            )
        try:
            int(content_digest, 16)
        except ValueError as error:
            raise CuratorRuntimeEventDigestError(
                f"runtime event {envelope.event_id} content digest is not hexadecimal"
            ) from error
        canonical = envelope.canonical
        if not isinstance(canonical, Mapping):
            return
        declared = str(canonical.get("contentDigest") or "").removeprefix("sha256:")
        if declared and declared != content_digest:
            raise CuratorRuntimeEventDigestError(
                f"runtime event {envelope.event_id} digest fields disagree"
            )

    def _kind(self, event_type: str, payload: Mapping[str, Any]) -> RuntimeTraceKind:
        normalized = str(event_type).casefold()
        if normalized in self.FAILURE_TYPES or payload.get("error"):
            return RuntimeTraceKind.FAILURE
        if normalized in self.RECOVERY_TYPES:
            return RuntimeTraceKind.RECOVERY
        if normalized in self.COMPACT_TYPES or "compact" in normalized:
            return RuntimeTraceKind.COMPACT
        if normalized in self.CONTROL_TYPES or "permission" in normalized:
            return RuntimeTraceKind.CONTROL
        if normalized in self.TOOL_TYPES or any(
            key in payload
            for key in ("tool_name", "tool_call_id", "tool_result", "command")
        ):
            return RuntimeTraceKind.TOOL_RESULT
        if normalized.startswith("browser_") or any(
            key in payload
            for key in ("browser_action", "browser_session_id", "selector", "url")
        ):
            return RuntimeTraceKind.BROWSER_TRACE
        if any(
            key in payload
            for key in ("patch", "diff", "file_path", "files_changed", "test_result")
        ):
            return RuntimeTraceKind.CODE_TRACE
        if normalized in self.SESSION_TYPES:
            return RuntimeTraceKind.SESSION
        if self._artifact_ids(payload):
            return RuntimeTraceKind.ARTIFACT
        return RuntimeTraceKind.EVENT

    @staticmethod
    def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
        value = event.get("payload")
        return dict(value) if isinstance(value, Mapping) else {}

    @staticmethod
    def _legacy_event_type(event: Mapping[str, Any]) -> str:
        value = event.get("event_type")
        if hasattr(value, "value"):
            value = value.value
        return str(value or "unknown")

    @staticmethod
    def _artifact_ids(payload: Mapping[str, Any]) -> tuple[str, ...]:
        values: list[object] = []
        for key in (
            "artifact_id",
            "screenshot_artifact_id",
            "trace_artifact_id",
            "diff_artifact_id",
        ):
            if payload.get(key):
                values.append(payload[key])
        for key in ("artifact_ids", "artifacts", "evidence_artifact_ids", "artifactRefs"):
            raw = payload.get(key)
            if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
                continue
            for item in raw:
                if isinstance(item, Mapping):
                    values.append(
                        item.get("artifact_id")
                        or item.get("artifactId")
                        or item.get("id")
                        or ""
                    )
                else:
                    values.append(item)
        return unique_strings(values)

    def _legacy_event_ids(self, task_id: str) -> tuple[str, ...]:
        return self._legacy_event_ids_from_events(self.canonical_store.task_events(task_id))

    @staticmethod
    def _legacy_event_ids_from_events(
        events: Sequence[Mapping[str, Any]],
    ) -> tuple[str, ...]:
        return unique_strings(
            event.get("event_id")
            for event in events
            if event.get("event_id")
        )

    @staticmethod
    def _duplicates(values: Sequence[str] | Any) -> tuple[str, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for value in values:
            text = str(value)
            if text in seen and text not in duplicates:
                duplicates.append(text)
            seen.add(text)
        return tuple(duplicates)

    @staticmethod
    def _projection_warnings(
        *,
        runtime_ids: Sequence[str],
        legacy_ids: Sequence[str],
    ) -> tuple[str, ...]:
        runtime = set(runtime_ids)
        legacy = set(legacy_ids)
        warnings: list[str] = []
        if legacy - runtime:
            warnings.append("legacy_projection_contains_non_selected_events")
        if runtime - legacy:
            warnings.append("runtime_spine_contains_non_legacy_events")
        return tuple(warnings)


__all__ = [
    "CanonicalTaskStore",
    "CuratorIngressSnapshot",
    "CuratorRuntimeEventDigestError",
    "CuratorRuntimeEventGapError",
    "CuratorRuntimeEventIngressError",
    "RuntimeEventCuratorIngress",
]
