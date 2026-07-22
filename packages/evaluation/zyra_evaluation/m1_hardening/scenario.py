from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen

from .contracts import EvidencePointer, Finding, GateResult, GateStatus, Severity, audit_id, utc_now


FOUNDATION_SCENARIO_ID = "m1-foundation-query-permission-control"


class ScenarioTransportError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, payload: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.payload = dict(payload or {})


class ScenarioTransport(Protocol):
    def get(
        self,
        path: str,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, Any]]: ...

    def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, Any]]: ...


class HttpScenarioTransport:
    def __init__(self, base_url: str, *, timeout_seconds: float = 60.0) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    def get(
        self,
        path: str,
        query: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        url = self._url(path)
        if query:
            url += "?" + urlencode({key: value for key, value in query.items() if value is not None})
        return self._request("GET", url, None, headers=headers)

    def post(
        self,
        path: str,
        payload: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        return self._request("POST", self._url(path), payload, headers=headers)

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    def _request(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> tuple[int, Mapping[str, Any]]:
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **{str(key): str(value) for key, value in (headers or {}).items()},
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
                if not isinstance(decoded, Mapping):
                    raise ScenarioTransportError(response.status, "invalid_response", "response is not an object")
                return int(response.status), dict(decoded)
        except HTTPError as error:
            raw = error.read()
            try:
                decoded = json.loads(raw.decode("utf-8")) if raw else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {"error": "http_error", "message": raw.decode("utf-8", errors="replace")}
            if not isinstance(decoded, Mapping):
                decoded = {"error": "http_error", "body": decoded}
            return int(error.code), dict(decoded)
        except URLError as error:
            raise ScenarioTransportError(0, "transport_unavailable", str(error)) from error


@dataclass(frozen=True, slots=True)
class ScenarioStage:
    stage_id: str
    summary: str
    required_event_patterns: tuple[str, ...] = ()
    required_task_paths: tuple[str, ...] = ()
    required_response_paths: tuple[str, ...] = ()
    require_state_change: bool = False
    require_causation: bool = False
    allow_expected_failure: bool = False


@dataclass(slots=True)
class ScenarioStepReceipt:
    step_id: str
    method: str
    path: str
    status: int
    started_at: str
    completed_at: str
    request: Mapping[str, Any]
    response: Mapping[str, Any]
    expected_statuses: tuple[int, ...]

    @property
    def ok(self) -> bool:
        return self.status in self.expected_statuses

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "method": self.method,
            "path": self.path,
            "status": self.status,
            "ok": self.ok,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "request": dict(self.request),
            "response": dict(self.response),
            "expected_statuses": list(self.expected_statuses),
        }


@dataclass(slots=True)
class ScenarioRun:
    scenario_id: str
    run_id: str
    task_id: str
    started_at: str
    completed_at: str
    task_before: Mapping[str, Any]
    task_after: Mapping[str, Any]
    events_before: Sequence[Mapping[str, Any]]
    events_after: Sequence[Mapping[str, Any]]
    steps: list[ScenarioStepReceipt]
    disable_evidence: Mapping[str, Any]
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scenario_id": self.scenario_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "task_before": dict(self.task_before),
            "task_after": dict(self.task_after),
            "events_before": [dict(item) for item in self.events_before],
            "events_after": [dict(item) for item in self.events_after],
            "steps": [item.to_dict() for item in self.steps],
            "disable_evidence": dict(self.disable_evidence),
            "metadata": dict(self.metadata),
        }


class JsonPathInspector:
    def values(self, value: Any, path: str) -> list[Any]:
        tokens = [token for token in path.strip("$.").split(".") if token]
        current = [value]
        for token in tokens:
            next_values: list[Any] = []
            wildcard = token in {"*", "[]"} or token.endswith("[]")
            key = token[:-2] if token.endswith("[]") else token
            for item in current:
                candidate = item
                if key and key not in {"*", "[]"}:
                    if not isinstance(candidate, Mapping) or key not in candidate:
                        continue
                    candidate = candidate[key]
                if wildcard:
                    if isinstance(candidate, Mapping):
                        next_values.extend(candidate.values())
                    elif isinstance(candidate, Sequence) and not isinstance(candidate, (str, bytes, bytearray)):
                        next_values.extend(candidate)
                else:
                    next_values.append(candidate)
            current = next_values
        return current

    def present(self, value: Any, path: str) -> bool:
        return any(item not in (None, "", [], {}) for item in self.values(value, path))


class ScenarioEvidenceEvaluator:
    def __init__(self) -> None:
        self.paths = JsonPathInspector()

    def evaluate(
        self,
        scenario_id: str,
        stages: Sequence[ScenarioStage],
        *,
        task_before: Mapping[str, Any],
        task_after: Mapping[str, Any],
        events_before: Sequence[Mapping[str, Any]],
        events_after: Sequence[Mapping[str, Any]],
        responses: Mapping[str, Mapping[str, Any]],
        disable_evidence: Mapping[str, Any] | None = None,
    ) -> GateResult:
        result = GateResult(
            gate_id=f"scenario:{scenario_id}",
            status=GateStatus.NOT_RUN,
            summary="Cross-module M1 main-path scenario evidence.",
        )
        new_events = self._new_events(events_before, events_after)
        event_text = "\n".join(repr(event) for event in new_events)
        stage_metrics: list[dict[str, Any]] = []
        for stage in stages:
            missing_events = [pattern for pattern in stage.required_event_patterns if not self._matches(pattern, event_text)]
            missing_task = [path for path in stage.required_task_paths if not self.paths.present(task_after, path)]
            response_view = responses.get(stage.stage_id, {})
            missing_response = [path for path in stage.required_response_paths if not self.paths.present(response_view, path)]
            state_changed = self._state_changed(task_before, task_after)
            causation = self._causation_present(new_events)
            if missing_events:
                result.add(
                    Finding(
                        code="scenario.event_evidence_missing",
                        severity=Severity.BLOCKER,
                        summary="Scenario stage lacks required canonical event evidence.",
                        capability=stage.stage_id,
                        detail=", ".join(missing_events),
                    )
                )
            if missing_task:
                result.add(
                    Finding(
                        code="scenario.task_state_missing",
                        severity=Severity.BLOCKER,
                        summary="Scenario stage lacks required task-state projection.",
                        capability=stage.stage_id,
                        detail=", ".join(missing_task),
                    )
                )
            if missing_response:
                result.add(
                    Finding(
                        code="scenario.response_evidence_missing",
                        severity=Severity.BLOCKER,
                        summary="Scenario stage lacks required API response evidence.",
                        capability=stage.stage_id,
                        detail=", ".join(missing_response),
                    )
                )
            if stage.require_state_change and not state_changed:
                result.add(
                    Finding(
                        code="scenario.state_unchanged",
                        severity=Severity.BLOCKER,
                        summary="Scenario stage did not change real task state.",
                        capability=stage.stage_id,
                    )
                )
            if stage.require_causation and not causation:
                result.add(
                    Finding(
                        code="scenario.causation_missing",
                        severity=Severity.ERROR,
                        summary="Scenario events lack causation/request/tool correlation.",
                        capability=stage.stage_id,
                    )
                )
            stage_metrics.append(
                {
                    "stage_id": stage.stage_id,
                    "missing_event_patterns": missing_events,
                    "missing_task_paths": missing_task,
                    "missing_response_paths": missing_response,
                    "state_changed": state_changed,
                    "causation_present": causation,
                }
            )
        disable = dict(disable_evidence or {})
        if not disable:
            result.add(
                Finding(
                    code="scenario.disable_evidence_missing",
                    severity=Severity.BLOCKER,
                    summary="Scenario has no module-disable failure evidence.",
                )
            )
        else:
            baseline_ok = disable.get("baseline_ok") is True
            disabled_ok = disable.get("disabled_ok") is True
            error_code = str(disable.get("error_code") or "")
            fallback = disable.get("fallback_masked") is True
            if not baseline_ok:
                result.add(
                    Finding(
                        code="scenario.disable_baseline_failed",
                        severity=Severity.BLOCKER,
                        summary="Disable evidence has no successful baseline behavior.",
                    )
                )
            if disabled_ok or not error_code:
                result.add(
                    Finding(
                        code="scenario.disable_no_failure",
                        severity=Severity.BLOCKER,
                        summary="Disabled module did not cause an explicit failure.",
                        detail=error_code,
                    )
                )
            if fallback:
                result.add(
                    Finding(
                        code="scenario.disable_fallback_masked",
                        severity=Severity.BLOCKER,
                        summary="Disabled module was masked by a fallback owner.",
                    )
                )
        result.metrics.update(
            {
                "scenario_id": scenario_id,
                "stage_count": len(stages),
                "new_event_count": len(new_events),
                "task_state_changed": self._state_changed(task_before, task_after),
                "stage_results": stage_metrics,
                "disable_evidence": disable,
            }
        )
        result.evidence.extend(
            EvidencePointer(
                kind="scenario_event",
                location=str(event.get("event_id") or f"event[{index}]"),
                summary=str(event.get("event_type") or "runtime event"),
                revision=str(self._revision(event) or ""),
                causation_id=str(self._causation(event) or ""),
            )
            for index, event in enumerate(new_events)
        )
        return result.finish()

    @staticmethod
    def _new_events(
        before: Sequence[Mapping[str, Any]],
        after: Sequence[Mapping[str, Any]],
    ) -> list[Mapping[str, Any]]:
        before_ids = {str(item.get("event_id") or "") for item in before if item.get("event_id")}
        if before_ids:
            return [item for item in after if str(item.get("event_id") or "") not in before_ids]
        return list(after[len(before):]) if len(after) >= len(before) else list(after)

    @staticmethod
    def _state_changed(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
        return json.dumps(before, sort_keys=True, default=str) != json.dumps(after, sort_keys=True, default=str)

    @staticmethod
    def _matches(pattern: str, text: str) -> bool:
        import re

        return bool(re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE))

    @staticmethod
    def _causation_present(events: Sequence[Mapping[str, Any]]) -> bool:
        return any(ScenarioEvidenceEvaluator._causation(event) for event in events)

    @staticmethod
    def _causation(event: Mapping[str, Any]) -> Any:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        return (
            event.get("causation_id")
            or payload.get("causation_id")
            or payload.get("request_id")
            or payload.get("tool_use_id")
            or payload.get("command_id")
        )

    @staticmethod
    def _revision(event: Mapping[str, Any]) -> Any:
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        return payload.get("after_revision") or payload.get("revision") or payload.get("sequence")


class M1MainPathScenarioSuite:
    def __init__(self) -> None:
        self.evaluator = ScenarioEvidenceEvaluator()
        self._stages = self._default_stages()

    def registered_scenario_ids(self) -> tuple[str, ...]:
        return (
            FOUNDATION_SCENARIO_ID,
            "m1-query-mcp",
            "m1-skill-memory-compact",
            "m1-subagent-worker-recovery",
            "m1-api-stream-fallback",
            "m1-control-session-mutation",
        )

    def run_http_foundation(
        self,
        base_url: str,
        *,
        goal: str = "Exercise M1 query permission control hardening foundation.",
        timeout_seconds: float = 90.0,
    ) -> tuple[ScenarioRun, GateResult]:
        transport = HttpScenarioTransport(base_url, timeout_seconds=timeout_seconds)
        started = utc_now()
        steps: list[ScenarioStepReceipt] = []
        created = self._step(
            transport,
            steps,
            "create-task",
            "POST",
            "/tasks",
            {"goal": goal, "auto_run": False},
            (200, 201),
        )
        task = self._task(created)
        task_id = str(task.get("task_id") or "")
        run_id = str(task.get("run_id") or "")
        if not task_id or not run_id:
            raise ScenarioTransportError(500, "task_identity_missing", "task creation returned no task_id/run_id", created)
        before_task = dict(task)
        before_events = self._events(
            self._step(
                transport,
                steps,
                "events-before",
                "GET",
                f"/tasks/{task_id}/events",
                {},
                (200,),
            )
        )
        trace_response = self._step(
            transport,
            steps,
            "trace-artifact",
            "POST",
            f"/tasks/{task_id}/commands",
            {"text": "/export"},
            (200, 201, 202),
        )
        permission_response = self._step(
            transport,
            steps,
            "query-tool-permission",
            "POST",
            f"/tasks/{task_id}/workers/code",
            {
                "tool_plan": [
                    {
                        "tool_name": "file_write",
                        "arguments": {"path": "m1-hardening/output.txt", "content": "must not write before permission"},
                    }
                ],
                "max_turns": 1,
            },
            (202, 409),
        )
        control_response = self._step(
            transport,
            steps,
            "control-session-mutation",
            "POST",
            f"/tasks/{task_id}/commands",
            {"text": "/change preserve permission denial and add recovery evidence"},
            (200, 201, 202),
        )
        disabled_response = self._step(
            transport,
            steps,
            "disable-codeworker",
            "POST",
            f"/tasks/{task_id}/workers/code",
            {
                "disable_typescript_runtime": True,
                "query_turns": [[{"tool_name": "file_read", "arguments": {"path": "m1-hardening/output.txt"}}]],
                "max_turns": 1,
            },
            (409, 500),
        )
        task_after_payload = self._step(
            transport,
            steps,
            "task-after",
            "GET",
            f"/tasks/{task_id}",
            {},
            (200,),
        )
        events_after_payload = self._step(
            transport,
            steps,
            "events-after",
            "GET",
            f"/tasks/{task_id}/events",
            {},
            (200,),
        )
        task_after = self._task(task_after_payload)
        events_after = self._events(events_after_payload)
        disable_evidence = self._disable_evidence(permission_response, disabled_response)
        responses = {
            "query-tool-permission": permission_response,
            "control-session-mutation": control_response,
            "trace-artifact": trace_response,
        }
        gate = self.evaluator.evaluate(
            FOUNDATION_SCENARIO_ID,
            self._stages,
            task_before=before_task,
            task_after=task_after,
            events_before=before_events,
            events_after=events_after,
            responses=responses,
            disable_evidence=disable_evidence,
        )
        run = ScenarioRun(
            scenario_id=FOUNDATION_SCENARIO_ID,
            run_id=run_id,
            task_id=task_id,
            started_at=started,
            completed_at=utc_now(),
            task_before=before_task,
            task_after=task_after,
            events_before=before_events,
            events_after=events_after,
            steps=steps,
            disable_evidence=disable_evidence,
            metadata={"base_url": base_url, "gate_status": gate.status.value},
        )
        return run, gate

    def evaluate_existing_task(
        self,
        task: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
        *,
        responses: Mapping[str, Mapping[str, Any]] | None = None,
        disable_evidence: Mapping[str, Any] | None = None,
    ) -> GateResult:
        before = {
            "task_id": task.get("task_id"),
            "run_id": task.get("run_id"),
            "metadata": {},
            "artifacts": [],
        }
        return self.evaluator.evaluate(
            FOUNDATION_SCENARIO_ID,
            self._stages,
            task_before=before,
            task_after=task,
            events_before=(),
            events_after=events,
            responses=responses or self._responses_from_events(events),
            disable_evidence=disable_evidence or self._disable_from_events(events),
        )

    @staticmethod
    def _default_stages() -> tuple[ScenarioStage, ...]:
        return (
            ScenarioStage(
                stage_id="query-tool-permission",
                summary="Claude-derived query/tool loop reaches permission and fails before side effect.",
                required_event_patterns=(r"permission|query|tool",),
                required_response_paths=("worker_result.error", "worker_result.metadata.canonical_runtime_owner"),
                require_state_change=True,
                allow_expected_failure=True,
            ),
            ScenarioStage(
                stage_id="control-session-mutation",
                summary="Control command mutates the same task/session state and emits canonical evidence.",
                required_event_patterns=(r"requirement_change|command|control",),
                required_task_paths=("metadata.control_commands[]",),
                require_state_change=True,
            ),
            ScenarioStage(
                stage_id="trace-artifact",
                summary="Real export command consumes canonical task/event state and writes a task artifact.",
                required_event_patterns=(r"artifact_written|export|command",),
                required_task_paths=("artifacts[]",),
                required_response_paths=("task.artifacts[]",),
                require_state_change=True,
            ),
        )

    @staticmethod
    def _step(
        transport: ScenarioTransport,
        receipts: list[ScenarioStepReceipt],
        step_id: str,
        method: str,
        path: str,
        payload: Mapping[str, Any],
        expected_statuses: Sequence[int],
    ) -> Mapping[str, Any]:
        started = utc_now()
        if method == "GET":
            status, response = transport.get(path, payload)
        else:
            status, response = transport.post(path, payload)
        receipt = ScenarioStepReceipt(
            step_id=step_id,
            method=method,
            path=path,
            status=status,
            started_at=started,
            completed_at=utc_now(),
            request=dict(payload),
            response=dict(response),
            expected_statuses=tuple(int(item) for item in expected_statuses),
        )
        receipts.append(receipt)
        if not receipt.ok:
            raise ScenarioTransportError(
                status,
                str(response.get("error") or "unexpected_status"),
                (
                    f"{step_id} returned {status}, expected {tuple(expected_statuses)}; "
                    f"error={response.get('error') or ''}; message={response.get('message') or ''}"
                ),
                response,
            )
        return response

    @staticmethod
    def _task(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        task = payload.get("task") if isinstance(payload.get("task"), Mapping) else payload
        return dict(task)

    @staticmethod
    def _events(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        events = payload.get("events") or []
        return [dict(item) for item in events if isinstance(item, Mapping)]

    @staticmethod
    def _disable_evidence(
        baseline: Mapping[str, Any],
        disabled: Mapping[str, Any],
    ) -> dict[str, Any]:
        baseline_result = baseline.get("worker_result") if isinstance(baseline.get("worker_result"), Mapping) else baseline
        disabled_result = disabled.get("worker_result") if isinstance(disabled.get("worker_result"), Mapping) else disabled
        error = str(disabled_result.get("error") or disabled.get("error") or "")
        return {
            "probe_id": "query-engine-typescript-runtime",
            "baseline_ok": bool(
                baseline_result.get("metadata", {}).get("canonical_runtime_owner") == "typescript"
                if isinstance(baseline_result.get("metadata"), Mapping)
                else baseline_result
            ),
            "disabled_ok": bool(disabled_result.get("ok") is True),
            "error_code": error,
            "fallback_masked": bool(
                disabled_result.get("metadata", {}).get("fallback")
                if isinstance(disabled_result.get("metadata"), Mapping)
                else False
            ),
            "disabled_result": dict(disabled_result),
        }

    @staticmethod
    def _responses_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
        text = "\n".join(repr(item) for item in events).lower()
        return {
            "query-tool-permission": {
                "worker_result": {
                    "error": "permission_suspended" if "permission" in text else "",
                    "metadata": {"canonical_runtime_owner": "typescript" if "typescript" in text else ""},
                }
            },
            "control-session-mutation": {"observed": "control" in text or "requirement_change" in text},
            "trace-artifact": {"tool_result": {"ok": "artifact" in text}, "task": {"artifacts": [1] if "artifact" in text else []}},
        }

    @staticmethod
    def _disable_from_events(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        for event in events:
            text = repr(event).lower()
            if "typescript_runtime_disabled" in text or "tool_loop_foundation_disabled" in text:
                return {
                    "probe_id": "query-engine-typescript-runtime",
                    "baseline_ok": True,
                    "disabled_ok": False,
                    "error_code": "typescript_runtime_disabled",
                    "fallback_masked": False,
                }
        return {}
