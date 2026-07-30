from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from zyra_evaluation.policy_benchmark.contracts import canonical_digest
from zyra_evaluation.policy_benchmark.metric_specs import (
    metric_spec_registry_payload,
)
from zyra_orchestration.topology_policy.contracts import parse_policy_contract


_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$")
_EVIDENCE_CURSOR_SCHEMA = "zyra.policy-evidence-cursor/v1"
_EVIDENCE_PROJECTION_SCHEMA = "zyra.policy-evidence-projection/v1"
_MAX_EVIDENCE_LIMIT = 200
_MAX_SOURCE_PAGE = 1_000
_MAX_ARTIFACT_BYTES = 8 * 1024 * 1024
_CURSOR_NAMESPACE = "zyra.policy-evidence.cursor.v1"


class PolicyMetricReportProvider(Protocol):
    def get_report(self, report_id: str) -> Mapping[str, Any] | None: ...


class PolicyEvidenceSource(Protocol):
    def health(self) -> Mapping[str, Any]: ...

    def earliest_sequence(self) -> int: ...

    def page(
        self,
        *,
        after_sequence: int,
        limit: int,
        task_id: str,
    ) -> Mapping[str, Any]: ...

    def contract_for_event(
        self,
        event: Mapping[str, Any],
    ) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class PolicyApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


class PolicyEvidenceProjectionError(ValueError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class FilesystemPolicyMetricReportProvider:
    """Read-only projection of metric artifacts written by evaluation owners."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def get_report(self, report_id: str) -> Mapping[str, Any] | None:
        if not _REPORT_ID.fullmatch(str(report_id or "")):
            return None
        selected = (self.root / f"{report_id}.json").resolve()
        if selected.parent != self.root or not selected.is_file():
            return None
        try:
            payload = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "schema_version": "zyra.policy-metric-projection-error/v1",
                "error": "metric_report_unreadable",
                "report_id": report_id,
            }
        if not isinstance(payload, Mapping):
            return {
                "schema_version": "zyra.policy-metric-projection-error/v1",
                "error": "metric_report_invalid",
                "report_id": report_id,
            }
        supplied = str(payload.get("digest") or "")
        body = {key: value for key, value in payload.items() if key != "digest"}
        if not supplied or canonical_digest(body) != supplied:
            return {
                "schema_version": "zyra.policy-metric-projection-error/v1",
                "error": "metric_report_digest_mismatch",
                "report_id": report_id,
            }
        return dict(payload)


class RuntimePolicyEvidenceSource:
    """Read canonical policy artifacts referenced by the runtime event spine."""

    def __init__(self, event_api: Any, artifact_root: Path) -> None:
        self.event_api = event_api
        self.artifact_root = artifact_root.resolve()

    def health(self) -> Mapping[str, Any]:
        try:
            result = self.event_api.health()
        except Exception as error:
            raise PolicyEvidenceProjectionError(
                503,
                "policy_evidence_adapter_disconnected",
                f"The canonical runtime event spine is unavailable: {error}",
            ) from error
        if int(result.status) != 200:
            raise PolicyEvidenceProjectionError(
                503,
                "policy_evidence_adapter_disconnected",
                "The canonical runtime event spine is unavailable.",
            )
        return dict(result.body)

    def earliest_sequence(self) -> int:
        try:
            result = self.event_api.list_events(
                {"after_sequence": 0, "limit": 1}
            )
        except Exception as error:
            raise PolicyEvidenceProjectionError(
                503,
                "policy_evidence_adapter_disconnected",
                f"The canonical runtime event query failed: {error}",
            ) from error
        events = result.body.get("events")
        if not isinstance(events, list) or not events:
            return 0
        event = events[0]
        if not isinstance(event, Mapping):
            return 0
        return max(0, int(event.get("globalSequence") or 0))

    def page(
        self,
        *,
        after_sequence: int,
        limit: int,
        task_id: str,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "after_sequence": after_sequence,
            "limit": min(_MAX_SOURCE_PAGE, max(1, limit)),
        }
        if task_id:
            params["task_id"] = task_id
        try:
            result = self.event_api.list_events(params)
        except Exception as error:
            raise PolicyEvidenceProjectionError(
                503,
                "policy_evidence_adapter_disconnected",
                f"The canonical runtime event query failed: {error}",
            ) from error
        if int(result.status) != 200:
            raise PolicyEvidenceProjectionError(
                503,
                "policy_evidence_adapter_disconnected",
                "The canonical runtime event query failed.",
            )
        return dict(result.body)

    def contract_for_event(
        self,
        event: Mapping[str, Any],
    ) -> Mapping[str, Any] | None:
        payload = _event_payload(event)
        if payload.get("schema") != "zyra.policy-contract-event/v1":
            return None
        reference = _record(payload.get("policy_artifact"))
        uri = str(reference.get("uri") or "").strip()
        expected_digest = str(reference.get("digest") or "").strip().lower()
        if not uri or not expected_digest:
            raise ValueError("policy artifact reference is incomplete")
        selected = Path(uri).resolve()
        try:
            selected.relative_to(self.artifact_root)
        except ValueError as error:
            raise ValueError("policy artifact path escapes canonical artifact root") from error
        if not selected.is_file():
            raise FileNotFoundError("policy artifact content is missing")
        if selected.stat().st_size > _MAX_ARTIFACT_BYTES:
            raise ValueError("policy artifact exceeds projection byte limit")
        raw = selected.read_bytes()
        observed_digest = hashlib.sha256(raw).hexdigest()
        if observed_digest != expected_digest:
            raise ValueError("policy artifact byte digest mismatch")
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("policy artifact is not a JSON object")
        contract = parse_policy_contract(value).to_dict()
        declared_digest = str(payload.get("policy_contract_digest") or "")
        if declared_digest and contract["digest"] != declared_digest:
            raise ValueError("policy contract digest differs from event declaration")
        return contract


class PolicyMetricApi:
    """GET-only API facade; it cannot mutate task, graph, lease, or run state."""

    def __init__(
        self,
        provider: PolicyMetricReportProvider,
        *,
        evidence_source: PolicyEvidenceSource | None = None,
    ) -> None:
        self.provider = provider
        self.evidence_source = evidence_source

    def route_get(
        self,
        parts: tuple[str, ...],
        query: Mapping[str, Any],
    ) -> PolicyApiResponse | None:
        headers = {"Cache-Control": "no-store, max-age=0"}
        if parts == ("policy", "evidence"):
            if self.evidence_source is None:
                return PolicyApiResponse(
                    status=503,
                    body=_projection_error(
                        "policy_evidence_adapter_disconnected",
                        "The canonical policy evidence adapter is not configured.",
                    ),
                    headers=headers,
                )
            try:
                body = self._evidence_projection(query)
            except PolicyEvidenceProjectionError as error:
                return PolicyApiResponse(
                    status=error.status,
                    body=_projection_error(error.code, error.message),
                    headers=headers,
                )
            return PolicyApiResponse(status=200, body=body, headers=headers)
        if parts == ("policy", "metrics", "specs"):
            return PolicyApiResponse(
                status=200,
                body=metric_spec_registry_payload(),
                headers=headers,
            )
        if (
            len(parts) == 4
            and parts[:3] == ("policy", "metrics", "reports")
        ):
            report_id = parts[3]
            report = self.provider.get_report(report_id)
            if report is None:
                return PolicyApiResponse(
                    status=404,
                    body={
                        "schema_version": "zyra.policy-metric-projection-error/v1",
                        "error": "metric_report_not_found",
                        "report_id": report_id,
                    },
                    headers=headers,
                )
            if report.get("error"):
                return PolicyApiResponse(
                    status=409,
                    body=report,
                    headers=headers,
                )
            return PolicyApiResponse(
                status=200,
                body=report,
                headers=headers,
            )
        return None

    def _evidence_projection(
        self,
        query: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        assert self.evidence_source is not None
        filters = _evidence_filters(query)
        filter_digest = canonical_digest(filters)
        limit = _bounded_limit(query.get("limit"))
        cursor = _decode_cursor(str(query.get("cursor") or ""))
        if cursor and cursor["filter_digest"] != filter_digest:
            raise PolicyEvidenceProjectionError(
                409,
                "policy_evidence_cursor_scope_mismatch",
                "The evidence cursor belongs to another filter set.",
            )
        health = self.evidence_source.health()
        current_high = max(0, int(health.get("highWatermark") or 0))
        after_sequence = int(cursor["after_sequence"]) if cursor else 0
        snapshot_high = int(cursor["high_watermark"]) if cursor else current_high
        if after_sequence > current_high or snapshot_high > current_high:
            raise PolicyEvidenceProjectionError(
                409,
                "policy_evidence_cursor_ahead",
                "The evidence cursor is ahead of the canonical event owner.",
            )
        earliest = self.evidence_source.earliest_sequence()
        if after_sequence and earliest and after_sequence < earliest - 1:
            raise PolicyEvidenceProjectionError(
                409,
                "policy_evidence_cursor_expired",
                "The evidence cursor predates retained canonical events.",
            )

        transitions: list[Mapping[str, Any]] = []
        issues: list[Mapping[str, Any]] = []
        scan_cursor = after_sequence
        source_has_more = scan_cursor < snapshot_high
        scanned = 0
        while len(transitions) < limit and source_has_more and scanned < 5_000:
            source_page = self.evidence_source.page(
                after_sequence=scan_cursor,
                limit=min(_MAX_SOURCE_PAGE, max(100, limit * 4)),
                # The cursor is bound to the global event-spine high watermark.
                # Scan the same global sequence space even for a task drilldown;
                # otherwise an empty task-filtered page cannot advance past
                # unrelated retained events and would emit a non-terminating
                # next cursor.
                task_id="",
            )
            events = source_page.get("events")
            if not isinstance(events, list):
                raise PolicyEvidenceProjectionError(
                    503,
                    "policy_evidence_adapter_schema_incompatible",
                    "The canonical event adapter returned an incompatible page.",
                )
            if not events:
                source_has_more = False
                break
            advanced = False
            for value in events:
                if not isinstance(value, Mapping):
                    issues.append(
                        _issue(
                            "event_schema_incompatible",
                            "A canonical event was not an object.",
                            "inconsistent",
                        )
                    )
                    continue
                sequence = max(0, int(value.get("globalSequence") or 0))
                if sequence <= scan_cursor:
                    continue
                if sequence > snapshot_high:
                    source_has_more = False
                    break
                scan_cursor = sequence
                scanned += 1
                advanced = True
                try:
                    transition = _project_event(
                        value,
                        self.evidence_source.contract_for_event(value),
                    )
                except Exception as error:
                    transition = _inconsistent_transition(value, str(error))
                    issues.append(
                        _issue(
                            "evidence_integrity_inconsistent",
                            str(error),
                            "inconsistent",
                            event_id=str(value.get("eventId") or ""),
                        )
                    )
                if transition is None or not _matches(transition, filters):
                    continue
                transitions.append(transition)
                if len(transitions) >= limit:
                    break
            if not advanced:
                source_has_more = False
                break
            source_has_more = scan_cursor < snapshot_high

        metric_report = _metric_projection(
            self.provider,
            filters["report_id"],
            issues,
        )
        status = "degraded" if issues else "ready"
        next_cursor = (
            _encode_cursor(
                after_sequence=scan_cursor,
                high_watermark=snapshot_high,
                filter_digest=filter_digest,
            )
            if scan_cursor < snapshot_high
            else ""
        )
        body: dict[str, Any] = {
            "schema_version": _EVIDENCE_PROJECTION_SCHEMA,
            "projection_owner": "canonical_event_artifact_metric_read_model",
            "canonical_write_allowed": False,
            "status": status,
            "filters": filters,
            "filter_digest": filter_digest,
            "cursor": str(query.get("cursor") or ""),
            "next_cursor": next_cursor,
            "high_watermark": snapshot_high,
            "has_more": bool(next_cursor),
            "scanned": scanned,
            "transition_count": len(transitions),
            "transitions": transitions,
            "metric_report": metric_report,
            "issues": issues,
            "labels": {
                "lifecycle": [
                    "validation",
                    "diagnostic",
                    "default",
                    "baseline",
                    "retired",
                ],
                "readiness": [
                    "deterministic_ready",
                    "evidence_only",
                    "unavailable",
                ],
                "execution": ["real", "simulated", "degraded", "not_applicable"],
                "integrity": [
                    "verified",
                    "pending",
                    "missing",
                    "stale",
                    "inconsistent",
                ],
            },
            "snapshot_digest": canonical_digest(
                {
                    "filter_digest": filter_digest,
                    "high_watermark": snapshot_high,
                }
            ),
        }
        body["evidence_digest"] = canonical_digest(body)
        return body


_policy_metric_api: PolicyMetricApi | None = None
_policy_metric_api_key: tuple[str, str, int] | None = None


def get_policy_metric_api(
    project_root: Path,
    *,
    event_api: Any | None = None,
    artifact_root: Path | None = None,
) -> PolicyMetricApi:
    global _policy_metric_api, _policy_metric_api_key
    key = (
        str(project_root.resolve()),
        str(artifact_root.resolve()) if artifact_root is not None else "",
        id(getattr(event_api, "bridge", event_api)),
    )
    if _policy_metric_api is None or _policy_metric_api_key != key:
        _policy_metric_api = PolicyMetricApi(
            FilesystemPolicyMetricReportProvider(
                project_root / ".zyra" / "reports" / "policy-metrics"
            ),
            evidence_source=(
                RuntimePolicyEvidenceSource(event_api, artifact_root)
                if event_api is not None and artifact_root is not None
                else None
            ),
        )
        _policy_metric_api_key = key
    return _policy_metric_api


def reset_policy_metric_api() -> None:
    global _policy_metric_api, _policy_metric_api_key
    _policy_metric_api = None
    _policy_metric_api_key = None


def _record(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    return _record(event.get("payload") or event.get("inline"))


def _bounded_limit(value: Any) -> int:
    try:
        selected = int(value or 100)
    except (TypeError, ValueError) as error:
        raise PolicyEvidenceProjectionError(
            400,
            "policy_evidence_limit_invalid",
            "Evidence limit must be an integer.",
        ) from error
    if selected < 1 or selected > _MAX_EVIDENCE_LIMIT:
        raise PolicyEvidenceProjectionError(
            400,
            "policy_evidence_limit_invalid",
            f"Evidence limit must be between 1 and {_MAX_EVIDENCE_LIMIT}.",
        )
    return selected


def _identity(value: Any, label: str) -> str:
    selected = str(value or "").strip()
    if selected and not _IDENTITY.fullmatch(selected):
        raise PolicyEvidenceProjectionError(
            400,
            f"policy_evidence_{label}_invalid",
            f"Evidence {label.replace('_', ' ')} is invalid.",
        )
    return selected


def _evidence_filters(query: Mapping[str, Any]) -> dict[str, str]:
    return {
        "run_id": _identity(query.get("run_id"), "run_id"),
        "task_id": _identity(query.get("task_id"), "task_id"),
        "mechanism_version": _identity(
            query.get("mechanism_version"),
            "mechanism_version",
        ),
        "receipt_id": _identity(query.get("receipt_id"), "receipt_id"),
        "report_id": _identity(query.get("report_id"), "report_id"),
    }


def _encode_cursor(
    *,
    after_sequence: int,
    high_watermark: int,
    filter_digest: str,
) -> str:
    content = {
        "schema": _EVIDENCE_CURSOR_SCHEMA,
        "after_sequence": after_sequence,
        "high_watermark": high_watermark,
        "filter_digest": filter_digest,
    }
    signed = {
        **content,
        "signature": canonical_digest(
            {"namespace": _CURSOR_NAMESPACE, **content}
        ),
    }
    raw = json.dumps(
        signed,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_cursor(value: str) -> Mapping[str, Any] | None:
    selected = value.strip()
    if not selected:
        return None
    try:
        padding = "=" * (-len(selected) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(selected + padding).decode("utf-8")
        )
        if not isinstance(payload, Mapping):
            raise ValueError("cursor is not an object")
        content = {
            "schema": payload.get("schema"),
            "after_sequence": int(payload.get("after_sequence")),
            "high_watermark": int(payload.get("high_watermark")),
            "filter_digest": str(payload.get("filter_digest") or ""),
        }
        if content["schema"] != _EVIDENCE_CURSOR_SCHEMA:
            raise ValueError("cursor schema is unsupported")
        if content["after_sequence"] < 0 or content["high_watermark"] < 0:
            raise ValueError("cursor sequence is invalid")
        expected = canonical_digest(
            {"namespace": _CURSOR_NAMESPACE, **content}
        )
        if str(payload.get("signature") or "") != expected:
            raise ValueError("cursor signature mismatch")
        return content
    except Exception as error:
        raise PolicyEvidenceProjectionError(
            400,
            "policy_evidence_cursor_invalid",
            "The evidence cursor is invalid or incompatible.",
        ) from error


def _projection_error(code: str, message: str) -> Mapping[str, Any]:
    return {
        "schema_version": "zyra.policy-evidence-projection-error/v1",
        "status": "error",
        "error": code,
        "message": message,
        "canonical_write_allowed": False,
    }


def _issue(
    code: str,
    message: str,
    integrity: str,
    *,
    event_id: str = "",
) -> Mapping[str, Any]:
    return {
        "code": code,
        "message": message,
        "integrity": integrity,
        "event_id": event_id,
    }


def _metric_projection(
    provider: PolicyMetricReportProvider,
    report_id: str,
    issues: list[Mapping[str, Any]],
) -> Mapping[str, Any]:
    if not report_id:
        return {}
    report = provider.get_report(report_id)
    if report is None:
        issues.append(
            _issue(
                "metric_report_missing",
                f"Metric report {report_id} is unavailable.",
                "missing",
            )
        )
        return {"report_id": report_id, "status": "missing"}
    if report.get("error"):
        issues.append(
            _issue(
                str(report.get("error")),
                f"Metric report {report_id} failed integrity admission.",
                "inconsistent",
            )
        )
        return {
            "report_id": report_id,
            "status": "inconsistent",
            "error": report.get("error"),
        }
    return {
        "report_id": report_id,
        "status": "verified",
        "schema_version": report.get("schema_version"),
        "digest": report.get("digest"),
        "registry_digest": report.get("registry_digest"),
        "aggregate_report": report.get("aggregate_report", {}),
        "run_reports": report.get("run_reports", []),
        "scenario_reports": report.get("scenario_reports", []),
        "mechanism_reports": report.get("mechanism_reports", []),
        "requirement_metrics": report.get("requirement_metrics", {}),
        "anti_gaming": report.get("anti_gaming", {}),
    }


def _event_identity(
    event: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
) -> tuple[str, str]:
    payload = _event_payload(event)
    identity = _record(event.get("identity"))
    contract_payload = _record(contract.get("payload")) if contract else {}
    run_id = str(
        contract_payload.get("run_id")
        or payload.get("run_id")
        or payload.get("runId")
        or identity.get("runId")
        or event.get("run_id")
        or event.get("runId")
        or ""
    )
    task_id = str(
        contract_payload.get("task_id")
        or payload.get("task_id")
        or payload.get("taskId")
        or identity.get("taskId")
        or event.get("task_id")
        or event.get("taskId")
        or (
            event.get("aggregateId")
            if event.get("aggregateType") == "task"
            else ""
        )
        or ""
    )
    return run_id, task_id


def _lifecycle(mechanism_id: str, payload: Mapping[str, Any]) -> str:
    declared = str(payload.get("lifecycle") or "").lower()
    if declared in {"validation", "diagnostic", "default", "baseline", "retired"}:
        return declared
    rendered = f"{mechanism_id} {payload.get('profile', '')}".lower()
    if "retired" in rendered:
        return "retired"
    if "diagnostic" in rendered:
        return "diagnostic"
    if "baseline" in rendered or "phase1" in rendered:
        return "baseline"
    if "default" in rendered:
        return "default"
    return "validation"


def _readiness(contract_kind: str, payload: Mapping[str, Any]) -> str:
    declared = str(payload.get("status") or payload.get("readiness") or "")
    if declared in {"deterministic_ready", "evidence_only", "unavailable"}:
        return declared
    if contract_kind == "mechanism_evidence_readiness_report_ref":
        return "unavailable"
    return "unavailable"


def _reference(kind: str, ref_id: Any, route: str = "") -> Mapping[str, Any] | None:
    selected = str(ref_id or "").strip()
    if not selected:
        return None
    return {"kind": kind, "id": selected, "route": route}


def _contract_references(
    kind: str,
    payload: Mapping[str, Any],
    *,
    task_id: str,
) -> list[Mapping[str, Any]]:
    refs: list[Mapping[str, Any] | None] = []
    if kind == "topology_proposal_artifact":
        base = _record(payload.get("base_graph"))
        refs.extend(
            [
                _reference("commit", base.get("commit_id")),
                _reference("graph", base.get("graph_id")),
            ]
        )
    elif kind == "policy_decision_receipt":
        commit = _record(payload.get("graph_commit"))
        refs.extend(
            [
                _reference("proposal", payload.get("proposal_id")),
                _reference("delta", payload.get("delta_id")),
                _reference(
                    "commit",
                    commit.get("commit_id")
                    or commit.get("receipt_id")
                    or commit.get("revision"),
                ),
            ]
        )
    elif kind == "policy_outcome":
        refs.extend(
            [
                _reference("proposal", payload.get("proposal_ref")),
                _reference("constraint", payload.get("decision_ref")),
                _reference("commit", payload.get("commit_ref")),
                _reference("verifier", payload.get("verifier_result")),
            ]
        )
    elif kind == "memory_continuity_receipt":
        refs.extend(
            [
                _reference("memory_before", payload.get("before_digest")),
                _reference("memory_after", payload.get("after_digest")),
                _reference("constraint", payload.get("downstream_decision_ref")),
            ]
        )
    elif kind == "neuro_symbolic_evidence_bundle":
        proposal = _record(payload.get("proposal_ref"))
        commit = _record(payload.get("commit_or_no_commit"))
        refs.extend(
            [
                _reference(
                    "proposal",
                    proposal.get("ref_id") or proposal.get("artifact_id"),
                ),
                _reference("delta", payload.get("projected_delta_ref")),
                _reference(
                    "commit",
                    commit.get("commit_id") or commit.get("reason"),
                ),
                _reference("permission", payload.get("permission_ref")),
                _reference("lease", payload.get("lease_ref")),
                _reference("verifier", payload.get("verification_ref")),
            ]
        )
    elif kind == "physical_dispatch_receipt":
        artifact = _record(payload.get("artifact_ref"))
        verifier = _record(payload.get("verifier_ref"))
        refs.extend(
            [
                _reference("placement", payload.get("placement_decision_id")),
                _reference("lease", payload.get("lease_id")),
                _reference("attempt", payload.get("physical_attempt_id")),
                _reference(
                    "artifact",
                    artifact.get("ref_id") or artifact.get("artifact_id"),
                    f"/tasks/{task_id}/artifacts"
                    if task_id
                    else "/artifacts",
                ),
                _reference(
                    "verifier",
                    verifier.get("ref_id") or verifier.get("artifact_id"),
                ),
                _reference("permission", payload.get("permission_ref")),
            ]
        )
    for value in payload.get("artifact_refs", []) if isinstance(payload.get("artifact_refs"), list) else []:
        item = _record(value)
        refs.append(
            _reference(
                "artifact",
                item.get("ref_id") or item.get("artifact_id"),
                f"/tasks/{task_id}/artifacts" if task_id else "/artifacts",
            )
        )
    unique: dict[tuple[str, str], Mapping[str, Any]] = {}
    for item in refs:
        if item is not None:
            unique[(str(item["kind"]), str(item["id"]))] = item
    return list(unique.values())


def _project_event(
    event: Mapping[str, Any],
    contract: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    event_payload = _event_payload(event)
    event_type = str(event.get("eventType") or "")
    event_schema = str(event_payload.get("schema") or "")
    is_loopx = "loopx" in event_type.lower() or "loopx" in event_schema.lower()
    is_runtime_evidence = any(
        token in event_type
        for token in (
            ".constraint.",
            ".artifact.",
            ".worker.",
            ".scheduler.",
            ".permission.",
            ".recovery.",
            ".checkpoint.",
            ".memory.",
            ".evaluation.",
            ".control.",
        )
    )
    if contract is None and not is_loopx and not is_runtime_evidence:
        return None
    header = _record(contract) if contract else {}
    payload = _record(contract.get("payload")) if contract else event_payload
    contract_kind = str(contract.get("contract_kind") or "") if contract else (
        "loopx_control_receipt" if is_loopx else "runtime_evidence_event"
    )
    run_id, task_id = _event_identity(event, contract)
    mechanism_id = str(header.get("mechanism_id") or (
        "loopx" if is_loopx else "zyra_runtime"
    ))
    mechanism_version = str(
        header.get("mechanism_version")
        or event_payload.get("mechanism_version")
        or event_payload.get("runtime_version")
        or "runtime-v1"
    )
    receipt_id = str(
        header.get("contract_id")
        or event_payload.get("receipt_id")
        or event_payload.get("command_id")
        or event.get("eventId")
        or ""
    )
    refs = _contract_references(contract_kind, payload, task_id=task_id)
    source_ref = _reference("event", event.get("eventId"), f"/events/{event.get('eventId')}")
    if source_ref:
        refs.append(source_ref)
    for item in event.get("artifactRefs", []) if isinstance(event.get("artifactRefs"), list) else []:
        artifact = _record(item)
        ref = _reference(
            "artifact",
            artifact.get("artifactId") or artifact.get("artifact_id"),
            f"/tasks/{task_id}/artifacts" if task_id else "/artifacts",
        )
        if ref:
            refs.append(ref)
    simulated = bool(
        payload.get("simulated", False)
        or payload.get("semantic_only", False)
    )
    physical_complete = all(
        (
            str(payload.get("lease_id") or ""),
            str(payload.get("physical_attempt_id") or ""),
            _record(payload.get("physical_identity")),
            _record(payload.get("worker_manifest_ref")),
            _record(payload.get("call_receipt")),
            _record(payload.get("artifact_ref")),
            _record(payload.get("verifier_ref")),
        )
    )
    execution = (
        "simulated"
        if simulated
        else (
            ("real" if physical_complete else "degraded")
            if contract_kind == "physical_dispatch_receipt"
            else "not_applicable"
        )
    )
    constraints = (
        payload.get("constraint_results")
        if isinstance(payload.get("constraint_results"), list)
        else []
    )
    operations = (
        payload.get("projected_operations")
        if isinstance(payload.get("projected_operations"), list)
        else payload.get("operations")
        if isinstance(payload.get("operations"), list)
        else []
    )
    return {
        "transition_id": f"{int(event.get('globalSequence') or 0)}:{receipt_id}",
        "sequence": int(event.get("globalSequence") or 0),
        "event_id": str(event.get("eventId") or ""),
        "event_type": event_type,
        "occurred_at": str(event.get("occurredAt") or event.get("createdAt") or ""),
        "run_id": run_id,
        "task_id": task_id,
        "receipt_id": receipt_id,
        "contract_kind": contract_kind,
        "schema_version": (
            str(contract.get("schema_version") or "")
            if contract
            else event_schema
        ),
        "contract_digest": (
            str(contract.get("digest") or "")
            if contract
            else str(event.get("contentDigest") or "")
        ),
        "mechanism": {
            "id": mechanism_id,
            "version": mechanism_version,
            "lifecycle": _lifecycle(mechanism_id, payload),
            "readiness": _readiness(contract_kind, payload),
        },
        "execution": execution,
        "integrity": "verified" if contract else "pending",
        "disposition": str(
            payload.get("disposition")
            or payload.get("continuity_result")
            or payload.get("status")
            or ""
        ),
        "constraints": constraints,
        "graph_diff": operations,
        "causal_refs": refs,
        "details": payload,
    }


def _inconsistent_transition(
    event: Mapping[str, Any],
    message: str,
) -> Mapping[str, Any]:
    run_id, task_id = _event_identity(event, None)
    return {
        "transition_id": f"{int(event.get('globalSequence') or 0)}:{event.get('eventId')}",
        "sequence": int(event.get("globalSequence") or 0),
        "event_id": str(event.get("eventId") or ""),
        "event_type": str(event.get("eventType") or ""),
        "occurred_at": str(event.get("occurredAt") or event.get("createdAt") or ""),
        "run_id": run_id,
        "task_id": task_id,
        "receipt_id": str(event.get("eventId") or ""),
        "contract_kind": "inconsistent_evidence",
        "schema_version": "",
        "contract_digest": str(event.get("contentDigest") or ""),
        "mechanism": {
            "id": "unknown",
            "version": "unknown",
            "lifecycle": "validation",
            "readiness": "unavailable",
        },
        "execution": "degraded",
        "integrity": "inconsistent",
        "disposition": "projection_failed",
        "constraints": [],
        "graph_diff": [],
        "causal_refs": [],
        "details": {"error": message},
    }


def _matches(
    transition: Mapping[str, Any],
    filters: Mapping[str, str],
) -> bool:
    mechanism = _record(transition.get("mechanism"))
    return all(
        (
            not filters[key]
            or str(observed) == filters[key]
        )
        for key, observed in (
            ("run_id", transition.get("run_id")),
            ("task_id", transition.get("task_id")),
            ("mechanism_version", mechanism.get("version")),
            ("receipt_id", transition.get("receipt_id")),
        )
    )


__all__ = [
    "FilesystemPolicyMetricReportProvider",
    "PolicyApiResponse",
    "PolicyEvidenceProjectionError",
    "PolicyEvidenceSource",
    "PolicyMetricApi",
    "PolicyMetricReportProvider",
    "RuntimePolicyEvidenceSource",
    "get_policy_metric_api",
    "reset_policy_metric_api",
]
