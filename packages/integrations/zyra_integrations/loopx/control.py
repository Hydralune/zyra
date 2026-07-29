from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from zyra_core import now_iso

from .bridge import (
    LoopXDispatcher,
    LoopXOutbox,
    LoopXRuntimeStateAdapter,
    OutboxState,
    SyncStatus,
    workspace_identity,
)


LOOPX_CONTROL_STATE_SCHEMA = "zyra.loopx-control-state/v1"
LOOPX_CONTROL_RESULT_SCHEMA = "zyra.loopx-control-result/v1"


def task_goal_id(task_id: str) -> str:
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:20]
    return f"goal_zyra_{digest}"


def _record_view(record: Any) -> dict[str, Any]:
    return {
        "sequence": record.sequence,
        "state": record.state.value,
        "attempts": record.attempts,
        "idempotency_key": record.command.idempotency_key,
        "goal_id": record.command.update.goal_id,
        "task_id": record.command.task_id,
        "last_error_code": record.last_error_code,
        "last_error_message": record.last_error_message,
        "apply_receipt": dict(record.apply_receipt or {}),
    }


class LoopXControlRuntime:
    """Versioned control projection over the durable LoopX bridge.

    This class owns no task graph, permission, lease, or execution budget
    state. Mutating callers must supply an already committed
    GraphStateCustody receipt and validation receipts from the Zyra owners.
    """

    def __init__(
        self,
        *,
        workspace_root: Path,
        outbox: LoopXOutbox,
        runtime: LoopXRuntimeStateAdapter,
        dispatcher: LoopXDispatcher,
    ) -> None:
        self.workspace_root = workspace_root.resolve()
        self.workspace_id = workspace_identity(self.workspace_root)
        self.outbox = outbox
        self.runtime = runtime
        self.dispatcher = dispatcher

    def snapshot(
        self,
        *,
        run_id: str,
        task_id: str,
        goal_id: str = "",
    ) -> dict[str, Any]:
        effective_goal = goal_id or task_goal_id(task_id)
        records = [
            item
            for item in self.outbox.list_records(limit=1000)
            if item.command.task_id == task_id
            and item.command.update.goal_id == effective_goal
        ]
        checkpoint = self.outbox.checkpoint()
        private: dict[str, Any] = {}
        error: dict[str, Any] | None = None
        try:
            private = self.runtime.read_private_state(effective_goal)
            if private.get("schema") != "zyra.loopx-private-state/v1":
                raise ValueError("unsupported LoopX private state schema")
            if private.get("mapping_version") != "zyra.loopx-state-mapping/v1":
                raise ValueError("unsupported LoopX mapping version")
        except FileNotFoundError:
            private = {}
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            error = {
                "code": (
                    "loopx_state_version_mismatch"
                    if "unsupported" in str(exc)
                    else "loopx_private_state_corrupt"
                ),
                "message": str(exc),
                "recovery": "retry_sync_or_disconnect_and_reconnect",
            }

        connected = bool(private.get("connected", False))
        pending = [item for item in records if item.state is OutboxState.PENDING]
        dead = [
            item for item in records if item.state is OutboxState.DEAD_LETTER
        ]
        degraded = bool(error or pending or dead)
        lifecycle = (
            "degraded"
            if degraded
            else "enabled"
            if connected
            else "disabled"
        )
        todo_projection = dict(private.get("todo_projection") or {})
        todos: list[dict[str, Any]] = []
        for role_name in ("user_todos", "agent_todos"):
            summary = todo_projection.get(role_name)
            items = summary.get("items") if isinstance(summary, Mapping) else ()
            for item in items if isinstance(items, list) else ():
                if isinstance(item, Mapping):
                    todos.append(dict(item))
        claims = [
            {
                "todo_id": str(item.get("todo_id") or ""),
                "claimant": str(item.get("claimed_by") or ""),
            }
            for item in todos
            if item.get("claimed_by")
        ]
        latest = records[-1] if records else None
        latest_receipt = (
            dict(latest.apply_receipt or {})
            if latest is not None
            else dict(private.get("last_validated_receipt") or {})
        )
        return {
            "schema": LOOPX_CONTROL_STATE_SCHEMA,
            "workspace_id": self.workspace_id,
            "run_id": run_id,
            "task_id": task_id,
            "goal_id": effective_goal,
            "lifecycle": lifecycle,
            "connected": connected,
            "degraded": degraded,
            "error": error,
            "private_state": {
                "owner": "LoopX",
                "goal": {
                    "goal_id": effective_goal,
                    "objective_ref": str(private.get("objective_ref") or ""),
                    "requirement_revision": str(
                        private.get("requirement_revision") or ""
                    ),
                },
                "todos": todos,
                "claims": claims,
                "quota": dict(private.get("quota") or {}),
                "history": list(private.get("history") or []),
            },
            "canonical_state": {
                "task_owner": "Zyra orchestration/runtime",
                "worker_lease_owner": "WorkerLeaseManager",
                "execution_budget_owner": "ResourceScheduler",
                "loopx_claim_is_worker_lease": False,
                "loopx_quota_is_execution_budget": False,
            },
            "sync": {
                "pending": len(pending),
                "acked": sum(
                    item.state is OutboxState.ACKED for item in records
                ),
                "dead_letter": len(dead),
                "cursor": max(
                    (item.sequence for item in records),
                    default=0,
                ),
                "workspace_cursor": int(checkpoint["max_sequence"]),
                "records": [_record_view(item) for item in records[-20:]],
            },
            "continuation": {
                "allowed": bool(private.get("continuation_allowed", False))
                and not degraded,
                "interaction_contract": dict(
                    private.get("interaction_contract") or {}
                ),
            },
            "last_validated_receipt": dict(
                private.get("last_validated_receipt") or {}
            ),
            "last_sync_receipt": latest_receipt,
        }

    def mutate(
        self,
        *,
        action: str,
        run_id: str,
        task_id: str,
        canonical_commit: Any,
        validation: Mapping[str, Any],
        payload: Mapping[str, Any],
        idempotency_key: str,
        causation_id: str,
    ) -> dict[str, Any]:
        normalized = action.strip().lower().replace("-", "_")
        goal_id = str(payload.get("goal_id") or task_goal_id(task_id))
        before = self.snapshot(
            run_id=run_id,
            task_id=task_id,
            goal_id=goal_id,
        )
        private = dict(before["private_state"])
        goal = dict(private["goal"])
        quota = dict(private["quota"])
        connected = before["connected"]
        update: dict[str, Any] = {
            "goal_id": goal_id,
            "objective_ref": str(
                payload.get("objective_ref")
                or goal.get("objective_ref")
                or f"zyra://run/{run_id}/task/{task_id}/objective"
            ),
            "requirement_revision": str(
                payload.get("requirement_revision")
                or goal.get("requirement_revision")
                or "r1"
            ),
            "connected": connected,
            "todos": [],
            "claims": [],
            "release_claims": [],
            "quota": {
                "limit_slots": int(
                    payload.get("limit_slots")
                    or quota.get("limit_slots")
                    or 8
                ),
                "requested_spend_slots": 0,
                "window_hours": float(
                    payload.get("window_hours")
                    or quota.get("window_hours")
                    or 24
                ),
            },
            "validation": dict(validation),
            "history": [
                {
                    "source_event_id": causation_id,
                    "summary": f"LoopX control action {normalized} accepted by Zyra",
                    "verified": True,
                    "evidence_refs": [
                        f"canonical-commit:{getattr(getattr(canonical_commit, 'receipt', canonical_commit), 'commit_id', '')}"
                    ],
                }
            ],
        }
        if normalized == "connect":
            title = str(
                payload.get("todo_title")
                or payload.get("objective")
                or "Continue the verified Zyra long-horizon task"
            ).strip()
            todo_id = str(payload.get("todo_id") or "todo_primary")
            update["connected"] = True
            update["todos"] = [
                {
                    "todo_id": todo_id,
                    "title": title,
                    "role": str(payload.get("role") or "agent"),
                    "priority": str(payload.get("priority") or "P1"),
                    "action_kind": "advance",
                }
            ]
        elif normalized == "disconnect":
            if not connected:
                raise ValueError("LoopX goal is not connected")
            update["connected"] = False
        elif normalized == "claim":
            self._require_connected(connected)
            update["claims"] = [
                {
                    "todo_id": str(payload.get("todo_id") or ""),
                    "claimant": str(payload.get("claimant") or ""),
                }
            ]
        elif normalized == "release":
            self._require_connected(connected)
            update["release_claims"] = [
                {
                    "todo_id": str(payload.get("todo_id") or ""),
                    "claimant": str(payload.get("claimant") or ""),
                }
            ]
        elif normalized == "interaction_submit":
            self._require_connected(connected)
            update["quota"]["requested_spend_slots"] = int(
                payload.get("spend_slots") or 0
            )
            update["interaction"] = {
                "input_ref": str(payload.get("input_ref") or causation_id),
                "feedback_ref": str(payload.get("feedback_ref") or ""),
                "continuation_hint": str(
                    payload.get("continuation_hint")
                    or payload.get("message")
                    or "Continue the next bounded verified todo"
                ),
            }
        else:
            raise ValueError(f"unsupported LoopX mutation: {action}")

        record = self.outbox.enqueue_after_commit(
            run_id=run_id,
            task_id=task_id,
            canonical_commit=canonical_commit,
            update=update,
            causation_id=causation_id,
            correlation_id=run_id,
            idempotency_key=idempotency_key,
            created_at=now_iso(),
        )
        receipts = self.dispatcher.dispatch(limit=100)
        receipt = next(
            (
                item
                for item in receipts
                if item.sequence == record.sequence
            ),
            None,
        )
        after = self.snapshot(
            run_id=run_id,
            task_id=task_id,
            goal_id=goal_id,
        )
        return {
            "schema": LOOPX_CONTROL_RESULT_SCHEMA,
            "action": normalized,
            "sequence": record.sequence,
            "receipt": receipt.to_dict() if receipt else {},
            "state": after,
        }

    def retry_sync(
        self,
        *,
        run_id: str,
        task_id: str,
        goal_id: str = "",
        sequence: int | None = None,
    ) -> dict[str, Any]:
        effective_goal = goal_id or task_goal_id(task_id)
        before = self.snapshot(
            run_id=run_id,
            task_id=task_id,
            goal_id=effective_goal,
        )
        rebuilt: dict[str, Any] | None = None
        if before.get("error"):
            acknowledged = [
                item
                for item in self.outbox.list_records(
                    state=OutboxState.ACKED,
                    limit=1000,
                )
                if item.command.task_id == task_id
                and item.command.update.goal_id == effective_goal
            ]
            if acknowledged:
                with self.dispatcher.single_writer.acquire() as fence:
                    rebuilt = self.runtime.apply(
                        acknowledged[-1].command,
                        fence,
                    ).to_dict()
        records = [
            item
            for item in self.outbox.list_records(
                state=OutboxState.DEAD_LETTER,
                limit=1000,
            )
            if item.command.task_id == task_id
            and item.command.update.goal_id == effective_goal
            and (sequence is None or item.sequence == sequence)
        ]
        replayed = []
        for item in records:
            replayed.append(
                _record_view(
                    self.dispatcher.replay_dead_letter(item.sequence)
                )
            )
        receipts = [
            item.to_dict() for item in self.dispatcher.dispatch(limit=100)
        ]
        return {
            "schema": LOOPX_CONTROL_RESULT_SCHEMA,
            "action": "sync_retry",
            "replayed": replayed,
            "rebuilt_private_projection": rebuilt,
            "receipts": receipts,
            "state": self.snapshot(
                run_id=run_id,
                task_id=task_id,
                goal_id=effective_goal,
            ),
        }

    @staticmethod
    def _require_connected(connected: bool) -> None:
        if not connected:
            raise ValueError("LoopX goal must be connected first")


__all__ = [
    "LOOPX_CONTROL_RESULT_SCHEMA",
    "LOOPX_CONTROL_STATE_SCHEMA",
    "LoopXControlRuntime",
    "task_goal_id",
]
