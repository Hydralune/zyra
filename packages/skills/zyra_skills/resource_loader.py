from __future__ import annotations

import json
from pathlib import Path
from threading import RLock
from typing import Iterable

from .digests import estimate_tokens, sha256_bytes
from .errors import (
    SkillBudgetExceeded,
    SkillResourceNotFound,
    SkillResourceTypeError,
    SkillRevisionMismatch,
)
from .models import LoadedSkillResource, SkillResourceKind, SkillRevision, SkillVersionRef
from .path_security import read_verified_bytes, resolve_member
from .revision_store import SkillRevisionStore


class SkillResourceLoader:
    def __init__(self, revision_store: SkillRevisionStore) -> None:
        self.revision_store = revision_store
        self._lock = RLock()
        self._load_count = 0

    @property
    def load_count(self) -> int:
        with self._lock:
            return self._load_count

    def list_resources(self, revision_or_ref: SkillRevision | SkillVersionRef) -> tuple[dict[str, object], ...]:
        revision = self._resolve(revision_or_ref)
        return tuple(item.to_dict() for item in revision.resources)

    def load(
        self,
        revision_or_ref: SkillRevision | SkillVersionRef,
        relative_path: str,
        *,
        token_budget: int | None = None,
        allow_truncation: bool = False,
    ) -> LoadedSkillResource:
        revision = self._resolve(revision_or_ref)
        descriptor = next((item for item in revision.resources if item.relative_path == relative_path), None)
        if descriptor is None:
            raise SkillResourceNotFound("resource is not declared by the skill revision", detail={"path": relative_path})
        path = resolve_member(revision.skill_root, descriptor.relative_path)
        payload = read_verified_bytes(
            path,
            root=revision.skill_root,
            max_bytes=revision.metadata.context_budget.max_resource_bytes,
        )
        digest = sha256_bytes(payload)
        if digest != descriptor.digest:
            raise SkillRevisionMismatch(
                "skill resource changed after revision validation",
                detail={"path": relative_path, "expected": descriptor.digest, "actual": digest},
            )
        content: str | bytes
        if descriptor.kind in {
            SkillResourceKind.TEXT,
            SkillResourceKind.JSON,
            SkillResourceKind.SCHEMA,
            SkillResourceKind.TEMPLATE,
            SkillResourceKind.REFERENCE,
        }:
            try:
                content = payload.decode("utf-8")
            except UnicodeDecodeError as error:
                raise SkillResourceTypeError("declared text skill resource is not UTF-8", detail={"path": relative_path}) from error
            if descriptor.kind in {SkillResourceKind.JSON, SkillResourceKind.SCHEMA}:
                try:
                    json.loads(content)
                except json.JSONDecodeError as error:
                    raise SkillResourceTypeError("declared JSON skill resource is invalid", detail={"path": relative_path}) from error
        else:
            content = payload
        estimated = estimate_tokens(content)
        budget = min(
            int(token_budget or revision.metadata.context_budget.resource_read_tokens),
            revision.metadata.context_budget.resource_read_tokens,
        )
        truncated = False
        if estimated > budget:
            if not allow_truncation or isinstance(content, bytes):
                raise SkillBudgetExceeded(
                    "skill resource exceeds token budget",
                    detail={"path": relative_path, "estimated_tokens": estimated, "budget": budget},
                )
            content = _truncate_text(content, budget)
            estimated = estimate_tokens(content)
            truncated = True
        with self._lock:
            self._load_count += 1
        return LoadedSkillResource(
            version_ref=revision.version_ref,
            descriptor=descriptor,
            content=content,
            token_estimate=estimated,
            truncated=truncated,
        )

    def load_many(
        self,
        revision_or_ref: SkillRevision | SkillVersionRef,
        paths: Iterable[str],
        *,
        total_token_budget: int | None = None,
    ) -> tuple[LoadedSkillResource, ...]:
        revision = self._resolve(revision_or_ref)
        remaining = int(total_token_budget or revision.metadata.context_budget.invocation_total_tokens)
        loaded: list[LoadedSkillResource] = []
        for path in paths:
            item = self.load(
                revision,
                path,
                token_budget=min(remaining, revision.metadata.context_budget.resource_read_tokens),
            )
            loaded.append(item)
            remaining -= item.token_estimate
            if remaining < 0:
                raise SkillBudgetExceeded("skill resources exceed invocation total token budget")
        return tuple(loaded)

    def _resolve(self, value: SkillRevision | SkillVersionRef) -> SkillRevision:
        return self.revision_store.get(value.version_ref if isinstance(value, SkillRevision) else value)


def _truncate_text(text: str, token_budget: int) -> str:
    limit = max(1, token_budget * 4)
    data = text.encode("utf-8")[:limit]
    while data:
        try:
            return data.decode("utf-8").rstrip() + "\n[resource truncated]\n"
        except UnicodeDecodeError:
            data = data[:-1]
    return "[resource truncated]\n"
