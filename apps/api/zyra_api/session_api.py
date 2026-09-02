from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from .typed_transport import MAX_CURSOR_BYTES, TypedTransportError


ACTIVE_TASK_STATUSES = frozenset(
    {"pending", "queued", "running", "blocked", "waiting", "paused", "recovering"}
)
TERMINAL_TASK_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "rejected", "timed_out"}
)
_TASK_SESSION_PATTERN = re.compile(r"^task:(task_[A-Za-z0-9._:-]+)$")


def canonical_session_id(value: object) -> str:
    rendered = str(value or "").strip()
    task_match = _TASK_SESSION_PATTERN.fullmatch(rendered)
    if task_match is not None:
        return f"session_{task_match.group(1)}"
    return rendered


def session_projection(tasks: Sequence[Mapping[str, Any]], session_id: str) -> dict[str, Any]:
    selected = [dict(task) for task in tasks if _task_session_id(task) == session_id]
    if not selected:
        raise KeyError(session_id)
    selected.sort(key=_task_order, reverse=True)
    task_ids = [str(task.get("task_id") or "") for task in selected]
    active_task_ids = [
        str(task.get("task_id") or "")
        for task in selected
        if _task_status(task) in ACTIVE_TASK_STATUSES
    ]
    candidates = active_task_ids if active_task_ids else task_ids
    resolution = "resolved" if len(candidates) == 1 else "ambiguous"
    statuses = sorted({_task_status(task) for task in selected if _task_status(task)})
    created_values = sorted(
        str(task.get("created_at") or "") for task in selected if task.get("created_at")
    )
    updated_values = sorted(
        str(task.get("updated_at") or task.get("created_at") or "")
        for task in selected
        if task.get("updated_at") or task.get("created_at")
    )
    terminal = bool(selected) and all(
        _task_status(task) in TERMINAL_TASK_STATUSES for task in selected
    )
    title = next(
        (
            str(task.get("session_title") or "").strip()
            for task in selected
            if str(task.get("session_title") or "").strip()
        ),
        "",
    )
    return {
        "session_id": session_id,
        "task_ids": task_ids,
        "active_task_ids": active_task_ids,
        "latest_task_id": task_ids[0],
        "resume_task_id": candidates[0] if resolution == "resolved" else None,
        "resolution": resolution,
        "task_count": len(task_ids),
        "statuses": statuses,
        "active": bool(active_task_ids),
        "terminal": terminal,
        "title": title or None,
        "created_at": created_values[0] if created_values else None,
        "updated_at": updated_values[-1] if updated_values else None,
    }


def list_session_projections(tasks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    session_ids = sorted({_task_session_id(task) for task in tasks if _task_session_id(task)})
    sessions = [session_projection(tasks, session_id) for session_id in session_ids]
    sessions.sort(
        key=lambda session: (
            str(session.get("updated_at") or session.get("created_at") or ""),
            str(session.get("session_id") or ""),
        ),
        reverse=True,
    )
    return sessions


def paginate_sessions(
    tasks: Sequence[Mapping[str, Any]],
    query: Mapping[str, list[str]],
) -> dict[str, Any]:
    status = str((query.get("status") or [""])[0] or "").strip().lower()
    try:
        limit = int((query.get("limit") or ["100"])[0] or 100)
    except (TypeError, ValueError) as error:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "invalid_session_limit",
            "Session list limit must be an integer.",
        ) from error
    limit = min(1000, max(1, limit))
    offset = _decode_cursor(
        str((query.get("cursor") or [""])[0] or ""),
        status=status,
        limit=limit,
    )
    sessions = [
        session
        for session in list_session_projections(tasks)
        if _session_matches(session, status)
    ]
    page = sessions[offset : offset + limit]
    next_offset = offset + len(page)
    return {
        "schema": "zyra.session-list.v1",
        "state_owner": "task_store_projection",
        "sessions": page,
        "total": len(sessions),
        "cursor": (
            _encode_cursor(next_offset, status=status, limit=limit)
            if next_offset < len(sessions)
            else None
        ),
    }


def session_detail(tasks: Sequence[Mapping[str, Any]], session_id: object) -> dict[str, Any]:
    normalized = canonical_session_id(session_id)
    if not normalized:
        raise KeyError("")
    return {
        "schema": "zyra.session-detail.v1",
        "state_owner": "task_store_projection",
        "session": session_projection(tasks, normalized),
    }


def _task_session_id(task: Mapping[str, Any]) -> str:
    metadata = task.get("metadata")
    values = metadata if isinstance(metadata, Mapping) else {}
    return canonical_session_id(
        values.get("query_session_id")
        or values.get("session_id")
        or task.get("session_id")
        or f"task:{task.get('task_id') or ''}"
    )


def _task_status(task: Mapping[str, Any]) -> str:
    return str(task.get("status") or "").strip().lower()


def _task_order(task: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(task.get("updated_at") or task.get("created_at") or ""),
        str(task.get("task_id") or ""),
    )


def _session_matches(session: Mapping[str, Any], status: str) -> bool:
    if not status:
        return True
    if status == "active":
        return bool(session.get("active"))
    if status == "terminal":
        return bool(session.get("terminal"))
    if status == "ambiguous":
        return session.get("resolution") == "ambiguous"
    statuses = session.get("statuses")
    return isinstance(statuses, Sequence) and status in statuses


def _encode_cursor(offset: int, *, status: str, limit: int) -> str:
    payload = json.dumps(
        {"v": 1, "scope": "session-list", "offset": offset, "status": status, "limit": limit},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(value: str, *, status: str, limit: int) -> int:
    rendered = str(value or "").strip()
    if not rendered:
        return 0
    if len(rendered.encode("utf-8")) > MAX_CURSOR_BYTES:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Session cursor is too large.",
        )
    try:
        padded = rendered + "=" * ((4 - len(rendered) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8"))
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Session cursor is not valid.",
            details={"cause": str(error)},
        ) from error
    if not isinstance(payload, Mapping):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Session cursor payload must be an object.",
        )
    try:
        cursor_limit = int(payload.get("limit") or 0)
        offset = int(payload.get("offset") or 0)
    except (TypeError, ValueError) as error:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Session cursor fields are not valid integers.",
        ) from error
    if (
        payload.get("v") != 1
        or payload.get("scope") != "session-list"
        or str(payload.get("status") or "") != status
        or cursor_limit != limit
    ):
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_scope_mismatch",
            "Session cursor does not match the current query.",
        )
    if offset < 0:
        raise TypedTransportError(
            HTTPStatus.BAD_REQUEST,
            "cursor_invalid",
            "Session cursor offset must be non-negative.",
        )
    return offset
