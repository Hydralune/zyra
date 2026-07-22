from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from .contracts import Finding, GateResult, GateStatus, Severity, TransitionObservation


_EXCLUDED_EVENT_TYPES = {
    "heartbeat",
    "worker_heartbeat",
    "keepalive",
    "keep_alive",
    "log",
    "debug_log",
    "ui_repaint",
    "projector_replay",
    "projector_duplicate",
    "fixture_replay",
    "retry_wait",
    "stream_keepalive",
}
_SEMANTIC_EVENT_PREFIXES = {
    "tool",
    "permission",
    "memory",
    "compact",
    "restore",
    "route",
    "topology",
    "worker",
    "lease",
    "backend",
    "provider",
    "artifact",
    "checkpoint",
    "recovery",
    "fault",
    "control",
    "command",
    "session",
    "task",
    "node",
    "mcp",
    "skill",
    "subagent",
    "browser",
    "workspace",
    "gateway",
    "event",
}
_REVISION_KEYS = (
    "after_revision",
    "revision_after",
    "new_revision",
    "committed_revision",
    "revision",
    "version",
    "sequence",
)
_BEFORE_REVISION_KEYS = (
    "before_revision",
    "revision_before",
    "expected_revision",
    "previous_revision",
    "base_revision",
)
_CAUSATION_KEYS = (
    "causation_id",
    "cause_event_id",
    "parent_event_id",
    "request_id",
    "command_id",
    "tool_use_id",
    "attempt_id",
    "lease_id",
)


@dataclass(frozen=True, slots=True)
class RejectedTransition:
    index: int
    event_type: str
    reason: str
    event_id: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "event_type": self.event_type,
            "reason": self.reason,
            "event_id": self.event_id,
            "detail": self.detail,
        }


@dataclass(slots=True)
class ProgressLedgerSnapshot:
    run_id: str
    transitions: list[TransitionObservation] = field(default_factory=list)
    rejected: list[RejectedTransition] = field(default_factory=list)
    action_ids: set[str] = field(default_factory=set)
    semantic_families: Counter[str] = field(default_factory=Counter)
    event_types: Counter[str] = field(default_factory=Counter)

    @property
    def effective_transition_count(self) -> int:
        return len(self.transitions)

    @property
    def effective_action_count(self) -> int:
        return len(self.action_ids)

    def to_dict(self, *, include_transitions: bool = True) -> dict[str, Any]:
        payload = {
            "run_id": self.run_id,
            "effective_transition_count": self.effective_transition_count,
            "effective_action_count": self.effective_action_count,
            "rejected_count": len(self.rejected),
            "semantic_families": dict(sorted(self.semantic_families.items())),
            "event_types": dict(sorted(self.event_types.items())),
            "rejected": [item.to_dict() for item in self.rejected],
        }
        if include_transitions:
            payload["transitions"] = [item.to_dict() for item in self.transitions]
        return payload


class TransitionExtractor:
    def __init__(self, *, allow_task_created_root: bool = True) -> None:
        self.allow_task_created_root = allow_task_created_root

    def extract(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        expected_run_id: str = "",
    ) -> ProgressLedgerSnapshot:
        run_id = expected_run_id or self._first_run_id(events)
        snapshot = ProgressLedgerSnapshot(run_id=run_id)
        seen_transition_ids: set[str] = set()
        seen_event_ids: set[str] = set()
        last_revision_by_key: dict[tuple[str, str], int | str] = {}
        for index, event in enumerate(events):
            accepted, rejected = self._extract_one(event, index=index, expected_run_id=run_id)
            if rejected is not None:
                snapshot.rejected.append(rejected)
                continue
            assert accepted is not None
            if accepted.transition_id in seen_transition_ids:
                snapshot.rejected.append(
                    RejectedTransition(
                        index=index,
                        event_type=accepted.event_type,
                        event_id=accepted.transition_id,
                        reason="duplicate_transition_id",
                    )
                )
                continue
            raw_event_id = str(event.get("event_id") or "")
            if raw_event_id and raw_event_id in seen_event_ids:
                snapshot.rejected.append(
                    RejectedTransition(
                        index=index,
                        event_type=accepted.event_type,
                        event_id=raw_event_id,
                        reason="duplicate_event_id",
                    )
                )
                continue
            monotonic_error = self._monotonic_error(accepted, last_revision_by_key)
            if monotonic_error:
                snapshot.rejected.append(
                    RejectedTransition(
                        index=index,
                        event_type=accepted.event_type,
                        event_id=accepted.transition_id,
                        reason="revision_not_monotonic",
                        detail=monotonic_error,
                    )
                )
                continue
            seen_transition_ids.add(accepted.transition_id)
            if raw_event_id:
                seen_event_ids.add(raw_event_id)
            key = (accepted.semantic_family, accepted.semantic_key)
            if accepted.after_revision is not None:
                last_revision_by_key[key] = accepted.after_revision
            snapshot.transitions.append(accepted)
            snapshot.event_types[accepted.event_type] += 1
            snapshot.semantic_families[accepted.semantic_family] += 1
            if accepted.action_id:
                snapshot.action_ids.add(accepted.action_id)
        return snapshot

    def _extract_one(
        self,
        event: Mapping[str, Any],
        *,
        index: int,
        expected_run_id: str,
    ) -> tuple[TransitionObservation | None, RejectedTransition | None]:
        event_type = str(event.get("event_type") or event.get("type") or "").strip()
        event_id = str(event.get("event_id") or event.get("id") or "").strip()
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        metadata = event.get("metadata") if isinstance(event.get("metadata"), Mapping) else {}
        run_id = str(event.get("run_id") or payload.get("run_id") or metadata.get("run_id") or "")
        task_id = str(event.get("task_id") or payload.get("task_id") or metadata.get("task_id") or "")
        if not event_type:
            return None, RejectedTransition(index, "", "event_type_missing", event_id)
        normalized_type = event_type.lower().replace("-", "_")
        if normalized_type in _EXCLUDED_EVENT_TYPES or self._is_noop(event, payload, metadata):
            return None, RejectedTransition(index, event_type, "non_effective_event", event_id)
        if expected_run_id and run_id and run_id != expected_run_id:
            return None, RejectedTransition(index, event_type, "run_id_mismatch", event_id, run_id)
        semantic_family = self._semantic_family(event_type, payload, metadata)
        if not semantic_family:
            return None, RejectedTransition(index, event_type, "semantic_family_unrecognized", event_id)
        before_revision = self._first_value(event, payload, metadata, keys=_BEFORE_REVISION_KEYS)
        after_revision = self._first_value(event, payload, metadata, keys=_REVISION_KEYS)
        before_state, after_state = self._states(event, payload, metadata)
        before_digest = self._digest(before_state)
        after_digest = self._digest(after_state)
        if normalized_type == "task_created" and self.allow_task_created_root:
            before_revision = 0 if before_revision is None else before_revision
            after_revision = 1 if after_revision is None else after_revision
            before_digest = before_digest or self._digest(None)
            after_digest = after_digest or self._digest(payload or event)
        if before_revision is None or after_revision is None:
            return None, RejectedTransition(index, event_type, "revision_pair_missing", event_id)
        if before_digest == after_digest:
            return None, RejectedTransition(index, event_type, "semantic_state_unchanged", event_id)
        causation_id = str(self._first_value(event, payload, metadata, keys=_CAUSATION_KEYS) or "")
        if not causation_id and not (normalized_type == "task_created" and self.allow_task_created_root):
            return None, RejectedTransition(index, event_type, "causation_missing", event_id)
        semantic_key = str(
            payload.get("semantic_key")
            or payload.get("state_key")
            or payload.get("node_id")
            or payload.get("session_id")
            or payload.get("worker_id")
            or payload.get("artifact_id")
            or event.get("node_id")
            or task_id
            or "global"
        )
        action_id = str(
            payload.get("action_id")
            or payload.get("tool_use_id")
            or payload.get("command_id")
            or payload.get("request_id")
            or payload.get("attempt_id")
            or causation_id
        )
        transition_id = event_id or self._derived_transition_id(
            run_id=run_id,
            task_id=task_id,
            event_type=event_type,
            semantic_family=semantic_family,
            semantic_key=semantic_key,
            before_revision=before_revision,
            after_revision=after_revision,
            causation_id=causation_id,
            index=index,
        )
        encoded_bytes = len(json.dumps(event, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
        return (
            TransitionObservation(
                transition_id=transition_id,
                run_id=run_id or expected_run_id,
                task_id=task_id,
                event_type=event_type,
                before_revision=before_revision,
                after_revision=after_revision,
                causation_id=causation_id or transition_id,
                semantic_family=semantic_family,
                semantic_key=semantic_key,
                before_digest=before_digest,
                after_digest=after_digest,
                action_id=action_id,
                actor_id=str(event.get("actor_id") or payload.get("actor_id") or metadata.get("actor_id") or ""),
                timestamp=str(event.get("created_at") or event.get("timestamp") or ""),
                payload_bytes=encoded_bytes,
                metadata={"source_index": index},
            ),
            None,
        )

    @staticmethod
    def _is_noop(
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> bool:
        flags = (
            event.get("noop"),
            payload.get("noop"),
            payload.get("no_op"),
            metadata.get("noop"),
            metadata.get("fixture"),
            metadata.get("replay_only"),
            metadata.get("projector_duplicate"),
        )
        if any(value is True for value in flags):
            return True
        status = str(payload.get("status") or metadata.get("status") or "").lower()
        return status in {"noop", "heartbeat", "duplicate", "replayed"}

    @staticmethod
    def _semantic_family(
        event_type: str,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> str:
        explicit = str(payload.get("semantic_family") or metadata.get("semantic_family") or "").strip()
        if explicit:
            return explicit
        normalized = event_type.lower().replace("-", "_")
        parts = [part for part in normalized.split("_") if part]
        for part in parts:
            if part in _SEMANTIC_EVENT_PREFIXES:
                return part
        for prefix in _SEMANTIC_EVENT_PREFIXES:
            if normalized.startswith(prefix):
                return prefix
        return ""

    @staticmethod
    def _states(
        event: Mapping[str, Any],
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> tuple[Any, Any]:
        before = (
            event.get("before")
            if "before" in event
            else payload.get("before", payload.get("previous_state", metadata.get("before")))
        )
        after = (
            event.get("after")
            if "after" in event
            else payload.get("after", payload.get("new_state", metadata.get("after")))
        )
        if before is None or after is None:
            mutation = payload.get("mutation") if isinstance(payload.get("mutation"), Mapping) else {}
            before = before if before is not None else mutation.get("before")
            after = after if after is not None else mutation.get("after")
        if before is None or after is None:
            revisions = payload.get("revisions") if isinstance(payload.get("revisions"), Mapping) else {}
            before = before if before is not None else revisions.get("before_state")
            after = after if after is not None else revisions.get("after_state")
        return before, after

    @staticmethod
    def _first_value(*sources: Mapping[str, Any], keys: Sequence[str]) -> Any:
        for source in sources:
            for key in keys:
                if key in source and source[key] is not None and source[key] != "":
                    return source[key]
        return None

    @staticmethod
    def _digest(value: Any) -> str:
        if value is None:
            return ""
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _derived_transition_id(**values: Any) -> str:
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":"), default=str)
        return "transition_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _first_run_id(events: Sequence[Mapping[str, Any]]) -> str:
        for event in events:
            value = str(event.get("run_id") or "").strip()
            if value:
                return value
        return ""

    @staticmethod
    def _monotonic_error(
        transition: TransitionObservation,
        last_revision_by_key: Mapping[tuple[str, str], int | str],
    ) -> str:
        key = (transition.semantic_family, transition.semantic_key)
        previous = last_revision_by_key.get(key)
        before = transition.before_revision
        after = transition.after_revision
        if isinstance(before, int) and isinstance(after, int):
            if after <= before:
                return f"after={after} must be greater than before={before}"
            if isinstance(previous, int) and before < previous:
                return f"before={before} regresses behind previous={previous}"
        elif before == after:
            return "before and after revisions are equal"
        return ""


class LongHorizonProgressLedger:
    def __init__(
        self,
        *,
        minimum_actions: int = 1_000,
        minimum_transitions: int = 2_000,
        extractor: TransitionExtractor | None = None,
    ) -> None:
        self.minimum_actions = int(minimum_actions)
        self.minimum_transitions = int(minimum_transitions)
        self.extractor = extractor or TransitionExtractor()
        if self.minimum_actions <= 0 or self.minimum_transitions <= 0:
            raise ValueError("long-horizon thresholds must be positive")

    def evaluate(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        run_id: str = "",
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="long-horizon-progress",
            status=GateStatus.NOT_RUN,
            summary="Effective action and canonical semantic-transition accounting.",
        )
        snapshot = self.extractor.extract(events, expected_run_id=run_id)
        result.findings.extend(self._integrity_findings(snapshot, final_completion=final_completion))
        action_ok = snapshot.effective_action_count >= self.minimum_actions
        transition_ok = snapshot.effective_transition_count >= self.minimum_transitions
        threshold_severity = Severity.BLOCKER if final_completion else Severity.WARNING
        if not action_ok:
            result.add(
                Finding(
                    code="progress.action_threshold_open",
                    severity=threshold_severity,
                    summary="The run has not reached the effective-action threshold.",
                    detail=f"observed={snapshot.effective_action_count}; required={self.minimum_actions}",
                )
            )
        if not transition_ok:
            result.add(
                Finding(
                    code="progress.transition_threshold_open",
                    severity=threshold_severity,
                    summary="The run has not reached the canonical-transition threshold.",
                    detail=f"observed={snapshot.effective_transition_count}; required={self.minimum_transitions}",
                )
            )
        if snapshot.effective_transition_count and snapshot.effective_action_count == 0:
            result.add(
                Finding(
                    code="progress.transitions_without_actions",
                    severity=Severity.ERROR if final_completion else Severity.WARNING,
                    summary="Effective transitions are not correlated with actions.",
                )
            )
        result.metrics.update(
            {
                **snapshot.to_dict(include_transitions=True),
                "minimum_actions": self.minimum_actions,
                "minimum_transitions": self.minimum_transitions,
                "action_threshold_met": action_ok,
                "transition_threshold_met": transition_ok,
                "bytes_per_effective_transition": round(
                    sum(item.payload_bytes for item in snapshot.transitions)
                    / max(1, snapshot.effective_transition_count),
                    3,
                ),
            }
        )
        if not final_completion and (not action_ok or not transition_ok):
            result.limitations.append("Foundation accounting is executable; final 1,000/2,000 live-run evidence remains open.")
        return result.finish(default_partial=not final_completion)

    @staticmethod
    def _integrity_findings(
        snapshot: ProgressLedgerSnapshot,
        *,
        final_completion: bool,
    ) -> list[Finding]:
        counts = Counter(item.reason for item in snapshot.rejected)
        findings: list[Finding] = []
        blockers = {
            "duplicate_transition_id",
            "duplicate_event_id",
            "revision_not_monotonic",
            "semantic_state_unchanged",
        }
        errors = {"revision_pair_missing", "causation_missing", "run_id_mismatch"}
        for reason, count in sorted(counts.items()):
            if reason in blockers:
                severity = Severity.BLOCKER
            elif reason in errors:
                severity = Severity.ERROR if final_completion else Severity.INFO
            else:
                severity = Severity.INFO
            findings.append(
                Finding(
                    code=f"progress.rejected.{reason}",
                    severity=severity,
                    summary="Events were excluded from effective transition accounting.",
                    detail=f"reason={reason}; count={count}",
                )
            )
        return findings


def transition_event(
    *,
    event_id: str,
    run_id: str,
    task_id: str,
    event_type: str,
    semantic_family: str,
    semantic_key: str,
    before_revision: int,
    after_revision: int,
    before: Any,
    after: Any,
    causation_id: str,
    action_id: str,
) -> dict[str, Any]:
    """Build a transition envelope for runtime producers; tests still validate non-helper input."""

    return {
        "event_id": event_id,
        "run_id": run_id,
        "task_id": task_id,
        "event_type": event_type,
        "payload": {
            "semantic_family": semantic_family,
            "semantic_key": semantic_key,
            "before_revision": before_revision,
            "after_revision": after_revision,
            "before": before,
            "after": after,
            "causation_id": causation_id,
            "action_id": action_id,
        },
    }
