from __future__ import annotations

import re
import unicodedata

from .retrieval_models import RetrievalFilter, RetrievalHit


WORD_PATTERN = re.compile(r"[^\W_]+(?:[-_][^\W_]+)*", re.UNICODE)


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text))
    value = value.replace("\x00", " ")
    return " ".join(value.split())


def query_terms(
    text: str,
    *,
    minimum_length: int = 1,
    limit: int = 64,
) -> tuple[str, ...]:
    """Tokenize a query for SQLite FTS expression construction only.

    Intent, temporal analysis, voice fusion, and MMR are owned by the retained
    TypeScript retrieval package. This helper does not score or rank results.
    """

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


def fts_match_expression(
    text: str,
    *,
    match_all: bool = False,
    prefix: bool = False,
) -> str:
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


def filter_hit_in_memory(hit: RetrievalHit, filters: RetrievalFilter) -> bool:
    """Apply already-resolved filter constraints to a hydrated vector hit."""

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
    if normalized.layers and hit.layer not in {
        str(item) for item in normalized.layers
    }:
        return False
    if normalized.artifact_ids and not set(normalized.artifact_ids).intersection(
        hit.artifact_ids
    ):
        return False
    temporal = normalized.temporal
    if temporal.start_at and (
        not hit.event_at or hit.event_at < temporal.start_at
    ):
        return False
    if temporal.end_at and (
        not hit.event_at or hit.event_at >= temporal.end_at
    ):
        return False
    return True


__all__ = [
    "filter_hit_in_memory",
    "fts_match_expression",
    "normalize_text",
    "query_terms",
]
