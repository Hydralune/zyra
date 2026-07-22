from __future__ import annotations

import re
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .contracts import HardeningContext
from .service import AuditOptions, HardeningServiceError, M1HardeningService
from .store import ConcurrentAuditError, HardeningStoreError, ReportIntegrityError, ReportNotFound


TaskLoader = Callable[[str], Mapping[str, Any] | None]
EventLoader = Callable[[str], Sequence[Mapping[str, Any]]]


@dataclass(frozen=True, slots=True)
class HardeningApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)


class HardeningApiRequestError(ValueError):
    def __init__(self, status: int, code: str, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.details = dict(details or {})

    def response(self) -> HardeningApiResponse:
        return HardeningApiResponse(
            self.status,
            {
                "schema": "zyra.m1-hardening-api-error/v1",
                "ok": False,
                "error": self.code,
                "message": str(self),
                "details": self.details,
            },
            {"Cache-Control": "no-store"},
        )


class AuditOptionParser:
    _COMMIT = re.compile(r"^[0-9a-fA-F]{7,64}$")

    def parse(self, payload: Mapping[str, Any], *, default_baseline: str) -> AuditOptions:
        baseline = str(payload.get("baseline_commit") or default_baseline).strip()
        if not self._COMMIT.fullmatch(baseline):
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "baseline_commit_invalid",
                "baseline_commit must be a 7-64 character hexadecimal Git identity",
            )
        selected = payload.get("selected_disable_probe_ids") or ()
        if not isinstance(selected, Sequence) or isinstance(selected, (str, bytes, bytearray)):
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "disable_probe_ids_invalid",
                "selected_disable_probe_ids must be an array",
            )
        selected_ids = tuple(self._bounded_string(value, "disable probe id", maximum=160) for value in selected)
        minimum = self._integer(payload.get("minimum_effective_lines"), default=9000, minimum=0, maximum=5_000_000)
        timeout = self._number(payload.get("gate_timeout_seconds"), default=180.0, minimum=1.0, maximum=1800.0)
        lease = self._number(payload.get("audit_lease_seconds"), default=1800.0, minimum=30.0, maximum=7200.0)
        sealed_policy = payload.get("sealed_policy") or {}
        if not isinstance(sealed_policy, Mapping):
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "sealed_policy_invalid",
                "sealed_policy must be an object",
            )
        options = AuditOptions(
            baseline_commit=baseline,
            final_completion=self._boolean(payload.get("final_completion"), default=False),
            persist=self._boolean(payload.get("persist"), default=True),
            run_dynamic_graph_probes=self._boolean(payload.get("run_dynamic_graph_probes"), default=True),
            run_disable_probes=self._boolean(payload.get("run_disable_probes"), default=False),
            selected_disable_probe_ids=selected_ids,
            minimum_effective_lines=minimum,
            line_audit_head=self._bounded_string(payload.get("line_audit_head") or "HEAD", "line audit head", maximum=128),
            include_line_audit=self._boolean(payload.get("include_line_audit"), default=False),
            include_cross_cutting=self._boolean(payload.get("include_cross_cutting"), default=True),
            include_scenario=self._boolean(payload.get("include_scenario"), default=True),
            gate_timeout_seconds=timeout,
            audit_lease_seconds=lease,
            sealed_policy=dict(sealed_policy),
        )
        options.validate()
        return options

    @staticmethod
    def _boolean(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
        if isinstance(value, int) and value in {0, 1}:
            return bool(value)
        raise HardeningApiRequestError(HTTPStatus.BAD_REQUEST, "boolean_invalid", f"invalid boolean value: {value!r}")

    @staticmethod
    def _integer(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        if value is None:
            return default
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise HardeningApiRequestError(HTTPStatus.BAD_REQUEST, "integer_invalid", f"invalid integer: {value!r}") from error
        if result < minimum or result > maximum:
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "integer_out_of_range",
                f"integer must be between {minimum} and {maximum}",
            )
        return result

    @staticmethod
    def _number(value: Any, *, default: float, minimum: float, maximum: float) -> float:
        if value is None:
            return default
        try:
            result = float(value)
        except (TypeError, ValueError) as error:
            raise HardeningApiRequestError(HTTPStatus.BAD_REQUEST, "number_invalid", f"invalid number: {value!r}") from error
        if result < minimum or result > maximum:
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "number_out_of_range",
                f"number must be between {minimum} and {maximum}",
            )
        return result

    @staticmethod
    def _bounded_string(value: Any, name: str, *, maximum: int) -> str:
        result = str(value or "").strip()
        if not result:
            raise HardeningApiRequestError(HTTPStatus.BAD_REQUEST, "string_empty", f"{name} must not be empty")
        if len(result) > maximum or any(character in result for character in "\r\n\0"):
            raise HardeningApiRequestError(HTTPStatus.BAD_REQUEST, "string_invalid", f"{name} is invalid")
        return result


class M1HardeningApi:
    def __init__(self, service: M1HardeningService, *, default_baseline: str) -> None:
        self.service = service
        self.default_baseline = default_baseline
        self.options = AuditOptionParser()

    def handle_get(
        self,
        parts: Sequence[str],
        query: Mapping[str, Any],
    ) -> HardeningApiResponse | None:
        normalized = tuple(str(item) for item in parts)
        try:
            if normalized == ("hardening", "m1", "status"):
                return self._response(HTTPStatus.OK, self.service.repository_status())
            if normalized == ("hardening", "m1", "reports"):
                return self._list_reports(query)
            if len(normalized) == 4 and normalized[:3] == ("hardening", "m1", "reports"):
                report = self.service.store.load(normalized[3], verify=True)
                return self._response(HTTPStatus.OK, report)
            if normalized == ("hardening", "m1", "chain"):
                chain = self.service.store.verify_chain()
                return self._response(HTTPStatus.OK if chain["valid"] else HTTPStatus.CONFLICT, chain)
        except HardeningApiRequestError as error:
            return error.response()
        except ReportNotFound as error:
            return self._error(HTTPStatus.NOT_FOUND, "hardening_report_not_found", str(error))
        except ReportIntegrityError as error:
            return self._error(HTTPStatus.CONFLICT, "hardening_report_integrity_failed", str(error))
        except HardeningStoreError as error:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "hardening_store_failed", str(error))
        return None

    def handle_post(
        self,
        parts: Sequence[str],
        payload: Mapping[str, Any],
        *,
        task_loader: TaskLoader,
        event_loader: EventLoader,
    ) -> HardeningApiResponse | None:
        normalized = tuple(str(item) for item in parts)
        try:
            if normalized == ("hardening", "m1", "audit"):
                return self._repository_audit(payload, task_loader=task_loader, event_loader=event_loader)
            if (
                len(normalized) == 5
                and normalized[0] == "tasks"
                and normalized[2:] == ("hardening", "m1", "foundation")
            ):
                return self._task_audit(normalized[1], payload, task_loader=task_loader, event_loader=event_loader)
            if (
                len(normalized) == 5
                and normalized[:3] == ("hardening", "m1", "reports")
                and normalized[4] == "verify"
            ):
                report = self.service.store.load(normalized[3], verify=True)
                return self._response(
                    HTTPStatus.OK,
                    {
                        "schema": "zyra.m1-hardening-report-verification/v1",
                        "ok": True,
                        "report_id": report.get("report_id"),
                        "content_digest": (report.get("storage") or {}).get("content_digest"),
                    },
                )
        except HardeningApiRequestError as error:
            return error.response()
        except ConcurrentAuditError as error:
            return self._error(HTTPStatus.CONFLICT, "hardening_audit_in_progress", str(error))
        except ReportNotFound as error:
            return self._error(HTTPStatus.NOT_FOUND, "hardening_report_not_found", str(error))
        except ReportIntegrityError as error:
            return self._error(HTTPStatus.CONFLICT, "hardening_report_integrity_failed", str(error))
        except (HardeningServiceError, HardeningStoreError, ValueError) as error:
            return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "hardening_audit_failed", str(error))
        return None

    def _repository_audit(
        self,
        payload: Mapping[str, Any],
        *,
        task_loader: TaskLoader,
        event_loader: EventLoader,
    ) -> HardeningApiResponse:
        task_id = str(payload.get("task_id") or "").strip()
        if task_id:
            return self._task_audit(task_id, payload, task_loader=task_loader, event_loader=event_loader)
        options = self.options.parse(payload, default_baseline=self.default_baseline)
        if "include_scenario" not in payload:
            options = self._replace_include_scenario(options, False)
        outcome = self.service.audit(
            HardeningContext(
                project_root=self.service.root,
                workspace_root=self.service.source_workspace,
                artifact_root=self.service.artifact_root,
            ),
            options,
        )
        return self._outcome_response(outcome.to_dict(include_scenario=False), accepted=outcome.accepted)

    def _task_audit(
        self,
        task_id: str,
        payload: Mapping[str, Any],
        *,
        task_loader: TaskLoader,
        event_loader: EventLoader,
    ) -> HardeningApiResponse:
        task = task_loader(task_id)
        if task is None:
            raise HardeningApiRequestError(HTTPStatus.NOT_FOUND, "task_not_found", f"task not found: {task_id}")
        events = tuple(event_loader(task_id))
        if len(events) > 250_000:
            raise HardeningApiRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                "event_trace_too_large",
                "task trace exceeds the bounded API audit limit",
                details={"event_count": len(events), "maximum": 250_000},
            )
        options = self.options.parse(payload, default_baseline=self.default_baseline)
        responses = payload.get("scenario_responses")
        if responses is not None and not isinstance(responses, Mapping):
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "scenario_responses_invalid",
                "scenario_responses must be an object",
            )
        disable_evidence = payload.get("disable_evidence")
        if disable_evidence is not None and not isinstance(disable_evidence, Mapping):
            raise HardeningApiRequestError(
                HTTPStatus.BAD_REQUEST,
                "disable_evidence_invalid",
                "disable_evidence must be an object",
            )
        outcome = self.service.evaluate_existing_task(
            task,
            events,
            options,
            responses=responses if isinstance(responses, Mapping) else None,
            disable_evidence=disable_evidence if isinstance(disable_evidence, Mapping) else None,
        )
        return self._outcome_response(outcome.to_dict(include_scenario=False), accepted=outcome.accepted)

    def _list_reports(self, query: Mapping[str, Any]) -> HardeningApiResponse:
        limit = self.options._integer(query.get("limit"), default=100, minimum=1, maximum=1000)
        offset = self.options._integer(query.get("offset"), default=0, minimum=0, maximum=10_000_000)
        accepted_only = self.options._boolean(query.get("accepted_only"), default=False)
        records = self.service.store.list_records(
            task_id=str(query.get("task_id") or ""),
            run_id=str(query.get("run_id") or ""),
            scenario_id=str(query.get("scenario_id") or ""),
            accepted_only=accepted_only,
            baseline_commit=str(query.get("baseline_commit") or ""),
            limit=limit,
            offset=offset,
        )
        return self._response(
            HTTPStatus.OK,
            {
                "schema": "zyra.m1-hardening-report-list/v1",
                "count": len(records),
                "limit": limit,
                "offset": offset,
                "reports": [item.to_dict() for item in records],
            },
        )

    @staticmethod
    def _replace_include_scenario(options: AuditOptions, value: bool) -> AuditOptions:
        return AuditOptions(
            baseline_commit=options.baseline_commit,
            final_completion=options.final_completion,
            persist=options.persist,
            run_dynamic_graph_probes=options.run_dynamic_graph_probes,
            run_disable_probes=options.run_disable_probes,
            selected_disable_probe_ids=options.selected_disable_probe_ids,
            minimum_effective_lines=options.minimum_effective_lines,
            line_audit_head=options.line_audit_head,
            include_line_audit=options.include_line_audit,
            include_cross_cutting=options.include_cross_cutting,
            include_scenario=value,
            gate_timeout_seconds=options.gate_timeout_seconds,
            audit_lease_seconds=options.audit_lease_seconds,
            sealed_policy=options.sealed_policy,
        )

    @staticmethod
    def _outcome_response(value: Mapping[str, Any], *, accepted: bool) -> HardeningApiResponse:
        status = HTTPStatus.OK if accepted else HTTPStatus.CONFLICT
        return M1HardeningApi._response(status, value)

    @staticmethod
    def _response(status: int, body: Mapping[str, Any]) -> HardeningApiResponse:
        return HardeningApiResponse(
            int(status),
            dict(body),
            {
                "Cache-Control": "no-store, max-age=0",
                "X-Zyra-Hardening-State-Owner": "evaluation.M1HardeningService",
            },
        )

    @staticmethod
    def _error(status: int, code: str, message: str) -> HardeningApiResponse:
        return HardeningApiResponse(
            int(status),
            {
                "schema": "zyra.m1-hardening-api-error/v1",
                "ok": False,
                "error": code,
                "message": message,
            },
            {"Cache-Control": "no-store"},
        )
