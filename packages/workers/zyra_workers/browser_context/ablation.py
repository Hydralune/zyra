from __future__ import annotations

"""Bounded three-lane browser context ablation.

The structured disclosure remains the production lane.  Full state is an
evaluation baseline and the bitmap frame is explicitly experimental,
non-authoritative, and default-off.  The bitmap encoder is a dependency-free
monochrome evidence frame; it never substitutes for selector refs or DOM
state.  When no real OCR evaluator is supplied, OCR is reported unavailable
instead of silently treating string serialization as OCR.
"""

import hashlib
import json
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from zyra_core import ArtifactKind, ArtifactRef, now_iso, to_jsonable

from ..browser_state.contracts import (
    BrowserContextDisclosure,
    BrowserDomCapture,
    BrowserSelectorMapRevision,
    SnapcompactFidelity,
    digest_json,
    state_id,
)
from ..browser_state.dom_builder import dom_fact_candidates
from ..browser_state.text import estimate_tokens, normalize_page_text, normalized_fact_key


class BrowserAblationLane(StrEnum):
    FULL_STATE = "full_dom_state"
    STRUCTURED_DISCLOSURE = "structured_disclosure"
    BITMAP_FRAME = "bitmap_frame_experimental"


class BrowserAblationStatus(StrEnum):
    COMPLETE = "complete"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class BrowserAblationRequirements:
    required_facts: tuple[str, ...] = ()
    required_selector_refs: tuple[str, ...] = ()
    required_coordinates: tuple[tuple[float, float], ...] = ()
    required_interactive_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_facts": list(self.required_facts),
            "required_selector_refs": list(self.required_selector_refs),
            "required_coordinates": [list(item) for item in self.required_coordinates],
            "required_interactive_count": self.required_interactive_count,
        }

    @classmethod
    def from_constraints(
        cls,
        constraints: Mapping[str, Any],
        *,
        revision: BrowserSelectorMapRevision,
        disclosure: BrowserContextDisclosure,
    ) -> "BrowserAblationRequirements":
        raw = constraints.get("browser_context_ablation_requirements")
        raw = raw if isinstance(raw, Mapping) else {}
        facts = _strings(raw.get("facts"))
        selectors = _strings(raw.get("selector_refs"))
        if not selectors:
            selectors = tuple(disclosure.selector_refs[: min(8, len(disclosure.selector_refs))])
        coordinates: list[tuple[float, float]] = []
        for item in _sequence(raw.get("coordinates")):
            if isinstance(item, Mapping):
                coordinates.append((_float(item.get("x")), _float(item.get("y"))))
            elif isinstance(item, Sequence) and len(item) >= 2:
                coordinates.append((_float(item[0]), _float(item[1])))
        if not coordinates:
            for entry in revision.entries:
                bounds = entry.bounds
                if not isinstance(bounds, Mapping) or not bounds:
                    continue
                coordinates.append((
                    _float(bounds.get("x")) + _float(bounds.get("width")) / 2,
                    _float(bounds.get("y")) + _float(bounds.get("height")) / 2,
                ))
                if len(coordinates) >= 8:
                    break
        return cls(
            required_facts=facts,
            required_selector_refs=selectors,
            required_coordinates=tuple(coordinates),
            required_interactive_count=max(0, _integer(raw.get("interactive_count"), default=min(8, len(revision.entries)))),
        )


@dataclass(frozen=True, slots=True)
class BrowserAblationLaneMetrics:
    lane: BrowserAblationLane
    status: BrowserAblationStatus
    bytes: int
    tokens: int
    compression_ratio: float
    fact_fidelity: float
    selector_fidelity: float
    executable_selector_coverage: float
    ocr_fidelity: float
    ocr_available: bool
    small_text_fidelity: float
    coordinate_fidelity: float
    interactive_element_fidelity: float
    task_success: bool
    authoritative: bool
    reversible: bool
    artifact_ids: tuple[str, ...] = ()
    findings: tuple[str, ...] = ()
    evaluator: str = ""

    def __post_init__(self) -> None:
        for name in (
            "compression_ratio", "fact_fidelity", "selector_fidelity",
            "executable_selector_coverage", "ocr_fidelity", "small_text_fidelity",
            "coordinate_fidelity", "interactive_element_fidelity",
        ):
            value = float(getattr(self, name))
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between zero and one")

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": str(self.lane),
            "status": str(self.status),
            "bytes": self.bytes,
            "tokens": self.tokens,
            "compression_ratio": self.compression_ratio,
            "fact_fidelity": self.fact_fidelity,
            "selector_fidelity": self.selector_fidelity,
            "executable_selector_coverage": self.executable_selector_coverage,
            "ocr_fidelity": self.ocr_fidelity,
            "ocr_available": self.ocr_available,
            "small_text_fidelity": self.small_text_fidelity,
            "coordinate_fidelity": self.coordinate_fidelity,
            "interactive_element_fidelity": self.interactive_element_fidelity,
            "task_success": self.task_success,
            "authoritative": self.authoritative,
            "reversible": self.reversible,
            "artifact_ids": list(self.artifact_ids),
            "findings": list(self.findings),
            "evaluator": self.evaluator,
        }


@dataclass(frozen=True, slots=True)
class BrowserCompressionAblationReport:
    report_id: str
    capture_id: str
    disclosure_id: str
    selector_revision_id: str
    requirements: BrowserAblationRequirements
    lanes: tuple[BrowserAblationLaneMetrics, ...]
    default_lane: BrowserAblationLane
    experimental_lane_enabled: bool
    report_artifact: ArtifactRef | None = None
    bitmap_artifact: ArtifactRef | None = None
    created_at: str = field(default_factory=now_iso)

    @property
    def structured_wins_tokens(self) -> bool:
        full = self.lane(BrowserAblationLane.FULL_STATE)
        structured = self.lane(BrowserAblationLane.STRUCTURED_DISCLOSURE)
        return structured.tokens < full.tokens

    @property
    def default_path_unchanged(self) -> bool:
        return self.default_lane == BrowserAblationLane.STRUCTURED_DISCLOSURE

    def lane(self, name: BrowserAblationLane) -> BrowserAblationLaneMetrics:
        return next(item for item in self.lanes if item.lane == name)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "zyra.browser-context-ablation.v1",
            "report_id": self.report_id,
            "capture_id": self.capture_id,
            "disclosure_id": self.disclosure_id,
            "selector_revision_id": self.selector_revision_id,
            "requirements": self.requirements.to_dict(),
            "lanes": [item.to_dict() for item in self.lanes],
            "default_lane": str(self.default_lane),
            "experimental_lane_enabled": self.experimental_lane_enabled,
            "structured_wins_tokens": self.structured_wins_tokens,
            "default_path_unchanged": self.default_path_unchanged,
            "report_artifact": to_jsonable(self.report_artifact) if self.report_artifact else None,
            "bitmap_artifact": to_jsonable(self.bitmap_artifact) if self.bitmap_artifact else None,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class BrowserBitmapEvidenceFrame:
    width: int
    height: int
    cell_width: int
    cell_height: int
    payload: str
    coordinate_manifest: tuple[Mapping[str, Any], ...]
    fact_manifest: tuple[str, ...]
    selector_manifest: tuple[str, ...]

    @property
    def bytes(self) -> int:
        return len(self.payload.encode("ascii", errors="replace"))

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.payload.encode("ascii")).hexdigest()

    def to_dict(self, *, include_payload: bool = False) -> dict[str, Any]:
        value = {
            "schema": "zyra.browser-bitmap-evidence-frame.v1",
            "format": "P1",
            "width": self.width,
            "height": self.height,
            "cell_width": self.cell_width,
            "cell_height": self.cell_height,
            "bytes": self.bytes,
            "digest": self.digest,
            "coordinate_manifest": [dict(item) for item in self.coordinate_manifest],
            "fact_manifest": list(self.fact_manifest),
            "selector_manifest": list(self.selector_manifest),
            "authoritative": False,
        }
        if include_payload:
            value["payload"] = self.payload
        return value


class BrowserCompressionAblationRuntime:
    """Evaluate full, structured, and experimental bitmap representations."""

    def __init__(
        self,
        artifact_store: Any,
        *,
        ocr_evaluator: Callable[[str], Sequence[str]] | None = None,
        disabled: bool = False,
    ) -> None:
        self.artifact_store = artifact_store
        self.ocr_evaluator = ocr_evaluator
        self.disabled = disabled
        self._runs = 0
        self._bitmap_runs = 0
        self._failures = 0

    def enabled(self, constraints: Mapping[str, Any]) -> bool:
        value = constraints.get("browser_context_ablation")
        if isinstance(value, Mapping):
            return bool(value.get("enabled", True))
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def run(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        disclosure: BrowserContextDisclosure,
        *,
        constraints: Mapping[str, Any],
    ) -> BrowserCompressionAblationReport:
        if self.disabled:
            raise RuntimeError("browser compression ablation runtime is disabled")
        if not self.enabled(constraints):
            raise RuntimeError("browser compression ablation is default-off and was not enabled")
        requirements = BrowserAblationRequirements.from_constraints(
            constraints,
            revision=revision,
            disclosure=disclosure,
        )
        full_payload = self._full_payload(capture, revision)
        full_text = json.dumps(full_payload, ensure_ascii=False, sort_keys=True, default=str)
        structured_text = disclosure.text
        full_facts = tuple(dom_fact_candidates(capture.root, max_facts=1024))
        full_selectors = tuple(entry.ref.opaque_ref for entry in revision.entries)
        full_coordinates = self._coordinates(revision)
        structured_payload = _json_mapping(structured_text)
        structured_facts = _strings(structured_payload.get("facts"))
        structured_selectors = tuple(
            str(item.get("ref") or "")
            for item in _sequence(structured_payload.get("selectors"))
            if isinstance(item, Mapping) and str(item.get("ref") or "")
        )
        structured_coordinates = self._selector_coordinates(
            structured_selectors,
            revision,
        )
        full = self._lane_metrics(
            lane=BrowserAblationLane.FULL_STATE,
            raw=full_text,
            baseline_bytes=len(full_text.encode("utf-8")),
            facts=full_facts,
            selectors=full_selectors,
            coordinates=full_coordinates,
            interactive_count=len(revision.entries),
            requirements=requirements,
            authoritative=True,
            reversible=True,
            evaluator="zyra-full-state-baseline",
        )
        structured = self._lane_metrics(
            lane=BrowserAblationLane.STRUCTURED_DISCLOSURE,
            raw=structured_text,
            baseline_bytes=max(1, full.bytes),
            facts=structured_facts,
            selectors=structured_selectors,
            coordinates=structured_coordinates,
            interactive_count=len(structured_selectors),
            requirements=requirements,
            authoritative=True,
            reversible=False,
            evaluator="BrowserStateCompressor+BrowserDisclosureFidelityAuditor",
        )
        frame = self._bitmap_frame(capture, revision, disclosure)
        bitmap_artifact = self._write_bitmap(capture, frame)
        recognized: tuple[str, ...] = ()
        ocr_available = self.ocr_evaluator is not None
        ocr_findings: list[str] = []
        if self.ocr_evaluator is not None:
            try:
                recognized = tuple(str(item) for item in self.ocr_evaluator(frame.payload))
            except Exception as error:
                ocr_available = False
                ocr_findings.append(f"ocr_evaluator_failed:{type(error).__name__}")
        else:
            ocr_findings.append("ocr_evaluator_unavailable")
        ocr_fidelity = _set_fidelity(requirements.required_facts, recognized) if ocr_available else 0.0
        bitmap_base = self._lane_metrics(
            lane=BrowserAblationLane.BITMAP_FRAME,
            raw=frame.payload,
            baseline_bytes=max(1, full.bytes),
            facts=recognized,
            selectors=frame.selector_manifest,
            coordinates=tuple(
                (_float(item.get("x")), _float(item.get("y")))
                for item in frame.coordinate_manifest
            ),
            interactive_count=len(frame.selector_manifest),
            requirements=requirements,
            authoritative=False,
            reversible=False,
            evaluator=("configured_ocr_evaluator" if ocr_available else "no_ocr_evaluator"),
        )
        snapcompact = SnapcompactFidelity(
            experimental=True,
            provider="zyra-bitmap-evidence-frame",
            model="configured-ocr" if ocr_available else "unavailable",
            ocr_fidelity=ocr_fidelity,
            small_text_fidelity=bitmap_base.small_text_fidelity if ocr_available else 0.0,
            coordinate_fidelity=bitmap_base.coordinate_fidelity,
            interactive_element_fidelity=bitmap_base.interactive_element_fidelity,
            reversible=False,
            artifact_id=bitmap_artifact.artifact_id,
            fallback="deterministic_disclosure",
        )
        bitmap = BrowserAblationLaneMetrics(
            lane=bitmap_base.lane,
            status=(bitmap_base.status if ocr_available else BrowserAblationStatus.UNAVAILABLE),
            bytes=bitmap_base.bytes,
            tokens=bitmap_base.tokens,
            compression_ratio=bitmap_base.compression_ratio,
            fact_fidelity=bitmap_base.fact_fidelity if ocr_available else 0.0,
            selector_fidelity=bitmap_base.selector_fidelity,
            executable_selector_coverage=0.0,
            ocr_fidelity=ocr_fidelity,
            ocr_available=ocr_available,
            small_text_fidelity=snapcompact.small_text_fidelity,
            coordinate_fidelity=bitmap_base.coordinate_fidelity,
            interactive_element_fidelity=bitmap_base.interactive_element_fidelity,
            task_success=bitmap_base.task_success and ocr_available,
            authoritative=False,
            reversible=False,
            artifact_ids=(bitmap_artifact.artifact_id,),
            findings=tuple((*bitmap_base.findings, *ocr_findings, "bitmap_lane_never_supplies_selector_authority")),
            evaluator=bitmap_base.evaluator,
        )
        provisional = BrowserCompressionAblationReport(
            report_id=state_id("brablation"),
            capture_id=capture.capture_id,
            disclosure_id=disclosure.disclosure_id,
            selector_revision_id=revision.revision_id,
            requirements=requirements,
            lanes=(full, structured, bitmap),
            default_lane=BrowserAblationLane.STRUCTURED_DISCLOSURE,
            experimental_lane_enabled=True,
        )
        report_artifact = self._write_report(capture, provisional, frame, snapcompact)
        report = BrowserCompressionAblationReport(
            report_id=provisional.report_id,
            capture_id=provisional.capture_id,
            disclosure_id=provisional.disclosure_id,
            selector_revision_id=provisional.selector_revision_id,
            requirements=provisional.requirements,
            lanes=provisional.lanes,
            default_lane=provisional.default_lane,
            experimental_lane_enabled=True,
            report_artifact=report_artifact,
            bitmap_artifact=bitmap_artifact,
            created_at=provisional.created_at,
        )
        self._runs += 1
        self._bitmap_runs += 1
        return report

    def _lane_metrics(
        self,
        *,
        lane: BrowserAblationLane,
        raw: str,
        baseline_bytes: int,
        facts: Sequence[str],
        selectors: Sequence[str],
        coordinates: Sequence[tuple[float, float]],
        interactive_count: int,
        requirements: BrowserAblationRequirements,
        authoritative: bool,
        reversible: bool,
        evaluator: str,
    ) -> BrowserAblationLaneMetrics:
        size = len(raw.encode("utf-8"))
        fact_fidelity = _set_fidelity(requirements.required_facts, facts)
        selector_fidelity = _set_fidelity(requirements.required_selector_refs, selectors)
        coordinate_fidelity = _coordinate_fidelity(requirements.required_coordinates, coordinates)
        interactive_fidelity = min(1.0, interactive_count / max(1, requirements.required_interactive_count))
        small_required = tuple(item for item in requirements.required_facts if len(item) <= 32)
        small_fidelity = _set_fidelity(small_required, facts)
        success = all((
            fact_fidelity == 1.0,
            selector_fidelity == 1.0,
            coordinate_fidelity >= 0.95,
            interactive_fidelity == 1.0,
        ))
        findings = [] if success else ["task_requirements_not_fully_preserved"]
        return BrowserAblationLaneMetrics(
            lane=lane,
            status=BrowserAblationStatus.COMPLETE if success else BrowserAblationStatus.DEGRADED,
            bytes=size,
            tokens=estimate_tokens(raw),
            compression_ratio=min(1.0, size / max(1, baseline_bytes)),
            fact_fidelity=fact_fidelity,
            selector_fidelity=selector_fidelity,
            executable_selector_coverage=selector_fidelity if authoritative else 0.0,
            ocr_fidelity=0.0,
            ocr_available=False,
            small_text_fidelity=small_fidelity,
            coordinate_fidelity=coordinate_fidelity,
            interactive_element_fidelity=interactive_fidelity,
            task_success=success,
            authoritative=authoritative,
            reversible=reversible,
            findings=tuple(findings),
            evaluator=evaluator,
        )

    def _full_payload(self, capture: BrowserDomCapture, revision: BrowserSelectorMapRevision) -> dict[str, Any]:
        return {
            "capture": capture.public_dict(),
            "dom": capture.raw_dom,
            "ax": capture.raw_ax,
            "snapshot": capture.raw_snapshot,
            "frames": [item.to_dict() for item in capture.frames],
            "selector_revision": revision.to_dict(),
        }

    def _coordinates(self, revision: BrowserSelectorMapRevision) -> tuple[tuple[float, float], ...]:
        result: list[tuple[float, float]] = []
        for entry in revision.entries:
            bounds = entry.bounds
            if not isinstance(bounds, Mapping) or not bounds:
                continue
            result.append((
                _float(bounds.get("x")) + _float(bounds.get("width")) / 2,
                _float(bounds.get("y")) + _float(bounds.get("height")) / 2,
            ))
        return tuple(result)

    def _selector_coordinates(
        self,
        refs: Sequence[str],
        revision: BrowserSelectorMapRevision,
    ) -> tuple[tuple[float, float], ...]:
        by_ref = revision.entry_by_ref
        result: list[tuple[float, float]] = []
        for ref in refs:
            entry = by_ref.get(ref)
            if entry is None or not isinstance(entry.bounds, Mapping):
                continue
            result.append((
                _float(entry.bounds.get("x")) + _float(entry.bounds.get("width")) / 2,
                _float(entry.bounds.get("y")) + _float(entry.bounds.get("height")) / 2,
            ))
        return tuple(result)

    def _bitmap_frame(
        self,
        capture: BrowserDomCapture,
        revision: BrowserSelectorMapRevision,
        disclosure: BrowserContextDisclosure,
    ) -> BrowserBitmapEvidenceFrame:
        width = max(64, min(512, int(math.ceil(max(1.0, capture.viewport.width) / 4))))
        height = max(64, min(512, int(math.ceil(max(1.0, capture.viewport.height) / 4))))
        rows = [[0 for _ in range(width)] for _ in range(height)]
        manifest: list[Mapping[str, Any]] = []
        for entry in revision.entries[:512]:
            bounds = entry.bounds if isinstance(entry.bounds, Mapping) else {}
            x = max(0, min(width - 1, int(_float(bounds.get("x")) / max(1.0, capture.viewport.width) * width)))
            y = max(0, min(height - 1, int(_float(bounds.get("y")) / max(1.0, capture.viewport.height) * height)))
            w = max(1, min(width - x, int(max(1.0, _float(bounds.get("width"))) / max(1.0, capture.viewport.width) * width)))
            h = max(1, min(height - y, int(max(1.0, _float(bounds.get("height"))) / max(1.0, capture.viewport.height) * height)))
            for px in range(x, min(width, x + w)):
                rows[y][px] = 1
                rows[min(height - 1, y + h - 1)][px] = 1
            for py in range(y, min(height, y + h)):
                rows[py][x] = 1
                rows[py][min(width - 1, x + w - 1)] = 1
            manifest.append({
                "selector_ref": entry.ref.opaque_ref,
                "x": _float(bounds.get("x")) + _float(bounds.get("width")) / 2,
                "y": _float(bounds.get("y")) + _float(bounds.get("height")) / 2,
                "target_id": entry.target_id,
                "frame_id": entry.frame_id,
                "authoritative": False,
            })
        header = f"P1\n# Zyra experimental browser evidence {capture.capture_id}\n{width} {height}\n"
        body = "\n".join(" ".join(str(value) for value in row) for row in rows)
        return BrowserBitmapEvidenceFrame(
            width=width,
            height=height,
            cell_width=4,
            cell_height=4,
            payload=header + body + "\n",
            coordinate_manifest=tuple(manifest),
            fact_manifest=tuple(disclosure.facts),
            selector_manifest=tuple(disclosure.selector_refs),
        )

    def _write_bitmap(self, capture: BrowserDomCapture, frame: BrowserBitmapEvidenceFrame) -> ArtifactRef:
        return self.artifact_store.write_text(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            content=frame.payload,
            title=f"Experimental browser bitmap frame {capture.capture_id}",
            kind=ArtifactKind.FILE,
            extension=".pbm",
            producer_node_id=capture.request.node_id or None,
        )

    def _write_report(
        self,
        capture: BrowserDomCapture,
        report: BrowserCompressionAblationReport,
        frame: BrowserBitmapEvidenceFrame,
        snapcompact: SnapcompactFidelity,
    ) -> ArtifactRef:
        payload = {
            **report.to_dict(),
            "bitmap_frame": frame.to_dict(include_payload=False),
            "snapcompact_fidelity": snapcompact.to_dict(),
            "snapcompact_authoritative": snapcompact.authoritative(),
            "source_roles": {
                "browser-use": "primary",
                "claude-code-best": "supplementary_context_contract",
                "oh-my-pi": "supplementary_tool_pair_budget",
                "snapcompact": "experimental_default_off",
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        return self.artifact_store.write_text(
            run_id=capture.request.run_id,
            task_id=capture.request.task_id,
            content=text,
            title=f"Browser context ablation {capture.capture_id}",
            kind=ArtifactKind.REPORT,
            extension=".json",
            producer_node_id=capture.request.node_id or None,
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "owner": "BrowserCompressionAblationRuntime",
            "owner_unit": "M1-S04B-02",
            "default_lane": str(BrowserAblationLane.STRUCTURED_DISCLOSURE),
            "experimental_default_enabled": False,
            "ocr_evaluator_configured": self.ocr_evaluator is not None,
            "runs": self._runs,
            "bitmap_runs": self._bitmap_runs,
            "failures": self._failures,
        }


def _set_fidelity(required: Sequence[str], actual: Sequence[str]) -> float:
    if not required:
        return 1.0
    actual_keys = {normalized_fact_key(item) for item in actual if normalized_fact_key(item)}
    matched = 0
    for item in required:
        key = normalized_fact_key(item)
        if key in actual_keys or any(key in value or value in key for value in actual_keys if value):
            matched += 1
    return matched / len(required)


def _coordinate_fidelity(
    required: Sequence[tuple[float, float]],
    actual: Sequence[tuple[float, float]],
    *,
    tolerance: float = 2.0,
) -> float:
    if not required:
        return 1.0
    matched = 0
    for x, y in required:
        if any(abs(x - ax) <= tolerance and abs(y - ay) <= tolerance for ax, ay in actual):
            matched += 1
    return matched / len(required)


def _json_mapping(value: str) -> Mapping[str, Any]:
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, Mapping) else {}


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(item) for item in _sequence(value) if str(item)))


def _sequence(value: Any) -> tuple[Any, ...]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return ()


def _float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _integer(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)
