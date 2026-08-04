from __future__ import annotations

import json
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from apps.api.zyra_api.provider_backend_api import ProviderBackendApi
from zyra_scheduler.backend_registry import (
    BackendDefinition,
    BackendKind,
    BackendLocation,
)
from zyra_scheduler.backend_registry.models import checksum
from zyra_scheduler.backend_registry.transport import BackendTransportFrame, _redact_url


def _frame_wire(**overrides: Any) -> dict[str, Any]:
    body = {
        "sequence": 1,
        "kind": "accepted",
        "created_at": 1.0,
        "payload": {"dispatch_id": "dispatch-terminal"},
        "output_observed": False,
    }
    body.update(overrides)
    return {**body, "digest": checksum(body)}


def test_remote_frame_requires_sender_digest_and_strict_sequence() -> None:
    valid = _frame_wire()
    decoded = BackendTransportFrame.from_wire(valid, expected_sequence=1)
    assert decoded.digest == valid["digest"]

    missing = dict(valid)
    missing.pop("digest")
    with pytest.raises(ValueError, match="digest is required"):
        BackendTransportFrame.from_wire(missing, expected_sequence=1)

    with pytest.raises(ValueError, match="digest mismatch"):
        BackendTransportFrame.from_wire({**valid, "digest": "0" * 64}, expected_sequence=1)

    with pytest.raises(ValueError, match="sequence mismatch"):
        BackendTransportFrame.from_wire(valid, expected_sequence=2)


def test_public_backend_projection_and_transport_metadata_hide_capability_path() -> None:
    token = "terminal-capability-token-0123456789abcdef0123456789"
    endpoint = f"http://127.0.0.1:43123/capability/{token}"
    definition = BackendDefinition(
        backend_id="terminal_backend_projection",
        display_name="Terminal projection",
        kind=BackendKind.EDGE_HTTP,
        location=BackendLocation.LOCAL,
        runtime_worker="CodeWorkerRuntime",
        endpoint=endpoint,
        health_endpoint=f"{endpoint}/health",
        metadata={
            "terminal_generation": "generation-projection",
            "terminal_capability_digest": "sha256:opaque",
            "nested": {"endpoint_url": endpoint, "safe": "visible"},
        },
    )

    public = definition.to_public_dict()
    rendered = json.dumps(public, sort_keys=True)
    assert public["endpoint"] is None
    assert public["health_endpoint"] is None
    assert str(public["endpoint_identity"]).startswith("sha256:")
    assert token not in rendered
    assert "terminal_capability_digest" not in public["metadata"]
    assert public["metadata"]["nested"]["safe"] == "visible"
    assert token not in _redact_url(endpoint)
    assert _redact_url(endpoint).endswith("/[OPAQUE]")


def test_terminal_registration_is_revision_fenced_scoped_and_health_attested() -> None:
    token = "terminal-registration-token-0123456789abcdef0123456789"
    generation = "terminal-generation-test"
    backend_id = "terminal_backend_registration"
    runtime_worker = "CodeWorkerRuntime"

    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != f"/capability/{token}/health":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            payload = json.dumps(
                {
                    "ok": True,
                    "accepting": True,
                    "backend_id": backend_id,
                    "generation": generation,
                    "runtime_worker": runtime_worker,
                    "active_dispatches": 0,
                    "terminal_dispatches": 0,
                    "maximum_concurrency": 1,
                    "operations": ["execute"],
                    "capabilities": ["code-change", "shell"],
                    "checked_at": 1.0,
                }
            ).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}/capability/{token}"
    try:
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            api = ProviderBackendApi(project_root=root, artifact_root=root / "artifacts")
            health = api.handle_get(["backends", "health"], {})
            assert health is not None
            revision = int(health.body["result"]["registry_revision"])

            backend = {
                "backend_id": backend_id,
                "display_name": "Terminal registration",
                "kind": "edge_http",
                "location": "local",
                "runtime_worker": runtime_worker,
                "capabilities": ["code-change", "shell"],
                "endpoint": endpoint,
                "metadata": {"safe_label": "terminal-test"},
            }
            proof = {
                "generation": generation,
                "owner_id": "terminal-owner-test",
                "capability_token": token,
            }

            missing_revision = api.handle_post(
                ["backends"],
                {"backend": backend, "terminal_registration": proof},
            )
            assert missing_revision is not None
            assert missing_revision.status == HTTPStatus.UNPROCESSABLE_ENTITY

            wrong_kind = api.handle_post(
                ["backends"],
                {
                    "backend": {**backend, "kind": "cloud_http", "location": "cloud"},
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert wrong_kind is not None
            assert wrong_kind.status == HTTPStatus.FORBIDDEN

            non_loopback = api.handle_post(
                ["backends"],
                {
                    "backend": {
                        **backend,
                        "endpoint": f"http://192.0.2.10:43123/capability/{token}",
                    },
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert non_loopback is not None
            assert non_loopback.status == HTTPStatus.FORBIDDEN
            assert non_loopback.body["error"] == "terminal_endpoint_not_loopback"

            wrong_capability_path = api.handle_post(
                ["backends"],
                {
                    "backend": {**backend, "endpoint": f"{endpoint}/nested"},
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert wrong_capability_path is not None
            assert wrong_capability_path.status == HTTPStatus.FORBIDDEN
            assert wrong_capability_path.body["error"] == "terminal_capability_mismatch"

            wrong_generation = api.handle_post(
                ["backends"],
                {
                    "backend": backend,
                    "expected_revision": revision,
                    "terminal_registration": {**proof, "generation": "wrong-generation"},
                },
            )
            assert wrong_generation is not None
            assert wrong_generation.status == HTTPStatus.CONFLICT
            assert wrong_generation.body["error"] == "terminal_listener_identity_mismatch"

            with socket.socket() as unused_socket:
                unused_socket.bind(("127.0.0.1", 0))
                unused_port = unused_socket.getsockname()[1]
            listener_unavailable = api.handle_post(
                ["backends"],
                {
                    "backend": {
                        **backend,
                        "endpoint": f"http://127.0.0.1:{unused_port}/capability/{token}",
                        "limits": {"health_timeout_seconds": 0.05},
                    },
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert listener_unavailable is not None
            assert listener_unavailable.status == HTTPStatus.SERVICE_UNAVAILABLE
            assert listener_unavailable.body["error"] == "terminal_listener_not_ready"

            registered = api.handle_post(
                ["backends"],
                {
                    "backend": backend,
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert registered is not None
            assert registered.status == HTTPStatus.OK, registered.body

            listing = api.handle_get(["backends"], {})
            assert listing is not None
            rendered = json.dumps(listing.body, sort_keys=True)
            assert token not in rendered
            terminal = next(
                item for item in listing.body["result"] if item["backend_id"] == backend_id
            )
            assert terminal["endpoint"] is None
            assert str(terminal["endpoint_identity"]).startswith("sha256:")

            current_revision = int(registered.body["result"]["registry_revision"])
            stale_revision = api.handle_post(
                ["backends"],
                {
                    "backend": backend,
                    "expected_revision": revision,
                    "terminal_registration": proof,
                },
            )
            assert stale_revision is not None
            assert stale_revision.status == HTTPStatus.CONFLICT

            owner_conflict = api.handle_post(
                ["backends"],
                {
                    "backend": backend,
                    "expected_revision": current_revision,
                    "terminal_registration": {**proof, "owner_id": "different-owner"},
                },
            )
            assert owner_conflict is not None
            assert owner_conflict.status == HTTPStatus.CONFLICT
            assert owner_conflict.body["error"] == "terminal_owner_conflict"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)
