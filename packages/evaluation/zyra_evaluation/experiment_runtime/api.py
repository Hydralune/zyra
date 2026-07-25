from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, Mapping

from .canonical import bounded_integer
from .errors import ExperimentError
from .matrix import VariantCatalog
from .metrics import MetricCatalog
from .requirements import requirement_definitions
from .runtime import ExperimentMatrixRuntime


@dataclass(frozen=True, slots=True)
class ExperimentApiResponse:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


class ExperimentApi:
    def __init__(self, runtime: ExperimentMatrixRuntime) -> None:
        self.runtime = runtime

    def route_get(
        self,
        parts: tuple[str, ...],
        query: Mapping[str, Any],
    ) -> ExperimentApiResponse | None:
        try:
            if parts == ("experiments", "registry"):
                return self._ok(
                    {
                        "schema": "zyra.experiment-registry/v1",
                        "variants": [
                            item.to_dict() for item in VariantCatalog().list()
                        ],
                        "metrics": [
                            item.to_dict() for item in MetricCatalog().list()
                        ],
                        "requirements": [
                            item.to_dict() for item in requirement_definitions()
                        ],
                        "capabilities": {
                            "baseline_matrix": True,
                            "ablation_matrix": True,
                            "raw_samples": True,
                            "p50_p95": True,
                            "dispersion": True,
                            "confidence": True,
                            "tamper_evident_bundle": True,
                            "reviewer_navigation": True,
                            "browser_connection_required": False,
                            "authenticated_provider_cli_allowed": False,
                            "external_model_request_allowed": False,
                        },
                    }
                )
            if parts == ("experiments", "runs"):
                limit = bounded_integer(
                    query.get("limit"),
                    "experiment list limit",
                    minimum=1,
                    maximum=10_000,
                    fallback=100,
                )
                offset = bounded_integer(
                    query.get("offset"),
                    "experiment list offset",
                    minimum=0,
                    maximum=2**31 - 1,
                    fallback=0,
                )
                include_archived = str(
                    query.get("include_archived") or ""
                ).casefold() in {"1", "true", "yes", "on"}
                return self._ok(
                    self.runtime.list(
                        include_archived=include_archived,
                        limit=limit,
                        offset=offset,
                    )
                )
            if len(parts) == 3 and parts[:2] == ("experiments", "runs"):
                return self._ok(self.runtime.status(parts[2]))
            if len(parts) == 4 and parts[:2] == ("experiments", "runs"):
                experiment_id = parts[2]
                resource = parts[3]
                if resource == "report":
                    return self._ok(
                        {
                            "schema": "zyra.experiment-report-response/v1",
                            "experiment_id": experiment_id,
                            "report": self.runtime.report(experiment_id),
                        }
                    )
                if resource == "samples":
                    limit = bounded_integer(
                        query.get("limit"),
                        "experiment sample limit",
                        minimum=1,
                        maximum=100_000,
                        fallback=1000,
                    )
                    offset = bounded_integer(
                        query.get("offset"),
                        "experiment sample offset",
                        minimum=0,
                        maximum=2**31 - 1,
                        fallback=0,
                    )
                    return self._ok(
                        self.runtime.raw_samples(
                            experiment_id,
                            metric=str(query.get("metric") or ""),
                            variant_id=str(query.get("variant_id") or ""),
                            cell_id=str(query.get("cell_id") or ""),
                            status=str(query.get("status") or ""),
                            limit=limit,
                            offset=offset,
                        )
                    )
                if resource == "bundle":
                    return self._ok(
                        {
                            "schema": "zyra.experiment-bundle-response/v1",
                            "experiment_id": experiment_id,
                            "bundle": self.runtime.bundle(experiment_id),
                        }
                    )
                if resource == "source":
                    status = self.runtime.status(experiment_id)
                    return self._ok(
                        {
                            "schema": "zyra.experiment-source-response/v1",
                            "experiment_id": experiment_id,
                            "source": self.runtime.store.source(experiment_id),
                            "source_admission_receipts": [
                                item
                                for item in status["receipts"]
                                if item.get("schema")
                                == "zyra.experiment-source-verification/v1"
                            ],
                        }
                    )
                if resource == "requirements":
                    report = self.runtime.report(experiment_id)
                    return self._ok(
                        {
                            "schema": "zyra.experiment-requirement-response/v1",
                            "experiment_id": experiment_id,
                            "requirements": report["requirements"],
                            "reviewer_navigation": report[
                                "reviewer_navigation"
                            ],
                        }
                    )
        except ExperimentError as error:
            return self._error(error)
        return None

    def route_post(
        self,
        parts: tuple[str, ...],
        payload: Mapping[str, Any],
        *,
        actor_id: str,
    ) -> ExperimentApiResponse | None:
        try:
            if parts == ("experiments", "runs"):
                run = self.runtime.create(
                    {
                        **dict(payload),
                        "requested_by": actor_id,
                    }
                )
                return self._response(
                    HTTPStatus.CREATED,
                    {
                        "schema": "zyra.experiment-create-response/v1",
                        "run": run.to_dict(),
                    },
                )
            if len(parts) == 4 and parts[:2] == ("experiments", "runs"):
                experiment_id = parts[2]
                operation = parts[3]
                if operation == "start":
                    wait = payload.get("wait") is True
                    timeout = payload.get("timeout_seconds")
                    run = self.runtime.start(
                        experiment_id,
                        wait=wait,
                        timeout=None if timeout is None else float(timeout),
                    )
                    return self._response(
                        HTTPStatus.OK if run.terminal else HTTPStatus.ACCEPTED,
                        {
                            "schema": "zyra.experiment-start-response/v1",
                            "run": run.to_dict(),
                            "backend_continues_after_disconnect": True,
                        },
                    )
                if operation == "verify":
                    return self._ok(
                        {
                            "schema": "zyra.experiment-verify-response/v1",
                            "verification_receipt": self.runtime.verify(
                                experiment_id
                            ),
                        }
                    )
                if operation == "cancel":
                    run = self.runtime.cancel(
                        experiment_id,
                        reason=str(payload.get("reason") or ""),
                        actor_id=actor_id,
                    )
                    return self._ok(
                        {
                            "schema": "zyra.experiment-cancel-response/v1",
                            "run": run.to_dict(),
                        }
                    )
                if operation == "archive":
                    run = self.runtime.archive(
                        experiment_id,
                        reason=str(payload.get("reason") or ""),
                    )
                    return self._ok(
                        {
                            "schema": "zyra.experiment-archive-response/v1",
                            "run": run.to_dict(),
                        }
                    )
        except ExperimentError as error:
            return self._error(error)
        except (TypeError, ValueError) as error:
            return self._response(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {
                    "schema": "zyra.experiment-error/v1",
                    "ok": False,
                    "error": "experiment_request_invalid",
                    "message": str(error),
                    "fallback": False,
                },
            )
        return None

    def _ok(self, body: Mapping[str, Any]) -> ExperimentApiResponse:
        return self._response(HTTPStatus.OK, body)

    def _error(self, error: ExperimentError) -> ExperimentApiResponse:
        return self._response(error.status, error.response())

    def _response(
        self,
        status: int | HTTPStatus,
        body: Mapping[str, Any],
    ) -> ExperimentApiResponse:
        return ExperimentApiResponse(
            status=int(status),
            body=dict(body),
            headers={
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Experiment-Owner": "python.ExperimentMatrixRuntime",
                "X-Zyra-Experiment-Fallback": "false",
            },
        )
