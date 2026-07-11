from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import SkillSourceError
from ..models import SkillProvenance, SkillSourceKind, SkillTrustTier
from ..path_security import canonical_root, iter_skill_directories
from ..revision_builder import build_revision
from .base import SkillSourceScan, SkillSourceState, rejected_scan


SOURCE_RANKS = {
    SkillSourceKind.MANAGED: 700,
    SkillSourceKind.BUILTIN: 600,
    SkillSourceKind.USER: 500,
    SkillSourceKind.PROJECT: 400,
    SkillSourceKind.ADD_DIR: 300,
    SkillSourceKind.PLUGIN: 200,
    SkillSourceKind.MCP: 100,
}

TRUST_BY_SOURCE = {
    SkillSourceKind.MANAGED: SkillTrustTier.MANAGED,
    SkillSourceKind.BUILTIN: SkillTrustTier.PRODUCT,
    SkillSourceKind.USER: SkillTrustTier.USER,
    SkillSourceKind.PROJECT: SkillTrustTier.PROJECT,
    SkillSourceKind.ADD_DIR: SkillTrustTier.PROJECT,
    SkillSourceKind.PLUGIN: SkillTrustTier.THIRD_PARTY,
    SkillSourceKind.MCP: SkillTrustTier.REMOTE,
}


@dataclass(slots=True)
class FilesystemSkillSource:
    root: Path
    source_kind: SkillSourceKind
    source_id: str
    namespace: str = ""
    enabled: bool = True
    configured_order: int = 0
    project_distance: int = 0
    max_depth: int = 8
    strict: bool = True
    origin_uri: str = ""

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        if not self.source_id.strip():
            raise ValueError("filesystem skill source requires source_id")
        if self.source_kind in {SkillSourceKind.PLUGIN, SkillSourceKind.MCP} and not self.namespace:
            raise ValueError("plugin and MCP source require a namespace")

    def scan(self, *, generation: int) -> SkillSourceScan:
        if not self.enabled:
            return SkillSourceScan(
                source_id=self.source_id,
                source_kind=self.source_kind,
                state=SkillSourceState.DISABLED,
                revisions=(),
                metadata={"root": str(self.root), "namespace": self.namespace},
            )
        try:
            boundary = canonical_root(self.root)
            directories = iter_skill_directories(boundary, max_depth=self.max_depth)
            revisions = []
            errors: list[dict[str, Any]] = []
            for directory in directories:
                relative = directory.relative_to(boundary).as_posix()
                provenance = SkillProvenance(
                    source_kind=self.source_kind,
                    source_id=self.source_id,
                    source_namespace=self.namespace or self.source_kind.value,
                    origin_uri=self.origin_uri or boundary.as_uri(),
                    canonical_root=str(directory.resolve(strict=True)),
                    discovery_root=str(boundary),
                    trust_tier=TRUST_BY_SOURCE[self.source_kind],
                    source_rank=SOURCE_RANKS[self.source_kind],
                    project_distance=self.project_distance,
                    configured_order=self.configured_order,
                    metadata={"relative_skill_root": relative},
                )
                try:
                    revisions.append(
                        build_revision(
                            skill_root=directory,
                            provenance=provenance,
                            generation=generation,
                        )
                    )
                except Exception as error:  # noqa: BLE001 - source scan reports typed errors.
                    errors.append(
                        {
                            "code": str(getattr(error, "code", "skill_revision_invalid")),
                            "message": str(error),
                            "detail": {
                                **dict(getattr(error, "detail", {}) or {}),
                                "skill_root": relative,
                            },
                        }
                    )
            if errors and self.strict:
                return SkillSourceScan(
                    source_id=self.source_id,
                    source_kind=self.source_kind,
                    state=SkillSourceState.RELOAD_REJECTED,
                    revisions=(),
                    errors=tuple(errors),
                    metadata={"root": str(boundary), "namespace": self.namespace},
                )
            return SkillSourceScan(
                source_id=self.source_id,
                source_kind=self.source_kind,
                state=SkillSourceState.STAGED,
                revisions=tuple(revisions),
                errors=tuple(errors),
                metadata={
                    "root": str(boundary),
                    "namespace": self.namespace,
                    "discovered_count": len(directories),
                    "valid_count": len(revisions),
                    "strict": self.strict,
                },
            )
        except Exception as error:  # noqa: BLE001 - preserve last-good registry on any scan failure.
            return rejected_scan(
                self,
                SkillSourceError(str(error), detail=dict(getattr(error, "detail", {}) or {})),
                metadata={"root": str(self.root), "namespace": self.namespace},
            )
