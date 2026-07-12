from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from zyra_core import ArtifactKind, ArtifactRef
from zyra_runtime import LocalArtifactStore

from .digests import digest_object
from .errors import StructuredHandoffRequired
from .models import (
    RecoverySignal,
    StructuredHandoff,
    StructuredSubagentMessage,
    SubagentExecutionResult,
    SubagentTaskRecord,
)


@dataclass(frozen=True, slots=True)
class HandoffPolicy:
    maximum_summary_chars: int = 8_000
    maximum_state_delta_chars: int = 16_000
    maximum_artifact_refs: int = 128
    maximum_evidence_refs: int = 128
    offload_large_summary: bool = True
    allowed_state_keys: tuple[str, ...] = (
        "completed",
        "failed",
        "outputs",
        "changed_paths",
        "verification",
        "open_questions",
        "next_actions",
        "constraints_observed",
    )


class SubagentHandoffRuntime:
    """Convert execution results to bounded low-entropy parent handoffs."""

    def __init__(self, artifact_store: LocalArtifactStore, policy: HandoffPolicy | None = None, *, disabled: bool = False) -> None:
        self.artifact_store = artifact_store
        self.policy = policy or HandoffPolicy()
        self.disabled = disabled

    def success(
        self,
        record: SubagentTaskRecord,
        result: SubagentExecutionResult,
        *,
        transcript_ref: str,
        state_delta: Mapping[str, Any] | None = None,
        evidence_refs: Sequence[str] = (),
    ) -> StructuredHandoff:
        if self.disabled:
            raise StructuredHandoffRequired(record.task_id)
        summary, extra_artifacts = self._bounded_summary(record, result.summary)
        artifacts = self._validate_artifacts((*result.artifacts, *extra_artifacts))
        delta = self._bounded_state_delta(state_delta or {
            "completed": result.ok,
            "failed": not result.ok,
            "outputs": [artifact.artifact_id for artifact in artifacts],
        })
        artifact_ids = tuple(item.artifact_id for item in artifacts)
        message = StructuredSubagentMessage(
            sender_task_id=record.task_id,
            target_task_id=record.parent_task_id,
            intent="subagent_result",
            summary=summary,
            state_delta=delta,
            evidence_refs=tuple(evidence_refs),
            artifact_refs=artifact_ids,
            uncertainty=_float_or_none(result.metadata.get("uncertainty")),
            metadata={
                "execution_ref": result.execution_ref,
                "agent_type": record.agent_type,
                "raw_transcript_included": False,
                "tool_dump_included": False,
            },
        )
        return StructuredHandoff(
            task_id=record.task_id,
            parent_task_id=record.parent_task_id,
            summary=summary,
            message=message,
            artifacts=artifacts,
            usage=result.usage,
            transcript_ref=transcript_ref,
            metadata={
                "result_digest": digest_object(result.safe_dict()),
                "structured_only": True,
                "event_count": len(result.events),
            },
        )

    def failure(
        self,
        record: SubagentTaskRecord,
        result: SubagentExecutionResult,
        recovery_signal: RecoverySignal,
        *,
        transcript_ref: str,
    ) -> StructuredHandoff:
        summary, extra_artifacts = self._bounded_summary(record, result.summary or result.error_message)
        artifacts = self._validate_artifacts((*result.artifacts, *extra_artifacts))
        message = StructuredSubagentMessage(
            sender_task_id=record.task_id,
            target_task_id=record.parent_task_id,
            intent="subagent_failure",
            summary=summary,
            state_delta={
                "completed": False,
                "failed": True,
                "next_actions": [recovery_signal.disposition.value],
            },
            artifact_refs=tuple(item.artifact_id for item in artifacts),
            metadata={
                "error_code": result.error_code,
                "recovery_signal_id": recovery_signal.signal_id,
                "raw_transcript_included": False,
            },
        )
        return StructuredHandoff(
            task_id=record.task_id,
            parent_task_id=record.parent_task_id,
            summary=summary,
            message=message,
            artifacts=artifacts,
            usage=result.usage,
            recovery_signal=recovery_signal,
            transcript_ref=transcript_ref,
            metadata={
                "result_digest": digest_object(result.safe_dict()),
                "structured_only": True,
            },
        )

    def validate(self, handoff: StructuredHandoff) -> None:
        if not handoff.summary.strip():
            raise StructuredHandoffRequired(handoff.task_id)
        if len(handoff.summary) > self.policy.maximum_summary_chars:
            raise StructuredHandoffRequired(handoff.task_id)
        if len(handoff.artifacts) > self.policy.maximum_artifact_refs:
            raise StructuredHandoffRequired(handoff.task_id)
        if len(handoff.message.evidence_refs) > self.policy.maximum_evidence_refs:
            raise StructuredHandoffRequired(handoff.task_id)
        self._bounded_state_delta(handoff.message.state_delta)
        if handoff.message.sender_task_id != handoff.task_id:
            raise StructuredHandoffRequired(handoff.task_id)
        if handoff.message.target_task_id != handoff.parent_task_id:
            raise StructuredHandoffRequired(handoff.task_id)

    def _bounded_summary(self, record: SubagentTaskRecord, summary: str) -> tuple[str, tuple[ArtifactRef, ...]]:
        text = str(summary or "").strip()
        if not text:
            text = "Subagent completed without a textual summary. See structured artifacts and events."
        if len(text) <= self.policy.maximum_summary_chars:
            return text, ()
        if not self.policy.offload_large_summary:
            return text[: self.policy.maximum_summary_chars], ()
        artifact = self.artifact_store.write_text(
            run_id=record.run_id,
            task_id=record.parent_task_id,
            content=text,
            title=f"Subagent {record.task_id} full result",
            kind=ArtifactKind.REPORT,
            extension=".md",
            producer_node_id=None,
        )
        bounded = text[: self.policy.maximum_summary_chars - 256].rstrip()
        bounded += f"\n\nFull output was offloaded to artifact `{artifact.artifact_id}`."
        return bounded, (artifact,)

    def _bounded_state_delta(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        allowed = set(self.policy.allowed_state_keys)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ValueError(f"subagent handoff contains forbidden state keys: {', '.join(unknown)}")
        payload = {str(key): value for key, value in raw.items()}
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        if len(encoded) > self.policy.maximum_state_delta_chars:
            raise ValueError("subagent handoff state delta exceeds budget")
        return payload

    def _validate_artifacts(self, artifacts: Iterable[ArtifactRef]) -> tuple[ArtifactRef, ...]:
        selected: list[ArtifactRef] = []
        seen: set[str] = set()
        for artifact in artifacts:
            if artifact.artifact_id in seen:
                continue
            if not artifact.artifact_id or not artifact.uri:
                raise ValueError("subagent handoff contains an invalid artifact ref")
            selected.append(artifact)
            seen.add(artifact.artifact_id)
        if len(selected) > self.policy.maximum_artifact_refs:
            raise ValueError("subagent handoff artifact ref budget exceeded")
        return tuple(selected)


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

