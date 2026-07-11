from __future__ import annotations

"""Late MCP server instructions delta and compact-restore bridge."""

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from zyra_core import ArtifactKind, ArtifactRef, EventRecord, EventType, new_id, now_iso
from zyra_runtime import LocalArtifactStore

from .models import (
    JsonValue,
    McpInstructionsDelta,
    McpInstructionsDeltaAction,
    stable_digest,
)


class McpInstructionsError(RuntimeError):
    pass


class McpInstructionsStatePort(Protocol):
    def get(self, key: str, default: Any = None) -> Any: ...

    def set(self, key: str, value: Any, **kwargs: Any) -> Any: ...


@dataclass(frozen=True, slots=True)
class McpInstructionState:
    session_id: str
    server_id: str
    connection_generation: int
    revision: int
    instructions: str
    instructions_hash: str
    artifact_id: str = ""
    enabled: bool = True
    source_event_id: str = ""
    updated_at: str = field(default_factory=now_iso)

    def safe_dict(self, *, include_instructions: bool = False) -> dict[str, JsonValue]:
        return {
            "session_id": self.session_id,
            "server_id": self.server_id,
            "connection_generation": self.connection_generation,
            "revision": self.revision,
            "instructions": self.instructions if include_instructions else "<present>" if self.instructions else "",
            "instructions_hash": self.instructions_hash,
            "artifact_id": self.artifact_id,
            "enabled": self.enabled,
            "source_event_id": self.source_event_id,
            "updated_at": self.updated_at,
            "untrusted_external_content": True,
        }


class McpInstructionsRuntime:
    def __init__(
        self,
        artifact_store: LocalArtifactStore,
        *,
        state_store: McpInstructionsStatePort | None = None,
        event_sink: Callable[[EventRecord], None] | None = None,
        inline_limit_chars: int = 1_800,
        disabled: bool = False,
    ) -> None:
        if inline_limit_chars <= 0:
            raise ValueError("inline_limit_chars must be positive")
        self.artifact_store = artifact_store
        self.state_store = state_store
        self.event_sink = event_sink
        self.inline_limit_chars = inline_limit_chars
        self.disabled = disabled
        self._states: dict[tuple[str, str], McpInstructionState] = {}
        self._history: dict[str, list[McpInstructionsDelta]] = {}
        self._lock = threading.RLock()

    def update(
        self,
        *,
        session_id: str,
        server_id: str,
        connection_generation: int,
        instructions: str,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        source_event_id: str = "",
        enabled: bool = True,
    ) -> McpInstructionsDelta | None:
        if self.disabled:
            raise McpInstructionsError("McpInstructionsRuntime is disabled")
        if not session_id or not server_id:
            raise ValueError("session_id and server_id are required")
        normalized = str(instructions or "")
        digest = stable_digest(normalized) if normalized else ""
        key = (session_id, server_id)
        with self._lock:
            current = self._states.get(key)
            if current is not None and connection_generation < current.connection_generation:
                return None
            if (
                current is not None
                and connection_generation == current.connection_generation
                and current.instructions_hash == digest
                and current.enabled == enabled
            ):
                return None
            revision = (current.revision + 1) if current else 1
            action = (
                McpInstructionsDeltaAction.CLEAR
                if not normalized or not enabled
                else McpInstructionsDeltaAction.REPLACE
            )
            artifact: ArtifactRef | None = None
            if normalized:
                artifact = self.artifact_store.write_text(
                    run_id=run_id,
                    task_id=task_id,
                    content=normalized,
                    title=f"MCP instructions {server_id} generation {connection_generation}",
                    kind=ArtifactKind.TEXT,
                    extension=".txt",
                    producer_node_id=node_id,
                )
            state = McpInstructionState(
                session_id=session_id,
                server_id=server_id,
                connection_generation=connection_generation,
                revision=revision,
                instructions=normalized,
                instructions_hash=digest,
                artifact_id=artifact.artifact_id if artifact else "",
                enabled=enabled and bool(normalized),
                source_event_id=source_event_id,
            )
            delta = McpInstructionsDelta(
                server_id=server_id,
                connection_generation=connection_generation,
                revision=revision,
                action=action,
                instructions=normalized if state.enabled else "",
                source_event_id=source_event_id,
                metadata={
                    "session_id": session_id,
                    "artifact_id": state.artifact_id,
                    "instructions_hash": digest,
                    "provenance": "mcp_initialize_or_list_changed",
                    "trust_level": "external_untrusted",
                },
            )
            self._states[key] = state
            self._history.setdefault(session_id, []).append(delta)
            self._persist(state)
        self._emit(delta, state, run_id=run_id, task_id=task_id, node_id=node_id)
        return delta

    def disable_server(
        self,
        *,
        session_id: str,
        server_id: str,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
    ) -> McpInstructionsDelta | None:
        current = self.get(session_id, server_id)
        return self.update(
            session_id=session_id,
            server_id=server_id,
            connection_generation=current.connection_generation if current else 0,
            instructions="",
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
            enabled=False,
        )

    def get(self, session_id: str, server_id: str) -> McpInstructionState | None:
        with self._lock:
            return self._states.get((session_id, server_id))

    def active(self, session_id: str) -> tuple[McpInstructionState, ...]:
        with self._lock:
            values = [
                state
                for (candidate_session, _), state in self._states.items()
                if candidate_session == session_id and state.enabled and state.instructions
            ]
        return tuple(sorted(values, key=lambda item: item.server_id))

    def restore_constraints(self, session_id: str) -> list[dict[str, JsonValue]]:
        """Return the exact 02D input contract, marked as untrusted context."""

        return [
            {
                "id": f"{state.server_id}:{state.connection_generation}:{state.revision}",
                "server": state.server_id,
                "instructions": state.instructions[: self.inline_limit_chars],
                "instructions_hash": state.instructions_hash,
                "connection_generation": state.connection_generation,
                "revision": state.revision,
                "artifact_id": state.artifact_id,
                "source_event_id": state.source_event_id,
                "source_provenance": "mcp_instruction_delta",
                "trust_level": "external_untrusted",
                "untrusted": True,
            }
            for state in self.active(session_id)
        ]

    def snapshot(self, session_id: str) -> dict[str, JsonValue]:
        states = self.active(session_id)
        with self._lock:
            history = tuple(self._history.get(session_id, ()))
        return {
            "schema": "zyra.mcp-instructions-snapshot.v1",
            "session_id": session_id,
            "active": [state.safe_dict(include_instructions=True) for state in states],
            "history": [delta.to_dict(include_instructions=False) for delta in history],
            "restore_constraints": self.restore_constraints(session_id),
        }

    def restore_snapshot(self, snapshot: Mapping[str, Any]) -> int:
        if self.disabled:
            raise McpInstructionsError("McpInstructionsRuntime is disabled")
        session_id = str(snapshot.get("session_id") or "")
        raw_states = snapshot.get("active")
        if not session_id or not isinstance(raw_states, Sequence):
            raise McpInstructionsError("invalid MCP instructions snapshot")
        restored = 0
        with self._lock:
            for raw in raw_states:
                if not isinstance(raw, Mapping):
                    continue
                state = McpInstructionState(
                    session_id=session_id,
                    server_id=str(raw.get("server_id") or ""),
                    connection_generation=int(raw.get("connection_generation") or 0),
                    revision=int(raw.get("revision") or 0),
                    instructions=str(raw.get("instructions") or ""),
                    instructions_hash=str(raw.get("instructions_hash") or ""),
                    artifact_id=str(raw.get("artifact_id") or ""),
                    enabled=bool(raw.get("enabled", True)),
                    source_event_id=str(raw.get("source_event_id") or ""),
                    updated_at=str(raw.get("updated_at") or now_iso()),
                )
                key = (session_id, state.server_id)
                current = self._states.get(key)
                if current is not None and (
                    current.connection_generation,
                    current.revision,
                ) >= (state.connection_generation, state.revision):
                    continue
                self._states[key] = state
                restored += 1
        return restored

    def _persist(self, state: McpInstructionState) -> None:
        if self.state_store is None:
            return
        self.state_store.set(
            f"instructions.{state.session_id}.{state.server_id}",
            state.safe_dict(include_instructions=True),
        )

    def _emit(
        self,
        delta: McpInstructionsDelta,
        state: McpInstructionState,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None,
    ) -> None:
        if self.event_sink is None:
            return
        event_type = getattr(EventType, "MCP_INSTRUCTIONS_CHANGED", EventType.SYSTEM_NOTICE)
        self.event_sink(
            EventRecord(
                run_id=run_id,
                task_id=task_id,
                node_id=node_id,
                event_type=event_type,
                payload={
                    "mcp_runtime": {
                        "schema": "zyra.mcp-instructions-event.v1",
                        "runtime_id": "McpInstructionsRuntime",
                        "server_id": state.server_id,
                        "session_id": state.session_id,
                        "delta": delta.to_dict(include_instructions=False),
                        "state": state.safe_dict(include_instructions=False),
                    }
                },
            )
        )


__all__ = [
    "McpInstructionState",
    "McpInstructionsError",
    "McpInstructionsRuntime",
]
