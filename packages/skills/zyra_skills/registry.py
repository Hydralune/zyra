from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Sequence

from .errors import (
    SkillAmbiguous,
    SkillDisabled,
    SkillNotFound,
    SkillRegistryDisabled,
    SkillReloadRejected,
    SkillRevoked,
)
from .models import (
    SkillDeclaredMetadata,
    SkillLifecycle,
    SkillListingEntry,
    SkillRegistrySnapshot,
    SkillRevision,
    SkillSourceKind,
    SkillTombstone,
    SkillVersionRef,
)
from .precedence import require_no_conflicts, resolve_precedence
from .revision_store import SkillRevisionStore
from .sources.base import SkillSource, SkillSourceScan


@dataclass(frozen=True, slots=True)
class SkillRegistryReloadResult:
    applied: bool
    previous_generation: int
    generation: int
    snapshot_id: str
    scans: tuple[SkillSourceScan, ...]
    changed_refs: tuple[str, ...]
    removed_refs: tuple[str, ...]
    errors: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "previous_generation": self.previous_generation,
            "generation": self.generation,
            "snapshot_id": self.snapshot_id,
            "scans": [scan.to_dict() for scan in self.scans],
            "changed_refs": list(self.changed_refs),
            "removed_refs": list(self.removed_refs),
            "errors": [dict(item) for item in self.errors],
        }


class SkillRegistry:
    """Atomic, versioned skill discovery registry.

    Reads are lock-bounded snapshot pointer reads. Reload scans and validates
    outside the swap lock, then publishes a complete generation in one step.
    A rejected source scan preserves the last-good snapshot without partial
    publication.
    """

    def __init__(
        self,
        skills: Iterable[Any] | None = None,
        *,
        sources: Sequence[SkillSource] = (),
        revision_store: SkillRevisionStore | None = None,
        disabled: bool = False,
    ) -> None:
        self._sources = list(sources)
        self.revision_store = revision_store or SkillRevisionStore()
        self.disabled = disabled
        self._lock = RLock()
        self._snapshot = SkillRegistrySnapshot.empty()
        self._tombstones: dict[str, SkillTombstone] = {}
        self._legacy_specs: dict[str, Any] = {}
        # Compatibility input is intentionally metadata-only. The production
        # default never uses this path; it keeps older callers from crashing
        # while removing vendor_paths as runtime authority.
        for item in skills or ():
            name = str(getattr(item, "name", "") or "")
            if name:
                self._legacy_specs[name] = item

    @property
    def generation(self) -> int:
        return self.snapshot().generation

    def configure_sources(self, sources: Sequence[SkillSource]) -> None:
        self._ensure_enabled()
        with self._lock:
            self._sources = list(sources)

    def sources(self) -> tuple[SkillSource, ...]:
        with self._lock:
            return tuple(self._sources)

    def snapshot(self) -> SkillRegistrySnapshot:
        self._ensure_enabled()
        with self._lock:
            return self._snapshot

    def reload(self, *, expected_generation: int | None = None) -> SkillRegistryReloadResult:
        self._ensure_enabled()
        previous = self.snapshot()
        if expected_generation is not None and previous.generation != expected_generation:
            raise SkillReloadRejected(
                "skill registry generation changed before reload",
                detail={"expected": expected_generation, "actual": previous.generation},
            )
        next_generation = previous.generation + 1
        scans = tuple(source.scan(generation=next_generation) for source in self.sources())
        errors = tuple(error for scan in scans for error in scan.errors)
        rejected = [scan for scan in scans if not scan.ok and str(scan.state) != "disabled"]
        if rejected:
            return SkillRegistryReloadResult(
                applied=False,
                previous_generation=previous.generation,
                generation=previous.generation,
                snapshot_id=previous.snapshot_id,
                scans=scans,
                changed_refs=(),
                removed_refs=(),
                errors=errors,
            )
        candidates = [revision for scan in scans if scan.ok for revision in scan.revisions]
        precedence = resolve_precedence(candidates, tombstones=self._tombstones)
        require_no_conflicts(precedence)
        active_refs = set(precedence.active_by_qualified_name.values())
        source_status = {
            scan.source_id: {
                "kind": str(scan.source_kind),
                "state": str(scan.state),
                "metadata": dict(scan.metadata),
                "errors": [dict(item) for item in scan.errors],
                "scanned_at": scan.scanned_at,
            }
            for scan in scans
        }
        with self._lock:
            if self._snapshot.snapshot_id != previous.snapshot_id:
                raise SkillReloadRejected("concurrent skill reload won the generation swap")
            staged = self.revision_store.commit_generation(
                candidates,
                active_refs=active_refs,
            )
            revisions_by_ref = {revision.version_ref.immutable_ref: revision for revision in staged}
            candidate_snapshot = SkillRegistrySnapshot(
                generation=next_generation,
                revisions_by_ref=revisions_by_ref,
                active_by_qualified_name=dict(precedence.active_by_qualified_name),
                aliases=dict(precedence.aliases),
                shadowed=dict(precedence.shadowed),
                tombstones=dict(self._tombstones),
                source_status=source_status,
            )
            self._snapshot = candidate_snapshot
        old_refs = set(previous.active_by_qualified_name.values())
        return SkillRegistryReloadResult(
            applied=True,
            previous_generation=previous.generation,
            generation=candidate_snapshot.generation,
            snapshot_id=candidate_snapshot.snapshot_id,
            scans=scans,
            changed_refs=tuple(sorted(active_refs - old_refs)),
            removed_refs=tuple(sorted(old_refs - active_refs)),
            errors=errors,
        )

    def get(self, name: str) -> SkillRevision | Any | None:
        try:
            return self.resolve(name)
        except (SkillNotFound, SkillDisabled, SkillRevoked, SkillAmbiguous):
            return self._legacy_specs.get(name)

    def resolve(
        self,
        name: str,
        *,
        requested_ref: SkillVersionRef | None = None,
        workspace_paths: Sequence[str] = (),
        require_model_invocable: bool = False,
        require_user_invocable: bool = False,
    ) -> SkillRevision:
        self._ensure_enabled()
        snapshot = self.snapshot()
        if requested_ref is not None:
            revision = self.revision_store.get(requested_ref)
            if revision.version_ref.content_digest != requested_ref.content_digest:
                raise SkillRevoked("requested skill revision digest no longer matches")
            tombstone = snapshot.tombstones.get(revision.metadata.name)
            if tombstone:
                raise SkillDisabled(
                    "requested skill revision is blocked by a tombstone",
                    detail={"name": revision.metadata.name, "reason": tombstone.reason},
                )
        else:
            selected = str(name or "").strip().removeprefix("/")
            qualified = selected if selected in snapshot.active_by_qualified_name else snapshot.aliases.get(selected, "")
            selected_name = selected.split(":", 1)[-1]
            selected_tombstone = snapshot.tombstones.get(selected_name)
            if selected_tombstone:
                raise SkillDisabled(
                    "skill name is blocked by a higher-precedence tombstone",
                    detail={"name": selected_name, "reason": selected_tombstone.reason},
                )
            if not qualified:
                tombstone = snapshot.tombstones.get(selected)
                if tombstone:
                    raise SkillDisabled("skill name is blocked by a higher-precedence tombstone", detail={"name": selected, "reason": tombstone.reason})
                namespaced = [key for key in snapshot.active_by_qualified_name if key.endswith(f":{selected}")]
                if len(namespaced) > 1:
                    raise SkillAmbiguous("skill name is ambiguous; use a qualified name", detail={"name": selected, "candidates": namespaced})
                raise SkillNotFound("skill was not found", detail={"name": selected})
            ref = snapshot.active_by_qualified_name[qualified]
            revision = self.revision_store.get(ref, allow_superseded=False)
        if require_model_invocable and not revision.metadata.model_invocable:
            raise SkillDisabled("skill does not permit model invocation", detail={"name": revision.qualified_name})
        if require_user_invocable and not revision.metadata.user_invocable:
            raise SkillDisabled("skill does not permit user invocation", detail={"name": revision.qualified_name})
        if revision.metadata.path_conditions and not _paths_match(revision.metadata.path_conditions, workspace_paths):
            raise SkillDisabled("skill path conditions are not active", detail={"name": revision.qualified_name})
        return revision

    def list(
        self,
        *,
        workspace_paths: Sequence[str] = (),
        include_namespaced: bool = True,
        for_model: bool = False,
        for_user: bool = False,
    ) -> list[SkillListingEntry | Any]:
        self._ensure_enabled()
        snapshot = self.snapshot()
        entries: list[SkillListingEntry] = []
        seen_refs: set[str] = set()
        for qualified_name, ref in sorted(snapshot.active_by_qualified_name.items()):
            revision = self.revision_store.get(ref, allow_superseded=False)
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            metadata = revision.metadata
            if for_model and not metadata.model_invocable:
                continue
            if for_user and not metadata.user_invocable:
                continue
            if metadata.path_conditions and not _paths_match(metadata.path_conditions, workspace_paths):
                continue
            if not include_namespaced and metadata.name not in snapshot.aliases:
                continue
            entries.append(
                SkillListingEntry(
                    name=metadata.name,
                    qualified_name=qualified_name,
                    description=metadata.description,
                    when_to_use=metadata.when_to_use,
                    source_kind=revision.provenance.source_kind,
                    source_namespace=revision.provenance.source_namespace,
                    invocation_mode=metadata.invocation.mode,
                    preferred_runtime=metadata.preferred_runtime,
                    declared_version=metadata.declared_version,
                    content_digest=revision.version_ref.content_digest,
                    token_estimate=metadata.context_budget.listing_tokens,
                    user_invocable=metadata.user_invocable,
                    model_invocable=metadata.model_invocable,
                    lifecycle=SkillLifecycle.ACTIVE,
                    shadowed_count=len(snapshot.shadowed.get(metadata.name, ())),
                )
            )
        if not entries and self._legacy_specs:
            return list(self._legacy_specs.values())
        return entries

    def disable(
        self,
        name: str,
        *,
        source_kind: SkillSourceKind = SkillSourceKind.MANAGED,
        source_id: str = "managed-policy",
        reason: str,
    ) -> SkillTombstone:
        self._ensure_enabled()
        canonical_name = str(name or "").strip().removeprefix("/").split(":", 1)[-1]
        if not canonical_name:
            raise ValueError("skill tombstone name must not be empty")
        snapshot = self.snapshot()
        existing = self._tombstones.get(canonical_name)
        epoch = (existing.revocation_epoch if existing else 0) + 1
        tombstone = SkillTombstone(
            name=canonical_name,
            source_kind=source_kind,
            source_id=source_id,
            reason=reason,
            revocation_epoch=epoch,
        )
        with self._lock:
            self._tombstones[canonical_name] = tombstone
            remaining_active = {
                qualified: ref
                for qualified, ref in snapshot.active_by_qualified_name.items()
                if snapshot.revisions_by_ref[ref].metadata.name != canonical_name
            }
            self._snapshot = SkillRegistrySnapshot(
                generation=snapshot.generation + 1,
                revisions_by_ref=dict(snapshot.revisions_by_ref),
                active_by_qualified_name=remaining_active,
                aliases={key: value for key, value in snapshot.aliases.items() if key != canonical_name},
                shadowed=dict(snapshot.shadowed),
                tombstones=dict(self._tombstones),
                source_status=dict(snapshot.source_status),
            )
        return tombstone

    def revoke(self, name: str, *, reason: str) -> SkillVersionRef:
        revision = self.resolve(name)
        self.revision_store.revoke(revision.version_ref, reason=reason)
        self.disable(
            revision.metadata.name,
            source_kind=revision.provenance.source_kind,
            source_id=revision.provenance.source_id,
            reason=reason,
        )
        return revision.version_ref

    def _ensure_enabled(self) -> None:
        if self.disabled:
            raise SkillRegistryDisabled("SkillRegistry is disabled")


# Compatibility alias. New callers should use SkillDeclaredMetadata and
# SkillRevision, but old imports continue to resolve without vendor paths.
SkillSpec = SkillDeclaredMetadata


def _paths_match(patterns: Sequence[str], paths: Sequence[str]) -> bool:
    if not patterns:
        return True
    normalized = [str(Path(path)).replace("\\", "/") for path in paths]
    return any(fnmatch(path, pattern) for pattern in patterns for path in normalized)
