from __future__ import annotations

from typing import Any, Mapping


def provider_failure_recovery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Project TS failure taxonomy without reclassifying or changing policy."""

    kind = str(value.get("kind") or "unknown_provider_failure")
    return {
        "failure_layer": str(value.get("layer") or "transport"),
        "failure_kind": kind,
        "recovery_intent": str(value.get("recoveryIntent") or "surface_to_operator"),
        "retryable": value.get("retryable") is True,
        "provider_route_change": kind in {
            "provider_unavailable",
            "provider_timeout",
            "stream_timeout",
        },
        "backend_route_change": False,
        "partial_output_requires_reconcile": value.get("outputObserved") is True,
    }
