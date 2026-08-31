from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef, new_id, now_iso, to_jsonable

from .artifacts import LocalArtifactStore
from .query_session import QuerySession, SessionTranscriptEntry, StopReason, snapshot_checkpoint_metadata, transcript_from_snapshot


class ClaudeSessionRecoveryStatus(StrEnum):
    READY = "ready"
    RESTORED = "restored"
    INTERRUPTED = "interrupted"
    INCONSISTENT = "inconsistent"
    MISSING = "missing"
    INVALID = "invalid"


class ClaudeSessionArtifactKind(StrEnum):
    SNAPSHOT = "snapshot"
    TRANSCRIPT = "transcript"
    RESUME_PLAN = "resume_plan"
    INTERRUPTION = "interruption"
    RESTORE_REPORT = "restore_report"


class ClaudeSessionInterruptionKind(StrEnum):
    NONE = "none"
    ACTIVE_TURN = "active_turn"
    STREAMING_MESSAGE = "streaming_message"
    FAILED_TURN = "failed_turn"
    MISSING_LEAF = "missing_leaf"
    INCONSISTENT_CHAIN = "inconsistent_chain"


@dataclass(frozen=True, slots=True)
class ClaudeSessionCheckpoint:
    session_id: str
    resume_token: str
    leaf_uuid: str
    sequence: int
    status: str
    turn_count: int
    message_count: int
    transcript_entry_count: int
    snapshot_artifact_id: str = ""
    transcript_artifact_id: str = ""
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)

    @classmethod
    def from_snapshot(
        cls,
        snapshot: Mapping[str, Any],
        *,
        snapshot_artifact_id: str = "",
        transcript_artifact_id: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> "ClaudeSessionCheckpoint":
        stats = snapshot.get("stats") if isinstance(snapshot.get("stats"), Mapping) else {}
        return cls(
            session_id=str(snapshot.get("session_id") or ""),
            resume_token=str(snapshot.get("resume_token") or ""),
            leaf_uuid=str(snapshot.get("leaf_uuid") or ""),
            sequence=_safe_int(snapshot.get("sequence"), default=0),
            status=str(snapshot.get("status") or ""),
            turn_count=_safe_int(stats.get("turn_count"), default=0),
            message_count=_safe_int(stats.get("message_count"), default=0),
            transcript_entry_count=_safe_int(stats.get("transcript_entry_count"), default=0),
            snapshot_artifact_id=snapshot_artifact_id,
            transcript_artifact_id=transcript_artifact_id,
            metadata={**dict(snapshot.get("metadata") or {}), **dict(metadata or {})},
        )


@dataclass(frozen=True, slots=True)
class ClaudeSessionInterruption:
    kind: ClaudeSessionInterruptionKind
    message: str
    retryable: bool
    turn_id: str = ""
    message_id: str = ""
    resume_token: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def interrupted(self) -> bool:
        return self.kind != ClaudeSessionInterruptionKind.NONE

    def to_dict(self) -> dict[str, Any]:
        return to_jsonable(self)


@dataclass(frozen=True, slots=True)
class ClaudeSessionResumePlan:
    status: ClaudeSessionRecoveryStatus
    session_id: str
    resume_token: str
    leaf_uuid: str
    replay_after_sequence: int
    replay_limit: int
    checkpoint: ClaudeSessionCheckpoint | None = None
    interruption: ClaudeSessionInterruption | None = None
    transcript_tail: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {ClaudeSessionRecoveryStatus.READY, ClaudeSessionRecoveryStatus.RESTORED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "resume_token": self.resume_token,
            "leaf_uuid": self.leaf_uuid,
            "replay_after_sequence": self.replay_after_sequence,
            "replay_limit": self.replay_limit,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "interruption": self.interruption.to_dict() if self.interruption else None,
            "transcript_tail": to_jsonable(self.transcript_tail),
            "warnings": list(self.warnings),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeSessionRestoreReport:
    status: ClaudeSessionRecoveryStatus
    session_id: str
    restored: bool
    checkpoint: ClaudeSessionCheckpoint | None = None
    resume_plan: ClaudeSessionResumePlan | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in {ClaudeSessionRecoveryStatus.READY, ClaudeSessionRecoveryStatus.RESTORED} and not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "ok": self.ok,
            "session_id": self.session_id,
            "restored": self.restored,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "resume_plan": self.resume_plan.to_dict() if self.resume_plan else None,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "metadata": to_jsonable(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ClaudeSessionArtifactSet:
    checkpoint: ClaudeSessionCheckpoint
    snapshot_artifact: ArtifactRef
    transcript_artifact: ArtifactRef
    resume_plan_artifact: ArtifactRef | None = None
    restore_report_artifact: ArtifactRef | None = None

    @property
    def artifacts(self) -> list[ArtifactRef]:
        items = [self.snapshot_artifact, self.transcript_artifact]
        if self.resume_plan_artifact is not None:
            items.append(self.resume_plan_artifact)
        if self.restore_report_artifact is not None:
            items.append(self.restore_report_artifact)
        return items

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint": self.checkpoint.to_dict(),
            "snapshot_artifact": to_jsonable(self.snapshot_artifact),
            "transcript_artifact": to_jsonable(self.transcript_artifact),
            "resume_plan_artifact": to_jsonable(self.resume_plan_artifact) if self.resume_plan_artifact else None,
            "restore_report_artifact": to_jsonable(self.restore_report_artifact) if self.restore_report_artifact else None,
        }


class ClaudeSessionLifecycleRuntime:
    """Session persistence and restore runtime for the productized QueryEngine."""

    def __init__(
        self,
        *,
        artifact_store: LocalArtifactStore,
        runtime_source: str,
        runtime_id: str,
        owner_unit: str = "M1-02A",
    ) -> None:
        self.artifact_store = artifact_store
        self.runtime_source = runtime_source
        self.runtime_id = runtime_id
        self.owner_unit = owner_unit
        self._checkpoints: list[ClaudeSessionCheckpoint] = []
        self._resume_plans: list[ClaudeSessionResumePlan] = []
        self._restore_reports: list[ClaudeSessionRestoreReport] = []

    @property
    def checkpoints(self) -> list[ClaudeSessionCheckpoint]:
        return list(self._checkpoints)

    @property
    def resume_plans(self) -> list[ClaudeSessionResumePlan]:
        return list(self._resume_plans)

    @property
    def restore_reports(self) -> list[ClaudeSessionRestoreReport]:
        return list(self._restore_reports)

    def materialize_session_artifacts(
        self,
        session: QuerySession,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
        metadata: Mapping[str, Any] | None = None,
        include_resume_plan: bool = True,
    ) -> ClaudeSessionArtifactSet:
        snapshot = session.snapshot_payload(
            include_transcript=True,
            metadata={
                "runtime_source": self.runtime_source,
                "runtime_id": self.runtime_id,
                "owner_unit": self.owner_unit,
                **dict(metadata or {}),
            },
        )
        transcript_jsonl = session.to_jsonl()
        snapshot_artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True),
            title=f"CodeWorker query session snapshot {session.session_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
            redact_secrets=True,
        )
        transcript_artifact = self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=transcript_jsonl,
            title=f"CodeWorker query session transcript {session.session_id}",
            kind=ArtifactKind.TRACE,
            extension=".jsonl",
            producer_node_id=producer_node_id,
            redact_secrets=True,
        )
        checkpoint = ClaudeSessionCheckpoint.from_snapshot(
            snapshot,
            snapshot_artifact_id=snapshot_artifact.artifact_id,
            transcript_artifact_id=transcript_artifact.artifact_id,
            metadata={
                "snapshot_uri": snapshot_artifact.uri,
                "transcript_uri": transcript_artifact.uri,
                "runtime_source": self.runtime_source,
            },
        )
        self._checkpoints.append(checkpoint)
        resume_plan_artifact = None
        if include_resume_plan:
            resume_plan = self.build_resume_plan(session, checkpoint=checkpoint)
            resume_plan_artifact = self.write_resume_plan_artifact(
                resume_plan,
                run_id=run_id,
                task_id=task_id,
                producer_node_id=producer_node_id,
            )
        return ClaudeSessionArtifactSet(
            checkpoint=checkpoint,
            snapshot_artifact=snapshot_artifact,
            transcript_artifact=transcript_artifact,
            resume_plan_artifact=resume_plan_artifact,
        )

    def build_resume_plan(
        self,
        session: QuerySession,
        *,
        checkpoint: ClaudeSessionCheckpoint | None = None,
        replay_limit: int = 80,
    ) -> ClaudeSessionResumePlan:
        snapshot = session.snapshot_payload(include_transcript=True)
        checkpoint = checkpoint or ClaudeSessionCheckpoint.from_snapshot(snapshot)
        interruption = self.detect_interruption(snapshot)
        consistency = snapshot.get("consistency") if isinstance(snapshot.get("consistency"), Mapping) else {}
        warnings: list[str] = []
        if consistency and consistency.get("ok") is not True:
            warnings.append("session consistency report is not ok")
        if interruption.interrupted:
            status = ClaudeSessionRecoveryStatus.INTERRUPTED
        elif warnings:
            status = ClaudeSessionRecoveryStatus.INCONSISTENT
        else:
            status = ClaudeSessionRecoveryStatus.READY
        transcript_tail = session.replay(after_sequence=max(0, session.sequence - replay_limit), limit=replay_limit)
        plan = ClaudeSessionResumePlan(
            status=status,
            session_id=session.session_id,
            resume_token=session.resume_token,
            leaf_uuid=session.leaf_uuid or "",
            replay_after_sequence=max(0, session.sequence - replay_limit),
            replay_limit=replay_limit,
            checkpoint=checkpoint,
            interruption=interruption,
            transcript_tail=transcript_tail,
            warnings=warnings,
            metadata={
                "runtime_source": self.runtime_source,
                "runtime_id": self.runtime_id,
                "owner_unit": self.owner_unit,
                "transcript_tail_count": len(transcript_tail),
            },
        )
        self._resume_plans.append(plan)
        return plan

    def detect_interruption(self, snapshot: Mapping[str, Any]) -> ClaudeSessionInterruption:
        turns = snapshot.get("turns") if isinstance(snapshot.get("turns"), list) else []
        messages = snapshot.get("messages") if isinstance(snapshot.get("messages"), list) else []
        leaf_uuid = str(snapshot.get("leaf_uuid") or "")
        resume_token = str(snapshot.get("resume_token") or "")
        if leaf_uuid == "":
            return ClaudeSessionInterruption(
                kind=ClaudeSessionInterruptionKind.MISSING_LEAF,
                message="session has no leaf uuid",
                retryable=False,
                resume_token=resume_token,
            )
        active_turn = next((turn for turn in reversed(turns) if isinstance(turn, Mapping) and not turn.get("completed_at")), None)
        if isinstance(active_turn, Mapping):
            return ClaudeSessionInterruption(
                kind=ClaudeSessionInterruptionKind.ACTIVE_TURN,
                message="session has an active turn without completion timestamp",
                retryable=True,
                turn_id=str(active_turn.get("turn_id") or ""),
                resume_token=resume_token,
                metadata={"phase": str(active_turn.get("phase") or "")},
            )
        streaming = next(
            (
                message
                for message in reversed(messages)
                if isinstance(message, Mapping) and str(message.get("phase") or "").endswith("streaming")
            ),
            None,
        )
        if isinstance(streaming, Mapping):
            return ClaudeSessionInterruption(
                kind=ClaudeSessionInterruptionKind.STREAMING_MESSAGE,
                message="session has a streaming message",
                retryable=True,
                message_id=str(streaming.get("message_id") or ""),
                resume_token=resume_token,
                metadata={"role": str(streaming.get("role") or "")},
            )
        failed_turn = next(
            (
                turn
                for turn in reversed(turns)
                if isinstance(turn, Mapping) and str(turn.get("phase") or "").endswith("failed")
            ),
            None,
        )
        if isinstance(failed_turn, Mapping):
            return ClaudeSessionInterruption(
                kind=ClaudeSessionInterruptionKind.FAILED_TURN,
                message="session ended with a failed turn",
                retryable=True,
                turn_id=str(failed_turn.get("turn_id") or ""),
                resume_token=resume_token,
                metadata={"error": str(failed_turn.get("error") or "")},
            )
        consistency = snapshot.get("consistency") if isinstance(snapshot.get("consistency"), Mapping) else {}
        if consistency and consistency.get("ok") is not True:
            return ClaudeSessionInterruption(
                kind=ClaudeSessionInterruptionKind.INCONSISTENT_CHAIN,
                message="session consistency report failed",
                retryable=False,
                resume_token=resume_token,
                metadata=to_jsonable(consistency),
            )
        return ClaudeSessionInterruption(
            kind=ClaudeSessionInterruptionKind.NONE,
            message="session is resumable",
            retryable=False,
            resume_token=resume_token,
        )

    def restore_from_snapshot_artifact(self, artifact: ArtifactRef) -> ClaudeSessionRestoreReport:
        path = _artifact_path(artifact)
        if path is None or not path.exists():
            report = ClaudeSessionRestoreReport(
                status=ClaudeSessionRecoveryStatus.MISSING,
                session_id="",
                restored=False,
                errors=[f"snapshot artifact missing: {artifact.artifact_id}"],
                metadata={"artifact_id": artifact.artifact_id},
            )
            self._restore_reports.append(report)
            return report
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            session = QuerySession.restore(snapshot)
        except Exception as error:  # noqa: BLE001 - restore failures must be data, not process failure.
            report = ClaudeSessionRestoreReport(
                status=ClaudeSessionRecoveryStatus.INVALID,
                session_id="",
                restored=False,
                errors=[str(error)],
                metadata={"artifact_id": artifact.artifact_id, "path": str(path)},
            )
            self._restore_reports.append(report)
            return report
        checkpoint = ClaudeSessionCheckpoint.from_snapshot(snapshot, snapshot_artifact_id=artifact.artifact_id)
        plan = self.build_resume_plan(session, checkpoint=checkpoint)
        status = ClaudeSessionRecoveryStatus.RESTORED if plan.ok else plan.status
        report = ClaudeSessionRestoreReport(
            status=status,
            session_id=session.session_id,
            restored=True,
            checkpoint=checkpoint,
            resume_plan=plan,
            warnings=list(plan.warnings),
            metadata={"artifact_id": artifact.artifact_id, "path": str(path), "resume_token": session.resume_token},
        )
        self._restore_reports.append(report)
        return report

    def restore_from_transcript_artifact(
        self,
        artifact: ArtifactRef,
        *,
        source_contract: Mapping[str, Any] | None = None,
    ) -> ClaudeSessionRestoreReport:
        path = _artifact_path(artifact)
        if path is None or not path.exists():
            report = ClaudeSessionRestoreReport(
                status=ClaudeSessionRecoveryStatus.MISSING,
                session_id="",
                restored=False,
                errors=[f"transcript artifact missing: {artifact.artifact_id}"],
                metadata={"artifact_id": artifact.artifact_id},
            )
            self._restore_reports.append(report)
            return report
        try:
            session = QuerySession.from_jsonl(path.read_text(encoding="utf-8").splitlines(), source_contract=source_contract)
        except Exception as error:  # noqa: BLE001
            report = ClaudeSessionRestoreReport(
                status=ClaudeSessionRecoveryStatus.INVALID,
                session_id="",
                restored=False,
                errors=[str(error)],
                metadata={"artifact_id": artifact.artifact_id, "path": str(path)},
            )
            self._restore_reports.append(report)
            return report
        snapshot = session.snapshot_payload(include_transcript=True)
        checkpoint = ClaudeSessionCheckpoint.from_snapshot(snapshot, transcript_artifact_id=artifact.artifact_id)
        plan = self.build_resume_plan(session, checkpoint=checkpoint)
        report = ClaudeSessionRestoreReport(
            status=ClaudeSessionRecoveryStatus.RESTORED if plan.ok else plan.status,
            session_id=session.session_id,
            restored=True,
            checkpoint=checkpoint,
            resume_plan=plan,
            warnings=list(plan.warnings),
            metadata={"artifact_id": artifact.artifact_id, "path": str(path), "resume_token": session.resume_token},
        )
        self._restore_reports.append(report)
        return report

    def write_resume_plan_artifact(
        self,
        plan: ClaudeSessionResumePlan,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
    ) -> ArtifactRef:
        return self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            title=f"CodeWorker resume plan {plan.session_id}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
            redact_secrets=True,
        )

    def write_restore_report_artifact(
        self,
        report: ClaudeSessionRestoreReport,
        *,
        run_id: str,
        task_id: str,
        producer_node_id: str | None,
    ) -> ArtifactRef:
        return self.artifact_store.write_text(
            run_id=run_id,
            task_id=task_id,
            content=json.dumps(report.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            title=f"CodeWorker restore report {report.session_id or 'missing'}",
            kind=ArtifactKind.STRUCTURED_DATA,
            extension=".json",
            producer_node_id=producer_node_id,
            redact_secrets=True,
        )

    def checkpoint_metadata(self, checkpoint: ClaudeSessionCheckpoint | None = None) -> dict[str, str]:
        checkpoint = checkpoint or (self._checkpoints[-1] if self._checkpoints else None)
        if checkpoint is None:
            return {
                "query_session_checkpoint_ready": "false",
                "query_session_snapshot_artifact_id": "",
                "query_session_transcript_artifact_id": "",
            }
        return {
            "query_session_checkpoint_ready": "true",
            "query_session_id": checkpoint.session_id,
            "query_session_resume_token": checkpoint.resume_token,
            "query_session_leaf_uuid": checkpoint.leaf_uuid,
            "query_session_sequence": str(checkpoint.sequence),
            "query_session_status": checkpoint.status,
            "query_session_turns": str(checkpoint.turn_count),
            "query_session_messages": str(checkpoint.message_count),
            "query_session_transcript_entries": str(checkpoint.transcript_entry_count),
            "query_session_snapshot_artifact_id": checkpoint.snapshot_artifact_id,
            "query_session_transcript_artifact_id": checkpoint.transcript_artifact_id,
        }

    def metadata(self) -> dict[str, str]:
        latest = self._checkpoints[-1] if self._checkpoints else None
        latest_plan = self._resume_plans[-1] if self._resume_plans else None
        latest_report = self._restore_reports[-1] if self._restore_reports else None
        return {
            **self.checkpoint_metadata(latest),
            "session_lifecycle_checkpoints": str(len(self._checkpoints)),
            "session_lifecycle_resume_plans": str(len(self._resume_plans)),
            "session_lifecycle_restore_reports": str(len(self._restore_reports)),
            "session_lifecycle_latest_resume_status": str(latest_plan.status) if latest_plan else "",
            "session_lifecycle_latest_restore_status": str(latest_report.status) if latest_report else "",
            "session_lifecycle_runtime_source": self.runtime_source,
            "session_lifecycle_runtime_id": self.runtime_id,
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "schema": "zyra.claude.session_lifecycle.v1",
            "runtime_source": self.runtime_source,
            "runtime_id": self.runtime_id,
            "owner_unit": self.owner_unit,
            "checkpoints": [item.to_dict() for item in self._checkpoints],
            "resume_plans": [item.to_dict() for item in self._resume_plans],
            "restore_reports": [item.to_dict() for item in self._restore_reports],
        }


def resume_plan_from_snapshot(snapshot: Mapping[str, Any], *, replay_limit: int = 80) -> ClaudeSessionResumePlan:
    try:
        session = QuerySession.restore(snapshot)
    except Exception:
        checkpoint = ClaudeSessionCheckpoint.from_snapshot(snapshot)
        return ClaudeSessionResumePlan(
            status=ClaudeSessionRecoveryStatus.INVALID,
            session_id=checkpoint.session_id,
            resume_token=checkpoint.resume_token,
            leaf_uuid=checkpoint.leaf_uuid,
            replay_after_sequence=0,
            replay_limit=replay_limit,
            checkpoint=checkpoint,
            warnings=["snapshot could not be restored"],
        )
    runtime = ClaudeSessionLifecycleRuntime(
        artifact_store=LocalArtifactStore(Path.cwd() / ".zyra-session-lifecycle"),
        runtime_source=str(snapshot.get("metadata", {}).get("runtime_source") or "zyra-claude-productized"),
        runtime_id=str(snapshot.get("metadata", {}).get("runtime_id") or "zyra-claude-code-productized-runtime"),
    )
    return runtime.build_resume_plan(session, replay_limit=replay_limit)


def transcript_tail_from_snapshot(snapshot: Mapping[str, Any], *, after_sequence: int = 0, limit: int = 80) -> list[dict[str, Any]]:
    entries = transcript_from_snapshot(snapshot)
    selected = [entry.to_dict() for entry in entries if entry.sequence > after_sequence]
    return selected[: max(0, limit)]


def session_artifact_metadata(artifact_set: ClaudeSessionArtifactSet) -> dict[str, str]:
    checkpoint = artifact_set.checkpoint
    return {
        "query_session_checkpoint_ready": "true",
        "query_session_id": checkpoint.session_id,
        "query_session_resume_token": checkpoint.resume_token,
        "query_session_leaf_uuid": checkpoint.leaf_uuid,
        "query_session_snapshot_artifact_id": artifact_set.snapshot_artifact.artifact_id,
        "query_session_transcript_artifact_id": artifact_set.transcript_artifact.artifact_id,
        "query_session_resume_plan_artifact_id": artifact_set.resume_plan_artifact.artifact_id if artifact_set.resume_plan_artifact else "",
    }


def checkpoint_from_artifact_payload(payload: Mapping[str, Any]) -> ClaudeSessionCheckpoint:
    if "checkpoint" in payload and isinstance(payload.get("checkpoint"), Mapping):
        payload = payload["checkpoint"]
    return ClaudeSessionCheckpoint(
        session_id=str(payload.get("session_id") or ""),
        resume_token=str(payload.get("resume_token") or ""),
        leaf_uuid=str(payload.get("leaf_uuid") or ""),
        sequence=_safe_int(payload.get("sequence"), default=0),
        status=str(payload.get("status") or ""),
        turn_count=_safe_int(payload.get("turn_count"), default=0),
        message_count=_safe_int(payload.get("message_count"), default=0),
        transcript_entry_count=_safe_int(payload.get("transcript_entry_count"), default=0),
        snapshot_artifact_id=str(payload.get("snapshot_artifact_id") or ""),
        transcript_artifact_id=str(payload.get("transcript_artifact_id") or ""),
        created_at=str(payload.get("created_at") or now_iso()),
        metadata=dict(payload.get("metadata") or {}),
    )


def validate_transcript_chain(entries: Sequence[SessionTranscriptEntry] | Sequence[Mapping[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    previous_sequence = 0
    for raw in entries:
        entry = raw.to_dict() if hasattr(raw, "to_dict") else dict(raw)
        uuid = str(entry.get("uuid") or "")
        parent_uuid = str(entry.get("parent_uuid") or "")
        sequence = _safe_int(entry.get("sequence"), default=0)
        if not uuid:
            errors.append("transcript entry missing uuid")
        if sequence <= previous_sequence:
            errors.append(f"non-increasing transcript sequence at {uuid or sequence}")
        if parent_uuid and parent_uuid not in seen:
            errors.append(f"parent uuid {parent_uuid} missing before {uuid}")
        seen.add(uuid)
        previous_sequence = sequence
    return errors


def _artifact_path(artifact: ArtifactRef) -> Path | None:
    if artifact.uri:
        return Path(artifact.uri)
    relative = artifact.metadata.get("relative_path")
    if isinstance(relative, str):
        return Path(relative)
    return None


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
