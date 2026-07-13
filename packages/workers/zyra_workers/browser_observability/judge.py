from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .models import (
    HistoryKind,
    HistoryRecord,
    ObservationScope,
    ReplayProjection,
    digest_value,
    new_observation_id,
    utc_now,
)


class JudgeVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True, slots=True)
class JudgePolicy:
    max_trace_records: int = 500
    max_evidence_chars: int = 80_000
    allow_model_advisory: bool = False
    deterministic_fail_on_replay_issue: bool = True
    authority: str = "advisory_only"

    def __post_init__(self) -> None:
        if self.max_trace_records < 1:
            raise ValueError("judge trace record limit must be positive")
        if self.max_evidence_chars < 1_000:
            raise ValueError("judge evidence budget is too small")
        if self.authority != "advisory_only":
            raise ValueError("04D judge must remain advisory_only")


@dataclass(frozen=True, slots=True)
class JudgeAdvisory:
    scope: ObservationScope
    verdict: JudgeVerdict
    summary: str
    advisory_id: str = field(default_factory=lambda: new_observation_id("browser-judge"))
    created_at: str = field(default_factory=utc_now)
    reasoning: str = ""
    failure_reasons: tuple[str, ...] = ()
    evidence_record_ids: tuple[str, ...] = ()
    evidence_artifact_ids: tuple[str, ...] = ()
    source: str = "deterministic"
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def authoritative(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-observability.judge-advisory.v1",
            "advisory_id": self.advisory_id,
            "scope": self.scope.to_dict(),
            "verdict": str(self.verdict),
            "summary": self.summary,
            "created_at": self.created_at,
            "reasoning": self.reasoning,
            "failure_reasons": list(self.failure_reasons),
            "evidence_record_ids": list(self.evidence_record_ids),
            "evidence_artifact_ids": list(self.evidence_artifact_ids),
            "source": self.source,
            "metadata": dict(self.metadata),
            "authoritative": False,
            "may_plan_recovery": False,
            "may_override_permission": False,
            "may_override_worker_result": False,
        }


class JudgeModelPort(Protocol):
    def evaluate(
        self,
        evidence: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        ...


class BrowserTraceJudge:
    """Advisory trace evaluation; deterministic owners keep all authority."""

    def __init__(
        self,
        *,
        policy: JudgePolicy | None = None,
        model: JudgeModelPort | None = None,
    ) -> None:
        self.policy = policy or JudgePolicy()
        self.model = model

    def evaluate(
        self,
        replay: ReplayProjection,
        *,
        worker_ok: bool,
        worker_error: str = "",
        success_criteria: Sequence[str] = (),
    ) -> JudgeAdvisory:
        deterministic_reasons: list[str] = []
        if not replay.complete:
            deterministic_reasons.extend(
                item.summary
                for item in replay.issues
                if item.fatal
            )
        failed_pairs = [item for item in replay.tool_pairs if not item.ok]
        if failed_pairs:
            deterministic_reasons.append(
                f"{len(failed_pairs)} browser tool call(s) failed"
            )
        terminal_signals = [item for item in replay.signals if item.terminal]
        if terminal_signals:
            deterministic_reasons.append(
                f"{len(terminal_signals)} terminal watchdog signal(s) were emitted"
            )
        if not worker_ok:
            deterministic_reasons.append(
                worker_error or "browser worker reported failure"
            )
        evidence = self._evidence(
            replay,
            worker_ok=worker_ok,
            worker_error=worker_error,
            success_criteria=success_criteria,
        )
        if deterministic_reasons:
            verdict = JudgeVerdict.FAIL
            summary = "Browser trace contains deterministic failure evidence."
        elif not success_criteria:
            verdict = JudgeVerdict.INDETERMINATE
            summary = "Trace is internally consistent but has no explicit success criteria."
        else:
            verdict = JudgeVerdict.PASS
            summary = "Browser trace is internally consistent with no deterministic failure."
        reasoning = "; ".join(deterministic_reasons) or summary
        source = "deterministic"
        metadata: dict[str, Any] = {
            "evidence_digest": digest_value(evidence),
            "criteria_count": len(success_criteria),
            "replay_complete": replay.complete,
        }
        if self.model is not None and self.policy.allow_model_advisory:
            model_value = dict(self.model.evaluate(evidence))
            source = "model_advisory"
            reasoning = str(model_value.get("reasoning") or reasoning)
            metadata["model_verdict"] = str(
                model_value.get("verdict")
                or model_value.get("success")
                or ""
            )
            metadata["model_failure_reason"] = str(
                model_value.get("failure_reason")
                or ""
            )
        return JudgeAdvisory(
            scope=replay.scope,
            verdict=verdict,
            summary=summary,
            reasoning=reasoning,
            failure_reasons=tuple(deterministic_reasons),
            evidence_record_ids=tuple(
                item.record_id
                for item in replay.records[-self.policy.max_trace_records:]
            ),
            evidence_artifact_ids=tuple(
                item.artifact_id
                for item in replay.artifacts
            ),
            source=source,
            metadata=metadata,
        )

    def history_record(
        self,
        advisory: JudgeAdvisory,
        *,
        sequence: int,
        previous_digest: str,
        parent_record_id: str,
    ) -> HistoryRecord:
        return HistoryRecord(
            scope=advisory.scope,
            kind=HistoryKind.JUDGE_ADVISORY,
            sequence=sequence,
            previous_digest=previous_digest,
            parent_record_id=parent_record_id,
            payload={
                "name": "browser_trace_judge",
                "advisory": advisory.to_dict(),
                "status": str(advisory.verdict),
                "authoritative": False,
            },
            artifact_ids=advisory.evidence_artifact_ids,
        )

    def _evidence(
        self,
        replay: ReplayProjection,
        *,
        worker_ok: bool,
        worker_error: str,
        success_criteria: Sequence[str],
    ) -> dict[str, Any]:
        records = [
            {
                "record_id": item.record_id,
                "kind": str(item.kind),
                "sequence": item.sequence,
                "tool_call_id": item.tool_call_id,
                "artifact_ids": list(item.artifact_ids),
                "payload_digest": digest_value(item.payload),
            }
            for item in replay.records[-self.policy.max_trace_records:]
        ]
        return {
            "scope": replay.scope.to_dict(),
            "worker_ok": worker_ok,
            "worker_error": worker_error,
            "success_criteria": list(success_criteria),
            "replay_complete": replay.complete,
            "issues": [item.to_dict() for item in replay.issues],
            "tool_pairs": [item.to_dict() for item in replay.tool_pairs],
            "signals": [item.to_dict() for item in replay.signals],
            "artifacts": [item.to_dict() for item in replay.artifacts],
            "records": records,
        }
