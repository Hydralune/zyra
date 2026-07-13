from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .contracts import BrowserDomCaptureRequest
from .errors import BrowserDomCaptureFailed
from .snapshot_decoder import snapshot_node_count


@dataclass(frozen=True, slots=True)
class BrowserCapturePolicyDecision:
    allowed: bool
    phase: str
    reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    raw_bytes: int
    dom_nodes: int
    ax_nodes: int
    snapshot_nodes: int
    frames: int
    limits: Mapping[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "phase": self.phase,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
            "raw_bytes": self.raw_bytes,
            "dom_nodes": self.dom_nodes,
            "ax_nodes": self.ax_nodes,
            "snapshot_nodes": self.snapshot_nodes,
            "frames": self.frames,
            "limits": dict(self.limits),
        }


class BrowserDomCapturePolicy:
    """Deterministic admission/size boundary for live browser read-state."""

    def __init__(
        self,
        *,
        max_raw_bytes: int = 32 * 1024 * 1024,
        max_dom_nodes: int = 250_000,
        max_ax_nodes: int = 250_000,
        max_snapshot_nodes: int = 500_000,
        max_frames: int = 128,
        max_constraint_items: int = 256,
        blocked_schemes: Sequence[str] = ("file", "filesystem", "chrome", "chrome-extension", "devtools"),
        disabled: bool = False,
    ) -> None:
        limits = (max_raw_bytes, max_dom_nodes, max_ax_nodes, max_snapshot_nodes, max_frames, max_constraint_items)
        if min(limits) <= 0:
            raise ValueError("browser capture policy limits must be positive")
        self.max_raw_bytes = max_raw_bytes
        self.max_dom_nodes = max_dom_nodes
        self.max_ax_nodes = max_ax_nodes
        self.max_snapshot_nodes = max_snapshot_nodes
        self.max_frames = max_frames
        self.max_constraint_items = max_constraint_items
        self.blocked_schemes = frozenset(str(item).lower() for item in blocked_schemes if str(item))
        self.disabled = disabled
        self._admissions = 0
        self._rejections = 0
        self._warnings = 0
        self._raw_bytes = 0

    @property
    def limits(self) -> dict[str, int]:
        return {
            "max_raw_bytes": self.max_raw_bytes,
            "max_dom_nodes": self.max_dom_nodes,
            "max_ax_nodes": self.max_ax_nodes,
            "max_snapshot_nodes": self.max_snapshot_nodes,
            "max_frames": self.max_frames,
            "max_constraint_items": self.max_constraint_items,
        }

    def admit_request(self, request: BrowserDomCaptureRequest) -> BrowserCapturePolicyDecision:
        if self.disabled:
            raise BrowserDomCaptureFailed("browser DOM capture policy is disabled")
        reasons: list[str] = []
        warnings: list[str] = []
        required = {
            "run_id": request.run_id,
            "task_id": request.task_id,
            "canonical_session_id": request.canonical_session_id,
            "browser_session_id": request.browser_session_id,
            "worker_request_id": request.worker_request_id,
            "target_id": request.target_id,
            "cdp_session_id": request.cdp_session_id,
        }
        missing = tuple(key for key, value in required.items() if not str(value))
        if missing:
            reasons.append("missing required capture identity: " + ", ".join(missing))
        if request.session_revision < 0 or request.target_generation < 0 or request.cdp_generation < 0:
            reasons.append("capture identity generation/revision cannot be negative")
        if len(request.constraints) > self.max_constraint_items:
            reasons.append(
                f"capture constraints exceed limit {self.max_constraint_items}: {len(request.constraints)}"
            )
        backend = str(request.constraints.get("browser_backend") or "zyra-browser-productized")
        if backend != "zyra-browser-productized":
            reasons.append(f"authoritative DOM capture rejects backend {backend!r}")
        if request.constraints.get("browser_state_capture") is False:
            reasons.append("browser state capture was explicitly disabled for a path that requires it")
        if request.constraints.get("browser_dom_fail_open") is True:
            reasons.append("browser DOM fail-open is forbidden")
        decision = BrowserCapturePolicyDecision(
            allowed=not reasons,
            phase="request",
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            raw_bytes=0,
            dom_nodes=0,
            ax_nodes=0,
            snapshot_nodes=0,
            frames=0,
            limits=self.limits,
        )
        return self._record(decision)

    def inspect_responses(
        self,
        request: BrowserDomCaptureRequest,
        *,
        dom: Mapping[str, Any],
        ax: Mapping[str, Any],
        snapshot: Mapping[str, Any],
        frame_tree: Mapping[str, Any],
    ) -> BrowserCapturePolicyDecision:
        if self.disabled:
            raise BrowserDomCaptureFailed("browser DOM capture policy is disabled")
        reasons: list[str] = []
        warnings: list[str] = []
        raw_bytes = _json_bytes((dom, ax, snapshot, frame_tree))
        dom_nodes = _dom_node_count(dom.get("root"))
        ax_nodes = len(ax.get("nodes")) if isinstance(ax.get("nodes"), list) else 0
        snapshot_nodes = snapshot_node_count(snapshot)
        frames = _frame_count(frame_tree)
        if raw_bytes > self.max_raw_bytes:
            reasons.append(f"raw CDP state is {raw_bytes} bytes; limit is {self.max_raw_bytes}")
        if dom_nodes > self.max_dom_nodes:
            reasons.append(f"DOM node count is {dom_nodes}; limit is {self.max_dom_nodes}")
        if ax_nodes > self.max_ax_nodes:
            reasons.append(f"AX node count is {ax_nodes}; limit is {self.max_ax_nodes}")
        if snapshot_nodes > self.max_snapshot_nodes:
            reasons.append(f"DOMSnapshot node count is {snapshot_nodes}; limit is {self.max_snapshot_nodes}")
        if frames > self.max_frames:
            reasons.append(f"frame count is {frames}; limit is {self.max_frames}")
        if dom_nodes == 0:
            reasons.append("DOM.getDocument returned no traversable nodes")
        if snapshot_nodes == 0:
            reasons.append("DOMSnapshot.captureSnapshot returned no nodes")
        if ax_nodes == 0:
            reasons.append("Accessibility.getFullAXTree returned no nodes")
        root = dom.get("root") if isinstance(dom.get("root"), Mapping) else {}
        document_url = str(root.get("documentURL") or root.get("baseURL") or "")
        scheme = urlparse(document_url).scheme.lower()
        configured_schemes = request.constraints.get("allowed_schemes", ())
        if isinstance(configured_schemes, (str, bytes)):
            configured_schemes = (configured_schemes,)
        elif not isinstance(configured_schemes, Sequence):
            configured_schemes = ()
        allowed_schemes = {
            str(item).lower()
            for item in configured_schemes
            if str(item)
        }
        allow_blocked_scheme = bool(request.constraints.get("browser_allow_privileged_scheme"))
        allow_blocked_scheme = allow_blocked_scheme or scheme in allowed_schemes
        if scheme in self.blocked_schemes and not allow_blocked_scheme:
            reasons.append(f"DOM capture rejects privileged document scheme {scheme!r}")
        if not document_url:
            warnings.append("document_url_missing")
        if frames == 0:
            warnings.append("frame_tree_missing")
        if raw_bytes > self.max_raw_bytes * 0.8:
            warnings.append("raw_state_near_byte_limit")
        if snapshot_nodes > self.max_snapshot_nodes * 0.8:
            warnings.append("snapshot_near_node_limit")
        decision = BrowserCapturePolicyDecision(
            allowed=not reasons,
            phase="responses",
            reasons=tuple(reasons),
            warnings=tuple(warnings),
            raw_bytes=raw_bytes,
            dom_nodes=dom_nodes,
            ax_nodes=ax_nodes,
            snapshot_nodes=snapshot_nodes,
            frames=frames,
            limits=self.limits,
        )
        return self._record(decision)

    def require_allowed(self, decision: BrowserCapturePolicyDecision) -> None:
        if decision.allowed:
            return
        raise BrowserDomCaptureFailed(
            f"browser DOM capture policy rejected {decision.phase}: " + "; ".join(decision.reasons),
            details=decision.to_dict(),
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserDomCapturePolicy",
            "owner_unit": "M1-S04B-01",
            "disabled": self.disabled,
            "admissions": self._admissions,
            "rejections": self._rejections,
            "warnings": self._warnings,
            "raw_bytes": self._raw_bytes,
            "limits": self.limits,
            "blocked_schemes": sorted(self.blocked_schemes),
        }

    def _record(self, decision: BrowserCapturePolicyDecision) -> BrowserCapturePolicyDecision:
        self._admissions += decision.allowed
        self._rejections += not decision.allowed
        self._warnings += len(decision.warnings)
        self._raw_bytes += decision.raw_bytes
        return decision


def _json_bytes(values: Any) -> int:
    try:
        return len(json.dumps(values, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
    except (TypeError, ValueError, OverflowError) as error:
        raise BrowserDomCaptureFailed(
            "CDP browser state cannot be serialized for size admission",
            details={"error": f"{type(error).__name__}: {error}"},
        ) from error


def _dom_node_count(root: Any) -> int:
    if not isinstance(root, Mapping):
        return 0
    count = 0
    pending: list[Mapping[str, Any]] = [root]
    while pending:
        node = pending.pop()
        count += 1
        for key in ("children", "shadowRoots", "pseudoElements", "distributedNodes"):
            values = node.get(key)
            if isinstance(values, list):
                pending.extend(item for item in values if isinstance(item, Mapping))
        content = node.get("contentDocument")
        if isinstance(content, Mapping):
            pending.append(content)
    return count


def _frame_count(frame_tree: Mapping[str, Any]) -> int:
    root = frame_tree.get("frameTree")
    if not isinstance(root, Mapping):
        return 0
    count = 0
    pending: list[Mapping[str, Any]] = [root]
    while pending:
        item = pending.pop()
        frame = item.get("frame")
        count += isinstance(frame, Mapping)
        children = item.get("childFrames")
        if isinstance(children, list):
            pending.extend(child for child in children if isinstance(child, Mapping))
    return count
