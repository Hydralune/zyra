from __future__ import annotations

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


_REPORT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class PolicyMetricReportProvider(Protocol):
    def get_report(self, report_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class PolicyApiResponse:
    status: int
    body: Mapping[str, Any]
    headers: Mapping[str, str]


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


class PolicyMetricApi:
    """GET-only API facade; it cannot mutate task, graph, lease, or run state."""

    def __init__(self, provider: PolicyMetricReportProvider) -> None:
        self.provider = provider

    def route_get(
        self,
        parts: tuple[str, ...],
        query: Mapping[str, Any],
    ) -> PolicyApiResponse | None:
        del query
        headers = {"Cache-Control": "no-store, max-age=0"}
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


_policy_metric_api: PolicyMetricApi | None = None


def get_policy_metric_api(project_root: Path) -> PolicyMetricApi:
    global _policy_metric_api
    if _policy_metric_api is None:
        _policy_metric_api = PolicyMetricApi(
            FilesystemPolicyMetricReportProvider(
                project_root / ".zyra" / "reports" / "policy-metrics"
            )
        )
    return _policy_metric_api


def reset_policy_metric_api() -> None:
    global _policy_metric_api
    _policy_metric_api = None


__all__ = [
    "FilesystemPolicyMetricReportProvider",
    "PolicyApiResponse",
    "PolicyMetricApi",
    "PolicyMetricReportProvider",
    "get_policy_metric_api",
    "reset_policy_metric_api",
]
