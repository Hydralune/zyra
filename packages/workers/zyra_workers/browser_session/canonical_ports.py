from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from zyra_core import ArtifactRef, EventRecord


@runtime_checkable
class CanonicalArtifactProjectionPort(Protocol):
    """Projects an already-owned artifact reference into canonical task state."""

    def project_artifact(self, artifact: ArtifactRef) -> None: ...


@runtime_checkable
class CanonicalEventProjectionPort(Protocol):
    """Projects an already-created event into the canonical event owner."""

    def project_event(self, event: EventRecord) -> None: ...


@dataclass(frozen=True, slots=True)
class CanonicalProjectionFailure:
    surface: str
    ref_id: str
    error_type: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {
            "surface": self.surface,
            "ref_id": self.ref_id,
            "error_type": self.error_type,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class CanonicalProjectionResult:
    artifact_refs: tuple[ArtifactRef, ...] = ()
    event_refs: tuple[EventRecord, ...] = ()
    projected_artifact_ids: tuple[str, ...] = ()
    projected_event_ids: tuple[str, ...] = ()
    failures: tuple[CanonicalProjectionFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def attempted(self) -> int:
        return len(self.artifact_refs) + len(self.event_refs)

    @property
    def projected(self) -> int:
        return len(self.projected_artifact_ids) + len(self.projected_event_ids)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "attempted": self.attempted,
            "projected": self.projected,
            "artifact_ids": [item.artifact_id for item in self.artifact_refs],
            "event_ids": [item.event_id for item in self.event_refs],
            "projected_artifact_ids": list(self.projected_artifact_ids),
            "projected_event_ids": list(self.projected_event_ids),
            "failures": [item.to_dict() for item in self.failures],
        }


@dataclass(slots=True)
class BrowserCanonicalPorts:
    """Stateless handoff to existing artifact and event owners.

    The object intentionally has no ref index, append log, checkpoint or replay
    state.  It only forwards references produced by the browser application.
    Deduplication is local to one projection call.
    """

    artifact_projector: CanonicalArtifactProjectionPort | Callable[[ArtifactRef], Any] | None = None
    event_projector: CanonicalEventProjectionPort | Callable[[EventRecord], Any] | None = None
    strict: bool = True
    disabled: bool = False
    metadata: dict[str, str] = field(default_factory=dict)

    def project_artifact(self, artifact: ArtifactRef) -> bool:
        if self.disabled:
            raise RuntimeError("browser canonical projection ports are disabled")
        if not isinstance(artifact, ArtifactRef):
            raise TypeError("canonical artifact projection accepts ArtifactRef only")
        if self.artifact_projector is None:
            return False
        self._invoke(
            self.artifact_projector,
            artifact,
            preferred=("project_artifact", "append_artifact", "add_artifact"),
        )
        return True

    def project_event(self, event: EventRecord) -> bool:
        if self.disabled:
            raise RuntimeError("browser canonical projection ports are disabled")
        if not isinstance(event, EventRecord):
            raise TypeError("canonical event projection accepts EventRecord only")
        if self.event_projector is None:
            return False
        self._invoke(
            self.event_projector,
            event,
            preferred=("project_event", "append_event", "persist_event"),
        )
        return True

    def project(
        self,
        *,
        artifacts: Iterable[ArtifactRef] = (),
        events: Iterable[EventRecord] = (),
    ) -> CanonicalProjectionResult:
        artifact_refs = self._unique_artifacts(artifacts)
        event_refs = self._unique_events(events)
        projected_artifacts: list[str] = []
        projected_events: list[str] = []
        failures: list[CanonicalProjectionFailure] = []

        for artifact in artifact_refs:
            try:
                if self.project_artifact(artifact):
                    projected_artifacts.append(artifact.artifact_id)
            except Exception as error:
                failure = CanonicalProjectionFailure(
                    surface="artifact",
                    ref_id=artifact.artifact_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                failures.append(failure)
                if self.strict:
                    raise RuntimeError(
                        f"canonical artifact projection failed for {artifact.artifact_id}: {error}"
                    ) from error

        for event in event_refs:
            try:
                if self.project_event(event):
                    projected_events.append(event.event_id)
            except Exception as error:
                failure = CanonicalProjectionFailure(
                    surface="event",
                    ref_id=event.event_id,
                    error_type=type(error).__name__,
                    message=str(error),
                )
                failures.append(failure)
                if self.strict:
                    raise RuntimeError(
                        f"canonical event projection failed for {event.event_id}: {error}"
                    ) from error

        return CanonicalProjectionResult(
            artifact_refs=artifact_refs,
            event_refs=event_refs,
            projected_artifact_ids=tuple(projected_artifacts),
            projected_event_ids=tuple(projected_events),
            failures=tuple(failures),
        )

    def describe(self) -> dict[str, Any]:
        return {
            "runtime_id": "zyra-browser-canonical-reference-ports",
            "artifact_projector": type(self.artifact_projector).__name__ if self.artifact_projector else "unbound",
            "event_projector": type(self.event_projector).__name__ if self.event_projector else "unbound",
            "strict": self.strict,
            "disabled": self.disabled,
            "owns_facts": False,
            "retains_refs": False,
            "metadata": dict(self.metadata),
        }

    @staticmethod
    def _invoke(projector: Any, value: Any, *, preferred: tuple[str, ...]) -> Any:
        if callable(projector):
            return projector(value)
        for name in preferred:
            method = getattr(projector, name, None)
            if callable(method):
                return method(value)
        raise TypeError(f"canonical projector {type(projector).__name__} has no supported projection method")

    @staticmethod
    def _unique_artifacts(values: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
        unique: dict[str, ArtifactRef] = {}
        for value in values:
            if not isinstance(value, ArtifactRef):
                raise TypeError("canonical artifact projection accepts ArtifactRef only")
            unique.setdefault(value.artifact_id, value)
        return tuple(unique.values())

    @staticmethod
    def _unique_events(values: Iterable[EventRecord]) -> tuple[EventRecord, ...]:
        unique: dict[str, EventRecord] = {}
        for value in values:
            if not isinstance(value, EventRecord):
                raise TypeError("canonical event projection accepts EventRecord only")
            unique.setdefault(value.event_id, value)
        return tuple(unique.values())
