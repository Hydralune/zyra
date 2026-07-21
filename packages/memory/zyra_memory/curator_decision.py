from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .curator_evidence import ExtractionResult
from .curator_models import (
    CandidateKind,
    CandidateRelation,
    CandidateRelationKind,
    CandidateState,
    EvidenceBundle,
    EvidenceDocument,
    EvidenceKind,
    MemoryCandidate,
    MemoryScope,
    TrustTier,
    stable_digest,
    unique_strings,
)
from .curator_store import CuratorCandidateStore


FAILURE_EVENT_TYPES = {
    "node_failed",
    "command_failed",
    "subagent_failed",
    "browser_runtime_diagnostic",
    "failure_injected",
}


SUCCESS_EVENT_TYPES = {
    "command_succeeded",
    "subagent_completed",
    "evaluation",
    "constraint_check",
    "artifact_written",
}


DECISION_EVENT_TYPES = {
    "requirement_change",
    "resource_decision",
    "recovery_planned",
    "topology_route",
    "constraint_check",
}


MUTATION_KEYS = {
    "policy",
    "permission",
    "permissions",
    "rules",
    "state_transition_rules",
    "allowed_tools",
    "required_tools",
    "forbidden",
    "system_prompt",
    "developer_message",
    "validator",
    "trust_tier",
}


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_./:-]{2,}")
ERROR_CODE_PATTERN = re.compile(
    r"\b(?:[A-Z][A-Z0-9_]{2,}|[A-Za-z]+Error|HTTP\s*[45]\d\d|exit(?:ed)?\s+code\s+\d+)\b"
)
PATH_PATTERN = re.compile(r"(?:[A-Za-z]:\\|/)?(?:[\w.-]+[/\\])+[\w.-]+")
COMMAND_PATTERN = re.compile(r"(?:^|\s)(?:python|pytest|bun|npm|git|rg|curl|node|powershell)\s+[^\n]{1,300}")


class CandidateModel(Protocol):
    def propose(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CandidateSignal:
    signal_id: str
    category: str
    subject: str
    summary: str
    content: Mapping[str, Any]
    evidence_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    confidence: float
    scope: MemoryScope
    layer: str
    candidate_kind: CandidateKind
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "category": self.category,
            "subject": self.subject,
            "summary": self.summary,
            "content": dict(self.content),
            "evidence_ids": list(self.evidence_ids),
            "artifact_ids": list(self.artifact_ids),
            "confidence": self.confidence,
            "scope": self.scope.value,
            "layer": self.layer,
            "candidate_kind": self.candidate_kind.value,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class CandidateGenerationResult:
    candidates: tuple[MemoryCandidate, ...]
    relations: tuple[CandidateRelation, ...]
    model_status: str
    signal_count: int
    suppressed_count: int
    diagnostics: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_ids": [item.candidate_id for item in self.candidates],
            "relation_ids": [item.relation_id for item in self.relations],
            "model_status": self.model_status,
            "signal_count": self.signal_count,
            "suppressed_count": self.suppressed_count,
            "diagnostics": dict(self.diagnostics),
        }


class EvidenceSignalAnalyzer:
    """Translate evidence into stable semantic signals without model authority."""

    def analyze(self, extraction: ExtractionResult) -> tuple[CandidateSignal, ...]:
        documents = extraction.documents
        signals: list[CandidateSignal] = []
        signals.extend(self._decision_signals(documents))
        signals.extend(self._artifact_signals(documents))
        signals.extend(self._success_signals(documents))
        signals.extend(self._compression_signals(extraction.bundle, documents))
        return self._deduplicate(signals)

    def _decision_signals(self, documents: Sequence[EvidenceDocument]) -> list[CandidateSignal]:
        output: list[CandidateSignal] = []
        for document in documents:
            if document.ref.kind is not EvidenceKind.EVENT:
                continue
            event_type = str(document.normalized.get("event_type") or "")
            if event_type not in DECISION_EVENT_TYPES:
                continue
            payload = document.normalized.get("payload")
            payload_map = dict(payload) if isinstance(payload, Mapping) else {}
            summary = self._first_text(
                payload_map,
                keys=("summary", "reason", "description", "message", "requirement", "decision"),
            )
            if not summary:
                summary = f"Task recorded {event_type.replace('_', ' ')}."
            subject = self._subject(event_type, summary, payload_map)
            content = {
                "event_type": event_type,
                "fact": summary,
                "reason": str(payload_map.get("reason") or ""),
                "decision": self._bounded_mapping(payload_map),
            }
            confidence = 0.94 if document.ref.trust is TrustTier.VERIFIED_RUNTIME else 0.82
            output.append(
                self._signal(
                    category="decision",
                    subject=subject,
                    summary=summary,
                    content=content,
                    documents=(document,),
                    confidence=confidence,
                    scope=MemoryScope.TASK,
                    layer="semantic",
                    candidate_kind=CandidateKind.PROMOTE,
                    metadata={"event_type": event_type},
                )
            )
        return output

    def _artifact_signals(self, documents: Sequence[EvidenceDocument]) -> list[CandidateSignal]:
        output: list[CandidateSignal] = []
        for document in documents:
            if document.ref.kind is not EvidenceKind.ARTIFACT:
                continue
            normalized = document.normalized
            if bool(normalized.get("binary", False)):
                continue
            content = str(normalized.get("content") or "").strip()
            title = str(normalized.get("title") or document.title).strip()
            if not content or len(content) < 24:
                continue
            preview = self._compact_text(content, maximum=1200)
            summary = f"Artifact {title or document.ref.artifact_id}: {preview}"
            output.append(
                self._signal(
                    category="artifact",
                    subject=f"artifact:{self._slug(title or document.ref.artifact_id)}",
                    summary=self._compact_text(summary, maximum=600),
                    content={
                        "artifact_id": document.ref.artifact_id,
                        "artifact_kind": str(normalized.get("kind") or ""),
                        "title": title,
                        "content_digest": document.ref.content_digest,
                        "preview": preview,
                        "truncated": bool(normalized.get("truncated", False)),
                    },
                    documents=(document,),
                    confidence=0.88,
                    scope=MemoryScope.TASK,
                    layer="semantic",
                    candidate_kind=CandidateKind.PROMOTE,
                    metadata={"source": "artifact"},
                )
            )
        return output

    def _success_signals(self, documents: Sequence[EvidenceDocument]) -> list[CandidateSignal]:
        output: list[CandidateSignal] = []
        for document in documents:
            if document.ref.kind not in {EvidenceKind.EVENT, EvidenceKind.TOOL_RESULT, EvidenceKind.CODE_TRACE}:
                continue
            event_type = str(document.normalized.get("event_type") or "")
            status = str(document.normalized.get("status") or "").casefold()
            if event_type not in SUCCESS_EVENT_TYPES and status not in {"ok", "success", "succeeded", "passed"}:
                continue
            text = self._first_text(
                document.normalized,
                keys=("summary", "output", "fact", "result", "command", "test_result"),
            )
            if not text:
                text = f"{event_type or document.ref.kind.value} succeeded"
            subject = self._subject("success", text, document.normalized)
            output.append(
                self._signal(
                    category="success",
                    subject=subject,
                    summary=self._compact_text(text, maximum=500),
                    content={
                        "outcome": "success",
                        "event_type": event_type,
                        "detail": self._bounded_mapping(document.normalized),
                    },
                    documents=(document,),
                    confidence=0.84,
                    scope=MemoryScope.TASK,
                    layer="episodic",
                    candidate_kind=CandidateKind.PROMOTE,
                    metadata={"source_kind": document.ref.kind.value},
                )
            )
        return output

    def _compression_signals(
        self,
        bundle: EvidenceBundle,
        documents: Sequence[EvidenceDocument],
    ) -> list[CandidateSignal]:
        text_chars = sum(document.ref.char_count for document in documents)
        if len(documents) < 8 and text_chars < 20_000:
            return []
        event_types = Counter(
            str(document.normalized.get("event_type") or document.ref.kind.value)
            for document in documents
        )
        summary = (
            f"Evidence range {bundle.evidence_range.start_sequence}-"
            f"{bundle.evidence_range.end_sequence} contains {len(documents)} records; "
            + ", ".join(f"{name}={count}" for name, count in event_types.most_common(8))
            + "."
        )
        representative = self._representative_documents(documents, limit=12)
        return [
            self._signal(
                category="compression",
                subject=f"trajectory:{bundle.evidence_range.start_sequence}:{bundle.evidence_range.end_sequence}",
                summary=summary,
                content={
                    "source_bundle_id": bundle.bundle_id,
                    "source_bundle_digest": bundle.bundle_digest,
                    "source_document_count": len(documents),
                    "source_char_count": text_chars,
                    "event_type_counts": dict(sorted(event_types.items())),
                    "representative_evidence_ids": [item.ref.evidence_id for item in representative],
                    "compressed_summary": self._deterministic_summary(representative),
                },
                documents=representative,
                confidence=0.9,
                scope=MemoryScope.TASK,
                layer="episodic",
                candidate_kind=CandidateKind.COMPRESS,
                metadata={
                    "full_evidence_retained": True,
                    "protected_range": bundle.evidence_range.to_dict(),
                },
            )
        ]

    def _signal(
        self,
        *,
        category: str,
        subject: str,
        summary: str,
        content: Mapping[str, Any],
        documents: Sequence[EvidenceDocument],
        confidence: float,
        scope: MemoryScope,
        layer: str,
        candidate_kind: CandidateKind,
        metadata: Mapping[str, Any] | None = None,
    ) -> CandidateSignal:
        evidence_ids = unique_strings([item.ref.evidence_id for item in documents])
        artifact_ids = unique_strings([item.ref.artifact_id for item in documents if item.ref.artifact_id])
        signal_id = stable_digest(
            {
                "category": category,
                "subject": subject,
                "summary": summary,
                "content": content,
                "evidence_ids": evidence_ids,
                "candidate_kind": candidate_kind.value,
            }
        )[:24]
        return CandidateSignal(
            signal_id=f"signal_{signal_id}",
            category=category,
            subject=subject,
            summary=summary,
            content=dict(content),
            evidence_ids=evidence_ids,
            artifact_ids=artifact_ids,
            confidence=max(0.0, min(1.0, float(confidence))),
            scope=scope,
            layer=layer,
            candidate_kind=candidate_kind,
            metadata=dict(metadata or {}),
        )

    @staticmethod
    def _first_text(value: Mapping[str, Any], *, keys: Sequence[str]) -> str:
        for key in keys:
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, Mapping):
                for nested_key in ("summary", "message", "text", "output", "reason"):
                    nested = item.get(nested_key)
                    if isinstance(nested, str) and nested.strip():
                        return nested.strip()
        return ""

    @staticmethod
    def _subject(category: str, text: str, payload: Mapping[str, Any]) -> str:
        for key in ("requirement_id", "constraint_id", "decision_id", "command_name", "tool_name", "path"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"{category}:{EvidenceSignalAnalyzer._slug(value)}"
        tokens = [token.casefold() for token in TOKEN_PATTERN.findall(text) if len(token) > 2]
        tokens = [token for token in tokens if token not in {"the", "and", "for", "with", "from", "that"}]
        core = "-".join(tokens[:8]) or "task-fact"
        return f"{category}:{EvidenceSignalAnalyzer._slug(core)}"

    @staticmethod
    def _slug(value: str) -> str:
        normalized = re.sub(r"[^a-z0-9._:-]+", "-", value.casefold()).strip("-.")
        return normalized[:120] or "unknown"

    @staticmethod
    def _compact_text(value: str, *, maximum: int) -> str:
        text = re.sub(r"\s+", " ", str(value)).strip()
        if len(text) <= maximum:
            return text
        return f"{text[:maximum - 20].rstrip()} ...[truncated]"

    @staticmethod
    def _bounded_mapping(value: Mapping[str, Any], *, maximum_chars: int = 4000) -> Mapping[str, Any]:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if len(raw) <= maximum_chars:
            return dict(value)
        return {
            "digest": stable_digest(value),
            "preview": raw[:maximum_chars],
            "truncated": True,
            "original_chars": len(raw),
        }

    @staticmethod
    def _representative_documents(
        documents: Sequence[EvidenceDocument],
        *,
        limit: int,
    ) -> tuple[EvidenceDocument, ...]:
        if len(documents) <= limit:
            return tuple(documents)
        protected: list[EvidenceDocument] = []
        protected.extend(documents[:2])
        protected.extend(documents[-3:])
        important = [
            item
            for item in documents
            if str(item.normalized.get("event_type") or "")
            in FAILURE_EVENT_TYPES | DECISION_EVENT_TYPES | SUCCESS_EVENT_TYPES
        ]
        protected.extend(important)
        midpoint_count = max(0, limit - len({item.ref.evidence_id for item in protected}))
        if midpoint_count:
            step = max(1, len(documents) // midpoint_count)
            protected.extend(documents[index] for index in range(0, len(documents), step))
        by_id = {item.ref.evidence_id: item for item in protected}
        ordered = sorted(by_id.values(), key=lambda item: (item.ref.sequence, item.ref.evidence_id))
        return tuple(ordered[:limit])

    @staticmethod
    def _deterministic_summary(documents: Sequence[EvidenceDocument]) -> str:
        lines: list[str] = []
        for document in documents:
            event_type = str(document.normalized.get("event_type") or document.ref.kind.value)
            summary = EvidenceSignalAnalyzer._first_text(
                document.normalized,
                keys=("summary", "fact", "output", "reason", "status", "title", "content"),
            )
            if not summary:
                summary = document.title
            lines.append(
                f"[{document.ref.sequence}:{event_type}] "
                f"{EvidenceSignalAnalyzer._compact_text(summary, maximum=320)}"
            )
        return "\n".join(lines)

    @staticmethod
    def _deduplicate(signals: Sequence[CandidateSignal]) -> tuple[CandidateSignal, ...]:
        by_key: dict[tuple[str, str, str], CandidateSignal] = {}
        for signal in signals:
            key = (signal.candidate_kind.value, signal.layer, signal.subject)
            existing = by_key.get(key)
            if existing is None or signal.confidence > existing.confidence:
                by_key[key] = signal
        return tuple(
            sorted(
                by_key.values(),
                key=lambda item: (
                    item.candidate_kind.value,
                    item.layer,
                    item.subject,
                    item.signal_id,
                ),
            )
        )


class SkillCandidateProjector:
    """Detect reusable successful tool sequences; never writes a skill."""

    def project(
        self,
        *,
        bundle: EvidenceBundle,
        documents: Sequence[EvidenceDocument],
    ) -> tuple[CandidateSignal, ...]:
        tool_documents = [
            item
            for item in documents
            if item.ref.kind in {EvidenceKind.TOOL_RESULT, EvidenceKind.CODE_TRACE, EvidenceKind.BROWSER_TRACE}
        ]
        if len(tool_documents) < 2:
            return ()
        groups: dict[str, list[EvidenceDocument]] = defaultdict(list)
        for document in tool_documents:
            normalized = document.normalized
            tool_name = str(
                normalized.get("tool_name")
                or normalized.get("command")
                or normalized.get("action")
                or document.ref.producer
                or "unknown"
            )
            groups[tool_name].append(document)
        output: list[CandidateSignal] = []
        for tool_name, items in sorted(groups.items()):
            if len(items) < 2:
                continue
            failures = sum(1 for item in items if self._is_failure(item))
            successes = len(items) - failures
            if successes < 2 or failures > successes:
                continue
            commands = unique_strings(
                str(item.normalized.get("command") or "")
                for item in items
                if item.normalized.get("command")
            )
            paths = unique_strings(
                match.group(0)
                for item in items
                for match in PATH_PATTERN.finditer(item.text)
            )[:20]
            summary = (
                f"Reusable {tool_name} sequence succeeded {successes} time(s) "
                f"across {len(items)} observed step(s)."
            )
            subject = f"skill:{EvidenceSignalAnalyzer._slug(tool_name)}"
            evidence_ids = unique_strings(item.ref.evidence_id for item in items)
            artifacts = unique_strings(item.ref.artifact_id for item in items if item.ref.artifact_id)
            content = {
                "skill_name_hint": EvidenceSignalAnalyzer._slug(tool_name),
                "purpose": summary,
                "observed_tool": tool_name,
                "success_count": successes,
                "failure_count": failures,
                "commands": list(commands),
                "paths": list(paths),
                "steps": [self._step_projection(item) for item in items[:20]],
                "source_bundle_id": bundle.bundle_id,
                "publication_state": "candidate_only",
            }
            signal_id = stable_digest(
                {
                    "subject": subject,
                    "content": content,
                    "evidence_ids": evidence_ids,
                }
            )[:24]
            confidence = min(0.96, 0.62 + 0.08 * successes - 0.05 * failures)
            output.append(
                CandidateSignal(
                    signal_id=f"skill_signal_{signal_id}",
                    category="skill_candidate",
                    subject=subject,
                    summary=summary,
                    content=content,
                    evidence_ids=evidence_ids,
                    artifact_ids=artifacts,
                    confidence=confidence,
                    scope=MemoryScope.PROJECT,
                    layer="skill",
                    candidate_kind=CandidateKind.SKILL_CANDIDATE,
                    metadata={
                        "projector": "SkillCandidateProjector/1",
                        "writes_skill_registry": False,
                    },
                )
            )
        return tuple(output)

    @staticmethod
    def _is_failure(document: EvidenceDocument) -> bool:
        event_type = str(document.normalized.get("event_type") or "")
        status = str(document.normalized.get("status") or "").casefold()
        error = str(document.normalized.get("error") or "")
        return event_type in FAILURE_EVENT_TYPES or status in {"failed", "error"} or bool(error)

    @staticmethod
    def _step_projection(document: EvidenceDocument) -> Mapping[str, Any]:
        return {
            "sequence": document.ref.sequence,
            "evidence_id": document.ref.evidence_id,
            "kind": document.ref.kind.value,
            "producer": document.ref.producer,
            "tool_name": str(document.normalized.get("tool_name") or ""),
            "command": str(document.normalized.get("command") or ""),
            "action": str(document.normalized.get("action") or ""),
            "status": str(document.normalized.get("status") or ""),
        }


class FailurePatternMiner:
    """Mine repeated failure signatures with causal/recovery evidence."""

    def mine(
        self,
        *,
        bundle: EvidenceBundle,
        documents: Sequence[EvidenceDocument],
    ) -> tuple[CandidateSignal, ...]:
        failure_documents = [item for item in documents if self._is_failure(item)]
        if not failure_documents:
            return ()
        groups: dict[str, list[EvidenceDocument]] = defaultdict(list)
        for document in failure_documents:
            signature = self._signature(document)
            groups[signature].append(document)
        output: list[CandidateSignal] = []
        for signature, items in sorted(groups.items()):
            if len(items) < 2:
                continue
            recoveries = self._recoveries_for(items, documents)
            error_codes = unique_strings(
                match.group(0)
                for item in items
                for match in ERROR_CODE_PATTERN.finditer(item.text)
            )
            commands = unique_strings(
                match.group(0).strip()
                for item in items
                for match in COMMAND_PATTERN.finditer(item.text)
            )[:20]
            paths = unique_strings(
                match.group(0)
                for item in items
                for match in PATH_PATTERN.finditer(item.text)
            )[:20]
            evidence_ids = unique_strings(
                [item.ref.evidence_id for item in items]
                + [item.ref.evidence_id for item in recoveries]
            )
            artifacts = unique_strings(
                item.ref.artifact_id
                for item in (*items, *recoveries)
                if item.ref.artifact_id
            )
            subject = f"failure:{signature}"
            summary = (
                f"Failure pattern {signature} occurred {len(items)} time(s); "
                f"{len(recoveries)} later recovery signal(s) were observed."
            )
            content = {
                "signature": signature,
                "occurrence_count": len(items),
                "recovery_count": len(recoveries),
                "error_codes": list(error_codes),
                "commands": list(commands),
                "paths": list(paths),
                "occurrences": [self._failure_projection(item) for item in items[:30]],
                "recoveries": [self._recovery_projection(item) for item in recoveries[:30]],
                "source_bundle_id": bundle.bundle_id,
            }
            signal_id = stable_digest(
                {
                    "signature": signature,
                    "content": content,
                    "evidence_ids": evidence_ids,
                }
            )[:24]
            confidence = min(0.98, 0.7 + math.log2(len(items) + 1) * 0.08)
            output.append(
                CandidateSignal(
                    signal_id=f"failure_signal_{signal_id}",
                    category="failure_pattern",
                    subject=subject,
                    summary=summary,
                    content=content,
                    evidence_ids=evidence_ids,
                    artifact_ids=artifacts,
                    confidence=confidence,
                    scope=MemoryScope.PROJECT,
                    layer="semantic",
                    candidate_kind=CandidateKind.FAILURE_PATTERN,
                    metadata={"miner": "FailurePatternMiner/1"},
                )
            )
        return tuple(output)

    @staticmethod
    def _is_failure(document: EvidenceDocument) -> bool:
        event_type = str(document.normalized.get("event_type") or "")
        status = str(document.normalized.get("status") or "").casefold()
        error = document.normalized.get("error")
        return (
            event_type in FAILURE_EVENT_TYPES
            or status in {"failed", "failure", "error", "timeout", "crashed"}
            or bool(error)
        )

    @staticmethod
    def _signature(document: EvidenceDocument) -> str:
        event_type = str(document.normalized.get("event_type") or document.ref.kind.value)
        producer = document.ref.producer or "unknown"
        errors = ERROR_CODE_PATTERN.findall(document.text)
        core = errors[0] if errors else event_type
        raw = f"{producer}:{core}"
        return EvidenceSignalAnalyzer._slug(raw)[:100]

    @staticmethod
    def _recoveries_for(
        failures: Sequence[EvidenceDocument],
        documents: Sequence[EvidenceDocument],
    ) -> tuple[EvidenceDocument, ...]:
        first = min(item.ref.sequence for item in failures)
        recovery_types = {"recovery_planned", "command_succeeded", "subagent_completed", "evaluation"}
        return tuple(
            item
            for item in documents
            if item.ref.sequence > first
            and str(item.normalized.get("event_type") or "") in recovery_types
        )

    @staticmethod
    def _failure_projection(document: EvidenceDocument) -> Mapping[str, Any]:
        return {
            "sequence": document.ref.sequence,
            "evidence_id": document.ref.evidence_id,
            "event_type": str(document.normalized.get("event_type") or ""),
            "producer": document.ref.producer,
            "status": str(document.normalized.get("status") or ""),
            "error": EvidenceSignalAnalyzer._compact_text(
                str(document.normalized.get("error") or ""),
                maximum=500,
            ),
        }

    @staticmethod
    def _recovery_projection(document: EvidenceDocument) -> Mapping[str, Any]:
        return {
            "sequence": document.ref.sequence,
            "evidence_id": document.ref.evidence_id,
            "event_type": str(document.normalized.get("event_type") or ""),
            "producer": document.ref.producer,
            "status": str(document.normalized.get("status") or ""),
        }


class ModelCandidateAdapter:
    """Bound and sanitize model suggestions into proposal-only signals."""

    def __init__(self, model: CandidateModel | None, *, max_suggestions: int = 16) -> None:
        self.model = model
        self.max_suggestions = max(0, min(int(max_suggestions), 100))

    def propose(
        self,
        *,
        bundle: EvidenceBundle,
        documents: Sequence[EvidenceDocument],
        deterministic_signals: Sequence[CandidateSignal],
        enabled: bool,
    ) -> tuple[tuple[CandidateSignal, ...], str, Mapping[str, Any]]:
        if not enabled:
            return (), "disabled", {"reason": "request_disabled"}
        if self.model is None:
            return (), "unavailable", {"reason": "no_model_configured", "deterministic_degrade": True}
        request = {
            "schema": "zyra.memory-model-proposal.v1",
            "bundle": {
                "bundle_id": bundle.bundle_id,
                "bundle_digest": bundle.bundle_digest,
                "range": bundle.evidence_range.to_dict(),
            },
            "evidence": [self._document_projection(item) for item in documents[:128]],
            "deterministic_signals": [item.to_dict() for item in deterministic_signals[:64]],
            "instructions": {
                "proposal_only": True,
                "cannot_write_memory": True,
                "cannot_change_policy": True,
                "allowed_kinds": [
                    CandidateKind.PROMOTE.value,
                    CandidateKind.DISCARD.value,
                    CandidateKind.COMPRESS.value,
                    CandidateKind.SKILL_CANDIDATE.value,
                    CandidateKind.FAILURE_PATTERN.value,
                ],
            },
        }
        try:
            response = self.model.propose(request)
        except BaseException as error:
            return (), "unavailable", {
                "reason": type(error).__name__,
                "message": str(error)[:300],
                "deterministic_degrade": True,
            }
        suggestions = response.get("suggestions") if isinstance(response, Mapping) else None
        if not isinstance(suggestions, Sequence) or isinstance(suggestions, (str, bytes, bytearray)):
            return (), "invalid", {"reason": "suggestions_not_array", "deterministic_degrade": True}
        by_id = {item.ref.evidence_id: item for item in documents}
        signals: list[CandidateSignal] = []
        rejected = 0
        for raw in suggestions[: self.max_suggestions]:
            if not isinstance(raw, Mapping):
                rejected += 1
                continue
            signal = self._parse_suggestion(raw, by_id=by_id)
            if signal is None:
                rejected += 1
                continue
            signals.append(signal)
        return tuple(signals), "available", {
            "suggestion_count": len(suggestions),
            "accepted_proposal_count": len(signals),
            "rejected_proposal_count": rejected,
            "proposal_only": True,
        }

    def _parse_suggestion(
        self,
        value: Mapping[str, Any],
        *,
        by_id: Mapping[str, EvidenceDocument],
    ) -> CandidateSignal | None:
        allowed = {
            "kind",
            "layer",
            "scope",
            "subject",
            "summary",
            "content",
            "evidence_ids",
            "confidence",
            "ttl_seconds",
        }
        if set(str(key) for key in value) - allowed:
            return None
        content = value.get("content")
        if not isinstance(content, Mapping):
            return None
        if self._contains_mutation_key(content):
            return None
        try:
            kind = CandidateKind(str(value.get("kind") or ""))
            scope = MemoryScope(str(value.get("scope") or MemoryScope.TASK.value))
        except ValueError:
            return None
        layer = str(value.get("layer") or "semantic")
        if layer not in {"working", "episodic", "semantic", "skill"}:
            return None
        subject = str(value.get("subject") or "").strip()
        summary = str(value.get("summary") or "").strip()
        if not subject or not summary:
            return None
        evidence_ids = unique_strings(value.get("evidence_ids") or ())
        if not evidence_ids or any(evidence_id not in by_id for evidence_id in evidence_ids):
            return None
        documents = tuple(by_id[evidence_id] for evidence_id in evidence_ids)
        confidence = max(0.0, min(1.0, float(value.get("confidence", 0.5))))
        artifacts = unique_strings(item.ref.artifact_id for item in documents if item.ref.artifact_id)
        signal_id = stable_digest(
            {
                "kind": kind.value,
                "scope": scope.value,
                "layer": layer,
                "subject": subject,
                "summary": summary,
                "content": content,
                "evidence_ids": evidence_ids,
            }
        )[:24]
        return CandidateSignal(
            signal_id=f"model_signal_{signal_id}",
            category="model_proposal",
            subject=subject,
            summary=summary,
            content=dict(content),
            evidence_ids=evidence_ids,
            artifact_ids=artifacts,
            confidence=confidence,
            scope=scope,
            layer=layer,
            candidate_kind=kind,
            metadata={
                "model_assisted": True,
                "untrusted_proposal": True,
                "ttl_seconds": value.get("ttl_seconds"),
            },
        )

    @classmethod
    def _contains_mutation_key(cls, value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() in MUTATION_KEYS:
                    return True
                if cls._contains_mutation_key(item):
                    return True
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return any(cls._contains_mutation_key(item) for item in value)
        return False

    @staticmethod
    def _document_projection(document: EvidenceDocument) -> Mapping[str, Any]:
        return {
            "evidence_id": document.ref.evidence_id,
            "kind": document.ref.kind.value,
            "sequence": document.ref.sequence,
            "trust": document.ref.trust.value,
            "producer": document.ref.producer,
            "title": document.title,
            "text": document.text[:2000],
            "redacted": document.redacted,
        }


class CandidateConsolidator:
    """Create explicit duplicate/contradiction/supersede relations."""

    def __init__(self, store: CuratorCandidateStore) -> None:
        self.store = store

    def consolidate(
        self,
        candidates: Sequence[MemoryCandidate],
    ) -> tuple[tuple[MemoryCandidate, ...], tuple[CandidateRelation, ...]]:
        accepted: list[MemoryCandidate] = []
        relations: list[CandidateRelation] = []
        for candidate in sorted(candidates, key=lambda item: item.candidate_id):
            prior = self._prior_candidates(candidate)
            relation = self._relation(candidate, prior)
            if relation is None:
                accepted.append(candidate)
                continue
            relations.append(relation)
            self.store.save_relation(relation)
            if relation.kind is CandidateRelationKind.DUPLICATE_OF:
                self.store.update_candidate_state(
                    candidate.candidate_id,
                    expected=(CandidateState.PROPOSED,),
                    target=CandidateState.MERGED,
                    metadata={"relation_id": relation.relation_id},
                )
                continue
            if relation.kind is CandidateRelationKind.SUPERSEDES:
                accepted.append(candidate)
                continue
            if relation.kind is CandidateRelationKind.CONTRADICTS:
                accepted.append(candidate)
                continue
            accepted.append(candidate)
        return tuple(accepted), tuple(relations)

    def _prior_candidates(self, candidate: MemoryCandidate) -> tuple[MemoryCandidate, ...]:
        values = self.store.candidates(
            task_id=candidate.task_id,
            subject=candidate.subject,
            limit=1000,
        )
        return tuple(item for item in values if item.candidate_id != candidate.candidate_id)

    def _relation(
        self,
        candidate: MemoryCandidate,
        prior: Sequence[MemoryCandidate],
    ) -> CandidateRelation | None:
        for existing in sorted(prior, key=lambda item: (item.created_at, item.candidate_id), reverse=True):
            if candidate.semantic_digest == existing.semantic_digest:
                return CandidateRelation.build(
                    source_candidate_id=candidate.candidate_id,
                    target_candidate_id=existing.candidate_id,
                    kind=CandidateRelationKind.DUPLICATE_OF,
                    reason="same semantic projection",
                    evidence_digest=candidate.evidence_digest,
                )
            if candidate.kind is CandidateKind.DISCARD or existing.kind is CandidateKind.DISCARD:
                return CandidateRelation.build(
                    source_candidate_id=candidate.candidate_id,
                    target_candidate_id=existing.candidate_id,
                    kind=CandidateRelationKind.CONTRADICTS,
                    reason="discard proposal conflicts with retained proposal for the same subject",
                    evidence_digest=candidate.evidence_digest,
                )
            if self._contradicts(candidate, existing):
                return CandidateRelation.build(
                    source_candidate_id=candidate.candidate_id,
                    target_candidate_id=existing.candidate_id,
                    kind=CandidateRelationKind.CONTRADICTS,
                    reason="opposed values for the same subject require explicit merge",
                    evidence_digest=candidate.evidence_digest,
                    metadata={
                        "existing_evidence_digest": existing.evidence_digest,
                        "candidate_confidence": candidate.confidence,
                        "existing_confidence": existing.confidence,
                    },
                )
            if (
                candidate.expected_memory_id
                and candidate.expected_memory_id == existing.expected_memory_id
                and candidate.expected_revision is not None
                and existing.expected_revision is not None
                and candidate.expected_revision > existing.expected_revision
            ):
                return CandidateRelation.build(
                    source_candidate_id=candidate.candidate_id,
                    target_candidate_id=existing.candidate_id,
                    kind=CandidateRelationKind.SUPERSEDES,
                    reason="explicit higher expected revision supersedes prior proposal",
                    evidence_digest=candidate.evidence_digest,
                )
        return None

    @staticmethod
    def _contradicts(left: MemoryCandidate, right: MemoryCandidate) -> bool:
        left_values = CandidateConsolidator._fact_values(left.content)
        right_values = CandidateConsolidator._fact_values(right.content)
        if not left_values or not right_values:
            return False
        common = set(left_values).intersection(right_values)
        return any(left_values[key] != right_values[key] for key in common)

    @staticmethod
    def _fact_values(value: Mapping[str, Any]) -> dict[str, str]:
        output: dict[str, str] = {}
        for key in ("value", "status", "outcome", "decision", "requirement", "fact", "signature"):
            item = value.get(key)
            if isinstance(item, (str, bool, int, float)):
                output[key] = str(item).strip().casefold()
        return output


class MemoryDecisionRuntime:
    """Generate and persist proposal-only candidates for all five outcomes."""

    def __init__(
        self,
        *,
        store: CuratorCandidateStore,
        model: CandidateModel | None = None,
        analyzer: EvidenceSignalAnalyzer | None = None,
        skill_projector: SkillCandidateProjector | None = None,
        failure_miner: FailurePatternMiner | None = None,
        supplementary_port: Any | None = None,
    ) -> None:
        self.store = store
        self.analyzer = analyzer or EvidenceSignalAnalyzer()
        self.skill_projector = skill_projector or SkillCandidateProjector()
        self.failure_miner = failure_miner or FailurePatternMiner()
        self.model = ModelCandidateAdapter(model)
        self.consolidator = CandidateConsolidator(store)
        self.supplementary_port = supplementary_port

    def decide(
        self,
        extraction: ExtractionResult,
        *,
        allow_model_assist: bool,
        max_candidates: int,
    ) -> CandidateGenerationResult:
        bundle = extraction.bundle
        deterministic = list(self.analyzer.analyze(extraction))
        deterministic.extend(
            self.skill_projector.project(bundle=bundle, documents=extraction.documents)
        )
        deterministic.extend(
            self.failure_miner.mine(bundle=bundle, documents=extraction.documents)
        )
        model_signals, model_status, model_diagnostics = self.model.propose(
            bundle=bundle,
            documents=extraction.documents,
            deterministic_signals=deterministic,
            enabled=allow_model_assist,
        )
        signals = self._rank_signals((*deterministic, *model_signals))
        selected = signals[: max(1, int(max_candidates))]
        suppressed = max(0, len(signals) - len(selected))
        candidates: list[MemoryCandidate] = []
        for signal in selected:
            candidate = self._candidate_from_signal(bundle, signal)
            candidates.append(candidate)
        supplementary_diagnostics: Mapping[str, Any]
        if self.supplementary_port is None:
            stored_candidates: list[MemoryCandidate] = []
            for candidate in candidates:
                stored, _created = self.store.save_candidate(candidate)
                stored_candidates.append(stored)
            active, relations = self.consolidator.consolidate(stored_candidates)
            supplementary_diagnostics = {
                "runtime": "python_conformance_path",
                "typescript_active": False,
            }
        else:
            prior = tuple(
                item
                for item in self.store.candidates(task_id=bundle.task_id, limit=100_000)
                if item.candidate_id not in {candidate.candidate_id for candidate in candidates}
            )
            receipt = self.supplementary_port.consolidate(
                candidates,
                prior_candidates=prior,
            )
            stored_by_id: dict[str, MemoryCandidate] = {}
            for candidate in candidates:
                stored, _created = self.store.save_candidate(candidate)
                stored_by_id[stored.candidate_id] = stored
            relation_values: list[CandidateRelation] = []
            for raw in receipt.relations:
                source_id = str(raw.get("sourceCandidateId") or "")
                target_id = str(raw.get("targetCandidateId") or "")
                source = stored_by_id.get(source_id)
                if source is None:
                    raise RuntimeError("TypeScript consolidation returned an unknown source candidate")
                kind = CandidateRelationKind(str(raw.get("kind") or ""))
                relation = CandidateRelation.build(
                    source_candidate_id=source_id,
                    target_candidate_id=target_id,
                    kind=kind,
                    reason=str(raw.get("reason") or "TypeScript candidate consolidation"),
                    evidence_digest=source.evidence_digest,
                    metadata={
                        **(dict(raw.get("metadata")) if isinstance(raw.get("metadata"), Mapping) else {}),
                        "typescript_relation_id": str(raw.get("relationId") or ""),
                        "typescript_request_id": receipt.request_id,
                    },
                )
                self.store.save_relation(relation)
                relation_values.append(relation)
            for candidate_id in receipt.suppressed_candidate_ids:
                stored = stored_by_id[candidate_id]
                if stored.state is CandidateState.PROPOSED:
                    self.store.update_candidate_state(
                        candidate_id,
                        expected=(CandidateState.PROPOSED,),
                        target=CandidateState.MERGED,
                        metadata={"typescript_request_id": receipt.request_id},
                    )
            active = tuple(stored_by_id[candidate_id] for candidate_id in receipt.active_candidate_ids)
            relations = tuple(relation_values)
            supplementary_diagnostics = {
                "runtime": "typescript_oh_my_pi_supplement",
                "typescript_active": True,
                **dict(receipt.diagnostics),
                "request_id": receipt.request_id,
            }
        return CandidateGenerationResult(
            candidates=active,
            relations=relations,
            model_status=model_status,
            signal_count=len(signals),
            suppressed_count=suppressed,
            diagnostics={
                "deterministic_signal_count": len(deterministic),
                "model_signal_count": len(model_signals),
                "selected_signal_count": len(selected),
                "active_candidate_count": len(active),
                "relation_count": len(relations),
                "model": dict(model_diagnostics),
                "supplementary_consolidation": dict(supplementary_diagnostics),
                "candidate_kind_counts": dict(
                    sorted(Counter(item.kind.value for item in active).items())
                ),
            },
        )

    @staticmethod
    def _candidate_from_signal(
        bundle: EvidenceBundle,
        signal: CandidateSignal,
    ) -> MemoryCandidate:
        ttl_raw = signal.metadata.get("ttl_seconds")
        ttl_seconds = int(ttl_raw) if ttl_raw is not None else None
        return MemoryCandidate.build(
            run_id=bundle.run_id,
            task_id=bundle.task_id,
            kind=signal.candidate_kind,
            proposed_layer=signal.layer,
            scope=signal.scope,
            subject=signal.subject,
            summary=signal.summary,
            content={
                **dict(signal.content),
                "signal_category": signal.category,
                "signal_id": signal.signal_id,
            },
            bundle=bundle,
            confidence=signal.confidence,
            ttl_seconds=ttl_seconds,
            artifact_ids=signal.artifact_ids,
            model_assisted=bool(signal.metadata.get("model_assisted", False)),
            proposer="model_proposal" if signal.metadata.get("model_assisted") else "deterministic",
            metadata={
                **dict(signal.metadata),
                "parent_bundle_id": bundle.bundle_id,
                "parent_bundle_digest": bundle.bundle_digest,
            },
        )

    @staticmethod
    def _rank_signals(signals: Sequence[CandidateSignal]) -> tuple[CandidateSignal, ...]:
        kind_priority = {
            CandidateKind.FAILURE_PATTERN: 0,
            CandidateKind.SKILL_CANDIDATE: 1,
            CandidateKind.PROMOTE: 2,
            CandidateKind.COMPRESS: 3,
            CandidateKind.DISCARD: 4,
        }
        deduped: dict[tuple[str, str, str], CandidateSignal] = {}
        for signal in signals:
            key = (signal.candidate_kind.value, signal.layer, signal.subject)
            existing = deduped.get(key)
            if existing is None or signal.confidence > existing.confidence:
                deduped[key] = signal
        return tuple(
            sorted(
                deduped.values(),
                key=lambda item: (
                    kind_priority[item.candidate_kind],
                    -item.confidence,
                    item.subject,
                    item.signal_id,
                ),
            )
        )


__all__ = [
    "CandidateConsolidator",
    "CandidateGenerationResult",
    "CandidateModel",
    "CandidateSignal",
    "EvidenceSignalAnalyzer",
    "FailurePatternMiner",
    "MemoryDecisionRuntime",
    "ModelCandidateAdapter",
    "SkillCandidateProjector",
]
