from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping

from .canonical import bounded_integer
from .errors import ScenarioRunnerError
from .runtime import ScenarioRunnerService


@dataclass(frozen=True, slots=True)
class ScenarioApiResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class ScenarioRunnerApi:
    def __init__(self, service: ScenarioRunnerService) -> None:
        self.service = service

    def route_get(
        self,
        parts: tuple[str, ...],
        query: Mapping[str, Any],
    ) -> ScenarioApiResponse | None:
        try:
            if parts == ("scenarios", "registry"):
                return self._ok(self.service.registry.catalog())
            if parts == ("scenarios", "runs"):
                limit = bounded_integer(
                    query.get("limit"),
                    "scenario list limit",
                    minimum=1,
                    maximum=10_000,
                    fallback=100,
                )
                offset = bounded_integer(
                    query.get("offset"),
                    "scenario list offset",
                    minimum=0,
                    maximum=2**31 - 1,
                    fallback=0,
                )
                include_archived = str(
                    query.get("include_archived") or ""
                ).casefold() in {"1", "true", "yes", "on"}
                return self._ok(
                    self.service.list(
                        include_archived=include_archived,
                        limit=limit,
                        offset=offset,
                    )
                )
            if len(parts) == 3 and parts[:2] == ("scenarios", "runs"):
                return self._ok(self.service.status(parts[2]))
            if (
                len(parts) == 4
                and parts[:2] == ("scenarios", "runs")
                and parts[3] == "evidence"
            ):
                status = self.service.status(parts[2])
                run = status["run"]
                return self._ok(
                    {
                        "schema": "zyra.scenario-evidence-response/v1",
                        "scenario_run_id": parts[2],
                        "evidence_manifest": run.get("evidence_manifest"),
                        "verification_receipt": run.get("verification_receipt"),
                        "receipts": status["receipts"],
                    }
                )
        except ScenarioRunnerError as error:
            return self._error(error)
        return None

    def route_post(
        self,
        parts: tuple[str, ...],
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> ScenarioApiResponse | None:
        try:
            if parts == ("scenarios", "runs"):
                run = self.service.create({**dict(payload), "requested_by": actor_id})
                return self._response(
                    HTTPStatus.CREATED,
                    {
                        "schema": "zyra.scenario-create-response/v1",
                        "run": run.to_dict(),
                    },
                )
            if len(parts) == 4 and parts[:2] == ("scenarios", "runs"):
                scenario_run_id = parts[2]
                operation = parts[3]
                if operation == "start":
                    wait = payload.get("wait") is True
                    timeout = payload.get("timeout_seconds")
                    run = self.service.start(
                        scenario_run_id,
                        wait=wait,
                        timeout=None if timeout is None else float(timeout),
                    )
                    return self._response(
                        HTTPStatus.OK if run.terminal else HTTPStatus.ACCEPTED,
                        {
                            "schema": "zyra.scenario-start-response/v1",
                            "run": run.to_dict(),
                            "backend_continues_after_disconnect": True,
                        },
                    )
                if operation == "cancel":
                    run = self.service.cancel(
                        scenario_run_id,
                        reason=str(payload.get("reason") or ""),
                        actor_id=actor_id,
                    )
                    return self._ok(
                        {
                            "schema": "zyra.scenario-cancel-response/v1",
                            "run": run.to_dict(),
                        }
                    )
                if operation == "archive":
                    run = self.service.archive(
                        scenario_run_id,
                        reason=str(payload.get("reason") or ""),
                    )
                    return self._ok(
                        {
                            "schema": "zyra.scenario-archive-response/v1",
                            "run": run.to_dict(),
                        }
                    )
                if operation == "verify":
                    receipt = self.service.verify(scenario_run_id)
                    return self._ok(
                        {
                            "schema": "zyra.scenario-verify-response/v1",
                            "verification_receipt": receipt,
                        }
                    )
        except ScenarioRunnerError as error:
            return self._error(error)
        except (TypeError, ValueError) as error:
            return self._response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "schema": "zyra.scenario-error/v1",
                    "ok": False,
                    "error": "scenario_request_invalid",
                    "message": str(error),
                    "fallback": False,
                },
            )
        return None

    def _ok(self, body: Mapping[str, Any]) -> ScenarioApiResponse:
        return self._response(HTTPStatus.OK, body)

    def _error(self, error: ScenarioRunnerError) -> ScenarioApiResponse:
        return self._response(error.status, error.response())

    def _response(
        self,
        status: int | HTTPStatus,
        body: Mapping[str, Any],
    ) -> ScenarioApiResponse:
        return ScenarioApiResponse(
            status=int(status),
            body=dict(body),
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Scenario-Owner": "python.ScenarioRunnerService",
                "X-Zyra-Scenario-Fallback": "false",
            },
        )
