from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Iterable

from .errors import SkillAmbiguous
from .models import SkillRevision, SkillSourceKind, SkillTombstone


SOURCE_PRECEDENCE = {
    SkillSourceKind.MANAGED: 700,
    SkillSourceKind.BUILTIN: 600,
    SkillSourceKind.USER: 500,
    SkillSourceKind.PROJECT: 400,
    SkillSourceKind.ADD_DIR: 300,
    SkillSourceKind.PLUGIN: 200,
    SkillSourceKind.MCP: 100,
}


@dataclass(frozen=True, slots=True)
class PrecedenceResult:
    active_by_qualified_name: dict[str, str]
    aliases: dict[str, str]
    shadowed: dict[str, tuple[str, ...]]
    conflicts: tuple[dict[str, object], ...]


def resolve_precedence(
    revisions: Iterable[SkillRevision],
    *,
    tombstones: dict[str, SkillTombstone] | None = None,
) -> PrecedenceResult:
    grouped: dict[str, list[SkillRevision]] = defaultdict(list)
    by_qualified: dict[str, list[SkillRevision]] = defaultdict(list)
    for revision in revisions:
        grouped[revision.metadata.name].append(revision)
        by_qualified[revision.qualified_name].append(revision)
    active_by_qualified: dict[str, str] = {}
    conflicts: list[dict[str, object]] = []
    for qualified_name, candidates in by_qualified.items():
        ordered = sorted(candidates, key=_sort_key, reverse=True)
        if len(ordered) > 1 and _sort_key(ordered[0]) == _sort_key(ordered[1]):
            conflicts.append(
                {
                    "kind": "qualified_name_conflict",
                    "name": qualified_name,
                    "sources": [item.provenance.source_id for item in ordered],
                }
            )
            continue
        active_by_qualified[qualified_name] = ordered[0].version_ref.immutable_ref
    aliases: dict[str, str] = {}
    shadowed: dict[str, tuple[str, ...]] = {}
    tombstone_map = dict(tombstones or {})
    for name, candidates in grouped.items():
        tombstone = tombstone_map.get(name)
        ordered = sorted(candidates, key=_sort_key, reverse=True)
        visible = [item for item in ordered if item.qualified_name in active_by_qualified]
        if tombstone is not None:
            tombstone_rank = SOURCE_PRECEDENCE[tombstone.source_kind]
            visible = [item for item in visible if _rank(item) > tombstone_rank]
            if not visible:
                shadowed[name] = tuple(item.version_ref.immutable_ref for item in ordered)
                continue
        aliasable = [item for item in visible if item.provenance.source_kind not in {SkillSourceKind.PLUGIN, SkillSourceKind.MCP}]
        if not aliasable:
            shadowed[name] = tuple(item.version_ref.immutable_ref for item in visible)
            continue
        winner = aliasable[0]
        if len(aliasable) > 1 and _sort_key(winner) == _sort_key(aliasable[1]):
            conflicts.append(
                {
                    "kind": "alias_conflict",
                    "name": name,
                    "sources": [item.provenance.source_id for item in aliasable],
                }
            )
            shadowed[name] = tuple(item.version_ref.immutable_ref for item in ordered)
            continue
        aliases[name] = winner.qualified_name
        shadowed[name] = tuple(
            item.version_ref.immutable_ref
            for item in ordered
            if item.version_ref.immutable_ref != winner.version_ref.immutable_ref
        )
    return PrecedenceResult(
        active_by_qualified_name=active_by_qualified,
        aliases=aliases,
        shadowed=shadowed,
        conflicts=tuple(conflicts),
    )


def require_no_conflicts(result: PrecedenceResult) -> None:
    if result.conflicts:
        raise SkillAmbiguous("skill source precedence contains equal-rank conflicts", detail={"conflicts": list(result.conflicts)})


def _rank(revision: SkillRevision) -> int:
    return max(SOURCE_PRECEDENCE[revision.provenance.source_kind], revision.provenance.source_rank)


def _sort_key(revision: SkillRevision) -> tuple[int, int, int, str]:
    # Project skills closer to cwd win. Add-dir order is explicit: lower
    # configured_order wins. Deterministic source id is only a stable tiebreak
    # after semantic precedence; equal semantic keys remain conflict candidates.
    project_closeness = -revision.provenance.project_distance
    configured = -revision.provenance.configured_order
    return (_rank(revision), project_closeness, configured, revision.provenance.source_id)
