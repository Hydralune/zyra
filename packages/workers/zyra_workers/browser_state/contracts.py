from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Mapping
from uuid import uuid4

from zyra_core import now_iso, to_jsonable

from .models import EnhancedDOMTreeNode, SerializedDOMState
from .text import estimate_tokens


def state_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def digest_json(value: Any) -> str:
    encoded = json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class CaptureCompleteness(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    FAILED = "failed"


class SelectorStaleReason(StrEnum):
    DOCUMENT_UPDATED = "document_updated"
    FRAME_NAVIGATED = "frame_navigated"
    EXECUTION_CONTEXT_CLEARED = "execution_context_cleared"
    TARGET_DETACHED = "target_detached"
    FOCUS_CHANGED = "focus_changed"
    VIEWPORT_CHANGED = "viewport_changed"
    NAVIGATION = "navigation"
    DOM_MUTATION = "dom_mutation"
    SUPERSEDED = "superseded"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class BrowserDomCaptureRequest:
    run_id: str
    task_id: str
    canonical_session_id: str
    browser_session_id: str
    worker_request_id: str
    target_id: str
    cdp_session_id: str
    session_revision: int
    target_generation: int
    cdp_generation: int
    node_id: str = ""
    step_index: int = 0
    document_loader_id: str = ""
    previous_capture_id: str = ""
    constraints: Mapping[str, Any] = field(default_factory=dict)
    capture_id: str = field(default_factory=lambda: state_id("domcap"))
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        required = {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "canonical_session_id": self.canonical_session_id,
            "browser_session_id": self.browser_session_id,
            "worker_request_id": self.worker_request_id,
            "target_id": self.target_id,
            "cdp_session_id": self.cdp_session_id,
        }
        missing = sorted(name for name, value in required.items() if not str(value))
        if missing:
            raise ValueError(f"browser DOM capture identity is incomplete: {', '.join(missing)}")
        if min(self.session_revision, self.target_generation, self.cdp_generation, self.step_index) < 0:
            raise ValueError("browser DOM capture generations cannot be negative")
        object.__setattr__(self, "constraints", dict(self.constraints))

    @property
    def identity_digest(self) -> str:
        return digest_json(self.identity_dict())

    def identity_dict(self) -> dict[str, Any]:
        return {
            "canonical_session_id": self.canonical_session_id,
            "browser_session_id": self.browser_session_id,
            "session_revision": self.session_revision,
            "target_id": self.target_id,
            "target_generation": self.target_generation,
            "cdp_session_id": self.cdp_session_id,
            "cdp_generation": self.cdp_generation,
            "document_loader_id": self.document_loader_id,
        }

    def public_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "node_id": self.node_id,
            "worker_request_id": self.worker_request_id,
            "step_index": self.step_index,
            "identity": self.identity_dict(),
            "identity_digest": self.identity_digest,
            "previous_capture_id": self.previous_capture_id,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserViewportState:
    width: float
    height: float
    device_pixel_ratio: float = 1.0
    scroll_x: float = 0.0
    scroll_y: float = 0.0
    document_width: float = 0.0
    document_height: float = 0.0

    @property
    def pages_above(self) -> float:
        return self.scroll_y / self.height if self.height > 0 else 0.0

    @property
    def pages_below(self) -> float:
        remaining = max(0.0, self.document_height - self.scroll_y - self.height)
        return remaining / self.height if self.height > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserFrameState:
    frame_id: str
    parent_frame_id: str = ""
    target_id: str = ""
    cdp_session_id: str = ""
    url: str = ""
    name: str = ""
    loader_id: str = ""
    security_origin: str = ""
    cross_origin: bool = False
    oopif: bool = False
    complete: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserCaptureMetrics:
    full_state_bytes: int
    full_state_tokens: int
    dom_nodes: int
    ax_nodes: int
    snapshot_nodes: int
    selector_candidates: int
    frames: int
    omitted_frames: int
    capture_duration_ms: float
    dom_duration_ms: float = 0.0
    ax_duration_ms: float = 0.0
    snapshot_duration_ms: float = 0.0
    compose_duration_ms: float = 0.0
    listener_detection_skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class BrowserDomCapture:
    request: BrowserDomCaptureRequest
    root: EnhancedDOMTreeNode
    serialized_state: SerializedDOMState
    raw_dom: Mapping[str, Any]
    raw_ax: Mapping[str, Any]
    raw_snapshot: Mapping[str, Any]
    frames: tuple[BrowserFrameState, ...]
    viewport: BrowserViewportState
    metrics: BrowserCaptureMetrics
    completeness: CaptureCompleteness = CaptureCompleteness.COMPLETE
    warnings: tuple[str, ...] = ()
    capture_id: str = ""
    state_digest: str = ""
    created_at: str = field(default_factory=now_iso)

    def __post_init__(self) -> None:
        self.capture_id = self.capture_id or self.request.capture_id
        if not self.state_digest:
            self.state_digest = digest_json({
                "identity": self.request.identity_dict(),
                "dom": self.raw_dom,
                "ax": self.raw_ax,
                "snapshot": self.raw_snapshot,
            })

    @property
    def selector_count(self) -> int:
        return len(self.serialized_state.selector_map)

    def public_dict(self) -> dict[str, Any]:
        return {
            "capture_id": self.capture_id,
            "request": self.request.public_dict(),
            "completeness": str(self.completeness),
            "warnings": list(self.warnings),
            "state_digest": self.state_digest,
            "selector_count": self.selector_count,
            "frames": [frame.to_dict() for frame in self.frames],
            "viewport": self.viewport.to_dict(),
            "metrics": self.metrics.to_dict(),
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SelectorMapIdentity:
    browser_session_id: str
    target_id: str
    target_generation: int
    cdp_session_id: str
    cdp_generation: int
    document_loader_id: str = ""

    @classmethod
    def from_request(cls, request: BrowserDomCaptureRequest) -> "SelectorMapIdentity":
        return cls(
            browser_session_id=request.browser_session_id,
            target_id=request.target_id,
            target_generation=request.target_generation,
            cdp_session_id=request.cdp_session_id,
            cdp_generation=request.cdp_generation,
            document_loader_id=request.document_loader_id,
        )

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserSelectorRef:
    revision_id: str
    selector_index: int
    backend_node_id: int
    identity_digest: str
    opaque_ref: str

    @classmethod
    def create(
        cls,
        *,
        revision_id: str,
        selector_index: int,
        backend_node_id: int,
        identity_digest: str,
    ) -> "BrowserSelectorRef":
        payload = f"{revision_id}:{selector_index}:{backend_node_id}:{identity_digest}"
        token = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]
        return cls(revision_id, selector_index, backend_node_id, identity_digest, f"zyra-selector:{token}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserSelectorEntry:
    ref: BrowserSelectorRef
    node_id: int
    backend_node_id: int
    target_id: str
    frame_id: str
    cdp_session_id: str
    tag_name: str
    role: str
    accessible_name: str
    text_preview: str
    xpath: str
    css_hint: str
    stable_hash: str
    attributes_digest: str
    visible: bool
    interactive: bool
    disabled: bool
    shadow_path: tuple[str, ...] = ()
    iframe_path: tuple[str, ...] = ()
    bounds: Mapping[str, Any] = field(default_factory=dict)
    paint_order: int | None = None
    provenance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "ref": self.ref.to_dict(),
            "bounds": dict(self.bounds),
            "shadow_path": list(self.shadow_path),
            "iframe_path": list(self.iframe_path),
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True, slots=True)
class BrowserSelectorMapRevision:
    revision_id: str
    revision: int
    identity: SelectorMapIdentity
    capture_id: str
    capture_digest: str
    entries: tuple[BrowserSelectorEntry, ...]
    stale: bool = False
    stale_reason: str = ""
    previous_revision_id: str = ""
    artifact_id: str = ""
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    @property
    def entry_by_ref(self) -> dict[str, BrowserSelectorEntry]:
        return {entry.ref.opaque_ref: entry for entry in self.entries}

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "revision": self.revision,
            "identity": self.identity.to_dict(),
            "capture_id": self.capture_id,
            "capture_digest": self.capture_digest,
            "entries": [entry.to_dict() for entry in self.entries],
            "stale": self.stale,
            "stale_reason": self.stale_reason,
            "previous_revision_id": self.previous_revision_id,
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserSelectorResolution:
    entry: BrowserSelectorEntry
    revision: BrowserSelectorMapRevision
    current: bool
    resolved_at: str = field(default_factory=now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry": self.entry.to_dict(),
            "revision_id": self.revision.revision_id,
            "revision": self.revision.revision,
            "current": self.current,
            "resolved_at": self.resolved_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserDisclosureBudget:
    max_context_tokens: int = 6000
    max_context_bytes: int = 24000
    max_inline_selectors: int = 120
    max_inline_facts: int = 80
    max_fact_chars: int = 500
    max_action_result_tokens: int = 1500
    raw_externalize_bytes: int = 12000
    preserve_recent_actions: int = 8

    def __post_init__(self) -> None:
        if min(
            self.max_context_tokens,
            self.max_context_bytes,
            self.max_inline_selectors,
            self.max_inline_facts,
            self.max_fact_chars,
            self.max_action_result_tokens,
            self.raw_externalize_bytes,
            self.preserve_recent_actions,
        ) <= 0:
            raise ValueError("browser disclosure budgets must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BrowserLowEntropyMetrics:
    full_state_bytes: int
    full_state_tokens: int
    disclosure_bytes: int
    disclosure_tokens: int
    repeated_fact_count: int
    unique_fact_count: int
    duplicate_fact_rate: float
    artifact_offload_bytes: int
    artifact_offload_ratio: float
    selector_total: int
    selector_inline: int
    selector_fidelity: float
    critical_fact_total: int
    critical_fact_preserved: int
    critical_fact_fidelity: float
    tool_pairs_total: int
    tool_pairs_preserved: int
    tool_pair_fidelity: float

    @property
    def compression_ratio(self) -> float:
        return self.disclosure_bytes / self.full_state_bytes if self.full_state_bytes else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "compression_ratio": self.compression_ratio}


@dataclass(frozen=True, slots=True)
class BrowserContextDisclosure:
    disclosure_id: str
    capture_id: str
    selector_revision_id: str
    text: str
    artifact_ids: tuple[str, ...]
    selector_refs: tuple[str, ...]
    facts: tuple[str, ...]
    metrics: BrowserLowEntropyMetrics
    trust: str = "external_untrusted"
    read_once: bool = True
    schema_version: int = 1
    created_at: str = field(default_factory=now_iso)

    @property
    def bytes(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "disclosure_id": self.disclosure_id,
            "capture_id": self.capture_id,
            "selector_revision_id": self.selector_revision_id,
            "text": self.text,
            "artifact_ids": list(self.artifact_ids),
            "selector_refs": list(self.selector_refs),
            "facts": list(self.facts),
            "metrics": self.metrics.to_dict(),
            "trust": self.trust,
            "read_once": self.read_once,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class SnapcompactFidelity:
    experimental: bool = True
    provider: str = ""
    model: str = ""
    ocr_fidelity: float = 0.0
    small_text_fidelity: float = 0.0
    coordinate_fidelity: float = 0.0
    interactive_element_fidelity: float = 0.0
    reversible: bool = False
    artifact_id: str = ""
    fallback: str = "deterministic_disclosure"

    def authoritative(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
