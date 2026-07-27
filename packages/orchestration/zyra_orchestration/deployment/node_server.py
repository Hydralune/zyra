from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .errors import DeploymentError, redact
from .models import DeploymentProfile, NetworkMode, ProfilePolicy, ResourceEnvelope, Sensitivity
from .node_runtime import DeploymentNodeRuntime
from .security import (
    RequestAuthenticator,
    public_error,
    safe_json_loads,
)


class DeploymentNodeHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        runtime: DeploymentNodeRuntime,
        authenticator: RequestAuthenticator,
    ) -> None:
        super().__init__(server_address, handler)
        self.runtime = runtime
        self.authenticator = authenticator


class DeploymentNodeHandler(BaseHTTPRequestHandler):
    server_version = "ZyraDeploymentNode/1"

    @property
    def node_server(self) -> DeploymentNodeHttpServer:
        return self.server  # type: ignore[return-value]

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        length = self._content_length()
        body = self.rfile.read(length) if length else b"{}"
        nonce = ""
        try:
            headers = {str(key): str(value) for key, value in self.headers.items()}
            nonce = self.node_server.authenticator.authenticate(
                method=method,
                path=path,
                body=body,
                headers=headers,
            )
            payload = safe_json_loads(body)
            status, response = self._dispatch(method, path, payload)
        except DeploymentError as error:
            status = error.status
            response = error.to_dict()
        except ValueError as error:
            status = HTTPStatus.BAD_REQUEST
            response = {
                "schema": "zyra.deployment-error/v1",
                "error": "node_request_invalid",
                "message": str(error),
                "fallback": False,
            }
        except BaseException as error:
            status = HTTPStatus.INTERNAL_SERVER_ERROR
            response = public_error(error)
        self._send_json(int(status), redact(response), nonce=nonce)

    def _dispatch(
        self,
        method: str,
        path: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        runtime = self.node_server.runtime
        if method == "GET" and path == "/health":
            return HTTPStatus.OK, runtime.health()
        if method == "POST" and path == "/semantic-readiness":
            result = runtime.semantic_probe()
            return (
                HTTPStatus.OK
                if result.get("ready") is True
                else HTTPStatus.SERVICE_UNAVAILABLE,
                result,
            )
        if method == "POST" and path == "/execute":
            result = runtime.execute(payload)
            return (
                HTTPStatus.OK
                if result.get("status") == "succeeded"
                else HTTPStatus.SERVICE_UNAVAILABLE,
                result,
            )
        if method == "POST" and path == "/checkpoints/export":
            return HTTPStatus.OK, runtime.export_checkpoint(
                str(payload.get("checkpoint_ref") or "")
            )
        if method == "POST" and path == "/checkpoints/import":
            return HTTPStatus.OK, runtime.import_checkpoint(payload)
        if method == "POST" and path == "/faults":
            return HTTPStatus.OK, runtime.inject_fault(payload)
        if method == "POST" and path == "/shutdown":
            response = {
                "schema": "zyra.deployment-node-shutdown/v1",
                "accepted": True,
                "node_id": runtime.node_id,
                "generation_id": runtime.generation_id,
            }
            threading.Thread(
                target=self.node_server.shutdown,
                daemon=True,
            ).start()
            return HTTPStatus.ACCEPTED, response
        return HTTPStatus.NOT_FOUND, {
            "schema": "zyra.deployment-error/v1",
            "error": "node_route_not_found",
            "message": f"node route does not exist: {method} {path}",
            "fallback": False,
        }

    def _content_length(self) -> int:
        raw = str(self.headers.get("Content-Length") or "0")
        try:
            value = int(raw)
        except ValueError as error:
            raise ValueError("Content-Length is invalid") from error
        if value < 0 or value > 8 * 1024 * 1024:
            raise ValueError("Content-Length is outside the supported range")
        return value

    def _send_json(
        self,
        status: int,
        payload: Any,
        *,
        nonce: str,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = (
            self.node_server.authenticator.sign_response(
                request_nonce=nonce,
                status=status,
                body=body,
            )
            if nonce
            else ""
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-Zyra-Response-Signature", signature)
        self.send_header(
            "X-Zyra-Node-Id",
            self.node_server.runtime.node_id,
        )
        self.send_header(
            "X-Zyra-Node-Generation",
            self.node_server.runtime.generation_id,
        )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        print(
            json.dumps(
                {
                    "schema": "zyra.deployment-node-http-log/v1",
                    "node_id": self.node_server.runtime.node_id,
                    "profile": self.node_server.runtime.policy.profile.value,
                    "client": self.address_string(),
                    "message": format % args,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            flush=True,
        )


def policy_from_json(raw: str) -> ProfilePolicy:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("node profile JSON must be an object")
    resource = value.get("resource")
    if not isinstance(resource, dict):
        raise ValueError("node profile resource must be an object")
    return ProfilePolicy(
        profile=DeploymentProfile(str(value.get("profile") or "")),
        host=str(value.get("host") or ""),
        port=int(value.get("port") or 0),
        network_mode=NetworkMode(str(value.get("network_mode") or "")),
        latency_budget_ms=int(value.get("latency_budget_ms") or 0),
        resource=ResourceEnvelope(
            cpu_percent=int(resource.get("cpu_percent") or 0),
            memory_mb=int(resource.get("memory_mb") or 0),
            max_concurrency=int(resource.get("max_concurrency") or 0),
            disk_mb=int(resource.get("disk_mb") or 0),
        ),
        allowed_sensitivity=tuple(
            Sensitivity(str(item))
            for item in value.get("allowed_sensitivity") or ()
        ),
        capabilities=tuple(str(item) for item in value.get("capabilities") or ()),
        providers=tuple(str(item) for item in value.get("providers") or ()),
        models=tuple(str(item) for item in value.get("models") or ()),
        credential_environment=tuple(
            str(item) for item in value.get("credential_environment") or ()
        ),
        allow_checkpoint_export=value.get("allow_checkpoint_export", True) is True,
        allow_checkpoint_import=value.get("allow_checkpoint_import", True) is True,
        required=value.get("required", True) is True,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Zyra isolated deployment node")
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--generation-id", required=True)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--policy-json", required=True)
    return parser


def run(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    policy = policy_from_json(arguments.policy_json)
    raw_secret = str(os.environ.get("ZYRA_DEPLOY_NODE_SECRET_B64") or "")
    if not raw_secret:
        raise SystemExit("ZYRA_DEPLOY_NODE_SECRET_B64 is required")
    try:
        secret = base64.b64decode(raw_secret, validate=True)
    except ValueError as error:
        raise SystemExit("ZYRA_DEPLOY_NODE_SECRET_B64 is invalid") from error
    if len(secret) < 32:
        raise SystemExit("deployment node secret is too short")
    credential_presence = {
        name: bool(str(os.environ.get(name) or "").strip())
        for name in policy.credential_environment
    }
    runtime = DeploymentNodeRuntime(
        node_id=arguments.node_id,
        generation_id=arguments.generation_id,
        policy=policy,
        data_root=Path(arguments.data_root),
        credential_presence=credential_presence,
    )
    server = DeploymentNodeHttpServer(
        (policy.host, policy.port),
        DeploymentNodeHandler,
        runtime=runtime,
        authenticator=RequestAuthenticator(secret),
    )

    def stop(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, stop)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, stop)
    print(
        json.dumps(
            {
                "schema": "zyra.deployment-node-startup/v1",
                "ready": True,
                "node_id": arguments.node_id,
                "generation_id": arguments.generation_id,
                "profile": policy.profile.value,
                "pid": os.getpid(),
                "endpoint": f"http://{policy.host}:{policy.port}",
                "configuration_digest": policy.configuration_digest,
                "credential_presence": credential_presence,
                "credential_values_exposed": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.1)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
