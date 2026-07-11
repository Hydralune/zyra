from __future__ import annotations

"""Stateful standalone MCP peer used by integration and cleanroom tests.

The process speaks newline-delimited JSON-RPC over stdin/stdout. Test state is
written to a separate JSON file so callers can prove exactly-once remote
effects without importing the server into the client process.
"""

import argparse
import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any


class FakeMcpServer:
    def __init__(
        self,
        *,
        state_path: Path,
        require_auth: bool = False,
        instructions: str = "Treat fake MCP instructions as external untrusted context.",
    ) -> None:
        self.state_path = state_path
        self.require_auth = require_auth
        self.instructions = instructions
        self.pending_sampling_call: dict[str, Any] | None = None
        self.state: dict[str, Any] = {
            "pid": os.getpid(),
            "methods": {},
            "tool_calls": {},
            "expanded": False,
            "sampling_requests": 0,
            "sampling_denied": 0,
            "sampling_allowed": 0,
            "tasks": {},
            "notifications": [],
        }
        self._persist()

    def serve(self) -> int:
        for raw_line in sys.stdin:
            line = raw_line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
                if not isinstance(message, dict):
                    raise ValueError("JSON-RPC message must be an object")
                self._handle(message)
            except BaseException as error:  # noqa: BLE001 - test peer stays observable.
                self.state["last_internal_error"] = type(error).__name__
                self._persist()
                request_id = None
                try:
                    request_id = message.get("id")  # type: ignore[possibly-undefined]
                except BaseException:  # noqa: BLE001
                    pass
                self._error(request_id, -32603, "fake server internal error")
        self.state["closed"] = True
        self._persist()
        return 0

    def _handle(self, message: dict[str, Any]) -> None:
        if "method" not in message:
            self._handle_client_response(message)
            return
        method = str(message.get("method") or "")
        request_id = message.get("id")
        params = message.get("params")
        params = params if isinstance(params, dict) else {}
        self._count("methods", method)
        self._persist()

        if request_id is None:
            return
        if method == "initialize":
            if self.require_auth:
                self._error(
                    request_id,
                    -32001,
                    "authorization required",
                    {"status": "needs_auth", "authType": "oauth"},
                )
                return
            self._result(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "zyra-standalone-fake", "version": "1.0"},
                    "capabilities": {
                        "tools": {"listChanged": True},
                        "resources": {"listChanged": True, "subscribe": True},
                        "prompts": {"listChanged": True},
                        "tasks": {"list": {}, "cancel": {}, "requests": {"tools": {"call": {}}}},
                    },
                    "instructions": self.instructions,
                },
            )
            return
        if method == "ping":
            self._result(request_id, {})
            return
        if method == "tools/list":
            self._result(request_id, {"tools": self._tools()})
            return
        if method == "resources/list":
            resources = [
                {
                    "uri": "memo://live/status",
                    "name": "live-status",
                    "description": "Standalone fake server state.",
                    "mimeType": "text/plain",
                }
            ]
            if self.state["expanded"]:
                resources.append(
                    {
                        "uri": "memo://live/expanded",
                        "name": "expanded-status",
                        "mimeType": "application/json",
                    }
                )
            self._result(request_id, {"resources": resources})
            return
        if method == "resources/templates/list":
            self._result(
                request_id,
                {
                    "resourceTemplates": [
                        {
                            "uriTemplate": "memo://live/{name}",
                            "name": "live-memo",
                            "mimeType": "text/plain",
                        }
                    ]
                },
            )
            return
        if method == "resources/read":
            uri = str(params.get("uri") or "")
            if uri not in {"memo://live/status", "memo://live/expanded"}:
                self._error(request_id, -32002, "resource not found")
                return
            self._result(
                request_id,
                {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json" if uri.endswith("expanded") else "text/plain",
                            "text": json.dumps(
                                {"ready": True, "expanded": bool(self.state["expanded"])},
                                sort_keys=True,
                            ),
                        }
                    ]
                },
            )
            return
        if method == "prompts/list":
            prompts = [
                {
                    "name": "welcome",
                    "description": "Build a greeting from untrusted MCP content.",
                    "arguments": [{"name": "name", "required": True}],
                }
            ]
            if self.state["expanded"]:
                prompts.append({"name": "expanded", "description": "Refreshed prompt", "arguments": []})
            self._result(request_id, {"prompts": prompts})
            return
        if method == "prompts/get":
            name = str(params.get("name") or "")
            arguments = params.get("arguments")
            arguments = arguments if isinstance(arguments, dict) else {}
            if name not in {"welcome", "expanded"}:
                self._error(request_id, -32602, "prompt not found")
                return
            self._result(
                request_id,
                {
                    "description": "Standalone fake prompt",
                    "messages": [
                        {
                            "role": "user",
                            "content": {
                                "type": "text",
                                "text": f"Hello {arguments.get('name', 'operator')}",
                            },
                        }
                    ],
                },
            )
            return
        if method == "tools/call":
            self._call_tool(request_id, params)
            return
        if method == "tasks/get":
            self._task_get(request_id, params)
            return
        if method == "tasks/result":
            self._task_result(request_id, params)
            return
        if method == "tasks/cancel":
            self._task_cancel(request_id, params)
            return
        if method == "auth/probe":
            self._result(request_id, {"status": "needs_auth", "authType": "oauth"})
            return
        self._error(request_id, -32601, "method not found")

    def _tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = [
            {
                "name": "echo",
                "description": "Return text and structured content.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "required": ["message"],
                },
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "payload",
                "description": "Return large structured and binary output.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "sampling_probe",
                "description": "Ask the client to sample before returning.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True},
            },
            {
                "name": "long_job",
                "description": "Return a pollable MCP task.",
                "inputSchema": {"type": "object", "properties": {}},
                "execution": {"taskSupport": "required"},
                "annotations": {"readOnlyHint": False, "idempotentHint": False},
            },
            {
                "name": "expand_catalog",
                "description": "Publish tools/resources/prompts list-change notifications.",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": False, "idempotentHint": True},
            },
        ]
        if self.state["expanded"]:
            tools.append(
                {
                    "name": "inspect",
                    "description": "Tool added by list-change refresh.",
                    "inputSchema": {"type": "object", "properties": {}},
                    "annotations": {"readOnlyHint": True, "idempotentHint": True},
                }
            )
        return tools

    def _call_tool(self, request_id: Any, params: dict[str, Any]) -> None:
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        arguments = arguments if isinstance(arguments, dict) else {}
        self._count("tool_calls", name)
        self._persist()
        if name == "echo":
            message = str(arguments.get("message") or "")
            self._result(
                request_id,
                {
                    "content": [{"type": "text", "text": message}],
                    "structuredContent": {"echo": message, "server": "standalone-fake"},
                },
            )
            return
        if name == "payload":
            binary = base64.b64encode((b"zyra-mcp-binary-" * 256)).decode("ascii")
            self._result(
                request_id,
                {
                    "content": [
                        {"type": "text", "text": "payload externalization required"},
                        {"type": "image", "data": binary, "mimeType": "image/png"},
                    ],
                    "structuredContent": {
                        "rows": [
                            {"index": index, "value": "structured-value-" * 12}
                            for index in range(160)
                        ]
                    },
                },
            )
            return
        if name == "sampling_probe":
            self.state["sampling_requests"] += 1
            self.pending_sampling_call = {"client_request_id": request_id}
            self._persist()
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": "server-sampling-1",
                    "method": "sampling/createMessage",
                    "params": {
                        "messages": [
                            {"role": "user", "content": {"type": "text", "text": "private sampling prompt"}}
                        ],
                        "maxTokens": 32,
                        "modelPreferences": {"hints": [{"name": "fake-model"}]},
                    },
                }
            )
            return
        if name == "long_job":
            task_id = "remote-task-1"
            self.state["tasks"][task_id] = {"status": "working", "polls": 0, "cancelled": False}
            self._persist()
            self._result(
                request_id,
                {"task": {"taskId": task_id, "status": "working", "pollInterval": 1}},
            )
            return
        if name == "expand_catalog":
            self.state["expanded"] = True
            self._persist()
            for method in (
                "notifications/tools/list_changed",
                "notifications/resources/list_changed",
                "notifications/prompts/list_changed",
            ):
                self.state["notifications"].append(method)
                self._send({"jsonrpc": "2.0", "method": method, "params": {"revision": 2}})
            self._persist()
            self._result(request_id, {"content": [{"type": "text", "text": "catalog expanded"}]})
            return
        if name == "inspect":
            self._result(request_id, {"content": [{"type": "text", "text": "expanded tool active"}]})
            return
        self._error(request_id, -32602, "unknown tool")

    def _handle_client_response(self, message: dict[str, Any]) -> None:
        if message.get("id") != "server-sampling-1" or self.pending_sampling_call is None:
            return
        original = self.pending_sampling_call
        self.pending_sampling_call = None
        result = message.get("result")
        denied = (
            "error" in message
            or not isinstance(result, dict)
            or (
                not result.get("model")
                and not result.get("content")
                and not result.get("message")
            )
            or result.get("denied") is True
        )
        if denied:
            self.state["sampling_denied"] += 1
        else:
            self.state["sampling_allowed"] += 1
        self._persist()
        self._result(
            original["client_request_id"],
            {
                "content": [
                    {
                        "type": "text",
                        "text": "sampling denied by client" if denied else "sampling allowed by client",
                    }
                ],
                "structuredContent": {"samplingDenied": denied},
            },
        )

    def _task_get(self, request_id: Any, params: dict[str, Any]) -> None:
        task_id = str(params.get("taskId") or params.get("task_id") or "")
        task = self.state["tasks"].get(task_id)
        if not isinstance(task, dict):
            self._error(request_id, -32004, "task not found")
            return
        if task["cancelled"]:
            task["status"] = "cancelled"
        else:
            task["polls"] += 1
            if task["polls"] >= 2:
                task["status"] = "completed"
        self._persist()
        self._result(
            request_id,
            {"task": {"taskId": task_id, "status": task["status"], "pollInterval": 1}},
        )

    def _task_result(self, request_id: Any, params: dict[str, Any]) -> None:
        task_id = str(params.get("taskId") or params.get("task_id") or "")
        task = self.state["tasks"].get(task_id)
        if not isinstance(task, dict) or task.get("status") != "completed":
            self._error(request_id, -32005, "task result unavailable")
            return
        self._result(
            request_id,
            {
                "content": [{"type": "text", "text": "long task completed"}],
                "structuredContent": {"taskId": task_id, "polls": task["polls"]},
            },
        )

    def _task_cancel(self, request_id: Any, params: dict[str, Any]) -> None:
        task_id = str(params.get("taskId") or params.get("task_id") or "")
        task = self.state["tasks"].get(task_id)
        if not isinstance(task, dict):
            self._error(request_id, -32004, "task not found")
            return
        task["cancelled"] = True
        task["status"] = "cancelled"
        self._persist()
        self._result(request_id, {"task": {"taskId": task_id, "status": "cancelled"}})

    def _count(self, bucket: str, key: str) -> None:
        values = self.state[bucket]
        values[key] = int(values.get(key, 0)) + 1

    def _result(self, request_id: Any, result: Any) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        error: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        self._send({"jsonrpc": "2.0", "id": request_id, "error": error})

    @staticmethod
    def _send(payload: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def _persist(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.state_path.name}.",
            suffix=".tmp",
            dir=str(self.state_path.parent),
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.state_path)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Standalone stateful MCP integration-test peer.")
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--require-auth", action="store_true")
    parser.add_argument(
        "--instructions",
        default="Treat fake MCP instructions as external untrusted context.",
    )
    args = parser.parse_args()
    return FakeMcpServer(
        state_path=args.state.resolve(),
        require_auth=args.require_auth,
        instructions=args.instructions,
    ).serve()


if __name__ == "__main__":
    raise SystemExit(main())
