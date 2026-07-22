from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from .component_runtime import RecoveryComponent, RecoveryComponentControl
from .contracts import RecoverySignal, RecoverySource, stable_digest, utc_now
from .signal_classifier import RecoverySignalClassificationError, RecoverySignalClassifier


class RecoveryIngressError(ValueError):
    pass


class RecoveryIngressRejected(RecoveryIngressError):
    def __init__(self, domain: "ObservationDomain", code: str, message: str) -> None:
        self.domain = domain
        self.code = code
        super().__init__(message)


class ObservationDomain(StrEnum):
    PERMISSION = "permission"
    MCP = "mcp"
    SESSION = "session"
    COMPACT = "compact"
    API = "api"
    PROVIDER = "provider"
    WORKER = "worker"
    BACKEND = "backend"
    SUBAGENT = "subagent"
    WORKSPACE = "workspace"
    TOOL = "tool"
    CONTROL = "control"
    CHECKPOINT = "checkpoint"
    WATCHDOG = "watchdog"
    OMP = "omp"


@dataclass(frozen=True, slots=True)
class RecoveryObservation:
    domain: ObservationDomain
    payload: Mapping[str, Any]
    owner: str
    owner_revision: str = ""
    event_ids: tuple[str, ...] = ()
    span_id: str = ""
    observed_at: str = field(default_factory=utc_now)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        return stable_digest({
            "domain": self.domain.value,
            "owner": self.owner,
            "owner_revision": self.owner_revision,
            "event_ids": list(self.event_ids),
            "span_id": self.span_id,
            "payload": dict(self.payload),
        })

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-observation/v1",
            "domain": self.domain.value,
            "owner": self.owner,
            "owner_revision": self.owner_revision,
            "event_ids": list(self.event_ids),
            "span_id": self.span_id,
            "observed_at": self.observed_at,
            "payload": copy.deepcopy(dict(self.payload)),
            "metadata": copy.deepcopy(dict(self.metadata)),
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class RecoveryAdmission:
    observation: RecoveryObservation
    signal: RecoverySignal
    classifier_rule: str
    state_families: tuple[str, ...]
    admitted_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-admission/v1",
            "observation": self.observation.to_dict(),
            "signal": self.signal.to_dict(),
            "classifier_rule": self.classifier_rule,
            "state_families": list(self.state_families),
            "admitted_at": self.admitted_at,
        }


@dataclass(frozen=True, slots=True)
class RecoveryAdmissionBatch:
    admissions: tuple[RecoveryAdmission, ...]
    rejected: tuple[Mapping[str, Any], ...]
    duplicate_digests: tuple[str, ...]

    @property
    def run_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.signal.refs.run_id for item in self.admissions))

    @property
    def task_ids(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.signal.refs.task_id for item in self.admissions))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-admission-batch/v1",
            "admissions": [item.to_dict() for item in self.admissions],
            "rejected": [copy.deepcopy(dict(item)) for item in self.rejected],
            "duplicate_digests": list(self.duplicate_digests),
            "run_ids": list(self.run_ids),
            "task_ids": list(self.task_ids),
        }


ObservationValidator = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class RecoveryIngressRuntime:
    OWNER_BY_DOMAIN: Mapping[ObservationDomain, str] = {
        ObservationDomain.PERMISSION: "PermissionControlPlane",
        ObservationDomain.MCP: "McpControlRuntime",
        ObservationDomain.SESSION: "SessionLifecycleRuntime",
        ObservationDomain.COMPACT: "MemoryFabric",
        ObservationDomain.API: "QueryEngine",
        ObservationDomain.PROVIDER: "ProviderControlPlane",
        ObservationDomain.WORKER: "WorkerPoolFoundationRuntime",
        ObservationDomain.BACKEND: "BackendRegistry",
        ObservationDomain.SUBAGENT: "AgentTaskRuntime",
        ObservationDomain.WORKSPACE: "WorkspaceManager",
        ObservationDomain.TOOL: "ToolRegistry",
        ObservationDomain.CONTROL: "GraphControlRuntime",
        ObservationDomain.CHECKPOINT: "RecoveryPlanStore",
        ObservationDomain.WATCHDOG: "FaultStateStore",
        ObservationDomain.OMP: "OmpRecoveryReceiptRuntime",
    }
    SOURCE_BY_DOMAIN: Mapping[ObservationDomain, RecoverySource] = {
        ObservationDomain.PERMISSION: RecoverySource.PERMISSION_RUNTIME,
        ObservationDomain.MCP: RecoverySource.MCP_RUNTIME,
        ObservationDomain.SESSION: RecoverySource.SESSION_RUNTIME,
        ObservationDomain.COMPACT: RecoverySource.COMPACT_RUNTIME,
        ObservationDomain.API: RecoverySource.API_RUNTIME,
        ObservationDomain.PROVIDER: RecoverySource.PROVIDER_RUNTIME,
        ObservationDomain.WORKER: RecoverySource.WORKER_HANDOFF,
        ObservationDomain.BACKEND: RecoverySource.BACKEND_RUNTIME,
        ObservationDomain.SUBAGENT: RecoverySource.SUBAGENT_RUNTIME,
        ObservationDomain.WORKSPACE: RecoverySource.WORKSPACE_RUNTIME,
        ObservationDomain.TOOL: RecoverySource.TOOL_RUNTIME,
        ObservationDomain.CONTROL: RecoverySource.CONTROL_RUNTIME,
        ObservationDomain.CHECKPOINT: RecoverySource.CHECKPOINT_RUNTIME,
        ObservationDomain.WATCHDOG: RecoverySource.WATCHDOG_HANDOFF,
        ObservationDomain.OMP: RecoverySource.OMP_SUPPLEMENT,
    }
    STATE_FAMILIES: Mapping[ObservationDomain, tuple[str, ...]] = {
        ObservationDomain.PERMISSION: ("permission", "tool"),
        ObservationDomain.MCP: ("mcp", "tool", "session"),
        ObservationDomain.SESSION: ("session", "checkpoint"),
        ObservationDomain.COMPACT: ("session", "memory", "checkpoint"),
        ObservationDomain.API: ("provider", "session", "failure_history"),
        ObservationDomain.PROVIDER: ("provider", "failure_history"),
        ObservationDomain.WORKER: ("worker", "checkpoint", "failure_history"),
        ObservationDomain.BACKEND: ("backend", "workspace", "worker"),
        ObservationDomain.SUBAGENT: ("worker", "graph", "memory"),
        ObservationDomain.WORKSPACE: ("workspace", "artifact", "graph"),
        ObservationDomain.TOOL: ("tool", "artifact", "checkpoint"),
        ObservationDomain.CONTROL: ("graph", "requirement", "artifact"),
        ObservationDomain.CHECKPOINT: ("checkpoint", "graph", "session"),
        ObservationDomain.WATCHDOG: ("failure_history", "worker", "checkpoint"),
        ObservationDomain.OMP: ("provider", "checkpoint", "failure_history"),
    }

    def __init__(
        self,
        classifier: RecoverySignalClassifier,
        *,
        components: RecoveryComponentControl | None = None,
    ) -> None:
        self.classifier = classifier
        self.components = components or RecoveryComponentControl()
        self._validators: dict[ObservationDomain, ObservationValidator] = {
            ObservationDomain.PERMISSION: self._validate_permission,
            ObservationDomain.MCP: self._validate_mcp,
            ObservationDomain.SESSION: self._validate_session,
            ObservationDomain.COMPACT: self._validate_compact,
            ObservationDomain.API: self._validate_api,
            ObservationDomain.PROVIDER: self._validate_provider,
            ObservationDomain.WORKER: self._validate_worker,
            ObservationDomain.BACKEND: self._validate_backend,
            ObservationDomain.SUBAGENT: self._validate_subagent,
            ObservationDomain.WORKSPACE: self._validate_workspace,
            ObservationDomain.TOOL: self._validate_tool,
            ObservationDomain.CONTROL: self._validate_control,
            ObservationDomain.CHECKPOINT: self._validate_checkpoint,
            ObservationDomain.WATCHDOG: self._validate_watchdog,
            ObservationDomain.OMP: self._validate_omp,
        }

    def observe(
        self,
        domain: ObservationDomain | str,
        payload: Mapping[str, Any],
        *,
        owner: str = "",
        owner_revision: str = "",
        event_ids: Sequence[str] = (),
        span_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> RecoveryAdmission:
        selected = ObservationDomain(str(domain))
        self.components.require(
            RecoveryComponent.CLASSIFIER,
            operation=f"admit {selected.value} recovery observation",
        )
        if selected is ObservationDomain.OMP:
            self.components.require(
                RecoveryComponent.OMP_NORMALIZER,
                operation="admit OMP supplementary receipt",
            )
        normalized = dict(self._validators[selected](self._mapping(payload)))
        expected_owner = self.OWNER_BY_DOMAIN[selected]
        actual_owner = str(owner or normalized.pop("owner", "") or expected_owner)
        if actual_owner != expected_owner:
            raise RecoveryIngressRejected(
                selected,
                "owner_mismatch",
                f"{selected.value} observation owner must be {expected_owner}, got {actual_owner}",
            )
        embedded_events = normalized.get("evidence_event_ids") or ()
        combined_events = self._identities("event id", (*event_ids, *embedded_events), optional=True)
        if combined_events:
            normalized["evidence_event_ids"] = list(combined_events)
        observation = RecoveryObservation(
            domain=selected,
            payload=copy.deepcopy(normalized),
            owner=actual_owner,
            owner_revision=str(owner_revision or normalized.get("owner_revision") or ""),
            event_ids=combined_events,
            span_id=self._identity("span id", span_id or str(normalized.get("span_id") or ""), optional=True),
            observed_at=str(normalized.get("observed_at") or normalized.get("created_at") or utc_now()),
            metadata=copy.deepcopy(dict(metadata or {})),
        )
        signal = self._classify(selected, normalized)
        details = dict(signal.details)
        rule = str(details.get("classification_rule") or "")
        return RecoveryAdmission(
            observation=observation,
            signal=signal,
            classifier_rule=rule,
            state_families=self.STATE_FAMILIES[selected],
        )

    def batch(
        self,
        observations: Sequence[Mapping[str, Any]],
        *,
        reject_entire_batch_on_scope_mismatch: bool = True,
    ) -> RecoveryAdmissionBatch:
        admissions: list[RecoveryAdmission] = []
        rejected: list[dict[str, Any]] = []
        duplicates: list[str] = []
        digests: set[str] = set()
        for index, raw in enumerate(observations):
            value = self._mapping(raw)
            try:
                domain = ObservationDomain(str(value.get("domain") or ""))
                admission = self.observe(
                    domain,
                    self._mapping(value.get("payload") or value),
                    owner=str(value.get("owner") or ""),
                    owner_revision=str(value.get("owner_revision") or ""),
                    event_ids=tuple(value.get("event_ids") or ()),
                    span_id=str(value.get("span_id") or ""),
                    metadata=dict(value.get("metadata") or {}),
                )
            except Exception as error:
                rejected.append({
                    "index": index,
                    "error_type": type(error).__name__,
                    "message": str(error)[:2000],
                })
                continue
            digest = admission.observation.digest
            if digest in digests:
                duplicates.append(digest)
                continue
            digests.add(digest)
            admissions.append(admission)
        scopes = {(item.signal.refs.run_id, item.signal.refs.task_id) for item in admissions}
        if reject_entire_batch_on_scope_mismatch and len(scopes) > 1:
            raise RecoveryIngressError("a recovery observation batch cannot cross run/task custody")
        admissions.sort(key=lambda item: (
            item.signal.observed_at,
            item.observation.domain.value,
            item.observation.digest,
        ))
        return RecoveryAdmissionBatch(tuple(admissions), tuple(rejected), tuple(duplicates))

    def permission(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.PERMISSION, payload)

    def mcp(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.MCP, payload)

    def api(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.API, payload)

    def worker(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.WORKER, payload)

    def backend(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.BACKEND, payload)

    def provider(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.PROVIDER, payload)

    def subagent(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.SUBAGENT, payload)

    def omp(self, payload: Mapping[str, Any]) -> RecoveryAdmission:
        return self.observe(ObservationDomain.OMP, payload)

    def contract(self) -> dict[str, Any]:
        return {
            "schema": "zyra.recovery-ingress-contract/v1",
            "domains": {
                domain.value: {
                    "owner": self.OWNER_BY_DOMAIN[domain],
                    "source": self.SOURCE_BY_DOMAIN[domain].value,
                    "state_families": list(self.STATE_FAMILIES[domain]),
                }
                for domain in ObservationDomain
            },
            "classifier_owner": type(self.classifier).__name__,
            "identity_from_free_text": False,
            "action_selected_at_ingress": False,
            "omp_supplementary_only": True,
        }

    def _classify(self, domain: ObservationDomain, payload: Mapping[str, Any]) -> RecoverySignal:
        try:
            if domain is ObservationDomain.PERMISSION:
                return self.classifier.classify_permission(payload)
            if domain is ObservationDomain.API:
                return self.classifier.classify_api_error(payload)
            if domain is ObservationDomain.WORKER and str(payload.get("schema") or "") == "zyra.worker-recovery-evidence/v1":
                return self.classifier.from_worker_evidence(payload)
            if domain is ObservationDomain.WATCHDOG and str(payload.get("schema") or "").startswith("zyra.watchdog"):
                return self.classifier.from_fault_handoff(payload)
            if domain is ObservationDomain.OMP:
                return self.classifier.from_omp_receipt(payload)
            return self.classifier.classify(payload, source=self.SOURCE_BY_DOMAIN[domain])
        except RecoverySignalClassificationError as error:
            raise RecoveryIngressRejected(domain, "classification_rejected", str(error)) from error

    def _validate_permission(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        effect = str(value.get("effect") or value.get("status") or "").casefold()
        phase = str(value.get("phase") or "").casefold()
        if effect not in {"ask", "pending", "deny", "denied", "rejected"} and phase not in {"created", "delivered"}:
            raise RecoveryIngressRejected(ObservationDomain.PERMISSION, "unsupported_effect", "permission effect is not ask/pending/deny")
        refs = self._refs(value)
        self._require_any(refs, "tool_call_id", "request_id", "permission_request_id")
        value["refs"] = refs
        value["effect"] = effect
        value["tool_dispatch_allowed"] = False
        return value

    def _validate_mcp(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        state = str(value.get("state") or value.get("source_kind") or value.get("kind") or "").casefold()
        aliases = {
            "needs_auth": "mcp_auth_required",
            "auth_required": "mcp_auth_required",
            "disconnected": "mcp_disconnected",
            "reconnect_exhausted": "mcp_disconnected",
            "breaker_open": "mcp_disconnected",
        }
        source_kind = aliases.get(state, state)
        if source_kind not in {"mcp_auth_required", "mcp_disconnected"}:
            raise RecoveryIngressRejected(ObservationDomain.MCP, "unsupported_state", "MCP state must require auth or reconnect")
        refs = self._refs(value)
        self._require_any(refs, "mcp_server_id", "request_id")
        value.update({"source_kind": source_kind, "refs": refs})
        details = dict(value.get("details") or {})
        details.update({
            "breaker_state": str(value.get("breaker_state") or details.get("breaker_state") or ""),
            "reconnect_attempt": self._non_negative(value.get("reconnect_attempt") or details.get("reconnect_attempt") or 0, "reconnect_attempt"),
            "elicitation_pending": bool(value.get("elicitation_pending", details.get("elicitation_pending", False))),
        })
        value["details"] = details
        return value

    def _validate_session(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        source_kind = str(value.get("source_kind") or value.get("kind") or "compact_needed").casefold()
        if source_kind not in {"prompt_too_long", "compact_needed"}:
            raise RecoveryIngressRejected(ObservationDomain.SESSION, "unsupported_state", "session recovery requires compact or prompt overflow state")
        refs = self._refs(value)
        self._require_any(refs, "session_id", "turn_id")
        value.update({"source_kind": source_kind, "refs": refs})
        return value

    def _validate_compact(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._validate_session(payload)
        details = dict(value.get("details") or {})
        details.update({
            "compact_generation": self._non_negative(value.get("compact_generation") or details.get("compact_generation") or 0, "compact_generation"),
            "restore_required": bool(value.get("restore_required", True)),
            "preserve_pending_requests": bool(value.get("preserve_pending_requests", True)),
        })
        value["details"] = details
        return value

    def _validate_api(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        error_kind = str(value.get("error_kind") or value.get("code") or value.get("source_kind") or "").casefold()
        allowed = {
            "prompt_too_long", "context_overflow", "retry_exhausted", "api_retry_exhausted",
            "stream_stall", "stream_idle_timeout", "stream_interrupted_after_output",
            "rate_limited", "provider_unavailable", "credential_exhausted",
        }
        if error_kind not in allowed:
            raise RecoveryIngressRejected(ObservationDomain.API, "unsupported_error", f"unsupported API recovery error: {error_kind}")
        refs = self._refs(value)
        self._require_any(refs, "request_id", "turn_id", "provider_id")
        value.update({"error_kind": error_kind, "refs": refs})
        value["attempt_count"] = self._non_negative(value.get("attempt_count") or value.get("retry_attempt") or 0, "attempt_count")
        if bool(value.get("partial_output")) and not refs.get("response_id"):
            raise RecoveryIngressRejected(ObservationDomain.API, "response_id_missing", "partial API output requires response_id")
        return value

    def _validate_provider(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        kind = str(value.get("source_kind") or value.get("kind") or value.get("error_kind") or "").casefold()
        aliases = {"429": "rate_limited", "revoked": "credential_exhausted", "route_unavailable": "provider_unavailable"}
        kind = aliases.get(kind, kind)
        if kind not in {"rate_limited", "provider_unavailable", "credential_exhausted", "api_retry_exhausted", "stream_stall"}:
            raise RecoveryIngressRejected(ObservationDomain.PROVIDER, "unsupported_error", "provider receipt is not a routable failure")
        refs = self._refs(value)
        self._require_any(refs, "provider_id", "provider_route_id", "request_id", "credential_id")
        value.update({"source_kind": kind, "refs": refs})
        details = dict(value.get("details") or {})
        details["credential_material_present"] = False
        details["route_lease_required"] = True
        details["prior_route_checksum"] = str(value.get("route_checksum") or details.get("route_checksum") or "")
        value["details"] = details
        return value

    def _validate_worker(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        if str(value.get("schema") or "") == "zyra.worker-recovery-evidence/v1":
            return value
        kind = str(value.get("source_kind") or value.get("kind") or value.get("signal_kind") or "").casefold()
        aliases = {"heartbeat_stale": "worker_heartbeat_stale", "lost": "worker_lost", "lease_expired": "worker_lease_expired"}
        kind = aliases.get(kind, kind)
        if kind not in {"worker_heartbeat_stale", "worker_lost", "worker_lease_expired"}:
            raise RecoveryIngressRejected(ObservationDomain.WORKER, "unsupported_signal", "worker receipt is not a lifecycle recovery signal")
        refs = self._refs(value)
        self._require_any(refs, "worker_id", "worker_lease_id", "attempt_id")
        value.update({"source_kind": kind, "refs": refs})
        return value

    def _validate_backend(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        kind = str(value.get("source_kind") or value.get("kind") or "backend_unavailable").casefold()
        if kind != "backend_unavailable":
            raise RecoveryIngressRejected(ObservationDomain.BACKEND, "unsupported_signal", "backend recovery only accepts unavailable receipts")
        refs = self._refs(value)
        self._require_any(refs, "backend_id", "backend_lease_id", "worker_id")
        value.update({"source_kind": kind, "refs": refs})
        details = dict(value.get("details") or {})
        details["workspace_rebind_required"] = bool(value.get("workspace_rebind_required", True))
        value["details"] = details
        return value

    def _validate_subagent(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        kind = str(value.get("source_kind") or value.get("kind") or value.get("state") or "").casefold()
        aliases = {"failed": "subagent_failed", "cancelled": "subagent_cancelled"}
        kind = aliases.get(kind, kind)
        if kind not in {"subagent_failed", "subagent_cancelled"}:
            raise RecoveryIngressRejected(ObservationDomain.SUBAGENT, "unsupported_signal", "subagent receipt must be failed or cancelled")
        refs = self._refs(value)
        self._require_any(refs, "subagent_id", "worker_id", "attempt_id")
        value.update({"source_kind": kind, "refs": refs})
        return value

    def _validate_workspace(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        refs = self._refs(value)
        self._require_any(refs, "workspace_id", "subagent_id", "node_id")
        conflicts = self._safe_paths(value.get("conflict_paths") or (value.get("details") or {}).get("conflict_paths") or ())
        dirty = self._safe_paths(value.get("dirty_paths") or (value.get("details") or {}).get("dirty_paths") or ())
        if not conflicts and not dirty:
            raise RecoveryIngressRejected(ObservationDomain.WORKSPACE, "evidence_missing", "workspace conflict requires conflict or dirty WIP paths")
        value.update({"source_kind": "workspace_conflict", "refs": refs})
        value["details"] = {**dict(value.get("details") or {}), "conflict_paths": list(conflicts), "dirty_paths": list(dirty)}
        return value

    def _validate_tool(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        kind = str(value.get("source_kind") or value.get("kind") or "tool_error").casefold()
        if kind not in {"tool_error", "tool_timeout"}:
            raise RecoveryIngressRejected(ObservationDomain.TOOL, "unsupported_signal", "tool recovery accepts error or timeout")
        refs = self._refs(value)
        self._require_any(refs, "tool_call_id", "node_id")
        value.update({"source_kind": kind, "refs": refs})
        if bool(value.get("tool_effect_committed")):
            value["observable_side_effect"] = True
        return value

    def _validate_control(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        kind = str(value.get("source_kind") or value.get("kind") or "requirement_changed").casefold()
        if kind != "requirement_changed":
            raise RecoveryIngressRejected(ObservationDomain.CONTROL, "unsupported_control", "only requirement change enters recovery control ingress")
        refs = self._refs(value)
        self._require_any(refs, "node_id", "request_id")
        details = dict(value.get("details") or {})
        if not str(details.get("requirement_revision") or value.get("requirement_revision") or ""):
            raise RecoveryIngressRejected(ObservationDomain.CONTROL, "revision_missing", "requirement change requires a revision")
        details["control_reason"] = True
        details["increment_failure_counter"] = False
        value.update({"source_kind": kind, "refs": refs, "details": details})
        return value

    def _validate_checkpoint(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        value = self._scope(payload)
        refs = self._refs(value)
        self._require_any(refs, "checkpoint_id", "graph_id")
        value.update({"source_kind": "checkpoint_diverged", "refs": refs})
        details = dict(value.get("details") or {})
        details["unsafe_resume_rejected"] = True
        value["details"] = details
        return value

    def _validate_watchdog(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        schema = str(payload.get("schema") or "")
        if schema and not schema.startswith("zyra.watchdog") and schema != "zyra.fault-recovery-handoff/v1":
            raise RecoveryIngressRejected(ObservationDomain.WATCHDOG, "schema_rejected", f"unsupported watchdog schema: {schema}")
        return self._scope(payload)

    def _validate_omp(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        schema = str(payload.get("schema") or "")
        if schema != "zyra.omp-recovery-receipt/v1":
            raise RecoveryIngressRejected(ObservationDomain.OMP, "schema_rejected", "OMP receipt schema must be zyra.omp-recovery-receipt/v1")
        value = self._scope(payload)
        provenance = self._mapping(value.get("provenance") or {})
        if bool(provenance.get("applied_action_selected_here", False)):
            raise RecoveryIngressRejected(ObservationDomain.OMP, "second_owner", "OMP receipt cannot select the applied action")
        decision_owner = str(provenance.get("canonical_policy_owner") or provenance.get("decision_owner") or "python.RecoveryDecisionRuntime")
        if decision_owner != "python.RecoveryDecisionRuntime":
            raise RecoveryIngressRejected(ObservationDomain.OMP, "owner_mismatch", "OMP receipt changed the canonical policy owner")
        refs = self._refs(value)
        self._require_any(refs, "request_id", "worker_id", "provider_id", "mcp_server_id", "workspace_id")
        value["refs"] = refs
        return value

    @classmethod
    def _scope(cls, payload: Mapping[str, Any]) -> dict[str, Any]:
        value = copy.deepcopy(dict(payload))
        refs = cls._refs(value)
        cls._identity("run id", str(refs.get("run_id") or ""))
        cls._identity("task id", str(refs.get("task_id") or ""))
        value["refs"] = refs
        return value

    @staticmethod
    def _refs(payload: Mapping[str, Any]) -> dict[str, Any]:
        refs = copy.deepcopy(dict(payload.get("refs") or {}))
        fields = (
            "run_id", "task_id", "session_id", "node_id", "turn_id", "tool_call_id",
            "request_id", "response_id", "worker_id", "attempt_id", "worker_lease_id",
            "backend_id", "backend_lease_id", "provider_id", "provider_route_id",
            "credential_id", "transport_id", "workspace_id", "checkpoint_id",
            "permission_request_id", "mcp_server_id", "subagent_id", "graph_id", "graph_revision",
        )
        for name in fields:
            if name not in refs and payload.get(name) not in {None, ""}:
                refs[name] = payload[name]
        return refs

    @staticmethod
    def _require_any(refs: Mapping[str, Any], *names: str) -> None:
        if not any(refs.get(name) not in {None, "", 0} for name in names):
            raise RecoveryIngressError("observation requires one structured ref from: " + ", ".join(names))

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise RecoveryIngressError("recovery observation must be an object")
        return dict(value)

    @staticmethod
    def _identity(name: str, value: str, *, optional: bool = False) -> str:
        candidate = str(value or "").strip()
        if optional and not candidate:
            return ""
        if not candidate:
            raise RecoveryIngressError(f"{name} is required")
        if len(candidate) > 512 or any(character.isspace() for character in candidate):
            raise RecoveryIngressError(f"{name} is invalid")
        return candidate

    @classmethod
    def _identities(cls, name: str, values: Sequence[Any], *, optional: bool = False) -> tuple[str, ...]:
        result: list[str] = []
        for value in values:
            candidate = cls._identity(name, str(value), optional=optional)
            if candidate and candidate not in result:
                result.append(candidate)
        return tuple(result)

    @staticmethod
    def _non_negative(value: Any, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as error:
            raise RecoveryIngressError(f"{name} must be an integer") from error
        if result < 0:
            raise RecoveryIngressError(f"{name} must be non-negative")
        return result

    @classmethod
    def _safe_paths(cls, values: Sequence[Any]) -> tuple[str, ...]:
        result: list[str] = []
        for raw in values:
            path = cls._identity("workspace path", str(raw)).replace("\\", "/")
            if path.startswith("/") or ":/" in path or any(part in {"", ".", ".."} for part in path.split("/")):
                raise RecoveryIngressError("workspace path escapes the owned workspace")
            if path not in result:
                result.append(path)
        return tuple(sorted(result))


def recovery_ingress_contract() -> dict[str, Any]:
    return {
        "schema": "zyra.recovery-ingress-surface/v1",
        "input_domains": [item.value for item in ObservationDomain],
        "classifier": "RecoverySignalClassifier",
        "decision_owner": "RecoveryDecisionRuntime",
        "free_text_identity_inference": False,
        "owner_receipt_validation": True,
        "batch_scope": "one run/task",
    }


__all__ = [
    "ObservationDomain",
    "RecoveryAdmission",
    "RecoveryAdmissionBatch",
    "RecoveryIngressError",
    "RecoveryIngressRejected",
    "RecoveryIngressRuntime",
    "RecoveryObservation",
    "recovery_ingress_contract",
]
