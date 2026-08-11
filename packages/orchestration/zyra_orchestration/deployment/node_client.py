from __future__ import annotations

import json
import socket
import time
from collections.abc import Mapping
from http.client import HTTPConnection, HTTPResponse
from typing import Any
from urllib.parse import urlparse

from .errors import (
    DeploymentError,
    NodeProtocolError,
    ProcessUnavailable,
)
from .models import (
    DeploymentProfile,
    LifecycleStatus,
    NodeObservation,
    now_iso,
)
from .security import RequestSigner


class DeploymentNodeClient:
    def __init__(
        self,
        endpoint: str,
        secret: bytes,
        *,
        timeout_seconds: float = 10.0,
        expected_node_id: str = "",
        expected_generation_id: str = "",
        expected_profile: DeploymentProfile | None = None,
    ) -> None:
        parsed = urlparse(endpoint)
        if parsed.scheme != "http" or not parsed.hostname or not parsed.port:
            raise ValueError("deployment node endpoint must be an explicit HTTP host and port")
        self.endpoint = endpoint.rstrip("/")
        self.host = parsed.hostname
        self.port = parsed.port
        self.timeout_seconds = timeout_seconds
        self.expected_node_id = expected_node_id
        self.expected_generation_id = expected_generation_id
        self.expected_profile = expected_profile
        self.signer = RequestSigner(secret)

    def request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        accepted_statuses: tuple[int, ...] = (200,),
        timeout_seconds: float | None = None,
        use_default_timeout: bool = True,
    ) -> dict[str, Any]:
        signed = self.signer.sign(method, path, payload)
        effective_timeout = (
            self.timeout_seconds
            if use_default_timeout and timeout_seconds is None
            else timeout_seconds
        )
        connection = HTTPConnection(
            self.host,
            self.port,
            timeout=effective_timeout,
        )
        try:
            connection.request(
                signed.method,
                signed.path,
                body=signed.body,
                headers=signed.headers(),
            )
            response = connection.getresponse()
            body = self._read_response(response)
            signature = str(response.getheader("X-Zyra-Response-Signature") or "")
            self.signer.verify_response(
                signed,
                status=response.status,
                body=body,
                signature=signature,
            )
            self._verify_identity_headers(response)
        except (ConnectionError, TimeoutError, socket.timeout, OSError) as error:
            raise ProcessUnavailable(
                "deployment_node_unreachable",
                "deployment node endpoint is unavailable",
                operation=path,
                retryable=True,
                details={
                    "endpoint": self.endpoint,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        finally:
            connection.close()
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NodeProtocolError(
                "deployment_node_json_invalid",
                "deployment node returned invalid JSON",
                operation=path,
                retryable=False,
                details={"endpoint": self.endpoint, "status": response.status},
            ) from error
        if not isinstance(value, dict):
            raise NodeProtocolError(
                "deployment_node_response_shape_invalid",
                "deployment node response is not an object",
                operation=path,
                details={"endpoint": self.endpoint, "status": response.status},
            )
        if response.status not in accepted_statuses:
            code = str(value.get("error") or value.get("failure_code") or "deployment_node_rejected")
            message = str(value.get("message") or f"node returned HTTP {response.status}")
            raise NodeProtocolError(
                code,
                message,
                operation=path,
                profile=self.expected_profile.value if self.expected_profile else "",
                retryable=response.status >= 500,
                details={
                    "endpoint": self.endpoint,
                    "status": response.status,
                    "node_response": value,
                },
            )
        return value

    @staticmethod
    def _read_response(response: HTTPResponse) -> bytes:
        raw_length = response.getheader("Content-Length")
        if raw_length is None:
            body = response.read(8 * 1024 * 1024 + 1)
        else:
            try:
                length = int(raw_length)
            except ValueError as error:
                raise NodeProtocolError(
                    "deployment_node_content_length_invalid",
                    "deployment node returned an invalid Content-Length",
                    operation="read_response",
                ) from error
            if length < 0 or length > 8 * 1024 * 1024:
                raise NodeProtocolError(
                    "deployment_node_response_too_large",
                    "deployment node response exceeds the maximum size",
                    operation="read_response",
                )
            body = response.read(length)
        if len(body) > 8 * 1024 * 1024:
            raise NodeProtocolError(
                "deployment_node_response_too_large",
                "deployment node response exceeds the maximum size",
                operation="read_response",
            )
        return body

    def _verify_identity_headers(self, response: HTTPResponse) -> None:
        node_id = str(response.getheader("X-Zyra-Node-Id") or "")
        generation = str(response.getheader("X-Zyra-Node-Generation") or "")
        if self.expected_node_id and node_id != self.expected_node_id:
            raise NodeProtocolError(
                "deployment_node_identity_mismatch",
                "deployment node response came from an unexpected node",
                operation="verify_identity",
                details={"expected": self.expected_node_id, "actual": node_id},
            )
        if self.expected_generation_id and generation != self.expected_generation_id:
            raise NodeProtocolError(
                "deployment_node_generation_mismatch",
                "deployment node response came from a stale generation",
                operation="verify_identity",
                details={
                    "expected": self.expected_generation_id,
                    "actual": generation,
                },
            )

    def health(self) -> dict[str, Any]:
        return self.request("GET", "/health", {})

    def semantic_readiness(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/semantic-readiness",
            {},
            accepted_statuses=(200, 503),
        )

    def execute(
        self,
        payload: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Execute one workload with an explicit transport budget.

        ``None`` deliberately means no transport deadline.  Health and
        lifecycle requests retain the client's short default, but a live
        model/tool loop may legitimately remain active for hours.  Keeping the
        two meanings separate also mirrors the deployment task contract where
        an execution budget of zero is an open run.
        """

        return self.request(
            "POST",
            "/execute",
            payload,
            accepted_statuses=(200, 503),
            timeout_seconds=timeout_seconds,
            use_default_timeout=False,
        )

    def inject_fault(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self.request("POST", "/faults", payload)

    def export_checkpoint(self, checkpoint_ref: str) -> dict[str, Any]:
        return self.request(
            "POST",
            "/checkpoints/export",
            {"checkpoint_ref": checkpoint_ref},
        )

    def import_checkpoint(
        self,
        checkpoint: Mapping[str, Any],
        *,
        source_node_id: str,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            "/checkpoints/import",
            {
                "checkpoint": dict(checkpoint),
                "source_node_id": source_node_id,
            },
        )

    def shutdown(self) -> dict[str, Any]:
        return self.request(
            "POST",
            "/shutdown",
            {},
            accepted_statuses=(202,),
        )

    def wait_ready(
        self,
        *,
        timeout_seconds: float = 20.0,
        poll_seconds: float = 0.1,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        last_error: BaseException | None = None
        while time.monotonic() < deadline:
            try:
                health = self.health()
                if (
                    health.get("node_id") == self.expected_node_id
                    and health.get("generation_id") == self.expected_generation_id
                    and health.get("status") in {"ready", "degraded"}
                ):
                    return health
            except DeploymentError as error:
                last_error = error
            time.sleep(max(0.01, poll_seconds))
        raise ProcessUnavailable(
            "deployment_node_readiness_timeout",
            "deployment node did not become ready before the deadline",
            operation="wait_ready",
            retryable=True,
            details={
                "endpoint": self.endpoint,
                "timeout_seconds": timeout_seconds,
                "last_error": (
                    f"{type(last_error).__name__}: {last_error}"
                    if last_error is not None
                    else ""
                ),
            },
        )

    def observation(self) -> NodeObservation:
        health = self.health()
        profile = DeploymentProfile(str(health.get("profile") or ""))
        if self.expected_profile is not None and profile is not self.expected_profile:
            raise NodeProtocolError(
                "deployment_node_profile_mismatch",
                "deployment node health reported an unexpected profile",
                operation="observation",
                profile=profile.value,
            )
        status = (
            LifecycleStatus.READY
            if health.get("status") == "ready"
            else LifecycleStatus.DEGRADED
            if health.get("status") == "degraded"
            else LifecycleStatus.BLOCKED
        )
        return NodeObservation(
            node_id=str(health.get("node_id") or ""),
            profile=profile,
            generation_id=str(health.get("generation_id") or ""),
            pid=int(health.get("pid") or 0),
            endpoint=str(health.get("endpoint") or self.endpoint),
            status=status,
            capabilities=tuple(str(item) for item in health.get("capabilities") or ()),
            heartbeat_sequence=int(health.get("heartbeat_sequence") or 0),
            resource=dict(health.get("resource") or {}),
            network=dict(health.get("network") or {}),
            credential_presence=dict(health.get("credential_presence") or {}),
            observed_at=now_iso(),
            nonce_window_size=int(health.get("nonce_window_size") or 0),
            active_dispatches=int(
                (health.get("resource") or {}).get("active_dispatches") or 0
            ),
            completed_dispatches=int(
                (health.get("journal") or {}).get("completed_dispatches") or 0
            ),
            configuration_digest=str(health.get("configuration_digest") or ""),
            semantic_digest=str(health.get("semantic_digest") or ""),
        )


__all__ = ["DeploymentNodeClient"]
