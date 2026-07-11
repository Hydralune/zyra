from __future__ import annotations

"""Discover 03C skill packages from the active 03B capability catalog."""

import copy
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Mapping, Sequence

from .digests import digest_object
from .integration_errors import (
    SkillMcpProjectionInvalid,
    SkillMcpResourceMissing,
    SkillMcpServerUnavailable,
)
from .mcp_integration import (
    McpClientSkillResourcePort,
    McpSkillMaterialization,
    McpSkillProjectionOpen,
    McpSkillProjectionRuntime,
)
from .models import utc_now
from .sources.base import SkillSource


@dataclass(frozen=True, slots=True)
class McpSkillServerCandidate:
    server_id: str
    index_uri: str
    connection_generation: int
    capability_generation: int
    capability_revision: str
    advertised_resource_uris: tuple[str, ...]
    discovery_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "index_uri": self.index_uri,
            "connection_generation": self.connection_generation,
            "capability_generation": self.capability_generation,
            "capability_revision": self.capability_revision,
            "advertised_resource_uris": list(self.advertised_resource_uris),
            "discovery_reason": self.discovery_reason,
        }


@dataclass(frozen=True, slots=True)
class McpSkillServerProjection:
    candidate: McpSkillServerCandidate
    status: str
    source_id: str = ""
    projection_id: str = ""
    materialization: dict[str, Any] = field(default_factory=dict)
    error_code: str = ""
    error_message: str = ""
    used_last_good: bool = False
    created_at: str = field(default_factory=utc_now)

    @property
    def ready(self) -> bool:
        return self.status in {"projected", "unchanged", "last_good"} and bool(self.source_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_dict(),
            "status": self.status,
            "source_id": self.source_id,
            "projection_id": self.projection_id,
            "materialization": copy.deepcopy(self.materialization),
            "error_code": self.error_code,
            "error_message": self.error_message,
            "used_last_good": self.used_last_good,
            "ready": self.ready,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpSkillDiscoveryReceipt:
    discovery_id: str
    run_id: str
    task_id: str
    worker_request_id: str
    candidates: tuple[McpSkillServerCandidate, ...]
    projections: tuple[McpSkillServerProjection, ...]
    source_ids: tuple[str, ...]
    generation: int
    changed_server_ids: tuple[str, ...]
    removed_server_ids: tuple[str, ...]
    failed_server_ids: tuple[str, ...]
    snapshot_digest: str
    created_at: str = field(default_factory=utc_now)

    @property
    def ok(self) -> bool:
        # A server advertising a skill index is an explicit capability claim.
        # Projection failure remains visible, but unrelated CodeWorker tools
        # stay usable.  The failed source itself never enters composition.
        return not self.failed_server_ids

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.mcp-skill-discovery.v1",
            "discovery_id": self.discovery_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "worker_request_id": self.worker_request_id,
            "candidates": [item.to_dict() for item in self.candidates],
            "projections": [item.to_dict() for item in self.projections],
            "source_ids": list(self.source_ids),
            "generation": self.generation,
            "changed_server_ids": list(self.changed_server_ids),
            "removed_server_ids": list(self.removed_server_ids),
            "failed_server_ids": list(self.failed_server_ids),
            "snapshot_digest": self.snapshot_digest,
            "ok": self.ok,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpSkillDiscoveryOpen:
    sources: tuple[SkillSource, ...]
    receipt: McpSkillDiscoveryReceipt


@dataclass(frozen=True, slots=True)
class _LastGoodProjection:
    candidate: McpSkillServerCandidate
    opened: McpSkillProjectionOpen


class McpSkillDiscoveryRuntime:
    """Reconcile advertised MCP skill indexes into immutable local sources.

    Discovery reads only the 03B in-memory capability catalog.  A server is a
    candidate only when it advertises the exact index resource (or a resource
    explicitly annotated with the Zyra skill-index schema).  Projection uses
    ``McpClientSkillResourcePort`` and therefore remains under 03B connection,
    authentication, resource, output, and artifact custody.
    """

    DEFAULT_INDEX_URI = McpSkillProjectionRuntime.INDEX_URI

    def __init__(
        self,
        client_runtime: Any,
        *,
        cache_root: str | Path,
        preserve_last_good: bool = True,
    ) -> None:
        self.client_runtime = client_runtime
        self.cache_root = Path(cache_root).resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.preserve_last_good = bool(preserve_last_good)
        self._lock = RLock()
        self._last_good: dict[str, _LastGoodProjection] = {}
        self._generation = 0
        self._last_digest = digest_object({"generation": 0, "servers": {}})

    def discover(
        self,
        *,
        run_id: str,
        task_id: str,
        worker_request_id: str,
        node_id: str | None = None,
        include_servers: Sequence[str] | None = None,
    ) -> McpSkillDiscoveryOpen:
        if not run_id or not task_id or not worker_request_id:
            raise SkillMcpProjectionInvalid(
                "MCP skill discovery requires run/task/worker identity"
            )
        candidates = self._candidates(include_servers=include_servers)
        port = McpClientSkillResourcePort(
            self.client_runtime,
            run_id=run_id,
            task_id=task_id,
            node_id=node_id,
        )
        projection_runtime = McpSkillProjectionRuntime(port, cache_root=self.cache_root)
        projections: list[McpSkillServerProjection] = []
        selected_sources: dict[str, SkillSource] = {}
        changed: list[str] = []
        failed: list[str] = []
        current_servers = {item.server_id for item in candidates}
        with self._lock:
            previous_servers = set(self._last_good)
        removed = sorted(previous_servers - current_servers)
        for candidate in candidates:
            previous = self._get_last_good(candidate.server_id)
            try:
                opened = projection_runtime.open(
                    candidate.server_id,
                    index_uri=candidate.index_uri,
                )
                source_id = str(getattr(opened.source, "source_id", ""))
                if not source_id:
                    raise SkillMcpProjectionInvalid(
                        "MCP skill projection produced a source without identity"
                    )
                selected_sources[source_id] = opened.source
                status = "unchanged" if previous and previous.opened.materialization.projection_id == opened.materialization.projection_id else "projected"
                if status == "projected":
                    changed.append(candidate.server_id)
                projections.append(
                    McpSkillServerProjection(
                        candidate=candidate,
                        status=status,
                        source_id=source_id,
                        projection_id=opened.materialization.projection_id,
                        materialization=opened.materialization.to_dict(),
                    )
                )
                self._set_last_good(candidate, opened)
            except Exception as error:  # noqa: BLE001 - per-server isolation with explicit receipt.
                if previous is not None and self.preserve_last_good and self._last_good_compatible(previous, candidate):
                    source = previous.opened.source
                    source_id = str(getattr(source, "source_id", ""))
                    selected_sources[source_id] = source
                    projections.append(
                        McpSkillServerProjection(
                            candidate=candidate,
                            status="last_good",
                            source_id=source_id,
                            projection_id=previous.opened.materialization.projection_id,
                            materialization=previous.opened.materialization.to_dict(),
                            error_code=str(getattr(error, "code", "mcp_skill_projection_failed")),
                            error_message=str(error)[:1_000],
                            used_last_good=True,
                        )
                    )
                else:
                    failed.append(candidate.server_id)
                    projections.append(
                        McpSkillServerProjection(
                            candidate=candidate,
                            status="failed",
                            error_code=str(getattr(error, "code", "mcp_skill_projection_failed")),
                            error_message=str(error)[:1_000],
                        )
                    )
        with self._lock:
            for server_id in removed:
                self._last_good.pop(server_id, None)
            payload = {
                "candidates": [item.to_dict() for item in candidates],
                "projections": [item.to_dict() for item in projections],
                "source_ids": sorted(selected_sources),
            }
            snapshot_digest = digest_object(payload)
            if snapshot_digest != self._last_digest:
                self._generation += 1
                self._last_digest = snapshot_digest
            generation = self._generation
        receipt = McpSkillDiscoveryReceipt(
            discovery_id=digest_object(
                {
                    "run_id": run_id,
                    "task_id": task_id,
                    "worker_request_id": worker_request_id,
                    "snapshot_digest": snapshot_digest,
                }
            )[:40],
            run_id=run_id,
            task_id=task_id,
            worker_request_id=worker_request_id,
            candidates=candidates,
            projections=tuple(projections),
            source_ids=tuple(sorted(selected_sources)),
            generation=generation,
            changed_server_ids=tuple(sorted(set(changed))),
            removed_server_ids=tuple(removed),
            failed_server_ids=tuple(sorted(set(failed))),
            snapshot_digest=snapshot_digest,
        )
        return McpSkillDiscoveryOpen(
            sources=tuple(selected_sources[key] for key in sorted(selected_sources)),
            receipt=receipt,
        )

    def revoke(self, server_id: str, *, reason: str) -> McpSkillMaterialization | None:
        del reason  # caller records the reason; no secret-bearing text is persisted here.
        with self._lock:
            previous = self._last_good.pop(str(server_id), None)
            if previous is None:
                return None
            self._generation += 1
            self._last_digest = digest_object(
                {
                    "generation": self._generation,
                    "servers": sorted(self._last_good),
                }
            )
            return previous.opened.materialization

    def active_sources(self) -> tuple[SkillSource, ...]:
        with self._lock:
            values = [item.opened.source for _, item in sorted(self._last_good.items())]
        return tuple(values)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            servers = {
                server_id: {
                    "candidate": value.candidate.to_dict(),
                    "materialization": value.opened.materialization.to_dict(),
                    "source_id": str(getattr(value.opened.source, "source_id", "")),
                }
                for server_id, value in sorted(self._last_good.items())
            }
            return {
                "schema": "zyra.mcp-skill-discovery-state.v1",
                "generation": self._generation,
                "servers": servers,
                "snapshot_digest": self._last_digest,
                "remote_transport_owned": False,
                "credential_state_persisted": False,
            }

    def _candidates(
        self,
        *,
        include_servers: Sequence[str] | None,
    ) -> tuple[McpSkillServerCandidate, ...]:
        catalog = getattr(self.client_runtime, "catalog", None)
        if catalog is None or not callable(getattr(catalog, "list", None)):
            raise SkillMcpServerUnavailable("03B MCP capability catalog is unavailable")
        allowed = {str(item) for item in include_servers} if include_servers is not None else None
        candidates: list[McpSkillServerCandidate] = []
        for snapshot in catalog.list():
            server_id = str(getattr(snapshot, "server_id", ""))
            if not server_id or (allowed is not None and server_id not in allowed):
                continue
            resources = tuple(getattr(snapshot, "resources", ()) or ())
            advertised = tuple(
                sorted(
                    str(getattr(item, "uri", ""))
                    for item in resources
                    if str(getattr(item, "uri", ""))
                )
            )
            index_uri, reason = self._index_resource(resources)
            if not index_uri:
                continue
            try:
                connection = self.client_runtime.connection_runtime.snapshot(server_id)
            except Exception:
                continue
            if not bool(getattr(connection, "healthy", False)):
                continue
            candidates.append(
                McpSkillServerCandidate(
                    server_id=server_id,
                    index_uri=index_uri,
                    connection_generation=int(getattr(connection, "generation", 0) or 0),
                    capability_generation=int(getattr(snapshot, "generation", 0) or 0),
                    capability_revision=str(getattr(snapshot, "snapshot_digest", "") or ""),
                    advertised_resource_uris=advertised,
                    discovery_reason=reason,
                )
            )
        candidates.sort(key=lambda item: item.server_id)
        return tuple(candidates)

    def _index_resource(self, resources: Iterable[Any]) -> tuple[str, str]:
        annotated: list[str] = []
        for resource in resources:
            uri = str(getattr(resource, "uri", "") or "")
            if uri == self.DEFAULT_INDEX_URI:
                return uri, "canonical_uri"
            metadata = getattr(resource, "metadata", None)
            if isinstance(metadata, Mapping):
                schema = str(metadata.get("schema") or metadata.get("zyra_schema") or "")
                role = str(metadata.get("role") or "")
                if schema == "zyra.mcp-skills/v1" and role in {"", "skill_index"}:
                    annotated.append(uri)
        annotated = sorted(set(item for item in annotated if item))
        if len(annotated) > 1:
            raise SkillMcpProjectionInvalid(
                "MCP server advertises multiple annotated skill indexes",
                detail={"uris": annotated},
            )
        return (annotated[0], "resource_annotation") if annotated else ("", "")

    def _get_last_good(self, server_id: str) -> _LastGoodProjection | None:
        with self._lock:
            return self._last_good.get(server_id)

    def _set_last_good(
        self,
        candidate: McpSkillServerCandidate,
        opened: McpSkillProjectionOpen,
    ) -> None:
        with self._lock:
            self._last_good[candidate.server_id] = _LastGoodProjection(candidate, opened)

    @staticmethod
    def _last_good_compatible(
        previous: _LastGoodProjection,
        candidate: McpSkillServerCandidate,
    ) -> bool:
        # A changed capability revision may revoke or replace resources.  Never
        # retain bytes across that boundary.  Last-good is only a transient
        # read failure fallback within the same exact catalog snapshot.
        return (
            previous.candidate.capability_revision == candidate.capability_revision
            and previous.candidate.connection_generation == candidate.connection_generation
            and Path(previous.opened.materialization.root).exists()
        )


_DISCOVERY_LOCK = RLock()
_DISCOVERY_BY_RUNTIME: dict[int, McpSkillDiscoveryRuntime] = {}


def default_mcp_skill_discovery_runtime(
    client_runtime: Any,
    *,
    cache_root: str | Path,
) -> McpSkillDiscoveryRuntime:
    key = id(client_runtime)
    with _DISCOVERY_LOCK:
        runtime = _DISCOVERY_BY_RUNTIME.get(key)
        if runtime is None:
            runtime = McpSkillDiscoveryRuntime(client_runtime, cache_root=cache_root)
            _DISCOVERY_BY_RUNTIME[key] = runtime
        return runtime


__all__ = [
    "McpSkillDiscoveryOpen",
    "McpSkillDiscoveryReceipt",
    "McpSkillDiscoveryRuntime",
    "McpSkillServerCandidate",
    "McpSkillServerProjection",
    "default_mcp_skill_discovery_runtime",
]
