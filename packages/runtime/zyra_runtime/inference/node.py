from __future__ import annotations

import argparse
import hmac
import json
import os
import socket
import ssl
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .graph import load_bundle, sha256
from .protocol import MAX_BYTES, decode_tensors, encode_tensors, tensor_digest


class InferenceNode:
    def __init__(self, bundle: Path, *, node_id: str, location: str,
                 parts: tuple[str, ...] = ("full", "front", "back"),
                 max_model_bytes: int = MAX_BYTES, threads: int = 1) -> None:
        import onnxruntime as ort

        self.bundle = bundle.resolve()
        self.manifest = load_bundle(self.bundle)
        self.identity = {
            "node_id": node_id, "location": location, "pid": os.getpid(),
            "hostname": socket.gethostname(), "generation": uuid.uuid4().hex,
            "execution_provider": "CPUExecutionProvider",
        }
        self.sessions = {}
        self.last_run_ms: dict[str, float] = {}
        self.lock = threading.Lock()
        self.max_model_bytes = max_model_bytes
        self.threads = threads
        for part in parts:
            if part not in self.manifest["parts"]:
                raise ValueError(f"unknown model partition: {part}")
            item = self.manifest["parts"][part]
            if item["bytes"] > max_model_bytes:
                continue
            options = ort.SessionOptions()
            options.intra_op_num_threads = threads
            options.inter_op_num_threads = 1
            self.sessions[part] = ort.InferenceSession(
                str(self.bundle / item["file"]), sess_options=options,
                providers=["CPUExecutionProvider"],
            )
        if not self.sessions:
            raise ValueError("no partition fits this node's declared model-byte budget")

    def health(self) -> dict[str, Any]:
        return {
            "schema": "zyra.inference-node/v1", "identity": dict(self.identity),
            "model_id": self.manifest["model_id"],
            "source_sha256": self.manifest["source_sha256"],
            "parts": {name: self.manifest["parts"][name] for name in self.sessions},
            "capacity": {"max_model_bytes": self.max_model_bytes, "threads": self.threads},
            "last_run_ms": dict(self.last_run_ms),
        }

    def infer(self, payload: dict[str, Any]) -> dict[str, Any]:
        part = str(payload.get("part") or "")
        if part not in self.sessions:
            raise ValueError("requested partition is not admitted on this node")
        item = self.manifest["parts"][part]
        if payload.get("model_sha256") != item["sha256"]:
            raise ValueError("request model digest does not match the loaded partition")
        if sha256(self.bundle / item["file"]) != item["sha256"]:
            raise ValueError("model artifact changed after node startup")
        inputs = decode_tensors(str(payload.get("tensors") or ""))
        if set(inputs) != set(item["inputs"]):
            raise ValueError("partition input names do not match the model contract")
        request_id = str(payload.get("request_id") or "")
        if not request_id or len(request_id) > 256:
            raise ValueError("inference request needs a bounded request identity")
        with self.lock:
            started = time.perf_counter()
            values = self.sessions[part].run(item["outputs"], inputs)
            elapsed = (time.perf_counter() - started) * 1000
            self.last_run_ms[part] = elapsed
        outputs = dict(zip(item["outputs"], values, strict=True))
        receipt = {
            "schema": "zyra.inference-stage/v1", "request_id": request_id,
            "identity": dict(self.identity), "part": part,
            "model_sha256": item["sha256"], "source_sha256": self.manifest["source_sha256"],
            "input_sha256": tensor_digest(inputs), "output_sha256": tensor_digest(outputs),
            "input_bytes": sum(v.nbytes for v in inputs.values()),
            "output_bytes": sum(v.nbytes for v in outputs.values()),
            "elapsed_ms": elapsed, "operator_count": item["operator_count"],
            "simulated": False,
        }
        return {"receipt": receipt, "tensors": encode_tensors(outputs)}


def serve(node: InferenceNode, *, host: str, port: int, token: str,
          cert: str | None = None, key: str | None = None) -> None:
    if len(token) < 32:
        raise ValueError("set ZYRA_INFERENCE_TOKEN to a random token of at least 32 characters")
    if host not in {"127.0.0.1", "localhost", "::1"} and not (cert and key):
        raise ValueError("non-loopback model serving requires a TLS certificate and key")

    class Handler(BaseHTTPRequestHandler):
        def setup(self) -> None:
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_args: Any) -> None:
            pass

        def do_GET(self) -> None:
            self.handle_request(False)

        def do_POST(self) -> None:
            self.handle_request(True)

        def handle_request(self, post: bool) -> None:
            if not hmac.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                self.reply(401, {"error": "authentication_required"})
                return
            try:
                if not post and self.path == "/health":
                    self.reply(200, node.health())
                elif post and self.path == "/infer":
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= MAX_BYTES * 2:
                        raise ValueError("request exceeds the byte budget")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("request must be an object")
                    self.reply(200, node.infer(payload))
                else:
                    self.reply(404, {"error": "unknown_operation"})
            except Exception as exc:
                self.reply(400, {"error": type(exc).__name__, "message": str(exc)[:400]})

        def reply(self, status: int, value: dict[str, Any]) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    if cert and key:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
    print(json.dumps({"status": "ready", "port": server.server_port, **node.health()}), flush=True)
    server.serve_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve an authenticated ONNX model partition")
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--location", choices=("device", "edge", "cloud"), required=True)
    parser.add_argument("--parts", default="full,front,back")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--max-model-bytes", type=int, default=MAX_BYTES)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--cert")
    parser.add_argument("--key")
    args = parser.parse_args()
    if not 1 <= args.threads <= 32 or args.max_model_bytes < 1:
        parser.error("invalid model capacity or thread budget")
    node = InferenceNode(args.bundle, node_id=args.node_id, location=args.location,
                         parts=tuple(args.parts.split(",")), max_model_bytes=args.max_model_bytes,
                         threads=args.threads)
    serve(node, host=args.host, port=args.port, token=os.environ.get("ZYRA_INFERENCE_TOKEN", ""),
          cert=args.cert, key=args.key)


if __name__ == "__main__":
    main()
