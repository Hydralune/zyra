from __future__ import annotations

import hashlib
import json
import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from uuid import uuid4

from .contracts import utc_now
from .integration_contracts import stable_digest
from .live_evidence import ProviderWireAttestation, WireDialect


class ManagedProviderError(RuntimeError):
    """An authenticated provider CLI could not prove a real bounded turn."""


@dataclass(frozen=True, slots=True)
class ManagedProviderSpec:
    provider_id: str
    cli_kind: str
    executable: str
    model_id: str
    dialect: WireDialect
    endpoint: str
    request_path: str
    timeout_seconds: float = 180.0
    maximum_budget_usd: float = 0.20

    def validate(self) -> None:
        if self.cli_kind not in {"claude", "codex"}:
            raise ValueError(f"unsupported managed provider CLI: {self.cli_kind}")
        if not self.provider_id or not self.model_id or not self.executable:
            raise ValueError("managed provider identity is incomplete")
        if not self.endpoint.startswith("https://") or not self.request_path.startswith("/"):
            raise ValueError("managed provider requires an HTTPS endpoint and absolute request path")
        if self.timeout_seconds <= 0 or self.maximum_budget_usd <= 0:
            raise ValueError("managed provider timeout and budget must be positive")


@dataclass(frozen=True, slots=True)
class CapturedLine:
    sequence: int
    timestamp: str
    raw: str
    value: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProcessCapture:
    command_id: str
    started_at: str
    completed_at: str
    exit_code: int
    lines: tuple[CapturedLine, ...]
    stderr_digest: str
    stderr_bytes: int
    executable_version: str

    @property
    def stdout_digest(self) -> str:
        encoded = "\n".join(item.raw for item in self.lines).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedProviderReceipt:
    attestation: ProviderWireAttestation
    trace_path: str
    trace_digest: str
    command_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "attestation": self.attestation.to_dict(),
            "trace_path": self.trace_path,
            "trace_digest": self.trace_digest,
            "command_id": self.command_id,
        }


class ManagedProviderProbe:
    """Run a real read-only tool roundtrip while the CLI retains credentials."""

    def execute(
        self,
        spec: ManagedProviderSpec,
        *,
        artifact_root: str | Path,
        expected_token: str = "",
        run_id: str = "",
    ) -> ManagedProviderReceipt:
        spec.validate()
        attempt_id = f"provider-attempt-{uuid4().hex}"
        workspace = Path(artifact_root).resolve() / "managed-provider" / attempt_id
        workspace.mkdir(parents=True, exist_ok=False)
        token = expected_token or f"ZYRA_PROVIDER_TOOL_{uuid4().hex.upper()}"
        (workspace / "provider_input.txt").write_text(token + "\n", encoding="utf-8")
        prompt = (
            "Use the only available read-only tool to read provider_input.txt, "
            "then return exactly the file contents and nothing else."
        )
        capture = self._capture(
            self._command(spec, workspace=workspace, prompt=prompt),
            cwd=workspace,
            timeout_seconds=spec.timeout_seconds,
            executable_version=self._version(spec.executable),
        )
        attestation = self.attestation_from_capture(
            spec,
            capture,
            expected_token=token,
            prompt=prompt,
            attempt_id=attempt_id,
            run_id=run_id,
        )
        trace = self._trace_payload(spec, capture, attestation)
        trace_path = workspace / "provider-trace.json"
        encoded = json.dumps(trace, ensure_ascii=False, sort_keys=True, indent=2)
        trace_path.write_text(encoded, encoding="utf-8")
        return ManagedProviderReceipt(
            attestation=attestation,
            trace_path=str(trace_path),
            trace_digest=hashlib.sha256(trace_path.read_bytes()).hexdigest(),
            command_id=capture.command_id,
        )

    def attestation_from_capture(
        self,
        spec: ManagedProviderSpec,
        capture: ProcessCapture,
        *,
        expected_token: str,
        prompt: str,
        attempt_id: str,
        run_id: str = "",
    ) -> ProviderWireAttestation:
        if capture.exit_code != 0:
            raise ManagedProviderError(
                f"{spec.cli_kind} provider process failed with exit {capture.exit_code}"
            )
        if len(capture.lines) < 2:
            raise ManagedProviderError("provider stream did not contain multiple canonical events")
        parsed = (
            self._parse_claude(capture.lines)
            if spec.cli_kind == "claude"
            else self._parse_codex(capture.lines)
        )
        if str(parsed["final_text"]).strip() != expected_token:
            raise ManagedProviderError("provider final response does not match the tool-owned file")
        tool_calls = tuple(sorted(parsed["tool_call_ids"]))
        tool_results = tuple(sorted(parsed["tool_result_ids"]))
        if not tool_calls or set(tool_calls) != set(tool_results):
            raise ManagedProviderError("provider stream lacks a matching real tool call/result roundtrip")
        observed_model = str(parsed.get("model_id") or spec.model_id)
        if not self._model_matches(spec.model_id, observed_model):
            raise ManagedProviderError(
                f"provider returned another model: expected={spec.model_id}; observed={observed_model}"
            )
        request_id = str(parsed.get("request_id") or "")
        if not request_id or parsed.get("terminal") is not True:
            raise ManagedProviderError("provider stream lacks a terminal request/thread identity")
        chunks = [
            {
                "sequence": item.sequence,
                "event": str(item.value.get("type") or "provider_event"),
                "content_digest": item.digest,
                "byte_count": len(item.raw.encode("utf-8")),
                "timestamp": item.timestamp,
                "tool_call_id": self._line_tool_id(item.value),
                "finish_reason": (
                    "end_turn" if item.sequence == capture.lines[-1].sequence else ""
                ),
            }
            for item in capture.lines
        ]
        request_record = {
            "provider_id": spec.provider_id,
            "model_id": spec.model_id,
            "dialect": spec.dialect.value,
            "request_path": spec.request_path,
            "prompt_digest": stable_digest(prompt),
            "tool_policy": "single-read-only-file",
            "attempt_id": attempt_id,
            "run_id": run_id,
        }
        retry_count = sum(
            1
            for item in capture.lines
            if item.value.get("type") == "error"
            and "reconnect" in str(item.value.get("message") or "").lower()
        )
        return ProviderWireAttestation.from_mapping(
            {
                "provider_id": spec.provider_id,
                "model_id": observed_model,
                "dialect": spec.dialect.value,
                "endpoint": spec.endpoint,
                "request_id": request_id,
                "attempt_id": attempt_id,
                "route_id": f"managed-cli:{spec.cli_kind}:{attempt_id}",
                "request_method": "POST",
                "request_path": spec.request_path,
                "request_headers": {},
                "request_digest": stable_digest(request_record),
                "response_status": 200,
                "response_headers": {},
                "response_digest": capture.stdout_digest,
                "chunks": chunks,
                "tool_call_ids": tool_calls,
                "tool_result_ids": tool_results,
                "started_at": capture.started_at,
                "completed_at": capture.completed_at,
                "retry_count": retry_count,
                "simulated": False,
                "metadata": {
                    "transport_kind": "provider_owned_managed_cli",
                    "authenticated_session": True,
                    "auth_custodian": "provider_cli",
                    "direct_wire_headers_observed": False,
                    "status_source": "provider_cli_success_exit",
                    "cli_kind": spec.cli_kind,
                    "cli_version": capture.executable_version,
                    "command_id": capture.command_id,
                    "stdout_line_count": len(capture.lines),
                    "stderr_digest": capture.stderr_digest,
                    "stderr_bytes": capture.stderr_bytes,
                    "final_text_digest": stable_digest(parsed["final_text"]),
                    "provider_request_ids": list(parsed.get("request_ids") or ()),
                    "run_id": run_id,
                },
            }
        )

    @staticmethod
    def _command(spec: ManagedProviderSpec, *, workspace: Path, prompt: str) -> list[str]:
        if spec.cli_kind == "claude":
            return [
                spec.executable,
                "-p",
                prompt,
                "--output-format",
                "stream-json",
                "--include-partial-messages",
                "--verbose",
                "--model",
                spec.model_id,
                "--tools",
                "Read",
                "--allowedTools",
                "Read",
                "--permission-mode",
                "dontAsk",
                "--safe-mode",
                "--no-session-persistence",
                "--no-chrome",
                "--disable-slash-commands",
                "--max-budget-usd",
                f"{spec.maximum_budget_usd:.2f}",
            ]
        return [
            spec.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "--json",
            "-m",
            spec.model_id,
            "-C",
            str(workspace),
            prompt,
        ]

    @staticmethod
    def _capture(
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_seconds: float,
        executable_version: str,
    ) -> ProcessCapture:
        started_at = utc_now()
        command_id = "managed-provider-" + stable_digest(
            {"command": Path(command[0]).name, "cwd": str(cwd), "started_at": started_at}
        )[:20]
        environment = os.environ.copy()
        environment.update({"NO_COLOR": "1", "CI": "1"})
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
        stderr_parts: list[str] = []

        def collect_stderr() -> None:
            if process.stderr is not None:
                stderr_parts.extend(process.stderr)

        stderr_thread = threading.Thread(target=collect_stderr, daemon=True)
        stderr_thread.start()
        captured: list[CapturedLine] = []
        try:
            if process.stdout is None:
                raise ManagedProviderError("provider process did not expose stdout")
            for raw in process.stdout:
                stripped = raw.rstrip("\r\n")
                if not stripped:
                    continue
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError:
                    value = {"type": "unparsed_provider_output", "digest": stable_digest(stripped)}
                if not isinstance(value, Mapping):
                    value = {"type": "scalar_provider_output", "digest": stable_digest(value)}
                captured.append(
                    CapturedLine(
                        sequence=len(captured),
                        timestamp=utc_now(),
                        raw=stripped,
                        value=dict(value),
                    )
                )
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            process.kill()
            process.wait(timeout=10)
            raise ManagedProviderError("managed provider process exceeded its bounded timeout") from error
        finally:
            stderr_thread.join(timeout=5)
        stderr = "".join(stderr_parts).encode("utf-8")
        return ProcessCapture(
            command_id=command_id,
            started_at=started_at,
            completed_at=utc_now(),
            exit_code=int(process.returncode or 0),
            lines=tuple(captured),
            stderr_digest=hashlib.sha256(stderr).hexdigest(),
            stderr_bytes=len(stderr),
            executable_version=executable_version,
        )

    @staticmethod
    def _version(executable: str) -> str:
        try:
            completed = subprocess.run(
                [executable, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
                creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            return "unavailable"
        return (completed.stdout or "").strip()[:256]

    @staticmethod
    def _parse_claude(lines: Iterable[CapturedLine]) -> Mapping[str, Any]:
        tool_calls: set[str] = set()
        tool_results: set[str] = set()
        request_ids: set[str] = set()
        final_text = ""
        model_id = ""
        terminal = False
        for line in lines:
            value = line.value
            if value.get("type") == "system" and value.get("subtype") == "init":
                model_id = str(value.get("model") or model_id)
            if value.get("type") == "assistant":
                message = value.get("message") if isinstance(value.get("message"), Mapping) else {}
                model_id = str(message.get("model") or model_id)
                if value.get("request_id"):
                    request_ids.add(str(value["request_id"]))
                for item in ManagedProviderProbe._content_items(message.get("content")):
                    if item.get("type") == "tool_use" and item.get("id"):
                        tool_calls.add(str(item["id"]))
                    if item.get("type") == "text":
                        final_text = str(item.get("text") or final_text)
            if value.get("type") == "user":
                message = value.get("message") if isinstance(value.get("message"), Mapping) else {}
                for item in ManagedProviderProbe._content_items(message.get("content")):
                    if item.get("type") == "tool_result" and item.get("tool_use_id"):
                        tool_results.add(str(item["tool_use_id"]))
            if value.get("type") == "result" and value.get("subtype") == "success":
                final_text = str(value.get("result") or final_text)
                terminal = str(value.get("stop_reason") or "") in {"end_turn", "stop_sequence"}
            if value.get("request_id"):
                request_ids.add(str(value["request_id"]))
        return {
            "tool_call_ids": tool_calls,
            "tool_result_ids": tool_results,
            "request_ids": sorted(request_ids),
            "request_id": sorted(request_ids)[0] if request_ids else "",
            "final_text": final_text,
            "model_id": model_id,
            "terminal": terminal,
        }

    @staticmethod
    def _parse_codex(lines: Iterable[CapturedLine]) -> Mapping[str, Any]:
        tool_calls: set[str] = set()
        tool_results: set[str] = set()
        thread_id = ""
        final_text = ""
        terminal = False
        for line in lines:
            value = line.value
            if value.get("type") == "thread.started":
                thread_id = str(value.get("thread_id") or thread_id)
            item = value.get("item") if isinstance(value.get("item"), Mapping) else {}
            if item.get("type") == "command_execution" and item.get("id"):
                identity = str(item["id"])
                if value.get("type") == "item.started":
                    tool_calls.add(identity)
                if (
                    value.get("type") == "item.completed"
                    and item.get("status") == "completed"
                    and int(item.get("exit_code") or 0) == 0
                ):
                    tool_results.add(identity)
            if value.get("type") == "item.completed" and item.get("type") == "agent_message":
                final_text = str(item.get("text") or final_text)
            if value.get("type") == "turn.completed":
                terminal = True
        return {
            "tool_call_ids": tool_calls,
            "tool_result_ids": tool_results,
            "request_ids": [thread_id] if thread_id else [],
            "request_id": thread_id,
            "final_text": final_text,
            "model_id": "",
            "terminal": terminal,
        }

    @staticmethod
    def _content_items(value: Any) -> tuple[Mapping[str, Any], ...]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            return ()
        return tuple(item for item in value if isinstance(item, Mapping))

    @staticmethod
    def _line_tool_id(value: Mapping[str, Any]) -> str:
        item = value.get("item") if isinstance(value.get("item"), Mapping) else {}
        if item.get("type") == "command_execution":
            return str(item.get("id") or "")
        message = value.get("message") if isinstance(value.get("message"), Mapping) else {}
        for content in ManagedProviderProbe._content_items(message.get("content")):
            if content.get("type") == "tool_use":
                return str(content.get("id") or "")
            if content.get("type") == "tool_result":
                return str(content.get("tool_use_id") or "")
        return ""

    @staticmethod
    def _model_matches(expected: str, observed: str) -> bool:
        left = expected.strip().lower()
        right = observed.strip().lower()
        return left == right or left in right or right in left

    @staticmethod
    def _trace_payload(
        spec: ManagedProviderSpec,
        capture: ProcessCapture,
        attestation: ProviderWireAttestation,
    ) -> Mapping[str, Any]:
        return {
            "schema": "zyra.managed-provider-trace/v1",
            "provider_id": spec.provider_id,
            "model_id": attestation.model_id,
            "dialect": spec.dialect.value,
            "endpoint": spec.endpoint,
            "request_path": spec.request_path,
            "command_id": capture.command_id,
            "started_at": capture.started_at,
            "completed_at": capture.completed_at,
            "exit_code": capture.exit_code,
            "stdout_digest": capture.stdout_digest,
            "stderr_digest": capture.stderr_digest,
            "stderr_bytes": capture.stderr_bytes,
            "executable_version": capture.executable_version,
            "events": [
                {
                    "sequence": item.sequence,
                    "timestamp": item.timestamp,
                    "type": str(item.value.get("type") or ""),
                    "digest": item.digest,
                    "byte_count": len(item.raw.encode("utf-8")),
                    "tool_id": ManagedProviderProbe._line_tool_id(item.value),
                }
                for item in capture.lines
            ],
            "attestation": attestation.to_dict(),
        }


__all__ = [
    "CapturedLine",
    "ManagedProviderError",
    "ManagedProviderProbe",
    "ManagedProviderReceipt",
    "ManagedProviderSpec",
    "ProcessCapture",
]
