from __future__ import annotations

import copy
import json
import re
import subprocess
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse
from urllib.request import Request, url2pathname, urlopen

from zyra_core import ArtifactKind, EventRecord, EventType, to_jsonable

from .artifacts import LocalArtifactStore
from .permissions import (
    JsonPermissionStore,
    PermissionDecision,
    PermissionEffect,
    PermissionOperation,
    PermissionRequest,
    ToolPermissionPolicy,
)
from .tools import (
    DynamicToolProvenance,
    ProvenancedDynamicHandler,
    ToolCall,
    ToolRegistry,
    ToolResult,
    default_tool_registry,
)


@dataclass(slots=True)
class ToolExecutionContext:
    workspace_root: Path
    artifact_store: LocalArtifactStore
    permission_policy: ToolPermissionPolicy
    registry: ToolRegistry
    permission_store: JsonPermissionStore | None = None
    event_reader: Callable[[str], list[dict[str, Any]]] | None = None
    checkpoint_reader: Callable[[str], dict[str, Any] | None] | None = None
    max_inline_chars: int = 12000
    shell_timeout_seconds: int = 30
    dynamic_handlers: Mapping[str, Callable[[ToolCall], ToolResult]] = field(default_factory=dict)

    @classmethod
    def for_workspace(
        cls,
        workspace_root: str | Path,
        artifact_root: str | Path,
        *,
        registry: ToolRegistry | None = None,
        permission_policy: ToolPermissionPolicy | None = None,
        permission_store: JsonPermissionStore | None = None,
        event_reader: Callable[[str], list[dict[str, Any]]] | None = None,
        checkpoint_reader: Callable[[str], dict[str, Any] | None] | None = None,
        dynamic_handlers: Mapping[str, Callable[[ToolCall], ToolResult]] | None = None,
    ) -> "ToolExecutionContext":
        root = Path(workspace_root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        artifact_store = LocalArtifactStore(artifact_root)
        rules = permission_store.list_rules() if permission_store is not None else None
        return cls(
            workspace_root=root,
            artifact_store=artifact_store,
            permission_policy=permission_policy or ToolPermissionPolicy.for_workspace(root, rules=rules),
            registry=registry or default_tool_registry(),
            permission_store=permission_store,
            event_reader=event_reader,
            checkpoint_reader=checkpoint_reader,
            dynamic_handlers=dict(dynamic_handlers or {}),
        )


@dataclass(frozen=True, slots=True)
class _PermissionExecutionView:
    """Immutable subset presented to the permission authority.

    Tool handlers use the same workspace/registry snapshot after validation.
    A caller cannot swap the mutable public context between grant validation
    and the side-effect boundary.
    """

    workspace_root: Path
    registry: ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        context: ToolExecutionContext,
        *,
        permission_authority: Any | None = None,
    ) -> None:
        self.context = context
        self._permission_authority = permission_authority
        self._workspace_root = Path(context.workspace_root).resolve()
        self._artifact_store = context.artifact_store
        self._permission_policy = context.permission_policy
        self._registry = context.registry
        self._permission_store = context.permission_store
        self._event_reader = context.event_reader
        self._checkpoint_reader = context.checkpoint_reader
        self._max_inline_chars = int(context.max_inline_chars)
        self._shell_timeout_seconds = int(context.shell_timeout_seconds)
        # Registry materialization and executable handlers form one immutable
        # snapshot.  A list-changed notification must create a new execution
        # context; mutating a shared handler map mid-call would break the
        # permission identity that was approved for this exact ToolSpec.
        self._dynamic_handlers = dict(context.dynamic_handlers)
        self._permission_execution_view = _PermissionExecutionView(
            workspace_root=self._workspace_root,
            registry=self._registry,
        )

    def execute(
        self,
        call: ToolCall,
        *,
        permission_grant: Any | None = None,
    ) -> ToolResult:
        # ``ToolCall`` is frozen but its mapping fields are not.  Detach them
        # before permission validation and execute only this private copy.
        # This closes argument mutation between validation and handler use.
        call = replace(
            call,
            arguments=copy.deepcopy(dict(call.arguments)),
            metadata=copy.deepcopy(dict(call.metadata)),
        )
        spec = self._registry.get(call.tool_name)
        if spec is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Unknown tool: {call.tool_name}",
                error="unknown_tool",
                metadata={"tool_name": call.tool_name},
            )

        dynamic_handler = self._dynamic_handlers.get(call.tool_name)
        dynamic_provenance: DynamicToolProvenance | None = None
        if dynamic_handler is not None:
            registered_provenance = self._registry.execution_provenance(call.tool_name)
            if not isinstance(dynamic_handler, ProvenancedDynamicHandler):
                return self._invalid_dynamic_handler_result(
                    call,
                    "dynamic handler has no immutable execution provenance",
                )
            dynamic_provenance = dynamic_handler.provenance
            # Identity comparison is deliberate: a value-equivalent record
            # reconstructed by an untrusted plugin is not the capability that
            # projection deposited into this registry snapshot.
            if registered_provenance is None or dynamic_provenance is not registered_provenance:
                return self._invalid_dynamic_handler_result(
                    call,
                    "dynamic handler provenance does not match the registry snapshot",
                )
            # PermissionRuntime treats call metadata only as an optional
            # consistency echo.  Replace any caller/display values with the
            # registry-owned identity before validation so an attacker can
            # neither downgrade MCP to builtin nor turn a valid exact grant
            # into a confused-deputy mismatch.
            trusted_metadata = dict(call.metadata)
            trusted_metadata.update(
                {
                    "tool_namespace": dynamic_provenance.namespace,
                    "namespace": dynamic_provenance.namespace,
                    "server_id": dynamic_provenance.server_id,
                    "server_name": dynamic_provenance.server_id,
                    "tool_version": dynamic_provenance.version,
                }
            )
            call = replace(call, metadata=trusted_metadata)

        authorized = False
        if permission_grant is not None:
            if (
                dynamic_provenance is not None
                and dynamic_provenance.requires_exact_grant
                and not self._grant_matches_dynamic_provenance(
                    permission_grant,
                    dynamic_provenance,
                )
            ):
                return self._invalid_grant_result(
                    call,
                    "permission grant does not match immutable dynamic handler provenance",
                )
            # The executor owns its authority binding.  A model/plugin/caller
            # cannot provide an arbitrary ``lambda: True`` validator.
            try:
                from .permission.runtime import ToolPermissionRuntime
            except ImportError:
                return self._invalid_grant_result(call, "permission authority is unavailable")
            if not isinstance(self._permission_authority, ToolPermissionRuntime):
                return self._invalid_grant_result(call, "permission authority is missing or untrusted")
            try:
                authorized = bool(
                    self._permission_authority.validate_and_consume(
                        call,
                        permission_grant,
                        self._permission_execution_view,
                    )
                )
            except Exception as error:  # noqa: BLE001 - grant failures must fail closed.
                return self._invalid_grant_result(
                    call,
                    f"permission grant validation failed: {type(error).__name__}",
                )
            if not authorized:
                return self._invalid_grant_result(call, "permission grant was rejected or already consumed")

        # The legacy workspace policy is a useful path/shell classifier, but it
        # is not an execution capability.  Every operation that can mutate
        # state or cross an external boundary must arrive with a one-use grant
        # minted by ToolPermissionRuntime.  This check deliberately lives at
        # the last side-effect boundary so direct ToolExecutor callers cannot
        # bypass the runtime by relying on an old ALLOW decision.
        try:
            if call.tool_name == "file_read":
                result = self._file_read(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "file_write":
                result = self._file_write(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "file_edit":
                result = self._file_edit(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "shell":
                result = self._shell(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "browser":
                result = self._browser(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "web_search":
                result = self._web_search(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "artifact_write":
                result = self._artifact_write(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "checkpoint":
                result = self._checkpoint(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if call.tool_name == "trace":
                result = self._trace(call, authorized=authorized)
                return self._stamp_grant(result, permission_grant, call) if authorized else result
            if dynamic_handler is not None:
                # Dynamic callables are executable capabilities, never passive
                # read-only metadata.  Every registered handler requires a
                # one-use grant; MCP additionally matches exact namespace and
                # canonical server identity before the grant is consumed.
                if not authorized:
                    return self._missing_grant_result(call)
                result = dynamic_handler(call)
                if not isinstance(result, ToolResult):
                    raise TypeError(
                        f"dynamic handler for {call.tool_name!r} returned "
                        f"{type(result).__name__}, expected ToolResult"
                    )
                if result.tool_call_id != call.tool_call_id:
                    raise ValueError(
                        "dynamic handler returned a ToolResult for a different tool call"
                    )
                return self._stamp_grant(result, permission_grant, call) if authorized else result
        except subprocess.TimeoutExpired as error:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"{call.tool_name} timed out after {error.timeout} second(s)",
                error="tool_timeout",
                metadata={
                    "message": str(error),
                    "timeout_seconds": str(error.timeout),
                    "failure_kind": "timeout",
                },
            )
        except Exception as error:  # noqa: BLE001 - execution errors must become traceable results.
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"{call.tool_name} failed",
                error=type(error).__name__,
                metadata={"message": str(error)},
            )

        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary=f"Tool is registered but has no executor implementation: {call.tool_name}",
            error="tool_not_implemented",
            metadata={"tool_name": call.tool_name},
        )

    def _file_read(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        target = self._resolve_path(call.arguments.get("path"))
        permission = self._permission_policy.decide_read(target)
        blocked = None if authorized else self._blocked_result(call, permission, PermissionOperation.READ, str(target))
        if blocked is not None:
            return blocked

        content = target.read_text(encoding=str(call.arguments.get("encoding") or "utf-8"))
        output: dict[str, Any] = {
            "path": str(target),
            "relative_path": self._relative_path(target),
            "chars": len(content),
        }
        artifacts = []
        if len(content) > self._max_inline_chars:
            artifact = self._artifact_store.write_text(
                run_id=call.run_id,
                task_id=call.task_id,
                content=content,
                title=f"file_read:{self._relative_path(target)}",
                kind=ArtifactKind.TEXT,
                extension=".txt",
                producer_node_id=call.node_id,
            )
            artifacts.append(artifact)
            output["content_preview"] = content[: self._max_inline_chars]
            output["truncated"] = True
        else:
            output["content"] = content
            output["truncated"] = False

        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Read {self._relative_path(target)}",
            output=output,
            artifacts=artifacts,
            metadata={"permission_effect": str(permission.effect)},
        )

    def _file_write(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        target = self._resolve_path(call.arguments.get("path"))
        permission = self._permission_policy.decide_write(target)
        blocked = None if authorized else self._blocked_result(call, permission, PermissionOperation.WRITE, str(target))
        if blocked is not None:
            return blocked
        if not authorized:
            return self._missing_grant_result(call)

        content = str(call.arguments.get("content") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding=str(call.arguments.get("encoding") or "utf-8"))
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Wrote {self._relative_path(target)}",
            output={
                "path": str(target),
                "relative_path": self._relative_path(target),
                "chars": len(content),
            },
            metadata={"permission_effect": str(permission.effect)},
        )

    def _file_edit(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        target = self._resolve_path(call.arguments.get("path"))
        read_permission = self._permission_policy.decide_read(target)
        blocked = None if authorized else self._blocked_result(call, read_permission, PermissionOperation.READ, str(target))
        if blocked is not None:
            return blocked
        write_permission = self._permission_policy.decide_write(target)
        blocked = None if authorized else self._blocked_result(call, write_permission, PermissionOperation.WRITE, str(target))
        if blocked is not None:
            return blocked
        if not authorized:
            return self._missing_grant_result(call)

        old = call.arguments.get("old")
        if old is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="file_edit requires an exact old string",
                error="missing_old",
            )
        old_text = str(old)
        new_text = str(call.arguments.get("new") or "")
        replace_all = bool(call.arguments.get("replace_all", False))
        content = target.read_text(encoding=str(call.arguments.get("encoding") or "utf-8"))
        occurrences = content.count(old_text)
        if occurrences == 0:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"No exact match found in {self._relative_path(target)}",
                error="old_text_not_found",
                output={"relative_path": self._relative_path(target)},
            )
        if occurrences > 1 and not replace_all:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Multiple matches found in {self._relative_path(target)}",
                error="ambiguous_edit",
                output={"relative_path": self._relative_path(target), "matches": occurrences},
            )

        edited = content.replace(old_text, new_text) if replace_all else content.replace(old_text, new_text, 1)
        target.write_text(edited, encoding=str(call.arguments.get("encoding") or "utf-8"))
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Edited {self._relative_path(target)}",
            output={
                "path": str(target),
                "relative_path": self._relative_path(target),
                "replacements": occurrences if replace_all else 1,
            },
            metadata={"permission_effect": str(write_permission.effect)},
        )

    def _shell(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        command = str(call.arguments.get("command") or "")
        if not command.strip():
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="shell requires a command",
                error="missing_command",
            )

        permission = self._permission_policy.decide_shell(command)
        # A model-supplied ``approved`` argument is data, not authorization.
        # Only a one-time grant consumed by this executor may bypass ASK.
        blocked = None if authorized else self._blocked_result(call, permission, PermissionOperation.SHELL, command)
        if blocked is not None:
            return blocked
        if not authorized:
            return self._missing_grant_result(call)

        completed = subprocess.run(
            command,
            cwd=self._workspace_root,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
            timeout=int(call.arguments.get("timeout_seconds") or self._shell_timeout_seconds),
        )
        stdout_artifact = self._large_output_artifact(call, "stdout", completed.stdout)
        stderr_artifact = self._large_output_artifact(call, "stderr", completed.stderr)
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=completed.returncode == 0,
            summary=f"Shell exited with code {completed.returncode}",
            output={
                "command": command,
                "returncode": completed.returncode,
                "stdout": self._inline_text(completed.stdout),
                "stderr": self._inline_text(completed.stderr),
                "stdout_truncated": stdout_artifact is not None,
                "stderr_truncated": stderr_artifact is not None,
            },
            artifacts=[item for item in [stdout_artifact, stderr_artifact] if item is not None],
            error=None if completed.returncode == 0 else "non_zero_exit",
            metadata={
                "permission_effect": str(permission.effect),
                "permission_execution_grant_present": str(authorized).lower(),
                "raw_approved_argument_ignored": str(call.arguments.get("approved") is True).lower(),
            },
        )

    def _artifact_write(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        if not authorized:
            return self._missing_grant_result(call)
        kind_value = str(call.arguments.get("kind") or ArtifactKind.TEXT)
        try:
            kind = ArtifactKind(kind_value)
        except ValueError:
            kind = ArtifactKind.TEXT
        content = str(call.arguments.get("content") or "")
        artifact = self._artifact_store.write_text(
            run_id=call.run_id,
            task_id=call.task_id,
            content=content,
            title=str(call.arguments.get("title") or "Tool artifact"),
            kind=kind,
            extension=str(call.arguments.get("extension") or ".txt"),
            producer_node_id=call.node_id,
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Wrote artifact {artifact.artifact_id}",
            output={"artifact_id": artifact.artifact_id, "uri": artifact.uri},
            artifacts=[artifact],
        )

    def _browser(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        action = str(call.arguments.get("action") or "snapshot_state")
        if action not in {"open_url", "extract_text", "snapshot_state", "navigate", "extract", "find_elements"}:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Unsupported browser action: {action}",
                error="unsupported_browser_action",
                metadata={"action": action},
            )
        html_content = str(call.arguments.get("html") or "")
        source = "inline_html"
        url = str(call.arguments.get("url") or "")
        if url and call.arguments.get("allow_network") is not True and not authorized:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="Network access is not allowed for this browser call.",
                error="network_not_allowed",
            )
        if not authorized:
            return self._missing_grant_result(call)
        if not html_content:
            if not url:
                return ToolResult(
                    tool_call_id=call.tool_call_id,
                    ok=False,
                    summary="browser requires html or url",
                    error="missing_browser_source",
                )
            fetched = self._fetch_search_source(call, url, authorized=authorized)
            if isinstance(fetched, ToolResult):
                return fetched
            source, html_content = fetched
        state = _browser_state(source if source != "inline_html" else url, html_content)
        artifacts = []
        if call.arguments.get("capture_html") is True or action in {"open_url", "navigate"}:
            html_artifact = self._artifact_store.write_text(
                run_id=call.run_id,
                task_id=call.task_id,
                content=html_content,
                title="browser raw HTML",
                kind=ArtifactKind.FILE,
                extension=".html",
                producer_node_id=call.node_id,
            )
            artifacts.append(html_artifact)
        state_artifact = self._artifact_store.write_text(
            run_id=call.run_id,
            task_id=call.task_id,
            content=json.dumps(state, ensure_ascii=False, indent=2),
            title="browser state snapshot",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=call.node_id,
        )
        artifacts.append(state_artifact)
        if action in {"extract_text", "extract"}:
            text_artifact = self._artifact_store.write_text(
                run_id=call.run_id,
                task_id=call.task_id,
                content=str(state["text"]),
                title="browser extracted text",
                kind=ArtifactKind.MARKDOWN,
                extension=".md",
                producer_node_id=call.node_id,
            )
            artifacts.append(text_artifact)
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Captured browser state from {state['url'] or source}",
            output={
                "action": action,
                "state": {
                    "url": state["url"],
                    "title": state["title"],
                    "text_preview": str(state["text"])[:1000],
                    "html_chars": state["html_chars"],
                    "text_chars": state["text_chars"],
                    "links": state["links"][:20],
                    "link_count": len(state["links"]),
                },
            },
            artifacts=artifacts,
            metadata={"mode": "inline_html" if source == "inline_html" else "url"},
        )

    def _checkpoint(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        if self._checkpoint_reader is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="checkpoint requires a checkpoint reader in the execution context",
                error="checkpoint_reader_missing",
            )
        checkpoint = self._checkpoint_reader(call.task_id)
        if checkpoint is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Checkpoint not found for task {call.task_id}",
                error="checkpoint_not_found",
            )
        summary = _checkpoint_summary(checkpoint)
        artifacts = []
        if call.arguments.get("write_artifact") is True:
            if not authorized:
                return self._missing_grant_result(call)
            # Reading a checkpoint is safe; materializing a new artifact is a
            # distinct mutation and requires the grant already consumed by the
            # dispatcher.
            artifact = self._artifact_store.write_text(
                run_id=call.run_id,
                task_id=call.task_id,
                content=json.dumps(checkpoint, ensure_ascii=False, indent=2),
                title=f"checkpoint:{call.task_id}",
                kind=ArtifactKind.STRUCTURED_DATA,
                extension=".json",
                producer_node_id=call.node_id,
            )
            artifacts.append(artifact)
        output: dict[str, Any] = {"summary": summary}
        if call.arguments.get("include_state") is True:
            output["checkpoint"] = checkpoint
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Read checkpoint for task {call.task_id}",
            output=output,
            artifacts=artifacts,
        )

    def _web_search(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        query = str(call.arguments.get("query") or "").strip()
        if not query:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="web_search requires a query",
                error="missing_query",
            )

        max_results = _bounded_int(call.arguments.get("max_results"), default=10, minimum=1, maximum=50)
        results: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        source_url = str(call.arguments.get("url") or "").strip()
        if source_url and call.arguments.get("allow_network") is not True and not authorized:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="Network access is not allowed for this search call.",
                error="network_not_allowed",
            )
        if not authorized:
            return self._missing_grant_result(call)
        if source_url:
            fetched = self._fetch_search_source(call, source_url, authorized=authorized)
            if isinstance(fetched, ToolResult):
                return fetched
            source_name, content = fetched
            results.extend(_search_text(query, content, source_name, max_results))
        else:
            paths = call.arguments.get("paths")
            search_paths = paths if isinstance(paths, list) and paths else ["."]
            for raw_path in search_paths:
                for file_path in self._iter_search_files(raw_path, skipped, authorized=authorized):
                    if len(results) >= max_results:
                        break
                    content = file_path.read_text(encoding=str(call.arguments.get("encoding") or "utf-8"), errors="ignore")
                    results.extend(_search_text(query, content, self._relative_path(file_path), max_results - len(results)))

        artifact = self._artifact_store.write_text(
            run_id=call.run_id,
            task_id=call.task_id,
            content=_format_search_results(query, results, skipped),
            title=f"web_search:{query}",
            kind=ArtifactKind.TRACE,
            extension=".md",
            producer_node_id=call.node_id,
        )
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"web_search found {len(results)} result(s)",
            output={
                "query": query,
                "result_count": len(results),
                "results": results,
                "skipped": skipped[:20],
                "artifact_id": artifact.artifact_id,
            },
            artifacts=[artifact],
            metadata={"mode": "url" if source_url else "workspace"},
        )

    def _trace(self, call: ToolCall, *, authorized: bool = False) -> ToolResult:
        if self._event_reader is None:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="trace requires an event reader in the execution context",
                error="trace_reader_missing",
            )
        limit = _bounded_int(call.arguments.get("limit"), default=20, minimum=1, maximum=500)
        event_type = str(call.arguments.get("event_type") or "").strip()
        events = self._event_reader(call.task_id)
        if event_type:
            events = [event for event in events if str(event.get("event_type") or "") == event_type]
        selected = events[-limit:]
        artifacts = []
        if call.arguments.get("write_artifact") is True:
            if not authorized:
                return self._missing_grant_result(call)
            artifact = self._artifact_store.write_text(
                run_id=call.run_id,
                task_id=call.task_id,
                content=json.dumps(selected, ensure_ascii=False, indent=2),
                title=f"trace:{call.task_id}",
                kind=ArtifactKind.TRACE,
                extension=".json",
                producer_node_id=call.node_id,
            )
            artifacts.append(artifact)
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=True,
            summary=f"Read {len(selected)} trace event(s)",
            output={
                "event_count": len(events),
                "returned_count": len(selected),
                "event_type": event_type,
                "events": selected,
            },
            artifacts=artifacts,
        )

    def _large_output_artifact(self, call: ToolCall, stream_name: str, content: str):
        if len(content) <= self._max_inline_chars:
            return None
        return self._artifact_store.write_text(
            run_id=call.run_id,
            task_id=call.task_id,
            content=content,
            title=f"shell:{stream_name}:{call.tool_call_id}",
            kind=ArtifactKind.TEXT,
            extension=".txt",
            producer_node_id=call.node_id,
        )

    def _inline_text(self, content: str) -> str:
        if len(content) <= self._max_inline_chars:
            return content
        return content[: self._max_inline_chars]

    def _resolve_path(self, value: Any) -> Path:
        if value is None:
            raise ValueError("path is required")
        path = Path(str(value))
        if not path.is_absolute():
            path = self._workspace_root / path
        return path.resolve()

    def _relative_path(self, path: Path) -> str:
        try:
            return str(path.relative_to(self._workspace_root))
        except ValueError:
            return str(path)

    def _iter_search_files(
        self,
        raw_path: Any,
        skipped: list[dict[str, str]],
        *,
        authorized: bool = False,
    ) -> list[Path]:
        target = self._resolve_path(raw_path)
        permission = self._permission_policy.decide_read(target)
        if not authorized and permission.effect == PermissionEffect.DENY:
            skipped.append({"path": str(raw_path), "reason": permission.reason})
            return []
        if target.is_file():
            return [target] if _looks_textual(target) else []
        if not target.exists():
            skipped.append({"path": str(raw_path), "reason": "path_not_found"})
            return []
        if not target.is_dir():
            skipped.append({"path": str(raw_path), "reason": "not_a_file_or_directory"})
            return []
        files: list[Path] = []
        for file_path in target.rglob("*"):
            if len(files) >= 200:
                skipped.append({"path": self._relative_path(target), "reason": "file_scan_limit_reached"})
                break
            if not file_path.is_file() or not _looks_textual(file_path):
                continue
            file_permission = self._permission_policy.decide_read(file_path)
            if not authorized and file_permission.effect == PermissionEffect.DENY:
                skipped.append({"path": self._relative_path(file_path), "reason": file_permission.reason})
                continue
            files.append(file_path)
        return files

    def _fetch_search_source(
        self,
        call: ToolCall,
        url: str,
        *,
        authorized: bool = False,
    ) -> tuple[str, str] | ToolResult:
        parsed = urlparse(url)
        if parsed.scheme == "file":
            path = Path(url2pathname(unquote(parsed.path)))
            if parsed.netloc:
                path = Path(f"//{parsed.netloc}{url2pathname(unquote(parsed.path))}")
            target = path.resolve()
            permission = self._permission_policy.decide_read(target)
            blocked = None if authorized else self._blocked_result(call, permission, PermissionOperation.READ, str(target))
            if blocked is not None:
                return blocked
            return str(target), target.read_text(encoding=str(call.arguments.get("encoding") or "utf-8"), errors="ignore")

        if parsed.scheme not in {"http", "https"}:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Unsupported search URL scheme: {parsed.scheme}",
                error="unsupported_url_scheme",
                metadata={"url": url},
            )
        if call.arguments.get("allow_network") is not True:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary="Network search requires allow_network=true",
                error="network_not_allowed",
                metadata={"url": url},
            )
        allowed_domains = call.arguments.get("allowed_domains")
        domains = {str(item).lower() for item in allowed_domains} if isinstance(allowed_domains, list) else set()
        hostname = (parsed.hostname or "").lower()
        if domains and hostname not in domains:
            return ToolResult(
                tool_call_id=call.tool_call_id,
                ok=False,
                summary=f"Network search domain is not allowed: {hostname}",
                error="domain_not_allowed",
                metadata={"url": url, "hostname": hostname},
            )
        request = Request(url, headers={"User-Agent": "ZyraResearchTool/0.1"})
        with urlopen(request, timeout=_bounded_int(call.arguments.get("timeout_seconds"), default=10, minimum=1, maximum=30)) as response:
            raw = response.read(self._max_inline_chars * 4)
        return url, raw.decode(str(call.arguments.get("encoding") or "utf-8"), errors="ignore")

    def _blocked_result(
        self,
        call: ToolCall,
        permission: PermissionDecision,
        operation: PermissionOperation,
        subject: str,
    ) -> ToolResult | None:
        if permission.effect == PermissionEffect.ALLOW:
            return None
        metadata = {
            "permission_effect": str(permission.effect),
            "permission_reason": permission.reason,
            "operation": str(operation),
            "raw_approved_argument_ignored": str(call.arguments.get("approved") is True).lower(),
        }
        if permission.effect == PermissionEffect.ASK and self._permission_store is not None:
            request = self._permission_store.create_request(
                PermissionRequest(
                    run_id=call.run_id,
                    task_id=call.task_id,
                    tool_call_id=call.tool_call_id,
                    operation=operation,
                    subject=subject,
                    reason=permission.reason,
                    metadata={"tool_name": call.tool_name},
                )
            )
            metadata["permission_request_id"] = request.request_id
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary=f"{operation} permission {permission.effect}",
            error="permission_required" if permission.effect == PermissionEffect.ASK else "permission_denied",
            metadata=metadata,
        )

    def _invalid_grant_result(self, call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary="Permission execution grant is invalid.",
            error="permission_grant_invalid",
            metadata={
                "permission_effect": str(PermissionEffect.DENY),
                "permission_reason": reason,
                "tool_name": call.tool_name,
                "raw_approved_argument_ignored": str(call.arguments.get("approved") is True).lower(),
            },
        )

    def _invalid_dynamic_handler_result(self, call: ToolCall, reason: str) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary="Dynamic tool registration is invalid.",
            error="dynamic_handler_provenance_invalid",
            metadata={
                "permission_effect": str(PermissionEffect.DENY),
                "permission_reason": reason,
                "tool_name": call.tool_name,
            },
        )

    def _missing_grant_result(self, call: ToolCall) -> ToolResult:
        return ToolResult(
            tool_call_id=call.tool_call_id,
            ok=False,
            summary="Permission approval is required before this side effect.",
            error="permission_required",
            metadata={
                "permission_effect": str(PermissionEffect.ASK),
                "permission_reason": "a one-use execution grant is required before side effects",
                "tool_name": call.tool_name,
                "raw_approved_argument_ignored": str(call.arguments.get("approved") is True).lower(),
            },
        )

    @staticmethod
    def _requires_execution_grant(call: ToolCall, spec: Any) -> bool:
        metadata = dict(getattr(spec, "metadata", {}) or {})
        if str(metadata.get("read_only") or "").lower() != "true":
            return True
        if str(metadata.get("mutates_workspace") or "").lower() == "true":
            return True
        if call.tool_name in {"browser", "web_search"}:
            return bool(call.arguments.get("url") or call.arguments.get("allow_network"))
        if call.tool_name in {"checkpoint", "trace"}:
            return bool(call.arguments.get("write_artifact"))
        return False

    @staticmethod
    def _grant_matches_dynamic_provenance(
        grant: Any,
        provenance: DynamicToolProvenance,
    ) -> bool:
        binding = grant.get("binding") if isinstance(grant, dict) else getattr(grant, "binding", None)
        if binding is None:
            return False

        def value(name: str) -> str:
            if isinstance(binding, Mapping):
                return str(binding.get(name) or "")
            return str(getattr(binding, name, "") or "")

        return (
            value("tool_name") == provenance.tool_name
            and value("tool_namespace") == provenance.namespace
            and value("server_name") == provenance.server_id
        )

    def _stamp_grant(self, result: ToolResult, grant: Any, call: ToolCall) -> ToolResult:
        def value(name: str) -> str:
            if isinstance(grant, dict):
                direct = grant.get(name)
                binding = grant.get("binding")
                nested = binding.get(name) if isinstance(binding, dict) else None
                return str(direct or nested or "")
            direct = getattr(grant, name, "")
            binding = getattr(grant, "binding", None)
            nested = getattr(binding, name, "") if binding is not None else ""
            return str(direct or nested or "")

        return ToolResult(
            tool_call_id=result.tool_call_id,
            ok=result.ok,
            summary=result.summary,
            output=result.output,
            artifacts=result.artifacts,
            error=result.error,
            completed_at=result.completed_at,
            metadata={
                **result.metadata,
                "permission_effect": str(PermissionEffect.ALLOW),
                "permission_decision_id": value("decision_id"),
                "permission_request_id": value("request_id"),
                "permission_arguments_digest": value("arguments_digest"),
                "permission_execution_grant_consumed": "true",
                "raw_approved_argument_ignored": str(call.arguments.get("approved") is True).lower(),
            },
        )


def tool_result_event(call: ToolCall, result: ToolResult) -> EventRecord:
    payload = {
        "tool_call": to_jsonable(call),
        "tool_result": to_jsonable(result),
    }
    return EventRecord(
        run_id=call.run_id,
        task_id=call.task_id,
        node_id=call.node_id,
        event_type=EventType.AGENT_MESSAGE,
        payload=payload,
    )


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))


def _looks_textual(path: Path) -> bool:
    return path.suffix.lower() in {
        "",
        ".txt",
        ".md",
        ".markdown",
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".jsx",
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".htm",
        ".css",
        ".csv",
        ".xml",
    }


def _search_text(query: str, content: str, source: str, max_results: int) -> list[dict[str, Any]]:
    needle = query.lower()
    results: list[dict[str, Any]] = []
    for line_number, line in enumerate(content.splitlines(), start=1):
        if len(results) >= max_results:
            break
        if needle in line.lower():
            results.append(
                {
                    "source": source,
                    "line": line_number,
                    "preview": line.strip()[:500],
                    "score": 1.0,
                }
            )
    if not results and needle in content.lower() and max_results > 0:
        index = content.lower().find(needle)
        start = max(index - 120, 0)
        end = min(index + len(query) + 120, len(content))
        results.append(
            {
                "source": source,
                "line": None,
                "preview": content[start:end].replace("\n", " ").strip()[:500],
                "score": 0.8,
            }
        )
    return results


def _format_search_results(query: str, results: list[dict[str, Any]], skipped: list[dict[str, str]]) -> str:
    lines = [
        "# Zyra Research Search Results",
        "",
        f"- query: `{query}`",
        f"- result_count: `{len(results)}`",
        f"- skipped_count: `{len(skipped)}`",
        "",
        "## Results",
        "",
    ]
    if not results:
        lines.append("- No matches found.")
    for index, result in enumerate(results, start=1):
        lines.extend(
            [
                f"### {index}. {result['source']}",
                "",
                f"- line: `{result.get('line')}`",
                f"- score: `{result.get('score')}`",
                "",
                str(result.get("preview") or ""),
                "",
            ]
        )
    if skipped:
        lines.extend(["## Skipped", ""])
        for item in skipped[:20]:
            lines.append(f"- {item.get('path')}: {item.get('reason')}")
    return "\n".join(lines)


def _checkpoint_summary(checkpoint: dict[str, Any]) -> dict[str, Any]:
    plan_nodes = checkpoint.get("plan_nodes") if isinstance(checkpoint.get("plan_nodes"), dict) else {}
    artifacts = checkpoint.get("artifacts") if isinstance(checkpoint.get("artifacts"), list) else []
    metadata = checkpoint.get("metadata") if isinstance(checkpoint.get("metadata"), dict) else {}
    budget = checkpoint.get("budget") if isinstance(checkpoint.get("budget"), dict) else {}
    return {
        "task_id": checkpoint.get("task_id"),
        "run_id": checkpoint.get("run_id"),
        "status": checkpoint.get("status"),
        "updated_at": checkpoint.get("updated_at"),
        "plan_nodes": len(plan_nodes),
        "artifacts": len(artifacts),
        "metadata_keys": sorted(str(key) for key in metadata.keys()),
        "budget": budget,
    }


class _HtmlTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[dict[str, str]] = []
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript"}:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag == "a":
            attrs_dict = {name: value or "" for name, value in attrs}
            href = attrs_dict.get("href")
            if href:
                self.links.append({"href": href, "text": ""})
        if tag in {"p", "br", "div", "section", "article", "li", "h1", "h2", "h3"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        text = data.strip()
        if not text:
            return
        if self._in_title:
            self.title = text
        self._parts.append(text)
        if self.links and not self.links[-1]["text"]:
            self.links[-1]["text"] = text[:120]

    def text(self) -> str:
        content = " ".join(part.strip() for part in self._parts if part.strip())
        return re.sub(r"\s+", " ", content).strip()


def _browser_state(url: str, html_content: str) -> dict[str, Any]:
    extractor = _HtmlTextExtractor()
    extractor.feed(html_content)
    text = extractor.text()
    title = extractor.title or text[:80].strip() or url
    return {
        "url": url,
        "title": title,
        "text": text,
        "html_chars": len(html_content),
        "text_chars": len(text),
        "links": extractor.links[:50],
    }
