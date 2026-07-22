from __future__ import annotations

import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .contracts import EnvelopeObservation, Finding, GateResult, GateStatus, Severity


TARGETED_POLICY = "targeted_artifact_ref"
REQUIRED_BASELINES = {
    "static_route",
    "full_connect_broadcast",
    "full_text_inline",
}


@dataclass(frozen=True, slots=True)
class EntropyBudget:
    maximum_inline_bytes: int = 4 * 1024
    maximum_envelope_bytes: int = 8 * 1024
    maximum_summary_bytes: int = 1024
    maximum_artifact_refs: int = 64
    maximum_p95_envelope_bytes: int = 4 * 1024

    def validate(self) -> None:
        values = (
            self.maximum_inline_bytes,
            self.maximum_envelope_bytes,
            self.maximum_summary_bytes,
            self.maximum_artifact_refs,
            self.maximum_p95_envelope_bytes,
        )
        if any(value <= 0 for value in values):
            raise ValueError("all low-entropy budgets must be positive")
        if self.maximum_p95_envelope_bytes > self.maximum_envelope_bytes:
            raise ValueError("p95 envelope budget cannot exceed the absolute envelope budget")

    def to_dict(self) -> dict[str, int]:
        return {
            "maximum_inline_bytes": self.maximum_inline_bytes,
            "maximum_envelope_bytes": self.maximum_envelope_bytes,
            "maximum_summary_bytes": self.maximum_summary_bytes,
            "maximum_artifact_refs": self.maximum_artifact_refs,
            "maximum_p95_envelope_bytes": self.maximum_p95_envelope_bytes,
        }


class EnvelopeObservationExtractor:
    def from_events(self, events: Sequence[Mapping[str, Any]]) -> tuple[EnvelopeObservation, ...]:
        observations: list[EnvelopeObservation] = []
        for index, event in enumerate(events):
            event_type = str(event.get("event_type") or event.get("type") or "").lower()
            payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
            envelope = self._envelope(payload, event)
            if not envelope and event_type not in {"agent_message", "runtime_message", "message_committed"}:
                continue
            observation = self._observation(envelope or payload, event=event, index=index)
            if observation is not None:
                observations.append(observation)
        return tuple(observations)

    @staticmethod
    def _envelope(payload: Mapping[str, Any], event: Mapping[str, Any]) -> Mapping[str, Any]:
        for key in ("agent_message", "message_envelope", "envelope", "runtime_message"):
            value = payload.get(key)
            if isinstance(value, Mapping):
                return value
            value = event.get(key)
            if isinstance(value, Mapping):
                return value
        return {}

    def _observation(
        self,
        envelope: Mapping[str, Any],
        *,
        event: Mapping[str, Any],
        index: int,
    ) -> EnvelopeObservation | None:
        policy = str(
            envelope.get("policy")
            or envelope.get("payload_policy")
            or envelope.get("routing_policy")
            or event.get("policy")
            or ""
        ).strip()
        if not policy:
            return None
        inline = self._inline_payload(envelope)
        summary = envelope.get("summary") or envelope.get("semantic_summary") or ""
        refs = envelope.get("artifact_refs") or envelope.get("refs") or []
        recipients = envelope.get("recipients") or envelope.get("target_ids") or []
        eligible = envelope.get("eligible_recipients") or envelope.get("eligible_target_ids") or recipients
        source_bytes = self._positive_int(
            envelope.get("source_bytes")
            or envelope.get("original_bytes")
            or envelope.get("full_text_bytes")
            or len(self._bytes(envelope.get("source") or envelope.get("full_text") or inline))
        )
        envelope_bytes = self._positive_int(
            envelope.get("envelope_bytes")
            or envelope.get("serialized_bytes")
            or len(self._bytes(envelope))
        )
        facts = envelope.get("fact_ids") or envelope.get("facts") or []
        duplicate_fact_count = self._positive_int(envelope.get("duplicate_fact_count"))
        if duplicate_fact_count == 0 and isinstance(facts, Sequence) and not isinstance(facts, (str, bytes, bytearray)):
            normalized = [self._fact_identity(item) for item in facts]
            duplicate_fact_count = len(normalized) - len(set(normalized))
        return EnvelopeObservation(
            message_id=str(envelope.get("message_id") or event.get("event_id") or f"message-{index}"),
            policy=policy,
            route_kind=str(envelope.get("route_kind") or envelope.get("routing") or "unknown"),
            envelope_bytes=envelope_bytes,
            inline_bytes=len(self._bytes(inline)),
            source_bytes=max(source_bytes, len(self._bytes(inline))),
            summary_bytes=len(self._bytes(summary)),
            artifact_refs=len(refs) if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes, bytearray)) else 0,
            recipient_count=self._count(recipients),
            eligible_recipient_count=max(1, self._count(eligible)),
            token_count=self._positive_int(
                envelope.get("token_count")
                or envelope.get("message_tokens")
                or math.ceil(envelope_bytes / 4)
            ),
            duplicate_fact_count=duplicate_fact_count,
            fact_count=self._count(facts),
            redelivery_count=self._positive_int(envelope.get("redelivery_count")),
            effective_transition_count=self._positive_int(
                envelope.get("effective_transition_count") or envelope.get("transition_count")
            ),
            task_succeeded=bool(envelope.get("task_succeeded", event.get("task_succeeded", True))),
            metadata={
                "event_type": str(event.get("event_type") or ""),
                "contains_full_transcript": self._contains_full_payload(envelope),
            },
        )

    @staticmethod
    def _inline_payload(envelope: Mapping[str, Any]) -> Any:
        for key in ("inline", "inline_payload", "content", "body", "text"):
            if key in envelope:
                return envelope[key]
        return ""

    @staticmethod
    def _contains_full_payload(envelope: Mapping[str, Any]) -> bool:
        lowered_keys = {str(key).lower() for key in envelope}
        if lowered_keys & {"full_transcript", "raw_tool_result", "raw_dom", "binary_inline", "source_blob"}:
            return True
        inline = EnvelopeObservationExtractor._inline_payload(envelope)
        if isinstance(inline, Mapping):
            inline_keys = {str(key).lower() for key in inline}
            return bool(inline_keys & {"transcript", "dom", "binary", "source", "raw_result"})
        return False

    @staticmethod
    def _bytes(value: Any) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        except (TypeError, ValueError):
            return repr(value).encode("utf-8")

    @staticmethod
    def _positive_int(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _count(value: Any) -> int:
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return len(value)
        return 1 if value else 0

    @staticmethod
    def _fact_identity(value: Any) -> str:
        encoded = EnvelopeObservationExtractor._bytes(value)
        return hashlib.sha256(encoded).hexdigest()


class EntropyMetrics:
    def summarize(self, observations: Sequence[EnvelopeObservation]) -> dict[str, Any]:
        if not observations:
            return self._empty()
        envelope_bytes = [item.envelope_bytes for item in observations]
        inline_bytes = [item.inline_bytes for item in observations]
        source_bytes = [item.source_bytes for item in observations]
        summary_bytes = [item.summary_bytes for item in observations]
        refs = [item.artifact_refs for item in observations]
        recipients = [item.recipient_count for item in observations]
        eligible = [item.eligible_recipient_count for item in observations]
        tokens = sum(item.token_count for item in observations)
        facts = sum(item.fact_count for item in observations)
        duplicates = sum(item.duplicate_fact_count for item in observations)
        redeliveries = sum(item.redelivery_count for item in observations)
        transitions = sum(item.effective_transition_count for item in observations)
        total_source = sum(source_bytes)
        total_inline = sum(inline_bytes)
        total_envelope = sum(envelope_bytes)
        offloaded = sum(max(0, item.source_bytes - item.inline_bytes) for item in observations if item.artifact_refs > 0)
        broadcast_messages = sum(
            1
            for item in observations
            if item.route_kind.lower() in {"broadcast", "full_broadcast", "full_connect"}
            or item.recipient_count >= item.eligible_recipient_count > 1
        )
        return {
            "message_count": len(observations),
            "envelope_bytes": self._distribution(envelope_bytes),
            "inline_bytes": self._distribution(inline_bytes),
            "source_bytes": self._distribution(source_bytes),
            "summary_bytes": self._distribution(summary_bytes),
            "artifact_refs": self._distribution(refs),
            "fanout": self._distribution(recipients),
            "inline_source_ratio": round(total_inline / max(1, total_source), 6),
            "route_density": round(sum(recipients) / max(1, sum(eligible)), 6),
            "broadcast_ratio": round(broadcast_messages / max(1, len(observations)), 6),
            "artifact_ref_offload_ratio": round(offloaded / max(1, total_source), 6),
            "duplicate_fact_rate": round(duplicates / max(1, facts), 6),
            "redelivery_rate": round(redeliveries / max(1, len(observations)), 6),
            "message_count_per_token": round(len(observations) / max(1, tokens), 8),
            "bytes_per_effective_transition": round(total_envelope / max(1, transitions), 6),
            "task_success": all(item.task_succeeded for item in observations),
            "full_payload_inline_count": sum(
                1 for item in observations if bool(item.metadata.get("contains_full_transcript"))
            ),
            "total_envelope_bytes": total_envelope,
            "total_inline_bytes": total_inline,
            "total_source_bytes": total_source,
            "total_tokens": tokens,
            "effective_transition_count": transitions,
        }

    @staticmethod
    def _distribution(values: Sequence[int]) -> dict[str, float | int]:
        ordered = sorted(values)
        return {
            "p50": EntropyMetrics._quantile(ordered, 0.50),
            "p95": EntropyMetrics._quantile(ordered, 0.95),
            "max": max(ordered) if ordered else 0,
            "mean": round(statistics.fmean(ordered), 3) if ordered else 0,
            "total": sum(ordered),
        }

    @staticmethod
    def _quantile(ordered: Sequence[int], quantile: float) -> float:
        if not ordered:
            return 0
        if len(ordered) == 1:
            return float(ordered[0])
        position = (len(ordered) - 1) * quantile
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return float(ordered[lower])
        weight = position - lower
        return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)

    @staticmethod
    def _empty() -> dict[str, Any]:
        distribution = {"p50": 0, "p95": 0, "max": 0, "mean": 0, "total": 0}
        return {
            "message_count": 0,
            "envelope_bytes": dict(distribution),
            "inline_bytes": dict(distribution),
            "source_bytes": dict(distribution),
            "summary_bytes": dict(distribution),
            "artifact_refs": dict(distribution),
            "fanout": dict(distribution),
            "inline_source_ratio": 0,
            "route_density": 0,
            "broadcast_ratio": 0,
            "artifact_ref_offload_ratio": 0,
            "duplicate_fact_rate": 0,
            "redelivery_rate": 0,
            "message_count_per_token": 0,
            "bytes_per_effective_transition": 0,
            "task_success": False,
            "full_payload_inline_count": 0,
            "total_envelope_bytes": 0,
            "total_inline_bytes": 0,
            "total_source_bytes": 0,
            "total_tokens": 0,
            "effective_transition_count": 0,
        }


class LowEntropyGate:
    def __init__(self, *, budget: EntropyBudget | None = None) -> None:
        self.budget = budget or EntropyBudget()
        self.budget.validate()
        self.extractor = EnvelopeObservationExtractor()
        self.metrics = EntropyMetrics()

    def evaluate(
        self,
        observations: Sequence[EnvelopeObservation] = (),
        *,
        events: Sequence[Mapping[str, Any]] = (),
        baselines: Mapping[str, Sequence[EnvelopeObservation]] | None = None,
        final_completion: bool = False,
    ) -> GateResult:
        result = GateResult(
            gate_id="low-entropy",
            status=GateStatus.NOT_RUN,
            summary="Targeted communication budgets and static/broadcast/full-text baseline comparison.",
        )
        observed = tuple(observations) or self.extractor.from_events(events)
        targeted = tuple(item for item in observed if self._is_targeted(item.policy))
        other = tuple(item for item in observed if not self._is_targeted(item.policy))
        baseline_map = {key: tuple(value) for key, value in (baselines or {}).items()}
        for item in other:
            baseline_map.setdefault(self._normalize_policy(item.policy), tuple())
            baseline_map[self._normalize_policy(item.policy)] = (*baseline_map[self._normalize_policy(item.policy)], item)
        targeted_metrics = self.metrics.summarize(targeted)
        result.findings.extend(self._budget_findings(targeted, targeted_metrics))
        result.findings.extend(self._baseline_findings(targeted_metrics, baseline_map, final_completion=final_completion))
        if not targeted:
            result.add(
                Finding(
                    code="entropy.targeted_observations_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="No targeted+artifact-ref communication observations were supplied.",
                )
            )
        baseline_metrics = {name: self.metrics.summarize(values) for name, values in sorted(baseline_map.items())}
        result.metrics.update(
            {
                "budget": self.budget.to_dict(),
                "targeted_policy": TARGETED_POLICY,
                "targeted": targeted_metrics,
                "baselines": baseline_metrics,
                "observation_count": len(observed),
                "targeted_observation_count": len(targeted),
                "observations": [item.to_dict() for item in observed],
            }
        )
        missing = REQUIRED_BASELINES - set(baseline_map)
        if missing and not final_completion:
            result.limitations.append("Final baseline lanes remain open: " + ", ".join(sorted(missing)))
        return result.finish(default_partial=not final_completion)

    def _budget_findings(
        self,
        observations: Sequence[EnvelopeObservation],
        metrics: Mapping[str, Any],
    ) -> list[Finding]:
        findings: list[Finding] = []
        for item in observations:
            checks = (
                (item.inline_bytes, self.budget.maximum_inline_bytes, "inline_bytes"),
                (item.envelope_bytes, self.budget.maximum_envelope_bytes, "envelope_bytes"),
                (item.summary_bytes, self.budget.maximum_summary_bytes, "summary_bytes"),
                (item.artifact_refs, self.budget.maximum_artifact_refs, "artifact_refs"),
            )
            for observed, maximum, metric in checks:
                if observed > maximum:
                    findings.append(
                        Finding(
                            code=f"entropy.{metric}_budget_exceeded",
                            severity=Severity.BLOCKER,
                            summary="Targeted message exceeds the 05C low-entropy budget.",
                            detail=f"observed={observed}; maximum={maximum}",
                            location=item.message_id,
                        )
                    )
            if item.metadata.get("contains_full_transcript"):
                findings.append(
                    Finding(
                        code="entropy.full_payload_inline",
                        severity=Severity.BLOCKER,
                        summary="Full transcript/source/DOM/binary/raw tool result was sent inline.",
                        location=item.message_id,
                    )
                )
        p95 = float((metrics.get("envelope_bytes") or {}).get("p95") or 0)
        if p95 > self.budget.maximum_p95_envelope_bytes:
            findings.append(
                Finding(
                    code="entropy.p95_budget_exceeded",
                    severity=Severity.BLOCKER,
                    summary="Targeted envelope p95 exceeds the required 4 KiB budget.",
                    detail=f"p95={p95}; maximum={self.budget.maximum_p95_envelope_bytes}",
                )
            )
        return findings

    def _baseline_findings(
        self,
        targeted: Mapping[str, Any],
        baselines: Mapping[str, Sequence[EnvelopeObservation]],
        *,
        final_completion: bool,
    ) -> list[Finding]:
        findings: list[Finding] = []
        missing = REQUIRED_BASELINES - set(baselines)
        for name in sorted(missing):
            findings.append(
                Finding(
                    code="entropy.baseline_missing",
                    severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                    summary="Required communication baseline is missing.",
                    detail=name,
                )
            )
        for name, observations in baselines.items():
            baseline = self.metrics.summarize(observations)
            if not observations:
                findings.append(
                    Finding(
                        code="entropy.baseline_empty",
                        severity=Severity.BLOCKER if final_completion else Severity.WARNING,
                        summary="A declared baseline contains no real observations.",
                        detail=name,
                    )
                )
                continue
            if bool(targeted.get("task_success")) and not bool(baseline.get("task_success")):
                findings.append(
                    Finding(
                        code="entropy.baseline_task_failed",
                        severity=Severity.ERROR,
                        summary="Baseline comparison is invalid because task success differs.",
                        detail=name,
                    )
                )
            if not bool(targeted.get("task_success")) and bool(baseline.get("task_success")):
                findings.append(
                    Finding(
                        code="entropy.targeted_task_success_regressed",
                        severity=Severity.BLOCKER,
                        summary="Targeted policy reduces communication by failing the task.",
                        detail=name,
                    )
                )
            if name in {"full_connect_broadcast", "full_text_inline"}:
                targeted_bytes = float(targeted.get("total_envelope_bytes") or 0)
                baseline_bytes = float(baseline.get("total_envelope_bytes") or 0)
                if targeted_bytes >= baseline_bytes and targeted_bytes > 0:
                    findings.append(
                        Finding(
                            code="entropy.no_byte_improvement",
                            severity=Severity.ERROR,
                            summary="Targeted policy does not reduce total envelope bytes versus baseline.",
                            detail=f"baseline={name}; targeted={targeted_bytes}; baseline_bytes={baseline_bytes}",
                        )
                    )
                if float(targeted.get("broadcast_ratio") or 0) >= float(baseline.get("broadcast_ratio") or 0):
                    findings.append(
                        Finding(
                            code="entropy.no_broadcast_improvement",
                            severity=Severity.ERROR,
                            summary="Targeted policy does not reduce broadcast ratio versus baseline.",
                            detail=name,
                        )
                    )
        return findings

    @staticmethod
    def _normalize_policy(value: str) -> str:
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "static": "static_route",
            "static_routing": "static_route",
            "broadcast": "full_connect_broadcast",
            "full_broadcast": "full_connect_broadcast",
            "full_connect": "full_connect_broadcast",
            "full_text": "full_text_inline",
            "full_inline": "full_text_inline",
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _is_targeted(value: str) -> bool:
        normalized = LowEntropyGate._normalize_policy(value)
        return normalized in {TARGETED_POLICY, "targeted", "targeted_route", "low_entropy"}
