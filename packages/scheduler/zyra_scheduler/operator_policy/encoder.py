from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from zyra_orchestration.topology_policy.contracts import canonical_digest

from .catalog import OperatorProfile


ENCODER_PROFILE = "zyra_deterministic_operator_semantic_v1"
_TERM_PATTERN = re.compile(r"[\w\u4e00-\u9fff][\w\-.:/\u4e00-\u9fff]*")


def semantic_terms(*values: Any) -> tuple[str, ...]:
    terms: set[str] = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, str):
            selected = (value,)
        elif isinstance(value, dict):
            selected = (*value.keys(), *value.values())
        else:
            try:
                selected = tuple(value)
            except TypeError:
                selected = (value,)
        for item in selected:
            rendered = str(item or "").lower().replace("_", " ")
            for term in _TERM_PATTERN.findall(rendered):
                normalized = term.strip("-.:/")
                if len(normalized) >= 2:
                    terms.add(normalized)
    return tuple(sorted(terms))


@dataclass(frozen=True, slots=True)
class OperatorSelectionContext:
    query: str
    phase: str
    requirement_revision: str
    roles: tuple[str, ...]
    desired_capabilities: tuple[str, ...]
    obligations: tuple[str, ...]
    privacy_class: str
    allowed_locations: tuple[str, ...]
    remaining_tokens: int
    remaining_cost_usd: float
    remaining_time_ms: int
    topology_commit_id: str
    topology_signature: str

    @property
    def terms(self) -> tuple[str, ...]:
        return semantic_terms(
            self.query,
            self.phase,
            self.requirement_revision,
            self.roles,
            self.desired_capabilities,
            self.obligations,
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "phase": self.phase,
            "requirement_revision": self.requirement_revision,
            "roles": list(self.roles),
            "desired_capabilities": list(self.desired_capabilities),
            "obligations": list(self.obligations),
            "privacy_class": self.privacy_class,
            "allowed_locations": list(self.allowed_locations),
            "remaining_tokens": self.remaining_tokens,
            "remaining_cost_usd": self.remaining_cost_usd,
            "remaining_time_ms": self.remaining_time_ms,
            "topology_commit_id": self.topology_commit_id,
            "topology_signature": self.topology_signature,
        }


@dataclass(frozen=True, slots=True)
class OperatorEncoding:
    operator_id: str
    profile: str
    query_terms: tuple[str, ...]
    operator_terms: tuple[str, ...]
    shared_terms: tuple[str, ...]
    semantic_score_basis_points: int
    observation_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "operator_id": self.operator_id,
            "profile": self.profile,
            "query_terms": list(self.query_terms),
            "operator_terms": list(self.operator_terms),
            "shared_terms": list(self.shared_terms),
            "semantic_score_basis_points": self.semantic_score_basis_points,
            "observation_digest": self.observation_digest,
        }


class DeterministicOperatorEncoder:
    """Auditable lexical feature encoder; it has no model weights or training path."""

    profile = ENCODER_PROFILE

    def encode(
        self,
        context: OperatorSelectionContext,
        profile: OperatorProfile,
    ) -> OperatorEncoding:
        query_terms = context.terms
        operator_terms = semantic_terms(
            profile.operator_id,
            profile.display_name,
            profile.description,
            profile.capabilities,
            profile.input_contract,
            profile.output_contract,
            profile.verifier_contracts,
        )
        shared = tuple(sorted(set(query_terms).intersection(operator_terms)))
        union = set(query_terms).union(operator_terms)
        semantic_score = 0 if not union else round(10_000 * len(shared) / len(union))
        digest = canonical_digest(
            {
                "profile": self.profile,
                "context_digest": context.digest,
                "operator_digest": profile.digest,
                "query_terms": list(query_terms),
                "operator_terms": list(operator_terms),
                "shared_terms": list(shared),
                "semantic_score_basis_points": semantic_score,
            }
        )
        return OperatorEncoding(
            operator_id=profile.operator_id,
            profile=self.profile,
            query_terms=query_terms,
            operator_terms=operator_terms,
            shared_terms=shared,
            semantic_score_basis_points=semantic_score,
            observation_digest=digest,
        )
