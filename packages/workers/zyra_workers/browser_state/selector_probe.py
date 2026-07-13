from __future__ import annotations

"""Live CDP verification for durable browser selector references."""

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from .contracts import (
    BrowserSelectorEntry,
    BrowserSelectorMapRevision,
    BrowserSelectorResolution,
    SelectorMapIdentity,
    digest_json,
)
from .errors import BrowserSelectorIdentityMismatch, BrowserSelectorNotFound, BrowserSelectorStale


class BrowserSelectorCdpPort(Protocol):
    def send(
        self,
        method: str,
        params: Mapping[str, Any] | None = None,
        *,
        cdp_session_id: str = "",
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]: ...

    def snapshot(self) -> Any: ...


class BrowserSelectorTargetPort(Protocol):
    @property
    def active_target_id(self) -> str: ...
    def snapshot(self) -> Any: ...
    def target(self, target_id: str) -> Any: ...
    def cdp_session(self, target_id: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class BrowserLiveSelectorReceipt:
    entry: BrowserSelectorEntry
    revision: BrowserSelectorMapRevision
    current: bool
    frontend_node_id: int
    backend_node_id: int
    remote_object_id: str
    remote_object_type: str
    target_id: str
    cdp_session_id: str
    frame_id: str
    tag_name: str
    attributes: Mapping[str, Any]
    text_preview: str
    identity_before: Mapping[str, Any]
    identity_after: Mapping[str, Any]
    push_duration_ms: float
    resolve_duration_ms: float
    inspect_duration_ms: float
    resolved_at: str
    findings: tuple[str, ...] = ()

    @property
    def live(self) -> bool:
        return bool(self.frontend_node_id and self.remote_object_id and self.current)

    @property
    def identity_stable(self) -> bool:
        return digest_json(self.identity_before) == digest_json(self.identity_after)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "revision_id": self.revision.revision_id,
            "revision": self.revision.revision,
            "current": self.current,
            "live": self.live,
            "frontend_node_id": self.frontend_node_id,
            "backend_node_id": self.backend_node_id,
            "remote_object_id": self.remote_object_id,
            "remote_object_type": self.remote_object_type,
            "target_id": self.target_id,
            "cdp_session_id": self.cdp_session_id,
            "frame_id": self.frame_id,
            "tag_name": self.tag_name,
            "attributes": dict(self.attributes),
            "text_preview": self.text_preview,
            "identity_before": dict(self.identity_before),
            "identity_after": dict(self.identity_after),
            "identity_stable": self.identity_stable,
            "push_duration_ms": self.push_duration_ms,
            "resolve_duration_ms": self.resolve_duration_ms,
            "inspect_duration_ms": self.inspect_duration_ms,
            "resolved_at": self.resolved_at,
            "findings": list(self.findings),
        }


@dataclass(frozen=True, slots=True)
class BrowserSelectorProbeAudit:
    revision_id: str
    attempted: int
    resolved: int
    failed: int
    target_count: int
    frame_count: int
    shadow_count: int
    executable_coverage: float
    receipts: tuple[BrowserLiveSelectorReceipt, ...]
    failures: tuple[Mapping[str, Any], ...]

    @property
    def valid(self) -> bool:
        return self.attempted > 0 and self.failed == 0 and self.resolved == self.attempted

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "attempted": self.attempted,
            "resolved": self.resolved,
            "failed": self.failed,
            "target_count": self.target_count,
            "frame_count": self.frame_count,
            "shadow_count": self.shadow_count,
            "executable_coverage": self.executable_coverage,
            "valid": self.valid,
            "receipts": [item.to_dict() for item in self.receipts],
            "failures": [dict(item) for item in self.failures],
        }


class BrowserLiveSelectorProbeRuntime:
    """Resolve a persisted selector through its owning target/session."""

    def __init__(self, *, timeout_seconds: float = 5.0, disabled: bool = False) -> None:
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.disabled = disabled
        self._resolutions = 0
        self._failures = 0
        self._stale_rejections = 0
        self._identity_rejections = 0

    def resolve(
        self,
        resolution: BrowserSelectorResolution,
        *,
        cdp_runtime: BrowserSelectorCdpPort,
        target_runtime: BrowserSelectorTargetPort,
    ) -> BrowserLiveSelectorReceipt:
        if self.disabled:
            raise BrowserSelectorNotFound("live browser selector probe is disabled")
        entry = resolution.entry
        revision = resolution.revision
        before = self._assert_current(entry, revision, cdp_runtime, target_runtime, phase="before_resolve")
        push_started = time.perf_counter()
        try:
            pushed = cdp_runtime.send(
                "DOM.pushNodesByBackendIdsToFrontend",
                {"backendNodeIds": [entry.backend_node_id]},
                cdp_session_id=entry.cdp_session_id,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as error:
            self._failures += 1
            raise BrowserSelectorNotFound(
                "CDP could not push browser selector backend node",
                details={
                    "selector_ref": entry.ref.opaque_ref,
                    "backend_node_id": entry.backend_node_id,
                    "target_id": entry.target_id,
                    "cdp_session_id": entry.cdp_session_id,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        push_ms = (time.perf_counter() - push_started) * 1000
        node_ids = _integers(pushed.get("nodeIds") if isinstance(pushed, Mapping) else ())
        if not node_ids or node_ids[0] <= 0:
            self._failures += 1
            raise BrowserSelectorNotFound(
                "CDP returned no frontend node for browser selector",
                details={"selector_ref": entry.ref.opaque_ref, "backend_node_id": entry.backend_node_id},
            )
        frontend_node_id = node_ids[0]

        resolve_started = time.perf_counter()
        try:
            resolved = cdp_runtime.send(
                "DOM.resolveNode",
                {"backendNodeId": entry.backend_node_id},
                cdp_session_id=entry.cdp_session_id,
                timeout_seconds=self.timeout_seconds,
            )
        except Exception as error:
            self._failures += 1
            raise BrowserSelectorNotFound(
                "CDP could not resolve browser selector backend node",
                details={
                    "selector_ref": entry.ref.opaque_ref,
                    "backend_node_id": entry.backend_node_id,
                    "error": f"{type(error).__name__}: {error}",
                },
            ) from error
        resolve_ms = (time.perf_counter() - resolve_started) * 1000
        remote = resolved.get("object") if isinstance(resolved, Mapping) else None
        remote = remote if isinstance(remote, Mapping) else {}
        object_id = str(remote.get("objectId") or "")
        if not object_id:
            self._failures += 1
            raise BrowserSelectorNotFound(
                "CDP resolved browser selector without a remote object",
                details={"selector_ref": entry.ref.opaque_ref},
            )

        inspect_started = time.perf_counter()
        attributes: Mapping[str, Any] = {}
        text_preview = ""
        findings: list[str] = []
        try:
            inspected = cdp_runtime.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": object_id,
                    "functionDeclaration": "function(){const a={};for(const x of (this.attributes||[])){a[x.name]=x.value;}return {tag:(this.tagName||'').toLowerCase(),text:(this.innerText||this.textContent||'').slice(0,500),attributes:a};}",
                    "returnByValue": True,
                    "silent": True,
                },
                cdp_session_id=entry.cdp_session_id,
                timeout_seconds=self.timeout_seconds,
            )
            result = inspected.get("result") if isinstance(inspected, Mapping) else None
            value = result.get("value") if isinstance(result, Mapping) else None
            value = value if isinstance(value, Mapping) else {}
            attributes = value.get("attributes") if isinstance(value.get("attributes"), Mapping) else {}
            text_preview = str(value.get("text") or "")[:500]
            live_tag = str(value.get("tag") or "").lower()
            if live_tag and entry.tag_name and live_tag != entry.tag_name.lower():
                findings.append("live_tag_differs_from_capture")
        except Exception as error:
            findings.append(f"runtime_inspection_degraded:{type(error).__name__}")
        inspect_ms = (time.perf_counter() - inspect_started) * 1000
        after = self._assert_current(entry, revision, cdp_runtime, target_runtime, phase="after_resolve")
        if digest_json(before) != digest_json(after):
            self._stale_rejections += 1
            raise BrowserSelectorStale(
                "browser selector identity changed during live resolution",
                details={"before": before, "after": after, "selector_ref": entry.ref.opaque_ref},
            )
        self._resolutions += 1
        return BrowserLiveSelectorReceipt(
            entry=entry,
            revision=revision,
            current=resolution.current,
            frontend_node_id=frontend_node_id,
            backend_node_id=entry.backend_node_id,
            remote_object_id=object_id,
            remote_object_type=str(remote.get("type") or "object"),
            target_id=entry.target_id,
            cdp_session_id=entry.cdp_session_id,
            frame_id=entry.frame_id,
            tag_name=entry.tag_name,
            attributes=attributes,
            text_preview=text_preview,
            identity_before=before,
            identity_after=after,
            push_duration_ms=push_ms,
            resolve_duration_ms=resolve_ms,
            inspect_duration_ms=inspect_ms,
            resolved_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            findings=tuple(findings),
        )

    def audit_revision(
        self,
        revision: BrowserSelectorMapRevision,
        *,
        resolve_from_store: Any,
        cdp_runtime: BrowserSelectorCdpPort,
        target_runtime: BrowserSelectorTargetPort,
        max_entries: int = 16,
        fail_closed: bool = False,
    ) -> BrowserSelectorProbeAudit:
        selected = self._sample_entries(revision, max_entries=max_entries)
        receipts: list[BrowserLiveSelectorReceipt] = []
        failures: list[Mapping[str, Any]] = []
        for entry in selected:
            try:
                resolution = resolve_from_store(
                    entry.ref.opaque_ref,
                    expected_identity=revision.identity,
                    require_current=True,
                )
                receipts.append(self.resolve(
                    resolution,
                    cdp_runtime=cdp_runtime,
                    target_runtime=target_runtime,
                ))
            except Exception as error:
                failures.append({
                    "selector_ref": entry.ref.opaque_ref,
                    "target_id": entry.target_id,
                    "cdp_session_id": entry.cdp_session_id,
                    "frame_id": entry.frame_id,
                    "error": f"{type(error).__name__}: {error}",
                })
                if fail_closed:
                    raise
        audit = BrowserSelectorProbeAudit(
            revision_id=revision.revision_id,
            attempted=len(selected),
            resolved=len(receipts),
            failed=len(failures),
            target_count=len({item.entry.target_id for item in receipts}),
            frame_count=len({item.entry.frame_id for item in receipts if item.entry.frame_id}),
            shadow_count=sum(bool(item.entry.shadow_path) for item in receipts),
            executable_coverage=len(receipts) / max(1, len(selected)),
            receipts=tuple(receipts),
            failures=tuple(failures),
        )
        if fail_closed and not audit.valid:
            raise BrowserSelectorNotFound(
                "live selector audit did not resolve every sampled selector",
                details=audit.to_dict(),
            )
        return audit

    def _assert_current(
        self,
        entry: BrowserSelectorEntry,
        revision: BrowserSelectorMapRevision,
        cdp_runtime: BrowserSelectorCdpPort,
        target_runtime: BrowserSelectorTargetPort,
        *,
        phase: str,
    ) -> dict[str, Any]:
        target_snapshot = target_runtime.snapshot()
        cdp_snapshot = cdp_runtime.snapshot()
        target_generation = int(getattr(target_snapshot, "generation", -1))
        cdp_generation = int(getattr(cdp_snapshot, "generation", -1))
        if revision.stale:
            self._stale_rejections += 1
            raise BrowserSelectorStale(
                "browser selector revision is stale",
                details={"revision_id": revision.revision_id, "reason": revision.stale_reason},
            )
        if target_generation != revision.identity.target_generation or cdp_generation != revision.identity.cdp_generation:
            self._stale_rejections += 1
            raise BrowserSelectorStale(
                f"browser selector generation changed {phase}",
                details={
                    "revision_id": revision.revision_id,
                    "expected_target_generation": revision.identity.target_generation,
                    "actual_target_generation": target_generation,
                    "expected_cdp_generation": revision.identity.cdp_generation,
                    "actual_cdp_generation": cdp_generation,
                },
            )
        if str(target_runtime.active_target_id) != revision.identity.target_id:
            self._identity_rejections += 1
            raise BrowserSelectorIdentityMismatch(
                f"active browser target changed {phase}",
                details={
                    "expected": revision.identity.target_id,
                    "actual": str(target_runtime.active_target_id),
                },
            )
        try:
            target_session = target_runtime.cdp_session(entry.target_id)
        except Exception as error:
            self._stale_rejections += 1
            raise BrowserSelectorStale(
                f"selector target is detached {phase}",
                details={"target_id": entry.target_id},
            ) from error
        actual_session_id = str(getattr(target_session, "cdp_session_id", "") or "")
        if actual_session_id != entry.cdp_session_id:
            self._identity_rejections += 1
            raise BrowserSelectorIdentityMismatch(
                f"selector CDP session changed {phase}",
                details={"expected": entry.cdp_session_id, "actual": actual_session_id},
            )
        return {
            "revision_id": revision.revision_id,
            "identity_digest": revision.identity.digest,
            "active_target_id": str(target_runtime.active_target_id),
            "entry_target_id": entry.target_id,
            "entry_cdp_session_id": entry.cdp_session_id,
            "target_generation": target_generation,
            "cdp_generation": cdp_generation,
            "document_loader_id": revision.identity.document_loader_id,
        }

    def _sample_entries(
        self,
        revision: BrowserSelectorMapRevision,
        *,
        max_entries: int,
    ) -> tuple[BrowserSelectorEntry, ...]:
        limit = max(0, int(max_entries))
        if not limit:
            return ()
        ordered = sorted(
            revision.entries,
            key=lambda item: (
                not bool(item.shadow_path),
                not bool(item.iframe_path),
                item.target_id == revision.identity.target_id,
                not item.interactive,
                item.ref.selector_index,
            ),
        )
        selected: list[BrowserSelectorEntry] = []
        target_seen: set[str] = set()
        for entry in ordered:
            if entry.target_id not in target_seen:
                selected.append(entry)
                target_seen.add(entry.target_id)
            if len(selected) >= limit:
                return tuple(selected)
        for entry in ordered:
            if entry in selected:
                continue
            selected.append(entry)
            if len(selected) >= limit:
                break
        return tuple(selected)

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserLiveSelectorProbeRuntime",
            "owner_unit": "M1-S04B-02",
            "selector_owner": "BrowserSelectorMapStore/M1-04B",
            "target_session_owner": "BrowserTargetRuntime/M1-04A",
            "disabled": self.disabled,
            "resolutions": self._resolutions,
            "failures": self._failures,
            "stale_rejections": self._stale_rejections,
            "identity_rejections": self._identity_rejections,
        }


def _integers(value: Any) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    result: list[int] = []
    for item in value:
        try:
            result.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(result)
