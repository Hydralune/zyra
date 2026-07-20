from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol, Sequence

from .retrieval_models import RetrievalHit, VectorAvailability


class EmbeddingProvider(Protocol):
    @property
    def model_id(self) -> str:
        ...

    @property
    def dimensions(self) -> int:
        ...

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        ...


class VectorSearchAdapter(Protocol):
    @property
    def availability(self) -> VectorAvailability:
        ...

    @property
    def reason(self) -> str:
        ...

    def replace_scope(
        self,
        scope_key: str,
        generation: int,
        documents: Sequence[RetrievalHit],
    ) -> None:
        ...

    def search(
        self,
        scope_key: str,
        generation: int,
        query: str,
        *,
        limit: int,
    ) -> Sequence[RetrievalHit]:
        ...

    def clear_scope(self, scope_key: str) -> None:
        ...


@dataclass(frozen=True, slots=True)
class VectorAdapterStatus:
    availability: VectorAvailability
    reason: str
    model_id: str = ""
    dimensions: int = 0
    document_count: int = 0
    generation_count: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "availability": self.availability.value,
            "reason": self.reason,
            "model_id": self.model_id,
            "dimensions": self.dimensions,
            "document_count": self.document_count,
            "generation_count": self.generation_count,
            "metadata": dict(self.metadata),
        }


class UnavailableVectorAdapter:
    """Explicit no-vector implementation.

    The adapter never downloads a model, probes the network, imports optional
    native extensions, or silently substitutes lexical ranking.  Callers still
    execute the FTS voice and return this adapter's reason as degradation
    provenance.
    """

    def __init__(self, reason: str = "no vector provider configured") -> None:
        self._reason = reason

    @property
    def availability(self) -> VectorAvailability:
        return VectorAvailability.UNAVAILABLE

    @property
    def reason(self) -> str:
        return self._reason

    def replace_scope(
        self,
        scope_key: str,
        generation: int,
        documents: Sequence[RetrievalHit],
    ) -> None:
        del scope_key, generation, documents

    def search(
        self,
        scope_key: str,
        generation: int,
        query: str,
        *,
        limit: int,
    ) -> Sequence[RetrievalHit]:
        del scope_key, generation, query, limit
        return ()

    def clear_scope(self, scope_key: str) -> None:
        del scope_key

    def status(self) -> VectorAdapterStatus:
        return VectorAdapterStatus(availability=self.availability, reason=self.reason)


class DisabledVectorAdapter(UnavailableVectorAdapter):
    @property
    def availability(self) -> VectorAvailability:
        return VectorAvailability.DISABLED


@dataclass(frozen=True, slots=True)
class _VectorEntry:
    hit: RetrievalHit
    vector: tuple[float, ...]


def _normalize_vector(values: Sequence[float], *, dimensions: int = 0) -> tuple[float, ...] | None:
    if not values:
        return None
    if dimensions and len(values) != dimensions:
        return None
    norm_square = 0.0
    output: list[float] = []
    for raw in values:
        value = float(raw)
        if not math.isfinite(value):
            return None
        output.append(value)
        norm_square += value * value
    if norm_square <= 0.0:
        return None
    norm = math.sqrt(norm_square)
    return tuple(value / norm for value in output)


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) != len(right):
        return float("-inf")
    return sum(a * b for a, b in zip(left, right, strict=True))


class ExactVectorAdapter:
    """Deterministic exact-cosine boundary for an injected embedding provider.

    The corpus is generation-scoped and stored in memory by design.  It is an
    adapter/conformance implementation, not the canonical memory store and not
    a durable index owner.  Reopening a process simply degrades to FTS until a
    generation is rebuilt.  This keeps the foundation dependency-free while
    preserving an honest adapter contract for a future ANN backend.
    """

    def __init__(self, provider: EmbeddingProvider) -> None:
        if provider.dimensions <= 0:
            raise ValueError("embedding provider dimensions must be positive")
        self.provider = provider
        self._entries: dict[tuple[str, int], tuple[_VectorEntry, ...]] = {}
        self._reason = ""
        self._availability = VectorAvailability.AVAILABLE
        self._guard = threading.RLock()

    @property
    def availability(self) -> VectorAvailability:
        return self._availability

    @property
    def reason(self) -> str:
        return self._reason

    def replace_scope(
        self,
        scope_key: str,
        generation: int,
        documents: Sequence[RetrievalHit],
    ) -> None:
        if generation < 1:
            raise ValueError("generation must be positive")
        texts = [f"{item.title}\n{item.content}" for item in documents]
        try:
            raw_vectors = self.provider.embed(texts)
        except Exception as error:  # noqa: BLE001 - adapter failure must degrade without hiding FTS.
            self._degrade(f"embedding provider failed: {type(error).__name__}: {error}")
            return
        if len(raw_vectors) != len(documents):
            self._degrade(
                f"embedding count mismatch: expected {len(documents)}, received {len(raw_vectors)}"
            )
            return
        entries: list[_VectorEntry] = []
        for hit, vector in zip(documents, raw_vectors, strict=True):
            normalized = _normalize_vector(vector, dimensions=self.provider.dimensions)
            if normalized is None:
                self._degrade(f"invalid vector for document {hit.document_id}")
                return
            entries.append(_VectorEntry(hit=hit, vector=normalized))
        entries.sort(key=lambda item: item.hit.document_id)
        with self._guard:
            self._entries[(scope_key, generation)] = tuple(entries)
            for key in tuple(self._entries):
                if key[0] == scope_key and key[1] != generation:
                    del self._entries[key]
            self._availability = VectorAvailability.AVAILABLE
            self._reason = ""

    def search(
        self,
        scope_key: str,
        generation: int,
        query: str,
        *,
        limit: int,
    ) -> Sequence[RetrievalHit]:
        if limit <= 0 or not query.strip() or self.availability is not VectorAvailability.AVAILABLE:
            return ()
        with self._guard:
            entries = self._entries.get((scope_key, generation), ())
        if not entries:
            self._degrade("active generation has no in-process vector corpus")
            return ()
        try:
            raw = self.provider.embed([query])
        except Exception as error:  # noqa: BLE001
            self._degrade(f"query embedding failed: {type(error).__name__}: {error}")
            return ()
        if len(raw) != 1:
            self._degrade("query embedding provider did not return exactly one vector")
            return ()
        query_vector = _normalize_vector(raw[0], dimensions=self.provider.dimensions)
        if query_vector is None:
            self._degrade("query embedding is empty, non-finite, zero, or dimension-mismatched")
            return ()
        scored: list[RetrievalHit] = []
        for entry in entries:
            cosine = _dot(query_vector, entry.vector)
            if not math.isfinite(cosine):
                continue
            scored.append(entry.hit.with_score((cosine + 1.0) / 2.0))
        scored.sort(key=lambda item: (-item.score, item.document_id))
        return tuple(scored[: max(0, int(limit))])

    def clear_scope(self, scope_key: str) -> None:
        with self._guard:
            for key in tuple(self._entries):
                if key[0] == scope_key:
                    del self._entries[key]

    def status(self) -> VectorAdapterStatus:
        with self._guard:
            generations = len(self._entries)
            count = sum(len(items) for items in self._entries.values())
        return VectorAdapterStatus(
            availability=self.availability,
            reason=self.reason,
            model_id=self.provider.model_id,
            dimensions=self.provider.dimensions,
            document_count=count,
            generation_count=generations,
            metadata={
                "algorithm": "exact_cosine",
                "durability": "rebuildable_process_local_adapter",
                "network_download": False,
            },
        )

    def _degrade(self, reason: str) -> None:
        with self._guard:
            self._availability = VectorAvailability.DEGRADED
            self._reason = reason[:500]


class CallableEmbeddingProvider:
    def __init__(
        self,
        embedder: Callable[[Sequence[str]], Sequence[Sequence[float]]],
        *,
        model_id: str,
        dimensions: int,
    ) -> None:
        if not model_id.strip():
            raise ValueError("model_id is required")
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self._embedder = embedder
        self._model_id = model_id
        self._dimensions = dimensions

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed(self, texts: Sequence[str]) -> Sequence[Sequence[float]]:
        return self._embedder(tuple(str(item) for item in texts))
