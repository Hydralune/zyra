from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from zyra_core import DecisionRecord, EventRecord, EventType, PlanNode, TaskState, new_id, to_jsonable


@dataclass(frozen=True, slots=True)
class RouteCandidate:
    worker_name: str
    score: float
    reasons: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    source: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TopologyRouter:
    """Sparse symbolic router over heterogeneous workers and current graph state."""

    def __init__(
        self,
        worker_descriptors: list[Any] | None = None,
        resource_scheduler: Any | None = None,
        topology_policy_trigger: (
            Callable[
                [TaskState, PlanNode | None, EventRecord | None],
                Mapping[str, Any],
            ]
            | None
        ) = None,
    ) -> None:
        self.worker_descriptors = worker_descriptors if worker_descriptors is not None else self._default_workers()
        self.resource_scheduler = resource_scheduler if resource_scheduler is not None else self._default_resource_scheduler()
        self.topology_policy_trigger = topology_policy_trigger

    def route(
        self,
        state: TaskState,
        *,
        node: PlanNode | None = None,
        top_k: int = 2,
        cause_event: EventRecord | None = None,
        route_type: str = "topology_route",
    ) -> tuple[DecisionRecord, EventRecord]:
        topology_policy: Mapping[str, Any] = {}
        if self.topology_policy_trigger is not None:
            try:
                topology_policy = dict(
                    self.topology_policy_trigger(
                        state,
                        node,
                        cause_event,
                    )
                )
            except Exception as error:  # noqa: BLE001 - baseline route remains explicit and available.
                if bool(getattr(self.topology_policy_trigger, "fail_closed", False)):
                    raise RuntimeError(
                        "required production topology policy failed closed: "
                        f"{type(error).__name__}: {error}"
                    ) from error
                topology_policy = {
                    "used_baseline": True,
                    "committed": False,
                    "degraded": True,
                    "degraded_reason": (
                        f"topology_policy_trigger:{type(error).__name__}"
                    ),
                    "fallback_profile": "phase1_deterministic_baseline",
                    "silent_fallback": False,
                }
                state.metadata["topology_policy_error"] = (
                    f"{type(error).__name__}: {error}"
                )
        resource_decision = None
        candidates: list[RouteCandidate]
        if topology_policy.get("reroute_required") is True:
            # The first policy window may only establish actual communication
            # outcomes.  Do not mutate placement/lease/graph state until a
            # later route consumes that prior window.
            candidates = self.rank_candidates(
                state,
                node=node,
                cause_event=cause_event,
            )
        elif self.resource_scheduler is not None:
            try:
                operator_input = topology_policy.get("operator_candidate_set")
                resource_decision = self.resource_scheduler.decide(
                    state,
                    node=node,
                    cause_event=cause_event,
                    operator_input=operator_input,
                )
                self.resource_scheduler.attach_decision_to_state(state, resource_decision, node=node)
                candidates = [
                    RouteCandidate(
                        worker_name=resource_decision.selected_worker,
                        score=resource_decision.score,
                        reasons=list(resource_decision.reasons),
                        capabilities=list(resource_decision.metadata.get("selected_capabilities", [])),
                        source=",".join(resource_decision.source_modules.keys()),
                        metadata={
                            "manifest_id": resource_decision.selected_manifest_id,
                            "backend": str(resource_decision.selected_backend),
                            "location": str(resource_decision.selected_location),
                            "resource_decision_id": resource_decision.decision_id,
                        },
                    ),
                    *[
                        RouteCandidate(
                            worker_name=str(item["runtime_worker"]),
                            score=float(item["score"]),
                            reasons=[str(reason) for reason in item.get("reasons", [])],
                            source="m5-resource-scheduler",
                            metadata={
                                "manifest_id": str(item["worker_id"]),
                                "backend": str(item["backend"]),
                                "location": str(item["location"]),
                            },
                        )
                        for item in resource_decision.alternatives
                    ],
                ]
            except Exception as error:  # noqa: BLE001 - keep symbolic fallback usable if scheduler package is absent/broken.
                resource_decision = None
                state.metadata["resource_scheduler_error"] = f"{type(error).__name__}: {error}"
                if topology_policy.get("operator_candidate_set") is not None:
                    raise RuntimeError(
                        "ResourceScheduler failed after a MaAS candidate set "
                        "was committed; symbolic fallback would bypass the "
                        "placement contract"
                    ) from error
                candidates = self.rank_candidates(state, node=node, cause_event=cause_event)
        else:
            candidates = self.rank_candidates(state, node=node, cause_event=cause_event)

        placement_binder = getattr(
            self.topology_policy_trigger,
            "bind_resource_decision",
            None,
        )
        if resource_decision is not None and callable(placement_binder):
            topology_policy = {
                **dict(topology_policy),
                "physical_placement": dict(
                    placement_binder(
                        state,
                        node,
                        resource_decision,
                        topology_policy,
                    )
                ),
            }

        selected = candidates[0] if candidates else RouteCandidate("ChiefPlanner", 0.0, ["fallback route"])
        if node is not None and selected.worker_name not in {"ChiefPlanner", "ConstraintKeeper"}:
            node.assigned_worker_id = selected.worker_name
            node.state_delta["assigned_worker_id"] = selected.worker_name
        decision = DecisionRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=None if node is None else node.node_id,
            decision_type=route_type,
            selected=selected.worker_name,
            summary=f"Selected {selected.worker_name} for {route_type}.",
            rationale="; ".join(selected.reasons) or "Highest weighted route candidate.",
            alternatives=[candidate.worker_name for candidate in candidates[1:top_k]],
            route_candidates=[to_jsonable(candidate) for candidate in candidates[:top_k]],
            affected_node_ids=[] if node is None else [node.node_id],
            state_delta={
                "selected_worker": selected.worker_name,
                "node_id": None if node is None else node.node_id,
                "route_type": route_type,
            },
            metadata={
                "router": "m5-resource-aware-topology-router" if resource_decision is not None else "m3-symbolic-topology-router",
                "top_k": str(top_k),
                "cause_event_id": "" if cause_event is None else cause_event.event_id,
                "topology_policy_profile": str(
                    (
                        topology_policy.get("execution_receipt")
                        or {}
                    ).get("actual_profile_id")
                    or "phase1_deterministic_baseline"
                ),
                "topology_policy_committed": str(
                    bool(topology_policy.get("committed", False))
                ).lower(),
                "operator_candidate_contract_consumed": str(
                    bool(
                        resource_decision is not None
                        and resource_decision.signals.metadata.get(
                            "operator_candidate_contract_consumed",
                            False,
                        )
                    )
                ).lower(),
                "physical_lease_ref": str(
                    (
                        topology_policy.get("physical_placement")
                        or {}
                    ).get("lease_id")
                    or ""
                ),
            },
        )
        if resource_decision is not None:
            self.resource_scheduler.enrich_decision_record(decision, resource_decision)
        state.decisions.append(decision)
        state.metadata["last_topology_route"] = {
            "decision_id": decision.decision_id,
            "selected_worker": selected.worker_name,
            "node_id": decision.node_id,
            "route_type": route_type,
            "resource_decision_id": "" if resource_decision is None else resource_decision.decision_id,
            "topology_policy": dict(topology_policy),
        }
        event = EventRecord(
            run_id=state.run_id,
            task_id=state.task_id,
            event_type=EventType.TOPOLOGY_ROUTE,
            node_id=decision.node_id,
            payload={
                "decision": to_jsonable(decision),
                "selected_worker": selected.worker_name,
                "candidate_count": len(candidates),
                "route_type": route_type,
                "resource_decision": None if resource_decision is None else to_jsonable(resource_decision),
                "topology_policy": dict(topology_policy),
            },
        )
        return decision, event

    def rank_candidates(
        self,
        state: TaskState,
        *,
        node: PlanNode | None = None,
        cause_event: EventRecord | None = None,
    ) -> list[RouteCandidate]:
        text = self._task_text(state, node=node, cause_event=cause_event)
        preferred = str(state.metadata.get("runtime_hints", {}).get("preferred_worker", ""))
        failed_workers = self._failed_workers(state, cause_event)
        candidates = [self._score_worker(worker, text, preferred, failed_workers, state, node) for worker in self.worker_descriptors]
        viable = [candidate for candidate in candidates if candidate.score > -100]
        viable.sort(key=lambda candidate: candidate.score, reverse=True)
        if viable:
            return viable
        return [
            RouteCandidate(
                worker_name="ChiefPlanner",
                score=0.0,
                reasons=["No concrete runtime matched; planner fallback keeps graph state traceable."],
            )
        ]

    def _score_worker(
        self,
        worker: Any,
        text: str,
        preferred: str,
        failed_workers: set[str],
        state: TaskState,
        node: PlanNode | None,
    ) -> RouteCandidate:
        name = self._descriptor_value(worker, "name")
        capabilities = self._descriptor_capabilities(worker)
        source = self._descriptor_value(worker, "source")
        allowed = []
        if node is not None:
            allowed.extend(node.constraints.allowed_workers)
        allowed.extend(state.constraints.allowed_workers)
        if allowed and name not in allowed:
            return RouteCandidate(name, -999.0, ["blocked by allowed_workers constraint"], capabilities, source)

        score = 1.0
        reasons = ["registered worker"]
        if preferred and preferred == name:
            score += 8.0
            reasons.append("matches preferred_worker hint")
        if name in failed_workers:
            score -= 5.0
            reasons.append("penalized by recent failure history")
        if name == "BrowserWorker":
            has_browser_input = _contains_url(text) or "browser_plan" in text
            if has_browser_input:
                score += 7.0
                reasons.append("task text contains executable browser input")
            if has_browser_input and _has_any(text, ["web", "browser", "page", "dom", "screenshot", "research"]):
                score += 4.0
                reasons.append("browser or web-research task profile")
            if "browser-agent" in capabilities:
                score += 1.0
                reasons.append("supports browser agent execution")
        if name == "CodeWorkerRuntime":
            if _has_any(text, ["code", "repo", "file", "patch", "test", "script", "python", "typescript", "shell"]):
                score += 5.0
                reasons.append("codebase or tool-execution task profile")
            if not _contains_url(text):
                score += 2.0
                reasons.append("default local workspace execution path")
            if "tool-permission" in capabilities:
                score += 1.0
                reasons.append("supports permission-governed tools")
        if node is not None and node.metadata.get("stage") in {"verify", "recover"} and "verification" in capabilities:
            score += 3.0
            reasons.append("verification-capable stage")
        return RouteCandidate(
            worker_name=name,
            score=round(score, 3),
            reasons=reasons,
            capabilities=capabilities,
            source=source,
            metadata={"candidate_id": new_id("routecand")},
        )

    def _failed_workers(self, state: TaskState, cause_event: EventRecord | None) -> set[str]:
        failed = {
            str(node.assigned_worker_id)
            for node in state.plan_nodes.values()
            if node.assigned_worker_id and str(node.status) == "failed"
        }
        raw = "" if cause_event is None else str(cause_event.payload.get("raw") or "").lower()
        if "browser" in raw:
            failed.add("BrowserWorker")
        if "code" in raw or "shell" in raw:
            failed.add("CodeWorkerRuntime")
        return failed

    def _task_text(
        self,
        state: TaskState,
        *,
        node: PlanNode | None,
        cause_event: EventRecord | None,
    ) -> str:
        parts = [state.user_goal]
        runtime_hints = state.metadata.get("runtime_hints")
        if isinstance(runtime_hints, dict):
            parts.append(str(runtime_hints))
        if node is not None:
            parts.extend([node.title, node.description, node.summary, str(node.metadata)])
        if cause_event is not None:
            parts.append(str(cause_event.payload.get("raw") or ""))
        return "\n".join(parts).lower()

    def _default_workers(self) -> list[Any]:
        try:
            from zyra_runtime import default_worker_descriptors
        except Exception:  # noqa: BLE001 - symbolic package can still load without runtime package path.
            return [
                {"name": "CodeWorkerRuntime", "source": "zyra", "capabilities": ("codebase-analysis", "verification")},
                {"name": "BrowserWorker", "source": "zyra", "capabilities": ("web-research", "browser-agent")},
            ]
        return list(default_worker_descriptors())

    def _default_resource_scheduler(self) -> Any | None:
        try:
            from zyra_scheduler import ResourceScheduler
        except Exception:  # noqa: BLE001 - scheduler is optional for early package-only imports.
            return None
        return ResourceScheduler()

    def _descriptor_value(self, worker: Any, key: str) -> str:
        if isinstance(worker, dict):
            return str(worker.get(key) or "")
        return str(getattr(worker, key, ""))

    def _descriptor_capabilities(self, worker: Any) -> list[str]:
        if isinstance(worker, dict):
            return [str(item) for item in worker.get("capabilities", ())]
        return [str(item) for item in getattr(worker, "capabilities", ())]


def _has_any(text: str, needles: list[str]) -> bool:
    return any(needle in text for needle in needles)


def _contains_url(text: str) -> bool:
    for token in text.split():
        candidate = token.strip(".,;()[]{}<>\"'")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https", "file"}:
            return True
    return False
