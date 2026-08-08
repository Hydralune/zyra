from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping, MutableMapping, Sequence
from urllib.parse import quote

from .contracts import (
    EvidencePointer,
    Finding,
    GateResult,
    GateStatus,
    Severity,
    utc_now,
)
from .integration_contracts import (
    ActionRisk,
    AssertionEvaluator,
    DisconnectRequirement,
    ExpectedEffect,
    JsonPath,
    RequestSpec,
    ScenarioAssertion,
    ScenarioDefinition,
    ScenarioEvidence,
    ScenarioEvidenceGate,
    ScenarioKind,
    StepEvidence,
    canonical_revision,
    new_events,
    request_digest,
    stable_digest,
)
from .owner_matrix import REQUIRED_DISABLE_CAPABILITIES
from .scenario import HttpScenarioTransport, ScenarioTransport, ScenarioTransportError


@dataclass(frozen=True, slots=True)
class ScenarioRuntimeOptions:
    timeout_seconds: float = 120.0
    poll_interval_seconds: float = 0.2
    poll_timeout_seconds: float = 20.0
    execute_optional_steps: bool = True
    execute_disconnects: bool = False
    final_completion: bool = False
    actor_id: str = "m1-hardening-integration"
    environment: Mapping[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("scenario timeout must be positive")
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll interval must be positive")
        if self.poll_timeout_seconds <= 0:
            raise ValueError("poll timeout must be positive")
        if self.poll_interval_seconds > self.poll_timeout_seconds:
            raise ValueError("poll interval cannot exceed poll timeout")


class ScenarioBindingError(ValueError):
    pass


class ScenarioTemplate:
    _EXPRESSION = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", re.IGNORECASE)

    @classmethod
    def render(cls, value: Any, bindings: Mapping[str, Any]) -> Any:
        if isinstance(value, Mapping):
            return {str(key): cls.render(item, bindings) for key, item in value.items()}
        if isinstance(value, list):
            return [cls.render(item, bindings) for item in value]
        if isinstance(value, tuple):
            return tuple(cls.render(item, bindings) for item in value)
        if not isinstance(value, str):
            return value
        matches = list(cls._EXPRESSION.finditer(value))
        if not matches:
            return value
        if len(matches) == 1 and matches[0].span() == (0, len(value)):
            name = matches[0].group(1)
            if name not in bindings:
                raise ScenarioBindingError(f"missing scenario binding: {name}")
            return bindings[name]
        rendered = value
        for match in matches:
            name = match.group(1)
            if name not in bindings:
                raise ScenarioBindingError(f"missing scenario binding: {name}")
            rendered = rendered.replace(match.group(0), quote(str(bindings[name]), safe="-._~"))
        return rendered


class BindingExtractor:
    def extract(
        self,
        response: Mapping[str, Any],
        expressions: Mapping[str, str],
        existing: MutableMapping[str, Any],
    ) -> dict[str, Any]:
        added: dict[str, Any] = {}
        for name, expression in expressions.items():
            value = JsonPath.first(response, expression, default=None)
            if value is None or value == "":
                raise ScenarioBindingError(f"binding {name} had no value at {expression}")
            if name in existing and existing[name] != value:
                raise ScenarioBindingError(
                    f"binding {name} changed during one scenario: {existing[name]!r} -> {value!r}"
                )
            existing[name] = value
            added[name] = value
        return added


@dataclass(slots=True)
class ScenarioExecutionContext:
    definition: ScenarioDefinition
    transport: ScenarioTransport
    options: ScenarioRuntimeOptions
    bindings: dict[str, Any] = field(default_factory=dict)
    responses: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    steps: list[StepEvidence] = field(default_factory=list)
    events_before: list[Mapping[str, Any]] = field(default_factory=list)
    events_after: list[Mapping[str, Any]] = field(default_factory=list)
    task_before: Mapping[str, Any] = field(default_factory=dict)
    task_after: Mapping[str, Any] = field(default_factory=dict)
    artifacts: list[Mapping[str, Any]] = field(default_factory=list)
    assertions: list[Any] = field(default_factory=list)
    disconnect_evidence: list[Mapping[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)


DisconnectExecutor = Callable[[DisconnectRequirement, ScenarioExecutionContext], Mapping[str, Any]]


class ProviderFailoverProbeServer:
    """Real loopback HTTP/SSE fault source for the provider failover scenario.

    Three provider requests return an observable capacity outage.  The next
    request is a valid SSE completion.  The canonical TypeScript model stream
    therefore has to exhaust its same-route retry allowance, select a fallback
    model and recover; no result is injected into the worker runtime.
    """

    def __init__(self, *, outage_attempts: int = 3) -> None:
        if outage_attempts < 0:
            raise ValueError("outage_attempts must be non-negative")
        self.outage_attempts = int(outage_attempts)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._requests: list[dict[str, Any]] = []
        self._statuses: list[int] = []
        self.base_url = ""

    def start(self) -> "ProviderFailoverProbeServer":
        if self._server is not None:
            raise RuntimeError("provider failover probe server is already running")
        probe = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = max(0, int(self.headers.get("Content-Length", "0")))
                except ValueError:
                    length = 0
                raw = self.rfile.read(length)
                try:
                    request = json.loads(raw.decode("utf-8")) if raw else {}
                except (UnicodeDecodeError, json.JSONDecodeError):
                    request = {}
                with probe._lock:
                    attempt = len(probe._requests) + 1
                    probe._requests.append(dict(request) if isinstance(request, Mapping) else {})
                if attempt <= probe.outage_attempts:
                    body = json.dumps(
                        {
                            "error": {
                                "code": "provider_capacity_overloaded",
                                "message": "M1 integration injected a bounded provider capacity outage.",
                            }
                        }
                    ).encode("utf-8")
                    self.send_response(529)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    with probe._lock:
                        probe._statuses.append(529)
                    return
                chunks = (
                    {
                        "id": "chatcmpl-m1-provider-failover",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"role": "assistant", "content": "recovered"},
                                "finish_reason": None,
                            }
                        ],
                    },
                    {
                        "id": "chatcmpl-m1-provider-failover",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {},
                                "finish_reason": "stop",
                            }
                        ],
                    },
                )
                body = "".join(
                    f"data: {json.dumps(chunk, separators=(',', ':'))}\n\n"
                    for chunk in chunks
                )
                body += "data: [DONE]\n\n"
                encoded = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
                with probe._lock:
                    probe._statuses.append(200)

            def log_message(self, format: str, *args: Any) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="m1-provider-failover-probe",
            daemon=True,
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def receipt(self) -> Mapping[str, Any]:
        with self._lock:
            requests = list(self._requests)
            statuses = list(self._statuses)
        return {
            "schema": "zyra.m1-provider-failover-probe/v1",
            "transport": "loopback_http_sse",
            "request_count": len(requests),
            "statuses": statuses,
            "models": [str(item.get("model") or "") for item in requests],
            "configured_outage_attempts": self.outage_attempts,
            "bounded_capacity_outage_observed": (
                statuses[: self.outage_attempts] == [529] * self.outage_attempts
            ),
            "recovered_stream_observed": (
                len(statuses) >= self.outage_attempts + 1 and statuses[-1] == 200
            ),
        }


class IntegrationScenarioExecutor:
    def __init__(
        self,
        transport: ScenarioTransport,
        *,
        disconnect_executor: DisconnectExecutor | None = None,
    ) -> None:
        self.transport = transport
        self.disconnect_executor = disconnect_executor
        self.bindings = BindingExtractor()
        self.assertions = AssertionEvaluator()
        self.gate = ScenarioEvidenceGate()

    def execute(
        self,
        definition: ScenarioDefinition,
        options: ScenarioRuntimeOptions | None = None,
    ) -> tuple[ScenarioEvidence, GateResult]:
        runtime = options or ScenarioRuntimeOptions()
        runtime.validate()
        context = ScenarioExecutionContext(definition=definition, transport=self.transport, options=runtime)
        context.bindings.update(
            {
                "actor_id": runtime.actor_id,
                "scenario_id": definition.scenario_id,
                "scenario_kind": definition.kind.value,
            }
        )
        started = utc_now()
        provider_probe = (
            ProviderFailoverProbeServer().start()
            if definition.kind is ScenarioKind.STREAM_FAILOVER
            else None
        )
        if provider_probe is not None:
            context.bindings["provider_fault_base_url"] = provider_probe.base_url
        try:
            for request in definition.requests:
                if request.optional and not runtime.execute_optional_steps:
                    continue
                self._execute_request(context, request)
        finally:
            if provider_probe is not None:
                provider_probe.stop()
        self._load_canonical_views(context)
        self._evaluate_assertions(context)
        cleanup_receipt = self._release_scenario_worker_leases(context)
        if not cleanup_receipt["ok"]:
            context.limitations.append("scenario worker lease cleanup failed")
        self._execute_disconnects(context)
        evidence = ScenarioEvidence(
            scenario_id=definition.scenario_id,
            kind=definition.kind,
            run_id=str(context.bindings.get("run_id") or self._identity(context.task_after, "run_id")),
            task_id=str(context.bindings.get("task_id") or self._identity(context.task_after, "task_id")),
            started_at=started,
            completed_at=utc_now(),
            baseline_revision=self._view_revision(context.task_before, context.events_before),
            final_revision=self._view_revision(context.task_after, context.events_after),
            steps=context.steps,
            assertions=context.assertions,
            events=new_events(context.events_before, context.events_after),
            artifacts=context.artifacts,
            disconnect_evidence=context.disconnect_evidence,
            limitations=context.limitations,
            metadata={
                "response_digests": {
                    key: stable_digest(value) for key, value in sorted(context.responses.items())
                },
                "binding_names": sorted(context.bindings),
                "final_completion": runtime.final_completion,
                "worker_lease_cleanup": cleanup_receipt,
                "provider_failover_probe": (
                    dict(provider_probe.receipt()) if provider_probe is not None else {}
                ),
            },
        )
        return evidence, self.gate.evaluate(
            definition,
            evidence,
            require_disconnects=runtime.execute_disconnects or runtime.final_completion,
        )

    def _execute_request(self, context: ScenarioExecutionContext, request: RequestSpec) -> None:
        try:
            path = str(ScenarioTemplate.render(request.path, context.bindings))
            payload = ScenarioTemplate.render(dict(request.payload), context.bindings)
            headers = ScenarioTemplate.render(dict(request.headers), context.bindings)
        except ScenarioBindingError as error:
            context.steps.append(
                StepEvidence(
                    step_id=request.step_id,
                    method=request.method,
                    path=request.path,
                    status=0,
                    started_at=utc_now(),
                    completed_at=utc_now(),
                    request_digest=request_digest(request.method, request.path, request.payload),
                    response={},
                    error=str(error),
                )
            )
            if not request.optional:
                context.limitations.append(str(error))
            return
        started = utc_now()
        response: Mapping[str, Any] = {}
        status = 0
        error_text = ""
        try:
            if request.method.upper() == "GET":
                status, response = context.transport.get(path, payload, headers=headers)
            else:
                status, response = context.transport.post(path, payload, headers=headers)
            if status not in request.expected_statuses:
                error_text = f"unexpected status {status}; expected {request.expected_statuses}"
            added = self.bindings.extract(response, request.bind, context.bindings) if not error_text else {}
        except (ScenarioTransportError, ScenarioBindingError, OSError, TimeoutError) as error:
            error_text = f"{type(error).__name__}: {error}"
            added = {}
        receipt = StepEvidence(
            step_id=request.step_id,
            method=request.method.upper(),
            path=path,
            status=status,
            started_at=started,
            completed_at=utc_now(),
            request_digest=request_digest(request.method, path, payload),
            response=dict(response),
            bindings=added,
            error=error_text,
        )
        context.steps.append(receipt)
        context.responses[request.step_id] = dict(response)
        if error_text and not request.optional:
            context.limitations.append(f"{request.step_id}: {error_text}")
        self._capture_view(context, request.step_id, response)

    def _capture_view(
        self,
        context: ScenarioExecutionContext,
        step_id: str,
        response: Mapping[str, Any],
    ) -> None:
        task = response.get("task") if isinstance(response.get("task"), Mapping) else None
        events = response.get("events") if isinstance(response.get("events"), Sequence) else None
        artifacts = response.get("artifacts") if isinstance(response.get("artifacts"), Sequence) else None
        if task:
            if not context.task_before and step_id in {"create-task", "task-before"}:
                context.task_before = dict(task)
            context.task_after = dict(task)
        if events is not None:
            normalized = [dict(item) for item in events if isinstance(item, Mapping)]
            if not context.events_before and step_id == "events-before":
                context.events_before = normalized
            context.events_after = normalized
        if artifacts is not None:
            context.artifacts = [dict(item) for item in artifacts if isinstance(item, Mapping)]

    def _load_canonical_views(self, context: ScenarioExecutionContext) -> None:
        task_id = str(context.bindings.get("task_id") or "")
        if not task_id:
            context.limitations.append("scenario produced no canonical task id")
            return
        for step_id, path in (
            ("canonical-task-after", f"/tasks/{quote(task_id, safe='-._~')}"),
            ("canonical-events-after", f"/tasks/{quote(task_id, safe='-._~')}/events"),
            ("canonical-artifacts-after", f"/tasks/{quote(task_id, safe='-._~')}/artifacts"),
        ):
            if any(step.step_id == step_id for step in context.steps):
                continue
            request = RequestSpec(step_id=step_id, method="GET", path=path, expected_statuses=(200,), optional=False)
            self._execute_request(context, request)

    def _evaluate_assertions(self, context: ScenarioExecutionContext) -> None:
        event_source = {"events": new_events(context.events_before, context.events_after)}
        comparison = self._comparison_source(context)
        sources: dict[str, Any] = {
            "response": {key: value for key, value in context.responses.items()},
            "event": event_source,
            "task": context.task_after,
            "artifact": {"artifacts": context.artifacts},
            "binding": context.bindings,
            "comparison": comparison,
        }
        for assertion in context.definition.assertions:
            source = sources.get(assertion.source, {})
            context.assertions.append(self.assertions.evaluate(assertion, source))

    def _comparison_source(self, context: ScenarioExecutionContext) -> dict[str, Any]:
        before_digest = stable_digest(context.task_before)
        after_digest = stable_digest(context.task_after)
        before_context = self._context_digest(context.task_before)
        after_context = self._context_digest(context.task_after)
        new_event_values = new_events(context.events_before, context.events_after)
        event_types = [str(item.get("event_type") or item.get("type") or "") for item in new_event_values]
        return {
            "task_changed": before_digest != after_digest,
            "context_changed": before_context != after_context,
            "post_compact_context_changed": self._command_context_changed(context),
            "before_task_digest": before_digest,
            "after_task_digest": after_digest,
            "before_context_digest": before_context,
            "after_context_digest": after_context,
            "new_event_count": len(new_event_values),
            "event_types": event_types,
            "revision_advance": max(
                0,
                (canonical_revision(context.task_after) or 0) - (canonical_revision(context.task_before) or 0),
            ),
        }

    @staticmethod
    def _command_context_changed(context: ScenarioExecutionContext) -> bool:
        def data(step_id: str) -> Mapping[str, Any]:
            response = context.responses.get(step_id) or {}
            result = response.get("command_result") if isinstance(response, Mapping) else {}
            return dict(result.get("data") or {}) if isinstance(result, Mapping) else {}

        before = data("context-before")
        after = data("next-worker-context")
        return bool(before and after and stable_digest(before) != stable_digest(after))

    def _execute_disconnects(self, context: ScenarioExecutionContext) -> None:
        if not context.options.execute_disconnects:
            context.limitations.append("scenario disconnects were not executed")
            return
        if self.disconnect_executor is None:
            context.limitations.append("no real disconnect executor is configured")
            return
        for requirement in context.definition.disconnects:
            try:
                receipt = dict(self.disconnect_executor(requirement, context))
            except Exception as error:
                receipt = {
                    "probe_id": requirement.probe_id,
                    "capability": requirement.capability,
                    "status": "failed",
                    "error_code": type(error).__name__,
                    "message": str(error),
                    "expected_failure_observed": False,
                    "fallback_masked": False,
                }
            receipt.setdefault("probe_id", requirement.probe_id)
            receipt.setdefault("capability", requirement.capability)
            context.disconnect_evidence.append(receipt)

    @staticmethod
    def _release_scenario_worker_leases(
        context: ScenarioExecutionContext,
    ) -> Mapping[str, Any]:
        task_id = str(context.bindings.get("task_id") or "")
        if not task_id:
            return {
                "ok": True,
                "skipped": True,
                "reason": "scenario has no canonical task identity",
                "children": [],
                "task": {},
            }
        child_receipts: list[dict[str, Any]] = []
        seen_leases: set[str] = set()
        try:
            for response in context.responses.values():
                workers = (
                    response.get("physical_workers")
                    if isinstance(response, Mapping)
                    else ()
                )
                if not isinstance(workers, Sequence) or isinstance(
                    workers,
                    (str, bytes, bytearray),
                ):
                    continue
                for item in workers:
                    if not isinstance(item, Mapping):
                        continue
                    lease_id = str(item.get("lease_id") or "")
                    if not lease_id or lease_id in seen_leases:
                        continue
                    seen_leases.add(lease_id)
                    status, body = context.transport.post(
                        f"/tasks/{quote(task_id, safe='-._~')}/worker-pool-control",
                        {
                            "kind": "cancel",
                            "reason": "M1 integration scenario evidence captured",
                            "lease_id": lease_id,
                            "binding_id": str(
                                item.get("integration_binding_id")
                                or item.get("binding_id")
                                or ""
                            ),
                            "worker_id": str(item.get("worker_id") or ""),
                            "idempotency_key": (
                                f"m1-scenario:{context.definition.scenario_id}:"
                                f"{task_id}:{lease_id}:cleanup"
                            ),
                        },
                        headers={},
                    )
                    child_receipts.append(
                        {
                            "lease_id": lease_id,
                            "status": status,
                            "ok": status in {200, 201, 202},
                            "response_digest": stable_digest(body),
                        }
                    )
            task_status, task_body = context.transport.post(
                f"/tasks/{quote(task_id, safe='-._~')}/worker-pool-cancel",
                {
                    "reason": "M1 integration scenario evidence captured",
                    "idempotency_key": (
                        f"m1-scenario:{context.definition.scenario_id}:"
                        f"{task_id}:cleanup"
                    ),
                },
                headers={},
            )
        except (ScenarioTransportError, OSError, TimeoutError) as error:
            return {
                "ok": False,
                "skipped": False,
                "error_code": type(error).__name__,
                "message": str(error),
                "children": child_receipts,
                "task": {},
            }
        task_receipt = {
            "status": task_status,
            "ok": task_status in {200, 201, 202},
            "response_digest": stable_digest(task_body),
        }
        return {
            "ok": task_receipt["ok"]
            and all(item["ok"] for item in child_receipts),
            "skipped": False,
            "children": child_receipts,
            "task": task_receipt,
        }

    @staticmethod
    def _identity(task: Mapping[str, Any], key: str) -> str:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
        return str(task.get(key) or metadata.get(key) or "")

    @staticmethod
    def _context_digest(task: Mapping[str, Any]) -> str:
        metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
        selected = {
            "session": metadata.get("code_worker_session"),
            "context": metadata.get("context_projection"),
            "compact": metadata.get("compact_state"),
            "skill": metadata.get("skill_session_context"),
            "memory": metadata.get("memory_projection"),
            "route": metadata.get("route_projection"),
        }
        return stable_digest(selected)

    @staticmethod
    def _view_revision(
        task: Mapping[str, Any],
        events: Sequence[Mapping[str, Any]],
    ) -> int | None:
        task_revision = canonical_revision(task)
        if task_revision is not None:
            return task_revision
        revisions = [canonical_revision(event) for event in events]
        observed = [value for value in revisions if value is not None]
        # EventRecord predates an explicit numeric task revision.  Its append-
        # only canonical order is still a monotonic revision source and is more
        # truthful than reporting no movement after real task/tool/artifact
        # events were committed.
        return max(observed) if observed else len(events)


class M1IntegrationScenarioSuite:
    def __init__(self) -> None:
        definitions = default_integration_scenarios()
        self._definitions = {item.scenario_id: item for item in definitions}
        if len(self._definitions) != len(definitions):
            raise ValueError("duplicate M1 integration scenario id")

    def scenario_ids(self) -> tuple[str, ...]:
        return tuple(self._definitions)

    def definitions(self) -> tuple[ScenarioDefinition, ...]:
        return tuple(self._definitions.values())

    def definition(self, scenario_id: str) -> ScenarioDefinition:
        try:
            return self._definitions[scenario_id]
        except KeyError as error:
            raise KeyError(f"unknown M1 integration scenario: {scenario_id}") from error

    def execute_http(
        self,
        base_url: str,
        *,
        scenario_ids: Sequence[str] = (),
        options: ScenarioRuntimeOptions | None = None,
        disconnect_executor: DisconnectExecutor | None = None,
    ) -> tuple[list[ScenarioEvidence], GateResult]:
        runtime = options or ScenarioRuntimeOptions()
        selected = tuple(scenario_ids) or self.scenario_ids()
        unknown = set(selected) - set(self._definitions)
        if unknown:
            raise ValueError("unknown integration scenarios: " + ", ".join(sorted(unknown)))
        transport = HttpScenarioTransport(base_url, timeout_seconds=runtime.timeout_seconds)
        executor = IntegrationScenarioExecutor(transport, disconnect_executor=disconnect_executor)
        evidence: list[ScenarioEvidence] = []
        scenario_gates: list[GateResult] = []
        for scenario_id in selected:
            run, gate = executor.execute(self._definitions[scenario_id], runtime)
            evidence.append(run)
            scenario_gates.append(gate)
        aggregate = self._aggregate(scenario_gates, evidence, selected, final_completion=runtime.final_completion)
        return evidence, aggregate

    def evaluate_evidence(
        self,
        evidence: Sequence[ScenarioEvidence],
        *,
        final_completion: bool,
    ) -> GateResult:
        gates: list[GateResult] = []
        observed_ids: list[str] = []
        for item in evidence:
            observed_ids.append(item.scenario_id)
            definition = self._definitions.get(item.scenario_id)
            if definition is None:
                gate = GateResult(
                    gate_id=f"scenario:{item.scenario_id}",
                    status=GateStatus.BLOCKED,
                    summary="Unknown integration scenario evidence.",
                )
                gate.add(
                    Finding(
                        code="integration.scenario_unknown",
                        severity=Severity.BLOCKER,
                        summary="Evidence names a scenario outside the frozen M1 suite.",
                        detail=item.scenario_id,
                    )
                )
                gates.append(gate.finish())
            else:
                gates.append(
                    ScenarioEvidenceGate().evaluate(
                        definition,
                        item,
                        require_disconnects=final_completion,
                    )
                )
        return self._aggregate(gates, list(evidence), observed_ids, final_completion=final_completion)

    def _aggregate(
        self,
        gates: Sequence[GateResult],
        evidence: Sequence[ScenarioEvidence],
        selected: Sequence[str],
        *,
        final_completion: bool,
    ) -> GateResult:
        result = GateResult(
            gate_id="m1-integration-scenarios",
            status=GateStatus.NOT_RUN,
            summary="Six real M1 cross-module scenarios with semantic and disconnect evidence.",
        )
        observed = {item.scenario_id for item in evidence}
        required = set(self.scenario_ids())
        if final_completion:
            for scenario_id in sorted(required - observed):
                result.add(
                    Finding(
                        code="integration.required_scenario_missing",
                        severity=Severity.BLOCKER,
                        summary="Final M1 evidence omits a required integration scenario.",
                        detail=scenario_id,
                    )
                )
        by_id = {gate.gate_id.removeprefix("scenario:"): gate for gate in gates}
        for scenario_id in selected:
            gate = by_id.get(scenario_id)
            if gate is None:
                result.add(
                    Finding(
                        code="integration.scenario_gate_missing",
                        severity=Severity.BLOCKER,
                        summary="Executed scenario returned no hardening gate.",
                        detail=scenario_id,
                    )
                )
                continue
            for finding in gate.findings:
                if finding.severity.failing:
                    result.add(
                        Finding(
                            code=f"integration.child.{finding.code}",
                            severity=finding.severity,
                            summary=finding.summary,
                            detail=f"{scenario_id}: {finding.detail}",
                            location=finding.location,
                            capability=finding.capability,
                            metadata=dict(finding.metadata),
                        )
                    )
            result.evidence.extend(gate.evidence)
        run_ids = {item.run_id for item in evidence if item.run_id}
        task_ids = {item.task_id for item in evidence if item.task_id}
        result.metrics.update(
            {
                "required_count": len(required),
                "selected_count": len(selected),
                "observed_count": len(observed),
                "passed_count": sum(gate.ok for gate in gates),
                "run_count": len(run_ids),
                "task_count": len(task_ids),
                "scenario_status": {
                    gate.gate_id.removeprefix("scenario:"): gate.status.value for gate in gates
                },
                "evidence_digests": {item.scenario_id: item.digest() for item in evidence},
            }
        )
        return result.finish(default_partial=not final_completion and observed != required)


def _create_request(goal: str) -> RequestSpec:
    return RequestSpec(
        step_id="create-task",
        method="POST",
        path="/tasks",
        payload={"goal": goal, "auto_run": False},
        expected_statuses=(200, 201),
        bind={"task_id": "task.task_id", "run_id": "task.run_id"},
    )


def _baseline_requests() -> tuple[RequestSpec, ...]:
    return (
        RequestSpec(
            step_id="task-before",
            method="GET",
            path="/tasks/{{task_id}}",
            expected_statuses=(200,),
        ),
        RequestSpec(
            step_id="events-before",
            method="GET",
            path="/tasks/{{task_id}}/events",
            expected_statuses=(200,),
        ),
    )


def _query_tool_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.QUERY_TOOL.value}",
        kind=ScenarioKind.QUERY_TOOL,
        summary="Ordinary query enters the canonical session/context and executes a low-risk tool.",
        requests=(
            _create_request("Read a workspace file and export evidence through the canonical CodeWorker session."),
            *_baseline_requests(),
            RequestSpec(
                step_id="seed-workspace",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/export"},
                expected_statuses=(200, 201, 202),
            ),
            RequestSpec(
                step_id="execute-query-tool",
                method="POST",
                path="/tasks/{{task_id}}/workers/code",
                payload={
                    "tool_plan": [{"tool_name": "file_read", "arguments": {"path": "missing-is-explicit.txt"}}],
                    "max_turns": 1,
                },
                expected_statuses=(201, 202, 409),
            ),
            RequestSpec(
                step_id="export-trace",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/export"},
                expected_statuses=(200, 201, 202),
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "query-typescript-owner",
                "response",
                "execute-query-tool.worker_result.metadata.canonical_runtime_owner",
                "equals",
                "typescript",
                summary="Query/tool execution must be owned by the TypeScript runtime.",
            ),
            ScenarioAssertion(
                "query-task-mutated",
                "comparison",
                "task_changed",
                "truthy",
                summary="Query/tool scenario must mutate canonical task state.",
            ),
            ScenarioAssertion(
                "query-artifact-written",
                "artifact",
                "artifacts[*]",
                "exists",
                summary="Query/tool scenario must create a canonical artifact.",
            ),
            ScenarioAssertion(
                "query-events-produced",
                "comparison",
                "new_event_count",
                "at_least",
                2,
                summary="Query/tool scenario must emit canonical state/tool events.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="query-session",
                probe_id="disable-query-session",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="execute-query-tool",
                expected_errors=("e04_query_source_runtime_disabled", "query_session_disabled"),
            ),
            DisconnectRequirement(
                capability="tool-loop",
                probe_id="disable-tool-loop",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="execute-query-tool",
                expected_errors=("e04_tool_source_runtime_disabled", "tool_loop_foundation_disabled"),
            ),
            DisconnectRequirement(
                capability="workspace-runtime",
                probe_id="disable-workspace-runtime",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="execute-query-tool",
                expected_errors=("workspace_backend_disabled", "workspace_runtime_disabled"),
            ),
            DisconnectRequirement(
                capability="code-index",
                probe_id="disable-code-index",
                expected_effect=ExpectedEffect.MATERIAL_DIFFERENCE,
                exercise_step_id="execute-query-tool",
            ),
            DisconnectRequirement(
                capability="sandbox-gateway",
                probe_id="disable-sandbox-gateway",
                expected_effect=ExpectedEffect.MATERIAL_DIFFERENCE,
                exercise_step_id="execute-query-tool",
            ),
        ),
        required_event_families=("task", "tool", "artifact"),
        required_state_families=("task_session", "runtime_event", "workspace"),
        minimum_revision_advances=1,
    )


def _permission_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.PERMISSION.value}",
        kind=ScenarioKind.PERMISSION,
        summary="Dangerous tool execution reaches ASK/DENY policy and cannot bypass the side-effect boundary.",
        requests=(
            _create_request("Prove dangerous file mutation is denied or exactly approved without bypass."),
            *_baseline_requests(),
            RequestSpec(
                step_id="dangerous-tool",
                method="POST",
                path="/tasks/{{task_id}}/workers/code",
                payload={
                    "tool_plan": [
                        {
                            "tool_name": "file_write",
                            "arguments": {
                                "path": "m1-hardening/permission-side-effect.txt",
                                "content": "must remain absent before exact approval",
                            },
                        }
                    ],
                    "max_turns": 1,
                },
                expected_statuses=(202, 409),
            ),
            RequestSpec(
                step_id="permission-requests",
                method="GET",
                path="/permissions/requests",
                payload={"task_id": "{{task_id}}"},
                expected_statuses=(200,),
                optional=True,
            ),
            RequestSpec(
                step_id="deny-recovery-command",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/change keep the denied side effect absent and replan read-only evidence"},
                expected_statuses=(200, 201, 202),
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "permission-explicit-effect",
                "response",
                "dangerous-tool.worker_result.error",
                "matches",
                r"permission|approval|denied|suspend",
                summary="Dangerous tool must produce an explicit permission effect.",
            ),
            ScenarioAssertion(
                "permission-owner",
                "response",
                "dangerous-tool.worker_result.metadata.canonical_runtime_owner",
                "equals",
                "typescript",
                summary="Permission path must stay inside the canonical TypeScript runtime.",
            ),
            ScenarioAssertion(
                "permission-recovery-mutated",
                "comparison",
                "task_changed",
                "truthy",
                summary="Denial must lead to a real recovery/replan task mutation.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="permission-runtime",
                probe_id="disable-permission-runtime",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="dangerous-tool",
                expected_errors=("permission_source_runtime_disabled", "permission_runtime_disabled"),
            ),
        ),
        required_event_families=("permission", "tool", "requirement+change"),
        required_state_families=("permission", "task_session", "workspace"),
    )


def _mcp_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.MCP.value}",
        kind=ScenarioKind.MCP,
        summary="MCP tool, resource and prompt traverse canonical auth and elicitation boundaries.",
        requests=(
            _create_request("Audit MCP tool/resource/prompt authentication and elicitation."),
            *_baseline_requests(),
            RequestSpec(
                step_id="mcp-state",
                method="GET",
                path="/mcp",
                expected_statuses=(200,),
            ),
            RequestSpec(
                step_id="mcp-tools",
                method="GET",
                path="/mcp/tools",
                expected_statuses=(200, 409, 423, 503),
            ),
            RequestSpec(
                step_id="mcp-resources",
                method="GET",
                path="/mcp/resources",
                expected_statuses=(200, 409, 423, 503),
            ),
            RequestSpec(
                step_id="mcp-prompts",
                method="GET",
                path="/mcp/prompts",
                expected_statuses=(200, 409, 423, 503),
            ),
            RequestSpec(
                step_id="mcp-elicitations",
                method="GET",
                path="/mcp/elicitations",
                expected_statuses=(200, 409, 423, 503),
            ),
            RequestSpec(
                step_id="mcp-command",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/mcp tools", "actor_id": "{{actor_id}}"},
                expected_statuses=(200, 201, 202, 409),
            ),
            RequestSpec(
                step_id="mcp-evidence-mutation",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={
                    "text": "/change retain MCP tool resource prompt authentication and elicitation evidence"
                },
                expected_statuses=(200, 201, 202),
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "mcp-canonical-entry",
                "response",
                "mcp-state.canonical_entrypoint",
                "contains",
                "E02CapabilityCoordinator",
                summary="MCP state must come from the TypeScript E02 owner.",
            ),
            ScenarioAssertion(
                "mcp-no-python-fallback",
                "response",
                "mcp-state.python_fallback",
                "falsey",
                summary="MCP cannot fall back to a Python decision owner.",
            ),
            ScenarioAssertion(
                "mcp-command-state-change",
                "comparison",
                "task_changed",
                "truthy",
                summary="MCP prompt/control projection must affect the real task context.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="mcp-runtime",
                probe_id="disable-mcp-runtime",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="mcp-tools",
                expected_errors=("mcp_source_runtime_disabled", "mcp_runtime_disabled"),
            ),
        ),
        required_event_families=("mcp", "requirement+change"),
        required_state_families=("task_session", "permission", "runtime_event"),
    )


def _skill_memory_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.SKILL_MEMORY.value}",
        kind=ScenarioKind.SKILL_MEMORY,
        summary="Skill outcome enters procedure memory and compact restore changes subsequent context.",
        requests=(
            _create_request("Invoke a skill, persist its outcome and prove compact restore changes context."),
            *_baseline_requests(),
            RequestSpec(
                step_id="context-before",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/context"},
                expected_statuses=(200, 201, 202),
            ),
            RequestSpec(
                step_id="invoke-skill",
                method="POST",
                path="/tasks/{{task_id}}/workers/code",
                payload={
                    "invoked_skills": ["codebase-analysis"],
                    "tool_plan": [
                        {
                            "tool_name": "skill",
                            "arguments": {"name": "codebase-analysis", "arguments": {}},
                        }
                    ],
                    "max_turns": 1,
                },
                expected_statuses=(201, 202, 409),
            ),
            RequestSpec(
                step_id="ingest-memory",
                method="POST",
                path="/tasks/{{task_id}}/memory/ingest",
                payload={},
                expected_statuses=(200, 201),
            ),
            RequestSpec(
                step_id="curator-health",
                method="POST",
                path="/tasks/{{task_id}}/memory/curator",
                payload={"operation": "health"},
                expected_statuses=(200, 201, 409),
            ),
            RequestSpec(
                step_id="mine-procedure",
                method="POST",
                path="/tasks/{{task_id}}/memory/procedures/mine",
                payload={"query": "compact restore evidence"},
                expected_statuses=(200, 201, 202, 409),
                optional=True,
            ),
            RequestSpec(
                step_id="compact-memory",
                method="POST",
                path="/tasks/{{task_id}}/memory/compact",
                payload={"focus": "skill outcome and compact restore evidence", "tail_groups": 2},
                expected_statuses=(200, 201),
            ),
            RequestSpec(
                step_id="next-worker-context",
                method="POST",
                path="/tasks/{{task_id}}/commands",
                payload={"text": "/context"},
                expected_statuses=(200, 201, 202),
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "skill-invocation-bound",
                "response",
                "invoke-skill.worker_request.constraints.invoked_skills[0]",
                "equals",
                "codebase-analysis",
                summary="Skill invocation must expose canonical invocation identity.",
            ),
            ScenarioAssertion(
                "skill-memory-layer",
                "response",
                "ingest-memory.layer_counts.working",
                "at_least",
                1,
                summary="Skill outcome must enter canonical memory ingestion.",
            ),
            ScenarioAssertion(
                "skill-compact-preserved",
                "response",
                "compact-memory.compact.preserved_event_ids[*]",
                "exists",
                summary="Compact operation must retain evidence by canonical event identity.",
            ),
            ScenarioAssertion(
                "skill-context-changed",
                "comparison",
                "post_compact_context_changed",
                "truthy",
                summary="Memory/compact restore must materially change subsequent context.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="memory-retrieval",
                probe_id="disable-memory-retrieval",
                expected_effect=ExpectedEffect.MATERIAL_DIFFERENCE,
                exercise_step_id="invoke-skill",
            ),
            DisconnectRequirement(
                capability="memory-curator",
                probe_id="disable-memory-curator",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="curator-health",
                expected_errors=("memory_curator_disabled", "curator_runtime_disabled"),
            ),
            DisconnectRequirement(
                capability="skill-memory-restore",
                probe_id="disable-skill-memory-restore",
                expected_effect=ExpectedEffect.MATERIAL_DIFFERENCE,
                exercise_step_id="invoke-skill",
            ),
        ),
        required_event_families=("skill", "memory", "compact"),
        required_state_families=("memory", "skill_invocation", "task_session"),
    )


def _subagent_recovery_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.SUBAGENT_RECOVERY.value}",
        kind=ScenarioKind.SUBAGENT_RECOVERY,
        summary="Subagent/background work acquires a physical lease and failure causes recovery reroute.",
        requests=(
            # The scenario measures physical fanout, worker loss and recovery
            # rerouting.  Its recovery continuation now executes the real
            # production graph, so keep the parent goal deterministic and
            # tool-free; otherwise provider-selected shell/browser work turns
            # this recovery contract into an unrelated permission test.
            _create_request("测试，收到请回复ok"),
            *_baseline_requests(),
            RequestSpec(
                step_id="fanout-subagent",
                method="POST",
                path="/tasks/{{task_id}}/subagents/fanout",
                payload={
                    "shared_context": "Inspect recovery evidence with bounded, no-tool work.",
                    "items": [
                        {
                            "task_id": "{{task_id}}-recovery-child-a",
                            "prompt": "Inspect the worker lease receipt.",
                            "background": True,
                            "tools": ["file_read"],
                            "isolation": "workspace",
                        },
                        {
                            "task_id": "{{task_id}}-recovery-child-b",
                            "prompt": "Inspect the recovery route evidence.",
                            "background": True,
                            "physical_location": "edge",
                            "tools": ["file_read"],
                            "isolation": "workspace",
                        },
                    ],
                    "failure_policy": "collect",
                    "maximum_concurrency": 2,
                    "disable_retrieval_context": True,
                    "sealed_bounded_read_only": True,
                    "request_id": "{{task_id}}-recovery-fanout",
                    "idempotency_key": "{{task_id}}-recovery-fanout",
                },
                expected_statuses=(200, 201, 202, 409),
                optional=True,
            ),
            RequestSpec(
                step_id="inject-worker-failure",
                method="POST",
                path="/tasks/{{task_id}}/faults/inject",
                payload={
                    "kind": "worker_lost",
                    "target": {"worker_id": "m1-integration-worker"},
                    "continuation": "recovery_handoff",
                    "idempotency_key": "m1-integration-worker-lost",
                },
                expected_statuses=(200, 201, 202),
                bind={"fault_handoff": "handoff"},
            ),
            RequestSpec(
                step_id="register-successor-worker",
                method="POST",
                path="/worker-pool/workers/local/register",
                payload={"worker_id": "m1-integration-worker-successor"},
                expected_statuses=(200, 201),
            ),
            RequestSpec(
                step_id="recover-reroute",
                method="POST",
                path="/tasks/{{task_id}}/recovery/fault-handoff",
                payload={
                    "task_id": "{{task_id}}",
                    "handoff": "{{fault_handoff}}",
                    "context": {"candidate_worker_ids": ["m1-integration-worker-successor"]},
                    "apply": True,
                },
                expected_statuses=(200, 201, 202),
            ),
            RequestSpec(
                step_id="fault-state",
                method="GET",
                path="/tasks/{{task_id}}/faults",
                expected_statuses=(200,),
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "recovery-task-mutated",
                "comparison",
                "task_changed",
                "truthy",
                summary="Worker failure and recovery must mutate canonical task state.",
            ),
            ScenarioAssertion(
                "recovery-events-produced",
                "comparison",
                "new_event_count",
                "at_least",
                2,
                summary="Worker failure/recovery must produce causal events.",
            ),
            ScenarioAssertion(
                "recovery-injection-ack",
                "response",
                "inject-worker-failure.handoff.metadata.consumer",
                "equals",
                "M1-S07C.RecoveryPlanner",
                summary="Fault injection must emit a typed handoff to the production recovery owner.",
            ),
            ScenarioAssertion(
                "recovery-rerouted-worker",
                "response",
                "recover-reroute.plan.decision.selected.action",
                "equals",
                "reroute",
                summary="The recovery planner must select the worker reroute action.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="physical-worker",
                probe_id="disable-physical-worker",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="fanout-subagent",
                expected_errors=("worker_pool_integration_disabled", "physical_worker_disabled"),
            ),
            DisconnectRequirement(
                capability="edge-worker",
                probe_id="disable-edge-worker",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="fanout-subagent",
                expected_errors=("edge_connector_disabled", "edge_worker_disabled"),
            ),
            DisconnectRequirement(
                capability="checkpoint-recovery",
                probe_id="disable-checkpoint-recovery",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="recover-reroute",
                expected_errors=("recovery_runtime_disabled", "checkpoint_recovery_disabled"),
            ),
            DisconnectRequirement(
                capability="layered-route",
                probe_id="disable-layered-route",
                expected_effect=ExpectedEffect.MATERIAL_DIFFERENCE,
                exercise_step_id="recover-reroute",
            ),
            DisconnectRequirement(
                capability="graph-custody",
                probe_id="disable-graph-state-store",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="fanout-subagent",
                expected_errors=("dynamic_graph_commit_disabled", "graph_custody_disabled"),
            ),
        ),
        required_event_families=("subagent", "worker", "failure", "recovery"),
        required_state_families=("worker_lease", "recovery", "graph_topology"),
    )


def _stream_failover_scenario() -> ScenarioDefinition:
    return ScenarioDefinition(
        scenario_id=f"m1-integration-{ScenarioKind.STREAM_FAILOVER.value}",
        kind=ScenarioKind.STREAM_FAILOVER,
        summary="Stream stall or backend failure produces bounded retry/fallback/failover evidence.",
        requests=(
            _create_request("Exercise provider stream stall, bounded retry and backend failover."),
            *_baseline_requests(),
            RequestSpec(
                step_id="provider-health",
                method="GET",
                path="/providers/health",
                expected_statuses=(200,),
            ),
            RequestSpec(
                step_id="runtime-stream-before",
                method="GET",
                path="/tasks/{{task_id}}/runtime-event-stream",
                expected_statuses=(200,),
                optional=True,
            ),
            RequestSpec(
                step_id="provider-stall-worker",
                method="POST",
                path="/tasks/{{task_id}}/workers/code",
                payload={
                    "raw_input": "Exercise bounded provider capacity failures and recover on the fallback model.",
                    # This canonical request intent seeds retrieval before the
                    # HTTP provider response becomes available.  HTTP/SSE
                    # remains the owner of the executed turn.
                    "query_turns": [
                        [{"tool_name": "trace", "arguments": {"limit": 1}}]
                    ],
                    "max_turns": 1,
                    "model_transport": "http_sse",
                    "model_api_base_url": "{{provider_fault_base_url}}",
                    "model_api_timeout_seconds": 5,
                    "api_retry_max_attempts": 4,
                    "api_retry_fallback_models": ["zyra-m1-fallback-model"],
                },
                expected_statuses=(201,),
            ),
            RequestSpec(
                step_id="watchdog-ingest",
                method="POST",
                path="/tasks/{{task_id}}/faults/runtime-events",
                payload={
                    "run_id": "{{run_id}}",
                    "task_id": "{{task_id}}",
                    "phase": "owner_disconnect_probe",
                    "signal_id": "watchdog-owner-probe:{{task_id}}",
                },
                expected_statuses=(202,),
            ),
            RequestSpec(
                step_id="runtime-stream-after",
                method="GET",
                path="/tasks/{{task_id}}/runtime-event-stream",
                expected_statuses=(200,),
                optional=True,
            ),
        ),
        assertions=(
            ScenarioAssertion(
                "failover-typescript-owner",
                "response",
                "provider-stall-worker.worker_result.metadata.canonical_runtime_owner",
                "equals",
                "typescript",
                summary="Provider failover remains in the canonical TypeScript owner.",
            ),
            ScenarioAssertion(
                "retry-fallback-selected",
                "response",
                "provider-stall-worker.worker_result.metadata.api_retry_status",
                "equals",
                "fallback_selected",
                summary="The first provider outage must activate bounded retry and fallback selection.",
            ),
            ScenarioAssertion(
                "failover-model-changed",
                "response",
                "provider-stall-worker.worker_result.metadata.api_retry_final_model",
                "equals",
                "zyra-m1-fallback-model",
                summary="The recovered request must execute on the declared fallback model.",
            ),
            ScenarioAssertion(
                "failover-used",
                "response",
                "provider-stall-worker.worker_result.metadata.api_retry_fallback_used",
                "equals",
                "true",
                summary="The canonical runtime must report a material provider fallback.",
            ),
            ScenarioAssertion(
                "failover-events-produced",
                "comparison",
                "new_event_count",
                "at_least",
                1,
                summary="Stream/backend failure must leave canonical evidence.",
            ),
            ScenarioAssertion(
                "failover-task-mutated",
                "comparison",
                "task_changed",
                "truthy",
                summary="Retry/fallback/failover must change canonical task/provider state.",
            ),
        ),
        disconnects=(
            DisconnectRequirement(
                capability="provider-control-plane",
                probe_id="disable-provider-control-plane",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="provider-health",
                expected_errors=("provider_control_plane_disabled", "provider_owner_disabled"),
            ),
            DisconnectRequirement(
                capability="runtime-event-spine",
                probe_id="disable-runtime-event-spine",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="runtime-stream-after",
                expected_errors=("runtime_event_spine_disabled", "event_owner_disabled"),
            ),
            DisconnectRequirement(
                capability="watchdog",
                probe_id="disable-watchdog",
                expected_effect=ExpectedEffect.EXPLICIT_FAILURE,
                exercise_step_id="watchdog-ingest",
                expected_errors=("watchdog_runtime_disabled",),
            ),
        ),
        required_event_families=("provider", "retry", "fallback", "stream"),
        required_state_families=("provider_backend", "runtime_event", "recovery"),
    )


def default_integration_scenarios() -> tuple[ScenarioDefinition, ...]:
    return (
        _query_tool_scenario(),
        _permission_scenario(),
        _mcp_scenario(),
        _skill_memory_scenario(),
        _subagent_recovery_scenario(),
        _stream_failover_scenario(),
    )


def scenario_catalog_gate() -> GateResult:
    suite = M1IntegrationScenarioSuite()
    result = GateResult(
        gate_id="m1-integration-scenario-catalog",
        status=GateStatus.NOT_RUN,
        summary="Frozen six-scenario M1 integration execution catalog.",
    )
    kinds: set[ScenarioKind] = set()
    probe_ids: set[str] = set()
    for definition in suite.definitions():
        for issue in definition.validate():
            result.add(
                Finding(
                    code="integration.catalog_definition_invalid",
                    severity=Severity.BLOCKER,
                    summary="Scenario catalog definition is invalid.",
                    detail=f"{definition.scenario_id}: {issue}",
                )
            )
        if definition.kind in kinds:
            result.add(
                Finding(
                    code="integration.catalog_kind_duplicate",
                    severity=Severity.BLOCKER,
                    summary="Scenario catalog has duplicate canonical kind.",
                    detail=definition.kind.value,
                )
            )
        kinds.add(definition.kind)
        probe_ids.update(item.probe_id for item in definition.disconnects)
    missing = set(ScenarioKind) - kinds
    for kind in sorted(missing, key=lambda item: item.value):
        result.add(
            Finding(
                code="integration.catalog_kind_missing",
                severity=Severity.BLOCKER,
                summary="Scenario catalog omits a required M1 integration kind.",
                detail=kind.value,
            )
        )
    mapped_capabilities = {
        disconnect.capability
        for definition in suite.definitions()
        for disconnect in definition.disconnects
    }
    for capability in sorted(set(REQUIRED_DISABLE_CAPABILITIES) - mapped_capabilities):
        result.add(
            Finding(
                code="integration.catalog_owner_disconnect_missing",
                severity=Severity.BLOCKER,
                summary="Scenario catalog omits a required owner disconnect.",
                capability=capability,
            )
        )
    metrics = {
        "scenario_count": len(suite.definitions()),
        "scenario_ids": list(suite.scenario_ids()),
        "kinds": sorted(kind.value for kind in kinds),
        "disconnect_probe_ids": sorted(probe_ids),
        "disconnect_capabilities": sorted(mapped_capabilities),
        "request_count": sum(len(item.requests) for item in suite.definitions()),
        "assertion_count": sum(len(item.assertions) for item in suite.definitions()),
    }
    result.metrics.update(metrics)
    result.evidence.append(
        EvidencePointer(
            kind="integration_scenario_catalog",
            location=(
                "packages/evaluation/zyra_evaluation/m1_hardening/"
                "integration_scenarios.py::default_integration_scenarios"
            ),
            summary=(
                f"Frozen catalog contains {metrics['scenario_count']} scenarios and "
                f"{len(probe_ids)} owner-disconnect probes."
            ),
            revision=stable_digest(metrics),
            metadata={
                "scenario_ids": metrics["scenario_ids"],
                "disconnect_capability_count": len(mapped_capabilities),
            },
        )
    )
    return result.finish()
