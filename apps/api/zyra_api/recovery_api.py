from __future__ import annotations

from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Mapping, Sequence

from zyra_scheduler.recovery_runtime import (
    CheckpointCodecError,
    CheckpointRuntimeError,
    DeltaConflictError,
    RecoveryActionError,
    RecoveryApplication,
    RecoveryApplicationError,
    RecoveryContextError,
    RecoveryLeaseError,
    RecoveryRuntimeDisabled,
    RecoverySource,
    RecoveryStoreConflict,
)


ZYRA_RECOVERY_API_ROUTES = (
    "GET /recovery/contract",
    "GET /recovery/plans/{plan_id}",
    "GET /tasks/{task_id}/recovery",
    "POST /tasks/{task_id}/recovery/signals",
    "POST /tasks/{task_id}/recovery/fault-handoff",
    "POST /tasks/{task_id}/recovery/worker-handoff",
    "POST /tasks/{task_id}/recovery/checkpoints",
    "POST /tasks/{task_id}/recovery/checkpoints/{checkpoint_id}/resume",
    "POST /tasks/{task_id}/recovery/deltas",
    "POST /recovery/plans/{plan_id}/resume-waiting",
)


@dataclass(frozen=True, slots=True)
class RecoveryApiResponse:
    status: HTTPStatus
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class RecoveryRuntimeApiService:
    def __init__(self, application: RecoveryApplication) -> None:
        self.application = application

    def route_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, str] | None = None,
    ) -> RecoveryApiResponse | None:
        values = list(parts)
        if values == ["recovery", "contract"]:
            return self._ok({
                "contract": self.application.contract(),
                "routes": list(ZYRA_RECOVERY_API_ROUTES),
            })
        if len(values) == 3 and values[:2] == ["recovery", "plans"]:
            try:
                return self._ok(self.application.plan_view(values[2]))
            except Exception as error:
                return self._error(error)
        if len(values) == 3 and values[0] == "tasks" and values[2] == "recovery":
            try:
                return self._ok(self.application.task_view(values[1]))
            except Exception as error:
                return self._error(error)
        return None

    def route_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any],
    ) -> RecoveryApiResponse | None:
        values = list(parts)
        if len(values) == 4 and values[0] == "tasks" and values[2] == "recovery":
            task_id = values[1]
            operation = values[3]
            try:
                self._require_task_payload(task_id, payload)
                if operation == "signals":
                    source = payload.get("source")
                    result = self.application.recover(
                        self._signal_payload(payload),
                        source=RecoverySource(str(source)) if source else None,
                        context_overrides=dict(payload.get("context") or {}),
                        apply=bool(payload.get("apply", True)),
                        idempotency_key=str(payload.get("idempotency_key") or ""),
                    )
                    return self._response(HTTPStatus.OK if not result.execution else HTTPStatus.ACCEPTED, result.to_dict())
                if operation == "fault-handoff":
                    result = self.application.recover_fault_handoff(
                        dict(payload.get("handoff") or payload),
                        context_overrides=dict(payload.get("context") or {}),
                        apply=bool(payload.get("apply", True)),
                    )
                    return self._response(HTTPStatus.ACCEPTED, result.to_dict())
                if operation == "worker-handoff":
                    results = self.application.recover_worker_handoff(
                        dict(payload.get("handoff") or payload),
                        context_overrides=dict(payload.get("context") or {}),
                        apply=bool(payload.get("apply", True)),
                    )
                    return self._response(HTTPStatus.ACCEPTED, {
                        "task_id": task_id,
                        "results": [item.to_dict() for item in results],
                    })
                if operation == "checkpoints":
                    result = self.application.commit_checkpoint(payload)
                    return self._response(HTTPStatus.CREATED if result["created"] else HTTPStatus.OK, result)
                if operation == "deltas":
                    result = self.application.commit_delta(payload)
                    return self._response(HTTPStatus.OK, result.to_dict())
            except Exception as error:
                return self._error(error)
        if (
            len(values) == 6
            and values[0] == "tasks"
            and values[2:4] == ["recovery", "checkpoints"]
            and values[5] == "resume"
        ):
            try:
                self._require_task_payload(values[1], payload)
                result = self.application.resume_checkpoint(values[4], payload)
                return self._response(HTTPStatus.OK, result)
            except Exception as error:
                return self._error(error)
        if len(values) == 4 and values[:2] == ["recovery", "plans"] and values[3] == "resume-waiting":
            try:
                result = self.application.resume_waiting(
                    values[2],
                    context_overrides=dict(payload.get("context") or {}),
                )
                return self._response(HTTPStatus.OK, result.to_dict())
            except Exception as error:
                return self._error(error)
        return None

    @staticmethod
    def _signal_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        signal = dict(payload.get("signal") or payload)
        for key in ("context", "apply", "idempotency_key"):
            signal.pop(key, None)
        return signal

    @staticmethod
    def _require_task_payload(task_id: str, payload: Mapping[str, Any]) -> None:
        payload_task_id = str(payload.get("task_id") or "")
        refs = payload.get("refs")
        if isinstance(refs, Mapping):
            payload_task_id = str(refs.get("task_id") or payload_task_id)
        signal = payload.get("signal")
        if isinstance(signal, Mapping) and isinstance(signal.get("refs"), Mapping):
            payload_task_id = str(signal["refs"].get("task_id") or payload_task_id)
        if payload_task_id and payload_task_id != task_id:
            raise ValueError("URL task identity does not match payload")

    @classmethod
    def _error(cls, error: Exception) -> RecoveryApiResponse:
        if isinstance(error, RecoveryRuntimeDisabled):
            status = HTTPStatus.SERVICE_UNAVAILABLE
            code = "recovery_runtime_disabled"
        elif isinstance(error, (RecoveryContextError, CheckpointCodecError, ValueError)):
            status = HTTPStatus.BAD_REQUEST
            code = "invalid_recovery_request"
        elif isinstance(error, (RecoveryStoreConflict, RecoveryLeaseError, DeltaConflictError)):
            status = HTTPStatus.CONFLICT
            code = "recovery_state_conflict"
        elif isinstance(error, (CheckpointRuntimeError, RecoveryActionError)):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
            code = "recovery_action_failed"
        elif isinstance(error, RecoveryApplicationError):
            status = HTTPStatus.NOT_FOUND if "not found" in str(error).lower() else HTTPStatus.BAD_REQUEST
            code = "recovery_application_error"
        else:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            code = "recovery_internal_error"
        return cls._response(status, {
            "ok": False,
            "error": code,
            "error_type": type(error).__name__,
            "message": str(error)[:2000],
        })

    @classmethod
    def _ok(cls, body: Mapping[str, Any]) -> RecoveryApiResponse:
        return cls._response(HTTPStatus.OK, body)

    @staticmethod
    def _response(status: HTTPStatus, body: Mapping[str, Any]) -> RecoveryApiResponse:
        return RecoveryApiResponse(
            status=status,
            body={"ok": int(status) < 400, **dict(body)},
            headers={"Cache-Control": "no-store, max-age=0"},
        )
