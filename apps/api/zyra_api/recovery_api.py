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
    RecoveryComponentDisabled,
    RecoveryComponentError,
    RecoveryContextError,
    RecoveryIngressError,
    RecoveryIntegrationError,
    RecoveryIntegrationRuntime,
    RecoveryLeaseError,
    RecoveryRestartError,
    RecoveryRuntimeDisabled,
    RecoverySemanticError,
    RecoverySource,
    RecoveryStateFusionError,
    RecoveryStoreConflict,
    RecoveryVerificationError,
)


ZYRA_RECOVERY_API_ROUTES = (
    "GET /recovery/contract",
    "GET /recovery/components",
    "GET /recovery/plans/{plan_id}",
    "GET /tasks/{task_id}/recovery",
    "POST /tasks/{task_id}/recovery/signals",
    "POST /tasks/{task_id}/recovery/observations",
    "POST /tasks/{task_id}/recovery/observation-batch",
    "POST /tasks/{task_id}/recovery/fault-handoff",
    "POST /tasks/{task_id}/recovery/worker-handoff",
    "POST /tasks/{task_id}/recovery/checkpoints",
    "POST /tasks/{task_id}/recovery/checkpoints/{checkpoint_id}/resume",
    "POST /tasks/{task_id}/recovery/deltas",
    "POST /recovery/plans/{plan_id}/resume-waiting",
    "POST /recovery/plans/{plan_id}/restart",
    "POST /recovery/restart-sweep",
)


@dataclass(frozen=True, slots=True)
class RecoveryApiResponse:
    status: HTTPStatus
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class RecoveryRuntimeApiService:
    def __init__(
        self,
        application: RecoveryApplication,
        *,
        integration: RecoveryIntegrationRuntime | None = None,
    ) -> None:
        self.application = application
        self.integration = integration

    def route_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, str] | None = None,
    ) -> RecoveryApiResponse | None:
        values = list(parts)
        if values == ["recovery", "contract"]:
            return self._ok({
                "contract": self.application.contract(),
                "integration": self.integration.contract() if self.integration else None,
                "routes": list(ZYRA_RECOVERY_API_ROUTES),
            })
        if values == ["recovery", "components"] and self.integration is not None:
            return self._ok(self.integration.components.matrix())
        if len(values) == 3 and values[:2] == ["recovery", "plans"]:
            try:
                return self._ok(
                    self.integration.plan_view(values[2])
                    if self.integration else self.application.plan_view(values[2])
                )
            except Exception as error:
                return self._error(error)
        if len(values) == 3 and values[0] == "tasks" and values[2] == "recovery":
            try:
                return self._ok(
                    self.integration.task_view(values[1])
                    if self.integration else self.application.task_view(values[1])
                )
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
                if operation == "observations":
                    if self.integration is None:
                        raise RecoveryRuntimeDisabled("integrated recovery ingress is unavailable")
                    observation = self._observation_payload(payload)
                    nested_observation = payload.get("observation") or payload.get("payload") or {}
                    domain = str(
                        payload.get("domain")
                        or (nested_observation.get("domain") if isinstance(nested_observation, Mapping) else "")
                        or ""
                    )
                    result = self.integration.observe_and_recover(
                        domain,
                        observation,
                        owner=str(payload.get("owner") or ""),
                        owner_revision=str(payload.get("owner_revision") or ""),
                        event_ids=tuple(payload.get("event_ids") or ()),
                        span_id=str(payload.get("span_id") or ""),
                        context_overrides=dict(payload.get("context") or {}),
                        apply=bool(payload.get("apply", True)),
                        allow_escalation=bool(payload.get("allow_escalation", False)),
                        require_causal_trace=bool(payload.get("require_causal_trace", True)),
                        idempotency_key=str(payload.get("idempotency_key") or ""),
                    )
                    return self._response(HTTPStatus.ACCEPTED if payload.get("apply", True) else HTTPStatus.OK, result.to_dict())
                if operation == "observation-batch":
                    if self.integration is None:
                        raise RecoveryRuntimeDisabled("integrated recovery ingress is unavailable")
                    result = self.integration.recover_batch(
                        tuple(payload.get("observations") or ()),
                        context_overrides=dict(payload.get("context") or {}),
                        apply=bool(payload.get("apply", True)),
                        allow_escalation=bool(payload.get("allow_escalation", False)),
                        stop_on_error=bool(payload.get("stop_on_error", True)),
                    )
                    return self._response(HTTPStatus.ACCEPTED if payload.get("apply", True) else HTTPStatus.OK, result.to_dict())
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
        if len(values) == 4 and values[:2] == ["recovery", "plans"] and values[3] == "restart":
            try:
                if self.integration is None:
                    raise RecoveryRuntimeDisabled("integrated recovery restart is unavailable")
                result = self.integration.restart_plan(
                    values[2],
                    context_overrides=dict(payload.get("context") or {}),
                )
                return self._response(HTTPStatus.OK if result.success else HTTPStatus.CONFLICT, result.to_dict())
            except Exception as error:
                return self._error(error)
        if values == ["recovery", "restart-sweep"]:
            try:
                if self.integration is None:
                    raise RecoveryRuntimeDisabled("integrated recovery restart is unavailable")
                result = self.integration.restart_sweep(
                    task_id=str(payload.get("task_id") or ""),
                    run_id=str(payload.get("run_id") or ""),
                    maximum_plans=int(payload.get("maximum_plans") or 100),
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
    def _observation_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
        observation = dict(payload.get("observation") or payload.get("payload") or payload)
        for key in (
            "domain",
            "owner",
            "owner_revision",
            "event_ids",
            "span_id",
            "context",
            "apply",
            "allow_escalation",
            "require_causal_trace",
            "idempotency_key",
        ):
            observation.pop(key, None)
        return observation

    @staticmethod
    def _require_task_payload(task_id: str, payload: Mapping[str, Any]) -> None:
        payload_task_id = str(payload.get("task_id") or "")
        refs = payload.get("refs")
        if isinstance(refs, Mapping):
            payload_task_id = str(refs.get("task_id") or payload_task_id)
        signal = payload.get("signal")
        if isinstance(signal, Mapping) and isinstance(signal.get("refs"), Mapping):
            payload_task_id = str(signal["refs"].get("task_id") or payload_task_id)
        observation = payload.get("observation") or payload.get("payload")
        if isinstance(observation, Mapping):
            observation_refs = observation.get("refs")
            if isinstance(observation_refs, Mapping):
                payload_task_id = str(observation_refs.get("task_id") or payload_task_id)
            payload_task_id = str(observation.get("task_id") or payload_task_id)
        if payload_task_id and payload_task_id != task_id:
            raise ValueError("URL task identity does not match payload")

    @classmethod
    def _error(cls, error: Exception) -> RecoveryApiResponse:
        if isinstance(error, RecoveryRuntimeDisabled):
            status = HTTPStatus.SERVICE_UNAVAILABLE
            code = "recovery_runtime_disabled"
        elif isinstance(error, (RecoveryComponentDisabled, RecoveryComponentError)):
            status = HTTPStatus.SERVICE_UNAVAILABLE
            code = "recovery_component_disabled"
        elif isinstance(error, (RecoveryContextError, RecoveryIngressError, RecoveryStateFusionError, CheckpointCodecError, ValueError)):
            status = HTTPStatus.BAD_REQUEST
            code = "invalid_recovery_request"
        elif isinstance(error, (RecoveryStoreConflict, RecoveryLeaseError, DeltaConflictError)):
            status = HTTPStatus.CONFLICT
            code = "recovery_state_conflict"
        elif isinstance(error, (CheckpointRuntimeError, RecoveryActionError, RecoveryVerificationError, RecoverySemanticError)):
            status = HTTPStatus.UNPROCESSABLE_ENTITY
            code = "recovery_action_failed"
        elif isinstance(error, (RecoveryRestartError, RecoveryIntegrationError)):
            status = HTTPStatus.CONFLICT
            code = "recovery_integration_failed"
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
