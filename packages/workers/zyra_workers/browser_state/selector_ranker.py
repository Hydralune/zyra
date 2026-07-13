from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import BrowserSelectorEntry, BrowserSelectorMapRevision
from .text import normalized_fact_key


@dataclass(frozen=True, slots=True)
class BrowserSelectorScore:
    entry: BrowserSelectorEntry
    score: float
    goal_overlap: float
    stable_identity: float
    visibility: float
    interactivity: float
    accessibility: float
    geometry: float
    penalties: tuple[str, ...]
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.entry.ref.opaque_ref,
            "score": self.score,
            "goal_overlap": self.goal_overlap,
            "stable_identity": self.stable_identity,
            "visibility": self.visibility,
            "interactivity": self.interactivity,
            "accessibility": self.accessibility,
            "geometry": self.geometry,
            "penalties": list(self.penalties),
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class BrowserSelectorSelection:
    selected: tuple[BrowserSelectorEntry, ...]
    scores: tuple[BrowserSelectorScore, ...]
    total: int
    visible_total: int
    interactive_total: int
    disabled_total: int
    frame_coverage: int
    selected_frame_coverage: int
    goal_matched: int

    @property
    def selector_fidelity(self) -> float:
        return len(self.selected) / self.total if self.total else 1.0

    @property
    def frame_fidelity(self) -> float:
        return self.selected_frame_coverage / self.frame_coverage if self.frame_coverage else 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_refs": [item.ref.opaque_ref for item in self.selected],
            "scores": [item.to_dict() for item in self.scores],
            "total": self.total,
            "selected": len(self.selected),
            "visible_total": self.visible_total,
            "interactive_total": self.interactive_total,
            "disabled_total": self.disabled_total,
            "frame_coverage": self.frame_coverage,
            "selected_frame_coverage": self.selected_frame_coverage,
            "goal_matched": self.goal_matched,
            "selector_fidelity": self.selector_fidelity,
            "frame_fidelity": self.frame_fidelity,
        }


class BrowserSelectorRanker:
    """Goal-aware, deterministic selector disclosure ranking.

    Ranking changes only what is disclosed to the next model context.  The
    durable selector revision remains complete and authoritative for 04C.
    """

    def __init__(
        self,
        *,
        minimum_per_frame: int = 1,
        prefer_visible: bool = True,
        include_disabled: bool = True,
    ) -> None:
        if minimum_per_frame < 0:
            raise ValueError("minimum_per_frame cannot be negative")
        self.minimum_per_frame = minimum_per_frame
        self.prefer_visible = prefer_visible
        self.include_disabled = include_disabled

    def select(
        self,
        revision: BrowserSelectorMapRevision,
        limit: int,
        *,
        goal: str = "",
        constraints: Sequence[str] = (),
        preferred_refs: Iterable[str] = (),
    ) -> BrowserSelectorSelection:
        if limit <= 0:
            raise ValueError("selector disclosure limit must be positive")
        preferred = {str(item) for item in preferred_refs if str(item)}
        query_tokens = _tokens((goal, *constraints))
        scored = tuple(
            self.score(entry, query_tokens=query_tokens, preferred=entry.ref.opaque_ref in preferred)
            for entry in revision.entries
            if self.include_disabled or not entry.disabled
        )
        ranked = sorted(scored, key=lambda item: (-item.score, item.entry.ref.selector_index, item.entry.ref.opaque_ref))
        selected_scores: list[BrowserSelectorScore] = []
        selected_refs: set[str] = set()

        if self.minimum_per_frame:
            frames = tuple(dict.fromkeys(item.entry.frame_id or "root" for item in ranked))
            for frame in frames:
                frame_items = [item for item in ranked if (item.entry.frame_id or "root") == frame]
                for item in frame_items[: self.minimum_per_frame]:
                    if len(selected_scores) >= limit:
                        break
                    selected_scores.append(item)
                    selected_refs.add(item.entry.ref.opaque_ref)
        for item in ranked:
            if len(selected_scores) >= limit:
                break
            if item.entry.ref.opaque_ref in selected_refs:
                continue
            selected_scores.append(item)
            selected_refs.add(item.entry.ref.opaque_ref)

        selected_scores.sort(key=lambda item: (-item.score, item.entry.ref.selector_index))
        selected = tuple(item.entry for item in selected_scores)
        all_frames = {item.frame_id or "root" for item in revision.entries}
        selected_frames = {item.frame_id or "root" for item in selected}
        return BrowserSelectorSelection(
            selected=selected,
            scores=tuple(selected_scores),
            total=len(revision.entries),
            visible_total=sum(item.visible for item in revision.entries),
            interactive_total=sum(item.interactive for item in revision.entries),
            disabled_total=sum(item.disabled for item in revision.entries),
            frame_coverage=len(all_frames),
            selected_frame_coverage=len(selected_frames),
            goal_matched=sum(item.goal_overlap > 0 for item in selected_scores),
        )

    def score(
        self,
        entry: BrowserSelectorEntry,
        *,
        query_tokens: frozenset[str],
        preferred: bool = False,
    ) -> BrowserSelectorScore:
        candidate_tokens = _tokens((
            entry.tag_name, entry.role, entry.accessible_name, entry.text_preview,
            entry.xpath, entry.css_hint,
        ))
        overlap = len(candidate_tokens & query_tokens) / max(1, len(query_tokens)) if query_tokens else 0.0
        stable_identity = 0.0
        reasons: list[str] = []
        penalties: list[str] = []
        if entry.stable_hash:
            stable_identity += 0.6
            reasons.append("stable_hash")
        if entry.attributes_digest:
            stable_identity += 0.4
            reasons.append("attribute_identity")
        visibility = 1.0 if entry.visible else 0.0
        interactivity = 1.0 if entry.interactive else 0.0
        accessibility = min(1.0, (0.5 if entry.role else 0.0) + (0.5 if entry.accessible_name else 0.0))
        geometry = _geometry_score(entry)
        score = overlap * 8.0
        score += stable_identity * 2.0
        score += visibility * (2.0 if self.prefer_visible else 0.5)
        score += interactivity * 3.0
        score += accessibility * 2.0
        score += geometry
        if preferred:
            score += 10.0
            reasons.append("preferred_ref")
        if overlap:
            reasons.append("goal_overlap")
        if entry.visible:
            reasons.append("visible")
        if entry.interactive:
            reasons.append("interactive")
        if entry.accessible_name:
            reasons.append("accessible_name")
        if entry.disabled:
            score -= 2.5
            penalties.append("disabled")
        if not entry.visible:
            score -= 1.5
            penalties.append("not_visible")
        if not entry.accessible_name and not entry.text_preview:
            score -= 1.0
            penalties.append("unnamed")
        if entry.tag_name in {"html", "body", "div", "span"} and not entry.role:
            score -= 0.75
            penalties.append("generic_container")
        return BrowserSelectorScore(
            entry=entry,
            score=score,
            goal_overlap=overlap,
            stable_identity=stable_identity,
            visibility=visibility,
            interactivity=interactivity,
            accessibility=accessibility,
            geometry=geometry,
            penalties=tuple(penalties),
            reasons=tuple(reasons),
        )


def _tokens(values: Iterable[str]) -> frozenset[str]:
    return frozenset(
        token
        for value in values
        for token in normalized_fact_key(value).split()
        if len(token) > 1
    )


def _geometry_score(entry: BrowserSelectorEntry) -> float:
    try:
        width = float(entry.bounds.get("width", 0.0))
        height = float(entry.bounds.get("height", 0.0))
        x = float(entry.bounds.get("x", 0.0))
        y = float(entry.bounds.get("y", 0.0))
    except (TypeError, ValueError):
        return 0.0
    if not all(math.isfinite(value) for value in (width, height, x, y)):
        return 0.0
    if width <= 0 or height <= 0:
        return 0.0
    area = width * height
    size = min(1.0, math.log1p(area) / math.log1p(120000.0))
    above_fold = 0.25 if y < 1200 else 0.0
    return size + above_fold
