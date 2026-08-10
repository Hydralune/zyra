from __future__ import annotations

from typing import Any, Iterable, Mapping, Protocol

from .canonical import digest
from .models import GatewayEvent, GatewayEventKind, GatewaySessionRecord
from .redaction import SecretRedactor
from .state_store import GatewayStateStore


class GatewayEventSink(Protocol):
    def append(self, event: GatewayEvent) -> Any:
        ...


class GatewayEventPort:
    """Writes canonical redacted gateway events and optionally mirrors them."""

    def __init__(
        self,
        store: GatewayStateStore,
        *,
        sink: GatewayEventSink | None = None,
        redactor: SecretRedactor | None = None,
        known_secrets: Iterable[str] = (),
    ) -> None:
        self.store = store
        self.sink = sink
        self.redactor = redactor or SecretRedactor(known_secrets=known_secrets)

    def emit(
        self,
        record: GatewaySessionRecord,
        kind: GatewayEventKind | str,
        payload: Mapping[str, Any],
        *,
        causation_id: str = "",
        correlation_id: str = "",
        idempotency_key: str = "",
    ) -> GatewayEvent:
        sanitized = self.redactor.redact_value(
            dict(payload),
            source=f"gateway_event.{kind}",
        )
        replay_key = idempotency_key or (
            digest(
                {
                    "kind": str(kind),
                    "causation_id": causation_id,
                    "payload": sanitized.value,
                }
            )
            if causation_id
            else ""
        )
        committed = self.store.append_next_event(
            kind=kind,
            run_id=record.run_id,
            task_id=record.task_id,
            session_id=record.session_id,
            worker_id=record.worker_id,
            payload=dict(sanitized.value),
            causation_id=causation_id,
            correlation_id=correlation_id,
            idempotency_key=replay_key,
        )
        if self.sink is not None:
            self.sink.append(committed)
        return committed

    def emit_recovery(
        self,
        record: GatewaySessionRecord,
        *,
        reason: str,
        error_code: str,
        alternatives: Iterable[str],
        causation_id: str = "",
    ) -> GatewayEvent:
        return self.emit(
            record,
            GatewayEventKind.RECOVERY_INPUT,
            {
                "reason": reason,
                "error_code": error_code,
                "alternatives": list(alternatives),
                "retryable": True,
                "human_intervention_count": 0,
            },
            causation_id=causation_id,
        )

    def list(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[GatewayEvent, ...]:
        return self.store.list_events(session_id, after_sequence=after_sequence)


class MemoryGatewayEventSink:
    def __init__(self) -> None:
        self.events: list[GatewayEvent] = []

    def append(self, event: GatewayEvent) -> None:
        self.events.append(event)
