from __future__ import annotations

import copy
import json
import os
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from threading import RLock
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import unquote, urlparse

from .digests import digest_object, sha256_bytes
from .integration_errors import (
    SkillMcpAuthenticationRequired,
    SkillMcpDigestMismatch,
    SkillMcpPaginationError,
    SkillMcpProjectionInvalid,
    SkillMcpProjectionStale,
    SkillMcpResourceMissing,
    SkillMcpResourceRejected,
    SkillMcpSamplingRejected,
    SkillMcpServerUnavailable,
)
from .models import utc_now, validate_skill_name
from .path_security import canonical_root, normalize_relative_path
from .sources.mcp import McpProjectedSkillSource, McpSkillProjection


class McpSkillResourcePort(Protocol):
    def server_snapshot(self, server_id: str) -> Mapping[str, Any]: ...

    def read_resource(
        self,
        server_id: str,
        uri: str,
        **context: Any,
    ) -> Mapping[str, Any]: ...


class McpClientSkillResourcePort:
    """Translate the productized 03B client into exact 03C resource reads.

    ``McpClientRuntime.read_resource`` intentionally returns a redacted,
    JSON-safe receipt for API callers.  A skill projection needs the verified
    resource bytes instead.  This in-process port stays behind 03B's active
    connection, capability catalog, output budget, and artifact custody; it
    never opens a second transport or accepts caller-supplied credentials.

    The class is duck typed on purpose.  ``packages/skills`` must not import
    the integrations package at module import time because the worker composes
    both packages.  Construction performs a strict structural check and every
    read revalidates connection/capability identity.
    """

    def __init__(
        self,
        client_runtime: Any,
        *,
        run_id: str,
        task_id: str,
        node_id: str | None = None,
        require_registered_resource: bool = True,
    ) -> None:
        self.client_runtime = client_runtime
        self.run_id = str(run_id).strip()
        self.task_id = str(task_id).strip()
        self.node_id = str(node_id).strip() if node_id else None
        self.require_registered_resource = bool(require_registered_resource)
        if not self.run_id or not self.task_id:
            raise SkillMcpProjectionInvalid(
                "MCP skill resource port requires run_id and task_id"
            )
        for attribute in (
            "connection_runtime",
            "catalog",
            "artifact_store",
        ):
            if getattr(client_runtime, attribute, None) is None:
                raise SkillMcpProjectionInvalid(
                    "MCP client runtime is missing required 03B capability",
                    detail={"attribute": attribute},
                )

    def server_snapshot(self, server_id: str) -> Mapping[str, Any]:
        server_id = str(server_id).strip()
        if not server_id:
            raise SkillMcpProjectionInvalid("MCP server id is required")
        try:
            connection = self.client_runtime.connection_runtime.snapshot(server_id)
        except Exception as error:
            raise SkillMcpServerUnavailable(
                "03B has no active MCP connection snapshot",
                detail={"server_id": server_id, "error_type": type(error).__name__},
            ) from error
        capability = self.client_runtime.catalog.get(server_id)
        state = str(getattr(connection, "state", "")).lower()
        if "." in state:
            state = state.rsplit(".", 1)[-1]
        healthy = bool(getattr(connection, "healthy", False))
        error_code = str(getattr(connection, "error_code", "") or "")
        authenticated = healthy and state == "connected" and error_code != "needs_auth"
        capability_revision = ""
        capability_generation = 0
        registered_resources: list[str] = []
        if capability is not None:
            capability_revision = str(getattr(capability, "snapshot_digest", "") or "")
            capability_generation = int(getattr(capability, "generation", 0) or 0)
            registered_resources = sorted(
                str(getattr(item, "uri", ""))
                for item in getattr(capability, "resources", ())
                if str(getattr(item, "uri", ""))
            )
        return {
            "schema": "zyra.mcp-skill-server-snapshot.v1",
            "server_id": server_id,
            "state": state,
            "status": state,
            "enabled": not bool(getattr(self.client_runtime, "disabled", False)),
            "healthy": healthy,
            "authenticated": authenticated,
            "auth_status": "authenticated" if authenticated else "required" if state == "needs_auth" else "unavailable",
            "connection_revision": int(getattr(connection, "revision", 0) or 0),
            "connection_generation": int(getattr(connection, "generation", 0) or 0),
            "capability_generation": capability_generation,
            "capability_revision": capability_revision,
            "registered_resources": registered_resources,
            "error_code": error_code,
        }

    def read_resource(
        self,
        server_id: str,
        uri: str,
        **context: Any,
    ) -> Mapping[str, Any]:
        before = self.server_snapshot(server_id)
        if not bool(before.get("healthy")):
            raise SkillMcpServerUnavailable(
                "MCP server disconnected before skill resource read",
                detail={"server_id": server_id, "state": before.get("state")},
            )
        resources = tuple(str(item) for item in before.get("registered_resources", ()))
        if self.require_registered_resource and uri not in resources:
            raise SkillMcpResourceMissing(
                "MCP skill resource is not present in the active 03B capability snapshot",
                detail={"server_id": server_id, "uri": uri},
            )
        if context.get("sampling_allowed") is not False:
            raise SkillMcpSamplingRejected(
                "MCP skill resource reads must explicitly disable sampling"
            )
        cursor = str(context.get("cursor") or "")
        if cursor:
            # MCP resources/read has no standard cursor input.  Pagination for
            # the skill index uses distinct index resources advertised by the
            # server; accepting an opaque cursor here would silently ignore it.
            raise SkillMcpPaginationError(
                "03B resources/read does not support an implicit skill cursor",
                detail={"server_id": server_id, "uri": uri, "cursor": cursor},
            )
        try:
            receipt = self.client_runtime.connection_runtime.read_resource(
                server_id,
                uri,
                run_id=self.run_id,
                task_id=self.task_id,
                node_id=self.node_id,
            )
        except Exception as error:
            raise SkillMcpResourceMissing(
                "03B failed to read MCP skill resource",
                detail={
                    "server_id": server_id,
                    "uri": uri,
                    "error_type": type(error).__name__,
                },
            ) from error
        after = self.server_snapshot(server_id)
        identity_fields = (
            "connection_generation",
            "capability_generation",
            "capability_revision",
        )
        changed = {
            key: {"before": before.get(key), "after": after.get(key)}
            for key in identity_fields
            if before.get(key) != after.get(key)
        }
        if changed:
            raise SkillMcpProjectionStale(
                "MCP connection or capabilities changed during skill resource read",
                detail={"server_id": server_id, "uri": uri, "changed": changed},
            )
        if bool(getattr(receipt, "is_error", False)):
            raise SkillMcpResourceMissing(
                "MCP resource receipt reports an error",
                detail={"server_id": server_id, "uri": uri},
            )
        content = self._receipt_content(receipt, expected_uri=uri)
        return {
            "schema": "zyra.mcp-skill-resource-read.v1",
            "ok": True,
            "server_id": server_id,
            "uri": uri,
            "content": content,
            "sampling_requested": False,
            "connection_generation": after["connection_generation"],
            "capability_generation": after["capability_generation"],
            "capability_revision": after["capability_revision"],
            "receipt_id": str(getattr(receipt, "receipt_id", "")),
        }

    def _receipt_content(self, receipt: Any, *, expected_uri: str) -> list[str]:
        artifacts = {
            str(getattr(item, "artifact_id", "")): item
            for item in getattr(receipt, "artifacts", ())
        }
        chunks: list[str] = []
        for projection in sorted(
            getattr(receipt, "projections", ()),
            key=lambda item: int(getattr(item, "index", 0)),
        ):
            source_uri = str(getattr(projection, "source_uri", "") or "")
            if source_uri and source_uri != expected_uri:
                raise SkillMcpResourceRejected(
                    "MCP resource receipt changed the requested URI",
                    detail={"expected": expected_uri, "actual": source_uri},
                )
            artifact_id = str(getattr(projection, "artifact_id", "") or "")
            if artifact_id:
                artifact = artifacts.get(artifact_id)
                if artifact is None:
                    raise SkillMcpResourceMissing(
                        "MCP resource projection references an absent artifact",
                        detail={"artifact_id": artifact_id},
                    )
                chunks.append(self._read_artifact_text(artifact))
                continue
            inline = getattr(projection, "inline", None)
            if not isinstance(inline, Mapping):
                raise SkillMcpResourceRejected(
                    "MCP resource projection is not textual"
                )
            if isinstance(inline.get("text"), str):
                chunks.append(str(inline["text"]))
                continue
            value = inline.get("value")
            if isinstance(value, (Mapping, list)):
                chunks.append(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
                continue
            if isinstance(value, str):
                chunks.append(value)
                continue
            raise SkillMcpResourceRejected(
                "MCP resource projection contains no exact text"
            )
        if not chunks:
            raise SkillMcpResourceMissing("MCP resource receipt contains no content")
        return chunks

    def _read_artifact_text(self, artifact: Any) -> str:
        try:
            path = self.client_runtime.artifact_store.resolve_path(artifact)
            root = canonical_root(self.client_runtime.artifact_store.root)
            resolved = Path(path).resolve(strict=True)
            resolved.relative_to(root)
        except Exception as error:
            raise SkillMcpResourceRejected(
                "MCP resource artifact escaped 03B artifact custody",
                detail={"artifact_id": str(getattr(artifact, "artifact_id", ""))},
            ) from error
        try:
            return resolved.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SkillMcpResourceRejected(
                "MCP skill resource artifact is not UTF-8 text",
                detail={"artifact_id": str(getattr(artifact, "artifact_id", ""))},
            ) from error


@dataclass(frozen=True, slots=True)
class McpSkillResourceEntry:
    relative_path: str
    uri: str
    digest: str
    media_type: str = "text/markdown"
    max_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        normalize_relative_path(self.relative_path)
        if not self.uri.strip():
            raise SkillMcpProjectionInvalid("MCP skill resource requires uri")
        if not self.digest.startswith("sha256:"):
            raise SkillMcpProjectionInvalid("MCP skill resource requires sha256 digest")
        if self.max_bytes <= 0:
            raise SkillMcpProjectionInvalid("MCP skill resource max_bytes must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "uri": self.uri,
            "digest": self.digest,
            "media_type": self.media_type,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True, slots=True)
class McpSkillIndexEntry:
    name: str
    version: str
    body_uri: str
    body_digest: str
    description: str = ""
    resources: tuple[McpSkillResourceEntry, ...] = ()
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_skill_name(self.name)
        if not self.version.strip():
            raise SkillMcpProjectionInvalid("MCP skill version is required")
        if not self.body_uri.strip():
            raise SkillMcpProjectionInvalid("MCP skill body_uri is required")
        if not self.body_digest.startswith("sha256:"):
            raise SkillMcpProjectionInvalid("MCP skill body requires sha256 digest")
        paths = [item.relative_path for item in self.resources]
        if len(paths) != len(set(paths)):
            raise SkillMcpProjectionInvalid(
                "MCP skill index contains duplicate resource paths",
                detail={"skill": self.name},
            )

    @property
    def entry_digest(self) -> str:
        return digest_object(
            {
                "name": self.name,
                "version": self.version,
                "body_uri": self.body_uri,
                "body_digest": self.body_digest,
                "description": self.description,
                "resources": [item.to_dict() for item in self.resources],
                "enabled": self.enabled,
                "metadata": self.metadata,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "body_uri": self.body_uri,
            "body_digest": self.body_digest,
            "description": self.description,
            "resources": [item.to_dict() for item in self.resources],
            "enabled": self.enabled,
            "metadata": copy.deepcopy(self.metadata),
            "entry_digest": self.entry_digest,
        }


@dataclass(frozen=True, slots=True)
class McpSkillIndex:
    server_id: str
    index_uri: str
    capability_revision: str
    entries: tuple[McpSkillIndexEntry, ...]
    next_cursor: str = ""
    index_digest: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.server_id.strip():
            raise SkillMcpProjectionInvalid("MCP skill index requires server_id")
        if not self.index_uri.strip():
            raise SkillMcpProjectionInvalid("MCP skill index requires index_uri")
        names = [item.name for item in self.entries]
        if len(names) != len(set(names)):
            raise SkillMcpProjectionInvalid("MCP skill index contains duplicate skill names")

    def calculated_digest(self) -> str:
        return digest_object(
            {
                "server_id": self.server_id,
                "index_uri": self.index_uri,
                "capability_revision": self.capability_revision,
                "entries": [item.to_dict() for item in self.entries],
                "next_cursor": self.next_cursor,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "index_uri": self.index_uri,
            "capability_revision": self.capability_revision,
            "entries": [item.to_dict() for item in self.entries],
            "next_cursor": self.next_cursor,
            "index_digest": self.index_digest or self.calculated_digest(),
            "metadata": copy.deepcopy(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class McpSkillMaterialization:
    server_id: str
    projection_id: str
    root: str
    capability_revision: str
    index_digest: str
    entry_refs: tuple[str, ...]
    changed: bool
    generation: int
    materialization_digest: str
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "projection_id": self.projection_id,
            "root": self.root,
            "capability_revision": self.capability_revision,
            "index_digest": self.index_digest,
            "entry_refs": list(self.entry_refs),
            "changed": self.changed,
            "generation": self.generation,
            "materialization_digest": self.materialization_digest,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class McpSkillProjectionOpen:
    source: McpProjectedSkillSource
    materialization: McpSkillMaterialization
    index: McpSkillIndex


class McpSkillProjectionRuntime:
    """03B resource projection -> 03C immutable skill package adapter.

    The adapter reads only from an already active/authenticated 03B runtime.
    It does not own transport, authentication, retries or permission.  Exact
    bytes are verified and atomically materialized into a Zyra-owned cache so
    compact restore is independent from subsequent server changes.
    """

    INDEX_URI = "skill://index.json"

    def __init__(
        self,
        port: McpSkillResourcePort,
        *,
        cache_root: str | Path,
        max_index_bytes: int = 1_000_000,
        max_body_bytes: int = 2_000_000,
        max_pages: int = 32,
        max_skills: int = 512,
    ) -> None:
        self.port = port
        self.cache_root = Path(cache_root).resolve()
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.max_index_bytes = max_index_bytes
        self.max_body_bytes = max_body_bytes
        self.max_pages = max_pages
        self.max_skills = max_skills
        self._lock = RLock()
        self._generation_by_server: dict[str, int] = {}
        self._active_by_server: dict[str, McpSkillMaterialization] = {}

    def open(self, server_id: str, *, index_uri: str | None = None) -> McpSkillProjectionOpen:
        server_id = str(server_id).strip()
        snapshot = self._validate_server(server_id)
        uri = str(index_uri or self.INDEX_URI)
        index = self._load_index(
            server_id,
            uri,
            capability_revision=str(snapshot.get("capability_revision") or snapshot.get("generation") or ""),
        )
        materialization = self._materialize(index)
        source = McpProjectedSkillSource(
            McpSkillProjection(
                server_id=server_id,
                projection_id=materialization.projection_id,
                root=materialization.root,
                enabled=True,
                authenticated=True,
                capability_revision=index.capability_revision,
                expected_digest=index.index_digest or index.calculated_digest(),
                metadata={
                    "owner": "M1-03B resource runtime -> M1-03C skill projection",
                    "materialization_digest": materialization.materialization_digest,
                    "index_uri": index.index_uri,
                    "cache_only": True,
                    "remote_body_lazy_before_materialization": True,
                },
            )
        )
        return McpSkillProjectionOpen(source=source, materialization=materialization, index=index)

    def active(self, server_id: str) -> McpSkillMaterialization | None:
        with self._lock:
            return self._active_by_server.get(str(server_id))

    def revoke(self, server_id: str, *, reason: str) -> McpSkillMaterialization | None:
        with self._lock:
            value = self._active_by_server.pop(str(server_id), None)
        if value is None:
            return None
        marker = Path(value.root).parent / ".revoked.json"
        marker.write_text(
            json.dumps(
                {
                    "server_id": server_id,
                    "projection_id": value.projection_id,
                    "reason": reason,
                    "revoked_at": utc_now(),
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return value

    def _validate_server(self, server_id: str) -> Mapping[str, Any]:
        if not server_id:
            raise SkillMcpProjectionInvalid("MCP skill projection requires server_id")
        snapshot = self.port.server_snapshot(server_id)
        if not isinstance(snapshot, Mapping):
            raise SkillMcpServerUnavailable(
                "MCP server snapshot is unavailable",
                detail={"server_id": server_id},
            )
        status = str(snapshot.get("status") or snapshot.get("state") or "").lower()
        enabled = bool(snapshot.get("enabled", True))
        authenticated = bool(snapshot.get("authenticated", snapshot.get("auth_status") != "required"))
        if not enabled or status in {"disabled", "closed", "failed", "unavailable"}:
            raise SkillMcpServerUnavailable(
                "MCP server is not active for skill projection",
                detail={"server_id": server_id, "status": status},
            )
        if not authenticated:
            raise SkillMcpAuthenticationRequired(
                "MCP server authentication is required",
                detail={"server_id": server_id},
            )
        return snapshot

    def _load_index(
        self,
        server_id: str,
        index_uri: str,
        *,
        capability_revision: str,
    ) -> McpSkillIndex:
        entries: list[McpSkillIndexEntry] = []
        cursor = ""
        seen_cursors: set[str] = set()
        page = 0
        supplied_digest = ""
        while True:
            page += 1
            if page > self.max_pages:
                raise SkillMcpPaginationError("MCP skill index exceeded page limit")
            if cursor in seen_cursors and cursor:
                raise SkillMcpPaginationError("MCP skill index cursor repeated")
            if cursor:
                seen_cursors.add(cursor)
            result = self.port.read_resource(
                server_id,
                index_uri,
                cursor=cursor,
                operation="skill_index_read",
                sampling_allowed=False,
            )
            raw = self._resource_bytes(result, max_bytes=self.max_index_bytes)
            try:
                value = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise SkillMcpProjectionInvalid("MCP skill index is not valid UTF-8 JSON") from error
            if not isinstance(value, dict):
                raise SkillMcpProjectionInvalid("MCP skill index must be an object")
            allowed = {"schema", "server_id", "capability_revision", "skills", "next_cursor", "digest", "metadata"}
            unknown = set(value) - allowed
            if unknown:
                raise SkillMcpProjectionInvalid(
                    "MCP skill index contains unknown fields",
                    detail={"fields": sorted(unknown)},
                )
            if str(value.get("schema") or "") != "zyra.mcp-skills/v1":
                raise SkillMcpProjectionInvalid("unsupported MCP skill index schema")
            declared_server = str(value.get("server_id") or server_id)
            if declared_server != server_id:
                raise SkillMcpProjectionInvalid("MCP skill index server identity mismatch")
            declared_revision = str(value.get("capability_revision") or capability_revision)
            if capability_revision and declared_revision and declared_revision != capability_revision:
                raise SkillMcpProjectionStale(
                    "MCP skill index capability revision is stale",
                    detail={"expected": capability_revision, "actual": declared_revision},
                )
            raw_entries = value.get("skills")
            if not isinstance(raw_entries, list):
                raise SkillMcpProjectionInvalid("MCP skill index skills must be an array")
            entries.extend(self._parse_entry(server_id, index_uri, item) for item in raw_entries)
            if len(entries) > self.max_skills:
                raise SkillMcpProjectionInvalid("MCP skill index exceeded skill limit")
            supplied_digest = str(value.get("digest") or supplied_digest)
            cursor = str(value.get("next_cursor") or "")
            if not cursor:
                metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                break
        index = McpSkillIndex(
            server_id=server_id,
            index_uri=index_uri,
            capability_revision=capability_revision,
            entries=tuple(entries),
            next_cursor="",
            index_digest=supplied_digest,
            metadata=dict(metadata),
        )
        calculated = index.calculated_digest()
        if supplied_digest and supplied_digest != calculated:
            raise SkillMcpDigestMismatch(
                "MCP skill index digest mismatch",
                detail={"expected": supplied_digest, "actual": calculated},
            )
        return McpSkillIndex(
            server_id=index.server_id,
            index_uri=index.index_uri,
            capability_revision=index.capability_revision,
            entries=index.entries,
            next_cursor=index.next_cursor,
            index_digest=supplied_digest or calculated,
            metadata=index.metadata,
        )

    def _parse_entry(
        self,
        server_id: str,
        index_uri: str,
        value: Any,
    ) -> McpSkillIndexEntry:
        if not isinstance(value, dict):
            raise SkillMcpProjectionInvalid("MCP skill index entry must be an object")
        allowed = {"name", "version", "description", "body_uri", "body_digest", "resources", "enabled", "metadata"}
        unknown = set(value) - allowed
        if unknown:
            raise SkillMcpProjectionInvalid(
                "MCP skill entry contains unknown fields",
                detail={"fields": sorted(unknown)},
            )
        resources = value.get("resources", [])
        if not isinstance(resources, list):
            raise SkillMcpProjectionInvalid("MCP skill resources must be an array")
        parsed_resources: list[McpSkillResourceEntry] = []
        for item in resources:
            if not isinstance(item, dict):
                raise SkillMcpProjectionInvalid("MCP skill resource entry must be an object")
            resource_allowed = {"path", "uri", "digest", "media_type", "max_bytes"}
            resource_unknown = set(item) - resource_allowed
            if resource_unknown:
                raise SkillMcpProjectionInvalid(
                    "MCP skill resource contains unknown fields",
                    detail={"fields": sorted(resource_unknown)},
                )
            uri = str(item.get("uri") or "")
            self._validate_uri(server_id, index_uri, uri)
            parsed_resources.append(
                McpSkillResourceEntry(
                    relative_path=str(item.get("path") or ""),
                    uri=uri,
                    digest=str(item.get("digest") or ""),
                    media_type=str(item.get("media_type") or "text/markdown"),
                    max_bytes=int(item.get("max_bytes") or 1_000_000),
                )
            )
        body_uri = str(value.get("body_uri") or "")
        self._validate_uri(server_id, index_uri, body_uri)
        return McpSkillIndexEntry(
            name=str(value.get("name") or ""),
            version=str(value.get("version") or ""),
            description=str(value.get("description") or ""),
            body_uri=body_uri,
            body_digest=str(value.get("body_digest") or ""),
            resources=tuple(parsed_resources),
            enabled=bool(value.get("enabled", True)),
            metadata=dict(value.get("metadata") or {}),
        )

    def _validate_uri(self, server_id: str, index_uri: str, uri: str) -> None:
        if not uri:
            raise SkillMcpProjectionInvalid("MCP skill resource URI is empty")
        parsed = urlparse(uri)
        if parsed.scheme not in {"skill", "mcp-resource"}:
            raise SkillMcpResourceRejected(
                "MCP skill resource URI uses an unsupported scheme",
                detail={"uri": uri},
            )
        if parsed.netloc and parsed.netloc != server_id:
            raise SkillMcpResourceRejected(
                "MCP skill resource URI targets another server",
                detail={"uri": uri, "server_id": server_id},
            )
        path = PurePosixPath(unquote(parsed.path))
        if path.is_absolute():
            parts = path.parts[1:]
        else:
            parts = path.parts
        if any(part in {"", ".", ".."} for part in parts):
            raise SkillMcpResourceRejected(
                "MCP skill resource URI contains path traversal",
                detail={"uri": uri},
            )
        index = urlparse(index_uri)
        if index.netloc and parsed.netloc and index.netloc != parsed.netloc:
            raise SkillMcpResourceRejected("MCP skill resource URI escaped index authority")

    def _materialize(self, index: McpSkillIndex) -> McpSkillMaterialization:
        index_digest = index.index_digest or index.calculated_digest()
        projection_id = digest_object(
            {
                "server_id": index.server_id,
                "capability_revision": index.capability_revision,
                "index_digest": index_digest,
            }
        )[:32]
        server_root = self.cache_root / _safe_component(index.server_id)
        final = server_root / projection_id / "skills"
        with self._lock:
            active = self._active_by_server.get(index.server_id)
            if active and active.projection_id == projection_id and Path(active.root).exists():
                return replace(active, changed=False)
            generation = self._generation_by_server.get(index.server_id, 0) + 1
        staging = server_root / f".staging-{projection_id}-{generation}"
        backup = server_root / f".backup-{projection_id}-{generation}"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=False)
        staging_skills = staging / "skills"
        staging_skills.mkdir(parents=True, exist_ok=False)
        entry_refs: list[str] = []
        try:
            for entry in index.entries:
                if not entry.enabled:
                    continue
                root = staging_skills / entry.name
                root.mkdir(parents=True, exist_ok=False)
                body = self._read_exact(
                    index.server_id,
                    entry.body_uri,
                    expected_digest=entry.body_digest,
                    max_bytes=self.max_body_bytes,
                    operation="skill_body_read",
                )
                (root / "SKILL.md").write_bytes(body)
                for resource in entry.resources:
                    content = self._read_exact(
                        index.server_id,
                        resource.uri,
                        expected_digest=resource.digest,
                        max_bytes=resource.max_bytes,
                        operation="skill_resource_read",
                    )
                    target = root.joinpath(*normalize_relative_path(resource.relative_path).split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                entry_refs.append(
                    f"mcp-skill://{index.server_id}/{entry.name}@sha256:{entry.entry_digest}"
                )
            metadata = {
                "server_id": index.server_id,
                "projection_id": projection_id,
                "capability_revision": index.capability_revision,
                "index_digest": index_digest,
                "entry_refs": entry_refs,
                "generation": generation,
                "created_at": utc_now(),
            }
            (staging / ".projection.json").write_text(
                json.dumps(metadata, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            projection_root = final.parent
            projection_root.parent.mkdir(parents=True, exist_ok=True)
            moved_previous = False
            if projection_root.exists():
                os.replace(projection_root, backup)
                moved_previous = True
            try:
                os.replace(staging, projection_root)
            except Exception:
                if moved_previous and backup.exists():
                    os.replace(backup, projection_root)
                raise
            if backup.exists():
                shutil.rmtree(backup, ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        payload = {
            "server_id": index.server_id,
            "projection_id": projection_id,
            "root": str(final),
            "capability_revision": index.capability_revision,
            "index_digest": index_digest,
            "entry_refs": entry_refs,
            "generation": generation,
        }
        receipt = McpSkillMaterialization(
            server_id=index.server_id,
            projection_id=projection_id,
            root=str(final),
            capability_revision=index.capability_revision,
            index_digest=index_digest,
            entry_refs=tuple(entry_refs),
            changed=True,
            generation=generation,
            materialization_digest=digest_object(payload),
        )
        with self._lock:
            self._generation_by_server[index.server_id] = generation
            self._active_by_server[index.server_id] = receipt
        return receipt

    def _read_exact(
        self,
        server_id: str,
        uri: str,
        *,
        expected_digest: str,
        max_bytes: int,
        operation: str,
    ) -> bytes:
        result = self.port.read_resource(
            server_id,
            uri,
            operation=operation,
            sampling_allowed=False,
        )
        value = self._resource_bytes(result, max_bytes=max_bytes)
        actual = f"sha256:{sha256_bytes(value)}"
        if actual != expected_digest:
            raise SkillMcpDigestMismatch(
                "MCP skill resource digest mismatch",
                detail={"uri": uri, "expected": expected_digest, "actual": actual},
            )
        return value

    def _resource_bytes(self, result: Mapping[str, Any], *, max_bytes: int) -> bytes:
        if not isinstance(result, Mapping):
            raise SkillMcpResourceMissing("MCP resource result is not an object")
        if result.get("sampling_requested") is True:
            raise SkillMcpSamplingRejected("MCP skill projection refuses sampling requests")
        if result.get("ok") is False:
            raise SkillMcpResourceMissing(
                "MCP resource read failed",
                detail={"error": result.get("error")},
            )
        content = result.get("content")
        if isinstance(content, str):
            value = content.encode("utf-8")
        elif isinstance(content, bytes):
            value = content
        elif isinstance(content, list):
            chunks: list[str] = []
            for item in content:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    chunks.append(str(item["text"]))
                else:
                    raise SkillMcpResourceRejected("MCP resource content contains a non-text block")
            value = "".join(chunks).encode("utf-8")
        else:
            raise SkillMcpResourceMissing("MCP resource result contains no text content")
        if len(value) > max_bytes:
            raise SkillMcpResourceRejected(
                "MCP skill resource exceeds byte limit",
                detail={"size": len(value), "maximum": max_bytes},
            )
        return value


def _safe_component(value: str) -> str:
    selected = "".join(character if character.isalnum() or character in "._-" else "_" for character in value)
    if not selected or selected in {".", ".."}:
        raise SkillMcpProjectionInvalid("MCP server id cannot form a cache path")
    return selected[:160]
