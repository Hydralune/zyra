from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import SkillSourceKind
from .base import SkillSourceScan, SkillSourceState
from .filesystem import FilesystemSkillSource


@dataclass(frozen=True, slots=True)
class McpSkillProjection:
    server_id: str
    projection_id: str
    root: str
    enabled: bool = True
    authenticated: bool = True
    capability_revision: str = ""
    expected_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class McpProjectedSkillSource:
    projection: McpSkillProjection
    configured_order: int = 0
    source_kind: SkillSourceKind = field(default=SkillSourceKind.MCP, init=False)

    @property
    def source_id(self) -> str:
        return f"mcp:{self.projection.server_id}:{self.projection.projection_id}"

    def scan(self, *, generation: int) -> SkillSourceScan:
        projection = self.projection
        if not projection.enabled or not projection.authenticated:
            return SkillSourceScan(
                source_id=self.source_id,
                source_kind=self.source_kind,
                state=SkillSourceState.DISABLED,
                revisions=(),
                metadata={
                    "server_id": projection.server_id,
                    "projection_id": projection.projection_id,
                    "reason": "disabled" if not projection.enabled else "authentication_required",
                },
            )
        scan = FilesystemSkillSource(
            root=Path(projection.root),
            source_kind=SkillSourceKind.MCP,
            source_id=self.source_id,
            namespace=f"mcp:{projection.server_id}",
            configured_order=self.configured_order,
            strict=True,
            origin_uri=f"mcp://{projection.server_id}/{projection.projection_id}",
        ).scan(generation=generation)
        return SkillSourceScan(
            source_id=scan.source_id,
            source_kind=scan.source_kind,
            state=scan.state,
            revisions=scan.revisions,
            errors=scan.errors,
            warnings=scan.warnings,
            metadata={
                **scan.metadata,
                **projection.metadata,
                "server_id": projection.server_id,
                "projection_id": projection.projection_id,
                "capability_revision": projection.capability_revision,
                "expected_digest": projection.expected_digest,
                "owner": "M1-03B MCP projection",
                "experimental": True,
            },
        )
