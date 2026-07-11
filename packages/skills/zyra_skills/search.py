from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from fnmatch import fnmatch
from threading import RLock
from typing import Any, Iterable, Sequence

from .models import SkillListingEntry, utc_now
from .registry import SkillRegistry


TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}")


@dataclass(frozen=True, slots=True)
class SkillSearchHit:
    qualified_name: str
    name: str
    description: str
    score: float
    matched_terms: tuple[str, ...]
    source: str
    invocation_mode: str
    content_digest: str
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "qualified_name": self.qualified_name,
            "name": self.name,
            "description": self.description,
            "score": round(self.score, 6),
            "matched_terms": list(self.matched_terms),
            "source": self.source,
            "invocation_mode": self.invocation_mode,
            "content_digest": self.content_digest,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class SkillSearchResult:
    query: str
    generation: int
    hits: tuple[SkillSearchHit, ...]
    local_only: bool = True
    remote_search_status: str = "deferred_upstream_stub"
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "generation": self.generation,
            "hits": [hit.to_dict() for hit in self.hits],
            "local_only": self.local_only,
            "remote_search_status": self.remote_search_status,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class _IndexedSkill:
    entry: SkillListingEntry
    terms: Counter[str]
    document_length: int


class SkillSearchIndex:
    """Metadata-only local search; never reads body or resources."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._lock = RLock()
        self._generation = -1
        self._documents: dict[str, _IndexedSkill] = {}
        self._document_frequency: Counter[str] = Counter()
        self._average_length = 1.0

    def refresh(self, *, workspace_paths: Sequence[str] = ()) -> int:
        generation = self.registry.generation
        entries = self.registry.list(workspace_paths=workspace_paths, include_namespaced=True)
        documents: dict[str, _IndexedSkill] = {}
        frequency: Counter[str] = Counter()
        total_length = 0
        for entry in entries:
            if not isinstance(entry, SkillListingEntry):
                continue
            weighted = " ".join(
                (
                    entry.name,
                    entry.name,
                    entry.qualified_name,
                    entry.description,
                    entry.description,
                    entry.when_to_use,
                    entry.preferred_runtime,
                    str(entry.source_kind),
                )
            )
            terms = Counter(_tokenize(weighted))
            length = sum(terms.values())
            documents[entry.qualified_name] = _IndexedSkill(entry, terms, length)
            frequency.update(terms.keys())
            total_length += length
        with self._lock:
            self._generation = generation
            self._documents = documents
            self._document_frequency = frequency
            self._average_length = total_length / len(documents) if documents else 1.0
        return generation

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        workspace_paths: Sequence[str] = (),
        source_filter: Sequence[str] = (),
        invocation_mode: str = "",
    ) -> SkillSearchResult:
        if self._generation != self.registry.generation:
            self.refresh(workspace_paths=workspace_paths)
        terms = tuple(dict.fromkeys(_tokenize(query)))
        if not terms:
            return SkillSearchResult(query=query, generation=self._generation, hits=())
        with self._lock:
            documents = tuple(self._documents.values())
            frequency = self._document_frequency.copy()
            average = self._average_length
            generation = self._generation
        candidates: list[SkillSearchHit] = []
        source_set = {value.lower() for value in source_filter}
        for document in documents:
            entry = document.entry
            if source_set and str(entry.source_kind).lower() not in source_set:
                continue
            if invocation_mode and str(entry.invocation_mode) != invocation_mode:
                continue
            score = 0.0
            matched: list[str] = []
            reasons: list[str] = []
            for term in terms:
                term_frequency = document.terms.get(term, 0)
                if term_frequency:
                    matched.append(term)
                    score += _bm25(
                        term_frequency,
                        frequency.get(term, 0),
                        document.document_length,
                        average,
                        max(1, len(documents)),
                    )
                if term == entry.name:
                    score += 5.0
                    reasons.append("exact_name")
                elif term in entry.name:
                    score += 2.0
                    reasons.append("partial_name")
            if not matched:
                continue
            coverage = len(set(matched)) / len(terms)
            score *= 0.5 + coverage
            if entry.source_kind.value == "builtin":
                score += 0.15
                reasons.append("product_owned")
            candidates.append(
                SkillSearchHit(
                    qualified_name=entry.qualified_name,
                    name=entry.name,
                    description=entry.description,
                    score=score,
                    matched_terms=tuple(sorted(set(matched))),
                    source=str(entry.source_kind),
                    invocation_mode=str(entry.invocation_mode),
                    content_digest=entry.content_digest,
                    reasons=tuple(dict.fromkeys(reasons)),
                )
            )
        candidates.sort(key=lambda hit: (-hit.score, hit.qualified_name))
        return SkillSearchResult(
            query=query,
            generation=generation,
            hits=tuple(candidates[: max(1, min(100, limit))]),
        )


class ConditionalSkillActivator:
    """Tracks path-conditioned availability without loading skill bodies."""

    def __init__(self, registry: SkillRegistry) -> None:
        self.registry = registry
        self._lock = RLock()
        self._activated: dict[tuple[str, str], set[str]] = defaultdict(set)

    def activate(
        self,
        *,
        session_id: str,
        agent_id: str,
        changed_paths: Sequence[str],
    ) -> tuple[str, ...]:
        snapshot = self.registry.snapshot()
        activated: list[str] = []
        for qualified_name, ref in snapshot.active_by_qualified_name.items():
            revision = snapshot.revisions_by_ref.get(ref)
            if revision is None or not revision.metadata.path_conditions:
                continue
            if any(
                fnmatch(path.replace("\\", "/"), pattern)
                for path in changed_paths
                for pattern in revision.metadata.path_conditions
            ):
                activated.append(qualified_name)
        key = (session_id, agent_id)
        with self._lock:
            previous = self._activated.setdefault(key, set())
            delta = tuple(sorted(set(activated) - previous))
            previous.update(activated)
            return delta

    def active(self, *, session_id: str, agent_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._activated.get((session_id, agent_id), set())))

    def clear_session(self, session_id: str) -> None:
        with self._lock:
            for key in [key for key in self._activated if key[0] == session_id]:
                self._activated.pop(key, None)


def _tokenize(value: str) -> list[str]:
    lowered = value.casefold().replace("_", "-")
    tokens = [match.group(0).strip(".-:") for match in TOKEN_PATTERN.finditer(lowered)]
    expanded: list[str] = []
    for token in tokens:
        if not token:
            continue
        expanded.append(token)
        expanded.extend(part for part in re.split(r"[-.:]", token) if len(part) > 1)
    return expanded


def _bm25(
    term_frequency: int,
    document_frequency: int,
    document_length: int,
    average_length: float,
    document_count: int,
    *,
    k1: float = 1.2,
    b: float = 0.75,
) -> float:
    inverse = math.log(1 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5))
    denominator = term_frequency + k1 * (1 - b + b * document_length / max(1.0, average_length))
    return inverse * (term_frequency * (k1 + 1)) / denominator
