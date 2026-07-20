from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

from .retrieval_models import (
    QueryIntent,
    QueryIntentCategory,
    RetrievalBudget,
    RetrievalDiagnostics,
    RetrievalFilter,
    RetrievalHit,
    RetrievalQuery,
    RetrievalResult,
    RetrievalVoice,
    TemporalConstraint,
    VectorAvailability,
    VoiceScore,
)


WORD_PATTERN = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)
ISO_DATE_PATTERN = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
SLASH_DATE_PATTERN = re.compile(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b")
RELATIVE_PATTERN = re.compile(
    r"\b(?P<count>\d+)\s+(?P<unit>second|minute|hour|day|week|month|year)s?\s+"
    r"(?P<direction>ago|before|earlier|back|later|from\s+now)\b",
    re.IGNORECASE,
)


INTENT_PATTERNS: tuple[tuple[QueryIntentCategory, tuple[re.Pattern[str], ...]], ...] = (
    (
        QueryIntentCategory.TEMPORAL,
        (
            re.compile(r"\b(when|last|yesterday|today|tomorrow|ago|before|after|since|until|recently|latest)\b", re.I),
            re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I),
            ISO_DATE_PATTERN,
            re.compile(r"\b(this|next|last)\s+(week|month|year)\b", re.I),
        ),
    ),
    (
        QueryIntentCategory.FACTUAL,
        (
            re.compile(r"\bwhat\s+is\b", re.I),
            re.compile(r"\bwho\s+is\b", re.I),
            re.compile(r"\bwhere\s+is\b", re.I),
            re.compile(r"\b(definition|define|explain|meaning)\b", re.I),
            re.compile(r"\bhow\s+(many|much|long|far)\b", re.I),
        ),
    ),
    (
        QueryIntentCategory.ENTITY,
        (
            re.compile(r"\b(tell\s+me\s+about|what\s+do\s+you\s+know\s+about)\b", re.I),
            re.compile(r"\b(about|regarding|concerning)\s+[\w-]+\b", re.I),
        ),
    ),
    (
        QueryIntentCategory.PREFERENCE,
        (
            re.compile(r"\b(prefer|like|dislike|want|hate|love|favorite|best|worst)\b", re.I),
            re.compile(r"\b(should\s+i|recommend|choose|pick|select|option|choice)\b", re.I),
        ),
    ),
    (
        QueryIntentCategory.PROCEDURAL,
        (
            re.compile(r"\bhow\s+(to|do|can|should|would)\b", re.I),
            re.compile(r"\b(step|process|procedure|workflow|guide|tutorial)\b", re.I),
            re.compile(r"\b(setup|install|configure|build|deploy|run|execute|start|stop)\b", re.I),
        ),
    ),
)


INTENT_WEIGHTS: Mapping[QueryIntentCategory, tuple[float, float, float]] = {
    QueryIntentCategory.TEMPORAL: (0.60, 1.50, 0.80),
    QueryIntentCategory.FACTUAL: (1.00, 1.20, 0.90),
    QueryIntentCategory.ENTITY: (1.10, 1.00, 1.30),
    QueryIntentCategory.PREFERENCE: (0.90, 0.80, 1.50),
    QueryIntentCategory.PROCEDURAL: (1.30, 0.90, 0.70),
    QueryIntentCategory.GENERAL: (1.00, 1.00, 1.00),
}


DAY_MAP: Mapping[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "mon": 0,
    "tue": 1,
    "wed": 2,
    "thu": 3,
    "fri": 4,
    "sat": 5,
    "sun": 6,
}


MONTH_MAP: Mapping[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("\x00", " ")
    return " ".join(value.split())


def query_terms(text: str, *, minimum_length: int = 1, limit: int = 64) -> tuple[str, ...]:
    normalized = normalize_text(text).casefold()
    seen: set[str] = set()
    terms: list[str] = []
    for match in WORD_PATTERN.finditer(normalized):
        term = match.group(0).strip("-_")
        if len(term) < minimum_length or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= limit:
            break
    return tuple(terms)


def fts_match_expression(text: str, *, match_all: bool = False, prefix: bool = False) -> str:
    terms = query_terms(text)
    if not terms:
        return ""
    escaped: list[str] = []
    for term in terms:
        value = term.replace('"', '""')
        suffix = "*" if prefix and len(value) >= 3 else ""
        escaped.append(f'"{value}"{suffix}')
    operator = " AND " if match_all else " OR "
    return operator.join(escaped)


def classify_intent(text: str) -> QueryIntent:
    best = QueryIntentCategory.GENERAL
    best_score = 0.0
    signals: list[QueryIntentCategory] = []
    for category, patterns in INTENT_PATTERNS:
        matches = 0
        for pattern in patterns:
            if pattern.search(text):
                matches += 1
                signals.append(category)
        if matches:
            score = min(0.30 + matches * 0.15, 1.0)
            if score > best_score:
                best = category
                best_score = score
    vector, fts, importance = INTENT_WEIGHTS[best]
    return QueryIntent(
        category=best,
        confidence=best_score,
        signals=tuple(signals),
        vector_bias=vector,
        fts_bias=fts,
        importance_bias=importance,
    )


def _reference_time(value: datetime | str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = datetime.now(UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _week_bounds(value: datetime) -> tuple[datetime, datetime]:
    start, _ = _day_bounds(value)
    start -= timedelta(days=start.weekday())
    return start, start + timedelta(days=7)


def _month_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _year_bounds(value: datetime) -> tuple[datetime, datetime]:
    start = value.astimezone(UTC).replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, start.replace(year=start.year + 1)


def _constraint(start: datetime, end: datetime, precision: str, text: str, *tags: str) -> TemporalConstraint:
    return TemporalConstraint(
        start_at=start.isoformat(),
        end_at=end.isoformat(),
        tags=tuple(tag for tag in tags if tag),
        precision=precision,
        source_text=text,
    )


def parse_temporal_constraint(text: str, *, reference: datetime | str | None = None) -> TemporalConstraint:
    query = normalize_text(text)
    lower = query.casefold()
    now = _reference_time(reference)

    matched = ISO_DATE_PATTERN.search(query)
    if matched:
        try:
            value = datetime(int(matched.group(1)), int(matched.group(2)), int(matched.group(3)), tzinfo=UTC)
        except ValueError:
            value = None
        if value is not None:
            start, end = _day_bounds(value)
            return _constraint(start, end, "day", matched.group(0), start.date().isoformat())

    matched = SLASH_DATE_PATTERN.search(query)
    if matched:
        first, second, year = (int(item) for item in matched.groups())
        year += 2000 if year < 100 else 0
        month, day = (second, first) if first > 12 else (first, second)
        try:
            value = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            value = None
        if value is not None:
            start, end = _day_bounds(value)
            return _constraint(start, end, "day", matched.group(0), start.date().isoformat())

    named_month = re.search(
        r"\b(" + "|".join(MONTH_MAP) + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?\b",
        lower,
    )
    if named_month:
        month = MONTH_MAP[named_month.group(1)]
        day = int(named_month.group(2))
        year = int(named_month.group(3) or now.year)
        try:
            value = datetime(year, month, day, tzinfo=UTC)
        except ValueError:
            value = None
        if value is not None:
            start, end = _day_bounds(value)
            return _constraint(start, end, "day", named_month.group(0), start.date().isoformat())

    if "day before yesterday" in lower:
        start, end = _day_bounds(now - timedelta(days=2))
        return _constraint(start, end, "day", "day before yesterday", "day-before-yesterday")
    if re.search(r"\byesterday\b", lower):
        start, end = _day_bounds(now - timedelta(days=1))
        return _constraint(start, end, "day", "yesterday", "yesterday")
    if re.search(r"\btoday\b", lower):
        start, end = _day_bounds(now)
        return _constraint(start, end, "day", "today", "today")
    if re.search(r"\btomorrow\b", lower):
        start, end = _day_bounds(now + timedelta(days=1))
        return _constraint(start, end, "day", "tomorrow", "tomorrow")

    period = re.search(r"\b(last|this|next)\s+(week|month|year)\b", lower)
    if period:
        qualifier, unit = period.groups()
        shifted = now
        if unit == "week":
            shifted += timedelta(days={"last": -7, "this": 0, "next": 7}[qualifier])
            start, end = _week_bounds(shifted)
        elif unit == "month":
            offset = {"last": -1, "this": 0, "next": 1}[qualifier]
            absolute = now.year * 12 + now.month - 1 + offset
            shifted = datetime(absolute // 12, absolute % 12 + 1, 1, tzinfo=UTC)
            start, end = _month_bounds(shifted)
        else:
            shifted = now.replace(year=now.year + {"last": -1, "this": 0, "next": 1}[qualifier])
            start, end = _year_bounds(shifted)
        return _constraint(start, end, unit, period.group(0), f"{qualifier}-{unit}")

    weekday = re.search(
        r"\b(?:(last|this|next)\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tue|wed|thu|fri|sat|sun)\b",
        lower,
    )
    if weekday:
        qualifier = weekday.group(1) or "this"
        target = DAY_MAP[weekday.group(2)]
        if qualifier == "last":
            delta = -(((now.weekday() - target) % 7) + 7)
        elif qualifier == "next":
            delta = (target - now.weekday()) % 7 or 7
        else:
            delta = -((now.weekday() - target) % 7)
        start, end = _day_bounds(now + timedelta(days=delta))
        return _constraint(start, end, "day", weekday.group(0), weekday.group(2), qualifier)

    relative = RELATIVE_PATTERN.search(lower)
    if relative:
        count = int(relative.group("count"))
        unit = relative.group("unit")
        direction = 1 if relative.group("direction") in {"later", "from now"} else -1
        seconds = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2_592_000,
            "year": 31_536_000,
        }[unit]
        target = now + timedelta(seconds=count * seconds * direction)
        start, end = _day_bounds(target)
        return _constraint(start, end, "relative", relative.group(0), f"{count}-{unit}-{relative.group('direction')}")

    if re.search(r"\b(recent|recently|latest|lately|not long ago)\b", lower):
        return _constraint(now - timedelta(days=7), now + timedelta(seconds=1), "relative", "recent", "recent")
    return TemporalConstraint()


def enrich_query(query: RetrievalQuery, *, reference: datetime | str | None = None) -> RetrievalQuery:
    validated = query.validated()
    intent = validated.intent or classify_intent(validated.text)
    temporal = validated.filters.temporal
    if not temporal.active():
        temporal = parse_temporal_constraint(validated.text, reference=reference)
    filters = replace(validated.filters, temporal=temporal)
    return replace(validated, intent=intent, filters=filters)


def lexical_similarity(left: str, right: str) -> float:
    left_terms = set(query_terms(left, minimum_length=2, limit=512))
    right_terms = set(query_terms(right, minimum_length=2, limit=512))
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def normalized_importance(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    if value <= 0:
        return 0.0
    return value / (1.0 + value)


def recency_score(event_at: str, *, reference: datetime | str | None = None, half_life_days: float = 30.0) -> float:
    if not event_at:
        return 0.0
    try:
        value = _reference_time(event_at)
    except (TypeError, ValueError):
        return 0.0
    now = _reference_time(reference)
    age_seconds = max(0.0, (now - value).total_seconds())
    if half_life_days <= 0:
        return 0.0
    return math.exp(-math.log(2.0) * age_seconds / (half_life_days * 86400.0))


def reciprocal_rank_fusion(
    voices: Mapping[RetrievalVoice, Sequence[RetrievalHit]],
    *,
    voice_weights: Mapping[RetrievalVoice, float] | None = None,
    rank_constant: int = 60,
) -> list[RetrievalHit]:
    weights = voice_weights or {}
    aggregate: dict[str, float] = defaultdict(float)
    details: dict[str, list[VoiceScore]] = defaultdict(list)
    representatives: dict[str, RetrievalHit] = {}
    for voice in sorted(voices, key=str):
        weight = max(0.0, float(weights.get(voice, 1.0)))
        ordered = sorted(
            voices[voice],
            key=lambda item: (-item.score, item.document_id),
        )
        for rank, hit in enumerate(ordered, start=1):
            representatives.setdefault(hit.document_id, hit)
            contribution = weight / (rank_constant + rank)
            aggregate[hit.document_id] += contribution
            details[hit.document_id].append(
                VoiceScore(
                    voice=voice,
                    score=contribution,
                    rank=rank,
                    detail={"raw_score": hit.score, "weight": weight},
                )
            )
    fused = [
        representatives[document_id].with_score(score, details[document_id])
        for document_id, score in aggregate.items()
    ]
    fused.sort(key=lambda item: (-item.score, item.document_id))
    return fused


def mmr_rerank(
    hits: Sequence[RetrievalHit],
    *,
    limit: int,
    lambda_value: float = 0.72,
) -> list[RetrievalHit]:
    count = max(0, int(limit))
    if count == 0:
        return []
    remaining = sorted(hits, key=lambda item: (-item.score, item.document_id))
    if len(remaining) <= 1:
        return remaining[:count]
    selected: list[RetrievalHit] = []
    while remaining and len(selected) < count:
        best_index = 0
        best_value = float("-inf")
        best_identity = ""
        for index, candidate in enumerate(remaining):
            similarity = max(
                (
                    lexical_similarity(
                        f"{candidate.title}\n{candidate.content}",
                        f"{prior.title}\n{prior.content}",
                    )
                    for prior in selected
                ),
                default=0.0,
            )
            value = lambda_value * candidate.score - (1.0 - lambda_value) * similarity
            if value > best_value or (value == best_value and candidate.document_id < best_identity):
                best_index = index
                best_value = value
                best_identity = candidate.document_id
        selected.append(remaining.pop(best_index))
    return [hit.with_rank(index) for index, hit in enumerate(selected, start=1)]


def apply_output_budget(hits: Sequence[RetrievalHit], budget: RetrievalBudget) -> tuple[list[RetrievalHit], bool]:
    selected: list[RetrievalHit] = []
    consumed = 0
    truncated = False
    for hit in hits:
        content = hit.content[: budget.max_document_chars]
        projected = len(hit.title) + len(content) + 128
        if selected and consumed + projected > budget.max_output_chars:
            truncated = True
            break
        if not selected and projected > budget.max_output_chars:
            available = max(0, budget.max_output_chars - len(hit.title) - 128)
            content = content[:available]
            truncated = len(content) < len(hit.content)
        selected.append(replace(hit, content=content))
        consumed += len(hit.title) + len(content) + 128
        if len(selected) >= budget.limit:
            truncated = truncated or len(hits) > len(selected)
            break
    return selected, truncated


def combine_retrieval_hits(
    query: RetrievalQuery,
    *,
    fts_hits: Sequence[RetrievalHit],
    vector_hits: Sequence[RetrievalHit] = (),
    vector_status: VectorAvailability = VectorAvailability.DISABLED,
    vector_reason: str = "",
    index_scope: str = "",
    index_generation: int = 0,
    elapsed_ms: float = 0.0,
    warnings: Iterable[str] = (),
) -> RetrievalResult:
    enriched = enrich_query(query)
    intent = enriched.intent or QueryIntent()
    vector_weight, fts_weight, importance_weight = intent.normalized_weights()
    importance_hits = [
        hit.with_score(normalized_importance(hit.importance))
        for hit in (*fts_hits, *vector_hits)
        if hit.importance > 0
    ]
    recency_hits = [
        hit.with_score(recency_score(hit.event_at, reference=enriched.request_time))
        for hit in (*fts_hits, *vector_hits)
        if hit.event_at
    ]
    voices: dict[RetrievalVoice, Sequence[RetrievalHit]] = {
        RetrievalVoice.FTS: fts_hits,
        RetrievalVoice.IMPORTANCE: importance_hits,
        RetrievalVoice.RECENCY: recency_hits,
    }
    weights: dict[RetrievalVoice, float] = {
        RetrievalVoice.FTS: fts_weight,
        RetrievalVoice.IMPORTANCE: importance_weight,
        RetrievalVoice.RECENCY: 0.10 if intent.category is QueryIntentCategory.TEMPORAL else 0.03,
    }
    if vector_hits:
        voices[RetrievalVoice.VECTOR] = vector_hits
        weights[RetrievalVoice.VECTOR] = vector_weight
    fused = reciprocal_rank_fusion(voices, voice_weights=weights)
    diverse = mmr_rerank(
        fused,
        limit=enriched.budget.candidate_limit,
        lambda_value=enriched.budget.mmr_lambda,
    )
    selected, truncated = apply_output_budget(diverse, enriched.budget)
    ranked = tuple(hit.with_rank(index) for index, hit in enumerate(selected, start=1))
    diagnostics = RetrievalDiagnostics(
        query_id=enriched.query_id,
        fts_used=True,
        vector_status=vector_status,
        vector_reason=vector_reason,
        candidate_count=len({hit.document_id for hit in (*fts_hits, *vector_hits)}),
        returned_count=len(ranked),
        filtered_count=max(0, len(fts_hits) + len(vector_hits) - len(fused)),
        truncated=truncated,
        index_generation=index_generation,
        index_scope=index_scope,
        elapsed_ms=elapsed_ms,
        intent=intent,
        temporal=enriched.filters.temporal,
        warnings=tuple(dict.fromkeys(str(item) for item in warnings if str(item))),
    )
    return RetrievalResult(query=enriched, hits=ranked, diagnostics=diagnostics)


def filter_hit_in_memory(hit: RetrievalHit, filters: RetrievalFilter) -> bool:
    normalized = filters.normalized()
    checks: tuple[tuple[tuple[str, ...], str], ...] = (
        (normalized.run_ids, hit.run_id),
        (normalized.task_ids, hit.task_id),
        (normalized.session_ids, hit.session_id),
        (normalized.source_types, hit.source_kind.value),
        (normalized.source_ids, hit.source_id),
        (normalized.skill_names, hit.skill_name),
        (normalized.failure_kinds, hit.failure_kind),
        (normalized.node_ids, hit.node_id),
        (normalized.workspace_ids, hit.workspace_id),
    )
    for allowed, actual in checks:
        if allowed and actual not in allowed:
            return False
    if normalized.layers and hit.layer not in {str(item) for item in normalized.layers}:
        return False
    if normalized.artifact_ids and not set(normalized.artifact_ids).intersection(hit.artifact_ids):
        return False
    temporal = normalized.temporal
    if temporal.start_at and (not hit.event_at or hit.event_at < temporal.start_at):
        return False
    if temporal.end_at and (not hit.event_at or hit.event_at >= temporal.end_at):
        return False
    return True
