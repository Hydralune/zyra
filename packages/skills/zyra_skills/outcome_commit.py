from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import RLock
from typing import Any, Iterable, Mapping, Protocol, Sequence
from urllib.parse import urlparse

from .digests import digest_object
from .integration_errors import (
    SkillOutcomeConflict,
    SkillOutcomeNotTerminal,
    SkillOutcomeReferenceRejected,
    SkillSessionOwnershipError,
)
from .models import InvokedSkillState, SkillInvocationStatus, SkillOutcomeProjection, utc_now


class SkillOutcomeRefKind(StrEnum):
    EVENT = "event"
    ARTIFACT = "artifact"
    EVIDENCE = "evidence"
    OUTCOME = "outcome"
    WORKER = "worker"
    CHILD_TASK = "child_task"


@dataclass(frozen=True, slots=True)
class SkillOutcomeReference:
    uri: str
    kind: SkillOutcomeRefKind
    run_id: str
    task_id: str
    session_id: str
    invocation_id: str
    source_event_id: str = ""
    source_artifact_id: str = ""
    content_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.uri.strip():
            raise ValueError("skill outcome reference requires uri")
        if not self.run_id or not self.task_id or not self.invocation_id:
            raise ValueError("skill outcome reference requires run/task/invocation identity")
        scheme = urlparse(self.uri).scheme
        allowed = {
            "event",
            "artifact",
            "evidence",
            "worker",
            "subagent",
            "skill-outcome",
        }
        if scheme not in allowed:
            raise SkillOutcomeReferenceRejected(
                "skill outcome reference uses an unsupported URI scheme",
                detail={"uri": self.uri},
            )

    @property
    def immutable_ref(self) -> str:
        return self.uri

    def to_dict(self) -> dict[str, Any]:
        return {
            "uri": self.uri,
            "kind": str(self.kind),
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "invocation_id": self.invocation_id,
            "source_event_id": self.source_event_id,
            "source_artifact_id": self.source_artifact_id,
            "content_digest": self.content_digest,
            "metadata": copy.deepcopy(self.metadata),
        }


class SkillOutcomeEvidencePort(Protocol):
    def event(self, event_id: str) -> Mapping[str, Any] | None: ...

    def artifact(self, artifact_id: str) -> Mapping[str, Any] | None: ...

    def events_for_task(self, run_id: str, task_id: str) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class SkillOutcomeCommitRequest:
    commit_id: str
    invocation_id: str
    run_id: str
    task_id: str
    session_id: str
    worker_request_id: str = ""
    child_task_id: str = ""
    event_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    explicit_refs: tuple[str, ...] = ()
    expected_state_revision: int = -1
    requested_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.commit_id or not self.invocation_id:
            raise ValueError("skill outcome commit requires commit_id and invocation_id")
        if not self.run_id or not self.task_id or not self.session_id:
            raise ValueError("skill outcome commit requires run/task/session identity")
        if not self.event_ids and not self.artifact_ids and not self.explicit_refs:
            raise ValueError("skill outcome commit requires causal evidence")

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_id": self.commit_id,
            "invocation_id": self.invocation_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "worker_request_id": self.worker_request_id,
            "child_task_id": self.child_task_id,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "explicit_refs": list(self.explicit_refs),
            "expected_state_revision": self.expected_state_revision,
            "requested_at": self.requested_at,
        }


@dataclass(frozen=True, slots=True)
class SkillOutcomeCommit:
    commit_id: str
    invocation_id: str
    version_ref: str
    policy_snapshot_digest: str
    status: str
    references: tuple[SkillOutcomeReference, ...]
    outcome_projection: SkillOutcomeProjection
    commit_digest: str
    committed_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_id": self.commit_id,
            "invocation_id": self.invocation_id,
            "version_ref": self.version_ref,
            "policy_snapshot_digest": self.policy_snapshot_digest,
            "status": self.status,
            "references": [item.to_dict() for item in self.references],
            "outcome_projection": self.outcome_projection.to_dict(),
            "commit_digest": self.commit_digest,
            "committed_at": self.committed_at,
            "body_persisted": False,
            "authority_persisted": False,
        }


class SkillOutcomeCommitRuntime:
    """Validates terminal outcome/evidence against canonical stores.

    Client supplied URIs are accepted only when they name an event/artifact
    discovered through the Zyra-owned evidence port and carry matching
    run/task/session/invocation causality.
    """

    def __init__(
        self,
        *,
        skill_runtime: Any,
        evidence_port: SkillOutcomeEvidencePort,
    ) -> None:
        self.skill_runtime = skill_runtime
        self.evidence_port = evidence_port
        self._lock = RLock()
        self._commits: dict[str, SkillOutcomeCommit] = {}
        self._by_invocation: dict[str, str] = {}

    def commit(self, request: SkillOutcomeCommitRequest) -> SkillOutcomeCommit:
        with self._lock:
            existing = self._commits.get(request.commit_id)
            if existing is not None:
                return existing
            prior_id = self._by_invocation.get(request.invocation_id)
            if prior_id:
                raise SkillOutcomeConflict(
                    "skill invocation already has an outcome commit",
                    detail={"commit_id": prior_id},
                )
        state = self.skill_runtime.state_store.get(request.invocation_id)
        self._validate_identity(request, state)
        if request.expected_state_revision >= 0 and state.revision != request.expected_state_revision:
            raise SkillOutcomeConflict(
                "skill invocation changed before outcome commit",
                detail={"expected": request.expected_state_revision, "actual": state.revision},
            )
        references = self._resolve_references(request)
        if not references:
            raise SkillOutcomeReferenceRejected("skill outcome commit resolved no evidence")
        terminal = state
        if not terminal.status.terminal:
            terminal = self.skill_runtime.invocation_runtime.complete(
                state.invocation_id,
                outcome_refs=tuple(
                    item.uri for item in references if item.kind in {SkillOutcomeRefKind.OUTCOME, SkillOutcomeRefKind.WORKER, SkillOutcomeRefKind.CHILD_TASK}
                ),
                evidence_refs=tuple(
                    item.uri for item in references if item.kind in {SkillOutcomeRefKind.EVENT, SkillOutcomeRefKind.EVIDENCE}
                ),
                artifact_refs=tuple(
                    item.uri for item in references if item.kind is SkillOutcomeRefKind.ARTIFACT
                ),
            )
        if terminal.status is not SkillInvocationStatus.COMPLETED:
            raise SkillOutcomeNotTerminal(
                "skill outcome can be committed only for a completed invocation",
                detail={"status": str(terminal.status)},
            )
        projection = self.skill_runtime.compact_bridge.memory_projection(terminal)
        payload = {
            "request": request.to_dict(),
            "version_ref": terminal.version_ref.immutable_ref,
            "policy_snapshot_digest": terminal.policy_snapshot.policy_digest
            if terminal.policy_snapshot
            else "",
            "references": [item.to_dict() for item in references],
            "projection": projection.to_dict(),
        }
        value = SkillOutcomeCommit(
            commit_id=request.commit_id,
            invocation_id=request.invocation_id,
            version_ref=terminal.version_ref.immutable_ref,
            policy_snapshot_digest=terminal.policy_snapshot.policy_digest
            if terminal.policy_snapshot
            else "",
            status=str(terminal.status),
            references=references,
            outcome_projection=projection,
            commit_digest=digest_object(payload),
        )
        with self._lock:
            if request.commit_id in self._commits or request.invocation_id in self._by_invocation:
                raise SkillOutcomeConflict("skill outcome commit raced with another writer")
            self._commits[request.commit_id] = value
            self._by_invocation[request.invocation_id] = request.commit_id
        return value

    def get(self, commit_id: str) -> SkillOutcomeCommit:
        with self._lock:
            value = self._commits.get(str(commit_id))
        if value is None:
            raise SkillOutcomeConflict("skill outcome commit was not found")
        return value

    def for_invocation(self, invocation_id: str) -> SkillOutcomeCommit | None:
        with self._lock:
            commit_id = self._by_invocation.get(str(invocation_id))
            return self._commits.get(commit_id) if commit_id else None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            values = {key: value.to_dict() for key, value in self._commits.items()}
        return {
            "owner": "M1-03C SkillOutcomeCommitRuntime",
            "commits": values,
            "snapshot_digest": digest_object(values),
            "body_persisted": False,
        }

    def _validate_identity(
        self,
        request: SkillOutcomeCommitRequest,
        state: InvokedSkillState,
    ) -> None:
        expected = (state.run_id, state.task_id, state.session_id, state.invocation_id)
        actual = (request.run_id, request.task_id, request.session_id, request.invocation_id)
        if actual != expected:
            raise SkillSessionOwnershipError(
                "skill outcome request belongs to another invocation scope",
                detail={"expected": expected, "actual": actual},
            )

    def _resolve_references(
        self,
        request: SkillOutcomeCommitRequest,
    ) -> tuple[SkillOutcomeReference, ...]:
        values: list[SkillOutcomeReference] = []
        seen: set[str] = set()
        for event_id in request.event_ids:
            event = self.evidence_port.event(event_id)
            if event is None:
                raise SkillOutcomeReferenceRejected(
                    "skill outcome event does not exist",
                    detail={"event_id": event_id},
                )
            value = self._reference_from_event(request, event_id, event)
            if value.uri not in seen:
                seen.add(value.uri)
                values.append(value)
        for artifact_id in request.artifact_ids:
            artifact = self.evidence_port.artifact(artifact_id)
            if artifact is None:
                raise SkillOutcomeReferenceRejected(
                    "skill outcome artifact does not exist",
                    detail={"artifact_id": artifact_id},
                )
            value = self._reference_from_artifact(request, artifact_id, artifact)
            if value.uri not in seen:
                seen.add(value.uri)
                values.append(value)
        for uri in request.explicit_refs:
            value = self._reference_from_explicit(request, uri)
            if value.uri not in seen:
                seen.add(value.uri)
                values.append(value)
        return tuple(values)

    def _reference_from_event(
        self,
        request: SkillOutcomeCommitRequest,
        event_id: str,
        event: Mapping[str, Any],
    ) -> SkillOutcomeReference:
        self._validate_record_scope(request, event, label=f"event:{event_id}")
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        skill_runtime = (
            payload.get("skill_runtime")
            if isinstance(payload.get("skill_runtime"), Mapping)
            else {}
        )
        skill_invocation = (
            payload.get("skill_invocation")
            if isinstance(payload.get("skill_invocation"), Mapping)
            else {}
        )
        invocation_id = str(
            payload.get("invocation_id")
            or skill_runtime.get("invocation_id")
            or skill_invocation.get("invocation_id")
            or event.get("invocation_id")
            or ""
        )
        if invocation_id and invocation_id != request.invocation_id:
            raise SkillOutcomeReferenceRejected(
                "skill outcome event belongs to another invocation",
                detail={"event_id": event_id, "invocation_id": invocation_id},
            )
        return SkillOutcomeReference(
            uri=f"event://{request.run_id}/{request.task_id}/{event_id}",
            kind=SkillOutcomeRefKind.EVENT,
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            source_event_id=event_id,
            content_digest=digest_object(event),
            metadata={"event_type": str(event.get("event_type") or event.get("type") or "")},
        )

    def _reference_from_artifact(
        self,
        request: SkillOutcomeCommitRequest,
        artifact_id: str,
        artifact: Mapping[str, Any],
    ) -> SkillOutcomeReference:
        self._validate_record_scope(request, artifact, label=f"artifact:{artifact_id}")
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), Mapping) else {}
        invocation_id = str(metadata.get("skill_invocation_id") or metadata.get("invocation_id") or "")
        if invocation_id and invocation_id != request.invocation_id:
            raise SkillOutcomeReferenceRejected(
                "skill outcome artifact belongs to another invocation",
                detail={"artifact_id": artifact_id, "invocation_id": invocation_id},
            )
        return SkillOutcomeReference(
            uri=f"artifact://{request.run_id}/{request.task_id}/{artifact_id}",
            kind=SkillOutcomeRefKind.ARTIFACT,
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            source_artifact_id=artifact_id,
            content_digest=str(artifact.get("digest") or digest_object(artifact)),
            metadata={"artifact_kind": str(artifact.get("kind") or "")},
        )

    def _reference_from_explicit(
        self,
        request: SkillOutcomeCommitRequest,
        uri: str,
    ) -> SkillOutcomeReference:
        parsed = urlparse(uri)
        parts = tuple(item for item in parsed.path.split("/") if item)
        if parsed.scheme == "event" and parts:
            event_id = parts[-1]
            event = self.evidence_port.event(event_id)
            if event is None:
                raise SkillOutcomeReferenceRejected("explicit event reference does not exist")
            return self._reference_from_event(request, event_id, event)
        if parsed.scheme == "artifact" and parts:
            artifact_id = parts[-1]
            artifact = self.evidence_port.artifact(artifact_id)
            if artifact is None:
                raise SkillOutcomeReferenceRejected("explicit artifact reference does not exist")
            return self._reference_from_artifact(request, artifact_id, artifact)
        events = self.evidence_port.events_for_task(request.run_id, request.task_id)
        match = next(
            (
                event
                for event in events
                if str(event.get("worker_request_id") or event.get("child_task_id") or "")
                in {request.worker_request_id, request.child_task_id}
                and str(event.get("uri") or "") == uri
            ),
            None,
        )
        if match is None:
            raise SkillOutcomeReferenceRejected(
                "explicit skill outcome reference has no canonical causal record",
                detail={"uri": uri},
            )
        kind = (
            SkillOutcomeRefKind.CHILD_TASK
            if parsed.scheme == "subagent"
            else SkillOutcomeRefKind.WORKER
        )
        return SkillOutcomeReference(
            uri=uri,
            kind=kind,
            run_id=request.run_id,
            task_id=request.task_id,
            session_id=request.session_id,
            invocation_id=request.invocation_id,
            source_event_id=str(match.get("event_id") or match.get("id") or ""),
            content_digest=digest_object(match),
        )

    def _validate_record_scope(
        self,
        request: SkillOutcomeCommitRequest,
        record: Mapping[str, Any],
        *,
        label: str,
    ) -> None:
        run_id = str(record.get("run_id") or "")
        task_id = str(record.get("task_id") or "")
        if run_id != request.run_id or task_id != request.task_id:
            raise SkillOutcomeReferenceRejected(
                "skill outcome record belongs to another task",
                detail={
                    "label": label,
                    "expected_run_id": request.run_id,
                    "actual_run_id": run_id,
                    "expected_task_id": request.task_id,
                    "actual_task_id": task_id,
                },
            )


class InMemorySkillOutcomeEvidencePort:
    """Deterministic port used by local runtime composition and tests."""

    def __init__(
        self,
        *,
        events: Iterable[Mapping[str, Any]] = (),
        artifacts: Iterable[Mapping[str, Any]] = (),
    ) -> None:
        self._events = {
            str(item.get("event_id") or item.get("id")): copy.deepcopy(dict(item))
            for item in events
        }
        self._artifacts = {
            str(item.get("artifact_id") or item.get("id")): copy.deepcopy(dict(item))
            for item in artifacts
        }

    def event(self, event_id: str) -> Mapping[str, Any] | None:
        value = self._events.get(str(event_id))
        return copy.deepcopy(value) if value is not None else None

    def artifact(self, artifact_id: str) -> Mapping[str, Any] | None:
        value = self._artifacts.get(str(artifact_id))
        return copy.deepcopy(value) if value is not None else None

    def events_for_task(self, run_id: str, task_id: str) -> Sequence[Mapping[str, Any]]:
        return tuple(
            copy.deepcopy(value)
            for value in self._events.values()
            if value.get("run_id") == run_id and value.get("task_id") == task_id
        )
