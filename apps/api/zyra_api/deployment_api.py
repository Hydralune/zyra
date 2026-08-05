from __future__ import annotations

import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Mapping

from zyra_orchestration.deployment import (
    DeploymentError,
    DeploymentOrchestrator,
    DeploymentProfile,
    Sensitivity,
    Workload,
    new_id,
)


@dataclass(frozen=True, slots=True)
class DeploymentApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)


class DeploymentApiFacade:
    """Typed deployment control surface backed by the product supervisor state."""

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self.orchestrator = DeploymentOrchestrator(self.project_root)

    def route_get(
        self,
        parts: tuple[str, ...],
        query: Mapping[str, Any],
    ) -> DeploymentApiResponse | None:
        if not parts or parts[0] != "deployment":
            return None
        try:
            if parts == ("deployment", "status"):
                return self._response(self.orchestrator.status())
            if parts == ("deployment", "profiles"):
                return self._response(
                    {
                        "schema": "zyra.deployment-profiles/v1",
                        "ready": True,
                        "profile_digest": self.orchestrator.catalog.profile_digest,
                        "catalog": self.orchestrator.catalog.public_projection(),
                        "fallback": False,
                    }
                )
            if parts == ("deployment", "events"):
                after = self._integer(query, "after_sequence", minimum=0, default=0)
                limit = self._integer(query, "limit", minimum=1, maximum=10_000, default=250)
                task_id = str(query.get("task_id") or "")
                event_types = tuple(
                    item.strip()
                    for item in str(query.get("event_types") or "").split(",")
                    if item.strip()
                )
                events = self.orchestrator.store.events(
                    after_sequence=after,
                    limit=limit,
                    task_id=task_id,
                    event_types=event_types,
                )
                return self._response(
                    {
                        "schema": "zyra.deployment-events/v1",
                        "ready": True,
                        "events": events,
                        "next_sequence": (
                            int(events[-1]["sequence"]) if events else after
                        ),
                        "fallback": False,
                    }
                )
            if parts == ("deployment", "semantic-health", "latest"):
                report = self.orchestrator.store.latest_probe_report()
                return self._response(
                    {
                        "schema": "zyra.deployment-latest-health/v1",
                        "ready": report is not None,
                        "report": report,
                        "fallback": False,
                    },
                    HTTPStatus.OK if report is not None else HTTPStatus.NOT_FOUND,
                )
            return self._error(
                HTTPStatus.NOT_FOUND,
                "deployment_route_not_found",
                "Unknown deployment GET route.",
            )
        except DeploymentError as error:
            return self._deployment_error(error)
        except (TypeError, ValueError) as error:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "deployment_request_invalid",
                str(error),
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "deployment_internal_error",
                "Deployment control state could not be read.",
            )

    def route_post(
        self,
        parts: tuple[str, ...],
        payload: Mapping[str, Any],
    ) -> DeploymentApiResponse | None:
        if not parts or parts[0] != "deployment":
            return None
        try:
            if parts == ("deployment", "doctor"):
                selected = self._strings(payload.get("checks"))
                result = self.orchestrator.doctor().run(
                    selected=selected,
                    fail_fast=payload.get("fail_fast") is True,
                )
                return self._response(
                    result,
                    HTTPStatus.OK if result.get("ready") is True else HTTPStatus.SERVICE_UNAVAILABLE,
                )
            if parts == ("deployment", "semantic-health"):
                result = self.orchestrator.semantic_health(
                    include_short_task=payload.get("include_short_task", True) is True,
                    fresh_state=payload.get("fresh_state", True) is True,
                    selected=self._strings(payload.get("probes")),
                )
                return self._response(
                    result,
                    HTTPStatus.OK if result.get("ready") is True else HTTPStatus.SERVICE_UNAVAILABLE,
                )
            if parts == ("deployment", "dispatch"):
                workload = self._workload(payload)
                result = self.orchestrator.dispatch_workload(
                    workload,
                    unavailable_profiles=self._profiles(payload.get("unavailable_profiles")),
                    excluded_profiles=self._profiles(payload.get("excluded_profiles")),
                    allow_degraded=payload.get("allow_degraded") is True,
                )
                return self._response(
                    {
                        "schema": "zyra.deployment-api-dispatch/v1",
                        "ready": result["receipt"].get("status") == "succeeded",
                        **result,
                        "fallback": False,
                    },
                    HTTPStatus.CREATED,
                )
            if (
                len(parts) == 3
                and parts[0] == "deployment"
                and parts[1] == "faults"
            ):
                result = self.orchestrator.inject_fault(
                    DeploymentProfile(parts[2]),
                    payload,
                )
                return self._response(result)
            if parts == ("deployment", "exercise"):
                task_id = self._required_text(payload, "task_id")
                run_id = self._required_text(payload, "run_id")
                result = self.orchestrator.exercise_profiles(
                    task_id=task_id,
                    run_id=run_id,
                )
                return self._response(
                    result,
                    HTTPStatus.OK if result.get("ready") is True else HTTPStatus.SERVICE_UNAVAILABLE,
                )
            return self._error(
                HTTPStatus.NOT_FOUND,
                "deployment_route_not_found",
                "Unknown deployment POST route.",
            )
        except DeploymentError as error:
            return self._deployment_error(error)
        except (TypeError, ValueError) as error:
            return self._error(
                HTTPStatus.BAD_REQUEST,
                "deployment_request_invalid",
                str(error),
            )
        except Exception:
            return self._error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "deployment_internal_error",
                "Deployment control operation failed.",
            )

    @staticmethod
    def _response(
        body: Mapping[str, Any],
        status: HTTPStatus = HTTPStatus.OK,
    ) -> DeploymentApiResponse:
        return DeploymentApiResponse(
            status=int(status),
            body=dict(body),
            headers={"Cache-Control": "no-store"},
        )

    @classmethod
    def _error(
        cls,
        status: HTTPStatus,
        code: str,
        message: str,
    ) -> DeploymentApiResponse:
        return cls._response(
            {
                "schema": "zyra.deployment-error/v1",
                "error": code,
                "message": message,
                "fallback": False,
            },
            status,
        )

    @classmethod
    def _deployment_error(cls, error: DeploymentError) -> DeploymentApiResponse:
        try:
            status = HTTPStatus(error.status)
        except ValueError:
            status = HTTPStatus.BAD_REQUEST
        return cls._response(error.to_dict(), status)

    @staticmethod
    def _strings(value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise TypeError("expected a list of strings")
        return tuple(str(item).strip() for item in value if str(item).strip())

    @classmethod
    def _profiles(cls, value: Any) -> frozenset[DeploymentProfile]:
        return frozenset(DeploymentProfile(item) for item in cls._strings(value))

    @staticmethod
    def _required_text(payload: Mapping[str, Any], key: str) -> str:
        value = str(payload.get(key) or "").strip()
        if not value:
            raise ValueError(f"{key} is required")
        return value

    @staticmethod
    def _integer(
        payload: Mapping[str, Any],
        key: str,
        *,
        minimum: int,
        maximum: int | None = None,
        default: int,
    ) -> int:
        raw = payload.get(key)
        value = default if raw in (None, "") else int(str(raw))
        if value < minimum or (maximum is not None and value > maximum):
            raise ValueError(f"{key} is outside the admitted range")
        return value

    @classmethod
    def _workload(cls, payload: Mapping[str, Any]) -> Workload:
        workload_payload = payload.get("workload", payload)
        if not isinstance(workload_payload, Mapping):
            raise TypeError("workload must be an object")
        body = dict(workload_payload)
        task_id = cls._required_text(body, "task_id")
        run_id = cls._required_text(body, "run_id")
        operation = cls._required_text(body, "operation")
        operation_payload = body.get("payload")
        if not isinstance(operation_payload, Mapping):
            raise TypeError("workload.payload must be an object")
        return Workload(
            workload_id=str(body.get("workload_id") or new_id("workload")),
            task_id=task_id,
            run_id=run_id,
            operation=operation,
            payload=dict(operation_payload),
            sensitivity=Sensitivity(str(body.get("sensitivity") or "internal")),
            complexity=cls._integer(body, "complexity", minimum=1, maximum=10, default=1),
            latency_sla_ms=cls._integer(
                body,
                "latency_sla_ms",
                minimum=1,
                maximum=3_600_000,
                default=5_000,
            ),
            cpu_units=cls._integer(body, "cpu_units", minimum=1, maximum=100, default=1),
            memory_mb=cls._integer(
                body,
                "memory_mb",
                minimum=1,
                maximum=1_048_576,
                default=64,
            ),
            required_capabilities=cls._strings(body.get("required_capabilities")),
            provider_required=body.get("provider_required") is True,
            preferred_provider=str(body.get("preferred_provider") or ""),
            preferred_model=str(body.get("preferred_model") or ""),
            checkpoint_ref=str(body.get("checkpoint_ref") or ""),
            idempotency_key=str(
                body.get("idempotency_key")
                or f"deployment-api:{task_id}:{run_id}:{operation}"
            ),
        )


_LOCK = threading.RLock()
_API: DeploymentApiFacade | None = None
_KEY = ""


def get_deployment_api() -> DeploymentApiFacade:
    from . import main as api_main

    global _API, _KEY
    key = str(api_main.PROJECT_ROOT.resolve())
    with _LOCK:
        if _API is None or _KEY != key:
            _API = DeploymentApiFacade(api_main.PROJECT_ROOT)
            _KEY = key
        return _API


def reset_deployment_api() -> None:
    global _API, _KEY
    with _LOCK:
        prior = _API
        _API = None
        _KEY = ""
    if prior is not None:
        prior.orchestrator.processes.stop_all()
