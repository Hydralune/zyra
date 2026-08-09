from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import posixpath
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from zyra_core import AgentMessage, AgentRole, MessageIntent, to_jsonable
from zyra_runtime import JsonPermissionStore, WorkerRequest
from zyra_runtime.artifacts import LocalArtifactStore
from zyra_runtime.provider_control_plane import ProviderControlPlaneClient
from zyra_runtime.sandbox_gateway import (
    DockerCliSandboxConnector,
    DockerSandboxBackend,
    canonical_logical_path,
)
from zyra_workers import CodeWorkerRuntime
from zyra_workspace import (
    WorkspaceEditPort,
    WorkspaceManagerConfig,
    WorkspaceManagerRuntime,
)

from ..goal_contracts import direct_response_contract
from .errors import DispatchRejected


def _physical_permission_session_id(
    payload: Mapping[str, Any],
    task_id: str,
    layer_index: int,
) -> str:
    """Name the permission session one physical operator attempt owns.

    Absent a recovery continuation and recovery pass this is the historical
    id, so an ordinary dispatch keeps its exact session identity.
    """

    base = (
        f"physical:{task_id}:layer:{layer_index}:"
        f"{str(payload.get('operator_ref') or '')}"
    )
    recovery_plan_id = str(payload.get("recovery_plan_id") or "")
    if recovery_plan_id:
        recovery_plan_digest = hashlib.sha256(
            recovery_plan_id.encode("utf-8")
        ).hexdigest()[:16]
        base = f"{base}:continuation:{recovery_plan_digest}"
    recovery_pass = int(payload.get("physical_recovery_pass") or 0)
    if not recovery_pass:
        return base
    return f"{base}:recovery:{recovery_pass}"


def execute_code_worker_operator(
    *,
    payload: Mapping[str, Any],
    node_id: str,
    node_data_root: str | Path,
) -> dict[str, Any]:
    """Run the existing TypeScript CodeWorker loop inside a deployment node.

    The function is an infrastructure adapter only.  Model iteration, tool
    selection, permission decisions, observation and revision remain owned by
    ``CodeWorkerRuntime`` and its TypeScript QueryEngine.
    """

    context = _required_mapping(payload.get("code_worker_context"), "code_worker_context")
    project_root = _required_path(context.get("project_root"), "project_root")
    if project_root != Path.cwd().resolve():
        raise ValueError("physical CodeWorker project root does not match the node release root")
    artifact_root = _required_path(context.get("artifact_root"), "artifact_root")
    workspace_config = _required_mapping(
        context.get("workspace_manager"), "workspace_manager"
    )
    workspace_session_id = str(workspace_config.get("session_id") or "").strip()
    if not workspace_session_id:
        raise ValueError("workspace manager session_id is required")
    provider_constraints = _required_mapping(
        context.get("provider_constraints"), "provider_constraints"
    )
    provider_database = _required_path(
        provider_constraints.get("provider_control_plane_database_path"),
        "provider_control_plane_database_path",
    )
    if provider_database != (
        artifact_root / ".provider-control-plane" / "provider.sqlite3"
    ).resolve():
        raise ValueError("provider database is not owned by the task artifact root")

    run_id = str(payload.get("run_id") or "")
    task_id = str(payload.get("task_id") or "")
    goal = str(payload.get("goal") or "")
    layer_index = int(payload.get("layer_index") or 0)
    if not run_id or not task_id or not goal or layer_index < 1:
        raise ValueError("physical CodeWorker identity, goal or layer is incomplete")

    manager = WorkspaceManagerRuntime(
        WorkspaceManagerConfig(
            state_root=_required_path(workspace_config.get("state_root"), "workspace state root"),
            data_root=_required_path(workspace_config.get("data_root"), "workspace data root"),
            local_enabled=workspace_config.get("local_enabled") is True,
            default_backend_id=str(
                workspace_config.get("default_backend_id") or "local-default"
            ),
            lease_ttl_seconds=max(
                30, int(workspace_config.get("lease_ttl_seconds") or 1800)
            ),
            reservation_ttl_seconds=max(
                5, int(workspace_config.get("reservation_ttl_seconds") or 300)
            ),
            max_receipts=max(128, int(workspace_config.get("max_receipts") or 4096)),
        )
    )
    worker_id = f"physical-code-worker:{node_id}"
    access = manager.acquire_for_worker(
        task_id=task_id,
        session_id=workspace_session_id,
        worker_id=worker_id,
    )
    workspace_root = manager.internal_task_root(access)
    benchmark_binding = _benchmark_docker_binding(
        node_data_root=Path(node_data_root).resolve(),
        workspace_root=workspace_root,
        workspace_data_root=_required_path(
            workspace_config.get("data_root"), "workspace data root"
        ),
    )
    benchmark_mirror: _BenchmarkWorkspaceMirror | None = None
    if benchmark_binding is not None:
        _pull_benchmark_workspace(benchmark_binding, workspace_root)
    before = _workspace_manifest(workspace_root)
    edit_port_arguments = {
        "worker_id": worker_id,
        "run_id": run_id,
        "task_id": task_id,
        "node_id": node_id,
        "artifact_store": LocalArtifactStore(artifact_root),
    }
    if benchmark_binding is not None:
        benchmark_mirror = _BenchmarkWorkspaceMirror(
            benchmark_binding,
            workspace_root,
            synced_manifest=before,
        )
        edit_port = _BenchmarkWorkspaceEditPort(
            manager,
            access,
            benchmark_mirror=benchmark_mirror,
            **edit_port_arguments,
        )
    else:
        edit_port = WorkspaceEditPort(
            manager,
            access,
            **edit_port_arguments,
        )

    permission_session_id = _physical_permission_session_id(
        payload,
        task_id,
        layer_index,
    )
    max_turns, runtime_timeout_seconds = _code_worker_reasoning_budget(
        context,
        benchmark_execution=benchmark_binding is not None,
    )
    constraints = {
        **dict(provider_constraints),
        "model_transport": "http_sse",
        "model_name": str(
            context.get("model_id")
            or provider_constraints.get("model_name")
            or payload.get("model")
            or ""
        ),
        "permission_mode": "acceptEdits",
        "permission_interactive": False,
        "permission_headless": True,
        # Permission-session custody is keyed by this id and its record outlives
        # a node restart, while the token proving ownership is only ever handed
        # back inside the dispatch response.  A node lost mid-dispatch therefore
        # spends its session id for good, so a recovery attempt owns a distinct
        # one rather than failing closed on a token nobody holds.
        "session_id": permission_session_id,
        "max_turns": max_turns,
        # This deadline governs the whole multi-turn reasoning loop and stays
        # below the physical dispatch transport budget. Official benchmark
        # containers receive a larger, still-bounded allowance below.
        "typescript_runtime_timeout_seconds": runtime_timeout_seconds,
        "tool_result_budget_chars": 120_000,
        "query_context_budget_chars": 128_000,
        "model_output_token_limit": max(
            512, min(32_768, int(context.get("model_output_token_limit") or 8192))
        ),
        "disable_retrieval_context": True,
        "physical_dispatch_task": True,
        "physical_dispatch_goal_digest": _digest(goal),
        "synthetic_turns_forbidden": True,
    }
    if benchmark_binding is not None:
        constraints["benchmark_physical_dispatch"] = True
        constraints["e02PermissionPolicy"] = _benchmark_permission_policy(
            session_id=permission_session_id,
            workspace_root=workspace_root,
            container_ref_digest=str(benchmark_binding["container_ref_digest"]),
        )
    execution_prompt = _execution_prompt(
        goal,
        delivery_contract=(
            payload.get("delivery_contract")
            if isinstance(payload.get("delivery_contract"), Mapping)
            else {}
        ),
        goal_contract=(
            payload.get("goal_contract")
            if isinstance(payload.get("goal_contract"), Mapping)
            else {}
        ),
    )
    if benchmark_binding is not None:
        execution_prompt = (
            f"{execution_prompt}\n\n"
            "OFFICIAL BENCHMARK ENVIRONMENT: The shell tool is physically bound to "
            "the canonical external task container. Use shell commands for repository "
            "inspection, Git operations, tests, and delivery. File tools and shell "
            "commands share one live, ordered view of that container workspace. Each "
            "shell call must be one executable command without redirects, pipes, &&, "
            "||, or command substitution; use file_write/file_read for file contents. "
            "Complete the task in the environment; do not merely describe what should "
            "be done."
        )
    request = WorkerRequest(
        run_id=run_id,
        task_id=task_id,
        node_id=node_id,
        worker_name="CodeWorkerRuntime",
        messages=[
            AgentMessage(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                sender_role=AgentRole.USER,
                receiver_role=AgentRole.WORKER,
                intent=MessageIntent.REQUEST,
                content=execution_prompt,
                metadata={
                    "source": "physical-dispatch",
                    "goal_digest": _digest(goal),
                    "workspace_owner": "WorkspaceManagerRuntime",
                },
            )
        ],
        constraints=constraints,
        metadata={
            "origin": "phase2-physical-operator",
            "physical_node_id": node_id,
            "provider_route_id": str(
                provider_constraints.get("provider_route_id") or ""
            ),
        },
    )
    sandbox_gateway_state_root = (
        Path(node_data_root).resolve()
        / "code-worker-sandbox"
        / _safe_id(task_id)
    )
    runtime_services: dict[str, Any] = {
        "workspace_edit_port": edit_port,
        "workspace_gateway_required": True,
        "sandbox_gateway_state_root": sandbox_gateway_state_root,
    }
    if benchmark_binding is not None:
        assert benchmark_mirror is not None
        runtime_services["sandbox_gateway_backend"] = DockerSandboxBackend(
            sandbox_gateway_state_root / "backend",
            _BenchmarkDockerCliSandboxConnector(
                container=str(benchmark_binding["container"]),
                workdir=str(benchmark_binding["workdir"]),
                docker_executable=str(benchmark_binding["docker_executable"]),
                benchmark_mirror=benchmark_mirror,
            ),
        )
    runtime = CodeWorkerRuntime(
        project_root=project_root,
        workspace_root=workspace_root,
        artifact_root=artifact_root,
        permission_store=JsonPermissionStore(
            Path(node_data_root).resolve()
            / "code-worker-permissions"
            / f"{_safe_id(task_id)}.json"
        ),
        permission_state_path=(
            Path(node_data_root).resolve()
            / "code-worker-permission-state"
            / f"{_safe_id(task_id)}.json"
        ),
        permission_accept_edits_available=True,
        runtime_services=runtime_services,
    )
    run = runtime.run(request)
    runtime_events = [to_jsonable(item) for item in run.event_records]
    current_access = edit_port.current_access()
    workspace_root = manager.internal_task_root(current_access)
    benchmark_sync: dict[str, Any] | None = None
    if benchmark_binding is not None:
        assert benchmark_mirror is not None
        benchmark_mirror.push_to_container()
        benchmark_mirror.pull_from_container()
        benchmark_sync = benchmark_mirror.report()
    after = _workspace_manifest(workspace_root)
    workspace_delta = _workspace_delta(before, after)
    evidence = dict(run.execution_evidence)
    if benchmark_binding is not None:
        evidence["benchmark_environment"] = {
            "schema": "zyra.benchmark-docker-binding/v1",
            "backend_id": "zyra.docker-sandbox.v1",
            "container_ref_digest": str(benchmark_binding["container_ref_digest"]),
            "container_workdir": str(benchmark_binding["workdir"]),
            "initial_pull_verified": True,
            "final_pull_verified": True,
            "live_bidirectional_sync_verified": True,
            "host_file_delta_push": dict(benchmark_sync or {}),
            "container_lifecycle_owner": "external-harness",
        }
    if not run.worker_result.ok:
        provider_failure = _provider_failure_summary(
            run.worker_result.metadata
        )
        provider_failed_before_output = bool(
            provider_failure
            and provider_failure.get("output_observed") is False
        )
        raise DispatchRejected(
            (
                "node_provider_failure"
                if provider_failed_before_output
                else "node_code_worker_failed"
            ),
            "canonical TypeScript CodeWorker failed",
            operation="phase2-operator-execution",
            profile="cloud",
            retryable=provider_failed_before_output,
            details={
                "worker_error": str(
                    run.worker_result.error or "unknown"
                )[:200],
                "worker_summary": str(run.worker_result.summary)[:500],
                # The runtime-level message carries the child's stderr, which is
                # the only place a stall or crash inside the TypeScript runtime
                # explains itself.  Without it the node reports a bare code.
                "worker_error_message": str(
                    (run.worker_result.metadata or {}).get(
                        "typescript_runtime_error_message"
                    )
                    or ""
                )[:2000],
                "provider_failure": provider_failure,
                "provider_called": evidence.get("provider_called") is True,
                "tool_call_count": int(evidence.get("tool_call_count") or 0),
            },
        )
    if evidence.get("provider_called") is not True:
        raise RuntimeError(
            "canonical TypeScript CodeWorker completed without a real provider response"
        )
    if not str(evidence.get("final_text") or "").strip() and not any(
        workspace_delta[name] for name in ("created", "modified", "deleted")
    ):
        raise RuntimeError(
            "canonical TypeScript CodeWorker produced neither a final response nor a workspace mutation"
        )

    provider_call = _provider_evidence(
        project_root=project_root,
        database_path=provider_database,
        execution_evidence=evidence,
        runtime_events=runtime_events,
        task_payload_digest=_digest(dict(payload)),
        goal_digest=_digest(goal),
        expected_initial_prompt_digest=_digest(execution_prompt),
    )
    if provider_call.get("task_execution_verified") is not True:
        raise RuntimeError("provider evidence is not bound to the physical task execution")
    final_text, final_response_projection = _governed_final_response(
        goal=goal,
        provider_text=str(evidence.get("final_text") or ""),
        goal_contract=(
            payload.get("goal_contract")
            if isinstance(payload.get("goal_contract"), Mapping)
            else None
        ),
    )
    evidence["final_response_projection"] = final_response_projection
    public_runtime_events = _public_runtime_events(runtime_events)
    return {
        "schema": "zyra.physical-code-worker-execution/v1",
        "runtime_worker": "CodeWorkerRuntime",
        "gateway": "DeploymentNodeRuntime",
        "canonical_runtime_owner": "typescript",
        "python_runtime_role": "physical-process-durability-side-effect-host",
        "worker_result": to_jsonable(run.worker_result),
        "runtime_events": public_runtime_events,
        "runtime_artifacts": [to_jsonable(item) for item in run.worker_result.artifacts],
        "execution_evidence": evidence,
        "provider_call": provider_call,
        "workspace": {
            "workspace_id": current_access.workspace_id,
            "owner_epoch": current_access.owner_epoch,
            "lease_id": current_access.lease_id,
            "physical_location_redacted": True,
            "external_benchmark_bound": benchmark_binding is not None,
            "external_container_ref_digest": (
                str(benchmark_binding["container_ref_digest"])
                if benchmark_binding is not None
                else ""
            ),
        },
        "workspace_delta": workspace_delta,
        "final_text": final_text,
    }


def _code_worker_reasoning_budget(
    context: Mapping[str, Any],
    *,
    benchmark_execution: bool,
) -> tuple[int, float]:
    """Resolve one bounded model-loop budget without relaxing production defaults.

    Terminal-Bench tasks have an official 900-second agent window. The normal
    twelve-turn/600-second production allowance proved too small for a genuine
    reverse-engineering task, so the externally verified Docker path gets up to
    twenty-four turns and 720 seconds. This remains below the 780-second
    physical-dispatch transport deadline and leaves Harbor time to run its
    independent verifier.
    """

    if benchmark_execution:
        return 24, 720.0
    return (
        max(2, min(24, int(context.get("max_turns") or 12))),
        max(
            60.0,
            min(600.0, float(context.get("reasoning_timeout_seconds") or 120.0)),
        ),
    )


_DROPPED_PUBLIC_EVENT_PHASES = frozenset(
    {
        "message_delta",
        "model_stream_frame",
    }
)


def _public_runtime_events(
    runtime_events: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project private model-loop events into low-entropy public evidence.

    Provider prompt binding is verified from the private in-process events
    before this projection.  Public task/event storage retains lifecycle,
    request commitments, aggregate stream reports, tool custody and result
    commitments, but never token deltas, model thinking, prompts, continuation
    messages or raw tool output.
    """

    projected: list[dict[str, Any]] = []
    for raw_event in runtime_events:
        event = dict(to_jsonable(raw_event))
        payload = dict(event.get("payload") or {})
        public_payload = _public_session_projection(payload)
        if public_payload is None:
            continue
        payload = public_payload
        drop_event = False
        for session_key in ("query_session", "typescript_runtime"):
            session = payload.get(session_key)
            if not isinstance(session, Mapping):
                continue
            public_session = _public_session_projection(session)
            if public_session is None:
                drop_event = True
                break
            payload[session_key] = public_session
        if drop_event:
            continue
        worker_result = payload.pop("worker_result", None)
        if worker_result is not None:
            worker_mapping = (
                dict(worker_result)
                if isinstance(worker_result, Mapping)
                else {}
            )
            payload["worker_result_commitment"] = {
                "schema": "zyra.public-worker-result-commitment/v1",
                "request_id": str(worker_mapping.get("request_id") or ""),
                "ok": worker_mapping.get("ok") is True,
                "artifact_count": len(
                    worker_mapping.get("artifacts")
                    if isinstance(worker_mapping.get("artifacts"), list)
                    else ()
                ),
                "result_digest": _digest(worker_result),
                "content_persisted": False,
            }
        event["payload"] = payload
        projected.append(event)
    return projected


def _provider_failure_summary(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only bounded, non-secret provider failure diagnostics."""

    raw = metadata.get("provider_failure")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"kind": "unparseable_provider_failure"}
    if not isinstance(value, Mapping):
        return {"kind": "invalid_provider_failure_shape"}
    detail = value.get("detail")
    detail_keys = (
        sorted(str(key) for key in detail)[:32]
        if isinstance(detail, Mapping)
        else []
    )
    return {
        "layer": str(value.get("layer") or "")[:80],
        "kind": str(value.get("kind") or "")[:120],
        "message": str(value.get("message") or "")[:500],
        "retryable": value.get("retryable") is True,
        "recovery_intent": str(value.get("recoveryIntent") or "")[:120],
        "http_status": int(value.get("httpStatus") or 0),
        "provider_id": str(value.get("providerId") or "")[:120],
        "model_id": str(value.get("modelId") or "")[:160],
        "bytes_sent": max(0, int(value.get("bytesSent") or 0)),
        "bytes_received": max(0, int(value.get("bytesReceived") or 0)),
        "output_observed": value.get("outputObserved") is True,
        "retry_after_ms": max(
            0,
            int(value.get("retryAfterMilliseconds") or 0),
        ),
        "detail_keys": detail_keys,
    }


def _public_session_projection(
    session: Mapping[str, Any],
) -> dict[str, Any] | None:
    public_session = dict(session)
    phase = str(public_session.get("phase") or "")
    if phase in _DROPPED_PUBLIC_EVENT_PHASES:
        return None
    _commit_private_field(
        public_session,
        "user_content",
        digest_name="user_content_digest",
    )
    _commit_private_field(
        public_session,
        "nudge_message",
        digest_name="nudge_message_digest",
    )
    _commit_private_field(
        public_session,
        "messages",
        digest_name="messages_digest",
        count_name="message_count",
    )
    tool_result = public_session.pop("tool_result", None)
    if tool_result is not None:
        tool_mapping = (
            dict(tool_result) if isinstance(tool_result, Mapping) else {}
        )
        public_session["tool_result_commitment"] = {
            "schema": "zyra.public-tool-result-commitment/v1",
            "tool_call_id": str(
                tool_mapping.get("tool_call_id")
                or public_session.get("tool_call_id")
                or ""
            ),
            "ok": tool_mapping.get("ok") is True,
            "artifact_count": len(
                tool_mapping.get("artifacts")
                if isinstance(tool_mapping.get("artifacts"), list)
                else ()
            ),
            "result_digest": _digest(tool_result),
            "content_persisted": False,
        }
    return public_session


def _commit_private_field(
    payload: dict[str, Any],
    field: str,
    *,
    digest_name: str,
    count_name: str | None = None,
) -> None:
    if field not in payload:
        return
    value = payload.pop(field)
    payload[digest_name] = _digest(value)
    if count_name is not None:
        payload[count_name] = len(value) if isinstance(value, list) else 0


def _execution_prompt(
    goal: str,
    *,
    delivery_contract: Mapping[str, Any],
    goal_contract: Mapping[str, Any],
) -> str:
    requirements: list[str] = []
    expected_response = str(goal_contract.get("expected_response") or "")
    if expected_response:
        requirements.append(
            "Your entire final response must be exactly this text, with no "
            f"prefix, suffix, quotes, or Markdown: {expected_response}"
        )
    for path in delivery_contract.get("required_paths") or ():
        if str(path):
            requirements.append(
                f"The governed workspace must contain this file: {path}"
            )
    expected_contents = delivery_contract.get("expected_file_contents")
    if isinstance(expected_contents, Mapping):
        for path, specification in expected_contents.items():
            if not isinstance(specification, Mapping):
                continue
            expected_text = str(specification.get("expected_text") or "")
            requirements.append(
                f"The exact text required in {path} is: {expected_text}"
            )
    contract_text = (
        "\n\nDELIVERY REQUIREMENTS:\n- " + "\n- ".join(requirements)
        if requirements
        else ""
    )
    return (
        "Complete the following user goal in the governed workspace. Use the "
        "available file or shell tools whenever the goal requires a concrete "
        "workspace change. Inspect tool results, correct failures, and do not "
        "claim completion unless the requested deliverable actually exists. "
        "After verification, return a concise final response.\n\nUSER GOAL:\n"
        + goal
        + contract_text
    )


def _governed_final_response(
    *,
    goal: str,
    provider_text: str,
    goal_contract: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """Render an explicit exact-response contract at the delivery boundary.

    The provider still performs the real, request-bound reasoning call.  When
    the user has supplied a bounded literal response contract, however, the
    delivery layer must not let stochastic punctuation or explanatory prose
    violate it.  Projection is allowed only when the supplied projection is
    exactly the contract independently compiled from the original goal.  Raw
    model text remains private; the receipt retains only its digest.
    """

    observed = str(provider_text or "").strip()
    compiled = direct_response_contract(goal)
    contract_bound = bool(
        compiled is not None
        and goal_contract is not None
        and dict(goal_contract) == compiled.to_dict()
    )
    if not contract_bound or compiled is None:
        return observed, {
            "schema": "zyra.governed-final-response/v1",
            "applicable": False,
            "contract_bound": contract_bound,
            "provider_response_digest": _digest(observed),
            "projected": False,
        }
    expected = compiled.expected_response
    provider_exact = observed == expected
    return expected, {
        "schema": "zyra.governed-final-response/v1",
        "applicable": True,
        "contract_bound": True,
        "match_mode": compiled.match_mode,
        "expected_response_digest": _digest(expected),
        "provider_response_digest": _digest(observed),
        "provider_exact": provider_exact,
        "projected": not provider_exact,
        "projection_authority": "compiled-explicit-direct-response-contract",
    }


def _provider_evidence(
    *,
    project_root: Path,
    database_path: Path,
    execution_evidence: Mapping[str, Any],
    runtime_events: list[Any],
    task_payload_digest: str,
    goal_digest: str,
    expected_initial_prompt_digest: str,
) -> dict[str, Any]:
    raw_calls = execution_evidence.get("provider_calls")
    calls = [
        dict(item)
        for item in (raw_calls if isinstance(raw_calls, list) else ())
        if isinstance(item, Mapping)
        and item.get("ok") is True
        and str(item.get("request_id") or "")
        and str(item.get("transport") or "")
        in {"provider_control_plane", "http_sse"}
        and str(item.get("provider_id") or "") not in {"", "local", "zyra-sim"}
    ]
    if not calls:
        return {"task_execution_verified": False, "provider_called": False}

    prompt_bindings = _provider_prompt_bindings(
        runtime_events=runtime_events,
        expected_initial_prompt_digest=expected_initial_prompt_digest,
    )

    enriched: list[dict[str, Any]] = []
    with ProviderControlPlaneClient(
        project_root=project_root,
        database_path=database_path,
    ) as client:
        for call in calls:
            dispatch_id = str(call["request_id"])
            attempts = [
                dict(item)
                for item in (
                    client.process.request(
                        "dispatch.attempts", {"dispatchId": dispatch_id}
                    )
                    or ()
                )
                if isinstance(item, Mapping)
            ]
            succeeded = [
                item
                for item in attempts
                if str(item.get("outcome") or "") == "succeeded"
            ]
            if not succeeded:
                continue
            final_attempt = succeeded[-1]
            prompt_binding = dict(prompt_bindings.get(dispatch_id) or {})
            provider_request_digest = str(
                final_attempt.get("requestDigest") or ""
            )
            route_id = str(call.get("route_id") or "")
            route = client.routing.get(route_id) if route_id else {}
            provider_id = str(call.get("provider_id") or route.get("providerId") or "")
            model_id = str(call.get("model_id") or route.get("modelId") or "")
            provider = client.catalog.provider(provider_id)
            model = client.catalog.model(provider_id, model_id)
            credential_id = str(route.get("credentialId") or "")
            credential = client.credentials.get(credential_id) if credential_id else {}
            usage = dict(call.get("usage") or {})
            usage["total_tokens"] = max(
                int(usage.get("total_tokens") or 0),
                int(usage.get("input_tokens") or 0)
                + int(usage.get("output_tokens") or 0),
            )
            pricing = next(
                (
                    dict(item)
                    for item in model.get("pricing") or ()
                    if isinstance(item, Mapping)
                ),
                {},
            )
            cost = _usage_cost(usage, pricing)
            model_metadata = dict(model.get("metadata") or {})
            currency = str(pricing.get("currency") or "")
            normalized_cost = _normalized_usd_cost(usage, model_metadata, currency, cost)
            started = int(final_attempt.get("startedAt") or 0)
            completed = int(final_attempt.get("completedAt") or 0)
            enriched.append(
                {
                    "request_id": dispatch_id,
                    "provider_attempt_id": str(final_attempt.get("attemptId") or ""),
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "route_id": str(route.get("routeId") or route_id),
                    "http_status": int(final_attempt.get("httpStatus") or 0),
                    "latency_ms": max(0, completed - started),
                    "usage": usage,
                    "cost_amount": cost,
                    "cost_currency": currency,
                    "cost_usd": normalized_cost,
                    "cost_usd_estimate_kind": (
                        "provider_catalog_native"
                        if currency.upper() == "USD"
                        else "conservative_native-currency-as-usd-upper-bound"
                    ),
                    "pricing_source_ref": str(
                        model_metadata.get("normalized_pricing_source")
                        or model_metadata.get("pricing_reference")
                        or f"provider-catalog://{provider_id}/{model_id}"
                    ),
                    "credential_ref": str(credential.get("secretRef") or ""),
                    "credential_material_persisted": False,
                    "endpoint": str(provider.get("baseUrl") or "")
                    + str(model.get("endpointPath") or ""),
                    "attempt_count": len(attempts),
                    "request_bytes": sum(int(item.get("requestBytes") or 0) for item in attempts),
                    "response_bytes": sum(int(item.get("responseBytes") or 0) for item in attempts),
                    "provider_request_digest": provider_request_digest,
                    "runtime_provider_request_digest": str(
                        prompt_binding.get("provider_request_digest") or ""
                    ),
                    "provider_request_digest_verified": bool(
                        provider_request_digest
                        and provider_request_digest
                        == str(
                            prompt_binding.get("provider_request_digest") or ""
                        )
                    ),
                    "prompt_messages_digest": str(
                        prompt_binding.get("messages_digest")
                        or ""
                    ),
                    "prompt_goal_bound": (
                        prompt_binding.get("goal_present") is True
                    ),
                }
            )
    if len(enriched) != len(calls):
        return {
            "task_execution_verified": False,
            "provider_called": bool(enriched),
            "calls": enriched,
        }
    usage = {
        name: sum(int(dict(item.get("usage") or {}).get(name) or 0) for item in enriched)
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "server_tool_use_tokens",
            "total_tokens",
        )
    }
    last = enriched[-1]
    external_request = all(
        _is_external_provider_endpoint(str(item.get("endpoint") or ""))
        for item in enriched
    )
    prompt_goal_bound = bool(
        enriched
        and all(
            item.get("prompt_goal_bound") is True
            and item.get("provider_request_digest_verified") is True
            and str(item.get("prompt_messages_digest") or "")
            and str(item.get("provider_request_digest") or "")
            for item in enriched
        )
    )
    return {
        "schema": "zyra.task-provider-execution-evidence/v1",
        **last,
        "usage": usage,
        "latency_ms": sum(int(item.get("latency_ms") or 0) for item in enriched),
        "cost_usd": round(sum(float(item.get("cost_usd") or 0.0) for item in enriched), 12),
        "cost_amount": round(sum(float(item.get("cost_amount") or 0.0) for item in enriched), 12),
        "calls": enriched,
        "provider_called": True,
        "live": external_request,
        "external_model_request": external_request,
        "simulated": False,
        "semantic_only": False,
        "task_execution_verified": prompt_goal_bound,
        "prompt_goal_bound": prompt_goal_bound,
        "goal_digest": goal_digest,
        "payload_digest": task_payload_digest,
        "workload_operation": "phase2-operator-execution",
        "synthetic_usage": False,
    }


def _provider_prompt_bindings(
    *,
    runtime_events: list[Any],
    expected_initial_prompt_digest: str,
) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for event in runtime_events:
        if not isinstance(event, Mapping):
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        query = payload.get("query_session")
        if not isinstance(query, Mapping):
            continue
        phase = str(query.get("phase") or "")
        if phase == "model_request_prepared":
            request = query.get("provider_request")
            if not isinstance(request, Mapping):
                continue
            request_id = str(request.get("request_id") or "").strip()
            messages_digest = str(request.get("messages_digest") or "").strip()
            initial_prompt_digest = str(
                request.get("initial_user_message_digest") or ""
            ).strip()
            if not request_id or not messages_digest or not initial_prompt_digest:
                continue
            bindings.setdefault(request_id, {}).update(
                {
                    "messages_digest": messages_digest,
                    "goal_present": bool(
                        expected_initial_prompt_digest
                        and initial_prompt_digest == expected_initial_prompt_digest
                    ),
                }
            )
        elif phase == "model_stream_report":
            report = query.get("model_stream")
            if not isinstance(report, Mapping) or report.get("ok") is not True:
                continue
            request_id = str(report.get("request_id") or "").strip()
            provider_request_digest = str(
                report.get("provider_request_digest") or ""
            ).strip()
            if not request_id or not provider_request_digest:
                continue
            bindings.setdefault(request_id, {}).update(
                {"provider_request_digest": provider_request_digest}
            )
    return bindings


def _usage_cost(usage: Mapping[str, Any], pricing: Mapping[str, Any]) -> float:
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
    regular_input = max(0, input_tokens - cached_tokens)
    return round(
        (
            regular_input * float(pricing.get("inputPerMillion") or 0)
            + cached_tokens * float(pricing.get("cachedInputPerMillion") or 0)
            + output_tokens * float(pricing.get("outputPerMillion") or 0)
        )
        / 1_000_000,
        12,
    )


def _normalized_usd_cost(
    usage: Mapping[str, Any],
    metadata: Mapping[str, Any],
    currency: str,
    native_cost: float,
) -> float:
    if currency.upper() == "USD":
        return native_cost
    required = (
        "normalized_input_usd_per_million",
        "normalized_output_usd_per_million",
    )
    if not all(metadata.get(name) is not None for name in required):
        return 0.0
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cached_tokens = int(usage.get("cache_read_input_tokens") or 0)
    regular_input = max(0, input_tokens - cached_tokens)
    return round(
        (
            regular_input
            * float(metadata.get("normalized_input_usd_per_million") or 0)
            + cached_tokens
            * float(metadata.get("normalized_cached_input_usd_per_million") or 0)
            + output_tokens
            * float(metadata.get("normalized_output_usd_per_million") or 0)
        )
        / 1_000_000,
        12,
    )


def _workspace_manifest(root: Path) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    total_bytes = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if not path.is_file():
            continue
        if path.is_symlink():
            raise ValueError("workspace manifest rejects symbolic-link files")
        if len(output) >= 100_000:
            raise ValueError("workspace manifest exceeds the 100000-file evidence budget")
        relative = path.relative_to(root).as_posix()
        size = path.stat().st_size
        total_bytes += size
        if total_bytes > 8 * 1024 * 1024 * 1024:
            raise ValueError("workspace manifest exceeds the 8 GiB evidence budget")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
        output[relative] = {
            "sha256": digest.hexdigest(),
            "bytes": size,
        }
    return output


def _is_external_provider_endpoint(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme.casefold() != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
    )


def _workspace_delta(
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    created = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path
        for path in before_paths.intersection(after_paths)
        if before[path].get("sha256") != after[path].get("sha256")
    )
    return {
        "schema": "zyra.physical-workspace-delta/v1",
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "changed": [*created, *modified, *deleted],
        "after": {path: dict(after[path]) for path in sorted(after)},
        "before_manifest_digest": _digest(dict(before)),
        "after_manifest_digest": _digest(dict(after)),
        "physical_location_redacted": True,
    }


class _BenchmarkWorkspaceMirror:
    """Keep WorkspaceEditPort and an external benchmark container coherent.

    The external harness owns the canonical container.  File tools still pass
    through WorkspaceManager for transaction and evidence custody, so this
    bridge synchronizes that managed mirror at every file/shell boundary.  A
    shared lock gives mixed tool calls one physical ordering instead of the
    former end-of-run eventual consistency.
    """

    def __init__(
        self,
        binding: Mapping[str, Any],
        workspace_root: Path,
        *,
        synced_manifest: Mapping[str, Mapping[str, Any]],
    ) -> None:
        self.binding = dict(binding)
        self.workspace_root = workspace_root.resolve()
        self.guard = threading.RLock()
        self._synced_manifest = {
            path: dict(record) for path, record in synced_manifest.items()
        }
        self._push_cycles = 0
        self._pull_cycles = 1
        self._written_path_digests: set[str] = set()
        self._deleted_path_digests: set[str] = set()

    def pull_from_container(self) -> None:
        with self.guard:
            _pull_benchmark_workspace(self.binding, self.workspace_root)
            self._synced_manifest = _workspace_manifest(self.workspace_root)
            self._pull_cycles += 1

    def push_to_container(self) -> dict[str, Any]:
        with self.guard:
            after = _workspace_manifest(self.workspace_root)
            result = _push_benchmark_workspace_delta(
                self.binding,
                self.workspace_root,
                before=self._synced_manifest,
                after=after,
            )
            self._synced_manifest = after
            self._push_cycles += 1
            self._written_path_digests.update(
                str(item) for item in result.get("written_path_digests") or ()
            )
            self._deleted_path_digests.update(
                str(item) for item in result.get("deleted_path_digests") or ()
            )
            return self.report()

    def report(self) -> dict[str, Any]:
        with self.guard:
            return {
                "written_count": len(self._written_path_digests),
                "deleted_count": len(self._deleted_path_digests),
                "written_path_digests": sorted(self._written_path_digests),
                "deleted_path_digests": sorted(self._deleted_path_digests),
                "push_cycles": self._push_cycles,
                "pull_cycles": self._pull_cycles,
                "ordering": "live-serialized",
            }


class _BenchmarkWorkspaceEditPort(WorkspaceEditPort):
    """Workspace transaction port synchronized with a benchmark container."""

    def __init__(
        self,
        *args: Any,
        benchmark_mirror: _BenchmarkWorkspaceMirror,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._benchmark_mirror = benchmark_mirror

    def read_bytes(self, *args: Any, **kwargs: Any) -> Any:
        with self._benchmark_mirror.guard:
            self._benchmark_mirror.pull_from_container()
            return super().read_bytes(*args, **kwargs)

    def apply(self, *args: Any, **kwargs: Any) -> Any:
        with self._benchmark_mirror.guard:
            result = super().apply(*args, **kwargs)
            self._benchmark_mirror.push_to_container()
            return result


class _BenchmarkDockerCliSandboxConnector(DockerCliSandboxConnector):
    """Serialize shell execution with the managed file-tool mirror."""

    def __init__(
        self,
        *,
        benchmark_mirror: _BenchmarkWorkspaceMirror,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._benchmark_mirror = benchmark_mirror

    def execute(self, *args: Any, **kwargs: Any) -> Any:
        with self._benchmark_mirror.guard:
            self._benchmark_mirror.push_to_container()
            try:
                return super().execute(*args, **kwargs)
            finally:
                self._benchmark_mirror.pull_from_container()


def _benchmark_docker_binding(
    *,
    node_data_root: Path,
    workspace_root: Path,
    workspace_data_root: Path,
) -> dict[str, Any] | None:
    """Resolve the explicit Harbor/Docker binding without a default fallback."""

    container = str(os.environ.get("ZYRA_BENCHMARK_DOCKER_CONTAINER") or "").strip()
    workdir = str(os.environ.get("ZYRA_BENCHMARK_DOCKER_WORKDIR") or "").strip()
    if not container and not workdir:
        return None
    if not container or not workdir:
        raise ValueError(
            "ZYRA_BENCHMARK_DOCKER_CONTAINER and ZYRA_BENCHMARK_DOCKER_WORKDIR "
            "must be configured together"
        )
    connector = DockerCliSandboxConnector(container=container, workdir=workdir)
    resolved_workspace = workspace_root.resolve()
    resolved_data_root = workspace_data_root.resolve()
    try:
        relative_workspace = resolved_workspace.relative_to(resolved_data_root)
    except ValueError as error:
        raise ValueError("benchmark mirror workspace escaped WorkspaceManager data root") from error
    if str(relative_workspace) in {"", "."}:
        raise ValueError("benchmark mirror workspace cannot be the WorkspaceManager data root")
    sync_root = (node_data_root.resolve() / "benchmark-workspace-sync").resolve()
    sync_root.relative_to(node_data_root.resolve())
    sync_root.mkdir(parents=True, exist_ok=True)
    return {
        "container": container,
        "container_ref_digest": connector.container_ref_digest,
        "workdir": connector.workdir,
        "docker_executable": connector.docker_executable,
        "workspace_data_root": resolved_data_root,
        "sync_root": sync_root,
    }


def _benchmark_permission_policy(
    *,
    session_id: str,
    workspace_root: Path,
    container_ref_digest: str,
) -> dict[str, Any]:
    """Authorize tool calls that remain fenced by the Docker command policy.

    Autonomous physical workers cannot answer an interactive ``ASK``.  Normal
    deployments therefore fail closed for unruled shell use.  An official
    benchmark binding is different: the external harness owns a disposable
    container and the structured Docker gateway independently rejects shell
    control syntax, path escape, destructive Git, and network Git.  This rule
    gives the canonical TypeScript permission owner authority to issue exact,
    one-use grants for the remaining commands, scoped to one physical session
    and its managed mirror workspace.
    """

    resolved_workspace = workspace_root.resolve()
    return {
        "version": "zyra.e02-typescript-permission-policy-input.v1",
        "canonical_owner": "typescript",
        "mode": "acceptEdits",
        "interactive": False,
        "headless": True,
        "rules": [
            {
                "rule_id": "managed-harbor-docker-shell",
                "effect": "allow",
                "source": "managed",
                "kind": "tool",
                "tool_pattern": "shell",
                "namespace_pattern": "builtin",
                "operation_pattern": "execute",
                "workspace_pattern": str(resolved_workspace),
                "session_pattern": session_id,
                "argument_pattern": "*",
                "priority": 1000,
                "enabled": True,
                "reason": (
                    "official benchmark shell is fenced by the structured "
                    "Docker command policy"
                ),
                "metadata": {
                    "authority": "external-disposable-benchmark-container",
                    "container_ref_digest": container_ref_digest,
                    "gateway_hard_denies_remain_authoritative": True,
                },
            }
        ],
        "python_policy_fallback": False,
    }


def _pull_benchmark_workspace(
    binding: Mapping[str, Any],
    workspace_root: Path,
) -> None:
    """Replace the managed mirror with the current canonical container tree."""

    root = _validated_benchmark_workspace(binding, workspace_root)
    staging = Path(
        tempfile.mkdtemp(
            prefix="pull-",
            dir=_required_path(binding.get("sync_root"), "benchmark sync root"),
        )
    ).resolve()
    try:
        _run_benchmark_docker(
            binding,
            (
                "cp",
                f"{binding['container']}:{binding['workdir']}/.",
                str(staging),
            ),
            operation="benchmark_workspace_pull",
            timeout_seconds=300.0,
        )
        for child in tuple(root.iterdir()):
            if child.is_symlink() or child.is_file():
                child.unlink()
            else:
                shutil.rmtree(child)
        for child in tuple(staging.iterdir()):
            shutil.move(str(child), str(root / child.name))
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _push_benchmark_workspace_delta(
    binding: Mapping[str, Any],
    workspace_root: Path,
    *,
    before: Mapping[str, Mapping[str, Any]],
    after: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Apply edits made through file tools to the canonical task container."""

    root = _validated_benchmark_workspace(binding, workspace_root)
    before_paths = set(before)
    after_paths = set(after)
    written = sorted(
        path
        for path in after_paths
        if path not in before_paths
        or before[path].get("sha256") != after[path].get("sha256")
    )
    deleted = sorted(before_paths - after_paths)
    workdir = str(binding["workdir"])
    container = str(binding["container"])
    for relative in written:
        canonical = canonical_logical_path(relative, allow_root=False)
        destination = posixpath.join(workdir, canonical)
        parent = posixpath.dirname(destination)
        _run_benchmark_docker(
            binding,
            ("exec", container, "mkdir", "-p", "--", parent),
            operation="benchmark_workspace_parent",
            timeout_seconds=30.0,
        )
        source = (root / Path(*canonical.split("/"))).resolve()
        source.relative_to(root)
        _run_benchmark_docker(
            binding,
            ("cp", str(source), f"{container}:{destination}"),
            operation="benchmark_workspace_push",
            timeout_seconds=120.0,
        )
    for relative in deleted:
        canonical = canonical_logical_path(relative, allow_root=False)
        destination = posixpath.join(workdir, canonical)
        _run_benchmark_docker(
            binding,
            ("exec", container, "rm", "-f", "--", destination),
            operation="benchmark_workspace_delete",
            timeout_seconds=30.0,
        )
    return {
        "written_count": len(written),
        "deleted_count": len(deleted),
        "written_path_digests": [_digest(path) for path in written],
        "deleted_path_digests": [_digest(path) for path in deleted],
    }


def _validated_benchmark_workspace(
    binding: Mapping[str, Any],
    workspace_root: Path,
) -> Path:
    root = workspace_root.resolve()
    data_root = _required_path(
        binding.get("workspace_data_root"), "benchmark workspace data root"
    )
    try:
        relative = root.relative_to(data_root)
    except ValueError as error:
        raise ValueError("benchmark workspace escaped its declared data root") from error
    if str(relative) in {"", "."} or not root.is_dir():
        raise ValueError("benchmark workspace mirror target is not a managed task directory")
    return root


def _run_benchmark_docker(
    binding: Mapping[str, Any],
    argv: tuple[str, ...],
    *,
    operation: str,
    timeout_seconds: float,
) -> subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            [str(binding["docker_executable"]), *argv],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout_seconds,
            shell=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{operation} could not execute Docker CLI: {type(error).__name__}") from error
    if completed.returncode != 0:
        error_text = completed.stderr.decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(
            f"{operation} failed with Docker exit {completed.returncode}: {error_text}"
        )
    return completed


def _required_mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _required_path(value: Any, name: str) -> Path:
    rendered = str(value or "").strip()
    if not rendered:
        raise ValueError(f"{name} is required")
    return Path(rendered).expanduser().resolve()


def _safe_id(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)[:160]


def _digest(value: Any) -> str:
    encoded = (
        value
        if isinstance(value, str)
        else json.dumps(
            to_jsonable(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["execute_code_worker_operator"]
