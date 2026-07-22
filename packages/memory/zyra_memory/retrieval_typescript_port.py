from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .retrieval_models import (
    QueryIntent,
    QueryIntentCategory,
    RetrievalDiagnostics,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalVoice,
    TemporalConstraint,
    VectorAvailability,
    VoiceScore,
    stable_identifier,
)


RETRIEVAL_ALGORITHM_PROTOCOL = "zyra.retrieval-algorithms.v1"


class TypeScriptRetrievalAlgorithmError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TypeScriptRetrievalAlgorithmsPort:
    """Typed boundary to the retained oh-my-pi retrieval algorithms.

    TypeScript owns intent, temporal parsing, voice fusion, and MMR ordering.
    Python keeps the canonical index, candidate hydration, delivery journal, and
    query audit records. The response is treated as untrusted process output and
    revalidated against the exact candidates sent across the boundary.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        bun_executable: str = "",
        timeout_seconds: float = 20.0,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.entrypoint = (
            self.project_root
            / "packages"
            / "memory"
            / "retrieval-algorithms"
            / "src"
            / "main.ts"
        ).resolve()
        workspace_bun = self.project_root / "node_modules" / ".bin" / (
            "bun.exe" if __import__("os").name == "nt" else "bun"
        )
        self.bun_executable = (
            bun_executable
            or shutil.which("bun")
            or (str(workspace_bun) if workspace_bun.is_file() else "")
        )
        self.timeout_seconds = max(1.0, float(timeout_seconds))

    @property
    def available(self) -> bool:
        return bool(self.bun_executable and self.entrypoint.is_file())

    def health(self) -> Mapping[str, Any]:
        return {
            "protocol": RETRIEVAL_ALGORITHM_PROTOCOL,
            "available": self.available,
            "bun_configured": bool(self.bun_executable),
            "entrypoint_exists": self.entrypoint.is_file(),
            "entrypoint": self._relative_entrypoint(),
            "canonical_write_capability": False,
            "database_path_shared": False,
            "source_mechanisms": (
                "oh-my-pi/mnemopi query-intent, temporal-parser, "
                "polyphonic RRF, and MMR"
            ),
        }

    def enrich_query(self, query: RetrievalQuery) -> RetrievalQuery:
        validated = query.validated()
        request_id = stable_identifier(
            "ts_retrieval_analyze",
            validated.query_id,
            validated.text,
            validated.request_time,
        )
        response = self._invoke(
            {
                "protocol": RETRIEVAL_ALGORITHM_PROTOCOL,
                "requestId": request_id,
                "operation": "analyze",
                "payload": {
                    "query": validated.text,
                    "requestTime": validated.request_time,
                },
            },
            timeout_seconds=min(
                self.timeout_seconds,
                max(0.1, validated.budget.timeout_ms / 2000.0),
            ),
        )
        result = self._result(response)
        analyzed_intent = self._intent(result.get("intent"))
        intent = validated.intent or analyzed_intent
        temporal = self._temporal(result.get("temporal"))
        filters = validated.filters
        if not filters.temporal.active():
            filters = replace(filters, temporal=temporal)
        return replace(validated, intent=intent, filters=filters)

    def rank(
        self,
        query: RetrievalQuery,
        *,
        fts_hits: Sequence[RetrievalHit],
        vector_hits: Sequence[RetrievalHit],
        vector_status: VectorAvailability,
        vector_reason: str,
        index_scope: str,
        index_generation: int,
        elapsed_ms: float,
        warnings: Sequence[str] = (),
    ) -> RetrievalResult:
        enriched = query.validated()
        if enriched.intent is None:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_analysis_required",
                "TypeScript query analysis must run before ranking",
            )
        request_id = stable_identifier(
            "ts_retrieval_rank",
            enriched.query_id,
            enriched.text,
            [item.content_digest for item in fts_hits],
            [item.content_digest for item in vector_hits],
        )
        candidates = [
            self._candidate(
                item,
                source_voice="fts",
                maximum_document_characters=enriched.budget.max_document_chars,
            )
            for item in fts_hits
        ] + [
            self._candidate(
                item,
                source_voice="vector",
                maximum_document_characters=enriched.budget.max_document_chars,
            )
            for item in vector_hits
        ]
        if len(candidates) > enriched.budget.candidate_limit * 2:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_candidate_budget_exceeded",
                "retrieval candidate count exceeds the two bounded search voices",
            )
        response = self._invoke(
            {
                "protocol": RETRIEVAL_ALGORITHM_PROTOCOL,
                "requestId": request_id,
                "operation": "rank",
                "payload": {
                    "query": enriched.text,
                    "requestTime": enriched.request_time,
                    "intent": {
                        "category": enriched.intent.category.value,
                        "confidence": enriched.intent.confidence,
                        "signals": [item.value for item in enriched.intent.signals],
                        "vectorBias": enriched.intent.vector_bias,
                        "ftsBias": enriched.intent.fts_bias,
                        "importanceBias": enriched.intent.importance_bias,
                    },
                    "temporal": {
                        "startAt": enriched.filters.temporal.start_at,
                        "endAt": enriched.filters.temporal.end_at,
                        "tags": list(enriched.filters.temporal.tags),
                        "precision": enriched.filters.temporal.precision,
                        "sourceText": enriched.filters.temporal.source_text,
                    },
                    "budget": {
                        "limit": enriched.budget.limit,
                        "candidateLimit": enriched.budget.candidate_limit,
                        "maximumOutputCharacters": enriched.budget.max_output_chars,
                        "maximumDocumentCharacters": enriched.budget.max_document_chars,
                        "mmrLambda": enriched.budget.mmr_lambda,
                    },
                    "candidates": candidates,
                },
            },
            timeout_seconds=min(
                self.timeout_seconds,
                max(0.1, enriched.budget.timeout_ms / 2000.0),
            ),
        )
        result = self._result(response)
        intent = self._intent(result.get("intent"))
        temporal = self._temporal(result.get("temporal"))
        if enriched.intent is not None and intent != enriched.intent:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_analysis_drift",
                "TypeScript intent changed between analysis and ranking",
            )
        if enriched.filters.temporal.active() and temporal != enriched.filters.temporal:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_analysis_drift",
                "TypeScript temporal constraint changed between analysis and ranking",
            )
        representatives = self._representatives((*fts_hits, *vector_hits))
        ranked = self._ranked_hits(result.get("hits"), representatives)
        if len(ranked) > enriched.budget.limit:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_output_budget_exceeded",
                "TypeScript returned more hits than the query limit",
            )
        consumed_characters = 0
        for hit in ranked:
            if len(hit.content) > enriched.budget.max_document_chars:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_output_budget_exceeded",
                    "TypeScript returned a document beyond max_document_chars",
                )
            consumed_characters += len(hit.title) + len(hit.content) + 128
        if consumed_characters > enriched.budget.max_output_chars:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_output_budget_exceeded",
                "TypeScript returned content beyond max_output_chars",
            )
        candidate_count = self._non_negative_integer(
            result.get("candidateCount"), "candidateCount"
        )
        expected_candidate_count = len(representatives)
        if candidate_count != expected_candidate_count:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_candidate_count_mismatch",
                "TypeScript candidate count does not match the request",
            )
        evidence_digest = str(result.get("evidenceDigest") or "")
        if re.fullmatch(r"[0-9a-f]{64}", evidence_digest) is None:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_evidence_invalid",
                "TypeScript ranking evidence digest is invalid",
            )
        diagnostics = RetrievalDiagnostics(
            query_id=enriched.query_id,
            fts_used=True,
            vector_status=vector_status,
            vector_reason=vector_reason,
            candidate_count=candidate_count,
            returned_count=len(ranked),
            filtered_count=max(
                0,
                len(fts_hits)
                + len(vector_hits)
                - self._non_negative_integer(result.get("fusedCount"), "fusedCount"),
            ),
            truncated=bool(result.get("truncated")),
            index_generation=index_generation,
            index_scope=index_scope,
            elapsed_ms=elapsed_ms,
            intent=intent,
            temporal=temporal,
            warnings=tuple(
                dict.fromkeys(
                    [
                        *(str(item) for item in warnings if str(item)),
                        "typescript_retrieval_algorithms_active",
                        f"typescript_ranking_evidence:{evidence_digest}",
                    ]
                )
            ),
        )
        return RetrievalResult(query=enriched, hits=ranked, diagnostics=diagnostics)

    def _invoke(
        self,
        request: Mapping[str, Any],
        *,
        timeout_seconds: float | None = None,
    ) -> Mapping[str, Any]:
        if not self.bun_executable:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_bun_unavailable",
                "Bun is required for the retained TypeScript retrieval algorithms",
            )
        if not self.entrypoint.is_file():
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_entrypoint_missing",
                f"TypeScript retrieval entrypoint is missing: {self._relative_entrypoint()}",
            )
        try:
            encoded = json.dumps(request, ensure_ascii=False, sort_keys=True)
            if len(encoded.encode("utf-8")) > 16 * 1024 * 1024:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_request_too_large",
                    "TypeScript retrieval request exceeds the 16 MiB process boundary",
                )
            completed = subprocess.run(
                [self.bun_executable, str(self.entrypoint)],
                input=encoded,
                text=True,
                capture_output=True,
                cwd=self.project_root,
                timeout=(
                    self.timeout_seconds
                    if timeout_seconds is None
                    else max(0.1, min(self.timeout_seconds, timeout_seconds))
                ),
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_timeout",
                "TypeScript retrieval algorithms did not respond before the deadline",
            ) from error
        except OSError as error:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_spawn_failed",
                str(error),
            ) from error
        stdout = completed.stdout.strip()
        if len(stdout.encode("utf-8")) > 16 * 1024 * 1024:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_response_too_large",
                "TypeScript retrieval response exceeds the 16 MiB process boundary",
            )
        if not stdout:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_empty_response",
                completed.stderr.strip()[:1000]
                or "TypeScript retrieval algorithms returned no response",
            )
        try:
            decoded = json.loads(stdout.splitlines()[-1])
        except json.JSONDecodeError as error:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_invalid_json",
                stdout[-1000:],
            ) from error
        if not isinstance(decoded, Mapping):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript retrieval response must be an object",
            )
        if decoded.get("protocol") != RETRIEVAL_ALGORITHM_PROTOCOL:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_mismatch",
                "TypeScript retrieval response protocol mismatch",
            )
        if decoded.get("requestId") != request.get("requestId"):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_request_mismatch",
                "TypeScript retrieval response request id mismatch",
            )
        if decoded.get("ok") is not True:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_rejected",
                str(decoded.get("error") or completed.stderr or "request rejected")[:1000],
            )
        return dict(decoded)

    @staticmethod
    def _result(response: Mapping[str, Any]) -> Mapping[str, Any]:
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript retrieval result must be an object",
            )
        return result

    @staticmethod
    def _candidate(
        hit: RetrievalHit,
        *,
        source_voice: str,
        maximum_document_characters: int,
    ) -> Mapping[str, Any]:
        if not hit.content_digest:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_candidate_invalid",
                f"retrieval candidate has no content digest: {hit.document_id}",
            )
        return {
            "documentId": hit.document_id,
            "title": hit.title[:1000],
            "content": hit.content[:maximum_document_characters],
            "score": hit.score,
            "importance": hit.importance,
            "eventAt": hit.event_at,
            "sourceVoice": source_voice,
            "contentDigest": hit.content_digest,
        }

    @staticmethod
    def _representatives(
        hits: Sequence[RetrievalHit],
    ) -> Mapping[str, RetrievalHit]:
        representatives: dict[str, RetrievalHit] = {}
        for hit in hits:
            existing = representatives.get(hit.document_id)
            if existing is None or hit.score > existing.score:
                representatives[hit.document_id] = hit
            elif existing.content_digest != hit.content_digest:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_candidate_conflict",
                    f"candidate identity has conflicting content: {hit.document_id}",
                )
        return representatives

    @classmethod
    def _ranked_hits(
        cls,
        value: object,
        representatives: Mapping[str, RetrievalHit],
    ) -> tuple[RetrievalHit, ...]:
        if not isinstance(value, list):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript ranked hits must be an array",
            )
        ranked: list[RetrievalHit] = []
        seen: set[str] = set()
        for expected_rank, raw in enumerate(value, start=1):
            if not isinstance(raw, Mapping):
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_protocol_invalid",
                    "TypeScript ranked hit must be an object",
                )
            document_id = str(raw.get("documentId") or "")
            source = representatives.get(document_id)
            if source is None or document_id in seen:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_partition_invalid",
                    "TypeScript returned an unknown or duplicate candidate",
                )
            rank = cls._non_negative_integer(raw.get("rank"), "hit.rank")
            if rank != expected_rank:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_rank_invalid",
                    "TypeScript returned a non-contiguous rank",
                )
            score = cls._finite_float(raw.get("score"), "hit.score")
            content = str(raw.get("content") or "")
            if not source.content.startswith(content):
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_content_invalid",
                    "TypeScript returned content outside the submitted candidate",
                )
            voices = cls._voices(raw.get("voices"))
            ranked.append(
                replace(
                    source,
                    score=score,
                    rank=rank,
                    content=content,
                    voices=voices,
                )
            )
            seen.add(document_id)
        return tuple(ranked)

    @classmethod
    def _voices(cls, value: object) -> tuple[VoiceScore, ...]:
        if not isinstance(value, list):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript voice scores must be an array",
            )
        result: list[VoiceScore] = []
        seen_voices: set[RetrievalVoice] = set()
        for raw in value:
            if not isinstance(raw, Mapping):
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_protocol_invalid",
                    "TypeScript voice score must be an object",
                )
            try:
                voice = RetrievalVoice(str(raw.get("voice") or ""))
            except ValueError as error:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_voice_invalid",
                    "TypeScript returned an unknown retrieval voice",
                ) from error
            if voice in seen_voices:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_voice_invalid",
                    "TypeScript returned a duplicate retrieval voice",
                )
            voice_rank = cls._non_negative_integer(
                raw.get("rank"), "voice.rank"
            )
            if voice_rank < 1:
                raise TypeScriptRetrievalAlgorithmError(
                    "typescript_retrieval_voice_invalid",
                    "TypeScript retrieval voice rank must start at one",
                )
            result.append(
                VoiceScore(
                    voice=voice,
                    score=cls._finite_float(raw.get("score"), "voice.score"),
                    rank=voice_rank,
                    detail={
                        "raw_score": cls._finite_float(
                            raw.get("rawScore"), "voice.rawScore"
                        ),
                        "weight": cls._finite_float(
                            raw.get("weight"), "voice.weight"
                        ),
                        "algorithm_owner": "typescript",
                    },
                )
            )
            seen_voices.add(voice)
        return tuple(result)

    @classmethod
    def _intent(cls, value: object) -> QueryIntent:
        if not isinstance(value, Mapping):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript intent must be an object",
            )
        try:
            category = QueryIntentCategory(str(value.get("category") or ""))
            signals = tuple(
                QueryIntentCategory(str(item))
                for item in value.get("signals", [])
            )
        except (TypeError, ValueError) as error:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_intent_invalid",
                "TypeScript returned an invalid intent category",
            ) from error
        return QueryIntent(
            category=category,
            confidence=cls._unit_float(value.get("confidence"), "intent.confidence"),
            signals=signals,
            vector_bias=cls._non_negative_float(
                value.get("vectorBias"), "intent.vectorBias"
            ),
            fts_bias=cls._non_negative_float(value.get("ftsBias"), "intent.ftsBias"),
            importance_bias=cls._non_negative_float(
                value.get("importanceBias"), "intent.importanceBias"
            ),
        )

    @staticmethod
    def _temporal(value: object) -> TemporalConstraint:
        if not isinstance(value, Mapping):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_protocol_invalid",
                "TypeScript temporal constraint must be an object",
            )
        tags = value.get("tags")
        if not isinstance(tags, list):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_temporal_invalid",
                "TypeScript temporal tags must be an array",
            )
        precision = str(value.get("precision") or "unknown")
        if precision not in {"day", "week", "month", "year", "relative", "unknown"}:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_temporal_invalid",
                "TypeScript temporal precision is invalid",
            )
        start_at = TypeScriptRetrievalAlgorithmsPort._normalized_timestamp(
            value.get("startAt"), "temporal.startAt"
        )
        end_at = TypeScriptRetrievalAlgorithmsPort._normalized_timestamp(
            value.get("endAt"), "temporal.endAt"
        )
        if start_at and end_at and start_at >= end_at:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_temporal_invalid",
                "TypeScript temporal window must have a positive duration",
            )
        return TemporalConstraint(
            start_at=start_at,
            end_at=end_at,
            tags=tuple(dict.fromkeys(str(item) for item in tags if str(item))),
            precision=precision,
            source_text=str(value.get("sourceText") or ""),
        )

    @staticmethod
    def _normalized_timestamp(value: object, name: str) -> str:
        text = str(value or "")
        if not text:
            return ""
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_temporal_invalid",
                f"{name} must be an ISO timestamp",
            ) from error
        if parsed.tzinfo is None:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_temporal_invalid",
                f"{name} must include a timezone",
            )
        return parsed.astimezone(UTC).isoformat()

    @staticmethod
    def _finite_float(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_numeric_invalid", f"{name} must be numeric"
            )
        result = float(value)
        if not math.isfinite(result):
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_numeric_invalid", f"{name} must be finite"
            )
        return result

    @classmethod
    def _non_negative_float(cls, value: object, name: str) -> float:
        result = cls._finite_float(value, name)
        if result < 0:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_numeric_invalid",
                f"{name} must be non-negative",
            )
        return result

    @classmethod
    def _unit_float(cls, value: object, name: str) -> float:
        result = cls._finite_float(value, name)
        if not 0 <= result <= 1:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_numeric_invalid",
                f"{name} must be between zero and one",
            )
        return result

    @staticmethod
    def _non_negative_integer(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeScriptRetrievalAlgorithmError(
                "typescript_retrieval_numeric_invalid",
                f"{name} must be a non-negative integer",
            )
        return value

    def _relative_entrypoint(self) -> str:
        try:
            return self.entrypoint.relative_to(self.project_root).as_posix()
        except ValueError:
            return str(self.entrypoint)


__all__ = [
    "RETRIEVAL_ALGORITHM_PROTOCOL",
    "TypeScriptRetrievalAlgorithmError",
    "TypeScriptRetrievalAlgorithmsPort",
]
