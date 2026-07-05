from __future__ import annotations

from typing import Any


def evaluate_task_trace(task: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Evaluate trace completeness without taking over runtime control."""

    artifacts = task.get("artifacts") if isinstance(task.get("artifacts"), list) else []
    plan_nodes = task.get("plan_nodes") if isinstance(task.get("plan_nodes"), dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    metrics = {
        "event_count": len(events),
        "plan_node_count": len(plan_nodes),
        "artifact_count": len(artifacts),
        "decision_record_count": len(task.get("decisions", [])) if isinstance(task.get("decisions"), list) else 0,
        "tool_result_count": _count_payload(events, "tool_result"),
        "browser_result_count": _count_payload(events, "browser_result"),
        "worker_result_count": _count_payload(events, "worker_result"),
        "skill_invocation_count": _count_events(events, "skill_invoked"),
        "requirement_change_count": _count_events(events, "requirement_change"),
        "failure_injection_count": _count_events(events, "failure_injected"),
        "constraint_check_count": _count_events(events, "constraint_check"),
        "topology_route_count": _count_events(events, "topology_route"),
        "structured_message_count": _count_low_entropy_messages(events),
        "replanned_node_count": _count_replanned_nodes(plan_nodes),
        "control_command_count": len(metadata.get("control_commands", [])) if isinstance(metadata.get("control_commands"), list) else 0,
    }
    checks = [
        _check("has_task_created", any(event.get("event_type") == "task_created" for event in events)),
        _check("has_plan_nodes", metrics["plan_node_count"] > 0),
        _check("has_runtime_activity", metrics["tool_result_count"] + metrics["browser_result_count"] + metrics["worker_result_count"] > 0),
        _check("has_artifacts", metrics["artifact_count"] > 0),
        _check("has_control_surface", metrics["control_command_count"] > 0 or any(event.get("event_type") == "control_command" for event in events)),
        _check("has_symbolic_control", metrics["constraint_check_count"] > 0 and metrics["topology_route_count"] > 0),
        _check("has_structured_messages", metrics["structured_message_count"] > 0),
        _check("tool_results_not_all_failed", not _all_payload_results_failed(events, "tool_result")),
        _check("browser_results_not_all_failed", not _all_payload_results_failed(events, "browser_result")),
    ]
    passed = sum(1 for check in checks if check["passed"])
    recommendations = _recommendations(metrics, checks)
    return {
        "score": round(passed / len(checks), 3),
        "passed_checks": passed,
        "total_checks": len(checks),
        "checks": checks,
        "metrics": metrics,
        "recommendations": recommendations,
    }


def _check(name: str, passed: bool) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed)}


def _count_payload(events: list[dict[str, Any]], key: str) -> int:
    return sum(1 for event in events if isinstance(event.get("payload"), dict) and key in event["payload"])


def _count_events(events: list[dict[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if event.get("event_type") == event_type)


def _all_payload_results_failed(events: list[dict[str, Any]], key: str) -> bool:
    results = [
        event["payload"][key]
        for event in events
        if isinstance(event.get("payload"), dict) and isinstance(event["payload"].get(key), dict)
    ]
    if not results:
        return False
    return all(result.get("ok") is False for result in results)


def _count_low_entropy_messages(events: list[dict[str, Any]]) -> int:
    return sum(
        1
        for event in events
        if event.get("event_type") == "agent_message"
        and isinstance(event.get("payload"), dict)
        and event["payload"].get("low_entropy") is True
    )


def _count_replanned_nodes(plan_nodes: dict[str, Any]) -> int:
    return sum(
        1
        for node in plan_nodes.values()
        if isinstance(node, dict)
        and (
            node.get("status") in {"replanned", "needs_revision", "superseded"}
            or node.get("metadata", {}).get("source") in {"requirement_change", "failure_injection"}
        )
    )


def _recommendations(metrics: dict[str, int], checks: list[dict[str, Any]]) -> list[str]:
    failed = {check["name"] for check in checks if not check["passed"]}
    recommendations: list[str] = []
    if "has_runtime_activity" in failed:
        recommendations.append("Run a worker or tool action so the trace contains executable evidence.")
    if "has_artifacts" in failed:
        recommendations.append("Persist large outputs, browser state, trace, or checkpoint data as artifacts.")
    if metrics["requirement_change_count"] == 0:
        recommendations.append("For competition demos, include at least one /change event to show dynamic requirement handling.")
    if metrics["failure_injection_count"] == 0:
        recommendations.append("For competition demos, include at least one /inject event or node failure recovery path.")
    if metrics["skill_invocation_count"] == 0:
        recommendations.append("For M2 runtime demos, record at least one skill_invoked event to prove the skills surface is part of the trace.")
    if metrics["constraint_check_count"] == 0 or metrics["topology_route_count"] == 0:
        recommendations.append("For M3 demos, include ConstraintKeeper and TopologyRouter events in the trace.")
    if metrics["structured_message_count"] == 0:
        recommendations.append("For M3 demos, preserve low-entropy structured agent messages instead of free-text-only coordination.")
    return recommendations
