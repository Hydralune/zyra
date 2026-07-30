from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from zyra_core import DecisionRecord, EventRecord, EventType, PlanNode, TaskState, to_jsonable

from .models import ResourceDecision, SchedulerSignals, WorkerManifest
from .pool import WorkerPool


SCHEDULER_SOURCE_MODULES = {
    "agentscope": ["manager/service/pipeline capability registry"],
    "OpenHands": ["runtime backend and event-stream controller"],
    "openclaw": ["active memory and failure-aware control concepts"],
    "agent-framework": ["middleware-style routing signals"],
    "claude-code-best": ["permission runtime, model/cost commands, QueryEngine tool loop"],
    "browser-use": ["browser session/controller runtime profile"],
}


@dataclass(frozen=True, slots=True)
class _ScoredManifest:
    manifest: WorkerManifest
    score: float
    reasons: list[str]


class ResourceScheduler:
    """Resource-aware scheduler for local/edge/cloud heterogeneous workers."""

    def __init__(self, worker_pool: WorkerPool | None = None) -> None:
        self.worker_pool = worker_pool or WorkerPool()

    def decide(
        self,
        state: TaskState,
        *,
        node: PlanNode | None = None,
        cause_event: EventRecord | Mapping[str, Any] | None = None,
        events: Sequence[Mapping[str, Any]] | None = None,
        memory_records: Sequence[Any] | None = None,
        avoid_workers: Sequence[str] | None = None,
        operator_input: Any | None = None,
    ) -> ResourceDecision:
        operator_contract = _operator_selection_input(
            operator_input,
            run_id=state.run_id,
            task_id=state.task_id,
        )
        event_dict = _event_dict(cause_event) if cause_event is not None else None
        signals = self._signals(
            state,
            node=node,
            cause_event=event_dict,
            events=events or [],
            memory_records=memory_records or [],
            avoid_workers=avoid_workers or [],
        )
        if operator_contract is not None:
            signals.metadata["operator_selection_input"] = operator_contract
            signals.metadata["operator_candidate_contract_consumed"] = True
            signals.metadata["operator_candidate_contract_effect"] = (
                "selection_input_only_pending_P2-S04-03"
            )
        health = {item.worker_id: item for item in self.worker_pool.health_snapshot(state=state, events=events or [])}
        scored = [
            self._score_manifest(manifest, signals, state=state, node=node, health=health.get(manifest.worker_id))
            for manifest in self.worker_pool.manifests()
        ]
        viable = [item for item in scored if item.score > -100]
        viable.sort(key=lambda item: item.score, reverse=True)
        selected = viable[0] if viable else _ScoredManifest(
            self.worker_pool.manifests()[0],
            0.0,
            ["fallback to first enabled worker manifest"],
        )
        alternatives = [
            {
                "worker_id": item.manifest.worker_id,
                "display_name": item.manifest.display_name,
                "runtime_worker": item.manifest.runtime_worker,
                "backend": str(item.manifest.backend),
                "location": str(item.manifest.location),
                "score": round(item.score, 3),
                "reasons": item.reasons,
            }
            for item in viable[1:4]
        ]
        model_split = self._model_split(selected.manifest, signals, alternatives)
        return ResourceDecision(
            run_id=state.run_id,
            task_id=state.task_id,
            node_id=None if node is None else node.node_id,
            selected_manifest_id=selected.manifest.worker_id,
            selected_worker=selected.manifest.runtime_worker,
            selected_backend=selected.manifest.backend,
            selected_location=selected.manifest.location,
            score=round(selected.score, 3),
            reasons=selected.reasons,
            alternatives=alternatives,
            model_split=model_split,
            signals=signals,
            source_modules=SCHEDULER_SOURCE_MODULES,
            metadata={
                "selected_display_name": selected.manifest.display_name,
                "selected_tools": list(selected.manifest.tools),
                "selected_capabilities": list(selected.manifest.capabilities),
                "selected_sandbox": selected.manifest.sandbox,
                "selected_gateway": selected.manifest.gateway,
                "selected_privacy": selected.manifest.privacy_level,
                "manifest_source_modules": selected.manifest.source_modules,
                "operator_selection_input": operator_contract,
                "operator_candidate_contract_consumed": (
                    operator_contract is not None
                ),
                "operator_candidate_contract_effect": (
                    "selection_input_only_pending_P2-S04-03"
                    if operator_contract is not None
                    else "phase1_scheduler_input"
                ),
                "placement_owner": "ResourceScheduler",
                "lease_owner": "WorkerPoolFoundationRuntime",
            },
        )

    def event_for_decision(self, decision: ResourceDecision) -> EventRecord:
        return EventRecord(
            run_id=decision.run_id,
            task_id=decision.task_id,
            event_type=EventType.RESOURCE_DECISION,
            node_id=decision.node_id,
            payload={
                "resource_decision": to_jsonable(decision),
                "selected_worker": decision.selected_worker,
                "selected_manifest_id": decision.selected_manifest_id,
                "selected_backend": str(decision.selected_backend),
                "selected_location": str(decision.selected_location),
                "model_split": decision.model_split,
            },
        )

    def attach_decision_to_state(
        self,
        state: TaskState,
        decision: ResourceDecision,
        *,
        node: PlanNode | None = None,
    ) -> None:
        entry = to_jsonable(decision)
        state.metadata["last_resource_decision"] = entry
        state.metadata.setdefault("resource_decisions", []).append(entry)
        if node is not None:
            node.assigned_worker_id = decision.selected_worker
            node.metadata["resource_decision"] = entry
            node.metadata["worker_manifest_id"] = decision.selected_manifest_id
            node.metadata["backend"] = str(decision.selected_backend)
            node.metadata["location"] = str(decision.selected_location)
            node.state_delta["assigned_worker_id"] = decision.selected_worker
            node.state_delta["worker_manifest_id"] = decision.selected_manifest_id

    def decision_record_metadata(self, decision: ResourceDecision) -> dict[str, Any]:
        return {
            "scheduler": "m5-resource-scheduler",
            "resource_decision_id": decision.decision_id,
            "selected_manifest_id": decision.selected_manifest_id,
            "selected_backend": str(decision.selected_backend),
            "selected_location": str(decision.selected_location),
            "model_split": decision.model_split,
            "signals": to_jsonable(decision.signals),
            "source_modules": decision.source_modules,
        }

    def enrich_decision_record(self, record: DecisionRecord, decision: ResourceDecision) -> None:
        record.selected = decision.selected_worker
        record.summary = f"Selected {decision.selected_worker} via {decision.selected_manifest_id}."
        record.rationale = "; ".join(decision.reasons)
        record.alternatives = [str(item["runtime_worker"]) for item in decision.alternatives]
        record.route_candidates = [
            {
                "worker_name": decision.selected_worker,
                "manifest_id": decision.selected_manifest_id,
                "score": decision.score,
                "reasons": list(decision.reasons),
                "capabilities": list(decision.metadata.get("selected_capabilities", [])),
                "source": ",".join(decision.source_modules.keys()),
                "metadata": self.decision_record_metadata(decision),
            },
            *[
                {
                    "worker_name": item["runtime_worker"],
                    "manifest_id": item["worker_id"],
                    "score": item["score"],
                    "reasons": item["reasons"],
                    "capabilities": [],
                    "source": "",
                    "metadata": {
                        "backend": item["backend"],
                        "location": item["location"],
                    },
                }
                for item in decision.alternatives
            ],
        ]
        record.state_delta.update(
            {
                "selected_worker": decision.selected_worker,
                "selected_manifest_id": decision.selected_manifest_id,
                "selected_backend": str(decision.selected_backend),
                "selected_location": str(decision.selected_location),
                "resource_decision_id": decision.decision_id,
            }
        )
        record.metadata.update(self.decision_record_metadata(decision))

    def _signals(
        self,
        state: TaskState,
        *,
        node: PlanNode | None,
        cause_event: Mapping[str, Any] | None,
        events: Sequence[Mapping[str, Any]],
        memory_records: Sequence[Any],
        avoid_workers: Sequence[str],
    ) -> SchedulerSignals:
        text = _task_text(state, node=node, cause_event=cause_event)
        hints = state.metadata.get("runtime_hints") if isinstance(state.metadata.get("runtime_hints"), Mapping) else {}
        required_tools = _required_tools(state, node=node, hints=hints)
        preferred_worker = str(hints.get("preferred_worker") or hints.get("worker") or "")
        failure_workers = _failure_workers_from_state_and_events(state, events, memory_records)
        for worker in avoid_workers:
            failure_workers[str(worker)] = failure_workers.get(str(worker), 0) + 3
        profile = _task_profile(text, required_tools)
        privacy = "sensitive" if _has_any(text, ["secret", "credential", "private", "本地", "隐私", "脱敏"]) else "project"
        return SchedulerSignals(
            required_tools=required_tools,
            preferred_worker=preferred_worker,
            avoided_workers=[str(item) for item in avoid_workers],
            privacy_mode=privacy,
            contains_url=_contains_url(text),
            stage="" if node is None else str(node.metadata.get("stage") or ""),
            task_profile=profile,
            failure_workers=failure_workers,
            requirement_change_count=_count_metadata_list(state, "requirement_changes") + _event_count(events, "requirement_change"),
            failure_count=_count_metadata_list(state, "failure_injections") + _event_count(events, "failure_injected") + _event_count(events, "node_failed"),
            compact_count=_count_metadata_list(state, "compactions") + _text_count(events, "compact"),
            trajectory_count=_text_count(events, "trajectory") + _text_count(memory_records, "trajectory"),
            memory_record_count=len(memory_records),
            model_pressure=_model_pressure(text, state, node),
            source_event_id="" if cause_event is None else str(cause_event.get("event_id") or ""),
            metadata={
                "text_terms": _keywords(text),
                "runtime_hints": dict(hints),
            },
        )

    def _score_manifest(
        self,
        manifest: WorkerManifest,
        signals: SchedulerSignals,
        *,
        state: TaskState,
        node: PlanNode | None,
        health: Any,
    ) -> _ScoredManifest:
        if not manifest.enabled:
            return _ScoredManifest(manifest, -999.0, ["manifest disabled"])

        allowed = []
        if node is not None:
            allowed.extend(node.constraints.allowed_workers)
        allowed.extend(state.constraints.allowed_workers)
        if allowed and not any(manifest.matches(str(item)) for item in allowed):
            return _ScoredManifest(manifest, -999.0, ["blocked by allowed_workers constraint"])

        missing_tools = [tool for tool in signals.required_tools if tool and tool not in manifest.tools and tool not in manifest.capabilities]
        if missing_tools:
            return _ScoredManifest(manifest, -130.0, [f"missing required tools: {', '.join(missing_tools)}"])

        score = 10.0
        reasons = ["registered M5 worker manifest"]
        if signals.preferred_worker and manifest.matches(signals.preferred_worker):
            score += 40.0
            reasons.append("matches preferred worker hint")
        for avoided in signals.avoided_workers:
            if manifest.matches(avoided):
                score -= 60.0
                reasons.append("explicitly avoided by recovery planner")

        failure_penalty = 0
        for key, count in signals.failure_workers.items():
            if manifest.matches(key):
                failure_penalty += count
        if failure_penalty:
            score -= min(45.0, failure_penalty * 9.0)
            reasons.append("penalized by failure history from event log or memory")

        if health is not None and str(health.status) in {"degraded", "failed", "unavailable"}:
            penalty = {"degraded": 8.0, "failed": 45.0, "unavailable": 100.0}.get(str(health.status), 0.0)
            score -= penalty
            reasons.append(f"health status {health.status}")

        if signals.privacy_mode == "sensitive" and manifest.privacy_level == "public_only":
            score -= 55.0
            reasons.append("cloud route penalized by sensitive/private context")
        if signals.privacy_mode == "sensitive" and manifest.location.value == "local":
            score += 12.0
            reasons.append("local route protects sensitive context")

        score += max(0, 12 - manifest.current_load * 4)
        score -= manifest.latency_ms / 80
        score -= manifest.cost_per_1k_tokens * 50

        if signals.task_profile == "browser" and manifest.runtime_worker == "BrowserWorker":
            score += 35.0
            reasons.append("browser/web profile requires DOM-capable edge worker")
        if signals.task_profile == "code" and manifest.worker_id == "local-code-worker":
            score += 30.0
            reasons.append("code/tool profile favors local permission-governed worker")
        if signals.task_profile == "memory" and manifest.worker_id == "local-memory-curator":
            score += 34.0
            reasons.append("memory/compact/trajectory profile favors memory curator")
        if signals.task_profile == "planning" and manifest.worker_id == "cloud-planner-verifier":
            score += 18.0
            reasons.append("planning/verification profile can use cloud verifier")
        if signals.stage in {"verify", "replan", "recover"} and "verification" in manifest.capabilities:
            score += 12.0
            reasons.append("stage benefits from verification capability")
        if signals.stage in {"recover", "replan"} and "recovery-context" in manifest.capabilities:
            score += 10.0
            reasons.append("stage benefits from memory-backed recovery context")
        if signals.requirement_change_count and manifest.worker_id in {"local-memory-curator", "cloud-planner-verifier"}:
            score += 7.0
            reasons.append("requirement change history increases replan/context priority")
        if signals.failure_count and "watchdog" in manifest.capabilities:
            score += 5.0
            reasons.append("failure history favors watchdog-capable worker")
        if signals.compact_count and "compact" in manifest.capabilities:
            score += 6.0
            reasons.append("compact history can restore context for this worker")
        if signals.model_pressure == "high" and manifest.worker_id == "cloud-planner-verifier" and signals.privacy_mode != "sensitive":
            score += 16.0
            reasons.append("high reasoning pressure allows cloud model split")
        return _ScoredManifest(manifest, score, reasons)

    def _model_split(
        self,
        manifest: WorkerManifest,
        signals: SchedulerSignals,
        alternatives: list[dict[str, Any]],
    ) -> dict[str, Any]:
        split = {
            "primary": manifest.models[0] if manifest.models else manifest.runtime_worker,
            "backend": str(manifest.backend),
            "location": str(manifest.location),
            "privacy_mode": signals.privacy_mode,
            "strategy": "single_worker",
        }
        if manifest.worker_id == "cloud-planner-verifier":
            split["strategy"] = "cloud_plan_local_execute"
            split["handoff"] = "Use cloud for planning/verification and keep artifacts in Zyra event log."
        elif manifest.worker_id == "edge-browser-worker":
            split["strategy"] = "edge_browser_local_trace"
            split["handoff"] = "Use simulated edge browser session, then persist DOM/screenshot artifacts locally."
        elif manifest.worker_id == "local-memory-curator":
            split["strategy"] = "memory_restore_then_route"
            split["handoff"] = "Use MemoryFabric/trajectory records before rerouting execution."
        elif any(item["worker_id"] == "cloud-planner-verifier" for item in alternatives) and signals.model_pressure == "high":
            split["secondary"] = "cloud-planner-verifier"
            split["strategy"] = "local_execute_cloud_verify_candidate"
        return split


def _event_dict(event: EventRecord | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(event, EventRecord):
        return {
            "event_id": event.event_id,
            "event_type": str(event.event_type),
            "payload": event.payload,
            "node_id": event.node_id,
        }
    return dict(event)


def _operator_selection_input(
    value: Any | None,
    *,
    run_id: str,
    task_id: str,
) -> dict[str, Any] | None:
    if value is None:
        return None
    to_dict = getattr(value, "to_dict", None)
    raw = to_dict() if callable(to_dict) else value
    if not isinstance(raw, Mapping):
        raise ValueError("operator_input must be a mapping or typed scheduler input")
    data = dict(raw)
    if data.get("schema_version") != "zyra.operator-scheduler-input/v1":
        raise ValueError("operator_input schema is unsupported")
    if data.get("diagnostic_only") is True:
        raise ValueError("diagnostic operator ranking cannot enter ResourceScheduler")
    if data.get("placement_owner") != "ResourceScheduler":
        raise ValueError("operator_input cannot replace ResourceScheduler ownership")
    if data.get("run_id") != run_id or data.get("task_id") != task_id:
        raise ValueError("operator_input belongs to another run or task")
    if not str(data.get("proposal_id") or "").strip():
        raise ValueError("operator_input proposal_id is required")
    proposal_digest = str(data.get("proposal_digest") or "")
    catalog_digest = str(data.get("catalog_digest") or "")
    graph_signature = str(data.get("committed_graph_signature") or "")
    if any(
        len(item) != 64
        or any(character not in "0123456789abcdef" for character in item.lower())
        for item in (proposal_digest, catalog_digest, graph_signature)
    ):
        raise ValueError("operator_input digests and graph signature must be SHA-256")
    candidates = data.get("candidate_references")
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("operator_input requires concrete candidate references")
    for candidate in candidates:
        if (
            not isinstance(candidate, Mapping)
            or not str(candidate.get("operator_id") or "").strip()
            or not str(candidate.get("version") or "").strip()
        ):
            raise ValueError("operator_input candidate reference is invalid")
    return data


def _task_text(
    state: TaskState,
    *,
    node: PlanNode | None,
    cause_event: Mapping[str, Any] | None,
) -> str:
    parts = [state.user_goal, str(state.constraints.objectives), str(state.constraints.requirements)]
    hints = state.metadata.get("runtime_hints")
    if isinstance(hints, Mapping):
        parts.append(str(hints))
    if node is not None:
        parts.extend([node.title, node.description, node.summary, str(node.metadata)])
    if cause_event is not None:
        parts.append(str(cause_event.get("payload") or ""))
    return "\n".join(parts).lower()


def _required_tools(state: TaskState, *, node: PlanNode | None, hints: Mapping[str, Any]) -> list[str]:
    tools: list[str] = []
    tools.extend(state.constraints.required_tools)
    if node is not None:
        tools.extend(node.constraints.required_tools)
    for plan_name, key_name in [("tool_plan", "tool_name"), ("browser_plan", "action")]:
        plan = hints.get(plan_name)
        if isinstance(plan, list):
            for item in plan:
                if isinstance(item, Mapping) and item.get(key_name):
                    tools.append(str(item[key_name]))
    return sorted(set(tools))


def _failure_workers_from_state_and_events(
    state: TaskState,
    events: Sequence[Mapping[str, Any]],
    memory_records: Sequence[Any],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in state.plan_nodes.values():
        if str(node.status) == "failed" and node.assigned_worker_id:
            _increment(counts, str(node.assigned_worker_id))
    for collection in [state.metadata.get("failure_injections", []), events, memory_records]:
        for item in collection if isinstance(collection, list | tuple) else []:
            text = str(item).lower()
            if any(word in text for word in ["failure", "failed", "timeout", "crash", "denied"]):
                _count_worker_mentions(counts, text)
    return counts


def _task_profile(text: str, required_tools: Sequence[str]) -> str:
    tool_text = " ".join(required_tools).lower()
    if _contains_url(text) or _has_any(text + tool_text, ["browser", "dom", "screenshot", "open_url", "extract_text"]):
        return "browser"
    if _has_any(text + tool_text, ["memory", "compact", "trajectory", "checkpoint", "replay"]):
        return "memory"
    if _has_any(text, ["verify", "plan", "reason", "推理", "规划", "验证", "replan"]):
        return "planning"
    if _has_any(text + tool_text, ["code", "repo", "file", "patch", "test", "python", "typescript", "shell"]):
        return "code"
    return "general"


def _model_pressure(text: str, state: TaskState, node: PlanNode | None) -> str:
    long_context = len(text) > 4000 or len(state.plan_nodes) > 8
    hard_words = _has_any(text, ["multi-agent", "heterogeneous", "long-horizon", "deep reasoning", "复杂", "长程", "协同"])
    if long_context or hard_words or (node is not None and str(node.metadata.get("stage")) in {"verify", "replan", "recover"}):
        return "high"
    return "balanced"


def _contains_url(text: str) -> bool:
    for token in text.split():
        candidate = token.strip(".,;()[]{}<>\"'")
        parsed = urlparse(candidate)
        if parsed.scheme in {"http", "https", "file"}:
            return True
    return False


def _has_any(text: str, needles: Sequence[str]) -> bool:
    return any(needle in text for needle in needles)


def _count_metadata_list(state: TaskState, key: str) -> int:
    value = state.metadata.get(key)
    return len(value) if isinstance(value, list) else 0


def _event_count(events: Sequence[Mapping[str, Any]], event_type: str) -> int:
    return sum(1 for event in events if str(event.get("event_type") or "") == event_type)


def _text_count(items: Sequence[Any], needle: str) -> int:
    return sum(1 for item in items if needle in str(item).lower())


def _count_worker_mentions(counts: dict[str, int], text: str) -> None:
    if "browser" in text:
        _increment(counts, "BrowserWorker")
        _increment(counts, "edge-browser-worker")
    if "code" in text or "shell" in text or "tool" in text:
        _increment(counts, "CodeWorkerRuntime")
        _increment(counts, "local-code-worker")
    if "cloud" in text or "model" in text:
        _increment(counts, "cloud-planner-verifier")
    if "memory" in text or "compact" in text or "trajectory" in text:
        _increment(counts, "local-memory-curator")


def _increment(counts: dict[str, int], key: str) -> None:
    counts[key] = counts.get(key, 0) + 1


def _keywords(text: str, limit: int = 24) -> list[str]:
    seen: list[str] = []
    for word in re.findall(r"[\w\u4e00-\u9fff]{2,}", text.lower()):
        if word not in seen:
            seen.append(word)
        if len(seen) >= limit:
            break
    return seen
