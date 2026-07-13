from __future__ import annotations

import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

from zyra_core import now_iso

from .contracts import (
    BrowserDomCapture,
    BrowserSelectorEntry,
    BrowserSelectorMapRevision,
    BrowserSelectorRef,
    BrowserSelectorResolution,
    SelectorMapIdentity,
    SelectorStaleReason,
    digest_json,
    state_id,
)
from .errors import (
    BrowserSelectorIdentityMismatch,
    BrowserSelectorMapConflict,
    BrowserSelectorMapMissing,
    BrowserSelectorNotFound,
    BrowserSelectorStale,
    BrowserSelectorStoreDisabled,
)
from .models import EnhancedDOMTreeNode
from .text import normalize_page_text


class BrowserSelectorMapStore:
    """Durable CAS owner for authoritative Browser selector generations.

    The store owns selector identity and freshness only. Chrome, target, CDP,
    task, context, artifact and memory state remain with their existing owners.
    """

    schema_version = 1

    def __init__(
        self,
        root: str | Path,
        *,
        disabled: bool = False,
        keep_revisions_per_target: int = 8,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / "browser-selector-maps.json"
        self.backup_path = self.root / "browser-selector-maps.backup.json"
        self.disabled = disabled
        self.keep_revisions_per_target = max(2, int(keep_revisions_per_target))
        self._lock = threading.RLock()
        self._revisions: dict[str, BrowserSelectorMapRevision] = {}
        self._latest: dict[str, str] = {}
        self._writes = 0
        self._conflicts = 0
        self._stale_marks = 0
        self._resolutions = 0
        self._resolution_failures = 0
        if not disabled:
            self.root.mkdir(parents=True, exist_ok=True)
            self._load()

    def _ensure_available(self) -> None:
        if self.disabled:
            raise BrowserSelectorStoreDisabled("browser selector map store is disabled")

    @staticmethod
    def target_key(browser_session_id: str, target_id: str) -> str:
        if not browser_session_id or not target_id:
            raise ValueError("browser session and target identity are required")
        return f"{browser_session_id}::{target_id}"

    def commit(
        self,
        capture: BrowserDomCapture,
        *,
        expected_previous_revision_id: str | None = None,
        artifact_id: str = "",
    ) -> BrowserSelectorMapRevision:
        self._ensure_available()
        identity = SelectorMapIdentity.from_request(capture.request)
        key = self.target_key(identity.browser_session_id, identity.target_id)
        with self._lock:
            current_id = self._latest.get(key, "")
            current = self._revisions.get(current_id)
            if expected_previous_revision_id is not None and current_id != expected_previous_revision_id:
                self._conflicts += 1
                raise BrowserSelectorMapConflict(
                    "selector map CAS precondition failed",
                    details={
                        "expected_previous_revision_id": expected_previous_revision_id,
                        "actual_previous_revision_id": current_id,
                        "target_key": key,
                    },
                )
            if current is not None:
                if _identity_older(identity, current.identity):
                    self._conflicts += 1
                    raise BrowserSelectorMapConflict(
                        "older DOM generation cannot replace the current selector map",
                        details={"current": current.identity.to_dict(), "proposed": identity.to_dict()},
                    )
                if current.capture_digest == capture.state_digest and current.identity == identity and not current.stale:
                    return current

            revision_id = state_id("selrev")
            revision_number = 1 if current is None else current.revision + 1
            entries = self._entries_from_capture(
                capture,
                identity=identity,
                revision_id=revision_id,
            )
            if current is not None and not current.stale:
                self._revisions[current.revision_id] = replace(
                    current,
                    stale=True,
                    stale_reason=str(SelectorStaleReason.SUPERSEDED),
                    updated_at=now_iso(),
                )
            revision = BrowserSelectorMapRevision(
                revision_id=revision_id,
                revision=revision_number,
                identity=identity,
                capture_id=capture.capture_id,
                capture_digest=capture.state_digest,
                entries=entries,
                previous_revision_id=current_id,
                artifact_id=artifact_id,
            )
            self._revisions[revision_id] = revision
            self._latest[key] = revision_id
            self._prune_locked(key)
            self._persist_locked()
            self._writes += 1
            return revision

    def latest(
        self,
        browser_session_id: str,
        target_id: str,
        *,
        require_current: bool = True,
    ) -> BrowserSelectorMapRevision:
        self._ensure_available()
        key = self.target_key(browser_session_id, target_id)
        with self._lock:
            revision = self._revisions.get(self._latest.get(key, ""))
        if revision is None:
            raise BrowserSelectorMapMissing(
                "no selector map exists for browser target",
                details={"browser_session_id": browser_session_id, "target_id": target_id},
            )
        if require_current and revision.stale:
            raise BrowserSelectorStale(
                "latest selector map is stale",
                details={"revision_id": revision.revision_id, "reason": revision.stale_reason},
            )
        return revision

    def revision(self, revision_id: str) -> BrowserSelectorMapRevision:
        self._ensure_available()
        with self._lock:
            revision = self._revisions.get(revision_id)
        if revision is None:
            raise BrowserSelectorMapMissing(
                f"selector map revision {revision_id} does not exist",
                details={"revision_id": revision_id},
            )
        return revision

    def resolve(
        self,
        selector_ref: str | BrowserSelectorRef,
        *,
        expected_identity: SelectorMapIdentity | None = None,
        require_current: bool = True,
    ) -> BrowserSelectorResolution:
        self._ensure_available()
        opaque_ref = selector_ref.opaque_ref if isinstance(selector_ref, BrowserSelectorRef) else str(selector_ref)
        with self._lock:
            revision: BrowserSelectorMapRevision | None = None
            entry: BrowserSelectorEntry | None = None
            for candidate in self._revisions.values():
                selected = candidate.entry_by_ref.get(opaque_ref)
                if selected is not None:
                    revision = candidate
                    entry = selected
                    break
        if revision is None or entry is None:
            self._resolution_failures += 1
            raise BrowserSelectorNotFound(
                "selector reference is not present in the authoritative store",
                details={"selector_ref": opaque_ref},
            )
        if expected_identity is not None and revision.identity != expected_identity:
            self._resolution_failures += 1
            raise BrowserSelectorIdentityMismatch(
                "selector reference belongs to another browser generation",
                details={
                    "expected": expected_identity.to_dict(),
                    "actual": revision.identity.to_dict(),
                    "selector_ref": opaque_ref,
                },
            )
        key = self.target_key(revision.identity.browser_session_id, revision.identity.target_id)
        with self._lock:
            current_id = self._latest.get(key, "")
        current = current_id == revision.revision_id and not revision.stale
        if require_current and not current:
            self._resolution_failures += 1
            raise BrowserSelectorStale(
                "selector reference is stale",
                details={
                    "selector_ref": opaque_ref,
                    "revision_id": revision.revision_id,
                    "latest_revision_id": current_id,
                    "reason": revision.stale_reason or "superseded",
                },
            )
        self._resolutions += 1
        return BrowserSelectorResolution(entry=entry, revision=revision, current=current)

    def mark_stale(
        self,
        browser_session_id: str,
        target_id: str,
        *,
        reason: SelectorStaleReason | str,
        expected_revision_id: str = "",
    ) -> BrowserSelectorMapRevision:
        self._ensure_available()
        key = self.target_key(browser_session_id, target_id)
        with self._lock:
            revision_id = self._latest.get(key, "")
            revision = self._revisions.get(revision_id)
            if revision is None:
                raise BrowserSelectorMapMissing("cannot mark a missing selector map stale")
            if expected_revision_id and revision_id != expected_revision_id:
                self._conflicts += 1
                raise BrowserSelectorMapConflict(
                    "selector stale CAS precondition failed",
                    details={"expected_revision_id": expected_revision_id, "actual_revision_id": revision_id},
                )
            if revision.stale and revision.stale_reason == str(reason):
                return revision
            stale = replace(revision, stale=True, stale_reason=str(reason), updated_at=now_iso())
            self._revisions[revision_id] = stale
            self._persist_locked()
            self._stale_marks += 1
            return stale

    def mark_session_stale(self, browser_session_id: str, *, reason: str) -> tuple[str, ...]:
        self._ensure_available()
        changed: list[str] = []
        with self._lock:
            targets = [
                revision.identity.target_id
                for revision in self._revisions.values()
                if revision.identity.browser_session_id == browser_session_id
                and self._latest.get(self.target_key(browser_session_id, revision.identity.target_id)) == revision.revision_id
                and not revision.stale
            ]
        for target_id in sorted(set(targets)):
            changed.append(self.mark_stale(browser_session_id, target_id, reason=reason).revision_id)
        return tuple(changed)

    def list_revisions(
        self,
        *,
        browser_session_id: str = "",
        target_id: str = "",
    ) -> tuple[BrowserSelectorMapRevision, ...]:
        self._ensure_available()
        with self._lock:
            revisions = tuple(self._revisions.values())
        if browser_session_id:
            revisions = tuple(item for item in revisions if item.identity.browser_session_id == browser_session_id)
        if target_id:
            revisions = tuple(item for item in revisions if item.identity.target_id == target_id)
        return tuple(sorted(revisions, key=lambda item: (item.created_at, item.revision)))

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            revisions = tuple(self._revisions.values())
            return {
                "schema_version": self.schema_version,
                "owner": "BrowserSelectorMapStore",
                "owner_unit": "M1-S04B-01",
                "disabled": self.disabled,
                "path": str(self.path),
                "revision_count": len(revisions),
                "current_count": sum(not item.stale for item in revisions),
                "entry_count": sum(len(item.entries) for item in revisions),
                "writes": self._writes,
                "conflicts": self._conflicts,
                "stale_marks": self._stale_marks,
                "resolutions": self._resolutions,
                "resolution_failures": self._resolution_failures,
                "latest": dict(self._latest),
            }

    def _entries_from_capture(
        self,
        capture: BrowserDomCapture,
        *,
        identity: SelectorMapIdentity,
        revision_id: str,
    ) -> tuple[BrowserSelectorEntry, ...]:
        result: list[BrowserSelectorEntry] = []
        used_refs: set[str] = set()
        for selector_index, (source_index, node) in enumerate(
            sorted(capture.serialized_state.selector_map.items(), key=lambda item: (int(item[0]), item[1].backend_node_id)),
            start=1,
        ):
            if not isinstance(node, EnhancedDOMTreeNode) or node.backend_node_id <= 0:
                continue
            ref = BrowserSelectorRef.create(
                revision_id=revision_id,
                selector_index=selector_index,
                backend_node_id=node.backend_node_id,
                identity_digest=identity.digest,
            )
            if ref.opaque_ref in used_refs:
                raise BrowserSelectorMapConflict("selector opaque reference collision")
            used_refs.add(ref.opaque_ref)
            role = str(getattr(node.ax_node, "role", "") or node.attributes.get("role", ""))
            accessible_name = str(getattr(node.ax_node, "name", "") or node.attributes.get("aria-label", ""))
            bounds = getattr(node.snapshot_node, "bounds", None)
            result.append(BrowserSelectorEntry(
                ref=ref,
                node_id=node.node_id,
                backend_node_id=node.backend_node_id,
                target_id=str(node.target_id or identity.target_id),
                frame_id=str(node.frame_id or ""),
                cdp_session_id=str(node.session_id or identity.cdp_session_id),
                tag_name=node.tag_name,
                role=role,
                accessible_name=normalize_page_text(accessible_name, limit=300),
                text_preview=normalize_page_text(_node_text(node), limit=300),
                xpath=str(getattr(node, "xpath", "") or ""),
                css_hint=_css_hint(node),
                stable_hash=str(node.compute_stable_hash()),
                attributes_digest=digest_json(node.attributes),
                visible=node.is_visible is not False,
                interactive=True,
                disabled=_disabled(node),
                shadow_path=_shadow_path(node),
                iframe_path=_iframe_path(node),
                bounds=bounds.to_dict() if bounds else {},
                paint_order=getattr(node.snapshot_node, "paint_order", None),
                provenance=(
                    "browser-use:DOMTreeSerializer",
                    f"source-index:{source_index}",
                    f"capture:{capture.capture_id}",
                ),
            ))
        return tuple(result)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            self._restore(payload)
        except Exception:
            if self.backup_path.exists():
                payload = json.loads(self.backup_path.read_text(encoding="utf-8"))
                self._restore(payload)
            else:
                raise

    def _restore(self, payload: Mapping[str, Any]) -> None:
        if int(payload.get("schema_version") or 0) != self.schema_version:
            raise ValueError("unsupported selector store schema version")
        revisions: dict[str, BrowserSelectorMapRevision] = {}
        for raw in payload.get("revisions") or ():
            revision = _revision_from_dict(raw)
            revisions[revision.revision_id] = revision
        latest = {str(key): str(value) for key, value in dict(payload.get("latest") or {}).items()}
        for key, revision_id in latest.items():
            if revision_id not in revisions:
                raise ValueError(f"latest selector revision {revision_id} is missing for {key}")
        self._revisions = revisions
        self._latest = latest

    def _persist_locked(self) -> None:
        payload = {
            "schema_version": self.schema_version,
            "updated_at": now_iso(),
            "latest": dict(self._latest),
            "revisions": [item.to_dict() for item in self._revisions.values()],
        }
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f".tmp-{os.getpid()}-{threading.get_ident()}")
        temporary.write_text(encoded, encoding="utf-8")
        if self.path.exists():
            self.backup_path.write_bytes(self.path.read_bytes())
        temporary.replace(self.path)

    def _prune_locked(self, key: str) -> None:
        revisions = [
            revision for revision in self._revisions.values()
            if self.target_key(revision.identity.browser_session_id, revision.identity.target_id) == key
        ]
        revisions.sort(key=lambda item: (item.revision, item.created_at), reverse=True)
        for revision in revisions[self.keep_revisions_per_target:]:
            if revision.revision_id != self._latest.get(key):
                self._revisions.pop(revision.revision_id, None)


def _identity_older(proposed: SelectorMapIdentity, current: SelectorMapIdentity) -> bool:
    if proposed.browser_session_id != current.browser_session_id or proposed.target_id != current.target_id:
        return False
    return (proposed.target_generation, proposed.cdp_generation) < (
        current.target_generation,
        current.cdp_generation,
    )


def _node_text(node: EnhancedDOMTreeNode) -> str:
    values: list[str] = []
    stack = list(reversed(node.children_and_shadow_roots))
    while stack and len(values) < 24:
        current = stack.pop()
        if current.node_value:
            values.append(current.node_value)
        stack.extend(reversed(current.children_and_shadow_roots))
    return " ".join(values)


def _css_hint(node: EnhancedDOMTreeNode) -> str:
    tag = node.tag_name or "*"
    element_id = node.attributes.get("id", "")
    if element_id and all(character.isalnum() or character in "-_" for character in element_id):
        return f"{tag}#{element_id}"
    test_id = node.attributes.get("data-testid", "")
    if test_id:
        return f'{tag}[data-testid="{test_id}"]'
    name = node.attributes.get("name", "")
    if name:
        return f'{tag}[name="{name}"]'
    return tag


def _disabled(node: EnhancedDOMTreeNode) -> bool:
    if "disabled" in node.attributes:
        return True
    if node.attributes.get("aria-disabled", "").casefold() == "true":
        return True
    if node.ax_node:
        for item in node.ax_node.properties or ():
            if str(item.name) == "disabled" and item.value is True:
                return True
    return False


def _shadow_path(node: EnhancedDOMTreeNode) -> tuple[str, ...]:
    result: list[str] = []
    current = node.parent_node
    while current is not None:
        if current.shadow_root_type:
            result.append(f"{current.backend_node_id}:{current.shadow_root_type}")
        current = current.parent_node
    return tuple(reversed(result))


def _iframe_path(node: EnhancedDOMTreeNode) -> tuple[str, ...]:
    result: list[str] = []
    current = node.parent_node
    while current is not None:
        if current.tag_name in {"iframe", "frame"}:
            result.append(f"{current.backend_node_id}:{current.frame_id or ''}")
        current = current.parent_node
    return tuple(reversed(result))


def _revision_from_dict(raw: Mapping[str, Any]) -> BrowserSelectorMapRevision:
    identity = SelectorMapIdentity(**dict(raw.get("identity") or {}))
    entries: list[BrowserSelectorEntry] = []
    for item in raw.get("entries") or ():
        item = dict(item)
        ref = BrowserSelectorRef(**dict(item.pop("ref")))
        entries.append(BrowserSelectorEntry(
            ref=ref,
            node_id=int(item.get("node_id") or 0),
            backend_node_id=int(item.get("backend_node_id") or 0),
            target_id=str(item.get("target_id") or ""),
            frame_id=str(item.get("frame_id") or ""),
            cdp_session_id=str(item.get("cdp_session_id") or ""),
            tag_name=str(item.get("tag_name") or ""),
            role=str(item.get("role") or ""),
            accessible_name=str(item.get("accessible_name") or ""),
            text_preview=str(item.get("text_preview") or ""),
            xpath=str(item.get("xpath") or ""),
            css_hint=str(item.get("css_hint") or ""),
            stable_hash=str(item.get("stable_hash") or ""),
            attributes_digest=str(item.get("attributes_digest") or ""),
            visible=bool(item.get("visible")),
            interactive=bool(item.get("interactive")),
            disabled=bool(item.get("disabled")),
            shadow_path=tuple(item.get("shadow_path") or ()),
            iframe_path=tuple(item.get("iframe_path") or ()),
            bounds=dict(item.get("bounds") or {}),
            paint_order=item.get("paint_order"),
            provenance=tuple(item.get("provenance") or ()),
        ))
    return BrowserSelectorMapRevision(
        revision_id=str(raw.get("revision_id") or ""),
        revision=int(raw.get("revision") or 0),
        identity=identity,
        capture_id=str(raw.get("capture_id") or ""),
        capture_digest=str(raw.get("capture_digest") or ""),
        entries=tuple(entries),
        stale=bool(raw.get("stale")),
        stale_reason=str(raw.get("stale_reason") or ""),
        previous_revision_id=str(raw.get("previous_revision_id") or ""),
        artifact_id=str(raw.get("artifact_id") or ""),
        created_at=str(raw.get("created_at") or now_iso()),
        updated_at=str(raw.get("updated_at") or now_iso()),
    )
