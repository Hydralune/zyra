from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .digests import estimate_tokens, normalize_text, sha256_text
from .errors import (
    SkillBodyLoaderDisabled,
    SkillBudgetExceeded,
    SkillRevisionMismatch,
    SkillRevoked,
)
from .frontmatter import parse_skill_document
from .models import SkillBody, SkillRevision, SkillVersionRef
from .path_security import read_verified_text
from .revision_store import SkillRevisionStore


class SkillBodyResourceLoader:
    """Lazy body and resource IO bound to immutable revisions."""

    def __init__(self, revision_store: SkillRevisionStore, *, disabled: bool = False) -> None:
        self.revision_store = revision_store
        self.disabled = disabled
        self._body_load_count = 0

    @property
    def body_load_count(self) -> int:
        return self._body_load_count

    def load_body(
        self,
        revision_or_ref: SkillRevision | SkillVersionRef,
        *,
        token_budget: int | None = None,
        allow_truncation: bool = False,
    ) -> SkillBody:
        self._ensure_enabled()
        revision = self._resolve(revision_or_ref)
        status = self.revision_store.status(revision.version_ref)
        if str(status.lifecycle) == "revoked":
            raise SkillRevoked("skill body cannot be loaded after revocation")
        raw = read_verified_text(
            revision.skill_file,
            root=revision.skill_root,
            max_bytes=max(2_000_000, revision.body.size_bytes + 64_000),
        )
        document = parse_skill_document(raw, expected_name=revision.metadata.name)
        body = normalize_text(document.body)
        digest = sha256_text(body)
        if digest != revision.body.digest or digest != revision.version_ref.body_digest:
            raise SkillRevisionMismatch(
                "skill body changed after revision validation",
                detail={"expected": revision.body.digest, "actual": digest},
            )
        estimated = estimate_tokens(body)
        budget = int(token_budget or revision.metadata.context_budget.body_tokens)
        truncated = False
        if estimated > budget:
            if not allow_truncation:
                raise SkillBudgetExceeded(
                    "skill body exceeds context token budget",
                    detail={"estimated_tokens": estimated, "budget": budget},
                )
            body = _truncate_to_tokens(body, budget)
            estimated = estimate_tokens(body)
            truncated = True
        self._body_load_count += 1
        return SkillBody(
            version_ref=replace(revision.version_ref, revocation_epoch=status.revocation_epoch),
            text=body,
            token_estimate=estimated,
            truncated=truncated,
        )

    def restore_excerpt(
        self,
        version_ref: SkillVersionRef,
        *,
        token_budget: int | None = None,
    ) -> SkillBody:
        revision = self.revision_store.get(version_ref)
        budget = min(
            int(token_budget or revision.metadata.context_budget.restore_tokens),
            revision.metadata.context_budget.restore_tokens,
            5_000,
        )
        return self.load_body(revision, token_budget=budget, allow_truncation=True)

    def _resolve(self, value: SkillRevision | SkillVersionRef) -> SkillRevision:
        if isinstance(value, SkillRevision):
            stored = self.revision_store.get(value.version_ref)
            if stored.version_ref.content_digest != value.version_ref.content_digest:
                raise SkillRevisionMismatch("provided skill revision does not match revision store")
            return stored
        return self.revision_store.get(value)

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise SkillBodyLoaderDisabled("SkillBodyResourceLoader is disabled")


def _truncate_to_tokens(text: str, token_budget: int) -> str:
    byte_budget = max(1, token_budget * 4)
    payload = text.encode("utf-8")
    if len(payload) <= byte_budget:
        return text
    clipped = payload[:byte_budget]
    while clipped:
        try:
            result = clipped.decode("utf-8")
            break
        except UnicodeDecodeError:
            clipped = clipped[:-1]
    else:
        result = ""
    return result.rstrip() + "\n\n[skill body truncated to restore/context budget]\n"
