from __future__ import annotations

from copy import deepcopy
from typing import Any

from zyra_core import EventRecord, TaskState, new_id

CONTEXT_SESSION_KEY = "context_session"


class ContextSessionRuntime:
    """Stateful context/session control for slash commands.

    The event log remains append-only. Commands such as /clear and /rewind only
    update the visible context window stored in the task checkpoint metadata.
    """

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events

    def summarize(self, state: TaskState) -> dict[str, Any]:
        session = ensure_context_session(state)
        visible = self.visible_events(session)
        snapshots = list(session.get("snapshots", []))
        return {
            "events": len(self.events),
            "visible_events": len(visible),
            "hidden_events": max(len(self.events) - len(visible), 0),
            "plan_nodes": len(state.plan_nodes),
            "artifacts": len(state.artifacts),
            "control_commands": len(state.metadata.get("control_commands", [])),
            "requirement_changes": len(state.metadata.get("requirement_changes", [])),
            "failure_injections": len(state.metadata.get("failure_injections", [])),
            "active_session_id": session.get("active_session_id"),
            "generation": session.get("generation", 0),
            "visible_segments": deepcopy(session.get("visible_segments", [])),
            "snapshots": snapshots[-8:],
            "snapshot_count": len(snapshots),
        }

    def clear(self, state: TaskState, event: EventRecord) -> dict[str, Any]:
        session = ensure_context_session(state)
        snapshot = self._snapshot_current_context(state, event, reason="before_clear")
        new_session_id = new_id("session")
        session["active_session_id"] = new_session_id
        session["generation"] = int(session.get("generation", 0)) + 1
        session["visible_segments"] = [
            {
                "after_event_id": event.event_id,
                "before_event_id": None,
                "reason": "after_clear",
            }
        ]
        session["last_clear_event_id"] = event.event_id
        self._append_history(
            session,
            event,
            action="clear",
            snapshot_id=snapshot["snapshot_id"],
            active_session_id=new_session_id,
        )
        return {
            "summary": "Visible context cleared; durable event trace retained.",
            "data": {
                "snapshot": snapshot,
                "active_session_id": new_session_id,
                "generation": session["generation"],
                "cleared_visible_events": snapshot["visible_event_count"],
                "session": self.summarize(state),
            },
        }

    def rewind(self, state: TaskState, event: EventRecord, target: str = "") -> dict[str, Any]:
        return self._restore_snapshot(
            state,
            event,
            target=target,
            command_name="rewind",
            no_target_summary="No context snapshot is available to rewind.",
            restored_summary="Context rewound to a prior visible window.",
        )

    def resume(self, state: TaskState, event: EventRecord, target: str = "") -> dict[str, Any]:
        session = ensure_context_session(state)
        if not target.strip():
            return {
                "summary": "Available resumable context snapshots.",
                "data": {
                    "snapshots": list(session.get("snapshots", [])),
                    "active_session_id": session.get("active_session_id"),
                    "session": self.summarize(state),
                },
            }
        return self._restore_snapshot(
            state,
            event,
            target=target,
            command_name="resume",
            no_target_summary="No matching context snapshot was found to resume.",
            restored_summary="Context session resumed from a saved snapshot.",
        )

    def memory_view(self, state: TaskState) -> dict[str, Any]:
        session_summary = self.summarize(state)
        return {
            "summary": "Task memory and context session summary.",
            "data": {
                "objectives": list(state.constraints.objectives),
                "requirements": list(state.constraints.requirements),
                "success_criteria": list(state.constraints.success_criteria),
                "compactions": list(state.metadata.get("compactions", [])),
                "exports": list(state.metadata.get("exports", [])),
                "requirement_changes": list(state.metadata.get("requirement_changes", [])),
                "failure_injections": list(state.metadata.get("failure_injections", [])),
                "session": session_summary,
            },
        }

    def visible_events(self, session: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        active_session = session or {}
        segments = _valid_segments(active_session.get("visible_segments"))
        if not segments:
            segments = [_open_segment()]
        selected: list[dict[str, Any]] = []
        selected_ids: set[str] = set()
        for segment in segments:
            for event in self._events_for_segment(segment):
                event_id = str(event.get("event_id") or "")
                if event_id and event_id not in selected_ids:
                    selected.append(event)
                    selected_ids.add(event_id)
        return selected

    def _restore_snapshot(
        self,
        state: TaskState,
        event: EventRecord,
        *,
        target: str,
        command_name: str,
        no_target_summary: str,
        restored_summary: str,
    ) -> dict[str, Any]:
        session = ensure_context_session(state)
        snapshot = _find_snapshot(session, target)
        if snapshot is None:
            return {
                "ok": False,
                "summary": no_target_summary,
                "data": {
                    "target": target,
                    "snapshots": list(session.get("snapshots", [])),
                    "session": self.summarize(state),
                },
            }

        current_snapshot = self._snapshot_current_context(state, event, reason=f"before_{command_name}")
        restored_segments = deepcopy(snapshot.get("visible_segments", []))
        restored_segments.append(
            {
                "after_event_id": event.event_id,
                "before_event_id": None,
                "reason": f"after_{command_name}",
            }
        )
        session["active_session_id"] = snapshot.get("session_id") or new_id("session")
        session["generation"] = int(snapshot.get("generation") or 0)
        session["visible_segments"] = restored_segments
        session[f"last_{command_name}_event_id"] = event.event_id
        self._append_history(
            session,
            event,
            action=command_name,
            snapshot_id=snapshot["snapshot_id"],
            pre_restore_snapshot_id=current_snapshot["snapshot_id"],
            active_session_id=session["active_session_id"],
        )
        return {
            "summary": restored_summary,
            "data": {
                "restored_snapshot": snapshot,
                "pre_restore_snapshot": current_snapshot,
                "active_session_id": session["active_session_id"],
                "generation": session["generation"],
                "session": self.summarize(state),
            },
        }

    def _snapshot_current_context(self, state: TaskState, event: EventRecord, *, reason: str) -> dict[str, Any]:
        session = ensure_context_session(state)
        frozen_segments = _freeze_open_segments(session.get("visible_segments"), event.event_id)
        visible_count = len(self.visible_events({"visible_segments": frozen_segments}))
        snapshot = {
            "snapshot_id": new_id("snapshot"),
            "session_id": session.get("active_session_id"),
            "generation": int(session.get("generation", 0)),
            "source_event_id": event.event_id,
            "created_at": event.created_at,
            "reason": reason,
            "label": _event_raw_text(event),
            "visible_segments": frozen_segments,
            "visible_event_count": visible_count,
            "artifact_count": len(state.artifacts),
            "control_command_count": len(state.metadata.get("control_commands", [])),
        }
        snapshots = session.setdefault("snapshots", [])
        snapshots.append(snapshot)
        return snapshot

    def _events_for_segment(self, segment: dict[str, Any]) -> list[dict[str, Any]]:
        after_event_id = segment.get("after_event_id")
        before_event_id = segment.get("before_event_id")
        start = _event_index(self.events, after_event_id) + 1 if after_event_id else 0
        end = _event_index(self.events, before_event_id) if before_event_id else len(self.events)
        if start < 0:
            start = 0
        if end < 0:
            end = len(self.events)
        if start > end:
            return []
        return self.events[start:end]

    def _append_history(self, session: dict[str, Any], event: EventRecord, *, action: str, **metadata: Any) -> None:
        history = session.setdefault("history", [])
        history.append(
            {
                "event_id": event.event_id,
                "action": action,
                "created_at": event.created_at,
                **metadata,
            }
        )


def ensure_context_session(state: TaskState) -> dict[str, Any]:
    session = state.metadata.setdefault(CONTEXT_SESSION_KEY, {})
    session.setdefault("active_session_id", "session_initial")
    session.setdefault("generation", 0)
    session.setdefault("visible_segments", [_open_segment()])
    session.setdefault("snapshots", [])
    session.setdefault("history", [])
    return session


def _open_segment() -> dict[str, Any]:
    return {"after_event_id": None, "before_event_id": None, "reason": "initial"}


def _valid_segments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    segments: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            segments.append(
                {
                    "after_event_id": item.get("after_event_id"),
                    "before_event_id": item.get("before_event_id"),
                    "reason": str(item.get("reason") or "context"),
                }
            )
    return segments


def _freeze_open_segments(value: Any, before_event_id: str) -> list[dict[str, Any]]:
    segments = _valid_segments(value) or [_open_segment()]
    frozen: list[dict[str, Any]] = []
    for segment in segments:
        next_segment = dict(segment)
        if next_segment.get("before_event_id") is None:
            next_segment["before_event_id"] = before_event_id
        frozen.append(next_segment)
    return frozen


def _find_snapshot(session: dict[str, Any], target: str) -> dict[str, Any] | None:
    snapshots = list(session.get("snapshots", []))
    if not snapshots:
        return None
    normalized = target.strip()
    if not normalized or normalized.lower() in {"latest", "last", "previous"}:
        return snapshots[-1]
    for snapshot in reversed(snapshots):
        if _snapshot_matches(snapshot, normalized):
            return snapshot
    return None


def _snapshot_matches(snapshot: dict[str, Any], target: str) -> bool:
    candidates = {
        str(snapshot.get("snapshot_id") or ""),
        str(snapshot.get("session_id") or ""),
        str(snapshot.get("source_event_id") or ""),
        str(snapshot.get("generation") or ""),
    }
    if target in candidates:
        return True
    label = str(snapshot.get("label") or "")
    return bool(label and target.lower() in label.lower())


def _event_index(events: list[dict[str, Any]], event_id: Any) -> int:
    if event_id is None:
        return -1
    expected = str(event_id)
    for index, event in enumerate(events):
        if str(event.get("event_id") or "") == expected:
            return index
    return -1


def _event_raw_text(event: EventRecord) -> str:
    raw = event.payload.get("raw", "")
    return str(raw) if raw is not None else ""
